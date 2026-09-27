"""Player in stream mode: the bus exists up front and the station starts sets."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import numpy as np

from autodj.mixbus import MixBus, RenderedTrack
from autodj.player import Player
from tests.unit.test_player import _make_cfg_mock, _make_sim_index

WAIT = 5.0


def _player(**kwargs) -> Player:
    return Player(_make_cfg_mock(), _make_sim_index(6), **kwargs)


def test_stream_mode_builds_the_bus_up_front() -> None:
    player = _player(stream_mode=True)
    assert isinstance(player.bus, MixBus)
    assert not player.bus.playing
    events = player.bus._events
    assert events.on_need_track == player._take_render
    assert events.on_track_start == player._on_track_start
    assert events.on_position == player._on_position


def test_without_stream_mode_there_is_no_bus_until_run() -> None:
    assert _player().bus is None


def test_random_start_entry_picks_from_the_library() -> None:
    player = _player()
    assert player._random_start_entry() in player._sim.entries


def test_random_start_entry_is_none_for_an_empty_library() -> None:
    player = _player()
    player._sim = MagicMock()
    player._sim.entries_snapshot.return_value = ()
    assert player._random_start_entry() is None


def test_begin_set_renders_from_the_entry_start_and_unpauses() -> None:
    player = _player(stream_mode=True)
    entry = player._sim.entries[3]
    player._state.is_paused = True
    player.begin_set(entry, "queue")
    assert player._seed_path == entry.path
    assert not player._state.is_paused
    assert player._pending_entry is entry
    assert player._pending_offset == 0
    assert player._pending_pick_mode == "queue"


def test_end_set_parks_the_worker_and_clears_now_playing() -> None:
    player = _player(stream_mode=True)
    entry = player._sim.entries[2]
    player._state.current_track = entry
    player._state.next_track = player._sim.entries[3]
    player._playing_render = MagicMock()
    player._playback_pos[0] = 1234
    player._playback_len = 5678
    player._current_lyrics = ["line"]
    player._current_lyrics_plain = "line"
    player.begin_set(entry, "seed")
    player.end_set()
    assert player._pending_entry is None
    assert player._state.current_track is None
    assert player._state.next_track is None
    assert player._playing_render is None
    assert player._playback_pos[0] == 0
    assert player._playback_len == 0
    assert player._current_lyrics == []
    assert player._current_lyrics_plain == ""


def test_parked_worker_renders_nothing() -> None:
    player = _player(stream_mode=True)
    player._render_track = MagicMock(  # type: ignore[method-assign]
        side_effect=AssertionError("must not render while parked")
    )
    player._render_ahead._retry_seconds = 0.01
    player.reset_render_ahead(None)
    player._render_ahead.start()
    try:
        assert not player._render_ahead.wait_ready(0.1)
    finally:
        player._render_ahead.stop(timeout=WAIT)
    player._render_track.assert_not_called()


def _run_stream_in_thread(player: Player) -> threading.Thread:
    player._ensure_dj_cache = MagicMock()  # type: ignore[method-assign]
    player._ensure_external_cues = MagicMock()  # type: ignore[method-assign]
    thread = threading.Thread(target=player._run_stream, daemon=True)
    thread.start()
    return thread


def test_run_stream_runs_the_bus_idle_until_stopped() -> None:
    player = _player(stream_mode=True)
    written: list[np.ndarray] = []
    got_block = threading.Event()

    class Sink:
        def write(self, block: np.ndarray) -> None:
            written.append(block)
            got_block.set()

        def close(self) -> None:
            pass

    player.bus.add_output(Sink())
    with patch("autodj.sound_output.SoundDeviceOutput") as sound:
        thread = _run_stream_in_thread(player)
        assert got_block.wait(WAIT)
        assert not player.bus.playing  # idle: the station starts sets
        assert not np.any(written[0])
        player._state.should_stop = True
        thread.join(WAIT)
    assert not thread.is_alive()
    sound.assert_not_called()
    player._ensure_external_cues.assert_called_once()


def test_run_stream_mirrors_pause_onto_the_bus() -> None:
    player = _player(stream_mode=True)
    with patch("autodj.sound_output.SoundDeviceOutput"):
        thread = _run_stream_in_thread(player)
        try:
            player._state.is_paused = True
            for _ in range(100):
                if player.bus._paused:
                    break
                threading.Event().wait(0.02)
            assert player.bus._paused
        finally:
            player._state.should_stop = True
            thread.join(WAIT)
    assert not thread.is_alive()


def test_run_stream_with_server_audio_adds_and_closes_the_sound_card() -> None:
    player = _player(stream_mode=True, server_audio_too=True)
    output = MagicMock()
    with patch("autodj.sound_output.SoundDeviceOutput", return_value=output) as sound:
        thread = _run_stream_in_thread(player)
        for _ in range(100):
            if output.write.called:
                break
            threading.Event().wait(0.02)
        player._state.should_stop = True
        thread.join(WAIT)
    assert not thread.is_alive()
    sound.assert_called_once()
    assert output.write.called
    output.close.assert_called_once()
    assert output not in player.bus._outputs


def test_run_stream_renders_ahead_once_a_set_begins() -> None:
    player = _player(stream_mode=True)
    player._render_track = lambda cur, nxt, off: RenderedTrack(  # type: ignore[method-assign]
        cur, np.zeros((MixBus.BLOCK * 4, 2), np.float32), nxt, 0, "", start_offset=off
    )
    with patch("autodj.sound_output.SoundDeviceOutput"):
        thread = _run_stream_in_thread(player)
        try:
            player.begin_set(player._sim.entries[0], "seed")
            assert player._render_ahead.wait_ready(WAIT)
        finally:
            player._state.should_stop = True
            thread.join(WAIT)
    assert not thread.is_alive()


def test_run_stream_starts_a_fresh_m3u_export(tmp_path) -> None:
    export = tmp_path / "live.m3u"
    export.write_text("stale\n", encoding="utf-8")
    player = _player(stream_mode=True, export_m3u=export)
    with patch("autodj.sound_output.SoundDeviceOutput"):
        thread = _run_stream_in_thread(player)
        player._state.should_stop = True
        thread.join(WAIT)
    assert not thread.is_alive()
    assert export.read_text(encoding="utf-8") == "#EXTM3U\n"
