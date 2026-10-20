"""Breaks in the render: talk-up and standalone timing, ducking, and the slot plan (#88).

The station has four 20-second songs at different left-channel levels and
breaks on the right channel (see ``tests/unit/_break_station.py``), so each
assertion reads one channel to find the music or the voice.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj.breaks import (
    AIRED,
    DROPPED,
    EXPIRED,
    ON_AIR,
    QUEUED,
    RENDERED,
    VOICE_TRACK,
    BreakError,
    new_id,
)
from autodj.stereo import SAMPLE_RATE as SR
from tests.unit._break_station import make_break, make_station, start_station


def at(render, seconds: float) -> np.ndarray:
    """The render's frame at *seconds* of its own song's timeline."""
    return render.audio[round(seconds * SR) - render.start_offset]


def voice_span(render) -> tuple[float, float]:
    """Where the first break's voice starts and its speech ends, in song seconds."""
    first, _id, end = render.break_events[0]
    return (first + render.start_offset) / SR, (end + render.start_offset) / SR


def incoming_entry(render) -> float:
    """Where the next song enters, in song seconds."""
    return (len(render.audio) - render.overlap_frames + render.start_offset) / SR


@pytest.fixture
def station(tmp_path):
    return make_station(tmp_path)


# ---------------------------------------------------------------------------
# Rendering one handoff
# ---------------------------------------------------------------------------


def test_talk_up_ends_the_voice_on_the_next_songs_vocal(station):
    player, (a, b, _c, _d), _points, desk = station
    item = make_break(desk, new_id(), seconds=3.0)
    desk.add(item)
    (taken,) = desk.take(item.slot_id, 0.0)
    render = player._render_track(a, b, 0, breaks=(taken,))
    start, end = voice_span(render)
    # No early start: the voice waits for a's segue, then b enters so that
    # its vocal (2 s into it) lands where the speech ends.
    assert start == pytest.approx(10.0, abs=1e-3)
    assert end == pytest.approx(13.0, abs=1e-3)
    assert incoming_entry(render) == pytest.approx(11.0, abs=1e-3)
    assert at(render, 12.0)[1] == pytest.approx(0.5)  # the voice
    assert at(render, 12.0)[0] == pytest.approx(0.25)  # b's intro under it, not ducked
    assert desk.get(item.break_id).mixed_as == "talk_up"


@pytest.mark.parametrize("placement", ["auto", "talk_up", "standalone"])
def test_manual_voice_is_dropped_when_its_full_chain_hits_a_hard_boundary(station, placement):
    player, (a, b, _c, _d), _points, desk = station
    slot_id = new_id()
    first = replace(
        make_break(desk, slot_id, seconds=3.0, placement=placement, allow_outro=True),
        source=VOICE_TRACK,
    )
    second = replace(
        make_break(desk, slot_id, seconds=2.0, placement=placement, allow_outro=True),
        source=VOICE_TRACK,
    )
    desk.add(first)
    desk.add(second)
    taken = tuple(desk.take(first.slot_id, 0.0))
    windows = []

    def crosses(start, duration):
        windows.append((start, duration))
        return True

    player.voice_track_crosses_hard_boundary = crosses
    rendered = player._render_track(a, b, 0, breaks=taken)
    assert rendered.break_events == ()
    assert all(desk.get(item.break_id).state == DROPPED for item in taken)
    assert windows[0][0] == pytest.approx(-2.0)
    assert windows[0][1] == pytest.approx(2.0 + first.cues.end - first.cues.start)
    assert windows[1][1] == pytest.approx(
        2.0 + first.cues.end - first.cues.start + second.cues.end - second.cues.start
    )


def test_hard_boundary_window_includes_a_planned_liner_before_voice(station):
    player, (a, b, liner, _d), _points, desk = station
    item = replace(make_break(desk, new_id(), seconds=3.0, allow_outro=True), source=VOICE_TRACK)
    desk.add(item)
    (taken,) = desk.take(item.slot_id, 0.0)
    windows = []
    player.voice_track_crosses_hard_boundary = lambda start, duration: (
        windows.append((start, duration)) or False
    )
    player._render_track(a, b, 0, liners=((liner, False),), breaks=(taken,))
    liner_points = player.saved_points(liner)
    liner_s = liner_points["end"] - liner_points["start"]
    assert windows[0] == pytest.approx((-2.0, 2.0 + liner_s + item.cues.end - item.cues.start))


def test_standalone_lets_the_next_song_in_when_the_speech_ends(station):
    player, (a, b, _c, _d), _points, desk = station
    item = make_break(desk, new_id(), kind="news", seconds=3.0)
    desk.add(item)
    render = player._render_track(a, b, 0, breaks=tuple(desk.take(item.slot_id, 0.0)))
    start, end = voice_span(render)
    assert start == pytest.approx(10.0, abs=1e-3)
    assert end == pytest.approx(13.0, abs=1e-3)
    assert incoming_entry(render) == pytest.approx(13.0, abs=1e-3)
    assert at(render, 12.0)[0] == 0.0  # a has faded, b has not started
    assert at(render, 12.0)[1] == pytest.approx(0.5)
    assert desk.get(item.break_id).mixed_as == "standalone"


@pytest.mark.parametrize("kind", ["dj", "news"])
def test_announcer_after_negative_liner_keeps_its_voice_before_vocals(station, kind):
    player, (a, b, liner, _d), points, desk = station
    points[b.path]["intro"] = 5.25
    points[liner.path].update(start=0, segue=1, end=1, overlap_start=-3)
    item = make_break(desk, new_id(), kind=kind, seconds=6.0)
    desk.add(item)
    render = player._render_track(
        a, b, 0, liners=((liner, False),), breaks=tuple(desk.take(item.slot_id, 0.0))
    )
    start, end = voice_span(render)
    assert render.liner_events == ((10 * SR, liner, False),)
    assert start == pytest.approx(11.0, abs=1e-3)
    assert end == pytest.approx(17.0, abs=1e-3)
    assert incoming_entry(render) == pytest.approx(12.0 if kind == "dj" else 17.0, abs=1e-3)


@pytest.mark.parametrize("placement", ["auto", "talk_up"])
def test_a_voice_track_after_a_negative_liner_talks_up_like_any_break(station, placement):
    # A voice track (#128) is a DJ break from the browser: after a liner
    # that starts inside the intro (#159) its speech still ends on b's vocal.
    player, (a, b, liner, _d), points, desk = station
    points[b.path]["intro"] = 5.25
    points[liner.path].update(start=0, segue=1, end=1, overlap_start=-3)
    item = replace(make_break(desk, new_id(), seconds=4.0, placement=placement), source=VOICE_TRACK)
    desk.add(item)
    render = player._render_track(
        a, b, 0, liners=((liner, False),), breaks=tuple(desk.take(item.slot_id, 0.0))
    )
    start, end = voice_span(render)
    vocal = incoming_entry(render) + 5.0  # b's intro: 0.25 s to 5.25 s
    assert end == pytest.approx(vocal, abs=1e-3)
    assert start == pytest.approx(end - 4.0, abs=1e-3)
    assert desk.get(item.break_id).mixed_as == "talk_up"


def test_auto_plays_standalone_when_the_intro_is_too_short(tmp_path):
    player, (a, b, _c, _d), _points, desk = make_station(tmp_path, min_intro_seconds=3.0)
    item = make_break(desk, new_id())
    desk.add(item)
    render = player._render_track(a, b, 0, breaks=tuple(desk.take(item.slot_id, 0.0)))
    assert incoming_entry(render) == pytest.approx(voice_span(render)[1], abs=1e-3)
    assert desk.get(item.break_id).mixed_as == "standalone"


def test_allow_outro_starts_the_voice_over_the_ending_songs_outro(station):
    player, (a, b, _c, _d), _points, desk = station
    item = make_break(desk, new_id(), seconds=3.0, allow_outro=True)
    desk.add(item)
    render = player._render_track(a, b, 0, breaks=tuple(desk.take(item.slot_id, 0.0)))
    start, end = voice_span(render)
    # The speech ends on b's vocal with b entering at a's segue: it starts
    # 3 s before that vocal, over a's outro (8 s) but no earlier.
    assert start == pytest.approx(9.0, abs=1e-3)
    assert end == pytest.approx(12.0, abs=1e-3)
    assert incoming_entry(render) == pytest.approx(10.0, abs=1e-3)
    assert at(render, 9.2)[0] == pytest.approx(1.0)  # a still at full level under it


def test_the_music_ducks_under_the_voice_only(tmp_path):
    player, (a, b, _c, _d), _points, desk = make_station(tmp_path, duck_db=-12.0)
    item = make_break(desk, new_id(), seconds=3.0)
    desk.add(item)
    render = player._render_track(a, b, 0, breaks=tuple(desk.take(item.slot_id, 0.0)))
    gain = 10 ** (-12 / 20)
    assert at(render, 12.0)[0] == pytest.approx(0.25 * gain, rel=1e-4)
    assert at(render, 12.0)[1] == pytest.approx(0.5)
    # Back to full level a ramp after the speech ends, before b's vocal.
    assert at(render, 13.3)[0] == pytest.approx(0.25, rel=1e-4)
    # Before the voice the outgoing song is untouched.
    assert at(render, 9.0)[0] == pytest.approx(1.0)


def test_a_news_break_goes_before_a_dj_talk_up(station):
    player, (a, b, _c, _d), _points, desk = station
    slot = new_id()
    dj = make_break(desk, slot, kind="dj", seconds=2.0, created_at=1.0)
    news = make_break(desk, slot, kind="news", seconds=3.0, created_at=2.0)
    desk.add(dj)
    desk.add(news)
    render = player._render_track(a, b, 0, breaks=tuple(desk.take(slot, 0.0)))
    events = {break_id: (first, end) for first, break_id, end in render.break_events}
    news_span, dj_span = events[news.break_id], events[dj.break_id]
    assert news_span[0] < dj_span[0]
    # The DJ break follows the news (its 3.4 s file, tail included) and
    # ends on b's vocal.
    vocal = len(render.audio) - render.overlap_frames + 2 * SR
    assert dj_span[1] == pytest.approx(vocal, abs=2)
    assert desk.get(news.break_id).mixed_as == "standalone"
    assert desk.get(dj.break_id).mixed_as == "talk_up"


def test_only_the_last_dj_break_talks_up(station):
    player, (a, b, _c, _d), _points, desk = station
    slot = new_id()
    first, second = make_break(desk, slot, created_at=1.0), make_break(desk, slot, created_at=2.0)
    desk.add(first)
    desk.add(second)
    player._render_track(a, b, 0, breaks=tuple(desk.take(slot, 0.0)))
    assert desk.get(first.break_id).mixed_as == "standalone"
    assert desk.get(second.break_id).mixed_as == "talk_up"


def test_a_break_that_cannot_be_decoded_is_dropped_and_the_music_plays(station):
    player, (a, b, _c, _d), _points, desk = station
    item = make_break(desk, new_id())
    item.path.write_bytes(b"not audio")
    desk.add(item)
    render = player._render_track(a, b, 0, breaks=tuple(desk.take(item.slot_id, 0.0)))
    assert render is not None and render.break_events == ()
    assert incoming_entry(render) == pytest.approx(10.0, abs=1e-3)
    assert desk.get(item.break_id).state == DROPPED
    assert desk.get(item.break_id).reason == "its file could not be decoded or holds no speech"


@pytest.mark.parametrize("mode", ["smooth", "stereotool"])
def test_processed_stations_process_a_break_like_a_voice_liner(station, mode, monkeypatch):
    player, *_rest, desk = station
    player._cfg.playback.loudness_processing = mode
    item = make_break(desk, new_id())
    audio = np.ones((10, 2), dtype=np.float32)
    load = MagicMock(return_value=(audio, {}))
    monkeypatch.setattr("autodj.processing.load_and_measure_audio", load)
    assert player._load_break_audio(item) is audio
    # FFmpeg is told the demuxer, so it probes nothing.
    load.assert_called_once_with(
        item.path, "voice", desk.config.max_seconds + 1.0, input_format="wav"
    )


# ---------------------------------------------------------------------------
# The slot plan: one song ahead
# ---------------------------------------------------------------------------


def test_the_open_slot_is_the_change_after_the_next_song(station):
    player, (_a, b, c, d), _points, _desk = station
    _render_a, render_b, plan = start_station(player, entries=station[1])
    assert render_b.entry is b and render_b.next_entry is c
    assert (plan.before, plan.ending, plan.upcoming, plan.mode) == (b, c, d, "queue")
    assert not plan.pinned
    status, closes_in, airs_in = player.slot_status()
    assert status.slot_id == plan.slot_id
    # a plays from the start of its render; c's change follows b and c.
    assert closes_in == pytest.approx(player._playing_remaining_s())
    assert airs_in > closes_in + plan.before_s


def test_the_planned_clock_step_survives_until_its_song_is_rendered(station, monkeypatch):
    player, entries, _points, _desk = station
    snapshot = {"number": 3, "step": {"type": "track", "path": entries[3].path}}
    choose = player._choose_station_next

    def choose_with_snapshot(*args, **kwargs):
        result = choose(*args, **kwargs)
        player._chosen_clock_step = snapshot
        return result

    monkeypatch.setattr(player, "_choose_station_next", choose_with_snapshot)
    _render_a, render_b, plan = start_station(player, entries)
    assert plan.clock_step == snapshot
    player._chosen_clock_step = {"number": 1, "step": {"type": "track", "path": entries[0].path}}
    player._on_track_start(render_b)
    render_c = player._next_rendered()
    assert render_c.next_clock_step == snapshot
    assert player._pending_clock_step == snapshot


def test_a_booked_break_airs_in_the_planned_change(station):
    player, entries, _points, desk = station
    _a, _b, c, d = entries
    _render_a, render_b, plan = start_station(player, entries)
    item = make_break(desk, plan.slot_id)
    pinned = player.book_break(item)
    assert pinned.pinned and desk.get(item.break_id).state == QUEUED
    player._on_track_start(render_b)
    render_c = player._next_rendered()
    assert render_c.entry is c and render_c.next_entry is d
    assert len(render_c.break_events) == 1
    assert desk.get(item.break_id).state == RENDERED
    assert desk.get(item.break_id).eta is not None
    assert player._slot_plan is None  # taken by the render
    first, break_id, end = render_c.break_events[0]
    player._on_track_start(render_c)
    player._on_position(first + 1)
    assert desk.get(break_id).state == ON_AIR
    player._on_position(end)
    assert desk.get(break_id).state == AIRED
    desk.maintain()
    assert not item.path.exists()


def test_a_booked_slot_keeps_its_songs_whatever_the_queue_does(station):
    player, entries, _points, desk = station
    _a, b, c, d = entries
    _render_a, render_b, plan = start_station(player, entries)
    player.book_break(make_break(desk, plan.slot_id))
    player._state.queue[:] = [b, d, c]  # c moves after d
    player.refresh_render_ahead()
    assert not player._queue_pick_is_stale(render_b)
    assert player._slot_plan is not None and not player._slot_plan.stale
    player._on_track_start(render_b)
    render_c = player._next_rendered()
    assert (render_c.entry, render_c.next_entry) == (c, d)
    assert render_c.break_events
    # A rendered break pins its next song too.
    assert not player._queue_pick_is_stale(render_c)


def test_an_unbooked_slot_is_planned_again_after_a_queue_edit(station):
    player, entries, _points, _desk = station
    _a, b, c, _d = entries
    _render_a, render_b, plan = start_station(player, entries)
    player._render_ahead.request_plan = MagicMock()
    player._state.queue[:] = [b, c]  # d leaves the queue
    player.refresh_render_ahead()
    assert player._slot_plan.stale
    player._render_ahead.request_plan.assert_called_once()
    assert player.slot_status() == (None, 0.0, 0.0)
    # Planned again from the same cursor, the slot gets a new id.
    player._plan_slot(render_b)
    assert player._slot_plan.slot_id != plan.slot_id
    assert player._slot_plan.ending is c and player._slot_plan.mode != "queue"
    # A stale plan is never used: the render picks again.
    player._slot_plan.stale = True
    assert player._claim_slot_plan(c) is None
    assert player._slot_plan is None


def test_a_re_render_with_the_same_ending_keeps_the_booking(station):
    player, entries, _points, desk = station
    _a, _b, c, _d = entries
    _render_a, render_b, plan = start_station(player, entries)
    item = make_break(desk, plan.slot_id)
    player.book_break(item)
    player._rewind_render_cursor(render_b)
    again = player._next_rendered()
    assert again.next_entry is c
    player._plan_slot(again)
    assert player._slot_plan.slot_id == plan.slot_id and player._slot_plan.pinned
    assert desk.get(item.break_id).state == QUEUED


def test_a_re_render_with_another_ending_drops_the_booking(station):
    player, entries, _points, desk = station
    _a, b, _c, d = entries
    _render_a, render_b, plan = start_station(player, entries)
    item = make_break(desk, plan.slot_id)
    player.book_break(item)
    player._state.queue[:] = [b, d]
    player._rewind_render_cursor(render_b)
    again = player._next_rendered()
    assert again.next_entry is d
    player._plan_slot(again)
    assert player._slot_plan.slot_id != plan.slot_id
    assert desk.get(item.break_id).state == DROPPED
    assert desk.get(item.break_id).reason == "the song before it changed"


def test_booking_a_closed_slot_is_refused(station):
    player, entries, _points, desk = station
    _render_a, _render_b, plan = start_station(player, entries)
    with pytest.raises(BreakError) as closed:
        player.book_break(make_break(desk, new_id()))
    assert (closed.value.status, closed.value.reason) == (409, "slot_closed")
    plan.stale = True
    with pytest.raises(BreakError):
        player.book_break(make_break(desk, plan.slot_id))
    player.break_desk = None
    with pytest.raises(BreakError) as off:
        player.book_break(make_break(desk, plan.slot_id))
    assert off.value.reason == "disabled"


def test_a_jump_drops_the_plan_and_every_break_not_yet_aired(station):
    player, entries, _points, desk = station
    _render_a, _render_b, plan = start_station(player, entries)
    item = make_break(desk, plan.slot_id)
    player.book_break(item)
    player.play_now(entries[3])
    assert player._slot_plan is None
    assert desk.get(item.break_id).state == DROPPED
    assert desk.get(item.break_id).reason == "a song was played at once"


def test_a_skipped_song_drops_the_break_it_carried(station):
    player, entries, _points, desk = station
    _render_a, render_b, plan = start_station(player, entries)
    item = make_break(desk, plan.slot_id)
    player.book_break(item)
    player._on_track_start(render_b)
    render_c = player._next_rendered()
    player._on_track_start(render_c)
    render_d = player._next_rendered()
    player._on_track_start(render_d)  # c was skipped before its break
    assert desk.get(item.break_id).state == DROPPED
    assert desk.get(item.break_id).reason == "skipped before it aired"


def test_a_break_that_reached_its_end_counts_as_aired_when_the_song_ends(station):
    player, entries, _points, desk = station
    _render_a, render_b, plan = start_station(player, entries)
    item = make_break(desk, plan.slot_id)
    player.book_break(item)
    player._on_track_start(render_b)
    render_c = player._next_rendered()
    player._on_track_start(render_c)
    first, _id, end = render_c.break_events[0]
    player._on_position(first + 1)
    player._on_position(end - SR // 4)  # the last report before the next song
    render_d = player._next_rendered()
    player._on_track_start(render_d)
    assert desk.get(item.break_id).state == AIRED


def test_a_break_that_would_air_too_late_expires(station):
    player, entries, _points, desk = station
    _render_a, render_b, plan = start_station(player, entries)
    item = make_break(desk, plan.slot_id, expires_at=1.0)
    player.book_break(item)
    player._on_track_start(render_b)
    render_c = player._next_rendered()
    assert render_c.break_events == ()
    assert desk.get(item.break_id).state == EXPIRED


def test_skipping_the_intro_moves_the_break_events_too(station):
    player, entries, _points, desk = station
    _render_a, render_b, plan = start_station(player, entries)
    player.book_break(make_break(desk, plan.slot_id))
    player._on_track_start(render_b)
    render_c = player._next_rendered()
    player._render_ahead._ready = render_c
    player._skip_next_intro = True
    taken = player._take_render()
    prefix = render_c.start_offset - render_c.source_start_offset
    assert taken.break_events == tuple(
        (first + prefix, break_id, end + prefix) for first, break_id, end in render_c.break_events
    )


def test_with_breaks_off_nothing_is_planned(tmp_path):
    player, entries, _points, desk = make_station(tmp_path)
    off = replace(desk.config, enabled=False)
    desk._config = lambda: off
    _render_a, _render_b, plan = start_station(player, entries)
    assert plan is None
    assert player.slot_status() == (None, 0.0, 0.0)


def test_cues_are_found_again_in_the_audio_that_plays(station, monkeypatch):
    player, (a, b, _c, _d), _points, desk = station
    item = make_break(desk, new_id(), kind="news", seconds=3.0)
    desk.add(item)
    # Processing that adds a second of quiet before the voice moves its start.
    shifted = np.zeros((round(5.0 * SR), 2), dtype=np.float32)
    shifted[SR : 4 * SR, 1] = 0.5
    monkeypatch.setattr(player, "_load_break_audio", lambda _item: shifted)
    render = player._render_track(a, b, 0, breaks=tuple(desk.take(item.slot_id, 0.0)))
    start, end = voice_span(render)
    assert at(render, start + 0.06)[1] == pytest.approx(0.5)
    assert end - start == pytest.approx(3.05, abs=0.03)
    assert incoming_entry(render) == pytest.approx(end, abs=1e-3)


def test_a_render_thrown_away_drops_its_breaks(station):
    player, entries, _points, desk = station
    _render_a, render_b, plan = start_station(player, entries)
    item = make_break(desk, plan.slot_id)
    player.book_break(item)
    player._on_track_start(render_b)
    render_c = player._next_rendered()
    assert desk.get(item.break_id).state == RENDERED
    player._render_ahead._discard(render_c)
    assert desk.get(item.break_id).state == DROPPED
    assert desk.get(item.break_id).reason == "its render was thrown away before it played"


def test_another_clockwheel_discards_the_plan_and_its_breaks(station):
    player, entries, _points, desk = station
    _render_a, render_b, plan = start_station(player, entries)
    item = make_break(desk, plan.slot_id)
    player.book_break(item)
    player._pending_clock_name = "Night"  # what selecting another clockwheel leaves
    player._on_track_start(render_b)
    assert player._claim_slot_plan(plan.ending) is None
    assert desk.get(item.break_id).state == DROPPED
    assert desk.get(item.break_id).reason == "the clockwheel changed"


def test_a_slot_whose_breaks_all_went_is_unpinned(station):
    player, entries, _points, desk = station
    _a, b, c, _d = entries
    _render_a, _render_b, plan = start_station(player, entries)
    item = make_break(desk, plan.slot_id)
    player.book_break(item)
    assert player._slot_plan.pinned
    desk.cancel(item.break_id)
    assert not player._slot_plan.pinned
    # Unpinned, a queue edit may change the slot again.
    player._render_ahead.request_plan = MagicMock()
    player._state.queue[:] = [b, c]
    player.refresh_render_ahead()
    assert player._slot_plan.stale


def test_a_discarded_plan_gives_back_its_clock_move(station):
    player, entries, _points, _desk = station
    _render_a, _render_b, plan = start_station(player, entries)
    player._pending_clock_boundary = 123.0
    plan.boundary_before = 99.0
    plan.stale = True
    assert player._claim_slot_plan(plan.ending) is None
    assert player._pending_clock_boundary == 99.0


def test_the_planned_change_judges_hours_when_its_song_starts(station):
    """The pick for song d, made two songs early, reads the hours when d is expected to start."""
    player, entries, points, _desk = station
    _a, _b, c, _d = entries
    _render_a, render_b, _plan = start_station(player, entries)
    player._slot_plan = None
    player._rewind_render_cursor(render_b)
    again = player._next_rendered()
    assert again.next_entry is c
    player._now = lambda: 5000.0  # type: ignore[method-assign]
    asked: list[float | None] = []
    real = player._pick_context

    def recording(*args, **kwargs):
        asked.append(kwargs.get("at"))
        return real(*args, **kwargs)

    player._pick_context = recording  # type: ignore[method-assign]
    player._plan_slot(again)
    lead = player._playing_remaining_s() + len(again.audio) / SR
    start, segue = points[c.path]["start"], points[c.path]["segue"]
    expected = 5000.0 + max(0.0, segue - max(start, player._pending_offset / SR)) + lead
    assert asked == [pytest.approx(expected)]
    assert asked[0] > 5000.0 + lead  # after c has played, not when c starts
