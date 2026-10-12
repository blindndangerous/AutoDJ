"""Themed show models, the shows file, the filter and the pool-size check (autodj.shows)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from autodj.catalog import normalize_path
from autodj.repeat_rules import RepeatRules
from autodj.shows import (
    BeetsSource,
    Show,
    ShowError,
    ShowsFile,
    ShowSpec,
    ShowStore,
    TrackFilter,
    count_text,
    describe,
    fallback_text,
    library_genres,
    match_library,
    parse_spec,
    pool_warning,
    split_genres,
)
from tests.unit._fakes import make_entry

ID = "a" * 32


def song(n: int, *, artist: str = "", album: str = "", genre: str = "Rock", year: int = 2000):
    entry = make_entry(n)
    entry.path = normalize_path(f"Z:/Music/song_{n}.flac")
    entry.title = f"Song {n}"
    entry.artist = artist or f"Artist {n}"
    entry.album = album or f"Album {n}"
    entry.genre = genre
    entry.year = year
    entry.length = 200.0
    return entry


# ---------------------------------------------------------------------------
# The spec
# ---------------------------------------------------------------------------


def test_a_minimal_show_has_soft_timing_and_an_hour() -> None:
    spec = parse_spec({"name": "  Remembering the 2000s  "})
    assert spec.name == "Remembering the 2000s"
    assert (spec.start, spec.end, spec.default_minutes) == ("soft", "soft", 60)
    assert spec.track_filter() == TrackFilter()


@pytest.mark.parametrize(
    ("body", "field", "words"),
    [
        ({"name": ""}, "name", "Enter a name"),
        ({"name": "x" * 101}, "name", "Enter a name"),
        ({"name": "A", "year_from": 2010, "year_to": 2000}, "year_to", "ends before it starts"),
        ({"name": "A", "year_from": 0}, "year_from", "from 1 to 9999"),
        ({"name": "A", "year_to": "2000"}, "year_to", "from 1 to 9999"),
        ({"name": "A", "default_minutes": 0}, "default_minutes", "1 to 1440"),
        ({"name": "A", "default_minutes": 1441}, "default_minutes", "1 to 1440"),
        ({"name": "A", "query": "genre::x"}, "query", "regular expressions"),
        ({"name": "A", "clockwheel": "../x"}, "clockwheel", "clockwheel name"),
        ({"name": "A", "intro": "/etc/passwd"}, "intro", "voice liners"),
        ({"name": "A", "outro": "C:/x.mp3"}, "outro", "voice liners"),
        (
            {"name": "A", "liner_folder": {"source": "s1", "folder": "../up"}},
            "liner_folder",
            "'..'",
        ),
        ({"name": "A", "liner_folder": {"source": "../s"}}, "liner_folder", "Show liner folder"),
        ({"name": "A", "start": "now"}, "start", "Start"),
        ({"name": "A", "path": "/x"}, "path", "Unknown field path"),
        (
            {"name": "A", "genres_include": ["Rock"], "genres_exclude": ["rock"]},
            "genres_exclude",
            "both included and left out",
        ),
    ],
)
def test_bad_shows_are_refused_naming_the_field(body, field, words) -> None:
    with pytest.raises(ShowError) as caught:
        parse_spec(body)
    assert caught.value.field == field
    assert words in str(caught.value)


def test_genres_lose_case_duplicates_and_keep_the_first_spelling() -> None:
    spec = parse_spec({"name": "A", "genres_include": ["Rock", " rock ", "Pop"]})
    assert spec.genres_include == ["Rock", "Pop"]
    assert spec.track_filter().genres_include == frozenset({"rock", "pop"})


def test_a_liner_folder_is_a_source_and_a_folder_inside_it() -> None:
    spec = parse_spec({"name": "A", "liner_folder": {"source": "s1", "folder": "shows/2000s"}})
    assert spec.liner_folder is not None
    assert (spec.liner_folder.source, spec.liner_folder.folder) == ("s1", "shows/2000s")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Rock", ["Rock"]),
        ("Rock, Pop;Jazz", ["Rock", "Pop", "Jazz"]),
        ("Rock\0Pop", ["Rock", "Pop"]),
        (b"Rock", ["Rock"]),
        ("", []),
        (None, []),
    ],
)
def test_split_genres(value, expected) -> None:
    assert split_genres(value) == expected


def test_library_genres_are_sorted_and_unique() -> None:
    entries = [song(1, genre="rock, Pop"), song(2, genre="Rock"), song(3, genre="")]
    assert library_genres(entries) == ["Pop", "rock"]


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


def test_the_store_round_trips_and_a_missing_file_is_empty(tmp_path: Path) -> None:
    store = ShowStore(tmp_path / "shows.json")
    assert store.load().shows == []
    show = Show(**parse_spec({"name": "A"}).model_dump(), id=ID)
    store.save(ShowsFile(schema_version=1, shows=[show]))
    assert store.load().shows == [show]
    assert b"\r\n" not in (tmp_path / "shows.json").read_bytes()


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"schema_version": 2, "shows": []}),
        json.dumps(
            {
                "schema_version": 1,
                "shows": [{"name": "A", "id": ID}, {"name": "B", "id": ID}],
            }
        ),
        json.dumps(
            {
                "schema_version": 1,
                "shows": [{"name": "A", "id": ID}, {"name": "a", "id": "b" * 32}],
            }
        ),
    ],
)
def test_a_bad_file_is_set_aside(tmp_path: Path, content: str, caplog) -> None:
    path = tmp_path / "shows.json"
    path.write_text(content, encoding="utf-8")
    assert ShowStore(path).load().shows == []
    assert not path.exists()
    assert [p.name.startswith("shows.json.bad-") for p in tmp_path.iterdir()] == [True]
    assert "set it aside" in caplog.text


def test_an_unreadable_file_is_logged_and_left_alone(tmp_path: Path, caplog) -> None:
    (tmp_path / "shows.json").mkdir()
    assert ShowStore(tmp_path / "shows.json").load().shows == []
    assert "Could not read the shows file" in caplog.text


def test_a_file_that_cannot_be_set_aside_is_logged(tmp_path: Path, monkeypatch, caplog) -> None:
    import autodj.shows as shows

    path = tmp_path / "shows.json"
    path.write_text("bad", encoding="utf-8")

    def refuse(*_args):
        raise OSError("busy")

    monkeypatch.setattr(shows.os, "replace", refuse)
    assert ShowStore(path).load().shows == []
    assert "Could not set aside" in caplog.text


# ---------------------------------------------------------------------------
# The filter
# ---------------------------------------------------------------------------


def evaluate(spec: dict, item: dict) -> tuple[bool, bool]:
    return parse_spec({"name": "A", **spec}).track_filter().evaluate(item)


def test_the_year_range_reads_the_first_release_year() -> None:
    decade = {"year_from": 2000, "year_to": 2009}
    # A 1985 song on a 2005 compilation is not from the 2000s.
    assert evaluate(decade, {"original_year": 1985, "year": 2005}) == (False, False)
    assert evaluate(decade, {"original_year": 2003, "year": 2015}) == (True, False)
    # Without a first-release year the release year stands in, and says so.
    assert evaluate(decade, {"original_year": 0, "year": 2005}) == (True, True)
    # With neither, the song is left out of year filters.
    assert evaluate(decade, {"original_year": 0, "year": 0}) == (False, False)
    assert evaluate({"year_from": 2000}, {"year": 1999}) == (False, False)
    assert evaluate({"year_to": 2000}, {"year": 2001}) == (False, False)


def test_a_fallback_only_counts_when_the_filter_reads_the_year() -> None:
    assert evaluate({"query": "genre:rock"}, {"genre": "Rock", "year": 2005}) == (True, False)
    assert evaluate({"query": "original_year:2005"}, {"year": 2005}) == (True, True)


def test_genres_to_include_and_leave_out() -> None:
    spec = {"genres_include": ["Rock", "Pop"], "genres_exclude": ["Christmas"]}
    assert evaluate(spec, {"genre": "rock"})[0]
    assert evaluate(spec, {"genre": "Jazz, Pop"})[0]
    assert not evaluate(spec, {"genre": "Jazz"})[0]
    assert not evaluate(spec, {"genre": "Rock; Christmas"})[0]
    assert not evaluate(spec, {})[0]
    assert evaluate({"genres_exclude": ["Christmas"]}, {})[0]


def test_the_form_and_the_query_must_both_match() -> None:
    spec = {"year_from": 1990, "year_to": 1999, "query": "^artist:jovi"}
    assert evaluate(spec, {"year": 1995, "artist": "Pearl Jam"})[0]
    assert not evaluate(spec, {"year": 1995, "artist": "Bon Jovi"})[0]


def test_filter_fields_name_what_beets_must_read() -> None:
    track_filter = parse_spec(
        {"name": "A", "year_from": 2000, "genres_include": ["Rock"], "query": "mood:happy"}
    ).track_filter()
    assert track_filter.fields() == {"original_year", "year", "genre", "mood"}
    assert TrackFilter().fields() == frozenset()


# ---------------------------------------------------------------------------
# Matching the library, with and without beets
# ---------------------------------------------------------------------------


def _beets_db(path: Path, rows: list[tuple]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB, genre TEXT, year INTEGER, "
        "original_year INTEGER)"
    )
    conn.execute(
        "CREATE TABLE item_attributes (id INTEGER PRIMARY KEY, entity_id INTEGER, key TEXT, "
        "value TEXT)"
    )
    for number, (item_path, genre, year, original, mood) in enumerate(rows, 1):
        conn.execute(
            "INSERT INTO items (id, path, genre, year, original_year) VALUES (?, ?, ?, ?, ?)",
            (number, item_path.encode(), genre, year, original),
        )
        if mood:
            conn.execute(
                "INSERT INTO item_attributes (entity_id, key, value) VALUES (?, 'mood', ?)",
                (number, mood),
            )
    conn.commit()
    conn.close()
    return path


def test_without_beets_the_library_year_is_the_fallback() -> None:
    entries = [song(1, year=2001), song(2, year=1999), song(3, year=0)]
    entries.append(replace(song(4, year=2005), energy=0.0))  # silent
    result = match_library(parse_spec({"name": "A", "year_from": 2000}), entries, None)
    assert result.paths == {entries[0].path}
    assert result.count == 1
    assert [entry.path for entry in result.fallback] == [entries[0].path]
    assert not result.beets
    assert result.matched == (entries[0],)


def test_beets_values_are_laid_over_the_library(tmp_path: Path) -> None:
    music = tmp_path / "music"
    entries = [song(n) for n in range(1, 4)]
    entries[0].path = normalize_path(music / "a" / "one.flac")
    entries[1].path = normalize_path(music / "two.flac")
    db = _beets_db(
        tmp_path / "library.db",
        [
            ("a/one.flac", "Rock", 2005, 1985, "happy"),  # relative to music_dir
            (str(music / "two.flac"), "Pop", 2003, 0, "happy"),  # absolute
        ],
    )
    beets = BeetsSource(db, music)
    eighties = match_library(
        parse_spec({"name": "A", "query": "original_year:1980..1989"}), entries, beets
    )
    assert eighties.paths == {entries[0].path}
    assert eighties.beets and not eighties.fallback
    happy = match_library(parse_spec({"name": "A", "query": "mood:happy"}), entries, beets)
    assert happy.paths == {entries[0].path, entries[1].path}
    noughties = match_library(
        parse_spec({"name": "A", "year_from": 2000, "year_to": 2009}), entries, beets
    )
    # Song 2 has no first-release year in beets; song 3 is not in beets at all.
    assert noughties.paths == {entries[1].path, entries[2].path}
    assert len(noughties.fallback) == 2


def test_a_field_no_song_has_is_an_error(tmp_path: Path) -> None:
    result = match_library(parse_spec({"name": "A", "query": "mood:happy"}), [song(1)], None)
    assert result.errors == (
        (
            "query",
            "No song in the library has a field named mood. Check the spelling; beets fields "
            "look like original_year or albumartist",
        ),
    )
    assert result.count == 0
    db = _beets_db(tmp_path / "library.db", [])
    beets = match_library(
        parse_spec({"name": "A", "query": "moood:happy"}), [song(1)], BeetsSource(db)
    )
    assert "the library or the beets database" in beets.errors[0][1]


def test_an_unreadable_beets_database_is_an_error(tmp_path: Path) -> None:
    missing = BeetsSource(tmp_path / "missing.db")
    result = match_library(parse_spec({"name": "A", "query": "genre:rock"}), [song(1)], missing)
    assert result.errors[0][0] == "query"
    assert "could not be read" in result.errors[0][1]
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database at all, not even close" * 10)
    result = match_library(parse_spec({"name": "A"}), [song(1)], BeetsSource(broken))
    assert "could not be read" in result.errors[0][1]


def test_count_and_fallback_words() -> None:
    assert count_text(1) == "1 song matches"
    assert count_text(1284) == "1,284 songs match"
    assert fallback_text(0) == ""
    assert fallback_text(1) == ("1 uses the release year because the first-release year is missing")
    assert fallback_text(96) == (
        "96 use the release year because the first-release year is missing"
    )


# ---------------------------------------------------------------------------
# The pool-size check
# ---------------------------------------------------------------------------


def rules(**values) -> RepeatRules:
    base = {"track_window": 0, "artist_window": 0, "album_window": 0, "title_window": 0}
    return RepeatRules(**{**base, **values})


def test_a_big_enough_pool_has_no_warning() -> None:
    songs = [song(n) for n in range(40)]
    assert pool_warning(songs, rules(track_window=20, artist_window=8), 60) is None


def test_the_track_rule_needs_as_many_songs_as_the_show_plays() -> None:
    songs = [song(n) for n in range(10)]
    # 60 minutes of 200-second songs is 18 songs; each of 10 may play once.
    assert pool_warning(songs, rules(track_window=50), 60) == (
        "about 18 songs needed; only 10 fit your track rule"
    )
    # A short track window lets songs come back, so the pool is enough.
    assert pool_warning(songs, rules(track_window=5), 60) is None


def test_the_artist_rule_counts_artists() -> None:
    songs = [song(n, artist=f"Artist {n % 7}") for n in range(40)]
    assert pool_warning(songs, rules(artist_window=8), 120) == (
        "about 36 songs needed; only 7 fit your artist rule"
    )
    # Minutes count too: 30 minutes of 200-second songs is 9 songs.
    assert pool_warning(songs, rules(artist_minutes=30), 120) == (
        "about 36 songs needed; only 7 fit your artist rule"
    )


def test_the_tightest_rule_is_named() -> None:
    songs = [song(n, artist=f"Artist {n % 7}", album=f"Album {n % 3}") for n in range(40)]
    warning = pool_warning(songs, rules(artist_window=8, album_window=8), 120)
    assert warning == "about 36 songs needed; only 3 fit your album rule"


def test_songs_without_an_album_or_title_are_never_held_back_by_those_rules() -> None:
    songs = [song(n, album="x") for n in range(20)]
    for entry in songs:
        entry.album = ""
        entry.title = ""
    assert pool_warning(songs, rules(album_window=50, title_window=50), 30) is None


def test_dmca_limits_cap_three_hours_of_the_show() -> None:
    songs = [song(n, artist=f"Artist {n % 3}", album=f"Album {n}") for n in range(30)]
    # 3 artists, 4 songs each in three hours: 12 fit, 18 are needed for an hour.
    assert pool_warning(songs, rules(), 60, dmca=True) == (
        "about 18 songs needed; only 12 fit the DMCA limits"
    )
    assert pool_warning(songs, rules(), 60) is None


def test_no_matching_song_is_a_warning_too() -> None:
    assert pool_warning([], rules(), 60) == ("about 18 songs needed; no song matches the show")


def test_songs_of_unknown_length_plan_with_three_and_a_half_minutes() -> None:
    songs = [song(n) for n in range(5)]
    for entry in songs:
        entry.length = 0
    assert pool_warning(songs, rules(track_window=50), 60) == (
        "about 18 songs needed; only 5 fit your track rule"
    )


# ---------------------------------------------------------------------------
# Words for the list
# ---------------------------------------------------------------------------


def test_describe_says_what_a_show_plays() -> None:
    spec = ShowSpec(
        name="Remembering the 2000s",
        year_from=2000,
        year_to=2009,
        genres_include=["Rock", "Pop", "Indie"],
        genres_exclude=["Christmas"],
        query="^artist:jovi",
        clockwheel="Hour",
        intro=ID,
        outro="b" * 32,
        liner_folder={"source": "s1", "folder": ""},
        start="hard",
        default_minutes=120,
    )
    names = {ID: "intro.mp3", "b" * 32: "outro.mp3"}
    assert describe(spec, names.__getitem__) == (
        "First released 2000 to 2009. Genres Rock, Pop and Indie. Leaves out Christmas. "
        "Beets query ^artist:jovi. Clockwheel Hour. Intro intro.mp3. Outro outro.mp3. "
        "Its own liner folder. Hard start, soft end. 120 minutes when started now."
    )
    assert describe(ShowSpec(name="All")) == (
        "Every song. Soft start, soft end. 60 minutes when started now."
    )
    assert describe(ShowSpec(name="A", year_from=1990)).startswith(
        "First released in 1990 or later."
    )
    assert describe(ShowSpec(name="A", year_to=1990)).startswith(
        "First released in 1990 or earlier."
    )
    assert describe(ShowSpec(name="A", intro=ID)).count(f"Intro {ID}") == 1
