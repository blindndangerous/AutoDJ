"""Background job runner for the web UI.

Wraps long-running library-maintenance commands (``index``, ``enrich``,
``prune``, ``stats``) so the web UI can drive them without dropping to a
terminal.  One concurrent job slot — running a second job while the
first is in flight returns 409 from the API.

Each job runs as a subprocess (``<python> -m autodj …``) so a crash in the
indexer can't take down the live web server, and so torch / muq /
librosa stay confined to the indexer process when the web UI is hosted
on a slim install.

Stdout + stderr are interleaved into a ring buffer of recent lines —
the web UI polls this via the standard WebSocket state push.

Example:
    >>> from autodj.jobs import get_manager
    >>> mgr = get_manager()
    >>> mgr.start("prune", ["--force"])
    >>> mgr.snapshot()
    {'name': 'prune', 'running': True, 'lines': [...], 'exit_code': None}
"""

from __future__ import annotations

import contextlib
import logging
import os
import shlex
import subprocess  # nosec B404 — used only for spawning vetted CLI subcommands
import sys
import threading
import time
from collections import deque
from typing import ClassVar

logger = logging.getLogger(__name__)


# Hard upper bound on retained log lines per job — protects the WS
# payload from growing unbounded over an overnight indexing run.
_MAX_LINES = 500


class JobManager:
    """Single-slot background job runner.

    Threadsafe.  Holds at most one running subprocess; a successful start
    transitions ``running`` to ``True``.  When the subprocess exits the
    final state (lines + exit_code) is preserved until the next ``start``.
    """

    # Allowlist of CLI subcommands the web UI is allowed to spawn.  Keeps
    # the API surface tight — no arbitrary command injection via the
    # `name` parameter.
    _ALLOWED: ClassVar[set[str]] = {
        "index",
        "enrich",
        "prune",
        "stats",
        "list-indexes",
    }

    # Subcommands that accept ``--name``.  ``list-indexes`` reports on every
    # index, so it has no single one to be given.
    _NAME_AWARE: ClassVar[set[str]] = {"index", "enrich", "prune", "stats"}

    def __init__(self) -> None:
        self._config_path: str | None = None
        self._index_dir: str | None = None
        self._index_name: str | None = None
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lines: deque[str] = deque(maxlen=_MAX_LINES)
        self._name: str | None = None
        self._args: list[str] = []
        self._exit_code: int | None = None
        self._started_at: float | None = None
        self._finished_at: float | None = None

    def configure(
        self,
        *,
        config_path: object = None,
        index_dir: object = None,
        index_name: object = None,
    ) -> None:
        """Point future jobs at the configuration the server is running on.

        Each job is a fresh interpreter, so it re-reads configuration from
        scratch and would otherwise load defaults -- which under any
        non-default config meant every job started and immediately died on
        "Index not found".

        Args:
            config_path: TOML file the server loaded, or None if it had none.
            index_dir: Directory holding the named indexes.
            index_name: Active index name, including a ``--name`` override.
        """
        self._config_path = str(config_path) if config_path else None
        self._index_dir = str(index_dir) if index_dir else None
        self._index_name = str(index_name) if index_name else None

    def _child_argv(self, name: str, args: list[str]) -> list[str]:
        """Build the child's argv, config first because Click reads it there."""
        argv = [sys.executable, "-m", "autodj"]
        if self._config_path:
            argv += ["--config", self._config_path]
        argv.append(name)
        # "default" is what the child assumes anyway; passing it would only
        # make the logged command noisier.
        if self._index_name and self._index_name != "default" and name in self._NAME_AWARE:
            argv += ["--name", self._index_name]
        return argv + args

    def _child_env(self) -> dict[str, str]:
        """Environment for the child: the server's index dir, UTF-8 output."""
        # A child printing a track name under a cp125x locale used to raise
        # inside the reader, which stopped the pump, filled the pipe and left
        # the child blocked on write forever -- holding the single job slot
        # until someone pressed Stop.
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        if self._index_dir:
            env["AUTODJ_INDEX_DIR"] = self._index_dir
        return env

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def _validate_request(self, name: str, args: list[str] | None) -> bool:
        """Reject disallowed subcommand names and shell-metachar arguments."""
        if name not in self._ALLOWED:
            logger.warning("Refused job: subcommand %r not allowed", name)
            return False
        forbidden = {"&", "|", ";", "`", "\n", "\r"}
        for a in args or []:
            if any(c in a for c in forbidden):
                logger.warning("Refused job: forbidden char in arg %r", a)
                return False
        return True

    def _spawn_proc(self, name: str, args: list[str]) -> bool:
        """Start the subprocess; populate `_proc` or return False on failure."""
        cmd = self._child_argv(name, args)
        self._lines.append(f"[autodj-jobs] $ {' '.join(shlex.quote(c) for c in cmd)}")
        try:
            # nosec B603 -- `cmd` is built from a hard-coded subcommand
            # allowlist + arg tokens already screened for shell metacharacters.
            # shell=False so no shell parsing happens regardless.
            child_env = self._child_env()
            self._proc = subprocess.Popen(  # nosec B603
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=child_env,
            )
        except (OSError, FileNotFoundError) as exc:
            self._lines.append(f"[autodj-jobs] failed to spawn: {exc}")
            self._exit_code = -1
            self._finished_at = time.time()
            self._proc = None
            return False
        return True

    def start(self, name: str, args: list[str] | None = None) -> bool:
        """Spawn ``autodj <name> [args]`` as a subprocess."""
        if not self._validate_request(name, args):
            return False
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return False
            self._lines.clear()
            self._name = name
            self._args = list(args or [])
            self._exit_code = None
            self._started_at = time.time()
            self._finished_at = None
            if not self._spawn_proc(name, self._args):
                return False
        self._thread = threading.Thread(
            target=self._read_loop,
            name=f"autodj-job-{name}",
            daemon=True,
        )
        self._thread.start()
        return True

    def _read_loop(self) -> None:
        """Pump subprocess stdout into the ring buffer until exit."""
        if self._proc is None or self._proc.stdout is None:
            return
        try:
            for line in self._proc.stdout:
                self._lines.append(line.rstrip("\n"))
        except (OSError, ValueError) as exc:
            self._lines.append(f"[autodj-jobs] read error: {exc}")
            # The pipe is no longer being drained; terminate before waiting so
            # a child blocked on write cannot hold the job slot open.
            with contextlib.suppress(OSError):
                self._proc.terminate()
        finally:
            try:
                self._exit_code = self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._exit_code = -2
            self._finished_at = time.time()
            self._lines.append(
                f"[autodj-jobs] exit {self._exit_code} (elapsed {self._elapsed():.1f}s)",
            )

    def stop(self) -> bool:
        """Terminate the running subprocess if any.  No-op when idle."""
        with self._lock:
            if not self._proc or self._proc.poll() is not None:
                return False
            with contextlib.suppress(OSError):
                self._proc.terminate()
        return True

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _elapsed(self) -> float:
        """Return seconds since the job started (or 0 when not started)."""
        if self._started_at is None:
            return 0.0
        end = self._finished_at if self._finished_at else time.time()
        return max(0.0, end - self._started_at)

    @property
    def running(self) -> bool:
        """True when a job subprocess is alive."""
        return self._proc is not None and self._proc.poll() is None

    def snapshot(self) -> dict:
        """Return a JSON-serialisable snapshot of the current job state."""
        return {
            "name": self._name,
            "args": list(self._args),
            "running": self.running,
            "exit_code": self._exit_code,
            "lines": list(self._lines),
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "elapsed_seconds": round(self._elapsed(), 1),
        }


# Process-wide singleton — the web server attaches its bridge to this
# instance on startup.
_MANAGER: JobManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_manager() -> JobManager:
    """Return the process-wide :class:`JobManager` singleton."""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = JobManager()
        return _MANAGER
