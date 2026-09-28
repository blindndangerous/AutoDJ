"""Daypart mood targets: pick BPM/energy targets by time of day.

When ``[playback] enable_daypart`` (or ``--daypart``, or the web toggle)
is on and no preset or mood arc applies, the picker nudges track
selection toward the current wall-clock daypart, like a radio station's
clock-driven rotation:

- morning (06:00-10:00): calm, around 80 BPM, low energy
- midday (10:00-14:00): moderate, around 105 BPM
- afternoon (14:00-18:00): rising, around 115 BPM
- evening (18:00-22:00): peak, around 128 BPM, high energy
- night (22:00-06:00): late-night, around 90 BPM
"""

from __future__ import annotations

# (start hour, target BPM, target energy, BPM weight).  Each row runs until
# the next row's start hour; the last one wraps past midnight.
_DAYPARTS: tuple[tuple[int, float, float, float], ...] = (
    (6, 80.0, 0.04, 0.3),  # morning
    (10, 105.0, 0.06, 0.25),  # midday
    (14, 115.0, 0.07, 0.25),  # afternoon
    (18, 128.0, 0.10, 0.3),  # evening
    (22, 90.0, 0.05, 0.25),  # night
)


def daypart_target(hour: int) -> tuple[float, float, float]:
    """Return ``(target_bpm, bpm_weight, target_energy)`` for local *hour* (0-23)."""
    row = _DAYPARTS[-1]
    for candidate in _DAYPARTS:
        if hour >= candidate[0]:
            row = candidate
    _start, target_bpm, target_energy, bpm_weight = row
    return target_bpm, bpm_weight, target_energy
