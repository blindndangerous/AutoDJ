from __future__ import annotations

import io
import shutil
import subprocess  # nosec B404 -- fixed FFmpeg argv for a test file
import threading
import time
import wave
from unittest.mock import patch

import numpy as np
import pytest

from autodj.stream_decode import SAMPLE_RATE, StreamDecoder


class _Process:
    def __init__(self, pcm: bytes, *, returncode: int = 0, stderr: bytes = b"") -> None:
        self.stdout = io.BytesIO(pcm)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode if self.stdout.closed else None

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.stdout.close()

    def kill(self):
        self.terminate()


def _decoder(pcm: bytes, *, returncode: int = 0, stderr: bytes = b""):
    child = _Process(pcm, returncode=returncode, stderr=stderr)
    patchers = (
        patch("autodj.stream_decode.shutil.which", return_value="ffmpeg"),
        patch("autodj.stream_decode.subprocess.Popen", return_value=child),
    )
    for patcher in patchers:
        patcher.start()
    decoder = StreamDecoder(
        "music.wav", start_seconds=1, end_seconds=4, filters="volume=0.5", playback_rate=1.1
    )
    return decoder, child, patchers


def test_progressive_decoder_backpressures_and_preserves_every_frame():
    """ADR-0004-08: the progressive decoder buffer stays capped at two seconds."""
    source = np.arange(SAMPLE_RATE * 3 * 2, dtype=np.float32).reshape(-1, 2) / 100
    decoder, _child, patchers = _decoder(source.tobytes())
    try:
        assert decoder.wait_ready(1)
        deadline = time.monotonic() + 1
        while decoder.buffered_frames < 2 * SAMPLE_RATE and time.monotonic() < deadline:
            time.sleep(0.005)
        assert decoder.buffered_frames == 2 * SAMPLE_RATE
        first = decoder.read(SAMPLE_RATE + 7)
        assert np.array_equal(first, source[: SAMPLE_RATE + 7])
        assert decoder.wait_ready(1)
        rest = []
        while True:
            chunk = decoder.read(17003)
            if not len(chunk):
                if decoder.eof:
                    break
                decoder.wait_ready(1)
                continue
            rest.append(chunk)
        assert np.array_equal(np.concatenate([first, *rest]), source)
        assert decoder.eof and decoder.error is None
        assert decoder.buffered_frames == 0
    finally:
        decoder.close()
        for patcher in patchers:
            patcher.stop()


def test_decoder_reports_ffmpeg_failure_and_keeps_diagnostic():
    decoder, _child, patchers = _decoder(b"", returncode=2, stderr=b"bad media")
    try:
        assert not decoder.wait_ready(1)
        deadline = time.monotonic() + 1
        while not decoder.eof and time.monotonic() < deadline:
            time.sleep(0.005)
        assert decoder.eof
        assert "bad media" in decoder.error
        assert decoder.read(1).shape == (0, 2)
    finally:
        decoder.close()
        for patcher in patchers:
            patcher.stop()


def test_decoder_rejects_nonfinite_samples():
    decoder, _child, patchers = _decoder(np.array([[np.nan, 0]], dtype=np.float32).tobytes())
    try:
        assert not decoder.wait_ready(1)
        assert decoder.eof and "non-finite" in decoder.error
    finally:
        decoder.close()
        for patcher in patchers:
            patcher.stop()


def test_process_is_reaped_when_child_binding_fails():
    child = _Process(b"")
    with (
        patch("autodj.stream_decode.shutil.which", return_value="ffmpeg"),
        patch("autodj.stream_decode.subprocess.Popen", return_value=child),
        patch("autodj.stream_decode.bind_to_autodj", side_effect=RuntimeError("bind failed")),
    ):
        decoder = StreamDecoder("music.wav")
        assert "bind failed" in decoder.error
        assert child.terminated
        decoder.close()


def test_unexpected_pipe_read_failure_becomes_decoder_error():
    class BrokenPipe(io.BytesIO):
        def read(self, size=-1):
            raise RuntimeError("pipe read failed")

    child = _Process(b"")
    child.stdout = BrokenPipe()
    with (
        patch("autodj.stream_decode.shutil.which", return_value="ffmpeg"),
        patch("autodj.stream_decode.subprocess.Popen", return_value=child),
    ):
        decoder = StreamDecoder("music.wav")
        try:
            deadline = time.monotonic() + 2
            while decoder.error is None and time.monotonic() < deadline:
                time.sleep(0.005)
            assert "pipe read failed" in decoder.error
            # Its stdout is closed first, which stops a real FFmpeg blocked writing.
            assert child.stdout.closed and child.poll() is not None
        finally:
            decoder.close()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs FFmpeg")
def test_real_ffmpeg_wav_decode_preserves_stereo_sample_order(tmp_path):
    source = np.column_stack(
        (np.arange(5000, dtype=np.int16) - 2500, 2500 - np.arange(5000, dtype=np.int16))
    )
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(source.astype("<i2").tobytes())

    decoder = StreamDecoder(path)
    chunks = []
    try:
        assert decoder.wait_ready(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            chunk = decoder.read(1024)
            if len(chunk):
                chunks.append(chunk)
            elif decoder.eof:
                break
            else:
                decoder.wait_ready(0.1)
        assert decoder.eof and decoder.error is None
        expected = source.astype(np.float32) / 32768
        assert np.allclose(np.concatenate(chunks), expected, atol=2e-5)
    finally:
        decoder.close()


def test_lead_in_is_decoded_before_the_start_and_dropped():
    """ADR-0004-19: The lead-in is decoded before the start and dropped."""
    lead_in = 1000
    source = np.arange(3000 * 2, dtype=np.float32).reshape(-1, 2)
    child = _Process(source.tobytes())
    with (
        patch("autodj.stream_decode.shutil.which", return_value="ffmpeg"),
        patch("autodj.stream_decode.subprocess.Popen", return_value=child) as popen,
    ):
        decoder = StreamDecoder(
            "music.mp3", start_seconds=1.0, lead_in_seconds=lead_in / SAMPLE_RATE
        )
        try:
            command = popen.call_args.args[0]
            seek = float(command[command.index("-ss") + 1])
            assert seek == pytest.approx(1.0 - lead_in / SAMPLE_RATE)
            assert decoder.wait_ready(1)
            deadline = time.monotonic() + 1
            while not decoder.eof and time.monotonic() < deadline:
                time.sleep(0.005)
            assert np.array_equal(decoder.read(3000), source[lead_in:])
        finally:
            decoder.close()


def _tone_mp3(tmp_path) -> str:
    path = tmp_path / "tone.mp3"
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-ac",
            "2",
            "-ar",
            str(SAMPLE_RATE),
            "-b:a",
            "192k",
            str(path),
        ],
        check=True,
    )
    return str(path)


def _first_frames(decoder: StreamDecoder, frames: int) -> np.ndarray:
    chunks, have = [], 0
    deadline = time.monotonic() + 5
    while have < frames and time.monotonic() < deadline:
        chunk = decoder.read(frames - have)
        if len(chunk):
            chunks.append(chunk)
            have += len(chunk)
        elif decoder.eof:
            break
        else:
            decoder.wait_ready(0.1)
    return np.concatenate(chunks)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs FFmpeg")
def test_real_mp3_seek_with_lead_in_starts_on_audio(tmp_path):
    """ADR-0004-19: An MP3 input seek begins with the decoder delay as exact zeros; the lead-in drops it."""
    path = _tone_mp3(tmp_path)
    decoder = StreamDecoder(path, start_seconds=2.0, lead_in_seconds=0.1)
    try:
        head = _first_frames(decoder, 2048)
    finally:
        decoder.close()
    silent = np.all(head == 0, axis=1)
    # Within a 440 Hz tone only isolated zero crossings can be exact zeros.
    assert not silent[:89].all()
    assert silent.sum() <= 2


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs FFmpeg")
def test_closing_a_running_decoder_raises_in_no_reader_thread(tmp_path, monkeypatch):
    """ADR-0004-20: close() lets both pipe readers reach end-of-file before closing the pipes."""
    failures = []
    monkeypatch.setattr(threading, "excepthook", lambda args: failures.append(args.exc_value))
    path = _tone_mp3(tmp_path)
    for _ in range(5):
        decoder = StreamDecoder(path, start_seconds=1.0)
        assert decoder.wait_ready(5)
        deadline = time.monotonic() + 5
        while decoder.buffered_frames < SAMPLE_RATE and time.monotonic() < deadline:
            time.sleep(0.01)
        decoder.close()
    assert failures == []


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs FFmpeg")
def test_closing_a_decoder_blocked_on_a_full_pipe_is_quick(tmp_path):
    """ADR-0004-20: On Linux FFmpeg ignores SIGTERM while blocked writing to a full pipe.

    close() must not wait out the 2 s SIGTERM grace and kill it: every deck
    seek closes a decoder like this one before it opens the next.
    """
    path = _tone_mp3(tmp_path)
    for _ in range(3):
        decoder = StreamDecoder(path, start_seconds=0.5)
        assert decoder.wait_ready(5)
        deadline = time.monotonic() + 5
        while decoder.buffered_frames < 2 * SAMPLE_RATE and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)  # FFmpeg fills the OS pipe behind the full buffer and blocks
        assert decoder._process.poll() is None
        began = time.monotonic()
        decoder.close()
        assert time.monotonic() - began < 0.5
        assert decoder._process.poll() is not None


def test_close_ends_a_decoder_blocked_on_a_full_pipe_without_a_kill():
    """ADR-0004-20: Linux FFmpeg blocked writing ignores SIGTERM but exits once its pipe closes."""

    class _BlockedWriter(_Process):
        killed = False

        def terminate(self):
            self.terminated = True  # ignored while the write is blocked

        def kill(self):
            self.killed = True
            self.stdout.close()

        def wait(self, timeout=None):
            if not self.stdout.closed:
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
            return self.returncode

    child = _BlockedWriter(np.zeros((3 * SAMPLE_RATE, 2), np.float32).tobytes())
    with (
        patch("autodj.stream_decode.shutil.which", return_value="ffmpeg"),
        patch("autodj.stream_decode.subprocess.Popen", return_value=child),
    ):
        decoder = StreamDecoder("music.mp3")
        deadline = time.monotonic() + 2
        while decoder.buffered_frames < 2 * SAMPLE_RATE and time.monotonic() < deadline:
            time.sleep(0.005)
        assert decoder.buffered_frames == 2 * SAMPLE_RATE
        decoder.close()
    assert child.stdout.closed
    assert not child.killed
