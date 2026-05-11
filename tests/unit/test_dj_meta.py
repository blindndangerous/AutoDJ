"""Tests for autodj.dj_meta — Camelot wheel, intro/outro, beats, cache."""

from __future__ import annotations

import json

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

    def test_legacy_json_migrates_on_open(self, tmp_path) -> None:
        legacy = tmp_path / "cache.json"
        legacy.write_text(
            json.dumps(
                {
                    "foo.flac": {
                        "intro_end_s": 3.0,
                        "outro_start_s": 150.0,
                        "beats": [0.4, 0.8],
                        "analysed": True,
                    }
                }
            ),
            encoding="utf-8",
        )
        # Passing the .json path triggers the auto-resolve-to-.db + migration.
        cache = DjMetaCache(legacy)
        meta = cache.get("foo.flac")
        assert meta.intro_end_s == 3.0
        assert meta.outro_start_s == 150.0
        assert meta.beats == [0.4, 0.8]
        # Legacy file is renamed, db is now authoritative.
        assert not legacy.exists()
        assert (tmp_path / "cache.json.legacy.bak").exists()
        assert (tmp_path / "cache.db").exists()

    def test_corrupt_legacy_json_starts_empty(self, tmp_path) -> None:
        legacy = tmp_path / "cache.json"
        legacy.write_text("not json {{{{", encoding="utf-8")
        cache = DjMetaCache(legacy)  # should not raise
        assert cache.get("foo.flac").analysed is False


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
    def test_migration_skips_malformed_legacy_entries(self, tmp_path) -> None:
        from autodj.dj_meta import DjMetaCache

        legacy = tmp_path / "cache.json"
        # Second entry is missing the required field shape so the migrator
        # falls back to defaults for it (no crash on partial data).
        legacy.write_text(
            '{"good.flac": {"intro_end_s": 1.0, "outro_start_s": 2.0, '
            '"beats": [], "analysed": true}, '
            '"weird.flac": {"unknown_field": "x"}, '
            '"not_a_dict.flac": "garbage"}',
            encoding="utf-8",
        )
        cache = DjMetaCache(legacy)
        assert cache.get("good.flac").analysed is True
        # "weird" landed with all-default fields (analysed=False).
        assert cache.get("weird.flac").analysed is False
        # The non-dict value was skipped entirely.
        assert cache.get("not_a_dict.flac").analysed is False

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
