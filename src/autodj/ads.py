"""Scheduled ad breaks with play reports (#51).

A station keeps *spots* (an audio file with its advertiser, campaign, flight
dates and play limits) and *ad breaks* (a name, the minutes past the hour it
runs, the days and hours it keeps to, and how many spots it may hold).  A
break also runs from a Schedule event (the ``ad_break`` action), a clockwheel
step, or from Run now in the Ads tab.

When a break falls due, :class:`AdDesk` picks its spots and books them, as
standalone breaks, on the station's open song change through the announcer
breaks' slot model (:mod:`autodj.breaks`): they are mixed in between the two
songs by the render-ahead worker, never by the audio thread, and air about a
song later.  A break is never cut into a song: it waits for a song change.

Picking a break's spots (:func:`choose`): a spot outside its flight dates, at
its daily or hourly limit, or within the separation time of a spot from the
same advertiser or the same competing category, is passed over; of the rest,
the one heard longest ago goes first.  One break never holds two spots of
one advertiser or one category, nor more spots or seconds than it allows.

Every spot booked, aired or missed is written to the station's ad play log
(:class:`AdLog`), one JSON line per change, appended and flushed to disk and
never rewritten, so a crash loses at most the line being written and a
damaged line costs only itself.  A spot booked when AutoDJ stopped is logged
as missed at the next start.  A break that cannot be booked within the
station's miss time (no song change could take it, the computer slept, no
spot could play) is logged as missed too, with the reason in words.  The
report (:func:`build_report`) reads the log back: what aired when, late or
on time, per advertiser and campaign, and what was missed.

Spots and breaks live in ``ads.json`` in the station's state folder
(:class:`AdStore`), the audio in its ``ads`` folder as ``spot-<id><ext>``.
One spot or break in the file that is not valid is skipped, logged and said
in the Ads tab, and written back as it was with every save, so the others
still air and nothing is lost.  A file that is not a file of ads at all is
set aside as ``ads.json.bad-<id>``; one that cannot be read just now is left
as it is and saves are refused.
"""

from __future__ import annotations

import calendar
import contextlib
import csv
import io
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as Date
from datetime import time as Time
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from autodj.clip_dates import ClipDates, day_words
from autodj.errors import FieldError
from autodj.fsutil import atomic_write, file_signature, fsync_directory, set_aside
from autodj.liners import LINER_EXTS
from autodj.timefmt import clock_text, length_text

if TYPE_CHECKING:
    from autodj.breaks import BreakDesk
    from autodj.config import AdsConfig

logger = logging.getLogger(__name__)

SCHEMA_VERSION: Literal[1] = 1
ADS_FILE = "ads.json"
#: The most ad breaks one station keeps.
MAX_BREAKS = 50
#: The most minutes past the hour one break runs at.
MAX_MARKS = 12
#: The most spots one break may hold.
MAX_SPOTS_PER_BREAK = 10
NAME_MAX = 100
#: The most missed runs logged one by one after a long sleep or stop; the
#: rest go into one line that says how many and when.
MAX_MISSED_PER_TICK = 200
#: The longest time AutoDJ was not running that the next start logs breaks for.
MAX_DOWNTIME = timedelta(days=35)
#: The station plans song changes this long before a break is due, so one is
#: open to book it; with no break due sooner, songs are picked as with ads off.
PLAN_AHEAD = timedelta(minutes=20)
#: How often the ad schedule's place is saved, so a start knows what it missed.
CURSOR_EVERY_SECONDS = 60.0
#: The file of the ad schedule's place, in the ad play log's folder.
CURSOR_FILE = "cursor.json"
#: Lines the log would not take at a stop, beside ``ads.json``, for the next start.
UNWRITTEN_FILE = "ad_plays-unwritten.jsonl"
#: Outcomes the Ads tab lists.
RECENT = 30
#: How far back the desk remembers plays for limits and separation.
HEARD_SPAN = timedelta(days=2)
#: Rows a report sends to the page; the CSV export has them all.
REPORT_ROWS = 1000
#: The longest span one report covers, in days.
MAX_REPORT_DAYS = 400

ID_PATTERN = r"^[0-9a-f]{32}$"
_ID = re.compile(r"[0-9a-f]{32}")
_EXTENSIONS = "|".join(ext.lstrip(".") for ext in LINER_EXTS)
_FILE = re.compile(rf"spot-([0-9a-f]{{32}})\.({_EXTENSIONS})")
_MONTH_FILE = re.compile(r"(\d{4})-(\d{2})\.jsonl")

#: Who the break desk logs as sending a booked spot.
AD_SENDER = "the ad schedule"
#: Where a run came from.
SOURCES: dict[str, str] = {
    "clock": "Minutes past the hour",
    "schedule": "Schedule event",
    "manual": "Run now",
    "clockwheel": "Clockwheel step",
}
#: A spot's state in the log, then the report's words for each.
Status = Literal["booked", "aired", "missed", "interrupted"]
BOOKED: Final = "booked"
AIRED: Final = "aired"
MISSED: Final = "missed"
#: Started on the air, then cut off by a skip or a stop: it may have aired.
INTERRUPTED: Final = "interrupted"
STATUS_WORDS = {
    BOOKED: "Waiting to air",
    AIRED: "Aired",
    "late": "Aired late",
    MISSED: "Missed",
    INTERRUPTED: "May have aired",
}
DAY_NAMES = tuple(calendar.day_name)


def new_id() -> str:
    """A fresh spot, break or run id: 32 lower-case hex digits."""
    return uuid.uuid4().hex


def valid_id(value: object) -> bool:
    """Whether *value* looks like an id of this module."""
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def spot_file_name(spot_id: str, suffix: str) -> str:
    """The ``ads`` folder's file name for a spot: ``spot-<id><suffix>``."""
    return f"spot-{spot_id}{suffix.lower()}"


class AdsError(FieldError):
    """A change the store refuses, naming the field at fault."""

    field: str

    def __init__(self, field_name: str, message: str, status: int = 422) -> None:
        super().__init__(message, field_name, status, null_empty_field=True)


def _clean(value: str) -> str:
    """*value* with control characters turned to spaces and runs of spaces joined."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())


Name = Annotated[StrictStr, StringConstraints(max_length=NAME_MAX)]


# ----------------------------------------------------------------------
# What the operator sets up
# ----------------------------------------------------------------------


FIELD_LABELS = {
    "title": "Spot title",
    "advertiser": "Advertiser",
    "campaign": "Campaign",
    "category": "Competing category",
    "start_date": "First day",
    "end_date": "Last day",
    "max_per_day": "Plays a day",
    "max_per_hour": "Plays an hour",
    "enabled": "Airs",
    "name": "Break name",
    "minutes": "Minutes past the hour",
    "days": "Days",
    "from_hour": "From hour",
    "until_hour": "Until hour",
    "max_spots": "Spots in the break",
    "max_seconds": "Longest break",
    "separation_minutes": "Separation",
    "late_minutes": "Late after",
    "miss_after_minutes": "Missed after",
}


class SpotFields(BaseModel):
    """What the operator says about a spot."""

    model_config = ConfigDict(extra="forbid")

    title: Name
    advertiser: Name
    campaign: Name = ""
    category: Name = ""
    start_date: Date | None = None
    end_date: Date | None = None
    max_per_day: StrictInt = Field(default=0, ge=0, le=500)
    max_per_hour: StrictInt = Field(default=0, ge=0, le=60)
    enabled: StrictBool = True

    @field_validator("title", "advertiser", "campaign", "category")
    @classmethod
    def _text(cls, value: str) -> str:
        return _clean(value)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.title:
            raise AdsError("title", "Enter a title for the spot")
        if not self.advertiser:
            raise AdsError("advertiser", "Enter the advertiser")
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise AdsError("end_date", "The last day must be on or after the first day")
        return self

    def flight(self) -> ClipDates:
        """The spot's flight dates."""
        return ClipDates(not_before=self.start_date, not_after=self.end_date)


class Spot(SpotFields):
    """A stored spot: what the operator said, its file and where its sound is."""

    file: StrictStr
    cue_start: float = Field(ge=0, allow_inf_nan=False)
    cue_segue: float = Field(ge=0, allow_inf_nan=False)
    cue_end: float = Field(ge=0, allow_inf_nan=False)
    length_s: float = Field(ge=0, allow_inf_nan=False)
    created_at: float = Field(default=0.0, allow_inf_nan=False)

    @property
    def seconds(self) -> float:
        """How long it plays: from its first sound to the end of its speech."""
        return max(0.0, self.cue_segue - self.cue_start)

    @property
    def label(self) -> str:
        """``Advertiser: title``, for logs, the queue and the hour preview."""
        return f"{self.advertiser}: {self.title}"


class BreakFields(BaseModel):
    """What the operator says about an ad break."""

    model_config = ConfigDict(extra="forbid")

    name: Name
    minutes: list[StrictInt] = Field(default_factory=list, max_length=MAX_MARKS)
    days: list[StrictInt] = Field(default_factory=list, max_length=7)
    from_hour: StrictInt = Field(default=0, ge=0, le=23)
    until_hour: StrictInt = Field(default=24, ge=1, le=24)
    start_date: Date | None = None
    end_date: Date | None = None
    max_spots: StrictInt = Field(default=3, ge=1, le=MAX_SPOTS_PER_BREAK)
    max_seconds: StrictInt = Field(default=0, ge=0, le=900)
    enabled: StrictBool = True

    @field_validator("name")
    @classmethod
    def _text(cls, value: str) -> str:
        return _clean(value)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.name:
            raise AdsError("name", "Enter a name for the break")
        if any(not 0 <= minute <= 59 for minute in self.minutes):
            raise AdsError("minutes", "Minutes past the hour must be from 0 to 59")
        self.minutes = sorted(set(self.minutes))
        if any(not 0 <= day <= 6 for day in self.days):
            raise AdsError("days", "Days must be numbered 0 (Monday) to 6 (Sunday)")
        self.days = sorted(set(self.days))
        if self.until_hour <= self.from_hour:
            raise AdsError("until_hour", "Until hour must be after From hour")
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise AdsError("end_date", "The last day must be on or after the first day")
        if self.max_seconds and self.max_seconds < 10:
            raise AdsError(
                "max_seconds", "The longest break must be 0 (no limit) or 10 seconds or more"
            )
        return self

    def runs_on(self, day: Date) -> bool:
        """Whether the break runs on the local *day*."""
        if self.start_date is not None and day < self.start_date:
            return False
        if self.end_date is not None and day > self.end_date:
            return False
        return not self.days or day.weekday() in self.days

    def when_text(self) -> str:
        """When the break runs by itself, in words."""
        if not self.minutes:
            return "Only from a Schedule event or Run now"
        marks = ", ".join(f":{minute:02d}" for minute in self.minutes)
        text = f"At {marks} past the hour"
        if self.from_hour or self.until_hour != 24:
            until = "midnight" if self.until_hour == 24 else f"{self.until_hour:02d}:00"
            text += f", from {self.from_hour:02d}:00 until {until}"
        if self.days:
            names = [DAY_NAMES[day] for day in self.days]
            text += ", on " + (
                names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
            )
        dates = ClipDates(not_before=self.start_date, not_after=self.end_date).describe()
        if dates:
            text += f". {dates.replace('Plays', 'Runs', 1)}"
        return text


class AdBreak(BreakFields):
    """A stored ad break."""

    created_at: float = Field(default=0.0, allow_inf_nan=False)


class AdSettings(BaseModel):
    """The station's rules for every break."""

    model_config = ConfigDict(extra="forbid")

    separation_minutes: StrictInt = Field(default=15, ge=0, le=1440)
    late_minutes: StrictInt = Field(default=10, ge=1, le=240)
    miss_after_minutes: StrictInt = Field(default=30, ge=5, le=720)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.miss_after_minutes < self.late_minutes:
            raise AdsError("miss_after_minutes", "Missed after must not be shorter than Late after")
        return self


class _AdsFile(BaseModel):
    """The file's frame; each spot and break in it is checked on its own."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    settings: dict[str, Any] = Field(default_factory=dict)
    spots: dict[str, Any] = Field(default_factory=dict)
    breaks: dict[str, Any] = Field(default_factory=dict)


def _spot_entry(spot_id: str, raw: Any) -> Spot:
    if not valid_id(spot_id):
        raise ValueError("its ID is not 32 hexadecimal digits")
    spot = Spot.model_validate(raw)
    found = _FILE.fullmatch(spot.file)
    if found is None or found.group(1) != spot_id:
        raise ValueError("it does not name its own file")
    return spot


def _break_entry(break_id: str, raw: Any) -> AdBreak:
    if not valid_id(break_id):
        raise ValueError("its ID is not 32 hexadecimal digits")
    return AdBreak.model_validate(raw)


@dataclass(frozen=True)
class AdState:
    """A copy of the store: the settings, the spots and the breaks, by id."""

    settings: AdSettings
    spots: dict[str, Spot]
    breaks: dict[str, AdBreak]

    def break_named(self, name: str) -> tuple[str, AdBreak] | None:
        """The break called *name* (case does not matter), with its id."""
        wanted = name.strip().casefold()
        return next(
            ((key, item) for key, item in self.breaks.items() if item.name.casefold() == wanted),
            None,
        )


class AdStore:
    """``ads.json``: one station's spots, ad breaks and settings.

    The file is read again when it changes on disk.  Every change is
    read, made and written under one lock, atomically.
    """

    def __init__(self, path: Path, folder: Path, *, max_spots: Callable[[], int] = lambda: 200):
        """Keep the ads in *path*, their audio in *folder*."""
        self.path = Path(path)
        self.folder = Path(folder)
        self._max_spots = max_spots
        self._lock = threading.Lock()
        self._stamp: tuple[int, int, int] | None = None
        self._state = AdState(AdSettings(), {}, {})
        self._skipped: dict[str, dict[str, Any]] = {"spots": {}, "breaks": {}}
        self._settings_problem = False
        self._seen = False
        #: Goes up with every change this store writes.
        self.revision = 0

    # -- the file ----------------------------------------------------------

    def _stat(self) -> tuple[int, int, int] | None:
        return file_signature(self.path)

    def _keep_bad_copy(self, reason: str) -> None:
        set_aside(self.path, "ads file", reason)

    def _load_locked(self) -> AdState:
        """Every spot and break, read again if the file changed.  Caller holds ``_lock``.

        Raises:
            OSError: The file could not be looked at or read.
        """
        stamp = self._stat()
        if stamp is not None:
            self._seen = True
        if stamp == self._stamp:
            return self._state
        settings = AdSettings()
        spots: dict[str, Spot] = {}
        breaks: dict[str, AdBreak] = {}
        skipped: dict[str, dict[str, Any]] = {"spots": {}, "breaks": {}}
        settings_problem = False
        if stamp is not None:
            data = self.path.read_bytes()
            try:
                document = _AdsFile.model_validate_json(data)
            except ValueError as exc:
                # Not a file of ads at all: set aside whole.
                self._keep_bad_copy(str(exc).splitlines()[0])
                stamp = None
            else:
                try:
                    settings = AdSettings.model_validate(document.settings)
                except ValueError as exc:
                    settings_problem = True
                    logger.error(
                        "Ads file %s: its settings are not valid (%s); using the defaults",
                        self.path,
                        str(exc).splitlines()[0],
                    )
                # One bad spot or break never silences the others: it is skipped.
                for kind, entries, check in (
                    ("spots", document.spots, _spot_entry),
                    ("breaks", document.breaks, _break_entry),
                ):
                    for key, raw in entries.items():
                        try:
                            item = check(key, raw)
                        except ValueError as exc:
                            skipped[kind][key] = raw
                            logger.error(
                                "Ads file %s: skipping %s %r, which is not valid (%s)",
                                self.path,
                                kind[:-1],
                                key[:40],
                                str(exc).splitlines()[0],
                            )
                            continue
                        if kind == "spots":
                            spots[key] = item  # type: ignore[assignment]
                        else:
                            breaks[key] = item  # type: ignore[assignment]
        self._stamp = stamp
        self._state = AdState(settings, spots, breaks)
        self._skipped = skipped
        self._settings_problem = settings_problem
        return self._state

    def _write_locked(self, state: AdState) -> None:
        """Write *state*, with the skipped entries as they were read.  Caller holds ``_lock``."""
        document: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "settings": state.settings.model_dump(mode="json"),
            "spots": {
                key: spot.model_dump(mode="json", exclude_none=True)
                for key, spot in state.spots.items()
            },
            "breaks": {
                key: item.model_dump(mode="json", exclude_none=True)
                for key, item in state.breaks.items()
            },
        }
        for kind in ("spots", "breaks"):
            for key, raw in self._skipped[kind].items():
                document[kind].setdefault(key, raw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        self._seen = True
        self._settings_problem = False
        try:
            stamp = self._stat()
        except OSError:
            stamp = None
        self._stamp, self._state = stamp, state
        self.revision += 1

    def _first_folder_locked(self) -> None:
        if not self._seen and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def state(self) -> AdState:
        """Every spot and break, read again if the file changed.

        When the file cannot be read just now, what was last read is used
        and a warning logged; a change then refuses, so nothing is written
        over the file.
        """
        with self._lock:
            try:
                return self._load_locked()
            except OSError as exc:
                logger.warning("Could not read the ads file %s: %s", self.path, exc)
                return self._state

    def cached(self) -> AdState:
        """What was last read, with no disk access and no wait: safe on any thread."""
        return self._state

    def problem(self) -> str | None:
        """What is wrong with the file, in words, or ``None``."""
        with self._lock:
            spots = sorted(self._skipped["spots"])
            breaks = sorted(self._skipped["breaks"])
            settings = self._settings_problem
        parts = []
        for count, noun in ((len(spots), "spot"), (len(breaks), "ad break")):
            if count:
                parts.append(f"{count} {noun}{'' if count == 1 else 's'}")
        if not parts and not settings:
            return None
        text = ""
        if parts:
            text = (
                f"{' and '.join(parts)} in {self.path.name} "
                f"{'is' if len(spots) + len(breaks) == 1 else 'are'} not valid and skipped until "
                "the file is corrected. AutoDJ keeps them in the file as written."
            )
        if settings:
            text = (text + " " if text else "") + (
                f"The settings in {self.path.name} are not valid, so the defaults are used "
                "until they are saved again."
            )
        return text

    def _change(self, make: Callable[[AdState], AdState]) -> AdState:
        """Read, change with *make* and write, under the lock.

        Raises:
            AdsError: *make* refuses.
            OSError: The file could not be read or written; nothing is saved.
        """
        with self._lock:
            self._first_folder_locked()
            current = self._load_locked()
            changed = make(current)
            self._write_locked(changed)
            return changed

    # -- changes ------------------------------------------------------------

    def add_spot(self, spot_id: str, spot: Spot) -> None:
        """Save a new spot.

        Raises:
            AdsError: The station has ``[ads] max_spots`` already.
            OSError: As :meth:`_change`.
        """
        limit = self._max_spots()

        def make(state: AdState) -> AdState:
            if len(state.spots) + len(self._skipped["spots"]) >= limit:
                raise AdsError("file", f"This station keeps at most {limit} spots", 409)
            return AdState(state.settings, {**state.spots, spot_id: spot}, state.breaks)

        self._change(make)

    def update_spot(self, spot_id: str, fields: SpotFields) -> Spot:
        """Change what is said about a spot.

        Raises:
            KeyError: No spot has that id.
            OSError: As :meth:`_change`.
        """
        updated: list[Spot] = []

        def make(state: AdState) -> AdState:
            old = state.spots[spot_id]
            spot = Spot.model_validate({**old.model_dump(), **fields.model_dump()})
            updated.append(spot)
            return AdState(state.settings, {**state.spots, spot_id: spot}, state.breaks)

        self._change(make)
        return updated[0]

    def remove_spot(self, spot_id: str) -> Spot | None:
        """Forget a spot; the one forgotten, or ``None`` when there was none."""
        removed: list[Spot] = []

        def make(state: AdState) -> AdState:
            spots = dict(state.spots)
            spot = spots.pop(spot_id, None)
            if spot is None:
                raise KeyError(spot_id)
            removed.append(spot)
            return AdState(state.settings, spots, state.breaks)

        try:
            self._change(make)
        except KeyError:
            return None
        return removed[0]

    def _name_free(self, state: AdState, name: str, break_id: str) -> None:
        found = state.break_named(name)
        if found is not None and found[0] != break_id:
            raise AdsError("name", f"An ad break is already called {found[1].name}", 409)

    def add_break(self, break_id: str, fields: BreakFields) -> AdBreak:
        """Save a new ad break.

        Raises:
            AdsError: Too many breaks, or another has that name.
            OSError: As :meth:`_change`.
        """
        item = AdBreak(**fields.model_dump(), created_at=time.time())

        def make(state: AdState) -> AdState:
            if len(state.breaks) + len(self._skipped["breaks"]) >= MAX_BREAKS:
                raise AdsError("name", f"This station keeps at most {MAX_BREAKS} ad breaks", 409)
            self._name_free(state, item.name, break_id)
            return AdState(state.settings, state.spots, {**state.breaks, break_id: item})

        self._change(make)
        return item

    def update_break(self, break_id: str, fields: BreakFields) -> AdBreak:
        """Change an ad break.

        Raises:
            KeyError: No break has that id.
            AdsError: Another break has the new name.
            OSError: As :meth:`_change`.
        """
        updated: list[AdBreak] = []

        def make(state: AdState) -> AdState:
            old = state.breaks[break_id]
            self._name_free(state, fields.name, break_id)
            item = AdBreak(**fields.model_dump(), created_at=old.created_at)
            updated.append(item)
            return AdState(state.settings, state.spots, {**state.breaks, break_id: item})

        self._change(make)
        return updated[0]

    def remove_break(self, break_id: str) -> AdBreak | None:
        """Forget an ad break; the one forgotten, or ``None``."""
        removed: list[AdBreak] = []

        def make(state: AdState) -> AdState:
            breaks = dict(state.breaks)
            item = breaks.pop(break_id, None)
            if item is None:
                raise KeyError(break_id)
            removed.append(item)
            return AdState(state.settings, state.spots, breaks)

        try:
            self._change(make)
        except KeyError:
            return None
        return removed[0]

    def set_settings(self, settings: AdSettings) -> None:
        """Save the station's rules for every break."""
        self._change(lambda state: AdState(settings, state.spots, state.breaks))

    def delete_file(self, spot: Spot) -> None:
        """Delete a forgotten spot's audio; a file that will not go is logged."""
        if _FILE.fullmatch(spot.file) is None:
            return
        try:
            (self.folder / spot.file).unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not delete the spot file %s", spot.file, exc_info=True)


# ----------------------------------------------------------------------
# The play log
# ----------------------------------------------------------------------


class PlayRecord(BaseModel):
    """One line of the ad play log: a spot booked, aired or missed, or a run that held none.

    A spot's later line (aired or missed) replaces its booked one; a run
    that could hold no spot has an empty ``spot_id`` and its own id.
    """

    model_config = ConfigDict(extra="ignore")

    v: Literal[1] = 1
    id: Annotated[StrictStr, StringConstraints(pattern=ID_PATTERN)]
    run: Annotated[StrictStr, StringConstraints(pattern=ID_PATTERN)]
    break_id: StrictStr = ""
    break_name: StrictStr = ""
    source: StrictStr = "clock"
    scheduled: datetime
    spot_id: StrictStr = ""
    title: StrictStr = ""
    advertiser: StrictStr = ""
    campaign: StrictStr = ""
    category: StrictStr = ""
    seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    status: Status
    reason: StrictStr = ""
    #: How many breaks a missed line stands for (one, or a summary after a
    #: long sleep or stop), the last of them at ``last_scheduled``.
    count: int = Field(default=1, ge=1)
    last_scheduled: datetime | None = None
    aired_at: datetime | None = None
    logged: datetime

    @field_validator("scheduled", "aired_at", "logged", "last_scheduled")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("times must carry their UTC offset")
        return value

    def delay_s(self) -> float | None:
        """Seconds from its time to its airing, or ``None`` when it did not air."""
        if self.status != AIRED or self.aired_at is None:
            return None
        return (self.aired_at - self.scheduled).total_seconds()


def _month(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m")


class AdLog:
    """The ad play log: ``ad_plays/YYYY-MM.jsonl``, by the UTC month a line was written.

    Lines are only ever appended, each flushed to disk before the next.
    Reading skips a line that is not a record and counts it.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self._lock = threading.Lock()

    def append(self, records: Sequence[PlayRecord]) -> None:
        """Add *records* to the end of their months' files and flush them.

        Raises:
            OSError: A file could not be written.
        """
        if not records:
            return
        by_month: dict[str, list[str]] = {}
        for record in records:
            line = record.model_dump_json(exclude_none=True)
            by_month.setdefault(_month(record.logged), []).append(line)
        with self._lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            for month, lines in by_month.items():
                path = self.folder / f"{month}.jsonl"
                new = not path.exists()
                with open(path, "ab+") as handle:
                    # A line cut short by a crash is closed first, so the
                    # next record starts on its own line.
                    handle.seek(0, os.SEEK_END)
                    if handle.tell():
                        handle.seek(-1, os.SEEK_END)
                        if handle.read(1) != b"\n":
                            handle.write(b"\n")
                    handle.write(("\n".join(lines) + "\n").encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                if new:
                    # The new file's name must survive a power cut too.
                    fsync_directory(self.folder)

    def cursor(self) -> float | None:
        """When the ad schedule last looked, saved; ``None`` when unknown.

        Raises:
            OSError: The file is there but could not be read.
        """
        path = self.folder / CURSOR_FILE
        try:
            data = json.loads(path.read_bytes())
            stamp = datetime.fromisoformat(data["cursor"])
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError, RecursionError):
            logger.warning("The ad schedule's place in %s is not valid; starting from now", path)
            return None
        if stamp.tzinfo is None:
            return None
        return stamp.timestamp()

    def save_cursor(self, now: float) -> None:
        """Save when the ad schedule last looked, atomically.

        Raises:
            OSError: It could not be written.
        """
        self.folder.mkdir(parents=True, exist_ok=True)
        document = {"cursor": datetime.fromtimestamp(now, UTC).isoformat()}
        atomic_write(self.folder / CURSOR_FILE, json.dumps(document) + "\n")

    def forget_cursor(self) -> None:
        """Forget the saved place (ads are off: no break is owed)."""
        (self.folder / CURSOR_FILE).unlink(missing_ok=True)

    def months(self) -> list[str]:
        """The months the log has files for, oldest first."""
        try:
            names = [path.name for path in self.folder.iterdir()]
        except FileNotFoundError:
            return []
        return sorted(name[:7] for name in names if _MONTH_FILE.fullmatch(name))

    def read(self, start: datetime, end: datetime) -> tuple[list[PlayRecord], int]:
        """The lines written from about *start* to *end*, and how many could not be read.

        Every month file that may hold a line about a time in the span is
        read (a spot's outcome is written after its time, so a day either
        side): the caller filters by time.

        Raises:
            OSError: A file is there but could not be read.
        """
        first = _month(start - timedelta(days=2))
        last = _month(end + timedelta(days=2))
        records: list[PlayRecord] = []
        damaged = 0
        with self._lock:
            months = [month for month in self.months() if first <= month <= last]
            for month in months:
                path = self.folder / f"{month}.jsonl"
                with open(path, "rb") as handle:
                    for raw in handle:
                        text = raw.strip()
                        if not text:
                            continue
                        try:
                            records.append(PlayRecord.model_validate_json(text))
                        except ValueError:
                            damaged += 1
        if damaged:
            logger.warning(
                "The ad play log in %s has %d line%s that could not be read; they are skipped",
                self.folder,
                damaged,
                "" if damaged == 1 else "s",
            )
        return records, damaged


def latest(records: Iterable[PlayRecord]) -> dict[str, PlayRecord]:
    """Each spot's last line, by id: an outcome replaces its booking."""
    found: dict[str, PlayRecord] = {}
    for record in records:
        before = found.get(record.id)
        if before is None or before.status == BOOKED or record.status != BOOKED:
            found[record.id] = record
    return found


# ----------------------------------------------------------------------
# Picking a break's spots
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Heard:
    """A spot that aired or is booked, for limits and separation.

    ``at`` is the time of the break it was in, in wall-clock seconds.
    """

    spot_id: str
    advertiser: str
    category: str
    at: float


@dataclass(frozen=True)
class Choice:
    """The spots picked for a break, or why none could play."""

    spots: tuple[tuple[str, Spot], ...]
    reason: str = ""

    @property
    def seconds(self) -> float:
        return sum(spot.seconds for _key, spot in self.spots)


def _key(text: str) -> str:
    return text.strip().casefold()


def choose(
    state: AdState,
    item: AdBreak,
    at: float,
    zone: ZoneInfo,
    heard: Sequence[Heard],
    *,
    usable: Callable[[str, Spot], bool] = lambda _key, _spot: True,
    advertiser: str = "",
    category: str = "",
) -> Choice:
    """The spots for *item* running at *at*, in the order they play.

    Pure: nothing is changed.  *heard* is what aired or is booked recently;
    *usable* says whether a spot's file can be played.
    """
    local = datetime.fromtimestamp(at, zone)
    today = local.date()
    hour_start = local.replace(minute=0, second=0, microsecond=0)
    separation = state.settings.separation_minutes * 60
    day_counts: dict[str, int] = {}
    hour_counts: dict[str, int] = {}
    last: dict[str, float] = {}
    near_advertisers: set[str] = set()
    near_categories: set[str] = set()
    for play in heard:
        when = datetime.fromtimestamp(play.at, zone)
        if when.date() == today:
            day_counts[play.spot_id] = day_counts.get(play.spot_id, 0) + 1
            if when.replace(minute=0, second=0, microsecond=0) == hour_start:
                hour_counts[play.spot_id] = hour_counts.get(play.spot_id, 0) + 1
        last[play.spot_id] = max(last.get(play.spot_id, float("-inf")), play.at)
        if play.at == at or (separation and abs(play.at - at) < separation):
            near_advertisers.add(_key(play.advertiser))
            if play.category.strip():
                near_categories.add(_key(play.category))
    if not state.spots and not (advertiser or category):
        return Choice((), "the station has no spots")
    filters = []
    if advertiser:
        filters.append(f"from advertiser {advertiser}")
    if category:
        filters.append(f"in category {category}")
    matched = {
        key: spot
        for key, spot in state.spots.items()
        if (not advertiser or _key(spot.advertiser) == _key(advertiser))
        and (not category or _key(spot.category) == _key(category))
    }
    if not matched:
        if advertiser and not any(
            _key(s.advertiser) == _key(advertiser) for s in state.spots.values()
        ):
            return Choice((), f"no spot is from advertiser {advertiser}")
        if category and not any(_key(s.category) == _key(category) for s in state.spots.values()):
            return Choice((), f"no spot is in category {category}")
        return Choice((), f"no spot is {' and '.join(filters)}")
    candidates: list[tuple[str, Spot]] = []
    passed: dict[str, int] = {}

    def skip(why: str) -> None:
        passed[why] = passed.get(why, 0) + 1

    for key, spot in matched.items():
        if not spot.enabled:
            skip("turned off")
        elif spot.flight().refusal(today) is not None:
            skip("outside its flight dates")
        elif spot.max_per_day and day_counts.get(key, 0) >= spot.max_per_day:
            skip("at its daily limit")
        elif spot.max_per_hour and hour_counts.get(key, 0) >= spot.max_per_hour:
            skip("at its hourly limit")
        elif _key(spot.advertiser) in near_advertisers or (
            spot.category.strip() and _key(spot.category) in near_categories
        ):
            skip("within the separation time of a competing spot")
        elif not usable(key, spot):
            skip("missing its audio file")
        else:
            candidates.append((key, spot))
    candidates.sort(
        key=lambda pair: (
            last.get(pair[0], float("-inf")),
            day_counts.get(pair[0], 0),
            pair[1].title.casefold(),
            pair[0],
        )
    )
    chosen: list[tuple[str, Spot]] = []
    advertisers: set[str] = set()
    categories: set[str] = set()
    total = 0.0
    too_long = False
    for key, spot in candidates:
        if len(chosen) >= item.max_spots:
            break
        if _key(spot.advertiser) in advertisers:
            continue
        category = _key(spot.category)
        if category and category in categories:
            continue
        if item.max_seconds and total + spot.seconds > item.max_seconds:
            too_long = True
            continue
        chosen.append((key, spot))
        advertisers.add(_key(spot.advertiser))
        if category:
            categories.add(category)
        total += spot.seconds
    if chosen:
        return Choice(tuple(chosen))
    if too_long:
        reason = f"every spot that could play is longer than {item.max_seconds} seconds"
        return Choice(
            (), f"no spot {' and '.join(filters)} could play: {reason}" if filters else reason
        )
    words = "; ".join(
        f"{count} {'spot is' if count == 1 else 'spots are'} {why}"
        for why, count in sorted(passed.items(), key=lambda pair: -pair[1])
    )
    prefix = f"no spot {' and '.join(filters)} could play" if filters else "no spot could play"
    return Choice((), f"{prefix}: {words}" if words else prefix)


# ----------------------------------------------------------------------
# When breaks run
# ----------------------------------------------------------------------


def _resolve(day: Date, at: Time, zone: ZoneInfo) -> datetime:
    from autodj.scheduler import _resolve as resolve

    return resolve(day, at, zone)


def marks_between(item: AdBreak, after: float, until: float, zone: ZoneInfo) -> list[float]:
    """The times *item* runs by its minutes past the hour, after *after* and up to *until*.

    Wall times are read in *zone*; a time a daylight-saving change skips
    runs as the clock jumps, one that happens twice runs once.
    """
    if not item.minutes or until <= after:
        return []
    found: list[float] = []
    start = datetime.fromtimestamp(after, zone).replace(minute=0, second=0, microsecond=0)
    hour = start.replace(tzinfo=None)
    last = datetime.fromtimestamp(until, zone).replace(tzinfo=None) + timedelta(hours=1)
    seen: set[float] = set()
    while hour <= last:
        if item.from_hour <= hour.hour < item.until_hour and item.runs_on(hour.date()):
            for minute in item.minutes:
                moment = _resolve(hour.date(), Time(hour.hour, minute), zone).timestamp()
                if after < moment <= until and moment not in seen:
                    seen.add(moment)
                    found.append(moment)
        hour += timedelta(hours=1)
    return sorted(found)


@dataclass
class Run:
    """A break that fell due and waits for a song change."""

    run_id: str
    break_id: str
    break_name: str
    scheduled: float
    source: str


@dataclass(frozen=True)
class Forecast:
    """A break the hour preview expects: when, its name, about how long, what it holds."""

    at: float
    label: str
    seconds: float
    detail: str
    own_point: bool = True
    spots: tuple[tuple[str, Spot], ...] = ()


AIR_OFF = "Ad breaks are off for this station: set [ads] enabled = true in config.toml."
AIR_SAVED_POINTS = (
    "Ad breaks air only with saved segue playback. In Settings, set Transition mode to "
    "Use saved segue points."
)
AIR_BROWSER = (
    "Ad breaks air only when the server mixes the audio, for server audio or a stream. "
    "This station plays in the browser."
)


def _utc(stamp: float) -> datetime:
    return datetime.fromtimestamp(stamp, UTC)


@dataclass
class _Tracked:
    record: PlayRecord
    heard: Heard


class AdDesk:
    """One station's ad breaks: when they run, booking their spots, and their outcomes.

    :meth:`tick` does the work, on a worker thread from the station's tick:
    it reads the store and appends to the log.  The other methods take only
    the desk's short lock and touch no disk, so they are safe on the event
    loop and on the render-ahead worker.
    """

    def __init__(
        self,
        config: Callable[[], AdsConfig],
        store: AdStore,
        log: AdLog,
        timezone: str,
        *,
        player: Any = None,
        breaks: BreakDesk | None = None,
        events: Callable[[], Sequence[Any]] = tuple,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Run the breaks in *store*, logging to *log*, through *player*'s song changes.

        Args:
            config: The station's ``[ads]`` settings, read on every use.
            store: The station's spots and breaks.
            log: The station's ad play log.
            timezone: The station time zone, for minutes past the hour and days.
            player: The station's player: it books spots on its open song change.
            breaks: The station's break desk, which reports what aired.
            events: The station's Schedule events, for the ones that run a break.
            clock: Wall-clock time, for tests.
        """
        self.config = config
        self.store = store
        self.log = log
        self.timezone = timezone
        self.zone = ZoneInfo(timezone)
        self.player = player
        self.breaks = breaks
        self.events = events
        self.clock = clock
        #: Lines a stop could not write, appended to the log at the next start.
        self.side_file = store.path.parent / UNWRITTEN_FILE
        self._lock = threading.Lock()
        # Held while spots are picked and booked, and while one is deleted.
        self._book_lock = threading.Lock()
        self._cursor_saved = 0.0
        self._cursor: float | None = None
        self._started = False
        self._pending: list[Run] = []
        self._booked: dict[str, _Tracked] = {}
        self._heard: deque[Heard] = deque()
        self._recent: deque[PlayRecord] = deque(maxlen=RECENT)
        self._unwritten: list[PlayRecord] = []
        self._missed_steps: list[tuple[dict, str]] = []
        self._handled_steps: OrderedDict[str, int] = OrderedDict()
        self._requests = 0
        # Whether a break is due within PLAN_AHEAD, as last worked out: the
        # render worker asks often, and must never wait for the disk.
        self._soon = False
        if breaks is not None:
            breaks.wants_slots = self.wants_slots
        if player is not None:
            player.ad_desk = self

    def noticed(self, state: AdState | None = None) -> None:
        """Take in the store's breaks after a change (no disk access)."""
        self._noticed(state or self.store.cached(), self.clock())

    def _noticed(self, state: AdState, now: float) -> None:
        soon = self.enabled and self._due_within(state, now, now + PLAN_AHEAD.total_seconds())
        with self._lock:
            started = soon and not self._soon
            self._soon = soon
        if started:
            self._ask_for_plan()

    def _ask_for_plan(self) -> None:
        """Wake the playout controller when an ad slot may need preparation."""
        ahead = getattr(self.player, "_playout", None)
        request_plan = getattr(ahead, "request_plan", None)
        if request_plan is not None:
            request_plan()

    def _due_within(self, state: AdState, now: float, until: float) -> bool:
        """Whether a break runs after *now* and by *until*: by its minutes or a Schedule event."""
        if any(
            item.enabled and marks_between(item, now, until, self.zone)
            for item in state.breaks.values()
        ):
            return True
        from autodj.scheduler import next_run

        try:
            events = list(self.events())
        except Exception:  # a schedule that cannot be read must not stop the tick
            logger.exception("Could not read the Schedule's ad break events")
            return False
        for event in events:
            if not getattr(event, "enabled", False) or getattr(event, "action", "") != "ad_break":
                continue
            try:
                when = next_run(event, _utc(now), self.zone)
            except (ValueError, OverflowError, OSError):
                continue
            if when is not None and when.timestamp() <= until:
                return True
        return False

    # -- state ----------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Whether ``[ads] enabled`` is on."""
        return self.config().enabled is True

    def wants_slots(self) -> bool:
        """Whether the station must plan song changes for ad breaks (cheap, any thread).

        Only while a break waits, is booked, or is due within
        :data:`PLAN_AHEAD`: the rest of the time songs are picked exactly
        as with ads off.
        """
        if not self.enabled:
            return False
        player = self.player
        if (
            player is not None
            and getattr(player._cfg.playback, "pick_mode", None) == "clock"
            and (
                getattr(player, "_clock_has_ad_steps", False)
                or getattr(player, "_clock_has_pending_ad_steps", False)
            )
        ):
            return True
        with self._lock:
            return bool(self._pending or self._booked or self._soon)

    @property
    def revision(self) -> tuple[int, int]:
        """Changes when what the hour preview forecasts may change."""
        with self._lock:
            return self.store.revision, self._requests

    def refusal(self) -> str | None:
        """Why no break can air on this station now, for good, or ``None``."""
        if not self.enabled:
            return AIR_OFF
        player = self.player
        if player is None:
            return AIR_BROWSER
        if player._cfg.playback.transition_mode != "saved_points":
            return AIR_SAVED_POINTS
        if getattr(player, "bus", None) is None:
            return AIR_BROWSER
        return None

    def pending(self) -> list[Run]:
        """Copies of the runs waiting for a song change, oldest first."""
        with self._lock:
            return [Run(**vars(run)) for run in self._pending]

    def recent(self) -> list[PlayRecord]:
        """The latest outcomes, newest first."""
        with self._lock:
            return list(reversed(self._recent))

    def booked_spots(self) -> set[str]:
        """The spots booked on a song change and not yet aired."""
        with self._lock:
            return {tracked.record.spot_id for tracked in self._booked.values()}

    def plays_today(self, now: float | None = None) -> dict[str, int]:
        """How often each spot aired or is booked today, in the station's day."""
        moment = self.clock() if now is None else now
        today = datetime.fromtimestamp(moment, self.zone).date()
        counts: dict[str, int] = {}
        with self._lock:
            for play in self._heard:
                if datetime.fromtimestamp(play.at, self.zone).date() == today:
                    counts[play.spot_id] = counts.get(play.spot_id, 0) + 1
        return counts

    def request(self, break_id: str, source: str, at: float | None = None) -> Run:
        """Run the break *break_id* at the next song change it can join.

        Raises:
            AdsError: Ads are off, or there is no such break.
        """
        if not self.enabled:
            raise AdsError("", AIR_OFF, 409)
        state = self.store.cached()
        item = state.breaks.get(break_id)
        if item is None:
            raise AdsError("", "That ad break no longer exists", 404)
        run = Run(new_id(), break_id, item.name, self.clock() if at is None else at, source)
        with self._lock:
            self._pending.append(run)
            self._requests += 1
        self._ask_for_plan()
        logger.info("Ad break %r (%s) is due", item.name, SOURCES.get(source, source))
        return run

    def request_named(self, name: str, source: str = "schedule") -> Run:
        """Run the break called *name* (a Schedule event names it).

        Raises:
            AdsError: Ads are off, or no break has that name.
        """
        found = self.store.cached().break_named(name)
        if found is None:
            raise AdsError("", f"No ad break is called {name}", 404)
        return self.request(found[0], source)

    # -- the tick ---------------------------------------------------------------

    def needs_tick(self) -> bool:
        """Whether :meth:`tick` has work (cheap; the event loop asks once a second)."""
        with self._lock:
            if self._pending or self._booked or self._unwritten or self._missed_steps:
                return True
        return self.enabled

    def tick(self) -> None:
        """Book due breaks, log missed ones and record what aired (a worker thread)."""
        now = self.clock()
        state = self.store.state()
        if not self._started:
            self._start(now, state)
        records: list[PlayRecord] = []
        enabled = self.enabled
        with self._lock:
            cursor = self._cursor if self._cursor is not None else now
            self._cursor = max(cursor, now)
        if enabled and now > cursor:
            records.extend(self._due(state, cursor, now, "the computer slept or AutoDJ stalled"))
        self._noticed(state, now)
        records.extend(self._book_pending(state, now))
        records.extend(self._book_steps(state, now))
        with self._lock:
            missed, self._missed_steps = self._missed_steps, []
        if enabled:
            records.extend(
                self._run_missed(self._step_run(step, now), reason, now) for step, reason in missed
            )
        records.extend(self._outcomes(now))
        self._write(records)
        if enabled and now - self._cursor_saved >= CURSOR_EVERY_SECONDS:
            self._save_cursor(now)

    def needs_flush(self) -> bool:
        """Whether a stop has anything to write (cheap, any thread)."""
        with self._lock:
            return self._started or bool(self._booked or self._unwritten)

    def flush(self) -> None:
        """The station is stopping: write every booked spot's outcome, booking nothing.

        A spot on the air now may have aired; one still waiting will not.
        A line that cannot be written is tried once more, then kept in a
        side file that the next start appends to the log.
        """
        if not self._started:
            return
        now = self.clock()
        records = self._outcomes(now)
        records.extend(self._stopped(now))
        self._write(records)
        with self._lock:
            left = list(self._unwritten)
        if left:
            self._write([])
            with self._lock:
                left, self._unwritten = list(self._unwritten), []
            if left:
                self._keep_aside(left)
        if self.enabled:
            self._save_cursor(now)

    def station_started(self) -> None:
        """The station started with ads off: no break is owed for the time they were off."""
        if self.enabled:
            return
        try:
            self.log.forget_cursor()
        except OSError:
            logger.warning("Could not clear the ad schedule's place", exc_info=True)

    def _stopped(self, now: float) -> list[PlayRecord]:
        """The booked spots a stop cuts off: the one on the air, and the ones waiting."""
        from autodj.breaks import ON_AIR

        with self._lock:
            tracked = list(self._booked.items())
            self._booked.clear()
        records: list[PlayRecord] = []
        for break_id, entry in tracked:
            item = self.breaks.get(break_id) if self.breaks is not None else None
            if item is not None and item.state == ON_AIR:
                aired = item.aired_at if item.aired_at is not None else now
                records.append(
                    self._final(
                        entry.record,
                        INTERRUPTED,
                        "it may have aired: AutoDJ stopped during the break",
                        aired,
                        now,
                    )
                )
            else:
                records.append(
                    self._final(entry.record, MISSED, "AutoDJ stopped before it aired", None, now)
                )
                with self._lock, contextlib.suppress(ValueError):
                    self._heard.remove(entry.heard)
        return records

    def _keep_aside(self, records: list[PlayRecord]) -> None:
        """Keep lines the log would not take in the side file, for the next start."""
        path = self.side_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as handle:
                lines = [record.model_dump_json(exclude_none=True) for record in records]
                handle.write(("\n".join(lines) + "\n").encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            logger.error(
                "Could not write %d ad play log line%s anywhere; they are lost",
                len(records),
                "" if len(records) == 1 else "s",
                exc_info=True,
            )
            return
        logger.error(
            "Could not write %d ad play log line%s to %s; kept them in %s for the next start",
            len(records),
            "" if len(records) == 1 else "s",
            self.log.folder,
            path,
        )

    def _take_aside(self) -> list[PlayRecord]:
        """The lines a stop kept aside, read back; damaged ones are counted and skipped."""
        path = self.side_file
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError:
            logger.warning("Could not read the kept ad play log lines in %s", path, exc_info=True)
            return []
        found: list[PlayRecord] = []
        damaged = 0
        for raw in data.splitlines():
            if not raw.strip():
                continue
            try:
                found.append(PlayRecord.model_validate_json(raw))
            except ValueError:
                damaged += 1
        if damaged:
            logger.warning("%d kept ad play log lines in %s could not be read", damaged, path)
        return found

    def _save_cursor(self, now: float) -> None:
        try:
            self.log.save_cursor(now)
        except OSError:
            logger.warning("Could not save the ad schedule's place", exc_info=True)
            return
        self._cursor_saved = now

    def _start(self, now: float, state: AdState) -> None:
        """Remember recent plays; log what a stop left: spots booked, and breaks owed.

        Lines a stop kept aside are appended to the log first.  A spot still
        booked in the log did not air.  When ads are on, each break whose
        time fell while AutoDJ was not running is logged as missed (or, when
        still within the station's miss time, waits for a song change).
        """
        aside = self._take_aside()
        start = _utc(now) - HEARD_SPAN
        try:
            found, _damaged = self.log.read(_utc(now) - timedelta(days=35), _utc(now))
        except OSError:
            logger.warning("Could not read the ad play log in %s", self.log.folder, exc_info=True)
            found = []
        records: list[PlayRecord] = []
        heard: list[Heard] = []
        for record in latest([*found, *aside]).values():
            if record.status == BOOKED:
                records.append(
                    self._final(record, MISSED, "AutoDJ stopped before it aired", None, now)
                )
                continue
            if record.status in (AIRED, INTERRUPTED) and record.spot_id:
                heard_at = record.aired_at or record.scheduled
                if heard_at < start:
                    continue
                heard.append(
                    Heard(
                        record.spot_id,
                        record.advertiser,
                        record.category,
                        heard_at.timestamp(),
                    )
                )
        heard.sort(key=lambda play: play.at)
        owed: list[PlayRecord] = []
        if self.enabled:
            try:
                saved = self.log.cursor()
            except OSError:
                logger.warning("Could not read the ad schedule's place", exc_info=True)
                saved = None
            if saved is not None and saved < now:
                since = max(saved, now - MAX_DOWNTIME.total_seconds())
                owed = self._due(state, since, now, "AutoDJ was not running")
        with self._lock:
            self._heard.extend(heard)
            self._unwritten.extend([*aside, *records, *owed])
            self._started = True
            if self._cursor is None:
                self._cursor = now
        if aside:
            try:
                self._write([])
                if not self._unwritten:
                    self.side_file.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove %s", self.side_file, exc_info=True)
        if records:
            logger.warning(
                "%d ad spot%s booked when AutoDJ stopped did not air; logged as missed",
                len(records),
                "" if len(records) == 1 else "s",
            )

    def _due(self, state: AdState, cursor: float, now: float, why: str) -> list[PlayRecord]:
        """Queue each break that fell due since *cursor*; log the ones past the miss time.

        A break whose time is more than **Missed after** ago is missed for
        *why*; the rest wait for a song change, as any due break does.
        """
        limit = state.settings.miss_after_minutes * 60
        missed: list[Run] = []
        runs: list[Run] = []
        for key, item in state.breaks.items():
            if not item.enabled:
                continue
            for moment in marks_between(item, cursor, now, self.zone):
                run = Run(new_id(), key, item.name, moment, "clock")
                (missed if now - moment > limit else runs).append(run)
        runs.sort(key=lambda run: run.scheduled)
        if runs:
            with self._lock:
                self._pending.extend(runs)
                self._requests += 1
            for run in runs:
                logger.info("Ad break %r is due", run.break_name)
            self._ask_for_plan()
        return self._missed_runs(missed, why, now)

    def _missed_runs(self, runs: list[Run], why: str, now: float) -> list[PlayRecord]:
        """A missed line for each run; past :data:`MAX_MISSED_PER_TICK`, one line for the rest.

        The summary line says how many breaks it stands for (``count``) and
        their time range, so nothing is left out of a report.
        """
        runs = sorted(runs, key=lambda run: run.scheduled)
        records = [self._run_missed(run, why, now) for run in runs[:MAX_MISSED_PER_TICK]]
        rest = runs[MAX_MISSED_PER_TICK:]
        if rest:
            names = sorted({run.break_name for run in rest})
            first, last = rest[0].scheduled, rest[-1].scheduled
            label = names[0] if len(names) == 1 else "Several ad breaks"
            summary = PlayRecord(
                id=new_id(),
                run=new_id(),
                break_name=label,
                source="clock",
                scheduled=_utc(first),
                last_scheduled=_utc(last),
                count=len(rest),
                status=MISSED,
                reason=(
                    f"{why}: {len(rest)} more ad breaks ({', '.join(names)}) "
                    f"due from {_utc(first):%Y-%m-%d %H:%M} to {_utc(last):%Y-%m-%d %H:%M} UTC"
                ),
                logged=_utc(now),
            )
            logger.warning("%s", summary.reason)
            records.append(summary)
        return records

    def _book_pending(self, state: AdState, now: float) -> list[PlayRecord]:
        with self._lock:
            waiting = list(self._pending)
        if not waiting:
            return []
        settings = state.settings
        miss_after = settings.miss_after_minutes * 60
        refusal = self.refusal()
        records: list[PlayRecord] = []
        done: set[str] = set()
        for run in waiting:
            expires = run.scheduled + miss_after
            if now > expires:
                why = refusal or "no song change could take it in time"
                records.append(self._run_missed(run, f"it could not air in time: {why}", now))
                done.add(run.run_id)
                continue
            if refusal is not None:
                continue
            outcome = self._try_book(run, now, expires)
            if outcome is None:
                continue  # no song change open yet: try again next tick
            records.extend(outcome)
            done.add(run.run_id)
        if done:
            with self._lock:
                self._pending = [run for run in self._pending if run.run_id not in done]
                self._requests += 1
        return records

    def _usable(self, _key: str, spot: Spot) -> bool:
        return (self.store.folder / spot.file).is_file()

    def remove_spot(self, spot_id: str) -> Spot | None:
        """Forget a spot that is not booked; the one forgotten, or ``None``.

        Checked and removed while no break is being booked, so a spot is
        never deleted just as it is booked (blocking: it writes the store).

        Raises:
            AdsError: 409 when the spot is booked for a coming song change.
            OSError: The store could not be written.
        """
        with self._book_lock:
            if spot_id in self.booked_spots():
                raise AdsError(
                    "", "That spot is booked for a coming song change; delete it after it airs", 409
                )
            return self.store.remove_spot(spot_id)

    def _try_book(self, run: Run, now: float, expires: float) -> list[PlayRecord] | None:
        """Book *run*'s spots on the open song change.  ``None`` to try again later."""
        player = self.player
        plan, _closes_in, airs_in = player.slot_status()
        if plan is None:
            return None
        if now + airs_in > expires:
            # The open change airs too late; a later one is later still.
            return None
        with self._book_lock:
            # What the store holds now: a spot deleted since the tick read
            # it is not picked (its file is about to go).
            state = self.store.cached()
            item = state.breaks.get(run.break_id)
            if item is None:
                return [self._run_missed(run, "its ad break was deleted", now)]
            if not item.enabled and run.source == "schedule":
                return [self._run_missed(run, "its ad break is turned off", now)]
            return self._book_item(
                run,
                item,
                plan.slot_id,
                now,
                airs_in,
                expires,
                state,
                transition_revision=self._slot_revision(plan),
            )

    def _slot_revision(self, plan: Any) -> str | None:
        """Return the current commit revision for *plan*, including new bookings."""
        revision = getattr(self.player, "_transition_revision", None)
        if callable(revision):
            return revision(plan)
        return getattr(plan, "transition_revision", "") or None

    def _book_item(
        self,
        run: Run,
        item: AdBreak,
        slot_id: str,
        now: float,
        airs_in: float,
        expires: float,
        state: AdState,
        *,
        advertiser: str = "",
        category: str = "",
        transition_revision: str | None = None,
    ) -> list[PlayRecord] | None:
        """Select and book one run; caller holds ``_book_lock``."""
        from autodj.breaks import AD, AD_SCHEDULE, Break, BreakCues, BreakError
        from autodj.breaks import new_id as new_break_id

        with self._lock:
            heard = list(self._heard)
        expected_airtime = now + airs_in
        choice = choose(
            state,
            item,
            expected_airtime,
            self.zone,
            heard,
            usable=self._usable,
            advertiser=advertiser,
            category=category,
        )
        if not choice.spots:
            logger.warning("Ad break %r has no spot to air: %s", item.name, choice.reason)
            return [self._run_missed(run, choice.reason, now)]
        limit = float(self.config().max_spot_seconds)
        breaks = [
            Break(
                break_id=new_break_id(),
                kind=AD,
                slot_id=slot_id,
                ending="",
                upcoming="",
                placement="standalone",
                allow_outro=False,
                path=self.store.folder / spot.file,
                size=0,
                cues=BreakCues(spot.cue_start, spot.cue_segue, spot.cue_end, spot.length_s),
                label=spot.label,
                token_name=AD_SENDER,
                created_at=now,
                expires_at=expires,
                source=AD_SCHEDULE,
                keep_file=True,
                limit_s=limit,
            )
            for _key, spot in choice.spots
        ]
        try:
            self.player.book_breaks(breaks, slot_id, transition_revision=transition_revision)
        except BreakError:
            return None  # the change closed as it was booked: the next one
        records = [
            PlayRecord(
                id=booked.break_id,
                run=run.run_id,
                break_id=run.break_id,
                break_name=item.name,
                source=run.source,
                scheduled=_utc(run.scheduled),
                spot_id=key,
                title=spot.title,
                advertiser=spot.advertiser,
                campaign=spot.campaign,
                category=spot.category,
                seconds=round(spot.seconds, 3),
                status=BOOKED,
                logged=_utc(now),
            )
            for booked, (key, spot) in zip(breaks, choice.spots, strict=True)
        ]
        with self._lock:
            for record in records:
                entry = Heard(record.spot_id, record.advertiser, record.category, expected_airtime)
                self._booked[record.id] = _Tracked(record, entry)
                self._heard.append(entry)
            self._forget_old_locked(now)
        logger.info(
            "Ad break %r booked with %d spot%s for the song change in about %d s",
            item.name,
            len(records),
            "" if len(records) == 1 else "s",
            round(airs_in),
        )
        return records

    @staticmethod
    def _step_run(step: dict, at: float) -> Run:
        name = f"Clockwheel {step['clock']}, step {step['number']}"
        return Run(new_id(), "", name, at, "clockwheel")

    def missed_steps(self, steps: Sequence[dict], reason: str) -> None:
        """Queue misses from the render worker; the tick writes their report lines."""
        if not self.enabled:
            return
        with self._lock:
            self._missed_steps.extend((step, reason) for step in steps)

    def handled_count(self, slot_id: str) -> int:
        with self._lock:
            return self._handled_steps.get(slot_id, 0)

    def _book_steps(self, state: AdState, now: float) -> list[PlayRecord]:
        if not self.enabled or self.player is None:
            return []
        plan, _closes_in, airs_in = self.player.slot_status()
        if (
            plan is None
            or not plan.ad_steps
            or self.handled_count(plan.slot_id) >= len(plan.ad_steps)
        ):
            return []
        refusal = self.refusal()
        records: list[PlayRecord] = []
        with self._book_lock:
            state = self.store.cached()
            slot_id = plan.slot_id
            steps = plan.ad_steps
            completed = self.handled_count(slot_id)
            for number, step in enumerate(steps[completed:], completed + 1):
                if number > completed + 1:
                    # A prior step changed the committed-break portion of the
                    # transition revision. Refresh the same open slot before
                    # admitting the next batch; never reuse the old revision.
                    plan, _closes_in, airs_in = self.player.slot_status()
                    if plan is None or plan.slot_id != slot_id:
                        return records
                run = self._step_run(step, now + airs_in)
                if refusal is not None:
                    records.append(self._run_missed(run, refusal, now))
                else:
                    item = AdBreak(
                        name=run.break_name,
                        max_spots=step["step"]["max_spots"],
                        max_seconds=step["step"]["max_seconds"],
                    )
                    result = self._book_item(
                        run,
                        item,
                        slot_id,
                        now,
                        airs_in,
                        now + airs_in + state.settings.miss_after_minutes * 60,
                        state,
                        advertiser=step["step"].get("advertiser", ""),
                        category=step["step"].get("category", ""),
                        transition_revision=self._slot_revision(plan),
                    )
                    if result is None:
                        return records
                    records.extend(result)
                with self._lock:
                    self._handled_steps[slot_id] = number
                    if len(self._handled_steps) > 64:
                        self._handled_steps.popitem(last=False)
        return records

    def heard_snapshot(self) -> list[Heard]:
        with self._lock:
            return list(self._heard)

    def forecast_step(self, step: dict, at: float, heard: list[Heard]) -> Forecast | None:
        if self.refusal() is not None:
            return None
        fields = step["step"]
        item = AdBreak(
            name=f"Clockwheel {step['clock']}, step {step['number']}",
            max_spots=fields["max_spots"],
            max_seconds=fields["max_seconds"],
        )
        choice = choose(
            self.store.cached(),
            item,
            at,
            self.zone,
            heard,
            advertiser=fields.get("advertiser", ""),
            category=fields.get("category", ""),
            usable=self._usable,
        )
        for spot_id, spot in choice.spots:
            heard.append(Heard(spot_id, spot.advertiser, spot.category, at))
        detail = (
            f"{len(choice.spots)} spot{'' if len(choice.spots) == 1 else 's'}, "
            f"{length_text(choice.seconds)}"
            if choice.spots
            else choice.reason
        )
        label = f"Ad break: clockwheel step {step['number']}"
        if not choice.spots:
            label += f" ({choice.reason})"
        return Forecast(
            at,
            label,
            choice.seconds,
            detail,
            False,
            tuple((key, spot.model_copy(deep=True)) for key, spot in choice.spots),
        )

    def _outcomes(self, now: float) -> list[PlayRecord]:
        """What became of the booked spots: aired, cut off on the air, or missed and why."""
        from autodj.breaks import AIRED as BREAK_AIRED
        from autodj.breaks import TERMINAL

        with self._lock:
            tracked = list(self._booked.items())
        if not tracked:
            return []
        records: list[PlayRecord] = []
        finished: list[str] = []
        not_aired: set[str] = set()
        aired_at_by_id: dict[str, float] = {}
        for break_id, entry in tracked:
            item = self.breaks.get(break_id) if self.breaks is not None else None
            if item is None:
                records.append(
                    self._final(entry.record, MISSED, "AutoDJ lost track of it", None, now)
                )
                not_aired.add(break_id)
            elif item.state not in TERMINAL:
                continue
            elif item.state == BREAK_AIRED:
                aired_at = item.aired_at if item.aired_at is not None else now
                records.append(self._final(entry.record, AIRED, "", aired_at, now))
                # Store the measured airtime; the provisional entry was for selection.
                aired_at_by_id[break_id] = aired_at
            elif item.aired_at is not None:
                # It started, then a skip or a stop cut it off.
                reason = f"it may have aired in part: {item.reason or item.state}"
                records.append(self._final(entry.record, INTERRUPTED, reason, item.aired_at, now))
                aired_at_by_id[break_id] = item.aired_at
            else:
                records.append(
                    self._final(entry.record, MISSED, item.reason or item.state, None, now)
                )
                not_aired.add(break_id)
            finished.append(break_id)
        with self._lock:
            for break_id in finished:
                gone = self._booked.pop(break_id, None)
                if gone is not None and break_id in not_aired:
                    # It did not air: it no longer counts toward limits.
                    with contextlib.suppress(ValueError):
                        self._heard.remove(gone.heard)
                elif gone is not None and break_id in aired_at_by_id:
                    with contextlib.suppress(ValueError):
                        self._heard.remove(gone.heard)
                    self._heard.append(
                        Heard(
                            gone.heard.spot_id,
                            gone.heard.advertiser,
                            gone.heard.category,
                            aired_at_by_id[break_id],
                        )
                    )
            if aired_at_by_id:
                self._heard = deque(sorted(self._heard, key=lambda play: play.at))
        return records

    def _forget_old_locked(self, now: float) -> None:
        cutoff = now - HEARD_SPAN.total_seconds()
        while self._heard and self._heard[0].at < cutoff:
            self._heard.popleft()

    def _final(
        self, record: PlayRecord, status: Status, reason: str, aired: float | None, now: float
    ) -> PlayRecord:
        final = record.model_copy(
            update={
                "status": status,
                "reason": reason,
                "aired_at": _utc(aired) if aired is not None else None,
                "logged": _utc(now),
            }
        )
        if status != AIRED:
            logger.warning(
                "Ad spot %s (break %r) %s: %s",
                record.title,
                record.break_name,
                "was missed" if status == MISSED else "was cut off",
                reason,
            )
        return final

    def _run_missed(self, run: Run, reason: str, now: float) -> PlayRecord:
        logger.warning("Ad break %r was missed: %s", run.break_name, reason)
        return PlayRecord(
            id=run.run_id,
            run=run.run_id,
            break_id=run.break_id,
            break_name=run.break_name,
            source=run.source,
            scheduled=_utc(run.scheduled),
            status=MISSED,
            reason=reason,
            logged=_utc(now),
        )

    def _write(self, records: list[PlayRecord]) -> None:
        """Append *records*, after any a failed write kept; keep them on failure."""
        with self._lock:
            waiting = [*self._unwritten, *records]
            self._unwritten = []
        if not waiting:
            return
        try:
            self.log.append(waiting)
        except OSError:
            logger.warning(
                "Could not write %d line%s to the ad play log in %s; trying again",
                len(waiting),
                "" if len(waiting) == 1 else "s",
                self.log.folder,
                exc_info=True,
            )
            with self._lock:
                self._unwritten = waiting + self._unwritten
            return
        with self._lock:
            for record in waiting:
                if record.status != BOOKED:
                    self._recent.append(record)

    # -- the hour preview -----------------------------------------------------------

    def forecast(self, now: float, end: float, events: Sequence[Any] = ()) -> list[Forecast]:
        """The ad breaks expected from *now* to *end*, with their spots guessed.

        Runs waiting for a song change come first, at their own times.
        Breaks that a Schedule event runs are forecast at the event's
        times, with ``own_point`` false: the preview already shows the event.
        Pure: nothing is booked or logged.
        """
        if self.refusal() is not None:
            return []
        state = self.store.cached()
        runs: list[tuple[float, str, AdBreak, bool]] = []
        for run in self.pending():
            item = state.breaks.get(run.break_id)
            if item is not None:
                runs.append((run.scheduled, run.break_id, item, False))
        for key, item in state.breaks.items():
            if item.enabled:
                runs.extend(
                    (moment, key, item, True) for moment in marks_between(item, now, end, self.zone)
                )
        from autodj.scheduler import next_run

        after = _utc(now)
        for event in events:
            if not getattr(event, "enabled", False) or getattr(event, "action", "") != "ad_break":
                continue
            found = state.break_named(str(event.params.get("name", "")))
            if found is None:
                continue
            cursor = after
            for _ in range(24):
                try:
                    when = next_run(event, cursor, self.zone)
                except (ValueError, OverflowError, OSError):
                    break
                if when is None or when.timestamp() > end:
                    break
                runs.append((when.timestamp(), found[0], found[1], False))
                cursor = when
        runs.sort(key=lambda run: run[0])
        with self._lock:
            heard = list(self._heard)
        found_runs: list[Forecast] = []
        for moment, _key, item, own in runs[:48]:
            choice = choose(state, item, moment, self.zone, heard, usable=lambda _k, _s: True)
            for spot_id, spot in choice.spots:
                heard.append(Heard(spot_id, spot.advertiser, spot.category, moment))
            count = len(choice.spots)
            detail = (
                f"{count} spot{'' if count == 1 else 's'}, {length_text(choice.seconds)}"
                if count
                else choice.reason
            )
            found_runs.append(
                Forecast(
                    moment,
                    f"Ad break: {item.name}",
                    choice.seconds,
                    detail,
                    own,
                    tuple((key, spot.model_copy(deep=True)) for key, spot in choice.spots),
                )
            )
        return found_runs


# ----------------------------------------------------------------------
# Reports
# ----------------------------------------------------------------------


def report_status(record: PlayRecord, late_minutes: int) -> str:
    """``aired``, ``late``, ``missed`` or ``booked`` for a report row."""
    delay = record.delay_s()
    if delay is not None and delay > late_minutes * 60:
        return "late"
    return record.status


def _local_text(moment: datetime | None, zone: ZoneInfo) -> str:
    return "" if moment is None else moment.astimezone(zone).isoformat(timespec="seconds")


def _clock_words(moment: datetime | None, zone: ZoneInfo) -> str:
    if moment is None:
        return ""
    local = moment.astimezone(zone)
    return f"{day_words(local.date())} at {clock_text(local, seconds=True)}"


def report_rows(records: Iterable[PlayRecord], start: datetime, end: datetime) -> list[PlayRecord]:
    """Each spot's last line whose break was due from *start* until *end*, in time order."""
    rows = [record for record in latest(records).values() if start <= record.scheduled < end]
    rows.sort(
        key=lambda record: (record.scheduled, record.aired_at or record.scheduled, record.title)
    )
    return rows


def summarise(rows: Sequence[PlayRecord], late_minutes: int) -> list[dict[str, Any]]:
    """Aired, late and missed counts and seconds aired, per advertiser and campaign."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for record in rows:
        if not record.spot_id:
            continue
        key = (record.advertiser, record.campaign)
        group = groups.setdefault(
            key,
            {
                "advertiser": record.advertiser,
                "campaign": record.campaign,
                "aired": 0,
                "late": 0,
                "missed": 0,
                "interrupted": 0,
                "waiting": 0,
                "seconds": 0.0,
            },
        )
        status = report_status(record, late_minutes)
        if status == INTERRUPTED:
            group["interrupted"] += 1
        elif status in (AIRED, "late"):
            group["aired"] += 1
            group["seconds"] += record.seconds
            if status == "late":
                group["late"] += 1
        elif status == MISSED:
            group["missed"] += 1
        else:
            group["waiting"] += 1
    found = sorted(
        groups.values(),
        key=lambda group: (group["advertiser"].casefold(), group["campaign"].casefold()),
    )
    for group in found:
        group["seconds"] = round(group["seconds"], 1)
    return found


def row_view(record: PlayRecord, zone: ZoneInfo, late_minutes: int) -> dict[str, Any]:
    """One report row as the page shows it."""
    status = report_status(record, late_minutes)
    delay = record.delay_s()
    return {
        "id": record.id,
        "scheduled": _local_text(record.scheduled, zone),
        "scheduled_text": _clock_words(record.scheduled, zone),
        "aired_at": _local_text(record.aired_at, zone) or None,
        "aired_text": _clock_words(record.aired_at, zone),
        "delay_minutes": None if delay is None else round(delay / 60, 1),
        "status": status,
        "status_text": STATUS_WORDS[status],
        "break_name": record.break_name,
        "source_text": SOURCES.get(record.source, record.source),
        "spot_id": record.spot_id or None,
        "title": record.title,
        "advertiser": record.advertiser,
        "campaign": record.campaign,
        "seconds": round(record.seconds, 1),
        "reason": record.reason,
        "count": record.count,
    }


def build_report(
    log: AdLog, start: datetime, end: datetime, zone: ZoneInfo, late_minutes: int
) -> dict[str, Any]:
    """The report from *start* until *end* as the page reads it (blocking: it reads the log).

    Raises:
        ValueError: The span is empty or too long.
        OSError: The log could not be read.
    """
    if end <= start:
        raise ValueError("The end of the range must be after its start.")
    if end - start > timedelta(days=MAX_REPORT_DAYS):
        raise ValueError(f"A report covers at most {MAX_REPORT_DAYS} days.")
    records, damaged = log.read(start, end)
    rows = report_rows(records, start, end)
    statuses = [report_status(record, late_minutes) for record in rows]
    spots = [status for record, status in zip(rows, statuses, strict=True) if record.spot_id]
    totals = {
        "aired": sum(1 for status in spots if status in (AIRED, "late")),
        "late": spots.count("late"),
        "missed": sum(record.count for record in rows if record.status == MISSED),
        "interrupted": statuses.count(INTERRUPTED),
        "waiting": statuses.count(BOOKED),
        "missed_breaks": sum(
            record.count for record in rows if not record.spot_id and record.status == MISSED
        ),
    }
    return {
        "rows": [row_view(record, zone, late_minutes) for record in rows[:REPORT_ROWS]],
        "truncated": len(rows) > REPORT_ROWS,
        "count": len(rows),
        "summary": summarise(rows, late_minutes),
        "totals": totals,
        "damaged": damaged,
        "late_minutes": late_minutes,
    }


def _cell(value: object) -> str:
    from autodj.playlog import _FORMULA_PREFIXES

    text = str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def _csv(header: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows([_cell(value) for value in row] for row in rows)
    return out.getvalue()


AFFIDAVIT_COLUMNS = (
    "scheduled",
    "aired",
    "status",
    "delay_minutes",
    "advertiser",
    "campaign",
    "title",
    "seconds",
    "break",
    "source",
    "reason",
    "breaks",
)
SUMMARY_COLUMNS = (
    "advertiser",
    "campaign",
    "aired",
    "aired_late",
    "missed",
    "may_have_aired",
    "waiting",
    "seconds_aired",
)


def affidavit_csv(rows: Sequence[PlayRecord], zone: ZoneInfo, late_minutes: int) -> str:
    """Every spot (and every break that held none) with what became of it, as CSV."""
    lines = []
    for record in rows:
        view = row_view(record, zone, late_minutes)
        lines.append(
            (
                view["scheduled"],
                view["aired_at"] or "",
                view["status_text"],
                "" if view["delay_minutes"] is None else view["delay_minutes"],
                record.advertiser,
                record.campaign,
                record.title,
                view["seconds"] if record.spot_id else "",
                record.break_name,
                view["source_text"],
                record.reason,
                record.count,
            )
        )
    return _csv(AFFIDAVIT_COLUMNS, lines)


def summary_csv(rows: Sequence[PlayRecord], late_minutes: int) -> str:
    """Per advertiser and campaign: aired, aired late, missed, waiting and seconds aired, as CSV."""
    return _csv(
        SUMMARY_COLUMNS,
        (
            (
                group["advertiser"],
                group["campaign"],
                group["aired"],
                group["late"],
                group["missed"],
                group["interrupted"],
                group["waiting"],
                group["seconds"],
            )
            for group in summarise(rows, late_minutes)
        ),
    )


def aired_plays(folder: Path, start: datetime, end: datetime) -> list[Any]:
    """The spots that aired from *start* until *end*, as play log rows (``kind`` ``ad``).

    For the play log export: the advertiser stands as the artist and the
    campaign as the album.  Blocking: it reads the log; a log that cannot
    be read adds nothing and is logged.
    """
    from autodj.playlog import Play

    try:
        records, _damaged = AdLog(folder).read(start, end)
    except OSError:
        logger.warning("Could not read the ad play log in %s", folder, exc_info=True)
        return []
    plays = []
    for record in latest(records).values():
        aired = record.aired_at
        if record.status != AIRED or aired is None or not start <= aired < end:
            continue
        plays.append(
            Play(
                start=aired,
                end=aired + timedelta(seconds=round(record.seconds)),
                artist=record.advertiser,
                title=record.title,
                album=record.campaign,
                isrc="",
                label="",
                kind="ad",
                path="",
                performances=None,
            )
        )
    return sorted(plays, key=lambda play: play.start)


__all__ = [
    "ADS_FILE",
    "MAX_BREAKS",
    "AdBreak",
    "AdDesk",
    "AdLog",
    "AdSettings",
    "AdState",
    "AdStore",
    "AdsError",
    "BreakFields",
    "Choice",
    "Forecast",
    "Heard",
    "PlayRecord",
    "Spot",
    "SpotFields",
    "affidavit_csv",
    "aired_plays",
    "build_report",
    "choose",
    "marks_between",
    "report_rows",
    "spot_file_name",
    "summary_csv",
]
