"""Routes for remote analysis workers and for managing machine tokens.

``/api/worker/...`` is for ``autodj worker`` only: the security middleware
lets a request in only with an ``Authorization: Bearer`` machine token that
has the :data:`~autodj.machine_tokens.SCOPE_ANALYSIS_WORKER` scope, over
HTTPS or from this machine, and puts the token in ``request.state``.  A
worker can claim songs, read the audio of the songs it claimed, post their
analysis, report a failure, and keep its claims alive; nothing else.

``/api/machine-tokens`` and ``/api/analysis/workers`` are ordinary browser
routes behind pairing, for Settings, Analysis workers.  There an operator
pauses a worker or sets its batch size and idle release (#554); the worker
reads them from its heartbeat and claim replies (``worker_manage_v1``), and
a paused worker is handed no songs whatever its version.  The server never
sends a worker anything to run.  Settings also shows the latest time the
server refused each worker token, and why, until that worker connects again.

``POST /api/machine-tokens/pairing-code`` makes a short-lived, single-use
code; ``autodj worker --pair`` trades it at :data:`WORKER_PAIR_PATH`, over
HTTPS and without a token, for a new analysis-worker token.

A result is stored only when it was made with this server's model, model
weights (the model's snapshot commit, never the branch name) and
:data:`~autodj.indexer.ANALYSIS_VERSION`, has exactly
:data:`~autodj.indexer.FEATURE_DIM` finite values of unit length, and the
song's pending scan identity (or catalog record for vector-only work) and
file still have the claimed size and fingerprint. It goes into the live catalog,
whose change counters make the
server load it within a couple of seconds.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import math
import secrets
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from autodj.analysis_queue import WORKER_LEASE_SECONDS, Lease
from autodj.indexer import FEATURE_DIM, file_fingerprint
from autodj.machine_tokens import (
    KNOWN_SCOPES,
    MAX_IDLE_RELEASE_MINUTES,
    MAX_WORKER_BATCH,
    SCOPE_ANALYSIS_WORKER,
    STATION_SCOPES,
    MachineToken,
    MachineTokenStore,
    WorkerSettings,
    validate_name,
)
from autodj.scan_analysis import SCAN_ANALYSIS_VERSION, ScanResult
from autodj.security import (
    PAIRING_CODE_WINDOW_SECONDS,
    PAIRING_MAX_FAILURES_PER_CLIENT,
    PAIRING_MAX_FAILURES_PER_WINDOW,
    audit_as_routine,
    peer_address,
)
from autodj.worker import WORKER_PAIR_PATH, identity_mismatch

if TYPE_CHECKING:
    from autodj.library import LibraryManager

logger = logging.getLogger(__name__)

#: Path prefix of the worker routes, and the token scope they need.
WORKER_PREFIX = "/api/worker/"
MACHINE_SCOPES = {WORKER_PREFIX: SCOPE_ANALYSIS_WORKER}
#: How long a worker pairing code works: as long as a browser pairing code can.
WORKER_PAIRING_SECONDS = 2 * PAIRING_CODE_WINDOW_SECONDS
#: Most songs one claim hands out.
MAX_BATCH = MAX_WORKER_BATCH
#: How often a worker should report in; a lease lasts twenty times this.
HEARTBEAT_SECONDS = 30
#: Request-body caps. Combined results also carry bounded beat grids and cues.
RESULT_BODY_MAX_BYTES = 1024 * 1024
SMALL_BODY_MAX_BYTES = 8 * 1024
#: Requests one token may make per minute; a fast GPU needs about three a song.
RATE_LIMIT_PER_MINUTE = 600
_VECTOR_BYTES = FEATURE_DIM * 4
_NO_STORE = {"Cache-Control": "no-store"}
_MAX_SCAN_MISMATCHES = 1024

LeaseId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
_LeaseQuery = Annotated[str, Query(pattern=r"^[0-9a-f]{32}$")]
_Device = Annotated[str, Field(max_length=120)]
_Finite = Annotated[float, Field(strict=True, allow_inf_nan=False)]


class WorkerIdentity(BaseModel):
    """What a worker analyses with, as far as it knows yet (no weights before download)."""

    model_config = ConfigDict(extra="forbid")
    model: str | None = Field(default=None, max_length=200)
    model_commit: str | None = Field(default=None, max_length=80)
    analysis_version: str | None = Field(default=None, max_length=100)
    dimension: StrictInt | None = None


class _ManagedBody(BaseModel):
    """Fields a worker sends once the server has said ``worker_manage_v1``."""

    worker_manage_v1: StrictBool = False
    # The settings revision the worker has applied.
    settings_revision: StrictInt | None = Field(default=None, ge=0)
    identity: WorkerIdentity | None = None


class ClaimBody(_ManagedBody):
    """Ask for up to *batch* songs to analyse."""

    model_config = ConfigDict(extra="forbid")
    batch: StrictInt = Field(default=1, ge=1, le=MAX_BATCH)
    device: _Device = ""
    scan_decode_v1: StrictBool = False
    scan_analysis_version: str = Field(default="", max_length=40)


class HeartbeatBody(_ManagedBody):
    """A worker reporting its claims, or starting a new process with this token."""

    model_config = ConfigDict(extra="forbid")
    device: _Device = ""
    scan_decode_v1: StrictBool = False
    scan_analysis_version: str = Field(default="", max_length=40)
    leases: list[LeaseId] | None = Field(default=None, max_length=MAX_BATCH * 4)
    connect: StrictBool = False


class ReleaseBody(BaseModel):
    """Songs a stopping worker gives back unanalysed."""

    model_config = ConfigDict(extra="forbid")
    leases: list[LeaseId] = Field(max_length=MAX_BATCH * 4)


class FailedBody(BaseModel):
    """A song the worker could not analyse."""

    model_config = ConfigDict(extra="forbid")
    lease: LeaseId
    reason: str = Field(default="", max_length=300)


class ResultBody(BaseModel):
    """One song's analysis, as ``autodj index`` would have stored it."""

    model_config = ConfigDict(extra="forbid")
    lease: LeaseId
    model: str = Field(max_length=200)
    model_commit: str = Field(max_length=80)
    analysis_version: str = Field(max_length=100)
    dimension: StrictInt
    size: StrictInt = Field(ge=1)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Little-endian float32, base64: exact, and a third the size of JSON numbers.
    vector: str | None = Field(max_length=(_VECTOR_BYTES + 2) // 3 * 4)
    energy: _Finite = Field(ge=0)
    key: StrictInt = Field(ge=-1, le=11)
    mode: StrictInt = Field(ge=-1, le=1)
    bpm: _Finite = Field(ge=0, le=1000)
    tempo_confidence: _Finite = Field(ge=0, le=1)
    scan: ScanResult | None = None
    preparation: ScanResult | None = None


class TokenCreateBody(BaseModel):
    """A new machine token from Settings."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=200)
    scopes: list[str] = Field(min_length=1, max_length=len(KNOWN_SCOPES))
    # The station an announcer token works for.
    station: str | None = Field(default=None, max_length=40)


class WorkerSettingsBody(BaseModel):
    """One worker's settings from Settings; ``None`` leaves a value to the worker."""

    model_config = ConfigDict(extra="forbid")
    paused: StrictBool
    batch: StrictInt | None = Field(ge=1, le=MAX_WORKER_BATCH)
    idle_release_minutes: StrictInt | None = Field(ge=1, le=MAX_IDLE_RELEASE_MINUTES)


class PairingCodeBody(BaseModel):
    """Ask for a worker pairing code; *name* names the token it makes."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="", max_length=200)


class WorkerPairBody(BaseModel):
    """A worker trading a pairing code; *name* is its host name, used if Settings gave none."""

    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^[0-9]{8}$")
    name: str = Field(default="", max_length=200)


@dataclass(frozen=True)
class PairingOutcome:
    """What one pairing attempt came to."""

    paired: bool
    name: str | None = None
    retry_after: int | None = None


class WorkerPairing:
    """The one open worker pairing code, kept in memory: single use, short-lived.

    Wrong codes follow the browser pairing's limits: one client address that
    reaches :data:`~autodj.security.PAIRING_MAX_FAILURES_PER_CLIENT` is locked
    out until the code ends, and once all clients together reach
    :data:`~autodj.security.PAIRING_MAX_FAILURES_PER_WINDOW` the code is
    burned.  Nothing is counted while no code is open, so the counts stay
    bounded.  A new code replaces the old one and its counts.
    """

    def __init__(
        self,
        seconds: float = WORKER_PAIRING_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._seconds = seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._code: str | None = None
        self._name: str | None = None
        self._until = 0.0
        self._failures = 0
        self._clients: dict[str, int] = {}

    def open(self, name: str | None) -> tuple[str, int]:
        """A new code for a token named *name* (or the worker's host name), and its life."""
        code = f"{secrets.randbelow(100_000_000):08d}"
        with self._lock:
            self._code = code
            self._name = name
            self._until = self._monotonic() + self._seconds
            self._failures = 0
            self._clients.clear()
        return code, int(self._seconds)

    def take(self, candidate: str, client: str) -> PairingOutcome:
        """Use the open code if *candidate* is it, closing it; count a wrong one."""
        with self._lock:
            now = self._monotonic()
            if self._code is None or now > self._until:
                self._code = None
                return PairingOutcome(False)
            retry_after = max(1, math.ceil(self._until - now))
            if self._clients.get(client, 0) >= PAIRING_MAX_FAILURES_PER_CLIENT:
                return PairingOutcome(False, retry_after=retry_after)
            if secrets.compare_digest(candidate.encode("ascii"), self._code.encode("ascii")):
                name = self._name
                self._code = None
                return PairingOutcome(True, name)
            failures = self._clients.get(client, 0) + 1
            self._clients[client] = failures
            self._failures += 1
            if failures == PAIRING_MAX_FAILURES_PER_CLIENT:
                logger.warning(
                    "Worker pairing locked for client %s for %d seconds after %d wrong codes.",
                    client,
                    retry_after,
                    PAIRING_MAX_FAILURES_PER_CLIENT,
                )
            if self._failures >= PAIRING_MAX_FAILURES_PER_WINDOW:
                self._code = None
                logger.warning(
                    "Worker pairing code invalidated after %d wrong attempts; someone may be "
                    "guessing.  Choose Pair a worker again for a new code.",
                    PAIRING_MAX_FAILURES_PER_WINDOW,
                )
            return PairingOutcome(False)


#: What Settings says about each kind of refusal; ``identity`` and
#: ``scan_version`` carry their own words.
_REFUSAL_ADVICE = {
    "revoked": "Its token was revoked.  Pair the worker again, or give it a new token, "
    "and start it again.",
    "invalid": "It sent a token whose secret is wrong.  Check AUTODJ_WORKER_TOKEN or the "
    "token saved on the worker, or pair the worker again.",
    "scope": "Its token is not an analysis worker token.  Pair the worker, or give it an "
    "analysis worker token.",
}


def _refusal_view(refusal: dict[str, object]) -> dict[str, object]:
    kind = str(refusal["kind"])
    return {**refusal, "message": _REFUSAL_ADVICE.get(kind) or refusal["detail"]}


def _host_token_name(name: str) -> str:
    """A worker's host name as a token name, or a plain one if it will not do."""
    try:
        return validate_name(_clean_text(name, 64))
    except ValueError:
        return "Analysis worker"


class RateLimiter:
    """At most *limit* requests per token in any *window* seconds."""

    def __init__(
        self, limit: int, window: float, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self._limit = limit
        self._window = window
        self._monotonic = monotonic
        self._seen: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> int | None:
        """Count one request; seconds to wait if it is over the limit, else ``None``."""
        now = self._monotonic()
        with self._lock:
            times = self._seen.setdefault(key, deque())
            while times and times[0] <= now - self._window:
                times.popleft()
            if len(times) >= self._limit:
                return max(1, math.ceil(times[0] + self._window - now))
            times.append(now)
        return None


def decode_vector(text: str) -> np.ndarray:
    """A posted vector as float32, or ``ValueError`` unless it is a usable unit vector."""
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("vector is not base64") from exc
    if len(raw) != _VECTOR_BYTES:
        raise ValueError(f"vector must have {FEATURE_DIM} values")
    vector = np.frombuffer(raw, dtype="<f4").astype(np.float32)
    if not np.isfinite(vector).all():
        raise ValueError("vector has values that are not finite")
    if abs(float(np.linalg.norm(vector)) - 1.0) > 1e-3:
        raise ValueError("vector is not normalized")
    return vector


def _clean_text(value: str, limit: int = 300) -> str:
    """*value* with control characters dropped, for the log."""
    return "".join(ch if ch.isprintable() else " " for ch in value)[:limit]


def _token_view(token: MachineToken) -> dict[str, object]:
    return {
        "token_id": token.token_id,
        "name": token.name,
        "scopes": list(token.scopes),
        "station": token.station,
        "created_at": token.created_at,
        "last_used_at": token.last_used_at,
    }


def _default_stations() -> list[tuple[str, str]]:
    return [("default", "This station")]


def install_worker_api(
    app: FastAPI,
    *,
    tokens: Callable[[], MachineTokenStore],
    library: Callable[[], LibraryManager | None],
    identity: Callable[[], dict[str, object]],
    rate_limit: int = RATE_LIMIT_PER_MINUTE,
    stations: Callable[[], list[tuple[str, str]]] = _default_stations,
) -> None:
    """Add the worker and token-management routes to *app*.

    Args:
        app: The server app.  Its security middleware must be given
            :data:`MACHINE_SCOPES` and the store's ``authenticate``.
        tokens: Where machine tokens are kept (opened on first use).
        library: The live library, or ``None`` when the server has none.
        identity: What results must be made with
            (:func:`~autodj.indexer.analysis_identity`).
        rate_limit: Requests per token per minute.
        stations: ``(slug, name)`` of every configured station, for
            announcer tokens.
    """
    limiter = RateLimiter(rate_limit, 60.0)
    scan_mismatches: dict[str, None] = {}
    # token_id -> the settings revision its worker last said it applied.
    applied: dict[str, int] = {}
    pairing = WorkerPairing()
    app.state.worker_pairing = pairing

    def scan_capable(token: MachineToken, body: ClaimBody | HeartbeatBody) -> bool:
        """Whether *token*'s worker decodes scans here; a mismatch is noted for Settings.

        Runs off the event loop: noting a mismatch can write the token database.
        """
        if body.scan_analysis_version == SCAN_ANALYSIS_VERSION:
            return bool(body.scan_decode_v1)
        if not body.scan_decode_v1 and not body.scan_analysis_version:
            return False
        tokens().record_refusal(
            token.token_id,
            "scan_version",
            f"Its scan analysis version is {_clean_text(body.scan_analysis_version) or 'unknown'}"
            f" and this server's is {SCAN_ANALYSIS_VERSION}, so this server decodes new music "
            "itself; the worker still does similarity analysis.  Update AutoDJ on both machines "
            "to the same release.",
        )
        if token.token_id not in scan_mismatches:
            if len(scan_mismatches) >= _MAX_SCAN_MISMATCHES:
                del scan_mismatches[next(iter(scan_mismatches))]
            scan_mismatches[token.token_id] = None
            logger.warning(
                "Analysis worker %s has incompatible scan analysis version %r; expected %r",
                _clean_text(token.name),
                _clean_text(body.scan_analysis_version),
                SCAN_ANALYSIS_VERSION,
            )
        return False

    def server_identity() -> dict[str, object]:
        """This server's identity, or 409 when it cannot check results at all."""
        found = identity()
        if not found.get("model_commit"):
            raise HTTPException(
                status_code=409,
                detail=(
                    "This server has no downloaded copy of the analysis model, so it cannot "
                    "check results. Restore an existing compatible model cache on the server, "
                    "or use a model-capable runtime to run `autodj index` with songs pending "
                    "similarity analysis. No-model indexing does not download weights."
                ),
            )
        return found

    def worker_context(request: Request) -> tuple[MachineToken, LibraryManager]:
        token: MachineToken | None = getattr(request.state, "machine_token", None)
        if token is None:  # pragma: no cover -- the middleware always sets it
            raise HTTPException(status_code=401, detail="Machine token required")
        wait = limiter.retry_after(token.token_id)
        if wait is not None:
            raise HTTPException(
                status_code=429,
                detail="Too many requests from this worker",
                headers={"Retry-After": str(wait)},
            )
        manager = library()
        if manager is None:
            raise HTTPException(status_code=503, detail="This server has no library catalog")
        return token, manager

    def managed(
        token: MachineToken, body: ClaimBody | HeartbeatBody, expected: dict[str, object]
    ) -> None:
        """Refuse a worker whose reported identity differs; note the revision it applied.

        A worker reports only what it knows: before it has downloaded the
        model, it has no weights to compare.  Runs off the event loop.
        """
        if body.identity is not None:
            reported = {
                key: value for key, value in body.identity.model_dump().items() if value is not None
            }
            if any(expected.get(key) != value for key, value in reported.items()):
                detail = identity_mismatch(expected, {**expected, **reported})
                tokens().record_refusal(token.token_id, "identity", detail)
                raise HTTPException(status_code=409, detail=detail)
        if body.settings_revision is not None:
            applied[token.token_id] = body.settings_revision

    def manage_reply(token: MachineToken) -> dict[str, object]:
        """What every heartbeat and claim reply tells a worker about its settings."""
        return {
            "worker_manage_v1": True,
            "settings": tokens().worker_settings(token.token_id).as_dict(),
        }

    # ------------------------------------------------------------------
    # Worker routes (machine token with the analysis-worker scope)
    # ------------------------------------------------------------------

    @app.post(WORKER_PREFIX + "claim")
    async def worker_claim(body: ClaimBody, request: Request) -> JSONResponse:
        """Lease up to *batch* songs to this worker and describe them.

        A worker paused in Settings is handed nothing, so pausing works
        for older workers too.
        """
        token, manager = worker_context(request)
        expected = await asyncio.to_thread(server_identity)

        def claim() -> dict[str, Any]:
            managed(token, body, expected)
            supports_scan = scan_capable(token, body)
            settings = tokens().worker_settings(token.token_id)
            reply = {"worker_manage_v1": True, "settings": settings.as_dict()}
            queue = manager.analysis_queue
            queue.seen(token.token_id, token.name, body.device or None, scan_decode=supports_scan)
            by_path: dict[str, dict[str, Any]] = {}
            changed_paths: set[str] = set()
            leases: list[Lease] = []
            paused = settings.paused
            advanced: Iterator[str] = (
                iter(()) if paused else manager.catalog.iter_unembedded_paths()
            )
            pending_paths = (
                queue.claimable_scans(limit=body.batch) if supports_scan and not paused else []
            )
            pending = set(pending_paths)
            candidates = chain(
                pending_paths,
                (path for path in advanced if path not in pending and not queue.scan_pending(path)),
            )
            held = queue.held_paths()
            while len(leases) < body.batch:
                records = []
                for path in candidates:
                    if path in held:
                        continue
                    try:
                        readable = Path(path).is_file()
                    except OSError:
                        readable = False
                    if not readable:
                        if path in pending:
                            queue.fail_scan(path)
                        continue
                    record = manager.catalog.record_for_path(path)
                    if record is None and path in pending:
                        # Tag publication may still be running under the scan lock.
                        queue.handoff_scan(path, expired_only=True)
                    if record is not None:
                        if path in pending:
                            if (
                                record["kind"] != "music"
                                or not manager.catalog.sources_for_path(path, "music")
                                or manager.catalog.sources_for_path(path, "liner")
                            ):
                                queue.cancel_scan(path)
                                continue
                            # Pending bytes can differ from the still-playable catalog row.
                            # Never roll pending identity back to that older record.
                            try:
                                identity = file_fingerprint(path)
                            except OSError:
                                queue.fail_scan(path)
                                continue
                            if not all(identity) or identity != queue.scan_identity(path):
                                if not all(identity):
                                    queue.fail_scan(path)
                                else:
                                    queue.handoff_scan(path)
                                continue
                            if (record["size"], record["fingerprint"]) != identity:
                                changed_paths.add(path)
                            record = {**record, "size": identity[0], "fingerprint": identity[1]}
                        records.append(record)
                    if len(records) == body.batch - len(leases):
                        break
                if not records:
                    break
                acquired = queue.claim(
                    token.token_id,
                    [(r["path"], r["size"], r["fingerprint"]) for r in records],
                    WORKER_LEASE_SECONDS,
                    require_scan=pending if supports_scan else None,
                )
                acquired_paths = {lease.path for lease in acquired}
                by_path.update((r["path"], r) for r in records if r["path"] in acquired_paths)
                leases.extend(acquired)
            songs = []
            for lease in leases:
                record = by_path[lease.path]
                audio_analysis = manager.catalog.get_audio_analysis(lease.path)
                songs.append(
                    {
                        "lease": lease.lease_id,
                        "suffix": Path(lease.path).suffix.lower(),
                        "size": lease.size,
                        "fingerprint": lease.fingerprint,
                        "title": record["title"],
                        "artist": record["artist"],
                        "length": record["length"],
                    }
                )
                if audio_analysis is not None:
                    songs[-1]["audio_analysis"] = audio_analysis
                elif supports_scan and lease.path not in pending:
                    options = manager.scan_options(Path(lease.path))
                    if options["dj"]:
                        songs[-1]["audio_options"] = options
                if supports_scan and lease.path in pending:
                    songs[-1]["scan"] = manager.scan_options(Path(lease.path))
                    songs[-1]["embed"] = manager.catalog.awaits_analysis(lease.path) or (
                        lease.path in changed_paths
                        and any(
                            source["advanced"]
                            for source in manager.catalog.sources_for_path(lease.path, "music")
                        )
                    )
                    songs[-1]["advanced_max_seconds"] = manager.advanced_max_minutes() * 60
            return {
                "identity": expected,
                "lease_seconds": WORKER_LEASE_SECONDS,
                "heartbeat_seconds": HEARTBEAT_SECONDS,
                "waiting": manager.catalog.unembedded_count(),
                "songs": songs,
                "scan_decode_v1": True,
                "scan_analysis_version": SCAN_ANALYSIS_VERSION,
                **reply,
            }

        reply = await asyncio.to_thread(claim)
        if not reply["songs"]:
            audit_as_routine(request)
        return JSONResponse(reply, headers=_NO_STORE)

    @app.api_route(WORKER_PREFIX + "audio", methods=["GET", "HEAD"])
    async def worker_audio(lease: _LeaseQuery, request: Request) -> FileResponse:
        """The original audio file of a song this worker has claimed."""
        token, manager = worker_context(request)

        def check() -> Path:
            queue = manager.analysis_queue
            queue.seen(token.token_id, token.name)
            held = queue.lease(lease, token.token_id)
            if held is None:
                raise HTTPException(status_code=404, detail="That song is not claimed")
            if file_fingerprint(held.path) != (held.size, held.fingerprint):
                queue.finish(held, "stale")
                raise HTTPException(status_code=409, detail="The song changed on the server")
            return Path(held.path)

        path = await asyncio.to_thread(check)
        # Always the file's own bytes, never a transcode, so the analysis
        # matches what this server would have made of it.
        return FileResponse(path, media_type="application/octet-stream", headers=_NO_STORE)

    async def fail_rejected_result(request: Request) -> AsyncIterator[None]:
        """End owned claims even when body validation rejects the result before storage."""
        try:
            yield
        except (HTTPException, RequestValidationError) as exc:
            invalid_content = isinstance(exc, RequestValidationError) or exc.status_code == 422
            token: MachineToken | None = getattr(request.state, "machine_token", None)
            manager = library()
            payload = await request.json()
            lease_id = payload.get("lease") if isinstance(payload, dict) else None
            if token is not None and manager is not None and isinstance(lease_id, str):

                def fail() -> None:
                    queue = manager.analysis_queue
                    held = queue.lease(lease_id, token.token_id)
                    if held is not None and (queue.scan_pending(held.path) or invalid_content):
                        queue.finish(held, "failed")

                await asyncio.to_thread(fail)
            raise

    @app.post(WORKER_PREFIX + "result", dependencies=[Depends(fail_rejected_result)])
    async def worker_result(body: ResultBody, request: Request) -> dict[str, bool]:
        """Store one song's analysis, or refuse it as stale or wrong."""
        token, manager = worker_context(request)
        expected = await asyncio.to_thread(server_identity)
        got = {
            "model": body.model,
            "model_commit": body.model_commit,
            "analysis_version": body.analysis_version,
            "dimension": body.dimension,
        }
        if got != expected:
            detail = identity_mismatch(expected, got)
            await asyncio.to_thread(tokens().record_refusal, token.token_id, "identity", detail)
            raise HTTPException(status_code=409, detail=detail)
        try:
            vector = None if body.vector is None else decode_vector(body.vector)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        def store() -> bool:
            queue = manager.analysis_queue
            queue.seen(token.token_id, token.name)
            held = queue.lease(body.lease, token.token_id)
            if held is None:
                raise HTTPException(status_code=404, detail="That song is not claimed")
            pending = queue.scan_pending(held.path)
            prepared = body.scan or body.preparation
            if body.scan is not None and body.preparation is not None:
                raise HTTPException(status_code=422, detail="Duplicate preparation result")
            if body.preparation is not None and body.preparation.audio_analysis is None:
                raise HTTPException(status_code=422, detail="Audio features are required")
            if not pending and body.scan is not None:
                raise HTTPException(
                    status_code=422, detail="Scan result does not match pending work"
                )
            if vector is None and body.scan is None:
                raise HTTPException(status_code=422, detail="A vector is required")
            if prepared is not None:
                options = manager.scan_options(Path(held.path))
                if options["dj"] and (prepared.dj is None or prepared.audio_analysis is None):
                    raise HTTPException(status_code=422, detail="Scan measurements are incomplete")
                record = manager.catalog.record_for_path(held.path)
                if record is not None:
                    same_bytes = (record["size"], record["fingerprint"]) == (
                        held.size,
                        held.fingerprint,
                    )
                    if same_bytes:
                        length = record["length"]
                    else:
                        from autodj.audio_meta import read_file_tags

                        length = read_file_tags(held.path).length
                    if math.isfinite(length) and length > 0:
                        measured = record["energy"] != 0 or any(
                            source not in {"user", "imported"}
                            for source in record["point_sources"].values()
                        )
                        if same_bytes and measured:
                            tolerance = max(2.0, length * 0.02)
                            lower, upper = length - tolerance, length + tolerance
                        else:
                            lower, upper = length * 0.5 - 3.0, length * 2.0 + 3.0
                        if not lower <= prepared.length <= upper:
                            raise HTTPException(
                                status_code=422, detail="Scan duration does not match the song"
                            )
            stored = (body.size, body.fingerprint) == (
                held.size,
                held.fingerprint,
            ) and manager.catalog.store_analysis(
                held.path,
                held.size,
                held.fingerprint,
                {
                    "energy": body.energy,
                    "key": body.key,
                    "mode": body.mode,
                    "bpm": body.bpm,
                    "tempo_confidence": body.tempo_confidence,
                },
                vector,
                scan=body.scan.model_dump() if body.scan is not None else None,
                audio_analysis=prepared.audio_analysis if prepared is not None else None,
            )
            if stored and prepared is not None:
                if prepared.dj is not None:
                    from autodj.dj_meta import Cue, DjMeta

                    meta = prepared.dj.model_dump()
                    meta["cues"] = [Cue(**cue) for cue in meta["cues"]]
                    try:
                        manager.store_scan_dj(Path(held.path), DjMeta(**meta), force=True)
                    except Exception as exc:
                        queue.finish(held, "failed")
                        logger.warning("Worker DJ result could not be stored: %s", exc)
                        raise HTTPException(
                            status_code=422, detail="DJ result could not be stored"
                        ) from exc
                if body.scan is not None:
                    queue.finish_scan(held.path, held.size, held.fingerprint)
            queue.finish(held, "done" if stored else "stale")
            return stored

        try:
            stored = await asyncio.to_thread(store)
        except ValueError as exc:
            # Merging with cues edited since the claim can reject otherwise valid suggestions.
            held = manager.analysis_queue.lease(body.lease, token.token_id)
            if held is not None:
                manager.analysis_queue.finish(held, "failed")
            raise HTTPException(
                status_code=422, detail="Result conflicts with current song data"
            ) from exc
        if not stored:
            raise HTTPException(
                status_code=409, detail="The song changed on the server; the result was discarded"
            )
        return {"stored": True}

    @app.post(WORKER_PREFIX + "failed")
    async def worker_failed(body: FailedBody, request: Request) -> dict[str, bool]:
        """Note a song the worker could not analyse; it is held back for a while."""
        token, manager = worker_context(request)

        def fail() -> None:
            queue = manager.analysis_queue
            queue.seen(token.token_id, token.name)
            held = queue.lease(body.lease, token.token_id)
            if held is None:
                raise HTTPException(status_code=404, detail="That song is not claimed")
            queue.finish(held, "failed")
            logger.warning(
                "Analysis worker %s could not analyse %s: %s",
                token.name,
                held.path,
                _clean_text(body.reason) or "no reason given",
            )

        await asyncio.to_thread(fail)
        return {"recorded": True}

    @app.post(WORKER_PREFIX + "heartbeat")
    async def worker_heartbeat(body: HeartbeatBody, request: Request) -> dict[str, object]:
        """Renew reported claims, or release orphaned claims on process startup.

        A connect that passes the identity check clears the token's refusal
        notice in Settings.
        """
        token, manager = worker_context(request)
        expected = await asyncio.to_thread(server_identity)

        def beat() -> tuple[int, dict[str, object]]:
            managed(token, body, expected)
            if body.connect:
                tokens().clear_refusal(token.token_id)
            supports_scan = scan_capable(token, body)
            queue = manager.analysis_queue
            queue.seen(token.token_id, token.name, body.device or None, scan_decode=supports_scan)
            if body.connect:
                queue.release_claims(token.token_id)
                return 0, manage_reply(token)
            return queue.renew(token.token_id, WORKER_LEASE_SECONDS, body.leases), manage_reply(
                token
            )

        renewed, reply = await asyncio.to_thread(beat)
        if not body.connect:
            audit_as_routine(request)
        return {
            "renewed": renewed,
            "lease_tracking": True,
            "lease_seconds": WORKER_LEASE_SECONDS,
            "heartbeat_seconds": HEARTBEAT_SECONDS,
            "identity": expected,
            "scan_decode_v1": True,
            "scan_analysis_version": SCAN_ANALYSIS_VERSION,
            **reply,
        }

    @app.post(WORKER_PREFIX + "release")
    async def worker_release(body: ReleaseBody, request: Request) -> dict[str, int]:
        """Give unanalysed songs back to the queue, as a stopping worker does."""
        token, manager = worker_context(request)
        released = await asyncio.to_thread(
            manager.analysis_queue.release, token.token_id, body.leases
        )
        return {"released": released}

    # ------------------------------------------------------------------
    # Settings routes (paired browser)
    # ------------------------------------------------------------------

    @app.get("/api/machine-tokens")
    async def api_machine_tokens() -> JSONResponse:
        """List the active machine tokens, the scopes a new one may have and the stations."""
        found = await asyncio.to_thread(lambda: tokens().list_tokens())
        return JSONResponse(
            {
                "scopes": [
                    {"id": key, "label": label, "station": key in STATION_SCOPES}
                    for key, label in KNOWN_SCOPES.items()
                ],
                "stations": [{"slug": slug, "name": name} for slug, name in stations()],
                "tokens": [_token_view(token) for token in found],
            },
            headers=_NO_STORE,
        )

    @app.post("/api/machine-tokens", status_code=201)
    async def api_machine_token_create(body: TokenCreateBody) -> JSONResponse:
        """Create a token and return its text, which is never shown again."""
        if body.station and body.station not in {slug for slug, _name in stations()}:
            raise HTTPException(status_code=422, detail="No station has that name")
        try:
            token, secret = await asyncio.to_thread(
                lambda: tokens().create(body.name, body.scopes, body.station)
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # The token's name and scopes, never its secret.
        logger.info(  # nosemgrep: python-logger-credential-disclosure
            "Machine token %r created with scopes %s%s",
            token.name,
            ", ".join(token.scopes),
            f" for station {token.station}" if token.station else "",
        )
        return JSONResponse(
            {"token": _token_view(token), "secret": secret}, status_code=201, headers=_NO_STORE
        )

    @app.delete("/api/machine-tokens/{token_id}")
    async def api_machine_token_revoke(token_id: str) -> dict[str, str]:
        """Revoke a token; a worker using it stops at its next request."""

        def revoke() -> MachineToken | None:
            store = tokens()
            found = store.get(token_id)
            if found is None or not store.revoke(token_id):
                return None
            manager = library()
            if manager is not None:
                manager.analysis_queue.forget_worker(token_id)
            return found

        revoked = await asyncio.to_thread(revoke)
        if revoked is None:
            raise HTTPException(status_code=404, detail="No such active token")
        scan_mismatches.pop(token_id, None)
        applied.pop(token_id, None)
        logger.info("Machine token %r revoked", revoked.name)
        return {"revoked": token_id, "name": revoked.name}

    @app.post("/api/machine-tokens/pairing-code", status_code=201)
    async def api_worker_pairing_code(body: PairingCodeBody) -> JSONResponse:
        """A code that one worker can trade for its own token, once, for ten minutes.

        The code is never logged.  A new code replaces one still open.
        """
        name = None
        if body.name.strip():
            try:
                name = validate_name(body.name)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        code, seconds = pairing.open(name)
        logger.info(
            "Worker pairing code created for %s; it works once, for %d minutes.",
            repr(name) if name else "the worker's host name",
            seconds // 60,
        )
        return JSONResponse(
            {"code": code, "valid_seconds": seconds, "name": name},
            status_code=201,
            headers=_NO_STORE,
        )

    @app.post(WORKER_PAIR_PATH, status_code=201)
    async def worker_pair(body: WorkerPairBody, request: Request) -> JSONResponse:
        """Trade a pairing code for a new analysis-worker token, shown only in this reply."""
        outcome = pairing.take(body.code, peer_address(request.scope))
        if outcome.retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail="Too many wrong pairing codes from this computer.  Try again in "
                f"{outcome.retry_after} seconds, or ask for a new code.",
                headers={"Retry-After": str(outcome.retry_after)},
            )
        if not outcome.paired:
            raise HTTPException(status_code=401, detail="Invalid or expired pairing code")
        name = outcome.name or _host_token_name(body.name)
        token, secret = await asyncio.to_thread(
            lambda: tokens().create(name, [SCOPE_ANALYSIS_WORKER])
        )
        # The token's name, never its secret.
        logger.info(  # nosemgrep: python-logger-credential-disclosure
            "Machine token %r created by worker pairing", token.name
        )
        return JSONResponse(
            {"token": secret, "token_id": token.token_id, "name": token.name},
            status_code=201,
            headers=_NO_STORE,
        )

    @app.put("/api/analysis/workers/{token_id}/settings")
    async def api_worker_settings(token_id: str, body: WorkerSettingsBody) -> JSONResponse:
        """Store what one worker should do; it applies them at its next heartbeat or claim."""
        try:
            saved = await asyncio.to_thread(
                lambda: tokens().set_worker_settings(
                    token_id, body.paused, body.batch, body.idle_release_minutes
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if saved is None:
            raise HTTPException(
                status_code=404, detail="No active analysis worker token has that id"
            )
        logger.info(
            "Analysis worker %s settings: paused %s, batch %s, idle release %s (revision %d)",
            token_id,
            saved.paused,
            saved.batch if saved.batch is not None else "the worker's own",
            f"{saved.idle_release_minutes} min"
            if saved.idle_release_minutes is not None
            else "the worker's own",
            saved.revision,
        )
        return JSONResponse(saved.as_dict(), headers=_NO_STORE)

    @app.delete("/api/analysis/workers/{token_id}/refusal")
    async def api_worker_refusal_dismiss(token_id: str) -> dict[str, str]:
        """Hide a worker's refusal notice until it is refused for another reason."""
        if not await asyncio.to_thread(tokens().dismiss_refusal, token_id):
            raise HTTPException(status_code=404, detail="That worker has no refusal notice")
        return {"dismissed": token_id}

    @app.get("/api/analysis/workers")
    async def api_analysis_workers() -> JSONResponse:
        """Connected workers, how much is waiting, and whether the CPU job is paused.

        Each worker carries its settings and whether it has applied them
        (``None`` for a worker too old to say), and ``refusals`` lists the
        latest time the server refused each worker token, and why.
        """
        manager = library()
        if manager is None:
            return JSONResponse({"available": False, "workers": [], "waiting": 0, "analysing": 0})

        def snapshot() -> dict[str, Any]:
            queue = manager.analysis_queue
            workers = queue.workers()
            store = tokens()
            settings = store.all_worker_settings()
            for worker in workers:
                found = settings.get(str(worker["token_id"])) or WorkerSettings()
                worker["settings"] = found.as_dict()
                revision = applied.get(str(worker["token_id"]))
                worker["settings_applied"] = (
                    None if revision is None else revision == found.revision
                )
            return {
                "available": True,
                "workers": workers,
                "waiting": manager.catalog.unembedded_count(),
                "analysing": queue.leased_count(),
                "server_paused": any(worker["connected"] for worker in workers),
                "refusals": [_refusal_view(refusal) for refusal in store.refusals()],
            }

        return JSONResponse(await asyncio.to_thread(snapshot), headers=_NO_STORE)


__all__ = [
    "MACHINE_SCOPES",
    "RESULT_BODY_MAX_BYTES",
    "SMALL_BODY_MAX_BYTES",
    "WORKER_PAIRING_SECONDS",
    "WORKER_PAIR_PATH",
    "WORKER_PREFIX",
    "RateLimiter",
    "WorkerPairing",
    "decode_vector",
    "install_worker_api",
]
