"""Live streaming stays on the progressive playout controller, not local render-ahead."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from autodj.live_audio import LiveAudio
from autodj.live_playout import LivePlayout
from autodj.mixbus import BusEvents, MixBus, RenderedTrack
from autodj.player import Player
from autodj.stereo import SAMPLE_RATE
from tests.unit._break_station import LEVELS, make_station
from tests.unit._fakes import make_cfg_mock, make_sim_index
from tests.unit.test_live_playout import _UnconsumedDeck


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _player() -> Player:
    return Player(make_cfg_mock(), make_sim_index(6), stream_mode=True)


def test_stream_lifecycle_uses_live_controller_without_local_slot_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start, queue maintenance, return, prepared-tail refresh, and stop use LivePlayout."""

    def forbidden_render_ahead(*_args, **_kwargs):
        raise AssertionError("stream mode must not construct local RenderAhead")

    monkeypatch.setattr("autodj.player.RenderAhead", forbidden_render_ahead)
    player = _player()
    assert isinstance(player._playout, LivePlayout)

    def forbidden_slot_plan(*_args, **_kwargs):
        raise AssertionError("stream mode must not run local slot planning hooks")

    monkeypatch.setattr(player, "_plan_slot_serial", forbidden_slot_plan)
    monkeypatch.setattr(player, "_claim_slot_plan", forbidden_slot_plan)

    next_index = 0
    prepared: list[RenderedTrack] = []

    def prepare() -> RenderedTrack:
        nonlocal next_index
        current = player._sim.entries[next_index % 6]
        following = player._sim.entries[(next_index + 1) % 6]
        next_index += 1
        track = RenderedTrack(
            current,
            np.zeros((2048, 2), dtype=np.float32),
            following,
            0,
            "",
        )
        prepared.append(track)
        return track

    monkeypatch.setattr(player, "_next_rendered", prepare)
    queue_refresh = threading.Event()

    def mark_refresh() -> None:
        player._live_queue_dirty = False
        queue_refresh.set()

    # A clean live queue refresh is a worker maintenance event. Avoid changing
    # the test queue while still observing that the controller processed it.
    monkeypatch.setattr(player, "_refresh_live_queue", mark_refresh)

    player.begin_set(player._sim.entries[0], "seed")
    player._live_queue_dirty = True
    player._playout.start()
    try:
        assert player._playout.wait_ready(2.0)
        assert prepared

        player._playout.request_plan()
        assert queue_refresh.wait(2.0)

        # With no current render, prepared-tail maintenance invalidates the
        # handoff and asks the same live controller to prepare it again.
        player._playing_render = None
        player.refresh_prepared_tail()
        assert player._playout.wait_ready(2.0)

        before_return = len(prepared)
        player.prepare_live_return(timeout=2.0)
        assert len(prepared) > before_return
        assert player._playout.ready() is not None
    finally:
        player._playout.stop(timeout=2.0)
        player.end_set()


def test_stream_player_start_stop_does_not_touch_local_render_ahead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual stream lifecycle owns and parks the live controller."""

    def forbidden_render_ahead(*_args, **_kwargs):
        raise AssertionError("stream mode must not construct local RenderAhead")

    monkeypatch.setattr("autodj.player.RenderAhead", forbidden_render_ahead)
    player = _player()
    entry = player._sim.entries[0]
    monkeypatch.setattr(player, "_next_rendered", lambda: None)
    player.begin_set(entry, "seed")
    player._playout.start()
    assert isinstance(player._playout, LivePlayout)
    player.end_set()
    player._playout.stop(timeout=2.0)
    assert player._playout.join(2.0)
    assert player._pending_entry is None


def test_real_stream_selection_replans_audible_handoff_before_stale_queue_item_airs(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0004-09: the real streaming picker replaces unheard queued music atomically."""
    monkeypatch.setattr(
        "autodj.player.RenderAhead",
        lambda *_args, **_kwargs: pytest.fail("stream mode constructed RenderAhead"),
    )
    player, entries, _points, _desk = make_station(tmp_path, stream_mode=True)
    monkeypatch.setattr(
        player,
        "_plan_slot_serial",
        lambda *_args, **_kwargs: pytest.fail("stream mode invoked local slot planning"),
    )
    monkeypatch.setattr(
        player,
        "_claim_slot_plan",
        lambda *_args, **_kwargs: pytest.fail("stream mode claimed a local slot plan"),
    )
    a, b, c, d = entries
    decks = {
        entry.path: _UnconsumedDeck(round(entry.length * SAMPLE_RATE), level)
        for entry, level in zip(entries, LEVELS, strict=True)
    }
    monkeypatch.setattr(player, "_live_file", lambda entry, **_kwargs: decks[entry.path])

    # This controller test isolates queue selection and bus handoff; bounded
    # transition windows are synthetic while the music layers remain live decks.
    monkeypatch.setattr(
        "autodj.live_transitions._read_window",
        lambda _player, _entry, deck, first, last: np.full(
            (max(0, last - first), 2), deck.value, np.float32
        ),
    )
    started: list[object] = []

    def on_track_start(rendered):
        started.append(rendered.entry)
        player._on_track_start(rendered)

    player.bus = MixBus(
        BusEvents(on_track_start, player._on_position, player._take_render),
        eq_gains=lambda: (1.0, 1.0, 1.0),
    )
    player._state.queue[:] = [b, c, d]
    player.begin_set(a, "seed")
    player._playout.start()
    player.bus.start_set()

    try:
        assert player._playout.wait_ready(2.0)
        original = player._playout.ready()
        assert original is not None and original.entry is a and original.next_entry is b

        first_block = player.bus.render_block()
        assert player._playing_render is original
        assert player.bus.current_render() is original
        assert np.mean(first_block[:, 0]) > 0.8
        paused_at = player.bus.live_position(original)
        player.bus.pause(True)
        player.bus.render_block()
        assert player.bus.live_position(original) == paused_at
        player.bus.pause(False)

        with player._state.queue_lock:
            player._state.queue[:] = [c, d, b]
            player.refresh_playout()

        assert _wait_for(
            lambda: (
                player._playing_render is not None
                and player._playing_render is not original
                and player._playing_render.entry is a
                and player._playing_render.next_entry is c
            )
        )
        replacement = player._playing_render
        assert replacement is not None and isinstance(replacement.audio, LiveAudio)
        assert player.bus.current_render() is replacement
        assert player.bus.live_position(replacement) == player.bus.BLOCK

        # The replacement tail actually yields C, then C is the next current
        # render. The previously mixed B handoff must never become current.
        last_overlap = None
        for _ in range(1400):
            if player.bus.current_render() is not replacement:
                break
            position = player.bus.live_position(replacement)
            if position >= len(replacement.audio) - player.bus.BLOCK:
                last_overlap = player.bus.render_block()
            else:
                player.bus.render_block()
        assert last_overlap is not None
        assert np.mean(last_overlap[:, 0]) > 0.4

        def resumed():
            player.bus.render_block()
            return player.bus.current_render() is not None

        assert _wait_for(resumed)
        assert player.bus.current_render().entry is c
        assert player.bus.current_render().entry is not b
        assert b not in started
    finally:
        player._playout.stop(timeout=2.0)
        player.end_set()


@pytest.mark.parametrize("case", ["overlap", "cut", "exhausted", "repeat", "delayed"])
def test_stream_selects_successor_only_when_continuation_becomes_current(
    tmp_path, monkeypatch, case
):
    """ADR-0004-12: B continues its deck; C is not opened while A is current."""
    player, entries, points, _desk = make_station(tmp_path, stream_mode=True)
    a, b, c, d = entries
    if case == "cut":
        points[a.path]["segue"] = points[a.path]["end"]
    elif case == "exhausted":
        b.length = 0.5
        points[b.path].update(intro=0.5, segue=0.5, end=0.5)
    decks = {
        entry.path: _UnconsumedDeck(round(entry.length * SAMPLE_RATE), level)
        for entry, level in zip(entries, LEVELS, strict=True)
    }
    opened = []
    entered, release = threading.Event(), threading.Event()
    prepare = player._next_rendered_serial

    def delayed_prepare():
        if case == "delayed" and player._pending_entry is b:
            entered.set()
            assert release.wait(2)
        return prepare()

    monkeypatch.setattr(player, "_next_rendered_serial", delayed_prepare)

    def open_deck(entry, **_kwargs):
        opened.append(entry)
        deck = decks[entry.path]
        return _UnconsumedDeck(len(deck), deck.value)

    monkeypatch.setattr(player, "_live_file", open_deck)
    monkeypatch.setattr(player, "_live_source_is_current", lambda *_args: True)
    monkeypatch.setattr(
        "autodj.live_transitions._read_window",
        lambda _player, _entry, deck, first, last: np.full(
            (max(0, last - first), 2), deck.value, np.float32
        ),
    )
    player.bus = MixBus(
        BusEvents(player._on_track_start, player._on_position, player._take_render),
        eq_gains=lambda: (1.0, 1.0, 1.0),
    )
    queued = [b, b, d] if case == "repeat" else [b, c, d]
    player._state.queue[:] = queued
    player.begin_set(a, "seed")
    player._playout.start()
    player.bus.start_set()
    try:
        assert player._playout.wait_ready(2)
        first = player._playout.ready()
        assert first.entry is a and first.next_entry is b
        player.bus.render_block()
        assert player._playout.wait_ready(2)
        continuation = player._playout.ready()
        assert continuation.entry is b and continuation.next_entry is None
        assert continuation.handoff_pending
        assert continuation.live_source is first.next_live_source
        assert continuation.start_offset == first.next_start_offset
        assert opened == [a, b]
        assert player._state.queue == queued
        for _ in range(1500):
            player.bus.render_block()
            if player.bus.current_render() is None:
                break
        assert player.bus.current_render() is None
        if case == "delayed":
            player.bus.render_block()
            assert entered.wait(1)
            position = player.bus.live_position(continuation)
            for _ in range(3):
                np.testing.assert_allclose(player.bus.render_block(), LEVELS[1])
            assert player.bus.live_position(continuation) == position + 3 * MixBus.BLOCK
            assert player._state.queue == queued[1:]
            assert opened == [a, b]
            release.set()

        expected = c if case == "exhausted" else b

        def promoted():
            player.bus.render_block()
            return (
                player._playing_render.entry is expected
                and not player._playing_render.handoff_pending
            )

        assert _wait_for(promoted)
        current = player._playing_render
        assert not current.handoff_pending
        assert player.bus.live_position(current) > 0
        assert player._playout.wait_ready(2)
        assert player._playout.ready().next_entry is None
        if case == "exhausted":
            assert first.next_start_offset >= round(b.length * SAMPLE_RATE)
            assert current.next_entry is d
            assert opened == [a, b, c, d]
            assert player._state.queue == [d]
        elif case == "repeat":
            assert current.entry is b and current.next_entry is b
            assert current.live_source is continuation.live_source
            assert current.next_live_source is not current.live_source
            assert opened == [a, b, b]
            assert player._state.queue == [b, d]
        else:
            assert current.next_entry is c
            assert current.live_source is continuation.live_source
            assert current.start_offset == continuation.start_offset
            assert opened == [a, b, c]
            assert player._state.queue == [c, d]
            assert player._playout.ready().entry is c
            assert d not in opened
    finally:
        release.set()
        player._playout.stop(timeout=2)
        player.end_set()


def test_live_cursor_carries_glide_and_reservations_without_selecting_a_successor(monkeypatch):
    player = _player()
    a, b, liner = player._sim.entries[:3]
    glide, source = object(), object()
    player._set_render_cursor(
        b,
        12345,
        "queue",
        from_queue=True,
        previous=a,
        mixed_in=("stretch", 1.05),
        glide=glide,
        reserved_liners=(liner,),
        live_source=source,
    )

    def render(current, upcoming, offset, *, glide_in):
        assert current is b and upcoming is None and offset == 12345
        assert glide_in is glide
        assert player._render_live_sources == (b, source, None, None)
        return RenderedTrack(b, LiveAudio(100, ()), None, 0, "", start_offset=offset)

    monkeypatch.setattr(player, "_render_track", render)
    cursor = player._live_continuation()
    assert cursor.handoff_pending and len(cursor.audio) == 100
    assert cursor.next_entry is None and cursor.next_live_source is None
    player._set_render_cursor(None, 0, "seed")
    player._rewind_render_cursor(cursor)
    assert player._pending_entry is b
    assert player._pending_offset == 12345
    assert player._pending_glide is glide
    assert player._pending_live_source is source
    assert player._pending_reserved_liners == (liner,)
    assert player._pending_mixed_in == ("stretch", 1.05)
