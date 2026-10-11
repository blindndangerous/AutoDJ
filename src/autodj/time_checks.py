"""Spoken station time, rendered as one voice-liner clip.

A time check is the optional intro clip, the time and the optional outro
clip, decoded into one stereo clip and played through the voice liner
scheduler, so it gets the liner duck depth and, after a song change, the
liner talk-up timing.  The time comes from a recorded clip named for the
station-local minute (``HH_MM``) or from an operator-configured text to
speech command.  Paths and the command come only from the config files;
a paired browser can change the three switches in :class:`SettingsBody`.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import subprocess  # nosec B404 -- operator-configured argv, never a shell
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, field_validator

from autodj.liner_scheduler import LinerDecodeError, decode_liner_path
from autodj.scheduler import Action, Scheduler
from autodj.stereo import SAMPLE_RATE

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


def clips_folder(cfg: AutoDJConfig) -> Path:
    """The recorded clips folder: ``clips_folder``, or ``time_checks`` in the index."""
    return Path(cfg.time_checks.clips_folder or Path(cfg.index.active_dir) / "time_checks")


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


class SettingsBody(BaseModel):
    """Only non-secret switches are writable from a paired browser."""

    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    hour_format: StrictStr
    rule: StrictStr

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


def state_path(cfg: AutoDJConfig) -> Path:
    """Where the switches saved from Settings live."""
    return Path(cfg.index.active_dir) / "time_checks.json"


def load_settings(cfg: AutoDJConfig) -> SettingsBody:
    """The switches saved from Settings, or the config file's when none were saved.

    Raises:
        ValueError: The saved file has another version or bad values.
    """
    path = state_path(cfg)
    if not path.is_file():
        tc = cfg.time_checks
        return SettingsBody(enabled=tc.enabled, hour_format=tc.hour_format, rule=tc.rule)
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != STATE_VERSION:
        raise ValueError(f"Unsupported time checks state version in {path}")
    return SettingsBody.model_validate(state["settings"])


def _has_audio(path: Path) -> bool:
    """Whether *path* is a sound file with at least one frame."""
    try:
        return sf.info(str(path)).frames > 0
    except (RuntimeError, OSError):  # soundfile.LibsndfileError is a RuntimeError
        return False


HourKey = tuple[int, int, int, int, int]


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

    def __init__(self, cfg: AutoDJConfig, timezone: str) -> None:
        self.cfg = cfg
        self.config = cfg.time_checks
        self.zone = ZoneInfo(timezone)
        self.folder = clips_folder(cfg)
        self.cache = Path(cfg.index.active_dir) / "time_checks_cache"
        self.state_path = state_path(cfg)
        self.settings = load_settings(cfg)
        self._lock = threading.Lock()
        self._tts_lock = threading.Lock()
        self._hour = _hour_key(datetime.now(self.zone))
        self._aired_hour: HourKey | None = None
        self._pending = False
        self._warned_unavailable = False

    def save(self, settings: SettingsBody) -> None:
        """Write operator switches atomically; clips and commands remain in TOML."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": STATE_VERSION, "settings": settings.model_dump()}
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
        with self._lock:
            self.settings = settings
            if not settings.enabled or settings.rule == "off":
                self._pending = False

    def status(self) -> dict[str, Any]:
        """Expose useful clip coverage without exposing paths or command arguments."""
        missing = missing_minutes(self.folder)
        return {
            **self.settings.model_dump(),
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
        return self._aired(self.render())

    def _aired(self, audio: np.ndarray | None) -> np.ndarray | None:
        """Remember the hour of a check that is about to play."""
        if audio is not None:
            with self._lock:
                self._aired_hour = _hour_key(datetime.now(self.zone))
        return audio

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

    def render(self, now: datetime | None = None) -> np.ndarray | None:
        """Decode intro, local time and outro into one stereo 44.1 kHz clip."""
        local = (now or datetime.now(self.zone)).astimezone(self.zone)
        source = clip_path(self.folder, local) or self._synthesize(
            spoken_text(local, self.settings.hour_format)
        )
        if source is None:
            if not self._warned_unavailable:
                logger.warning("Skipping time checks: no recorded clip or working TTS command")
                self._warned_unavailable = True
            return None
        self._warned_unavailable = False
        paths = [
            Path(path) for path in (self.config.intro_clip, source, self.config.outro_clip) if path
        ]
        try:
            return np.concatenate([decode_liner_path(path) for path in paths], axis=0)
        except (OSError, subprocess.TimeoutExpired, LinerDecodeError) as exc:
            logger.warning("Skipping time check: audio could not be decoded: %s", exc)
            return None

    def wav(self) -> bytes | None:
        """Render a browser preview through the same decoder and clip order."""
        audio = self.render()
        if audio is None:
            return None
        buffer = io.BytesIO()
        sf.write(buffer, audio, SAMPLE_RATE, format="WAV", subtype="PCM_16")
        return buffer.getvalue()

    def prepare(self, bridge: Any) -> Callable[[], None]:
        """Prepare a time check and return its playback callable (worker thread).

        Raises:
            ValueError: Why nothing played.
        """
        worker = bridge.liner_scheduler
        if worker is None:
            raise ValueError("Time checks on the server need server audio or stream mode")
        if bridge.station is not None and bridge.station.state != "playing":
            raise ValueError("The stream must be playing to air a time check")
        audio = self.render()
        if audio is None:
            raise ValueError("No recorded clip or working TTS command for this minute")

        def play() -> None:
            self._aired(audio)
            worker.play_audio(audio)

        return play

    def fire(self, bridge: Any) -> None:
        """Prepare and play now on the server mix bus (worker thread)."""
        self.prepare(bridge)()


def install_time_checks(
    app: FastAPI, bridge: Any, scheduler: Scheduler, timezone: str
) -> TimeChecks:
    """Register paired routes and the scheduler's fieldless Time check action."""
    checks = TimeChecks(bridge.player._cfg, timezone)

    async def scheduled(_params: dict[str, str]) -> Callable[[], None]:
        if not checks.settings.enabled:
            raise ValueError("Enable time checks in Settings first")
        return await asyncio.to_thread(checks.prepare, bridge)

    scheduler.register(Action("time_check", "Time check", scheduled, interrupt="before"))

    @app.get("/api/time-checks")
    async def get_settings() -> dict[str, Any]:
        return await asyncio.to_thread(checks.status)

    @app.put("/api/time-checks")
    async def put_settings(body: SettingsBody) -> dict[str, Any]:
        await asyncio.to_thread(checks.save, body)
        return await asyncio.to_thread(checks.status)

    @app.post("/api/time-checks/test")
    async def test_now() -> dict[str, str]:
        try:
            await asyncio.to_thread(checks.fire, bridge)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {"message": "Time check played"}

    @app.get("/api/time-checks/preview")
    async def preview() -> Response:
        rendered = await asyncio.to_thread(checks.wav)
        if rendered is None:
            raise HTTPException(status_code=409, detail="No time check audio is available")
        return Response(rendered, media_type="audio/wav")

    return checks
