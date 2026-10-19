"""Trusted-proxy identities stay separate from native browser authorization."""

import os
import subprocess
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from httpx2 import Headers
from starlette.responses import JSONResponse
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from autodj.config import ServerConfig, load_config, validate_server_exposure
from autodj.live_routes import install_live_routes
from autodj.pairing import DeviceRegistry
from autodj.secret_files import write_private_file
from autodj.security import COOKIE_NAME, SecurityMiddleware, SecurityPolicy

TOKEN = "a" * 40
IDENTITY = [(b"x-autodj-user-id", b"alice"), (b"x-autodj-user-name", "Álice".encode())]
PROXY_AUTH = (b"x-autodj-proxy-auth", TOKEN.encode())


def scope(headers=(), kind="http"):
    return {"type": kind, "headers": list(headers)}


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
    cfg = ServerConfig(
        auth_mode="trusted_proxy",
        trusted_proxy_secret=TOKEN,
        sso_logout_url=url,
        host="192.168.1.5",
    )
    validate_server_exposure(cfg)
    assert cfg.sso_logout_url == url
    assert ServerConfig().auth_mode == "native"
    with pytest.raises(ValueError):
        validate_server_exposure(ServerConfig(host="192.168.1.5"))


@pytest.mark.parametrize("host", ["127.0.0.1", "192.168.1.5"])
@pytest.mark.parametrize("insecure", [False, True])
def test_proxy_startup_requires_secret_even_on_loopback(host, insecure):
    with pytest.raises(ValueError, match="trusted_proxy_secret"):
        validate_server_exposure(
            ServerConfig(auth_mode="trusted_proxy", host=host, insecure_lan=insecure)
        )


@pytest.mark.parametrize("secret", ["", "a" * 31, "a" * 32 + "\n", "é" * 32])
def test_proxy_secret_must_be_safe_for_a_header(secret):
    with pytest.raises(ValueError, match="trusted_proxy_secret"):
        ServerConfig(trusted_proxy_secret=secret)


def test_proxy_secret_sources_are_mutually_exclusive(tmp_path):
    """ADR-0007-12: an old direct secret cannot shadow a rotated secret file."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        ServerConfig(trusted_proxy_secret=TOKEN, trusted_proxy_secret_file=str(tmp_path / "secret"))


def test_environment_secret_cannot_shadow_a_configured_secret_file(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[server]\nauth_mode = "trusted_proxy"\ntrusted_proxy_secret_file = "secret"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="mutually exclusive"):
        load_config(config, environ={"AUTODJ_TRUSTED_PROXY_SECRET": TOKEN})


def test_proxy_secret_file_rejects_other_readers(tmp_path):
    """ADR-0007-12: shared-readable secret files cannot start trusted-proxy mode."""
    path = tmp_path / "secret"
    write_private_file(path, TOKEN)
    if os.name == "nt":
        subprocess.run(
            ["icacls", str(path), "/grant", "*S-1-1-0:(R)"], check=True, capture_output=True
        )
    else:
        path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only") as invalid:
        validate_server_exposure(
            ServerConfig(auth_mode="trusted_proxy", trusted_proxy_secret_file=str(path))
        )
    assert TOKEN not in str(invalid.value)


@pytest.mark.parametrize("source", ["environment", "file", "environment-file"])
def test_proxy_secret_loads_privately_from_supported_sources(tmp_path, source):
    path = tmp_path / "proxy-secret"
    write_private_file(path, TOKEN + "\n")
    config = tmp_path / "config.toml"
    text = '[server]\nauth_mode = "trusted_proxy"\n'
    environ = {}
    if source == "environment":
        environ["AUTODJ_TRUSTED_PROXY_SECRET"] = TOKEN
    elif source == "file":
        text += f"trusted_proxy_secret_file = '{path.as_posix()}'\n"
    else:
        environ["AUTODJ_TRUSTED_PROXY_SECRET_FILE"] = str(path)
    config.write_text(text, encoding="utf-8")
    cfg = load_config(config, environ=environ)
    validate_server_exposure(cfg.server)
    validate_server_exposure(cfg.server)
    assert SecurityPolicy(cfg.server).sso_identity(scope([*IDENTITY, PROXY_AUTH])) == (
        "alice",
        "Álice",
    )
    if source != "environment":
        assert cfg.server.trusted_proxy_secret is None
    assert TOKEN not in repr(cfg.server)
    assert path.read_text(encoding="utf-8") == TOKEN + "\n"


def test_proxy_secret_file_fails_closed_without_disclosing_invalid_contents(tmp_path):
    path = tmp_path / "proxy-secret"
    with pytest.raises(ValueError, match="trusted_proxy_secret"):
        validate_server_exposure(
            ServerConfig(auth_mode="trusted_proxy", trusted_proxy_secret_file=str(path))
        )
    write_private_file(path, "invalid-secret")
    with pytest.raises(ValueError) as invalid:
        validate_server_exposure(
            ServerConfig(auth_mode="trusted_proxy", trusted_proxy_secret_file=str(path))
        )
    assert "invalid-secret" not in str(invalid.value)


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("X-AutoDJ-Proxy-Auth", "wrong")],
        [("X-AutoDJ-Proxy-Auth", TOKEN), ("x-autodj-proxy-auth", TOKEN)],
        [(b"X-AutoDJ-Proxy-Auth", b"\xff" * 40)],
    ],
)
def test_proxy_secret_is_required_for_http_identity(headers):
    """ADR-0007-10: an asserted identity alone cannot authorize HTTP control."""
    app = FastAPI()
    app.add_api_route("/api/status", lambda: {"ok": True})
    config = ServerConfig(auth_mode="trusted_proxy", allowed_hosts=["testserver"])
    config.trusted_proxy_secret = TOKEN
    app.add_middleware(SecurityMiddleware, policy=SecurityPolicy(config), body_limits={})
    with TestClient(app) as client:
        assert (
            client.get("/api/status", headers=[("X-AutoDJ-User-ID", "alice"), *headers]).status_code
            == 401
        )
        assert (
            client.get(
                "/api/status",
                headers={
                    "X-AutoDJ-User-ID": "alice",
                    "X-AutoDJ-Proxy-Auth": TOKEN,
                },
            ).status_code
            == 200
        )


@pytest.mark.parametrize("kind", ["http", "websocket"])
def test_browser_auth_modes_do_not_accept_each_others_credentials(kind):
    native = SecurityPolicy(ServerConfig(access_token=TOKEN), now=lambda: 1)
    proxy = SecurityPolicy(
        ServerConfig(auth_mode="trusted_proxy", trusted_proxy_secret=TOKEN, access_token=TOKEN),
        now=lambda: 1,
    )
    cookie = (b"cookie", f"{COOKIE_NAME}={native.issue_device_session('a' * 32)}".encode())
    assert native.authentication_required and native.pairing_required
    assert proxy.authentication_required and not proxy.pairing_required
    assert native.browser_authenticated(scope([cookie], kind))
    assert not proxy.browser_authenticated(scope([cookie], kind))
    assert proxy.browser_authenticated(scope([*IDENTITY, PROXY_AUTH], kind))
    assert not native.browser_authenticated(scope(IDENTITY, kind))
    assert native.sso_identity(scope(IDENTITY, kind)) is None
    assert proxy.sso_identity(scope([*IDENTITY, PROXY_AUTH], kind)) == ("alice", "Álice")


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
    proxy = SecurityPolicy(ServerConfig(auth_mode="trusted_proxy", trusted_proxy_secret=TOKEN))
    assert proxy.sso_identity(scope([*headers, PROXY_AUTH])) is None
    assert not proxy.browser_authenticated(scope([*headers, PROXY_AUTH]))


@pytest.mark.parametrize("name_headers", [[], [(b"x-autodj-user-name", b"")]])
def test_optional_proxy_name_falls_back_to_user_id(name_headers):
    proxy = SecurityPolicy(ServerConfig(auth_mode="trusted_proxy", trusted_proxy_secret=TOKEN))
    assert proxy.sso_identity(
        scope([(b"x-autodj-user-id", b"alice"), *name_headers, PROXY_AUTH])
    ) == (
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
            trusted_proxy_secret=TOKEN,
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
        assert (
            client.get(
                "/api/status", headers={"X-AutoDJ-User-ID": "alice", "X-AutoDJ-Proxy-Auth": TOKEN}
            ).status_code
            == 200
        )
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


def test_sso_browser_inventory_evicts_least_recently_seen_per_owner(tmp_path):
    clock = [100]
    registry = DeviceRegistry(tmp_path / "devices.db", now=lambda: clock[0])
    first = registry.register_sso("alice", "First")
    bob = registry.register_sso("bob", "Bob")
    for _ in range(19):
        clock[0] += 1
        registry.register_sso("alice", "Alice")
    clock[0] += 1
    registry.register_sso("alice", "Alice", first.device_id)
    for _ in range(5):
        clock[0] += 1
        newest = registry.register_sso("alice", "Alice")
    browsers = registry.sso_devices("alice")
    assert len(browsers) == 20
    assert first.device_id in {browser.device_id for browser in browsers}
    assert newest.device_id in {browser.device_id for browser in browsers}
    assert registry.sso_devices("bob") == [bob]


@pytest.mark.parametrize(
    "proofs",
    [
        [],
        [("X-AutoDJ-Proxy-Auth", "wrong")],
        [("X-AutoDJ-Proxy-Auth", TOKEN), ("x-autodj-proxy-auth", TOKEN)],
    ],
)
def test_microphone_uses_proxy_identity_once_at_handshake(monkeypatch, proofs):
    policy = SecurityPolicy(
        ServerConfig(
            auth_mode="trusted_proxy",
            trusted_proxy_secret=TOKEN,
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
                "/api/live/mic?rate=48000",
                headers=Headers(
                    [
                        ("Origin", "http://testserver"),
                        ("X-AutoDJ-User-ID", "alice"),
                        *proofs,
                    ]
                ),
            ),
        ):
            pass
        assert denied.value.code == 4401
        authenticated.reset_mock()
        with client.websocket_connect(
            "/api/live/mic?rate=48000",
            headers={
                "Origin": "http://testserver",
                "X-AutoDJ-User-ID": "alice",
                "X-AutoDJ-Proxy-Auth": TOKEN,
            },
        ) as socket:
            socket.send_text("invalid PCM")
            with pytest.raises(WebSocketDisconnect) as invalid:
                socket.receive_bytes()
            assert invalid.value.code == 1003
        assert authenticated.call_count == 1
        station.live.release_mic.assert_called_once_with(mic)


@pytest.mark.parametrize("ttl,age", [(7776000, 900), (60, 60)])
def test_sso_microphone_rejects_frames_after_socket_age_limit(monkeypatch, ttl, age):
    """ADR-0007-11."""
    clock = [100.0]
    config = ServerConfig(
        auth_mode="trusted_proxy",
        session_ttl_seconds=ttl,
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
    )
    config.trusted_proxy_secret = TOKEN
    policy = SecurityPolicy(config)
    policy.monotonic_now = lambda: clock[0]
    mic = Mock()

    def receive_frames(_):
        clock[0] += age

    mic.push_frames.side_effect = receive_frames
    station = SimpleNamespace(
        stream_mode=True,
        stopped=False,
        player=SimpleNamespace(bus=object()),
        live=SimpleNamespace(claim_mic=Mock(return_value=mic), release_mic=Mock()),
    )
    monkeypatch.setattr("autodj.live_routes.resolve_station", lambda _: station)
    app = FastAPI()
    install_live_routes(app, policy, allow_lan_plain_http=False)
    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/api/live/mic?rate=48000",
            headers={
                "Origin": "http://testserver",
                "X-AutoDJ-User-ID": "alice",
                "X-AutoDJ-Proxy-Auth": TOKEN,
            },
        ) as socket,
    ):
        socket.send_bytes(b"\x00\x00" * 480)
        socket.send_text("invalid PCM")
        with pytest.raises(WebSocketDisconnect) as expired:
            socket.receive_bytes()
        assert expired.value.code == 4401
    assert mic.push_frames.call_count == 1
    station.live.release_mic.assert_called_once_with(mic)


def test_native_microphone_checks_session_once_per_frame(monkeypatch):
    policy = SecurityPolicy(
        ServerConfig(
            access_token=TOKEN,
            allowed_hosts=["testserver"],
            allowed_origins=["http://testserver"],
        )
    )
    verify = Mock(wraps=policy.verify_session)
    monkeypatch.setattr(policy, "verify_session", verify)
    processed = Event()
    mic = Mock()
    station = SimpleNamespace(
        stream_mode=True,
        stopped=False,
        player=SimpleNamespace(bus=object()),
        live=SimpleNamespace(claim_mic=Mock(return_value=mic), release_mic=Mock()),
    )

    def received(_):
        if mic.push_frames.call_count == 2:
            station.stopped = True
            processed.set()

    mic.push_frames.side_effect = received
    monkeypatch.setattr("autodj.live_routes.resolve_station", lambda _: station)
    app = FastAPI()
    install_live_routes(app, policy, allow_lan_plain_http=False)
    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/api/live/mic?rate=48000",
            headers={
                "Origin": "http://testserver",
                "Cookie": f"{COOKIE_NAME}={policy.issue_device_session('d' * 32)}",
            },
        ) as socket,
    ):
        socket.send_bytes(b"\x00\x00" * 480)
        socket.send_bytes(b"\x00\x00" * 480)
        assert processed.wait(5), "The microphone did not consume both frames"
        assert verify.call_count == 3  # One handshake and one check for each frame.
