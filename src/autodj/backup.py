"""Back up and restore the index and the data that exists only on this machine.

An archive is a plain ZIP file holding ``manifest.json`` (the AutoDJ version
that wrote it and the list of files) and these members:

- ``index/index-manifest.json`` and the two generation files it names
  (``tracks.gN.db`` and ``vectors.gN.index``): the live index generation.
- ``index/dj_meta.db``, ``index/library.db``, ``index/web_state.json``,
  ``index/related-artists.toml``, ``index/related-tracks.toml``, ``index/schedule.json``,
  ``index/carts.json``,
  ``index/time_checks.json``, ``index/listener_bans.json``, ``index/recording.json``,
  ``index/categories.json``, ``index/liner_dates.json`` and ``index/matched_intros.json``
  from the active index folder.  ``relays.json``, which holds relay source
  passwords, is never backed up (:data:`SECRET_STATE_FILES`).
- ``liners/...``, ``carts/...``, ``time_checks/...``, ``profiles/...``, ``clockwheels/...``
  and ``intros/...`` (the matched intros' audio): every file in those folders and their
  subfolders.  Time check
  voice packs kept in subfolders of the ``time_checks`` folder come along; packs listed
  from other folders do not, as they are external libraries like the music.
- ``stereo-tool-presets/...``: the uploaded ``.sts`` presets.
- ``stations/<slug>/index/<file>`` and ``stations/<slug>/<folder>/...`` for every
  other station in the configuration (``[stations.<slug>]``): the same state files
  and folders as the default station's, from its own folder.  A station folder
  that is the default station's (a shared liner folder, say) is stored once, under
  its default name, and ``"shared"`` in the manifest says so.  ``manifest.json``
  lists those stations under ``"stations"``, and restore refuses a station the
  configuration does not define.

Every station's state files are copied with the index generation under its
publication lock; each SQLite database is copied through SQLite's backup API.
A backup holds no secrets: relay settings stay out of it, and a restore skips
them in an archive that has them, leaving the station's own file in place.
Restore is all or nothing: what it replaces is set aside first
(``.<name>.old-<id>``), each step written to a journal (``.autodj-restore.json``),
and put back if any replacement fails; after a crash, :func:`recover_restore`
rolls it back.  Restore refuses while ``autodj serve`` holds its lock.

Broadcast recordings are archives, not station state: they can run to many
gigabytes and a restore would replace the whole folder, so they stay out.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from collections.abc import Callable, Iterable
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from filelock import FileLock, Timeout

from autodj import index_manifest as im
from autodj.fsutil import atomic_write, fsync_directory
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
    "matched_intros.json",
    im.MANIFEST_NAME,
)
#: State files holding secrets (relay source passwords).  A backup never
#: holds them, and a restore skips them in an archive that does (one made by
#: a build that included them), so the station keeps its own file.
SECRET_STATE_FILES = frozenset({"relays.json"})
_FOLDERS = (
    "liners",
    "carts",
    "time_checks",
    "profiles",
    "clockwheels",
    "intros",
    "stereo-tool-presets",
)
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
    "matched_intros.json",
)
# Another station's state files, in ``stations/<slug>/``.  The categories
# are shared, so they are only in the default station's list.
STATION_FILES = tuple(name for name in _STATE_FILES if name != "categories.json")
STATION_FOLDERS = ("liners", "carts", "time_checks", "profiles", "clockwheels", "intros")
# Restore refuses an archive that unpacks to more than this, and keeps this
# much free beside what it unpacks.
MAX_RESTORE_BYTES = 256 * 2**30
RESTORE_SPACE_MARGIN = 64 * 2**20


class BackupError(RuntimeError):
    """Raised when a backup cannot be written or an archive cannot be restored."""


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
        "intros": station.intros,
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
        roots[f"{prefix}/intros"] = paths.intros
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


def _swallows(folder: Path, other: Path) -> bool:
    """Whether replacing *folder* whole would also replace *other*."""
    folder, other = folder.resolve(), other.resolve()
    return other == folder or other.is_relative_to(folder)


def _check_layout(roots: dict[str, Path], keys: Iterable[str]) -> None:
    """Refuse folders that replacing *keys* whole would replace something else with.

    A folder may not be, or hold, any other folder or state folder, and may
    not sit in another folder.  It may sit in a state folder, which only
    has its named files replaced.

    Raises:
        BackupError: Naming both folders.
    """
    keys = set(keys)
    for key in keys:
        if _is_files(key):
            continue
        for other, root in roots.items():
            if other == key:
                continue
            # A folder the configuration gives two stations is restored once
            # for both; two archive folders landing on it are refused below.
            if (
                not _is_files(other)
                and other not in keys
                and root.resolve() == roots[key].resolve()
            ):
                continue
            if _swallows(roots[key], root) or (
                not _is_files(other) and _swallows(root, roots[key])
            ):
                raise BackupError(
                    f"the {key} folder {roots[key]} and the {other} folder {root} overlap, so "
                    "restoring one would replace the other; give each its own folder, side by side"
                )


def _folder_roots(roots: dict[str, Path]) -> tuple[dict[str, Path], dict[str, str]]:
    """The folders a backup takes whole, each once, and the ones stored under another name.

    Another station's folder that is the same folder as one already taken
    (a liner folder shared with the default station, say) is stored once:
    its files are in the archive under that folder's name, and the second
    mapping says so (``{"stations/nightowl/liners": "liners"}``).

    Raises:
        BackupError: Two folders overlap in any other way, or a folder holds
            a state folder.
    """
    kept: dict[str, Path] = {}
    shared: dict[str, str] = {}
    for key, root in roots.items():
        if _is_files(key):
            continue
        same = next((other for other in kept if kept[other].resolve() == root.resolve()), None)
        if same is not None:
            shared[key] = same
            continue
        kept[key] = root
    _check_layout({**{k: v for k, v in roots.items() if _is_files(k)}, **kept}, kept)
    return kept, shared


def _is_link(path: Path) -> bool:
    """Whether *path* is a symbolic link or a Windows junction."""
    return path.is_symlink() or path.is_junction()


def _within_root(path: Path, root: Path) -> None:
    """Refuse a file that a link takes outside its configured folder (#193).

    A link to another file in the same folder is fine; it is read as the
    file it points to.

    Raises:
        BackupError: Naming the link and the folder.
    """
    if not path.resolve().is_relative_to(root.resolve()):
        raise BackupError(
            f"{path} points outside backup folder {root}; replace that linked path with a real file or folder"
        )


def _single_link(path: Path) -> None:
    """Refuse hard-link aliases, whose other name cannot be checked for containment."""
    if path.stat().st_nlink > 1:
        raise BackupError(f"hard-linked backup file {path}; use a separate file copy instead")


# Temporary and set-aside names AutoDJ itself makes beside real files: an
# atomic write's ``.<name>.<hex>.tmp``, a restore's ``.<name>.restore-<hex>``
# stage and ``.<name>.old-<hex>`` aside.  Never data to back up.
_SCRATCH_NAME = re.compile(r"^\..+\.(?:[0-9a-f]{32}\.tmp|(?:old|restore)-[0-9a-f]{32})$")
# SQLite's own files beside a database: a hot rollback journal or a WAL
# left beside a restored database would be replayed into it.
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def _is_scratch(name: str) -> bool:
    """Whether *name* is one of AutoDJ's own temporary or set-aside names."""
    return _SCRATCH_NAME.match(name) is not None


def _folder_files(root: Path, pattern: str = "**/*") -> list[Path]:
    """The files a backup takes from folder *root*, refusing links that leave it.

    A linked subfolder (a symbolic link or a Windows junction) is refused,
    and so is a file link that points outside *root*.  Uploaded Stereo Tool
    presets that are links stay out, as before.  AutoDJ's own temporary and
    set-aside files are skipped.

    Raises:
        BackupError: A link leaves the folder.
    """
    if not root.is_dir():
        return []
    if pattern == "*.sts":
        found = [
            path
            for path in sorted(root.glob(pattern))
            if path.is_file() and not _is_link(path) and not _is_scratch(path.name)
        ]
        for path in found:
            _single_link(path)
        return found
    found = []
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = [name for name in folders if not _is_scratch(name)]
        for name in folders:
            path = Path(directory) / name
            if _is_link(path):
                raise BackupError(f"linked backup folder {path}; use a real folder instead")
        for name in files:
            if _is_scratch(name):
                continue
            path = Path(directory) / name
            _within_root(path, root)
            if path.is_file():
                _single_link(path)
                found.append(path)
    return sorted(found)


def _state_sources(cfg: AutoDJConfig, roots: dict[str, Path]) -> list[tuple[str, Path]]:
    """Every station's state files that exist, by archive name, checked for links."""
    state = [("index", name) for name in _STATE_FILES]
    state += [
        (f"stations/{spec.slug}/index", name) for spec in cfg.stations for name in STATION_FILES
    ]
    sources = []
    for key, name in state:
        original = roots[key] / name
        if not original.exists():
            continue
        _within_root(original, roots[key])
        if not original.is_file():
            continue
        _single_link(original)
        sources.append((f"{key}/{name}", original))
    return sources


def _snapshot(cfg: AutoDJConfig, roots: dict[str, Path], snapshot: Path) -> dict[str, Path]:
    """Copy the index and every station's state files into *snapshot*.

    The index generation, its manifest and the state files are copied under
    the index's publication lock, so they belong together; the copies are
    checked against the manifest's digests after the lock is released, so
    hashing a large generation never holds up indexing.  Each SQLite
    database is copied through SQLite's backup API, which gives a
    consistent copy of it, a moment after the other files.  Returns the
    archive members, by name.
    """
    active = roots["index"]
    index = snapshot / "index"
    index.mkdir(parents=True)
    members: dict[str, Path] = {}
    digests: list[tuple[str, str]] = []
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
                shutil.copyfile(active / name, index / name)
                digests.append((name, digest))
            manifest_path = active / im.MANIFEST_NAME
            _within_root(manifest_path, active)
            _single_link(manifest_path)
            shutil.copyfile(manifest_path, index / im.MANIFEST_NAME)
        for member, original in _state_sources(cfg, roots):
            copy = snapshot / member
            copy.parent.mkdir(parents=True, exist_ok=True)
            # With its permission bits, so a private file is restored private.
            shutil.copy(original, copy)
            members[member] = copy
    for name, digest in digests:
        if im.sha256_file(index / name) != digest:
            raise BackupError(f"{active / name} does not match its SHA-256 in the manifest")
    for name in ("dj_meta.db", "library.db", "related-artists.toml", "related-tracks.toml"):
        if (active / name).exists():
            _within_root(active / name, active)
            _single_link(active / name)
    for name in ("dj_meta.db", "library.db"):
        database = active / name
        if not database.is_file():
            continue
        for suffix in _SQLITE_SIDECARS:
            sidecar = database.with_name(database.name + suffix)
            if sidecar.exists():
                _within_root(sidecar, active)
                _single_link(sidecar)
        with (
            closing(sqlite3.connect(readonly_uri(database), uri=True)) as source,
            closing(sqlite3.connect(index / name)) as target,
        ):
            source.backup(target)
        _check_sqlite(index / name)
    for name in ("related-artists.toml", "related-tracks.toml"):
        related = active / name
        if related.is_file():
            _single_link(related)
            shutil.copyfile(related, index / name)
    for path in sorted(index.iterdir()):
        members.setdefault(f"index/{path.name}", path)
    return dict(
        sorted(members.items(), key=lambda item: (item[0].startswith("stations/"), item[0]))
    )


def _check_sqlite(path: Path) -> None:
    """Refuse a SQLite database that would replace a working local copy."""
    try:
        with closing(sqlite3.connect(readonly_uri(path), uri=True)) as conn:
            result = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    except sqlite3.Error as exc:
        raise BackupError(f"{path} failed SQLite integrity_check: {exc}") from exc
    finally:
        for suffix in _SQLITE_SIDECARS:
            path.with_name(path.name + suffix).unlink(missing_ok=True)
    if result != ["ok"]:
        raise BackupError(f"{path} failed SQLite integrity_check: {'; '.join(map(str, result))}")


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _check_backup_space(
    roots: dict[str, Path], folder_files: list[Path], temp: Path, destination: Path
) -> None:
    """Refuse a backup that cannot fit: the snapshot in *temp*, the archive beside *destination*.

    The snapshot holds the index generation, the databases and the state
    files; the archive at most everything, uncompressed.
    """
    active = roots["index"]
    snapshot = (
        sum(_file_size(path) for path in active.iterdir() if path.is_file())
        if active.is_dir()
        else 0
    )
    for key, root in roots.items():
        if key != "index" and _is_files(key) and root.is_dir():
            snapshot += sum(_file_size(root / name) for name in STATION_FILES)
    total = snapshot + sum(_file_size(path) for path in folder_files)
    for place, size, what in (
        (_nearest_existing(temp), snapshot, "the snapshot"),
        (_nearest_existing(destination.parent), total, "the archive"),
    ):
        free = shutil.disk_usage(place).free
        if size + RESTORE_SPACE_MARGIN > free:
            raise BackupError(
                f"not enough free space for {what}: up to {size / 2**20:.0f} MiB in {place}, "
                f"with {free / 2**20:.0f} MiB free"
            )


def create_backup(cfg: AutoDJConfig, destination: Path, *, force: bool) -> Path:
    """Write a backup archive to *destination* and return its absolute path.

    Safe while AutoDJ serves: the index generation and every station's state
    files are copied together under the index's publication lock, the index
    checked against its manifest, and each SQLite database copied through
    SQLite's backup API.  The free space for the snapshot and the archive is
    checked first.

    Raises:
        BackupError: If *destination* exists without *force*, the index is
            old, damaged or unreadable, two folders overlap, a link leaves its
            folder, or there is not enough free space.
    """
    destination = destination.expanduser().resolve()
    if destination.exists() and not force:
        raise BackupError(f"{destination} already exists; pass --force to replace it")
    roots = _roots(cfg)
    folders, shared = _folder_roots(roots)
    for key, root in folders.items():
        if destination.is_relative_to(root.resolve()):
            raise BackupError(f"write the backup outside the {key} folder {root}")
    folder_members: dict[str, Path] = {}
    for key, root in folders.items():
        pattern = "*.sts" if key == "stereo-tool-presets" else "**/*"
        for path in _folder_files(root, pattern):
            folder_members[f"{key}/{path.relative_to(root).as_posix()}"] = path
    _check_backup_space(
        roots, list(folder_members.values()), Path(tempfile.gettempdir()), destination
    )
    partial = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tempfile.TemporaryDirectory(prefix="autodj-backup-") as temp:
            members = _snapshot(cfg, roots, Path(temp))
            members.update(folder_members)
            with partial.open("xb") as handle:
                with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
                    for name, path in members.items():
                        archive.write(path, name)
                    manifest = {
                        "autodj_version": current_version(),
                        "files": list(members),
                        "stations": [spec.slug for spec in cfg.stations],
                        "shared": shared,
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


@dataclass(frozen=True)
class _Manifest:
    files: list[str]
    stations: list[str]
    shared: dict[str, str]


def _read_manifest(archive: zipfile.ZipFile) -> _Manifest:
    """Return the manifest of a backup made by this major.minor version.

    An archive made before stations has no ``"stations"`` list and no
    ``"shared"`` map: only the default station.
    """
    from autodj.stations import DEFAULT_SLUG, SLUG_PATTERN

    try:
        manifest = json.loads(archive.read("manifest.json"))
        version, files = manifest["autodj_version"], manifest["files"]
        stations = manifest.get("stations", [])
        shared = manifest.get("shared", {})
        if not isinstance(version, str) or not isinstance(files, list):
            raise TypeError("wrong field types")
        if not all(isinstance(name, str) for name in files):
            raise TypeError("wrong field types")
        if not isinstance(stations, list) or not all(isinstance(s, str) for s in stations):
            raise TypeError("wrong field types")
        if not isinstance(shared, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in shared.items()
        ):
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
    return _Manifest(list(files), list(stations), dict(shared))


def _unknown_station(slug: str) -> BackupError:
    """The refusal for a station the configuration does not define."""
    return BackupError(
        f"this backup holds station {slug}, which the configuration does not define; "
        f"add [stations.{slug}] to config.toml first"
    )


# Names Windows gives to devices: a file called NUL or com1.txt cannot be
# made there, and opening one reaches the device.
_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
    | {f"{kind}{n}" for kind in ("COM", "LPT") for n in "123456789\u00b9\u00b2\u00b3"}
)
# Characters Windows refuses in a file name.
_WINDOWS_FORBIDDEN = frozenset('*?"<>|')


def _unsafe_part(part: str) -> bool:
    """Whether one path segment cannot be written safely on every system."""
    if part.endswith((".", " ")) or any(ord(char) < 32 for char in part):
        return True
    if any(char in _WINDOWS_FORBIDDEN for char in part):
        return True
    return part.split(".", 1)[0].rstrip(" ").upper() in _DEVICE_NAMES


def _is_secret(name: str) -> bool:
    """Whether archive member *name* is a station's secret state file."""
    key, rest = _split(name)
    return _is_files(key) and len(rest) == 1 and rest[0] in SECRET_STATE_FILES


def _target(roots: dict[str, Path], name: str) -> Path:
    """Return where archive member *name* is restored, refusing unsafe or unknown names."""
    from autodj.stations import DEFAULT_SLUG, SLUG_PATTERN

    parts = PurePosixPath(name).parts
    if (
        not parts
        or "/".join(parts) != name
        or ".." in parts
        or "\\" in name
        or ":" in name
        or any(_unsafe_part(part) for part in parts)
    ):
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


def _nearest_existing(path: Path) -> Path:
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _check_space(archive: zipfile.ZipFile, files: list[str], roots: dict[str, Path]) -> None:
    """Refuse an archive larger than the cap, or than the free space where it unpacks.

    The sizes are the ones the archive declares; reading a member stops at
    its declared size, so a member cannot unpack to more.
    """
    infos = {info.filename: info for info in archive.infolist()}
    total = sum(infos[name].file_size for name in files)
    if total > MAX_RESTORE_BYTES:
        raise BackupError(
            f"this backup unpacks to {total / 2**30:.1f} GiB, more than the "
            f"{MAX_RESTORE_BYTES / 2**30:.0f} GiB AutoDJ restores; it is not an AutoDJ backup"
        )
    needed: dict[int, tuple[Path, int]] = {}
    for name in files:
        base = _nearest_existing(roots[_split(name)[0]].parent)
        device = base.stat().st_dev
        place, size = needed.get(device, (base, 0))
        needed[device] = (place, size + infos[name].file_size)
    for place, size in needed.values():
        free = shutil.disk_usage(place).free
        if size + RESTORE_SPACE_MARGIN > free:
            raise BackupError(
                f"not enough free space to restore: {size / 2**20:.0f} MiB to unpack beside "
                f"{place}, with {free / 2**20:.0f} MiB free"
            )


# ---------------------------------------------------------------------------
# A restore's journal, so a crash part way can be rolled back or finished
# ---------------------------------------------------------------------------

#: Beside the index, while a restore replaces files.
JOURNAL_NAME = ".autodj-restore.json"
#: Held by ``autodj serve`` for each index folder it serves; restore refuses while it is held.
SERVE_LOCK_NAME = ".autodj-serve.lock"


def _write_journal(path: Path, journal: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(journal, indent=2) + "\n", mode=0o600)


def _remove(path: Path) -> None:
    """Remove a file, folder, link or junction; a link is removed as a link."""
    if _is_link(path):
        if path.is_junction() or (path.is_dir() and os.name == "nt"):
            os.rmdir(path)
        else:
            path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


class _Swap:
    """Every replacement a restore makes, so a failure puts everything back.

    Each step is a rename within one folder, written to the journal before
    it is made.  Until :meth:`commit`, every old file and folder is kept
    aside under a hidden name; :meth:`undo` reverses the steps newest first.
    If the process dies part way, the journal lets :func:`recover_restore`
    finish the undo.
    """

    def __init__(self, journal: Path, stages: Iterable[Path]) -> None:
        self._journal_path = journal
        self._journal: dict[str, Any] = {
            "version": 1,
            "state": "installing",
            "stages": [str(stage) for stage in stages],
            "steps": [],
            "leftovers": [],
        }
        self._undo: list[tuple[dict[str, str], Callable[[], None]]] = []
        self._aside: list[Path] = []
        self._dirs: set[Path] = set()

    def start(self) -> None:
        """Write the journal before the first replacement."""
        _write_journal(self._journal_path, self._journal)

    def _log(self, step: dict[str, str]) -> None:
        self._journal["steps"].append(step)
        _write_journal(self._journal_path, self._journal)

    def move_aside(self, path: Path) -> None:
        """Move *path* out of the way, to be deleted after a commit or put back."""
        if not path.exists() and not path.is_symlink():
            return
        aside = path.with_name(f".{path.name}.old-{uuid.uuid4().hex}")
        step = {"op": "aside", "path": str(path), "aside": str(aside)}
        self._log(step)
        os.replace(path, aside)
        self._aside.append(aside)
        self._dirs.add(path.parent)
        self._undo.append((step, lambda: os.replace(aside, path)))

    def put(self, source: Path, target: Path) -> None:
        """Move *source* to *target*, where nothing is now."""
        step = {"op": "put", "source": str(source), "target": str(target)}
        self._log(step)
        os.replace(source, target)
        self._dirs.add(target.parent)
        self._undo.append((step, lambda: os.replace(target, source)))

    def make_dir(self, path: Path) -> None:
        """Make folder *path* (and its parents) if it is missing."""
        missing = []
        for folder in (path, *path.parents):
            if folder.exists():
                break
            missing.append(folder)
        # Outermost first, so the undo, newest first, removes the innermost first.
        for folder in reversed(missing):
            step = {"op": "mkdir", "path": str(folder)}
            self._log(step)
            folder.mkdir()
            self._undo.append((step, folder.rmdir))

    def undo(self) -> list[str]:
        """Put everything back, newest step first; return what is not back, in words.

        The journal stays when anything is left, so doctor can finish.
        """
        problems = []
        for step, action in reversed(self._undo):
            try:
                action()
            except OSError as exc:
                if step["op"] == "aside":
                    problems.append(f"{step['path']} is still at {step['aside']} ({exc})")
                elif step["op"] == "put":
                    problems.append(f"{step['target']} holds the backup's copy ({exc})")
                else:
                    problems.append(f"the new folder {step['path']} is still there ({exc})")
        self._undo.clear()
        if not problems:
            self._journal_path.unlink(missing_ok=True)
        return problems

    def installed(self) -> None:
        """Note in the journal that every new file is in place."""
        self._journal["state"] = "installed"
        _write_journal(self._journal_path, self._journal)

    def commit(self) -> list[Path]:
        """Keep the new files: delete what was set aside; return what could not be deleted.

        The restore has succeeded by now, so a file that cannot be deleted
        is only a leftover: it stays named in the journal for doctor.
        """
        self._undo.clear()
        leftovers = []
        for aside in self._aside:
            try:
                _remove(aside)
            except OSError:
                leftovers.append(aside)
        for folder in self._dirs:
            # Flushing is best effort once the files are in place.
            with suppress(OSError):
                if folder.is_dir():
                    fsync_directory(folder)
        if leftovers:
            self._journal["leftovers"] = [str(path) for path in leftovers]
            # If even this fails, the stray names still show in doctor's scan.
            with suppress(OSError):
                _write_journal(self._journal_path, self._journal)
        else:
            self._journal_path.unlink(missing_ok=True)
        return leftovers


def _install(
    swap: _Swap, files: list[str], roots: dict[str, Path], stages: dict[str, Path]
) -> None:
    """Replace what the archive holds: first move every old file and folder aside, then put the new ones in."""
    keys = list(stages)
    folder_keys = [key for key in keys if not _is_files(key)]
    file_names = {
        key: sorted(path.name for path in stages[key].iterdir()) for key in keys if _is_files(key)
    }
    swap.start()
    for key in folder_keys:
        swap.move_aside(roots[key])
    for key, names in file_names.items():
        root = roots[key]
        for name in names:
            for suffix in (*_SQLITE_SIDECARS, ""):
                swap.move_aside(root / f"{name}{suffix}")
        if key == "index" and im.MANIFEST_NAME in names and root.is_dir():
            # The backup's generation replaces every other one.
            for path in sorted(root.iterdir()):
                if im.GENERATION_FILE_RE.match(path.name) and path.name not in names:
                    swap.move_aside(path)
    for key in folder_keys:
        swap.make_dir(roots[key].parent)
        swap.put(stages[key], roots[key])
    for key, names in file_names.items():
        swap.make_dir(roots[key])
        # The manifest goes last, so it only names files that are in place.
        for name in sorted(names, key=lambda name: name == im.MANIFEST_NAME):
            swap.put(stages[key] / name, roots[key] / name)
    swap.installed()


def server_running(index_dir: Path) -> bool:
    """Whether an ``autodj serve`` holds the serve lock of *index_dir*."""
    lock_path = index_dir / SERVE_LOCK_NAME
    if not lock_path.exists():
        return False
    lock = FileLock(lock_path, timeout=0)
    try:
        lock.acquire()
    except Timeout:
        return True
    lock.release()
    return False


def hold_serve_lock(index_dir: Path) -> FileLock:
    """Take the serve lock of *index_dir* for as long as the server runs.

    Restore refuses while it is held.  The operating system drops it when
    the process ends, however it ends.

    Raises:
        BackupError: Another process holds it: AutoDJ already serves this
            index folder.
    """
    index_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(index_dir / SERVE_LOCK_NAME, timeout=0)
    try:
        lock.acquire()
    except Timeout as exc:
        raise BackupError(f"another AutoDJ is already serving {index_dir}; stop it first") from exc
    return lock


def _stage_member(archive: zipfile.ZipFile, name: str, destination: Path) -> None:
    """Unpack member *name* to *destination*, keeping its saved permission bits on POSIX."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(name) as source, destination.open("xb") as target:
        shutil.copyfileobj(source, target)
    mode = (archive.getinfo(name).external_attr >> 16) & 0o666
    if os.name != "nt" and mode:
        # A file kept private (0600) comes back private.
        os.chmod(destination, mode)


def _restore(
    archive: zipfile.ZipFile, roots: dict[str, Path], *, force: bool, warnings: list[str]
) -> int:
    """Check, unpack and install *archive*; return the number of files restored.

    Nothing is replaced until every file is unpacked and checked, and if a
    replacement fails, every one already made is undone.
    """
    if server_running(roots["index"]):
        raise BackupError(
            f"AutoDJ is serving {roots['index']}; stop it before restoring, so it does not keep "
            "using the files the restore replaces"
        )
    pending = _read_journal(roots["index"])
    if pending is not None:
        raise BackupError(
            "an earlier restore did not finish; run `autodj doctor` to put the old files back "
            "or finish it before restoring again"
        )
    manifest = _read_manifest(archive)
    files = [name for name in manifest.files if not _is_secret(name)]
    if len(files) != len(manifest.files):
        warnings.append(
            "Relay settings in this backup were skipped; each station keeps its own relays.json."
        )
    for slug in manifest.stations:
        if f"stations/{slug}/index" not in roots:
            raise _unknown_station(slug)
    targets = {name: _target(roots, name) for name in files}
    if sorted(manifest.files) != sorted(
        name for name in archive.namelist() if name != "manifest.json"
    ):
        raise BackupError("the archive's files do not match its manifest.json")
    for name, place in targets.items():
        # A destination that a link takes elsewhere would be written there.
        _within_root(place, roots[_split(name)[0]])
        if stat.S_ISLNK(archive.getinfo(name).external_attr >> 16):
            raise BackupError(f"linked file in archive: {name}")
    keys = list(dict.fromkeys(_split(name)[0] for name in files))
    _check_layout(roots, keys)
    folders = [key for key in keys if not _is_files(key)]
    replaced = [targets[name] for name in files if _is_files(_split(name)[0])]
    replaced += [roots[key] for key in folders]
    existing = [path for path in replaced if path.exists()]
    if existing and not force:
        raise BackupError(f"{existing[0]} already exists; pass --force to replace it")
    _check_space(archive, files, roots)
    for station_key, held_in in manifest.shared.items():
        if (
            station_key in roots
            and held_in in roots
            and held_in in keys
            and roots[station_key].resolve() != roots[held_in].resolve()
        ):
            warnings.append(
                f"In this backup {station_key} shared the {held_in} folder; it is restored "
                f"to {roots[held_in]} only. This configuration gives it {roots[station_key]}: "
                "copy the files there if that station should have them."
            )
    stages = {
        key: roots[key].with_name(f".{roots[key].name}.restore-{uuid.uuid4().hex}") for key in keys
    }
    swap = _Swap(roots["index"] / JOURNAL_NAME, stages.values())
    keep_stages = False
    with im.publication_lock(roots["index"]):
        try:
            for name in files:
                key, rest = _split(name)
                _stage_member(archive, name, stages[key].joinpath(*rest))
            if "index" in stages:
                _check_index(stages["index"])
                for name in ("dj_meta.db", "library.db"):
                    database = stages["index"] / name
                    if database.is_file():
                        _check_sqlite(database)
            try:
                _install(swap, files, roots, stages)
            except BaseException as exc:
                left = swap.undo()
                if left:
                    keep_stages = True
                    raise BackupError(
                        f"the restore failed ({exc}) and could not put everything back: "
                        f"{'; '.join(left)}. Run `autodj doctor` to finish putting the old "
                        "files back."
                    ) from exc
                if isinstance(exc, OSError):
                    raise BackupError(f"the restore failed and nothing was changed: {exc}") from exc
                raise
            leftovers = swap.commit()
            if leftovers:
                warnings.append(
                    "The restore is complete, but these old copies could not be deleted: "
                    + "; ".join(str(path) for path in leftovers)
                    + ". `autodj doctor` can move them out of the way."
                )
        finally:
            if not keep_stages:
                for stage in stages.values():
                    shutil.rmtree(stage, ignore_errors=True)
    return len(files)


def restore_backup(
    cfg: AutoDJConfig, archive: Path, *, force: bool, warnings: list[str] | None = None
) -> int:
    """Restore a backup made by this major.minor version and return its file count.

    AutoDJ must be stopped first: a running ``autodj serve`` holds a lock on
    its index folder, and restore refuses while it is held.  Every file is
    unpacked beside its destination and the index checked before anything
    is replaced, and a replacement that fails is undone, so a damaged
    archive or a full disk changes nothing.  A journal beside the index
    records each step, so a crash part way can be rolled back by
    ``autodj doctor``.  Restored ``liners`` and ``profiles`` folders replace
    the current ones whole, and a restored index replaces every generation.
    Another station's files go back to its own folder; a station the
    configuration does not define is refused before anything is unpacked.

    Args:
        cfg: The configuration to restore into.
        archive: The backup.
        force: Replace files and folders that exist.
        warnings: Gets a sentence for each thing the restore could not do as
            the backup had it, such as a folder two stations shared that this
            configuration keeps apart, or old copies it could not delete.

    Raises:
        BackupError: If AutoDJ is serving, an earlier restore did not finish,
            the archive is invalid, from another version, holds an old-format
            index or a station the configuration lacks, is too big for the free
            space, or would replace existing files without *force*.
    """
    try:
        with zipfile.ZipFile(archive) as opened:
            return _restore(
                opened, _roots(cfg), force=force, warnings=warnings if warnings is not None else []
            )
    except BackupError:
        raise
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
        raise BackupError(f"could not restore {archive}: {exc}") from exc


# ---------------------------------------------------------------------------
# After a crash: what doctor reports and repairs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RestoreLeftovers:
    """What an interrupted or untidy restore left, for doctor.

    Attributes:
        journal: The journal file, or ``None`` without one.
        state: ``"installing"`` (stopped part way: some files new, some
            old), ``"installed"`` (every new file in place; only old copies
            to clear) or ``None`` without a journal.
        strays: Hidden ``.old-`` and ``.restore-`` files and folders that no
            journal names.
    """

    journal: Path | None
    state: str | None
    strays: tuple[Path, ...]

    def __bool__(self) -> bool:
        return self.journal is not None or bool(self.strays)


def _read_journal(index_dir: Path) -> dict[str, Any] | None:
    path = index_dir / JOURNAL_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "unreadable", "steps": [], "stages": [], "leftovers": []}
    return data if isinstance(data, dict) else {"state": "unreadable", "steps": []}


def pending_restore_state(index_dir: Path) -> str | None:
    """The state of an unfinished restore of *index_dir*, or ``None`` without one.

    ``"installing"`` means it stopped part way, ``"installed"`` that only
    its old copies are left, ``"unreadable"`` that the journal cannot be read.
    """
    journal = _read_journal(index_dir)
    return None if journal is None else str(journal.get("state"))


def _journal_paths(journal: dict[str, Any]) -> set[Path]:
    named: set[Path] = set()
    for step in journal.get("steps", []):
        if isinstance(step, dict):
            named.update(
                Path(value) for key, value in step.items() if key != "op" and isinstance(value, str)
            )
    named.update(Path(value) for value in journal.get("stages", []) if isinstance(value, str))
    named.update(Path(value) for value in journal.get("leftovers", []) if isinstance(value, str))
    return named


def restore_leftovers(cfg: AutoDJConfig) -> RestoreLeftovers:
    """Find what an interrupted restore left beside the folders a restore touches."""
    roots = _roots(cfg)
    journal = _read_journal(roots["index"])
    named = _journal_paths(journal) if journal else set()
    places = {root.parent for root in roots.values()} | {
        root for key, root in roots.items() if _is_files(key)
    }
    strays: set[Path] = set()
    for place in places:
        try:
            children = list(place.iterdir()) if place.is_dir() else []
        except OSError:
            continue
        for child in children:
            if re.match(r"^\..+\.(?:old|restore)-[0-9a-f]{32}$", child.name) and child not in named:
                strays.add(child)
    return RestoreLeftovers(
        roots["index"] / JOURNAL_NAME if journal is not None else None,
        str(journal.get("state")) if journal is not None else None,
        tuple(sorted(strays)),
    )


def _keep(path: Path, keep: Path) -> Path:
    """Move *path* into folder *keep* (made if needed); return where it went."""
    keep.mkdir(parents=True, exist_ok=True)
    target = keep / f"{len(list(keep.iterdir())):03d}-{path.name.lstrip('.')}"
    try:
        os.replace(path, target)
    except OSError:
        # Another drive: copy, then remove the original.
        if path.is_dir() and not _is_link(path):
            shutil.copytree(path, target, symlinks=True)
        else:
            shutil.copy2(path, target, follow_symlinks=False)
        _remove(path)
    return target


def recover_restore(cfg: AutoDJConfig, keep: Path) -> list[str]:
    """Roll back an interrupted restore, or finish one, and clear stray copies.

    Nothing is deleted: what is moved out of the way goes into *keep*.
    With a journal in the ``installing`` state, every step it records is
    reversed, newest first, where it took place: the backup's copies go
    into *keep* and the old files back where they were.  In the
    ``installed`` state the old copies go into *keep*.  Stray ``.old-`` and
    ``.restore-`` names go into *keep*.  Returns what was done, in words.

    Raises:
        BackupError: AutoDJ is serving, or a step could not be reversed
            (the journal stays, naming what is left).
    """
    roots = _roots(cfg)
    if server_running(roots["index"]):
        raise BackupError(f"AutoDJ is serving {roots['index']}; stop it first")
    found = restore_leftovers(cfg)
    done: list[str] = []
    journal = _read_journal(roots["index"])
    if journal is not None and found.journal is not None:
        steps = [step for step in journal.get("steps", []) if isinstance(step, dict)]
        if journal.get("state") == "installing":
            problems = []
            for step in reversed(steps):
                try:
                    if step.get("op") == "put":
                        target, source = Path(step["target"]), Path(step["source"])
                        if (target.exists() or target.is_symlink()) and not source.exists():
                            _keep(target, keep)
                    elif step.get("op") == "aside":
                        path, aside = Path(step["path"]), Path(step["aside"])
                        if (aside.exists() or aside.is_symlink()) and not (
                            path.exists() or path.is_symlink()
                        ):
                            os.replace(aside, path)
                            done.append(f"Put {path} back.")
                    elif step.get("op") == "mkdir":
                        folder = Path(step["path"])
                        if folder.is_dir() and not any(folder.iterdir()):
                            folder.rmdir()
                except (OSError, KeyError) as exc:
                    problems.append(f"{step}: {exc}")
            if problems:
                raise BackupError(
                    "could not put everything back: "
                    + "; ".join(problems)
                    + f". The journal {found.journal} still names each step."
                )
            done.append("The interrupted restore was rolled back; the old files are back.")
        else:
            for step in steps:
                if step.get("op") == "aside":
                    aside = Path(step.get("aside", ""))
                    if aside.exists() or aside.is_symlink():
                        done.append(f"Moved the old copy {aside} to {_keep(aside, keep)}.")
            for value in journal.get("leftovers", []):
                leftover = Path(value)
                if leftover.exists() or leftover.is_symlink():
                    done.append(f"Moved the old copy {leftover} to {_keep(leftover, keep)}.")
            done.append("The restore was already complete; its old copies are out of the way.")
        for value in journal.get("stages", []):
            stage = Path(value)
            if stage.exists():
                _keep(stage, keep)
        found.journal.unlink(missing_ok=True)
    for stray in found.strays:
        if stray.exists() or stray.is_symlink():
            done.append(f"Moved {stray} to {_keep(stray, keep)}.")
    return done
