"""Tests for stereo helpers and stereo-safe DSP primitives."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import soundfile as sf

from autodj import eq, player, stereo


def _tone(n: int = 4410, hz: float = 440.0, sr: int = 44100) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / sr
    return (0.5 * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def test_to_stereo_duplicates_mono_and_keeps_stereo() -> None:
    mono = _tone(10)
    out = stereo.to_stereo(mono)
    assert out.shape == (10, 2)
    assert out.dtype == np.float32
    np.testing.assert_array_equal(out[:, 0], out[:, 1])
    both = np.stack([mono, -mono], axis=1)
    np.testing.assert_array_equal(stereo.to_stereo(both), both)
    many = np.stack([mono, -mono, mono * 0], axis=1)
    np.testing.assert_array_equal(stereo.to_stereo(many), both)


def test_per_channel_matches_mono_for_identical_channels() -> None:
    mono = _tone()
    out = stereo.per_channel(lambda ch: ch * 2.0, stereo.to_stereo(mono))
    np.testing.assert_allclose(out[:, 0], mono * 2.0)
    np.testing.assert_allclose(out[:, 1], mono * 2.0)
    np.testing.assert_array_equal(stereo.per_channel(lambda ch: ch + 1, mono), mono + 1)


def test_per_channel_equalises_column_lengths() -> None:
    two = stereo.to_stereo(_tone(10))
    calls = iter([np.ones(10, np.float32), np.ones(12, np.float32)])
    out = stereo.per_channel(lambda _ch: next(calls), two)
    assert out.shape == (10, 2)
    calls = iter([np.ones(10, np.float32), np.ones(7, np.float32)])
    out = stereo.per_channel(lambda _ch: next(calls), two)
    assert out.shape == (10, 2)
    assert out[9, 1] == 0.0


def test_envelope_broadcasts_for_stereo() -> None:
    env = np.linspace(0, 1, 5, dtype=np.float32)
    assert stereo.envelope(env, np.zeros(5)).shape == (5,)
    assert stereo.envelope(env, np.zeros((5, 2))).shape == (5, 1)


def test_load_stereo_resamples_and_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "mono48k.wav"
    sf.write(path, _tone(4800, sr=48000), 48000)
    out = stereo.load_stereo(str(path))
    assert out.ndim == 2 and out.shape[1] == 2
    assert abs(out.shape[0] - 4410) <= 2
    np.testing.assert_array_equal(out[:, 0], out[:, 1])


def test_load_stereo_keeps_two_channels(tmp_path: Path) -> None:
    path = tmp_path / "st.wav"
    left, right = _tone(441), -_tone(441)
    sf.write(path, np.stack([left, right], axis=1), 44100)
    out = stereo.load_stereo(str(path))
    np.testing.assert_allclose(out[:, 0], left, atol=1e-4)
    np.testing.assert_allclose(out[:, 1], right, atol=1e-4)


def _fake_ffmpeg(decoded: np.ndarray, sr: int):
    """Patchers that make FFmpeg "decode" any file to *decoded* at *sr*."""
    return (
        patch.object(stereo.shutil, "which", return_value="/usr/bin/ffmpeg"),
        patch.object(stereo.subprocess, "run", return_value=MagicMock(returncode=0, stderr=b"")),
        patch.object(stereo.sf, "read", return_value=(decoded, sr)),
    )


def test_load_stereo_decodes_m4a_with_ffmpeg_at_the_target_rate() -> None:
    decoded = np.zeros((44100, 2), dtype=np.float32)
    which, run, read = _fake_ffmpeg(decoded, 44100)
    with which, run as ran, read:
        out = stereo.load_stereo("song.m4a", 44100, max_seconds=60.0)

    command = ran.call_args.args[0]
    assert command[command.index("-i") + 1] == "song.m4a"
    assert command[command.index("-ac") + 1] == "2"
    assert command[command.index("-ar") + 1] == "44100"
    assert command[command.index("-t") + 1] == "61.000"
    assert out.shape == (44100, 2)


def test_ffmpeg_success_logs_complete_stderr_warning(caplog: pytest.LogCaptureFixture) -> None:
    diagnostic = b"[mp3 @ 0x1] first warning\n[decoder] second warning\n"
    decoded = np.zeros(8, dtype=np.float32)
    with (
        patch.object(stereo.shutil, "which", return_value="/usr/bin/ffmpeg"),
        patch.object(
            stereo.subprocess,
            "run",
            return_value=MagicMock(returncode=0, stderr=diagnostic),
        ) as run,
        patch.object(stereo.sf, "read", return_value=(decoded, 44100)),
        caplog.at_level("WARNING", logger="autodj.stereo"),
    ):
        audio, sr = stereo.load_with_ffmpeg("damaged.mp3", channels=1)

    assert audio is decoded
    assert sr == 44100
    assert run.call_args.args[0][1:3] == ["-v", "warning"]
    assert len(caplog.records) == 1
    assert caplog.records[0].message == (
        "Audio decode warning for damaged.mp3:\n[mp3 @ 0x1] first warning\n[decoder] second warning"
    )


def test_ffmpeg_success_with_empty_stderr_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        patch.object(stereo.shutil, "which", return_value="/usr/bin/ffmpeg"),
        patch.object(
            stereo.subprocess,
            "run",
            return_value=MagicMock(returncode=0, stderr=b""),
        ),
        patch.object(stereo.sf, "read", return_value=(np.zeros(1, dtype=np.float32), 44100)),
        caplog.at_level("WARNING", logger="autodj.stereo"),
    ):
        stereo.load_with_ffmpeg("clean.mp3", channels=1)

    assert not caplog.records


def test_ffmpeg_failure_keeps_filename_and_stderr_in_exception() -> None:
    with (
        patch.object(stereo.shutil, "which", return_value="/usr/bin/ffmpeg"),
        patch.object(
            stereo.subprocess,
            "run",
            return_value=MagicMock(returncode=1, stderr=b"bad frame\nno audio stream\n"),
        ),
        pytest.raises(RuntimeError, match=r"broken\.mp3") as caught,
    ):
        stereo.load_with_ffmpeg("broken.mp3", channels=1)

    assert "bad frame\nno audio stream" in str(caught.value)


def test_load_stereo_refuses_an_m4a_longer_than_max_seconds() -> None:
    decoded = np.zeros((44100 * 3, 2), dtype=np.float32)
    which, run, read = _fake_ffmpeg(decoded, 44100)
    with which, run, read, pytest.raises(stereo.TrackTooLongError):
        stereo.load_stereo("song.m4a", 44100, max_seconds=2.0)


def test_player_load_audio_decodes_m4a_with_ffmpeg() -> None:
    decoded = np.zeros(4800, dtype=np.float32)
    which, run, read = _fake_ffmpeg(decoded, 48000)
    with which, run as ran, read:
        audio, sr = player.load_audio("song.m4a")

    command = ran.call_args.args[0]
    assert command[command.index("-ac") + 1] == "1"
    assert "-ar" not in command
    assert sr == 48000
    assert audio.shape == (4800,)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs FFmpeg to encode ALAC")
def test_alac_file_loads_for_the_server_mix_and_analysis(tmp_path: Path) -> None:
    source = tmp_path / "tone.wav"
    left, right = _tone(44100), -_tone(44100)
    sf.write(source, np.stack([left, right], axis=1), 44100)
    alac = tmp_path / "tone.m4a"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(source), "-c:a", "alac", str(alac)],
        check=True,
    )

    mixed = stereo.load_stereo(str(alac), 44100, max_seconds=60.0)
    assert mixed.shape == (44100, 2)
    np.testing.assert_allclose(mixed[:, 0], left, atol=1e-3)
    np.testing.assert_allclose(mixed[:, 1], right, atol=1e-3)

    analysed, sr = player.load_audio(str(alac))
    assert sr == 44100
    assert analysed.shape == (44100,)


@pytest.mark.parametrize("ducked", [False, True])
def test_crossfades_accept_stereo(ducked: bool) -> None:
    a, b = _tone(8820), _tone(8820, hz=220.0)
    cf = 4410
    if ducked:
        mono_out = player._apply_crossfade_ducked(a, b, cf, 44100)
        st_out = player._apply_crossfade_ducked(stereo.to_stereo(a), stereo.to_stereo(b), cf, 44100)
    else:
        mono_out = player._apply_crossfade(a, b, cf)
        st_out = player._apply_crossfade(stereo.to_stereo(a), stereo.to_stereo(b), cf)
    assert st_out.shape == (len(mono_out), 2)
    np.testing.assert_allclose(st_out[:, 0], mono_out, atol=1e-5)
    np.testing.assert_allclose(st_out[:, 1], mono_out, atol=1e-5)


def test_filter_sweep_accepts_stereo() -> None:
    a = _tone(8820)
    mono_out = player.apply_filter_sweep(a, 44100, 8000.0, 400.0)
    st_out = player.apply_filter_sweep(stereo.to_stereo(a), 44100, 8000.0, 400.0)
    np.testing.assert_allclose(st_out[:, 0], mono_out, atol=1e-5)
    np.testing.assert_allclose(st_out[:, 1], mono_out, atol=1e-5)


def test_eq_stateful_stereo_matches_mono() -> None:
    filters = eq.make_eq_filters(44100)
    a = _tone(1764)
    mono_state = eq.make_eq_state(filters)
    st_state = eq.make_eq_state(filters, channels=2)
    st = stereo.to_stereo(a)
    for start in (0, 882):
        m = eq.apply_eq(a[start : start + 882], filters, 0.5, 1.0, 1.5, state=mono_state)
        s = eq.apply_eq(st[start : start + 882], filters, 0.5, 1.0, 1.5, state=st_state)
        np.testing.assert_allclose(s[:, 0], m, atol=1e-5)
        np.testing.assert_allclose(s[:, 1], m, atol=1e-5)


def test_load_stereo_falls_back_to_librosa_on_soundfile_failure() -> None:
    fake_mono = np.ones(22050, dtype=np.float32)
    with (
        patch("autodj.stereo.sf.SoundFile", side_effect=Exception("unsupported format")),
        patch("librosa.load", return_value=(fake_mono, 22050)),
        patch("librosa.resample", side_effect=lambda audio, orig_sr, target_sr, axis: audio),
    ):
        out = stereo.load_stereo("fake.opus", target_sr=22050)
    assert out.shape == (22050, 2)
    np.testing.assert_array_equal(out[:, 0], out[:, 1])


def test_mono_helper() -> None:
    st = np.stack([np.ones(3), np.zeros(3)], axis=1).astype(np.float32)
    np.testing.assert_allclose(stereo.mono(st), 0.5)
    np.testing.assert_array_equal(stereo.mono(np.ones(3)), np.ones(3))


def test_load_stereo_refuses_a_long_file_before_decoding_it(tmp_path: Path) -> None:
    path = tmp_path / "long.wav"
    sf.write(path, _tone(3 * 44100), 44100)
    with (
        patch("soundfile.SoundFile.read", side_effect=AssertionError("decoded")),
        pytest.raises(stereo.TrackTooLongError) as caught,
    ):
        stereo.load_stereo(str(path), max_seconds=2.0)
    assert caught.value.seconds == pytest.approx(3.0)
    assert stereo.load_stereo(str(path), max_seconds=3.0).shape == (3 * 44100, 2)


def test_load_stereo_fallback_decodes_at_most_a_second_past_the_limit() -> None:
    too_long = np.ones((2, 22050 * 3), dtype=np.float32)
    with (
        patch("autodj.stereo.sf.SoundFile", side_effect=Exception("unsupported format")),
        patch("librosa.load", return_value=(too_long, 22050)) as load,
        pytest.raises(stereo.TrackTooLongError),
    ):
        stereo.load_stereo("mix.mp3", target_sr=22050, max_seconds=2.0)
    assert load.call_args.kwargs["duration"] == 3.0
