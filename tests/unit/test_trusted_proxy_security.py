"""Trusted-proxy identities stay separate from native browser authorization."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from starlette.responses import JSONResponse
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from autodj.config import ServerConfig, validate_server_exposure
from autodj.live_routes import install_live_routes
from autodj.pairing import DeviceRegistry
from autodj.security import COOKIE_NAME, SecurityMiddleware, SecurityPolicy

TOKEN = "a" * 40
IDENTITY = [(b"x-autodj-user-id", b"alice"), (b"x-autodj-user-name", "Álice".encode())]


def scope(headers=(), kind="http"):
    return {"type": kind, "headers": list(headers)}


@pytest.mark.parametrize("mode", ["sso", "", "Native", None])
def test_invalid_auth_modes_are_rejected(mode):
    with pytest.raises((TypeError, ValueError)):
        ServerConfig(auth_mode=mode)


@pytest.mark.parametrize(
    "url",
    [
        "//evil.example",
        "/\\evil",
        "https://good.example/\\evil",
        "/logout\n",
        "http://good.example",
        "javascript:logout()",
        "relative/path",
        "https://",
        "https://user@host/logout",
    ],
)
def test_logout_url_rejects_unsafe_destinations(url):
    with pytest.raises((TypeError, ValueError)):
        ServerConfig(sso_logout_url=url)


@pytest.mark.parametrize("url", [None, "/logout", "/logout?next=%2F", "https://id.example/logout"])
def test_logout_url_and_explicit_proxy_exposure(url):
    cfg = ServerConfig(auth_mode="trusted_proxy", sso_logout_url=url, host="192.168.1.5")
    validate_server_exposure(cfg)
    assert cfg.sso_logout_url == url
    assert ServerConfig().auth_mode == "native"
    with pytest.raises(ValueError):
        validate_server_exposure(ServerConfig(host="192.168.1.5"))


@pytest.mark.parametrize("kind", ["http", "websocket"])
def test_browser_auth_modes_do_not_accept_each_others_credentials(kind):
    native = SecurityPolicy(ServerConfig(access_token=TOKEN), now=lambda: 1)
    proxy = SecurityPolicy(
        ServerConfig(auth_mode="trusted_proxy", access_token=TOKEN), now=lambda: 1
    )
    cookie = (b"cookie", f"{COOKIE_NAME}={native.issue_device_session('a' * 32)}".encode())
    assert native.authentication_required and native.pairing_required
    assert proxy.authentication_required and not proxy.pairing_required
    assert native.browser_authenticated(scope([cookie], kind))
    assert not proxy.browser_authenticated(scope([cookie], kind))
    assert proxy.browser_authenticated(scope(IDENTITY, kind))
    assert not native.browser_authenticated(scope(IDENTITY, kind))
    assert native.sso_identity(scope(IDENTITY, kind)) is None
    assert proxy.sso_identity(scope(IDENTITY, kind)) == ("alice", "Álice")


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"x-autodj-user-id", b"")],
        [(b"x-autodj-user-id", b"a" * 257)],
        [(b"x-autodj-user-id", b"alice\n")],
        [(b"x-autodj-user-id", b"a\xff")],
        [*IDENTITY, (b"X-AutoDJ-User-ID", b"bob")],
        [*IDENTITY, (b"X-AutoDJ-User-Name", b"Other")],
        [(b"x-autodj-user-id", b"alice"), (b"x-autodj-user-name", b"\xff")],
        [(b"x-autodj-user-id", b"alice"), (b"x-autodj-user-name", b"a" * 257)],
        [(b"x-autodj-user-id", b"alice"), (b"x-autodj-user-name", b"Alice\x00")],
        [(b"remote-user", b"alice")],
    ],
)
def test_proxy_identity_rejects_missing_duplicate_or_malformed_headers(headers):
    proxy = SecurityPolicy(ServerConfig(auth_mode="trusted_proxy"))
    assert proxy.sso_identity(scope(headers)) is None
    assert not proxy.browser_authenticated(scope(headers))


@pytest.mark.parametrize("name_headers", [[], [(b"x-autodj-user-name", b"")]])
def test_optional_proxy_name_falls_back_to_user_id(name_headers):
    proxy = SecurityPolicy(ServerConfig(auth_mode="trusted_proxy"))
    assert proxy.sso_identity(scope([(b"x-autodj-user-id", b"alice"), *name_headers])) == (
        "alice",
        "alice",
    )


def test_proxy_middleware_preserves_public_machine_and_origin_boundaries():
    app = FastAPI()
    for path in ["/api/status", "/healthz", "/api/worker/check", "/live"]:
        app.add_api_route(path, lambda: JSONResponse({"ok": True}), methods=["GET", "POST", "PUT"])
    policy = SecurityPolicy(
        ServerConfig(
            auth_mode="trusted_proxy",
            allowed_hosts=["testserver"],
            allowed_origins=["http://testserver"],
        )
    )
    app.add_middleware(
        SecurityMiddleware,
        policy=policy,
        body_limits={},
        machine_scopes={"/api/worker/": "analysis"},
    )
    with TestClient(app, client=("127.0.0.1", 1234)) as client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/status", headers={"X-AutoDJ-User-ID": "alice"}).status_code == 200
        assert client.get("/healthz").status_code == 200
        assert (
            client.get("/api/worker/check", headers={"X-AutoDJ-User-ID": "alice"}).status_code
            == 401
        )
        assert client.post("/api/status", headers={"X-AutoDJ-User-ID": "alice"}).status_code == 403
        assert client.put("/live").status_code == 200


def test_sso_browser_records_are_owner_scoped_and_cannot_authorize_native_sessions(tmp_path):
    clock = [100]
    registry = DeviceRegistry(tmp_path / "devices.db", now=lambda: clock[0])
    paired = registry.pair("Native browser")
    browser = registry.register_sso("alice", "Á" * 100)
    assert browser.name == "Á" * 64
    assert not registry.is_active(browser.device_id)
    assert registry.sso_devices("bob") == []
    assert registry.rename_sso("bob", browser.device_id, "stolen") is None
    assert not registry.forget_sso("bob", browser.device_id)
    assert registry.rename_sso("alice", browser.device_id, "Studio") == "Studio"
    clock[0] = 200
    reused = registry.register_sso("alice", "Updated name", browser.device_id)
    assert reused.device_id == browser.device_id and reused.name == "Studio"
    assert reused.paired_at == 100 and reused.last_seen_at == 200
    stranger = registry.register_sso("bob", "Bob", browser.device_id)
    assert stranger.device_id != browser.device_id
    assert registry.list_devices() == [paired]
    assert registry.sso_devices("alice") == [reused]
    assert registry.forget_sso("alice", browser.device_id)
    assert registry.sso_devices("alice") == []
    assert registry.is_active(paired.device_id)


def test_sso_browser_names_use_existing_validation(tmp_path):
    registry = DeviceRegistry(tmp_path / "devices.db")
    browser = registry.register_sso("alice", "Alice")
    with pytest.raises(ValueError):
        registry.rename_sso("alice", browser.device_id, "bad\nname")
    assert registry.sso_devices("alice") == [browser]


def test_microphone_uses_proxy_identity_once_at_handshake(monkeypatch):
    policy = SecurityPolicy(
        ServerConfig(
            auth_mode="trusted_proxy",
            allowed_hosts=["testserver"],
            allowed_origins=["http://testserver"],
        )
    )
    mic = Mock()
    station = SimpleNamespace(
        stream_mode=True,
        stopped=False,
        player=SimpleNamespace(bus=object()),
        live=SimpleNamespace(claim_mic=Mock(return_value=mic), release_mic=Mock()),
    )
    monkeypatch.setattr("autodj.live_routes.resolve_station", lambda _: station)
    authenticated = Mock(wraps=policy.browser_authenticated)
    monkeypatch.setattr(policy, "browser_authenticated", authenticated)
    app = FastAPI()
    install_live_routes(app, policy, allow_lan_plain_http=False)
    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as denied,
            client.websocket_connect(
                "/api/live/mic?rate=48000", headers={"Origin": "http://testserver"}
            ),
        ):
            pass
        assert denied.value.code == 4401
        authenticated.reset_mock()
        with client.websocket_connect(
            "/api/live/mic?rate=48000",
            headers={"Origin": "http://testserver", "X-AutoDJ-User-ID": "alice"},
        ) as socket:
            socket.send_text("invalid PCM")
            with pytest.raises(WebSocketDisconnect) as invalid:
                socket.receive_bytes()
            assert invalid.value.code == 1003
        assert authenticated.call_count == 1
        station.live.release_mic.assert_called_once_with(mic)
