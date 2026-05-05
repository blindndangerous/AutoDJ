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
