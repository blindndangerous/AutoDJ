from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj.mastering import _QUEUE_BLOCKS, BLOCK_FRAMES, BLOCK_SECONDS, ContinuousMaster, _command

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is unavailable")

# The advertised pipeline delay in blocks; processed audio is due from here on.
_LATENCY_BLOCKS = 24


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
    behind, which the master rightly treats as an underrun and restarts;
    that is scheduling, not the behaviour under test.  ``cushion_due`` marks
    the block at the advertised latency, by which the output cushion must
    be full.
    """
    deadline = time.monotonic() + _CATCH_UP_SECONDS
    while not _caught_up(master, cushion_due=cushion_due):
        assert time.monotonic() < deadline, "the master pipeline stopped making progress"
        time.sleep(0.002)
    return master.process(block)


def test_live_pipeline_processes_continuously_and_closes() -> None:
    master = ContinuousMaster()
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
        assert master.restarts == 0
        assert master.latency_seconds == pytest.approx(_LATENCY_BLOCKS * BLOCK_SECONDS)
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
        assert master.restarts == 1
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


def test_stereotool_command_defaults_to_packaged_executable_path() -> None:
    expected = (
        "./tools/stereo-tool/stereo_tool_cmd.exe"
        if os.name == "nt"
        else "./tools/stereo-tool/stereo_tool_cmd"
    )
    assert _command("stereotool")[0] == expected


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
    executable = os.environ.get("AUTODJ_TEST_STEREOTOOL") or shutil.which("stereo_tool_cmd")
    if not executable:
        pytest.skip("Set AUTODJ_TEST_STEREOTOOL to test a separately installed Stereo Tool")
    master = ContinuousMaster(mode=lambda: "stereotool", stereotool_path=executable)
    outputs = []
    try:
        started = time.monotonic()
        for index in range(220):
            if index == 90:
                assert master._post_proc is not None
                master._post_proc.kill()
            t = np.arange(BLOCK_FRAMES) + index * BLOCK_FRAMES
            tone = (0.03 * np.sin(2 * np.pi * 997 * t / 44100)).astype(np.float32)
            outputs.append(master.process(np.column_stack((tone, tone))))
            time.sleep(max(0, started + (index + 1) * BLOCK_SECONDS - time.monotonic()))
        assert master.restarts == 1
        assert master.error is None
        assert all(np.max(np.abs(block)) > 1e-5 for block in outputs[40:])
        assert np.isfinite(np.concatenate(outputs)).all()
        processes = (master._proc, master._post_proc)
    finally:
        master.close()
    assert all(proc is not None and proc.poll() is not None for proc in processes)
