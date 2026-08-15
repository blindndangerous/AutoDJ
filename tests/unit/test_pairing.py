from __future__ import annotations

import hashlib
import hmac
import math
import secrets

import pytest

from autodj.config import ServerConfig
from autodj.pairing import DeviceRegistry
from autodj.security import SecurityPolicy

_SECRET = "pairing-secret-that-is-at-least-32-bytes"


def _policy(registry: DeviceRegistry, *, now: float = 1_000.0) -> SecurityPolicy:
    return SecurityPolicy(
        ServerConfig(access_token=_SECRET, session_ttl_seconds=300),
        now=lambda: now,
        device_is_active=registry.is_active,
    )


def test_pairing_code_rotates_with_five_minute_boundary_grace(tmp_path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.sqlite3", now=lambda: 1_000)
    current = _policy(registry, now=1_000)
    later = _policy(registry, now=1_301)
    expired = _policy(registry, now=1_601)

    code = current.current_pairing_code()

    assert len(code) == 8
    assert code.isdecimal()
    assert current.verify_pairing_code(code)
    assert later.verify_pairing_code(code)
    assert not expired.verify_pairing_code(code)


def test_pairing_code_comparison_is_constant_time(tmp_path, monkeypatch) -> None:
    registry = DeviceRegistry(tmp_path / "devices.sqlite3")
    policy = _policy(registry)
    expected = policy.current_pairing_code().encode("ascii")
    seen: list[tuple[bytes, bytes]] = []

    def compare(left: bytes, right: bytes) -> bool:
        seen.append((left, right))
        return left == right

    monkeypatch.setattr(secrets, "compare_digest", compare)

    assert policy.verify_pairing_code(expected.decode("ascii"))
    assert seen[0] == (expected, expected)
    assert len(seen) == 2


def test_registry_persists_distinct_devices_and_revocation(tmp_path) -> None:
    path = tmp_path / "devices.sqlite3"
    registry = DeviceRegistry(path, now=lambda: 1_000)

    kitchen = registry.pair("Kitchen tablet")
    phone = registry.pair("Mike's phone")
    reopened = DeviceRegistry(path, now=lambda: 2_000)

    assert kitchen.device_id != phone.device_id
    assert [device.name for device in reopened.list_devices()] == [
        "Kitchen tablet",
        "Mike's phone",
    ]
    assert reopened.is_active(kitchen.device_id)
    assert reopened.revoke(kitchen.device_id)
    assert not reopened.is_active(kitchen.device_id)
    assert reopened.is_active(phone.device_id)


def test_registry_touch_updates_active_device_and_rejects_unknown_ids(tmp_path) -> None:
    now = [1_000.0]
    registry = DeviceRegistry(tmp_path / "devices.sqlite3", now=lambda: now[0])
    device = registry.pair("Kitchen tablet")
    now[0] = 2_000.0

    assert registry.touch(device.device_id)
    assert registry.list_devices()[0].last_seen_at == 2_000
    assert not registry.touch("f" * 32)
    assert not registry.is_active("invalid")
    assert not registry.revoke("invalid")


def test_registry_reset_revokes_only_active_devices(tmp_path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.sqlite3", now=lambda: 1_000)
    first = registry.pair("First browser")
    registry.pair("Second browser")
    assert registry.revoke(first.device_id)

    assert registry.reset() == 1
    assert registry.reset() == 0
    assert all(device.revoked_at == 1_000 for device in registry.list_devices())


@pytest.mark.parametrize("clock", [math.nan, math.inf, -1, "invalid"])
def test_registry_rejects_invalid_clock_values(tmp_path, clock: object) -> None:
    registry = DeviceRegistry(tmp_path / "devices.sqlite3", now=lambda: clock)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="clock"):
        registry.pair("Kitchen tablet")


def test_device_session_identifies_device_and_honors_revocation(tmp_path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.sqlite3", now=lambda: 1_000)
    device = registry.pair("Living room")
    policy = _policy(registry)

    cookie = policy.issue_device_session(device.device_id)

    assert policy.session_device_id(cookie) == device.device_id
    assert policy.verify_session(cookie)
    assert registry.revoke(device.device_id)
    assert policy.session_device_id(cookie) is None
    assert not policy.verify_session(cookie)


def test_legacy_unbound_session_is_rejected(tmp_path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.sqlite3", now=lambda: 1_000)
    policy = _policy(registry)
    payload = "1300." + "a" * 32
    signature = hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()

    assert not policy.verify_session(f"{payload}.{signature}")


def test_registry_rejects_invalid_device_names(tmp_path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.sqlite3")

    for name in (None, "", "   ", "x" * 65, "control\nname", "hidden\u202ename"):
        try:
            registry.pair(name)
        except ValueError as exc:
            assert "device name" in str(exc)
        else:
            raise AssertionError(f"invalid device name accepted: {name!r}")


def test_pairing_policy_rejects_missing_token_clock_and_device(tmp_path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.sqlite3", now=lambda: 1_000)
    no_token = SecurityPolicy(ServerConfig(), device_is_active=registry.is_active)
    with pytest.raises(RuntimeError, match="access token"):
        no_token.current_pairing_code()
    assert not no_token.verify_pairing_code("12345678")

    bad_clock = SecurityPolicy(
        ServerConfig(access_token=_SECRET),
        now=lambda: math.nan,
        device_is_active=registry.is_active,
    )
    assert not bad_clock.verify_pairing_code("12345678")
    with pytest.raises(ValueError, match="device ID"):
        _policy(registry).issue_device_session("invalid")
    with pytest.raises(ValueError, match="not active"):
        _policy(registry).issue_device_session("f" * 32)
