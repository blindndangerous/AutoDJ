"""Stream routes, security and the bridge's stream-mode wiring."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from autodj._bridge import PlayerBridge, StreamSeekUnavailable
from autodj.listener_access import proxy_allowlist
from autodj.server import create_app
from autodj.stream import EncoderUnavailableError, ListenerLimitError

from ._helpers import _make_entry, _make_player_mock, _make_sim_mock


def _stream_mock() -> MagicMock:
    stream = MagicMock()
    stream.listener_count = 0
    listener = MagicMock()
    listener.closed = False
    listener.abort.side_effect = lambda: setattr(listener, "closed", True)

    async def chunks():
        yield b"\xff\xfb\x90\x00" + b"\x00" * 413

    listener.chunks = chunks
    stream.add_listener.return_value = listener
    stream.remove_listener.side_effect = lambda target: setattr(target, "closed", True)
    return stream


@pytest.fixture
def stream_app(bridge, tmp_path: Path):
    stream = _stream_mock()
    bridge.player._cfg.index.active_dir = tmp_path
    bridge.player._cfg.stream.enabled = True
    bridge.attach_stream(stream=stream, station=MagicMock(state="idle"))
    app = create_app(bridge)
    return TestClient(app, client=("192.0.2.4", 50000)), stream


# -- the stream routes -------------------------------------------------------


_OLD_KEYED_PATHS = (
    "/stream/" + "x" * 43 + ".mp3",
    "/stream/" + "x" * 43 + ".m3u",
    "/stream/.mp3",
    "/stream/anything.ogg",
)


def test_old_keyed_links_are_not_found(stream_app) -> None:
    """The removed ``/stream/<secret>.mp3`` and ``.m3u`` links are 404 for any key."""
    client, stream = stream_app
    for path in _OLD_KEYED_PATHS:
        assert client.get(path).status_code == 404, path
        assert client.head(path).status_code == 404, path
    stream.add_listener.assert_not_called()


def test_only_mp3_and_m3u_are_served_under_stream_dot(stream_app) -> None:
    client, stream = stream_app
    for path in ("/stream.ogg", "/stream.aac", "/stream.pls"):
        assert client.get(path).status_code == 404, path
        assert client.head(path).status_code == 404, path
    stream.add_listener.assert_not_called()


def test_rotate_route_is_gone(stream_app) -> None:
    client, stream = stream_app
    assert client.post("/api/stream/rotate").status_code in (404, 405)
    stream.disconnect_all.assert_not_called()


@pytest.mark.parametrize("query", ["", "?connection=new-listener"])
def test_stream_mp3_streams(stream_app, query) -> None:
    client, stream = stream_app
    with client.stream("GET", f"/stream.mp3{query}") as response:
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.headers["cache-control"] == "no-store"
        assert "icy-metaint" not in response.headers
        assert next(response.iter_bytes())[:2] == b"\xff\xfb"
    stream.add_listener.assert_called_once_with(icy=False)
    stream.remove_listener.assert_called_with(stream.add_listener.return_value)


def _authenticated_stream_client(bridge, tmp_path: Path) -> TestClient:
    """A stream-mode app with pairing on, and a client that has not paired."""
    from autodj.config import ServerConfig

    bridge.player._cfg.server = ServerConfig(
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
        access_token="t" * 32,
    )
    bridge.player._cfg.index.active_dir = tmp_path
    bridge.player._cfg.index.index_dir = tmp_path
    bridge.attach_stream(stream=_stream_mock(), station=MagicMock(state="idle"))
    return TestClient(create_app(bridge), client=("192.0.2.4", 50000))


def test_stream_needs_no_session_when_authentication_is_on(bridge, tmp_path: Path) -> None:
    """Public stream URLs work without pairing; stream controls still need login."""
    client = _authenticated_stream_client(bridge, tmp_path)
    assert client.get("/api/stream").status_code == 401
    assert client.post("/api/stream/settings", json={"bitrate": 192}).status_code == 401
    for path in ("/api/stream/listeners", "/api/stream/bans"):
        assert client.get(path).status_code == 401
    assert client.post("/api/stream/bans", json={"address": "192.0.2.4"}).status_code == 401
    assert client.get("/stream.m3u").status_code == 200
    assert client.head("/stream.m3u").status_code == 200


def test_web_relay_save_persists_privately_and_never_returns_password(stream_app, bridge) -> None:
    client, _ = stream_app
    manager = MagicMock()
    manager.status.return_value = []
    bridge.relays = manager
    response = client.post(
        "/api/stream/relays",
        json={
            "name": "web",
            "type": "icecast",
            "host": "192.168.1.20",
            "port": 8000,
            "mount": "/live",
            "password": "source-secret",
            "enabled": True,
            "public": False,
            "bitrate": None,
        },
    )
    assert response.status_code == 200
    assert "source-secret" not in response.text
    assert client.get("/api/stream/relays").json()["relays"][0]["password_configured"] is True
    saved = bridge.player._cfg.index.active_dir / "relays.json"
    assert "source-secret" in saved.read_text(encoding="utf-8")
    assert manager.reconfigure.call_count == 1


def test_web_relay_failed_private_save_keeps_live_configuration(
    stream_app, bridge, monkeypatch
) -> None:
    import autodj.station_routes as routes

    client, _ = stream_app
    manager = MagicMock()
    manager.status.return_value = []
    bridge.relays = manager
    monkeypatch.setattr(
        routes, "save_relays", lambda *args: (_ for _ in ()).throw(OSError("private"))
    )
    response = client.post(
        "/api/stream/relays",
        json={
            "name": "web",
            "type": "icecast",
            "host": "192.168.1.20",
            "port": 8000,
            "mount": "/live",
            "password": "source-secret",
        },
    )
    assert response.status_code == 503
    assert not bridge.player._cfg.stream.relays
    manager.reconfigure.assert_not_called()
    assert "source-secret" not in response.text
    assert client.head("/stream.mp3").status_code == 200
    with client.stream("GET", "/stream.mp3", headers={"Accept-Encoding": "gzip"}) as response:
        assert response.status_code == 200
        assert "content-encoding" not in response.headers
        assert next(response.iter_bytes())[:2] == b"\xff\xfb"


def test_old_keyed_links_need_a_session_and_then_are_not_found(bridge, tmp_path: Path) -> None:
    """Keyed links are no longer public: no session is 401, a paired browser gets 404."""
    client = _authenticated_stream_client(bridge, tmp_path)
    client.app.state.security_policy.now = lambda: 1_000
    for path in (*_OLD_KEYED_PATHS, "/stream.ogg"):
        assert client.get(path).status_code == 401, path
        assert client.head(path).status_code == 401, path
    assert client.post("/api/stream/rotate").status_code == 401
    code = client.app.state.request_pairing_code()[0]
    paired = client.post("/api/pair", json={"code": code, "device_name": "Den"})
    assert paired.status_code == 200
    for path in (*_OLD_KEYED_PATHS, "/stream.ogg"):
        assert client.get(path).status_code == 404, path
        assert client.head(path).status_code == 404, path
    assert client.post("/api/stream/rotate").status_code in (404, 405)
    bridge.stream.add_listener.assert_not_called()


def test_icy_headers_when_asked(stream_app) -> None:
    client, stream = stream_app
    with client.stream("GET", "/stream.mp3", headers={"Icy-MetaData": "1"}) as response:
        assert response.headers["icy-metaint"] == "16000"
        assert response.headers["icy-name"] == "AutoDJ"
        assert response.headers["icy-br"] == "320"
    stream.add_listener.assert_called_with(icy=True)


def test_m3u_points_at_stream(stream_app) -> None:
    client, stream = stream_app
    response = client.get("/stream.m3u")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/x-mpegurl")
    assert response.headers["cache-control"] == "no-store"
    assert response.text.strip() == "http://testserver/stream.mp3"
    stream.add_listener.assert_not_called()


def test_playlist_uses_forwarded_https_only_from_trusted_peer(bridge) -> None:
    bridge.player._cfg.stream.trusted_proxies = ["198.51.100.5"]
    bridge.attach_stream(stream=_stream_mock(), station=MagicMock(state="idle"))
    app = ProxyHeadersMiddleware(
        create_app(bridge), trusted_hosts=proxy_allowlist(bridge.player._cfg.stream.trusted_proxies)
    )
    headers = {"X-Forwarded-Proto": "https"}
    trusted = TestClient(app, client=("198.51.100.5", 50000))
    assert trusted.get("/stream.m3u", headers=headers).text == "https://testserver/stream.mp3\n"
    untrusted = TestClient(app, client=("203.0.113.5", 50000))
    assert untrusted.get("/stream.m3u", headers=headers).text == "http://testserver/stream.mp3\n"


def test_listener_password_protects_mp3_and_playlist(bridge) -> None:
    stream = _stream_mock()
    bridge.player._cfg.stream.listener_user = "radio"
    bridge.player._cfg.stream.listener_password = "secret"
    bridge.attach_stream(stream=stream, station=MagicMock(state="idle"))
    client = TestClient(create_app(bridge), client=("192.0.2.4", 50000))
    for path in ("/stream.mp3", "/stream.m3u"):
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == 'Basic realm="AutoDJ"'
        assert client.head(path).status_code == 401
    playlist = client.get("/stream.m3u", auth=("radio", "secret"))
    assert playlist.status_code == 200
    assert "secret" not in playlist.text
    assert client.head("/stream.mp3", auth=("radio", "secret")).status_code == 200
    assert client.get("/stream.m3u", auth=("radio", "wrong")).status_code == 401
    assert client.get("/api/stream/listeners").json()["password_set"] is True
    stream.add_listener.assert_not_called()


@pytest.mark.parametrize("authorization", ["radio secret", "Basic %%%", "Basic bm9jb2xvbg=="])
def test_malformed_listener_authorization_is_rejected(bridge, authorization: str) -> None:
    bridge.player._cfg.stream.listener_user = "radio"
    bridge.player._cfg.stream.listener_password = "secret"
    bridge.attach_stream(stream=_stream_mock(), station=MagicMock(state="idle"))
    client = TestClient(create_app(bridge), client=("192.0.2.4", 50000))
    response = client.get("/stream.m3u", headers={"Authorization": authorization})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="AutoDJ"'


def test_failed_listener_auth_is_throttled_before_ban_status(bridge, tmp_path: Path) -> None:
    bridge.player._cfg.index.active_dir = tmp_path
    bridge.player._cfg.stream.listener_user = "radio"
    bridge.player._cfg.stream.listener_password = "secret"
    bridge.attach_stream(stream=_stream_mock(), station=MagicMock(state="idle"))
    client = TestClient(create_app(bridge), client=("192.0.2.4", 50000))
    assert client.post("/api/stream/bans", json={"address": "192.0.2.4"}).status_code == 200
    for _ in range(5):
        assert client.get("/stream.m3u").status_code == 401
    assert client.get("/stream.m3u").status_code == 429
    assert client.get("/stream.m3u", auth=("radio", "secret")).status_code == 429


def test_per_address_limit_kick_and_ban_routes(stream_app) -> None:
    client, stream = stream_app
    registry = client.app.state.listener_access
    registry.max_per_address = 1
    ident, listener = registry.add(stream, "192.0.2.4", "VLC", icy=False)
    assert client.get("/stream.mp3").status_code == 503
    kicked = client.delete(f"/api/stream/listeners/{ident}")
    assert kicked.status_code == 200
    assert listener.closed
    assert registry.listeners() == []
    assert client.get("/stream.mp3").status_code == 200
    ident, listener = registry.add(stream, "192.0.2.4", "VLC", icy=False)
    banned = client.post("/api/stream/bans", json={"address": "192.0.2.4"})
    assert banned.status_code == 200
    assert listener.closed
    assert registry.listeners() == []
    assert client.get("/stream.m3u").status_code == 403


async def test_kick_route_ends_open_stream_response(stream_app) -> None:
    import asyncio

    from autodj.stream import Listener

    client, stream = stream_app
    loop = asyncio.get_running_loop()
    stream.add_listener.side_effect = lambda icy: Listener(icy, 1024, loop)
    stream.remove_listener.side_effect = lambda listener: listener.close()
    path = "/stream.mp3"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("192.0.2.4", 50000),
        "server": ("testserver", 80),
    }
    sent: list[dict] = []
    started = asyncio.Event()

    async def receive() -> dict:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)
        if message["type"] == "http.response.start":
            started.set()

    task = asyncio.create_task(client.app(scope, receive, send))
    try:
        await asyncio.wait_for(started.wait(), 2)
        ident = client.app.state.listener_access.listeners()[0]["id"]
        kick = next(
            route.endpoint
            for route in client.app.routes
            if route.path == "/api/stream/listeners/{ident}"
        )
        station = client.app.state.stations.default
        assert (await kick(station, ident))["address"] == "192.0.2.4"
        await asyncio.wait_for(task, 2)
        assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
        assert client.app.state.listener_access.listeners() == []
    finally:
        task.cancel()


def test_corrupt_bans_file_does_not_prevent_startup(bridge, tmp_path: Path) -> None:
    bridge.player._cfg.index.active_dir = tmp_path
    (tmp_path / "listener_bans.json").write_text("broken", encoding="utf-8")
    bridge.attach_stream(stream=_stream_mock(), station=MagicMock(state="idle"))
    client = TestClient(create_app(bridge), client=("192.0.2.4", 50000))
    assert client.get("/stream.m3u").status_code == 200
    assert len(list(tmp_path.glob("listener_bans.json.*.bad"))) == 1


def test_ban_write_failure_returns_json_error(stream_app) -> None:
    client, _stream = stream_app
    with patch("autodj.listener_access.atomic_write", side_effect=OSError("disk full")):
        response = client.post("/api/stream/bans", json={"address": "192.0.2.4"})
    assert response.status_code == 503
    assert response.json()["detail"] == "Could not save listener ban"
    assert client.post("/api/stream/bans", json={"address": "192.0.2.4"}).status_code == 200
    with patch("autodj.listener_access.atomic_write", side_effect=OSError("disk full")):
        response = client.delete("/api/stream/bans?address=192.0.2.4")
    assert response.status_code == 503
    assert response.json()["detail"] == "Could not remove listener ban"


def test_ban_api_blocks_stream_and_survives_new_app(bridge, tmp_path: Path) -> None:
    bridge.player._cfg.index.active_dir = tmp_path
    bridge.attach_stream(stream=_stream_mock(), station=MagicMock(state="idle"))
    client = TestClient(create_app(bridge), client=("192.0.2.4", 50000))
    created = client.post(
        "/api/stream/bans",
        json={
            "address": "192.0.2.0/24",
            "minutes": 60,
            "note": "too many connections",
        },
    )
    assert created.status_code == 200, created.text
    assert client.get("/stream.m3u").status_code == 403
    assert client.head("/stream.mp3").status_code == 403
    assert client.get("/api/stream/bans").json()["bans"][0]["note"] == "too many connections"
    assert client.get("/api/stream/listeners").json()["password_set"] is False
    restarted = TestClient(create_app(bridge), client=("192.0.2.4", 50000))
    assert restarted.get("/stream.m3u").status_code == 403
    assert restarted.delete("/api/stream/bans?address=192.0.2.0%2F24").status_code == 200
    assert restarted.get("/stream.m3u").status_code == 200


def test_per_address_limit_setting_updates_admission(stream_app) -> None:
    client, _stream = stream_app
    assert (
        client.post("/api/stream/settings", json={"max_listeners_per_address": 0}).status_code
        == 200
    )
    assert client.get("/api/stream/listeners").json()["max_per_address"] == 0
    assert (
        client.post("/api/stream/settings", json={"max_listeners_per_address": -1}).status_code
        == 422
    )
    assert client.get("/api/stream/listeners").json()["max_per_address"] == 0


def test_host_check_still_applies(stream_app) -> None:
    client, stream = stream_app
    response = client.get("/stream.mp3", headers={"Host": "evil.example"})
    assert response.status_code in (400, 403, 421)
    stream.add_listener.assert_not_called()


def test_listener_limit_is_503(stream_app) -> None:
    client, stream = stream_app
    stream.add_listener.side_effect = ListenerLimitError("listener limit reached")
    response = client.get("/stream.mp3")
    assert response.status_code == 503
    assert "limit" in response.text.lower()


def test_encoder_cooldown_is_503(stream_app) -> None:
    client, stream = stream_app
    stream.add_listener.side_effect = EncoderUnavailableError("stream encoder failed")
    response = client.get("/stream.mp3")
    assert response.status_code == 503
    assert "stream encoder failed" in response.text


def test_stream_routes_are_404_in_browser_mode(client) -> None:
    assert client.get("/stream/" + "x" * 43 + ".mp3").status_code == 404
    assert client.get("/stream.mp3").status_code == 404
    assert client.get("/stream.m3u").status_code == 404
    assert client.head("/stream.mp3").status_code == 404
    assert client.get("/api/stream").status_code == 404
    assert client.post("/api/stream/rotate").status_code in (404, 405)
    assert client.post("/api/stream/settings", json={"bitrate": 192}).status_code == 404


# -- the authenticated stream API --------------------------------------------


def test_stream_info(stream_app) -> None:
    client, stream = stream_app
    stream.listener_count = 2
    assert client.get("/api/stream").json() == {
        "path": "/stream.mp3",
        "m3u_path": "/stream.m3u",
        "bitrate": 320,
        "continuous": False,
        "station_name": "AutoDJ",
        "genre": "",
        "description": "",
        "url": "",
        "show_next": False,
        "listeners": 2,
        "state": "idle",
        "relays": [],
    }


def test_bitrate_setting_validated_and_applied(stream_app, bridge) -> None:
    client, stream = stream_app
    bridge.save_persistent_state = MagicMock()
    assert client.post("/api/stream/settings", json={"bitrate": 64}).status_code == 422
    assert client.post("/api/stream/settings", json={"bitrate": 192, "x": 1}).status_code == 422
    stream.set_bitrate.assert_not_called()
    assert client.post("/api/stream/settings", json={"bitrate": 192}).json()["bitrate"] == 192
    stream.set_bitrate.assert_called_once_with(192)
    assert bridge.player._cfg.stream.bitrate == 192
    bridge.save_persistent_state.assert_called_once()


def test_continuous_setting_is_strict_and_persisted(stream_app, bridge) -> None:
    client, stream = stream_app
    bridge.save_persistent_state = MagicMock()
    assert client.post("/api/stream/settings", json={"continuous": "true"}).status_code == 422
    response = client.post("/api/stream/settings", json={"continuous": True})
    assert response.status_code == 200
    assert response.json()["continuous"] is True
    assert bridge.player._cfg.stream.continuous is True
    stream.set_bitrate.assert_not_called()
    bridge.save_persistent_state.assert_called_once()


def test_show_next_is_strict_persisted_and_reaches_the_public_feed(stream_app, bridge) -> None:
    client, _stream = stream_app
    bridge.save_persistent_state = MagicMock()
    # The upcoming song stays private until the operator turns it on.
    assert client.get("/api/nowplaying").json()["next_track"] is None
    assert client.post("/api/stream/settings", json={"show_next": "true"}).status_code == 422
    response = client.post("/api/stream/settings", json={"show_next": True})
    assert response.status_code == 200 and response.json()["show_next"] is True
    assert bridge.player._cfg.nowplaying.show_next is True
    assert client.get("/api/nowplaying").json()["next_track"]["title"] == "Song 1"
    bridge.save_persistent_state.assert_called_once()


def test_station_name_is_validated_applied_and_persisted(stream_app, bridge) -> None:
    client, stream = stream_app
    bridge.save_persistent_state = MagicMock()
    assert client.post("/api/stream/settings", json={"station_name": "  "}).status_code == 422
    bridge.save_persistent_state.assert_not_called()
    response = client.post("/api/stream/settings", json={"station_name": " Kitchen Radio "})
    assert response.status_code == 200
    assert response.json()["station_name"] == "Kitchen Radio"
    assert bridge.player._cfg.stream.station_name == "Kitchen Radio"
    stream.set_bitrate.assert_not_called()
    bridge.save_persistent_state.assert_called_once()


def test_bitrate_encoder_failure_is_503_and_not_persisted(stream_app, bridge) -> None:
    client, stream = stream_app
    bridge.save_persistent_state = MagicMock()
    stream.set_bitrate.side_effect = EncoderUnavailableError("stream encoder failed")
    response = client.post("/api/stream/settings", json={"bitrate": 128})
    assert response.status_code == 503
    assert response.json()["detail"] == "stream encoder failed"
    assert bridge.player._cfg.stream.bitrate == 320
    bridge.save_persistent_state.assert_not_called()


def test_state_reports_stream_mode(stream_app) -> None:
    client, stream = stream_app
    stream.listener_count = 3
    state = client.get("/api/status").json()
    assert state["stream_mode"] is True
    assert state["browser_playback"] is False
    assert state["stream_state"] == "idle"
    assert state["stream_listeners"] == 3
    assert state["stream_event"] is None
    assert state["settings"]["playback"]["stream_bitrate"] == 320
    assert state["settings"]["playback"]["stream_continuous"] is False
    assert state["stream_server_audio"] is False


def test_state_reports_server_audio_in_stream_mode(stream_app, bridge) -> None:
    client, *_ = stream_app
    bridge.player._server_audio_too = True
    assert client.get("/api/status").json()["stream_server_audio"] is True


def test_state_in_browser_mode_is_unchanged(client) -> None:
    state = client.get("/api/status").json()
    assert state["stream_mode"] is False
    assert state["stream_state"] is None
    assert state["stream_listeners"] == 0
    assert state["stream_event"] is None


def test_seek_refused_in_stream_mode(stream_app) -> None:
    client, *_ = stream_app
    for body in ({"seconds": 10}, {"delta": 5}, {}):
        response = client.post("/api/seek", json=body)
        assert response.status_code == 409
        assert response.json()["detail"] == "Seeking is not available while streaming."


# -- bridge wiring -----------------------------------------------------------


def _stream_bridge(scheduler=None) -> tuple[PlayerBridge, MagicMock]:
    bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock())
    stream = _stream_mock()
    bridge.attach_stream(stream=stream, station=MagicMock(state="idle"))
    if scheduler is not None:
        bridge.attach_liner_scheduler(scheduler)
    return bridge, stream


def _started(bridge: PlayerBridge, entry) -> None:
    """Start *entry* the way the player does: make it current, then call the hook."""
    bridge.player._state.current_track = entry
    bridge.on_track_started(entry)


def test_seek_raises_in_stream_mode() -> None:
    bridge, _stream = _stream_bridge()
    with pytest.raises(StreamSeekUnavailable):
        bridge.seek(seconds=1.0)
    bridge.player.seek_to.assert_not_called()


def test_on_track_started_records_history_and_sets_the_title() -> None:
    bridge, stream = _stream_bridge()
    entry = _make_entry(4)
    _started(bridge, entry)
    assert [row["title"] for row in bridge.history_snapshot()] == ["Song 4"]
    stream.set_title.assert_called_once_with("Artist", "Song 4")


def test_on_track_started_in_server_audio_mode_records_history_only() -> None:
    bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock())
    bridge.on_track_started(_make_entry(2))
    assert [row["title"] for row in bridge.history_snapshot()] == ["Song 2"]


def test_on_track_started_hands_the_liner_check_to_a_worker() -> None:
    scheduler = MagicMock()
    started = threading.Event()
    caller: list[threading.Thread] = []

    def on_track_start(_track=None) -> None:
        caller.append(threading.current_thread())
        started.set()

    scheduler.on_track_start.side_effect = on_track_start
    bridge, _stream = _stream_bridge(scheduler)
    try:
        _started(bridge, _make_entry(1))
        assert started.wait(2.0)
        assert caller[0] is not threading.current_thread()
    finally:
        bridge.shutdown_liner_worker()


def test_liner_worker_failure_is_logged(caplog) -> None:
    import time

    scheduler = MagicMock()
    scheduler.on_track_start.side_effect = RuntimeError("decode failed")
    bridge, _stream = _stream_bridge(scheduler)
    _started(bridge, _make_entry(1))
    deadline = time.monotonic() + 2.0
    while "liner track-start check failed" not in caplog.text.lower():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    bridge.shutdown_liner_worker()


def test_on_track_started_after_shutdown_skips_the_liner() -> None:
    scheduler = MagicMock()
    bridge, stream = _stream_bridge(scheduler)
    bridge.shutdown_liner_worker()
    _started(bridge, _make_entry(1))
    scheduler.on_track_start.assert_not_called()
    stream.set_title.assert_called_once()


def test_on_track_started_racing_shutdown_skips_the_liner() -> None:
    """The worker can be shut down between the check and the submit."""
    from concurrent.futures import ThreadPoolExecutor

    scheduler = MagicMock()
    bridge, _stream = _stream_bridge(scheduler)
    bridge.shutdown_liner_worker()
    closed = ThreadPoolExecutor(max_workers=1)
    closed.shutdown()
    bridge._liner_worker = closed
    _started(bridge, _make_entry(1))
    scheduler.on_track_start.assert_not_called()


def test_shutdown_without_a_stream_is_a_no_op() -> None:
    PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock()).shutdown_liner_worker()


def test_forget_track_removes_the_newest_matching_row() -> None:
    bridge, _stream = _stream_bridge()
    first, second = _make_entry(1), _make_entry(2)
    for entry in (first, second, first):
        _started(bridge, entry)
    bridge.forget_track(first)
    assert [row["title"] for row in bridge.history_snapshot()] == ["Song 1", "Song 2"]
    bridge.forget_track(_make_entry(9))  # never played: nothing to forget
    assert len(bridge.history_snapshot()) == 2


def test_forget_track_keeps_an_older_play_when_the_cut_short_start_was_ignored() -> None:
    """The set stopped between the player making the track current and the
    start hook, so that start was never recorded.  Forgetting it removed the
    row of an earlier play of the same song instead."""
    bridge, _stream = _stream_bridge()
    song, other = _make_entry(1), _make_entry(2)
    _started(bridge, song)
    _started(bridge, other)
    bridge.player._state.current_track = None  # end_set ran first
    bridge.on_track_started(song)  # ignored
    bridge.forget_track(song)
    assert [row["title"] for row in bridge.history_snapshot()] == ["Song 1", "Song 2"]


def test_forget_track_before_its_start_hook_keeps_the_history() -> None:
    bridge, _stream = _stream_bridge()
    song, other = _make_entry(1), _make_entry(2)
    _started(bridge, song)
    _started(bridge, other)
    bridge.forget_track(_make_entry(1))  # cut short before its hook ran
    assert [row["title"] for row in bridge.history_snapshot()] == ["Song 1", "Song 2"]


def test_forget_track_removes_only_the_play_that_ended() -> None:
    bridge, _stream = _stream_bridge()
    song = _make_entry(1)
    _started(bridge, song)
    _started(bridge, song)  # the same song again later
    bridge.forget_track(song)
    bridge.forget_track(song)  # a second stop forgets nothing more
    assert [row["title"] for row in bridge.history_snapshot()] == ["Song 1"]


def test_pause_moves_the_bus_too() -> None:
    bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock())
    bridge.player.bus = MagicMock()
    assert bridge.pause() is True
    bridge.player.bus.pause.assert_called_once_with(True)
    assert bridge.pause() is False
    bridge.player.bus.pause.assert_called_with(False)


def test_station_events_carry_a_sequence_number() -> None:
    bridge, _stream = _stream_bridge()
    bridge.announce_station_event("set_started")
    first = bridge.get_state()["stream_event"]
    bridge.announce_station_event("set_stopped")
    second = bridge.get_state()["stream_event"]
    assert first["name"] == "set_started"
    assert second == {"id": first["id"], "seq": first["seq"] + 1, "name": "set_stopped"}


def test_station_events_are_tied_to_this_server_process() -> None:
    """A page that outlives a restart must not mistake the new seq 1 for an old one."""
    first, _ = _stream_bridge()
    second, _ = _stream_bridge()
    first.announce_station_event("set_started")
    second.announce_station_event("set_started")
    a, b = first.get_state()["stream_event"], second.get_state()["stream_event"]
    assert a["seq"] == b["seq"] == 1
    assert isinstance(a["id"], str) and a["id"]
    assert a["id"] != b["id"]


def test_set_stream_bitrate_rejects_unknown_values() -> None:
    bridge, stream = _stream_bridge()
    with pytest.raises(ValueError, match="bitrate"):
        bridge.set_stream_bitrate(100)
    stream.set_bitrate.assert_not_called()


# -- app start-up and shutdown in stream mode --------------------------------


def test_lifespan_starts_the_station_and_shuts_it_down_first() -> None:
    from unittest.mock import patch

    from autodj.station import Station
    from autodj.stream import StreamOutput
    from tests.unit._fakes import FakeEncoder

    player = _make_player_mock()
    player.bus = MagicMock()
    player.bus.playing = False
    bridge = PlayerBridge(player=player, sim=_make_sim_mock())
    order: list[str] = []
    player.bus.remove_output.side_effect = lambda _out: order.append("remove_output")
    with patch("autodj.stream.FfmpegEncoder", FakeEncoder):
        app = create_app(bridge, stream_mode=True)
        with TestClient(app, client=("192.0.2.4", 50000)) as client:
            assert bridge.stream_mode
            stream = bridge.stream
            assert isinstance(stream, StreamOutput)
            assert isinstance(bridge.station, Station)
            assert bridge.liner_scheduler is not None
            # Automatic liners only fire while a set is heard.
            assert bridge.liner_scheduler._can_fire() is False
            player._state.is_paused = False
            player.bus.playing = True
            assert bridge.liner_scheduler._can_fire() is True
            player._state.is_paused = True
            assert bridge.liner_scheduler._can_fire() is False
            player.bus.playing = False
            player.bus.add_output.assert_called_once_with(stream)
            info = client.get("/api/stream").json()
            assert info["path"] == "/stream.mp3"
            assert info["state"] == "idle"
            original_close = stream.close

            def close() -> None:
                # The stream closes before the player is told to stop.
                order.append("close-after-stop" if player._state.should_stop else "close")
                original_close()

            stream.close = close  # type: ignore[method-assign]
    assert order == ["remove_output", "close"]
    assert bridge._liner_worker is None
    assert player._state.should_stop is True


def test_server_audio_lifespan_fires_liners_into_the_mix_bus(tmp_path: Path) -> None:
    """Plain --server-audio schedules liners too, with the configured ducking."""
    import time

    import numpy as np
    import soundfile as sf

    player = _make_player_mock()
    player.bus = MagicMock()
    player.bus.playing = True
    playback = player._cfg.playback
    playback.liners_folder = str(tmp_path)
    playback.liners_enabled = True
    playback.liners_every_n_songs = 1
    playback.liners_every_minutes = None
    playback.liners_random_min_minutes = None
    playback.liners_random_max_minutes = None
    playback.liners_pick_mode = "sequential"
    playback.liners_duck_db = -9.0
    sf.write(tmp_path / "station-id.wav", np.full(441, 0.25, np.float32), 44100)
    bridge = PlayerBridge(player=player, sim=_make_sim_mock())
    with TestClient(create_app(bridge, server_audio=True)):
        assert not bridge.stream_mode
        scheduler = bridge.liner_scheduler
        assert scheduler is not None
        player._state.is_paused = True
        assert scheduler._can_fire() is False
        player._state.is_paused = False
        bridge.on_track_started(player._state.current_track)
        deadline = time.monotonic() + 5.0
        while not player.bus.play_liner.called:
            assert time.monotonic() < deadline, "no liner reached the mix bus"
            time.sleep(0.01)
    audio = player.bus.play_liner.call_args.args[0]
    assert audio.shape == (441, 2)
    # A liner waits for a live DJ to hand back rather than being dropped (#48).
    assert player.bus.play_liner.call_args.kwargs == {"duck_db": -9.0, "after_live": True}
    assert bridge._liner_worker is None


async def _yield(_seconds: float) -> None:
    """A tick-loop sleep that only yields, so tests run many ticks quickly."""
    import asyncio

    await asyncio.sleep(0.001)


def _run_ticks_for(bridge: PlayerBridge, seconds: float, then=lambda: None) -> None:
    """Run the stream tick loops for *seconds* of wall time, cancel them, call *then*.

    *then* runs before the event loop closes, so it can release a tick
    still blocked in a worker thread (``asyncio.run`` waits for those).
    """
    import asyncio

    from autodj.server import _tick_stream_workers

    async def main() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(_tick_stream_workers(bridge, sleep=_yield), seconds)
        then()

    asyncio.run(main())


def test_tick_stream_workers_ticks_both_and_survives_failures(caplog) -> None:
    bridge, _stream = _stream_bridge(MagicMock())
    bridge.station.tick.side_effect = RuntimeError("station broke")
    _run_ticks_for(bridge, 0.2)
    assert bridge.station.tick.call_count >= 2  # a failure does not end the loop
    assert bridge.liner_scheduler.tick.call_count >= 2
    assert "station tick failed" in caplog.text
    bridge.shutdown_liner_worker()


def test_slow_liner_tick_does_not_hold_up_the_station() -> None:
    # A liner decode can take up to 30 s; the station's idle check must
    # keep its once-a-second rhythm meanwhile.
    decoding = threading.Event()
    release = threading.Event()

    def slow_liner_tick() -> None:
        decoding.set()
        release.wait(5)

    scheduler = MagicMock()
    scheduler.tick.side_effect = slow_liner_tick
    bridge, _stream = _stream_bridge(scheduler)
    try:
        _run_ticks_for(bridge, 0.3, then=release.set)
        assert decoding.is_set()
        assert scheduler.tick.call_count == 1  # still stuck in its first decode
        assert bridge.station.tick.call_count >= 3
    finally:
        release.set()
        bridge.shutdown_liner_worker()


def test_tick_stream_workers_skips_what_is_missing() -> None:
    import asyncio

    from autodj.server import _tick_stream_workers

    asyncio.run(
        _tick_stream_workers(
            PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock()), sleep=_yield
        )
    )


def test_history_route_reads_the_hooked_history(bridge) -> None:
    client = TestClient(create_app(bridge), client=("192.0.2.4", 50000))
    bridge.on_track_started(_make_entry(3))
    items = client.get("/api/history").json()["items"]
    assert [row["title"] for row in items] == ["Song 3"]


# -- fix round 1 ---------------------------------------------------------------


def _lifespan_bridge() -> PlayerBridge:
    player = _make_player_mock()
    player.bus = MagicMock()
    player.bus.playing = False
    return PlayerBridge(player=player, sim=_make_sim_mock())


def test_non_ascii_station_name_streams_with_icy(stream_app, bridge) -> None:
    """Driven at the ASGI level: TestClient itself cannot carry non-ASCII headers."""
    import asyncio

    client, stream = stream_app
    bridge.player._cfg.stream.station_name = "Радио 🎵 Café"
    path = "/stream.mp3"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"icy-metadata", b"1")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    sent: list[dict] = []

    async def receive() -> dict:
        await asyncio.sleep(10)
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(client.app(scope, receive, send))
    start = sent[0]
    assert start["status"] == 200
    headers = dict(start["headers"])
    # UTF-8 bytes through the latin-1 header transport, as Icecast does.
    assert headers[b"icy-name"] == "Радио 🎵 Café".encode()
    body = b"".join(m.get("body", b"") for m in sent[1:])
    assert body[:2] == b"\xff\xfb"
    stream.remove_listener.assert_called_with(stream.add_listener.return_value)


def test_failure_building_the_response_frees_the_listener_slot() -> None:
    from unittest.mock import patch

    from tests.unit._fakes import FakeEncoder

    bridge = _lifespan_bridge()
    with (
        patch("autodj.stream.FfmpegEncoder", FakeEncoder),
        patch("autodj.server._ListenerResponse", side_effect=RuntimeError("boom")),
        TestClient(create_app(bridge, stream_mode=True), client=("192.0.2.4", 50000)) as client,
    ):
        quiet = TestClient(client.app, raise_server_exceptions=False, client=("192.0.2.4", 50000))
        response = quiet.get("/stream.mp3")
        assert response.status_code == 500
        assert bridge.stream.listener_count == 0


def test_failure_while_sending_frees_the_listener_slot() -> None:
    import asyncio

    from starlette.requests import ClientDisconnect

    from autodj.server import _ListenerResponse

    closed: list[bool] = []

    async def body():
        yield b"never sent"

    async def failing_send(_message) -> None:
        raise OSError("client went away before the headers")

    async def receive():
        return {"type": "http.disconnect"}

    response = _ListenerResponse(body(), on_close=lambda: closed.append(True))
    scope = {"type": "http", "method": "GET", "asgi": {"spec_version": "2.4"}}
    with pytest.raises((ClientDisconnect, OSError)):  # Starlette wraps the OSError
        asyncio.run(response(scope, receive, failing_send))
    assert closed == [True]


def test_head_on_the_stream_sends_headers_only(stream_app) -> None:
    client, stream = stream_app
    response = client.head("/stream.mp3", headers={"Icy-MetaData": "1"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["icy-metaint"] == "16000"
    assert response.headers["icy-name"] == "AutoDJ"
    assert "content-length" not in response.headers
    assert response.content == b""
    stream.add_listener.assert_not_called()


def test_head_on_the_playlist_sends_headers_only(stream_app) -> None:
    client, _stream = stream_app
    body = client.get("/stream.m3u").content
    response = client.head("/stream.m3u")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/x-mpegurl")
    assert response.headers["content-length"] == str(len(body))
    assert response.content == b""


def _real_station(bridge: PlayerBridge, *, playing: bool) -> Any:
    """Give *bridge* a real station over a mock bus that is (not) playing."""
    from autodj.station import Station

    bus = MagicMock(playing=playing)
    bridge.station = Station(bus, bridge.stream, bridge.player, idle_grace=30.0)
    return bridge.station


def test_idle_play_now_sets_the_next_sets_first_track() -> None:
    bridge, _stream = _stream_bridge()
    station = _real_station(bridge, playing=False)
    entry = bridge.sim.entries[2]
    assert bridge.play_next(entry.path, now=True) is True
    assert (station._start_entry, station._start_mode) == (entry, "queue")
    bridge.player.play_now.assert_not_called()
    assert bridge.player._state.queued_next is None


def test_play_now_during_a_set_plays_it_now() -> None:
    bridge, _stream = _stream_bridge()
    bridge.player.bus = MagicMock()
    station = _real_station(bridge, playing=True)
    entry = bridge.sim.entries[2]
    assert bridge.play_next(entry.path, now=True) is True
    bridge.player.play_now.assert_called_once_with(entry)
    assert station._start_entry is None


def test_play_now_holds_the_station_lock_so_the_set_cannot_stop_meanwhile() -> None:
    """The idle grace stopping the set between "is a set playing?" and
    playing the track lost the track and started the next set elsewhere."""
    bridge, _stream = _stream_bridge()
    bridge.player.bus = MagicMock()
    station = _real_station(bridge, playing=True)
    held: list[bool] = []
    bridge.player.play_now.side_effect = lambda *_a, **_k: held.append(station._lock.locked())
    assert bridge.play_next(bridge.sim.entries[2].path, now=True) is True
    assert bridge.reseed_random() is True
    assert held == [True, True]


def test_idle_random_sets_the_next_sets_first_track() -> None:
    bridge, _stream = _stream_bridge()
    station = _real_station(bridge, playing=False)
    assert bridge.reseed_random() is True
    assert station._start_entry in bridge.sim.entries
    assert station._start_mode == "seed"
    bridge.player.play_now.assert_not_called()


def test_random_during_a_set_plays_it_now() -> None:
    bridge, _stream = _stream_bridge()
    _real_station(bridge, playing=True)
    assert bridge.reseed_random() is True
    entry = bridge.player.play_now.call_args.args[0]
    assert entry in bridge.sim.entries
    assert bridge.player.play_now.call_args.kwargs == {"pick_mode": "seed"}


def test_hook_ignores_a_start_from_a_stopped_set() -> None:
    scheduler = MagicMock()
    bridge, stream = _stream_bridge(scheduler)
    try:
        bridge.player._state.current_track = None  # end_set cleared it
        bridge.on_track_started(_make_entry(3))
        assert bridge.history_snapshot() == []
        stream.set_title.assert_not_called()
    finally:
        bridge.shutdown_liner_worker()
    scheduler.on_track_start.assert_not_called()


def test_shutdown_does_not_wait_for_a_liner_decode() -> None:
    import time

    scheduler = MagicMock()
    release = threading.Event()
    running = threading.Event()

    def slow_decode(_track=None) -> None:
        running.set()
        release.wait(5.0)

    scheduler.on_track_start.side_effect = slow_decode
    bridge, _stream = _stream_bridge(scheduler)
    _started(bridge, _make_entry(1))
    assert running.wait(2.0)
    began = time.monotonic()
    bridge.shutdown_liner_worker()
    assert time.monotonic() - began < 1.0
    release.set()


def test_stream_shutdown_failure_still_stops_the_player(caplog) -> None:
    from unittest.mock import patch

    from tests.unit._fakes import FakeEncoder

    bridge = _lifespan_bridge()
    bridge.player.bus.remove_output.side_effect = RuntimeError("bus gone")
    with (
        patch("autodj.stream.FfmpegEncoder", FakeEncoder),
        TestClient(create_app(bridge, stream_mode=True), client=("192.0.2.4", 50000)),
    ):
        stream = bridge.stream
    assert "Stopping the stream" in caplog.text
    assert stream._closed  # close still ran after remove_output failed
    assert bridge._liner_worker is None
    assert bridge.player._state.should_stop is True


def test_seed_becomes_the_first_sets_first_track() -> None:
    from unittest.mock import patch

    from tests.unit._fakes import FakeEncoder

    bridge = _lifespan_bridge()
    seed = _make_entry(2)
    with (
        patch("autodj.stream.FfmpegEncoder", FakeEncoder),
        TestClient(
            create_app(bridge, stream_mode=True, stream_first_track=seed),
            client=("192.0.2.4", 50000),
        ),
    ):
        bus = bridge.player.bus
        bus.start_set.side_effect = lambda: setattr(bus, "playing", True)
        bridge.stream.add_listener(icy=False)
        bridge.station.tick()
        bridge.player.begin_set.assert_called_once_with(seed, "seed")


def test_recording_routes_capture_stream_and_expose_scheduler_actions(tmp_path: Path) -> None:
    from autodj.config import RecordingConfig
    from tests.unit._fakes import FakeEncoder
    from tests.unit.test_recorder import _wait_for

    bridge = _lifespan_bridge()
    bridge.player._cfg.index.active_dir = tmp_path / "index"
    bridge.player._cfg.server.station_timezone = "UTC"
    bridge.player._cfg.recording = RecordingConfig(tmp_path / "recordings")
    with (
        patch("autodj.stream.FfmpegEncoder", FakeEncoder),
        TestClient(create_app(bridge, stream_mode=True)) as client,
    ):
        assert client.get("/api/recording").json()["running"] is False
        renamed = client.post("/api/stream/settings", json={"station_name": "Night Owl FM"})
        assert renamed.status_code == 200, renamed.text
        assert client.post("/api/recording/start").json()["running"] is True
        assert bridge.station._continuous() is True
        sink = bridge.stream._sinks[0]
        sink.feed(b"on air", "title")
        _wait_for(lambda: sink.status()["bytes"] == 6)
        first = sink.status()["file"]
        assert first.startswith("Night-Owl-FM-")
        assert (tmp_path / "recordings" / first).read_bytes() == b"on air"
        status = client.get("/api/status").json()["recording"]
        assert status["running"] is True
        assert "recordings" not in json.dumps(status)  # never the folder path
        actions = {action["name"] for action in client.get("/api/schedule").json()["actions"]}
        assert {"recording_start", "recording_stop", "recording_split"} <= actions
        settings = client.post(
            "/api/recording/settings",
            json={"split": "show", "retention_days": 3, "free_space_mb": 10},
        )
        assert settings.status_code == 200, settings.text
        assert settings.json()["split"] == "show"
        for bad in (
            {"split": "hour", "retention_days": True, "free_space_mb": 10},
            {"split": "hour", "retention_days": 1, "free_space_mb": 10, "folder": "C:/"},
        ):
            assert client.post("/api/recording/settings", json=bad).status_code == 422
        assert client.post("/api/recording/split").status_code == 200
        with patch.object(sink, "_save", side_effect=OSError("index volume full")):
            failed = client.post("/api/recording/stop")
        assert failed.status_code == 503
        assert failed.json()["detail"] == "Could not save the recording state"
        assert client.post("/api/recording/start").json()["running"] is True

        def run(action: str) -> dict[str, Any]:
            created = client.post(
                "/api/schedule",
                json={
                    "name": action,
                    "action": action,
                    "params": {},
                    "repeat": "daily",
                    "time": "09:00",
                },
            )
            assert created.status_code == 201, created.text
            return client.post(f"/api/schedule/{created.json()['id']}/run").json()

        assert run("recording_stop")["result"] == "ok"
        assert sink.status()["running"] is False
        assert run("recording_split")["message"] == "Recording is stopped"
        assert run("recording_start")["result"] == "ok"
        assert run("recording_split")["result"] == "ok"
        assert client.post("/api/recording/stop").json()["running"] is False
        assert client.post("/api/recording/split").status_code == 409
    state = json.loads((tmp_path / "index" / "recording.json").read_text(encoding="utf-8"))
    assert state["running"] is False
    assert state["split"] == "show"


def test_stream_headers_go_out_before_the_first_audio(stream_app) -> None:
    """A speaker gets the 200 at once, not when the encoder's first bytes arrive."""
    import asyncio
    import contextlib

    client, stream = stream_app

    async def silent():
        await asyncio.Event().wait()
        yield b""

    stream.add_listener.return_value.chunks = silent
    path = "/stream.mp3"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"accept-encoding", b"gzip")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    sent: list[dict] = []

    async def receive() -> dict:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    async def run() -> None:
        task = asyncio.create_task(client.app(scope, receive, send))
        for _ in range(100):
            if sent:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 200


@pytest.mark.parametrize("version", ["1.0", "1.1"])
def test_stream_http_framing_matches_the_players_protocol(stream_app, version) -> None:
    """Exercise the wire protocol: TestClient does not apply HTTP transfer framing."""
    import http.client
    import socket
    import time

    import uvicorn

    client, stream = stream_app
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        config = uvicorn.Config(
            client.app, http="h11", lifespan="off", log_level="error", access_log=False
        )
        server = uvicorn.Server(config)
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            with socket.create_connection(listener.getsockname(), timeout=5) as connection:
                connection.sendall(
                    f"GET /stream.mp3 HTTP/{version}\r\n"
                    "Host: testserver\r\nIcy-MetaData: 1\r\nConnection: close\r\n\r\n".encode()
                )
                response = http.client.HTTPResponse(connection)
                response.begin()
                assert response.status == 200
                if version == "1.0":
                    assert response.getheader("Transfer-Encoding") is None
                    assert response.getheader("Content-Length") is None
                    assert response.getheader("Connection") == "close"
                assert response.getheader("icy-metaint") == "16000"
                assert response.read() == b"\xff\xfb\x90\x00" + b"\x00" * 413
        finally:
            server.should_exit = True
            worker.join(timeout=5)
        assert not worker.is_alive()
    stream.remove_listener.assert_called_once_with(stream.add_listener.return_value)


def test_relays_start_with_the_stream_and_keep_the_station_playing(tmp_path: Path) -> None:
    """Relays are wired in at startup, reported without passwords, and stopped at shutdown."""
    import socket
    from unittest.mock import patch

    from autodj.config import StreamConfig
    from tests.unit._fakes import FakeEncoder

    closed_port = socket.socket()
    closed_port.bind(("127.0.0.1", 0))
    port = closed_port.getsockname()[1]
    closed_port.close()  # nothing listens there: the relays keep retrying
    password = "relay-password-value"
    bridge = _lifespan_bridge()
    bridge.player._cfg.index.index_dir = tmp_path
    bridge.player._cfg.stream = StreamConfig(
        enabled=True,
        relays={
            "same": {
                "type": "icecast",
                "host": "127.0.0.1",
                "port": port,
                "mount": "/live",
                "password": password,
            },
            "low": {
                "type": "shoutcast1",
                "host": "127.0.0.1",
                "port": port - 1,
                "password": password,
                "bitrate": 128,
            },
        },
    )
    with (
        patch("autodj.stream.FfmpegEncoder", FakeEncoder),
        TestClient(create_app(bridge, stream_mode=True), client=("192.0.2.4", 50000)) as client,
    ):
        relays = bridge.relays
        bus = bridge.player.bus
        assert bus.add_output.call_count == 2  # the stream's encoder and one for 128 kbps
        assert bridge.station._continuous() is True
        state = client.get("/api/status").json()
        assert [relay["name"] for relay in state["stream_relays"]] == ["same", "low"]
        stream_relays = client.get("/api/stream").json()["relays"]
        assert [(relay["name"], relay["type"], relay["target"]) for relay in stream_relays] == [
            (relay["name"], relay["type"], relay["target"]) for relay in state["stream_relays"]
        ]
        # Nothing listens on the port, so each relay is still logging in or
        # waiting to retry; which one depends on when the refusal arrived, and
        # it can change between the two snapshots.
        for relay in [*state["stream_relays"], *stream_relays]:
            assert relay["state"] in {"connecting", "retrying"}, relay
        assert password not in client.get("/api/status").text
        bridge.on_track_started(bridge.player._state.current_track)
        assert (tmp_path / "relay-status.json").exists()
    assert bus.remove_output.call_count == 2
    assert all(relay.status()["state"] == "stopped" for relay in relays._relays)
    assert all(output._closed for output in relays.outputs)
    assert password not in (tmp_path / "relay-status.json").read_text(encoding="utf-8")


def test_stream_listeners_are_counted_for_the_play_log(stream_app, bridge) -> None:
    """Each connection reports its client to the bridge, and its end too."""
    client, stream = stream_app
    seen: list[tuple[str, tuple[str, str]]] = []
    bridge.listener_connected = lambda who: seen.append(("in", who))
    bridge.listener_disconnected = lambda who: seen.append(("out", who))
    with client.stream("GET", "/stream.mp3", headers={"User-Agent": "VLC/3.0"}) as response:
        assert response.status_code == 200
        next(response.iter_bytes())
    assert seen == [("in", ("192.0.2.4", "VLC/3.0")), ("out", ("192.0.2.4", "VLC/3.0"))]
    stream.remove_listener.assert_called_with(stream.add_listener.return_value)


def test_a_failed_count_still_frees_the_listener(stream_app, bridge) -> None:
    client, stream = stream_app
    left: list[tuple[str, str]] = []

    def broken(_who):
        raise RuntimeError("count failed")

    bridge.listener_connected = broken
    bridge.listener_disconnected = left.append
    assert (
        TestClient(client.app, raise_server_exceptions=False, client=("192.0.2.4", 50000))
        .get("/stream.mp3")
        .status_code
        == 500
    )
    stream.remove_listener.assert_called_once_with(stream.add_listener.return_value)
    assert left == [("192.0.2.4", "testclient")]
