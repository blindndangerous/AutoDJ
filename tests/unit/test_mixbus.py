"""Mix bus behaviour with a fake clock and fake outputs."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj.mixbus import BusEvents, MixBus, RenderedTrack


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class Recorder:
    def __init__(self) -> None:
        self.blocks: list[np.ndarray] = []
        self.closed = False

    def write(self, block: np.ndarray) -> None:
        self.blocks.append(block.copy())

    def close(self) -> None:
        self.closed = True


def _track(value: float, frames: int, name: str = "t") -> RenderedTrack:
    entry = MagicMock()
    entry.path = name
    return RenderedTrack(entry, np.full((frames, 2), value, np.float32), None, 0, "")


def _bus(tracks: list[RenderedTrack], gains=(1.0, 1.0, 1.0)) -> tuple[MixBus, list, list]:
    started: list[RenderedTrack] = []
    positions: list[int] = []
    queue = list(tracks)
    events = BusEvents(
        on_track_start=started.append,
        on_position=positions.append,
        on_need_track=lambda: queue.pop(0) if queue else None,
    )
    bus = MixBus(events, eq_gains=lambda: gains, clock=FakeClock())
    return bus, started, positions


def test_idle_bus_emits_silence() -> None:
    bus, started, _ = _bus([_track(0.5, 2000)])
    assert not bus.playing
    np.testing.assert_array_equal(bus.render_block(), np.zeros((882, 2), np.float32))
    assert started == []


def test_plays_tracks_back_to_back_and_reports_starts() -> None:
    first, second = _track(0.5, 1000, "a"), _track(0.25, 1000, "b")
    bus, started, positions = _bus([first, second])
    bus.start_set()
    b1 = bus.render_block()
    b2 = bus.render_block()
    assert started == [first, second]
    np.testing.assert_allclose(b1[:, 0], 0.5)
    np.testing.assert_allclose(b2[: 1000 - 882, 0], 0.5)
    np.testing.assert_allclose(b2[1000 - 882 :, 0], 0.25)
    assert positions[0] == 882


def test_pause_emits_silence_and_holds_position() -> None:
    bus, _, positions = _bus([_track(0.5, 5000)])
    bus.start_set()
    bus.render_block()
    bus.pause(True)
    np.testing.assert_array_equal(bus.render_block(), np.zeros((882, 2), np.float32))
    bus.pause(False)
    bus.render_block()
    assert positions == [882, 1764]


def test_skip_fades_then_moves_on() -> None:
    first, second = _track(1.0, 100_000, "a"), _track(0.5, 100_000, "b")
    bus, started, _ = _bus([first, second])
    bus.start_set()
    bus.render_block()
    bus.skip()
    fade_blocks = [bus.render_block() for _ in range(10)]
    assert fade_blocks[0][0, 0] <= 1.0
    assert started[-1] is second
    assert any(np.allclose(b[:, 0], 0.5) for b in fade_blocks)


def test_eq_gains_are_applied() -> None:
    bus, _, _ = _bus([_track(0.5, 5000)], gains=(0.0, 0.0, 0.0))
    bus.start_set()
    bus.render_block()
    assert np.max(np.abs(bus.render_block())) < 0.05


def test_liner_ducks_music_and_is_mixed_in() -> None:
    bus, _, _ = _bus([_track(0.5, 50_000)])
    bus.start_set()
    bus.play_liner(np.full((882 * 3, 2), 0.2, np.float32), duck_db=-12.0)
    block = bus.render_block()
    duck = 10 ** (-12.0 / 20.0)
    assert block[-1, 0] == pytest.approx(0.5 * duck + 0.2, abs=0.02)


def test_stop_set_drops_tracks_and_goes_silent() -> None:
    bus, _, _ = _bus([_track(0.5, 50_000), _track(0.5, 50_000)])
    bus.start_set()
    bus.render_block()
    bus.stop_set()
    assert not bus.playing
    np.testing.assert_array_equal(bus.render_block(), np.zeros((882, 2), np.float32))


def test_no_next_track_means_silence_not_stall() -> None:
    bus, _, _ = _bus([_track(0.5, 100)])
    bus.start_set()
    bus.render_block()
    np.testing.assert_array_equal(bus.render_block(), np.zeros((882, 2), np.float32))


def test_run_paces_by_clock_and_isolates_failing_output() -> None:
    bus, _, _ = _bus([_track(0.5, 882 * 10)])
    good, bad = Recorder(), MagicMock()
    bad.write.side_effect = RuntimeError("gone")
    bus.add_output(good)
    bus.add_output(bad)
    bus.start_set()
    stop = threading.Event()
    clock: FakeClock = bus._clock  # type: ignore[assignment]
    original_sleep = clock.sleep

    def sleep_and_maybe_stop(seconds: float) -> None:
        original_sleep(seconds)
        if len(good.blocks) >= 5:
            stop.set()

    clock.sleep = sleep_and_maybe_stop  # type: ignore[method-assign]
    bus.run(stop)
    assert len(good.blocks) >= 5
    assert bad not in bus._outputs
    assert clock.t == pytest.approx(len(good.blocks) * 0.02, abs=0.021)
