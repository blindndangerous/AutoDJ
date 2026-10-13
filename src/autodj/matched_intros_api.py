"""The matched intros routes (#127): attach a voice clip to a song or its artist from Track info.

Browser routes, with the signed-in session and, for a change, the page's
``Origin``, like every other station route.  They answer under
``/api/intros`` for the default station and ``/api/stations/<slug>/intros``
for any station by name:

* ``GET intros``: every intro of the station, or with ``?track_id=<id>`` the
  song's own intros and its artist's, with the limits and whether this
  station plays them.
* ``POST intros``: upload one intro as ``multipart/form-data``: the audio as
  ``file``, ``track_id`` (the song's library ID), ``applies_to`` (``song``
  or ``artist``), and optionally ``label``, ``chance`` (1 to 100),
  ``cooldown_minutes`` (0 to a week), ``not_before`` and ``not_after``.
* ``GET intros/<id>/audio``: the intro's audio, to listen to.
* ``DELETE intros/<id>``: remove one intro and its audio.

An upload has the liner upload's limits: a file type a liner may be and at
most ``[server] liner_upload_max_mib``, capped by the security middleware
before the body is read.  It is streamed to the station's ``intros`` folder
under a name the server makes up, then decoded off the event loop, one at a
time, with the announcer breaks' bounded decoder
(:func:`autodj.break_audio.decode_break`): a file that is not audio, holds
no speech or runs past :data:`~autodj.matched_intros.MAX_INTRO_SECONDS` is
refused and deleted.
"""

from __future__ import annotations

import asyncio
import logging
import stat
import time
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile

from autodj.breaks import BreakCues, new_id, speech_cues, valid_id
from autodj.breaks_api import MULTIPART_OVERHEAD_BYTES, _gated_decode, _quiet_unlink, _within
from autodj.liner_files import LinerTooLargeError, store_liner_upload
from autodj.liners import LINER_EXTS
from autodj.matched_intros import (
    MAX_INTRO_SECONDS,
    TRACK_ID_PATTERN,
    Intro,
    IntroError,
    MatchedIntros,
    file_name,
    parse_rules,
    rules_text,
)
from autodj.station_routes import StationDep, StationRouter, audio_mime

if TYPE_CHECKING:
    from autodj.stations import StationRuntime

logger = logging.getLogger(__name__)

#: The upload route, for the request-body cap.
UPLOAD_PATH = "/api/intros"
#: An upload must arrive within this many seconds, plus one second per
#: 256 KiB it declares (408 past that).
BODY_DEADLINE_SECONDS = 60.0
MIN_BODY_BYTES_PER_SECOND = 256 * 1024
_NO_STORE = {"Cache-Control": "no-store"}
#: Why a station that does not mix with saved points does not play them.
INACTIVE_REASON = (
    "Matched intros play only with saved segue playback. In Settings, set Transition mode "
    "to Use saved segue points."
)
#: Why a station whose browser plays the audio does not play them.
BROWSER_REASON = (
    "Matched intros play only when the server mixes the audio, for server audio or a stream. "
    "This station plays in the browser."
)


class _IntroFields(BaseModel):
    """The form fields of an intro upload, besides the file."""

    model_config = ConfigDict(extra="forbid")
    track_id: str = Field(pattern=TRACK_ID_PATTERN)
    applies_to: Literal["song", "artist"] = "song"
    label: str = ""
    chance: str = ""
    cooldown_minutes: str = ""
    not_before: str = ""
    not_after: str = ""


def _store(station: StationRuntime) -> MatchedIntros:
    store: MatchedIntros | None = getattr(station, "intros", None)
    if store is None:
        raise HTTPException(status_code=503, detail="This station cannot keep matched intros")
    return store


def _library(station: StationRuntime) -> Any:
    library = station.bridge.library
    if library is None:
        raise HTTPException(status_code=503, detail="Matched intros need the folder library")
    return library


def _inactive_reason(station: StationRuntime) -> str | None:
    """Why *station* does not play matched intros, or ``None`` when it does."""
    player = station.player
    if player._cfg.playback.transition_mode != "saved_points":
        return INACTIVE_REASON
    if getattr(player, "bus", None) is None:
        return BROWSER_REASON
    return None


def _field_error(status: int, field: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": message, "field": field or None}, status_code=status)


def intro_view(
    intro_id: str, intro: Intro, today: Any, titles: dict[str, str] | None = None
) -> dict[str, Any]:
    """One intro as the page shows it."""
    title = (titles or {}).get(intro.track_id or "", "")
    target = f"Any song by {intro.artist}" if intro.artist is not None else "This song"
    return {
        "id": intro_id,
        "applies_to": intro.applies_to,
        "track_id": intro.track_id,
        "artist": intro.artist,
        "song_title": title or None,
        "target_text": target,
        "label": intro.label,
        "chance": intro.chance,
        "cooldown_minutes": intro.cooldown_minutes,
        "speech_s": round(intro.speech_s, 1),
        "rules_text": rules_text(intro),
        "created_at": intro.created_at,
        "last_aired": intro.last_aired,
        **intro.dates.view(today),
    }


def _listing(station: StationRuntime, track_id: str | None) -> dict[str, Any]:
    """What the page lists (blocking: it reads the file and the catalog)."""
    from autodj.clip_dates import station_today

    store = _store(station)
    library = station.bridge.library
    today = station_today(station.cfg)
    intros = store.all()
    reason = _inactive_reason(station)
    answer: dict[str, Any] = {
        "station": station.slug,
        "active": reason is None,
        "reason": reason,
        "limits": {
            "max_bytes": station.cfg.server.liner_upload_max_bytes,
            "max_seconds": MAX_INTRO_SECONDS,
            "extensions": list(LINER_EXTS),
        },
    }
    if track_id is None:
        titles: dict[str, str] = {}
        for intro in intros.values():
            if intro.track_id is not None and intro.track_id not in titles and library is not None:
                record = library.catalog.record_for_id(intro.track_id)
                titles[intro.track_id] = str(record.get("title") or "") if record else ""
        answer["intros"] = [
            intro_view(intro_id, intro, today, titles)
            for intro_id, intro in sorted(intros.items(), key=lambda item: item[1].created_at)
        ]
        return answer
    record = library.record_for_id(track_id) if library is not None else None
    if record is None:
        raise HTTPException(status_code=404, detail="That song is not in the library")
    artist = str(record.get("artist") or "")
    song_ids, artist_ids = store.candidates(track_id, artist)
    titles = {track_id: str(record.get("title") or "")}
    answer["track"] = {"id": track_id, "title": record.get("title"), "artist": artist or None}
    answer["song"] = [
        intro_view(intro_id, intros[intro_id], today, titles)
        for intro_id in sorted(song_ids, key=lambda key: intros[key].created_at)
        if intro_id in intros
    ]
    answer["artist"] = [
        intro_view(intro_id, intros[intro_id], today, titles)
        for intro_id in sorted(artist_ids, key=lambda key: intros[key].created_at)
        if intro_id in intros
    ]
    return answer


def _intro_cues(path: Path, max_seconds: float) -> BreakCues:
    """Decode an uploaded intro within its limits and find its speech (blocking).

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
            status_code=422, detail=f"An intro may be at most {max_seconds:g} seconds long"
        ) from exc
    except BreakAudioError as exc:
        raise HTTPException(status_code=422, detail=f"That intro cannot be used: {exc}") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail="That file is not audio AutoDJ can play"
        ) from exc
    try:
        return speech_cues(audio)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"That intro is unusable: {exc}") from exc


async def _read_form(request: Request) -> tuple[dict[str, str], StarletteUploadFile]:
    """Parse the multipart body: one file and a few short fields.

    Raises:
        HTTPException: 422 for a missing file or a body that is not a form
            of one file.
    """
    try:
        form = await request.form(max_files=1, max_fields=16, max_part_size=4096)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail="Send the intro as multipart/form-data"
        ) from exc
    upload = form.get("file")
    values = {key: value for key, value in form.multi_items() if key != "file"}
    if not isinstance(upload, StarletteUploadFile) or any(
        not isinstance(value, str) for value in values.values()
    ):
        await form.close()
        raise HTTPException(status_code=422, detail="Send one audio file as the field file")
    return {key: str(value) for key, value in values.items()}, upload


def install_matched_intros(router: StationRouter) -> None:
    """Add the matched intros routes; each acts on the station its URL names."""
    # One decode at a time; uploads share the breaks' one decode thread too.
    decoding: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
        weakref.WeakKeyDictionary()
    )

    def decoder() -> asyncio.Semaphore:
        return decoding.setdefault(asyncio.get_running_loop(), asyncio.Semaphore(1))

    @router.get("/intros")
    async def intros(
        station: StationDep,
        track_id: str | None = Query(None, pattern=TRACK_ID_PATTERN),
    ) -> JSONResponse:
        """The station's intros, or one song's and its artist's."""
        _store(station)
        return JSONResponse(await asyncio.to_thread(_listing, station, track_id), headers=_NO_STORE)

    @router.post("/intros", status_code=201)
    async def intro_upload(station: StationDep, request: Request) -> JSONResponse:
        """Upload one intro for a song or for its artist."""
        from autodj.clip_dates import station_today

        store = _store(station)
        library = _library(station)
        cap = int(station.cfg.server.liner_upload_max_bytes)
        too_large = f"An intro file may be at most {cap // (1024 * 1024)} MB"
        declared = request.headers.get("content-length")
        size = cap
        if declared is not None:
            if not declared.isdecimal() or int(declared) > cap + MULTIPART_OVERHEAD_BYTES:
                raise HTTPException(status_code=413, detail=too_large)
            size = min(cap, int(declared))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BODY_DEADLINE_SECONDS + size / MIN_BODY_BYTES_PER_SECOND
        values, upload = await _within(deadline, _read_form(request))
        intro_id = new_id()
        path: Path | None = None
        saved = False
        try:
            try:
                suffix = Path(upload.filename or "").suffix.lower()
                if suffix not in LINER_EXTS:
                    return _field_error(
                        415,
                        "file",
                        "An intro must be one of these file types: " + ", ".join(LINER_EXTS),
                    )
                try:
                    fields = _IntroFields.model_validate(values)
                except ValidationError as exc:
                    error = exc.errors()[0]
                    name = str((error.get("loc") or ("",))[0])
                    message = (
                        f"Unknown field {name}"
                        if error.get("type") == "extra_forbidden"
                        else f"{name}: {error['msg']}"
                    )
                    return _field_error(422, name, message)
                try:
                    rules = parse_rules(
                        {
                            "label": fields.label,
                            "chance": fields.chance,
                            "cooldown_minutes": fields.cooldown_minutes,
                            "not_before": fields.not_before,
                            "not_after": fields.not_after,
                        }
                    )
                except IntroError as exc:
                    return _field_error(422, exc.field, exc.message)
                record = await asyncio.to_thread(library.record_for_id, fields.track_id)
                if record is None or record.get("kind", "music") != "music":
                    return _field_error(404, "track_id", "That song is not in the library")
                artist = str(record.get("artist") or "").strip()
                if fields.applies_to == "artist" and not artist:
                    return _field_error(
                        422,
                        "applies_to",
                        "This song has no artist, so its intro can only be for this song",
                    )
                try:
                    path, _stored = await _within(
                        deadline,
                        store_liner_upload(
                            store.folder,
                            file_name(intro_id, suffix),
                            upload,
                            max_bytes=cap,
                            replace=False,
                        ),
                    )
                except LinerTooLargeError as exc:
                    raise HTTPException(status_code=413, detail=too_large) from exc
            finally:
                await upload.close()
            assert path is not None  # stored, or the request has already answered
            cues = await _gated_decode(decoder(), path, MAX_INTRO_SECONDS, decode=_intro_cues)
            intro = Intro(
                file=path.name,
                track_id=fields.track_id if fields.applies_to == "song" else None,
                artist=artist if fields.applies_to == "artist" else None,
                speech_s=cues.speech,
                created_at=time.time(),
                **rules,
            )
            try:
                await asyncio.to_thread(store.add, intro_id, intro)
            except IntroError as exc:
                return _field_error(409, exc.field, exc.message)
            except OSError as exc:
                logger.warning("Could not save a matched intro: %s", exc)
                raise HTTPException(
                    status_code=503,
                    detail="The matched intros file could not be read just now; nothing was saved",
                ) from exc
            saved = True
        finally:
            if not saved and path is not None:
                await asyncio.shield(asyncio.to_thread(_quiet_unlink, path))
        titles = {fields.track_id: str(record.get("title") or "")}
        view = intro_view(intro_id, intro, station_today(station.cfg), titles)
        return JSONResponse(view, status_code=201, headers=_NO_STORE)

    @router.get("/intros/{intro_id}/audio")
    async def intro_audio(station: StationDep, intro_id: str) -> FileResponse:
        """Serve one intro's audio, to listen to."""
        store = _store(station)
        intro = (await asyncio.to_thread(store.all)).get(intro_id) if valid_id(intro_id) else None
        if intro is None:
            raise HTTPException(status_code=404, detail="No intro has that ID")
        path = store.folder / intro.file
        try:
            metadata = await asyncio.to_thread(path.lstat)
        except OSError as exc:
            raise HTTPException(status_code=404, detail="That intro's audio is missing") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise HTTPException(status_code=404, detail="That intro's audio is missing")
        return FileResponse(
            path, media_type=audio_mime(path), stat_result=metadata, headers=_NO_STORE
        )

    @router.delete("/intros/{intro_id}")
    async def intro_delete(station: StationDep, intro_id: str) -> JSONResponse:
        """Remove one intro and its audio."""
        store = _store(station)
        if not valid_id(intro_id):
            raise HTTPException(status_code=404, detail="No intro has that ID")
        try:
            removed = await asyncio.to_thread(store.remove, intro_id)
        except OSError as exc:
            logger.warning("Could not remove a matched intro: %s", exc)
            raise HTTPException(
                status_code=503,
                detail="The matched intros file could not be read just now; nothing was removed",
            ) from exc
        if removed is None:
            raise HTTPException(status_code=404, detail="No intro has that ID")
        await asyncio.to_thread(store.delete_file, removed)
        return JSONResponse({"deleted": intro_id}, headers=_NO_STORE)


__all__ = [
    "BROWSER_REASON",
    "INACTIVE_REASON",
    "UPLOAD_PATH",
    "install_matched_intros",
    "intro_view",
]
