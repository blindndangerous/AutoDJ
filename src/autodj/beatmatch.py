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
4. With ``[djmix] key_shift`` the incoming track also plays a semitone or
   two higher or lower while the tracks overlap (:attr:`Glide.semitones`)
   and slides back to its own key over the same glide.  The vocoder then
   reads a resampled copy of the track (:class:`_KeyMap`): played faster
   by the pitch factor, which raises the pitch and the tempo together,
   then stretched back by the same factor, so only the pitch moves.  The
   time map from the output to the track is unchanged, so beats, phase
   lock and the render boundary stay where they were.  The copy is
   offset from the track by a whole number of frames once the pitch is
   home, so the steering in step 3 lands on the decoded audio as before.

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
from numpy.lib.stride_tricks import sliding_window_view

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
# Largest key shift, in semitones.
MAX_SEMITONES = 2
# Shortest glide after a key shift, in frames (4 s at 44.1 kHz): the pitch
# slides back over at least this long.
KEY_RAMP_FRAMES = 344
# The windowed-sinc resampler of a key shift: taps on each side, phase
# steps a sample, and samples read with one cutoff.
_TAPS = 8
_PHASES = 4096
_CHUNK = 1 << 15


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


def drop_landing(
    out_beats: Sequence[float],
    anchors: Sequence[float],
    *,
    phrase_beats: int,
    overlap_s: float,
    target_s: float,
    earliest_s: float,
    latest_s: float,
) -> float | None:
    """Where in the outgoing track the incoming drop should land, for a drop mix.

    Dance tracks are built in phrases counted from their drops and
    breakdowns, so the landing is a whole number of phrases (at least
    one) after one of *anchors*, counted in beats of the outgoing grid:
    the start of a section, where the outgoing drop or breakdown has
    run its course.  The overlap before it must start after the anchor,
    so the outgoing drop is heard in full.  Of the landings in bounds,
    the one nearest *target_s* (where the fade would end without drop
    mixing) wins.

    Args:
        out_beats: The outgoing beat grid, in seconds.
        anchors: Its drops and breakdowns (:func:`autodj.dj_meta.mix_anchors`).
        phrase_beats: Beats in a phrase.
        overlap_s: Length of the overlap that ends on the landing.
        target_s: Where the fade would end otherwise.
        earliest_s: Earliest allowed landing.
        latest_s: Latest allowed landing.

    Returns:
        The landing in seconds, or ``None`` when no landing is in bounds or
        no anchor sits on the grid (within a quarter of a beat).
    """
    if len(out_beats) < 2 or phrase_beats < 1:
        return None
    grid = np.asarray(out_beats, dtype=np.float64)
    spacing = float(np.median(np.diff(grid)))
    landings: list[float] = []
    for anchor in anchors:
        nearest = int(np.argmin(np.abs(grid - anchor)))
        if abs(grid[nearest] - anchor) > spacing / 4:
            continue
        for land in grid[nearest + phrase_beats :: phrase_beats]:
            if earliest_s <= land <= latest_s and land - overlap_s >= anchor:
                landings.append(float(land))
    return min(landings, key=lambda t: abs(t - target_s)) if landings else None


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
        semitones: Pitch shift of the incoming track while the tracks
            overlap, at most :data:`MAX_SEMITONES` either way; it slides
            back to 0 over the glide, which then lasts at least
            :data:`KEY_RAMP_FRAMES`.  0 for none.
    """

    entry: int
    ratio: float
    played: int
    ramp_frames: int
    beats: tuple[int, ...] = field(default=(), repr=False)
    semitones: float = 0.0

    def _steps(self) -> tuple[np.ndarray, int]:
        """Input frame (from the entry) for each output frame, and the ramp's end.

        The overlap runs at ``1 / ratio`` speed, the ramp moves the speed
        linearly to 1.0, and every frame after it is a whole source frame,
        one per output frame.  The ramp is nudged by under one frame in
        all so that it ends on a whole frame.
        """
        speed = 1.0 / self.ratio
        overlap = -(-self.played // HOP)
        ramp_len = max(KEY_RAMP_FRAMES if self.semitones else MIN_RAMP_FRAMES, self.ramp_frames)
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

    def _key_map(self, steps: np.ndarray, steady_from: int) -> _KeyMap | None:
        """The resampled copy a key shift reads, or ``None`` without one."""
        if not self.semitones:
            return None
        overlap = -(-self.played // HOP)
        return _KeyMap.build(
            self.semitones, float(steps[overlap]) * HOP, float(steps[steady_from]) * HOP
        )

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
        key_map = self._key_map(steps, steady_from)
        # Frames read from the track, or from its resampled copy for a key shift.
        reads = steps if key_map is None else key_map.reads(steps)
        pre = min(_PRE_FRAMES, self.entry // HOP)
        needed = min(len(steps), frames + _EDGE_FRAMES + 1)
        times = reads[:needed] + pre
        start = -pre * HOP
        stop = start + (int(times[-1]) + 2) * HOP + N_FFT
        if key_map is None:
            segment = audio[self.entry + start : self.entry + stop]
        else:
            segment = key_map.signal(audio, self.entry, start, stop)
        spectrum = librosa.stft(np.ascontiguousarray(segment.T), n_fft=N_FFT, hop_length=HOP)
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
        # A key shift stretches by up to a fifth, where bins drifting apart
        # around a partial thin it out: their phases stay locked to the peaks.
        lock = None if key_map is None else _PeakLock.of(out_mag, source_phase[..., whole])
        bounds = [0, *self._reset_frames(steps[:needed], steady_from), needed]
        for begin, end in pairwise(bounds):
            if begin:
                phase[..., begin] = phase[..., begin - 1] + advance[..., begin - 1]
                if lock is not None:
                    phase[..., begin] = lock.apply(phase[..., begin], begin)
                # The bins a beat's transient raises, in this frame and the
                # two before it (which already overlap the transient).
                rising = (
                    magnitude[..., after[begin]]
                    > 2.0 * magnitude[..., max(0, int(whole[begin]) - 2)] + 1e-4
                )
                for k in range(max(bounds[0] + 1, begin - _EDGE_FRAMES), begin + 1):
                    exact = _phase_between(source_phase, int(whole[k]), float(frac[k]))
                    phase[..., k] = np.where(rising, exact, phase[..., k])
            if end - begin > 1 and lock is not None:
                for k in range(begin + 1, end):
                    phase[..., k] = lock.apply(phase[..., k - 1] + advance[..., k - 1], k)
            elif end - begin > 1:
                phase[..., begin + 1 : end] = phase[..., begin, None] + np.cumsum(
                    advance[..., begin : end - 1], axis=-1
                )
        del magnitude, advance, lock
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


@dataclass(frozen=True)
class _KeyMap:
    """Where a key shift's resampled copy of the track reads the track.

    Positions are in samples from the entry: *v* in the track, *u* in the
    copy.  The pitch factor ``P(v)`` is the shift for the whole overlap
    (up to *ramp_from*), slides back to 1.0 in a straight line in
    semitones until *ramp_to*, and is 1.0 from there on.  The copy plays
    the track ``P`` times as fast, so ``du/dv = 1 / P(v) = 1 - g(v)`` and
    ``u(v) = v - G(v)`` with ``G`` the integral of ``g``.  Once the pitch
    is home the copy is the track delayed by :attr:`offset` samples.  A
    small bump in ``g`` over the slide (zero at both of its ends, under a
    twentieth of a semitone) makes that a whole number of frames.

    Attributes:
        semitones: The shift over the overlap.
        ramp_from: Track position where the pitch starts back.
        ramp_to: Track position where it is home.
        bump: Height of the correction bump in ``g``.
        offset: ``v - u`` once the pitch is home, a multiple of :data:`HOP`.
    """

    semitones: float
    ramp_from: float
    ramp_to: float
    bump: float
    offset: int

    @classmethod
    def build(cls, semitones: float, ramp_from: float, ramp_to: float) -> _KeyMap:
        """The map for *semitones*, sliding home between the two track positions."""
        g0 = 1.0 - 2.0 ** (-semitones / 12.0)
        span = ramp_to - ramp_from
        rate = semitones * math.log(2.0) / (12.0 * span)
        slide = span - (1.0 - math.exp(-rate * span)) / rate
        natural = g0 * ramp_from + slide
        offset = HOP * round(natural / HOP)
        return cls(semitones, ramp_from, ramp_to, (offset - natural) / (span / 2.0), offset)

    @property
    def _g0(self) -> float:
        return 1.0 - 2.0 ** (-self.semitones / 12.0)

    @property
    def _rate(self) -> float:
        return self.semitones * math.log(2.0) / (12.0 * (self.ramp_to - self.ramp_from))

    def _g(self, v: np.ndarray) -> np.ndarray:
        """``1 - 1 / P`` at track positions *v*."""
        span = self.ramp_to - self.ramp_from
        t = np.clip((v - self.ramp_from) / span, 0.0, 1.0)
        sliding = 1.0 - np.exp(-self._rate * span * (1.0 - t))
        return sliding + self.bump * np.sin(np.pi * t) ** 2

    def _u(self, v: np.ndarray) -> np.ndarray:
        """Copy positions of track positions *v*."""
        span = self.ramp_to - self.ramp_from
        rate = self._rate
        t = np.clip((v - self.ramp_from) / span, 0.0, 1.0)
        into = t * span
        slid = into - (np.exp(-rate * span * (1.0 - t)) - math.exp(-rate * span)) / rate
        bumped = self.bump * (into / 2.0 - span * np.sin(2.0 * np.pi * t) / (4.0 * np.pi))
        held = self._g0 * np.minimum(v, self.ramp_from)
        return np.where(v >= self.ramp_to, v - self.offset, v - held - slid - bumped)

    def reads(self, steps: np.ndarray) -> np.ndarray:
        """The copy's frame for each of the glide's track frames *steps*."""
        return self._u(steps * float(HOP)) / HOP

    def _track_positions(self, u: np.ndarray) -> np.ndarray:
        """Track positions the copy positions *u* (ascending) read.

        Solved by Newton's method every :data:`_MAP_STEP` samples and
        interpolated in between: ``u(v)`` bends so slowly that the
        straight lines are within a thousandth of a sample of it.
        """
        held_end = self.ramp_from * (1.0 - self._g0)
        home = self.ramp_to - self.offset
        coarse = np.append(u[::_MAP_STEP], u[-1])
        guess = self.ramp_from + (coarse - held_end) * (self.ramp_to - self.ramp_from) / (
            home - held_end
        )
        for _ in range(4):
            guess = guess - (self._u(guess) - coarse) / (1.0 - self._g(guess))
        solved = np.where(
            coarse <= held_end,
            coarse / (1.0 - self._g0),
            np.where(coarse >= home, coarse + self.offset, guess),
        )
        return np.interp(u, coarse, solved)

    def signal(self, audio: np.ndarray, entry: int, start: int, stop: int) -> np.ndarray:
        """Samples *start* to *stop* of the copy (from the entry), as a track segment.

        Read through a windowed sinc low-passed below the copy's Nyquist
        where it plays the track faster; from where the pitch is home on,
        the samples are the track's own.
        """
        home = round(self.ramp_to) - self.offset
        split = min(max(start, home), stop)
        parts = []
        if split > start:
            v = self._track_positions(np.arange(start, split, dtype=np.float64))
            scale = np.minimum(1.0, 1.0 - self._g(v))
            parts.append(_read(audio, entry + v, scale))
        if stop > split:
            lo = entry + split + self.offset
            parts.append(_padded(audio, lo, lo + stop - split))
        return np.concatenate(parts).astype(np.float32, copy=False)


# Copy positions between two exact solutions of a key shift's map.
_MAP_STEP = 32


def _padded(audio: np.ndarray, lo: int, hi: int) -> np.ndarray:
    """``audio[lo:hi]``, with silence for whatever lies outside the track."""
    inside = audio[max(0, lo) : max(0, min(hi, len(audio)))]
    before = min(max(0, -lo), hi - lo)
    after = hi - lo - before - len(inside)
    if not before and not after:
        return inside
    pad = (*audio.shape[1:],)
    return np.concatenate(
        [np.zeros((before, *pad), audio.dtype), inside, np.zeros((after, *pad), audio.dtype)]
    )


def _read(audio: np.ndarray, positions: np.ndarray, scale: np.ndarray) -> np.ndarray:
    """*audio* at ascending fractional *positions*, low-passed to *scale* times its Nyquist.

    A Hann-windowed sinc over :data:`_TAPS` samples each side, its phase
    rounded to :data:`_PHASES` steps a sample and its cutoff held for
    :data:`_CHUNK` samples at a time (the cutoff moves over seconds);
    samples outside the track count as silence.  At a whole position with
    *scale* 1.0 it returns the sample itself.
    """
    frames = audio.reshape(len(audio), -1)
    base = np.floor(positions).astype(np.int64)
    phase = np.rint((positions - base) * _PHASES).astype(np.int64)
    source = _padded(frames, int(base[0]) - _TAPS + 1, int(base[-1]) + _TAPS + 1)
    windows = sliding_window_view(source, 2 * _TAPS, axis=0)
    lead = base - base[0]
    out = np.empty((len(positions), frames.shape[1]), dtype=np.float32)
    kernels: dict[float, np.ndarray] = {}
    for lo in range(0, len(positions), _CHUNK):
        part = slice(lo, lo + _CHUNK)
        cutoff = round(float(scale[lo]), 3)
        if cutoff not in kernels:
            kernels[cutoff] = _kernel(cutoff)
        weights = kernels[cutoff][phase[part]]
        out[part] = np.matmul(windows[lead[part]], weights[:, :, None])[..., 0]
    return out.reshape(len(positions), *audio.shape[1:])


def _kernel(scale: float) -> np.ndarray:
    """Tap weights of :func:`_read` for each phase step, ``(_PHASES + 1, 2 * _TAPS)``."""
    dist = (np.arange(_PHASES + 1) / _PHASES)[:, None] + (_TAPS - 1) - np.arange(2 * _TAPS)
    window = 0.5 + 0.5 * np.cos(np.pi * dist / _TAPS)
    return np.asarray(scale * np.sinc(scale * dist) * window, dtype=np.float32)


@dataclass(frozen=True)
class _PeakLock:
    """Identity phase locking (Laroche and Dolson) for the frames of a key shift.

    Each bin belongs to the spectral peak nearest it in its frame; only
    the peaks' phases advance on their own, and every other bin keeps the
    phase offset from its peak that the source has there, so the bins
    around a partial stay one partial however far it is stretched.

    Attributes:
        peaks: For each bin and output frame, its peak's bin, ``(..., bins, frames)``.
        offsets: The source's phase of each bin minus its peak's, same shape.
    """

    peaks: np.ndarray
    offsets: np.ndarray

    @classmethod
    def of(cls, magnitude: np.ndarray, source_phase: np.ndarray) -> _PeakLock:
        """The lock for output *magnitude* and the source phases read for each frame."""
        bins = magnitude.shape[-2]
        # Bin numbers fit 16 bits, which halves the memory the scans walk.
        index = np.arange(bins, dtype=np.int16).reshape(bins, 1)
        is_peak = np.zeros(magnitude.shape, dtype=bool)
        is_peak[..., 1:-1, :] = (magnitude[..., 1:-1, :] >= magnitude[..., :-2, :]) & (
            magnitude[..., 1:-1, :] > magnitude[..., 2:, :]
        )
        none_below, none_above = np.int16(-bins), np.int16(2 * bins)
        below = np.maximum.accumulate(np.where(is_peak, index, none_below), axis=-2)
        above = np.flip(
            np.minimum.accumulate(np.flip(np.where(is_peak, index, none_above), -2), axis=-2), -2
        )
        nearer = np.where(index - below <= above - index, below, above)
        peaks = np.where((nearer >= 0) & (nearer < bins), nearer, index)
        offsets = source_phase - np.take_along_axis(source_phase, peaks, axis=-2)
        return cls(peaks, offsets.astype(np.float32))

    def apply(self, phases: np.ndarray, frame: int) -> np.ndarray:
        """Frame *frame*'s *phases* (``(..., bins)``) with every bin locked to its peak."""
        peaks = self.peaks[..., frame]
        return np.take_along_axis(phases, peaks, axis=-1) + self.offsets[..., frame]


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
    "KEY_RAMP_FRAMES",
    "MAX_SEMITONES",
    "MIN_RAMP_FRAMES",
    "NO_STRETCH",
    "ON_BEAT_S",
    "Glide",
    "downbeats",
    "drop_landing",
    "local_period",
    "phase_locked_start",
    "tempo_ratio",
]
