"""Generic speech admission on a streaming current-to-next handoff."""

from __future__ import annotations

from dataclasses import replace

import pytest

from autodj.breaks import BreakError
from autodj.stereo import SAMPLE_RATE
from tests.unit._break_station import make_break
from tests.unit.test_live_voice import _current_handoff


def test_live_speech_batch_refusal_rolls_back_without_replacing_existing_audio(tmp_path):
    player, _entries, _points, desk, _track, _future = _current_handoff(tmp_path)
    desk.config.max_per_slot = 3
    plan, _closes, _airs = player.voice_slot_status()
    assert plan is not None

    existing = make_break(desk, plan.slot_id)
    player.book_breaks((existing,), plan.slot_id, transition_revision=plan.transition_revision)
    original = player._playing_render
    assert original is not None
    original_audio = original.audio
    original_events = original.break_events

    batch = tuple(make_break(desk, plan.slot_id) for _ in range(3))
    current_plan, _closes, _airs = player.voice_slot_status()
    assert current_plan is not None
    with pytest.raises(BreakError) as caught:
        player.book_breaks(
            batch, plan.slot_id, transition_revision=current_plan.transition_revision
        )

    assert caught.value.reason == "slot_full"
    assert player._playing_render is original
    assert player._playing_render.audio is original_audio
    assert player._playing_render.break_events == original_events
    assert desk.get(existing.break_id).state == "rendered"
    assert all(desk.get(item.break_id).state == "dropped" for item in batch[:2])
    assert all(desk.get(item.break_id) is None for item in batch[2:])


def test_hard_boundary_fit_covers_the_full_retained_speech_span(tmp_path, monkeypatch):
    player, _entries, _points, desk, _track, _future = _current_handoff(tmp_path)
    plan, _closes, _airs = player.voice_slot_status()
    assert plan is not None
    existing = make_break(desk, plan.slot_id)
    player.book_breaks((existing,), plan.slot_id, transition_revision=plan.transition_revision)
    original = player._playing_render
    assert original is not None

    rendered = []
    render = player._render_track

    def capture_render(*args, **kwargs):
        result = render(*args, **kwargs)
        rendered.append(result)
        return result

    monkeypatch.setattr(player, "_render_track", capture_render)
    checks = []

    def reject_span(_starts_at: float, duration: float) -> bool:
        checks.append(duration)
        return True

    monkeypatch.setattr(player, "voice_track_crosses_hard_boundary", reject_span)
    additional = make_break(desk, plan.slot_id)
    current_plan, _closes, _airs = player.voice_slot_status()
    assert current_plan is not None
    with pytest.raises(BreakError) as caught:
        player.book_breaks(
            (additional,), plan.slot_id, transition_revision=current_plan.transition_revision
        )

    assert caught.value.reason == "scheduled_boundary"
    assert rendered and rendered[-1] is not None
    events = rendered[-1].break_events
    assert {event[1] for event in events} == {existing.break_id, additional.break_id}
    expected_span = (
        max(event[2] for event in events) - min(event[0] for event in events)
    ) / SAMPLE_RATE
    assert checks == pytest.approx([expected_span])
    assert expected_span > max((event[2] - event[0]) / SAMPLE_RATE for event in events)
    assert player._playing_render is original
    assert desk.get(existing.break_id).state == "rendered"
    assert desk.get(additional.break_id).state == "dropped"


def test_live_speech_booking_preserves_scheduled_handoff_metadata(tmp_path):
    player, _entries, _points, desk, track, _future = _current_handoff(tmp_path)
    ad_steps = ({"step": "ad", "break_id": "scheduled-ad"},)
    track = replace(
        track,
        handoff_id="clock-handoff-stable",
        next_pick_mode="clock",
        next_clock_cursor=12,
        next_clock_boundary=456.5,
        next_clock_name="overnight",
        next_clock_step={"hour": 3},
        next_clock_revision=7,
        ad_steps=ad_steps,
    )
    player._playing_render = track
    player.bus._current = track

    plan, _closes, _airs = player.voice_slot_status()
    assert plan is not None
    assert plan.slot_id == "clock-handoff-stable"
    assert plan.mode == "clock"
    assert plan.next_cursor == 12
    assert plan.boundary_after == 456.5
    assert plan.clock_name == "overnight"
    assert plan.clock_step == {"hour": 3}
    assert plan.clock_revision == 7
    assert plan.ad_steps == ad_steps

    speech = make_break(desk, plan.slot_id)
    booked = player.book_breaks(
        (speech,), plan.slot_id, transition_revision=plan.transition_revision
    )
    refreshed, _closes, _airs = player.voice_slot_status()
    assert refreshed is not None
    for result in (booked, refreshed):
        assert result.slot_id == "clock-handoff-stable"
        assert result.mode == "clock"
        assert result.next_cursor == 12
        assert result.boundary_after == 456.5
        assert result.clock_name == "overnight"
        assert result.clock_step == {"hour": 3}
        assert result.clock_revision == 7
        assert result.ad_steps == ad_steps
    assert player._playing_render.handoff_id == "clock-handoff-stable"


def test_same_handoff_id_rejects_revision_after_queue_replan(tmp_path):
    player, entries, _points, desk, original, _future = _current_handoff(tmp_path)
    original = replace(original, handoff_id="same-handoff-id")
    player._playing_render = original
    player.bus._current = original
    before, _closes, _airs = player.voice_slot_status()
    assert before is not None

    decks = {entry.path: player._live_file(entry) for entry in (entries[0], entries[2])}
    player._render_live_sources = (
        entries[0],
        decks[entries[0].path],
        entries[2],
        decks[entries[2].path],
    )
    replanned = player._render_track(entries[0], entries[2], 0)
    player._render_live_sources = None
    assert replanned is not None
    replanned = replace(replanned, handoff_id="same-handoff-id")
    player._playing_render = replanned
    player.bus._current = replanned
    player._live_queue_revision += 1

    after, _closes, _airs = player.voice_slot_status()
    assert after is not None
    assert after.slot_id == before.slot_id == "same-handoff-id"
    assert after.transition_revision != before.transition_revision
    item = make_break(desk, before.slot_id)
    with pytest.raises(BreakError) as caught:
        player.book_breaks((item,), before.slot_id, transition_revision=before.transition_revision)

    assert caught.value.reason == "transition_changed"
    assert desk.get(item.break_id) is None
    assert player._playing_render is replanned
