"""Stream fan-out with a fake encoder."""

from __future__ import annotations

import asyncio
import threading
import time

import numpy as np
import pytest

from autodj.icy import encode_metadata
from autodj.stream import EncoderUnavailableError, ListenerLimitError, StreamOutput
from tests.unit._fakes import FakeEncoder


def _block(value: float = 0.1) -> np.ndarray:
    return np.full((882, 2), value, np.float32)


async def _collect(listener, n: int) -> bytes:
    got = bytearray()
    async for chunk in listener.chunks():
        got += chunk
        if len(got) >= n:
            break
    return bytes(got)


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


@pytest.fixture
async def output():
    out = StreamOutput(
        320, max_listeners=2, encoder_factory=FakeEncoder, loop=asyncio.get_running_loop()
    )
    yield out
    out.close()


async def test_fan_out_same_bytes_to_every_listener(output: StreamOutput) -> None:
    a, b = output.add_listener(icy=False), output.add_listener(icy=False)
    output.write(_block())
    got_a, got_b = await asyncio.gather(_collect(a, 64), _collect(b, 64))
    assert got_a == got_b and len(got_a) >= 64


async def test_listener_limit(output: StreamOutput) -> None:
    output.add_listener(icy=False)
    output.add_listener(icy=False)
    with pytest.raises(ListenerLimitError):
        output.add_listener(icy=False)


async def test_slow_listener_is_dropped_others_continue(output: StreamOutput) -> None:
    slow = output.add_listener(icy=False)
    slow._max_bytes = 100
    fast = output.add_listener(icy=False)
    for _ in range(10):
        output.write(_block())
    await _collect(fast, 128)
    await asyncio.sleep(0.05)
    assert slow.closed
    assert output.listener_count == 1


async def test_icy_listener_gets_title_block(output: StreamOutput) -> None:
    output.set_title("Artist", "Song")
    listener = output.add_listener(icy=True)
    listener._icy._metaint = 32
    listener._icy._until_meta = 32
    output.write(_block())
    data = await _collect(listener, 32 + len(encode_metadata("Artist - Song")))
    assert encode_metadata("Artist - Song") in data


async def test_listener_count_callback_and_disconnect_all(output: StreamOutput) -> None:
    counts: list[int] = []
    output.on_listener_change = counts.append
    first = output.add_listener(icy=False)
    output.remove_listener(first)
    output.add_listener(icy=False)
    output.disconnect_all()
    assert counts == [1, 0, 1, 0]


async def test_connect_disconnect_churn_does_not_leak(output: StreamOutput) -> None:
    for _ in range(50):
        listener = output.add_listener(icy=False)
        output.remove_listener(listener)
    assert output.listener_count == 0
    assert output._listeners == []


async def test_encoder_failures_back_off_then_503() -> None:
    now = [0.0]

    class Dying(FakeEncoder):
        def __init__(self, bitrate: int) -> None:
            super().__init__(bitrate)
            self.alive = False  # reports dead; read() still blocks until close()

    out = StreamOutput(
        320, 8, encoder_factory=Dying, clock=lambda: now[0], loop=asyncio.get_running_loop()
    )
    for _ in range(5):
        out._restart_encoder()
    with pytest.raises(EncoderUnavailableError):
        out.add_listener(icy=False)
    now[0] = 61.0
    out.add_listener(icy=False)
    out.close()


async def test_set_bitrate_restarts_and_disconnects(output: StreamOutput) -> None:
    listener = output.add_listener(icy=False)
    output.set_bitrate(192)
    assert listener.closed
    assert output._encoder.bitrate == 192


# --- Additional coverage: controller-mandated non-blocking write, thread
# safety, restart, drop-oldest, and branch coverage the brief's own test
# list does not exercise. ---


class BlockingEncoder(FakeEncoder):
    """An encoder whose write() blocks until released."""

    def __init__(self, bitrate: int) -> None:
        super().__init__(bitrate)
        self._release = threading.Event()
        self.write_started = threading.Event()

    def write(self, pcm: bytes) -> None:
        self.write_started.set()
        self._release.wait(5.0)
        super().write(pcm)

    def release(self) -> None:
        self._release.set()


async def test_write_returns_promptly_when_encoder_write_blocks() -> None:
    out = StreamOutput(320, 8, encoder_factory=BlockingEncoder, loop=asyncio.get_running_loop())
    encoder: BlockingEncoder = out._encoder
    try:
        out.write(_block())
        assert encoder.write_started.wait(1.0)
        start = time.monotonic()
        out.write(_block())
        elapsed = time.monotonic() - start
        assert elapsed < 0.5
    finally:
        encoder.release()
        out.close()


async def test_slow_pcm_writer_drops_oldest_and_logs_once(caplog: pytest.LogCaptureFixture) -> None:
    out = StreamOutput(320, 8, encoder_factory=BlockingEncoder, loop=asyncio.get_running_loop())
    encoder: BlockingEncoder = out._encoder
    try:
        out.write(_block())  # picked up by the writer thread, which then blocks in write()
        assert encoder.write_started.wait(1.0)
        for _ in range(out._pcm_queue_max + 5):
            out.write(_block())
        with out._pcm_cond:
            assert len(out._pcm_queue) <= out._pcm_queue_max
        with caplog.at_level("WARNING", logger="autodj.stream"):
            out.write(_block())
        warnings = [r for r in caplog.records if "dropping" in r.message.lower()]
        assert len(warnings) == 1
    finally:
        encoder.release()
        out.close()


async def test_pcm_writer_survives_encoder_write_error() -> None:
    class Rejecting(FakeEncoder):
        def write(self, pcm: bytes) -> None:
            raise BrokenPipeError("closed")

    out = StreamOutput(320, 8, encoder_factory=Rejecting, loop=asyncio.get_running_loop())
    out.write(_block())
    _wait_until(lambda: len(out._pcm_queue) == 0)
    out.close()


async def test_listener_push_after_close_is_a_noop() -> None:
    out = StreamOutput(320, 8, encoder_factory=FakeEncoder, loop=asyncio.get_running_loop())
    listener = out.add_listener(icy=False)
    listener.close()
    assert listener.push(b"abc", "") is False
    out.close()


async def test_listener_drains_remaining_chunks_after_close() -> None:
    out = StreamOutput(320, 8, encoder_factory=FakeEncoder, loop=asyncio.get_running_loop())
    listener = out.add_listener(icy=False)
    listener.push(b"leftover", "")
    listener.close()
    got = bytearray()
    async for chunk in listener.chunks():
        got += chunk
    assert bytes(got) == b"leftover"
    out.close()


async def test_remove_listener_is_idempotent(output: StreamOutput) -> None:
    counts: list[int] = []
    output.on_listener_change = counts.append
    listener = output.add_listener(icy=False)
    output.remove_listener(listener)
    output.remove_listener(listener)
    assert counts == [1, 0]
    assert output.listener_count == 0


async def test_encoder_exit_triggers_automatic_restart() -> None:
    class OneShot(FakeEncoder):
        """Exits (read() returns b"") after its first chunk."""

        def __init__(self, bitrate: int) -> None:
            super().__init__(bitrate)
            self._served = False

        def read(self, n: int) -> bytes:
            with self._cond:
                while not self._chunks and not self._closed and not self._served:
                    self._cond.wait(0.05)
                if self._chunks:
                    chunk = self._chunks.pop(0)
                    self._served = True
                    return chunk
                return b""

    out = StreamOutput(320, 8, encoder_factory=OneShot, loop=asyncio.get_running_loop())
    first_encoder = out._encoder
    out.write(_block())
    _wait_until(lambda: out._encoder is not first_encoder, timeout=2.0)
    assert out._encoder is not first_encoder
    out.close()


async def test_restart_encoder_swallows_close_errors() -> None:
    class BadClose(FakeEncoder):
        def close(self) -> None:
            raise RuntimeError("boom")

    out = StreamOutput(320, 8, encoder_factory=BadClose, loop=asyncio.get_running_loop())
    out._restart_encoder()  # must not raise despite close() blowing up
    out.close()


async def test_title_switch_waits_for_output_position(output: StreamOutput) -> None:
    output.write(_block())
    _wait_until(lambda: output._mp3_out > 0)
    output.set_title("Later", "Title")
    assert output._title == ""
    for _ in range(200):
        output.write(_block())
    _wait_until(lambda: output._title != "")
    assert output._title != ""


async def test_stale_failures_are_evicted_from_the_window() -> None:
    now = [0.0]
    out = StreamOutput(
        320, 8, encoder_factory=FakeEncoder, clock=lambda: now[0], loop=asyncio.get_running_loop()
    )
    out._restart_encoder()  # failure recorded at t=0
    now[0] = 61.0
    out._restart_encoder()  # t=61: the t=0 failure falls outside the 60s window
    assert len(out._failures) == 1
    out.close()


def _valid_mp3_frame() -> bytes:
    """One synthetic 128kbps/44100Hz MPEG-1 Layer III frame (zero payload)."""
    header = bytes([0xFF, 0xFB, 0x90, 0x00])
    size = 144 * 128 * 1000 // 44100
    return header + bytes(size - len(header))


class MP3FrameEncoder(FakeEncoder):
    """Emits one real MP3 frame per write, so burst replay can be tested."""

    _FRAME = _valid_mp3_frame()

    def write(self, pcm: bytes) -> None:
        with self._cond:
            self._chunks.append(self._FRAME)
            self._cond.notify_all()


async def test_add_listener_replays_burst_from_frame_boundary() -> None:
    out = StreamOutput(128, 8, encoder_factory=MP3FrameEncoder, loop=asyncio.get_running_loop())
    out.write(_block())
    _wait_until(lambda: len(out._burst) > 0)
    listener = out.add_listener(icy=False)
    got = await _collect(listener, len(MP3FrameEncoder._FRAME))
    assert got == MP3FrameEncoder._FRAME
    out.close()


async def test_burst_buffer_is_trimmed_to_limit() -> None:
    out = StreamOutput(1, 8, encoder_factory=FakeEncoder, loop=asyncio.get_running_loop())
    for _ in range(20):
        out.write(_block())
    # Wait for every chunk to be processed (not just the first one) so the
    # trim loop has definitely run its steady-state number of times; a
    # wait keyed on "the burst is non-empty" can be satisfied by the very
    # first chunk, before any trimming has happened at all.
    _wait_until(lambda: out._mp3_out >= 20 * 64)
    burst_limit = int(out._bitrate * 1000 / 8 * 2.0)
    assert out._burst_size - len(out._burst[0]) < burst_limit
    assert out._burst_size < burst_limit + 64
    out.close()


# --- Review round 1 fixes: stale pending titles, dead-encoder log floods,
# factory failures, restart races, stale-encoder data, post-close behavior,
# thread joins, notification ordering, and the deprecated event-loop
# fallback. ---


async def test_pending_titles_flushed_on_restart(output: StreamOutput) -> None:
    output.write(_block())
    _wait_until(lambda: output._mp3_out > 0)
    output.set_title("First", "Title")
    output.set_title("Second", "Title")
    assert output._title == ""  # neither mark has been reached yet
    output._restart_encoder()
    assert output._title == "Second - Title"
    assert list(output._pending_titles) == []


async def test_pending_titles_flushed_on_set_bitrate(output: StreamOutput) -> None:
    output.write(_block())
    _wait_until(lambda: output._mp3_out > 0)
    output.set_title("First", "Title")
    output.set_title("Second", "Title")
    output.set_bitrate(192)
    assert output._title == "Second - Title"
    assert list(output._pending_titles) == []


async def test_writer_skips_dead_encoder_and_logs_once_per_episode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    out = StreamOutput(320, 8, encoder_factory=FakeEncoder, loop=asyncio.get_running_loop())
    encoder: FakeEncoder = out._encoder
    encoder.alive = False
    with caplog.at_level("WARNING", logger="autodj.stream"):
        for _ in range(5):
            out.write(_block())
        _wait_until(lambda: len(out._pcm_queue) == 0)
    dead_warnings = [r for r in caplog.records if "not alive" in r.message.lower()]
    assert len(dead_warnings) == 1

    caplog.clear()
    encoder.alive = True
    out.write(_block())
    _wait_until(lambda: len(out._pcm_queue) == 0 and out._pcm_in > 0)
    encoder.alive = False
    with caplog.at_level("WARNING", logger="autodj.stream"):
        for _ in range(5):
            out.write(_block())
        _wait_until(lambda: len(out._pcm_queue) == 0)
    dead_warnings = [r for r in caplog.records if "not alive" in r.message.lower()]
    assert len(dead_warnings) == 1  # a new episode logs again, but still just once
    out.close()


class _OnceThenRaiseFactory:
    """Returns a working encoder once, then raises on every later call."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, bitrate: int) -> FakeEncoder:
        self.calls += 1
        if self.calls == 1:
            return FakeEncoder(bitrate)
        raise RuntimeError("ffmpeg missing")


async def test_restart_encoder_records_failure_when_factory_raises() -> None:
    factory = _OnceThenRaiseFactory()
    out = StreamOutput(320, 8, encoder_factory=factory, loop=asyncio.get_running_loop())
    dead = out._encoder
    out._restart_encoder(dead)
    assert factory.calls == 2
    assert len(out._failures) == 2  # the dead encoder, then the failed factory call
    assert out._encoder is dead  # replacement failed; nothing to swap in
    out.close()


async def test_add_listener_raises_and_records_failure_when_factory_raises() -> None:
    factory = _OnceThenRaiseFactory()
    out = StreamOutput(320, 8, encoder_factory=factory, loop=asyncio.get_running_loop())
    out._encoder.alive = False
    with pytest.raises(EncoderUnavailableError):
        out.add_listener(icy=False)
    assert factory.calls == 2
    assert len(out._failures) == 1
    out.close()


async def test_restart_encoder_ignores_a_superseded_dead_encoder() -> None:
    out = StreamOutput(320, 8, encoder_factory=FakeEncoder, loop=asyncio.get_running_loop())
    stale = out._encoder
    out.set_bitrate(192)
    current = out._encoder
    out._restart_encoder(stale)
    assert out._encoder is current
    assert len(out._failures) == 0
    out.close()


async def test_add_listener_closes_the_dead_encoder_it_replaces() -> None:
    out = StreamOutput(320, 8, encoder_factory=FakeEncoder, loop=asyncio.get_running_loop())
    dead = out._encoder
    dead.alive = False
    out.add_listener(icy=False)
    assert dead._closed is True
    out.close()


class _BadCloseThenGoodFactory:
    """First encoder's close() raises; every later encoder is a plain FakeEncoder."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, bitrate: int) -> FakeEncoder:
        self.calls += 1
        if self.calls == 1:

            class BadClose(FakeEncoder):
                def close(self) -> None:
                    raise RuntimeError("boom")

            return BadClose(bitrate)
        return FakeEncoder(bitrate)


async def test_add_listener_swallows_dead_encoder_close_errors() -> None:
    factory = _BadCloseThenGoodFactory()
    out = StreamOutput(320, 8, encoder_factory=factory, loop=asyncio.get_running_loop())
    out._encoder.alive = False
    out.add_listener(icy=False)  # must not raise despite the dead encoder's close() blowing up
    assert out._encoder.alive is True
    out.close()


async def test_on_encoded_drops_data_from_a_superseded_encoder(output: StreamOutput) -> None:
    stale_encoder = output._encoder
    output.set_bitrate(192)
    mp3_out_before = output._mp3_out
    output._on_encoded(stale_encoder, b"x" * 64)
    assert output._mp3_out == mp3_out_before
    output._on_encoded(output._encoder, b"y" * 64)
    assert output._mp3_out == mp3_out_before + 64


class _CountingFactory:
    """Records how many encoders it has been asked to build."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, bitrate: int) -> FakeEncoder:
        self.calls += 1
        return FakeEncoder(bitrate)


async def test_add_listener_after_close_raises_without_spawning_encoder() -> None:
    factory = _CountingFactory()
    out = StreamOutput(320, 8, encoder_factory=factory, loop=asyncio.get_running_loop())
    out.close()
    calls_before = factory.calls
    with pytest.raises(EncoderUnavailableError):
        out.add_listener(icy=False)
    assert factory.calls == calls_before


async def test_write_after_close_is_a_noop() -> None:
    out = StreamOutput(320, 8, encoder_factory=FakeEncoder, loop=asyncio.get_running_loop())
    out.close()
    out.write(_block())
    assert len(out._pcm_queue) == 0


async def test_close_joins_writer_and_reader_threads() -> None:
    out = StreamOutput(320, 8, encoder_factory=FakeEncoder, loop=asyncio.get_running_loop())
    writer, reader = out._writer, out._reader
    out.close()
    assert not writer.is_alive()
    assert not reader.is_alive()


class ForeverBlockingEncoder(FakeEncoder):
    """Both write() and read() block until released; never notice close()."""

    def __init__(self, bitrate: int) -> None:
        super().__init__(bitrate)
        self._release = threading.Event()
        self.write_started = threading.Event()

    def write(self, pcm: bytes) -> None:
        self.write_started.set()
        self._release.wait()

    def read(self, n: int) -> bytes:
        self._release.wait()
        return b""

    def release(self) -> None:
        self._release.set()


async def test_close_logs_when_a_thread_outlives_the_join_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    out = StreamOutput(
        320, 8, encoder_factory=ForeverBlockingEncoder, loop=asyncio.get_running_loop()
    )
    encoder: ForeverBlockingEncoder = out._encoder
    out.write(_block())
    assert encoder.write_started.wait(1.0)
    try:
        with caplog.at_level("DEBUG", logger="autodj.stream"):
            out.close()  # both threads are stuck; each 5s join times out
        messages = [r.message for r in caplog.records]
        assert any("Stream writer thread did not stop" in m for m in messages)
        assert any("Stream reader thread did not stop" in m for m in messages)
    finally:
        encoder.release()
        out._writer.join(timeout=2)
        out._reader.join(timeout=2)


async def test_notify_callback_can_remove_a_listener_without_deadlock() -> None:
    out = StreamOutput(320, 8, encoder_factory=FakeEncoder, loop=asyncio.get_running_loop())
    listener = out.add_listener(icy=False)
    calls: list[int] = []

    def on_change(count: int) -> None:
        calls.append(count)
        if count > 0 and not listener.closed:
            out.remove_listener(listener)

    out.on_listener_change = on_change

    # Triggers a delivery whose callback removes a listener reentrantly.
    # Before the fix this deadlocked on _notify_lock forever; pytest's
    # per-test timeout bounds it either way, but a passing run proves it
    # returned rather than hung.
    out.add_listener(icy=False)

    assert calls == [2, 1]
    assert calls[-1] == out.listener_count == 1
    out.close()


async def test_notify_callback_exception_is_logged_not_propagated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    out = StreamOutput(320, 8, encoder_factory=FakeEncoder, loop=asyncio.get_running_loop())

    def bad_callback(count: int) -> None:
        raise RuntimeError("callback exploded")

    out.on_listener_change = bad_callback

    with caplog.at_level("ERROR", logger="autodj.stream"):
        listener = out.add_listener(icy=False)  # must not raise despite the callback

    assert any("Stream listener-count callback raised" in r.message for r in caplog.records)

    # _notify_lock must not be left held: a later notification still
    # has to go through promptly.
    good_calls: list[int] = []
    out.on_listener_change = good_calls.append
    out.remove_listener(listener)
    assert good_calls == [0]
    out.close()


async def test_notify_callback_exception_does_not_mask_the_original_exception() -> None:
    out = StreamOutput(320, 8, encoder_factory=FakeEncoder, loop=asyncio.get_running_loop())

    def bad_callback(count: int) -> None:
        raise RuntimeError("callback exploded")

    out.on_listener_change = bad_callback

    with pytest.raises(ValueError, match="boom"), out._locked():
        out._notify_due = True
        raise ValueError("boom")

    out.close()


async def test_notifications_never_overlap_and_final_count_is_correct() -> None:
    out = StreamOutput(320, 100, encoder_factory=FakeEncoder, loop=asyncio.get_running_loop())
    calls: list[int] = []
    active = 0
    max_concurrent = 0
    guard = threading.Lock()

    def on_change(count: int) -> None:
        nonlocal active, max_concurrent
        with guard:
            active += 1
            max_concurrent = max(max_concurrent, active)
        time.sleep(0.005)  # give a genuinely concurrent call a chance to overlap
        with guard:
            active -= 1
        calls.append(count)

    out.on_listener_change = on_change

    def add_and_remove() -> None:
        listener = out.add_listener(icy=False)
        out.remove_listener(listener)

    threads = [threading.Thread(target=add_and_remove) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert max_concurrent == 1  # callbacks never ran concurrently
    assert calls  # at least one delivery happened
    assert calls[-1] == out.listener_count == 0  # the last delivery reflects true final state
    out.close()


async def test_restart_set_bitrate_close_notify_without_holding_the_lock() -> None:
    out = StreamOutput(320, 8, encoder_factory=FakeEncoder, loop=asyncio.get_running_loop())
    lock_was_free: list[bool] = []

    def on_change(count: int) -> None:
        results: list[bool] = []

        def try_from_another_thread() -> None:
            got = out._lock.acquire(blocking=False)
            if got:
                out._lock.release()
            results.append(got)

        checker = threading.Thread(target=try_from_another_thread)
        checker.start()
        checker.join(timeout=1)
        lock_was_free.append(bool(results) and results[0])

    out.on_listener_change = on_change

    out.set_bitrate(192)
    out._restart_encoder()
    out.close()

    # set_bitrate, _restart_encoder and close each notify at least once
    # (disconnect_all always notifies), and none of them may still be
    # holding self._lock while the callback runs.
    assert len(lock_was_free) == 3
    assert all(lock_was_free)


async def test_set_bitrate_factory_failure_leaves_state_untouched() -> None:
    class RaisingFactory:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, bitrate: int) -> FakeEncoder:
            self.calls += 1
            if self.calls == 1:
                return FakeEncoder(bitrate)
            raise RuntimeError("ffmpeg missing")

    factory = RaisingFactory()
    out = StreamOutput(320, 8, encoder_factory=factory, loop=asyncio.get_running_loop())
    listener = out.add_listener(icy=False)
    original_encoder = out._encoder

    with pytest.raises(EncoderUnavailableError):
        out.set_bitrate(192)

    assert out._bitrate == 320
    assert out._encoder is original_encoder
    assert out.listener_count == 1
    assert not listener.closed
    out.close()


def test_requires_a_loop_outside_a_running_event_loop() -> None:
    with pytest.raises(RuntimeError):
        StreamOutput(320, 8, encoder_factory=FakeEncoder)


async def test_loop_defaults_to_the_running_loop_when_omitted() -> None:
    out = StreamOutput(320, 8, encoder_factory=FakeEncoder)
    assert out._loop is asyncio.get_running_loop()
    out.close()
