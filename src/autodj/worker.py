"""``autodj worker``: analyse songs for an AutoDJ server on another machine.

The worker never reads the server's music folder.  It claims a batch of
songs (``POST /api/worker/claim``), downloads each one's audio, analyses it
with :func:`autodj.indexer.analyse_file`, the code ``autodj index`` runs,
and posts the vector and measured fields back.  The server checks the
result was made with its own model and analysis version and that the song
has not changed, then stores it in its live catalog.

When both ends support ``scan_decode_v1``, the same download also supplies
pending scan measurements. Songs excluded from embedding receive only those
measurements. The server supplies scan settings and owns tags and cue imports.

Network trouble is retried with a growing pause, so a worker left running
rides out a server restart.  Ctrl+C finishes the song in hand, gives the
rest of the batch back and exits; a second Ctrl+C quits at once.  A heartbeat
every half minute keeps the claims alive and tells the server the worker is
there, which pauses the server's own CPU analysis.

``--once`` stops at the first empty claim; ``--idle-exit MINUTES`` keeps
asking every half minute and stops once the queue has stayed empty that
long, for a server whose scan may still be finding new songs.

``--before-command`` and ``--after-command`` run a program (no shell)
before the model is loaded and after the worker stops, for example to stop
and restart another program that shares the GPU.

``--idle-release MINUTES`` defers those commands and model loading until
songs are claimed. After that long with an empty queue, it frees the model
and runs the after-command, but keeps polling and sending heartbeats.
New songs load the model again. If both idle timers expire, exit wins.

A server that says ``worker_manage_v1`` sends settings from its Settings,
Analysis workers page in each heartbeat and claim reply (#554): paused,
batch size and idle release, with a revision, so each change is applied
once.  A paused worker claims nothing, gives back the rest of its batch
after the song in hand, keeps its heartbeats, and frees the model at once,
running the after-command; paused counts as an empty queue for ``--once``
and ``--idle-exit``.  ``--batch`` and ``--idle-release`` given on the command line win over
the server's values.  The server never sends a command to run: the hooks
come only from this machine's command line.  The worker also tells such a
server what it analyses with, so the server can show why it refused it.

``--pair CODE`` trades a code from that page for a token
(:func:`pair_with_server`), which is kept in a file only this user can read
(:func:`worker_token_file`) and used when no token is given.
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
import socket
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
from autodj.scan_analysis import SCAN_ANALYSIS_VERSION
from autodj.secret_files import read_private_json, update_private_json

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
#: Where ``autodj worker --pair`` trades a pairing code for a token.  It takes
#: no token and no session, and the server's middleware lets only a program
#: reach it, over HTTPS or from that machine.
WORKER_PAIR_PATH = "/api/worker-pair"
_TOKEN_TEXT = re.compile(r"adj1\.[0-9a-f]{32}\.[A-Za-z0-9_-]{43}\Z")
#: The settings a server may set, and the command-line option that wins over each.
MANAGED_OPTIONS = {"batch": "--batch", "idle_release": "--idle-release"}
_MAX_BATCH = 16
_MAX_IDLE_RELEASE_MINUTES = 7 * 24 * 60


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
        # Empty only while pairing, which has no token yet.
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
            "Accept": "application/json",
            "User-Agent": "autodj-worker",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
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
        self._scan_envelope = False
        self._scan_decode = False
        self._lease_tracking = False
        self._leases: set[str] = set()
        self._leases_lock = threading.Lock()
        #: Whether the server takes ``worker_manage_v1`` fields: settings from
        #: the server, and this worker's identity for its refusal notice.
        self.manages = False
        #: The settings revision this worker has applied, reported to the server.
        self.settings_revision: int | None = None
        #: What this worker analyses with, as far as it knows; ``None`` values are left out.
        self.identity: dict[str, object] = {}

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

    def _work_request(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Negotiate support while preserving it through transient request failures."""
        if self._scan_envelope:
            body = {
                **body,
                "scan_decode_v1": self._scan_decode,
                "scan_analysis_version": SCAN_ANALYSIS_VERSION,
            }
        if self.manages:
            body = {**body, "worker_manage_v1": True}
            if self.settings_revision is not None:
                body["settings_revision"] = self.settings_revision
            known = {key: value for key, value in self.identity.items() if value is not None}
            if known:
                body["identity"] = known
        try:
            reply = self._call("POST", path, body)
        except ApiError as exc:
            optional = {
                "scan_decode_v1",
                "scan_analysis_version",
                "leases",
                "connect",
                "worker_manage_v1",
                "settings_revision",
                "identity",
            }
            if exc.status != 422 or not optional.intersection(body):
                raise
            if "scan_decode_v1" in body:
                self._scan_envelope = False
                self._scan_decode = False
            if "leases" in body or "connect" in body:
                self._lease_tracking = False
            if "worker_manage_v1" in body:
                self.manages = False
            body = {key: value for key, value in body.items() if key not in optional}
            reply = self._call("POST", path, body)
        self.manages = reply.get("worker_manage_v1") is True
        version = reply.get("scan_analysis_version")
        self._scan_envelope = (
            reply.get("scan_decode_v1") is True and isinstance(version, str) and bool(version)
        )
        self._scan_decode = self._scan_envelope and version == SCAN_ANALYSIS_VERSION
        return reply

    def heartbeat(self, device: str = "", *, connect: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {"device": device}
        if self._lease_tracking:
            with self._leases_lock:
                body["leases"] = sorted(self._leases)
            body["connect"] = connect
        reply = self._work_request("/api/worker/heartbeat", body)
        self._lease_tracking = reply.get("lease_tracking") is True
        if connect and self._lease_tracking and "connect" not in body:
            # The first legacy-shaped request discovers support before resetting claims.
            return self.heartbeat(device, connect=True)
        return reply

    def claim(self, batch: int, device: str = "") -> dict[str, Any]:
        reply = self._work_request("/api/worker/claim", {"batch": batch, "device": device})
        with self._leases_lock:
            self._leases.update(str(song["lease"]) for song in reply.get("songs", []))
        return reply

    def download(self, lease: str, dest: Path, size: int) -> None:
        with dest.open("wb") as handle:
            self._call(
                "GET",
                "/api/worker/audio?" + urlencode({"lease": lease}),
                sink=_CappedFile(handle, size),
            )

    def submit(self, result: dict[str, Any]) -> bool:
        stored = self._call("POST", "/api/worker/result", result).get("stored") is True
        with self._leases_lock:
            self._leases.discard(str(result["lease"]))
        return stored

    def fail(self, lease: str, reason: str) -> None:
        self._call("POST", "/api/worker/failed", {"lease": lease, "reason": reason[:300]})
        with self._leases_lock:
            self._leases.discard(lease)

    def release(self, leases: list[str]) -> None:
        try:
            self._call("POST", "/api/worker/release", {"leases": leases})
        finally:
            with self._leases_lock:
                self._leases.difference_update(leases)


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


def worker_token_file() -> Path:
    """This user's file of tokens from ``autodj worker --pair``, one per server.

    ``%APPDATA%\\autodj\\worker-tokens.json`` on Windows, otherwise
    ``$XDG_CONFIG_HOME/autodj/worker-tokens.json`` (``~/.config`` by default).
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base) if base else Path.home() / ".config"
    return root / "autodj" / "worker-tokens.json"


def server_key(server: str) -> str:
    """*server* as the token file names it: scheme, lower-case host, port and path."""
    parts = urlsplit(server.strip().rstrip("/"))
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port is not None else ""
    return f"{parts.scheme.lower()}://{host}{port}{parts.path}"


def save_worker_token(server: str, token: str, name: str, path: Path | None = None) -> Path:
    """Keep *token* for *server* in the token file, readable only by this user.

    Returns the file.  Other servers' tokens in it are kept.

    Raises:
        OSError: The file cannot be written.
        ValueError: The file is there but is not a token file; it is left as it is.
    """
    path = path or worker_token_file()

    def change(document: dict[str, Any]) -> bool:
        servers = document.setdefault("servers", {})
        if not isinstance(servers, dict):
            raise ValueError(f"{path.name} is not a worker token file")
        servers[server_key(server)] = {"token": token, "name": name, "paired_at": int(time.time())}
        return True

    update_private_json(path, change)
    return path


def load_worker_token(server: str, path: Path | None = None) -> str | None:
    """The token saved for *server* by ``--pair``, or ``None``.

    Raises:
        OSError: The file cannot be read, or (``PermissionError``) others may read it.
        ValueError: The file is not a token file.
    """
    document = read_private_json(path or worker_token_file(), require_private=True)
    servers = document.get("servers")
    entry = servers.get(server_key(server)) if isinstance(servers, dict) else None
    token = entry.get("token") if isinstance(entry, dict) else None
    return token if isinstance(token, str) and _TOKEN_TEXT.fullmatch(token) else None


def pair_with_server(
    server: str, code: str, *, ca_cert: Path | None = None, name: str | None = None
) -> tuple[str, str]:
    """Trade a pairing code from Settings, Analysis workers for a new token.

    The code goes only over HTTPS, or plain HTTP to this machine, like a
    token.  *name* (this computer's host name by default) names the token
    unless Settings named it.

    Returns:
        ``(token, the name the server gave it)``.

    Raises:
        WorkerError: The code was refused, or the server could not be reached.
    """
    api = ServerApi(HttpTransport(server, "", ca_cert=ca_cert))
    host = socket.gethostname() if name is None else name
    try:
        reply = api._call("POST", WORKER_PAIR_PATH, {"code": code, "name": host[:200]})
    except ApiError as exc:
        if exc.status == 404:
            raise WorkerError(
                "This server cannot pair workers; it is older than this worker.  Create a token "
                "in Settings, Analysis workers instead."
            ) from exc
        raise WorkerError(f"The server refused the pairing code ({exc.detail}).") from exc
    except NetworkError as exc:
        raise WorkerError(f"Could not reach the server to pair: {exc}") from exc
    token, given = reply.get("token"), reply.get("name")
    if not isinstance(token, str) or _TOKEN_TEXT.fullmatch(token) is None:
        raise WorkerError("The server's pairing reply held no usable token.")
    return token, given if isinstance(given, str) else host


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


def _minutes(seconds: float) -> str:
    minutes = seconds / 60
    return f"{minutes:g} minute" + ("" if minutes == 1 else "s")


def _whole(value: object, maximum: int) -> int | None:
    """*value* if it is a whole number from 1 to *maximum*, else ``None``."""
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= maximum:
        return value
    return None


def _setting_words(name: str, value: float | None) -> str:
    if name == "batch":
        return f"a batch of {value:g} songs" if value is not None else "the default batch"
    return f"an idle release of {_minutes(value)}" if value is not None else "no idle release"


def _option_words(name: str, value: float | None) -> str:
    if value is None:
        return f"no {MANAGED_OPTIONS[name]}"
    shown = value if name == "batch" else value / 60
    return f"{MANAGED_OPTIONS[name]} {shown:g}"


class Worker:
    """Claim, download, analyse and submit until stopped, or until the queue stays empty.

    Args:
        api: The server.
        analyse: Analyses one audio file; :func:`autodj.indexer.analyse_file`
            with the loaded model.
        identity: This worker's :func:`~autodj.indexer.analysis_identity`.
            Work stops if the server's differs.
        batch: Songs per claim.
        once: Stop when the server has nothing left to hand out; the same
            as ``idle_exit=0``.
        device: Shown in the server's Settings, such as the GPU's name.
        stop: Set to stop after the current song.
        idle_seconds: How long an empty queue waits before asking again.
        idle_exit: Stop once the queue has been empty for this many seconds.
            ``None`` (and not *once*) keeps working until stopped.
        idle_release: Release prepared resources after this many empty seconds.
            An expired *idle_exit* takes precedence.
        prepare: Acquire resources before processing the first nonempty batch,
            and again after an idle release. Heartbeats renew leases during preparation.
        release: Free prepared resources on idle release. The caller owns exit cleanup,
            including cleanup after a failed preparation.
        fixed: The settings given on the command line (keys of
            :data:`MANAGED_OPTIONS`): the server's values for them are ignored.

    Set :attr:`prepared` when the caller has already prepared the resources.
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
        idle_exit: float | None = None,
        idle_release: float | None = None,
        prepare: Callable[[], None] | None = None,
        release: Callable[[], None] | None = None,
        fixed: frozenset[str] = frozenset(),
    ) -> None:
        self.api = api
        self.analyse = analyse
        self.identity = identity
        self.batch = batch
        self.idle_exit = 0.0 if once and idle_exit is None else idle_exit
        self.device = device
        self.stop = stop or threading.Event()
        self.idle_seconds = idle_seconds
        self.idle_release = idle_release
        self.prepare = prepare
        self.release = release
        self.stats = WorkerStats()
        self._backoff = 0.0
        self._heartbeat_every = 30.0
        self.server_identity: dict[str, object] | None = None
        self.prepared = False
        # Settings from the server (#554), applied by the heartbeat thread and the loop.
        self.fixed = fixed
        self.paused = False
        self.settings_revision: int | None = None
        self._own: dict[str, float | None] = {"batch": batch, "idle_release": idle_release}
        self._overridden: set[str] = set()
        self._settings_lock = threading.Lock()

    @property
    def identity(self) -> dict[str, object]:
        """What this worker analyses with; the server is told too, once it manages workers."""
        return self._identity

    @identity.setter
    def identity(self, value: dict[str, object]) -> None:
        self._identity = value
        self.api.identity = dict(value)

    # -- settings from the server ---------------------------------------

    def apply_settings(self, settings: object) -> None:
        """Apply the server's settings for this worker once per revision.

        A value given on the command line wins, and that is logged once.  A
        value the server leaves unset goes back to this worker's own.
        """
        if not isinstance(settings, dict):
            return
        revision = settings.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool):
            return
        with self._settings_lock:
            # An older reply, such as a slow claim's, never undoes a newer one.
            if self.settings_revision is not None and revision <= self.settings_revision:
                return
            minutes = _whole(settings.get("idle_release_minutes"), _MAX_IDLE_RELEASE_MINUTES)
            wanted = {
                "batch": _whole(settings.get("batch"), _MAX_BATCH),
                "idle_release": None if minutes is None else minutes * 60.0,
            }
            for name, value in wanted.items():
                own = self._own[name]
                if name in self.fixed:
                    if value is not None and value != own and name not in self._overridden:
                        self._overridden.add(name)
                        logger.info(
                            "The server asks for %s; %s on the command line wins.",
                            _setting_words(name, value),
                            _option_words(name, own),
                        )
                    continue
                chosen = own if value is None else value
                if chosen != getattr(self, name):
                    setattr(self, name, chosen)
                    logger.info("The server set %s.", _setting_words(name, chosen))
            paused = settings.get("paused") is True
            if paused != self.paused:
                self.paused = paused
                if paused:
                    logger.info("Paused by the server: no more songs until it resumes this worker.")
                else:
                    logger.info("Resumed by the server.")
            self.settings_revision = revision
            self.api.settings_revision = revision

    # -- helpers --------------------------------------------------------

    def _wait(self, seconds: float) -> None:
        self.stop.wait(seconds)

    def _now(self) -> float:
        return time.monotonic()

    def _idle_pause(self, empty_since: float, now: float) -> float | None:
        """How long to wait after an empty claim before asking again; ``None`` to stop."""
        if self.idle_exit is None:
            logger.debug("Nothing to analyse; asking again later.")
            return self.idle_seconds
        left = empty_since + self.idle_exit - now
        if left <= 0:
            if self.idle_exit:
                logger.info("Queue empty for %s; stopping.", _minutes(self.idle_exit))
            else:
                logger.info("Nothing left to analyse.")
            return None
        if now == empty_since:
            logger.info("Queue empty; waiting up to %s for new songs.", _minutes(self.idle_exit))
        return min(self.idle_seconds, left)

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
        reply = self._start_heartbeat(connect=True)
        self.server_identity = dict(reply.get("identity", {}))
        self.check_identity(self.server_identity, ("model", "analysis_version", "dimension"))
        self._heartbeat_every = float(reply.get("heartbeat_seconds", 30))
        self.apply_settings(reply.get("settings"))
        return self.server_identity

    def report_identity(self) -> None:
        """Tell a server that manages workers this worker's full identity, weights included.

        That server refuses a mismatch and shows why in Settings.  An older
        server is not asked; :meth:`check_identity` decides either way.

        Raises:
            WorkerError: The server refused the identity, or could not be reached.
        """
        if getattr(self.api, "manages", False) is not True:
            return
        self.apply_settings(self._start_heartbeat().get("settings"))

    def _start_heartbeat(self, *, connect: bool = False) -> dict[str, Any]:
        try:
            return self._call(lambda: self.api.heartbeat(self.device, connect=connect))
        except ApiError as exc:
            if exc.status == 409:
                # The server's own words: what differs and what to change.
                raise WorkerError(exc.detail) from exc
            raise WorkerError(f"Could not start work with the server: {exc}") from exc
        except NetworkError as exc:
            raise WorkerError(f"Could not start work with the server: {exc}") from exc

    # -- heartbeat ------------------------------------------------------

    def _beat(self, finished: threading.Event) -> None:
        while not finished.wait(self._heartbeat_every):
            try:
                reply = self.api.heartbeat(self.device)
            except (ApiError, NetworkError) as exc:
                logger.debug("Heartbeat failed: %s", exc)
                continue
            self.apply_settings(reply.get("settings"))

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
                self._release([lease], retry=True)
                return
            except ValueError as exc:
                self._report_failure(lease, name, number, str(exc))
                return
            if file_fingerprint(path) != (size, fingerprint):
                self._report_failure(lease, name, number, "the download did not match the song")
                return
            try:
                scan = None
                if song.get("scan") is not None:
                    from autodj.scan_analysis import analyse_scan

                    scan = analyse_scan(path, song["scan"])
                embed = song.get("embed", True)
                maximum = song.get("advanced_max_seconds", 0)
                if scan is not None and maximum and scan["length"] > maximum:
                    embed = False
                analysis = self.analyse(path) if embed else None
                if analysis is not None:
                    energy = analysis.energy
                elif scan is not None:
                    energy = scan["energy"]
                else:
                    raise ValueError("claim has no analysis task")
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
            "vector": encode_vector(analysis.vector) if analysis is not None else None,
            "energy": energy,
            "key": analysis.key if analysis is not None else -1,
            "mode": analysis.mode if analysis is not None else -1,
            "bpm": analysis.bpm if analysis is not None else 0.0,
            "tempo_confidence": analysis.tempo_confidence if analysis is not None else 0.0,
        }
        if scan is not None:
            result["scan"] = scan
        try:
            stored = self._call(lambda: self.api.submit(result), give_up_on_stop=False)
        except ApiError as exc:
            logger.warning("%s %s not stored: %s", number, name, exc.detail)
            if scan is not None and exc.status in {409, 413, 422}:
                self._report_failure(lease, name, number, "server rejected scan measurements")
                return
            self.stats.stale += 1
            self._release([lease], retry=True)
            return
        if stored is False:
            logger.warning("%s %s not stored by the server.", number, name)
            self.stats.stale += 1
            self._release([lease], retry=True)
            return
        self.stats.done += 1
        logger.info("%s %s analysed in %.1f s.", number, name, time.monotonic() - started)

    def _report_failure(self, lease: str, name: str, number: str, reason: str) -> None:
        logger.warning("%s %s could not be analysed: %s", number, name, reason)
        self.stats.failed += 1
        with contextlib.suppress(ApiError, NetworkError):
            self._call(lambda: self.api.fail(lease, reason), give_up_on_stop=False)
        self._release([lease], retry=True)

    def _release(self, leases: list[str], *, retry: bool = False) -> None:
        if not leases:
            return
        try:
            if retry:
                self._call(lambda: self.api.release(leases))
            else:
                self.api.release(leases)
            logger.info("Gave %d unanalysed songs back to the server.", len(leases))
        except (ApiError, NetworkError) as exc:
            if retry and not self.stop.is_set():
                raise WorkerError(f"Could not release rejected songs: {exc}") from exc
            logger.warning(
                "Could not give %d songs back (%s); the server takes them back by itself "
                "in a few minutes.",
                len(leases),
                exc,
            )

    def _release_when_idle(
        self, prepared: bool, empty_since: float, now: float, pause: float, why: str
    ) -> tuple[bool, float]:
        """Free the prepared resources once *idle_release* has passed since *empty_since*.

        Returns whether they are still prepared, and how long to wait.
        """
        if not prepared or self.idle_release is None:
            return prepared, pause
        left = empty_since + self.idle_release - now
        if left > 0:
            return prepared, min(pause, left)
        if self.release is not None:
            self.release()
        logger.info("%s for %s; released analysis resources.", why, _minutes(self.idle_release))
        return False, pause

    def run(self) -> WorkerStats:
        """Work until stopped, or until the queue has been empty for *idle_exit*.

        While the server has this worker paused, it claims nothing and waits
        for a heartbeat to resume it.  Pausing frees the prepared resources
        at once, idle release or not, so the after-command runs; resuming
        prepares again only when songs arrive.  Paused counts as an empty
        queue for *idle_exit*, so ``--once`` never waits on a pause.

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
        empty_since: float | None = None
        paused_since: float | None = None
        prepared = self.prepared
        try:
            with tempfile.TemporaryDirectory(prefix="autodj-worker-") as folder:
                while not self.stop.is_set():
                    if self.paused:
                        now = self._now()
                        if paused_since is None:
                            paused_since = now
                            if prepared:
                                if self.release is not None:
                                    self.release()
                                prepared = False
                                logger.info("Paused; released analysis resources.")
                        # Paused is an empty queue for --once and --idle-exit.
                        empty_since = now if empty_since is None else empty_since
                        paused_wait = self._idle_pause(empty_since, now)
                        if paused_wait is None:
                            break
                        self._wait(paused_wait)
                        continue
                    if paused_since is not None:
                        paused_since = None
                        empty_since = None
                    try:
                        claim = self._call(lambda: self.api.claim(self.batch, self.device))
                    except (ApiError, NetworkError) as exc:
                        if self.stop.is_set():
                            break
                        raise WorkerError(f"The server refused to hand out songs: {exc}") from exc
                    self.apply_settings(claim.get("settings"))
                    self.check_identity(claim.get("identity", {}))
                    songs = list(claim.get("songs", []))
                    if not songs:
                        if self.paused:
                            continue
                        now = self._now()
                        empty_since = now if empty_since is None else empty_since
                        pause = self._idle_pause(empty_since, now)
                        if pause is None:
                            break
                        prepared, pause = self._release_when_idle(
                            prepared, empty_since, now, pause, "Queue empty"
                        )
                        self._wait(pause)
                        continue
                    empty_since = None
                    held = [str(song["lease"]) for song in songs]
                    if not prepared:
                        if self.prepare is not None:
                            self.prepare()
                        prepared = True
                    waiting = int(claim.get("waiting", 0))
                    for position, song in enumerate(songs, 1):
                        # Paused: the rest of the batch goes back after the song in hand.
                        if self.stop.is_set() or self.paused:
                            break
                        self._process(
                            song, Path(folder), f"[{position}/{len(songs)}, {waiting} waiting]"
                        )
                        held.remove(str(song["lease"]))
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
    "load_worker_token",
    "pair_with_server",
    "release_gpu",
    "run_hook",
    "save_worker_token",
    "server_key",
    "worker_token_file",
]
