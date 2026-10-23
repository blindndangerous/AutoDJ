"""The render-ahead worker keeps one rendered track ready for the mix bus."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj.mixbus import RenderedTrack
from autodj.render_ahead import RenderAhead

WAIT = 2.0  # generous upper bound; every wait below returns as soon as it can


def _track(name: str, count: int = 0) -> RenderedTrack:
    """A tiny render; ``start_offset`` stores the cursor position it began at."""
    entry = MagicMock()
    entry.path = f"{name}{count}"
    return RenderedTrack(entry, np.zeros((10, 2), np.float32), None, 0, "", start_offset=count)


class _Cursor:
    """Fake ``Player._next_rendered`` with rewind / skip-past hooks.

    Renders ``name0``, ``name1``... in order.  An optional gate blocks
    renders (when cleared) so tests can hold one in flight.
    """

    def __init__(self, name: str = "t") -> None:
        self.name = name
        self.count = 0
        self.calls = 0
        self.called = threading.Event()
        self.rendering = threading.Event()
        self.gate = threading.Event()
        self.gate.set()
        self.stale: set[str] = set()
        self.rewound: list[str] = []
        self.skipped_past: list[str] = []

    def __call__(self) -> RenderedTrack | None:
        self.calls += 1
        self.called.set()
        self.rendering.set()
        assert self.gate.wait(WAIT)
        track = _track(self.name, self.count)
        self.count += 1
        return track

    def is_stale(self, track: RenderedTrack) -> bool:
        return track.entry.path in self.stale

    def rewind(self, track: RenderedTrack) -> None:
        self.rewound.append(track.entry.path)
        self.count = track.start_offset

    def skip_past(self, track: RenderedTrack) -> None:
        self.skipped_past.append(track.entry.path)
        self.count = track.start_offset + 1


@pytest.fixture
def worker_factory(monkeypatch: pytest.MonkeyPatch):
    workers: list[RenderAhead] = []
    cursors: list[_Cursor] = []
    monkeypatch.setattr("autodj.render_ahead._RETRY_SECONDS", 0.01)

    def make(render=None) -> tuple[RenderAhead, _Cursor]:
        cursor = _Cursor()
        worker = RenderAhead(
            render or cursor,
            is_stale=cursor.is_stale,
            rewind=cursor.rewind,
            skip_past=cursor.skip_past,
        )
        workers.append(worker)
        cursors.append(cursor)
        return worker, cursor

    yield make
    for cursor in cursors:
        cursor.gate.set()
    for worker in workers:
        worker.stop(timeout=WAIT)


def _wait_for_ready(worker: RenderAhead) -> RenderedTrack:
    """Wait for a freshly rendered track (not just a fallback) and peek it."""
    with worker._cond:
        assert worker._cond.wait_for(lambda: worker._ready is not None, WAIT)
        ready = worker._ready
    assert ready is not None
    return ready


def test_pop_returns_none_before_anything_is_ready(worker_factory) -> None:
    worker, _ = worker_factory()
    assert worker.pop() is None


def test_renders_one_track_ahead_and_pops_it(worker_factory) -> None:
    worker, _ = worker_factory()
    worker.start()
    assert worker.wait_ready(WAIT)
    track = worker.pop()
    assert track is not None
    assert track.entry.path == "t0"


def test_keeps_at_most_one_track_ready(worker_factory) -> None:
    worker, cursor = worker_factory()
    worker.start()
    assert worker.wait_ready(WAIT)
    # With a track waiting, the worker must not render another one.
    cursor.called.clear()
    assert not cursor.called.wait(0.05)
    assert cursor.calls == 1


def test_pop_triggers_the_next_render(worker_factory) -> None:
    worker, _ = worker_factory()
    worker.start()
    assert worker.wait_ready(WAIT)
    first = worker.pop()
    assert worker.pop() is None  # the next one is not ready the instant we pop
    assert worker.wait_ready(WAIT)
    second = worker.pop()
    assert first is not None and second is not None
    assert (first.entry.path, second.entry.path) == ("t0", "t1")


def test_nothing_renderable_retries_without_a_ready_track(worker_factory) -> None:
    calls = []
    retried = threading.Event()

    def render() -> RenderedTrack | None:
        calls.append(1)
        if len(calls) >= 3:
            retried.set()
        return None

    worker, _ = worker_factory(render)
    worker.start()
    assert retried.wait(WAIT)
    assert worker.pop() is None
    assert not worker.wait_ready(0.01)


def test_render_exception_is_logged_and_retried(worker_factory, caplog) -> None:
    outcomes: list[object] = [RuntimeError("decode failed"), _track("ok")]

    def render() -> RenderedTrack | None:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]

    worker, _ = worker_factory(render)
    with caplog.at_level("ERROR", logger="autodj.render_ahead"):
        worker.start()
        assert worker.wait_ready(WAIT)
    track = worker.pop()
    assert track is not None and track.entry.path == "ok0"
    assert "decode failed" in caplog.text


# --- hard reset (new set, play now) ---------------------------------------


def test_reset_while_idle_applies_immediately_and_discards_ready(worker_factory) -> None:
    worker, cursor = worker_factory()
    worker.start()
    assert worker.wait_ready(WAIT)
    prepared = []
    worker.reset(lambda: prepared.append(cursor.count))
    assert prepared == [1]  # ran right away; the ready "t0" is gone
    assert worker.pop() is None

    def rename() -> None:
        cursor.name, cursor.count = "new", 0

    worker.reset(rename)
    assert worker.wait_ready(WAIT)
    track = worker.pop()
    assert track is not None and track.entry.path == "new0"


def test_reset_before_start_prepares_first_render(worker_factory) -> None:
    worker, cursor = worker_factory()
    worker.reset(lambda: setattr(cursor, "name", "seed"))
    assert cursor.name == "seed"
    worker.start()
    assert worker.wait_ready(WAIT)
    track = worker.pop()
    assert track is not None and track.entry.path == "seed0"


def test_reset_during_a_render_discards_it_and_defers_prepare(worker_factory) -> None:
    worker, cursor = worker_factory()
    cursor.gate.clear()
    worker.start()
    assert cursor.rendering.wait(WAIT)
    prepared = threading.Event()

    def prepare() -> None:
        cursor.name, cursor.count = "new", 0
        prepared.set()

    worker.reset(prepare)
    # Deferred: applying it now would be overwritten by the in-flight render.
    assert not prepared.is_set()
    cursor.gate.set()
    track = _wait_for_ready(worker)
    assert prepared.is_set()
    assert track.entry.path == "new0"


def test_resets_queued_behind_a_render_run_in_order(worker_factory) -> None:
    worker, cursor = worker_factory()
    cursor.gate.clear()
    worker.start()
    assert cursor.rendering.wait(WAIT)
    order: list[str] = []

    def move_to(name: str):
        def prepare() -> None:
            order.append(name)
            cursor.name, cursor.count = name, 0

        return prepare

    worker.reset(move_to("first"))
    worker.reset(move_to("second"))
    cursor.gate.set()
    assert _wait_for_ready(worker).entry.path == "second0"
    assert order == ["first", "second"]


def test_reset_drops_a_fallback_too(worker_factory) -> None:
    worker, cursor = worker_factory()
    worker.start()
    ready = _wait_for_ready(worker)
    cursor.stale.add(ready.entry.path)
    cursor.gate.clear()
    worker.refresh()
    assert worker._fallback is ready
    worker.reset(lambda: setattr(cursor, "name", "jump"))
    assert worker._fallback is None
    cursor.gate.set()
    assert _wait_for_ready(worker).entry.path.startswith("jump")


# --- soft refresh (queue edits): never dead air -----------------------------


def test_refresh_leaves_a_ready_track_that_is_still_valid(worker_factory) -> None:
    worker, cursor = worker_factory()
    worker.start()
    ready = _wait_for_ready(worker)
    worker.refresh()
    assert worker._ready is ready
    assert cursor.rewound == []
    assert cursor.calls == 1


def test_refresh_replaces_a_stale_ready_track(worker_factory) -> None:
    worker, cursor = worker_factory()
    worker.start()
    ready = _wait_for_ready(worker)  # "t0"
    cursor.stale.add("t0")
    worker.refresh()
    assert cursor.rewound == ["t0"]
    replacement = _wait_for_ready(worker)
    assert replacement is not ready
    assert worker._fallback is None  # dropped once its replacement is ready
    assert worker.pop() is replacement


def test_stale_ready_track_stays_available_until_replaced(worker_factory) -> None:
    """The offline worker retains its original soft-refresh behavior."""
    worker, cursor = worker_factory()
    worker.start()
    ready = _wait_for_ready(worker)  # "t0"
    cursor.stale.add("t0")
    cursor.gate.clear()  # the replacement render is slow
    worker.refresh()
    assert cursor.rendering.wait(WAIT)
    assert worker.wait_ready(0)  # something to play right now: the old render
    # The bus needs a track before the replacement is done: it gets the old one.
    assert worker.pop() is ready
    cursor.gate.set()
    # The replacement (of "t0", which is now playing) is thrown away and the
    # next render continues after the old one.
    after = _wait_for_ready(worker)
    assert cursor.skipped_past == ["t0"]
    assert after.entry.path == "t1"


def test_refresh_during_a_render_revalidates_it_on_completion(worker_factory) -> None:
    worker, cursor = worker_factory()
    cursor.gate.clear()
    worker.start()
    assert cursor.rendering.wait(WAIT)
    cursor.stale.add("t0")
    worker.refresh()  # can't judge "t0" until it exists
    cursor.gate.set()
    replacement = _wait_for_ready(worker)
    assert cursor.rewound == ["t0"]  # judged stale: rewound and rendered again
    assert replacement.entry.path == "t0"
    assert cursor.calls == 2


def test_refresh_during_a_render_keeps_a_valid_result(worker_factory) -> None:
    worker, cursor = worker_factory()
    cursor.gate.clear()
    worker.start()
    assert cursor.rendering.wait(WAIT)
    worker.refresh()
    cursor.gate.set()
    assert _wait_for_ready(worker).entry.path == "t0"
    assert cursor.rewound == []
    assert cursor.calls == 1


def test_refresh_when_idle_with_nothing_ready_does_nothing(worker_factory) -> None:
    worker, cursor = worker_factory()
    worker.refresh()
    assert cursor.rewound == [] and worker._fallback is None


def test_ready_popped_while_being_judged_is_left_alone(worker_factory) -> None:
    worker, cursor = worker_factory()
    worker.start()
    ready = _wait_for_ready(worker)

    def judge(track: RenderedTrack) -> bool:
        assert worker.pop() is track  # the bus takes it mid-judgement
        return True

    worker._is_stale = judge
    worker.refresh()
    assert worker._fallback is None
    assert cursor.rewound == []
    assert ready is not None


# --- lifecycle ---------------------------------------------------------------


def test_stop_ends_the_worker_thread(worker_factory) -> None:
    worker, _ = worker_factory()
    worker.start()
    assert worker.wait_ready(WAIT)
    thread = worker._thread
    assert thread is not None and thread.is_alive()
    worker.stop(timeout=WAIT)
    assert not thread.is_alive()


def test_stop_without_start_and_start_twice_are_safe(worker_factory) -> None:
    worker, _ = worker_factory()
    worker.stop(timeout=WAIT)  # never started: nothing to join
    worker.start()
    first_thread = worker._thread
    worker.start()  # already running: no second thread
    assert worker._thread is first_thread


def test_stop_can_be_signalled_without_waiting(worker_factory) -> None:
    worker, cursor = worker_factory()
    cursor.gate.clear()
    worker.start()
    assert cursor.rendering.wait(WAIT)
    worker.stop(timeout=0)  # returns at once even though a render is in flight
    thread = worker._thread
    cursor.gate.set()
    assert thread is not None
    thread.join(WAIT)
    assert not thread.is_alive()


def test_join_waits_for_a_render_in_flight_after_stop(worker_factory) -> None:
    worker, cursor = worker_factory()
    assert worker.join(0) is True  # never started
    cursor.gate.clear()
    worker.start()
    assert cursor.rendering.wait(WAIT)
    worker.stop(timeout=0)
    assert worker.join(0.02) is False  # still rendering
    cursor.gate.set()
    assert worker.join(WAIT) is True
    assert worker.join() is True  # already gone: returns at once


def test_join_from_the_worker_itself_does_not_wait(worker_factory) -> None:
    joined: list[bool] = []

    def render() -> RenderedTrack | None:
        worker.stop(timeout=0)
        joined.append(worker.join(WAIT))
        return None

    worker, _ = worker_factory(render)
    worker.start()
    thread = worker._thread
    assert thread is not None
    thread.join(WAIT)
    assert joined == [True]


def test_empty_render_during_stop_exits_without_retry_wait(
    worker_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder: dict[str, RenderAhead] = {}

    def render() -> RenderedTrack | None:
        holder["worker"].stop(timeout=0)  # stop arrives while this render runs
        return None

    worker, _ = worker_factory(render)
    monkeypatch.setattr("autodj.render_ahead._RETRY_SECONDS", 60.0)
    holder["worker"] = worker
    worker.start()
    thread = worker._thread
    assert thread is not None
    thread.join(WAIT)  # a 60 s retry wait here would blow this bound
    assert not thread.is_alive()


def test_restart_after_a_full_stop_starts_a_fresh_worker(worker_factory) -> None:
    worker, _ = worker_factory()
    worker.start()
    assert worker.wait_ready(WAIT)
    old = worker._thread
    worker.stop(timeout=WAIT)
    assert old is not None and not old.is_alive()
    assert worker._thread is None  # the exiting worker cleared itself
    assert worker.pop() is not None  # the ready track survived the stop
    worker.start()
    assert worker._thread is not None and worker._thread is not old
    assert worker.wait_ready(WAIT)


def test_restart_while_the_old_render_is_in_flight_keeps_one_worker(worker_factory) -> None:
    worker, cursor = worker_factory()
    cursor.gate.clear()
    worker.start()
    assert cursor.rendering.wait(WAIT)
    old = worker._thread
    worker.stop(timeout=0)
    worker.start()  # the old worker has not exited yet: it simply carries on
    assert worker._thread is old
    cursor.gate.set()
    assert worker.wait_ready(WAIT)
    assert worker.pop() is not None
    assert worker.wait_ready(WAIT)  # still rendering after the restart
    track = worker.pop()
    assert track is not None and track.entry.path == "t1"


def test_reset_while_a_finished_render_is_judged_wins(worker_factory) -> None:
    worker, cursor = worker_factory()
    cursor.gate.clear()
    worker.start()
    assert cursor.rendering.wait(WAIT)

    def judge_then_jump(_track: RenderedTrack) -> bool:
        worker.reset(lambda: setattr(cursor, "name", "jump"))  # lands mid-judgement
        return True

    worker._is_stale = judge_then_jump
    worker.refresh()  # flags the in-flight render for judging
    cursor.gate.set()
    assert _wait_for_ready(worker).entry.path.startswith("jump")
    assert worker._fallback is None  # the judged render was dropped, not kept
    assert cursor.rewound == []


# ---------------------------------------------------------------------------
# The plan hook (#88): the song change after the ready track
# ---------------------------------------------------------------------------


def _planning_worker(monkeypatch: pytest.MonkeyPatch, plan) -> tuple[RenderAhead, _Cursor]:
    monkeypatch.setattr("autodj.render_ahead._RETRY_SECONDS", 0.01)
    cursor = _Cursor()
    worker = RenderAhead(
        cursor,
        is_stale=cursor.is_stale,
        rewind=cursor.rewind,
        skip_past=cursor.skip_past,
        plan=plan,
    )
    return worker, cursor


def test_the_plan_hook_runs_for_each_ready_track_and_on_request(monkeypatch):
    planned: list[str] = []
    done = threading.Semaphore(0)

    def plan(track: RenderedTrack) -> None:
        planned.append(track.entry.path)
        done.release()

    worker, _cursor = _planning_worker(monkeypatch, plan)
    worker.start()
    try:
        assert done.acquire(timeout=WAIT)
        assert planned == ["t0"]
        worker.request_plan()
        assert done.acquire(timeout=WAIT)
        assert planned == ["t0", "t0"]
        assert worker.pop().entry.path == "t0"
        assert done.acquire(timeout=WAIT)
        assert planned[-1] == "t1"
    finally:
        worker.stop()


def test_cursor_moves_wait_while_a_plan_runs(monkeypatch):
    planning = threading.Event()
    release = threading.Event()
    seen: list[int] = []

    def plan(_track: RenderedTrack) -> None:
        planning.set()
        assert release.wait(WAIT)

    worker, cursor = _planning_worker(monkeypatch, plan)
    worker.start()
    try:
        assert planning.wait(WAIT)
        # A reset during the plan is queued, not run under it ...
        worker.reset(lambda: seen.append(cursor.count))
        assert seen == []
        release.set()
        # ... and runs before the next render.
        assert worker.wait_ready(WAIT)
        assert seen == [1]
    finally:
        release.set()
        worker.stop()


def test_a_failing_plan_never_stops_the_worker(monkeypatch):
    def plan(_track: RenderedTrack) -> None:
        raise RuntimeError("boom")

    worker, _cursor = _planning_worker(monkeypatch, plan)
    worker.start()
    try:
        assert worker.wait_ready(WAIT)
        assert worker.pop() is not None
        assert worker.wait_ready(WAIT)
    finally:
        worker.stop()


def test_request_plan_without_a_hook_or_a_ready_track_does_nothing(worker_factory):
    worker, _cursor = worker_factory()
    worker.request_plan()
    assert worker._plan_due is False


def test_the_discard_hook_hears_every_render_thrown_away(monkeypatch):
    monkeypatch.setattr("autodj.render_ahead._RETRY_SECONDS", 0.01)
    cursor = _Cursor()
    discarded: list[str] = []
    worker = RenderAhead(
        cursor,
        is_stale=cursor.is_stale,
        rewind=cursor.rewind,
        skip_past=cursor.skip_past,
        discard=lambda track: discarded.append(track.entry.path),
    )
    worker.start()
    try:
        assert worker.wait_ready(WAIT)
        # A reset throws away the ready track ...
        worker.reset(lambda: None)
        assert discarded == ["t0"]
        assert worker.wait_ready(WAIT)
        # ... and a render that a reset outdated while it was in flight.
        cursor.gate.clear()
        cursor.rendering.clear()
        assert worker.pop() is not None
        assert cursor.rendering.wait(WAIT)
        worker.reset(lambda: None)
        cursor.gate.set()
        assert worker.wait_ready(WAIT)
        assert len(discarded) == 2
    finally:
        cursor.gate.set()
        worker.stop()


def test_a_failing_discard_hook_never_stops_the_worker(monkeypatch):
    monkeypatch.setattr("autodj.render_ahead._RETRY_SECONDS", 0.01)
    cursor = _Cursor()

    def broken(_track):
        raise RuntimeError("boom")

    worker = RenderAhead(
        cursor,
        is_stale=cursor.is_stale,
        rewind=cursor.rewind,
        skip_past=cursor.skip_past,
        discard=broken,
    )
    worker.start()
    try:
        assert worker.wait_ready(WAIT)
        worker.reset(lambda: None)
        assert worker.wait_ready(WAIT)
    finally:
        worker.stop()
