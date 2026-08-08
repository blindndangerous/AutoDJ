"""Durable publication boundary for coherent index generations."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

SCHEMA_VERSION = 1
MANIFEST_NAME = "index-manifest.json"
_GENERATION_RE = re.compile(r"^(tracks|vectors)\.g(\d{20})\.(db|index)$")
_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_WINDOWS_LOCK_RETRY_SECONDS = 0.1
logger = logging.getLogger(__name__)


class _LockState(threading.local):
    def __init__(self) -> None:
        self.paths: set[Path] = set()


_HELD_LOCKS = _LockState()


class IndexConsistencyError(RuntimeError):
    """Raised when published index artifacts do not describe one snapshot."""


@dataclass(frozen=True)
class IndexManifest:
    """Identity and integrity metadata for one published index generation."""

    schema_version: int
    generation: int
    vector_count: int
    published_at: str
    tracks_file: str
    vectors_file: str
    tracks_sha256: str
    vectors_sha256: str


@dataclass(frozen=True)
class IndexSnapshotToken:
    """Optimistic-concurrency identity; generation zero means no manifest."""

    generation: int

    def __post_init__(self) -> None:
        if self.generation < 0:
            raise ValueError("snapshot generation must be non-negative")


def snapshot_token_for_manifest(manifest: IndexManifest | None) -> IndexSnapshotToken:
    """Return explicit identity for a manifest or its confirmed absence."""
    return IndexSnapshotToken(0 if manifest is None else manifest.generation)


def current_snapshot_token(index_dir: Path) -> IndexSnapshotToken:
    """Read current manifest identity; caller must hold publication lock for mutation."""
    return snapshot_token_for_manifest(read_manifest(index_dir))


def require_snapshot_token(
    index_dir: Path,
    expected: IndexSnapshotToken,
) -> IndexManifest | None:
    """Raise unless current manifest identity exactly matches *expected*."""
    current = read_manifest(index_dir)
    actual = snapshot_token_for_manifest(current)
    if actual != expected:
        raise IndexConsistencyError(
            f"expected generation {expected.generation}, got {actual.generation}"
        )
    return current


def read_manifest(index_dir: Path) -> IndexManifest | None:
    """Read and validate the current generation manifest, when present."""
    path = index_dir / MANIFEST_NAME
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        fields = {
            "schema_version",
            "generation",
            "vector_count",
            "published_at",
            "tracks_file",
            "vectors_file",
            "tracks_sha256",
            "vectors_sha256",
        }
        if type(raw) is not dict or set(raw) != fields:
            raise IndexConsistencyError("invalid index manifest structure")
        int_fields = ("schema_version", "generation", "vector_count")
        string_fields = tuple(fields - set(int_fields))
        if any(type(raw[field]) is not int for field in int_fields) or any(
            type(raw[field]) is not str for field in string_fields
        ):
            raise IndexConsistencyError("invalid index manifest field types")
        manifest = IndexManifest(
            schema_version=raw["schema_version"],
            generation=raw["generation"],
            vector_count=raw["vector_count"],
            published_at=raw["published_at"],
            tracks_file=raw["tracks_file"],
            vectors_file=raw["vectors_file"],
            tracks_sha256=raw["tracks_sha256"],
            vectors_sha256=raw["vectors_sha256"],
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise IndexConsistencyError(f"invalid index manifest: {exc}") from exc
    if manifest.schema_version != SCHEMA_VERSION:
        raise IndexConsistencyError(
            f"unsupported manifest schema {manifest.schema_version}; expected {SCHEMA_VERSION}"
        )
    if manifest.generation < 1 or manifest.vector_count < 0:
        raise IndexConsistencyError("manifest generation/count must be non-negative")
    canonical_pair = ("tracks.db", "vectors.index")
    generation_pair = (
        f"tracks.g{manifest.generation:020d}.db",
        f"vectors.g{manifest.generation:020d}.index",
    )
    if (manifest.tracks_file, manifest.vectors_file) not in {canonical_pair, generation_pair}:
        raise IndexConsistencyError("manifest artifacts do not match its generation")
    for digest in (manifest.tracks_sha256, manifest.vectors_sha256):
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise IndexConsistencyError("manifest contains an invalid SHA-256 digest")
    return manifest


def sha256_file(path: Path) -> str:
    """Return lowercase SHA-256 digest for *path*."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _immutable_sqlite_uri(path: Path) -> str:
    """Build a read-only immutable SQLite URI, including for Windows UNC paths."""
    encoded = quote(str(path.resolve()), safe="/:\\")
    return f"file:{encoded}?mode=ro&immutable=1"


def _thread_lock(path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.RLock())


def _acquire_os_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN} and getattr(
                    exc, "winerror", None
                ) not in {32, 33}:
                    raise
                time.sleep(_WINDOWS_LOCK_RETRY_SECONDS)
            else:
                break
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]


def _release_os_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


@contextmanager
def publication_lock(index_dir: Path) -> Iterator[None]:
    """Serialize index readers and publishers across threads and processes."""
    index_dir = index_dir.resolve()
    index_dir.mkdir(parents=True, exist_ok=True)
    local_lock = _thread_lock(index_dir)
    with local_lock:
        held = _HELD_LOCKS.paths
        if index_dir in held:
            yield
            return
        lock_path = index_dir / ".index-publication.lock"
        with lock_path.open("a+b") as handle:
            _acquire_os_lock(handle)
            _HELD_LOCKS.paths = {*held, index_dir}
            try:
                yield
            finally:
                _HELD_LOCKS.paths = held
                _release_os_lock(handle)


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_copy(source: Path, destination: Path) -> None:
    tmp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as reader, tmp.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)


def _checkpoint_working_tracks(index_dir: Path) -> None:
    db_path = index_dir / "tracks.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        busy, remaining, _checkpointed = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.close()
    if busy or remaining:
        raise IndexConsistencyError(
            f"tracks WAL checkpoint incomplete: busy={busy}, remaining={remaining}"
        )
    wal = db_path.with_name(f"{db_path.name}-wal")
    if wal.exists() and wal.stat().st_size:
        raise IndexConsistencyError("tracks WAL remains non-empty after checkpoint")


def _write_manifest(path: Path, manifest: IndexManifest) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(asdict(manifest), handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _cleanup_generations(index_dir: Path, keep: set[int]) -> None:
    for path in index_dir.iterdir():
        match = _GENERATION_RE.fullmatch(path.name)
        if match is None or int(match.group(2)) in keep:
            continue
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("Could not remove old index generation %s: %s", path.name, exc)
    fsync_directory(index_dir)


def publish_manifest(index_dir: Path, vector_count: int) -> IndexManifest:
    """Publish canonical working files as a new immutable generation."""
    with publication_lock(index_dir):
        previous = read_manifest(index_dir)
        generation = 1 if previous is None else previous.generation + 1
        _checkpoint_working_tracks(index_dir)
        tracks_name = f"tracks.g{generation:020d}.db"
        vectors_name = f"vectors.g{generation:020d}.index"
        tracks_path = index_dir / tracks_name
        vectors_path = index_dir / vectors_name
        _durable_copy(index_dir / "tracks.db", tracks_path)
        _durable_copy(index_dir / "vectors.index", vectors_path)
        manifest = IndexManifest(
            schema_version=SCHEMA_VERSION,
            generation=generation,
            vector_count=vector_count,
            published_at=datetime.now(UTC).isoformat(timespec="seconds"),
            tracks_file=tracks_name,
            vectors_file=vectors_name,
            tracks_sha256=sha256_file(tracks_path),
            vectors_sha256=sha256_file(vectors_path),
        )
        _validate_snapshot_files(index_dir, manifest)
        _write_manifest(index_dir / MANIFEST_NAME, manifest)
        keep = {generation}
        if previous is not None:
            keep.add(previous.generation)
        _cleanup_generations(index_dir, keep)
        return manifest


def restore_working_snapshot(
    index_dir: Path,
    *,
    expected_generation: int | None = None,
) -> IndexManifest:
    """Restore mutable working files from one validated live generation."""
    with publication_lock(index_dir):
        manifest = read_manifest(index_dir)
        if manifest is None:
            raise IndexConsistencyError("published manifest is missing")
        if expected_generation is not None and manifest.generation != expected_generation:
            raise IndexConsistencyError(
                f"expected generation {expected_generation}, got {manifest.generation}"
            )
        _validate_snapshot_files(index_dir, manifest)
        (index_dir / "tracks.db-wal").unlink(missing_ok=True)
        (index_dir / "tracks.db-shm").unlink(missing_ok=True)
        _durable_copy(index_dir / manifest.tracks_file, index_dir / "tracks.db")
        _durable_copy(index_dir / manifest.vectors_file, index_dir / "vectors.index")
        fsync_directory(index_dir)
        return manifest


def _validate_snapshot_files(root: Path, manifest: IndexManifest) -> None:
    tracks = root / manifest.tracks_file
    vectors = root / manifest.vectors_file
    if sha256_file(tracks) != manifest.tracks_sha256:
        raise IndexConsistencyError("tracks SHA-256 mismatch")
    if sha256_file(vectors) != manifest.vectors_sha256:
        raise IndexConsistencyError("vectors SHA-256 mismatch")
    conn = sqlite3.connect(_immutable_sqlite_uri(tracks), uri=True)
    try:
        sqlite_count = int(conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0])
    finally:
        conn.close()
    import faiss

    faiss_count = int(faiss.read_index(str(vectors)).ntotal)
    if sqlite_count != manifest.vector_count or faiss_count != manifest.vector_count:
        raise IndexConsistencyError(
            f"index count mismatch: manifest={manifest.vector_count}, "
            f"sqlite={sqlite_count}, faiss={faiss_count}"
        )


def copy_published_snapshot(
    index_dir: Path,
    destination: Path,
    *,
    expected_generation: int | None = None,
) -> IndexManifest:
    """Copy one validated published generation to canonical backup files."""
    with publication_lock(index_dir):
        before = read_manifest(index_dir)
        if before is None:
            raise IndexConsistencyError("published manifest is missing")
        if expected_generation is not None and before.generation != expected_generation:
            raise IndexConsistencyError(
                f"expected generation {expected_generation}, got {before.generation}"
            )
        _validate_snapshot_files(index_dir, before)
        if destination.exists():
            raise FileExistsError(destination)
        staging = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            staging.mkdir(parents=True)
            _durable_copy(index_dir / before.tracks_file, staging / "tracks.db")
            _durable_copy(index_dir / before.vectors_file, staging / "vectors.index")
            copied = replace(before, tracks_file="tracks.db", vectors_file="vectors.index")
            _write_manifest(staging / MANIFEST_NAME, copied)
            _validate_snapshot_files(staging, copied)
            after = read_manifest(index_dir)
            if after != before:
                raise IndexConsistencyError("generation changed while copying snapshot")
            os.replace(staging, destination)
            fsync_directory(destination.parent)
            return copied
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
