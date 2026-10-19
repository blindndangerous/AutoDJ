"""A stream output in AAC or Opus: joining mid-stream, the Ogg header, never blocking (#50)."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import ClassVar

import numpy as np
import pytest

from autodj.stream import StreamOutput
from autodj.stream_formats import AAC, OPUS
from tests.unit._fakes import FakeEncoder
from tests.unit._stream_frames import adts_frame, ogg_page, ogg_stream


def _block() -> np.ndarray:
    return np.full((882, 2), 0.1, np.float32)


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


async def _collect(listener, n: int) -> bytes:
    got = bytearray()
    async for chunk in listener.chunks():
        got += chunk
        if len(got) >= n:
            break
    return bytes(got)


class ScriptedEncoder(FakeEncoder):
    """Hands out the next scripted chunk for each block written."""

    script: ClassVar[list[bytes]] = []

    def __init__(self, bitrate: int) -> None:
        super().__init__(bitrate)
        self._left = list(type(self).script)

    def write(self, pcm: bytes) -> None:
        with self._cond:
            if self._left:
                self._chunks.append(self._left.pop(0))
                self._cond.notify_all()


def _output(fmt, script: list[bytes], **kwargs) -> StreamOutput:
    encoder = type("Encoder", (ScriptedEncoder,), {"script": script})
    return StreamOutput(
        96, 8, encoder_factory=encoder, loop=asyncio.get_running_loop(), fmt=fmt, **kwargs
    )


async def test_an_aac_listener_starts_on_a_whole_frame() -> None:
    frame = adts_frame()
    # The encoder's output splits a frame across chunks, as a pipe can.
    out = _output(AAC, [frame[4:], frame, frame])
    try:
        for _ in range(3):
            out.write(_block())
        _wait_until(lambda: out._mp3_out >= 2 * len(frame) + len(frame) - 4)
        listener = out.add_listener(icy=False)
        assert await _collect(listener, 2 * len(frame)) == frame + frame
        assert out.format is AAC
    finally:
        out.close()


async def test_an_opus_listener_gets_the_header_then_the_recent_pages() -> None:
    header, stream = ogg_stream(audio_pages=6)
    audio = stream[len(header) :]
    page = len(ogg_page(960, b"\xfc" * 40))
    # 1 kbps keeps a burst of 250 bytes, so the stream's first page is gone.
    script = [header, *(audio[at : at + page] for at in range(0, len(audio), page))]
    encoder = type("Encoder", (ScriptedEncoder,), {"script": script})
    out = StreamOutput(1, 8, encoder_factory=encoder, loop=asyncio.get_running_loop(), fmt=OPUS)
    try:
        for _ in script:
            out.write(_block())
        _wait_until(lambda: out._mp3_out >= len(stream))
        assert out._ogg_header == header
        listener = out.add_listener(icy=True)
        got = await _collect(listener, len(header) + page)
        assert got.startswith(header)
        assert got[len(header) :].startswith(b"OggS")
        assert got[len(header) :] in audio
        # No ICY blocks in an Ogg stream, even when the player asks.
        assert listener._icy is None
    finally:
        out.close()


async def test_an_opus_listener_early_on_gets_the_stream_from_its_first_page() -> None:
    _header, stream = ogg_stream(audio_pages=1)
    out = _output(OPUS, [stream])
    try:
        out.write(_block())
        _wait_until(lambda: out._mp3_out >= len(stream))
        listener = out.add_listener(icy=False)
        # The header is not sent twice.
        assert await _collect(listener, len(stream)) == stream
    finally:
        out.close()


async def test_a_new_encoder_starts_a_new_ogg_header() -> None:
    header, stream = ogg_stream(audio_pages=2)
    out = _output(OPUS, [stream])
    try:
        out.write(_block())
        _wait_until(lambda: out._ogg_header == header)
        out.set_bitrate(64)
        assert out._ogg_header == b""
        assert out._ogg_pending == bytearray()
    finally:
        out.close()


async def test_a_stream_without_ogg_headers_is_logged_once_and_left(
    caplog: pytest.LogCaptureFixture,
) -> None:
    out = _output(OPUS, [b"\x00" * 40_000, b"\x00" * 40_000])
    try:
        with caplog.at_level("WARNING", logger="autodj.stream"):
            out.write(_block())
            out.write(_block())
            _wait_until(lambda: out._ogg_pending is None)
        assert out._ogg_header == b""
        assert [r.message for r in caplog.records if "Ogg header" in r.message] == [
            "The Opus stream did not start with Ogg header pages; new listeners may not play it"
        ]
        # Nothing whole to join on: the listener waits for the next pages.
        listener = out.add_listener(icy=False)
        assert listener._size == 0
    finally:
        out.close()


class StalledEncoder(FakeEncoder):
    """Never takes a block: as if its ffmpeg hung."""

    def __init__(self, bitrate: int) -> None:
        super().__init__(bitrate)
        self.release = threading.Event()
        self.started = threading.Event()

    def write(self, pcm: bytes) -> None:
        self.started.set()
        self.release.wait(5.0)


async def test_a_stalled_mount_never_holds_up_the_mix(caplog: pytest.LogCaptureFixture) -> None:
    """The bus's write returns at once; the oldest blocks go, with one warning."""
    out = StreamOutput(
        96, 8, encoder_factory=StalledEncoder, loop=asyncio.get_running_loop(), fmt=AAC
    )
    encoder: StalledEncoder = out._encoder
    try:
        out.write(_block())
        assert encoder.started.wait(1.0)
        with caplog.at_level("WARNING", logger="autodj.stream"):
            start = time.monotonic()
            for _ in range(out._pcm_queue_max * 3):
                out.write(_block())
            elapsed = time.monotonic() - start
        assert elapsed < 0.5
        with out._pcm_cond:
            assert len(out._pcm_queue) <= out._pcm_queue_max
        assert len([r for r in caplog.records if "falling behind" in r.message]) == 1
        # Its threads say which format they serve.
        assert out._writer.name.endswith("autodj-stream-aac-writer")
    finally:
        encoder.release.set()
        out.close()


async def test_quiet_audio_never_makes_a_new_listener_start_far_behind() -> None:
    """A burst holds at most the last 2 s, however few bytes quiet audio takes."""
    now = [0.0]
    frame = adts_frame()
    out = _output(AAC, [frame] * 10, clock=lambda: now[0])
    try:
        for step in range(10):
            now[0] = step * 0.5
            out.write(_block())
            _wait_until(lambda step=step: out._mp3_out >= (step + 1) * len(frame))
        # 10 tiny frames, far under 2 s of 96 kbps, but only the last 2 s stay.
        assert len(out._burst) == 5
        assert len(out._burst_times) == 5
    finally:
        out.close()


async def test_titles_follow_the_audio_written_for_formats_without_a_fixed_bitrate() -> None:
    frame = adts_frame()
    out = _output(AAC, [frame] * 4)
    try:
        out.write(_block())
        _wait_until(lambda: out._mp3_out >= len(frame))
        out.set_title("Artist", "Song")
        assert out._title == ""
        # The third block's frame comes out after the second block went in.
        for _ in range(3):
            out.write(_block())
        _wait_until(lambda: out._title == "Artist - Song")
    finally:
        out.close()


async def test_an_opus_listener_who_joins_before_the_header_hears_nothing_until_it_is_whole() -> (
    None
):
    header, stream = ogg_stream(audio_pages=2)
    first_half = header[:30]
    out = _output(OPUS, [first_half, header[30:], stream[len(header) :]])
    try:
        out.write(_block())
        _wait_until(lambda: out._mp3_out >= len(first_half))
        listener = out.add_listener(icy=False)
        assert listener._size == 0
        assert out.listener_count == 1
        out.write(_block())
        out.write(_block())
        # The stream from its very first page: the header, then the audio.
        assert await _collect(listener, len(stream)) == stream
        assert out._waiting == []
    finally:
        out.close()


async def test_a_waiting_opus_listener_can_leave_and_is_dropped_on_close() -> None:
    header, _stream = ogg_stream()
    out = _output(OPUS, [header[:30]])
    try:
        out.write(_block())
        _wait_until(lambda: out._mp3_out >= 30)
        leaving, staying = out.add_listener(icy=False), out.add_listener(icy=False)
        out.remove_listener(leaving)
        assert leaving.closed and out.listener_count == 1
        out.close()
        assert staying.closed and out.listener_count == 0
    finally:
        out.close()


async def test_the_title_and_the_count_never_wait_on_the_output_lock() -> None:
    """The mix thread retitles, and the loop counts, while an encoder restarts under the lock."""
    out = _output(AAC, [])
    held, release = threading.Event(), threading.Event()

    def restart() -> None:
        with out._lock:
            held.set()
            release.wait(5)

    worker = threading.Thread(target=restart)
    worker.start()
    try:
        assert held.wait(2)
        start = time.monotonic()
        out.set_title("Artist", "Song")
        assert out.listener_count == 0
        assert time.monotonic() - start < 0.5
    finally:
        release.set()
        worker.join(5)
        out.close()


async def test_an_encoder_that_exits_has_its_last_words_logged_and_shown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import collections

    class Saying(FakeEncoder):
        made = 0

        def __init__(self, bitrate: int) -> None:
            super().__init__(bitrate)
            self.errors = collections.deque(["Unknown encoder 'libopus'"])
            type(self).made += 1
            if type(self).made == 1:
                self.close()  # the first one exits at once

    with caplog.at_level("WARNING", logger="autodj.stream"):
        out = StreamOutput(64, 8, encoder_factory=Saying, loop=asyncio.get_running_loop(), fmt=OPUS)
        try:
            _wait_until(lambda: Saying.made >= 2)
            assert out.last_error == "Unknown encoder 'libopus'"
        finally:
            out.close()
    assert "Opus stream encoder exited; restarting. ffmpeg said: Unknown encoder" in caplog.text
