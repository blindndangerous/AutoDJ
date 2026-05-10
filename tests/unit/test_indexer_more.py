"""Additional indexer unit tests targeting previously-uncovered branches.

Focus on the small pure-function helpers (``_apply_text_fields``,
``_apply_numeric_fields``, ``_apply_beets_row``, ``_resolve_for_runtime``
remap branch, ``_maybe_migrate_paths``, ``_check_prune_safety`` happy
paths) and on the error-rollback paths in ``save_index``.
"""

from __future__ import annotations

import json
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
        assert _is_relative_storage([{"path": "a/b.flac"}, {"path": "c.flac"}])

    def test_one_absolute_posix(self) -> None:
        assert not _is_relative_storage([{"path": "a/b.flac"}, {"path": "/c.flac"}])

    def test_one_absolute_windows_drive(self) -> None:
        assert not _is_relative_storage([{"path": "Z:/a/b.flac"}])

    def test_one_with_backslash(self) -> None:
        assert not _is_relative_storage([{"path": "a\\b.flac"}])

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
    def test_unlinks_both_files(self, tmp_path: Path) -> None:
        a = tmp_path / "metadata.json"
        b = tmp_path / "vectors.index"
        a.write_text("{}", encoding="utf-8")
        b.write_bytes(b"x")
        _delete_index_files(a, b)
        assert not a.exists()
        assert not b.exists()


# ---------------------------------------------------------------------------
# _maybe_migrate_paths
# ---------------------------------------------------------------------------


class TestMaybeMigratePaths:
    def test_skip_when_music_dir_none(self) -> None:
        # Should not call save_index
        with patch("autodj.indexer.save_index") as sv:
            _maybe_migrate_paths(MagicMock(), [], Path("/idx"), None, already_relative=False)
        sv.assert_not_called()

    def test_skip_when_already_relative(self) -> None:
        with patch("autodj.indexer.save_index") as sv:
            _maybe_migrate_paths(MagicMock(), [], Path("/idx"), Path("/m"), already_relative=True)
        sv.assert_not_called()

    def test_calls_save_index_when_migration_needed(self, tmp_path: Path) -> None:
        # Build a real save_index round-trip with absolute paths and trigger migration
        entries = [_entry(path=str(tmp_path / "song0.flac"))]
        vectors = np.random.randn(1, FEATURE_DIM).astype(np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        index_dir = tmp_path / "idx"
        index_dir.mkdir()
        # Save without music_dir so paths stay absolute
        save_index(entries, vectors, index_dir)

        # Now load FAISS and trigger migration
        import faiss

        loaded = faiss.read_index(str(index_dir / "vectors.index"))
        _maybe_migrate_paths(loaded, entries, index_dir, tmp_path, already_relative=False)

        raw = json.loads((index_dir / "metadata.json").read_text(encoding="utf-8"))
        # After migration, path should be relative (no drive, no leading slash)
        p = raw[0]["path"]
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

    def test_rolls_back_metadata_tmp_on_failure(self, tmp_path: Path) -> None:
        entries, vectors = self._entries_vectors()
        idx = tmp_path / "idx"
        idx.mkdir()

        # Allow vectors to write fine, but make the metadata json.dumps fail.
        with patch("autodj.indexer.json.dumps", side_effect=RuntimeError("boom")):
            # Pre-touch tmp so the cleanup unlink path runs
            (idx / "metadata.json.tmp").write_bytes(b"partial")
            with pytest.raises(RuntimeError):
                save_index(entries, vectors, idx)
        # vectors did get written successfully...
        assert (idx / "vectors.index").exists()
        # ... but metadata tmp was rolled back.
        assert not (idx / "metadata.json.tmp").exists()
        assert not (idx / "metadata.json").exists()


# ---------------------------------------------------------------------------
# load_index missing-file branch (FileNotFoundError at line 1053)
# ---------------------------------------------------------------------------


class TestLoadIndexMissingFiles:
    def test_dir_exists_but_files_missing_raises(self, tmp_path: Path) -> None:
        idx = tmp_path / "idx"
        idx.mkdir()
        # No metadata.json or vectors.index
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

        # Old layout: parent has metadata.json + vectors.index
        parent = tmp_path
        target = parent / "default"
        (parent / "metadata.json").write_text("[]", encoding="utf-8")
        (parent / "vectors.index").write_bytes(b"")
        # Force replace to fail
        with (
            patch("pathlib.Path.replace", side_effect=OSError("denied")),
            caplog.at_level("WARNING"),
        ):
            _migrate_flat_index_if_needed(target)
        # Should log a warning, not raise
        assert any("Auto-migration failed" in rec.message for rec in caplog.records)
