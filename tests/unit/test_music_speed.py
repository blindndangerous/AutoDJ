from __future__ import annotations

import numpy as np
import pytest

from autodj.music_speed import resample_music


def _tone(frequency: float, frames: int, sample_rate: int = 44100) -> np.ndarray:
    time = np.arange(frames, dtype=np.float64) / sample_rate
    left = np.sin(2 * np.pi * frequency * time)
    right = np.sin(2 * np.pi * frequency * 1.5 * time)
    return np.column_stack((left, right)).astype(np.float32)


def _peak_frequency(signal: np.ndarray, sample_rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(signal))
    peak = int(np.argmax(spectrum[1:]) + 1)
    return peak * sample_rate / len(signal)


def test_rate_changes_duration_and_pitch_per_channel() -> None:
    sample_rate = 44100
    source = _tone(440, sample_rate)

    sped_up = resample_music(source, 1.25)
    slowed_down = resample_music(source, 0.8)

    assert sped_up.shape == (round(len(source) / 1.25), 2)
    assert slowed_down.shape == (round(len(source) / 0.8), 2)
    assert sped_up.dtype == slowed_down.dtype == np.float32
    assert _peak_frequency(sped_up[:, 0], sample_rate) == pytest.approx(550, abs=2)
    assert _peak_frequency(sped_up[:, 1], sample_rate) == pytest.approx(825, abs=2)
    assert _peak_frequency(slowed_down[:, 0], sample_rate) == pytest.approx(352, abs=2)
    assert _peak_frequency(slowed_down[:, 1], sample_rate) == pytest.approx(528, abs=2)


def test_unity_rate_returns_float32_input_without_copy() -> None:
    source = _tone(440, 100)

    assert resample_music(source, 1.0) is source


@pytest.mark.parametrize("rate", [0.5, 1.25, 2.0])
@pytest.mark.parametrize("frames", [0, 1, 2, 3])
def test_empty_and_tiny_inputs_have_exact_expected_lengths(frames: int, rate: float) -> None:
    source = np.ones((frames, 2), dtype=np.float32)

    result = resample_music(source, rate)

    assert result.shape == (round(frames / rate), 2)
    assert result.dtype == np.float32


@pytest.mark.parametrize("rate", [0, -1, float("inf"), float("nan"), True])
def test_rate_must_be_finite_and_positive(rate: float) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        resample_music(np.zeros((4, 2), np.float32), rate)


def test_input_must_be_stereo_frames_first_array() -> None:
    with pytest.raises(ValueError, match="stereo"):
        resample_music(np.zeros((4, 1), np.float32), 1.1)
