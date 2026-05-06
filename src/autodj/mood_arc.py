"""Set-relative mood arc — pick BPM/energy targets along a session envelope.

A mood arc is a *set-relative* targeting curve: the user picks "now I'm
starting a 3-hour set" and AutoDJ ramps BPM and energy from a calm
warmup, through a peak around 75% of the set, and back down to a cool
finish.  This complements the wall-clock daypart in :mod:`autodj.daypart`
which is *clock-relative* and runs forever.

Use one or the other, or both: when both are enabled the arc owns the
picker target while the session is in progress; daypart is the baseline
that resumes when the arc completes (after which the arc loops).

Built-in shape (normalised to ``[0, 1]`` over arc duration)::

    progress    BPM     energy   note
    0.00        0.55    0.30     warmup
    0.30        0.80    0.55     building
    0.50        0.95    0.80     pre-peak
    0.75        1.00    1.00     peak
    0.90        0.85    0.70     wind-down
    1.00        0.55    0.35     close

The values above are *interpolation anchors*; the picker target is a
linear interpolation between adjacent anchors.  The actual BPM / energy
numbers are computed by mapping ``[0, 1]`` to a configurable
``(low_bpm, high_bpm)`` and ``(low_energy, high_energy)`` range.

Defaults (electronic-leaning): 95–128 BPM, 0.04–0.11 energy.

Example:
    >>> from autodj.mood_arc import current_arc_target, MoodArc
    >>> arc = MoodArc(start_time_s=0.0, duration_s=3 * 3600)
    >>> target = current_arc_target(arc, now_s=arc.duration_s * 0.5)
    >>> round(target.target_bpm, 1)
    122.7
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ArcAnchor:
    """A single anchor on the arc curve (progress, bpm_frac, energy_frac).

    All three values are in ``[0, 1]``.  ``progress`` is fraction of the
    arc duration; ``bpm_frac`` and ``energy_frac`` map onto the
    arc's ``(low, high)`` ranges via linear interpolation.
    """

    progress: float
    bpm_frac: float
    energy_frac: float


# Default warmup -> peak -> cool envelope.
DEFAULT_ANCHORS: tuple[ArcAnchor, ...] = (
    ArcAnchor(progress=0.00, bpm_frac=0.20, energy_frac=0.20),
    ArcAnchor(progress=0.30, bpm_frac=0.55, energy_frac=0.50),
    ArcAnchor(progress=0.50, bpm_frac=0.80, energy_frac=0.80),
    ArcAnchor(progress=0.75, bpm_frac=1.00, energy_frac=1.00),
    ArcAnchor(progress=0.90, bpm_frac=0.65, energy_frac=0.60),
    ArcAnchor(progress=1.00, bpm_frac=0.20, energy_frac=0.25),
)


@dataclass
class MoodArc:
    """A live mood arc anchored to a real-clock start time.

    Attributes:
        start_time_s: Monotonic seconds when the arc began.  Used so
            arcs survive across server restarts when persisted (the
            caller stores ``time.time()`` and rehydrates).
        duration_s: Total length of the arc in seconds.  After this
            elapses, the arc loops -- progress = (now - start) % duration.
        low_bpm: BPM at progress 0 / 1 (warmup / close).
        high_bpm: BPM at progress 0.75 (peak).
        low_energy: Energy at progress 0 / 1 (warmup / close).
        high_energy: Energy at progress 0.75 (peak).
        bpm_weight: How heavily BPM target affects the picker score.
            0.0 = ignore, 1.0 = sole criterion.  Default 0.3.
    """

    start_time_s: float
    duration_s: float = 3 * 3600.0
    low_bpm: float = 95.0
    high_bpm: float = 128.0
    low_energy: float = 0.04
    high_energy: float = 0.11
    bpm_weight: float = 0.3


@dataclass(frozen=True)
class ArcTarget:
    """Picker target derived from a :class:`MoodArc` at a given moment."""

    target_bpm: float
    target_energy: float
    bpm_weight: float
    progress: float


def _interpolate(anchors: tuple[ArcAnchor, ...], progress: float) -> tuple[float, float]:
    """Linear interp ``(bpm_frac, energy_frac)`` at *progress* in ``[0, 1]``."""
    progress = max(0.0, min(1.0, progress))
    # Find the bracketing anchors.
    for lo, hi in itertools.pairwise(anchors):
        if lo.progress <= progress <= hi.progress:
            span = hi.progress - lo.progress
            if span <= 0:
                return (lo.bpm_frac, lo.energy_frac)
            t = (progress - lo.progress) / span
            return (
                lo.bpm_frac + t * (hi.bpm_frac - lo.bpm_frac),
                lo.energy_frac + t * (hi.energy_frac - lo.energy_frac),
            )
    # Fall through (progress beyond last anchor) — clamp to last.
    last = anchors[-1]
    return (last.bpm_frac, last.energy_frac)


def current_arc_target(
    arc: MoodArc,
    now_s: float | None = None,
    anchors: tuple[ArcAnchor, ...] = DEFAULT_ANCHORS,
) -> ArcTarget:
    """Compute the active :class:`ArcTarget` for *arc* at the given moment.

    The returned ``progress`` value wraps modulo the arc duration so the
    arc loops after completing once -- gentle warmup, peak, cool, then
    repeat from warmup.  This matches the user expectation of "leave
    autodj running, music tracks the day" without needing a manual
    re-arm step.

    Args:
        arc: The :class:`MoodArc` controlling the envelope.
        now_s: Override clock time (seconds).  Defaults to ``time.time()``.
        anchors: Interpolation anchors.  Override to produce a custom
            shape (e.g. festival peak earlier, cooldown longer).

    Returns:
        An :class:`ArcTarget` for the picker to bias next-track scoring.
    """
    if now_s is None:
        now_s = time.time()
    elapsed = max(0.0, now_s - arc.start_time_s)
    duration = max(1.0, arc.duration_s)
    progress = (elapsed % duration) / duration
    bpm_frac, energy_frac = _interpolate(anchors, progress)
    return ArcTarget(
        target_bpm=arc.low_bpm + bpm_frac * (arc.high_bpm - arc.low_bpm),
        target_energy=arc.low_energy + energy_frac * (arc.high_energy - arc.low_energy),
        bpm_weight=max(0.0, min(1.0, arc.bpm_weight)),
        progress=progress,
    )


def make_default_arc(duration_hours: float = 3.0) -> MoodArc:
    """Convenience: build a :class:`MoodArc` starting now, *duration_hours* long."""
    return MoodArc(
        start_time_s=time.time(),
        duration_s=max(0.25, float(duration_hours)) * 3600.0,
    )
