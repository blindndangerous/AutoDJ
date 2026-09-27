"""Stream routes, security and the bridge's stream-mode wiring."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from autodj._bridge import PlayerBridge, StreamSeekUnavailable
from autodj.server import create_app
from autodj.stream import EncoderUnavailableError, ListenerLimitError
from autodj.stream_secret import StreamSecret

from ._helpers import _make_entry, _make_player_mock, _make_sim_mock


def _stream_mock() -> MagicMock:
    stream = MagicMock()
    stream.listener_count = 0
    listener = MagicMock()

    async def chunks():
        yield b"\xff\xfb\x90\x00" + b"\x00" * 413

    listener.chunks = chunks
    stream.add_listener.return_value = listener
    return stream


@pytest.fixture
def stream_app(bridge, tmp_path: Path):
    secret = StreamSecret.load_or_create(tmp_path / ".stream-secret")
    stream = _stream_mock()
    bridge.player._cfg.stream.enabled = True
    bridge.attach_stream(stream=stream, secret=secret, station=MagicMock(state="idle"))
    app = create_app(bridge)
    return TestClient(app), secret, stream


# -- the stream routes -------------------------------------------------------


def test_wrong_secret_is_404(stream_app) -> None:
    client, _secret, stream = stream_app
    assert client.get("/stream/" + "x" * 43 + ".mp3").status_code == 404
    assert client.get("/stream/.mp3").status_code == 404
    assert client.get("/stream/" + "x" * 43 + ".ogg").status_code == 404
    stream.add_listener.assert_not_called()


def test_right_secret_streams_mp3(stream_app) -> None:
    client, secret, stream = stream_app
    with client.stream("GET", f"/stream/{secret.value}.mp3") as response:
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.headers["cache-control"] == "no-store"
        assert "icy-metaint" not in response.headers
        assert next(response.iter_bytes())[:2] == b"\xff\xfb"
    stream.add_listener.assert_called_once_with(icy=False)
    stream.remove_listener.assert_called_with(stream.add_listener.return_value)


def test_stream_needs_no_session_when_authentication_is_on(bridge, tmp_path: Path) -> None:
    """Sonos cannot pair: the secret URL is enough, everything else still needs a login."""
    from autodj.config import ServerConfig

    bridge.player._cfg.server = ServerConfig(
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
        access_token="t" * 32,
    )
    secret = StreamSecret.load_or_create(tmp_path / ".stream-secret")
    bridge.attach_stream(stream=_stream_mock(), secret=secret, station=MagicMock(state="idle"))
    client = TestClient(create_app(bridge))
    assert client.get("/api/stream").status_code == 401
    assert client.get(f"/stream/{secret.value}.m3u").status_code == 200
    with client.stream("GET", f"/stream/{secret.value}.mp3") as response:
        assert response.status_code == 200


def test_icy_headers_when_asked(stream_app) -> None:
    client, secret, stream = stream_app
    with client.stream(
        "GET", f"/stream/{secret.value}.mp3", headers={"Icy-MetaData": "1"}
    ) as response:
        assert response.headers["icy-metaint"] == "16000"
        assert response.headers["icy-name"] == "AutoDJ"
        assert response.headers["icy-br"] == "320"
    stream.add_listener.assert_called_with(icy=True)


def test_m3u_points_at_stream(stream_app) -> None:
    client, secret, stream = stream_app
    response = client.get(f"/stream/{secret.value}.m3u")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/x-mpegurl")
    assert response.headers["cache-control"] == "no-store"
    assert response.text.strip() == f"http://testserver/stream/{secret.value}.mp3"
    stream.add_listener.assert_not_called()


def test_rotate_invalidates_old_url(stream_app) -> None:
    client, secret, stream = stream_app
    old = secret.value
    data = client.post("/api/stream/rotate").json()
    assert old not in data["path"]
    assert data["path"] == f"/stream/{secret.value}.mp3"
    assert client.get(f"/stream/{old}.mp3").status_code == 404
    stream.disconnect_all.assert_called_once()
    assert client.get("/api/status").json()["stream_event"]["name"] == "link_changed"


def test_host_check_still_applies(stream_app) -> None:
    client, secret, stream = stream_app
    response = client.get(f"/stream/{secret.value}.mp3", headers={"Host": "evil.example"})
    assert response.status_code in (400, 403, 421)
    stream.add_listener.assert_not_called()


def test_listener_limit_is_503(stream_app) -> None:
    client, secret, stream = stream_app
    stream.add_listener.side_effect = ListenerLimitError("listener limit reached")
    response = client.get(f"/stream/{secret.value}.mp3")
    assert response.status_code == 503
    assert "limit" in response.text.lower()


def test_encoder_cooldown_is_503(stream_app) -> None:
    client, secret, stream = stream_app
    stream.add_listener.side_effect = EncoderUnavailableError("stream encoder failed")
    response = client.get(f"/stream/{secret.value}.mp3")
    assert response.status_code == 503
    assert "stream encoder failed" in response.text


def test_wrong_secret_attempts_are_rate_limited(stream_app) -> None:
    client, *_ = stream_app
    responses = [client.get("/stream/" + "y" * 43 + ".mp3") for _ in range(8)]
    codes = [r.status_code for r in responses]
    assert codes[:5] == [404] * 5
    assert codes[5:] == [429] * 3
    assert int(responses[-1].headers["retry-after"]) >= 1


def test_right_secret_is_not_rate_limited(stream_app) -> None:
    client, secret, _stream = stream_app
    codes = [client.get(f"/stream/{secret.value}.m3u").status_code for _ in range(8)]
    assert codes == [200] * 8


def test_stream_routes_are_404_in_browser_mode(client) -> None:
    assert client.get("/stream/" + "x" * 43 + ".mp3").status_code == 404
    assert client.get("/api/stream").status_code == 404
    assert client.post("/api/stream/rotate").status_code == 404
    assert client.post("/api/stream/settings", json={"bitrate": 192}).status_code == 404


# -- the authenticated stream API --------------------------------------------


def test_stream_info(stream_app) -> None:
    client, secret, stream = stream_app
    stream.listener_count = 2
    assert client.get("/api/stream").json() == {
        "path": f"/stream/{secret.value}.mp3",
        "m3u_path": f"/stream/{secret.value}.m3u",
        "bitrate": 320,
        "listeners": 2,
        "state": "idle",
    }


def test_bitrate_setting_validated_and_applied(stream_app, bridge) -> None:
    client, _secret, stream = stream_app
    bridge.save_persistent_state = MagicMock()
    assert client.post("/api/stream/settings", json={"bitrate": 64}).status_code == 422
    assert client.post("/api/stream/settings", json={"bitrate": 192, "x": 1}).status_code == 422
    stream.set_bitrate.assert_not_called()
    assert client.post("/api/stream/settings", json={"bitrate": 192}).json()["bitrate"] == 192
    stream.set_bitrate.assert_called_once_with(192)
    assert bridge.player._cfg.stream.bitrate == 192
    bridge.save_persistent_state.assert_called_once()


def test_bitrate_encoder_failure_is_503_and_not_persisted(stream_app, bridge) -> None:
    client, _secret, stream = stream_app
    bridge.save_persistent_state = MagicMock()
    stream.set_bitrate.side_effect = EncoderUnavailableError("stream encoder failed")
    response = client.post("/api/stream/settings", json={"bitrate": 128})
    assert response.status_code == 503
    assert response.json()["detail"] == "stream encoder failed"
    assert bridge.player._cfg.stream.bitrate == 320
    bridge.save_persistent_state.assert_not_called()


def test_state_reports_stream_mode(stream_app) -> None:
    client, _secret, stream = stream_app
    stream.listener_count = 3
    state = client.get("/api/status").json()
    assert state["stream_mode"] is True
    assert state["browser_playback"] is False
    assert state["stream_state"] == "idle"
    assert state["stream_listeners"] == 3
    assert state["stream_event"] is None
    assert state["settings"]["playback"]["stream_bitrate"] == 320


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
    bridge.attach_stream(
        stream=stream, secret=MagicMock(), station=MagicMock(state="idle"), scheduler=scheduler
    )
    return bridge, stream


def test_seek_raises_in_stream_mode() -> None:
    bridge, _stream = _stream_bridge()
    with pytest.raises(StreamSeekUnavailable):
        bridge.seek(seconds=1.0)
    bridge.player.seek_to.assert_not_called()


def test_on_track_started_records_history_and_sets_the_title() -> None:
    bridge, stream = _stream_bridge()
    entry = _make_entry(4)
    bridge.on_track_started(entry)
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

    def on_track_start() -> None:
        caller.append(threading.current_thread())
        started.set()

    scheduler.on_track_start.side_effect = on_track_start
    bridge, _stream = _stream_bridge(scheduler)
    try:
        bridge.on_track_started(_make_entry(1))
        assert started.wait(2.0)
        assert caller[0] is not threading.current_thread()
    finally:
        bridge.shutdown_stream_workers()


def test_liner_worker_failure_is_logged(caplog) -> None:
    scheduler = MagicMock()
    done = threading.Event()

    def boom() -> None:
        done.set()
        raise RuntimeError("decode failed")

    scheduler.on_track_start.side_effect = boom
    bridge, _stream = _stream_bridge(scheduler)
    bridge.on_track_started(_make_entry(1))
    assert done.wait(2.0)
    bridge.shutdown_stream_workers()  # waits for the worker to finish logging
    assert "liner" in caplog.text.lower()


def test_on_track_started_after_shutdown_skips_the_liner() -> None:
    scheduler = MagicMock()
    bridge, stream = _stream_bridge(scheduler)
    bridge.shutdown_stream_workers()
    bridge.on_track_started(_make_entry(1))
    scheduler.on_track_start.assert_not_called()
    stream.set_title.assert_called_once()


def test_on_track_started_racing_shutdown_skips_the_liner() -> None:
    """The worker can be shut down between the check and the submit."""
    from concurrent.futures import ThreadPoolExecutor

    scheduler = MagicMock()
    bridge, _stream = _stream_bridge(scheduler)
    bridge.shutdown_stream_workers()
    closed = ThreadPoolExecutor(max_workers=1)
    closed.shutdown()
    bridge._liner_worker = closed
    bridge.on_track_started(_make_entry(1))
    scheduler.on_track_start.assert_not_called()


def test_shutdown_without_a_stream_is_a_no_op() -> None:
    PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock()).shutdown_stream_workers()


def test_forget_track_removes_the_newest_matching_row() -> None:
    bridge, _stream = _stream_bridge()
    first, second = _make_entry(1), _make_entry(2)
    for entry in (first, second, first):
        bridge.on_track_started(entry)
    bridge.forget_track(first)
    assert [row["title"] for row in bridge.history_snapshot()] == ["Song 1", "Song 2"]
    bridge.forget_track(_make_entry(9))  # never played: nothing to forget
    assert len(bridge.history_snapshot()) == 2


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
    assert second == {"seq": first["seq"] + 1, "name": "set_stopped"}


def test_set_stream_bitrate_rejects_unknown_values() -> None:
    bridge, stream = _stream_bridge()
    with pytest.raises(ValueError, match="bitrate"):
        bridge.set_stream_bitrate(100)
    stream.set_bitrate.assert_not_called()


# -- app start-up and shutdown in stream mode --------------------------------


def test_lifespan_starts_the_station_and_shuts_it_down_first(tmp_path: Path) -> None:
    from unittest.mock import patch

    from autodj.station import Station
    from autodj.stream import StreamOutput
    from tests.unit.test_stream_output import FakeEncoder

    player = _make_player_mock()
    player.bus = MagicMock()
    player.bus.playing = False
    bridge = PlayerBridge(player=player, sim=_make_sim_mock())
    secret = StreamSecret.load_or_create(tmp_path / ".stream-secret")
    order: list[str] = []
    player.bus.remove_output.side_effect = lambda _out: order.append("remove_output")
    with patch("autodj.stream.ffmpeg_encoder", FakeEncoder):
        app = create_app(bridge, stream_secret=secret)
        with TestClient(app) as client:
            assert bridge.stream_mode
            stream = bridge.stream
            assert isinstance(stream, StreamOutput)
            assert isinstance(bridge.station, Station)
            assert bridge.liner_scheduler is not None
            assert stream.on_listener_change == bridge.station.listener_changed
            player.bus.add_output.assert_called_once_with(stream)
            info = client.get("/api/stream").json()
            assert info["path"] == f"/stream/{secret.value}.mp3"
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


def test_tick_stream_workers_ticks_both_and_survives_failures(caplog) -> None:
    import asyncio

    from autodj.server import _tick_stream_workers

    bridge, _stream = _stream_bridge(MagicMock())
    bridge.station.tick.side_effect = RuntimeError("station broke")
    asyncio.run(_tick_stream_workers(bridge))
    bridge.station.tick.assert_called_once()
    bridge.liner_scheduler.tick.assert_called_once()
    assert "station tick failed" in caplog.text
    bridge.shutdown_stream_workers()


def test_tick_stream_workers_skips_what_is_missing() -> None:
    import asyncio

    from autodj.server import _tick_stream_workers

    asyncio.run(
        _tick_stream_workers(PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock()))
    )


def test_history_route_reads_the_hooked_history(bridge) -> None:
    client = TestClient(create_app(bridge))
    bridge.on_track_started(_make_entry(3))
    items = client.get("/api/history").json()["items"]
    assert [row["title"] for row in items] == ["Song 3"]
