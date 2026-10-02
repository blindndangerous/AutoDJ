"""Where the singing is, from synced (LRC) lyrics, for the server mix.

Two mix features need to know when a track's vocals run:

- The vocal-clash guard (``[djmix] vocal_guard``) shortens or moves a
  crossfade so the outgoing track's last sung line is over before the
  incoming track starts singing (:func:`guard_fade`).
- Liner talk-ups (``[playback] liners_talk_up``) end a liner just before
  the incoming track's first sung line.

A lyric line only gives its start time.  Its end is taken as the next
line's start, but no more than :data:`LINE_CAP_S` after its own start,
so a line followed by a long instrumental break does not count as sung
all the way through it.  Lines with no text (instrumental markers) are
not sung.

Reading lyrics can mean a tag read over the network, so
:class:`VocalCache` keeps the parsed spans of recent tracks: each track is
read once however many times the mix asks.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence

from autodj.audio_meta import LyricLine

#: Longest a single sung line is assumed to last, in seconds.
LINE_CAP_S = 5.0
#: Shortest crossfade the vocal guard leaves, in seconds.
MIN_GUARDED_FADE_S = 1.0
#: Furthest the vocal guard moves a crossfade's start, in seconds.
MAX_GUARD_MOVE_S = 15.0
#: Step between the start positions the guard tries when there is no beat grid.
_FREE_MOVE_STEP_S = 0.1

Span = tuple[float, float]


def sung_spans(lines: Sequence[LyricLine]) -> tuple[Span, ...]:
    """Return ``(start, end)`` seconds of every sung line, in time order.

    Args:
        lines: Timed lyric lines, sorted by time (as
            :func:`~autodj.audio_meta.parse_lrc` returns them).

    Returns:
        One span per line with text.  Empty when nothing is sung.
    """
    times = [line.time_s for line in lines]
    spans: list[Span] = []
    for index, line in enumerate(lines):
        if not line.text.strip():
            continue
        start = line.time_s
        following = (t for t in times[index + 1 :] if t > start)
        end = min(next(following, math.inf), start + LINE_CAP_S)
        spans.append((start, end))
    return tuple(spans)


def first_vocal_s(spans: Sequence[Span], after: float = 0.0) -> float | None:
    """Return when the first sung line at or after *after* starts.

    Args:
        spans: Sung spans (:func:`sung_spans`).
        after: Ignore lines that start before this, in seconds: lines in
            leading silence the mix skips are never heard.

    Returns:
        The start in seconds, or ``None`` when no such line is sung.
    """
    return next((start for start, _end in spans if start >= after), None)


def _longest_clear_fade(spans: Sequence[Span], start: float, lead: float, limit: float) -> float:
    """Longest fade from *start*, at most *limit*, with no vocal clash.

    The incoming track starts singing *lead* seconds into the fade.  The
    outgoing track is heard until the fade ends, so a line of it clashes
    when it starts before the fade ends and is still going when the
    incoming vocal starts.

    Returns:
        The fade length in seconds; *lead* when an outgoing line is sung
        across the incoming vocal's start (the fade must be over by then).
    """
    if lead >= limit:
        return limit
    vocal = start + lead
    next_line = math.inf
    for line_start, line_end in spans:
        if line_start < vocal < line_end:
            return lead
        if line_start >= vocal:
            next_line = line_start
            break
    return min(limit, next_line - start)


def _move_offsets(step: float, limit: float) -> list[float]:
    """Offsets to try for the fade start, nearest first, later before earlier."""
    count = int(limit / step)
    offsets: list[float] = []
    for k in range(1, count + 1):
        offsets.extend((k * step, -k * step))
    return offsets


def guard_fade(
    out_spans: Sequence[Span],
    in_first_s: float | None,
    *,
    start_s: float,
    fade_s: float,
    entry_s: float,
    ratio: float = 1.0,
    earliest_s: float = 0.0,
    end_s: float = math.inf,
    beat_s: float | None = None,
) -> tuple[float, float] | None:
    """Shorten or move a crossfade so the two tracks' vocals do not overlap.

    The incoming track enters at *entry_s* of its own file at the moment
    the fade starts, stretched by *ratio* (output over input duration),
    so its first sung line is heard ``(in_first_s - entry_s) * ratio``
    seconds into the fade.

    The fade is first shortened, keeping its start (so a beatmatched
    fade stays phase locked): it ends before the incoming vocal, or
    before the outgoing track's next line when that comes later.  When
    that would leave less than :data:`MIN_GUARDED_FADE_S`, the start is
    moved up to :data:`MAX_GUARD_MOVE_S` either way, nearest first, in
    whole beats when *beat_s* is given.  The fade is never lengthened.

    Args:
        out_spans: The outgoing track's sung spans (:func:`sung_spans`),
            in its own file's seconds.
        in_first_s: When the incoming track's first sung line starts, in
            its own file's seconds; ``None`` when unknown.
        start_s: Where the fade starts in the outgoing track.
        fade_s: The fade length.
        entry_s: Where the incoming track enters, in its own file.
        ratio: The incoming track's stretch during the fade.
        earliest_s: Earliest allowed fade start (already played before it).
        end_s: The outgoing track's length; the fade must end by then.
        beat_s: The outgoing track's beat period, to move in whole beats.

    Returns:
        The new ``(start_s, fade_s)``, or ``None`` to keep the fade as it
        is: no clash, nothing to go on, or no arrangement avoids it.
    """
    if in_first_s is None or not out_spans or fade_s < MIN_GUARDED_FADE_S:
        return None
    lead = max(0.0, (in_first_s - entry_s) * ratio)
    clear = _longest_clear_fade(out_spans, start_s, lead, fade_s)
    if clear >= fade_s:
        return None
    if clear >= MIN_GUARDED_FADE_S:
        return start_s, clear
    step = beat_s if beat_s and beat_s > 0 else _FREE_MOVE_STEP_S
    for offset in _move_offsets(step, MAX_GUARD_MOVE_S):
        start = start_s + offset
        limit = min(fade_s, end_s - start)
        if start < earliest_s or limit < MIN_GUARDED_FADE_S:
            continue
        clear = _longest_clear_fade(out_spans, start, lead, limit)
        if clear >= MIN_GUARDED_FADE_S:
            return start, clear
    return None


class VocalCache:
    """Sung spans of recently mixed tracks, each read once.

    Thread-safe.  The loader runs outside the lock, so two threads asking
    for the same new track at once may both read it; the result is the
    same either way.

    Args:
        loader: Returns a track's timed lyric lines (empty when it has
            none or they cannot be read).
        size: Tracks remembered.
    """

    def __init__(self, loader: Callable[[str], Sequence[LyricLine]], size: int = 64) -> None:
        """Create an empty cache around *loader*."""
        self._loader = loader
        self._size = size
        self._spans: OrderedDict[str, tuple[Span, ...]] = OrderedDict()
        self._lock = threading.Lock()

    def spans(self, path: str) -> tuple[Span, ...]:
        """Return the sung spans of the track at *path*."""
        with self._lock:
            if path in self._spans:
                self._spans.move_to_end(path)
                return self._spans[path]
        spans = sung_spans(self._loader(path))
        with self._lock:
            self._spans[path] = spans
            while len(self._spans) > self._size:
                self._spans.popitem(last=False)
        return spans


__all__ = [
    "LINE_CAP_S",
    "MAX_GUARD_MOVE_S",
    "MIN_GUARDED_FADE_S",
    "VocalCache",
    "first_vocal_s",
    "guard_fade",
    "sung_spans",
]
