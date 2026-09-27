"""Mix bus behaviour with a fake clock and fake outputs."""

from __future__ import annotations

import threading
from itertools import pairwise
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj.mixbus import BusEvents, MixBus, RenderedTrack, SystemClock
from tests.unit._fakes import FakeClock


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

    # The level must fall smoothly across the fade rather than staying flat
    # and then cutting.
    first_samples = [float(b[0, 0]) for b in fade_blocks]
    fading = [v for v in first_samples if v > 0.5 + 1e-6]
    assert len(fading) >= 2
    assert all(a > b for a, b in pairwise(fading))

    # No block may contain a long run of near-silence: once the fade
    # completes the very next samples must belong to the next track, with
    # no gap of zeros in between.
    for block in fade_blocks:
        near_zero = int(np.sum(np.abs(block[:, 0]) < 1e-3))
        assert near_zero < 20, "skip fade must not leave a run of near-silence"


def test_remove_output_ignores_unknown_output() -> None:
    bus, _, _ = _bus([_track(0.5, 100)])
    unknown = Recorder()
    bus.remove_output(unknown)  # must not raise
    assert unknown not in bus._outputs


def test_skip_is_a_noop_without_a_current_track_or_already_fading() -> None:
    bus, _, _ = _bus([_track(0.5, 100_000)])
    bus.skip()  # no current track yet; must not raise or arm a fade
    assert bus._skip_fade == 0
    bus.start_set()
    bus.render_block()
    bus.skip()
    fade_after_first_skip = bus._skip_fade
    bus.skip()  # already fading; a second call must not restart it
    assert bus._skip_fade == fade_after_first_skip


def test_eq_gains_are_applied() -> None:
    bus, _, _ = _bus([_track(0.5, 5000)], gains=(0.0, 0.0, 0.0))
    bus.start_set()
    bus.render_block()
    assert np.max(np.abs(bus.render_block())) < 0.05


def test_liner_ducks_music_and_is_mixed_in() -> None:
    bus, _, _ = _bus([_track(0.5, 50_000)])
    bus.start_set()
    bus.play_liner(np.full((882 * 3, 2), 0.2, np.float32), duck_db=-12.0)
    for _ in range(2):
        bus.render_block()
    block = bus.render_block()
    duck = 10 ** (-12.0 / 20.0)
    assert block[-1, 0] == pytest.approx(0.5 * duck + 0.2, abs=0.02)


def test_liner_duck_ramps_rather_than_steps() -> None:
    bus, _, _ = _bus([_track(0.5, 50_000)])
    bus.start_set()
    bus.play_liner(np.zeros((882 * 3, 2), np.float32), duck_db=-12.0)
    first = bus.render_block()
    duck_target = 10 ** (-12.0 / 20.0)
    # At the very start of the block the duck has barely moved...
    assert first[0, 0] == pytest.approx(0.5, abs=0.02)
    # ...and by the end of the block (half of the 40 ms ramp) it is well
    # below unity but not yet at the fully-ducked target: a ramp, not a step.
    assert first[-1, 0] > 0.5 * duck_target + 0.05
    assert first[-1, 0] < first[0, 0]


def test_music_returns_to_full_level_after_liner_ends() -> None:
    bus, _, _ = _bus([_track(0.5, 50_000)])
    bus.start_set()
    bus.play_liner(np.zeros((882, 2), np.float32), duck_db=-12.0)
    bus.render_block()  # the liner plays out and ends within this one block
    block = bus.render_block()
    for _ in range(2):
        block = bus.render_block()
    np.testing.assert_allclose(block[:, 0], 0.5, atol=0.01)


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


def test_run_recovers_from_render_block_exception() -> None:
    queue = [_track(0.5, 882 * 10)]
    started: list[RenderedTrack] = []
    positions: list[int] = []
    calls = {"n": 0}

    def gains() -> tuple[float, float, float]:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return (1.0, 1.0, 1.0)

    events = BusEvents(
        on_track_start=started.append,
        on_position=positions.append,
        on_need_track=lambda: queue.pop(0) if queue else None,
    )
    bus = MixBus(events, eq_gains=gains, clock=FakeClock())
    good = Recorder()
    bus.add_output(good)
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
    # The second render_block() call's eq_gains() raised; run() must
    # substitute silence for that period and keep going rather than dying.
    np.testing.assert_array_equal(good.blocks[1], np.zeros((882, 2), np.float32))
    np.testing.assert_array_equal(good.blocks[0], np.full((882, 2), 0.5, np.float32))


def test_run_recovers_schedule_after_output_stalls_clock() -> None:
    bus, _, _ = _bus([_track(0.5, 882 * 20)])
    clock: FakeClock = bus._clock  # type: ignore[assignment]
    good = Recorder()
    stalled = {"done": False}

    def maybe_stall(block: np.ndarray) -> None:
        good.write(block)
        if not stalled["done"]:
            stalled["done"] = True
            clock.t += 2.0

    stalling = MagicMock()
    stalling.write.side_effect = maybe_stall
    bus.add_output(stalling)
    bus.start_set()
    stop = threading.Event()
    original_sleep = clock.sleep

    def sleep_and_maybe_stop(seconds: float) -> None:
        original_sleep(seconds)
        if len(good.blocks) >= 5:
            stop.set()

    clock.sleep = sleep_and_maybe_stop  # type: ignore[method-assign]
    bus.run(stop)
    assert len(good.blocks) >= 5
    # The schedule must reset after the stall instead of trying to "catch
    # up": no burst of dozens of unpaced blocks before pacing resumes.
    assert len(good.blocks) <= 8


def test_run_skips_sleep_when_slightly_behind_schedule() -> None:
    bus, _, _ = _bus([_track(0.5, 882 * 5)])
    clock: FakeClock = bus._clock  # type: ignore[assignment]
    good = Recorder()
    stop = threading.Event()

    def slow_write(block: np.ndarray) -> None:
        good.write(block)
        clock.t += 0.5  # behind schedule, but under the 1s catch-up threshold
        stop.set()

    slow = MagicMock()
    slow.write.side_effect = slow_write
    bus.add_output(slow)
    bus.start_set()
    bus.run(stop)
    assert len(good.blocks) == 1
    # Neither the normal-pacing sleep nor the catch-up reset should fire.
    assert clock.slept == []


def test_system_clock_now_is_monotonic_and_sleep_returns() -> None:
    clock = SystemClock()
    first = clock.now()
    clock.sleep(0)
    second = clock.now()
    assert second >= first


def test_seek_clamps_within_track() -> None:
    first = _track(0.5, 10_000, "a")
    bus, _, positions = _bus([first, _track(0.25, 10_000, "b")])
    bus.seek(500)  # nothing playing yet: ignored
    assert bus._pos == 0
    bus.start_set()
    bus.render_block()
    bus.seek(5_000)
    assert bus._pos == 5_000
    bus.seek(-10)
    assert bus._pos == 0
    bus.seek(50_000)
    assert bus._pos == 9_999
    assert bus._current is first
    bus.seek(2_000)
    bus.render_block()
    assert positions[-1] == 2_000 + 882
