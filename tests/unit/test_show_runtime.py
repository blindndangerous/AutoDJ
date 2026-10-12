"""The show on air: start, end, takeover, timing, restart and liners (autodj.show_runtime)."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from autodj.catalog import normalize_path
from autodj.repeat_rules import RepeatRules, TrackKeyCache
from autodj.show_runtime import CheckBody, ShowHooks, ShowManager, _inside, end_at_wall_time
from autodj.shows import ClockState, Resume, ShowError, parse_spec
from tests.unit._fakes import make_entry

START = datetime(2026, 10, 11, 18, 0, tzinfo=UTC)
INTRO, OUTRO, OTHER = "1" * 32, "2" * 32, "3" * 32


def song(n: int, year: int, genre: str = "Rock"):
    entry = make_entry(n)
    entry.path = normalize_path(f"Z:/Music/song_{n}.flac")
    entry.artist, entry.album, entry.title = f"Artist {n}", f"Album {n}", f"Song {n}"
    entry.year, entry.genre = year, genre
    return entry


SONGS = [song(n, 1990 + n) for n in range(20)]


class Station:
    """Fake hooks: records what the runtime asked the station to do."""

    def __init__(self, tmp_path: Path) -> None:
        self.calls: list[tuple] = []
        self.theme = None
        self.picking_now = Resume(
            pick_mode="similarity",
            transition_mode="full_intro_outro",
            clock=ClockState(name="Station", cursor=7),
        )
        self.clock_fails: Exception | None = None
        self.messages: list[dict] = []
        liners_root = tmp_path / "liners"
        self.sources = [{"id": "src", "path": str(liners_root), "kind": "liner"}]
        self.liner_files = [
            (INTRO, liners_root / "intro.mp3"),
            (OUTRO, liners_root / "outro.mp3"),
            (OTHER, liners_root / "shows" / "2000s" / "id.mp3"),
        ]

    def hooks(self) -> ShowHooks:
        async def use_clock(name: str) -> None:
            if self.clock_fails is not None:
                raise self.clock_fails
            self.calls.append(("use_clock", name))

        async def restore(resume: Resume) -> None:
            self.calls.append(("restore", resume.pick_mode, resume.clock))

        async def publish(message: dict) -> None:
            self.messages.append(message)

        def apply_theme(theme) -> None:
            self.theme = theme
            self.calls.append(("theme", theme.name if theme else None))

        def decode(liner_id: str):
            if liner_id == OTHER:
                raise ValueError("gone")
            return np.full((4, 2), 0.5, dtype=np.float32)

        return ShowHooks(
            entries=lambda: SONGS,
            beets=lambda: None,
            rules=lambda: RepeatRules(track_window=0, artist_window=0),
            keys=TrackKeyCache,
            dmca=lambda: False,
            apply_theme=apply_theme,
            interrupt=lambda: self.calls.append(("interrupt",)),
            picking=lambda: self.picking_now,
            use_clock=use_clock,
            restore=restore,
            liner_sources=lambda: self.sources,
            liners=lambda: self.liner_files,
            clock_names=lambda: ["Station", "Decades"],
            decode=decode,
            on_started=lambda: self.calls.append(("started",)),
            on_changed=lambda: self.calls.append(("changed",)),
            publish=publish,
        )


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def station(tmp_path: Path) -> Station:
    return Station(tmp_path)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def manager(tmp_path: Path, station: Station, clock: Clock) -> ShowManager:
    return ShowManager(tmp_path / "shows.json", "UTC", station.hooks(), clock=clock)


def run(coroutine):
    return asyncio.run(coroutine)


def add(manager: ShowManager, **fields):
    body = {"name": "Remembering the 2000s", "year_from": 2000, "year_to": 2009, **fields}
    show, _report = run(manager.add(parse_spec(body)))
    return show


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def test_saving_checks_the_filter_and_reports_the_count(manager, station, tmp_path) -> None:
    show, report = run(manager.add(parse_spec({"name": "Nineties", "year_to": 1999})))
    assert report["count"] == 10
    assert report["count_text"] == "10 songs match"
    assert report["fallback"] == 10
    assert report["fallback_examples"][0] == "Artist 0 \u2014 Song 0"
    assert report["errors"] == [] and report["beets"] is False
    saved = json.loads((tmp_path / "shows.json").read_text(encoding="utf-8"))
    assert saved["shows"][0]["id"] == show.id
    assert station.messages == [{"type": "shows_changed"}]
    assert manager.choices() == [(show.id, "Nineties")]
    assert manager.view(show)["summary"].startswith("First released in 1999 or earlier.")


def test_a_short_show_warns_about_the_repeat_rules(manager, station) -> None:
    station_rules = RepeatRules(track_window=50, artist_window=0)
    manager.hooks.rules = lambda: station_rules
    _show, report = run(
        manager.add(parse_spec({"name": "Tiny", "year_from": 2009, "default_minutes": 60}))
    )
    # An hour of three-minute songs is 20 songs; one song matches.
    assert report["warning"] == "about 20 songs needed; only 1 fit your track rule"


@pytest.mark.parametrize(
    ("fields", "field", "words"),
    [
        ({"clockwheel": "Missing"}, "clockwheel", "no clockwheel named Missing"),
        ({"intro": "f" * 32}, "intro", "no longer registered"),
        ({"liner_folder": {"source": "gone"}}, "liner_folder", "no longer registered"),
        ({"query": "mood:happy"}, "query", "field named mood"),
    ],
)
def test_saving_refuses_what_does_not_exist_now(manager, fields, field, words) -> None:
    with pytest.raises(ShowError) as caught:
        run(manager.add(parse_spec({"name": "A", **fields})))
    assert caught.value.field == field
    assert words in str(caught.value)


def test_names_are_unique_ignoring_case(manager) -> None:
    add(manager)
    with pytest.raises(ShowError, match="already exists"):
        add(manager, name="REMEMBERING the 2000s")


def test_at_most_a_hundred_shows(manager) -> None:
    first = add(manager, name="S0")
    manager.doc.shows = [
        first.model_copy(update={"id": f"{n:032x}", "name": f"S{n}"}) for n in range(100)
    ]
    with pytest.raises(ShowError, match="at most 100"):
        add(manager, name="One too many")


def test_check_counts_without_saving(manager, tmp_path) -> None:
    report = run(manager.check(CheckBody(year_from=2000, minutes=30)))
    assert report["count"] == 10
    assert not (tmp_path / "shows.json").exists()
    with pytest.raises(ShowError, match="ends before it starts"):
        run(manager.check(CheckBody(year_from=2005, year_to=2000)))


def test_options_list_genres_liners_folders_and_clockwheels(manager) -> None:
    options = manager.options()
    assert options["genres"] == ["Rock"]
    assert [item["name"] for item in options["liners"]] == ["id.mp3", "intro.mp3", "outro.mp3"]
    assert options["liner_folders"][0]["id"] == "src"
    assert options["clockwheels"] == ["Station", "Decades"]


# ---------------------------------------------------------------------------
# Starting and ending
# ---------------------------------------------------------------------------


def test_start_gives_the_player_the_show_songs_until_the_end(manager, station, clock) -> None:
    show = add(manager, intro=INTRO, outro=OUTRO)
    active = run(manager.start(show.id, START + timedelta(hours=1)))
    assert station.theme.paths == {entry.path for entry in SONGS[10:]}
    assert station.theme.name == show.name
    assert active.run_id == station.theme.run_id
    assert manager.state() == {
        "id": show.id,
        "name": show.name,
        "started_at": "2026-10-11T18:00:00+00:00",
        "ends_at": "2026-10-11T19:00:00+00:00",
    }
    assert manager.public_state() == {"name": show.name, "ends_at": "2026-10-11T19:00:00+00:00"}
    assert manager.active_view()["count"] == 10
    assert ("started",) in station.calls
    # Soft start: the song playing is not cut.
    assert ("interrupt",) not in station.calls
    # The intro plays once, at the next song start.
    assert manager.due_audio() is not None
    assert manager.due_audio() is None


def test_hard_start_fades_the_song_playing(manager, station) -> None:
    show = add(manager, start="hard")
    run(manager.start(show.id, START + timedelta(hours=1)))
    assert station.calls.index(("theme", show.name)) < station.calls.index(("interrupt",))


def test_a_show_that_matches_nothing_does_not_start(manager, station) -> None:
    show = add(manager, year_from=2050, year_to=2060)
    with pytest.raises(ShowError, match="No song matches"):
        run(manager.start(show.id, START + timedelta(hours=1)))
    assert manager.state() is None and station.theme is None


def test_start_refuses_an_end_in_the_past_and_an_unknown_show(manager) -> None:
    show = add(manager)
    with pytest.raises(ShowError, match="passed"):
        run(manager.start(show.id, START))
    with pytest.raises(KeyError):
        run(manager.start("f" * 32, START + timedelta(hours=1)))


def test_soft_end_at_the_end_time_then_the_outro(manager, station, clock) -> None:
    show = add(manager, intro=INTRO, outro=OUTRO)
    run(manager.start(show.id, START + timedelta(hours=1)))
    manager.due_audio()
    clock.now = START + timedelta(minutes=59)
    run(manager.tick())
    assert manager.state() is not None
    clock.now = START + timedelta(hours=1)
    run(manager.tick())
    assert manager.state() is None and station.theme is None
    assert ("interrupt",) not in station.calls
    assert ("changed",) in station.calls
    assert manager.due_audio() is not None  # the outro, over the next song
    assert station.messages[-1] == {"type": "shows_changed"}
    run(manager.tick())  # nothing on air: nothing happens


def test_hard_end_fades_the_song_playing(manager, station, clock) -> None:
    show = add(manager, end="hard")
    run(manager.start(show.id, START + timedelta(minutes=30)))
    clock.now = START + timedelta(minutes=31)
    run(manager.tick())
    assert station.calls[-2:] == [("interrupt",), ("changed",)]


def test_stop_ends_the_show_now(manager, station) -> None:
    show = add(manager, outro=OUTRO)
    assert run(manager.stop()) is None
    run(manager.start(show.id, START + timedelta(hours=1)))
    ended = run(manager.stop())
    assert ended is not None and ended.show_id == show.id
    assert manager.state() is None
    assert manager.due_audio() is not None


def test_a_new_show_takes_over_and_skips_the_old_outro(manager, station) -> None:
    first = add(manager, outro=OUTRO)
    second = add(manager, name="Nineties", year_from=1990, year_to=1999, intro=INTRO)
    run(manager.start(first.id, START + timedelta(hours=2)))
    manager.due_audio()
    run(manager.start(second.id, START + timedelta(hours=1)))
    assert manager.state()["name"] == "Nineties"
    assert station.theme.paths == {entry.path for entry in SONGS[:10]}
    # The new show's intro is due, not the old show's outro.
    audio = manager.due_audio()
    assert audio is not None
    assert manager.due_audio() is None


def test_a_bad_intro_is_logged_and_skipped(manager, caplog) -> None:
    manager.hooks.liners = lambda: [(OTHER, Path("x.mp3"))]
    show = add(manager, intro=OTHER)
    run(manager.start(show.id, START + timedelta(hours=1)))
    assert manager.due_audio() is None
    assert "Could not play the show's intro or outro" in caplog.text


# ---------------------------------------------------------------------------
# Clockwheels
# ---------------------------------------------------------------------------


def test_a_show_clockwheel_is_used_and_the_station_comes_back(manager, station, clock) -> None:
    show = add(manager, clockwheel="Decades")
    run(manager.start(show.id, START + timedelta(hours=1)))
    assert ("use_clock", "Decades") in station.calls
    assert manager.doc.active.resume == station.picking_now
    run(manager.stop())
    assert station.calls[-2] == ("restore", "similarity", ClockState(name="Station", cursor=7))


def test_a_show_without_a_clockwheel_leaves_the_station_clock_alone(manager, station) -> None:
    show = add(manager)
    run(manager.start(show.id, START + timedelta(hours=1)))
    run(manager.stop())
    assert not [call for call in station.calls if call[0] in ("use_clock", "restore")]


def test_takeover_keeps_the_station_picking_from_before_the_first_show(manager, station) -> None:
    first = add(manager, clockwheel="Decades")
    second = add(manager, name="Same clock", year_from=1990, clockwheel="Decades")
    third = add(manager, name="No clock", year_from=1995)
    run(manager.start(first.id, START + timedelta(hours=1)))
    station.picking_now = Resume(pick_mode="clock", transition_mode="saved_points")
    run(manager.start(second.id, START + timedelta(hours=1)))
    # The same clockwheel keeps playing at its place.
    assert station.calls.count(("use_clock", "Decades")) == 1
    assert manager.doc.active.resume.pick_mode == "similarity"
    run(manager.start(third.id, START + timedelta(hours=1)))
    assert [call[0] for call in station.calls[-3:]] == ["restore", "theme", "started"]
    assert station.calls[-3][1] == "similarity"
    assert manager.doc.active.resume is None


def test_a_clockwheel_that_cannot_play_stops_the_start(manager, station) -> None:
    show = add(manager, clockwheel="Decades")
    station.clock_fails = RuntimeError("Step 1 is broken")
    with pytest.raises(ShowError, match="Step 1 is broken") as caught:
        run(manager.start(show.id, START + timedelta(hours=1)))
    assert caught.value.field == "clockwheel"
    station.clock_fails = ShowError("clockwheel", "The clockwheel cannot play: off")
    with pytest.raises(ShowError, match="cannot play"):
        run(manager.start(show.id, START + timedelta(hours=1)))
    assert manager.state() is None


def test_a_failed_restore_is_logged(manager, station, caplog) -> None:
    async def broken(_resume) -> None:
        raise RuntimeError("no")

    manager.hooks.restore = broken
    show = add(manager, clockwheel="Decades")
    run(manager.start(show.id, START + timedelta(hours=1)))
    run(manager.stop())
    assert "Could not bring back the song picking" in caplog.text
    assert manager.state() is None


# ---------------------------------------------------------------------------
# Editing and deleting the show on air
# ---------------------------------------------------------------------------


def test_editing_the_show_on_air_applies_at_once(manager, station) -> None:
    show = add(manager)
    run(manager.start(show.id, START + timedelta(hours=1)))
    before = station.theme.run_id
    body = {"name": "Late nineties", "year_from": 1995, "year_to": 1999, "clockwheel": "Decades"}
    run(manager.replace(show.id, parse_spec(body)))
    assert station.theme.run_id != before
    assert station.theme.paths == {entry.path for entry in SONGS[5:10]}
    assert manager.state()["name"] == "Late nineties"
    assert ("changed",) in station.calls  # the public feed names the show again
    assert manager.state()["ends_at"] == "2026-10-11T19:00:00+00:00"
    assert ("use_clock", "Decades") in station.calls
    run(manager.replace(show.id, parse_spec({**body, "clockwheel": None})))
    assert [call[0] for call in station.calls[-3:]] == ["restore", "theme", "changed"]
    with pytest.raises(ShowError, match="is on air"):
        run(manager.replace(show.id, parse_spec({"name": "Empty", "year_from": 2050})))
    with pytest.raises(KeyError):
        run(manager.replace("f" * 32, parse_spec(body)))


def test_editing_a_show_that_is_not_on_air_leaves_the_player_alone(manager, station) -> None:
    show = add(manager)
    run(manager.replace(show.id, parse_spec({"name": "Renamed", "year_from": 2050})))
    assert station.theme is None
    assert manager.get(show.id).name == "Renamed"


def test_deleting_the_show_on_air_ends_it(manager, station) -> None:
    show = add(manager, outro=OUTRO)
    other = add(manager, name="Other")
    run(manager.start(show.id, START + timedelta(hours=1)))
    run(manager.remove(show.id))
    assert manager.state() is None and station.theme is None
    assert [item.id for item in manager.doc.shows] == [other.id]
    with pytest.raises(KeyError):
        run(manager.remove(show.id))


# ---------------------------------------------------------------------------
# Restarts
# ---------------------------------------------------------------------------


def test_a_restart_carries_on_with_the_show_on_air(tmp_path, station, clock) -> None:
    first = ShowManager(tmp_path / "shows.json", "UTC", station.hooks(), clock=clock)
    show = add(first, clockwheel="Decades")
    run(first.start(show.id, START + timedelta(hours=1)))
    station.calls.clear()
    station.theme = None
    clock.now = START + timedelta(minutes=10)
    second = ShowManager(tmp_path / "shows.json", "UTC", station.hooks(), clock=clock)
    run(second.start_up())
    assert station.theme is not None and second.state()["name"] == show.name
    # The station already plays the show's clockwheel, at its place.
    assert ("use_clock", "Decades") not in station.calls
    assert second.due_audio() is None  # no intro again


def test_a_show_that_ended_while_down_ends_quietly(tmp_path, station, clock) -> None:
    first = ShowManager(tmp_path / "shows.json", "UTC", station.hooks(), clock=clock)
    show = add(first, clockwheel="Decades", outro=OUTRO)
    run(first.start(show.id, START + timedelta(hours=1)))
    clock.now = START + timedelta(hours=2)
    second = ShowManager(tmp_path / "shows.json", "UTC", station.hooks(), clock=clock)
    run(second.start_up())
    assert second.state() is None
    assert station.calls[-1][0] == "restore"
    assert second.due_audio() is None
    saved = json.loads((tmp_path / "shows.json").read_text(encoding="utf-8"))
    assert saved["active"] is None


def test_a_show_that_can_no_longer_match_ends_at_restart(tmp_path, station, clock) -> None:
    first = ShowManager(tmp_path / "shows.json", "UTC", station.hooks(), clock=clock)
    show = add(first)
    run(first.start(show.id, START + timedelta(hours=1)))
    second = ShowManager(tmp_path / "shows.json", "UTC", station.hooks(), clock=clock)
    second.hooks.entries = lambda: []
    run(second.start_up())
    assert second.state() is None


def test_start_up_with_no_file_and_no_show_on_air(manager, station) -> None:
    run(manager.start_up())
    assert manager.state() is None and station.calls == []


# ---------------------------------------------------------------------------
# Liners and helpers
# ---------------------------------------------------------------------------


def test_the_show_liner_folder_replaces_the_automatic_liners(manager, station) -> None:
    every = [(key, normalize_path(path)) for key, path in station.liner_files]
    plain = add(manager, name="Plain")
    assert manager.liners_for_now(every) == every  # no show has a folder
    show = add(manager, liner_folder={"source": "src", "folder": "shows/2000s"})
    # Outside the show, its folder never plays.
    assert [key for key, _path in manager.liners_for_now(every)] == [INTRO, OUTRO]
    run(manager.start(show.id, START + timedelta(hours=1)))
    assert [key for key, _path in manager.liners_for_now(every)] == [OTHER]
    run(manager.start(plain.id, START + timedelta(hours=1)))
    assert [key for key, _path in manager.liners_for_now(every)] == [INTRO, OUTRO]


def test_inside_compares_whole_folder_names() -> None:
    native = os.sep.join(["", "liners", "shows"])
    assert _inside(native + os.sep + "a.mp3", native)
    assert _inside("/liners/shows/a.mp3", "/liners/shows/")
    assert _inside("/liners/shows", "/liners/shows")
    assert not _inside("/liners/showsx/a.mp3", "/liners/shows")


def test_end_at_wall_time_is_the_next_time_the_clock_reads_it() -> None:
    zone = ZoneInfo("America/Denver")
    now = datetime(2026, 10, 11, 20, 0, tzinfo=zone)
    assert end_at_wall_time("21:30", now, zone) == datetime(2026, 10, 11, 21, 30, tzinfo=zone)
    # Past the time today: tomorrow.
    assert end_at_wall_time("02:00", now, zone) == datetime(2026, 10, 12, 2, 0, tzinfo=zone)
    with pytest.raises(ShowError, match="the minute it starts"):
        end_at_wall_time("20:00", now, zone)


def test_the_liner_name_for_a_gone_liner(manager) -> None:
    assert manager._liner_name(INTRO) == "intro.mp3"
    assert manager._liner_name("f" * 32) == "a liner that is no longer registered"


def test_state_reads_times_in_the_station_zone(tmp_path, station, clock) -> None:
    manager = ShowManager(tmp_path / "shows.json", "America/Denver", station.hooks(), clock=clock)
    show = add(manager)
    run(manager.start(show.id, START + timedelta(hours=3)))
    assert manager.state()["ends_at"] == "2026-10-11T15:00:00-06:00"
    assert SimpleNamespace(**manager.public_state()).ends_at[11:16] == "15:00"
