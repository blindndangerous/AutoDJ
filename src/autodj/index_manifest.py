"""Deprecated published-index format, retained for import and migration.

Current serving and indexing use library.db. Existing generations remain readable
only for the explicit migration command. Normal startup and backups use library.db.

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


def has_legacy_index(directory: Path) -> bool:
    """Recognize old library artifacts without loading or validating a generation."""
    return any(
        (directory / name).exists() for name in (MANIFEST_NAME, "tracks.db", "vectors.index")
    ) or any(GENERATION_FILE_RE.fullmatch(path.name) for path in directory.glob("*.g*.*"))


class IndexConsistencyError(RuntimeError):
    """Raised when published index artifacts do not describe one snapshot."""


class LibraryMigrationRequired(IndexConsistencyError):
    """Supported legacy library data requires the explicit migration command."""


REBUILD_COMMAND = "autodj index --force"
CONVERT_SCRIPT = "scripts/convert_index_v3_to_v4.py"


class UnsupportedIndexError(IndexConsistencyError):
    """Raised when index data was written in a format this release no longer reads."""

    def __init__(
        self,
        index_dir: Path | str,
        reason: str,
        remedy: str | None = None,
        *,
        schema_version: int | None = None,
    ) -> None:
        """Build the one rebuild message shared by every old-format refusal.

        Args:
            index_dir: Index directory holding the old data, or a description
                of where it is, such as "this backup".
            reason: What marks the data as old, e.g. "it has no index-manifest.json".
            remedy: What to do about it.  Defaults to rebuilding with
                :data:`REBUILD_COMMAND`.
            schema_version: Known manifest version, for choosing a recovery command.
        """
        self.reason = reason
        self.schema_version = schema_version
        super().__init__(
            f"The index in {index_dir} was made by an older AutoDJ ({reason}). "
            + (remedy or f"Rebuild it with `{REBUILD_COMMAND}`.")
        )


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
            schema_version=version,
            remedy=(
                "Preview conversion with `autodj migrate-index`, then run "
                "`autodj migrate-index --apply` using the same --config and --name. "
                f"The legacy `{CONVERT_SCRIPT}` entry point also works. "
                "See docs/guides/operations/catalog-migration.md."
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
