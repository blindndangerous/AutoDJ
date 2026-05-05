"""Unit tests for autodj.jobs — background subprocess job runner."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from autodj.jobs import JobManager


class TestJobManagerStart:
    def test_rejects_unknown_subcommand(self) -> None:
        mgr = JobManager()
        assert mgr.start("rm", ["-rf", "/"]) is False

    def test_rejects_shell_metacharacters(self) -> None:
        mgr = JobManager()
        assert mgr.start("prune", ["foo;bar"]) is False
        assert mgr.start("prune", ["a|b"]) is False
        assert mgr.start("prune", ["x&y"]) is False

    def test_starts_allowed_subcommand(self) -> None:
        """Spawn a python subprocess that exits immediately so the test
        exercises the Popen + reader-thread code paths without depending on
        the real autodj CLI being installed."""
        mgr = JobManager()
        with patch("autodj.jobs.subprocess.Popen") as popen:
            fake = MagicMock()
            fake.poll.return_value = None  # running
            fake.stdout = iter(["line one\n", "line two\n"])
            fake.wait.return_value = 0
            popen.return_value = fake
            ok = mgr.start("prune", [])
            # Reader thread is daemonised; give it a beat to flush.
            for _ in range(20):
                if not mgr.running:
                    break
                time.sleep(0.05)
        assert ok is True
        snap = mgr.snapshot()
        assert snap["name"] == "prune"
        assert any("line one" in s for s in snap["lines"])
        assert snap["exit_code"] == 0

    def test_refuses_concurrent_start(self) -> None:
        mgr = JobManager()
        with patch("autodj.jobs.subprocess.Popen") as popen:
            fake = MagicMock()
            fake.poll.return_value = None
            fake.stdout = iter([])
            fake.wait.return_value = 0
            popen.return_value = fake
            assert mgr.start("prune") is True
            # Force "running" so the second start sees a live process.
            mgr._proc.poll = lambda: None  # type: ignore[union-attr]
            assert mgr.start("stats") is False

    def test_snapshot_idle_state(self) -> None:
        mgr = JobManager()
        snap = mgr.snapshot()
        assert snap["running"] is False
        assert snap["lines"] == []
        assert snap["exit_code"] is None

    def test_stop_idle_returns_false(self) -> None:
        mgr = JobManager()
        assert mgr.stop() is False


class TestGetManager:
    def test_returns_singleton(self) -> None:
        from autodj.jobs import get_manager

        a = get_manager()
        b = get_manager()
        assert a is b


class TestSpawnFailure:
    def test_oserror_on_spawn_records_failure(self) -> None:
        mgr = JobManager()
        with patch("autodj.jobs.subprocess.Popen", side_effect=OSError("no such")):
            ok = mgr.start("prune")
        assert ok is False
        assert mgr.snapshot()["exit_code"] == -1


class TestReadLoopAndStop:
    def test_read_loop_handles_oserror(self) -> None:
        """OSError mid-stream → recorded, loop still completes."""
        import time as _time

        mgr = JobManager()
        mgr._proc = MagicMock()
        mgr._proc.stdout = MagicMock()
        mgr._proc.stdout.__iter__ = lambda self: iter([])
        mgr._proc.wait = MagicMock(return_value=0)
        mgr._started_at = _time.time()

        # Force the iter to raise OSError.
        def _bad_iter(self):
            raise OSError("pipe closed")

        mgr._proc.stdout.__iter__ = _bad_iter

        mgr._read_loop()
        snap = mgr.snapshot()
        assert any("read error" in line for line in snap["lines"])
        assert snap["exit_code"] == 0

    def test_read_loop_handles_wait_timeout(self) -> None:
        """subprocess.TimeoutExpired on wait() → exit_code -2."""
        import subprocess as _sub
        import time as _time

        mgr = JobManager()
        mgr._proc = MagicMock()
        mgr._proc.stdout = MagicMock()
        mgr._proc.stdout.__iter__ = lambda self: iter([])
        mgr._proc.wait = MagicMock(side_effect=_sub.TimeoutExpired("x", 5))
        mgr._started_at = _time.time()

        mgr._read_loop()
        assert mgr.snapshot()["exit_code"] == -2

    def test_stop_terminates_running(self) -> None:
        """stop() while running calls terminate()."""
        mgr = JobManager()
        proc = MagicMock()
        proc.poll = MagicMock(return_value=None)  # running
        mgr._proc = proc
        assert mgr.stop() is True
        proc.terminate.assert_called_once()

    def test_stop_swallows_terminate_oserror(self) -> None:
        """OSError from terminate() is suppressed."""
        mgr = JobManager()
        proc = MagicMock()
        proc.poll = MagicMock(return_value=None)
        proc.terminate = MagicMock(side_effect=OSError("already gone"))
        mgr._proc = proc
        assert mgr.stop() is True  # no exception
