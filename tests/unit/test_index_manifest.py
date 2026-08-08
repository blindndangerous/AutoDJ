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
    IndexSnapshotToken,
    publish_manifest,
    read_manifest,
    restore_working_snapshot,
    sha256_file,
)


def _write_working_artifacts(index_dir: Path, count: int) -> None:
    from autodj.indexer import _TRACKS_SCHEMA

    index_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_dir / "tracks.db", isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_TRACKS_SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM tracks")
        conn.executemany(
            "INSERT INTO tracks (vec_row, path) VALUES (?, ?)",
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


def test_snapshot_token_rejects_negative_generation() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        IndexSnapshotToken(-1)


def _manifest_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "generation": 1,
        "vector_count": 2,
        "published_at": "2026-08-08T00:00:00+00:00",
        "tracks_file": "tracks.g00000000000000000001.db",
        "vectors_file": "vectors.g00000000000000000001.index",
        "tracks_sha256": "0" * 64,
        "vectors_sha256": "1" * 64,
    }
    payload.update(changes)
    return payload


def test_manifest_rejects_non_object_and_extra_keys(tmp_path: Path) -> None:
    path = tmp_path / "index-manifest.json"
    for payload in ([], _manifest_payload(unexpected="value")):
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(IndexConsistencyError, match="manifest"):
            read_manifest(tmp_path)


@pytest.mark.parametrize("field", ["schema_version", "generation", "vector_count"])
@pytest.mark.parametrize("value", [True, "1", 1.0, None])
def test_manifest_rejects_non_integer_fields(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    (tmp_path / "index-manifest.json").write_text(
        json.dumps(_manifest_payload(**{field: value})), encoding="utf-8"
    )
    with pytest.raises(IndexConsistencyError, match="manifest"):
        read_manifest(tmp_path)


@pytest.mark.parametrize(
    "field",
    ["published_at", "tracks_file", "vectors_file", "tracks_sha256", "vectors_sha256"],
)
@pytest.mark.parametrize("value", [1, 1.0, None])
def test_manifest_rejects_non_string_fields(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    (tmp_path / "index-manifest.json").write_text(
        json.dumps(_manifest_payload(**{field: value})), encoding="utf-8"
    )
    with pytest.raises(IndexConsistencyError, match="manifest"):
        read_manifest(tmp_path)


@pytest.mark.parametrize(
    ("tracks_file", "vectors_file"),
    [
        ("tracks.g00000000000000000002.db", "vectors.g00000000000000000001.index"),
        ("tracks.g00000000000000000001.db", "vectors.g00000000000000000002.index"),
        ("other.db", "other.index"),
        ("tracks.db", "vectors.g00000000000000000001.index"),
    ],
)
def test_manifest_rejects_artifacts_not_matching_its_generation(
    tmp_path: Path,
    tracks_file: str,
    vectors_file: str,
) -> None:
    (tmp_path / "index-manifest.json").write_text(
        json.dumps(_manifest_payload(tracks_file=tracks_file, vectors_file=vectors_file)),
        encoding="utf-8",
    )
    with pytest.raises(IndexConsistencyError, match="artifact"):
        read_manifest(tmp_path)


def test_manifest_accepts_canonical_backup_artifact_pair(tmp_path: Path) -> None:
    (tmp_path / "index-manifest.json").write_text(
        json.dumps(_manifest_payload(tracks_file="tracks.db", vectors_file="vectors.index")),
        encoding="utf-8",
    )
    assert read_manifest(tmp_path) is not None


@pytest.mark.parametrize(
    "published_at",
    ["not-a-timestamp", "2026-08-08T00:00:00", "2026-08-08T00:00:00Z", "2026-08-08T00:00:00+01:00"],
)
def test_manifest_rejects_noncanonical_utc_timestamp(tmp_path: Path, published_at: str) -> None:
    (tmp_path / "index-manifest.json").write_text(
        json.dumps(_manifest_payload(published_at=published_at)), encoding="utf-8"
    )
    with pytest.raises(IndexConsistencyError, match="timestamp"):
        read_manifest(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="msvcrt locking is Windows-only")
def test_windows_lock_retries_contention_until_acquired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno
    import msvcrt

    import autodj.index_manifest as manifest_module

    calls: list[int] = []

    def locking(_fd: int, mode: int, _length: int) -> None:
        calls.append(mode)
        if len(calls) < 3:
            raise OSError(errno.EACCES, "locked")

    monkeypatch.setattr(msvcrt, "locking", locking)
    with (tmp_path / "lock").open("a+b") as handle:
        manifest_module._acquire_os_lock(handle)
    assert calls == [msvcrt.LK_NBLCK, msvcrt.LK_NBLCK, msvcrt.LK_NBLCK]


@pytest.mark.skipif(os.name != "nt", reason="msvcrt locking is Windows-only")
def test_windows_lock_propagates_non_contention_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno
    import msvcrt

    import autodj.index_manifest as manifest_module

    calls: list[int] = []

    def locking(_fd: int, mode: int, _length: int) -> None:
        calls.append(mode)
        raise OSError(errno.EIO, "disk failure")

    monkeypatch.setattr(msvcrt, "locking", locking)
    with (tmp_path / "lock").open("a+b") as handle, pytest.raises(OSError, match="disk failure"):
        manifest_module._acquire_os_lock(handle)
    assert calls == [msvcrt.LK_NBLCK]


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


def test_restore_rejects_same_count_noncanonical_vec_rows(tmp_path: Path) -> None:
    _write_working_artifacts(tmp_path, 3)
    manifest = publish_manifest(tmp_path, 3)
    tracks = tmp_path / manifest.tracks_file
    conn = sqlite3.connect(tracks)
    try:
        conn.execute("UPDATE tracks SET vec_row = vec_row + 10")
        conn.commit()
    finally:
        conn.close()
    raw = json.loads((tmp_path / "index-manifest.json").read_text(encoding="utf-8"))
    raw["tracks_sha256"] = sha256_file(tracks)
    (tmp_path / "index-manifest.json").write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(IndexConsistencyError, match="vec_row"):
        restore_working_snapshot(tmp_path)


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


def test_manifested_sqlite_reads_ignore_committed_wal_sidecar(tmp_path: Path) -> None:
    from autodj.index_manifest import (
        _immutable_sqlite_uri,
        _validate_snapshot_files,
    )
    from autodj.indexer import FEATURE_DIM, IndexEntry, load_index, save_index

    paths = [str(tmp_path / "first.flac"), str(tmp_path / "second.flac")]
    stored_paths = [path.replace("\\", "/") for path in paths]
    entries = [
        IndexEntry(
            path=path,
            title=title,
            artist="Artist",
            album="Album",
            genre="Genre",
            bpm=120.0,
            year=2026,
            length=180.0,
            energy=0.5,
            key=0,
            mode=1,
            tempo_confidence=0.8,
        )
        for path, title in zip(paths, ("First", "Second"), strict=True)
    ]
    vectors = np.zeros((2, FEATURE_DIM), dtype=np.float32)
    vectors[0, 3] = 1.0
    vectors[1, 7] = 1.0
    save_index(entries, vectors, tmp_path)
    manifest = read_manifest(tmp_path)
    assert manifest is not None
    tracks_path = tmp_path / manifest.tracks_file
    main_hash = sha256_file(tracks_path)

    writer = sqlite3.connect(tracks_path, isolation_level=None)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE tracks SET path = 'dirty.tmp' WHERE vec_row = 0")
        writer.execute(
            "UPDATE tracks SET path = ?, title = 'Dirty first' WHERE vec_row = 1", (paths[0],)
        )
        writer.execute(
            "UPDATE tracks SET path = ?, title = 'Dirty second' WHERE vec_row = 0", (paths[1],)
        )
        writer.commit()
        wal = tracks_path.with_name(f"{tracks_path.name}-wal")
        assert wal.stat().st_size > 0
        assert sha256_file(tracks_path) == main_hash == manifest.tracks_sha256

        uri = _immutable_sqlite_uri(tracks_path)
        assert "mode=ro" in uri
        assert "immutable=1" in uri
        immutable = sqlite3.connect(uri, uri=True)
        try:
            immutable_rows = immutable.execute(
                "SELECT path, title FROM tracks ORDER BY vec_row"
            ).fetchall()
        finally:
            immutable.close()
        assert immutable_rows == [
            (stored_paths[0], "First"),
            (stored_paths[1], "Second"),
        ]

        _validate_snapshot_files(tmp_path, manifest)
        loaded, loaded_vectors = load_index(tmp_path)
        assert [(entry.path, entry.title) for entry in loaded] == [
            (stored_paths[0], "First"),
            (stored_paths[1], "Second"),
        ]
        assert [int(np.argmax(loaded_vectors.reconstruct(row))) for row in range(2)] == [3, 7]
    finally:
        writer.close()
