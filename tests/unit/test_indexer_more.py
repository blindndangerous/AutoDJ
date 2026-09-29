"""Additional indexer unit tests targeting previously-uncovered branches.

Focus on the small pure-function helpers (``_apply_beets_row``,
``_find_beets_row``, ``_check_prune_safety`` happy paths) and on the
error-rollback paths in ``save_index``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from autodj.indexer import (
    IndexEntry,
    PruneSafetyError,
    _apply_beets_row,
    _check_prune_safety,
    _find_beets_row,
    load_index,
)


def _entry(**kw) -> IndexEntry:
    base = {
        "path": "Z:/Music/x.flac",
        "title": "t",
        "artist": "a",
        "album": "al",
        "genre": "g",
        "bpm": 120.0,
        "year": 2020,
        "length": 180.0,
        "energy": 0.05,
        "key": 0,
        "mode": 1,
        "tempo_confidence": 0.5,
    }
    base.update(kw)
    return IndexEntry(**base)


_TEXT_COLS = ("title", "artist", "album", "genre")


def _row(**kw) -> dict:
    """Return a beets row with every selected column present and neutral."""
    base = {
        "title": "",
        "artist": "",
        "album": "",
        "genre": "",
        "bpm": 0,
        "year": 0,
        "length": 0,
        "initial_key": "",
    }
    base.update(kw)
    return base


_NO_KEY = (False, lambda _s: None)


# ---------------------------------------------------------------------------
# _apply_beets_row — text columns
# ---------------------------------------------------------------------------


class TestApplyBeetsRowText:
    def test_empty_value_is_skipped(self) -> None:
        e = _entry(title="orig")
        assert _apply_beets_row(e, _row(), _TEXT_COLS, *_NO_KEY) is False
        assert e.title == "orig"

    def test_same_value_no_change(self) -> None:
        e = _entry(title="same")
        row = _row(title="same", artist="a", album="al", genre="g")
        assert _apply_beets_row(e, row, _TEXT_COLS, *_NO_KEY) is False

    def test_changed_value_returns_true(self) -> None:
        e = _entry(title="orig")
        row = _row(title="new", artist="a", album="al", genre="g")
        assert _apply_beets_row(e, row, _TEXT_COLS, *_NO_KEY) is True
        assert e.title == "new"

    def test_none_value_treated_as_empty(self) -> None:
        e = _entry(title="orig")
        row = _row(title=None, artist=None, album=None, genre=None)
        assert _apply_beets_row(e, row, _TEXT_COLS, *_NO_KEY) is False
        assert e.title == "orig"


# ---------------------------------------------------------------------------
# _apply_beets_row — numeric columns
# ---------------------------------------------------------------------------


class TestApplyBeetsRowNumeric:
    def test_zero_values_skipped(self) -> None:
        e = _entry(bpm=120.0, year=2020, length=180.0)
        assert _apply_beets_row(e, _row(), (), *_NO_KEY) is False
        assert e.bpm == 120.0

    def test_none_values_skipped(self) -> None:
        e = _entry()
        row = _row(bpm=None, year=None, length=None)
        assert _apply_beets_row(e, row, (), *_NO_KEY) is False

    def test_bpm_change_only(self) -> None:
        e = _entry(bpm=120.0)
        assert _apply_beets_row(e, _row(bpm=130.0), (), *_NO_KEY) is True
        assert e.bpm == 130.0

    def test_year_change_only(self) -> None:
        e = _entry(year=2000)
        assert _apply_beets_row(e, _row(year=2024), (), *_NO_KEY) is True
        assert e.year == 2024

    def test_length_change_only(self) -> None:
        e = _entry(length=180.0)
        assert _apply_beets_row(e, _row(length=240.5), (), *_NO_KEY) is True
        assert e.length == pytest.approx(240.5)

    def test_bpm_within_epsilon_no_change(self) -> None:
        e = _entry(bpm=120.0)
        assert _apply_beets_row(e, _row(bpm=120.0001), (), *_NO_KEY) is False


# ---------------------------------------------------------------------------
# _apply_beets_row — initial_key column
# ---------------------------------------------------------------------------


class TestApplyBeetsRowKey:
    def test_unparseable_returns_false(self) -> None:
        e = _entry(key=0, mode=1)
        row = _row(initial_key="garbage")
        assert _apply_beets_row(e, row, (), True, lambda _s: None) is False

    def test_same_key_no_change(self) -> None:
        e = _entry(key=5, mode=0)
        row = _row(initial_key="anything")
        assert _apply_beets_row(e, row, (), True, lambda _s: (5, 0)) is False

    def test_change_returns_true(self) -> None:
        e = _entry(key=0, mode=1)
        row = _row(initial_key="anything")
        assert _apply_beets_row(e, row, (), True, lambda _s: (7, 0)) is True
        assert (e.key, e.mode) == (7, 0)

    def test_none_initial_key_handled(self) -> None:
        e = _entry()
        row = _row(initial_key=None)
        # Parser receives "" -- exercises the str()-coerce branch.
        assert _apply_beets_row(e, row, (), True, lambda _s: None) is False


# ---------------------------------------------------------------------------
# _apply_beets_row — combined
# ---------------------------------------------------------------------------


class TestApplyBeetsRow:
    def test_no_change_returns_false(self) -> None:
        e = _entry(title="t", bpm=120.0, key=0, mode=1)
        assert _apply_beets_row(e, _row(), _TEXT_COLS, True, lambda _s: None) is False

    def test_text_change_only(self) -> None:
        e = _entry(title="old")
        row = _row(title="new", artist="a", album="al", genre="g")
        assert _apply_beets_row(e, row, _TEXT_COLS, *_NO_KEY) is True

    def test_numeric_change_only(self) -> None:
        e = _entry(bpm=100.0)
        assert _apply_beets_row(e, _row(bpm=130.0), _TEXT_COLS, *_NO_KEY) is True

    def test_initial_key_change_only(self) -> None:
        e = _entry(key=0, mode=1)
        row = _row(initial_key="Cm")
        assert _apply_beets_row(e, row, _TEXT_COLS, True, lambda _s: (0, 0)) is True

    def test_initial_key_disabled_when_column_absent(self) -> None:
        e = _entry(key=0, mode=1)
        row = _row()
        del row["initial_key"]
        # has_initial_key=False, so row["initial_key"] is never read.
        assert _apply_beets_row(e, row, _TEXT_COLS, False, lambda _s: (0, 0)) is False


# ---------------------------------------------------------------------------
# _find_beets_row
# ---------------------------------------------------------------------------


class TestFindBeetsRow:
    def test_returns_row_when_candidate_matches(self) -> None:
        rows = {b"Z:/Music/x.flac": {"title": "X"}}

        def candidates(p: str, _md):
            return ["Z:/Music/x.flac"]

        result = _find_beets_row("Z:/Music/x.flac", None, rows, candidates)
        assert result == {"title": "X"}

    def test_returns_none_when_no_match(self) -> None:
        rows = {b"Z:/Music/y.flac": {"title": "Y"}}

        def candidates(p: str, _md):
            return ["Z:/Music/x.flac"]

        result = _find_beets_row("Z:/Music/x.flac", None, rows, candidates)
        assert result is None

    def test_tries_multiple_candidates(self) -> None:
        rows = {b"second": {"title": "Found"}}

        def candidates(p: str, _md):
            return ["first", "second"]

        assert _find_beets_row("ignored", None, rows, candidates) == {"title": "Found"}


# ---------------------------------------------------------------------------
# _check_prune_safety
# ---------------------------------------------------------------------------


class TestCheckPruneSafety:
    def test_zero_total_is_safe(self) -> None:
        _check_prune_safety(0, 0, allow_mass_prune=False)  # no raise

    def test_under_threshold_is_safe(self) -> None:
        _check_prune_safety(1, 100, allow_mass_prune=False)

    def test_over_threshold_raises(self) -> None:
        with pytest.raises(PruneSafetyError):
            _check_prune_safety(50, 100, allow_mass_prune=False)

    def test_mass_prune_override(self) -> None:
        _check_prune_safety(50, 100, allow_mass_prune=True)


# ---------------------------------------------------------------------------
# _delete_index_files
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# save_index error rollback paths
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# load_index missing-file branch (FileNotFoundError at line 1053)
# ---------------------------------------------------------------------------


class TestLoadIndexMissingFiles:
    def test_dir_exists_but_files_missing_raises(self, tmp_path: Path) -> None:
        idx = tmp_path / "idx"
        idx.mkdir()
        # No tracks.db or vectors.index
        with pytest.raises(FileNotFoundError):
            load_index(idx)

    def test_dir_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_index(tmp_path / "no-such-dir")


# ---------------------------------------------------------------------------
# autodj.indexer — minor-key branch + tempo confidence fallback
# ---------------------------------------------------------------------------


class TestIndexerExtract:
    def test_weighted_profiles_distinguish_c_major_and_a_minor(self) -> None:
        import numpy as np

        from autodj.indexer import _estimate_key_from_chroma

        c_major = np.array(
            [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88],
            dtype=np.float32,
        )
        a_minor = np.roll(
            np.array(
                [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17],
                dtype=np.float32,
            ),
            9,
        )

        assert _estimate_key_from_chroma(c_major) == (0, 1)
        assert _estimate_key_from_chroma(a_minor) == (9, 0)

    def test_key_estimation_preserves_unknown_for_insufficient_evidence(self) -> None:
        import numpy as np

        from autodj.indexer import _estimate_key_from_chroma

        assert _estimate_key_from_chroma(np.zeros(12, dtype=np.float32)) == (-1, -1)
        assert _estimate_key_from_chroma(np.full(12, np.nan, dtype=np.float32)) == (-1, -1)

    def test_key_estimation_rejects_weak_noisy_chroma(self) -> None:
        import numpy as np

        from autodj.indexer import _estimate_key_from_chroma

        noisy = np.array(
            [1.00, 0.99, 1.01, 1.00, 0.98, 1.02, 1.00, 0.99, 1.01, 1.00, 0.98, 1.02],
            dtype=np.float32,
        )
        assert _estimate_key_from_chroma(noisy) == (-1, -1)

    def test_key_estimation_rejects_negative_chroma_bins(self) -> None:
        import numpy as np

        from autodj.indexer import _estimate_key_from_chroma

        invalid = np.array(
            [6.35, -0.50, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88],
            dtype=np.float32,
        )
        assert _estimate_key_from_chroma(invalid) == (-1, -1)

    def test_extract_returns_estimated_bpm_metadata(self) -> None:
        from pathlib import Path

        import numpy as np

        from autodj import indexer

        with patch.object(indexer, "_load_audio") as load, patch.object(indexer, "librosa") as lib:
            load.return_value = (np.ones(22050, dtype=np.float32), 22050)
            lib.feature.rms.return_value = np.array([[0.5]])
            lib.feature.spectral_centroid.return_value = np.array([[1000.0]])
            lib.feature.zero_crossing_rate.return_value = np.array([[0.1]])
            lib.feature.chroma_stft.return_value = np.tile(
                np.array(
                    [
                        [6.35],
                        [2.23],
                        [3.48],
                        [2.33],
                        [4.38],
                        [4.09],
                        [2.52],
                        [5.19],
                        [2.39],
                        [3.66],
                        [2.29],
                        [2.88],
                    ]
                ),
                (1, 4),
            )
            lib.onset.onset_strength.return_value = np.array([0.5])
            lib.beat.beat_track.return_value = (np.array([123.0]), np.array([0, 10]))

            _, _, _, meta = indexer._extract_librosa_features(Path("dummy.flac"))

        assert meta["bpm"] == 123.0
        assert meta["key"] == 0
        assert meta["mode"] == 1

    @pytest.mark.parametrize("invalid_tempo", [np.nan, np.inf, -1.0, 0.0])
    def test_invalid_tempo_has_zero_bpm_and_confidence(self, invalid_tempo: float) -> None:
        from pathlib import Path

        import numpy as np

        from autodj import indexer

        with patch.object(indexer, "_load_audio") as load, patch.object(indexer, "librosa") as lib:
            load.return_value = (np.ones(22050, dtype=np.float32), 22050)
            lib.feature.rms.return_value = np.array([[0.5]])
            lib.feature.spectral_centroid.return_value = np.array([[1000.0]])
            lib.feature.zero_crossing_rate.return_value = np.array([[0.1]])
            lib.feature.chroma_stft.return_value = np.ones((12, 4), dtype=np.float32)
            lib.onset.onset_strength.return_value = np.array([0.5])
            lib.beat.beat_track.return_value = (
                np.array([invalid_tempo]),
                np.array([0, 10]),
            )

            _, _, _, meta = indexer._extract_librosa_features(Path("dummy.flac"))

        assert meta["bpm"] == 0.0
        assert meta["tempo_confidence"] == 0.0

    def test_tempo_confidence_exception_fallback(self) -> None:
        """beat_track raising means tempo_confidence falls back to 0.0."""
        from pathlib import Path

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
        from pathlib import Path

        import numpy as np
        import pytest

        from autodj import indexer

        with patch.object(indexer, "_load_audio") as load:
            load.return_value = (np.array([], dtype=np.float32), 22050)
            with pytest.raises(ValueError, match="no samples"):
                indexer._extract_librosa_features(Path("dummy.flac"))


def test_index_entry_prefers_tag_bpm_and_falls_back_to_estimate() -> None:
    from pathlib import Path

    from autodj.indexer import IndexEntry, Track, _apply_analysis_metadata

    tagged = IndexEntry.from_track(
        Track(
            path=Path("tagged.flac"),
            title="Tagged",
            artist="Artist",
            album="Album",
            genre="Rock",
            bpm=128.0,
            year=2026,
            length=180.0,
        )
    )
    unknown = IndexEntry.from_track(
        Track(
            path=Path("unknown.flac"),
            title="Unknown",
            artist="Artist",
            album="Album",
            genre="Rock",
            bpm=0.0,
            year=2026,
            length=180.0,
        )
    )
    meta = {"energy": 0.4, "key": 9, "mode": 0, "tempo_confidence": 0.8, "bpm": 121.5}

    _apply_analysis_metadata(tagged, meta)
    _apply_analysis_metadata(unknown, meta)

    assert tagged.bpm == 128.0
    assert unknown.bpm == 121.5


@pytest.mark.parametrize("tag_bpm", [np.nan, np.inf, -np.inf])
def test_index_entry_treats_nonfinite_tag_bpm_as_unknown(tag_bpm: float) -> None:
    from autodj.indexer import _apply_analysis_metadata

    entry = _entry(bpm=tag_bpm)
    meta = {"energy": 0.4, "key": 9, "mode": 0, "tempo_confidence": 0.8, "bpm": 121.5}

    _apply_analysis_metadata(entry, meta)

    assert entry.bpm == 121.5


@pytest.mark.parametrize("tag_bpm", [np.nan, np.inf, -np.inf, -1.0, 0.0])
def test_invalid_tag_bpm_normalizes_to_zero_without_estimate(tag_bpm: float) -> None:
    from autodj.indexer import _apply_analysis_metadata

    entry = _entry(bpm=tag_bpm)
    meta = {"energy": 0.4, "key": 9, "mode": 0, "tempo_confidence": 0.8, "bpm": 0.0}

    _apply_analysis_metadata(entry, meta)

    assert entry.bpm == 0.0


@pytest.mark.parametrize("estimated_bpm", [np.nan, np.inf, -np.inf])
def test_nonfinite_estimated_bpm_is_never_stored(estimated_bpm: float) -> None:
    from autodj.indexer import _apply_analysis_metadata

    entry = _entry(bpm=0.0)
    meta = {
        "energy": 0.4,
        "key": 9,
        "mode": 0,
        "tempo_confidence": 0.8,
        "bpm": estimated_bpm,
    }

    _apply_analysis_metadata(entry, meta)

    assert entry.bpm == 0.0
