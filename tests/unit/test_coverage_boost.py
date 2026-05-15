"""Targeted coverage tests for thin defensive branches.

Each test in this module covers a single, previously-untested branch
identified by line-coverage analysis.  Tests are intentionally small
and unit-scoped so they stay fast and don't depend on heavy fixtures.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# autodj.compute  (lines 38, 52, 58)
# ---------------------------------------------------------------------------


class TestCompute:
    def test_global_disabled_via_env_var(self) -> None:
        from autodj import compute

        compute.reset_probe_cache()
        with patch.dict(os.environ, {"AUTODJ_GPU": "0"}):
            assert compute.gpu_available() is False
            assert compute.device_string() == "cpu"
        compute.reset_probe_cache()

    def test_device_string_returns_cuda_when_gpu_available(self) -> None:
        from autodj import compute

        compute.reset_probe_cache()
        with patch.object(compute, "gpu_available", return_value=True):
            assert compute.device_string() == "cuda"

    def test_reset_probe_cache_clears_state(self) -> None:
        from autodj import compute

        compute._PROBE_CACHE = True
        compute.reset_probe_cache()
        assert compute._PROBE_CACHE is None


# ---------------------------------------------------------------------------
# autodj.config  (lines 72-76 — invalid path_remap pair)
# ---------------------------------------------------------------------------


class TestConfigPathRemapValidation:
    def test_invalid_path_remap_entry_not_list_raises(self) -> None:
        from autodj.config import LibraryConfig

        with pytest.raises(ValueError, match="path_remap"):
            LibraryConfig.from_dict(
                {"music_dir": "/x", "path_remap": ["not-a-pair"]},
            )

    def test_invalid_path_remap_wrong_length_raises(self) -> None:
        from autodj.config import LibraryConfig

        with pytest.raises(ValueError, match="path_remap"):
            LibraryConfig.from_dict(
                {"music_dir": "/x", "path_remap": [["only-one"]]},
            )

    def test_valid_path_remap_passes_through(self) -> None:
        from autodj.config import LibraryConfig

        cfg = LibraryConfig.from_dict(
            {"music_dir": "/x", "path_remap": [["/a", "/b"]]},
        )
        assert cfg.path_remap == [("/a", "/b")]


# ---------------------------------------------------------------------------
# autodj.beets  (lines 126, 153, 174)
# ---------------------------------------------------------------------------


class TestBeetsHelpers:
    def test_camelot_key_with_invalid_number_returns_none(self) -> None:
        from autodj.beets import _parse_camelot_key

        # Out-of-range numbers
        assert _parse_camelot_key("0A") is None
        assert _parse_camelot_key("13B") is None

    def test_split_note_and_mode_empty_after_strip(self) -> None:
        from autodj.beets import parse_initial_key

        # 'major' alone becomes empty note_part — should return None (line 153)
        assert parse_initial_key("major") is None
        assert parse_initial_key("minor") is None

    def test_decode_path_from_str(self) -> None:
        from autodj.beets import _decode_path

        # String input branch (line 174)
        result = _decode_path("/some/path/track.mp3")
        assert isinstance(result, Path)
        assert str(result).replace("\\", "/") == "/some/path/track.mp3"

    def test_decode_path_from_bytes(self) -> None:
        from autodj.beets import _decode_path

        result = _decode_path(b"/some/path/track.mp3")
        assert isinstance(result, Path)


# ---------------------------------------------------------------------------
# autodj.audio_meta defensive branches (493, 499)
# ---------------------------------------------------------------------------


class TestAudioMetaDefensive:
    def test_id3_get_returns_none_when_tags_missing(self) -> None:
        from autodj.audio_meta import _id3_get

        m = SimpleNamespace(tags=None)
        assert _id3_get(m, "TIT2") is None

    def test_id3_get_returns_none_when_value_is_none(self) -> None:
        from autodj.audio_meta import _id3_get

        class _Tags:
            def __getitem__(self, key: str) -> None:
                return None

        m = SimpleNamespace(tags=_Tags())
        assert _id3_get(m, "TIT2") is None

    def test_id3_get_returns_none_on_keyerror(self) -> None:
        from autodj.audio_meta import _id3_get

        class _Tags:
            def __getitem__(self, key: str) -> str:
                raise KeyError(key)

        m = SimpleNamespace(tags=_Tags())
        assert _id3_get(m, "TIT2") is None


# ---------------------------------------------------------------------------
# autodj.runtime_state — _restore_validated_strings invalid branches
# ---------------------------------------------------------------------------


class TestRuntimeStateValidation:
    def test_invalid_transition_mode_logged_not_raised(self, caplog) -> None:
        from autodj.runtime_state import _restore_validated_strings

        cfg = MagicMock()
        cfg.playback.transition_mode = "full_intro_outro"
        with caplog.at_level("WARNING"):
            _restore_validated_strings(cfg, {"transition_mode": "garbage-mode"})
        assert any("transition_mode" in r.message for r in caplog.records)
        # Unchanged
        assert cfg.playback.transition_mode == "full_intro_outro"

    def test_invalid_key_notation_logged_not_raised(self, caplog) -> None:
        from autodj.runtime_state import _restore_validated_strings

        cfg = MagicMock()
        cfg.playback.key_notation = "camelot"
        with caplog.at_level("WARNING"):
            _restore_validated_strings(cfg, {"key_notation": "alien"})
        assert any("key_notation" in r.message for r in caplog.records)

    def test_valid_transition_mode_applied(self) -> None:
        from autodj.runtime_state import _restore_validated_strings

        cfg = MagicMock()
        _restore_validated_strings(cfg, {"transition_mode": "fixed"})
        assert cfg.playback.transition_mode == "fixed"


# ---------------------------------------------------------------------------
# autodj.dj_meta — _hm_energy_boost different side (line 358)
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


# ---------------------------------------------------------------------------
# autodj.indexer — minor-key branch + tempo confidence fallback
# (lines 576-577, 586-587)
# ---------------------------------------------------------------------------


class TestIndexerExtract:
    def test_minor_branch_is_lp_infeasible(self) -> None:
        """Document why the minor branch is marked ``# pragma: no cover``.

        For every rotation of the minor template, the major template has a
        rotation whose dot product is at least as large.  An LP search
        across non-negative chromas confirms no feasible point — the
        ``else`` branch in ``_extract_librosa_features`` is dead code.
        """
        import numpy as np
        from scipy.optimize import linprog

        major = np.array([1, 0, 1, 0, 1, 1, 0, 1, 0, 1, 0, 1], dtype=np.float32)
        minor = np.array([1, 0, 1, 1, 0, 1, 0, 1, 1, 0, 1, 0], dtype=np.float32)
        feasible = False
        for k in range(12):
            target = np.roll(minor, k)
            a_ub = np.array([np.roll(major, j) - target for j in range(12)])
            res = linprog(
                c=-target,
                A_ub=a_ub,
                b_ub=-1e-3 * np.ones(12),
                bounds=[(0, 1)] * 12,
            )
            if res.success and -res.fun > 0:
                feasible = True
                break
        assert feasible is False

    def test_tempo_confidence_exception_fallback(self) -> None:
        """beat_track raising means tempo_confidence falls back to 0.0."""
        import numpy as np

        from autodj import indexer

        with patch.object(indexer, "_load_audio") as load, patch.object(indexer, "librosa") as lib:
            load.return_value = (np.ones(1024, dtype=np.float32), 22050)
            lib.feature.rms.return_value = np.array([[0.5]])
            lib.feature.spectral_centroid.return_value = np.array([[1000.0]])
            lib.feature.zero_crossing_rate.return_value = np.array([[0.1]])
            lib.feature.chroma_stft.return_value = np.ones((12, 4), dtype=np.float32)
            lib.onset.onset_strength.return_value = np.array([0.5])
            lib.beat.beat_track.side_effect = RuntimeError("librosa failed")
            _, _, _, meta = indexer._extract_librosa_features(Path("dummy.flac"))
        assert meta["tempo_confidence"] == 0.0

    def test_extract_raises_on_empty_audio(self) -> None:
        import numpy as np

        from autodj import indexer

        with patch.object(indexer, "_load_audio") as load:
            load.return_value = (np.array([], dtype=np.float32), 22050)
            with pytest.raises(ValueError, match="no samples"):
                indexer._extract_librosa_features(Path("dummy.flac"))
