"""Constant-rate, pitch-changing playback for stereo music buffers."""

from __future__ import annotations

import math
import sys
from fractions import Fraction

import numpy as np
from scipy.signal import resample_poly

from autodj.stereo import SAMPLE_RATE, SPILL_FRAMES, audio_buffer

#: A long track is resampled this many seconds at a time, each block with
#: :data:`_PAD_FRAMES` of its neighbours so the filter sees what it would
#: in one pass.  The filter reaches about ten samples either side here.
_BLOCK_SECONDS = 60
_PAD_FRAMES = 4096


def resample_music(audio: np.ndarray, rate: float) -> np.ndarray:
    """Change the playback rate of stereo audio without preserving pitch.

    A rate greater than one makes the audio shorter and raises its pitch; a
    rate below one makes it longer and lowers its pitch. The returned frame
    count is ``round(len(audio) / rate)``. Inputs and outputs use frame-major
    ``(frames, 2)`` stereo layout; the output dtype is always ``float32``.

    At rate one, an existing float32 array is returned unchanged.
    """
    if not isinstance(audio, np.ndarray) or audio.ndim != 2 or audio.shape[1] != 2:
        raise ValueError("audio must be a stereo (frames, 2) NumPy array")
    if isinstance(rate, bool) or not isinstance(rate, int | float) or not math.isfinite(rate):
        raise ValueError("rate must be a finite positive number")
    rate = float(rate)
    if rate <= 0:
        raise ValueError("rate must be a finite positive number")
    if rate == 1 and audio.dtype == np.float32:
        return audio

    output_frames_float = len(audio) / rate
    if not math.isfinite(output_frames_float) or output_frames_float > sys.maxsize:
        raise ValueError("rate requests an unrepresentable output length")
    output_frames = round(output_frames_float)
    if output_frames == 0:
        return np.empty((0, 2), dtype=np.float32)
    if len(audio) == 0:
        return np.empty((0, 2), dtype=np.float32)

    # resample_poly's output/input ratio is 1/rate. Limiting the denominator
    # gives practical rational factors while keeping common decimal rates exact.
    ratio = Fraction(1.0 / rate).limit_denominator(10_000)
    source = audio.astype(np.float32, copy=False)
    if len(source) > SPILL_FRAMES:
        return _resample_long(source, ratio.numerator, ratio.denominator, output_frames)
    result = resample_poly(source, ratio.numerator, ratio.denominator, axis=0)
    if len(result) > output_frames:
        result = result[:output_frames]
    elif len(result) < output_frames:
        result = np.pad(result, ((0, output_frames - len(result)), (0, 0)))
    return result.astype(np.float32, copy=False)


def _resample_long(source: np.ndarray, up: int, down: int, output_frames: int) -> np.ndarray:
    """:func:`resample_poly` a block at a time into :func:`~autodj.stereo.audio_buffer`.

    Each block starts on a multiple of *down* input frames, so its output
    starts on a whole output frame, and is padded with its neighbours'
    frames, which are then cut off: the same samples as one pass, without
    the several whole-track copies one pass makes.
    """
    out = audio_buffer(output_frames)
    block = down * max(1, round(_BLOCK_SECONDS * SAMPLE_RATE / down))
    pad = down * math.ceil(_PAD_FRAMES / down)
    written = 0
    for start in range(0, len(source), block):
        if written >= output_frames:
            break
        first = max(0, start - pad)
        piece = resample_poly(
            source[first : min(len(source), start + block + pad)], up, down, axis=0
        )
        lead = (start - first) * up // down
        take = min(output_frames - written, -(-min(block, len(source) - start) * up // down))
        chunk = piece[lead : lead + take]
        out[written : written + len(chunk)] = chunk
        written += len(chunk)
    return out
