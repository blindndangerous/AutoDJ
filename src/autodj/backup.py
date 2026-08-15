"""Versioned coherent backups and staged, rollback-capable restores."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import struct
import tempfile
import unicodedata
import zipfile
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, BinaryIO, cast
from urllib.parse import quote

from autodj import index_manifest as index_publication
from autodj.index_manifest import (
    MANIFEST_NAME,
    PUBLICATION_STATE_NAME,
    IndexConsistencyError,
    IndexManifest,
    copy_published_snapshot,
    publication_lock,
    read_manifest,
)
from autodj.index_manifest import SCHEMA_VERSION as INDEX_MANIFEST_SCHEMA_VERSION
from autodj.version import current_version

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig


SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 16 * 1024**2
MAX_CENTRAL_DIRECTORY_BYTES = 16 * 1024**2
_COPY_CHUNK_BYTES = 1024 * 1024
_SQLITE_MAIN_NAMES = ("tracks.db", "dj_meta.db")
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
# Publication artifact names permit exactly 20 decimal generation digits.
_MAX_INDEX_REVISION = 10**20 - 1
_SPACE_MARGIN_FLOOR = 64 * 1024**2
_SPACE_MARGIN_CAP = 1024**3
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WIN32_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_DERIVED_MAPPINGS = {
    "derived/vectors.index": "active/vectors.index",
    "derived/tracks.db": "active/tracks.db",
    "derived/index-manifest.json": "active/index-manifest.json",
    "derived/dj_meta.db": "active/dj_meta.db",
}
_UNIQUE_LABELS = frozenset({"web_state", "liners", "profiles", "dayparts", "history"})
_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_EOCD_FIXED_BYTES = 22
_MAX_ZIP_COMMENT_BYTES = 65_535
_CENTRAL_DIRECTORY_ENTRY_MIN_BYTES = 46


class BackupError(RuntimeError):
    """Raised when a backup cannot be created or safely restored."""


@dataclass(frozen=True)
class BackupItem:
    """One checksummed archive payload and its restore destination."""

    archive_path: str
    classification: str
    destination: str
    size: int
    sha256: str


@dataclass(frozen=True)
class RestoreResult:
    """Successful restore count plus non-fatal post-commit warnings."""

    restored: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ResolvedRestore:
    """Describe one validated archive item and its restore target."""

    item: BackupItem
    target: Path
    root: Path
    force: bool
    ancestors: tuple[_PathIdentity, ...]


@dataclass(frozen=True)
class _PathIdentity:
    """Pair a filesystem path with its captured object identity."""

    path: Path
    identity: tuple[int, int, int]


@dataclass
class _StagedRestore:
    """Track a staged restore payload and its rollback state."""

    item: BackupItem
    target: Path
    root: Path
    force: bool
    stage: Path
    ancestors: tuple[_PathIdentity, ...]
    created_parents: tuple[Path, ...] = ()
    stage_identity: tuple[int, int, int, int, int] | None = None
    previous: Path | None = None
    previous_placeholder_identity: tuple[int, int, int, int, int] | None = None
    previous_identity: tuple[int, int, int, int, int] | None = None
    previous_populated: bool = False
    installed: bool = False
    installed_identity: tuple[int, int, int, int, int] | None = None


def _is_reparse(metadata: os.stat_result) -> bool:
    """Return whether Windows metadata marks a reparse point."""

    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE)


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare file identity, type, size, and modification time."""

    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
    )


def _same_object_identity(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare identity/type while permitting content changes by a live writer."""

    return (left.st_dev, left.st_ino, left.st_mode) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
    )


def _object_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    """Return the device, inode, and mode that identify an object."""

    return metadata.st_dev, metadata.st_ino, metadata.st_mode


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return an object's identity together with size and modification time."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _observed_regular_identity(path: Path) -> tuple[int, int, int, int, int] | None:
    """Return a safe regular file's identity, or ``None`` when it is absent."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BackupError(f"unable to reconcile filesystem operation at {path}: {exc}") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise BackupError(f"filesystem operation produced an unsafe object at {path}")
    return _file_identity(metadata)


def _reserved_move_completed(
    reservation: Path,
    *,
    expected_identity: tuple[int, int, int, int, int] | None,
    placeholder_identity: tuple[int, int, int, int, int] | None,
) -> bool:
    """Determine whether a reserved move installed its expected source."""

    observed = _observed_regular_identity(reservation)
    if expected_identity is not None and observed == expected_identity:
        return True
    if placeholder_identity is not None and observed == placeholder_identity:
        return False
    raise BackupError(f"reserved move outcome could not be reconciled at {reservation}")


def _backup_cleanup_rollback_completed(
    destination: Path,
    recovery: Path,
    *,
    old_identity: tuple[int, int, int, int, int] | None,
    new_identity: tuple[int, int, int, int, int] | None,
) -> bool:
    """Determine whether backup cleanup restored the former destination."""

    destination_identity = _observed_regular_identity(destination)
    recovery_identity = _observed_regular_identity(recovery)
    if (
        old_identity is not None
        and destination_identity == old_identity
        and recovery_identity is None
    ):
        return True
    if (
        old_identity is not None
        and new_identity is not None
        and recovery_identity == old_identity
        and destination_identity == new_identity
    ):
        return False
    raise BackupError(
        "backup cleanup rollback outcome could not be reconciled; inspect destination "
        f"{destination} and recovery path {recovery}"
    )


def _path_ancestors(path: Path) -> tuple[Path, ...]:
    """Return *path* and its ancestors from filesystem root to leaf."""

    return tuple(reversed((path, *path.parents)))


def _capture_ancestor_identities(
    path: Path, *, leaf_may_be_file: bool = False
) -> tuple[_PathIdentity, ...]:
    """Capture safe existing ancestors and their object identities."""

    identities: list[_PathIdentity] = []
    for candidate in _path_ancestors(path):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise BackupError(f"unable to inspect path ancestor {candidate}: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise BackupError(f"path ancestor is a symbolic link or reparse point: {candidate}")
        if not stat.S_ISDIR(metadata.st_mode) and not (
            candidate == path and leaf_may_be_file and stat.S_ISREG(metadata.st_mode)
        ):
            raise BackupError(f"path ancestor is not a directory: {candidate}")
        identities.append(_PathIdentity(candidate, _object_identity(metadata)))
    return tuple(identities)


def _revalidate_ancestor_identities(identities: tuple[_PathIdentity, ...], *, target: Path) -> None:
    """Reject a target whose captured ancestors changed or became unsafe."""

    for captured in identities:
        try:
            metadata = captured.path.lstat()
        except OSError as exc:
            raise BackupError(
                f"restore target ancestor changed or disappeared for {target}: "
                f"{captured.path}: {exc}"
            ) from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or _is_reparse(metadata)
            or _object_identity(metadata) != captured.identity
        ):
            raise BackupError(
                f"restore target ancestor identity changed for {target}: {captured.path}"
            )


def _regular_source_stat(path: Path) -> os.stat_result:
    """Stat a backup source after rejecting links and nonregular files."""

    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BackupError(f"backup source is unreadable: {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        raise BackupError(f"refusing symbolic link in backup source: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise BackupError(f"backup source is not a regular file: {path}")
    return metadata


@contextmanager
def _open_regular_source(path: Path, root: Path) -> Iterator[BinaryIO]:
    """Open one source without accepting a link swap outside *root*."""

    root = root.resolve(strict=True)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BackupError(f"backup source is unreadable: {path}: {exc}") from exc
    if resolved != root and root not in resolved.parents:
        raise BackupError(f"backup source escapes its configured root: {path}")
    before = _regular_source_stat(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BackupError(f"backup source could not be opened safely: {path}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        after = _regular_source_stat(path)
        if not stat.S_ISREG(opened.st_mode) or _is_reparse(opened):
            raise BackupError(f"backup source is not a regular file: {path}")
        if not _same_file_identity(before, opened) or not _same_file_identity(opened, after):
            raise BackupError(f"backup source changed while it was opened: {path}")
        if path.resolve(strict=True) != resolved:
            raise BackupError(f"backup source changed while it was opened: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            yield handle
            opened_after = os.fstat(handle.fileno())
            path_after = _regular_source_stat(path)
            if (
                not _same_file_identity(before, opened_after)
                or not _same_file_identity(opened_after, path_after)
                or path.resolve(strict=True) != resolved
            ):
                raise BackupError(f"backup source changed while it was read: {path}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _unique_roots(cfg: AutoDJConfig) -> list[tuple[Path, str]]:
    """Return configured backup roots with their archive labels."""

    active = cfg.index.active_dir
    roots = [
        (active / "web_state.json", "web_state"),
        (
            Path(cfg.playback.liners_folder) if cfg.playback.liners_folder else active / "liners",
            "liners",
        ),
        (active.parent / "profiles", "profiles"),
    ]
    if cfg.playback.dayparts_dir:
        roots.append((Path(cfg.playback.dayparts_dir), "dayparts"))
    if cfg.playback.history_file:
        roots.append((cfg.playback.history_file, "history"))
    return roots


def _canonical_unique_roots(cfg: AutoDJConfig) -> list[tuple[Path, str]]:
    """Validate configured backup roots and reject overlapping locations."""

    roots: list[tuple[Path, str]] = []
    canonical_roots: list[tuple[Path, str]] = []
    for source, label in _unique_roots(cfg):
        expanded = source.expanduser()
        if not expanded.is_absolute():
            expanded = Path.cwd() / expanded
        ancestors = _capture_ancestor_identities(expanded, leaf_may_be_file=True)
        try:
            canonical = expanded.resolve(strict=False)
        except OSError as exc:
            raise BackupError(f"backup source root is invalid: {expanded}: {exc}") from exc
        _revalidate_ancestor_identities(ancestors, target=expanded)
        for prior, prior_label in canonical_roots:
            if canonical == prior or canonical in prior.parents or prior in canonical.parents:
                raise BackupError(
                    "overlapping unique backup roots would map the same source more than once: "
                    f"{prior_label}={prior}, {label}={canonical}"
                )
        canonical_roots.append((canonical, label))
        roots.append((expanded, label))
    return roots


def _reject_destination_in_unique_roots(destination: Path, roots: list[tuple[Path, str]]) -> None:
    """Reject an archive destination nested within a source root."""

    for lexical_root, label in roots:
        root = lexical_root.resolve(strict=False)
        if destination == root or root in destination.parents:
            raise BackupError(
                f"backup destination is inside configured unique backup source {label}: {root}"
            )


def _walk_regular_files(root: Path) -> list[Path]:
    """Recursively list regular files below a link-free source root."""

    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise BackupError(f"backup source is unreadable: {root}: {exc}") from exc
    if stat.S_ISLNK(root_stat.st_mode) or _is_reparse(root_stat):
        raise BackupError(f"refusing symbolic link in backup source: {root}")
    if stat.S_ISREG(root_stat.st_mode):
        return [root]
    if not stat.S_ISDIR(root_stat.st_mode):
        raise BackupError(f"backup source is not a regular file or directory: {root}")

    files: list[Path] = []

    def visit(directory: Path) -> None:
        """Visit one validated directory while preserving ancestor identities."""

        ancestors = _capture_ancestor_identities(directory)
        _revalidate_ancestor_identities(ancestors, target=directory)
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise BackupError(f"backup source directory is unreadable: {directory}: {exc}") from exc
        _revalidate_ancestor_identities(ancestors, target=directory)
        for entry in entries:
            path = Path(entry.path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise BackupError(f"backup source is unreadable: {path}: {exc}") from exc
            if entry.is_symlink() or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                raise BackupError(f"refusing symbolic link in backup source: {path}")
            if stat.S_ISDIR(metadata.st_mode):
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                files.append(path)
            else:
                raise BackupError(f"backup source is not a regular file or directory: {path}")
        _revalidate_ancestor_identities(ancestors, target=directory)

    visit(root)
    return files


def _add_file(
    zf: zipfile.ZipFile,
    source: Path,
    source_root: Path,
    archive_path: str,
    classification: str,
    destination: str,
    items: list[BackupItem],
) -> None:
    """Copy one verified source file into the archive and record its digest."""

    digest = hashlib.sha256()
    size = 0
    with _open_regular_source(source, source_root) as src, zf.open(archive_path, "w") as dst:
        while chunk := src.read(_COPY_CHUNK_BYTES):
            dst.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    items.append(BackupItem(archive_path, classification, destination, size, digest.hexdigest()))


def _add_path(
    zf: zipfile.ZipFile,
    source: Path,
    prefix: str,
    items: list[BackupItem],
) -> None:
    """Add every regular file under one configured source path."""

    files = _walk_regular_files(source)
    if not files:
        return
    source_is_file = stat.S_ISREG(source.lstat().st_mode)
    source_root = source.parent if source_is_file else source
    for file in files:
        relative = file.name if source_is_file else file.relative_to(source).as_posix()
        _add_file(
            zf,
            file,
            source_root,
            f"unique/{prefix}/{relative}",
            "unique",
            f"{prefix}/{relative}",
            items,
        )


def _sqlite_snapshot(source: Path, target: Path, root: Path) -> None:
    """Create a read-only SQLite snapshot without following source swaps."""

    before = _regular_source_stat(source)
    resolved_root = root.resolve(strict=True)
    resolved_source = source.resolve(strict=True)
    if resolved_source.parent != resolved_root:
        raise BackupError(f"SQLite backup source escapes the active index: {source}")
    source_posix = resolved_source.as_posix()
    encoded_source = quote(source_posix, safe="/")
    source_uri = (
        f"file://{encoded_source}?mode=ro"
        if source_posix.startswith("//")
        else f"{resolved_source.as_uri()}?mode=ro"
    )
    try:
        with (
            closing(sqlite3.connect(source_uri, uri=True)) as src,
            closing(sqlite3.connect(target)) as dst,
        ):
            src.backup(dst)
    except sqlite3.Error as exc:
        raise BackupError(
            f"SQLite backup source could not be opened read-only: {source}: {exc}"
        ) from exc
    after = _regular_source_stat(source)
    if not _same_object_identity(before, after) or source.resolve(strict=True) != resolved_source:
        raise BackupError(f"SQLite backup source changed identity during snapshot: {source}")


def _fsync_directory(path: Path) -> None:
    """Flush a directory entry update on platforms that support it."""

    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_snapshot(path: Path) -> None:
    """Remove a temporary snapshot directory when it exists."""

    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _capture_stopped_state(active: Path) -> dict[str, tuple[int, int, int, int, int]]:
    """Capture identities of index SQLite files and sidecars."""

    names = [Path(name) for name in _SQLITE_MAIN_NAMES]
    names.extend(
        Path(f"{name}{suffix}")
        for name in _SQLITE_MAIN_NAMES
        for suffix in _SQLITE_SIDECAR_SUFFIXES
    )
    state: dict[str, tuple[int, int, int, int, int]] = {}
    for relative in names:
        path = active / relative
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise BackupError(f"unable to inspect stopped SQLite state {path}: {exc}") from exc
        state[relative.as_posix()] = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
        )
    return state


def _begin_stopped_snapshot(active: Path) -> dict[str, tuple[int, int, int, int, int]]:
    """Capture stopped-mode SQLite state after rejecting active sidecars."""

    state = _capture_stopped_state(active)
    sidecars = sorted(name for name in state if name.endswith(_SQLITE_SIDECAR_SUFFIXES))
    if sidecars:
        raise BackupError(
            f"SQLite state {', '.join(sidecars)} exists; stop the service or use --online"
        )
    return state


def _verify_stopped_state(
    expected: dict[str, tuple[int, int, int, int, int]],
    active: Path,
    destination: Path,
    *,
    phase: str,
) -> None:
    """Ensure stopped-mode SQLite files match their captured state."""

    current = _capture_stopped_state(active)
    if current == expected:
        return
    changed = sorted(
        name for name in set(expected) | set(current) if expected.get(name) != current.get(name)
    )
    _remove_snapshot(destination)
    raise BackupError(
        f"SQLite state changed during stopped-mode snapshot ({phase}: {changed}); "
        "stop the service or use --online"
    )


def _snapshot_derived(cfg: AutoDJConfig, destination: Path, *, online: bool) -> None:
    """Copy the published index and DJ metadata into a temporary snapshot."""

    active = cfg.index.active_dir
    stopped_state = None if online else _begin_stopped_snapshot(active)
    try:
        manifest = read_manifest(active)
    except IndexConsistencyError as exc:
        raise BackupError(f"published index manifest is invalid: {exc}") from exc
    has_index = any((active / name).exists() for name in ("vectors.index", "tracks.db"))
    if manifest is None and has_index:
        raise BackupError(
            "index has no published manifest; rebuild it before backup so one coherent "
            "generation can be selected"
        )
    if manifest is not None:
        attempts = 3 if online else 1
        expected_generation = manifest.generation
        for attempt in range(attempts):
            _remove_snapshot(destination)
            if stopped_state is not None:
                _verify_stopped_state(
                    stopped_state, active, destination, phase="before published index copy"
                )
            try:
                copy_published_snapshot(
                    active,
                    destination,
                    expected_generation=expected_generation,
                )
                if stopped_state is not None:
                    _verify_stopped_state(
                        stopped_state, active, destination, phase="published index copy"
                    )
                break
            except IndexConsistencyError as exc:
                if attempt + 1 == attempts:
                    if online:
                        raise BackupError(
                            f"published index changed during {attempts} snapshot attempts; "
                            "retry later"
                        ) from exc
                    raise BackupError(f"published index snapshot failed: {exc}") from exc
                try:
                    latest = read_manifest(active)
                except IndexConsistencyError as read_exc:
                    raise BackupError(
                        f"published index manifest is invalid: {read_exc}"
                    ) from read_exc
                if latest is None:
                    raise BackupError("published index disappeared during backup") from exc
                expected_generation = latest.generation

    metadata = active / "dj_meta.db"
    if os.path.lexists(metadata):
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / metadata.name
        if stopped_state is not None:
            _verify_stopped_state(
                stopped_state, active, destination, phase="before DJ metadata copy"
            )
        if online:
            _sqlite_snapshot(metadata, target, active)
        else:
            _regular_source_stat(metadata)
            shutil.copy2(metadata, target)
        if stopped_state is not None:
            _verify_stopped_state(stopped_state, active, destination, phase="DJ metadata copy")


def _write_backup_archive(cfg: AutoDJConfig, archive: Path, *, online: bool) -> None:
    """Write derived and unique backup data plus its manifest to *archive*."""

    items: list[BackupItem] = []
    with tempfile.TemporaryDirectory(prefix="autodj-backup-snapshot-") as temp_name:
        snapshot = Path(temp_name) / "published"
        _snapshot_derived(cfg, snapshot, online=online)
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name in ("vectors.index", "tracks.db", "index-manifest.json", "dj_meta.db"):
                source = snapshot / name
                if source.exists():
                    _add_file(
                        zf,
                        source,
                        snapshot,
                        f"derived/{name}",
                        "derived",
                        f"active/{name}",
                        items,
                    )
            for source, label in _canonical_unique_roots(cfg):
                _add_path(zf, source, label, items)
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "autodj_version": current_version(),
                "created_at": datetime.now(UTC).isoformat(),
                "index_name": cfg.index.name,
                "mode": "online" if online else "stopped",
                "items": [asdict(item) for item in items],
            }
            encoded = (json.dumps(manifest, indent=2) + "\n").encode()
            if len(encoded) > MAX_MANIFEST_BYTES:
                raise BackupError("backup manifest exceeds 16 MiB metadata limit")
            zf.writestr("manifest.json", encoded)


def _absolute_destination(path: Path) -> Path:
    """Expand a destination path and anchor relative paths to the current directory."""

    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    try:
        parent = expanded.parent.resolve(strict=False)
    except OSError as exc:
        raise BackupError(
            f"backup destination parent is invalid: {expanded.parent}: {exc}"
        ) from exc
    return parent / expanded.name


def _validate_existing_regular(path: Path, *, description: str) -> bool:
    """Return whether *path* exists as a safe regular file."""

    if not os.path.lexists(path):
        return False
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BackupError(f"unable to inspect {description} {path}: {exc}") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise BackupError(f"{description} is not a regular file: {path}")
    return True


def _unlink_quietly(path: Path) -> None:
    """Remove a path while suppressing cleanup errors."""

    with suppress(OSError):
        path.unlink(missing_ok=True)


def _new_backup_recovery(destination: Path) -> Path:
    """Reserve a same-directory path for the replaced backup archive."""

    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.backup-old-",
        dir=destination.parent,
    )
    recovery = Path(name)
    try:
        os.close(descriptor)
    except BaseException:
        _unlink_quietly(recovery)
        raise
    return recovery


def _new_failed_backup_path(destination: Path) -> Path:
    """Reserve a same-directory path for a failed new backup archive."""

    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.backup-failed-",
        dir=destination.parent,
    )
    failed = Path(name)
    try:
        os.close(descriptor)
    except BaseException:
        _unlink_quietly(failed)
        raise
    return failed


def _cleanup_empty_reservation(path: Path, *, purpose: str) -> str | None:
    """Remove an unused reservation and return any cleanup error."""

    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return f"empty {purpose} reservation retained at {path}: {exc}"
    return None


def _recover_backup_destination(
    destination: Path,
    recovery: Path | None,
    *,
    destination_installed: bool,
) -> str | None:
    """Restore the pre-publication state and return an actionable error, if any."""

    if recovery is not None and os.path.lexists(recovery):
        try:
            os.replace(recovery, destination)
        except OSError as exc:
            return f"previous archive recovery failed; recovery copy retained at {recovery}: {exc}"
    elif destination_installed:
        try:
            destination.unlink(missing_ok=True)
        except OSError as exc:
            retained: Path | None = None
            try:
                retained = _new_failed_backup_path(destination)
                os.replace(destination, retained)
            except OSError as move_exc:
                cleanup_error = (
                    _cleanup_empty_reservation(retained, purpose="quarantine")
                    if retained is not None
                    else None
                )
                message = (
                    f"new archive remains at {destination} after removal failed: {exc}; "
                    f"moving it aside also failed: {move_exc}"
                )
                return f"{message}; {cleanup_error}" if cleanup_error else message
            with suppress(OSError):
                _fsync_directory(destination.parent)
            return f"new archive retained at {retained} after destination removal failed: {exc}"
    try:
        _fsync_directory(destination.parent)
    except OSError as exc:
        return f"previous destination state was restored, but directory sync failed: {exc}"
    return None


@dataclass
class _BackupPublication:
    """Track backup publication state needed for recovery."""

    destination: Path
    unpublished: Path
    recovery: Path | None = None
    recovery_expected_identity: tuple[int, int, int, int, int] | None = None
    recovery_placeholder_identity: tuple[int, int, int, int, int] | None = None
    recovery_populated: bool = False
    destination_installed: bool = False
    unpublished_identity: tuple[int, int, int, int, int] | None = None
    retain_recovery: bool = False
    recovery_failure_finalized: bool = False
    cleanup_rollback_intended: bool = False


def _publish_backup_destination(state: _BackupPublication, *, force: bool) -> None:
    """Install an unpublished archive and record observable outcomes."""

    if force:
        existing_now = _validate_existing_regular(
            state.destination,
            description="backup destination",
        )
        if existing_now:
            state.recovery_expected_identity = _observed_regular_identity(state.destination)
            state.recovery = _new_backup_recovery(state.destination)
            state.recovery_placeholder_identity = _observed_regular_identity(state.recovery)
            try:
                os.replace(state.destination, state.recovery)
            except BaseException:
                state.recovery_populated = _reserved_move_completed(
                    state.recovery,
                    expected_identity=state.recovery_expected_identity,
                    placeholder_identity=state.recovery_placeholder_identity,
                )
                raise
            state.recovery_populated = _reserved_move_completed(
                state.recovery,
                expected_identity=state.recovery_expected_identity,
                placeholder_identity=state.recovery_placeholder_identity,
            )
            if not state.recovery_populated:
                raise BackupError("old backup destination move could not be reconciled")
            _fsync_directory(state.destination.parent)
        try:
            os.replace(state.unpublished, state.destination)
        except BaseException:
            state.destination_installed = (
                _observed_regular_identity(state.destination) == state.unpublished_identity
            )
            raise
        state.destination_installed = (
            _observed_regular_identity(state.destination) == state.unpublished_identity
        )
        if not state.destination_installed:
            raise BackupError("backup destination publication could not be reconciled")
        return

    try:
        os.link(state.unpublished, state.destination)
        state.destination_installed = (
            _observed_regular_identity(state.destination) == state.unpublished_identity
        )
    except FileExistsError as exc:
        raise BackupError(
            f"{state.destination} appeared during backup; pass --force to replace it"
        ) from exc
    except OSError as exc:
        raise BackupError(
            "filesystem cannot atomically publish a no-clobber backup; "
            "choose a local destination or pass --force"
        ) from exc
    except BaseException:
        state.destination_installed = (
            _observed_regular_identity(state.destination) == state.unpublished_identity
        )
        raise
    if not state.destination_installed:
        raise BackupError("backup destination publication could not be reconciled")


def _finish_backup_publication(state: _BackupPublication) -> None:
    """Sync publication and remove recovery state, rolling back on failure."""

    _unlink_quietly(state.unpublished)
    try:
        _fsync_directory(state.destination.parent)
    except OSError as exc:
        raise BackupError(
            f"backup destination directory sync failed after publication: {exc}"
        ) from exc
    if state.recovery is None:
        return
    try:
        state.recovery.unlink(missing_ok=True)
    except OSError as cleanup_exc:
        state.cleanup_rollback_intended = True
        try:
            os.replace(state.recovery, state.destination)
            cleanup_rollback_completed = _backup_cleanup_rollback_completed(
                state.destination,
                state.recovery,
                old_identity=state.recovery_expected_identity,
                new_identity=state.unpublished_identity,
            )
        except BaseException as rollback_exc:
            try:
                cleanup_rollback_completed = _backup_cleanup_rollback_completed(
                    state.destination,
                    state.recovery,
                    old_identity=state.recovery_expected_identity,
                    new_identity=state.unpublished_identity,
                )
            except BackupError as reconcile_exc:
                state.retain_recovery = True
                state.recovery_failure_finalized = True
                raise reconcile_exc from rollback_exc
            if cleanup_rollback_completed:
                state.recovery = None
                state.recovery_populated = False
                state.destination_installed = False
                state.cleanup_rollback_intended = False
                raise
            state.retain_recovery = True
            state.recovery_failure_finalized = True
            raise BackupError(
                f"backup recovery cleanup failed: {cleanup_exc}; new archive remains at "
                f"{state.destination}; recovery copy retained at {state.recovery}: {rollback_exc}"
            ) from cleanup_exc
        if not cleanup_rollback_completed:
            state.retain_recovery = True
            state.recovery_failure_finalized = True
            raise BackupError(
                f"backup recovery cleanup failed: {cleanup_exc}; new archive remains at "
                f"{state.destination}; recovery copy retained at {state.recovery}"
            ) from cleanup_exc
        state.recovery = None
        state.recovery_populated = False
        state.destination_installed = False
        state.cleanup_rollback_intended = False
        try:
            _fsync_directory(state.destination.parent)
        except OSError as sync_exc:
            raise BackupError(
                "backup recovery cleanup failed and the old destination was restored, "
                f"but directory sync failed: {sync_exc}"
            ) from cleanup_exc
        raise BackupError(
            f"backup recovery cleanup failed; old destination restored: {cleanup_exc}"
        ) from cleanup_exc
    with suppress(OSError):
        _fsync_directory(state.destination.parent)


def _raise_backup_failure(state: _BackupPublication, exc: BaseException) -> None:
    """Reconcile interrupted publication and raise its final error."""

    recovery_error = None
    reservation_error = None
    if state.cleanup_rollback_intended and state.recovery is not None:
        try:
            cleanup_rollback_completed = _backup_cleanup_rollback_completed(
                state.destination,
                state.recovery,
                old_identity=state.recovery_expected_identity,
                new_identity=state.unpublished_identity,
            )
        except BackupError as reconcile_exc:
            recovery_error = str(reconcile_exc)
            state.retain_recovery = True
            state.recovery_failure_finalized = True
        else:
            if cleanup_rollback_completed:
                state.recovery = None
                state.recovery_populated = False
                state.destination_installed = False
            else:
                recovery_error = (
                    "backup cleanup rollback was interrupted; new archive remains at "
                    f"{state.destination}; recovery copy retained at {state.recovery}"
                )
                state.retain_recovery = True
                state.recovery_failure_finalized = True
        state.cleanup_rollback_intended = False
    if (
        state.recovery is not None
        and state.recovery_expected_identity is not None
        and state.recovery_placeholder_identity is not None
        and not state.recovery_populated
    ):
        observed_recovery = _observed_regular_identity(state.recovery)
        if observed_recovery == state.recovery_expected_identity:
            state.recovery_populated = True
        elif observed_recovery != state.recovery_placeholder_identity:
            recovery_error = (
                "old backup move outcome could not be reconciled; inspect destination "
                f"{state.destination} and recovery path {state.recovery}"
            )
            state.retain_recovery = True
    if state.unpublished_identity is not None and not state.destination_installed:
        state.destination_installed = (
            _observed_regular_identity(state.destination) == state.unpublished_identity
        )
    if (
        state.recovery is not None
        and not state.recovery_populated
        and not state.recovery_failure_finalized
        and recovery_error is None
    ):
        reservation_error = _cleanup_empty_reservation(state.recovery, purpose="recovery")
        if reservation_error is None:
            state.recovery = None
        else:
            state.retain_recovery = True
    if (
        state.recovery_populated or state.destination_installed
    ) and not state.recovery_failure_finalized:
        recovered_error = _recover_backup_destination(
            state.destination,
            state.recovery if state.recovery_populated else None,
            destination_installed=state.destination_installed,
        )
        if recovered_error is not None:
            recovery_error = recovered_error
        state.retain_recovery = state.retain_recovery or (
            state.recovery_populated
            and state.recovery is not None
            and os.path.lexists(state.recovery)
        )
    _unlink_quietly(state.unpublished)
    cleanup_errors = [error for error in (reservation_error, recovery_error) if error is not None]
    if cleanup_errors:
        raise BackupError(f"backup creation failed: {exc}; " + "; ".join(cleanup_errors)) from exc
    if isinstance(exc, BackupError) or not isinstance(exc, Exception):
        raise exc
    raise BackupError(f"backup creation failed: {exc}") from exc


def create_backup(
    cfg: AutoDJConfig,
    destination: Path,
    *,
    online: bool,
    force: bool = False,
) -> Path:
    """Create and durably publish one backup archive."""

    destination = _absolute_destination(destination)
    unique_roots = _canonical_unique_roots(cfg)
    _reject_destination_in_unique_roots(destination, unique_roots)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"backup destination directory could not be created: {exc}") from exc
    exists = _validate_existing_regular(destination, description="backup destination")
    if exists and not force:
        raise BackupError(f"{destination} exists; pass --force to replace it")
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.backup-",
            suffix=".tmp",
            dir=destination.parent,
        )
    except OSError as exc:
        raise BackupError(f"backup temporary file could not be created: {exc}") from exc
    os.close(descriptor)
    unpublished = Path(temp_name)
    state = _BackupPublication(destination=destination, unpublished=unpublished)
    try:
        _write_backup_archive(cfg, unpublished, online=online)
        with unpublished.open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        state.unpublished_identity = _observed_regular_identity(unpublished)
        _publish_backup_destination(state, force=force)
        _finish_backup_publication(state)
    except BaseException as exc:
        _raise_backup_failure(state, exc)
    finally:
        _unlink_quietly(unpublished)
        if state.recovery is not None and not state.retain_recovery:
            _unlink_quietly(state.recovery)
    return destination


def _safe_relative(value: str, *, field: str) -> PurePosixPath:
    """Parse a portable, nonescaping relative archive path."""

    if not isinstance(value, str):
        raise BackupError(f"unsafe restore path in {field}: {value!r}")
    pieces = value.split("/")
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or path.is_absolute()
        or any(piece in ("", ".", "..") for piece in pieces)
    ):
        raise BackupError(f"unsafe restore path in {field}: {value!r}")
    for piece in pieces:
        device_stem = piece.split(".", 1)[0].upper()
        if (
            piece.endswith((".", " "))
            or ":" in piece
            or any(ord(character) < 32 for character in piece)
            or device_stem in _WIN32_RESERVED_NAMES
        ):
            raise BackupError(f"unsafe restore path in {field}: {value!r}")
    return path


def _normalized_path_key(path: PurePosixPath) -> tuple[str, ...]:
    """Return a Unicode-normalized, case-insensitive path comparison key."""

    return tuple(unicodedata.normalize("NFC", piece).casefold() for piece in path.parts)


def _validate_member_info(info: zipfile.ZipInfo) -> None:
    """Reject encrypted, nonregular, or unsafe ZIP members."""

    if info.flag_bits & 0x1:
        raise BackupError(f"encrypted archive member is unsupported: {info.filename}")
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if info.is_dir() or (info.create_system == 3 and file_type not in (0, stat.S_IFREG)):
        raise BackupError(f"archive member is not a regular file: {info.filename}")
    _safe_relative(info.filename, field="archive member")


def _zip64_directory_metadata(handle: BinaryIO, *, eocd_offset: int) -> tuple[int, int, int]:
    """Read ZIP64 central-directory bounds from a validated locator."""

    locator_offset = eocd_offset - 20
    if locator_offset < 0:
        raise BackupError("backup ZIP64 central-directory locator is missing")
    handle.seek(locator_offset)
    locator = handle.read(20)
    if len(locator) != 20:
        raise BackupError("backup ZIP64 central-directory locator is truncated")
    signature, disk, zip64_offset, disks = struct.unpack("<4sLQL", locator)
    if signature != _ZIP64_LOCATOR_SIGNATURE or disk != 0 or disks != 1:
        raise BackupError("multi-disk backup archives are unsupported")
    handle.seek(zip64_offset)
    record = handle.read(56)
    if len(record) != 56:
        raise BackupError("backup ZIP64 central-directory record is truncated")
    (
        signature,
        record_size,
        _made_by,
        _needed,
        disk_number,
        directory_disk,
        entries_on_disk,
        entries,
        directory_size,
        directory_offset,
    ) = struct.unpack("<4sQ2H2L4Q", record)
    if signature != _ZIP64_EOCD_SIGNATURE or record_size < 44:
        raise BackupError("backup ZIP64 central-directory record is invalid")
    if disk_number != 0 or directory_disk != 0 or entries_on_disk != entries:
        raise BackupError("multi-disk backup archives are unsupported")
    return entries, directory_size, directory_offset


def _preflight_zip_metadata(handle: BinaryIO) -> None:
    """Validate archive central-directory bounds before opening the ZIP."""

    try:
        handle.seek(0, os.SEEK_END)
        archive_size = handle.tell()
        tail_size = min(archive_size, _EOCD_FIXED_BYTES + _MAX_ZIP_COMMENT_BYTES)
        handle.seek(archive_size - tail_size)
        tail = handle.read(tail_size)
        search_end = len(tail)
        eocd_index = -1
        while search_end >= 0:
            candidate = tail.rfind(_EOCD_SIGNATURE, 0, search_end)
            if candidate < 0:
                break
            if candidate + _EOCD_FIXED_BYTES <= len(tail):
                comment_size = struct.unpack_from("<H", tail, candidate + 20)[0]
                if candidate + _EOCD_FIXED_BYTES + comment_size == len(tail):
                    eocd_index = candidate
                    break
            search_end = candidate
        if eocd_index < 0:
            raise BackupError("backup end-of-central-directory record is missing")
        eocd_offset = archive_size - tail_size + eocd_index
        (
            _signature,
            disk_number,
            directory_disk,
            entries_on_disk,
            entries,
            directory_size,
            directory_offset,
            _comment_size,
        ) = struct.unpack_from("<4s4H2LH", tail, eocd_index)
        if disk_number != 0 or directory_disk != 0 or entries_on_disk != entries:
            raise BackupError("multi-disk backup archives are unsupported")
        if entries == 0xFFFF or directory_size == 0xFFFFFFFF or directory_offset == 0xFFFFFFFF:
            entries, directory_size, directory_offset = _zip64_directory_metadata(
                handle, eocd_offset=eocd_offset
            )
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(f"backup archive is unreadable: {exc}") from exc
    if directory_size > MAX_CENTRAL_DIRECTORY_BYTES:
        raise BackupError(
            f"backup central-directory metadata exceeds 16 MiB limit: {directory_size} bytes"
        )
    if entries > directory_size // _CENTRAL_DIRECTORY_ENTRY_MIN_BYTES:
        raise BackupError("backup central-directory member count is inconsistent with its size")
    if directory_offset + directory_size > eocd_offset:
        raise BackupError("backup central-directory bounds are invalid")


def _open_handle_identity(handle: BinaryIO) -> tuple[int, int, int, int, int] | None:
    """Return an open file handle's identity when its descriptor is available."""

    try:
        return _file_identity(os.fstat(handle.fileno()))
    except (AttributeError, OSError):
        return None


def _member_map(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """Validate ZIP members and index them by their archive paths."""

    members: dict[str, zipfile.ZipInfo] = {}
    normalized: set[tuple[str, ...]] = set()
    for info in zf.infolist():
        _validate_member_info(info)
        if info.filename in members:
            raise BackupError(f"backup contains a duplicate archive member: {info.filename}")
        key = _normalized_path_key(PurePosixPath(info.filename))
        if key in normalized:
            raise BackupError(
                f"backup contains a normalized archive member collision: {info.filename}"
            )
        members[info.filename] = info
        normalized.add(key)
    return members


def _validate_item_mapping(
    item: BackupItem, archive_path: PurePosixPath, destination: PurePosixPath
) -> None:
    """Require a manifest item to use its permitted archive-to-target mapping."""

    if item.classification == "derived":
        if _DERIVED_MAPPINGS.get(item.archive_path) != item.destination:
            raise BackupError(
                f"backup item does not use a canonical mapping: {item.archive_path} -> "
                f"{item.destination}"
            )
        return
    if (
        len(archive_path.parts) < 3
        or len(destination.parts) < 2
        or archive_path.parts[1] not in _UNIQUE_LABELS
        or destination.parts[0] != archive_path.parts[1]
        or destination.parts[1:] != archive_path.parts[2:]
    ):
        raise BackupError(
            f"backup item does not use a canonical mapping: {item.archive_path} -> "
            f"{item.destination}"
        )


def _parse_items(members: dict[str, zipfile.ZipInfo], raw_manifest: Any) -> list[BackupItem]:
    """Validate manifest items against the archive and return typed records."""

    if not isinstance(raw_manifest, dict):
        raise BackupError("backup manifest must be an object")
    raw_items = raw_manifest.get("items")
    if not isinstance(raw_items, list):
        raise BackupError("backup manifest items must be a list")
    items: list[BackupItem] = []
    archive_paths: set[str] = set()
    destinations: set[str] = set()
    normalized_destinations: set[tuple[str, ...]] = set()
    required_fields = {"archive_path", "classification", "destination", "size", "sha256"}
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise BackupError("backup manifest items must be objects")
        if set(raw) != required_fields:
            raise BackupError("backup manifest item is missing or has invalid fields")
        item = BackupItem(**raw)
        if not all(
            isinstance(value, str)
            for value in (item.archive_path, item.classification, item.destination, item.sha256)
        ):
            raise BackupError("backup manifest item fields have invalid types")
        archive_path = _safe_relative(item.archive_path, field="archive_path")
        destination = _safe_relative(item.destination, field="destination")
        if len(destination.parts) < 2:
            raise BackupError(f"unsafe restore path in destination: {item.destination!r}")
        if item.classification not in {"derived", "unique"}:
            raise BackupError(f"unsupported backup classification {item.classification!r}")
        if archive_path.parts[0] != item.classification:
            raise BackupError("archive member classification does not match its path")
        if item.archive_path in archive_paths:
            raise BackupError(f"duplicate archive member {item.archive_path!r}")
        if item.destination in destinations:
            raise BackupError(f"duplicate restore destination {item.destination!r}")
        _validate_item_mapping(item, archive_path, destination)
        if isinstance(item.size, bool) or not isinstance(item.size, int) or item.size < 0:
            raise BackupError("backup manifest item size must be a non-negative integer")
        if re.fullmatch(r"[0-9a-f]{64}", item.sha256) is None:
            raise BackupError("backup manifest item checksum is invalid")
        normalized_destination = _normalized_path_key(destination)
        if normalized_destination in normalized_destinations:
            raise BackupError(f"duplicate normalized restore destination {item.destination!r}")
        archive_paths.add(item.archive_path)
        destinations.add(item.destination)
        normalized_destinations.add(normalized_destination)
        info = members.get(item.archive_path)
        if info is not None and info.file_size != item.size:
            raise BackupError(
                f"central-directory size for {item.archive_path} is {info.file_size}; "
                f"manifest declares {item.size}"
            )
        items.append(item)
    member_names = set(members)
    missing = archive_paths - member_names
    unexpected = member_names - archive_paths - {"manifest.json"}
    if missing:
        raise BackupError(f"backup member is missing: {sorted(missing)[0]}")
    if unexpected:
        raise BackupError(f"backup contains unmanifested member: {sorted(unexpected)[0]}")
    return items


def _destination_root(cfg: AutoDJConfig, label: str) -> Path:
    """Return the configured restore root associated with a manifest label."""

    active = cfg.index.active_dir
    roots = {
        "active": active,
        "web_state": active,
        "liners": (
            Path(cfg.playback.liners_folder) if cfg.playback.liners_folder else active / "liners"
        ),
        "profiles": active.parent / "profiles",
        "dayparts": (
            Path(cfg.playback.dayparts_dir)
            if cfg.playback.dayparts_dir
            else active.parent / "dayparts"
        ),
        "history": (
            cfg.playback.history_file.parent if cfg.playback.history_file else active.parent
        ),
    }
    try:
        return roots[label]
    except KeyError as exc:
        raise BackupError(f"unsupported restore destination {label!r}") from exc


def _destination(
    cfg: AutoDJConfig, label: str, relative: PurePosixPath
) -> tuple[Path, Path, tuple[_PathIdentity, ...]]:
    """Resolve a restore target within its validated destination root."""

    lexical_root = _destination_root(cfg, label)
    if not lexical_root.is_absolute():
        lexical_root = Path.cwd() / lexical_root
    root_ancestors = _capture_ancestor_identities(lexical_root)
    candidate = lexical_root.joinpath(*relative.parts)
    ancestors = _capture_ancestor_identities(candidate.parent)
    try:
        root = lexical_root.resolve(strict=False)
        if os.path.lexists(candidate):
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                raise BackupError(f"restore target is a symbolic link: {candidate}")
        parent = candidate.parent.resolve(strict=False)
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(f"unsafe restore path {candidate}: {exc}") from exc
    target = parent / candidate.name
    if parent != root and root not in parent.parents:
        raise BackupError(f"unsafe restore path {target}")
    _revalidate_ancestor_identities(root_ancestors, target=target)
    _revalidate_ancestor_identities(ancestors, target=target)
    return target, root, ancestors


def _compatibility_line(version: str) -> tuple[int, int]:
    """Parse the major and minor components of an AutoDJ version."""

    match = re.match(r"^(\d+)\.(\d+)(?:\.|$)", version)
    if match is None:
        raise BackupError(f"invalid AutoDJ version in backup: {version!r}")
    return int(match.group(1)), int(match.group(2))


def _reject_nonfinite_json(value: str) -> None:
    """Reject a non-finite JSON constant encountered in a manifest."""

    raise BackupError(f"backup manifest contains non-finite JSON constant {value}")


def _validate_creator_fields(manifest: dict[str, Any]) -> None:
    """Validate manifest metadata recorded by the backup creator."""

    for field in ("created_at", "index_name"):
        if not isinstance(manifest.get(field), str):
            raise BackupError(f"backup manifest {field} must be a string")
    mode = manifest.get("mode")
    if not isinstance(mode, str) or mode not in {"online", "stopped"}:
        raise BackupError("backup manifest mode must be 'online' or 'stopped'")


def _target_is_regular(target: Path) -> bool:
    """Return whether a restore target exists as a safe regular file."""

    if not os.path.lexists(target):
        return False
    try:
        metadata = target.lstat()
    except OSError as exc:
        raise BackupError(f"unable to inspect restore target {target}: {exc}") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise BackupError(f"restore target is not a regular file: {target}")
    return True


def _assert_target_contained(target: Path, root: Path) -> None:
    """Ensure a restore target's resolved parent remains inside its root."""

    try:
        parent = target.parent.resolve(strict=False)
    except OSError as exc:
        raise BackupError(f"unsafe restore path {target}: {exc}") from exc
    if parent != target.parent or (parent != root and root not in parent.parents):
        raise BackupError(f"restore target parent changed or escapes configured root: {target}")


def _resolve_targets(
    cfg: AutoDJConfig, items: list[BackupItem], *, force: bool
) -> list[_ResolvedRestore]:
    """Resolve every manifest item into a distinct validated restore target."""

    targets: list[_ResolvedRestore] = []
    resolved: set[Path] = set()
    for item in items:
        parts = PurePosixPath(item.destination).parts
        relative = PurePosixPath(*parts[1:])
        target, root, ancestors = _destination(cfg, parts[0], relative)
        if target in resolved:
            raise BackupError(f"duplicate restore target {target}")
        exists = _target_is_regular(target)
        _revalidate_ancestor_identities(ancestors, target=target)
        if exists and not force:
            raise BackupError(f"{target} exists; pass --force to replace it")
        resolved.add(target)
        targets.append(_ResolvedRestore(item, target, root, force, ancestors))
    return targets


def _required_free_space(payload_bytes: int) -> int:
    """Return payload plus a 5% margin bounded from 64 MiB to 1 GiB."""

    margin = min(max(payload_bytes // 20, _SPACE_MARGIN_FLOOR), _SPACE_MARGIN_CAP)
    return payload_bytes + margin


def _existing_ancestor(path: Path) -> Path:
    """Return the nearest existing ancestor of a prospective restore path."""

    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise BackupError(f"no existing filesystem ancestor for restore target {path}")
        candidate = parent
    return candidate


def _preflight_free_space(targets: list[_ResolvedRestore]) -> None:
    """Require sufficient free space for staged payloads on each device."""

    by_device: dict[int, tuple[Path, int]] = {}
    for resolved in targets:
        _validate_restore_guard(resolved)
        anchor = _existing_ancestor(resolved.target.parent)
        try:
            device = os.stat(anchor).st_dev
        except OSError as exc:
            raise BackupError(f"unable to inspect restore filesystem at {anchor}: {exc}") from exc
        prior_anchor, total = by_device.get(device, (anchor, 0))
        by_device[device] = (prior_anchor, total + resolved.item.size)
        _validate_restore_guard(resolved)
    for anchor, payload_bytes in by_device.values():
        required = _required_free_space(payload_bytes)
        try:
            free = shutil.disk_usage(anchor).free
        except OSError as exc:
            raise BackupError(f"unable to inspect free space at {anchor}: {exc}") from exc
        if free < required:
            raise BackupError(
                f"insufficient free space on {anchor}: need {required} bytes for staged payload "
                f"plus bounded safety margin; {free} available"
            )


def _missing_parents(parent: Path, root: Path) -> tuple[Path, ...]:
    """List nonexistent parents that staging may need to create."""

    missing: list[Path] = []
    candidate = parent
    while not candidate.exists():
        missing.append(candidate)
        next_candidate = candidate.parent
        if next_candidate == candidate:
            raise BackupError(f"no existing filesystem ancestor for restore target {parent}")
        candidate = next_candidate
    return tuple(reversed(missing))


def _cleanup_empty_parents(records: list[_StagedRestore]) -> None:
    """Remove empty directories created for failed restore staging."""

    created = {path for record in records for path in record.created_parents}
    for path in sorted(created, key=lambda value: len(value.parts), reverse=True):
        with suppress(OSError, BackupError, StopIteration):
            guarded = next(record for record in records if path in record.target.parents)
            _validate_restore_guard(guarded)
            path.rmdir()
            _validate_restore_guard(guarded)


def _validate_restore_guard(record: _ResolvedRestore | _StagedRestore) -> None:
    """Revalidate a restore record's ancestors and target containment."""

    _revalidate_ancestor_identities(record.ancestors, target=record.target)
    _assert_target_contained(record.target, record.root)


def _validate_restore_file_identity(
    path: Path,
    expected: tuple[int, int, int, int, int] | None,
    *,
    description: str,
) -> None:
    """Ensure a restore file remains the captured safe regular file."""

    if expected is None:
        raise BackupError(f"{description} identity was not captured: {path}")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BackupError(f"{description} changed or disappeared: {path}: {exc}") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or _file_identity(metadata) != expected
    ):
        raise BackupError(f"{description} identity changed: {path}")


def _reconcile_previous_move(
    record: _StagedRestore,
    expected_identity: tuple[int, int, int, int, int],
) -> None:
    """Reconcile whether moving a target into its recovery reservation succeeded."""

    if record.previous is None:
        raise BackupError("restore recovery reservation was not recorded")
    observed = _observed_regular_identity(record.previous)
    if observed == expected_identity:
        record.previous_identity = observed
        record.previous_populated = True
        return
    target_observed = _observed_regular_identity(record.target)
    if observed == record.previous_placeholder_identity and target_observed == expected_identity:
        record.previous_populated = False
        return
    raise BackupError(
        f"restore target move outcome could not be reconciled; inspect {record.target} and "
        f"recovery reservation {record.previous}"
    )


def _reconcile_installed_target(record: _StagedRestore) -> None:
    """Reconcile whether a staged payload was installed at its target."""

    observed = _observed_regular_identity(record.target)
    if observed == record.stage_identity:
        record.installed = True
        record.installed_identity = observed
        return
    if _observed_regular_identity(record.stage) == record.stage_identity:
        record.installed = False
        record.installed_identity = None
        return
    raise BackupError(
        f"restore install outcome could not be reconciled; inspect target {record.target} "
        f"and stage {record.stage}"
    )


def _move_target_to_previous(record: _StagedRestore) -> None:
    """Move an existing target to a guarded recovery reservation."""

    expected_identity = _observed_regular_identity(record.target)
    if expected_identity is None:
        raise BackupError(f"restore target changed or disappeared: {record.target}")
    descriptor, previous_name = tempfile.mkstemp(
        prefix=f".{record.target.name}.restore-old-",
        dir=record.target.parent,
    )
    previous = Path(previous_name)
    record.previous = previous
    record.previous_placeholder_identity = _file_identity(os.fstat(descriptor))
    record.previous_identity = expected_identity
    try:
        os.close(descriptor)
        _validate_restore_guard(record)
        _validate_restore_file_identity(
            record.target,
            expected_identity,
            description="restore target",
        )
        os.replace(record.target, previous)
        _validate_restore_guard(record)
    except BaseException as exc:
        try:
            _reconcile_previous_move(record, expected_identity)
        except BackupError as reconcile_exc:
            raise reconcile_exc from exc
        raise
    _reconcile_previous_move(record, expected_identity)
    if not record.previous_populated:
        raise BackupError(f"restore target move did not complete: {record.target}")


def _install_stage(record: _StagedRestore) -> None:
    """Atomically install a validated staging file at its restore target."""

    _validate_restore_guard(record)
    _validate_restore_file_identity(
        record.stage,
        record.stage_identity,
        description="restore staging file",
    )
    if _observed_regular_identity(record.target) is not None:
        raise BackupError(f"{record.target} appeared during restore; refusing to replace it")
    try:
        if record.force:
            os.replace(record.stage, record.target)
        else:
            os.link(record.stage, record.target)
        _validate_restore_guard(record)
    except BaseException as exc:
        try:
            _reconcile_installed_target(record)
        except BackupError as reconcile_exc:
            raise reconcile_exc from exc
        if isinstance(exc, FileExistsError):
            raise BackupError(
                f"{record.target} appeared during restore; refusing to replace it without --force"
            ) from exc
        if isinstance(exc, OSError):
            operation = "replace" if record.force else "no-clobber install"
            raise BackupError(
                f"filesystem could not atomically {operation} restore target {record.target}: {exc}"
            ) from exc
        raise
    _reconcile_installed_target(record)
    if not record.installed:
        raise BackupError(f"restore target install did not complete: {record.target}")


def _restore_previous(record: _StagedRestore) -> None:
    """Restore the former target after a failed staged installation."""

    if record.previous is None or record.previous_identity is None:
        raise BackupError(f"restore recovery identity is missing for {record.target}")
    previous = record.previous
    expected_identity = record.previous_identity
    _validate_restore_guard(record)
    _validate_restore_file_identity(
        previous,
        expected_identity,
        description="restore recovery copy",
    )
    if record.installed:
        _validate_restore_file_identity(
            record.target,
            record.installed_identity,
            description="installed restore target",
        )
    try:
        os.replace(previous, record.target)
        _validate_restore_guard(record)
    except BaseException as exc:
        if _observed_regular_identity(record.target) == expected_identity:
            try:
                _validate_restore_guard(record)
            except BaseException as guard_exc:
                raise BackupError(
                    f"old restore target was reinstalled at {record.target}, but containment "
                    f"validation failed: {guard_exc}"
                ) from exc
            record.previous_populated = False
            record.installed = False
            return
        raise BackupError(
            f"restore rollback move failed; recovery copy retained at {previous}: {exc}"
        ) from exc
    if _observed_regular_identity(record.target) != expected_identity:
        raise BackupError(
            f"restore rollback move outcome could not be reconciled; inspect target "
            f"{record.target} and recovery copy {previous}"
        )
    record.previous_populated = False
    record.installed = False


def _cleanup_stages(records: list[_StagedRestore]) -> list[str]:
    """Remove remaining staging files and return cleanup errors."""

    errors: list[str] = []
    for record in records:
        try:
            _validate_restore_guard(record)
            if os.path.lexists(record.stage):
                _validate_restore_file_identity(
                    record.stage,
                    record.stage_identity,
                    description="restore staging file",
                )
            record.stage.unlink(missing_ok=True)
            _validate_restore_guard(record)
        except BaseException as exc:
            errors.append(f"retained restore stage {record.stage}: {exc}")
    return errors


def _stage_payloads(zf: zipfile.ZipFile, targets: list[_ResolvedRestore]) -> list[_StagedRestore]:
    """Extract verified archive payloads into guarded staging files."""

    staged: list[_StagedRestore] = []
    try:
        for resolved in targets:
            _validate_restore_guard(resolved)
            created = _missing_parents(resolved.target.parent, resolved.root)
            current_ancestors = resolved.ancestors
            descriptor = -1
            record: _StagedRestore | None = None
            try:
                _validate_restore_guard(resolved)
                resolved.target.parent.mkdir(parents=True, exist_ok=True)
                _revalidate_ancestor_identities(current_ancestors, target=resolved.target)
                _assert_target_contained(resolved.target, resolved.root)
                current_ancestors = _capture_ancestor_identities(resolved.target.parent)
                _revalidate_ancestor_identities(current_ancestors, target=resolved.target)
                descriptor, stage_name = tempfile.mkstemp(
                    prefix=f".{resolved.target.name}.restore-stage-",
                    dir=resolved.target.parent,
                )
                stage = Path(stage_name)
                record = _StagedRestore(
                    item=resolved.item,
                    target=resolved.target,
                    root=resolved.root,
                    force=resolved.force,
                    stage=stage,
                    ancestors=current_ancestors,
                    created_parents=created,
                )
                staged.append(record)
                record.stage_identity = _file_identity(os.fstat(descriptor))
                _validate_restore_file_identity(
                    stage,
                    record.stage_identity,
                    description="restore staging file",
                )
                _revalidate_ancestor_identities(current_ancestors, target=resolved.target)
            except BaseException:
                if descriptor >= 0:
                    os.close(descriptor)
                if record is None:
                    for directory in reversed(created):
                        with suppress(OSError):
                            directory.rmdir()
                raise
            record = cast(_StagedRestore, record)
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(descriptor, "wb") as dst, zf.open(resolved.item.archive_path) as src:
                descriptor = -1
                while chunk := src.read(_COPY_CHUNK_BYTES):
                    dst.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                dst.flush()
                os.fsync(dst.fileno())
                opened_stage = os.fstat(dst.fileno())
            staged_path = record.stage.lstat()
            if (
                not stat.S_ISREG(staged_path.st_mode)
                or _is_reparse(staged_path)
                or _object_identity(opened_stage) != _object_identity(staged_path)
                or opened_stage.st_size != staged_path.st_size
            ):
                raise BackupError(f"restore staging file identity changed: {record.stage}")
            record.stage_identity = _file_identity(staged_path)
            if record.stage.parent.resolve(strict=True) != resolved.target.parent:
                raise BackupError(f"restore staging path escaped target directory: {record.stage}")
            if size != resolved.item.size:
                raise BackupError(f"backup member size mismatch: {resolved.item.archive_path}")
            if digest.hexdigest() != resolved.item.sha256:
                raise BackupError(f"backup member checksum mismatch: {resolved.item.archive_path}")
        return staged
    except BaseException as exc:
        cleanup_errors = _cleanup_stages(staged)
        _cleanup_empty_parents(staged)
        if cleanup_errors:
            raise BackupError(
                f"backup member extraction failed: {exc}; retained restore stages: "
                + "; ".join(cleanup_errors)
            ) from exc
        if isinstance(exc, BackupError) or not isinstance(exc, Exception):
            raise
        raise BackupError(f"backup member extraction failed: {exc}") from exc


def _read_staged_bytes(record: _StagedRestore) -> bytes:
    """Read a bounded staged manifest after verifying its identity."""

    if record.item.size > MAX_MANIFEST_BYTES:
        raise BackupError(
            f"backup index manifest exceeds 16 MiB metadata limit: {record.item.size} bytes"
        )
    _validate_restore_guard(record)
    _validate_restore_file_identity(
        record.stage,
        record.stage_identity,
        description="restore staging file",
    )
    try:
        with record.stage.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if _file_identity(opened) != record.stage_identity:
                raise BackupError(f"restore staging file identity changed: {record.stage}")
            payload = handle.read(MAX_MANIFEST_BYTES + 1)
            closed = os.fstat(handle.fileno())
    except OSError as exc:
        raise BackupError(f"restore staging file could not be read: {record.stage}: {exc}") from exc
    observed = record.stage.lstat()
    if (
        _object_identity(opened) != _object_identity(closed)
        or _object_identity(closed) != _object_identity(observed)
        or closed.st_size != observed.st_size
    ):
        raise BackupError(f"restore staging file identity changed: {record.stage}")
    if len(payload) > MAX_MANIFEST_BYTES:
        raise BackupError(
            f"backup index manifest exceeds 16 MiB metadata limit: {len(payload)} bytes"
        )
    _validate_restore_guard(record)
    return payload


def _staged_index_manifest(record: _StagedRestore) -> IndexManifest:
    """Parse and validate the index manifest held in a staged record."""

    payload = _read_staged_bytes(record)
    try:
        with tempfile.TemporaryDirectory(prefix="autodj-restore-manifest-") as temp_name:
            root = Path(temp_name)
            (root / MANIFEST_NAME).write_bytes(payload)
            manifest = read_manifest(root)
    except IndexConsistencyError as exc:
        raise BackupError(f"backup index manifest is invalid: {exc}") from exc
    return cast(IndexManifest, manifest)


def _rewrite_staged_payload(record: _StagedRestore, payload: bytes) -> None:
    """Durably replace a staged payload and refresh its recorded identity."""

    _validate_restore_guard(record)
    _validate_restore_file_identity(
        record.stage,
        record.stage_identity,
        description="restore staging file",
    )
    try:
        with record.stage.open("r+b") as handle:
            before = os.fstat(handle.fileno())
            if _file_identity(before) != record.stage_identity:
                raise BackupError(f"restore staging file identity changed: {record.stage}")
            handle.seek(0)
            handle.write(payload)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise BackupError(
            f"restore staging file could not be reconciled: {record.stage}: {exc}"
        ) from exc
    observed = record.stage.lstat()
    if (
        _object_identity(before) != _object_identity(after)
        or _object_identity(after) != _object_identity(observed)
        or after.st_size != len(payload)
        or observed.st_size != len(payload)
    ):
        raise BackupError(f"restore staging file identity changed: {record.stage}")
    record.stage_identity = _file_identity(observed)
    record.item = replace(
        record.item,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    _validate_restore_guard(record)


def _stage_publication_state(
    cfg: AutoDJConfig,
    payload: bytes,
    *,
    force: bool,
) -> _StagedRestore:
    """Stage the reconciled publication state for atomic installation."""

    target, root, ancestors = _destination(
        cfg,
        "active",
        PurePosixPath(PUBLICATION_STATE_NAME),
    )
    item = BackupItem(
        archive_path="internal/publication-state",
        classification="derived",
        destination=f"active/{PUBLICATION_STATE_NAME}",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    resolved = _ResolvedRestore(item, target, root, force, ancestors)
    _validate_restore_guard(resolved)
    created = _missing_parents(target.parent, root)
    descriptor = -1
    record: _StagedRestore | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _revalidate_ancestor_identities(ancestors, target=target)
        current_ancestors = _capture_ancestor_identities(target.parent)
        descriptor, stage_name = tempfile.mkstemp(
            prefix=f".{target.name}.restore-stage-",
            dir=target.parent,
        )
        record = _StagedRestore(
            item=item,
            target=target,
            root=root,
            force=force,
            stage=Path(stage_name),
            ancestors=current_ancestors,
            created_parents=created,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            opened = os.fstat(handle.fileno())
            record.stage_identity = _file_identity(opened)
        observed = record.stage.lstat()
        if (
            not stat.S_ISREG(observed.st_mode)
            or _is_reparse(observed)
            or _object_identity(opened) != _object_identity(observed)
            or observed.st_size != len(payload)
        ):
            raise BackupError(f"restore staging file identity changed: {record.stage}")
        record.stage_identity = _file_identity(observed)
        _validate_restore_guard(record)
        return record
    except BaseException as exc:
        if descriptor >= 0:
            os.close(descriptor)
        cleanup_errors: list[str] = []
        if record is not None:
            cleanup_errors = _cleanup_stages([record])
        for directory in reversed(created):
            with suppress(OSError):
                directory.rmdir()
        if cleanup_errors:
            raise BackupError(
                f"publication state staging failed: {exc}; retained restore stages: "
                + "; ".join(cleanup_errors)
            ) from exc
        raise


def _validate_staged_index_snapshot(
    manifest: IndexManifest,
    by_destination: dict[str, _StagedRestore],
) -> None:
    """Validate staged index artifacts against their staged manifest."""

    tracks = by_destination["active/tracks.db"]
    vectors = by_destination["active/vectors.index"]
    records = (tracks, vectors)
    for record in records:
        _validate_restore_guard(record)
        _validate_restore_file_identity(
            record.stage,
            record.stage_identity,
            description="restore staging file",
        )
    if tracks.stage.parent != vectors.stage.parent:
        raise BackupError("backup index staging files do not share a validated directory")
    staged_manifest = replace(
        manifest,
        tracks_file=tracks.stage.name,
        vectors_file=vectors.stage.name,
    )
    try:
        index_publication._validate_snapshot_files(tracks.stage.parent, staged_manifest)
    except (IndexConsistencyError, OSError, RuntimeError, sqlite3.DatabaseError) as exc:
        raise BackupError(f"backup index snapshot is invalid: {exc}") from exc
    for record in records:
        _validate_restore_file_identity(
            record.stage,
            record.stage_identity,
            description="restore staging file",
        )
        _validate_restore_guard(record)


def _prepare_publication_restore(
    cfg: AutoDJConfig,
    staged: list[_StagedRestore],
    *,
    force: bool,
) -> _StagedRestore:
    """Reconcile index revisions and stage the resulting publication state."""

    by_destination = {record.item.destination: record for record in staged}
    manifest_record = by_destination[f"active/{MANIFEST_NAME}"]
    required = {
        "active/tracks.db",
        "active/vectors.index",
        f"active/{MANIFEST_NAME}",
    }
    missing = required - set(by_destination)
    if missing:
        raise BackupError(
            "backup index publication is incomplete; missing " + ", ".join(sorted(missing))
        )
    restored = _staged_index_manifest(manifest_record)
    if (restored.tracks_file, restored.vectors_file) != ("tracks.db", "vectors.index"):
        raise BackupError(
            "backup index manifest must reference canonical tracks.db/vectors.index artifacts"
        )
    restored_identity = max(restored.generation, restored.state_revision)
    if restored_identity > _MAX_INDEX_REVISION:
        raise BackupError(
            "backup index revision exceeds the supported 20-digit generation filename range"
        )
    _validate_staged_index_snapshot(restored, by_destination)
    active = cfg.index.active_dir
    try:
        target_state = index_publication._read_publication_state(active)
        target_manifest = read_manifest(active)
    except IndexConsistencyError as exc:
        raise BackupError(f"restore target publication state is invalid: {exc}") from exc
    target_high_water = max(
        0 if target_state is None else target_state.high_water,
        0 if target_state is None else target_state.tombstone_revision,
        0 if target_manifest is None else target_manifest.generation,
        0 if target_manifest is None else target_manifest.state_revision,
    )
    tombstone_revision = max(
        0 if target_state is None else target_state.tombstone_revision,
        0 if target_manifest is None else target_manifest.generation,
        0 if target_manifest is None else target_manifest.state_revision,
    )
    if restored_identity > target_high_water:
        revision = restored_identity
    else:
        if target_high_water >= _MAX_INDEX_REVISION:
            raise BackupError(
                "restore target has exhausted the supported 20-digit generation filename range"
            )
        revision = target_high_water + 1
    reconciled = replace(
        restored,
        schema_version=INDEX_MANIFEST_SCHEMA_VERSION,
        generation=revision,
        state_revision=revision,
    )
    manifest_payload = (json.dumps(asdict(reconciled), sort_keys=True) + "\n").encode()
    _rewrite_staged_payload(manifest_record, manifest_payload)
    state_payload = (
        json.dumps(
            {"high_water": revision, "tombstone_revision": tombstone_revision},
            sort_keys=True,
        )
        + "\n"
    ).encode()
    return _stage_publication_state(cfg, state_payload, force=force)


def _publication_restore_order(record: _StagedRestore) -> tuple[int, str]:
    """Return canonical commit order independent of archive item order."""

    destination = record.item.destination
    priorities = {
        f"active/{PUBLICATION_STATE_NAME}": 0,
        "active/tracks.db": 1,
        "active/vectors.index": 2,
        f"active/{MANIFEST_NAME}": 4,
    }
    return priorities.get(destination, 3), destination


def _rollback_staged(staged: list[_StagedRestore]) -> list[str]:
    """Undo staged installations and return any rollback errors."""

    errors: list[str] = []
    for record in reversed(staged):
        try:
            _validate_restore_guard(record)
            if record.previous_populated:
                _restore_previous(record)
            elif record.installed:
                _validate_restore_file_identity(
                    record.target,
                    record.installed_identity,
                    description="installed restore target",
                )
                try:
                    record.target.unlink(missing_ok=True)
                    _validate_restore_guard(record)
                except BaseException as exc:
                    if not os.path.lexists(record.target):
                        try:
                            _validate_restore_guard(record)
                        except BaseException as guard_exc:
                            raise BackupError(
                                f"installed restore target was removed at {record.target}, but "
                                f"containment validation failed: {guard_exc}"
                            ) from exc
                        record.installed = False
                    else:
                        raise BackupError(
                            f"installed restore target retained at {record.target}: {exc}"
                        ) from exc
            if (
                record.previous is not None
                and not record.previous_populated
                and os.path.lexists(record.previous)
            ):
                _validate_restore_file_identity(
                    record.previous,
                    record.previous_placeholder_identity,
                    description="restore recovery reservation",
                )
                record.previous.unlink(missing_ok=True)
            _validate_restore_guard(record)
        except BaseException as exc:
            errors.append(f"{record.target}: {exc}")
    errors.extend(_cleanup_stages(staged))
    _cleanup_empty_parents(staged)
    return errors


def _commit_staged(
    staged: list[_StagedRestore],
    *,
    restored_count: int | None = None,
) -> RestoreResult:
    """Install staged restore files and roll back the batch on failure."""

    parents = {record.target.parent for record in staged}
    parents.update(parent.parent for record in staged for parent in record.created_parents)
    try:
        for record in staged:
            _validate_restore_guard(record)
            exists = _target_is_regular(record.target)
            if exists and not record.force:
                raise BackupError(f"{record.target} appeared during restore; pass --force")
        for record in staged:
            _validate_restore_guard(record)
            _validate_restore_file_identity(
                record.stage,
                record.stage_identity,
                description="restore staging file",
            )
            if record.force:
                if _target_is_regular(record.target):
                    _move_target_to_previous(record)
                _install_stage(record)
                continue
            _install_stage(record)
            # If stage cleanup fails, rollback removes the installed target hard link.
            _validate_restore_guard(record)
            record.stage.unlink()
            _validate_restore_guard(record)
    except BaseException as exc:
        rollback_errors = _rollback_staged(staged)
        if rollback_errors:
            retained = [
                str(record.previous)
                for record in staged
                if record.previous_populated
                and record.previous is not None
                and os.path.lexists(record.previous)
            ]
            retained_text = "; ".join(retained) if retained else "none"
            raise BackupError(
                "restore failed and rollback was incomplete; inspect targets; retained recovery "
                f"copies: {retained_text}; errors: " + "; ".join(rollback_errors)
            ) from exc
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, BackupError):
            raise BackupError(f"{exc}; previous files restored") from exc
        raise BackupError("restore failed; previous files restored") from exc

    warnings: list[str] = []
    for record in staged:
        if record.previous_populated and record.previous is not None:
            try:
                _validate_restore_guard(record)
                _validate_restore_file_identity(
                    record.previous,
                    record.previous_identity,
                    description="restore recovery copy",
                )
                record.previous.unlink(missing_ok=True)
                _validate_restore_guard(record)
            except (OSError, BackupError) as exc:
                warnings.append(f"recovery copy retained at {record.previous}: {exc}")
    for parent in parents:
        try:
            guarded = next(record for record in staged if parent in record.target.parents)
            _validate_restore_guard(guarded)
            _fsync_directory(parent)
            _validate_restore_guard(guarded)
        except (OSError, BackupError, StopIteration) as exc:
            warnings.append(f"directory sync failed for installed restore at {parent}: {exc}")
    return RestoreResult(
        restored=len(staged) if restored_count is None else restored_count,
        warnings=tuple(warnings),
    )


def restore_backup(cfg: AutoDJConfig, archive: Path, *, force: bool) -> RestoreResult:
    """Preflight and stage every payload, then install with rollback."""

    try:
        archive_handle = archive.open("rb")
    except OSError as exc:
        raise BackupError(f"backup archive is unreadable: {exc}") from exc
    staged: list[_StagedRestore] = []
    try:
        with archive_handle:
            opened_identity = _open_handle_identity(archive_handle)
            _preflight_zip_metadata(archive_handle)
            archive_handle.seek(0)
            with zipfile.ZipFile(archive_handle) as zf:
                members = _member_map(zf)
                manifest_info = members.get("manifest.json")
                if manifest_info is None:
                    raise BackupError("backup manifest is missing")
                if manifest_info.file_size > MAX_MANIFEST_BYTES:
                    raise BackupError(
                        "backup manifest exceeds 16 MiB metadata limit: "
                        f"{manifest_info.file_size} bytes"
                    )
                try:
                    manifest = json.loads(
                        zf.read(manifest_info), parse_constant=_reject_nonfinite_json
                    )
                except BackupError:
                    raise
                except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise BackupError("backup manifest is missing or invalid") from exc
                if not isinstance(manifest, dict):
                    raise BackupError("backup manifest must be an object")
                _validate_creator_fields(manifest)
                schema = manifest.get("schema_version")
                if isinstance(schema, bool) or not isinstance(schema, int):
                    raise BackupError("backup manifest schema_version must be an integer")
                if schema != SCHEMA_VERSION:
                    raise BackupError(
                        f"unsupported backup schema {schema}; expected {SCHEMA_VERSION}"
                    )
                backup_version = manifest.get("autodj_version")
                if not isinstance(backup_version, str):
                    raise BackupError("backup manifest autodj_version must be a string")
                running_version = current_version()
                if _compatibility_line(backup_version) != _compatibility_line(running_version):
                    raise BackupError(
                        f"backup AutoDJ version {backup_version} is incompatible with "
                        f"{running_version}"
                    )
                items = _parse_items(members, manifest)
                targets = _resolve_targets(cfg, items, force=force)
                _preflight_free_space(targets)
                staged = _stage_payloads(zf, targets)
            closed_identity = _open_handle_identity(archive_handle)
            if (
                opened_identity is not None
                and closed_identity is not None
                and opened_identity != closed_identity
            ):
                raise BackupError("backup archive changed while it was being read")
    except BaseException as exc:
        cleanup_errors = _cleanup_stages(staged)
        _cleanup_empty_parents(staged)
        if cleanup_errors:
            raise BackupError(
                f"backup archive validation failed: {exc}; retained restore stages: "
                + "; ".join(cleanup_errors)
            ) from exc
        if isinstance(exc, BackupError) or not isinstance(exc, Exception):
            raise
        if isinstance(exc, (OSError, RuntimeError, ValueError, zipfile.BadZipFile)):
            raise BackupError(f"backup archive validation failed: {exc}") from exc
        raise
    restored_count = len(staged)
    has_index_manifest = any(
        record.item.destination == f"active/{MANIFEST_NAME}" for record in staged
    )
    if not has_index_manifest:
        return _commit_staged(staged)
    with publication_lock(cfg.index.active_dir):
        try:
            state_record = _prepare_publication_restore(cfg, staged, force=force)
        except BaseException as exc:
            cleanup_errors = _cleanup_stages(staged)
            _cleanup_empty_parents(staged)
            if cleanup_errors:
                raise BackupError(
                    f"restore publication preparation failed: {exc}; retained restore stages: "
                    + "; ".join(cleanup_errors)
                ) from exc
            if isinstance(exc, BackupError) or not isinstance(exc, Exception):
                raise
            raise BackupError(f"restore publication preparation failed: {exc}") from exc
        staged.append(state_record)
        staged.sort(key=_publication_restore_order)
        return _commit_staged(staged, restored_count=restored_count)
