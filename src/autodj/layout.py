"""Move an index folder from the old layout into libraries/, shared/ and stations/.

The old layout kept one named index in ``<index_dir>/<name>/`` (``default``
unless ``[index] name`` said otherwise): the library, what stations share,
the default station's own files, and every other station in
``stations/<slug>/``.  A few of the default station's files sat one level
up, in ``<index_dir>``.  Now:

- ``libraries/main/`` holds the library (``library.db``, ``dj_meta.db``,
  their logs, the related artists and tracks files, and an older index's files for ``autodj migrate-index``);
- ``shared/`` what every station shares (playlists, Stereo Tool presets,
  time check packs);
- ``stations/<slug>/`` each station, the default station's too, with its
  own categories (each starts with a copy of the ones every station shared);
- ``logs/`` the server's log.

:func:`move_old_layout` renames each file once, inside the index folder, so
nothing is copied.  Another named index's folder is left alone.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig

logger = logging.getLogger(__name__)

#: What every station shares; everything else unknown is the default station's.
_SHARED = frozenset({"playlists", "time_checks"})
_LIBRARY = frozenset(
    {
        "library.db",
        "dj_meta.db",
        "decode-indexing.log",
        "decode-analysing.log",
        "decode-library.log",
        # Related artists and tracks name the library's songs.
        "related-artists.toml",
        "related-tracks.toml",
        # An index older than library.db, for autodj migrate-index.
        "index-manifest.json",
        "tracks.db",
        "vectors.index",
    }
)
#: Folders of the new layout, never another named index.
_NEW = frozenset({"libraries", "shared", "stations", "tools", "logs"})
#: The default station's files that the old layout kept in <index_dir>.
_UP_ONE = {"relay-status.json": "station", "profiles": "station", "stereo-tool-presets": "shared"}


class LayoutError(RuntimeError):
    """The old layout could not be moved; the message says why."""


def _old_name(cfg: AutoDJConfig) -> str:
    """The ``[index] name`` the configuration had, ``default`` when none."""
    path = cfg.config_path
    if path is None or not Path(path).is_file():
        return "default"
    try:
        with Path(path).open("rb") as fh:
            name = tomllib.load(fh).get("index", {}).get("name", "default")
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        return "default"
    return (
        name
        if isinstance(name, str) and name and "/" not in name and "\\" not in name
        else "default"
    )


def _is_library(name: str) -> bool:
    if name in _LIBRARY or name.startswith(("library.db", "dj_meta.db")):
        # The databases' -wal, -shm and doctor's .bak copies go with them.
        return True
    if name.startswith(("tracks.g", "vectors.g")):
        return True
    # Hidden locks, publication state and a restore's leftovers.
    return name.startswith(".")


def old_layout(cfg: AutoDJConfig) -> Path | None:
    """The old named index folder still to move, or ``None``."""
    old = Path(cfg.index.index_dir) / _old_name(cfg)
    if old.name in _NEW or not old.is_dir():
        return None
    return old


def move_old_layout(cfg: AutoDJConfig) -> bool:
    """Move the old layout into the new one; return whether anything moved.

    Raises:
        LayoutError: A file could not be moved, for example because an older
            AutoDJ still runs and holds it open, or a restore did not finish.
    """
    old = old_layout(cfg)
    if old is None:
        return False
    if (old / ".autodj-restore.json").exists():
        raise LayoutError(
            f"A restore of {old} did not finish. Run `autodj doctor` with the AutoDJ release "
            "that made it, then start this one."
        )
    root = Path(cfg.index.index_dir)
    library, shared, stations = cfg.index.library_dir, cfg.index.shared_dir, cfg.index.stations_dir
    default = stations / "default"
    moves: list[tuple[Path, Path]] = []
    moved_stations: list[Path] = []
    for item in sorted(old.iterdir()):
        name = item.name
        if name == ".autodj-serve.lock":
            continue
        if name == "stations" and item.is_dir():
            for child in sorted(item.iterdir()):
                moves.append((child, stations / child.name))
                if child.is_dir():
                    moved_stations.append(stations / child.name)
        elif name == "logs":
            moves.append((item, root / "logs"))
        elif name in _SHARED:
            moves.append((item, shared / name))
        elif _is_library(name):
            moves.append((item, library / name))
        else:
            moves.append((item, default / name))
    for name, where in _UP_ONE.items():
        item = root / name
        if item.exists():
            moves.append((item, (default if where == "station" else shared) / name))
    left: list[str] = []
    for source, target in moves:
        if target.exists():
            left.append(source.name)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            source.rename(target)
        except OSError as exc:
            raise LayoutError(
                f"Could not move {source} to {target} ({exc.strerror or exc}). Stop any other "
                "AutoDJ that uses this index folder, then start this one again."
            ) from exc
    # Every station used the shared categories: each now has its own copy.
    categories = default / "categories.json"
    for station in moved_stations:
        if categories.is_file() and not (station / "categories.json").exists():
            try:
                shutil.copyfile(categories, station / "categories.json")
            except OSError as exc:
                raise LayoutError(
                    f"Could not copy {categories} to {station} ({exc.strerror or exc})."
                ) from exc
    for leftover in (old / ".autodj-serve.lock", old / "stations"):
        with contextlib.suppress(OSError):
            if leftover.is_file():
                leftover.unlink()
            else:
                leftover.rmdir()
    try:
        old.rmdir()
    except OSError:
        left = left or sorted(path.name for path in old.iterdir())
    logger.warning("Moved %s into %s, %s and %s.", old, library, shared, stations)
    if left:
        logger.warning(
            "Left in %s because the new layout already has them: %s", old, ", ".join(left)
        )
    for other in sorted(root.iterdir()):
        if other.is_dir() and other.name not in _NEW and (other / "library.db").is_file():
            logger.warning(
                "%s is another named index; AutoDJ no longer reads named indexes, so it was "
                "left as it is.",
                other,
            )
    return True
