"""Durable publication boundary for coherent index generations."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock

from autodj.fsutil import atomic_write, fsync_directory
from autodj.sqlite_utils import readonly_uri

SCHEMA_VERSION = 2
MANIFEST_NAME = "index-manifest.json"
PUBLICATION_STATE_NAME = ".index-publication-state.json"
_GENERATION_RE = re.compile(r"^(tracks|vectors)\.g(\d{20})\.(db|index)$")
_PUBLICATION_TEMP_RE = re.compile(
    r"^\.(?:index-manifest\.json|\.index-publication-state\.json|"
    r"tracks\.g\d{20}\.db|vectors\.g\d{20}\.index)\.[0-9a-f]{32}\.tmp$"
)
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
logger = logging.getLogger(__name__)


class IndexConsistencyError(RuntimeError):
    """Raised when published index artifacts do not describe one snapshot."""


REBUILD_COMMAND = "autodj index --force"


class UnsupportedIndexError(IndexConsistencyError):
    """Raised when index data was written in a format this release no longer reads."""

    def __init__(self, index_dir: Path | str, reason: str) -> None:
        """Build the one rebuild message shared by every old-format refusal.

        Args:
            index_dir: Index directory holding the old data, or a description
                of where it is, such as "this backup".
            reason: What marks the data as old, e.g. "it has no index-manifest.json".
        """
        self.reason = reason
        super().__init__(
            f"The index in {index_dir} was made by an older AutoDJ ({reason}). "
            f"Rebuild it with `{REBUILD_COMMAND}`."
        )


class OldDjMetaCacheError(ValueError):
    """Raised when ``dj_meta.db`` stores absolute track paths, as releases before relative keys did."""

    def __init__(self, cache_path: Path, absolute: str) -> None:
        """Build the message telling the user to delete and rebuild the cache.

        Args:
            cache_path: The ``dj_meta.db`` file.
            absolute: One absolute path found in it.
        """
        super().__init__(
            f"The DJ metadata cache {cache_path} was made by an older AutoDJ "
            f"(it stores the absolute path {absolute}). "
            "Delete it and run `autodj analyse` to rebuild it."
        )


def first_absolute_path(conn: sqlite3.Connection, table: str) -> str | None:
    """Return one absolute path stored in *table*, or ``None`` when all are relative.

    Args:
        conn: Open connection to ``tracks.db`` or ``dj_meta.db``.
        table: ``"tracks"`` or ``"dj_meta"``; both keep the path in a ``path`` column.

    Returns:
        The first absolute path found, matching :func:`is_absolute_storage`.
    """
    query = {
        "tracks": "SELECT path FROM tracks",
        "dj_meta": "SELECT path FROM dj_meta",
    }[table]
    row = conn.execute(
        query + " WHERE substr(path, 1, 1) IN ('/', '\\') OR substr(path, 2, 1) = ':' LIMIT 1"
    ).fetchone()
    return None if row is None else str(row[0])


def is_absolute_storage(path: str) -> bool:
    """Whether a stored *path* is absolute: a POSIX root, a UNC share, or a drive letter.

    Args:
        path: Path text as stored in ``tracks.db`` or ``dj_meta.db``.

    Returns:
        ``True`` for an absolute path on any platform.
    """
    return path.startswith(("/", "\\")) or path[1:2] == ":"


def relative_storage_path(path: str | os.PathLike[str], music_dir: Path | None) -> str:
    """Return *path* as a forward-slashed path relative to *music_dir*.

    Both sides are compared component by component after
    ``os.path.normpath`` and ``os.path.normcase``, so on Windows a
    drive-letter or folder case difference, or mixed separators, still
    match.  The result keeps the path's own spelling.  Pure string work:
    nothing is resolved or stat()ed, which matters on network libraries.

    Args:
        path: Runtime track path.
        music_dir: Library root.  ``None`` keeps already-relative paths as-is.

    Returns:
        The relative, forward-slashed path to store.

    Raises:
        ValueError: If *path* is absolute and not under *music_dir*.
    """
    parts = Path(os.path.normpath(path)).parts
    if music_dir is not None:
        root = Path(os.path.normpath(music_dir)).parts  # "." has no parts
        if (
            root
            and len(parts) > len(root)
            and all(
                os.path.normcase(a) == os.path.normcase(b)
                for a, b in zip(parts, root, strict=False)
            )
        ):
            return "/".join(parts[len(root) :])
    stored = Path(path).as_posix()
    if is_absolute_storage(stored):
        raise ValueError(f"{path} is not under music_dir {music_dir}")
    return stored


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
    state_revision: int


@dataclass(frozen=True)
class IndexSnapshotToken:
    """Optimistic-concurrency identity; generation zero means no manifest."""

    generation: int
    state_revision: int = 0

    def __post_init__(self) -> None:
        """Reject negative snapshot identity components."""
        if self.generation < 0 or self.state_revision < 0:
            raise ValueError("snapshot generation must be non-negative")


def snapshot_token_for_manifest(manifest: IndexManifest) -> IndexSnapshotToken:
    """Return a live manifest's exact identity."""
    if manifest.generation < 1 or manifest.state_revision != manifest.generation:
        raise ValueError("published manifest token must be positive")
    return IndexSnapshotToken(manifest.generation, manifest.state_revision)


_OLD_PUBLICATION_STATE_KEYS = frozenset({"revision", "high_water_generation", "tombstone"})


@dataclass(frozen=True)
class _PublicationState:
    """Store publication revision and tombstone counters."""

    high_water: int
    tombstone_revision: int


def _read_publication_state(index_dir: Path) -> _PublicationState | None:
    """Read and validate durable publication counters when present."""
    path = index_dir / PUBLICATION_STATE_NAME
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if type(raw) is dict and set(raw) == _OLD_PUBLICATION_STATE_KEYS:
            raise UnsupportedIndexError(index_dir, "its publication state uses the old layout")
        if (
            type(raw) is not dict
            or set(raw) != {"high_water", "tombstone_revision"}
            or type(raw["high_water"]) is not int
            or type(raw["tombstone_revision"]) is not int
        ):
            raise ValueError("invalid publication state")
        state = _PublicationState(**raw)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise IndexConsistencyError(f"invalid publication state: {exc}") from exc
    if not 0 <= state.tombstone_revision <= state.high_water:
        raise IndexConsistencyError("invalid publication state counters")
    return state


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace *path* with *payload* as sorted, newline-terminated JSON."""
    atomic_write(path, json.dumps(payload, sort_keys=True) + "\n")


def _write_publication_state(index_dir: Path, state: _PublicationState) -> None:
    """Atomically replace publication counters and flush the directory when supported."""
    _atomic_json_write(index_dir / PUBLICATION_STATE_NAME, asdict(state))


def _next_revision(state: _PublicationState, manifest: IndexManifest | None) -> int:
    """Return the next never-reused publication revision for *index_dir*'s state."""
    return (
        max(
            state.high_water,
            state.tombstone_revision,
            0 if manifest is None else manifest.generation,
            0 if manifest is None else manifest.state_revision,
        )
        + 1
    )


def _state_for_manifest(index_dir: Path, manifest: IndexManifest | None) -> _PublicationState:
    """Return stored publication state or derive its initial counters."""
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


def _never_published(index_dir: Path) -> bool:
    """Whether *index_dir* has neither a live manifest nor publication history."""
    return read_manifest(index_dir) is None and _read_publication_state(index_dir) is None


def require_current_format(index_dir: Path) -> None:
    """Refuse working index files that no manifest publication ever produced.

    AutoDJ releases before the manifest format wrote ``tracks.db`` and
    ``vectors.index`` alone.  Those indexes are no longer read.

    Raises:
        UnsupportedIndexError: If ``tracks.db`` or ``vectors.index`` exists
            in a directory that was never published.
    """
    if _never_published(index_dir) and any(
        (index_dir / name).exists() for name in ("tracks.db", "vectors.index")
    ):
        raise UnsupportedIndexError(index_dir, "it has no index-manifest.json")


def require_no_orphan_generations(index_dir: Path) -> None:
    """Refuse a manifest-free directory that still holds published generation files.

    Such a directory is not empty: its manifest was lost.  Commands that
    would otherwise report "no index" must say so instead.

    Raises:
        UnsupportedIndexError: If the directory holds a pre-manifest index.
        IndexConsistencyError: If generation files exist without a live
            manifest and the publication is not tombstoned.
    """
    require_current_format(index_dir)
    if publication_is_tombstoned(index_dir):
        return
    if any(_GENERATION_RE.fullmatch(path.name) for path in index_dir.iterdir()):
        raise IndexConsistencyError(
            f"{index_dir} holds index generation files but no index-manifest.json; "
            f"rebuild it with `{REBUILD_COMMAND}`"
        )


def reserve_first_publication(index_dir: Path) -> None:
    """Record publication state before a never-published directory gets working files.

    Without it, a build killed between its first working write and its
    first publication leaves ``tracks.db`` and ``vectors.index`` with no
    manifest or state, which :func:`require_current_format` cannot tell
    from an index made before the manifest format.  With the state file
    present the next run discards those working files and starts over.
    """
    with publication_lock(index_dir):
        if _read_publication_state(index_dir) is None and read_manifest(index_dir) is None:
            _write_publication_state(
                index_dir, _PublicationState(high_water=0, tombstone_revision=0)
            )


def discard_publication_record(index_dir: Path) -> None:
    """Delete an unreadable or old manifest and publication state for a forced rebuild.

    ``autodj index --force`` is the recovery path for an index this release
    cannot read, so it must not fail on the very files it replaces.  Old
    generation files are left for the next publication's cleanup.
    """
    with publication_lock(index_dir):
        for name in (MANIFEST_NAME, PUBLICATION_STATE_NAME):
            (index_dir / name).unlink(missing_ok=True)
        fsync_directory(index_dir)


def publication_is_tombstoned(index_dir: Path) -> bool:
    """Whether a committed logical-empty state currently wins."""
    state = _read_publication_state(index_dir)
    return state is not None and state.tombstone_revision > 0 and read_manifest(index_dir) is None


def publication_is_pristine(index_dir: Path) -> bool:
    """Return whether no committed, working, or interrupted publication exists."""
    with publication_lock(index_dir):
        if not _never_published(index_dir):
            return False
        return not any(
            path.name in _WORKING_ARTIFACT_NAMES
            or _GENERATION_RE.fullmatch(path.name) is not None
            or _PUBLICATION_TEMP_RE.fullmatch(path.name) is not None
            for path in index_dir.iterdir()
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
        revision = _next_revision(state, manifest)
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
            "state_revision",
        }
        if type(raw) is not dict:
            raise IndexConsistencyError("invalid index manifest structure")
        version = raw.get("schema_version")
        if type(version) is int and version < SCHEMA_VERSION:
            raise UnsupportedIndexError(index_dir, f"its manifest uses schema {version}")
        if version != SCHEMA_VERSION:
            raise IndexConsistencyError("unsupported manifest schema")
        if set(raw) != fields:
            raise IndexConsistencyError("invalid index manifest structure")
        int_fields = ("schema_version", "generation", "vector_count", "state_revision")
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
            state_revision=raw["state_revision"],
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise IndexConsistencyError(f"invalid index manifest: {exc}") from exc
    if (
        manifest.generation < 1
        or manifest.vector_count < 0
        or manifest.state_revision != manifest.generation
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
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _immutable_sqlite_uri(path: Path) -> str:
    """Build a read-only immutable SQLite URI, including for Windows UNC paths."""
    return readonly_uri(path, immutable=True)


@contextmanager
def publication_lock(index_dir: Path) -> Iterator[None]:
    """Serialize index readers and publishers across threads and processes.

    Reentrant within one thread; other threads and processes wait.
    """
    index_dir = index_dir.resolve()
    index_dir.mkdir(parents=True, exist_ok=True)
    # One shared instance per path gives per-thread reentrancy.  Keep the lock
    # file on release: doctor treats a published index without it as damaged.
    lock = FileLock(
        index_dir / ".index-publication.lock", is_singleton=True, preserve_lock_file=True
    )
    with lock:
        yield


def _durable_copy(source: Path, destination: Path) -> None:
    """Copy a file through a flushed temporary replacement."""
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
    """Checkpoint the working tracks database and require an empty WAL."""
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


def _cleanup_generations(index_dir: Path, keep: set[int]) -> None:
    """Remove obsolete generation files and flush the directory."""
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
        revision = _next_revision(state, previous)
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
        _atomic_json_write(index_dir / MANIFEST_NAME, asdict(manifest))
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
    """Validate published artifact hashes, schemas, identities, and counts."""
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
                tracks_file="tracks.db",
                vectors_file="vectors.index",
            )
            _atomic_json_write(staging / MANIFEST_NAME, asdict(copied))
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
