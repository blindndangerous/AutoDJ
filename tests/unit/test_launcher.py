"""Tests for the standard-library runtime dispatcher."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from autodj import launcher


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "ROOT", root)
    monkeypatch.setattr(launcher.sys, "argv", ["autodj", "doctor"])
    monkeypatch.setenv("AUTODJ_RUNTIME", "")
    for variable in (
        "AUTODJ_GPU",
        "AUTODJ_NO_MODEL",
        "MIOPEN_USER_DB_PATH",
        "MIOPEN_CUSTOM_CACHE_DIR",
    ):
        monkeypatch.delenv(variable, raising=False)
    return root


def _save(root: Path, environment: Path, backend: str = "cpu") -> None:
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_text("placeholder", encoding="utf-8")
    state = root / launcher.STATE
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps({"environment": str(environment), "backend": backend}), encoding="utf-8"
    )


def test_no_saved_state_uses_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _project(tmp_path, monkeypatch)
    cli = Mock(return_value=0)
    monkeypatch.setattr(launcher, "_cli", cli)
    dispatch = Mock()
    monkeypatch.setattr(launcher.subprocess, "run", dispatch)

    assert launcher.main() == 0
    cli.assert_called_once_with()
    dispatch.assert_not_called()


def test_installed_wheel_does_not_read_setup_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "installed"
    root.mkdir()
    monkeypatch.setattr(launcher, "ROOT", root)
    cli = Mock(return_value=0)
    monkeypatch.setattr(launcher, "_cli", cli)

    assert launcher.main() == 0
    cli.assert_called_once_with()


def test_saved_relative_environment_dispatches_with_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, monkeypatch)
    environment = root / ".uv" / "runtime"
    _save(root, environment)
    completed = SimpleNamespace(returncode=7)
    dispatch = Mock(return_value=completed)
    monkeypatch.setattr(launcher.subprocess, "run", dispatch)

    assert launcher.main() == 7
    (command,) = dispatch.call_args.args
    assert command == [str(launcher._python_path(environment.resolve())), "-m", "autodj", "doctor"]
    assert dispatch.call_args.kwargs["env"]["AUTODJ_GPU"] == "0"
    assert dispatch.call_args.kwargs["check"] is False
    assert "cwd" not in dispatch.call_args.kwargs


def test_amd_sets_and_creates_miopen_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, monkeypatch)
    environment = root / "amd-env"
    _save(root, environment, "amd")
    dispatch = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(launcher.subprocess, "run", dispatch)

    assert launcher.main() == 0
    child_env = dispatch.call_args.kwargs["env"]
    assert child_env["MIOPEN_USER_DB_PATH"] == str(environment.resolve() / "miopen/db")
    assert child_env["MIOPEN_CUSTOM_CACHE_DIR"] == str(environment.resolve() / "miopen/cache")
    assert (environment / "miopen/db").is_dir()
    assert (environment / "miopen/cache").is_dir()


def test_current_override_skips_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path, monkeypatch)
    _save(root, root / "missing", "nvidia")
    monkeypatch.setenv("AUTODJ_RUNTIME", "current")
    cli = Mock(return_value=4)
    monkeypatch.setattr(launcher, "_cli", cli)
    dispatch = Mock()
    monkeypatch.setattr(launcher.subprocess, "run", dispatch)

    assert launcher.main() == 4
    cli.assert_called_once_with()
    dispatch.assert_not_called()


@pytest.mark.parametrize(
    "state",
    [
        [],
        {"environment": 3, "backend": "cpu"},
        {"environment": ".uv/env", "backend": "cuda"},
    ],
)
def test_invalid_saved_state_fails_clearly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: object,
) -> None:
    root = _project(tmp_path, monkeypatch)
    state_path = root / launcher.STATE
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    cli = Mock()
    monkeypatch.setattr(launcher, "_cli", cli)

    assert launcher.main() == 1
    assert "AutoDJ:" in capsys.readouterr().err
    cli.assert_not_called()


def test_missing_interpreter_fails_without_cpu_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, monkeypatch)
    _save(root, root / "deleted-env")
    python = launcher._python_path(root / "deleted-env")
    python.unlink()
    cli = Mock()
    monkeypatch.setattr(launcher, "_cli", cli)

    assert launcher.main() == 1
    assert "interpreter is missing" in capsys.readouterr().err
    cli.assert_not_called()


@pytest.mark.parametrize("backend", ["cpu", "amd"])
def test_same_environment_invokes_cli_without_recursion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    root = _project(tmp_path, monkeypatch)
    environment = root / "runtime"
    _save(root, environment, backend)
    monkeypatch.setattr(launcher.sys, "prefix", str(environment.resolve()))
    cli = Mock(return_value=0)
    monkeypatch.setattr(launcher, "_cli", cli)
    dispatch = Mock()
    monkeypatch.setattr(launcher.subprocess, "run", dispatch)

    assert launcher.main() == 0
    cli.assert_called_once_with()
    dispatch.assert_not_called()
    if backend == "cpu":
        assert os.environ["AUTODJ_GPU"] == "0"
    else:
        assert os.environ["MIOPEN_USER_DB_PATH"] == str(environment / "miopen/db")


def test_keyboard_interrupt_returns_130(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path, monkeypatch)
    _save(root, root / "runtime")
    monkeypatch.setattr(launcher.subprocess, "run", Mock(side_effect=KeyboardInterrupt))

    assert launcher.main() == 130


@pytest.mark.parametrize("existing_venv", [False, True])
def test_first_run_sets_up_then_runs_original_command(
    tmp_path: Path, monkeypatch, existing_venv: bool
) -> None:
    root = _project(tmp_path, monkeypatch)
    if existing_venv:
        python = launcher._python_path(root / ".venv")
        python.parent.mkdir(parents=True)
        python.touch()
    (root / "scripts").mkdir()
    (root / "scripts/setup.py").touch()
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: True)
    environment = root / ".uv/setup-amd"

    def setup(arguments):
        assert arguments == []
        _save(root, environment, "amd")
        return 0

    monkeypatch.setattr(launcher, "run_setup", setup)
    dispatch = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(launcher.subprocess, "run", dispatch)
    assert launcher.main() == 0
    assert dispatch.call_args.args[0][-3:] == ["-m", "autodj", "doctor"]
    assert dispatch.call_args.args[0][0] == str(launcher._python_path(environment))


def test_cancelled_first_run_does_not_launch(tmp_path: Path, monkeypatch) -> None:
    root = _project(tmp_path, monkeypatch)
    (root / "scripts").mkdir()
    (root / "scripts/setup.py").touch()
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(launcher, "run_setup", Mock(return_value=1))
    cli = Mock()
    monkeypatch.setattr(launcher, "_cli", cli)
    assert launcher.main() == 1
    cli.assert_not_called()


def test_explicit_setup_bypasses_invalid_saved_selection(tmp_path: Path, monkeypatch) -> None:
    root = _project(tmp_path, monkeypatch)
    (root / ".uv").mkdir()
    (root / launcher.STATE).write_text("not JSON")
    monkeypatch.setattr(launcher.sys, "argv", ["autodj", "setup", "--backend", "cpu"])
    setup = Mock(return_value=0)
    monkeypatch.setattr(launcher, "run_setup", setup)
    assert launcher.main() == 0
    setup.assert_called_once_with(["--backend", "cpu"])


@pytest.mark.parametrize("argument,status", [("--help", 0), ("serve", 1)])
def test_unattended_fresh_bootstrap_does_not_install(
    tmp_path: Path, monkeypatch, argument, status
) -> None:
    """ADR-0012-01."""
    root = _project(tmp_path, monkeypatch)
    (root / "scripts").mkdir()
    (root / "scripts/setup.py").touch()
    monkeypatch.setenv("AUTODJ_BOOTSTRAP", "1")
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(launcher.sys, "argv", ["autodj", argument])
    setup = Mock()
    monkeypatch.setattr(launcher, "run_setup", setup)
    assert launcher.main() == status
    setup.assert_not_called()


def test_setup_subprocess_bypasses_saved_runtime(tmp_path: Path, monkeypatch) -> None:
    root = _project(tmp_path, monkeypatch)
    (root / "scripts").mkdir()
    (root / "scripts/setup.py").touch()
    dispatch = Mock(return_value=SimpleNamespace(returncode=9))
    monkeypatch.setattr(launcher.subprocess, "run", dispatch)
    assert launcher.run_setup(["--dry-run"]) == 9
    assert dispatch.call_args.args[0] == [
        launcher.sys.executable,
        str(root / "scripts/setup.py"),
        "--dry-run",
    ]
    assert dispatch.call_args.kwargs["env"]["AUTODJ_RUNTIME"] == "current"


@pytest.mark.parametrize("backend", ["cpu", "nvidia", "amd", "no-model"])
@pytest.mark.parametrize("same_prefix", [False, True])
def test_saved_model_mode_overrides_inherited_no_model(tmp_path, monkeypatch, backend, same_prefix):
    root = _project(tmp_path, monkeypatch)
    environment = root / "runtime"
    _save(root, environment, backend)
    monkeypatch.setenv("AUTODJ_NO_MODEL", "1")
    if same_prefix:
        monkeypatch.setattr(launcher.sys, "prefix", str(environment.resolve()))
    cli = Mock(return_value=0)
    dispatch = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(launcher, "_cli", cli)
    monkeypatch.setattr(launcher.subprocess, "run", dispatch)
    assert launcher.main() == 0
    if same_prefix:
        cli.assert_called_once_with()
        dispatch.assert_not_called()
        env = os.environ
    else:
        env = dispatch.call_args.kwargs["env"]
        cli.assert_not_called()
    assert env.get("AUTODJ_NO_MODEL") == ("1" if backend == "no-model" else None)
    if backend == "no-model":
        assert env["AUTODJ_GPU"] == "0"


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["serve"], True),
        (["-v", "serve", "--lan"], True),
        (["--config", "x.toml", "serve"], True),
        (["--config=x.toml", "--verbose", "serve"], True),
        (["doctor"], False),
        (["--config", "serve"], False),
        ([], False),
    ],
)
def test_runs_serve_finds_the_command_after_global_options(
    arguments: list[str], expected: bool
) -> None:
    """ADR-0016-04."""
    assert launcher.runs_serve(arguments) is expected


def test_the_restart_loop_starts_serve_again_on_the_restart_code() -> None:
    """ADR-0016-04: the dedicated exit status, then any other status ends the loop."""
    statuses = iter([launcher.RESTART_EXIT_CODE, launcher.RESTART_EXIT_CODE, 0])
    seen: list[dict[str, str]] = []

    def run_child(command: list[str], environment: dict[str, str]) -> int:
        seen.append(dict(environment))
        return next(statuses)

    clock = iter([0.0, 100.0, 200.0])
    assert (
        launcher.run_supervised(
            ["python", "-m", "autodj", "serve"],
            {"A": "1"},
            run_child=run_child,
            clock=lambda: next(clock),
        )
        == 0
    )
    assert len(seen) == 3
    assert all(env[launcher.SUPERVISOR_ENV] == "1" and env["A"] == "1" for env in seen)


def test_the_restart_loop_stops_a_server_that_keeps_asking(capsys) -> None:
    """ADR-0016-04."""
    calls = []

    def run_child(command: list[str], environment: dict[str, str]) -> int:
        calls.append(command)
        return launcher.RESTART_EXIT_CODE

    assert (
        launcher.run_supervised(["serve"], {}, run_child=run_child, clock=lambda: 1.0)
        == launcher.RESTART_EXIT_CODE
    )
    assert len(calls) == launcher.MAX_RESTARTS
    assert "not restarting again" in capsys.readouterr().err


@pytest.mark.parametrize("same_prefix", [False, True])
def test_the_checkout_launcher_supervises_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_prefix: bool
) -> None:
    """ADR-0016-04: only the checkout's launcher loops, and only for serve."""
    root = _project(tmp_path, monkeypatch)
    environment = root / "runtime"
    _save(root, environment)
    if same_prefix:
        monkeypatch.setattr(launcher.sys, "prefix", str(environment.resolve()))
    monkeypatch.setattr(launcher.sys, "argv", ["autodj", "serve"])
    monkeypatch.setenv("AUTODJ_BOOTSTRAP", "1")
    monkeypatch.delenv(launcher.SUPERVISOR_ENV, raising=False)
    supervised = Mock(return_value=0)
    monkeypatch.setattr(launcher, "run_supervised", supervised)
    monkeypatch.setattr(launcher, "_cli", Mock(side_effect=AssertionError("not in-process")))

    assert launcher.main() == 0
    command, child_environment = supervised.call_args.args
    expected = (
        launcher.sys.executable
        if same_prefix
        else str(launcher._python_path(environment.resolve()))
    )
    assert command == [expected, "-m", "autodj", "serve"]
    assert child_environment["AUTODJ_GPU"] == "0"
    assert child_environment.get("AUTODJ_RUNTIME") == ("current" if same_prefix else "")


@pytest.mark.parametrize(
    "environ",
    [{}, {"AUTODJ_BOOTSTRAP": "1", launcher.SUPERVISOR_ENV: "1"}],
    ids=["installed-command", "already-supervised"],
)
def test_serve_is_not_supervised_twice_or_outside_the_checkout_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environ: dict[str, str]
) -> None:
    """ADR-0016-04."""
    root = _project(tmp_path, monkeypatch)
    environment = root / "runtime"
    _save(root, environment)
    monkeypatch.setattr(launcher.sys, "prefix", str(environment.resolve()))
    monkeypatch.setattr(launcher.sys, "argv", ["autodj", "serve"])
    monkeypatch.delenv("AUTODJ_BOOTSTRAP", raising=False)
    monkeypatch.delenv(launcher.SUPERVISOR_ENV, raising=False)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    supervised = Mock()
    monkeypatch.setattr(launcher, "run_supervised", supervised)
    cli = Mock(return_value=0)
    monkeypatch.setattr(launcher, "_cli", cli)

    assert launcher.main() == 0
    supervised.assert_not_called()
    cli.assert_called_once_with()


def test_a_real_child_reports_its_exit_status() -> None:
    """ADR-0016-04: on Windows the child also joins a job that ends with the launcher."""
    status = launcher._run_child(
        [launcher.sys.executable, "-c", "raise SystemExit(75)"], dict(os.environ)
    )
    assert status == launcher.RESTART_EXIT_CODE


def test_a_stop_signal_ends_the_loop_even_when_the_server_asked_to_restart() -> None:
    """ADR-0016-10: Ctrl+C or SIGTERM wins over a pending restart."""
    import signal

    calls: list[list[str]] = []

    def run_child(command: list[str], environment: dict[str, str]) -> int:
        calls.append(command)
        raise launcher.StopRequested(signal.SIGTERM, launcher.RESTART_EXIT_CODE)

    ended = Mock(return_value=128 + signal.SIGTERM)
    assert (
        launcher.run_supervised(["serve"], {}, run_child=run_child, end_with_signal=ended)
        == 128 + signal.SIGTERM
    )
    assert len(calls) == 1
    ended.assert_called_once_with(signal.SIGTERM)


_LAUNCHER = """
import os, sys
from autodj.launcher import run_supervised
child = [sys.executable, sys.argv[1], sys.argv[2]]
raise SystemExit(run_supervised(child, dict(os.environ)))
"""

_SERVER = """
import os, signal, sys, time
from pathlib import Path
folder = Path(sys.argv[1])
(folder / f"started-{os.getpid()}").touch()
def stop(signum, frame):
    with open(folder / "stopped-by", "a") as record:
        print(signal.Signals(signum).name, file=record)
    raise SystemExit(75)  # as if a web restart was pending too
for name in ("SIGINT", "SIGTERM", "SIGHUP"):
    signal.signal(getattr(signal, name), stop)
(folder / "ready").touch()
time.sleep(60)
"""


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
@pytest.mark.parametrize(
    ("signame", "target"),
    [
        ("SIGTERM", "launcher"),
        ("SIGINT", "launcher"),
        ("SIGHUP", "launcher"),
        # As systemd stops a service: SIGTERM to every process at once.
        ("SIGTERM", "group"),
    ],
)
def test_a_signal_to_the_launcher_reaches_the_server_and_ends_the_launcher_with_it(
    tmp_path: Path, signame: str, target: str
) -> None:
    """ADR-0016-10: the server gets the signal, nothing restarts, and the launcher dies of it."""
    import signal
    import subprocess
    import time

    folder = tmp_path / "run"
    folder.mkdir()
    (tmp_path / "launcher.py").write_text(_LAUNCHER, encoding="utf-8")
    (tmp_path / "server.py").write_text(_SERVER, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(Path(launcher.__file__).resolve().parents[1])}
    env.pop(launcher.SUPERVISOR_ENV, None)
    process = subprocess.Popen(
        [
            launcher.sys.executable,
            str(tmp_path / "launcher.py"),
            str(tmp_path / "server.py"),
            str(folder),
        ],
        env=env,
        # Its own group, so a signal to the whole group reaches only the two.
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not (folder / "ready").exists():
            assert time.monotonic() < deadline, "the server did not start"
            time.sleep(0.05)
        if target == "group":
            os.killpg(process.pid, getattr(signal, signame))
        else:
            process.send_signal(getattr(signal, signame))
        status = process.wait(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
    assert status == -getattr(signal, signame)
    assert (folder / "stopped-by").read_text().split()[0] == signame
    assert len(list(folder.glob("started-*"))) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_a_stop_that_comes_while_the_server_starts_still_stops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0016-10: the handlers are in place before the server starts."""
    import signal

    started = Mock()
    started.wait.return_value = 0

    def popen(*_args: object, **_kwargs: object) -> Mock:
        os.kill(os.getpid(), signal.SIGTERM)  # lands between the check and the start
        return started

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    with pytest.raises(launcher.StopRequested) as stop:
        launcher._run_child(["serve"], {})
    assert stop.value.signum == signal.SIGTERM
    started.send_signal.assert_called_with(signal.SIGTERM)
