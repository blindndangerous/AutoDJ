"""DJ-style skip effects for the server mix."""

from __future__ import annotations

import numpy as np
import pytest

from autodj.skip_fx import (
    BLEND_FRAMES,
    MAX_TAIL_S,
    SKIP_STYLES,
    UNTIMED_TAIL_S,
    render_tail,
    tail_frames,
)
from autodj.stereo import SAMPLE_RATE

_SR = SAMPLE_RATE
EFFECTS = [style for style in SKIP_STYLES if style != "fade"]


def _music(seconds: float = 6.0) -> np.ndarray:
    t = np.arange(int(seconds * _SR)) / _SR
    tone = (0.5 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
    return np.stack([tone, tone], axis=1)


class TestTailFrames:
    def test_an_untrusted_tempo_gets_a_fixed_length(self) -> None:
        assert tail_frames(1000, 0.0, [5000, 9000]) == int(UNTIMED_TAIL_S * _SR)

    def test_it_ends_on_the_first_beat_at_least_a_beat_away(self) -> None:
        beat = int(0.5 * _SR)  # 120 BPM
        start = 10_000
        beats = [start + 100, start + beat - 10, start + beat + 300, start + 2 * beat + 300]
        assert tail_frames(start, 120.0, beats) == beat + 300

    def test_a_trusted_tempo_without_a_grid_lasts_two_beats(self) -> None:
        assert tail_frames(0, 120.0, []) == _SR  # two half-second beats

    def test_slow_tempos_are_capped(self) -> None:
        assert tail_frames(0, 40.0, []) == int(MAX_TAIL_S * _SR)
        # A grid with no beat inside the cap falls back to the capped length.
        assert tail_frames(0, 40.0, [int(5 * _SR)]) == int(MAX_TAIL_S * _SR)


class TestRenderTail:
    @pytest.mark.parametrize("style", EFFECTS)
    def test_each_effect_joins_the_music_and_ends_silent(self, style: str) -> None:
        audio = _music()
        start, frames = 2 * _SR, _SR
        tail = render_tail(style, audio, start, frames, beat_frames=_SR // 2)
        assert tail.shape == (frames, 2)
        assert tail.dtype == np.float32
        # Its first frame is the music it takes over from.
        np.testing.assert_allclose(tail[0], audio[start], atol=1e-6)
        assert np.max(np.abs(tail[-BLEND_FRAMES:])) < 0.01
        assert np.max(np.abs(tail)) <= 1.0
        assert np.max(np.abs(tail[: frames // 2])) > 0.05

    @pytest.mark.parametrize("style", EFFECTS)
    def test_effects_near_the_edges_of_the_render(self, style: str) -> None:
        audio = _music(1.0)
        # From the very start (a backspin has nothing to spin back over)
        # and running past the end (padded with silence).
        for start in (0, len(audio) - 1000):
            tail = render_tail(style, audio, start, _SR // 2)
            assert tail.shape == (_SR // 2, 2)
            assert np.all(np.isfinite(tail))

    def test_backspin_of_an_empty_render_is_silence(self) -> None:
        tail = render_tail("backspin", np.zeros((0, 2), np.float32), 0, 2000)
        assert not tail.any()

    def test_fade_is_not_an_effect(self) -> None:
        with pytest.raises(ValueError, match="not a skip effect"):
            render_tail("fade", _music(), 0, 1000)
