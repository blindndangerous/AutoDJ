from __future__ import annotations

import io
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

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
    measured = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-f",
            "f32le",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-i",
            "pipe:0",
            "-af",
            "ebur128=peak=none",
            "-f",
            "null",
            "-",
        ],
        input=audio.astype("<f4").tobytes(),
        capture_output=True,
        check=True,
    )
    lufs = re.findall(rb"I:\s*(-?[\d.]+) LUFS", measured.stderr)
    assert float(lufs[-1]) == pytest.approx(processing.TARGET_LUFS[kind], abs=0.1)


def test_loudness_measurement_reports_ffmpeg_decode_failure(tmp_path: Path) -> None:
    damaged = tmp_path / "damaged.wav"
    damaged.write_bytes(b"not an audio file")
    with pytest.raises(RuntimeError, match="loudness measurement failed"):
        processing.analyze_loudness(damaged)


def _measurement_for(source: Path) -> dict:
    stat = source.stat()
    return {
        "version": processing.PREPARATION_VERSION,
        "kind": "music",
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "lufs": -20.0,
        "gain_db": -8.0,
    }


class _FinishedDecode:
    """Stands in for an FFmpeg decode child that has already written its output."""

    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def poll(self) -> int:
        return self.returncode

    def kill(self) -> None:
        pass


def test_ffmpeg_measurement_timeout_is_bounded_and_reported(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "song.wav"
    source.write_bytes(b"source")
    calls = []

    def timeout(command, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(processing.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="ffmpeg timed out"):
        processing.analyze_loudness(source)

    assert len(calls) == 1
    assert calls[0]["timeout"] == 600
    assert calls[0]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_ffmpeg_decode_timeout_kills_the_child_and_is_reported(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "song.wav"
    source.write_bytes(b"source")
    measurement = _measurement_for(source)
    real_popen = subprocess.Popen
    started: list[tuple[dict, subprocess.Popen]] = []

    def hung(command, **kwargs):
        # A child that never writes or exits, like a stuck decode.
        proc = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        started.append((kwargs, proc))
        return proc

    monkeypatch.setattr(processing, "_FFMPEG_TIMEOUT_S", 0.3)
    monkeypatch.setattr(processing.subprocess, "Popen", hung)
    with pytest.raises(RuntimeError, match="ffmpeg timed out"):
        processing.load_processed_audio(source, measurement)

    kwargs, proc = started[0]
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert proc.poll() is not None


def test_decode_holds_one_full_copy_of_the_samples(tmp_path: Path) -> None:
    import tracemalloc

    rate = 44_100
    t = np.arange(rate * 20, dtype=np.float32) / rate
    signal = 0.12 * np.sin(2 * np.pi * 440 * t)
    source = tmp_path / "long.wav"
    sf.write(source, np.column_stack((signal, signal)), rate, subtype="PCM_16")
    measurement = processing.analyze_loudness(source)

    tracemalloc.start()
    try:
        audio = processing.load_processed_audio(source, measurement)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert audio.shape == (20 * processing.SAMPLE_RATE, 2)
    # The samples are a writable view of the read buffer, not a copy of it.
    assert audio.flags.writeable and not audio.flags.owndata
    # Pipe bytes plus a copied array would peak above twice the samples.
    assert peak < 1.6 * audio.nbytes


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


def test_recovered_decode_reports_diagnostics(tmp_path: Path, monkeypatch, caplog) -> None:
    source = _source(tmp_path / "song.wav")
    measurement = processing.analyze_loudness(source)
    monkeypatch.setattr(
        processing.subprocess,
        "Popen",
        lambda *args, **kwargs: _FinishedDecode(
            np.ones((20, 2), dtype="<f4").tobytes(), b"damaged frame recovered"
        ),
    )
    processing.load_processed_audio(source, measurement)
    assert "damaged frame recovered" in caplog.text
    assert str(source) in caplog.text


@pytest.mark.parametrize("during_measurement", [False, True])
def test_source_replacement_during_processing_is_rejected(
    tmp_path: Path, monkeypatch, during_measurement
) -> None:
    from types import SimpleNamespace

    source = _source(tmp_path / "song.wav")
    measurement = processing.analyze_loudness(source)

    def replaced(*args, **kwargs):
        source.write_bytes(b"replacement")
        return SimpleNamespace(
            returncode=0, stdout=np.ones((20, 2), dtype="<f4").tobytes(), stderr=b"I: -20.0 LUFS"
        )

    def replaced_mid_decode(*args, **kwargs):
        source.write_bytes(b"replacement")
        return _FinishedDecode(np.ones((20, 2), dtype="<f4").tobytes())

    monkeypatch.setattr(processing.subprocess, "run", replaced)
    monkeypatch.setattr(processing.subprocess, "Popen", replaced_mid_decode)
    with pytest.raises(RuntimeError, match="source changed"):
        if during_measurement:
            processing.analyze_loudness(source)
        else:
            processing.load_processed_audio(source, measurement)


@pytest.mark.parametrize("kind", ["music", "liner"])
@pytest.mark.parametrize("maker", [_source, _mono_48k_source])
def test_one_pass_decode_and_measure_matches_the_two_passes(tmp_path: Path, kind, maker) -> None:
    """A loudness cache miss decodes once and gives the same samples and measurement."""
    source = maker(tmp_path / "song.wav")
    measurement = processing.analyze_loudness(source, kind)
    expected = processing.load_processed_audio(source, measurement, kind)

    audio, measured = processing.load_and_measure_audio(source, kind)

    assert measured == measurement
    np.testing.assert_array_equal(audio, expected)
    assert audio.flags.writeable


def test_one_pass_decode_runs_ffmpeg_once(tmp_path: Path, monkeypatch) -> None:
    source = _source(tmp_path / "song.wav")
    real = processing.subprocess.Popen
    started: list[list[str]] = []

    def counting(command, *args, **kwargs):
        started.append(command)
        return real(command, *args, **kwargs)

    monkeypatch.setattr(processing.subprocess, "Popen", counting)
    monkeypatch.setattr(processing.subprocess, "run", MagicMock(side_effect=AssertionError))
    processing.load_and_measure_audio(source)
    assert len(started) == 1


def test_one_pass_decode_rejects_silence_long_tracks_and_reports_errors(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    with pytest.raises(ValueError, match="silent"):
        processing.load_and_measure_audio(_source(tmp_path / "silence.wav", silence=True))
    source = _source(tmp_path / "song.wav")
    with pytest.raises(processing.TrackTooLongError):
        processing.load_and_measure_audio(source, max_seconds=1.0)
    log = b"[mp3 @ 0x1] [error] damaged frame recovered\n[info] I: -20.0 LUFS\n"
    monkeypatch.setattr(
        processing.subprocess,
        "Popen",
        lambda *args, **kwargs: _FinishedDecode(np.full((20, 2), 0.1, "<f4").tobytes(), log),
    )
    audio, measurement = processing.load_and_measure_audio(source)
    assert measurement["lufs"] == -20.0
    np.testing.assert_allclose(audio, 0.1 * 10 ** ((-28.0 + 20.0) / 20), rtol=1e-6)
    assert "damaged frame recovered" in caplog.text
    assert "LUFS" not in caplog.text


def test_scan_refuses_a_track_over_the_limit_before_decoding(tmp_path: Path, monkeypatch) -> None:
    """A three-hour mix once needed about 10 GB to analyse; its header says it is too long."""
    source = _source(tmp_path / "mix.wav")  # 3 s
    monkeypatch.setattr(processing, "load_stereo", MagicMock(side_effect=AssertionError("decoded")))
    with pytest.raises(processing.TrackTooLongError) as refused:
        processing.analyze_basic(source, max_seconds=1.0)
    assert refused.value.seconds == pytest.approx(3.0)


def test_scan_passes_the_limit_to_the_decode(tmp_path: Path, monkeypatch) -> None:
    """A header that understates the length is caught by the decode, a second past the limit."""
    source = _source(tmp_path / "song.wav")
    monkeypatch.setattr(processing, "probe_seconds", lambda _source: None)
    with pytest.raises(processing.TrackTooLongError):
        processing.analyze_basic(source, max_seconds=1.0)
    entry, _points = processing.analyze_basic(source, max_seconds=10.0)
    assert entry.length == pytest.approx(3.0)


def test_probe_seconds_reads_headers_then_ffprobe(tmp_path: Path, monkeypatch) -> None:
    source = _source(tmp_path / "song.wav")
    assert processing.probe_seconds(source) == pytest.approx(3.0)
    monkeypatch.setattr("autodj.audio_meta._open_mutagen", MagicMock(return_value=None))
    if shutil.which("ffprobe"):
        assert processing.probe_seconds(source) == pytest.approx(3.0, abs=0.05)
    assert processing.probe_seconds(tmp_path / "missing.wav") is None


@pytest.mark.parametrize(
    "sketchy,mode,expected", [(False, 0, None), (True, 2, None), (False, 2, 1000.0)]
)
def test_only_reliable_mpeg_duration_is_used_for_refusal(
    tmp_path, monkeypatch, sketchy, mode, expected
):
    from types import SimpleNamespace

    from mutagen.mp3 import MPEGInfo

    info = object.__new__(MPEGInfo)
    info.length, info.sketchy, info.bitrate_mode = 1000.0, sketchy, mode
    monkeypatch.setattr(
        "autodj.audio_meta._open_mutagen", lambda _source: SimpleNamespace(info=info)
    )
    monkeypatch.setattr(
        processing.subprocess, "run", MagicMock(side_effect=AssertionError("estimated fallback"))
    )
    assert processing.probe_seconds(tmp_path / "song.mp3") == expected


def test_unknown_raw_aac_duration_is_not_used_for_refusal(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "autodj.audio_meta._open_mutagen",
        lambda _source: SimpleNamespace(info=SimpleNamespace(length=1000.0)),
    )
    assert processing.probe_seconds(tmp_path / "song.aac") is None
