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
  its default name, and ``"shared"`` in the manifest says so.  ``manifest.json``
  lists those stations under ``"stations"``, and restore refuses a station the
  configuration does not define.

Every station's state files are copied with the index under its publication
lock.  Restore is all or nothing: what it replaces
is set aside first (``.<name>.old-<id>``) and put back if any replacement fails.

Broadcast recordings are archives, not station state: they can run to many
gigabytes and a restore would replace the whole folder, so they stay out.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
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


def _folder_files(root: Path, pattern: str = "**/*") -> list[Path]:
    """The files a backup takes from folder *root*.

    Uploaded Stereo Tool presets that are links stay out, as before; how
    links in the other folders are treated is #193's (PR #197).
    """
    return [
        path
        for path in sorted(root.glob(pattern))
        if path.is_file() and (pattern != "*.sts" or not path.is_symlink())
    ]


def _snapshot(cfg: AutoDJConfig, roots: dict[str, Path], snapshot: Path) -> dict[str, Path]:
    """Copy the index and every station's state files into *snapshot*.

    The index generation and the state files are copied under the index's
    publication lock, so they are one moment's; the databases go through
    SQLite's backup API.  Returns the archive members, by name.
    """
    active = roots["index"]
    index = snapshot / "index"
    index.mkdir(parents=True)
    members: dict[str, Path] = {}
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
                shutil.copyfile(active / name, index / name)
                if im.sha256_file(index / name) != digest:
                    raise BackupError(f"{active / name} does not match its SHA-256 in the manifest")
            shutil.copyfile(active / im.MANIFEST_NAME, index / im.MANIFEST_NAME)
        state = [("index", name) for name in _STATE_FILES]
        state += [
            (f"stations/{spec.slug}/index", name) for spec in cfg.stations for name in STATION_FILES
        ]
        for key, name in state:
            original = roots[key] / name
            if not original.is_file():
                continue
            copy = snapshot / key / name
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, copy)
            members[f"{key}/{name}"] = copy
    for name in ("dj_meta.db", "library.db"):
        database = active / name
        if not database.is_file():
            continue
        with (
            closing(sqlite3.connect(readonly_uri(database), uri=True)) as source,
            closing(sqlite3.connect(index / name)) as target,
        ):
            source.backup(target)
        _check_sqlite(index / name)
    for name in ("related-artists.toml", "related-tracks.toml"):
        related = active / name
        if related.is_file():
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
        for suffix in ("-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
    if result != ["ok"]:
        raise BackupError(f"{path} failed SQLite integrity_check: {'; '.join(map(str, result))}")


def create_backup(cfg: AutoDJConfig, destination: Path, *, force: bool) -> Path:
    """Write a backup archive to *destination* and return its absolute path.

    Safe while AutoDJ serves: the index and every station's state files are
    copied under the index's publication lock, the index checked against its
    manifest, and SQLite databases copied through SQLite's backup API.

    Raises:
        BackupError: If *destination* exists without *force*, the index is
            old, damaged or unreadable, or two folders overlap.
    """
    destination = destination.expanduser().resolve()
    if destination.exists() and not force:
        raise BackupError(f"{destination} already exists; pass --force to replace it")
    roots = _roots(cfg)
    folders, shared = _folder_roots(roots)
    for key, root in folders.items():
        if destination.is_relative_to(root.resolve()):
            raise BackupError(f"write the backup outside the {key} folder {root}")
    partial = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tempfile.TemporaryDirectory(prefix="autodj-backup-") as temp:
            members = _snapshot(cfg, roots, Path(temp))
            for key, root in folders.items():
                pattern = "*.sts" if key == "stereo-tool-presets" else "**/*"
                for path in _folder_files(root, pattern):
                    members[f"{key}/{path.relative_to(root).as_posix()}"] = path
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
    {"CON", "PRN", "AUX", "NUL"} | {f"{kind}{n}" for kind in ("COM", "LPT") for n in "123456789¹²³"}
)


def _unsafe_part(part: str) -> bool:
    """Whether one path segment cannot be written safely on every system."""
    if part.endswith((".", " ")) or any(ord(char) < 32 for char in part):
        return True
    return part.split(".", 1)[0].rstrip(" ").upper() in _DEVICE_NAMES


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


class _Swap:
    """Every replacement a restore makes, so a failure puts everything back.

    Each step is a rename within one folder.  Until :meth:`commit`, every
    old file and folder is kept aside under a hidden name; :meth:`undo`
    reverses the steps in the opposite order.
    """

    def __init__(self) -> None:
        self._undo: list[Callable[[], None]] = []
        self._aside: list[Path] = []
        self._dirs: set[Path] = set()

    def _aside_name(self, path: Path) -> Path:
        return path.with_name(f".{path.name}.old-{uuid.uuid4().hex}")

    def move_aside(self, path: Path) -> None:
        """Move *path* out of the way, to be deleted after a commit or put back."""
        if not path.exists() and not path.is_symlink():
            return
        aside = self._aside_name(path)
        os.replace(path, aside)
        self._aside.append(aside)
        self._dirs.add(path.parent)
        self._undo.append(lambda: os.replace(aside, path))

    def put(self, source: Path, target: Path) -> None:
        """Move *source* to *target*, where nothing is now."""
        os.replace(source, target)
        self._dirs.add(target.parent)
        self._undo.append(lambda: os.replace(target, source))

    def make_dir(self, path: Path) -> None:
        """Make folder *path* (and its parents) if it is missing."""
        missing = []
        for folder in (path, *path.parents):
            if folder.exists():
                break
            missing.append(folder)
        path.mkdir(parents=True, exist_ok=True)
        for folder in missing:
            self._undo.append(folder.rmdir)

    def undo(self) -> list[str]:
        """Put everything back, newest step first; return what could not be."""
        problems = []
        for step in reversed(self._undo):
            try:
                step()
            except OSError as exc:
                problems.append(str(exc))
        self._undo.clear()
        return problems

    def commit(self) -> None:
        """Keep the new files: delete what was set aside and flush the folders."""
        self._undo.clear()
        for aside in self._aside:
            if aside.is_dir() and not aside.is_symlink():
                shutil.rmtree(aside, ignore_errors=True)
            else:
                aside.unlink(missing_ok=True)
        for folder in self._dirs:
            if folder.is_dir():
                fsync_directory(folder)


def _install(
    swap: _Swap, files: list[str], roots: dict[str, Path], stages: dict[str, Path]
) -> None:
    """Replace what the archive holds: first move every old file and folder aside, then put the new ones in."""
    keys = list(stages)
    folder_keys = [key for key in keys if not _is_files(key)]
    file_names = {
        key: sorted(path.name for path in stages[key].iterdir()) for key in keys if _is_files(key)
    }
    for key in folder_keys:
        swap.move_aside(roots[key])
    for key, names in file_names.items():
        root = roots[key]
        for name in names:
            for suffix in ("-wal", "-shm", ""):
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


def _restore(
    archive: zipfile.ZipFile, roots: dict[str, Path], *, force: bool, warnings: list[str]
) -> int:
    """Check, unpack and install *archive*; return the number of files restored.

    Nothing is replaced until every file is unpacked and checked, and if a
    replacement fails, every one already made is undone.
    """
    manifest = _read_manifest(archive)
    files = manifest.files
    for slug in manifest.stations:
        if f"stations/{slug}/index" not in roots:
            raise _unknown_station(slug)
    targets = {name: _target(roots, name) for name in files}
    if sorted(files) != sorted(name for name in archive.namelist() if name != "manifest.json"):
        raise BackupError("the archive's files do not match its manifest.json")
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
    swap = _Swap()
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
            try:
                _install(swap, files, roots, stages)
            except BaseException as exc:
                left = swap.undo()
                if left:
                    raise BackupError(
                        f"the restore failed ({exc}) and could not put everything back: "
                        f"{'; '.join(left)}. Files set aside are named .<name>.old-<id> "
                        "beside them."
                    ) from exc
                if isinstance(exc, OSError):
                    raise BackupError(f"the restore failed and nothing was changed: {exc}") from exc
                raise
            swap.commit()
        finally:
            for stage in stages.values():
                shutil.rmtree(stage, ignore_errors=True)
    return len(files)


def restore_backup(
    cfg: AutoDJConfig, archive: Path, *, force: bool, warnings: list[str] | None = None
) -> int:
    """Restore a backup made by this major.minor version and return its file count.

    AutoDJ must be stopped first; nothing here can tell whether it runs.  Every
    file is unpacked beside its destination and the index checked before
    anything is replaced, and a replacement that fails is undone, so a damaged
    archive or a full disk changes nothing.  Restored ``liners`` and
    ``profiles`` folders replace the current ones whole, and a restored index
    replaces every generation.  Another station's files go back to its own
    folder; a station the configuration does not define is refused before
    anything is unpacked.

    Args:
        cfg: The configuration to restore into.
        archive: The backup.
        force: Replace files and folders that exist.
        warnings: Gets a sentence for each thing the restore could not do as
            the backup had it, such as a folder two stations shared that this
            configuration keeps apart.

    Raises:
        BackupError: If the archive is invalid, from another version, holds an
            old-format index or a station the configuration lacks, is too big
            for the free space, or would replace existing files without *force*.
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
