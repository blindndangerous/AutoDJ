from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj.mastering import (
    _MEASURED_PIPELINE_BLOCKS,
    _QUEUE_BLOCKS,
    _RUNNING_INPUT_BLOCKS,
    BLOCK_FRAMES,
    BLOCK_SECONDS,
    STEREOTOOL_MISSING,
    ContinuousMaster,
    _command,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is unavailable")

# The advertised pipeline delay in blocks; processed audio is due from here on.
_LATENCY_BLOCKS = 24


def _spawning() -> tuple[list[subprocess.Popen[bytes]], Callable[..., subprocess.Popen[bytes]]]:
    """A Popen factory that keeps every process it starts, in order."""
    spawned: list[subprocess.Popen[bytes]] = []

    def factory(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(*args, **kwargs)  # nosec B603 -- the master's own argv
        spawned.append(proc)
        return proc

    return spawned, factory


def _block(value: float = 0.0) -> np.ndarray:
    return np.full((BLOCK_FRAMES, 2), value, dtype=np.float32)


# Generous: the pipeline normally keeps up within a block or two.
_CATCH_UP_SECONDS = 30.0


def _caught_up(master: ContinuousMaster, *, cushion_due: bool) -> bool:
    with master._lock:
        if master._input:
            return False  # the pipe writer has not taken the last block yet
        if master._ready and not master._failed:
            # Live: the next processed block must exist, or the lifecycle
            # thread must first notice that the processor died.
            return bool(master._output) and master._output[0][0] == master._generation
        if cushion_due:
            return len(master._output) > master._output_guard_blocks
        return True


def _live_step(
    master: ContinuousMaster, block: np.ndarray, *, cushion_due: bool = False
) -> np.ndarray:
    """Feed one block once the master's own threads have caught up.

    The bus calls ``process`` every 20 ms and never waits; these tests keep
    that pace but also wait until FFmpeg and the pipe threads have handled
    the previous blocks.  On a busy machine they can fall a few blocks
    behind, which the master covers with delayed dry audio; that is
    scheduling, not the behaviour under test.  ``cushion_due`` marks
    the block at the advertised latency, by which the output cushion must
    be full.
    """
    deadline = time.monotonic() + _CATCH_UP_SECONDS
    while not _caught_up(master, cushion_due=cushion_due):
        assert time.monotonic() < deadline, "the master pipeline stopped making progress"
        time.sleep(0.002)
    return master.process(block)


def test_live_pipeline_processes_continuously_and_closes() -> None:
    spawned, factory = _spawning()
    master = ContinuousMaster(popen_factory=factory)
    output = []
    try:
        deadline = time.monotonic()
        for index in range(55):
            block = _block(0.03 if index >= 20 else 0.0)
            output.append(_live_step(master, block, cushion_due=index == _LATENCY_BLOCKS))
            deadline = max(deadline, time.monotonic()) + BLOCK_SECONDS
            time.sleep(max(0, deadline - time.monotonic()))
        audible = [sample for sample in output if np.any(sample)]
        assert audible
        assert all(np.isfinite(sample).all() for sample in audible)
        assert max(float(np.max(np.abs(sample))) for sample in audible) <= 0.871
        assert master.error is None
        assert len(spawned) == 1  # never restarted
    finally:
        start = time.monotonic()
        master.close()
        assert time.monotonic() - start < 2.5


def test_bypass_is_unchanged_and_reenable_flushes_old_audio() -> None:
    master = ContinuousMaster()
    try:
        bypass = _block(0.1)
        assert master.process(bypass, enabled=False) is bypass
        time.sleep(0.05)
        for _ in range(4):
            out = master.process(_block(0.0), enabled=True)
            assert np.isfinite(out).all()
            assert not np.any(out)
            time.sleep(BLOCK_SECONDS)
    finally:
        master.close()


def test_failed_ffmpeg_uses_limited_fallback_and_reports_status() -> None:
    def unavailable(*args, **kwargs):
        raise OSError("ffmpeg missing")

    master = ContinuousMaster(popen_factory=unavailable)
    try:
        deadline = time.monotonic() + 2
        while master.error is None and time.monotonic() < deadline:
            time.sleep(0.01)
        source = _block(0.5)
        result = master.process(source)
        assert master.error and "unavailable" in master.error
        assert np.max(np.abs(result)) <= 0.871
        assert np.isfinite(result).all()
    finally:
        master.close()


def test_killed_ffmpeg_recovers_without_dropping_the_live_timeline() -> None:
    spawned, factory = _spawning()
    master = ContinuousMaster(popen_factory=factory)
    outputs = []
    try:
        deadline = time.monotonic()
        for index in range(45):
            outputs.append(_live_step(master, _block(0.04), cushion_due=index == _LATENCY_BLOCKS))
            deadline = max(deadline, time.monotonic()) + BLOCK_SECONDS
            time.sleep(max(0, deadline - time.monotonic()))
        process = master._proc
        assert process is not None and process.poll() is None
        process.kill()
        for _ in range(100):
            outputs.append(_live_step(master, _block(0.04)))
            deadline = max(deadline, time.monotonic()) + BLOCK_SECONDS
            time.sleep(max(0, deadline - time.monotonic()))
        assert len(spawned) == 2  # restarted once
        assert master.error is None
        assert all(np.isfinite(block).all() for block in outputs)
        assert all(np.any(np.abs(block) > 1e-7) for block in outputs[_LATENCY_BLOCKS:])
        assert max(float(np.max(np.abs(block))) for block in outputs) <= 0.871
    finally:
        master.close()


def test_reader_backpressures_when_output_queue_is_full() -> None:
    master = ContinuousMaster()
    try:
        block = _block()
        generation = master._generation
        with master._output_cond:
            master._output.extend((generation, block.copy()) for _ in range(_QUEUE_BLOCKS))

        class FakeStdout:
            def __init__(self) -> None:
                self.remaining = 2

            def read(self, _size: int) -> bytes:
                if self.remaining == 0:
                    return b""
                self.remaining -= 1
                return block.tobytes()

        stop = threading.Event()
        reader = threading.Thread(
            target=master._read_loop,
            args=(SimpleNamespace(stdout=FakeStdout()), generation, stop),
        )
        reader.start()
        time.sleep(0.05)
        assert reader.is_alive()
        assert len(master._output) == _QUEUE_BLOCKS
        assert master.error is None
        with master._output_cond:
            master._output.clear()
            master._output_cond.notify_all()
        reader.join(timeout=1)
        assert not reader.is_alive()
        assert len(master._output) == 2
        assert master.error is None
    finally:
        master.close()


def test_invalid_block_shape_is_rejected() -> None:
    master = ContinuousMaster()
    try:
        with pytest.raises(ValueError, match="shape"):
            master.process(np.zeros((10, 2), dtype=np.float32))
    finally:
        master.close()


def test_stereotool_runs_our_host_with_the_library_and_the_packaged_preset() -> None:
    import sys

    command = _command("stereotool", "C:/Audio Tools/libStereoTool_64.dll", None, "/tmp/st")
    assert command[:3] == [sys.executable, "-m", "autodj.stereotool_host"]
    assert command[command.index("--library") + 1] == "C:/Audio Tools/libStereoTool_64.dll"
    assert command[command.index("--home") + 1] == "/tmp/st"
    assert Path(command[command.index("--preset") + 1]).is_file()


@pytest.mark.parametrize("partial_write", [False, True])
def test_stereotool_writer_preserves_samples_across_unaligned_bus_blocks(partial_write) -> None:
    master = ContinuousMaster(mode=lambda: "off")
    master.close()
    stop = threading.Event()
    source = np.arange(BLOCK_FRAMES * 2 * 5, dtype=np.float32).tobytes()
    received = bytearray()
    requested = []
    complete_bytes = len(source) // 8192 * 8192

    class FakeStdin:
        def write(self, data):
            requested.append(len(data))
            count = min(2048, len(data)) if partial_write else len(data)
            received.extend(data[:count])
            if len(received) == complete_bytes:
                stop.set()
            return count

    try:
        master._mode = "stereotool"
        master._enabled = True
        block_bytes = BLOCK_FRAMES * 2 * 4
        master._input.append((-1, b"stale data"))
        master._input.extend(
            (0, source[offset : offset + block_bytes])
            for offset in range(0, len(source), block_bytes)
        )
        master._write_loop(SimpleNamespace(stdin=FakeStdin()), 0, stop)
        assert received == source[:complete_bytes]
        assert requested == ([8192, 6144, 4096, 2048] if partial_write else [8192]) * 4
    finally:
        master.close()


def test_switching_processors_preserves_delayed_fallback() -> None:
    master = ContinuousMaster(mode=lambda: "off")
    master.close()
    with master._lock:
        master._enabled = True
        master._fallback_delay.extend(_block(0.03) for _ in range(25))
        master._input.append((0, b"stale"))
        master._output.append((0, _block(0.1)))
        master._owed_blocks = 2
        master._stall_started = 100.0
        master._switch(True, "stereotool")
        assert not master._input and not master._output
        assert master._owed_blocks == 0
        assert master._stall_started is None
        assert master._recovering
        assert np.any(master._delayed_fallback())
        master._switch(False)
        assert not master._fallback_delay


def test_old_writer_leaves_new_processor_input_queued() -> None:
    master = ContinuousMaster(mode=lambda: "off")
    master.close()
    master._generation = 1
    master._enabled = True
    master._input.append((1, _block(0.1).tobytes()))
    master._write_loop(SimpleNamespace(stdin=object()), 0, threading.Event())
    assert len(master._input) == 1


@pytest.mark.parametrize("mode", ["smooth", "stereotool"])
def test_output_cushion_absorbs_jitter_then_recovers_from_real_underrun(
    mode, monkeypatch, caplog
) -> None:
    master = ContinuousMaster(mode=lambda: "off")
    master.close()
    # Run the consumer without a worker so the output pause is deterministic.
    master._closed = False
    master._select_mode = lambda: mode
    now = [100.0]
    monkeypatch.setattr("autodj.mastering.time.monotonic", lambda: now[0])
    caplog.set_level("INFO", logger="autodj.mastering")
    try:
        with master._lock:
            master._switch(True, mode)
            generation = master._generation
            available = master._output_guard_blocks + 1
            master._output.extend((generation, _block(0.4 + i / 1000)) for i in range(available))
            master._fallback_delay.extend(_block(i / 1000) for i in range(24))
            for i in range(available):
                np.testing.assert_array_equal(
                    master.process(_block((i + 24) / 1000)), _block(0.4 + i / 1000)
                )
            assert master.error is None
            # An earlier overflow must not suppress the stall's own warning.
            master._set_error("master input queue overflow; dropped oldest block")
            for i in range(available, available + 3):
                now[0] += BLOCK_SECONDS
                np.testing.assert_allclose(
                    master.process(_block((i + 24) / 1000)),
                    _block(i / 1000 * 10 ** (9.01 / 20)),
                )
                assert "using delayed dry audio" in master.error
                assert master._generation == generation
            assert master._owed_blocks == 3
            assert len(master._input) == available + 3

            # A partial answer pays some debt but still cannot cover this period.
            master._output.append((generation, _block(0.4 + available / 1000)))
            now[0] += BLOCK_SECONDS
            i = available + 3
            np.testing.assert_allclose(
                master.process(_block((i + 24) / 1000)),
                _block(i / 1000 * 10 ** (9.01 / 20)),
            )
            assert master._owed_blocks == 3
            master._output.extend(
                (generation, _block(0.4 + i / 1000)) for i in range(available + 1, available + 6)
            )
            for i in range(available + 4, available + 6):
                now[0] += BLOCK_SECONDS
                np.testing.assert_array_equal(
                    master.process(_block((i + 24) / 1000)), _block(0.4 + i / 1000)
                )
            assert master._owed_blocks == 0
            assert master._generation == generation
            assert master.error is None
            assert caplog.text.count("using delayed dry audio") == 1
            assert "covered 4 blocks; longest stall 0.080 s" in caplog.text

            master.process(_block())
            assert caplog.text.count("using delayed dry audio") == 1
    finally:
        master.close()


def test_sustained_output_stall_restarts_after_limit(monkeypatch) -> None:
    master = ContinuousMaster(mode=lambda: "off")
    master.close()
    master._closed = False
    master._select_mode = lambda: "smooth"
    now = [100.0]
    monkeypatch.setattr("autodj.mastering.time.monotonic", lambda: now[0])
    try:
        with master._lock:
            master._switch(True, "smooth")
        master._ready = True
        master._fallback_delay.extend(_block(0.03) for _ in range(24))
        generation = master._generation
        for i in range(150):
            now[0] = 100.0 + i * BLOCK_SECONDS
            assert np.any(master.process(_block(0.03)))
            assert master._generation == generation
        # Ready input buffering must outlast the short warmup queue.
        assert len(master._input) == 150
        assert master._owed_blocks == 150
        now[0] = 103.01
        assert np.any(master.process(_block(0.03)))
        assert master._generation == generation + 1
        assert "restarting processor" in master.error
        assert master._recovering and not master._ready
        assert not master._input and not master._output
        assert master._owed_blocks == 0
    finally:
        master.close()


def test_trickled_output_restarts_at_stall_age_limit(monkeypatch) -> None:
    master = ContinuousMaster(mode=lambda: "off")
    master.close()
    master._closed = False
    master._select_mode = lambda: "smooth"
    now = [100.0]
    monkeypatch.setattr("autodj.mastering.time.monotonic", lambda: now[0])
    try:
        with master._lock:
            master._switch(True, "smooth")
        master._ready = True
        master._fallback_delay.extend(_block(0.03) for _ in range(24))
        generation = master._generation
        master.process(_block(0.03))
        for now[0] in (102.9, 105.8, 108.7, 109.99):
            master._output.append((generation, _block(0.8)))
            np.testing.assert_allclose(
                master.process(_block(0.03)), _block(0.03 * 10 ** (9.01 / 20))
            )
            assert master._generation == generation
            assert master._owed_blocks == 1
        now[0] = 110.0
        master._output.append((generation, _block(0.8)))
        np.testing.assert_allclose(master.process(_block(0.03)), _block(0.03 * 10 ** (9.01 / 20)))
        assert master._generation == generation + 1
        assert "restarting processor" in master.error
        assert master._recovering and not master._ready
    finally:
        master.close()


def test_stall_logs_are_rate_limited_with_totals_and_immediate_status(monkeypatch, caplog):
    master = ContinuousMaster(mode=lambda: "off")
    master.close()
    master._closed = False
    master._select_mode = lambda: "smooth"
    now = [100.0]
    monkeypatch.setattr("autodj.mastering.time", SimpleNamespace(monotonic=lambda: now[0]))
    caplog.set_level("INFO", logger="autodj.mastering")
    try:
        with master._lock:
            master._switch(True, "smooth")
        master._ready = True
        generation = master._generation
        for start, blocks in ((100.0, 1), (102.0, 2), (104.0, 3), (110.0, 1)):
            for offset in range(blocks):
                now[0] = start + offset * BLOCK_SECONDS
                master.process(_block(0.01))
                assert master.error == "master output stalled; using delayed dry audio"
            now[0] = start + blocks * BLOCK_SECONDS
            master._output.extend((generation, _block(0.01)) for _ in range(blocks + 1))
            master.process(_block(0.01))
            assert master.error is None
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        recoveries = [r for r in caplog.records if "stall ended" in r.message]
        assert len(warnings) == len(recoveries) == 2
        assert "stalls: 3; covered 6 blocks; longest stall 0.060 s" in warnings[1].message
        assert "stalls: 3; covered 6 blocks; longest stall 0.060 s" in recoveries[1].message
        assert master._generation == generation
    finally:
        master.close()


def test_gated_processor_stall_preserves_every_delayed_input(monkeypatch) -> None:
    """Real pipe threads must discard covered output, then resume at the advertised delay."""
    now = [100.0]
    monkeypatch.setattr("autodj.mastering.time", SimpleNamespace(monotonic=lambda: now[0]))

    class InputPipe:
        def __init__(self):
            self.blocks = queue.Queue()

        def write(self, data):
            self.blocks.put(bytes(data))
            return len(data)

        def close(self):
            self.blocks.put(b"")

    class OutputPipe:
        def __init__(self, stdin):
            self.stdin = stdin
            self.gate = threading.Semaphore(0)
            self.entered = threading.Event()
            self.condition = threading.Condition()
            self.completed = 0
            self.returned = False
            self.closed = False

        def read(self, size):
            # Entering the next read acknowledges that the real reader has
            # enqueued the previous block. No production queues are edited.
            with self.condition:
                if self.returned:
                    self.completed += 1
                self.condition.notify_all()
            self.entered.set()
            self.gate.acquire()
            if self.closed:
                return b""
            data = self.stdin.blocks.get()
            assert len(data) <= size
            self.returned = bool(data)
            return data

        def advance(self, blocks):
            with self.condition:
                target = self.completed + blocks
                self.gate.release(blocks)
                assert self.condition.wait_for(lambda: self.completed == target, timeout=5), (
                    "processor reader did not finish the released blocks"
                )

        def close(self):
            self.closed = True
            self.gate.release()

    class Processor:
        def __init__(self):
            self.stdin = InputPipe()
            self.stdout = OutputPipe(self.stdin)
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0
            self.stdin.close()
            self.stdout.close()

        kill = terminate

        def wait(self, timeout=None):
            return self.returncode

    spawned = []
    created = threading.Event()

    def factory(*args, **kwargs):
        child = Processor()
        spawned.append(child)
        created.set()
        return child

    master = ContinuousMaster(popen_factory=factory)
    heard = []
    dry_at = []
    processed_at = []
    inputs = []
    try:
        # Wait for lifecycle initialization before feeding the first block.
        assert created.wait(timeout=5)
        child = spawned[0]
        assert child.stdout.entered.wait(timeout=5)
        delay = _MEASURED_PIPELINE_BLOCKS
        for index in range(150):
            now[0] = 100.0 + index * BLOCK_SECONDS
            source = _block((index + 1) / 1000)
            source[:, 1] = 0.9 if index % 2 else -0.9  # Exercise the dry limiter too.
            inputs.append(source.copy())
            output = master.process(source)
            if index < delay:
                assert not np.any(output)
            else:
                expected = inputs[index - delay]
                if np.array_equal(output, expected):
                    processed_at.append(index)
                    decoded = float(output[0, 0])
                else:
                    dry_at.append(index)
                    np.testing.assert_allclose(
                        output, np.clip(expected * 10 ** (9.01 / 20), -0.87096359, 0.87096359)
                    )
                    decoded = float(output[0, 0]) / 10 ** (9.01 / 20)
                heard.append(round(decoded * 1000) - 1)
                assert (index - heard[-1]) * BLOCK_SECONDS == pytest.approx(master.latency_seconds)
            # One second of processor silence, followed by up to three blocks
            # per bus period until caught up. The gate, not wall time, sets pace.
            if not 40 <= index < 90:
                child.stdout.advance(min(3, index + 1 - child.stdout.completed))
        assert dry_at and processed_at[0] < dry_at[0] < dry_at[-1] < processed_at[-1]
        assert heard == list(range(150 - delay))  # No repeated or missing input indices.
        assert master.error is None
        assert len(spawned) == 1
    finally:
        assert master.close()


def test_ready_input_overflow_reduces_debt_and_preserves_alignment(monkeypatch) -> None:
    # A smaller running bound exercises the exceptional drop before the timeout.
    monkeypatch.setattr("autodj.mastering._RUNNING_INPUT_BLOCKS", 26)
    master = ContinuousMaster(mode=lambda: "off")
    master.close()
    master._closed = False
    master._select_mode = lambda: "smooth"
    monkeypatch.setattr("autodj.mastering.time.monotonic", lambda: 100.0)
    try:
        with master._lock:
            master._switch(True, "smooth")
        master._ready = True
        generation = master._generation
        master._fallback_delay.extend(_block(i / 1000) for i in range(24))
        for i in range(40):
            np.testing.assert_allclose(
                master.process(_block((i + 24) / 1000)),
                _block(i / 1000 * 10 ** (9.01 / 20)),
            )
        assert len(master._input) == 26
        assert master._owed_blocks == 26  # 40 dry periods, 14 inputs never processed
        assert master._generation == generation
        assert master._warmup_audio_drops == master._warmup_silent_drops == 0
        # The writer has taken the surviving inputs. Blocks 24..37 were dropped;
        # blocks 0..23 were already in the processor when the stall began.
        master._input.clear()
        master._output.extend(
            (generation, _block(0.4 + i / 1000)) for i in (*range(24), 38, 39, 40, 41)
        )
        for i in (40, 41):
            np.testing.assert_array_equal(
                master.process(_block((i + 24) / 1000)), _block(0.4 + i / 1000)
            )
        assert master._owed_blocks == 0
        assert master.error is None
    finally:
        master.close()


def test_stall_debt_larger_than_output_queue_releases_reader(monkeypatch) -> None:
    import io

    master = ContinuousMaster(mode=lambda: "off")
    master.close()
    master._closed = False
    master._select_mode = lambda: "smooth"
    monkeypatch.setattr("autodj.mastering.time.monotonic", lambda: 100.0)
    try:
        with master._lock:
            master._switch(True, "smooth")
        master._ready = True
        generation = master._generation
        master._fallback_delay.extend(_block(i / 1000) for i in range(24))
        for i in range(45):
            master.process(_block((i + 24) / 1000))
        assert master._owed_blocks == 45
        master._input.clear()
        waits = []

        def consume_when_backpressured(timeout=None):
            # Run the consumer at the reader's wait point, without threads or
            # sleeps. It must free space even though debt exceeds the queue.
            waits.append(len(master._output))
            assert waits == [_QUEUE_BLOCKS]
            np.testing.assert_allclose(
                master.process(_block(0.069)), _block(0.045 * 10 ** (9.01 / 20))
            )
            assert not master._output
            return True

        monkeypatch.setattr(master._output_cond, "wait", consume_when_backpressured)
        stdout = io.BytesIO(b"".join(_block(0.4 + i / 1000).tobytes() for i in range(48)))
        master._read_loop(SimpleNamespace(stdout=stdout), generation, threading.Event())
        assert waits == [_QUEUE_BLOCKS]
        assert len(master._output) == 10
        for i in (46, 47):
            np.testing.assert_array_equal(
                master.process(_block((i + 24) / 1000)), _block(0.4 + i / 1000)
            )
        assert master._owed_blocks == 0
        assert master._generation == generation
        assert master.error is None
    finally:
        master.close()


def test_a_processor_faster_than_measured_still_waits_out_the_advertised_delay() -> None:
    master = ContinuousMaster(mode=lambda: "off")
    master.close()
    # Run the consumer without a worker: the processed audio is all there at once.
    master._closed = False
    master._select_mode = lambda: "smooth"
    try:
        with master._lock:
            master._switch(True, "smooth")
            generation = master._generation
            master._output.extend((generation, _block(0.1)) for _ in range(_QUEUE_BLOCKS))
            outputs = [master.process(_block(0.03)) for _ in range(_LATENCY_BLOCKS + 1)]
        assert not any(np.any(block) for block in outputs[:_LATENCY_BLOCKS])
        np.testing.assert_array_equal(outputs[_LATENCY_BLOCKS], _block(0.1))
        assert master.error is None
    finally:
        master.close()


def test_installed_stereotool_preserves_tone_without_periodic_gaps() -> None:
    library = os.environ.get("AUTODJ_TEST_STEREOTOOL_LIBRARY")
    if not library:
        pytest.skip("Set AUTODJ_TEST_STEREOTOOL_LIBRARY to test Thimeo's Stereo Tool library")
    spawned, factory = _spawning()
    master = ContinuousMaster(
        mode=lambda: "stereotool", stereotool_path=library, popen_factory=factory
    )
    outputs = []
    try:
        started = time.monotonic()
        for index in range(350):
            samples = np.arange(BLOCK_FRAMES) + index * BLOCK_FRAMES
            tone = (0.08 * np.sin(2 * np.pi * 997 * samples / 44100)).astype(np.float32)
            outputs.append(master.process(np.column_stack((tone, tone))))
            time.sleep(max(0, started + (index + 1) * BLOCK_SECONDS - time.monotonic()))
        assert master.error is None
        assert len(spawned) == 2  # never restarted
        assert all(np.any(block) for block in outputs[30:])
        audio = np.concatenate(outputs)
        assert np.isfinite(audio).all()
        assert np.max(np.abs(audio)) <= 1
        spectrum = np.abs(np.fft.rfft(audio[-44100:, 0] * np.hanning(44100)))
        assert np.argmax(spectrum) == 997
    finally:
        master.close()


def test_stereotool_output_lifts_quiet_audio_and_limits_peaks() -> None:
    from scipy.signal import resample_poly

    from autodj.mastering import _STEREOTOOL_OUTPUT_FILTER, _ffmpeg_command

    samples = np.arange(44100 * 2)
    low = 0.03 * np.sin(2 * np.pi * 997 * samples / 44100)
    loud = 0.8 * np.sin(2 * np.pi * 997 * samples / 44100)
    source = np.column_stack((np.concatenate((low, loud)),) * 2).astype(np.float32)
    result = subprocess.run(
        _ffmpeg_command(_STEREOTOOL_OUTPUT_FILTER),
        input=source.tobytes(),
        capture_output=True,
        check=True,
        timeout=10,
    )
    output = np.frombuffer(result.stdout, dtype=np.float32).reshape(-1, 2)
    assert output.shape == source.shape
    assert np.isfinite(output).all()
    interior = slice(22050, 66150)
    gain = np.sqrt(np.mean(output[interior] ** 2) / np.mean(source[interior] ** 2))
    assert 20 * np.log10(gain) == pytest.approx(5.8, abs=0.05)
    assert np.max(np.abs(resample_poly(output, 4, 1, axis=0))) <= 10 ** (-1 / 20)


def test_stereotool_output_stage_adds_no_sample_delay() -> None:
    from autodj.mastering import _STEREOTOOL_OUTPUT_FILTER, _ffmpeg_command

    impulse = np.zeros((44100, 2), dtype=np.float32)
    impulse[10_000] = 0.2
    result = subprocess.run(
        _ffmpeg_command(_STEREOTOOL_OUTPUT_FILTER),
        input=impulse.tobytes(),
        capture_output=True,
        check=True,
        timeout=10,
    )
    output = np.frombuffer(result.stdout, dtype=np.float32).reshape(-1, 2)
    assert output.shape == impulse.shape
    assert int(np.argmax(np.abs(output[:, 0]))) == 10_000


@pytest.mark.parametrize("signal", ["noise", "square"])
def test_stereotool_output_true_peak_stays_below_ceiling_after_resample(signal: str) -> None:
    from scipy.signal import resample_poly

    from autodj.mastering import _STEREOTOOL_OUTPUT_FILTER, _ffmpeg_command

    samples = np.arange(44100 * 3)
    if signal == "noise":
        hot = np.clip(np.random.default_rng(1).normal(0, 0.4, len(samples)), -1, 1)
    else:
        hot = 0.9 * np.sign(np.sin(2 * np.pi * 1000 * samples / 44100))
    source = np.column_stack((hot, hot)).astype(np.float32)
    result = subprocess.run(
        _ffmpeg_command(_STEREOTOOL_OUTPUT_FILTER),
        input=source.tobytes(),
        capture_output=True,
        check=True,
        timeout=10,
    )
    output = np.frombuffer(result.stdout, dtype=np.float32).reshape(-1, 2)[22050:]
    true_peak = np.max(np.abs(resample_poly(output, 4, 1, axis=0)))
    assert 20 * np.log10(true_peak) <= -1.2


def test_output_limiter_start_failure_cleans_up_stereotool() -> None:
    primary = MagicMock()
    primary.poll.return_value = None
    calls = 0

    def factory(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return primary
        raise OSError("output limiter missing")

    master = ContinuousMaster(popen_factory=factory, mode=lambda: "stereotool")
    try:
        deadline = time.monotonic() + 2
        while master.error is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert master.error and "output limiter missing" in master.error
        primary.terminate.assert_called_once()
        primary.stdin.close.assert_called_once()
        primary.stdout.close.assert_called_once()
        assert np.isfinite(master.process(_block(0.5))).all()
    finally:
        master.close()


def test_installed_stereotool_recovers_when_output_limiter_exits() -> None:
    library = os.environ.get("AUTODJ_TEST_STEREOTOOL_LIBRARY")
    if not library:
        pytest.skip("Set AUTODJ_TEST_STEREOTOOL_LIBRARY to test Thimeo's Stereo Tool library")
    spawned, factory = _spawning()
    master = ContinuousMaster(
        mode=lambda: "stereotool", stereotool_path=library, popen_factory=factory
    )
    outputs = []
    try:
        started = time.monotonic()
        for index in range(220):
            if index == 90:
                spawned[1].kill()  # the output limiter, started after Stereo Tool's host
            t = np.arange(BLOCK_FRAMES) + index * BLOCK_FRAMES
            tone = (0.03 * np.sin(2 * np.pi * 997 * t / 44100)).astype(np.float32)
            outputs.append(master.process(np.column_stack((tone, tone))))
            time.sleep(max(0, started + (index + 1) * BLOCK_SECONDS - time.monotonic()))
        assert len(spawned) == 4  # both processes restarted once
        assert master.error is None
        assert all(np.max(np.abs(block)) > 1e-5 for block in outputs[40:])
        assert np.isfinite(np.concatenate(outputs)).all()
    finally:
        master.close()
    assert all(proc.poll() is not None for proc in spawned)


def test_a_second_close_returns_at_once() -> None:
    master = ContinuousMaster()
    master.close()
    start = time.monotonic()
    master.close()  # a failed start's clean-up, then a stop
    assert time.monotonic() - start < 0.1
    assert not master._worker.is_alive()


class _Host:
    """A stand-in Stereo Tool host process: records stdin, says *reports* on stderr."""

    def __init__(self, reports: list[bytes], code: int | None = None) -> None:
        import io

        self.stdin = io.BytesIO()
        self.stdin.close = lambda: None  # keep what was written readable
        self.stdout = MagicMock()
        self.stdout.read.return_value = b""
        self.stderr = io.BytesIO(b"".join(reports))
        self.returncode = code
        self.kwargs: dict = {}

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.returncode = self.returncode if self.returncode is not None else -15

    def kill(self):
        self.terminate()


def _report(**values) -> bytes:
    import json

    from autodj.stereotool_host import REPORT_PREFIX

    return (REPORT_PREFIX + json.dumps(values) + "\n").encode()


def _wait_for(condition, seconds: float = 3.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_the_licence_key_goes_over_stdin_never_argv_or_the_environment(monkeypatch) -> None:
    from autodj.stereotool_host import read_header

    monkeypatch.setenv("AUTODJ_STEREOTOOL_LICENSE_KEY", "<KEY-FROM-ENV-1234>")
    monkeypatch.setenv("AUTODJ_ACCESS_TOKEN", "server-secret-token-for-tests-only")
    hosts: list[_Host] = []

    def factory(command, **kwargs):
        if "autodj.stereotool_host" not in command:
            raise OSError("no output limiter in this test")
        host = _Host([_report(version="11.05", latency_ms=10.0)])
        host.kwargs = kwargs
        host.command = command
        hosts.append(host)
        return host

    master = ContinuousMaster(
        popen_factory=factory,
        mode=lambda: "stereotool",
        stereotool_path=lambda: "/index/tools/stereo-tool/libStereoTool_intel64.so",
        stereotool_license=lambda: "<KEY-SECRET-1234>",
    )
    try:
        _wait_for(lambda: bool(hosts) and len(hosts[0].stdin.getvalue()) > 10)
        host = hosts[0]
        assert not any("KEY-SECRET" in part for part in host.command)
        env = host.kwargs["env"]
        assert not [name for name in env if name.upper().startswith("AUTODJ_")]
        assert "<KEY-SECRET-1234>" not in "".join(env.values())
        assert host.kwargs["stderr"] is subprocess.PIPE
        import io

        assert read_header(io.BytesIO(host.stdin.getvalue())).decode() == "<KEY-SECRET-1234>"
    finally:
        master.close()


def test_stereotool_restarts_with_the_current_library_and_licence() -> None:
    commands: list[list[str]] = []
    library = ["./first/libStereoTool_intel64.so"]
    generation = [0]

    def factory(command, **_kwargs):
        commands.append(command)
        raise OSError("stop here")

    master = ContinuousMaster(
        popen_factory=factory,
        mode=lambda: "stereotool",
        stereotool_path=lambda: library[0],
        setup_generation=lambda: generation[0],
    )
    try:
        _wait_for(lambda: bool(commands))
        assert commands[0][commands[0].index("--library") + 1] == "./first/libStereoTool_intel64.so"
        # A saved key or a new library bumps the generation: Stereo Tool restarts.
        library[0] = "/index/tools/stereo-tool/libStereoTool_intel64.so"
        generation[0] = 1
        master.process(_block())
        _wait_for(lambda: commands[-1][commands[-1].index("--library") + 1] == library[0])
        assert commands[-1][commands[-1].index("--library") + 1] == library[0]
    finally:
        master.close()


def test_a_missing_library_points_to_the_download_button_without_starting_a_host() -> None:
    started = []

    def factory(command, **_kwargs):
        started.append(command)
        raise AssertionError("no host starts without its library")

    master = ContinuousMaster(
        popen_factory=factory,
        mode=lambda: "stereotool",
        library_present=lambda _path: False,
    )
    try:
        _wait_for(lambda: master.error is not None)
        assert master.error == (
            "Stereo Tool master unavailable: Stereo Tool is not installed. Download it in "
            "Settings, Sound, with the Download Stereo Tool button."
        )
        assert started == []
    finally:
        master.close()


def test_the_hosts_reports_are_kept_and_its_last_error_is_said() -> None:
    def factory(command, **_kwargs):
        if "autodj.stereotool_host" not in command:
            raise OSError("no output limiter in this test")
        return _Host(
            [
                b"Stereo Tool library chatter that is not ours\n",
                b"partial chatter " + _report(error="Stereo Tool could not load the preset A.sts"),
            ],
            code=4,
        )

    master = ContinuousMaster(popen_factory=factory, mode=lambda: "stereotool")
    try:
        _wait_for(lambda: bool(master.stereotool_report.get("error")))
        assert master.stereotool_report["error"] == "Stereo Tool could not load the preset A.sts"
    finally:
        master.close()


def test_a_host_that_exits_after_saying_why_shows_that_reason() -> None:
    class Exiting(_Host):
        pass

    def factory(command, **_kwargs):
        if "autodj.stereotool_host" not in command:
            output = MagicMock()
            output.poll.return_value = None
            output.stdout.read.return_value = b""
            return output
        return Exiting([_report(error="libX.so is not there", missing=True)], code=3)

    master = ContinuousMaster(
        popen_factory=factory,
        mode=lambda: "stereotool",
    )
    try:
        _wait_for(lambda: master.error is not None and "not installed" in master.error)
        assert master.error == f"Stereo Tool master unavailable: {STEREOTOOL_MISSING}"
    finally:
        master.close()


def test_licence_reports_are_logged_without_any_key(caplog) -> None:
    import logging

    master = ContinuousMaster(mode=lambda: "smooth")
    try:
        with caplog.at_level(logging.INFO, logger="autodj.mastering"):
            master._note_report(
                {"version": "11.05", "latency_ms": 12.5, "preset_load_type": 20386, "ignored": "x"}
            )
            master._note_report({"licensed": False, "unlicensed": "Declipper"})
        assert master.stereotool_report == {
            "version": "11.05",
            "latency_ms": 12.5,
            "preset_load_type": 20386,
            "licensed": False,
            "unlicensed": "Declipper",
        }
        text = " ".join(record.getMessage() for record in caplog.records)
        assert "Stereo Tool 11.05 started" in text
        assert "preset load type 20386" in text
        assert "not licensed for this preset: Declipper" in text
    finally:
        master.close()


def test_a_stuck_process_is_logged_without_its_command_line(caplog) -> None:
    import logging

    from autodj.mastering import _end_process

    proc = MagicMock()
    proc.poll.return_value = None
    proc.wait.side_effect = subprocess.TimeoutExpired(["python", "-m", "secret-ish argv"], 2)
    with caplog.at_level(logging.WARNING, logger="autodj.mastering"):
        assert _end_process(proc) is False
    assert caplog.records
    assert all(
        "secret-ish" not in record.getMessage() and record.exc_info is None
        for record in caplog.records
    )


def test_a_report_is_tagged_with_the_generation_it_started_with() -> None:
    generation = [7]

    def factory(command, **_kwargs):
        if "autodj.stereotool_host" not in command:
            raise OSError("no output limiter in this test")
        return _Host([_report(version="11.05")])

    master = ContinuousMaster(
        popen_factory=factory, mode=lambda: "stereotool", setup_generation=lambda: generation[0]
    )
    try:
        _wait_for(lambda: master.stereotool_report.get("version") == "11.05")
        assert master.stereotool_report["generation"] == 7
    finally:
        master.close()


def test_a_helper_that_stops_reading_its_header_says_its_own_reason() -> None:
    import io

    class Closed(_Host):
        def __init__(self) -> None:
            super().__init__(
                [_report(error="libStereoTool_intel64.so is not there", missing=True)], 3
            )

            class Pipe(io.RawIOBase):
                def writable(self) -> bool:
                    return True

                def write(self, _data) -> int:
                    raise BrokenPipeError(32, "Broken pipe")

            self.stdin = Pipe()

    def factory(command, **_kwargs):
        return Closed()

    master = ContinuousMaster(
        popen_factory=factory,
        mode=lambda: "stereotool",
    )
    try:
        _wait_for(lambda: master.error is not None)
        assert master.error == f"Stereo Tool master unavailable: {STEREOTOOL_MISSING}"
        assert "Broken pipe" not in master.error
    finally:
        master.close()


def test_an_os_error_with_a_file_names_only_the_file() -> None:
    from autodj.mastering import _unavailable

    error = PermissionError(13, "Permission denied", "/srv/autodj/index/tools/stereo-tool/x.so")
    assert _unavailable("stereotool", error) == "x.so: Permission denied"


@pytest.mark.parametrize("mode", ["smooth", "stereotool"])
def test_slow_start_trims_only_silence_and_reports_first_input(mode, caplog, monkeypatch):
    master = ContinuousMaster(mode=lambda: "off")
    master.close()
    master._closed = False
    master._select_mode = lambda: mode
    now = [100.0]
    monkeypatch.setattr("autodj.mastering.time.monotonic", lambda: now[0])
    caplog.set_level("INFO", logger="autodj.mastering")
    try:
        with master._lock:
            master._switch(True, mode)
        for _ in range(_QUEUE_BLOCKS + 50):
            assert not np.any(master.process(_block()))
        assert len(master._input) == _QUEUE_BLOCKS
        assert master._warmup_silent_drops == 50
        assert master.error is None
        assert not caplog.records

        # A delayed processor catches up while the bus still carries silence.
        master._input.clear()
        generation = master._generation
        master._output.extend(
            (generation, _block(1e-12)) for _ in range(master._output_guard_blocks + 1)
        )
        now[0] = 105.0
        np.testing.assert_array_equal(master.process(_block()), _block(1e-12))
        assert "ready after 5.000 s; discarded 50 silent and 0 non-silent" in caplog.text
        assert "First non-silent master input" not in caplog.text
        master._output.clear()
        first_song = _block(0.025)
        now[0] = 105.2
        master._output.append((generation, _block()))
        master.process(first_song)
        now[0] = 105.7
        master._output.append((generation, first_song))
        np.testing.assert_array_equal(master.process(first_song), first_song)
        assert "First non-silent master input after 5.200 s" in caplog.text
        master._output.append((generation, first_song))
        master.process(first_song)
        assert caplog.text.count("First non-silent master input") == 1
        assert master.error is None
    finally:
        master.close()


@pytest.mark.parametrize("phase", ["quiet_music", "after_music", "ready", "recovering"])
def test_warmup_does_not_hide_program_or_running_overflows(phase, caplog):
    master = ContinuousMaster(mode=lambda: "off")
    master.close()
    master._closed = False
    master._select_mode = lambda: "smooth"
    try:
        with master._lock:
            master._switch(True, "smooth")
        if phase == "after_music":
            master.process(_block(0.1))
            master._input.clear()
        master._ready = phase == "ready"
        master._recovering = phase == "recovering"
        queued = _block(1e-12 if phase == "quiet_music" else 0).tobytes()
        limit = _RUNNING_INPUT_BLOCKS if master._ready else _QUEUE_BLOCKS
        master._input.extend((master._generation, queued) for _ in range(limit))
        if master._ready:
            master._output.append((master._generation, _block()))
        master.process(_block(1e-12 if phase == "quiet_music" else 0))
        assert "master input queue overflow; dropped oldest block" in caplog.text
        assert master.error == "master input queue overflow; dropped oldest block"
        assert master._warmup_audio_drops == (1 if phase == "quiet_music" else 0)
        assert master._warmup_silent_drops == (1 if phase in {"after_music", "recovering"} else 0)
        assert len(master._input) == limit
    finally:
        master.close()


@pytest.mark.parametrize("release", ["ready", "failed", "recovering", "disabled", "timeout"])
def test_startup_hold_is_bounded_and_never_holds_recovery(release, monkeypatch):
    master = ContinuousMaster(mode=lambda: "off")
    master.close()
    master._closed = False
    master._select_mode = lambda: "smooth"
    now = [10.0]
    monkeypatch.setattr("autodj.mastering.time.monotonic", lambda: now[0])
    try:
        with master._lock:
            master._switch(True, "smooth")
        assert master.startup_pending
        if release == "timeout":
            now[0] += 5.0
        elif release == "disabled":
            master._enabled = False
        else:
            setattr(master, f"_{release}", True)
        assert not master.startup_pending
        if release == "timeout":
            assert "exceeded 5 seconds" in master.error
            # The processor continues receiving audio while bounded bypass plays.
            for _ in range(25):
                output = master.process(_block(0.01))
            assert np.any(output)
            assert len(master._input) == 25
            assert not master.startup_pending
    finally:
        master.close()
