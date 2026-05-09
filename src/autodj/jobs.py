"""Background job runner for the web UI.

Wraps long-running library-maintenance commands (``index``, ``enrich``,
``prune``, ``stats``) so the web UI can drive them without dropping to a
terminal.  One concurrent job slot — running a second job while the
first is in flight returns 409 from the API.

Each job runs as a subprocess (``uv run autodj …``) so a crash in the
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

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lines: deque[str] = deque(maxlen=_MAX_LINES)
        self._name: str | None = None
        self._args: list[str] = []
        self._exit_code: int | None = None
        self._started_at: float | None = None
        self._finished_at: float | None = None

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def start(self, name: str, args: list[str] | None = None) -> bool:
        """Spawn ``autodj <name> [args]`` as a subprocess.

        Args:
            name: Subcommand name.  Must be in :attr:`_ALLOWED`.
            args: Extra CLI arguments (already split into tokens).
                Must NOT contain shell metacharacters — passed positionally.

        Returns:
            ``True`` if the job was started.  ``False`` when another job
            is already running, when *name* is not allowed, or when args
            contain a forbidden character.
        """
        if name not in self._ALLOWED:
            logger.warning("Refused job: subcommand %r not allowed", name)
            return False
        # Reject anything looking like shell metacharacters in the args —
        # we use shell=False but defence-in-depth.
        for a in args or []:
            if any(c in a for c in ("&", "|", ";", "`", "\n", "\r")):
                logger.warning("Refused job: forbidden char in arg %r", a)
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

            cmd = [sys.executable, "-m", "autodj", name, *self._args]
            self._lines.append(f"[autodj-jobs] $ {' '.join(shlex.quote(c) for c in cmd)}")
            try:
                # nosec B603 — `cmd` is fully constructed from a hard-coded
                # subcommand allowlist plus arg tokens that have already
                # been screened for shell metacharacters above.  shell=False
                # so no shell parsing happens regardless.
                self._proc = subprocess.Popen(  # nosec B603 — see comment above
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except (OSError, FileNotFoundError) as exc:
                self._lines.append(f"[autodj-jobs] failed to spawn: {exc}")
                self._exit_code = -1
                self._finished_at = time.time()
                self._proc = None
                return False

        # Reader thread captures stdout lines into the ring buffer.
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
