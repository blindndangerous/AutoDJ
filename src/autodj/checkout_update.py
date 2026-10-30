"""Update a source checkout between two runs of ``serve``: the launcher's half of Update.

Settings, Updates' Update button makes the server write ``request.json``
(:mod:`autodj.update_contract`) into the launcher's update folder,
``.uv/update`` in the checkout, and exit with
:data:`autodj.updates.UPDATE_EXIT_CODE`.  While no server runs, the
launcher's supervise loop (:func:`autodj.launcher.run_supervised`) asks a
:class:`CheckoutUpdater` to follow the upgrade checklist:

1. ``check``: check again itself, for the channel the server follows, and
   refuse anything but a newer version on that channel, or a checkout with
   any change (``git status --porcelain``);
2. ``backup``: make a backup with ``autodj backup``;
3. ``fetch``, then ``switch``: a release tag detached, or ``master``
   fast-forwarded to ``origin/master``;
4. ``setup``: run the checkout's setup unattended (``setup --yes``), which
   synchronizes a setup-managed runtime to the new release's lock and backend
   profile, so new Python packages are installed, then verifies it; an
   environment adopted with ``--environment`` keeps its packages;
5. ``migration-check``: run Doctor read-only, and stop if the library
   needs a migration.

The loop then starts the new server (``start``) and
:meth:`CheckoutUpdater.watch` waits for it to be healthy (``health``):
still running, and answering ``GET /healthz`` with 200 and ``"ok"`` within
HEALTH_TIMEOUT_SECONDS.  Anything that fails after the switch puts the
checkout back on the commit it was on, restores the runtime selection,
runs setup for it, which synchronizes the runtime back to the old lock, and
starts the old version (``rollback``).  A stop
signal puts the checkout back and stops.  Progress goes into
``state.json`` and the outcome into ``result.json``.

Moving a stable release to development records ``go-back.json`` with that
update's backup.  Go back (action ``go-back``) switches to that release
again, runs its setup and restores that backup with ``autodj restore``,
after a backup of the current data that a rollback restores.

The request names only its id, its action, the channel the server follows
and where the server listens; the launcher validates each and works out the
target itself.  Standard library only, like the launcher.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import signal
import subprocess  # nosec B404 -- fixed git and AutoDJ argv lists, never a shell
import sys
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Imported now, before the checkout changes under this process: the update
# check imports these lazily, and the new version's copies must not mix in.
import autodj.child_jobs
import autodj.fsutil
import autodj.version
from autodj import update_contract as contract
from autodj import updates
from autodj.update_contract import GoBack, HealthProbe, Request
from autodj.updates import (
    CHECK_ENV,
    DEVELOPMENT_BRANCH,
    TAG_PATTERN,
    Installation,
    UpdateCheck,
    UpdateSettings,
    version_key,
)

#: The launcher's update folder, inside the checkout's ignored ``.uv``.
FOLDER = Path(".uv") / "update"
#: Locked while an update runs, so two launchers never update one checkout.
#: The operating system releases the lock when its launcher ends, however it ends.
LOCK = "lock"
#: The updater's own backups beyond this many, oldest first, are deleted.
#: The one kept for Go back is not counted.
KEEP_BACKUPS = 3
FETCH_TIMEOUT_SECONDS = 300.0
GIT_TIMEOUT_SECONDS = 120.0
BACKUP_TIMEOUT_SECONDS = 1800.0
RESTORE_TIMEOUT_SECONDS = 1800.0
#: Setup synchronizes a managed runtime, which may download packages: a new
#: PyTorch build for a GPU is several gigabytes.
SETUP_TIMEOUT_SECONDS = 3600.0
DOCTOR_TIMEOUT_SECONDS = 600.0
#: How long a new server has to answer ``/healthz`` before it is rolled back.
HEALTH_TIMEOUT_SECONDS = 180.0
HEALTH_POLL_SECONDS = 1.0
#: Tries, and the pause between them, to take a lock another process may be
#: looking at that very moment.
LOCK_ATTEMPTS = 5
LOCK_PAUSE_SECONDS = 0.05
#: How often a running step looks for a stop signal.
STEP_POLL_SECONDS = 0.5
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
    tag: str | None
    version: str
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
    autodj``) and environment; it is called again after setup.
    *global_options* are the options given before ``serve`` (``--config``),
    passed on to backup, restore and Doctor.  The rest are stand-ins for the
    tests.
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
        probe: Callable[[HealthProbe], bool] = contract.probe_health,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        setup_python: str = sys.executable,
        windows: bool = os.name == "nt",
    ) -> None:
        """See the class."""
        self.root = Path(root)
        self.folder = self.root / FOLDER
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
        self._stopped: list[int] = []
        self._serving: Any = None
        self._serving_path = self.folder / "serving.lock"
        self._current: tuple[subprocess.Popen[str], int | None, bool] | None = None
        self._reset()

    def _reset(self) -> None:
        self._record: dict[str, Any] = {}
        self._steps = contract.new_steps()
        self._request: Request | None = None
        self._previous: _Previous | None = None
        self._switched = False
        self._target = ""
        self._target_label = ""
        self._previous_label = ""
        self._backup: Path | None = None
        self._backup_at = 0.0
        self._restored = False
        #: Setup ran for the new version, so the runtime may have its packages.
        self._synced = False
        #: The new version has started, so it may have changed the data.
        self._started = False
        self._go_back: GoBack | None = None
        #: Where the update left the previous branch; rollback resets it only from there.
        self._branch_left: str | None = None
        self._branch_moved = False
        #: The branch the switch moves (master, from a release or a detached
        #: commit), and where it was before: ``None`` when it did not exist.
        self._moved_branch: str | None = None
        self._moved_branch_tip: str | None = None
        self._lock_handle: Any = None
        self._start_failure = ""
        #: The last update was put back, so the old version starts again.
        self._rolled_back = False

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

        The launcher waits in short slices, so a stop signal is seen within
        STEP_POLL_SECONDS on every platform (on Windows a signal handler runs
        only when the main thread wakes).  An *interruptible* step (fetch,
        backup, setup, restore, Doctor) is then ended with its whole process
        tree and answers exit status -1, never success; ``git`` commands that
        change or read the checkout are left to finish, so a stop never leaves
        one half done or misread.
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
        deadline = self._monotonic() + timeout
        ended = False
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=STEP_POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    if self._stopped and interruptible and not ended:
                        self._end_step(process, job)
                        ended = True
                    elif self._monotonic() >= deadline:
                        updates._kill_tree(process, job)
                        job = None
                        with contextlib.suppress(subprocess.TimeoutExpired):
                            process.wait(timeout=5)
                        raise subprocess.TimeoutExpired(argv, timeout) from None
        finally:
            self._current = None
            if job is not None:
                autodj.child_jobs.end_job(job)
        # A step ended for a stop did not succeed, whatever status it was killed with.
        status = -1 if ended else process.returncode
        return subprocess.CompletedProcess(argv, status, stdout or "", stderr or "")

    @staticmethod
    def _end_step(process: subprocess.Popen[str], job: int | None) -> None:
        """Ask a step's process tree to stop: SIGTERM to its group, or close its job."""
        if sys.platform == "win32":
            if job is not None:
                autodj.child_jobs.end_job(job)
            else:
                with contextlib.suppress(OSError):
                    process.kill()
        else:
            with contextlib.suppress(OSError):
                os.killpg(process.pid, signal.SIGTERM)

    def _step(self, argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        """Run one command; one that cannot start or times out answers exit status -1."""
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
        self, *args: str, timeout: float = GIT_TIMEOUT_SECONDS, interruptible: bool = False
    ) -> subprocess.CompletedProcess[str]:
        fetching = args[:1] == ("fetch",)
        ssh = updates._batch_ssh_command(self.root, runner=self._runner) if fetching else None
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

    def _tag_commit(self, tag: str) -> str | None:
        return self._git_value("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}")

    def _cli(self, *words: str) -> str:
        """A launcher command line for this computer, with the options given to ``serve``."""
        launcher = ".\\autodj.cmd" if self._windows else "./autodj"
        argv = [*self.global_options, *words]
        quoted = subprocess.list2cmdline(argv) if self._windows else shlex.join(argv)
        return f"{launcher} {quoted}".rstrip()

    # ------------------------------------------------------------------
    # The contract files and the lock
    # ------------------------------------------------------------------

    def _save(self, name: str, record: Mapping[str, Any]) -> None:
        path = self.folder / name
        try:
            contract.write(path, record)
        except OSError as exc:
            print(f"AutoDJ: could not write {path}: {exc}", file=sys.stderr)

    def _mark(self, name: str, status: str) -> None:
        for step in self._steps:
            if step["name"] == name:
                step["status"] = status

    def _running(self) -> str | None:
        return next((step["name"] for step in self._steps if step["status"] == "running"), None)

    def _begin(self, name: str, text: str) -> None:
        """Start step *name*: the one before it is done; progress goes into ``state.json``."""
        running = self._running()
        if running is not None:
            self._mark(running, "done")
        self._mark(name, "running")
        print(f"AutoDJ: update: {text}", file=sys.stderr)
        self._save(
            contract.STATE,
            {**self._record, "step": name, "steps": self._steps, "updated_at": self._clock()},
        )

    def _finish(self, outcome: str, message: str, commands: list[str], reason: str) -> None:
        """Write ``result.json`` once, with every step that did not run as skipped.

        ``request.json`` is deleted then, whatever the outcome.
        """
        self._prune_backups()
        running = self._running()
        if running is not None:
            self._mark(running, "done" if outcome in {"updated", "rolled-back"} else "failed")
        for step in self._steps:
            if step["status"] == "pending":
                step["status"] = "skipped"
        print(f"AutoDJ: {message}", file=sys.stderr)
        for command in commands:
            print(f"  {command}", file=sys.stderr)
        now = self._clock()
        self._save(
            contract.STATE,
            {**self._record, "step": None, "steps": self._steps, "updated_at": now},
        )
        self._save(
            contract.RESULT,
            {
                **self._record,
                "outcome": outcome,
                "reason": reason,
                "message": message,
                "commands": commands if outcome in contract.FAILURES else [],
                "steps": self._steps,
                "finished_at": now,
            },
        )
        # The request is answered: only this update read it, and result.json keeps its id.
        contract.remove(self.folder / contract.REQUEST)
        self._unlock()

    def _try_lock(self) -> bool:
        """Lock the checkout for this update; False when another launcher holds it.

        A few short tries: another launcher may be looking at the lock that
        very moment (:meth:`update_running`).
        """
        for attempt in range(LOCK_ATTEMPTS):
            if self._lock_handle is None:
                self._lock_handle = contract.try_lock(self.folder / LOCK)
            if self._lock_handle is not None:
                return True
            if attempt + 1 < LOCK_ATTEMPTS:
                self._sleep(LOCK_PAUSE_SECONDS)
        return False

    def update_running(self) -> bool:
        """Whether another launcher is updating this checkout right now."""
        if self._try_lock():
            self._unlock()
            return False
        return True

    def _unlock(self) -> None:
        handle, self._lock_handle = self._lock_handle, None
        contract.unlock(handle)

    def hold_serving(self, pid: int | None = None) -> None:
        """Show, for as long as this launcher runs, that it serves from this checkout.

        Another launcher's update refuses while this lock is held
        (:func:`autodj.update_contract.other_servers`).
        *pid* is this launcher's process id, by default its own.
        """
        if self._serving is not None:
            return
        owner = os.getpid() if pid is None else pid
        self._serving_path = contract.serving_path(self.folder, owner)
        # Another process may be looking at it that very moment; or, on POSIX,
        # may have deleted it as free between opening and locking.
        for attempt in range(LOCK_ATTEMPTS):
            handle = contract.try_lock(self._serving_path)
            if handle is not None and contract.holds(handle, self._serving_path):
                self._serving = handle
                break
            contract.unlock(handle)
            if attempt + 1 < LOCK_ATTEMPTS:
                self._sleep(LOCK_PAUSE_SECONDS)
        else:
            print(
                f"AutoDJ: could not lock {self._serving_path}; another launcher's update "
                "would not see that this one serves from this folder.",
                file=sys.stderr,
            )
        # Lock files nobody holds belong to launchers that have ended.
        contract.clear_dead_servers(self.folder, owner)

    def release_serving(self) -> None:
        """This launcher stops serving: let go of its lock and delete its file."""
        handle, self._serving = self._serving, None
        contract.release_serving(handle, self._serving_path)

    @property
    def locked(self) -> bool:
        """Whether this updater holds the checkout's update lock."""
        return self._lock_handle is not None

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def _note_stop(self, signum: int, _frame: object) -> None:
        # The running step sees it within STEP_POLL_SECONDS (see _spawn).
        self._stopped.append(signum)

    @contextlib.contextmanager
    def _signals(self) -> Iterator[None]:
        """Note stop signals instead of dying of them, so the checkout can be put back."""
        if sys.platform == "win32":
            names: tuple[str, ...] = ("SIGINT", "SIGBREAK")
        else:
            names = ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT")
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

    def _target_for(self, installation: Installation, result: UpdateCheck) -> str:
        target = result.target
        if result.status == "available" and target is not None:
            if TAG_PATTERN.fullmatch(target):
                mine, theirs = version_key(installation.version), version_key(target)
                # The same version number only from a commit that release includes,
                # which the switch step checks after fetching.
                if theirs < mine or (theirs == mine and installation.tag is not None):
                    raise _Refuse(
                        f"{target} is not newer than the installed {installation.version}. "
                        + DOWNGRADE
                    )
                return target
            if target == DEVELOPMENT_BRANCH:
                return target
        if result.status in {"ahead", "waiting-for-release"}:
            raise _Refuse(f"{result.message} {DOWNGRADE}")
        if result.status == "current":
            raise _Refuse("AutoDJ is already the latest version on its channel.")
        raise _Refuse(result.message or "No update is available.")

    def _clean_tree(self) -> None:
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

    def _remember(self, installation: Installation) -> None:
        setup_state = self.root / SETUP_STATE
        self._previous = _Previous(
            installation.commit or "",
            installation.branch,
            installation.tag,
            installation.version,
            setup_state.read_bytes() if setup_state.is_file() else None,
        )

    def _backup_now(self, installation: Installation) -> None:
        base, environment = self._runtime()
        folder = self.folder / contract.BACKUPS
        folder.mkdir(parents=True, exist_ok=True)
        self._backup_at = self._clock()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(self._backup_at))
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

    def _prune_backups(self) -> None:
        """Keep the KEEP_BACKUPS newest ordinary backups; Go back's is not one of them.

        Run as the update ends, after this update's backup may have become
        Go back's, so it is never counted among the ordinary ones.
        """
        folder = self.folder / contract.BACKUPS
        with contextlib.suppress(OSError):
            backups = sorted(
                folder.glob("autodj-update-*.zip"), key=lambda item: item.stat().st_mtime
            )
            for old in backups[:-KEEP_BACKUPS]:
                with contextlib.suppress(OSError):
                    old.unlink()

    def _fetch(self, target: str) -> None:
        if TAG_PATTERN.fullmatch(target):
            # Only that tag: another tag moved on the remote must not stop the update.
            done = self._git(
                "fetch",
                "--no-tags",
                "origin",
                f"refs/tags/{target}:refs/tags/{target}",
                timeout=FETCH_TIMEOUT_SECONDS,
                interruptible=True,
            )
        else:
            done = self._git(
                "fetch",
                "origin",
                DEVELOPMENT_BRANCH,
                timeout=FETCH_TIMEOUT_SECONDS,
                interruptible=True,
            )
        self._check_stop()
        if (
            done.returncode != 0
            and TAG_PATTERN.fullmatch(target)
            and ("clobber" in done.stderr or "rejected" in done.stderr)
        ):
            raise _Refuse(
                f"The release tag {target} on the origin remote is not the {target} in this "
                "checkout, so AutoDJ changed nothing. Compare the two with these commands; if "
                f"the remote's is right, delete this checkout's with git tag -d {target} and try "
                "again.",
                [f"git ls-remote origin refs/tags/{target}", f"git rev-parse {target}"],
            )
        if done.returncode != 0:
            # git's own message is not passed on: it can hold the remote's address.
            raise _Refuse(
                f"git could not fetch from the origin remote (exit status {done.returncode}), "
                "so AutoDJ changed nothing. Check the network and try again."
            )

    def _verify(self, installation: Installation, target: str) -> str:
        """The commit to switch to; refuses a target that would go back."""
        if TAG_PATTERN.fullmatch(target):
            commit = self._tag_commit(target)
            if commit is None:
                raise _Refuse(f"The release tag {target} is not in the checkout after fetching.")
            if installation.tag is None:
                # From development: only a release that includes this commit.
                ancestor = self._git("merge-base", "--is-ancestor", "HEAD", f"refs/tags/{target}")
                if ancestor.returncode != 0:
                    raise _Refuse(
                        f"Release {target} does not include this version yet. You'll move to "
                        "Stable when a release includes it."
                    )
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
        # Older setup scripts ignore this; newer ones preserve the runtime's
        # model mode without asking or authorizing new weight downloads.
        environment["AUTODJ_SETUP_UPDATE"] = str(os.getpid())
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

    def _restore(self, archive: Path) -> int:
        """Restore *archive* with the runtime selected now (the version that made it)."""
        base, environment = self._runtime()
        done = self._step(
            [*base, *self.global_options, "restore", "--force", str(archive)],
            timeout=RESTORE_TIMEOUT_SECONDS,
            env={**environment, CHECK_ENV: "0"},
            capture=False,
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
        if previous.branch and not self._branch_moved:
            return f"git switch --force-create {previous.branch} {previous.commit}"
        return f"git switch --detach {previous.commit}"

    def _restore_checkout(self) -> str | None:
        """Put the checkout and the runtime selection back; a problem, or ``None``."""
        previous = self._previous
        if previous is None:
            return None
        moved: str | None = None
        where = ["--detach", previous.commit]
        if previous.branch:
            tip = self._git_value(
                "rev-parse", "--verify", "--quiet", f"refs/heads/{previous.branch}"
            )
            if tip is not None and tip not in {previous.commit, self._branch_left}:
                # Someone committed on it from another shell meanwhile: keep their
                # work, and only detach the checkout at the old commit.
                self._branch_moved = True
                moved = (
                    f"The branch {previous.branch} moved while AutoDJ was updating (it is "
                    f"at {tip[:7]} now), so AutoDJ left that branch as it is."
                )
            else:
                where = ["--force-create", previous.branch, previous.commit]
        done = self._git("switch", *where, interruptible=False)
        if done.returncode != 0:
            # The tree was clean before the update: anything changed now is ours.
            done = self._git("switch", "--discard-changes", *where, interruptible=False)
        if self._head() != previous.commit:
            return (
                f"git could not put the checkout back on {previous.commit[:7]} "
                f"(exit status {done.returncode})."
            )
        self._restore_moved_branch(previous)
        state = self.root / SETUP_STATE
        try:
            if previous.setup_state is None:
                state.unlink(missing_ok=True)
            else:
                autodj.fsutil.atomic_write(state, previous.setup_state)
        except OSError as exc:
            return f"AutoDJ could not restore {state}: {exc}."
        return moved

    def _restore_moved_branch(self, previous: _Previous) -> None:
        """Put back the branch the switch moved when the checkout was not on it.

        Only from where the update left it: a commit made on it from another
        shell meanwhile is kept.  A branch the switch created is deleted.
        """
        branch = self._moved_branch
        if branch is None or branch == previous.branch:
            return  # not moved, or the checkout's own branch, put back above
        ref = f"refs/heads/{branch}"
        tip = self._git_value("rev-parse", "--verify", "--quiet", ref)
        if tip is None or tip == self._moved_branch_tip:
            return
        if tip != self._branch_left:
            print(
                f"AutoDJ: the branch {branch} moved while AutoDJ was updating (it is at "
                f"{tip[:7]} now), so AutoDJ left it as it is.",
                file=sys.stderr,
            )
            return
        if self._moved_branch_tip is None:
            done = self._git("branch", "--delete", "--force", branch, interruptible=False)
        else:
            done = self._git(
                "branch", "--force", branch, self._moved_branch_tip, interruptible=False
            )
        if done.returncode != 0:
            print(
                f"AutoDJ: git could not put the branch {branch} back "
                f"(exit status {done.returncode}).",
                file=sys.stderr,
            )

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
        commands = ["git status --short", self._restore_command(), self._cli("setup")]
        if self._data_changed and self._backup is not None:
            commands.append(self._cli("restore", "--force", str(self._backup)))
        commands.append(self._cli("doctor", "--no-fix"))
        self._finish("failed", message, commands, f"{problem} {reason}")
        return Attempt(failed=True)

    def _roll_back(self, reason: str, commands: list[str]) -> Attempt:
        running = self._running()
        if running is not None:
            self._mark(running, "failed")
        self._begin("rollback", f"going back to {self._previous_label}.")
        problem = self._restore_checkout()
        if problem is None and not self._stopped:
            # Synchronizes a managed runtime back to the old version's lock.
            status = self._setup(self._previous_label)
            if status != 0:
                problem = f"Setup for {self._previous_label} failed (exit status {status})."
            else:
                self._synced = False
        data_back = False
        if (
            problem is None
            and not self._stopped
            and self._data_changed
            and self._backup is not None
        ):
            # Go back replaced the data, or the new version started and may
            # have changed it (a new layout, a newer schema): put back the
            # data from before, with the old version that made that backup.
            status = self._restore(self._backup)
            if status != 0:
                problem = f"Restoring {self._backup} failed (exit status {status})."
            else:
                self._restored = self._started = False
                data_back = True
        if self._stopped:
            return self._stop()
        if problem is not None:
            return self._failed(reason, problem)
        self._switched = False
        message = f"The update failed, so AutoDJ went back to {self._previous_label}. {reason}"
        if data_back:
            message += (
                " The data is back as it was before the update; changes the new version made "
                "while it ran are gone."
            )
        self._finish("rolled-back", message, commands, reason)
        self._rolled_back = True
        return Attempt()

    @property
    def _data_changed(self) -> bool:
        """Whether the data may no longer be what the backup from before holds."""
        return self._restored or self._started

    def _stop(self) -> Attempt:
        signum = self._stopped[0]
        problem = self._restore_checkout() if self._switched else None
        restore = (
            [self._cli("restore", "--force", str(self._backup))]
            if self._data_changed and self._backup is not None
            else []
        )
        # No setup while stopping, as no restore: a service manager would kill
        # it half done.  The runtime may still have the new version's packages.
        resync = [self._cli("setup")] if self._synced else []
        packages = (
            " The runtime may still have the new version's packages: run setup before "
            "starting AutoDJ."
            if self._synced
            else ""
        )
        if problem is None and self._data_changed:
            message = (
                f"AutoDJ was stopped during the update. The checkout is back at "
                f"{self._previous_label}, but the new version had "
                + ("restored an older backup" if self._restored else "started")
                + " and may have changed the data. Before starting AutoDJ, run these commands "
                "in the AutoDJ folder: setup puts back the old version's packages, and the "
                "restore the data from before the update."
                if self._synced
                else f"AutoDJ was stopped during the update. The checkout is back at "
                f"{self._previous_label}, but the new version had "
                + ("restored an older backup" if self._restored else "started")
                + " and may have changed the data. Before starting AutoDJ, restore the backup "
                "from before the update with this command in the AutoDJ folder."
            )
            commands: list[str] = [*resync, *restore]
        elif problem is None:
            back = f"; the checkout is back at {self._previous_label}." if self._switched else "."
            message = "AutoDJ was stopped during the update, so it did not update" + back + packages
            commands = resync
        else:
            head = self._head()
            message = (
                f"AutoDJ was stopped during the update and could not go back to "
                f"{self._previous_label}: {problem} The checkout is at "
                f"{head[:7] if head else 'an unknown commit'}. Run these commands in the AutoDJ "
                "folder before starting AutoDJ."
            )
            commands = [
                "git status --short",
                self._restore_command(),
                self._cli("setup"),
                *restore,
            ]
        self._switched = False
        self._finish("stopped", message, commands, "stopped")
        return Attempt(stopped=signum)

    # ------------------------------------------------------------------
    # The update
    # ------------------------------------------------------------------

    def update(self) -> Attempt:
        """Update the checkout, or go back; see the module.  Never raises for a failed step."""
        self._reset()
        self._stopped.clear()
        request = contract.read_request(self.folder)
        self._request = request
        self._record = {
            "id": request.id if request else None,
            "action": request.action if request else "update",
            "channel": request.channel if request else None,
            "requested_at": request.requested_at if request else None,
            "from": None,
            "to": None,
            "backup": None,
        }
        with self._signals():
            if not self._try_lock():
                # Another launcher's update owns state.json: leave it alone, and
                # answer only this request, so its page hears why.
                reason = "Another update is already running from this AutoDJ folder."
                print(f"AutoDJ: {reason}", file=sys.stderr)
                if request is not None:
                    steps = [
                        {"name": name, "status": "failed" if name == "check" else "skipped"}
                        for name in contract.STEPS
                    ]
                    self._save(
                        contract.RESULT,
                        {
                            **self._record,
                            "outcome": "refused",
                            "reason": reason,
                            "message": f"AutoDJ did not update: {reason}",
                            "commands": [],
                            "steps": steps,
                            "finished_at": self._clock(),
                        },
                    )
                return Attempt()
            try:
                if request is None:
                    raise _Refuse("AutoDJ could not read the update request the server wrote.")
                others = contract.other_servers(self.folder, {os.getpid()})
                if others:
                    raise _Refuse(contract.other_servers_reason(others))
                if request.action == "go-back":
                    return self._go_back_now()
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

    def _start_from(self, installation: Installation) -> None:
        if installation.kind != "git" or installation.commit is None:
            raise _Refuse("This is not a git checkout that AutoDJ can update.")
        self._previous_label = self._label(
            installation.version, installation.commit, installation.branch, installation.tag
        )
        self._record["from"] = {
            "version": installation.version,
            "commit": installation.commit,
            "branch": installation.branch,
            "label": self._previous_label,
        }

    def _switched_to(self, target: str, commit: str, branch: str | None) -> None:
        version = self._version()
        self._target_label = self._label(
            version, commit, branch, target if TAG_PATTERN.fullmatch(target) else None
        )
        self._record["to"] = {"version": version, "commit": commit, "label": self._target_label}

    def _update(self, request: Request) -> Attempt:
        self._begin("check", f"checking the {request.channel} channel.")
        installation = self._installation()
        self._start_from(installation)
        result = self._check(installation, UpdateSettings(check=True, channel=request.channel))
        target = self._target_for(installation, result)
        self._target = target
        self._check_stop()
        self._clean_tree()
        self._remember(installation)
        self._begin("backup", "making a backup.")
        self._backup_now(installation)
        self._begin("fetch", f"fetching {target}.")
        self._fetch(target)
        commit = self._verify(installation, target)
        self._check_stop()
        self._begin("switch", f"switching to {target}.")
        self._switched = True
        # A release leaves the branch alone; master is fast-forwarded to commit.
        self._branch_left = commit if target == DEVELOPMENT_BRANCH else installation.commit
        if target == DEVELOPMENT_BRANCH:
            self._moved_branch = DEVELOPMENT_BRANCH
            self._moved_branch_tip = self._git_value(
                "rev-parse", "--verify", "--quiet", f"refs/heads/{DEVELOPMENT_BRANCH}"
            )
        self._switch(target, commit)
        self._check_stop()
        self._switched_to(target, commit, None if TAG_PATTERN.fullmatch(target) else "master")
        self._begin("setup", f"running setup for {self._target_label}.")
        self._synced = True
        status = self._setup(self._target_label)
        self._check_stop()
        if status != 0:
            raise _RollBack(f"Setup for {self._target_label} failed (exit status {status}).")
        self._begin("migration-check", "asking Doctor whether the library needs a migration.")
        self._doctor(target)
        self._begin("start", f"starting {self._target_label}.")
        self._started = True
        return Attempt(switched=True)

    def _go_back_now(self) -> Attempt:
        self._begin("check", "checking the backup to go back to.")
        record = contract.read_go_back(self.folder)
        if record is None:
            raise _Refuse("There is no backup from a release to go back to.")
        problem = contract.go_back_problem(record)
        if problem is not None:
            raise _Refuse(problem)
        self._go_back = record
        installation = self._installation()
        self._start_from(installation)
        if installation.tag is not None:
            raise _Refuse(f"AutoDJ already runs the release {installation.tag}.")
        moved = updates.go_back_moved(
            installation, record.commit, record.version, runner=self._runner
        )
        if moved is not None:
            raise _Refuse(moved)
        if self._tag_commit(record.tag) != record.commit:
            raise _Refuse(
                f"The release tag {record.tag} no longer points at the commit AutoDJ moved "
                "from, so it did not go back."
            )
        self._clean_tree()
        self._remember(installation)
        self._target = record.tag
        self._begin("backup", "making a backup of the data as it is now.")
        self._backup_now(installation)
        self._begin("switch", f"switching to {record.tag}.")
        self._switched = True
        self._branch_left = installation.commit
        self._switch(record.tag, record.commit)
        self._check_stop()
        self._switched_to(record.tag, record.commit, None)
        self._begin("setup", f"running setup for {self._target_label}.")
        self._synced = True
        status = self._setup(self._target_label)
        self._check_stop()
        if status != 0:
            raise _RollBack(f"Setup for {self._target_label} failed (exit status {status}).")
        self._begin("restore", f"restoring the backup made on {record.date}.")
        self._restored = True
        status = self._restore(record.backup)
        self._check_stop()
        if status != 0:
            raise _RollBack(
                f"Restoring the backup made on {record.date} failed (exit status {status})."
            )
        self._begin("migration-check", "asking Doctor whether the library needs a migration.")
        self._doctor(record.tag)
        self._begin("start", f"starting {self._target_label}.")
        self._started = True
        return Attempt(switched=True)

    # ------------------------------------------------------------------
    # Go back's record
    # ------------------------------------------------------------------

    def _release_go_back(self) -> None:
        """Forget Go back; its backup becomes an ordinary one, deleted in its turn."""
        record = contract.read_go_back(self.folder)
        contract.remove(self.folder / contract.GO_BACK)
        if record is not None and record.backup.is_file():
            ordinary = record.backup.with_name(
                record.backup.name.replace("autodj-go-back-", "autodj-update-", 1)
            )
            with contextlib.suppress(OSError):
                record.backup.replace(ordinary)

    def _keep_for_go_back(self) -> None:
        """Keep this update's backup for Go back to the release it moved from."""
        previous, backup = self._previous, self._backup
        if previous is None or previous.tag is None or backup is None or not backup.is_file():
            return
        self._release_go_back()
        kept = backup.with_name(backup.name.replace("autodj-update-", "autodj-go-back-", 1))
        try:
            backup.replace(kept)
        except OSError as exc:
            print(f"AutoDJ: could not keep {backup} for Go back: {exc}", file=sys.stderr)
            return
        self._record["backup"] = str(kept)
        record = GoBack(previous.tag, previous.version, previous.commit, kept, self._backup_at)
        self._save(contract.GO_BACK, record.to_dict())

    def _succeeded(self) -> None:
        action = self._record.get("action")
        if action == "go-back":
            date = self._go_back.date if self._go_back is not None else "the backup's day"
            self._release_go_back()
            message = (
                f"Went back to {self._target_label}. Settings, play history and library "
                f"changes made since {date} were replaced by the backup from that day."
            )
        else:
            if self._target == DEVELOPMENT_BRANCH:
                self._keep_for_go_back()
            elif TAG_PATTERN.fullmatch(self._target):
                # On a release again: going back to an older one would be a downgrade.
                self._release_go_back()
            message = f"Updated to {self._target_label}."
        self._switched = False
        self._restored = self._started = False
        self._finish("updated", message, [], "updated")

    # ------------------------------------------------------------------
    # The new server
    # ------------------------------------------------------------------

    def watch(self, process: subprocess.Popen[Any], stopping: Callable[[], bool]) -> None:
        """Wait for the new server to be healthy; stop it if it is not in time.

        Healthy: still running, and ``/healthz`` answers 200 ``"ok"`` within
        HEALTH_TIMEOUT_SECONDS.  Afterwards :attr:`pending` is False when it
        was healthy; otherwise :meth:`failed_start` rolls back.
        """
        self._begin("health", "waiting for the new version to answer.")
        deadline = self._monotonic() + HEALTH_TIMEOUT_SECONDS
        probe = self._request.health if self._request is not None else None
        while not stopping():
            code = process.poll()
            if code is not None:
                self._start_failure = (
                    f"The new version stopped with exit status {code} before it was ready."
                )
                return
            if probe is not None and self._probe_fn(probe):
                self._succeeded()
                return
            if self._monotonic() >= deadline:
                self._start_failure = (
                    f"The new version did not answer within {HEALTH_TIMEOUT_SECONDS:.0f} seconds."
                )
                _stop_process(process)
                return
            self._sleep(HEALTH_POLL_SECONDS)

    @property
    def rolled_back(self) -> bool:
        """Whether the last update was put back: the next start is the old version's."""
        return self._rolled_back

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
        state = contract.read(self.folder / contract.STATE)
        if state is None or state.get("step") is None:
            return
        result = contract.read_result(self.folder)
        if result is not None and result.get("id") == state.get("id"):
            return
        if not self._try_lock():
            return  # another launcher is still updating this checkout
        head = self._head()
        before = state.get("from") if isinstance(state.get("from"), dict) else {}
        old_commit = before.get("commit") if isinstance(before, dict) else None
        message = (
            f"An earlier update did not finish (it stopped during {state.get('step')}). "
            f"The checkout is at {head[:7] if head else 'an unknown commit'}."
        )
        commands = ["git status --short"]
        old_branch = before.get("branch") if isinstance(before, dict) else None
        if (
            isinstance(old_commit, str)
            and head
            and old_commit != head
            and re.fullmatch(r"[0-9a-f]{40}", old_commit)
        ):
            if isinstance(old_branch, str) and re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", old_branch):
                message += (
                    f" Before the update it was at {old_commit[:7]} on {old_branch}. First list "
                    f"the commits on {old_branch} since then; if there are any, keep them and use "
                    f"git switch --detach {old_commit} instead of the switch below."
                )
                commands += [
                    f"git log --oneline {old_commit}..{old_branch}",
                    f"git switch --force-create {old_branch} {old_commit}",
                ]
            else:
                message += f" Before the update it was at {old_commit[:7]}."
                commands.append(f"git switch --detach {old_commit}")
            commands.append(self._cli("setup"))
        self._record = {
            key: state.get(key) for key in ("id", "action", "channel", "from", "to", "backup")
        }
        if self._record.get("action") not in contract.ACTIONS:
            self._record["action"] = "update"
        self._steps = contract.known_steps(state.get("steps")) or self._steps
        self._finish("failed", message, commands, "interrupted")
