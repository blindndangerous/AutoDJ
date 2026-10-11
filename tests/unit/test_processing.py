from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from autodj import processing

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is unavailable")


def _source(path: Path, *, silence: bool = False) -> Path:
    rate = 44_100
    t = np.arange(rate * 3, dtype=np.float32) / rate
    signal = np.zeros_like(t) if silence else 0.12 * np.sin(2 * np.pi * 440 * t)
    sf.write(path, np.column_stack((signal, signal)), rate, subtype="PCM_16")
    return path


def _mono_48k_source(path: Path) -> Path:
    rate = 48_000
    t = np.arange(rate * 2, dtype=np.float32) / rate
    sf.write(path, 0.12 * np.sin(2 * np.pi * 440 * t), rate, subtype="PCM_16")
    return path


def test_loudness_measurement_and_original_music_playout(tmp_path: Path) -> None:
    source = _source(tmp_path / "song.wav")
    before = source.stat()
    measurement = processing.analyze_loudness(source)
    audio = processing.load_processed_audio(source, measurement)

    assert measurement["lufs"] == pytest.approx(-19.1, abs=1.0)
    assert measurement["gain_db"] == pytest.approx(-8.9, abs=1.0)
    assert audio.shape == (3 * processing.SAMPLE_RATE, 2)
    assert np.isfinite(audio).all()
    assert source.stat().st_mtime_ns == before.st_mtime_ns
    assert processing.loudness_matches(measurement, source)
    assert list(tmp_path.iterdir()) == [source]


def test_liner_filter_preserves_duration_and_relative_level(tmp_path: Path) -> None:
    source = _source(tmp_path / "liner.wav")
    music = processing.analyze_loudness(source)
    liner_measurement = processing.analyze_loudness(source, "liner")
    music_audio = processing.load_processed_audio(source, music)
    liner_audio = processing.load_processed_audio(source, liner_measurement, "liner")

    assert liner_audio.shape == music_audio.shape
    assert np.isfinite(liner_audio).all()
    assert liner_measurement["lufs"] != pytest.approx(music["lufs"], abs=0.05)
    # Targets preserve the requested 3 dB liner advantage.
    assert (
        liner_measurement["lufs"] + liner_measurement["gain_db"] - music["lufs"] - music["gain_db"]
    ) == pytest.approx(3.0)


def test_loudness_validation_rejects_silence_stale_and_bad_gain(tmp_path: Path) -> None:
    silence = _source(tmp_path / "silence.wav", silence=True)
    with pytest.raises(ValueError, match="silent"):
        processing.analyze_loudness(silence)

    source = _source(tmp_path / "song.wav")
    measurement = processing.analyze_loudness(source)
    altered = dict(measurement, gain_db=99)
    assert not processing.loudness_matches(altered, source)
    with pytest.raises(ValueError, match="stale"):
        processing.load_processed_audio(source, altered)
    source.write_bytes(source.read_bytes() + b"x")
    assert not processing.loudness_matches(measurement, source)


def test_processed_audio_obeys_max_seconds(tmp_path: Path) -> None:
    source = _source(tmp_path / "song.wav")
    measurement = processing.analyze_loudness(source)
    with pytest.raises(ValueError):
        processing.load_processed_audio(source, measurement, max_seconds=1.0)
    liner_measurement = processing.analyze_loudness(source, "liner")
    with pytest.raises(ValueError):
        processing.load_processed_audio(source, liner_measurement, "liner", max_seconds=1.0)


@pytest.mark.parametrize("kind", ["music", "liner"])
def test_mono_48k_processing_uses_only_memory_pipe(tmp_path: Path, monkeypatch, kind: str) -> None:
    source = _mono_48k_source(tmp_path / f"{kind}.wav")
    monkeypatch.setattr(
        "tempfile.SpooledTemporaryFile",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("disk spool used")),
    )
    measurement = processing.analyze_loudness(source, kind)
    audio = processing.load_processed_audio(source, measurement, kind)

    assert audio.shape == (2 * processing.SAMPLE_RATE, 2)
    assert np.isfinite(audio).all() and np.any(audio)
    assert measurement["lufs"] + measurement["gain_db"] == pytest.approx(
        processing.TARGET_LUFS[kind]
    )


def test_loudness_measurement_reports_ffmpeg_decode_failure(tmp_path: Path) -> None:
    damaged = tmp_path / "damaged.wav"
    damaged.write_bytes(b"not an audio file")
    with pytest.raises(RuntimeError, match="loudness measurement failed"):
        processing.analyze_loudness(damaged)


def test_basic_suggestions_keep_segue_unset_without_sustained_tail_drop(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path / "song.wav")
    entry, points = processing.analyze_basic(source)

    assert entry.length == pytest.approx(3.0)
    assert points["start"] == pytest.approx(0.0, abs=1 / processing.SAMPLE_RATE)
    assert points["end"] == pytest.approx(3.0, abs=1 / processing.SAMPLE_RATE)
    assert points["segue"] is None


def test_liner_segue_uses_only_a_sustained_final_drop() -> None:
    rate = processing.SAMPLE_RATE
    audio = np.full(rate * 6, 0.2, dtype=np.float32)
    audio[rate : rate * 2] = 0.0  # Interior pause followed by more content.
    audio[rate * 5 + rate // 2 :] = 0.0  # Final sustained drop begins at 5.5s.

    assert processing._suggest_liner_segue(audio, rate) == pytest.approx(5.5)

    no_final_drop = np.full(rate * 6, 0.2, dtype=np.float32)
    no_final_drop[rate : rate * 2] = 0.0
    assert processing._suggest_liner_segue(no_final_drop, rate) is None


def test_liner_without_a_final_drop_falls_back_to_end(tmp_path: Path) -> None:
    source = _source(tmp_path / "liner.wav")

    _, music_points = processing.analyze_basic(source)
    _, liner_points = processing.analyze_basic(source, kind="liner")

    assert music_points["segue"] is None
    assert liner_points["segue"] == liner_points["end"]


def test_liner_segue_analysis_handles_silence_and_short_buffers() -> None:
    rate = processing.SAMPLE_RATE

    assert processing._suggest_liner_segue(np.zeros(rate * 4), rate) is None
    assert processing._suggest_liner_segue(np.zeros(rate // 10), rate) is None
