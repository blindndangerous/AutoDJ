"""The acme library adapter, isolated from request handlers and playout."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import dns.resolver
from acme import challenges, client, errors, messages
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from autodj.acme_dns import MAX_RESPONSE, AcmeError, CloudflareDNS, retry_after


class BoundedNetwork(client.ClientNetwork):
    """Keep library signing/nonce behavior, bound transport and omit payload logging."""

    origin: str
    retry_at = 0.0
    order_receipt: Callable[[str, dict], None] | None = None
    order_endpoint = ""

    def _send_request(self, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != self.origin:
            raise AcmeError("The ACME server returned an unsupported endpoint.")
        kwargs.update(timeout=(5, 15), allow_redirects=False, stream=True, verify=True)
        kwargs.setdefault("headers", {})["User-Agent"] = "AutoDJ ACME"
        try:
            with self.session.request(method, url, *args, **kwargs) as response:
                chunks = bytearray()
                for chunk in response.iter_content(16384):
                    chunks.extend(chunk)
                    if len(chunks) > MAX_RESPONSE:
                        raise AcmeError("The ACME response exceeds the size limit.")
                response._content = bytes(chunks)
                response.encoding = "utf-8"
                self.retry_at = max(
                    self.retry_at, retry_after(response.headers.get("Retry-After"), time.time())
                )
                if 300 <= response.status_code < 400:
                    raise AcmeError("ACME redirects are unsupported.")
                if response.status_code == 429 or response.status_code >= 500:
                    raise AcmeError(
                        "The ACME server is temporarily unavailable or rate limited.",
                        permanent=False,
                        retry_at=self.retry_at,
                    )
                # Persist Location before the library fetches authorizations: those can fail.
                if (
                    url == self.order_endpoint
                    and response.status_code == 201
                    and self.order_receipt
                ):
                    self.order_receipt(response.headers["Location"], response.json())
                return response
        except AcmeError:
            raise
        except Exception:
            raise AcmeError(
                "The ACME server could not be reached.", permanent=False, retry_at=self.retry_at
            ) from None


def _key_pem() -> str:
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )


def normalize_dns(task: dict) -> None:
    """Preserve cleanup ownership from older singleton checkpoints."""
    for field in ("dns_receipt", "dns_intent"):
        entries = task.setdefault(field + "s", [])
        previous = task.pop(field, None)
        if previous and previous not in entries:
            entries.append(previous)


def cleanup_dns(task: dict, save: Callable[[dict], None], dns: CloudflareDNS) -> None:
    """Recover lost receipts and persist each independent cleanup result."""
    normalize_dns(task)
    save(task)
    receipts, intents = task["dns_receipts"], task["dns_intents"]
    failures: list[Exception] = []

    def matches(receipt: dict, intent: dict) -> bool:
        return (
            receipt["content"] == intent["value"]
            and receipt["comment"] == "AutoDJ ACME " + intent["marker"]
        )

    for intent in list(intents):
        if any(matches(receipt, intent) for receipt in receipts):
            continue
        try:
            receipt = dns.recover(task["domain"], intent["value"], intent["marker"])
        except Exception as exc:
            failures.append(exc)
            continue
        if receipt:
            receipts.append(receipt)
        else:
            intents.remove(intent)
        save(task)
    for receipt in list(receipts):
        try:
            dns.cleanup(receipt)
        except Exception as exc:
            failures.append(exc)
            continue
        receipts.remove(receipt)
        intents[:] = [intent for intent in intents if not matches(receipt, intent)]
        save(task)
    task["cleanup_pending"] = bool(receipts or intents)
    task["cleanup_retry_at"] = max(
        (exc.retry_at for exc in failures if isinstance(exc, AcmeError)), default=0
    )
    save(task)
    if task["cleanup_pending"]:
        raise AcmeError(
            "DNS challenge cleanup could not finish. Review DNS settings and retry cleanup.",
            permanent=any(isinstance(exc, AcmeError) and exc.permanent for exc in failures),
            retry_at=task["cleanup_retry_at"],
        )


class Authority:
    def __init__(
        self, account: dict, save_account: Callable[[dict], None], budget: Any, now: float
    ) -> None:
        from autodj.acme_service import DIRECTORIES

        self.account, self.save_account, self.budget, self.now = account, save_account, budget, now
        directory_url = DIRECTORIES[account["environment"]]
        self.net = BoundedNetwork(timeout=15)
        self.net.origin = urlsplit(directory_url).netloc
        directory = messages.Directory.from_json(self.net.get(directory_url).json())
        self.terms = getattr(directory.meta, "terms_of_service", None)
        if not self.terms or urlsplit(self.terms).scheme != "https":
            raise AcmeError("The ACME directory has no usable terms link.")
        self.client = client.ClientV2(directory, self.net)

    def register(self, environment: str) -> None:
        if not self.account.get("key"):
            self.account["key"] = _key_pem()
            self.save_account(self.account)
        self.net.key = client.jose.JWKRSA(
            key=serialization.load_pem_private_key(self.account["key"].encode(), password=None)
        )
        if self.account.get("registration"):
            self.net.account = messages.RegistrationResource.from_json(self.account["registration"])
            self.save_account(self.account)
            return
        self.budget.take("account:" + environment, self.now)
        try:
            registration = self.client.new_account(
                messages.NewRegistration.from_data(terms_of_service_agreed=True)
            )
        except errors.ConflictError as exc:
            registration = self.client.query_registration(
                messages.RegistrationResource(uri=exc.location, body=messages.Registration())
            )
        except messages.Error as exc:
            raise AcmeError(
                "ACME account registration was refused. Review the terms and account settings.",
                permanent=exc.code not in {"rateLimited", "serverInternal", "badNonce"},
                retry_at=self.net.retry_at,
            ) from None
        self.account["registration"] = registration.to_json()
        self.save_account(self.account)

    def issue(
        self, task: dict, save: Callable[[dict], None], dns: CloudflareDNS
    ) -> tuple[str, str, datetime | None]:
        deadline = datetime.now() + timedelta(minutes=14)
        try:
            normalize_dns(task)
            if not task.get("private_key"):
                task["private_key"] = _key_pem()
                save(task)
            key = serialization.load_pem_private_key(task["private_key"].encode(), password=None)
            if not isinstance(key, rsa.RSAPrivateKey):
                raise AcmeError("The saved ACME certificate key is invalid.")
            csr = (
                x509.CertificateSigningRequestBuilder()
                .subject_name(x509.Name([]))
                .add_extension(
                    x509.SubjectAlternativeName([x509.DNSName(task["domain"])]), critical=False
                )
                .sign(key, hashes.SHA256())
                .public_bytes(serialization.Encoding.PEM)
            )
            if task.get("order"):
                saved = task["order"]
                response = self.client._post_as_get(saved["uri"])
                order = messages.OrderResource(
                    uri=saved["uri"],
                    body=messages.Order.from_json(response.json()),
                    csr_pem=csr,
                    authorizations=[],
                )
            else:
                self.budget.take("order:" + task["environment"] + ":" + task["domain"], self.now)
                self.net.order_endpoint = self.client.directory["newOrder"]

                def receipt(uri: str, body: dict) -> None:
                    task["order"] = {"uri": uri, "body": body}
                    save(task)

                self.net.order_receipt = receipt
                order = self.client.new_order(csr)
                receipt(order.uri, order.body.to_json())
            if order.body.status == messages.STATUS_INVALID:
                raise AcmeError(
                    "The ACME order is invalid. Correct the DNS configuration before retrying."
                )
            for url in order.body.authorizations:
                response = self.client._post_as_get(url)
                authorization = messages.AuthorizationResource(
                    uri=url, body=messages.Authorization.from_json(response.json())
                )
                if authorization.body.status == messages.STATUS_VALID:
                    continue
                if authorization.body.status != messages.STATUS_PENDING:
                    raise AcmeError("ACME authorization failed. Check DNS and CAA before retrying.")
                challenge = next(
                    (
                        c
                        for c in authorization.body.challenges
                        if isinstance(c.chall, challenges.DNS01)
                    ),
                    None,
                )
                if challenge is None:
                    raise AcmeError("The ACME server did not offer DNS-01.")
                response_body, value = challenge.response_and_validation(self.net.key)
                marker = task.setdefault("dns_marker", uuid.uuid4().hex)
                # The marker precedes POST, so a lost response can be recovered by ownership.
                intent = {"value": value, "marker": marker}
                if intent not in task["dns_intents"]:
                    task["dns_intents"].append(intent)
                save(task)
                dns_receipt = dns.present(task["domain"], value, marker)
                if dns_receipt not in task["dns_receipts"]:
                    task["dns_receipts"].append(dns_receipt)
                save(task)
                self._wait_txt(task["domain"], value, deadline)
                self.client.answer_challenge(challenge, response_body)
            if order.body.status == messages.STATUS_PENDING:
                order = self.client.poll_authorizations(order, deadline)
            if order.body.status in {messages.STATUS_PROCESSING, messages.STATUS_VALID}:
                order = self.client.poll_finalization(order, deadline)
            else:
                order = self.client.finalize_order(order, deadline)
            certificate = order.fullchain_pem
            # Save before ARI or cleanup: a later failure must not repeat issuance.
            task["certificate"] = certificate
            save(task)
            try:
                ari, _ = self.client.renewal_time(certificate.encode())
            except Exception:
                ari = None
            return certificate, task["private_key"], ari
        except AcmeError:
            raise
        except (errors.ValidationError, errors.IssuanceError):
            raise AcmeError("ACME validation failed. Check DNS and CAA before retrying.") from None
        except messages.Error as exc:
            permanent = exc.code not in {"rateLimited", "serverInternal", "badNonce"}
            raise AcmeError(
                "ACME refused issuance. Check DNS, CAA and account terms.",
                permanent=permanent,
                retry_at=self.net.retry_at,
            ) from None
        except Exception:
            raise AcmeError(
                "ACME issuance could not finish. The saved order will be retried.",
                permanent=False,
                retry_at=self.net.retry_at,
            ) from None
        finally:
            with suppress(AcmeError):
                cleanup_dns(task, save, dns)

    @staticmethod
    def _wait_txt(domain: str, value: str, deadline: datetime) -> None:
        while datetime.now() < deadline:
            try:
                answer = dns.resolver.resolve(
                    "_acme-challenge." + domain, "TXT", lifetime=15, search=False
                )
                if answer.canonical_name.to_text().rstrip(".") != "_acme-challenge." + domain:
                    raise AcmeError("A CNAME at the challenge name is unsupported.")
                if any(b"".join(record.strings).decode() == value for record in answer):
                    return
            except dns.exception.DNSException:
                pass
            time.sleep(min(5, max(0, (deadline - datetime.now()).total_seconds())))
        raise AcmeError(
            "DNS propagation timed out. The saved order will be retried.", permanent=False
        )
