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
