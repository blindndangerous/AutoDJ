"""Windows bootstrap checks using temporary PowerShell command shims."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh.exe")


pytestmark = pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None, reason="Windows PowerShell required"
)


def _project(tmp_path: Path, *, interactive: bool = False) -> tuple[Path, Path]:
    root = tmp_path / "project"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / ".python-version", root / ".python-version")
    script = (ROOT / "scripts" / "run.ps1").read_text(encoding="utf-8")
    if interactive:
        script = script.replace(
            "$interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected",
            "$interactive = $true",
        )
        script = script.replace(
            "$ErrorActionPreference = 'Stop'",
            "$ErrorActionPreference = 'Stop'\nfunction Read-Host { $env:AUTODJ_TEST_ANSWER }",
        )
    script = script.replace(
        "[Environment]::GetEnvironmentVariable('Path', $scope)",
        "$(if ($scope -eq 'User') { $env:AUTODJ_TEST_USER_PATH } else { $env:AUTODJ_TEST_MACHINE_PATH })",
    )
    (scripts / "run.ps1").write_text(script, encoding="utf-8")
    run_py = scripts / "run.py"
    run_py.write_text(
        "import json, os, sys\n"
        "open(os.environ['AUTODJ_TEST_LOG'], 'w').write(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    return root, scripts / "run.ps1"


def _run(script: Path, *arguments: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


def _env(tmp_path: Path, *path_entries: Path) -> dict[str, str]:
    result = os.environ.copy()
    result["PATH"] = os.pathsep.join(map(str, path_entries))
    result["AUTODJ_TEST_LOG"] = str(tmp_path / "run.json")
    result["AUTODJ_TEST_USER_PATH"] = ""
    result["AUTODJ_TEST_MACHINE_PATH"] = ""
    return result


def _saved_cpu(root: Path) -> Path:
    runtime = root / ".uv" / "runtime"
    scripts = runtime / "Scripts"
    scripts.mkdir(parents=True)
    executable = sys_executable()
    shutil.copy2(executable, scripts / "python.exe")
    (runtime / "pyvenv.cfg").write_text(f"home = {Path(executable).parent}\n", encoding="utf-8")
    (root / ".uv" / "setup.json").write_text(
        json.dumps({"backend": "cpu", "environment": ".uv/runtime"}), encoding="utf-8"
    )
    return scripts / "python.exe"


def sys_executable() -> str:
    import sys

    return sys.executable


def _tool(path: Path, body: str = "@echo off\r\nexit /b 0\r\n") -> None:
    path.write_text(body, encoding="utf-8")


def test_existing_saved_runtime_forwards_args_without_installing(tmp_path: Path) -> None:
    root, script = _project(tmp_path)
    python = _saved_cpu(root)
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run(script, "doctor", "--json", env=_env(tmp_path, empty))

    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "run.json").read_text()) == ["doctor", "--json"]
    assert not (tmp_path / "winget.log").exists()
    assert python.is_file()


@pytest.mark.parametrize("arguments", [("--help",), ("setup", "--help")])
def test_saved_amd_runtime_can_show_help_without_uv(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    root, script = _project(tmp_path)
    runtime = root / ".uv" / "amd"
    python_dir = runtime / "Scripts"
    python_dir.mkdir(parents=True)
    executable = sys_executable()
    shutil.copy2(executable, python_dir / "python.exe")
    (runtime / "pyvenv.cfg").write_text(f"home = {Path(executable).parent}\n", encoding="utf-8")
    (root / ".uv" / "setup.json").write_text(
        json.dumps({"backend": "amd", "environment": ".uv/amd"}), encoding="utf-8"
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run(script, *arguments, env=_env(tmp_path, empty))

    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "run.json").read_text()) == list(arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        ("setup", "--dry-run"),
        ("--help",),
        ("setup", "--yes", "--dry-run"),
        ("setup", "--yes", "--help"),
    ],
)
def test_dry_run_and_help_never_install(tmp_path: Path, arguments: tuple[str, ...]) -> None:
    root, script = _project(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    _tool(
        tools / "winget.cmd",
        f'@echo off\r\necho called>>"{tmp_path / "winget.log"}"\r\nexit /b 0\r\n',
    )
    result = _run(script, *arguments, env=_env(tmp_path, tools, empty))

    assert result.returncode != 0
    assert "winget install --id astral-sh.uv" in result.stderr
    assert not (tmp_path / "winget.log").exists()
    assert not (root / ".uv" / "setup.json").exists()


@pytest.mark.parametrize("unattended", [False, True])
def test_install_refreshes_path_and_forwards_setup_args(tmp_path: Path, unattended: bool) -> None:
    user_bin = tmp_path / "user-bin"
    user_bin.mkdir()
    _, script = _project(tmp_path, interactive=not unattended)
    staging = tmp_path / "staging"
    staging.mkdir()
    _tool(staging / "uv.cmd", f'@echo off\r\necho %*>>"{tmp_path / "uv.log"}"\r\nexit /b 0\r\n')
    tools = tmp_path / "installer-bin"
    tools.mkdir()
    winget_log = tmp_path / "winget.log"
    _tool(
        tools / "winget.cmd",
        f'@echo off\r\necho %*>>"{winget_log}"\r\n'
        f'copy /Y "{staging / "uv.cmd"}" "{user_bin / "uv.cmd"}" >nul\r\n'
        "exit /b 0\r\n",
    )
    env = _env(tmp_path, tools)
    env["AUTODJ_TEST_ANSWER"] = "y"
    env["AUTODJ_TEST_USER_PATH"] = str(user_bin)
    arguments = ["setup", "--backend", "cpu"]
    if unattended:
        arguments.append("--yes")
    result = _run(script, *arguments, env=env)

    assert result.returncode == 0, result.stderr
    calls = winget_log.read_text().splitlines()
    assert len(calls) == 1
    assert "--id astral-sh.uv --exact --source winget --scope user" in calls[0]
    assert (user_bin / "uv.cmd").is_file()
    python = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert f"run --no-project --python {python}" in (tmp_path / "uv.log").read_text()
    assert "setup --backend cpu" in (tmp_path / "uv.log").read_text()
    if unattended:
        assert "--yes" in (tmp_path / "uv.log").read_text()


def test_missing_winget_reports_manual_uv_install(tmp_path: Path) -> None:
    _, script = _project(tmp_path, interactive=True)
    empty = tmp_path / "empty"
    empty.mkdir()
    env = _env(tmp_path, empty)
    env["AUTODJ_TEST_ANSWER"] = "y"
    result = _run(script, "setup", env=env)

    assert result.returncode == 1
    assert "WinGet is not available" in result.stderr
    assert "winget install --id astral-sh.uv --exact --source winget --scope user" in result.stderr
    assert "https://docs.astral.sh/uv/getting-started/installation/" in result.stderr


def test_missing_ffmpeg_does_not_trigger_bootstrap_install_or_warning(tmp_path: Path) -> None:
    _, script = _project(tmp_path)
    tools = tmp_path / "tools"
    tools.mkdir()
    uv_log = tmp_path / "uv.log"
    _tool(tools / "uv.cmd", f'@echo off\r\necho %*>>"{uv_log}"\r\nexit /b 0\r\n')
    winget_log = tmp_path / "winget.log"
    _tool(tools / "winget.cmd", f'@echo off\r\necho called>>"{winget_log}"\r\nexit /b 0\r\n')
    result = _run(script, "setup", env=_env(tmp_path, tools))

    assert result.returncode == 0, result.stderr
    assert "FFmpeg" not in result.stderr
    assert uv_log.is_file()
    assert not winget_log.exists()


@pytest.mark.parametrize("answer", ["", "n", "no"])
def test_declined_install_is_actionable_and_does_not_launch(tmp_path: Path, answer: str) -> None:
    _, script = _project(tmp_path, interactive=True)
    tools = tmp_path / "tools"
    tools.mkdir()
    _tool(tools / "winget.cmd")
    env = _env(tmp_path, tools)
    env["AUTODJ_TEST_ANSWER"] = answer
    result = _run(script, "setup", env=env)

    assert result.returncode == 1
    assert "cannot continue without it" in result.stderr
    assert "astral-sh.uv" in result.stderr


def test_failed_winget_install_reports_manual_commands(tmp_path: Path) -> None:
    _, script = _project(tmp_path, interactive=True)
    tools = tmp_path / "tools"
    tools.mkdir()
    _tool(tools / "winget.cmd", "@echo off\r\nexit /b 9\r\n")
    env = _env(tmp_path, tools)
    env["AUTODJ_TEST_ANSWER"] = "y"
    result = _run(script, "setup", env=env)

    assert result.returncode == 1
    assert "WinGet could not install uv" in result.stderr
    assert "winget install --id astral-sh.uv" in result.stderr


@pytest.mark.parametrize("interactive", [False, True])
def test_existing_runtime_without_state_checks_tools_for_interactive_setup(
    tmp_path: Path, interactive: bool
) -> None:
    root, script = _project(tmp_path, interactive=interactive)
    python_dir = root / ".venv" / "Scripts"
    python_dir.mkdir(parents=True)
    executable = sys_executable()
    shutil.copy2(executable, python_dir / "python.exe")
    (root / ".venv" / "pyvenv.cfg").write_text(
        f"home = {Path(executable).parent}\n", encoding="utf-8"
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run(script, "serve", env=_env(tmp_path, empty))

    if interactive:
        assert result.returncode == 1
        assert "WinGet is not available" in result.stderr
        assert "winget install --id astral-sh.uv" in result.stderr
        assert not (tmp_path / "run.json").exists()
        return
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "run.json").read_text()) == ["serve"]
    assert "uv" not in result.stderr


def test_the_launcher_never_passes_on_an_inherited_update_watch(tmp_path: Path) -> None:
    """Only the launcher marks a start an update watches, so run.ps1 clears the mark."""
    root, script = _project(tmp_path)
    _saved_cpu(root)
    (root / "scripts" / "run.py").write_text(
        "import json, os\n"
        "open(os.environ['AUTODJ_TEST_LOG'], 'w').write("
        "json.dumps(os.environ.get('AUTODJ_UPDATE_WATCH')))\n",
        encoding="utf-8",
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    environment = {**_env(tmp_path, empty), "AUTODJ_UPDATE_WATCH": "1"}

    result = _run(script, "serve", env=environment)

    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "run.json").read_text()) is None
