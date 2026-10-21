"""The breaks API: an announcer program feeds a station DJ breaks, news and weather (#88).

Every ``announcer`` route here is a machine route.  The security middleware
lets a request in only with an ``Authorization: Bearer`` machine token that
has the :data:`~autodj.machine_tokens.SCOPE_ANNOUNCER` scope, over HTTPS or
from this machine, with no ``Origin`` header; a browser session does not
work here.  The token is bound to one station and works for no other.

The ``voice-tracks`` routes (#128) are the browser's: an operator signed in
to the web UI records a DJ break for the open song change and books it.
They take the session cookie and, for a change, the page's ``Origin``, as
every other browser route does, and never a machine token.  A voice track
is a break like any other: the same checks, caps, decoding, quota and
placement.

The routes answer under ``/api/announcer/`` for the default station and
``/api/stations/<slug>/announcer/`` for any station by name:

* ``GET context``: the playing song, the next one, the open slot (the
  song change a break can still join), recent plays and queued requests.
* ``POST breaks``: upload one break for the open slot, as
  ``multipart/form-data``.  ``201`` books it; ``409`` says the slot closed
  or its songs changed, with the slot that is open now.
* ``GET breaks`` and ``GET breaks/<id>``: what became of the breaks.
* ``DELETE breaks/<id>``: drop a break that is still waiting.

Settings, Breaks (a browser route, like the voice tracker's) answers at
``/api/breaks/settings`` and ``/api/stations/<slug>/breaks/settings``:

* ``GET breaks/settings``: whether the station takes breaks, where a DJ
  break goes, the shortest intro talked up and the duck under a break.
* ``POST breaks/settings``: change any of them, saved with the station's
  other settings in ``web_state.json`` and used at once.  Turning breaks
  off drops the breaks still waiting for their song change; from then on
  ``GET context`` says breaks are off and an upload is refused with 409.

The voice tracker's routes answer under ``/api/voice-tracks`` and
``/api/stations/<slug>/voice-tracks``:

* ``GET voice-tracks``: the open slot, the limits, and the station's voice
  tracks.
* ``POST voice-tracks``: book one recording for the open slot, as an
  announcer's upload, ``kind`` left out (always a DJ break).
* ``DELETE voice-tracks/<id>``: cancel a voice track that is still waiting.

Uploads stream to the station's announcements folder under a name the
server makes up, are decoded off the event loop (one at a time, so a
burst of uploads cannot starve the render-ahead worker of CPU), and are
deleted once the break airs, is dropped or expires.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import hashlib
import logging
import threading
import time
import weakref
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile

from autodj.breaks import (
    ANNOUNCER,
    BREAKS_OFF,
    EXTENSIONS,
    KINDS,
    VOICE_TRACK,
    Break,
    BreakCues,
    BreakDesk,
    BreakError,
    SlotPlan,
    file_name,
    new_id,
    placement_for,
    speech_cues,
    valid_id,
)
from autodj.liner_files import LinerTooLargeError, store_liner_upload
from autodj.machine_tokens import SCOPE_ANNOUNCER, MachineToken
from autodj.settings_bodies import PAGE_SAVED_BREAKS, BreakSettingsBody
from autodj.station_routes import SavedStationDep, StationDep, StationRouter
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
#: The voice tracker's upload route (#128), a browser route with the same cap.
VOICE_UPLOAD_PATH = "/api/voice-tracks"
#: Room for the multipart framing and the form fields around the file.
MULTIPART_OVERHEAD_BYTES = 64 * 1024
#: Plays and queued requests the context lists.
CONTEXT_ROWS = 10
#: How long an upload waits for the decoder before the server says it is busy.
DECODE_WAIT_SECONDS = 20.0
#: An upload must arrive within this many seconds, plus one second for every
#: :data:`MIN_BODY_BYTES_PER_SECOND` bytes it declares (408 past that).  A
#: client that trickles its body cannot hold the station's room for long.
BODY_DEADLINE_SECONDS = 60.0
MIN_BODY_BYTES_PER_SECOND = 256 * 1024
_NO_STORE = {"Cache-Control": "no-store"}
#: ... and while the station does not play from saved segue points.
NEEDS_SAVED_POINTS = "Breaks need saved-point playback (transition mode: use saved segue points)"
VOICE_TRACK_NEEDS_RENDERER = (
    "Voice tracks need server playback (server audio or stream) with saved-point playback"
)
#: Why the waiting breaks were dropped when breaks were turned off.
TURNED_OFF = "breaks were turned off in Settings"


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


def _context(station: StationRuntime, *, voice_track: bool = False) -> dict[str, Any]:
    """Everything ``GET context`` says (blocking: it reads the catalog)."""
    player = station.player
    state = player._state
    songs = _Songs(station)
    desk: BreakDesk = station.breaks
    config = desk.config
    reason = None
    if not voice_track and not desk.enabled:
        reason = BREAKS_OFF
    elif voice_track and not player.request_voice_track_slot():
        reason = VOICE_TRACK_NEEDS_RENDERER
    elif not player._breaks_on():
        reason = NEEDS_SAVED_POINTS
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
    decoder = decode_gate

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
            raise HTTPException(status_code=409, detail=BREAKS_OFF)
        if not station.player._breaks_on():
            raise HTTPException(status_code=409, detail=NEEDS_SAVED_POINTS)

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
    async def announcer_upload(station: StationDep, request: Request) -> JSONResponse:
        """Upload one break for the open song change."""
        token, desk = announcer(request, station)
        return await receive(station, request, desk, _UploadFields, token.name, ANNOUNCER)

    async def receive(
        station: StationRuntime,
        request: Request,
        desk: BreakDesk,
        form: type[_UploadFields],
        who: str,
        source: str,
    ) -> JSONResponse:
        """Take one break upload for the open song change: the announcer's or the voice tracker's.

        The body is read only after the station, the declared length and
        the station's room are checked, and room for it is held while it
        arrives and decodes, so uploads in parallel cannot pass the caps
        together.  *who* names the sender in the log; *source* says where
        the break came from.
        """
        if source == ANNOUNCER:
            require_breaks(station, desk)
        elif not station.player.request_voice_track_slot():
            raise HTTPException(status_code=409, detail=VOICE_TRACK_NEEDS_RENDERER)
        config = desk.config
        cap = config.max_upload_bytes
        declared = request.headers.get("content-length")
        size = cap
        if declared is not None:
            if not declared.isdecimal() or int(declared) > cap + MULTIPART_OVERHEAD_BYTES:
                raise HTTPException(
                    status_code=413, detail=f"A break may be at most {config.max_upload_mb} MB"
                )
            size = min(cap, int(declared))
        break_id = new_id()
        try:
            desk.reserve(break_id, "", size, file_name(break_id, ".part"))
        except BreakError as exc:
            return refused(exc.status, exc.detail, exc.reason, None)
        path: Path | None = None
        booked = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BODY_DEADLINE_SECONDS + size / MIN_BODY_BYTES_PER_SECOND
        try:
            fields, upload = await _within(deadline, _read_form(request, form))
            try:
                suffix = Path(upload.filename or "").suffix.lower()
                if suffix not in EXTENSIONS:
                    raise HTTPException(
                        status_code=415,
                        detail="A break must be one of these file types: " + ", ".join(EXTENSIONS),
                    )
                if fields.kind != "dj" and fields.placement == "talk_up":
                    raise HTTPException(
                        status_code=422, detail="Only a DJ break can talk up a song; use standalone"
                    )
                if fields.expires_s is not None and fields.expires_s > config.max_expires_seconds:
                    raise HTTPException(
                        status_code=422,
                        detail=f"expires_s must be at most {config.max_expires_seconds}",
                    )
                slot = await asyncio.to_thread(_current_slot, station)
                if slot is None or slot["slot_id"] != fields.slot_id:
                    return refused(409, "That song change is no longer open", "slot_closed", slot)
                if (slot["ending"] or {}).get("id") != fields.ending_id or (
                    slot["upcoming"] or {}
                ).get("id") != fields.upcoming_id:
                    return refused(
                        409, "The songs around that song change have changed", "pair_changed", slot
                    )
                name = file_name(break_id, suffix)
                try:
                    desk.retarget(break_id, fields.slot_id, name)
                except BreakError as exc:
                    return refused(exc.status, exc.detail, exc.reason, slot)
                try:
                    path, stored = await _within(
                        deadline,
                        store_liner_upload(desk.folder, name, upload, max_bytes=cap, replace=False),
                    )
                except LinerTooLargeError as exc:
                    raise HTTPException(
                        status_code=413, detail=f"A break may be at most {config.max_upload_mb} MB"
                    ) from exc
            finally:
                await upload.close()
            assert path is not None  # stored, or the request has already answered
            cues = await decode_one(path, config.max_seconds)
            now = time.time()
            label = _clean_label(fields.label)
            if not label and source == VOICE_TRACK:
                label = _clean_label(_pair_label(slot))
            item = Break(
                break_id=break_id,
                kind=fields.kind,
                slot_id=fields.slot_id,
                ending=fields.ending_id,
                upcoming=fields.upcoming_id,
                placement=fields.placement,
                allow_outro=fields.allow_outro,
                path=path,
                size=stored,
                cues=cues,
                label=label,
                token_name=who,
                created_at=now,
                expires_at=now + (fields.expires_s or config.default_expires_seconds),
                source=source,
            )
            try:
                if source == VOICE_TRACK and not station.player.request_voice_track_slot():
                    raise HTTPException(status_code=409, detail=VOICE_TRACK_NEEDS_RENDERER)
                await asyncio.to_thread(station.player.book_break, item)
            except BreakError as exc:
                slot = await asyncio.to_thread(_current_slot, station)
                return refused(exc.status, exc.detail, exc.reason, slot)
            booked = True
        finally:
            if not booked:
                # The room goes back first, with no await before it, so even a
                # cancelled request gives it back.  The file goes now if it can;
                # otherwise the station's upkeep deletes it once a decode still
                # holding it lets go.
                desk.release(break_id, path)
                if path is not None:
                    await asyncio.shield(asyncio.to_thread(_quiet_unlink, path))
        slot = await asyncio.to_thread(_current_slot, station)
        view = item.view()
        view["airs_in_s"] = (
            slot["airs_in_s"] if slot and slot["slot_id"] == fields.slot_id else None
        )
        return JSONResponse(view, status_code=201, headers=_NO_STORE)

    async def decode_one(path: Path, max_seconds: float) -> BreakCues:
        return await _gated_decode(decoder(), path, max_seconds)

    @router.get("/announcer/breaks")
    async def announcer_breaks(station: StationDep, request: Request) -> JSONResponse:
        """The announcer's breaks waiting to air, then the finished ones, newest first.

        Voice tracks booked in the browser are the operators', not the
        announcer's: they are not listed here.
        """
        _token, desk = announcer(request, station)
        items = [item.view() for item in desk.listing() if item.source == ANNOUNCER]
        return JSONResponse({"breaks": items}, headers=_NO_STORE)

    @router.get("/announcer/breaks/{break_id}")
    async def announcer_break(station: StationDep, request: Request, break_id: str) -> JSONResponse:
        """What became of one break."""
        _token, desk = announcer(request, station)
        item = desk.get(break_id) if valid_id(break_id) else None
        if item is None or item.source != ANNOUNCER:
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
            # Off the event loop: cancelling may unpin the player's slot.
            item = await asyncio.to_thread(
                desk.cancel, break_id, "cancelled by the announcer", ANNOUNCER
            )
        except BreakError as exc:
            return refused(exc.status, exc.detail, exc.reason, None)
        return JSONResponse(item.view(), headers=_NO_STORE)

    # ------------------------------------------------------------------
    # The voice tracker (#128): browser routes, with the signed-in session
    # ------------------------------------------------------------------

    def voice_desk(station: StationRuntime) -> BreakDesk:
        """The station's desk for a voice tracker request, or the HTTP error that refuses it.

        The security middleware has already checked the session and, for a
        change, the page's ``Origin``.  Changes count against the
        station's ``requests_per_minute``, shared by every open page.
        """
        desk: BreakDesk | None = getattr(station, "breaks", None)
        if desk is None:
            raise HTTPException(status_code=503, detail="This station cannot take voice tracks")
        per_minute = desk.config.requests_per_minute
        limiter = limiters.setdefault(per_minute, RateLimiter(per_minute, 60.0))
        wait = limiter.retry_after(f"voice-tracker:{station.slug}")
        if wait is not None:
            raise HTTPException(
                status_code=429,
                detail=f"Too many voice track changes this minute; wait {wait} seconds",
                headers={"Retry-After": str(wait)},
            )
        return desk

    @router.get("/voice-tracks")
    async def voice_tracks(station: SavedStationDep) -> JSONResponse:
        """The open song change, the limits, and this station's voice tracks."""
        desk: BreakDesk | None = getattr(station, "breaks", None)
        if desk is None:
            return JSONResponse(
                {
                    "station": station.slug,
                    "accepting": False,
                    "reason": "This station cannot take voice tracks",
                    "slot": None,
                    "tracks": [],
                },
                headers=_NO_STORE,
            )
        return JSONResponse(await asyncio.to_thread(_voice_context, station), headers=_NO_STORE)

    @router.post("/voice-tracks", status_code=201)
    async def voice_track_upload(station: StationDep, request: Request) -> JSONResponse:
        """Book a voice track recorded in the browser for the open song change.

        The same checks, caps, decoding and quota as an announcer's break.
        """
        desk = voice_desk(station)
        return await receive(station, request, desk, _VoiceFields, "the voice tracker", VOICE_TRACK)

    @router.delete("/voice-tracks/{break_id}")
    async def voice_track_cancel(station: SavedStationDep, break_id: str) -> JSONResponse:
        """Cancel a voice track that is still waiting for its song change."""
        desk = voice_desk(station)
        if not valid_id(break_id):
            raise HTTPException(status_code=404, detail="No voice track has that id")
        try:
            item = await asyncio.to_thread(
                desk.cancel, break_id, "cancelled in the voice tracker", VOICE_TRACK
            )
        except BreakError as exc:
            detail = "No voice track has that id" if exc.status == 404 else exc.detail
            return refused(exc.status, detail, exc.reason, None)
        return JSONResponse(item.view(), headers=_NO_STORE)

    # ------------------------------------------------------------------
    # Settings, Breaks: browser routes, with the signed-in session
    # ------------------------------------------------------------------

    @router.get("/breaks/settings")
    async def break_settings(station: SavedStationDep) -> JSONResponse:
        """Whether this station takes breaks, and how it places and ducks them."""
        return JSONResponse(break_settings_view(station), headers=_NO_STORE)

    @router.post("/breaks/settings")
    async def save_break_settings(
        station: SavedStationDep, body: BreakSettingsBody
    ) -> JSONResponse:
        """Change the break settings; saved for this station at once and used from now on.

        Turning breaks off drops the announcer uploads still waiting for
        their song change; manual voice tracks and mixed breaks remain.
        """
        config = station.cfg.breaks
        was_on = config.enabled is True
        for field in body.model_fields_set:
            value = getattr(body, field)
            # Null leaves a setting as it is, except the duck: there it
            # means "as far as a voice liner".
            if value is not None or field == "duck_db":
                setattr(config, field, value)
        # From now on these are the page's, not config.toml's, for this station.
        station.bridge.page_saved.add(PAGE_SAVED_BREAKS)
        dropped = 0
        desk: BreakDesk | None = getattr(station, "breaks", None)
        if was_on and config.enabled is not True and desk is not None:
            dropped = await asyncio.to_thread(desk.drop_waiting_breaks, TURNED_OFF)
        if config.enabled is not was_on:
            logger.info("Breaks turned %s in Settings", "on" if config.enabled else "off")
        await asyncio.to_thread(station.bridge.save_persistent_state)
        return JSONResponse({**break_settings_view(station), "dropped": dropped}, headers=_NO_STORE)


def break_settings_view(station: StationRuntime) -> dict[str, Any]:
    """What Settings, Breaks shows: the settings, and why breaks cannot be taken, if they cannot.

    ``duck_db`` is ``None`` when a break ducks the music as far as a voice
    liner does (``liners_duck_db``).
    """
    config = station.cfg.breaks
    reason = None
    library = (
        getattr(station.player, "_library", None)
        if station.player is not None
        else station.bridge.library
    )
    if config.enabled is True and (
        library is None or station.cfg.playback.transition_mode != "saved_points"
    ):
        reason = NEEDS_SAVED_POINTS
    return {
        "enabled": config.enabled is True,
        "dj_placement": config.dj_placement,
        "min_intro_seconds": float(config.min_intro_seconds),
        "duck_db": None if config.duck_db is None else float(config.duck_db),
        "liners_duck_db": float(station.cfg.playback.liners_duck_db),
        "reason": reason,
    }


def _pair_label(slot: dict[str, Any] | None) -> str:
    """ "Ending into Upcoming", by title, for a voice track's label."""
    if not slot:
        return ""
    names = [
        (slot.get(key) or {}).get("title") or "an unnamed song" for key in ("ending", "upcoming")
    ]
    return f"{names[0]} into {names[1]}"


def _voice_context(station: StationRuntime) -> dict[str, Any]:
    """What the voice tracker shows (blocking: it reads the catalog).

    The open song change, with where a voice track set to ``auto`` would
    go, the limits a recording must keep, and the voice tracks booked on
    this station, waiting ones first.
    """
    context: dict[str, Any] = (
        {
            "station": station.slug,
            "accepting": False,
            "reason": "Station is stopped",
            "now": None,
            "next": None,
            "slot": None,
        }
        if station.player is None
        else _context(station, voice_track=True)
    )
    desk: BreakDesk = station.breaks
    config = desk.config
    slot = context["slot"]
    if slot is not None:
        placement, note = placement_for(
            "dj", "auto", config.dj_placement, slot["upcoming_intro_s"], config.min_intro_seconds
        )
        slot["auto_placement"] = placement
        slot["auto_note"] = note or None
    return {
        "station": context["station"],
        "accepting": context["accepting"],
        "reason": context["reason"],
        "now": context["now"],
        "next": context["next"],
        "slot": slot,
        "limits": {
            "max_bytes": config.max_upload_bytes,
            "max_seconds": config.max_seconds,
            "sample_rate": SAMPLE_RATE,
            "dj_placement": config.dj_placement,
            "min_intro_s": config.min_intro_seconds,
            "max_per_slot": config.max_per_slot,
        },
        "tracks": [item.view() for item in desk.listing() if item.source == VOICE_TRACK],
    }


def _decode_cues(path: Path, max_seconds: float) -> BreakCues:
    """Decode an uploaded break within its limits and find its speech (blocking).

    Raises:
        HTTPException: 422 when the file is not audio AutoDJ can play, breaks
            the rate, channel or length limits, or holds no speech.
    """
    from autodj.break_audio import BreakAudioError, decode_break
    from autodj.stereo import TrackTooLongError

    try:
        audio = decode_break(path, max_seconds)
    except TrackTooLongError as exc:
        raise HTTPException(
            status_code=422, detail=f"A break may be at most {max_seconds:g} seconds long"
        ) from exc
    except BreakAudioError as exc:
        raise HTTPException(status_code=422, detail=f"That break cannot be used: {exc}") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail="That file is not audio AutoDJ can play"
        ) from exc
    try:
        return speech_cues(audio)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"That break is unusable: {exc}") from exc


async def _gated_decode(
    gate: asyncio.Semaphore,
    path: Path,
    max_seconds: float,
    decode: Callable[[Path, float], BreakCues] | None = None,
) -> BreakCues:
    """Decode an upload in a worker thread, one at a time across the server.

    The gate opens when the decode thread ends, not when the request does,
    so a client that hangs up cannot let a second decode start beside a
    running one.

    Raises:
        HTTPException: 503 with ``Retry-After`` when the gate stays shut
            for :data:`DECODE_WAIT_SECONDS`; as :func:`_decode_cues`.
    """
    run = decode or _decode_cues
    try:
        await asyncio.wait_for(gate.acquire(), DECODE_WAIT_SECONDS)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=503,
            detail="The server is decoding other breaks; try again shortly",
            headers={"Retry-After": "5"},
        ) from exc
    loop = asyncio.get_running_loop()
    try:
        future = _decode_pool().submit(run, path, max_seconds)
    except BaseException:
        gate.release()
        raise
    # The gate opens when the decode is over: finished, failed, or cancelled
    # before it started.  Never when the request gives up while it runs.
    future.add_done_callback(lambda _done: loop.call_soon_threadsafe(gate.release))
    return await asyncio.wrap_future(future)


# One decode at a time across the server, for breaks, voice tracks, matched
# intros and ad spots alike: decoding takes a CPU core.
_DECODE_GATES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def decode_gate() -> asyncio.Semaphore:
    """The server's one upload-decode gate, for the running event loop."""
    return _DECODE_GATES.setdefault(asyncio.get_running_loop(), asyncio.Semaphore(1))


def _quiet_unlink(path: Path) -> None:
    """Delete *path* if nothing holds it open."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


_DECODE_POOL: list[concurrent.futures.ThreadPoolExecutor] = []
_DECODE_POOL_LOCK = threading.Lock()


def _decode_pool() -> concurrent.futures.ThreadPoolExecutor:
    """The one thread that decodes uploads (made on first use)."""
    with _DECODE_POOL_LOCK:
        if not _DECODE_POOL:
            _DECODE_POOL.append(
                concurrent.futures.ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="autodj-break-decode"
                )
            )
        return _DECODE_POOL[0]


async def _within[T](deadline: float, work: Awaitable[T]) -> T:
    """*work*, or 408 once the loop's clock passes *deadline*."""
    remaining = deadline - asyncio.get_running_loop().time()
    try:
        return await asyncio.wait_for(work, max(0.0, remaining))
    except TimeoutError as exc:
        raise HTTPException(
            status_code=408, detail="The upload arrived too slowly; send it again"
        ) from exc


class _UploadFields(BaseModel):
    """The form fields of a break upload, besides the file."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["dj", "news", "weather", "music_news"]
    slot_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    ending_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    upcoming_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    placement: Literal["auto", "talk_up", "standalone"] = "auto"
    allow_outro: bool = False
    expires_s: int | None = Field(default=None, ge=30)
    label: str = Field(default="", max_length=200)


class _VoiceFields(_UploadFields):
    """The form fields of a voice track: a DJ break, so ``kind`` may be left out."""

    kind: Literal["dj"] = "dj"


async def _read_form[F: _UploadFields](
    request: Request, model: type[F]
) -> tuple[F, StarletteUploadFile]:
    """Parse the multipart body: one file and a few short fields, as *model*.

    Raises:
        HTTPException: 422 for a missing file, an unknown or bad field, or
            a body that is not a form of one file.
    """
    try:
        form = await request.form(max_files=1, max_fields=16, max_part_size=4096)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail="Send the break as multipart/form-data"
        ) from exc
    upload = form.get("file")
    values = {key: value for key, value in form.multi_items() if key != "file"}
    if not isinstance(upload, StarletteUploadFile) or any(
        not isinstance(value, str) for value in values.values()
    ):
        await form.close()
        raise HTTPException(status_code=422, detail="Send one audio file as the field file")
    try:
        fields = model.model_validate(values)
    except ValidationError as exc:
        await form.close()
        raise HTTPException(
            status_code=422,
            detail=[
                {"loc": [str(part) for part in error.get("loc", ())], "msg": str(error["msg"])}
                for error in exc.errors()
            ],
        ) from exc
    return fields, upload


__all__ = [
    "ANNOUNCER_PREFIX",
    "ANNOUNCER_SCOPES",
    "MULTIPART_OVERHEAD_BYTES",
    "UPLOAD_PATH",
    "VOICE_UPLOAD_PATH",
    "decode_gate",
    "install_breaks",
]
