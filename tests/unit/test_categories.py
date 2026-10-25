"""Rotation categories: the saved file, M3U matching, beets queries and the live cache."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from autodj.categories import (
    Category,
    CategoryError,
    CategoryIndex,
    CategoryStore,
    list_playlists,
    parse_spec,
    playlist_file,
    read_playlist,
    resolve_all,
    summary,
)
from tests._layout import layout_index


def song(path: Path | str, **fields):
    values = {
        "title": Path(path).stem,
        "artist": "Artist",
        "album": "Album",
        "genre": "",
        "year": 0,
        "bpm": 120.0,
        "length": 200.0,
        "is_silent": False,
    }
    values.update(fields)
    return SimpleNamespace(path=str(path), **values)


def config(tmp_path: Path, *, beets_db: Path | None = None, playlists: Path | None = None):
    return SimpleNamespace(
        index=layout_index(tmp_path / "index"),
        library=SimpleNamespace(
            beets_db=beets_db, music_dir=tmp_path / "music", playlists_folder=playlists
        ),
    )


def make_beets(path: Path, rows, attributes=()) -> Path:
    """A beets database: rows of (id, path, title, genre, year, original_year)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB, title TEXT, artist TEXT, "
        "genre TEXT, year INTEGER, original_year INTEGER)"
    )
    conn.executemany(
        "INSERT INTO items (id, path, title, artist, genre, year, original_year) "
        "VALUES (?, ?, ?, 'Artist', ?, ?, ?)",
        rows,
    )
    conn.execute(
        "CREATE TABLE item_attributes (id INTEGER PRIMARY KEY, entity_id INTEGER, key TEXT, "
        "value TEXT)"
    )
    conn.executemany(
        "INSERT INTO item_attributes (entity_id, key, value) VALUES (?, ?, ?)", attributes
    )
    conn.commit()
    conn.close()
    return path


def query(name: str, text: str, category_id: str = "c1") -> Category:
    return Category(id=category_id, name=name, source="query", query=text)


def playlist(name: str, file: str, category_id: str = "p1") -> Category:
    return Category(id=category_id, name=name, source="m3u", playlist=file)


# ---------------------------------------------------------------------------
# The form and the file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "field", "message"),
    [
        ({"name": "", "source": "query", "query": "genre:rock"}, "name", "Enter a name"),
        ({"name": "A", "source": "query", "query": ""}, "query", "Enter a beets query"),
        ({"name": "A", "source": "query", "query": "genre::ro"}, "query", "regular expressions"),
        ({"name": "A", "source": "query", "query": "year+"}, "query", "sorting"),
        ({"name": "A", "source": "query", "query": "a, b"}, "query", "Make a category for each"),
        ({"name": "A", "source": "m3u", "playlist": "../A.m3u"}, "playlist", "no '..'"),
        ({"name": "A", "source": "m3u", "playlist": "A.txt"}, "playlist", ".m3u or .m3u8"),
        ({"name": "A", "source": "radio"}, "source", "beets query or a playlist"),
        ({"name": "A\x07", "source": "query", "query": "x"}, "name", "control characters"),
        ({"name": "A", "source": "query", "query": "x", "path": "/"}, "name", "Unknown field"),
    ],
)
def test_the_form_is_refused_with_the_field_and_the_reason(body, field, message):
    with pytest.raises(CategoryError, match=message) as refused:
        parse_spec(body)
    assert refused.value.field == field


def test_names_and_sources_are_trimmed():
    spec = parse_spec(
        {"name": "  2000s ", "source": "query", "query": " original_year:2000..2009 "}
    )
    assert (spec.name, spec.query) == ("2000s", "original_year:2000..2009")
    assert parse_spec({"name": "A", "source": "m3u", "playlist": "Rot/A.M3U8"}).playlist == (
        "Rot/A.M3U8"
    )


def test_filter_category_uses_clock_conditions_and_rejects_nested_categories(tmp_path):
    music = tmp_path / "music"
    tracks = [
        song(music / "Rock" / "one.flac", genre="Rock;Pop", bpm=120),
        song(music / "Rock" / "two.flac", genre="Pop", bpm=91),
        song(music / "Jazz" / "three.flac", genre="Jazz", bpm=120),
    ]
    spec = parse_spec(
        {
            "name": "Upbeat pop-free",
            "source": "filters",
            "match": "all",
            "conditions": [
                {"field": "folder", "source": "music", "folder": "Rock", "recursive": True},
                {"field": "genre", "operator": "is_not", "value": "Rock"},
                {"field": "bpm", "operator": "not_eq", "value": 90},
            ],
        }
    )
    [result] = resolve_all(
        [spec],
        tracks,
        beets=None,
        music_dir=music,
        folder=tmp_path,
        sources_by_id={
            "music": {"id": "music", "kind": "music", "path": str(music), "recursive": True}
        },
    )
    assert result.paths == {tracks[1].path}
    from autodj.clockwheel import ClockSlot, condition_paths
    from autodj.clockwheel_store import resolve_conditions

    same_conditions = resolve_conditions(
        {"type": "music", "match": spec.match, "conditions": spec.conditions},
        {"music": {"id": "music", "kind": "music", "path": str(music), "recursive": True}},
        1,
    )
    assert (
        condition_paths(
            ClockSlot(kind="music", match=spec.match, conditions=same_conditions), tracks
        )
        == result.paths
    )

    any_spec = parse_spec(
        {
            "name": "Either",
            "source": "filters",
            "match": "any",
            "conditions": [
                {"field": "genre", "operator": "is", "value": "Rock"},
                {"field": "bpm", "operator": "gte", "value": 120},
            ],
        }
    )
    [any_result] = resolve_all(
        [any_spec],
        tracks,
        beets=None,
        music_dir=music,
        folder=tmp_path,
    )
    assert any_result.paths == {tracks[0].path, tracks[2].path}
    with pytest.raises(CategoryError, match="cannot refer"):
        parse_spec(
            {
                "name": "Nested",
                "source": "filters",
                "conditions": [{"field": "category", "category": "other"}],
            }
        )


def test_store_round_trips_and_sets_a_bad_file_aside(tmp_path, caplog):
    store = CategoryStore(tmp_path / "categories.json")
    assert store.load() == []
    saved = [query("2000s", "original_year:2000..2009"), playlist("A", "A.m3u8")]
    store.save(saved)
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert store.load() == saved

    store.path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "categories": [saved[0].model_dump(), {**saved[1].model_dump(), "name": "2000S"}],
            }
        ),
        encoding="utf-8",
    )
    with caplog.at_level(logging.ERROR):
        assert store.load() == []
    assert "same name" in caplog.text
    assert not store.path.exists()
    assert len(list(tmp_path.glob("categories.json.bad-*"))) == 1

    store.path.write_text('{"schema_version": 2, "categories": []}', encoding="utf-8")
    assert store.load() == []
    assert len(list(tmp_path.glob("categories.json.bad-*"))) == 2


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------


def test_playlist_lines_skip_comments_and_read_old_encodings(tmp_path):
    utf8 = tmp_path / "a.m3u8"
    utf8.write_bytes("﻿#EXTM3U\n#EXTINF:200,Café\n\n  Café.flac  \r\n".encode())
    assert read_playlist(utf8) == ["Café.flac"]
    latin = tmp_path / "b.m3u"
    latin.write_bytes("#EXTM3U\nCaf\xe9.mp3\n".encode("cp1252"))
    assert read_playlist(latin) == ["Café.mp3"]


def test_playlists_are_listed_and_kept_inside_their_folder(tmp_path):
    folder = tmp_path / "playlists"
    (folder / "Rotation").mkdir(parents=True)
    for name in ("B.m3u", "a.M3U8", "Rotation/C.m3u8", "notes.txt", ".hidden.m3u"):
        (folder / name).write_text("", encoding="utf-8")
    assert list_playlists(folder) == ["a.M3U8", "B.m3u", "Rotation/C.m3u8"]
    assert list_playlists(tmp_path / "none") == []
    assert playlist_file(folder, "Rotation/C.m3u8") == (folder / "Rotation" / "C.m3u8").resolve()
    with pytest.raises(ValueError, match=r"no '\.\.'"):
        playlist_file(folder, "../C.m3u8")


def test_playlist_lines_match_library_tracks_and_report_the_missing(tmp_path):
    music = tmp_path / "music"
    folder = tmp_path / "playlists"
    folder.mkdir()
    songs = [song(music / "Rock" / f"{n}.flac") for n in range(4)]
    songs.append(song(folder / "local.mp3"))
    songs.append(song(music / "quiet.flac", is_silent=True))
    lines = [
        "#EXTM3U",
        str(music / "Rock" / "0.flac"),  # absolute
        "local.mp3",  # beside the playlist
        "Rock/1.flac",  # inside the music folder, as beets writes it
        (music / "Rock" / "2.flac").as_uri(),  # a file:// address
        "http://example.com/stream.mp3",
        "Rock/gone.flac",
        str(music / "Rock" / "0.flac"),  # repeated
        "quiet.flac",
    ]
    (folder / "A.m3u8").write_text("\n".join(lines), encoding="utf-8")
    [result] = resolve_all(
        [playlist("A", "A.m3u8")], songs, beets=None, music_dir=music, folder=folder
    )
    assert result.paths == {songs[0].path, songs[4].path, songs[1].path, songs[2].path}
    assert result.missing == ("http://example.com/stream.mp3", "Rock/gone.flac")
    assert result.missing_count == 2
    assert summary(result) == "4 songs; 2 playlist lines match no library track"

    [gone] = resolve_all(
        [playlist("B", "B.m3u")], songs, beets=None, music_dir=music, folder=folder
    )
    assert gone.error == "The playlist file B.m3u is missing"
    assert summary(gone) == gone.error


def test_file_url_decodes_percent_escapes_once(tmp_path):
    folder = tmp_path / "playlists"
    folder.mkdir()
    track = song(tmp_path / "music" / "50%20off.flac")
    (folder / "A.m3u8").write_text(Path(track.path).as_uri(), encoding="utf-8")
    [result] = resolve_all(
        [playlist("A", "A.m3u8")], [track], beets=None, music_dir=None, folder=folder
    )
    assert result.paths == {track.path}


@pytest.mark.skipif(os.name != "nt", reason="Windows paths fold case")
def test_playlist_paths_match_in_any_letter_case_on_windows(tmp_path):
    folder = tmp_path / "playlists"
    folder.mkdir()
    track = song(tmp_path / "Music" / "Song.flac")
    (folder / "A.m3u").write_text(str(tmp_path / "MUSIC" / "song.FLAC"), encoding="utf-8")
    [result] = resolve_all(
        [playlist("A", "A.m3u")], [track], beets=None, music_dir=None, folder=folder
    )
    assert result.paths == {track.path}


# ---------------------------------------------------------------------------
# Beets queries
# ---------------------------------------------------------------------------


def test_queries_read_beets_fields_and_count_the_release_year_fallbacks(tmp_path):
    music = tmp_path / "music"
    songs = [song(music / f"{n}.flac", genre="Rock", year=2005) for n in range(5)]
    db = make_beets(
        tmp_path / "library.db",
        [
            (1, b"0.flac", "Zero", "Rock", 2005, 1985),  # an 80s song on a 2005 compilation
            (2, str(music / "1.flac"), "One", "Rock", 2003, 2003),
            (3, b"2.flac", "Two", "Rock", 2004, 0),  # no first-release year: year stands in
            (4, b"3.flac", "Three", "Pop", 0, 0),  # neither: left out of year filters
            (5, b"elsewhere.flac", "Not in the library", "Rock", 2001, 2001),
        ],
        attributes=[(2, "mood", "happy"), (3, "mood", "Happy days")],
    )
    cfg = config(tmp_path, beets_db=db)
    from autodj.categories import beets_source

    results = resolve_all(
        [
            query("2000s", "original_year:2000..2009"),
            query("Happy", "mood:happy"),
            query("Not happy", "^mood:happy genre:rock", "c3"),
            query("Typo", "moood:happy", "c4"),
        ],
        songs,
        beets=beets_source(cfg),
        music_dir=music,
        folder=tmp_path,
    )
    decade, happy, unhappy, typo = results
    # Song 4 is not in beets, so the library's own year stands in for it too.
    assert decade.paths == {songs[1].path, songs[2].path, songs[4].path}
    assert decade.fallback == 2
    assert summary(decade) == (
        "3 songs; 2 use the release year because the first-release year is missing"
    )
    assert happy.paths == {songs[1].path, songs[2].path} and happy.fallback == 0
    # Song 3 has genre Pop in beets, laid over the library's Rock.
    assert unhappy.paths == {songs[0].path, songs[4].path}
    assert typo.error is not None and "field named moood" in typo.error


def test_without_beets_queries_use_the_library_fields(tmp_path):
    songs = [
        song(tmp_path / "a.flac", genre="Rock", year=2002),
        song(tmp_path / "b.flac", genre="Rock", year=1999),
        song(tmp_path / "c.flac", genre="Rock", year=2004, is_silent=True),
    ]
    decade, mood = resolve_all(
        [query("2000s", "original_year:2000..2009 genre:rock"), query("Mood", "mood:x", "c2")],
        songs,
        beets=None,
        music_dir=None,
        folder=tmp_path,
    )
    assert decade.paths == {songs[0].path} and decade.fallback == 1
    assert mood.error is not None and "set [library] beets_db" in mood.error


def test_filter_beets_exclusions_do_not_fall_back_to_library_metadata(tmp_path):
    music = tmp_path / "music"
    tracks = [song(music / "a.flac", bpm=120), song(music / "b.flac", bpm=130)]
    db = make_beets(
        tmp_path / "library.db",
        [(1, b"a.flac", "A", "Rock", 2000, 2000), (2, b"b.flac", "B", "Rock", 2000, 2000)],
        attributes=[(1, "rating", "6")],
    )
    from autodj.categories import beets_source

    spec = parse_spec(
        {
            "name": "Not five",
            "source": "filters",
            "conditions": [{"field": "beets", "name": "rating", "operator": "not_eq", "value": 5}],
        }
    )
    [result] = resolve_all(
        [spec],
        tracks,
        beets=beets_source(config(tmp_path, beets_db=db)),
        music_dir=music,
        folder=tmp_path,
    )
    assert result.paths == {tracks[0].path}


def test_an_unreadable_beets_database_is_an_error_not_a_crash(tmp_path):
    from autodj.categories import BeetsSource

    [result] = resolve_all(
        [query("Rock", "genre:rock")],
        [song(tmp_path / "a.flac", genre="Rock")],
        beets=BeetsSource(tmp_path / "missing.db"),
        music_dir=None,
        folder=tmp_path,
    )
    assert result.error is not None and "beets database could not be read" in result.error


# ---------------------------------------------------------------------------
# The live cache
# ---------------------------------------------------------------------------


class Entries:
    """The library's entries: a new tuple each time they change, as the library does."""

    def __init__(self, songs):
        self.value = tuple(songs)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


def test_the_index_works_categories_out_again_only_when_a_source_changes(tmp_path, monkeypatch):
    music = tmp_path / "music"
    songs = [song(music / f"{n}.flac", genre="Rock" if n < 2 else "Pop") for n in range(4)]
    db = make_beets(tmp_path / "library.db", [(1, b"0.flac", "Zero", "Rock", 2000, 2000)])
    cfg = config(tmp_path, beets_db=db)
    entries = Entries(songs)
    index = CategoryIndex(cfg, entries)
    folder = index.folder
    folder.mkdir(parents=True)
    (folder / "A.m3u8").write_text("0.flac\n", encoding="utf-8")
    index.save([query("Rock", "genre:rock"), playlist("A", "A.m3u8")])

    import autodj.categories as module

    runs = []
    real = module.resolve_checked
    monkeypatch.setattr(module, "resolve_checked", lambda *a, **k: runs.append(1) or real(*a, **k))
    first = index.members()
    assert first == {
        "c1": ("Rock", frozenset({songs[0].path, songs[1].path})),
        "p1": ("A", frozenset({songs[0].path})),
    }
    assert index.members() == first
    assert len(runs) == 1  # the second look only checked the sources

    # A playlist rewritten by the update script.
    target = folder / "A.m3u8"
    target.write_text(f"{music / '2.flac'}\n{music / '3.flac'}\n", encoding="utf-8")
    os.utime(target, ns=(1, 1_000_000_000))
    assert index.members()["p1"][1] == {songs[2].path, songs[3].path}
    assert len(runs) == 2

    # beet modify changes a genre.
    conn = sqlite3.connect(db)
    conn.execute("UPDATE items SET genre = 'Pop' WHERE id = 1")
    conn.commit()
    conn.close()
    os.utime(db, ns=(2, 2_000_000_000))
    assert index.members()["c1"][1] == {songs[1].path}
    assert len(runs) == 3

    # New songs imported into the library.
    entries.value = (*entries.value, song(music / "4.flac", genre="Rock"))
    assert index.members()["c1"][1] == {songs[1].path, str(music / "4.flac")}
    assert len(runs) == 4

    # A saved change.
    index.save([query("Pop", "genre:pop")])
    assert index.members() == {
        "c1": ("Pop", frozenset({songs[0].path, songs[2].path, songs[3].path}))
    }
    assert len(runs) == 5


def test_a_busy_source_keeps_the_songs_it_held_last_time(tmp_path, caplog):
    music = tmp_path / "music"
    songs = [song(music / "0.flac"), song(music / "1.flac")]
    cfg = config(tmp_path)
    index = CategoryIndex(cfg, Entries(songs))
    index.folder.mkdir(parents=True)
    target = index.folder / "A.m3u"
    target.write_text("../../music/0.flac\n", encoding="utf-8")
    index.save([playlist("A", "A.m3u")])
    assert index.members()["p1"][1] == {songs[0].path}
    target.unlink()
    with caplog.at_level(logging.WARNING, logger="autodj.categories"):
        _categories, results = index.resolutions()
    assert results["p1"].paths == {songs[0].path}
    assert results["p1"].error == "The playlist file A.m3u is missing"
    assert "Category A: The playlist file A.m3u is missing" in caplog.text


def test_failed_category_keeps_display_paths_but_not_playback_members(tmp_path):
    from autodj.clockwheel import ClockSlot, condition_paths

    songs = [song(tmp_path / "0.flac"), song(tmp_path / "1.flac")]
    index = CategoryIndex(config(tmp_path), Entries(songs))
    index.folder.mkdir(parents=True)
    target = index.folder / "A.m3u"
    target.write_text(f"{songs[0].path}\n", encoding="utf-8")
    index.save([playlist("A", "A.m3u")])
    assert index.members()["p1"][1] == {songs[0].path}

    target.unlink()
    _categories, results = index.resolutions()
    assert results["p1"].paths == {songs[0].path}  # retained for display
    assert results["p1"].error == "The playlist file A.m3u is missing"
    assert "p1" not in index.members()

    for operator in ("is", "is_not"):
        slot = ClockSlot(
            kind="music",
            conditions=({"field": "category", "category": "p1", "operator": operator},),
        )
        assert condition_paths(slot, songs, categories=index.members()) == set()


def test_an_empty_category_is_logged_and_check_counts_an_unsaved_one(tmp_path, caplog):
    songs = [song(tmp_path / "a.flac", genre="Rock")]
    index = CategoryIndex(config(tmp_path), Entries(songs))
    index.save([query("Jazz", "genre:jazz")])
    with caplog.at_level(logging.WARNING, logger="autodj.categories"):
        assert index.members() == {"c1": ("Jazz", frozenset())}
    assert "Category Jazz has no songs in the library" in caplog.text
    checked = index.check(parse_spec({"name": "Rock", "source": "query", "query": "rock"}))
    assert checked.count == 1


def test_each_station_keeps_its_own_categories(tmp_path: Path) -> None:
    from autodj.categories import store_path
    from autodj.config import IndexConfig
    from autodj.stations import StationPaths

    cfg = SimpleNamespace(index=IndexConfig(index_dir=tmp_path))
    assert store_path(cfg) == tmp_path / "stations" / "default" / "categories.json"
    owl = StationPaths.for_station(cfg, "owl")
    assert store_path(cfg, owl) == tmp_path / "stations" / "owl" / "categories.json"


def test_a_new_music_category_follows_the_clock(tmp_path, monkeypatch):
    """ADR-0009-07: songs added within the last N days, worked out again as the hours pass."""
    music = tmp_path / "music"
    songs = [song(music / f"{n}.flac") for n in range(3)]
    now = [1_760_000_000 // 3600 * 3600 + 900]
    day = 86400
    db = tmp_path / "library.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB, title TEXT, added REAL, mtime REAL)"
    )
    conn.executemany(
        "INSERT INTO items (id, path, title, added, mtime) VALUES (?, ?, ?, ?, ?)",
        [
            (1, b"0.flac", "Old", now[0] - 90 * day, now[0] - 90 * day),
            (2, b"1.flac", "Recent", now[0] - 20 * day, now[0] - 20 * day),
            (3, b"2.flac", "New", now[0] - day, now[0] - 2 * day),
        ],
    )
    conn.execute(
        "CREATE TABLE item_attributes (id INTEGER PRIMARY KEY, entity_id INTEGER, key TEXT, "
        "value TEXT)"
    )
    conn.commit()
    conn.close()
    index = CategoryIndex(config(tmp_path, beets_db=db), Entries(songs), clock=lambda: now[0])
    new_music = parse_spec(
        {
            "name": "New music",
            "source": "filters",
            "conditions": [
                {"field": "beets", "name": "added", "operator": "within_days", "value": 30}
            ],
        }
    )
    index.save([Category(id="new", **new_music.model_dump())])
    saved = json.loads(index.store.path.read_text(encoding="utf-8"))
    # The file keeps the number of days, not a date worked out when it was saved.
    assert saved["categories"][0]["conditions"][0]["value"] == 30

    import autodj.categories as module

    runs = []
    real = module.resolve_checked
    monkeypatch.setattr(module, "resolve_checked", lambda *a, **k: runs.append(1) or real(*a, **k))
    assert index.members()["new"][1] == {songs[1].path, songs[2].path}
    signature = index.cache_signature()
    now[0] += 1800  # The same hour: nothing is worked out again.
    assert index.members()["new"][1] == {songs[1].path, songs[2].path}
    assert index.cache_signature() == signature
    assert len(runs) == 1
    now[0] += 11 * day  # The recent song is now 31 days old.
    assert index.members()["new"][1] == {songs[2].path}
    assert index.cache_signature() != signature
    assert len(runs) == 2
    # The Check button judges an unsaved category at the same time.
    by_mtime = parse_spec(
        {
            "name": "Changed this week",
            "source": "filters",
            "conditions": [
                {"field": "beets", "name": "mtime", "operator": "within_days", "value": 7}
            ],
        }
    )
    now[0] -= 11 * day
    assert index.check(by_mtime).paths == {songs[2].path}


def test_a_category_without_relative_dates_ignores_the_hour(tmp_path):
    songs = [song(tmp_path / "music" / "0.flac", genre="Rock")]
    now = [1_760_000_000.0]
    index = CategoryIndex(config(tmp_path), Entries(songs), clock=lambda: now[0])
    index.save([query("Rock", "genre:rock")])
    signature = index.cache_signature()
    now[0] += 7 * 86400
    assert index.cache_signature() == signature


def test_the_clockwheel_and_every_station_read_beets_once(tmp_path, monkeypatch):
    """ADR-0008-35. One read of Beets per change serves the clock and each station's categories."""
    import autodj.beets as beets
    from autodj.clock_condition_data import BeetsConditions

    music = tmp_path / "music"
    songs = [song(music / f"{n}.flac") for n in range(3)]
    db = make_beets(
        tmp_path / "library.db",
        [(1, b"0.flac", "Zero", "Rock", 1985, 1985), (2, b"1.flac", "One", "Pop", 1995, 1995)],
        [(1, "mood", "happy"), (2, "mood", "sad")],
    )
    cfg = config(tmp_path, beets_db=db)
    entries = Entries(songs)
    reads = []
    real = beets.read_items
    monkeypatch.setattr(
        beets, "read_items", lambda path, fields: reads.append(set(fields)) or real(path, fields)
    )
    shared = BeetsConditions(cfg, entries)
    shared.want(["mood"])  # the clockwheel's Beets conditions, announced when it loads
    stations = [
        CategoryIndex(cfg, entries, path=tmp_path / name / "categories.json", beets=shared)
        for name in ("one", "two")
    ]
    for index in stations:
        index.save([query("Rock", "genre:rock")])
        assert index.members() == {"c1": ("Rock", frozenset({songs[0].path}))}
    _names, values, error = shared.snapshot(["mood"])
    from autodj.catalog import normalize_path

    assert error is None and values[normalize_path(songs[0].path)]["mood"] == "happy"
    assert len(reads) == 1 and {"genre", "mood"} <= reads[0]

    # The library changed: the first reader reads every field again, once.
    entries.value = (*entries.value,)
    for index in stations:
        index.members()
    assert shared.snapshot(["mood"])[1] is not values
    assert len(reads) == 2 and {"genre", "mood"} <= reads[1]


def test_working_categories_out_again_says_why(tmp_path, caplog):
    """ADR-0008-35. Each full rebuild is logged with its trigger and how long it took."""
    songs = [song(tmp_path / "music" / "0.flac", genre="Rock")]
    entries = Entries(songs)
    index = CategoryIndex(config(tmp_path), entries)
    index.save([query("Rock", "genre:rock")])
    with caplog.at_level(logging.INFO, logger="autodj.categories"):
        index.members()
        index.members()  # nothing changed: nothing worked out or logged
        entries.value = (*entries.value, song(tmp_path / "music" / "1.flac", genre="Rock"))
        index.members()
    lines = [r.getMessage() for r in caplog.records if "Categories worked out" in r.getMessage()]
    assert len(lines) == 2
    assert lines[0].startswith("Categories worked out for 1 songs in ")
    assert lines[0].endswith("(first time since the start)")
    assert lines[1].endswith("(the library changed)")
