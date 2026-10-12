"""Compiled audio helpers match the NumPy and SciPy code they replace."""

from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import sosfilt

import autodj.kernels as kernels
from autodj.eq import apply_eq, make_eq_filters, make_eq_state, ramp_eq
from autodj.kernels import duck_and_mix_in_place, nan_to_num_in_place


def _noise(frames: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.uniform(-1.0, 1.0, (frames, 2)).astype(np.float32)


def test_compile_kernel_retries_without_cache_at_decoration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    njit = kernels.numba.njit

    def without_cache(signature: str):
        return njit(signature)

    def fail_cached(signature: str, *, cache: bool = False):
        if cache:

            def decorate(function):
                raise RuntimeError("cannot cache function: no locator available for file")

            return decorate
        return without_cache(signature)

    monkeypatch.setattr(kernels.numba, "njit", fail_cached)

    @kernels.compile_kernel("int64(int64)")
    def increment(value: int) -> int:
        return value + 1

    assert increment(4) == 5
    assert increment.signatures == [(kernels.numba.int64,)]


def test_compile_kernel_propagates_other_runtime_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_cached(signature: str, *, cache: bool = False):
        if cache:

            def decorate(function):
                raise RuntimeError("compilation failed")

            return decorate
        pytest.fail("unexpected uncached retry")

    monkeypatch.setattr(kernels.numba, "njit", fail_cached)

    with pytest.raises(RuntimeError, match="compilation failed"):
        kernels.compile_kernel("int64(int64)")(lambda value: value + 1)


def _scipy_eq(chunk, sos, gains, state):  # type: ignore[no-untyped-def]
    low_gain, mid_gain, high_gain = gains
    low, state["low"] = sosfilt(sos["low"], chunk, axis=0, zi=state["low"])
    high, state["high"] = sosfilt(sos["high"], chunk, axis=0, zi=state["high"])
    return (chunk * mid_gain + low * (low_gain - mid_gain) + high * (high_gain - mid_gain)).astype(
        np.float32
    )


@pytest.mark.parametrize("gains", [(1.0, 1.0, 1.0), (1.6, 0.4, 1.2), (0.0, 0.0, 0.0)])
def test_eq_matches_scipy_block_by_block(gains: tuple[float, float, float]) -> None:
    sos = make_eq_filters(44100)
    audio = _noise(882 * 20)
    ours, theirs = make_eq_state(sos, channels=2), make_eq_state(sos, channels=2)
    for start in range(0, len(audio), 882):
        block = audio[start : start + 882]
        expected = _scipy_eq(block, sos, gains, theirs)
        np.testing.assert_allclose(apply_eq(block, sos, *gains, state=ours), expected, atol=3e-7)
    for band in ours:
        np.testing.assert_allclose(ours[band], theirs[band], rtol=1e-12, atol=1e-15)


def test_ramp_matches_per_frame_linspace_gains() -> None:
    sos = make_eq_filters(44100)
    block = _noise(882, seed=1)
    for start, end in (((1.0, 1.0, 1.0), (0.5, 1.0, 1.0)), ((1.0, 0.7, 1.3), (1.8, 0.2, 0.9))):
        ramp = np.linspace(start, end, 883)[1:]
        low, mid, high = ramp.T[:, :, None]
        expected = apply_eq(block, sos, low, mid, high, state=make_eq_state(sos, channels=2))
        got = ramp_eq(block, sos, start, end, make_eq_state(sos, channels=2))
        np.testing.assert_array_equal(got, expected)


def test_mono_eq_still_works_with_carried_state() -> None:
    sos = make_eq_filters(44100)
    audio = _noise(4410)[:, 0].copy()
    whole = apply_eq(audio, sos, 1.6, 0.4, 1.2, state=make_eq_state(sos))
    state = make_eq_state(sos)
    pieces = [
        apply_eq(audio[i : i + 441], sos, 1.6, 0.4, 1.2, state=state) for i in range(0, 4410, 441)
    ]
    np.testing.assert_allclose(np.concatenate(pieces), whole, atol=1e-6)


def test_nan_to_num_in_place_matches_numpy() -> None:
    block = _noise(882)
    block[3, 0], block[5, 1], block[7, 0] = np.nan, np.inf, -np.inf
    expected = np.nan_to_num(block.copy())
    nan_to_num_in_place(block)
    np.testing.assert_array_equal(block, expected)


@pytest.mark.parametrize("ramp_frames", [0, 882])
@pytest.mark.parametrize("liner_left", [0, 300, 2000])
def test_duck_and_mix_matches_the_numpy_ramp(ramp_frames: int, liner_left: int) -> None:
    block = _noise(882)
    liner = _noise(5000, seed=2)
    gain, step, target = 1.0, (0.25 - 1.0) / 1764, 0.25
    duck = (gain + step * np.arange(1, 883, dtype=np.float64)).astype(np.float32)
    duck[ramp_frames:] = np.float32(target)
    expected = block * duck[:, None]
    pos = len(liner) - liner_left
    piece = liner[pos : pos + 882]
    expected[: len(piece)] += piece
    duck_and_mix_in_place(block, gain, step, ramp_frames, target, liner, pos)
    np.testing.assert_array_equal(block, expected)
