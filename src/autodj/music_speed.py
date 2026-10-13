"""Constant-rate, pitch-changing playback for stereo music buffers."""

from __future__ import annotations

import math
import sys
from fractions import Fraction

import numpy as np
from scipy.signal import resample_poly


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
    result = resample_poly(source, ratio.numerator, ratio.denominator, axis=0)
    if len(result) > output_frames:
        result = result[:output_frames]
    elif len(result) < output_frames:
        result = np.pad(result, ((0, output_frames - len(result)), (0, 0)))
    return result.astype(np.float32, copy=False)
