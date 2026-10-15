"""Live DJ input (#48): accounts, buffers, the takeover on the mix bus and talkover."""

from __future__ import annotations

import base64
import json
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from autodj import live as live_module
from autodj.live import (
    DjCredentials,
    LiveDesk,
    MicResampler,
    PcmBuffer,
    TalkoverSettings,
    strip_secrets,
)
from autodj.live_protocol import IcecastSourceH11Protocol, close_delimited_source_head
from autodj.mixbus import _LIVE_PREROLL_FRAMES, BusEvents, MixBus, RenderedTrack
from autodj.render_ahead import RenderAhead
from autodj.station import Station
from autodj.stations import StationPaths
from autodj.stereo import SAMPLE_RATE

PASSWORD = "correct horse battery"


def _basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


# --- accounts ---------------------------------------------------------------


def test_accounts_keep_only_scrypt_hashes_written_atomically(tmp_path) -> None:
    path = tmp_path / "live_djs.json"
    store = DjCredentials(path)
    assert store.save("alice", "DJ Alice", PASSWORD) == {
        "username": "alice",
        "display_name": "DJ Alice",
        "password_set": True,
    }
    assert PASSWORD not in path.read_text()
    assert list(tmp_path.glob("*.tmp")) == []
    assert store.accounts() == [
        {"username": "alice", "display_name": "DJ Alice", "password_set": True}
    ]
    assert "hash" not in json.dumps(store.accounts())
    assert store.authenticate(_basic("alice", PASSWORD)) == ("alice", "DJ Alice")
    for header in (
        _basic("alice", "wrong password"),
        _basic("bob", PASSWORD),
        "Bearer token",
        "Basic not-base64!",
        "Basic " + base64.b64encode(b"no-colon").decode(),
        "Basic",
    ):
        assert store.authenticate(header) is None


def test_editing_a_dj_keeps_the_password_unless_a_new_one_is_given(tmp_path) -> None:
    store = DjCredentials(tmp_path / "live_djs.json")
    with pytest.raises(ValueError, match="Password"):
        store.save("alice", "DJ Alice", None)  # a new DJ needs one
    store.save("alice", "DJ Alice", PASSWORD)
    store.save("alice", "Alice in the Morning", None)
    assert store.authenticate(_basic("alice", PASSWORD)) == ("alice", "Alice in the Morning")
    store.save("alice", "Alice", "another long password")
    assert store.authenticate(_basic("alice", PASSWORD)) is None
    assert store.authenticate(_basic("alice", "another long password")) == ("alice", "Alice")
    for username, name, password in (
        ("", "x", PASSWORD),
        ("a:b", "x", PASSWORD),
        (" alice", "x", PASSWORD),
        ("é", "x", PASSWORD),
        ("bob", "", PASSWORD),
        ("bob", "x" * 81, PASSWORD),
        ("bob", "x", "short"),
        ("bob", "x", 12345678901234),
    ):
        with pytest.raises(ValueError):
            store.save(username, name, password)
    assert store.delete("alice") is True
    assert store.delete("alice") is False
    assert store.authenticate(_basic("alice", "another long password")) is None


def test_every_password_the_store_saves_can_log_in(tmp_path) -> None:
    """The Basic header cap fits the longest username and password save accepts."""
    store = DjCredentials(tmp_path / "live_djs.json")
    username = "u" * 64
    for password in ("пароль-" * 30, chr(0x1F600) * 256, "p" * 256):
        store.save(username, "DJ Long", password)
        assert store.authenticate(_basic(username, password)) == (username, "DJ Long")
    too_long = "Basic " + "A" * (live_module._MAX_BASIC_LENGTH + 4)
    assert store.authenticate(too_long) is None


def test_a_damaged_account_file_lets_nobody_in(tmp_path) -> None:
    path = tmp_path / "live_djs.json"
    store = DjCredentials(path)
    store.save("alice", "DJ Alice", PASSWORD)
    path.write_text("{not json", encoding="utf-8")
    assert store.authenticate(_basic("alice", PASSWORD)) is None
    with pytest.raises(ValueError):
        store.accounts()
    assert store.settings() == TalkoverSettings()


def test_talkover_settings_are_validated_and_saved_beside_the_accounts(tmp_path) -> None:
    store = DjCredentials(tmp_path / "live_djs.json")
    store.save("alice", "DJ Alice", PASSWORD)
    settings = TalkoverSettings.parse({"mic_return_seconds": 2, "mic_duck_db": 20})
    store.save_settings(settings)
    assert DjCredentials(store.path).settings() == settings
    assert store.authenticate(_basic("alice", PASSWORD)) == ("alice", "DJ Alice")
    for bad in (
        {"mic_return_seconds": 0},
        {"mic_return_seconds": 11},
        {"mic_duck_db": -1},
        {"mic_duck_db": True},
        {"mic_duck_db": "12"},
        {"mic_duck_db": float("nan")},
        [],
    ):
        with pytest.raises(ValueError):
            TalkoverSettings.parse(bad)


def test_backups_keep_the_accounts_but_not_their_hashes(tmp_path) -> None:
    store = DjCredentials(tmp_path / "live_djs.json")
    store.save("alice", "DJ Alice", PASSWORD)
    store.save_settings(TalkoverSettings(mic_return_seconds=1.5))
    raw = store.path.read_bytes()
    assert b'"hash"' in raw
    stripped = strip_secrets(raw)
    assert b"hash" not in stripped and b"salt" not in stripped
    restored = tmp_path / "restored.json"
    restored.write_bytes(stripped)
    again = DjCredentials(restored)
    assert again.accounts() == [
        {"username": "alice", "display_name": "DJ Alice", "password_set": False}
    ]
    assert again.settings().mic_return_seconds == 1.5
    assert again.authenticate(_basic("alice", PASSWORD)) is None
    again.save("alice", "DJ Alice", PASSWORD)  # the operator sets it again
    assert again.authenticate(_basic("alice", PASSWORD)) == ("alice", "DJ Alice")
    assert strip_secrets(b"{damaged") == b""
    assert strip_secrets(b'{"version": 1, "users": []}') == b""


def test_named_stations_have_their_own_accounts(tmp_path) -> None:
    cfg = SimpleNamespace(index=SimpleNamespace(active_dir=tmp_path))
    default = StationPaths.default(cfg).live_djs
    named = StationPaths.for_station(cfg, "night").live_djs
    assert named == tmp_path / "stations" / "night" / "live_djs.json"
    DjCredentials(default).save("alice", "Day DJ", PASSWORD)
    assert DjCredentials(named).authenticate(_basic("alice", PASSWORD)) is None


# --- buffers -----------------------------------------------------------------


def test_pcm_buffer_drops_old_frames_and_sanitizes_input() -> None:
    buffer = PcmBuffer(seconds=0.02)
    first = np.full((882, 2), 0.25, np.float32)
    second = np.full((882, 2), 0.75, np.float32)
    second[0, 0] = np.nan
    second[1, 0] = 9.0
    buffer.push(first.tobytes())
    buffer.push(second.tobytes())
    assert buffer.frames == 882
    got = buffer.pop(1000)
    assert got[0, 0] == 0 and got[1, 0] == 1.0
    assert np.allclose(got[2:882], 0.75)
    assert not got[882:].any()  # padded with silence


@pytest.mark.parametrize("rate", [16000, 44100, 48000, 96000])
def test_microphone_resampling_is_seamless_across_messages(rate: int) -> None:
    seconds = 0.5
    t = np.arange(int(rate * seconds)) / rate
    pcm = (np.sin(2 * np.pi * 220 * t) * 16000).astype("<i2")
    whole = MicResampler(rate).convert(pcm.tobytes())
    pieces = MicResampler(rate)
    parts = [pieces.convert(pcm[i : i + 997].tobytes()) for i in range(0, len(pcm), 997)]
    joined = np.concatenate(parts)
    assert abs(len(joined) - seconds * SAMPLE_RATE) <= 2
    # The same samples either way (rounding may move the very last one).
    common = min(len(joined), len(whole))
    assert abs(len(joined) - len(whole)) <= 1
    assert np.allclose(joined[:common], whole[:common], atol=1e-4)
    assert np.array_equal(joined[:, 0], joined[:, 1])
    assert MicResampler(rate).convert(b"").shape == (0, 2)


# --- the takeover on the bus ---------------------------------------------------


class _Source:
    """A decoded live source as the bus sees it."""

    def __init__(self, level: float = 0.3, seconds: float = 1.0, started: bool = True) -> None:
        self.buffer = PcmBuffer(2.0)
        self.started = started
        self.primed = False
        self.underruns = 0
        self.feed(level, seconds)

    def feed(self, level: float, seconds: float) -> None:
        self.buffer.push_frames(np.full((int(seconds * SAMPLE_RATE), 2), level, np.float32))


def _bus(
    tmp_path, *levels: float
) -> tuple[MixBus, LiveDesk, list[RenderedTrack], list, list[RenderedTrack]]:
    """A playing bus over *levels* (one constant track each), its desk and its queue."""
    tracks = [
        RenderedTrack(
            SimpleNamespace(path=f"t{i}"), np.full((400000, 2), level, np.float32), None, 0, ""
        )
        for i, level in enumerate(levels)
    ]
    queue = list(tracks)
    started: list[RenderedTrack] = []
    bus = MixBus(
        BusEvents(started.append, lambda _pos: None, lambda: queue.pop(0) if queue else None),
        eq_gains=lambda: (1.0, 1.0, 1.0),
    )
    desk = LiveDesk(tmp_path / "live_djs.json")
    desk.crossfade_seconds = 0.1
    bus.set_live(desk)
    bus.start_set()
    bus.render_block()  # the set is under way: its first song has started
    return bus, desk, tracks, started, queue


def _blocks(bus: MixBus, count: int) -> np.ndarray:
    return np.concatenate([bus.render_block() for _ in range(count)])


def test_a_live_source_crossfades_in_and_holds_automation_where_it_is(tmp_path) -> None:
    bus, desk, tracks, started, _queue = _bus(tmp_path, 0.6, 0.2)
    _blocks(bus, 3)
    source = _Source(0.3, seconds=0.1)  # less than the pre-roll
    desk.source = source
    _blocks(bus, 2)
    assert bus._live_gain == 0.0  # still buffering: the music plays on
    source.feed(0.3, 1.0)
    fade = _blocks(bus, 6)
    # Equal power: the music and the DJ cross on a quarter circle.
    gain = (0.2 + 0.2 * 441 / 881) * np.pi / 2  # halfway through the second block
    assert fade[882 + 441, 0] == pytest.approx(0.6 * np.cos(gain) + 0.3 * np.sin(gain), abs=1e-3)
    assert bus._live_gain == 1.0 and bus.live_on_air
    held = bus._pos
    live = _blocks(bus, 5)
    assert np.allclose(live, 0.3, atol=1e-4)
    assert bus._pos == held and bus._current is tracks[0]
    assert started == [tracks[0]]  # nothing advanced under the DJ


def test_a_source_sending_only_silence_stays_off_the_air(tmp_path) -> None:
    bus, desk, _tracks, _started, _queue = _bus(tmp_path, 0.6)
    desk.source = _Source(0.0, seconds=1.0, started=False)
    out = _blocks(bus, 10)
    assert bus._live_gain == 0.0
    assert np.allclose(out, 0.6)


def test_an_underrun_plays_silence_and_keeps_automation_held(tmp_path, caplog) -> None:
    """Counted on the source for its route to log: the audio thread writes no log line."""
    bus, desk, _tracks, _started, _queue = _bus(tmp_path, 0.6)
    source = _Source(0.3, seconds=0.5)
    desk.source = source
    _blocks(bus, 10)
    held = bus._pos
    for _ in range(30):  # drains the source, then more
        bus.render_block()
    assert bus._pos == held and bus._live_gain == 1.0
    assert not bus.render_block().any()
    assert source.underruns == 1
    assert caplog.text == ""
    source.feed(0.3, _LIVE_PREROLL_FRAMES / SAMPLE_RATE + 0.05)
    assert np.allclose(bus.render_block(), 0.3, atol=1e-4)


def test_a_liner_due_under_a_dj_plays_once_after_and_a_time_check_never(tmp_path) -> None:
    bus, desk, _tracks, _started, _queue = _bus(tmp_path, 0.0)
    source = _Source(0.3, seconds=1.5)
    desk.source = source
    _blocks(bus, 10)
    first_id = np.full((4410, 2), 0.25, np.float32)
    station_id = np.full((4410, 2), 0.5, np.float32)
    bus.play_liner(first_id, -6.0, after_live=True)
    bus.play_liner(station_id, -6.0, after_live=True)  # the latest replaces it: once
    bus.play_liner(np.full((4410, 2), 0.9, np.float32), -6.0)  # a time check: stale later
    assert bus._liner is None
    assert np.allclose(bus.render_block(), 0.3, atol=1e-4)
    bus.drop_held_track()
    desk.source, desk.return_source = None, source
    _blocks(bus, 6)  # the DJ fades out
    assert bus._liner is station_id
    assert bus._deferred_liner is None


def test_automation_comes_back_with_the_next_song_not_the_interrupted_one(tmp_path) -> None:
    bus, desk, tracks, started, _queue = _bus(tmp_path, 0.6, 0.2)
    source = _Source(0.3, seconds=1.5)
    desk.source = source
    _blocks(bus, 10)
    bus.drop_held_track()
    desk.source = None
    desk.return_source = source
    back = _blocks(bus, 8)
    assert started == [tracks[0], tracks[1]]
    assert bus._live_gain == 0.0 and desk.return_source is None
    assert np.allclose(back[-882:], 0.2)


def test_a_song_ending_under_the_crossfade_does_not_start_the_next(tmp_path) -> None:
    """From the moment the DJ takes the air, no song starts until they leave."""
    short = RenderedTrack(
        SimpleNamespace(path="short"), np.full((882 * 3, 2), 0.6, np.float32), None, 0, ""
    )
    queue = [short]
    later = RenderedTrack(
        SimpleNamespace(path="next"), np.full((88200, 2), 0.2, np.float32), None, 0, ""
    )
    started: list[RenderedTrack] = []
    bus = MixBus(
        BusEvents(started.append, lambda _pos: None, lambda: queue.pop(0) if queue else None),
        eq_gains=lambda: (1.0, 1.0, 1.0),
    )
    desk = LiveDesk(tmp_path / "live_djs.json")
    desk.crossfade_seconds = 0.2  # ten blocks: the song ends three blocks in
    bus.set_live(desk)
    bus.start_set()
    bus.render_block()
    queue.append(later)
    source = _Source(0.3, seconds=1.5)
    desk.source = source
    _blocks(bus, 15)
    assert started == [short]  # "next" waits under the DJ
    assert bus._current is None and queue == [later]
    desk.source, desk.return_source = None, source
    _blocks(bus, 12)
    assert started == [short, later]


def test_the_bus_never_waits_for_the_desk_or_a_decoder_start(tmp_path, monkeypatch) -> None:
    bus, desk, _tracks, _started, _queue = _bus(tmp_path, 0.6)
    gate = threading.Event()

    class SlowSource(_Source):
        def __init__(self, *_args) -> None:
            gate.wait(5)
            super().__init__(0.3)

    monkeypatch.setattr(live_module, "LiveSource", SlowSource)
    claims: list[object] = []
    first = threading.Thread(target=lambda: claims.append(desk.claim("a", "A", "mp3")))
    first.start()
    time.sleep(0.05)
    with desk._lock:  # nothing on the bus thread takes the desk's lock
        begun = time.monotonic()
        for _ in range(5):
            bus.render_block()
        assert time.monotonic() - begun < 1.0
    # One source per station, even while the first decoder is starting.
    assert desk.claim("b", "B", "mp3") is None
    gate.set()
    first.join(5)
    assert isinstance(claims[0], SlowSource) and desk.source is claims[0]
    assert desk.claim("b", "B", "mp3") is None


def test_talkover_ducks_by_the_setting_and_returns_over_the_return_time(tmp_path) -> None:
    bus, desk, _tracks, _started, _queue = _bus(tmp_path, 0.5)
    desk.settings = TalkoverSettings(mic_return_seconds=0.2, mic_duck_db=20.0)
    mic = desk.claim_mic()
    assert mic is not None and desk.talkover and desk.claim_mic() is None
    mic.push_frames(np.full((882, 2), 0.1, np.float32))
    assert np.allclose(bus.render_block(), 0.5)  # gathering its pre-roll
    mic.push_frames(np.full((SAMPLE_RATE // 2, 2), 0.1, np.float32))
    _blocks(bus, 5)
    assert bus._mic_gain == pytest.approx(0.1)
    assert bus.render_block()[0, 0] == pytest.approx(0.5 * 0.1 + 0.1, abs=1e-3)
    desk.release_mic(mic)
    assert not desk.talkover
    blocks = int(0.2 * SAMPLE_RATE / 882)
    _blocks(bus, blocks - 1)
    assert bus._mic_gain < 1.0
    _blocks(bus, 2)
    assert bus._mic_gain == 1.0


def test_talkover_is_heard_while_no_set_is_playing(tmp_path) -> None:
    bus, desk, _tracks, _started, _queue = _bus(tmp_path, 0.5)
    bus.stop_set()
    mic = desk.claim_mic()
    assert mic is not None
    mic.push_frames(np.full((SAMPLE_RATE // 4, 2), 0.1, np.float32))
    assert np.allclose(bus.render_block(), 0.1, atol=1e-4)


def test_desk_status_names_the_dj_only_once_they_are_on_the_air(tmp_path) -> None:
    desk = LiveDesk(tmp_path / "live_djs.json")
    source = _Source(0.3, started=False)
    source.name = "DJ Alice"
    desk.source = source
    assert desk.status() is None
    source.started = True
    desk.since = datetime.now(UTC).isoformat()
    assert desk.status() == {"dj": "DJ Alice", "since": desk.since}


def test_releasing_a_source_readies_automation_before_it_stops(tmp_path) -> None:
    order: list[str] = []
    desk = LiveDesk(
        tmp_path / "live_djs.json",
        before_return=lambda _source: order.append(f"ready:{desk.source is not None}"),
        on_end=lambda: order.append("end"),
    )
    source = MagicMock(started=True, username="alice")
    source.close.side_effect = lambda: order.append("close")
    desk.source = source
    desk.release_username("bob")
    assert desk.source is source
    desk.release_username("alice")
    assert order == ["ready:True", "close", "end"]
    assert desk.source is None and desk.return_source is source
    quiet = MagicMock(started=False)
    desk.source = quiet
    desk.release(quiet)
    assert order == ["ready:True", "close", "end"]  # never on the air: nothing to hand back


def test_a_source_is_handed_back_once_however_many_let_it_go(tmp_path) -> None:
    calls: list[str] = []
    gate = threading.Event()

    def ready(_source) -> None:
        calls.append("ready")
        gate.wait(2)

    desk = LiveDesk(
        tmp_path / "live_djs.json", before_return=ready, on_end=lambda: calls.append("end")
    )
    source = MagicMock(started=True, username="alice")
    desk.source = source
    threads = [threading.Thread(target=desk.release, args=(source,)) for _ in range(3)]
    for thread in threads:
        thread.start()
    time.sleep(0.1)
    gate.set()
    for thread in threads:
        thread.join(5)
    assert calls == ["ready", "end"]
    source.close.assert_called_once()


# --- the station and render-ahead ------------------------------------------------


def test_the_station_holds_its_set_under_a_dj_and_starts_none_for_one() -> None:
    bus = MagicMock(playing=True)
    stream = MagicMock(listener_count=0)
    player = MagicMock()
    player._state.is_paused = False
    now = [0.0]
    station = Station(bus, stream, player, idle_grace=5, clock=lambda: now[0])
    station.live_active = lambda: True
    for step in range(20):
        now[0] = float(step)
        station.tick()
    bus.stop_set.assert_not_called()
    bus.playing = False
    station.tick()
    bus.start_set.assert_not_called()


def test_rerender_keeps_the_waiting_track_to_fall_back_on() -> None:
    old = RenderedTrack(SimpleNamespace(path="a"), np.zeros((10, 2), np.float32), None, 0, "")
    fresh = RenderedTrack(SimpleNamespace(path="a"), np.zeros((10, 2), np.float32), None, 0, "")
    rewound: list[RenderedTrack] = []
    worker = RenderAhead(
        render=lambda: None,
        is_stale=lambda _track: False,
        rewind=rewound.append,
        skip_past=lambda _track: None,
    )
    worker._ready = old
    worker.rerender()
    assert rewound == [old] and worker._ready is None and worker._fallback is old
    assert not worker.wait_rendered(0.01)
    assert worker.wait_ready(0)  # the fallback is there for the bus
    worker._ready, worker._fallback = fresh, None
    assert worker.wait_rendered(0)
    worker._ready = None
    worker.rerender()  # nothing waiting: nothing to do
    assert rewound == [old]


# --- the source framing shim ----------------------------------------------------


@pytest.mark.parametrize("closing", [False, True])
def test_head_deadline_ignores_gone_tls_transport(closing: bool) -> None:
    transport = MagicMock()
    transport.is_closing.return_value = closing
    transport.get_protocol.side_effect = AttributeError("SSL protocol detached")
    protocol = IcecastSourceH11Protocol.__new__(IcecastSourceH11Protocol)
    protocol.transport = transport
    protocol.cycle = None
    protocol._head_deadline = MagicMock()

    protocol._head_too_slow()

    assert protocol._head_deadline is None
    if closing:
        transport.get_protocol.assert_not_called()
    transport.close.assert_not_called()


def test_head_deadline_closes_only_its_active_transport() -> None:
    transport = MagicMock()
    transport.is_closing.return_value = False
    protocol = IcecastSourceH11Protocol.__new__(IcecastSourceH11Protocol)
    protocol.transport = transport
    protocol.cycle = None
    protocol._head_deadline = MagicMock()
    transport.get_protocol.return_value = protocol

    protocol._head_too_slow()

    transport.close.assert_called_once_with()
    transport.close.reset_mock()
    transport.get_protocol.return_value = object()
    protocol._head_too_slow()
    transport.close.assert_not_called()


def test_connection_lost_cancels_head_deadline() -> None:
    protocol = IcecastSourceH11Protocol.__new__(IcecastSourceH11Protocol)
    deadline = MagicMock()
    protocol._head_deadline = deadline

    with patch("autodj.live_protocol.H11Protocol.connection_lost") as parent:
        protocol.connection_lost(None)

    deadline.cancel.assert_called_once_with()
    assert protocol._head_deadline is None
    parent.assert_called_once_with(None)


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (
            b"PUT /live HTTP/1.1\r\nHost: radio\r\nContent-Type: audio/mpeg",
            b"PUT /live HTTP/1.1\r\nHost: radio\r\nContent-Type: audio/mpeg\r\n"
            b"Transfer-Encoding: chunked",
        ),
        (
            b"SOURCE /stations/night-owl/live?x=1 ICE/1.0\r\nContent-Type: audio/ogg",
            b"SOURCE /stations/night-owl/live?x=1 HTTP/1.0\r\nContent-Type: audio/ogg\r\n"
            b"Transfer-Encoding: chunked",
        ),
        (b"PUT /live HTTP/1.1\r\nHost: radio\r\nContent-Length: 4", None),
        (b"PUT /live HTTP/1.1\r\nHost: radio\r\ntransfer-encoding : chunked", None),
        (b"PUT /live HTTP/1.1\r\nHost: radio\r\n folded", None),
        (b"GET /live HTTP/1.1\r\nHost: radio", None),
        (b"PUT /api/live HTTP/1.1\r\nHost: radio", None),
        (b"PUT /stations/a/b/live HTTP/1.1\r\nHost: radio", None),
        (b"PUT /live HTTP/2\r\nHost: radio", None),
        (b"PUT  /live HTTP/1.1\r\nHost: radio", None),
    ],
)
def test_only_a_close_delimited_source_request_is_reframed(head: bytes, expected) -> None:
    assert close_delimited_source_head(head) == expected


def test_a_hard_event_under_a_live_dj_takes_effect_when_they_hand_back(tmp_path) -> None:
    """What a hard-timed schedule event does while a DJ is live (documented in operations.md).

    A hard "play now" (a show, a song) points the render-ahead at its track
    and asks the bus to skip; the bus holds both until the DJ leaves, so the
    event's track is the first thing automation plays afterwards.  A liner
    or time check due meanwhile is not played at all.
    """
    bus, desk, tracks, started, queue = _bus(tmp_path, 0.6, 0.2)  # queue: the render-ahead
    source = _Source(0.3, seconds=1.5)
    desk.source = source
    _blocks(bus, 10)
    show = RenderedTrack(
        SimpleNamespace(path="show"), np.full((88200, 2), 0.4, np.float32), None, 0, ""
    )
    queue[:] = [show]  # Player.play_now: reset the render-ahead to the show's song...
    bus.skip()  # ...and fade out the song playing
    bus.play_liner(np.full((4410, 2), 0.5, np.float32), -6.0)  # a time check
    held = _blocks(bus, 10)
    assert np.allclose(held, 0.3, atol=1e-4) and started == [tracks[0]]
    bus.drop_held_track()
    desk.source, desk.return_source = None, source
    _blocks(bus, 8)
    assert started == [tracks[0], show]


def test_one_stations_dj_leaves_another_station_alone(tmp_path) -> None:
    day, day_desk, _day_tracks, _, _queue = _bus(tmp_path / "day", 0.6)
    night, _night_desk, night_tracks, night_started, _night_queue = _bus(tmp_path / "night", 0.2)
    day_desk.source = _Source(0.3)
    _blocks(day, 10)
    assert day.live_on_air and not night.live_on_air
    assert np.allclose(_blocks(night, 5), 0.2)
    assert night_started == [night_tracks[0]]
