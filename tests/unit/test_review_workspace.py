"""Review PCM allocations stay isolated and refuse unsafe storage growth."""

from contextlib import nullcontext

import numpy as np
import pytest

from autodj import stereo


def test_mapping_finalizer_can_release_budget_during_locked_gc(tmp_path):
    """ADR-0014-08: GC inside allocation must not deadlock the render worker."""
    import os
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    script = textwrap.dedent("""
        import gc
        import sys
        from pathlib import Path
        from autodj.stereo import audio_buffer, audio_workspace

        gc.disable()
        with audio_workspace(Path(sys.argv[1]), max_bytes=64, reserve_bytes=0) as workspace:
            cycle = [audio_buffer(8)]
            cycle.append(cycle)
            del cycle
            assert workspace.allocated == 64
            with workspace.lock:
                gc.collect()
            assert workspace.allocated == 0
            assert audio_buffer(8).shape == (8, 2)
    """)
    subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=True,
        timeout=10,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(Path(stereo.__file__).resolve().parents[1])},
    )


def test_freed_pcm_releases_budget_but_a_live_view_keeps_its_reservation(tmp_path):
    import gc

    with stereo.audio_workspace(tmp_path, max_bytes=64, reserve_bytes=0) as workspace:
        audio = stereo.audio_buffer(8)
        view = audio[:1]
        del audio
        gc.collect()
        with pytest.raises(OSError, match="scratch"):
            stereo.audio_buffer(1)
        del view
        gc.collect()
        assert workspace.allocated == 0
        assert stereo.audio_buffer(8).shape == (8, 2)


def test_spool_releases_budget_after_close_or_buffer_collection(tmp_path):
    import gc

    with stereo.audio_workspace(tmp_path, max_bytes=64, reserve_bytes=0) as workspace:
        spool = stereo.AudioSpool()
        spool.write(bytes(64))
        spool.close()
        assert workspace.allocated == 0
        spool = stereo.AudioSpool()
        spool.write(bytes(64))
        raw = spool.buffer()
        spool.close()
        assert workspace.allocated == 64
        del raw
        gc.collect()
        assert workspace.allocated == 0


def test_processed_pcm_can_be_closed_at_workspace_exit_while_still_referenced(tmp_path):
    import soundfile as sf

    from autodj.processing import load_and_measure_audio, load_processed_audio

    path = tmp_path / "tone.wav"
    samples = 0.1 * np.sin(2 * np.pi * 220 * np.arange(44100) / 44100)
    sf.write(path, np.repeat(samples[:, None], 2, axis=1), 44100)
    with stereo.audio_workspace(tmp_path / "scratch"):
        audio, measurement = load_and_measure_audio(path)
        cached = load_processed_audio(path, measurement)
        assert np.any(audio) and np.any(cached)


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
            actual = np.ndarray((8,), buffer=spool.buffer(), dtype=np.float32)
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
