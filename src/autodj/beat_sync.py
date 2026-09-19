"""Beat- and key-synchronisation helpers shared by the web + CLI players.

The browser player (``static/app.js``) and the server-side CLI player both
need to align transition effects with the beat grid + root note of the
tracks playing on each side of a crossfade.  This module owns the small
amount of music-theory math used by both:

- :func:`extract_downbeats` — subsample a dense beat grid to one
  timestamp per bar (every 4 beats by default).
- :func:`synthesize_downbeats` — when a track has no detected beat grid
  (silent intro / odd-time / detection failure), generate a synthetic
  grid from BPM and a phase anchor (typically ``outro_start_s``).  The
  fallback is good enough for 4-on-floor material; degrades gracefully
  on swung / free-time content where it's no worse than the legacy
  fixed-second scheduling.
- :func:`bar_seconds` — seconds per bar for a given BPM.
- :func:`key_to_hz` — chromatic key 0-11 → root frequency in Hz (A4 =
  440 reference, octave 4).  Mode does not affect the root.

Module is dependency-free so it imports cleanly under both the indexer
process (where librosa is loaded) and the headless CLI / web server
(where it is not strictly required).
"""

from __future__ import annotations

from collections.abc import Sequence

# A4 = 440 Hz, octave 4.  C4 (chromatic 0) = 440 / 2^(9/12).
_A4_HZ = 440.0
_C4_HZ = _A4_HZ * (2.0 ** (-9.0 / 12.0))


def extract_downbeats(
    beats: Sequence[float],
    beats_per_bar: int = 4,
) -> list[float]:
    """Return beats subsampled to one timestamp per bar.

    The dense beat grid produced by :func:`autodj.dj_meta.detect_beat_grid`
    has one entry per beat with no downbeat marker.  Heuristic: assume the
    first detected beat is on a downbeat (true for the vast majority of
    pop / dance / electronic material), then take every ``beats_per_bar``-th
    beat.

    Args:
        beats: Sorted ascending beat-onset timestamps in seconds.
        beats_per_bar: Beats per bar; 4 covers most music.

    Returns:
        Subsampled list of downbeat timestamps.  Empty when input empty.
    """
    if not beats or beats_per_bar <= 0:
        return []
    return [float(t) for t in beats[::beats_per_bar]]


def bar_seconds(bpm: float, beats_per_bar: int = 4) -> float:
    """Return seconds per bar for *bpm*.

    Args:
        bpm: Tempo in beats-per-minute.
        beats_per_bar: Time signature numerator (default 4/4).

    Returns:
        Seconds per bar.  Returns ``2.0`` (a safe musical default) when
        ``bpm`` is non-positive — keeps callers from dividing by zero.
    """
    if bpm <= 0 or beats_per_bar <= 0:
        return 2.0
    return 60.0 * beats_per_bar / float(bpm)


def synthesize_downbeats(
    bpm: float,
    length_s: float,
    *,
    anchor_s: float = 0.0,
    beats_per_bar: int = 4,
) -> list[float]:
    """Generate a synthetic downbeat grid from BPM + a phase anchor.

    Used when a track has no detected beat grid.  The grid is phase-locked
    to ``anchor_s`` (commonly ``outro_start_s`` so the grid lines up with
    the outro the listener actually hears) and extends both directions
    until ``length_s``.

    Args:
        bpm: Tempo in beats-per-minute.  Returns ``[]`` when non-positive.
        length_s: Track length in seconds.
        anchor_s: Phase anchor — one downbeat is placed at this time.
            Earlier downbeats are extrapolated back toward 0.
        beats_per_bar: Time signature numerator.

    Returns:
        Sorted ascending downbeat timestamps.
    """
    if bpm <= 0 or length_s <= 0 or beats_per_bar <= 0:
        return []
    bs = bar_seconds(bpm, beats_per_bar)
    if bs <= 0:
        return []
    # Walk back from anchor toward 0 to pick the earliest in-range downbeat
    first = anchor_s
    while first - bs >= 0:
        first -= bs
    out: list[float] = []
    t = first
    while t < length_s:
        if t >= 0:
            out.append(round(t, 6))
        t += bs
    return out


def key_to_hz(key: int, octave: int = 4) -> float | None:
    """Return the root-note frequency in Hz for chromatic *key* in *octave*.

    Args:
        key: Chromatic key 0-11 (C=0, C#=1, ..., B=11).  ``-1`` (or any
            out-of-range value) returns ``None``.
        octave: Octave number (scientific pitch notation).  Default 4
            (middle C octave).  Higher octaves are 2× per step.

    Returns:
        Root frequency in Hz, or ``None`` when ``key`` is unknown.
    """
    if not (0 <= key <= 11):
        return None
    return _C4_HZ * (2.0 ** ((key + 12 * (octave - 4)) / 12.0))


__all__ = [
    "bar_seconds",
    "extract_downbeats",
    "key_to_hz",
    "synthesize_downbeats",
]
