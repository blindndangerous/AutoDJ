"""Behavior tests for defensive and recovery paths in ``autodj.indexer``."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import faiss
import numpy as np
import pytest

import autodj.indexer as indexer
from autodj.index_manifest import IndexConsistencyError, IndexSnapshotToken


def _entry(path: str, *, artist: str = "Artist") -> indexer.IndexEntry:
    return indexer.IndexEntry(
        path=path,
        title="Song",
        artist=artist,
        album="Album",
        genre="Rock",
        bpm=120.0,
        year=2024,
        length=180.0,
        energy=0.5,
        key=0,
        mode=1,
        tempo_confidence=0.8,
    )


def _vector() -> np.ndarray:
    vector = np.zeros((1, indexer.FEATURE_DIM), dtype=np.float32)
    vector[0, 7] = 1.0
    return vector


def _create_legacy_cores(parent: Path, *, vector_in: Path | None = None) -> None:
    entry = _entry(str(parent / "song.flac"))
    connection = indexer._open_tracks_db(parent)
    try:
        connection.execute(
            indexer._TRACKS_INSERT_SQL,
            indexer._entry_to_row(entry, music_dir=None, vec_row=0),
        )
    finally:
        connection.close()
    destination = parent / "vectors.index" if vector_in is None else vector_in
    faiss.write_index(indexer.build_faiss_index(_vector()), str(destination))


def test_display_name_falls_back_to_title_without_artist() -> None:
    assert _entry("song.flac", artist="").display_name == "Song"


def test_load_audio_downmixes_stereo_soundfile_result() -> None:
    stereo = np.array([[1.0, 3.0], [5.0, 7.0]], dtype=np.float32)

    with patch.object(indexer.sf, "read", return_value=(stereo, 48_000)):
        audio, sample_rate = indexer._load_audio(Path("song.flac"))

    assert sample_rate == 48_000
    assert np.array_equal(audio, np.array([2.0, 6.0], dtype=np.float32))


def test_key_estimation_rejects_ambiguous_chroma() -> None:
    ambiguous = np.array(
        [1.00, 0.99, 1.01, 1.00, 0.98, 1.02, 1.00, 0.99, 1.01, 1.00, 0.98, 1.02],
        dtype=np.float32,
    )

    assert indexer._estimate_key_from_chroma(ambiguous) == (-1, -1)


def test_key_estimation_rejects_near_constant_low_magnitude_chroma() -> None:
    near_constant = np.full(12, 1e-7, dtype=np.float32)
    near_constant[0] = 2e-7

    assert indexer._estimate_key_from_chroma(near_constant) == (-1, -1)


def test_key_estimation_rejects_weak_profile_match() -> None:
    weak_match = np.array(
        [
            0.43263078,
            0.6692973,
            0.4227847,
            0.6331844,
            0.96743596,
            0.6830648,
            0.39162484,
            0.18725257,
            0.34596068,
            0.51106596,
            0.8912094,
            0.77556396,
        ],
        dtype=np.float32,
    )

    assert indexer._estimate_key_from_chroma(weak_match) == (-1, -1)


def test_schema_migration_ignores_non_unique_indexes(tmp_path: Path) -> None:
    database = tmp_path / "tracks.db"
    connection = sqlite3.connect(database, isolation_level=None)
    try:
        connection.executescript(
            """
            CREATE TABLE tracks (path TEXT NOT NULL, title TEXT NOT NULL);
            CREATE INDEX tracks_title_idx ON tracks(title);
            INSERT INTO tracks(path, title) VALUES ('song.flac', 'Song');
            """
        )

        indexer._ensure_vec_row_schema(connection)

        assert connection.execute("SELECT vec_row, path, title FROM tracks").fetchall() == [
            (0, "song.flac", "Song")
        ]
    finally:
        connection.close()


def test_chunked_faiss_write_survives_unsupported_fsync(tmp_path: Path) -> None:
    faiss_index = indexer.build_faiss_index(_vector())
    destination = tmp_path / "vectors.index"

    with patch("os.fsync", side_effect=OSError("unsupported")):
        indexer._write_faiss_chunked(faiss_index, destination, chunk_size=7)

    restored = faiss.read_index(str(destination))
    assert restored.ntotal == 1
    assert np.array_equal(restored.reconstruct(0), _vector()[0])


def test_empty_checkpoint_write_leaves_storage_untouched(tmp_path: Path) -> None:
    checkpoint = indexer.IncrementalCheckpoint(
        index_dir=tmp_path,
        music_dir=None,
        existing_entries=[],
        existing_vectors=[],
        total_new=1,
        expected_snapshot=IndexSnapshotToken(0),
    )

    checkpoint.write([], [])

    assert list(tmp_path.iterdir()) == []


def test_failed_first_checkpoint_refreshes_retry_token(tmp_path: Path) -> None:
    checkpoint = indexer.IncrementalCheckpoint(
        index_dir=tmp_path,
        music_dir=None,
        existing_entries=[],
        existing_vectors=[],
        total_new=1,
        expected_snapshot=IndexSnapshotToken(0),
        flush_every=1,
    )
    refreshed = IndexSnapshotToken(0, 1)

    with (
        patch.object(indexer, "require_snapshot_token", return_value=None),
        patch.object(indexer, "_save_vectors"),
        patch.object(indexer, "_upsert_tracks_metadata"),
        patch.object(indexer, "publish_manifest", side_effect=OSError("publish failed")),
        patch.object(indexer, "current_snapshot_token", return_value=refreshed),
        pytest.raises(OSError, match="publish failed"),
    ):
        checkpoint.write([_entry("song.flac")], [_vector()[0]])

    assert checkpoint.expected_snapshot == refreshed
    assert checkpoint.published_new_count == 0


def test_enrich_rejects_manifest_free_artifacts_with_history(tmp_path: Path) -> None:
    with (
        patch.object(indexer, "read_manifest", return_value=None),
        patch.object(indexer, "legacy_artifacts_allowed", return_value=False),
        pytest.raises(IndexConsistencyError, match="publication history"),
    ):
        indexer.enrich_from_beets(tmp_path, music_dir=None, beets_db=tmp_path / "beets.db")


def test_prune_rejects_manifest_free_artifacts_with_history(tmp_path: Path) -> None:
    with (
        patch.object(indexer, "read_manifest", return_value=None),
        patch.object(indexer, "legacy_artifacts_allowed", return_value=False),
        pytest.raises(IndexConsistencyError, match="publication history"),
    ):
        indexer.prune_index(tmp_path)


def test_enrich_skips_index_entries_absent_from_beets(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    entry = _entry(str(tmp_path / "indexed.flac"))
    indexer.save_index([entry], _vector(), index_dir)
    beets_db = tmp_path / "beets.db"
    connection = sqlite3.connect(beets_db)
    try:
        connection.execute(
            """CREATE TABLE items (
                id INTEGER PRIMARY KEY, path BLOB, title TEXT, artist TEXT,
                album TEXT, genre TEXT, bpm REAL, year INTEGER, length REAL,
                initial_key TEXT
            )"""
        )
        connection.execute(
            "INSERT INTO items VALUES (1, ?, 'Other', '', '', '', 0, 0, 0, '')",
            (str(tmp_path / "other.flac").encode(),),
        )
        connection.commit()
    finally:
        connection.close()

    assert indexer.enrich_from_beets(index_dir, music_dir=None, beets_db=beets_db) == (0, 1)


def test_prune_applies_requested_stat_throttle(tmp_path: Path) -> None:
    track = tmp_path / "song.flac"
    track.touch()
    index_dir = tmp_path / "index"
    indexer.save_index([_entry(str(track))], _vector(), index_dir)
    sleeps: list[float] = []

    with patch.object(time, "sleep", side_effect=sleeps.append):
        result = indexer.prune_index(index_dir, throttle_ms=5.0, stat_workers=1)

    assert result == (0, 1)
    assert sleeps == [0.005]


@pytest.mark.parametrize(
    "contents, message",
    [
        ("{", "invalid flat migration marker:"),
        (json.dumps({"staging": "../outside"}), "invalid flat migration marker"),
    ],
)
def test_flat_migration_marker_rejects_malformed_state(
    tmp_path: Path, contents: str, message: str
) -> None:
    (tmp_path / indexer._FLAT_MIGRATION_MARKER).write_text(contents, encoding="utf-8")

    with pytest.raises(IndexConsistencyError, match=message):
        indexer._read_flat_migration_staging(tmp_path)


def test_flat_migration_rejects_mismatched_database_and_vector_counts(tmp_path: Path) -> None:
    staged_db = tmp_path / "tracks.db"
    connection = sqlite3.connect(staged_db)
    try:
        connection.execute("CREATE TABLE tracks (path TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO tracks(path) VALUES (?)", [("one.flac",), ("two.flac",)]
        )
        connection.commit()
    finally:
        connection.close()

    staged_vectors = tmp_path / "vectors.index"
    faiss.write_index(indexer.build_faiss_index(_vector()), str(staged_vectors))

    with pytest.raises(IndexConsistencyError, match="sqlite=2, faiss=1"):
        indexer._validate_flat_migration_staging(staged_db, staged_vectors)


def test_tombstoned_migration_logs_cleanup_failure(tmp_path: Path, caplog) -> None:
    target = tmp_path / "default"
    target.mkdir()
    staging = target / f"{indexer._FLAT_MIGRATION_STAGING_PREFIX}owned"
    staging.mkdir()
    (target / indexer._FLAT_MIGRATION_MARKER).write_text(
        json.dumps({"staging": staging.name}), encoding="utf-8"
    )

    with (
        patch.object(indexer, "publication_is_tombstoned", return_value=True),
        patch.object(indexer, "_clear_flat_migration_state", side_effect=OSError("busy")),
        caplog.at_level("WARNING"),
    ):
        indexer._migrate_flat_index_if_needed(target)

    assert "Could not clean stale flat migration state for tombstoned target" in caplog.text


def test_historical_split_failure_preserves_existing_vector(tmp_path: Path) -> None:
    target = tmp_path / "default"
    target.mkdir()
    target_vector = target / "vectors.index"
    target_vector.write_bytes(b"existing vector")
    source_db = tmp_path / "tracks.db"
    sqlite3.connect(source_db).close()

    with (
        patch.object(indexer, "_migrate_staged_legacy_tracks_db", side_effect=RuntimeError("bad")),
        pytest.raises(RuntimeError, match="bad"),
    ):
        indexer._migrate_flat_index_if_needed(target)

    assert target_vector.read_bytes() == b"existing vector"
    assert not (target / "tracks.db").exists()
    assert not (target / indexer._FLAT_MIGRATION_MARKER).exists()
    assert not any(
        path.name.startswith(indexer._FLAT_MIGRATION_STAGING_PREFIX) for path in target.iterdir()
    )


def test_historical_split_logs_best_effort_cleanup_failures(tmp_path: Path, caplog) -> None:
    target = tmp_path / "default"
    target.mkdir()
    _create_legacy_cores(tmp_path, vector_in=target / "vectors.index")
    source_db = tmp_path / "tracks.db"
    real_unlink = Path.unlink

    def fail_source_cleanup(path: Path, *args, **kwargs):
        if path == source_db:
            raise OSError("busy source")
        return real_unlink(path, *args, **kwargs)

    with (
        patch.object(Path, "unlink", fail_source_cleanup),
        patch.object(indexer, "_clear_flat_migration_state", side_effect=OSError("busy marker")),
        caplog.at_level("WARNING"),
    ):
        indexer._migrate_flat_index_if_needed(target)

    loaded, vectors = indexer.load_index(target, _migrate_flat=False)
    assert [entry.title for entry in loaded] == ["Song"]
    assert vectors.ntotal == 1
    assert "Could not remove migrated legacy artifact" in caplog.text
    assert "Could not clean flat migration marker" in caplog.text


def test_owned_stale_migration_is_cleared_when_sources_are_gone(tmp_path: Path) -> None:
    target = tmp_path / "default"
    target.mkdir()
    staging = target / f"{indexer._FLAT_MIGRATION_STAGING_PREFIX}owned"
    staging.mkdir()
    (target / indexer._FLAT_MIGRATION_MARKER).write_text(
        json.dumps({"staging": staging.name}), encoding="utf-8"
    )

    indexer._migrate_flat_index_if_needed(target)

    assert not staging.exists()
    assert not (target / indexer._FLAT_MIGRATION_MARKER).exists()


def test_flat_migration_does_not_overwrite_existing_target_core(tmp_path: Path) -> None:
    target = tmp_path / "default"
    target.mkdir()
    _create_legacy_cores(tmp_path)
    existing = target / "vectors.index"
    existing.write_bytes(b"target wins")

    indexer._migrate_flat_index_if_needed(target)

    assert existing.read_bytes() == b"target wins"
    assert (tmp_path / "tracks.db").exists()
    assert (tmp_path / "vectors.index").exists()


def test_new_flat_migration_rolls_back_unexpected_failure(tmp_path: Path) -> None:
    target = tmp_path / "default"
    target.mkdir()
    _create_legacy_cores(tmp_path)

    with (
        patch.object(indexer, "_migrate_staged_legacy_tracks_db", side_effect=RuntimeError("bad")),
        pytest.raises(RuntimeError, match="bad"),
    ):
        indexer._migrate_flat_index_if_needed(target)

    assert not (target / "tracks.db").exists()
    assert not (target / "vectors.index").exists()
    assert not (target / indexer._FLAT_MIGRATION_MARKER).exists()


def test_successful_flat_migration_logs_best_effort_cleanup_failures(
    tmp_path: Path, caplog
) -> None:
    target = tmp_path / "default"
    target.mkdir()
    _create_legacy_cores(tmp_path)
    sidecar = tmp_path / "web_state.json"
    sidecar.write_text("{}", encoding="utf-8")
    source_db = tmp_path / "tracks.db"
    source_vector = tmp_path / "vectors.index"
    real_unlink = Path.unlink
    real_replace = Path.replace

    def fail_source_cleanup(path: Path, *args, **kwargs):
        if path in {source_db, source_vector}:
            raise OSError("busy source")
        return real_unlink(path, *args, **kwargs)

    def fail_sidecar_move(path: Path, target_path: Path):
        if path == sidecar:
            raise OSError("busy sidecar")
        return real_replace(path, target_path)

    with (
        patch.object(Path, "unlink", fail_source_cleanup),
        patch.object(Path, "replace", fail_sidecar_move),
        patch.object(indexer, "_clear_flat_migration_state", side_effect=OSError("busy marker")),
        caplog.at_level("WARNING"),
    ):
        indexer._migrate_flat_index_if_needed(target)

    loaded, vectors = indexer.load_index(target, _migrate_flat=False)
    assert [entry.title for entry in loaded] == ["Song"]
    assert vectors.ntotal == 1
    assert "Could not remove migrated legacy artifact" in caplog.text
    assert "Could not migrate sidecar" in caplog.text
    assert "Could not clean flat migration marker" in caplog.text


def test_load_rejects_wrong_expected_generation(tmp_path: Path) -> None:
    with pytest.raises(IndexConsistencyError, match="expected generation 1, got None"):
        indexer.load_index(tmp_path, expected_generation=1, _migrate_flat=False)


def test_load_detects_artifact_changed_during_read(tmp_path: Path) -> None:
    indexer.save_index([_entry("song.flac")], _vector(), tmp_path)
    stable_hash = "a" * 64
    changed_hash = "b" * 64

    with (
        patch.object(
            indexer,
            "sha256_file",
            side_effect=[stable_hash, stable_hash, changed_hash, stable_hash],
        ),
        pytest.raises(IndexConsistencyError, match="artifact changed during load"),
    ):
        indexer.load_index(tmp_path, _migrate_flat=False)


def test_load_rejects_faiss_count_mismatch(tmp_path: Path) -> None:
    indexer.save_index([_entry("song.flac")], _vector(), tmp_path)
    empty_index = faiss.IndexFlatIP(indexer.FEATURE_DIM)

    with (
        patch.object(indexer.faiss, "read_index", return_value=empty_index),
        pytest.raises(IndexConsistencyError, match="index count mismatch"),
    ):
        indexer.load_index(tmp_path, _migrate_flat=False)


def test_stat_mtimes_throttles_existing_and_missing_files(tmp_path: Path) -> None:
    existing = tmp_path / "existing.flac"
    existing.touch()
    entries = [_entry(str(existing)), _entry(str(tmp_path / "missing.flac"))]
    sleeps: list[float] = []

    with patch.object(time, "sleep", side_effect=sleeps.append):
        mtimes = indexer._stat_mtimes(entries, throttle_ms=5.0, stat_workers=1)

    assert mtimes[0] == existing.stat().st_mtime
    assert mtimes[1] is None
    assert sleeps == [0.005, 0.005]


def test_backfill_flushes_a_durable_checkpoint_after_25_tracks(tmp_path: Path) -> None:
    class Cache:
        def __init__(self) -> None:
            self.stored: list[str] = []
            self.flushes: list[bool] = []

        def get(self, _path: str):
            return MagicMock(analysed=False)

        def set(self, path: str, _meta: object) -> None:
            self.stored.append(path)

        def flush(self, *, force: bool = False) -> None:
            self.flushes.append(force)

    cache = Cache()
    entries = [_entry(f"song-{number}.flac") for number in range(25)]

    with (
        patch("autodj.dj_meta.get_cache", return_value=cache),
        patch.object(
            indexer,
            "_analyse_one_track",
            side_effect=lambda path: (path, object(), None),
        ),
    ):
        indexer._backfill_dj_meta(entries, tmp_path, workers=1)

    assert cache.stored == [entry.path for entry in entries]
    assert cache.flushes == [False, True]


def test_existing_artifact_loader_rejects_publication_history(tmp_path: Path) -> None:
    (tmp_path / "tracks.db").touch()

    with (
        patch.object(indexer, "read_manifest", return_value=None),
        patch.object(indexer, "publication_is_tombstoned", return_value=False),
        patch.object(indexer, "publication_has_uncommitted_reservation", return_value=False),
        patch.object(indexer, "legacy_artifacts_allowed", return_value=False),
        pytest.raises(IndexConsistencyError, match="publication history"),
    ):
        indexer._load_existing_artifacts(tmp_path, tmp_path, None)
