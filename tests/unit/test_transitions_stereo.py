"""Every transition effect must accept stereo and keep channels in step."""

from __future__ import annotations

import numpy as np
import pytest

from autodj import stereo
from autodj.transitions import TransitionFx, apply_transition

_SR = 44100
_CONCRETE = [fx for fx in TransitionFx if fx not in (TransitionFx.RANDOM, TransitionFx.ROTATE)]


def _signal(n: int) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / _SR
    return (0.4 * np.sin(2 * np.pi * 330 * t)).astype(np.float32)


@pytest.mark.parametrize("effect", _CONCRETE, ids=lambda fx: fx.value)
def test_effect_accepts_stereo_and_keeps_channels_in_step(effect: TransitionFx) -> None:
    tail = stereo.to_stereo(_signal(_SR))
    head = stereo.to_stereo(_signal(_SR // 2))
    out_tail, out_head, extra = apply_transition(tail, head, _SR, effect, seed=7)
    assert out_tail.shape == tail.shape
    assert out_head.shape == head.shape
    assert extra.ndim == 2 and extra.shape[1] == 2
    np.testing.assert_allclose(out_tail[:, 0], out_tail[:, 1], atol=1e-6)
    np.testing.assert_allclose(out_head[:, 0], out_head[:, 1], atol=1e-6)
    if len(extra):
        np.testing.assert_allclose(extra[:, 0], extra[:, 1], atol=1e-6)


@pytest.mark.parametrize("effect", [TransitionFx.GLITCH, TransitionFx.NOISE_RISER])
def test_seed_makes_random_effects_repeatable(effect: TransitionFx) -> None:
    tail = stereo.to_stereo(_signal(_SR))
    head = stereo.to_stereo(_signal(_SR // 2))
    first = apply_transition(tail, head, _SR, effect, seed=3)
    second = apply_transition(tail, head, _SR, effect, seed=3)
    for a, b in zip(first, second, strict=True):
        np.testing.assert_array_equal(a, b)


def test_mono_path_unchanged() -> None:
    tail, head = _signal(_SR), _signal(_SR // 2)
    out_tail, out_head, extra = apply_transition(tail, head, _SR, TransitionFx.ECHO_OUT)
    assert out_tail.ndim == 1 and out_head.ndim == 1 and extra.ndim == 1
