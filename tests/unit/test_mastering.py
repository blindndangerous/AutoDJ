from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from autodj.mastering import _QUEUE_BLOCKS, BLOCK_FRAMES, BLOCK_SECONDS, ContinuousMaster, _command

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is unavailable")


def _block(value: float = 0.0) -> np.ndarray:
    return np.full((BLOCK_FRAMES, 2), value, dtype=np.float32)


def test_live_pipeline_processes_continuously_and_closes() -> None:
    master = ContinuousMaster()
    output = []
    try:
        for index in range(55):
            block = _block(0.03 if index >= 20 else 0.0)
            output.append(master.process(block))
            time.sleep(BLOCK_SECONDS)
        audible = [sample for sample in output if np.any(sample)]
        assert audible
        assert all(np.isfinite(sample).all() for sample in audible)
        assert max(float(np.max(np.abs(sample))) for sample in audible) <= 0.871
        assert master.error is None
        assert master.restarts == 0
        assert master.latency_seconds == pytest.approx(24 * BLOCK_SECONDS)
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
        assert master.restarts >= 1
        assert np.max(np.abs(result)) <= 0.871
        assert np.isfinite(result).all()
    finally:
        master.close()


def test_killed_ffmpeg_recovers_without_dropping_the_live_timeline() -> None:
    master = ContinuousMaster()
    outputs = []
    try:
        for _ in range(45):
            outputs.append(master.process(_block(0.04)))
            time.sleep(BLOCK_SECONDS)
        process = master._proc
        assert process is not None and process.poll() is None
        process.kill()
        for _ in range(100):
            outputs.append(master.process(_block(0.04)))
            time.sleep(BLOCK_SECONDS)
        assert master.restarts == 1
        assert master.error is None
        assert all(np.isfinite(block).all() for block in outputs)
        assert all(np.any(np.abs(block) > 1e-7) for block in outputs[24:])
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


def test_stereotool_command_uses_packaged_preset_and_float_audio() -> None:
    command = _command("stereotool", "C:/Audio Tools/stereo_tool_cmd.exe")
    assert command[:7] == [
        "C:/Audio Tools/stereo_tool_cmd.exe",
        "-",
        "-",
        "-b",
        "32f",
        "-r",
        "44100",
    ]
    preset = Path(command[command.index("-s") + 1])
    assert preset.is_file()
    assert command[command.index("-l") + 1] == "1024"


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
        master._switch(True, "stereotool")
        assert not master._input and not master._output
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
def test_output_cushion_absorbs_jitter_then_recovers_from_real_underrun(mode) -> None:
    master = ContinuousMaster(mode=lambda: "off")
    master.close()
    # Run the consumer without a worker so the output pause is deterministic.
    master._closed = False
    master._select_mode = lambda: mode
    try:
        with master._lock:
            master._switch(True, mode)
            generation = master._generation
            available = master._output_guard_blocks + 1
            master._output.extend((generation, _block(0.1)) for _ in range(available))
            master._fallback_delay.extend(_block(0.03) for _ in range(24))
            for _ in range(available):
                np.testing.assert_array_equal(master.process(_block(0.03)), _block(0.1))
            assert master.error is None
            assert np.any(master.process(_block(0.03)))
            assert master.error == "master output underrun; restarting processor"
            assert master.restarts == 1
            assert master._generation == generation + 1
    finally:
        master.close()


def test_installed_stereotool_preserves_tone_without_periodic_gaps() -> None:
    executable = os.environ.get("AUTODJ_TEST_STEREOTOOL") or shutil.which("stereo_tool_cmd")
    if not executable:
        pytest.skip("Set AUTODJ_TEST_STEREOTOOL to test a separately installed Stereo Tool")
    master = ContinuousMaster(mode=lambda: "stereotool", stereotool_path=executable)
    outputs = []
    try:
        started = time.monotonic()
        for index in range(350):
            samples = np.arange(BLOCK_FRAMES) + index * BLOCK_FRAMES
            tone = (0.08 * np.sin(2 * np.pi * 997 * samples / 44100)).astype(np.float32)
            outputs.append(master.process(np.column_stack((tone, tone))))
            time.sleep(max(0, started + (index + 1) * BLOCK_SECONDS - time.monotonic()))
        assert master.error is None
        assert master.restarts == 0
        assert all(np.any(block) for block in outputs[30:])
        audio = np.concatenate(outputs)
        assert np.isfinite(audio).all()
        assert np.max(np.abs(audio)) <= 1
        spectrum = np.abs(np.fft.rfft(audio[-44100:, 0] * np.hanning(44100)))
        assert np.argmax(spectrum) == 997
    finally:
        master.close()
