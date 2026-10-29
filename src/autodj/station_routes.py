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
import hashlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)
from pydantic import Field as PydanticField
from starlette.requests import HTTPConnection

from autodj._bridge import (
    HISTORY_AIRED_ROWS,
    HISTORY_VIEW_ROWS,
    SEEK_TRACK_CHANGED,
    STREAM_SEEK_UNAVAILABLE,
    SeekTrackChanged,
    StreamSeekUnavailable,
)
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
from autodj.config import (
    MAX_EMBED_ORIGINS,
    MAX_RELAYS,
    MAX_STATS_DAYS,
    RecordingConfig,
    RelayConfig,
    _relay_target,
)
from autodj.history_kinds import INSERT_KINDS
from autodj.history_pages import history_page
from autodj.live_queue import VoiceQueueConflict
from autodj.playlog import (
    missing_fields,
    parse_bound,
    plays_between,
    plays_csv,
    soundexchange_filename,
    soundexchange_report,
)
from autodj.recorder import Recorder, load_saved_state, save_state
from autodj.relay import resolve_safe_relay_addresses, source_port
from autodj.relay_settings import (
    credential_is_missing,
    password_variable,
    public_relay,
    save_relays,
)
from autodj.scheduler import Action, ActionField, system_timezone
from autodj.settings_bodies import (
    EqBody,
    FadeSeconds,
    FiniteFloat,
    PlaybackSettingsBody,
    StationSettingsPatchBody,
    StreamSettingsBody,
    VolumeBody,
    validate_playback_choices,
)
from autodj.station_log import CURRENT_STATION
from autodj.station_name import listener_name
from autodj.stations import DEFAULT_SLUG, StationRuntime
from autodj.stream import EncoderUnavailableError
from autodj.stream_formats import FORMATS

if TYPE_CHECKING:
    from autodj.listener_access import ListenerAccess
    from autodj.listener_requests import RequestDesk

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


async def editable_station(request: Request) -> Any:
    """Resolve saved configuration independently of playback availability."""
    slug = request.path_params.get("slug", DEFAULT_SLUG)
    try:
        station = await asyncio.to_thread(request.app.state.stations.editable, slug)
    except KeyError:
        raise HTTPException(status_code=404, detail="No station has that name") from None
    except Exception as exc:
        from autodj.stations import StationControlError

        if isinstance(exc, StationControlError):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise
    CURRENT_STATION.set(slug)
    return station


SavedStationDep = Annotated[Any, Depends(editable_station)]


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

    name: str = PydanticField(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9_-](?:[A-Za-z0-9 _-]{0,30}[A-Za-z0-9_-])?$",
    )
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
    # The song the seek is for (#60); older pages leave it out.
    path: str | None = PydanticField(default=None, min_length=1, max_length=4096)


class PlayNextBody(BaseModel):
    """Request body for POST /api/play-next."""

    path: str
    now: bool = False  # if True, also skip the current track
    # DMCA mode: queue it even though it breaks a limit (the operator said so).
    confirm: StrictBool = False
    voice_action: Literal["keep", "discard"] | None = None
    voice_revision: StrictStr | None = None


class QueueReorderBody(BaseModel):
    """Request body for POST /api/queue/reorder.

    Attributes:
        paths: New ordering of the queue, expressed as a list of
            :attr:`~autodj.indexer.IndexEntry.path` strings.  The first
            entry plays after the current track finishes.
    """

    paths: list[str]
    # The queue as the page showed it, and each new slot's place in it: with
    # both, the entries themselves move (a show's copy and a listener's
    # request of the same file keep who they are, #106).
    from_paths: list[str] | None = None
    order: list[int] | None = None
    voice_action: Literal["keep", "discard"] | None = None
    voice_revision: StrictStr | None = None


class QueueRemoveBody(BaseModel):
    """Request body for POST /api/queue/remove.

    Attributes:
        path: The track to remove.
        position: Its place in the queue as the page showed it; that entry
            goes when the queue still has *path* there.
    """

    path: str
    position: int | None = None
    voice_action: Literal["keep", "discard"] | None = None
    voice_revision: StrictStr | None = None


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
    # The station name while it is active (#472); clockwheel_store checks it.
    station_name: str = ""


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


def _stream_info(station: Any) -> dict[str, Any]:
    """Use live stream details when running, saved stream config while stopped."""
    bridge = station.bridge
    if getattr(bridge, "stream_mode", False):
        return bridge.stream_info()
    cfg = station.cfg
    from autodj.stations import public_addresses

    addresses = public_addresses(station.slug, streams=True)
    stream_path = addresses["stream_path"] or "/stream.mp3"
    formats = []
    for name, fmt in FORMATS.items():
        bitrate = (
            int(cfg.stream.bitrate)
            if name == "mp3"
            else int(getattr(cfg.stream, f"{name}_bitrate", 0))
        )
        formats.append(
            {
                "format": name,
                "label": fmt.label,
                "bitrate": bitrate,
                "path": f"{stream_path.removesuffix('.mp3')}.{name}",
                "listeners": 0,
                "error": None,
            }
        )
    return {
        "path": stream_path,
        "m3u_path": addresses["m3u_path"] or "/stream.m3u",
        "bitrate": int(cfg.stream.bitrate),
        "continuous": bool(cfg.stream.continuous),
        "station_name": cfg.stream.station_name,
        "genre": cfg.stream.genre,
        "description": cfg.stream.description,
        "url": cfg.stream.url,
        "show_next": cfg.nowplaying.show_next,
        "listeners": 0,
        "state": "stopped",
        "relays": [],
        "formats": formats,
        "stats_days": int(cfg.stream.stats_days),
    }


def _recording_values(station: Any) -> tuple[bool, RecordingConfig]:
    """Read validated recorder settings without constructing its writer thread."""
    cfg = station.cfg.recording
    return load_saved_state(
        station.paths.recording,
        cfg.folder,
        RecordingConfig(cfg.folder, cfg.split, cfg.retention_days, cfg.free_space_mb),
    )


def _recording_saved_status(station: Any) -> dict[str, object]:
    _running, cfg = _recording_values(station)
    return {
        "running": False,
        "file": None,
        "bytes": 0,
        "error": None,
        "split": cfg.split,
        "retention_days": cfg.retention_days,
        "free_space_mb": cfg.free_space_mb,
    }


def _station_settings_values(
    station: Any, request: Request, profile: str | None = None
) -> dict[str, dict[str, Any]]:
    """Collect the editable bodies from each station-owned settings store."""
    from autodj import timefmt
    from autodj.breaks_api import break_settings_view

    registry = request.app.state.stations
    slug = station.slug
    configuration = registry.configuration(slug)
    bridge = station.bridge
    settings_owner = getattr(bridge, "settings", bridge)
    values = settings_owner.editable_values()
    breaks = break_settings_view(station)
    stream = station.cfg.stream
    values.update(
        {
            "stream": {
                "bitrate": int(stream.bitrate),
                "aac_bitrate": int(stream.aac_bitrate),
                "opus_bitrate": int(stream.opus_bitrate),
                "stats_days": int(stream.stats_days),
                "continuous": bool(stream.continuous),
                "station_name": stream.station_name,
                "genre": stream.genre,
                "description": stream.description,
                "url": stream.url,
                "max_listeners_per_address": stream.max_listeners_per_address,
            },
            "public": registry.public_settings(slug),
            "station": {
                "output": configuration.station.output,
                "server_audio_too": configuration.station.server_audio_too,
                "liner_upload_max_mib": configuration.station.liner_upload_max_mib,
                "log_label": configuration.station.log_label,
            },
            "time": {
                "timezone": configuration.station.timezone or system_timezone(),
                "time_format": timefmt.time_format,
            },
            "time_checks": station.time_checks.settings.model_dump(),
            "breaks": {
                key: breaks[key]
                for key in ("enabled", "dj_placement", "min_intro_seconds", "duck_db")
            },
            "live": station.live.settings.as_dict(),
        }
    )
    _running, recording = _recording_values(station)
    values["recording"] = {
        "split": recording.split,
        "retention_days": recording.retention_days,
        "free_space_mb": recording.free_space_mb,
    }
    values["stream"]["show_next"] = configuration.nowplaying.show_next
    if profile is not None:
        from autodj.profile_settings import merge_values, profile_settings

        values = merge_values(values, profile_settings(station).resolve(profile))
    return values


def _profile_document(station: Any, settings: dict, profile: str | None) -> dict:
    if profile is not None:
        from autodj.profile_settings import PROFILE_GROUPS, profile_settings

        settings["profile"] = profile
        settings["profile_groups"] = sorted(PROFILE_GROUPS)
        settings["active_profile"] = profile_settings(station).active
    return settings


def _settings_revision(values: dict[str, Any]) -> str:
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _save_recording_settings(station: Any, body: RecordingSettingsBody) -> dict[str, object]:
    was_running, _old = _recording_values(station)
    cfg = RecordingConfig(
        station.cfg.recording.folder, body.split, body.retention_days, body.free_space_mb
    )
    save_state(station.paths.recording, was_running, cfg)
    station.cfg.recording.split = cfg.split
    station.cfg.recording.retention_days = cfg.retention_days
    station.cfg.recording.free_space_mb = cfg.free_space_mb
    return _recording_saved_status(station)


def _stopped_webhooks(station: Any) -> list[dict[str, str | int]]:
    from urllib.parse import urlsplit

    return [
        {
            "index": index,
            "host": urlsplit(hook.url).hostname or "",
            "format": hook.format,
            "result": "Not sent",
        }
        for index, hook in enumerate(station.cfg.webhooks)
    ]


def _require_stream_mode(station: StationRuntime) -> ListenerAccess:
    """404 unless the station streams."""
    if not station.bridge.stream_mode or station.listener_access is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return station.listener_access


def _refresh_relay_metadata(station: StationRuntime, registry: Any) -> None:
    with registry.relay_lock:
        station.bridge.relays.reconfigure(station.cfg.stream.relays.values(), restart_all=True)
    # They now have the name on the air (#472): no second reconnect for it.
    station.relay_name = listener_name(station.bridge)


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
        if station.bridge.relays is not None:
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
    sharing = next(
        (
            name
            for name in station.cfg.stream.relays
            if name != body.name and password_variable(station.slug, name) == variable
        ),
        None,
    )
    managed = variable in os.environ
    if sharing is not None and managed:
        # a-b and a_b (or Main and main) read one password variable; once it
        # is set, both would send its password.
        raise HTTPException(
            status_code=409,
            detail=(
                f"Relay {sharing} takes its password from the same environment variable, "
                "which is set; choose another name."
            ),
        )
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
    ledger = getattr(station, "ledger", None)
    if ledger is None:
        ledger = (
            library.ledger
            if station.slug == DEFAULT_SLUG and hasattr(library, "ledger")
            else library.station_ledger(station.slug)
        )
    setting = ledger.get_setting("clock", {})
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
        library.clock_beets = BeetsConditions(
            station.cfg, getattr(library, "entries_snapshot", None)
        )
    return library.clock_beets


def _validate_step_conditions(
    station: StationRuntime, library: Any, step: dict, sources: dict, number: int
) -> None:
    from autodj.clockwheel_store import resolve_conditions

    categories = _category_names(station, library)
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


def _station_categories(station: StationRuntime, library: Any) -> Any:
    """The station's rotation categories, or ``None`` without a library."""
    pick = getattr(library, "categories_for", None)
    if callable(pick):
        return pick(station.paths)
    return getattr(library, "categories", None)


def _category_names(station: StationRuntime, library: Any) -> dict[str, str]:
    """Each of the station's saved rotation categories' ID and name."""
    index = _station_categories(station, library)
    if index is None:
        return {}
    return {category.id: category.name for category in index.categories()}


def _validate_native_clock(station: StationRuntime, library: Any, value: Any) -> dict:
    """Validate *value*, then check its folders, tracks and presets exist."""
    clock = validate_clockwheel(value)
    sources = {source["id"]: source for source in library.catalog.sources()}
    categories = _category_names(station, library)
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
    ads = getattr(station, "ads", None)
    advertisers: list[str] = []
    ad_categories: list[str] = []
    if ads is not None:
        try:
            spots = ads.store.cached().spots.values()
            advertisers = sorted(
                {
                    spot.advertiser.casefold(): spot.advertiser for spot in spots if spot.advertiser
                }.values(),
                key=str.casefold,
            )
            ad_categories = sorted(
                {
                    spot.category.casefold(): spot.category for spot in spots if spot.category
                }.values(),
                key=str.casefold,
            )
        except (OSError, ValueError):
            pass
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
        "categories": [
            {"id": key, "name": name} for key, name in _category_names(station, library).items()
        ],
        "cursor": (getattr(player, "_clock", None) or {}).get("cursor", 0),
        "last_status": getattr(player, "_clock_status", None),
        "pending_update": getattr(player, "_pending_clock_update", None) is not None,
        "notice": OLD_CLOCK_MESSAGE if "slots" in setting else None,
        "ad_advertisers": advertisers,
        "ad_categories": ad_categories,
        "ads_reason": ads.refusal() if ads is not None else None,
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
    ledger = station.ledger or library.station_ledger(station.slug)
    await asyncio.to_thread(ledger.select_clock, name, cursor, renamed_from)
    if station.player is not None:
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
    """Activate the latest saved defaults plus a profile's overrides."""
    from autodj.profile_settings import profile_settings

    manager = profile_settings(station)
    try:
        values = await asyncio.to_thread(manager.resolve, name)
        await asyncio.to_thread(manager.validate, values)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(
            status_code=400, detail=f"Profile {name or 'Station defaults'}: {exc}"
        ) from exc
    try:
        await asyncio.to_thread(manager.apply, name)
        await station.carts.changed()
        if station.recorder is not None:
            station.recorder.show_changed()
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Profile application did not finish; some settings may already be active: {exc}",
        ) from exc
    return {"applied": sorted(values), "name": name, "active": manager.active}


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


def equip_station(station: Any, publish: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
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

    from autodj.prepared_editions import edition_store
    from autodj.shows import ShowRunner, ShowStore, show_action

    # Shows (#106): a list of songs prepared ahead that a schedule event airs.
    station.shows = ShowStore(station.paths.shows)
    station.show_runner = ShowRunner(
        bridge,
        station.shows,
        publish,
        state_path=station.paths.show_on_air,
        edition_store=edition_store(station),
        edition_station=station,
    )
    schedule.register(show_action(station.show_runner, station.shows, edition_store(station)))
    if bridge.player is not None:
        bridge.player.prepared_handoff = station.show_runner.prepared_handoff
        bridge.player.prepared_preview_handoff = functools.partial(
            station.show_runner.prepared_handoff, report_omissions=False
        )
        bridge.player.prepared_speech_failed = station.show_runner.prepared_speech_failed
        # A song kept from before a restart never starts a set a show holds (#584).
        bridge.player.show_holds_air = station.show_runner.holds_air

    def show_starts_within(within_s: float) -> str | None:
        # Called from the request desk's threads: only show events, copied
        # under the scheduler's lock.
        from datetime import timedelta

        for _when, event in schedule.runs_within("show", timedelta(seconds=within_s)):
            show = station.shows.get(str(event.params.get("show", "")))
            return show.name if show is not None else event.name
        return None

    station.show_runner.starts_within = show_starts_within
    # A remove or reorder by path never takes the show's own copy by mistake.
    bridge.show_entry = station.show_runner.is_show_entry

    from autodj.breaks import BreakDesk

    station.breaks = BreakDesk(lambda: station.cfg.breaks, station.paths.announcements)
    if bridge.player is not None:
        bridge.player.break_desk = station.breaks
    _equip_ads(station, schedule)

    if not station.stopped and station.cfg.requests.enabled is True:
        station.requests = request_desk(station)
        # A show that starts ahead of queued requests updates their notes.
        station.show_runner.on_started = station.requests.show_started

    from autodj.categories import station_wall
    from autodj.matched_intros import MatchedIntros

    # Play dates are judged by the station's day when the intro airs.
    station.intros = MatchedIntros(
        station.paths.matched_intros,
        station.paths.intros,
        day_at=lambda at: station_wall(station.cfg, at).date(),
    )
    from autodj.intro_rules import IntroRules

    # Rules share the intros' day and random source (never the picker's).
    station.intros.rules = IntroRules(
        station.paths.state_dir / "intro_rules.json",
        station.intros._day_at,
        station.intros._rng,
        library=lambda: bridge.library,
        paths=station.paths,
    )
    if bridge.player is not None:
        bridge.player.matched_intros = station.intros
    from autodj.profile_settings import profile_settings

    profile_settings(station).restore()


def request_desk(station: StationRuntime) -> RequestDesk:
    """A new listener request desk for *station* (blocking: it reads the request book).

    Built when the station starts with requests on, or when they are first
    turned on while it runs.  The caller sets it on the station.
    """
    from autodj.listener_requests import (
        FORM_TOKENS,
        TRACK_SECRET,
        RequestBook,
        RequestDesk,
        TrackIds,
    )

    return RequestDesk(
        station.slug,
        station.cfg.requests,
        station.bridge,
        RequestBook(station.paths.listener_requests, timezone=station.timezone),
        music_dir=station.cfg.library.music_dir,
        tokens=FORM_TOKENS,
        track_ids=TrackIds(TRACK_SECRET),
        on_queued=station.nowplaying.refresh,
        shows=station.show_runner,
    )


def _equip_ads(station: StationRuntime, schedule: Any) -> None:
    """Give *station* its ad breaks (#51) and the Schedule's Ad break action."""
    from autodj.ads import AdDesk, AdLog, AdsError, AdStore

    paths = station.paths
    store = AdStore(paths.ads, paths.ad_spots, max_spots=lambda: station.cfg.ads.max_spots)
    station.ads = AdDesk(
        lambda: station.cfg.ads,
        store,
        AdLog(paths.ad_plays),
        station.timezone,
        player=station.bridge.player,
        breaks=station.breaks,
        events=lambda: list(schedule.events.values()),
    )

    async def ad_break(params: dict[str, str]) -> None:
        desk = station.ads
        reason = desk.refusal()
        if reason is not None:
            raise ValueError(reason)
        try:
            desk.request_named(params["name"], "schedule")
        except AdsError as exc:
            raise ValueError(exc.message) from exc

    def break_names() -> list[str]:
        return sorted(item.name for item in store.state().breaks.values())

    schedule.register(
        Action(
            "ad_break",
            "Ad break",
            ad_break,
            (ActionField("name", "Ad break", names=break_names),),
            # The break waits for a song change: it never cuts a song.
            interrupt="none",
        )
    )


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

    def patch(self, path: str, **options: Any) -> Callable[[_Endpoint], _Endpoint]:
        """Register a PATCH route."""
        return self.api_route(path, methods=["PATCH"], **options)

    def delete(self, path: str, **options: Any) -> Callable[[_Endpoint], _Endpoint]:
        """Register a DELETE route."""
        return self.api_route(path, methods=["DELETE"], **options)


def install_station_routes(router: StationRouter) -> None:
    """Register every station route, with paths relative to the API root (``/skip``, ...)."""
    from autodj.ads_api import install_ads
    from autodj.breaks_api import install_breaks
    from autodj.carts import install_carts
    from autodj.categories_api import install_categories
    from autodj.hour_preview import install_hour_preview
    from autodj.landmarks import install_landmarks
    from autodj.live_routes import install_live_api
    from autodj.matched_intros_api import install_matched_intros
    from autodj.requests_api import install_requests
    from autodj.rotation_api import install_rotation_test
    from autodj.scheduler_api import install_scheduler
    from autodj.shows_api import install_shows
    from autodj.stereotool_api import install_stereo_tool
    from autodj.time_checks import install_time_checks

    @router.get("/settings")
    async def api_settings(
        station: SavedStationDep, request: Request, profile: str | None = None
    ) -> dict:
        settings = await asyncio.to_thread(station.bridge.get_settings)
        try:
            values = await asyncio.to_thread(_station_settings_values, station, request, profile)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        settings["values"] = values
        settings["revision"] = _settings_revision(values)
        return _profile_document(station, settings, profile)

    @router.patch("/settings")
    async def api_settings_patch(
        station: SavedStationDep, body: StationSettingsPatchBody, request: Request
    ) -> Any:
        registry = request.app.state.stations
        slug = station.slug
        lock = getattr(request.app.state, "station_settings_lock", None)
        if lock is None:
            lock = request.app.state.station_settings_lock = asyncio.Lock()
        async with lock:
            values = await asyncio.to_thread(
                _station_settings_values, station, request, body.profile
            )
            revision = _settings_revision(values)
            if body.revision != revision:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Station settings changed in another session.",
                        "revision": revision,
                        "values": values,
                    },
                )

            from autodj.settings_bodies import StationSettingsChangesBody

            try:
                changes = StationSettingsChangesBody.model_validate(body.changes)
            except ValidationError as exc:
                issue = exc.errors()[0]
                field = str(issue["loc"][-1]) if issue["loc"] else "changes"
                nested = re.search(rf"\b{re.escape(field)}\.([A-Za-z_]\w*)", issue["msg"])
                if nested:
                    field = nested.group(1)
                return JSONResponse(
                    status_code=422,
                    content={"detail": f"{field}: {issue['msg']}", "field": field},
                )
            parsed: dict[str, Any] = {}
            from autodj.breaks_api import BreakSettingsBody
            from autodj.config import DmcaConfig, PlayerPageConfig
            from autodj.live import TalkoverSettings
            from autodj.time_checks import SettingsBody as TimeChecksSettingsBody

            def validated(model: Any, data: Any) -> Any:
                try:
                    return model.model_validate(data)
                except ValidationError as exc:
                    issue = exc.errors()[0]
                    field = str(issue["loc"][-1]) if issue["loc"] else "settings"
                    raise HTTPException(status_code=422, detail=f"{field}: {issue['msg']}") from exc

            for name in changes.model_fields_set:
                value = getattr(changes, name)
                if value is None:
                    continue
                if name == "time_checks":
                    proposed = validated(
                        TimeChecksSettingsBody,
                        {**values["time_checks"], **value},
                    )
                    for pack in proposed.enabled_packs:
                        from autodj.time_checks import _known_pack

                        _known_pack(station.time_checks, pack)
                    refusal = await asyncio.to_thread(station.time_checks.check_choices, proposed)
                    if refusal:
                        raise HTTPException(status_code=400, detail=refusal)
                    parsed[name] = proposed
                elif name == "breaks":
                    parsed[name] = validated(BreakSettingsBody, value)
                elif name == "stream":
                    proposed = validated(StreamSettingsBody, value)
                    if proposed.show_next and station.cfg.dmca.enabled:
                        raise HTTPException(
                            status_code=409,
                            detail="DMCA mode is on, so the next song is never shown publicly.",
                        )
                    parsed[name] = proposed
                elif name == "public":
                    allowed = {
                        "dmca_enabled",
                        "player_page_enabled",
                        "embed_enabled",
                        "embed_origins",
                    }
                    if not value or value.keys() - allowed:
                        raise HTTPException(
                            status_code=400, detail="Choose a public setting to change"
                        )
                    try:
                        DmcaConfig(value.get("dmca_enabled", station.cfg.dmca.enabled))
                        PlayerPageConfig(
                            enabled=value.get(
                                "player_page_enabled", station.cfg.player_page.enabled
                            ),
                            embed=value.get("embed_enabled", station.cfg.player_page.embed),
                            embed_origins=value.get(
                                "embed_origins", station.cfg.player_page.embed_origins
                            ),
                        )
                    except (TypeError, ValueError) as exc:
                        detail = str(exc)
                        if detail.startswith("player_page.embed_origins"):
                            if detail.startswith("player_page.embed_origins entry "):
                                site = detail.removeprefix(
                                    "player_page.embed_origins entry "
                                ).rsplit(" must be an HTTP(S) origin", 1)[0]
                                detail = (
                                    f"Sites allowed to embed: {site} is not an http:// or https:// "
                                    "site address, such as https://radio.example.net."
                                )
                            elif "allows at most" in detail:
                                detail = f"Sites allowed to embed: use at most {MAX_EMBED_ORIGINS} sites."
                            else:
                                detail = "Sites allowed to embed: enter one site per line."
                            return JSONResponse(
                                status_code=400,
                                content={"detail": detail, "field": "embed_origins"},
                            )
                        raise HTTPException(status_code=400, detail=detail) from exc
                    parsed[name] = value
                elif name == "station":
                    allowed = {"output", "server_audio_too", "liner_upload_max_mib", "log_label"}
                    if not value or value.keys() - allowed:
                        raise HTTPException(
                            status_code=400, detail="Choose a station option to change"
                        )
                    if "log_label" in value:
                        from autodj.config import LOG_LABEL_RULE, check_log_label

                        try:
                            label = check_log_label(value["log_label"])
                        except (TypeError, ValueError):
                            problem = (
                                f"Log label must be {LOG_LABEL_RULE}. "
                                "Leave it empty to use the station ID."
                            )
                        else:
                            value = {**value, "log_label": label}
                            problem = registry.log_label_problem(slug, label)
                        if problem is not None:
                            return JSONResponse(
                                status_code=400,
                                content={"detail": problem, "field": "log_label"},
                            )
                    from dataclasses import replace

                    try:
                        replace(station.cfg.station, **value)
                    except (TypeError, ValueError) as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
                    parsed[name] = value
                elif name == "time":
                    if not value or value.keys() - {"timezone", "time_format"}:
                        raise HTTPException(status_code=400, detail="Invalid time settings")
                    from autodj import timefmt

                    value = {
                        "timezone": station.cfg.station.timezone or system_timezone(),
                        "time_format": timefmt.time_format,
                        **value,
                    }
                    if value["time_format"] not in {"12", "24"}:
                        raise HTTPException(status_code=400, detail="Invalid time settings")
                    try:
                        ZoneInfo(value["timezone"])
                    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
                        raise HTTPException(
                            status_code=400, detail="Invalid station time zone"
                        ) from exc
                    parsed[name] = value
                elif name == "live":
                    try:
                        parsed[name] = TalkoverSettings.parse(
                            {**station.live.settings.as_dict(), **value}
                        )
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
                elif name == "recording":
                    current_recording = values["recording"]
                    parsed[name] = validated(RecordingSettingsBody, {**current_recording, **value})
                else:
                    parsed[name] = value

            if not parsed:
                raise HTTPException(
                    status_code=422, detail="At least one settings group is required"
                )

            # Validate all core groups and owner-specific inputs before the first write.
            core_names = {
                "preset",
                "transition",
                "djmix",
                "playback",
                "bpm_range",
                "discovery",
                "eq",
            }
            core_changes = {
                key: getattr(changes, key)
                for key in core_names
                if getattr(changes, key) is not None
            }
            if core_changes:
                from autodj.station_settings import StationSettings

                try:
                    await asyncio.to_thread(
                        StationSettings(station.cfg).validate_settings_patch, core_changes
                    )
                except (OSError, ValueError) as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            profile_changes: dict[str, Any] = {}
            if body.profile is not None:
                from autodj.profile_settings import LOCAL_PLAYBACK, PROFILE_GROUPS, profile_settings

                profile_changes = {
                    key: value for key, value in body.changes.items() if key in PROFILE_GROUPS
                }
                if "playback" in profile_changes:
                    profile_changes["playback"] = {
                        key: value
                        for key, value in profile_changes["playback"].items()
                        if key not in LOCAL_PLAYBACK
                    }
                    if not profile_changes["playback"]:
                        del profile_changes["playback"]
                if profile_changes:
                    manager = profile_settings(station)
                    try:
                        from autodj.profile_settings import merge_values

                        target_values = await asyncio.to_thread(manager.resolve, body.profile)
                        await asyncio.to_thread(
                            manager.validate, merge_values(target_values, profile_changes)
                        )
                    except (OSError, ValueError) as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
                    for key in profile_changes:
                        core_changes.pop(key, None)
                        parsed.pop(key, None)
                    local = {
                        key: value
                        for key, value in body.changes.get("playback", {}).items()
                        if key in LOCAL_PLAYBACK
                    }
                    if local:
                        core_changes["playback"] = PlaybackSettingsBody.model_validate(local)
            elif "clock" in parsed or "cart" in parsed:
                raise HTTPException(status_code=400, detail="Choose a profile editing target first")
            applied: list[str] = []
            failed: dict[str, str] = {}

            async def apply_group(name: str, callback: Callable[[], Any]) -> None:
                try:
                    result = callback()
                    if asyncio.iscoroutine(result):
                        await result
                    applied.append(name)
                except Exception as exc:
                    logger.exception("Could not save station settings group %s", name)
                    failure = str(exc) or "The settings could not be saved."
                    if name == "core":
                        failure = f"Settings are active in memory but could not be saved: {failure}"
                    failed[name] = failure

            if profile_changes:
                await apply_group(
                    "profile",
                    lambda: asyncio.to_thread(manager.save, body.profile or "", profile_changes),
                )
                if "profile" in applied:
                    applied.remove("profile")
                    applied.extend(sorted(profile_changes))
                if "profile" in failed:
                    failure = failed.pop("profile")
                    failed.update(dict.fromkeys(profile_changes, failure))
            if core_changes:
                settings_owner = getattr(station.bridge, "settings", station.bridge)
                await apply_group("core", lambda: settings_owner.apply_settings_patch(core_changes))
                if "core" in applied:
                    applied.remove("core")
                    applied.extend(sorted(core_changes))
                if "core" in failed:
                    failure = failed.pop("core")
                    failed.update(dict.fromkeys(core_changes, failure))

            for name in (
                "time_checks",
                "breaks",
                "public",
                "station",
                "time",
                "live",
                "recording",
                "stream",
            ):
                if name not in parsed:
                    continue
                value = parsed[name]
                if name == "time_checks":

                    def save_time_checks(value: Any = value) -> Any:
                        return station.time_checks.save(value)

                    await apply_group(name, save_time_checks)
                elif name == "breaks":

                    def save_breaks(value: Any = value) -> None:
                        from autodj.breaks_api import TURNED_OFF

                        config = station.cfg.breaks
                        was_on = config.enabled is True
                        before = {field: getattr(config, field) for field in value.model_fields_set}
                        page_was_saved = "breaks" in station.bridge.page_saved
                        try:
                            for field in value.model_fields_set:
                                item = getattr(value, field)
                                if item is not None or field == "duck_db":
                                    setattr(config, field, item)
                            station.bridge.page_saved.add("breaks")
                            station.bridge.save_persistent_state(strict=True)
                        except Exception:
                            for field, item in before.items():
                                setattr(config, field, item)
                            if not page_was_saved:
                                station.bridge.page_saved.discard("breaks")
                            raise
                        if was_on and config.enabled is not True and station.breaks is not None:
                            station.breaks.drop_waiting_breaks(TURNED_OFF)

                    await apply_group(name, save_breaks)
                elif name == "public":

                    def save_public(value: Any = value) -> Any:
                        return registry.save_public_settings(slug, value)

                    await apply_group(name, save_public)
                elif name == "station":

                    def save_station(value: Any = value) -> Any:
                        return registry.save_options(slug, value)

                    await apply_group(name, save_station)
                elif name == "time":

                    def save_time(value: Any = value) -> None:
                        from autodj import timefmt

                        registry.save_options(slug, {"timezone": value["timezone"]})
                        try:
                            _catalog_library(station).catalog.set_setting(
                                "time_format", value["time_format"]
                            )
                        except Exception as exc:
                            raise RuntimeError(
                                f"The station time zone was saved, but time format was not: {exc}"
                            ) from exc
                        timefmt.time_format = value["time_format"]

                    await apply_group(name, save_time)
                elif name == "live":

                    def save_live(value: Any = value) -> None:
                        station.live.credentials.save_settings(value)
                        station.live.settings = value

                    await apply_group(name, save_live)
                elif name == "recording":
                    if station.recorder is None:

                        def save_recording_settings(value: Any = value) -> Any:
                            return _save_recording_settings(station, value)

                        await apply_group(name, save_recording_settings)
                    else:

                        def configure_recording(value: Any = value) -> Any:
                            return station.recorder.configure(
                                value.split, value.retention_days, value.free_space_mb
                            )

                        await apply_group(name, configure_recording)
                elif name == "stream":

                    async def save_stream(value: Any = value) -> None:
                        cfg = station.cfg
                        changed_formats: list[str] = []
                        if station.bridge.stream_mode:
                            formats = {
                                label: bitrate
                                for label, bitrate in (
                                    ("aac", value.aac_bitrate),
                                    ("opus", value.opus_bitrate),
                                )
                                if bitrate is not None
                            }
                            if formats:
                                changed_formats = await asyncio.to_thread(
                                    station.bridge.set_stream_formats, formats
                                )
                            if value.bitrate is not None:
                                try:
                                    await asyncio.to_thread(
                                        station.bridge.set_stream_bitrate, value.bitrate
                                    )
                                except EncoderUnavailableError as exc:
                                    if changed_formats:
                                        await asyncio.to_thread(
                                            station.bridge.save_persistent_state, strict=True
                                        )
                                        labels = " and ".join(
                                            FORMATS[name].label for name in changed_formats
                                        )
                                        raise RuntimeError(
                                            f"The MP3 encoder failed; the {labels} change was saved, "
                                            "and MP3 quality is unchanged."
                                        ) from exc
                                    raise
                        metadata_changed = any(
                            getattr(cfg.stream, field) != getattr(value, field)
                            for field in ("station_name", "genre", "description", "url")
                            if getattr(value, field) is not None
                        )
                        for field in (
                            "bitrate",
                            "aac_bitrate",
                            "opus_bitrate",
                            "stats_days",
                            "continuous",
                            "station_name",
                            "genre",
                            "description",
                            "url",
                            "max_listeners_per_address",
                        ):
                            item = getattr(value, field)
                            if item is not None:
                                setattr(cfg.stream, field, item)
                        if value.stats_days is not None and station.listener_stats is not None:
                            await asyncio.to_thread(station.listener_stats.tick)
                            await asyncio.to_thread(station.listener_stats.flush)
                        if value.show_next is not None:
                            cfg.nowplaying.show_next = value.show_next
                        if (
                            value.max_listeners_per_address is not None
                            and station.listener_access is not None
                        ):
                            station.listener_access.max_per_address = (
                                value.max_listeners_per_address
                            )
                        await asyncio.to_thread(station.bridge.save_persistent_state, strict=True)
                        if station.bridge.stream_mode:
                            station.nowplaying.refresh()
                        if (
                            station.bridge.stream_mode
                            and metadata_changed
                            and station.bridge.relays is not None
                        ):
                            await asyncio.to_thread(
                                _refresh_relay_metadata, station, request.app.state.stations
                            )

                    await apply_group(name, save_stream)

            values = await asyncio.to_thread(
                _station_settings_values, station, request, body.profile
            )
            document = await asyncio.to_thread(station.bridge.get_settings)
            document.update({"values": values, "revision": _settings_revision(values)})
            _profile_document(station, document, body.profile)
            response = {
                "revision": document["revision"],
                "applied": sorted(applied),
                "settings": document,
            }
            if failed:
                return JSONResponse(status_code=207, content={**response, "failed": failed})
            return response

    @router.get("/stereo-tool-presets")
    async def api_stereo_tool_presets(station: SavedStationDep) -> dict:
        from autodj.stereotool_presets import INCLUDED, list_presets

        presets = await asyncio.to_thread(list_presets, station.cfg.index.shared_dir)
        active = station.cfg.playback.stereotool_preset
        if active not in {preset["name"] for preset in presets}:
            active = INCLUDED
        return {"active": active, "presets": presets}

    @router.get("/status")
    async def api_status(station: StationDep) -> JSONResponse:
        """Return the current player state snapshot."""
        state = await asyncio.to_thread(station.bridge.get_state)
        state.update(station.live_state())
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
        station: SavedStationDep,
        page: int = Query(1, ge=1),
        per_page: int = Query(50, ge=1, le=500),
        liners: bool = False,
        anchor: Annotated[str | None, Query(max_length=64)] = None,
    ) -> JSONResponse:
        """Return recent play history across restarts (newest first), one page.

        Songs and live sets, or with *liners* everything that aired (#601):
        liners, ads, matched intros and the rest, each with its ``kind``.
        With *anchor*, a row's ``event_id``, the page is the one holding
        that row, or the next older row this view shows; switching views
        keeps the listener near the same place.
        """
        if station.player is not None:
            result = station.bridge.history_page(page, per_page, liners=liners, anchor=anchor)
        else:
            rows = station.ledger.recent_history(HISTORY_VIEW_ROWS, inserts=HISTORY_AIRED_ROWS)
            for order, row in enumerate(rows):
                row["_seq"] = order
            result = history_page(
                [row for row in rows if row["kind"] not in INSERT_KINDS],
                [row for row in rows if row["kind"] in INSERT_KINDS],
                page=page,
                per_page=per_page,
                liners=liners,
                anchor=anchor,
            )
        return JSONResponse(result)

    @router.get("/history/export")
    async def api_history_export(
        station: SavedStationDep,
        from_text: Annotated[str, Query(alias="from", max_length=64)],
        to_text: Annotated[str, Query(alias="to", max_length=64)],
        report_format: Annotated[Literal["csv", "soundexchange"], Query(alias="format")] = "csv",
        service: Annotated[str, Query(max_length=200)] = "",
        category: Annotated[str, Query(max_length=1)] = "A",
        liners: bool = False,
    ) -> Response:
        """Download the play log between two dates (see :mod:`autodj.playlog`).

        Dates are ``YYYY-MM-DD`` in the server's time zone and ``to`` is
        included.  ``format=soundexchange`` gives a Report of Use for
        *service* and transmission *category* instead of the CSV.  The CSV
        also lists the ad spots that aired (#51), as ``kind`` ``ad``, and
        with *liners* the other audio aired over or between the songs (#601);
        a Report of Use lists songs only either way.
        ``X-Play-Count`` and ``X-Songs-Missing-Fields`` give the page the
        numbers it announces.
        """
        library = _catalog_library(station)
        try:
            start = parse_bound(from_text)
            end = parse_bound(to_text, end=True)
            inserts = liners and report_format == "csv"
            plays = await asyncio.to_thread(
                functools.partial(
                    plays_between,
                    station=station.slug,
                    station_store=station.ledger.store,
                    include_inserts=inserts,
                ),
                library.catalog,
                start,
                end,
            )
            if inserts:
                # The ad play log below is the record of the spots that aired.
                plays = [play for play in plays if play.kind != "ad"]
            last_day = end - timedelta(microseconds=1)
            count = len(plays)
            if report_format == "soundexchange":
                body = soundexchange_report(plays, service, category)
                media_type = "text/plain; charset=us-ascii"
                name = soundexchange_filename(service, start, last_day)
            else:
                from autodj.ads import aired_plays

                ads = await asyncio.to_thread(aired_plays, station.paths.ad_plays, start, end)
                rows = sorted([*plays, *ads], key=lambda play: play.start)
                body = plays_csv(rows)
                count = len(rows)
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
                "X-Play-Count": str(count),
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
            new_pos = station.bridge.seek(seconds=body.seconds, delta=body.delta, path=body.path)
        except StreamSeekUnavailable as exc:
            raise HTTPException(status_code=409, detail=STREAM_SEEK_UNAVAILABLE) from exc
        except SeekTrackChanged as exc:
            raise HTTPException(status_code=409, detail=SEEK_TRACK_CHANGED) from exc
        return {"elapsed": round(new_pos, 2)}

    # ------------------------------------------------------------------
    # Stream configuration remains editable while the station is stopped.
    # ------------------------------------------------------------------

    @router.get("/stream")
    async def api_stream_info(station: SavedStationDep) -> dict:
        """Return the stream URL paths, bitrate, listener count and state."""
        return _stream_info(station)

    @router.get("/stream/relays")
    async def api_stream_relays(station: SavedStationDep) -> dict:
        statuses = {item["name"]: item for item in station.bridge.relay_status()}
        return {
            "relays": [
                public_relay(cfg, statuses.get(name, {}), station.slug)
                for name, cfg in station.cfg.stream.relays.items()
            ]
        }

    @router.post("/stream/relays")
    async def api_stream_relay_save(station: SavedStationDep, request: Request) -> Any:
        registry = request.app.state.stations
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("Expected a relay settings object.")
            body = RelaySettingsBody.model_validate(data)
        except (TypeError, ValueError, ValidationError) as exc:
            if isinstance(exc, ValidationError):
                issue = exc.errors()[0]
                field = str(issue["loc"][-1]) if issue["loc"] else "name"
                detail = (
                    "Name may use letters, numbers, internal spaces, hyphens, and underscores (1 to 32 characters)."
                    if field == "name"
                    else f"{field}: {issue['msg']}"
                )
            else:
                field, detail = "name", str(exc)
            return JSONResponse(status_code=422, content={"detail": detail, "field": field})
        return await asyncio.to_thread(_save_stream_relay, station, body, registry)

    @router.delete("/stream/relays/{name}")
    async def api_stream_relay_remove(
        station: SavedStationDep, name: str, request: Request
    ) -> dict:
        registry = request.app.state.stations
        return await asyncio.to_thread(_remove_stream_relay, station, name, registry)

    @router.get("/recording")
    async def api_recording_status(station: SavedStationDep) -> dict[str, object]:
        if station.recorder is None:
            return _recording_saved_status(station)
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

    @router.get("/webhooks")
    async def api_webhooks(station: SavedStationDep) -> list[dict[str, str | int]]:
        """Show redacted webhook destinations to a paired browser."""
        if station.player is None:
            return _stopped_webhooks(station)
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

    @router.get("/stream/stats")
    async def api_stream_stats(
        station: StationDep, days: Annotated[int, Query(ge=1, le=MAX_STATS_DAYS)] = 30
    ) -> JSONResponse:
        """Listener statistics for the last *days* days: counts only, no addresses (#50)."""
        _require_stream_mode(station)
        stats = station.listener_stats
        if stats is None:
            raise HTTPException(status_code=404, detail="Not Found")
        bridge = station.bridge
        listening = (
            bridge.mounts.counts()
            if bridge.mounts is not None
            else {"mp3": int(bridge.stream.listener_count)}
        )
        summary = await asyncio.to_thread(stats.summary, days, listening)
        return JSONResponse(summary, headers=_NO_STORE)

    @router.get("/stream/stats/export")
    async def api_stream_stats_export(station: StationDep) -> Response:
        """Download every kept day of listener statistics as CSV."""
        _require_stream_mode(station)
        stats = station.listener_stats
        if stats is None:
            raise HTTPException(status_code=404, detail="Not Found")
        body = await asyncio.to_thread(stats.csv_text)
        return Response(
            body.encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={
                **_NO_STORE,
                "Content-Disposition": f'attachment; filename="autodj-listeners-{station.slug}.csv"',
            },
        )

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    @router.get("/profiles")
    async def api_profiles(station: SavedStationDep) -> dict:
        """List saved profile bundles."""
        store = _profile_store(station)
        from autodj.profile_settings import profile_settings

        return {
            "profiles": await asyncio.to_thread(store.list_names),
            "root": str(store.root),
            "active": profile_settings(station).active,
            "clockwheels": await asyncio.to_thread(_clock_store(station).list_names),
            "cart_pages": [page.name for page in station.carts.store.wall.pages],
        }

    @router.post("/profiles")
    async def api_profile_save(station: SavedStationDep, body: ProfileSaveBody) -> dict:
        """Save (or overwrite) a profile bundle."""
        from autodj.profiles import ProfileSnapshot

        values = body.model_dump()
        snap = ProfileSnapshot(**values)
        from autodj.profile_settings import profile_settings

        with _profile_http_errors():
            await asyncio.to_thread(profile_settings(station).create, snap)
        return {"saved": snap.name}

    @router.post("/profiles/apply-defaults")
    async def api_profile_defaults(station: SavedStationDep) -> dict:
        return await apply_profile(station, "")

    @router.post("/profiles/{name}/reset")
    async def api_profile_reset(station: SavedStationDep, name: str) -> dict:
        from autodj.profile_settings import profile_settings

        with _profile_http_errors():
            await asyncio.to_thread(profile_settings(station).reset, name)
        return {"reset": name}

    @router.delete("/profiles/{name}")
    async def api_profile_delete(station: SavedStationDep, name: str) -> dict:
        """Delete a saved profile bundle by name."""
        from autodj.profile_settings import profile_settings

        if profile_settings(station).active == name:
            raise HTTPException(
                status_code=409,
                detail="Apply another profile or Station defaults before deleting the active profile",
            )
        with _profile_http_errors():
            ok = await asyncio.to_thread(_profile_store(station).delete, name)
        if not ok:
            raise HTTPException(status_code=404, detail="Profile not found")
        return {"deleted": name}

    @router.post("/profiles/{name}/apply")
    async def api_profile_apply(station: SavedStationDep, name: str) -> dict:
        """Load a saved profile and push every set field through the bridge."""
        return await apply_profile(station, name)

    # ------------------------------------------------------------------
    # Liners
    # ------------------------------------------------------------------

    @router.get("/liners")
    async def api_liners(station: SavedStationDep) -> dict:
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
            "folder": str(folder),
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
        station: SavedStationDep, file: UploadFile = File(...), replace: bool = False
    ) -> dict[str, str | int]:
        """Validate, bound, and atomically store a liner clip."""
        from autodj.break_audio import AudioCheckError, BreakAudioError, check_playable
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
                max_bytes=(station.cfg.station.liner_upload_max_mib * 1024 * 1024),
                replace=replace,
                # A file that is not audio is refused now, not when it plays
                # (#323), with the header check the breaks use.
                check=lambda part: check_playable(part, os.path.splitext(name)[1]),
            )
        except InvalidLinerName as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except LinerConflictError as exc:
            raise HTTPException(status_code=409, detail="Liner already exists") from exc
        except LinerTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except AudioCheckError as exc:
            logger.warning("Liner upload %r not checked: %s", name, exc.__cause__ or exc)
            raise HTTPException(
                status_code=503,
                detail=f"Could not check {name}, so it was not saved. Try again in a moment.",
            ) from exc
        except BreakAudioError as exc:
            raise HTTPException(
                status_code=422, detail=f"{name} is not an audio file AutoDJ can play."
            ) from exc
        return {"filename": target.name, "size": size}

    @router.delete("/liners/file/{name}")
    async def api_liner_delete(station: SavedStationDep, name: str) -> dict[str, str]:
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
        station: SavedStationDep, liner_id: str, body: dict[str, Any] = Body(...)
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
    async def api_liner_file(station: SavedStationDep, name: str) -> FileResponse:
        """Serve one validated plain liner file."""
        from autodj.liner_files import InvalidLinerName, resolve_liner_path

        try:
            path, metadata, registered = await asyncio.to_thread(
                resolve_liner_path, station.bridge.library, station.paths.liners, name
            )
        except InvalidLinerName as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Liner not found") from exc
        if registered:
            return FileResponse(path, media_type=audio_mime(path), stat_result=metadata)
        return FileResponse(path, stat_result=metadata)

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
        if station.live.status() is not None:
            # AutoDJ does not talk over a live DJ (#48).
            return {
                "played": None,
                "message": "A live DJ is on the air, so the liner was not played.",
            }
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
    ) -> dict[str, Any] | JSONResponse:
        """Make a track the one that plays next, after the song playing.

        There is one "play next" song (#519): another Play next takes its
        place.  The answer names a song it replaced, so the page can say so.
        """
        # Off the event loop: the check reads the renders and the library (#217).
        if refusal := await asyncio.to_thread(_dmca_refusal, station, body, append=False):
            return refusal
        earlier = station.bridge.player._state.queued_next
        try:
            queued = station.bridge.play_next(
                body.path,
                now=body.now,
                voice_action=body.voice_action,
                voice_revision=body.voice_revision,
            )
        except VoiceQueueConflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        if not queued:
            raise HTTPException(status_code=404, detail=TRACK_GONE_DETAIL)
        station.nowplaying.refresh()
        replaced = (
            earlier
            if not body.now
            and earlier is not None
            and station.bridge.player._state.queued_next is not earlier
            and getattr(earlier, "path", None) != body.path
            else None
        )
        if replaced is None:
            return {"ok": True}
        return {
            "ok": True,
            "replaced": {
                "path": replaced.path,
                "artist": replaced.artist or "",
                "title": replaced.title or "",
            },
        }

    @router.post("/queue/add", response_model=None)
    async def api_queue_add(
        station: StationDep, body: PlayNextBody
    ) -> dict[str, bool] | JSONResponse:
        """Append a track to the user-ordered queue."""
        # Off the event loop: the check reads the renders and the library (#217).
        if refusal := await asyncio.to_thread(_dmca_refusal, station, body, append=True):
            return refusal
        try:
            queued = station.bridge.queue_add(
                body.path,
                voice_action=body.voice_action,
                voice_revision=body.voice_revision,
            )
        except VoiceQueueConflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        if not queued:
            raise HTTPException(status_code=404, detail=TRACK_GONE_DETAIL)
        station.nowplaying.refresh()
        return {"ok": True}

    @router.post("/queue/remove")
    async def api_queue_remove(station: StationDep, body: QueueRemoveBody) -> dict[str, bool]:
        """Remove a track from the user-ordered queue."""
        try:
            removed = station.bridge.queue_remove(
                body.path,
                body.position,
                voice_action=body.voice_action,
                voice_revision=body.voice_revision,
            )
        except VoiceQueueConflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        if removed:
            station.nowplaying.refresh()
        return {"ok": removed}

    @router.post("/queue/reorder")
    async def api_queue_reorder(station: StationDep, body: QueueReorderBody) -> dict[str, bool]:
        """Reorder the user-ordered queue."""
        try:
            changed = station.bridge.queue_reorder(
                body.paths,
                from_paths=body.from_paths,
                order=body.order,
                voice_action=body.voice_action,
                voice_revision=body.voice_revision,
            )
        except VoiceQueueConflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
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
            if isinstance(body, dict) and isinstance(body.get("blacklist"), str):
                # Only a path: any other JSON value would go into the
                # no-repeat window, and an object or list there breaks
                # every later pick (the picker builds a set from it).
                blacklist = body["blacklist"]
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

    # Settings configuration is saved through PATCH /settings. The discovery toggle below
    # remains operational and immediate.
    @router.post("/discovery/toggle")
    async def api_discovery_toggle(station: StationDep) -> dict[str, bool]:
        """Turn discovery on or off and remember it (the Now Playing Discovery button)."""
        enabled = station.bridge.toggle_discovery()
        station.bridge.save_persistent_state()
        return {"discovery_enabled": enabled}

    @router.post("/eq")
    async def api_eq(station: StationDep, body: EqBody) -> dict[str, float]:
        """Adjust the live EQ immediately, like the other Now Playing controls."""
        result = station.bridge.set_eq(low=body.low, mid=body.mid, high=body.high)
        station.bridge.save_persistent_state()
        return result

    # ------------------------------------------------------------------
    # Clockwheels: each station has its own, and its own active one.
    # They keep their old Library tools addresses.
    # ------------------------------------------------------------------

    @router.get("/library/clock")
    async def api_library_clock(station: SavedStationDep) -> dict:
        return await asyncio.to_thread(_clock_snapshot, station, _catalog_library(station))

    @router.post("/library/clock")
    async def api_library_create_clock(station: SavedStationDep, body: ClockBody) -> dict:
        library = _catalog_library(station)
        try:
            clock = await asyncio.to_thread(
                _validate_native_clock, station, library, body.model_dump()
            )
            return _clock_store(station).save(clock, overwrite=False)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/library/clock/check")
    async def api_library_clock_check(station: SavedStationDep, body: ClockStepCheckBody) -> dict:
        library = _catalog_library(station)

        def count() -> dict:
            try:
                from autodj.clockwheel import ClockSlot, condition_paths
                from autodj.clockwheel_store import resolve_conditions, validate_step

                step = validate_step(body.step)
                if step["type"] in {"settings", "ad_break"}:
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
                    _, results = _station_categories(station, library).resolutions()
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
                categories_index = _station_categories(station, library)
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
    async def api_library_select_clock(station: SavedStationDep, body: ClockNameBody) -> dict:
        try:
            return await select_clock(station, _catalog_library(station), body.name)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/library/clock/import")
    async def api_library_import_clock(station: SavedStationDep, body: PalImportBody) -> dict:
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
    async def api_library_get_clock(station: SavedStationDep, name: str) -> dict:
        return _existing_clock(station, name)

    @router.put("/library/clock/{name}")
    async def api_library_update_clock(
        station: SavedStationDep,
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
            if station.player is not None and _active_clock(station, library) == name:
                await asyncio.to_thread(station.player.apply_clockwheel_update, name, apply_timing)
            return clock
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/library/clock/{name}/duplicate")
    async def api_library_duplicate_clock(
        station: SavedStationDep, name: str, body: NewClockNameBody
    ) -> dict:
        clock = _existing_clock(station, name)
        try:
            return _clock_store(station).save({**clock, "name": body.name}, overwrite=False)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/library/clock/{name}/rename")
    async def api_library_rename_clock(
        station: SavedStationDep, name: str, body: NewClockNameBody
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
    async def api_library_delete_clock(station: SavedStationDep, name: str) -> dict:
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
    install_categories(router)
    install_scheduler(router)
    install_time_checks(router)
    install_breaks(router)
    install_ads(router)
    install_requests(router)
    install_matched_intros(router)
    install_shows(router)
    from autodj.prepared_api import install_prepared
    from autodj.prepared_review import prepared_review_receipt

    install_prepared(
        router,
        receipt=prepared_review_receipt,
        active=lambda station, identity: (
            runner.edition_active(identity)
            if (runner := getattr(station, "show_runner", None)) is not None
            else False
        ),
    )
    from autodj.review_api import install_reviews

    install_reviews(router)
    install_rotation_test(router)
    install_hour_preview(router)
    install_landmarks(router)
    install_live_api(router)
    install_stereo_tool(router)


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
