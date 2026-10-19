"""One server-owned render slot; immutable station-owned review results.

Callers supply trusted station paths, never paths from browser requests. Renderers
write ``review.wav`` and ``review.mp3`` in their staging directory, return JSON
metadata, and must poll the cancellation predicate during expensive work. It also
becomes true at the wall-time limit. A worker that ignores it retains the slot;
shutdown reports that ownership instead of starting a duplicate worker.

Results have no automatic expiry. Admission refuses a full station (20 results or
512 MiB by default); publication checks the byte cap again including new output.
Only explicit deletion removes a completed snapshot. No live station objects or
broadcast outputs enter this service.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from autodj.fsutil import atomic_write, fsync_directory

logger = logging.getLogger(__name__)
_ID = re.compile(r"[0-9a-f]{32}\Z")
type Renderer = Callable[[Path, Callable[[], bool]], dict[str, Any]]


class ReviewError(ValueError):
    """An actionable, explicitly public error without paths or provider secrets."""

    reports_field = True

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


def _identity(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid review ID")
    return value


def _bytes(path: Path) -> int:
    return sum(
        item.stat().st_size for item in path.rglob("*") if not item.is_symlink() and item.is_file()
    )


@dataclass
class _Run:
    root: Path
    identity: str
    fingerprint: str
    started_at: float
    deadline: float
    max_stage_bytes: int
    max_scratch_bytes: int
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    storage_exceeded: bool = False
    next_storage_check: float = 0

    def cancelled(self, *, force_storage: bool = False) -> bool:
        now = time.monotonic()
        if self.cancel.is_set() or now >= self.deadline or self.storage_exceeded:
            return True
        if not force_storage and now < self.next_storage_check:
            return False
        # PCM allocations enforce their budget before writing. Poll external
        # capture/encoder files periodically, and force a final check at publication.
        self.next_storage_check = now + 0.25
        stage = self.root / f".staging-{self.identity}"
        artifacts = sum(
            path.stat().st_size
            for path in (stage / "review.wav", stage / "review.mp3")
            if path.is_file() and not path.is_symlink()
        )
        self.storage_exceeded = self.storage_exceeded or (
            artifacts > self.max_stage_bytes or _bytes(stage) - artifacts > self.max_scratch_bytes
        )
        return self.storage_exceeded


class ReviewJobs:
    """The server owns one instance; station artifacts are supplied by StationPaths."""

    def __init__(
        self,
        *,
        max_bytes: int = 512 * 1024 * 1024,
        max_results: int = 20,
        max_seconds: float = 120,
        max_scratch_bytes: int = 2 * 1024**3,
    ) -> None:
        if (
            max_bytes < 1
            or max_results < 1
            or max_scratch_bytes < 1
            or not math.isfinite(max_seconds)
            or max_seconds <= 0
        ):
            raise ValueError("Review limits must be positive and finite")
        self.max_bytes = max_bytes
        self.max_scratch_bytes = max_scratch_bytes
        self.max_results = max_results
        self.max_seconds = max_seconds
        self._lock = threading.RLock()
        self._active: _Run | None = None
        self._closing = False
        self._recovered: set[Path] = set()
        self._latest: dict[Path, dict[str, Any]] = {}

    def _recover(self, root: Path) -> None:
        """Recover once per station; never touch another worker's active staging."""
        if root in self._recovered:
            return
        status_path = root / ".job.json"
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            _identity(status["id"])
            if status.get("state") == "running":
                if self._result_dir(root, status["id"]).is_dir():
                    status["state"] = "done"
                else:
                    status.update(state="failed", error="Review interrupted by server restart")
                atomic_write(status_path, json.dumps(status, allow_nan=False))
            self._latest[root] = status
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            pass
        for path in root.glob(".staging-*"):
            if _ID.fullmatch(path.name.removeprefix(".staging-")):
                self._remove_stage(path)
        self._recovered.add(root)

    @staticmethod
    def _result_dir(root: Path, identity: str) -> Path:
        path = root / _identity(identity)
        if path.is_symlink():
            raise FileNotFoundError("Review not found")
        return path

    @staticmethod
    def _remove_stage(path: Path) -> None:
        if path.is_symlink():
            path.unlink(missing_ok=True)
        elif path.exists():
            shutil.rmtree(path)

    def _complete(self, root: Path) -> list[dict[str, Any]]:
        results = []
        for path in root.iterdir() if root.exists() else ():
            if not _ID.fullmatch(path.name):
                continue
            try:
                results.append(self.result(root, path.name))
            except (FileNotFoundError, ValueError, OSError):
                continue
        return sorted(results, key=lambda item: item["created_at"], reverse=True)

    def _used_bytes(self, root: Path) -> int:
        return sum(
            _bytes(path)
            for path in (root.iterdir() if root.exists() else ())
            if _ID.fullmatch(path.name) and not path.is_symlink() and path.is_dir()
        )

    def submit(self, root: Path, fingerprint: str, render: Renderer) -> dict[str, Any]:
        """Start a render, refusing concurrent work or full storage without eviction."""
        root = Path(root).resolve()
        with self._lock:
            if self._closing:
                raise RuntimeError("The server is shutting down")
            if self._active is not None:
                raise RuntimeError("A review render is already running")
            self._recover(root)
            if len(self._complete(root)) >= self.max_results:
                raise ValueError("Review result limit reached; delete an old result")
            used = self._used_bytes(root) if root.exists() else 0
            if used >= self.max_bytes:
                raise ValueError("Review storage limit reached; delete an old result")
            identity = uuid.uuid4().hex
            root.mkdir(parents=True, exist_ok=True)
            stage = root / f".staging-{identity}"
            stage.mkdir()
            run = _Run(
                root,
                identity,
                fingerprint,
                time.time(),
                time.monotonic() + self.max_seconds,
                self.max_bytes - used,
                self.max_scratch_bytes,
            )
            status = {"id": identity, "state": "running", "started_at": run.started_at}
            try:
                atomic_write(root / ".job.json", json.dumps(status), mode=0o600)
                run.thread = threading.Thread(
                    target=self._work, args=(run, render), name="autodj-review", daemon=True
                )
                self._active = run
                self._latest[root] = status
                run.thread.start()
            except Exception:
                self._active = None
                self._remove_stage(stage)
                status.update(state="failed", error="Review worker could not start")
                self._latest[root] = status
                raise
            return dict(status)

    def _work(self, run: _Run, render: Renderer) -> None:
        stage = run.root / f".staging-{run.identity}"
        status: dict[str, Any] = {
            "id": run.identity,
            "state": "failed",
            "started_at": run.started_at,
        }
        try:
            metadata = render(stage, run.cancelled)
            interrupted = run.cancelled(force_storage=True)
            if run.storage_exceeded:
                raise ReviewError("Review storage limit exceeded; delete an old result")
            if interrupted:
                raise InterruptedError(
                    "Review cancelled" if run.cancel.is_set() else "Review timed out"
                )
            if not isinstance(metadata, dict):
                raise ReviewError("Review renderer must return metadata")
            allowed = {"review.wav", "review.mp3"}
            if {path.name for path in stage.iterdir()} != allowed:
                raise ReviewError("Review renderer must produce WAV and MP3 files only")
            for name in allowed:
                path = stage / name
                if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                    raise ReviewError("Review audio is missing or unsafe")
                # Windows' FlushFileBuffers requires a writable handle. Close
                # it before publishing the directory so rename remains portable.
                with path.open("r+b") as handle:
                    os.fsync(handle.fileno())
            manifest = {
                "id": run.identity,
                "created_at": time.time(),
                "fingerprint": metadata.get("_fingerprint", run.fingerprint),
                "metadata": metadata,
            }
            atomic_write(stage / "manifest.json", json.dumps(manifest, allow_nan=False), mode=0o600)
            size = sum(path.stat().st_size for path in stage.iterdir())
            with self._lock:
                if run.cancelled(force_storage=True):
                    raise InterruptedError("Review cancelled or timed out")
                if self._used_bytes(run.root) + size > self.max_bytes:
                    raise ReviewError("Review storage limit exceeded; delete an old result")
                stage.rename(run.root / run.identity)
                try:
                    fsync_directory(run.root)
                except OSError:
                    shutil.rmtree(run.root / run.identity)
                    raise
                status["state"] = "done"
        except InterruptedError:
            status.update(
                state="cancelled" if run.cancel.is_set() else "failed",
                error=(
                    "Review storage limit exceeded"
                    if run.storage_exceeded
                    else "Review cancelled"
                    if run.cancel.is_set()
                    else "Review timed out"
                    if time.monotonic() >= run.deadline
                    else "Review rendering was interrupted; retry the review"
                ),
            )
        except Exception as exc:
            logger.warning("Review rendering failed: %s", type(exc).__name__)
            status["error"] = (
                str(exc)
                if isinstance(exc, ReviewError)
                else "Review rendering failed; check available storage and source audio, then retry"
            )
            if isinstance(exc, ReviewError) and exc.field:
                status["field"] = exc.field
        finally:
            try:
                self._remove_stage(stage)
            except OSError:
                logger.exception("Could not clean review staging directory")
            with self._lock:
                status["finished_at"] = time.time()
                self._latest[run.root] = status
                try:
                    atomic_write(run.root / ".job.json", json.dumps(status), mode=0o600)
                except OSError:
                    logger.exception("Could not persist review job status")
                # Retain ownership until renderer and cleanup have actually finished.
                self._active = None

    def current(self, root: Path) -> dict[str, Any] | None:
        """The station's latest job, including a terminal state after restart."""
        root = Path(root).resolve()
        with self._lock:
            self._recover(root)
            status = self._latest.get(root)
            return dict(status) if status is not None else None

    def status(self, root: Path, identity: str) -> dict[str, Any]:
        """Read the current job or a completed result, scoped to its station."""
        _identity(identity)
        root = Path(root).resolve()
        with self._lock:
            self._recover(root)
            status = self._latest.get(root)
            if status is not None and status["id"] == identity:
                return dict(status)
            result = self.result(root, identity)
            return {"id": result["id"], "state": "done"}

    def cancel(self, root: Path, identity: str) -> dict[str, Any]:
        """Request cancellation; capacity stays owned until the renderer returns."""
        _identity(identity)
        root = Path(root).resolve()
        with self._lock:
            status = self.status(root, identity)
            if self._active is not None and (self._active.root, self._active.identity) == (
                root,
                identity,
            ):
                self._active.cancel.set()
            return status

    def results(self, root: Path, current_fingerprint: str | None = None) -> list[dict[str, Any]]:
        root = Path(root).resolve()
        with self._lock:
            self._recover(root)
            results = self._complete(root)
            for result in results:
                result["stale"] = (
                    current_fingerprint is not None and result["fingerprint"] != current_fingerprint
                )
            return results

    def result(
        self, root: Path, identity: str, current_fingerprint: str | None = None
    ) -> dict[str, Any]:
        path = self._result_dir(Path(root).resolve(), identity) / "manifest.json"
        with self._lock:
            if path.is_symlink():
                raise FileNotFoundError("Review not found")
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise FileNotFoundError("Review not found") from exc
            if (
                not isinstance(result, dict)
                or result.get("id") != identity
                or not all(key in result for key in ("created_at", "fingerprint", "metadata"))
            ):
                raise FileNotFoundError("Review not found")
            result["stale"] = (
                current_fingerprint is not None and result["fingerprint"] != current_fingerprint
            )
            return result

    def audio_path(self, root: Path, identity: str, format: str) -> Path:
        self.result(root, identity)
        if format not in {"wav", "mp3"}:
            raise ValueError("Review format must be WAV or MP3")
        path = self._result_dir(Path(root).resolve(), identity) / f"review.{format}"
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError("Review audio not found")
        return path

    def delete(self, root: Path, identity: str) -> None:
        _identity(identity)
        root = Path(root).resolve()
        with self._lock:
            self._recover(root)
            self.result(root, identity)
            shutil.rmtree(self._result_dir(root, identity))
            fsync_directory(root)
            if self._latest.get(root, {}).get("id") == identity:
                self._latest.pop(root, None)
                (root / ".job.json").unlink(missing_ok=True)

    def shutdown(self, timeout: float = 5) -> bool:
        """Cancel and wait boundedly; false means a renderer still owns the slot."""
        with self._lock:
            self._closing = True
            run = self._active
            if run is None:
                return True
            run.cancel.set()
            thread = run.thread
        if thread is not None:
            thread.join(max(0, timeout))
            return not thread.is_alive()
        return True
