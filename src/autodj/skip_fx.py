"""DJ-style skips for the server mix (``[playback] skip_style``).

A skip on the mix bus normally fades the playing track out over 150 ms
(``"fade"``).  The other styles play a short effect on the playing audio
instead, then cut to the next track exactly as the fade does:

- ``"echo_out"``: the music stops and its echoes ring out and fade.
- ``"backspin"``: the record is spun backwards and runs down.
- ``"loop_roll"``: a short loop repeats, rolling faster as it fades.

The effect lasts one to two beats, at most :data:`MAX_TAIL_S`.  With a
trusted tempo and a beat grid it ends on a beat; with a trusted tempo
alone it lasts two beats; otherwise it lasts :data:`UNTIMED_TAIL_S`.

:func:`render_tail` is pure numpy over a ``(frames, 2)`` render and is
called from the thread that asked for the skip, never from the mix bus's
real-time thread (see :meth:`autodj.mixbus.MixBus.skip`).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from autodj.config import SKIP_STYLES
from autodj.stereo import SAMPLE_RATE

#: Longest a skip effect lasts, in seconds.
MAX_TAIL_S = 2.0
#: How long a skip effect lasts when the tempo is not trusted, in seconds.
UNTIMED_TAIL_S = 1.0
#: Beat period assumed for sizing echoes and loops without a tempo (120 BPM).
_DEFAULT_BEAT_S = 0.5
#: Frames over which the effect takes over from the dry audio, so it never clicks.
BLEND_FRAMES = int(0.01 * SAMPLE_RATE)
#: Shortest effect worth playing, in seconds; a beat that ends sooner is skipped.
_MIN_TAIL_S = 0.25


def tail_frames(start: int, bpm: float, beats: Sequence[int]) -> int:
    """How long the effect starting at frame *start* lasts, in frames.

    Args:
        start: Frame of the render where the effect starts.
        bpm: The playing track's trusted tempo, ``0`` when not trusted.
        beats: Beat positions as render frames, ascending; may be empty.

    Returns:
        A length of at most :data:`MAX_TAIL_S`.
    """
    cap = int(MAX_TAIL_S * SAMPLE_RATE)
    if bpm <= 0:
        return int(UNTIMED_TAIL_S * SAMPLE_RATE)
    beat = 60.0 / bpm * SAMPLE_RATE
    floor = start + max(beat, _MIN_TAIL_S * SAMPLE_RATE)
    on_beat = [b for b in beats if floor <= b <= start + cap]
    if on_beat:
        return int(on_beat[0] - start)
    return min(cap, int(2 * beat))


def _fade_out(frames: int) -> np.ndarray:
    """A ``(frames, 1)`` gain falling from 1 to 0 along an eased curve."""
    t = np.linspace(0.0, 1.0, frames, endpoint=False, dtype=np.float32)
    return (np.cos(t * np.pi / 2.0) ** 2)[:, None]


def _source(audio: np.ndarray, start: int, frames: int) -> np.ndarray:
    """*frames* of *audio* from *start*, padded with silence past its end."""
    out = np.zeros((frames, 2), np.float32)
    piece = audio[start : start + frames]
    out[: len(piece)] = piece
    return out


def _echo_out(audio: np.ndarray, start: int, frames: int, beat: int) -> np.ndarray:
    """Up to a beat of music, cut, with its echoes every half beat."""
    gate = min(max(BLEND_FRAMES * 2, frames // 2), beat)
    dry = _source(audio, start, gate)
    ramp = min(BLEND_FRAMES, gate)
    dry[gate - ramp :] *= np.linspace(1.0, 0.0, ramp, dtype=np.float32)[:, None]
    out = np.zeros((frames, 2), np.float32)
    delay = max(1, beat // 2)
    gain = 1.0
    at = 0
    while at < frames and gain > 0.01:
        n = min(gate, frames - at)
        out[at : at + n] += dry[:n] * gain
        at += delay
        gain *= 0.5
    return out


def _backspin(audio: np.ndarray, start: int, frames: int) -> np.ndarray:
    """Play backwards from *start*, fast at first, slowing to a stop."""
    if len(audio) == 0:
        return np.zeros((frames, 2), np.float32)
    t = np.arange(frames, dtype=np.float64) / frames
    speed = -3.0 * (1.0 - t) ** 2
    travel = float(-speed.sum())
    if travel > start:  # cannot spin back past the start of the render
        speed *= start / travel
    pos = np.clip(start + np.cumsum(speed), 0.0, len(audio) - 1)
    # Only the stretch of record the spin passes over is interpolated.
    lo = int(pos.min())
    hi = min(len(audio), int(np.ceil(pos.max())) + 2)
    index = np.arange(lo, hi)
    window = audio[lo:hi]
    return np.stack([np.interp(pos, index, window[:, ch]) for ch in range(2)], axis=1).astype(
        np.float32
    )


def _loop_roll(audio: np.ndarray, start: int, frames: int, beat: int) -> np.ndarray:
    """Repeat a loop from *start* that halves in length every quarter."""
    out = np.zeros((frames, 2), np.float32)
    edge = max(1, BLEND_FRAMES // 4)
    at = 0
    quarter = max(1, frames // 4)
    while at < frames:
        stage = min(3, at // quarter)
        size = max(4 * edge, beat // (2 << stage))
        n = min(size, frames - at)
        piece = _source(audio, start, n)
        ramp = min(edge, n // 2)
        if ramp:
            up = np.linspace(0.0, 1.0, ramp, dtype=np.float32)[:, None]
            piece[:ramp] *= up
            piece[n - ramp :] *= up[::-1]
        out[at : at + n] = piece
        at += n
    return out


def render_tail(
    style: str, audio: np.ndarray, start: int, frames: int, beat_frames: int = 0
) -> np.ndarray:
    """Return the effect *style* played on *audio* from frame *start*.

    The first :data:`BLEND_FRAMES` cross from the dry audio into the
    effect and the whole tail fades to silence by its end, so it joins
    the music it replaces without a click and ends silent.

    Args:
        style: One of :data:`SKIP_STYLES` other than ``"fade"``.
        audio: The playing render, ``(n, 2)`` float32.
        start: Frame of *audio* where the effect takes over.
        frames: Length of the effect (:func:`tail_frames`).
        beat_frames: The beat period in frames, ``0`` when unknown.

    Returns:
        ``(frames, 2)`` float32 audio.

    Raises:
        ValueError: If *style* is not an effect style.
    """
    beat = beat_frames if beat_frames > 0 else int(_DEFAULT_BEAT_S * SAMPLE_RATE)
    if style == "echo_out":
        wet = _echo_out(audio, start, frames, beat)
    elif style == "backspin":
        wet = _backspin(audio, start, frames)
    elif style == "loop_roll":
        wet = _loop_roll(audio, start, frames, beat)
    else:
        raise ValueError(f"not a skip effect: {style!r}")
    wet *= _fade_out(frames)
    blend = min(BLEND_FRAMES, frames)
    mix = np.linspace(0.0, 1.0, blend, dtype=np.float32)[:, None]
    dry = _source(audio, start, blend)
    wet[:blend] = dry * (1.0 - mix) + wet[:blend] * mix
    return np.clip(wet, -1.0, 1.0).astype(np.float32, copy=False)


__all__ = [
    "BLEND_FRAMES",
    "MAX_TAIL_S",
    "SKIP_STYLES",
    "UNTIMED_TAIL_S",
    "render_tail",
    "tail_frames",
]
