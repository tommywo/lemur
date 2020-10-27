"""
.. module: lemur.plugins.lemur_acme.plugin
    :platform: Unix
    :synopsis: This module contains handlers for certain acme related tasks. It needed to be refactored to avoid circular imports
    :copyright: (c) 2018 by Netflix Inc., see AUTHORS for more
    :license: Apache, see LICENSE for more details.

    Snippets from https://raw.githubusercontent.com/alex/letsencrypt-aws/master/letsencrypt-aws.py

.. moduleauthor:: Kevin Glisson <kglisson@netflix.com>
.. moduleauthor:: Mikhail Khodorovskiy <mikhail.khodorovskiy@jivesoftware.com>
.. moduleauthor:: Curtis Castrapel <ccastrapel@netflix.com>
.. moduleauthor:: Mathias Petermann <mathias.petermann@projektfokus.ch>
"""
import datetime
import json
import time

import OpenSSL.crypto
import josepy as jose
from acme import challenges, errors, messages
from acme.client import BackwardsCompatibleClientV2, ClientNetwork
from acme.errors import TimeoutError
from acme.messages import Error as AcmeError
from flask import current_app

from lemur.common.utils import generate_private_key
from lemur.dns_providers import service as dns_provider_service
from lemur.exceptions import InvalidAuthority, UnknownProvider
from lemur.extensions import metrics, sentry

from lemur.plugins.lemur_acme import cloudflare, dyn, route53, ultradns, powerdns
from lemur.authorities import service as authorities_service
from retrying import retry


class AuthorizationRecord(object):
    def __init__(self, host, authz, dns_challenge, change_id):
        self.host = host
        self.authz = authz
        self.dns_challenge = dns_challenge
        self.change_id = change_id


class AcmeHandler(object):

    def reuse_account(self, authority):
        if not authority.options:
            raise InvalidAuthority("Invalid authority. Options not set")
        existing_key = False
        existing_regr = False

        for option in json.loads(authority.options):
            if option["name"] == "acme_private_key" and option["value"]:
                existing_key = True
            if option["name"] == "acme_regr" and option["value"]:
                existing_regr = True

        if not existing_key and current_app.config.get("ACME_PRIVATE_KEY"):
            existing_key = True

        if not existing_regr and current_app.config.get("ACME_REGR"):
            existing_regr = True

        if existing_key and existing_regr:
            return True
        else:
            return False

    def strip_wildcard(self, host):
        """Removes the leading *. and returns Host and whether it was removed or not (True/False)"""
        prefix = "*."
        if host.startswith(prefix):
            return host[len(prefix):], True
        return host, False

    def maybe_add_extension(self, host, dns_provider_options):
        if dns_provider_options and dns_provider_options.get(
                "acme_challenge_extension"
        ):
            host = host + dns_provider_options.get("acme_challenge_extension")
        return host

    def request_certificate(self, acme_client, authorizations, order):
        for authorization in authorizations:
            for authz in authorization.authz:
                authorization_resource, _ = acme_client.poll(authz)

        deadline = datetime.datetime.now() + datetime.timedelta(seconds=360)

        try:
            orderr = acme_client.poll_and_finalize(order, deadline)

        except (AcmeError, TimeoutError):
            sentry.captureException(extra={"order_url": str(order.uri)})
            metrics.send("request_certificate_error", "counter", 1, metric_tags={"uri": order.uri})
            current_app.logger.error(
                f"Unable to resolve Acme order: {order.uri}", exc_info=True
            )
            raise
        except errors.ValidationError:
            if order.fullchain_pem:
                orderr = order
            else:
                raise

        metrics.send("request_certificate_success", "counter", 1, metric_tags={"uri": order.uri})
        current_app.logger.info(
            f"Successfully resolved Acme order: {order.uri}", exc_info=True
        )

        pem_certificate = OpenSSL.crypto.dump_certificate(
            OpenSSL.crypto.FILETYPE_PEM,
            OpenSSL.crypto.load_certificate(
                OpenSSL.crypto.FILETYPE_PEM, orderr.fullchain_pem
            ),
        ).decode()

        if current_app.config.get("IDENTRUST_CROSS_SIGNED_LE_ICA", False) \
                and datetime.datetime.now() < datetime.datetime.strptime(
                current_app.config.get("IDENTRUST_CROSS_SIGNED_LE_ICA_EXPIRATION_DATE", "17/03/21"), '%d/%m/%y'):
            pem_certificate_chain = current_app.config.get("IDENTRUST_CROSS_SIGNED_LE_ICA")
        else:
            pem_certificate_chain = orderr.fullchain_pem[
                                    len(pem_certificate):  # noqa
                                    ].lstrip()

        current_app.logger.debug(
            "{0} {1}".format(type(pem_certificate), type(pem_certificate_chain))
        )
        return pem_certificate, pem_certificate_chain

    @retry(stop_max_attempt_number=5, wait_fixed=5000)
    def setup_acme_client(self, authority):
        if not authority.options:
            raise InvalidAuthority("Invalid authority. Options not set")
        options = {}

        for option in json.loads(authority.options):
            options[option["name"]] = option.get("value")
        email = options.get("email", current_app.config.get("ACME_EMAIL"))
        tel = options.get("telephone", current_app.config.get("ACME_TEL"))
        directory_url = options.get(
            "acme_url", current_app.config.get("ACME_DIRECTORY_URL")
        )

        existing_key = options.get(
            "acme_private_key", current_app.config.get("ACME_PRIVATE_KEY")
        )
        existing_regr = options.get("acme_regr", current_app.config.get("ACME_REGR"))

        if existing_key and existing_regr:
            current_app.logger.debug("Reusing existing ACME account")
            # Reuse the same account for each certificate issuance
            key = jose.JWK.json_loads(existing_key)
            regr = messages.RegistrationResource.json_loads(existing_regr)
            current_app.logger.debug(
                "Connecting with directory at {0}".format(directory_url)
            )
            net = ClientNetwork(key, account=regr)
            client = BackwardsCompatibleClientV2(net, key, directory_url)
            return client, {}
        else:
            # Create an account for each certificate issuance
            key = jose.JWKRSA(key=generate_private_key("RSA2048"))

            current_app.logger.debug("Creating a new ACME account")
            current_app.logger.debug(
                "Connecting with directory at {0}".format(directory_url)
            )

            net = ClientNetwork(key, account=None, timeout=3600)
            client = BackwardsCompatibleClientV2(net, key, directory_url)
            registration = client.new_account_and_tos(
                messages.NewRegistration.from_data(email=email)
            )

            # if store_account is checked, add the private_key and registration resources to the options
            if options['store_account']:
                new_options = json.loads(authority.options)
                # the key returned by fields_to_partial_json is missing the key type, so we add it manually
                key_dict = key.fields_to_partial_json()
                key_dict["kty"] = "RSA"
                acme_private_key = {
                    "name": "acme_private_key",
                    "value": json.dumps(key_dict)
                }
                new_options.append(acme_private_key)

                acme_regr = {
                    "name": "acme_regr",
                    "value": json.dumps({"body": {}, "uri": registration.uri})
                }
                new_options.append(acme_regr)

                authorities_service.update_options(authority.id, options=json.dumps(new_options))

            current_app.logger.debug("Connected: {0}".format(registration.uri))

        return client, registration

    def get_domains(self, options):
        """
        Fetches all domains currently requested
        :param options:
        :return:
        """
        current_app.logger.debug("Fetching domains")

        domains = [options["common_name"]]
        if options.get("extensions"):
            for dns_name in options["extensions"]["sub_alt_names"]["names"]:
                if dns_name.value not in domains:
                    domains.append(dns_name.value)

        current_app.logger.debug("Got these domains: {0}".format(domains))
        return domains


class AcmeDnsHandler(AcmeHandler):

    def __init__(self):
        self.dns_providers_for_domain = {}
        try:
            self.all_dns_providers = dns_provider_service.get_all_dns_providers()
        except Exception as e:
            metrics.send("AcmeHandler_init_error", "counter", 1)
            sentry.captureException()
            current_app.logger.error(f"Unable to fetch DNS Providers: {e}")
            self.all_dns_providers = []

    def get_all_zones(self, dns_provider):
        dns_provider_options = json.loads(dns_provider.credentials)
        account_number = dns_provider_options.get("account_id")
        dns_provider_plugin = self.get_dns_provider(dns_provider.provider_type)
        return dns_provider_plugin.get_zones(account_number=account_number)

    def get_dns_challenges(self, host, authorizations):
        """Get dns challenges for provided domain"""

        domain_to_validate, is_wildcard = self.strip_wildcard(host)
        dns_challenges = []
        for authz in authorizations:
            if not authz.body.identifier.value.lower() == domain_to_validate.lower():
                continue
            if is_wildcard and not authz.body.wildcard:
                continue
            if not is_wildcard and authz.body.wildcard:
                continue
            for combo in authz.body.challenges:
                if isinstance(combo.chall, challenges.DNS01):
                    dns_challenges.append(combo)

        return dns_challenges

    def get_dns_provider(self, type):
        provider_types = {
            "cloudflare": cloudflare,
            "dyn": dyn,
            "route53": route53,
            "ultradns": ultradns,
            "powerdns": powerdns
        }
        provider = provider_types.get(type)
        if not provider:
            raise UnknownProvider("No such DNS provider: {}".format(type))
        return provider

    def start_dns_challenge(
            self,
            acme_client,
            account_number,
            host,
            dns_provider,
            order,
            dns_provider_options,
    ):
        current_app.logger.debug("Starting DNS challenge for {0}".format(host))

        change_ids = []
        dns_challenges = self.get_dns_challenges(host, order.authorizations)
        host_to_validate, _ = self.strip_wildcard(host)
        host_to_validate = self.maybe_add_extension(
            host_to_validate, dns_provider_options
        )

        if not dns_challenges:
            sentry.captureException()
            metrics.send("start_dns_challenge_error_no_dns_challenges", "counter", 1)
            raise Exception("Unable to determine DNS challenges from authorizations")

        for dns_challenge in dns_challenges:
            change_id = dns_provider.create_txt_record(
                dns_challenge.validation_domain_name(host_to_validate),
                dns_challenge.validation(acme_client.client.net.key),
                account_number,
            )
            change_ids.append(change_id)

        return AuthorizationRecord(
            host, order.authorizations, dns_challenges, change_ids
        )

    def complete_dns_challenge(self, acme_client, authz_record):
        current_app.logger.debug(
            "Finalizing DNS challenge for {0}".format(
                authz_record.authz[0].body.identifier.value
            )
        )
        dns_providers = self.dns_providers_for_domain.get(authz_record.host)
        if not dns_providers:
            metrics.send("complete_dns_challenge_error_no_dnsproviders", "counter", 1)
            raise Exception(
                "No DNS providers found for domain: {}".format(authz_record.host)
            )

        for dns_provider in dns_providers:
            # Grab account number (For Route53)
            dns_provider_options = json.loads(dns_provider.credentials)
            account_number = dns_provider_options.get("account_id")
            dns_provider_plugin = self.get_dns_provider(dns_provider.provider_type)
            for change_id in authz_record.change_id:
                try:
                    dns_provider_plugin.wait_for_dns_change(
                        change_id, account_number=account_number
                    )
                except Exception:
                    metrics.send("complete_dns_challenge_error", "counter", 1)
                    sentry.captureException()
                    current_app.logger.debug(
                        f"Unable to resolve DNS challenge for change_id: {change_id}, account_id: "
                        f"{account_number}",
                        exc_info=True,
                    )
                    raise

            for dns_challenge in authz_record.dns_challenge:
                response = dns_challenge.response(acme_client.client.net.key)

                verified = response.simple_verify(
                    dns_challenge.chall,
                    authz_record.host,
                    acme_client.client.net.key.public_key(),
                )

            if not verified:
                metrics.send("complete_dns_challenge_verification_error", "counter", 1)
                raise ValueError("Failed verification")

            time.sleep(5)
            res = acme_client.answer_challenge(dns_challenge, response)
            current_app.logger.debug(f"answer_challenge response: {res}")

    def get_authorizations(self, acme_client, order, order_info):
        authorizations = []

        for domain in order_info.domains:
            if not self.dns_providers_for_domain.get(domain):
                metrics.send(
                    "get_authorizations_no_dns_provider_for_domain", "counter", 1
                )
                raise Exception("No DNS providers found for domain: {}".format(domain))
            for dns_provider in self.dns_providers_for_domain[domain]:
                dns_provider_plugin = self.get_dns_provider(dns_provider.provider_type)
                dns_provider_options = json.loads(dns_provider.credentials)
                account_number = dns_provider_options.get("account_id")
                authz_record = self.start_dns_challenge(
                    acme_client,
                    account_number,
                    domain,
                    dns_provider_plugin,
                    order,
                    dns_provider.options,
                )
                authorizations.append(authz_record)
        return authorizations

    def autodetect_dns_providers(self, domain):
        """
        Get DNS providers associated with a domain when it has not been provided for certificate creation.
        :param domain:
        :return: dns_providers: List of DNS providers that have the correct zone.
        """
        self.dns_providers_for_domain[domain] = []
        match_length = 0
        for dns_provider in self.all_dns_providers:
            if not dns_provider.domains:
                continue
            for name in dns_provider.domains:
                if name == domain or domain.endswith("." + name):
                    if len(name) > match_length:
                        self.dns_providers_for_domain[domain] = [dns_provider]
                        match_length = len(name)
                    elif len(name) == match_length:
                        self.dns_providers_for_domain[domain].append(dns_provider)

        return self.dns_providers_for_domain

    def finalize_authorizations(self, acme_client, authorizations):
        for authz_record in authorizations:
            self.complete_dns_challenge(acme_client, authz_record)

        for authz_record in authorizations:
            dns_challenges = authz_record.dns_challenge
            for dns_challenge in dns_challenges:
                dns_providers = self.dns_providers_for_domain.get(authz_record.host)
                for dns_provider in dns_providers:
                    # Grab account number (For Route53)
                    dns_provider_plugin = self.get_dns_provider(
                        dns_provider.provider_type
                    )
                    dns_provider_options = json.loads(dns_provider.credentials)
                    account_number = dns_provider_options.get("account_id")
                    host_to_validate, _ = self.strip_wildcard(authz_record.host)
                    host_to_validate = self.maybe_add_extension(
                        host_to_validate, dns_provider_options
                    )
                    dns_provider_plugin.delete_txt_record(
                        authz_record.change_id,
                        account_number,
                        dns_challenge.validation_domain_name(host_to_validate),
                        dns_challenge.validation(acme_client.client.net.key),
                    )

        return authorizations

    def cleanup_dns_challenges(self, acme_client, authorizations):
        """
        Best effort attempt to delete DNS challenges that may not have been deleted previously. This is usually called
        on an exception

        :param acme_client:
        :param account_number:
        :param dns_provider:
        :param authorizations:
        :param dns_provider_options:
        :return:
        """
        for authz_record in authorizations:
            dns_providers = self.dns_providers_for_domain.get(authz_record.host)
            for dns_provider in dns_providers:
                # Grab account number (For Route53)
                dns_provider_options = json.loads(dns_provider.credentials)
                account_number = dns_provider_options.get("account_id")
                dns_challenges = authz_record.dns_challenge
                host_to_validate, _ = self.strip_wildcard(authz_record.host)
                host_to_validate = self.maybe_add_extension(
                    host_to_validate, dns_provider_options
                )
                dns_provider_plugin = self.get_dns_provider(dns_provider.provider_type)
                for dns_challenge in dns_challenges:
                    try:
                        dns_provider_plugin.delete_txt_record(
                            authz_record.change_id,
                            account_number,
                            dns_challenge.validation_domain_name(host_to_validate),
                            dns_challenge.validation(acme_client.client.net.key),
                        )
                    except Exception as e:
                        # If this fails, it's most likely because the record doesn't exist (It was already cleaned up)
                        # or we're not authorized to modify it.
                        metrics.send("cleanup_dns_challenges_error", "counter", 1)
                        sentry.captureException()
                        pass
