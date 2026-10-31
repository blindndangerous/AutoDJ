"""Live DJ input: DJ accounts, the source decoder and the talkover buffer.

A live DJ connects an Icecast source client (BUTT, Mixxx) to the
station's source mount (:mod:`autodj.live_routes`).  Their audio is
decoded by an FFmpeg process into a short PCM buffer that the mix bus
reads one block at a time; the bus never waits for the network or the
decoder (see ``MixBus._mix_live``).  A paired browser can also talk over
the station: its microphone PCM goes into a second, shorter buffer.

Every buffer here drops its oldest audio when full, so a client sending
faster than real time adds no latency beyond the buffer's length and no
memory beyond its cap.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import logging
import math
import queue
import secrets
import shutil
import subprocess  # nosec B404 -- fixed FFmpeg arguments, no shell
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from autodj.fsutil import atomic_write
from autodj.station_log import CURRENT_STATION, track_station_thread
from autodj.stereo import SAMPLE_RATE

logger = logging.getLogger(__name__)

#: The source content types accepted, and the FFmpeg demuxer each is read
#: with.  Naming the demuxer keeps FFmpeg from probing the input as
#: anything else (a playlist, an image) and starts decoding at once.
SOURCE_FORMATS: dict[str, str] = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/aac": "aac",
    "audio/aacp": "aac",
    "audio/x-aac": "aac",
    "audio/ogg": "ogg",
    "application/ogg": "ogg",
}
#: Most a source may send in one connection: about 14 hours at 320 kbit/s.
MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
#: Highest sustained input rate, averaged over five seconds, plus a burst.
MAX_SOURCE_BITRATE = 512_000
SOURCE_BURST_BYTES = 64 * 1024
#: A source that sends no bytes for this long is disconnected (slow-loris).
SOURCE_IDLE_SECONDS = 10.0
#: A connected source sending only silence waits off the air this long.
SOURCE_STANDBY_SECONDS = 30 * 60.0
#: Silence on the air this long hands the station back to automation.
SOURCE_SILENCE_SECONDS = 15.0
#: Decoded PCM at or below this level (about -60 dBFS) counts as silence.
SILENCE_LEVEL = 0.001
#: Decoded source PCM kept for the bus; older audio is dropped beyond it.
SOURCE_BUFFER_SECONDS = 2.0
#: Microphone PCM kept for the bus: short, so talkover stays in sync.
MIC_BUFFER_SECONDS = 0.5
#: A microphone socket that sends nothing for this long is closed.
MIC_IDLE_SECONDS = 2.0
#: Sample rates a browser may declare for its microphone PCM.
MIC_RATES = range(8000, 192001)
#: Largest microphone message: about 0.17 s of 48 kHz Int16 mono.
MIC_MAX_MESSAGE_BYTES = 32 * 1024
#: Encoded chunks queued for the decoder before the source counts as stuck.
_DECODER_QUEUE_CHUNKS = 64
_MAX_CREDENTIAL_FILE_BYTES = 256 * 1024
MAX_DJS = 50
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
# The longest Basic value a saved account can send: a 64-character ASCII
# username, a colon and a 256-character password of up to 4 UTF-8 bytes
# each, base64-encoded.  Longer values are refused before decoding.
_MAX_BASIC_LENGTH = 4 * -(-(64 + 1 + 256 * 4) // 3)


@dataclass(frozen=True)
class TalkoverSettings:
    """How browser talkover ducks the station.

    Attributes:
        mic_return_seconds: Seconds the music takes to come back up after
            the talker lets go.
        mic_duck_db: How far the music is lowered while someone talks.
    """

    mic_return_seconds: float = 0.5
    mic_duck_db: float = 12.0

    @classmethod
    def parse(cls, data: object) -> TalkoverSettings:
        """Validate *data* (a mapping); raises ``ValueError`` naming the bad field."""
        if not isinstance(data, dict):
            raise ValueError("Expected an object")
        values = {}
        for name, low, high in (("mic_return_seconds", 0.1, 10.0), ("mic_duck_db", 0.0, 30.0)):
            value = data.get(name, getattr(cls, name))
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or not low <= value <= high
            ):
                raise ValueError(f"{name} must be a number from {low:g} to {high:g}")
            values[name] = float(value)
        return cls(**values)

    def as_dict(self) -> dict[str, float]:
        """The settings for JSON."""
        return {"mic_return_seconds": self.mic_return_seconds, "mic_duck_db": self.mic_duck_db}


def _valid_username(username: object) -> bool:
    return (
        isinstance(username, str)
        and 1 <= len(username) <= 64
        and username.isascii()
        and username.isprintable()
        and ":" not in username
        and username.strip() == username
    )


class DjCredentials:
    """A station's DJ accounts in ``live_djs.json``: scrypt hashes, never passwords.

    The file is written atomically (a temporary file, fsynced, then
    renamed) and readable only by its owner on POSIX.  A damaged file
    fails closed: nobody can log in until it is fixed or replaced, and
    the accounts list reports the error.  Backups keep the accounts but
    not their hashes (:func:`strip_secrets`), so passwords are entered
    again after a restore.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        # Unknown names are checked against this, so a wrong name costs
        # the same scrypt run as a wrong password.
        self._dummy_salt = secrets.token_bytes(16)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "users": {}, "settings": {}}
        if self.path.stat().st_size > _MAX_CREDENTIAL_FILE_BYTES:
            raise ValueError("The live DJ file is too large")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("The live DJ file is not in a known format")
        users = data.get("users", {})
        settings = data.get("settings", {})
        if not isinstance(users, dict) or len(users) > MAX_DJS or not isinstance(settings, dict):
            raise ValueError("The live DJ file is not in a known format")
        for username, record in users.items():
            if not _valid_username(username) or not isinstance(record, dict):
                raise ValueError("The live DJ file has a damaged account")
            if not isinstance(record.get("name"), str):
                raise ValueError("The live DJ file has a damaged account")
        return {"version": 1, "users": users, "settings": settings}

    def _write(self, data: dict[str, Any]) -> None:
        atomic_write(self.path, json.dumps(data, indent=2), mode=0o600)

    def accounts(self) -> list[dict[str, Any]]:
        """The accounts for the settings page: names only, never hashes."""
        with self._lock:
            users = self._read()["users"]
        return [
            {
                "username": username,
                "display_name": record["name"],
                "password_set": _hash_parts(record) is not None,
            }
            for username, record in sorted(users.items(), key=lambda item: item[0].lower())
        ]

    def save(self, username: object, display_name: object, password: object) -> dict[str, Any]:
        """Add or change an account; ``password=None`` keeps the saved one.

        Raises:
            ValueError: A field is invalid; the message names it.
        """
        if not _valid_username(username):
            raise ValueError(
                "Username must be 1 to 64 printable ASCII characters, without a colon "
                "or spaces at either end"
            )
        assert isinstance(username, str)
        if (
            not isinstance(display_name, str)
            or not 1 <= len(display_name.strip()) <= 80
            or not display_name.isprintable()
        ):
            raise ValueError("Display name must be 1 to 80 printable characters")
        if password is not None and (
            not isinstance(password, str) or not 12 <= len(password) <= 256
        ):
            raise ValueError("Password must be 12 to 256 characters")
        digest = None
        salt = secrets.token_bytes(16)
        if isinstance(password, str):
            digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
        with self._lock:
            data = self._read()
            users = data["users"]
            old = users.get(username)
            if old is None and len(users) >= MAX_DJS:
                raise ValueError(f"A station can have at most {MAX_DJS} live DJs")
            record: dict[str, str] = {"name": display_name.strip()}
            if digest is not None:
                record.update(salt=salt.hex(), hash=digest.hex())
            elif old is not None and _hash_parts(old) is not None:
                record.update(salt=old["salt"], hash=old["hash"])
            else:
                raise ValueError("Password must be 12 to 256 characters")
            users[username] = record
            self._write(data)
        return {
            "username": username,
            "display_name": record["name"],
            "password_set": _hash_parts(record) is not None,
        }

    def delete(self, username: str) -> bool:
        """Remove an account; whether it existed."""
        with self._lock:
            data = self._read()
            if data["users"].pop(username, None) is None:
                return False
            self._write(data)
            return True

    def settings(self) -> TalkoverSettings:
        """The saved talkover settings; the defaults when unset or unreadable."""
        try:
            with self._lock:
                return TalkoverSettings.parse(self._read()["settings"])
        except (OSError, ValueError, TypeError):
            return TalkoverSettings()

    def save_settings(self, settings: TalkoverSettings) -> None:
        """Save the talkover settings beside the accounts."""
        with self._lock:
            data = self._read()
            data["settings"] = settings.as_dict()
            self._write(data)

    def authenticate(self, header: str) -> tuple[str, str] | None:
        """``(username, display name)`` for a valid Basic header, else ``None``.

        Every refusal after the header parses costs one scrypt run, a
        known name or not, and names and hashes are compared in constant
        time.  A damaged file refuses everyone.
        """
        try:
            scheme, _, encoded = header.partition(" ")
            if scheme.lower() != "basic" or not encoded or len(encoded) > _MAX_BASIC_LENGTH:
                return None
            username, sep, password = (
                base64.b64decode(encoded.strip(), validate=True).decode("utf-8").partition(":")
            )
            if not sep:
                return None
            with self._lock:
                users = self._read()["users"]
            record = None
            for stored_name, stored_record in users.items():
                if hmac.compare_digest(username.encode(), stored_name.encode()):
                    record = stored_record
            parts = _hash_parts(record) if isinstance(record, dict) else None
            salt, expected = parts if parts is not None else (self._dummy_salt, bytes(64))
            candidate = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
            if parts is not None and hmac.compare_digest(candidate, expected):
                assert isinstance(record, dict)
                return username, str(record["name"])
            return None
        except (ValueError, UnicodeError, binascii.Error, TypeError, OSError):
            return None


def _hash_parts(record: object) -> tuple[bytes, bytes] | None:
    """A record's ``(salt, hash)``, or ``None`` when it has no usable password."""
    if not isinstance(record, dict):
        return None
    try:
        salt = bytes.fromhex(str(record["salt"]))
        digest = bytes.fromhex(str(record["hash"]))
    except (KeyError, ValueError):
        return None
    return (salt, digest) if len(salt) == 16 and len(digest) == 64 else None


def strip_secrets(raw: bytes) -> bytes:
    """``live_djs.json`` for a backup: the accounts and settings, no hashes.

    A file that cannot be read is left out of the backup altogether
    (``b""`` tells the caller to skip it) rather than copied with its
    secrets.
    """
    try:
        data = json.loads(raw.decode("utf-8"))
        users = data["users"] if isinstance(data, dict) else None
        if not isinstance(users, dict):
            return b""
        clean = {
            "version": 1,
            "users": {
                str(name): {"name": str(record.get("name", ""))}
                for name, record in users.items()
                if isinstance(record, dict)
            },
            "settings": data.get("settings", {}) if isinstance(data.get("settings"), dict) else {},
        }
    except (ValueError, UnicodeError, KeyError, TypeError, AttributeError):
        return b""
    return json.dumps(clean, indent=2).encode("utf-8")


class PcmBuffer:
    """A bounded, nonblocking stereo float32 FIFO; old audio is dropped on overflow.

    One producer (a decoder or socket thread) calls ``push``; one consumer,
    the mix bus thread, calls ``pop`` and ``frames``.  The lock is never
    held across I/O or a wait: ``push`` holds it to append one array
    reference and drop old ones (bounded by the cap), ``pop`` to copy at
    most one block, so the bus waits at most microseconds.  It stays a
    lock rather than a lock-free ring because dropping the oldest audio
    moves the read position from the producer's side.
    """

    def __init__(self, seconds: float) -> None:
        self._lock = threading.Lock()
        self._chunks: deque[np.ndarray] = deque()
        self._head = 0
        self._frames = 0
        self._limit = max(1, int(seconds * SAMPLE_RATE))

    @property
    def frames(self) -> int:
        """Frames waiting."""
        with self._lock:
            return self._frames

    def push_frames(self, frames: np.ndarray) -> None:
        """Queue ``(n, 2)`` float32 *frames*, made finite and clipped to [-1, 1]."""
        np.nan_to_num(frames, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
        np.clip(frames, -1.0, 1.0, out=frames)
        with self._lock:
            self._chunks.append(frames)
            self._frames += len(frames)
            while self._frames > self._limit and self._chunks:
                excess = min(self._frames - self._limit, len(self._chunks[0]) - self._head)
                self._head += excess
                self._frames -= excess
                if self._head == len(self._chunks[0]):
                    self._chunks.popleft()
                    self._head = 0

    def push(self, pcm: bytes) -> None:
        """Queue interleaved little-endian float32 stereo bytes (whole frames)."""
        self.push_frames(np.frombuffer(pcm, dtype="<f4").reshape(-1, 2).copy())

    def pop(self, frames: int) -> np.ndarray:
        """Take up to *frames* frames, padded with silence when fewer are waiting."""
        out = np.zeros((frames, 2), np.float32)
        with self._lock:
            offset = 0
            while offset < frames and self._chunks:
                head = self._chunks[0]
                take = min(frames - offset, len(head) - self._head)
                out[offset : offset + take] = head[self._head : self._head + take]
                offset += take
                self._head += take
                self._frames -= take
                if self._head == len(head):
                    self._chunks.popleft()
                    self._head = 0
        return out


class MicResampler:
    """Int16 mono PCM at the browser's rate to float32 stereo at the station's.

    Linear interpolation, carrying the last sample and the fractional
    position between messages, so message boundaries leave no clicks.
    Plenty for speech; it runs on the socket's task, not the bus thread.
    """

    def __init__(self, rate: int) -> None:
        self._step = rate / SAMPLE_RATE
        self._last = 0.0
        self._phase = 0.0  # position of the next output sample, in input samples after _last

    def convert(self, pcm: bytes) -> np.ndarray:
        """Stereo float32 frames for one message of Int16 mono *pcm*."""
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if not len(samples):
            return np.zeros((0, 2), np.float32)
        if self._step == 1.0:
            mono = samples
        else:
            source = np.concatenate(([self._last], samples))
            # Index 0 of *source* is the previous message's last sample.
            positions = np.arange(self._phase, len(samples), self._step, dtype=np.float64)
            mono = np.interp(positions, np.arange(len(source)), source).astype(np.float32)
            following = positions[-1] + self._step if len(positions) else self._phase
            self._phase = following - len(samples)
            self._last = float(samples[-1])
        return np.repeat(mono[:, None], 2, axis=1)


class LiveSource:
    """One connected source: an FFmpeg decoder fed and drained off the event loop.

    The route hands encoded chunks to :meth:`write`, which only queues
    them; a writer thread feeds FFmpeg's stdin and a reader thread drains
    its stdout into :attr:`buffer`.  Nothing here blocks the event loop
    or the mix bus, and a stuck decoder is noticed (its queue fills) and
    the source dropped.

    Attributes:
        started: Set once audible audio has been decoded; the takeover
            waits for it, so a client sending silence stays off the air.
        primed: Set by the mix bus once it has buffered enough to start.
        last_audio: Monotonic time of the last audible decoded audio.
    """

    def __init__(self, username: str, name: str, demuxer: str) -> None:
        self._station_slug = CURRENT_STATION.get()
        self.username = username
        self.name = name
        self.buffer = PcmBuffer(SOURCE_BUFFER_SECONDS)
        self.opened = time.monotonic()
        self.last_audio = self.opened
        self.received = 0
        self.started = False
        self.primed = False
        # Underruns the mix bus has met, counted there and logged by the
        # route: nothing on the audio thread writes a log line.
        self.underruns = 0
        self.on_audio: Callable[[], None] = lambda: None
        self._closed = False
        self._chunks: queue.Queue[bytes | None] = queue.Queue(_DECODER_QUEUE_CHUNKS)
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise OSError("FFmpeg is not installed")
        self.process = subprocess.Popen(  # nosec B603 -- fixed argv, no shell
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                # Only the pipe: nothing in the stream can reach a file or the network.
                "-protocol_whitelist",
                "pipe",
                "-probesize",
                "32768",
                "-analyzeduration",
                "500000",
                "-f",
                demuxer,
                "-i",
                "pipe:0",
                "-map",
                "0:a:0",
                "-vn",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                "2",
                "-f",
                "f32le",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._writer = threading.Thread(target=self._feed, name="autodj-live-feed", daemon=True)
        self._reader = threading.Thread(target=self._read, name="autodj-live-decode", daemon=True)
        track_station_thread(self._writer, self._station_slug)
        track_station_thread(self._reader, self._station_slug)
        self._writer.start()
        self._reader.start()

    def _feed(self) -> None:
        stdin = self.process.stdin
        assert stdin is not None
        try:
            while (chunk := self._chunks.get()) is not None and not self._closed:
                stdin.write(chunk)
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(OSError, ValueError):
                stdin.close()

    def _read(self) -> None:
        stdout = self.process.stdout
        assert stdout is not None
        carry = b""
        while not self._closed:
            try:
                data = stdout.read(SAMPLE_RATE // 10 * 8)
            except (OSError, ValueError):
                break
            if not data:
                break
            data = carry + data
            usable = len(data) - len(data) % 8
            carry = data[usable:]
            if not usable:
                continue
            frames = np.frombuffer(data[:usable], dtype="<f4").reshape(-1, 2).copy()
            audible = bool(np.max(np.abs(np.nan_to_num(frames))) > SILENCE_LEVEL)
            self.buffer.push_frames(frames)
            if audible:
                self.last_audio = time.monotonic()
                if not self.started:
                    self.started = True
                    try:
                        self.on_audio()
                    except Exception:
                        logger.exception("Could not announce the live source")

    def write(self, chunk: bytes) -> None:
        """Queue *chunk* for the decoder without blocking.

        Raises:
            ValueError: The connection's byte limit is reached.
            OverflowError: The decoder is not keeping up.
        """
        self.received += len(chunk)
        if self.received > MAX_SOURCE_BYTES:
            raise ValueError("Live source byte limit reached")
        try:
            self._chunks.put_nowait(chunk)
        except queue.Full:
            raise OverflowError("The live decoder is not keeping up") from None

    def finish(self, timeout: float = 1.0) -> None:
        """After a clean end of input, let the decoder drain its last frames."""
        with contextlib.suppress(queue.Full):
            self._chunks.put_nowait(None)
        self._writer.join(timeout)
        self._reader.join(timeout)

    def close(self) -> None:
        """Stop the decoder and its threads; safe to call more than once."""
        self._closed = True
        with contextlib.suppress(queue.Full):
            self._chunks.put_nowait(None)
        if self.process.poll() is None:
            with contextlib.suppress(OSError):
                self.process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=2)
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe is not None:
                with contextlib.suppress(OSError, ValueError):
                    pipe.close()
        self._writer.join(timeout=2)
        self._reader.join(timeout=2)


class LiveDesk:
    """A station's live input: one source and one talker at a time.

    Shared by the routes (which claim and release) and the mix bus (which
    only reads :attr:`source`, :attr:`return_source`, :attr:`mic` and
    :attr:`settings`, never taking this desk's lock, so nothing here can
    hold up the audio).  Starting a decoder happens outside the lock.

    Args:
        credentials: The station's ``live_djs.json``.
        on_start: Called (on the decoder's thread) when a source first
            sends audible audio.
        before_return: Called on the releasing thread while the source
            is still on the air, to ready automation; it may block briefly.
        on_end: Called after a source that was on the air is gone.
    """

    def __init__(
        self,
        credentials: Path,
        on_start: Callable[[], None] = lambda: None,
        before_return: Callable[[LiveSource], None] = lambda _source: None,
        on_end: Callable[[], None] = lambda: None,
        on_talkover: Callable[[], None] = lambda: None,
    ) -> None:
        self._station_slug = CURRENT_STATION.get()
        self.credentials = DjCredentials(credentials)
        self.settings = self.credentials.settings()
        self._lock = threading.Lock()
        self._claiming = False
        self._local_owner: object | None = None
        self._releasing: LiveSource | None = None
        self.source: LiveSource | None = None
        self.return_source: LiveSource | None = None
        self.since: str | None = None
        self.mic: PcmBuffer | None = None
        self.on_start = on_start
        self.before_return = before_return
        self.on_end = on_end
        self.on_talkover = on_talkover
        self.crossfade_seconds = 1.0
        self.attack_seconds = 0.04
        self.title = "Live"

    def claim(self, username: str, name: str, demuxer: str) -> LiveSource | None:
        """Start a decoder for a DJ, or ``None`` when the station has a source.

        Raises:
            OSError: FFmpeg could not be started.
        """
        with self._lock:
            if self.source is not None or self._claiming or self._local_owner is not None:
                return None
            self._claiming = True
        try:
            token = CURRENT_STATION.set(self._station_slug)
            try:
                source = LiveSource(username, name, demuxer)
            finally:
                CURRENT_STATION.reset(token)
        except BaseException:
            with self._lock:
                self._claiming = False
            raise
        source.on_audio = lambda: self._started(source)
        with self._lock:
            self.source = source
            self._claiming = False
        return source

    def _started(self, source: LiveSource) -> None:
        with self._lock:
            if self.source is not source:
                return
            self.since = datetime.now(UTC).isoformat()
        # Off the decoder's thread: recording the session may wait on the
        # database, and the decoder must keep draining meanwhile.
        thread = threading.Thread(target=self._run_on_start, name="autodj-live-start", daemon=True)
        track_station_thread(thread, getattr(source, "_station_slug", self._station_slug))
        thread.start()

    def _run_on_start(self) -> None:
        try:
            self.on_start()
        except Exception:
            logger.exception("Could not record the start of the live source")

    def release(self, source: LiveSource) -> None:
        """Hand back to automation (if *source* was on the air) and stop it."""
        with self._lock:
            # Once: the route, a removed account and a stopping station may
            # all let the same source go at once.
            if self.source is not source or self._releasing is source:
                return
            self._releasing = source
            was_on_air = source.started
        if was_on_air:
            try:
                self.before_return(source)
            except Exception:
                logger.exception("Could not ready automation after the live source")
        with self._lock:
            self.source = None
            self.since = None
            self._releasing = None
            if was_on_air:
                self.return_source = source
        try:
            source.close()
        finally:
            if was_on_air:
                try:
                    self.on_end()
                except Exception:
                    logger.exception("Could not record the end of the live source")

    def release_username(self, username: str) -> None:
        """Disconnect *username*'s source, if it is the one connected."""
        source = self.source
        if source is not None and source.username == username:
            self.release(source)

    def clear_return_source(self, source: LiveSource) -> None:
        """Forget the source that has faded out (called by the mix bus)."""
        if self.return_source is source:
            self.return_source = None

    def status(self) -> dict[str, str] | None:
        """``{"dj", "since"}`` while a DJ is on the air, else ``None``."""
        source = self.source
        since = self.since
        if source is None or since is None or not source.started:
            return None
        return {"dj": source.name, "since": since}

    @property
    def talkover(self) -> bool:
        """Whether someone is talking from a browser."""
        return self.mic is not None

    @property
    def active(self) -> bool:
        """Whether live input is connected (a source or a talker)."""
        return self.source is not None or self.mic is not None

    def claim_mic(self) -> PcmBuffer | None:
        """The talkover buffer, or ``None`` while someone else is talking."""
        with self._lock:
            if self.mic is not None or self._local_owner is not None:
                return None
            self.mic = PcmBuffer(MIC_BUFFER_SECONDS)
        self.on_talkover()
        return self.mic

    def reserve_local(self, owner: object) -> bool:
        """Atomically exclude external sources and microphones during preparation."""
        with self._lock:
            if (
                self._local_owner is not None
                or self._claiming
                or self.source is not None
                or self.return_source is not None
                or self.mic is not None
            ):
                return False
            self._local_owner = owner
            return True

    def release_local(self, owner: object) -> None:
        """A stale socket cannot release a newer browser's reservation."""
        with self._lock:
            if self._local_owner is owner:
                self._local_owner = None

    def release_mic(self, mic: PcmBuffer) -> None:
        """Let the talker go; the music comes back up over the return time."""
        with self._lock:
            if self.mic is not mic:
                return
            self.mic = None
        self.on_talkover()
