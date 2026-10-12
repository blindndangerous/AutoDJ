"""Start and end dates on scheduled events, and chosen days for every-few-minutes events."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from autodj.scheduler import EventError, describe_range, describe_repeat, next_run

from .test_scheduler import DENVER, Rig, field_of, runs, spec, utc

UTC_ZONE = ZoneInfo("UTC")


# -- next_run -----------------------------------------------------------------


def test_daily_runs_only_within_its_dates_both_included() -> None:
    item = spec(time="00:00", start_date="2026-11-27", end_date="2026-12-26")
    season = runs(item, "2026-10-01 00:00", 30)
    assert season[0] == "2026-11-27 07:00"
    assert season[-1] == "2026-12-26 07:00"
    assert next_run(item, utc("2026-12-26 07:00"), DENVER) is None


def test_the_holiday_season_example(tmp_path: Path) -> None:
    """Holiday clockwheel from 27 November to 26 December, all day, then back."""
    rig = Rig(tmp_path / "schedule.json", now="2026-11-26 12:00", timezone="America/Denver")
    rig.schedule.start()
    rig.add(name="Holiday music", time="00:00", start_date="2026-11-27", end_date="2026-12-26")
    rig.add(name="Regular music", repeat="once", date="2026-12-27", time="00:00")

    def ran_at(when: str) -> list[str]:
        before = len(rig.schedule.runs)
        # Tick a minute later, as if the server had been running all along.
        rig.schedule.cursor = utc(when) - timedelta(minutes=1)
        rig.tick_at(when)
        return [run.event for run in rig.schedule.runs[before:]]

    assert ran_at("2026-11-27 07:00") == ["Holiday music"]  # midnight in Denver
    assert ran_at("2026-12-26 07:00") == ["Holiday music"]
    rows = rig.schedule.upcoming(5)
    assert [(when.isoformat(), event.name) for when, event in rows] == [
        ("2026-12-27T07:00:00+00:00", "Regular music")
    ]
    assert ran_at("2026-12-27 07:00") == ["Regular music"]
    assert ran_at("2026-12-28 07:00") == []
    assert rig.schedule.upcoming(5) == []
    assert all(run.result == "ok" for run in rig.schedule.runs)


def test_next_runs_skip_events_before_their_start(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "schedule.json")
    rig.schedule.start()
    rig.add(name="Later", time="06:00", start_date="2026-10-20")
    rig.add(name="Now", time="13:00")
    rows = rig.schedule.upcoming(3)
    assert [(when.strftime("%d %H:%M"), event.name) for when, event in rows] == [
        ("11 13:00", "Now"),
        ("12 13:00", "Now"),
        ("13 13:00", "Now"),
    ]
    later = [event for event in rig.schedule.events.values() if event.name == "Later"]
    assert rig.schedule.next_time(later[0]) == utc("2026-10-20 06:00")


def test_a_rule_stops_after_its_end_date(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "schedule.json")
    rig.schedule.start()
    item = rig.add(time="12:01", end_date="2026-10-11")
    rig.tick_at("2026-10-11 12:01")
    rig.schedule.cursor = utc("2026-10-12 12:00")
    rig.tick_at("2026-10-12 12:01")
    assert rig.calls == ["action"]
    assert rig.schedule.next_time(item) is None


def test_overnight_window_on_a_chosen_day_runs_past_the_end_date() -> None:
    # Fridays 18:00 to 02:00; 25 December 2026 is a Friday.
    item = spec(
        repeat="interval",
        interval_minutes=60,
        window_start="18:00",
        window_end="02:00",
        days=[4],
        start_date="2026-12-19",
        end_date="2026-12-25",
    )
    # The window that began on Friday 18 December, before the start date, is left out.
    assert runs(item, "2026-12-18 23:30", 9, UTC_ZONE) == [
        "2026-12-25 18:00",
        "2026-12-25 19:00",
        "2026-12-25 20:00",
        "2026-12-25 21:00",
        "2026-12-25 22:00",
        "2026-12-25 23:00",
        "2026-12-26 00:00",
        "2026-12-26 01:00",
        "2026-12-26 02:00",
    ]
    assert next_run(item, utc("2026-12-26 02:00"), UTC_ZONE) is None
    # Without a start date, the window that began on Friday runs on into Saturday.
    open_start = spec(**{**item.model_dump(mode="json"), "start_date": None})
    assert runs(open_start, "2026-12-18 23:30", 1, UTC_ZONE) == ["2026-12-19 00:00"]
    # Other days get nothing.
    assert runs(open_start, "2026-12-19 02:00", 1, UTC_ZONE) == ["2026-12-25 18:00"]


def test_chosen_days_without_dates_for_every_few_minutes() -> None:
    item = spec(repeat="interval", interval_minutes=30, days=[0])  # Mondays, all day
    assert runs(item, "2026-10-11 23:00", 2, UTC_ZONE) == ["2026-10-12 00:00", "2026-10-12 00:30"]
    assert runs(item, "2026-10-12 23:30", 1, UTC_ZONE) == ["2026-10-19 00:00"]


def test_a_start_date_on_the_spring_forward_day() -> None:
    # 02:30 does not exist in Denver on 8 March 2026; the run is at the jump.
    item = spec(time="02:30", start_date="2026-03-08", end_date="2026-03-08")
    assert runs(item, "2026-03-01 00:00", 1) == ["2026-03-08 09:00"]
    assert next_run(item, utc("2026-03-08 09:00"), DENVER) is None


def test_an_overnight_window_ending_on_the_spring_forward_night() -> None:
    item = spec(
        repeat="interval",
        interval_minutes=60,
        window_start="22:00",
        window_end="03:00",
        end_date="2026-03-07",
    )
    # 02:00 falls in the gap and runs at the jump, together with 03:00.
    assert runs(item, "2026-03-07 12:00", 5) == [
        "2026-03-08 05:00",
        "2026-03-08 06:00",
        "2026-03-08 07:00",
        "2026-03-08 08:00",
        "2026-03-08 09:00",
    ]
    assert next_run(item, utc("2026-03-08 09:00"), DENVER) is None


def test_an_end_date_on_the_fall_back_day() -> None:
    # 01:30 happens twice in Denver on 1 November 2026: it runs the first time only.
    item = spec(time="01:30", end_date="2026-11-01")
    assert runs(item, "2026-10-31 12:00", 1) == ["2026-11-01 07:30"]
    assert next_run(item, utc("2026-11-01 07:30"), DENVER) is None


def test_an_overnight_window_ending_on_the_fall_back_night() -> None:
    item = spec(
        repeat="interval",
        interval_minutes=60,
        window_start="22:00",
        window_end="02:00",
        end_date="2026-10-31",
    )
    # 01:00 runs once, the first time; 02:00 is after the clock went back.
    assert runs(item, "2026-10-31 12:00", 5) == [
        "2026-11-01 04:00",
        "2026-11-01 05:00",
        "2026-11-01 06:00",
        "2026-11-01 07:00",
        "2026-11-01 09:00",
    ]
    assert next_run(item, utc("2026-11-01 09:00"), DENVER) is None


def test_weekdays_with_no_weekday_in_range_never_run() -> None:
    item = spec(repeat="weekdays", start_date="2026-10-17", end_date="2026-10-18")
    assert next_run(item, utc("2026-10-01 00:00"), DENVER) is None


# -- validation and words ---------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        (
            {"start_date": "2026-12-26", "end_date": "2026-11-27"},
            "end_date",
            "End date must be on or after the start date",
        ),
        (
            {"repeat": "once", "date": "2026-12-27", "end_date": "2026-12-26"},
            "date",
            "Date must be within the start and end dates",
        ),
        (
            {"repeat": "interval", "interval_minutes": 5, "days": [7]},
            "days",
            "Days must be numbered 0 (Monday) to 6 (Sunday)",
        ),
    ],
)
def test_bad_dates_name_the_field(changes: dict[str, Any], field: str, message: str) -> None:
    assert field_of(**changes) == (field, message)


def test_an_unreadable_date_names_its_field() -> None:
    field, message = field_of(start_date="27/11/2026")
    assert field == "start_date"
    assert message.startswith("Start date: ")


def test_chosen_days_are_kept_for_every_few_minutes_only() -> None:
    assert spec(repeat="interval", interval_minutes=5, days=[4, 4, 0]).days == [0, 4]
    assert spec(repeat="weekdays", days=[4]).days == []
    once = spec(repeat="once", date="2026-12-01", start_date="2026-11-27", end_date="2026-12-26")
    assert (once.start_date is not None, once.end_date is not None) == (True, True)


def test_describe_dates_in_words() -> None:
    season = spec(time="00:00", start_date="2026-11-27", end_date="2026-12-26")
    assert describe_repeat(season) == "Every day at 00:00, from 27 November to 26 December 2026"
    assert describe_repeat(spec(start_date="2026-12-20", end_date="2027-01-02")) == (
        "Every day at 07:00, from 20 December 2026 to 2 January 2027"
    )
    assert describe_repeat(spec(repeat="weekdays", start_date="2027-03-01")) == (
        "Weekdays at 07:00, starting 1 March 2027"
    )
    assert (
        describe_repeat(spec(end_date="2026-12-26"))
        == "Every day at 07:00, ending 26 December 2026"
    )
    friday = spec(
        repeat="interval",
        interval_minutes=15,
        window_start="18:00",
        window_end="02:00",
        days=[4],
        end_date="2026-12-25",
    )
    assert describe_repeat(friday) == (
        "Every 15 minutes from 18:00 until 02:00 on Friday, ending 25 December 2026"
    )
    assert describe_repeat(spec(repeat="interval", interval_minutes=5, days=[5, 6])) == (
        "Every 5 minutes on Saturday and Sunday"
    )
    assert describe_range(spec()) == ""


# -- editing and storage ------------------------------------------------------


def test_an_event_with_no_run_left_is_refused_or_kept_disabled(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "schedule.json")
    rig.schedule.start()
    with pytest.raises(EventError) as caught:
        rig.add(end_date="2026-10-10")
    assert (caught.value.field, str(caught.value)) == (
        "end_date",
        "The event has no run left before its end date",
    )
    with pytest.raises(EventError) as caught:
        rig.add(repeat="weekdays", start_date="2026-10-17", end_date="2026-10-18")
    assert caught.value.field == "end_date"
    ended = rig.add(end_date="2026-10-10", enabled=False)
    assert rig.schedule.next_time(ended) is None
    with pytest.raises(EventError, match="end date has passed") as caught:
        asyncio.run(rig.schedule.set_enabled(ended.id, True))
    assert caught.value.field == "enabled"
    # A range still to come with none of the event's days in it has not
    # passed; enabling says there is no run in it.
    weekend = rig.add(
        repeat="weekdays", start_date="2026-10-17", end_date="2026-10-18", enabled=False
    )
    with pytest.raises(EventError) as caught:
        asyncio.run(rig.schedule.set_enabled(weekend.id, True))
    assert (caught.value.field, str(caught.value)) == (
        "enabled",
        "This event has no run left before its end date; edit its dates first",
    )


def test_files_without_dates_load_unchanged_and_stay_readable(tmp_path: Path) -> None:
    path = tmp_path / "schedule.json"
    old = {
        "id": "a" * 32,
        "name": "Old",
        "action": "noop",
        "params": {},
        "repeat": "daily",
        "date": None,
        "time": "07:00:00",
        "days": [],
        "interval_minutes": None,
        "window_start": None,
        "window_end": None,
        "timing": "soft",
        "enabled": True,
    }
    path.write_text(
        json.dumps({"schema_version": 1, "events": [old], "runs": []}), encoding="utf-8"
    )
    rig = Rig(path)
    rig.schedule.start()
    assert list(rig.schedule.events) == ["a" * 32]
    assert not list(tmp_path.glob("schedule.json.bad-*"))
    rig.add(name="Season", start_date="2026-11-27", end_date="2026-12-26")
    saved = json.loads(path.read_text(encoding="utf-8"))["events"]
    assert saved[0] == old
    assert (saved[1]["start_date"], saved[1]["end_date"]) == ("2026-11-27", "2026-12-26")
    again = Rig(path)
    again.schedule.start()
    assert again.schedule.events[saved[1]["id"]].end_date.isoformat() == "2026-12-26"
