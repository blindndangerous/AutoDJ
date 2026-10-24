"""Seasons for rotation categories, and days and hours for clockwheel steps (#140)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from autodj.categories import (
    Category,
    CategoryError,
    CategoryIndex,
    CategoryStore,
    Resolution,
    StepHours,
    daypart_refusal,
    describe_daypart,
    parse_spec,
    parse_step_hours,
)
from autodj.clockwheel_store import ClockwheelStore, validate_clockwheel, validate_step
from autodj.rotation_sim import SimulationRequest, simulate
from tests.unit._rotation_station import MUSIC, category_step, make_station, song


def track_step(entry: Any) -> dict[str, Any]:
    return {"type": "track", "source": "music", "path": entry.path.removeprefix(f"{MUSIC}/")}


def category(**changes: Any) -> Category:
    values: dict[str, Any] = {"id": "c1", "name": "Christmas", "source": "query", "query": "a:b"}
    values.update(changes)
    return Category.model_validate(values)


def wall(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _at(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()


# -- seasons for categories -------------------------------------------------------


def test_a_season_allows_its_first_and_last_day_and_nothing_outside() -> None:
    season = category(not_before="2026-12-01", not_after="2026-12-26")
    assert season.dayparted
    assert not season.allows(wall("2026-11-30 23:59"))
    assert season.allows(wall("2026-12-01 00:00"))
    assert season.allows(wall("2026-12-26 23:59"))
    assert not season.allows(wall("2026-12-27 00:00"))
    assert daypart_refusal(season, wall("2026-12-27 00:00")) == "dates"
    assert category(not_before="2026-12-01").allows(wall("2027-06-01 12:00"))
    assert not category(not_after="2026-12-26").allows(wall("2027-01-01 12:00"))


def test_a_window_across_midnight_belongs_to_the_season_day_it_starts() -> None:
    eve = category(
        hours_from="22:00", hours_until="02:00", not_before="2026-12-24", not_after="2026-12-24"
    )
    assert eve.allows(wall("2026-12-24 23:00"))
    assert eve.allows(wall("2026-12-25 01:30"))  # still the window of the 24th
    assert not eve.allows(wall("2026-12-25 22:30"))
    assert daypart_refusal(eve, wall("2026-12-25 22:30")) == "dates"
    assert daypart_refusal(eve, wall("2026-12-24 12:00")) == "hours"


def test_seasons_in_words() -> None:
    assert describe_daypart(category(not_before="2026-12-01", not_after="2026-12-26")) == (
        "Plays only between 1 December 2026 and 26 December 2026"
    )
    assert describe_daypart(category(not_before="2026-12-01")) == (
        "Plays only from 1 December 2026"
    )
    assert describe_daypart(category(not_after="2026-12-26")) == (
        "Plays only until 26 December 2026"
    )
    assert describe_daypart(
        category(days=[4], hours_from="22:00", hours_until="02:00", not_after="2026-12-26")
    ) == ("Plays only on Friday from 22:00 until 02:00, until 26 December 2026")
    assert describe_daypart(category(days=[5, 6], not_before="2026-12-01")) == (
        "Plays only at weekends, from 1 December 2026"
    )
    # All seven days and a season: the season is what holds the songs back.
    assert describe_daypart(category(days=list(range(7)), not_after="2026-12-26")) == (
        "Plays only until 26 December 2026"
    )


def test_a_backwards_or_unreadable_season_names_its_field() -> None:
    body = {"name": "Christmas", "source": "query", "query": "genre:Xmas"}
    with pytest.raises(CategoryError) as caught:
        parse_spec({**body, "not_before": "2026-12-26", "not_after": "2026-12-01"})
    assert (caught.value.field, caught.value.message) == (
        "not_after",
        "Do not play after must be on or after the Not before date",
    )
    with pytest.raises(CategoryError) as caught:
        parse_spec({**body, "not_before": "soon"})
    assert caught.value.field == "not_before"


def test_files_without_a_season_load_and_save_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "categories.json"
    old = {"name": "A", "source": "query", "query": "genre:Rock", "playlist": "", "id": "a1"}
    path.write_text(json.dumps({"schema_version": 1, "categories": [old]}), encoding="utf-8")
    store = CategoryStore(path)
    [loaded] = store.load()
    store.save([loaded, category(id="x1", not_before="2026-12-01", not_after="2026-12-26")])
    saved = json.loads(path.read_text(encoding="utf-8"))["categories"]
    assert saved[0] == old  # no new keys, so an older AutoDJ still reads it
    assert (saved[1]["not_before"], saved[1]["not_after"]) == ("2026-12-01", "2026-12-26")
    assert "days" not in saved[1] and "hours_from" not in saved[1]
    assert store.load()[1].not_after == date(2026, 12, 26)


def test_the_closed_songs_name_a_season_as_its_dates() -> None:
    index = CategoryIndex.__new__(CategoryIndex)
    index._closed = None
    season = category(id="xmas", not_before="2026-12-01", not_after="2026-12-26")
    index.resolutions = lambda: (  # type: ignore[method-assign]
        (season,),
        {"xmas": Resolution(frozenset({"a"}))},
    )
    assert index.closed_now(wall("2026-12-10 12:00")) == (frozenset(), {})
    assert index.closed_now(wall("2027-01-02 12:00")) == (
        frozenset({"a"}),
        {"xmas": "outside its dates: plays only between 1 December 2026 and 26 December 2026"},
    )


def test_the_rotation_tester_keeps_seasonal_songs_to_their_season(tmp_path: Path) -> None:
    christmas = [song(n, f"Artist {n}") for n in range(20)]
    regular = [song(100 + n, f"Artist {100 + n}") for n in range(40)]
    source = make_station(
        tmp_path,
        [*christmas, *regular],
        categories=[
            {
                "id": "xmas",
                "name": "Christmas",
                "members": christmas,
                "not_before": "2026-12-01",
                "not_after": "2026-12-26",
            },
            {"id": "regular", "name": "Regular", "members": regular},
        ],
        clockwheels=[{"name": "Main", "steps": [category_step("xmas"), category_step("regular")]}],
        active_clock="Main",
        playback={"pick_mode": "clock"},
    )
    october = simulate(source, SimulationRequest(hours=3, start=_at("2026-10-12 12:00"), seed=2))
    rows = {row["name"]: row for row in october["categories"]}
    assert rows["Christmas"]["plays"] == 0
    assert (
        "category Christmas: outside its dates: "
        "plays only between 1 December 2026 and 26 December 2026"
    ) in [item["reason"] for item in october["skipped_steps"]]
    december = simulate(source, SimulationRequest(hours=3, start=_at("2026-12-12 12:00"), seed=2))
    rows = {row["name"]: row for row in december["categories"]}
    assert rows["Christmas"]["plays"] > 0


# -- days and hours for clockwheel steps -------------------------------------------


def test_step_hours_are_checked_kept_and_written_only_when_set() -> None:
    step = {"type": "settings", "preset": "Night", "days": [5, 4, 4], "hours_from": "22:00"}
    with pytest.raises(ValueError, match="Enter both Allowed from and Allowed until"):
        validate_step(step)
    step["hours_until"] = "02:00"
    assert validate_step(step) == {
        "type": "settings",
        "preset": "Night",
        "days": [4, 5],
        "hours_from": "22:00",
        "hours_until": "02:00",
    }
    # Empty days and hours are no limit, and are not written.
    assert validate_step({"type": "settings", "preset": "Night", "days": []}) == {
        "type": "settings",
        "preset": "Night",
    }
    with pytest.raises(ValueError, match="Days must be numbered 0"):
        validate_step({"type": "settings", "preset": "Night", "days": [9]})
    with pytest.raises(ValueError, match="Allowed until must differ"):
        validate_step(
            {"type": "settings", "preset": "N", "hours_from": "06:00", "hours_until": "06:00"}
        )
    # Other unknown keys are still refused by the step type.
    with pytest.raises(ValueError, match="unknown fields: colour"):
        validate_step({"type": "settings", "preset": "Night", "colour": "red"})


def test_clockwheel_files_without_step_hours_load_unchanged(tmp_path: Path) -> None:
    store = ClockwheelStore(tmp_path)
    plain = {
        "version": 1,
        "name": "Main",
        "resync_minutes": [0],
        "steps": [category_step("a"), {"type": "settings", "preset": "Night"}],
    }
    store.save(plain)
    written = json.loads((tmp_path / "Main.json").read_text(encoding="utf-8"))
    assert written["steps"] == plain["steps"]
    assert store.load("Main") == validate_clockwheel(plain)


def test_a_step_hours_rule_is_the_category_rule() -> None:
    hours = parse_step_hours({"days": [4], "hours_from": "22:00", "hours_until": "02:00"})
    assert isinstance(hours, StepHours)
    assert hours.refusal(wall("2026-10-16 23:00")) is None  # Friday night
    assert hours.refusal(wall("2026-10-17 01:00")) is None  # still Friday's window
    assert hours.refusal(wall("2026-10-17 23:00")) == (  # Saturday's own night
        "outside its days: plays only on Friday from 22:00 until 02:00"
    )
    assert hours.refusal(wall("2026-10-16 12:00")) == (  # Friday noon
        "outside its hours: plays only on Friday from 22:00 until 02:00"
    )
    assert parse_step_hours({"type": "music"}) is None
    assert parse_step_hours({"days": list(range(7))}) is not None  # every day: allows all
    assert parse_step_hours({"days": list(range(7))}).refusal(wall("2026-10-17 03:00")) is None


def _step_station(tmp_path: Path, night_step: dict[str, Any]):
    night = [song(n, f"Artist {n}") for n in range(20)]
    day = [song(100 + n, f"Artist {100 + n}") for n in range(40)]
    return make_station(
        tmp_path,
        [*night, *day],
        categories=[
            {"id": "night", "name": "Night", "members": night},
            {"id": "day", "name": "Day", "members": day},
        ],
        clockwheels=[{"name": "Main", "steps": [night_step, category_step("day")]}],
        active_clock="Main",
        playback={"pick_mode": "clock"},
    ), {entry.path for entry in night}


def test_a_step_outside_its_hours_is_skipped_and_says_why(tmp_path: Path, caplog) -> None:
    step = {**category_step("night"), "hours_from": "22:00", "hours_until": "02:00"}
    source, night = _step_station(tmp_path, step)
    player = source.player
    # This assertion exercises the live warning path; station fixtures are
    # normally dry-run players so simulations stay quiet.
    player._dry_run = False
    noon = _at("2026-10-12 12:00")
    player._now = lambda: noon  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="autodj.player"):
        for _ in range(4):
            picked = player._clock_pick(player._pick_context(), 0)
            assert picked.entry.path not in night
    assert player._clock_status == (
        "Step 1: outside its hours: plays only every day from 22:00 until 02:00"
    )
    assert caplog.text.count("outside its hours") == 1  # logged once
    # Checked at the song's expected start: at 21:58, a song starting at 22:02 may play.
    almost = _at("2026-10-12 21:58")
    player._now = lambda: almost  # type: ignore[method-assign]
    picked = player._clock_pick(player._pick_context(at=almost + 240), 0)
    assert picked.entry.path in night
    picked = player._clock_pick(player._pick_context(), 0)
    assert picked.entry.path not in night


def test_a_category_clock_whose_every_step_is_closed_by_its_hours_acts_as_one_with_none(
    tmp_path: Path,
) -> None:
    # Step hours are not category hours: the clock takes the path it takes
    # when no step can play.  For category steps that already plays a song
    # from the library; nothing says it is the hours of the songs.
    step = {**category_step("night"), "hours_from": "22:00", "hours_until": "02:00"}
    source, _night = _step_station(tmp_path, step)
    player = source.player
    player._clock_step_hours = (
        parse_step_hours({"hours_from": "22:00", "hours_until": "02:00"}),
    ) * 2
    noon = _at("2026-10-12 12:00")
    player._now = lambda: noon  # type: ignore[method-assign]
    current = player.playable_entries()[-1]
    picked, mode, *_rest = player._choose_station_next(current, player._pick_context())
    assert mode == "fallback"
    assert picked is not None
    assert not player._clock_closed_only
    assert "every song this clockwheel can play" not in player._clock_status


def test_a_folder_clock_whose_every_step_is_closed_by_its_hours_stops_as_before(
    tmp_path: Path,
) -> None:
    # No new fallback to the whole library for a clock of folder or track
    # steps: closed by their hours, it does what an empty folder clock does.
    music = [song(n, f"Artist {n}") for n in range(10)]
    source = make_station(
        tmp_path,
        music,
        clockwheels=[{"name": "Main", "steps": [{**track_step(music[0]), "days": [4]}]}],
        active_clock="Main",
        playback={"pick_mode": "clock"},
    )
    player = source.player
    monday = _at("2026-10-12 12:00")
    player._now = lambda: monday  # type: ignore[method-assign]
    current = music[1]
    with pytest.raises(ValueError, match="Step 1: outside its days: plays only on Friday"):
        player._choose_station_next(current, player._pick_context())
    assert not player._clock_closed_only


def test_the_rotation_tester_skips_a_step_outside_its_hours(tmp_path: Path) -> None:
    step = {**category_step("night"), "days": [4, 5], "hours_from": "22:00", "hours_until": "02:00"}
    source, _night = _step_station(tmp_path, step)
    monday = simulate(source, SimulationRequest(hours=3, start=_at("2026-10-12 12:00"), seed=4))
    rows = {row["name"]: row for row in monday["categories"]}
    assert rows["Night"]["plays"] == 0
    assert ("outside its hours: plays only on Friday and Saturday from 22:00 until 02:00") in [
        item["reason"] for item in monday["skipped_steps"]
    ]
    friday = simulate(source, SimulationRequest(hours=3, start=_at("2026-10-16 22:00"), seed=4))
    rows = {row["name"]: row for row in friday["categories"]}
    assert rows["Night"]["plays"] > 0


def test_the_warnings_name_what_holds_the_songs_back(caplog) -> None:
    from types import SimpleNamespace

    from autodj.player import _closure_words
    from tests.unit.test_dayparting import _dayparted
    from tests.unit.test_dmca import _player

    def context(*reasons: str) -> Any:
        return SimpleNamespace(closed_categories={str(n): r for n, r in enumerate(reasons)})

    assert _closure_words(context()) == "hours"
    assert _closure_words(context("outside its dates: plays only until 26 December 2026")) == (
        "dates"
    )
    assert _closure_words(
        context("outside its dates: x", "outside its hours: y", "outside its days: z")
    ) == ("days, hours or dates")
    everything = {entry.title for entry in _player("random").playable_entries()}
    player = _dayparted(_player("random", dmca=False), everything)
    paths = player._library.categories.closed_now(None)[0]
    player._library.categories.closed_now = lambda _wall: (
        paths,
        {"xmas": "outside its dates: plays only until 26 December 2026"},
    )
    current = player.playable_entries()[0]
    with caplog.at_level(logging.WARNING, logger="autodj.player"):
        player._choose_next(current, player._pick_context())
    assert "every song is outside its category's dates" in caplog.text


# -- seasons that repeat every week or every year (#470) --------------------------


def test_a_weekly_season_keeps_its_weekdays_from_its_first_date() -> None:
    """ADR-0009-09: Friday 6th to Sunday 8th is every Friday to Sunday from the 6th."""
    weekend = category(not_before="2026-11-06", not_after="2026-11-08", dates_repeat="weekly")
    assert not weekend.allows(wall("2026-10-31 12:00"))  # a Saturday before the first date
    assert weekend.allows(wall("2026-11-06 00:00"))
    assert weekend.allows(wall("2026-11-15 23:59"))
    assert not weekend.allows(wall("2026-11-16 00:00"))
    assert weekend.allows(wall("2027-06-04 12:00"))  # a Friday, with no end
    assert daypart_refusal(weekend, wall("2027-06-03 12:00")) == "dates"
    # Saturday to Monday runs past the week's end.
    wrap = category(not_before="2026-11-07", not_after="2026-11-09", dates_repeat="weekly")
    assert [wrap.allows(wall(f"2026-11-{day:02d} 12:00")) for day in range(13, 18)] == [
        False, True, True, True, False,
    ]  # fmt: skip
    # With only a first date, it is that one weekday.
    fridays = category(not_before="2026-11-06", dates_repeat="weekly")
    assert [fridays.allows(wall(f"2026-11-{day:02d} 12:00")) for day in range(12, 15)] == [
        False, True, False,
    ]  # fmt: skip
    # A window across midnight belongs to the day it starts.
    late = category(
        hours_from="22:00", hours_until="02:00", not_before="2026-11-06", dates_repeat="weekly"
    )
    assert late.allows(wall("2026-11-14 01:30"))  # Friday night's window
    assert not late.allows(wall("2026-11-14 22:30"))


def test_a_yearly_season_runs_past_new_year_and_keeps_29_february() -> None:
    """ADR-0009-09: month and day only; 29 February is 28 February in a common year."""
    holidays = category(not_before="2026-12-20", not_after="2026-01-02", dates_repeat="yearly")
    assert not holidays.allows(wall("2029-12-19 23:59"))
    assert holidays.allows(wall("2029-12-20 00:00"))
    assert holidays.allows(wall("2030-01-02 23:59"))
    assert daypart_refusal(holidays, wall("2030-01-03 00:00")) == "dates"
    leap = category(not_before="2028-02-29", dates_repeat="yearly")
    assert leap.allows(wall("2027-02-28 12:00"))
    assert not leap.allows(wall("2028-02-28 12:00"))
    assert leap.allows(wall("2028-02-29 12:00"))


def test_repeating_seasons_in_words() -> None:
    """ADR-0009-09: the list says how the dates repeat."""
    assert (
        describe_daypart(
            category(not_before="2026-11-06", not_after="2026-11-08", dates_repeat="weekly")
        )
        == "Plays only every week from Friday to Sunday, starting 6 November 2026"
    )
    assert describe_daypart(category(not_before="2026-11-06", dates_repeat="weekly")) == (
        "Plays only on Fridays, starting 6 November 2026"
    )
    assert (
        describe_daypart(
            category(not_before="2026-12-20", not_after="2027-01-02", dates_repeat="yearly")
        )
        == "Plays only every year between 20 December and 2 January"
    )
    assert (
        describe_daypart(
            category(
                hours_from="18:00",
                hours_until="23:00",
                not_before="2026-10-31",
                dates_repeat="yearly",
            )
        )
        == "Plays only every day from 18:00 until 23:00, every year on 31 October"
    )


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        (
            {"not_after": "2026-11-08", "dates_repeat": "weekly"},
            "not_before",
            "Enter a Not before date to repeat the dates",
        ),
        (
            {"dates_repeat": "yearly"},
            "not_before",
            "Enter a Not before date to repeat the dates",
        ),
        (
            {"not_before": "2026-11-06", "not_after": "2026-11-13", "dates_repeat": "weekly"},
            "not_after",
            "For Every week, Do not play after must be within 6 days of the Not before date",
        ),
        (
            {"not_before": "2026-11-08", "not_after": "2026-11-06", "dates_repeat": "weekly"},
            "not_after",
            "Do not play after must be on or after the Not before date",
        ),
        (
            {"not_before": "2026-11-06", "dates_repeat": "monthly"},
            "dates_repeat",
            "Repeat dates must be Don't repeat, Every week or Every year",
        ),
    ],
)
def test_a_repeating_season_that_cannot_be_saved_names_its_field(
    changes: dict[str, Any], field: str, message: str
) -> None:
    """ADR-0009-09: the field errors for repeating dates."""
    body = {"name": "Christmas", "source": "query", "query": "genre:Xmas"}
    with pytest.raises(CategoryError) as caught:
        parse_spec({**body, **changes})
    assert (caught.value.field, caught.value.message) == (field, message)


def test_a_repeating_season_is_saved_only_when_set(tmp_path: Path) -> None:
    """ADR-0009-09: Don't repeat is never written; old files load with it."""
    path = tmp_path / "categories.json"
    old = {
        "name": "A", "source": "query", "query": "genre:Rock", "playlist": "", "id": "a1",
        "not_before": "2026-12-01", "not_after": "2026-12-26",
    }  # fmt: skip
    path.write_text(json.dumps({"schema_version": 1, "categories": [old]}), encoding="utf-8")
    store = CategoryStore(path)
    [loaded] = store.load()
    assert loaded.dates_repeat == "none"
    yearly = category(
        id="x1",
        name="Holidays",
        not_before="2026-12-20",
        not_after="2026-01-02",
        dates_repeat="yearly",
    )
    store.save([loaded, yearly])
    saved = json.loads(path.read_text(encoding="utf-8"))["categories"]
    assert saved[0] == old
    assert saved[1]["dates_repeat"] == "yearly"
    assert store.load()[1].dates_repeat == "yearly"
