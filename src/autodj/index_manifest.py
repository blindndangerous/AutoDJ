"""Publication of index generations: the manifest, its files and the lock.

An index directory holds, for its live generation *N*:

- ``tracks.gN.db``: SQLite metadata, one row per track.
- ``vectors.gN.index``: FAISS ``IndexFlatIP`` vectors in the same row order.
- ``index-manifest.json``: names generation *N*, its track count and the
  SHA-256 of both files.  Readers load only what the manifest names.

N is written as 20 digits.  A publication writes both files under
temporary names, renames them to their generation names, and then replaces
the manifest in one atomic write, so a reader sees the old generation or the
new one, never a mix.  The previous generation's files are kept until the
next publication, so a reader that read the old manifest can still open
them.  Copying an index to another machine works the same way: copy the
generation files first and the manifest last.

``.index-publication.lock`` serializes readers and publishers on one
machine.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock

from autodj.fsutil import atomic_write, fsync_directory

SCHEMA_VERSION = 4
MANIFEST_NAME = "index-manifest.json"
GENERATION_FILE_RE = re.compile(r"^(?:tracks|vectors)\.g(\d{20})\.(?:db|index)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
logger = logging.getLogger(__name__)


class IndexConsistencyError(RuntimeError):
    """Raised when published index artifacts do not describe one snapshot."""


REBUILD_COMMAND = "autodj index --force"
CONVERT_SCRIPT = "scripts/convert_index_v3_to_v4.py"


class UnsupportedIndexError(IndexConsistencyError):
    """Raised when index data was written in a format this release no longer reads."""

    def __init__(self, index_dir: Path | str, reason: str, remedy: str | None = None) -> None:
        """Build the one rebuild message shared by every old-format refusal.

        Args:
            index_dir: Index directory holding the old data, or a description
                of where it is, such as "this backup".
            reason: What marks the data as old, e.g. "it has no index-manifest.json".
            remedy: What to do about it.  Defaults to rebuilding with
                :data:`REBUILD_COMMAND`.
        """
        self.reason = reason
        super().__init__(
            f"The index in {index_dir} was made by an older AutoDJ ({reason}). "
            + (remedy or f"Rebuild it with `{REBUILD_COMMAND}`.")
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
    """The contents of ``index-manifest.json``: one published generation."""

    schema_version: int
    generation: int
    vector_count: int
    published_at: str
    tracks_sha256: str
    vectors_sha256: str

    @property
    def tracks_file(self) -> str:
        """File name of this generation's SQLite metadata."""
        return f"tracks.g{self.generation:020d}.db"

    @property
    def vectors_file(self) -> str:
        """File name of this generation's FAISS vectors."""
        return f"vectors.g{self.generation:020d}.index"


_MANIFEST_FIELDS = {
    "schema_version": int,
    "generation": int,
    "vector_count": int,
    "published_at": str,
    "tracks_sha256": str,
    "vectors_sha256": str,
}


def read_manifest(index_dir: Path) -> IndexManifest | None:
    """Read and validate ``index-manifest.json``.

    Args:
        index_dir: The index directory.

    Returns:
        The manifest, or ``None`` when the directory has none.

    Raises:
        UnsupportedIndexError: If the manifest uses an older schema.
        IndexConsistencyError: If the manifest is unreadable or malformed.
    """
    path = index_dir / MANIFEST_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IndexConsistencyError(f"cannot read {path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IndexConsistencyError(f"{path} is not valid JSON: {exc}") from exc
    if type(raw) is not dict:
        raise IndexConsistencyError(f"{path} is not a JSON object")
    version = raw.get("schema_version")
    if type(version) is int and version < SCHEMA_VERSION:
        raise UnsupportedIndexError(
            index_dir,
            f"its manifest uses schema {version}",
            remedy=(
                f"Convert it once with `uv run python {CONVERT_SCRIPT} --help` "
                f"(see docs/operations.md), or rebuild it with `{REBUILD_COMMAND}`."
                if version == SCHEMA_VERSION - 1
                else None
            ),
        )
    if version != SCHEMA_VERSION:
        raise IndexConsistencyError(f"{path} has unsupported schema {version!r}")
    if set(raw) != set(_MANIFEST_FIELDS) or any(
        type(raw[name]) is not kind for name, kind in _MANIFEST_FIELDS.items()
    ):
        raise IndexConsistencyError(f"{path} does not have the expected fields")
    manifest = IndexManifest(**raw)
    if manifest.generation < 1 or manifest.vector_count < 0:
        raise IndexConsistencyError(f"{path} has a negative generation or count")
    if not all(_SHA256_RE.match(d) for d in (manifest.tracks_sha256, manifest.vectors_sha256)):
        raise IndexConsistencyError(f"{path} holds an invalid SHA-256 digest")
    return manifest


def require_manifest(index_dir: Path) -> IndexManifest:
    """Return the live manifest, or say exactly why there is none.

    Raises:
        UnsupportedIndexError: If the directory holds an index from before
            the manifest format (``tracks.db`` or ``vectors.index``), or a
            manifest with an older schema.
        IndexConsistencyError: If the manifest is malformed, or generation
            files exist without one.
        FileNotFoundError: If the directory holds no index at all.
    """
    manifest = read_manifest(index_dir)
    if manifest is not None:
        return manifest
    if any((index_dir / name).exists() for name in ("tracks.db", "vectors.index")):
        raise UnsupportedIndexError(index_dir, "it has no index-manifest.json")
    if index_dir.is_dir() and any(GENERATION_FILE_RE.match(p.name) for p in index_dir.iterdir()):
        raise IndexConsistencyError(
            f"{index_dir} holds index generation files but no {MANIFEST_NAME}; copy the "
            f"manifest too, or rebuild with `{REBUILD_COMMAND}`"
        )
    raise FileNotFoundError(f"No index in {index_dir}; run `autodj index`")


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 digest of *path*."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


@contextmanager
def publication_lock(index_dir: Path) -> Iterator[None]:
    """Serialize index readers and publishers across threads and processes.

    Reentrant within one thread; other threads and processes wait.
    """
    index_dir = index_dir.resolve()
    index_dir.mkdir(parents=True, exist_ok=True)
    # One shared instance per path gives per-thread reentrancy.
    with FileLock(index_dir / ".index-publication.lock", is_singleton=True):
        yield


def publish_generation(
    index_dir: Path,
    *,
    base_generation: int,
    vector_count: int,
    write_files: Callable[[Path, Path], None],
) -> IndexManifest:
    """Write a new generation and make it live.

    *write_files* writes the tracks database and the vectors file to the
    two temporary paths it is given.  They are then renamed to the next
    generation's names and the manifest is replaced last.  Generations
    other than the new one and the one it replaces are deleted.

    Args:
        index_dir: The index directory.
        base_generation: The generation the new one was built from (0 for
            none).  Publication is refused when another command published
            since, so its work is not silently lost.
        vector_count: Rows in the tracks database and vectors in the index.
        write_files: ``write_files(tracks_path, vectors_path)``.

    Returns:
        The new manifest.

    Raises:
        IndexConsistencyError: If the live generation is no longer
            *base_generation*.
    """
    index_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    tracks_tmp = index_dir / f".tracks.{token}.tmp"
    vectors_tmp = index_dir / f".vectors.{token}.tmp"
    try:
        write_files(tracks_tmp, vectors_tmp)
        tracks_sha, vectors_sha = sha256_file(tracks_tmp), sha256_file(vectors_tmp)
        with publication_lock(index_dir):
            live = read_manifest(index_dir)
            live_generation = 0 if live is None else live.generation
            if live_generation != base_generation:
                raise IndexConsistencyError(
                    f"another command published generation {live_generation} of {index_dir} "
                    f"while this one worked from generation {base_generation}; run it again"
                )
            manifest = IndexManifest(
                schema_version=SCHEMA_VERSION,
                generation=live_generation + 1,
                vector_count=vector_count,
                published_at=datetime.now(UTC).isoformat(timespec="seconds"),
                tracks_sha256=tracks_sha,
                vectors_sha256=vectors_sha,
            )
            os.replace(tracks_tmp, index_dir / manifest.tracks_file)
            os.replace(vectors_tmp, index_dir / manifest.vectors_file)
            fsync_directory(index_dir)
            atomic_write(index_dir / MANIFEST_NAME, json.dumps(asdict(manifest), indent=2) + "\n")
            _remove_generations(index_dir, keep={manifest.generation, live_generation})
    finally:
        tracks_tmp.unlink(missing_ok=True)
        vectors_tmp.unlink(missing_ok=True)
    return manifest


def _remove_generations(index_dir: Path, *, keep: set[int]) -> None:
    """Delete generation files whose number is not in *keep*."""
    for path in index_dir.iterdir():
        match = GENERATION_FILE_RE.match(path.name)
        if match is None or int(match.group(1)) in keep:
            continue
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("Could not remove old index generation %s: %s", path.name, exc)
