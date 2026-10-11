"""Tests for autodj.dj_cues_import.

Cover Mixxx SQLite reader, Rekordbox/Traktor XML readers, the Serato
Markers2 tag reader, library auto-discovery, file:// URL conversion, and key-normalisation merge
edge cases.  Test fixtures construct minimal in-memory artefacts so
the suite never needs real DJ-software installs.
"""

from __future__ import annotations

import base64
import itertools
import logging
import sqlite3
import struct
import xml.etree.ElementTree as ET
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
    auto_import_cues,
    import_from_mixxx,
    import_from_rekordbox_xml,
    import_from_serato_tags,
    import_from_traktor_nml,
)
from autodj.dj_meta import Cue, DjMeta, DjMetaCache, detect_cues, merge_cues

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
        assert cues[0].type == "intro_start"
        assert cues[0].source == "mixxx"
        assert cues[1].time_s == pytest.approx(200.0)
        assert cues[1].type == "outro_start"

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
        assert cues[0].type == "intro_start"
        assert cues[0].source == "rekordbox"
        assert cues[1].type == "outro_start"

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
            assert all(c.type != "intro_start" or c.time_s != 0 for c in cues)
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

    def test_non_file_scheme_and_empty_url_return_empty(self) -> None:
        assert _file_url_to_path("https://example.com/track.mp3") == ""
        assert _file_url_to_path("") == ""

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


# ---------------------------------------------------------------------------
# Auto-detect from audio
# ---------------------------------------------------------------------------


def _make_track_with_drop(sr: int = 22050, duration_s: float = 60.0) -> np.ndarray:
    """Synthesize a track with a quiet intro, energetic body, and outro.

    Drop sits around the 30 % mark; breakdown sits at 50 %.
    """
    n = int(sr * duration_s)
    rng = np.random.default_rng(42)
    audio = np.zeros(n, dtype=np.float32)

    # Quiet intro (first 8 s) — low-amplitude noise
    intro_end = int(8.0 * sr)
    audio[:intro_end] = rng.normal(0.0, 0.02, intro_end).astype(np.float32)

    # Body — full amplitude
    body_start = intro_end
    audio[body_start:] = rng.normal(0.0, 0.4, n - body_start).astype(np.float32)

    # Drop spike at 30 %
    drop_idx = int(0.3 * n)
    drop_len = int(2.0 * sr)
    audio[drop_idx : drop_idx + drop_len] = rng.normal(0.0, 0.9, drop_len).astype(np.float32)

    # Breakdown dip at 50 %
    bd_start = int(0.5 * n)
    bd_len = int(6.0 * sr)
    audio[bd_start : bd_start + bd_len] = rng.normal(0.0, 0.02, bd_len).astype(np.float32)

    # Quiet outro (last 6 s)
    outro_start = n - int(6.0 * sr)
    audio[outro_start:] = rng.normal(0.0, 0.02, n - outro_start).astype(np.float32)

    return audio


class TestDetectCues:
    def test_short_audio_returns_empty(self) -> None:
        audio = np.zeros(1000, dtype=np.float32)
        cues = detect_cues(audio, sr=22050, intro_end_s=0.0, outro_start_s=0.0, beats=[])
        assert cues == []

    def test_silent_audio_returns_empty(self) -> None:
        audio = np.zeros(22050 * 30, dtype=np.float32)
        cues = detect_cues(audio, sr=22050, intro_end_s=0.0, outro_start_s=0.0, beats=[])
        assert cues == []

    def test_track_without_drop_or_breakdown(self) -> None:
        # Constant-amplitude noise - no spike, no dip.
        rng = np.random.default_rng(0)
        audio = rng.normal(0.0, 0.2, 22050 * 30).astype(np.float32)
        cues = detect_cues(
            audio,
            sr=22050,
            intro_end_s=2.0,
            outro_start_s=28.0,
            beats=[i * 0.5 for i in range(60)],
        )
        types = {c.type for c in cues}
        # Drop / breakdown require spikes / dips that this track lacks.
        assert "drop" not in types
        assert "breakdown" not in types
        # first_downbeat / outro_downbeat still fire from the beat grid.
        assert "first_downbeat" in types or "outro_downbeat" in types

    def test_outro_zero_means_use_track_duration(self) -> None:
        # outro_start_s == 0 -> body extends to end-of-track.
        audio = _make_track_with_drop(duration_s=40.0)
        cues = detect_cues(
            audio,
            sr=22050,
            intro_end_s=4.0,
            outro_start_s=0.0,  # signals "no outro known"
            beats=[i * 0.5 for i in range(80)],
        )
        # Should still emit phrase / drop cues without crashing.
        assert isinstance(cues, list)

    def test_synthetic_track_emits_drop_and_breakdown(self) -> None:
        audio = _make_track_with_drop()
        # Provide a fake beat grid so first_downbeat / outro_downbeat can fire.
        beats = [i * 0.5 for i in range(120)]  # 0.5 s spacing -> 120 BPM
        cues = detect_cues(
            audio,
            sr=22050,
            intro_end_s=8.0,
            outro_start_s=54.0,
            beats=beats,
        )
        types = {c.type for c in cues}
        assert "drop" in types
        assert "breakdown" in types
        assert "first_downbeat" in types
        assert "outro_downbeat" in types
        # All cues are sorted ascending.
        for lo, hi in itertools.pairwise(cues):
            assert lo.time_s <= hi.time_s
        # All auto-sourced.
        assert all(c.source == "auto" for c in cues)

    def test_phrase_cues_emitted_with_long_beat_grid(self) -> None:
        audio = _make_track_with_drop(duration_s=120.0)
        # 120 s at 120 BPM -> 240 beats (>= 32, so phrases fire).
        beats = [i * 0.5 for i in range(240)]
        cues = detect_cues(
            audio,
            sr=22050,
            intro_end_s=8.0,
            outro_start_s=110.0,
            beats=beats,
        )
        phrase_cues = [c for c in cues if c.type == "phrase"]
        assert len(phrase_cues) >= 1


class TestMergeCues:
    def test_user_wins_over_auto_at_same_time(self) -> None:
        merged = merge_cues(
            [Cue(time_s=10.0, type="drop", source="auto")],
            [Cue(time_s=10.05, type="user", source="user", label="My drop")],
        )
        assert len(merged) == 1
        assert merged[0].source == "user"
        assert merged[0].label == "My drop"

    def test_far_apart_cues_both_kept(self) -> None:
        merged = merge_cues(
            [Cue(time_s=10.0, type="drop", source="auto")],
            [Cue(time_s=30.0, type="user", source="user")],
        )
        assert len(merged) == 2

    def test_dj_software_wins_over_auto(self) -> None:
        merged = merge_cues(
            [Cue(time_s=10.0, type="drop", source="auto")],
            [Cue(time_s=10.1, type="user", source="rekordbox")],
        )
        assert len(merged) == 1
        assert merged[0].source == "rekordbox"

    def test_empty_inputs_return_empty(self) -> None:
        assert merge_cues() == []
        assert merge_cues([], []) == []

    def test_unknown_source_ranks_lowest(self) -> None:
        # Unknown source priority falls back to 0 -- still kept when no
        # collision, but loses against any known source on the same time.
        merged = merge_cues(
            [Cue(time_s=10.0, type="user", source="unknown_xyz")],
            [Cue(time_s=10.1, type="user", source="auto")],
        )
        assert len(merged) == 1
        # auto (priority 1) > unknown (priority 0), so auto wins.
        assert merged[0].source == "auto"

    def test_two_dj_software_sources_keep_first(self) -> None:
        # Same priority -> the earlier-occurring (which sort-stable-keeps) wins.
        merged = merge_cues(
            [Cue(time_s=10.0, type="user", source="mixxx")],
            [Cue(time_s=10.05, type="user", source="rekordbox")],
        )
        assert len(merged) == 1
        # Equal priority: out[-1] is replaced only if new priority is HIGHER.
        # So the first-seen mixxx cue wins.
        assert merged[0].source == "mixxx"


# ---------------------------------------------------------------------------
# Cache round-trip with cues
# ---------------------------------------------------------------------------


class TestDjMetaCacheCues:
    def test_cues_roundtrip_through_sqlite(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "dj_meta.db"
        with DjMetaCache(sidecar) as cache:
            meta = DjMeta(
                intro_end_s=5.0,
                outro_start_s=180.0,
                beats=[0.5, 1.0, 1.5],
                analysed=True,
                cues=[
                    Cue(time_s=10.0, type="drop", source="auto"),
                    Cue(time_s=60.0, type="user", source="rekordbox", label="Hot 1"),
                ],
            )
            cache.set("track.mp3", meta)
            cache.flush(force=True)

        # Reload from disk in a fresh cache.
        with DjMetaCache(sidecar) as cache2:
            loaded = cache2.get("track.mp3")
            assert loaded.analysed
            assert len(loaded.cues) == 2
            assert loaded.cues[0].type == "drop"
            assert loaded.cues[1].label == "Hot 1"

    def test_row_without_cues_loads_empty_list(self, tmp_path: Path) -> None:
        # A track stored before any cues were detected: cues column NULL.
        sidecar = tmp_path / "dj_meta.db"
        with DjMetaCache(sidecar) as cache:
            cache.set(
                "track.mp3",
                DjMeta(
                    intro_end_s=5.0,
                    outro_start_s=180.0,
                    beats=[],
                    analysed=True,
                    cues=[],
                ),
            )
            cache.flush(force=True)

        with DjMetaCache(sidecar) as cache2:
            loaded = cache2.get("track.mp3")
            assert loaded.analysed
            assert loaded.cues == []


# ---------------------------------------------------------------------------
# Mixxx importer
# ---------------------------------------------------------------------------


def _build_mixxx_db(path: Path) -> None:
    """Create a minimal Mixxx-shaped SQLite DB with one track and two cues."""
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.executescript(
        """
        CREATE TABLE track_locations (id INTEGER PRIMARY KEY, location TEXT);
        CREATE TABLE library (id INTEGER PRIMARY KEY, location INTEGER, samplerate INTEGER);
        CREATE TABLE cues (
            track_id INTEGER, position INTEGER, type INTEGER, label TEXT
        );
        INSERT INTO track_locations VALUES (1, '/music/song.mp3');
        INSERT INTO library VALUES (10, 1, 44100);
        -- type 1 = hot cue, position is in stereo samples at 44.1 kHz
        -- 10 s * 44100 * 2 = 882000
        INSERT INTO cues VALUES (10, 882000, 1, 'Hot 1');
        -- type 7 = outro (position is its start), at 60 s
        INSERT INTO cues VALUES (10, 5292000, 7, 'Outro');
        """,
    )
    con.commit()
    con.close()


class TestMixxxImporter:
    def test_imports_cues_with_correct_time_conversion(self, tmp_path: Path) -> None:
        db = tmp_path / "mixxx.db"
        _build_mixxx_db(db)
        result = import_from_mixxx(db)
        # Keys are normalised via str(Path(...)) so they match the FAISS
        # index's native-separator strings -- look up the same way.
        key = str(Path("/music/song.mp3"))
        assert key in result
        cues = result[key]
        assert len(cues) == 2
        # Hot cue at 10 s
        hot = next(c for c in cues if c.label == "Hot 1")
        assert abs(hot.time_s - 10.0) < 0.1
        assert hot.source == "mixxx"
        # Outro at 60 s
        outro = next(c for c in cues if c.label == "Outro")
        assert abs(outro.time_s - 60.0) < 0.1
        assert outro.type == "outro_start"


# ---------------------------------------------------------------------------
# Rekordbox XML importer
# ---------------------------------------------------------------------------


def _build_rekordbox_xml(path: Path) -> None:
    root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    coll = ET.SubElement(root, "COLLECTION", Entries="1")
    track = ET.SubElement(
        coll,
        "TRACK",
        Location="file://localhost/C:/music/track.mp3",
        Name="Sample",
        Artist="Artist",
    )
    ET.SubElement(
        track,
        "POSITION_MARK",
        Name="Hot 1",
        Type="0",
        Start="12.5",
        Red="255",
        Green="0",
        Blue="128",
    )
    ET.SubElement(
        track,
        "POSITION_MARK",
        Name="FadeOut",
        Type="2",
        Start="180.0",
    )
    tree = ET.ElementTree(root)
    tree.write(path, encoding="utf-8", xml_declaration=True)


class TestRekordboxImporter:
    def test_imports_cues_with_color_and_type_mapping(self, tmp_path: Path) -> None:
        xml = tmp_path / "Library.xml"
        _build_rekordbox_xml(xml)
        result = import_from_rekordbox_xml(xml)
        # Path normalised from file:// URL — Windows drive letter prefix dropped.
        keys = list(result.keys())
        assert any("track.mp3" in k for k in keys)
        track_path = next(k for k in keys if "track.mp3" in k)
        cues = result[track_path]
        assert len(cues) == 2
        hot = next(c for c in cues if c.label == "Hot 1")
        assert hot.color == "#ff0080"
        assert hot.source == "rekordbox"
        fade = next(c for c in cues if c.label == "FadeOut")
        assert fade.type == "outro_start"


# ---------------------------------------------------------------------------
# Traktor NML importer
# ---------------------------------------------------------------------------


def _build_traktor_nml(path: Path) -> None:
    root = ET.Element("NML", VERSION="20")
    coll = ET.SubElement(root, "COLLECTION")
    entry = ET.SubElement(coll, "ENTRY", ARTIST="Artist", TITLE="Sample")
    ET.SubElement(
        entry,
        "LOCATION",
        DIR="/:music/:",
        FILE="track.mp3",
        VOLUME="C:",
    )
    ET.SubElement(
        entry,
        "CUE_V2",
        NAME="Hot 1",
        TYPE="0",
        START="12500.0",  # ms
    )
    ET.SubElement(
        entry,
        "CUE_V2",
        NAME="Beat",
        TYPE="4",
        START="60000.0",
    )
    tree = ET.ElementTree(root)
    tree.write(path, encoding="utf-8", xml_declaration=True)


class TestTraktorImporter:
    def test_imports_cues_in_seconds(self, tmp_path: Path) -> None:
        nml = tmp_path / "collection.nml"
        _build_traktor_nml(nml)
        result = import_from_traktor_nml(nml)
        # Should have one entry whose path contains "track.mp3".
        keys = list(result.keys())
        assert any("track.mp3" in k for k in keys)
        cues = result[next(k for k in keys if "track.mp3" in k)]
        # Type 0 -> "user", type 4 -> "phrase".  Times converted from ms.
        types = {c.type for c in cues}
        assert "user" in types
        assert "phrase" in types
        assert all(c.source == "traktor" for c in cues)

    def test_malformed_nml_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.nml"
        path.write_text("garbage", encoding="utf-8")
        assert import_from_traktor_nml(path) == {}


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------


class TestAutoImportCues:
    def test_no_extras_no_library_root_returns_dict(self) -> None:
        # Defaults search ~/Documents etc. -- on a clean test runner those
        # are absent, so the function returns an empty dict (not a crash).
        result = auto_import_cues(library_root=None, extra_paths=[])
        assert isinstance(result, dict)

    def test_picks_up_library_root_files(self, tmp_path: Path) -> None:
        # Drop a Mixxx db AND a Rekordbox XML in the library root.
        _build_mixxx_db(tmp_path / "mixxx.db")
        _build_rekordbox_xml(tmp_path / "Library.xml")
        result = auto_import_cues(library_root=tmp_path, extra_paths=[])
        # Both sources should contribute at least one track.
        all_sources = {c.source for cues in result.values() for c in cues}
        assert "mixxx" in all_sources
        assert "rekordbox" in all_sources

    def test_explicit_extra_paths_take_priority_in_discovery(self, tmp_path: Path) -> None:
        nml = tmp_path / "collection.nml"
        _build_traktor_nml(nml)
        result = auto_import_cues(library_root=None, extra_paths=[nml])
        all_sources = {c.source for cues in result.values() for c in cues}
        assert "traktor" in all_sources


class TestKeyNormalisation:
    """Regression: importer keys must match the FAISS index path style.

    Index entries store ``str(Path(absolute_path))`` -- native separators.
    Importers emit forward slashes regardless of host.  Without
    normalisation, the path matching in the DJ-meta backfill silently
    misses on Windows.
    """

    def test_rekordbox_keys_match_native_path_style(self, tmp_path: Path) -> None:
        xml = tmp_path / "Library.xml"
        _build_rekordbox_xml(xml)
        result = import_from_rekordbox_xml(xml)
        for key in result:
            assert key == str(Path(key))

    def test_traktor_keys_match_native_path_style(self, tmp_path: Path) -> None:
        nml = tmp_path / "collection.nml"
        _build_traktor_nml(nml)
        result = import_from_traktor_nml(nml)
        for key in result:
            assert key == str(Path(key))

    def test_mixxx_keys_match_native_path_style(self, tmp_path: Path) -> None:
        db = tmp_path / "mixxx.db"
        _build_mixxx_db(db)
        result = import_from_mixxx(db)
        for key in result:
            assert key == str(Path(key))
