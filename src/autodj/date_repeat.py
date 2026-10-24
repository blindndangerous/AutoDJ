"""Date ranges that repeat: the same days every week or every year.

A category's season (``not_before`` and ``not_after``) and a schedule
event's date range (``start_date`` and ``end_date``) are saved as full
dates.  ``dates_repeat`` next to them says how to read them:

- ``"none"`` (the default): the dates are fixed days, both included, as
  they always were.
- ``"weekly"`` (categories only): the first date's weekday to the last
  date's weekday, every week, from the first date on with no end.  The
  last date is at most 6 days after the first, so the window may run past
  Sunday, such as Saturday to Monday.  Without a last date the window is
  the first date's weekday alone.
- ``"yearly"``: the first date's month and day to the last date's, every
  year; the years are ignored.  A range whose last day comes before its
  first in the year runs past New Year, such as 20 December to 2 January.
  Without a last date, or with the same month and day, it is that one day.
  29 February counts as 28 February in a year that has no 29 February.

A repeating range needs its first date.  The ``"none"`` value is never
written to a file, so an older AutoDJ, which refuses unknown keys, still
reads files whose ranges do not repeat.
"""

from __future__ import annotations

import calendar
from datetime import date as Date
from datetime import timedelta
from typing import Literal

DateRepeat = Literal["none", "weekly", "yearly"]

#: A weekly window's last date is at most this many days after its first.
WEEK_SPAN = 6


def on_year(day: Date, year: int) -> Date:
    """*day*'s month and day in *year*; 29 February is 28 February in a common year."""
    if day.month == 2 and day.day == 29 and not calendar.isleap(year):
        return Date(year, 2, 28)
    return day.replace(year=year)


def problem(first: Date | None, last: Date | None, repeat: DateRepeat) -> str | None:
    """What is wrong with a range, as a key, or ``None`` when it is fine.

    ``"needs_first"``: a repeating range has no first date.
    ``"backwards"``: the last date is before the first (not for yearly).
    ``"too_long"``: a weekly window runs longer than a week.
    """
    if repeat != "none" and first is None:
        return "needs_first"
    if repeat == "yearly" or first is None or last is None:
        return None
    if last < first:
        return "backwards"
    if repeat == "weekly" and (last - first).days > WEEK_SPAN:
        return "too_long"
    return None


def contains(day: Date, first: Date | None, last: Date | None, repeat: DateRepeat) -> bool:
    """Whether *day* is within the range, both ends included."""
    if repeat == "none" or first is None:
        return (first is None or day >= first) and (last is None or day <= last)
    end = last or first
    if repeat == "weekly":
        return day >= first and (day - first).days % 7 <= (end - first).days
    start, stop = on_year(first, day.year), on_year(end, day.year)
    if (first.month, first.day) <= (end.month, end.day):
        return start <= day <= stop
    return day >= start or day <= stop  # past New Year


def next_day(day: Date, first: Date | None, last: Date | None, repeat: DateRepeat) -> Date | None:
    """The first day on or after *day* within the range; ``None`` when none is left."""
    if repeat == "none" or first is None:
        found = max(day, first) if first is not None else day
        return None if last is not None and found > last else found
    if contains(day, first, last, repeat):
        return day
    if repeat == "weekly":
        found = max(day, first)
        offset = (found - first).days % 7
        return found if offset == 0 else found + timedelta(days=7 - offset)
    start = on_year(first, day.year)
    return start if start > day else on_year(first, day.year + 1)


def weekday_name(day: Date) -> str:
    """The weekday of *day* in words: "Friday"."""
    return calendar.day_name[day.weekday()]


def month_day(day: Date) -> str:
    """The day and month of *day* in words: "20 December"."""
    return f"{day.day} {calendar.month_name[day.month]}"
