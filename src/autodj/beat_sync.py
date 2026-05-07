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
- :func:`next_downbeat_at` — first downbeat ≥ a target time (used by
  the player to snap effect start to a downbeat).
- :func:`bar_seconds` — seconds per bar for a given BPM.
- :func:`lerp_bpm` — linear interpolation across the crossfade window
  so per-bar timing morphs from the outgoing track's tempo to the
  incoming track's tempo.
- :func:`key_to_hz` — chromatic key 0-11 → root frequency in Hz (A4 =
  440 reference, octave 4).  Mode does not affect the root.
- :func:`lerp_hz` — pitch interpolation in log space so the perceived
  glide is even.

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


def next_downbeat_at(
    downbeats: Sequence[float],
    t: float,
    *,
    epsilon: float = 1e-3,
) -> float | None:
    """Return the first downbeat ``>= t``.

    Args:
        downbeats: Sorted ascending downbeat timestamps in seconds.
        t: Target time in seconds.
        epsilon: Tolerance — a downbeat within ``t - epsilon`` counts as
            "now" so callers don't get a 1-bar shift on already-on-grid
            timings.

    Returns:
        First matching downbeat, or ``None`` when ``downbeats`` is empty
        or every entry is earlier than ``t``.
    """
    if not downbeats:
        return None
    target = t - epsilon
    for d in downbeats:
        if d >= target:
            return float(d)
    return None


def lerp_bpm(out_bpm: float, in_bpm: float, frac: float) -> float:
    """Linear blend from *out_bpm* at ``frac=0`` to *in_bpm* at ``frac=1``.

    Either BPM may be 0 (unknown).  In that case the *known* BPM is used
    for the entire window.  When neither is known, returns ``120.0`` —
    the long-standing AutoDJ fallback tempo.

    Args:
        out_bpm: Outgoing track's tempo.
        in_bpm: Incoming track's tempo.
        frac: 0-1 position across the crossfade.  Clamped.

    Returns:
        Blended BPM.
    """
    f = max(0.0, min(1.0, frac))
    if out_bpm > 0 and in_bpm > 0:
        return float(out_bpm) * (1.0 - f) + float(in_bpm) * f
    if out_bpm > 0:
        return float(out_bpm)
    if in_bpm > 0:
        return float(in_bpm)
    return 120.0


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


def lerp_hz(out_hz: float | None, in_hz: float | None, frac: float) -> float | None:
    """Logarithmic (perceptual) frequency blend.

    Linear blends in Hz sound front-loaded — the high-frequency side
    races past the low side.  Log-space lerp keeps the glide perceptually
    uniform.

    Args:
        out_hz: Outgoing root frequency, or ``None`` when unknown.
        in_hz: Incoming root frequency, or ``None`` when unknown.
        frac: 0-1 position across the crossfade.  Clamped.

    Returns:
        Blended Hz.  When one side is ``None``, the known side is used.
        When both are ``None``, returns ``None``.
    """
    f = max(0.0, min(1.0, frac))
    if out_hz and in_hz:
        import math

        return math.exp(math.log(out_hz) * (1.0 - f) + math.log(in_hz) * f)
    if out_hz:
        return float(out_hz)
    if in_hz:
        return float(in_hz)
    return None


# ---------------------------------------------------------------------------
# Per-effect bar-length defaults
# ---------------------------------------------------------------------------
# Each entry maps an effect name to (bars, snap_to_downbeat).
#   bars: integer bar count.  When the effect would otherwise be sized via
#         `outro_len * fraction`, this is rounded to the nearest integer
#         number of bars at the blended tempo and used directly.
#   snap_to_downbeat: when True, the FX scheduler shifts its start time
#         forward to the next outgoing downbeat (≤ 1 bar of latency).  Pure
#         envelope FX where the start phase doesn't matter (ambient pad
#         reverb_tail, lowpass_sweep) get False so they fire immediately.
#
# Surfaced to the browser via /api/state so app.js doesn't have to maintain
# a duplicate table.
FX_BAR_TABLE: dict[str, tuple[int, bool]] = {
    # Rhythmic FX — bars matter, snap matters
    "beat_repeat": (4, True),
    "gate_stutter": (4, True),
    "stutter_build": (4, True),
    "sidechain_pump": (8, True),
    "halftime": (4, True),
    "transformer": (2, True),
    "echo_out": (4, True),
    "dub_delay": (8, True),
    "scratch": (2, True),
    # Risers / drops — bar-snapped, downbeat-aligned
    "noise_riser": (4, True),
    "noise_drop": (4, True),
    "reverse_reverb": (4, True),
    "air_horn": (2, True),
    "dub_siren": (4, True),
    # Envelope sweeps — bar-rounded but downbeat-snap optional
    "highpass_sweep": (4, False),
    "lowpass_sweep": (4, False),
    "cross_eq_swap": (4, False),
    "submerge": (4, False),
    "telephone": (4, False),
    "chorus": (4, False),
    "phaser": (4, False),
    "flanger": (4, False),
    "wow_flutter": (4, False),
    "vinyl_wow": (4, False),
    "ring_modulator": (4, False),
    "bitcrusher": (4, False),
    # Pitch / spin FX — quick events, snap matters
    "pitch_swell": (2, True),
    "pitch_fall": (2, True),
    "tape_stop": (2, True),
    "backspin": (2, True),
    "forward_spin": (2, True),
    "vinyl_rewind": (4, True),
    "freeze": (2, True),
    "glitch": (4, True),
    "reverb_tail": (4, False),
}


def fx_bars(effect: str) -> int:
    """Return the default bar count for *effect*, or 4 when unknown."""
    entry = FX_BAR_TABLE.get(effect)
    return entry[0] if entry else 4


def fx_snaps_to_downbeat(effect: str) -> bool:
    """Return True when *effect* should snap its start to a downbeat."""
    entry = FX_BAR_TABLE.get(effect)
    return bool(entry[1]) if entry else False


__all__ = [
    "FX_BAR_TABLE",
    "bar_seconds",
    "extract_downbeats",
    "fx_bars",
    "fx_snaps_to_downbeat",
    "key_to_hz",
    "lerp_bpm",
    "lerp_hz",
    "next_downbeat_at",
    "synthesize_downbeats",
]
