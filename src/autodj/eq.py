"""3-band gain-only EQ (real-time, applied per output chunk).

Kept out of :mod:`autodj.player` so :mod:`autodj.mixbus` can use it
without importing the player.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.signal import butter

from autodj.kernels import compile_kernel

# Band boundaries: low / mid at 250 Hz, mid / high at 4 kHz.
_LOW_CROSSOVER_HZ = 250.0
_HIGH_CROSSOVER_HZ = 4000.0


def make_eq_filters(sample_rate: int) -> dict[str, Any]:
    """Return first-order low-pass and high-pass SOS coefficients.

    The middle band is the input minus these two bands, so the three
    bands reconstruct the original signal exactly at unity gain.

    Args:
        sample_rate: Sample rate in Hz.

    Returns:
        Dict of SOS coefficients.
    """
    nyquist = sample_rate / 2.0
    low_norm = max(1e-4, min(0.99, _LOW_CROSSOVER_HZ / nyquist))
    high_norm = max(1e-4, min(0.99, _HIGH_CROSSOVER_HZ / nyquist))
    return {
        "low": butter(1, low_norm, btype="low", output="sos"),
        "high": butter(1, high_norm, btype="high", output="sos"),
    }


def make_eq_state(sos_filters: dict[str, Any], channels: int = 1) -> dict[str, np.ndarray]:
    """Return zero-initialised filter memory for :func:`apply_eq`.

    One ``zi`` array per band, shaped for the second-order sections that
    :func:`make_eq_filters` produced.  Zeros mean "the stream starts from
    silence", which is what a fresh track does.

    Args:
        sos_filters: Dict from :func:`make_eq_filters`.
        channels: Number of audio channels the state will filter.  ``1``
            (mono, the default) shapes each band's ``zi`` as
            ``(sections, 2)``; ``2`` (stereo) shapes it as
            ``(sections, 2, 2)`` to match ``scipy.signal.sosfilt``'s
            ``axis=0`` convention for ``(frames, channels)`` input.

    Returns:
        Dict of per-band state arrays.
    """
    shape_tail = () if channels == 1 else (channels,)
    return {
        name: np.zeros((np.asarray(sos).shape[0], 2, *shape_tail), dtype=np.float64)
        for name, sos in sos_filters.items()
    }


def reset_eq_state(state: dict[str, np.ndarray]) -> None:
    """Zero the filter memory in *state*, in place.

    Call this when the stream stops. During playback, keep filtering even
    at unity gain so the next slider change uses current filter memory.

    Args:
        state: Dict from :func:`make_eq_state`.
    """
    for band in state.values():
        band.fill(0.0)


def apply_eq(
    chunk: np.ndarray,
    sos_filters: dict[str, Any],
    low_gain: float | np.ndarray,
    mid_gain: float | np.ndarray,
    high_gain: float | np.ndarray,
    state: dict[str, np.ndarray] | None = None,
) -> np.ndarray:
    """Apply a 3-band gain-only EQ to a short audio chunk.

    Splits *chunk* into low / mid / high bands via the filters returned
    by :func:`make_eq_filters`, scales each by its gain, and sums them
    back together.  Called by the mix bus once per 20 ms block.

    Pass *state* from :func:`make_eq_state` when filtering a stream one
    block at a time.  Without it every block restarts each biquad from
    zero, which puts a step discontinuity — an audible zipper — at every
    block boundary as soon as a band leaves unity gain.

    Args:
        chunk: Mono ``(n,)`` or stereo ``(n, 2)`` float32 audio chunk.
        sos_filters: Dict from :func:`make_eq_filters`.
        low_gain: Multiplier for the low band (1.0 = unity, 0.0 = kill).
            Gains may also be per-frame ramps: ``(n,)`` for mono input,
            or ``(n, 1)`` for stereo input.
        mid_gain: Multiplier for the mid band.
        high_gain: Multiplier for the high band.
        state: Per-band filter memory, updated in place.  ``None`` filters
            the chunk as a standalone signal.  Build with
            ``make_eq_state(sos_filters, channels=2)`` for stereo *chunk*.

    Returns:
        EQ-processed float32 chunk, with the same shape. Levels may exceed
        ±1.0; the mix bus's output stage handles limiting or clipping.
    """

    return _run_kernel(
        chunk, sos_filters, state, *_gain_rows(len(chunk), low_gain, mid_gain, high_gain)
    )


def ramp_eq(
    chunk: np.ndarray,
    sos_filters: dict[str, Any],
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    state: dict[str, np.ndarray] | None = None,
) -> np.ndarray:
    """Apply the EQ while moving the gains linearly from *start* to *end*.

    The same as :func:`apply_eq` with per-frame gains
    ``np.linspace(start, end, len(chunk) + 1)[1:]`` (the chunk ends on
    *end*), worked out in compiled code instead: building the ramp with
    NumPy would give up the GIL on the mix bus thread.

    Args:
        chunk: Mono ``(n,)`` or stereo ``(n, 2)`` float32 audio chunk.
        sos_filters: Dict from :func:`make_eq_filters`.
        start: ``(low, mid, high)`` gains just before the chunk.
        end: ``(low, mid, high)`` gains at its last frame.
        state: Per-band filter memory, as for :func:`apply_eq`.

    Returns:
        EQ-processed float32 chunk, with the same shape.
    """
    rows = np.array([start, end], dtype=np.float64)
    return _run_kernel(chunk, sos_filters, state, rows, _RAMP)


# How _eq_kernel reads its gain rows.
_CONSTANT, _PER_FRAME, _RAMP = 0, 1, 2


def _run_kernel(
    chunk: np.ndarray,
    sos_filters: dict[str, Any],
    state: dict[str, np.ndarray] | None,
    gains: np.ndarray,
    mode: int,
) -> np.ndarray:
    audio = np.asarray(chunk, dtype=np.float32)
    frames = audio.reshape(len(audio), -1)
    channels = frames.shape[1]
    if state is None:
        state = make_eq_state(sos_filters, channels=channels)
    out = np.empty_like(frames)
    _eq_kernel(
        frames,
        out,
        np.asarray(sos_filters["low"], dtype=np.float64),
        state["low"].reshape(-1, 2, channels),
        np.asarray(sos_filters["high"], dtype=np.float64),
        state["high"].reshape(-1, 2, channels),
        gains,
        mode,
    )
    return out.reshape(audio.shape)


def _gain_rows(
    frames: int,
    low: float | np.ndarray,
    mid: float | np.ndarray,
    high: float | np.ndarray,
) -> tuple[np.ndarray, int]:
    """Return the gains as ``(1, 3)`` constant or ``(frames, 3)`` per-frame rows."""
    if all(np.ndim(gain) == 0 for gain in (low, mid, high)):
        return np.array([[low, mid, high]], dtype=np.float64), _CONSTANT
    columns = [
        np.broadcast_to(np.asarray(g, np.float64).reshape(-1), (frames,)) for g in (low, mid, high)
    ]
    return np.ascontiguousarray(np.stack(columns, axis=1)), _PER_FRAME


@compile_kernel(
    "void(float32[:, :], float32[:, :], float64[:, :], float64[:, :, :],"
    " float64[:, :], float64[:, :, :], float64[:, :], int64)"
)
def _eq_kernel(  # pragma: no cover - compiled; covered through apply_eq
    chunk: np.ndarray,
    out: np.ndarray,
    low_sos: np.ndarray,
    low_state: np.ndarray,
    high_sos: np.ndarray,
    high_state: np.ndarray,
    gains: np.ndarray,
    mode: int,
) -> None:
    """Split, scale and sum one chunk in compiled code, updating the filter state.

    Each band runs the same transposed direct-form II second-order
    sections as ``scipy.signal.sosfilt``, in float64, so the result and
    the carried state match it.  Running compiled with the GIL held means
    a block costs microseconds and never waits to take the GIL back from
    another busy Python thread (``sosfilt`` gives it up on every call).

    *mode* says how to read *gains*: ``_CONSTANT`` uses row 0 throughout,
    ``_PER_FRAME`` has one row per frame, and ``_RAMP`` steps from row 0
    to row 1 exactly as ``np.linspace(row0, row1, n + 1)[1:]`` does.
    """
    n = chunk.shape[0]
    current = np.empty(3)
    step = np.empty(3)
    delta = np.empty(3)
    flat = False
    if mode == _RAMP:
        for band in range(3):
            delta[band] = gains[1, band] - gains[0, band]
            step[band] = delta[band] / n
            flat = flat or step[band] == 0
    for channel in range(chunk.shape[1]):
        for i in range(chunk.shape[0]):
            x = np.float64(chunk[i, channel])
            low = x
            for s in range(low_sos.shape[0]):
                y = low_sos[s, 0] * low + low_state[s, 0, channel]
                low_state[s, 0, channel] = (
                    low_sos[s, 1] * low - low_sos[s, 4] * y + low_state[s, 1, channel]
                )
                low_state[s, 1, channel] = low_sos[s, 2] * low - low_sos[s, 5] * y
                low = y
            high = x
            for s in range(high_sos.shape[0]):
                y = high_sos[s, 0] * high + high_state[s, 0, channel]
                high_state[s, 0, channel] = (
                    high_sos[s, 1] * high - high_sos[s, 4] * y + high_state[s, 1, channel]
                )
                high_state[s, 1, channel] = high_sos[s, 2] * high - high_sos[s, 5] * y
                high = y
            if mode == _RAMP:
                for band in range(3):
                    if i == n - 1:
                        current[band] = gains[1, band]
                    elif flat:  # NumPy's form when any band's step is zero
                        current[band] = (i + 1) / n * delta[band] + gains[0, band]
                    else:
                        current[band] = (i + 1) * step[band] + gains[0, band]
            else:
                row = i if mode == _PER_FRAME else 0
                for band in range(3):
                    current[band] = gains[row, band]
            low_gain, mid_gain, high_gain = current[0], current[1], current[2]
            out[i, channel] = np.float32(
                x * mid_gain + low * (low_gain - mid_gain) + high * (high_gain - mid_gain)
            )
