from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from autodj.breaks import VOICE_TRACK, BreakError, SlotPlan, new_id
from autodj.live_audio import LiveAudio
from autodj.mixbus import BusEvents, MixBus, RenderedTrack
from autodj.stereo import SAMPLE_RATE
from tests.unit._break_station import LEVELS, make_break, make_station
from tests.unit.test_live_playout import _UnconsumedDeck


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
    player._render_ahead = SimpleNamespace(ready=lambda: None, reset=lambda callback: callback())
    future_plan = SlotPlan(new_id(), 0, b, 0.0, c, d, "queue", (), None, None, None, None, ())
    player._slot_plan = future_plan
    return player, entries, points, desk, current, future_plan


def _voice(desk, slot_id: str, *, seconds: float = 1.0):
    item = make_break(desk, slot_id, kind="dj", placement="talk_up", seconds=seconds)
    return replace(item, source=VOICE_TRACK, ending="a", upcoming="b")


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


def test_live_voice_reset_preserves_queued_voice_for_future_ready_track(tmp_path) -> None:
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
        entries[3],
        0,
        "",
        voice_breaks=(future,),
    )
    player._render_ahead = SimpleNamespace(ready=lambda: ready, reset=lambda callback: callback())

    player.book_break(
        item,
        transition_revision=plan.transition_revision,
        transition_choices=(),
    )

    edit = player._live_queue_edits[id(entries[2])]
    assert edit.breaks == (future,)
    assert desk.get(future.break_id).state == "queued"
    assert desk.get(item.break_id).state == "rendered"
