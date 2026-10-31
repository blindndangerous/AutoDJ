"""Calibration checks for the local programme measurement harness."""

import numpy as np
import pytest
from scipy.signal import resample_poly

from tests.playwright.local_programme_measure import measure, signal


def test_detector_measures_injected_delay_and_gain():
    """ADR-0019-15: calibration detects injected signal changes."""
    source = signal(8000, 4)
    received = np.pad(source * 0.4, ((800, 0), (0, 0)))
    result = measure(source, received, 8000)
    assert result["delay_ms"] == pytest.approx(100, abs=1)
    assert result["dropouts"] == 0
    assert result["channel_swap"] is False


@pytest.mark.parametrize("missing_seconds", [1, 2.5])
def test_detector_finds_dropout_and_reversed_channels(missing_seconds):
    """ADR-0019-15: calibration detects injected signal changes."""
    source = signal(8000, 4)
    received = source[:, ::-1].copy()
    received[8000 : 8000 + int(8000 * missing_seconds)] = 0
    result = measure(source, received, 8000)
    assert result["dropouts"] >= 2
    assert result["channel_swap"] is True


def test_detector_measures_clock_error():
    """ADR-0019-15: calibration detects injected signal changes."""
    source = signal(8000, 8)
    received = resample_poly(source, 1001, 1000)
    result = measure(source, received, 8000)
    assert result["drift_ppm"] == pytest.approx(1000, abs=150)


def test_detector_finds_one_missing_ten_ms_packet():
    """ADR-0019-15: a missing PCM packet cannot hide in a correlated long window."""
    source = signal(44100, 3)
    received = source.copy()
    received[44100:44541] = 0
    result = measure(source, received, 44100)
    assert result["dropouts"] >= 1
    assert result["dropout_window_ms"] == 10


def test_private_marker_detector_rejects_injected_audition():
    """ADR-0019-15: the private marker sits outside the programme code's passband."""
    from tests.playwright.local_programme_prototype_measure import private_energy

    source = signal(44100, 1)
    assert private_energy(source, 44100) < 1e-4
    source[:, 0] += 0.2 * np.sin(2 * np.pi * 5000 * np.arange(len(source)) / 44100)
    assert private_energy(source, 44100) > 0.5


def test_occurrence_measurement_does_not_jump_to_a_later_repeated_song():
    """ADR-0019-15: align the first occurrence when later audio repeats its codes."""
    from tests.playwright.local_programme_prototype_measure import measure_first

    reference = signal(8000, 2)
    earlier = reference + np.random.default_rng(1).normal(0, 0.0001, reference.shape)
    received = np.concatenate([np.zeros((800, 2)), earlier, np.zeros((8000, 2)), reference])
    result = measure_first(reference, received, 8000)
    assert result["delay_ms"] == pytest.approx(100, abs=1)
    assert abs(result["drift_ppm"]) < 100


def test_streaming_detector_covers_every_chunk_and_missing_packet(tmp_path):
    """ADR-0019-15: bounded-memory endurance analysis retains dropout evidence."""
    import soundfile as sf

    from tests.playwright.local_programme_measure import measure_files

    source = signal(8000, 9)
    received = np.pad(source[:, ::-1], ((800, 0), (0, 0)))
    received[5 * 8000 : 5 * 8000 + 80] = 0
    sf.write(tmp_path / "reference.wav", source, 8000, subtype="FLOAT")
    sf.write(tmp_path / "received.wav", received, 8000, subtype="FLOAT")
    result = measure_files(tmp_path / "reference.wav", tmp_path / "received.wav")
    assert result["analysed_seconds"] > 7
    assert result["delay_ms"] == pytest.approx(100, abs=2)
    assert result["dropouts"] >= 1
    assert result["channel_swap"] is True
    assert abs(result["drift_ppm"]) < 100


def test_streaming_detector_measures_accumulating_clock_error(tmp_path):
    """ADR-0019-15: local chunk alignment must not erase long-run drift."""
    import soundfile as sf

    from tests.playwright.local_programme_measure import measure_files

    source = signal(8000, 12)
    received = np.pad(resample_poly(source, 1001, 1000), ((800, 0), (0, 0)))
    sf.write(tmp_path / "reference.wav", source, 8000, subtype="FLOAT")
    sf.write(tmp_path / "received.wav", received, 8000, subtype="FLOAT")
    result = measure_files(tmp_path / "reference.wav", tmp_path / "received.wav")
    assert result["drift_ppm"] == pytest.approx(1000, abs=150)
