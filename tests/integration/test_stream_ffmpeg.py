"""End-to-end encode with a real ffmpeg; skipped when ffmpeg is absent."""

from __future__ import annotations

import shutil
import threading

import numpy as np
import pytest

from autodj.icy import mp3_frame_offset
from autodj.stream import FfmpegEncoder
from autodj.stream_formats import AAC, OPUS, adts_frame_offset, ogg_header_end, ogg_page_offset

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def test_two_seconds_encode_to_valid_mp3() -> None:
    encoder = FfmpegEncoder(128)
    t = np.arange(88200, dtype=np.float32) / 44100
    pcm = np.stack([np.sin(2 * np.pi * 440 * t)] * 2, axis=1).astype(np.float32) * 0.3

    # Write and read concurrently, as StreamOutput does with its writer and
    # reader threads: a single-threaded write-then-read sequence can
    # deadlock a real subprocess once the PCM exceeds the OS pipe buffer,
    # because ffmpeg blocks writing encoded output before we ever start
    # draining it.
    def feed() -> None:
        encoder.write(pcm.tobytes())
        encoder.close_input()

    writer = threading.Thread(target=feed)
    writer.start()
    data = bytearray()
    while chunk := encoder.read(65536):
        data += chunk
    writer.join(timeout=5)
    encoder.close()
    assert mp3_frame_offset(bytes(data)) >= 0
    assert len(data) > 128_000 // 8


def _encode(fmt, bitrate: int, seconds: float = 3.0) -> bytes:
    """*seconds* of a tone through a real encoder in *fmt*, written and read at once."""
    encoder = FfmpegEncoder(bitrate, fmt)
    t = np.arange(int(44100 * seconds), dtype=np.float32) / 44100
    pcm = np.stack([np.sin(2 * np.pi * 440 * t)] * 2, axis=1).astype(np.float32) * 0.3

    def feed() -> None:
        encoder.write(pcm.tobytes())
        encoder.close_input()

    writer = threading.Thread(target=feed)
    writer.start()
    data = bytearray()
    while chunk := encoder.read(65536):
        data += chunk
    writer.join(timeout=5)
    encoder.close()
    return bytes(data)


def _decoded_seconds(data: bytes, demuxer: str) -> float:
    """How much audio ffmpeg decodes from *data*; fails the test on a decode error."""
    import subprocess  # nosec B404 -- the test's own ffmpeg, fixed argv

    result = subprocess.run(  # nosec B603 B607
        [
            *("ffmpeg", "-hide_banner", "-v", "error", "-f", demuxer, "-i", "pipe:0"),
            *("-f", "s16le", "-ac", "2", "-ar", "44100", "pipe:1"),
        ],
        input=data,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return len(result.stdout) / (44100 * 2 * 2)


def test_an_aac_listener_joining_mid_stream_can_decode_it() -> None:
    data = _encode(AAC, 96)
    middle = len(data) // 2
    start = adts_frame_offset(data[middle:])
    assert start >= 0
    assert _decoded_seconds(data[middle + start :], "aac") > 1.0


def test_an_opus_listener_joining_mid_stream_can_decode_it_with_the_header() -> None:
    data = _encode(OPUS, 64)
    header_end = ogg_header_end(data)
    assert header_end > 0
    middle = len(data) // 2
    start = ogg_page_offset(data[middle:])
    assert start >= 0
    joined = data[:header_end] + data[middle + start :]
    assert _decoded_seconds(joined, "ogg") > 1.0
