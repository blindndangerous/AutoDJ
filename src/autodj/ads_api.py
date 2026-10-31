"""The Ads routes (#51): spots, ad breaks, their settings and the ad report.

Browser routes, with the signed-in session and, for a change, the page's
``Origin``, like every other station route.  They answer under ``/api/ads``
for the default station and ``/api/stations/<slug>/ads`` for any station by
name:

* ``GET ads``: the spots, the breaks, the settings, the runs waiting for a
  song change, the latest outcomes, the limits, and why the station does not
  air ads when it does not.
* ``POST ads/spots``: upload one spot as ``multipart/form-data``: the audio
  as ``file`` and its ``title``, ``advertiser`` and optionally ``campaign``,
  ``category``, ``start_date``, ``end_date``, ``max_per_day``,
  ``max_per_hour`` and ``enabled``.
* ``PUT ads/spots/<id>``: change what is said about a spot (JSON).
* ``GET ads/spots/<id>/audio``: the spot's audio, to listen to.
* ``DELETE ads/spots/<id>``: remove a spot and its audio.
* ``POST ads/breaks``, ``PUT ads/breaks/<id>``, ``DELETE ads/breaks/<id>``:
  add, change and remove an ad break (JSON).
* ``POST ads/breaks/<id>/run``: run a break at the next song change it can join.
* ``PUT ads/settings``: separation, late and missed times (JSON).
* ``GET ads/report?from=YYYY-MM-DD&to=YYYY-MM-DD``: what aired, late or
  missed, per advertiser and campaign, both days included, in the station
  time zone; ``GET ads/report/export`` gives it as CSV (``format=affidavit``
  for every spot, ``format=summary`` for the totals).

An upload is capped by ``[ads] max_upload_mb`` before its body is read (the
security middleware) and while it streams; it is stored under a name the
server makes up, then decoded off the event loop, one at a time, with the
announcer breaks' bounded decoder: a file that is not audio, holds no sound
or runs past ``[ads] max_spot_seconds`` is refused and deleted.  A JSON
change is at most :data:`JSON_BODY_MAX_BYTES`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import stat
import time
from datetime import date as Date
from datetime import datetime, timedelta
from datetime import time as Time
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import ValidationError

from autodj.ads import (
    FIELD_LABELS,
    MAX_BREAKS,
    MAX_REPORT_DAYS,
    MAX_SPOTS_PER_BREAK,
    SOURCES,
    AdBreak,
    AdDesk,
    AdsError,
    AdSettings,
    BreakFields,
    Spot,
    SpotFields,
    affidavit_csv,
    build_report,
    marks_between,
    new_id,
    report_rows,
    spot_file_name,
    summary_csv,
    valid_id,
)
from autodj.api_errors import error_headers
from autodj.breaks import BreakCues, speech_cues
from autodj.breaks_api import (
    MULTIPART_OVERHEAD_BYTES,
    _gated_decode,
    _quiet_unlink,
    _within,
    decode_gate,
)
from autodj.errors import FieldError, first_issue
from autodj.liner_files import LinerTooLargeError, store_liner_upload
from autodj.liners import LINER_EXTS
from autodj.station_routes import SavedStationDep, StationDep, StationRouter, audio_mime
from autodj.upload_forms import read_single_file_form

if TYPE_CHECKING:
    from autodj.stations import StationRuntime

logger = logging.getLogger(__name__)

#: The spot upload route and the break route, for the request-body caps.
UPLOAD_PATH = "/api/ads/spots"
BREAKS_PATH = "/api/ads/breaks"
#: The largest JSON body a change may send.
JSON_BODY_MAX_BYTES = 16 * 1024
#: An upload must arrive within this many seconds, plus one second per
#: 256 KiB it declares (408 past that).
BODY_DEADLINE_SECONDS = 60.0
MIN_BODY_BYTES_PER_SECOND = 256 * 1024
_NO_STORE = {"Cache-Control": "no-store"}
_FORM_FIELDS = frozenset(
    {
        "title",
        "advertiser",
        "campaign",
        "category",
        "start_date",
        "end_date",
        "max_per_day",
        "max_per_hour",
        "enabled",
    }
)


def _desk(station: StationRuntime) -> AdDesk:
    desk: AdDesk | None = getattr(station, "ads", None)
    if desk is None:
        raise HTTPException(status_code=503, detail="This station cannot keep ads")
    return desk


def _unreadable(exc: OSError) -> HTTPException:
    logger.warning("Could not use the ads file: %s", exc)
    return HTTPException(
        status_code=503, detail="The ads file could not be read just now; nothing was saved"
    )


async def _read_json(request: Request) -> Any:
    """The request's JSON body, at most :data:`JSON_BODY_MAX_BYTES`.

    Raises:
        HTTPException: 413 for a larger body, 422 for one that is not JSON.
    """
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdecimal() or int(declared) > JSON_BODY_MAX_BYTES):
        raise HTTPException(status_code=413, detail="The request is too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > JSON_BODY_MAX_BYTES:
            raise HTTPException(status_code=413, detail="The request is too large")
    try:
        return json.loads(bytes(body) or b"null")
    except (ValueError, RecursionError) as exc:
        raise HTTPException(status_code=422, detail="Send the change as JSON") from exc


def _form_values(values: dict[str, str]) -> dict[str, Any]:
    """An upload's text fields as :class:`SpotFields` takes them; empty means not set.

    Raises:
        AdsError: A field that is not one of the form's, or a number that is not one.
    """
    clean: dict[str, Any] = {}
    for name, value in values.items():
        if name not in _FORM_FIELDS:
            raise AdsError(name, f"Unknown field {name}")
        text = value.strip()
        if name in ("max_per_day", "max_per_hour"):
            if text and not text.isdecimal():
                raise AdsError(name, "Enter a whole number, or 0 for no limit")
            clean[name] = int(text) if text else 0
        elif name == "enabled":
            if text not in ("", "true", "false"):
                raise AdsError(name, "Airs must be true or false")
            clean[name] = text != "false"
        elif name in ("start_date", "end_date"):
            clean[name] = text or None
        else:
            clean[name] = value
    return clean


def _spot_cues(path: Path, max_seconds: float) -> BreakCues:
    """Decode an uploaded spot within its limits and find its sound (blocking).

    Raises:
        HTTPException: 422 when the file is not audio AutoDJ can play, breaks
            the rate, channel or length limits, or holds no sound.
    """
    from autodj.break_audio import BreakAudioError, decode_break
    from autodj.stereo import TrackTooLongError

    try:
        audio = decode_break(path, max_seconds)
    except TrackTooLongError as exc:
        raise HTTPException(
            status_code=422, detail=f"A spot may be at most {max_seconds:g} seconds long"
        ) from exc
    except BreakAudioError as exc:
        raise HTTPException(status_code=422, detail=f"That spot cannot be used: {exc}") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail="That file is not audio AutoDJ can play"
        ) from exc
    try:
        return speech_cues(audio)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"That spot is unusable: {exc}") from exc


# ----------------------------------------------------------------------
# Views
# ----------------------------------------------------------------------


def spot_view(
    spot_id: str,
    spot: Spot,
    today: Date,
    plays_today: dict[str, int],
    booked: set[str],
    folder: Path,
) -> dict[str, Any]:
    """One spot as the page shows it."""
    flight = spot.flight().view(today)
    return {
        "id": spot_id,
        "title": spot.title,
        "advertiser": spot.advertiser,
        "campaign": spot.campaign,
        "category": spot.category,
        "start_date": flight["not_before"],
        "end_date": flight["not_after"],
        "dates_text": str(flight["dates_text"]).replace("Plays", "Flight", 1),
        "in_flight": flight["playable"],
        "flight_text": flight["unplayable_text"],
        "max_per_day": spot.max_per_day,
        "max_per_hour": spot.max_per_hour,
        "enabled": spot.enabled,
        "seconds": round(spot.seconds, 1),
        "plays_today": plays_today.get(spot_id, 0),
        "booked": spot_id in booked,
        "missing": not (folder / spot.file).is_file(),
        "created_at": spot.created_at,
    }


def _clock_words(stamp: float, zone: Any) -> str:
    from autodj.clip_dates import day_words
    from autodj.timefmt import clock_text

    local = datetime.fromtimestamp(stamp, zone)
    return f"{local:%A} {day_words(local.date())} at {clock_text(local)}"


def break_view(break_id: str, item: AdBreak, now: float, zone: Any) -> dict[str, Any]:
    """One ad break as the page shows it, with its next run by the minutes past the hour."""
    following = None
    if item.enabled:
        found = marks_between(item, now, now + 8 * 86400, zone)
        following = found[0] if found else None
    return {
        "id": break_id,
        "name": item.name,
        "minutes": item.minutes,
        "days": item.days,
        "from_hour": item.from_hour,
        "until_hour": item.until_hour,
        "start_date": item.start_date.isoformat() if item.start_date else None,
        "end_date": item.end_date.isoformat() if item.end_date else None,
        "max_spots": item.max_spots,
        "max_seconds": item.max_seconds,
        "enabled": item.enabled,
        "when_text": item.when_text(),
        "next_at": (
            datetime.fromtimestamp(following, zone).isoformat(timespec="seconds")
            if following
            else None
        ),
        "next_text": _clock_words(following, zone) if following else "",
    }


def _listing(station: StationRuntime) -> dict[str, Any]:
    """What the Ads tab shows (blocking: it reads the store and looks at the files)."""
    from autodj.ads import row_view

    desk = _desk(station)
    config = station.cfg.ads
    state = desk.store.state()
    desk.noticed(state)
    now = desk.clock()
    zone = desk.zone
    today = datetime.fromtimestamp(now, zone).date()
    plays_today = desk.plays_today(now)
    booked = desk.booked_spots()
    spots = sorted(
        state.spots.items(),
        key=lambda pair: (pair[1].advertiser.casefold(), pair[1].title.casefold(), pair[0]),
    )
    breaks = sorted(state.breaks.items(), key=lambda pair: (pair[1].name.casefold(), pair[0]))
    late = state.settings.late_minutes
    return {
        "station": station.slug,
        "enabled": desk.enabled,
        "reason": desk.refusal(),
        "problem": desk.store.problem(),
        "timezone": desk.timezone,
        "limits": {
            "max_bytes": config.max_upload_bytes,
            "max_seconds": config.max_spot_seconds,
            "max_spots": config.max_spots,
            "max_breaks": MAX_BREAKS,
            "max_spots_per_break": MAX_SPOTS_PER_BREAK,
            "extensions": list(LINER_EXTS),
        },
        "settings": state.settings.model_dump(),
        "spots": [
            spot_view(key, spot, today, plays_today, booked, desk.store.folder)
            for key, spot in spots
        ],
        "breaks": [break_view(key, item, now, zone) for key, item in breaks],
        "pending": [
            {
                "break_name": run.break_name,
                "scheduled_text": _clock_words(run.scheduled, zone),
                "source_text": SOURCES.get(run.source, run.source),
            }
            for run in desk.pending()
        ],
        "recent": [row_view(record, zone, late) for record in desk.recent()],
    }


def _day(text: str, name: str) -> Date:
    try:
        return Date.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"{name} must be a date written YYYY-MM-DD"
        ) from exc


def _span(desk: AdDesk, from_text: str, to_text: str) -> tuple[datetime, datetime, Date, Date]:
    """The report's start and end instants, from two station-local days, both included."""
    first = _day(from_text, "From")
    last = _day(to_text, "To")
    if last < first:
        raise HTTPException(status_code=422, detail="To must be on or after From")
    start = datetime.combine(first, Time(0), desk.zone)
    end = datetime.combine(last + timedelta(days=1), Time(0), desk.zone)
    return start, end, first, last


def install_ads(router: StationRouter) -> None:
    """Add the Ads routes; each acts on the station its URL names."""
    # One decode at a time across the server, shared with breaks and intros.
    decoder = decode_gate

    @router.get("/ads")
    @error_headers(_NO_STORE)
    async def ads(station: SavedStationDep) -> JSONResponse:
        """The spots, the breaks, the settings and what is waiting."""
        _desk(station)
        return JSONResponse(await asyncio.to_thread(_listing, station), headers=_NO_STORE)

    @router.post("/ads/spots", status_code=201)
    @error_headers(_NO_STORE)
    async def spot_upload(station: SavedStationDep, request: Request) -> JSONResponse:
        """Upload one spot."""
        desk = _desk(station)
        config = station.cfg.ads
        cap = config.max_upload_bytes
        too_large = f"A spot file may be at most {config.max_upload_mb} MB"
        declared = request.headers.get("content-length")
        size = cap
        if declared is not None:
            if not declared.isdecimal() or int(declared) > cap + MULTIPART_OVERHEAD_BYTES:
                raise HTTPException(status_code=413, detail=too_large)
            size = min(cap, int(declared))
        if len(desk.store.cached().spots) >= config.max_spots:
            raise FieldError(
                f"This station keeps at most {config.max_spots} spots", "file", status=409
            )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BODY_DEADLINE_SECONDS + size / MIN_BODY_BYTES_PER_SECOND
        values, upload = await _within(deadline, read_single_file_form(request, "spot"))
        spot_id = new_id()
        path: Path | None = None
        saved = False
        try:
            try:
                suffix = Path(upload.filename or "").suffix.lower()
                if suffix not in LINER_EXTS:
                    raise FieldError(
                        "A spot must be one of these file types: " + ", ".join(LINER_EXTS),
                        "file",
                        status=415,
                    )
                try:
                    fields = SpotFields.model_validate(_form_values(values))
                except ValidationError as exc:
                    raise AdsError(
                        *first_issue(exc, FIELD_LABELS, fallback_label="The form")
                    ) from exc
                try:
                    path, _stored = await _within(
                        deadline,
                        store_liner_upload(
                            desk.store.folder,
                            spot_file_name(spot_id, suffix),
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
            cues = await _gated_decode(
                decoder(), path, float(config.max_spot_seconds), decode=_spot_cues
            )
            spot = Spot(
                **fields.model_dump(),
                file=path.name,
                cue_start=cues.start,
                cue_segue=cues.segue,
                cue_end=cues.end,
                length_s=cues.length,
                created_at=time.time(),
            )
            try:
                await asyncio.to_thread(desk.store.add_spot, spot_id, spot)
            except OSError as exc:
                raise _unreadable(exc) from exc
            saved = True
        finally:
            if not saved and path is not None:
                await asyncio.shield(asyncio.to_thread(_quiet_unlink, path))
        desk.noticed()
        logger.info("Ad spot %r for %s added", spot.title, spot.advertiser)
        now = desk.clock()
        today = datetime.fromtimestamp(now, desk.zone).date()
        view = spot_view(spot_id, spot, today, {}, set(), desk.store.folder)
        return JSONResponse(view, status_code=201, headers=_NO_STORE)

    @router.put("/ads/spots/{spot_id}")
    @error_headers(_NO_STORE)
    async def spot_update(station: SavedStationDep, request: Request, spot_id: str) -> JSONResponse:
        """Change what is said about a spot; its audio stays."""
        desk = _desk(station)
        body = await _read_json(request)
        if not valid_id(spot_id):
            raise HTTPException(status_code=404, detail="No spot has that ID")
        try:
            fields = SpotFields.model_validate(body)
        except ValidationError as exc:
            raise AdsError(*first_issue(exc, FIELD_LABELS, fallback_label="The form")) from exc
        try:
            spot = await asyncio.to_thread(desk.store.update_spot, spot_id, fields)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="No spot has that ID") from exc
        except OSError as exc:
            raise _unreadable(exc) from exc
        desk.noticed()
        now = desk.clock()
        today = datetime.fromtimestamp(now, desk.zone).date()
        view = await asyncio.to_thread(
            spot_view,
            spot_id,
            spot,
            today,
            desk.plays_today(now),
            desk.booked_spots(),
            desk.store.folder,
        )
        return JSONResponse(view, headers=_NO_STORE)

    @router.get("/ads/spots/{spot_id}/audio")
    @error_headers(_NO_STORE)
    async def spot_audio(station: SavedStationDep, spot_id: str) -> FileResponse:
        """Serve one spot's audio, to listen to."""
        desk = _desk(station)
        spot = (
            (await asyncio.to_thread(desk.store.state)).spots.get(spot_id)
            if valid_id(spot_id)
            else None
        )
        if spot is None:
            raise HTTPException(status_code=404, detail="No spot has that ID")
        path = desk.store.folder / spot.file
        try:
            metadata = await asyncio.to_thread(path.lstat)
        except OSError as exc:
            raise HTTPException(status_code=404, detail="That spot's audio is missing") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise HTTPException(status_code=404, detail="That spot's audio is missing")
        return FileResponse(
            path, media_type=audio_mime(path), stat_result=metadata, headers=_NO_STORE
        )

    @router.delete("/ads/spots/{spot_id}")
    @error_headers(_NO_STORE)
    async def spot_delete(station: SavedStationDep, spot_id: str) -> JSONResponse:
        """Remove one spot and its audio; one booked to air is kept until it has."""
        desk = _desk(station)
        if not valid_id(spot_id):
            raise HTTPException(status_code=404, detail="No spot has that ID")
        try:
            # Checked and removed while no break is being booked.
            removed = await asyncio.to_thread(desk.remove_spot, spot_id)
        except OSError as exc:
            raise _unreadable(exc) from exc
        if removed is None:
            raise HTTPException(status_code=404, detail="No spot has that ID")
        await asyncio.to_thread(desk.store.delete_file, removed)
        desk.noticed()
        return JSONResponse({"deleted": spot_id}, headers=_NO_STORE)

    async def break_fields(request: Request) -> BreakFields:
        body = await _read_json(request)
        try:
            return BreakFields.model_validate(body)
        except ValidationError as exc:
            raise AdsError(*first_issue(exc, FIELD_LABELS, fallback_label="The form")) from exc

    @router.post("/ads/breaks", status_code=201)
    @error_headers(_NO_STORE)
    async def break_add(station: SavedStationDep, request: Request) -> JSONResponse:
        """Add an ad break."""
        desk = _desk(station)
        fields = await break_fields(request)
        break_id = new_id()
        try:
            item = await asyncio.to_thread(desk.store.add_break, break_id, fields)
        except OSError as exc:
            raise _unreadable(exc) from exc
        desk.noticed()
        view = break_view(break_id, item, desk.clock(), desk.zone)
        return JSONResponse(view, status_code=201, headers=_NO_STORE)

    @router.put("/ads/breaks/{break_id}")
    @error_headers(_NO_STORE)
    async def break_update(
        station: SavedStationDep, request: Request, break_id: str
    ) -> JSONResponse:
        """Change an ad break."""
        desk = _desk(station)
        fields = await break_fields(request)
        if not valid_id(break_id):
            raise HTTPException(status_code=404, detail="No ad break has that ID")
        try:
            item = await asyncio.to_thread(desk.store.update_break, break_id, fields)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="No ad break has that ID") from exc
        except OSError as exc:
            raise _unreadable(exc) from exc
        desk.noticed()
        return JSONResponse(break_view(break_id, item, desk.clock(), desk.zone), headers=_NO_STORE)

    @router.delete("/ads/breaks/{break_id}")
    @error_headers(_NO_STORE)
    async def break_delete(station: SavedStationDep, break_id: str) -> JSONResponse:
        """Remove an ad break; a run of it still waiting is logged as missed."""
        desk = _desk(station)
        if not valid_id(break_id):
            raise HTTPException(status_code=404, detail="No ad break has that ID")
        try:
            removed = await asyncio.to_thread(desk.store.remove_break, break_id)
        except OSError as exc:
            raise _unreadable(exc) from exc
        if removed is None:
            raise HTTPException(status_code=404, detail="No ad break has that ID")
        desk.noticed()
        return JSONResponse({"deleted": break_id}, headers=_NO_STORE)

    @router.post("/ads/breaks/{break_id}/run", status_code=202)
    @error_headers(_NO_STORE)
    async def break_run(station: StationDep, break_id: str) -> JSONResponse:
        """Run an ad break at the next song change it can join."""
        desk = _desk(station)
        if not valid_id(break_id):
            raise HTTPException(status_code=404, detail="No ad break has that ID")
        reason = desk.refusal()
        if reason is not None:
            raise FieldError(reason, None, status=409)
        run = desk.request(break_id, "manual")
        return JSONResponse(
            {
                "break_name": run.break_name,
                "message": f"Ad break {run.break_name} airs at the next song change it can join.",
            },
            status_code=202,
            headers=_NO_STORE,
        )

    @router.put("/ads/settings")
    @error_headers(_NO_STORE)
    async def settings_update(station: SavedStationDep, request: Request) -> JSONResponse:
        """Save the separation, late and missed times."""
        desk = _desk(station)
        body = await _read_json(request)
        try:
            settings = AdSettings.model_validate(body)
        except ValidationError as exc:
            raise AdsError(*first_issue(exc, FIELD_LABELS, fallback_label="The form")) from exc
        try:
            await asyncio.to_thread(desk.store.set_settings, settings)
        except OSError as exc:
            raise _unreadable(exc) from exc
        desk.noticed()
        return JSONResponse(settings.model_dump(), headers=_NO_STORE)

    @router.get("/ads/report")
    @error_headers(_NO_STORE)
    async def report(
        station: SavedStationDep,
        from_text: Annotated[str, Query(alias="from", max_length=10)],
        to_text: Annotated[str, Query(alias="to", max_length=10)],
    ) -> JSONResponse:
        """What aired, late or on time, and what was missed, from one day to another."""
        desk = _desk(station)
        start, end, first, last = _span(desk, from_text, to_text)
        late = desk.store.cached().settings.late_minutes
        try:
            view = await asyncio.to_thread(build_report, desk.log, start, end, desk.zone, late)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OSError as exc:
            logger.warning("Could not read the ad play log: %s", exc)
            raise HTTPException(
                status_code=503, detail="The ad play log could not be read just now"
            ) from exc
        view.update({"station": station.slug, "from": first.isoformat(), "to": last.isoformat()})
        return JSONResponse(view, headers=_NO_STORE)

    @router.get("/ads/report/export")
    @error_headers(_NO_STORE)
    async def report_export(
        station: SavedStationDep,
        from_text: Annotated[str, Query(alias="from", max_length=10)],
        to_text: Annotated[str, Query(alias="to", max_length=10)],
        report_format: Annotated[Literal["affidavit", "summary"], Query(alias="format")] = (
            "affidavit"
        ),
    ) -> Response:
        """Download the report as CSV: every spot (``affidavit``) or the totals (``summary``)."""
        desk = _desk(station)
        start, end, first, last = _span(desk, from_text, to_text)
        late = desk.store.cached().settings.late_minutes

        def make() -> tuple[str, int]:
            if end - start > timedelta(days=MAX_REPORT_DAYS):
                raise ValueError(f"A report covers at most {MAX_REPORT_DAYS} days.")
            records, _damaged = desk.log.read(start, end)
            rows = report_rows(records, start, end)
            if report_format == "summary":
                return summary_csv(rows, late), len(rows)
            return affidavit_csv(rows, desk.zone, late), len(rows)

        try:
            body, count = await asyncio.to_thread(make)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OSError as exc:
            logger.warning("Could not read the ad play log: %s", exc)
            raise HTTPException(
                status_code=503, detail="The ad play log could not be read just now"
            ) from exc
        kind = "ads" if report_format == "affidavit" else "ad-summary"
        name = f"autodj-{kind}-{first:%Y-%m-%d}-to-{last:%Y-%m-%d}.csv"
        return Response(
            body.encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={
                **_NO_STORE,
                "Content-Disposition": f'attachment; filename="{name}"',
                "X-Row-Count": str(count),
            },
        )


__all__ = [
    "BREAKS_PATH",
    "JSON_BODY_MAX_BYTES",
    "UPLOAD_PATH",
    "break_view",
    "install_ads",
    "spot_view",
]
