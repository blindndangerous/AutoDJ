"""How the server writes times of day and lengths on screen: one place, so every page agrees.

Times are 24 hour ("15:04") unless Settings > General settings > Server sets 12 hour (#286).
Pages format times in the browser with ``static/modules/time-format.js``;
log files keep their own timestamps.
"""

from __future__ import annotations

from datetime import datetime, time

#: Settings > General settings > Server, Time format: ``"24"`` or ``"12"``.
time_format = "24"


def clock_text(moment: datetime | time, *, seconds: bool = False) -> str:
    """ "15:04", or "15:04:30" with *seconds*; "3:04 PM" in 12-hour format."""
    if time_format == "12":
        text = moment.strftime("%I:%M:%S" if seconds else "%I:%M").removeprefix("0")
        return f"{text} {'AM' if moment.hour < 12 else 'PM'}"
    return moment.strftime("%H:%M:%S" if seconds else "%H:%M")


def length_text(seconds: float) -> str:
    """ "3:45" or "1:02:05": a length as a clock shows it."""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
