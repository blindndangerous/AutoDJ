"""Spoken station time, rendered as one voice-liner clip.

A time check is the optional intro clip, the time and the optional outro
clip, decoded into one stereo clip and played through the voice liner
scheduler, so it gets the liner duck depth and, after a song change, the
liner talk-up timing.  The time comes from the first source that has it: an
enabled voice pack (hour and minute files joined by :mod:`autodj.voice_packs`),
a recorded clip named for the station-local minute (``HH_MM``), or an
operator-configured text to speech command.  Paths and the command come only
from the config files; a paired browser can change the switches in
:class:`SettingsBody` and name a configured pack, never a path.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
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
from fastapi import Body, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, field_validator

from autodj.clip_dates import ClipDates, ClipDatesError, parse_dates
from autodj.fsutil import confirm_missing
from autodj.liner_scheduler import LinerDecodeError, decode_liner_path
from autodj.scheduler import Action
from autodj.station_routes import StationDep, StationRouter
from autodj.stations import StationPaths
from autodj.stereo import SAMPLE_RATE
from autodj.voice_packs import JoinCache, coverage, join_voice, pieces_for, scan_pack, signature

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig

logger = logging.getLogger(__name__)
EXTENSIONS = (".wav", ".mp3", ".flac", ".ogg")
STATE_VERSION = 1
TTS_TIMEOUT_S = 20


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


def pack_folders(cfg: AutoDJConfig) -> dict[str, Path]:
    """Configured voice packs by name (the folder name), in config order."""
    return {Path(path).name: Path(path) for path in cfg.time_checks.voice_packs}


class SettingsBody(BaseModel):
    """Only non-secret switches are writable from a paired browser.

    ``enabled_packs`` holds pack names; a name that is not configured is
    refused by the route and ignored when it comes from a saved file.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    hour_format: StrictStr
    rule: StrictStr
    enabled_packs: list[StrictStr]
    pack_order: StrictStr

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
    ``enabled_packs``, every configured pack is enabled.

    Raises:
        ValueError: The saved file has another version or bad values.
    """
    tc = cfg.time_checks
    defaults = {
        "enabled": tc.enabled,
        "hour_format": tc.hour_format,
        "rule": tc.rule,
        "enabled_packs": list(pack_folders(cfg)),
        "pack_order": tc.pack_order,
    }
    path = state_path(cfg, paths)
    if not path.is_file():
        return SettingsBody.model_validate(defaults)
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != STATE_VERSION:
        raise ValueError(f"Unsupported time checks state version in {path}")
    return SettingsBody.model_validate({**defaults, **state["settings"]})


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


def load_pack_dates(cfg: AutoDJConfig, paths: StationPaths | None = None) -> dict[str, ClipDates]:
    """Each voice pack's play dates that could be read (see :func:`read_pack_dates`)."""
    return read_pack_dates(cfg, paths).dates


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
        self.zone = ZoneInfo(timezone)
        self.folder = clips_folder(cfg, paths)
        self.cache = paths.time_checks_cache
        self.state_path = state_path(cfg, paths)
        self.settings = load_settings(cfg, paths)
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
        self.packs = pack_folders(cfg)
        self._joined = JoinCache()
        self._rotation = 0
        self._lock = threading.Lock()
        self._tts_lock = threading.Lock()
        self._hour = _hour_key(datetime.now(self.zone))
        self._aired_hour: HourKey | None = None
        self._pending = False
        self._warned_unavailable = False

    def save(self, settings: SettingsBody) -> None:
        """Write operator switches atomically; clips and commands remain in TOML.

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

    def pack_refusal(self, name: str, today: date) -> str | None:
        """Why voice pack *name* may not speak on the station's *today*, or ``None``."""
        found = self.pack_dates.get(name)
        return found.refusal(today) if found is not None else None

    def pack_status(self) -> list[dict[str, Any]]:
        """Each configured pack's name, switch, coverage and play dates, without its path."""
        today = datetime.now(self.zone).date()
        result = []
        for name, folder in self.packs.items():
            pack = scan_pack(folder)
            covered = coverage(pack, self.settings.hour_format)
            result.append(
                {
                    "name": name,
                    "enabled": name in self.settings.enabled_packs,
                    "minutes_covered": covered.minutes_covered,
                    "summary": covered.describe(pack),
                    **self.pack_dates.get(name, ClipDates()).view(today),
                }
            )
        return result

    def status(self) -> dict[str, Any]:
        """Expose useful clip coverage without exposing paths or command arguments."""
        missing = missing_minutes(self.folder)
        return {
            **self.settings.model_dump(),
            "packs": self.pack_status(),
            "missing_minutes": len(missing),
            "missing_examples": missing[:10],
            "tts_available": bool(self.config.tts_command),
            "timezone": self.zone.key,
            "duck_db": self.cfg.playback.liners_duck_db,
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
                start = secrets.randbelow(len(enabled))
            else:
                start = self._rotation % len(enabled)
                self._rotation += 1
        return enabled[start:] + enabled[:start]

    def _pack_voice(self, name: str, local: datetime) -> np.ndarray | None:
        """Join *name*'s files for this minute, from the cache when unchanged.

        ``None`` when the pack lacks a piece or a piece cannot be decoded, so
        the next source speaks instead.
        """
        hour_format = self.settings.hour_format
        paths = pieces_for(scan_pack(self.packs[name]), local.hour, local.minute, hour_format)
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
                argv = [
                    part.replace("{out}", str(temp)).replace("{text}", text) for part in command
                ]
                result = subprocess.run(
                    argv,
                    input=text,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=TTS_TIMEOUT_S,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )  # nosec B603
                if result.returncode != 0:
                    logger.warning("Time check TTS command failed (exit %s)", result.returncode)
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
            intro, outro = (
                [decode_liner_path(Path(path))] if path else []
                for path in (self.config.intro_clip, self.config.outro_clip)
            )
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


def _known_pack(checks: TimeChecks, pack: str | None) -> str | None:
    if pack is not None and pack not in checks.packs:
        raise HTTPException(status_code=404, detail=f"No voice pack named {pack}")
    return pack


def install_time_checks(router: StationRouter) -> None:
    """Register the paired time check routes; each acts on the station its URL names."""

    @router.get("/time-checks")
    async def get_settings(station: StationDep) -> dict[str, Any]:
        return await asyncio.to_thread(station.time_checks.status)

    @router.put("/time-checks")
    async def put_settings(station: StationDep, body: SettingsBody) -> dict[str, Any]:
        checks = station.time_checks
        for name in body.enabled_packs:
            _known_pack(checks, name)
        try:
            await asyncio.to_thread(checks.save, body)
        except TimeChecksUnreadable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return await asyncio.to_thread(checks.status)

    @router.put("/time-checks/packs/{name}/dates", response_model=None)
    async def put_pack_dates(
        station: StationDep, name: str, body: dict[str, Any] = Body(...)
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
    async def preview(station: StationDep, pack: str | None = None) -> Response:
        checks = station.time_checks
        rendered = await asyncio.to_thread(checks.wav, _known_pack(checks, pack))
        if rendered is None:
            raise HTTPException(status_code=409, detail="No time check audio is available")
        return Response(rendered, media_type="audio/wav")
