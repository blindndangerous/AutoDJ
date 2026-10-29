"""Spoken station time, rendered as one voice-liner clip.

A time check is the optional intro clip, the time and the optional outro
clip, decoded into one stereo clip and played through the voice liner
scheduler, so it gets the liner duck depth and, after a song change, the
liner talk-up timing.  The time comes from the first source that has it: an
enabled voice pack (hour and minute files joined by :mod:`autodj.voice_packs`),
a recorded clip named for the station-local minute (``HH_MM``), or an
operator-configured text to speech command.

Voice packs are the folders listed in ``[time_checks] voice_packs`` and every
folder in the station's time checks folder, found again whenever the
settings are read, so a pack copied in or added from the page needs no
restart (:func:`found_packs`).  A paired browser changes the switches in
:class:`SettingsBody`, names packs, clips and folders by name, never by path,
and adds or removes packs and clips through :mod:`autodj.time_check_files`.
The text to speech command comes only from the config files, because it runs
a program on the server.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import logging
import os
import random
import secrets
import subprocess  # nosec B404 -- operator-configured argv, never a shell
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import numpy as np
import soundfile as sf
from fastapi import Body, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, field_validator

from autodj.clip_dates import ClipDates, ClipDatesError, parse_dates
from autodj.fsutil import confirm_missing
from autodj.liner_scheduler import LinerDecodeError, decode_liner_path
from autodj.scheduler import Action
from autodj.station_routes import SavedStationDep, StationDep, StationRouter
from autodj.stations import StationPaths
from autodj.stereo import SAMPLE_RATE
from autodj.time_check_files import (
    MAX_PACK_UPLOAD_BYTES,
    MINUTE_CLIP,
    PackConflict,
    PackError,
    PackExists,
    PackNotFound,
    PackTooLarge,
    check_clip_name,
    check_pack_name,
    clip_files,
    hidden_name,
    install_clip,
    install_pack,
    only_minute_clips,
    pack_name_from,
    receive_upload,
    remove_pack,
    subfolders,
    sweep_leftovers,
)
from autodj.tts_command import TTS_TIMEOUT_S, run_command
from autodj.voice_packs import JoinCache, coverage, join_voice, pieces_for, scan_pack, signature

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig

logger = logging.getLogger(__name__)
EXTENSIONS = (".wav", ".mp3", ".flac", ".ogg")
STATE_VERSION = 1
MIB = 1024 * 1024
#: The time check routes that take an upload, for the request body caps.
PACK_UPLOAD_PATH = "/api/time-checks/packs"
CLIP_UPLOAD_PATH = "/api/time-checks/clips"


def spoken_text(now: datetime, hour_format: str) -> str:
    """Say a station-local minute in natural English."""
    hour = (now.hour % 12 or 12) if hour_format == "12" else now.hour
    if now.minute:
        return f"It's {hour}:{now.minute:02d}"
    if now.hour in (0, 12):
        return "It's midnight" if now.hour == 0 else "It's noon"
    return f"It's {hour} o'clock" if hour_format == "12" else f"It's {hour} hundred"


def clips_folder(cfg: AutoDJConfig, paths: StationPaths | None = None) -> Path:
    """The recorded clips folder: ``clips_folder``, or ``time_checks`` in the station folder."""
    return (paths or StationPaths.default(cfg)).time_check_clips


def clip_path(folder: Path, now: datetime) -> Path | None:
    """Find the one clip named for this local 24-hour minute."""
    stem = f"{now.hour:02d}_{now.minute:02d}"
    return next(
        (folder / f"{stem}{ext}" for ext in EXTENSIONS if (folder / f"{stem}{ext}").is_file()), None
    )


def missing_minutes(folder: Path) -> list[str]:
    """List every minute without a recorded clip."""
    present = {path.name for path in folder.iterdir()} if folder.is_dir() else set()
    return [
        f"{hour:02d}_{minute:02d}"
        for hour in range(24)
        for minute in range(60)
        if not any(f"{hour:02d}_{minute:02d}{ext}" in present for ext in EXTENSIONS)
    ]


def shared_folder(cfg: AutoDJConfig) -> Path:
    """The shared time checks folder, ``<index_dir>/shared/time_checks``.

    Every station finds the voice packs in it, as well as those in its own
    time checks folder; for the default station without a ``clips_folder``
    the two are the same folder.
    """
    return Path(cfg.index.shared_dir) / "time_checks"


@dataclass(frozen=True)
class PackFolder:
    """Where one voice pack is, and how AutoDJ found it.

    Attributes:
        path: The pack's folder.
        source: ``config`` (listed in ``voice_packs``), ``station`` (a folder
            in the station's time checks folder) or ``shared`` (a folder in
            the library's time checks folder, :func:`shared_folder`).
    """

    path: Path
    source: str

    @property
    def removable(self) -> bool:
        """Only a folder in the station's own time checks folder may be removed from the page."""
        return self.source == "station"


@dataclass(frozen=True)
class FoundPacks:
    """The voice packs by name, in order, and the folders left out for a taken name."""

    packs: dict[str, PackFolder]
    clashes: tuple[str, ...] = ()


def _folder_key(path: Path) -> str:
    """One spelling per folder: links and ``..`` resolved, case folded where the system does."""
    return os.path.normcase(os.path.realpath(path))


def _default_recorded_folder(cfg: AutoDJConfig) -> str:
    """The recorded clips folder the default station chose, by name, or ``""``.

    Read from the default station's ``time_checks.json`` so another station
    does not list that folder of minute clips as a shared voice pack.
    """
    try:
        state = json.loads(StationPaths.default(cfg).time_checks.read_text(encoding="utf-8"))
        chosen = state["settings"]["recorded_folder"]
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    return chosen if isinstance(chosen, str) else ""


def found_packs(
    cfg: AutoDJConfig, paths: StationPaths | None = None, *, skip: str = ""
) -> FoundPacks:
    """Every voice pack: those listed in config, then the folders found.

    The order is the ``voice_packs`` list, then the folders in the station's
    time checks folder (``[time_checks] clips_folder`` or the default), then
    those in :func:`shared_folder`, each by name.  A pack's name is its
    folder name, compared without regard to case.  One folder is one pack,
    however it was found, and the first source wins: a configured pack
    keeps its name over a found folder, and the station's own folder over
    the shared one; the folder left out is named in ``clashes``.  *skip* is
    the station folder chosen for recorded minute clips, which is not a
    pack, and neither is the default station's choice in the shared folder,
    nor a found folder whose audio files are all minute clips
    (``15_05.wav``).  Hidden folders (``.name``) are AutoDJ's unfinished
    uploads and are never packs.
    """
    paths = paths or StationPaths.default(cfg)
    packs: dict[str, PackFolder] = {}
    names: dict[str, str] = {}
    folders: set[str] = set()
    clashes: list[str] = []

    def add(folder: Path, source: str) -> None:
        key = _folder_key(folder)
        if key in folders:
            return
        folders.add(key)
        taken = names.get(folder.name.casefold())
        if taken is not None:
            clashes.append(
                f"{folder} is not used: the voice pack name {folder.name} is taken by "
                f"{packs[taken].path}"
            )
            return
        names[folder.name.casefold()] = folder.name
        packs[folder.name] = PackFolder(folder, source)

    for listed in cfg.time_checks.voice_packs:
        add(Path(listed), "config")
    own = clips_folder(cfg, paths)
    for folder in subfolders(own):
        if folder.name != skip and not only_minute_clips(folder):
            add(folder, "station")
    shared = shared_folder(cfg)
    if _folder_key(shared) != _folder_key(own):
        theirs = _default_recorded_folder(cfg)
        for folder in subfolders(shared):
            if folder.name != theirs and not only_minute_clips(folder):
                add(folder, "shared")
    return FoundPacks(packs, tuple(clashes))


def pack_folders(cfg: AutoDJConfig, paths: StationPaths | None = None) -> dict[str, Path]:
    """Every voice pack's folder by name (see :func:`found_packs`)."""
    return {name: found.path for name, found in found_packs(cfg, paths).packs.items()}


def clip_sources(
    cfg: AutoDJConfig, settings: SettingsBody, paths: StationPaths | None = None
) -> tuple[Path, Path | None, Path | None]:
    """The recorded minute clips folder and the intro and outro files in use.

    Each comes from the choice saved from Settings, by name in the
    station's time checks folder; an intro or outro never chosen there is
    ``intro_clip`` or ``outro_clip`` from the config files.
    """
    root = clips_folder(cfg, paths)
    folder = root / settings.recorded_folder if settings.recorded_folder else root

    def chosen(value: str | None, configured: str | None) -> Path | None:
        if value is None:
            return Path(configured) if configured else None
        return root / value if value else None

    return (
        folder,
        chosen(settings.intro, cfg.time_checks.intro_clip),
        chosen(settings.outro, cfg.time_checks.outro_clip),
    )


class SettingsBody(BaseModel):
    """Only non-secret switches are writable from a paired browser.

    ``enabled_packs`` holds pack names; a name that is not a pack is
    refused by the route and ignored when it comes from a saved file.
    ``recorded_folder`` names a folder in the station's time checks folder
    for the minute clips (``""``: the time checks folder itself).  ``intro``
    and ``outro`` name a file there; ``""`` is none, and ``None`` (never
    chosen) uses ``intro_clip`` or ``outro_clip`` from the config files.
    Names only: a path is refused.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    hour_format: StrictStr
    rule: StrictStr
    enabled_packs: list[StrictStr]
    pack_order: StrictStr
    recorded_folder: StrictStr = ""
    intro: StrictStr | None = None
    outro: StrictStr | None = None

    @field_validator("hour_format")
    @classmethod
    def check_format(cls, value: str) -> str:
        if value not in ("12", "24"):
            raise ValueError("hour_format must be 12 or 24")
        return value

    @field_validator("rule")
    @classmethod
    def check_rule(cls, value: str) -> str:
        if value not in ("off", "after_hour_song"):
            raise ValueError("rule must be off or after_hour_song")
        return value

    @field_validator("enabled_packs")
    @classmethod
    def check_packs(cls, value: list[str]) -> list[str]:
        if len(value) > 100 or len(set(value)) != len(value):
            raise ValueError("enabled_packs must name each pack once, at most 100")
        return value

    @field_validator("pack_order")
    @classmethod
    def check_order(cls, value: str) -> str:
        if value not in ("rotate", "random"):
            raise ValueError("pack_order must be rotate or random")
        return value

    @field_validator("recorded_folder")
    @classmethod
    def check_folder(cls, value: str) -> str:
        if value:
            try:
                check_pack_name(value)
            except PackError as exc:
                raise ValueError(f"recorded_folder: {exc}") from None
            if value != value.strip():
                raise ValueError("recorded_folder must be a folder name")
        return value

    @field_validator("intro", "outro")
    @classmethod
    def check_clip(cls, value: str | None) -> str | None:
        if value:
            try:
                check_clip_name(value)
            except PackError as exc:
                raise ValueError(str(exc)) from None
        return value


class PlayNowBody(BaseModel):
    """Optionally test one configured pack by name."""

    model_config = ConfigDict(extra="forbid")
    pack: StrictStr | None = None


def state_path(cfg: AutoDJConfig, paths: StationPaths | None = None) -> Path:
    """Where the switches saved from Settings live."""
    return (paths or StationPaths.default(cfg)).time_checks


def load_settings(cfg: AutoDJConfig, paths: StationPaths | None = None) -> SettingsBody:
    """The switches saved from Settings over the config file's.

    A key the saved file lacks takes the config value; with no saved
    ``enabled_packs``, every pack is enabled.

    Raises:
        ValueError: The saved file has another version or bad values.
    """
    tc = cfg.time_checks
    defaults = {
        "enabled": tc.enabled,
        "hour_format": tc.hour_format,
        "rule": tc.rule,
        "enabled_packs": list(pack_folders(cfg, paths)),
        "pack_order": tc.pack_order,
    }
    path = state_path(cfg, paths)
    if not path.is_file():
        return SettingsBody.model_validate(defaults)
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != STATE_VERSION:
        raise ValueError(f"Unsupported time checks state version in {path}")
    return SettingsBody.model_validate({**defaults, **state["settings"]})


def _packs_saved(path: Path) -> bool:
    """Whether ``time_checks.json`` names the ticked packs.

    A file that cannot be read counts as naming them, so no pack is ticked
    on its own while the saved choice is out of reach.
    """
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    settings = state.get("settings") if isinstance(state, dict) else None
    return not isinstance(settings, dict) or "enabled_packs" in settings


#: The keys ``time_checks.json`` holds; any other is warned about and ignored.
_STATE_KEYS = frozenset({"version", "settings", "pack_dates"})


@dataclass
class PackDatesState:
    """What ``time_checks.json`` holds besides the switches.

    Attributes:
        dates: Each voice pack's play dates that could be read, by name.
        unread: ``pack_dates`` entries that could not be read, as written;
            they are saved back unchanged.
        extra: Top-level keys this version does not know, as written; they
            are saved back unchanged, so a newer AutoDJ's settings survive.
    """

    dates: dict[str, ClipDates] = field(default_factory=dict)
    unread: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    #: The file could not be read just now: saves are refused until it can.
    unreadable: bool = False
    #: The file is not valid JSON: it is set aside before the next save.
    damaged: bool = False


def read_pack_dates(cfg: AutoDJConfig, paths: StationPaths | None = None) -> PackDatesState:
    """Each voice pack's play dates, saved from Settings, and what is kept as written.

    They live beside the switches in ``time_checks.json`` under
    ``pack_dates``, written only when a pack has dates, so an older AutoDJ,
    which reads only the switches, still reads the file.  A pack's dates
    that cannot be read, and keys this version does not know, are warned
    about and not used, but kept as written for the next save.  Dates of a
    pack no longer in config.toml are kept, so they come back with it.
    """
    path = state_path(cfg, paths)
    try:
        return pack_dates_from(_read_state(path), path)
    except FileNotFoundError as exc:
        if not path.parent.exists():
            # A station folder not made yet: nothing saved, and the first
            # save makes it.  (A share away at start looks the same; its
            # file is not written over, as the save makes a folder of its own
            # or fails.)
            return PackDatesState()
        logger.warning("Time checks: could not read the voice pack dates in %s: %s", path, exc)
        return PackDatesState(unreadable=True)
    except OSError as exc:
        logger.warning("Time checks: could not read the voice pack dates in %s: %s", path, exc)
        return PackDatesState(unreadable=True)
    except ValueError as exc:
        logger.warning("Time checks: the voice pack dates in %s are not valid: %s", path, exc)
        return PackDatesState(damaged=True)


class TimeChecksUnreadable(OSError):
    """``time_checks.json`` could not be read just now, so nothing was saved over it."""


def _read_state(path: Path) -> dict[str, Any] | None:
    """``time_checks.json`` as read, or ``None`` when there is none.

    Raises:
        OSError: It could not be read, or its folder is away (a dropped
            share also says "not found"; see :func:`confirm_missing`).
        ValueError: It is not a JSON object.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        confirm_missing(path, exc)
        return None
    state = json.loads(text)
    if not isinstance(state, dict):
        raise ValueError("time_checks.json is not a JSON object")
    return state


def pack_dates_from(state: dict[str, Any] | None, path: Path) -> PackDatesState:
    """The voice pack dates and kept keys of a read ``time_checks.json`` (see above)."""
    if state is None:
        return PackDatesState()
    found = PackDatesState(extra={key: state[key] for key in state if key not in _STATE_KEYS})
    for key in sorted(found.extra):
        logger.warning("Time checks: ignoring unknown key %r in %s", key, path)
    saved = state.get("pack_dates") or {}
    if not isinstance(saved, dict):
        logger.warning("Time checks: ignoring pack_dates in %s: it is not a table", path)
        found.extra["pack_dates"] = saved
        return found
    for name, value in saved.items():
        try:
            dates = parse_dates(value)
        except ClipDatesError as exc:
            logger.warning(
                "Time checks: ignoring the dates of voice pack %s in %s: %s", name, path, exc
            )
            found.unread[str(name)] = value
            continue
        if not dates.empty:
            found.dates[str(name)] = dates
    return found


def _has_audio(path: Path) -> bool:
    """Whether *path* is a sound file with at least one frame."""
    try:
        return sf.info(str(path)).frames > 0
    except (RuntimeError, OSError):  # soundfile.LibsndfileError is a RuntimeError
        return False


HourKey = tuple[int, int, int, int, int]


@dataclass(frozen=True)
class Rendered:
    """A time check ready to play, and where its voice came from."""

    audio: np.ndarray
    source: str


def _hour_key(now: datetime) -> HourKey:
    """One local clock hour; the offset tells a repeated DST hour apart."""
    offset = now.utcoffset()
    return (
        now.year,
        now.month,
        now.day,
        now.hour,
        int(offset.total_seconds()) if offset else 0,
    )


class TimeChecks:
    """Own runtime settings, the TTS cache and the pending top-of-hour rule.

    Thread-safety: :meth:`tick` runs on the event loop, :meth:`due_audio`
    on the liner worker and :meth:`fire` on request worker threads; one
    lock covers the settings, the pending flag and the hour that last aired.
    """

    def __init__(self, cfg: AutoDJConfig, timezone: str, paths: StationPaths | None = None) -> None:
        """Load the station's saved switches; *paths* defaults to the default station's."""
        paths = paths or StationPaths.default(cfg)
        self.cfg = cfg
        self.config = cfg.time_checks
        self.paths = paths
        self.zone = ZoneInfo(timezone)
        #: The station's time checks folder: minute clips, intro and outro
        #: clips, and one folder per voice pack added from the page.
        self.root = clips_folder(cfg, paths)
        self.cache = paths.time_checks_cache
        self.state_path = state_path(cfg, paths)
        self.settings = load_settings(cfg, paths)
        self._packs_chosen = _packs_saved(self.state_path)
        kept = read_pack_dates(cfg, paths)
        self.pack_dates = kept.dates
        # Kept as written and saved back: see PackDatesState.
        self._unread_pack_dates = kept.unread
        self._extra_state = kept.extra
        # One save at a time, from the snapshot it starts with to the
        # finished write, so a switch saved while dates are saved (or the
        # other way round) never writes over the other.
        self._save_lock = threading.Lock()
        # Whether the file has been seen (read at start, or written since):
        # until then a folder that is not there yet is made on the first save.
        self._seen = self.state_path.exists() or kept.unreadable
        if kept.unreadable:
            logger.warning(
                "Time checks: %s could not be read; settings are not saved until it can",
                self.state_path,
            )
        self._joined = JoinCache()
        self._rotation = 0
        self._forecast_rng: random.Random | None = None
        self._forecast_only = False
        self._lock = threading.Lock()
        self._tts_lock = threading.Lock()
        # One pack or clip added or removed at a time.
        self._files_lock = threading.Lock()
        self._clashes_logged: set[str] = set()
        self.packs: dict[str, PackFolder] = {}
        # Uploads and removals a crash left half done, older than an hour.
        sweep_leftovers(self.root)
        self.rescan()
        self._hour = _hour_key(datetime.now(self.zone))
        self._aired_hour: HourKey | None = None
        self._pending = False
        self._warned_unavailable = False

    @property
    def folder(self) -> Path:
        """The recorded minute clips folder in use (see :func:`clip_sources`)."""
        return clip_sources(self.cfg, self.settings, self.paths)[0]

    def rescan(self) -> dict[str, PackFolder]:
        """Find the voice packs again, so a folder added or removed counts at once.

        Called before each status and each time check.  While no choice of
        packs has been saved, every pack found is ticked, as every listed
        pack always was.  A folder left out for a taken name is logged once.
        """
        with self._lock:
            skip = self.settings.recorded_folder
        found = found_packs(self.cfg, self.paths, skip=skip)
        for clash in found.clashes:
            if clash not in self._clashes_logged:
                self._clashes_logged.add(clash)
                logger.warning("Time checks: %s", clash)
        with self._lock:
            self.packs = found.packs
            if not self._packs_chosen:
                self.settings = self.settings.model_copy(
                    update={"enabled_packs": list(found.packs)}
                )
        return found.packs

    def save(self, settings: SettingsBody) -> None:
        """Write operator switches atomically; the speech command stays in TOML.

        Raises:
            TimeChecksUnreadable: The file could not be read; nothing was saved.
        """
        with self._save_lock:
            self._first_folder_locked()
            self._reread_locked()
            with self._lock:
                pack_dates = dict(self.pack_dates)
                unread = dict(self._unread_pack_dates)
            self._write(settings, pack_dates, unread)
            with self._lock:
                self.settings = settings
                if not settings.enabled or settings.rule == "off":
                    self._pending = False
        self.rescan()

    def check_choices(self, settings: SettingsBody) -> str | None:
        """Why *settings* names a folder or clip that is not there, or ``None``."""
        if settings.recorded_folder and settings.recorded_folder not in {
            folder.name for folder in subfolders(self.root)
        }:
            return f"No folder named {settings.recorded_folder} in the time checks folder"
        files = clip_files(self.root)
        for label, value in (("intro", settings.intro), ("outro", settings.outro)):
            if value and value not in files:
                return f"No {label} clip named {value} in the time checks folder"
        return None

    def _forget_pack(self, name: str) -> None:
        """Untick pack *name* and drop its play dates, for a pack removed or new.

        Raises:
            TimeChecksUnreadable: The file could not be read; nothing was changed.
        """
        with self._save_lock:
            self._first_folder_locked()
            settings = self._reread_locked()
            with self._lock:
                settings = settings or self.settings
                dates = dict(self.pack_dates)
                unread = dict(self._unread_pack_dates)
                chosen = self._packs_chosen
            ticked = chosen and name in settings.enabled_packs
            if not (ticked or name in dates or name in unread):
                return
            dates.pop(name, None)
            unread.pop(name, None)
            if ticked:
                settings = settings.model_copy(
                    update={
                        "enabled_packs": [item for item in settings.enabled_packs if item != name]
                    }
                )
            self._write(settings, dates, unread)
            with self._lock:
                self.settings = settings
                self.pack_dates = dates
                self._unread_pack_dates = unread

    def _forget_after_files_changed(self, name: str) -> None:
        """:meth:`_forget_pack` once the folder is in place or gone; a failure is logged.

        The folder change already happened, so it is not undone: a tick or
        dates left for a removed pack are ignored, like those of a folder
        that went away, and a new pack of that name forgets them again.
        """
        try:
            self._forget_pack(name)
        except OSError as exc:
            logger.warning(
                "Time checks: could not untick voice pack %s or drop its play dates: %s", name, exc
            )

    def add_pack(self, name: str, upload: Path, *, replace: bool, filename: str = "") -> str:
        """Unpack the zip *upload* as voice pack *name* in the time checks folder.

        A blank *name* is the zip's one folder, or *filename* without its
        extension; it is read under the same lock as the unpacking, so two
        uploads never read zips at once.  A new pack starts unticked once a choice of packs has been saved
        (ticked while none has, like every pack), with no play dates; a
        replaced one keeps its tick and dates.

        Returns:
            The pack's name.

        Raises:
            PackConflict: The name is taken and *replace* is false, or it is a
                pack that cannot be replaced from the page.
            PackError: The zip is refused.
            TimeChecksUnreadable: The settings file could not be read.
            OSError: The folder could not be written.
        """
        with self._files_lock:
            name = check_pack_name(name.strip() or pack_name_from(upload, filename))
            packs = self.rescan()
            taken = next((key for key in packs if key.casefold() == name.casefold()), None)
            if taken is not None:
                if not packs[taken].removable:
                    raise PackConflict(f"{_pack_place(taken, packs[taken])}; choose another name")
                if not replace:
                    raise PackExists(f"A voice pack named {taken} already exists")
                name = taken
            elif self.settings.recorded_folder.casefold() == name.casefold():
                raise PackConflict(
                    f"{name} is the recorded minute clips folder; choose another name"
                )
            install_pack(self.root, name, upload, replace=replace)
            if taken is None:
                self._forget_after_files_changed(name)
            self.rescan()
        return name

    def delete_pack(self, name: str) -> None:
        """Remove voice pack *name*, a folder in the time checks folder, untick it and drop its dates.

        Raises:
            PackNotFound: No pack by that name.
            PackConflict: It is listed in config.toml or kept in the shared folder.
            TimeChecksUnreadable: The settings file could not be read; nothing was removed.
            OSError: The folder could not be removed.
        """
        with self._files_lock:
            packs = self.rescan()
            found = packs.get(name)
            if found is None:
                raise PackNotFound(f"No voice pack named {name}")
            if not found.removable:
                raise PackConflict(f"{_pack_place(name, found)}, so it cannot be removed here")
            remove_pack(self.root, name)
            self._forget_after_files_changed(name)
            self.rescan()

    def add_clip(self, name: str, upload: Path, *, replace: bool) -> None:
        """Check the received clip *upload* and move it to *name*.

        A minute clip (``15_05.wav``) goes to the recorded minute clips
        folder in use, where it is played; any other clip, such as an intro,
        to the time checks folder, where the intro and outro are chosen from.

        Raises:
            PackConflict: *name* exists and *replace* is false.
            PackError: The name or the audio is refused.
        """
        with self._files_lock:
            folder = self.folder if MINUTE_CLIP.fullmatch(Path(name).stem) else self.root
            folder.mkdir(parents=True, exist_ok=True)
            install_clip(folder, name, upload, replace=replace)

    def save_pack_dates(self, name: str, dates: ClipDates) -> None:
        """Save voice pack *name*'s play dates; empty dates remove them.

        Raises:
            TimeChecksUnreadable: The file could not be read; nothing was saved.
        """
        with self._save_lock:
            self._first_folder_locked()
            settings = self._reread_locked()
            with self._lock:
                settings = settings or self.settings
                changed = dict(self.pack_dates)
                unread = dict(self._unread_pack_dates)
            # New dates replace any that could not be read.
            unread.pop(name, None)
            if dates.empty:
                changed.pop(name, None)
            else:
                changed[name] = dates
            self._write(settings, changed, unread)
            with self._lock:
                self.settings = settings
                self.pack_dates = changed
                self._unread_pack_dates = unread

    def _reread_locked(self) -> SettingsBody | None:
        """Read ``time_checks.json`` again before a save; its switches, if valid.

        Another process (a second AutoDJ, an editor) may have changed it, so
        a save starts from what is on disk: its pack dates and the keys this
        version does not know are kept.  A file that is not valid JSON is
        set aside as ``time_checks.json.bad-<id>`` first.  Caller holds
        ``_save_lock``.

        Raises:
            TimeChecksUnreadable: The file could not be read; nothing is saved.
        """
        try:
            state = _read_state(self.state_path)
        except OSError as exc:
            raise TimeChecksUnreadable(
                f"The time checks file could not be read just now ({exc}); nothing was saved"
            ) from exc
        except ValueError as exc:
            # Set aside, and saved again from what this process last read.
            self._keep_bad_copy(str(exc).splitlines()[0])
            return None
        kept = pack_dates_from(state, self.state_path)
        settings = None
        if state is not None and isinstance(state.get("settings"), dict):
            try:
                settings = SettingsBody.model_validate(
                    {**self.settings.model_dump(), **state["settings"]}
                )
            except ValueError:
                settings = None
        with self._lock:
            self.pack_dates = kept.dates
            self._unread_pack_dates = kept.unread
            self._extra_state = kept.extra
        return settings

    def _first_folder_locked(self) -> None:
        """Make the state folder before the first save ever.  Caller holds ``_save_lock``."""
        if not self._seen and not self.state_path.parent.exists():
            self.state_path.parent.mkdir(parents=True, exist_ok=True)

    def _keep_bad_copy(self, reason: str) -> None:
        """Set a damaged ``time_checks.json`` aside as ``time_checks.json.bad-<id>``."""
        bad = self.state_path.with_name(f"{self.state_path.name}.bad-{secrets.token_hex(4)}")
        try:
            os.replace(self.state_path, bad)
        except OSError:
            logger.exception("Could not set aside the damaged %s", self.state_path)
            raise
        logger.error(
            "Time checks file %s is not valid (%s); set it aside as %s",
            self.state_path,
            reason,
            bad,
        )

    def _write(
        self,
        settings: SettingsBody,
        pack_dates: dict[str, ClipDates],
        unread: dict[str, Any],
    ) -> None:
        """Write ``time_checks.json`` atomically: the switches, and pack dates when any.

        Keys this version does not know, and pack dates it could not read,
        are written back as they were.  Caller holds ``_save_lock``.
        """
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            **self._extra_state,
            "version": STATE_VERSION,
            "settings": settings.model_dump(),
        }
        written = {
            **unread,
            **{
                name: found.model_dump(mode="json", exclude_none=True)
                for name, found in pack_dates.items()
            },
        }
        if written:
            data["pack_dates"] = dict(sorted(written.items()))
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.state_path.parent,
            prefix=".time_checks_",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp = Path(handle.name)
            json.dump(data, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.state_path)
        self._seen = True
        # The file now names the ticked packs, so a pack found later starts
        # unticked; set under the lock rescan() reads it with.
        with self._lock:
            self._packs_chosen = True

    def pack_refusal(self, name: str, today: date) -> str | None:
        """Why voice pack *name* may not speak on the station's *today*, or ``None``."""
        found = self.pack_dates.get(name)
        return found.refusal(today) if found is not None else None

    def pack_status(self) -> list[dict[str, Any]]:
        """Each pack's name, switch, coverage, play dates and source, without its path.

        ``shared_with_stations`` marks a pack the page may remove that other
        stations use too: the default station's time checks folder is the
        library's shared one when ``clips_folder`` is not set.
        """
        today = datetime.now(self.zone).date()
        shared = bool(getattr(self.cfg, "stations", ())) and _folder_key(self.root) == _folder_key(
            shared_folder(self.cfg)
        )
        result = []
        for name, found in self.packs.items():
            pack = scan_pack(found.path)
            covered = coverage(pack, self.settings.hour_format)
            result.append(
                {
                    "name": name,
                    "enabled": name in self.settings.enabled_packs,
                    "minutes_covered": covered.minutes_covered,
                    "summary": covered.describe(pack),
                    "source": found.source,
                    "removable": found.removable,
                    "shared_with_stations": found.removable and shared,
                    **self.pack_dates.get(name, ClipDates()).view(today),
                }
            )
        return result

    def status(self) -> dict[str, Any]:
        """Expose useful clip coverage without exposing paths or command arguments.

        The packs are found again first, so one copied in shows at once.
        ``folders`` and ``clip_files`` are the names the recorded clips
        folder and the intro and outro may be chosen from; ``intro_config``
        and ``outro_config`` are the file names (only) of the config files'
        clips, used while none is chosen.
        """
        self.rescan()
        from autodj.speech import load_state

        generated_set = load_state(self.paths.speech).get("active_set")
        missing = missing_minutes(self.folder)
        tc = self.config
        return {
            **self.settings.model_dump(),
            "packs": self.pack_status(),
            "missing_minutes": len(missing),
            "missing_examples": missing[:10],
            "tts_available": bool(tc.tts_command),
            "generated_set": generated_set,
            "timezone": self.zone.key,
            "duck_db": self.cfg.playback.liners_duck_db,
            "folders": [folder.name for folder in subfolders(self.root)],
            "clip_files": clip_files(self.root),
            "intro_config": Path(tc.intro_clip).name if tc.intro_clip else None,
            "outro_config": Path(tc.outro_clip).name if tc.outro_clip else None,
            "pack_upload_max_mb": MAX_PACK_UPLOAD_BYTES // MIB,
            "clip_upload_max_mb": self.cfg.station.liner_upload_max_mib,
        }

    def tick(self, now: datetime, song_playing: bool) -> None:
        """Remember a crossed hour until the current song ends."""
        key = _hour_key(now.astimezone(self.zone))
        with self._lock:
            if key == self._hour:
                return
            self._hour = key
            self._pending = (
                song_playing and self.settings.enabled and self.settings.rule == "after_hour_song"
            )

    def due_audio(self) -> np.ndarray | None:
        """The after-the-hour check, once, when a song ends (liner worker thread).

        ``None`` when none is pending, or when a check (a scheduled one)
        already aired in this hour.
        """
        with self._lock:
            pending, self._pending = self._pending, False
            if not pending or self._aired_hour == _hour_key(datetime.now(self.zone)):
                return None
        rendered = self._aired(self.render())
        return None if rendered is None else rendered.audio

    def _aired(self, rendered: Rendered | None) -> Rendered | None:
        """Remember the hour of a check that is about to play."""
        if rendered is not None:
            with self._lock:
                self._aired_hour = _hour_key(datetime.now(self.zone))
        return rendered

    def _candidates(self, pack: str | None, today: date | None = None) -> list[str]:
        """Packs to try, first choice first: *pack* alone, or the enabled ones.

        Without *pack*, each call moves the rotation on one pack, or picks the
        first at random; the other enabled packs follow as fallbacks.  A pack
        outside its play dates on the station's *today* is passed over; one
        named for a test speaks whatever its dates.
        """
        if pack is not None:
            return [pack]
        with self._lock:
            enabled = [
                name
                for name in self.packs
                if name in self.settings.enabled_packs
                and (today is None or self.pack_refusal(name, today) is None)
            ]
            if not enabled:
                return []
            if self.settings.pack_order == "random":
                forecast_rng = getattr(self, "_forecast_rng", None)
                start = (
                    forecast_rng.randrange(len(enabled))
                    if forecast_rng is not None
                    else secrets.randbelow(len(enabled))
                )
            else:
                start = self._rotation % len(enabled)
                self._rotation += 1
        return enabled[start:] + enabled[:start]

    def snapshot(self) -> TimeChecks:
        """An isolated, read-only copy for forecasting time-check audio."""
        clone = copy.copy(self)
        clone.cfg = copy.deepcopy(self.cfg)
        clone.config = clone.cfg.time_checks
        clone.settings = self.settings.model_copy(deep=True)
        with self._lock:
            clone.packs = dict(self.packs)
            clone._rotation = self._rotation
            clone._hour = self._hour
            clone._aired_hour = self._aired_hour
            clone._pending = self._pending
            clone._warned_unavailable = self._warned_unavailable
        clone.pack_dates = dict(self.pack_dates)
        clone._packs_chosen = self._packs_chosen
        clone._unread_pack_dates = copy.deepcopy(self._unread_pack_dates)
        clone._extra_state = copy.deepcopy(self._extra_state)
        clone._clashes_logged = set(self._clashes_logged)
        clone._lock = threading.Lock()
        clone._save_lock = threading.Lock()
        clone._tts_lock = threading.Lock()
        clone._files_lock = threading.Lock()
        clone._joined = JoinCache()
        clone._forecast_rng = random.Random(0)
        clone._forecast_only = True
        return clone

    def forecast_seconds(
        self,
        now: datetime | None = None,
        *,
        scheduled: bool = False,
        on_audio: Callable[[np.ndarray], None] | None = None,
    ) -> float | None:
        """Duration of an automatic or scheduled check without changing live state or using TTS.

        ``None`` means no configured audio is available, or TTS would be
        needed to know the duration.
        """
        if not getattr(self, "_forecast_only", False):
            return self.snapshot().forecast_seconds(now, scheduled=scheduled, on_audio=on_audio)
        now = (now or datetime.now(self.zone)).astimezone(self.zone)
        if not self.settings.enabled:
            return 0.0
        if not scheduled:
            self.tick(now, True)
            with self._lock:
                pending = self._pending
                self._pending = False
                aired = self._aired_hour == _hour_key(now)
            if not pending or aired:
                return 0.0
        rendered = self.render(now)
        if rendered is not None:
            with self._lock:
                self._aired_hour = _hour_key(now)
        if rendered is not None and on_audio is not None:
            from autodj.stereo import copy_audio

            on_audio(copy_audio(rendered.audio))
        return None if rendered is None else len(rendered.audio) / SAMPLE_RATE

    def _pack_voice(self, name: str, local: datetime) -> np.ndarray | None:
        """Join *name*'s files for this minute, from the cache when unchanged.

        ``None`` when the pack lacks a piece or a piece cannot be decoded, so
        the next source speaks instead.
        """
        hour_format = self.settings.hour_format
        found = self.packs.get(name)
        if found is None:
            return None
        paths = pieces_for(scan_pack(found.path), local.hour, local.minute, hour_format)
        if paths is None:
            return None
        key = (name, local.hour, local.minute, hour_format)
        try:
            current = signature(paths)
            cached = self._joined.get(key, current)
            if cached is not None:
                return cached
            audio = join_voice([decode_liner_path(path) for path in paths])
        except (OSError, subprocess.TimeoutExpired, LinerDecodeError, ValueError) as exc:
            logger.warning(
                "Voice pack %s skipped at %02d:%02d: %s", name, local.hour, local.minute, exc
            )
            return None
        self._joined.put(key, current, audio)
        return audio

    def _voice(self, local: datetime, pack: str | None) -> tuple[np.ndarray, str] | None:
        """The spoken time from the first source that has it, and that source.

        Raises:
            OSError: A recorded or synthesized clip could not be read.
            subprocess.TimeoutExpired: Decoding it took too long.
            LinerDecodeError: It could not be decoded.
        """
        from autodj.speech import generated_clip

        try:
            selected, generated = generated_clip(self.paths.speech, local)
            if selected:
                if generated is None:
                    logger.warning(
                        "Generated time check audio is missing; skipping without synthesis"
                    )
                    return None
                return decode_liner_path(generated), "generated set"
        except (OSError, ValueError, LinerDecodeError):
            logger.warning("Generated time check could not be read; skipping without synthesis")
            return None
        self.rescan()
        for name in self._candidates(pack, local.date()):
            audio = self._pack_voice(name, local)
            if audio is not None:
                return audio, f"voice pack {name}"
        clip = clip_path(self.folder, local)
        if clip is not None:
            return decode_liner_path(clip), "recorded clip"
        spoken = self._synthesize(spoken_text(local, self.settings.hour_format))
        return None if spoken is None else (decode_liner_path(spoken), "text to speech")

    def _synthesize(self, text: str) -> Path | None:
        """Render *text* with the configured command, cached per command and text."""
        if self._forecast_only:
            return None
        command = self.config.tts_command
        if not command:
            return None
        key = hashlib.sha256(json.dumps([command, text]).encode("utf-8")).hexdigest()[:24]
        target = self.cache / f"{key}.wav"
        # One synthesis at a time, so a second request for the text hits the cache.
        with self._tts_lock:
            if target.is_file():
                return target
            self.cache.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.cache, suffix=".wav", delete=False) as handle:
                temp = Path(handle.name)
            try:
                returncode = run_command(command, text, temp, timeout=TTS_TIMEOUT_S)
                if returncode != 0:
                    logger.warning("Time check TTS command failed (exit %s)", returncode)
                    return None
                if not _has_audio(temp):
                    # A WAV header alone: some voices (a screen reader's SAPI
                    # bridge) speak elsewhere and write no samples.
                    logger.warning("Time check TTS command wrote no audio")
                    return None
                os.replace(temp, target)
                return target
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.warning("Time check TTS unavailable: %s", exc)
                return None
            finally:
                temp.unlink(missing_ok=True)

    def render(self, now: datetime | None = None, pack: str | None = None) -> Rendered | None:
        """Decode intro, local time and outro into one stereo 44.1 kHz clip.

        The time comes from *pack* when given (a test), else from the enabled
        packs in turn; then from the recorded clip, then from text to speech.
        """
        local = (now or datetime.now(self.zone)).astimezone(self.zone)
        try:
            voice = self._voice(local, pack)
            if voice is None:
                if not self._warned_unavailable:
                    logger.warning(
                        "Skipping time checks: no voice pack, recorded clip or working TTS command"
                    )
                    self._warned_unavailable = True
                return None
            self._warned_unavailable = False
            _folder, *clips = clip_sources(self.cfg, self.settings, self.paths)
            intro, outro = ([decode_liner_path(path)] if path else [] for path in clips)
            return Rendered(np.concatenate([*intro, voice[0], *outro], axis=0), voice[1])
        except (OSError, subprocess.TimeoutExpired, LinerDecodeError) as exc:
            logger.warning("Skipping time check: audio could not be decoded: %s", exc)
            return None

    def wav(self, pack: str | None = None) -> bytes | None:
        """Render a browser preview through the same decoder and clip order."""
        rendered = self.render(pack=pack)
        if rendered is None:
            return None
        buffer = io.BytesIO()
        sf.write(buffer, rendered.audio, SAMPLE_RATE, format="WAV", subtype="PCM_16")
        return buffer.getvalue()

    def prepare(self, bridge: Any, pack: str | None = None) -> tuple[Callable[[], None], str]:
        """Prepare a time check and return its playback callable (worker thread).

        Returns:
            The callable that plays it on the server mix bus, with the voice
            liner duck, and where the voice came from, such as
            ``voice pack Studio``.

        Raises:
            ValueError: Why nothing played.
        """
        worker = bridge.liner_scheduler
        if worker is None:
            raise ValueError("Time checks on the server need server audio or stream mode")
        if bridge.station is not None and bridge.station.state != "playing":
            raise ValueError("The stream must be playing to air a time check")
        rendered = self.render(pack=pack)
        if rendered is None:
            raise ValueError("No voice pack, recorded clip or working TTS command for this minute")

        def play() -> None:
            self._aired(rendered)
            worker.play_audio(rendered.audio)

        return play, rendered.source

    def fire(self, bridge: Any, pack: str | None = None) -> str:
        """Prepare and play now on the server mix bus (worker thread).

        Returns:
            Where the voice came from, such as ``voice pack Studio``.
        """
        play, source = self.prepare(bridge, pack)
        play()
        return source


def time_check_action(checks: TimeChecks, bridge: Any) -> Action:
    """The scheduler's fieldless Time check action for one station."""

    async def scheduled(_params: dict[str, str]) -> Callable[[], None]:
        if not checks.settings.enabled:
            raise ValueError("Enable time checks in Settings first")
        play, _source = await asyncio.to_thread(checks.prepare, bridge)
        return play

    return Action("time_check", "Time check", scheduled, interrupt="before")


def _pack_place(name: str, found: PackFolder) -> str:
    """Why pack *name* is not the page's to replace or remove, in words."""
    if found.source == "config":
        return f"Voice pack {name} is listed in config.toml"
    return f"Voice pack {name} is in the library's shared time checks folder"


def _known_pack(checks: TimeChecks, pack: str | None) -> str | None:
    if pack is not None and pack not in checks.packs:
        raise HTTPException(status_code=404, detail=f"No voice pack named {pack}")
    return pack


def _file_error(exc: PackError | OSError) -> JSONResponse:
    """The answer for a pack or clip that was refused or could not be written.

    A name the page may replace answers ``409`` with ``"field": "replace"``,
    so the page can ask before it sends the upload again with ``replace``.
    """
    content: dict[str, str] = {"detail": str(exc)}
    if isinstance(exc, TimeChecksUnreadable):
        return JSONResponse(status_code=503, content=content)
    if isinstance(exc, OSError):
        logger.warning("Time checks: a pack or clip could not be written: %s", exc)
        content["detail"] = (
            "The time checks folder could not be changed just now (a file in it may be "
            "in use); nothing was changed. Try again in a moment"
        )
        return JSONResponse(status_code=503, content=content)
    status = 400
    if isinstance(exc, PackExists):
        status, content["field"] = 409, "replace"
    elif isinstance(exc, PackConflict):
        status = 409
    elif isinstance(exc, PackTooLarge):
        status = 413
    elif isinstance(exc, PackNotFound):
        status = 404
    return JSONResponse(status_code=status, content=content)


def install_time_checks(router: StationRouter) -> None:
    """Register the paired time check routes; each acts on the station its URL names."""

    @router.get("/time-checks")
    async def get_settings(station: SavedStationDep) -> dict[str, Any]:
        return await asyncio.to_thread(station.time_checks.status)

    @router.put("/time-checks")
    async def put_settings(station: SavedStationDep, body: SettingsBody) -> dict[str, Any]:
        checks = station.time_checks
        # The clip choices a request leaves out keep their saved values.
        body = SettingsBody.model_validate(
            {**checks.settings.model_dump(), **body.model_dump(exclude_unset=True)}
        )
        for name in body.enabled_packs:
            _known_pack(checks, name)
        refusal = await asyncio.to_thread(checks.check_choices, body)
        if refusal is not None:
            raise HTTPException(status_code=400, detail=refusal)
        try:
            await asyncio.to_thread(checks.save, body)
        except TimeChecksUnreadable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return await asyncio.to_thread(checks.status)

    @router.post("/time-checks/packs", response_model=None)
    async def add_pack(
        station: SavedStationDep,
        file: UploadFile = File(...),
        name: str = "",
        replace: bool = False,
    ) -> dict[str, Any] | JSONResponse:
        """Add a voice pack from a zip of its audio files (see :mod:`autodj.time_check_files`)."""
        checks = station.time_checks
        upload = hidden_name(checks.root, "pack-upload", ".zip")
        try:
            await receive_upload(file, upload, MAX_PACK_UPLOAD_BYTES)
            added = await asyncio.to_thread(
                checks.add_pack, name, upload, replace=replace, filename=file.filename or ""
            )
        except (PackError, OSError) as exc:
            return _file_error(exc)
        finally:
            await asyncio.to_thread(upload.unlink, missing_ok=True)
        return {**await asyncio.to_thread(checks.status), "added": added}

    @router.delete("/time-checks/packs/{name}", response_model=None)
    async def delete_pack(station: SavedStationDep, name: str) -> dict[str, Any] | JSONResponse:
        """Remove a voice pack that is a folder in the station's time checks folder."""
        checks = station.time_checks
        from autodj.profile_settings import profile_settings

        def remove() -> None:
            with profile_settings(station).changing_reference("pack", name, None):
                checks.delete_pack(name)

        try:
            await asyncio.to_thread(remove)
        except (PackError, OSError) as exc:
            return _file_error(exc)
        return {**await asyncio.to_thread(checks.status), "removed": name}

    @router.post("/time-checks/clips", response_model=None)
    async def add_clip(
        station: SavedStationDep, file: UploadFile = File(...), replace: bool = False
    ) -> dict[str, Any] | JSONResponse:
        """Upload one clip, such as an intro, an outro or a minute clip, to the time checks folder."""
        checks = station.time_checks
        filename = file.filename or ""
        try:
            check_clip_name(filename)
        except PackError as exc:
            return _file_error(exc)
        upload = hidden_name(checks.root, "clip-upload", Path(filename).suffix.lower())
        try:
            await receive_upload(
                file, upload, (station.cfg.station.liner_upload_max_mib * 1024 * 1024)
            )
            await asyncio.to_thread(checks.add_clip, filename, upload, replace=replace)
        except (PackError, OSError) as exc:
            return _file_error(exc)
        finally:
            await asyncio.to_thread(upload.unlink, missing_ok=True)
        return {**await asyncio.to_thread(checks.status), "uploaded": filename}

    @router.put("/time-checks/packs/{name}/dates", response_model=None)
    async def put_pack_dates(
        station: SavedStationDep, name: str, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any] | JSONResponse:
        """Set a voice pack's play dates: "not before" and "do not play after"."""
        checks = station.time_checks
        _known_pack(checks, name)
        try:
            dates = parse_dates(body)
        except ClipDatesError as exc:
            return JSONResponse(
                status_code=400, content={"detail": exc.message, "field": exc.field}
            )
        try:
            await asyncio.to_thread(checks.save_pack_dates, name, dates)
        except TimeChecksUnreadable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return await asyncio.to_thread(checks.status)

    @router.post("/time-checks/test")
    async def test_now(station: StationDep, body: PlayNowBody | None = None) -> dict[str, str]:
        checks = station.time_checks
        pack = _known_pack(checks, body.pack if body else None)
        try:
            source = await asyncio.to_thread(checks.fire, station.bridge, pack)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        if pack is not None and source != f"voice pack {pack}":
            return {"message": f"Voice pack {pack} cannot say this minute; played the {source}"}
        return {"message": f"Time check played from the {source}"}

    @router.get("/time-checks/preview")
    async def preview(station: SavedStationDep, pack: str | None = None) -> Response:
        checks = station.time_checks
        rendered = await asyncio.to_thread(checks.wav, _known_pack(checks, pack))
        if rendered is None:
            raise HTTPException(status_code=409, detail="No time check audio is available")
        return Response(rendered, media_type="audio/wav")
