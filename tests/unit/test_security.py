from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import replace
from types import SimpleNamespace

import pytest

from autodj.config import ServerConfig
from autodj.security import (
    COOKIE_NAME,
    PairingRateLimiter,
    SecurityMiddleware,
    SecurityPolicy,
    audit_record,
    new_request_id,
)

_TOKEN = "test-access-token-that-is-32-bytes-long"
_ROTATED_TOKEN = "rotated-access-token-that-is-32-bytes"
_DEVICE_ID = "d" * 32


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
    device_id: str = _DEVICE_ID,
    nonce: str = "a" * 32,
    token: str = _TOKEN,
) -> str:
    payload = f"{expires}.{device_id}.{nonce}"
    signature = hmac.new(token.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def test_cookie_name_is_stable_for_server_integration() -> None:
    assert COOKIE_NAME == "autodj_session"


def test_authentication_required_tracks_configured_token() -> None:
    assert SecurityPolicy(_server()).authentication_required is False
    assert SecurityPolicy(_server(access_token=_TOKEN)).authentication_required is True


def test_pairing_code_comparison_uses_constant_time_bytes(monkeypatch) -> None:
    seen: list[tuple[bytes, bytes]] = []
    policy = SecurityPolicy(_server(access_token=_TOKEN), now=lambda: 1_000)
    expected = policy.current_pairing_code().encode("ascii")

    def compare(left: bytes, right: bytes) -> bool:
        seen.append((left, right))
        return True

    monkeypatch.setattr(secrets, "compare_digest", compare)

    assert policy.verify_pairing_code("12345678")
    assert seen[0] == (b"12345678", expected)
    assert len(seen) == 2


def test_pairing_code_rejects_missing_configuration_and_hostile_candidates() -> None:
    assert SecurityPolicy(_server()).verify_pairing_code("12345678") is False
    policy = SecurityPolicy(_server(access_token=_TOKEN))
    assert policy.verify_pairing_code("1234\N{LOCK}") is False
    assert policy.verify_pairing_code("1234\ud800") is False
    assert policy.verify_pairing_code(None) is False  # type: ignore[arg-type]


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

    cookie = policy.issue_device_session(_DEVICE_ID)

    assert cookie == _signed_cookie("1060")
    assert policy.verify_session(cookie) is True
    assert nonces == [16]


def test_issue_device_session_requires_configured_token() -> None:
    with pytest.raises(RuntimeError, match="access token is not configured"):
        SecurityPolicy(_server()).issue_device_session(_DEVICE_ID)


def test_session_expiry_boundary_is_inclusive() -> None:
    cookie = SecurityPolicy(
        _server(access_token=_TOKEN, session_ttl_seconds=60), now=lambda: 1000
    ).issue_device_session(_DEVICE_ID)

    assert SecurityPolicy(_server(access_token=_TOKEN), now=lambda: 1060).verify_session(cookie)
    assert not SecurityPolicy(_server(access_token=_TOKEN), now=lambda: 1061).verify_session(cookie)


@pytest.mark.parametrize("component", [0, 1, 2, 3])
def test_session_rejects_tampering_of_each_component(component: int) -> None:
    policy = SecurityPolicy(_server(access_token=_TOKEN), now=lambda: 1000)
    parts = policy.issue_device_session(_DEVICE_ID).split(".")
    parts[component] = ("b" if parts[component][0] != "b" else "c") + parts[component][1:]

    assert policy.verify_session(".".join(parts)) is False


def test_session_signature_comparison_uses_constant_time_bytes(monkeypatch) -> None:
    policy = SecurityPolicy(_server(access_token=_TOKEN), now=lambda: 1000)
    cookie = policy.issue_device_session(_DEVICE_ID)
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
    cookie = policy.issue_device_session(_DEVICE_ID)
    signature = cookie.rsplit(".", 1)[1].encode("ascii")
    seen: list[tuple[bytes, bytes]] = []

    def compare(left: bytes, right: bytes) -> bool:
        seen.append((left, right))
        return False

    monkeypatch.setattr(secrets, "compare_digest", compare)

    assert policy.verify_session(cookie) is False
    assert seen == [(signature, signature)]


def test_session_token_rotation_invalidates_existing_cookie() -> None:
    cookie = SecurityPolicy(_server(access_token=_TOKEN), now=lambda: 1000).issue_device_session(
        _DEVICE_ID
    )

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
        "9223372036854775808." + _DEVICE_ID + "." + "a" * 32 + "." + "b" * 64,
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
    cookie = SecurityPolicy(_server(access_token=_TOKEN), now=lambda: 1000).issue_device_session(
        _DEVICE_ID
    )
    policy = SecurityPolicy(_server(access_token=_TOKEN), now=lambda: bad_now)  # type: ignore[arg-type]

    assert policy.verify_session(cookie) is False


@pytest.mark.parametrize("error_type", [ValueError, OverflowError])
def test_clock_conversion_errors_reject_session_without_raising(
    error_type: type[Exception],
) -> None:
    cookie = SecurityPolicy(_server(access_token=_TOKEN), now=lambda: 1000).issue_device_session(
        _DEVICE_ID
    )

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
        policy.issue_device_session(_DEVICE_ID)
    assert _TOKEN not in str(raised.value)


@pytest.mark.parametrize("error_type", [ValueError, OverflowError])
def test_clock_conversion_errors_fail_session_issue_safely(
    error_type: type[Exception],
) -> None:
    def broken_clock() -> float:
        raise error_type("secret clock details")

    policy = SecurityPolicy(_server(access_token=_TOKEN), now=broken_clock)
    with pytest.raises(ValueError, match="clock") as raised:
        policy.issue_device_session(_DEVICE_ID)
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
    encoded = audit_record("request", "pair", "rejected")
    assert encoded == '{"action":"pair","outcome":"rejected","request_id":"request"}'
    assert "token" not in encoded
    assert "query" not in encoded
    assert "body" not in encoded
    assert "path" not in encoded

    with pytest.raises(TypeError):
        audit_record(  # type: ignore[call-arg]
            "request", "pair", "rejected", body={"code": "12345678"}
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
        "action": "pair",
        "outcome": "success",
        field: value,
    }

    with pytest.raises(TypeError, match=field):
        audit_record(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize("status", [99, 5000])
def test_audit_record_rejects_status_outside_http_and_websocket_ranges(status: int) -> None:
    with pytest.raises(ValueError, match="status"):
        audit_record("request", "pair", "success", status=status)


def test_request_ids_are_unique_lowercase_hex() -> None:
    values = {new_request_id() for _ in range(128)}
    assert len(values) == 128
    assert all(re.fullmatch(r"[0-9a-f]{32}", value) for value in values)


@pytest.mark.parametrize(
    "setting",
    [
        {"per_client_limit": 0},
        {"global_limit": 0},
        {"window_seconds": 0},
        {"max_clients": 0},
    ],
)
def test_pairing_limiter_requires_positive_settings(setting) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        PairingRateLimiter(**setting)


def test_pairing_limiter_rejects_nonfinite_clock() -> None:
    with pytest.raises(ValueError, match="clock returned an invalid value"):
        PairingRateLimiter(now=lambda: float("nan"))


def test_pairing_limiter_discards_expired_client_window() -> None:
    clock = [0.0]
    limiter = PairingRateLimiter(now=lambda: clock[0], per_client_limit=1, window_seconds=10)
    assert limiter.reserve("peer").allowed is True
    assert limiter.reserve("peer").allowed is False

    limiter._global_started = 5.0
    clock[0] = 10.0

    assert limiter.reserve("peer").allowed is True


def test_issue_device_session_rejects_expiry_overflow() -> None:
    policy = SecurityPolicy(
        _server(access_token=_TOKEN, session_ttl_seconds=60), now=lambda: 2**63 - 1
    )

    with pytest.raises(RuntimeError, match="expiry is outside"):
        policy.issue_device_session(_DEVICE_ID)


def test_negative_clock_timestamp_is_rejected() -> None:
    policy = SecurityPolicy(_server(access_token=_TOKEN), now=lambda: -1)

    with pytest.raises(ValueError, match="invalid timestamp"):
        policy.issue_device_session(_DEVICE_ID)


def test_secure_unspecified_host_has_no_implicit_origin() -> None:
    policy = SecurityPolicy(_server(host="0.0.0.0", allowed_origins=None), secure_cookie=True)

    assert policy.effective_allowed_origins() == []


@pytest.mark.parametrize("host", [".", "-invalid.example"])
def test_host_policy_rejects_empty_or_invalid_dns_name(host: str) -> None:
    assert SecurityPolicy(_server()).host_allowed(host) is False


def test_security_middleware_unknown_peer_and_malformed_length() -> None:
    assert SecurityMiddleware._peer({"type": "http"}) == "<unknown>"
    assert SecurityMiddleware._declared_pairing_body_too_large(
        {"type": "http", "headers": [(b"content-length", b"invalid")]}
    )


@pytest.mark.asyncio
async def test_pairing_body_buffer_rejects_nonbytes_chunk() -> None:
    async def receive():
        return {"type": "http.request", "body": "not bytes"}

    with pytest.raises(ValueError, match="invalid ASGI request body"):
        await SecurityMiddleware._buffer_pairing_body(receive)


def _pairing_scope() -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": "/api/pair",
        "headers": [
            (b"host", b"radio.local"),
            (b"origin", b"https://radio.local:8080"),
        ],
        "app": SimpleNamespace(routes=[]),
    }


@pytest.mark.asyncio
async def test_pairing_body_replay_delegates_after_buffered_message() -> None:
    received = []

    async def app(_scope, receive, _send):
        received.append(await receive())
        received.append(await receive())

    messages = iter(
        [
            {"type": "http.request", "body": b"pairing", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(messages)

    async def send(_message):
        return None

    middleware = SecurityMiddleware(app, SecurityPolicy(_server()))
    await middleware(_pairing_scope(), receive, send)

    assert received == [
        {"type": "http.request", "body": b"pairing", "more_body": False},
        {"type": "http.disconnect"},
    ]


@pytest.mark.asyncio
async def test_pairing_disconnect_is_replayed_to_downstream() -> None:
    received = []

    async def app(_scope, receive, _send):
        received.append(await receive())

    async def receive():
        return {"type": "http.disconnect"}

    async def send(_message):
        return None

    middleware = SecurityMiddleware(app, SecurityPolicy(_server()))
    await middleware(_pairing_scope(), receive, send)

    assert received == [{"type": "http.disconnect"}]
