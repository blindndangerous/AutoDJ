from __future__ import annotations

import json
import os
import sqlite3
import stat
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import faiss
import numpy as np
import pytest

from autodj.index_manifest import (
    IndexConsistencyError,
    IndexManifest,
    IndexSnapshotToken,
    copy_published_snapshot,
    current_snapshot_token,
    publish_manifest,
    read_manifest,
    restore_working_snapshot,
    sha256_file,
    snapshot_token_for_manifest,
    tombstone_publication,
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


def test_snapshot_token_for_manifest_requires_live_manifest() -> None:
    with pytest.raises(ValueError, match="positive"):
        snapshot_token_for_manifest(IndexManifest(1, 0, 0, "", "", "", "", ""))


def test_snapshot_token_for_live_v1_manifest_keeps_zero_revision(tmp_path: Path) -> None:
    payload = _manifest_payload()
    (tmp_path / "index-manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    manifest = read_manifest(tmp_path)

    assert manifest is not None
    assert snapshot_token_for_manifest(manifest) == IndexSnapshotToken(1, 0)


def test_fork_reset_rebinds_inherited_lock_state_without_acquiring_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autodj.index_manifest as manifest_module

    inherited_guard = __import__("threading").Lock()
    inherited_guard.acquire()
    monkeypatch.setattr(manifest_module, "_LOCKS_GUARD", inherited_guard)
    monkeypatch.setattr(manifest_module, "_LOCKS", {Path("old"): __import__("threading").RLock()})
    manifest_module._HELD_LOCKS.paths = {Path("old")}

    manifest_module._reset_process_local_locks()

    assert manifest_module._LOCKS == {}
    assert manifest_module._LOCKS_GUARD is not inherited_guard
    assert manifest_module._HELD_LOCKS.paths == set()


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


def test_v2_manifest_rejects_negative_state_revision_without_state_file(tmp_path: Path) -> None:
    payload = _manifest_payload(schema_version=2, state_revision=-1)
    (tmp_path / "index-manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(IndexConsistencyError, match="generation/count"):
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


def test_publish_and_public_tombstone_serialize_to_coherent_state(tmp_path: Path) -> None:
    _write_working_artifacts(tmp_path, 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        published = pool.submit(publish_manifest, tmp_path, 1)
        tombstoned = pool.submit(tombstone_publication, tmp_path)
        manifest = published.result()
        tombstoned.result()

    state = json.loads((tmp_path / ".index-publication-state.json").read_text(encoding="utf-8"))
    live = read_manifest(tmp_path)
    assert state["high_water"] >= manifest.generation
    if live is None:
        assert current_snapshot_token(tmp_path).generation == 0
    else:
        assert live.state_revision > state["tombstone_revision"]


def test_failed_reserved_publish_keeps_prior_snapshot_live_and_consumes_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import autodj.index_manifest as manifest_module

    _write_working_artifacts(tmp_path, 2)
    first = publish_manifest(tmp_path, 2)
    before = current_snapshot_token(tmp_path)
    monkeypatch.setattr(
        manifest_module,
        "_checkpoint_working_tracks",
        lambda _index_dir: (_ for _ in ()).throw(OSError("checkpoint failed")),
    )
    with pytest.raises(OSError, match="checkpoint failed"):
        publish_manifest(tmp_path, 2)

    assert read_manifest(tmp_path) == first
    assert current_snapshot_token(tmp_path) == before
    monkeypatch.undo()
    retried = publish_manifest(tmp_path, 2)
    assert retried.generation == first.generation + 2


def test_failed_first_reservation_invalidates_empty_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import autodj.index_manifest as manifest_module

    _write_working_artifacts(tmp_path, 1)
    empty = current_snapshot_token(tmp_path)
    monkeypatch.setattr(
        manifest_module,
        "_checkpoint_working_tracks",
        lambda _index_dir: (_ for _ in ()).throw(OSError("checkpoint failed")),
    )
    with pytest.raises(OSError, match="checkpoint failed"):
        publish_manifest(tmp_path, 1)

    assert current_snapshot_token(tmp_path) == IndexSnapshotToken(0, 1)
    with pytest.raises(IndexConsistencyError, match="expected generation"):
        manifest_module.require_snapshot_token(tmp_path, empty)


def test_manifest_rejects_mismatched_v2_identity_and_state_bounds(tmp_path: Path) -> None:
    payload = _manifest_payload(schema_version=2, state_revision=2)
    (tmp_path / "index-manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(IndexConsistencyError, match="generation/count"):
        read_manifest(tmp_path)

    payload["state_revision"] = 1
    (tmp_path / "index-manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / ".index-publication-state.json").write_text(
        json.dumps({"high_water": 1, "tombstone_revision": 2}), encoding="utf-8"
    )
    with pytest.raises(IndexConsistencyError, match="publication state"):
        read_manifest(tmp_path)


def test_tombstoned_stale_manifest_is_logically_empty_and_publish_recovers(tmp_path: Path) -> None:
    from autodj.indexer import load_index

    _write_working_artifacts(tmp_path, 2)
    first = publish_manifest(tmp_path, 2)
    tombstone_publication(tmp_path)

    assert (tmp_path / "index-manifest.json").exists()
    assert read_manifest(tmp_path) is None
    assert current_snapshot_token(tmp_path).generation == 0
    with pytest.raises(IndexConsistencyError, match="publication history"):
        load_index(tmp_path)

    recovered = publish_manifest(tmp_path, 2)
    assert recovered.generation > first.generation
    assert read_manifest(tmp_path) == recovered


def test_second_tombstone_supersedes_newer_live_manifest(tmp_path: Path) -> None:
    _write_working_artifacts(tmp_path, 1)
    publish_manifest(tmp_path, 1)
    tombstone_publication(tmp_path)
    first_empty = current_snapshot_token(tmp_path)
    recovered = publish_manifest(tmp_path, 1)
    assert read_manifest(tmp_path) == recovered

    tombstone_publication(tmp_path)

    assert read_manifest(tmp_path) is None
    second_empty = current_snapshot_token(tmp_path)
    assert second_empty.generation == 0
    assert second_empty.state_revision > first_empty.state_revision


def test_v1_manifest_remains_live_with_initialized_non_tombstone_state(tmp_path: Path) -> None:
    _write_working_artifacts(tmp_path, 1)
    published = publish_manifest(tmp_path, 1)
    path = tmp_path / "index-manifest.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 1
    del raw["state_revision"]
    path.write_text(json.dumps(raw), encoding="utf-8")

    legacy = read_manifest(tmp_path)
    assert legacy is not None
    assert legacy.schema_version == 1
    copied = copy_published_snapshot(tmp_path, tmp_path / "backup")
    assert copied.schema_version == 2
    assert read_manifest(tmp_path / "backup") == copied
    upgraded = publish_manifest(tmp_path, 1)
    assert upgraded.schema_version == 2
    assert upgraded.generation > published.generation


def test_manifest_free_cores_with_publication_history_are_not_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import autodj.index_manifest as manifest_module
    from autodj.indexer import load_index

    _write_working_artifacts(tmp_path, 1)
    monkeypatch.setattr(
        manifest_module,
        "_checkpoint_working_tracks",
        lambda _index_dir: (_ for _ in ()).throw(OSError("checkpoint failed")),
    )
    with pytest.raises(OSError, match="checkpoint failed"):
        publish_manifest(tmp_path, 1)

    with pytest.raises(IndexConsistencyError, match="publication history"):
        load_index(tmp_path)


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


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"unexpected": 1},
        {"high_water": True, "tombstone_revision": 0},
        {"revision": 1, "high_water_generation": 0, "tombstone": 1},
    ],
)
def test_publication_state_rejects_invalid_shapes(tmp_path: Path, payload: object) -> None:
    import autodj.index_manifest as manifest_module

    (tmp_path / manifest_module.PUBLICATION_STATE_NAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )

    with pytest.raises(IndexConsistencyError, match="invalid publication state"):
        manifest_module._read_publication_state(tmp_path)


@pytest.mark.parametrize("tombstone", [False, True])
def test_legacy_publication_state_is_migrated_in_memory(tmp_path: Path, tombstone: bool) -> None:
    import autodj.index_manifest as manifest_module

    (tmp_path / manifest_module.PUBLICATION_STATE_NAME).write_text(
        json.dumps(
            {
                "revision": 3,
                "high_water_generation": 5,
                "tombstone": tombstone,
            }
        ),
        encoding="utf-8",
    )

    state = manifest_module._read_publication_state(tmp_path)

    assert state == manifest_module._PublicationState(5, 3 if tombstone else 0)


def test_publication_state_rejects_out_of_range_counters(tmp_path: Path) -> None:
    import autodj.index_manifest as manifest_module

    (tmp_path / manifest_module.PUBLICATION_STATE_NAME).write_text(
        json.dumps({"high_water": 1, "tombstone_revision": 2}), encoding="utf-8"
    )

    with pytest.raises(IndexConsistencyError, match="counters"):
        manifest_module._read_publication_state(tmp_path)


def test_repeated_tombstone_is_idempotent(tmp_path: Path) -> None:
    tombstone_publication(tmp_path)
    before = current_snapshot_token(tmp_path)

    tombstone_publication(tmp_path)

    assert current_snapshot_token(tmp_path) == before


def test_manifest_rejects_invalid_digest(tmp_path: Path) -> None:
    (tmp_path / "index-manifest.json").write_text(
        json.dumps(_manifest_payload(tracks_sha256="not-a-digest")), encoding="utf-8"
    )

    with pytest.raises(IndexConsistencyError, match="SHA-256"):
        read_manifest(tmp_path)


def test_manifest_revision_cannot_exceed_publication_state(tmp_path: Path) -> None:
    payload = _manifest_payload(schema_version=2, state_revision=1)
    (tmp_path / "index-manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / ".index-publication-state.json").write_text(
        json.dumps({"high_water": 0, "tombstone_revision": 0}), encoding="utf-8"
    )

    with pytest.raises(IndexConsistencyError, match="exceeds publication state"):
        read_manifest(tmp_path)


def test_thread_lock_resets_state_after_pid_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import autodj.index_manifest as manifest_module

    monkeypatch.setattr(manifest_module, "_LOCKS_PROCESS_ID", -1)

    lock = manifest_module._thread_lock(tmp_path)

    assert os.getpid() == manifest_module._LOCKS_PROCESS_ID
    assert manifest_module._LOCKS[tmp_path] is lock


def test_fsync_directory_ignores_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import autodj.index_manifest as manifest_module

    fsync = MagicMock()
    monkeypatch.setattr(manifest_module.os, "open", MagicMock(side_effect=OSError("denied")))
    monkeypatch.setattr(manifest_module.os, "fsync", fsync)

    manifest_module.fsync_directory(tmp_path)

    fsync.assert_not_called()


def test_fsync_directory_closes_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import autodj.index_manifest as manifest_module

    fsync = MagicMock()
    close = MagicMock()
    monkeypatch.setattr(manifest_module.os, "open", MagicMock(return_value=91))
    monkeypatch.setattr(manifest_module.os, "fsync", fsync)
    monkeypatch.setattr(manifest_module.os, "close", close)

    manifest_module.fsync_directory(tmp_path)

    fsync.assert_called_once_with(91)
    close.assert_called_once_with(91)


def test_checkpoint_rejects_busy_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import autodj.index_manifest as manifest_module

    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = (1, 2, 0)
    monkeypatch.setattr(manifest_module.sqlite3, "connect", MagicMock(return_value=connection))

    with pytest.raises(IndexConsistencyError, match="checkpoint incomplete"):
        manifest_module._checkpoint_working_tracks(tmp_path)

    connection.close.assert_called_once()


def test_checkpoint_rejects_nonempty_wal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import autodj.index_manifest as manifest_module

    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = (0, 0, 1)
    monkeypatch.setattr(manifest_module.sqlite3, "connect", MagicMock(return_value=connection))
    (tmp_path / "tracks.db-wal").write_bytes(b"pending")

    with pytest.raises(IndexConsistencyError, match="WAL remains"):
        manifest_module._checkpoint_working_tracks(tmp_path)


def test_cleanup_warns_when_old_generation_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import autodj.index_manifest as manifest_module

    old = tmp_path / "tracks.g00000000000000000001.db"
    old.touch()
    monkeypatch.setattr(Path, "unlink", MagicMock(side_effect=OSError("busy")))
    monkeypatch.setattr(manifest_module, "fsync_directory", MagicMock())

    manifest_module._cleanup_generations(tmp_path, keep=set())

    assert "Could not remove old index generation" in caplog.text


def test_restore_requires_live_expected_generation(tmp_path: Path) -> None:
    with pytest.raises(IndexConsistencyError, match="manifest is missing"):
        restore_working_snapshot(tmp_path)

    _write_working_artifacts(tmp_path, 1)
    manifest = publish_manifest(tmp_path, 1)
    with pytest.raises(IndexConsistencyError, match="expected generation"):
        restore_working_snapshot(tmp_path, expected_generation=manifest.generation + 1)


def test_copy_requires_live_expected_generation_and_new_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    with pytest.raises(IndexConsistencyError, match="manifest is missing"):
        copy_published_snapshot(source, tmp_path / "backup")

    _write_working_artifacts(source, 1)
    manifest = publish_manifest(source, 1)
    with pytest.raises(IndexConsistencyError, match="expected generation"):
        copy_published_snapshot(
            source,
            tmp_path / "backup",
            expected_generation=manifest.generation + 1,
        )

    destination = tmp_path / "backup"
    destination.mkdir()
    with pytest.raises(FileExistsError):
        copy_published_snapshot(source, destination)


def test_snapshot_validation_rejects_tracks_and_vectors_hash_changes(tmp_path: Path) -> None:
    import autodj.index_manifest as manifest_module

    _write_working_artifacts(tmp_path, 1)
    manifest = publish_manifest(tmp_path, 1)
    (tmp_path / manifest.tracks_file).write_bytes(b"changed")
    with pytest.raises(IndexConsistencyError, match="tracks SHA-256"):
        manifest_module._validate_snapshot_files(tmp_path, manifest)

    _write_working_artifacts(tmp_path, 1)
    manifest = publish_manifest(tmp_path, 1)
    (tmp_path / manifest.vectors_file).write_bytes(b"changed")
    with pytest.raises(IndexConsistencyError, match="vectors SHA-256"):
        manifest_module._validate_snapshot_files(tmp_path, manifest)


def test_snapshot_validation_rejects_vector_count_mismatch(tmp_path: Path) -> None:
    import autodj.index_manifest as manifest_module

    _write_working_artifacts(tmp_path, 1)
    manifest = publish_manifest(tmp_path, 1)
    vectors = faiss.IndexFlatIP(2)
    vectors.add(np.ones((2, 2), dtype=np.float32))
    vectors_path = tmp_path / manifest.vectors_file
    faiss.write_index(vectors, str(vectors_path))
    changed = manifest_module.replace(manifest, vectors_sha256=sha256_file(vectors_path))

    with pytest.raises(IndexConsistencyError, match="index count mismatch"):
        manifest_module._validate_snapshot_files(tmp_path, changed)


def test_snapshot_validation_rejects_extra_tracks_column(tmp_path: Path) -> None:
    import autodj.index_manifest as manifest_module

    _write_working_artifacts(tmp_path, 1)
    manifest = publish_manifest(tmp_path, 1)
    tracks_path = tmp_path / manifest.tracks_file
    connection = sqlite3.connect(tracks_path)
    try:
        connection.execute("ALTER TABLE tracks ADD COLUMN unexpected TEXT")
        connection.commit()
    finally:
        connection.close()
    changed = manifest_module.replace(manifest, tracks_sha256=sha256_file(tracks_path))

    with pytest.raises(IndexConsistencyError, match="schema does not match"):
        manifest_module._validate_snapshot_files(tmp_path, changed)


def test_snapshot_validation_requires_unique_track_identities(tmp_path: Path) -> None:
    import autodj.index_manifest as manifest_module

    _write_working_artifacts(tmp_path, 1)
    manifest = publish_manifest(tmp_path, 1)
    tracks_path = tmp_path / manifest.tracks_file
    columns = ", ".join(
        f"{name} {kind} NOT NULL" for name, kind in manifest_module._TRACKS_SCHEMA_CONTRACT
    )
    names = ", ".join(name for name, _kind in manifest_module._TRACKS_SCHEMA_CONTRACT)
    connection = sqlite3.connect(tracks_path)
    try:
        connection.execute(f"CREATE TABLE replacement ({columns})")
        connection.execute(f"INSERT INTO replacement ({names}) SELECT {names} FROM tracks")
        connection.execute("DROP TABLE tracks")
        connection.execute("ALTER TABLE replacement RENAME TO tracks")
        connection.execute("CREATE INDEX tracks_title_idx ON tracks(title)")
        connection.commit()
    finally:
        connection.close()
    changed = manifest_module.replace(manifest, tracks_sha256=sha256_file(tracks_path))

    with pytest.raises(IndexConsistencyError, match="unique identities"):
        manifest_module._validate_snapshot_files(tmp_path, changed)
