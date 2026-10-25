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

Each station keeps its categories in ``categories.json`` in its own folder
(versioned, written atomically; a file that cannot be read is set aside
and the station starts with none).  :class:`CategoryIndex` works out which
library songs each category holds and keeps the answer until the
categories, the beets database, a playlist file or the library changes,
which a few ``stat`` calls tell it; a clockwheel pick never re-reads them.

A year filter reads beets' first-release year (``original_year``), falling
back to ``year`` when beets has none; songs with neither are left out, and
the songs that used the fallback are counted so Settings and ``autodj
doctor`` can say how many.

A category may have allowed hours and days (dayparting), and a first and
last date (a season): outside them its songs are not picked in any pick
mode, and a clockwheel step for it is skipped.  The season may repeat every
week or every year (:mod:`autodj.date_repeat`).  The hours are wall-clock
times in the station time zone; a window that crosses midnight belongs to
the day it starts, for its days and its dates alike.  A song in several
categories plays only while every one of them allows it.  Clockwheel steps
take the same days and hours (:class:`StepHours`).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import date as Date
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
from autodj.clock_conditions import relative_date_now, uses_relative_dates
from autodj.date_repeat import DateRepeat, month_day, weekday_name
from autodj.date_repeat import contains as dates_contain
from autodj.date_repeat import problem as dates_problem
from autodj.fsutil import atomic_write, set_aside
from autodj.timefmt import clock_text

if TYPE_CHECKING:
    from autodj.beets_paths import PathCheck
    from autodj.config import AutoDJConfig
    from autodj.stations import StationPaths

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
    source: Literal["query", "m3u", "filters"]
    query: str = Field(default="", max_length=500)
    playlist: str = Field(default="", max_length=1024)
    conditions: list[dict[str, Any]] = Field(default_factory=list, max_length=32)
    match: Literal["all", "any"] = "all"
    # Dayparting: the weekdays (0 is Monday) and the hours the songs may
    # play.  No days means every day; no hours means all day.
    days: list[StrictInt] = Field(default_factory=list, max_length=7)
    hours_from: Time | None = None
    hours_until: Time | None = None
    # A season: the first and last day the songs may play, both included,
    # once or repeating every week or every year.
    not_before: Date | None = None
    not_after: Date | None = None
    dates_repeat: DateRepeat = "none"

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("A category name cannot hold control characters")
        return value

    @model_validator(mode="after")
    def _source(self) -> Self:
        if self.source == "filters":
            if self.query or self.playlist or not self.conditions:
                raise ValueError("A filter category needs conditions and no query or playlist")
            from autodj.clockwheel_store import _condition

            normalized = []
            for condition in self.conditions:
                item = _condition(condition)
                if item["field"] == "category":
                    raise ValueError("Categories cannot refer to other categories")
                normalized.append(item)
            self.conditions = normalized
            return self
        if self.conditions or self.match != "all":
            raise ValueError("Only a filter category can have conditions or Match any")
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
        self.days = check_daypart(self)
        return self

    @property
    def dayparted(self) -> bool:
        """Whether the category limits its songs to some days, hours or dates."""
        return limits_anything(self)

    def allows(self, wall: datetime) -> bool:
        """Whether the songs may play at the station's local wall time *wall*."""
        return daypart_refusal(self, wall) is None

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
    "conditions": "Conditions",
    "playlist": "Playlist file",
    "days": "Days",
    "hours_from": "Allowed from",
    "hours_until": "Allowed until",
    "not_before": "Not before",
    "not_after": "Do not play after",
    "dates_repeat": "Repeat dates",
}

_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


def check_daypart(item: Any) -> list[int]:
    """Check *item*'s days, hours and dates; return its days, sorted, each once.

    *item* has ``days``, ``hours_from`` and ``hours_until``, and may have
    ``not_before`` and ``not_after`` (a category; a clockwheel step has no
    dates).

    Raises:
        CategoryError: Names the field at fault and says why in words.
    """
    days = item.days
    if any(type(day) is not int or day not in range(7) for day in days):
        raise CategoryError("days", "Days must be numbered 0 (Monday) to 6 (Sunday)")
    for name in ("hours_from", "hours_until"):
        value = getattr(item, name)
        if value is not None and (value.tzinfo is not None or value.second or value.microsecond):
            raise CategoryError(name, f"{_FIELD_NAMES[name]} must be a whole minute")
    if (item.hours_from is None) != (item.hours_until is None):
        missing = "hours_until" if item.hours_until is None else "hours_from"
        raise CategoryError(
            missing, "Enter both Allowed from and Allowed until, or leave both empty"
        )
    if item.hours_from is not None and item.hours_from == item.hours_until:
        raise CategoryError("hours_until", "Allowed until must differ from Allowed from")
    first, last = getattr(item, "not_before", None), getattr(item, "not_after", None)
    found = dates_problem(first, last, getattr(item, "dates_repeat", "none"))
    if found == "needs_first":
        raise CategoryError("not_before", "Enter a Not before date to repeat the dates")
    if found == "backwards":
        raise CategoryError(
            "not_after", "Do not play after must be on or after the Not before date"
        )
    if found == "too_long":
        raise CategoryError(
            "not_after",
            "For Every week, Do not play after must be within 6 days of the Not before date",
        )
    return sorted(set(days))


def limits_anything(item: Any) -> bool:
    """Whether *item* (see :func:`check_daypart`) limits when its songs play."""
    return (
        bool(item.days)
        or item.hours_from is not None
        or getattr(item, "not_before", None) is not None
        or getattr(item, "not_after", None) is not None
    )


def daypart_refusal(item: Any, wall: datetime) -> str | None:
    """What of *item* leaves out local wall time *wall*, or ``None`` when it allows it.

    ``"hours"`` (outside its times), ``"days"`` (not one of its days) or
    ``"dates"`` (outside its season), the first that leaves it out.

    The start time is included and the end time is not.  A window that
    crosses midnight belongs to the day it starts, for the days and the
    dates alike, so a Friday 22:00 until 02:00 window whose last date is
    that Friday still runs at 01:00 on Saturday.
    """
    day = wall.date()
    if item.hours_from is not None and item.hours_until is not None:
        at = wall.time()
        if item.hours_from < item.hours_until:
            if not item.hours_from <= at < item.hours_until:
                return "hours"
        elif at < item.hours_until:
            day -= timedelta(days=1)  # the window that began yesterday
        elif at < item.hours_from:
            return "hours"
    if item.days and day.weekday() not in item.days:
        return "days"
    first, last = getattr(item, "not_before", None), getattr(item, "not_after", None)
    if not dates_contain(day, first, last, getattr(item, "dates_repeat", "none")):
        return "dates"
    return None


def _day_words(day: Date) -> str:
    return f"{day.day} {_MONTH_NAMES[day.month - 1]} {day.year}"


def describe_season(spec: Any) -> str:
    """Say a category's dates in words, after "Plays only"; empty for none."""
    first, last = getattr(spec, "not_before", None), getattr(spec, "not_after", None)
    repeat = getattr(spec, "dates_repeat", "none")
    if repeat == "weekly" and first is not None:
        starting = f"starting {_day_words(first)}"
        if last is None or last == first:
            return f"on {weekday_name(first)}s, {starting}"
        return f"every week from {weekday_name(first)} to {weekday_name(last)}, {starting}"
    if repeat == "yearly" and first is not None:
        if last is None or (last.month, last.day) == (first.month, first.day):
            return f"every year on {month_day(first)}"
        return f"every year between {month_day(first)} and {month_day(last)}"
    if first is not None and last is not None:
        return f"between {_day_words(first)} and {_day_words(last)}"
    if first is not None:
        return f"from {_day_words(first)}"
    if last is not None:
        return f"until {_day_words(last)}"
    return ""


class StepHours(BaseModel):
    """A clockwheel step's allowed days and hours: outside them the step is skipped.

    The same rule as a category's (see :func:`daypart_refusal`); no days
    means every day and no hours all day.
    """

    model_config = ConfigDict(extra="forbid")

    days: list[StrictInt] = Field(default_factory=list, max_length=7)
    hours_from: Time | None = None
    hours_until: Time | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        self.days = check_daypart(self)
        return self

    @property
    def limits_anything(self) -> bool:
        return limits_anything(self)

    def refusal(self, wall: datetime) -> str | None:
        """Why the step is skipped at *wall*, in words, or ``None`` when it may play."""
        refused = daypart_refusal(self, wall)
        if refused is None:
            return None
        text = describe_daypart(self)
        return f"outside its {refused}: {text[0].lower()}{text[1:]}"

    def to_json(self) -> dict[str, Any]:
        """The step keys, only those set: ``days``, ``hours_from``, ``hours_until``."""
        found: dict[str, Any] = {}
        if self.days:
            found["days"] = list(self.days)
        if self.hours_from is not None and self.hours_until is not None:
            found["hours_from"] = self.hours_from.strftime("%H:%M")
            found["hours_until"] = self.hours_until.strftime("%H:%M")
        return found


#: The keys a clockwheel step may carry for its days and hours.
STEP_HOURS_KEYS = ("days", "hours_from", "hours_until")


def parse_step_hours(step: Mapping[str, Any]) -> StepHours | None:
    """The days and hours a clockwheel step's JSON gives it, or ``None`` for none.

    Raises:
        ValueError: The days or hours are not valid; says which and why.
    """
    values = {key: step[key] for key in STEP_HOURS_KEYS if key in step}
    if not values:
        return None
    try:
        hours = StepHours.model_validate(values)
    except ValidationError as exc:
        found = spec_error(exc)
        raise ValueError(found.message) from None
    return hours if hours.limits_anything else None


def station_now(cfg: AutoDJConfig) -> datetime:
    """The station's local wall time, naive: ``[station] timezone`` or the system's."""
    import time

    return station_wall(cfg, time.time())


def station_wall(cfg: AutoDJConfig, timestamp: float) -> datetime:
    """The station's local wall time at Unix *timestamp*, naive (see :func:`station_now`)."""
    from zoneinfo import ZoneInfo

    from autodj.scheduler import system_timezone

    zone = ZoneInfo(cfg.station.timezone or system_timezone())
    return datetime.fromtimestamp(timestamp, zone).replace(tzinfo=None)


def describe_daypart(spec: Any) -> str:
    """Say a category's (or a step's) allowed days, hours and dates in words; empty for none."""
    if not limits_anything(spec):
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
    season = describe_season(spec)
    if spec.hours_from is None or spec.hours_until is None:
        if days != "every day":
            text = f"Plays only {days}"
        elif season:
            return f"Plays only {season}"
        else:
            # All seven days ticked and no hours: nothing is held back.
            return "Plays every day, at any hour"
    else:
        start, end = clock_text(spec.hours_from), clock_text(spec.hours_until)
        text = f"Plays only {days} from {start} until {end}"
    return f"{text}, {season}" if season else text


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
        folded = text.casefold()
        name = (
            "conditions"
            if "condition" in folded or "categor" in folded
            else "query"
            if "query" in folded
            else "playlist"
            if "playlist" in folded
            else ""
        )
    if error.get("type") in {"string_too_short", "missing"} and name == "name":
        message = "Enter a name for the category"
    elif error.get("type") == "string_too_long" and name:
        message = f"{_FIELD_NAMES[name]} is too long"
    elif error.get("type") == "literal_error" and name == "source":
        message = "Choose shared filters, a beets query or a playlist file as the source"
    elif error.get("type") == "literal_error" and name == "dates_repeat":
        message = "Repeat dates must be Don't repeat, Every week or Every year"
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
        set_aside(self.path, "categories file", reason)

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
            # Older AutoDJ releases reject unknown keys in the categories
            # file.  Filter-only defaults must not be written for legacy
            # query and playlist categories.
            if category["source"] != "filters":
                category.pop("conditions", None)
                category.pop("match", None)
            for key, empty in (
                ("days", []),
                ("hours_from", None),
                ("hours_until", None),
                ("not_before", None),
                ("not_after", None),
                ("dates_repeat", "none"),
            ):
                if category[key] == empty:
                    del category[key]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(document, indent=2, ensure_ascii=False) + "\n")


def store_path(cfg: AutoDJConfig, paths: StationPaths | None = None) -> Path:
    """Where a station keeps its categories; the default station's without *paths*."""
    from autodj.stations import StationPaths

    return (paths or StationPaths.default(cfg)).state_dir / FILE_NAME


def playlists_folder(cfg: AutoDJConfig) -> Path:
    """The folder M3U categories read from."""
    configured = cfg.library.playlists_folder
    return Path(configured) if configured else Path(cfg.index.shared_dir) / "playlists"


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
        from autodj.beets_paths import library_key

        return {
            library_key(str(path), self.music_dir): values
            for path, values in read_items(self.db_path, fields)
        }

    def checked_items(
        self, fields: Iterable[str], library: Sequence[str], *, remember_check: bool = True
    ) -> tuple[dict[str, dict[str, Any]], PathCheck]:
        """Like :meth:`items`, matched against the *library* paths and checked.

        A relative beets path is joined to ``music_dir``, or to the library
        folder that holds the paths when ``music_dir`` is unset or missing
        (see :func:`autodj.beets_paths.match`).  The check is remembered,
        and logged once when what it says changed.
        """
        from autodj.beets import read_items
        from autodj.beets_paths import match, remember

        rows = [(str(path), values) for path, values in read_items(self.db_path, fields)]
        found, check = match(rows, library, self.music_dir)
        if remember_check:
            remember(self.db_path, self.music_dir, check, len(library))
        return found, check


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
        from urllib.parse import urlsplit
        from urllib.request import url2pathname

        line = url2pathname(urlsplit(line).path)
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
    beets_items: dict[str, dict[str, Any]] = field(default_factory=dict)
    beets_check: PathCheck | None = None
    remember_check: bool = True
    # The library's shared Beets reader (autodj.clock_condition_data), if any.
    shared: Any = None

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
        if self.beets is not None and self.shared is not None:
            try:
                found = self.shared.read(fields, self.entries)
            except (OSError, sqlite3.Error) as exc:
                self.beets_error = f"The beets database could not be read: {exc}"
            else:
                if found is not None:
                    names, overlay, self.beets_check = found
                    known |= names
        elif self.beets is not None:
            try:
                known |= self.beets.fields()
                library = [
                    entry.path for entry in self.entries if not getattr(entry, "is_silent", False)
                ]
                overlay, self.beets_check = self.beets.checked_items(
                    fields, library, remember_check=self.remember_check
                )
            except (OSError, sqlite3.Error) as exc:
                self.beets_error = f"The beets database could not be read: {exc}"
        self.known = frozenset(known)
        self.beets_items = overlay
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
    sources_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    now: float | None = None,
) -> list[Resolution]:
    """Work out every category in *categories*, reading beets once for all of them.

    *now* is the time relative date conditions are judged at (now when not given).
    """
    return resolve_checked(
        categories,
        entries,
        beets=beets,
        music_dir=music_dir,
        folder=folder,
        sources_by_id=sources_by_id,
        now=now,
    )[0]


def uses_beets(category: CategorySpec) -> bool:
    """Whether *category* reads the beets database: a query, or a Beets field condition."""
    return category.source == "query" or (
        category.source == "filters" and any(c["field"] == "beets" for c in category.conditions)
    )


def resolve_checked(
    categories: Sequence[CategorySpec],
    entries: Sequence[Any],
    *,
    beets: BeetsSource | None,
    music_dir: Path | None,
    folder: Path,
    sources_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    remember_check: bool = True,
    now: float | None = None,
    shared_beets: Any = None,
) -> tuple[list[Resolution], PathCheck | None]:
    """Like :func:`resolve_all`, with how beets' paths matched the library.

    The check is ``None`` when no category read beets.  With
    *shared_beets* (:class:`~autodj.clock_condition_data.BeetsConditions`),
    beets is read through it, so the clockwheel and every station share
    one read.
    """
    sources = _Sources(
        entries, beets, music_dir, folder, remember_check=remember_check, shared=shared_beets
    )
    queries = [spec.parsed_query() if spec.source == "query" else None for spec in categories]
    fields: set[str] = set()
    for query in queries:
        if query is not None:
            fields |= query.fields()
    for spec in categories:
        fields.update(
            condition["name"] for condition in spec.conditions if condition["field"] == "beets"
        )
    if any(query is not None for query in queries) or any(
        c.source == "filters" and uses_beets(c) for c in categories
    ):
        sources.read(fields)
    results: list[Resolution] = []
    for spec, query in zip(categories, queries, strict=True):
        if spec.source == "m3u":
            try:
                target = playlist_file(folder, spec.playlist)
            except (OSError, ValueError) as exc:
                results.append(Resolution(error=str(exc)))
                continue
            results.append(resolve_playlist(target, sources.lookup(), music_dir))
            continue
        if sources.beets_error and (
            query is not None or any(c["field"] == "beets" for c in spec.conditions)
        ):
            results.append(Resolution(error=sources.beets_error))
            continue
        if query is not None:
            problem = _unknown_field_error(query, sources.known, beets is not None)
            if problem:
                results.append(Resolution(error=problem))
                continue
            results.append(resolve_query(query, sources.items or []))
            continue
        from autodj.clockwheel import ClockSlot, condition_paths
        from autodj.clockwheel_store import resolve_conditions

        try:
            step = {"type": "music", "conditions": spec.conditions, "match": spec.match}
            conditions = resolve_conditions(step, dict(sources_by_id or {}), 1)
            slot = ClockSlot(kind="music", conditions=conditions, match=spec.match)
        except (ValueError, KeyError) as exc:
            results.append(Resolution(error=f"A filter folder is unavailable: {exc}"))
            continue
        beets_by_path = sources.beets_items
        if any(c["field"] == "beets" for c in spec.conditions):
            if beets is None:
                results.append(
                    Resolution(error="Configure a Beets database to use Beets field conditions.")
                )
                continue
            problem = next(
                (
                    f"No song in the library has a field named {c['name']}"
                    for c in spec.conditions
                    if c["field"] == "beets" and c["name"] not in sources.known
                ),
                None,
            )
            if problem:
                results.append(Resolution(error=problem))
                continue
        music_entries = [entry for entry in entries if not getattr(entry, "is_silent", False)]
        results.append(
            Resolution(frozenset(condition_paths(slot, music_entries, beets_by_path, now=now)))
        )
    return results, sources.beets_check


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


def _rebuild_reason(previous: _Resolved | None, signature: tuple, entries: Sequence[Any]) -> str:
    """Why the categories were worked out again, in words, for the log (#584)."""
    if previous is None:
        return "first time since the start"
    old = previous.signature
    reasons = [
        text
        for changed, text in (
            (old[0] != signature[0], "the categories changed"),
            (previous.entries is not entries, "the library changed"),
            (old[2] != signature[2], "the music folders changed"),
            (old[3:5] != signature[3:5], "the Beets database changed"),
            (old[5] != signature[5], "a playlist file changed"),
            (old[6] != signature[6], "the hour moved on for relative dates"),
        )
        if changed
    ]
    return ", ".join(reasons) or "asked again"


#: What :meth:`CategoryIndex.closed_now` last worked out: the results it read,
#: the minute, the closed songs and why.
_Closed = tuple[dict[str, Resolution], datetime, frozenset[str], dict[str, str]]


class CategoryIndex:
    """The saved categories and their songs, worked out again only when a source changes.

    The check before each use is a few ``stat`` calls: the categories file,
    the beets database (and its write-ahead log), each playlist file, and
    whether the library's song list is a new one.  Only when one of them
    changed does it read beets and the playlists again.

    A category with a relative date condition (within the last N days) is
    also worked out again when the hour changes, as its window moves by the
    hour (:func:`autodj.clock_conditions.relative_date_now`).

    Args:
        cfg: The configuration, for the index folder, beets and the
            playlists folder.
        entries: Returns the library's music entries; a new tuple each time
            the library changes, the same one otherwise.
        clock: Returns the wall-clock time relative date conditions are
            judged at.
        beets: The library's :class:`~autodj.clock_condition_data.BeetsConditions`,
            which beets is read through when given.
    """

    def __init__(
        self,
        cfg: AutoDJConfig,
        entries: Callable[[], Sequence[Any]],
        folders: Callable[[], Sequence[Mapping[str, Any]]] = lambda: (),
        path: Path | None = None,
        clock: Callable[[], float] | None = None,
        beets: Any = None,
    ) -> None:
        self._cfg = cfg
        self._entries = entries
        self._folders = folders
        self._clock = clock or time.time
        # The library's shared Beets reader, so stations share one read.
        self._beets = beets
        # One station's categories: the default station's without *path*.
        self.store = CategoryStore(path or store_path(cfg))
        self._lock = threading.Lock()
        self._resolved: _Resolved | None = None
        self._loaded: tuple[tuple[int, int] | None, tuple[Category, ...]] | None = None
        # closed_now's answer, with the results and the minute it was worked out for.
        self._closed: _Closed | None = None

    @property
    def folder(self) -> Path:
        return playlists_folder(self._cfg)

    def categories(self) -> tuple[Category, ...]:
        """The saved categories, read again only when the file changed."""
        stamp = _stat(self.store.path)
        if self._loaded is None or self._loaded[0] != stamp or stamp is None:
            self._loaded = (stamp, tuple(self.store.load()))
        return self._loaded[1]

    def folders(self) -> list[dict[str, Any]]:
        """Registered music folders, with the IDs filter definitions store."""
        return [dict(source) for source in self._folders() if source.get("kind") == "music"]

    def save(self, categories: Sequence[Category]) -> None:
        """Write *categories* and forget what was worked out."""
        with self._lock:
            self.store.save(categories)
            self._loaded = None
            self._resolved = None

    def _signature(
        self, categories: Sequence[Category], entries: Sequence[Any], now: float | None = None
    ) -> tuple:
        folder = self.folder
        beets = self._cfg.library.beets_db
        reads_beets = beets is not None and any(uses_beets(c) for c in categories)
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
            tuple(
                (s.get("id"), s.get("kind"), s.get("path"), s.get("recursive"))
                for s in self._folders()
            ),
            _stat(Path(beets)) if reads_beets and beets else None,
            _stat(Path(f"{beets}-wal")) if reads_beets and beets else None,
            tuple(playlists),
            relative_date_now(self._clock() if now is None else now)
            if any(uses_relative_dates(c.conditions) for c in categories)
            else None,
        )

    def cache_signature(self) -> tuple:
        """Freshness key for category-dependent consumers without holding the pick lock for I/O."""
        with self._lock:
            categories = self.categories()
        entries = self._entries()
        return (id(entries), self._signature(categories, entries))

    def resolutions(self) -> tuple[tuple[Category, ...], dict[str, Resolution]]:
        """The saved categories and what each holds, worked out again if a source changed."""
        with self._lock:
            categories = self.categories()
            entries = self._entries()
            now = self._clock()
            signature = self._signature(categories, entries, now)
            resolved = self._resolved
            if (
                resolved is not None
                and resolved.signature == signature
                and resolved.entries is entries
            ):
                return resolved.categories, resolved.results
            started = time.perf_counter()
            results, check = resolve_checked(
                categories,
                entries,
                beets=beets_source(self._cfg),
                music_dir=self._cfg.library.music_dir,
                folder=self.folder,
                sources_by_id={source["id"]: source for source in self._folders()},
                now=now,
                shared_beets=self._beets,
            )
            cause = check.cause if check is not None else None
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
                elif not result.paths and cause and uses_beets(category):
                    logger.warning(
                        "Category %s has no songs in the library: %s", category.name, cause
                    )
                elif not result.paths:
                    logger.warning("Category %s has no songs in the library", category.name)
            self._resolved = _Resolved(signature, entries, categories, by_id)
            if categories:
                logger.info(
                    "Categories worked out for %d songs in %.2f s (%s)",
                    len(entries),
                    time.perf_counter() - started,
                    _rebuild_reason(resolved, signature, entries),
                )
            return categories, by_id

    def members(self) -> dict[str, tuple[str, frozenset[str]]]:
        """Each category's ID mapped to its name and the paths of its songs."""
        categories, results = self.resolutions()
        # Keep stale paths in ``resolutions`` for the UI to display during a
        # transient source failure, but never let playback treat them as current.
        return {
            category.id: (category.name, results[category.id].paths)
            for category in categories
            if not results[category.id].error
        }

    def closed_now(self, wall: datetime) -> tuple[frozenset[str], dict[str, str]]:
        """The songs that may not play at local wall time *wall*, and why.

        Returns the paths of every song in a category whose days and hours
        leave *wall* out, and each such category's ID mapped to the reason
        in words.

        Hours and days are whole minutes, so the answer holds for the whole
        minute of *wall*: it is kept until the minute or a category's songs
        change, and a pick only checks the cache.  While the closed songs
        stay the same, the same set is returned, so the player's own caches
        keyed on it hold too.
        """
        categories, results = self.resolutions()
        minute = wall.replace(second=0, microsecond=0)
        cached = self._closed
        if cached is not None and cached[0] is results and cached[1] == minute:
            return cached[2], dict(cached[3])
        paths: set[str] = set()
        reasons: dict[str, str] = {}
        for category in categories:
            refused = daypart_refusal(category, wall) if category.dayparted else None
            if refused:
                paths |= results[category.id].paths
                text = describe_daypart(category)
                reasons[category.id] = f"outside its {refused}: {text[0].lower()}{text[1:]}"
        closed = frozenset(paths)
        if cached is not None and cached[2] == closed:
            closed = cached[2]
        self._closed = (results, minute, closed, reasons)
        return closed, dict(reasons)

    def check(self, spec: CategorySpec) -> Resolution:
        """Work out an unsaved category, for the Check button."""
        return resolve_all(
            [spec],
            self._entries(),
            beets=beets_source(self._cfg),
            music_dir=self._cfg.library.music_dir,
            folder=self.folder,
            sources_by_id={source["id"]: source for source in self._folders()},
            now=self._clock(),
        )[0]
