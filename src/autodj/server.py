"""FastAPI web server and PlayerBridge for the autodj serve command.

Architecture
------------
``autodj serve`` runs a single process:

- :class:`Player` runs in a background daemon thread (blocking loop).
- :class:`PlayerBridge` (in :mod:`autodj._bridge`) is the thread-safe adapter
  between the Player and the FastAPI app.
- FastAPI + uvicorn run on the main thread (asyncio event loop).
- A startup asyncio task broadcasts the current player state to all connected
  WebSocket clients once per second.

Routes
------
Shared routes (pairing, the library, search, cover art, audio files) are
registered here.  Routes that act on one station (``/api/status``,
``/api/skip``, the queue, settings, stream, schedule, carts and the rest)
live in :mod:`autodj.station_routes` and answer twice: under ``/api`` for
the default station and under ``/api/stations/<slug>`` for any station by
name.

``GET  /``                  → index.html
``GET  /api/status``        → JSON state snapshot (station)
``GET  /api/version``       → {version, commit, built_at, commit_url?} for footer build stamp
                              (commit and built_at only with a session when pairing is on)
``POST /api/skip``          → skip to next track (station)
``GET  /api/search?q=``     → fuzzy search of indexed tracks
``WS   /ws``                → push the default station's state JSON every 1 second
``WS   /api/stations/<slug>/ws`` → the same for one station by name

Stream mode (``serve --stream``) adds ``GET /stream.mp3`` and
``GET /stream.m3u`` (no login required) and the authenticated
``GET /api/stream`` and persistent stream configuration through ``PATCH /api/settings``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import functools
import hmac
import json
import logging
import os
import re
import shutil
import ssl
import stat
import sys
import threading
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, BinaryIO, Literal

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from starlette.background import BackgroundTask
from starlette.convertors import Convertor, register_url_convertor
from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import NotModifiedResponse
from starlette.types import Receive, Scope, Send

# PlayerBridge lives in autodj._bridge; it is re-exported here.
from autodj._bridge import LibraryServices, PlayerBridge
from autodj.ads_api import BREAKS_PATH as AD_BREAKS_PATH
from autodj.ads_api import JSON_BODY_MAX_BYTES as AD_JSON_BODY_MAX_BYTES
from autodj.ads_api import UPLOAD_PATH as AD_UPLOAD_PATH
from autodj.breaks_api import (
    ANNOUNCER_SCOPES,
    UPLOAD_PATH,
    VOICE_MAX_UPLOAD_BYTES,
    VOICE_UPLOAD_PATH,
)
from autodj.breaks_api import MULTIPART_OVERHEAD_BYTES as BREAK_MULTIPART_OVERHEAD
from autodj.catalog import ADVANCED_MAX_MINUTES_LIMIT
from autodj.config import AdsConfig, BreaksConfig
from autodj.icy import METAINT
from autodj.indexer import analysis_identity
from autodj.lan import (
    LOG_FILE_TEXT,
    PAIRING_ON_REQUEST_HINT,
    format_lan_banner,
    lan_urls,
    running_in_container,
)
from autodj.listener_access import (
    AddressLimitError,
    BannedAddressError,
    ListenerAuthLimiter,
    StationFullError,
    normalize_address,
    trusted_proxy_setting,
)
from autodj.live_routes import install_live_routes
from autodj.machine_tokens import MachineTokenStore
from autodj.matched_intros_api import RULE_BODY_MAX_BYTES
from autodj.matched_intros_api import RULES_PATH as INTRO_RULES_PATH
from autodj.matched_intros_api import UPLOAD_PATH as INTRO_UPLOAD_PATH
from autodj.model_identity import CachedModelCommit
from autodj.pairing import DeviceRegistry
from autodj.player_page import install_player_pages
from autodj.request_page import install_request_pages
from autodj.scheduler import system_timezone
from autodj.secret_files import machine_tokens_path, paired_devices_path
from autodj.security import (
    COOKIE_NAME,
    LIVE_SOURCE_PATH,
    PAIRING_BODY_MAX_BYTES,
    SecurityMiddleware,
    SecurityPolicy,
    _raw_header_values,
    emit_audit,
    new_request_id,
    peer_address,
)
from autodj.station_log import (
    CURRENT_STATION,
    DEFAULT_SLUG,
    install_log_prefix,
    station_thread,
)
from autodj.station_name import listener_name
from autodj.station_routes import (
    STATION_PREFIX,
    StationRouter,
    equip_station,
    install_station_routes,
    resolve_station,
)
from autodj.station_routes import audio_mime as _audio_mime
from autodj.stations import (
    AbandonedPlayer,
    ExtraStation,
    SharedLibrary,
    StationControlError,
    StationPaths,
    StationRegistry,
    StationRuntime,
    StoppedStation,
    public_feed_path,
    quiesce_cache_writers,
    tick_stream_workers,
    timed_shutdown_step,
)
from autodj.status_page import STATUS_PAGE_CSP, render_off_air_page, render_status_page
from autodj.stereo import SAFE_FFMPEG_INPUT
from autodj.stereotool_presets import MAX_PRESET_BYTES
from autodj.stream import EncoderUnavailableError, ListenerLimitError
from autodj.stream_formats import FORMATS
from autodj.tags_api import TAG_BODY_MAX_BYTES, install_tag_editor, tag_form
from autodj.time_check_files import MAX_PACK_UPLOAD_BYTES
from autodj.time_checks import CLIP_UPLOAD_PATH as TIME_CHECK_CLIP_PATH
from autodj.time_checks import PACK_UPLOAD_PATH as TIME_CHECK_PACK_PATH
from autodj.tls import CertificateReloader
from autodj.version import current_version
from autodj.worker_api import (
    MACHINE_SCOPES,
    RESULT_BODY_MAX_BYTES,
    SMALL_BODY_MAX_BYTES,
    WORKER_PAIR_PATH,
    WORKER_PREFIX,
    install_worker_api,
)

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig, StationSpec
    from autodj.indexer import IndexEntry
    from autodj.library import LibraryManager
    from autodj.similarity import SimilarityIndex

logger = logging.getLogger(__name__)

# Set once the web app has finished starting.  The CLI offers Doctor only for
# failures before this point, never for a crash in the middle of a broadcast.
STARTUP_COMPLETE = threading.Event()


_WS_SEND_TIMEOUT_SECONDS = 2.0
# Connections beyond every station's stream listeners that the server
# accepts at once (see serve()'s limit_concurrency).
_CONNECTION_ROOM = 256
# Stream listeners stay connected until they hang up, so uvicorn would wait
# for them forever on Ctrl+C; give them this long, then cancel them.
_STREAM_SHUTDOWN_GRACE_SECONDS = 3
_ALAC_PREFETCH_TIMEOUT_SECONDS = 5.0
# Room for the multipart framing around an uploaded liner.
_MULTIPART_OVERHEAD_BYTES = 64 * 1024
# Total seconds shutdown waits for the player and cache writers to stop.
_SHUTDOWN_TIMEOUT_S = 30.0
_STATIC_DIR = Path(__file__).parent / "static"


@dataclass(eq=False)
class _WebSocketClient:
    """Track one WebSocket connection and its coordinated shutdown state."""

    websocket: Any
    request_id: str | None = None
    session_is_valid: Callable[[], bool] | None = field(default=None, repr=False)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    handler_task: asyncio.Task[Any] | None = None
    failure_code: int | None = None
    close_started: bool = False
    # The socket's route template, for the audit log: /ws or the station's.
    route: str = "/ws"


async def _close_websocket_client(
    client: _WebSocketClient,
    *,
    code: int,
    timeout_seconds: float,
    failure_action: str,
) -> bool:
    """Close one WebSocket client once and audit a failed close attempt."""
    try:
        async with asyncio.timeout(timeout_seconds):
            async with client.send_lock:
                if client.close_started:
                    return True
                client.close_started = True
                client.failure_code = code
                await client.websocket.close(code=code)
    except Exception:
        if client.request_id is not None:
            emit_audit(
                client.request_id,
                failure_action,
                "rejected",
                method="WS",
                route=client.route,
                status=code,
                level=logging.WARNING,
            )
        return False
    return True


async def _close_and_cancel_websocket(
    client: _WebSocketClient, timeout_seconds: float, code: int, failure_action: str
) -> None:
    """Close a client with *code* and cancel its handler task."""
    try:
        await _close_websocket_client(
            client,
            code=code,
            timeout_seconds=timeout_seconds,
            failure_action=failure_action,
        )
    finally:
        task = client.handler_task
        if task is not None and not task.done():
            task.cancel()


def _websocket_session_is_valid(client: _WebSocketClient) -> bool:
    """Return whether the client session callback accepts its current session."""
    if client.session_is_valid is None:
        return True
    try:
        return client.session_is_valid()
    except Exception:
        return False


async def _send_websocket_payload(
    client: _WebSocketClient, payload: str, timeout_seconds: float
) -> bool:
    """Send one payload or close the client after a send or session failure."""
    if not _websocket_session_is_valid(client):
        await _close_and_cancel_websocket(client, timeout_seconds, 4401, "websocket_session")
        return False
    expired = False
    try:
        async with asyncio.timeout(timeout_seconds):
            async with client.send_lock:
                if client.close_started:
                    return False
                if not _websocket_session_is_valid(client):
                    expired = True
                else:
                    await client.websocket.send_text(payload)
    except Exception:
        await _close_and_cancel_websocket(client, timeout_seconds, 1013, "broadcast")
        return False
    if expired:
        await _close_and_cancel_websocket(client, timeout_seconds, 4401, "websocket_session")
        return False
    return True


async def _broadcast_clients(
    clients: tuple[_WebSocketClient, ...],
    payload: str,
    *,
    timeout_seconds: float = _WS_SEND_TIMEOUT_SECONDS,
) -> set[_WebSocketClient]:
    """Broadcast a payload and return clients that did not receive it."""
    results = await asyncio.gather(
        *(_send_websocket_payload(client, payload, timeout_seconds) for client in clients)
    )
    return {client for client, sent in zip(clients, results, strict=True) if not sent}


async def _broadcast_and_prune(
    clients: set[_WebSocketClient],
    clients_lock: asyncio.Lock,
    payload: str,
    *,
    timeout_seconds: float = _WS_SEND_TIMEOUT_SECONDS,
) -> set[_WebSocketClient]:
    """Broadcast a payload and remove clients that failed to receive it."""
    async with clients_lock:
        snapshot = tuple(clients)
    dead = await _broadcast_clients(snapshot, payload, timeout_seconds=timeout_seconds)
    if dead:
        async with clients_lock:
            clients.difference_update(dead)
    return dead


class _BroadcastFailureLog:
    """Rate-limit the log of failed state pushes to the web pages.

    The first failure with each exception type logs its traceback; after
    that, at most one warning per *every_s* seconds says the pushes still
    fail and how many failures went unreported.  A success after failures
    logs once that pushes work again.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, every_s: float = 60.0) -> None:
        """Start with nothing failing.

        Args:
            clock: Monotonic seconds.
            every_s: Least seconds between two repeat warnings.
        """
        self._clock = clock
        self._every_s = every_s
        self._seen: set[type[BaseException]] = set()
        self._next_warning = 0.0
        self._unreported = 0
        self._failing = False

    def failed(self, exc: Exception) -> None:
        """Log (or count) one failed push."""
        self._failing = True
        now = self._clock()
        if type(exc) not in self._seen:
            self._seen.add(type(exc))
            logger.error("Pushing state to the web pages failed; trying again", exc_info=exc)
        elif now >= self._next_warning:
            logger.warning(
                "Pushing state to the web pages still fails (%s: %s); "
                "%d failures since the last report",
                type(exc).__name__,
                exc,
                self._unreported + 1,
            )
        else:
            self._unreported += 1
            return
        self._unreported = 0
        self._next_warning = now + self._every_s

    def succeeded(self) -> None:
        """Note a push that worked, ending a run of failures."""
        if self._failing:
            self._failing = False
            self._unreported = 0
            logger.info("Pushing state to the web pages works again")


async def _broadcast_state_once(
    bridge: PlayerBridge,
    clients: set[_WebSocketClient],
    clients_lock: asyncio.Lock,
    failures: _BroadcastFailureLog,
    listener_revision: int | None = None,
    recording_status: Callable[[], dict[str, object]] | None = None,
    station_list: Callable[[], list[dict[str, Any]]] | None = None,
    live_state: Callable[[], dict[str, Any]] | None = None,
) -> None:
    """Push the player state to every WebSocket client, if any are connected.

    With several stations, *station_list* gives every station's entry, as
    ``GET /api/stations`` does, so the page's station switcher stays current.

    A failure (building the state, encoding it, or sending it) is logged
    through *failures* and never raised, so the next push still runs:
    one bad state must not stop every page's updates until a restart.
    """
    if not clients:
        return
    try:
        # Off the event loop: it reads the catalog, and pages, the stream and
        # API calls must not wait on that.
        state = await asyncio.to_thread(bridge.get_state)
        if listener_revision is not None:
            state["stream_listener_revision"] = listener_revision
        if recording_status is not None:
            state["recording"] = recording_status()
        if station_list is not None:
            state["stations"] = station_list()
        if live_state is not None:
            state.update(live_state())
        payload = json.dumps(state)
        await _broadcast_and_prune(clients, clients_lock, payload)
    except Exception as exc:
        failures.failed(exc)
    else:
        failures.succeeded()


# Moved to autodj.stations; still importable from here.
_quiesce_cache_writers = quiesce_cache_writers
_tick_stream_workers = tick_stream_workers


def _advertised_server_origin(policy: SecurityPolicy) -> str:
    """Choose one browser URL accepted by both transport and request policy."""
    from urllib.parse import urlsplit

    expected_scheme = "https" if policy.secure_cookie else "http"
    for origin in policy.effective_allowed_origins():
        parsed = urlsplit(origin)
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if (
            parsed.scheme == expected_scheme
            and origin_port == policy.config.port
            and policy.host_allowed(parsed.netloc)
        ):
            return origin
    raise ValueError(
        "server.allowed_origins must include an allowed origin whose hostname is "
        "present in server.allowed_hosts and whose scheme and port match server TLS/bind"
    )


# Browsers get index.html fresh every time; scripts and the stylesheet
# carry an ETag and are revalidated on each load (304 when unchanged).
_NO_STORE = {"Cache-Control": "no-store"}
_ASSET_MEDIA_TYPES = {".js": "text/javascript", ".css": "text/css"}


class _RevalidatedStaticFiles(StaticFiles):
    """Serve web UI files with ``Cache-Control: no-cache`` and fixed JS/CSS types.

    The media type does not come from :mod:`mimetypes`, which on Windows reads
    the registry and can call ``.js`` ``text/plain`` (module scripts and
    AudioWorklet modules then refuse to load).
    """

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        """Return the file, or 304 when the request's validator still matches."""
        response = FileResponse(
            full_path,
            status_code=status_code,
            stat_result=stat_result,
            media_type=_ASSET_MEDIA_TYPES.get(Path(full_path).suffix),
            headers={"Cache-Control": "no-cache"},
        )
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return NotModifiedResponse(response.headers)
        return response


class _GZipExceptStream(GZipMiddleware):
    """Gzip responses, except the radio stream routes.

    Starlette leaves audio/*, images, 206 partial responses and bodies that
    are already encoded alone.  But it holds back every response's headers
    until the first body bytes, and a live stream's first bytes can take a
    while, so stream routes bypass it: a speaker gets its 200 at once.  So
    do the live DJ source mounts (#48), whose answer must reach the DJ's
    software before it sends any audio.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass stream and live source requests straight through; gzip the rest."""
        if scope["type"] == "http" and (
            _is_stream_path(scope["path"]) or LIVE_SOURCE_PATH.fullmatch(scope["path"])
        ):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


#: The addresses' extensions: each stream format's, and the playlist's.
_LISTEN_EXTENSIONS = frozenset({*FORMATS, "m3u"})
#: A station's stream or playlist file at the top level, such as ``nightowl.mp3``.
_STREAM_FILE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\.(?:" + "|".join(sorted(_LISTEN_EXTENSIONS)) + ")"
)


class _StreamFileConvertor(Convertor[str]):
    """Matches only a stream file name, so ``/{name}`` shadows no other page."""

    regex = _STREAM_FILE.pattern

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("streamfile", _StreamFileConvertor())


def _is_stream_path(path: str) -> bool:
    """Whether *path* is a station's stream or playlist: ``/stream.mp3``, ``/nightowl.m3u``."""
    return _STREAM_FILE.fullmatch(path.removeprefix("/")) is not None


# Where the footer's build stamp links a commit.  A constant, so neither the
# environment nor a request can point the link anywhere else.
_REPOSITORY_URL = "https://github.com/blindndangerous/AutoDJ"
# A short or full git object name.  Anything else is not shown or linked.
_COMMIT_RE = re.compile(r"[0-9a-f]{7,40}")


def _valid_commit(value: str | None) -> str | None:
    """Return *value* lowercased when it is 7 to 40 hex digits, else None."""
    candidate = (value or "").strip().lower()
    return candidate if _COMMIT_RE.fullmatch(candidate) else None


def _git_commit() -> str | None:
    """Return the checkout's short HEAD commit, or None without git or a checkout."""
    import subprocess  # nosec B404 - trusted invocation (git, fixed argv)

    try:
        # Static argv, no shell, no untrusted input.  A container image has
        # neither git nor .git, so this falls through to AUTODJ_COMMIT.
        out = subprocess.check_output(  # nosec B603 B607
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_STATIC_DIR.parent,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _valid_commit(out)


@functools.cache
def _version_info() -> dict[str, str]:
    """Return {version, commit, built_at} and, for a known commit, commit_url.

    Cached on first call.  ``built_at`` is the modification time of the
    served ``app.js``, or the time of the first call when it is missing.
    Commit is the short SHA from `git rev-parse` when the source tree is a
    git checkout, else the AUTODJ_COMMIT the container image was built
    with, else "unknown".  Either must be 7 to 40 hex digits to be used;
    ``commit_url`` links that commit in the project's repository.
    """
    import datetime

    version = current_version()
    commit = _git_commit() or _valid_commit(os.environ.get("AUTODJ_COMMIT"))

    candidate = _STATIC_DIR / "app.js"
    built_at: str | None = None
    if candidate.exists():
        built_at = datetime.datetime.fromtimestamp(
            candidate.stat().st_mtime, datetime.UTC
        ).isoformat(timespec="seconds")
    if built_at is None:
        built_at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")

    info = {"version": version, "commit": commit or "unknown", "built_at": built_at}
    if commit:
        info["commit_url"] = f"{_REPOSITORY_URL}/commit/{commit}"
    return info


__all__ = ["PlayerBridge", "create_app", "serve"]

AUTO_RESCAN_MIN_MINUTES = 5


class LibraryJobBody(BaseModel):
    """Request body for POST /api/library/run — invokes a CLI subcommand."""

    name: str
    args: list[str] = []


class LibraryFolderBody(BaseModel):
    """Register one operator-selected audio folder."""

    model_config = ConfigDict(extra="forbid")
    path: str
    kind: Literal["music", "liner"] = "music"
    recursive: bool = True
    advanced: bool = True
    analysis: Literal["basic", "audio", "similarity"] | None = None


class LibraryFolderUpdateBody(BaseModel):
    """Change a registered folder's scan options."""

    model_config = ConfigDict(extra="forbid")
    recursive: bool | None = None
    advanced: bool | None = None
    analysis: Literal["basic", "audio", "similarity"] | None = None


class LibraryRescanBody(BaseModel):
    """Options for an explicit library rescan."""

    model_config = ConfigDict(extra="forbid")
    override_tags: StrictBool = False


class LibraryAutoRescanBody(BaseModel):
    """Shared library automatic rescan interval."""

    model_config = ConfigDict(extra="forbid")
    hours: StrictInt = Field(ge=0, le=168)
    minutes: StrictInt = Field(ge=0, le=59)


class LibraryMusicFadeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    drop_db: StrictInt = Field(ge=1, le=30)


class LibraryAdvancedLengthBody(BaseModel):
    """Longest song the advanced analysis takes, in minutes; 0 takes every length."""

    model_config = ConfigDict(extra="forbid")
    minutes: StrictInt = Field(ge=0, le=ADVANCED_MAX_MINUTES_LIMIT)


class LibraryRemoveMissingBody(BaseModel):
    """Explicit confirmation for removing catalog records after a clean scan."""

    model_config = ConfigDict(extra="forbid")
    token: str
    confirmed: StrictBool
    allow_mass: StrictBool = False


class SongPointsBody(BaseModel):
    """Editable points; the catalog validates their times against the recording."""

    model_config = ConfigDict(extra="forbid")
    path: str
    points: dict[str, Any]
    allow_outro: StrictBool | None = None
    station: str = DEFAULT_SLUG


class CueRescanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    station: str = DEFAULT_SLUG


class CueRescanApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    station: str = DEFAULT_SLUG
    token: str
    fields: list[str]


class PairBody(BaseModel):
    """Short-lived code and operator-visible name for one browser."""

    code: str
    device_name: str


class DeviceRenameBody(BaseModel):
    """New operator-visible name for one paired browser."""

    name: str


class StationAutostartBody(BaseModel):
    """Whether a station starts with the server."""

    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


class CreateStationBody(BaseModel):
    """A browser's request for a new stopped station."""

    model_config = ConfigDict(extra="forbid")

    slug: StrictStr
    name: StrictStr
    source: StrictStr | None = None
    copy_clockwheels: StrictBool = False
    copy_schedule: StrictBool = False
    copy_carts: StrictBool = False
    autostart: StrictBool = False

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        from autodj.stations import DEFAULT_SLUG, SLUG_PATTERN

        if value == DEFAULT_SLUG or not SLUG_PATTERN.fullmatch(value):
            raise ValueError(
                "Choose a station ID of 1 to 32 lowercase letters, digits or inner hyphens, other than default or stream"
            )
        return value

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        name = value.strip()
        if not name or len(name) > 80 or not name.isprintable():
            raise ValueError("Enter a printable station name of 1 to 80 characters")
        return name

    @model_validator(mode="after")
    def _flags(self) -> CreateStationBody:
        if self.source is None and (self.copy_clockwheels or self.copy_schedule or self.copy_carts):
            raise ValueError("Copy options need a source station")
        return self


def _is_alac(path: Path) -> bool:
    """Return whether an MP4-family file reports Apple Lossless audio."""
    if path.suffix.lower() not in (".m4a", ".mp4"):
        return False
    try:
        import mutagen
        from mutagen.mp4 import MP4

        mutagen_error = vars(mutagen)["MutagenError"]
    except (ImportError, KeyError):
        return False

    try:
        codec = getattr(MP4(path).info, "codec", None) or ""
        return codec.lower() == "alac"
    except (OSError, ValueError, mutagen_error):
        return False


async def _start_alac_transcoder(executable: str, source: BinaryIO) -> Any:
    """Spawn ffmpeg from resolved executable reading the opened source on stdin."""
    return await asyncio.create_subprocess_exec(
        executable,
        "-loglevel",
        "error",
        *SAFE_FFMPEG_INPUT,
        "-i",
        "pipe:0",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        "-f",
        "mp3",
        "pipe:1",
        stdin=source,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def _transcode_alac_to_mp3(
    process: Any,
    prefetched: bytes = b"",
) -> AsyncGenerator[bytes]:
    """Yield transcoded MP3 bytes and always reap ffmpeg subprocess."""
    try:
        if process.stdout is None:
            return
        if prefetched:
            yield prefetched
        while chunk := await process.stdout.read(64 * 1024):
            yield chunk
    finally:
        await _terminate_alac_process(process)


async def _kill_and_wait(process: Any) -> None:
    """Kill and reap one transcoder process."""
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    await process.wait()


async def _terminate_alac_process(process: Any) -> None:
    """Idempotently finish process cleanup even if caller is cancelled."""
    cleanup = getattr(process, "_autodj_cleanup_task", None)
    if cleanup is None:
        cleanup = asyncio.create_task(_kill_and_wait(process))
        process._autodj_cleanup_task = cleanup
    await asyncio.shield(cleanup)


async def _prefetch_alac_output(process: Any) -> bytes | None:
    """Read one output chunk before response headers or return fallback signal."""
    if process.stdout is None:
        await _terminate_alac_process(process)
        return None
    try:
        async with asyncio.timeout(_ALAC_PREFETCH_TIMEOUT_SECONDS):
            first_chunk = await process.stdout.read(64 * 1024)
    except TimeoutError:
        await _terminate_alac_process(process)
        return None
    except asyncio.CancelledError:
        await _terminate_alac_process(process)
        raise
    except Exception:
        await _terminate_alac_process(process)
        return None
    if not first_chunk:
        await _terminate_alac_process(process)
        return None
    return first_chunk


async def _close_alac_stream(stream: AsyncGenerator[bytes], process: Any) -> None:
    """Close transcoder stream and reap process even before first body read."""
    try:
        await stream.aclose()
    finally:
        await _terminate_alac_process(process)


async def _alac_response(audio_path: Path, method: str) -> Response | None:
    """Return an MP3 transcode of an ALAC file, or None to serve the source bytes."""
    ffmpeg = await asyncio.to_thread(shutil.which, "ffmpeg")
    if ffmpeg is None:
        logger.warning("ffmpeg unavailable; serving ALAC source bytes for %s", audio_path)
        return None
    if method == "HEAD":
        response = Response(
            status_code=200, media_type="audio/mpeg", headers={"Accept-Ranges": "none"}
        )
        del response.headers["content-length"]
        return response
    source = await asyncio.to_thread(audio_path.open, "rb")
    try:
        process = await _start_alac_transcoder(ffmpeg, source)
    except OSError:
        logger.warning(
            "ffmpeg failed to start; serving ALAC source bytes for %s", audio_path, exc_info=True
        )
        return None
    finally:
        await asyncio.to_thread(source.close)
    first_chunk = await _prefetch_alac_output(process)
    if first_chunk is None:
        logger.warning(
            "ffmpeg produced no initial output; serving ALAC source bytes for %s", audio_path
        )
        return None
    try:
        stream = _transcode_alac_to_mp3(process, first_chunk)
        return StreamingResponse(
            stream,
            media_type="audio/mpeg",
            headers={"Accept-Ranges": "none"},
            background=BackgroundTask(_close_alac_stream, stream, process),
        )
    except BaseException:
        await _terminate_alac_process(process)
        raise


# ---------------------------------------------------------------------------
# FastAPI application factory
# ---------------------------------------------------------------------------


_LISTENER_SEND_TIMEOUT = 10.0


class _ListenerResponse(StreamingResponse):
    """A streaming response that frees its stream listener however it ends.

    ``on_close`` runs once the response is over: finished, cancelled, the
    client gone, or sending failed before the body even started (which a
    generator's ``finally`` or a background task would miss).
    """

    def __init__(self, content: Any, *, on_close: Callable[[], None], **kwargs: Any) -> None:
        """Wrap *content* like :class:`StreamingResponse`; call *on_close* at the end."""
        super().__init__(content, **kwargs)
        self._on_close = on_close
        self._released = False

    def _release(self) -> None:
        if not self._released:
            self._released = True
            self._on_close()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Send the response, then always release the listener."""

        timed_out = asyncio.Event()

        async def send_with_timeout(message: Any) -> None:
            try:
                await asyncio.wait_for(send(message), timeout=_LISTENER_SEND_TIMEOUT)
            except TimeoutError:
                timed_out.set()
                self._release()
                raise

        async def receive_or_timeout() -> Any:
            if timed_out.is_set():
                return {"type": "http.disconnect"}
            incoming = asyncio.create_task(receive())
            timeout = asyncio.create_task(timed_out.wait())
            try:
                done, _ = await asyncio.wait(
                    (incoming, timeout), return_when=asyncio.FIRST_COMPLETED
                )
                if incoming in done:
                    return incoming.result()
                return {"type": "http.disconnect"}
            finally:
                for task in (incoming, timeout):
                    if not task.done():
                        task.cancel()

        try:
            await super().__call__(scope, receive_or_timeout, send_with_timeout)
        except Exception:
            # A listener dropped for a stalled send has been released; end
            # quietly so uvicorn does not log every slow client as an error.
            if not timed_out.is_set():
                raise
        finally:
            self._release()


def _header_text(value: str) -> str:
    """Carry *value*'s UTF-8 bytes through the latin-1 HTTP header transport.

    Icecast does the same with ``icy-name``: players that expect UTF-8
    decode it, and a non-Latin-1 name no longer breaks the response.
    """
    return value.encode("utf-8").decode("latin-1")


# A schedule event is a few hundred bytes; this leaves ample room.
_SCHEDULE_BODY_MAX_BYTES = 16 * 1024
_SETTINGS_BODY_MAX_BYTES = 64 * 1024
# A show's list (#106): 500 paths, each well under 2 KiB in practice.
SHOWS_SONGS_BODY_MAX_BYTES = 1024 * 1024


def create_app(
    *,
    services: LibraryServices,
    default_station: ExtraStation,
    secure_cookie: bool = False,
    extra_stations: Sequence[ExtraStation] = (),
) -> FastAPI:
    """Create the server with shared library services and station factories."""
    _lyrics_read_gate = asyncio.Semaphore(1)
    install_log_prefix()
    cfg = services.cfg
    shared = services
    stations = StationRegistry(default_station, extra_stations)
    station_timezone = cfg.station.timezone or system_timezone()
    station_create_lock = asyncio.Lock()
    stations.stop_timeout_s = _SHUTDOWN_TIMEOUT_S
    listener_auth_limiter = ListenerAuthLimiter()

    async def _read_lyrics_owned(path: str) -> list[dict[str, object]]:
        """Run one path read at a time without cancellation leaking capacity."""
        await _lyrics_read_gate.acquire()
        try:
            worker = asyncio.create_task(asyncio.to_thread(shared.lyrics_for, path))
        except BaseException:
            _lyrics_read_gate.release()
            raise

        def release_capacity(completed: asyncio.Task[list[dict]]) -> None:
            """Release the lyrics-read slot after the worker task finishes."""
            _lyrics_read_gate.release()
            if not completed.cancelled():
                completed.exception()

        worker.add_done_callback(release_capacity)
        return await asyncio.shield(worker)

    # ------------------------------------------------------------------
    # Lifespan (replaces deprecated on_event)
    # ------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """FastAPI lifespan: start and stop the station and the shared library."""
        library = SharedLibrary(shared.library, stations)
        await stations.start_all()
        await library.start()
        STARTUP_COMPLETE.set()
        try:
            yield
        finally:
            logger.info("Shutting down...")
            # The scan winds down while the rest stops; the library waits for
            # it before the DJ-meta cache closes.
            library.stop_scan(_SHUTDOWN_TIMEOUT_S)
            from autodj.jobs import get_manager

            async def stopped(name: str, worker: Any) -> bool:
                """Shut *worker* down in a thread; whether it stopped in time (or is absent)."""
                if worker is None:
                    return True
                return bool(
                    await timed_shutdown_step(
                        name, asyncio.to_thread(worker.shutdown, _SHUTDOWN_TIMEOUT_S)
                    )
                )

            # These are independent, so they stop together: one after another,
            # their timeouts would add up.
            (
                job_was_running,
                speech_stopped,
                rotation_stopped,
                shows_stopped,
                previews_stopped,
                rescan_stopped,
                reviews_stopped,
            ) = await asyncio.gather(
                # A Library tools job is a child process: stop it as its Stop
                # button does, so a restart does not leave it beside the next server.
                timed_shutdown_step(
                    "stopping the library job",
                    asyncio.to_thread(get_manager().shutdown, _SHUTDOWN_TIMEOUT_S),
                ),
                stopped("stopping speech", getattr(app.state, "speech", None)),
                # A rotation test reads the library: stop it before the library closes.
                stopped("stopping rotation tests", getattr(app.state, "rotation_tests", None)),
                # So does a show's generation (#106): it is cancelled and ends at
                # its next pick.
                stopped("stopping show generations", getattr(app.state, "show_generations", None)),
                # So does an hour preview (#126): it ends within its own time limit,
                # and a worker that has not stopped keeps the library open.
                stopped("stopping hour previews", getattr(app.state, "hour_previews", None)),
                stopped("stopping the cue rescan", getattr(app.state, "cue_rescan", None)),
                stopped("stopping review jobs", getattr(app.state, "review_jobs", None)),
            )
            if job_was_running:
                logger.info("Stopped the running library job")
            if not speech_stopped:
                logger.warning(
                    "Speech worker is still stopping; pending requests will not be retried"
                )
            if not rotation_stopped:
                logger.warning(
                    "A rotation test was still running %.2f seconds after it was cancelled",
                    _SHUTDOWN_TIMEOUT_S,
                )
            if not shows_stopped:
                logger.warning(
                    "A show's list was still being generated %.2f seconds after it was cancelled",
                    _SHUTDOWN_TIMEOUT_S,
                )
            if not previews_stopped:
                logger.error(
                    "An hour preview was still reading the library %.2f seconds after "
                    "shutdown began; the library is left open rather than closed under it",
                    _SHUTDOWN_TIMEOUT_S,
                )
            if not rescan_stopped:
                logger.error("A cue rescan is still reading; the library remains open")
            if not reviews_stopped:
                logger.error("A review renderer is still reading; the library remains open")
            readers_stopped = previews_stopped and rescan_stopped and reviews_stopped
            writers_quiesced, cache_closed = await timed_shutdown_step(
                "stopping the stations and the library",
                stations.shutdown(library, _SHUTDOWN_TIMEOUT_S, readers_stopped=readers_stopped),
            )
            from autodj.mastering import kill_leftover_processes

            # A player that would not stop kept its mastering process; no
            # Stereo Tool may outlive the server.  In a worker thread: it
            # waits up to 2 seconds for each process it kills.
            loop = asyncio.get_running_loop()
            if killed := await timed_shutdown_step(
                "stopping leftover mastering processes",
                loop.run_in_executor(None, kill_leftover_processes),
            ):
                logger.warning("Stopped %d mastering process(es) left running", killed)
            if writers_quiesced and cache_closed:
                logger.info("Server stopped cleanly.")
            elif not writers_quiesced:
                logger.warning(
                    "Degraded shutdown: DJ-meta cache remains open because writers "
                    "did not stop within %.2f seconds",
                    _SHUTDOWN_TIMEOUT_S,
                )
                logger.warning(
                    "Server shutdown completed in degraded mode: active cache writers "
                    "remain; DJ-meta cache left open."
                )
            else:
                # A scan that would not stop, or a close that failed.
                logger.warning(
                    "Server shutdown completed in degraded mode: DJ-meta cache could not be closed."
                )

    # No /docs, /redoc or /openapi.json: the web UI does not use them, and
    # a full map of every route and body schema is reconnaissance material
    # for anyone who reaches the port.
    app = FastAPI(
        title="AutoDJ",
        version=current_version(),
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.listener_access = None
    app.state.stations = stations
    from autodj.cue_rescan import CueRescan

    app.state.cue_rescan = CueRescan()

    from autodj.speech_api import SPEECH_BODY_LIMIT, WRITE_ROUTES, install_speech

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """Return a 422 whose body is always JSON-encodable.

        FastAPI's default handler echoes the rejected input back to the
        client.  A rejected ``NaN``/``Infinity`` cannot be re-encoded, so the
        echo itself would raise and turn a validation error into a 500.  Only
        the type, location and message are returned.

        Errors marked ``reports_field`` answer
        with their message and ``field`` so the editor can focus that input.
        Other validation errors keep the list format.
        """
        for error in exc.errors():
            cause = (error.get("ctx") or {}).get("error")
            named = getattr(cause, "field", None)
            if getattr(cause, "reports_field", False) is True and isinstance(named, str) and named:
                message = str(getattr(cause, "message", "") or cause)
                return JSONResponse(status_code=422, content={"detail": message, "field": named})
        detail = [
            {
                "type": str(error.get("type", "")),
                "loc": [str(part) for part in error.get("loc", ())],
                "msg": str(error.get("msg", "")),
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": detail})

    def _liner_upload_max_bytes() -> int:
        return (
            max(
                stations.configuration(slug).station.liner_upload_max_mib
                for slug in stations.slugs()
            )
            * 1024
            * 1024
        )

    server_config = cfg.server
    if server_config.auth_mode == "trusted_proxy":
        from autodj.config import is_loopback_bind, validate_server_exposure

        validate_server_exposure(server_config)
        if not is_loopback_bind(server_config.host) and server_config.ssl_certfile is None:
            logger.warning(
                "Trusted-proxy authentication on a non-loopback bind without TLS can expose "
                "the proxy secret. Keep proxy traffic on loopback or a private network on "
                "the same host; use TLS between hosts."
            )
    device_registry: DeviceRegistry | None = None
    if isinstance(server_config.access_token, str) or server_config.auth_mode == "trusted_proxy":
        device_registry = DeviceRegistry(paired_devices_path(cfg))
    policy = SecurityPolicy(
        server_config,
        secure_cookie=secure_cookie,
        device_is_active=(device_registry.is_active if device_registry is not None else None),
    )
    # Read-only handles for serve()'s startup banner and for tests; the
    # routes and the middleware use these same objects directly.
    app.state.security_policy = policy
    app.state.device_registry = device_registry

    def request_pairing_code() -> tuple[str, int, int]:
        """Return the current pairing code and let one device pair with it.

        Returns ``(code, valid_seconds, next_code_seconds)``.  The code works
        until it expires or one device pairs with it, whichever comes first.

        Raises:
            RuntimeError: Pairing is not enabled.
        """
        if device_registry is None or not policy.pairing_required:
            raise RuntimeError("pairing is not enabled")
        code, valid_for, next_code_in = policy.pairing_code_now()
        device_registry.open_pairing(valid_for)
        return code, valid_for, next_code_in

    # serve() asks for the first code when no browser is paired yet.
    app.state.request_pairing_code = request_pairing_code
    # Request bodies the middleware caps before any route parses them.
    from autodj.prepared_editions import MAX_UPLOAD_BYTES as EDITION_UPLOAD_BYTES
    from autodj.updates_api import ROUTES as UPDATE_ROUTES

    body_limits: dict[str, Callable[[], int]] = {
        **{path: (lambda: SPEECH_BODY_LIMIT) for path in WRITE_ROUTES},
        "/api/stations": lambda: 4096,
        "/api/pair": lambda: PAIRING_BODY_MAX_BYTES,
        "/api/liners/upload": lambda: _liner_upload_max_bytes() + _MULTIPART_OVERHEAD_BYTES,
        "/api/carts/upload": lambda: _liner_upload_max_bytes() + _MULTIPART_OVERHEAD_BYTES,
        # Matched intros (#127) have the liner upload's cap.
        INTRO_UPLOAD_PATH: lambda: _liner_upload_max_bytes() + _MULTIPART_OVERHEAD_BYTES,
        # Time checks (#388): a voice pack zip, and one intro, outro or minute clip.
        TIME_CHECK_PACK_PATH: lambda: MAX_PACK_UPLOAD_BYTES + _MULTIPART_OVERHEAD_BYTES,
        TIME_CHECK_CLIP_PATH: lambda: _liner_upload_max_bytes() + _MULTIPART_OVERHEAD_BYTES,
        INTRO_RULES_PATH: lambda: RULE_BODY_MAX_BYTES,
        "/api/stereo-tool-presets/upload": lambda: MAX_PRESET_BYTES + _MULTIPART_OVERHEAD_BYTES,
        "/api/stereo-tool/license": lambda: SMALL_BODY_MAX_BYTES,
        "/api/stereo-tool/download": lambda: SMALL_BODY_MAX_BYTES,
        **{path: (lambda: SMALL_BODY_MAX_BYTES) for path in UPDATE_ROUTES},
        "/api/schedule": lambda: _SCHEDULE_BODY_MAX_BYTES,
        "/api/machine-tokens": lambda: SMALL_BODY_MAX_BYTES,
        "/api/machine-tokens/pairing-code": lambda: SMALL_BODY_MAX_BYTES,
        "/api/analysis/workers/{token_id}/settings": lambda: SMALL_BODY_MAX_BYTES,
        WORKER_PAIR_PATH: lambda: SMALL_BODY_MAX_BYTES,
        "/api/live/djs": lambda: SMALL_BODY_MAX_BYTES,
        "/api/library/points/rescan": lambda: SMALL_BODY_MAX_BYTES,
        "/api/library/points/rescan/apply": lambda: SMALL_BODY_MAX_BYTES,
        "/api/library/music-fade": lambda: SMALL_BODY_MAX_BYTES,
        "/api/settings": lambda: _SETTINGS_BODY_MAX_BYTES,
        "/api/stations/{slug}/settings": lambda: _SETTINGS_BODY_MAX_BYTES,
        "/api/stream/relays": lambda: SMALL_BODY_MAX_BYTES,
        "/api/stations/{slug}/stream/relays": lambda: SMALL_BODY_MAX_BYTES,
        # The largest break any station takes; each station checks its own.
        UPLOAD_PATH: lambda: _break_upload_cap(stations),
        VOICE_UPLOAD_PATH: lambda: _voice_upload_cap(stations),
        # Ad spots (#51): the largest any station takes; each checks its own.
        AD_UPLOAD_PATH: lambda: _ad_upload_cap(stations),
        AD_BREAKS_PATH: lambda: AD_JSON_BODY_MAX_BYTES,
        "/api/rotation-test": lambda: SMALL_BODY_MAX_BYTES,
        # 500 keep-complete targets of up to 80 characters, plus timing fields.
        "/api/reviews": lambda: 64 * 1024,
        # Shows (#106): POST and PUT; a list of up to 500 songs is the largest.
        "/api/shows": lambda: SMALL_BODY_MAX_BYTES,
        "/api/shows/{show_id}": lambda: SMALL_BODY_MAX_BYTES,
        "/api/shows/{show_id}/songs": lambda: SHOWS_SONGS_BODY_MAX_BYTES,
        "/api/shows/{show_id}/generate": lambda: SMALL_BODY_MAX_BYTES,
        "/api/shows/{show_id}/start": lambda: SMALL_BODY_MAX_BYTES,
        "/api/editions": lambda: SMALL_BODY_MAX_BYTES,
        "/api/editions/{edition_id}": lambda: SHOWS_SONGS_BODY_MAX_BYTES,
        "/api/editions/{edition_id}/takes": lambda: EDITION_UPLOAD_BYTES,
        "/api/editions/{edition_id}/takes/{take_id}/approve": lambda: SMALL_BODY_MAX_BYTES,
        "/api/editions/{edition_id}/takes/{take_id}/reattach": lambda: SMALL_BODY_MAX_BYTES,
        "/api/editions/{edition_id}/start": lambda: SMALL_BODY_MAX_BYTES,
        "/api/library/tags": lambda: TAG_BODY_MAX_BYTES,
        WORKER_PREFIX + "result": lambda: RESULT_BODY_MAX_BYTES,
        **{
            WORKER_PREFIX + name: (lambda: SMALL_BODY_MAX_BYTES)
            for name in ("claim", "failed", "heartbeat", "release")
        },
    }
    # Machine tokens (analysis workers) open their database on first use.
    machine_token_lock = threading.Lock()
    machine_token_store: list[MachineTokenStore] = []

    def machine_tokens() -> MachineTokenStore:
        with machine_token_lock:
            if not machine_token_store:
                machine_token_store.append(MachineTokenStore(machine_tokens_path(cfg)))
            return machine_token_store[0]

    app.state.machine_tokens = machine_tokens
    app.add_middleware(
        SecurityMiddleware,
        policy=policy,
        body_limits=body_limits,
        machine_scopes={**MACHINE_SCOPES, **ANNOUNCER_SCOPES},
        machine_auth=lambda token, scope: machine_tokens().authenticate(token, scope),
        program_paths=frozenset({WORKER_PAIR_PATH}),
        station_slugs=stations.slugs,
    )
    model_commit = CachedModelCommit(lambda: (cfg.model, cfg.index))
    install_worker_api(
        app,
        tokens=machine_tokens,
        library=lambda: getattr(shared, "library", None),
        identity=lambda: analysis_identity(cfg.model.name, model_commit()),
        stations=lambda: _station_names(stations),
    )
    # Compresses the page, scripts and JSON; audio, cover art and Range
    # replies pass through as is.
    app.add_middleware(_GZipExceptStream, minimum_size=1024, compresslevel=6)

    # ------------------------------------------------------------------
    # Static HTML
    # ------------------------------------------------------------------

    _static_html_path = _STATIC_DIR / "index.html"
    # Read the HTML once at startup so the GET / handler never blocks the
    # asyncio event loop on a disk read -- important when the static dir
    # lives on a NAS mount and the file is ~80 KB.
    try:
        _index_html_cache = _static_html_path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover -- only when the package is corrupt
        logger.error("Could not read %s at startup: %s", _static_html_path, exc)
        _index_html_cache = (
            "<!doctype html><meta charset=utf-8>"
            "<title>AutoDJ unavailable</title>"
            "<p>Web UI files missing — see server log.</p>"
        )

    @app.get("/", response_class=HTMLResponse)
    async def get_index() -> HTMLResponse:
        """Serve the web UI HTML from the in-memory cache."""
        return HTMLResponse(content=_index_html_cache, headers=_NO_STORE)

    # The page loads /app.css and /app.js; app.js imports /modules/*.js and
    # the audio engine loads the AudioWorklet modules by absolute URL.
    _assets = _RevalidatedStaticFiles(directory=_STATIC_DIR)

    def _asset_route(filename: str) -> Callable[[Request], Awaitable[Response]]:
        """Build a handler serving one top-level web UI file."""

        async def get_asset(request: Request) -> Response:
            """Serve the stylesheet, the page script or an AudioWorklet module."""
            return await _assets.get_response(filename, request.scope)

        return get_asset

    for _asset_name in (
        "app.css",
        "app.js",
        "bitcrusher-worklet.js",
        "stutter-worklet.js",
        "freeze-worklet.js",
        "glitch-worklet.js",
    ):
        app.get(f"/{_asset_name}")(_asset_route(_asset_name))

    app.mount(
        "/modules", _RevalidatedStaticFiles(directory=_STATIC_DIR / "modules"), name="modules"
    )

    # ------------------------------------------------------------------
    # REST API
    # ------------------------------------------------------------------

    # ADR-0007: the proxy authorizes people; this cookie identifies browsers only.
    sso_cookie = "autodj_browser"

    def sso_user(request: Request) -> tuple[str, str]:
        identity = policy.sso_identity(request.scope)
        if identity is None:
            raise HTTPException(status_code=401, detail="SSO identity is missing or invalid")
        return identity

    @app.get("/api/auth/status")
    async def api_auth_status(request: Request, response: Response) -> dict[str, object]:
        """Report whether this browser holds a valid authenticated session."""
        request_policy = policy
        if request_policy.config.auth_mode == "trusted_proxy":
            identity = request_policy.sso_identity(request.scope)
            if identity is None:
                response.status_code = 401
                response.headers["Cache-Control"] = "no-store"
                return {
                    "required": True,
                    "authenticated": False,
                    "pairing": False,
                    "mode": "trusted_proxy",
                }
            user_id, user_name = identity
            assert device_registry is not None
            browser = await asyncio.to_thread(
                device_registry.register_sso, user_id, user_name, request.cookies.get(sso_cookie)
            )
            response.set_cookie(
                sso_cookie,
                browser.device_id,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                max_age=request_policy.config.session_ttl_seconds,
                path="/",
            )
            response.headers["Cache-Control"] = "no-store"
            return {
                "required": True,
                "authenticated": True,
                "pairing": False,
                "mode": "trusted_proxy",
                "user": {"id": user_id, "name": user_name},
                "device_id": browser.device_id,
                "device_name": browser.name,
                "logout_url": request_policy.config.sso_logout_url,
            }
        device_id = request_policy.session_device_id(request.cookies.get(COOKIE_NAME))
        registry = device_registry
        cookie = request.cookies.get(COOKIE_NAME)

        def _status() -> dict[str, object]:
            """Touch the device and build the status off the event loop.

            touch() writes and verify_session() reads the device SQLite
            database.
            """
            if device_id is not None and registry is not None:
                registry.touch(device_id)
            return {
                "required": request_policy.authentication_required,
                "authenticated": (
                    not request_policy.authentication_required
                    or request_policy.verify_session(cookie)
                ),
                "pairing": request_policy.authentication_required,
                "device_id": device_id,
            }

        return await asyncio.to_thread(_status)

    @app.post("/api/pair")
    async def api_pair(body: PairBody, request: Request) -> Response:
        """Pair one browser and issue its persistent device-bound session."""
        request_policy = policy
        registry = device_registry
        if registry is None or not request_policy.pairing_required:
            raise HTTPException(status_code=409, detail="Pairing is not enabled")
        client = peer_address(request.scope)
        block = request_policy.pairing_block(client)
        if block is not None:
            raise HTTPException(
                status_code=429,
                detail=block.detail,
                headers={"Retry-After": str(block.retry_after)},
            )
        if not request_policy.verify_pairing_code(body.code, client):
            raise HTTPException(status_code=401, detail="Invalid or expired pairing code")
        # A right code works only after someone asked for it, for one device.
        try:
            device = await asyncio.to_thread(registry.pair_on_request, body.device_name)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if device is None:
            raise HTTPException(status_code=401, detail="Invalid or expired pairing code")
        # Issuing checks the device is still active, another SQLite read.
        session = await asyncio.to_thread(request_policy.issue_device_session, device.device_id)
        response = JSONResponse(
            {"authenticated": True, "device_id": device.device_id, "device_name": device.name}
        )
        response.set_cookie(
            COOKIE_NAME,
            session,
            httponly=True,
            samesite="strict",
            secure=request_policy.secure_cookie,
            max_age=request_policy.config.session_ttl_seconds,
            path="/",
        )
        return response

    def _signed_out_response(
        request_policy: SecurityPolicy, payload: dict[str, object]
    ) -> Response:
        """Return *payload* with the session cookie deleted in its own scope."""
        response = JSONResponse(payload)
        response.delete_cookie(
            COOKIE_NAME,
            path="/",
            secure=request_policy.secure_cookie,
            httponly=True,
            samesite="strict",
        )
        return response

    async def _has_session(request: Request) -> bool:
        """Return whether *request* may see details that need pairing.

        True when the server needs no pairing, or when the browser holds a
        valid session.  ``verify_session`` reads the device database, so it
        runs off the event loop.
        """
        request_policy = policy
        if not request_policy.authentication_required:
            return True
        return await asyncio.to_thread(request_policy.browser_authenticated, request.scope)

    @app.post("/api/logout")
    async def api_logout(request: Request) -> Response:
        """Sign this browser out: revoke its paired device and delete the cookie.

        Only deleting the cookie would leave the device active in the paired
        devices list, and a copied cookie would keep working for the rest of
        its 90 days.
        """
        request_policy = policy
        registry = device_registry
        if request_policy.config.auth_mode == "trusted_proxy":
            sso_user(request)
            if request_policy.config.sso_logout_url is None:
                raise HTTPException(status_code=409, detail="SSO sign-out is managed externally")
            return JSONResponse(
                {"authenticated": False, "logout_url": request_policy.config.sso_logout_url}
            )
        device_id = request_policy.session_device_id(request.cookies.get(COOKIE_NAME))
        if device_id is not None and registry is not None:
            await asyncio.to_thread(registry.revoke, device_id)
        return _signed_out_response(request_policy, {"authenticated": False})

    @app.get("/api/devices")
    async def api_devices(request: Request) -> dict[str, object]:
        """List paired browsers, marking the one making this request."""
        request_policy = policy
        registry = device_registry
        if request_policy.config.auth_mode == "trusted_proxy":
            user_id, user_name = sso_user(request)
            assert registry is not None
            browsers = await asyncio.to_thread(registry.sso_devices, user_id)
            current = request.cookies.get(sso_cookie)
            return {
                "pairing": False,
                "mode": "trusted_proxy",
                "user": {"id": user_id, "name": user_name},
                "devices": [
                    {
                        "device_id": browser.device_id,
                        "name": browser.name,
                        "paired_at": browser.paired_at,
                        "last_seen_at": browser.last_seen_at,
                        "current": browser.device_id == current,
                    }
                    for browser in browsers
                ],
            }
        if registry is None or not request_policy.pairing_required:
            return {"pairing": False, "devices": []}
        current = request_policy.session_device_id(request.cookies.get(COOKIE_NAME))
        devices = await asyncio.to_thread(registry.list_devices)
        return {
            "pairing": True,
            "devices": [
                {
                    "device_id": device.device_id,
                    "name": device.name,
                    "paired_at": device.paired_at,
                    "last_seen_at": device.last_seen_at,
                    "current": device.device_id == current,
                }
                for device in devices
                if device.revoked_at is None
            ],
        }

    @app.delete("/api/devices/{device_id}")
    async def api_device_revoke(device_id: str, request: Request) -> Response:
        """Revoke one paired browser; revoking this browser also signs it out."""
        request_policy = policy
        registry = device_registry
        if request_policy.config.auth_mode == "trusted_proxy":
            user_id, _ = sso_user(request)
            assert registry is not None
            if not await asyncio.to_thread(registry.forget_sso, user_id, device_id):
                raise HTTPException(status_code=404, detail="That browser was not found")
            response = JSONResponse({"forgotten": device_id, "signed_out": False})
            if request.cookies.get(sso_cookie) == device_id:
                response.delete_cookie(
                    sso_cookie,
                    path="/",
                    httponly=True,
                    samesite="strict",
                    secure=request.url.scheme == "https",
                )
            return response
        if registry is None or not request_policy.pairing_required:
            raise HTTPException(status_code=409, detail="Pairing is not enabled")
        # Read before revoking: afterwards this session no longer resolves.
        current = request_policy.session_device_id(request.cookies.get(COOKIE_NAME))
        if not await asyncio.to_thread(registry.revoke, device_id):
            raise HTTPException(status_code=404, detail="That device is not paired")
        payload: dict[str, object] = {"revoked": device_id, "signed_out": False}
        if current == device_id:
            payload["signed_out"] = True
            return _signed_out_response(request_policy, payload)
        return JSONResponse(payload)

    @app.patch("/api/devices/{device_id}")
    async def api_device_rename(
        device_id: str, body: DeviceRenameBody, request: Request
    ) -> dict[str, str]:
        """Rename one paired browser."""
        registry = device_registry
        if policy.config.auth_mode == "trusted_proxy":
            user_id, _ = sso_user(request)
            assert registry is not None
            try:
                name = await asyncio.to_thread(registry.rename_sso, user_id, device_id, body.name)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if name is None:
                raise HTTPException(status_code=404, detail="That browser was not found")
            return {"device_id": device_id, "name": name}
        if registry is None or not policy.pairing_required:
            raise HTTPException(status_code=409, detail="Pairing is not enabled")
        try:
            name = await asyncio.to_thread(registry.rename, device_id, body.name)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if name is None:
            raise HTTPException(status_code=404, detail="That device is not paired")
        return {"device_id": device_id, "name": name}

    @app.get("/api/pairing-code")
    async def api_pairing_code() -> JSONResponse:
        """Show a paired browser a pairing code that one more device can use.

        Any paired browser may pair another one, the same as the operator
        running ``autodj devices pairing-code``.  The code is never logged.
        """
        if device_registry is None or not policy.pairing_required:
            raise HTTPException(status_code=409, detail="Pairing is not enabled")
        paused = policy.pairing_paused()
        if paused is not None:
            raise HTTPException(
                status_code=429,
                detail=paused.detail,
                headers={"Retry-After": str(paused.retry_after)},
            )
        code, valid_for, next_code_in = await asyncio.to_thread(request_pairing_code)
        return JSONResponse(
            {"code": code, "valid_seconds": valid_for, "next_code_seconds": next_code_in},
            headers=_NO_STORE,
        )

    @app.get("/api/version")
    async def api_version(request: Request) -> JSONResponse:
        """Return the package version, plus commit and build time once paired.

        The page reads this before pairing, so the route stays public, but
        the commit and build time would tell anyone on the network exactly
        which build is running.
        """
        # Footer build stamp.  Lets the user verify which commit the server
        # is actually serving.
        info = _version_info()
        if not await _has_session(request):
            info = {"version": info["version"]}
        return JSONResponse(info)

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, str | int]:
        """Return process readiness, plus the indexed track count once paired.

        Container health checks call this without a session, so it stays
        public; without one it reports only ``status``.
        """
        if not await _has_session(request):
            return {"status": "ok"}
        return {"status": "ok", "tracks": shared.sim.ntotal}

    def status_page(station: StationRuntime | StoppedStation, *, by_name: bool) -> HTMLResponse:
        """A station's public status page; a stopped station's says it is off air (503)."""
        slug = station.slug
        feed_url = public_feed_path(slug, by_name=by_name)
        stream_path = f"/{slug}.mp3" if by_name else "/stream.mp3"
        m3u_path = stream_path.removesuffix(".mp3") + ".m3u"
        playlist_name = f"{slug}.m3u" if slug != DEFAULT_SLUG else "autodj.m3u"
        if isinstance(station, StoppedStation):
            html = render_off_air_page(
                station.name,
                feed_url=feed_url,
                stream_path=stream_path,
                m3u_path=m3u_path,
                playlist_name=playlist_name,
            )
            status = 503
        else:
            html = render_status_page(
                station.nowplaying.snapshot(),
                stream_mode=station.bridge.stream_mode,
                feed_url=feed_url,
                stream_path=stream_path,
                m3u_path=m3u_path,
                playlist_name=playlist_name,
            )
            status = 200
        return HTMLResponse(
            html,
            status_code=status,
            headers={
                "Cache-Control": "no-cache",
                "Content-Security-Policy": STATUS_PAGE_CSP,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )

    @app.api_route("/status", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def get_status_page() -> HTMLResponse:
        """Serve the default station's public status page, built from its feed."""
        return status_page(stations.default, by_name=False)

    @app.api_route("/stations/{slug}/status", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def get_station_status_page(slug: str) -> HTMLResponse:
        """Serve any station's public status page by name; 503 "off air" while it is stopped."""
        try:
            station = stations.get(slug)
        except KeyError:
            raise HTTPException(status_code=404, detail="Not Found") from None
        return status_page(station, by_name=True)

    # The listener request page (#49), off unless [requests] turns it on.
    install_request_pages(app, stations)
    # The player page and embeddable player (#52), off unless [player_page]
    # turns them on for the station.
    install_player_pages(app, stations)

    # ------------------------------------------------------------------
    # Radio stream (stream mode only; 404 otherwise)
    # ------------------------------------------------------------------

    async def _serve_listener(
        station: StationRuntime | StoppedStation, ext: str, request: Request
    ) -> Response:
        """Serve *station*'s stream in a format (``mp3``, ``aac``, ``opus``) or its playlist (``m3u``).

        ``HEAD`` answers with the same checks and headers but no body and
        without becoming a listener.  404 unless the station streams, and
        for a format that is off.
        """
        if isinstance(station, StoppedStation):
            raise HTTPException(status_code=503, detail="That station is off air")
        sbridge = station.bridge
        access = station.listener_access
        if not sbridge.stream_mode or access is None:
            raise HTTPException(status_code=404, detail="Not Found")
        if ext not in _LISTEN_EXTENSIONS:
            raise HTTPException(status_code=404, detail="Not Found")
        # Never starts an encoder: that happens off the loop, for a real listener.
        if ext != "m3u" and not sbridge.stream_enabled(ext):
            raise HTTPException(status_code=404, detail="Not Found")
        stream_cfg = station.cfg.stream
        try:
            address = normalize_address(request.client.host if request.client else "")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid listener address") from exc
        if stream_cfg.listener_password:
            if listener_auth_limiter.blocked(address):
                raise HTTPException(
                    status_code=429,
                    detail="Too many failed listener logins; try again in one minute",
                    headers={"Retry-After": "60"},
                )
            auth = request.headers.get("authorization", "")
            try:
                scheme, encoded = auth.split(" ", 1)
                user, password = (
                    base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
                )
            except (ValueError, UnicodeError, binascii.Error):
                scheme, user, password = "", "", ""
            valid_user = hmac.compare_digest(
                user.encode("utf-8"), stream_cfg.listener_user.encode("utf-8")
            )
            valid_password = hmac.compare_digest(
                password.encode("utf-8"), stream_cfg.listener_password.encode("utf-8")
            )
            if scheme.lower() != "basic" or not (valid_user and valid_password):
                listener_auth_limiter.failed(address)
                logger.info("Failed listener Basic authentication from %s", address)
                realm = _header_text(stream_cfg.station_name.replace('"', "'").replace("\\", ""))
                raise HTTPException(
                    status_code=401,
                    detail="Listener credentials required",
                    headers={"WWW-Authenticate": f'Basic realm="{realm}"'},
                )
        if await asyncio.to_thread(access.banned, address):
            raise HTTPException(status_code=403, detail="Listener address banned")
        head = request.method == "HEAD"
        headers = {"Cache-Control": "no-store"}
        if ext == "m3u":
            scheme = request.url.scheme
            host = request.headers.get("host", "")
            body = f"{scheme}://{host}{sbridge.stream_path}\n".encode()
            if head:
                headers["Content-Length"] = str(len(body))
                body = b""
            return Response(body, media_type="audio/x-mpegurl", headers=headers)
        fmt = FORMATS[ext]
        # An Ogg (Opus) stream carries no ICY titles; its player is never offered them.
        icy = fmt.icy and request.headers.get("icy-metadata", "").strip() == "1"
        if icy:
            headers.update(
                {
                    "icy-metaint": str(METAINT),
                    "icy-name": _header_text(listener_name(sbridge)),
                    "icy-genre": _header_text(stream_cfg.genre),
                    "icy-url": _header_text(stream_cfg.url),
                    "icy-br": str(
                        stream_cfg.bitrate if ext == "mp3" else sbridge.mounts.bitrate(ext)
                    ),
                }
            )
        if head:
            response = Response(media_type=fmt.media_type, headers=headers)
            del response.headers["content-length"]  # a live stream has no length
            return response
        # Off the loop: an extra format's first listener starts its encoder.
        try:
            stream = await asyncio.to_thread(sbridge.stream_output, ext)
        except (EncoderUnavailableError, OSError) as exc:
            logger.error("The %s stream encoder could not start", ext, exc_info=True)
            raise HTTPException(status_code=503, detail="stream encoder failed") from exc
        if stream is None:
            raise HTTPException(status_code=404, detail="Not Found")
        add_task = asyncio.create_task(
            asyncio.to_thread(
                functools.partial(
                    access.add,
                    stream,
                    address,
                    request.headers.get("user-agent", ""),
                    icy=icy,
                    fmt=ext,
                )
            )
        )
        try:
            # Keep the result available if the request is cancelled after admission.
            ident, listener = await asyncio.shield(add_task)
        except asyncio.CancelledError:
            try:
                ident, _ = await add_task
            except Exception:
                # Admission itself failed, so there is no listener to remove.
                logger.debug("Cancelled listener admission had failed", exc_info=True)
            else:
                access.remove(ident)
            raise
        except (ListenerLimitError, AddressLimitError, StationFullError) as exc:
            raise HTTPException(status_code=503, detail="Stream listener limit reached") from exc
        except BannedAddressError as exc:
            raise HTTPException(status_code=403, detail="Listener address banned") from exc
        except EncoderUnavailableError as exc:
            raise HTTPException(status_code=503, detail="stream encoder failed") from exc
        # Who is listening, for the play log's performance counts.
        client = (
            address,
            request.headers.get("user-agent", ""),
        )
        feed = station.nowplaying
        stats = station.listener_stats
        started = stats.connected(ext) if stats is not None else (0.0, 0.0)
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            access.remove(ident)
            if stats is not None:
                stats.disconnected(ext, started)
            sbridge.listener_disconnected(client)
            feed.refresh()

        try:
            sbridge.listener_connected(client)
            feed.refresh()
            return _ListenerResponse(
                listener.chunks(),
                on_close=release,
                media_type=fmt.media_type,
                headers=headers,
            )
        except BaseException:
            release()  # never leak a listener slot
            raise

    @app.api_route("/stream.{ext}", methods=["GET", "HEAD"])
    async def api_stream_audio(ext: str, request: Request) -> Response:
        """Serve the default station's ``/stream.mp3``, or ``/stream.m3u`` pointing at it.

        ``/stream.aac`` and ``/stream.opus`` while those formats are on; any
        other ``/stream.*`` name is not found.
        """
        return await _serve_listener(stations.default, ext, request)

    @app.api_route("/{station_file:streamfile}", methods=["GET", "HEAD"])
    async def api_station_stream(station_file: str, request: Request) -> Response:
        """Serve ``/<slug>.mp3`` or ``.m3u``: any station's stream by name.

        ``.aac`` and ``.opus`` too, while the station has those formats on.
        404 for a name that is no station or another extension; 503 "off
        air" while the station is stopped.
        """
        slug, dot, ext = station_file.rpartition(".")
        if not dot or ext not in _LISTEN_EXTENSIONS:
            raise HTTPException(status_code=404, detail="Not Found")
        try:
            station = stations.get(slug)
        except KeyError:
            raise HTTPException(status_code=404, detail="Not Found") from None
        if station.stopped:
            raise HTTPException(status_code=503, detail="That station is off air")
        assert isinstance(station, StationRuntime)
        CURRENT_STATION.set(station.slug)
        return await _serve_listener(station, ext, request)

    # ------------------------------------------------------------------
    # Stations: the list, and starting and stopping a station other than
    # the default one
    # ------------------------------------------------------------------

    @app.get("/api/stations")
    async def api_stations() -> dict[str, Any]:
        """List every configured station: name, state, listeners, song and stream address."""
        from autodj import timefmt

        return {"stations": stations.summaries(), "time_format": timefmt.time_format}

    @app.post("/api/stations", status_code=201)
    async def api_station_create(body: CreateStationBody) -> dict[str, Any]:
        """Publish a validated definition and register a stopped station."""
        from autodj.config import MAX_STATIONS, _build_station, _check_station_conflicts
        from autodj.station_creation import CreationConflict, duplicate_table, stage_station
        from autodj.station_store import StationStore, migrate_legacy
        from autodj.stations import StationPaths

        root = cfg
        library = shared.library
        if library is None or not hasattr(library, "catalog") or shared.sim is None:
            raise HTTPException(status_code=503, detail="This server cannot host another station")
        async with station_create_lock:
            if stations._closing:
                raise HTTPException(status_code=409, detail="The server is shutting down")
            if body.slug in stations.slugs():
                raise HTTPException(status_code=409, detail=f"Station {body.slug} already exists")
            if body.source is not None and body.source not in stations.slugs():
                raise HTTPException(status_code=404, detail="The source station was not found")
            if len(root.stations) >= MAX_STATIONS:
                raise HTTPException(
                    status_code=409, detail=f"At most {MAX_STATIONS} other stations are allowed"
                )
            if any(
                body.name.casefold() == name.casefold().strip()
                for _, name in _station_names(stations)
            ):
                raise HTTPException(
                    status_code=409, detail="A station with that display name already exists"
                )
            destination = StationPaths.for_station(root, body.slug).state_dir
            if await asyncio.to_thread(lambda: destination.exists() or destination.is_symlink()):
                raise HTTPException(
                    status_code=409,
                    detail=f"A folder from an earlier station with ID {body.slug} exists; choose another ID or move that folder away",
                )
            if await asyncio.to_thread(library.catalog.has_station_plays, body.slug):
                raise HTTPException(
                    status_code=409, detail=f"Station {body.slug} already has play history"
                )
            try:
                table = await asyncio.to_thread(
                    duplicate_table, root, body.source, body.slug, body.name, body.autostart
                )
                spec = replace(_build_station(body.slug, table, root), defined_in="web")
                _check_station_conflicts(root, (*root.stations, spec))
            except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            clock = None
            if body.copy_clockwheels:
                assert body.source is not None
                clock = await asyncio.to_thread(
                    lambda: library.station_ledger(body.source).get_setting("clock")
                )
            try:
                _, destination, copied = await asyncio.to_thread(
                    stage_station,
                    root,
                    body.slug,
                    body.source,
                    table,
                    copy_clockwheels=body.copy_clockwheels,
                    copy_schedule=body.copy_schedule,
                    copy_carts=body.copy_carts,
                )
            except (OSError, CreationConflict) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            clock_written = False
            try:
                if clock is not None:
                    clock_written = True

                    def save_clock() -> None:
                        paths = StationPaths.for_station(root, body.slug)
                        store = StationStore(paths.state_dir / "station.db", body.slug)
                        try:
                            migrate_legacy(library.catalog, store)
                            store.set_setting("clock", clock)
                        finally:
                            store.close()

                    await asyncio.to_thread(save_clock)
                if stations._closing:
                    raise StationControlError("The server is shutting down")
                extra = _extra_station(root, spec, shared.sim, library, station_timezone)
                stations.add(extra)
                root.stations += (spec,)
            except Exception as exc:
                try:
                    if clock_written:

                        def delete_clock() -> None:
                            paths = StationPaths.for_station(root, body.slug)
                            store = StationStore(paths.state_dir / "station.db", body.slug)
                            try:
                                store.delete_setting("clock")
                            finally:
                                store.close()

                        await asyncio.to_thread(delete_clock)
                finally:
                    await asyncio.to_thread(shutil.rmtree, destination)
                raise HTTPException(
                    status_code=409, detail=f"Could not finish station creation: {exc}"
                ) from exc
            return {
                **stations.get(body.slug).summary(),
                "copied": copied,
                "not_copied": [
                    "queue and play history",
                    "active playback and pending requests",
                    "listener sessions, bans, and statistics",
                    "running jobs and recording state",
                    "relays and passwords",
                    "liner play dates, ads, shows, intros, and profiles",
                    "liners, time-check clips, announcements, and caches",
                    "liner source registration marker",
                    "music files, library records, analysis, song cues, and Stereo Tool preset files are shared",
                    "schedule targets missing from the new station may fail when events run",
                    *(["clockwheels"] if not body.copy_clockwheels else []),
                    *(["schedule"] if not body.copy_schedule else []),
                    *(["carts"] if not body.copy_carts else []),
                ],
            }

    @app.post("/api/stations/{slug}/start")
    async def api_station_start(slug: str) -> dict[str, Any]:
        """Build and start a stopped station; a running one is left as it is."""
        try:
            station = await stations.start(slug)
        except KeyError:
            raise HTTPException(status_code=404, detail="No station has that name") from None
        except StationControlError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return station.summary()

    @app.put("/api/stations/{slug}/autostart")
    async def api_station_autostart(slug: str, body: StationAutostartBody) -> dict[str, Any]:
        """Save whether a station made in the page starts with the server."""
        try:
            await asyncio.to_thread(stations.set_autostart, slug, body.enabled)
        except KeyError:
            raise HTTPException(status_code=404, detail="No station has that name") from None
        except StationControlError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return next(item for item in stations.summaries() if item["slug"] == slug)

    @app.post("/api/stations/{slug}/stop")
    async def api_station_stop(slug: str) -> dict[str, Any]:
        """Stop a station other than the default: listeners, encoders, relays and player."""
        try:
            warnings = await stations.stop(slug)
        except KeyError:
            raise HTTPException(status_code=404, detail="No station has that name") from None
        except StationControlError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # Stopped either way; what went wrong is said beside it.
        return {**stations.get(slug).summary(), "warnings": warnings}

    @app.get("/api/search")
    async def api_search(q: str = "", limit: int = 100) -> dict[str, list]:
        """Search the indexed library by title / artist / album."""
        results = await asyncio.to_thread(shared.search, q, max(1, min(500, int(limit))))
        return {"results": results}

    # ------------------------------------------------------------------
    # Cover art and lyrics
    # ------------------------------------------------------------------

    @app.get("/api/art")
    async def api_art(path: str) -> Response:
        """Return the embedded album art for a track (304 if unchanged)."""
        # Only serve art for tracks that exist in the index — prevents
        # arbitrary file access through the path parameter.
        known = shared.entry_for_path(path) is not None
        if not known:
            raise HTTPException(status_code=404, detail="Track not in index")
        result = await asyncio.to_thread(shared.cover_art_for, path)
        if result is None:
            raise HTTPException(status_code=404, detail="No embedded cover art")
        data, mime = result
        return Response(content=data, media_type=mime)

    @app.get("/api/lyrics")
    async def api_lyrics(path: str) -> dict[str, object]:
        """Return timed lyric lines for one exact indexed track path."""
        authorized_entry = shared.entry_for_path(path)
        if authorized_entry is None:
            raise HTTPException(status_code=404, detail="Track not in index")
        lyrics = await _read_lyrics_owned(path)
        if shared.entry_for_path(path) != authorized_entry:
            raise HTTPException(status_code=409, detail="Track index changed")
        return {"path": path, "lyrics": lyrics}

    # ------------------------------------------------------------------
    # Browser-driven playback — stream audio bytes + advance trigger
    # ------------------------------------------------------------------

    @app.api_route("/api/audio", methods=["GET", "HEAD"])
    async def api_audio(path: str, request: Request) -> Response:
        """Serve indexed audio with byte ranges; ALAC is transcoded to MP3."""
        if shared.entry_for_path(path) is None:
            raise HTTPException(status_code=404, detail="Track not in index")
        audio_path = Path(path)
        try:
            metadata = await asyncio.to_thread(audio_path.stat)
        except OSError:
            raise HTTPException(status_code=404, detail="File not found on disk") from None
        if not stat.S_ISREG(metadata.st_mode):
            raise HTTPException(status_code=404, detail="File not found on disk")
        if await asyncio.to_thread(_is_alac, audio_path):
            transcoded = await _alac_response(audio_path, request.method)
            if transcoded is not None:
                return transcoded
        return FileResponse(audio_path, media_type=_audio_mime(audio_path), stat_result=metadata)

    @app.post("/api/stereo-tool-presets/upload")
    async def api_stereo_tool_preset_upload(file: UploadFile = File(...)) -> dict:
        """Store one size-bounded text preset under the station data folder."""
        from autodj.stereotool_presets import store_preset

        name = file.filename or ""
        try:
            path = await store_preset(cfg.index.shared_dir, name, file)
        except FileExistsError as exc:
            raise HTTPException(
                status_code=409, detail=f"a preset named {name} already exists"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"name": path.name}

    from autodj.stereotool_api import install_download as install_stereo_tool_download

    # Settings, Sound, Download Stereo Tool: shared, into the index folder.
    install_stereo_tool_download(app, lambda: cfg.index.index_dir)

    from autodj.updates_api import install_updates

    # Settings, Updates: the update check and Restart, shared by every station.
    install_updates(app, lambda: cfg.index.index_dir)

    @app.delete("/api/stereo-tool-presets/{name}")
    async def api_stereo_tool_preset_delete(name: str) -> dict:
        """Delete an uploaded preset other than the one currently selected."""
        from autodj.stereotool_presets import PresetInUseError, delete_preset

        # Every station's preset: the running ones' and the stopped ones' saved
        # choice, read off the event loop (web_state.json may be on a NAS).
        in_use = await asyncio.to_thread(stations.stereo_tool_presets)
        try:
            await asyncio.to_thread(
                delete_preset,
                cfg.index.shared_dir,
                name,
                active=[preset for _station, preset in in_use],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PresetInUseError as exc:
            users = [station for station, preset in in_use if preset == exc.args[0]]
            where = f" on {', '.join(users)}" if len(stations.slugs()) > 1 and users else ""
            raise HTTPException(
                status_code=409, detail=f"select another preset{where} before deleting this one"
            ) from exc
        return {"deleted": name}

    # ------------------------------------------------------------------
    # Library tools — index / enrich / prune / stats from the web UI
    # ------------------------------------------------------------------

    def _catalog_library() -> Any:
        if shared.library is None:
            raise HTTPException(status_code=503, detail="Folder library is not attached")
        return shared.library

    @app.get("/api/library/tracks")
    async def api_library_tracks(
        q: str = "",
        kind: Literal["music", "liner"] = "music",
        source_id: str | None = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict:
        library = _catalog_library()
        try:
            return await asyncio.to_thread(library.browse_tracks, kind, source_id, q, offset, limit)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Folder not registered") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/library/folders")
    async def api_library_folders() -> dict:
        return await asyncio.to_thread(_catalog_library().snapshot)

    @app.post("/api/library/folders")
    async def api_library_add_folder(body: LibraryFolderBody) -> dict:
        library = _catalog_library()
        try:
            source = await asyncio.to_thread(
                library.catalog.add_source,
                body.path,
                body.kind,
                body.recursive,
                body.advanced if body.kind == "music" else False,
                analysis=body.analysis,
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        library.start_scan({source["id"]})
        return await asyncio.to_thread(library.snapshot)

    # Browse buttons: the server's folders, for typing a new library folder,
    # and the folders and audio files inside a registered one (path_browse).
    @app.get("/api/library/browse")
    async def api_library_browse(path: Annotated[str, Query(max_length=4096)] = "") -> dict:
        from autodj.path_browse import BrowseError, list_server_folder

        try:
            return await asyncio.to_thread(list_server_folder, path)
        except BrowseError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None

    @app.get("/api/library/folders/{source_id}/browse")
    async def api_library_folder_browse(
        source_id: str,
        path: Annotated[str, Query(max_length=4096)] = "",
        files: bool = False,
    ) -> dict:
        from autodj.path_browse import BrowseError, list_library_folder

        library = _catalog_library()
        sources = await asyncio.to_thread(library.catalog.sources)
        source = next((s for s in sources if s["id"] == source_id), None)
        if source is None:
            raise HTTPException(status_code=404, detail="Folder not registered")
        extensions = {f".{ext.lower().lstrip('.')}" for ext in cfg.library.supported_formats}
        try:
            return await asyncio.to_thread(
                lambda: list_library_folder(
                    source["path"], path, files=files, extensions=extensions
                )
            )
        except BrowseError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None

    @app.patch("/api/library/folders/{source_id}")
    async def api_library_update_folder(source_id: str, body: LibraryFolderUpdateBody) -> dict:
        library = _catalog_library()
        source = next((s for s in library.catalog.sources() if s["id"] == source_id), None)
        if source is None:
            raise HTTPException(status_code=404, detail="Folder not registered")
        recursive = source["recursive"] if body.recursive is None else body.recursive
        advanced = source["advanced"] if body.advanced is None else body.advanced
        await asyncio.to_thread(
            library.catalog.update_source,
            source_id,
            recursive,
            advanced if source["kind"] == "music" else False,
            analysis=body.analysis,
        )
        library.start_scan({source_id})
        return await asyncio.to_thread(library.snapshot)

    @app.delete("/api/library/folders/{source_id}")
    async def api_library_remove_folder(source_id: str) -> dict:
        library = _catalog_library()
        if not await asyncio.to_thread(library.catalog.remove_source, source_id):
            raise HTTPException(status_code=404, detail="Folder not registered")
        await asyncio.to_thread(library.refresh)
        return await asyncio.to_thread(library.snapshot)

    @app.post("/api/library/rescan")
    async def api_library_rescan(body: LibraryRescanBody | None = None) -> dict:
        library = _catalog_library()
        if not library.start_scan(override_tags=body.override_tags if body else False):
            raise HTTPException(status_code=409, detail="Library cleanup is running")
        return await asyncio.to_thread(library.snapshot)

    @app.get("/api/library/auto-rescan")
    async def api_library_auto_rescan() -> dict[str, int]:
        hours, minutes = divmod(_catalog_library().auto_rescan_minutes(), 60)
        return {"hours": hours, "minutes": minutes}

    station_router = StationRouter(app.router)
    install_speech(app, station_router, cfg, stations)

    def _settings_slug(request: Request) -> str:
        slug = request.path_params.get("slug", DEFAULT_SLUG)
        if slug not in stations.slugs():
            raise HTTPException(status_code=404, detail="Station not found")
        return slug

    def _time_settings(slug: str) -> dict[str, Any]:
        from zoneinfo import available_timezones

        from autodj import timefmt

        station_cfg = stations.configuration(slug)
        current = stations.get(slug)
        return {
            "timezone": station_cfg.station.timezone or system_timezone(),
            "running_timezone": getattr(current, "timezone", None),
            "time_format": timefmt.time_format,
            "timezones": sorted(available_timezones()),
        }

    @station_router.get("/time-settings")
    async def api_time_settings(request: Request) -> dict[str, Any]:
        return await asyncio.to_thread(_time_settings, _settings_slug(request))

    @station_router.get("/station-settings")
    async def api_station_settings(request: Request) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(stations.configuration(_settings_slug(request)).station)

    @station_router.get("/public-settings")
    async def api_public_settings(request: Request) -> dict[str, Any]:
        return await asyncio.to_thread(stations.public_settings, _settings_slug(request))

    @app.put("/api/library/auto-rescan")
    async def api_library_set_auto_rescan(body: LibraryAutoRescanBody) -> dict[str, int]:
        library = _catalog_library()
        total = body.hours * 60 + body.minutes
        if 0 < total < AUTO_RESCAN_MIN_MINUTES:
            raise HTTPException(
                status_code=400, detail="Use at least 5 minutes, or 0 to turn it off."
            )
        await asyncio.to_thread(library.set_auto_rescan_minutes, total)
        return {"hours": body.hours, "minutes": body.minutes}

    @app.get("/api/library/music-fade")
    async def api_library_music_fade() -> dict[str, int]:
        return {"drop_db": await asyncio.to_thread(_catalog_library().music_fade_drop_db)}

    @app.put("/api/library/music-fade")
    async def api_library_set_music_fade(body: LibraryMusicFadeBody) -> dict[str, int]:
        library = _catalog_library()
        await asyncio.to_thread(library.set_music_fade_drop_db, body.drop_db)
        return {"drop_db": body.drop_db}

    @app.get("/api/library/advanced-length")
    async def api_library_advanced_length() -> dict[str, int]:
        """The advanced analysis length cutoff in minutes; 0 analyses every length."""
        return {"minutes": await asyncio.to_thread(_catalog_library().advanced_max_minutes)}

    @app.put("/api/library/advanced-length")
    async def api_library_set_advanced_length(body: LibraryAdvancedLengthBody) -> dict[str, int]:
        await asyncio.to_thread(_catalog_library().set_advanced_max_minutes, body.minutes)
        return {"minutes": body.minutes}

    @app.get("/api/library/missing")
    async def api_library_missing_preview() -> dict:
        """Preview records missing after a complete scan of available folders."""
        return await asyncio.to_thread(_catalog_library().missing_removal_preview)

    @app.post("/api/library/remove-missing")
    async def api_library_remove_missing(body: LibraryRemoveMissingBody) -> dict:
        """Remove only the unchanged set shown by a clean-scan preview."""
        library = _catalog_library()
        try:
            removed = await asyncio.to_thread(
                library.remove_missing,
                body.token,
                confirmed=body.confirmed,
                allow_mass=body.allow_mass,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "removed": removed,
            "remaining": (await asyncio.to_thread(library.snapshot))["tracks_ready"],
        }

    def library_station(slug: str) -> str:
        if slug not in app.state.stations.slugs():
            raise HTTPException(status_code=404, detail="Station not found")
        return slug

    @app.get("/api/library/points")
    async def api_library_points(path: str, station: str = DEFAULT_SLUG) -> dict:
        station = library_station(station)
        library = _catalog_library()
        entry = library.entry_for_path(path)
        if entry is None:
            raise HTTPException(status_code=404, detail="Recording not in library")
        return await _track_info(library, path, station)

    async def _track_info(library: Any, path: str, station: str = DEFAULT_SLUG) -> dict:
        """The Track info record of *path*: details, cue points and tag form."""
        record = await asyncio.to_thread(library.catalog.record_for_path, path)
        if record is None:
            raise HTTPException(status_code=404, detail="Recording not in library")
        record["last_played"] = library.station_ledger(station).last_played().get(record["path"], 0)
        record["playable"] = True
        from autodj.processing import loudness_matches

        record["loudness_ready"] = await asyncio.to_thread(
            loudness_matches, record.get("loudness"), Path(path), record["kind"]
        )
        advanced = record["kind"] == "music" and any(
            source["advanced"]
            for source in await asyncio.to_thread(library.catalog.sources_for_path, path, "music")
        )
        has_vector = await asyncio.to_thread(library.catalog.has_vector, path)
        record["analysis"] = "ready" if has_vector else "pending" if advanced else "disabled"
        from autodj.dj_meta import key_label

        record["key_label"] = key_label(
            record["key"],
            record["mode"],
            library.cfg.playback.key_notation,
            prefer_flats=library.cfg.playback.key_prefer_flats,
        )
        from autodj.clock_conditions import genre_text

        # Shown in Track info and the Tags form; the catalog keeps the
        # file's own separators, and an unchanged field is not saved back.
        record["genre"] = genre_text(record.get("genre"))
        record.update(await asyncio.to_thread(tag_form, library, record))
        return record

    install_tag_editor(app, shared, _track_info)

    @app.post("/api/library/points")
    async def api_library_save_points(body: SongPointsBody) -> dict:
        station = library_station(body.station)
        library = _catalog_library()
        entry = library.entry_for_path(body.path)
        if entry is None:
            raise HTTPException(status_code=404, detail="Recording not in library")
        if library.catalog.record_for_path(body.path).get("kind") == "liner":
            allowed = {"start", "segue", "end", "overlap_start"}
            if set(body.points) - allowed:
                raise HTTPException(
                    status_code=400,
                    detail="Liner points support only start, segue, end, and overlap_start",
                )
        elif body.allow_outro is not None:
            raise HTTPException(status_code=400, detail="allow_outro can only be set for liners")
        try:
            if body.allow_outro is None:
                await asyncio.to_thread(library.catalog.set_points, body.path, body.points)
            else:
                await asyncio.to_thread(
                    library.catalog.set_points, body.path, body.points, allow_outro=body.allow_outro
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        record = await asyncio.to_thread(library.catalog.record_for_path, body.path)
        record["last_played"] = library.station_ledger(station).last_played().get(record["path"], 0)
        return record

    @app.post("/api/library/points/rescan")
    async def api_library_points_rescan(body: CueRescanBody) -> dict:
        station = library_station(body.station)
        library = _catalog_library()
        if await asyncio.to_thread(library.entry_for_path, body.path) is None:
            raise HTTPException(status_code=404, detail="Recording not in library")
        try:
            return await asyncio.to_thread(
                app.state.cue_rescan.scan, library.catalog, body.path, station
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Recording not in library") from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=409, detail="Recording is no longer available") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/library/points/rescan/apply")
    async def api_library_points_rescan_apply(body: CueRescanApplyBody) -> dict:
        station = library_station(body.station)
        library = _catalog_library()
        if await asyncio.to_thread(library.entry_for_path, body.path) is None:
            raise HTTPException(status_code=404, detail="Recording not in library")
        cue_rescan = getattr(app.state, "cue_rescan", None)
        if cue_rescan is None:
            raise HTTPException(status_code=409, detail="Scan expired; scan the cues again")
        try:
            await asyncio.to_thread(
                cue_rescan.apply, library.catalog, body.path, station, body.token, body.fields
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Recording not in library") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=409, detail="Recording or lyrics changed; scan again"
            ) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await _track_info(library, body.path, station)

    @app.get("/api/library/job")
    async def api_library_job_status() -> dict:
        """Return the live library job status."""
        from autodj.jobs import get_manager

        return get_manager().snapshot()

    @app.post("/api/library/run")
    async def api_library_run(body: LibraryJobBody) -> dict:
        """Start one of the allowed library CLI jobs."""
        from autodj.jobs import get_manager

        mgr = get_manager()
        # Point the child at the same configuration this server is running
        # on, every time: settings can change under a live server,
        # so reading it at app-creation time would go stale.
        mgr.configure(
            config_path=cfg.config_path,
            index_dir=cfg.index.index_dir,
        )
        # start() may wait up to a second for the previous job's output reader
        # and spawns a process, so keep it off the event loop.
        ok = await asyncio.to_thread(mgr.start, body.name, body.args)
        if not ok:
            raise HTTPException(
                status_code=409,
                detail="Job already running, or subcommand not allowed.",
            )
        return mgr.snapshot()

    @app.post("/api/library/stop")
    async def api_library_stop() -> dict[str, bool]:
        """Cancel the running library job."""
        from autodj.jobs import get_manager

        return {"stopped": get_manager().stop()}

    @app.get("/api/library/stats")
    async def api_library_stats() -> dict:
        """Return summary stats about the loaded SimilarityIndex.

        Single pass over ``entries`` so the cost stays O(N) instead of
        4×O(N).  Negligible at 1 k tracks; measurable at 70 k.
        """
        similarity = shared.sim
        entries = (
            shared.library.entries_snapshot()
            if shared.library is not None
            else similarity.entries_snapshot()
        )
        n = len(entries)
        bpm_count = 0
        bpm_sum = 0.0
        with_genre = 0
        with_key = 0
        with_energy = 0
        for e in entries:
            if e.bpm > 0:
                bpm_count += 1
                bpm_sum += e.bpm
            if e.genre:
                with_genre += 1
            if e.key >= 0 and e.mode >= 0:
                with_key += 1
            if e.energy > 0:
                with_energy += 1
        avg_bpm = round(bpm_sum / bpm_count, 1) if bpm_count else 0.0
        return {
            "track_count": n,
            "tracks_with_bpm": bpm_count,
            "average_bpm": avg_bpm,
            "tracks_with_genre": with_genre,
            "tracks_with_key": with_key,
            "tracks_with_energy": with_energy,
        }

    # ------------------------------------------------------------------
    # Station routes: /api/<route> for the default station, and
    # /api/stations/<slug>/<route> for any station by name
    # ------------------------------------------------------------------

    install_station_routes(station_router)

    # ------------------------------------------------------------------
    # WebSocket broadcast: one socket per station
    # ------------------------------------------------------------------

    async def websocket_endpoint(websocket: WebSocket) -> None:
        """WebSocket: broadcast one station's state updates to a connected browser."""
        request_id = new_request_id()
        route = STATION_PREFIX + "/ws" if "slug" in websocket.path_params else "/ws"
        request_policy = policy
        host_values = _raw_header_values(websocket.scope, b"host")
        origin_values = _raw_header_values(websocket.scope, b"origin")

        async def reject(status: int, close_code: int) -> None:
            """Audit the rejection and close the handshake with *close_code*."""
            emit_audit(
                request_id,
                route,
                "rejected",
                method="WS",
                route=route,
                status=status,
                level=logging.WARNING,
            )
            await websocket.close(code=close_code)

        if len(host_values) != 1 or not request_policy.host_allowed(host_values[0]):
            await reject(403, 4403)
            return
        if len(origin_values) != 1 or not request_policy.origin_allowed(origin_values[0]):
            await reject(403, 4403)
            return
        session_cookie = websocket.cookies.get(COOKIE_NAME)
        if request_policy.authentication_required and not request_policy.browser_authenticated(
            websocket.scope
        ):
            await reject(401, 4401)
            return
        try:
            station = resolve_station(websocket)
        except HTTPException as exc:
            # 4404: no station has that name; 4409: the station is stopped.
            await reject(exc.status_code, 4000 + exc.status_code)
            return

        accepted_at = request_policy.monotonic_now()
        await websocket.accept()

        def session_is_valid() -> bool:
            """Return whether the accepted WebSocket session remains valid."""
            return request_policy.socket_session_is_valid(session_cookie, accepted_at)

        client = _WebSocketClient(
            websocket,
            request_id=request_id,
            session_is_valid=session_is_valid,
            handler_task=asyncio.current_task(),
            route=route,
        )
        client_host = getattr(getattr(websocket, "client", None), "host", "unknown")
        async with station.ws_lock:
            # A station that began stopping after it was looked up may have
            # closed its pages' sockets already; this one would never close.
            stopped = station.stopped
            if not stopped:
                station.ws_clients.add(client)
            client_count = len(station.ws_clients)
        if stopped:
            await reject(409, 4409)
            return
        logger.info(
            "WebSocket connected: %s (%d active client%s)",
            client_host,
            client_count,
            "" if client_count == 1 else "s",
        )
        emit_audit(
            request_id,
            route,
            "connected",
            method="WS",
            route=route,
            status=101,
        )
        disconnect_code = 1000
        try:
            # The socket only pushes state; frames the page sends are ignored.
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    disconnect_code = message.get("code", 1000)
                    break
        except asyncio.CancelledError:
            if client.failure_code is None:
                raise
            disconnect_code = client.failure_code
        finally:
            async with station.ws_lock:
                station.ws_clients.discard(client)
                remaining = len(station.ws_clients)
            logger.info(
                "WebSocket disconnected: %s (%d active client%s remain)",
                client_host,
                remaining,
                "" if remaining == 1 else "s",
            )
            emit_audit(
                request_id,
                route,
                "disconnected",
                method="WS",
                route=route,
                status=disconnect_code,
            )

    app.add_api_websocket_route("/ws", websocket_endpoint)
    app.add_api_websocket_route(STATION_PREFIX + "/ws", websocket_endpoint)
    install_live_routes(
        app,
        policy,
        allow_lan_plain_http=bool(getattr(cfg.server, "live_plain_http_lan", False)),
    )

    def prepare(station: StationRuntime) -> None:
        """Wire a station that is about to start to this app."""
        station.bridge.public_state_changed = station.public_state_changed
        station.bridge.track_started_callback = station.track_started
        if station.slug == DEFAULT_SLUG:
            app.state.listener_access = station.listener_access
        _share_station(stations, station)
        _equip_station(station)

    async def disconnect(station: StationRuntime) -> None:
        """Close the sockets of a stopped station's pages: 4409, the station is stopped."""
        async with station.ws_lock:
            clients = list(station.ws_clients)
        for client in clients:
            await _close_and_cancel_websocket(client, 1.0, 4409, "station stopped")

    def build_content(extra: ExtraStation) -> Any:
        from autodj.config import station_config
        from autodj.station_content import StationContent

        saved_cfg = station_config(extra.paths._cfg, cfg)
        return StationContent(
            saved_cfg, StationPaths.for_station(saved_cfg, extra.slug), shared.library, shared.sim
        )

    stations.build_content = build_content
    stations.prepare = prepare
    stations.disconnect = disconnect
    return app


def _station_names(stations: StationRegistry) -> list[tuple[str, str]]:
    """``(slug, name)`` of every configured station, the default first."""
    first = stations.default.slug
    names = []
    for slug in (first, *sorted(stations.slugs() - {first})):
        try:
            name = str(stations.get(slug).name)
        except Exception:  # a station whose configuration cannot say
            name = slug
        names.append((slug, name))
    return names


def _break_upload_cap(stations: StationRegistry) -> int:
    """The request-body cap of a break upload: the largest any running station takes."""
    caps = [
        getattr(getattr(station.cfg, "breaks", None), "max_upload_bytes", None)
        for station in stations.all()
    ]
    largest = max(
        (cap for cap in caps if isinstance(cap, int)),
        default=BreaksConfig().max_upload_bytes,
    )
    return largest + BREAK_MULTIPART_OVERHEAD


def _voice_upload_cap(stations: StationRegistry) -> int:
    """The body cap of a browser voice recording, independent of announcer settings."""
    return VOICE_MAX_UPLOAD_BYTES + BREAK_MULTIPART_OVERHEAD


def _ad_upload_cap(stations: StationRegistry) -> int:
    """The request-body cap of a spot upload: the largest any running station takes."""
    caps = [
        getattr(getattr(station.cfg, "ads", None), "max_upload_bytes", None)
        for station in stations.all()
    ]
    largest = max(
        (cap for cap in caps if isinstance(cap, int)),
        default=AdsConfig().max_upload_bytes,
    )
    return largest + BREAK_MULTIPART_OVERHEAD


def _share_station(stations: StationRegistry, station: StationRuntime) -> None:
    """Give *station* what it shares with the other stations."""
    station.relay_lock = stations.relay_lock
    if len(stations.slugs()) > 1:
        station.station_list = stations.summaries


def _connection_limit(cfg: Any) -> int:
    """Connections the server takes at once: every station's listeners, and room besides."""
    configs = (cfg, *(spec.cfg for spec in cfg.stations))
    listeners = (getattr(getattr(c, "stream", None), "max_listeners", 0) for c in configs)
    return _CONNECTION_ROOM + sum(n for n in listeners if isinstance(n, int))


def _station_broadcast(station: StationRuntime) -> Callable[[], Coroutine[Any, Any, None]]:
    """The loop that pushes *station*'s state to its WebSocket clients once a second."""

    async def broadcast() -> None:  # pragma: no cover — long-running task
        failures = _BroadcastFailureLog()
        bridge = station.bridge
        listener_access = station.listener_access
        while True:
            await asyncio.sleep(1)
            if bridge.stream_mode and listener_access is not None:
                # One revision per tick covers churn, encoder resets and expired bans.
                await asyncio.to_thread(listener_access.listeners)
                await asyncio.to_thread(listener_access.prune_expired)
            if bridge.stream_mode and station.listener_stats is not None:
                # A new day's peak, old days pruned, the counts saved each minute.
                await asyncio.to_thread(station.listener_stats.tick)
            recorder = station.recorder
            await _broadcast_state_once(
                bridge,
                station.ws_clients,
                station.ws_lock,
                failures,
                listener_access.revision if listener_access is not None else None,
                recorder.status if recorder is not None else None,
                station.station_list,
                station.live_state,
            )

    return broadcast


def _equip_station(station: StationRuntime) -> None:
    """Give *station* its WebSocket push, cart wall, scheduler and time checks."""

    async def publish(message: dict[str, Any]) -> None:
        """Send a schedule or cart event to this station's pages only."""
        await _broadcast_and_prune(station.ws_clients, station.ws_lock, json.dumps(message))

    station.broadcast = _station_broadcast(station)
    equip_station(station, publish)


# ---------------------------------------------------------------------------
# serve() — wires everything together
# ---------------------------------------------------------------------------


def _attach_master(player: Any, cfg: AutoDJConfig) -> None:
    """Put the mastering process (Smooth or Stereo Tool) on *player*'s mix bus."""
    from autodj import stereotool_setup
    from autodj.mastering import ContinuousMaster
    from autodj.stations import StationPaths
    from autodj.stereotool_presets import preset_path

    def processing_mode() -> str:
        return getattr(player, "_active_processing_mode", cfg.playback.loudness_processing) or "off"

    paths = getattr(player, "station_paths", None)
    if not isinstance(paths, StationPaths):
        paths = StationPaths.default(cfg)

    player._continuous_master = ContinuousMaster(
        mode=processing_mode,
        stereotool_path=lambda: stereotool_setup.library_path(cfg.index.index_dir),
        preset=lambda: preset_path(
            cfg.index.shared_dir, cfg.playback.stereotool_preset, require_existing=False
        ),
        input_trim_db=lambda: cfg.playback.stereotool_input_trim_db,
        agc_drive_db=lambda: cfg.playback.stereotool_agc_drive_db,
        stereotool_license=lambda: stereotool_setup.license_key(
            index_dir=cfg.index.index_dir,
            state_dir=paths.state_dir,
        ),
        setup_generation=lambda: stereotool_setup.generation(paths.state_dir),
        library_present=stereotool_setup.library_present,
    )
    player.bus.set_master(
        player._continuous_master,
        enabled=lambda: processing_mode() in {"smooth", "stereotool"},
    )


def _extra_station(
    root: AutoDJConfig,
    spec: StationSpec,
    sim: SimilarityIndex,
    library: LibraryManager,
    timezone: str,
    *,
    stream_mode: bool | None = None,
    no_playback: bool | None = None,
    seed_entry: IndexEntry | None = None,
) -> ExtraStation:
    """Build any station afresh, with its own player and optional audio outputs.

    Each start builds everything afresh from the spec and the station's
    saved settings, so a stopped station holds nothing in memory.
    """
    slug = spec.slug

    def build(registry: StationRegistry) -> StationRuntime:
        from autodj.config import station_config
        from autodj.player import Player

        nonlocal seed_entry
        cfg = station_config(spec.cfg, root)
        streams = cfg.station.output == "stream" if stream_mode is None else stream_mode
        speakers = (
            cfg.station.output == "server" or cfg.station.server_audio_too
            if no_playback is None
            else not no_playback
        )
        if speakers and _missing_audio_modules():
            raise RuntimeError("Server audio requires soundfile and sounddevice.")
        if streams or speakers:
            import shutil

            if shutil.which("ffmpeg") is None:
                raise RuntimeError("Server-mixed audio requires ffmpeg on PATH.")
        first_track = seed_entry
        paths = StationPaths.for_station(cfg, slug)
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        # Its liner folder joins the library's liner sources the first time.
        library.seed_station_liners(slug, paths.liners)
        player = Player(
            cfg,
            sim,
            dry_run=not speakers and not streams,
            stream_mode=streams,
            server_audio_too=streams and speakers,
        )
        player._library = library
        ledger = library.station_ledger(slug)
        player._ledger = ledger
        player.station_paths = paths
        bridge = PlayerBridge(
            player=player,
            sim=sim,
            library=library,
            ledger=ledger,
            paths=paths,
            stream_path="/stream.mp3" if slug == DEFAULT_SLUG else f"/{slug}.mp3",
        )
        player.on_track_started = bridge.on_track_started
        bridge.load_persistent_state()
        player.configure_clock()
        # The mastering process and the player thread start from here; a
        # failure after that stops them again before it is raised.
        player_thread: threading.Thread | None = None
        try:
            if player.bus is not None:
                _attach_master(player, cfg)
            if cfg.playback.import_external_cues:
                station_thread(target=player.load_library_cues, name="autodj-library-cues").start()
            player_thread = station_thread(
                target=player.run, kwargs={"seed_entry": first_track}, name="autodj-player"
            )
            player_thread.start()
            runtime = StationRuntime(
                bridge,
                timezone=cfg.station.timezone or system_timezone(),
                player_thread=player_thread,
                stream_mode=streams,
                server_audio=speakers and not streams,
                first_track=first_track if streams else None,
                slug=slug,
                paths=paths,
            )
            if not speakers and not streams:
                bridge.record_seed(first_track)
            runtime.defined_in = spec.defined_in
            seed_entry = None
            return runtime
        except BaseException:
            thread = player_thread
            _abandon_player(
                player,
                thread,
                registry.stop_timeout_s,
                on_stuck=lambda: registry.hold_until_player_ends(
                    AbandonedPlayer(slug, spec.name, player, thread, cfg)  # type: ignore[arg-type]
                ),
            )
            raise

    return ExtraStation(
        slug=slug,
        name=spec.name,
        autostart=spec.autostart,
        paths=StationPaths.for_station(spec.cfg, slug),
        preset=spec.cfg.playback.stereotool_preset,
        build=build,
        requests_enabled=spec.cfg.requests.enabled,
        player_page=spec.cfg.player_page,
        defined_in=spec.defined_in,
    )


def _abandon_player(
    player: Any,
    player_thread: threading.Thread | None,
    timeout_s: float,
    on_stuck: Callable[[], None] | None = None,
) -> None:
    """Stop a player whose station failed to build: its thread, then its master.

    A player still running after *timeout_s* may still write to its
    master, so both are then left alone, logged, and handed to
    *on_stuck* (the registry holds the station, and its Stereo Tool,
    until the player ends).  Nothing here raises; the build's own error
    is the one reported.
    """
    try:
        player._state.should_stop = True
        player._skip_event.set()
        if player_thread is not None and player_thread.is_alive():
            player_thread.join(timeout_s)
    except Exception:
        logger.exception("A station that failed to start: stopping its player failed")
    if player_thread is not None and player_thread.is_alive():
        logger.warning(
            "A station that failed to start: its player did not finish within %.0f seconds; "
            "its player and mastering process are left running",
            timeout_s,
        )
        if on_stuck is not None:
            try:
                on_stuck()
            except Exception:
                logger.exception("A station that failed to start: holding its player failed")
        return
    master = getattr(player, "_continuous_master", None)
    gone = True
    if master is not None:
        try:
            gone = master.close() is not False
        except Exception:
            logger.exception("A station that failed to start: closing its master failed")
            gone = False
    if not gone and on_stuck is not None:
        # Its mastering process outlived the close: held, with Stereo Tool,
        # until it has ended.
        try:
            on_stuck()
        except Exception:
            logger.exception("A station that failed to start: holding its master failed")


def _missing_audio_modules() -> list[str]:
    """Return the sound-card libraries (soundfile, sounddevice) not installed."""
    import importlib.util

    missing: list[str] = []
    for name in ("soundfile", "sounddevice"):
        try:
            if importlib.util.find_spec(name) is None:  # pragma: no cover — present in CI
                missing.append(name)
        except (ImportError, ValueError):
            # ValueError fires when the name is mocked / partially
            # installed (e.g. during tests).  Treat as available.
            pass
    return missing


def _apply_time_settings(cfg: AutoDJConfig, library: Any) -> None:
    """Use the time zone and time format saved in Settings over config.toml (#282, #286)."""
    from autodj import timefmt

    catalog = getattr(library, "catalog", None)
    if catalog is None:
        return
    timefmt.time_format = "12" if catalog.get_setting("time_format") == "12" else "24"


def serve(
    cfg: AutoDJConfig,
    sim: SimilarityIndex,
    seed_entry: IndexEntry | None,
    host: str | None = None,
    port: int | None = None,
    no_playback: bool | None = None,
    stream: bool | None = None,
    lan_configured_hosts: list[str] | None = None,
    library: LibraryManager | None = None,
) -> int | None:
    """Start the Player thread and the FastAPI/uvicorn web server.

    This function blocks until uvicorn is stopped (Ctrl+C), and returns the
    exit status Settings, Updates asked for (RESTART_EXIT_CODE for Restart,
    UPDATE_EXIT_CODE for Update), or ``None`` when nothing was asked or a
    stop signal won.

    Args:
        cfg: Full :class:`~autodj.config.AutoDJConfig` instance.
        sim: Loaded :class:`~autodj.similarity.SimilarityIndex`.
        seed_entry: Starting :class:`~autodj.indexer.IndexEntry`, or ``None``
            for a random track.
        host: Interface to bind uvicorn to.
        port: Port to bind uvicorn to.
        no_playback: Skip server-side audio playback (browser drives audio).
            With *stream*, ``False`` also plays the mix on the sound card.
        stream: Serve the live mix as an MP3 radio stream: the server mixes,
            a station starts a set on the first listener, and the page is a
            remote.  *seed_entry* starts the first set; later sets start
            from the queue or a shuffle pick.
        lan_configured_hosts: In LAN mode, the allowed hosts configured before
            detection was merged in; inside a container these are the only
            addresses printed, because container addresses are unreachable.

    HTTPS is on when ``cfg.server.ssl_certfile`` and ``ssl_keyfile`` are set;
    a :class:`~autodj.tls.CertificateReloader` then loads renewed files into
    the running server.
    """
    from dataclasses import replace

    import faiss

    from autodj.config import StationSpec, validate_server_exposure
    from autodj.stereo import configure_spill_dir

    configure_spill_dir(cfg.index.index_dir)

    # One exact search per pick: OpenMP's thread pool makes it slower (27 ms
    # on one thread against 41 ms on four, measured on 77k tracks) and
    # competes with the mix bus on a small NAS.  Results are the same.
    faiss.omp_set_num_threads(1)

    host = cfg.server.host if host is None else host
    port = cfg.server.port if port is None else port
    staged_server = replace(cfg.server, host=host, port=port)
    validate_server_exposure(staged_server)
    ssl_certfile = staged_server.ssl_certfile
    ssl_keyfile = staged_server.ssl_keyfile
    secure_cookie = ssl_certfile is not None
    advertised_origin = _advertised_server_origin(
        SecurityPolicy(staged_server, secure_cookie=secure_cookie)
    )
    cfg.server = staged_server
    host = staged_server.host
    port = staged_server.port

    import uvicorn

    from autodj.library import LibraryManager

    if library is None:
        library = LibraryManager(cfg, sim)
    _apply_time_settings(cfg, library)
    timezone = cfg.station.timezone or system_timezone()
    default = cfg.default_station or StationSpec(slug=DEFAULT_SLUG, autostart=True, cfg=cfg)
    default_station = _extra_station(
        cfg,
        default,
        sim,
        library,
        timezone,
        stream_mode=stream,
        no_playback=no_playback,
        seed_entry=seed_entry,
    )
    extra_stations = [_extra_station(cfg, spec, sim, library, timezone) for spec in cfg.stations]
    app = create_app(
        services=LibraryServices(cfg, sim, library),
        default_station=default_station,
        secure_cookie=secure_cookie,
        extra_stations=extra_stations,
    )

    startup_policy: SecurityPolicy = app.state.security_policy
    startup_registry: DeviceRegistry | None = app.state.device_registry
    # Starting with no paired browser counts as asking for a code; once one
    # is paired, codes come only from `autodj devices pairing-code` or a
    # paired browser, and none is logged.
    pairing: tuple[str, int] | None = None
    if (
        startup_policy.pairing_required
        and startup_registry is not None
        and not startup_registry.has_active_devices()
    ):
        code, valid_for, _next_code_in = app.state.request_pairing_code()
        pairing = (code, valid_for)
    if cfg.server.lan:
        in_container = running_in_container()
        urls = lan_urls(
            cfg.server,
            tls=secure_cookie,
            in_container=in_container,
            configured_hosts=lan_configured_hosts or (),
        )
        # The log file gets the banner without the code: see LOG_FILE_TEXT.
        logger.info(
            "%s",
            format_lan_banner(
                urls,
                pairing,
                in_container=in_container,
                code_on_request=startup_policy.pairing_required,
            ),
            extra={
                LOG_FILE_TEXT: format_lan_banner(
                    urls,
                    None,
                    in_container=in_container,
                    code_on_request=startup_policy.pairing_required,
                )
            },
        )
    elif pairing is not None:
        logger.info(
            "Pair this browser within five minutes using code: %s",
            pairing[0],
            extra={LOG_FILE_TEXT: "A pairing code was shown on the console."},
        )
    elif startup_policy.pairing_required:
        logger.info("%s", PAIRING_ON_REQUEST_HINT)

    if stream:
        audio_mode = "radio stream" + (" + server-audio" if not no_playback else "")
        logger.info("Stream mode: copy the stream link from Settings, Stream.")
    else:
        audio_mode = "server-audio" if not no_playback else "browser-driven"
    for extra in extra_stations:
        logger.info(
            "Station %s (%s): %s/%s.mp3, status page %s/stations/%s/status%s",
            extra.slug,
            extra.name,
            advertised_origin,
            extra.slug,
            advertised_origin,
            extra.slug,
            "" if extra.autostart else " (stopped until started)",
        )
    logger.info(
        "AutoDJ server ready: %s  (%s, %d indexed tracks, seed=%s)",
        advertised_origin,
        audio_mode,
        sim.ntotal,
        getattr(seed_entry, "display_name", "random"),
    )
    logger.info(
        "Tip: pass `-v` (autodj -v serve) for debug logs.  "
        "Browser-side debug: append ?debug=1 to the URL or set "
        "localStorage.autodjDebug='1'.",
    )

    # ssl_certfile / ssl_keyfile flip uvicorn into HTTPS mode, which is
    # required by browsers to expose AudioWorklet on non-localhost hosts.
    # Without HTTPS the gate stutter / freeze / glitch / bitcrusher
    # worklets are skipped and those transitions play a plain crossfade.
    uvicorn_kwargs: dict = {
        "host": host,
        "port": port,
        "log_level": "warning",
        # No handlers of uvicorn's own: its warnings and errors go through the
        # root logger, so they reach the log file once, in AutoDJ's format.
        # Access lines are not logged at all.
        "log_config": None,
        "access_log": False,
    }
    # Uvicorn rewrites request.client and scheme only for configured proxy
    # peers, in every mode: the listener limits, the pairing lockout and the
    # request page's per-address limits all key on the client address.
    # Without [stream] trusted_proxies, an existing FORWARDED_ALLOW_IPS is
    # honoured; without either, only a proxy on this machine.
    uvicorn_kwargs["proxy_headers"] = True
    # Open connections and requests the server takes at once; past it new
    # ones get 503.  Room for every station's stream listeners, plus the
    # pages, their sockets, live DJs and talkers (#48).
    uvicorn_kwargs["limit_concurrency"] = _connection_limit(cfg)
    allow, proxy_warnings = trusted_proxy_setting(cfg.server.trusted_proxies, os.environ)
    uvicorn_kwargs["forwarded_allow_ips"] = allow
    for warning in proxy_warnings:
        logger.warning("%s", warning)
    if stream or extra_stations:
        # HTTP/1.0 radio players need close-delimited audio, not chunked
        # framing; and live DJs' source clients send it (#48).
        from autodj.live_protocol import IcecastSourceH11Protocol

        uvicorn_kwargs["http"] = IcecastSourceH11Protocol
        uvicorn_kwargs["timeout_graceful_shutdown"] = _STREAM_SHUTDOWN_GRACE_SECONDS
    reloaders: list[CertificateReloader] = []
    if ssl_certfile is not None and ssl_keyfile is not None:
        certfile, keyfile = ssl_certfile, ssl_keyfile

        def _watched_ssl_context(
            _config: object, default_factory: Callable[[], ssl.SSLContext]
        ) -> ssl.SSLContext:
            """Build uvicorn's context and reload renewed certificate files into it."""
            context = default_factory()
            reloader = CertificateReloader(context, certfile, keyfile)
            reloader.start()
            reloaders.append(reloader)
            return context

        uvicorn_kwargs["ssl_certfile"] = certfile
        uvicorn_kwargs["ssl_keyfile"] = keyfile
        uvicorn_kwargs["ssl_context_factory"] = _watched_ssl_context
    # uvicorn normally swallows SIGINT and exits cleanly via the
    # FastAPI lifespan, but a second Ctrl+C (or a SIGINT received
    # mid-asyncio-teardown on Windows) can re-raise.  Suppress so the
    # CLI sees a clean return; the lifespan already logged
    # "Server stopped cleanly."
    #
    # uvicorn.Server rather than uvicorn.run: Settings, Updates' Restart
    # (autodj.updates_api) stops this server through app.state.server_exit.
    server = uvicorn.Server(uvicorn.Config(app, **uvicorn_kwargs))

    def _server_exit() -> None:
        server.should_exit = True

    app.state.server_exit = _server_exit
    from autodj.update_contract import health_for

    # Where the checkout's launcher asks a new version whether it is up,
    # after Update (autodj.checkout_update).
    app.state.health_probe = health_for(
        host, port, https=secure_cookie, host_allowed=startup_policy.host_allowed
    )
    interrupted = False
    try:
        server.run()
    except KeyboardInterrupt:
        interrupted = True
    finally:
        for reloader in reloaders:
            reloader.stop()
    # uvicorn records each stop signal it handled; Ctrl+C or SIGTERM during a
    # restart's shutdown means stop, not restart.
    captured = getattr(server, "_captured_signals", None)
    if isinstance(captured, list) and captured:
        interrupted = True
    if not server.started:
        # What uvicorn.run does when the server could not start (port in use).
        sys.exit(3)
    if not getattr(app.state, "restart_requested", False) or interrupted:
        return None
    from autodj.updates import RESTART_EXIT_CODE

    status = getattr(app.state, "exit_status", None)
    return status if isinstance(status, int) else RESTART_EXIT_CODE
