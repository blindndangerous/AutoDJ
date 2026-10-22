"""Scheduled ad breaks (#51): spots, breaks, picking, booking, the play log and reports."""

from __future__ import annotations

import json
import math
import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
import soundfile as sf
from pydantic import ValidationError

from autodj.ads import (
    AdBreak,
    AdDesk,
    AdLog,
    AdsError,
    AdSettings,
    AdState,
    AdStore,
    BreakFields,
    Heard,
    PlayRecord,
    Spot,
    SpotFields,
    affidavit_csv,
    aired_plays,
    build_report,
    choose,
    field_error,
    latest,
    marks_between,
    new_id,
    report_rows,
    spot_file_name,
    summary_csv,
)
from autodj.breaks import AD, AD_SCHEDULE, DROPPED, QUEUED, RENDERED, BreakError
from autodj.config import AdsConfig, BreaksConfig
from autodj.stereo import SAMPLE_RATE as SR
from tests.unit._break_station import make_station, start_station

UTC_ZONE = ZoneInfo("UTC")
NOON = datetime(2026, 10, 13, 12, 0, tzinfo=UTC).timestamp()


def spot(advertiser="Example Motors", title="Fall sale", **fields) -> Spot:
    spot_id = fields.pop("spot_id", "a" * 32)
    values = {
        "title": title,
        "advertiser": advertiser,
        "file": spot_file_name(spot_id, ".wav"),
        "cue_start": 0.0,
        "cue_segue": fields.pop("seconds", 30.0),
        "cue_end": 30.4,
        "length_s": 31.0,
        **fields,
    }
    return Spot.model_validate(values)


def state(*spots: tuple[str, Spot], **settings) -> AdState:
    return AdState(AdSettings(**settings), dict(spots), {})


def ad_break(**fields) -> AdBreak:
    return AdBreak(**{"name": "Quarter past", "minutes": [15, 45], **fields})


# ---------------------------------------------------------------------------
# What the operator sets up
# ---------------------------------------------------------------------------


def refused(make) -> AdsError:
    """The field and words of the problem *make* is refused for."""
    with pytest.raises(ValidationError) as caught:
        make()
    return field_error(caught.value)


def test_a_spot_needs_a_title_an_advertiser_and_dates_in_order():
    assert refused(lambda: SpotFields(title="  ", advertiser="Example Motors")).field == "title"
    assert refused(lambda: SpotFields(title="Sale", advertiser="	")).field == "advertiser"
    late = refused(
        lambda: SpotFields(
            title="Sale",
            advertiser="Example Motors",
            start_date=date(2026, 10, 2),
            end_date=date(2026, 10, 1),
        )
    )
    assert (late.field, late.message) == (
        "end_date",
        "The last day must be on or after the first day",
    )
    unknown = refused(lambda: SpotFields.model_validate({"title": "a", "advertiser": "b", "x": 1}))
    assert (unknown.field, unknown.message) == ("x", "Unknown field x")
    fields = SpotFields(title="  Big\nsale ", advertiser="Example\x00Motors")
    assert (fields.title, fields.advertiser) == ("Big sale", "Example Motors")


def test_a_break_checks_its_minutes_hours_and_length():
    for values, name in (
        ({"minutes": [60]}, "minutes"),
        ({"days": [7]}, "days"),
        ({"from_hour": 6, "until_hour": 6}, "until_hour"),
        ({"max_seconds": 5}, "max_seconds"),
        ({"name": " "}, "name"),
    ):
        assert refused(lambda values=values: BreakFields(**{"name": "Ads", **values})).field == name
    item = BreakFields(name="Ads", minutes=[45, 15, 15], days=[4, 0])
    assert item.minutes == [15, 45] and item.days == [0, 4]
    assert item.when_text() == "At :15, :45 past the hour, on Monday and Friday"
    assert BreakFields(name="Ads").when_text() == "Only from a Schedule event or Run now"
    problem = refused(lambda: AdSettings(late_minutes=40, miss_after_minutes=30))
    assert problem.field == "miss_after_minutes"


def test_marks_keep_to_the_minutes_hours_days_and_dates():
    item = ad_break(from_hour=12, until_hour=14, days=[1])  # Tuesdays, 12:00 to 14:00
    marks = marks_between(item, NOON - 3600, NOON + 86400 * 7, UTC_ZONE)
    words = [datetime.fromtimestamp(mark, UTC).strftime("%a %H:%M") for mark in marks]
    assert words == ["Tue 12:15", "Tue 12:45", "Tue 13:15", "Tue 13:45"]
    ended = ad_break(end_date=date(2026, 10, 12))
    assert marks_between(ended, NOON, NOON + 7200, UTC_ZONE) == []
    # The bounds: after *after*, up to and including *until*.
    quarter = NOON + 15 * 60
    assert marks_between(ad_break(), quarter, quarter + 1800, UTC_ZONE) == [quarter + 1800]


def test_a_mark_that_daylight_saving_skips_runs_as_the_clock_jumps():
    zone = ZoneInfo("America/New_York")
    item = ad_break(minutes=[30], from_hour=2, until_hour=3)
    start = datetime(2026, 3, 8, 0, 0, tzinfo=zone).timestamp()
    (mark,) = marks_between(item, start, start + 6 * 3600, zone)
    assert datetime.fromtimestamp(mark, zone).strftime("%H:%M") == "03:00"


# ---------------------------------------------------------------------------
# Picking a break's spots
# ---------------------------------------------------------------------------


def test_the_spot_heard_longest_ago_goes_first_and_one_advertiser_once():
    first = ("1" * 32, spot("Example Motors", "Fall sale"))
    second = ("2" * 32, spot("Example Motors", "Winter tyres"))
    third = ("3" * 32, spot("Sample Bakery", "Fresh bread"))
    heard = [Heard("1" * 32, "Example Motors", "", NOON - 7200)]
    choice = choose(state(first, second, third), ad_break(), NOON, UTC_ZONE, heard)
    # Never heard first (by title), then one spot of each advertiser.
    assert [key for key, _spot in choice.spots] == ["3" * 32, "2" * 32]
    assert math.isclose(choice.seconds, 60.0)


def test_clockwheel_filters_choose_spots_and_explain_empty_matches():
    cars = ("1" * 32, spot("Example Motors", category="Car dealers"))
    bread = ("2" * 32, spot("Sample Bakery", category="Bakers"))
    pool = state(cars, bread)
    selected = choose(
        pool, ad_break(), NOON, UTC_ZONE, [], advertiser=" example motors ", category="CAR DEALERS"
    )
    assert [key for key, _ in selected.spots] == [cars[0]]
    assert (
        choose(pool, ad_break(), NOON, UTC_ZONE, [], advertiser="Missing").reason
        == "no spot is from advertiser Missing"
    )
    assert (
        choose(pool, ad_break(), NOON, UTC_ZONE, [], category="Missing").reason
        == "no spot is in category Missing"
    )
    heard = [Heard(cars[0], "Example Motors", "Car dealers", NOON)]
    missed = choose(pool, ad_break(), NOON, UTC_ZONE, heard, advertiser="Example Motors")
    assert "no spot from advertiser Example Motors could play" in missed.reason


def test_competing_categories_never_share_a_break_or_the_separation_time():
    cars = ("1" * 32, spot("Example Motors", category="Car dealers"))
    rival = ("2" * 32, spot("Sample Autos", category="car dealers"))
    bread = ("3" * 32, spot("Sample Bakery", "Bread"))
    choice = choose(state(cars, rival, bread), ad_break(), NOON, UTC_ZONE, [])
    assert len(choice.spots) == 2
    assert {spot.advertiser for _key, spot in choice.spots} >= {"Sample Bakery"}
    # Ten minutes after a car dealer aired, no car dealer may follow.
    heard = [Heard("9" * 32, "Other", "Car dealers", NOON - 600)]
    choice = choose(state(cars, rival, bread), ad_break(), NOON, UTC_ZONE, heard)
    assert [spot.advertiser for _key, spot in choice.spots] == ["Sample Bakery"]
    # With no separation, they may.
    choice = choose(
        state(cars, rival, bread, separation_minutes=0), ad_break(), NOON, UTC_ZONE, heard
    )
    assert len(choice.spots) == 2


def test_flight_dates_and_daily_and_hourly_limits_pass_a_spot_over():
    key = "1" * 32
    one = (key, spot(max_per_day=2, max_per_hour=1))
    today = [Heard(key, "Example Motors", "", NOON - 3 * 3600)]
    assert choose(state(one, separation_minutes=0), ad_break(), NOON, UTC_ZONE, today).spots
    this_hour = [Heard(key, "Example Motors", "", NOON + 60)]
    choice = choose(state(one, separation_minutes=0), ad_break(), NOON + 600, UTC_ZONE, this_hour)
    assert not choice.spots and "at its hourly limit" in choice.reason
    twice = [*today, Heard(key, "Example Motors", "", NOON - 2 * 3600)]
    choice = choose(state(one, separation_minutes=0), ad_break(), NOON, UTC_ZONE, twice)
    assert choice.reason == "no spot could play: 1 spot is at its daily limit"
    later = (key, spot(start_date=date(2026, 10, 14)))
    choice = choose(state(later), ad_break(), NOON, UTC_ZONE, [])
    assert choice.reason == "no spot could play: 1 spot is outside its flight dates"
    off = (key, spot(enabled=False))
    assert "turned off" in choose(state(off), ad_break(), NOON, UTC_ZONE, []).reason
    assert choose(state(), ad_break(), NOON, UTC_ZONE, []).reason == "the station has no spots"


def test_a_break_holds_no_more_spots_or_seconds_than_it_allows():
    spots = [(f"{n}" * 32, spot(f"Advertiser {n}", seconds=20.0)) for n in range(1, 6)]
    assert len(choose(state(*spots), ad_break(max_spots=2), NOON, UTC_ZONE, []).spots) == 2
    short = choose(state(*spots), ad_break(max_seconds=45), NOON, UTC_ZONE, [])
    assert len(short.spots) == 2 and short.seconds == 40.0
    long = (("9" * 32), spot("Long", seconds=60.0))
    choice = choose(state(long), ad_break(max_seconds=30), NOON, UTC_ZONE, [])
    assert choice.reason == "every spot that could play is longer than 30 seconds"


def test_a_spot_without_its_file_is_passed_over():
    one = ("1" * 32, spot())
    choice = choose(state(one), ad_break(), NOON, UTC_ZONE, [], usable=lambda _k, _s: False)
    assert "missing its audio file" in choice.reason


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return AdStore(tmp_path / "ads.json", tmp_path / "ads", max_spots=lambda: 2)


def test_the_store_saves_spots_breaks_and_settings_atomically(store):
    spot_id = new_id()
    store.add_spot(spot_id, spot(spot_id=spot_id))
    break_id = new_id()
    store.add_break(break_id, BreakFields(name="Morning", minutes=[15]))
    store.set_settings(AdSettings(separation_minutes=20))
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 1
    assert saved["spots"][spot_id]["advertiser"] == "Example Motors"
    assert saved["breaks"][break_id]["name"] == "Morning"
    assert saved["settings"]["separation_minutes"] == 20
    fresh = AdStore(store.path, store.folder).state()
    assert set(fresh.spots) == {spot_id} and fresh.settings.separation_minutes == 20
    updated = store.update_spot(spot_id, SpotFields(title="New", advertiser="Sample Bakery"))
    assert updated.file == spot_file_name(spot_id, ".wav") and updated.title == "New"
    with pytest.raises(AdsError) as caught:
        store.add_break(new_id(), BreakFields(name="morning"))
    assert caught.value.status == 409
    assert store.remove_break(break_id).name == "Morning"
    assert store.remove_break(break_id) is None
    assert not list(store.path.parent.glob(".ads.json.*.tmp"))


def test_the_store_keeps_at_most_max_spots(store):
    for _ in range(2):
        key = new_id()
        store.add_spot(key, spot(spot_id=key))
    with pytest.raises(AdsError) as caught:
        key = new_id()
        store.add_spot(key, spot(spot_id=key))
    assert caught.value.status == 409


def test_a_bad_entry_is_skipped_kept_and_said(store):
    good = new_id()
    store.add_spot(good, spot(spot_id=good))
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["spots"]["b" * 32] = {"title": "Broken"}
    data["breaks"]["not-an-id"] = {"name": "Odd"}
    data["settings"] = {"late_minutes": "soon"}
    store.path.write_text(json.dumps(data), encoding="utf-8")
    fresh = AdStore(store.path, store.folder)
    assert set(fresh.state().spots) == {good}
    problem = fresh.problem()
    assert "1 spot and 1 ad break in ads.json are not valid" in problem
    assert "settings in ads.json are not valid" in problem
    assert fresh.state().settings == AdSettings()
    # A save writes the skipped entries back as they were.
    fresh.set_settings(AdSettings())
    again = json.loads(store.path.read_text(encoding="utf-8"))
    assert again["spots"]["b" * 32] == {"title": "Broken"}
    assert again["breaks"]["not-an-id"] == {"name": "Odd"}


def test_a_file_that_is_not_ads_is_set_aside(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("not json", encoding="utf-8")
    assert store.state().spots == {}
    (bad,) = store.path.parent.glob("ads.json.bad-*")
    assert bad.read_text(encoding="utf-8") == "not json"


# ---------------------------------------------------------------------------
# The play log
# ---------------------------------------------------------------------------


def record(status="aired", *, scheduled=NOON, aired=None, logged=None, **fields) -> PlayRecord:
    values = {
        "id": fields.pop("id", "c" * 32),
        "run": fields.pop("run", "d" * 32),
        "break_name": "Quarter past",
        "scheduled": datetime.fromtimestamp(scheduled, UTC),
        "spot_id": "a" * 32,
        "title": "Fall sale",
        "advertiser": "Example Motors",
        "campaign": "Autumn",
        "seconds": 30.0,
        "status": status,
        "aired_at": None if aired is None else datetime.fromtimestamp(aired, UTC),
        "logged": datetime.fromtimestamp(logged or scheduled + 60, UTC),
        **fields,
    }
    return PlayRecord.model_validate(values)


def test_the_log_appends_by_month_and_survives_a_cut_line(tmp_path):
    log = AdLog(tmp_path / "ad_plays")
    log.append([record("booked")])
    path = log.folder / "2026-10.jsonl"
    with open(path, "ab") as handle:
        handle.write(b'{"v": 1, "id": "cut sho')  # a crash mid-line
    log.append([record("aired", aired=NOON + 90, logged=NOON + 120)])
    november = NOON + 40 * 86400
    log.append([record("missed", id="e" * 32, scheduled=november)])
    assert log.months() == ["2026-10", "2026-11"]
    found, damaged = log.read(
        datetime.fromtimestamp(NOON - 86400, UTC), datetime.fromtimestamp(NOON + 86400, UTC)
    )
    assert damaged == 1
    assert [item.status for item in found] == ["booked", "aired"]
    (final,) = latest(found).values()
    assert final.status == "aired" and final.delay_s() == 90


def test_reports_say_what_aired_late_or_missed_per_advertiser(tmp_path):
    log = AdLog(tmp_path / "ad_plays")
    log.append(
        [
            record("aired", id="1" * 32, aired=NOON + 120),
            record("aired", id="2" * 32, aired=NOON + 20 * 60, campaign="Winter"),
            record("missed", id="3" * 32, reason="skipped before it aired"),
            record(
                "missed",
                id="4" * 32,
                spot_id="",
                title="",
                advertiser="",
                campaign="",
                reason="no spot could play",
            ),
            record("booked", id="5" * 32, advertiser="=SUM(A1)"),
        ]
    )
    start = datetime(2026, 10, 13, tzinfo=UTC)
    view = build_report(log, start, start + timedelta(days=1), UTC_ZONE, 10)
    assert view["totals"] == {
        "aired": 2,
        "late": 1,
        "missed": 2,
        "interrupted": 0,
        "waiting": 1,
        "missed_breaks": 1,
    }
    statuses = [row["status_text"] for row in view["rows"]]
    assert sorted(statuses) == sorted(["Aired", "Aired late", "Missed", "Missed", "Waiting to air"])
    summary = {(group["advertiser"], group["campaign"]): group for group in view["summary"]}
    assert summary[("Example Motors", "Autumn")]["aired"] == 1
    assert summary[("Example Motors", "Autumn")]["missed"] == 1
    assert summary[("Example Motors", "Winter")]["late"] == 1
    rows = report_rows(
        log.read(start, start + timedelta(days=1))[0], start, start + timedelta(days=1)
    )
    text = affidavit_csv(rows, UTC_ZONE, 10)
    assert text.splitlines()[0].startswith("scheduled,aired,status,delay_minutes,advertiser")
    assert "'=SUM(A1)" in text
    assert "Aired late" in text and "no spot could play" in text
    assert summary_csv(rows, 10).splitlines()[0] == (
        "advertiser,campaign,aired,aired_late,missed,may_have_aired,waiting,seconds_aired"
    )
    plays = aired_plays(log.folder, start, start + timedelta(days=1))
    assert [(play.kind, play.artist, play.album) for play in plays] == [
        ("ad", "Example Motors", "Autumn"),
        ("ad", "Example Motors", "Winter"),
    ]
    with pytest.raises(ValueError, match="at most"):
        build_report(log, start, start + timedelta(days=500), UTC_ZONE, 10)


# ---------------------------------------------------------------------------
# The desk, on a real saved-point station
# ---------------------------------------------------------------------------


def write_spot(folder: Path, spot_id: str, seconds: float = 2.0) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    sound = np.zeros((round((seconds + 0.5) * SR), 2), dtype=np.float32)
    sound[: round(seconds * SR), 1] = 0.5
    path = folder / spot_file_name(spot_id, ".wav")
    sf.write(path, sound, SR, subtype="FLOAT")
    return path


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def station(tmp_path):
    """A saved-point station with breaks off and ads on, two spots and one break.

    The break has no minutes past the hour, so none is due: the station
    plans no song change until one is (:func:`run_now`).
    """
    player, entries, _points, desk = make_station(tmp_path)
    cfg = player._cfg
    cfg.breaks = BreaksConfig(enabled=False)
    cfg.ads = AdsConfig(enabled=True)
    player.bus = object()  # the server mixes the audio
    store = AdStore(tmp_path / "ads.json", tmp_path / "ads")
    for advertiser in ("Example Motors", "Sample Bakery"):
        key = new_id()
        write_spot(store.folder, key)
        store.add_spot(
            key,
            Spot(
                title="Spot",
                advertiser=advertiser,
                file=spot_file_name(key, ".wav"),
                cue_start=0.0,
                cue_segue=2.0,
                cue_end=2.4,
                length_s=2.5,
            ),
        )
    break_id = new_id()
    store.add_break(break_id, BreakFields(name="Top", minutes=[]))
    clock = Clock(time.time())
    ads = AdDesk(
        lambda: cfg.ads,
        store,
        AdLog(tmp_path / "ad_plays"),
        "UTC",
        player=player,
        breaks=desk,
        clock=clock,
    )
    ads.noticed()
    renders = start_station(player, entries)
    return player, entries, desk, ads, clock, break_id, renders


def plan(station) -> None:
    """What the render-ahead worker does when the desk asks for the open song change."""
    player, *_rest, renders = station
    player._plan_slot(renders[1])


def run_now(station) -> None:
    """Run now: the break is due, and the open song change is planned for it."""
    _player, _entries, _desk, ads, _clock, break_id, _renders = station
    ads.request(break_id, "manual")
    plan(station)


def log_lines(ads: AdDesk) -> list[PlayRecord]:
    found, _damaged = ads.log.read(
        datetime.fromtimestamp(ads.clock() - 86400, UTC),
        datetime.fromtimestamp(ads.clock() + 86400, UTC),
    )
    return found


def booked_spots(desk) -> list:
    return [item for item in desk.listing() if item.source == AD_SCHEDULE]


def test_with_no_break_due_the_station_plans_nothing(station):
    player, _entries, desk, _ads, _clock, _break_id, renders = station
    assert not desk.enabled and not desk.plans_slots and not player._breaks_on()
    assert renders[2] is None and player._slot_plan is None
    run_now(station)  # a break waits: now the change is planned
    assert desk.plans_slots and player._slot_plan is not None
    player._cfg.ads = AdsConfig(enabled=False)
    assert not desk.plans_slots and not player._breaks_on()


def test_a_break_due_within_the_plan_ahead_time_plans_the_change(station):
    _player, _entries, desk, ads, clock, break_id, _renders = station
    soon = datetime.fromtimestamp(clock.now + 600, UTC).minute
    ads.store.update_break(break_id, BreakFields(name="Top", minutes=[soon]))
    ads.noticed()
    assert ads.wants_slots() and desk.plans_slots
    later = datetime.fromtimestamp(clock.now, UTC).date() + timedelta(days=2)
    ads.store.update_break(break_id, BreakFields(name="Top", minutes=[soon], start_date=later))
    ads.noticed()
    assert not ads.wants_slots()


def test_due_ads_wake_the_playout_controller(station):
    player, _entries, _desk, ads, _clock, _break_id, _renders = station
    requested = []
    player._playout = type("Playout", (), {"request_plan": lambda _self: requested.append(1)})()

    ads._ask_for_plan()

    assert requested == [1]


def test_schedule_booking_retries_after_atomic_handoff_refusal(station, monkeypatch):
    """ADR-0004-11: scheduled batch booking retries after handoff revision refusal."""
    player, _entries, desk, ads, _clock, _break_id, _renders = station
    run_now(station)
    original = player.book_breaks
    calls = []

    def refuse_once(items, slot_id, *, transition_revision=None):
        calls.append(transition_revision)
        if len(calls) == 1:
            raise BreakError(409, "The handoff changed", "slot_closed")
        return original(items, slot_id, transition_revision=transition_revision)

    monkeypatch.setattr(player, "book_breaks", refuse_once)
    ads.tick()

    assert ads._pending
    assert not booked_spots(desk)
    assert not ads._booked
    assert len(calls) == 1 and calls[0]

    ads.tick()

    assert not ads._pending
    assert booked_spots(desk)
    assert ads._booked
    assert len(calls) == 2 and calls[1]


def test_a_schedule_event_due_soon_plans_the_change(station, tmp_path):
    _player, _entries, _desk, ads, clock, _break_id, _renders = station
    from autodj.scheduler import ScheduledEvent

    at = datetime.fromtimestamp(clock.now + 300, UTC)
    event = ScheduledEvent(
        id="e" * 32,
        name="Sponsor",
        action="ad_break",
        params={"name": "Top"},
        repeat="daily",
        time=at.time().replace(second=0, microsecond=0),
    )
    ads.events = lambda: [event]
    ads.noticed()
    assert ads.wants_slots()


def test_ads_on_with_no_break_due_picks_the_same_songs_as_ads_off(tmp_path):
    import random

    def picks(with_ads: bool, folder: Path) -> list[str]:
        player, entries, _points, desk = make_station(folder)
        player._cfg.breaks = BreaksConfig(enabled=False)
        player._cfg.ads = AdsConfig(enabled=with_ads)
        player.bus = object()
        if with_ads:
            store = AdStore(folder / "ads.json", folder / "ads")
            store.add_break(new_id(), BreakFields(name="Later", minutes=[], enabled=True))
            ads = AdDesk(
                lambda: player._cfg.ads,
                store,
                AdLog(folder / "ad_plays"),
                "UTC",
                player=player,
                breaks=desk,
            )
            ads.noticed()
        player._rng = np.random.default_rng(5)
        player._random = random.Random(5)
        random.seed(5)
        player._set_render_cursor(entries[0], 0, "seed")
        render = player._next_rendered()
        found = [render.entry.path]
        for _ in range(12):
            player._on_track_start(render)
            render = player._next_rendered()
            player._plan_slot(render)  # the render worker's plan hook
            found.append(render.entry.path)
        assert player._slot_plan is None
        return found

    assert picks(True, tmp_path / "on") == picks(False, tmp_path / "off")


def test_the_mix_bus_never_plans_or_rechecks_a_song_change(station, monkeypatch):
    import threading

    from autodj.player import Player

    player, _entries, _desk, _ads, _clock, _break_id, renders = station
    seen: list[tuple[str, str]] = []
    for name in ("_claim_slot_plan", "_recheck_slot_plan", "_plan_slot"):
        real = getattr(Player, name)

        def spy(self, *args, _real=real, _name=name, **kwargs):
            seen.append((_name, threading.current_thread().name))
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(Player, name, spy)
    run_now(station)
    _render_a, render_b, _plan = renders

    def bus_thread(work) -> None:
        thread = threading.Thread(target=work, name="mix-bus")
        thread.start()
        thread.join()

    bus_thread(lambda: player._on_track_start(render_b))
    render_c = player._next_rendered()  # the render worker
    bus_thread(lambda: player._on_track_start(render_c))
    for first, _id, end in render_c.break_events:
        bus_thread(
            lambda first=first, end=end: (player._on_position(first + 1), player._on_position(end))
        )
    player.refresh_playout()  # a queue edit, from an API thread
    assert {name for name, _thread in seen} >= {"_claim_slot_plan", "_recheck_slot_plan"}
    assert not [entry for entry in seen if entry[1] == "mix-bus"]
    # And by the source: no bus callback reaches them.
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(Player))
    methods = {node.name: node for node in tree.body[0].body if isinstance(node, ast.FunctionDef)}

    def calls(name: str) -> set[str]:
        return {
            node.attr
            for node in ast.walk(methods[name])
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr in methods
        }

    reached: set[str] = set()
    todo = ["_on_track_start", "_on_position", "_take_render"]
    while todo:
        name = todo.pop()
        if name in reached:
            continue
        reached.add(name)
        todo.extend(calls(name))
    assert not reached & {
        "_claim_slot_plan",
        "_recheck_slot_plan",
        "_plan_slot",
        "refresh_playout",
    }


def test_a_due_break_is_booked_rendered_aired_and_logged(station):
    player, _entries, desk, ads, clock, _break_id, renders = station
    run_now(station)
    ads.tick()
    booked = booked_spots(desk)
    assert len(booked) == 2
    assert {item.kind for item in booked} == {AD} and all(item.keep_file for item in booked)
    assert all(item.state == QUEUED for item in booked)
    assert player._slot_plan.pinned
    assert [line.status for line in log_lines(ads)] == ["booked", "booked"]
    assert ads.wants_slots()
    # The change is rendered with both spots between the songs.
    _render_a, render_b, _plan = renders
    player._on_track_start(render_b)
    render_c = player._next_rendered()
    assert len(render_c.break_events) == 2
    assert {desk.get(item.break_id).state for item in booked} == {RENDERED}
    assert {desk.get(item.break_id).mixed_as for item in booked} == {"standalone"}
    player._on_track_start(render_c)
    for first, _id, end in render_c.break_events:
        player._on_position(first + 1)
        player._on_position(end)
    clock.now += 60
    ads.tick()
    final = latest(log_lines(ads))
    assert sorted(line.status for line in final.values()) == ["aired", "aired"]
    assert all(line.aired_at is not None for line in final.values())
    assert sorted(play.at for play in ads._heard) == pytest.approx(
        sorted(line.aired_at.timestamp() for line in final.values())
    )
    assert len(ads.recent()) == 2
    restarted = AdDesk(lambda: player._cfg.ads, ads.store, ads.log, "UTC", clock=clock)
    restarted.tick()
    assert sorted(play.at for play in restarted._heard) == pytest.approx(
        sorted(line.aired_at.timestamp() for line in final.values())
    )
    # The spots' files stay: they are the station's, not uploads.
    desk.maintain()
    assert all(item.path.exists() for item in booked)
    assert ads.plays_today()  # they count toward the daily limits


def _clock_step(number=2, **fields):
    return {
        "index": number - 1,
        "number": number,
        "clock": "Morning",
        "step": {"type": "ad_break", "max_spots": 3, "max_seconds": 0, **fields},
    }


def _open_clock_step(station, *steps):
    player, _entries, _desk, ads, _clock, _break_id, _renders = station
    run_now(station)
    with ads._lock:
        ads._pending.clear()
    player._slot_plan.ad_steps = tuple(steps)
    return player._slot_plan


def test_clockwheel_step_books_once_and_airs_in_standalone_render(station):
    player, _entries, desk, ads, clock, _break_id, renders = station
    opened = _open_clock_step(station, _clock_step())
    ads.tick()
    assert ads.handled_count(opened.slot_id) == 1
    assert len(booked_spots(desk)) == 2
    ads.tick()
    assert len(booked_spots(desk)) == 2
    assert all(line.source == "clockwheel" for line in log_lines(ads))
    player._on_track_start(renders[1])
    rendered = player._next_rendered()
    assert len(rendered.break_events) == 2
    assert all(desk.get(item.break_id).mixed_as == "standalone" for item in booked_spots(desk))
    player._on_track_start(rendered)
    for first, _id, end in rendered.break_events:
        player._on_position(first + 1)
        player._on_position(end)
    clock.now += 60
    ads.tick()
    assert {line.status for line in latest(log_lines(ads)).values()} == {"aired"}


def test_two_clockwheel_steps_book_distinct_advertisers(station):
    _player, _entries, desk, ads, _clock, _break_id, _renders = station
    _open_clock_step(station, _clock_step(max_spots=1), _clock_step(3, max_spots=1))
    ads.tick()
    assert len(booked_spots(desk)) == 2
    assert {line.advertiser for line in log_lines(ads)} == {"Example Motors", "Sample Bakery"}


def test_clockwheel_step_with_ads_off_writes_no_report_line(station):
    player, _entries, _desk, ads, _clock, _break_id, renders = station
    _open_clock_step(station, _clock_step())
    player._cfg.ads = AdsConfig(enabled=False)
    ads.tick()
    player._on_track_start(renders[1])
    player._next_rendered()
    ads.tick()
    assert not log_lines(ads)


def test_clockwheel_step_refusal_is_reported(station):
    player, _entries, _desk, ads, _clock, _break_id, _renders = station
    _open_clock_step(station, _clock_step())
    player._cfg.playback = replace(player._cfg.playback, transition_mode="fixed")
    ads.tick()
    lines = log_lines(ads)
    assert len(lines) == 1 and "saved segue playback" in lines[0].reason


def test_clockwheel_step_requests_slot_and_plan_keeps_snapshot(station, monkeypatch):
    player, entries, _desk, ads, _clock, _break_id, _renders = station
    player._cfg.playback = replace(player._cfg.playback, pick_mode="clock")
    player._clock_has_ad_steps = True
    assert ads.wants_slots()
    step = _clock_step()

    def choose(*_args, **_kwargs):
        player._chosen_clock_ad_steps = (step,)
        player._chosen_clock_revision = player._clock_revision
        return entries[3], "clock", (), 0, ""

    monkeypatch.setattr(player, "_choose_station_next", choose)
    plan(station)
    assert player._slot_plan.ad_steps == (step,)


def test_clockwheel_step_missing_spot_and_render_race_are_reported(station):
    _player, _entries, _desk, ads, _clock, _break_id, _renders = station
    _open_clock_step(station, _clock_step(advertiser="Missing"))
    ads.tick()
    missed = log_lines(ads)
    assert len(missed) == 1 and missed[0].status == "missed"
    assert "no spot is from advertiser Missing" in missed[0].reason


def test_clockwheel_step_rendered_before_booking_is_reported(station):
    player, _entries, _desk, ads, _clock, _break_id, renders = station
    _open_clock_step(station, _clock_step(4))
    player._on_track_start(renders[1])
    player._next_rendered()
    ads.tick()
    assert any("closed before its spots could be booked" in line.reason for line in log_lines(ads))


def test_clockwheel_forecast_does_not_book_or_log(station):
    _player, _entries, desk, ads, clock, _break_id, _renders = station
    heard = ads.heard_snapshot()
    forecast = ads.forecast_step(_clock_step(), clock.now, heard)
    assert forecast is not None and forecast.seconds == 4.0
    assert len(heard) == 2
    assert not booked_spots(desk) and not log_lines(ads)


def test_consecutive_clockwheel_forecasts_share_heard_spots(station):
    _player, _entries, _desk, ads, clock, _break_id, _renders = station
    heard = ads.heard_snapshot()
    first = ads.forecast_step(_clock_step(max_spots=1), clock.now, heard)
    second = ads.forecast_step(_clock_step(3, max_spots=1), clock.now, heard)
    assert first is not None and second is not None
    assert first.seconds == second.seconds == 2.0
    assert len({play.advertiser for play in heard}) == 2


def test_delayed_break_uses_expected_airtime_across_midnight_and_separation(station, monkeypatch):
    player, _entries, _desk, ads, clock, break_id, _renders = station
    now = datetime(2026, 10, 13, 23, 58, tzinfo=UTC).timestamp()
    airtime = now + 180
    clock.now = now
    run = ads.request(break_id, "manual", at=now + 60)
    plan(station)
    slot, _closes_in, _airs_in = player.slot_status()
    monkeypatch.setattr(player, "slot_status", lambda: (slot, 0, 180))

    target_id, other_id = sorted(ads.store.state().spots)
    target = ads.store.state().spots[target_id]
    ads.store.update_spot(
        target_id,
        SpotFields.model_validate(
            {
                **target.model_dump(
                    exclude={"file", "cue_start", "cue_segue", "cue_end", "length_s", "created_at"}
                ),
                "category": "cars",
                "start_date": date(2026, 10, 14),
            }
        ),
    )
    other = ads.store.state().spots[other_id]
    ads.store.update_spot(
        other_id,
        SpotFields.model_validate(
            {
                **other.model_dump(
                    exclude={"file", "cue_start", "cue_segue", "cue_end", "length_s", "created_at"}
                ),
                "enabled": False,
            }
        ),
    )
    ads.store.set_settings(AdSettings(separation_minutes=15))
    target = ads.store.state().spots[target_id]
    prior_play = Heard(target_id, target.advertiser, target.category, airtime - 15 * 60)
    ads._heard.append(prior_play)
    state = ads.store.cached()
    item = state.breaks[break_id]
    assert not choose(state, item, run.scheduled, UTC_ZONE, [prior_play]).spots

    fields = SpotFields.model_validate(
        {
            **target.model_dump(
                exclude={"file", "cue_start", "cue_segue", "cue_end", "length_s", "created_at"}
            ),
            "start_date": None,
        }
    )
    ads.store.update_spot(target_id, fields)
    state = ads.store.cached()
    assert not choose(state, item, run.scheduled, UTC_ZONE, [prior_play]).spots
    assert [key for key, _spot in choose(state, item, airtime, UTC_ZONE, [prior_play]).spots] == [
        target_id
    ]
    ads.store.update_spot(target_id, fields.model_copy(update={"start_date": date(2026, 10, 14)}))

    records = ads._try_book(run, now, now + 30 * 60)

    assert records is not None and [record.spot_id for record in records] == [target_id]
    assert ads._booked[records[0].id].heard.at == airtime


def test_a_skipped_spot_is_logged_as_missed_with_its_reason(station):
    player, _entries, _desk, ads, _clock, _break_id, renders = station
    run_now(station)
    ads.tick()
    _render_a, render_b, _plan = renders
    player._on_track_start(render_b)
    render_c = player._next_rendered()
    player._on_track_start(render_c)
    render_d = player._next_rendered()
    player._on_track_start(render_d)  # c was skipped before its ads
    ads.tick()
    final = latest(log_lines(ads)).values()
    assert {(line.status, line.reason) for line in final} == {("missed", "skipped before it aired")}
    assert ads.plays_today() == {}  # missed spots do not count


def test_a_spot_skipped_on_the_air_may_have_aired(station):
    player, _entries, _desk, ads, _clock, _break_id, renders = station
    run_now(station)
    ads.tick()
    _render_a, render_b, _plan = renders
    player._on_track_start(render_b)
    render_c = player._next_rendered()
    player._on_track_start(render_c)
    first, _id, _end = render_c.break_events[0]
    player._on_position(first + 1)  # the first spot is on the air
    render_d = player._next_rendered()
    player._on_track_start(render_d)  # then the song is skipped
    ads.tick()
    final_records = latest(log_lines(ads)).values()
    final = sorted((line.status, line.reason) for line in final_records)
    assert final == [
        ("interrupted", "it may have aired in part: cut short: the song was skipped"),
        ("missed", "skipped before it aired"),
    ]
    interrupted = next(line for line in final_records if line.status == "interrupted")
    assert interrupted.aired_at is not None
    assert [play.at for play in ads._heard] == pytest.approx([interrupted.aired_at.timestamp()])


def test_a_stop_records_the_spot_on_the_air_and_the_ones_waiting(station):
    player, _entries, _desk, ads, _clock, _break_id, renders = station
    run_now(station)
    ads.tick()
    _render_a, render_b, _plan = renders
    player._on_track_start(render_b)
    render_c = player._next_rendered()
    player._on_track_start(render_c)
    first, _id, _end = render_c.break_events[0]
    player._on_position(first + 1)
    assert ads.needs_flush()
    ads.flush()
    final = sorted((line.status, line.reason) for line in latest(log_lines(ads)).values())
    assert final == [
        ("interrupted", "it may have aired: AutoDJ stopped during the break"),
        ("missed", "AutoDJ stopped before it aired"),
    ]
    assert not ads.booked_spots()


def test_a_break_that_cannot_join_a_change_in_time_is_missed(station):
    player, _entries, desk, ads, clock, _break_id, _renders = station
    player._cfg.playback.transition_mode = "crossfade"
    run_now(station)
    ads.tick()
    assert ads.pending() and not booked_spots(desk)
    clock.now += 31 * 60
    ads.tick()
    (line,) = log_lines(ads)
    assert line.status == "missed" and line.spot_id == ""
    assert line.reason.startswith("it could not air in time: Ad breaks air only with saved segue")
    assert not ads.pending()


def test_a_break_with_no_spot_to_play_is_missed_with_why(station):
    _player, _entries, _desk, ads, _clock, _break_id, _renders = station
    for key in ads.store.state().spots:
        ads.store.update_spot(key, SpotFields(title="Spot", advertiser="X", enabled=False))
    run_now(station)
    ads.tick()
    (line,) = log_lines(ads)
    assert line.status == "missed"
    assert line.reason == "no spot could play: 2 spots are turned off"


def test_minutes_past_the_hour_run_the_break_and_the_miss_time_rules_a_sleep(station):
    _player, _entries, _desk, ads, clock, break_id, _renders = station
    ads.tick()  # starts: counts from now
    mark = (math.floor(clock.now / 60) + 2) * 60
    minute = datetime.fromtimestamp(mark, UTC).minute
    ads.store.update_break(break_id, BreakFields(name="Top", minutes=[minute]))
    ads.tick()  # due within the plan-ahead time: the change is planned
    plan(station)
    clock.now = mark + 5
    ads.tick()
    assert [line.status for line in log_lines(ads)] == ["booked", "booked"]
    assert {line.source for line in log_lines(ads)} == {"clock"}
    # Asleep 5 minutes past the next hour's mark, within Missed after (30):
    # it still joins a song change, to air late.
    clock.now = mark + 3600 + 300
    ads.tick()
    late = [line for line in log_lines(ads) if line.scheduled.timestamp() == mark + 3600]
    assert [line.status for line in late] == ["booked", "booked"]
    # Asleep past Missed after: it is logged as missed, not run late.
    clock.now = mark + 2 * 3600 + 31 * 60
    ads.tick()
    missed = [line for line in log_lines(ads) if line.status == "missed" and not line.spot_id]
    assert any("slept" in line.reason for line in missed)


def test_a_long_sleep_logs_every_missed_break_with_a_summary_line(station):
    _player, _entries, _desk, ads, clock, break_id, _renders = station
    ads.tick()
    ads.store.update_break(break_id, BreakFields(name="Top", minutes=list(range(0, 60, 5))))
    start = clock.now
    clock.now += 24 * 3600 + 3600  # 25 hours of marks, 12 an hour
    ads.tick()
    missed = [line for line in log_lines(ads) if line.status == "missed" and not line.spot_id]
    marks = marks_between(
        ads.store.state().breaks[break_id], start, clock.now - 30 * 60, ZoneInfo("UTC")
    )
    assert len(missed) == 201
    summary = missed[-1]
    assert summary.count == len(marks) - 200 and summary.last_scheduled is not None
    assert f"{summary.count} more ad breaks (Top)" in summary.reason
    begin = datetime.fromtimestamp(start, UTC)
    view = build_report(ads.log, begin - timedelta(days=1), begin + timedelta(days=2), UTC_ZONE, 10)
    assert view["totals"]["missed_breaks"] == len(marks)


def test_breaks_due_while_autodj_was_not_running_are_logged_at_the_next_start(station):
    player, _entries, _desk, ads, clock, break_id, _renders = station
    ads.store.update_break(break_id, BreakFields(name="Top", minutes=[0, 30]))
    ads.tick()
    ads.flush()  # a stop saves where the schedule was
    assert ads.log.cursor() == pytest.approx(clock.now)
    clock.now += 3 * 3600
    restarted = AdDesk(lambda: player._cfg.ads, ads.store, ads.log, "UTC", clock=clock)
    restarted.tick()
    missed = [line for line in log_lines(ads) if line.status == "missed"]
    assert len(missed) >= 5
    assert {line.reason for line in missed} == {"AutoDJ was not running"}


def test_a_start_with_ads_off_owes_no_breaks_for_that_time(station):
    player, _entries, _desk, ads, clock, break_id, _renders = station
    ads.tick()
    ads.flush()
    player._cfg.ads = AdsConfig(enabled=False)
    ads.station_started()
    assert ads.log.cursor() is None
    player._cfg.ads = AdsConfig(enabled=True)
    clock.now += 3 * 3600
    ads.store.update_break(break_id, BreakFields(name="Top", minutes=[0, 30]))
    AdDesk(lambda: player._cfg.ads, ads.store, ads.log, "UTC", clock=clock).tick()
    assert [line for line in log_lines(ads) if line.status == "missed"] == []


def test_spots_left_booked_by_a_crash_are_logged_as_missed_at_the_next_start(station):
    player, _entries, _desk, ads, clock, _break_id, _renders = station
    run_now(station)
    ads.tick()
    restarted = AdDesk(lambda: player._cfg.ads, ads.store, ads.log, "UTC", clock=clock)
    restarted.tick()
    final = latest(log_lines(ads)).values()
    assert {(line.status, line.reason) for line in final} == {
        ("missed", "AutoDJ stopped before it aired")
    }


def test_a_failed_log_write_is_kept_and_written_later(station, monkeypatch):
    _player, _entries, _desk, ads, _clock, _break_id, _renders = station
    real = AdLog.append
    calls = []

    def fails_once(self, records):
        calls.append(len(records))
        if len(calls) == 1:
            raise OSError("disk full")
        real(self, records)

    monkeypatch.setattr(AdLog, "append", fails_once)
    run_now(station)
    ads.tick()
    assert log_lines(ads) == []
    ads.tick()
    assert [line.status for line in log_lines(ads)] == ["booked", "booked"]


def test_lines_a_stop_cannot_write_are_kept_aside_for_the_next_start(station, monkeypatch, caplog):
    player, _entries, _desk, ads, clock, _break_id, _renders = station
    real = AdLog.append
    monkeypatch.setattr(
        AdLog, "append", lambda self, records: (_ for _ in ()).throw(OSError("away"))
    )
    run_now(station)
    ads.tick()
    with caplog.at_level("ERROR"):
        ads.flush()
    assert ads.side_file.exists()
    assert "Could not write 4 ad play log lines" in caplog.text
    monkeypatch.setattr(AdLog, "append", real)
    AdDesk(lambda: player._cfg.ads, ads.store, ads.log, "UTC", clock=clock).tick()
    assert not ads.side_file.exists()
    final = sorted(line.status for line in latest(log_lines(ads)).values())
    assert final == ["missed", "missed"]


def test_a_spot_is_never_deleted_as_it_is_booked(station):
    _player, _entries, desk, ads, _clock, _break_id, _renders = station
    first = sorted(ads.store.state().spots)[0]
    assert ads.remove_spot(first) is not None
    run_now(station)
    ads.tick()
    assert [item.label for item in booked_spots(desk)] == ["Sample Bakery: Spot"] or [
        item.label for item in booked_spots(desk)
    ] == ["Example Motors: Spot"]
    (left,) = ads.store.state().spots
    with pytest.raises(AdsError) as caught:
        ads.remove_spot(left)
    assert caught.value.status == 409
    assert left in ads.store.state().spots


def test_a_new_month_file_flushes_its_folder(tmp_path, monkeypatch):
    import autodj.ads as ads_module

    flushed = []
    monkeypatch.setattr(ads_module, "fsync_directory", lambda path: flushed.append(path))
    log = AdLog(tmp_path / "ad_plays")
    log.append([record("booked")])
    log.append([record("aired", aired=NOON + 90)])
    assert flushed == [log.folder]


def test_the_forecast_guesses_each_run_without_booking(station):
    player, _entries, desk, ads, clock, break_id, _renders = station
    ads.store.update_break(break_id, BreakFields(name="Top", minutes=[0, 30]))
    ads.noticed()
    found = ads.forecast(clock.now, clock.now + 3600)
    assert len(found) == 2
    assert all(run.label == "Ad break: Top" and run.own_point for run in found)
    assert found[0].detail == "2 spots, 0:04"
    assert not booked_spots(desk)
    player._cfg.ads = AdsConfig(enabled=False)
    assert ads.forecast(clock.now, clock.now + 3600) == []


def test_dropping_a_booked_ad_keeps_its_file(station):
    _player, _entries, desk, ads, _clock, _break_id, _renders = station
    run_now(station)
    ads.tick()
    item = booked_spots(desk)[0]
    desk.drop(item.break_id, "test")
    assert desk.get(item.break_id).state == DROPPED
    desk.maintain()
    assert item.path.exists()


def test_a_stop_writes_what_aired_without_booking_more(station):
    player, _entries, _desk, ads, _clock, break_id, renders = station
    ads.flush()  # before the first tick: nothing to write, nothing read
    assert log_lines(ads) == []
    run_now(station)
    ads.tick()
    _render_a, render_b, _plan = renders
    player._on_track_start(render_b)
    render_c = player._next_rendered()
    player._on_track_start(render_c)
    for first, _id, end in render_c.break_events:
        player._on_position(first + 1)
        player._on_position(end)
    ads.request(break_id, "manual")
    ads.flush()
    assert sorted(line.status for line in latest(log_lines(ads)).values()) == ["aired", "aired"]
    assert len(ads.pending()) == 1  # the new run is not booked by a flush
