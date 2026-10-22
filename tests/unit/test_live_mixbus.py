from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np

from autodj.live import PcmBuffer, TalkoverSettings
from autodj.live_audio import AudioLayer, BufferedFile, LiveAudio
from autodj.mixbus import BusEvents, MixBus, RenderedTrack
from autodj.stereo import SAMPLE_RATE
from tests.unit._fakes import FakeClock


class _Buffered(BufferedFile):
    """A counted buffered deck for bus tests; no decoder thread required."""

    def __init__(self, value: float, frames: int, *, available: bool) -> None:
        self.value = value
        self.frames = frames
        self.available = available
        self.read_calls = 0
        self.ready_calls = 0

    def __len__(self) -> int:
        return self.frames

    def ready(self, position: int, frames: int) -> bool:
        self.ready_calls += 1
        return self.available

    def read(self, position: int, frames: int) -> np.ndarray:
        self.read_calls += 1
        return np.full((frames, 2), self.value, dtype=np.float32)


def _track(audio, *, overlap: int = 0, name: str = "track") -> RenderedTrack:
    entry = MagicMock()
    entry.path = name
    return RenderedTrack(entry, audio, None, 0, "", overlap_frames=overlap)


def _bus(track: RenderedTrack) -> tuple[MixBus, list[int]]:
    positions: list[int] = []
    events = BusEvents(
        on_track_start=lambda _track: None,
        on_position=positions.append,
        on_need_track=lambda: track,
    )
    return MixBus(events, eq_gains=lambda: (1.0, 1.0, 1.0), clock=FakeClock()), positions


def test_pending_live_layer_holds_cursor_and_does_not_consume_ready_companion() -> None:
    """ADR-0004-09: unavailable live layers hold the shared sample cursor."""
    block = MixBus.BLOCK
    first = _Buffered(0.1, block * 3, available=True)
    pending = _Buffered(0.2, block * 3, available=False)
    audio = LiveAudio(
        block * 3,
        (AudioLayer(first, 0, block * 3), AudioLayer(pending, 0, block * 3)),
    )
    track = _track(audio)
    bus, positions = _bus(track)
    bus.start_set()

    np.testing.assert_array_equal(bus.render_block(), np.zeros((block, 2), np.float32))
    assert bus.live_position(track) == 0
    assert positions == []
    assert first.ready_calls == pending.ready_calls == 1
    assert first.read_calls == pending.read_calls == 0

    pending.available = True
    np.testing.assert_allclose(bus.render_block(), 0.3)
    assert bus.live_position(track) == block
    assert first.read_calls == pending.read_calls == 1


def test_failed_live_decode_does_not_announce_unheard_track() -> None:
    class Failed(_Buffered):
        def ready(self, position: int, frames: int) -> bool:
            raise RuntimeError("decoder failed")

    source = Failed(0.2, MixBus.BLOCK * 3, available=False)
    track = _track(LiveAudio(len(source), (AudioLayer(source, 0, len(source)),)))
    started, positions = [], []
    waiting = iter((track, None))
    bus = MixBus(
        BusEvents(started.append, positions.append, lambda: next(waiting)),
        eq_gains=lambda: (1, 1, 1),
        clock=FakeClock(),
    )
    bus.start_set()
    assert not bus.render_block().any()
    assert started == positions == []
    assert bus.current_render() is None


def test_held_handoff_never_airs_displaced_incoming_and_atomic_replace_airs_new() -> None:
    """ADR-0004-09: a held live handoff never airs its displaced incoming track."""
    block = MixBus.BLOCK
    boundary = block * 3
    old_audio = np.full((block * 4, 2), 0.8, dtype=np.float32)
    old = _track(old_audio, overlap=block, name="old")
    bus, _ = _bus(old)
    bus.start_set()
    np.testing.assert_allclose(bus.render_block(), 0.8)
    bus.hold_handoff(old)

    for _ in range(2):
        np.testing.assert_allclose(bus.render_block(), 0.8)
    assert bus.live_position(old) == boundary
    np.testing.assert_array_equal(bus.render_block(), np.zeros((block, 2), np.float32))
    assert bus.live_position(old) == boundary

    replacement_audio = np.full((block * 5, 2), 0.35, dtype=np.float32)
    replacement = _track(replacement_audio, overlap=block, name="replacement")
    assert bus.replace_live_plan(old, replacement)
    np.testing.assert_allclose(bus.render_block(), 0.35)
    assert bus.live_position(replacement) == boundary + block


def test_incoming_started_uses_bus_sample_cursor_and_strict_boundary() -> None:
    block = MixBus.BLOCK
    track = _track(np.zeros((block * 6, 2), np.float32), overlap=block * 2)
    bus, _ = _bus(track)
    bus.start_set()
    for _ in range(4):
        bus.render_block()
    boundary = len(track.audio) - track.overlap_frames
    assert bus.live_position(track) == boundary
    assert bus.incoming_started(track) is False

    # The predicate follows the actual sample cursor even before the next
    # position callback could report it.
    bus._pos += 1
    assert bus.incoming_started(track) is True


def test_live_mic_and_liner_mix_while_music_layers_are_buffered() -> None:
    block = MixBus.BLOCK
    music = _Buffered(0.2, block * 4, available=True)
    audio = LiveAudio(block * 4, (AudioLayer(music, 0, block * 4),))
    track = _track(audio)
    bus, _ = _bus(track)
    mic = PcmBuffer(1.0)
    mic.push_frames(np.full((int(0.1 * SAMPLE_RATE), 2), 0.3, np.float32))
    bus.set_live(
        SimpleNamespace(
            active=False,
            source=None,
            return_source=None,
            mic=mic,
            settings=TalkoverSettings(mic_return_seconds=0.5, mic_duck_db=0),
            crossfade_seconds=1.0,
            attack_seconds=0.04,
        )
    )
    bus.start_set()
    bus.play_liner(np.full((block * 2, 2), 0.1, np.float32), duck_db=0)

    output = bus.render_block()
    np.testing.assert_allclose(output, 0.6, atol=1e-6)
    assert music.read_calls == 1
    assert bus.live_position(track) == block
