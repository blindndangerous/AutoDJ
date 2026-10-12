"""Rotation categories: named sets of songs that clockwheel steps pick from.

A category such as A, B, "2000s" or "Rock gold" has a name and one source:

- a beets query (:mod:`autodj.beets_query`), such as
  ``original_year:2000..2009``, evaluated against AutoDJ's library with the
  live beets database's fields laid over it when ``[library] beets_db`` is
  set (any beets field, flexible attributes included); or
- an M3U or M3U8 file in the playlists folder (``[library]
  playlists_folder``, by default ``<index_dir>/<name>/playlists``), such as
  one an update-playlists script writes.  Each line is matched to a library
  track; the lines that match none are reported as missing.

The categories are kept in ``categories.json`` in the active index folder
(versioned, written atomically; a file that cannot be read is set aside
and the station starts with none).  :class:`CategoryIndex` works out which
library songs each category holds and keeps the answer until the
categories, the beets database, a playlist file or the library changes,
which a few ``stat`` calls tell it; a clockwheel pick never re-reads them.

A year filter reads beets' first-release year (``original_year``), falling
back to ``year`` when beets has none; songs with neither are left out, and
the songs that used the fallback are counted so Settings and ``autodj
doctor`` can say how many.

A category may have allowed hours and days (dayparting): outside them its
songs are not picked in any pick mode, and a clockwheel step for it is
skipped.  The hours are wall-clock times in the station time zone; a
window that crosses midnight belongs to the day it starts.  A song in
several categories plays only while every one of them allows it.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as Time
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from autodj.beets_query import FIRST_RELEASE_YEAR, Query, QueryError, parse_query
from autodj.beets_query import first_release_year as _first_release_year
from autodj.fsutil import atomic_write

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig

logger = logging.getLogger(__name__)

SCHEMA_VERSION: Literal[1] = 1
FILE_NAME = "categories.json"
MAX_CATEGORIES = 200
NAME_MAX = 64
#: Playlist files a category may read.
PLAYLIST_SUFFIXES = (".m3u", ".m3u8")
#: The largest playlist file read, in bytes.
MAX_PLAYLIST_BYTES = 8 * 1024 * 1024
#: The most playlist files listed for the Settings choice.
MAX_PLAYLIST_FILES = 500
#: Missing playlist lines named in a report before it says how many more.
MISSING_EXAMPLES = 20
#: The fields AutoDJ's own library holds for every song, beets or not.
LIBRARY_FIELDS = ("title", "artist", "album", "genre", "year", "bpm", "length")

CategoryId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class CategoryError(ValueError):
    """A category that cannot be saved, with the field at fault and the reason in words."""

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(message)
        self.field = field_name
        self.message = message


def _playlist_path(value: str) -> str:
    """Check a playlist file name: a path inside the playlists folder, forward slashes."""
    parts = value.split("/")
    if (
        not value
        or len(value) > 1024
        or "\\" in value
        or ":" in value
        or "\0" in value
        or any(part in {"", ".", ".."} or part != part.strip() for part in parts)
    ):
        raise ValueError(
            "Choose a playlist file inside the playlists folder, such as A.m3u8, "
            "with forward slashes and no '..'"
        )
    if not value.casefold().endswith(PLAYLIST_SUFFIXES):
        raise ValueError("A playlist file must end in .m3u or .m3u8")
    return value


class CategorySpec(BaseModel):
    """What the Settings form sends: a name and one source."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=NAME_MAX)
    source: Literal["query", "m3u"]
    query: str = Field(default="", max_length=500)
    playlist: str = Field(default="", max_length=1024)
    # Dayparting: the weekdays (0 is Monday) and the hours the songs may
    # play.  No days means every day; no hours means all day.
    days: list[StrictInt] = Field(default_factory=list, max_length=7)
    hours_from: Time | None = None
    hours_until: Time | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("A category name cannot hold control characters")
        return value

    @model_validator(mode="after")
    def _source(self) -> Self:
        if self.source == "query":
            if self.playlist:
                raise ValueError("A query category has no playlist file")
            if not self.query:
                raise ValueError("Enter a beets query, such as original_year:2000..2009")
            parse_query(self.query)
        else:
            if self.query:
                raise ValueError("A playlist category has no query")
            _playlist_path(self.playlist)
        return self

    @model_validator(mode="after")
    def _daypart(self) -> Self:
        if any(type(day) is not int or day not in range(7) for day in self.days):
            raise CategoryError("days", "Days must be numbered 0 (Monday) to 6 (Sunday)")
        self.days = sorted(set(self.days))
        for name in ("hours_from", "hours_until"):
            value = getattr(self, name)
            if value is not None and (
                value.tzinfo is not None or value.second or value.microsecond
            ):
                raise CategoryError(name, f"{_FIELD_NAMES[name]} must be a whole minute")
        if (self.hours_from is None) != (self.hours_until is None):
            missing = "hours_until" if self.hours_until is None else "hours_from"
            raise CategoryError(
                missing, "Enter both Allowed from and Allowed until, or leave both empty"
            )
        if self.hours_from is not None and self.hours_from == self.hours_until:
            raise CategoryError("hours_until", "Allowed until must differ from Allowed from")
        return self

    @property
    def dayparted(self) -> bool:
        """Whether the category limits its songs to some days or hours."""
        return bool(self.days) or self.hours_from is not None

    def allows(self, wall: datetime) -> bool:
        """Whether the songs may play at the station's local wall time *wall*."""
        if self.hours_from is None or self.hours_until is None:
            return not self.days or wall.weekday() in self.days
        at = wall.time()
        day = wall.date()
        if self.hours_from < self.hours_until:
            if not self.hours_from <= at < self.hours_until:
                return False
        elif at < self.hours_until:
            day -= timedelta(days=1)  # the window that began yesterday
        elif at < self.hours_from:
            return False
        return not self.days or day.weekday() in self.days

    def parsed_query(self) -> Query:
        """The query, parsed (an empty query for a playlist category)."""
        return parse_query(self.query) if self.source == "query" else Query()


class Category(CategorySpec):
    """A saved category, with the ID clockwheel steps name it by."""

    id: CategoryId


class CategoriesFile(BaseModel):
    """``categories.json``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    categories: list[Category] = Field(default_factory=list, max_length=MAX_CATEGORIES)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [category.id for category in self.categories]
        names = [category.name.casefold() for category in self.categories]
        if len(set(ids)) != len(ids):
            raise ValueError("two categories have the same ID")
        if len(set(names)) != len(names):
            raise ValueError("two categories have the same name")
        return self


_FIELD_NAMES = {
    "name": "Name",
    "source": "Source",
    "query": "Beets query",
    "playlist": "Playlist file",
    "days": "Days",
    "hours_from": "Allowed from",
    "hours_until": "Allowed until",
}

_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def station_now(cfg: AutoDJConfig) -> datetime:
    """The station's local wall time, naive: ``[server] station_timezone`` or the system's."""
    from zoneinfo import ZoneInfo

    from autodj.scheduler import system_timezone

    zone = ZoneInfo(cfg.server.station_timezone or system_timezone())
    return datetime.now(zone).replace(tzinfo=None)


def describe_daypart(spec: CategorySpec) -> str:
    """Say a category's allowed days and hours in words; empty when it has none."""
    if not spec.dayparted:
        return ""
    if spec.days == [0, 1, 2, 3, 4]:
        days = "on weekdays"
    elif spec.days == [5, 6]:
        days = "at weekends"
    elif spec.days and len(spec.days) < 7:
        names = [_DAY_NAMES[day] for day in spec.days]
        days = "on " + (names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}")
    else:
        days = "every day"
    if spec.hours_from is None or spec.hours_until is None:
        return f"Plays only {days}"
    start, end = spec.hours_from.strftime("%H:%M"), spec.hours_until.strftime("%H:%M")
    return f"Plays only {days} from {start} until {end}"


def spec_error(exc: ValidationError) -> CategoryError:
    """The first problem of a validation error, as a :class:`CategoryError` in words."""
    error = exc.errors()[0]
    location = [str(part) for part in error.get("loc", ())]
    name = location[0] if location and location[0] in _FIELD_NAMES else ""
    message = str(error.get("msg", "is not valid"))
    message = message.removeprefix("Value error, ")
    cause = (error.get("ctx") or {}).get("error")
    if isinstance(cause, CategoryError):
        return CategoryError(cause.field, cause.message)
    if isinstance(cause, QueryError):
        name, message = "query", str(cause)
    elif not name and isinstance(cause, ValueError):
        text = str(cause)
        name = "query" if "query" in text else "playlist" if "playlist" in text else ""
    if error.get("type") in {"string_too_short", "missing"} and name == "name":
        message = "Enter a name for the category"
    elif error.get("type") == "string_too_long" and name:
        message = f"{_FIELD_NAMES[name]} is too long"
    elif error.get("type") == "literal_error" and name == "source":
        message = "Choose a beets query or a playlist file as the source"
    elif error.get("type") == "extra_forbidden":
        message = f"Unknown field {location[-1] if location else ''}".strip()
    return CategoryError(name or "name", message)


def parse_spec(body: Any) -> CategorySpec:
    """Validate a form body.

    Raises:
        CategoryError: The body is not a valid category; says which field.
    """
    try:
        return CategorySpec.model_validate(body)
    except ValidationError as exc:
        raise spec_error(exc) from None


def new_id() -> str:
    """A fresh category ID."""
    return uuid.uuid4().hex[:12]


class CategoryStore:
    """``categories.json``: the saved categories, written atomically.

    A file that cannot be read is set aside as ``categories.json.bad-<id>``
    and the station starts with no categories, as the schedule does.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _keep_bad_copy(self, reason: str) -> None:
        bad = self.path.with_name(f"{self.path.name}.bad-{uuid.uuid4().hex[:8]}")
        try:
            os.replace(self.path, bad)
        except OSError:
            logger.exception("Could not set aside the invalid categories file %s", self.path)
            return
        logger.error(
            "Categories file %s is invalid (%s); set it aside as %s", self.path, reason, bad
        )

    def load(self) -> list[Category]:
        """Read the file; a missing file is no categories, a bad one is set aside."""
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.error("Could not read the categories file %s: %s", self.path, exc)
            return []
        try:
            return CategoriesFile.model_validate_json(data).categories
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first.get("loc", ()))
            self._keep_bad_copy(f"{where}: {first['msg']}" if where else str(first["msg"]))
            return []

    def save(self, categories: Sequence[Category]) -> None:
        """Write *categories* atomically."""
        document = CategoriesFile(
            schema_version=SCHEMA_VERSION, categories=list(categories)
        ).model_dump(mode="json")
        # A category without days or hours is written without the three keys,
        # so an older AutoDJ, which refuses unknown keys, still reads the file.
        for category in document["categories"]:
            for key, empty in (("days", []), ("hours_from", None), ("hours_until", None)):
                if category[key] == empty:
                    del category[key]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(document, indent=2, ensure_ascii=False) + "\n")


def store_path(cfg: AutoDJConfig) -> Path:
    """Where this configuration keeps its categories."""
    return Path(cfg.index.active_dir) / FILE_NAME


def playlists_folder(cfg: AutoDJConfig) -> Path:
    """The folder M3U categories read from."""
    configured = cfg.library.playlists_folder
    return Path(configured) if configured else Path(cfg.index.active_dir) / "playlists"


def list_playlists(folder: Path) -> list[str]:
    """The M3U and M3U8 files in *folder* and its subfolders, as forward-slash paths."""
    if not folder.is_dir():
        return []
    found: list[str] = []
    for root, folders, files in os.walk(folder):
        folders[:] = sorted(name for name in folders if not name.startswith("."))
        for name in sorted(files, key=str.casefold):
            if name.casefold().endswith(PLAYLIST_SUFFIXES) and not name.startswith("."):
                relative = Path(root, name).relative_to(folder)
                found.append(relative.as_posix())
                if len(found) >= MAX_PLAYLIST_FILES:
                    return found
    return sorted(found, key=str.casefold)


def playlist_file(folder: Path, name: str) -> Path:
    """The file a playlist category reads, refused when it would leave *folder*."""
    _playlist_path(name)
    target = (folder / name).resolve()
    if not target.is_relative_to(folder.resolve()):
        raise ValueError(f"The playlist file {name} is outside the playlists folder")
    return target


# ----------------------------------------------------------------------
# Working out what a category holds
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """The library songs one category holds.

    Attributes:
        paths: The library paths of its songs.
        missing: Playlist lines that match no library track, the first
            :data:`MISSING_EXAMPLES` of them, as written.
        missing_count: How many playlist lines match no library track.
        fallback: Songs placed by ``year`` because beets has no first-release
            year for them (counted only when the query reads ``original_year``).
        error: Why the category could not be worked out, in words.
    """

    paths: frozenset[str] = frozenset()
    missing: tuple[str, ...] = ()
    missing_count: int = 0
    fallback: int = 0
    error: str | None = None

    @property
    def count(self) -> int:
        return len(self.paths)


def count_text(count: int) -> str:
    """Say a song count: "1 song", "1,284 songs"."""
    return f"{count:,} song" if count == 1 else f"{count:,} songs"


def fallback_text(count: int) -> str:
    """Say how many songs used the release year; empty for none."""
    if not count:
        return ""
    verb = "uses" if count == 1 else "use"
    return f"{count:,} {verb} the release year because the first-release year is missing"


def missing_text(count: int) -> str:
    """Say how many playlist lines match no library track; empty for none."""
    if not count:
        return ""
    return f"{count:,} playlist {'line matches' if count == 1 else 'lines match'} no library track"


def summary(resolution: Resolution) -> str:
    """One sentence about a category: its songs, missing lines, fallbacks or error."""
    if resolution.error:
        return resolution.error
    parts = [count_text(resolution.count)]
    parts += [text for text in (missing_text(resolution.missing_count),) if text]
    parts += [text for text in (fallback_text(resolution.fallback),) if text]
    return "; ".join(parts)


def library_item(entry: Any) -> dict[str, Any]:
    """The fields AutoDJ's own library holds for *entry*, as a query reads them."""
    return {name: getattr(entry, name, None) for name in LIBRARY_FIELDS}


@dataclass(frozen=True)
class BeetsSource:
    """The live beets database, and the folder its relative paths start in."""

    db_path: Path
    music_dir: Path | None = None

    def fields(self) -> frozenset[str]:
        """Every field beets holds for items, flexible attributes included."""
        from autodj.beets import item_fields

        return item_fields(self.db_path)

    def items(self, fields: Iterable[str]) -> dict[str, dict[str, Any]]:
        """The values of *fields* for every beets item, by AutoDJ's library path."""
        from autodj.beets import read_items
        from autodj.catalog import normalize_path

        found: dict[str, dict[str, Any]] = {}
        for path, values in read_items(self.db_path, fields):
            raw = str(path).replace("\\", "/")
            absolute = raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":")
            if not absolute and self.music_dir is not None:
                path = Path(self.music_dir) / raw.lstrip("/")
            found[normalize_path(path)] = values
        return found


def beets_source(cfg: AutoDJConfig) -> BeetsSource | None:
    """The configured beets database, or ``None`` when there is none."""
    db = cfg.library.beets_db
    return BeetsSource(Path(db), cfg.library.music_dir) if db else None


def read_playlist(path: Path) -> list[str]:
    """The track lines of an M3U or M3U8 file: no blanks, no ``#`` comments or tags.

    M3U8 is UTF-8; a plain M3U that is not UTF-8 is read as Windows-1252,
    the encoding Winamp-era playlists use.

    Raises:
        OSError: The file cannot be read.
        ValueError: The file is larger than :data:`MAX_PLAYLIST_BYTES`.
    """
    if path.stat().st_size > MAX_PLAYLIST_BYTES:
        raise ValueError(
            f"The playlist file {path.name} is larger than {MAX_PLAYLIST_BYTES // (1024 * 1024)} MB"
        )
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", "replace")
    lines = (line.strip() for line in text.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _line_candidates(line: str, base: Path, music_dir: Path | None) -> list[str]:
    """Where a playlist line may point: as written, then from the playlist or music folder."""
    if line.casefold().startswith("file://"):
        from urllib.parse import unquote, urlsplit
        from urllib.request import url2pathname

        line = url2pathname(unquote(urlsplit(line).path))
    spelled = line if os.sep == "\\" else line.replace("\\", "/")
    windows_absolute = len(spelled) >= 2 and spelled[1] == ":"
    if os.path.isabs(spelled) or (os.sep == "\\" and windows_absolute):
        return [spelled]
    candidates = [str(base / spelled)]
    if music_dir is not None:
        candidates.append(str(Path(music_dir) / spelled))
    return candidates


def resolve_playlist(path: Path, by_path: Mapping[str, Any], music_dir: Path | None) -> Resolution:
    """Match each line of the playlist at *path* to a library track.

    *by_path* maps normalized library paths to entries.  A relative line is
    tried from the playlist's own folder, then from the music folder (where
    beets keeps relative paths).  Web addresses and unknown files count as
    missing.
    """
    from autodj.catalog import normalize_path

    try:
        lines = read_playlist(path)
    except FileNotFoundError:
        return Resolution(error=f"The playlist file {path.name} is missing")
    except (OSError, ValueError) as exc:
        return Resolution(error=f"The playlist file {path.name} cannot be read: {exc}")
    paths: set[str] = set()
    missing: list[str] = []
    for line in lines:
        entry = None
        if "://" not in line or line.casefold().startswith("file://"):
            for candidate in _line_candidates(line, path.parent, music_dir):
                try:
                    entry = by_path.get(normalize_path(candidate))
                except (OSError, ValueError):
                    entry = None
                if entry is not None:
                    break
        if entry is None:
            missing.append(line)
        elif not getattr(entry, "is_silent", False):
            paths.add(entry.path)
    return Resolution(frozenset(paths), tuple(missing[:MISSING_EXAMPLES]), len(missing), 0, None)


def _unknown_field_error(query: Query, known: AbstractSet[str], beets: bool) -> str | None:
    unknown = sorted(query.named_fields() - known)
    if not unknown:
        return None
    where = "the library or the beets database" if beets else "the library"
    hint = (
        "beets fields look like original_year or albumartist"
        if beets
        else "set [library] beets_db in config.toml to use beets fields"
    )
    return f"No song in {where} has a field named {unknown[0]}. Check the spelling; {hint}"


def resolve_query(query: Query, items: Sequence[tuple[Any, Mapping[str, Any]]]) -> Resolution:
    """The songs among *items* (``(entry, fields)`` pairs) that *query* matches."""
    reads_year = query.uses_first_release_year()
    paths: set[str] = set()
    fallback = 0
    for entry, item in items:
        if not query.matches(item):
            continue
        paths.add(entry.path)
        if reads_year and _first_release_year(item)[1]:
            fallback += 1
    return Resolution(frozenset(paths), fallback=fallback)


@dataclass
class _Sources:
    """What a run of :func:`resolve_all` reads once and shares between categories."""

    entries: Sequence[Any]
    beets: BeetsSource | None
    music_dir: Path | None
    folder: Path
    by_path: dict[str, Any] | None = None
    items: list[tuple[Any, dict[str, Any]]] | None = None
    known: frozenset[str] = field(default_factory=frozenset)
    beets_error: str | None = None

    def lookup(self) -> dict[str, Any]:
        if self.by_path is None:
            from autodj.catalog import normalize_path

            self.by_path = {normalize_path(entry.path): entry for entry in self.entries}
        return self.by_path

    def read(self, fields: AbstractSet[str]) -> None:
        """Build the items the queries read, the beets values laid over the library's."""
        from autodj.catalog import normalize_path

        known = set(LIBRARY_FIELDS) | {FIRST_RELEASE_YEAR}
        overlay: dict[str, dict[str, Any]] = {}
        if self.beets is not None:
            try:
                known |= self.beets.fields()
                overlay = self.beets.items(fields)
            except (OSError, sqlite3.Error) as exc:
                self.beets_error = f"The beets database could not be read: {exc}"
        self.known = frozenset(known)
        items = []
        for entry in self.entries:
            if getattr(entry, "is_silent", False):
                continue
            item = library_item(entry)
            extra = overlay.get(normalize_path(entry.path)) if overlay else None
            if extra:
                item.update(extra)
            items.append((entry, item))
        self.items = items


def resolve_all(
    categories: Sequence[CategorySpec],
    entries: Sequence[Any],
    *,
    beets: BeetsSource | None,
    music_dir: Path | None,
    folder: Path,
) -> list[Resolution]:
    """Work out every category in *categories*, reading beets once for all of them."""
    sources = _Sources(entries, beets, music_dir, folder)
    queries = [spec.parsed_query() if spec.source == "query" else None for spec in categories]
    fields: set[str] = set()
    for query in queries:
        if query is not None:
            fields |= query.fields()
    if any(query is not None for query in queries):
        sources.read(fields)
    results: list[Resolution] = []
    for spec, query in zip(categories, queries, strict=True):
        if query is None:
            try:
                target = playlist_file(folder, spec.playlist)
            except (OSError, ValueError) as exc:
                results.append(Resolution(error=str(exc)))
                continue
            results.append(resolve_playlist(target, sources.lookup(), music_dir))
            continue
        if sources.beets_error:
            results.append(Resolution(error=sources.beets_error))
            continue
        problem = _unknown_field_error(query, sources.known, beets is not None)
        if problem:
            results.append(Resolution(error=problem))
            continue
        results.append(resolve_query(query, sources.items or []))
    return results


# ----------------------------------------------------------------------
# The live cache the player and the web routes share
# ----------------------------------------------------------------------


def _stat(path: Path | None) -> tuple[int, int] | None:
    if path is None:
        return None
    try:
        found = path.stat()
    except OSError:
        return None
    return found.st_mtime_ns, found.st_size


@dataclass(frozen=True)
class _Resolved:
    """The categories and what each holds, with what they were worked out from."""

    signature: tuple[Any, ...]
    entries: Sequence[Any]
    categories: tuple[Category, ...]
    results: dict[str, Resolution]


class CategoryIndex:
    """The saved categories and their songs, worked out again only when a source changes.

    The check before each use is a few ``stat`` calls: the categories file,
    the beets database (and its write-ahead log), each playlist file, and
    whether the library's song list is a new one.  Only when one of them
    changed does it read beets and the playlists again.

    Args:
        cfg: The configuration, for the index folder, beets and the
            playlists folder.
        entries: Returns the library's music entries; a new tuple each time
            the library changes, the same one otherwise.
    """

    def __init__(self, cfg: AutoDJConfig, entries: Callable[[], Sequence[Any]]) -> None:
        self._cfg = cfg
        self._entries = entries
        self.store = CategoryStore(store_path(cfg))
        self._lock = threading.Lock()
        self._resolved: _Resolved | None = None
        self._loaded: tuple[tuple[int, int] | None, tuple[Category, ...]] | None = None

    @property
    def folder(self) -> Path:
        return playlists_folder(self._cfg)

    def categories(self) -> tuple[Category, ...]:
        """The saved categories, read again only when the file changed."""
        stamp = _stat(self.store.path)
        if self._loaded is None or self._loaded[0] != stamp or stamp is None:
            self._loaded = (stamp, tuple(self.store.load()))
        return self._loaded[1]

    def save(self, categories: Sequence[Category]) -> None:
        """Write *categories* and forget what was worked out."""
        with self._lock:
            self.store.save(categories)
            self._loaded = None
            self._resolved = None

    def _signature(self, categories: Sequence[Category], entries: Sequence[Any]) -> tuple:
        folder = self.folder
        beets = self._cfg.library.beets_db
        uses_beets = beets is not None and any(c.source == "query" for c in categories)
        playlists = []
        for category in categories:
            if category.source == "m3u":
                try:
                    playlists.append(_stat(playlist_file(folder, category.playlist)))
                except (OSError, ValueError):
                    playlists.append(None)
        return (
            tuple(category.model_dump_json() for category in categories),
            len(entries),
            _stat(Path(beets)) if uses_beets and beets else None,
            _stat(Path(f"{beets}-wal")) if uses_beets and beets else None,
            tuple(playlists),
        )

    def resolutions(self) -> tuple[tuple[Category, ...], dict[str, Resolution]]:
        """The saved categories and what each holds, worked out again if a source changed."""
        with self._lock:
            categories = self.categories()
            entries = self._entries()
            signature = self._signature(categories, entries)
            resolved = self._resolved
            if (
                resolved is not None
                and resolved.signature == signature
                and resolved.entries is entries
            ):
                return resolved.categories, resolved.results
            results = resolve_all(
                categories,
                entries,
                beets=beets_source(self._cfg),
                music_dir=self._cfg.library.music_dir,
                folder=self.folder,
            )
            by_id: dict[str, Resolution] = {}
            previous = resolved.results if resolved is not None else {}
            before = {c.id: c for c in resolved.categories} if resolved is not None else {}
            for category, result in zip(categories, results, strict=True):
                if (
                    result.error
                    and before.get(category.id) == category
                    and previous[category.id].paths
                ):
                    # A beets database busy with an import, or a playlist being
                    # rewritten: keep playing the songs it held last time.
                    kept = previous[category.id]
                    result = Resolution(
                        kept.paths, kept.missing, kept.missing_count, kept.fallback, result.error
                    )
                by_id[category.id] = result
                if result.error:
                    logger.warning("Category %s: %s", category.name, result.error)
                elif not result.paths:
                    logger.warning("Category %s has no songs in the library", category.name)
            self._resolved = _Resolved(signature, entries, categories, by_id)
            return categories, by_id

    def members(self) -> dict[str, tuple[str, frozenset[str]]]:
        """Each category's ID mapped to its name and the paths of its songs."""
        categories, results = self.resolutions()
        return {category.id: (category.name, results[category.id].paths) for category in categories}

    def closed_now(self, wall: datetime) -> tuple[frozenset[str], dict[str, str]]:
        """The songs that may not play at local wall time *wall*, and why.

        Returns the paths of every song in a category whose days and hours
        leave *wall* out, and each such category's ID mapped to the reason
        in words.
        """
        categories, results = self.resolutions()
        paths: set[str] = set()
        reasons: dict[str, str] = {}
        for category in categories:
            if category.dayparted and not category.allows(wall):
                paths |= results[category.id].paths
                text = describe_daypart(category)
                reasons[category.id] = f"outside its hours: {text[0].lower()}{text[1:]}"
        return frozenset(paths), reasons

    def check(self, spec: CategorySpec) -> Resolution:
        """Work out an unsaved category, for the Check button."""
        return resolve_all(
            [spec],
            self._entries(),
            beets=beets_source(self._cfg),
            music_dir=self._cfg.library.music_dir,
            folder=self.folder,
        )[0]
