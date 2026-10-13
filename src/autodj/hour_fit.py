"""Bounded choice among already eligible songs near a fixed time."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

FIT_CANDIDATES = 20

#: How early a song may hand off before a top of the hour or clockwheel
#: restart and still count as landing on it.  A hard event gets none: a
#: song that started just before it would be cut a few seconds in.
EARLY_SECONDS = 10.0


class Target(NamedTuple):
    """The fixed point the next handoffs aim for.

    Attributes:
        at: When it falls, in wall-clock seconds.
        hard: True for a hard schedule event, which cuts the song playing.
    """

    at: float
    hard: bool = False


def next_target(
    at: float,
    window_minutes: int,
    timezone: str,
    clock_boundary: float | None,
    events: Sequence[Any],
) -> Target | None:
    """Nearest hour, clock restart or hard event within the fitting window."""
    end = at + window_minutes * 60
    zone = ZoneInfo(timezone)
    local = datetime.fromtimestamp(at, zone)
    hour = local.replace(minute=0, second=0, microsecond=0).timestamp() + 3600
    # Timestamp arithmetic sees the repeated hour when clocks go back. A
    # half-hour clock change may land at :30, so advance to the next :00.
    for _ in range(61):
        wall = datetime.fromtimestamp(hour, zone)
        if wall.minute == 0 and wall.second == 0:
            break
        hour += 60
    targets = [Target(hour)]
    if clock_boundary is not None:
        targets.append(Target(clock_boundary))
    if events:
        from autodj.scheduler import next_run

        after = datetime.fromtimestamp(at, UTC)
        for event in events[:100]:
            if not event.enabled or event.timing != "hard":
                continue
            try:
                due = next_run(event, after, zone)
            except (ValueError, OverflowError, OSError):
                continue
            if due is not None:
                targets.append(Target(due.timestamp(), True))
    # At the same instant a hard event wins: it allows no early handoff.
    return min(
        (target for target in targets if at < target.at <= end),
        key=lambda target: (target.at, not target.hard),
        default=None,
    )


def fit_chooser[T](
    needed: float, seconds: Callable[[T], float], *, hard: bool = False
) -> Callable[[Sequence[T]], T]:
    """Return a choice of the candidate that best lands *needed* seconds away.

    *seconds* is how long a candidate plays before it hands off.  A song
    that hands off at or after the target scores how late it is.  One that
    hands off before it scores how well the songs still to come can fill
    the rest, taking the candidates' average as a typical song: leaving a
    few seconds that another whole song then overruns is no fit at all.
    Up to :data:`EARLY_SECONDS` before a top of the hour or restart counts
    as landing on it.  Ties keep the earlier candidate, so the picker's own
    order (least recently played first, or most similar) still decides.
    """
    early = 0.0 if hard else EARLY_SECONDS

    def choose(candidates: Sequence[T]) -> T:
        lengths = [seconds(candidate) for candidate in candidates]
        known = [length for length in lengths if length > 1]
        if not known:
            return candidates[0]
        typical = sum(known) / len(known)

        def score(index: int) -> float:
            length = lengths[index]
            if length <= 1:
                return float("inf")
            rest = needed - length
            if rest <= early:
                return abs(rest)
            return abs(rest - max(1, round(rest / typical)) * typical)

        return candidates[min(range(len(candidates)), key=score)]

    return choose
