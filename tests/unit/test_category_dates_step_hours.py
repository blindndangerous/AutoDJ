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
from tests.unit._rotation_station import category_step, make_station, song


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
    assert hours.refusal(wall("2026-10-17 23:00")) == (
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


def test_a_clock_whose_every_step_is_outside_its_hours_plays_an_allowed_song(
    tmp_path: Path,
) -> None:
    step = {**category_step("night"), "hours_from": "22:00", "hours_until": "02:00"}
    source, _night = _step_station(tmp_path, step)
    player = source.player
    # The other step keeps the same hours, so at noon no step can play.
    player._clock_step_hours = (
        parse_step_hours({"hours_from": "22:00", "hours_until": "02:00"}),
    ) * 2
    noon = _at("2026-10-12 12:00")
    player._now = lambda: noon  # type: ignore[method-assign]
    current = player.playable_entries()[-1]
    picked, mode, *_rest = player._choose_station_next(current, player._pick_context())
    assert mode == "fallback"
    assert picked is not None
    assert player._clock_status.endswith("every song this clockwheel can play is outside its hours")


def test_the_rotation_tester_skips_a_step_outside_its_hours(tmp_path: Path) -> None:
    step = {**category_step("night"), "days": [4, 5], "hours_from": "22:00", "hours_until": "02:00"}
    source, _night = _step_station(tmp_path, step)
    monday = simulate(source, SimulationRequest(hours=3, start=_at("2026-10-12 12:00"), seed=4))
    rows = {row["name"]: row for row in monday["categories"]}
    assert rows["Night"]["plays"] == 0
    assert (
        "Step 1: outside its hours: plays only on Friday and Saturday from 22:00 until 02:00"
    ) in [item["reason"] for item in monday["skipped_steps"]]
    friday = simulate(source, SimulationRequest(hours=3, start=_at("2026-10-16 22:00"), seed=4))
    rows = {row["name"]: row for row in friday["categories"]}
    assert rows["Night"]["plays"] > 0
