"""Count versions as one song (#177): a song's files share its odds and its track rule."""

from __future__ import annotations

import random
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autodj.clockwheel import ClockSlot, least_recent, pick_slot
from autodj.repeat_rules import RepeatRules, SongVersions, song_key
from autodj.repeat_settings import RepeatSettings
from autodj.rotation_sim import SimulationRequest, report_text, simulate
from autodj.similarity import _one_per_song
from tests.unit._rotation_station import category_step, make_station, song
from tests.unit.test_repeat_pick_modes import _library, _player


def _entry(path: str, artist: str, title: str) -> SimpleNamespace:
    return SimpleNamespace(path=path, artist=artist, title=title, album="")


# Song "a" has three versions, "b" and "c" one each.
LIBRARY = [
    _entry("a1", "Mariah Carey", "Fantasy"),
    _entry("a2", "Mariah Carey feat. ODB", "Fantasy (Remix)"),
    _entry("a3", "Mariah Carey", "Fantasy (Ft ODB)"),
    _entry("b", "Brandy", "The Boy Is Mine"),
    _entry("c", "Ariana Grande", "The Boy Is Mine"),
]


class TestSongKey:
    @pytest.mark.parametrize(
        ("artist", "title"),
        [
            ("Mariah Carey", "Fantasy"),
            ("Mariah Carey feat. ODB", "Fantasy (Remix)"),
            ("Mariah Carey (feat. ODB)", "Fantasy - Radio Edit"),
            ("MARIAH CAREY", "fantasy (w/ ODB)"),
        ],
    )
    def test_versions_by_the_same_lead_artist_are_one_song(self, artist, title) -> None:
        assert song_key(artist, title) == ("mariah carey", "fantasy")

    def test_another_lead_artist_is_another_song(self) -> None:
        assert song_key("Brandy", "The Boy Is Mine") != song_key("Ariana Grande", "The Boy Is Mine")

    @pytest.mark.parametrize(
        ("artist", "title"), [("", "Fantasy"), ("Mariah Carey", ""), (None, None)]
    )
    def test_a_file_without_artist_or_title_has_no_song(self, artist, title) -> None:
        assert song_key(artist, title) is None


class TestSongVersions:
    def test_versions_are_grouped_and_single_files_stand_alone(self) -> None:
        versions = SongVersions(LIBRARY)
        assert versions.versions("a2") == ("a1", "a2", "a3")
        assert versions.versions("b") == ("b",)
        assert versions.song("a3") == versions.song("a1") == "a1"
        assert versions.song("c") == "c"

    def test_no_song_with_two_versions_is_falsy(self) -> None:
        assert not SongVersions(LIBRARY[3:])
        assert SongVersions(LIBRARY)

    def test_with_versions_adds_each_songs_other_files_once(self) -> None:
        versions = SongVersions(LIBRARY)
        assert versions.with_versions(["b", "a2", "a1"]) == ["b", "a1", "a2", "a3"]

    def test_a_song_is_as_old_as_its_latest_play(self) -> None:
        played = {"a1": 50.0, "a3": 10.0}.get
        song_played = SongVersions(LIBRARY).last_played(played)
        assert song_played("a2") == 50.0
        assert song_played("b") is None

    def test_choice_gives_each_song_the_same_chance(self) -> None:
        versions = SongVersions(LIBRARY)
        rng = random.Random(4)
        picks = Counter(versions.song(versions.choice(LIBRARY, rng).path) for _ in range(6000))
        # Three songs, so about 2000 each; without grouping "a" would get 3600.
        assert all(1800 < count < 2200 for count in picks.values()), picks
        assert set(picks) == {"a1", "b", "c"}

    def test_choice_without_versions_is_a_plain_choice(self) -> None:
        versions = SongVersions(LIBRARY[3:])
        assert versions.choice(LIBRARY[3:], random.Random(1)) is random.Random(1).choice(
            LIBRARY[3:]
        )


class TestLeastRecent:
    def test_a_song_counts_as_played_when_any_version_did(self) -> None:
        # a1 played most recently; a2 never played; b played long ago.
        history = {"a1": 300.0, "b": 100.0, "c": 200.0}
        versions = SongVersions(LIBRARY)
        assert least_recent(LIBRARY, history, random.Random(0)).path in {"a2", "a3"}
        assert least_recent(LIBRARY, history, random.Random(0), versions=versions).path == "b"

    def test_the_oldest_version_of_a_song_plays_next(self) -> None:
        history = {"a1": 300.0, "a2": 100.0, "a3": 200.0, "b": 400.0, "c": 500.0}
        picked = least_recent(
            LIBRARY, history, random.Random(0), versions=SongVersions(LIBRARY), allowed=bool
        )
        assert picked.path == "a2"

    def test_versions_take_turns(self) -> None:
        versions = SongVersions(LIBRARY)
        history: dict[str, float] = {}
        order = []
        for now in range(9):
            picked = least_recent(LIBRARY, history, random.Random(now), versions=versions)
            history[picked.path] = float(now)
            order.append(picked.path)
        # Each pass plays every song once, and song "a" plays a different version each time.
        assert [versions.song(path) for path in order[:3]].count("a1") == 1
        assert {path for path in order if versions.song(path) == "a1"} == {"a1", "a2", "a3"}


def test_a_random_clock_step_gives_each_song_the_same_chance() -> None:
    entries = [_entry(f"/music/{e.path}.flac", e.artist, e.title) for e in LIBRARY]
    versions = SongVersions(entries)
    slot = ClockSlot("/music", selection="random")
    rng = random.Random(9)
    picks = Counter(
        versions.song(pick_slot([slot], 0, entries, {}, bool, rng, versions=versions).entry.path)
        for _ in range(3000)
    )
    assert all(900 < count < 1100 for count in picks.values()), picks


def test_similarity_keeps_the_best_version_of_each_song() -> None:
    versions = SongVersions(LIBRARY)
    scored = [(0.9, LIBRARY[1]), (0.8, LIBRARY[3]), (0.7, LIBRARY[0]), (0.6, LIBRARY[4])]
    assert _one_per_song(scored, versions.song) == [scored[0], scored[1], scored[3]]
    assert _one_per_song(scored, None) is scored


# ---------------------------------------------------------------------------
# The player
# ---------------------------------------------------------------------------

TRACKS = [
    ("Mariah Carey", "Fantasy"),
    ("Mariah Carey feat. ODB", "Fantasy (Remix)"),
    ("Brandy", "Sittin' Up In My Room"),
    ("Monica", "Before You Walk Out"),
]


def _versions_player(mode: str, on: bool, track: int = 1):
    rules = {
        "track": {"tracks": track},
        "artist": {"tracks": 0},
        "album": {"tracks": 0},
        "title": {"tracks": 0},
        "use_related_artists": False,
        "versions_as_one_song": on,
    }
    player = _player(mode, TRACKS, repeat_rules=rules)
    player._random = random.Random(3)
    if mode == "least_recent":
        player._library = _library(player)
    return player


def test_the_setting_reaches_the_rules() -> None:
    settings = RepeatSettings.model_validate({"versions_as_one_song": True})
    playback = SimpleNamespace(repeat_rules=settings)
    assert RepeatRules.from_playback(playback).versions_as_one_song is True
    assert RepeatSettings().versions_as_one_song is False


@pytest.mark.parametrize("on", [False, True])
def test_the_track_rule_holds_back_every_version_only_with_the_setting(on: bool) -> None:
    player = _versions_player("random", on)
    first = player.playable_entries()[0]
    with player._pick_lock:
        player._state.record_played(first)
    recent = set(player._pick_context().recent)
    remix = player.playable_entries()[1].path
    assert first.path in recent
    assert (remix in recent) is on


@pytest.mark.parametrize("on", [False, True])
def test_random_picks_give_a_song_with_two_versions_its_fair_share(on: bool) -> None:
    player = _versions_player("random", on, track=0)
    current = player.playable_entries()[2]
    fantasy = {entry.path for entry in player.playable_entries()[:2]}
    picks = [player._pick_pure_shuffle(current, player._pick_context()) for _ in range(3000)]
    share = sum(entry.path in fantasy for entry in picks) / len(picks)
    # Brandy is playing, so three files in two songs are left: two thirds of
    # the picks by file, half by song.
    assert share == pytest.approx(1 / 2 if on else 2 / 3, abs=0.04)


def test_least_recent_treats_the_song_as_played_with_the_setting() -> None:
    player = _versions_player("least_recent", True)
    entries = player.playable_entries()
    stamps = {entries[0].path: 400.0, entries[2].path: 100.0, entries[3].path: 200.0}
    player._library.ledger.last_played = lambda: stamps
    player._ledger = player._library.ledger
    picked = player._pick_pure_shuffle(entries[3], player._pick_context())
    # The remix never played, but its song did most recently; Brandy is the oldest song.
    assert picked.path == entries[2].path


def test_similarity_passes_the_song_names_only_with_the_setting() -> None:
    seen: list[Any] = []
    for on in (False, True):
        player = _versions_player("similarity", on)

        def find(recently_played, player=player, **search):
            seen.append(search.get("song_of"))
            return player.playable_entries()[2]

        player._sim.find_next_for_path = find  # type: ignore[method-assign]
        current = player.playable_entries()[3]
        player._choose_by_mode(current, player._pick_context())
    assert seen[0] is None
    assert seen[1](player.playable_entries()[1].path) == player.playable_entries()[0].path


# ---------------------------------------------------------------------------
# The rotation tester
# ---------------------------------------------------------------------------


def _station(tmp_path: Path, on: bool):
    music = [
        song(1, "Mariah Carey", title="Fantasy"),
        song(2, "Mariah Carey feat. ODB", title="Fantasy (Remix)"),
        song(3, "Mariah Carey", title="Fantasy (Radio Edit)"),
        song(4, "Brandy", title="Sittin' Up In My Room"),
        song(5, "Monica", title="Before You Walk Out"),
        song(6, "Aaliyah", title="Back & Forth"),
    ]
    step = {**category_step("all"), "selection": "random"}
    return make_station(
        tmp_path,
        music,
        categories=[{"id": "all", "name": "All", "members": music}],
        clockwheels=[{"name": "Only", "steps": [step]}],
        active_clock="Only",
        playback={
            "pick_mode": "clock",
            "repeat_rules": {
                "track": {"tracks": 1},
                "artist": {"tracks": 0},
                "versions_as_one_song": on,
            },
        },
    )


@pytest.mark.parametrize("on", [False, True])
def test_the_rotation_tester_counts_songs_and_their_share(tmp_path: Path, on: bool) -> None:
    start = 1_790_000_000.0
    report = simulate(_station(tmp_path, on), SimulationRequest(picks=800, start=start, seed=8))
    songs = report["songs"]
    assert report["song_picking"]["versions_as_one_song"] is on
    assert songs["library"] == (4 if on else 6)
    fantasy = next(item for item in songs["most_played"] if "Fantasy" in item["song"])
    if on:
        # Four songs share 800 picks, the three Fantasy files among them as one song.
        assert fantasy["plays"] == pytest.approx(200, abs=40)
        assert "Versions of a song count as one song." in report_text(report)
    else:
        assert fantasy["plays"] == pytest.approx(800 / 6, abs=40)
    # The track rule never let a version follow another version of its song.
    assert "track repeat window" not in report["relaxed"]


# ---------------------------------------------------------------------------
# Why this track
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("on", "index", "note"), [(True, 1, True), (True, 2, False), (False, 1, False)]
)
def test_why_this_track_names_a_version_only_with_the_setting(on, index, note) -> None:
    from autodj._bridge import _build_why

    player = _versions_player("random", on)
    entry = player.playable_entries()[index]
    reasons = _build_why(player, entry)
    expected = "One of 2 versions of this song, which count as one song."
    assert (expected in reasons) is note
