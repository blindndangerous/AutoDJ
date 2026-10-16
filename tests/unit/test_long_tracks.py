"""Tracks of any length (#391): bounded memory for decoding, analysis and playout.

No test needs a long file: the thresholds are lowered so a few seconds of
audio count as a long track.
"""

from __future__ import annotations

import mmap
import shutil
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import soundfile as sf
from scipy.signal import resample_poly

from autodj import dj_meta, music_speed, processing, stereo
from autodj.dj_meta import Cue, DjMeta

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is unavailable")
SR = stereo.SAMPLE_RATE


def _song(path: Path, seconds: float = 6.0) -> Path:
    """A tone that fades in for a second and stops a second before the end."""
    t = np.arange(round(SR * seconds), dtype=np.float32) / SR
    signal = 0.2 * np.sin(2 * np.pi * 440 * t)
    signal[t < 1.0] *= t[t < 1.0]
    signal[t > seconds - 1.0] = 0.0
    sf.write(path, np.column_stack((signal, signal)), SR, subtype="FLOAT")
    return path


def _mapped(array: np.ndarray) -> bool:
    base: object = array
    while isinstance(base, np.ndarray | memoryview):
        base = base.base if isinstance(base, np.ndarray) else base.obj
    return isinstance(base, mmap.mmap)


@pytest.fixture
def tiny_spill(monkeypatch):
    """Spill anything over a tenth of a second to a temporary file."""
    monkeypatch.setattr(stereo, "SPILL_FRAMES", SR // 10)
    monkeypatch.setattr(stereo, "_SPILL_BYTES", SR // 10 * 8)
    monkeypatch.setattr(music_speed, "SPILL_FRAMES", SR // 10)


def test_audio_buffer_and_join_spill_long_audio(tiny_spill) -> None:
    short = stereo.audio_buffer(10)
    assert short.shape == (10, 2) and not _mapped(short)
    long = stereo.audio_buffer(SR)
    assert long.shape == (SR, 2) and long.dtype == np.float32 and not long.any()
    assert type(long) is np.ndarray and long.flags.writeable and _mapped(long)
    parts = [np.full((SR // 2, 2), 0.25, np.float32), np.full((SR // 2, 2), 0.5, np.float32)]
    joined = stereo.join_audio(parts)
    assert _mapped(joined)
    np.testing.assert_array_equal(joined, np.concatenate(parts))
    mono = np.linspace(-1, 1, SR, dtype=np.float32)
    both = stereo.to_stereo(mono)
    assert _mapped(both)
    np.testing.assert_array_equal(both, np.column_stack((mono, mono)))


def test_audio_spool_moves_to_a_file_and_maps_whole_frames(tiny_spill) -> None:
    samples = np.arange(SR * 2, dtype="<f4").reshape(-1, 2)
    spool = stereo.AudioSpool()
    data = samples.tobytes() + b"\x01\x02"  # a torn last frame
    for start in range(0, len(data), 4096):
        spool.write(data[start : start + 4096])
    buffer = spool.buffer()
    assert isinstance(buffer, mmap.mmap)
    audio = np.frombuffer(buffer, dtype="<f4").reshape(-1, 2)
    np.testing.assert_array_equal(audio, samples)
    audio *= 2  # writable, like the in-memory bytearray
    small = stereo.AudioSpool()
    small.write(b"\x00" * 16)
    assert isinstance(small.buffer(), bytearray)


def test_validity_check_reads_in_blocks() -> None:
    audio = np.zeros((1000, 2), np.float32)
    assert not stereo.all_finite_and_audible(audio, block=100)
    audio[950, 0] = 0.1
    assert stereo.all_finite_and_audible(audio, block=100)
    audio[10, 1] = np.nan
    assert not stereo.all_finite_and_audible(audio, block=100)
    assert not stereo.all_finite_and_audible(np.zeros((0, 2), np.float32))


def test_long_resample_matches_one_pass(tiny_spill) -> None:
    audio = np.random.default_rng(3).standard_normal((SR * 3, 2)).astype(np.float32) * 0.1
    rate = 1.03
    ratio = Fraction(1 / rate).limit_denominator(10_000)
    expected = resample_poly(audio, ratio.numerator, ratio.denominator, axis=0)
    expected = expected[: round(len(audio) / rate)]
    with patch.object(music_speed, "_BLOCK_SECONDS", 1):
        result = music_speed.resample_music(audio, rate)
    assert _mapped(result)
    np.testing.assert_allclose(result, expected, atol=1e-6)


@needs_ffmpeg
def test_long_decode_for_playout_goes_to_a_mapped_file(tmp_path, tiny_spill) -> None:
    source = _song(tmp_path / "mix.wav")
    measurement = processing.analyze_loudness(source)
    audio = processing.load_processed_audio(source, measurement)
    assert audio.shape == (6 * SR, 2) and audio.flags.writeable and _mapped(audio)
    measured, _ = processing.load_and_measure_audio(source)
    assert _mapped(measured)
    np.testing.assert_allclose(measured, audio, atol=1e-6)
    stereo_audio = stereo.load_stereo(str(source))
    assert stereo_audio.shape == (6 * SR, 2) and _mapped(stereo_audio)
    piped, rate = stereo.load_with_ffmpeg(source, channels=2, sample_rate=SR)
    assert rate == SR and piped.shape == (6 * SR, 2) and _mapped(piped)
    np.testing.assert_allclose(piped, stereo_audio, atol=1e-6)
    mono, _ = stereo.load_with_ffmpeg(source, channels=1, sample_rate=SR)
    assert mono.shape == (6 * SR,)


def test_ffmpeg_time_limit_grows_with_the_track(tmp_path, monkeypatch) -> None:
    source = _song(tmp_path / "song.wav", 2.0)
    assert processing._ffmpeg_timeout(source) == processing._FFMPEG_TIMEOUT_S
    monkeypatch.setattr(processing, "_FFMPEG_TIMEOUT_S", 1)
    assert processing._ffmpeg_timeout(source) == pytest.approx(2.0)
    assert processing._ffmpeg_timeout(tmp_path / "missing.wav") == 1


@needs_ffmpeg
def test_long_track_is_read_once_keeping_its_start_and_end(tmp_path) -> None:
    source = _song(tmp_path / "mix.wav")
    whole = processing.read_windows(source, window_seconds=1.0, whole_seconds=10.0)
    assert isinstance(whole, np.ndarray) and len(whole) == 6 * SR
    windows = processing.read_windows(source, window_seconds=1.0, whole_seconds=2.0)
    assert isinstance(windows, processing.TrackWindows)
    assert windows.frames == 6 * SR and windows.seconds == pytest.approx(6.0)
    assert len(windows.head) == len(windows.tail) == SR
    assert windows.tail_start == 5 * SR
    np.testing.assert_allclose(windows.head, whole[:SR], atol=1e-6)
    np.testing.assert_allclose(windows.tail, whole[-SR:], atol=1e-6)
    energy = float(np.sqrt(np.mean(whole.mean(axis=1).astype(np.float64) ** 2)))
    assert windows.energy == pytest.approx(energy, rel=1e-4)


@needs_ffmpeg
def test_long_track_basic_analysis_uses_its_ends_on_the_whole_timeline(
    tmp_path, monkeypatch
) -> None:
    source = _song(tmp_path / "mix.wav", 8.0)
    reference, ref_points = processing.analyze_basic(source)
    monkeypatch.setattr(processing, "LONG_TRACK_SECONDS", 3.0)
    monkeypatch.setattr(processing, "ANALYSIS_WINDOW_SECONDS", 2.5)
    decoded = processing.load_for_analysis(source)
    assert isinstance(decoded, processing.TrackWindows)
    entry, points = processing.analyze_basic(source, audio=decoded)
    assert entry.length == pytest.approx(reference.length)
    assert entry.energy == pytest.approx(reference.energy, rel=1e-3)
    assert points["start"] == pytest.approx(ref_points["start"], abs=0.01)
    # The tone stops a second before the end: End is found in the end window.
    assert points["end"] == pytest.approx(7.0, abs=0.01)
    assert points["end"] == pytest.approx(ref_points["end"], abs=0.01)
    # Decoded here too when no audio is passed.
    again, _ = processing.analyze_basic(source)
    assert again.length == pytest.approx(8.0)


def test_long_track_beats_and_cues_come_from_its_ends(monkeypatch) -> None:
    calls = []

    def analyse(audio, sr, intro_end_s=None, outro_start_s=None):
        calls.append((len(audio), intro_end_s, outro_start_s))
        return DjMeta(
            intro_start_s=0.5,
            intro_end_s=intro_end_s or 0.0,
            outro_start_s=outro_start_s or 0.0,
            beats=[1.0, 2.0],
            analysed=True,
            cues=[
                Cue(1.0, "first_downbeat"),
                Cue(1.5, "phrase"),
                Cue(2.0, "outro_downbeat"),
            ],
        )

    monkeypatch.setattr(dj_meta, "analyse_audio", analyse)
    monkeypatch.setattr(dj_meta, "LONG_TRACK_SECONDS", 10)
    monkeypatch.setattr(dj_meta, "ANALYSIS_WINDOW_SECONDS", 3)
    sr = 100
    meta = dj_meta.analyse_track_audio(np.ones((30 * sr, 2), np.float32), sr, 2.0, 28.0)
    # The start window with the saved Intro, the end window with the saved Outro.
    assert calls == [(300, 2.0, 3.0), (300, 0.0, 1.0)]
    assert meta.analysed and meta.intro_start_s == 0.5 and meta.intro_end_s == 2.0
    assert meta.outro_start_s == pytest.approx(28.0)
    assert meta.beats == [1.0, 2.0, 28.0, 29.0]
    assert [(c.time_s, c.type) for c in meta.cues] == [
        (1.0, "first_downbeat"),
        (1.5, "phrase"),
        (28.5, "phrase"),
        (29.0, "outro_downbeat"),
    ]
    calls.clear()
    short = dj_meta.analyse_track_audio(np.ones((5 * sr, 2), np.float32), sr)
    assert calls == [(500, None, None)] and short.analysed


def test_drop_check_reads_only_around_the_drop_of_stereo_audio() -> None:
    sr = 100
    audio = np.zeros((20 * sr, 2), np.float32)
    audio[10 * sr :] = 0.8
    beats = [float(b) for b in range(1, 20)]
    stereo_drop = dj_meta.confident_drop(audio, sr, 10.0, beats)
    assert stereo_drop == dj_meta.confident_drop(audio.mean(axis=1), sr, 10.0, beats) == 10.0
