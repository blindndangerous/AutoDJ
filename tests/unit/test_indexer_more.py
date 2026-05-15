"""Additional indexer unit tests targeting previously-uncovered branches.

Focus on the small pure-function helpers (``_apply_text_fields``,
``_apply_numeric_fields``, ``_apply_beets_row``, ``_resolve_for_runtime``
remap branch, ``_maybe_migrate_paths``, ``_check_prune_safety`` happy
paths) and on the error-rollback paths in ``save_index``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from autodj.indexer import (
    FEATURE_DIM,
    IndexEntry,
    PruneSafetyError,
    _apply_beets_row,
    _apply_key_field,
    _apply_numeric_fields,
    _apply_text_fields,
    _check_prune_safety,
    _delete_index_files,
    _find_beets_row,
    _is_relative_storage,
    _maybe_migrate_paths,
    _resolve_for_runtime,
    load_index,
    save_index,
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


# ---------------------------------------------------------------------------
# _apply_text_fields
# ---------------------------------------------------------------------------


class TestApplyTextFields:
    def test_empty_value_is_skipped(self) -> None:
        e = _entry(title="orig")
        row = {"title": "", "artist": "", "album": "", "genre": ""}
        assert _apply_text_fields(e, row, ("title", "artist", "album", "genre")) is False
        assert e.title == "orig"

    def test_same_value_no_change(self) -> None:
        e = _entry(title="same")
        row = {"title": "same", "artist": "a", "album": "al", "genre": "g"}
        assert _apply_text_fields(e, row, ("title", "artist", "album", "genre")) is False

    def test_changed_value_returns_true(self) -> None:
        e = _entry(title="orig")
        row = {"title": "new", "artist": "a", "album": "al", "genre": "g"}
        assert _apply_text_fields(e, row, ("title", "artist", "album", "genre")) is True
        assert e.title == "new"

    def test_none_value_treated_as_empty(self) -> None:
        e = _entry(title="orig")
        row = {"title": None, "artist": None, "album": None, "genre": None}
        assert _apply_text_fields(e, row, ("title", "artist", "album", "genre")) is False
        assert e.title == "orig"


# ---------------------------------------------------------------------------
# _apply_numeric_fields
# ---------------------------------------------------------------------------


class TestApplyNumericFields:
    def test_zero_values_skipped(self) -> None:
        e = _entry(bpm=120.0, year=2020, length=180.0)
        row = {"bpm": 0, "year": 0, "length": 0}
        assert _apply_numeric_fields(e, row) is False
        assert e.bpm == 120.0

    def test_none_values_skipped(self) -> None:
        e = _entry()
        row = {"bpm": None, "year": None, "length": None}
        assert _apply_numeric_fields(e, row) is False

    def test_bpm_change_only(self) -> None:
        e = _entry(bpm=120.0)
        row = {"bpm": 130.0, "year": 0, "length": 0}
        assert _apply_numeric_fields(e, row) is True
        assert e.bpm == 130.0

    def test_year_change_only(self) -> None:
        e = _entry(year=2000)
        row = {"bpm": 0, "year": 2024, "length": 0}
        assert _apply_numeric_fields(e, row) is True
        assert e.year == 2024

    def test_length_change_only(self) -> None:
        e = _entry(length=180.0)
        row = {"bpm": 0, "year": 0, "length": 240.5}
        assert _apply_numeric_fields(e, row) is True
        assert e.length == pytest.approx(240.5)

    def test_bpm_within_epsilon_no_change(self) -> None:
        e = _entry(bpm=120.0)
        row = {"bpm": 120.0001, "year": 0, "length": 0}
        assert _apply_numeric_fields(e, row) is False


# ---------------------------------------------------------------------------
# _apply_key_field
# ---------------------------------------------------------------------------


class TestApplyKeyField:
    def test_unparseable_returns_false(self) -> None:
        e = _entry(key=0, mode=1)
        row = {"initial_key": "garbage"}
        assert _apply_key_field(e, row, lambda _s: None) is False

    def test_same_key_no_change(self) -> None:
        e = _entry(key=5, mode=0)
        row = {"initial_key": "anything"}
        assert _apply_key_field(e, row, lambda _s: (5, 0)) is False

    def test_change_returns_true(self) -> None:
        e = _entry(key=0, mode=1)
        row = {"initial_key": "anything"}
        assert _apply_key_field(e, row, lambda _s: (7, 0)) is True
        assert (e.key, e.mode) == (7, 0)

    def test_none_initial_key_handled(self) -> None:
        e = _entry()
        row = {"initial_key": None}
        # parser receives "" and may return None or a real value -- exercises str()-coerce branch
        assert _apply_key_field(e, row, lambda _s: None) is False


# ---------------------------------------------------------------------------
# _apply_beets_row
# ---------------------------------------------------------------------------


class TestApplyBeetsRow:
    def test_no_change_returns_false(self) -> None:
        e = _entry(title="t", bpm=120.0, key=0, mode=1)
        row = {
            "title": "",
            "artist": "",
            "album": "",
            "genre": "",
            "bpm": 0,
            "year": 0,
            "length": 0,
            "initial_key": "",
        }
        assert (
            _apply_beets_row(e, row, ("title", "artist", "album", "genre"), True, lambda _s: None)
            is False
        )

    def test_text_change_only(self) -> None:
        e = _entry(title="old")
        row = {
            "title": "new",
            "artist": "a",
            "album": "al",
            "genre": "g",
            "bpm": 0,
            "year": 0,
            "length": 0,
            "initial_key": "",
        }
        assert (
            _apply_beets_row(e, row, ("title", "artist", "album", "genre"), False, lambda _s: None)
            is True
        )

    def test_numeric_change_only(self) -> None:
        e = _entry(bpm=100.0)
        row = {
            "title": "",
            "artist": "",
            "album": "",
            "genre": "",
            "bpm": 130.0,
            "year": 0,
            "length": 0,
            "initial_key": "",
        }
        assert (
            _apply_beets_row(e, row, ("title", "artist", "album", "genre"), False, lambda _s: None)
            is True
        )

    def test_initial_key_change_only(self) -> None:
        e = _entry(key=0, mode=1)
        row = {
            "title": "",
            "artist": "",
            "album": "",
            "genre": "",
            "bpm": 0,
            "year": 0,
            "length": 0,
            "initial_key": "Cm",
        }
        assert (
            _apply_beets_row(e, row, ("title", "artist", "album", "genre"), True, lambda _s: (0, 0))
            is True
        )

    def test_initial_key_disabled_when_column_absent(self) -> None:
        e = _entry(key=0, mode=1)
        row = {
            "title": "",
            "artist": "",
            "album": "",
            "genre": "",
            "bpm": 0,
            "year": 0,
            "length": 0,
        }
        # has_initial_key=False, so parse_initial_key never called -- no row["initial_key"] key needed
        assert (
            _apply_beets_row(
                e, row, ("title", "artist", "album", "genre"), False, lambda _s: (0, 0)
            )
            is False
        )


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
# _resolve_for_runtime — path_remap branch
# ---------------------------------------------------------------------------


class TestResolveForRuntimeRemap:
    def test_remap_swaps_prefix(self) -> None:
        result = _resolve_for_runtime(
            "/volume1/music/song.flac", None, [("/volume1/music", "Z:/Music")]
        )
        assert "Z:" in result.replace("\\", "/")
        assert "song.flac" in result.replace("\\", "/")

    def test_remap_only_first_match_wins(self) -> None:
        result = _resolve_for_runtime(
            "/a/b/song.flac",
            None,
            [("/a", "/X"), ("/a/b", "/Y")],
        )
        # First match wins per implementation
        assert result.replace("\\", "/").startswith("/X")

    def test_remap_no_match_passes_through(self) -> None:
        result = _resolve_for_runtime("/somewhere/song.flac", None, [("/elsewhere", "/X")])
        assert "song.flac" in result.replace("\\", "/")
        # Did not get remapped
        assert "/X" not in result.replace("\\", "/")

    def test_relative_path_uses_music_dir(self) -> None:
        result = _resolve_for_runtime("song.flac", Path("Z:/Music"), None)
        rs = result.replace("\\", "/")
        assert rs.startswith("Z:/Music")
        assert rs.endswith("song.flac")

    def test_relative_with_no_music_dir(self) -> None:
        # base = Path() empty
        result = _resolve_for_runtime("song.flac", None, None)
        assert "song.flac" in result.replace("\\", "/")


# ---------------------------------------------------------------------------
# _is_relative_storage
# ---------------------------------------------------------------------------


class TestIsRelativeStorage:
    def test_all_relative(self) -> None:
        assert _is_relative_storage(["a/b.flac", "c.flac"])

    def test_one_absolute_posix(self) -> None:
        assert not _is_relative_storage(["a/b.flac", "/c.flac"])

    def test_one_absolute_windows_drive(self) -> None:
        assert not _is_relative_storage(["Z:/a/b.flac"])

    def test_one_with_backslash(self) -> None:
        assert not _is_relative_storage(["a\\b.flac"])

    def test_empty_list_is_relative(self) -> None:
        # all([]) is True
        assert _is_relative_storage([]) is True


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


class TestDeleteIndexFiles:
    def test_unlinks_all_known_index_files(self, tmp_path: Path) -> None:
        for name in (
            "vectors.index",
            "tracks.db",
            "tracks.db-wal",
            "tracks.db-shm",
        ):
            (tmp_path / name).write_bytes(b"x")
        _delete_index_files(tmp_path)
        for name in (
            "vectors.index",
            "tracks.db",
            "tracks.db-wal",
            "tracks.db-shm",
        ):
            assert not (tmp_path / name).exists()

    def test_missing_files_is_noop(self, tmp_path: Path) -> None:
        # Idempotent — no error when nothing is there.
        _delete_index_files(tmp_path)


# ---------------------------------------------------------------------------
# _maybe_migrate_paths
# ---------------------------------------------------------------------------


class TestMaybeMigratePaths:
    def test_skip_when_music_dir_none(self) -> None:
        # Should not touch the tracks DB
        with patch("autodj.indexer._replace_tracks_rows") as rep:
            _maybe_migrate_paths(MagicMock(), [], Path("/idx"), None, already_relative=False)
        rep.assert_not_called()

    def test_skip_when_already_relative(self) -> None:
        with patch("autodj.indexer._replace_tracks_rows") as rep:
            _maybe_migrate_paths(MagicMock(), [], Path("/idx"), Path("/m"), already_relative=True)
        rep.assert_not_called()

    def test_relativises_tracks_db_when_migration_needed(self, tmp_path: Path) -> None:
        # Build a real save_index round-trip with absolute paths and trigger migration
        entries = [_entry(path=str(tmp_path / "song0.flac"))]
        vectors = np.random.randn(1, FEATURE_DIM).astype(np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        index_dir = tmp_path / "idx"
        index_dir.mkdir()
        # Save without music_dir so paths stay absolute
        save_index(entries, vectors, index_dir)

        # No FAISS read needed any more; pass a placeholder.
        _maybe_migrate_paths(MagicMock(), entries, index_dir, tmp_path, already_relative=False)

        import sqlite3 as _sql

        conn = _sql.connect(index_dir / "tracks.db")
        try:
            p = conn.execute("SELECT path FROM tracks ORDER BY id ASC LIMIT 1").fetchone()[0]
        finally:
            conn.close()
        # After migration, path should be relative (no drive, no leading slash)
        assert not (p.startswith("/") or (len(p) >= 2 and p[1] == ":"))


# ---------------------------------------------------------------------------
# save_index error rollback paths
# ---------------------------------------------------------------------------


class TestSaveIndexErrorPaths:
    def _entries_vectors(self, n: int = 2) -> tuple[list[IndexEntry], np.ndarray]:
        entries = [_entry(path=f"Z:/x{i}.flac") for i in range(n)]
        v = np.random.randn(n, FEATURE_DIM).astype(np.float32)
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        return entries, v

    def test_rolls_back_vectors_tmp_on_failure(self, tmp_path: Path) -> None:
        entries, vectors = self._entries_vectors()
        idx = tmp_path / "idx"
        idx.mkdir()

        # Force the chunked-write replace step to blow up after the temp file exists.
        with patch("autodj.indexer._write_faiss_chunked", side_effect=OSError("boom")) as wfc:
            # Pre-create the tmp file so the cleanup branch executes
            (idx / "vectors.index.tmp").write_bytes(b"partial")
            with pytest.raises(OSError):
                save_index(entries, vectors, idx)
        wfc.assert_called_once()
        assert not (idx / "vectors.index.tmp").exists()
        assert not (idx / "vectors.index").exists()

    def test_rolls_back_when_tracks_db_write_fails(self, tmp_path: Path) -> None:
        entries, vectors = self._entries_vectors()
        idx = tmp_path / "idx"
        idx.mkdir()

        # Allow vectors to write fine, but make the SQLite replacement fail.
        with (
            patch(
                "autodj.indexer._replace_tracks_rows",
                side_effect=RuntimeError("boom"),
            ),
            pytest.raises(RuntimeError),
        ):
            save_index(entries, vectors, idx)
        # FAISS index landed (it writes first)...
        assert (idx / "vectors.index").exists()
        # ... and tracks.db was created (sqlite3.connect makes the file),
        # but the failed transaction left zero rows.
        import sqlite3 as _sql

        conn = _sql.connect(idx / "tracks.db")
        try:
            count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        finally:
            conn.close()
        assert count == 0


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
# Auto-migrate failure (line 1002-1003)
# ---------------------------------------------------------------------------


class TestMigrateFlatIndexFailure:
    def test_migration_oserror_swallowed_logged(self, tmp_path: Path, caplog) -> None:
        from autodj.indexer import _migrate_flat_index_if_needed

        # Old layout: parent has tracks.db + vectors.index
        parent = tmp_path
        target = parent / "default"
        (parent / "tracks.db").write_bytes(b"")
        (parent / "vectors.index").write_bytes(b"")
        # Force replace to fail
        with (
            patch("pathlib.Path.replace", side_effect=OSError("denied")),
            caplog.at_level("WARNING"),
        ):
            _migrate_flat_index_if_needed(target)
        # Should log a warning, not raise
        assert any("Auto-migration failed" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# autodj.indexer — minor-key branch + tempo confidence fallback
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
        from pathlib import Path
        from unittest.mock import patch

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
        from unittest.mock import patch

        import numpy as np
        import pytest

        from autodj import indexer

        with patch.object(indexer, "_load_audio") as load:
            load.return_value = (np.array([], dtype=np.float32), 22050)
            with pytest.raises(ValueError, match="no samples"):
                indexer._extract_librosa_features(Path("dummy.flac"))
