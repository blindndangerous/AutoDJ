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

    def test_rejects_forbidden_argument_after_safe_argument(self) -> None:
        mgr = JobManager()

        assert mgr.start("prune", ["safe", "bad;arg"]) is False

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
    def test_read_loop_without_process_is_a_noop(self) -> None:
        mgr = JobManager()

        mgr._read_loop()

        assert mgr.snapshot()["lines"] == []

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


class TestJobPipeEncoding:
    def test_child_output_is_read_as_utf8(self) -> None:
        """A track name the locale codec cannot decode used to kill the reader.

        The reader thread stopped, the pipe filled, the child blocked on write
        forever, and the single job slot stayed busy until someone pressed Stop.
        """
        mgr = JobManager()
        with patch("autodj.jobs.subprocess.Popen") as popen:
            fake = MagicMock()
            fake.poll.return_value = None
            fake.stdout = iter([])
            fake.wait.return_value = 0
            popen.return_value = fake
            assert mgr.start("prune", []) is True

        kwargs = popen.call_args.kwargs
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"

    def test_read_error_terminates_the_child(self) -> None:
        mgr = JobManager()

        def exploding_lines():
            raise ValueError("undecodable byte")
            yield  # pragma: no cover -- generator protocol only

        with patch("autodj.jobs.subprocess.Popen") as popen:
            fake = MagicMock()
            fake.poll.return_value = None
            fake.stdout = exploding_lines()
            fake.wait.return_value = 0
            popen.return_value = fake
            assert mgr.start("prune", []) is True
            for _ in range(50):
                if fake.terminate.called:
                    break
                time.sleep(0.02)

        fake.terminate.assert_called_once()


class TestJobManagerConfigPassthrough:
    """A child job must run against the same config the server is using.

    ``jobs.py`` spawns a fresh interpreter, so the child re-reads
    configuration from scratch.  Without the server's ``--config`` it
    loaded defaults instead, and every job under a non-default config
    started and then died on "Index not found".
    """

    @staticmethod
    def _capture(mgr, name="stats", args=None):
        with patch("autodj.jobs.subprocess.Popen") as popen:
            popen.return_value = MagicMock(poll=lambda: 0, stdout=None)
            mgr._spawn_proc(name, args or [])
        return popen.call_args

    def test_unconfigured_manager_spawns_the_bare_subcommand(self) -> None:
        call = self._capture(JobManager())

        assert call.args[0][1:] == ["-m", "autodj", "stats"]
        assert "AUTODJ_INDEX_DIR" not in call.kwargs["env"]

    def test_config_path_precedes_the_subcommand(self) -> None:
        mgr = JobManager()
        mgr.configure(config_path="C:/tmp/autodj.toml")

        argv = self._capture(mgr).args[0]

        # Click reads --config on the GROUP, so it must come first.
        assert argv[1:] == ["-m", "autodj", "--config", "C:/tmp/autodj.toml", "stats"]

    def test_index_name_is_passed_to_name_aware_subcommands(self) -> None:
        mgr = JobManager()
        mgr.configure(config_path="cfg.toml", index_name="party")

        for name in ("index", "enrich", "prune", "stats"):
            argv = self._capture(mgr, name).args[0]
            assert argv[1:] == [
                "-m",
                "autodj",
                "--config",
                "cfg.toml",
                name,
                "--name",
                "party",
            ]

    def test_index_name_is_withheld_from_subcommands_that_reject_it(self) -> None:
        mgr = JobManager()
        mgr.configure(index_name="party")

        argv = self._capture(mgr, "list-indexes").args[0]

        assert argv[1:] == ["-m", "autodj", "list-indexes"]

    def test_default_index_name_is_not_passed(self) -> None:
        mgr = JobManager()
        mgr.configure(index_name="default")

        assert self._capture(mgr).args[0][1:] == ["-m", "autodj", "stats"]

    def test_job_arguments_follow_the_subcommand(self) -> None:
        mgr = JobManager()
        mgr.configure(config_path="cfg.toml", index_name="party")

        argv = self._capture(mgr, "index", ["--limit", "5"]).args[0]

        assert argv[1:] == [
            "-m",
            "autodj",
            "--config",
            "cfg.toml",
            "index",
            "--name",
            "party",
            "--limit",
            "5",
        ]

    def test_index_dir_rides_along_in_the_child_environment(self) -> None:
        mgr = JobManager()
        mgr.configure(index_dir="Z:/Scripts/autodj/index")

        env = self._capture(mgr).kwargs["env"]

        assert env["AUTODJ_INDEX_DIR"] == "Z:/Scripts/autodj/index"
        assert env["PYTHONIOENCODING"] == "utf-8"

    def test_logged_command_shows_what_was_actually_run(self) -> None:
        mgr = JobManager()
        mgr.configure(config_path="cfg.toml")
        self._capture(mgr)

        assert "--config" in mgr.snapshot()["lines"][0]

    def test_configure_is_idempotent_and_clearable(self) -> None:
        mgr = JobManager()
        mgr.configure(config_path="cfg.toml", index_name="party")
        mgr.configure(config_path=None, index_name=None)

        assert self._capture(mgr).args[0][1:] == ["-m", "autodj", "stats"]
