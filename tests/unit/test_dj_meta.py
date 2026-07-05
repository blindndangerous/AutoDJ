"""Tests for autodj.dj_meta — Camelot wheel, intro/outro, beats, cache."""

from __future__ import annotations

import numpy as np
import pytest

from autodj.dj_meta import (
    DjMeta,
    DjMetaCache,
    camelot_label,
    camelot_position,
    detect_intro_outro,
    harmonic_compatible,
    key_label,
    musical_label,
    nearest_phrase_boundary,
)

# ---------------------------------------------------------------------------
# Camelot wheel
# ---------------------------------------------------------------------------


class TestCamelotPosition:
    def test_c_major_is_8b(self) -> None:
        assert camelot_position(0, 1) == (8, "B")

    def test_a_minor_is_8a(self) -> None:
        assert camelot_position(9, 0) == (8, "A")

    def test_e_major_is_12b(self) -> None:
        assert camelot_position(4, 1) == (12, "B")

    def test_unknown_key_returns_none(self) -> None:
        assert camelot_position(-1, 1) is None
        assert camelot_position(0, -1) is None
        assert camelot_position(12, 1) is None


class TestCamelotLabel:
    def test_known_keys(self) -> None:
        assert camelot_label(0, 1) == "8B"
        assert camelot_label(9, 0) == "8A"

    def test_unknown_returns_dashes(self) -> None:
        assert camelot_label(-1, 1) == "--"


class TestMusicalLabel:
    def test_majors_use_letter_only(self) -> None:
        assert musical_label(0, 1) == "C"
        assert musical_label(7, 1) == "G"
        assert musical_label(11, 1) == "B"

    def test_minors_get_m_suffix(self) -> None:
        assert musical_label(9, 0) == "Am"
        assert musical_label(0, 0) == "Cm"

    def test_default_uses_sharps(self) -> None:
        # Pitch class 1 = C# / Db.  Default sharps.
        assert musical_label(1, 1) == "C#"
        assert musical_label(6, 1) == "F#"
        assert musical_label(10, 0) == "A#m"

    def test_prefer_flats_uses_flats(self) -> None:
        assert musical_label(1, 1, prefer_flats=True) == "Db"
        assert musical_label(6, 1, prefer_flats=True) == "Gb"
        assert musical_label(10, 0, prefer_flats=True) == "Bbm"

    def test_naturals_unchanged_by_flats_flag(self) -> None:
        # C / D / E / F / G / A / B have no enharmonic flat spelling.
        assert musical_label(0, 1, prefer_flats=True) == "C"
        assert musical_label(11, 1, prefer_flats=True) == "B"

    def test_unknown_returns_dashes(self) -> None:
        assert musical_label(-1, 1) == "--"
        assert musical_label(0, -1) == "--"
        assert musical_label(12, 1) == "--"


class TestKeyLabelDispatcher:
    def test_camelot_default(self) -> None:
        assert key_label(9, 0) == "8A"

    def test_camelot_explicit(self) -> None:
        assert key_label(9, 0, "camelot") == "8A"

    def test_musical(self) -> None:
        assert key_label(9, 0, "musical") == "Am"

    def test_musical_with_flats(self) -> None:
        assert key_label(1, 1, "musical", prefer_flats=True) == "Db"

    def test_unknown_notation_falls_back_to_camelot(self) -> None:
        assert key_label(9, 0, "bogus") == "8A"

    def test_unknown_input_returns_dashes(self) -> None:
        assert key_label(-1, 1, "musical") == "--"
        assert key_label(-1, 1, "camelot") == "--"


class TestHarmonicCompatible:
    def test_same_key_same_mode(self) -> None:
        # C major + C major
        assert harmonic_compatible(0, 1, 0, 1) is True

    def test_relative_major_minor(self) -> None:
        # C major (8B) + A minor (8A)
        assert harmonic_compatible(0, 1, 9, 0) is True

    def test_one_step_around_wheel(self) -> None:
        # 8B → 7B (one step counter-clockwise)
        # 8B = C major, 7B = F major
        assert harmonic_compatible(0, 1, 5, 1) is True
        # 8B → 9B (clockwise)
        # 9B = G major
        assert harmonic_compatible(0, 1, 7, 1) is True

    def test_two_steps_not_compatible(self) -> None:
        # 8B → 10B = D major (two steps clockwise, NOT compatible)
        assert harmonic_compatible(0, 1, 2, 1) is False

    def test_wheel_wraps_at_12(self) -> None:
        # 12B → 1B should be compatible
        # 12B = E, 1B = B major
        assert harmonic_compatible(4, 1, 11, 1) is True

    def test_unknown_keys_always_pass(self) -> None:
        assert harmonic_compatible(-1, 1, 5, 0) is True
        assert harmonic_compatible(0, 1, -1, 0) is True


# ---------------------------------------------------------------------------
# Intro / outro detection
# ---------------------------------------------------------------------------


class TestDetectIntroOutro:
    def test_silent_audio_returns_zero_to_duration(self) -> None:
        sr = 22050
        audio = np.zeros(sr * 5, dtype=np.float32)
        intro, outro = detect_intro_outro(audio, sr)
        # Silent → all-zero RMS → fall through to (0, duration)
        assert intro == 0.0
        assert outro == pytest.approx(5.0, abs=0.5)

    def test_quiet_intro_loud_middle_quiet_outro(self) -> None:
        sr = 22050
        audio = np.zeros(sr * 30, dtype=np.float32)
        # Loud middle (4s to 26s)
        rng = np.random.default_rng(0)
        audio[sr * 4 : sr * 26] = rng.standard_normal(sr * 22).astype(np.float32) * 0.4
        intro, outro = detect_intro_outro(audio, sr)
        assert 3.0 < intro < 5.0
        assert 25.0 < outro < 27.0

    def test_empty_audio(self) -> None:
        intro, outro = detect_intro_outro(np.zeros(0, dtype=np.float32), 22050)
        assert intro == 0.0
        assert outro == 0.0


# ---------------------------------------------------------------------------
# Phrase boundary snapping
# ---------------------------------------------------------------------------


class TestNearestPhraseBoundary:
    def test_snaps_to_nearest_8_bar_boundary(self) -> None:
        # 64 beats at 0.5 s/beat = 128 BPM
        beats = [i * 0.5 for i in range(64)]
        # 8 bars * 4 beats = 32 beats per phrase = 16 s per phrase
        snap = nearest_phrase_boundary(beats, target_time_s=15.7, bars=8)
        # Closest phrase boundary is beat 32 = 16.0 s
        assert snap == pytest.approx(16.0)

    def test_returns_none_when_no_grid(self) -> None:
        assert nearest_phrase_boundary([], 10.0) is None

    def test_returns_none_when_grid_too_short(self) -> None:
        # Need at least 8 * 4 = 32 beats to form a phrase
        assert nearest_phrase_boundary([0.5, 1.0, 1.5], 10.0) is None

    def test_returns_none_when_target_far_from_any_boundary(self) -> None:
        beats = [i * 0.5 for i in range(64)]
        # target halfway between phrase 0 (0s) and phrase 1 (16s) = 8 s
        # Half-phrase tolerance = 8 s, exactly on boundary.  Try 7.999
        # which is INSIDE tolerance → returns 0.0
        snap = nearest_phrase_boundary(beats, target_time_s=7.99, bars=8)
        assert snap == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# DjMetaCache
# ---------------------------------------------------------------------------


class TestDjMetaCache:
    def test_get_returns_empty_for_unknown(self, tmp_path) -> None:
        cache = DjMetaCache(tmp_path / "cache.db")
        meta = cache.get("unknown.flac")
        assert meta.analysed is False
        assert meta.beats == []

    def test_set_then_get_round_trip(self, tmp_path) -> None:
        cache = DjMetaCache(tmp_path / "cache.db")
        cache.set(
            "foo.flac",
            DjMeta(
                intro_end_s=2.5,
                outro_start_s=180.0,
                beats=[0.5, 1.0, 1.5],
                analysed=True,
            ),
        )
        meta = cache.get("foo.flac")
        assert meta.intro_end_s == 2.5
        assert meta.outro_start_s == 180.0
        assert meta.beats == [0.5, 1.0, 1.5]
        assert meta.analysed is True

    def test_flush_persists_to_db(self, tmp_path) -> None:
        path = tmp_path / "cache.db"
        cache = DjMetaCache(path)
        cache.set("foo.flac", DjMeta(intro_end_s=1.0, analysed=True))
        cache.flush(force=True)
        cache.close()
        # Reopen and confirm the row survives a fresh connection.
        cache2 = DjMetaCache(path)
        meta = cache2.get("foo.flac")
        assert meta.intro_end_s == 1.0
        assert meta.analysed is True

    def test_flush_skips_when_not_dirty_enough(self, tmp_path) -> None:
        path = tmp_path / "cache.db"
        cache = DjMetaCache(path)
        cache.set("foo.flac", DjMeta(analysed=True))
        cache.flush(force=False, batch=10)  # only 1 dirty, batch=10
        # DB file exists (opened on init) but contains no row yet.
        cache.close()
        cache2 = DjMetaCache(path)
        assert cache2.get("foo.flac").analysed is False
        cache2.close()

    def test_prune_to_paths_removes_stale_rows(self, tmp_path) -> None:
        path = tmp_path / "cache.db"
        cache = DjMetaCache(path)
        cache.set("keep.flac", DjMeta(analysed=True))
        cache.set("stale.flac", DjMeta(analysed=True))

        removed = cache.prune_to_paths({"keep.flac"})

        assert removed == 1
        assert cache.get("keep.flac").analysed is True
        assert cache.get("stale.flac").analysed is False
        cache.close()

        cache2 = DjMetaCache(path)
        assert cache2.get("keep.flac").analysed is True
        assert cache2.get("stale.flac").analysed is False
        cache2.close()

    def test_absolute_paths_are_stored_as_relative_keys(self, tmp_path) -> None:
        import sqlite3 as _sql

        path = tmp_path / "cache.db"
        music_dir = tmp_path / "Music"
        cache = DjMetaCache(path, music_dir=music_dir)
        cache.set(str(music_dir / "Artist" / "song.flac"), DjMeta(analysed=True))
        cache.flush(force=True)
        cache.close()

        conn = _sql.connect(path)
        try:
            rows = conn.execute("SELECT path FROM dj_meta").fetchall()
        finally:
            conn.close()
        assert rows == [("Artist/song.flac",)]

    def test_absolute_paths_outside_music_dir_stay_absolute(self, tmp_path) -> None:
        import sqlite3 as _sql

        path = tmp_path / "cache.db"
        music_dir = tmp_path / "Music"
        outside = tmp_path / "Other" / "song.flac"
        cache = DjMetaCache(path, music_dir=music_dir)
        cache.set(str(outside), DjMeta(analysed=True))
        cache.flush(force=True)
        cache.close()

        conn = _sql.connect(path)
        try:
            rows = conn.execute("SELECT path FROM dj_meta").fetchall()
        finally:
            conn.close()
        assert rows == [(outside.as_posix(),)]

    def test_legacy_absolute_rows_migrate_to_relative_keys(self, tmp_path) -> None:
        import sqlite3 as _sql

        path = tmp_path / "cache.db"
        music_dir = tmp_path / "Music"
        legacy = "/volume1/Mike/Beetsmusic/Artist/song.flac"

        conn = _sql.connect(path)
        conn.execute(DjMetaCache._SCHEMA)
        conn.execute(
            "INSERT INTO dj_meta (path, intro_end_s, outro_start_s, analysed, beats, cues) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (legacy, 1.0, 100.0, 1, "[]", "[]"),
        )
        conn.commit()
        conn.close()

        cache = DjMetaCache(
            path,
            music_dir=music_dir,
            path_remap=[("/volume1/Mike/Beetsmusic/", f"{music_dir.as_posix()}/")],
        )
        assert cache.get(str(music_dir / "Artist" / "song.flac")).analysed is True
        cache.close()

        conn = _sql.connect(path)
        try:
            rows = conn.execute("SELECT path FROM dj_meta").fetchall()
        finally:
            conn.close()
        assert rows == [("Artist/song.flac",)]

    def test_legacy_duplicate_migration_prefers_analysed_row(self, tmp_path) -> None:
        import sqlite3 as _sql

        path = tmp_path / "cache.db"
        music_dir = tmp_path / "Music"
        legacy = "/volume1/Mike/Beetsmusic/Artist/song.flac"

        conn = _sql.connect(path)
        conn.execute(DjMetaCache._SCHEMA)
        conn.executemany(
            "INSERT INTO dj_meta (path, intro_end_s, outro_start_s, analysed, beats, cues) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("Artist/song.flac", 0.0, 0.0, 0, "[]", "[]"),
                (legacy, 4.0, 120.0, 1, "[]", "[]"),
            ],
        )
        conn.commit()
        conn.close()

        cache = DjMetaCache(
            path,
            music_dir=music_dir,
            path_remap=[("/volume1/Mike/Beetsmusic/", f"{music_dir.as_posix()}/")],
        )
        meta = cache.get("Artist/song.flac")
        assert meta.analysed is True
        assert meta.intro_end_s == 4.0
        cache.close()

        conn = _sql.connect(path)
        try:
            rows = conn.execute("SELECT path FROM dj_meta").fetchall()
        finally:
            conn.close()
        assert rows == [("Artist/song.flac",)]

    def test_legacy_duplicate_migration_keeps_analysed_target(self, tmp_path) -> None:
        import sqlite3 as _sql

        path = tmp_path / "cache.db"
        music_dir = tmp_path / "Music"
        legacy = "/volume1/Mike/Beetsmusic/Artist/song.flac"

        conn = _sql.connect(path)
        conn.execute(DjMetaCache._SCHEMA)
        conn.executemany(
            "INSERT INTO dj_meta (path, intro_end_s, outro_start_s, analysed, beats, cues) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("Artist/song.flac", 2.0, 90.0, 1, "[]", "[]"),
                (legacy, 4.0, 120.0, 0, "[]", "[]"),
            ],
        )
        conn.commit()
        conn.close()

        cache = DjMetaCache(
            path,
            music_dir=music_dir,
            path_remap=[("/volume1/Mike/Beetsmusic/", f"{music_dir.as_posix()}/")],
        )
        meta = cache.get("Artist/song.flac")
        assert meta.analysed is True
        assert meta.intro_end_s == 2.0
        cache.close()

    def test_prune_to_paths_returns_zero_when_nothing_stale(self, tmp_path) -> None:
        path = tmp_path / "cache.db"
        music_dir = tmp_path / "Music"
        song = music_dir / "Artist" / "song.flac"
        cache = DjMetaCache(path, music_dir=music_dir)
        cache.set(str(song), DjMeta(analysed=True))
        cache.flush(force=True)

        removed = cache.prune_to_paths({str(song)})

        assert removed == 0
        assert cache.get(str(song)).analysed is True
        cache.close()


# ---------------------------------------------------------------------------
# harmonic_compatible — extended modes (added with the harmonic combo box)
# ---------------------------------------------------------------------------


class TestHarmonicCompatibleModes:
    def test_off_mode_always_true(self) -> None:
        from autodj.dj_meta import harmonic_compatible

        # 8A vs 1B — clearly incompatible under classic rule
        assert harmonic_compatible(0, 0, 11, 1, mode="off")

    def test_strict_requires_exact_position(self) -> None:
        from autodj.dj_meta import harmonic_compatible

        # 8B (C major) -> 8B should pass
        assert harmonic_compatible(0, 1, 0, 1, mode="strict")
        # 8B -> 9B should fail
        assert not harmonic_compatible(0, 1, 7, 1, mode="strict")

    def test_neighbour_same_side_only(self) -> None:
        from autodj.dj_meta import harmonic_compatible

        # 8B (C major) -> 9B (G major) — same side, +1 — pass
        assert harmonic_compatible(0, 1, 7, 1, mode="neighbour")
        # 8B -> 8A (relative minor) — opposite side — fail
        assert not harmonic_compatible(0, 1, 0, 0, mode="neighbour")

    def test_mood_change_only_allows_relative(self) -> None:
        from autodj.dj_meta import harmonic_compatible

        # 8B (C major, key=0 mode=1) -> 8A (A minor, key=9 mode=0):
        # same Camelot number, opposite side — the relative minor — pass.
        assert harmonic_compatible(0, 1, 9, 0, mode="mood_change")
        # Same position 8B -> 8B → fail (not a mood change)
        assert not harmonic_compatible(0, 1, 0, 1, mode="mood_change")

    def test_energy_boost_plus_two(self) -> None:
        from autodj.dj_meta import harmonic_compatible

        # 8B (C major, num=8) -> 10B (D major, num=10) — same side, +2 — pass
        assert harmonic_compatible(0, 1, 2, 1, mode="energy_boost")
        # +1 should fail under energy boost (not a 2-step jump)
        assert not harmonic_compatible(0, 1, 7, 1, mode="energy_boost")

    def test_unknown_mode_falls_back_to_compatible(self) -> None:
        from autodj.dj_meta import harmonic_compatible

        # 8B (C major) and 8A (A minor) are relative pair → compatible.
        assert harmonic_compatible(0, 1, 9, 0, mode="bogus")

    def test_unknown_keys_always_compatible(self) -> None:
        from autodj.dj_meta import harmonic_compatible

        for mode in ("off", "compatible", "strict", "neighbour", "mood_change", "energy_boost"):
            assert harmonic_compatible(-1, -1, 7, 1, mode=mode)


class TestDetectIntroOutroEdges:
    def test_silent_audio_returns_zero_duration(self) -> None:
        from autodj.dj_meta import detect_intro_outro

        audio = np.zeros(44100 * 5, dtype=np.float32)
        intro, _outro = detect_intro_outro(audio, 44100)
        assert intro == 0.0

    def test_constant_loud_audio_falls_back(self) -> None:
        from autodj.dj_meta import detect_intro_outro

        audio = np.ones(44100 * 5, dtype=np.float32) * 0.5
        intro, outro = detect_intro_outro(audio, 44100)
        assert intro >= 0.0
        assert outro >= intro

    def test_empty_audio(self) -> None:
        from autodj.dj_meta import detect_intro_outro

        intro, _outro = detect_intro_outro(np.zeros(0, dtype=np.float32), 44100)
        assert intro == 0.0


class TestNearestPhraseBoundaryExtra:
    def test_empty_beats(self) -> None:
        from autodj.dj_meta import nearest_phrase_boundary

        assert nearest_phrase_boundary([], 10.0) is None

    def test_too_few_beats_returns_none(self) -> None:
        from autodj.dj_meta import nearest_phrase_boundary

        assert nearest_phrase_boundary([0.5, 1.0, 1.5], 10.0, bars=8) is None

    def test_close_target_returns_boundary(self) -> None:
        from autodj.dj_meta import nearest_phrase_boundary

        beats = [i * 0.5 for i in range(64)]
        result = nearest_phrase_boundary(beats, 15.5, bars=8)
        assert result is not None
        assert abs(result - 16.0) < 0.5

    def test_target_far_from_any_boundary_returns_none(self) -> None:
        from autodj.dj_meta import nearest_phrase_boundary

        beats = [i * 0.5 for i in range(64)]
        result = nearest_phrase_boundary(beats, 1000.0, bars=8)
        assert result is None


class TestCamelotPositionEdges:
    def test_invalid_key_returns_none(self) -> None:
        from autodj.dj_meta import camelot_position

        assert camelot_position(-1, 1) is None
        assert camelot_position(12, 1) is None

    def test_invalid_mode_returns_none(self) -> None:
        from autodj.dj_meta import camelot_position

        assert camelot_position(0, -1) is None
        assert camelot_position(0, 2) is None


class TestDjMetaCacheExtra:
    def test_flush_force_with_no_pending_writes_is_noop(self, tmp_path) -> None:
        from autodj.dj_meta import DjMetaCache

        path = tmp_path / "cache.db"
        cache = DjMetaCache(path)
        cache.flush(force=True)  # no writes pending; must not raise
        # Round-trip survives the no-op flush.
        cache.set("a.flac", DjMeta(analysed=True))
        cache.flush(force=True)
        cache.close()
        cache2 = DjMetaCache(path)
        assert cache2.get("a.flac").analysed is True

    def test_get_uses_mem_cache_on_second_call(self, tmp_path) -> None:
        from autodj.dj_meta import DjMetaCache

        path = tmp_path / "cache.db"
        cache = DjMetaCache(path)
        # First call: miss, returns empty DjMeta and caches it.
        first = cache.get("unknown.flac")
        # Second call: hits self._mem_cache (different code path).
        second = cache.get("unknown.flac")
        assert first is second
        cache.close()

    def test_row_to_meta_handles_corrupt_blob(self, tmp_path) -> None:
        import sqlite3 as _sql

        from autodj.dj_meta import DjMetaCache

        path = tmp_path / "cache.db"
        # Pre-seed a row whose beats/cues columns hold invalid JSON.
        conn = _sql.connect(path)
        conn.execute(DjMetaCache._SCHEMA)
        conn.execute(
            "INSERT INTO dj_meta (path, intro_end_s, outro_start_s, analysed, "
            "beats, cues) VALUES (?, ?, ?, ?, ?, ?)",
            ("x.flac", 0.0, 0.0, 1, "{not json", "also {bad}"),
        )
        conn.commit()
        conn.close()

        cache = DjMetaCache(path)
        meta = cache.get("x.flac")
        # Corrupt JSON falls back to empty lists rather than blowing up.
        assert meta.beats == []
        assert meta.cues == []
        cache.close()


class TestGetCacheAndAnalyse:
    def test_get_cache_initialises_once(self, tmp_path) -> None:
        from autodj import dj_meta as _dm

        _dm._CACHE = None
        c1 = _dm.get_cache(tmp_path)
        c2 = _dm.get_cache(tmp_path)
        assert c1 is c2
        assert _dm.get_cache(None) is c1

    def test_get_cache_none_when_uninitialised(self) -> None:
        from autodj import dj_meta as _dm

        _dm._CACHE = None
        assert _dm.get_cache(None) is None

    def test_analyse_audio_returns_populated_djmeta(self) -> None:
        from autodj.dj_meta import analyse_audio

        rng = np.random.default_rng(0)
        audio = rng.standard_normal(44100 * 3).astype(np.float32) * 0.1
        meta = analyse_audio(audio, 44100)
        assert meta.analysed is True


class TestDjMetaInternalHelpers:
    """Cover the small private helpers in dj_meta that detect cues."""

    def test_detect_beat_grid_returns_empty_for_short_audio(self) -> None:
        from autodj.dj_meta import detect_beat_grid

        # < 1 second → early-return empty list (line 190)
        audio = np.zeros(100, dtype=np.float32)
        assert detect_beat_grid(audio, 44100) == []

    def test_gpu_onset_envelope_disabled_via_env(self, monkeypatch) -> None:
        from autodj.dj_meta import _gpu_onset_envelope

        monkeypatch.setenv("AUTODJ_DJMETA_GPU", "0")
        audio = np.zeros(1024, dtype=np.float32)
        assert _gpu_onset_envelope(audio, 22050) is None

    def test_gpu_onset_envelope_returns_none_when_gpu_unavailable(self, monkeypatch) -> None:
        from autodj import dj_meta as _dm

        monkeypatch.setenv("AUTODJ_DJMETA_GPU", "1")
        monkeypatch.setattr(_dm, "logger", _dm.logger)
        # Force gpu_available() to return False
        import autodj.compute as _compute

        monkeypatch.setattr(_compute, "gpu_available", lambda: False)
        audio = np.zeros(1024, dtype=np.float32)
        assert _dm._gpu_onset_envelope(audio, 22050) is None

    def test_detect_first_downbeat_no_match_returns_none(self) -> None:
        from autodj.dj_meta import _detect_first_downbeat

        # All beats below intro_end → no downbeat ≥ intro_end
        beats = [0.0, 0.5, 1.0, 1.5, 2.0]
        assert _detect_first_downbeat(beats, intro_end_s=10.0) is None

    def test_detect_first_downbeat_finds_first_aligned(self) -> None:
        from autodj.dj_meta import _detect_first_downbeat

        beats = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
        cue = _detect_first_downbeat(beats, intro_end_s=1.0)
        assert cue is not None
        # Index 4 (2.0s) is the first index%4==0 that is ≥ 1.0
        assert cue.time_s == 2.0
        assert cue.type == "first_downbeat"

    def test_detect_drop_too_short_window_returns_none(self) -> None:
        from autodj.dj_meta import _detect_drop

        rms = np.array([0.1, 0.2, 0.1], dtype=np.float32)
        rolling = np.array([0.1, 0.1, 0.1], dtype=np.float32)
        # search window 0..3 = 3, less than 4 → None (line 756)
        assert _detect_drop(rms, rolling, [], 0.0, 1.5) is None

    def test_detect_drop_finds_spike(self) -> None:
        from autodj.dj_meta import _detect_drop

        # 10 blocks, with a spike at index 5 that is 2x baseline
        rms = np.array([0.1, 0.1, 0.1, 0.1, 0.1, 0.3, 0.1, 0.1, 0.1, 0.1], dtype=np.float32)
        rolling = np.full(10, 0.1, dtype=np.float32)
        cue = _detect_drop(rms, rolling, [], 0.0, 0.0)
        assert cue is not None
        assert cue.type == "drop"

    def test_longest_run_open_at_end(self) -> None:
        from autodj.dj_meta import _longest_run

        # Run continues to the end of the array (line 781)
        below = np.array([False, False, True, True, True], dtype=bool)
        start, end = _longest_run(below)
        assert start == 2
        assert end == 5

    def test_longest_run_no_true(self) -> None:
        from autodj.dj_meta import _longest_run

        below = np.array([False, False, False], dtype=bool)
        assert _longest_run(below) == (0, 0)

    def test_detect_breakdown_too_short(self) -> None:
        from autodj.dj_meta import _detect_breakdown

        # Less than 4s body window → None (line 794)
        rms = np.array([0.5] * 6, dtype=np.float32)
        assert _detect_breakdown(rms, 0.0, 0.0) is None

    def test_detect_breakdown_finds_dip(self) -> None:
        from autodj.dj_meta import _detect_breakdown

        # 60 blocks (30 s).  Middle third (20..40) has a deep sustained dip.
        rms = np.full(60, 0.5, dtype=np.float32)
        rms[25:35] = 0.05  # sustained quiet patch, well below half of median
        cue = _detect_breakdown(rms, 0.0, 0.0)
        assert cue is not None
        assert cue.type == "breakdown"

    def test_detect_phrases_too_few_beats(self) -> None:
        from autodj.dj_meta import _detect_phrases

        # Fewer than 32 beats → returns [] (line 812)
        assert _detect_phrases([0.0, 0.5, 1.0], 0.0, 0.0, 10.0) == []

    def test_detect_phrases_emits_per_32_beats(self) -> None:
        from autodj.dj_meta import _detect_phrases

        beats = [i * 0.5 for i in range(96)]  # 48 seconds of half-second beats
        cues = _detect_phrases(beats, intro_end_s=0.0, outro_start_s=40.0, duration=48.0)
        # indices 0, 32, 64 → 0.0, 16.0, 32.0 (32.0 still ≤ horizon 40.0)
        times = [c.time_s for c in cues]
        assert 0.0 in times
        assert 16.0 in times
        # 32.0 should be in (≤ horizon=40)
        assert 32.0 in times

    def test_detect_outro_downbeat_no_beats_returns_none(self) -> None:
        from autodj.dj_meta import _detect_outro_downbeat

        assert _detect_outro_downbeat([], 0.0, 10.0) is None

    def test_detect_outro_downbeat_outro_zero_returns_none(self) -> None:
        from autodj.dj_meta import _detect_outro_downbeat

        assert _detect_outro_downbeat([0.0, 0.5, 1.0], 0.0, 0.0) is None

    def test_detect_outro_downbeat_when_last_in_intro_returns_none(self) -> None:
        from autodj.dj_meta import _detect_outro_downbeat

        # All downbeats are ≤ intro_end_s → last_db <= intro_end → None (line 834)
        beats = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
        # downbeat indices 0,4 → times 0.0, 2.0.  intro_end_s = 5.0, outro = 6.0
        assert _detect_outro_downbeat(beats, intro_end_s=5.0, outro_start_s=6.0) is None

    def test_detect_outro_downbeat_finds_last_before_outro(self) -> None:
        from autodj.dj_meta import _detect_outro_downbeat

        beats = [i * 0.5 for i in range(20)]
        # downbeats at 0, 2, 4, ... outro at 7 → last_db = 6.0
        cue = _detect_outro_downbeat(beats, intro_end_s=1.0, outro_start_s=7.0)
        assert cue is not None
        assert cue.time_s == 6.0
        assert cue.type == "outro_downbeat"

    def test_detect_cues_short_audio_returns_empty(self) -> None:
        from autodj.dj_meta import detect_cues

        audio = np.zeros(100, dtype=np.float32)
        assert detect_cues(audio, 44100, 0.0, 0.0, []) == []

    def test_detect_cues_silent_audio_returns_empty(self) -> None:
        from autodj.dj_meta import detect_cues

        audio = np.zeros(44100 * 5, dtype=np.float32)
        assert detect_cues(audio, 44100, 0.0, 0.0, []) == []

    def test_merge_cues_priority_overrides_auto(self) -> None:
        from autodj.dj_meta import Cue, merge_cues

        auto = [Cue(time_s=10.0, type="drop", source="auto")]
        user = [Cue(time_s=10.05, type="drop", source="user")]
        merged = merge_cues(auto, user)
        # The two cues are within 250 ms; user beats auto.
        assert len(merged) == 1
        assert merged[0].source == "user"

    def test_merge_cues_keeps_separate_cues(self) -> None:
        from autodj.dj_meta import Cue, merge_cues

        a = [Cue(time_s=10.0, type="drop", source="auto")]
        b = [Cue(time_s=20.0, type="drop", source="auto")]
        merged = merge_cues(a, b)
        assert len(merged) == 2


# ---------------------------------------------------------------------------
# autodj.dj_meta — _hm_energy_boost harmonic mode branches
# ---------------------------------------------------------------------------


class TestDjMetaHarmonic:
    def test_energy_boost_different_sides_returns_false(self) -> None:
        from autodj.dj_meta import _hm_energy_boost

        # pos_a side 'A', pos_b side 'B' — must short-circuit to False
        assert _hm_energy_boost((1, "A"), (1, "B")) is False

    def test_energy_boost_same_side_within_range(self) -> None:
        from autodj.dj_meta import _hm_energy_boost

        assert _hm_energy_boost((1, "A"), (3, "A")) is True
        # diff == 10 wraps
        assert _hm_energy_boost((1, "A"), (11, "A")) is True

    def test_energy_boost_same_side_too_far(self) -> None:
        from autodj.dj_meta import _hm_energy_boost

        assert _hm_energy_boost((1, "A"), (5, "A")) is False
