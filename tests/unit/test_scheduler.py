"""Repeat rules, DST handling, storage and running of scheduled events."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from autodj.config import ServerConfig
from autodj.scheduler import (
    MAX_EVENTS,
    Action,
    ActionField,
    EventError,
    EventSpec,
    ScheduledEvent,
    Scheduler,
    describe_repeat,
    field_error,
    next_run,
    system_timezone,
)

DENVER = ZoneInfo("America/Denver")


def spec(**changes: Any) -> EventSpec:
    values: dict[str, Any] = {
        "name": "Morning",
        "action": "noop",
        "repeat": "daily",
        "time": "07:00",
    }
    values.update(changes)
    return EventSpec.model_validate(values)


def stored(event_id: str, **changes: Any) -> ScheduledEvent:
    return ScheduledEvent.model_validate(
        {**spec(**changes).model_dump(mode="json"), "id": event_id}
    )


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def runs(item: EventSpec, after: str, count: int, zone: ZoneInfo = DENVER) -> list[str]:
    found = []
    cursor = utc(after)
    for _ in range(count):
        when = next_run(item, cursor, zone)
        assert when is not None
        found.append(when.strftime("%Y-%m-%d %H:%M"))
        cursor = when
    return found


# -- next_run ---------------------------------------------------------------


def test_daily_runs_at_the_local_time_and_strictly_after() -> None:
    item = spec()
    assert runs(item, "2026-01-02 13:00", 2) == ["2026-01-02 14:00", "2026-01-03 14:00"]
    assert next_run(item, utc("2026-01-02 14:00"), DENVER) == utc("2026-01-03 14:00")


def test_dst_gap_runs_at_the_jump() -> None:
    # 02:30 does not exist in Denver on 8 March 2026: clocks go 02:00 MST -> 03:00 MDT.
    assert runs(spec(time="02:30"), "2026-03-07 12:00", 3) == [
        "2026-03-08 09:00",
        "2026-03-09 08:30",
        "2026-03-10 08:30",
    ]


def test_dst_gap_of_a_whole_day_runs_at_the_jump() -> None:
    # Samoa skipped 30 December 2011.
    item = spec(repeat="once", date="2011-12-30", time="12:00")
    assert next_run(item, utc("2011-12-29 00:00"), ZoneInfo("Pacific/Apia")) == utc(
        "2011-12-30 10:00"
    )


def test_ambiguous_time_runs_once_on_the_first_pass() -> None:
    # 01:30 happens twice in Denver on 1 November 2026.
    assert runs(spec(time="01:30"), "2026-11-01 06:00", 2) == [
        "2026-11-01 07:30",
        "2026-11-02 08:30",
    ]


def test_weekdays_and_chosen_days() -> None:
    friday_noon = "2026-10-09 18:00"
    assert runs(spec(repeat="weekdays"), friday_noon, 2) == [
        "2026-10-12 13:00",
        "2026-10-13 13:00",
    ]
    assert runs(spec(repeat="days", days=[6, 2, 6]), friday_noon, 3) == [
        "2026-10-11 13:00",
        "2026-10-14 13:00",
        "2026-10-18 13:00",
    ]


def test_once_runs_then_never_again() -> None:
    item = spec(repeat="once", date="2026-10-11", time="09:00")
    assert next_run(item, utc("2026-10-11 14:59"), DENVER) == utc("2026-10-11 15:00")
    assert next_run(item, utc("2026-10-11 15:00"), DENVER) is None
    with pytest.raises(ValueError, match="time zone"):
        next_run(item, datetime(2026, 10, 11), DENVER)


def test_interval_without_window_restarts_at_midnight() -> None:
    # 23:20 local is a slot; 23:45 is the last of the day, then midnight.
    assert runs(spec(repeat="interval", interval_minutes=25), "2026-01-02 06:20", 3) == [
        "2026-01-02 06:45",
        "2026-01-02 07:00",
        "2026-01-02 07:25",
    ]


def test_interval_window_is_inclusive() -> None:
    item = spec(repeat="interval", interval_minutes=30, window_start="09:00", window_end="10:00")
    assert runs(item, "2026-10-09 18:00", 4) == [
        "2026-10-10 15:00",
        "2026-10-10 15:30",
        "2026-10-10 16:00",
        "2026-10-11 15:00",
    ]


def test_interval_window_crossing_midnight() -> None:
    item = spec(repeat="interval", interval_minutes=60, window_start="22:00", window_end="02:00")
    zone = ZoneInfo("UTC")
    assert runs(item, "2026-10-10 12:00", 6, zone) == [
        "2026-10-10 22:00",
        "2026-10-10 23:00",
        "2026-10-11 00:00",
        "2026-10-11 01:00",
        "2026-10-11 02:00",
        "2026-10-11 22:00",
    ]
    # Starting after midnight continues the window that began the day before.
    assert runs(item, "2026-10-11 00:30", 2, zone) == ["2026-10-11 01:00", "2026-10-11 02:00"]


def test_interval_across_dst_changes() -> None:
    item = spec(repeat="interval", interval_minutes=30, window_start="01:00", window_end="03:30")
    # Spring: 02:00, 02:30 and 03:00 all fall on the jump and run once.
    assert runs(item, "2026-03-08 07:30", 5) == [
        "2026-03-08 08:00",
        "2026-03-08 08:30",
        "2026-03-08 09:00",
        "2026-03-08 09:30",
        "2026-03-09 07:00",
    ]
    # Autumn: the repeated hour runs once.
    assert runs(item, "2026-11-01 06:30", 4) == [
        "2026-11-01 07:00",
        "2026-11-01 07:30",
        "2026-11-01 09:00",
        "2026-11-01 09:30",
    ]


# -- validation ---------------------------------------------------------------


def field_of(**changes: Any) -> tuple[str, str]:
    with pytest.raises(ValidationError) as caught:
        spec(**changes)
    return field_error(caught.value)


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"name": "   "}, "name"),
        ({"name": "x" * 101}, "name"),
        ({"repeat": "once"}, "date"),
        ({"time": None}, "time"),
        ({"time": "07:00:30"}, "time"),
        ({"time": "07:00+01:00"}, "time"),
        ({"time": "noon"}, "time"),
        ({"repeat": "days", "days": []}, "days"),
        ({"repeat": "days", "days": [7]}, "days"),
        ({"repeat": "days", "days": [True]}, "days"),
        ({"repeat": "interval", "interval_minutes": 0}, "interval_minutes"),
        ({"repeat": "interval", "interval_minutes": 1441}, "interval_minutes"),
        ({"repeat": "interval", "interval_minutes": 5, "window_start": "09:00"}, "window_end"),
        ({"repeat": "interval", "interval_minutes": 5, "window_end": "09:00"}, "window_start"),
        (
            {
                "repeat": "interval",
                "interval_minutes": 5,
                "window_start": "09:00",
                "window_end": "09:00",
            },
            "window_end",
        ),
        ({"timing": "now"}, "timing"),
        ({"surprise": 1}, "surprise"),
        ({"params": {"name": "x" * 201}}, "params.name"),
    ],
)
def test_invalid_events_name_the_field(changes: dict[str, Any], field: str) -> None:
    assert field_of(**changes)[0] == field


def test_validation_messages_are_words() -> None:
    assert field_of(repeat="days", days=[]) == ("days", "Choose at least one day")
    assert field_of(name="") == ("name", "Enter a name of up to 100 characters")
    assert field_of(surprise=1) == ("surprise", "Unknown field surprise")


def test_spec_keeps_only_the_fields_its_rule_uses() -> None:
    item = spec(date="2026-10-11", days=[1], interval_minutes=5, window_start="01:00")
    assert (item.date, item.days, item.interval_minutes, item.window_start) == (
        None,
        [],
        None,
        None,
    )
    assert spec(repeat="interval", interval_minutes=5).time is None
    assert spec(name="  Morning  ").name == "Morning"


def test_describe_repeat() -> None:
    assert describe_repeat(spec(repeat="once", date="2026-10-11")) == (
        "Once, Sunday 11 October 2026 at 07:00"
    )
    assert describe_repeat(spec()) == "Every day at 07:00"
    assert describe_repeat(spec(repeat="weekdays")) == "Weekdays at 07:00"
    assert describe_repeat(spec(repeat="days", days=[0])) == "Monday at 07:00"
    assert describe_repeat(spec(repeat="days", days=[4, 0, 2])) == (
        "Monday, Wednesday and Friday at 07:00"
    )
    assert describe_repeat(spec(repeat="interval", interval_minutes=15)) == "Every 15 minutes"
    assert describe_repeat(
        spec(repeat="interval", interval_minutes=15, window_start="22:00", window_end="02:00")
    ) == ("Every 15 minutes from 22:00 until 02:00")


def test_action_check_describe_and_metadata() -> None:
    async def nothing(_params: dict[str, str]) -> None:
        pass

    action = Action(
        "profile",
        "Switch profile",
        nothing,
        (
            ActionField("name", "Profile", names=lambda: ["Morning"]),
            ActionField("how", "How", choices=(("fast", "Quickly"),)),
        ),
    )
    assert action.check({"name": " Morning ", "how": "fast"}) == {"name": "Morning", "how": "fast"}
    assert action.describe({"name": "Morning", "how": "fast"}) == "Switch profile: Morning, Quickly"
    with pytest.raises(EventError) as caught:
        action.check({"name": "Morning", "how": "slow"})
    assert caught.value.field == "params.how"
    with pytest.raises(EventError, match="Choose profile"):
        action.check({"how": "fast"})
    with pytest.raises(EventError, match="no setting"):
        action.check({"name": "Morning", "how": "fast", "extra": "1"})
    # Only a live check needs the profile to exist now.
    assert action.check({"name": "Gone", "how": "fast"})["name"] == "Gone"
    with pytest.raises(EventError, match="Profile Gone was not found"):
        action.check({"name": "Gone", "how": "fast"}, live=True)
    fields = action.metadata()["fields"]
    assert fields[0]["options"] == [{"value": "Morning", "label": "Morning"}]
    assert fields[1]["options"] == [{"value": "fast", "label": "Quickly"}]
    assert ActionField("note", "Note").metadata() == {
        "name": "note",
        "label": "Note",
        "type": "text",
    }


def test_the_system_zone_is_asked_for_once_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    asked = []

    def unknown() -> str:
        asked.append(1)
        return "Mars/Base"

    system_timezone.cache_clear()
    monkeypatch.setitem(sys.modules, "tzlocal", SimpleNamespace(get_localzone_name=unknown))
    try:
        with caplog.at_level(logging.WARNING, logger="autodj.scheduler"):
            assert [system_timezone() for _ in range(5)] == ["UTC"] * 5
    finally:
        system_timezone.cache_clear()
    assert len(asked) == 1
    assert caplog.text.count("Could not identify the system time zone") == 1


def test_station_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    def zone_named(name: str) -> SimpleNamespace:
        return SimpleNamespace(get_localzone_name=lambda: name)

    system_timezone.cache_clear()
    monkeypatch.setitem(sys.modules, "tzlocal", zone_named("America/Denver"))
    assert system_timezone() == "America/Denver"
    system_timezone.cache_clear()
    monkeypatch.setitem(sys.modules, "tzlocal", zone_named("Mars/Base"))
    assert system_timezone() == "UTC"
    system_timezone.cache_clear()
    assert ServerConfig(station_timezone="America/Denver").station_timezone == "America/Denver"
    for bad in ("Mars/Base", "", "../etc/passwd", 5):
        with pytest.raises(ValueError, match="IANA time zone"):
            ServerConfig(station_timezone=bad)  # type: ignore[arg-type]


# -- the scheduler --------------------------------------------------------------


class Rig:
    """A scheduler with a settable clock that records what it does."""

    def __init__(
        self,
        path: Path,
        *,
        playing: bool = False,
        now: str = "2026-10-11 12:00",
        timezone: str = "UTC",
    ) -> None:
        self.now = utc(now)
        self.calls: list[str] = []
        self.messages: list[dict[str, Any]] = []
        self.playing = playing
        self.fail: Exception | None = None
        self.schedule = Scheduler(
            path,
            timezone,
            self.publish,
            interrupt=lambda: self.calls.append("skip"),
            song_playing=lambda: self.playing,
            clock=lambda: self.now,
        )
        self.schedule.register(Action("noop", "Do nothing", self.handle))
        self.schedule.register(Action("liner", "Air a liner", self.prepare, interrupt="before"))

    async def publish(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    async def handle(self, _params: dict[str, str]) -> None:
        self.calls.append("action")
        if self.fail is not None:
            raise self.fail

    async def prepare(self, _params: dict[str, str]) -> Callable[[], None]:
        self.calls.append("prepare")
        if self.fail is not None:
            raise self.fail
        return lambda: self.calls.append("action")

    def add(self, **changes: Any) -> ScheduledEvent:
        return asyncio.run(self.schedule.add(spec(**changes)))

    def tick_at(self, when: str) -> None:
        self.now = utc(when)
        asyncio.run(self.schedule.tick())


def write_schedule(path: Path, events: list[ScheduledEvent]) -> None:
    rows = [event.model_dump(mode="json") for event in events]
    path.write_text(json.dumps({"schema_version": 1, "events": rows, "runs": []}), encoding="utf-8")


def test_events_and_runs_survive_a_restart(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "schedule.json")
    rig.schedule.start()
    item = rig.add(time="12:01")
    rig.tick_at("2026-10-11 12:01")
    assert rig.calls == ["action"]
    assert (
        json.loads((tmp_path / "schedule.json").read_text(encoding="utf-8"))["schema_version"] == 1
    )
    again = Rig(tmp_path / "schedule.json")
    again.schedule.start()
    assert list(again.schedule.events) == [item.id]
    assert [(run.result, run.summary) for run in again.schedule.runs] == [("ok", "Do nothing")]


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        '{"schema_version": 2, "events": [], "runs": []}',
        '{"schema_version": 1, "events": [], "runs": [], "extra": 1}',
        '["a list"]',
    ],
)
def test_a_bad_file_is_kept_and_does_not_stop_startup(tmp_path: Path, content: str) -> None:
    path = tmp_path / "schedule.json"
    path.write_text(content, encoding="utf-8")
    rig = Rig(path)
    rig.schedule.start()
    assert rig.schedule.events == {}
    [copy] = tmp_path.glob("schedule.json.bad-*")
    assert copy.read_text(encoding="utf-8") == content


def test_duplicate_ids_make_the_file_bad(tmp_path: Path) -> None:
    path = tmp_path / "schedule.json"
    write_schedule(path, [stored("a" * 32), stored("a" * 32)])
    rig = Rig(path)
    rig.schedule.start()
    assert rig.schedule.events == {}
    assert len(list(tmp_path.glob("schedule.json.bad-*"))) == 1


def test_an_event_for_an_unknown_action_is_kept_as_it_was_but_never_runs(tmp_path: Path) -> None:
    """Another version's action (#106: an older AutoDJ and Play a show) survives a save."""
    path = tmp_path / "schedule.json"
    other = stored("b" * 32, action="cart", params={"page": "1"})
    write_schedule(path, [stored("a" * 32), other])
    rig = Rig(path)
    rig.schedule.start()
    assert list(rig.schedule.events) == ["a" * 32]
    assert list(tmp_path.glob("schedule.json.bad-*")) == []
    # A save after any change writes it back unchanged.
    rig.add(name="Evening", time="19:00")
    events = json.loads(path.read_text(encoding="utf-8"))["events"]
    saved = next(event for event in events if event["id"] == "b" * 32)
    assert saved == {
        key: value
        for key, value in other.model_dump(mode="json").items()
        if key not in ("start_date", "end_date") or value is not None
    }
    rig.tick_at("2026-10-12 07:00")
    assert "b" * 32 not in {run.event_id for run in rig.schedule.runs}
    assert "a" * 32 in {run.event_id for run in rig.schedule.runs}


def test_actions_register_before_start_only(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "schedule.json")
    with pytest.raises(ValueError, match="already registered"):
        rig.schedule.register(Action("noop", "Again", rig.handle))
    rig.schedule.start()
    with pytest.raises(RuntimeError, match="before the scheduler starts"):
        rig.schedule.register(Action("cart", "Play a cart", rig.handle))


def test_missed_once_event_is_logged_and_disabled_not_replayed(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "schedule.json", now="2026-10-11 08:00")
    rig.schedule.start()
    item = rig.add(repeat="once", date="2026-10-11", time="09:00")
    later = Rig(tmp_path / "schedule.json", now="2026-10-11 10:00")
    later.schedule.start()
    assert later.schedule.events[item.id].enabled is False
    assert [(run.result, run.message) for run in later.schedule.runs] == [
        ("missed", "Missed while the server was not running")
    ]
    later.tick_at("2026-10-11 10:01")
    assert later.calls == []
    saved = json.loads((tmp_path / "schedule.json").read_text(encoding="utf-8"))
    assert saved["events"][0]["enabled"] is False


def test_a_late_tick_records_a_miss_instead_of_running(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "schedule.json")
    rig.schedule.start()
    rig.add(time="12:01")
    rig.tick_at("2026-10-11 12:30")  # the computer slept
    assert rig.calls == []
    assert rig.schedule.runs[-1].result == "missed"


def test_a_clock_moving_back_does_not_repeat_events(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "schedule.json")
    rig.schedule.start()
    rig.add(time="12:01")
    for when in ("12:01", "11:59", "12:01", "12:02"):
        rig.tick_at(f"2026-10-11 {when}")
    assert rig.calls == ["action"]


def test_soft_event_waits_for_the_song_and_runs_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "schedule.json", playing=True)
    rig.schedule.start()
    item = rig.add(repeat="interval", interval_minutes=1)
    for minute in range(1, 4):
        rig.tick_at(f"2026-10-11 12:0{minute}")
    assert rig.calls == []
    assert rig.schedule.pending == [item.id]

    async def song_ends() -> None:
        rig.schedule.track_changed()
        await asyncio.gather(*rig.schedule._tasks)

    asyncio.run(song_ends())
    assert rig.calls == ["action"]
    assert rig.schedule.pending == []
    asyncio.run(song_ends())
    assert rig.calls == ["action"]


def test_soft_event_runs_when_nothing_is_playing(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "schedule.json", playing=True)
    rig.schedule.start()
    rig.add(time="12:01")
    rig.tick_at("2026-10-11 12:01")
    assert rig.calls == []
    rig.playing = False
    rig.tick_at("2026-10-11 12:02")
    assert rig.calls == ["action"]


def test_editing_disabling_or_deleting_cancels_a_waiting_soft_event(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "schedule.json", playing=True)
    rig.schedule.start()
    first, second, third = (rig.add(time="12:01", name=name) for name in ("A", "B", "C"))
    rig.tick_at("2026-10-11 12:01")
    assert rig.schedule.pending == [first.id, second.id, third.id]
    asyncio.run(rig.schedule.replace(first.id, spec(time="13:00")))
    asyncio.run(rig.schedule.set_enabled(second.id, False))
    asyncio.run(rig.schedule.remove(third.id))
    assert rig.schedule.pending == []


def test_hard_timing_fades_through_the_skip_path(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "schedule.json", playing=True)
    rig.schedule.start()
    rig.add(time="12:01", timing="hard")
    rig.tick_at("2026-10-11 12:01")
    assert rig.calls == ["action", "skip"]
    rig.calls.clear()
    liner = rig.add(action="liner", time="12:05", timing="hard")
    asyncio.run(rig.schedule.run(liner))
    assert rig.calls == ["prepare", "skip", "action"]


def test_a_failing_action_is_recorded_and_the_scheduler_continues(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "schedule.json")
    rig.schedule.start()
    rig.add(time="12:01", name="Broken")
    rig.add(time="12:01", name="Fine")
    rig.fail = RuntimeError("The selected liner is gone")
    rig.tick_at("2026-10-11 12:01")
    assert [(run.event, run.result, run.message) for run in rig.schedule.runs] == [
        ("Broken", "error", "The selected liner is gone"),
        ("Fine", "error", "The selected liner is gone"),
    ]
    rig.fail = None
    rig.tick_at("2026-10-12 12:01")
    assert rig.schedule.runs[-1].result == "ok"
    assert {message["type"] for message in rig.messages} == {"schedule_changed", "schedule_run"}


def test_a_failing_publish_or_save_does_not_stop_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path / "schedule.json")
    rig.schedule.start()
    item = rig.add()

    async def broken_publish(_message: dict[str, Any]) -> None:
        raise ConnectionError("socket gone")

    def broken_save() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(rig.schedule, "_publisher", broken_publish)
    monkeypatch.setattr(rig.schedule, "save", broken_save)
    assert asyncio.run(rig.schedule.run(item)).result == "ok"


def test_editing_rules(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "schedule.json")
    rig.schedule.start()
    with pytest.raises(EventError) as caught:
        rig.add(action="cart")
    assert caught.value.field == "action"
    with pytest.raises(EventError) as caught:
        rig.add(repeat="once", date="2026-10-11", time="11:00")
    assert caught.value.field == "time"
    with pytest.raises(EventError) as caught:
        rig.add(repeat="once", date="2026-10-10", time="13:00")
    assert caught.value.field == "date"
    # A disabled one-time event may lie in the past, but cannot be enabled there.
    past = rig.add(repeat="once", date="2026-10-10", time="13:00", enabled=False)
    with pytest.raises(EventError, match="in the past"):
        asyncio.run(rig.schedule.set_enabled(past.id, True))
    with pytest.raises(KeyError):
        asyncio.run(rig.schedule.replace("f" * 32, spec()))


def test_the_schedule_has_a_size_limit(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "schedule.json")
    rig.schedule.start()
    for number in range(MAX_EVENTS):
        rig.schedule.events[f"{number:032x}"] = stored(f"{number:032x}")
    with pytest.raises(EventError, match="at most"):
        rig.add()


def test_upcoming_merges_events_in_time_order(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "schedule.json")
    rig.schedule.start()
    rig.add(name="Hourly", repeat="interval", interval_minutes=60)
    rig.add(name="Lunch", time="12:30")
    rig.add(name="Off", time="12:10", enabled=False)
    rows = rig.schedule.upcoming(3)
    assert [(when.strftime("%H:%M"), event.name) for when, event in rows] == [
        ("12:30", "Lunch"),
        ("13:00", "Hourly"),
        ("14:00", "Hourly"),
    ]
    assert rows[0][0] - rig.now == timedelta(minutes=30)


def test_failed_audio_preparation_does_not_interrupt(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "schedule.json", playing=True)
    item = rig.add(action="liner", timing="hard")
    rig.fail = RuntimeError("The selected liner is gone")

    result = asyncio.run(rig.schedule.run(item))

    assert result.result == "error"
    assert result.message == "The selected liner is gone"
    assert rig.calls == ["prepare"]


def test_an_action_can_run_hard_events_itself_and_soft_ones_at_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "schedule.json", playing=True)

    async def queue(_params: dict[str, str]) -> None:
        rig.calls.append("queue")

    async def now(_params: dict[str, str]) -> None:
        rig.calls.append("now")

    rig.schedule.register(
        Action("show", "Play a show", queue, interrupt="none", hard=now, soft_waits=False)
    )
    rig.schedule.start()
    rig.add(action="show", time="12:01")
    rig.add(action="show", time="12:02", timing="hard", name="Hard")
    # Soft: at once, though a song is playing, and without a skip.
    rig.tick_at("2026-10-11 12:01")
    assert rig.calls == ["queue"]
    assert rig.schedule.pending == []
    # Hard: the action's own run, which does its own interrupting.
    rig.tick_at("2026-10-11 12:02")
    assert rig.calls == ["queue", "now"]
    assert [run.result for run in rig.schedule.runs] == ["ok", "ok"]
