"""Play dates for clips: "not before" and "do not play after", in the station time zone.

A voice liner or a cart can carry a first and a last date to play, both
included, such as a promotion that must stop after the event.  Outside its
dates a clip is skipped and said to be expired (or not playable yet).  The
dates are station-local calendar days, read in ``[server] station_timezone``.

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
import uuid
from collections.abc import Callable, Sequence
from datetime import date as Date
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from autodj.fsutil import atomic_write

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig

logger = logging.getLogger(__name__)

SCHEMA_VERSION: Literal[1] = 1
LINER_DATES_FILE = "liner_dates.json"
#: The most liners that can have dates.
MAX_LINER_DATES = 2000

FIELD_LABELS = {"not_before": "Not before", "not_after": "Do not play after"}


class ClipDatesError(ValueError):
    """Dates that cannot be saved, naming the field at fault."""

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(message)
        self.field = field_name
        self.message = message


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
        error = exc.errors()[0]
        cause = (error.get("ctx") or {}).get("error")
        if isinstance(cause, ClipDatesError):
            raise ClipDatesError(cause.field, cause.message) from None
        location = [str(part) for part in error.get("loc", ())]
        name = location[0] if location else ""
        if error.get("type") == "extra_forbidden":
            raise ClipDatesError(name, f"Unknown field {name}") from None
        label = FIELD_LABELS.get(name, name or "Dates")
        raise ClipDatesError(name, f"{label}: {error['msg']}") from None


def station_zone(cfg: AutoDJConfig) -> str:
    """The station time zone's name: ``[server] station_timezone`` or the system's."""
    from autodj.scheduler import system_timezone

    zone = getattr(getattr(cfg, "server", None), "station_timezone", None)
    return str(zone or system_timezone())


def station_today(cfg: AutoDJConfig) -> Date:
    """Today in the station time zone: ``[server] station_timezone`` or the system's."""
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

    def _stat(self) -> tuple[int, int, int] | None:
        # The file ID too: every save replaces the file, so a new file of the
        # same size written within one tick of the clock is still noticed.
        try:
            found = self.path.stat()
        except OSError:
            return None
        return found.st_mtime_ns, found.st_size, found.st_ino

    def _keep_bad_copy(self, reason: str) -> None:
        bad = self.path.with_name(f"{self.path.name}.bad-{uuid.uuid4().hex[:8]}")
        try:
            self.path.replace(bad)
        except OSError:
            logger.exception("Could not set aside the invalid liner dates file %s", self.path)
            return
        logger.error(
            "Liner dates file %s is invalid (%s); set it aside as %s", self.path, reason, bad
        )

    def _load_locked(self) -> dict[str, ClipDates]:
        """Every liner's dates, read again if the file changed. Caller holds ``_lock``."""
        stamp = self._stat()
        if stamp == self._stamp:
            return dict(self._dates)
        dates: dict[str, ClipDates] = {}
        if stamp is not None:
            try:
                dates = _LinerDatesFile.model_validate_json(self.path.read_bytes()).liners
            except (OSError, ValueError) as exc:
                self._keep_bad_copy(str(exc).splitlines()[0])
                stamp = None
        self._stamp, self._dates = stamp, dates
        return dict(dates)

    def all(self) -> dict[str, ClipDates]:
        """Every liner's dates, read again if the file changed."""
        with self._lock:
            return self._load_locked()

    def get(self, liner_id: str) -> ClipDates:
        """The dates of *liner_id*; empty when it has none."""
        return self.all().get(liner_id) or ClipDates()

    def set(self, liner_id: str, dates: ClipDates) -> None:
        """Save *dates* for *liner_id*; empty dates remove the entry.

        Raises:
            ClipDatesError: Too many liners have dates already.
        """
        # Read, change and write under one lock, so two saves at once (two
        # requests, or a save and a delete) never lose each other's change.
        with self._lock:
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
            self._stamp, self._dates = self._stat(), current

    def refusal(self, liner_id: str, today: Date) -> str | None:
        """Why *liner_id* may not play on *today*, or ``None`` when it may."""
        return self.get(liner_id).refusal(today)


class LinerGate:
    """One station's liner dates and its calendar day, for the code that picks liners.

    Args:
        store: The station's ``liner_dates.json``.
        today: The station's calendar day now.
        liner_id: The ID a liner path is known by (its catalog ID, or its
            file name), for the clockwheel's liner steps.
    """

    def __init__(
        self,
        store: LinerDates,
        today: Callable[[], Date],
        liner_id: Callable[[str], str] = lambda path: Path(path).name,
    ) -> None:
        self.store = store
        self._today = today
        self._liner_id = liner_id

    def refusal(self, liner_id: str) -> str | None:
        """Why *liner_id* may not play now, or ``None`` when it may."""
        dates = self.store.all().get(liner_id)
        return dates.refusal(self._today()) if dates is not None else None

    def playable(self, entries: Sequence[Any]) -> Sequence[Any]:
        """The liner *entries* that may play now; *entries* itself when no liner has dates."""
        dates = self.store.all()
        if not dates:
            return entries
        today = self._today()
        return tuple(
            entry
            for entry in entries
            if (found := dates.get(self._liner_id(entry.path))) is None
            or found.refusal(today) is None
        )
