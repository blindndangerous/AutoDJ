"""Listener statistics: peaks, sessions and listening time by day and format (#50).

For each day, in the station's time zone, it keeps the most listeners
connected at once and when that was, how many listening sessions there
were, their total and longest length, how many fell in each length band,
and the peak, sessions and listening time of each format (MP3, AAC,
Opus).  Days older than ``[stream] stats_days`` are deleted; 0 keeps
none.

Nothing identifies a listener: no address, player name, connection time
or anything else about one connection is ever written, only these daily
counts and totals.  A session counts on the day it began.

The counts live in memory and are written to the station's
``listener_stats.json`` about once a minute, and when the station stops.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from autodj.fsutil import atomic_write, set_aside
from autodj.playlog import safe_csv
from autodj.stream_formats import FORMATS

logger = logging.getLogger(__name__)

#: Session length bands: (upper bound in seconds, or None for no bound, label).
SESSION_BANDS: tuple[tuple[float | None, str], ...] = (
    (60.0, "Under 1 minute"),
    (300.0, "1 to 5 minutes"),
    (900.0, "5 to 15 minutes"),
    (3600.0, "15 minutes to 1 hour"),
    (3 * 3600.0, "1 to 3 hours"),
    (None, "3 hours or more"),
)
# The CSV column for each band.
_BAND_COLUMNS = (
    "sessions_under_1_minute",
    "sessions_1_to_5_minutes",
    "sessions_5_to_15_minutes",
    "sessions_15_to_60_minutes",
    "sessions_1_to_3_hours",
    "sessions_3_hours_or_more",
)
_SAVE_EVERY_SECONDS = 60.0
_VERSION = 1
# A peak's time of day, as saved: "21:45".
_PEAK_AT = re.compile(r"\d\d:\d\d")
#: A session's start: wall-clock seconds (its day) and monotonic seconds (its length).
Session = tuple[float, float]


def session_band(seconds: float) -> int:
    """The index in :data:`SESSION_BANDS` of a session *seconds* long."""
    for index, (limit, _label) in enumerate(SESSION_BANDS):
        if limit is not None and seconds < limit:
            return index
    return len(SESSION_BANDS) - 1


def _empty_row(day: str) -> dict[str, Any]:
    return {
        "date": day,
        "peak": 0,
        "peak_at": None,
        "sessions": 0,
        "seconds": 0.0,
        "longest": 0.0,
        "bands": [0] * len(SESSION_BANDS),
        "formats": {},
    }


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("not a count")
    return value


def _seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("not a length")
    if not math.isfinite(value) or value < 0:
        raise ValueError("not a length")
    return float(value)


def _checked_row(raw: Any) -> dict[str, Any]:
    """A saved day, checked field by field; anything else raises ``ValueError``."""
    if not isinstance(raw, dict):
        raise ValueError("a day must be an object")
    day = date.fromisoformat(raw["date"]).isoformat()
    peak_at = raw.get("peak_at")
    if peak_at is not None and (not isinstance(peak_at, str) or not _PEAK_AT.fullmatch(peak_at)):
        raise ValueError("bad peak time")
    bands = raw["bands"]
    if not isinstance(bands, list) or len(bands) != len(SESSION_BANDS):
        raise ValueError("bad session bands")
    formats = raw.get("formats", {})
    if not isinstance(formats, dict):
        raise ValueError("bad formats")
    return {
        "date": day,
        "peak": _count(raw["peak"]),
        "peak_at": peak_at,
        "sessions": _count(raw["sessions"]),
        "seconds": _seconds(raw["seconds"]),
        "longest": _seconds(raw["longest"]),
        "bands": [_count(value) for value in bands],
        "formats": {
            name: {
                "peak": _count(entry["peak"]),
                "sessions": _count(entry["sessions"]),
                "seconds": _seconds(entry["seconds"]),
            }
            for name, entry in formats.items()
            if name in FORMATS and isinstance(entry, dict)
        },
    }


def _merge(into: dict[str, Any], row: dict[str, Any]) -> None:
    """Add day *row* to *into*, the same day counted since: sums, and the higher peaks."""
    if row["peak"] > into["peak"]:
        into["peak"], into["peak_at"] = row["peak"], row["peak_at"]
    into["sessions"] += row["sessions"]
    into["seconds"] = round(into["seconds"] + row["seconds"], 1)
    into["longest"] = max(into["longest"], row["longest"])
    into["bands"] = [a + b for a, b in zip(into["bands"], row["bands"], strict=True)]
    for name, entry in row["formats"].items():
        total = into["formats"].setdefault(name, {"peak": 0, "sessions": 0, "seconds": 0.0})
        total["peak"] = max(total["peak"], entry["peak"])
        total["sessions"] += entry["sessions"]
        total["seconds"] = round(total["seconds"] + entry["seconds"], 1)


class ListenerStats:
    """Count listeners as they come and go, by day and by format.

    Thread-safety: :meth:`connected` and :meth:`disconnected` run on the
    event loop, :meth:`tick` and :meth:`flush` on worker threads; one lock
    guards the counts, and only quick arithmetic happens under it.
    """

    def __init__(
        self,
        path: Path,
        *,
        timezone: str,
        keep_days: Callable[[], int],
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Load the saved days from *path*, if any.

        Args:
            path: The station's ``listener_stats.json``.
            timezone: The station's time zone, which says where a day ends.
            keep_days: Days of statistics to keep, read at each use
                (``[stream] stats_days``); 0 keeps none.
            clock: Wall-clock seconds, which say what day a session began.
            monotonic: Monotonic seconds, for session lengths and the save
                interval, so a clock change never stretches a session.
        """
        self.path = path
        self._zone = ZoneInfo(timezone)
        self._keep_days = keep_days
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._days: dict[str, dict[str, Any]] = {}
        self._live: dict[str, int] = {}
        self._dirty = False
        self._saved_at = monotonic()
        # The saved days could not be read yet (the file system said no):
        # nothing is written over them until they are, and each tick tries
        # again.
        self._unread = False
        self._load()

    # -- loading and saving ----------------------------------------------

    def _read(self) -> list[dict[str, Any]] | None:
        """The saved days, or ``None`` when the file cannot be read just now.

        A day that is not right is skipped and the others kept.  A file
        that is not a statistics file at all is moved aside with a ``.bad``
        suffix, as for the listener bans.
        """
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning(
                "Cannot read listener statistics from %s: %s; trying again", self.path, exc
            )
            return None
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or not isinstance(data.get("days"), list):
                raise ValueError("not a listener statistics file")
        except ValueError as exc:
            logger.error(
                "Cannot load listener statistics from %s: %s; starting with none", self.path, exc
            )
            set_aside(self.path, "listener statistics file", str(exc), naming="timestamp")
            return []
        rows = []
        for raw in data["days"]:
            try:
                rows.append(_checked_row(raw))
            except (ValueError, TypeError, KeyError, AttributeError):
                logger.warning("Skipping an unreadable day in %s: %.80r", self.path, raw)
        return rows

    def _load(self) -> None:
        """Read the saved days into the counts, adding to any counted meanwhile."""
        rows = self._read()
        with self._lock:
            self._unread = rows is None
            for row in rows or []:
                current = self._days.get(row["date"])
                if current is None:
                    self._days[row["date"]] = row
                else:
                    _merge(current, row)
                    self._dirty = True

    def flush(self) -> None:
        """Write the counts now if they changed; a failure is logged and retried later."""
        with self._save_lock:
            with self._lock:
                if not self._dirty or self._unread:
                    return
                days = [row for _key, row in sorted(self._days.items())]
                text = json.dumps({"version": _VERSION, "days": days}, indent=1) + "\n"
                self._dirty = False
                self._saved_at = self._monotonic()
            try:
                atomic_write(self.path, text)
            except OSError:
                logger.warning("Could not save listener statistics", exc_info=True)
                with self._lock:
                    self._dirty = True

    def tick(self) -> None:
        """Carry the listeners still connected into a new day, prune, save when due.

        Call about once a second (off the event loop).
        """
        if self._unread:
            self._load()
        now = self._clock()
        with self._lock:
            self._fold_peak(now)
            self._prune(now)
            due = self._dirty and self._monotonic() - self._saved_at >= _SAVE_EVERY_SECONDS
        if due:
            self.flush()

    # -- counting ----------------------------------------------------------

    def _local(self, at: float) -> datetime:
        return datetime.fromtimestamp(at, self._zone)

    def _oldest_kept(self, now: float) -> date:
        return self._local(now).date() - timedelta(days=max(1, self._keep_days()) - 1)

    def _row(self, day: str) -> dict[str, Any]:
        row = self._days.get(day)
        if row is None:
            row = self._days[day] = _empty_row(day)
        return row

    @staticmethod
    def _format_row(row: dict[str, Any], name: str) -> dict[str, Any]:
        entry: dict[str, Any] = row["formats"].setdefault(
            name, {"peak": 0, "sessions": 0, "seconds": 0.0}
        )
        return entry

    def _fold_peak(self, now: float) -> None:
        """Fold the listeners connected now into today's peaks (lock held)."""
        total = sum(self._live.values())
        if self._keep_days() <= 0:
            return
        local = self._local(now)
        day = local.date().isoformat()
        if not total and day not in self._days:
            return
        row = self._row(day)
        if total > row["peak"]:
            row["peak"] = total
            row["peak_at"] = local.strftime("%H:%M")
            self._dirty = True
        for name, count in self._live.items():
            if count and count > row["formats"].get(name, {}).get("peak", 0):
                self._format_row(row, name)["peak"] = count
                self._dirty = True

    def _prune(self, now: float) -> None:
        """Delete the days no longer kept (lock held)."""
        if self._keep_days() <= 0:
            stale = list(self._days)
        else:
            oldest = self._oldest_kept(now).isoformat()
            stale = [day for day in self._days if day < oldest]
        for day in stale:
            del self._days[day]
        if stale:
            self._dirty = True

    def connected(self, fmt: str) -> Session:
        """Count a listener who just connected to format *fmt*.

        Returns:
            The session's start, to hand back to :meth:`disconnected`.
        """
        now = self._clock()
        with self._lock:
            self._live[fmt] = self._live.get(fmt, 0) + 1
            self._fold_peak(now)
        return now, self._monotonic()

    def disconnected(self, fmt: str, session: Session) -> None:
        """Count the *session* of a listener to *fmt* as ended.

        Its length is measured on the monotonic clock; its wall-clock start
        only says what day it counts on.
        """
        now = self._clock()
        started, started_mono = session
        length = max(0.0, self._monotonic() - started_mono)
        with self._lock:
            self._live[fmt] = max(0, self._live.get(fmt, 0) - 1)
            if self._keep_days() <= 0:
                return
            day = self._local(started).date()
            if day < self._oldest_kept(now):
                return
            row = self._row(day.isoformat())
            row["sessions"] += 1
            row["seconds"] = round(row["seconds"] + length, 1)
            row["longest"] = round(max(row["longest"], length), 1)
            row["bands"][session_band(length)] += 1
            entry = self._format_row(row, fmt)
            entry["sessions"] += 1
            entry["seconds"] = round(entry["seconds"] + length, 1)
            self._dirty = True

    # -- reading ------------------------------------------------------------

    def summary(self, days: int, listening: Mapping[str, int]) -> dict[str, Any]:
        """The last *days* days, newest first, and their totals.

        Days without listening between the first one with some and today
        are listed with zeros, so the table has no gaps.

        Args:
            days: How many days back from today, today included.
            listening: Listeners connected now, by format.
        """
        now = self._clock()
        today = self._local(now).date()
        with self._lock:
            self._fold_peak(now)
            keep = self._keep_days()
            first = today - timedelta(days=max(1, days) - 1)
            rows = {
                day: json.loads(json.dumps(row))
                for day, row in self._days.items()
                if day >= first.isoformat()
            }
        if rows:
            start = date.fromisoformat(min(rows))
            span = (today - start).days + 1
            listed = [
                rows.get(day, _empty_row(day))
                for day in ((today - timedelta(days=back)).isoformat() for back in range(span))
            ]
        else:
            listed = []
        return {
            "keep_days": keep,
            "days_shown": days,
            "today": today.isoformat(),
            "now": {
                "total": sum(listening.values()),
                "formats": dict(listening),
            },
            "days": listed,
            "period": _period(listed),
            "bands": [label for _limit, label in SESSION_BANDS],
        }

    def csv_text(self) -> str:
        """Every kept day, oldest first, as CSV: one row per day, counts only.

        Cells go through the play log's spreadsheet guard, so none can run
        as a formula.
        """
        with self._lock:
            self._fold_peak(self._clock())
            rows = [json.loads(json.dumps(row)) for _key, row in sorted(self._days.items())]
        header = [
            "date",
            "peak_listeners",
            "peak_time",
            "sessions",
            "listening_hours",
            "average_session_minutes",
            "longest_session_minutes",
        ]
        for name in FORMATS:
            header += [f"{name}_peak_listeners", f"{name}_sessions", f"{name}_listening_hours"]
        lines: list[list[Any]] = []
        for row in rows:
            average = row["seconds"] / row["sessions"] / 60 if row["sessions"] else 0.0
            line: list[Any] = [
                row["date"],
                row["peak"],
                row["peak_at"] or "",
                row["sessions"],
                f"{row['seconds'] / 3600:.2f}",
                f"{average:.1f}",
                f"{row['longest'] / 60:.1f}",
            ]
            for name in FORMATS:
                entry = row["formats"].get(name, {"peak": 0, "sessions": 0, "seconds": 0.0})
                line += [entry["peak"], entry["sessions"], f"{entry['seconds'] / 3600:.2f}"]
            lines.append([*line, *row["bands"]])
        return safe_csv([*header, *_BAND_COLUMNS], lines)


def _period(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals over *rows*: sessions, time, the longest session, the peak day and each format."""
    best = max(rows, key=lambda row: (row["peak"], row["date"]), default=None)
    formats: dict[str, dict[str, Any]] = {}
    for row in rows:
        for name, entry in row["formats"].items():
            total = formats.setdefault(name, {"peak": 0, "sessions": 0, "seconds": 0.0})
            total["peak"] = max(total["peak"], entry["peak"])
            total["sessions"] += entry["sessions"]
            total["seconds"] = round(total["seconds"] + entry["seconds"], 1)
    return {
        "sessions": sum(row["sessions"] for row in rows),
        "seconds": round(sum(row["seconds"] for row in rows), 1),
        "longest": max((row["longest"] for row in rows), default=0.0),
        "peak": best["peak"] if best else 0,
        "peak_date": best["date"] if best and best["peak"] else None,
        "peak_at": best["peak_at"] if best and best["peak"] else None,
        "bands": [sum(row["bands"][index] for row in rows) for index in range(len(SESSION_BANDS))],
        "formats": formats,
    }
