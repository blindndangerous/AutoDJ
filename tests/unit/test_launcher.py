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
    for variable in ("AUTODJ_GPU", "MIOPEN_USER_DB_PATH", "MIOPEN_CUSTOM_CACHE_DIR"):
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


def test_first_run_sets_up_then_runs_original_command(tmp_path: Path, monkeypatch) -> None:
    root = _project(tmp_path, monkeypatch)
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
