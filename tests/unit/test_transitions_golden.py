"""Every transition effect still renders the audio it was tuned to.

Each effect runs over the same fixed-seed music and its output is
reduced to a fingerprint: the RMS of each twelfth of the treated tail,
the treated head and the added layer.  The fingerprints in
``transition_fingerprints.json`` were taken before the effects were
deduplicated into shared families; a change to any effect's settings or
maths moves them.  The tolerance only absorbs the last-bit differences
numpy's float32 sin and exp can show between CPUs.

After an intended change to an effect, rewrite the file with
``AUTODJ_UPDATE_FINGERPRINTS=1 pytest tests/unit/test_transitions_golden.py``
and say in the commit which effects moved.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

from autodj.transitions import META_MODES, TransitionFx, apply_transition

_FILE = Path(__file__).with_name("transition_fingerprints.json")
_SR = 22050
_SEGMENTS = 12
_EFFECTS = [fx for fx in TransitionFx if fx not in META_MODES]


def _music(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(_SR) / _SR
    audio = sum(0.3 * np.sin(2 * np.pi * f * t + rng.uniform(0, 6)) for f in (55, 110, 220, 330))
    audio = audio + 0.05 * rng.standard_normal(_SR)
    return (0.4 * audio).astype(np.float32)


def _fingerprint(effect: TransitionFx) -> list[list[float]]:
    outputs = apply_transition(_music(1), _music(2), _SR, effect, seed=7)
    return [
        [
            float(np.sqrt(np.mean(part.astype(np.float64) ** 2))) if len(part) else 0.0
            for part in np.array_split(out, _SEGMENTS)
        ]
        for out in outputs
    ]


def _stored() -> dict[str, list[list[float]]]:
    return json.loads(_FILE.read_text(encoding="utf-8"))


def test_every_effect_has_a_fingerprint() -> None:
    if os.environ.get("AUTODJ_UPDATE_FINGERPRINTS"):
        prints = {fx.value: _fingerprint(fx) for fx in _EFFECTS}
        rounded = {
            k: [[float(f"{v:.7g}") for v in row] for row in rows] for k, rows in prints.items()
        }
        rows = ",\n".join(f" {json.dumps(k)}: {json.dumps(v)}" for k, v in rounded.items())
        _FILE.write_text("{\n" + rows + "\n}\n", encoding="utf-8", newline="\n")
    assert set(_stored()) == {fx.value for fx in _EFFECTS}


@pytest.mark.parametrize("effect", _EFFECTS, ids=str)
def test_effect_renders_as_tuned(effect: TransitionFx) -> None:
    expected = _stored()[effect.value]
    for name, got, want in zip(
        ("tail", "head", "layer"), _fingerprint(effect), expected, strict=True
    ):
        np.testing.assert_allclose(got, want, rtol=1e-4, atol=1e-7, err_msg=f"{effect} {name}")
