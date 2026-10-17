"""Review PCM allocations stay isolated and refuse unsafe storage growth."""

from contextlib import nullcontext

import numpy as np
import pytest

from autodj import stereo


@pytest.mark.parametrize("fail", [False, True])
def test_workspace_releases_mapping_even_while_capture_still_references_audio(tmp_path, fail):
    """Captured arrays are scoped to the job, including exceptional exits."""
    with (
        pytest.raises(RuntimeError) if fail else nullcontext(),
        stereo.audio_workspace(tmp_path, max_bytes=64, reserve_bytes=0),
    ):
        captured = stereo.audio_buffer(4)
        captured[:] = 0.5
        mapping = captured.base
        while isinstance(mapping, np.ndarray):
            mapping = mapping.base
        if isinstance(mapping, memoryview):
            mapping = mapping.obj
        assert not mapping.closed
        if fail:
            raise RuntimeError("render failed")
    assert mapping.closed


def test_review_workspace_bounds_small_buffers_and_restores_normal_allocation(tmp_path):
    with stereo.audio_workspace(tmp_path, max_bytes=64, reserve_bytes=0):
        first = stereo.audio_buffer(4)
        first[:] = 0.25
        assert first.shape == (4, 2)
        with pytest.raises(OSError, match="scratch"):
            stereo.audio_buffer(5)
    assert stereo.audio_buffer(10).shape == (10, 2)


def test_review_workspace_bounds_spool_before_growing_and_uses_private_directory(
    tmp_path, monkeypatch
):
    real = stereo.tempfile.TemporaryFile
    directories = []

    def temporary(*args, **kwargs):
        directories.append(kwargs.get("dir"))
        return real(*args, **kwargs)

    monkeypatch.setattr(stereo.tempfile, "TemporaryFile", temporary)
    with stereo.audio_workspace(tmp_path, max_bytes=64, reserve_bytes=0):
        spool = stereo.AudioSpool()
        try:
            spool.write(np.full((4, 2), 0.25, np.float32).tobytes())
            with pytest.raises(OSError, match="scratch"):
                spool.write(bytes(40))
            actual = np.frombuffer(spool.buffer(), dtype=np.float32)
            assert len(actual) == 8 and np.all(actual == 0.25)
        finally:
            spool.close()
    assert directories and all(path == tmp_path for path in directories)


def test_review_workspace_keeps_reserve_and_propagates_to_station_decode_thread(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from autodj.station_log import station_thread

    monkeypatch.setattr(stereo.shutil, "disk_usage", lambda path: SimpleNamespace(free=100))
    errors = []

    def allocate():
        try:
            stereo.audio_buffer(8)
        except OSError as exc:
            errors.append(str(exc))

    with stereo.audio_workspace(tmp_path, max_bytes=1024, reserve_bytes=50):
        worker = station_thread(target=allocate, name="review-workspace-test")
        worker.start()
        worker.join(1)
    assert not worker.is_alive()
    assert errors and "space" in errors[0]
