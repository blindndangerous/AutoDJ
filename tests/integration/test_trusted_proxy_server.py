"""Provider-neutral SSO browser behavior, alongside native pairing regressions."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from httpx2 import Headers
from starlette.websockets import WebSocketDisconnect

from autodj.server import create_app
from tests._layout import point_index

from .test_stream_server import _stream_mock

PROXY_SECRET = "test-proxy-secret-" + "a" * 32


def _client(bridge, tmp_path, *, logout_url=None):
    cfg = bridge.player._cfg
    cfg.server.auth_mode = "trusted_proxy"
    cfg.server.trusted_proxy_secret = PROXY_SECRET
    cfg.server.sso_logout_url = logout_url
    point_index(cfg.index, tmp_path)
    cfg.index.index_dir = tmp_path
    app = create_app(bridge)
    return TestClient(
        app,
        client=("127.0.0.1", 1234),
        headers={
            "X-AutoDJ-User-ID": "alice",
            "X-AutoDJ-User-Name": "Alice",
            "X-AutoDJ-Proxy-Auth": PROXY_SECRET,
        },
    )


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
    bob = TestClient(
        alice.app,
        headers={
            "X-AutoDJ-User-ID": "bob",
            "X-AutoDJ-User-Name": "Bob",
            "X-AutoDJ-Proxy-Auth": PROXY_SECRET,
        },
    )
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
    bridge.player._cfg.station.output = "stream"
    bridge.player._cfg.player_page.enabled = True
    bridge.attach_stream(stream=_stream_mock(), station=None)
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
    assert client.get("/player").status_code == 200
    assert client.get("/stream.m3u").status_code == 200
    assert client.get("/stream.mp3").status_code == 200
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


def test_sso_app_refuses_startup_without_proxy_secret(bridge, tmp_path):
    """ADR-0007-10: no trusted-proxy app starts without a configured secret."""
    cfg = bridge.player._cfg
    cfg.server.auth_mode = "trusted_proxy"
    point_index(cfg.index, tmp_path)
    with pytest.raises(ValueError, match="trusted_proxy_secret"):
        create_app(bridge)


@pytest.mark.parametrize(
    "host,tls,warnings",
    [("127.0.0.1", False, 0), ("192.168.1.5", False, 1), ("192.168.1.5", True, 0)],
)
def test_proxy_plaintext_network_bind_warns_once(bridge, tmp_path, caplog, host, tls, warnings):
    """ADR-0007-13."""
    bridge.player._cfg.server.host = host
    if tls:
        certificate, key = tmp_path / "certificate.pem", tmp_path / "key.pem"
        certificate.touch()
        key.touch()
        bridge.player._cfg.server.ssl_certfile = str(certificate)
        bridge.player._cfg.server.ssl_keyfile = str(key)
    with caplog.at_level(logging.WARNING, logger="autodj.server"):
        _client(bridge, tmp_path)
    messages = [
        record.getMessage() for record in caplog.records if "proxy secret" in record.getMessage()
    ]
    assert len(messages) == warnings
    assert PROXY_SECRET not in caplog.text


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("X-AutoDJ-Proxy-Auth", "wrong")],
        [("X-AutoDJ-Proxy-Auth", PROXY_SECRET), ("x-autodj-proxy-auth", PROXY_SECRET)],
    ],
)
def test_sso_websocket_rejects_identity_without_single_proxy_secret(bridge, tmp_path, headers):
    """ADR-0007-10: spoofed headers cannot open default or named control sockets."""
    client = _client(bridge, tmp_path)
    del client.headers["X-AutoDJ-Proxy-Auth"]
    for path in ("/ws", "/api/stations/default/ws"):
        with (
            pytest.raises(WebSocketDisconnect) as denied,
            client.websocket_connect(path, headers=Headers(headers)),
        ):
            pass
        assert denied.value.code == 4401


@pytest.mark.parametrize("ttl,age", [(7776000, 900), (60, 60)])
def test_sso_control_socket_expires_and_requires_a_new_proxy_handshake(bridge, tmp_path, ttl, age):
    """ADR-0007-11."""
    clock = [100.0]
    bridge.player._cfg.server.session_ttl_seconds = ttl
    client = _client(bridge, tmp_path)
    client.app.state.security_policy.monotonic_now = lambda: clock[0]
    with client, client.websocket_connect("/ws") as socket:
        socket.receive_json()
        clock[0] += age
        with pytest.raises(WebSocketDisconnect) as expired:
            while True:
                socket.receive_text()
        assert expired.value.code == 4401
