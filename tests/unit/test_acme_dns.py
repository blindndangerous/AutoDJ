"""DNS-01 provider boundaries, with no real DNS writes."""

import pytest


def test_domain_validation_and_preflight():
    from autodj.acme_dns import AcmeError, preflight, validate_domain

    assert validate_domain("Radio.Example.COM.", "example.com") == "radio.example.com"
    for name in (
        "localhost",
        "radio.local",
        "radio.lan",
        "radio.internal",
        "127.0.0.1",
        "::1",
        "*.example.com",
        "example.com.evil.org",
        "radio",
        "a..example.com",
    ):
        with pytest.raises(AcmeError):
            validate_domain(name, "example.com")
    result = preflight(
        "radio.example.com", resolve=lambda _: ["192.168.1.2"], local_addresses={"192.168.1.2"}
    )
    assert result == {"addresses": ["192.168.1.2"], "confirmed": True}
    result = preflight(
        "radio.example.com", resolve=lambda _: ["2001:db8::1"], local_addresses={"192.168.1.2"}
    )
    assert result == {"addresses": ["2001:db8::1"], "confirmed": False}


class FakeCloudflare:
    def __init__(self):
        self.records = [
            {
                "id": "b" * 32,
                "type": "TXT",
                "name": "_acme-challenge.radio.example.com",
                "content": "unrelated",
            }
        ]
        self.calls = []

    def __call__(self, method, path, *, body=None, params=None):
        self.calls.append((method, path, body, params))
        if path.endswith("/" + "a" * 32):
            return {"id": "a" * 32, "name": "example.com", "status": "active"}
        if method == "GET":
            return [r for r in self.records if all(r.get(k) == v for k, v in params.items())]
        if method == "POST":
            item = {**body, "id": "c" * 32}
            self.records.append(item)
            return item
        if method == "DELETE":
            self.records = [r for r in self.records if r["id"] != path.rsplit("/", 1)[1]]
            return {}
        raise AssertionError(method)


def test_owned_txt_cleanup_and_cname_refusal():
    """ADR-0019-08."""
    from autodj.acme_dns import AcmeError, CloudflareDNS

    fake = FakeCloudflare()
    dns = CloudflareDNS("a" * 32, "private-token", request=fake, cname=lambda _: False)
    assert dns.zone() == "example.com"
    receipt = dns.present("radio.example.com", "challenge-value", "unique-receipt")
    assert len(fake.records) == 2
    dns.cleanup(receipt)
    assert [r["content"] for r in fake.records] == ["unrelated"]
    assert all(method not in {"PUT", "PATCH"} for method, *_ in fake.calls)
    fake.records.append(
        {"id": "d" * 32, "name": "_acme-challenge.radio.example.com", "type": "CNAME"}
    )
    with pytest.raises(AcmeError, match="CNAME"):
        dns.present("radio.example.com", "new-value", "next-receipt")


def test_recovers_only_own_creation_receipt():
    from autodj.acme_dns import CloudflareDNS

    fake = FakeCloudflare()
    dns = CloudflareDNS("a" * 32, "private-token", request=fake, cname=lambda _: False)
    first = dns.present("radio.example.com", "challenge-value", "unique-receipt")
    recovered = dns.present("radio.example.com", "challenge-value", "unique-receipt")
    assert recovered == first
    assert len(fake.records) == 2


def test_invalid_zone_and_token_never_reach_network():
    from autodj.acme_dns import AcmeError, CloudflareDNS

    for zone, token in (("../other", "secret"), ("a" * 32, "secret\r\nheader")):
        with pytest.raises(AcmeError):
            CloudflareDNS(zone, token)


def test_cleanup_recovery_never_creates_a_missing_record():
    from autodj.acme_dns import CloudflareDNS

    fake = FakeCloudflare()
    dns = CloudflareDNS("a" * 32, "secret", request=fake, cname=lambda _: False)
    assert dns.recover("radio.example.com", "value", "receipt") is None
    assert all(method == "GET" for method, *_ in fake.calls)
    owned = dns.present("radio.example.com", "value", "receipt")
    assert dns.recover("radio.example.com", "value", "receipt") == owned


@pytest.mark.parametrize(
    "status,payload,headers",
    [
        (302, b"", {"Location": "https://evil.example"}),
        (429, b"private-token", {"Retry-After": "3600"}),
        (200, b"x" * (1024 * 1024 + 1), {}),
    ],
)
def test_cloudflare_transport_is_fixed_bounded_and_redacted(monkeypatch, status, payload, headers):
    from autodj import acme_dns

    calls = []

    class Connection:
        sock = None

        def __init__(self, host, timeout):
            calls.append((host, timeout))
            self.sock = self

        def connect(self):
            pass

        def settimeout(self, seconds):
            assert seconds == 15

        def request(self, *args):
            calls.append(args)

        def getresponse(self):
            self.status = status
            return self

        def getheader(self, name):
            return headers.get(name)

        def read(self, limit):
            assert limit == 1024 * 1024 + 1
            return payload

        def close(self):
            pass

    monkeypatch.setattr(acme_dns.http.client, "HTTPSConnection", Connection)
    dns = acme_dns.CloudflareDNS("a" * 32, "private-token")
    with pytest.raises(acme_dns.AcmeError) as caught:
        dns.zone()
    assert calls[0] == ("api.cloudflare.com", 5)
    assert "private-token" not in str(caught.value)
    if status == 429:
        assert caught.value.retry_at > 0 and not caught.value.permanent
