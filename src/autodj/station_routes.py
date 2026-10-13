"""The web routes that act on one station (#70).

Every route here works on the station its URL names.  The router is
mounted twice: under ``/api`` for the default station, so every address
that worked before still works, and under ``/api/stations/{slug}`` for
any station by name.  Each route takes ``station: StationDep``, which
:func:`current_station` resolves from the URL on every request: an
unknown name answers 404 and a stopped station 409.

Routes that act on the shared library, pairing or the server itself stay
on the app in :mod:`autodj.server`.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import fields
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeVar

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, ValidationError, model_validator
from pydantic import Field as PydanticField
from starlette.requests import HTTPConnection

from autodj._bridge import STREAM_SEEK_UNAVAILABLE, StreamSeekUnavailable
from autodj.clockwheel import parse_pal
from autodj.clockwheel_store import (
    OLD_CLOCK_MESSAGE,
    ClockwheelStore,
    from_pal,
    source_folder,
    step_source,
    step_types,
    validate_clockwheel,
    validate_name,
)
from autodj.config import MAX_RELAYS, RelayConfig, _relay_target
from autodj.playlog import (
    missing_fields,
    parse_bound,
    plays_between,
    plays_csv,
    soundexchange_filename,
    soundexchange_report,
)
from autodj.recorder import Recorder
from autodj.relay import resolve_safe_relay_addresses, source_port
from autodj.relay_settings import (
    credential_is_missing,
    password_variable,
    public_relay,
    save_relays,
)
from autodj.scheduler import Action
from autodj.settings_bodies import (
    BpmRangeBody,
    DiscoveryBody,
    DjMixBody,
    EqBody,
    FadeSeconds,
    FiniteFloat,
    PlaybackSettingsBody,
    PresetBody,
    StreamSettingsBody,
    TransitionBody,
    VolumeBody,
    validate_playback_choices,
)
from autodj.station_ledger import StationLedger, setting_key
from autodj.station_log import CURRENT_STATION
from autodj.stations import DEFAULT_SLUG, StationRuntime
from autodj.stream import EncoderUnavailableError

if TYPE_CHECKING:
    from autodj.listener_access import ListenerAccess

logger = logging.getLogger(__name__)

_Endpoint = TypeVar("_Endpoint", bound=Callable[..., Any])

# The prefix of the routes that name their station.
STATION_PREFIX = "/api/stations/{slug}"

_NO_STORE = {"Cache-Control": "no-store"}
# Play now, Play next and Add to queue on a search result whose file was
# pruned from the index since the search ran.
TRACK_GONE_DETAIL = "That track is no longer in the library."

_MIME_BY_SUFFIX = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
}


def audio_mime(path: Path) -> str:
    """Return MIME type associated with *path*'s audio suffix."""
    return _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")


# ---------------------------------------------------------------------------
# Which station a request is for
# ---------------------------------------------------------------------------


def resolve_station(connection: HTTPConnection) -> StationRuntime:
    """The station a request or WebSocket names; no name means the default.

    Raises:
        HTTPException: 404 when no station has that name, 409 when it is stopped.
    """
    stations = connection.app.state.stations
    slug = connection.path_params.get("slug")
    if slug is None:
        station: StationRuntime = stations.default
    else:
        try:
            station = stations.get(slug)
        except KeyError:
            raise HTTPException(status_code=404, detail="No station has that name") from None
    if station.stopped:
        raise HTTPException(status_code=409, detail="That station is stopped")
    return station


async def current_station(request: Request) -> StationRuntime:
    """The station this request's URL names: the FastAPI dependency.

    The rest of the request then runs as that station, so its log lines
    and the threads it starts carry the station's name.  (Async, so it
    runs in the request's own task rather than a worker thread.)
    """
    station = resolve_station(request)
    CURRENT_STATION.set(station.slug)
    return station


StationDep = Annotated[StationRuntime, Depends(current_station)]


async def public_station(request: Request) -> StationRuntime:
    """:func:`current_station` for a public route: a stopped station is 503 "off air".

    Listeners and outside sites see the same answer from a stopped
    station's feed as from its stream; the controls' 409 is for the page.
    """
    try:
        return await current_station(request)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        # Readable by other sites, like the feed itself.
        raise HTTPException(
            status_code=503,
            detail="That station is off air",
            headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
        ) from None


PublicStationDep = Annotated[StationRuntime, Depends(public_station)]


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class RecordingSettingsBody(BaseModel):
    """Runtime recording settings; the archive folder stays config-only."""

    model_config = ConfigDict(extra="forbid")

    split: Literal["hour", "show"]
    retention_days: StrictInt = PydanticField(ge=0)
    free_space_mb: StrictInt = PydanticField(ge=0)


class ListenerBanBody(BaseModel):
    """An address or CIDR to block from the public stream."""

    model_config = ConfigDict(extra="forbid")
    address: str
    minutes: int | None = PydanticField(default=None, ge=1, le=525600)
    note: str = PydanticField(default="", max_length=200)


class ProfileSaveBody(BaseModel):
    """Request body for POST /api/profiles.

    All snapshot fields are optional except ``name``; missing fields
    mean "do not override" when the profile is later applied.  Unknown
    fields are refused, as the stored profile would refuse them.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    preset: str | None = None
    bpm_lo: FiniteFloat | None = None
    bpm_hi: FiniteFloat | None = None
    harmonic_mode: str | None = None
    transition_mode: str | None = None
    post_queue_seed: str | None = None
    beat_sync_fx: bool | None = None
    key_sync_fx: bool | None = None
    beatmatch_on_skip: bool | None = None
    crossfade_seconds: FadeSeconds | None = None
    fade_in_seconds: FadeSeconds | None = None
    pure_shuffle: bool | None = None
    pick_mode: Literal["similarity", "random", "least_recent", "clock"] | None = None
    loudness_processing: Literal["off", "replaygain", "smooth", "stereotool"] | None = None
    anchor_to_seed: bool | None = None
    liners_enabled: bool | None = None
    liners_pick_mode: str | None = None

    @model_validator(mode="after")
    def _check_choices(self) -> ProfileSaveBody:
        """Refuse to store a profile that could never be applied."""
        validate_playback_choices(self.model_dump())
        return self


class RelaySettingsBody(BaseModel):
    """One relay definition submitted from Settings > Stream."""

    model_config = ConfigDict(extra="forbid")

    name: str = PydanticField(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    type: Literal["icecast", "shoutcast1", "shoutcast2"]
    host: str = PydanticField(min_length=1, max_length=253)
    port: int = PydanticField(ge=1, le=65535, strict=True)
    password: str | None = PydanticField(default=None, max_length=255)
    mount: str | None = PydanticField(default=None, max_length=255)
    user: str | None = PydanticField(default=None, max_length=255)
    stream_id: int | None = PydanticField(default=None, ge=1, le=2_147_483_647, strict=True)
    bitrate: Literal[128, 192, 256, 320] | None = None
    public: StrictBool = False
    enabled: StrictBool = True
    is_edit: StrictBool = False


@contextlib.contextmanager
def _profile_http_errors() -> Iterator[None]:
    """Answer a bad profile name or file with 400 and a missing profile with 404."""
    try:
        yield
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class SeekBody(BaseModel):
    """Request body for POST /api/seek.

    Either ``seconds`` (absolute) or ``delta`` (relative) is set.  When
    both are provided, ``seconds`` wins.
    """

    seconds: FiniteFloat | None = None
    delta: FiniteFloat | None = None


class PlayNextBody(BaseModel):
    """Request body for POST /api/play-next."""

    path: str
    now: bool = False  # if True, also skip the current track
    # DMCA mode: queue it even though it breaks a limit (the operator said so).
    confirm: StrictBool = False


class QueueReorderBody(BaseModel):
    """Request body for POST /api/queue/reorder.

    Attributes:
        paths: New ordering of the queue, expressed as a list of
            :attr:`~autodj.indexer.IndexEntry.path` strings.  The first
            entry plays after the current track finishes.
    """

    paths: list[str]


class LinerTestBody(BaseModel):
    """Request body for POST /api/liners/test.

    ``name`` names a specific clip; omitted, the scheduler's configured
    rotation mode picks the next one.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None


class ClockBody(BaseModel):
    """A native clockwheel from the editor; clockwheel_store validates the rest."""

    model_config = ConfigDict(extra="forbid")
    version: int
    name: str = PydanticField(max_length=64)
    steps: list[Any] = PydanticField(max_length=256)
    resync_minutes: list[Any] = PydanticField(max_length=60)


class ClockStepCheckBody(BaseModel):
    """One unsaved music or liner step to count in its matching pool."""

    model_config = ConfigDict(extra="forbid")
    step: dict[str, Any]


class PalImportBody(BaseModel):
    """PAL text is parsed as a small declarative subset, never executed."""

    model_config = ConfigDict(extra="forbid")
    text: str = PydanticField(max_length=256 * 1024)
    name: str = PydanticField(max_length=64)
    resync_minutes: list[Any] = PydanticField(max_length=60)


class ClockNameBody(BaseModel):
    """The clockwheel to make active, or null for none."""

    model_config = ConfigDict(extra="forbid")
    name: str | None


class NewClockNameBody(BaseModel):
    """The name for a copied or renamed clockwheel."""

    model_config = ConfigDict(extra="forbid")
    name: str = PydanticField(max_length=64)


# ---------------------------------------------------------------------------
# Helpers that act on one station
# ---------------------------------------------------------------------------


def _catalog_library(station: StationRuntime) -> Any:
    library = station.bridge.library
    if library is None:
        raise HTTPException(status_code=503, detail="Folder library is not attached")
    return library


def _require_stream_mode(station: StationRuntime) -> ListenerAccess:
    """404 unless the station streams."""
    if not station.bridge.stream_mode or station.listener_access is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return station.listener_access


def _refresh_relay_metadata(station: StationRuntime, registry: Any) -> None:
    with registry.relay_lock:
        station.bridge.relays.reconfigure(station.cfg.stream.relays.values(), restart_all=True)


def _persist_relay_set(station: StationRuntime, proposed: dict[str, RelayConfig]) -> None:
    """Save the authoritative set before changing live relays; restore bytes on failure."""
    from autodj.secret_files import write_private_file

    path = station.paths.relays
    try:
        previous = path.read_bytes() if path.exists() else None
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Could not save relay settings.") from exc
    try:
        save_relays(path, proposed, station.slug)
    except (OSError, ValueError) as exc:
        logger.error("Could not save relay settings", exc_info=True)
        raise HTTPException(status_code=503, detail="Could not save relay settings.") from exc
    try:
        station.bridge.relays.reconfigure(proposed.values())
    except Exception as exc:
        logger.error("Could not apply relay settings", exc_info=True)
        try:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                write_private_file(path, previous)
        except (OSError, UnicodeError):
            logger.error("Could not restore prior relay settings", exc_info=True)
        raise HTTPException(status_code=503, detail="Could not apply relay settings.") from exc
    station.cfg.stream.relays = proposed


def _save_stream_relay(station: StationRuntime, body: RelaySettingsBody, registry: Any) -> dict:
    # Keep serialization in the worker even if its HTTP request is cancelled.
    with registry.relay_lock:
        return _save_stream_relay_locked(station, body, registry)


def _save_stream_relay_locked(
    station: StationRuntime, body: RelaySettingsBody, registry: Any
) -> dict:
    old = station.cfg.stream.relays.get(body.name)
    if old is not None and not body.is_edit:
        raise HTTPException(status_code=409, detail="A relay with this name already exists.")
    if body.is_edit and old is None:
        raise HTTPException(status_code=404, detail="Relay not found.")
    variable = password_variable(station.slug, body.name)
    managed = variable in os.environ
    if managed and body.password:
        raise HTTPException(
            status_code=409, detail="This relay password is managed by the environment."
        )
    same_destination = old is not None and (
        old.host.lower().rstrip(".") == body.host.lower().rstrip(".")
        and old.port == body.port
        and old.type == body.type
    )
    if managed and old is not None and not same_destination:
        raise HTTPException(
            status_code=409,
            detail="The environment-managed password cannot be reused for a changed destination.",
        )
    password = (
        os.environ[variable]
        if managed
        else old.password
        if old is not None and same_destination and not body.password
        else body.password
    )
    if not password:
        raise HTTPException(status_code=422, detail="Enter a source password.")
    if credential_is_missing(password) and body.enabled:
        raise HTTPException(
            status_code=422, detail="Enter a source password before enabling this relay."
        )
    try:
        relay = RelayConfig.from_table(
            body.name,
            {
                "type": body.type,
                "host": body.host,
                "port": body.port,
                "password": password,
                "mount": body.mount,
                "user": body.user,
                "stream_id": body.stream_id,
                "bitrate": body.bitrate,
                "public": body.public,
                "enabled": body.enabled,
            },
        )
        resolve_safe_relay_addresses(relay.host, source_port(relay))
        proposed = dict(station.cfg.stream.relays)
        proposed[body.name] = relay
        if len(proposed) > MAX_RELAYS:
            raise ValueError("too many relays")
        targets = [_relay_target(item) for item in proposed.values()]
        if len(targets) != len(set(targets)):
            raise ValueError("duplicate destination")
        for slug, other_relays in registry.relay_configs().items():
            if slug == station.slug:
                continue
            other_targets = {_relay_target(item) for item in other_relays.values()}
            if other_targets.intersection(targets):
                raise ValueError("duplicate destination across stations")
    except OSError as exc:
        raise HTTPException(
            status_code=422, detail="Relay host could not be resolved to a public address."
        ) from exc
    except (TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"{body.name}: {exc}") from exc
    _persist_relay_set(station, proposed)
    statuses = {item["name"]: item for item in station.bridge.relay_status()}
    return {"relay": public_relay(relay, statuses.get(body.name, {}), station.slug)}


def _remove_stream_relay(station: StationRuntime, name: str, registry: Any) -> dict:
    with registry.relay_lock:
        return _remove_stream_relay_locked(station, name)


def _remove_stream_relay_locked(station: StationRuntime, name: str) -> dict:
    if name not in station.cfg.stream.relays:
        raise HTTPException(status_code=404, detail="Relay not found.")
    proposed = dict(station.cfg.stream.relays)
    proposed.pop(name)
    _persist_relay_set(station, proposed)
    return {"removed": name}


def _profile_store(station: StationRuntime) -> Any:
    """Return the station's ProfileStore."""
    from autodj.profiles import ProfileStore

    return ProfileStore(station.paths.profiles)


def _clock_store(station: StationRuntime) -> ClockwheelStore:
    return ClockwheelStore(station.paths.clockwheels)


def _clock_setting(station: StationRuntime, library: Any) -> dict:
    setting = library.catalog.get_setting(setting_key(station.slug, "clock"), {})
    return setting if isinstance(setting, dict) else {}


def _active_clock(station: StationRuntime, library: Any) -> str | None:
    setting = _clock_setting(station, library)
    name = setting.get("name")
    # Old single-clock data has a name too, but no file behind it.
    return name if isinstance(name, str) and "slots" not in setting else None


def _preset_names(station: StationRuntime) -> list[str]:
    from autodj.presets import BUILTIN_PRESETS

    return sorted(set(BUILTIN_PRESETS) | set(station.cfg.presets or {}))


def _clock_beets(station: StationRuntime, library: Any) -> Any:
    from autodj.clock_condition_data import BeetsConditions

    if not hasattr(library, "clock_beets"):
        library.clock_beets = BeetsConditions(station.cfg)
    return library.clock_beets


def _validate_step_conditions(
    station: StationRuntime, library: Any, step: dict, sources: dict, number: int
) -> None:
    from autodj.clockwheel_store import resolve_conditions

    categories = _category_names(library)
    for condition in resolve_conditions(step, sources, number):
        if condition["field"] == "category" and condition["category"] not in categories:
            raise ValueError(
                f"Step {number}: its category is not saved; choose one in Library tools, Categories"
            )
        if condition["field"] == "beets":
            known, _, error = _clock_beets(station, library).snapshot()
            if error:
                raise ValueError(f"Step {number}: {error}")
            if condition["name"] not in known:
                raise ValueError(f"Step {number}: Beets has no field named {condition['name']}")


def _category_names(library: Any) -> dict[str, str]:
    """Each saved rotation category's ID and name."""
    index = getattr(library, "categories", None)
    if index is None:
        return {}
    return {category.id: category.name for category in index.categories()}


def _validate_native_clock(station: StationRuntime, library: Any, value: Any) -> dict:
    """Validate *value*, then check its folders, tracks and presets exist."""
    clock = validate_clockwheel(value)
    sources = {source["id"]: source for source in library.catalog.sources()}
    categories = _category_names(library)
    for number, step in enumerate(clock["steps"], 1):
        kind = step["type"]
        if kind in {"music", "liner"}:
            _validate_step_conditions(station, library, step, sources, number)
        elif kind == "track":
            source = step_source(step, sources, number)
            target = source_folder(source["path"], step["path"])
            if library.entry_for_path(target) is None:
                raise ValueError(
                    f"Step {number}: {step['path']} is not a track in {source['path']}"
                )
        elif kind == "settings" and step["preset"] not in _preset_names(station):
            raise ValueError(f"Step {number}: there is no preset named {step['preset']}")
        elif kind == "category" and step["category"] not in categories:
            raise ValueError(
                f"Step {number}: its category is not saved; choose one in Library tools, Categories"
            )
    return clock


class _GenreCache:
    """The genre names last listed, with the music and liner entries they came from."""

    def __init__(self) -> None:
        self._music: Any = None
        self._liners: Any = None
        self._names: list[str] = []

    def names(self, library: Any) -> list[str]:
        """Every genre in the library, worked out again only when its entries change."""
        from autodj.clock_conditions import genres

        music, liners = library.entries_snapshot(), library.entries_snapshot("liner")
        if self._music is not music or self._liners is not liners:
            names = {genre for entry in music + liners for genre in genres(entry.genre)}
            self._music, self._liners = music, liners
            self._names = sorted(names, key=str.casefold)
        return list(self._names)


_clock_genres = _GenreCache()


def _clock_snapshot(station: StationRuntime, library: Any) -> dict:
    setting = _clock_setting(station, library)
    known, _, beets_error = _clock_beets(station, library).snapshot()
    player = station.player
    return {
        "names": _clock_store(station).list_names(),
        "active": _active_clock(station, library),
        "types": step_types(),
        "sources": [
            {"id": s["id"], "kind": s["kind"], "path": s["path"], "recursive": s["recursive"]}
            for s in library.catalog.sources()
        ],
        "presets": _preset_names(station),
        "genres": _clock_genres.names(library),
        "beets_fields": sorted(known),
        "beets_error": beets_error,
        "categories": [{"id": key, "name": name} for key, name in _category_names(library).items()],
        "cursor": (getattr(player, "_clock", None) or {}).get("cursor", 0),
        "last_status": getattr(player, "_clock_status", None),
        "pending_update": getattr(player, "_pending_clock_update", None) is not None,
        "notice": OLD_CLOCK_MESSAGE if "slots" in setting else None,
    }


async def select_clock(
    station: StationRuntime,
    library: Any,
    name: str | None,
    cursor: int | None = None,
    *,
    renamed_from: str | None = None,
) -> dict:
    """Make *name* the station's active clockwheel.

    Reselecting it keeps its place, as does renaming it (*renamed_from*
    is its old name; a rename does not change its conditions).
    """
    if name is not None and renamed_from is None:
        clock = _clock_store(station).load(name)
        sources = {source["id"]: source for source in library.catalog.sources()}
        for number, step in enumerate(clock["steps"], 1):
            if "conditions" in step:
                await asyncio.to_thread(
                    _validate_step_conditions, station, library, step, sources, number
                )
    # Through the station's ledger when it has one: its buffered cursor is
    # the latest place, and the old clockwheel's must not flush over this choice.
    ledger = station.ledger or StationLedger(library.catalog, station.slug)
    await asyncio.to_thread(ledger.select_clock, name, cursor, renamed_from)
    await asyncio.to_thread(station.player.configure_clock_after_selection)
    return await asyncio.to_thread(_clock_snapshot, station, library)


def _existing_clock(station: StationRuntime, name: str) -> dict:
    try:
        validate_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        return _clock_store(station).load(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def apply_profile(station: StationRuntime, name: str) -> dict:
    """Load a saved profile and push every set field through the station's bridge."""
    from autodj.profiles import ProfileSnapshot

    bridge = station.bridge
    with _profile_http_errors():
        snap = await asyncio.to_thread(_profile_store(station).load, name)

    # A preset the configuration no longer has would be dropped without a
    # word by set_preset, so the profile is refused before anything
    # changes, the same as a bad playback choice below.
    if snap.preset is not None:
        from autodj.presets import get_preset

        try:
            get_preset(snap.preset, station.cfg.presets)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Profile {name} uses preset {snap.preset!r}, which does not exist",
            ) from exc
    if snap.clockwheel is not None:
        try:
            _clock_store(station).load(snap.clockwheel)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    applied: list[str] = []
    # Playback flags: every snapshot field not applied on its own below.
    kw: dict = {}
    for fld in fields(ProfileSnapshot):
        if fld.name in {
            "name",
            "preset",
            "bpm_lo",
            "bpm_hi",
            "harmonic_mode",
            "cart_page",
            "clockwheel",
        }:
            continue
        v = getattr(snap, fld.name)
        if v is not None:
            kw[fld.name] = v
            applied.append(fld.name)
    # A profile saved before its fields were validated, or edited by hand,
    # can hold a bad value.  Both bodies are validated before any setter
    # runs, so a rejected profile leaves the session untouched.
    try:
        playback = PlaybackSettingsBody.model_validate(kw)
        bpm = BpmRangeBody(lo=snap.bpm_lo, hi=snap.bpm_hi)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Profile {name} is invalid: {exc}") from exc
    # The profile names its cart page.  A page renamed since then refuses
    # the profile before anything changes, like a missing preset.
    carts = station.carts
    cart_page = None
    if snap.cart_page is not None:
        cart_page = next(
            (
                number
                for number, page in enumerate(carts.store.wall.pages, 1)
                if page.name.casefold() == snap.cart_page.casefold()
            ),
            None,
        )
        if cart_page is None:
            raise HTTPException(
                status_code=400,
                detail=f"Profile {name} uses cart page {snap.cart_page!r}, which does not exist",
            )
    try:
        await asyncio.to_thread(bridge.set_playback_settings, playback)
    except ValueError as exc:
        # Refused before anything changed, such as Stereo Tool already
        # processing another station.
        raise HTTPException(status_code=400, detail=f"Profile {name}: {exc}") from exc
    if snap.clockwheel is not None:
        await select_clock(station, _catalog_library(station), snap.clockwheel)
        applied.append("clockwheel")
    # BPM range
    if bpm.lo is not None and bpm.hi is not None:
        bridge.set_bpm_range(bpm.lo, bpm.hi)
        applied.append("bpm_range")
    # DJ-mix harmonic mode
    if snap.harmonic_mode is not None:
        bridge.set_djmix(harmonic_mode=snap.harmonic_mode)
        applied.append("harmonic_mode")
    # Preset, checked above
    if snap.preset is not None:
        bridge.set_preset(snap.preset)
        applied.append("preset")
    if cart_page is not None:
        carts.store.wall.active_page = cart_page
        await carts.changed()
        applied.append("cart_page")
    bridge.save_persistent_state()
    if station.recorder is not None:
        station.recorder.show_changed()
    return {"applied": applied, "name": name}


async def _recording_call(
    station: StationRuntime, call: Callable[[Recorder], dict[str, object]]
) -> dict[str, object]:
    """Run *call* on the station's recorder off the event loop (it saves a state file).

    Raises:
        ValueError: Not in stream mode, or the call is not possible now.
        OSError: The change was made but its state file could not be saved.
    """
    recorder = station.recorder
    if recorder is None:
        raise ValueError("Recording requires stream mode")
    return await asyncio.to_thread(call, recorder)


async def _recording_route(
    station: StationRuntime, call: Callable[[Recorder], dict[str, object]]
) -> dict[str, object]:
    try:
        return await _recording_call(station, call)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        logger.error("Could not save the recording state", exc_info=True)
        raise HTTPException(status_code=503, detail="Could not save the recording state") from exc


# A liner without play dates, as the liner list shows it.
_NO_DATES: dict[str, Any] = {
    "not_before": None,
    "not_after": None,
    "dates_text": "",
    "playable": True,
    "unplayable_text": "",
}


async def _forget_new_liner_dates(station: StationRuntime, target: Path) -> None:
    """Forget dates left by an earlier liner at *target*: by its name and its catalog ID.

    Raises:
        OSError: The dates file could not be read, so nothing was forgotten.
    """
    library = station.bridge.library
    ids = [target.name]
    if library is not None:
        record = await asyncio.to_thread(library.catalog.record_for_path, target)
        if record is not None:
            ids.append(str(record["id"]))
    await asyncio.to_thread(station.liner_gate.store.forget, ids)


async def _forget_liner_dates(station: StationRuntime, liner_id: str) -> dict[str, str]:
    """Drop a deleted liner's play dates, so a new file of that name starts without them.

    Returns:
        Nothing when that worked; otherwise a ``message`` for the page,
        saying the dates stay until the dates file can be read again.
    """
    from autodj.clip_dates import ClipDates

    try:
        await asyncio.to_thread(station.liner_gate.store.set, liner_id, ClipDates())
    except OSError:
        logger.warning("Could not forget the play dates of liner %s", liner_id, exc_info=True)
        return {
            "message": "Its play dates could not be forgotten just now, because the dates file "
            "could not be read; a scan forgets them later"
        }
    return {}


def _liner_names(station: StationRuntime) -> list[str]:
    from autodj.liners import LinerLibrary

    return [path.name for path in LinerLibrary.from_folder(station.paths.liners).files]


def _dmca_refusal(
    station: StationRuntime, body: PlayNextBody, *, append: bool
) -> JSONResponse | None:
    """A 409 naming the DMCA limits a queued song would break, unless *body* confirms it.

    The song is not queued until the operator confirms; a confirmed
    song is queued and the limits it breaks are logged.
    """
    bridge = station.bridge
    if not station.cfg.dmca.enabled:
        return None
    entry = bridge.entry_for_path(body.path)
    if entry is None:
        raise HTTPException(status_code=404, detail=TRACK_GONE_DETAIL)
    now = body.now and not append
    broken = bridge.dmca_queue_limits(entry, now=now, append=append)
    if not broken:
        return None
    limits = "; ".join(broken)
    if body.confirm:
        logger.warning(
            "DMCA mode: the operator queued %s although it breaks the limit: %s",
            entry.display_name,
            limits,
        )
        return None
    where = "now" if now else "at the end of the queue" if append else "next"
    return JSONResponse(
        status_code=409,
        content={
            "detail": f"DMCA mode: playing this song {where} would mean {limits}.",
            "field": "confirm",
        },
    )


# ---------------------------------------------------------------------------
# A station's schedule, carts and time checks
# ---------------------------------------------------------------------------


def equip_station(
    station: StationRuntime, publish: Callable[[dict[str, Any]], Awaitable[None]]
) -> None:
    """Build the station's cart wall, scheduler and time checks; start nothing.

    Features with scheduler actions register them here, before the
    station starts its scheduler.  *publish* sends a message to the
    station's WebSocket clients.
    """
    from autodj.carts import StationCarts
    from autodj.scheduler_api import build_scheduler
    from autodj.time_checks import TimeChecks, time_check_action

    bridge = station.bridge
    api_root = "/api" if station.slug == DEFAULT_SLUG else f"/api/stations/{station.slug}"
    station.carts = StationCarts(
        bridge,
        publish,
        lambda: station.paths.liners,
        station.paths.state_dir,
        api_root,
        # Looked up at each call: the registry sets it after this.
        owns_liner=lambda path: station.owns_liner(path),
    )

    async def profile(name: str) -> dict:
        return await apply_profile(station, name)

    async def clock(name: str) -> dict:
        return await select_clock(station, _catalog_library(station), name)

    schedule = build_scheduler(
        bridge,
        path=station.paths.schedule,
        timezone=station.timezone,
        apply_profile=profile,
        profile_names=lambda: _profile_store(station).list_names(),
        liner_names=lambda: _liner_names(station),
        clock_names=lambda: _clock_store(station).list_names(),
        select_clock=clock,
        publish=publish,
    )
    station.schedule = schedule
    schedule.register(station.carts.action)

    for name, description, call in (
        ("start", "Start recording", Recorder.start),
        ("stop", "Stop recording", Recorder.stop),
        ("split", "Split recording", Recorder.split),
    ):

        async def run_recording(
            _params: dict[str, str], call: Callable[[Recorder], dict[str, object]] = call
        ) -> None:
            try:
                await _recording_call(station, call)
            except OSError as exc:
                # The run log reaches the browser: no index path in it.
                logger.error("Could not save the recording state", exc_info=True)
                raise RuntimeError("Could not save the recording state") from exc

        schedule.register(Action(f"recording_{name}", description, run_recording, interrupt="none"))

    station.time_checks = TimeChecks(station.cfg, station.timezone, station.paths)
    schedule.register(time_check_action(station.time_checks, bridge))

    from autodj.breaks import BreakDesk

    station.breaks = BreakDesk(lambda: station.cfg.breaks, station.paths.announcements)
    bridge.player.break_desk = station.breaks

    requests_cfg = station.cfg.requests
    if requests_cfg.enabled is True:
        from autodj.listener_requests import (
            FORM_TOKENS,
            TRACK_SECRET,
            RequestBook,
            RequestDesk,
            TrackIds,
        )

        station.requests = RequestDesk(
            station.slug,
            requests_cfg,
            bridge,
            RequestBook(station.paths.listener_requests, timezone=station.timezone),
            music_dir=Path(station.cfg.library.music_dir),
            tokens=FORM_TOKENS,
            track_ids=TrackIds(TRACK_SECRET),
            on_queued=station.nowplaying.refresh,
        )

    from autodj.categories import station_wall
    from autodj.matched_intros import MatchedIntros

    # Play dates are judged by the station's day when the intro airs.
    station.intros = MatchedIntros(
        station.paths.matched_intros,
        station.paths.intros,
        day_at=lambda at: station_wall(station.cfg, at).date(),
    )
    bridge.player.matched_intros = station.intros


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------


class StationRouter:
    """Registers each station route once under every prefix it answers on.

    A route is written relative to the API root (``/skip``) and becomes
    ``/api/skip`` for the default station and
    ``/api/stations/{slug}/skip`` for any station by name.  The routes
    are added to the app's own router, each with its full path, so
    ``app.routes`` lists every address and the audit log names it.
    (``include_router`` would do the same, but this FastAPI version keeps
    included routers as one opaque entry.)
    """

    def __init__(
        self, router: APIRouter, prefixes: Sequence[str] = ("/api", STATION_PREFIX)
    ) -> None:
        """Add routes to *router* under each of *prefixes*."""
        self._router = router
        self.prefixes = tuple(prefixes)

    def api_route(
        self, path: str, *, methods: Sequence[str], **options: Any
    ) -> Callable[[_Endpoint], _Endpoint]:
        """Register the decorated endpoint for *methods* on *path* under every prefix."""

        def register(endpoint: _Endpoint) -> _Endpoint:
            for prefix in self.prefixes:
                self._router.add_api_route(
                    prefix + path, endpoint, methods=list(methods), **options
                )
            return endpoint

        return register

    def get(self, path: str, **options: Any) -> Callable[[_Endpoint], _Endpoint]:
        """Register a GET route."""
        return self.api_route(path, methods=["GET"], **options)

    def post(self, path: str, **options: Any) -> Callable[[_Endpoint], _Endpoint]:
        """Register a POST route."""
        return self.api_route(path, methods=["POST"], **options)

    def put(self, path: str, **options: Any) -> Callable[[_Endpoint], _Endpoint]:
        """Register a PUT route."""
        return self.api_route(path, methods=["PUT"], **options)

    def delete(self, path: str, **options: Any) -> Callable[[_Endpoint], _Endpoint]:
        """Register a DELETE route."""
        return self.api_route(path, methods=["DELETE"], **options)


def install_station_routes(router: StationRouter) -> None:
    """Register every station route, with paths relative to the API root (``/skip``, ...)."""
    from autodj.breaks_api import install_breaks
    from autodj.carts import install_carts
    from autodj.hour_preview import install_hour_preview
    from autodj.matched_intros_api import install_matched_intros
    from autodj.requests_api import install_requests
    from autodj.rotation_api import install_rotation_test
    from autodj.scheduler_api import install_scheduler
    from autodj.time_checks import install_time_checks

    @router.get("/status")
    async def api_status(station: StationDep) -> JSONResponse:
        """Return the current player state snapshot."""
        state = await asyncio.to_thread(station.bridge.get_state)
        if station.recorder is not None:
            state["recording"] = station.recorder.status()
        return JSONResponse(state)

    @router.api_route("/nowplaying", methods=["GET", "HEAD"])
    async def api_nowplaying(station: PublicStationDep) -> JSONResponse:
        """Return public metadata with no file paths or pairing details."""
        return JSONResponse(
            station.nowplaying.snapshot(),
            headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
        )

    @router.get("/history")
    async def api_history(
        station: StationDep,
        page: int = Query(1, ge=1),
        per_page: int = Query(50, ge=1, le=500),
    ) -> JSONResponse:
        """Return recent play history across restarts (newest first), one page."""
        hist = station.bridge.history_snapshot()
        total = len(hist)
        pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, pages))
        start = (page - 1) * per_page
        end = start + per_page
        # Slice from the "newest-first" perspective: index ``start`` from
        # the end of the deque becomes ``total - 1 - start`` walking back.
        if total == 0:
            items: list = []
        else:
            hi = total - start
            lo = max(0, total - end)
            items = list(reversed([hist[i] for i in range(lo, hi)]))
        return JSONResponse({"items": items, "total": total, "page": page, "pages": pages})

    @router.get("/history/export")
    async def api_history_export(
        station: StationDep,
        from_text: Annotated[str, Query(alias="from", max_length=64)],
        to_text: Annotated[str, Query(alias="to", max_length=64)],
        report_format: Annotated[Literal["csv", "soundexchange"], Query(alias="format")] = "csv",
        service: Annotated[str, Query(max_length=200)] = "",
        category: Annotated[str, Query(max_length=1)] = "A",
    ) -> Response:
        """Download the play log between two dates (see :mod:`autodj.playlog`).

        Dates are ``YYYY-MM-DD`` in the server's time zone and ``to`` is
        included.  ``format=soundexchange`` gives a Report of Use for
        *service* and transmission *category* instead of the CSV.
        ``X-Play-Count`` and ``X-Songs-Missing-Fields`` give the page the
        numbers it announces.
        """
        library = _catalog_library(station)
        try:
            start = parse_bound(from_text)
            end = parse_bound(to_text, end=True)
            plays = await asyncio.to_thread(
                functools.partial(plays_between, station=station.slug), library.catalog, start, end
            )
            last_day = end - timedelta(microseconds=1)
            if report_format == "soundexchange":
                body = soundexchange_report(plays, service, category)
                media_type = "text/plain; charset=us-ascii"
                name = soundexchange_filename(service, start, last_day)
            else:
                body = plays_csv(plays)
                media_type = "text/csv; charset=utf-8"
                name = f"autodj-plays-{start:%Y-%m-%d}-to-{last_day:%Y-%m-%d}.csv"
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        missing = missing_fields(plays)
        return Response(
            body.encode("utf-8"),
            media_type=media_type,
            headers={
                **_NO_STORE,
                "Content-Disposition": f'attachment; filename="{name}"',
                "X-Play-Count": str(len(plays)),
                "X-Songs-Missing-Fields": str(len(missing)),
                "X-Songs-Unmatchable": str(sum(1 for item in missing if item[5] == "no")),
            },
        )

    @router.post("/skip")
    async def api_skip(station: StationDep) -> JSONResponse:
        """Skip the current track immediately."""
        station.bridge.skip()
        # Return fresh state so the browser updates its now-playing UI
        # without waiting up to 1 s for the next WS broadcast tick.
        return JSONResponse(await asyncio.to_thread(station.bridge.get_state))

    @router.post("/seek")
    async def api_seek(station: StationDep, body: SeekBody) -> dict[str, float]:
        """Seek the active track (absolute or relative seconds)."""
        try:
            new_pos = station.bridge.seek(seconds=body.seconds, delta=body.delta)
        except StreamSeekUnavailable as exc:
            raise HTTPException(status_code=409, detail=STREAM_SEEK_UNAVAILABLE) from exc
        return {"elapsed": round(new_pos, 2)}

    # ------------------------------------------------------------------
    # Stream, recording and webhooks (stream mode only; 404 otherwise)
    # ------------------------------------------------------------------

    @router.get("/stream")
    async def api_stream_info(station: StationDep) -> dict:
        """Return the stream URL paths, bitrate, listener count and state."""
        _require_stream_mode(station)
        return station.bridge.stream_info()

    @router.get("/stream/relays")
    async def api_stream_relays(station: StationDep) -> dict:
        _require_stream_mode(station)
        statuses = {item["name"]: item for item in station.bridge.relay_status()}
        return {
            "relays": [
                public_relay(cfg, statuses.get(name, {}), station.slug)
                for name, cfg in station.cfg.stream.relays.items()
            ]
        }

    @router.post("/stream/relays")
    async def api_stream_relay_save(
        station: StationDep, body: RelaySettingsBody, request: Request
    ) -> dict:
        _require_stream_mode(station)
        registry = request.app.state.stations
        return await asyncio.to_thread(_save_stream_relay, station, body, registry)

    @router.delete("/stream/relays/{name}")
    async def api_stream_relay_remove(station: StationDep, name: str, request: Request) -> dict:
        _require_stream_mode(station)
        registry = request.app.state.stations
        return await asyncio.to_thread(_remove_stream_relay, station, name, registry)

    @router.get("/recording")
    async def api_recording_status(station: StationDep) -> dict[str, object]:
        return await _recording_route(station, Recorder.status)

    @router.post("/recording/start")
    async def api_recording_start(station: StationDep) -> dict[str, object]:
        return await _recording_route(station, Recorder.start)

    @router.post("/recording/stop")
    async def api_recording_stop(station: StationDep) -> dict[str, object]:
        return await _recording_route(station, Recorder.stop)

    @router.post("/recording/split")
    async def api_recording_split(station: StationDep) -> dict[str, object]:
        return await _recording_route(station, Recorder.split)

    @router.post("/recording/settings")
    async def api_recording_settings(
        station: StationDep, body: RecordingSettingsBody
    ) -> dict[str, object]:
        return await _recording_route(
            station,
            lambda target: target.configure(body.split, body.retention_days, body.free_space_mb),
        )

    @router.get("/webhooks")
    async def api_webhooks(station: StationDep) -> list[dict[str, str | int]]:
        """Show redacted webhook destinations to a paired browser."""
        return station.webhooks.status()

    @router.post("/webhooks/{index}/test")
    async def api_webhook_test(station: StationDep, index: int) -> dict[str, bool]:
        """Send the current public snapshot to one configured webhook."""
        webhooks = station.webhooks
        if index < 0 or index >= len(webhooks.configs):
            raise HTTPException(status_code=404, detail="Webhook not found")
        result = webhooks.submit(station.nowplaying.snapshot(), index)
        assert result is not None
        try:
            delivered = await asyncio.wait_for(asyncio.wrap_future(result), timeout=20)
        except TimeoutError:
            delivered = False
        return {"delivered": delivered}

    @router.get("/stream/listeners")
    async def api_stream_listeners(station: StationDep) -> dict:
        access = _require_stream_mode(station)
        return {
            "listeners": await asyncio.to_thread(access.listeners),
            "revision": access.revision,
            "password_set": bool(station.cfg.stream.listener_password),
            "max_per_address": access.max_per_address,
        }

    @router.delete("/stream/listeners/{ident}")
    async def api_stream_kick(station: StationDep, ident: str) -> dict:
        access = _require_stream_mode(station)
        address = await asyncio.to_thread(access.kick, ident)
        if address is None:
            raise HTTPException(status_code=404, detail="Listener already disconnected")
        return {"address": address}

    @router.get("/stream/bans")
    async def api_stream_bans(station: StationDep) -> dict:
        access = _require_stream_mode(station)
        return {"bans": await asyncio.to_thread(access.bans)}

    @router.post("/stream/bans")
    async def api_stream_ban(station: StationDep, body: ListenerBanBody) -> dict:
        access = _require_stream_mode(station)
        try:
            return {
                "ban": await asyncio.to_thread(access.ban, body.address, body.minutes, body.note)
            }
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OSError as exc:
            logger.error("Could not save listener ban", exc_info=True)
            raise HTTPException(status_code=503, detail="Could not save listener ban") from exc

    @router.delete("/stream/bans")
    async def api_stream_unban(station: StationDep, address: str) -> dict:
        access = _require_stream_mode(station)
        try:
            removed = await asyncio.to_thread(access.unban, address)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OSError as exc:
            logger.error("Could not remove listener ban", exc_info=True)
            raise HTTPException(status_code=503, detail="Could not remove listener ban") from exc
        if not removed:
            raise HTTPException(status_code=404, detail="Ban not found")
        return {"address": address}

    @router.post("/stream/settings")
    async def api_stream_settings(
        station: StationDep, body: StreamSettingsBody, request: Request
    ) -> dict:
        """Change the stream bitrate (listeners reconnect), continuous play or name."""
        access = _require_stream_mode(station)
        bridge = station.bridge
        cfg = station.cfg
        metadata_changed = any(
            getattr(cfg.stream, field) != getattr(body, field)
            for field in ("station_name", "genre", "description", "url")
            if getattr(body, field) is not None
        )
        if body.show_next and cfg.dmca.enabled:
            raise HTTPException(
                status_code=409,
                detail="DMCA mode is on, so the next song is never shown publicly.",
            )
        if body.bitrate is not None:
            try:
                await asyncio.to_thread(bridge.set_stream_bitrate, body.bitrate)
            except EncoderUnavailableError as exc:
                raise HTTPException(status_code=503, detail="stream encoder failed") from exc
        if body.continuous is not None:
            cfg.stream.continuous = body.continuous
        if body.station_name is not None:
            # Read per stream request, so each player shows it when it next connects.
            cfg.stream.station_name = body.station_name
        for field in ("genre", "description", "url"):
            value = getattr(body, field)
            if value is not None:
                setattr(cfg.stream, field, value)
        if body.show_next is not None:
            cfg.nowplaying.show_next = body.show_next
        station.nowplaying.refresh()
        if body.max_listeners_per_address is not None:
            cfg.stream.max_listeners_per_address = body.max_listeners_per_address
            access.max_per_address = body.max_listeners_per_address
        await asyncio.to_thread(bridge.save_persistent_state)
        if metadata_changed and bridge.relays is not None:
            await asyncio.to_thread(_refresh_relay_metadata, station, request.app.state.stations)
        return bridge.stream_info()

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    @router.get("/profiles")
    async def api_profiles(station: StationDep) -> dict:
        """List saved profile bundles."""
        store = _profile_store(station)
        return {"profiles": await asyncio.to_thread(store.list_names), "root": str(store.root)}

    @router.post("/profiles")
    async def api_profile_save(station: StationDep, body: ProfileSaveBody) -> dict:
        """Save (or overwrite) a profile bundle."""
        from autodj.profiles import ProfileSnapshot

        values = body.model_dump()
        # The cart page shown now is part of the profile.
        store = station.carts.store
        values["cart_page"] = store.page(store.wall.active_page).name
        snap = ProfileSnapshot(**values)
        # The active clockwheel goes with the picking settings.
        library = station.bridge.library
        snap.clockwheel = _active_clock(station, library) if library is not None else None
        with _profile_http_errors():
            target = await asyncio.to_thread(_profile_store(station).save, snap)
        return {"saved": snap.name, "path": str(target)}

    @router.delete("/profiles/{name}")
    async def api_profile_delete(station: StationDep, name: str) -> dict:
        """Delete a saved profile bundle by name."""
        with _profile_http_errors():
            ok = await asyncio.to_thread(_profile_store(station).delete, name)
        if not ok:
            raise HTTPException(status_code=404, detail="Profile not found")
        return {"deleted": name}

    @router.post("/profiles/{name}/apply")
    async def api_profile_apply(station: StationDep, name: str) -> dict:
        """Load a saved profile and push every set field through the bridge."""
        return await apply_profile(station, name)

    # ------------------------------------------------------------------
    # Liners
    # ------------------------------------------------------------------

    @router.get("/liners")
    async def api_liners(station: StationDep) -> dict:
        """List discovered voice liners + current trigger config.

        The browser uses this to populate the Settings panel listing
        and to fetch raw liner bytes for ducking-overlay playback.
        """
        from autodj.liners import LinerLibrary

        cfg = station.cfg
        folder = station.paths.liners
        # from_folder lists the configured directory (not recursive).  That
        # directory is operator-controlled and may live on a slow mount, so
        # keep the walk off the event loop.
        lib = await asyncio.to_thread(LinerLibrary.from_folder, folder)
        items: list[dict[str, Any]] = []
        catalog_paths: set[str] = set()
        library = station.bridge.library
        if library is not None:
            for entry in await asyncio.to_thread(library.entries_snapshot, "liner"):
                if not station.owns_liner(entry.path):
                    continue  # another station's liner folder
                record = await asyncio.to_thread(library.catalog.record_for_path, entry.path)
                if not record or record.get("kind") != "liner":
                    continue
                path = Path(record["path"])
                catalog_paths.add(os.path.normcase(os.path.abspath(path)))
                items.append(
                    {
                        "id": record["id"],
                        "name": path.name,
                        "path": str(path),
                        "kind": "liner",
                        "editable": True,
                        "length": record.get("length"),
                        "points": record.get("points", {}),
                    }
                )
        for file_path in lib.files:
            normalized = os.path.normcase(os.path.abspath(file_path))
            if normalized not in catalog_paths:
                items.append(
                    {
                        "id": file_path.name,
                        "name": file_path.name,
                        "path": str(file_path),
                        "kind": "liner",
                        "editable": False,
                    }
                )
        # Each liner's play dates, in words, and whether it may play today.
        from autodj.clip_dates import station_today, station_zone

        today = station_today(cfg)
        dates = await asyncio.to_thread(station.liner_gate.store.all)
        for item in items:
            found = dates.get(item["id"])
            item.update(found.view(today) if found is not None else _NO_DATES)
        return {
            # Echo the operator's configured string verbatim; only the derived
            # default is rendered from a Path.
            "folder": cfg.playback.liners_folder or str(folder),
            "today": today.isoformat(),
            # So a page left open past midnight still knows the station's day.
            "timezone": station_zone(cfg),
            "files": [f.name for f in lib.files],
            "count": len(lib.files),
            "items": items,
            "metadata": {item["id"]: item for item in items},
            "folders": library.catalog.sources("liner") if library is not None else [],
            "config": {
                "enabled": bool(cfg.playback.liners_enabled),
                "every_n_songs": cfg.playback.liners_every_n_songs,
                "every_minutes": cfg.playback.liners_every_minutes,
                "random_min_minutes": cfg.playback.liners_random_min_minutes,
                "random_max_minutes": cfg.playback.liners_random_max_minutes,
                "pick_mode": cfg.playback.liners_pick_mode,
                "duck_db": cfg.playback.liners_duck_db,
            },
        }

    @router.post("/liners/upload")
    async def api_liner_upload(
        station: StationDep, file: UploadFile = File(...), replace: bool = False
    ) -> dict[str, str | int]:
        """Validate, bound, and atomically store a liner clip."""
        from autodj.liner_files import (
            InvalidLinerName,
            LinerConflictError,
            LinerTooLargeError,
            store_liner_upload,
        )

        name = file.filename or ""
        if not replace:
            # A new file starts without dates, even where a liner of that name
            # was deleted outside AutoDJ and its dates were left behind.  They
            # are forgotten first: when the dates file cannot be read, the
            # upload waits, so the new file can never take the old dates.
            from autodj.liner_files import validate_liner_name

            try:
                validate_liner_name(name)
            except InvalidLinerName as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            planned = station.paths.liners / name
            if not await asyncio.to_thread(planned.exists):
                try:
                    await _forget_new_liner_dates(station, planned)
                except OSError as exc:
                    logger.warning("Liner upload refused: %s", exc)
                    raise HTTPException(
                        status_code=503,
                        detail="The liner play dates could not be read just now, so the upload "
                        "was not saved; try again in a moment",
                    ) from exc
        try:
            target, size = await store_liner_upload(
                station.paths.liners,
                name,
                file,
                max_bytes=station.cfg.server.liner_upload_max_bytes,
                replace=replace,
            )
        except InvalidLinerName as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except LinerConflictError as exc:
            raise HTTPException(status_code=409, detail="Liner already exists") from exc
        except LinerTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        return {"filename": target.name, "size": size}

    @router.delete("/liners/file/{name}")
    async def api_liner_delete(station: StationDep, name: str) -> dict[str, str]:
        """Remove one validated plain liner file."""
        from autodj.liner_files import InvalidLinerName, delete_liner_file

        library = station.bridge.library
        record = library.record_for_id(name) if library is not None else None
        if (
            record is not None
            and record.get("kind") == "liner"
            and library.entry_for_path(record["path"]) is not None
        ):
            path = Path(record["path"])
            try:
                from autodj.liner_files import validate_liner_name

                validate_liner_name(path.name)
                if path.is_symlink() or not path.is_file():
                    raise FileNotFoundError(path.name)
                path.unlink()
                await asyncio.to_thread(library.refresh)
            except InvalidLinerName as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail="Liner not found") from exc
            except OSError as exc:
                raise HTTPException(status_code=500, detail="Unable to delete liner") from exc
            return {"deleted": path.name, **await _forget_liner_dates(station, name)}

        try:
            await asyncio.to_thread(delete_liner_file, station.paths.liners, name)
        except InvalidLinerName as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Liner not found") from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail="Unable to delete liner") from exc
        return {"deleted": name, **await _forget_liner_dates(station, name)}

    @router.put("/liners/dates/{liner_id}", response_model=None)
    async def api_liner_dates(
        station: StationDep, liner_id: str, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any] | JSONResponse:
        """Set a liner's play dates: "not before" and "do not play after", both optional."""
        from autodj.clip_dates import ClipDatesError, parse_dates, station_today

        listed = await api_liners(station)
        item = next((entry for entry in listed["items"] if entry["id"] == liner_id), None)
        if item is None:
            raise HTTPException(status_code=404, detail="Liner not found")
        try:
            dates = parse_dates(body)
            await asyncio.to_thread(station.liner_gate.store.set, liner_id, dates)
        except ClipDatesError as exc:
            return JSONResponse(
                status_code=400, content={"detail": exc.message, "field": exc.field}
            )
        except OSError as exc:
            logger.warning("Could not save liner dates: %s", exc)
            raise HTTPException(
                status_code=503,
                detail="The liner dates file could not be read just now; nothing was saved",
            ) from exc
        return {**item, **dates.view(station_today(station.cfg))}

    @router.get("/liners/file/{name}")
    async def api_liner_file(station: StationDep, name: str) -> FileResponse:
        """Serve one validated plain liner file."""
        from autodj.liner_files import InvalidLinerName, open_liner_file

        library = station.bridge.library
        record = library.record_for_id(name) if library is not None else None
        if (
            record is not None
            and record.get("kind") == "liner"
            and library.entry_for_path(record["path"]) is not None
        ):
            path = Path(record["path"])
            if path.is_symlink() or not path.is_file():
                raise HTTPException(status_code=404, detail="Liner not found")
            metadata = await asyncio.to_thread(path.stat)
            return FileResponse(path, media_type=audio_mime(path), stat_result=metadata)

        folder = station.paths.liners
        try:
            opened = await asyncio.to_thread(open_liner_file, folder, name)
        except InvalidLinerName as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Liner not found") from exc
        await asyncio.to_thread(opened.file.close)
        return FileResponse(folder / name, stat_result=opened.stat_result)

    @router.post("/liners/test")
    async def api_liner_test(station: StationDep, body: LinerTestBody) -> dict[str, str | None]:
        """Fire a liner through the server mix bus, in server-mixed modes.

        Browser-driven mode has no server-side mix bus to fire into —
        ``bridge.liner_scheduler`` is ``None`` there and the browser's
        "Test liner" button already plays the clip itself by fetching
        ``GET /api/liners/file/<name>`` directly, so this is a no-op.

        In stream mode the bus renders nothing while the station is idle
        or paused, so the liner is refused with a ``message`` saying why
        instead of waiting unheard to open the next set.
        """
        bridge = station.bridge
        scheduler = bridge.liner_scheduler
        if scheduler is None:
            return {"played": None}
        controller = bridge.station
        state = controller.state if controller is not None else "playing"
        if state == "idle":
            return {"played": None, "message": "Nobody is listening, so the liner was not played."}
        if state == "paused":
            return {"played": None, "message": "Playback is paused, so the liner was not played."}
        # Test never plays a liner outside its play dates, as the automatic
        # liners skip it; the page says why.
        refused = await asyncio.to_thread(scheduler.refusal, body.name) if body.name else None
        if isinstance(refused, str) and refused:
            return {"played": None, "message": f"That liner was skipped: it is {refused}."}
        played = await asyncio.to_thread(scheduler.fire, body.name)
        return {"played": played}

    # ------------------------------------------------------------------
    # Transport and queue
    # ------------------------------------------------------------------

    @router.post("/pause")
    async def api_pause(station: StationDep) -> dict[str, bool]:
        """Toggle pause/play."""
        paused = station.bridge.pause()
        return {"paused": paused}

    @router.post("/volume")
    async def api_volume(station: StationDep, body: VolumeBody) -> dict[str, float]:
        """Set the playback volume (0.0-1.0)."""
        bridge = station.bridge
        bridge.set_volume(body.volume)
        # Saved so a restart comes back at this volume, not at full.  On the
        # event loop like the other settings, so two saves never overlap.
        bridge.save_persistent_state()
        return {"volume": bridge.player._state.volume}

    @router.post("/mute")
    async def api_mute(station: StationDep) -> dict[str, bool]:
        """Toggle mute."""
        muted = station.bridge.toggle_mute()
        station.bridge.save_persistent_state()
        return {"muted": muted}

    @router.post("/play-next", response_model=None)
    async def api_play_next(
        station: StationDep, body: PlayNextBody
    ) -> dict[str, bool] | JSONResponse:
        """Insert a track at the head of the queue."""
        if refusal := _dmca_refusal(station, body, append=False):
            return refusal
        if not station.bridge.play_next(body.path, now=body.now):
            raise HTTPException(status_code=404, detail=TRACK_GONE_DETAIL)
        station.nowplaying.refresh()
        return {"ok": True}

    @router.post("/queue/add", response_model=None)
    async def api_queue_add(
        station: StationDep, body: PlayNextBody
    ) -> dict[str, bool] | JSONResponse:
        """Append a track to the user-ordered queue."""
        if refusal := _dmca_refusal(station, body, append=True):
            return refusal
        if not station.bridge.queue_add(body.path):
            raise HTTPException(status_code=404, detail=TRACK_GONE_DETAIL)
        station.nowplaying.refresh()
        return {"ok": True}

    @router.post("/queue/remove")
    async def api_queue_remove(station: StationDep, body: PlayNextBody) -> dict[str, bool]:
        """Remove a track from the user-ordered queue."""
        removed = station.bridge.queue_remove(body.path)
        if removed:
            station.nowplaying.refresh()
        return {"ok": removed}

    @router.post("/queue/reorder")
    async def api_queue_reorder(station: StationDep, body: QueueReorderBody) -> dict[str, bool]:
        """Reorder the user-ordered queue."""
        changed = station.bridge.queue_reorder(body.paths)
        if changed:
            station.nowplaying.refresh()
        return {"ok": changed}

    @router.post("/advance")
    async def api_advance(station: StationDep, request: Request) -> JSONResponse:
        """Browser signals end-of-track — server picks next track.

        Returns the fresh state synchronously so the browser can update
        ``current_track`` / ``next_track`` immediately without waiting
        for the 1 Hz WS broadcast.  Decouples advance latency from the
        broadcast cadence.

        Optional JSON body: ``{"from_path": "<the track the page is
        playing>"}``.  When it is not the server's current track, another
        tab (or a retried request) already advanced, so nothing changes
        and the current state is returned.  Without it, the server
        advances unconditionally.
        """
        from_path = None
        try:
            body = await request.json()
        except (ValueError, TypeError):
            body = None  # no JSON body: advance unconditionally
        if isinstance(body, dict) and isinstance(body.get("from_path"), str):
            from_path = body["from_path"]
        station.bridge.advance(from_path)
        return JSONResponse(await asyncio.to_thread(station.bridge.get_state))

    @router.post("/repick-next")
    async def api_repick_next(station: StationDep, request: Request) -> JSONResponse:
        """Replace next_track without advancing current.

        Browser POSTs this when its standby deck fails to load the
        prefetched next-track (bad codec, codec not supported on this
        client, network drop).  Server picks a different next-track so
        the currently-playing track keeps playing instead of being
        cascade-skipped along with the unplayable upcoming.

        Optional JSON body: ``{"blacklist": "path/that/failed.flac"}``
        adds the failed path to recently-played so similarity skips it.
        """
        blacklist = None
        try:
            body = await request.json()
            blacklist = body.get("blacklist") if isinstance(body, dict) else None
        except (ValueError, TypeError):
            # No JSON body or malformed payload — endpoint accepts either.
            logger.debug("repick_next: no JSON body, blacklist not set")
        station.bridge.repick_next(blacklist_path=blacklist)
        station.nowplaying.refresh()
        return JSONResponse(await asyncio.to_thread(station.bridge.get_state))

    @router.post("/random-track")
    async def api_random_track(station: StationDep) -> JSONResponse:
        """Reseed the auto-DJ from a fresh random track in the index.

        Status code distinguishes success from an empty index:
        ``200`` when a track was picked, ``409 Conflict`` when the
        index has nothing to reseed from (no tracks, or only silent ones).
        """
        if not station.bridge.reseed_random():
            raise HTTPException(
                status_code=409, detail="The index has no tracks that are not silent"
            )
        return JSONResponse(await asyncio.to_thread(station.bridge.get_state))

    # ------------------------------------------------------------------
    # Settings (mirror of CLI flags)
    # ------------------------------------------------------------------

    @router.post("/preset")
    async def api_preset(station: StationDep, body: PresetBody) -> dict:
        """Activate a preset by name."""
        bridge = station.bridge
        bridge.set_preset(body.name)
        bridge.save_persistent_state()
        return bridge.get_settings()

    @router.post("/transition")
    async def api_transition(station: StationDep, body: TransitionBody) -> dict:
        """Apply transition-effect overrides."""
        bridge = station.bridge
        try:
            bridge.set_transition(body.effect)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        bridge.save_persistent_state()
        return bridge.get_settings()

    @router.post("/djmix")
    async def api_djmix(station: StationDep, body: DjMixBody) -> dict:
        """Apply dj-mix flag overrides (beatmatch, harmonic, etc.)."""
        bridge = station.bridge
        bridge.set_djmix(**body.model_dump(exclude_none=True))
        bridge.save_persistent_state()
        return bridge.get_settings()

    @router.post("/playback-settings")
    async def api_playback_settings(station: StationDep, body: PlaybackSettingsBody) -> dict:
        """Apply playback-flag overrides (transition mode, lyrics, ...)."""
        bridge = station.bridge
        try:
            await run_in_threadpool(bridge.set_playback_settings, body)
        # FileNotFoundError (an OSError) is a Stereo Tool preset that is gone.
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        bridge.save_persistent_state()
        return bridge.get_settings()

    @router.post("/bpm-range")
    async def api_bpm_range(station: StationDep, body: BpmRangeBody) -> dict:
        """Set the active BPM range filter."""
        bridge = station.bridge
        bridge.set_bpm_range(body.lo, body.hi)
        bridge.save_persistent_state()
        return bridge.get_settings()

    @router.post("/discovery")
    async def api_discovery(station: StationDep, body: DiscoveryBody) -> dict:
        """Set the discovery rate (null turns discovery off)."""
        bridge = station.bridge
        bridge.set_discovery_every(body.every)
        bridge.save_persistent_state()
        return bridge.get_settings()

    @router.post("/discovery/toggle")
    async def api_discovery_toggle(station: StationDep) -> dict[str, bool]:
        """Turn discovery on or off and remember it (the Now Playing Discovery button)."""
        enabled = station.bridge.toggle_discovery()
        station.bridge.save_persistent_state()
        return {"discovery_enabled": enabled}

    @router.post("/eq")
    async def api_eq(station: StationDep, body: EqBody) -> dict[str, float]:
        """Set the 3-band EQ gains."""
        result = station.bridge.set_eq(low=body.low, mid=body.mid, high=body.high)
        station.bridge.save_persistent_state()
        return result

    # ------------------------------------------------------------------
    # Clockwheels: each station has its own, and its own active one.
    # They keep their old Library tools addresses.
    # ------------------------------------------------------------------

    @router.get("/library/clock")
    async def api_library_clock(station: StationDep) -> dict:
        return await asyncio.to_thread(_clock_snapshot, station, _catalog_library(station))

    @router.post("/library/clock")
    async def api_library_create_clock(station: StationDep, body: ClockBody) -> dict:
        library = _catalog_library(station)
        try:
            clock = await asyncio.to_thread(
                _validate_native_clock, station, library, body.model_dump()
            )
            return _clock_store(station).save(clock, overwrite=False)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/library/clock/check")
    async def api_library_clock_check(station: StationDep, body: ClockStepCheckBody) -> dict:
        library = _catalog_library(station)

        def count() -> dict:
            try:
                from autodj.clockwheel import ClockSlot, condition_paths
                from autodj.clockwheel_store import resolve_conditions, validate_step

                step = validate_step(body.step)
                if step["type"] == "settings":
                    return {"tracks": None, "error": None}
                sources = {source["id"]: source for source in library.catalog.sources()}
                if step["type"] == "track":
                    source = step_source(step, sources, 1)
                    target = source_folder(source["path"], step["path"])
                    return {
                        "tracks": int(library.entry_for_path(target) is not None),
                        "error": None,
                    }
                if step["type"] == "category":
                    _, results = library.categories.resolutions()
                    result = results.get(step["category"])
                    if result is None:
                        raise ValueError("Its category is unavailable; choose a saved category")
                    if result.error:
                        raise ValueError(result.error)
                    return {"tracks": result.count, "error": None}
                _validate_step_conditions(station, library, step, sources, 1)
                conditions = resolve_conditions(step, sources, 1)
                entries = library.entries_snapshot("liner" if step["type"] == "liner" else "music")
                fields = [c["name"] for c in conditions if c["field"] == "beets"]
                _, beets, error = _clock_beets(station, library).snapshot(fields)
                if error and fields:
                    raise ValueError(error)
                categories_index = getattr(library, "categories", None)
                from autodj.clockwheel_store import step_categories

                if categories_index and any(c["field"] == "category" for c in conditions):
                    _, results = categories_index.resolutions()
                    for category_id in step_categories(step):
                        result = results.get(category_id)
                        if result is None:
                            raise ValueError("A referenced category is unavailable")
                        if result.error:
                            raise ValueError(result.error)
                categories = (
                    categories_index.members()
                    if step["type"] == "music" and categories_index
                    else None
                )
                slot = ClockSlot(
                    kind=step["type"], conditions=conditions, match=step.get("match", "all")
                )
                paths = condition_paths(slot, entries, beets, categories)
                return {"tracks": len(paths), "error": None}
            except (ValueError, OSError) as exc:
                return {"tracks": None, "error": str(exc)}

        return await asyncio.to_thread(count)

    @router.post("/library/clock/active")
    async def api_library_select_clock(station: StationDep, body: ClockNameBody) -> dict:
        try:
            return await select_clock(station, _catalog_library(station), body.name)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/library/clock/import")
    async def api_library_import_clock(station: StationDep, body: PalImportBody) -> dict:
        library = _catalog_library(station)
        try:
            slots = parse_pal(
                body.text, [source["path"] for source in library.catalog.sources("liner")]
            )
            clock = from_pal(body.name, slots, library.catalog.sources(), body.resync_minutes)
            clock = await asyncio.to_thread(_validate_native_clock, station, library, clock)
            return _clock_store(station).save(clock, overwrite=False)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/library/clock/{name}")
    async def api_library_get_clock(station: StationDep, name: str) -> dict:
        return _existing_clock(station, name)

    @router.put("/library/clock/{name}")
    async def api_library_update_clock(
        station: StationDep,
        name: str,
        body: ClockBody,
        apply_timing: Literal["immediate", "continue", "next_restart"] = "immediate",
    ) -> dict:
        library = _catalog_library(station)
        _existing_clock(station, name)
        if body.name != name:
            raise HTTPException(status_code=400, detail="Use Rename to change the name")
        try:
            checked = await asyncio.to_thread(
                _validate_native_clock, station, library, body.model_dump()
            )
            clock = _clock_store(station).save(checked)
            if _active_clock(station, library) == name:
                await asyncio.to_thread(station.player.apply_clockwheel_update, name, apply_timing)
            return clock
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/library/clock/{name}/duplicate")
    async def api_library_duplicate_clock(
        station: StationDep, name: str, body: NewClockNameBody
    ) -> dict:
        clock = _existing_clock(station, name)
        try:
            return _clock_store(station).save({**clock, "name": body.name}, overwrite=False)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/library/clock/{name}/rename")
    async def api_library_rename_clock(
        station: StationDep, name: str, body: NewClockNameBody
    ) -> dict:
        library = _catalog_library(station)
        clock = _existing_clock(station, name)
        store = _clock_store(station)
        try:
            renamed = validate_clockwheel({**clock, "name": body.name})
            if body.name.casefold() == name.casefold():
                # Only the letter case changes: the same file on Windows.
                store.delete(name)
                store.save(renamed)
            else:
                store.save(renamed, overwrite=False)
                store.delete(name)
            if _active_clock(station, library) == name:
                await select_clock(station, library, body.name, renamed_from=name)
            return renamed
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/library/clock/{name}")
    async def api_library_delete_clock(station: StationDep, name: str) -> dict:
        library = _catalog_library(station)
        _existing_clock(station, name)
        try:
            _clock_store(station).delete(name)
            if _active_clock(station, library) == name:
                return await select_clock(station, library, None)
            return await asyncio.to_thread(_clock_snapshot, station, library)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    install_carts(router)
    install_scheduler(router)
    install_time_checks(router)
    install_breaks(router)
    install_requests(router)
    install_matched_intros(router)
    install_rotation_test(router)
    install_hour_preview(router)


__all__ = [
    "DEFAULT_SLUG",
    "STATION_PREFIX",
    "StationDep",
    "StationRouter",
    "apply_profile",
    "current_station",
    "equip_station",
    "install_station_routes",
    "resolve_station",
    "select_clock",
]
