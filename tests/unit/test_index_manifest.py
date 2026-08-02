from __future__ import annotations

import json
import os
import sqlite3
import stat
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import faiss
import numpy as np
import pytest

from autodj.index_manifest import (
    IndexConsistencyError,
    publish_manifest,
    read_manifest,
    sha256_file,
)


def _write_working_artifacts(index_dir: Path, count: int) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_dir / "tracks.db", isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS tracks (vec_row INTEGER, path TEXT)")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM tracks")
        conn.executemany(
            "INSERT INTO tracks VALUES (?, ?)",
            [(row, f"song-{row}.flac") for row in range(count)],
        )
        conn.commit()
    finally:
        conn.close()
    vectors = faiss.IndexFlatIP(2)
    if count:
        vectors.add(np.ones((count, 2), dtype=np.float32))
    faiss.write_index(vectors, str(index_dir / "vectors.index"))


def _publish_once(index_dir_text: str, count: int) -> int:
    return publish_manifest(Path(index_dir_text), count).generation


def test_publish_manifest_is_monotonic_atomic_and_retains_two_generations(
    tmp_path: Path,
) -> None:
    _write_working_artifacts(tmp_path, 3)
    first = publish_manifest(tmp_path, vector_count=3)
    _write_working_artifacts(tmp_path, 5)
    second = publish_manifest(tmp_path, vector_count=5)
    _write_working_artifacts(tmp_path, 7)
    third = publish_manifest(tmp_path, vector_count=7)
    assert first.generation == 1
    assert second.generation == 2
    assert third.generation == 3
    assert read_manifest(tmp_path) == third
    names = {path.name for path in tmp_path.iterdir()}
    assert first.tracks_file not in names
    assert first.vectors_file not in names
    assert {
        second.tracks_file,
        second.vectors_file,
        third.tracks_file,
        third.vectors_file,
    } <= names
    assert list(tmp_path.glob(".*.tmp")) == []


def test_publish_manifest_serializes_concurrent_generation_numbers(tmp_path: Path) -> None:
    _write_working_artifacts(tmp_path, 3)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish_manifest, tmp_path, 3) for _ in range(2)]
    assert {future.result().generation for future in futures} == {1, 2}


def test_publish_manifest_serializes_concurrent_processes(tmp_path: Path) -> None:
    _write_working_artifacts(tmp_path, 3)
    with ProcessPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_publish_once, str(tmp_path), 3) for _ in range(2)]
    assert {future.result(timeout=10) for future in futures} == {1, 2}


@pytest.mark.skipif(os.name == "nt", reason="opening a directory for fsync is POSIX-only")
def test_manifest_replace_fsyncs_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_working_artifacts(tmp_path, 1)
    real_fsync = os.fsync
    directory_fsyncs = 0

    def recording_fsync(fd: int) -> None:
        nonlocal directory_fsyncs
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_fsyncs += 1
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    publish_manifest(tmp_path, 1)
    assert directory_fsyncs >= 1


def test_publish_checkpoints_wal_before_hashing(tmp_path: Path) -> None:
    _write_working_artifacts(tmp_path, 2)
    manifest = publish_manifest(tmp_path, 2)
    wal = tmp_path / "tracks.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0
    assert manifest.tracks_sha256 == sha256_file(tmp_path / manifest.tracks_file)


def test_copy_published_snapshot_produces_valid_canonical_backup(tmp_path: Path) -> None:
    from autodj.index_manifest import copy_published_snapshot

    source = tmp_path / "source"
    destination = tmp_path / "backup"
    _write_working_artifacts(source, 2)
    published = publish_manifest(source, 2)
    copied = copy_published_snapshot(
        source,
        destination,
        expected_generation=published.generation,
    )
    assert copied.generation == published.generation
    assert copied.tracks_file == "tracks.db"
    assert copied.vectors_file == "vectors.index"
    assert read_manifest(destination) == copied
    assert sha256_file(destination / "tracks.db") == copied.tracks_sha256
    assert sha256_file(destination / "vectors.index") == copied.vectors_sha256


def test_copy_published_snapshot_rejects_generation_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autodj.index_manifest as manifest_module

    source = tmp_path / "source"
    destination = tmp_path / "backup"
    _write_working_artifacts(source, 2)
    publish_manifest(source, 2)
    real_validate = manifest_module._validate_snapshot_files
    raced = False

    def validate_then_publish(root: Path, manifest: object) -> None:
        nonlocal raced
        real_validate(root, manifest)
        if root == source and not raced:
            raced = True
            publish_manifest(source, 2)

    monkeypatch.setattr(manifest_module, "_validate_snapshot_files", validate_then_publish)
    with pytest.raises(IndexConsistencyError, match="changed while copying"):
        manifest_module.copy_published_snapshot(source, destination)
    assert not destination.exists()


def test_corrupt_manifest_raises_consistency_error(tmp_path: Path) -> None:
    (tmp_path / "index-manifest.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(IndexConsistencyError, match="manifest"):
        read_manifest(tmp_path)


def test_manifest_rejects_wrong_schema(tmp_path: Path) -> None:
    payload = {
        "schema_version": 99,
        "generation": 1,
        "vector_count": 2,
        "published_at": "2026-08-02T00:00:00+00:00",
        "tracks_file": "tracks.g00000000000000000001.db",
        "vectors_file": "vectors.g00000000000000000001.index",
        "tracks_sha256": "0" * 64,
        "vectors_sha256": "1" * 64,
    }
    (tmp_path / "index-manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(IndexConsistencyError, match="schema"):
        read_manifest(tmp_path)
