"""3-band gain-only EQ (real-time, applied per output chunk).

Kept out of :mod:`autodj.player` so :mod:`autodj.mixbus` can use it
without importing the player.
"""

from __future__ import annotations

from typing import Any, cast

import numpy as np
from scipy.signal import butter, sosfilt

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

    def _filter(band: str, signal: np.ndarray) -> np.ndarray:
        if state is None or band not in state:
            return cast(np.ndarray, sosfilt(sos_filters[band], signal, axis=0))
        filtered, state[band] = sosfilt(sos_filters[band], signal, axis=0, zi=state[band])
        return cast(np.ndarray, filtered)

    low = _filter("low", chunk)
    high = _filter("high", chunk)
    return (chunk * mid_gain + low * (low_gain - mid_gain) + high * (high_gain - mid_gain)).astype(
        np.float32
    )
