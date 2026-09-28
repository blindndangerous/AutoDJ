"""End-to-end encode with a real ffmpeg; skipped when ffmpeg is absent."""

from __future__ import annotations

import shutil
import threading

import numpy as np
import pytest

from autodj.icy import mp3_frame_offset
from autodj.stream import FfmpegEncoder

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
