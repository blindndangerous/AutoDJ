"""The render-ahead worker keeps one rendered track ready for the mix bus."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj.mixbus import RenderedTrack
from autodj.render_ahead import RenderAhead

WAIT = 2.0  # generous upper bound; every wait below returns as soon as it can


def _track(name: str) -> RenderedTrack:
    entry = MagicMock()
    entry.path = name
    return RenderedTrack(entry, np.zeros((10, 2), np.float32), None, 0, "")


class _Cursor:
    """Fake ``Player._next_rendered``: renders ``name0``, ``name1``... in order."""

    def __init__(self, name: str = "t") -> None:
        self.name = name
        self.count = 0
        self.calls = 0
        self.called = threading.Event()

    def __call__(self) -> RenderedTrack | None:
        self.calls += 1
        self.called.set()
        track = _track(f"{self.name}{self.count}")
        self.count += 1
        return track


@pytest.fixture
def worker_factory():
    workers: list[RenderAhead] = []

    def make(render, retry_seconds: float = 0.01) -> RenderAhead:
        worker = RenderAhead(render, retry_seconds=retry_seconds)
        workers.append(worker)
        return worker

    yield make
    for worker in workers:
        worker.stop(timeout=WAIT)


def test_pop_returns_none_before_anything_is_ready(worker_factory) -> None:
    worker = worker_factory(_Cursor())
    assert worker.pop() is None


def test_renders_one_track_ahead_and_pops_it(worker_factory) -> None:
    cursor = _Cursor()
    worker = worker_factory(cursor)
    worker.start()
    assert worker.wait_ready(WAIT)
    track = worker.pop()
    assert track is not None
    assert track.entry.path == "t0"


def test_keeps_at_most_one_track_ready(worker_factory) -> None:
    cursor = _Cursor()
    worker = worker_factory(cursor)
    worker.start()
    assert worker.wait_ready(WAIT)
    # With a track waiting, the worker must not render another one.
    cursor.called.clear()
    assert not cursor.called.wait(0.05)
    assert cursor.calls == 1


def test_pop_triggers_the_next_render(worker_factory) -> None:
    worker = worker_factory(_Cursor())
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

    worker = worker_factory(render)
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

    worker = worker_factory(render)
    with caplog.at_level("ERROR", logger="autodj.render_ahead"):
        worker.start()
        assert worker.wait_ready(WAIT)
    track = worker.pop()
    assert track is not None and track.entry.path == "ok"
    assert "decode failed" in caplog.text


def test_reset_while_idle_applies_immediately_and_discards_ready(worker_factory) -> None:
    cursor = _Cursor()
    discards: list[RenderedTrack] = []
    worker = worker_factory(cursor)
    worker._on_discard = discards.append
    worker.start()
    assert worker.wait_ready(WAIT)
    prepared = []
    worker.reset(lambda discarded: prepared.append((cursor.count, discarded)))
    # The ready "t0" is thrown away and handed to both prepare and on_discard;
    # the prepare step ran right away.
    assert len(prepared) == 1
    count, discarded = prepared[0]
    assert count == 1
    assert discarded is not None and discarded.entry.path == "t0"
    assert discards == [discarded]

    def rename(_discarded: RenderedTrack | None) -> None:
        cursor.name, cursor.count = "new", 0

    worker.reset(rename)
    assert worker.wait_ready(WAIT)
    track = worker.pop()
    assert track is not None and track.entry.path == "new0"


def test_reset_before_start_prepares_first_render(worker_factory) -> None:
    cursor = _Cursor()
    worker = worker_factory(cursor)
    seen = []

    def prepare(discarded: RenderedTrack | None) -> None:
        seen.append(discarded)
        cursor.name = "seed"

    worker.reset(prepare)
    assert cursor.name == "seed"
    assert seen == [None]
    worker.start()
    assert worker.wait_ready(WAIT)
    track = worker.pop()
    assert track is not None and track.entry.path == "seed0"


def _gated_render(state: dict, gate: threading.Event, rendering: threading.Event):
    def render() -> RenderedTrack | None:
        name = state["name"]
        if name == "old":
            rendering.set()
            assert gate.wait(WAIT)
        track = _track(f"{name}{state['count']}")
        state["count"] += 1  # the render advances its cursor, like _next_rendered
        return track

    return render


def test_reset_during_a_render_discards_it_and_defers_prepare(worker_factory) -> None:
    gate, rendering = threading.Event(), threading.Event()
    state = {"name": "old", "count": 0}
    prepared: list[RenderedTrack | None] = []
    discards: list[RenderedTrack] = []

    def prepare(discarded: RenderedTrack | None) -> None:
        prepared.append(discarded)
        state["name"], state["count"] = "new", 0

    worker = worker_factory(_gated_render(state, gate, rendering))
    worker._on_discard = discards.append
    worker.start()
    assert rendering.wait(WAIT)
    worker.reset(prepare)
    # Deferred: applying it now would be overwritten by the in-flight render.
    assert prepared == []
    gate.set()
    assert worker.wait_ready(WAIT)
    track = worker.pop()
    assert track is not None and track.entry.path == "new0"
    # The stale in-flight render went to on_discard and to the prepare.
    assert len(prepared) == 1 and prepared[0] is not None
    assert prepared[0].entry.path == "old0"
    assert discards == [prepared[0]]


def test_resets_queued_behind_a_render_run_in_order(worker_factory) -> None:
    gate, rendering = threading.Event(), threading.Event()
    state = {"name": "old", "count": 0}
    calls: list[tuple[str, str | None]] = []

    def move_to(name: str):
        def prepare(discarded: RenderedTrack | None) -> None:
            calls.append((name, discarded.entry.path if discarded else None))
            state["name"], state["count"] = name, 0

        return prepare

    worker = worker_factory(_gated_render(state, gate, rendering))
    worker.start()
    assert rendering.wait(WAIT)
    worker.reset(move_to("first"))
    worker.reset(move_to("second"))
    gate.set()
    assert worker.wait_ready(WAIT)
    track = worker.pop()
    assert track is not None and track.entry.path == "second0"
    # Only the first queued prepare sees the discarded render.
    assert calls == [("first", "old0"), ("second", None)]


def test_stop_ends_the_worker_thread(worker_factory) -> None:
    worker = worker_factory(_Cursor())
    worker.start()
    assert worker.wait_ready(WAIT)
    thread = worker._thread
    assert thread is not None and thread.is_alive()
    worker.stop(timeout=WAIT)
    assert not thread.is_alive()


def test_stop_without_start_and_start_twice_are_safe(worker_factory) -> None:
    worker = worker_factory(_Cursor())
    worker.stop(timeout=WAIT)  # never started: nothing to join
    worker.start()
    first_thread = worker._thread
    worker.start()  # already running: no second thread
    assert worker._thread is first_thread


def test_stop_can_be_signalled_without_waiting(worker_factory) -> None:
    gate = threading.Event()
    rendering = threading.Event()

    def render() -> RenderedTrack | None:
        rendering.set()
        gate.wait(WAIT)
        return _track("late")

    worker = worker_factory(render)
    worker.start()
    assert rendering.wait(WAIT)
    worker.stop(timeout=0)  # returns at once even though a render is in flight
    thread = worker._thread
    gate.set()
    assert thread is not None
    thread.join(WAIT)
    assert not thread.is_alive()


def test_empty_render_during_stop_exits_without_retry_wait(worker_factory) -> None:
    holder: dict[str, RenderAhead] = {}

    def render() -> RenderedTrack | None:
        holder["worker"].stop(timeout=0)  # stop arrives while this render runs
        return None

    worker = worker_factory(render, retry_seconds=60.0)
    holder["worker"] = worker
    worker.start()
    thread = worker._thread
    assert thread is not None
    thread.join(WAIT)  # a 60 s retry wait here would blow this bound
    assert not thread.is_alive()


def test_restart_after_a_full_stop_starts_a_fresh_worker(worker_factory) -> None:
    worker = worker_factory(_Cursor())
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
    gate, rendering = threading.Event(), threading.Event()
    state = {"name": "old", "count": 0}
    worker = worker_factory(_gated_render(state, gate, rendering))
    worker.start()
    assert rendering.wait(WAIT)
    old = worker._thread
    worker.stop(timeout=0)
    worker.start()  # the old worker has not exited yet: it simply carries on
    assert worker._thread is old
    gate.set()
    assert worker.wait_ready(WAIT)
    assert worker.pop() is not None
    state["name"] = "later"
    assert worker.wait_ready(WAIT)  # still rendering after the restart
    track = worker.pop()
    assert track is not None and track.entry.path.startswith("later")
