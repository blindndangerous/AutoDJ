"""Transition-effect library applied at the moment of crossfade.

A "transition effect" is an audio treatment layered ONTO the standard
crossfade so the moment two tracks meet sounds intentional rather than
just fading.  Pro DJs use these to disguise tempo / key clashes and to
add energy lifts.  An effect treats the outgoing tail, the incoming
head, or both (typically 1–8 bars), or synthesises a layer (noise, horn,
siren) to mix over them.

Each effect is one row in the tables near the end of this module (a
function plus the parameters that make it that effect), except glitch,
highpass_sweep and cross_eq_swap, which :func:`apply_transition` handles
in branches of their own.  Effects that
differ only in their settings share one function:

- :func:`_delay` — feedback delays: echo_out, flanger, dub_delay
- :func:`_spin` — variable-speed reads: tape_stop, pitch_swell,
  pitch_fall, forward_spin, vinyl_rewind, backspin
- :func:`_sweep` — filter sweeps: lowpass_sweep, highpass_sweep and the
  sweep inside submerge; :func:`_noise_sweep` builds the noise_riser and
  noise_drop layers on it
- :func:`_gate` — rhythmic gates: gate_stutter, transformer

:class:`TransitionFx` and :func:`apply_transition` are the single
dispatch surface.  All effects are stateless — buffers in, buffers
out — so they can be swapped per crossfade with no setup cost.

:func:`apply_transition` also holds every effect to the level of the
music it works on: a treated tail or head never peaks above the audio
it was made from, and a synthesised layer (noise, horn, siren) is set
against the peak of the two tracks it is mixed over.  That audio has
already had ReplayGain applied, so the effects follow it too.

None of the effects raise on short / silent buffers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import StrEnum
from functools import partial
from typing import cast

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Effect catalogue
# ---------------------------------------------------------------------------


class TransitionFx(StrEnum):
    """Selectable transition effects."""

    NONE = "none"
    ECHO_OUT = "echo_out"
    REVERB_TAIL = "reverb_tail"
    HIGHPASS_SWEEP = "highpass_sweep"  # filter-IN on incoming (was: highpass_riser)
    LOWPASS_SWEEP = "lowpass_sweep"  # filter-OUT on outgoing
    TAPE_STOP = "tape_stop"
    GATE_STUTTER = "gate_stutter"
    NOISE_RISER = "noise_riser"
    BACKSPIN = "backspin"
    FORWARD_SPIN = "forward_spin"  # vinyl push-forward (opposite of backspin)
    CROSS_EQ_SWAP = "cross_eq_swap"
    BITCRUSHER = "bitcrusher"  # lo-fi bit-depth crush on outgoing
    FLANGER = "flanger"  # short LFO-modulated delay on outgoing
    PITCH_SWELL = "pitch_swell"  # pitch ramp UP on outgoing (vinyl rewind reverse)
    PITCH_FALL = "pitch_fall"  # pitch ramp DOWN on outgoing (mirror of swell)
    TELEPHONE = "telephone"  # narrow band-pass — sounds like a phone call
    NOISE_DROP = "noise_drop"  # noise crashes from bright to dark (opposite of riser)
    CHORUS = "chorus"  # multi-voice detuned chorus
    SUBMERGE = "submerge"  # heavy lowpass + reverb (underwater)
    VINYL_WOW = "vinyl_wow"  # pitch wobble (drunk turntable)
    FREEZE = "freeze"  # capture last slice + loop with fade-out
    GLITCH = "glitch"  # random buffer slicing + reorder
    SCRATCH = "scratch"  # rapid back-and-forth slice (turntablist sweep)
    BEAT_REPEAT = "beat_repeat"  # capture short slice, retrigger N times
    SIDECHAIN_PUMP = "sidechain_pump"  # rhythmic 4-on-the-floor amplitude pump
    REVERSE_REVERB = "reverse_reverb"  # reverse'd reverb tail swelling INTO the cut
    AIR_HORN = "air_horn"  # square-wave horn rising 220 -> 880 Hz over the music
    VINYL_REWIND = "vinyl_rewind"  # slow musical reverse + pitch drop (vs harsh backspin)
    TRANSFORMER = "transformer"  # tempo-cut DJ-fader transformer pattern
    DUB_SIREN = "dub_siren"  # sine-wave reggae siren (smoother than air_horn)
    STUTTER_BUILD = "stutter_build"  # accelerating gate frequency 4 Hz → 32 Hz
    WOW_FLUTTER = "wow_flutter"  # combined pitch wobble + amplitude tremolo
    PHASER = "phaser"  # 4-stage allpass cascade (sweepy notch w/o flanger comb)
    RING_MODULATOR = "ring_modulator"  # signal × sine carrier (clangy bell tone)
    DUB_DELAY = "dub_delay"  # long lowpass-feedback delay (vs short echo_out 1/4)
    HALFTIME = "halftime"  # tempo halve, pitch preserved (vs pitch_fall pitch-down)
    RANDOM = "random"  # pick uniformly at random per crossfade
    ROTATE = "rotate"  # cycle through the catalogue in order


TRANSITION_EFFECT_NAMES: frozenset[str] = frozenset(fx.value for fx in TransitionFx)
"""Every selectable effect name, including ``none``, ``random`` and ``rotate``.

Single source of truth for the web-UI allowlist and the
persisted web-state validator, so a new enum member cannot be offered by one
surface and rejected by another.
"""


# Catalogue used by RANDOM / ROTATE — every effect except NONE and the meta-modes.
_REAL_EFFECTS: list[TransitionFx] = [
    fx
    for fx in TransitionFx
    if fx not in (TransitionFx.NONE, TransitionFx.RANDOM, TransitionFx.ROTATE)
]


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------


def _ramp(n: int) -> np.ndarray:
    """*n* float32 steps from 0.0 to 1.0."""
    return np.linspace(0.0, 1.0, n, dtype=np.float32)


def _mix(dry: np.ndarray, treated: np.ndarray, wet: float) -> np.ndarray:
    """Blend *treated* over *dry* at *wet* (0 = dry only), clipped to ±1.0."""
    mixed = (1.0 - wet) * dry + wet * treated
    np.clip(mixed, -1.0, 1.0, out=mixed)
    return mixed.astype(np.float32)


def _resample_by_rate(
    src: np.ndarray, rate: np.ndarray, *, interpolate: bool = False
) -> np.ndarray:
    """Read *src* at a variable *rate*, stretched to span the whole buffer.

    ``rate`` is a per-output-sample playback speed.  Its cumulative sum is
    the read position, normalised so the last sample lands on the final
    frame of *src* — otherwise a curve that averages below 1.0 would stop
    short of the end.  *interpolate* reads between samples linearly
    instead of taking the one before.

    Returns:
        Float32 array with one sample per entry of *rate*.
    """
    pos = np.cumsum(rate)
    if pos[-1] > 0:
        pos = pos * ((len(src) - 1) / pos[-1])
    idx = pos.astype(np.int32)
    np.clip(idx, 0, len(src) - 1, out=idx)
    if not interpolate:
        return src[idx].astype(np.float32)
    frac = (pos - idx).astype(np.float32)
    after = np.minimum(idx + 1, len(src) - 1)
    return (src[idx] * (1.0 - frac) + src[after] * frac).astype(np.float32)


def _edge_fades(grain: np.ndarray, sample_rate: int) -> None:
    """Fade *grain* in and out over up to 5 ms, in place, so its edges don't click."""
    seam = min(len(grain) // 16, int(0.005 * sample_rate))
    if seam > 0:
        grain[:seam] *= np.linspace(0.0, 1.0, seam, dtype=np.float32)
        grain[-seam:] *= np.linspace(1.0, 0.0, seam, dtype=np.float32)


def _butter(kind: str, hz: float, sample_rate: int) -> np.ndarray:
    """Fourth-order Butterworth *kind* ("low" / "high") at *hz*, as SOS."""
    from scipy.signal import butter

    return cast(
        np.ndarray,
        butter(4, max(1e-4, min(0.99, hz / (sample_rate / 2.0))), btype=kind, output="sos"),
    )


# ---------------------------------------------------------------------------
# Effect families
# ---------------------------------------------------------------------------


def _delay(
    tail: np.ndarray,
    sample_rate: int,
    *,
    delay_ms: float,
    feedback: float,
    wet: float,
    sweep_hz: float | None = None,
    damping_hz: float | None = None,
    direct: bool = False,
) -> np.ndarray:
    """Feedback delay: echo_out, flanger and dub_delay.

    The delay line's output feeds back into it at *feedback*.  With
    *sweep_hz* an LFO sweeps the delay between one sample and *delay_ms*
    (the flanger's comb); with *damping_hz* a one-pole lowpass darkens
    every repeat (dub delay).  With *direct* the dry audio enters the loop
    undelayed, so the wet signal is the audio plus its repeats; otherwise
    it is the repeats alone and silent until the first one.
    """
    n = len(tail)
    if n == 0:
        return tail
    longest = int((delay_ms / 1000.0) * sample_rate)
    if sweep_hz is None:
        delays = [max(1, longest)] * n
    else:
        lfo = 0.5 * (1 - np.cos(2 * np.pi * sweep_hz * np.arange(n) / sample_rate))
        delays = ((lfo * (max(2, longest) - 1)).astype(np.int32) + 1).tolist()
    alpha = None
    if damping_hz is not None:
        rc = 1.0 / (2 * np.pi * damping_hz)
        dt = 1.0 / sample_rate
        alpha = np.float32(dt / (rc + dt))
    dry = tail.astype(np.float32, copy=True)
    out = dry.copy() if direct else np.zeros_like(dry)
    lowpassed = np.float32(0.0)
    for i, d in enumerate(delays):
        if i < d:
            continue
        fed = out[i - d]
        if alpha is not None:
            lowpassed = lowpassed + alpha * (fed - lowpassed)
            fed = lowpassed
        out[i] = (dry[i] if direct else dry[i - d]) + feedback * fed
    return _mix(dry, out, wet)


def _spin(
    tail: np.ndarray,
    sample_rate: int,
    *,
    speed: Callable[[int], np.ndarray],
    reverse: bool = False,
    lead_in: int = 0,
    fade_s: float = 0.0,
    fade_share: int = 1,
    shortest: int = 1,
) -> np.ndarray:
    """Read *tail* at a changing speed: tape stop, pitch ramps and the spins.

    ``speed(n)`` gives the shape of the playback speed for each of the *n*
    samples read, with pitch following speed.  The read is scaled to span
    the source (see :func:`_resample_by_rate`), so only the ratios between
    speeds hold, not the absolute values.  With *lead_in* k the first 1/k of
    *tail* plays untouched and the rest is read from that stretch (a
    backspin throws back what was just heard); otherwise the whole tail
    is read.  *reverse* reads it backwards.  The last *fade_s* seconds,
    at most 1/*fade_share* of the read, fade to silence.  Buffers shorter
    than *shortest* come back unchanged.
    """
    n = len(tail)
    if n < shortest:
        return tail
    kept = n // lead_in if lead_in else 0
    src = tail[:kept] if kept else tail
    if reverse:
        src = src[::-1]
    out = _resample_by_rate(src, speed(n - kept))
    fade = min(int(fade_s * sample_rate), (n - kept) // fade_share)
    if fade > 0:
        out[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
    if not kept:
        return out
    return np.concatenate([tail[:kept], out]).astype(np.float32)


def _sweep(
    audio: np.ndarray,
    sample_rate: int,
    *,
    kind: str,
    start_hz: float | None,
    end_hz: float,
) -> np.ndarray:
    """Filter sweep: a *kind* ("lowpass" / "highpass") cutoff gliding from
    *start_hz* (None = Nyquist, full range) to *end_hz* over the buffer.
    """
    from autodj.player import apply_filter_sweep

    return apply_filter_sweep(
        audio,
        sample_rate,
        start_hz=sample_rate / 2.0 if start_hz is None else start_hz,
        end_hz=end_hz,
        filter_type=kind,
    )


def _gate(
    tail: np.ndarray,
    sample_rate: int,
    *,
    rate_hz: float,
    pattern: tuple[int, ...],
    duty: float,
    max_fade: int,
) -> np.ndarray:
    """Rhythmic gate: gate_stutter and transformer.

    The tail is cut into cycles of 1/*rate_hz* seconds.  A cycle whose
    step in *pattern* (repeated) is 1 opens for its first *duty* share,
    fading in over up to *max_fade* samples so the edge doesn't click;
    the rest is silent.
    """
    cycle = max(2, int(sample_rate / rate_hz))
    opened = max(1, int(cycle * duty))
    fade_in = np.linspace(0.0, 1.0, min(max_fade, opened // 4), dtype=np.float32)
    out = np.zeros_like(tail, dtype=np.float32)
    for i, start in enumerate(range(0, len(tail), cycle)):
        if pattern[i % len(pattern)]:
            block = out[start : start + opened]
            block[:] = tail[start : start + opened]
            block[: len(fade_in)] *= fade_in[: len(block)]
    return out


def _noise_sweep(
    n_samples: int,
    sample_rate: int,
    seed: int | None,
    *,
    start_hz: float,
    end_hz: float,
    swell: tuple[float, float],
) -> np.ndarray:
    """Synthesised noise layer: noise_riser and noise_drop.

    White noise through a lowpass swept from *start_hz* to *end_hz*, its
    amplitude ramping linearly across *swell*.  Reproducible with *seed*.
    """
    if n_samples <= 0:
        return np.zeros(0, dtype=np.float32)
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(n_samples).astype(np.float32) * 0.5
    swept = _sweep(noise, sample_rate, kind="lowpass", start_hz=start_hz, end_hz=end_hz)
    env = np.linspace(*swell, n_samples, dtype=np.float32)
    return (swept * env).astype(np.float32)


# ---------------------------------------------------------------------------
# Single effects
# ---------------------------------------------------------------------------


def _reverb(tail: np.ndarray, sample_rate: int, *, wet: float) -> np.ndarray:
    """Schroeder reverb, a small room of about 1 s: reverb_tail, and submerge's wash.

    Four parallel combs, then two serial allpasses to break up their
    metallic ring.  Pure numpy.
    """
    if len(tail) == 0:
        return tail

    # Comb filter delays (samples) at 44.1 kHz, scaled to actual SR.
    base_sr = 44100.0
    comb_delays = [int(d * sample_rate / base_sr) for d in (1116, 1188, 1277, 1356)]
    comb_gains = [0.84, 0.81, 0.78, 0.75]

    wet_sum = np.zeros_like(tail, dtype=np.float32)
    for d, g in zip(comb_delays, comb_gains, strict=True):
        if d <= 0 or d >= len(tail):
            continue
        buf = np.zeros_like(tail, dtype=np.float32)
        for i in range(len(tail)):
            buf[i] = tail[i] + (g * buf[i - d] if i >= d else 0.0)
        wet_sum += buf
    wet_sum /= len(comb_delays)

    allpass_delays = [int(d * sample_rate / base_sr) for d in (556, 441)]
    allpass_gain = 0.5
    for d in allpass_delays:
        if d <= 0 or d >= len(wet_sum):
            continue
        out = np.zeros_like(wet_sum)
        for i in range(len(wet_sum)):
            delayed = wet_sum[i - d] if i >= d else 0.0
            out[i] = (
                -allpass_gain * wet_sum[i]
                + delayed
                + allpass_gain * (out[i - d] if i >= d else 0.0)
            )
        wet_sum = out

    return _mix(tail, wet_sum, wet)


def _submerge(tail: np.ndarray, sample_rate: int, *, floor_hz: float, wet: float) -> np.ndarray:
    """Underwater wash: a lowpass closing down to *floor_hz*, then the reverb."""
    swept = _sweep(tail, sample_rate, kind="lowpass", start_hz=None, end_hz=floor_hz)
    return _reverb(swept, sample_rate, wet=wet)


def _bitcrusher(
    tail: np.ndarray, sample_rate: int, *, start_bits: int, end_bits: int
) -> np.ndarray:
    """Bit depth falling from *start_bits* to *end_bits* — a lo-fi breakdown."""
    n = len(tail)
    if n == 0:
        return tail
    depths = np.linspace(start_bits, end_bits, n).astype(np.int32)
    levels = (1 << (depths - 1)).astype(np.float32)  # 2^(bits-1)
    out = np.round(tail * levels) / np.maximum(levels, 1.0)
    np.clip(out, -1.0, 1.0, out=out)
    return out.astype(np.float32)


def _telephone(tail: np.ndarray, sample_rate: int, *, low_hz: float, high_hz: float) -> np.ndarray:
    """Band-pass from *low_hz* to *high_hz*: the tail as heard down a phone line."""
    if len(tail) == 0:
        return tail
    from scipy.signal import sosfilt

    highpassed = cast(np.ndarray, sosfilt(_butter("high", low_hz, sample_rate), tail))
    return cast(np.ndarray, sosfilt(_butter("low", high_hz, sample_rate), highpassed)).astype(
        np.float32
    )


def _cross_eq_swap(
    tail: np.ndarray, head: np.ndarray, sample_rate: int, *, crossover_hz: float
) -> tuple[np.ndarray, np.ndarray]:
    """Mirror EQ-duck — outgoing keeps its highs while incoming brings the bass.

    Both buffers split at *crossover_hz*.  The tail keeps only its treble;
    the head keeps its bass and has its treble brought back in over the
    buffer, so the two hand the bass over instead of clashing in the
    crossfade.
    """
    from scipy.signal import sosfilt

    if len(tail) == 0 or len(head) == 0:
        return tail, head
    hp = _butter("high", crossover_hz, sample_rate)
    lp = _butter("low", crossover_hz, sample_rate)
    tail_treble = cast(np.ndarray, sosfilt(hp, tail)).astype(np.float32)
    head_bass = cast(np.ndarray, sosfilt(lp, head)).astype(np.float32)
    head_treble = cast(np.ndarray, sosfilt(hp, head)).astype(np.float32)
    bring_in = np.linspace(0.0, 1.0, len(head), dtype=np.float32)
    return tail_treble, head_bass + head_treble * bring_in


def _chorus(tail: np.ndarray, sample_rate: int, *, wet: float) -> np.ndarray:
    """Three-voice chorus: 20/25/30 ms delays each swept by its own slow LFO."""
    n = len(tail)
    if n == 0:
        return tail
    rates = (0.4, 0.6, 0.8)
    base_delays_ms = (20.0, 25.0, 30.0)
    depths_ms = (3.0, 4.0, 5.0)

    t = np.arange(n) / sample_rate
    wet_sum = np.zeros(n, dtype=np.float32)
    for rate, base_ms, depth_ms in zip(rates, base_delays_ms, depths_ms, strict=True):
        delay = (base_ms + depth_ms * np.sin(2 * np.pi * rate * t)) / 1000.0
        idx = np.clip(np.arange(n) - (delay * sample_rate), 0, n - 1)
        i0 = idx.astype(np.int32)
        frac = (idx - i0).astype(np.float32)
        i1 = np.minimum(i0 + 1, n - 1)
        wet_sum += (tail[i0] * (1.0 - frac) + tail[i1] * frac).astype(np.float32)
    wet_sum /= len(rates)
    return _mix(tail, wet_sum, wet)


def _vinyl_wow(
    tail: np.ndarray,
    sample_rate: int,
    *,
    rate_hz: float,
    start_depth: float,
    end_depth: float,
) -> np.ndarray:
    """Pitch wobble at *rate_hz*, its depth growing from *start_depth* to *end_depth*."""
    n = len(tail)
    if n == 0:
        return tail
    t = np.arange(n) / sample_rate
    depth = np.linspace(start_depth, end_depth, n, dtype=np.float32)
    rate = (1.0 + depth * np.sin(2 * np.pi * rate_hz * t)).astype(np.float32)
    return _resample_by_rate(tail, rate, interpolate=True)


def _wow_flutter(
    tail: np.ndarray,
    sample_rate: int,
    *,
    wow_hz: float,
    flutter_hz: float,
    pitch_depth: float,
    amp_depth: float,
) -> np.ndarray:
    """Worn cassette: pitch wobble at *wow_hz* plus amplitude tremolo at *flutter_hz*."""
    n = len(tail)
    if n == 0:
        return tail
    t = np.arange(n, dtype=np.float32) / sample_rate
    rate = 1.0 + pitch_depth * np.sin(2 * np.pi * wow_hz * t)
    pitched = _resample_by_rate(tail, rate.astype(np.float32))
    tremolo = (1.0 - amp_depth) + amp_depth * np.sin(2 * np.pi * flutter_hz * t).astype(np.float32)
    out = (pitched * tremolo).astype(np.float32)
    np.clip(out, -1.0, 1.0, out=out)
    return out


def _freeze(tail: np.ndarray, sample_rate: int, *, grain_ms: float) -> np.ndarray:
    """Loop the last *grain_ms* of the tail, fading to silence across it.

    The grain's ends are crossfaded into each other so the loop point
    doesn't click.
    """
    n = len(tail)
    if n == 0:
        return tail
    grain_samples = min(max(1, int(grain_ms * sample_rate / 1000.0)), n)
    grain = tail[-grain_samples:].astype(np.float32, copy=True)

    seam = min(grain_samples // 8, int(0.005 * sample_rate))
    if seam > 0:
        fade = np.linspace(1.0, 0.0, seam, dtype=np.float32)
        head = grain[:seam].copy()
        grain[:seam] = grain[:seam] * (1.0 - fade) + grain[-seam:] * fade
        grain[-seam:] = grain[-seam:] * fade + head * (1.0 - fade)

    out = np.resize(grain, n)
    out *= np.linspace(1.0, 0.0, n, dtype=np.float32)
    return out


def _glitch(tail: np.ndarray, sample_rate: int, seed: int | None, *, slice_ms: float) -> np.ndarray:
    """Cut the tail into *slice_ms* slices and play them back in random order.

    Slices are drawn with replacement and faded at their edges.
    Reproducible with *seed*.
    """
    n = len(tail)
    if n == 0:
        return tail
    slice_samples = max(1, int(slice_ms * sample_rate / 1000.0))
    if slice_samples >= n:
        return tail.astype(np.float32, copy=True)

    rng = np.random.default_rng(seed)
    src_slices = n // slice_samples
    out = np.zeros(n, dtype=np.float32)
    for dst_start in range(0, n, slice_samples):
        src_start = int(rng.integers(0, src_slices)) * slice_samples
        src = tail[src_start : src_start + slice_samples].copy()
        _edge_fades(src, sample_rate)
        dst_end = min(dst_start + slice_samples, n)
        out[dst_start:dst_end] = src[: dst_end - dst_start]
    return out


def _scratch(tail: np.ndarray, sample_rate: int, *, passes: int, slice_ms: float) -> np.ndarray:
    """Turntablist scratch over the last *slice_ms* of the tail.

    The slice plays forward, then backward, *passes* times in all across
    the tail, each pass speeding up then slowing down (the "wikka").
    """
    n = len(tail)
    if n < 4:
        return tail
    slice_samples = min(max(2, int(slice_ms * sample_rate / 1000.0)), n)
    src = tail[-slice_samples:].astype(np.float32, copy=True)

    out = np.empty(n, dtype=np.float32)
    pass_len = n // passes
    for p in range(passes):
        start = p * pass_len
        end = start + pass_len if p < passes - 1 else n
        rate = (0.4 + 1.6 * np.sin(np.pi * _ramp(end - start))).astype(np.float32)
        out[start:end] = _resample_by_rate(src[::-1] if p % 2 else src, rate)
    return out


def _beat_repeat(
    tail: np.ndarray, sample_rate: int, *, slice_ms: float, repeats: int
) -> np.ndarray:
    """Loop roll: the last *slice_ms* of the tail retriggered *repeats* times across it."""
    n = len(tail)
    if n < 4:
        return tail
    slice_samples = min(max(2, int(slice_ms * sample_rate / 1000.0)), n // 2)
    src = tail[-slice_samples:].astype(np.float32, copy=True)
    _edge_fades(src, sample_rate)

    out = np.zeros(n, dtype=np.float32)
    chunk_len = n // repeats
    for i in range(repeats):
        start = i * chunk_len
        end = min(start + slice_samples, n)
        out[start:end] = src[: end - start]
    return out


def _sidechain_pump(tail: np.ndarray, sample_rate: int, *, bpm: float, depth: float) -> np.ndarray:
    """Four-on-the-floor pump: ducked by *depth* on every beat, recovering between."""
    n = len(tail)
    if n == 0:
        return tail
    period_samples = max(1, int(60.0 / bpm * sample_rate))
    t_in_beat = np.arange(n) % period_samples
    recovery = 1.0 - depth * np.exp(-3.0 * t_in_beat / period_samples)
    return (tail * recovery.astype(np.float32)).astype(np.float32)


def _reverse_reverb(tail: np.ndarray, sample_rate: int, *, reverb_seconds: float) -> np.ndarray:
    """A reversed reverb that swells up INTO the cut.

    The tail is convolved with a noise impulse whose envelope rises over
    *reverb_seconds*, normalised to unit energy so the wash comes out at
    about the level (RMS) of the audio going in.
    """
    n = len(tail)
    if n == 0:
        return tail
    rng = np.random.default_rng(seed=0xA17DA)
    ir_len = max(1, int(reverb_seconds * sample_rate))
    decay = np.linspace(0.0, 1.0, ir_len, dtype=np.float32) ** 2  # reversed env
    ir = rng.standard_normal(ir_len).astype(np.float32) * decay
    ir /= max(float(np.sqrt(np.sum(ir * ir))), 1e-12)
    convolved = np.convolve(tail, ir, mode="full")[:n].astype(np.float32)
    # Wet-heavy mix so the swell is what is heard.
    return _mix(tail, convolved, 0.6)


def _stutter_build(
    tail: np.ndarray, sample_rate: int, *, start_hz: float, end_hz: float
) -> np.ndarray:
    """Gate that speeds up from *start_hz* to *end_hz*: chops getting faster into the cut."""
    n = len(tail)
    if n == 0:
        return tail
    freq = start_hz + (end_hz - start_hz) * (np.arange(n) / max(1, n - 1))
    # Phase is the integral of frequency; the first half of each cycle is open.
    cell = (np.cumsum(freq / sample_rate) * 2).astype(np.int32)
    open_mask = (cell % 2 == 0).astype(np.float32)
    # Smooth the mask over 32 samples so the edges don't click.
    if n > 64:
        kernel = np.ones(32, dtype=np.float32) / 32.0
        open_mask = np.convolve(open_mask, kernel, mode="same").astype(np.float32)
    return (tail * open_mask).astype(np.float32)


def _phaser(
    tail: np.ndarray,
    sample_rate: int,
    *,
    stages: int,
    lfo_hz: float,
    depth: float,
    feedback: float,
) -> np.ndarray:
    """Allpass-cascade phaser: moving notches, without a flanger's comb.

    *stages* first-order allpasses whose break frequency an LFO sweeps
    within 200-1600 Hz (the full range at *depth* 1), fed back at *feedback* and mixed 50/50 with
    the dry tail.
    """
    n = len(tail)
    if n == 0:
        return tail
    min_hz, max_hz = 200.0, 1600.0
    t = np.arange(n, dtype=np.float32) / sample_rate
    lfo = 0.5 * (1.0 + depth * np.sin(2 * np.pi * lfo_hz * t))
    break_hz = min_hz + (max_hz - min_hz) * lfo
    # Allpass coefficient per sample: a = (1 - tan(πf/sr)) / (1 + tan(πf/sr))
    tan_arg = np.tan(np.pi * break_hz / sample_rate).astype(np.float32)
    a = ((1.0 - tan_arg) / (1.0 + tan_arg)).astype(np.float32)
    shifted = np.empty(n, dtype=np.float32)
    fb = np.float32(0.0)
    states = [np.float32(0.0)] * stages
    for i in range(n):
        x = tail[i] + feedback * fb
        for s in range(stages):
            y = -a[i] * x + states[s]
            states[s] = x + a[i] * y
            x = y
        fb = x
        shifted[i] = x
    return _mix(tail, shifted, 0.5)


def _ring_modulator(tail: np.ndarray, sample_rate: int, *, carrier_hz: float) -> np.ndarray:
    """Tail times a *carrier_hz* sine, 50/50 with the dry tail: clangy bell sidebands."""
    n = len(tail)
    if n == 0:
        return tail
    t = np.arange(n, dtype=np.float32) / sample_rate
    carrier = np.sin(2 * np.pi * carrier_hz * t).astype(np.float32)
    return _mix(tail, (tail * carrier).astype(np.float32), 0.5)


def _halftime(tail: np.ndarray, sample_rate: int) -> np.ndarray:
    """Half tempo at the same pitch (granular time-stretch), the trap pre-drop.

    Hann-windowed 50 ms grains are read at 50 % overlap and each written
    twice, spreading every grain over twice the time; the result is cut
    back to the tail's length.
    """
    n = len(tail)
    if n < 4:
        return tail
    grain_n = max(1, int(0.05 * sample_rate))
    hop_in = max(1, grain_n // 2)
    w = np.hanning(grain_n).astype(np.float32) if grain_n >= 2 else np.ones(1, dtype=np.float32)
    out = np.zeros(n * 2 + grain_n, dtype=np.float32)
    win_sum = np.zeros_like(out)
    out_pos = 0
    for read_pos in range(0, n - grain_n, hop_in):
        grain = tail[read_pos : read_pos + grain_n] * w
        for target in (out_pos, out_pos + hop_in):
            out[target : target + grain_n] += grain
            win_sum[target : target + grain_n] += w
        out_pos += 2 * hop_in
    # Normalise overlapping windows (avoid amplitude bumps at overlap)
    safe = win_sum > 1e-6
    out[safe] /= win_sum[safe]
    result = out[:n].astype(np.float32)
    np.clip(result, -1.0, 1.0, out=result)
    return result


def _air_horn(n_samples: int, sample_rate: int, _seed: int | None) -> np.ndarray:
    """Synth air horn rising 220 → 880 Hz, peaking at 1.0 (set against the music later)."""
    n = n_samples
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    freq = 220.0 + 660.0 * (np.arange(n) / max(1, n - 1))
    phase = np.cumsum(2 * np.pi * freq / sample_rate)
    # Square-ish horn via tanh of sine
    horn = np.tanh(2.5 * np.sin(phase)).astype(np.float32)
    # Envelope: fade in over 0.3 s, hold, sharp fade-out in last 0.1 s
    env = np.ones(n, dtype=np.float32)
    fade_in = min(int(0.3 * sample_rate), n // 4)
    fade_out_n = min(int(0.1 * sample_rate), n // 8)
    if fade_in > 0:
        env[:fade_in] = np.linspace(0.0, 1.0, fade_in, dtype=np.float32)
    if fade_out_n > 0:
        env[-fade_out_n:] = np.linspace(1.0, 0.0, fade_out_n, dtype=np.float32)
    horn *= env
    return horn


def _dub_siren(n_samples: int, sample_rate: int, _seed: int | None) -> np.ndarray:
    """Reggae sine siren, 440 → 1760 Hz with 5 Hz vibrato and a slow fade-in, peaking at 1.0."""
    n = n_samples
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    pos = np.arange(n, dtype=np.float32) / max(1, n - 1)
    base_freq = 440.0 * (4.0**pos)  # exponential 440 → 1760 Hz
    vibrato = 0.0087 * np.sin(2 * np.pi * 5.0 * np.arange(n) / sample_rate)  # ±15 cents
    freq = base_freq * (1.0 + vibrato.astype(np.float32))
    siren = np.sin(np.cumsum(2 * np.pi * freq / sample_rate)).astype(np.float32)
    siren *= np.minimum(1.0, 2.0 * pos).astype(np.float32)
    return siren


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


# Peak of each synthesised layer as a fraction of the music's peak.
# Mastered music peaks some 12 to 14 dB above its RMS.  Gaussian noise
# peaks about 13 dB above its own RMS, so noise at half the music's peak
# sits roughly 6 to 8 dB under it; the horn and siren are near-square and
# sine waves, as loud as their peaks, so they sit lower still.  Before,
# these layers were fixed amplitudes, as loud after ReplayGain had turned
# the music down 6 to 9 dB as before it.
_LAYER_LEVELS: dict[TransitionFx, float] = {
    TransitionFx.NOISE_RISER: 0.5,
    TransitionFx.NOISE_DROP: 0.5,
    TransitionFx.AIR_HORN: 0.15,
    TransitionFx.DUB_SIREN: 0.15,
}


def _peak(audio: np.ndarray) -> float:
    """Largest absolute sample of *audio*, 0.0 when it is empty."""
    return float(np.max(np.abs(audio))) if audio.size else 0.0


# Width of the smoothing around each peak that has to come down.
_HOLD_SMOOTHING_S = 0.02
# Length of the S-curve that joins a treated tail or head to the
# untouched track it continues.
_SEAM_S = 0.25


def _join_seam(dry: np.ndarray, treated: np.ndarray, sample_rate: int, *, tail: bool) -> np.ndarray:
    """Blend *treated* into the untouched *dry* audio where the two meet.

    A tail follows the outgoing track, so it starts as the dry audio and
    turns into the effect; a head leads into the rest of the incoming
    track, so it ends as the dry audio.  Many effects start or end at
    another level than the music (echo out's first repeat comes
    375 ms after its dry part drops to 0.35), which was a step at the
    join.  The curve is a raised cosine, so the first and last 50 ms
    stay within a few percent of the dry audio.
    """
    n = len(treated)
    m = min(int(sample_rate * _SEAM_S), n // 4)
    if m < 2 or treated is dry:
        return treated
    ramp = (0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, m))).astype(np.float32)
    part = slice(0, m) if tail else slice(n - m, n)
    effect_share = ramp if tail else ramp[::-1]
    if treated.ndim > 1:
        effect_share = effect_share[:, None]
    out = treated.astype(np.float32, copy=True)
    out[part] = dry[part] * (1.0 - effect_share) + treated[part] * effect_share
    return out


def _no_louder_than(audio: np.ndarray, limit: float, sample_rate: int) -> np.ndarray:
    """Turn *audio* down where it passes *limit*, and nowhere else.

    One constant gain for the whole buffer put a level step where the
    treated audio meets the untouched track: a dub delay whose repeats
    build up late turned the start of its tail down 1.5 to 3 dB against
    the audio just before it.  The gain here is 1 wherever the audio is
    under the limit and eases down around each peak over it (a minimum
    filter then a moving average of the same width, which never rises
    above the gain a sample needs), so the seams stay at the music's
    level unless a peak sits right at them.
    """
    if _peak(audio) <= limit:
        return audio
    from scipy.ndimage import minimum_filter1d, uniform_filter1d

    magnitude = np.abs(audio) if audio.ndim == 1 else np.max(np.abs(audio), axis=1)
    needed = np.minimum(1.0, limit / np.maximum(magnitude, 1e-12))
    width = 2 * max(1, int(sample_rate * _HOLD_SMOOTHING_S / 2)) + 1
    gain = uniform_filter1d(minimum_filter1d(needed, width, mode="nearest"), width, mode="nearest")
    if audio.ndim > 1:
        gain = gain[:, None]
    out = (audio * gain).astype(np.float32)
    np.clip(out, -limit, limit, out=out)
    return out


def _level_layer(layer: np.ndarray, target_peak: float) -> np.ndarray:
    """Scale a synthesised *layer* so it peaks at *target_peak*."""
    peak = _peak(layer)
    if peak == 0.0:
        return layer
    return (layer * (target_peak / peak)).astype(np.float32)


# Effects that only transform the OUTGOING tail, each a function and the
# settings that make it this effect.
_TAIL_EFFECTS: dict[TransitionFx, Callable[[np.ndarray, int], np.ndarray]] = {
    # Feedback delays.  echo_out: 375 ms (a quarter note at 160 BPM,
    # which sits loosely on most tempos); dub_delay: a slow 1 s with each
    # repeat darker; flanger: a comb swept from one sample to 6 ms.
    TransitionFx.ECHO_OUT: partial(_delay, delay_ms=375.0, feedback=0.55, wet=0.65),
    TransitionFx.DUB_DELAY: partial(
        _delay, delay_ms=1000.0, feedback=0.55, wet=0.55, damping_hz=1500.0, direct=True
    ),
    TransitionFx.FLANGER: partial(_delay, delay_ms=6.0, feedback=0.3, wet=0.5, sweep_hz=0.5),
    # Variable-speed reads.
    TransitionFx.TAPE_STOP: partial(_spin, speed=lambda n: np.exp(-3.0 * _ramp(n))),
    TransitionFx.PITCH_SWELL: partial(
        _spin, speed=lambda n: np.linspace(1.0, 2.0, n, dtype=np.float32), shortest=4
    ),
    TransitionFx.PITCH_FALL: partial(
        _spin, speed=lambda n: np.linspace(1.0, 0.4, n, dtype=np.float32), shortest=4
    ),
    # Push forward: speed rises 2.5-fold, most of it at the end.
    TransitionFx.FORWARD_SPIN: partial(
        _spin, speed=lambda n: 1.0 + _ramp(n) ** 3 * 1.5, shortest=4
    ),
    # Walkman rewind: the whole tail backwards, slowing to half its start speed.
    TransitionFx.VINYL_REWIND: partial(
        _spin,
        speed=lambda n: 1.0 - 0.5 * _ramp(n),
        reverse=True,
        fade_s=0.1,
        fade_share=8,
        shortest=2,
    ),
    # Backspin: a third plays on, then the record is thrown back fast and
    # friction slows it about fortyfold (the Pioneer DJM / Numark envelope).
    TransitionFx.BACKSPIN: partial(
        _spin,
        speed=lambda n: 2.0 * (1.0 - _ramp(n) ** 2) + 0.05,
        reverse=True,
        lead_in=3,
        fade_s=0.3,
        fade_share=4,
        shortest=3,
    ),
    # Filters.
    TransitionFx.LOWPASS_SWEEP: partial(_sweep, kind="lowpass", start_hz=None, end_hz=250.0),
    TransitionFx.SUBMERGE: partial(_submerge, floor_hz=400.0, wet=0.6),
    TransitionFx.TELEPHONE: partial(_telephone, low_hz=300.0, high_hz=3500.0),
    TransitionFx.REVERB_TAIL: partial(_reverb, wet=0.45),
    TransitionFx.REVERSE_REVERB: partial(_reverse_reverb, reverb_seconds=1.5),
    # Gates and amplitude.  gate_stutter: 1/16 notes at 120 BPM; transformer:
    # a syncopated open/cut pattern at 16 steps a second.
    TransitionFx.GATE_STUTTER: partial(_gate, rate_hz=8.0, pattern=(1,), duty=0.5, max_fade=64),
    TransitionFx.TRANSFORMER: partial(
        _gate, rate_hz=16.0, pattern=(1, 0, 1, 0, 0, 1, 0, 1), duty=1.0, max_fade=32
    ),
    TransitionFx.STUTTER_BUILD: partial(_stutter_build, start_hz=4.0, end_hz=32.0),
    TransitionFx.SIDECHAIN_PUMP: partial(_sidechain_pump, bpm=120.0, depth=0.7),
    # Modulation.
    TransitionFx.CHORUS: partial(_chorus, wet=0.45),
    TransitionFx.PHASER: partial(_phaser, stages=4, lfo_hz=0.5, depth=0.7, feedback=0.4),
    TransitionFx.RING_MODULATOR: partial(_ring_modulator, carrier_hz=173.0),  # about F3
    TransitionFx.VINYL_WOW: partial(_vinyl_wow, rate_hz=1.5, start_depth=0.02, end_depth=0.12),
    TransitionFx.WOW_FLUTTER: partial(
        _wow_flutter, wow_hz=1.5, flutter_hz=8.0, pitch_depth=0.04, amp_depth=0.15
    ),
    # Slices, loops and lo-fi.
    TransitionFx.FREEZE: partial(_freeze, grain_ms=120.0),
    TransitionFx.SCRATCH: partial(_scratch, passes=4, slice_ms=250.0),
    TransitionFx.BEAT_REPEAT: partial(_beat_repeat, slice_ms=250.0, repeats=8),
    TransitionFx.BITCRUSHER: partial(_bitcrusher, start_bits=16, end_bits=4),
    TransitionFx.HALFTIME: _halftime,
}

# Synthesised layers mixed over the crossfade: (length, sample rate, seed).
_LAYERS: dict[TransitionFx, Callable[[int, int, int | None], np.ndarray]] = {
    TransitionFx.NOISE_RISER: partial(
        _noise_sweep, start_hz=200.0, end_hz=16000.0, swell=(0.0, 1.0)
    ),
    TransitionFx.NOISE_DROP: partial(
        _noise_sweep, start_hz=16000.0, end_hz=150.0, swell=(1.0, 0.0)
    ),
    TransitionFx.AIR_HORN: _air_horn,
    TransitionFx.DUB_SIREN: _dub_siren,
}


def _apply_transition_mono(
    tail: np.ndarray,
    head: np.ndarray,
    sample_rate: int,
    effect: TransitionFx,
    seed: int | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Apply *effect* to a mono (tail, head) overlap and return processed buffers.

    Returns:
        ``(tail, head, extra_layer)``, all mono.
    """
    empty_extra = np.zeros(0, dtype=np.float32)
    fn = _TAIL_EFFECTS.get(effect)
    if fn is not None:
        return fn(tail, sample_rate), head, empty_extra
    layer = _LAYERS.get(effect)
    if layer is not None:
        return tail, head, layer(len(tail), sample_rate, seed)
    if effect == TransitionFx.GLITCH:
        return _glitch(tail, sample_rate, seed, slice_ms=80.0), head, empty_extra
    if effect == TransitionFx.HIGHPASS_SWEEP:
        # Filter-in: the incoming track enters as treble only and its bass
        # blooms in as the cutoff falls.
        swept = _sweep(head, sample_rate, kind="highpass", start_hz=4000.0, end_hz=60.0)
        return tail, swept, empty_extra
    if effect == TransitionFx.CROSS_EQ_SWAP:
        t, h = _cross_eq_swap(tail, head, sample_rate, crossover_hz=250.0)
        return t, h, empty_extra
    return tail, head, empty_extra  # NONE


def apply_transition(
    tail: np.ndarray,
    head: np.ndarray,
    sample_rate: int,
    effect: TransitionFx,
    *,
    seed: int | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Apply *effect* to a crossfade, for mono or stereo audio.

    Stereo input is processed one channel at a time with the same *seed*,
    so effects that use randomness treat both channels identically.

    The results are held to the music's level: a treated tail starts as
    the untouched audio and a treated head ends as it (``_join_seam``),
    so no level step is heard where they meet the track; wherever either
    peaks above the audio it came from, it is turned down around that
    peak (``_no_louder_than``); and a synthesised layer is set to a fixed
    fraction of the louder of the two tracks' peaks (see
    ``_LAYER_LEVELS``).

    Args:
        tail: Outgoing tail, ``(n,)`` or ``(n, 2)``.
        head: Incoming head, same channel layout as *tail*.
        sample_rate: Sample rate in Hz.
        effect: Concrete effect to apply.
        seed: Seed for effects that use randomness.  ``None`` draws a fresh
            seed, which is then shared by both channels.

    Returns:
        ``(tail, head, extra_layer)`` in the input's channel layout.  The
        extra layer is empty (length 0) when the effect adds none.
    """
    if tail.ndim == 1:
        out_tail, out_head, extra = _apply_transition_mono(tail, head, sample_rate, effect, seed)
    else:
        out_tail, out_head, extra = _apply_transition_stereo(tail, head, sample_rate, effect, seed)
    music_peak = max(_peak(tail), _peak(head))
    out_tail = _join_seam(tail, out_tail, sample_rate, tail=True)
    out_head = _join_seam(head, out_head, sample_rate, tail=False)
    return (
        _no_louder_than(out_tail, _peak(tail), sample_rate),
        _no_louder_than(out_head, _peak(head), sample_rate),
        _level_layer(extra, _LAYER_LEVELS.get(effect, 0.0) * music_peak),
    )


def _apply_transition_stereo(
    tail: np.ndarray,
    head: np.ndarray,
    sample_rate: int,
    effect: TransitionFx,
    seed: int | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Apply *effect* to ``(n, channels)`` buffers one channel at a time."""
    shared_seed = seed if seed is not None else int(np.random.default_rng().integers(2**31))
    results = [
        _apply_transition_mono(
            np.ascontiguousarray(tail[:, c]),
            np.ascontiguousarray(head[:, c]),
            sample_rate,
            effect,
            shared_seed,
        )
        for c in range(tail.shape[1])
    ]
    stacked = []
    for part in range(3):
        columns = [result[part] for result in results]
        length = min(len(column) for column in columns)
        stacked.append(np.stack([column[:length] for column in columns], axis=1).astype(np.float32))
    return stacked[0], stacked[1], stacked[2]


# Process-local rotation cursor for ROTATE mode
_rotate_cursor = 0


def pick_effect(
    mode: TransitionFx,
    rng: np.random.Generator | None = None,
) -> TransitionFx:
    """Resolve a meta-mode (RANDOM / ROTATE) to a concrete effect.

    Concrete effects pass through unchanged.  NONE returns NONE.

    Args:
        mode: A :class:`TransitionFx`.
        rng: Optional numpy RNG for RANDOM (defaults to a fresh instance).

    Returns:
        A concrete (non-meta) :class:`TransitionFx`.
    """
    global _rotate_cursor
    if mode == TransitionFx.RANDOM:
        if rng is None:
            rng = np.random.default_rng()
        return _REAL_EFFECTS[int(rng.integers(0, len(_REAL_EFFECTS)))]
    if mode == TransitionFx.ROTATE:
        chosen = _REAL_EFFECTS[_rotate_cursor % len(_REAL_EFFECTS)]
        _rotate_cursor += 1
        return chosen
    return mode
