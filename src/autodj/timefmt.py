"""How the server writes times of day and lengths on screen: one place, so every page agrees.

Times are 24 hour ("15:04"). Pages format times in the browser with
``static/modules/time-format.js``; log files keep their own timestamps.
"""

from __future__ import annotations

from datetime import datetime, time


def clock_text(moment: datetime | time, *, seconds: bool = False) -> str:
    """ "15:04", or "15:04:30" with *seconds*: a time of day."""
    return moment.strftime("%H:%M:%S" if seconds else "%H:%M")


def length_text(seconds: float) -> str:
    """ "3:45" or "1:02:05": a length as a clock shows it."""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
