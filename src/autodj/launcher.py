"""Dispatch AutoDJ through the saved local runtime selection."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess  # nosec B404 -- explicit Python interpreter and argv, never a shell
import sys
import time
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autodj.child_jobs import bind_to_autodj
from autodj.update_contract import LAUNCHER_PID_ENV, UPDATE_DIR_ENV
from autodj.updates import RESTART_EXIT_CODE, SUPERVISOR_ENV, UPDATE_EXIT_CODE, UPDATER_ENV

if TYPE_CHECKING:
    from autodj.checkout_update import CheckoutUpdater

ROOT = Path(__file__).resolve().parents[2]
STATE = Path(".uv/setup.json")
BACKENDS = {"amd", "nvidia", "cpu", "no-model"}
#: More restarts than this within RESTART_WINDOW_SECONDS stop the loop.
MAX_RESTARTS = 5
RESTART_WINDOW_SECONDS = 60.0
UPDATE_RUNNING = (
    "AutoDJ: an update is running from this AutoDJ folder; start AutoDJ again when it finishes."
)


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


def _command_index(arguments: list[str]) -> int:
    """The index of the command word after the global options (``len`` when there is none)."""
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word == "--config":
            index += 2
        elif word.startswith("--config=") or word in {"-v", "--verbose"}:
            index += 1
        else:
            return index
    return len(arguments)


def runs_serve(arguments: list[str]) -> bool:
    """Whether *arguments* start the server: ``serve`` after the global options."""
    index = _command_index(arguments)
    return index < len(arguments) and arguments[index] == "serve"


def global_options(arguments: list[str]) -> list[str]:
    """The options before the command (``--config``, ``-v``), for the update's own commands."""
    return arguments[: _command_index(arguments)]


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


class StopRequested(Exception):
    """The launcher got a stop signal while the server ran: stop, never restart."""

    def __init__(self, signum: int, status: int) -> None:
        """*signum* is the stop signal; *status* the server's exit status."""
        super().__init__(signum, status)
        self.signum = signum
        self.status = status


def _signal_came_from_the_terminal() -> bool:
    """Whether a terminal signal also reached the server: the launcher is in its foreground group.

    The server shares the launcher's process group, so Ctrl+C, a closed
    terminal or Ctrl+\\ reach both.  Without a controlling terminal, or in
    the background, the signal was sent to the launcher alone.
    """
    if sys.platform == "win32":
        return False
    try:
        return os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp()
    except (OSError, ValueError, AttributeError):
        return False


Watch = Callable[[subprocess.Popen[bytes], Callable[[], bool]], None]


def _run_child(
    command: list[str], environment: Mapping[str, str], watch: Watch | None = None
) -> int:
    """Run the server once and wait for it.

    *watch*, when given, is called with the started server and a function
    that says whether a stop signal has arrived, before the launcher waits:
    an update watches its new version start (``CheckoutUpdater.watch``).

    The server stays in the launcher's process group and the terminal's
    foreground, so Ctrl+C, a closed terminal and Ctrl+\\ reach it directly
    and it can still ask a question on the terminal.  The launcher only
    notes that a stop was asked for, so it never restarts after one.  It
    passes SIGINT, SIGHUP or SIGQUIT on only when they came to it alone
    (no terminal, or not in the foreground): a second SIGINT would make
    uvicorn skip its graceful shutdown.  SIGTERM is always passed on, since
    uvicorn treats a second SIGTERM like the first (only a repeated SIGINT
    forces an exit); systemd sends it to both, which is the same.  On
    Windows, Ctrl+C reaches both and the launcher keeps waiting.

    The handlers are in place before the server starts, so no signal can
    fall between the two and leave a server nobody waits for.

    Raises:
        StopRequested: A stop signal arrived; the server has exited.
    """
    stopped: list[int] = []
    started: list[subprocess.Popen[bytes]] = []
    previous: dict[int, Any] = {}
    if sys.platform != "win32":
        alone_only = {signal.SIGINT, signal.SIGHUP, signal.SIGQUIT}

        def note(signum: int, _frame: object) -> None:
            stopped.append(signum)
            if not started:
                return
            if signum in alone_only and _signal_came_from_the_terminal():
                return
            with contextlib.suppress(OSError):
                started[0].send_signal(signum)

        for stop in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
            previous[stop] = signal.signal(stop, note)
    try:
        if stopped:
            raise StopRequested(stopped[0], 0)
        process = subprocess.Popen(command, env=dict(environment))  # nosec B603
        started.append(process)
        # Windows: a job object ends the server with the launcher, however the
        # launcher ends (the shared helper the mastering processes use).
        bind_to_autodj(process)
        if stopped:
            # A stop that came between the check above and the start.
            with contextlib.suppress(OSError):
                process.send_signal(signal.SIGTERM)
        if watch is not None:
            try:
                watch(process, lambda: bool(stopped))
            except KeyboardInterrupt:
                stopped.append(signal.SIGINT)
        while True:
            try:
                status = process.wait()
                break
            except KeyboardInterrupt:
                stopped.append(signal.SIGINT)
    finally:
        for handled, handler in previous.items():
            signal.signal(handled, handler)
    if stopped:
        raise StopRequested(stopped[0], status)
    return status


def _end_with_signal(signum: int) -> int:
    """End the launcher the way the signal it got would have; returns ``128 + signum`` otherwise.

    systemd counts a service killed by SIGTERM as stopped cleanly, but an
    exit status of 143 as failed, so on POSIX the launcher dies of the
    same signal.  SIGQUIT is the exception: dying of it would dump core.
    """
    if os.name != "nt" and signum != getattr(signal, "SIGQUIT", None):
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    return 128 + signum


def run_supervised(
    command: list[str],
    environment: Mapping[str, str],
    *,
    run_child: Callable[..., int] = _run_child,
    clock: Callable[[], float] = time.monotonic,
    end_with_signal: Callable[[int], int] = _end_with_signal,
    updater: CheckoutUpdater | None = None,
    plan: Callable[[], tuple[list[str], dict[str, str]]] | None = None,
) -> int:
    """Run ``serve`` and start it again each time it exits with RESTART_EXIT_CODE.

    Settings, Updates' Restart makes the server exit with that code.  When
    MAX_RESTARTS restarts fall within RESTART_WINDOW_SECONDS the loop ends
    with that code, so a server that asks again and again does not spin.
    A Ctrl+C or SIGTERM always wins over a pending restart.

    With an *updater*, the server is told it may ask for an update
    (UPDATER_ENV), and exit status UPDATE_EXIT_CODE runs the update while no
    server runs.  *plan* then gives the command and environment again, since
    setup may change the runtime.  A new version is watched as it starts and
    rolled back when it is not healthy; a stop signal before it is healthy
    puts the checkout back too.  Updates count as restarts for the limit.
    """
    extra = {SUPERVISOR_ENV: "1"}
    if updater is not None:
        extra[UPDATER_ENV] = "launcher"
        extra[UPDATE_DIR_ENV] = str(updater.folder)
        extra[LAUNCHER_PID_ENV] = str(os.getpid())
    child_environment = {**environment, **extra}
    restarts: deque[float] = deque(maxlen=MAX_RESTARTS)
    watching = False

    def replan() -> None:
        nonlocal command, child_environment
        if plan is not None:
            command, fresh = plan()
            child_environment = {**fresh, **extra}

    while True:
        try:
            if watching and updater is not None:
                status = run_child(command, child_environment, watch=updater.watch)
            else:
                status = run_child(command, child_environment)
        except StopRequested as stop:
            if watching and updater is not None and updater.pending:
                updater.stopped_while_starting(stop.signum)
            return end_with_signal(stop.signum)
        except OSError:
            if not (watching and updater is not None and updater.pending):
                raise
            # The new version could not even start: put the old one back.
            status = -1
        if watching and updater is not None:
            watching = False
            if updater.pending:
                attempt = updater.failed_start()
                if attempt.stopped is not None:
                    return end_with_signal(attempt.stopped)
                if attempt.failed:
                    return 1
                replan()
                continue
        if status == UPDATE_EXIT_CODE and updater is not None:
            attempt = updater.update()
            if attempt.stopped is not None:
                return end_with_signal(attempt.stopped)
            if attempt.failed:
                return 1
            replan()
            watching = attempt.switched
        elif status != RESTART_EXIT_CODE:
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


def _runtime_environment(environment: Path, backend: str) -> dict[str, str]:
    """The environment for AutoDJ in the selected runtime: its device and cache policy."""
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
    return child_environment


def _child_plan(selected: tuple[Path, str]) -> tuple[list[str], dict[str, str]]:
    """``python -m autodj`` in the selected runtime, and its environment."""
    environment, backend = selected
    child_environment = _runtime_environment(environment, backend)
    if environment != Path(sys.prefix).resolve():
        return [str(_python_path(environment)), "-m", "autodj"], child_environment
    child_environment["AUTODJ_RUNTIME"] = "current"
    return [sys.executable, "-m", "autodj"], child_environment


def _supervise(arguments: list[str], selected: tuple[Path, str]) -> int:
    """Run ``serve`` as a child, so Restart and Update can start it again."""
    from autodj.checkout_update import CheckoutUpdater

    def current() -> tuple[list[str], dict[str, str]]:
        # Setup may change the selection; read it again for each start.
        return _child_plan(_selection(ROOT) or selected)

    def plan() -> tuple[list[str], dict[str, str]]:
        base, environment = current()
        return [*base, *arguments], environment

    updater = CheckoutUpdater(ROOT, global_options(arguments), current)
    updater.hold_serving()

    def end_with_signal(signum: int) -> int:
        # Dying of the signal skips the finally below: let go first.
        updater.release_serving()
        return _end_with_signal(signum)

    try:
        if updater.update_running():
            # Its checkout is half switched until it finishes.
            print(UPDATE_RUNNING, file=sys.stderr)
            return 1
        updater.recover_interrupted()
        base, child_environment = _child_plan(selected)
        return run_supervised(
            [*base, *arguments],
            child_environment,
            updater=updater,
            plan=plan,
            end_with_signal=end_with_signal,
        )
    finally:
        updater.release_serving()


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
            child_environment = _runtime_environment(environment, backend)
            # Compare prefixes, since venv executables can be symlinks.
            other_runtime = environment != Path(sys.prefix).resolve()
            if _supervised(arguments, os.environ):
                return _supervise(arguments, selected)
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
