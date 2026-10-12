"""The breaks API: an announcer program feeds a station DJ breaks, news and weather (#88).

Every route here is a machine route.  The security middleware lets a
request in only with an ``Authorization: Bearer`` machine token that has
the :data:`~autodj.machine_tokens.SCOPE_ANNOUNCER` scope, over HTTPS or
from this machine, with no ``Origin`` header; a browser session does not
work here.  The token is bound to one station and works for no other.

The routes answer under ``/api/announcer/`` for the default station and
``/api/stations/<slug>/announcer/`` for any station by name:

* ``GET context``: the playing song, the next one, the open slot (the
  song change a break can still join), recent plays and queued requests.
* ``POST breaks``: upload one break for the open slot, as
  ``multipart/form-data``.  ``201`` books it; ``409`` says the slot closed
  or its songs changed, with the slot that is open now.
* ``GET breaks`` and ``GET breaks/<id>``: what became of the breaks.
* ``DELETE breaks/<id>``: drop a break that is still waiting.

Uploads stream to the station's announcements folder under a name the
server makes up, are decoded off the event loop (one at a time, so a
burst of uploads cannot starve the render-ahead worker of CPU), and are
deleted once the break airs, is dropped or expires.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from autodj.breaks import (
    EXTENSIONS,
    KINDS,
    Break,
    BreakDesk,
    BreakError,
    SlotPlan,
    file_name,
    new_id,
    speech_cues,
    valid_id,
)
from autodj.liner_files import LinerTooLargeError, store_liner_upload
from autodj.machine_tokens import SCOPE_ANNOUNCER, MachineToken
from autodj.station_routes import StationDep, StationRouter
from autodj.stereo import SAMPLE_RATE
from autodj.worker_api import RateLimiter

if TYPE_CHECKING:
    from autodj.stations import StationRuntime

logger = logging.getLogger(__name__)

#: Path prefix of the announcer routes (and the token scope they need).
ANNOUNCER_PREFIX = "/api/announcer/"
ANNOUNCER_SCOPES = {ANNOUNCER_PREFIX: SCOPE_ANNOUNCER}
#: The upload route, for the request-body cap.
UPLOAD_PATH = ANNOUNCER_PREFIX + "breaks"
#: Room for the multipart framing and the form fields around the file.
MULTIPART_OVERHEAD_BYTES = 64 * 1024
#: Plays and queued requests the context lists.
CONTEXT_ROWS = 10
#: How long an upload waits for the decoder before the server says it is busy.
DECODE_WAIT_SECONDS = 20.0
_NO_STORE = {"Cache-Control": "no-store"}

_Id = Annotated[str, Form(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")]


def _path_id(path: str) -> str:
    """A stable id for a song the catalog has no record of."""
    return hashlib.sha256(path.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _clean_label(value: str) -> str:
    """*value* with control characters turned to spaces, at most 120 characters."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())[:120]


class _Songs:
    """Song views for one request, with catalog ids looked up once each."""

    def __init__(self, station: StationRuntime) -> None:
        self._library = station.player._library
        self._ids: dict[str, str] = {}

    def song_id(self, path: str) -> str:
        found = self._ids.get(path)
        if found is None:
            record = (
                self._library.catalog.record_for_path(path) if self._library is not None else None
            )
            found = str(record["id"]) if record and record.get("id") else _path_id(path)
            self._ids[path] = found
        return found

    def view(self, entry: Any) -> dict[str, Any] | None:
        if entry is None:
            return None
        return {
            "id": self.song_id(entry.path),
            "title": entry.title,
            "artist": entry.artist,
            "album": entry.album,
            "year": entry.year or None,
            "duration_s": round(float(entry.length), 3),
        }


def _slot_view(
    station: StationRuntime, songs: _Songs, plan: SlotPlan, closes_in: float, airs_in: float
) -> dict[str, Any]:
    """The open slot, for the announcer."""
    player = station.player
    points = player.saved_points(plan.upcoming)
    intro = points.get("intro")
    start = points.get("start") or 0.0
    desk: BreakDesk = station.breaks
    return {
        "slot_id": plan.slot_id,
        "ending": songs.view(plan.ending),
        "upcoming": songs.view(plan.upcoming),
        "closes_in_s": round(closes_in, 1),
        "airs_in_s": round(airs_in, 1),
        "upcoming_intro_s": None if intro is None else round(max(0.0, intro - start), 3),
        "pinned": plan.pinned,
        "breaks_booked": desk.waiting_in(plan.slot_id),
    }


def _current_slot(station: StationRuntime) -> dict[str, Any] | None:
    """The open slot now, or ``None`` (blocking: it reads the catalog)."""
    plan, closes_in, airs_in = station.player.slot_status()
    if plan is None:
        return None
    return _slot_view(station, _Songs(station), plan, closes_in, airs_in)


def _context(station: StationRuntime) -> dict[str, Any]:
    """Everything ``GET context`` says (blocking: it reads the catalog)."""
    player = station.player
    state = player._state
    songs = _Songs(station)
    desk: BreakDesk = station.breaks
    config = desk.config
    reason = None
    if not desk.enabled:
        reason = "Breaks are off for this station: set [breaks] enabled = true"
    elif not player._breaks_on():
        reason = "Breaks need saved-point playback (transition mode: use saved segue points)"
    plan, closes_in, airs_in = player.slot_status() if reason is None else (None, 0.0, 0.0)
    if reason is None and plan is None:
        reason = "No song change is open yet; ask again in a few seconds"
    playing = player._playing_render
    now = None
    if state.current_track is not None:
        now = songs.view(state.current_track)
        if now is not None and playing is not None and playing.entry is state.current_track:
            now["elapsed_s"] = round(player._playback_pos[0] / SAMPLE_RATE, 1)
            now["remaining_s"] = round(player._playing_remaining_s(), 1)
    with player._pick_lock:
        history = list(state.play_history)[-CONTEXT_ROWS:]
    with state.queue_lock:
        waiting = ([state.queued_next] if state.queued_next else []) + list(state.queue)
    return {
        "station": station.slug,
        "accepting": plan is not None,
        "reason": reason,
        "now": now,
        "next": songs.view(plan.before if plan is not None else state.next_track),
        "slot": None if plan is None else _slot_view(station, songs, plan, closes_in, airs_in),
        "history": [
            {
                "id": songs.song_id(record.path),
                "title": record.title,
                "artist": record.artist,
                "album": record.album,
                "played_at": round(record.started, 3),
            }
            for record in reversed(history)
        ],
        "requests": [songs.view(entry) for entry in waiting[:CONTEXT_ROWS]],
        "limits": {
            "kinds": list(KINDS),
            "extensions": list(EXTENSIONS),
            "max_bytes": config.max_upload_bytes,
            "max_seconds": config.max_seconds,
            "max_queued": config.max_queued,
            "max_per_slot": config.max_per_slot,
            "default_expires_s": config.default_expires_seconds,
            "max_expires_s": config.max_expires_seconds,
            "dj_placement": config.dj_placement,
        },
    }


def install_breaks(router: StationRouter) -> None:
    """Add the announcer routes; each acts on the station its URL names."""
    limiters: dict[int, RateLimiter] = {}
    # One decode at a time across every station: decoding takes a CPU core.
    decoding: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
        weakref.WeakKeyDictionary()
    )

    def decoder() -> asyncio.Semaphore:
        return decoding.setdefault(asyncio.get_running_loop(), asyncio.Semaphore(1))

    def announcer(request: Request, station: StationRuntime) -> tuple[MachineToken, BreakDesk]:
        """The request's token and the station's desk, or the HTTP error that refuses it."""
        token: MachineToken | None = getattr(request.state, "machine_token", None)
        if token is None:  # pragma: no cover -- the middleware always sets it
            raise HTTPException(status_code=401, detail="Machine token required")
        if token.station != station.slug:
            raise HTTPException(status_code=403, detail="This token is for another station")
        desk: BreakDesk | None = getattr(station, "breaks", None)
        if desk is None:
            raise HTTPException(status_code=503, detail="This station cannot take breaks")
        per_minute = desk.config.requests_per_minute
        limiter = limiters.setdefault(per_minute, RateLimiter(per_minute, 60.0))
        wait = limiter.retry_after(token.token_id)
        if wait is not None:
            raise HTTPException(
                status_code=429,
                detail="Too many requests from this token",
                headers={"Retry-After": str(wait)},
            )
        return token, desk

    def require_breaks(station: StationRuntime, desk: BreakDesk) -> None:
        if not desk.enabled:
            raise HTTPException(
                status_code=409, detail="Breaks are off for this station: set [breaks] enabled"
            )
        if not station.player._breaks_on():
            raise HTTPException(
                status_code=409,
                detail="Breaks need saved-point playback (transition mode: use saved segue points)",
            )

    def refused(status: int, detail: str, reason: str, slot: dict[str, Any] | None) -> JSONResponse:
        return JSONResponse(
            {"detail": detail, "reason": reason, "slot": slot},
            status_code=status,
            headers=_NO_STORE,
        )

    @router.get("/announcer/context")
    async def announcer_context(station: StationDep, request: Request) -> JSONResponse:
        """What is playing, what is next, and the song change a break can join."""
        announcer(request, station)
        return JSONResponse(await asyncio.to_thread(_context, station), headers=_NO_STORE)

    @router.post("/announcer/breaks", status_code=201)
    async def announcer_upload(
        station: StationDep,
        request: Request,
        file: Annotated[UploadFile, File()],
        kind: Annotated[Literal["dj", "news", "weather", "music_news"], Form()],
        slot_id: Annotated[str, Form(min_length=32, max_length=32)],
        ending_id: _Id,
        upcoming_id: _Id,
        placement: Annotated[Literal["auto", "talk_up", "standalone"], Form()] = "auto",
        allow_outro: Annotated[bool, Form()] = False,
        expires_s: Annotated[int | None, Form(ge=30)] = None,
        label: Annotated[str, Form(max_length=200)] = "",
    ) -> JSONResponse:
        """Upload one break for the open song change."""
        token, desk = announcer(request, station)
        require_breaks(station, desk)
        config = desk.config
        if kind != "dj" and placement == "talk_up":
            raise HTTPException(
                status_code=422, detail="Only a DJ break can talk up a song; use standalone"
            )
        if not valid_id(slot_id):
            raise HTTPException(status_code=422, detail="slot_id is not a slot id")
        if expires_s is not None and expires_s > config.max_expires_seconds:
            raise HTTPException(
                status_code=422,
                detail=f"expires_s must be at most {config.max_expires_seconds}",
            )
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in EXTENSIONS:
            raise HTTPException(
                status_code=415,
                detail="A break must be one of these file types: " + ", ".join(EXTENSIONS),
            )
        slot = await asyncio.to_thread(_current_slot, station)
        if slot is None or slot["slot_id"] != slot_id:
            return refused(409, "That song change is no longer open", "slot_closed", slot)
        if (slot["ending"] or {}).get("id") != ending_id or (slot["upcoming"] or {}).get(
            "id"
        ) != upcoming_id:
            return refused(
                409, "The songs around that song change have changed", "pair_changed", slot
            )
        try:
            desk.check_room(slot_id)
        except BreakError as exc:
            return refused(exc.status, exc.detail, exc.reason, slot)

        break_id = new_id()
        name = file_name(break_id, suffix)
        try:
            path, size = await store_liner_upload(
                desk.folder, name, file, max_bytes=config.max_upload_bytes, replace=False
            )
        except LinerTooLargeError as exc:
            raise HTTPException(
                status_code=413, detail=f"A break may be at most {config.max_upload_mb} MB"
            ) from exc
        booked = False
        try:
            gate = decoder()
            try:
                await asyncio.wait_for(gate.acquire(), DECODE_WAIT_SECONDS)
            except TimeoutError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="The server is decoding other breaks; try again shortly",
                    headers={"Retry-After": "5"},
                ) from exc
            try:
                cues = await asyncio.to_thread(_decode_cues, path, config.max_seconds)
            finally:
                gate.release()
            now = time.time()
            item = Break(
                break_id=break_id,
                kind=kind,
                slot_id=slot_id,
                ending=ending_id,
                upcoming=upcoming_id,
                placement=placement,
                allow_outro=bool(allow_outro),
                path=path,
                size=size,
                cues=cues,
                label=_clean_label(label),
                token_name=token.name,
                created_at=now,
                expires_at=now + (expires_s or config.default_expires_seconds),
            )
            try:
                await asyncio.to_thread(station.player.book_break, item)
            except BreakError as exc:
                slot = await asyncio.to_thread(_current_slot, station)
                return refused(exc.status, exc.detail, exc.reason, slot)
            booked = True
        finally:
            if not booked:
                await asyncio.shield(asyncio.to_thread(path.unlink, missing_ok=True))
        slot = await asyncio.to_thread(_current_slot, station)
        view = item.view()
        view["airs_in_s"] = slot["airs_in_s"] if slot and slot["slot_id"] == slot_id else None
        return JSONResponse(view, status_code=201, headers=_NO_STORE)

    @router.get("/announcer/breaks")
    async def announcer_breaks(station: StationDep, request: Request) -> JSONResponse:
        """The breaks waiting to air, then the finished ones, newest first."""
        _token, desk = announcer(request, station)
        return JSONResponse({"breaks": [item.view() for item in desk.listing()]}, headers=_NO_STORE)

    @router.get("/announcer/breaks/{break_id}")
    async def announcer_break(station: StationDep, request: Request, break_id: str) -> JSONResponse:
        """What became of one break."""
        _token, desk = announcer(request, station)
        item = desk.get(break_id) if valid_id(break_id) else None
        if item is None:
            raise HTTPException(status_code=404, detail="No break has that id")
        return JSONResponse(item.view(), headers=_NO_STORE)

    @router.delete("/announcer/breaks/{break_id}")
    async def announcer_cancel(
        station: StationDep, request: Request, break_id: str
    ) -> JSONResponse:
        """Drop a break that is still waiting for its song change."""
        _token, desk = announcer(request, station)
        if not valid_id(break_id):
            raise HTTPException(status_code=404, detail="No break has that id")
        try:
            item = desk.cancel(break_id)
        except BreakError as exc:
            return refused(exc.status, exc.detail, exc.reason, None)
        return JSONResponse(item.view(), headers=_NO_STORE)


def _decode_cues(path: Path, max_seconds: float) -> Any:
    """Decode an uploaded break and find its speech (blocking).

    Raises:
        HTTPException: 422 when the file is not audio AutoDJ can play, is
            longer than *max_seconds*, or holds no speech.
    """
    from autodj.stereo import TrackTooLongError, load_stereo

    try:
        audio = load_stereo(str(path), SAMPLE_RATE, max_seconds)
    except TrackTooLongError as exc:
        raise HTTPException(
            status_code=422, detail=f"A break may be at most {max_seconds:g} seconds long"
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail="That file is not audio AutoDJ can play"
        ) from exc
    try:
        return speech_cues(audio)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"That break is unusable: {exc}") from exc


__all__ = [
    "ANNOUNCER_PREFIX",
    "ANNOUNCER_SCOPES",
    "MULTIPART_OVERHEAD_BYTES",
    "UPLOAD_PATH",
    "install_breaks",
]
