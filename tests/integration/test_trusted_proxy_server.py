"""Provider-neutral SSO browser behavior, alongside native pairing regressions."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from autodj.server import create_app
from tests._layout import point_index


def _client(bridge, tmp_path, *, logout_url=None):
    cfg = bridge.player._cfg
    cfg.server.auth_mode = "trusted_proxy"
    cfg.server.sso_logout_url = logout_url
    point_index(cfg.index, tmp_path)
    cfg.index.index_dir = tmp_path
    app = create_app(bridge)
    return TestClient(app, headers={"X-AutoDJ-User-ID": "alice", "X-AutoDJ-User-Name": "Alice"})


def test_sso_registers_browser_without_pairing(bridge, tmp_path):
    """ADR-0007-06."""
    client = _client(bridge, tmp_path)
    response = client.get("/api/auth/status")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "trusted_proxy"
    assert body["user"] == {"id": "alice", "name": "Alice"}
    assert body["authenticated"] is True and body["pairing"] is False
    assert body["device_name"] == "Alice"
    assert body["device_id"] == client.get("/api/auth/status").json()["device_id"]
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()
    assert client.get("/api/status").status_code == 200
    assert client.app.state.device_registry.list_devices() == []
    assert (
        client.post("/api/pair", json={"code": "12345678", "device_name": "No"}).status_code == 409
    )
    assert client.get("/api/pairing-code").status_code == 409


def test_sso_browser_management_is_scoped_to_owner(bridge, tmp_path):
    """ADR-0007-07."""
    alice = _client(bridge, tmp_path)
    device_id = alice.get("/api/auth/status").json()["device_id"]
    assert (
        alice.patch(f"/api/devices/{device_id}", json={"name": "Studio laptop"}).status_code == 200
    )
    assert alice.get("/api/auth/status").json()["device_name"] == "Studio laptop"
    bob = TestClient(alice.app, headers={"X-AutoDJ-User-ID": "bob", "X-AutoDJ-User-Name": "Bob"})
    bob_id = bob.get("/api/auth/status").json()["device_id"]
    assert {d["device_id"] for d in bob.get("/api/devices").json()["devices"]} == {bob_id}
    assert bob.patch(f"/api/devices/{device_id}", json={"name": "Stolen"}).status_code == 404
    assert bob.delete(f"/api/devices/{device_id}").status_code == 404
    response = alice.delete(f"/api/devices/{device_id}")
    assert response.status_code == 200 and response.json()["signed_out"] is False
    assert alice.get("/api/status").status_code == 200
    assert alice.get("/api/auth/status").json()["device_id"] != device_id


def test_sso_account_change_does_not_reuse_another_users_browser(bridge, tmp_path):
    client = _client(bridge, tmp_path)
    alice_id = client.get("/api/auth/status").json()["device_id"]
    client.headers["X-AutoDJ-User-ID"] = "bob"
    client.headers["X-AutoDJ-User-Name"] = "Bob"
    bob_id = client.get("/api/auth/status").json()["device_id"]
    assert bob_id != alice_id
    client.headers["X-AutoDJ-User-ID"] = "alice"
    client.headers["X-AutoDJ-User-Name"] = "Alice"
    assert client.get("/api/auth/status").json()["device_id"] not in {alice_id, bob_id}


def test_sso_rejects_missing_identity_and_preserves_public_metadata(bridge, tmp_path):
    """ADR-0007-08."""
    client = _client(bridge, tmp_path)
    client.get("/api/auth/status")
    del client.headers["X-AutoDJ-User-ID"]
    assert client.get("/api/status").status_code == 401
    response = client.get("/api/auth/status")
    assert response.status_code == 401
    assert response.json()["mode"] == "trusted_proxy"
    assert response.json()["pairing"] is False
    assert client.get("/api/nowplaying").status_code == 200
    assert client.get("/api/version").status_code == 200
    assert client.get("/api/stations/default/status").status_code == 401


def test_sso_websocket_needs_identity_but_no_pairing(bridge, tmp_path):
    """ADR-0007-09."""
    client = _client(bridge, tmp_path)
    for path in ("/ws", "/api/stations/default/ws"):
        with client.websocket_connect(path) as socket:
            socket.send_json({"type": "ignored-client-frame"})
    del client.headers["X-AutoDJ-User-ID"]
    with pytest.raises(WebSocketDisconnect) as caught, client.websocket_connect("/ws"):
        pass
    assert caught.value.code == 4401


def test_sso_logout_is_operator_configured_and_cookie_is_not_authority(bridge, tmp_path):
    client = _client(bridge, tmp_path, logout_url="/sso/logout")
    assert client.get("/api/auth/status").json()["logout_url"] == "/sso/logout"
    assert client.post("/api/logout").json() == {
        "authenticated": False,
        "logout_url": "/sso/logout",
    }
    assert client.get("/api/status").status_code == 200
    client.app.state.security_policy.config.sso_logout_url = None
    assert client.post("/api/logout").status_code == 409


def test_sso_still_checks_browser_origin(bridge, tmp_path):
    client = _client(bridge, tmp_path)
    assert client.post("/api/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    with (
        pytest.raises(WebSocketDisconnect) as caught,
        client.websocket_connect("/ws", headers={"Origin": "https://evil.example"}),
    ):
        pass
    assert caught.value.code == 4403
