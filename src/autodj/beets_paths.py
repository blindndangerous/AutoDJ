"""Whether the songs in the beets database are the songs in AutoDJ's library.

AutoDJ matches a beets item to a library song by its file path.  Beets may
store paths relative to its music folder; AutoDJ joins those to
``[library] music_dir``.  When that setting names the wrong folder (an
example value left in a container, say) or is not set, no beets item
matches a library song, every beets query finds nothing, and clockwheel
steps for those categories are skipped (#386).

:func:`match` keys beets' values by library path and says how well they
matched (:class:`PathCheck`).  When ``music_dir`` is not set, or names a
folder that does not exist here, and every sampled relative path sits
under one and the same folder of the library, that folder is used
instead.  Otherwise nothing is guessed: the check only says what is wrong
and which folder probably holds the paths.

:func:`remember` keeps the latest check per beets database and
``music_dir`` and logs it once each time what it says changes, so a
problem is logged once, not on every pick.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Relative beets paths looked up in the library to find their folder.
SAMPLE = 200
#: Matches needed before a found folder is used.
MIN_AGREEING = 3


def is_absolute(raw: str) -> bool:
    """Whether a path as beets stores it is absolute, on either platform."""
    text = raw.replace("\\", "/")
    return text.startswith("/") or (len(text) >= 2 and text[1] == ":")


def library_key(raw: str, root: Path | str | None) -> str:
    """The catalog spelling of beets path *raw*, a relative one joined to *root*."""
    from autodj.catalog import normalize_path

    if root is not None and not is_absolute(raw):
        return normalize_path(Path(root) / raw.replace("\\", "/").lstrip("/"))
    return normalize_path(raw)


@dataclass(frozen=True)
class PathCheck:
    """How the beets database's paths matched AutoDJ's library songs.

    Attributes:
        items: Items in the beets database.
        library: Songs in AutoDJ's library.
        matched: Library songs that have a beets item.
        relative: Beets stores paths relative to its music folder.
        music_dir: ``[library] music_dir`` as configured.
        music_dir_exists: Whether that folder exists here.
        root: The folder relative paths were joined to (configured or found).
        found: The library folder the sampled relative paths sit under,
            used or only suggested; ``None`` when none was found.
        example: One beets path, as beets stores it.
    """

    items: int
    library: int
    matched: int
    relative: bool
    music_dir: Path | None
    music_dir_exists: bool
    root: Path | None
    found: Path | None
    example: str

    @property
    def mismatch(self) -> bool:
        """Beets and the library both have songs, and not one matches."""
        return self.items > 0 and self.library > 0 and self.matched == 0

    @property
    def detected(self) -> bool:
        """The found folder is in use because ``music_dir`` is unset or missing."""
        return self.found is not None and self.root == self.found and self.matched > 0

    def _setting(self) -> str:
        if self.music_dir is None:
            return "is not set"
        if not self.music_dir_exists:
            return f"is {self.music_dir}, a folder that does not exist here"
        return f"is {self.music_dir}"

    @property
    def summary(self) -> str:
        """One line about the match, for doctor and the page."""
        return f"{self.matched:,} of {self.library:,} library songs have a beets entry"

    @property
    def problem(self) -> str | None:
        """What is wrong, in plain words; ``None`` when nothing is."""
        if not self.mismatch:
            return None
        text = (
            "No song in the beets database matches a song in AutoDJ's library, so beets "
            "queries and Beets field conditions find no songs."
        )
        if self.relative:
            return (
                f"{text} Beets stores paths relative to its music folder, such as "
                f"{self.example}, and AutoDJ reads them from [library] music_dir, which "
                f"{self._setting()}."
            )
        return (
            f"{text} Beets stores full paths, such as {self.example}, and none of them is a "
            "song in a registered music folder."
        )

    @property
    def fix(self) -> str | None:
        """What to change; ``None`` when nothing needs changing."""
        if not self.mismatch:
            return None
        if self.relative and self.found is not None:
            return (
                f'Set music_dir = "{self.found.as_posix()}" under [library] in config.toml or '
                "config.local.toml, then restart AutoDJ. That folder holds beets' paths here."
            )
        if self.relative:
            return (
                "Set [library] music_dir to the folder on this machine that holds beets' "
                "music (the folder beets' directory setting names), then restart AutoDJ."
            )
        return (
            "Beets and AutoDJ must reach the music by the same paths: mount the music at "
            "the folder beets uses, or register it in Library tools by that path."
        )

    @property
    def cause(self) -> str | None:
        """A short likely cause, for a category with no songs."""
        if not self.mismatch:
            return None
        if self.relative:
            return "no beets song path matches the library; check [library] music_dir"
        return "no beets song path matches the library; beets uses other folder paths"

    @property
    def note(self) -> str | None:
        """Says the found folder is in use; ``None`` when it is not."""
        if not self.detected or self.found is None:
            return None
        root = self.found.as_posix()
        return (
            f"Beets queries and Beets conditions read relative paths from {root}, "
            "found from the library, because "
            f'[library] music_dir {self._setting()}. Set music_dir = "{root}" under [library] '
            "to choose the folder yourself."
        )

    def message(self) -> str | None:
        """The problem and its fix, or the note; ``None`` when all is well."""
        if self.problem:
            return f"{self.problem} {self.fix}"
        return self.note


def _parts(path: str) -> list[str]:
    return path.replace("\\", "/").split("/")


def _fold(part: str) -> str:
    return part.casefold() if os.name == "nt" else part


def _roots(relative: Sequence[str], library: Sequence[str]) -> Counter[str]:
    """For a sample of *relative* paths, the library folders each one sits under."""
    by_name: dict[str, list[list[str]]] = {}
    for path in library:
        parts = _parts(path)
        by_name.setdefault(_fold(parts[-1]), []).append(parts)
    step = max(1, len(relative) // SAMPLE)
    roots: Counter[str] = Counter()
    for raw in relative[::step][:SAMPLE]:
        tail = [_fold(part) for part in _parts(raw) if part]
        if not tail:
            continue
        found: set[str] = set()
        for parts in by_name.get(tail[-1], ()):
            if len(parts) > len(tail) and [_fold(p) for p in parts[-len(tail) :]] == tail:
                root = "/".join(parts[: -len(tail)])
                found.add(root + "/" if not root or root.endswith(":") else root)
        roots.update(found)
    return roots


def match(
    rows: Iterable[tuple[str, Mapping[str, Any]]],
    library: Sequence[str],
    music_dir: Path | None,
) -> tuple[dict[str, dict[str, Any]], PathCheck]:
    """Key beets' values by library path, and say how well they matched.

    Args:
        rows: ``(path, values)`` per beets item, the path as beets stores it.
        library: The paths of AutoDJ's library songs.
        music_dir: ``[library] music_dir``.

    Returns:
        The values by catalog path, and the :class:`PathCheck`.
    """
    from autodj.catalog import normalize_path

    items = [(str(path), dict(values)) for path, values in rows]
    keys = {normalize_path(path) for path in library}
    relative = [raw for raw, _ in items if not is_absolute(raw)]
    is_relative = len(relative) * 2 > len(items)
    exists = music_dir is not None and Path(music_dir).is_dir()

    def keyed(root: Path | None) -> dict[str, dict[str, Any]]:
        return {library_key(raw, root): values for raw, values in items}

    root = music_dir
    found_items = keyed(root)
    matched = len(keys & found_items.keys())
    found: Path | None = None
    if items and keys and matched == 0 and is_relative:
        roots = _roots(relative, library)
        if roots:
            best, count = roots.most_common(1)[0]
            found = Path(best)
            if len(roots) == 1 and count >= MIN_AGREEING and not exists:
                trial = keyed(found)
                tried = len(keys & trial.keys())
                if tried:
                    root, found_items, matched = found, trial, tried
    check = PathCheck(
        items=len(items),
        library=len(keys),
        matched=matched,
        relative=is_relative,
        music_dir=music_dir,
        music_dir_exists=exists,
        root=root,
        found=found,
        example=items[0][0] if items else "",
    )
    return found_items, check


# ----------------------------------------------------------------------
# The latest check, logged once per change
# ----------------------------------------------------------------------

_lock = threading.Lock()
#: (beets database, music_dir) -> (stamp, check, what was logged).
_latest: dict[tuple[str, str | None], tuple[tuple[Any, ...], PathCheck, str | None]] = {}


def _key(db_path: Path, music_dir: Path | None) -> tuple[str, str | None]:
    return str(db_path), str(music_dir) if music_dir is not None else None


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        found = path.stat()
    except OSError:
        return None
    return found.st_mtime_ns, found.st_size


def _stamp(db_path: Path, music_dir: Path | None, songs: int) -> tuple[Any, ...]:
    return (
        _stat(db_path),
        _stat(Path(f"{db_path}-wal")),
        music_dir is not None and Path(music_dir).is_dir(),
        songs,
    )


def remember(db_path: Path, music_dir: Path | None, check: PathCheck, songs: int) -> None:
    """Keep *check* as the latest for this database, logging it when what it says changed.

    *songs* is how many library songs the check read, so a later
    :func:`check_library` call knows whether the library changed.
    """
    key = _key(db_path, music_dir)
    said = check.message()
    with _lock:
        before = _latest.get(key)
        _latest[key] = (_stamp(db_path, music_dir, songs), check, said)
    if before is not None and before[2] == said:
        return
    if check.problem:
        logger.error("Beets paths: %s %s", check.problem, check.fix)
    elif check.note:
        log = logger.warning if check.music_dir is not None else logger.info
        log("Beets paths: %s", check.note)
    elif before is not None and before[2]:
        logger.info("Beets paths: %s", check.summary)


def latest(cfg: Any) -> PathCheck | None:
    """The latest check for *cfg*'s beets database, or ``None`` when none was made."""
    library = getattr(cfg, "library", None)
    db = getattr(library, "beets_db", None)
    if not db or not isinstance(db, str | os.PathLike):
        return None
    with _lock:
        found = _latest.get(_key(Path(db), getattr(library, "music_dir", None)))
    return found[1] if found else None


def check_library(cfg: Any, entries: Sequence[Any]) -> PathCheck | None:
    """Check *cfg*'s beets database against library *entries*, again only when either changed.

    Returns ``None`` without a beets database, or when it cannot be read
    (the ``beets-db`` doctor check and the categories say why).
    """
    library = getattr(cfg, "library", None)
    db = getattr(library, "beets_db", None)
    if not db or not isinstance(db, str | os.PathLike):
        return None
    db_path = Path(db)
    music_dir = getattr(library, "music_dir", None)
    paths = [entry.path for entry in entries if not getattr(entry, "is_silent", False)]
    with _lock:
        found = _latest.get(_key(db_path, music_dir))
    if found is not None and found[0] == _stamp(db_path, music_dir, len(paths)):
        return found[1]
    from autodj.beets import read_items

    try:
        rows = [(str(path), values) for path, values in read_items(db_path, ())]
    except (OSError, sqlite3.Error):
        return None
    _, check = match(rows, paths, music_dir)
    remember(db_path, music_dir, check, len(paths))
    return check


def page_view(check: PathCheck | None) -> dict[str, Any] | None:
    """What the page shows about *check*; ``None`` without beets."""
    if check is None:
        return None
    return {
        "summary": check.summary,
        "problem": check.problem,
        "fix": check.fix,
        "note": check.note,
        "mismatch": check.mismatch,
    }
