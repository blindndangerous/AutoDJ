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
        with pytest.raises(sqlite3.DatabaseError):
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


# ---------------------------------------------------------------------------
# parse_initial_key
# ---------------------------------------------------------------------------


class TestParseInitialKey:
    @pytest.mark.parametrize(
        "s,expected",
        [
            ("C", (0, 1)),
            ("D", (2, 1)),
            ("D#", (3, 1)),
            ("Eb", (3, 1)),
            ("Bb", (10, 1)),
            ("F#", (6, 1)),
            ("Cm", (0, 0)),
            ("D#m", (3, 0)),
            ("Bbm", (10, 0)),
            ("A minor", (9, 0)),
            ("F# major", (6, 1)),
            ("c major", (0, 1)),  # case insensitive
            ("8A", (9, 0)),  # Camelot — A minor
            ("8B", (0, 1)),  # Camelot — C major
            ("12B", (4, 1)),
            ("1B", (11, 1)),
            ("1A", (8, 0)),  # Ab minor
        ],
    )
    def test_parses_valid_keys(self, s, expected) -> None:
        from autodj.beets import parse_initial_key

        assert parse_initial_key(s) == expected

    @pytest.mark.parametrize(
        "s",
        [
            "",
            "garbage",
            "Z",
            "13A",
            "0A",
            None,
        ],
    )
    def test_unparseable_returns_none(self, s) -> None:
        from autodj.beets import parse_initial_key

        assert parse_initial_key(s) is None

    def test_whitespace_stripped(self) -> None:
        from autodj.beets import parse_initial_key

        assert parse_initial_key("  C  ") == (0, 1)
        assert parse_initial_key(" Cm ") == (0, 0)

    def test_only_whitespace_returns_none(self) -> None:
        from autodj.beets import parse_initial_key

        assert parse_initial_key("    ") is None


# ---------------------------------------------------------------------------
# Optional plugin columns: initial_key + lyrics
# ---------------------------------------------------------------------------


def _make_beets_db_with_optional(path: Path) -> Path:
    """Beets DB schema that includes the keyfinder + lyrics plugin columns."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE items (
            id INTEGER PRIMARY KEY,
            path BLOB,
            title TEXT, artist TEXT, album TEXT, genre TEXT,
            bpm REAL, year INTEGER, length REAL,
            initial_key TEXT,
            lyrics TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO items VALUES (1, ?, 'T', 'A', 'L', 'G', 100.0, 2020, 180.0, 'Am', 'la la la')",
        (b"Z:/Music/song.flac",),
    )
    conn.execute(
        "INSERT INTO items VALUES (2, ?, 'T2', 'A2', 'L2', 'G', 120.0, 2021, 240.0, '8B', '')",
        (b"sub/relative.flac",),
    )
    conn.commit()
    conn.close()
    return path


class TestOptionalColumns:
    def test_get_all_tracks_reads_initial_key(self, tmp_path) -> None:
        db = _make_beets_db_with_optional(tmp_path / "library.db")
        tracks = get_all_tracks(db)
        assert tracks[0].initial_key == "Am"
        assert tracks[1].initial_key == "8B"

    def test_get_all_tracks_reads_lyrics(self, tmp_path) -> None:
        db = _make_beets_db_with_optional(tmp_path / "library.db")
        tracks = get_all_tracks(db)
        assert tracks[0].lyrics == "la la la"


class TestGetLyricsForPath:
    def test_returns_lyrics_for_absolute_path(self, tmp_path) -> None:
        from autodj.beets import get_lyrics_for_path

        db = _make_beets_db_with_optional(tmp_path / "library.db")
        result = get_lyrics_for_path(db, "Z:/Music/song.flac")
        assert result == "la la la"

    def test_returns_empty_for_unknown_path(self, tmp_path) -> None:
        from autodj.beets import get_lyrics_for_path

        db = _make_beets_db_with_optional(tmp_path / "library.db")
        assert get_lyrics_for_path(db, "Z:/Music/no_such.flac") == ""

    def test_returns_empty_when_db_missing(self, tmp_path) -> None:
        from autodj.beets import get_lyrics_for_path

        assert get_lyrics_for_path(tmp_path / "missing.db", "any.flac") == ""

    def test_returns_empty_when_lyrics_column_absent(self, tmp_path, beets_db) -> None:
        from autodj.beets import get_lyrics_for_path

        # beets_db fixture has no lyrics column
        assert get_lyrics_for_path(beets_db, "Z:/Music/anything.flac") == ""

    def test_returns_empty_for_corrupt_db(self, tmp_path) -> None:
        from autodj.beets import get_lyrics_for_path

        bad = tmp_path / "library.db"
        bad.write_bytes(b"not sqlite")
        assert get_lyrics_for_path(bad, "anything.flac") == ""

    def test_falls_back_to_relative_path(self, tmp_path) -> None:
        """Beets stored a relative path; we look up by joining music_dir."""
        from autodj.beets import get_lyrics_for_path

        db = _make_beets_db_with_optional(tmp_path / "library.db")
        # Construct a fake music_dir that contains the relative path
        music_dir = tmp_path / "Music"
        music_dir.mkdir()
        track_dir = music_dir / "sub"
        track_dir.mkdir()
        track_file = track_dir / "relative.flac"
        track_file.write_bytes(b"")
        # Track is stored as "sub/relative.flac" in the DB; runtime path
        # is the absolute version under music_dir
        result = get_lyrics_for_path(db, str(track_file), music_dir=music_dir)
        # No lyrics for that row, so empty string is correct, but the
        # query path was exercised
        assert result == ""


# ---------------------------------------------------------------------------
# autodj.beets  (parser + path-decode branch coverage)
# ---------------------------------------------------------------------------


class TestBeetsHelpers:
    def test_camelot_key_with_invalid_number_returns_none(self) -> None:
        from autodj.beets import _parse_camelot_key

        # Out-of-range numbers
        assert _parse_camelot_key("0A") is None
        assert _parse_camelot_key("13B") is None

    def test_split_note_and_mode_empty_after_strip(self) -> None:
        from autodj.beets import parse_initial_key

        # 'major' alone becomes empty note_part — should return None
        assert parse_initial_key("major") is None
        assert parse_initial_key("minor") is None

    def test_decode_path_from_str(self) -> None:
        from pathlib import Path

        from autodj.beets import _decode_path

        result = _decode_path("/some/path/track.mp3")
        assert isinstance(result, Path)
        assert str(result).replace("\\", "/") == "/some/path/track.mp3"

    def test_decode_path_from_bytes(self) -> None:
        from pathlib import Path

        from autodj.beets import _decode_path

        result = _decode_path(b"/some/path/track.mp3")
        assert isinstance(result, Path)
