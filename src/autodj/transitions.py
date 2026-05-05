"""Transition-effect library applied at the moment of crossfade.

A "transition effect" is an audio treatment layered ONTO the standard
crossfade so the moment two tracks meet sounds intentional rather than
just fading.  Pro DJs use these to disguise tempo / key clashes and to
add energy lifts.  Each function in this module mutates a short
overlap-region buffer (typically 1–8 bars) and returns the result.

Available effects:

- :func:`echo_out` — feedback-delay tail on outgoing (the "echo throw")
- :func:`reverb_tail` — Schroeder reverb on outgoing
- :func:`highpass_riser` — high-pass sweep DOWN on incoming intro (filter-in)
- :func:`tape_stop` — time-stretch ramp-to-zero on outgoing (vinyl stop)
- :func:`gate_stutter` — rhythmic amplitude gate on outgoing (stutter cut)
- :func:`noise_riser` — synthesised white-noise build between tracks
- :func:`backspin` — pitched-down reverse on outgoing (turntablist sweep)
- :func:`cross_eq_swap` — outgoing keeps highs / drops bass while incoming
  keeps bass / drops highs (mirror of the standard EQ-duck)

The :class:`TransitionFx` enum + :func:`apply_transition` give the player
a single dispatch surface.  All effects are stateless — they take a
buffer in, return a buffer out — so they can be chained or swapped per
crossfade with no setup cost.

Every effect degrades gracefully when scipy is missing (returns the
input unchanged).  None raise on short / silent buffers.
"""

from __future__ import annotations

import logging
from enum import StrEnum

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
    AIR_HORN = "air_horn"  # synth dub-siren riser layered with the music
    RANDOM = "random"  # pick uniformly at random per crossfade
    ROTATE = "rotate"  # cycle through the catalogue in order


# Catalogue used by RANDOM / ROTATE — excludes NONE and the meta-modes.
_REAL_EFFECTS: list[TransitionFx] = [
    TransitionFx.ECHO_OUT,
    TransitionFx.REVERB_TAIL,
    TransitionFx.HIGHPASS_SWEEP,
    TransitionFx.LOWPASS_SWEEP,
    TransitionFx.TAPE_STOP,
    TransitionFx.GATE_STUTTER,
    TransitionFx.NOISE_RISER,
    TransitionFx.CROSS_EQ_SWAP,
    TransitionFx.BITCRUSHER,
    TransitionFx.FLANGER,
    TransitionFx.PITCH_SWELL,
    TransitionFx.TELEPHONE,
    TransitionFx.NOISE_DROP,
    TransitionFx.CHORUS,
    TransitionFx.SUBMERGE,
    TransitionFx.VINYL_WOW,
    TransitionFx.FORWARD_SPIN,
    TransitionFx.FREEZE,
    TransitionFx.GLITCH,
    TransitionFx.SCRATCH,
    TransitionFx.BEAT_REPEAT,
    TransitionFx.SIDECHAIN_PUMP,
    TransitionFx.REVERSE_REVERB,
    TransitionFx.AIR_HORN,
]


# ---------------------------------------------------------------------------
# Outgoing-tail effects (mutate the last `crossfade_samples` of audio_a)
# ---------------------------------------------------------------------------


def echo_out(
    tail: np.ndarray,
    sample_rate: int,
    delay_ms: float = 375.0,
    feedback: float = 0.55,
    wet: float = 0.65,
) -> np.ndarray:
    """Apply a feedback-delay echo to *tail* (the outgoing-track overlap).

    Implements a classic single-tap delay with feedback — sample-rate-agnostic,
    no scipy required.  *delay_ms* defaults to 375 ms ≈ 1/4-note at 160 BPM
    (works well over a wide BPM range; the echo locks loosely to the beat
    without needing the actual BPM).

    Args:
        tail: Mono float32 audio of the crossfade region.
        sample_rate: Sample rate in Hz.
        delay_ms: Delay length.  Try 250–500 ms.
        feedback: How much of the delayed signal feeds back into itself
            (0.0 = single echo, 0.95 = nearly endless).
        wet: Mix of the dry tail vs the echoed signal in the output
            (0.0 = dry only, 1.0 = wet only).

    Returns:
        Float32 array of the same length as *tail*, hard-clipped ±1.0.
    """
    if len(tail) == 0:
        return tail
    delay = max(1, int((delay_ms / 1000.0) * sample_rate))
    out = tail.astype(np.float32, copy=True)
    # Wet bus: in-place feedback delay
    wet_buf = np.zeros_like(out)
    for i in range(len(out)):
        if i >= delay:
            wet_buf[i] = out[i - delay] + feedback * wet_buf[i - delay]
    mixed = (1.0 - wet) * out + wet * wet_buf
    np.clip(mixed, -1.0, 1.0, out=mixed)
    return mixed.astype(np.float32)


def reverb_tail(
    tail: np.ndarray,
    sample_rate: int,
    wet: float = 0.45,
) -> np.ndarray:
    """Add a Schroeder reverb to *tail* (parallel comb + serial allpass).

    Pure-numpy implementation — no scipy needed.  Sounds like a small
    room (~1 s reverb time).  Adds tail decay that bleeds into the
    incoming track, smoothing key clashes.

    Args:
        tail: Mono float32 audio of the outgoing overlap.
        sample_rate: Sample rate in Hz.
        wet: Wet/dry mix (0.0 = dry, 1.0 = wet only).

    Returns:
        Reverberated float32 array of the same length as *tail*.
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
    wet_sum /= max(1, len(comb_delays))

    # Two serial allpass filters — break up combs to sound less metallic
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

    mixed = (1.0 - wet) * tail + wet * wet_sum
    np.clip(mixed, -1.0, 1.0, out=mixed)
    return mixed.astype(np.float32)


def tape_stop(
    tail: np.ndarray,
    sample_rate: int,
    curve: str = "exponential",
) -> np.ndarray:
    """Apply a vinyl-stop / tape-stop ramp to *tail* (slows pitch + speed to zero).

    Implementation: progressive resampling — each output sample is read
    from a position that advances ever more slowly through *tail*.
    Sounds exactly like flicking a turntable's stop button.

    Args:
        tail: Mono float32 audio of the outgoing overlap.
        sample_rate: Sample rate in Hz (unused — kept for API consistency).
        curve: ``"exponential"`` (more dramatic, classic tape feel) or
            ``"linear"`` (gentler).

    Returns:
        Tape-stopped float32 array of the same length as *tail*.
    """
    n = len(tail)
    if n == 0:
        return tail
    # Speed envelope: starts at 1.0, ramps to 0.0 over the buffer length
    if curve == "linear":
        speed = np.linspace(1.0, 0.0, n, dtype=np.float32)
    else:
        # Exponential decay — most of the slowdown happens in the last 1/3
        speed = np.exp(-3.0 * np.linspace(0.0, 1.0, n, dtype=np.float32))
    # Cumulative read position
    read_pos = np.cumsum(speed)
    # Normalise so the maximum read position equals n - 1 (we use the
    # whole tail).  Without this the early/exponential curves don't reach
    # the end of the buffer.
    if read_pos[-1] > 0:
        read_pos = read_pos * ((n - 1) / read_pos[-1])
    out = np.empty(n, dtype=np.float32)
    idx = read_pos.astype(np.int32)
    np.clip(idx, 0, n - 1, out=idx)
    out[:] = tail[idx]
    return out


def gate_stutter(
    tail: np.ndarray,
    sample_rate: int,
    gate_hz: float = 8.0,
    duty: float = 0.5,
) -> np.ndarray:
    """Apply a hard amplitude gate at *gate_hz* — chops the tail into a stutter.

    Args:
        tail: Mono float32 audio.
        sample_rate: Sample rate in Hz.
        gate_hz: Gate rate in Hz.  8 Hz ≈ 1/16-note at 120 BPM.
        duty: Fraction of each cycle that's open (0.5 = square, 0.25 = punchy).

    Returns:
        Gated float32 array of the same length as *tail*.
    """
    n = len(tail)
    if n == 0 or gate_hz <= 0:
        return tail
    cycle = max(1, int(sample_rate / gate_hz))
    open_samples = max(1, int(cycle * duty))
    out = np.zeros_like(tail, dtype=np.float32)
    for start in range(0, n, cycle):
        end = min(n, start + open_samples)
        out[start:end] = tail[start:end]
    # Gentle 64-sample fade-in on each open block to avoid clicks
    fade = min(64, open_samples // 4)
    if fade > 0:
        env = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        for start in range(0, n, cycle):
            end = min(n, start + fade)
            if end > start:
                out[start:end] *= env[: end - start]
    return out


def backspin(
    tail: np.ndarray,
    sample_rate: int,
) -> np.ndarray:
    """Vinyl backspin on the last 2/3 of *tail* — decelerating reverse.

    Models real-world physics: a DJ pushes the record back at ~2× speed,
    friction decelerates it to a stop over ~2 seconds.  The first 1/3
    of *tail* plays normally; the final 2/3 reverses and time-stretches
    with rate decaying 2.0 → 0.05 (industry-standard envelope used by
    Pioneer DJM "Backspin" + Numark "Reverse Roll").

    Args:
        tail: Mono float32 audio.
        sample_rate: Sample rate in Hz (unused — kept for API consistency).

    Returns:
        Float32 array of the same length as *tail*.
    """
    n = len(tail)
    if n < 3:
        return tail
    head_n = n // 3
    spin_n = n - head_n

    # Reversed source segment, taken from immediately before the spin region.
    # Pull in audio up to twice as long as spin_n so the variable rate has
    # source material to read from at the high-rate start.
    src_start = max(0, head_n - 2 * spin_n)
    src = tail[src_start:head_n][::-1]
    if len(src) == 0:
        return tail

    # Variable-rate read: rate decelerates 2.0 → 0.05 (decel curve, not linear,
    # to match physical friction).  Quadratic falls off harder near the end.
    t = np.linspace(0.0, 1.0, spin_n, dtype=np.float32)
    rate = (2.0 * (1.0 - t * t) + 0.05).astype(np.float32)
    pos = np.cumsum(rate)
    if pos[-1] > 0:
        pos = pos * ((len(src) - 1) / pos[-1])
    idx = pos.astype(np.int32)
    np.clip(idx, 0, len(src) - 1, out=idx)
    spin = src[idx]

    # Apply gentle amplitude fade in the final 0.3 s so the spin lands on silence
    fade_samples = min(int(0.3 * sample_rate), spin_n // 4)
    if fade_samples > 0:
        env = np.linspace(1.0, 0.0, fade_samples, dtype=np.float32)
        spin[-fade_samples:] *= env

    out = np.concatenate([tail[:head_n], spin]).astype(np.float32)
    if len(out) < n:
        out = np.pad(out, (0, n - len(out)))
    return out[:n]


# ---------------------------------------------------------------------------
# Incoming-head effects (mutate the first `crossfade_samples` of audio_b)
# ---------------------------------------------------------------------------


def highpass_sweep(
    head: np.ndarray,
    sample_rate: int,
    start_hz: float = 4000.0,
    end_hz: float = 60.0,
) -> np.ndarray:
    """High-pass sweep DOWN on the incoming-track head — "filter-in".

    Mirror of the standard outgoing filter sweep.  The incoming track
    enters muffled (only highs above *start_hz*) and the cutoff sweeps
    down to *end_hz* over the buffer length, so the bass blooms in
    progressively.  Pairs naturally with :func:`echo_out` on the
    outgoing side.

    Falls back to the unfiltered head when scipy is unavailable.

    Args:
        head: Mono float32 audio of the incoming overlap.
        sample_rate: Sample rate in Hz.
        start_hz: Cutoff at sample 0 (high — only treble passes).
        end_hz: Cutoff at the last sample (low — full range).

    Returns:
        Filtered float32 array of the same length as *head*.
    """
    # Reuse the linear sweep helper from player.py to avoid duplication
    from autodj.player import apply_filter_sweep

    return apply_filter_sweep(
        head, sample_rate, start_hz=start_hz, end_hz=end_hz, filter_type="highpass"
    )


def lowpass_sweep(
    tail: np.ndarray,
    sample_rate: int,
    start_hz: float | None = None,
    end_hz: float = 250.0,
) -> np.ndarray:
    """Low-pass sweep DOWN on the outgoing-track tail — "filter-out".

    Mirror of :func:`highpass_sweep`.  The outgoing track loses its
    high-frequency content gradually (cutoff sliding from full-range
    down to *end_hz*), giving the classic DJ filter-out effect that
    launches a build.  Pairs naturally with :func:`echo_out` or
    :func:`noise_riser` on the incoming side.

    Args:
        tail: Mono float32 audio of the outgoing overlap.
        sample_rate: Sample rate in Hz.
        start_hz: Cutoff at sample 0.  Defaults to nyquist (full range).
        end_hz: Cutoff at the last sample (low — bass / kick territory).

    Returns:
        Filtered float32 array of the same length as *tail*.
    """
    from autodj.player import apply_filter_sweep

    if start_hz is None:
        start_hz = sample_rate / 2.0
    return apply_filter_sweep(
        tail, sample_rate, start_hz=start_hz, end_hz=end_hz, filter_type="lowpass"
    )


def bitcrusher(
    tail: np.ndarray,
    sample_rate: int,
    start_bits: int = 16,
    end_bits: int = 4,
) -> np.ndarray:
    """Progressive bit-depth crush on the outgoing tail — lo-fi degrade.

    Linearly drops the effective bit depth from *start_bits* to *end_bits*
    over the buffer length, quantising amplitude to fewer levels.  The
    resulting audible noise + distortion is a recognisable "digital
    breakdown" transition.

    Args:
        tail: Mono float32 audio.
        sample_rate: Sample rate in Hz (unused — kept for API consistency).
        start_bits: Bit depth at sample 0.  16 = no audible change.
        end_bits: Bit depth at the last sample.  3-4 = harsh crush.

    Returns:
        Crushed float32 array of the same length as *tail*.
    """
    n = len(tail)
    if n == 0:
        return tail
    # Per-sample bit depth, integer.
    depths = np.linspace(start_bits, end_bits, n).astype(np.int32)
    levels = (1 << (depths - 1)).astype(np.float32)  # 2^(bits-1)
    out = np.round(tail * levels) / np.maximum(levels, 1.0)
    np.clip(out, -1.0, 1.0, out=out)
    return out.astype(np.float32)


def flanger(
    tail: np.ndarray,
    sample_rate: int,
    rate_hz: float = 0.5,
    max_delay_ms: float = 6.0,
    feedback: float = 0.3,
    wet: float = 0.5,
) -> np.ndarray:
    """LFO-modulated short-delay flanger on the outgoing tail.

    A classic flanger: a comb-filter delay whose length sweeps with a
    low-frequency oscillator, mixed with the dry signal.  Feedback
    intensifies the swirly metallic character.

    Args:
        tail: Mono float32 audio.
        sample_rate: Sample rate in Hz.
        rate_hz: LFO sweep rate.  0.3-1 Hz is typical.
        max_delay_ms: Peak delay length in ms (1-10 ms is the flanger range).
        feedback: 0.0-0.9.  Higher = more metallic resonance.
        wet: Wet/dry mix.

    Returns:
        Flanged float32 array of the same length as *tail*.
    """
    n = len(tail)
    if n == 0:
        return tail
    max_delay = max(2, int((max_delay_ms / 1000.0) * sample_rate))
    t = np.arange(n) / sample_rate
    # LFO 0..1 — rectified-sine sweep
    lfo = 0.5 * (1 - np.cos(2 * np.pi * rate_hz * t))
    delay_samples = (lfo * (max_delay - 1)).astype(np.int32) + 1
    out = tail.astype(np.float32, copy=True)
    wet_buf = np.zeros_like(out)
    for i in range(n):
        d = delay_samples[i]
        if i >= d:
            wet_buf[i] = out[i - d] + feedback * wet_buf[i - d]
    mixed = (1.0 - wet) * out + wet * wet_buf
    np.clip(mixed, -1.0, 1.0, out=mixed)
    return mixed.astype(np.float32)


def pitch_swell(
    tail: np.ndarray,
    sample_rate: int,
) -> np.ndarray:
    """Pitch-up swell on the outgoing tail — opposite of :func:`tape_stop`.

    Speed accelerates from 1.0× to ~2.0× over the tail (with pitch
    coupled to speed via simple resampling).  Sounds like a tape
    rewind played forward, building tension into the cut.

    Args:
        tail: Mono float32 audio.
        sample_rate: Sample rate (unused — kept for API consistency).

    Returns:
        Pitch-swelled float32 array of the same length as *tail*.
    """
    n = len(tail)
    if n < 4:
        return tail
    # Accelerating speed envelope: 1.0 -> 2.0
    speed = np.linspace(1.0, 2.0, n, dtype=np.float32)
    read_pos = np.cumsum(speed)
    if read_pos[-1] > 0:
        read_pos = read_pos * ((n - 1) / read_pos[-1])
    idx = read_pos.astype(np.int32)
    np.clip(idx, 0, n - 1, out=idx)
    return tail[idx].astype(np.float32)


def telephone(
    tail: np.ndarray,
    sample_rate: int,
) -> np.ndarray:
    """Narrow band-pass on the outgoing tail — "phone call" / radio sound.

    Passes ~300-3500 Hz, drops everything else.  Combined with the
    amplitude crossfade it sounds like the outgoing track is being
    answered through a low-fi handset right before the new track lands.

    Args:
        tail: Mono float32 audio.
        sample_rate: Sample rate in Hz.

    Returns:
        Band-passed float32 array of the same length as *tail*.
    """
    if len(tail) == 0:
        return tail
    try:
        from scipy.signal import butter, sosfilt
    except ImportError:
        return tail
    nyq = sample_rate / 2.0
    lo = max(1e-4, min(0.99, 300.0 / nyq))
    hi = max(1e-4, min(0.99, 3500.0 / nyq))
    sos_hp = butter(4, lo, btype="high", output="sos")
    sos_lp = butter(4, hi, btype="low", output="sos")
    out = sosfilt(sos_lp, sosfilt(sos_hp, tail)).astype(np.float32)
    return out


# ---------------------------------------------------------------------------
# Standalone (synthesised, no source audio)
# ---------------------------------------------------------------------------


def noise_riser(
    n_samples: int,
    sample_rate: int,
    cutoff_start_hz: float = 200.0,
    cutoff_end_hz: float = 16000.0,
    peak_amplitude: float = 0.35,
) -> np.ndarray:
    """Generate a synthesised white-noise riser of *n_samples* length.

    Output is white noise band-pass filtered with the cutoff sweeping up
    from *cutoff_start_hz* to *cutoff_end_hz*, amplitude rising from 0
    to *peak_amplitude*.  Designed to be ADDED to the crossfade overlap
    so it crests right at the mix point.

    Args:
        n_samples: Length of the riser in samples.
        sample_rate: Sample rate in Hz.
        cutoff_start_hz: Low-pass cutoff at sample 0.
        cutoff_end_hz: Low-pass cutoff at the last sample.
        peak_amplitude: Maximum amplitude reached at the end (0.0–1.0).

    Returns:
        Synthesised float32 array of length *n_samples*.
    """
    if n_samples <= 0:
        return np.zeros(0, dtype=np.float32)
    rng = np.random.default_rng()
    noise = rng.standard_normal(n_samples).astype(np.float32) * 0.5
    # Sweep band-pass via the sweeping low-pass helper
    from autodj.player import apply_filter_sweep

    swept = apply_filter_sweep(
        noise,
        sample_rate,
        start_hz=cutoff_start_hz,
        end_hz=cutoff_end_hz,
        filter_type="lowpass",
    )
    # Linear amplitude rise
    env = np.linspace(0.0, peak_amplitude, n_samples, dtype=np.float32)
    return (swept * env).astype(np.float32)


# ---------------------------------------------------------------------------
# Cross-EQ swap (acts on both outgoing tail + incoming head simultaneously)
# ---------------------------------------------------------------------------


def cross_eq_swap(
    tail: np.ndarray,
    head: np.ndarray,
    sample_rate: int,
    crossover_hz: float = 250.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Mirror EQ-duck — outgoing keeps highs while incoming brings the bass.

    Splits both buffers around *crossover_hz* (bass / treble).  In the
    mixed output:
    - outgoing keeps its TREBLE band (bass progressively removed)
    - incoming keeps its BASS band (treble progressively muted at the start
      and added back over the buffer length)

    The two bands then sum naturally during the standard amplitude
    crossfade, producing a smooth bass-handover instead of bass-clash.

    Args:
        tail: Outgoing audio (last *N* samples of audio_a).
        head: Incoming audio (first *N* samples of audio_b).
        sample_rate: Sample rate in Hz.
        crossover_hz: Bass/treble split frequency.

    Returns:
        ``(tail_treble, head_bass)`` — pre-processed buffers ready to
        feed straight into a linear crossfade.
    """
    try:
        from scipy.signal import butter, sosfilt
    except ImportError:
        return tail, head

    nyq = sample_rate / 2.0
    cutoff = max(1e-4, min(0.99, crossover_hz / nyq))
    hp = butter(4, cutoff, btype="high", output="sos")
    lp = butter(4, cutoff, btype="low", output="sos")

    tail_treble = sosfilt(hp, tail).astype(np.float32)
    head_bass = sosfilt(lp, head).astype(np.float32)

    # Bring incoming treble back over the second half so the new track
    # doesn't sound permanently bass-only.
    n = len(head)
    head_treble = sosfilt(hp, head).astype(np.float32)
    bring_in = np.linspace(0.0, 1.0, n, dtype=np.float32)
    head_full = head_bass + head_treble * bring_in

    return tail_treble, head_full


# ---------------------------------------------------------------------------
# Browser-parity effects ported from Web Audio
# ---------------------------------------------------------------------------


def chorus(
    tail: np.ndarray,
    sample_rate: int,
    wet: float = 0.45,
) -> np.ndarray:
    """3-voice detuned chorus on the outgoing tail.

    Three short delays (20/25/30 ms) modulated by independent slow LFOs
    produce a thick doubled-vocal / lush instrument feel.  Mirrors the
    browser-side `chorus` effect built from `DelayNode` + `OscillatorNode`.

    Args:
        tail: Mono float32 audio.
        sample_rate: Sample rate in Hz.
        wet: Mix of chorused signal vs dry (0.0 = dry, 1.0 = wet).

    Returns:
        Chorused float32 array of the same length as *tail*.
    """
    n = len(tail)
    if n == 0:
        return tail
    rates = (0.4, 0.6, 0.8)
    base_delays_ms = (20.0, 25.0, 30.0)
    depths_ms = (3.0, 4.0, 5.0)

    t = np.arange(n) / sample_rate
    wet_sum = np.zeros(n, dtype=np.float32)
    for rate, base_ms, depth_ms in zip(rates, base_delays_ms, depths_ms, strict=True):
        # Per-sample fractional delay
        delay = (base_ms + depth_ms * np.sin(2 * np.pi * rate * t)) / 1000.0
        idx = np.arange(n) - (delay * sample_rate)
        idx = np.clip(idx, 0, n - 1)
        i0 = idx.astype(np.int32)
        frac = (idx - i0).astype(np.float32)
        # Linear interpolation
        i1 = np.minimum(i0 + 1, n - 1)
        wet_sum += (tail[i0] * (1.0 - frac) + tail[i1] * frac).astype(np.float32)

    wet_sum /= len(rates)
    out = (1.0 - wet) * tail + wet * wet_sum
    np.clip(out, -1.0, 1.0, out=out)
    return out.astype(np.float32)


def submerge(
    tail: np.ndarray,
    sample_rate: int,
    floor_hz: float = 400.0,
    wet: float = 0.6,
) -> np.ndarray:
    """Underwater wash — heavy lowpass sweep + reverb wash on outgoing.

    Combines :func:`reverb_tail`'s wet signal with a steep lowpass that
    progressively closes the high end.  Result: outgoing track sounds
    like it's submerging.

    Args:
        tail: Mono float32 audio.
        sample_rate: Sample rate in Hz.
        floor_hz: Final lowpass cutoff at end of buffer.
        wet: Reverb wet/dry mix.

    Returns:
        Submerged float32 array.
    """
    if len(tail) == 0:
        return tail
    # Sweeping lowpass via the existing helper (player.apply_filter_sweep)
    from autodj.player import apply_filter_sweep

    swept = apply_filter_sweep(
        tail,
        sample_rate,
        start_hz=sample_rate / 2.0,
        end_hz=floor_hz,
        filter_type="lowpass",
    )
    rev = reverb_tail(swept, sample_rate, wet=wet)
    return rev.astype(np.float32)


def vinyl_wow(
    tail: np.ndarray,
    sample_rate: int,
    rate_hz: float = 1.5,
    start_depth: float = 0.02,
    end_depth: float = 0.12,
) -> np.ndarray:
    """Pitch wobble (drunk turntable / tape wow) on the outgoing tail.

    LFO-modulated time-stretch / fractional-delay read produces a
    seasick pitch wobble that grows from *start_depth* to *end_depth*
    over the buffer length.

    Args:
        tail: Mono float32 audio.
        sample_rate: Sample rate in Hz.
        rate_hz: LFO frequency.
        start_depth: Initial pitch deviation (fractional, e.g. 0.02 = ±2 %).
        end_depth: Final pitch deviation.

    Returns:
        Wobbled float32 array of the same length as *tail*.
    """
    n = len(tail)
    if n == 0:
        return tail
    t = np.arange(n) / sample_rate
    depth = np.linspace(start_depth, end_depth, n, dtype=np.float32)
    rate = (1.0 + depth * np.sin(2 * np.pi * rate_hz * t)).astype(np.float32)
    # Cumulative read position with variable rate, normalised to fit
    pos = np.cumsum(rate)
    if pos[-1] > 0:
        pos = pos * ((n - 1) / pos[-1])
    i0 = pos.astype(np.int32)
    frac = (pos - i0).astype(np.float32)
    i1 = np.minimum(i0 + 1, n - 1)
    out = tail[i0] * (1.0 - frac) + tail[i1] * frac
    return out.astype(np.float32)


def freeze(
    tail: np.ndarray,
    sample_rate: int,
    grain_ms: float = 120.0,
    fade_out: bool = True,
) -> np.ndarray:
    """Capture the last *grain_ms* of audio and loop it for the rest of the tail.

    Hands-down a worklet-friendly effect (the browser implementation runs
    in the AudioWorklet thread for sample-accuracy) — the numpy version
    here mirrors the same logic for CLI playback.  Slight crossfade on
    every loop seam prevents clicks.

    Args:
        tail: Mono float32 audio.
        sample_rate: Sample rate in Hz.
        grain_ms: Length of the captured slice that loops.
        fade_out: If True, ramp the looped output to zero over the tail.

    Returns:
        Float32 array of the same length as *tail*.
    """
    n = len(tail)
    if n == 0:
        return tail
    grain_samples = max(1, int(grain_ms * sample_rate / 1000.0))
    grain_samples = min(grain_samples, n)
    grain = tail[-grain_samples:].astype(np.float32, copy=True)

    # Smooth the grain seam with a small linear crossfade between end-of-grain
    # and start-of-grain so the loop point doesn't click.
    seam = min(grain_samples // 8, int(0.005 * sample_rate))
    if seam > 0:
        fade = np.linspace(1.0, 0.0, seam, dtype=np.float32)
        head = grain[:seam].copy()
        grain[:seam] = grain[:seam] * (1.0 - fade) + grain[-seam:] * fade
        grain[-seam:] = grain[-seam:] * fade + head * (1.0 - fade)

    out = np.empty(n, dtype=np.float32)
    for i in range(n):
        out[i] = grain[i % grain_samples]

    if fade_out:
        env = np.linspace(1.0, 0.0, n, dtype=np.float32)
        out *= env
    return out


def glitch(
    tail: np.ndarray,
    sample_rate: int,
    slice_ms: float = 80.0,
    seed: int | None = None,
) -> np.ndarray:
    """Slice *tail* into short grains and re-order them randomly.

    Each output slice is one of the input slices picked at random (with
    replacement).  Slice boundaries crossfade with a 5 ms ramp so the
    seams don't click.  Reproducible with *seed*.

    Args:
        tail: Mono float32 audio.
        sample_rate: Sample rate in Hz.
        slice_ms: Slice length.  Smaller = more chaotic.
        seed: Optional RNG seed for reproducibility.

    Returns:
        Float32 array of the same length as *tail*.
    """
    n = len(tail)
    if n == 0:
        return tail
    slice_samples = max(1, int(slice_ms * sample_rate / 1000.0))
    if slice_samples >= n:
        return tail.astype(np.float32, copy=True)

    rng = np.random.default_rng(seed)
    n_slices = (n + slice_samples - 1) // slice_samples
    src_slices = n // slice_samples
    if src_slices == 0:
        return tail.astype(np.float32, copy=True)

    out = np.zeros(n, dtype=np.float32)
    seam = min(slice_samples // 16, int(0.005 * sample_rate))
    fade_in = np.linspace(0.0, 1.0, seam, dtype=np.float32) if seam > 0 else None
    fade_out_e = np.linspace(1.0, 0.0, seam, dtype=np.float32) if seam > 0 else None
    for i in range(n_slices):
        src_idx = int(rng.integers(0, src_slices))
        src_start = src_idx * slice_samples
        src = tail[src_start : src_start + slice_samples].copy()
        if seam > 0 and len(src) >= 2 * seam:
            src[:seam] *= fade_in
            src[-seam:] *= fade_out_e
        dst_start = i * slice_samples
        dst_end = min(dst_start + slice_samples, n)
        out[dst_start:dst_end] = src[: dst_end - dst_start]
    return out


def scratch(
    tail: np.ndarray,
    sample_rate: int,
    n_passes: int = 4,
    slice_ms: float = 250.0,
) -> np.ndarray:
    """Turntablist scratch — rapid back-and-forth sweep over a short slice.

    Captures the last *slice_ms* of audio, then plays it forward and
    reverse alternately *n_passes* times across the full tail length.
    Each pass is variable-speed so the scratch sounds rhythmic rather
    than mechanical.

    Args:
        tail: Mono float32 audio.
        sample_rate: Sample rate in Hz.
        n_passes: Total forward+reverse passes (4 = 2× forward, 2× reverse).
        slice_ms: Length of the scratched slice in ms.

    Returns:
        Float32 array of the same length as *tail*.
    """
    n = len(tail)
    if n < 4:
        return tail
    slice_samples = max(2, int(slice_ms * sample_rate / 1000.0))
    slice_samples = min(slice_samples, n)
    src = tail[-slice_samples:].astype(np.float32, copy=True)

    out = np.empty(n, dtype=np.float32)
    pass_len = n // n_passes
    for p in range(n_passes):
        start = p * pass_len
        end = start + pass_len if p < n_passes - 1 else n
        plen = end - start
        # Variable-rate read with sine envelope so each pass accelerates
        # then decelerates — the classic "wikka" sound.
        t = np.linspace(0.0, 1.0, plen, dtype=np.float32)
        rate = (0.4 + 1.6 * np.sin(np.pi * t)).astype(np.float32)
        pos = np.cumsum(rate)
        if pos[-1] > 0:
            pos = pos * ((slice_samples - 1) / pos[-1])
        idx = pos.astype(np.int32)
        np.clip(idx, 0, slice_samples - 1, out=idx)
        if p % 2 == 1:
            idx = (slice_samples - 1) - idx
        out[start:end] = src[idx]
    return out


def beat_repeat(
    tail: np.ndarray,
    sample_rate: int,
    slice_ms: float = 250.0,
    n_repeats: int = 8,
) -> np.ndarray:
    """Beat-repeat / loop-roll — capture short slice, retrigger N times.

    Pioneer DJM "Loop Roll" / Mixxx "Beat Loop": grabs a small slice
    from the end of the outgoing tail and stamps it across the whole
    tail length *n_repeats* times.  Each retrigger has a short fade-in/
    fade-out to avoid clicks.

    Args:
        tail: Mono float32 audio.
        sample_rate: Sample rate in Hz.
        slice_ms: Slice length in ms (250 ms ≈ 1/2 beat at 120 BPM).
        n_repeats: How many times to repeat the slice across the tail.

    Returns:
        Float32 array of the same length as *tail*.
    """
    n = len(tail)
    if n < 4:
        return tail
    slice_samples = max(2, int(slice_ms * sample_rate / 1000.0))
    slice_samples = min(slice_samples, n // 2)
    src = tail[-slice_samples:].astype(np.float32, copy=True)
    seam = min(slice_samples // 16, int(0.005 * sample_rate))
    if seam > 0:
        src[:seam] *= np.linspace(0.0, 1.0, seam, dtype=np.float32)
        src[-seam:] *= np.linspace(1.0, 0.0, seam, dtype=np.float32)

    out = np.zeros(n, dtype=np.float32)
    chunk_len = n // n_repeats
    for i in range(n_repeats):
        start = i * chunk_len
        end = min(start + slice_samples, n)
        out[start:end] = src[: end - start]
    return out


def sidechain_pump(
    tail: np.ndarray,
    sample_rate: int,
    bpm: float = 120.0,
    depth: float = 0.7,
) -> np.ndarray:
    """Rhythmic 4-on-the-floor amplitude pump.

    Models the sidechain-compression sound EDM producers get from
    ducking everything against the kick drum.  Applies a periodic
    envelope at the configured BPM: full-attenuation at every beat
    onset, exponential recovery between beats.

    Args:
        tail: Mono float32 audio.
        sample_rate: Sample rate in Hz.
        bpm: Beats per minute for the pump rate.  Default 120.
        depth: 1.0 = full duck (silence at beat), 0.0 = no pump.

    Returns:
        Float32 array of the same length as *tail*.
    """
    n = len(tail)
    if n == 0:
        return tail
    period_samples = max(1, int(60.0 / bpm * sample_rate))
    # Per-beat envelope: starts at (1 - depth), recovers exponentially to 1
    t_in_beat = np.arange(n) % period_samples
    recovery = 1.0 - depth * np.exp(-3.0 * t_in_beat / period_samples)
    return (tail * recovery.astype(np.float32)).astype(np.float32)


def reverse_reverb(
    tail: np.ndarray,
    sample_rate: int,
    reverb_seconds: float = 1.5,
) -> np.ndarray:
    """Reverse'd reverb that swells INTO the cut point.

    Builds an exponentially-decaying reverb impulse, reverses it, then
    convolves with the tail.  Sound: a wash that crescendos right up
    to the moment the new track lands — classic "incoming" effect from
    pop and trance production.

    Args:
        tail: Mono float32 audio.
        sample_rate: Sample rate in Hz.
        reverb_seconds: Length of the reverse'd reverb impulse.

    Returns:
        Float32 array of the same length as *tail*.
    """
    n = len(tail)
    if n == 0:
        return tail
    rng = np.random.default_rng(seed=0xA17DA)
    ir_len = max(1, int(reverb_seconds * sample_rate))
    decay = np.linspace(0.0, 1.0, ir_len, dtype=np.float32) ** 2  # reversed env
    ir = rng.standard_normal(ir_len).astype(np.float32) * decay * 0.05

    # Convolve via numpy (slow for huge buffers but adequate for crossfade tails)
    convolved = np.convolve(tail, ir, mode="full")[:n].astype(np.float32)
    # Mix wet over dry — full wet for the swell to be audible
    out = (tail * 0.4 + convolved * 1.6).astype(np.float32)
    np.clip(out, -1.0, 1.0, out=out)
    return out


def air_horn(
    tail: np.ndarray,
    sample_rate: int,
) -> np.ndarray:
    """Synth dub-siren / air-horn riser layered with the outgoing audio.

    Generates a square-wave horn that rises in pitch + volume across
    the tail length, summed with the original audio.  Loud, classic
    DJ build-up cliché.  Use sparingly.

    Args:
        tail: Mono float32 audio.
        sample_rate: Sample rate in Hz.

    Returns:
        Float32 array of the same length as *tail*.
    """
    n = len(tail)
    if n == 0:
        return tail
    t = np.arange(n) / sample_rate
    # Pitch sweep 220 → 880 Hz over the tail length
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
    horn *= env * 0.35  # peak ~0.35 so it sits with the music
    out = (tail + horn).astype(np.float32)
    np.clip(out, -1.0, 1.0, out=out)
    del t  # silence unused-var lint
    return out


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def apply_transition(
    tail: np.ndarray,
    head: np.ndarray,
    sample_rate: int,
    effect: TransitionFx,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Apply *effect* to the (tail, head) overlap and return processed buffers.

    Returns a tuple ``(tail_out, head_out, extra_layer)`` where
    *extra_layer* is summed on top of the crossfade output (used by
    :func:`noise_riser`; zero-length array for every other effect).

    Unknown / NONE effect = passthrough.

    Args:
        tail: Outgoing-track overlap (last *N* samples of audio_a).
        head: Incoming-track overlap (first *N* samples of audio_b).
        sample_rate: Sample rate in Hz.
        effect: Which transition to apply.

    Returns:
        ``(tail_out, head_out, extra_layer)`` ready to feed into the
        amplitude crossfade.
    """
    n = len(tail)
    empty_extra = np.zeros(0, dtype=np.float32)

    if effect == TransitionFx.NONE:
        return tail, head, empty_extra

    if effect == TransitionFx.ECHO_OUT:
        return echo_out(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.REVERB_TAIL:
        return reverb_tail(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.HIGHPASS_SWEEP:
        return tail, highpass_sweep(head, sample_rate), empty_extra

    if effect == TransitionFx.LOWPASS_SWEEP:
        return lowpass_sweep(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.TAPE_STOP:
        return tape_stop(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.BITCRUSHER:
        return bitcrusher(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.FLANGER:
        return flanger(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.PITCH_SWELL:
        return pitch_swell(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.TELEPHONE:
        return telephone(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.CHORUS:
        return chorus(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.SUBMERGE:
        return submerge(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.VINYL_WOW:
        return vinyl_wow(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.FREEZE:
        return freeze(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.GLITCH:
        return glitch(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.SCRATCH:
        return scratch(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.BEAT_REPEAT:
        return beat_repeat(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.SIDECHAIN_PUMP:
        return sidechain_pump(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.REVERSE_REVERB:
        return reverse_reverb(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.AIR_HORN:
        return air_horn(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.NOISE_DROP:
        # Synthesised noise that crests at the start and falls/dimms.
        if len(tail) == 0:
            return tail, head, empty_extra
        rng = np.random.default_rng()
        noise = rng.standard_normal(len(tail)).astype(np.float32) * 0.5
        from autodj.player import apply_filter_sweep

        swept = apply_filter_sweep(
            noise, sample_rate, start_hz=16000, end_hz=150, filter_type="lowpass"
        )
        env = np.linspace(0.4, 0.0, len(tail), dtype=np.float32)
        return tail, head, (swept * env).astype(np.float32)

    if effect == TransitionFx.FORWARD_SPIN:
        # Forward-spin = accelerating playback into the cut.  Mirror of
        # backspin (which uses reversed read).  Simple resampling with
        # cubic-ease-in rate from 1.0 → 2.5.
        n = len(tail)
        if n < 4:
            return tail, head, empty_extra
        t = np.linspace(0.0, 1.0, n, dtype=np.float32)
        rate = (1.0 + (t**3) * 1.5).astype(np.float32)
        pos = np.cumsum(rate)
        if pos[-1] > 0:
            pos = pos * ((n - 1) / pos[-1])
        idx = pos.astype(np.int32)
        np.clip(idx, 0, n - 1, out=idx)
        return tail[idx].astype(np.float32), head, empty_extra

    if effect == TransitionFx.GATE_STUTTER:
        return gate_stutter(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.BACKSPIN:
        return backspin(tail, sample_rate), head, empty_extra

    if effect == TransitionFx.NOISE_RISER:
        return tail, head, noise_riser(n, sample_rate)

    if effect == TransitionFx.CROSS_EQ_SWAP:
        t, h = cross_eq_swap(tail, head, sample_rate)
        return t, h, empty_extra

    return tail, head, empty_extra


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
