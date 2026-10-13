"""Daypart and quarter-hour song separation: repeat rules for the time of day (#129)."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from autodj.config import PlaybackConfig
from autodj.repeat_rules import (
    HISTORY_LIMIT,
    HOURPART_DAYS,
    REASONS,
    ArtistSplitter,
    PlayRecord,
    RepeatRules,
    TrackKeyCache,
    build_filter,
    daypart_spans,
    plays_to_load,
    prune_history,
    quarter_hour,
)
from autodj.repeat_settings import (
    DEFAULT_DAYPART_START_HOURS,
    RepeatSettings,
    ignore_unknown_repeat_keys,
)
from autodj.rotation_sim import SimulationRequest, report_text, simulate
from tests.unit._rotation_station import category_step, make_station, song

NEW_YORK = ZoneInfo("America/New_York")
DAY = 86400.0


def _utc(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()


def _local(text: str, zone: ZoneInfo) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=zone).timestamp()


def _entry(path: str, artist: str = "", title: str = "") -> SimpleNamespace:
    return SimpleNamespace(path=path, artist=artist, title=title or path, album="")


def _played(entry: Any, started: float) -> PlayRecord:
    return PlayRecord.of(entry, started)


def _filter(history, rules: RepeatRules, at: float, zone=UTC):
    return build_filter(history, rules, at, TrackKeyCache(ArtistSplitter()), at=at, zone=zone)[0]


# -- dayparts and quarters ------------------------------------------------------------


def test_the_default_dayparts_are_the_usual_radio_ones() -> None:
    assert DEFAULT_DAYPART_START_HOURS == (0, 6, 10, 15, 19)
    assert RepeatRules().daypart_starts == DEFAULT_DAYPART_START_HOURS


def test_a_daypart_span_runs_to_the_next_start_on_each_day() -> None:
    spans = daypart_spans(_utc("2026-10-12T07:30"), (0, 6, 10, 15, 19), 2, UTC)
    assert spans == [
        (_utc("2026-10-12T06:00"), _utc("2026-10-12T10:00")),
        (_utc("2026-10-11T06:00"), _utc("2026-10-11T10:00")),
        (_utc("2026-10-10T06:00"), _utc("2026-10-10T10:00")),
    ]


def test_the_last_daypart_runs_past_midnight() -> None:
    # 2 in the morning is still the evening that started at 19 the day before.
    spans = daypart_spans(_utc("2026-10-12T02:00"), (6, 19), 1, UTC)
    assert spans == [
        (_utc("2026-10-11T19:00"), _utc("2026-10-12T06:00")),
        (_utc("2026-10-10T19:00"), _utc("2026-10-11T06:00")),
    ]
    # One start makes the whole day one daypart.
    assert daypart_spans(_utc("2026-10-12T23:59"), (0,), 0, UTC) == [
        (_utc("2026-10-12T00:00"), _utc("2026-10-13T00:00"))
    ]


def test_a_daypart_that_starts_in_the_skipped_spring_forward_hour() -> None:
    # New York skipped 2:00 to 3:00 on Sunday 8 March 2026; the 2 o'clock daypart starts
    # at 3:00 that day, so it is an hour shorter, and a song at 3:30 is in it.
    at = _local("2026-03-08T04:00", NEW_YORK)
    spans = daypart_spans(at, (0, 2, 6), 1, NEW_YORK)
    assert spans == [
        (_utc("2026-03-08T07:00"), _utc("2026-03-08T10:00")),
        (_utc("2026-03-07T07:00"), _utc("2026-03-07T11:00")),
    ]
    rules = RepeatRules(track_window=0, artist_window=0, daypart_days=1, daypart_starts=(0, 2, 6))
    for played, held in (
        ("2026-03-08T03:30", True),
        ("2026-03-07T02:30", True),
        ("2026-03-07T01:59", False),
        ("2026-03-07T06:00", False),
    ):
        history = [_played(SONG, _local(played, NEW_YORK))]
        assert (_filter(history, rules, at, NEW_YORK).reason(SONG) == "same daypart") is held


@pytest.mark.parametrize(
    ("played", "held"),
    [
        ("2026-10-11T23:30", True),  # earlier in tonight's 22:00 to 6:00
        ("2026-10-11T05:00", True),  # last night's, which began at 22:00 the day before
        ("2026-10-10T22:00", True),  # its first minute
        ("2026-10-11T07:00", False),  # the 6:00 daypart
        ("2026-10-10T21:59", False),
    ],
)
def test_a_daypart_across_midnight_on_the_station_clock(played: str, held: bool) -> None:
    hours = RepeatSettings.model_validate({"daypart": {"start_hours": [22, 6]}}).daypart
    assert hours.start_hours == [6, 22]
    rules = RepeatRules(
        track_window=0, artist_window=0, daypart_days=1, daypart_starts=tuple(hours.start_hours)
    )
    at = _local("2026-10-12T01:00", NEW_YORK)
    history = [_played(SONG, _local(played, NEW_YORK))]
    assert (_filter(history, rules, at, NEW_YORK).reason(SONG) == "same daypart") is held


def test_dayparts_keep_station_hours_across_a_clock_change() -> None:
    # New York put its clocks back at 2:00 on Sunday 1 November 2026.
    spans = daypart_spans(_local("2026-11-02T07:00", NEW_YORK), (0, 6, 10), 2, NEW_YORK)
    assert [
        (datetime.fromtimestamp(begin, NEW_YORK).hour, datetime.fromtimestamp(end, NEW_YORK).hour)
        for begin, end in spans
    ] == [(6, 10)] * 3
    overnight = daypart_spans(_local("2026-11-01T03:00", NEW_YORK), (0, 6), 0, NEW_YORK)
    assert overnight[0][1] - overnight[0][0] == 7 * 3600  # an hour longer that night


def test_quarters_of_the_hour() -> None:
    assert [
        quarter_hour(_utc(f"2026-10-12T12:{minute:02d}")) for minute in (0, 14, 15, 44, 45)
    ] == [
        0,
        0,
        1,
        2,
        3,
    ]


# -- the daypart rule -----------------------------------------------------------------


NOW = _utc("2026-10-12T07:30")
SONG = _entry("song.flac", "Artist", "Song")
OTHER = _entry("other.flac", "Other", "Other")


@pytest.mark.parametrize(
    ("played", "days", "held"),
    [
        ("2026-10-11T08:00", 1, True),  # yesterday's morning drive
        ("2026-10-11T06:00", 1, True),  # its first minute
        ("2026-10-11T10:00", 1, False),  # midday, the next daypart
        ("2026-10-11T05:59", 1, False),  # overnight
        ("2026-10-12T06:10", 1, True),  # earlier this morning
        ("2026-10-10T08:00", 1, False),  # two days ago, one day looked at
        ("2026-10-10T08:00", 2, True),
        ("2026-10-05T08:00", 7, True),
        ("2026-10-04T08:00", 7, False),
        ("2026-10-11T08:00", 0, False),  # the rule is off
    ],
)
def test_a_song_in_the_same_daypart_is_held_back(played: str, days: int, held: bool) -> None:
    rules = RepeatRules(track_window=0, artist_window=0, daypart_days=days)
    repeat = _filter([_played(SONG, _utc(played)), _played(OTHER, NOW - 60)], rules, NOW)
    assert (repeat.reason(SONG) == "same daypart") is held
    # Another song this morning is held back too, while the rule is on.
    assert repeat.reason(OTHER) == ("same daypart" if days else None)


def test_dayparts_are_read_on_the_station_clock() -> None:
    # 5:00 UTC yesterday was 1 in the morning in New York, overnight as at 5:00 today
    # there; in UTC the song played overnight and the pick starts in morning drive.
    rules = RepeatRules(track_window=0, artist_window=0, daypart_days=1)
    at = _local("2026-10-12T05:00", NEW_YORK)
    history = [_played(SONG, _utc("2026-10-11T05:00"))]
    assert _filter(history, rules, at, NEW_YORK).reason(SONG) == "same daypart"
    assert _filter(history, rules, at, UTC).reason(SONG) is None


def test_the_daypart_is_where_the_song_starts_not_now() -> None:
    rules = RepeatRules(track_window=0, artist_window=0, daypart_days=1)
    history = [_played(SONG, _utc("2026-10-11T10:30"))]
    now = _utc("2026-10-12T09:58")
    cache = TrackKeyCache(ArtistSplitter())
    starts_in_midday = build_filter(history, rules, now, cache, at=now + 180)[0]
    assert starts_in_midday.reason(SONG) == "same daypart"
    assert build_filter(history, rules, now, cache)[0].reason(SONG) is None


def test_versions_of_a_song_share_its_daypart_when_counted_as_one() -> None:
    edit = _entry("edit.flac", "Artist", "Song (Radio Edit)")
    remix = _entry("remix.flac", "Artist", "Song (Remix)")
    rules = RepeatRules(track_window=0, artist_window=0, daypart_days=1)
    history = [_played(edit, _utc("2026-10-11T08:00"))]
    assert _filter(history, rules, NOW).reason(remix) is None
    together = replace(rules, versions_as_one_song=True)
    assert _filter(history, together, NOW).reason(remix) == "same daypart"
    assert _filter(history, together, NOW).reason(edit) == "same daypart"


# -- the quarter-hour rule ------------------------------------------------------------


def _quarters(*minutes_ago_and_minute: tuple[str, int]) -> list[PlayRecord]:
    return [_played(SONG, _utc(f"{day}:{minute:02d}")) for day, minute in minutes_ago_and_minute]


@pytest.mark.parametrize(
    ("plays", "at_minute", "held"),
    [
        (1, 10, True),  # the last play started at :05, the same quarter
        (1, 20, False),
        (1, 50, False),  # the play before was at :50, but only the last counts
        (2, 50, True),
        (2, 35, False),  # the third play back was at :35
        (3, 35, True),
        (0, 10, False),
    ],
)
def test_a_song_is_held_back_from_the_quarters_of_its_last_plays(
    plays: int, at_minute: int, held: bool
) -> None:
    history = _quarters(("2026-10-10T09", 35), ("2026-10-11T09", 50), ("2026-10-12T05", 5))
    rules = RepeatRules(track_window=0, artist_window=0, hourpart_plays=plays)
    at = _utc(f"2026-10-12T12:{at_minute:02d}")
    assert (_filter(history, rules, at).reason(SONG) == "same quarter hour") is held


def test_versions_count_together_toward_the_quarter_hour_rule() -> None:
    edit = _entry("edit.flac", "Artist", "Song (Radio Edit)")
    remix = _entry("remix.flac", "Artist", "Song (Remix)")
    # The edit started at :05; the remix played since, at :20.
    history = [
        _played(edit, _utc("2026-10-11T09:05")),
        _played(remix, _utc("2026-10-12T06:20")),
    ]
    at = _utc("2026-10-12T12:10")
    rules = RepeatRules(track_window=0, artist_window=0, hourpart_plays=1)
    # Apart, each file's own last play counts: the edit's :05 holds it back.
    assert _filter(history, rules, at).reason(edit) == "same quarter hour"
    assert _filter(history, rules, at).reason(remix) is None
    # Together, the song's last play is the remix at :20, so neither is held at :10 ...
    together = replace(rules, versions_as_one_song=True)
    assert _filter(history, together, at).reason(edit) is None
    assert _filter(history, together, at).reason(remix) is None
    # ... and both are at :25, and with two plays counted, at :10 too.
    assert _filter(history, together, _utc("2026-10-12T12:25")).reason(edit) == "same quarter hour"
    two = replace(together, hourpart_plays=2)
    assert _filter(history, two, at).reason(remix) == "same quarter hour"


def test_the_quarter_hour_rule_looks_back_a_week() -> None:
    rules = RepeatRules(track_window=0, artist_window=0, hourpart_plays=1)
    at = _utc("2026-10-12T12:05")
    assert _filter([_played(SONG, at - 6 * DAY)], rules, at).reason(SONG) == "same quarter hour"
    assert _filter([_played(SONG, at - (HOURPART_DAYS * DAY + 60))], rules, at).reason(SONG) is None


def test_the_new_reasons_come_last_and_are_counted() -> None:
    assert REASONS[-2:] == ("same daypart", "same quarter hour")
    rules = RepeatRules(track_window=0, artist_window=1, daypart_days=1, hourpart_plays=1)
    at = _utc("2026-10-12T07:05")
    same_artist = _entry("same.flac", "Someone", "Another")
    history = [_played(SONG, _utc("2026-10-11T07:10")), _played(same_artist, at - 60)]
    repeat = _filter(history, rules, at)
    # The artist rule is checked first, then the daypart, then the quarter hour.
    assert repeat.reason(same_artist) == "same artist"
    assert repeat.reason(SONG) == "same daypart"
    assert not repeat(same_artist) and not repeat(SONG)
    assert repeat.summary() == "1 same artist, 1 same daypart"
    only_quarter = _filter(history, replace(rules, daypart_days=0), at)
    assert only_quarter.reason(SONG) == "same quarter hour"


def test_both_rules_are_off_by_default() -> None:
    settings = RepeatSettings()
    assert settings.daypart.days == 0
    assert settings.daypart.start_hours == list(DEFAULT_DAYPART_START_HOURS)
    assert settings.hourpart.plays == 0
    rules = RepeatRules.from_playback(PlaybackConfig())
    assert (rules.daypart_days, rules.hourpart_plays) == (0, 0)
    history = [_played(SONG, NOW - DAY)]
    repeat = _filter(history, replace(rules, artist_window=0), NOW)
    assert not repeat
    assert repeat.reason(SONG) is None


# -- history kept -----------------------------------------------------------------------


def test_the_history_keeps_the_days_the_rules_look_at() -> None:
    base = RepeatRules(track_window=5, artist_window=2)
    assert base.keep_seconds == 0
    assert replace(base, daypart_days=3).keep_seconds == 5 * DAY
    assert replace(base, hourpart_plays=2).keep_seconds == HOURPART_DAYS * DAY
    # Either one loads the saved plays its days can hold, at a minute a song.
    assert plays_to_load(base) == 5
    assert plays_to_load(replace(base, daypart_days=1)) == 3 * 1440
    assert plays_to_load(replace(base, daypart_days=7)) == 9 * 1440
    assert plays_to_load(replace(base, hourpart_plays=1)) == HOURPART_DAYS * 1440
    # A minute window or DMCA mode still loads up to the cap.
    assert plays_to_load(replace(base, daypart_days=1, artist_minutes=30)) == HISTORY_LIMIT
    assert plays_to_load(replace(base, daypart_days=1), minimum_seconds=10800) == HISTORY_LIMIT
    # A long track window is never cut short.
    assert plays_to_load(replace(base, daypart_days=1, track_window=6000)) == 6000
    from collections import deque

    history = deque(_played(SONG, NOW - age * DAY) for age in (9, 4, 2, 0.1))
    prune_history(history, replace(base, daypart_days=3), NOW)
    assert [round((NOW - record.started) / DAY, 1) for record in history] == [4, 2, 0.1]


# -- settings -----------------------------------------------------------------------------


def test_settings_map_onto_the_rules() -> None:
    playback = PlaybackConfig(
        repeat_rules={"daypart": {"days": 2, "start_hours": [19, 5]}, "hourpart": {"plays": 3}}
    )
    rules = RepeatRules.from_playback(playback)
    assert (rules.daypart_days, rules.daypart_starts, rules.hourpart_plays) == (2, (5, 19), 3)


@pytest.mark.parametrize(
    "values",
    [
        {"daypart": {"days": 8}},
        {"daypart": {"days": -1}},
        {"daypart": {"days": "1"}},
        {"daypart": {"days": 1.5}},
        {"daypart": {"start_hours": []}},
        {"daypart": {"start_hours": [24]}},
        {"daypart": {"start_hours": [-1, 6]}},
        {"daypart": {"start_hours": [6, 6]}},
        {"daypart": {"start_hours": ["6"]}},
        {"daypart": {"start_hours": "0, 6"}},
        {"hourpart": {"plays": 4}},
        {"hourpart": {"plays": True}},
    ],
)
def test_out_of_range_settings_are_refused(values: dict) -> None:
    with pytest.raises(ValidationError):
        RepeatSettings.model_validate(values)


def test_unknown_keys_in_the_new_tables_are_warned_about_and_ignored(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        cleaned = ignore_unknown_repeat_keys(
            {"daypart": {"days": 1, "hours": [6]}, "hourpart": {"plays": 1, "quarters": 2}}
        )
    assert cleaned == {"daypart": {"days": 1}, "hourpart": {"plays": 1}}
    assert "[playback.repeat_rules.daypart]: ['hours']" in caplog.text
    assert "[playback.repeat_rules.hourpart]: ['quarters']" in caplog.text


def test_config_toml_reads_the_new_tables(tmp_path: Path, caplog) -> None:
    from autodj.config import load_config

    config = tmp_path / "config.toml"
    config.write_text(
        "[playback.repeat_rules.daypart]\ndays = 1\nstart_hours = [0, 12]\nextra = 1\n"
        "[playback.repeat_rules.hourpart]\nplays = 2\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        cfg = load_config(config, environ={})
    rules = cfg.playback.repeat_rules
    assert (rules.daypart.days, rules.daypart.start_hours, rules.hourpart.plays) == (1, [0, 12], 2)
    assert "extra" in caplog.text


# -- relaxing ---------------------------------------------------------------------------


def _trio_station(tmp_path: Path, rules: dict, **changes: Any):
    """Three songs in the only clockwheel step, and a hundred others for the track window."""
    trio = [song(n, f"Artist {n}") for n in range(3)]
    rest = [song(100 + n, f"Other {n}") for n in range(100)]
    options: dict[str, Any] = {
        "categories": [{"id": "trio", "name": "Trio", "members": trio}],
        "clockwheels": [{"name": "Only", "steps": [category_step("trio")]}],
        "active_clock": "Only",
        "playback": {"pick_mode": "clock", "repeat_rules": rules},
    }
    options.update(changes)
    return make_station(tmp_path, [*trio, *rest], **options)


def test_a_clock_step_relaxes_the_daypart_with_the_artist_rules_before_the_track_window(
    tmp_path: Path,
) -> None:
    # The window holds the last two songs back; the daypart, all three once each has played.
    source = _trio_station(
        tmp_path,
        {
            "track": {"tracks": 2},
            "artist": {"tracks": 1},
            "daypart": {"days": 1, "start_hours": [0]},
        },
    )
    report = simulate(source, SimulationRequest(picks=30, start=NOON, seed=4))
    relaxed = report["relaxed"]
    assert relaxed["same daypart"] >= 25
    # The window was kept every time: the one song it did not hold back played.
    assert "track repeat window" not in relaxed
    assert "back to back" not in relaxed
    assert report["dead_air"]["failed_picks"] == 0


def test_dmca_limits_still_hold_with_the_daypart_rule_on(tmp_path: Path) -> None:
    # One act and one album: the DMCA limits allow few of its songs, and the daypart
    # rule soon holds all of them back too.
    solo = [song(n, "Solo Act", album="One Album") for n in range(8)]
    others = [song(100 + n, f"Artist {n}", album=f"Album {n}") for n in range(40)]
    source = make_station(
        tmp_path,
        [*solo, *others],
        categories=[{"id": "solo", "name": "Solo", "members": solo}],
        clockwheels=[{"name": "Only", "steps": [category_step("solo")]}],
        active_clock="Only",
        playback={
            "pick_mode": "clock",
            "repeat_rules": {
                "artist": {"tracks": 0},
                "daypart": {"days": 1, "start_hours": [0]},
                "hourpart": {"plays": 3},
            },
        },
        dmca=True,
    )
    report = simulate(source, SimulationRequest(hours=3, start=NOON, seed=4))
    assert report["dmca"]["replaced"] > 0
    assert report["dmca"]["broken"] == 0


# -- the rotation tester counts the relaxations ----------------------------------------


NOON = _utc("2026-10-12T12:00")


@pytest.mark.parametrize(
    ("rules", "reason"),
    [
        ({"daypart": {"days": 1, "start_hours": [0]}}, "same daypart"),
        ({"hourpart": {"plays": 3}}, "same quarter hour"),
    ],
)
def test_the_rotation_tester_reports_how_often_the_new_rules_relaxed(
    tmp_path: Path, rules: dict, reason: str
) -> None:
    trio = [song(n, f"Artist {n}") for n in range(3)]
    source = make_station(
        tmp_path,
        trio,
        categories=[{"id": "trio", "name": "Trio", "members": trio}],
        clockwheels=[{"name": "Only", "steps": [category_step("trio")]}],
        active_clock="Only",
        playback={
            "pick_mode": "clock",
            "repeat_rules": {"track": {"tracks": 1}, "artist": {"tracks": 0}, **rules},
        },
    )
    report = simulate(source, SimulationRequest(picks=30, start=NOON, seed=4))
    # Three songs cannot keep out of one daypart all day, or out of every quarter.
    assert report["relaxed"][reason] > 0
    assert report["dead_air"]["failed_picks"] == 0
    assert f"{reason.capitalize()}: relaxed for" in report_text(report)
