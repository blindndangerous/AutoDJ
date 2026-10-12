"""``autodj worker``: analyse songs for an AutoDJ server on another machine.

The worker never reads the server's music folder.  It claims a batch of
songs (``POST /api/worker/claim``), downloads each one's audio, analyses it
with :func:`autodj.indexer.analyse_file`, the code ``autodj index`` runs,
and posts the vector and measured fields back.  The server checks the
result was made with its own model and analysis version and that the song
has not changed, then stores it in its live catalog.

Network trouble is retried with a growing pause, so a worker left running
rides out a server restart.  Ctrl+C finishes the song in hand, gives the
rest of the batch back and exits; a second Ctrl+C quits at once.  A heartbeat
every half minute keeps the claims alive and tells the server the worker is
there, which pauses the server's own CPU analysis.

``--before-command`` and ``--after-command`` run a program (no shell)
before the model is loaded and after the worker stops, for example to stop
and restart another program that shares the GPU.
"""

from __future__ import annotations

import base64
import contextlib
import gc
import ipaddress
import json
import logging
import os
import re
import shlex
import ssl
import subprocess  # nosec B404 -- runs only the operator's own --before/--after commands
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Protocol
from urllib.parse import urlencode, urlsplit

import numpy as np

from autodj.indexer import TrackAnalysis, file_fingerprint

logger = logging.getLogger(__name__)

#: The longest pause between retries when the server cannot be reached.
MAX_BACKOFF_SECONDS = 300.0
#: How long an empty queue waits before asking again.
IDLE_POLL_SECONDS = 30.0
#: How long a --before-command or --after-command may run.
HOOK_TIMEOUT_SECONDS = 120.0
_CHUNK = 1 << 16
_MAX_REPLY_BYTES = 1 << 20
_SUFFIX = re.compile(r"\.[a-z0-9]{1,8}\Z")


class WorkerError(RuntimeError):
    """A problem retrying will not fix: a refused token, a model mismatch."""


class NetworkError(RuntimeError):
    """The server could not be reached, or the connection broke."""


class ApiError(RuntimeError):
    """The server answered with an error status."""

    def __init__(self, status: int, detail: str, retry_after: float | None = None) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail
        self.retry_after = retry_after

    @property
    def transient(self) -> bool:
        """Whether asking again later may work: rate limits and server errors."""
        return self.status == 429 or self.status >= 500


class Transport(Protocol):
    """Sends one request to the server and returns ``(status, headers, body)``.

    With *sink*, a successful reply's body is written there instead.
    """

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        sink: BinaryIO | None = None,
    ) -> tuple[int, dict[str, str], bytes]: ...


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would send the token on to the new host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class HttpTransport:
    """:class:`Transport` over HTTPS (or HTTP to this machine) with the standard library.

    Raises:
        WorkerError: The address is not ``https://``, or ``http://`` to a
            loopback host: a token sent in the clear could be read on the way.
    """

    def __init__(
        self, server: str, token: str, *, ca_cert: Path | None = None, timeout: float = 60.0
    ) -> None:
        parts = urlsplit(server.rstrip("/"))
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise WorkerError(f"--server must be an https:// address, not {server!r}")
        if parts.scheme == "http" and not _is_loopback_host(parts.hostname):
            raise WorkerError(
                "The worker sends its token only over HTTPS (or plain HTTP to this "
                f"machine).  Use https:// for {parts.hostname}."
            )
        self._base = f"{parts.scheme}://{parts.netloc}{parts.path}"
        self._token = token
        self._timeout = timeout
        handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
        if parts.scheme == "https":
            context = ssl.create_default_context(cafile=str(ca_cert) if ca_cert else None)
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        sink: BinaryIO | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        data = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": "autodj-worker",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(  # nosec B310 -- scheme checked in __init__
            self._base + path, data=data, method=method, headers=headers
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as reply:  # nosec B310
                if sink is not None:
                    while chunk := reply.read(_CHUNK):
                        sink.write(chunk)
                    return reply.status, dict(reply.headers), b""
                return reply.status, dict(reply.headers), reply.read(_MAX_REPLY_BYTES)
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, dict(exc.headers or {}), exc.read(_MAX_REPLY_BYTES)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise NetworkError(str(getattr(exc, "reason", exc))) from exc


class _CappedFile:
    """A file that refuses to grow past *limit* bytes."""

    def __init__(self, handle: BinaryIO, limit: int) -> None:
        self._handle = handle
        self._limit = limit
        self.written = 0

    def write(self, data: bytes) -> int:
        self.written += len(data)
        if self.written > self._limit:
            raise ValueError("the server sent more audio than the song's size")
        return self._handle.write(data)


class ServerApi:
    """The worker routes of one AutoDJ server."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def _call(
        self, method: str, path: str, body: dict[str, Any] | None = None, sink: Any = None
    ) -> dict[str, Any]:
        status, headers, raw = self._transport.request(method, path, body, sink)
        if 200 <= status < 300:
            return {} if sink is not None else json.loads(raw or b"{}")
        detail = raw.decode("utf-8", "replace")[:300]
        with contextlib.suppress(ValueError, AttributeError):
            detail = str(json.loads(raw)["detail"])[:300]
        retry = {k.lower(): v for k, v in headers.items()}.get("retry-after")
        raise ApiError(status, detail, float(retry) if retry and retry.isdigit() else None)

    def heartbeat(self, device: str = "") -> dict[str, Any]:
        return self._call("POST", "/api/worker/heartbeat", {"device": device})

    def claim(self, batch: int, device: str = "") -> dict[str, Any]:
        return self._call("POST", "/api/worker/claim", {"batch": batch, "device": device})

    def download(self, lease: str, dest: Path, size: int) -> None:
        with dest.open("wb") as handle:
            self._call(
                "GET",
                "/api/worker/audio?" + urlencode({"lease": lease}),
                sink=_CappedFile(handle, size),
            )

    def submit(self, result: dict[str, Any]) -> None:
        self._call("POST", "/api/worker/result", result)

    def fail(self, lease: str, reason: str) -> None:
        self._call("POST", "/api/worker/failed", {"lease": lease, "reason": reason[:300]})

    def release(self, leases: list[str]) -> None:
        self._call("POST", "/api/worker/release", {"leases": leases})


def identity_mismatch(server: dict[str, object], worker: dict[str, object]) -> str:
    """Why a worker's analysis cannot be used here, and what its owner should do."""
    if server.get("model") != worker.get("model"):
        return (
            f"The server analyses with the model {server.get('model')}, the worker with "
            f"{worker.get('model')}.  Set [model] name in the worker's config.toml to the "
            "server's."
        )
    if server.get("model_commit") != worker.get("model_commit"):
        wanted = server.get("model_commit")
        advice = f'set [model] revision = "{wanted}" in its config.toml'
        return (  # nosec B608 -- a message for the operator, not SQL
            f"The worker's copy of {server.get('model')} ({worker.get('model_commit')}) is not "
            f"the server's ({wanted}).  Update the model on the worker: {advice}, which "
            "downloads exactly the server's weights, and start it again."
        )
    return (
        f"The server uses analysis version {server.get('analysis_version')} with "
        f"{server.get('dimension')} values, the worker {worker.get('analysis_version')} with "
        f"{worker.get('dimension')}.  Update AutoDJ on both machines to the same release."
    )


def encode_vector(vector: np.ndarray) -> str:
    """A vector as the base64 little-endian float32 text the server reads."""
    return base64.b64encode(np.asarray(vector, dtype="<f4").tobytes()).decode("ascii")


def run_hook(command: str | None, label: str, timeout: float = HOOK_TIMEOUT_SECONDS) -> bool:
    """Run an operator's command without a shell; log what happened.

    The command is split like a shell would (``shlex``), but no shell runs
    it, so pipes and variables mean nothing.  A failure is logged, never
    raised: the worker goes on either way.

    Returns:
        Whether it ran and exited 0.  ``True`` when there is no command.
    """
    if not command:
        return True
    try:
        argv = shlex.split(command, posix=os.name != "nt")
        if os.name == "nt":
            argv = [arg[1:-1] if len(arg) > 1 and arg[0] == arg[-1] == '"' else arg for arg in argv]
    except ValueError as exc:
        logger.warning("%s not run: %s", label, exc)
        return False
    if not argv:
        return True
    logger.info("Running %s: %s", label, " ".join(argv))
    try:
        done = subprocess.run(  # nosec B603 -- the operator's own command, no shell
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        logger.warning("%s did not finish within %d seconds.", label, timeout)
        return False
    except OSError as exc:
        logger.warning("%s could not start: %s", label, exc)
        return False
    output = (done.stdout + done.stderr).strip()
    if output:
        logger.info("%s said: %s", label, output[-500:])
    if done.returncode != 0:
        logger.warning("%s exited with code %d.", label, done.returncode)
        return False
    logger.info("%s finished.", label)
    return True


@dataclass
class WorkerStats:
    """What one run of the worker did."""

    done: int = 0
    failed: int = 0
    stale: int = 0
    started: float = field(default_factory=time.monotonic)


class Worker:
    """Claim, download, analyse and submit until stopped (or, with *once*, the queue empties).

    Args:
        api: The server.
        analyse: Analyses one audio file; :func:`autodj.indexer.analyse_file`
            with the loaded model.
        identity: This worker's :func:`~autodj.indexer.analysis_identity`.
            Work stops if the server's differs.
        batch: Songs per claim.
        once: Stop when the server has nothing left to hand out.
        device: Shown in the server's Settings, such as the GPU's name.
        stop: Set to stop after the current song.
    """

    def __init__(
        self,
        api: ServerApi,
        analyse: Callable[[Path], TrackAnalysis],
        identity: dict[str, object],
        *,
        batch: int = 4,
        once: bool = False,
        device: str = "",
        stop: threading.Event | None = None,
        idle_seconds: float = IDLE_POLL_SECONDS,
    ) -> None:
        self.api = api
        self.analyse = analyse
        self.identity = identity
        self.batch = batch
        self.once = once
        self.device = device
        self.stop = stop or threading.Event()
        self.idle_seconds = idle_seconds
        self.stats = WorkerStats()
        self._backoff = 0.0
        self._heartbeat_every = 30.0
        self.server_identity: dict[str, object] | None = None

    # -- helpers --------------------------------------------------------

    def _wait(self, seconds: float) -> None:
        self.stop.wait(seconds)

    def _retry_pause(self, exc: Exception) -> float:
        """Log *exc* and return how long to wait before trying again."""
        if isinstance(exc, ApiError) and exc.retry_after:
            return exc.retry_after
        self._backoff = min(MAX_BACKOFF_SECONDS, max(2.0, self._backoff * 2))
        logger.warning("Server unavailable (%s); trying again in %.0f s.", exc, self._backoff)
        return self._backoff

    def _call(self, action: Callable[[], Any], *, give_up_on_stop: bool = True) -> Any:
        """Run *action*, retrying network trouble and server errors.

        Raises:
            WorkerError: The token was refused.
            ApiError: Any other refusal (the caller decides), or a transient
                one once a stop was asked for.
            NetworkError: The server is unreachable and a stop was asked for.
        """
        while True:
            try:
                result = action()
            except ApiError as exc:
                if exc.status in {401, 403}:
                    raise WorkerError(
                        f"The server refused this worker ({exc.detail}).  Check the token "
                        "in Settings, Analysis workers: it may be mistyped or revoked."
                    ) from exc
                if not exc.transient or (give_up_on_stop and self.stop.is_set()):
                    raise
                self._wait(self._retry_pause(exc))
                continue
            except NetworkError as exc:
                if give_up_on_stop and self.stop.is_set():
                    raise
                self._wait(self._retry_pause(exc))
                continue
            self._backoff = 0.0
            return result

    def check_identity(
        self, server: dict[str, object], fields: tuple[str, ...] | None = None
    ) -> None:
        """Refuse to work for a server that uses other weights or another analysis version.

        *fields* limits the check, for before this worker knows its own weights.
        """
        names = tuple(server) if fields is None else fields
        mine = {name: self.identity.get(name) for name in names}
        if {name: server.get(name) for name in names} != mine:
            raise WorkerError(identity_mismatch(server, {**server, **mine}))

    def connect(self) -> dict[str, object]:
        """Check the token, the model name and the analysis version with one heartbeat.

        Returns the server's identity; the model's weights are compared once
        this worker has its copy of the model (:meth:`check_identity`).
        """
        try:
            reply = self._call(lambda: self.api.heartbeat(self.device))
        except (ApiError, NetworkError) as exc:
            raise WorkerError(f"Could not start work with the server: {exc}") from exc
        self.server_identity = dict(reply.get("identity", {}))
        self.check_identity(self.server_identity, ("model", "analysis_version", "dimension"))
        self._heartbeat_every = float(reply.get("heartbeat_seconds", 30))
        return self.server_identity

    # -- heartbeat ------------------------------------------------------

    def _beat(self, finished: threading.Event) -> None:
        while not finished.wait(self._heartbeat_every):
            try:
                self.api.heartbeat(self.device)
            except (ApiError, NetworkError) as exc:
                logger.debug("Heartbeat failed: %s", exc)

    # -- work ----------------------------------------------------------

    def _process(self, song: dict[str, Any], folder: Path, number: str) -> None:
        lease = str(song["lease"])
        suffix = str(song.get("suffix", "")).lower()
        if not _SUFFIX.fullmatch(suffix):
            suffix = ".audio"
        name = " - ".join(part for part in (song.get("artist"), song.get("title")) if part)
        name = name or "untitled"
        path = folder / f"song{suffix}"
        size, fingerprint = int(song["size"]), str(song["fingerprint"])
        started = time.monotonic()
        try:
            try:
                self._call(lambda: self.api.download(lease, path, size), give_up_on_stop=False)
            except ApiError as exc:
                logger.warning("%s %s skipped: %s", number, name, exc.detail)
                self.stats.stale += 1
                return
            except ValueError as exc:
                self._report_failure(lease, name, number, str(exc))
                return
            if file_fingerprint(path) != (size, fingerprint):
                self._report_failure(lease, name, number, "the download did not match the song")
                return
            try:
                analysis = self.analyse(path)
            except Exception as exc:
                self._report_failure(lease, name, number, str(exc) or type(exc).__name__)
                return
        finally:
            path.unlink(missing_ok=True)
        result = {
            "lease": lease,
            "model": self.identity["model"],
            "model_commit": self.identity["model_commit"],
            "analysis_version": self.identity["analysis_version"],
            "dimension": self.identity["dimension"],
            "size": size,
            "fingerprint": fingerprint,
            "vector": encode_vector(analysis.vector),
            "energy": analysis.energy,
            "key": analysis.key,
            "mode": analysis.mode,
            "bpm": analysis.bpm,
            "tempo_confidence": analysis.tempo_confidence,
        }
        try:
            self._call(lambda: self.api.submit(result), give_up_on_stop=False)
        except ApiError as exc:
            logger.warning("%s %s not stored: %s", number, name, exc.detail)
            self.stats.stale += 1
            return
        self.stats.done += 1
        logger.info("%s %s analysed in %.1f s.", number, name, time.monotonic() - started)

    def _report_failure(self, lease: str, name: str, number: str, reason: str) -> None:
        logger.warning("%s %s could not be analysed: %s", number, name, reason)
        self.stats.failed += 1
        with contextlib.suppress(ApiError, NetworkError):
            self._call(lambda: self.api.fail(lease, reason), give_up_on_stop=False)

    def _release(self, leases: list[str]) -> None:
        if not leases:
            return
        try:
            self.api.release(leases)
            logger.info("Gave %d unanalysed songs back to the server.", len(leases))
        except (ApiError, NetworkError) as exc:
            logger.warning(
                "Could not give %d songs back (%s); the server takes them back by itself "
                "in a few minutes.",
                len(leases),
                exc,
            )

    def run(self) -> WorkerStats:
        """Work until stopped, or until the queue is empty with *once*.

        Raises:
            WorkerError: The token was refused or the model does not match.
        """
        if self.server_identity is None:
            self.connect()
        self.check_identity(self.server_identity or {})
        finished = threading.Event()
        beat = threading.Thread(target=self._beat, args=(finished,), daemon=True)
        beat.start()
        held: list[str] = []
        try:
            with tempfile.TemporaryDirectory(prefix="autodj-worker-") as folder:
                while not self.stop.is_set():
                    try:
                        claim = self._call(lambda: self.api.claim(self.batch, self.device))
                    except (ApiError, NetworkError) as exc:
                        if self.stop.is_set():
                            break
                        raise WorkerError(f"The server refused to hand out songs: {exc}") from exc
                    self.check_identity(claim.get("identity", {}))
                    songs = list(claim.get("songs", []))
                    if not songs:
                        if self.once:
                            logger.info("Nothing left to analyse.")
                            break
                        logger.debug("Nothing to analyse; asking again later.")
                        self._wait(self.idle_seconds)
                        continue
                    held = [str(song["lease"]) for song in songs]
                    waiting = int(claim.get("waiting", 0))
                    for position, song in enumerate(songs, 1):
                        if self.stop.is_set():
                            break
                        held.remove(str(song["lease"]))
                        self._process(
                            song, Path(folder), f"[{position}/{len(songs)}, {waiting} waiting]"
                        )
                    self._release(held)
                    held = []
        finally:
            finished.set()
            if held:
                self._release(held)
        elapsed = time.monotonic() - self.stats.started
        logger.info(
            "Worker stopped: %d analysed, %d failed, %d skipped in %.0f minutes.",
            self.stats.done,
            self.stats.failed,
            self.stats.stale,
            elapsed / 60,
        )
        return self.stats


def choose_device(requested: str) -> str:
    """``"cuda"`` or ``"cpu"`` for ``--device`` *requested* (``auto``, ``cuda`` or ``cpu``).

    Raises:
        WorkerError: ``cuda`` was asked for but PyTorch sees no GPU.
    """
    import torch

    available = torch.cuda.is_available()
    if requested == "cpu":
        return "cpu"
    if requested == "cuda" and not available:
        raise WorkerError(
            "--device cuda was given, but PyTorch sees no CUDA or ROCm GPU "
            "(torch.cuda.is_available() is False).  Install the CUDA build of torch, "
            "or use --device cpu."
        )
    return "cuda" if available else "cpu"


def release_gpu(wrapper: Any) -> None:
    """Move the model off the GPU and hand its memory back, so another program can use it."""
    import torch

    model = getattr(wrapper, "model", None)
    if getattr(wrapper, "device", "cpu") != "cpu" and model is not None:
        model.to("cpu")
        wrapper.device = "cpu"
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


__all__ = [
    "ApiError",
    "HttpTransport",
    "NetworkError",
    "ServerApi",
    "Worker",
    "WorkerError",
    "choose_device",
    "encode_vector",
    "release_gpu",
    "run_hook",
]
