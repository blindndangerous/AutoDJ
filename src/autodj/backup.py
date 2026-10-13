"""Back up and restore the index and the data that exists only on this machine.

An archive is a plain ZIP file holding ``manifest.json`` (the AutoDJ version
that wrote it and the list of files) and these members:

- ``index/index-manifest.json`` and the two generation files it names
  (``tracks.gN.db`` and ``vectors.gN.index``): the live index generation.
- ``index/dj_meta.db``, ``index/library.db``, ``index/web_state.json``,
  ``index/related-artists.toml``, ``index/related-tracks.toml``, ``index/schedule.json``,
  ``index/carts.json``,
  ``index/time_checks.json``, ``index/listener_bans.json``, ``index/recording.json``,
  ``index/categories.json`` and ``index/liner_dates.json`` from the active index folder.
- ``liners/...``, ``carts/...``, ``time_checks/...``, ``profiles/...`` and
  ``clockwheels/...``: every file in those folders and their subfolders.  Time check
  voice packs kept in subfolders of the ``time_checks`` folder come along; packs listed
  from other folders do not, as they are external libraries like the music.
- ``stereo-tool-presets/...``: the uploaded ``.sts`` presets.
- ``stations/<slug>/index/<file>`` and ``stations/<slug>/<folder>/...`` for every
  other station in the configuration (``[stations.<slug>]``): the same state files
  and folders as the default station's, from its own folder.  A station folder
  that is the default station's (a shared liner folder, say) is stored once, under
  its default name.  ``manifest.json`` lists those stations under ``"stations"``,
  and restore refuses a station the configuration does not define.

Broadcast recordings are archives, not station state: they can run to many
gigabytes and a restore would replace the whole folder, so they stay out.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from collections.abc import Iterator
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
INDEX_FILES = (
    "dj_meta.db",
    "library.db",
    "web_state.json",
    "related-artists.toml",
    "related-tracks.toml",
    "schedule.json",
    "carts.json",
    "time_checks.json",
    "listener_bans.json",
    "recording.json",
    "categories.json",
    "liner_dates.json",
    im.MANIFEST_NAME,
)
_FOLDERS = ("liners", "carts", "time_checks", "profiles", "clockwheels", "stereo-tool-presets")
# The default station's state files in the active index folder.
_STATE_FILES = (
    "web_state.json",
    "schedule.json",
    "carts.json",
    "time_checks.json",
    "listener_bans.json",
    "recording.json",
    "categories.json",
    "liner_dates.json",
)
# Another station's state files, in ``stations/<slug>/``.  The categories
# are shared, so they are only in the default station's list.
STATION_FILES = tuple(name for name in _STATE_FILES if name != "categories.json")
STATION_FOLDERS = ("liners", "carts", "time_checks", "profiles", "clockwheels")


class BackupError(RuntimeError):
    """Raised when a backup cannot be written or an archive cannot be restored."""


def _within_root(path: Path, root: Path) -> None:
    """Do not silently copy linked files from outside a configured backup root."""
    if not path.resolve().is_relative_to(root.resolve()):
        raise BackupError(
            f"{path} points outside backup folder {root}; replace that linked path with a real file or folder"
        )


def _single_link(path: Path) -> None:
    """Hard-link aliases cannot be checked for containment without scanning the volume."""
    if path.stat().st_nlink > 1:
        raise BackupError(f"hard-linked backup file {path}; use a separate file copy instead")


def _folder_files(root: Path) -> Iterator[Path]:
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in folders:
            path = Path(directory) / name
            if path.is_symlink() or path.is_junction():
                raise BackupError(f"linked backup folder {path}; use a real folder instead")
        for name in files:
            path = Path(directory) / name
            _within_root(path, root)
            if path.is_file():
                yield path


def _roots(cfg: AutoDJConfig) -> dict[str, Path]:
    """Map each archive prefix to where it lives with this configuration.

    The default station's prefixes are the top-level names (``index``,
    ``liners``...); another station's are ``stations/<slug>/index`` and
    ``stations/<slug>/<folder>``.
    """
    from autodj.config import station_config
    from autodj.stations import StationPaths

    active = cfg.index.active_dir
    station = StationPaths.default(cfg)
    roots = {
        "index": active,
        "liners": station.liners,
        "carts": station.cart_uploads,
        "time_checks": station.time_check_clips,
        "profiles": station.profiles,
        "clockwheels": station.clockwheels,
        "stereo-tool-presets": active.parent / "stereo-tool-presets",
    }
    for spec in cfg.stations:
        paths = StationPaths.for_station(station_config(spec.cfg, cfg), spec.slug)
        prefix = f"stations/{spec.slug}"
        roots[f"{prefix}/index"] = paths.state_dir
        roots[f"{prefix}/liners"] = paths.liners
        roots[f"{prefix}/carts"] = paths.cart_uploads
        roots[f"{prefix}/time_checks"] = paths.time_check_clips
        roots[f"{prefix}/profiles"] = paths.profiles
        roots[f"{prefix}/clockwheels"] = paths.clockwheels
    return roots


def _is_files(key: str) -> bool:
    """Whether archive prefix *key* holds single state files, not a whole folder."""
    return key == "index" or key.endswith("/index")


def _split(name: str) -> tuple[str, tuple[str, ...]]:
    """Split a member name into its archive prefix and the path below it."""
    parts = PurePosixPath(name).parts
    if parts[0] == "stations" and len(parts) > 3:
        return "/".join(parts[:3]), parts[3:]
    return parts[0], parts[1:]


def _overlap(first: Path, second: Path) -> bool:
    """Whether two folders are the same folder or one holds the other."""
    first, second = first.resolve(), second.resolve()
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _folder_roots(roots: dict[str, Path]) -> dict[str, Path]:
    """The folders a backup takes whole, each once.

    Another station's folder that is, or sits inside, a folder already
    taken (a liner folder shared with the default station, say) is left
    out: its files are in the archive under that folder's name.  The
    default station's folders are all taken, as they always were.

    Raises:
        BackupError: A station folder holds a folder already taken, so
            restoring one would replace the other.
    """
    kept: dict[str, Path] = {}
    for key, root in roots.items():
        if _is_files(key):
            continue
        if not key.startswith("stations/"):
            kept[key] = root
            continue
        holder = next((other for other in kept if _overlap(root, kept[other])), None)
        if holder is None:
            kept[key] = root
        elif not root.resolve().is_relative_to(kept[holder].resolve()):
            raise BackupError(
                f"the {key} folder {root} holds the {holder} folder {kept[holder]}; "
                "give each station folders of its own, side by side"
            )
    return kept


def _snapshot_index(active: Path, snapshot: Path) -> None:
    """Copy the live index generation, its manifest and SQLite data into *snapshot*."""
    snapshot.mkdir(parents=True)
    for name in INDEX_FILES:
        path = active / name
        _within_root(path, active)
        if path.is_file():
            _single_link(path)
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
                _within_root(active / name, active)
                _single_link(active / name)
                shutil.copyfile(active / name, snapshot / name)
                if im.sha256_file(snapshot / name) != digest:
                    raise BackupError(f"{active / name} does not match its SHA-256 in the manifest")
            shutil.copyfile(active / im.MANIFEST_NAME, snapshot / im.MANIFEST_NAME)
    for name in ("dj_meta.db", "library.db"):
        database = active / name
        if not database.is_file():
            continue
        with (
            closing(sqlite3.connect(readonly_uri(database), uri=True)) as source,
            closing(sqlite3.connect(snapshot / name)) as target,
        ):
            source.backup(target)
        _check_sqlite(snapshot / name)
    for name in ("related-artists.toml", "related-tracks.toml"):
        related = active / name
        if related.is_file():
            shutil.copyfile(related, snapshot / name)


def _check_sqlite(path: Path) -> None:
    """Refuse a SQLite database that would replace a working local copy."""
    try:
        with closing(sqlite3.connect(readonly_uri(path), uri=True)) as conn:
            result = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    except sqlite3.Error as exc:
        raise BackupError(f"{path} failed SQLite integrity_check: {exc}") from exc
    finally:
        for suffix in ("-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
    if result != ["ok"]:
        raise BackupError(f"{path} failed SQLite integrity_check: {'; '.join(map(str, result))}")


def create_backup(cfg: AutoDJConfig, destination: Path, *, force: bool) -> Path:
    """Write a backup archive to *destination* and return its absolute path.

    Safe while AutoDJ serves: the index is copied under its publication lock
    and checked against its manifest, and SQLite databases are copied through
    SQLite's backup API.

    Raises:
        BackupError: If *destination* exists without *force*, or the index is
            old, damaged or unreadable.
    """
    destination = destination.expanduser().resolve()
    if destination.exists() and not force:
        raise BackupError(f"{destination} already exists; pass --force to replace it")
    roots = _roots(cfg)
    folders = _folder_roots(roots)
    for key, root in folders.items():
        if destination.is_relative_to(root.resolve()):
            raise BackupError(f"write the backup outside the {key} folder {root}")
    partial = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tempfile.TemporaryDirectory(prefix="autodj-backup-") as temp:
            snapshot = Path(temp) / "index"
            _snapshot_index(roots["index"], snapshot)
            members: dict[str, Path] = {}
            state_files = [roots["index"] / name for name in _STATE_FILES]
            for path in [*sorted(snapshot.glob("*")), *state_files]:
                if path.is_file():
                    members[f"index/{path.name}"] = path
            for spec in cfg.stations:
                key = f"stations/{spec.slug}/index"
                for name in STATION_FILES:
                    path = roots[key] / name
                    _within_root(path, roots[key])
                    if path.is_file():
                        members[f"{key}/{name}"] = path
            for key, root in folders.items():
                paths = root.glob("*.sts") if key == "stereo-tool-presets" else _folder_files(root)
                for path in sorted(paths):
                    if path.is_file() and (key != "stereo-tool-presets" or not path.is_symlink()):
                        members[f"{key}/{path.relative_to(root).as_posix()}"] = path
            with partial.open("xb") as handle:
                with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
                    for name, path in members.items():
                        _single_link(path)
                        archive.write(path, name)
                    manifest = {
                        "autodj_version": current_version(),
                        "files": list(members),
                        "stations": [spec.slug for spec in cfg.stations],
                    }
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


def _read_file_list(archive: zipfile.ZipFile) -> tuple[list[str], list[str]]:
    """Return the file list and stations of a backup made by this major.minor version.

    An archive made before stations has no ``"stations"`` list: only the
    default station.
    """
    from autodj.stations import DEFAULT_SLUG, SLUG_PATTERN

    try:
        manifest = json.loads(archive.read("manifest.json"))
        version, files = manifest["autodj_version"], manifest["files"]
        stations = manifest.get("stations", [])
        if not isinstance(version, str) or not isinstance(files, list):
            raise TypeError("wrong field types")
        if not all(isinstance(name, str) for name in files):
            raise TypeError("wrong field types")
        if not isinstance(stations, list) or not all(isinstance(s, str) for s in stations):
            raise TypeError("wrong field types")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise BackupError(f"the archive has no valid manifest.json: {exc!r}") from exc
    for slug in stations:
        if slug == DEFAULT_SLUG or not SLUG_PATTERN.match(slug):
            raise BackupError(f"unsafe station name in archive: {slug!r}")
    running = current_version()
    if version.split(".")[:2] != running.split(".")[:2]:
        raise BackupError(
            f"this backup was made by AutoDJ {version}; AutoDJ {running} only restores "
            "backups made by the same major and minor version"
        )
    return list(files), list(stations)


def _unknown_station(slug: str) -> BackupError:
    """The refusal for a station the configuration does not define."""
    return BackupError(
        f"this backup holds station {slug}, which the configuration does not define; "
        f"add [stations.{slug}] to config.toml first"
    )


def _target(roots: dict[str, Path], name: str) -> Path:
    """Return where archive member *name* is restored, refusing unsafe or unknown names."""
    from autodj.stations import DEFAULT_SLUG, SLUG_PATTERN

    parts = PurePosixPath(name).parts
    if not parts or "/".join(parts) != name or ".." in parts or "\\" in name or ":" in name:
        raise BackupError(f"unsafe file name in archive: {name!r}")
    if parts[0] == "stations" and len(parts) > 1:
        slug = parts[1]
        if slug == DEFAULT_SLUG or not SLUG_PATTERN.match(slug):
            raise BackupError(f"unsafe station name in archive: {name!r}")
        if f"stations/{slug}/index" not in roots:
            raise _unknown_station(slug)
    key, rest = _split(name)
    if key == "index":
        if len(rest) == 1 and (rest[0] in INDEX_FILES or im.GENERATION_FILE_RE.match(rest[0])):
            return roots["index"] / rest[0]
    elif _is_files(key):
        if len(rest) == 1 and rest[0] in STATION_FILES:
            return roots[key] / rest[0]
    elif key in roots and rest and (key in _FOLDERS or key.split("/")[-1] in STATION_FOLDERS):
        return roots[key].joinpath(*rest)
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


def _install(key: str, root: Path, stage: Path) -> None:
    """Move one staged part of a backup into place and drop what it supersedes."""
    if _is_files(key):
        root.mkdir(parents=True, exist_ok=True)
        restored = sorted(path.name for path in stage.iterdir())
        for name in restored:
            for suffix in ("-wal", "-shm"):
                (root / f"{name}{suffix}").unlink(missing_ok=True)
        # The manifest goes last, so it only names files that are in place.
        for name in sorted(restored, key=lambda name: name == im.MANIFEST_NAME):
            os.replace(stage / name, root / name)
        if key == "index" and im.MANIFEST_NAME in restored:
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
    fsync_directory(root if _is_files(key) else root.parent)


def _restore(archive: zipfile.ZipFile, roots: dict[str, Path], *, force: bool) -> int:
    """Check, unpack and install *archive*; return the number of files restored."""
    files, stations = _read_file_list(archive)
    for slug in stations:
        if f"stations/{slug}/index" not in roots:
            raise _unknown_station(slug)
    targets = {name: _target(roots, name) for name in files}
    if sorted(files) != sorted(name for name in archive.namelist() if name != "manifest.json"):
        raise BackupError("the archive's files do not match its manifest.json")
    for name, target_path in targets.items():
        _within_root(target_path, roots[_split(name)[0]])
        if stat.S_ISLNK(archive.getinfo(name).external_attr >> 16):
            raise BackupError(f"linked file in archive: {name}")
    keys = list(dict.fromkeys(_split(name)[0] for name in files))
    folders = [key for key in keys if not _is_files(key)]
    for at, key in enumerate(folders):
        for other in folders[at + 1 :]:
            if other.startswith("stations/") and _overlap(roots[key], roots[other]):
                raise BackupError(
                    f"the backup's {key} and {other} folders would both restore into "
                    f"{roots[key]}; give each station folders of its own"
                )
    replaced = [targets[name] for name in files if _is_files(_split(name)[0])]
    replaced += [roots[key] for key in folders]
    existing = [path for path in replaced if path.exists()]
    if existing and not force:
        raise BackupError(f"{existing[0]} already exists; pass --force to replace it")
    stages = {
        key: roots[key].with_name(f".{roots[key].name}.restore-{uuid.uuid4().hex}") for key in keys
    }
    with im.publication_lock(roots["index"]):
        try:
            for name in files:
                key, rest = _split(name)
                destination = stages[key].joinpath(*rest)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target)
            if "index" in stages:
                _check_index(stages["index"])
                for name in ("dj_meta.db", "library.db"):
                    database = stages["index"] / name
                    if database.is_file():
                        _check_sqlite(database)
            for key, stage in stages.items():
                _install(key, roots[key], stage)
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
    Another station's files go back to its own folder; a station the
    configuration does not define is refused before anything is unpacked.

    Raises:
        BackupError: If the archive is invalid, from another version, holds an
            old-format index or a station the configuration lacks, or would
            replace existing files without *force*.
    """
    try:
        with zipfile.ZipFile(archive) as opened:
            return _restore(opened, _roots(cfg), force=force)
    except BackupError:
        raise
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
        raise BackupError(f"could not restore {archive}: {exc}") from exc
