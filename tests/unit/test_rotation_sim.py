"""The rotation rule tester: a dry run of the station's own picker (#125)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import pytest

from autodj.player import Player
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
    assert (
        reasons["Step 1: category Power: all matching entries blocked by rules"]
        == power["skipped"]
    )
    # The clock moved on to Current and Gold instead, so no rule had to be relaxed.
    assert power["plays"] == 6
    assert report["relaxed"] == {}
    text = report_text(report)
    assert "Step 1: category Power: all matching entries blocked by rules:" in text


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


def test_related_tracks_count_as_their_own_rule(tmp_path: Path) -> None:
    pair = [song(1, "Alpha Group", title="Original"), song(2, "Beta Duo", title="Sampler")]
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
                "artist": {"tracks": 0},
                "related_track": {"tracks": 1},
                "use_related_tracks": True,
            },
        },
        related_tracks={"Alpha Group - Original": ["Beta Duo - Sampler"]},
    )
    report = simulate(source, SimulationRequest(picks=10, start=NOON, seed=2))
    assert report["relaxed"]["related track"] == 9


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
        "Step 1: category Late night: outside its hours: plays only every day from 22:00 until 02:00"
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
    unknown = pytest.raises(SimulationError, match="No station named nightowl is configured")
    with unknown, open_station(cfg, "nightowl"):
        pass
    with open_station(cfg) as source:
        assert source.station == "default"
        assert source.events == []
        assert source.player._clock is None
        with pytest.raises(SimulationError, match="no songs"):
            simulate(source, SimulationRequest(hours=1))
    (cfg.index.active_dir / "schedule.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SimulationError, match="schedule file"), open_station(cfg):
        pass


# -- review fixes: read-only, reproducible, bounded ----------------------------


def _files(folder: Path) -> dict[str, tuple[bytes, int]]:
    """Every file under *folder*: its bytes and modification time.

    SQLite's shared-memory index of the write-ahead log (``-shm``) is only
    named: readers map it, so it is SQLite's own to touch.
    """
    return {
        str(path.relative_to(folder)): (
            (b"", 0) if path.name.endswith("-shm") else (path.read_bytes(), path.stat().st_mtime_ns)
        )
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def test_simulate_beside_a_live_server_leaves_the_library_untouched(tmp_path: Path) -> None:
    """The CLI opens the catalog read-only: no lock, no migration, no write."""
    from dataclasses import replace

    from autodj.catalog import LibraryCatalog
    from autodj.config import load_config
    from autodj.rotation_sim import open_station

    music = tmp_path / "music"
    music.mkdir()
    config = tmp_path / "cli.toml"
    config.write_text(
        f'[index]\nindex_dir = "{(tmp_path / "index").as_posix()}"\n'
        f'[library]\nmusic_dir = "{music.as_posix()}"\n'
        '[server]\nstation_timezone = "UTC"\n[playback]\npick_mode = "random"\n',
        encoding="utf-8",
    )
    cfg = load_config(config, environ={})
    active = cfg.index.active_dir
    active.mkdir(parents=True)
    # A categories file that cannot be read stays where it is.
    (active / "categories.json").write_text("{broken", encoding="utf-8")
    live = LibraryCatalog(active / "library.db")
    try:
        live.seed_sources(music, tmp_path / "liners")
        live.seed_entries(
            replace(song(n, f"Artist {n}"), path=str(music / f"{n:03d}.flac")) for n in range(30)
        )
        # The running server holds the write lock for the whole run.
        live._db().execute("BEGIN IMMEDIATE")
        before = _files(active)
        with open_station(cfg) as source:
            report = simulate(source, SimulationRequest(picks=10, start=NOON, seed=1))
        assert report["picks"] == 10
        assert _files(active) == before
        assert any(name.endswith("library.db-wal") for name in before)
    finally:
        live._db().rollback()
        live.close()


def test_a_corrupt_liner_dates_file_is_read_never_moved(tmp_path: Path) -> None:
    music = _songs(0, 20)
    source = make_station(
        tmp_path,
        music,
        liners=[liner(n) for n in range(3)],
        categories=[{"id": "all", "name": "All", "members": music}],
        clockwheels=[{"name": "Main", "steps": [liner_step(), category_step("all")]}],
        active_clock="Main",
        playback={"pick_mode": "clock"},
    )
    dates = tmp_path / "liner_dates.json"
    dates.write_text("{this is not json", encoding="utf-8")
    source.liner_dates = dates
    before = _files(tmp_path)
    report = simulate(source, SimulationRequest(picks=12, start=NOON, seed=1))
    assert report["liners"] > 0  # Every liner plays, as with no dates.
    assert dates.read_text(encoding="utf-8") == "{this is not json"
    assert not list(tmp_path.glob("liner_dates.json.bad-*"))
    assert _files(tmp_path) == before


def _sequence(source: Any, seed: int, picks: int = 30) -> list[tuple[float, str]]:
    played: list[tuple[float, str]] = []
    simulate(
        source,
        SimulationRequest(picks=picks, start=NOON, seed=seed),
        on_pick=lambda when, entry: played.append((when, entry.path)),
    )
    return played


def test_similarity_runs_repeat_song_for_song_with_their_seed(tmp_path: Path, monkeypatch) -> None:
    import random as module

    import numpy

    source = make_station(
        tmp_path,
        _songs(0, 60),
        playback={
            "pick_mode": "similarity",
            "transition_mode": "full_intro_outro",
            "pick_top_k": 8,
            "pick_temperature": 2.0,
            "discovery_every": 5,
        },
        similarity=True,
    )
    source.player._state.discovery_enabled = True
    first = _sequence(source, 21)

    def shared(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("a pick used a shared random generator")

    # Nothing the run picks comes from the shared generators.
    monkeypatch.setattr(module, "choice", shared)
    monkeypatch.setattr(module, "shuffle", shared)
    monkeypatch.setattr(numpy.random, "choice", shared)
    assert _sequence(source, 21) == first
    assert len(first) == 30
    assert _sequence(source, 22) != first


def test_the_start_pick_without_a_library_uses_the_run_generator() -> None:
    import random as module

    from tests.unit._fakes import make_cfg_mock, make_sim_index

    player = Player(make_cfg_mock(), make_sim_index(12), dry_run=True)
    picks = []
    for _ in range(2):
        player._random = module.Random(5)
        picks.append([player._random_start_entry().path for _ in range(6)])
    assert picks[0] == picks[1]
    assert len(set(picks[0])) > 1


def test_runs_do_not_depend_on_the_hash_seed(tmp_path: Path) -> None:
    """Candidate order never follows set order: two processes, two hash seeds, one run."""
    import json
    import os
    import subprocess  # nosec B404 -- the test runs its own interpreter
    import sys

    script = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from datetime import UTC, datetime\n"
        "from autodj.rotation_sim import SimulationRequest, simulate\n"
        "from tests.unit._rotation_station import make_station, song, category_step\n"
        "root = Path(sys.argv[1])\n"
        "music = [song(n, f'Artist {n % 13}', album=f'Album {n % 7}') for n in range(80)]\n"
        "source = make_station(root, music, categories=[\n"
        "    {'id': 'a', 'name': 'A', 'members': music[:30]},\n"
        "    {'id': 'b', 'name': 'B', 'members': music[20:]}],\n"
        "    clockwheels=[{'name': 'Main', 'steps': [category_step('a'), category_step('b')]}],\n"
        "    active_clock='Main', playback={'pick_mode': 'clock'}, dmca=True)\n"
        "played = []\n"
        "start = datetime(2026, 10, 12, 12, tzinfo=UTC).timestamp()\n"
        "simulate(source, SimulationRequest(picks=60, start=start, seed=3),\n"
        "         on_pick=lambda when, entry: played.append(entry.path))\n"
        "print(json.dumps(played))\n"
    )
    runs = []
    for hash_seed in ("1", "2"):
        done = subprocess.run(  # nosec B603 -- fixed arguments
            [sys.executable, "-c", script, str(tmp_path / hash_seed)],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
            cwd=Path(__file__).resolve().parents[2],
            check=True,
            timeout=300,
        )
        runs.append(json.loads(done.stdout.strip().splitlines()[-1]))
    assert runs[0] == runs[1]
    assert len(runs[0]) == 60


def test_a_run_matches_the_live_player_pick_for_pick(tmp_path: Path, monkeypatch) -> None:
    """The dry run's player is the live one plus a clock: same seed, same picks."""
    import random as module

    from autodj import player as player_module
    from autodj.catalog import normalize_path

    music = [song(n, f"Artist {n % 9}", album=f"Album {n % 5}") for n in range(40)]
    source = make_station(
        tmp_path,
        music,
        categories=[
            {"id": "a", "name": "A", "members": music[:12]},
            {"id": "b", "name": "B", "members": music[8:]},
        ],
        clockwheels=[{"name": "Main", "steps": [category_step("a"), category_step("b")]}],
        active_clock="Main",
        playback={"pick_mode": "clock"},
    )
    simulated = [path for _, path in _sequence(source, 9, picks=25)]

    # The same picks made by the live player class itself, driven by hand.
    live = source.player
    now = [NOON]
    monkeypatch.setattr(player_module.time, "time", lambda: now[0])
    live._state.clock = lambda: now[0]
    played: dict[str, float] = {}

    class Ledger:
        def setting_key(self, name: str) -> str:
            return name

        def last_played(self) -> dict[str, float]:
            return played

        def remember_setting(self, *_args: Any) -> None:
            pass

    live._ledger = Ledger()
    live._random = module.Random(9)
    live.activate_clock("Main", 0)
    current = live._random_start_entry()
    live._pending_clock_cursor = live._seed_clock_cursor
    live._pending_clock_name = live._seed_clock_name
    by_hand = []
    starts = NOON
    for number in range(25):
        if number:
            # The next song is picked as the current one starts, for when it ends.
            context = live._pick_context(at=live._handoff_time(current))
            upcoming, _mode, _liners, cursor, _key = live._choose_station_next(current, context)
            live._pending_clock_cursor = cursor
            starts += current.length
            current = upcoming
        now[0] = starts
        live._state.record_played(current)
        live._state.track_number += 1
        live._state.current_track = current
        played[normalize_path(current.path)] = starts  # As the station ledger keys it.
        by_hand.append(current.path)
    assert simulated == by_hand


def test_a_bad_schedule_event_is_a_refusal_not_a_failed_run(tmp_path: Path) -> None:
    events = [
        _event(
            name="Odd mode",
            action="pick_mode",
            params={"mode": "sideways"},
            repeat="daily",
            time=time(12, 30),
        )
    ]
    report = simulate(
        _rotation(tmp_path, events=events), SimulationRequest(hours=2, start=NOON, seed=1)
    )
    assert report["picks"] > 0
    [(reason, count)] = report["schedule"]["refused"].items()
    assert reason.startswith("Odd mode: it could not be applied:")
    assert count == 1


@pytest.mark.parametrize("start", [_at("1999-12-31 23:59"), _at("2100-01-01 00:00"), float("nan")])
def test_a_start_out_of_range_is_refused(start: float) -> None:
    with pytest.raises(SimulationError, match="between 1 January 2000 and 31 December 2099"):
        SimulationRequest(hours=1, start=start)


def test_a_run_stops_at_its_time_budget_and_says_so(tmp_path: Path) -> None:
    report = simulate(
        _rotation(tmp_path), SimulationRequest(hours=24, start=NOON, seed=1), budget=0.0
    )
    assert report["stopped_early"] is True
    assert report["stopped_reason"] == "time limit"
    assert "Stopped early: the run reached its time limit of 10 minutes." in report_text(report)


def test_runs_pause_between_picks(tmp_path: Path, monkeypatch) -> None:
    from autodj import rotation_sim

    pauses: list[float] = []
    monkeypatch.setattr(rotation_sim.time, "sleep", pauses.append)
    simulate(_rotation(tmp_path), SimulationRequest(picks=15, start=NOON, seed=1))
    assert len(pauses) >= 14  # One after each pick (the test sees every sleep).
    assert all(pause >= 0.001 for pause in pauses)


def test_the_settings_copy_waits_for_a_settings_change_in_progress(tmp_path: Path) -> None:
    import threading

    from autodj.player import SETTINGS_LOCK
    from autodj.rotation_sim import snapshot_player

    source = _rotation(tmp_path)
    copied = threading.Event()
    with SETTINGS_LOCK:
        worker = threading.Thread(
            target=lambda: (snapshot_player(source.player), copied.set()), daemon=True
        )
        worker.start()
        assert not copied.wait(0.2)
    assert copied.wait(10)
    worker.join(10)


# -- second review: lock scope, atomic events, unreadable libraries ------------


def _held_elsewhere(lock: Any) -> bool:
    """Whether another thread holds *lock* (a probe thread cannot take it)."""
    import threading

    taken: list[bool] = []

    def probe() -> None:
        got = lock.acquire(blocking=False)
        taken.append(got)
        if got:
            lock.release()

    worker = threading.Thread(target=probe)
    worker.start()
    worker.join()
    return not taken[0]


def test_settings_changes_read_files_and_plays_outside_the_settings_lock(
    tmp_path: Path, monkeypatch
) -> None:
    from autodj import related_artist_file
    from autodj._bridge import PlayerBridge
    from autodj.player import SETTINGS_LOCK
    from autodj.repeat_rules import RelatedArtists
    from autodj.settings_bodies import PlaybackSettingsBody

    source = _rotation(tmp_path)
    player = source.player
    bridge = PlayerBridge(
        player=player, sim=player._sim, library=source.library, ledger=source.ledger
    )
    seen: dict[str, bool] = {}

    def related(_folder: Any) -> RelatedArtists:
        seen["related artists file"] = _held_elsewhere(SETTINGS_LOCK)
        return RelatedArtists()

    def restore(_ledger: Any, *, refresh: bool = False) -> int:
        seen["saved plays"] = _held_elsewhere(SETTINGS_LOCK)
        return 0

    monkeypatch.setattr(related_artist_file, "load_configured_related_artists", related)
    monkeypatch.setattr(player, "restore_repeat_history", restore)
    rules = {"artist": {"tracks": 3, "minutes": 0}, "use_related_artists": True}
    bridge.set_playback_settings(
        PlaybackSettingsBody.model_validate({"repeat_rules": rules, "pick_mode": "random"})
    )
    # Reads happen on this thread: the lock would show as free to the probe
    # only if this thread does not hold it.
    assert seen == {"related artists file": False, "saved plays": False}
    assert player._cfg.playback.pick_mode == "random"
    assert player._cfg.playback.repeat_rules.artist.tracks == 3


def test_loading_a_clockwheel_reads_outside_the_settings_lock(tmp_path: Path, monkeypatch) -> None:
    from autodj.player import SETTINGS_LOCK

    source = _rotation(tmp_path)
    player = source.player
    catalog = source.library.catalog
    seen: list[bool] = []
    real_get, real_load = catalog.get_setting, player._load_clockwheel

    def get_setting(key: str, default: Any = None) -> Any:
        seen.append(_held_elsewhere(SETTINGS_LOCK))
        return real_get(key, default)

    def load(name: str) -> Any:
        seen.append(_held_elsewhere(SETTINGS_LOCK))
        return real_load(name)

    monkeypatch.setattr(catalog, "get_setting", get_setting)
    monkeypatch.setattr(player, "_load_clockwheel", load)
    player.configure_clock()
    assert seen == [False, False]
    assert player._clock_status == "Ready"


def test_a_run_never_waits_on_the_live_settings_lock(tmp_path: Path) -> None:
    import threading

    from autodj.player import SETTINGS_LOCK
    from autodj.rotation_sim import snapshot_player

    events = [
        _event(
            name="Switch",
            action="pick_mode",
            params={"mode": "random"},
            repeat="daily",
            time=time(12, 30),
        )
    ]
    source = _rotation(tmp_path, events=events)
    source.snapshot = snapshot_player(source.player)
    reports: list[dict] = []
    with SETTINGS_LOCK:  # A live settings change that never ends.
        worker = threading.Thread(
            target=lambda: reports.append(
                simulate(source, SimulationRequest(hours=2, start=NOON, seed=1))
            ),
            daemon=True,
        )
        worker.start()
        worker.join(120)
        assert not worker.is_alive()
    assert reports[0]["schedule"]["applied"] == 1
    assert reports[0]["song_picking"]["final_mode"] == "random"


def test_the_copy_keeps_the_dry_run_defaults_for_settings_the_player_lacks(
    tmp_path: Path,
) -> None:
    from autodj.rotation_sim import snapshot_player

    source = _rotation(tmp_path)
    del source.player._anchor_to_seed
    snapshot = snapshot_player(source.player)
    assert "_anchor_to_seed" not in snapshot.settings
    source.snapshot = snapshot
    report = simulate(source, SimulationRequest(picks=5, start=NOON, seed=1))
    assert report["picks"] == 5


def test_a_refused_clockwheel_switch_changes_nothing(tmp_path: Path) -> None:
    events = [
        _event(
            name="Missing clock",
            action="pick_mode",
            params={"mode": "clock:Gone"},
            repeat="daily",
            time=time(12, 30),
        )
    ]
    report = simulate(
        _rotation(tmp_path, events=events), SimulationRequest(hours=2, start=NOON, seed=1)
    )
    [reason] = report["schedule"]["refused"]
    assert reason.startswith("Missing clock: the clockwheel cannot play: Clockwheel Gone is off")
    # The clock it would have replaced kept playing.
    assert report["song_picking"]["final_clockwheel"] == "Main"
    assert report["modes"].get("clock", 0) == report["picks"] - report["modes"].get("start", 0)


def test_an_event_that_fails_half_way_is_undone(tmp_path: Path, monkeypatch) -> None:
    from autodj._bridge import PlayerBridge

    events = [
        _event(
            name="Evening",
            action="pick_mode",
            params={"mode": "clock:Second"},
            repeat="daily",
            time=time(12, 30),
        )
    ]
    second = {"name": "Second", "steps": [category_step("gold")]}
    source = _rotation(
        tmp_path,
        events=events,
        clockwheels=[
            {
                "name": "Main",
                "steps": [category_step("power"), category_step("current")],
            },
            second,
        ],
    )

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("settings store went away")

    monkeypatch.setattr(PlayerBridge, "set_playback_settings", broken)
    report = simulate(source, SimulationRequest(hours=2, start=NOON, seed=1))
    assert report["schedule"]["refused"] == {
        "Evening: it could not be applied: settings store went away": 1
    }
    # The clockwheel switch made before the failure was put back.
    assert report["song_picking"]["final_clockwheel"] == "Main"
    gold = next(row for row in report["categories"] if row["name"] == "Gold")
    assert gold["plays"] == 0


@pytest.mark.parametrize("content", [b"", b"this is not a database at all" * 40])
def test_an_unreadable_library_stops_simulate_in_words(tmp_path: Path, content: bytes) -> None:
    from autodj.config import load_config
    from autodj.rotation_sim import open_station

    config = tmp_path / "cli.toml"
    config.write_text(
        f'[index]\nindex_dir = "{(tmp_path / "index").as_posix()}"\n', encoding="utf-8"
    )
    cfg = load_config(config, environ={})
    cfg.index.active_dir.mkdir(parents=True)
    database = cfg.index.active_dir / "library.db"
    database.write_bytes(content)
    with pytest.raises(SimulationError) as refused, open_station(cfg):
        pass
    message = str(refused.value)
    assert message.startswith(f"The music library {database} cannot be read (")
    assert message.endswith("start autodj serve once to check it.")
    assert database.read_bytes() == content


def test_the_first_pick_after_a_fresh_start_takes_the_next_clock_step(tmp_path: Path) -> None:
    """The start's clock place, and the clockwheel it is in, carry into the next pick."""
    first, second = _songs(0, 10), _songs(100, 10)
    source = make_station(
        tmp_path,
        [*first, *second],
        categories=[
            {"id": "one", "name": "One", "members": first},
            {"id": "two", "name": "Two", "members": second},
        ],
        clockwheels=[{"name": "Main", "steps": [category_step("one"), category_step("two")]}],
        active_clock="Main",
        playback={"pick_mode": "clock"},
    )
    played = [path for _, path in _sequence(source, 4, picks=6)]
    ones = {entry.path for entry in first}
    assert [path in ones for path in played] == [True, False, True, False, True, False]
