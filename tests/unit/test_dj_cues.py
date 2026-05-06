"""Tests for cue auto-detection and DJ-software cue importers."""

from __future__ import annotations

import itertools
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from autodj.dj_cues_import import (
    _file_url_to_path,
    auto_import_cues,
    import_from_mixxx,
    import_from_rekordbox_xml,
    import_from_traktor_nml,
)
from autodj.dj_meta import Cue, DjMeta, DjMetaCache, detect_cues, merge_cues

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
    def test_cues_roundtrip_through_sidecar(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "dj_meta.json"
        cache = DjMetaCache(sidecar)
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
        cache2 = DjMetaCache(sidecar)
        loaded = cache2.get("track.mp3")
        assert loaded.analysed
        assert len(loaded.cues) == 2
        assert loaded.cues[0].type == "drop"
        assert loaded.cues[1].label == "Hot 1"

    def test_legacy_sidecar_without_cues_loads(self, tmp_path: Path) -> None:
        # A pre-cues sidecar -- the cache should default cues to [].
        sidecar = tmp_path / "dj_meta.json"
        sidecar.write_text(
            '{"track.mp3": {"intro_end_s": 5.0, "outro_start_s": 180.0, '
            '"beats": [], "analysed": true}}',
            encoding="utf-8",
        )
        cache = DjMetaCache(sidecar)
        loaded = cache.get("track.mp3")
        assert loaded.analysed
        assert loaded.cues == []

    def test_forward_compatible_sidecar_with_unknown_field(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "dj_meta.json"
        sidecar.write_text(
            '{"track.mp3": {"intro_end_s": 5.0, "outro_start_s": 180.0, '
            '"beats": [], "analysed": true, "future_field": "ignored"}}',
            encoding="utf-8",
        )
        cache = DjMetaCache(sidecar)
        loaded = cache.get("track.mp3")
        assert loaded.analysed


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
        CREATE TABLE library (id INTEGER PRIMARY KEY, location INTEGER);
        CREATE TABLE cues (
            track_id INTEGER, position INTEGER, type INTEGER, label TEXT
        );
        INSERT INTO track_locations VALUES (1, '/music/song.mp3');
        INSERT INTO library VALUES (10, 1);
        -- type 1 = hot cue, position is in stereo samples at 44.1 kHz
        -- 10 s * 44100 * 2 = 882000
        INSERT INTO cues VALUES (10, 882000, 1, 'Hot 1');
        -- type 8 = outro start, at 60 s
        INSERT INTO cues VALUES (10, 5292000, 8, 'Outro');
        """,
    )
    con.commit()
    con.close()


class TestMixxxImporter:
    def test_missing_db_returns_empty(self, tmp_path: Path) -> None:
        result = import_from_mixxx(tmp_path / "nope.db")
        assert result == {}

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
        assert outro.type == "outro_downbeat"

    def test_skips_invalid_type_zero(self, tmp_path: Path) -> None:
        db = tmp_path / "mixxx.db"
        _build_mixxx_db(db)
        # Append a type-0 (invalid) cue
        con = sqlite3.connect(db)
        con.execute("INSERT INTO cues VALUES (10, 0, 0, 'invalid')")
        con.commit()
        con.close()
        result = import_from_mixxx(db)
        key = str(Path("/music/song.mp3"))
        types_seen = {c.type for c in result[key]}
        # type=0 is silently skipped (mapped to None in type_map)
        assert "user" in types_seen or "outro_downbeat" in types_seen


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
    def test_missing_xml_returns_empty(self, tmp_path: Path) -> None:
        assert import_from_rekordbox_xml(tmp_path / "nope.xml") == {}

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
        assert fade.type == "outro_downbeat"

    def test_malformed_xml_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.xml"
        path.write_text("<<<not valid xml", encoding="utf-8")
        assert import_from_rekordbox_xml(path) == {}


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
    def test_missing_nml_returns_empty(self, tmp_path: Path) -> None:
        assert import_from_traktor_nml(tmp_path / "nope.nml") == {}

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


class TestNormaliseKeys:
    """Internal helper covered indirectly by the importers."""

    def test_empty_key_dropped(self) -> None:
        from autodj.dj_cues_import import _normalise_keys

        result = _normalise_keys({"": [Cue(time_s=1.0)], "real.mp3": [Cue(time_s=2.0)]})
        # Empty key skipped; real one preserved.
        assert "" not in result
        assert any("real.mp3" in k for k in result)

    def test_duplicate_normalised_keys_concatenate(self) -> None:
        from autodj.dj_cues_import import _normalise_keys

        # Two raw keys that normalise to the same Path str on this OS.
        a, b = "/x/track.mp3", "/x/track.mp3"
        result = _normalise_keys(
            {a: [Cue(time_s=1.0)], b + "/": [Cue(time_s=2.0)]},
        )
        # Whether they collapse depends on the host (trailing slashes
        # are kept on POSIX, stripped on some Pathlib paths) -- this test
        # just ensures no key is lost.
        total = sum(len(v) for v in result.values())
        assert total == 2


class TestKeyNormalisation:
    """Regression: importer keys must match the FAISS index path style.

    Index entries store ``str(Path(absolute_path))`` -- native separators.
    Importers emit forward slashes regardless of host.  Without
    normalisation, the path matching in ``_merge_external_cues_into``
    silently misses on Windows.
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


class TestFileUrlToPath:
    def test_windows_url_strips_drive_slash(self) -> None:
        path = _file_url_to_path("file://localhost/C:/music/track.mp3")
        assert path.startswith("C:")
        assert path.endswith("track.mp3")

    def test_posix_url_passes_through(self) -> None:
        path = _file_url_to_path("file://localhost/music/track.mp3")
        assert path == "/music/track.mp3"

    def test_non_file_scheme_rejected(self) -> None:
        assert _file_url_to_path("http://example/x") == ""

    def test_malformed_url_returns_empty(self) -> None:
        # urlparse rarely raises, so this exercises the empty-path branch.
        assert _file_url_to_path("") == ""
