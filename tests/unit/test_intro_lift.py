"""The opening gain raises quiet music without crossing its source ceiling."""

import shutil
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from autodj.intro_lift import (
    CEILING,
    apply_intro_lift,
    intro_gain_db,
    intro_weights,
    measure_source_intro,
)


def test_quiet_opening_gets_bounded_lift_and_returns_to_unity() -> None:
    sample_rate = 100
    audio = np.full((1200, 2), 0.05, dtype=np.float32)
    audio[300:] = 0.2
    lift = intro_gain_db(audio, sample_rate, 0.2, 4.0)
    assert lift == 4.0
    result = apply_intro_lift(audio, sample_rate, lift)
    assert result[0, 0] == pytest.approx(0.05 * 10 ** (4 / 20))
    assert result[799, 0] == pytest.approx(audio[799, 0], abs=0.0002)
    assert np.array_equal(result[800:], audio[800:])
    assert intro_weights(801, sample_rate)[800] == 0.0


def test_opening_peak_restricts_lift_and_silence_gets_none() -> None:
    audio = np.full((1200, 2), 0.1, dtype=np.float32)
    audio[100, :] = 0.8
    audio[300:] = 0.4
    lift = intro_gain_db(audio, 100, 0.4, 6)
    assert 0 < lift < 6
    assert np.max(np.abs(apply_intro_lift(audio, 100, lift))) <= CEILING + 1e-5
    assert intro_gain_db(np.zeros_like(audio), 100, 0.4, 6) == 0
    assert intro_gain_db(audio, 100, 0.4, 0) == 0


def test_prepared_decode_is_checked_against_its_own_peak() -> None:
    audio = np.full((900, 2), 0.2, dtype=np.float32)
    audio[50] = 0.88
    result = apply_intro_lift(audio, 100, 6)
    assert np.max(np.abs(result)) <= max(np.max(np.abs(audio)), CEILING) + 1e-5


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is unavailable")
def test_measure_source_reads_long_track_opening(tmp_path: Path) -> None:
    audio = np.full((12 * 1000, 2), 0.05, dtype=np.float32)
    audio[3000:] = 0.2
    path = tmp_path / "song.wav"
    sf.write(path, audio, 1000)
    assert measure_source_intro(path, 1000, 0.2) == 6.0


def test_measure_source_uses_ffmpeg_for_container_libraries_cannot_read(
    monkeypatch, tmp_path: Path
) -> None:
    def unavailable(_path):
        raise OSError("unsupported container")

    opening = np.full((800, 2), 0.05, dtype=np.float32)
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=opening.tobytes())

    monkeypatch.setattr("autodj.intro_lift.sf.SoundFile", unavailable)
    monkeypatch.setattr("autodj.intro_lift.subprocess.run", run)
    assert measure_source_intro(tmp_path / "song.m4a", 100, 0.2) == 6.0
    assert commands[0][commands[0].index("-t") + 1] == "8.0"


def test_stored_lift_rounds_down_so_it_never_passes_the_peak_limit() -> None:
    limit_db = 2.0006
    audio = np.full((900, 2), 0.05, dtype=np.float64)
    audio[0] = CEILING / 10 ** (limit_db / 20)
    lift = intro_gain_db(audio, 100, 1.0, 6.0)
    assert lift == 2.0
    assert lift <= limit_db


def test_opening_is_measured_as_the_mono_mix_like_track_energy() -> None:
    # Wide channels: the mono mix of this opening is half as loud as either channel.
    audio = np.zeros((900, 2), dtype=np.float32)
    audio[:, 0] = 0.1
    track_rms = 0.1
    assert intro_gain_db(audio, 100, track_rms, 6.0) == 6.0
    audio[:, 1] = 0.1
    assert intro_gain_db(audio, 100, track_rms, 6.0) == 0.0
