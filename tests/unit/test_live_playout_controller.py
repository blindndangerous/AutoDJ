from __future__ import annotations

import threading
import time

from autodj.live_playout import LivePlayout


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_live_playout_pop_waits_for_explicit_cursor_promotion() -> None:
    """ADR-0004-12: streaming preparation stays in its independent worker."""
    calls = 0

    def prepare():
        nonlocal calls
        calls += 1
        return f"handoff-{calls}"

    worker = LivePlayout(prepare, rewind=lambda _track: None)
    worker.start()
    try:
        assert worker.wait_ready(1.0)
        first = worker.ready()
        assert first == "handoff-1"
        assert worker.ready() is first
        assert worker.pop() is first
        assert not worker.wait_ready(0.05)
        assert calls == 1
        worker.reset(lambda: None)
        assert worker.wait_ready(1.0)
        assert worker.ready() == "handoff-2"
    finally:
        worker.stop()
    assert worker.join(1.0)


def test_reset_during_prepare_discards_old_result_before_moving_and_repreparing() -> None:
    entered, release = threading.Event(), threading.Event()
    cursor: list[str] = []
    discarded: list[str] = []
    calls = 0

    def prepare():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(2.0)
            return "old"
        return f"new:{cursor[-1]}"

    worker = LivePlayout(prepare, rewind=lambda _track: None, discard=discarded.append)
    worker.start()
    assert entered.wait(1.0)
    worker.reset(lambda: cursor.append("reset"))
    assert cursor == []  # The cursor move waits until preparation yields.
    release.set()
    try:
        assert worker.wait_ready(1.0)
        assert worker.ready() == "new:reset"
        assert discarded == ["old"]
    finally:
        worker.stop()
    assert worker.join(1.0)


def test_worker_waits_for_synchronous_reset_cursor_move() -> None:
    entered, release, prepared = threading.Event(), threading.Event(), threading.Event()
    reset_done = threading.Event()

    def prepare():
        prepared.set()
        return "prepared"

    worker = LivePlayout(prepare, rewind=lambda _track: None)

    def move_cursor():
        entered.set()
        assert release.wait(2.0)

    reset_thread = threading.Thread(
        target=lambda: (worker.reset(move_cursor), reset_done.set()), daemon=True
    )
    reset_thread.start()
    assert entered.wait(1.0)
    worker.start()
    assert not prepared.wait(0.05)
    release.set()
    assert reset_done.wait(1.0)
    try:
        assert worker.wait_ready(1.0)
        assert prepared.is_set()
    finally:
        worker.stop()
    assert worker.join(1.0)
    reset_thread.join(1.0)


def test_request_plan_runs_maintenance_without_a_ready_handoff() -> None:
    maintained: list[str | None] = []
    worker = LivePlayout(
        prepare=lambda: "prepared",
        rewind=lambda _track: None,
        maintain=maintained.append,
    )
    worker.request_plan()
    worker.start()
    try:
        assert _wait_for(lambda: bool(maintained))
        assert maintained[0] is None
        assert worker.wait_ready(1.0)
        assert _wait_for(lambda: "prepared" in maintained)
    finally:
        worker.stop()
    assert worker.join(1.0)


def test_request_plan_during_preparation_is_not_lost() -> None:
    entered, release = threading.Event(), threading.Event()
    maintained: list[str | None] = []

    def prepare():
        entered.set()
        assert release.wait(2.0)
        return "prepared"

    worker = LivePlayout(
        prepare,
        rewind=lambda _track: None,
        maintain=maintained.append,
    )
    worker.start()
    assert entered.wait(1.0)
    worker.request_plan()
    release.set()
    try:
        assert _wait_for(lambda: maintained == ["prepared"])
        assert worker.ready() == "prepared"
    finally:
        worker.stop()
    assert worker.join(1.0)


def test_refresh_withdraws_ready_without_fallback_and_rewinds_before_retry() -> None:
    calls = 0
    actions: list[str] = []

    def prepare():
        nonlocal calls
        calls += 1
        actions.append(f"prepare-{calls}")
        return f"handoff-{calls}"

    worker = LivePlayout(
        prepare,
        rewind=lambda track: actions.append(f"rewind-{track}"),
        discard=lambda track: actions.append(f"discard-{track}"),
    )
    worker.start()
    try:
        assert worker.wait_ready(1.0)
        old = worker.ready()
        worker.refresh()
        assert old == "handoff-1"
        assert worker.wait_ready(1.0)
        assert worker.ready() == "handoff-2"
        assert worker.pop() == "handoff-2"
        assert "rewind-handoff-1" in actions
        assert "discard-handoff-1" in actions
        assert not any(action.startswith("rewind-handoff-2") for action in actions)
    finally:
        worker.stop()
    assert worker.join(1.0)


def test_refresh_during_preparation_rewinds_stale_result_before_retry() -> None:
    entered, release = threading.Event(), threading.Event()
    calls = 0
    actions: list[str] = []

    def prepare():
        nonlocal calls
        calls += 1
        actions.append(f"prepare-{calls}")
        if calls == 1:
            entered.set()
            assert release.wait(2.0)
            return "stale"
        return "fresh"

    worker = LivePlayout(
        prepare,
        rewind=lambda track: actions.append(f"rewind-{track}"),
        discard=lambda track: actions.append(f"discard-{track}"),
    )
    worker.start()
    assert entered.wait(1.0)
    worker.refresh()
    release.set()
    try:
        assert worker.wait_ready(1.0)
        assert worker.ready() == "fresh"
        assert actions[:4] == [
            "prepare-1",
            "rewind-stale",
            "discard-stale",
            "prepare-2",
        ]
    finally:
        worker.stop()
    assert worker.join(1.0)


def test_replace_ready_is_identity_checked_and_discards_only_replaced_item() -> None:
    discarded: list[str] = []
    worker = LivePlayout(
        prepare=lambda: "first",
        rewind=lambda _track: None,
        discard=discarded.append,
    )
    worker.start()
    try:
        assert worker.wait_ready(1.0)
        first = worker.ready()
        assert first == "first"
        assert not worker.replace_ready("not-current", "wrong")
        assert worker.replace_ready(first, "replacement")
        assert worker.ready() == "replacement"
        assert discarded == []
    finally:
        worker.stop()
    assert worker.join(1.0)
    assert discarded == ["replacement"]


def test_stop_cleans_ready_and_restart_prepares_a_new_handoff() -> None:
    calls = 0
    discarded: list[str] = []

    def prepare():
        nonlocal calls
        calls += 1
        return f"handoff-{calls}"

    worker = LivePlayout(prepare, rewind=lambda _track: None, discard=discarded.append)
    worker.start()
    assert worker.wait_ready(1.0)
    worker.stop()
    assert worker.join(1.0)
    assert worker.ready() is None
    assert discarded == ["handoff-1"]

    worker.start()
    try:
        assert worker.wait_ready(1.0)
        assert worker.ready() == "handoff-2"
    finally:
        worker.stop()
    assert worker.join(1.0)
    assert discarded == ["handoff-1", "handoff-2"]
