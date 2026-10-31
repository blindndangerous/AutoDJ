"""Write the listener MP3 stream to dated files without blocking its encoder."""

from __future__ import annotations

import errno
import json
import logging
import queue
import re
import shutil
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO
from zoneinfo import ZoneInfo

from autodj.config import RecordingConfig
from autodj.fsutil import atomic_write, set_aside
from autodj.station_log import station_thread

logger = logging.getLogger(__name__)

_NAME = re.compile(r"^[A-Za-z0-9_-]+-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.mp3$")
_QUEUE_CHUNKS = 512
_RETRY_SECONDS = 5


def load_saved_state(
    state_path: Path, folder: Path, fallback: RecordingConfig
) -> tuple[bool, RecordingConfig]:
    """Load the durable recorder choice and settings shared by live/offline editors."""
    if not state_path.exists():
        return False, fallback
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if (
            not isinstance(state, dict)
            or set(state) != {"version", "running", "split", "retention_days", "free_space_mb"}
            or state["version"] != 1
            or not isinstance(state["running"], bool)
        ):
            raise ValueError("unsupported recording state")
        config = RecordingConfig(
            folder, state["split"], state["retention_days"], state["free_space_mb"]
        )
    except (OSError, TypeError, ValueError) as exc:
        logger.error(
            "Cannot load recording state from %s: %s; recording starts stopped",
            state_path,
            exc,
        )
        set_aside(state_path, "recording state file", str(exc), naming="timestamp")
        return False, fallback
    return state["running"], config


def save_state(state_path: Path, running: bool, config: RecordingConfig) -> None:
    """Persist validated recorder state for the live recorder and saved editor."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        state_path,
        json.dumps(
            {
                "version": 1,
                "running": running,
                "split": config.split,
                "retention_days": config.retention_days,
                "free_space_mb": config.free_space_mb,
            }
        )
        + "\n",
    )


def _hour(wall: datetime) -> tuple[int, int, int, int, int]:
    """The clock hour of *wall*; ``fold`` tells a repeated DST hour apart."""
    return (wall.year, wall.month, wall.day, wall.hour, wall.fold)


class _LowSpaceError(OSError):
    """The recording volume has less free space than the configured floor."""


def _disk_error(exc: OSError) -> str:
    """Describe a storage failure without exposing the configured folder."""
    if isinstance(exc, _LowSpaceError):
        return str(exc)
    if exc.errno == errno.ENOSPC:
        return "Recording volume is full"
    return f"Recording storage error: {exc.strerror or 'unavailable'}"


class Recorder:
    """A stream sink whose bounded queue is drained by one file writer thread."""

    def __init__(
        self,
        config: RecordingConfig,
        state_path: Path,
        station_name: Callable[[], str],
        timezone: str,
    ) -> None:
        self.folder = config.folder
        self.state_path = state_path
        self.station_name = station_name
        self.zone = ZoneInfo(timezone)
        self.split_mode = config.split
        self.retention_days = config.retention_days
        self.free_space_mb = config.free_space_mb
        self._lock = threading.Lock()
        self._control_lock = threading.Lock()
        self._queue: queue.Queue[tuple[int, bytes]] = queue.Queue(maxsize=_QUEUE_CHUNKS)
        self._desired = False
        self._generation = 0
        self._file_name: str | None = None
        self._size = 0
        self._error: str | None = None
        self._closed = False
        self._overflow = False
        self._load()
        self._thread = station_thread(target=self._run, name="autodj-recorder", daemon=True)
        self._thread.start()

    def _load(self) -> None:
        """Restore the saved settings and running choice.

        A damaged file must not keep the station off the air: it is moved
        aside and recording starts stopped with the config.toml settings.
        """
        running, config = load_saved_state(
            self.state_path,
            self.folder,
            RecordingConfig(self.folder, self.split_mode, self.retention_days, self.free_space_mb),
        )
        self.split_mode = config.split
        self.retention_days = config.retention_days
        self.free_space_mb = config.free_space_mb
        self._desired = running

    def _save(self, running: bool, split: str, retention_days: int, free_space_mb: int) -> None:
        save_state(
            self.state_path,
            running,
            RecordingConfig(self.folder, split, retention_days, free_space_mb),
        )

    @property
    def wanted(self) -> bool:
        with self._lock:
            return self._desired

    def status(self) -> dict[str, object]:
        """Return a browser-safe snapshot, without the configured folder path."""
        with self._lock:
            return {
                "running": self._desired,
                "file": self._file_name,
                "bytes": self._size,
                "error": self._error,
                "split": self.split_mode,
                "retention_days": self.retention_days,
                "free_space_mb": self.free_space_mb,
            }

    def start(self) -> dict[str, object]:
        """Start, or in show mode split, the recording; then save the choice.

        The change takes effect even when saving it raises OSError, so a full
        index volume cannot keep the recorder from obeying.
        """
        with self._control_lock:
            with self._lock:
                split_now = not self._desired or self.split_mode == "show"
                self._desired = True
                if split_now:
                    self._generation += 1
                self._error = None
                split, days, free = self.split_mode, self.retention_days, self.free_space_mb
            self._save(True, split, days, free)
        return self.status()

    def stop(self) -> dict[str, object]:
        """Stop recording, then save the choice (see :meth:`start`)."""
        with self._control_lock:
            with self._lock:
                self._desired = False
                self._generation += 1
                self._error = None
                self._file_name = None
                self._size = 0
                split, days, free = self.split_mode, self.retention_days, self.free_space_mb
            self._save(False, split, days, free)
        return self.status()

    def split(self) -> dict[str, object]:
        """Close the current show file and open the next one on incoming audio."""
        with self._lock:
            if not self._desired:
                raise ValueError("Recording is stopped")
            self._generation += 1
        return self.status()

    def show_changed(self) -> None:
        """Start a new file for a new show (a profile change) in show mode."""
        with self._lock:
            if self._desired and self.split_mode == "show":
                self._generation += 1

    def configure(self, split: str, retention_days: int, free_space_mb: int) -> dict[str, object]:
        """Save runtime settings, keeping the folder from config.toml."""
        checked = RecordingConfig(self.folder, split, retention_days, free_space_mb)
        with self._control_lock:
            with self._lock:
                desired = self._desired
            self._save(desired, checked.split, checked.retention_days, checked.free_space_mb)
            with self._lock:
                if self.split_mode != checked.split:
                    self._generation += 1
                self.split_mode = checked.split
                self.retention_days = checked.retention_days
                self.free_space_mb = checked.free_space_mb
        return self.status()

    def feed(self, data: bytes, _title: str) -> None:
        """Take encoded audio on the encoder thread; never wait for disk I/O."""
        with self._lock:
            if not self._desired or self._closed:
                return
            generation = self._generation
        try:
            self._queue.put_nowait((generation, data))
        except queue.Full:
            with self._lock:
                self._overflow = True

    def encoder_changed(self) -> None:
        """Keep a new encoder's stream in a new file."""
        with self._lock:
            self._generation += 1

    def close(self) -> None:
        """Write what is queued, close the file and stop the writer.

        The saved running choice is kept, so a restart resumes recording.
        """
        with self._lock:
            self._closed = True
        self._thread.join(timeout=5)

    def _failure(self, message: str) -> None:
        with self._lock:
            first = self._error is None
            self._error = message
            self._file_name = None
            self._size = 0
        if first:
            logger.error("Recording paused: %s", message)

    def _open(self) -> tuple[BinaryIO, str, datetime]:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._check_space()
        station = re.sub(r"[^A-Za-z0-9_-]+", "-", self.station_name())[:80].strip("-_")
        station = station or "AutoDJ"
        now = datetime.now(self.zone)
        for offset in range(60):
            name = f"{station}-{(now + timedelta(seconds=offset)):%Y-%m-%d_%H-%M-%S}.mp3"
            try:
                return (self.folder / name).open("xb", buffering=0), name, now
            except FileExistsError:
                continue
        raise OSError("Could not create a unique recording name")

    def _check_space(self) -> None:
        with self._lock:
            floor = self.free_space_mb
        if shutil.disk_usage(self.folder).free < floor * 1024 * 1024:
            raise _LowSpaceError(f"Less than {floor} MB free on the recording volume")

    def _prune(self) -> None:
        with self._lock:
            days = self.retention_days
        if not days or not self.folder.is_dir():
            return
        cutoff = time.time() - days * 86400
        for path in self.folder.iterdir():
            try:
                if path.is_file() and _NAME.fullmatch(path.name) and path.stat().st_mtime < cutoff:
                    path.unlink()
                    logger.info("Deleted expired recording %s", path)
            except OSError:
                logger.warning("Could not check or delete recording %s", path, exc_info=True)

    def _run(self) -> None:
        handle: BinaryIO | None = None
        opened_generation = -1
        opened_hour: tuple[int, int, int, int, int] | None = None
        retry_at = 0.0
        prune_at = 0.0
        space_at = 0.0
        while True:
            with self._lock:
                closed = self._closed
                desired = self._desired
                generation = self._generation
                overflow = self._overflow
                self._overflow = False
                split_mode = self.split_mode
            if closed:
                handle = self._drain(handle, opened_generation)
                break
            now = time.monotonic()
            if now >= prune_at:
                try:
                    self._prune()
                except OSError:
                    logger.warning("Could not prune recordings", exc_info=True)
                prune_at = now + 3600
            if overflow:
                self._failure("Recording writer fell behind; audio was dropped")
                retry_at = now + _RETRY_SECONDS
            if handle is not None and now >= space_at:
                try:
                    self._check_space()
                except OSError as exc:
                    self._failure(_disk_error(exc))
                    retry_at = now + _RETRY_SECONDS
                    overflow = True
                space_at = now + _RETRY_SECONDS
            hour = _hour(datetime.now(self.zone))
            if handle is not None and (
                not desired
                or overflow
                or opened_generation != generation
                or (split_mode == "hour" and opened_hour != hour)
            ):
                with suppress(OSError):
                    handle.close()
                handle = None
                with self._lock:
                    self._file_name = None
                    self._size = 0
            try:
                item_generation, data = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            with self._lock:
                desired = self._desired
                generation = self._generation
            if handle is not None and opened_generation != generation:
                with suppress(OSError):
                    handle.close()
                handle = None
            if not desired or overflow or item_generation != generation or now < retry_at:
                continue
            if handle is None:
                try:
                    handle, name, opened_at = self._open()
                except OSError as exc:
                    self._failure(_disk_error(exc))
                    retry_at = now + _RETRY_SECONDS
                    continue
                opened_generation = generation
                opened_hour = _hour(opened_at)
                with self._lock:
                    self._file_name = name
                    self._size = 0
                    self._error = None
                logger.info("Recording to %s", name)
            try:
                handle.write(data)
                with self._lock:
                    self._size += len(data)
            except OSError as exc:
                self._failure(_disk_error(exc))
                with suppress(OSError):
                    handle.close()
                handle = None
                retry_at = now + _RETRY_SECONDS
        if handle is not None:
            with suppress(OSError):
                handle.close()

    def _drain(self, handle: BinaryIO | None, generation: int) -> BinaryIO | None:
        """Write the open file's queued audio before shutdown."""
        while handle is not None:
            try:
                item_generation, data = self._queue.get_nowait()
            except queue.Empty:
                break
            if item_generation != generation:
                continue
            try:
                handle.write(data)
            except OSError as exc:
                self._failure(_disk_error(exc))
                with suppress(OSError):
                    handle.close()
                handle = None
        return handle
