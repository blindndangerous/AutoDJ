"""The rotation rule tester: a dry run of the station's own picker (#125)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import pytest

from autodj.rotation_sim import (
    MAX_HOURS,
    MAX_PICKS,
    SimulationError,
    SimulationRequest,
    counted,
    number_words,
    report_text,
    simulate,
)
from autodj.scheduler import ScheduledEvent
from tests.unit._rotation_station import (
    category_step,
    liner,
    liner_step,
    make_station,
    song,
)

# Monday 12 October 2026, 12:00 UTC.
NOON = datetime(2026, 10, 12, 12, 0, tzinfo=UTC).timestamp()


def _at(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()


def _songs(first: int, count: int, artist: str | None = None, **changes: Any) -> list:
    return [
        song(first + n, artist or f"Artist {first + n}", album=f"Album {first + n}", **changes)
        for n in range(count)
    ]


def _rotation(tmp_path: Path, **changes: Any):
    """Power (6 songs), Current (40) and Gold (200) categories under one clockwheel."""
    power, current, gold = _songs(0, 6), _songs(100, 40), _songs(1000, 200)
    options: dict[str, Any] = {
        "liners": [liner(n) for n in range(4)],
        "categories": [
            {"id": "power", "name": "Power", "members": power},
            {"id": "current", "name": "Current", "members": current},
            {"id": "gold", "name": "Gold", "members": gold},
        ],
        "clockwheels": [
            {
                "name": "Main",
                "steps": [
                    liner_step(),
                    category_step("power"),
                    category_step("current"),
                    category_step("gold"),
                ],
            }
        ],
        "active_clock": "Main",
        "playback": {"pick_mode": "clock"},
    }
    options.update(changes)
    return make_station(tmp_path, [*power, *current, *gold], **options)


def _without_timing(report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key != "elapsed_seconds"}


# -- requests ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"hours": MAX_HOURS + 1}, "at most 720 hours"),
        ({"hours": 0}, "more than 0"),
        ({"picks": MAX_PICKS + 1}, "from 1 to 20,000"),
        ({"picks": 0}, "from 1 to 20,000"),
        ({"hours": 1, "picks": 5}, "not both"),
        ({"seed": -1}, "0 or more"),
    ],
)
def test_requests_out_of_range_are_refused_in_words(values: dict, message: str) -> None:
    with pytest.raises(SimulationError, match=message):
        SimulationRequest(**values)


# -- determinism and safety ----------------------------------------------------


def test_the_same_seed_gives_the_same_run(tmp_path: Path) -> None:
    source = _rotation(tmp_path)
    request = SimulationRequest(hours=12, start=NOON, seed=7)
    first = simulate(source, request)
    again = simulate(source, request)
    assert _without_timing(first) == _without_timing(again)
    assert first["seed"] == 7
    other = simulate(source, SimulationRequest(hours=12, start=NOON, seed=8))
    assert other["songs"]["most_played"] != first["songs"]["most_played"] or (
        other["categories"] != first["categories"]
    )


def test_a_run_without_a_seed_reports_the_one_it_drew(tmp_path: Path) -> None:
    source = _rotation(tmp_path)
    report = simulate(source, SimulationRequest(picks=20, start=NOON))
    repeat = simulate(source, SimulationRequest(picks=20, start=NOON, seed=report["seed"]))
    assert _without_timing(report) == _without_timing(repeat)


def test_nothing_on_air_changes(tmp_path: Path) -> None:
    source = _rotation(tmp_path, dmca=True)
    player = source.player
    before = (
        list(player._state.play_history),
        list(player._state.recently_played),
        dict(player._clock),
        player._cfg.playback.pick_mode,
        player._state.track_number,
    )
    settings = dict(source.library.catalog.settings)
    simulate(source, SimulationRequest(hours=6, start=NOON, seed=1))
    after = (
        list(player._state.play_history),
        list(player._state.recently_played),
        dict(player._clock),
        player._cfg.playback.pick_mode,
        player._state.track_number,
    )
    assert after == before
    assert source.library.catalog.settings == settings
    assert source.library.catalog.writes == 0
    assert source.ledger.writes == 0


def test_a_run_of_n_songs_picks_n_songs(tmp_path: Path) -> None:
    report = simulate(_rotation(tmp_path), SimulationRequest(picks=25, start=NOON, seed=3))
    assert report["picks"] == 25
    assert report["requested"] == {"picks": 25}
    assert report["liners"] > 0
    assert sum(report["modes"].values()) == 25


def test_a_cancelled_run_stops_with_what_it_has(tmp_path: Path) -> None:
    calls = []

    def cancelled() -> bool:
        calls.append(1)
        return len(calls) > 10

    report = simulate(
        _rotation(tmp_path), SimulationRequest(hours=24, start=NOON, seed=3), cancelled=cancelled
    )
    assert report["stopped_early"] is True
    assert 0 < report["picks"] <= 11


def test_progress_reaches_the_end(tmp_path: Path) -> None:
    seen = []
    simulate(
        _rotation(tmp_path),
        SimulationRequest(picks=250, start=NOON, seed=3),
        progress=seen.append,
    )
    assert [round(item.fraction, 1) for item in seen] == [0.4, 0.8, 1.0]
    assert seen[-1].picks == 250


# -- the counts ----------------------------------------------------------------


def test_plays_per_song_category_and_artist_add_up(tmp_path: Path) -> None:
    rules = {"track": {"tracks": 3}, "artist": {"tracks": 0}}
    source = _rotation(tmp_path, playback={"pick_mode": "clock", "repeat_rules": rules})
    report = simulate(source, SimulationRequest(hours=24, start=NOON, seed=5))
    picks = report["picks"]
    songs = report["songs"]
    assert songs["library"] == 246
    assert songs["played"] + songs["never_played"] == 246
    assert songs["average"] == round(picks / 246, 2)
    assert len(songs["never_played_sample"]) == min(10, songs["never_played"])
    rows = {row["name"]: row for row in report["categories"]}
    assert sum(row["plays"] for row in rows.values()) == picks
    # Each liner step leads into one Power, one Current and one Gold song; the clock
    # restarts on the hour, at Power, so Power gets a few more.
    assert 0 <= rows["Power"]["plays"] - rows["Gold"]["plays"] <= 10
    # Six Power songs share a third of the picks: each plays far more than any Gold song.
    assert rows["Power"]["min"] > rows["Gold"]["max"]
    assert report["artists"]["library"] == 246


def test_long_repeat_rules_starve_a_small_category(tmp_path: Path) -> None:
    """The issue's question: 720 minutes per song empties the six-song category fast."""
    rules = {"track": {"tracks": 50, "minutes": 720}, "artist": {"tracks": 8, "minutes": 240}}
    report = simulate(
        _rotation(tmp_path, playback={"pick_mode": "clock", "repeat_rules": rules}),
        SimulationRequest(hours=10, start=NOON, seed=5),
    )
    power = next(row for row in report["categories"] if row["name"] == "Power")
    assert power["skipped"] > 0
    reasons = {item["reason"]: item["count"] for item in report["skipped_steps"]}
    assert reasons["category Power: all matching entries blocked by rules"] == power["skipped"]
    # The clock moved on to Current and Gold instead, so no rule had to be relaxed.
    assert power["plays"] == 6
    assert report["relaxed"] == {}
    text = report_text(report)
    assert "category Power: all matching entries blocked by rules:" in text


def test_a_clock_with_nothing_else_relaxes_the_track_window(tmp_path: Path) -> None:
    # The rest of the library keeps the 50-song track window from shrinking to fit.
    trio, rest = _songs(0, 3), _songs(100, 100)
    source = make_station(
        tmp_path,
        [*trio, *rest],
        categories=[{"id": "trio", "name": "Trio", "members": trio}],
        clockwheels=[{"name": "Only", "steps": [category_step("trio")]}],
        active_clock="Only",
        playback={"pick_mode": "clock", "repeat_rules": {"artist": {"tracks": 0}}},
    )
    report = simulate(source, SimulationRequest(picks=30, start=NOON, seed=2))
    # Three songs, then every pick repeats one from the track window.
    assert report["relaxed"]["track repeat window"] == 27
    assert "back to back" not in report["relaxed"]
    assert report["dead_air"]["failed_picks"] == 0


def test_related_artists_count_as_their_own_rule(tmp_path: Path) -> None:
    pair = [song(1, "Alpha Group"), song(2, "Beta Duo")]
    source = make_station(
        tmp_path,
        pair,
        categories=[{"id": "pair", "name": "Pair", "members": pair}],
        clockwheels=[{"name": "Only", "steps": [category_step("pair")]}],
        active_clock="Only",
        playback={
            "pick_mode": "clock",
            "repeat_rules": {
                "track": {"tracks": 1},
                "artist": {"tracks": 1},
                "use_related_artists": True,
            },
        },
        related={"Alpha Group": ["Beta Duo"], "Beta Duo": ["Alpha Group"]},
    )
    report = simulate(source, SimulationRequest(picks=10, start=NOON, seed=2))
    assert report["relaxed"]["related artist"] == 9


def test_a_step_that_ignores_the_rules_is_not_counted_as_relaxing_them(tmp_path: Path) -> None:
    trio, rest = _songs(0, 3), _songs(100, 100)
    source = make_station(
        tmp_path,
        [*trio, *rest],
        categories=[{"id": "trio", "name": "Trio", "members": trio}],
        clockwheels=[{"name": "Only", "steps": [category_step("trio", enforce_rules=False)]}],
        active_clock="Only",
        playback={"pick_mode": "clock"},
    )
    report = simulate(source, SimulationRequest(picks=12, start=NOON, seed=2))
    assert "track repeat window" not in report["relaxed"]
    assert report["ignored_by_steps"]["track repeat window"] > 0


@pytest.mark.parametrize("mode", ["random", "least_recent", "similarity"])
def test_every_pick_mode_runs_and_repeats_with_its_seed(tmp_path: Path, mode: str) -> None:
    music = _songs(0, 60)
    source = make_station(
        tmp_path,
        music,
        playback={
            "pick_mode": mode,
            "transition_mode": "full_intro_outro",
            "pick_top_k": 5,
            "pick_temperature": 1.0,
        },
        similarity=mode == "similarity",
    )
    request = SimulationRequest(picks=40, start=NOON, seed=11)
    first = simulate(source, request)
    assert first["picks"] == 40
    assert first["song_picking"]["mode"] == mode
    assert _without_timing(simulate(source, request)) == _without_timing(first)


# -- dayparting ----------------------------------------------------------------


def _late_night(tmp_path: Path):
    night, day = _songs(0, 30), _songs(100, 60)
    return make_station(
        tmp_path,
        [*night, *day],
        categories=[
            {
                "id": "night",
                "name": "Late night",
                "members": night,
                "hours_from": "22:00",
                "hours_until": "02:00",
            },
            {"id": "day", "name": "Daytime", "members": day},
        ],
        clockwheels=[{"name": "Main", "steps": [category_step("night"), category_step("day")]}],
        active_clock="Main",
        playback={"pick_mode": "clock"},
    )


def test_dayparting_keeps_a_category_to_its_hours(tmp_path: Path) -> None:
    source = _late_night(tmp_path)
    afternoon = simulate(source, SimulationRequest(hours=4, start=_at("2026-10-12 13:00"), seed=1))
    rows = {row["name"]: row for row in afternoon["categories"]}
    assert rows["Late night"]["plays"] == 0
    assert rows["Late night"]["skipped"] > 0
    reasons = [item["reason"] for item in afternoon["skipped_steps"]]
    assert reasons == [
        "category Late night: outside its hours: plays only every day from 22:00 until 02:00"
    ]
    assert "dayparting" not in afternoon["relaxed"]
    night = simulate(source, SimulationRequest(hours=4, start=_at("2026-10-12 22:00"), seed=1))
    rows = {row["name"]: row for row in night["categories"]}
    assert rows["Late night"]["plays"] == rows["Daytime"]["plays"]
    assert night["skipped_steps"] == []


# -- DMCA ----------------------------------------------------------------------


def test_dmca_limits_are_kept_by_replacing_songs(tmp_path: Path) -> None:
    solo = [song(n, "Solo Act", album="One Album") for n in range(8)]
    others = _songs(100, 40)
    source = make_station(
        tmp_path,
        [*solo, *others],
        categories=[{"id": "solo", "name": "Solo", "members": solo}],
        clockwheels=[{"name": "Only", "steps": [category_step("solo", enforce_rules=False)]}],
        active_clock="Only",
        playback={"pick_mode": "clock", "repeat_rules": {"artist": {"tracks": 0}}},
        dmca=True,
    )
    report = simulate(source, SimulationRequest(hours=3, start=NOON, seed=4))
    assert report["dmca"]["enabled"] is True
    assert report["dmca"]["replaced"] > 0
    assert report["dmca"]["broken"] == 0
    assert "DMCA limits" in report_text(report)


def test_dmca_conflicts_are_reported_when_nothing_keeps_the_limits(tmp_path: Path) -> None:
    solo = [song(n, "Solo Act", album="One Album") for n in range(8)]
    source = make_station(
        tmp_path,
        solo,
        categories=[{"id": "solo", "name": "Solo", "members": solo}],
        clockwheels=[{"name": "Only", "steps": [category_step("solo")]}],
        active_clock="Only",
        playback={"pick_mode": "clock", "repeat_rules": {"artist": {"tracks": 0}}},
        dmca=True,
    )
    report = simulate(source, SimulationRequest(picks=12, start=NOON, seed=4))
    dmca = report["dmca"]
    assert dmca["broken"] > 0
    assert set(dmca["limits"]) <= {
        "album, in three hours",
        "album, in a row",
        "artist, in three hours",
        "artist, in a row",
    }
    assert dmca["limits"]["album, in a row"] > 0
    assert dmca["examples"][0]["limits"]


# -- the schedule --------------------------------------------------------------


def _event(**fields: Any) -> ScheduledEvent:
    return ScheduledEvent.model_validate({"id": uuid.uuid4().hex, "timing": "soft", **fields})


def test_a_date_ranged_schedule_switches_clockwheels_only_in_its_range(tmp_path: Path) -> None:
    morning, evening = _songs(0, 40), _songs(100, 40)
    events = [
        _event(
            name="Evening sound",
            action="pick_mode",
            params={"mode": "clock:Evening"},
            repeat="daily",
            time=time(18, 0),
            start_date=date(2026, 10, 13),
            end_date=date(2026, 10, 13),
        ),
        _event(
            name="Back to mornings",
            action="pick_mode",
            params={"mode": "clock:Morning"},
            repeat="daily",
            time=time(6, 0),
        ),
    ]
    source = make_station(
        tmp_path,
        [*morning, *evening],
        categories=[
            {"id": "m", "name": "Morning songs", "members": morning},
            {"id": "e", "name": "Evening songs", "members": evening},
        ],
        clockwheels=[
            {"name": "Morning", "steps": [category_step("m")]},
            {"name": "Evening", "steps": [category_step("e")]},
        ],
        active_clock="Morning",
        playback={"pick_mode": "clock"},
        events=events,
    )
    # Monday noon to Thursday noon: the evening clock runs once, Tuesday 18:00 to Wednesday 06:00.
    report = simulate(source, SimulationRequest(hours=72, start=NOON, seed=9))
    applied = [(item["time"], item["event"]) for item in report["schedule"]["examples"]]
    assert applied == [
        ("2026-10-13T06:00+00:00", "Back to mornings"),
        ("2026-10-13T18:00+00:00", "Evening sound"),
        ("2026-10-14T06:00+00:00", "Back to mornings"),
        ("2026-10-15T06:00+00:00", "Back to mornings"),
    ]
    rows = {row["name"]: row for row in report["categories"]}
    # Twelve hours of evening songs out of 72.
    share = rows["Evening songs"]["plays"] / report["picks"]
    assert 0.12 < share < 0.22
    assert report["song_picking"]["final_clockwheel"] == "Morning"
    assert "Back to mornings, song picking: clockwheel Morning." in report_text(report)


def test_a_profile_switch_is_named_as_not_simulated(tmp_path: Path) -> None:
    events = [
        _event(
            name="Weekend profile",
            action="profile",
            params={"name": "Weekend"},
            repeat="daily",
            time=time(13, 0),
        )
    ]
    report = simulate(
        _rotation(tmp_path, events=events), SimulationRequest(hours=4, start=NOON, seed=1)
    )
    assert report["schedule"]["refused"] == {
        "Weekend profile: profile switches are not simulated": 1
    }


# -- dead air ------------------------------------------------------------------


def test_dead_air_is_found_when_the_clockwheel_cannot_play(tmp_path: Path) -> None:
    music = _songs(0, 10)
    source = make_station(tmp_path, music, playback={"pick_mode": "clock"})
    report = simulate(source, SimulationRequest(hours=2, start=NOON, seed=1))
    dead = report["dead_air"]
    assert report["picks"] == 0
    assert dead["silent_minutes"] == 120
    assert dead["silences"][0]["reason"] == "No clockwheel is active"
    text = report_text(report)
    assert "Silence: about 120 minutes with nothing playable." in text


def test_dead_air_ends_when_the_schedule_brings_a_clockwheel_back(tmp_path: Path) -> None:
    music = _songs(0, 20)
    events = [
        _event(
            name="Start the clock",
            action="pick_mode",
            params={"mode": "clock:Main"},
            repeat="once",
            date=date(2026, 10, 12),
            time=time(13, 0),
        )
    ]
    source = make_station(
        tmp_path,
        music,
        categories=[{"id": "all", "name": "All", "members": music}],
        clockwheels=[{"name": "Main", "steps": [category_step("all")]}],
        playback={"pick_mode": "clock"},
        events=events,
    )
    report = simulate(source, SimulationRequest(hours=3, start=NOON, seed=1))
    assert report["dead_air"]["silent_minutes"] == 60
    assert report["picks"] > 20


# -- the report in words -------------------------------------------------------


def test_the_text_report_is_headings_and_short_lines(tmp_path: Path) -> None:
    report = simulate(_rotation(tmp_path), SimulationRequest(hours=6, start=NOON, seed=5))
    text = report_text(report)
    lines = text.splitlines()
    assert lines[0] == "Rotation test for station default"
    for heading in ("Summary", "Songs", "Artists", "Categories", "Rules relaxed", "Dead air"):
        index = lines.index(heading)
        assert lines[index - 1] == ""
    assert "|" not in text
    assert "Seed 5." in text
    assert all(len(line) < 400 for line in lines)


def test_small_numbers_are_words() -> None:
    assert [number_words(n) for n in (0, 1, 2, 10, 11, 1234)] == [
        "no",
        "one",
        "two",
        "ten",
        "11",
        "1,234",
    ]
    assert counted(0, "song") == "no songs"
    assert counted(1, "song") == "one song"
    assert counted(2400, "category", "categories") == "2,400 categories"


# -- autodj simulate -----------------------------------------------------------


def _cli(tmp_path: Path, *args: str):
    from click.testing import CliRunner

    from autodj.cli import cli

    config = tmp_path / "cli.toml"
    if not config.exists():
        root = (tmp_path / "index").as_posix()
        config.write_text(
            f'[index]\nindex_dir = "{root}"\n[server]\nstation_timezone = "UTC"\n',
            encoding="utf-8",
        )
    return CliRunner().invoke(cli, ["--config", str(config), "simulate", *args])


def test_simulate_prints_the_report_in_words_or_json(tmp_path: Path, monkeypatch) -> None:
    import contextlib
    import json

    from autodj import rotation_sim

    source = _rotation(tmp_path / "station")
    opened = []

    @contextlib.contextmanager
    def fake_open(cfg, station):
        opened.append(station)
        yield source

    monkeypatch.setattr(rotation_sim, "open_station", fake_open)
    words = _cli(tmp_path, "--picks", "12", "--seed", "4", "--start", "2026-10-12T12:00")
    assert words.exit_code == 0, words.output
    assert words.stdout.startswith("Rotation test for station default\n")
    assert "Picked 12 songs" in words.stdout
    assert words.stderr == "Simulating; nothing on air changes.\n"
    as_json = _cli(tmp_path, "--days", "0.25", "--seed", "4", "--json", "--station", "nightowl")
    assert as_json.exit_code == 0, as_json.output
    report = json.loads(as_json.stdout)
    assert report["requested"] == {"hours": 6.0}
    assert report["seed"] == 4
    assert opened == ["default", "nightowl"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--hours", "2", "--picks", "4"], "Give --hours, --days or --picks, only one of them."),
        (["--station", "Night Owl"], "a station name is lower-case letters"),
        (["--start", "tomorrow"], "write the time as 2026-10-12T06:00"),
    ],
)
def test_simulate_refuses_bad_options(tmp_path: Path, args: list[str], message: str) -> None:
    result = _cli(tmp_path, *args)
    assert result.exit_code == 2
    assert message in result.output


def test_simulate_needs_a_library(tmp_path: Path) -> None:
    result = _cli(tmp_path, "--hours", "1")
    assert result.exit_code == 1
    assert "No music library in" in result.stderr


def test_simulate_says_when_the_span_is_too_long(tmp_path: Path) -> None:
    result = _cli(tmp_path, "--days", "31")
    assert result.exit_code == 1
    assert "at most 720 hours" in result.stderr


def test_open_station_reads_the_saved_station_without_writing(tmp_path: Path) -> None:
    from autodj.catalog import LibraryCatalog
    from autodj.config import load_config
    from autodj.rotation_sim import open_station

    config = tmp_path / "cli.toml"
    config.write_text(
        f'[index]\nindex_dir = "{(tmp_path / "index").as_posix()}"\n'
        f'[library]\nmusic_dir = "{(tmp_path / "music").as_posix()}"\n',
        encoding="utf-8",
    )
    cfg = load_config(config, environ={})
    cfg.index.active_dir.mkdir(parents=True)
    LibraryCatalog(cfg.index.active_dir / "library.db").close()
    with open_station(cfg, "nightowl") as source:
        assert source.station == "nightowl"
        assert source.events == []
        assert source.player._clock is None
        with pytest.raises(SimulationError, match="no songs"):
            simulate(source, SimulationRequest(hours=1))
    (cfg.index.active_dir / "schedule.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SimulationError, match="schedule file"), open_station(cfg):
        pass
