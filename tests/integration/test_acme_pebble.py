"""Opt-in DNS-01 against local Pebble binaries; no public CA or DNS provider.

Set AUTODJ_PEBBLE_BIN and AUTODJ_PEBBLE_CHALLTESTSRV_BIN to the two executables.
The fixture starts both on loopback, trusts its temporary TLS certificate only
inside this test, and terminates both children. Missing paths skip ordinary
suites; AUTODJ_REQUIRE_PEBBLE=1 makes missing executables a hard CI failure.
"""

import ipaddress
import json
import os
import socket
import subprocess
import time
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

import dns.resolver
import pytest
from acme import client
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from autodj.acme_client import Authority, BoundedNetwork, cleanup_dns
from autodj.acme_dns import AcmeError, CloudflareDNS
from autodj.acme_service import DIRECTORIES, Budget


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture
def pebble(tmp_path, monkeypatch):
    binaries = [
        os.environ.get(name, "") for name in ("AUTODJ_PEBBLE_BIN", "AUTODJ_PEBBLE_CHALLTESTSRV_BIN")
    ]
    if not all(path and Path(path).is_file() for path in binaries):
        if os.environ.get("AUTODJ_REQUIRE_PEBBLE") == "1":
            pytest.fail("Both Pebble executable paths are required by this job")
        pytest.skip("Set AUTODJ_PEBBLE_BIN and AUTODJ_PEBBLE_CHALLTESTSRV_BIN")
    ca_port, dns_port, management_port = _port(), _port(), _port()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "Local Pebble test")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "localhost.pem", tmp_path / "localhost-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    config = tmp_path / "pebble.json"
    config.write_text(
        json.dumps(
            {
                "pebble": {
                    "listenAddress": f"127.0.0.1:{ca_port}",
                    "managementListenAddress": "",
                    "certificate": str(cert_path),
                    "privateKey": str(key_path),
                    "httpPort": 5002,
                    "tlsPort": 5001,
                    "retryAfter": {"authz": 1, "order": 1},
                }
            }
        ),
        encoding="utf-8",
    )
    directory = f"https://localhost:{ca_port}/dir"
    management = f"http://127.0.0.1:{management_port}"
    environment = {
        **os.environ,
        "PEBBLE_VA_NOSLEEP": "1",
        "PEBBLE_VA_ALWAYS_VALID": "0",
        "PEBBLE_AUTHZREUSE": "0",
        "PEBBLE_WFE_NONCEREJECT": "0",
    }
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert_path))
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setitem(DIRECTORIES, "staging", directory)
    send_request = BoundedNetwork._send_request

    def local_terms(network, method, url, *args, **kwargs):
        response = send_request(network, method, url, *args, **kwargs)
        if url == directory:
            document = response.json()
            # Pebble advertises a data: ToS. Keep production's HTTPS-only policy;
            # substitute only test directory metadata, never protocol responses.
            assert document["meta"]["termsOfService"].startswith("data:")
            document["meta"]["termsOfService"] = f"https://localhost:{ca_port}/terms"
            response._content = json.dumps(document).encode()
        return response

    monkeypatch.setattr(BoundedNetwork, "_send_request", local_terms)
    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = ["127.0.0.1"]
    resolver.port = dns_port
    monkeypatch.setattr(dns.resolver, "resolve", resolver.resolve)
    with ExitStack() as stack:
        session = stack.enter_context(client.requests.Session())
        commands = [
            [
                binaries[1],
                "-dnsserver",
                f"127.0.0.1:{dns_port}",
                "-management",
                f"127.0.0.1:{management_port}",
                "-http01=",
                "-https01=",
                "-tlsalpn01=",
                "-doh=",
            ],
            [
                binaries[0],
                "-config",
                str(config),
                "-dnsserver",
                f"127.0.0.1:{dns_port}",
                "-strict=false",
            ],
        ]
        processes = []
        for index, command in enumerate(commands):
            log = stack.enter_context((tmp_path / f"pebble-{index}.log").open("w"))
            process = subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            stack.callback(_stop, process)
            processes.append(process)
        deadline = time.monotonic() + 15
        while True:
            assert all(p.poll() is None for p in processes), f"Pebble exited; see {tmp_path}"
            try:
                session.get(directory, timeout=1).raise_for_status()
                session.post(
                    management + "/dns-request-history",
                    json={"host": "radio.example.com"},
                    timeout=1,
                ).raise_for_status()
                break
            except client.requests.RequestException:
                if time.monotonic() >= deadline:
                    pytest.fail(f"Pebble startup timed out; see {tmp_path}")
                time.sleep(0.1)
        yield session, management, resolver


class LocalDNSProvider:
    """Test-only Cloudflare request bridge to challtestsrv's DNS management API."""

    def __init__(self, session, management):
        self.session, self.management = session, management
        self.records = [
            {
                "id": "b" * 32,
                "type": "TXT",
                "name": "_acme-challenge.radio.example.com",
                "content": "unrelated",
            }
        ]
        self.next_id = 12
        self.fail_delete = False
        self._publish(self.records[0]["name"])

    def _publish(self, name):
        response = self.session.post(
            self.management + "/clear-txt", json={"host": name + "."}, timeout=5
        )
        response.raise_for_status()
        for record in self.records:
            if record["name"] == name:
                response = self.session.post(
                    self.management + "/set-txt",
                    json={"host": name + ".", "value": record["content"]},
                    timeout=5,
                )
                response.raise_for_status()

    def __call__(self, method, path, *, body=None, params=None):
        if path == "/zones/" + "a" * 32:
            return {"id": "a" * 32, "name": "example.com", "status": "active"}
        if method == "GET":
            return [r for r in self.records if r["name"] == params["name"]]
        if method == "POST":
            record = {**body, "id": f"{self.next_id:032x}"}
            self.next_id += 1
            self.records.append(record)
            self._publish(record["name"])
            return record
        if method == "DELETE":
            if self.fail_delete:
                raise AcmeError("Local test provider unavailable", permanent=False)
            record = next(r for r in self.records if r["id"] == path.rsplit("/", 1)[1])
            self.records.remove(record)
            self._publish(record["name"])
            return {}
        raise AssertionError((method, path))


@pytest.mark.timeout(90)
@pytest.mark.parametrize("cleanup_interrupted", [False, True])
def test_pebble_dns01_issuance_and_owned_cleanup(pebble, tmp_path, cleanup_interrupted):
    """ADR-0019-19: actual CA validation, chain/key checks and persisted cleanup retry."""
    session, management, resolver = pebble
    provider = LocalDNSProvider(session, management)
    dns_provider = CloudflareDNS("a" * 32, "local-test-token", request=provider)
    provider.fail_delete = cleanup_interrupted
    account = {"environment": "staging"}
    ca = Authority(account, lambda _: None, Budget(tmp_path), time.time())
    ca.register("staging")
    task = {"environment": "staging", "domain": "radio.example.com"}
    checkpoint = tmp_path / "operation.json"

    def save(value):
        checkpoint.write_text(json.dumps(value), encoding="utf-8")

    fullchain, private_key, _ari = ca.issue(task, save, dns_provider)
    chain = x509.load_pem_x509_certificates(fullchain.encode())
    assert len(chain) >= 2
    chain[0].verify_directly_issued_by(chain[1])
    assert chain[0].extensions.get_extension_for_class(x509.SubjectAlternativeName).value == (
        x509.SubjectAlternativeName([x509.DNSName("radio.example.com")])
    )
    key = serialization.load_pem_private_key(private_key.encode(), password=None)
    assert chain[0].public_key().public_numbers() == key.public_key().public_numbers()
    history = session.post(
        management + "/dns-request-history",
        json={"host": "_acme-challenge.radio.example.com"},
        timeout=5,
    )
    history.raise_for_status()
    # One query is AutoDJ's propagation check; the CA must query DNS as well.
    assert sum(event["Question"]["Qtype"] == 16 for event in history.json()) > 1
    persisted = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert persisted["cleanup_pending"] is cleanup_interrupted
    if cleanup_interrupted:
        assert len(persisted["dns_receipts"]) == 1
        provider.fail_delete = False
        cleanup_dns(persisted, save, dns_provider)
    assert not persisted["dns_receipts"] and not persisted["dns_intents"]
    assert [
        b"".join(r.strings).decode()
        for r in resolver.resolve("_acme-challenge.radio.example.com", "TXT", lifetime=5)
    ] == ["unrelated"]
    assert all(not record.get("comment") for record in provider.records)
