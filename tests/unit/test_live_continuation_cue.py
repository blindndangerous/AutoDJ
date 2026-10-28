"""A live continuation's deck is decoding where it will play before the bus needs it."""

from __future__ import annotations

import shutil
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from autodj.config import PlaybackConfig
from autodj.live_audio import BufferedFile, LiveAudio
from autodj.mixbus import RenderedTrack
from autodj.player import Player
from autodj.stereo import SAMPLE_RATE
from tests.unit._fakes import make_cfg_mock, make_sim_index

WAIT = 20.0
SECONDS = 8.0


def _wav(path: Path, frequency: float) -> None:
    count = round(SECONDS * SAMPLE_RATE)
    time_axis = np.arange(count, dtype=np.float64) / SAMPLE_RATE
    signal = (0.12 * np.sin(2 * np.pi * frequency * time_axis) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(np.column_stack((signal, signal)).tobytes())


def _first_deck_layer(audio: LiveAudio):
    return next(layer for layer in audio.layers if isinstance(layer.source, BufferedFile))


def _continuation_while_a_plays(tmp_path: Path, mode: str) -> tuple[Player, RenderedTrack]:
    files = [tmp_path / f"{name}.wav" for name in ("A", "B", "C")]
    for path, frequency in zip(files, (220, 660, 440), strict=True):
        _wav(path, frequency)
    sim = make_sim_index(3)
    for entry, path in zip(sim.entries, files, strict=True):
        entry.path = str(path)
        entry.length = SECONDS
    sim = type(sim)(sim.faiss_index, sim.entries)
    cfg = make_cfg_mock()
    cfg.replaygain.enabled = False
    cfg.playback = PlaybackConfig(
        transition_mode=mode,
        crossfade_seconds=1.0,
        loudness_processing="off",
        pick_mode="random",
        liners_duck_db=0.0,
    )
    player = Player(cfg, sim, stream_mode=True)
    a, b, _c = player._sim.entries
    points = {
        entry.path: {"start": 0.25, "intro": 1.0, "segue": SECONDS - 1.0, "end": SECONDS}
        for entry in player._sim.entries
    }
    player.saved_points = lambda entry: dict(points[entry.path])
    player._state.queue.append(b)
    player.begin_set(a, "seed")
    player._playout.start()
    player.bus.start_set()
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        if player.bus._current is None:
            player._playout.wait_ready(0.05)
        player.bus.render_block()
        ready = player._playout.ready()
        if (
            player.bus.current_render() is not None
            and player.bus.current_render().entry is a
            and ready is not None
            and ready.handoff_pending
        ):
            return player, ready
        time.sleep(0.002)
    raise AssertionError("B's continuation was not prepared while A played")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs FFmpeg")
@pytest.mark.parametrize("mode", ["full_intro_outro", "saved_points"])
def test_continuation_deck_is_cued_before_promotion(tmp_path: Path, mode: str) -> None:
    """ADR-0004-12: promotion never waits for the incoming deck to reseek.

    The incoming deck opens at the song's saved start.  When the overlap came
    from decoded windows rather than that deck, the continuation reads it
    further in; unless the deck is moved there while A still plays, the bus
    holds the programme in exact silence at the song change while FFmpeg
    restarts (seconds on a slow host).
    """
    player, continuation = _continuation_while_a_plays(tmp_path, mode)
    try:
        assert isinstance(continuation.audio, LiveAudio)
        layer = _first_deck_layer(continuation.audio)
        deck = layer.source
        with deck._condition:
            target = deck._requested if deck._requested is not None else deck._position
        if mode == "saved_points":
            # A's overlap reads this deck itself and leaves it at the body.
            playing = player.bus.current_render()
            assert any(item.source is deck for item in playing.audio.layers)
        else:
            assert target == layer.offset
    finally:
        player.bus.stop_set()
        player.end_set()
        assert player.wait_for_background_analysis(WAIT)
