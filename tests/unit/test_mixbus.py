"""Mix bus behaviour with a fake clock and fake outputs."""

from __future__ import annotations

import threading
from itertools import pairwise
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj.mixbus import BusEvents, LinerCue, MixBus, RenderedTrack, SkipTail, SystemClock
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


def test_start_set_drops_a_liner_left_waiting_while_idle() -> None:
    # Fired while idle, the liner never rendered; the next set must not open with it.
    bus, _, _ = _bus([_track(0.5, 50_000)])
    bus.play_liner(np.full((882 * 50, 2), 0.2, np.float32), duck_db=-12.0)
    bus.render_block()  # idle: returns silence without touching the liner
    bus.start_set()
    np.testing.assert_allclose(bus.render_block()[:, 0], 0.5, atol=1e-6)


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


# ---------------------------------------------------------------------------
# DJ-style skips: an effect tail made off the bus thread
# ---------------------------------------------------------------------------


def _ramp_track(frames: int, name: str) -> RenderedTrack:
    """A track whose sample values are their own frame numbers / 1e6."""
    entry = MagicMock()
    entry.path = name
    values = (np.arange(frames, dtype=np.float32) / 1e6)[:, None]
    return RenderedTrack(entry, np.repeat(values, 2, axis=1), None, 0, "")


def _tail_bus(tracks, maker) -> tuple[MixBus, list]:
    started: list[RenderedTrack] = []
    queue = list(tracks)
    events = BusEvents(
        on_track_start=started.append,
        on_position=lambda _pos: None,
        on_need_track=lambda: queue.pop(0) if queue else None,
    )
    return MixBus(events, eq_gains=lambda: (1.0, 1.0, 1.0), skip_tail=maker), started


def test_skip_plays_the_effect_then_the_next_track_in_the_same_block() -> None:
    first, second = _track(0.2, 100_000, "a"), _track(0.5, 100_000, "b")
    effect = np.full((1000, 2), 0.9, np.float32)
    bus, started = _tail_bus([first, second], lambda track, pos: SkipTail(track, pos + 400, effect))
    bus.start_set()
    bus.render_block()
    bus.skip()
    block = bus.render_block()
    np.testing.assert_allclose(block[:400, 0], 0.2)  # dry until the effect starts
    np.testing.assert_allclose(block[400:, 0], 0.9)
    block = bus.render_block()
    rest = 1000 - (882 - 400)
    np.testing.assert_allclose(block[:rest, 0], 0.9)
    np.testing.assert_allclose(block[rest:, 0], 0.5)  # the next track, no gap
    assert started == [first, second]


def test_the_effect_is_made_on_the_skipping_thread_never_the_bus_thread() -> None:
    first, second = _track(0.2, 400_000, "a"), _track(0.5, 400_000, "b")
    made_on: list[int] = []

    def maker(track, pos):
        made_on.append(threading.get_ident())
        return SkipTail(track, pos, np.zeros((2000, 2), np.float32))

    bus, _started = _tail_bus([first, second], maker)
    bus.start_set()
    stop = threading.Event()
    runner = threading.Thread(target=bus.run, args=(stop,))
    runner.start()
    try:
        bus.skip()
    finally:
        stop.set()
        runner.join(5.0)
    assert made_on == [threading.get_ident()]
    assert runner.ident not in made_on


def test_rendering_blocks_never_makes_an_effect() -> None:
    maker = MagicMock(side_effect=AssertionError("made on the bus thread"))
    bus, _ = _tail_bus([_track(0.2, 50_000), _track(0.1, 50_000)], maker)
    bus.start_set()
    for _ in range(120):
        bus.render_block()
    maker.assert_not_called()


def test_an_effect_installed_late_takes_over_from_where_the_bus_is() -> None:
    first = _ramp_track(200_000, "a")
    holder: dict[str, MixBus] = {}

    def maker(track, pos):
        # The bus plays two more blocks while the effect is being made.
        holder["bus"].render_block()
        holder["bus"].render_block()
        return SkipTail(track, pos, np.full((5000, 2), 0.7, np.float32))

    bus, _ = _tail_bus([first, _track(0.1, 50_000)], maker)
    holder["bus"] = bus
    bus.start_set()
    bus.render_block()
    bus.skip()
    block = bus.render_block()
    # It starts from the dry audio 3 blocks in and crosses to the effect.
    assert block[0, 0] == pytest.approx(3 * 882 / 1e6, abs=1e-6)
    np.testing.assert_allclose(block[441:, 0], 0.7)


def test_an_effect_that_is_too_late_falls_back_to_the_fade() -> None:
    first = _track(1.0, 200_000, "a")
    holder: dict[str, MixBus] = {}

    def maker(track, pos):
        for _ in range(3):
            holder["bus"].render_block()
        return SkipTail(track, pos, np.full((100, 2), 0.7, np.float32))

    bus, _ = _tail_bus([first, _track(0.1, 50_000)], maker)
    holder["bus"] = bus
    bus.start_set()
    bus.render_block()
    bus.skip()
    assert bus._tail is None
    assert bus._skip_fade > 0


@pytest.mark.parametrize("outcome", ["none", "raises", "other_track"])
def test_skip_falls_back_to_the_fade(outcome: str) -> None:
    first, second = _track(1.0, 100_000, "a"), _track(0.5, 100_000, "b")

    def maker(track, pos):
        if outcome == "raises":
            raise RuntimeError("boom")
        if outcome == "other_track":
            return SkipTail(second, pos, np.zeros((100, 2), np.float32))
        return None

    bus, _ = _tail_bus([first, second], maker)
    bus.start_set()
    bus.render_block()
    bus.skip()
    assert bus._skip_fade > 0
    block = bus.render_block()
    assert block[0, 0] > block[-1, 0]  # the 150 ms fade


def test_skip_during_an_effect_and_seek_are_ignored() -> None:
    first = _track(0.2, 100_000, "a")
    maker = MagicMock(
        side_effect=lambda track, pos: SkipTail(track, pos, np.ones((9000, 2), np.float32))
    )
    bus, _ = _tail_bus([first, _track(0.5, 1000)], maker)
    bus.start_set()
    bus.render_block()
    bus.skip()
    bus.skip()
    assert maker.call_count == 1
    bus.render_block()
    pos = bus._pos
    bus.seek(50_000)
    assert bus._pos == pos


def test_a_track_change_while_the_effect_is_made_still_skips() -> None:
    first, second = _track(0.2, 100_000, "a"), _track(0.5, 100_000, "b")
    third = _track(0.7, 100_000, "c")
    holder: dict[str, MixBus] = {}

    def maker(track, pos):
        holder["bus"].stop_set()
        holder["bus"].start_set()
        holder["bus"].render_block()  # now playing "b"
        return SkipTail(track, pos, np.zeros((100, 2), np.float32))

    bus, started = _tail_bus([first, second, third], maker)
    holder["bus"] = bus
    bus.start_set()
    bus.render_block()
    bus.skip()
    assert started[-1] is second
    assert bus._skip_fade > 0


def test_a_skip_racing_the_end_of_the_set_does_nothing() -> None:
    holder: dict[str, MixBus] = {}

    def maker(track, pos):
        holder["bus"].stop_set()
        return SkipTail(track, pos, np.zeros((100, 2), np.float32))

    bus, _ = _tail_bus([_track(0.2, 100_000)], maker)
    holder["bus"] = bus
    bus.start_set()
    bus.render_block()
    bus.skip()
    assert bus._tail is None
    assert bus._skip_fade == 0


def test_stop_set_drops_a_pending_effect() -> None:
    bus, _ = _tail_bus(
        [_track(0.2, 100_000)],
        lambda track, pos: SkipTail(track, pos + 5000, np.ones((100, 2), np.float32)),
    )
    bus.start_set()
    bus.render_block()
    bus.skip()
    assert bus._tail is not None
    bus.stop_set()
    assert bus._tail is None


# ---------------------------------------------------------------------------
# Liner cues: a liner that waits for a point in a track
# ---------------------------------------------------------------------------


def test_a_cued_liner_starts_at_its_frame() -> None:
    track = _track(0.0, 100_000, "a")
    bus, _, _ = _bus([track])
    bus.start_set()
    bus.render_block()
    cue = LinerCue(track, 3000)
    bus.play_liner(np.full((882, 2), 0.3, np.float32), duck_db=0.0, at=cue)
    np.testing.assert_array_equal(bus.render_block(), 0.0)  # at 1764: not yet
    assert not cue.started
    bus.render_block()  # 2646
    assert not cue.started
    block = bus.render_block()  # reaches 3528: the liner starts in this block
    assert cue.started
    np.testing.assert_allclose(block[:, 0], 0.3)


def test_a_cue_already_passed_starts_at_once() -> None:
    track = _track(0.0, 100_000, "a")
    bus, _, _ = _bus([track])
    bus.start_set()
    bus.render_block()
    cue = LinerCue(track, 10)
    bus.play_liner(np.full((882, 2), 0.3, np.float32), duck_db=0.0, at=cue)
    assert cue.started
    np.testing.assert_allclose(bus.render_block()[:, 0], 0.3)


def test_a_cue_is_dropped_when_its_track_ends_first() -> None:
    first, second = _track(0.0, 2000, "a"), _track(0.0, 100_000, "b")
    bus, _, _ = _bus([first, second])
    bus.start_set()
    bus.render_block()
    cue = LinerCue(first, 5000)
    bus.play_liner(np.full((882, 2), 0.3, np.float32), duck_db=0.0, at=cue)
    for _ in range(10):
        np.testing.assert_array_equal(bus.render_block(), 0.0)
    assert not cue.started


def test_a_cue_late_in_the_last_block_of_its_track_still_starts() -> None:
    first, second = _track(0.0, 2000, "a"), _track(0.0, 100_000, "b")
    bus, _, _ = _bus([first, second])
    bus.start_set()
    bus.render_block()
    cue = LinerCue(first, 1900)
    bus.play_liner(np.full((882, 2), 0.3, np.float32), duck_db=0.0, at=cue)
    bus.render_block()  # to 1764
    assert not cue.started
    bus.render_block()  # "a" ends at 2000 and "b" starts in this block
    assert cue.started


def test_a_cue_in_a_skip_effect_still_starts() -> None:
    track = _track(0.0, 100_000, "a")
    bus, _ = _tail_bus(
        [track, _track(0.0, 100_000, "b")],
        lambda t, pos: SkipTail(t, pos, np.zeros((5000, 2), np.float32)),
    )
    bus.start_set()
    bus.render_block()
    cue = LinerCue(track, 2500)
    bus.play_liner(np.full((882, 2), 0.3, np.float32), duck_db=0.0, at=cue)
    bus.skip()
    bus.render_block()
    bus.render_block()
    assert cue.started
