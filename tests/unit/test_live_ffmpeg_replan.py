from __future__ import annotations

import shutil
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from autodj._bridge import PlayerBridge
from autodj.config import PlaybackConfig
from autodj.live_audio import StreamDecoder
from autodj.player import Player
from autodj.stereo import SAMPLE_RATE
from tests.unit._fakes import make_cfg_mock, make_sim_index

WAIT = 20.0


def _wav(path: Path, frequency: float, seconds: float = 3.0) -> None:
    count = round(seconds * SAMPLE_RATE)
    time_axis = np.arange(count, dtype=np.float64) / SAMPLE_RATE
    signal = (0.12 * np.sin(2 * np.pi * frequency * time_axis) * 32767).astype("<i2")
    stereo = np.column_stack((signal, signal))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(stereo.tobytes())


def _dominant_energy(block: np.ndarray, center_hz: float) -> float:
    spectrum = np.abs(np.fft.rfft(block[:, 0] * np.hanning(len(block))))
    freqs = np.fft.rfftfreq(len(block), 1 / SAMPLE_RATE)
    return float(spectrum[np.abs(freqs - center_hz) <= 100].sum())


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs FFmpeg")
def test_real_ffmpeg_player_replaces_unheard_incoming_and_closes_decks(tmp_path, monkeypatch):
    files = [tmp_path / f"{name}.wav" for name in ("A", "B", "C")]
    for path, frequency in zip(files, (220, 660, 440), strict=True):
        _wav(path, frequency)

    sim = make_sim_index(3)
    for entry, path in zip(sim.entries, files, strict=True):
        entry.path = str(path)
        entry.length = 3.0
    # Rebuild its frozen public snapshot after changing the synthetic entries.
    sim = type(sim)(sim.faiss_index, sim.entries)
    cfg = make_cfg_mock()
    cfg.playback = PlaybackConfig(
        transition_mode="saved_points",
        crossfade_seconds=0.5,
        loudness_processing="off",
        pick_mode="random",
        liners_duck_db=0.0,
    )
    player = Player(cfg, sim, stream_mode=True)
    a, b, c = player._sim.entries
    points = {
        entry.path: {"start": 0.0, "outro": 2.3, "segue": 2.5, "intro": 0.0, "end": 3.0}
        for entry in player._sim.entries
    }
    player.saved_points = lambda entry: dict(points[entry.path])
    player._state.queue.append(b)
    started: list[str] = []
    player.on_track_started = lambda entry: started.append(entry.path)

    decoders = []
    original_decoder = StreamDecoder

    def track_decoder(*args, **kwargs):
        decoder = original_decoder(*args, **kwargs)
        decoders.append(decoder)
        return decoder

    monkeypatch.setattr("autodj.live_audio.StreamDecoder", track_decoder)
    decks = []
    original_live_file = player._live_file

    def track_deck(entry):
        deck = original_live_file(entry)
        if deck not in decks:
            decks.append(deck)
        return deck

    player._live_file = track_deck

    player.begin_set(a, "seed")
    player._render_ahead.start()
    player.bus.start_set()
    try:
        deadline = time.monotonic() + WAIT
        while player._playing_render is None and time.monotonic() < deadline:
            if player.bus._current is None:
                player._render_ahead.wait_ready(0.05)
            player.bus.render_block()
            time.sleep(0.002)
        assert player._playing_render is not None
        original_handoff = player._playing_render
        assert original_handoff.entry.path == a.path
        assert original_handoff.next_entry.path == b.path
        assert not player.bus.incoming_started(original_handoff)
        held_position = player.bus.live_position(original_handoff)

        bridge = PlayerBridge(player=player, sim=player._sim)
        assert bridge.play_next_entry(c)
        assert player.bus._handoff_hold is not None

        deadline = time.monotonic() + WAIT
        while player._playing_render is original_handoff and time.monotonic() < deadline:
            player.bus.render_block()
            time.sleep(0.002)
        changed_handoff = player._playing_render
        assert changed_handoff is not None
        assert changed_handoff.entry.path == a.path
        assert changed_handoff.next_entry.path == c.path
        changed_position = player.bus.live_position(changed_handoff)
        assert changed_position >= held_position
        old_prefix = original_handoff.audio[:changed_position]
        new_prefix = changed_handoff.audio[:changed_position]
        assert [
            (layer.source, layer.start, layer.length, layer.offset) for layer in new_prefix.layers
        ] == [
            (layer.source, layer.start, layer.length, layer.offset) for layer in old_prefix.layers
        ]
        assert not player.bus.incoming_started(changed_handoff)

        deadline = time.monotonic() + WAIT
        while c.path not in started and time.monotonic() < deadline:
            if player.bus._current is None:
                player._render_ahead.wait_ready(0.05)
            player.bus.render_block()
            time.sleep(0.002)
        assert started[0] == a.path
        assert c.path in started
        assert b.path not in started

        # The first full block after C's start has C's 440 Hz signature;
        # a stale B render would carry its distinct 660 Hz signature.
        c_block = player.bus.render_block()
        assert _dominant_energy(c_block, 440) > 2 * _dominant_energy(c_block, 660)
    finally:
        player.bus.stop_set()
        player.end_set()
        assert player.wait_for_background_analysis(WAIT)

    assert all(not deck._worker.is_alive() for deck in decks)
    assert all(
        decoder._process is None or decoder._process.poll() is not None for decoder in decoders
    )
    assert all(
        thread is None or not thread.is_alive()
        for decoder in decoders
        for thread in (decoder._producer, decoder._stderr_reader)
    )
