"""Timed station events: repeat rules, an action registry, durable state and run history.

An event pairs a repeat rule (once, daily, weekdays, chosen days, or every N
minutes within an optional window) with an action from the registry.  Wall
times are read in the station time zone.  A wall time that a daylight-saving
change skips runs at the instant the clock jumps; one that happens twice runs
the first time only.

Features register their own actions (:meth:`Scheduler.register`) before the
scheduler starts; the web UI builds its action list and fields from
:meth:`Action.metadata`.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as Date
from datetime import time as Time
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    model_validator,
)

from autodj.fsutil import atomic_write

logger = logging.getLogger(__name__)

SCHEMA_VERSION: Literal[1] = 1
MAX_EVENTS = 100
MAX_RUNS = 200
NAME_MAX = 100
PARAM_MAX = 200
# A due time older than this when the scheduler sees it was missed (the
# computer slept, or the server stalled), so it is logged, not run late.
MISSED_AFTER = timedelta(minutes=2)

Repeat = Literal["once", "daily", "weekdays", "days", "interval"]
Timing = Literal["hard", "soft"]
RunResult = Literal["ok", "error", "missed"]

# Words for each field, used in error messages that name the field.
FIELD_LABELS = {
    "name": "Name",
    "action": "Action",
    "params": "Action settings",
    "repeat": "Repeat",
    "date": "Date",
    "time": "Time",
    "days": "Days",
    "interval_minutes": "Minutes between runs",
    "window_start": "Active from",
    "window_end": "Active until",
    "timing": "Timing",
    "enabled": "Enabled",
}


class EventError(ValueError):
    """A rejected event, naming the field at fault (``params.<name>`` for an action setting)."""

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(message)
        self.field = field_name


def system_timezone() -> str:
    """Return the system's IANA time zone name, or ``"UTC"`` when it has none."""
    from tzlocal import get_localzone_name

    try:
        name = get_localzone_name()
        if name:
            ZoneInfo(name)
            return name
    except (LookupError, ValueError, OSError):
        pass
    logger.warning("Could not identify the system time zone; schedules use UTC")
    return "UTC"


Name = Annotated[
    StrictStr, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX)
]
Key = Annotated[StrictStr, StringConstraints(min_length=1, max_length=64)]
ParamValue = Annotated[StrictStr, StringConstraints(max_length=PARAM_MAX)]


def _whole_minute(value: Time | None, field_name: str) -> None:
    if value is None:
        return
    if value.tzinfo is not None:
        raise EventError(field_name, f"{FIELD_LABELS[field_name]} must be a local time")
    if value.second or value.microsecond:
        raise EventError(field_name, f"{FIELD_LABELS[field_name]} must be a whole minute")


class EventSpec(BaseModel):
    """The editable part of an event, as the API receives it."""

    model_config = ConfigDict(extra="forbid")

    name: Name
    action: Key
    params: dict[Key, ParamValue] = Field(default_factory=dict, max_length=8)
    repeat: Repeat
    date: Date | None = None
    time: Time | None = None
    days: list[StrictInt] = Field(default_factory=list, max_length=7)
    interval_minutes: StrictInt | None = None
    window_start: Time | None = None
    window_end: Time | None = None
    timing: Timing = "soft"
    enabled: StrictBool = True

    @model_validator(mode="after")
    def _check_rule(self) -> Self:
        """Require the fields the repeat rule uses and drop the others."""
        for name in ("time", "window_start", "window_end"):
            _whole_minute(getattr(self, name), name)
        if self.repeat == "once" and self.date is None:
            raise EventError("date", "Enter a date for a one-time event")
        if self.repeat != "interval" and self.time is None:
            raise EventError("time", "Enter a time")
        if self.repeat == "days":
            if not self.days:
                raise EventError("days", "Choose at least one day")
            if any(day not in range(7) for day in self.days):
                raise EventError("days", "Days must be numbered 0 (Monday) to 6 (Sunday)")
            self.days = sorted(set(self.days))
        else:
            self.days = []
        if self.repeat == "interval":
            if self.interval_minutes is None or not 1 <= self.interval_minutes <= 1440:
                raise EventError("interval_minutes", "Minutes between runs must be from 1 to 1440")
            if (self.window_start is None) != (self.window_end is None):
                missing = "window_end" if self.window_end is None else "window_start"
                raise EventError(
                    missing, "Enter both Active from and Active until, or leave both empty"
                )
            if self.window_start is not None and self.window_start == self.window_end:
                raise EventError("window_end", "Active until must differ from Active from")
            self.time = None
        else:
            self.interval_minutes = self.window_start = self.window_end = None
        if self.repeat != "once":
            self.date = None
        return self


class ScheduledEvent(EventSpec):
    """A stored event: the spec plus its server-assigned ID."""

    id: Annotated[StrictStr, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class RunRecord(BaseModel):
    """One line of the run history."""

    model_config = ConfigDict(extra="forbid")

    time: datetime
    event_id: StrictStr
    event: StrictStr
    summary: StrictStr
    result: RunResult
    message: StrictStr = ""


class ScheduleFile(BaseModel):
    """The on-disk schedule document."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    events: list[ScheduledEvent] = Field(max_length=MAX_EVENTS)
    runs: list[RunRecord]

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        if len({event.id for event in self.events}) != len(self.events):
            raise ValueError("duplicate event ID")
        return self


def field_error(exc: ValidationError) -> tuple[str, str]:
    """Return the field and a message naming it for the first error in *exc*."""
    error = exc.errors()[0]
    original = (error.get("ctx") or {}).get("error")
    if isinstance(original, EventError):
        return original.field, str(original)
    loc = [str(part) for part in error["loc"]]
    if not loc:
        return "", str(error["msg"])
    if error["type"] == "extra_forbidden":
        return loc[0], f"Unknown field {loc[0]}"
    if loc[0] == "name":
        return "name", f"Enter a name of up to {NAME_MAX} characters"
    label = FIELD_LABELS.get(loc[0], loc[0])
    if loc[0] == "params" and len(loc) > 1:
        return f"params.{loc[1]}", f"{label}: {loc[1]}: {error['msg']}"
    return loc[0], f"{label}: {error['msg']}"


# ----------------------------------------------------------------------
# Repeat rules
# ----------------------------------------------------------------------


def _resolve(day: Date, at: Time, zone: ZoneInfo) -> datetime:
    """Return the UTC instant of a wall time in *zone*.

    An ambiguous wall time (the clock goes back) gives its first instant.
    A skipped one (the clock goes forward) gives the instant of the jump,
    the first valid time after it.
    """
    wall = datetime.combine(day, at)
    first = wall.replace(tzinfo=zone, fold=0)
    if first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == wall:
        return first.astimezone(UTC)
    # In a gap, fold=1 reads the wall time with the offset after the jump,
    # an instant before it, and fold=0 with the offset before, one after.
    lo = wall.replace(tzinfo=zone, fold=1).astimezone(UTC)
    hi = first.astimezone(UTC)
    target = hi.astimezone(zone).utcoffset()
    while hi - lo > timedelta(seconds=1):
        mid = lo + (hi - lo) / 2
        if mid.astimezone(zone).utcoffset() == target:
            hi = mid
        else:
            lo = mid
    # Offsets change on whole seconds, so the jump is hi rounded down.
    return hi.replace(microsecond=0)


def _next_interval(event: EventSpec, after: datetime, zone: ZoneInfo) -> datetime | None:
    assert event.interval_minutes is not None
    step = timedelta(minutes=event.interval_minutes)
    after_wall = after.astimezone(zone).replace(tzinfo=None)
    start = event.window_start or Time(0)
    # A window that starts the day before may run past midnight into today.
    for offset in (-1, 0, 1):
        day = after_wall.date() + timedelta(days=offset)
        first = datetime.combine(day, start)
        if event.window_start is None or event.window_end is None:
            last = datetime.combine(day + timedelta(days=1), Time(0)) - timedelta(seconds=1)
        else:
            last = datetime.combine(day, event.window_end)
            if event.window_end < event.window_start:
                last += timedelta(days=1)
        # Wall times map to instants in order, so no slot before after_wall
        # can fall after *after*.
        slot = first + max(0, (after_wall - first) // step) * step
        while slot <= last:
            instant = _resolve(slot.date(), slot.time(), zone)
            if instant > after:
                return instant
            slot += step
    return None  # pragma: no cover - tomorrow's window always has a slot


def next_run(event: EventSpec, after: datetime, zone: ZoneInfo) -> datetime | None:
    """Return the first UTC run of *event* strictly after the aware instant *after*.

    ``None`` means it never runs again (a one-time event in the past).
    """
    if after.tzinfo is None:
        raise ValueError("after must have a time zone")
    after = after.astimezone(UTC)
    if event.repeat == "interval":
        return _next_interval(event, after, zone)
    assert event.time is not None
    if event.repeat == "once":
        assert event.date is not None
        instant = _resolve(event.date, event.time, zone)
        return instant if instant > after else None
    today = after.astimezone(zone).date()
    for offset in range(8):
        day = today + timedelta(days=offset)
        if event.repeat == "weekdays" and day.weekday() >= 5:
            continue
        if event.repeat == "days" and day.weekday() not in event.days:
            continue
        instant = _resolve(day, event.time, zone)
        if instant > after:
            return instant
    return None  # pragma: no cover - every rule matches within a week


def _hhmm(value: Time | None) -> str:
    return value.strftime("%H:%M") if value is not None else ""


def describe_repeat(event: EventSpec) -> str:
    """Say when *event* runs, in words, for the UI and the log."""
    at = _hhmm(event.time)
    if event.repeat == "once":
        assert event.date is not None
        day = event.date
        weekday = calendar.day_name[day.weekday()]
        return f"Once, {weekday} {day.day} {calendar.month_name[day.month]} {day.year} at {at}"
    if event.repeat == "daily":
        return f"Every day at {at}"
    if event.repeat == "weekdays":
        return f"Weekdays at {at}"
    if event.repeat == "days":
        names = [calendar.day_name[day] for day in event.days]
        joined = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
        return f"{joined} at {at}"
    text = f"Every {event.interval_minutes} minutes"
    if event.window_start is not None:
        text += f" from {_hhmm(event.window_start)} until {_hhmm(event.window_end)}"
    return text


# ----------------------------------------------------------------------
# Action registry
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ActionField:
    """One setting of an action; every value is a string.

    ``choices`` lists fixed ``(value, label)`` pairs.  ``names`` lists the
    values that exist right now (profiles, liners).  It is read when the
    form is built and when an event is saved, not when a stored event
    loads, so a removed profile makes that event's runs fail instead of
    discarding the event.  ``more`` adds ``(value, label)`` pairs that
    exist right now, after the fixed ``choices``, checked like ``names``.
    A field with none of them is free text.
    """

    name: str
    label: str
    choices: tuple[tuple[str, str], ...] = ()
    names: Callable[[], Sequence[str]] | None = None
    more: Callable[[], Sequence[tuple[str, str]]] | None = None

    def pairs(self) -> list[tuple[str, str]]:
        """The fixed choices followed by the ones that exist right now."""
        return [*self.choices, *(self.more() if self.more is not None else ())]

    def metadata(self) -> dict[str, Any]:
        """Describe the field for the web form."""
        if self.choices or self.more is not None:
            options = [{"value": value, "label": label} for value, label in self.pairs()]
        elif self.names is not None:
            options = [{"value": name, "label": name} for name in self.names()]
        else:
            return {"name": self.name, "label": self.label, "type": "text"}
        return {"name": self.name, "label": self.label, "type": "select", "options": options}


@dataclass(frozen=True)
class Action:
    """A registered action: its form, its handler and where hard timing interrupts.

    ``interrupt`` says when a hard event fades out the current song:
    ``"after"`` the change, so the next song reflects it; ``"before"`` it,
    so a liner airs over the next song; or ``"none"``.
    """

    name: str
    description: str
    handle: Callable[[dict[str, str]], Awaitable[None]]
    fields: tuple[ActionField, ...] = ()
    interrupt: Literal["before", "after", "none"] = "after"

    def metadata(self) -> dict[str, Any]:
        """Describe the action for the web form (reads ``names`` lists)."""
        return {
            "name": self.name,
            "description": self.description,
            "fields": [item.metadata() for item in self.fields],
        }

    def check(self, params: dict[str, str], *, live: bool = False) -> dict[str, str]:
        """Return *params* trimmed, or raise :class:`EventError` naming the bad one.

        With *live*, a value must also be one that exists now.
        """
        known = {item.name for item in self.fields}
        for name in params:
            if name not in known:
                raise EventError("params", f"{self.description} has no setting {name}")
        clean: dict[str, str] = {}
        for item in self.fields:
            key = f"params.{item.name}"
            value = params.get(item.name, "").strip()
            if not value:
                verb = "Choose" if item.choices or item.names or item.more else "Enter"
                raise EventError(key, f"{verb} {item.label.lower()}")
            # Like names, the choices from ``more`` are checked only live.
            checked = item.choices if item.more is None else live
            if checked and value not in {choice for choice, _ in item.pairs()}:
                raise EventError(key, f"{item.label}: {value} is not one of the choices")
            if live and item.names is not None and value not in item.names():
                raise EventError(key, f"{item.label} {value} was not found")
            clean[item.name] = value
        return clean

    def describe(self, params: dict[str, str]) -> str:
        """Say what the action does with *params*, for the UI and the log."""
        values = [params.get(item.name, "") for item in self.fields]
        parts = [
            dict(item.pairs()).get(value, value)
            for item, value in zip(self.fields, values, strict=True)
        ]
        return f"{self.description}: {', '.join(parts)}" if parts else self.description


# ----------------------------------------------------------------------
# The scheduler
# ----------------------------------------------------------------------


class Scheduler:
    """Own the events, run them when due, keep the run history and the file.

    Everything runs on the event loop: the API routes, :meth:`tick` once a
    second, and :meth:`track_changed`, which the player thread reaches
    through ``loop.call_soon_threadsafe``.
    """

    def __init__(
        self,
        path: Path,
        timezone: str,
        publish: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        interrupt: Callable[[], None] = lambda: None,
        song_playing: Callable[[], bool] = lambda: False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Create an empty scheduler; :meth:`start` loads the file.

        Args:
            path: The schedule file.
            timezone: The station's IANA time zone.
            publish: Sends a message to every web page.
            interrupt: Fades out the current song (the skip path).
            song_playing: Whether a song is playing, for soft timing.
            clock: The current aware time.
        """
        self.path = path
        self.timezone = timezone
        self.zone = ZoneInfo(timezone)
        self._publisher = publish
        self._interrupt = interrupt
        self._song_playing = song_playing
        self._clock = clock
        self.actions: dict[str, Action] = {}
        self.events: dict[str, ScheduledEvent] = {}
        self.runs: list[RunRecord] = []
        # Soft events waiting for the current song to end, in due order.
        self.pending: list[str] = []
        self.started = False
        self.cursor = clock()
        self._tasks: set[asyncio.Task[None]] = set()

    def register(self, action: Action) -> None:
        """Add *action*; every action must be registered before :meth:`start`."""
        if self.started:
            raise RuntimeError("register scheduler actions before the scheduler starts")
        if action.name in self.actions:
            raise ValueError(f"action already registered: {action.name}")
        self.actions[action.name] = action

    def start(self) -> None:
        """Load the file, record missed one-time events and count from now."""
        self._load()
        self.started = True
        self.cursor = self._clock()
        missed = [
            event
            for event in self.events.values()
            if event.enabled
            and event.repeat == "once"
            and next_run(event, self.cursor, self.zone) is None
        ]
        for event in missed:
            event.enabled = False
            logger.warning("Scheduled event %r was missed while the server was down", event.name)
            self._append_run(event, "missed", "Missed while the server was not running")
        if missed:
            self._save_quietly()

    # -- storage --------------------------------------------------------

    def _keep_bad_copy(self, reason: str) -> None:
        bad = self.path.with_name(f"{self.path.name}.bad-{uuid.uuid4().hex[:8]}")
        try:
            bad.write_bytes(self.path.read_bytes())
        except OSError:
            logger.exception("Could not keep a copy of the invalid schedule file %s", self.path)
        logger.error("Schedule file %s is invalid (%s); kept a copy as %s", self.path, reason, bad)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = ScheduleFile.model_validate_json(self.path.read_bytes())
        except (OSError, ValueError) as exc:
            self._keep_bad_copy(str(exc).splitlines()[0])
            return
        dropped = []
        for event in data.events:
            action = self.actions.get(event.action)
            try:
                if action is None:
                    raise EventError("action", f"unknown action {event.action}")
                event.params = action.check(event.params)
            except EventError as exc:
                dropped.append(f"{event.name}: {exc}")
                continue
            self.events[event.id] = event
        self.runs = data.runs[-MAX_RUNS:]
        if dropped:
            self._keep_bad_copy("; ".join(dropped))

    def save(self) -> None:
        """Write the schedule atomically."""
        document = ScheduleFile(
            schema_version=SCHEMA_VERSION, events=list(self.events.values()), runs=self.runs
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, document.model_dump_json(indent=2) + "\n")

    def _save_quietly(self) -> None:
        """Save from the timer: a failed write is logged, never raised."""
        try:
            self.save()
        except OSError:
            logger.exception("Could not save the schedule to %s", self.path)

    async def _publish(self, message: dict[str, Any]) -> None:
        try:
            await self._publisher(message)
        except Exception:
            logger.exception("Could not tell the web pages about a schedule change")

    async def _changed(self) -> None:
        self.save()
        await self._publish({"type": "schedule_changed"})

    # -- editing --------------------------------------------------------

    async def _checked(self, spec: EventSpec) -> dict[str, str]:
        action = self.actions.get(spec.action)
        if action is None:
            raise EventError("action", f"Action {spec.action} is not available")
        # A live check lists profiles and liners on disk.
        params = await asyncio.to_thread(action.check, spec.params, live=True)
        now = self._clock()
        if spec.enabled and next_run(spec, now, self.zone) is None:
            today = now.astimezone(self.zone).date()
            raise EventError(
                "time" if spec.date == today else "date", "Choose a date and time in the future"
            )
        return params

    async def add(self, spec: EventSpec) -> ScheduledEvent:
        """Add an event; raise :class:`EventError` when it is not valid."""
        if len(self.events) >= MAX_EVENTS:
            raise EventError("", f"The schedule holds at most {MAX_EVENTS} events")
        params = await self._checked(spec)
        event = ScheduledEvent(**{**spec.model_dump(), "params": params, "id": uuid.uuid4().hex})
        self.events[event.id] = event
        await self._changed()
        return event

    async def replace(self, event_id: str, spec: EventSpec) -> ScheduledEvent:
        """Replace an event's spec; raise ``KeyError`` for an unknown ID."""
        if event_id not in self.events:
            raise KeyError(event_id)
        params = await self._checked(spec)
        event = ScheduledEvent(**{**spec.model_dump(), "params": params, "id": event_id})
        self.events[event_id] = event
        self._forget_pending(event_id)
        await self._changed()
        return event

    async def remove(self, event_id: str) -> ScheduledEvent:
        """Delete an event; raise ``KeyError`` for an unknown ID."""
        event = self.events.pop(event_id)
        self._forget_pending(event_id)
        await self._changed()
        return event

    async def set_enabled(self, event_id: str, enabled: bool) -> ScheduledEvent:
        """Enable or disable an event; raise ``KeyError`` for an unknown ID."""
        event = self.events[event_id]
        if enabled and next_run(event, self._clock(), self.zone) is None:
            raise EventError("enabled", "This one-time event is in the past; edit its date first")
        event.enabled = enabled
        if not enabled:
            self._forget_pending(event_id)
        await self._changed()
        return event

    def _forget_pending(self, event_id: str) -> None:
        self.pending = [item for item in self.pending if item != event_id]

    # -- running --------------------------------------------------------

    def summary(self, event: EventSpec) -> str:
        """Say what *event*'s action does."""
        action = self.actions.get(event.action)
        return action.describe(event.params) if action else event.action

    def _append_run(self, event: ScheduledEvent, result: RunResult, message: str) -> RunRecord:
        record = RunRecord(
            time=self._clock(),
            event_id=event.id,
            event=event.name,
            summary=self.summary(event),
            result=result,
            message=message,
        )
        self.runs = [*self.runs, record][-MAX_RUNS:]
        return record

    async def _record(self, event: ScheduledEvent, result: RunResult, message: str) -> RunRecord:
        record = self._append_run(event, result, message)
        self._save_quietly()
        await self._publish({"type": "schedule_run", "run": record.model_dump(mode="json")})
        return record

    async def run(self, event: ScheduledEvent) -> RunRecord:
        """Run *event*'s action now; a failure is recorded, never raised."""
        action = self.actions.get(event.action)
        summary = self.summary(event)
        try:
            if action is None:
                raise ValueError(f"Action {event.action} is not available")
            hard = event.timing == "hard"
            if hard and action.interrupt == "before":
                self._interrupt()
            await action.handle(dict(event.params))
            if hard and action.interrupt == "after":
                self._interrupt()
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            logger.warning("Scheduled event %r (%s) failed: %s", event.name, summary, message)
            logger.debug("Scheduled event failure", exc_info=True)
            return await self._record(event, "error", message)
        logger.info("Scheduled event %r ran: %s", event.name, summary)
        return await self._record(event, "ok", "")

    async def tick(self) -> None:
        """Run, queue or log every event that fell due since the last tick."""
        now = self._clock()
        before = self.cursor
        if now <= before:
            # The clock went back: wait until it passes the last tick
            # rather than run those events a second time.
            return
        self.cursor = now
        disabled = False
        for event in list(self.events.values()):
            if not event.enabled:
                continue
            due = next_run(event, before, self.zone)
            if due is None or due > now:
                continue
            if event.repeat == "once":
                event.enabled = False
                disabled = True
            if now - due > MISSED_AFTER:
                logger.warning("Scheduled event %r due at %s was missed", event.name, due)
                await self._record(event, "missed", "Missed while the computer slept or stalled")
            elif event.timing == "soft" and self._song_playing():
                if event.id not in self.pending:
                    self.pending.append(event.id)
            else:
                await self.run(event)
        if disabled:
            self._save_quietly()
            await self._publish({"type": "schedule_changed"})
        if self.pending and not self._song_playing():
            await self._run_pending()

    def track_changed(self) -> None:
        """Run the soft events that waited for the song that just ended (event loop)."""
        if not self.pending:
            return
        task = asyncio.get_running_loop().create_task(self._run_pending())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_pending(self) -> None:
        waiting, self.pending = self.pending, []
        for event_id in waiting:
            event = self.events.get(event_id)
            if event is not None:
                await self.run(event)

    def upcoming(self, count: int) -> list[tuple[datetime, ScheduledEvent]]:
        """Return the next *count* runs across every enabled event, soonest first."""
        now = self._clock()
        rows: list[tuple[datetime, ScheduledEvent]] = []
        for event in self.events.values():
            if not event.enabled:
                continue
            cursor = now
            for _ in range(count):
                when = next_run(event, cursor, self.zone)
                if when is None:
                    break
                rows.append((when, event))
                cursor = when
        rows.sort(key=lambda row: row[0])
        return rows[:count]
