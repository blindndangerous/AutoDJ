"""Host installation never runs without consent and must produce a usable tool."""

import os
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from autodj import host_tools


def test_path_refresh_preserves_process_paths_and_expands_registered_paths(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    registry = SimpleNamespace(
        HKEY_LOCAL_MACHINE=1,
        HKEY_CURRENT_USER=2,
        OpenKey=lambda hive, key: nullcontext(hive),
        QueryValueEx=lambda hive, name: ("machine" if hive == 1 else "$FFMPEG_HOME", 1),
    )
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setenv("PATH", "process")
    monkeypatch.setenv("FFMPEG_HOME", "installed")
    host_tools._refresh_windows_path()
    assert os.environ["PATH"].split(os.pathsep) == ["process", "machine", "installed"]


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(host_tools.sys, "platform", "win32")
    refresh = Mock()
    monkeypatch.setattr(host_tools, "_refresh_windows_path", refresh)
    monkeypatch.setattr(host_tools.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        host_tools.shutil, "which", lambda name: "winget.exe" if name == "winget" else None
    )
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(host_tools.subprocess, "run", run)
    return run, refresh


@pytest.mark.parametrize("interactive,answer", [(True, "n"), (False, "y")])
def test_declined_or_unattended_install_does_not_run(windows, monkeypatch, interactive, answer):
    monkeypatch.setattr(host_tools.sys.stdin, "isatty", lambda: interactive)
    monkeypatch.setattr("builtins.input", lambda _: answer)
    assert not host_tools.ensure_ffmpeg()
    windows[0].assert_not_called()


@pytest.mark.parametrize("auto_install", [False, True])
def test_accepted_install_refreshes_path_and_verifies_tool(windows, monkeypatch, auto_install):
    run, refresh = windows
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    monkeypatch.setattr(
        host_tools.shutil,
        "which",
        lambda name: "winget.exe" if name == "winget" else ("ffmpeg.exe" if run.called else None),
    )
    assert host_tools.ensure_ffmpeg(auto_install=auto_install)
    run.assert_called_once_with(
        [
            "winget.exe",
            "install",
            "--id",
            "Gyan.FFmpeg",
            "--exact",
            "--source",
            "winget",
            "--scope",
            "user",
            "--accept-source-agreements",
            "--accept-package-agreements",
        ],
        check=False,
    )
    assert refresh.call_count == 2


@pytest.mark.parametrize("code,message", [(7, "exit code 7"), (0, "still not on PATH")])
def test_install_failure_is_reported(windows, code, message):
    windows[0].return_value.returncode = code
    with pytest.raises(RuntimeError, match=message):
        host_tools.ensure_ffmpeg(auto_install=True)


def test_existing_ffmpeg_needs_no_installer(windows, monkeypatch):
    monkeypatch.setattr(host_tools.shutil, "which", lambda _: "ffmpeg.exe")
    assert host_tools.ensure_ffmpeg()
    windows[0].assert_not_called()


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_no_installer_gives_manual_guidance(windows, monkeypatch, capsys, platform):
    monkeypatch.setattr(host_tools.sys, "platform", platform)
    monkeypatch.setattr(host_tools.shutil, "which", lambda _: None)
    assert not host_tools.ensure_ffmpeg(auto_install=True)
    assert "https://ffmpeg.org/download.html" in capsys.readouterr().out
    windows[0].assert_not_called()
