from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from autodj.breaks import VOICE_TRACK, BreakError, SlotPlan, new_id
from autodj.live_audio import LiveAudio
from autodj.mixbus import BusEvents, MixBus, RenderedTrack
from autodj.stereo import SAMPLE_RATE
from tests.unit._break_station import LEVELS, make_break, make_station
from tests.unit.test_live_playout import _UnconsumedDeck


class _FakePlayout:
    def __init__(self, ready=None):
        self._ready = ready

    def ready(self):
        return self._ready

    def reset(self, callback):
        callback()

    def replace_ready(self, expected, replacement):
        if self._ready is not expected:
            return False
        self._ready = replacement
        return True


def _current_handoff(tmp_path: Path):
    player, entries, points, desk = make_station(tmp_path)
    a, b, c, d = entries
    player.live_mixing = True
    player._voice_tracking_requested = True
    decks = {
        entry.path: _UnconsumedDeck(round(entry.length * SAMPLE_RATE), level)
        for entry, level in zip(entries, LEVELS, strict=True)
    }
    player._live_file = lambda entry, **_kwargs: decks[entry.path]
    player._render_live_sources = (a, decks[a.path], b, decks[b.path])
    current = player._render_track(a, b, 0)
    player._render_live_sources = None
    assert current is not None and isinstance(current.audio, LiveAudio)
    current = replace(current, live_source=decks[a.path], next_live_source=decks[b.path])
    player.bus = MixBus(
        BusEvents(player._on_track_start, player._on_position, player._take_render),
        eq_gains=lambda: (1.0, 1.0, 1.0),
    )
    player.bus._playing = True
    player.bus._current = current
    player._playing_render = current
    player._state.current_track = a
    player._playback_output_pos[0] = 0
    player._live_queue_revision = 0
    player._playout = _FakePlayout()
    future_plan = SlotPlan(new_id(), 0, b, 0.0, c, d, "queue", (), None, None, None, None, ())
    player._slot_plan = future_plan
    return player, entries, points, desk, current, future_plan


def _voice(desk, slot_id: str, *, seconds: float = 1.0):
    item = make_break(desk, slot_id, kind="dj", placement="talk_up", seconds=seconds)
    return replace(item, source=VOICE_TRACK, ending="a", upcoming="b")


def _book_future(tmp_path):
    from autodj.hour_preview import Item
    from autodj.live_queue import capture_voice_targets

    player, entries, _points, desk, _current, _ = _current_handoff(tmp_path)
    player._state.queue[:] = entries[1:]
    targets = capture_voice_targets(
        player,
        tuple(
            Item("song", time.time() + i * 20, 20, "queued", entry)
            for i, entry in enumerate(entries)
        ),
        now=time.time(),
    )
    target = targets[-1]
    item = _voice(desk, target.target_id)
    item.run_id = target.run_id
    player.book_break(item, transition_revision=target.revision)
    return player, entries, desk, item


def _promote_pair(player, entries):
    outgoing, incoming = entries[-2:]
    player._render_live_sources = (
        outgoing,
        player._live_file(outgoing),
        incoming,
        player._live_file(incoming),
    )
    track = player._render_track(outgoing, incoming, 0)
    player._render_live_sources = None
    track = replace(
        track, live_source=player._live_file(outgoing), next_live_source=player._live_file(incoming)
    )
    player._playing_render = track
    player.bus._current = track
    player.bus._pos = 0
    player._state.current_track = outgoing
    player._state.queue[:] = [incoming]
    player._live_voice_slot = None


def test_future_voice_promotes_once_only_for_its_exact_pair(tmp_path):
    """ADR-0004-25: future voice promotes once only for its exact pair."""
    player, entries, desk, item = _book_future(tmp_path)
    assert desk.get(item.break_id).state == "queued"
    player._maintain_live_handoff(None)
    assert desk.get(item.break_id).state == "queued"
    assert not player._playing_render.break_events

    _promote_pair(player, entries)
    player._maintain_live_handoff(None)
    assert desk.get(item.break_id).state == "rendered"
    assert [event[1] for event in player._playing_render.break_events] == [item.break_id]
    player._maintain_live_handoff(None)
    assert [event[1] for event in player._playing_render.break_events] == [item.break_id]
    player.cancel_voice_track(item.break_id)
    assert desk.get(item.break_id).state == "dropped"
    assert not player._playing_render.break_events


def test_future_claim_survives_stale_preparation_and_retries_once(tmp_path, monkeypatch):
    """ADR-0004-25: future claim survives stale preparation and retries once."""
    player, entries, desk, item = _book_future(tmp_path)
    _promote_pair(player, entries)
    render = player._render_track
    calls = []

    def stale_once(*args, **kwargs):
        rendered = render(*args, **kwargs)
        if not calls:
            player._live_queue_revision += 1
        calls.append(True)
        return rendered

    monkeypatch.setattr(player, "_render_track", stale_once)
    player._maintain_live_handoff(None)
    assert desk.get(item.break_id).state == "queued"
    assert not player._playing_render.break_events
    player._maintain_live_handoff(None)
    assert desk.get(item.break_id).state == "rendered"
    assert [event[1] for event in player._playing_render.break_events] == [item.break_id]


def test_rebind_cap_counts_independent_speech_and_upload_reservations(tmp_path):
    """ADR-0004-24: rebind cap counts independent speech and upload reservations."""
    _player, _entries, desk, item = _book_future(tmp_path)
    destination = "destination"
    independent = _voice(desk, destination)
    independent.source = "announcer"
    desk.add(independent)
    reservation = new_id()
    desk.reserve(reservation, destination, 10, "upload")
    assert not desk.can_rebind_voice({item.break_id: destination})
    desk.release(reservation)
    assert desk.can_rebind_voice({item.break_id: destination})


def test_failed_future_claim_keeps_the_current_liner_reservation(tmp_path, monkeypatch):
    """ADR-0004-25: rejected preparation preserves the installed reservation."""
    from types import SimpleNamespace

    from autodj.breaks import TransitionItem

    player, entries, desk, item = _book_future(tmp_path)
    item.transition_items += (
        TransitionItem("chosen", "liner", "automatic", "Chosen clip", entry=entries[0]),
    )
    item.transition_order = ("chosen", f"break:{item.break_id}")
    _promote_pair(player, entries)
    key = f"{entries[-2].path}\n{entries[-1].path}"
    track = replace(player._playing_render, liner_plan_key=key)
    player._playing_render = player.bus._current = track
    released = []
    player.liner_scheduler = SimpleNamespace(
        reserve_frozen_handoff=lambda *_a, **_kw: True, release_plan=released.append
    )
    render = player._render_track

    def race(*args, **kwargs):
        result = render(*args, **kwargs)
        player._live_queue_revision += 1
        return result

    monkeypatch.setattr(player, "_render_track", race)
    player._maintain_live_handoff(None)
    assert desk.get(item.break_id).state == "queued"
    assert player._playing_render is track
    assert released == []


def test_future_upload_promoted_during_decode_obeys_current_cutoff(tmp_path):
    """ADR-0004-25: future upload promoted during decode obeys current cutoff."""
    from autodj.live_queue import resolve_voice_target

    player, entries, desk, booked = _book_future(tmp_path)
    player.cancel_voice_track(booked.break_id)
    _promote_pair(player, entries)
    player.bus._pos = round(19 * SAMPLE_RATE)
    target = resolve_voice_target(player, booked.slot_id)
    item = _voice(desk, target.target_id)
    item.run_id = target.run_id
    with pytest.raises(BreakError, match="no longer open"):
        player.book_break(item, transition_revision=target.revision)
    assert desk.get(item.break_id) is None or desk.get(item.break_id).state == "dropped"


def test_future_booking_checks_complete_chain_expiry(tmp_path):
    from autodj.live_queue import resolve_voice_target

    player, _entries, desk, booked = _book_future(tmp_path)
    target = resolve_voice_target(player, booked.slot_id)
    second = _voice(desk, target.target_id, seconds=1)
    second.run_id = target.run_id
    second.placement = "standalone"
    second.expires_at = target.estimated_airtime + 1.5
    with pytest.raises(BreakError, match="expire"):
        player.book_break(second, transition_revision=target.revision)
    assert desk.get(second.break_id) is None
    assert desk.get(booked.break_id).state == "queued"


def test_rejected_final_target_check_leaves_submitted_break_unchanged(tmp_path, monkeypatch):
    from copy import deepcopy

    from autodj.live_queue import resolve_voice_target

    player, _entries, desk, booked = _book_future(tmp_path)
    target = resolve_voice_target(player, booked.slot_id)
    item = _voice(desk, target.target_id)
    item.run_id = target.run_id
    before = deepcopy(vars(item))
    order = player._voice_transition_order

    def race(*args):
        value = order(*args)
        player._live_queue_revision += 1
        return value

    monkeypatch.setattr(player, "_voice_transition_order", race)
    with pytest.raises(BreakError):
        player.book_break(item, transition_revision=target.revision)
    assert vars(item) == before
    assert desk.get(item.break_id) is None


def test_predicted_target_can_be_inspected_but_not_booked(tmp_path):
    from autodj.live_queue import resolve_voice_target
    from autodj.live_voice import future_plan

    player, _entries, desk, booked = _book_future(tmp_path)
    player.cancel_voice_track(booked.break_id)
    target = resolve_voice_target(player, booked.slot_id)
    target = replace(target, selection="predicted")
    player._voice_targets[target.target_id] = target
    plan, inspected = future_plan(player, target.target_id)
    assert inspected.selection == "predicted"
    item = _voice(desk, plan.slot_id)
    item.run_id = target.run_id
    with pytest.raises(BreakError, match="Confirm songs"):
        player.book_break(item, transition_revision=target.revision)


@pytest.mark.parametrize("failure", ("reservation", "render"))
def test_future_promotion_refuses_unavailable_frozen_liner(tmp_path, monkeypatch, failure):
    """ADR-0004-25: future promotion refuses unavailable frozen liner."""
    from types import SimpleNamespace

    from autodj.breaks import TransitionItem
    from autodj.hour_preview import Item
    from autodj.live_queue import capture_voice_targets

    player, entries, _points, desk, _current, _ = _current_handoff(tmp_path)
    player._state.queue[:] = entries[1:]
    target = capture_voice_targets(
        player,
        tuple(
            Item("song", time.time() + i * 20, 20, "queued", entry)
            for i, entry in enumerate(entries)
        ),
        now=time.time(),
    )[-1]
    candidate = TransitionItem(
        "liner:chosen", "liner", "automatic", "Chosen clip", entry=entries[0]
    )
    target = replace(target, transition_items=(candidate,))
    player._voice_targets[target.target_id] = target
    item = _voice(desk, target.target_id)
    item.run_id = target.run_id
    player.book_break(
        item,
        transition_revision=target.revision,
        transition_choices=({"id": candidate.item_id, "action": "after"},),
    )
    released = []
    player.liner_scheduler = SimpleNamespace(
        reserve_frozen_handoff=lambda *_args, **_kw: failure == "render",
        release_plan=released.append,
    )
    _promote_pair(player, entries)
    render = player._render_track
    if failure == "render":
        monkeypatch.setattr(
            player,
            "_render_track",
            lambda *args, **kwargs: replace(render(*args, **kwargs), liner_events=()),
        )
    player._maintain_live_handoff(None)
    assert desk.get(item.break_id).state == "dropped"
    assert "candidate" in desk.get(item.break_id).reason.lower()
    assert not player._playing_render.break_events
    assert bool(released) == (failure == "render")


def test_future_cancel_serializes_with_desk_take_and_removes_installed_audio(tmp_path, monkeypatch):
    """ADR-0004-25: future cancel serializes with desk take and removes installed audio."""
    import concurrent.futures
    import threading

    player, entries, desk, item = _book_future(tmp_path)
    _promote_pair(player, entries)
    claimed, proceed, cancel_started, cancelled = (threading.Event() for _ in range(4))
    take = desk.take

    def blocked_take(*args):
        claimed.set()
        assert proceed.wait(5)
        return take(*args)

    def cancel_take():
        cancel_started.set()
        result = player.cancel_voice_track(item.break_id)
        cancelled.set()
        return result

    monkeypatch.setattr(desk, "take", blocked_take)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        promotion = pool.submit(player._maintain_live_handoff, None)
        try:
            assert claimed.wait(3)
            cancellation = pool.submit(cancel_take)
            assert cancel_started.wait(3)
            assert not cancelled.wait(0.1)
        finally:
            proceed.set()
        promotion.result(5)
        assert cancellation.result(5).state == "dropped"
    assert not player._playing_render.break_events


@pytest.mark.parametrize("path", ("legacy", "live_edit"))
def test_production_desk_claims_hold_the_cancellation_lock(tmp_path, monkeypatch, path):
    from autodj.live_queue import QueueEdit
    from tests.unit._break_station import start_station

    if path == "legacy":
        player, entries, _points, desk = make_station(tmp_path)
        _a, b, plan = start_station(player, entries)
        player.book_break(_voice(desk, plan.slot_id))
        player._on_track_start(b)
        run = player._next_rendered
    else:
        player, entries, _points, desk, current, _plan = _current_handoff(tmp_path)
        item = _voice(desk, "retained")
        desk.add(item)
        player._state.queue[:] = entries[2:]
        player._live_queue_edits[id(current.entry)] = QueueEdit(
            current, entries[2], "keep", (item,), None
        )
        player._live_queue_target = current
        player._live_queue_dirty = True

        def run():
            return player._maintain_live_handoff(None)

    take = desk.take
    observed = []

    def checked_take(*args):
        observed.append(player._clock_selection_lock._is_owned())
        return take(*args)

    monkeypatch.setattr(desk, "take", checked_take)
    run()
    assert observed and all(observed)


def test_current_live_voice_books_before_outro_and_keeps_future_slot(tmp_path) -> None:
    """ADR-0004-11: streaming voice booking uses the current handoff before outro."""
    player, entries, _points, desk, current, future_plan = _current_handoff(tmp_path)
    plan, closes, _airs = player.voice_slot_status()
    assert plan is not None
    assert closes == pytest.approx(8.0, abs=0.03)
    assert plan.ending is entries[0] and plan.upcoming is entries[1]

    position = SAMPLE_RATE
    before = current.audio.read(0, position)
    player.bus._pos = position
    item = _voice(desk, plan.slot_id)
    result = player.book_break(
        item,
        transition_revision=plan.transition_revision,
        transition_choices=(),
    )

    aired_plan = player._playing_render
    assert result.slot_id == plan.slot_id
    assert aired_plan is not None and isinstance(aired_plan.audio, LiveAudio)
    assert aired_plan.entry is entries[0] and aired_plan.next_entry is entries[1]
    assert len(aired_plan.break_events) == 1
    start, break_id, end = aired_plan.break_events[0]
    # Booking is allowed until the outgoing outro; playback can be at the
    # segue, after the outro has finished.
    assert break_id == item.break_id and start == round(10.0 * SAMPLE_RATE)
    assert end > start
    assert np.max(np.abs(aired_plan.audio.read(start, end - start)[:, 1])) > 0
    np.testing.assert_array_equal(aired_plan.audio.read(0, position), before)
    assert desk.get(item.break_id).state == "rendered"
    assert player._slot_plan is future_plan
    assert player._slot_plan.ending is entries[2]
    assert player._slot_plan.upcoming is entries[3]


def test_live_voice_cutoff_respects_rate_and_started_speech(tmp_path) -> None:
    player, _entries, _points, _desk, current, _future_plan = _current_handoff(tmp_path)

    # At rate 2, an 8-second source outro after a 1-second source offset is
    # only 3.5 seconds away in the output timeline.
    rate_changed = replace(current, start_offset=SAMPLE_RATE, playback_rate=2.0)
    player._playing_render = rate_changed
    player.bus._current = rate_changed
    player.bus._pos = round(3.6 * SAMPLE_RATE)
    assert player.voice_slot_status()[0] is None

    # A started speech chain closes the edit window before its duck envelope.
    speech_started = replace(
        current,
        playback_rate=1.5,
        start_offset=SAMPLE_RATE,
        break_events=((round(4.5 * SAMPLE_RATE), "heard", round(5.5 * SAMPLE_RATE)),),
    )
    player._playing_render = speech_started
    player.bus._current = speech_started
    player.bus._pos = round(4.3 * SAMPLE_RATE)
    assert player.voice_slot_status()[0] is None


def test_live_voice_rejects_stale_slot_without_changing_audio(tmp_path) -> None:
    player, _entries, _points, desk, current, _future_plan = _current_handoff(tmp_path)
    plan, _closes, _airs = player.voice_slot_status()
    assert plan is not None
    before = current.audio.read(0, round(2 * SAMPLE_RATE))
    player.bus._pos = round(8 * SAMPLE_RATE)
    item = _voice(desk, plan.slot_id)

    with pytest.raises(BreakError, match="no longer open") as caught:
        player.book_break(
            item,
            transition_revision=plan.transition_revision,
            transition_choices=(),
        )

    assert caught.value.reason == "slot_closed"
    assert desk.get(item.break_id) is None
    assert player._playing_render is current and player.bus._current is current
    np.testing.assert_array_equal(current.audio.read(0, len(before)), before)


def test_live_voice_does_not_follow_a_track_change_during_admission(tmp_path, monkeypatch):
    from autodj import live_voice

    player, entries, _points, desk, current, _future_plan = _current_handoff(tmp_path)
    plan, _closes, _airs = player.voice_slot_status()
    assert plan is not None
    status = live_voice.slot_status
    following = replace(current, entry=entries[1], next_entry=entries[2])

    def status_then_advance(player):
        result = status(player)
        player._playing_render = player.bus._current = following
        return result

    monkeypatch.setattr(live_voice, "slot_status", status_then_advance)
    item = _voice(desk, plan.slot_id)
    with pytest.raises(BreakError, match="no longer open"):
        player.book_break(item, transition_revision=plan.transition_revision)
    assert desk.get(item.break_id) is None
    assert player.bus._current is following


@pytest.mark.parametrize("race", ("queue_revision", "outro_cutoff"))
def test_live_voice_render_race_drops_item_and_keeps_old_audio(tmp_path, monkeypatch, race) -> None:
    """ADR-0004-11: final admission rejects an elapsed cutoff or changed queue."""
    player, _entries, _points, desk, current, _future_plan = _current_handoff(tmp_path)
    plan, _closes, _airs = player.voice_slot_status()
    assert plan is not None
    position = SAMPLE_RATE
    before = current.audio.read(0, position)
    player.bus._pos = position
    item = _voice(desk, plan.slot_id)
    render = player._render_track

    def render_then_race(*args, **kwargs):
        result = render(*args, **kwargs)
        if race == "queue_revision":
            player._live_queue_revision += 1
        else:
            player.bus._pos = round(8 * SAMPLE_RATE)
        return result

    monkeypatch.setattr(player, "_render_track", render_then_race)
    with pytest.raises(BreakError, match="no longer open") as caught:
        player.book_break(
            item,
            transition_revision=plan.transition_revision,
            transition_choices=(),
        )

    assert caught.value.reason == "slot_closed"
    dropped = desk.get(item.break_id)
    assert dropped is not None and dropped.state == "dropped"
    assert player._playing_render is current and player.bus._current is current
    np.testing.assert_array_equal(current.audio.read(0, position), before)


def test_live_voice_reset_leaves_future_voice_queued_until_track_is_current(tmp_path) -> None:
    player, entries, _points, desk, current, _future_plan = _current_handoff(tmp_path)
    plan, _closes, _airs = player.voice_slot_status()
    assert plan is not None
    player.bus._pos = SAMPLE_RATE
    item = _voice(desk, plan.slot_id)
    future = _voice(desk, "future-slot")
    desk.add(future)
    ready = RenderedTrack(
        entries[2],
        current.audio,
        None,
        0,
        "",
        handoff_pending=True,
    )
    player._playout = _FakePlayout(ready)

    player.book_break(
        item,
        transition_revision=plan.transition_revision,
        transition_choices=(),
    )

    assert id(entries[2]) not in player._live_queue_edits
    assert desk.get(future.break_id).state == "queued"
    assert desk.get(item.break_id).state == "rendered"


@pytest.mark.parametrize("rebuild_plan", (False, True))
def test_live_voice_cancel_removes_unheard_audio_without_preparing_ready_voice(
    tmp_path, rebuild_plan
) -> None:
    """ADR-0004-11: unheard installed speech can be removed without changing heard audio."""
    from autodj import live_voice

    player, entries, _points, desk, current, _future_plan = _current_handoff(tmp_path)
    plan, _closes, _airs = player.voice_slot_status()
    assert plan is not None
    item = _voice(desk, plan.slot_id)
    player.book_break(
        item,
        transition_revision=plan.transition_revision,
        transition_choices=(),
    )
    current = player._playing_render
    assert current is not None
    player.bus._pos = position = round(9.0 * SAMPLE_RATE)
    ready = RenderedTrack(entries[2], current.audio, None, 0, "", handoff_pending=True)
    player._playout = _FakePlayout(ready)
    before = current.audio.read(0, position)
    current_plan = player._live_voice_slot[1]
    assert player.bus._pos > round(8.0 * SAMPLE_RATE)  # The current song's saved outro.
    without_voice = live_voice._render_handoff(
        player,
        current,
        (),
        tuple(key for key in current_plan.transition_order if key != f"break:{item.break_id}"),
        tuple(
            content
            for content in current_plan.transition_items
            if content.item_id != f"break:{item.break_id}"
        ),
    )
    assert without_voice is not None

    if rebuild_plan:
        player._live_voice_slot = None
    assert player.can_cancel_voice_track(item.break_id)
    cancelled = player.cancel_voice_track(item.break_id)
    replacement = player._playing_render

    assert cancelled.state == "dropped"
    assert replacement is not None and replacement is not current
    assert replacement.entry is entries[0] and replacement.next_entry is entries[1]
    assert all(event[1] != item.break_id for event in replacement.break_events)
    assert all(voice.break_id != item.break_id for voice in replacement.voice_breaks)
    np.testing.assert_array_equal(replacement.audio.read(0, position), before)
    np.testing.assert_array_equal(
        replacement.audio.read(position, SAMPLE_RATE),
        without_voice.audio.read(position, SAMPLE_RATE),
    )
    assert player._playout.ready() is ready
    assert id(entries[2]) not in player._live_queue_edits
    assert not player.can_cancel_voice_track(item.break_id)


@pytest.mark.parametrize("race", ("queue_revision", "voice_start"))
def test_live_voice_cancel_rejects_races_without_dropping_audio(tmp_path, monkeypatch, race):
    player, _entries, _points, desk, current, _future_plan = _current_handoff(tmp_path)
    plan, _closes, _airs = player.voice_slot_status()
    assert plan is not None
    player.bus._pos = SAMPLE_RATE
    item = _voice(desk, plan.slot_id)
    player.book_break(
        item,
        transition_revision=plan.transition_revision,
        transition_choices=(),
    )
    current = player._playing_render
    assert current is not None
    before = current.audio.read(0, SAMPLE_RATE)
    assert player.can_cancel_voice_track(item.break_id)
    render = player._render_track

    def render_then_race(*args, **kwargs):
        result = render(*args, **kwargs)
        if race == "queue_revision":
            player._live_queue_revision += 1
        else:
            player.bus._pos = next(
                start for start, identity, _end in current.break_events if identity == item.break_id
            )
        return result

    monkeypatch.setattr(player, "_render_track", render_then_race)
    with pytest.raises(BreakError, match="no longer open"):
        player.cancel_voice_track(item.break_id)

    assert desk.get(item.break_id).state == "rendered"
    assert player._playing_render is current and player.bus._current is current
    np.testing.assert_array_equal(current.audio.read(0, SAMPLE_RATE), before)
    if race == "voice_start":
        assert not player.can_cancel_voice_track(item.break_id)


def test_live_voice_cancel_replaces_exact_ready_occurrence_and_keeps_other_speech(tmp_path):
    """ADR-0004-11: ready cancellation preserves occurrence identity and other speech."""
    player, entries, _points, desk, current, _future_plan = _current_handoff(tmp_path)
    target = _voice(desk, "ready-slot")
    sibling = _voice(desk, "ready-slot")
    desk.add(target)
    desk.add(sibling)
    rendered_breaks = tuple(desk.take("ready-slot", 0.0))
    target, sibling = (
        next(item for item in rendered_breaks if item.break_id == item_id)
        for item_id in (target.break_id, sibling.break_id)
    )
    repeated_entry = replace(entries[0])
    ready = RenderedTrack(
        repeated_entry,
        current.audio,
        entries[1],
        1234,
        "",
        next_pick_mode="clock",
        next_from_queue=True,
        next_clock_cursor=17,
        next_clock_boundary=321.5,
        next_clock_name="overnight",
        next_clock_step={"step": 4},
        next_clock_revision=8,
        handoff_id="stable-live-handoff",
        break_events=(
            (round(10 * SAMPLE_RATE), target.break_id, round(12 * SAMPLE_RATE)),
            (round(12 * SAMPLE_RATE), sibling.break_id, round(14 * SAMPLE_RATE)),
        ),
        live_source=player._live_file(repeated_entry),
        next_live_source=player._live_file(entries[1]),
        voice_breaks=rendered_breaks,
    )
    playout = _FakePlayout(ready)
    player._playout = playout

    assert ready.entry.path == current.entry.path and ready.entry is not current.entry
    assert player.can_cancel_voice_track(target.break_id)
    cancelled = player.cancel_voice_track(target.break_id)
    replacement = playout.ready()

    assert cancelled.state == "dropped"
    assert replacement is not None and replacement is not ready
    assert replacement.entry is repeated_entry and replacement.next_entry is entries[1]
    assert replacement.handoff_id == "stable-live-handoff"
    assert replacement.next_pick_mode == "clock" and replacement.next_from_queue
    assert replacement.live_source is ready.live_source
    assert replacement.next_live_source is ready.next_live_source
    assert player._pending_entry is entries[1]
    assert player._pending_offset == replacement.next_start_offset
    assert player._pending_pick_mode == "clock" and player._pending_from_queue
    assert player._pending_live_source is ready.next_live_source
    assert player._pending_clock_cursor == 17
    assert player._pending_clock_boundary == 321.5
    assert player._pending_clock_name == "overnight"
    assert player._pending_clock_step == {"step": 4}
    assert player._pending_clock_cursor_revision == 8
    assert all(event[1] != target.break_id for event in replacement.break_events)
    assert any(event[1] == sibling.break_id for event in replacement.break_events)
    assert all(item.break_id != target.break_id for item in replacement.voice_breaks)
    assert any(item.break_id == sibling.break_id for item in replacement.voice_breaks)
    assert desk.get(sibling.break_id).state == "rendered"
    edit = player._live_queue_edits[id(repeated_entry)]
    assert tuple(item.break_id for item in edit.breaks) == (sibling.break_id,)


def test_live_voice_cancel_ignores_unprepared_ready_continuation(tmp_path):
    """A body-only ready track has no cancellable future speech slot."""
    player, entries, _points, desk, current, _future_plan = _current_handoff(tmp_path)
    item = _voice(desk, "ready-slot")
    desk.add(item)
    item = desk.take("ready-slot", 0.0)[0]
    ready = RenderedTrack(
        entries[2],
        current.audio,
        None,
        0,
        "",
        live_source=player._live_file(entries[2]),
        next_live_source=player._live_file(entries[3]),
        break_events=((round(10 * SAMPLE_RATE), item.break_id, round(12 * SAMPLE_RATE)),),
        voice_breaks=(item,),
        handoff_pending=True,
    )
    playout = _FakePlayout(ready)
    player._playout = playout
    before = ready.audio.read(0, SAMPLE_RATE)
    assert not player.can_cancel_voice_track(item.break_id)
    with pytest.raises(BreakError, match="can no longer be changed"):
        player.cancel_voice_track(item.break_id)

    assert playout.ready() is ready
    assert desk.get(item.break_id).state == "rendered"
    np.testing.assert_array_equal(ready.audio.read(0, SAMPLE_RATE), before)


def test_live_voice_cancel_keeps_other_current_speech(tmp_path):
    player, _entries, _points, desk, _current, _future_plan = _current_handoff(tmp_path)
    booked = []
    for _ in range(2):
        plan, _closes, _airs = player.voice_slot_status()
        assert plan is not None
        item = _voice(desk, plan.slot_id)
        player.book_break(item, transition_revision=plan.transition_revision)
        booked.append(item)
    player.cancel_voice_track(booked[0].break_id)
    current = player._playing_render
    assert [event[1] for event in current.break_events] == [booked[1].break_id]
    assert [item.break_id for item in current.voice_breaks] == [booked[1].break_id]
    assert desk.get(booked[0].break_id).state == "dropped"
    assert desk.get(booked[1].break_id).state == "rendered"


@pytest.mark.parametrize("operation", ["reset", "stop", "new_set", "end_set"])
def test_future_bindings_end_with_the_station_run(tmp_path, monkeypatch, operation):
    """ADR-0004-26: station lifecycle invalidates bindings without restoring temporary speech."""
    from autodj.live_queue import resolve_voice_target

    player, entries, desk, item = _book_future(tmp_path)
    old_run = player._voice_run_id
    monkeypatch.setattr(player._playout, "stop", lambda **kwargs: None, raising=False)
    desk.on_air(item.break_id)
    if operation == "reset":
        player.reset_playout(None)
    elif operation == "new_set":
        player.begin_set(entries[0], "queue")
    elif operation == "end_set":
        player.end_set()
    else:
        player.stop()
    assert player._voice_run_id != old_run
    assert not player._voice_targets
    assert desk.get(item.break_id).state == "dropped"
    assert desk.get(item.break_id).reason.startswith("cut short:")
    with pytest.raises(BreakError):
        resolve_voice_target(player, item.slot_id)


def test_schedule_change_marks_confirmed_target_at_risk(tmp_path):
    """ADR-0004-26: new hard boundaries change the revision and flag existing bindings."""
    from datetime import UTC, datetime, timedelta

    from autodj.breaks_api import _voice_target_views
    from autodj.live_queue import resolve_voice_target
    from autodj.scheduler import ScheduledEvent

    player, _entries, _desk, item = _book_future(tmp_path)
    player._hour_fit_timezone = "UTC"
    before = resolve_voice_target(player, item.slot_id)
    player._hour_fit_events = (
        ScheduledEvent(
            id="b" * 32,
            name="Boundary",
            action="show",
            repeat="once",
            timing="hard",
            date=(datetime.now(UTC) + timedelta(minutes=1)).date(),
            time=(datetime.now(UTC) + timedelta(minutes=1)).time().replace(second=0, microsecond=0),
        ),
    )
    after = resolve_voice_target(player, item.slot_id)
    assert after.revision != before.revision
    row = next(row for row in _voice_target_views(player) if row["target_id"] == item.slot_id)
    assert row["at_risk"]
    assert "schedule" in row["reason"]


def test_future_promotion_rechecks_schedule_at_final_install(tmp_path, monkeypatch):
    """ADR-0004-26: a schedule edit after fit checking cannot install stale speech."""
    from datetime import time as clock_time

    from autodj import live_voice
    from autodj.scheduler import ScheduledEvent

    player, entries, desk, item = _book_future(tmp_path)
    _promote_pair(player, entries)
    install = live_voice._install_handoff

    def change_before_install(*args, **kwargs):
        player._hour_fit_events = (
            ScheduledEvent(
                id="c" * 32,
                name="New boundary",
                action="show",
                repeat="daily",
                timing="hard",
                time=clock_time(12),
            ),
        )
        return install(*args, **kwargs)

    monkeypatch.setattr(live_voice, "_install_handoff", change_before_install)
    player._maintain_live_handoff(None)
    assert not player._playing_render.break_events
    assert desk.get(item.break_id).state == "dropped"
    assert "schedule" in desk.get(item.break_id).reason.lower()


def test_pause_past_future_expiry_drops_speech_without_replay(tmp_path):
    """ADR-0004-27: elapsed estimates never claim airing; expiry wins after a pause."""
    player, entries, desk, item = _book_future(tmp_path)
    item.expires_at = time.time() - 1
    _promote_pair(player, entries)
    player._maintain_live_handoff(None)
    assert desk.get(item.break_id).state == "expired"
    assert desk.get(item.break_id).aired_at is None
    assert not player._playing_render.break_events


def test_waiting_future_voice_prevents_competing_automatic_choice(tmp_path, monkeypatch):
    """ADR-0004-25: frozen candidates are reserved by promotion, never rerolled beforehand."""
    player, entries, _desk, _item = _book_future(tmp_path)

    def competing(*args, **kwargs):
        pytest.fail("normal chooser must not compete with a booked frozen transition")

    monkeypatch.setattr(player, "_auto_liner", competing)
    monkeypatch.setattr(player, "_matched_intro", competing)
    assert player._transition_voice(entries[-2], entries[-1], (), (), time.time()) == ((), "", None)


def test_future_promotion_adopts_its_existing_intro_reservation(tmp_path):
    """ADR-0004-25: the same handoff's positive cooldown cannot reject its own frozen intro."""
    from autodj.breaks import TransitionItem
    from tests.unit.test_matched_intros import _add, _store

    player, entries, desk, item = _book_future(tmp_path)
    store = _store(tmp_path)
    identity = _add(store, cooldown_minutes=30)
    intro = store.choose("song-a", "", time.time())
    assert intro is not None and intro.intro_id == identity
    player.matched_intros = store
    player._library.entry_for_path = lambda path: None
    item.transition_items = (
        TransitionItem("intro:kept", "matched_intro", "matched_intro", "Frozen intro", intro=intro),
        *item.transition_items,
    )
    item.transition_order = ("intro:kept", f"break:{item.break_id}")
    _promote_pair(player, entries)
    current = replace(
        player._playing_render,
        incoming_intro=intro,
        intro_events=((0, identity, SAMPLE_RATE, "Frozen intro"),),
    )
    player._playing_render = player.bus._current = current
    player._maintain_live_handoff(None)
    assert desk.get(item.break_id).state == "rendered"
    assert [row[1] for row in player._playing_render.break_events] == [item.break_id]
    player._maintain_live_handoff(None)
    assert len(player._playing_render.break_events) == 1


def test_crash_style_recreation_does_not_restore_future_bindings(tmp_path):
    """ADR-0004-26: recreation cannot restore temporary speech or claim old airtime."""
    from autodj.live_queue import resolve_voice_target

    old, _entries, _desk, item = _book_future(tmp_path)
    fresh, _entries, _points, desk = make_station(tmp_path)
    assert fresh._voice_run_id != old._voice_run_id
    assert not fresh._voice_targets
    assert not desk.listing()
    with pytest.raises(BreakError):
        resolve_voice_target(fresh, item.slot_id)


@pytest.mark.parametrize("future", [False, True])
@pytest.mark.parametrize("failure", ["render", "expiry", "boundary"])
def test_failed_live_booking_returns_claimed_siblings(tmp_path, monkeypatch, future, failure):
    """ADR-0004-25: failed live booking returns claimed siblings."""
    from autodj import live_voice

    player, _entries, _points, desk, current, _ = _current_handoff(tmp_path)
    plan, _, _ = player.voice_slot_status()
    sibling = _voice(desk, plan.slot_id)
    desk.add(sibling)
    added = _voice(desk, plan.slot_id)
    if future:
        desk.add(added)
    if failure == "render":
        monkeypatch.setattr(live_voice, "_render_handoff", lambda *_a, **_kw: None)
    elif failure == "expiry":
        added.expires_at = time.time() - 1
    else:

        def reject_install(*_args, **_kwargs):
            raise BreakError(409, "The schedule changed", "scheduled_boundary")

        monkeypatch.setattr(live_voice, "_install_handoff", reject_install)
    with pytest.raises(BreakError):
        live_voice.book_live_breaks(player, (added,), plan.slot_id, already_queued=future)
    assert desk.get(sibling.break_id).state == "queued"
    assert player._playing_render is current
    assert not current.break_events
    assert desk.get(added.break_id).state == (
        "expired" if failure == "expiry" else "queued" if future else "dropped"
    )


@pytest.mark.parametrize("promotion", [False, True])
def test_failed_future_install_restores_cached_plan(tmp_path, monkeypatch, promotion):
    """ADR-0004-25: failed future install restores cached plan."""
    from autodj import live_voice
    from autodj.live_queue import resolve_voice_target

    player, entries, desk, booked = _book_future(tmp_path)
    _promote_pair(player, entries)
    player.voice_slot_status()
    previous = player._live_voice_slot
    monkeypatch.setattr(live_voice, "_render_handoff", lambda *_a, **_kw: None)
    if promotion:
        live_voice.promote_future_voice(player)
    else:
        target = resolve_voice_target(player, booked.slot_id)
        item = _voice(desk, target.target_id)
        item.run_id = target.run_id
        with pytest.raises(BreakError):
            player.book_break(item, transition_revision=target.revision)
    assert player._live_voice_slot is previous
    assert not player._playing_render.break_events


def test_paused_future_target_has_unknown_airtime_without_schedule_risk(tmp_path):
    """ADR-0004-26: paused future target has unknown airtime without schedule risk."""
    from autodj.breaks_api import _voice_target_views

    player, _entries, _desk, item = _book_future(tmp_path)
    player._state.is_paused = True
    row = next(row for row in _voice_target_views(player) if row["target_id"] == item.slot_id)
    assert row["estimated_airtime"] is None
    assert row["at_risk"] is False


def test_future_upload_catalog_reads_precede_bus_guard(tmp_path, monkeypatch):
    """ADR-0004-23: future upload catalog reads precede bus guard."""
    from contextlib import contextmanager

    from autodj.live_queue import resolve_voice_target

    player, _entries, desk, booked = _book_future(tmp_path)
    held = False
    guard = player.bus.queue_edit_guard

    @contextmanager
    def checked_guard():
        nonlocal held
        with guard():
            held = True
            try:
                yield
            finally:
                held = False

    def versions():
        assert not held, "catalog query under audio boundary guard"
        return (1, 2)

    monkeypatch.setattr(player.bus, "queue_edit_guard", checked_guard)
    player._library.catalog.change_versions = versions
    target = resolve_voice_target(player, booked.slot_id)
    item = _voice(desk, target.target_id)
    item.run_id = target.run_id
    player.book_break(item, transition_revision=target.revision)
    assert desk.get(item.break_id).state == "queued"


def test_expired_future_intro_measurement_leaves_no_reservation_or_cooldown(tmp_path):
    """ADR-0004-25: expired future intro measurement leaves no reservation or cooldown."""
    from autodj.breaks import TransitionItem
    from autodj.live_queue import resolve_voice_target
    from tests.unit.test_matched_intros import _add, _store

    player, _entries, desk, booked = _book_future(tmp_path)
    store = _store(tmp_path)
    identity = _add(store, cooldown_minutes=30)
    intro = store.choose("song-a", "", time.time())
    store.release((identity,))
    player.matched_intros = store
    target = resolve_voice_target(player, booked.slot_id)
    candidate = TransitionItem(
        "intro:frozen", "matched_intro", "matched_intro", "Intro", intro=intro
    )
    booked.transition_items = (candidate, *booked.transition_items)
    booked.transition_order = ()
    item = _voice(desk, target.target_id)
    item.run_id = target.run_id
    item.expires_at = target.estimated_airtime + 0.1
    with pytest.raises(BreakError, match="expire"):
        player.book_break(
            item,
            transition_revision=target.revision,
            transition_choices=({"id": candidate.item_id, "action": "after"},),
        )
    # A real subsequent choice remains available despite the positive cooldown.
    assert store.choose("song-a", "", time.time()).intro_id == identity
    assert desk.get(item.break_id) is None


def test_queue_conflict_catalog_reads_precede_bus_guard(tmp_path, monkeypatch):
    """ADR-0004-24: queue conflict catalog reads precede bus guard."""
    from contextlib import contextmanager

    from autodj._bridge import PlayerBridge
    from autodj.live_queue import VoiceQueueConflict

    player, _entries, _desk, _booked = _book_future(tmp_path)
    bridge = PlayerBridge(player, player._sim, library=player._library)
    held = False
    guard = player.bus.queue_edit_guard

    @contextmanager
    def checked_guard():
        nonlocal held
        with guard():
            held = True
            try:
                yield
            finally:
                held = False

    def versions():
        assert not held, "catalog query under audio boundary guard"
        return (1, 2)

    monkeypatch.setattr(player.bus, "queue_edit_guard", checked_guard)
    player._library.catalog.change_versions = versions
    with pytest.raises(VoiceQueueConflict):
        bridge.queue_remove(player._state.queue[2].path, 2)


@pytest.mark.parametrize("length", [None, 0, float("nan")])
def test_future_booking_refuses_unplayable_liner_without_partial_changes(
    tmp_path, monkeypatch, length
):
    """ADR-0004-27: future booking refuses unplayable liner without partial changes."""
    from copy import deepcopy

    from autodj.breaks import TransitionItem
    from autodj.live_queue import resolve_voice_target

    player, entries, desk, booked = _book_future(tmp_path)
    liner = replace(entries[0], length=length)
    candidate = TransitionItem("liner:missing-end", "liner", "queued", "Queued liner", entry=liner)
    booked.transition_items = (candidate, *booked.transition_items)
    booked.transition_order = ()
    points = player.saved_points
    monkeypatch.setattr(
        player, "saved_points", lambda entry: {"start": 0} if entry is liner else points(entry)
    )
    target = resolve_voice_target(player, booked.slot_id)
    item = _voice(desk, target.target_id)
    item.run_id = target.run_id
    before = deepcopy(vars(item))
    sibling_before = vars(booked).copy()
    queue_before = list(player._state.queue)
    with pytest.raises(BreakError, match="The complete prefix is no longer playable") as failure:
        player.book_break(
            item,
            transition_revision=target.revision,
            transition_choices=({"id": candidate.item_id, "action": "after"},),
        )
    assert failure.value.status == 409
    assert failure.value.reason == "target_changed"
    assert vars(item) == before
    assert vars(booked) == sibling_before
    assert desk.get(item.break_id) is None
    assert player._state.queue == queue_before


@pytest.mark.parametrize("later_read", ["refresh", "promotion"])
def test_catalog_snapshot_window_ends_before_refresh_or_promotion(
    tmp_path, monkeypatch, later_read
):
    """ADR-0004-22, ADR-0004-25: catalog snapshot window ends before refresh or promotion."""
    from contextlib import contextmanager

    from autodj.breaks_api import _voice_target_views
    from autodj.live_queue import resolve_voice_target
    from autodj.live_voice import promote_future_voice

    player, entries, desk, booked = _book_future(tmp_path)
    _promote_pair(player, entries)
    version = [1]
    reads = []
    held = False
    guard = player.bus.queue_edit_guard

    def versions():
        assert not held, "catalog read under the bus guard"
        reads.append(version[0])
        return {"entries": version[0]}

    @contextmanager
    def change_at_guard():
        nonlocal held
        version[0] = 2
        with guard():
            held = True
            try:
                yield
            finally:
                held = False

    player._library.catalog.change_versions = versions
    before = resolve_voice_target(player, booked.slot_id)
    monkeypatch.setattr(player.bus, "queue_edit_guard", change_at_guard)
    with player.live_queue_guard():
        # Accepted window: the catalog changed after the snapshot read.
        assert resolve_voice_target(player, booked.slot_id).revision == before.revision
    reads.clear()
    if later_read == "refresh":
        row = next(row for row in _voice_target_views(player) if row["target_id"] == booked.slot_id)
        assert row["revision"] != before.revision
    else:
        promote_future_voice(player)
        assert desk.get(booked.break_id).state == "rendered"
        assert player._playing_render.break_events
    assert reads and set(reads) == {2}
