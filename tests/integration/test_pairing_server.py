from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from autodj.pairing import DeviceRegistry
from autodj.security import PairingRateLimiter
from autodj.server import create_app

_SECRET = "pairing-secret-that-is-at-least-32-bytes"


def _paired_client(bridge, tmp_path) -> tuple[TestClient, DeviceRegistry]:
    bridge.player._cfg.server.access_token = _SECRET
    bridge.player._cfg.index.active_dir = tmp_path
    registry = DeviceRegistry(tmp_path / ".paired-devices.sqlite3", now=lambda: 1_000)
    app = create_app(bridge, device_registry=registry)
    app.state.security_policy.now = lambda: 1_000
    return TestClient(app), registry


def test_browser_pairs_once_and_reuses_device_session(bridge, tmp_path) -> None:
    client, registry = _paired_client(bridge, tmp_path)
    code = client.app.state.security_policy.current_pairing_code()

    response = client.post(
        "/api/pair",
        json={"code": code, "device_name": "Kitchen tablet"},
    )

    assert response.status_code == 200
    device_id = response.json()["device_id"]
    assert registry.is_active(device_id)
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/auth/status").json() == {
        "required": True,
        "authenticated": True,
        "pairing": True,
        "device_id": device_id,
    }


def test_invalid_pairing_code_cannot_create_device(bridge, tmp_path) -> None:
    client, registry = _paired_client(bridge, tmp_path)

    response = client.post(
        "/api/pair",
        json={"code": "00000000", "device_name": "Unknown browser"},
    )

    assert response.status_code == 401
    assert registry.list_devices() == []


def test_revoked_device_loses_api_access_without_server_restart(bridge, tmp_path) -> None:
    client, registry = _paired_client(bridge, tmp_path)
    code = client.app.state.security_policy.current_pairing_code()
    paired = client.post(
        "/api/pair",
        json={"code": code, "device_name": "Old phone"},
    ).json()

    assert registry.revoke(paired["device_id"])
    assert client.get("/api/status").status_code == 401
    status = client.get("/api/auth/status").json()
    assert status["authenticated"] is False
    assert status["device_id"] is None


def test_pairing_attempts_share_bounded_authentication_limiter(bridge, tmp_path) -> None:
    bridge.player._cfg.server.access_token = _SECRET
    bridge.player._cfg.index.active_dir = tmp_path
    registry = DeviceRegistry(tmp_path / ".paired-devices.sqlite3", now=lambda: 1_000)
    app = create_app(
        bridge,
        device_registry=registry,
        pairing_rate_limiter=PairingRateLimiter(per_client_limit=1, global_limit=10),
    )
    app.state.security_policy.now = lambda: 1_000
    client = TestClient(app)

    assert (
        client.post(
            "/api/pair",
            json={"code": "00000000", "device_name": "Unknown browser"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/pair",
            json={
                "code": app.state.security_policy.current_pairing_code(),
                "device_name": "Kitchen tablet",
            },
        ).status_code
        == 429
    )
    assert registry.list_devices() == []


def test_pairing_rejects_oversized_body_before_json_parsing(bridge, tmp_path) -> None:
    client, registry = _paired_client(bridge, tmp_path)

    response = client.post(
        "/api/pair",
        content=b"x" * 5_000,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert registry.list_devices() == []


def test_pairing_rejects_untrusted_browser_origin(bridge, tmp_path) -> None:
    client, registry = _paired_client(bridge, tmp_path)

    response = client.post(
        "/api/pair",
        json={
            "code": client.app.state.security_policy.current_pairing_code(),
            "device_name": "Unknown browser",
        },
        headers={"Origin": "http://evil.example"},
    )

    assert response.status_code == 403
    assert registry.list_devices() == []


def test_pairing_is_disabled_for_anonymous_loopback_server(bridge) -> None:
    client = TestClient(create_app(bridge))

    response = client.post(
        "/api/pair",
        json={"code": "12345678", "device_name": "Unused browser"},
    )

    assert response.status_code == 409


def test_mock_secret_does_not_create_registry_paths(bridge, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    bridge.player._cfg.server.access_token = MagicMock()
    bridge.player._cfg.index.index_dir = MagicMock()

    app = create_app(bridge)

    assert app.state.device_registry is None
    assert list(tmp_path.iterdir()) == []


def test_pairing_rejects_unsafe_device_name_without_creating_identity(bridge, tmp_path) -> None:
    client, registry = _paired_client(bridge, tmp_path)

    response = client.post(
        "/api/pair",
        json={
            "code": client.app.state.security_policy.current_pairing_code(),
            "device_name": "hidden\u202ename",
        },
    )

    assert response.status_code == 422
    assert registry.list_devices() == []


def test_legacy_login_endpoint_is_removed(bridge, tmp_path) -> None:
    client, registry = _paired_client(bridge, tmp_path)
    code = client.app.state.security_policy.current_pairing_code()
    paired = client.post(
        "/api/pair",
        json={"code": code, "device_name": "Current browser"},
    )

    response = client.post("/api/login", json={"token": _SECRET})

    assert paired.status_code == 200
    assert response.status_code == 404
    assert [device.name for device in registry.list_devices()] == ["Current browser"]


def test_pairing_and_auth_status_keep_sqlite_off_the_event_loop(
    bridge, tmp_path, monkeypatch
) -> None:
    import asyncio

    client, _registry = _paired_client(bridge, tmp_path)
    offloaded: list[str] = []
    real_to_thread = asyncio.to_thread

    async def recording_to_thread(function, *args, **kwargs):
        offloaded.append(getattr(function, "__name__", repr(function)))
        return await real_to_thread(function, *args, **kwargs)

    monkeypatch.setattr("autodj.server.asyncio.to_thread", recording_to_thread)
    code = client.app.state.security_policy.current_pairing_code()

    assert client.post("/api/pair", json={"code": code, "device_name": "Den"}).status_code == 200
    assert client.get("/api/auth/status").json()["authenticated"] is True
    # registry.pair, the active-device check behind issuing the session, and
    # the touch + verify pair in /api/auth/status all hit SQLite.
    assert {"pair", "issue_device_session", "_status"} <= set(offloaded)


def test_locked_out_client_gets_429_with_retry_after_even_for_the_right_code(
    bridge, tmp_path
) -> None:
    from autodj.security import PAIRING_MAX_FAILURES_PER_CLIENT

    client, registry = _paired_client(bridge, tmp_path)
    # Only the per-client lockout is under test, not the request-rate limiter.
    client.app.state.pairing_rate_limiter = PairingRateLimiter(per_client_limit=1_000)
    policy = client.app.state.security_policy
    code = policy.current_pairing_code()
    wrong = "00000000" if code != "00000000" else "11111111"

    for _ in range(PAIRING_MAX_FAILURES_PER_CLIENT):
        assert client.post("/api/pair", json={"code": wrong, "device_name": "x"}).status_code == 401
    response = client.post("/api/pair", json={"code": code, "device_name": "Real"})

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "200"
    assert "Try again in 200 seconds" in response.json()["detail"]
    assert registry.list_devices() == []


def test_paused_pairing_tells_the_user_to_wait_for_a_new_code(bridge, tmp_path) -> None:
    from autodj.security import PAIRING_MAX_FAILURES_PER_WINDOW

    client, _registry = _paired_client(bridge, tmp_path)
    policy = client.app.state.security_policy
    for attempt in range(PAIRING_MAX_FAILURES_PER_WINDOW):
        policy.verify_pairing_code("00000000", f"10.9.0.{attempt}")

    response = client.post(
        "/api/pair",
        json={"code": policy.current_pairing_code(), "device_name": "Real"},
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "200"
    assert response.json()["detail"] == (
        "Pairing is paused after too many wrong codes. Try again in 200 seconds with a new code."
    )
