"""Daypart mood profiles — pick BPM/energy targets by time of day.

A "daypart" is a named slice of the 24-hour clock that maps to a
target BPM range and energy level.  When enabled, AutoDJ uses the
*current local time* to nudge track selection toward whichever
daypart is active — gentler tempos in the morning, energetic in the
evening, etc.  Same idea as a radio station's clock-driven music
rotation.

This is a separate signal from explicit user presets.  Presets shape
the BPM curve over a single session ("warm up over 2 hours").
Dayparts shape the *baseline target* across the wall-clock day.

Built-in profiles:

- ``morning`` (06:00-10:00)   — calm, 60-90 BPM, low energy
- ``midday``  (10:00-14:00)   — moderate, 90-115 BPM
- ``afternoon`` (14:00-18:00) — rising, 100-125 BPM
- ``evening`` (18:00-22:00)   — peak, 115-140 BPM, high energy
- ``night``   (22:00-06:00)   — chill / late-night, 70-110 BPM

Custom profiles can be defined in ``config.toml`` under ``[dayparts]``;
see :func:`from_config_dict`.

Example:
    >>> from autodj.daypart import current_daypart_target
    >>> target = current_daypart_target()
    >>> target.target_bpm
    120.0
    >>> target.target_energy
    0.07

The daypart hook is opt-in — ``[playback] enable_daypart = true`` in
config, or ``--daypart`` on the CLI, or the toggle in the web UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Daypart:
    """A named slice of the day with target BPM + energy.

    Attributes:
        name: Human-readable name (``"morning"``, ``"evening"`` …).
        start_hour: Local hour the daypart begins (0-23 inclusive).
        end_hour: Local hour the daypart ends (0-23 exclusive).
            Set ``end_hour < start_hour`` to wrap past midnight.
        target_bpm: Centre of the BPM Gaussian for track scoring.
        target_energy: Centre of the energy Gaussian (0.0-1.0).
        bpm_weight: How heavily BPM affects the score (0.0-1.0).  Higher
            = stronger pull toward target_bpm.  Default 0.25.
    """

    name: str
    start_hour: int
    end_hour: int
    target_bpm: float
    target_energy: float
    bpm_weight: float = 0.25

    def covers(self, hour: int) -> bool:
        """Return True iff *hour* (0-23) falls inside this daypart's window."""
        if self.start_hour <= self.end_hour:
            return self.start_hour <= hour < self.end_hour
        # Wraps past midnight (e.g. 22 → 06)
        return hour >= self.start_hour or hour < self.end_hour


# Built-in daypart definitions — exposed via DAYPARTS for tests + introspection.
DAYPARTS: list[Daypart] = [
    Daypart(
        name="morning",
        start_hour=6,
        end_hour=10,
        target_bpm=80.0,
        target_energy=0.04,
        bpm_weight=0.3,
    ),
    Daypart(
        name="midday",
        start_hour=10,
        end_hour=14,
        target_bpm=105.0,
        target_energy=0.06,
        bpm_weight=0.25,
    ),
    Daypart(
        name="afternoon",
        start_hour=14,
        end_hour=18,
        target_bpm=115.0,
        target_energy=0.07,
        bpm_weight=0.25,
    ),
    Daypart(
        name="evening",
        start_hour=18,
        end_hour=22,
        target_bpm=128.0,
        target_energy=0.10,
        bpm_weight=0.3,
    ),
    Daypart(
        name="night",
        start_hour=22,
        end_hour=6,
        target_bpm=90.0,
        target_energy=0.05,
        bpm_weight=0.25,
    ),
]


def daypart_for_hour(hour: int, profiles: list[Daypart] | None = None) -> Daypart:
    """Return the active daypart for the given local *hour* (0-23).

    Falls back to the first daypart in *profiles* if none cover *hour*
    — should never happen with the built-ins because ``night`` wraps
    midnight, but a custom config might leave gaps.

    Args:
        hour: Local hour, 0-23 inclusive.
        profiles: Override the built-in list.  ``None`` uses :data:`DAYPARTS`.

    Returns:
        The matching :class:`Daypart`.

    Example:
        >>> daypart_for_hour(7).name
        'morning'
        >>> daypart_for_hour(20).name
        'evening'
        >>> daypart_for_hour(2).name   # wraps past midnight
        'night'
    """
    if not 0 <= hour <= 23:
        raise ValueError(f"hour must be in [0, 23], got {hour}")
    src = profiles or DAYPARTS
    for dp in src:
        if dp.covers(hour):
            return dp
    return src[0]


def current_daypart(now: datetime | None = None, profiles: list[Daypart] | None = None) -> Daypart:
    """Return the daypart active at *now* (defaults to current local time).

    Args:
        now: Optional datetime override (handy for tests).
        profiles: Override the built-in list.

    Returns:
        The :class:`Daypart` covering ``now.hour``.
    """
    if now is None:
        now = datetime.now()
    return daypart_for_hour(now.hour, profiles)


def from_config_dict(data: dict[str, Any]) -> list[Daypart]:
    """Build a list of :class:`Daypart` from a TOML-loaded dict.

    Expected shape::

        [dayparts.warmup]
        start_hour = 7
        end_hour = 9
        target_bpm = 70
        target_energy = 0.03
        bpm_weight = 0.4

    Args:
        data: Dict where each key is a daypart name and each value is a
            dict of ``start_hour`` / ``end_hour`` / ``target_bpm`` /
            ``target_energy`` / optional ``bpm_weight``.

    Returns:
        List of :class:`Daypart` instances in dict-iteration order.
        Returns the built-in :data:`DAYPARTS` if *data* is empty.

    Raises:
        ValueError: If a daypart entry is missing required fields or
            has an out-of-range hour.
    """
    if not data:
        return DAYPARTS
    out: list[Daypart] = []
    for name, fields in data.items():
        if not isinstance(fields, dict):
            raise ValueError(f"daypart '{name}' must be a TOML table")
        try:
            start = int(fields["start_hour"])
            end = int(fields["end_hour"])
            bpm = float(fields["target_bpm"])
            energy = float(fields["target_energy"])
        except KeyError as exc:
            raise ValueError(
                f"daypart '{name}' missing required field {exc}",
            ) from exc
        weight = float(fields.get("bpm_weight", 0.25))
        if not 0 <= start <= 23 or not 0 <= end <= 23:
            raise ValueError(
                f"daypart '{name}' hours must be 0-23, got start={start} end={end}",
            )
        out.append(
            Daypart(
                name=str(name),
                start_hour=start,
                end_hour=end,
                target_bpm=bpm,
                target_energy=energy,
                bpm_weight=weight,
            )
        )
    return out
