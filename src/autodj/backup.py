"""Back up and restore the index and the data that exists only on this machine.

An archive is a plain ZIP file holding ``manifest.json`` (the AutoDJ version
that wrote it and the list of files) and these members:

- ``index/tracks.db``, ``index/vectors.index`` and ``index/index-manifest.json``:
  the published index generation, under the canonical names.
- ``index/dj_meta.db`` and ``index/web_state.json`` from the active index folder.
- ``liners/...`` and ``profiles/...``: every file in those folders.
- ``history``: the ``[playback] history_file``, when one is configured.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from autodj import index_manifest as im
from autodj.fsutil import fsync_directory
from autodj.sqlite_utils import readonly_uri
from autodj.version import current_version

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig

# Installed in this order, so the index manifest only changes once its files are in place.
INDEX_FILES = ("tracks.db", "vectors.index", "dj_meta.db", "web_state.json", im.MANIFEST_NAME)
_FOLDERS = ("liners", "profiles")


class BackupError(RuntimeError):
    """Raised when a backup cannot be written or an archive cannot be restored."""


def _roots(cfg: AutoDJConfig) -> dict[str, Path]:
    """Map each top-level archive name to where it lives with this configuration."""
    active = cfg.index.active_dir
    roots = {
        "index": active,
        "liners": Path(cfg.playback.liners_folder or active / "liners"),
        "profiles": active.parent / "profiles",
    }
    if cfg.playback.history_file:
        roots["history"] = cfg.playback.history_file
    return roots


def _snapshot_index(active: Path, snapshot: Path) -> None:
    """Copy the published index generation and ``dj_meta.db`` into *snapshot*."""
    try:
        if im.read_manifest(active) is not None:
            im.copy_published_snapshot(active, snapshot)
        else:
            im.require_current_format(active)
            if any((active / name).exists() for name in ("tracks.db", "vectors.index")):
                raise BackupError(
                    f"the index has no published manifest; rebuild it with `{im.REBUILD_COMMAND}`"
                )
    except im.UnsupportedIndexError as exc:
        raise BackupError(str(exc)) from exc
    except im.IndexConsistencyError as exc:
        raise BackupError(f"the published index is invalid: {exc}") from exc
    dj_meta = active / "dj_meta.db"
    if dj_meta.is_file():
        snapshot.mkdir(parents=True, exist_ok=True)
        with (
            closing(sqlite3.connect(readonly_uri(dj_meta), uri=True)) as source,
            closing(sqlite3.connect(snapshot / "dj_meta.db")) as target,
        ):
            source.backup(target)


def create_backup(cfg: AutoDJConfig, destination: Path, *, force: bool) -> Path:
    """Write a backup archive to *destination* and return its absolute path.

    Safe while AutoDJ serves: the index is copied under its publication lock
    and ``dj_meta.db`` through SQLite's backup API.

    Raises:
        BackupError: If *destination* exists without *force*, or the index is
            old, damaged or unreadable.
    """
    destination = destination.expanduser().resolve()
    if destination.exists() and not force:
        raise BackupError(f"{destination} already exists; pass --force to replace it")
    roots = _roots(cfg)
    for top in _FOLDERS:
        if destination.is_relative_to(roots[top].resolve()):
            raise BackupError(f"write the backup outside the {top} folder {roots[top]}")
    partial = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tempfile.TemporaryDirectory(prefix="autodj-backup-") as temp:
            snapshot = Path(temp) / "index"
            _snapshot_index(roots["index"], snapshot)
            members: dict[str, Path] = {}
            web_state = roots["index"] / "web_state.json"
            for path in [*sorted(snapshot.glob("*")), web_state]:
                if path.is_file():
                    members[f"index/{path.name}"] = path
            for top in _FOLDERS:
                for path in sorted(roots[top].rglob("*")):
                    if path.is_file():
                        members[f"{top}/{path.relative_to(roots[top]).as_posix()}"] = path
            if "history" in roots and roots["history"].is_file():
                members["history"] = roots["history"]
            with partial.open("xb") as handle:
                with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
                    for name, path in members.items():
                        archive.write(path, name)
                    manifest = {"autodj_version": current_version(), "files": list(members)}
                    archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(partial, destination)
    except BaseException as exc:
        partial.unlink(missing_ok=True)
        if isinstance(exc, (OSError, sqlite3.Error)):
            raise BackupError(f"could not write {destination}: {exc}") from exc
        raise
    fsync_directory(destination.parent)
    return destination


def _read_file_list(archive: zipfile.ZipFile) -> list[str]:
    """Return the file list of a backup made by this major.minor version."""
    try:
        manifest = json.loads(archive.read("manifest.json"))
        version, files = manifest["autodj_version"], manifest["files"]
        if not isinstance(version, str) or not isinstance(files, list):
            raise TypeError("wrong field types")
        if not all(isinstance(name, str) for name in files):
            raise TypeError("wrong field types")
    except (KeyError, TypeError, ValueError) as exc:
        raise BackupError(f"the archive has no valid manifest.json: {exc!r}") from exc
    running = current_version()
    if version.split(".")[:2] != running.split(".")[:2]:
        raise BackupError(
            f"this backup was made by AutoDJ {version}; AutoDJ {running} only restores "
            "backups made by the same major and minor version"
        )
    return list(files)


def _target(roots: dict[str, Path], name: str) -> Path:
    """Return where archive member *name* is restored, refusing unsafe or unknown names."""
    parts = PurePosixPath(name).parts
    if not parts or "/".join(parts) != name or ".." in parts or "\\" in name or ":" in name:
        raise BackupError(f"unsafe file name in archive: {name!r}")
    top, rest = parts[0], parts[1:]
    if top == "index" and len(rest) == 1 and rest[0] in INDEX_FILES:
        return roots["index"] / rest[0]
    if top in _FOLDERS and rest:
        return roots[top].joinpath(*rest)
    if top == "history" and not rest:
        if "history" not in roots:
            raise BackupError("this backup holds a play history; set [playback] history_file")
        return roots["history"]
    raise BackupError(f"unknown file in archive: {name!r}")


def _check_index(stage: Path) -> None:
    """Refuse a staged index that is damaged, incomplete or made by an older AutoDJ."""
    try:
        im.require_current_format(stage)
        manifest = im.read_manifest(stage)
    except im.UnsupportedIndexError as exc:
        raise BackupError(str(im.UnsupportedIndexError("this backup", exc.reason))) from exc
    except im.IndexConsistencyError as exc:
        raise BackupError(f"the index in this backup is invalid: {exc}") from exc
    if manifest and not all((stage / name).is_file() for name in INDEX_FILES[:2]):
        raise BackupError("the index in this backup is incomplete")


def _install(top: str, root: Path, stage: Path) -> None:
    """Move one staged part of a backup into place and drop what it supersedes."""
    if top == "index":
        restored = {path.name for path in stage.iterdir()}
        stale = [root / f"{db}{suffix}" for db in restored for suffix in ("-wal", "-shm")]
        # The backup's generation replaces the whole publication history.
        if im.MANIFEST_NAME in restored:
            stale += [*root.glob("tracks.g*.db"), *root.glob("vectors.g*.index")]
            stale.append(root / im.PUBLICATION_STATE_NAME)
        for path in stale:
            path.unlink(missing_ok=True)
        for name in (name for name in INDEX_FILES if name in restored):
            os.replace(stage / name, root / name)
    elif top == "history":
        os.replace(stage / "history", root)
    else:
        old = root.with_name(f".{root.name}.old-{uuid.uuid4().hex}")
        if root.exists():
            os.replace(root, old)
        os.replace(stage, root)
        shutil.rmtree(old, ignore_errors=True)
    fsync_directory(root if top == "index" else root.parent)


def _restore(archive: zipfile.ZipFile, roots: dict[str, Path], *, force: bool) -> int:
    """Check, unpack and install *archive*; return the number of files restored."""
    files = _read_file_list(archive)
    targets = {name: _target(roots, name) for name in files}
    if sorted(files) != sorted(name for name in archive.namelist() if name != "manifest.json"):
        raise BackupError("the archive's files do not match its manifest.json")
    tops = list(dict.fromkeys(name.split("/")[0] for name in files))
    replaced = [targets[name] for name in files if name.startswith("index/")]
    replaced += [roots[top] for top in tops if top != "index"]
    existing = [path for path in replaced if path.exists()]
    if existing and not force:
        raise BackupError(f"{existing[0]} already exists; pass --force to replace it")
    stages = {
        top: roots[top].with_name(f".{roots[top].name}.restore-{uuid.uuid4().hex}") for top in tops
    }
    with im.publication_lock(roots["index"]):
        try:
            for name in files:
                top, *rest = name.split("/")
                destination = stages[top].joinpath(*(rest or [top]))
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target)
            if "index" in stages:
                _check_index(stages["index"])
            for top, stage in stages.items():
                _install(top, roots[top], stage)
        finally:
            for stage in stages.values():
                shutil.rmtree(stage, ignore_errors=True)
    return len(files)


def restore_backup(cfg: AutoDJConfig, archive: Path, *, force: bool) -> int:
    """Restore a backup made by this major.minor version and return its file count.

    AutoDJ must be stopped first; nothing here can tell whether it runs.  Every
    file is unpacked beside its destination and the index checked before
    anything is replaced.  Restored ``liners`` and ``profiles`` folders replace
    the current ones whole, and a restored index replaces every generation.

    Raises:
        BackupError: If the archive is invalid, from another version, holds an
            old-format index, or would replace existing files without *force*.
    """
    try:
        with zipfile.ZipFile(archive) as opened:
            return _restore(opened, _roots(cfg), force=force)
    except BackupError:
        raise
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
        raise BackupError(f"could not restore {archive}: {exc}") from exc
