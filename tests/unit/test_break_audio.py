"""Decoding announcer breaks within limits (#88): header checks, no probing, a byte budget."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import soundfile as sf

from autodj import break_audio
from autodj.break_audio import (
    BreakAudioError,
    check_break_audio,
    decode_break,
    demuxer_for,
)
from autodj.stereo import SAFE_FFMPEG_INPUT, SAMPLE_RATE, TrackTooLongError

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def write(path: Path, seconds: float = 1.0, rate: int = SAMPLE_RATE, channels: int = 2, **kw):
    sound = np.full((max(1, round(seconds * rate)), channels), 0.25, dtype=np.float32)
    sf.write(path, sound, rate, **kw)
    return path


def test_a_playout_rate_wav_decodes_with_soundfile(tmp_path):
    audio = decode_break(write(tmp_path / "b.wav"), 5.0)
    assert audio.shape == (SAMPLE_RATE, 2)
    assert np.allclose(audio, 0.25)


def test_mono_is_made_stereo(tmp_path):
    audio = decode_break(write(tmp_path / "b.flac", channels=1), 5.0)
    assert audio.shape == (SAMPLE_RATE, 2)


@pytest.mark.parametrize(
    ("name", "kw", "words"),
    [
        ("many.flac", {"channels": 8, "rate": 48000}, "mono or stereo"),
        ("fast.wav", {"rate": 655_350, "seconds": 0.01}, "at most 192000 Hz"),
    ],
    ids=["eight channels", "huge rate"],
)
def test_the_header_refuses_what_would_not_fit_before_decoding(tmp_path, name, kw, words):
    path = write(tmp_path / name, **kw)
    with (
        patch.object(break_audio.sf, "read", side_effect=AssertionError("decoded")),
        pytest.raises(BreakAudioError, match=words),
    ):
        decode_break(path, 300.0)


def test_a_header_longer_than_allowed_is_refused_unread(tmp_path):
    path = write(tmp_path / "long.wav", seconds=3.0)
    with (
        patch.object(break_audio.sf, "read", side_effect=AssertionError("decoded")),
        pytest.raises(TrackTooLongError),
    ):
        decode_break(path, 2.0)


def test_contents_must_match_the_extension(tmp_path):
    path = tmp_path / "b.mp3"
    path.write_bytes((write(tmp_path / "real.wav")).read_bytes())
    with pytest.raises(BreakAudioError, match=r"not \.mp3 audio"):
        check_break_audio(path, 5.0)


def test_only_break_types_have_a_demuxer():
    assert demuxer_for(Path("x.M4A")) == "mov"
    with pytest.raises(BreakAudioError):
        demuxer_for(Path("x.m3u8"))


def _ffprobe_reply(rate: int, channels: int, seconds: float | None = 1.0) -> MagicMock:
    found = {"streams": [{"sample_rate": str(rate), "channels": channels}]}
    if seconds is not None:
        found["format"] = {"duration": str(seconds)}
    import json

    return MagicMock(stdout=json.dumps(found).encode())


def test_ffprobe_runs_with_the_demuxer_and_the_protocol_whitelist(tmp_path):
    path = tmp_path / "b.m4a"
    path.write_bytes(b"not read by soundfile")
    runner = MagicMock(return_value=_ffprobe_reply(48000, 2))
    with patch.object(break_audio.shutil, "which", return_value="ffprobe"):
        assert check_break_audio(path, 5.0, runner=runner) == "ffmpeg"
    argv = runner.call_args.args[0]
    assert argv[argv.index("-f") + 1] == "mov"
    assert argv[argv.index("-protocol_whitelist") + 1] == "file,pipe"
    for reply, error in (
        (_ffprobe_reply(655_350, 2), BreakAudioError),
        (_ffprobe_reply(44100, 8), BreakAudioError),
        (_ffprobe_reply(44100, 2, 9.0), TrackTooLongError),
        (MagicMock(stdout=b"{}"), BreakAudioError),
    ):
        runner.return_value = reply
        with (
            patch.object(break_audio.shutil, "which", return_value="ffprobe"),
            pytest.raises(error),
        ):
            check_break_audio(path, 5.0, runner=runner)
    with (
        patch.object(break_audio.shutil, "which", return_value=None),
        pytest.raises(BreakAudioError, match="ffprobe"),
    ):
        check_break_audio(path, 5.0, runner=runner)


class _Pipe:
    """A stand-in FFmpeg process whose output never ends."""

    def __init__(self, chunk: bytes, chunks: int | None = None, code: int = 0) -> None:
        self.stdout = SimpleNamespace(read=self._read, close=lambda: None)
        self._chunk = chunk
        self._left = chunks
        self.returncode: int | None = None
        self._code = code
        self.killed = False

    def _read(self, _size: int) -> bytes:
        if self.killed or self._left == 0:
            return b""
        if self._left is not None:
            self._left -= 1
        return self._chunk

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = self._code
        return self._code

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_ffmpeg_output_is_read_against_a_byte_budget(tmp_path):
    path = write(tmp_path / "b.wav", rate=48000)
    endless = _Pipe(b"\0" * (1024 * 1024))
    popen = MagicMock(return_value=endless)
    with (
        patch.object(break_audio.shutil, "which", return_value="ffmpeg"),
        pytest.raises(TrackTooLongError),
    ):
        decode_break(path, 2.0, popen=popen)
    assert endless.killed
    argv = popen.call_args.args[0]
    assert argv[argv.index("-f") + 1] == "wav"
    assert list(SAFE_FFMPEG_INPUT) == argv[argv.index("-protocol_whitelist") :][:2]
    assert argv[argv.index("-ar") + 1] == str(SAMPLE_RATE)
    assert argv[argv.index("-ac") + 1] == "2"
    assert argv[argv.index("-t") + 1] == "3.000"


def test_ffmpeg_output_becomes_stereo_float32(tmp_path):
    path = write(tmp_path / "b.wav", rate=48000)
    frames = np.full((100, 2), 0.5, dtype="<f4").tobytes()
    with patch.object(break_audio.shutil, "which", return_value="ffmpeg"):
        audio = decode_break(path, 2.0, popen=MagicMock(return_value=_Pipe(frames, 1)))
        assert audio.shape == (100, 2) and np.allclose(audio, 0.5)
        with pytest.raises(BreakAudioError):
            decode_break(path, 2.0, popen=MagicMock(return_value=_Pipe(frames, 1, code=1)))
        with pytest.raises(BreakAudioError):
            decode_break(path, 2.0, popen=MagicMock(return_value=_Pipe(b"", 0)))
    with (
        patch.object(break_audio.shutil, "which", return_value=None),
        pytest.raises(BreakAudioError, match="needs FFmpeg"),
    ):
        decode_break(path, 2.0)


@pytest.mark.skipif(not HAVE_FFMPEG, reason="FFmpeg is not installed")
def test_a_playlist_named_mp3_is_never_followed(tmp_path):
    target = tmp_path / "secret.wav"
    write(target)
    path = tmp_path / "b.mp3"
    path.write_text(f"#EXTM3U\n#EXTINF:1,\n{target.as_posix()}\nhttp://127.0.0.1:9/x.mp3\n")
    with pytest.raises(BreakAudioError):
        decode_break(path, 5.0)


@pytest.mark.skipif(not HAVE_FFMPEG, reason="FFmpeg is not installed")
def test_ffmpeg_resamples_another_rate_to_the_playout_rate(tmp_path):
    audio = decode_break(write(tmp_path / "b.wav", seconds=1.0, rate=48000), 5.0)
    assert abs(len(audio) - SAMPLE_RATE) < 100
    assert audio.shape[1] == 2


def test_every_ffmpeg_file_input_is_whitelisted():
    from autodj import stereo

    with (
        patch.object(stereo.shutil, "which", return_value="ffmpeg"),
        patch.object(
            stereo.subprocess, "run", return_value=MagicMock(returncode=0, stderr=b"")
        ) as run,
        patch.object(stereo.sf, "read", return_value=(np.zeros(2, np.float32), 44100)),
    ):
        stereo.load_with_ffmpeg("song.mp3", channels=1)
    argv = run.call_args.args[0]
    assert argv.index("-protocol_whitelist") < argv.index("-i")
    assert argv[argv.index("-protocol_whitelist") + 1] == "file,pipe"


def test_processing_and_liner_decodes_are_whitelisted(tmp_path, monkeypatch):
    from autodj import liner_scheduler, processing

    commands: list[list[str]] = []

    def fake_stream(command):
        commands.append(command)
        return 1, bytearray(), b"failed"

    monkeypatch.setattr(processing, "_stream_ffmpeg", fake_stream)
    source = write(tmp_path / "x.wav")
    with pytest.raises(RuntimeError):
        processing.load_and_measure_audio(source, "liner", 5.0, input_format="wav")
    assert commands[0][commands[0].index("-f") + 1] == "wav"
    assert "-protocol_whitelist" in commands[0]
    runner = MagicMock(return_value=subprocess.CompletedProcess([], 1, b"", b""))
    (tmp_path / "liners").mkdir()
    (tmp_path / "liners" / "bad.mp3").write_bytes(b"not audio")
    with pytest.raises(liner_scheduler.LinerDecodeError):
        liner_scheduler.decode_liner(tmp_path / "liners", "bad.mp3", runner=runner)
    assert "-protocol_whitelist" in runner.call_args.args[0]
