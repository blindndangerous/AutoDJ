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

SCHEMA_VERSION = 2
MANIFEST_NAME = "index-manifest.json"
PUBLICATION_STATE_NAME = ".index-publication-state.json"
_GENERATION_RE = re.compile(r"^(tracks|vectors)\.g(\d{20})\.(db|index)$")
_PUBLICATION_TEMP_RE = re.compile(
    r"^\.(?:index-manifest\.json|\.index-publication-state\.json|"
    r"tracks\.g\d{20}\.db|vectors\.g\d{20}\.index)\.[0-9a-f]{32}\.tmp$"
)
_FLAT_MIGRATION_STAGING_RE = re.compile(r"^\.flat-migration-[0-9a-f]{32}$")
_WORKING_ARTIFACT_NAMES = frozenset(
    {
        MANIFEST_NAME,
        PUBLICATION_STATE_NAME,
        "tracks.db",
        "tracks.db-wal",
        "tracks.db-shm",
        "vectors.index",
        "vectors.index.tmp",
    }
)
_TRACKS_SCHEMA_CONTRACT = (
    ("vec_row", "INTEGER"),
    ("path", "TEXT"),
    ("title", "TEXT"),
    ("artist", "TEXT"),
    ("album", "TEXT"),
    ("genre", "TEXT"),
    ("bpm", "REAL"),
    ("year", "INTEGER"),
    ("length", "REAL"),
    ("energy", "REAL"),
    ("key", "INTEGER"),
    ("mode", "INTEGER"),
    ("tempo_confidence", "REAL"),
    ("embedded_at", "REAL"),
)
_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_LOCKS_PROCESS_ID = os.getpid()
_WINDOWS_LOCK_RETRY_SECONDS = 0.1
logger = logging.getLogger(__name__)


class _LockState(threading.local):
    def __init__(self) -> None:
        self.paths: set[Path] = set()


_HELD_LOCKS = _LockState()


def _reset_process_local_locks() -> None:
    """Discard inherited lock ownership after fork."""
    # Never touch inherited synchronization primitives here: another thread
    # may have held one at fork time, making acquisition in the child hang.
    global _HELD_LOCKS, _LOCKS, _LOCKS_GUARD, _LOCKS_PROCESS_ID
    _LOCKS = {}
    _LOCKS_GUARD = threading.Lock()
    _HELD_LOCKS = _LockState()
    _LOCKS_PROCESS_ID = os.getpid()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_process_local_locks)


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
    state_revision: int = 0


@dataclass(frozen=True)
class IndexSnapshotToken:
    """Optimistic-concurrency identity; generation zero means no manifest."""

    generation: int
    state_revision: int = 0

    def __post_init__(self) -> None:
        if self.generation < 0 or self.state_revision < 0:
            raise ValueError("snapshot generation must be non-negative")


def snapshot_token_for_manifest(manifest: IndexManifest) -> IndexSnapshotToken:
    """Return a live manifest's exact identity, including schema-v1 tokens."""
    if manifest.generation < 1 or (
        manifest.schema_version == SCHEMA_VERSION
        and (manifest.state_revision < 1 or manifest.state_revision != manifest.generation)
    ):
        raise ValueError("published manifest token must be positive")
    return IndexSnapshotToken(manifest.generation, manifest.state_revision)


@dataclass(frozen=True)
class _PublicationState:
    high_water: int
    tombstone_revision: int


def _read_publication_state(index_dir: Path) -> _PublicationState | None:
    path = index_dir / PUBLICATION_STATE_NAME
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if type(raw) is not dict:
            raise ValueError("invalid publication state")
        if set(raw) == {"high_water", "tombstone_revision"}:
            if type(raw["high_water"]) is not int or type(raw["tombstone_revision"]) is not int:
                raise ValueError("invalid publication state")
            state = _PublicationState(**raw)
        elif set(raw) == {"revision", "high_water_generation", "tombstone"}:
            if (
                type(raw["revision"]) is not int
                or type(raw["high_water_generation"]) is not int
                or type(raw["tombstone"]) is not bool
            ):
                raise ValueError("invalid publication state")
            state = _PublicationState(
                high_water=max(raw["revision"], raw["high_water_generation"]),
                tombstone_revision=raw["revision"] if raw["tombstone"] else 0,
            )
        else:
            raise ValueError("invalid publication state")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise IndexConsistencyError(f"invalid publication state: {exc}") from exc
    if not 0 <= state.tombstone_revision <= state.high_water:
        raise IndexConsistencyError("invalid publication state counters")
    return state


def _write_publication_state(index_dir: Path, state: _PublicationState) -> None:
    path = index_dir / PUBLICATION_STATE_NAME
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(asdict(state), handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(index_dir)
    finally:
        temporary.unlink(missing_ok=True)


def _state_for_manifest(index_dir: Path, manifest: IndexManifest | None) -> _PublicationState:
    state = _read_publication_state(index_dir)
    if state is not None:
        return state
    return _PublicationState(
        high_water=0 if manifest is None else max(manifest.generation, manifest.state_revision),
        tombstone_revision=0,
    )


def current_snapshot_token(index_dir: Path) -> IndexSnapshotToken:
    """Read current manifest identity without mutating publication state."""
    manifest = read_manifest(index_dir)
    state = _state_for_manifest(index_dir, manifest)
    if manifest is not None:
        return IndexSnapshotToken(manifest.generation, manifest.state_revision)
    return IndexSnapshotToken(0, state.high_water)


def legacy_artifacts_allowed(index_dir: Path) -> bool:
    """Whether manifest-free canonical cores are an untouched legacy index."""
    if read_manifest(index_dir) is not None:
        return False
    state = _state_for_manifest(index_dir, None)
    return state.high_water == 0 and state.tombstone_revision == 0


def publication_is_tombstoned(index_dir: Path) -> bool:
    """Whether a committed logical-empty state currently wins."""
    state = _read_publication_state(index_dir)
    return state is not None and state.tombstone_revision > 0 and read_manifest(index_dir) is None


def publication_is_pristine(index_dir: Path) -> bool:
    """Return whether no committed, working, or interrupted publication exists."""
    with publication_lock(index_dir):
        if not legacy_artifacts_allowed(index_dir):
            return False
        return not any(
            path.name in _WORKING_ARTIFACT_NAMES
            or _GENERATION_RE.fullmatch(path.name) is not None
            or _PUBLICATION_TEMP_RE.fullmatch(path.name) is not None
            or _FLAT_MIGRATION_STAGING_RE.fullmatch(path.name) is not None
            for path in index_dir.iterdir()
        )


def publication_has_uncommitted_reservation(index_dir: Path) -> bool:
    """Whether reservation history exists without a committed live snapshot."""
    state = _read_publication_state(index_dir)
    return (
        state is not None
        and state.high_water > 0
        and state.tombstone_revision == 0
        and read_manifest(index_dir) is None
    )


def require_snapshot_token(
    index_dir: Path,
    expected: IndexSnapshotToken,
) -> IndexManifest | None:
    """Raise unless current manifest identity exactly matches *expected*."""
    actual = current_snapshot_token(index_dir)
    if actual != expected:
        raise IndexConsistencyError(
            f"expected generation {expected.generation}/{expected.state_revision}, got "
            f"{actual.generation}/{actual.state_revision}"
        )
    return None if actual.generation == 0 else read_manifest(index_dir)


def tombstone_publication(index_dir: Path) -> None:
    """Durably supersede the live snapshot before prune-all removes files."""
    with publication_lock(index_dir):
        manifest = read_manifest(index_dir)
        state = _state_for_manifest(index_dir, manifest)
        if manifest is None and state.tombstone_revision:
            return
        revision = (
            max(
                state.high_water,
                state.tombstone_revision,
                0 if manifest is None else manifest.generation,
                0 if manifest is None else manifest.state_revision,
            )
            + 1
        )
        _write_publication_state(
            index_dir,
            _PublicationState(
                high_water=revision,
                tombstone_revision=revision,
            ),
        )


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
        if type(raw) is not dict:
            raise IndexConsistencyError("invalid index manifest structure")
        if raw.get("schema_version") not in {1, SCHEMA_VERSION}:
            raise IndexConsistencyError("unsupported manifest schema")
        if raw["schema_version"] == SCHEMA_VERSION:
            fields.add("state_revision")
        if set(raw) != fields:
            raise IndexConsistencyError("invalid index manifest structure")
        int_fields: tuple[str, ...] = ("schema_version", "generation", "vector_count")
        if raw["schema_version"] == SCHEMA_VERSION:
            int_fields += ("state_revision",)
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
            state_revision=raw.get("state_revision", 0),
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise IndexConsistencyError(f"invalid index manifest: {exc}") from exc
    if manifest.schema_version not in {1, SCHEMA_VERSION}:
        raise IndexConsistencyError(
            f"unsupported manifest schema {manifest.schema_version}; expected {SCHEMA_VERSION}"
        )
    if (
        manifest.generation < 1
        or manifest.vector_count < 0
        or (
            manifest.schema_version == SCHEMA_VERSION
            and (manifest.state_revision <= 0 or manifest.state_revision != manifest.generation)
        )
    ):
        raise IndexConsistencyError("manifest generation/count must be non-negative")
    try:
        published = datetime.fromisoformat(manifest.published_at)
    except ValueError as exc:
        raise IndexConsistencyError("manifest timestamp is invalid") from exc
    if published.tzinfo != UTC or manifest.published_at != published.astimezone(UTC).isoformat(
        timespec="seconds"
    ):
        raise IndexConsistencyError("manifest timestamp must be canonical UTC")
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
    state = _read_publication_state(index_dir)
    if state is not None and manifest.state_revision > state.high_water:
        raise IndexConsistencyError("manifest revision exceeds publication state")
    if (
        state is not None
        and state.tombstone_revision > 0
        and manifest.state_revision <= state.tombstone_revision
    ):
        return None
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
    if os.getpid() != _LOCKS_PROCESS_ID:
        _reset_process_local_locks()
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
        state = _state_for_manifest(index_dir, previous)
        revision = (
            max(
                state.high_water,
                state.tombstone_revision,
                0 if previous is None else previous.generation,
                0 if previous is None else previous.state_revision,
            )
            + 1
        )
        state = _PublicationState(
            high_water=revision,
            tombstone_revision=state.tombstone_revision,
        )
        # This reserves a never-reused ID.  It intentionally leaves the
        # prior manifest live until the new manifest replace commits.
        _write_publication_state(index_dir, state)
        _checkpoint_working_tracks(index_dir)
        generation = revision
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
            state_revision=revision,
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
        columns = tuple(
            (str(row[1]), str(row[2]).strip().upper(), int(row[3]))
            for row in conn.execute("PRAGMA table_info(tracks)")
        )
        required = tuple((name, kind, 1) for name, kind in _TRACKS_SCHEMA_CONTRACT)
        if columns != required:
            raise IndexConsistencyError("tracks schema does not match published contract")
        unique_columns: set[str] = set()
        for index in conn.execute("PRAGMA index_list(tracks)"):
            if not int(index[2]):
                continue
            names = tuple(str(row[2]) for row in conn.execute(f"PRAGMA index_info({index[1]!r})"))
            if len(names) == 1:
                unique_columns.add(names[0])
        if not {"vec_row", "path"}.issubset(unique_columns):
            raise IndexConsistencyError("tracks schema is missing required unique identities")
        sqlite_count = int(conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0])
        vec_rows = [row[0] for row in conn.execute("SELECT vec_row FROM tracks ORDER BY vec_row")]
        if vec_rows != list(range(sqlite_count)):
            raise IndexConsistencyError("tracks vec_row identity is not canonical")
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
            copied = replace(
                before,
                schema_version=SCHEMA_VERSION,
                state_revision=before.generation,
                tracks_file="tracks.db",
                vectors_file="vectors.index",
            )
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
