"""Compiled per-block audio helpers for the real-time playout threads.

NumPy gives up the GIL for any operation on more than a few hundred
samples, and getting it back from a thread that is busy running Python
takes a whole switch interval (5 ms by default).  A mix bus block lasts
20 ms, so a handful of such operations per block were enough to make
the bus fall behind whenever the library scan, a track pick or a state
push was running.  These helpers do the same work in numba-compiled
loops, which keep the GIL for the few microseconds they take.

Every kernel is compiled for one fixed signature when this module is
imported (at startup), so the first audio block never waits for the
compiler; the machine code is cached on disk when a cache directory is
writable.
"""

from __future__ import annotations

from typing import Any

import numba
import numpy as np

_FLOAT32_MAX = float(np.finfo(np.float32).max)


def compile_kernel(signature: str) -> Any:
    """Return a decorator compiling a function eagerly for *signature*.

    The on-disk cache needs a writable ``__pycache__`` beside this file or
    a user cache directory (the container sets ``XDG_CACHE_HOME``); when
    there is neither, compile without it rather than fail the import.
    """
    cached = numba.njit(signature, cache=True)

    def decorate(function: Any) -> Any:
        try:
            return cached(function)
        except RuntimeError as exc:
            if "no locator available" not in str(exc).lower():
                raise
            return numba.njit(signature)(function)

    return decorate


@compile_kernel("void(float32[:, :])")
def nan_to_num_in_place(block: np.ndarray) -> None:  # pragma: no cover - compiled
    """``np.nan_to_num(block, copy=False)`` for float32 audio, keeping the GIL.

    NaN becomes 0 and an infinity the largest finite float32 of its sign.
    """
    for i in range(block.shape[0]):
        for channel in range(block.shape[1]):
            x = block[i, channel]
            if x != x:
                block[i, channel] = 0.0
            elif x > _FLOAT32_MAX:
                block[i, channel] = _FLOAT32_MAX
            elif x < -_FLOAT32_MAX:
                block[i, channel] = -_FLOAT32_MAX


@compile_kernel("void(float32[:, :], float64, float64, int64, float64, float32[:, :], int64)")
def duck_and_mix_in_place(  # pragma: no cover - compiled
    block: np.ndarray,
    gain: float,
    step: float,
    ramp_frames: int,
    target: float,
    liner: np.ndarray,
    liner_pos: int,
) -> None:
    """Scale *block* by the duck gain, then add the liner's next frames.

    Frame ``i`` is scaled by ``float32(gain + step * (i + 1))`` for the
    first *ramp_frames* frames and by ``float32(target)`` after them, as
    the mix bus's duck ramp always has; ``liner[liner_pos:]`` is then
    added from the first frame for as long as it lasts.
    """
    frames = block.shape[0]
    held = np.float32(target)
    for i in range(frames):
        duck = np.float32(gain + step * (i + 1)) if i < ramp_frames else held
        for channel in range(block.shape[1]):
            block[i, channel] = block[i, channel] * duck
    left = min(frames, liner.shape[0] - liner_pos)
    for i in range(max(0, left)):
        for channel in range(block.shape[1]):
            block[i, channel] = block[i, channel] + liner[liner_pos + i, channel]
