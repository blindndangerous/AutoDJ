"""Beat-matched entry of the incoming track in the server mix.

With ``[djmix] beatmatch`` on, the server mix plays the incoming track at
the outgoing track's tempo while the two overlap, then eases it back to
its own tempo.  Only that stretch of the incoming track is time-stretched,
never the whole song:

1. :func:`tempo_ratio` finds the stretch that puts the incoming tempo on
   the outgoing one.  Tempos a factor of two apart count as the same
   tempo (70 BPM against 140 BPM is half time), so they match as well.
2. :func:`phase_locked_start` moves the fade start in the outgoing track
   so the incoming track's first downbeat lands on one of the outgoing
   track's downbeats, using both tracks' beat grids.
3. :class:`Glide` runs a phase vocoder over the incoming track with a
   variable rate: the matched tempo for the whole overlap, then a linear
   glide back to the native tempo over a few bars.  On each beat of a
   trusted grid the phases of the bins the beat's transient raises are
   reset to the source's, so kicks land within a millisecond of the time
   map (a plain phase vocoder lets them drift by up to a frame, about
   12 ms, which is enough to flam).  Once the tempo is
   native again the vocoder's phases are steered back onto the source's
   own phases over :data:`_STEER_FRAMES` frames (a frequency offset of a
   fraction of a hertz), after which its output *is* the source, so the
   mix carries on with the decoded audio itself and no seam is left.

The render of the outgoing track plays the overlap; the render of the
incoming track recomputes the same plan (the computation is
deterministic) and keeps everything after the overlap, so the stretch
flows across the render boundary with no repeated or skipped audio.

Pitch is preserved throughout.  librosa is imported lazily, on first
use, like elsewhere in the player.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise

import numpy as np

# STFT frame size and hop of the vocoder (librosa's defaults).
N_FFT = 2048
HOP = 512
# Frames over which the phases return to the source's once the tempo is
# native again: about 0.37 s at 44.1 kHz.
_STEER_FRAMES = 32
# Exact source frames needed on each side of the cut for the inverse STFT
# to reproduce the source there (half a window).
_EDGE_FRAMES = N_FFT // HOP // 2
# Frames of source kept in front of the entry point, so the first frames
# of the overlap are not computed against zero padding.
_PRE_FRAMES = 4
# Shortest tempo ramp, used when no glide is configured: the tempo then
# returns within a tenth of a second, still without a seam.
MIN_RAMP_FRAMES = 8
# Stretches closer to 1.0 than this are left alone.
NO_STRETCH = 0.002
# Downbeat or beat that counts as "on" a moment, in seconds.
ON_BEAT_S = 0.005


def tempo_ratio(out_period: float, in_period: float, max_stretch: float) -> float | None:
    """The stretch that puts the incoming tempo on the outgoing one.

    Half and double time count as the same tempo: the incoming beat may
    be matched to two outgoing beats, or to half of one, whichever needs
    the smallest stretch.

    Args:
        out_period: Seconds per beat of the outgoing track.
        in_period: Seconds per beat of the incoming track.
        max_stretch: Largest allowed ``|ratio - 1|``.

    Returns:
        Output over input duration for the incoming track (above 1.0
        slows it down), or ``None`` when no match is within *max_stretch*
        or a period is not positive.
    """
    if out_period <= 0 or in_period <= 0:
        return None
    ratio = min(
        (out_period * multiple / in_period for multiple in (0.5, 1.0, 2.0)),
        key=lambda r: abs(math.log(r)),
    )
    return ratio if abs(ratio - 1.0) <= max_stretch else None


def local_period(beats: Sequence[float], at_s: float) -> float | None:
    """Seconds per beat of a beat grid around *at_s*, or ``None`` when unreliable.

    Takes the median spacing of the 16 beats around the beat nearest
    *at_s* (fewer near the ends of a short grid).  The grid counts as
    unreliable with fewer than nine beats, or when the spacings vary by
    more than 8 % (median absolute deviation): a beat tracker lost in a
    breakdown, for instance.
    """
    if len(beats) < 9:
        return None
    grid = np.asarray(beats, dtype=np.float64)
    nearest = int(np.argmin(np.abs(grid - at_s)))
    lo = max(0, min(nearest - 8, len(grid) - 17))
    gaps = np.diff(grid[lo : lo + 17])
    period = float(np.median(gaps))
    if period <= 0 or float(np.median(np.abs(gaps - period))) > 0.08 * period:
        return None
    return period


def downbeats(beats: Sequence[float], anchor_s: float | None = None) -> list[float]:
    """Every fourth beat of *beats*: the grid's downbeats.

    Args:
        beats: Ascending beat times in seconds.
        anchor_s: A time known to be a downbeat, such as a first-downbeat
            or intro-start marker set in DJ software.  The beat nearest it
            sets which beats are downbeats; without it (or when it is more
            than a quarter of a beat from every beat) the first beat does.

    Returns:
        The downbeat times, ascending.
    """
    if not beats:
        return []
    first = 0
    if anchor_s is not None and len(beats) > 1:
        grid = np.asarray(beats, dtype=np.float64)
        nearest = int(np.argmin(np.abs(grid - anchor_s)))
        spacing = float(np.median(np.diff(grid)))
        if abs(grid[nearest] - anchor_s) <= spacing / 4:
            first = nearest % 4
    return [float(t) for t in beats[first::4]]


def phase_locked_start(
    out_downbeats: Sequence[float],
    in_downbeats: Sequence[float],
    *,
    entry_s: float,
    ratio: float,
    start_s: float,
    fade_s: float,
    earliest_s: float,
    latest_s: float,
    phrase_starts: Sequence[float] = (),
) -> float | None:
    """Where the fade must start for the two tracks' downbeats to coincide.

    The incoming track enters at *entry_s* (unchanged: never earlier, so
    no more of its intro is cut than without beatmatching) and plays
    stretched by *ratio*, so its first downbeat at or after the entry is
    heard ``lead = (downbeat - entry_s) * ratio`` seconds into the fade.
    The fade then has to start *lead* seconds before an outgoing
    downbeat; this picks the one that moves the fade the least from
    *start_s*.  When *phrase_starts* is given (phrase alignment is on)
    the incoming downbeat lands on a phrase boundary when one fits.

    Args:
        out_downbeats: Outgoing downbeats, in the outgoing track's seconds.
        in_downbeats: Incoming downbeats, in the incoming track's seconds.
        entry_s: Where the incoming track enters, in its own seconds.
        ratio: The incoming track's stretch during the fade.
        start_s: Where the fade would start without phase lock.
        fade_s: Fade length in seconds.
        earliest_s: Earliest allowed fade start (what is already played).
        latest_s: Latest allowed fade start (room for the whole fade).
        phrase_starts: Phrase boundaries to prefer, in the outgoing
            track's seconds.

    Returns:
        The new fade start in seconds, or ``None`` when there is nothing
        to lock: no incoming downbeat inside the fade, or no outgoing
        downbeat that leaves the fade within bounds.
    """
    first = next((t for t in in_downbeats if t >= entry_s - ON_BEAT_S), None)
    if first is None:
        return None
    lead = max(0.0, first - entry_s) * ratio
    if lead >= fade_s:
        return None
    for targets in (phrase_starts, out_downbeats):
        starts = [t - lead for t in targets if earliest_s <= t - lead <= latest_s]
        if starts:
            return min(starts, key=lambda s: abs(s - start_s))
    return None


@dataclass(frozen=True)
class Glide:
    """How the incoming track is stretched from its entry back to its own tempo.

    A pure function of these fields and the decoded audio, so the render
    that plays the overlap and the render that plays the rest compute
    exactly the same audio.

    Attributes:
        entry: Sample of the incoming track where it enters.
        ratio: Output over input duration while the tracks overlap.
        played: Output samples of the overlap (the crossfade length).
        ramp_frames: STFT frames over which the tempo returns to native.
        beats: Source samples of the incoming track's beats (from a
            trusted beat grid), where transients are kept in place; empty
            when the grid is not trusted.
    """

    entry: int
    ratio: float
    played: int
    ramp_frames: int
    beats: tuple[int, ...] = field(default=(), repr=False)

    def _steps(self) -> tuple[np.ndarray, int]:
        """Input frame (from the entry) for each output frame, and the ramp's end.

        The overlap runs at ``1 / ratio`` speed, the ramp moves the speed
        linearly to 1.0, and every frame after it is a whole source frame,
        one per output frame.  The ramp is nudged by under one frame in
        all so that it ends on a whole frame.
        """
        speed = 1.0 / self.ratio
        overlap = -(-self.played // HOP)
        ramp_len = max(MIN_RAMP_FRAMES, self.ramp_frames)
        ramp = speed + (1.0 - speed) * np.arange(1, ramp_len + 1) / (ramp_len + 1)
        reach = overlap * speed + float(ramp.sum())
        landed = round(reach)
        ramp += (landed - reach) / ramp_len
        steady = _STEER_FRAMES + 2 * _EDGE_FRAMES + 1
        moving = np.concatenate([np.full(overlap, speed), ramp])
        steps = np.empty(len(moving) + steady)
        steps[0] = 0.0
        np.cumsum(moving[:-1], out=steps[1 : len(moving)])
        steps[len(moving) :] = landed + np.arange(steady)
        return steps, len(moving)

    @property
    def _cut_frame(self) -> int:
        """Output frame where the mix switches to the decoded source."""
        _, steady_from = self._steps()
        return steady_from + _STEER_FRAMES + _EDGE_FRAMES

    @property
    def cut(self) -> int:
        """Output samples, from the entry, that come from the vocoder."""
        return self._cut_frame * HOP

    @property
    def source_end(self) -> int:
        """Source sample where the decoded audio takes over from the vocoder."""
        steps, _ = self._steps()
        return self.entry + int(steps[self._cut_frame]) * HOP

    @property
    def origin(self) -> int:
        """Where the continuation sits in the source's own timeline.

        The incoming track's own render starts *origin* samples into its
        file: exact from the end of the glide on, and within a fraction
        of a second during it (the glide plays the source a little faster
        or slower than real time).
        """
        return self.source_end - (self.cut - self.played)

    def fits(self, length: int) -> bool:
        """Whether a track of *length* samples is long enough for this plan."""
        steps, _ = self._steps()
        return self.entry + int(steps[-1] + 2) * HOP + N_FFT <= length

    def head(self, audio: np.ndarray, samples: int) -> np.ndarray:
        """The first *samples* output samples, from the entry on (the overlap and on)."""
        cut = self.cut
        if samples > cut:
            rest = audio[self.source_end : self.source_end + samples - cut]
            return np.concatenate([self._vocode(audio, self._cut_frame), rest])[:samples]
        frames = min(self._steps()[0].shape[0], -(-samples // HOP) + _EDGE_FRAMES + 1)
        return self._vocode(audio, frames)[:samples]

    def continuation(self, audio: np.ndarray) -> np.ndarray:
        """Output from the end of the overlap to where the decoded source takes over."""
        return self._vocode(audio, self._cut_frame)[self.played :]

    def _reset_frames(self, steps: np.ndarray, steady_from: int) -> list[int]:
        """Output frames nearest each of :attr:`beats`, while the tempo is moving."""
        moving = steps[: min(steady_from, len(steps))]
        frames: set[int] = set()
        for beat in self.beats:
            at = (beat - self.entry) / HOP
            k = int(np.searchsorted(moving, at))
            if k > 0 and (k == len(moving) or at - moving[k - 1] < moving[k] - at):
                k -= 1
            if 0 < k < len(moving):
                frames.add(k)
        return sorted(frames)

    def _vocode(self, audio: np.ndarray, frames: int) -> np.ndarray:
        """The first ``frames * HOP`` output samples (frames are cut at the plan's end).

        A variable-rate phase vocoder (the method of librosa's
        ``phase_vocoder``, with its rate set per frame).  At each beat of
        :attr:`beats` the phases of the bins whose level jumps there are
        reset to the source's, so a kick or snare lands where the time
        map puts it instead of drifting by up to a frame; once the tempo
        is native the phases are steered back onto the source's.
        """
        import librosa

        steps, steady_from = self._steps()
        pre = min(_PRE_FRAMES, self.entry // HOP)
        needed = min(len(steps), frames + _EDGE_FRAMES + 1)
        times = steps[:needed] + pre
        start = self.entry - pre * HOP
        stop = start + (int(times[-1]) + 2) * HOP + N_FFT
        spectrum = librosa.stft(
            np.ascontiguousarray(audio[start:stop].T), n_fft=N_FFT, hop_length=HOP
        )
        whole = np.floor(times).astype(np.int64)
        frac = (times - whole).astype(np.float32)
        after = np.minimum(whole + 1, spectrum.shape[-1] - 1)
        source_phase = np.angle(spectrum)
        magnitude = np.abs(spectrum)
        del spectrum
        out_mag = magnitude[..., whole] * (1.0 - frac) + magnitude[..., after] * frac
        advance = source_phase[..., after] - source_phase[..., whole]
        phase = np.empty_like(out_mag)
        phase[..., 0] = source_phase[..., whole[0]]
        bounds = [0, *self._reset_frames(steps[:needed], steady_from), needed]
        for begin, end in pairwise(bounds):
            if begin:
                phase[..., begin] = phase[..., begin - 1] + advance[..., begin - 1]
                # The bins a beat's transient raises, in this frame and the
                # two before it (which already overlap the transient).
                rising = (
                    magnitude[..., after[begin]]
                    > 2.0 * magnitude[..., max(0, int(whole[begin]) - 2)] + 1e-4
                )
                for k in range(max(bounds[0] + 1, begin - _EDGE_FRAMES), begin + 1):
                    exact = _phase_between(source_phase, int(whole[k]), float(frac[k]))
                    phase[..., k] = np.where(rising, exact, phase[..., k])
            if end - begin > 1:
                phase[..., begin + 1 : end] = phase[..., begin, None] + np.cumsum(
                    advance[..., begin : end - 1], axis=-1
                )
        del magnitude, advance
        if needed > steady_from:
            target = source_phase[..., whole[steady_from:]]
            drift = np.angle(np.exp(1j * (phase[..., steady_from] - target[..., 0])))
            weight = np.clip(1.0 - np.arange(target.shape[-1]) / _STEER_FRAMES, 0.0, 1.0)
            phase[..., steady_from:] = target + drift[..., None] * weight.astype(np.float32)
        del source_phase
        stretched = np.empty(out_mag.shape, dtype=np.complex64)
        stretched.real = out_mag * np.cos(phase)
        stretched.imag = out_mag * np.sin(phase)
        del out_mag, phase
        out = librosa.istft(stretched, hop_length=HOP, n_fft=N_FFT, length=needed * HOP)
        return np.ascontiguousarray(out.T[: frames * HOP], dtype=np.float32)


def _phase_between(source_phase: np.ndarray, frame: int, frac: float) -> np.ndarray:
    """The source's phase *frac* of the way from *frame* to the next frame.

    Each bin advances by its expected ``2 pi k HOP / N_FFT`` plus the
    measured deviation, so a fractional position is interpolated
    without phase wrapping errors.
    """
    bins = source_phase.shape[-2]
    expected = (2.0 * np.pi * HOP / N_FFT) * np.arange(bins, dtype=np.float32)
    here = source_phase[..., frame]
    following = source_phase[..., min(frame + 1, source_phase.shape[-1] - 1)]
    deviation = following - here - expected
    deviation -= 2.0 * np.pi * np.round(deviation / (2.0 * np.pi))
    return (here + frac * (expected + deviation)).astype(np.float32)


__all__ = [
    "HOP",
    "MIN_RAMP_FRAMES",
    "NO_STRETCH",
    "ON_BEAT_S",
    "Glide",
    "downbeats",
    "local_period",
    "phase_locked_start",
    "tempo_ratio",
]
