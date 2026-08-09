from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import replace

import pytest

from autodj.config import ServerConfig
from autodj.security import COOKIE_NAME, SecurityPolicy, audit_record, new_request_id

_TOKEN = "test-access-token-that-is-32-bytes-long"
_ROTATED_TOKEN = "rotated-access-token-that-is-32-bytes"


def _server(**changes: object) -> ServerConfig:
    return replace(
        ServerConfig(
            allowed_hosts=["radio.local", "192.168.1.5", "::1"],
            allowed_origins=[
                "https://radio.local:8080",
                "http://192.168.1.5:8080",
                "http://[::1]:8080",
            ],
        ),
        **changes,
    )


def _signed_cookie(
    expires: str,
    *,
    nonce: str = "a" * 32,
    token: str = _TOKEN,
) -> str:
    payload = f"{expires}.{nonce}"
    signature = hmac.new(token.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def test_cookie_name_is_stable_for_server_integration() -> None:
    assert COOKIE_NAME == "autodj_session"


def test_authentication_required_tracks_configured_token() -> None:
    assert SecurityPolicy(_server()).authentication_required is False
    assert SecurityPolicy(_server(access_token=_TOKEN)).authentication_required is True


def test_access_token_comparison_uses_constant_time_bytes(monkeypatch) -> None:
    seen: list[tuple[bytes, bytes]] = []

    def compare(left: bytes, right: bytes) -> bool:
        seen.append((left, right))
        return True

    monkeypatch.setattr(secrets, "compare_digest", compare)

    assert SecurityPolicy(_server(access_token=_TOKEN)).verify_access_token("candidate")
    assert seen == [(b"candidate", _TOKEN.encode("utf-8"))]


def test_access_token_rejects_missing_configuration_and_hostile_candidates() -> None:
    assert SecurityPolicy(_server()).verify_access_token(_TOKEN) is False
    policy = SecurityPolicy(_server(access_token=_TOKEN))
    assert policy.verify_access_token("not-secret-\N{LOCK}") is False
    assert policy.verify_access_token("lone-surrogate-\ud800") is False
    assert policy.verify_access_token(None) is False  # type: ignore[arg-type]


def test_access_token_never_appears_in_policy_repr() -> None:
    assert _TOKEN not in repr(SecurityPolicy(_server(access_token=_TOKEN)))


def test_session_round_trip_uses_integer_expiry_and_generated_nonce(monkeypatch) -> None:
    nonces: list[int] = []

    def token_hex(byte_count: int) -> str:
        nonces.append(byte_count)
        return "a" * (byte_count * 2)

    monkeypatch.setattr(secrets, "token_hex", token_hex)
    policy = SecurityPolicy(
        _server(access_token=_TOKEN, session_ttl_seconds=60), now=lambda: 1000.9
    )

    cookie = policy.issue_session()

    assert cookie == _signed_cookie("1060")
    assert policy.verify_session(cookie) is True
    assert nonces == [16]


def test_issue_session_requires_configured_token() -> None:
    with pytest.raises(RuntimeError, match="access token is not configured"):
        SecurityPolicy(_server()).issue_session()


def test_session_expiry_boundary_is_inclusive() -> None:
    cookie = SecurityPolicy(
        _server(access_token=_TOKEN, session_ttl_seconds=60), now=lambda: 1000
    ).issue_session()

    assert SecurityPolicy(_server(access_token=_TOKEN), now=lambda: 1060).verify_session(cookie)
    assert not SecurityPolicy(_server(access_token=_TOKEN), now=lambda: 1061).verify_session(cookie)


@pytest.mark.parametrize("component", [0, 1, 2])
def test_session_rejects_tampering_of_each_component(component: int) -> None:
    policy = SecurityPolicy(_server(access_token=_TOKEN), now=lambda: 1000)
    parts = policy.issue_session().split(".")
    parts[component] = ("b" if parts[component][0] != "b" else "c") + parts[component][1:]

    assert policy.verify_session(".".join(parts)) is False


def test_session_signature_comparison_uses_constant_time_bytes(monkeypatch) -> None:
    policy = SecurityPolicy(_server(access_token=_TOKEN), now=lambda: 1000)
    cookie = policy.issue_session()
    signature = cookie.rsplit(".", 1)[1].encode("ascii")
    seen: list[tuple[bytes, bytes]] = []

    def compare(left: bytes, right: bytes) -> bool:
        seen.append((left, right))
        return True

    monkeypatch.setattr(secrets, "compare_digest", compare)

    assert policy.verify_session(cookie)
    assert seen == [(signature, signature)]


def test_session_rejects_when_constant_time_signature_comparison_fails(monkeypatch) -> None:
    policy = SecurityPolicy(_server(access_token=_TOKEN), now=lambda: 1000)
    cookie = policy.issue_session()
    signature = cookie.rsplit(".", 1)[1].encode("ascii")
    seen: list[tuple[bytes, bytes]] = []

    def compare(left: bytes, right: bytes) -> bool:
        seen.append((left, right))
        return False

    monkeypatch.setattr(secrets, "compare_digest", compare)

    assert policy.verify_session(cookie) is False
    assert seen == [(signature, signature)]


def test_session_token_rotation_invalidates_existing_cookie() -> None:
    cookie = SecurityPolicy(_server(access_token=_TOKEN), now=lambda: 1000).issue_session()

    assert (
        SecurityPolicy(_server(access_token=_ROTATED_TOKEN), now=lambda: 1000).verify_session(
            cookie
        )
        is False
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "only-one-component",
        "1.two",
        "1.two.three.four",
        "." + "a" * 32 + "." + "b" * 64,
        "+1000." + "a" * 32 + "." + "b" * 64,
        "01000." + "a" * 32 + "." + "b" * 64,
        "-1." + "a" * 32 + "." + "b" * 64,
        "9223372036854775808." + "a" * 32 + "." + "b" * 64,
        "1000.short." + "b" * 64,
        "1000." + "A" * 32 + "." + "b" * 64,
        "1000." + "g" * 32 + "." + "b" * 64,
        "1000." + "a" * 32 + ".short",
        "1000." + "a" * 32 + "." + "B" * 64,
        "1000." + "a" * 32 + "." + "g" * 64,
        "1000." + "a" * 32 + "." + "b" * 63 + "\ud800",
        123,
        b"1000.cookie.value",
    ],
)
def test_malformed_session_values_are_rejected_without_raising(value: object) -> None:
    policy = SecurityPolicy(_server(access_token=_TOKEN), now=lambda: 1000)
    assert policy.verify_session(value) is False  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad_now",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        "not-a-time",
    ],
)
def test_invalid_clock_values_reject_session_without_raising(bad_now: object) -> None:
    cookie = SecurityPolicy(_server(access_token=_TOKEN), now=lambda: 1000).issue_session()
    policy = SecurityPolicy(_server(access_token=_TOKEN), now=lambda: bad_now)  # type: ignore[arg-type]

    assert policy.verify_session(cookie) is False


@pytest.mark.parametrize("error_type", [ValueError, OverflowError])
def test_clock_conversion_errors_reject_session_without_raising(
    error_type: type[Exception],
) -> None:
    cookie = SecurityPolicy(_server(access_token=_TOKEN), now=lambda: 1000).issue_session()

    def broken_clock() -> float:
        raise error_type("clock failed")

    assert (
        SecurityPolicy(_server(access_token=_TOKEN), now=broken_clock).verify_session(cookie)
        is False
    )


@pytest.mark.parametrize(
    "bad_now",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        "not-a-time",
    ],
)
def test_invalid_clock_values_fail_session_issue_safely(bad_now: object) -> None:
    policy = SecurityPolicy(_server(access_token=_TOKEN), now=lambda: bad_now)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="clock") as raised:
        policy.issue_session()
    assert _TOKEN not in str(raised.value)


@pytest.mark.parametrize("error_type", [ValueError, OverflowError])
def test_clock_conversion_errors_fail_session_issue_safely(
    error_type: type[Exception],
) -> None:
    def broken_clock() -> float:
        raise error_type("secret clock details")

    policy = SecurityPolicy(_server(access_token=_TOKEN), now=broken_clock)
    with pytest.raises(ValueError, match="clock") as raised:
        policy.issue_session()
    assert "secret clock details" not in str(raised.value)
    assert _TOKEN not in str(raised.value)


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("radio.local", True),
        ("RADIO.LOCAL:8080", True),
        ("192.168.1.5", True),
        ("192.168.1.5:65535", True),
        ("[::1]", True),
        ("[0:0:0:0:0:0:0:1]:8080", True),
        ("evil-radio.local", False),
        ("radio.local.evil.example", False),
        ("evil.example@radio.local", False),
        ("radio.local@evil.example", False),
        (" radio.local", False),
        ("radio.local ", False),
        ("radio.local\t", False),
        ("radio.local\\evil", False),
        ("radio%2elocal", False),
        ("radio.local,evil.example", False),
        ("radio.local:8080,evil.example", False),
        ("radio.local:", False),
        ("radio.local:0", False),
        ("radio.local:65536", False),
        ("radio.local:+80", False),
        ("radio.local:abc", False),
        ("::1", False),
        ("radio.local/path", False),
        ("radio.local?query", False),
        ("radio.local#fragment", False),
        ("[v1.fe80]:8080", False),
        ("[::1", False),
        ("::1]", False),
        ("", False),
        (None, False),
        (123, False),
    ],
)
def test_host_policy_requires_valid_exact_hostname(host: object, allowed: bool) -> None:
    assert SecurityPolicy(_server()).host_allowed(host) is allowed  # type: ignore[arg-type]


def test_host_policy_rejects_extremely_long_port_without_raising() -> None:
    assert SecurityPolicy(_server()).host_allowed("radio.local:" + "9" * 5000) is False


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        ("https://radio.local:8080", True),
        ("https://radio.local:8080/", True),
        ("HTTPS://RADIO.LOCAL:8080", True),
        ("http://192.168.1.5:8080", True),
        ("http://[0:0:0:0:0:0:0:1]:8080", True),
        ("http://radio.local:8080", False),
        ("https://radio.local", False),
        ("https://radio.local:8080//", False),
        ("https://radio.local:8080/path", False),
        ("https://radio.local:8080?query", False),
        ("https://radio.local:8080#fragment", False),
        ("https://user@radio.local:8080", False),
        ("https://radio.local:8080@evil.example", False),
        ("https://radio.local:8080.evil.example", False),
        ("https://radio.local:8080,https://evil.example", False),
        ("https://radio%2elocal:8080", False),
        ("https://radio.local\\@evil.example:8080", False),
        ("https://[v1.fe80]:8080", False),
        (" https://radio.local:8080", False),
        ("null", False),
        ("\ud800", False),
        (None, False),
        (123, False),
    ],
)
def test_origin_policy_is_canonical_and_exact(origin: object, allowed: bool) -> None:
    assert SecurityPolicy(_server()).origin_allowed(origin) is allowed  # type: ignore[arg-type]


def test_default_loopback_policy_uses_effective_host_and_origin() -> None:
    policy = SecurityPolicy(ServerConfig(host="127.0.0.2", port=9090))

    assert policy.host_allowed("127.0.0.2:12345")
    assert policy.origin_allowed("http://127.0.0.2:9090")
    assert not policy.host_allowed("127.0.0.1:9090")
    assert not policy.origin_allowed("http://127.0.0.2:8080")


def test_default_lan_policy_uses_effective_host_and_origin() -> None:
    policy = SecurityPolicy(ServerConfig(host="192.168.1.10", port=9090, access_token=_TOKEN))

    assert policy.host_allowed("192.168.1.10")
    assert policy.origin_allowed("http://192.168.1.10:9090/")
    assert not policy.host_allowed("192.168.1.11")
    assert not policy.origin_allowed("http://192.168.1.10:9091")


def test_audit_record_has_only_closed_deterministic_fields() -> None:
    encoded = audit_record(
        "0123456789abcdef0123456789abcdef",
        "upload_liner",
        "success",
        method="POST",
        route="/api/liners/{filename}",
        status=201,
    )

    assert encoded == (
        '{"action":"upload_liner","method":"POST","outcome":"success",'
        '"request_id":"0123456789abcdef0123456789abcdef",'
        '"route":"/api/liners/{filename}","status":201}'
    )
    assert json.loads(encoded) == {
        "action": "upload_liner",
        "method": "POST",
        "outcome": "success",
        "request_id": "0123456789abcdef0123456789abcdef",
        "route": "/api/liners/{filename}",
        "status": 201,
    }


def test_audit_record_omits_optional_fields_and_rejects_extra_data() -> None:
    encoded = audit_record("request", "login", "rejected")
    assert encoded == '{"action":"login","outcome":"rejected","request_id":"request"}'
    assert "token" not in encoded
    assert "query" not in encoded
    assert "body" not in encoded
    assert "path" not in encoded

    with pytest.raises(TypeError):
        audit_record(  # type: ignore[call-arg]
            "request", "login", "rejected", body={"token": _TOKEN}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", 123),
        ("action", None),
        ("outcome", {"secret": "value"}),
        ("method", 1),
        ("route", ["/private/path"]),
        ("status", True),
        ("status", 200.0),
        ("status", float("nan")),
    ],
)
def test_audit_record_rejects_wrong_runtime_field_types(field: str, value: object) -> None:
    arguments: dict[str, object] = {
        "request_id": "request",
        "action": "login",
        "outcome": "success",
        field: value,
    }

    with pytest.raises(TypeError, match=field):
        audit_record(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize("status", [99, 5000])
def test_audit_record_rejects_status_outside_http_and_websocket_ranges(status: int) -> None:
    with pytest.raises(ValueError, match="status"):
        audit_record("request", "login", "success", status=status)


def test_request_ids_are_unique_lowercase_hex() -> None:
    values = {new_request_id() for _ in range(128)}
    assert len(values) == 128
    assert all(re.fullmatch(r"[0-9a-f]{32}", value) for value in values)
