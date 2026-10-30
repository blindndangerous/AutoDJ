"""Read-only checks that AutoDJ is idle before setup changes its runtime."""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Process:
    """The small subset of host process information used by the setup guard."""

    pid: int
    name: str
    command: str
    cwd: str = ""
    parent_pid: int = 0


def _windows_processes() -> list[Process]:
    """Read process names and command lines without opening a console window."""
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    payload = json.loads(result.stdout or "[]")
    if payload is None:
        payload = []
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise ValueError("Windows returned an unexpected process list")
    processes = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item["ProcessId"])
        except (KeyError, TypeError, ValueError):
            continue
        processes.append(
            Process(
                pid,
                str(item.get("Name") or ""),
                str(item.get("CommandLine") or ""),
                parent_pid=int(item.get("ParentProcessId") or 0),
            )
        )
    return processes


def _posix_processes() -> list[Process]:
    """Read Linux process command lines and working directories from procfs."""
    proc = Path("/proc")
    if proc.is_dir():
        processes = []
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                command = shlex.join(
                    part.decode(errors="replace")
                    for part in (entry / "cmdline").read_bytes().split(b"\0")
                    if part
                )
                name = (entry / "comm").read_text(errors="replace").strip()
                cwd = str((entry / "cwd").resolve())
            except (OSError, PermissionError):
                # Protected processes are not inspectable; the procfs listing as
                # a whole remains usable, while our own process is always visible.
                continue
            try:
                status = (entry / "status").read_text(errors="replace")
                parent_match = re.search(r"^PPid:\s*(\d+)$", status, re.MULTILINE)
                parent_pid = int(parent_match.group(1)) if parent_match else 0
            except OSError:
                parent_pid = 0
            processes.append(Process(int(entry.name), name, command, cwd, parent_pid))
        return processes
    # macOS and other Unix hosts have no procfs. A failed `ps` must fail closed.
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,comm=,args="],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    processes = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            processes.append(Process(int(parts[0]), parts[2], parts[3], parent_pid=int(parts[1])))
    return processes


def _processes() -> list[Process]:
    """Return process data, raising when the host cannot provide it."""
    return _windows_processes() if os.name == "nt" else _posix_processes()


def updater_owner(root: Path) -> int | None:
    """Find the active updater's launcher, if setup runs inside its process tree.

    New updaters identify themselves through an environment marker. For an
    older updater, infer ownership only when the updater lock is already
    present and held by another process, then match a serving-lock PID in the
    current parent chain. This never creates a missing updater lock.
    """
    marker = os.environ.get("AUTODJ_SETUP_UPDATE", "")
    if marker.isdigit():
        return int(marker)

    folder = Path(root) / ".uv" / "update"
    lock = folder / "lock"
    if not lock.is_file():
        return None
    from autodj.update_contract import other_servers, try_lock, unlock

    try:
        handle = try_lock(lock)
    except OSError as exc:
        raise RuntimeError(f"Could not inspect the AutoDJ updater lock at {lock}: {exc}") from exc
    if handle is not None:
        unlock(handle)
        return None

    try:
        processes = _processes()
        serving = set(other_servers(folder, set()))
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not verify ownership of the active AutoDJ update ({exc}); "
            "setup cannot safely continue."
        ) from exc
    parents = {process.pid: process.parent_pid for process in processes}
    pid = os.getppid()
    visited: set[int] = set()
    for _ in range(32):
        if pid <= 0 or pid in visited:
            break
        if pid in serving:
            return pid
        visited.add(pid)
        pid = parents.get(pid, 0)
    raise RuntimeError(
        "Another AutoDJ update is running in this checkout; wait for it to finish before setup."
    )


def _activity(name: str, command: str) -> str | None:
    """Classify actual AutoDJ CLI argv, excluding shells and Python snippets."""
    executable = Path(name.replace("\\", "/")).name.casefold()
    is_python = bool(re.fullmatch(r"(?:python|pypy)(?:w)?(?:\d+(?:\.\d+)*)?(?:\.exe)?", executable))
    is_autodj = executable in {"autodj", "autodj.exe"}
    if not (is_python or is_autodj):
        return None
    try:
        words = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return None
    words = [word.strip('"') for word in words]
    if is_python:
        index = 1
        arguments = []
        while index < len(words):
            word = words[index]
            if word == "-c":
                return None
            if word == "-m":
                if index + 1 >= len(words):
                    return None
                if words[index + 1] == "autodj.setup_model":
                    return "model verification"
                if words[index + 1] != "autodj":
                    return None
                arguments = words[index + 2 :]
                break
            if word in {"-W", "-X", "--check-hash-based-pycs"}:
                index += 2
                continue
            if word.startswith("-"):
                index += 1
                continue
            if not word.replace("\\", "/").casefold().endswith("scripts/run.py"):
                return None
            arguments = words[index + 1 :]
            break
    else:
        arguments = words[1:]
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word == "--config":
            index += 2
            continue
        if word.startswith("-"):
            index += 1
            continue
        if word in {"serve", "index", "analyse", "analyze", "worker"}:
            return word
        # The first non-option token is the CLI verb; later values are not verbs.
        return None
    return None


def _scoped(process: Process, root: Path, environment: Path) -> bool:
    """Whether a process belongs to this checkout or its selected venv."""
    root_text = str(root).casefold().replace("\\", "/").rstrip("/")
    env_text = str(environment).casefold().replace("\\", "/").rstrip("/")
    command = process.command.casefold().replace("\\", "/")
    cwd = process.cwd.casefold().replace("\\", "/").rstrip("/")

    def contained(path: str) -> bool:
        return bool(re.search(r"(?<![\w.-])" + re.escape(path) + r"(?=$|[/\s\"'])", command))

    return (
        contained(root_text)
        or contained(env_text)
        or cwd == root_text
        or cwd.startswith(root_text + "/")
    )


def _busy_port(port: int) -> bool:
    """Whether a local process currently owns the configured loopback port."""
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True


def ensure_idle(root: Path, environment: Path, port: int, updater_pid: int | None = None) -> None:
    """Stop setup before mutation when a known AutoDJ process or server is active.

    The launcher's existing serving locks detect servers started through the
    checkout launcher. Process inspection additionally catches direct Python
    CLI jobs when their command line or working directory identifies this
    checkout. This is a point-in-time check: it cannot prevent a process from
    starting immediately afterwards, and arbitrary custom scripts may not be
    identifiable as AutoDJ.
    """
    from autodj.update_contract import other_servers

    # CheckoutUpdater's serving locks are rooted here, independently of the
    # configured shared library/index path.
    state_dir = root / ".uv" / "update"
    own_pids = {os.getpid()}
    if updater_pid is None:
        updater = os.environ.get("AUTODJ_SETUP_UPDATE", "")
        updater_pid = int(updater) if updater.isdigit() else None
    if updater_pid is not None:
        own_pids.add(updater_pid)
    servers = other_servers(state_dir, own_pids)
    if servers:
        raise RuntimeError(
            "AutoDJ is running from this checkout (launcher process "
            f"{', '.join(map(str, servers))}); stop it before applying setup."
        )
    if _busy_port(port):
        raise RuntimeError(
            f"Port {port} is in use; stop its server or choose a free port before applying setup."
        )
    try:
        processes = _processes()
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not inspect running processes ({exc}); setup cannot safely verify that "
            "AutoDJ is idle. Close AutoDJ and rerun setup."
        ) from exc
    found = [
        (process, activity)
        for process in processes
        if process.pid not in own_pids
        and (activity := _activity(process.name, process.command))
        and _scoped(process, root.resolve(), environment.resolve())
    ]
    if found:
        details = ", ".join(f"{activity} (process {process.pid})" for process, activity in found)
        raise RuntimeError(f"AutoDJ {details} is still running; stop it before applying setup.")


__all__ = ["Process", "ensure_idle", "updater_owner"]
