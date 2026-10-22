"""Optional host-tool installation shared by setup and Doctor; standard library only."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 -- fixed WinGet package and argv, no shell
import sys


def _refresh_windows_path() -> None:
    """Pick up WinGet's registered paths in this process, without changing the registry."""
    import winreg

    paths = os.environ.get("PATH", "").split(os.pathsep)
    for hive, key in (
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            with winreg.OpenKey(hive, key) as registry:
                value, _ = winreg.QueryValueEx(registry, "Path")
            paths.extend(os.path.expandvars(value).split(os.pathsep))
        except OSError:
            continue
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys(path for path in paths if path))


def ensure_ffmpeg(*, auto_install: bool = False) -> bool:
    """Offer a user-scoped Windows install; return False if unavailable or declined.

    An attempted install that fails raises RuntimeError or OSError.
    """
    if sys.platform == "win32":
        _refresh_windows_path()
    if shutil.which("ffmpeg"):
        return True
    winget = shutil.which("winget") if sys.platform == "win32" else None
    if winget is None:
        print("Install FFmpeg from https://ffmpeg.org/download.html and add it to PATH.")
        return False
    print(
        "Install FFmpeg for your Windows account through WinGet, accepting its package agreements."
    )
    if not auto_install:
        if not sys.stdin.isatty():
            return False
        if input("Download and install FFmpeg now? [y/N] ").strip().lower() not in {"y", "yes"}:
            return False
    result = subprocess.run(  # nosec B603 -- resolved executable, fixed arguments
        [
            winget,
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
    if result.returncode:
        raise RuntimeError(f"WinGet could not install FFmpeg (exit code {result.returncode}).")
    _refresh_windows_path()
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "WinGet finished, but FFmpeg is still not on PATH. Reopen the terminal and run Doctor again."
        )
    return True
