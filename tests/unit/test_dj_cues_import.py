"""Tests for autodj.dj_cues_import.

Cover Mixxx SQLite reader, Rekordbox/Traktor XML readers, library
auto-discovery, file:// URL conversion, and key-normalisation merge
edge cases.  Test fixtures construct minimal in-memory artefacts so
the suite never needs real DJ-software installs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autodj.dj_cues_import import (
    _default_search_paths,
    _file_url_to_path,
    _normalise_keys,
    import_from_mixxx,
    import_from_rekordbox_xml,
    import_from_traktor_nml,
)
from autodj.dj_meta import Cue

# ---------------------------------------------------------------------------
# Mixxx
# ---------------------------------------------------------------------------


def _make_mixxx_db(path: Path, rows: list[tuple[str, float, int, str | None]]) -> None:
    """Build a tiny Mixxx-compatible SQLite db with the rows given.

    Each row = (location, position_in_samples, cue_type, label).
    """
    con = sqlite3.connect(str(path))
    cur = con.cursor()
    cur.executescript(
        """
        CREATE TABLE library (id INTEGER PRIMARY KEY, location INTEGER);
        CREATE TABLE track_locations (id INTEGER PRIMARY KEY, location TEXT);
        CREATE TABLE cues (
            track_id INTEGER, position REAL, type INTEGER, label TEXT
        );
        """,
    )
    for i, (loc, pos, ctype, label) in enumerate(rows, start=1):
        cur.execute("INSERT INTO track_locations VALUES (?, ?)", (i, loc))
        cur.execute("INSERT INTO library VALUES (?, ?)", (i, i))
        cur.execute(
            "INSERT INTO cues (track_id, position, type, label) VALUES (?, ?, ?, ?)",
            (i, pos, ctype, label),
        )
    con.commit()
    con.close()


class TestImportFromMixxx:
    def test_missing_db_returns_empty(self, tmp_path) -> None:
        result = import_from_mixxx(tmp_path / "nope.db")
        assert result == {}

    def test_parses_intro_outro_cues(self, tmp_path) -> None:
        db = tmp_path / "m.db"
        # type 6 = intro start, type 8 = outro start
        # position is in samples assuming 44100 Hz stereo, so 88200/sample
        _make_mixxx_db(
            db,
            [
                ("/music/a.flac", 44100.0 * 2.0 * 5.0, 6, "intro"),  # 5.0s
                ("/music/a.flac", 44100.0 * 2.0 * 200.0, 8, "outro"),  # 200.0s
            ],
        )
        result = import_from_mixxx(db)
        cues = result.get(str(Path("/music/a.flac")), [])
        assert len(cues) == 2
        # Sorted by time after normalisation.
        assert cues[0].time_s == pytest.approx(5.0)
        assert cues[0].type == "first_downbeat"
        assert cues[0].source == "mixxx"
        assert cues[1].time_s == pytest.approx(200.0)
        assert cues[1].type == "outro_downbeat"

    def test_skips_unmapped_type_zero(self, tmp_path) -> None:
        db = tmp_path / "m.db"
        # type 0 is "invalid" in Mixxx -- not in our type_map, must be skipped
        _make_mixxx_db(db, [("/music/x.flac", 88200.0, 0, None)])
        result = import_from_mixxx(db)
        assert result == {}

    def test_skips_negative_time(self, tmp_path) -> None:
        db = tmp_path / "m.db"
        _make_mixxx_db(db, [("/music/x.flac", -88200.0, 1, None)])
        result = import_from_mixxx(db)
        assert result == {}

    def test_skips_missing_location(self, tmp_path) -> None:
        db = tmp_path / "m.db"
        _make_mixxx_db(db, [("", 88200.0, 1, None)])
        result = import_from_mixxx(db)
        assert result == {}

    def test_corrupt_db_returns_empty(self, tmp_path) -> None:
        # Write a non-SQLite file that exists but isn't a database.
        db = tmp_path / "garbage.db"
        db.write_bytes(b"not a sqlite database at all")
        # Should not raise, just return empty.
        result = import_from_mixxx(db)
        assert result == {}


# ---------------------------------------------------------------------------
# Rekordbox XML
# ---------------------------------------------------------------------------


def _rb_xml(path: Path, tracks: list[tuple[str, list[tuple[str, float, str | None]]]]) -> None:
    """Write a minimal Rekordbox-compatible Library.xml.

    Each track = (location, [(type, start_seconds, name), ...]).
    """
    track_xml = []
    for location, marks in tracks:
        marks_xml = "".join(
            f'<POSITION_MARK Type="{t}" Start="{s}" Name="{n or ""}" />' for t, s, n in marks
        )
        track_xml.append(f'<TRACK Location="{location}">{marks_xml}</TRACK>')
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<DJ_PLAYLISTS><COLLECTION>{''.join(track_xml)}</COLLECTION></DJ_PLAYLISTS>"
    )
    path.write_text(body, encoding="utf-8")


class TestImportFromRekordbox:
    def test_missing_xml_returns_empty(self, tmp_path) -> None:
        result = import_from_rekordbox_xml(tmp_path / "nope.xml")
        assert result == {}

    def test_parses_position_marks(self, tmp_path) -> None:
        xml = tmp_path / "Library.xml"
        _rb_xml(
            xml,
            [
                (
                    "file://localhost/music/a.mp3",
                    [("1", 5.0, "intro"), ("2", 200.0, "outro")],
                ),
            ],
        )
        result = import_from_rekordbox_xml(xml)
        cues = result.get(str(Path("/music/a.mp3")), [])
        assert len(cues) == 2
        assert cues[0].time_s == pytest.approx(5.0)
        assert cues[0].type == "first_downbeat"
        assert cues[0].source == "rekordbox"
        assert cues[1].type == "outro_downbeat"

    def test_skips_track_with_no_location(self, tmp_path) -> None:
        xml = tmp_path / "Library.xml"
        _rb_xml(xml, [("", [("1", 5.0, None)])])
        result = import_from_rekordbox_xml(xml)
        assert result == {}

    def test_skips_unparseable_start(self, tmp_path) -> None:
        xml = tmp_path / "Library.xml"
        # Start="banana" can't be cast to float
        body = (
            '<?xml version="1.0"?><DJ_PLAYLISTS><COLLECTION>'
            '<TRACK Location="file://localhost/x.mp3">'
            '<POSITION_MARK Type="1" Start="banana" Name="" />'
            "</TRACK></COLLECTION></DJ_PLAYLISTS>"
        )
        xml.write_text(body, encoding="utf-8")
        result = import_from_rekordbox_xml(xml)
        # The track key may exist with empty list, OR be omitted.  Either
        # way the bad mark must NOT show up as a cue.
        for cues in result.values():
            assert all(c.type != "first_downbeat" or c.time_s != 0 for c in cues)
        assert not any(cues for cues in result.values())

    def test_corrupt_xml_returns_empty(self, tmp_path) -> None:
        xml = tmp_path / "broken.xml"
        xml.write_text("<not><well-formed", encoding="utf-8")
        result = import_from_rekordbox_xml(xml)
        assert result == {}


# ---------------------------------------------------------------------------
# Traktor NML
# ---------------------------------------------------------------------------


class TestImportFromTraktor:
    def test_missing_nml_returns_empty(self, tmp_path) -> None:
        result = import_from_traktor_nml(tmp_path / "nope.nml")
        assert result == {}

    def test_parses_cue_v2(self, tmp_path) -> None:
        nml = tmp_path / "collection.nml"
        body = (
            '<?xml version="1.0"?><NML><COLLECTION>'
            '<ENTRY><LOCATION VOLUME="C:" DIR="/:music/:" FILE="track.mp3" />'
            '<CUE_V2 START="5000" TYPE="0" NAME="hot1" />'
            '<CUE_V2 START="200000" TYPE="1" NAME="fade-out" />'
            "</ENTRY></COLLECTION></NML>"
        )
        nml.write_text(body, encoding="utf-8")
        result = import_from_traktor_nml(nml)
        # Path reassembled to "C:/music/track.mp3" then normalised.
        keys = list(result.keys())
        assert any("track.mp3" in k for k in keys)
        cues = next(iter(result.values()))
        # START is in milliseconds in NML — first cue is 5000 ms = 5.0 s
        times = sorted(c.time_s for c in cues)
        assert times[0] == pytest.approx(5.0)
        assert times[1] == pytest.approx(200.0)
        assert cues[0].source == "traktor"

    def test_skips_entry_with_no_filename(self, tmp_path) -> None:
        nml = tmp_path / "collection.nml"
        body = (
            '<?xml version="1.0"?><NML><COLLECTION>'
            '<ENTRY><LOCATION VOLUME="C:" DIR="/:music/:" FILE="" />'
            '<CUE_V2 START="5000" TYPE="0" />'
            "</ENTRY></COLLECTION></NML>"
        )
        nml.write_text(body, encoding="utf-8")
        result = import_from_traktor_nml(nml)
        assert result == {}

    def test_skips_unparseable_start(self, tmp_path) -> None:
        nml = tmp_path / "collection.nml"
        body = (
            '<?xml version="1.0"?><NML><COLLECTION>'
            '<ENTRY><LOCATION VOLUME="C:" DIR="/:m/:" FILE="t.mp3" />'
            '<CUE_V2 START="bad" TYPE="0" />'
            "</ENTRY></COLLECTION></NML>"
        )
        nml.write_text(body, encoding="utf-8")
        result = import_from_traktor_nml(nml)
        # Bad START should produce no cue for that entry.
        assert all(len(cs) == 0 for cs in result.values()) or result == {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestFileUrlToPath:
    def test_macos_style_path(self) -> None:
        # No drive letter -> leading slash kept
        assert _file_url_to_path("file://localhost/Users/Foo/track.mp3") == ("/Users/Foo/track.mp3")

    def test_windows_drive_letter_strips_leading_slash(self) -> None:
        result = _file_url_to_path("file://localhost/C:/Music/x.mp3")
        assert result == "C:/Music/x.mp3"

    def test_url_decodes_percent_escapes(self) -> None:
        result = _file_url_to_path("file://localhost/music/track%20with%20spaces.mp3")
        assert "track with spaces.mp3" in result

    def test_non_file_scheme_returns_empty(self) -> None:
        assert _file_url_to_path("https://example.com/track.mp3") == ""

    def test_garbage_returns_empty_or_path(self) -> None:
        # urlparse is lenient; just confirm we don't raise.
        result = _file_url_to_path("not a url at all")
        assert isinstance(result, str)


class TestNormaliseKeys:
    def test_sorts_cues_by_time(self) -> None:
        raw = {
            "/music/a.flac": [
                Cue(time_s=10.0, type="user", source="auto"),
                Cue(time_s=2.0, type="user", source="auto"),
                Cue(time_s=5.0, type="user", source="auto"),
            ],
        }
        result = _normalise_keys(raw)
        cues = next(iter(result.values()))
        assert [c.time_s for c in cues] == [2.0, 5.0, 10.0]

    def test_skips_empty_keys(self) -> None:
        result = _normalise_keys({"": [Cue(time_s=1.0, type="user", source="auto")]})
        assert result == {}

    def test_concatenates_duplicate_paths(self) -> None:
        # Two raw keys that normalise to the same path should be merged.
        a = Cue(time_s=1.0, type="user", source="mixxx")
        b = Cue(time_s=2.0, type="user", source="rekordbox")
        raw = {"/music/x.flac": [a], "/music//x.flac": [b]}
        result = _normalise_keys(raw)
        # Both entries collapse to one key
        assert len(result) == 1
        merged = next(iter(result.values()))
        assert len(merged) == 2


class TestDefaultSearchPaths:
    def test_returns_list_of_paths(self, tmp_path) -> None:
        result = _default_search_paths(tmp_path)
        assert isinstance(result, list)
        for p in result:
            assert isinstance(p, Path)

    def test_includes_rekordbox_export_path(self, tmp_path) -> None:
        result = _default_search_paths(tmp_path)
        assert any(p.name == "Library.xml" for p in result)

    def test_picks_up_traktor_collection_when_present(self, tmp_path) -> None:
        traktor = tmp_path / "Documents" / "Native Instruments" / "Traktor 3.5.0"
        traktor.mkdir(parents=True)
        (traktor / "collection.nml").write_text("<NML/>", encoding="utf-8")
        result = _default_search_paths(tmp_path)
        assert any(p.name == "collection.nml" for p in result)
