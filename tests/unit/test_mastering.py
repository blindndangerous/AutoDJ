from __future__ import annotations

import shutil
import time

import numpy as np
import pytest

from autodj.mastering import BLOCK_FRAMES, BLOCK_SECONDS, ContinuousMaster

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
        assert master.latency_seconds == pytest.approx(14 * BLOCK_SECONDS)
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


def test_invalid_block_shape_is_rejected() -> None:
    master = ContinuousMaster()
    try:
        with pytest.raises(ValueError, match="shape"):
            master.process(np.zeros((10, 2), dtype=np.float32))
    finally:
        master.close()
