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
import json
import logging
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from autodj.api_errors import error_headers
from autodj.breaks import BreakCues, new_id, speech_cues, valid_id
from autodj.breaks_api import (
    MULTIPART_OVERHEAD_BYTES,
    _gated_decode,
    _quiet_unlink,
    _within,
    decode_gate,
)
from autodj.errors import FieldError, first_issue
from autodj.intro_rules import MAX_RULES, IntroRules
from autodj.liner_files import LinerTooLargeError, store_liner_upload
from autodj.liners import LINER_EXTS
from autodj.matched_intros import (
    MAX_COOLDOWN_MINUTES,
    MAX_INTRO_SECONDS,
    TRACK_ID_PATTERN,
    Intro,
    MatchedIntros,
    file_name,
    parse_rules,
    rules_text,
)
from autodj.station_routes import SavedStationDep, StationRouter, audio_mime
from autodj.upload_forms import read_single_file_form

if TYPE_CHECKING:
    from autodj.stations import StationRuntime

logger = logging.getLogger(__name__)

#: The upload route, for the request-body cap.
UPLOAD_PATH = "/api/intros"
#: The intro rules route, and the most a rule's JSON may be, in bytes.
RULES_PATH = "/api/intros/rules"
RULE_BODY_MAX_BYTES = 64 * 1024
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


class RuleRefused(FieldError):
    """An intro rule that cannot be saved, naming the field at fault for the page."""

    def __init__(self, field_name: str, message: str, status: int = 422) -> None:
        super().__init__(message, field_name, status)


#: What each rule field is called on the page, for the errors.
_RULE_LABELS = {
    "match": "Match",
    "priority": "Priority",
    "label": "Label",
    "chance": "Chance",
    "cooldown_minutes": "Wait between plays",
    "not_before": "Not before",
    "not_after": "Do not play after",
    "filename_pattern": "Filename pattern",
}


def parse_rule(value: Any) -> Any:
    """An :class:`~autodj.intro_rules.IntroRule` from a request body, or :class:`RuleRefused`."""
    from autodj.intro_rules import IntroRule

    if not isinstance(value, dict):
        raise RuleRefused("", "Send an intro rule as a JSON object")
    if set(value) & {"created_at", "last_aired"}:
        raise RuleRefused("", "created_at and last_aired are kept by the server")
    try:
        return IntroRule.model_validate(value)
    except ValidationError as exc:
        raise RuleRefused(
            *first_issue(
                exc,
                _RULE_LABELS,
                fallback_field="clip_ids",
                prefix_unlabelled=False,
                strip_value_error=True,
                unknown_field=False,
            )
        ) from None


def rule_choices(station: StationRuntime, library: Any) -> dict[str, Any]:
    """What the page offers in a rule: the condition editor's choices (as a
    clockwheel's), and the station's registered voice liners and liner folders."""
    from autodj.station_routes import _category_names, _clock_beets, _clock_genres

    found: list[tuple[str, str, str]] = []
    for entry in library.entries_snapshot("liner"):
        record = library.catalog.record_for_path(entry.path)
        if record and record.get("kind") == "liner":
            path = Path(record["path"])
            found.append((str(record["id"]), path.name, str(path.parent)))
    counts: dict[str, int] = {}
    for _id, name, _folder in found:
        counts[name.casefold()] = counts.get(name.casefold(), 0) + 1
    # Two liners of one name are told apart by their folder.
    liners = [
        {"id": key, "label": f"{name} in {folder}" if counts[name.casefold()] > 1 else name}
        for key, name, folder in found
    ]
    liners.sort(key=lambda item: (item["label"].casefold(), item["id"]))
    folders = [str(source["path"]) for source in library.catalog.sources("liner")]
    known, _items, beets_error = _clock_beets(station, library).snapshot()
    return {
        "liners": liners,
        "liner_folders": folders,
        "sources": [
            {"id": source["id"], "path": source["path"]}
            for source in library.catalog.sources("music")
        ],
        "genres": _clock_genres.names(library),
        "beets_fields": sorted(known),
        "beets_error": beets_error,
        "categories": [
            {"id": key, "name": name} for key, name in _category_names(station, library).items()
        ],
    }


def validate_rule_clips(rule: Any, station: StationRuntime, library: Any) -> None:
    """Only this station's registered liners and liner folders may be chosen by a rule."""
    from autodj.clockwheel import _under
    from autodj.clockwheel_store import resolve_conditions

    for clip_id in rule.clip_ids:
        record = library.record_for_id(clip_id)
        if record is None or record.get("kind") != "liner":
            raise RuleRefused(
                "clip_ids", "A chosen liner is not one of this station's registered voice liners"
            )
    folder = rule.liner_folder
    if folder is not None and not any(
        _under(folder, source["path"]) for source in library.catalog.sources("liner")
    ):
        raise RuleRefused(
            "liner_folder", "Choose one of this station's registered voice liner folders"
        )
    if any(c["field"] == "folder" for c in rule.conditions):
        sources = {source["id"]: source for source in library.catalog.sources()}
        try:
            resolve_conditions({"type": "music", "conditions": rule.conditions}, sources, 1)
        except ValueError as exc:
            message = str(exc).removeprefix("Step 1: ")
            raise RuleRefused("conditions", message[:1].upper() + message[1:]) from None


def _inactive_reason(station: StationRuntime) -> str | None:
    """Why *station* does not play matched intros, or ``None`` when it does."""
    player = station.player
    if player._cfg.playback.transition_mode != "saved_points":
        return INACTIVE_REASON
    if getattr(player, "bus", None) is None:
        return BROWSER_REASON
    return None


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
        # Intros in the file that are not valid and are skipped, in words.
        "problem": store.problem(),
        "limits": {
            "max_bytes": (station.cfg.station.liner_upload_max_mib * 1024 * 1024),
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


def install_matched_intros(router: StationRouter) -> None:
    """Add the matched intros routes; each acts on the station its URL names."""
    # One decode at a time across the server, shared with breaks and ad spots.
    decoder = decode_gate

    def _rules(station: StationRuntime) -> IntroRules:
        rules: IntroRules | None = getattr(_store(station), "rules", None)
        if rules is None:
            raise HTTPException(status_code=503, detail="This station cannot keep intro rules")
        return rules

    async def rule_body(request: Request, station: StationRuntime) -> Any:
        """Read a bounded JSON rule and check its liners against the library."""
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > RULE_BODY_MAX_BYTES:
                raise RuleRefused("", "An intro rule may be at most 64 KiB", 413)
        try:
            value = json.loads(payload)
        except (ValueError, UnicodeError):
            raise RuleRefused("", "Send an intro rule as a JSON object") from None
        rule = parse_rule(value)
        await asyncio.to_thread(validate_rule_clips, rule, station, _library(station))
        return rule

    def _saved(key: str, rule: Any, status_code: int = 200) -> JSONResponse:
        return JSONResponse(
            {"id": key, **rule.model_dump(mode="json")}, status_code=status_code, headers=_NO_STORE
        )

    @router.get("/intros/rules")
    @error_headers(_NO_STORE, error_type=RuleRefused)
    async def intro_rules(station: SavedStationDep) -> JSONResponse:
        """The station's intro rules, and the liners and liner folders they may use."""
        store = _rules(station)
        rules = await asyncio.to_thread(store.all)
        library = station.bridge.library
        choices = (
            await asyncio.to_thread(rule_choices, station, library)
            if library is not None
            else {"liners": [], "liner_folders": [], "sources": [], "genres": []}
        )

        ordered = sorted(rules.items(), key=lambda item: (item[1].created_at, item[0]))
        return JSONResponse(
            {
                "rules": [{"id": key, **rule.model_dump(mode="json")} for key, rule in ordered],
                "problem": store.problem(),
                "max_rules": MAX_RULES,
                "max_cooldown_minutes": MAX_COOLDOWN_MINUTES,
                **choices,
            },
            headers=_NO_STORE,
        )

    @router.post("/intros/rules/test")
    @error_headers(_NO_STORE, error_type=RuleRefused)
    async def intro_rule_test(
        station: SavedStationDep,
        request: Request,
        path: str = Query(..., min_length=1, max_length=4096),
    ) -> JSONResponse:
        """Preview a filename pattern against this station's registered liner pool."""
        rule = await rule_body(request, station)
        if rule.filename_pattern is None:
            raise FieldError("Add a filename pattern to test", "filename_pattern", status=422)
        filename_pattern = rule.filename_pattern
        library = _library(station)
        record = await asyncio.to_thread(library.catalog.record_for_path, path)
        if record is None or record.get("kind", "music") != "music":
            raise FieldError("That song is not in the library", "path", status=404)
        song_entry = await asyncio.to_thread(library.entry_for_path, record["path"])
        if song_entry is None:
            raise FieldError("That song is not in the library", "path", status=404)
        from autodj.intro_rules import resolve_filename_pattern

        def scan() -> tuple[str | None, tuple[str, ...], list[dict[str, Any]]]:
            expanded, matcher, missing = resolve_filename_pattern(filename_pattern, song_entry)
            matches: list[dict[str, Any]] = []
            if matcher is None or missing:
                return expanded, missing, matches
            pool = IntroRules._pool(rule.liner_folder, tuple(library.entries_snapshot("liner")))
            now = time.time()
            for liner_entry in pool:
                name = Path(liner_entry.path).name
                if matcher.fullmatch(name) is None:
                    continue
                liner = library.catalog.record_for_path(liner_entry.path)
                if liner is None or liner.get("kind") != "liner":
                    continue
                liner_id = str(liner["id"])
                reason = (
                    station.liner_gate.refusal(liner_id, now)
                    if Path(liner_entry.path).is_file()
                    else "The liner file is missing"
                )
                eligible = reason is None
                matches.append(
                    {
                        "id": liner_id,
                        "label": name,
                        "eligible": eligible,
                        "reason": reason,
                    }
                )
                if len(matches) == 100:
                    break
            return expanded, missing, matches

        expanded, missing, matches = await asyncio.to_thread(scan)
        return JSONResponse(
            {
                "expanded_pattern": expanded,
                "missing_fields": list(missing),
                "matches": matches,
                "note": (
                    "This checks filename matches only; conditions, chance and cooldown are "
                    "not evaluated, and matches are not rolled or consumed."
                    + (
                        " Showing up to 100 matching files; narrow the pattern to inspect fewer results."
                        if len(matches) == 100
                        else ""
                    )
                ),
            },
            headers=_NO_STORE,
        )

    @router.post("/intros/rules", status_code=201)
    @error_headers(_NO_STORE, error_type=RuleRefused)
    async def intro_rule_create(station: SavedStationDep, request: Request) -> JSONResponse:
        store = _rules(station)
        fields = await rule_body(request, station)
        key = new_id()
        rule = fields.model_copy(update={"created_at": time.time()})
        try:
            await asyncio.to_thread(store.save, key, rule, create=True)
        except ValueError as exc:
            raise RuleRefused("", str(exc), 409) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail="The intro rules file could not be saved"
            ) from exc
        return _saved(key, rule, 201)

    @router.put("/intros/rules/{rule_id}")
    @error_headers(_NO_STORE, error_type=RuleRefused)
    async def intro_rule_edit(
        station: SavedStationDep, rule_id: str, request: Request
    ) -> JSONResponse:
        store = _rules(station)
        if not valid_id(rule_id):
            raise HTTPException(status_code=404, detail="No intro rule has that ID")
        fields = await rule_body(request, station)
        previous = (await asyncio.to_thread(store.all)).get(rule_id)
        if previous is None:
            raise HTTPException(status_code=404, detail="No intro rule has that ID")
        rule = fields.model_copy(
            update={"created_at": previous.created_at, "last_aired": previous.last_aired}
        )
        try:
            await asyncio.to_thread(store.save, rule_id, rule, create=False)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="No intro rule has that ID") from exc
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail="The intro rules file could not be saved"
            ) from exc
        return _saved(rule_id, rule)

    @router.delete("/intros/rules/{rule_id}")
    @error_headers(_NO_STORE, error_type=RuleRefused)
    async def intro_rule_delete(station: SavedStationDep, rule_id: str) -> JSONResponse:
        store = _rules(station)
        if not valid_id(rule_id):
            raise HTTPException(status_code=404, detail="No intro rule has that ID")
        try:
            removed = await asyncio.to_thread(store.remove, rule_id)
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail="The intro rules file could not be saved"
            ) from exc
        if not removed:
            raise HTTPException(status_code=404, detail="No intro rule has that ID")
        return JSONResponse({"deleted": rule_id}, headers=_NO_STORE)

    @router.get("/intros")
    async def intros(
        station: SavedStationDep,
        track_id: str | None = Query(None, pattern=TRACK_ID_PATTERN),
    ) -> JSONResponse:
        """The station's intros, or one song's and its artist's."""
        _store(station)
        return JSONResponse(await asyncio.to_thread(_listing, station, track_id), headers=_NO_STORE)

    @router.post("/intros", status_code=201)
    async def intro_upload(station: SavedStationDep, request: Request) -> JSONResponse:
        """Upload one intro for a song or for its artist."""
        from autodj.clip_dates import station_today

        store = _store(station)
        library = _library(station)
        cap = int((station.cfg.station.liner_upload_max_mib * 1024 * 1024))
        too_large = f"An intro file may be at most {cap // (1024 * 1024)} MB"
        declared = request.headers.get("content-length")
        size = cap
        if declared is not None:
            if not declared.isdecimal() or int(declared) > cap + MULTIPART_OVERHEAD_BYTES:
                raise HTTPException(status_code=413, detail=too_large)
            size = min(cap, int(declared))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BODY_DEADLINE_SECONDS + size / MIN_BODY_BYTES_PER_SECOND
        values, upload = await _within(deadline, read_single_file_form(request, "intro"))
        intro_id = new_id()
        path: Path | None = None
        saved = False
        try:
            try:
                suffix = Path(upload.filename or "").suffix.lower()
                if suffix not in LINER_EXTS:
                    raise FieldError(
                        "An intro must be one of these file types: " + ", ".join(LINER_EXTS),
                        "file",
                        status=415,
                    )
                try:
                    fields = _IntroFields.model_validate(values)
                except ValidationError as exc:
                    field, message = first_issue(exc, {})
                    raise FieldError(message, field or None, status=422) from exc
                rules = parse_rules(
                    {
                        "label": fields.label,
                        "chance": fields.chance,
                        "cooldown_minutes": fields.cooldown_minutes,
                        "not_before": fields.not_before,
                        "not_after": fields.not_after,
                    }
                )
                record = await asyncio.to_thread(library.record_for_id, fields.track_id)
                if record is None or record.get("kind", "music") != "music":
                    raise FieldError("That song is not in the library", "track_id", status=404)
                artist = str(record.get("artist") or "").strip()
                if fields.applies_to == "artist" and not artist:
                    raise FieldError(
                        "This song has no artist, so its intro can only be for this song",
                        "applies_to",
                        status=422,
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
    async def intro_audio(station: SavedStationDep, intro_id: str) -> FileResponse:
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
    async def intro_delete(station: SavedStationDep, intro_id: str) -> JSONResponse:
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
