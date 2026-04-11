"""Unit tests for autodj.beets.

Uses an in-memory SQLite database to simulate a beets library.db without
requiring a real beets installation or music files.
"""

import sqlite3
from pathlib import Path

import pytest

from autodj.beets import BeetsNotFoundError, Track, get_all_tracks, search_tracks


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_beets_db(path: Path, rows: list[dict]) -> Path:
    """Create a minimal beets-compatible SQLite database at *path*.

    Beets encodes file paths as UTF-8 bytes in the ``path`` column.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE items (
            id INTEGER PRIMARY KEY,
            path BLOB,
            title TEXT,
            artist TEXT,
            album TEXT,
            genre TEXT,
            bpm REAL,
            year INTEGER,
            length REAL
        )
        """
    )
    for row in rows:
        conn.execute(
            "INSERT INTO items (path, title, artist, album, genre, bpm, year, length) "
            "VALUES (:path, :title, :artist, :album, :genre, :bpm, :year, :length)",
            {
                "path": row["path"].encode("utf-8"),
                "title": row.get("title", ""),
                "artist": row.get("artist", ""),
                "album": row.get("album", ""),
                "genre": row.get("genre", ""),
                "bpm": row.get("bpm", 0.0),
                "year": row.get("year", 0),
                "length": row.get("length", 0.0),
            },
        )
    conn.commit()
    conn.close()
    return path


SAMPLE_ROWS = [
    {
        "path": "Z:/Music/Portishead/Dummy/01 - Mysterons.flac",
        "title": "Mysterons",
        "artist": "Portishead",
        "album": "Dummy",
        "genre": "Trip-Hop",
        "bpm": 95.0,
        "year": 1994,
        "length": 300.0,
    },
    {
        "path": "Z:/Music/Portishead/Dummy/02 - Sour Times.flac",
        "title": "Sour Times",
        "artist": "Portishead",
        "album": "Dummy",
        "genre": "Trip-Hop",
        "bpm": 98.0,
        "year": 1994,
        "length": 262.0,
    },
    {
        "path": "Z:/Music/Massive Attack/Blue Lines/01 - Safe From Harm.flac",
        "title": "Safe From Harm",
        "artist": "Massive Attack",
        "album": "Blue Lines",
        "genre": "Trip-Hop",
        "bpm": 104.0,
        "year": 1991,
        "length": 340.0,
    },
]


@pytest.fixture
def beets_db(tmp_path: Path) -> Path:
    """An in-memory beets SQLite database with three sample tracks."""
    return _make_beets_db(tmp_path / "library.db", SAMPLE_ROWS)


@pytest.fixture
def empty_beets_db(tmp_path: Path) -> Path:
    return _make_beets_db(tmp_path / "library.db", [])


# ---------------------------------------------------------------------------
# Track dataclass
# ---------------------------------------------------------------------------


class TestTrack:
    def test_track_has_expected_fields(self) -> None:
        t = Track(
            path=Path("Z:/Music/song.flac"),
            title="Song",
            artist="Artist",
            album="Album",
            genre="Jazz",
            bpm=120.0,
            year=2000,
            length=180.0,
        )
        assert t.path == Path("Z:/Music/song.flac")
        assert t.bpm == 120.0

    def test_track_display_name(self) -> None:
        t = Track(
            path=Path("Z:/Music/song.flac"),
            title="Song",
            artist="Artist",
            album="Album",
            genre="Jazz",
            bpm=120.0,
            year=2000,
            length=180.0,
        )
        assert t.display_name == "Artist — Song"

    def test_track_display_name_no_artist(self) -> None:
        t = Track(
            path=Path("Z:/Music/song.flac"),
            title="Song",
            artist="",
            album="Album",
            genre="",
            bpm=0.0,
            year=0,
            length=0.0,
        )
        assert t.display_name == "Song"


# ---------------------------------------------------------------------------
# get_all_tracks
# ---------------------------------------------------------------------------


class TestGetAllTracks:
    def test_returns_all_rows(self, beets_db: Path) -> None:
        tracks = get_all_tracks(beets_db)
        assert len(tracks) == 3

    def test_returns_track_objects(self, beets_db: Path) -> None:
        tracks = get_all_tracks(beets_db)
        assert all(isinstance(t, Track) for t in tracks)

    def test_path_decoded_from_bytes(self, beets_db: Path) -> None:
        tracks = get_all_tracks(beets_db)
        assert all(isinstance(t.path, Path) for t in tracks)

    def test_metadata_correct(self, beets_db: Path) -> None:
        tracks = get_all_tracks(beets_db)
        mysterons = next(t for t in tracks if t.title == "Mysterons")
        assert mysterons.artist == "Portishead"
        assert mysterons.bpm == 95.0
        assert mysterons.year == 1994

    def test_empty_library_returns_empty_list(self, empty_beets_db: Path) -> None:
        tracks = get_all_tracks(empty_beets_db)
        assert tracks == []

    def test_raises_if_db_missing(self, tmp_path: Path) -> None:
        with pytest.raises(BeetsNotFoundError):
            get_all_tracks(tmp_path / "nonexistent.db")

    def test_raises_if_db_not_sqlite(self, tmp_path: Path) -> None:
        bad = tmp_path / "library.db"
        bad.write_bytes(b"not a sqlite file")
        with pytest.raises(Exception):
            get_all_tracks(bad)


# ---------------------------------------------------------------------------
# search_tracks
# ---------------------------------------------------------------------------


class TestSearchTracks:
    def test_search_by_artist(self, beets_db: Path) -> None:
        results = search_tracks(beets_db, "Portishead")
        assert len(results) == 2
        assert all(t.artist == "Portishead" for t in results)

    def test_search_by_title(self, beets_db: Path) -> None:
        results = search_tracks(beets_db, "Mysterons")
        assert len(results) == 1
        assert results[0].title == "Mysterons"

    def test_search_case_insensitive(self, beets_db: Path) -> None:
        results = search_tracks(beets_db, "portishead")
        assert len(results) == 2

    def test_search_partial_match(self, beets_db: Path) -> None:
        results = search_tracks(beets_db, "ortis")  # substring of "Portishead"
        assert len(results) == 2

    def test_search_no_match_returns_empty(self, beets_db: Path) -> None:
        results = search_tracks(beets_db, "xyzzy_nonexistent")
        assert results == []

    def test_search_across_album(self, beets_db: Path) -> None:
        results = search_tracks(beets_db, "Blue Lines")
        assert len(results) == 1
        assert results[0].artist == "Massive Attack"

    def test_search_raises_if_db_missing(self, tmp_path: Path) -> None:
        with pytest.raises(BeetsNotFoundError):
            search_tracks(tmp_path / "nonexistent.db", "query")
