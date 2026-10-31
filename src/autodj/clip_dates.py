"""Play dates for clips: "not before" and "do not play after", in the station time zone.

A voice liner or a cart can carry a first and a last date to play, both
included, such as a promotion that must stop after the event.  Outside its
dates a clip is skipped and said to be expired (or not playable yet).  The
dates are station-local calendar days, read in ``[station] timezone``.

Carts keep their dates in their slot in ``carts.json``.  Voice liners keep
theirs in ``liner_dates.json`` in the station's state folder, keyed by the
ID ``/api/liners`` lists (the catalog ID of a registered liner, or the file
name of one in the liner folder).  That file is read again whenever it
changes on disk, so every part of the station that checks a liner sees the
same dates without sharing one object.
"""

from __future__ import annotations

import calendar
import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from datetime import date as Date
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from autodj.errors import FieldError, first_issue
from autodj.fsutil import atomic_write, file_signature, read_bytes_shared, set_aside

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig

logger = logging.getLogger(__name__)

SCHEMA_VERSION: Literal[1] = 1
#: The most liners that can have dates.
MAX_LINER_DATES = 2000

FIELD_LABELS = {"not_before": "Not before", "not_after": "Do not play after"}


class ClipDatesError(FieldError):
    """Dates that cannot be saved, naming the field at fault."""

    field: str

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(message, field_name)


def day_words(day: Date) -> str:
    """Say a date in words: "26 December 2026"."""
    return f"{day.day} {calendar.month_name[day.month]} {day.year}"


class ClipDates(BaseModel):
    """A clip's first and last day to play, both optional and both included."""

    model_config = ConfigDict(extra="forbid")

    not_before: Date | None = None
    not_after: Date | None = None

    @model_validator(mode="after")
    def _order(self) -> Self:
        if self.not_before and self.not_after and self.not_after < self.not_before:
            raise ClipDatesError(
                "not_after", "Do not play after must be on or after the Not before date"
            )
        return self

    @property
    def empty(self) -> bool:
        """Whether the clip has no dates, so it always plays."""
        return self.not_before is None and self.not_after is None

    def refusal(self, today: Date) -> str | None:
        """Why the clip may not play on *today*, in words, or ``None`` when it may."""
        if self.not_after is not None and today > self.not_after:
            return f"expired after {day_words(self.not_after)}"
        if self.not_before is not None and today < self.not_before:
            return f"not playable before {day_words(self.not_before)}"
        return None

    def describe(self) -> str:
        """Say the dates in words; empty when there are none."""
        if self.not_before and self.not_after:
            return f"Plays from {day_words(self.not_before)} until {day_words(self.not_after)}"
        if self.not_after:
            return f"Plays until {day_words(self.not_after)}"
        if self.not_before:
            return f"Plays from {day_words(self.not_before)}"
        return ""

    def view(self, today: Date) -> dict[str, object]:
        """The dates, in words, and whether the clip may play today, for the web page."""
        refusal = self.refusal(today)
        return {
            "not_before": self.not_before.isoformat() if self.not_before else None,
            "not_after": self.not_after.isoformat() if self.not_after else None,
            "dates_text": self.describe(),
            "playable": refusal is None,
            "unplayable_text": refusal or "",
        }


def parse_dates(body: object) -> ClipDates:
    """Validate a request body.

    Raises:
        ClipDatesError: The body is not valid; names the field.
    """
    try:
        return ClipDates.model_validate(body)
    except ValidationError as exc:
        raise ClipDatesError(*first_issue(exc, FIELD_LABELS, fallback_label="Dates")) from None


def station_zone(cfg: AutoDJConfig) -> str:
    """The station time zone's name: ``[station] timezone`` or the system's."""
    from autodj.scheduler import system_timezone

    zone = getattr(getattr(cfg, "station", None), "timezone", None)
    return str(zone or system_timezone())


def station_today(cfg: AutoDJConfig) -> Date:
    """Today in the station time zone: ``[station] timezone`` or the system's."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(station_zone(cfg))).date()


class _LinerDatesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    liners: dict[str, ClipDates] = Field(default_factory=dict, max_length=MAX_LINER_DATES)


class LinerDates:
    """``liner_dates.json``: each voice liner's play dates, by its ID.

    The file is read again when its time or size changes, so separate
    instances on one file agree.  A file that cannot be read is set aside
    as ``liner_dates.json.bad-<id>`` and every liner plays, as before dates
    existed.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp: tuple[int, int, int] | None = None
        self._dates: dict[str, ClipDates] = {}
        # Whether this store has ever seen its file.  Until it has, a folder
        # that is not there yet is made on the first save; after, a missing
        # folder means the disk or share is away, and saves are refused.
        self._seen = False

    def _stat(self) -> tuple[int, int, int] | None:
        """The file's time, size and ID; ``None`` when there is no file.

        Raises:
            OSError: The file is there but cannot be looked at.
        """
        return file_signature(self.path)

    def _keep_bad_copy(self, reason: str) -> None:
        set_aside(self.path, "liner dates file", reason)

    def _load_locked(self) -> dict[str, ClipDates]:
        """Every liner's dates, read again if the file changed. Caller holds ``_lock``.

        Only a file that is not valid dates is set aside.  One that cannot
        be read just now (a busy disk, a share away) is left as it is.

        Raises:
            OSError: The file could not be looked at or read.
        """
        stamp = self._stat()
        if stamp is not None:
            self._seen = True
        if stamp == self._stamp:
            return dict(self._dates)
        dates: dict[str, ClipDates] = {}
        if stamp is not None:
            data = read_bytes_shared(self.path)
            try:
                dates = _LinerDatesFile.model_validate_json(data).liners
            except ValueError as exc:
                self._keep_bad_copy(str(exc).splitlines()[0])
                stamp = None
        self._stamp, self._dates = stamp, dates
        return dict(dates)

    def all(self) -> dict[str, ClipDates]:
        """Every liner's dates, read again if the file changed.

        When the file cannot be read just now, the dates last read are used
        and a warning logged; a save then refuses (see :meth:`set`), so
        nothing is written over the file.
        """
        with self._lock:
            try:
                return self._load_locked()
            except OSError as exc:
                logger.warning("Could not read the liner dates file %s: %s", self.path, exc)
                return dict(self._dates)

    def get(self, liner_id: str) -> ClipDates:
        """The dates of *liner_id*; empty when it has none."""
        return self.all().get(liner_id) or ClipDates()

    def set(self, liner_id: str, dates: ClipDates) -> None:
        """Save *dates* for *liner_id*; empty dates remove the entry.

        Raises:
            ClipDatesError: Too many liners have dates already.
            OSError: The file could not be read, so nothing is written.
        """
        # Read, change and write under one lock, so two saves at once (two
        # requests, or a save and a delete) never lose each other's change.
        with self._lock:
            self._first_folder_locked()
            current = self._load_locked()
            if dates.empty:
                if liner_id not in current:
                    return  # Nothing to forget; the file is left as it is.
                del current[liner_id]
            else:
                if liner_id not in current and len(current) >= MAX_LINER_DATES:
                    raise ClipDatesError("", f"At most {MAX_LINER_DATES} liners can have dates")
                current[liner_id] = dates
            document = _LinerDatesFile(schema_version=SCHEMA_VERSION, liners=current)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.path, document.model_dump_json(indent=2) + "\n")
            self._written_locked(current)

    def _first_folder_locked(self) -> None:
        """Make the folder for a store that has never had a file.  Caller holds ``_lock``."""
        if not self._seen and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def _written_locked(self, dates: dict[str, ClipDates]) -> None:
        """Remember *dates*, just written.  Caller holds ``_lock``.

        The write is done, so a file that cannot be looked at just after it
        is not a failed save: the dates are read again next time.
        """
        self._seen = True
        try:
            stamp = self._stat()
        except OSError:
            stamp = None
        self._stamp, self._dates = stamp, dates

    def refusal(self, liner_id: str, today: Date) -> str | None:
        """Why *liner_id* may not play on *today*, or ``None`` when it may."""
        return self.get(liner_id).refusal(today)

    def forget(self, liner_ids: Iterable[str]) -> list[str]:
        """Remove the dates of *liner_ids* in one write; the IDs that had dates.

        The file is left as it is when none of them had dates.

        Raises:
            OSError: The file could not be read, so nothing is written.
        """
        with self._lock:
            self._first_folder_locked()
            current = self._load_locked()
            gone = sorted({liner_id for liner_id in liner_ids if liner_id in current})
            if not gone:
                return []
            for liner_id in gone:
                del current[liner_id]
            document = _LinerDatesFile(schema_version=SCHEMA_VERSION, liners=current)
            atomic_write(self.path, document.model_dump_json(indent=2) + "\n")
            self._written_locked(current)
        return gone


class LinerGate:
    """One station's liner dates and its calendar day, for the code that picks liners.

    Args:
        store: The station's ``liner_dates.json``.
        today: The station's calendar day now.
        liner_id: The ID a liner path is known by (its catalog ID, or its
            file name), for the clockwheel's liner steps.
        day_at: The station's calendar day at a Unix time, for a liner that
            airs later (a talk-up planned ahead, a clockwheel liner before
            the next song); without it, today is used.
    """

    def __init__(
        self,
        store: LinerDates,
        today: Callable[[], Date],
        liner_id: Callable[[str], str] = lambda path: Path(path).name,
        day_at: Callable[[float], Date] | None = None,
    ) -> None:
        self.store = store
        self._today = today
        self._liner_id = liner_id
        self._day_at = day_at

    def _day(self, at: float | None) -> Date:
        return self._day_at(at) if at is not None and self._day_at is not None else self._today()

    def refusal(self, liner_id: str, at: float | None = None) -> str | None:
        """Why *liner_id* may not play now (or at Unix time *at*), or ``None`` when it may."""
        dates = self.store.all().get(liner_id)
        return dates.refusal(self._day(at)) if dates is not None else None

    def playable(self, entries: Sequence[Any], at: float | None = None) -> Sequence[Any]:
        """The liner *entries* that may play now, or at Unix time *at*.

        *entries* itself when no liner has dates.
        """
        dates = self.store.all()
        if not dates:
            return entries
        today = self._day(at)
        return tuple(
            entry
            for entry in entries
            if (found := dates.get(self._liner_id(entry.path))) is None
            or found.refusal(today) is None
        )
