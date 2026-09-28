"""Temporary: the effect refactor must render every effect bit for bit the same.

``_transitions_before`` is a verbatim copy of ``autodj.transitions`` from
before the refactor.  Every effect runs through both on the same
fixed-seed audio, mono and stereo, at two sample rates and a range of
lengths down to a single sample, and the outputs must be identical.
Deleted, with the copy, once the refactor is done.
"""

from __future__ import annotations

import numpy as np
import pytest

from autodj import transitions as after
from tests.unit import _transitions_before as before

_EFFECTS = [
    fx
    for fx in after.TransitionFx
    if fx not in (after.TransitionFx.RANDOM, after.TransitionFx.ROTATE)
]


def _music(n: int, seed: int, sample_rate: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(n) / sample_rate
    audio = sum(0.3 * np.sin(2 * np.pi * f * t + rng.uniform(0, 6)) for f in (55, 110, 220, 330))
    audio = audio + 0.05 * rng.standard_normal(n)
    return (0.4 * audio).astype(np.float32)


def _run(module, tail, head, sample_rate, effect):
    try:
        return module.apply_transition(
            tail, head, sample_rate, module.TransitionFx(effect.value), seed=7
        )
    except Exception as exc:  # both sides must fail the same way
        return type(exc)


@pytest.mark.parametrize("effect", _EFFECTS, ids=str)
@pytest.mark.parametrize("sample_rate", [22050, 44100])
@pytest.mark.parametrize("n", [0, 1, 2, 3, 4, 5, 64, 3001])
def test_mono_short_buffers_match(effect, sample_rate, n) -> None:
    tail = _music(n, 1, sample_rate)
    head = _music(n, 2, sample_rate)
    old = _run(before, tail, head, sample_rate, effect)
    new = _run(after, tail, head, sample_rate, effect)
    if isinstance(old, type):
        assert new is old
        return
    for o, a in zip(old, new, strict=True):
        assert o.dtype == a.dtype
        np.testing.assert_array_equal(a, o)


@pytest.mark.parametrize("effect", _EFFECTS, ids=str)
@pytest.mark.parametrize("sample_rate", [22050, 44100])
def test_long_mono_and_stereo_match(effect, sample_rate) -> None:
    n = int(1.5 * sample_rate)
    mono_tail = _music(n, 1, sample_rate)
    mono_head = _music(n, 2, sample_rate)
    stereo_tail = np.stack([mono_tail, _music(n, 3, sample_rate)], axis=1)
    stereo_head = np.stack([mono_head, _music(n, 4, sample_rate)], axis=1)
    for tail, head in ((mono_tail, mono_head), (stereo_tail, stereo_head)):
        old = _run(before, tail, head, sample_rate, effect)
        new = _run(after, tail, head, sample_rate, effect)
        assert not isinstance(old, type), old
        for o, a in zip(old, new, strict=True):
            assert o.dtype == a.dtype
            assert o.shape == a.shape
            np.testing.assert_array_equal(a, o)
