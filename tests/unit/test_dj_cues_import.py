"""Tests for autodj.dj_cues_import.

Cover Mixxx SQLite reader, Rekordbox/Traktor XML readers, the Serato
Markers2 tag reader, library auto-discovery, file:// URL conversion, and key-normalisation merge
edge cases.  Test fixtures construct minimal in-memory artefacts so
the suite never needs real DJ-software installs.
"""

from __future__ import annotations

import base64
import logging
import sqlite3
import struct
from pathlib import Path
from types import SimpleNamespace

import mutagen
import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC
from mutagen.id3 import GEOB, ID3
from mutagen.mp4 import MP4FreeForm, MP4Tags
from mutagen.oggvorbis import OggVorbis

from autodj.dj_cues_import import (
    _default_search_paths,
    _file_url_to_path,
    _normalise_keys,
    import_from_mixxx,
    import_from_rekordbox_xml,
    import_from_serato_tags,
    import_from_traktor_nml,
)
from autodj.dj_meta import Cue

# ---------------------------------------------------------------------------
# Mixxx
# ---------------------------------------------------------------------------


def _make_mixxx_db(
    path: Path,
    rows: list[tuple[str, float, int, str | None]],
    samplerate: int | None = 44100,
) -> None:
    """Build a tiny Mixxx-compatible SQLite db with the rows given.

    Each row = (location, position_in_samples, cue_type, label).
    """
    con = sqlite3.connect(str(path))
    cur = con.cursor()
    cur.executescript(
        """
        CREATE TABLE library (id INTEGER PRIMARY KEY, location INTEGER, samplerate INTEGER);
        CREATE TABLE track_locations (id INTEGER PRIMARY KEY, location TEXT);
        CREATE TABLE cues (
            track_id INTEGER, position REAL, type INTEGER, label TEXT
        );
        """,
    )
    for i, (loc, pos, ctype, label) in enumerate(rows, start=1):
        cur.execute("INSERT INTO track_locations VALUES (?, ?)", (i, loc))
        cur.execute("INSERT INTO library VALUES (?, ?, ?)", (i, i, samplerate))
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
        # type 6 = intro, type 7 = outro; position in stereo samples at 48 kHz
        _make_mixxx_db(
            db,
            [
                ("/music/a.flac", 48000.0 * 2.0 * 5.0, 6, "intro"),  # 5.0s
                ("/music/a.flac", 48000.0 * 2.0 * 200.0, 7, "outro"),  # 200.0s
            ],
            samplerate=48000,
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

    def test_skips_n60db_sound_range(self, tmp_path) -> None:
        # type 8 is Mixxx's hidden "audible sound" range, not an outro
        db = tmp_path / "m.db"
        _make_mixxx_db(db, [("/music/x.flac", 88200.0, 8, None)])
        assert import_from_mixxx(db) == {}

    def test_skips_track_without_samplerate(self, tmp_path) -> None:
        db = tmp_path / "m.db"
        _make_mixxx_db(db, [("/music/x.flac", 88200.0, 1, None)], samplerate=None)
        assert import_from_mixxx(db) == {}

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


# ---------------------------------------------------------------------------
# Serato Markers2
# ---------------------------------------------------------------------------
# Hand-built tags following the serato-tags format notes; no real Serato
# library was available to copy from.


def _serato_entry(name: bytes, data: bytes) -> bytes:
    return name + b"\x00" + struct.pack(">I", len(data)) + data


def _serato_geob_body() -> bytes:
    """A Markers2 body with a track COLOR, one hot cue and one saved loop.

    Encoded the way Serato does it: '=' padding written as 'A', a linefeed
    every 72 base64 characters, NUL padding to 470 bytes.
    """
    cue = b"\x00\x00" + struct.pack(">I", 12500) + b"\x00\xcc\x00\x00\x00\x00"
    cue += "Dröp".encode() + b"\x00"
    loop = b"\x00\x01" + struct.pack(">II", 30000, 32000) + b"\xff" * 4
    loop += b"\x00\x27\xaa\xe1" + b"\x00\x00" + b"Loop A\x00"
    payload = b"\x01\x01" + _serato_entry(b"COLOR", b"\x00\xff\xff\xff")
    payload += _serato_entry(b"CUE", cue) + _serato_entry(b"LOOP", loop) + b"\x00"
    b64 = base64.b64encode(payload).replace(b"=", b"A")
    b64 = b"\n".join(b64[i : i + 72] for i in range(0, len(b64), 72))
    return (b"\x01\x01" + b64).ljust(470, b"\x00")


EXPECTED_SERATO = [
    Cue(time_s=12.5, type="user", label="Dröp", source="serato", color="#cc0000"),
    Cue(time_s=30.0, type="user", label="Loop A", source="serato", color="#27aae1"),
]


def _tiny_mp3(path: Path) -> None:
    """Ten silent MPEG-1 Layer III frames (128 kbps, 44.1 kHz, 417 bytes each)."""
    path.write_bytes((b"\xff\xfb\x90\x00" + b"\x00" * 413) * 10)


def _mp3_with_geob(path: Path, body: bytes) -> Path:
    _tiny_mp3(path)
    tags = ID3()
    tags.add(
        GEOB(encoding=0, mime="application/octet-stream", desc="Serato Markers2", data=body),
    )
    tags.save(path)
    return path


class TestImportFromSeratoTags:
    def test_mp3_geob_frame(self, tmp_path: Path) -> None:
        mp3 = _mp3_with_geob(tmp_path / "t.mp3", _serato_geob_body())
        assert import_from_serato_tags(mp3) == EXPECTED_SERATO

    def test_flac_vorbis_comment(self, tmp_path: Path) -> None:
        flac = tmp_path / "t.flac"
        sf.write(flac, np.zeros(4410, dtype="float32"), 44100)
        tags = FLAC(flac)
        wrapped = b"application/octet-stream\x00\x00Serato Markers2\x00" + _serato_geob_body()
        tags["SERATO_MARKERS_V2"] = base64.b64encode(wrapped).decode("ascii").rstrip("=")
        tags.save()
        assert import_from_serato_tags(flac) == EXPECTED_SERATO

    def test_mp4_freeform_atom(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # No MP4 writer in the test deps: hand mutagen's MP4Tags to the reader.
        tags = MP4Tags()
        wrapped = b"application/octet-stream\x00\x00Serato Markers2\x00" + _serato_geob_body()
        tags["----:com.serato.dj:markersv2"] = [MP4FreeForm(base64.b64encode(wrapped))]
        monkeypatch.setattr(mutagen, "File", lambda _p: SimpleNamespace(tags=tags))
        assert import_from_serato_tags(tmp_path / "t.m4a") == EXPECTED_SERATO

    def test_ogg_is_not_read(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        # serato-tags says Ogg uses a different, undocumented layout.
        ogg = tmp_path / "t.ogg"
        sf.write(ogg, np.zeros(4410, dtype="float32"), 44100, format="OGG")
        tags = OggVorbis(ogg)
        tags["SERATO_MARKERS_V2"] = "not the FLAC layout"
        tags.save()
        with caplog.at_level(logging.WARNING, logger="autodj.dj_cues_import"):
            assert import_from_serato_tags(ogg) == []
        assert caplog.text == ""

    def test_file_without_serato_tag(self, tmp_path: Path) -> None:
        mp3 = tmp_path / "plain.mp3"
        _tiny_mp3(mp3)
        assert import_from_serato_tags(mp3) == []

    def test_missing_file(self, tmp_path: Path) -> None:
        assert import_from_serato_tags(tmp_path / "gone.mp3") == []

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(_serato_geob_body()[:40], id="cut-mid-entry"),
            pytest.param(b"" + _serato_geob_body()[2:], id="unknown-version"),
        ],
    )
    def test_malformed_tag_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, body: bytes
    ) -> None:
        mp3 = _mp3_with_geob(tmp_path / "bad.mp3", body)
        with caplog.at_level(logging.WARNING, logger="autodj.dj_cues_import"):
            assert import_from_serato_tags(mp3) == []
        assert "malformed Markers2" in caplog.text

    def test_flac_field_without_envelope_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The bare GEOB body, without the FLAC/MP4 header, is not a valid field.
        flac = tmp_path / "t.flac"
        sf.write(flac, np.zeros(4410, dtype="float32"), 44100)
        tags = FLAC(flac)
        tags["SERATO_MARKERS_V2"] = base64.b64encode(_serato_geob_body()).decode("ascii")
        tags.save()
        with caplog.at_level(logging.WARNING, logger="autodj.dj_cues_import"):
            assert import_from_serato_tags(flac) == []
        assert "malformed Markers2" in caplog.text
