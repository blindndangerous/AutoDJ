"""Dispatch AutoDJ through the saved local runtime selection."""

from __future__ import annotations

import json
import os
import signal
import subprocess  # nosec B404 -- explicit Python interpreter and argv, never a shell
import sys
import time
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from autodj.updates import RESTART_EXIT_CODE, SUPERVISOR_ENV

ROOT = Path(__file__).resolve().parents[2]
STATE = Path(".uv/setup.json")
BACKENDS = {"amd", "nvidia", "cpu", "no-model"}
#: More restarts than this within RESTART_WINDOW_SECONDS stop the loop.
MAX_RESTARTS = 5
RESTART_WINDOW_SECONDS = 60.0


def _python_path(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _selection(root: Path) -> tuple[Path, str] | None:
    """Return the selected environment, or None when no local selection applies."""
    # Installed wheels may be launched from arbitrary working directories. Only
    # a source checkout owns the setup state file.
    if not (root / "pyproject.toml").is_file():
        return None
    state_path = root / STATE
    if not state_path.exists():
        return None
    try:
        state: Any = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read saved runtime selection at {state_path}: {exc}") from exc
    if not isinstance(state, dict):
        raise ValueError(
            f"Invalid saved runtime selection at {state_path}: expected a JSON object."
        )
    environment_value = state.get("environment")
    backend = state.get("backend")
    if not isinstance(environment_value, str) or not environment_value.strip():
        raise ValueError(f"Invalid saved environment path in {state_path}. Run setup again.")
    if not isinstance(backend, str) or backend not in BACKENDS:
        raise ValueError(
            f"Invalid saved backend in {state_path}. Choose cpu, nvidia, or amd and run setup again."
        )
    environment_path = Path(environment_value)
    if not environment_path.is_absolute():
        environment_path = root / environment_path
    environment = environment_path.resolve()
    python = _python_path(environment)
    if not python.is_file():
        raise ValueError(f"Saved environment interpreter is missing: {python}. Run setup again.")
    return environment, backend


def _cli() -> int:
    from autodj.cli import cli

    result = cli()
    return result if isinstance(result, int) else 0


def run_setup(arguments: list[str]) -> int:
    """Bootstrap hardware-aware setup without importing application dependencies."""
    setup = ROOT / "scripts/setup.py"
    if not setup.is_file():
        print("Guided setup requires an AutoDJ source checkout.", file=sys.stderr)
        return 1
    environment = os.environ.copy()
    environment["AUTODJ_RUNTIME"] = "current"
    try:
        return subprocess.run(  # nosec B603 -- local setup script and argv, no shell
            [sys.executable, str(setup), *arguments], env=environment, check=False
        ).returncode
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"Could not start AutoDJ setup: {exc}", file=sys.stderr)
        return 1


def runs_serve(arguments: list[str]) -> bool:
    """Whether *arguments* start the server: ``serve`` after the global options."""
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word == "--config":
            index += 2
        elif word.startswith("--config=") or word in {"-v", "--verbose"}:
            index += 1
        else:
            return word == "serve"
    return False


def _supervised(arguments: list[str], environ: Mapping[str, str]) -> bool:
    """Whether this launch restarts ``serve`` when it asks to (the checkout's launcher).

    Only the source checkout's ``autodj`` and ``autodj.cmd`` set
    AUTODJ_BOOTSTRAP; a server they already supervise does not loop again.
    """
    return (
        environ.get("AUTODJ_BOOTSTRAP") == "1"
        and environ.get(SUPERVISOR_ENV) != "1"
        and runs_serve(arguments)
    )


def _kill_with_launcher(process: subprocess.Popen[bytes]) -> object | None:
    """On Windows, put *process* in a job that ends it when the launcher ends.

    A POSIX child gets SIGTERM from :func:`_run_child` instead.  Returns
    the job handle, which must stay open while the child runs.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _Limits(ctypes.Structure):
            _fields_ = [
                ("per_process_time", ctypes.c_int64),
                ("per_job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t),
                ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class _ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", _Limits),
                ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(
            wintypes.HANDLE(job), 9, ctypes.byref(limits), ctypes.sizeof(limits)
        )
        handle = getattr(process, "_handle", None)
        if handle is None or not kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(job), wintypes.HANDLE(int(handle))
        ):
            kernel32.CloseHandle(wintypes.HANDLE(job))
            return None
        return job
    except (OSError, AttributeError, ValueError):
        return None


def _run_child(command: list[str], environment: Mapping[str, str]) -> int:
    """Run the server once and wait for it, passing SIGTERM on to it.

    Ctrl+C reaches the server as well, which shuts down by itself, so the
    launcher keeps waiting for it rather than leaving it behind.
    """
    process = subprocess.Popen(command, env=dict(environment))  # nosec B603 -- argv only
    job = _kill_with_launcher(process)
    previous: Any = None
    if os.name != "nt":

        def forward(signum: int, _frame: object) -> None:
            process.send_signal(signum)

        previous = signal.signal(signal.SIGTERM, forward)
    try:
        while True:
            try:
                return process.wait()
            except KeyboardInterrupt:
                continue
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        del job


def run_supervised(
    command: list[str],
    environment: Mapping[str, str],
    *,
    run_child: Callable[[list[str], Mapping[str, str]], int] = _run_child,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run ``serve`` and start it again each time it exits with RESTART_EXIT_CODE.

    Settings, Updates' Restart makes the server exit with that code.  When
    MAX_RESTARTS restarts fall within RESTART_WINDOW_SECONDS the loop ends
    with that code, so a server that asks again and again does not spin.
    """
    child_environment = {**environment, SUPERVISOR_ENV: "1"}
    restarts: deque[float] = deque(maxlen=MAX_RESTARTS)
    while True:
        status = run_child(command, child_environment)
        if status != RESTART_EXIT_CODE:
            return status
        now = clock()
        restarts.append(now)
        if len(restarts) == MAX_RESTARTS and now - restarts[0] < RESTART_WINDOW_SECONDS:
            print(
                f"AutoDJ: restarted {MAX_RESTARTS} times within "
                f"{RESTART_WINDOW_SECONDS:.0f} seconds; not restarting again.",
                file=sys.stderr,
            )
            return status
        print("AutoDJ: restarting.", file=sys.stderr)


def main() -> int:
    """Run AutoDJ with the selected local runtime when one has been saved."""
    arguments = sys.argv[1:]
    if arguments[:1] == ["setup"]:
        return run_setup(arguments[1:])
    if os.environ.get("AUTODJ_RUNTIME", "").casefold() == "current":
        return _cli()
    try:
        selected = _selection(ROOT)
        if selected is None and (ROOT / "scripts/setup.py").is_file():
            bootstrap = os.environ.get("AUTODJ_BOOTSTRAP") == "1"
            help_requested = not arguments or arguments[0] in {"--help", "-h"}
            if not help_requested and sys.stdin.isatty():
                print(
                    "Welcome to AutoDJ. Let's detect your hardware and set up your music library."
                )
                status = run_setup([])
                if status:
                    return status
                selected = _selection(ROOT)
                if selected is None:
                    print(
                        "Setup did not select a runtime. Run `autodj setup` to retry.",
                        file=sys.stderr,
                    )
                    return 1
            elif bootstrap:
                print(
                    "AutoDJ: run `autodj setup` to detect your hardware and install its runtime.\n"
                    "Then use the same launcher for serve, doctor, index, or analyse.\n"
                    "For unattended setup, use `autodj setup --help` for the required options."
                )
                return 0 if help_requested else 1
        if selected is not None:
            environment, backend = selected
            child_environment = os.environ.copy()
            if backend != "no-model":
                child_environment.pop("AUTODJ_NO_MODEL", None)
            if backend in {"cpu", "no-model"}:
                child_environment["AUTODJ_GPU"] = "0"
                if backend == "no-model":
                    child_environment["AUTODJ_NO_MODEL"] = "1"
            elif backend == "amd":
                for relative, variable in (
                    ("miopen/db", "MIOPEN_USER_DB_PATH"),
                    ("miopen/cache", "MIOPEN_CUSTOM_CACHE_DIR"),
                ):
                    cache = environment / relative
                    cache.mkdir(parents=True, exist_ok=True)
                    child_environment[variable] = str(cache)
            # Compare prefixes, since venv executables can be symlinks.
            other_runtime = environment != Path(sys.prefix).resolve()
            if _supervised(arguments, os.environ):
                # The server runs as a child, so Restart can start it again.
                python = str(_python_path(environment)) if other_runtime else sys.executable
                if not other_runtime:
                    child_environment["AUTODJ_RUNTIME"] = "current"
                return run_supervised([python, "-m", "autodj", *arguments], child_environment)
            if other_runtime:
                completed = subprocess.run(  # nosec B603 -- user-selected runtime, argv only
                    [str(_python_path(environment)), "-m", "autodj", *sys.argv[1:]],
                    env=child_environment,
                    check=False,
                )
                return completed.returncode
            # Apply device/cache policy even when already in the selected runtime.
            if backend != "no-model":
                os.environ.pop("AUTODJ_NO_MODEL", None)
            for variable in (
                "AUTODJ_GPU",
                "AUTODJ_NO_MODEL",
                "MIOPEN_USER_DB_PATH",
                "MIOPEN_CUSTOM_CACHE_DIR",
            ):
                if variable in child_environment:
                    os.environ[variable] = child_environment[variable]
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as exc:
        print(f"AutoDJ: {exc}", file=sys.stderr)
        return 1
    return _cli()
