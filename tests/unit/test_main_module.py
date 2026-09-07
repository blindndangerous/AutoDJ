"""``python -m autodj`` must work: the web UI's job runner depends on it.

``jobs.py`` spawns ``[sys.executable, "-m", "autodj", <subcommand>]``, so
without an importable ``autodj.__main__`` every Index / Enrich / Prune /
Stats button in the Library tools panel dies after about a second with
"No module named autodj.__main__".
"""

from __future__ import annotations

import subprocess  # nosec B404 -- launching this interpreter, no shell
import sys


def test_module_entry_point_exists_and_calls_the_cli() -> None:
    """The module wires straight to the Click group the console script uses."""
    import autodj.__main__ as module
    from autodj.cli import cli

    assert module.main is cli


def test_python_dash_m_autodj_help_exits_zero() -> None:
    """The exact invocation jobs.py uses must run."""
    completed = subprocess.run(  # nosec B603 -- fixed argv, shell=False
        [sys.executable, "-m", "autodj", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "No module named" not in completed.stderr
    assert "Usage:" in completed.stdout


def test_job_runner_argv_names_a_runnable_module() -> None:
    """The runner's own argv, run for real, must not fail to import."""
    from autodj.jobs import JobManager

    manager = JobManager()
    captured: dict[str, list[str]] = {}

    class _FakePopen:
        def __init__(self, cmd, **_kwargs) -> None:
            captured["cmd"] = list(cmd)
            self.stdout = None

        def poll(self) -> int:
            return 0

    import autodj.jobs as jobs_module

    original = jobs_module.subprocess.Popen
    jobs_module.subprocess.Popen = _FakePopen  # type: ignore[misc]
    try:
        manager._spawn_proc("stats", [])
    finally:
        jobs_module.subprocess.Popen = original  # type: ignore[misc]

    assert captured["cmd"][:3] == [sys.executable, "-m", "autodj"]
    probe = subprocess.run(  # nosec B603 -- fixed argv, shell=False
        [*captured["cmd"][:3], "--help"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
