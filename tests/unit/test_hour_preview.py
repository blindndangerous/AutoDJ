"""The hour preview: estimated start times, fixed or predicted, and backtiming (#126)."""

from __future__ import annotations

import itertools
import random
import threading
import time
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime
from datetime import time as clock_time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from autodj import hour_preview
from autodj.breaks import SlotPlan
from autodj.hour_preview import (
    KEEP_SECONDS,
    HourPreviews,
    Item,
    Point,
    PreviewError,
    build_preview,
    check_minutes,
    clock_text,
    clock_words,
    fixed_points,
    land,
    landing_words,
    length_text,
    length_words,
    read_station,
    song_changes,
    span_words,
)
from autodj.mixbus import RenderedTrack
from autodj.player import apply_repeat_windows
from autodj.repeat_rules import PlayRecord
from autodj.rotation_sim import (
    SimulationRequest,
    average_length,
    simulate,
    snapshot_player,
    song_seconds,
)
from autodj.scheduler import ScheduledEvent, system_timezone
from autodj.stereo import SAMPLE_RATE as SR
from tests.unit._rotation_station import category_step, liner, liner_step, make_station, song

UTC_ZONE = ZoneInfo("UTC")
# Monday 12 October 2026, 12:10 UTC.
NOW = datetime(2026, 10, 12, 12, 10, tzinfo=UTC).timestamp()


def _station(tmp_path: Path, **changes: Any):
    """Power (6 songs) and Gold (60) under a liner, Power, Gold clockwheel; no restarts."""
    power = [song(n, f"Power {n}") for n in range(6)]
    gold = [song(100 + n, f"Gold {n}") for n in range(60)]
    options: dict[str, Any] = {
        "liners": [liner(1), liner(2)],
        "categories": [
            {"id": "power", "name": "Power", "members": power},
            {"id": "gold", "name": "Gold", "members": gold},
        ],
        "clockwheels": [
            {
                "name": "Main",
                "resync_minutes": [],
                "steps": [liner_step(), category_step("power"), category_step("gold")],
            }
        ],
        "active_clock": "Main",
        "playback": {"pick_mode": "clock"},
    }
    options.update(changes)
    return make_station(tmp_path, [*power, *gold], **options), power, gold


def _playing(source: Any, entry: Any, started: float) -> None:
    state = source.player._state
    state.current_track = entry
    state.record_played(entry)
    state.play_history[-1] = PlayRecord.of(entry, started)


def _frames(seconds: float) -> np.ndarray:
    """Audio of *seconds* that takes no memory: only its length is read."""
    return np.empty((round(seconds * SR), 0), dtype=np.float32)


# -- words ----------------------------------------------------------------------


def test_times_and_lengths_read_well() -> None:
    two_pm = datetime(2026, 10, 12, 14, 0, tzinfo=UTC).timestamp()
    assert clock_text(two_pm + 270, UTC_ZONE) == "2:04:30 PM"
    assert clock_words(two_pm + 270, UTC_ZONE) == "2:04 PM and 30 seconds"
    assert clock_words(two_pm, UTC_ZONE) == "2 PM"
    assert clock_words(two_pm + 61, UTC_ZONE) == "2:01 PM and 1 second"
    midnight = datetime(2026, 10, 12, 0, 5, tzinfo=UTC).timestamp()
    assert clock_text(midnight, UTC_ZONE) == "12:05:00 AM"
    assert length_text(225) == "3:45"
    assert length_words(225) == "3 minutes 45 seconds"
    assert length_words(60) == "1 minute"
    assert length_words(1) == "1 second"
    assert span_words(60) == "hour"
    assert span_words(120) == "2 hours"
    assert span_words(30) == "30 minutes"


@pytest.mark.parametrize("minutes", [0, 14, 181, True, 60.0])
def test_spans_out_of_range_are_refused(minutes: Any) -> None:
    with pytest.raises(PreviewError, match="minutes"):
        check_minutes(minutes)


# -- backtiming -----------------------------------------------------------------


def _songs(*starts: float) -> list[Item]:
    return [
        Item("song", at, 200.0, "predicted", song(n, f"Artist {n}")) for n, at in enumerate(starts)
    ]


TOP = datetime(2026, 10, 12, 13, 0, tzinfo=UTC).timestamp()


def test_the_hour_lands_at_the_nearer_song_change() -> None:
    hour = Point("hour", TOP, "Top of the hour")
    early = land(hour, song_changes(_songs(TOP - 400, TOP - 35, TOP + 160)), TOP + 400)
    assert early.offset == pytest.approx(-35)
    assert early.playing is not None and early.playing.entry.artist == "Artist 1"
    short, sentence = landing_words(early, UTC_ZONE)
    assert short == "35 seconds early"
    assert sentence == (
        "Top of the hour, 1 PM: lands about 35 seconds early, at the song change into "
        "Artist 1, Song 1 (predicted) at 12:59 PM and 25 seconds. "
        "That song runs 2 minutes 40 seconds past 1 PM."
    )
    late = land(hour, song_changes(_songs(TOP - 150, TOP + 20)), TOP + 220)
    assert late.offset == pytest.approx(20)
    assert landing_words(late, UTC_ZONE) == (
        "20 seconds late",
        "Top of the hour, 1 PM: lands about 20 seconds late. Artist 0, Song 0 (predicted) is "
        "playing then; the next song change is at 1:00 PM and 20 seconds.",
    )
    on_time = land(hour, song_changes(_songs(TOP - 200, TOP + 0.4)), TOP + 200)
    assert on_time.offset == 0
    assert landing_words(on_time, UTC_ZONE)[0] == "on time"


def test_liners_before_a_song_open_its_change() -> None:
    songs = _songs(TOP - 300, TOP - 20)
    ident = Item("liner", TOP - 50, 30.0, "predicted", liner(1))
    landing = land(Point("hour", TOP, "Top of the hour"), song_changes([*songs, ident]), TOP + 180)
    assert landing.began == pytest.approx(TOP - 50)
    assert landing.offset == pytest.approx(-50)
    # A liner mixed over a song does not move the change.
    over = Item("liner", TOP - 100, 6.0, "ready", liner(2))
    assert song_changes([*songs, over])[1][0] == pytest.approx(TOP - 20)


def test_events_wait_or_cut_by_their_timing() -> None:
    songs = song_changes(_songs(TOP - 100, TOP + 90))
    soft = land(Point("event", TOP, "News", "soft", "Play a liner: News"), songs, TOP + 300)
    assert soft.offset == pytest.approx(90)
    assert landing_words(soft, UTC_ZONE)[1] == (
        "News (Play a liner: News), soft, at 1 PM: runs about 1 minute 30 seconds late, after "
        "Artist 0, Song 0 (predicted) ends at 1:01 PM and 30 seconds. A soft event waits for "
        "the song playing to end."
    )
    hard = land(Point("event", TOP, "News", "hard", "Play a liner: News"), songs, TOP + 300)
    assert (hard.offset, hard.cut) == (0.0, pytest.approx(90))
    assert landing_words(hard, UTC_ZONE) == (
        "on time, cuts 1 minute 30 seconds",
        "News (Play a liner: News), hard, at 1 PM: on time. It cuts Artist 0, Song 0 "
        "(predicted) short by about 1 minute 30 seconds.",
    )


def test_a_point_past_the_preview_is_not_guessed() -> None:
    landing = land(
        Point("hour", TOP, "Top of the hour"), song_changes(_songs(TOP - 500)), TOP - 300
    )
    assert landing.starts is None
    assert landing_words(landing, UTC_ZONE) == (
        "not known",
        "Top of the hour, 1 PM: the preview does not reach the song change after it.",
    )


def _event(name: str, **fields: Any) -> ScheduledEvent:
    spec = {
        "id": uuid.uuid4().hex,
        "name": name,
        "action": "liner",
        "params": {"name": "news.mp3"},
        "repeat": "daily",
        "time": clock_time(13, 30),
        **fields,
    }
    return ScheduledEvent.model_validate(spec)


def test_fixed_points_are_hours_restarts_and_events_in_the_span() -> None:
    zone = ZoneInfo(system_timezone())
    now = datetime(2026, 10, 12, 12, 10, tzinfo=zone).timestamp()
    events = [
        _event("News", timing="soft"),
        _event("Weather", time=clock_time(12, 45), timing="hard"),
        _event("Later", start_date=date(2026, 10, 13)),
        _event("Off", enabled=False),
    ]
    points = fixed_points(
        now,
        now + 7200,
        zone,
        resync_minutes=[0, 30],
        events=events,
        describe=lambda event: f"Play a liner: {event.params['name']}",
    )
    words = [(point.kind, point.label, clock_words(point.at, zone)) for point in points]
    # The clockwheel restarts are read in the computer's zone, as the player
    # reads them; the station here uses that zone too.
    assert words == [
        ("restart", "Clockwheel restart", "12:30 PM"),
        ("event", "Weather", "12:45 PM"),
        ("restart", "Top of the hour, clockwheel restart", "1 PM"),
        ("event", "News", "1:30 PM"),
        ("restart", "Clockwheel restart", "1:30 PM"),
        ("restart", "Top of the hour, clockwheel restart", "2 PM"),
    ]
    assert points[1].timing == "hard"
    assert points[1].detail == "Play a liner: news.mp3"
    plain = fixed_points(now, now + 7200, zone)
    assert [(point.kind, point.label) for point in plain] == [("hour", "Top of the hour")] * 2


# -- what is fixed, browser playback ---------------------------------------------


def test_nothing_playing_says_so(tmp_path: Path) -> None:
    source, _power, _gold = _station(tmp_path)
    view = build_preview(source, 60, now=NOW)
    assert view["reason"] == hour_preview.NOTHING_PLAYING
    assert view["items"] == []
    assert view["summary"] == hour_preview.NOTHING_PLAYING


def test_browser_playback_lists_fixed_then_predicted(tmp_path: Path) -> None:
    source, power, gold = _station(tmp_path)
    player = source.player
    _playing(source, gold[0], NOW - 50)
    player._state.next_track = gold[1]
    player._state.queue.append(power[3])
    player._clock = {**player._clock, "cursor": 2}
    view = build_preview(source, 60, now=NOW)
    items = view["items"]
    head = [(item["status"], item["title"], item["starts_text"]) for item in items[:3]]
    assert head == [
        ("on_air", "Gold 0, Song 100", "12:09:10 PM"),
        ("picked", "Gold 1, Song 101", "12:12:30 PM"),
        ("queued", "Power 3, Song 3", "12:15:50 PM"),
    ]
    assert [item["status_text"] for item in items[:3]] == [
        "Fixed: on the air",
        "Fixed: picked",
        "Fixed: in the queue",
    ]
    predicted = items[3:]
    assert predicted and all(item["status_text"] == "Predicted" for item in predicted)
    assert not any(item["fixed"] for item in predicted)
    # The dry run goes on from the clockwheel's place: step 3 is Gold.
    assert predicted[0]["title"].startswith("Gold ")
    assert predicted[0]["starts_text"] == "12:19:10 PM"
    assert items[0]["starts_words"] == "12:09 PM and 10 seconds"
    assert items[0]["length_text"] == "3:20"
    assert items[0]["length_words"] == "3 minutes 20 seconds"
    # Each item starts as the one before ends, up to the end of the span.
    songs = [item for item in items if item["kind"] == "song"]
    assert all(
        datetime.fromisoformat(after["starts_at"]).timestamp()
        >= datetime.fromisoformat(before["starts_at"]).timestamp()
        for before, after in itertools.pairwise(songs)
    )
    last = datetime.fromisoformat(items[-1]["starts_at"]).timestamp()
    assert NOW + 3600 - 400 < last < NOW + 3600
    assert view["counts"] == {"fixed": 3, "predicted": len(predicted)}
    assert view["summary"].startswith("Top of the hour, 1 PM: lands about ")
    assert view["summary"].endswith(
        f"Next hour: 3 fixed and {len(predicted)} predicted, until 1:10 PM."
    )
    assert [point["kind"] for point in view["points"]] == ["hour"]
    assert view["stopped"] is None


def test_a_preview_changes_nothing_on_the_air(tmp_path: Path) -> None:
    source, power, gold = _station(tmp_path, dmca=True)
    player = source.player
    _playing(source, gold[0], NOW - 50)
    player._state.next_track = gold[1]
    player._state.queue.append(power[3])
    random.seed(11)
    before_random = random.getstate()
    before = (
        list(player._state.play_history),
        list(player._state.recently_played),
        dict(player._clock),
        player._pending_clock_cursor,
        player._state.track_number,
        list(player._state.queue),
        player._state.current_track,
        player._state.next_track,
    )
    first = build_preview(source, 120, now=NOW)
    again = build_preview(source, 120, now=NOW)
    after = (
        list(player._state.play_history),
        list(player._state.recently_played),
        dict(player._clock),
        player._pending_clock_cursor,
        player._state.track_number,
        list(player._state.queue),
        player._state.current_track,
        player._state.next_track,
    )
    assert after == before
    assert random.getstate() == before_random
    assert source.library.catalog.writes == 0
    assert source.ledger.writes == 0
    # The same fixed part gives the same guess, so a refresh does not reshuffle it.
    assert [item["title"] for item in first["items"]] == [item["title"] for item in again["items"]]


def test_the_dry_run_stops_at_its_limits(tmp_path: Path, monkeypatch) -> None:
    source, _power, gold = _station(tmp_path)
    _playing(source, gold[0], NOW - 50)
    monkeypatch.setattr(hour_preview, "MAX_PREDICTED", 3)
    view = build_preview(source, 180, now=NOW)
    assert view["counts"]["predicted"] <= 5  # three songs and their liners
    assert view["stopped"] == (
        "The prediction stopped early: it reached its limit of songs. "
        "The list ends at the last song it picked."
    )
    timed = build_preview(source, 180, now=NOW, budget=0.0)
    assert "time limit" in timed["stopped"]


def test_a_scheduled_change_of_picking_reaches_the_prediction(tmp_path: Path) -> None:
    switch = _event(
        "Gold only",
        action="pick_mode",
        params={"mode": "random"},
        time=clock_time(12, 20),
    )
    source, _power, gold = _station(tmp_path, events=[switch])
    _playing(source, gold[0], NOW - 50)
    view = build_preview(source, 60, now=NOW)
    predicted = [item for item in view["items"] if item["status"] == "predicted"]
    # Random picking adds no clockwheel liners after the change.
    after = [
        item
        for item in predicted
        if datetime.fromisoformat(item["starts_at"]).timestamp() > NOW + 900
    ]
    assert after and all(item["kind"] == "song" for item in after)
    # The change is a fixed point too: a soft event waits for the song playing.
    events = [point for point in view["points"] if point["kind"] == "event"]
    assert [(point["label"], point["timing"], point["at_words"]) for point in events] == [
        ("Gold only", "soft", "12:20 PM")
    ]
    assert events[0]["landing"].endswith("late") or events[0]["landing"] == "on time"


# -- what is fixed, server audio ---------------------------------------------------


class _Desk:
    """The break desk calls the preview makes."""

    def __init__(self, booked: list[Any], names: dict[str, Any]) -> None:
        self._booked = booked
        self._names = names

    def listing(self) -> list[Any]:
        return list(self._booked)

    def get(self, break_id: str) -> Any:
        return self._names.get(break_id)


def _render(entry: Any, seconds: float, following: Any, overlap: float, **fields: Any):
    return RenderedTrack(
        entry=entry,
        audio=_frames(seconds),
        next_entry=following,
        next_start_offset=round(overlap * SR),
        transition_fx="none",
        overlap_frames=round(overlap * SR),
        **fields,
    )


def test_server_audio_reads_renders_the_open_change_and_its_breaks(tmp_path: Path) -> None:
    source, power, gold = _station(tmp_path)
    player = source.player
    ident = source.library.entries_snapshot("liner")[0]
    _playing(source, gold[0], NOW - 50)
    playing = _render(
        gold[0],
        200,
        gold[1],
        5,
        liner_events=((round(190 * SR), ident, False),),
    )
    ready = _render(
        gold[1],
        190,
        gold[2],
        5,
        break_events=((round(185 * SR), "b0", round(195 * SR)),),
        next_from_queue=True,
    )
    player.bus = object()
    player._playing_render = playing
    player._playback_pos[0] = round(50 * SR)
    player._render_ahead._ready = ready
    player._pending_entry = gold[2]
    player._pending_offset = round(5 * SR)
    player._cursor_serial = 7
    player._slot_plan = SlotPlan(
        slot_id="s1",
        serial=7,
        before=gold[1],
        before_s=190,
        ending=gold[2],
        upcoming=power[3],
        mode="clock",
        liners=((ident, False),),
        next_cursor=2,
        boundary_before=None,
        boundary_after=None,
        queue_reserved=None,
        reserved=(),
        clock_name="Main",
        pinned=True,
    )
    desk = _Desk(
        [
            SimpleNamespace(
                slot_id="s1", state="queued", cues=SimpleNamespace(speech=12.0), break_id="b1"
            )
        ],
        {
            "b0": SimpleNamespace(kind="dj", label="Hello"),
            "b1": SimpleNamespace(kind="news", label="Top stories"),
        },
    )
    player._state.queue.extend([gold[2], power[4]])
    decided = read_station(player, desk, now=NOW)
    rows = [
        (item.kind, item.status, round(item.starts - NOW), round(item.seconds))
        for item in decided.items
    ]
    assert rows == [
        ("song", "on_air", -50, 195),
        ("liner", "ready", 140, 6),
        ("song", "ready", 145, 190),
        ("break", "ready", 335, 10),
        ("song", "picked", 335, 200),
        ("liner", "picked", 535, 6),
        ("break", "booked", 541, 12),
        ("song", "picked", 553, 200),
        ("song", "queued", 753, 200),
    ]
    labels = [item.label for item in decided.items if item.kind == "break"]
    assert labels == ["DJ break: Hello", "News break: Top stories"]
    # Gold 2 came from the queue, so only Power 4 is left waiting.
    assert decided.items[-1].entry is power[4]
    assert decided.last is power[4]
    assert decided.handoff == pytest.approx(NOW + 953)
    assert (decided.clock_cursor, decided.clock_name) == (2, "Main")
    # The render worker's place and the open change are only read.
    assert player._render_ahead._ready is ready
    assert player._slot_plan.pinned and player._cursor_serial == 7
    assert list(player._state.queue) == [gold[2], power[4]]
    view = build_preview(source, 30, desk=desk, now=NOW, decided=decided)
    statuses = [item["status_text"] for item in view["items"]]
    assert statuses[:9] == [
        "Fixed: on the air",
        "Fixed: ready to play",
        "Fixed: ready to play",
        "Fixed: ready to play",
        "Fixed: picked",
        "Fixed: picked",
        "Fixed: booked break",
        "Fixed: picked",
        "Fixed: in the queue",
    ]
    # Step 3 of the clockwheel (Gold) follows the open change's place.
    assert view["items"][9]["title"].startswith("Gold ")


def test_a_render_under_way_leaves_only_the_next_song_fixed(tmp_path: Path) -> None:
    source, _power, gold = _station(tmp_path)
    player = source.player
    _playing(source, gold[0], NOW - 50)
    player.bus = object()
    player._playing_render = _render(gold[0], 200, gold[1], 5)
    player._playback_pos[0] = round(50 * SR)
    player._pending_entry = gold[1]
    player._pending_offset = round(5 * SR)
    decided = read_station(player, now=NOW)
    rows = [(item.status, round(item.starts - NOW), round(item.seconds)) for item in decided.items]
    assert rows == [("on_air", -50, 195), ("picked", 145, 200)]
    assert decided.handoff == pytest.approx(NOW + 345)


def test_a_cursor_that_keeps_moving_is_not_trusted(tmp_path: Path) -> None:
    source, _power, gold = _station(tmp_path)
    player = source.player
    _playing(source, gold[0], NOW - 50)
    player.bus = object()
    player._playing_render = _render(gold[0], 200, gold[1], 5)
    player._playback_pos[0] = round(50 * SR)

    # Every read of the cursor's serial finds it moved on.
    counter = iter(range(1, 1000))
    player.__class__ = type(
        "Moving", (type(player),), {"_cursor_serial": property(lambda _self: next(counter))}
    )
    decided = read_station(player, now=NOW)
    assert [item.status for item in decided.items] == ["on_air", "picked"]
    assert decided.clock_cursor is None


# -- the server's previews ------------------------------------------------------------


def test_a_preview_is_kept_while_nothing_new_is_fixed(tmp_path: Path, monkeypatch) -> None:
    source, _power, gold = _station(tmp_path)
    _playing(source, gold[0], time.time() - 30)
    previews = HourPreviews()
    built: list[int] = []
    real = hour_preview.build_preview

    def counting(*args: Any, **kwargs: Any) -> dict[str, Any]:
        built.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(hour_preview, "build_preview", counting)
    first = previews.submit(source, 60).result(timeout=30)
    assert previews.submit(source, 60).result(timeout=30) is first
    assert len(built) == 1
    # Another span is its own preview; a new song on the air is a new one.
    previews.submit(source, 30).result(timeout=30)
    source.player._state.next_track = gold[5]
    previews.submit(source, 60).result(timeout=30)
    assert len(built) == 3
    assert previews.shutdown(5.0)
    with pytest.raises(RuntimeError):
        previews.submit(source, 60)
    # Kept past the page's own refresh, so an open card that finds nothing new
    # fixed costs no dry run.
    assert KEEP_SECONDS > hour_preview.PAGE_REFRESH_SECONDS


def test_cache_freshness_covers_handoff_rules_schedule_library_and_refresh(
    tmp_path: Path, monkeypatch
) -> None:
    source, _power, _gold = _station(tmp_path)
    decided = hour_preview.Decided(
        now=1000, handoff=1100, items=(Item("song", 900, 200, "on_air"),)
    )
    monkeypatch.setattr(hour_preview, "read_station", lambda *_args, **_kwargs: decided)
    builds: list[int] = []
    monkeypatch.setattr(
        hour_preview, "build_preview", lambda *_args, **_kwargs: builds.append(1) or {}
    )
    versions = [0]
    source.library.catalog.change_versions = lambda: {"entries": versions[0]}
    source.player._cfg = SimpleNamespace(playback=SimpleNamespace(repeat_rules="one"))
    previews = HourPreviews()

    previews.get(source, 60)
    previews.get(source, 60)
    assert len(builds) == 1
    decided = replace(decided, handoff=980)  # Same tracks and statuses after a seek.
    monkeypatch.setattr(hour_preview, "read_station", lambda *_args, **_kwargs: decided)
    previews.get(source, 60)
    source.player._cfg.playback.repeat_rules = "two"
    previews.get(source, 60)
    source.events = ("changed scheduled event",)
    previews.get(source, 60)
    versions[0] += 1
    previews.get(source, 60)
    previews.get(source, 60, force=True)
    assert len(builds) == 6


def test_previews_run_on_their_own_worker_thread(tmp_path: Path, monkeypatch) -> None:
    source, _power, gold = _station(tmp_path)
    _playing(source, gold[0], time.time() - 30)
    seen: list[str] = []
    monkeypatch.setattr(
        hour_preview,
        "build_preview",
        lambda *_args, **_kwargs: seen.append(threading.current_thread().name) or {},
    )
    previews = HourPreviews()
    previews.submit(source, 15).result(timeout=30)
    assert previews.shutdown(5.0)
    assert seen and seen[0].startswith("autodj-hour-preview")


# -- review follow-ups ----------------------------------------------------------------


def test_browser_playback_lists_a_queued_next_song_once(tmp_path: Path) -> None:
    source, power, gold = _station(tmp_path)
    player = source.player
    _playing(source, gold[0], NOW - 50)
    # After a queue edit the page's next song is the queue's head, still queued.
    player._state.queue.extend([power[3], power[4]])
    player._state.next_track = power[3]
    decided = read_station(player, now=NOW)
    rows = [(item.status, item.entry.path) for item in decided.items]
    assert rows == [
        ("on_air", gold[0].path),
        ("picked", power[3].path),
        ("queued", power[4].path),
    ]


def test_a_position_from_another_render_is_not_used(tmp_path: Path) -> None:
    source, _power, gold = _station(tmp_path)
    player = source.player
    _playing(source, gold[0], NOW - 50)
    player.bus = object()
    player._playing_render = _render(gold[0], 200, gold[1], 5, start_offset=round(10 * SR))
    # Still the old track's place: before this render's audio begins.
    player._playback_pos[0] = round(2 * SR)
    playing, _position, steady = hour_preview._read_position(player)
    assert playing is player._playing_render
    assert not steady
    decided = read_station(player, now=NOW)
    # Not trusted, so only the song on the air and its successor are fixed.
    assert [item.status for item in decided.items] == ["on_air", "picked"]
    assert decided.clock_cursor is None
    player._playback_pos[0] = round(60 * SR)
    assert hour_preview._read_position(player)[2]


def test_more_fixed_points_than_are_timed_are_said(tmp_path: Path, monkeypatch) -> None:
    source, _power, gold = _station(tmp_path, events=[_event("News", time=clock_time(12, 20))])
    _playing(source, gold[0], NOW - 50)
    monkeypatch.setattr(hour_preview, "MAX_POINTS", 2)
    view = build_preview(source, 180, now=NOW)
    assert len(view["points"]) == 2
    assert view["points_note"] == "Only the first 2 of 3 fixed points are timed."
    assert build_preview(source, 60, now=NOW)["points_note"] is None


def test_the_preview_and_the_rotation_tester_time_songs_alike(tmp_path: Path) -> None:
    """Both move time by song_seconds, with average_length for a song without a length."""
    music = [song(n, f"Artist {n}", length=150.0 + 7 * n) for n in range(12)]
    music.append(song(99, "No length", length=0.0))
    source = make_station(
        tmp_path,
        music,
        playback={"transition_mode": "fixed", "crossfade_seconds": 4.0, "pick_mode": "random"},
    )
    player = source.player
    average = average_length(player.playable_entries())
    assert average == pytest.approx(sum(150.0 + 7 * n for n in range(12)) / 12)
    assert song_seconds(player, music[3], average) == pytest.approx(150.0 + 21 - 4.0)
    assert song_seconds(player, music[-1], average) == pytest.approx(average)

    starts: list[tuple[float, Any]] = []
    simulate(
        source,
        SimulationRequest(picks=12, start=NOW, seed=5),
        on_pick=lambda at, entry: starts.append((at, entry)),
    )
    for (at, entry), (following, _next) in itertools.pairwise(starts):
        assert following - at == pytest.approx(song_seconds(player, entry, average))

    _playing(source, music[0], NOW - 10)
    view = build_preview(source, 60, now=NOW)
    predicted = [item for item in view["items"] if item["status"] == "predicted"]
    by_title = {f"{entry.artist}, {entry.title}": entry for entry in music}
    for item in predicted:
        expected = song_seconds(player, by_title[item["title"]], average)
        assert item["seconds"] == pytest.approx(expected, abs=0.05)


@pytest.mark.parametrize("first_overlap,second_overlap", [(None, 0), (0, -1), (2, 1), (-2, 0)])
@pytest.mark.parametrize("incoming_intro", [None, 0.0, 4.0])
@pytest.mark.parametrize("allow_outro", [False, True])
def test_preview_liner_chain_matches_synthetic_live_render(
    tmp_path: Path,
    monkeypatch,
    first_overlap: float | None,
    second_overlap: float,
    incoming_intro: float | None,
    allow_outro: bool,
) -> None:
    source, _power, gold = _station(tmp_path)
    player = source.player
    liners = source.library.entries_snapshot("liner")
    points = {
        gold[0].path: {"start": 0, "outro": 4, "segue": 8, "end": 12},
        liners[0].path: {"start": 0.5, "segue": 2.5, "end": 3.5, "overlap_start": first_overlap},
        liners[1].path: {"start": 0.25, "segue": 1, "end": 2, "overlap_start": second_overlap},
        gold[1].path: {"start": 0, "end": 12},
    }
    if incoming_intro is not None:
        points[gold[1].path]["intro"] = incoming_intro
    monkeypatch.setattr(source.library.catalog, "points", lambda path: points.get(path, {}))
    monkeypatch.setattr(
        source.library.catalog, "record_for_path", lambda _path: {"allow_outro": allow_outro}
    )

    def audio(entry):
        return np.zeros((round(entry.length * SR), 2), dtype=np.float32)

    monkeypatch.setattr(player, "_playout_audio", lambda entry, _decode: audio(entry))
    monkeypatch.setattr(player, "_load_playout_audio", audio)

    actual = player._render_saved_track(
        gold[0], gold[1], 0, ((liners[0], False), (liners[1], False))
    )
    rows, delay = hour_preview._preview_liner_group(player, gold[0], gold[1], liners, NOW + 8)
    actual_incoming = len(actual.audio) - actual.overlap_frames
    assert delay == pytest.approx(actual_incoming / SR - 8)
    assert rows[0][1] == pytest.approx(NOW + actual.liner_events[0][0] / SR)
    assert rows[1][1] == pytest.approx(NOW + actual.liner_events[1][0] / SR)


def test_the_snapshot_shares_nothing_of_the_clockwheel(tmp_path: Path) -> None:
    source, _power, _gold = _station(tmp_path)
    player = source.player
    player._clock = {**player._clock, "resync_minutes": [0, 30]}
    snapshot = snapshot_player(player)
    assert snapshot.clock == player._clock
    assert snapshot.clock["resync_minutes"] is not player._clock["resync_minutes"]


def _slow_previews(monkeypatch, release: threading.Event, started: threading.Event):
    def slow(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        started.set()
        release.wait(10)
        return {"made": time.monotonic()}

    monkeypatch.setattr(hour_preview, "build_preview", slow)


def test_asks_for_the_same_preview_share_one_answer(tmp_path: Path, monkeypatch) -> None:
    source, _power, gold = _station(tmp_path)
    _playing(source, gold[0], time.time() - 30)
    release, started = threading.Event(), threading.Event()
    _slow_previews(monkeypatch, release, started)
    previews = HourPreviews(waiting=2)
    first = previews.submit(source, 60)
    assert started.wait(10)
    # The same station and span while it is being made: the same answer.
    assert previews.submit(source, 60) is first
    # Others wait, up to the limit; one more is refused in words.
    waiting = [previews.submit(source, 30), previews.submit(source, 120)]
    assert previews.submit(source, 30) is waiting[0]
    with pytest.raises(hour_preview.PreviewBusy, match="busy"):
        previews.submit(source, 180)
    release.set()
    assert all(future.result(timeout=10) for future in (first, *waiting))
    assert previews.shutdown(5.0)


def test_shutdown_cancels_waiting_previews_and_joins_the_worker(
    tmp_path: Path, monkeypatch
) -> None:
    source, _power, gold = _station(tmp_path)
    _playing(source, gold[0], time.time() - 30)
    release, started = threading.Event(), threading.Event()
    _slow_previews(monkeypatch, release, started)
    previews = HourPreviews()
    running = previews.submit(source, 60)
    assert started.wait(10)
    queued = previews.submit(source, 30)
    # The worker is still making its preview: it is not joined in time.
    assert previews.shutdown(0.2) is False
    assert queued.cancelled()
    with pytest.raises(hour_preview.PreviewsClosed):
        previews.submit(source, 60)
    release.set()
    assert running.result(timeout=10)
    assert previews.shutdown(5.0) is True
    # This server's worker has ended (other tests' servers may keep their own).
    assert previews._thread is not None and not previews._thread.is_alive()


# -- parity with live picking --------------------------------------------------------


class _Plays:
    """A ledger that remembers plays, as the station's own does."""

    def __init__(self, played: dict[str, float] | None = None) -> None:
        self.played = dict(played or {})

    def setting_key(self, name: str) -> str:
        return name

    def get_setting(self, _name: str, default: Any = None) -> Any:
        return default

    def last_played(self) -> dict[str, float]:
        return self.played

    def remember_play(self, path: str, timestamp: float | None = None) -> None:
        # Keyed as the station's ledger keys them.
        from autodj.catalog import normalize_path

        self.played[normalize_path(path)] = float(timestamp or 0.0)

    def remember_setting(self, _name: str, _value: Any) -> None:
        """The live place is kept on the player in this test."""

    def recent_plays(self, _limit: int) -> list[dict]:
        return []

    def pending_history(self) -> list[dict]:
        return []


def _parity_station(tmp_path: Path):
    source, power, gold = _station(
        tmp_path, dmca=True, related={"Power 1": ["Power 2"], "Gold 3": ["Gold 4"]}
    )
    source.player._ledger = source.ledger = _Plays()
    # As the station's library tick does every two seconds.
    apply_repeat_windows(source.player)
    return source, power, gold


def _live_picks(player: Any, current: Any, handoff: float, end: float, seed: int | None) -> list:
    """Pick on the live player as the render worker does, on a clock that follows the songs.

    *seed*, when given, starts its random choices afresh.
    """
    clock = [handoff]
    player._now = lambda: clock[0]
    player._state.clock = lambda: clock[0]
    if seed is not None:
        player._random = random.Random(seed)
    average = average_length(player.playable_entries())
    liners = {entry.path for entry in player._library.entries_snapshot("liner")}
    state = player._state
    picks = []
    while handoff < end:
        clock[0] = handoff - song_seconds(player, current, average)
        context = player._pick_context(at=handoff)
        entry, _mode, added, cursor, _key = player._choose_station_next(current, context)
        player._pending_clock_cursor = cursor
        moment = handoff
        for clip, _queued in added:
            player._ledger.remember_play(clip.path, moment)
            picks.append(clip.path)
            moment += float(clip.length or 0.0)
        clock[0] = moment
        # As Player._start_playing does when the song starts.
        player._previous_track = state.current_track
        state.current_track = entry
        state.record_played(entry)
        state.track_number += 1
        player._ledger.remember_play(entry.path, moment)
        picks.append(entry.path)
        assert entry.path not in liners
        current, handoff = entry, moment + song_seconds(player, entry, average)
    return picks


def test_the_preview_picks_what_the_live_station_would(tmp_path: Path) -> None:
    source, _power, gold = _parity_station(tmp_path)
    player = source.player
    _playing(source, gold[0], NOW - 50)
    player._state.next_track = gold[1]
    player._pending_clock_cursor = 1
    player._pending_clock_name = "Main"
    decided = read_station(player, now=NOW)
    end = NOW + 2 * 3600
    predicted, _ends, stopped = hour_preview.predict(
        source, snapshot_player(player), decided, end, seed=42
    )
    assert stopped is None and len(predicted) > 20
    # The live player, from the same place, with the same random choices.
    player._state.current_track = gold[1]
    player._state.record_played(gold[1])
    player._state.play_history[-1] = PlayRecord.of(gold[1], decided.items[1].starts)
    player._ledger.remember_play(gold[1].path, decided.items[1].starts)
    live = _live_picks(player, gold[1], decided.handoff, end, 42)
    assert [item.entry.path for item in predicted] == live


def test_the_rotation_tester_picks_what_the_live_station_would(tmp_path: Path) -> None:
    source, _power, _gold = _parity_station(tmp_path)
    tested: list[str] = []
    simulate(
        source,
        SimulationRequest(picks=40, start=NOW, seed=9),
        on_pick=lambda _at, entry: tested.append(entry.path),
    )
    player = source.player
    clock = [NOW]
    player._now = lambda: clock[0]
    player._state.clock = lambda: clock[0]
    player._random = random.Random(9)
    player._seed_clock_cursor = None
    player._seed_clock_name = None
    player._pick_context(at=NOW)
    first = player._random_start_entry()
    player._pending_clock_cursor = player._seed_clock_cursor
    player._pending_clock_name = player._seed_clock_name
    player._previous_track = player._state.current_track
    player._state.current_track = first
    player._state.record_played(first)
    player._state.track_number += 1
    player._ledger.remember_play(first.path, NOW)
    average = average_length(player.playable_entries())
    live = [
        first.path,
        *_live_picks(
            player,
            first,
            NOW + song_seconds(player, first, average),
            NOW + 10 * 3600,
            None,  # the start pick has already drawn from the same choices
        ),
    ]
    assert tested == live[: len(tested)]
