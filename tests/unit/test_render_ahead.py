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
    worker = worker_factory(cursor)
    worker.start()
    assert worker.wait_ready(WAIT)
    prepared = []
    worker.reset(lambda: prepared.append(cursor.count))
    # The ready "t0" is thrown away; the prepare step ran right away.
    assert prepared == [1]

    def rename() -> None:
        cursor.name, cursor.count = "new", 0

    worker.reset(rename)
    assert worker.wait_ready(WAIT)
    track = worker.pop()
    assert track is not None and track.entry.path == "new0"


def test_reset_before_start_prepares_first_render(worker_factory) -> None:
    cursor = _Cursor()
    worker = worker_factory(cursor)
    worker.reset(lambda: setattr(cursor, "name", "seed"))
    assert cursor.name == "seed"
    worker.start()
    assert worker.wait_ready(WAIT)
    track = worker.pop()
    assert track is not None and track.entry.path == "seed0"


def test_reset_during_a_render_discards_it_and_defers_prepare(worker_factory) -> None:
    gate = threading.Event()
    rendering = threading.Event()
    state = {"name": "old", "count": 0}

    def render() -> RenderedTrack | None:
        name = state["name"]
        if name == "old":
            rendering.set()
            assert gate.wait(WAIT)
        track = _track(f"{name}{state['count']}")
        state["count"] += 1  # the render advances its cursor, like _next_rendered
        return track

    prepared = threading.Event()

    def prepare() -> None:
        state["name"], state["count"] = "new", 0
        prepared.set()

    worker = worker_factory(render)
    worker.start()
    assert rendering.wait(WAIT)
    worker.reset(prepare)
    # Deferred: applying it now would be overwritten by the in-flight render.
    assert not prepared.is_set()
    gate.set()
    assert worker.wait_ready(WAIT)
    track = worker.pop()
    assert prepared.is_set()
    assert track is not None and track.entry.path == "new0"


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
