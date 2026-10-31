"""Exercise the real acme library/JWS adapter against an in-memory HTTP authority."""

import base64
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from acme import client
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from autodj.acme_client import Authority, BoundedNetwork, cleanup_dns
from autodj.acme_dns import AcmeError, CloudflareDNS
from autodj.acme_service import Budget
from tests.unit.test_acme_dns import FakeCloudflare

ORIGIN = "https://acme-staging-v02.api.letsencrypt.org"


class AuthorityHTTP:
    def __init__(self):
        self.validated = False
        self.finalized = False
        self.orders = 0
        self.fail_auth_once = False
        self.ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = ""
        self.calls = []
        self.reject_repeat_finalize = False
        self.directory_retry = False
        self.authorization_count = 1
        self.validated_ids = set()

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert kwargs["allow_redirects"] is False and kwargs["verify"] is True
        assert kwargs["timeout"] == (5, 15) and kwargs["stream"] is True
        path = url.removeprefix(ORIGIN)
        response = client.requests.Response()
        response.status_code = 200
        response.headers.update(
            {"Replay-Nonce": "bm9uY2UtMDEyMzQ1Njc4OTA", "Content-Type": "application/json"}
        )
        response.url = url
        body = {}
        if path == "/directory":
            if self.directory_retry:
                response.status_code = 503
                response.headers["Retry-After"] = "7200"
                response.headers["Content-Type"] = "application/problem+json"
                response.raw = io.BytesIO(
                    json.dumps({"type": "urn:ietf:params:acme:error:serverInternal"}).encode()
                )
                return response
            body = {
                "newNonce": ORIGIN + "/nonce",
                "newAccount": ORIGIN + "/account",
                "newOrder": ORIGIN + "/new-order",
                "revokeCert": ORIGIN + "/revoke",
                "keyChange": ORIGIN + "/key",
                "meta": {"termsOfService": "https://example.com/terms"},
            }
        elif path == "/account":
            body = {"status": "valid", "contact": [], "orders": ORIGIN + "/orders"}
            response.status_code = 201
            response.headers["Location"] = ORIGIN + "/account/1"
        elif path in {"/new-order", "/order/1", "/finalize/1"}:
            if path == "/new-order":
                self.orders += 1
                response.status_code = 201
                response.headers["Location"] = ORIGIN + "/order/1"
            if path == "/finalize/1":
                if self.finalized and self.reject_repeat_finalize:
                    response.status_code = 403
                    response.headers["Content-Type"] = "application/problem+json"
                    response.raw = io.BytesIO(
                        json.dumps({"type": "urn:ietf:params:acme:error:unauthorized"}).encode()
                    )
                    return response
                payload = json.loads(kwargs["data"])["payload"]
                data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
                encoded = data["csr"]
                csr = x509.load_der_x509_csr(
                    base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
                )
                now = datetime.now(UTC)
                cert = (
                    x509.CertificateBuilder()
                    .subject_name(csr.subject)
                    .issuer_name(x509.Name([]))
                    .public_key(csr.public_key())
                    .serial_number(x509.random_serial_number())
                    .not_valid_before(now - timedelta(minutes=1))
                    .not_valid_after(now + timedelta(days=6))
                    .add_extension(
                        csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value,
                        False,
                    )
                    .sign(self.ca_key, hashes.SHA256())
                )
                self.pem = cert.public_bytes(serialization.Encoding.PEM).decode()
                self.finalized = True
            body = {
                "status": "valid" if self.finalized else "ready" if self.validated else "pending",
                "identifiers": [{"type": "dns", "value": "radio.example.com"}],
                "authorizations": [
                    ORIGIN + f"/auth/{i + 1}" for i in range(self.authorization_count)
                ],
                "finalize": ORIGIN + "/finalize/1",
            }
            if self.finalized:
                body["certificate"] = ORIGIN + "/cert/1"
        elif path.startswith("/auth/"):
            ident = path.rsplit("/", 1)[1]
            if self.fail_auth_once:
                self.fail_auth_once = False
                raise OSError("network failed after order receipt")
            body = {
                "identifier": {"type": "dns", "value": "radio.example.com"},
                "status": "valid" if ident in self.validated_ids else "pending",
                "challenges": [
                    {
                        "type": "dns-01",
                        "url": ORIGIN + "/challenge/" + ident,
                        "status": "pending",
                        "token": base64.urlsafe_b64encode(("challenge-token-" + ident).encode())
                        .decode()
                        .rstrip("="),
                    }
                ],
            }
        elif path.startswith("/challenge/"):
            ident = path.rsplit("/", 1)[1]
            self.validated_ids.add(ident)
            self.validated = len(self.validated_ids) == self.authorization_count
            body = {
                "type": "dns-01",
                "url": url,
                "status": "valid",
                # A fake ACME challenge token: base64url of "01234567890123456789".
                "token": "MDEyMzQ1Njc4OTAxMjM0NTY3ODk",  # gitleaks:allow
            }
            response.headers["Link"] = f'<{ORIGIN}/auth/{ident}>;rel="up"'
        elif path == "/cert/1":
            response.headers["Content-Type"] = "application/pem-certificate-chain"
        elif path != "/nonce":
            raise AssertionError(path)
        response.raw = io.BytesIO(
            self.pem.encode() if path == "/cert/1" else json.dumps(body).encode()
        )
        return response

    def close(self):
        pass


@pytest.fixture
def authority(tmp_path, monkeypatch):
    http = AuthorityHTTP()
    original = BoundedNetwork.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.session.close()
        self.session = http

    monkeypatch.setattr(BoundedNetwork, "__init__", init)
    monkeypatch.setattr(client.time, "sleep", lambda _: None)
    ca = Authority({"environment": "staging"}, lambda _: None, Budget(tmp_path), 1000)
    ca.register("staging")
    fake_dns = FakeCloudflare()
    provider = CloudflareDNS("a" * 32, "secret", request=fake_dns, cname=lambda _: False)

    def txt(domain, value, deadline):
        assert any(r.get("content") == value for r in fake_dns.records)

    monkeypatch.setattr(ca, "_wait_txt", txt)
    return ca, http, provider, fake_dns


def test_real_library_dns01_and_cleanup(authority):
    ca, http, dns, records = authority
    task = {"environment": "staging", "domain": "radio.example.com"}
    checkpoints = []
    cert, key, ari = ca.issue(task, lambda t: checkpoints.append(json.loads(json.dumps(t))), dns)
    assert "CERTIFICATE" in cert and "PRIVATE KEY" in key
    assert http.orders == 1 and http.validated and http.finalized
    assert [r["content"] for r in records.records] == ["unrelated"]
    assert any(c.get("dns_receipts") for c in checkpoints)
    assert ari is None


def test_library_resume_uses_saved_order_after_auth_fetch_failure(authority):
    """ADR-0019-15."""
    ca, http, dns, _records = authority
    task = {"environment": "staging", "domain": "radio.example.com"}
    http.fail_auth_once = True
    with pytest.raises(AcmeError):
        ca.issue(task, lambda _: None, dns)
    assert task["order"]["uri"] == ORIGIN + "/order/1"
    ca.issue(task, lambda _: None, dns)
    assert http.orders == 1


def test_network_refuses_other_origins_without_sending(authority):
    ca, http, *_ = authority
    count = len(http.calls)
    with pytest.raises(AcmeError, match="endpoint"):
        ca.net.get("https://evil.example/steal")
    assert len(http.calls) == count


def test_valid_order_resume_does_not_finalize_again(authority):
    ca, http, dns, _records = authority
    task = {"environment": "staging", "domain": "radio.example.com"}
    ca.issue(task, lambda _: None, dns)
    http.reject_repeat_finalize = True
    task.pop("certificate")  # Simulate interruption while retrieving the finalized pair.
    certificate, _, _ = ca.issue(task, lambda _: None, dns)
    assert certificate == http.pem and http.orders == 1


def test_directory_failure_preserves_retry_after(authority, tmp_path):
    import time

    _ca, http, *_ = authority
    http.directory_retry = True
    with pytest.raises(AcmeError) as caught:
        Authority({"environment": "staging"}, lambda _: None, Budget(tmp_path), time.time())
    assert caught.value.retry_at >= time.time() + 7100
    assert not caught.value.permanent


@pytest.mark.parametrize("fail_first_cleanup", [False, True])
def test_two_authorizations_keep_every_receipt_and_cleanup_independently(
    authority, monkeypatch, fail_first_cleanup
):
    """ADR-0019-22."""
    ca, http, provider, records = authority
    http.authorization_count = 2
    original_request = records.__call__
    next_id = 12

    def request(method, path, **kwargs):
        nonlocal next_id
        result = original_request(method, path, **kwargs)
        if method == "POST":
            result["id"] = f"{next_id:032x}"
            next_id += 1
        return result

    provider._request = request
    cleanup = provider.cleanup

    def clean(receipt):
        if fail_first_cleanup and receipt["id"] == f"{12:032x}":
            raise AcmeError("Temporary DNS failure", permanent=False)
        cleanup(receipt)

    monkeypatch.setattr(provider, "cleanup", clean)
    task = {"environment": "staging", "domain": "radio.example.com"}
    checkpoints = []
    ca.issue(task, lambda t: checkpoints.append(json.loads(json.dumps(t))), provider)
    owned = [r for r in records.records if r.get("comment")]
    assert len(owned) == int(fail_first_cleanup)
    assert any(len(c.get("dns_receipts", [])) == 2 for c in checkpoints)
    assert len(task.get("dns_receipts", [])) == int(fail_first_cleanup)
    assert len(task.get("dns_intents", [])) == int(fail_first_cleanup)
    assert task["cleanup_pending"] is fail_first_cleanup
    assert records.records[0]["content"] == "unrelated"


@pytest.mark.parametrize("lost_receipt", [False, True])
def test_cleanup_recovers_legacy_singleton_checkpoints(authority, lost_receipt):
    _ca, _http, provider, records = authority
    receipt = provider.present("radio.example.com", "legacy-value", "legacy-marker")
    task = {
        "domain": "radio.example.com",
        "dns_intent": {"value": "legacy-value", "marker": "legacy-marker"},
    }
    if not lost_receipt:
        task["dns_receipt"] = receipt
    checkpoints = []
    cleanup_dns(task, lambda t: checkpoints.append(json.loads(json.dumps(t))), provider)
    assert [r["content"] for r in records.records] == ["unrelated"]
    assert not task["dns_receipts"] and not task["dns_intents"]
    assert not task["cleanup_pending"]
    assert "dns_receipt" not in task and "dns_intent" not in task
    assert any(c["dns_receipts"] for c in checkpoints)


def test_cleanup_preserves_provider_retry_after_and_permanent_failure(authority, monkeypatch):
    _ca, _http, provider, _records = authority
    receipt = provider.present("radio.example.com", "value", "marker")
    task = {"domain": "radio.example.com", "dns_receipts": [receipt]}

    def fail(_receipt):
        raise AcmeError("Safe provider error", permanent=True, retry_at=9000)

    monkeypatch.setattr(provider, "cleanup", fail)
    with pytest.raises(AcmeError) as caught:
        cleanup_dns(task, lambda _: None, provider)
    assert caught.value.permanent
    assert caught.value.retry_at == 9000
    assert task["cleanup_retry_at"] == 9000
    assert task["cleanup_pending"] and task["dns_receipts"] == [receipt]
