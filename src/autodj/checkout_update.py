"""Update a source checkout between two runs of ``serve``: the launcher's half of Update.

Settings, Updates' Update button makes the server write a request into the
hand-off record (:data:`autodj.updates.UPDATE_STATE`) and exit with
:data:`autodj.updates.UPDATE_EXIT_CODE`.  While no server runs, the
launcher's supervise loop (:func:`autodj.launcher.run_supervised`) asks a
:class:`CheckoutUpdater` to follow the upgrade checklist:

1. check again itself, for the channel the server follows, and refuse
   anything but a newer version on that channel;
2. refuse a checkout with any change (``git status --porcelain``);
3. make a backup with ``autodj backup``;
4. fetch, then switch: a release tag detached, or ``master``
   fast-forwarded to ``origin/master``;
5. run the checkout's setup unattended (``setup --yes``), which reuses and
   verifies the saved runtime without changing its packages;
6. run Doctor read-only, and stop if the library needs a migration.

The loop then starts the new server and :meth:`CheckoutUpdater.watch`
waits for it to be healthy: still running, and answering ``GET /healthz``
with 200 and ``"ok"`` within HEALTH_TIMEOUT_SECONDS.  Anything that fails
after the switch puts the checkout back on the commit it was on, restores
the runtime selection, runs setup for it, and starts the old version.  A
stop signal puts the checkout back and stops.  The result goes into the
hand-off record for the page.

The request names only its id, the channel the server follows and where
the server listens; the launcher validates each and works out the target
itself.  Standard library only, like the launcher.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import shlex
import signal
import ssl
import subprocess  # nosec B404 -- fixed git and AutoDJ argv lists, never a shell
import sys
import time
import tomllib
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Imported now, before the checkout changes under this process: the update
# check imports these lazily, and the new version's copies must not mix in.
import autodj.child_jobs
import autodj.fsutil
import autodj.version
from autodj import updates
from autodj.updates import (
    CHANNELS,
    CHECK_ENV,
    DEVELOPMENT_BRANCH,
    FINAL_UPDATE_STATES,
    TAG_PATTERN,
    HealthProbe,
    Installation,
    UpdateCheck,
    UpdateSettings,
    parse_health_probe,
    read_update_state,
    update_state_path,
    valid_update_id,
    version_key,
    write_update_state,
)

#: Where the updater keeps the backups it makes, inside the ignored ``.uv``.
BACKUPS = Path(".uv") / "update" / "backups"
#: Held while an update runs, so two launchers never update one checkout.
LOCK = Path(".uv") / "update" / "lock"
#: The updater's own backups beyond this many, oldest first, are deleted.
KEEP_BACKUPS = 3
#: A lock older than this is left over from a launcher that was killed.
LOCK_STALE_SECONDS = 4 * 3600.0
FETCH_TIMEOUT_SECONDS = 300.0
GIT_TIMEOUT_SECONDS = 120.0
BACKUP_TIMEOUT_SECONDS = 1800.0
SETUP_TIMEOUT_SECONDS = 1800.0
DOCTOR_TIMEOUT_SECONDS = 600.0
#: How long a new server has to answer ``/healthz`` before it is rolled back.
HEALTH_TIMEOUT_SECONDS = 180.0
HEALTH_POLL_SECONDS = 1.0
#: How long an unhealthy new server has to stop before it is killed.
STOP_GRACE_SECONDS = 30.0
SETUP_STATE = Path(".uv") / "setup.json"

DOWNGRADE = (
    "AutoDJ does not move to an older version by itself, because a newer version's "
    "changes to your library may not be reversible."
)

#: ``run(argv, *, timeout, cwd=None, env=None, capture=True, interruptible=True)``.
Run = Callable[..., subprocess.CompletedProcess[str]]
Runtime = Callable[[], tuple[list[str], dict[str, str]]]


@dataclass(frozen=True)
class Attempt:
    """What an update did.

    ``switched``: the checkout runs a new version; start it and :meth:`watch` it.
    ``failed``: the checkout could not be put back; do not start a server.
    ``stopped``: a stop signal ended the update; end with that signal.
    Otherwise nothing changed, or it was put back: start the old version.
    """

    switched: bool = False
    failed: bool = False
    stopped: int | None = None


@dataclass(frozen=True)
class _Previous:
    """Where the checkout was before the update."""

    commit: str
    branch: str | None
    setup_state: bytes | None


class _Refuse(Exception):
    """Refuse the update before anything changed."""

    def __init__(self, reason: str, commands: list[str] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.commands = commands or []


class _RollBack(_Refuse):
    """Something failed after the switch: put the checkout back."""


class _Stop(Exception):
    """A stop signal arrived during the update."""


def probe_health(probe: HealthProbe, timeout: float = 2.0) -> bool:
    """Whether the server answers ``GET /healthz`` with 200 and ``"ok"``.

    No proxy is used.  Over HTTPS the certificate is not checked: the probe
    asks this computer (or the address the server is bound to), sends
    nothing secret, and the certificate names the public host name.
    """
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
    if probe.scheme == "https":
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # nosec B501 -- local readiness probe, see above
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(probe.url, headers={"Host": probe.host}, method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:  # nosec B310 -- http(s) only
            if response.status != 200:
                return False
            body = json.loads(response.read(4096).decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException):
        return False
    return isinstance(body, dict) and body.get("status") == "ok"


def _stop_process(process: subprocess.Popen[Any], grace: float = STOP_GRACE_SECONDS) -> None:
    """Ask a server to stop, then kill it if it is still running after *grace* seconds."""
    with contextlib.suppress(OSError):
        if sys.platform == "win32":
            process.terminate()
        else:
            process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


class CheckoutUpdater:
    """Update the checkout at *root* when ``serve`` asks, and roll back when it fails.

    *runtime* gives the selected runtime's command prefix (``python -m
    autodj``) and environment; it is called again after setup.  *global_options*
    are the options given before ``serve`` (``--config``), passed on to
    backup and Doctor.  The rest are stand-ins for the tests.
    """

    def __init__(
        self,
        root: Path,
        global_options: list[str],
        runtime: Runtime,
        *,
        run: Run | None = None,
        installation: Callable[[], Installation] | None = None,
        check: Callable[[Installation, UpdateSettings], UpdateCheck] | None = None,
        probe: Callable[[HealthProbe], bool] = probe_health,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        setup_python: str = sys.executable,
        windows: bool = os.name == "nt",
    ) -> None:
        """See the class."""
        self.root = Path(root)
        self.global_options = list(global_options)
        self._runtime = runtime
        self._run = run or self._spawn
        self._installation = installation or self._detect
        self._check = check or self._check_now
        self._probe_fn = probe
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._setup_python = setup_python
        self._windows = windows
        self.state_path = update_state_path(self.root)
        self._stopped: list[int] = []
        self._current: tuple[subprocess.Popen[str], int | None, bool] | None = None
        self._reset()

    def _reset(self) -> None:
        self._record: dict[str, Any] = {}
        self._previous: _Previous | None = None
        self._switched = False
        self._probe: HealthProbe | None = None
        self._target_label = ""
        self._previous_label = ""
        self._backup: Path | None = None
        self._locked = False
        #: After :meth:`watch`: True when the new server was healthy, else None.
        self.started_ok: bool | None = None
        self._start_failure = ""

    # ------------------------------------------------------------------
    # Running commands
    # ------------------------------------------------------------------

    def _spawn(
        self,
        argv: list[str],
        *,
        timeout: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        capture: bool = True,
        interruptible: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run *argv* (no shell) in its own process tree, which a stop or timeout ends.

        An *interruptible* step (fetch, backup, setup, Doctor) is asked to stop
        when a stop signal arrives; ``git switch`` and ``git merge`` are left
        to finish, so a stop never leaves one half done.
        """
        options: dict[str, Any] = {}
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        pipe = subprocess.PIPE if capture else None
        process = subprocess.Popen(  # nosec B603 -- argv list built here, no shell
            argv,
            cwd=cwd,
            env=None if env is None else dict(env),
            stdin=subprocess.DEVNULL,
            stdout=pipe,
            stderr=pipe,
            text=True,
            encoding="utf-8",
            errors="replace",
            **options,
        )
        job = autodj.child_jobs.job_for_tree(process)
        self._current = (process, job, interruptible)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            updates._kill_tree(process, job)
            job = None
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
            raise
        finally:
            self._current = None
            if job is not None:
                autodj.child_jobs.end_job(job)
        return subprocess.CompletedProcess(argv, process.returncode, stdout or "", stderr or "")

    def _step(self, argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        """Run one step; a step that cannot start or times out answers exit status -1."""
        try:
            return self._run(argv, **options)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(argv, -1, "", "timed out")
        except OSError as exc:
            return subprocess.CompletedProcess(argv, -1, "", type(exc).__name__)

    def _runner(self, argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        """The update check's runner (:data:`autodj.updates.Runner`)."""
        return self._run(argv, **options)

    def _git(
        self, *args: str, timeout: float = GIT_TIMEOUT_SECONDS, interruptible: bool = True
    ) -> subprocess.CompletedProcess[str]:
        ssh = (
            updates._batch_ssh_command(self.root, runner=self._runner)
            if args[:1] == ("fetch",)
            else None
        )
        return self._step(
            ["git", *args],
            timeout=timeout,
            cwd=self.root,
            env=updates._git_environment(ssh),
            interruptible=interruptible,
        )

    def _git_value(self, *args: str) -> str | None:
        done = self._git(*args)
        lines = done.stdout.strip().splitlines() if done.returncode == 0 else []
        return lines[0].strip() if lines else None

    def _head(self) -> str | None:
        return self._git_value("rev-parse", "--verify", "HEAD^{commit}")

    def _cli(self, *words: str) -> str:
        """A launcher command line for this computer, with the options given to ``serve``."""
        launcher = ".\\autodj.cmd" if self._windows else "./autodj"
        argv = [*self.global_options, *words]
        quoted = subprocess.list2cmdline(argv) if self._windows else shlex.join(argv)
        return f"{launcher} {quoted}".rstrip()

    # ------------------------------------------------------------------
    # The record and the lock
    # ------------------------------------------------------------------

    def _write(self, state: str, **fields: Any) -> None:
        self._record.update(fields, state=state, updated_at=self._clock())
        try:
            write_update_state(self.state_path, self._record)
        except OSError as exc:
            print(
                f"AutoDJ: could not record the update in {self.state_path}: {exc}", file=sys.stderr
            )

    def _progress(self, step: str, text: str) -> None:
        print(f"AutoDJ: update: {text}", file=sys.stderr)
        self._write("running", step=step)

    def _lock(self) -> None:
        path = self.root / LOCK
        path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(2):
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                with contextlib.suppress(OSError):
                    if self._clock() - path.stat().st_mtime > LOCK_STALE_SECONDS:
                        path.unlink()
                        continue
                raise _Refuse(
                    "Another AutoDJ launcher is updating this checkout, so this one did not."
                ) from None
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"{os.getpid()}\n")
            self._locked = True
            return
        raise _Refuse("Another AutoDJ launcher is updating this checkout, so this one did not.")

    def _unlock(self) -> None:
        if self._locked:
            with contextlib.suppress(OSError):
                (self.root / LOCK).unlink()
            self._locked = False

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def _note_stop(self, signum: int, _frame: object) -> None:
        self._stopped.append(signum)
        current = self._current
        if current is None or not current[2]:
            return
        process, job, _interruptible = current
        if sys.platform == "win32":
            if job is not None:
                autodj.child_jobs.end_job(job)
            else:
                with contextlib.suppress(OSError):
                    process.kill()
        else:
            with contextlib.suppress(OSError):
                os.killpg(process.pid, signal.SIGTERM)

    @contextlib.contextmanager
    def _signals(self) -> Iterator[None]:
        """Note stop signals instead of dying of them, so the checkout can be put back."""
        names = (
            ("SIGINT", "SIGBREAK")
            if sys.platform == "win32"
            else (
                "SIGINT",
                "SIGTERM",
                "SIGHUP",
                "SIGQUIT",
            )
        )
        previous: dict[int, Any] = {}
        for name in names:
            number = getattr(signal, name, None)
            if number is not None:
                with contextlib.suppress(ValueError, OSError):
                    previous[number] = signal.signal(number, self._note_stop)
        try:
            yield
        finally:
            for number, handler in previous.items():
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(number, handler)

    def _check_stop(self) -> None:
        if self._stopped:
            raise _Stop

    # ------------------------------------------------------------------
    # The steps
    # ------------------------------------------------------------------

    def _version(self) -> str:
        return autodj.version._project_version(self.root / "pyproject.toml")

    def _detect(self) -> Installation:
        return updates.detect_installation(
            root=self.root, version=self._version(), runner=self._runner
        )

    def _check_now(self, installation: Installation, settings: UpdateSettings) -> UpdateCheck:
        return updates.check_for_update(installation, settings, runner=self._runner)

    @staticmethod
    def _label(version: str, commit: str | None, branch: str | None, tag: str | None) -> str:
        if tag:
            return version
        short = commit[:7] if commit else "unknown"
        return f"{version}, commit {short}" + (f" on {branch}" if branch else "")

    def _target(self, installation: Installation, result: UpdateCheck) -> str:
        target = result.target
        if result.status == "available" and target is not None:
            if TAG_PATTERN.fullmatch(target):
                if version_key(target) <= version_key(installation.version):
                    raise _Refuse(
                        f"{target} is not newer than the installed {installation.version}. "
                        + DOWNGRADE
                    )
                return target
            if target == DEVELOPMENT_BRANCH:
                return target
        if result.status == "ahead":
            raise _Refuse(f"{result.message} {DOWNGRADE}")
        if result.status == "current":
            raise _Refuse("AutoDJ is already the latest version on its channel.")
        raise _Refuse(result.message or "No update is available.")

    def _backup_now(self, installation: Installation) -> None:
        base, environment = self._runtime()
        folder = self.root / BACKUPS
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(self._clock()))
        version = re.sub(r"[^0-9A-Za-z.]+", "-", installation.version)
        path = folder / f"autodj-update-{version}-{stamp}.zip"
        done = self._step(
            [*base, *self.global_options, "backup", str(path)],
            timeout=BACKUP_TIMEOUT_SECONDS,
            env=environment,
            capture=False,
        )
        self._check_stop()
        if done.returncode != 0:
            raise _Refuse(
                f"The backup failed (exit status {done.returncode}), so AutoDJ changed nothing. "
                "Run the backup yourself to see why.",
                [self._cli("backup", "backups/before-update.zip")],
            )
        self._backup = path
        self._record["backup"] = str(path)
        backups = sorted(folder.glob("autodj-update-*.zip"), key=lambda item: item.stat().st_mtime)
        for old in backups[:-KEEP_BACKUPS]:
            with contextlib.suppress(OSError):
                old.unlink()

    def _fetch(self, target: str) -> None:
        if TAG_PATTERN.fullmatch(target):
            done = self._git("fetch", "--tags", "origin", timeout=FETCH_TIMEOUT_SECONDS)
        else:
            done = self._git("fetch", "origin", DEVELOPMENT_BRANCH, timeout=FETCH_TIMEOUT_SECONDS)
        self._check_stop()
        if done.returncode != 0:
            # git's own message is not passed on: it can hold the remote's address.
            raise _Refuse(
                f"git could not fetch from the origin remote (exit status {done.returncode}), "
                "so AutoDJ changed nothing. Check the network and try again."
            )

    def _verify(self, installation: Installation, target: str) -> str:
        """The commit to switch to; refuses a target that would go back."""
        if TAG_PATTERN.fullmatch(target):
            commit = self._git_value(
                "rev-parse", "--verify", "--quiet", f"refs/tags/{target}^{{commit}}"
            )
            if commit is None:
                raise _Refuse(f"The release tag {target} is not in the checkout after fetching.")
            return commit
        remote = f"refs/remotes/origin/{DEVELOPMENT_BRANCH}"
        commit = self._git_value("rev-parse", "--verify", "--quiet", f"{remote}^{{commit}}")
        if commit is None:
            raise _Refuse(f"origin/{DEVELOPMENT_BRANCH} is not in the checkout after fetching.")
        if self._git("merge-base", "--is-ancestor", "HEAD", remote).returncode != 0:
            raise _Refuse(
                f"This version is not part of {DEVELOPMENT_BRANCH}'s history, so moving to "
                f"{DEVELOPMENT_BRANCH} could mean an older version. " + DOWNGRADE
            )
        shown = self._git("show", f"{remote}:pyproject.toml")
        try:
            newer = (
                tomllib.loads(shown.stdout)["project"]["version"] if shown.returncode == 0 else None
            )
        except (tomllib.TOMLDecodeError, KeyError, TypeError):
            newer = None
        if isinstance(newer, str) and version_key(newer) < version_key(installation.version):
            raise _Refuse(
                f"{DEVELOPMENT_BRANCH} has version {newer}, older than the installed "
                f"{installation.version}. " + DOWNGRADE
            )
        return commit

    def _switch_commands(self, target: str) -> list[str]:
        if TAG_PATTERN.fullmatch(target):
            return [f"git switch --detach {target}"]
        return [
            f"git switch {DEVELOPMENT_BRANCH}",
            f"git merge --ff-only origin/{DEVELOPMENT_BRANCH}",
        ]

    def _switch(self, target: str, commit: str) -> None:
        if TAG_PATTERN.fullmatch(target):
            done = self._git("switch", "--detach", target, interruptible=False)
        else:
            done = self._git("switch", DEVELOPMENT_BRANCH, interruptible=False)
            if done.returncode == 0:
                done = self._git(
                    "merge", "--ff-only", f"origin/{DEVELOPMENT_BRANCH}", interruptible=False
                )
        if done.returncode != 0:
            raise _RollBack(f"git could not switch to {target} (exit status {done.returncode}).")
        if self._head() != commit:
            raise _RollBack(f"After switching, the checkout is not at {commit[:7]}.")

    def _setup(self, which: str) -> int:
        script = self.root / "scripts" / "setup.py"
        if not script.is_file():
            return -1
        environment = os.environ.copy()
        environment["AUTODJ_RUNTIME"] = "current"
        done = self._step(
            [self._setup_python, str(script), "--yes"],
            timeout=SETUP_TIMEOUT_SECONDS,
            env=environment,
            capture=False,
        )
        print(
            f"AutoDJ: update: setup for {which} ended with status {done.returncode}.",
            file=sys.stderr,
        )
        return done.returncode

    def _doctor(self, target: str) -> None:
        base, environment = self._runtime()
        done = self._step(
            [*base, *self.global_options, "doctor", "--no-fix", "--json", "--offline"],
            timeout=DOCTOR_TIMEOUT_SECONDS,
            env={**environment, CHECK_ENV: "0"},
        )
        self._check_stop()
        try:
            report = json.loads(done.stdout)
            checks = report["checks"]
            if not isinstance(checks, list):
                raise TypeError("checks")
        except (ValueError, KeyError, TypeError):
            raise _RollBack(
                f"Doctor could not check the new version (exit status {done.returncode})."
            ) from None
        migration = any(
            isinstance(check, dict)
            and check.get("repair") == "migrate-index"
            and check.get("status") == "fail"
            for check in checks
        )
        if migration:
            raise _RollBack(
                "The new version needs a library migration, which AutoDJ does not run by "
                "itself. To migrate, stop AutoDJ and run these commands in the AutoDJ folder.",
                [
                    *self._switch_commands(target),
                    self._cli("setup"),
                    self._cli("migrate-index"),
                    self._cli("migrate-index", "--apply"),
                    self._cli("doctor", "--no-fix"),
                ],
            )

    # ------------------------------------------------------------------
    # Putting it back
    # ------------------------------------------------------------------

    def _restore_command(self) -> str:
        previous = self._previous
        if previous is None:
            return "git status --short"
        if previous.branch:
            return f"git switch --force-create {previous.branch} {previous.commit}"
        return f"git switch --detach {previous.commit}"

    def _restore_checkout(self) -> str | None:
        """Put the checkout and the runtime selection back; a problem, or ``None``."""
        previous = self._previous
        if previous is None:
            return None
        if previous.branch:
            where = ["--force-create", previous.branch, previous.commit]
        else:
            where = ["--detach", previous.commit]
        done = self._git("switch", *where, interruptible=False)
        if done.returncode != 0:
            # The tree was clean before the update: anything changed now is ours.
            done = self._git("switch", "--discard-changes", *where, interruptible=False)
        if self._head() != previous.commit:
            return (
                f"git could not put the checkout back on {previous.commit[:7]} "
                f"(exit status {done.returncode})."
            )
        state = self.root / SETUP_STATE
        try:
            if previous.setup_state is None:
                state.unlink(missing_ok=True)
            else:
                autodj.fsutil.atomic_write(state, previous.setup_state)
        except OSError as exc:
            return f"AutoDJ could not restore {state}: {exc}."
        return None

    def _finish(self, state: str, message: str, commands: list[str], reason: str) -> None:
        print(f"AutoDJ: {message}", file=sys.stderr)
        for command in commands:
            print(f"  {command}", file=sys.stderr)
        self._write(state, step=None, reason=reason, message=message, commands=commands)
        self._unlock()

    def _failed(self, reason: str, problem: str) -> Attempt:
        head = self._head()
        where = (
            f"The checkout is at {head[:7]}."
            if head
            else "AutoDJ cannot tell which commit the checkout is at."
        )
        backup = f" A backup from before the update is at {self._backup}." if self._backup else ""
        message = (
            f"The update failed and AutoDJ could not go back to {self._previous_label}: "
            f"{problem} {reason} {where} AutoDJ did not start again. Stop AutoDJ, run these "
            f"commands in the AutoDJ folder, then start AutoDJ.{backup}"
        )
        commands = [
            "git status --short",
            self._restore_command(),
            self._cli("setup"),
            self._cli("doctor", "--no-fix"),
        ]
        self._finish("failed", message, commands, f"{problem} {reason}")
        return Attempt(failed=True)

    def _roll_back(self, reason: str, commands: list[str]) -> Attempt:
        self._progress("rollback", f"going back to {self._previous_label}.")
        problem = self._restore_checkout()
        if problem is None and not self._stopped:
            status = self._setup(self._previous_label)
            if status != 0:
                problem = f"Setup for {self._previous_label} failed (exit status {status})."
        if self._stopped:
            return self._stop()
        if problem is not None:
            return self._failed(reason, problem)
        self._switched = False
        message = f"The update failed, so AutoDJ went back to {self._previous_label}. {reason}"
        self._finish("rolled-back", message, commands, reason)
        return Attempt()

    def _stop(self) -> Attempt:
        signum = self._stopped[0]
        problem = self._restore_checkout() if self._switched else None
        if problem is None:
            message = "AutoDJ was stopped during the update, so it did not update" + (
                f"; the checkout is back at {self._previous_label}." if self._switched else "."
            )
            commands: list[str] = []
        else:
            head = self._head()
            message = (
                f"AutoDJ was stopped during the update and could not go back to "
                f"{self._previous_label}: {problem} The checkout is at "
                f"{head[:7] if head else 'an unknown commit'}. Run these commands in the AutoDJ "
                "folder before starting AutoDJ."
            )
            commands = ["git status --short", self._restore_command(), self._cli("setup")]
        self._switched = False
        self._finish("stopped", message, commands, "stopped")
        return Attempt(stopped=signum)

    # ------------------------------------------------------------------
    # The update
    # ------------------------------------------------------------------

    def update(self) -> Attempt:
        """Update the checkout; see the module.  Never raises for a failed step."""
        self._reset()
        self._stopped.clear()
        request = read_update_state(self.state_path) or {}
        self._record = {
            "id": valid_update_id(request.get("id")),
            "requested_at": request.get("requested_at")
            if isinstance(request.get("requested_at"), (int, float))
            else None,
            "channel": None,
            "from": None,
            "to": None,
            "backup": None,
            "reason": None,
            "message": None,
            "commands": [],
        }
        with self._signals():
            try:
                return self._update(request)
            except _Stop:
                return self._stop()
            except _RollBack as exc:
                return self._roll_back(exc.reason, exc.commands)
            except _Refuse as exc:
                message = f"AutoDJ did not update: {exc.reason}"
                self._finish("refused", message, exc.commands, exc.reason)
                return Attempt()
            except Exception as exc:  # a bug must not leave a half-switched checkout
                reason = f"The update stopped on an unexpected error ({type(exc).__name__}: {exc})."
                if self._switched:
                    return self._roll_back(reason, [])
                self._finish("refused", f"AutoDJ did not update: {reason}", [], reason)
                return Attempt()

    def _update(self, request: Mapping[str, Any]) -> Attempt:
        channel = request.get("channel")
        self._probe = parse_health_probe(request.get("probe"))
        if request.get("state") != "requested" or channel not in CHANNELS or self._probe is None:
            raise _Refuse("AutoDJ could not read the update request the server wrote.")
        self._record["channel"] = channel
        self._lock()
        self._progress("check", f"checking the {channel} channel.")
        installation = self._installation()
        if installation.kind != "git" or installation.commit is None:
            raise _Refuse("This is not a git checkout that AutoDJ can update.")
        self._previous_label = self._label(
            installation.version, installation.commit, installation.branch, installation.tag
        )
        self._record["from"] = {
            "version": installation.version,
            "commit": installation.commit,
            "label": self._previous_label,
        }
        result = self._check(installation, UpdateSettings(check=True, channel=channel))
        target = self._target(installation, result)
        self._check_stop()
        status = self._git("status", "--porcelain")
        if status.returncode != 0:
            raise _Refuse(
                f"git could not read the checkout's status (exit status {status.returncode})."
            )
        if status.stdout.strip():
            raise _Refuse(
                "The AutoDJ folder has changes git does not know about, or files it does not "
                "track, so AutoDJ did not update. Save them on a branch or move them out of "
                "the folder, then try again. These commands list them and put them aside.",
                [
                    "git status --short",
                    'git stash push --include-untracked -m "Before updating AutoDJ"',
                ],
            )
        setup_state = self.root / SETUP_STATE
        self._previous = _Previous(
            installation.commit,
            installation.branch,
            setup_state.read_bytes() if setup_state.is_file() else None,
        )
        self._progress("backup", "making a backup.")
        self._backup_now(installation)
        self._progress("fetch", f"fetching {target}.")
        self._fetch(target)
        commit = self._verify(installation, target)
        self._check_stop()
        self._progress("switch", f"switching to {target}.")
        self._switched = True
        self._switch(target, commit)
        self._check_stop()
        version = self._version()
        branch = None if TAG_PATTERN.fullmatch(target) else DEVELOPMENT_BRANCH
        self._target_label = self._label(
            version, commit, branch, target if branch is None else None
        )
        self._record["to"] = {"version": version, "commit": commit, "label": self._target_label}
        self._progress("setup", f"running setup for {self._target_label}.")
        status_code = self._setup(self._target_label)
        self._check_stop()
        if status_code != 0:
            raise _RollBack(f"Setup for {self._target_label} failed (exit status {status_code}).")
        self._progress("doctor", "asking Doctor whether the library needs a migration.")
        self._doctor(target)
        self._progress("start", f"starting {self._target_label}.")
        return Attempt(switched=True)

    # ------------------------------------------------------------------
    # The new server
    # ------------------------------------------------------------------

    def watch(self, process: subprocess.Popen[Any], stopping: Callable[[], bool]) -> None:
        """Wait for the new server to be healthy; stop it if it is not in time.

        Healthy: still running, and ``/healthz`` answers 200 ``"ok"`` within
        HEALTH_TIMEOUT_SECONDS.  Afterwards :attr:`started_ok` is True, or
        :meth:`failed_start` rolls back.
        """
        deadline = self._monotonic() + HEALTH_TIMEOUT_SECONDS
        probe = self._probe
        while not stopping():
            code = process.poll()
            if code is not None:
                self._start_failure = (
                    f"The new version stopped with exit status {code} before it was ready."
                )
                return
            if probe is not None and self._probe_fn(probe):
                self.started_ok = True
                message = f"Updated to {self._target_label}."
                self._switched = False
                self._finish("updated", message, [], "updated")
                return
            if self._monotonic() >= deadline:
                self._start_failure = (
                    f"The new version did not answer within {HEALTH_TIMEOUT_SECONDS:.0f} seconds."
                )
                _stop_process(process)
                return
            self._sleep(HEALTH_POLL_SECONDS)

    @property
    def pending(self) -> bool:
        """Whether the checkout runs a new version that has not been confirmed yet."""
        return self._switched

    def failed_start(self) -> Attempt:
        """The new server was not healthy: put the old version back."""
        with self._signals():
            return self._roll_back(self._start_failure or "The new version did not start.", [])

    def stopped_while_starting(self, signum: int) -> Attempt:
        """A stop signal came while the new server started: put the checkout back."""
        with self._signals():
            self._stopped[:] = [signum]
            return self._stop()

    def recover_interrupted(self) -> None:
        """Record an update a killed launcher left unfinished, when no launcher holds it."""
        record = read_update_state(self.state_path)
        if record is None or record.get("state") in FINAL_UPDATE_STATES:
            return
        lock = self.root / LOCK
        with contextlib.suppress(OSError):
            if self._clock() - lock.stat().st_mtime <= LOCK_STALE_SECONDS:
                return
            lock.unlink()
        head = self._head()
        before = record.get("from") if isinstance(record.get("from"), dict) else {}
        old_commit = before.get("commit") if isinstance(before, dict) else None
        message = (
            f"An earlier update did not finish (it stopped during {record.get('step') or 'its start'}). "
            f"The checkout is at {head[:7] if head else 'an unknown commit'}."
        )
        commands = ["git status --short"]
        if (
            isinstance(old_commit, str)
            and head
            and old_commit != head
            and re.fullmatch(r"[0-9a-f]{40}", old_commit)
        ):
            message += f" Before the update it was at {old_commit[:7]}."
            commands += [f"git switch --detach {old_commit}", self._cli("setup")]
        self._record = dict(record)
        self._finish("failed", message, commands, "interrupted")
