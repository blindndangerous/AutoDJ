"""Back up and restore the index and the data that exists only on this machine.

An archive is a plain ZIP file holding ``manifest.json`` (the AutoDJ version
that wrote it and the list of files) and these members:

- ``index/index-manifest.json`` and the two generation files it names
  (``tracks.gN.db`` and ``vectors.gN.index``): the live index generation.
- ``index/dj_meta.db`` and ``index/web_state.json`` from the active index folder.
- ``liners/...`` and ``profiles/...``: every file in those folders.
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

# Files of the index folder a backup holds besides the generation files.
INDEX_FILES = ("dj_meta.db", "web_state.json", im.MANIFEST_NAME)
_FOLDERS = ("liners", "profiles")


class BackupError(RuntimeError):
    """Raised when a backup cannot be written or an archive cannot be restored."""


def _roots(cfg: AutoDJConfig) -> dict[str, Path]:
    """Map each top-level archive name to where it lives with this configuration."""
    active = cfg.index.active_dir
    return {
        "index": active,
        "liners": Path(cfg.playback.liners_folder or active / "liners"),
        "profiles": active.parent / "profiles",
    }


def _snapshot_index(active: Path, snapshot: Path) -> None:
    """Copy the live index generation, its manifest and ``dj_meta.db`` into *snapshot*."""
    snapshot.mkdir(parents=True)
    with im.publication_lock(active):
        try:
            manifest: im.IndexManifest | None = im.require_manifest(active)
        except FileNotFoundError:
            manifest = None
        except im.UnsupportedIndexError as exc:
            raise BackupError(str(exc)) from exc
        except im.IndexConsistencyError as exc:
            raise BackupError(f"the published index is invalid: {exc}") from exc
        if manifest is not None:
            for name, digest in (
                (manifest.tracks_file, manifest.tracks_sha256),
                (manifest.vectors_file, manifest.vectors_sha256),
            ):
                shutil.copyfile(active / name, snapshot / name)
                if im.sha256_file(snapshot / name) != digest:
                    raise BackupError(f"{active / name} does not match its SHA-256 in the manifest")
            shutil.copyfile(active / im.MANIFEST_NAME, snapshot / im.MANIFEST_NAME)
    dj_meta = active / "dj_meta.db"
    if dj_meta.is_file():
        with (
            closing(sqlite3.connect(readonly_uri(dj_meta), uri=True)) as source,
            closing(sqlite3.connect(snapshot / "dj_meta.db")) as target,
        ):
            source.backup(target)


def create_backup(cfg: AutoDJConfig, destination: Path, *, force: bool) -> Path:
    """Write a backup archive to *destination* and return its absolute path.

    Safe while AutoDJ serves: the index is copied under its publication lock
    and checked against its manifest, and ``dj_meta.db`` is copied through
    SQLite's backup API.

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
    if (
        top == "index"
        and len(rest) == 1
        and (rest[0] in INDEX_FILES or im.GENERATION_FILE_RE.match(rest[0]))
    ):
        return roots["index"] / rest[0]
    if top in _FOLDERS and rest:
        return roots[top].joinpath(*rest)
    raise BackupError(f"unknown file in archive: {name!r}")


def _check_index(stage: Path) -> None:
    """Refuse a staged index that the loader would refuse."""
    from autodj.indexer import load_index

    try:
        load_index(stage)
    except FileNotFoundError:
        return  # the backup holds no index
    except im.UnsupportedIndexError as exc:
        raise BackupError(str(im.UnsupportedIndexError("this backup", exc.reason))) from exc
    except im.IndexConsistencyError as exc:
        raise BackupError(f"the index in this backup is invalid: {exc}") from exc


def _install(top: str, root: Path, stage: Path) -> None:
    """Move one staged part of a backup into place and drop what it supersedes."""
    if top == "index":
        restored = sorted(path.name for path in stage.iterdir())
        for name in restored:
            for suffix in ("-wal", "-shm"):
                (root / f"{name}{suffix}").unlink(missing_ok=True)
        # The manifest goes last, so it only names files that are in place.
        for name in sorted(restored, key=lambda name: name == im.MANIFEST_NAME):
            os.replace(stage / name, root / name)
        if im.MANIFEST_NAME in restored:
            # The backup's generation replaces every other one.
            for path in root.iterdir():
                if im.GENERATION_FILE_RE.match(path.name) and path.name not in restored:
                    path.unlink()
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
                destination = stages[top].joinpath(*rest)
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
