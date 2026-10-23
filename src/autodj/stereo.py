"""Audio loading and stereo helpers shared by indexing, analysis and playback.

Every decode of a library file goes through :func:`load_mono` or
:func:`load_stereo`, so a file that plays also indexes, and both see the same
samples at the same level.  FFmpeg decodes MP3 and MP4-container audio;
soundfile decodes the rest, with FFmpeg as the fallback when it cannot.

The server mix is stereo ``float32`` shaped ``(frames, 2)`` at
:data:`SAMPLE_RATE`.  Most DSP in AutoDJ was written for mono arrays; these
helpers let that code run on each channel without rewriting it.
"""

from __future__ import annotations

import contextvars
import logging
import mmap
import os
import shutil
import subprocess  # nosec B404 -- FFmpeg runs with a fixed argv and no shell
import tempfile
import threading
import weakref
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import numpy as np
import soundfile as sf

SAMPLE_RATE = 44_100
logger = logging.getLogger(__name__)

#: Decoded stereo audio longer than this (ten minutes, about 210 MB) is kept
#: in a temporary file mapped into memory instead of in ordinary memory, so
#: an hour-long mix costs disk space while it plays, not gigabytes of RAM.
#: The operating system reads it in as it is played and can drop it again.
SPILL_FRAMES = 10 * 60 * SAMPLE_RATE
_SPILL_BYTES = SPILL_FRAMES * 2 * 4
#: A track longer than this is analysed from its start and end only (its
#: first and last :data:`ANALYSIS_WINDOW_SECONDS`), so cue, beat and
#: structure analysis never holds more than a few minutes of it.
LONG_TRACK_SECONDS = 15 * 60
ANALYSIS_WINDOW_SECONDS = 5 * 60
_spill_dir = Path("index") / "cache" / "spill"


@dataclass
class AudioWorkspace:
    """Conservative allocation budget shared by a review and its decode threads."""

    directory: Path
    max_bytes: int
    reserve_bytes: int
    allocated: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)
    mappings: weakref.WeakSet[mmap.mmap] = field(default_factory=weakref.WeakSet)

    def reserve(self, nbytes: int) -> None:
        with self.lock:
            if nbytes < 0 or self.allocated + nbytes > self.max_bytes:
                raise OSError(
                    "Review scratch allocation limit exceeded; choose a shorter programme"
                )
            if shutil.disk_usage(self.directory).free < nbytes + self.reserve_bytes:
                raise OSError(
                    "Not enough scratch space for review; free disk space or choose a shorter programme"
                )
            self.allocated += nbytes

    def release(self, nbytes: int) -> None:
        with self.lock:
            self.allocated -= nbytes

    def track(self, mapping: mmap.mmap, nbytes: int) -> None:
        # Views keep the mapping alive; the workspace must not retain freed PCM.
        with self.lock:
            self.mappings.add(mapping)
        weakref.finalize(mapping, self.release, nbytes)


_AUDIO_WORKSPACE: contextvars.ContextVar[AudioWorkspace | None] = contextvars.ContextVar(
    "autodj_audio_workspace", default=None
)


@contextmanager
def audio_workspace(
    directory: Path, *, max_bytes: int = 2 * 1024**3, reserve_bytes: int = 64 * 1024**2
) -> Iterator[AudioWorkspace]:
    """Keep offline PCM on owned scratch storage; normal playout is unchanged.

    Accounting bounds live allocations, including temporary copies.
    Nested render helpers and station decode threads share the same budget.
    All allocated PCM is valid only inside this scope; mappings close before
    the caller removes the scratch directory, including on Windows.
    """
    current = _AUDIO_WORKSPACE.get()
    if current is not None:
        yield current
        return
    if max_bytes <= 0 or reserve_bytes < 0:
        raise ValueError("Audio workspace limits must be positive")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    workspace = AudioWorkspace(directory, max_bytes, reserve_bytes)
    token = _AUDIO_WORKSPACE.set(workspace)
    try:
        yield workspace
    finally:
        _AUDIO_WORKSPACE.reset(token)
        for mapping in list(workspace.mappings):
            mapping.close()


def configure_spill_dir(index_dir: Path) -> None:
    """Use the index disk for long decodes and remove leftovers from earlier runs."""
    global _spill_dir
    _spill_dir = Path(index_dir) / "cache" / "spill"
    if _spill_dir.is_dir():
        for stale in _spill_dir.glob("autodj-audio-*"):
            # Windows keeps an active mapping's file locked.
            with suppress(OSError):
                stale.unlink()


def _spill_location() -> Path | None:
    """Return writable spill directory, or system temp when unavailable."""
    workspace = _AUDIO_WORKSPACE.get()
    if workspace is not None:
        return workspace.directory
    try:
        _spill_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=_spill_dir):
            pass
    except OSError:
        logger.warning("Cannot write audio spill directory %s; using system temp", _spill_dir)
        return None
    return _spill_dir


def _temporary_file() -> IO[bytes]:
    return tempfile.TemporaryFile(prefix="autodj-audio-", dir=_spill_location())


def _mapped(nbytes: int) -> mmap.mmap:
    """A writable, zeroed mapping of a new temporary file of *nbytes*.

    The file has no name once it is open (POSIX) or is deleted when the
    mapping closes (Windows), so nothing is left behind.
    """
    workspace = _AUDIO_WORKSPACE.get()
    if workspace is not None:
        workspace.reserve(nbytes)
    try:
        return _map_file(nbytes, workspace)
    except BaseException:
        if workspace is not None:
            workspace.release(nbytes)
        raise


def _map_file(nbytes: int, workspace: AudioWorkspace | None) -> mmap.mmap:
    with _temporary_file() as handle:
        if hasattr(os, "posix_fallocate"):
            # Reserve the space now: a full disk is an OSError here, not a
            # crash (SIGBUS) when a sparse page is first written.
            os.posix_fallocate(handle.fileno(), 0, nbytes)
        elif os.name == "nt":
            handle.truncate(nbytes)
        else:
            # truncate leaves holes on macOS; write pages before mapping.
            block = bytes(8 * 1024 * 1024)
            for offset in range(0, nbytes, len(block)):
                handle.write(block[: min(len(block), nbytes - offset)])
            handle.flush()
        mapping = mmap.mmap(handle.fileno(), nbytes)
        if workspace is not None:
            workspace.track(mapping, nbytes)
        return mapping


def reserve_audio(nbytes: int) -> None:
    """Charge an explicit offline PCM allocation to the active review workspace."""
    workspace = _AUDIO_WORKSPACE.get()
    if workspace is not None:
        workspace.reserve(nbytes)


def copy_audio(audio: np.ndarray) -> np.ndarray:
    """Own PCM without allocating an unbounded ordinary-memory copy."""
    channels = 1 if audio.ndim == 1 else audio.shape[1]
    output = audio_buffer(len(audio), channels)
    if audio.ndim == 1:
        output = output[:, 0]
    block = SAMPLE_RATE * 10
    for at in range(0, len(audio), block):
        output[at : at + block] = audio[at : at + block]
    return output


def audio_buffer(frames: int, channels: int = 2) -> np.ndarray:
    """Return a zeroed ``(frames, channels)`` float32 array.

    Up to :data:`SPILL_FRAMES` frames it is an ordinary array; a longer one
    is a plain ndarray over a temporary file mapped into memory.
    """
    if frames == 0 or (frames <= SPILL_FRAMES and _AUDIO_WORKSPACE.get() is None):
        return np.zeros((frames, channels), dtype=np.float32)
    # ndarray owns the mapping directly: frombuffer retains an exported
    # memoryview that prevents explicit scope cleanup on Windows.
    return np.ndarray((frames, channels), dtype=np.float32, buffer=_mapped(frames * channels * 4))


def join_audio(parts: Sequence[np.ndarray]) -> np.ndarray:
    """``np.concatenate(parts).astype(np.float32)``, spilled when long and 2-D.

    Joining a whole track to a transition copies it; a long one goes to
    :func:`audio_buffer` instead of ordinary memory.
    """
    if not parts:
        return audio_buffer(0)
    total = sum(len(part) for part in parts)
    if (total <= SPILL_FRAMES and _AUDIO_WORKSPACE.get() is None) or parts[0].ndim != 2:
        return np.concatenate(parts, axis=0).astype(np.float32, copy=False)
    out = audio_buffer(total, parts[0].shape[1])
    at = 0
    for part in parts:
        out[at : at + len(part)] = part
        at += len(part)
    return out


class AudioSpool:
    """Collect raw float32 samples from a pipe, in memory or, once long, on disk.

    Up to :data:`SPILL_FRAMES` of stereo audio stays in a growing bytearray;
    past that the bytes so far and the rest go to a temporary file, which
    :meth:`buffer` maps into memory.
    """

    def __init__(self) -> None:
        """Start empty, in memory."""
        self._memory = bytearray()
        self._file: IO[bytes] | None = None
        self._size = 0
        self._workspace = _AUDIO_WORKSPACE.get()

    def write(self, data: bytes) -> None:
        """Append *data*."""
        workspace = self._workspace
        if workspace is not None:
            workspace.reserve(len(data))
        if self._file is None and (
            workspace is not None or len(self._memory) + len(data) > _SPILL_BYTES
        ):
            self._file = _temporary_file()
            self._file.write(self._memory)
            self._memory = bytearray()
        if self._file is not None:
            self._file.write(data)
        else:
            self._memory += data
        self._size += len(data)

    def buffer(self, frame_bytes: int = 8) -> bytearray | mmap.mmap:
        """The samples as a writable buffer for :func:`numpy.frombuffer`.

        A mapped file is cut to whole frames of *frame_bytes*.
        """
        if self._file is None:
            return self._memory
        handle, self._file = self._file, None
        with handle:
            handle.flush()
            usable = self._size - self._size % frame_bytes
            if usable == 0:
                self.close()
                return bytearray()
            mapping = mmap.mmap(handle.fileno(), usable)
            if self._workspace is not None:
                self._workspace.track(mapping, usable)
                self._workspace.release(self._size - usable)
            self._size = 0
            return mapping

    def close(self) -> None:
        """Drop a temporary file :meth:`buffer` did not take."""
        if self._file is not None:
            self._file.close()
            self._file = None
        if self._workspace is not None:
            self._workspace.release(self._size)
        self._size = 0


def all_finite_and_audible(audio: np.ndarray, block: int = SAMPLE_RATE * 60) -> bool:
    """Whether *audio* is non-empty, all finite and not all zero.

    Checked a minute at a time, so a long track needs no full-size
    temporary arrays.
    """
    if not len(audio):
        return False
    audible = False
    for start in range(0, len(audio), block):
        piece = audio[start : start + block]
        if not np.isfinite(piece).all():
            return False
        audible = audible or bool(np.any(piece))
    return audible


# FFmpeg decodes MP4-container audio (ALAC, AAC), which libsndfile cannot
# open, and MP3, where it recovers frames libsndfile rejects and keeps
# libsndfile's native diagnostics off the terminal.
FFMPEG_FORMATS = frozenset({".mp3", ".aac", ".m4a", ".mp4"})
#: FFmpeg input options for every file it reads: only local files and pipes,
#: so a file that probes as a playlist (HLS, concat) cannot reach the network.
SAFE_FFMPEG_INPUT: tuple[str, ...] = ("-protocol_whitelist", "file,pipe")

# FFmpeg prints this for every MP3 without a Xing/VBRI header.  The decode is
# complete and correct; only the container's duration estimate is approximate.
_ROUTINE_FFMPEG_NOTES = ("Estimating duration from bitrate, this may be inaccurate",)


def to_stereo(audio: np.ndarray) -> np.ndarray:
    """Return *audio* as a C-contiguous ``(frames, 2)`` float32 array.

    Args:
        audio: Mono ``(frames,)`` or multi-channel ``(frames, channels)`` audio.

    Returns:
        Stereo audio.  Mono is duplicated to both channels; extra channels
        beyond the first two are dropped.
    """
    arr = np.asarray(audio, dtype=np.float32)
    if arr.ndim == 2 and arr.shape[1] == 2 and arr.flags.c_contiguous:
        return arr
    if len(arr) > SPILL_FRAMES or _AUDIO_WORKSPACE.get() is not None:
        # A long track's copy goes to a mapped file, not ordinary memory.
        out = audio_buffer(len(arr))
        left = arr if arr.ndim == 1 else arr[:, 0]
        out[:, 0] = left
        out[:, 1] = left if arr.ndim == 1 or arr.shape[1] == 1 else arr[:, 1]
        return out
    if arr.ndim == 1:
        arr = np.stack([arr, arr], axis=1)
    elif arr.shape[1] == 1:
        arr = np.repeat(arr, 2, axis=1)
    elif arr.shape[1] > 2:
        arr = arr[:, :2]
    return np.ascontiguousarray(arr, dtype=np.float32)


def mono(audio: np.ndarray) -> np.ndarray:
    """Return a mono view of *audio* for analysis.

    Args:
        audio: Mono or ``(frames, channels)`` audio.

    Returns:
        The channel mean for 2-D input, or *audio* unchanged for 1-D input.
    """
    if audio.ndim == 1:
        return audio
    return audio.mean(axis=1).astype(np.float32)


def envelope(env: np.ndarray, like: np.ndarray) -> np.ndarray:
    """Shape a 1-D gain envelope so it broadcasts against *like*.

    Args:
        env: 1-D envelope, one gain per frame.
        like: The audio the envelope will multiply.

    Returns:
        *env* for mono audio, ``env[:, None]`` for 2-D audio.
    """
    return env if like.ndim == 1 else env[:, None]


class UnplayableAudioError(RuntimeError):
    """The decoder rejected the audio, rather than failing to run."""


class AudioAccessError(RuntimeError):
    """The decoder could not access its input or required system resources."""


def decode_error(path: str | Path, detail: str) -> RuntimeError:
    """Classify explicit invalid audio; unknown decoder/system failures remain retryable."""
    invalid = any(
        message in detail
        for message in (
            "Invalid data found when processing input",
            "matches no streams",
            "does not contain any stream",
        )
    )
    unavailable = any(
        message in detail
        for message in (
            "Permission denied",
            "Input/output error",
            "No such file or directory",
            "Cannot allocate memory",
            "No space left on device",
            "Resource temporarily unavailable",
        )
    )
    error = AudioAccessError if unavailable else UnplayableAudioError if invalid else RuntimeError
    return error(f"FFmpeg could not decode {path}: {detail or 'unknown error'}")


class TrackTooLongError(ValueError):
    """A track is longer than the caller is willing to hold in memory.

    Attributes:
        seconds: The track's length (at least the part that was read).
    """

    def __init__(self, path: str, seconds: float) -> None:
        """Record *path* and its length in *seconds*."""
        super().__init__(f"{path} is {seconds / 60:.1f} minutes long")
        self.seconds = seconds


def load_with_ffmpeg(
    path: str | Path,
    *,
    channels: int,
    sample_rate: int | None = None,
    max_seconds: float | None = None,
) -> tuple[np.ndarray, int]:
    """Decode *path*'s first audio stream through FFmpeg into float32.

    Args:
        path: Audio file path.
        channels: Output channels.  ``1`` returns ``(frames,)``; more
            returns ``(frames, channels)``.
        sample_rate: Output rate in Hz, or ``None`` for the file's own.
        max_seconds: Decode at most this many seconds, or ``None`` for all.

    Returns:
        ``(audio, sample_rate)``.

    Raises:
        RuntimeError: If FFmpeg is not installed or cannot decode the file.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError(
            f"FFmpeg is required to decode {Path(path).suffix or 'this audio format'} files"
        )
    command = [ffmpeg, "-v", "warning", "-nostdin", *SAFE_FFMPEG_INPUT, "-i", str(path)]
    command += ["-map", "0:a:0"]
    command += ["-ac", str(channels)]
    if sample_rate is not None:
        command += ["-ar", str(sample_rate)]
    if max_seconds is not None:
        command += ["-t", f"{max_seconds:.3f}"]
    command += ["-c:a", "pcm_f32le", "-f", "wav", "pipe:1"]

    # Spool larger decodes to disk so the WAV byte stream does not duplicate a
    # full track in RAM alongside the numpy array.  The returned array remains
    # valid after the temporary file closes.
    spill_dir = _spill_location()
    with tempfile.SpooledTemporaryFile(
        max_size=16 * 1024 * 1024,
        prefix="autodj-audio-",
        dir=str(spill_dir) if spill_dir is not None else None,
    ) as decoded:
        result = subprocess.run(  # nosec B603 -- fixed argv, no shell
            command,
            stdout=decoded,
            stderr=subprocess.PIPE,
            check=False,
        )
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        if result.returncode != 0:
            raise decode_error(path, detail)
        detail = "\n".join(
            line
            for line in detail.splitlines()
            if not any(note in line for note in _ROUTINE_FFMPEG_NOTES)
        ).strip()
        if detail:
            # A decoder can recover and succeed while still reporting damaged
            # frames. Preserve those diagnostics through the progress console,
            # with the filename even when multiple tracks decode concurrently.
            # The track field lets a DecodeReport collect them for the run's log.
            logger.warning(
                "Decoded %s with warnings:\n%s", path, detail, extra={"track": str(path)}
            )
        size = decoded.seek(0, os.SEEK_END)
        decoded.seek(0)
        if size <= _SPILL_BYTES:
            audio, sr = sf.read(decoded, dtype="float32", always_2d=channels > 1)
        else:
            audio, sr = _read_spilled(decoded, size)
            if channels == 1:
                audio = audio.reshape(-1)
    return audio, int(sr)


def _read_spilled(source_file: IO[bytes], size: int) -> tuple[np.ndarray, int]:
    """Read a long decoded WAV into :func:`audio_buffer` instead of ordinary memory."""
    with sf.SoundFile(source_file) as source:
        # A WAV written to a pipe may claim more frames than the file holds.
        frames = min(source.frames, size // (4 * source.channels))
        out = audio_buffer(frames, source.channels)
        read = len(source.read(frames, dtype="float32", always_2d=True, out=out))
        return out[:read], int(source.samplerate)


def _decode(
    path: str | Path, *, sample_rate: int | None = None, max_seconds: float | None = None
) -> tuple[np.ndarray, int]:
    """Decode *path* to ``(frames, channels)`` float32.

    FFmpeg decodes :data:`FFMPEG_FORMATS`; soundfile decodes the rest, and
    FFmpeg takes over when soundfile cannot read a file, for example a FLAC
    that loses sync over a network share.

    Args:
        path: Audio file path.
        sample_rate: Rate FFmpeg resamples to, or ``None`` for the file's own.
            soundfile always returns the file's own rate.
        max_seconds: Longest track to decode, or ``None`` for no limit.
            soundfile reads the length from the header; FFmpeg decodes at most
            a second past the limit.

    Returns:
        ``(audio, sample_rate)`` with *audio* shaped ``(frames, channels)``.

    Raises:
        RuntimeError: If neither decoder can read the file.
        TrackTooLongError: If the track is longer than *max_seconds*.
    """
    if Path(path).suffix.lower() not in FFMPEG_FORMATS:
        try:
            with sf.SoundFile(str(path)) as source:
                seconds = source.frames / source.samplerate
                if max_seconds is not None and seconds > max_seconds:
                    raise TrackTooLongError(str(path), seconds)
                rate = int(source.samplerate)
                if source.frames <= SPILL_FRAMES and _AUDIO_WORKSPACE.get() is None:
                    return source.read(dtype="float32", always_2d=True), rate
                if sample_rate is None or rate == sample_rate:
                    # A long track goes to a mapped file, not ordinary memory.
                    out = audio_buffer(source.frames, source.channels)
                    read = len(source.read(dtype="float32", always_2d=True, out=out))
                    return out[:read], rate
                # Resampling a long track at once copies it several times;
                # FFmpeg resamples it as it decodes.
        except (RuntimeError, OSError) as exc:
            logger.debug("soundfile could not read %s, trying FFmpeg: %s", path, exc)
    duration = None if max_seconds is None else max_seconds + 1.0
    audio, rate = load_with_ffmpeg(path, channels=2, sample_rate=sample_rate, max_seconds=duration)
    if max_seconds is not None and len(audio) > max_seconds * rate:
        raise TrackTooLongError(str(path), len(audio) / rate)
    return audio, rate


def load_mono(path: str | Path) -> tuple[np.ndarray, int]:
    """Load an audio file as mono float32 at its own sample rate, for analysis.

    Mono is the mean of the decoded channels, so it matches the level of the
    stereo audio that plays.  FFmpeg's own mono downmix is about 3 dB louder.

    Args:
        path: Audio file path.

    Returns:
        ``(audio, sample_rate)`` with *audio* shaped ``(frames,)``.

    Raises:
        RuntimeError: If neither decoder can read the file.
    """
    audio, rate = _decode(path)
    return mono(audio), rate


def load_stereo(
    path: str, target_sr: int = SAMPLE_RATE, max_seconds: float | None = None
) -> np.ndarray:
    """Load an audio file as stereo float32 at *target_sr*, for playback.

    A decoded track costs about 21 MB per minute (stereo float32 at
    44.1 kHz), so *max_seconds* refuses longer files before decoding all
    of them.

    Args:
        path: Audio file path.
        target_sr: Output sample rate in Hz.
        max_seconds: Longest track to load, or ``None`` for no limit.

    Returns:
        ``(frames, 2)`` float32 audio.

    Raises:
        RuntimeError: If neither decoder can read the file.
        TrackTooLongError: If the track is longer than *max_seconds*.
    """
    audio, sr = _decode(path, sample_rate=target_sr, max_seconds=max_seconds)
    audio = to_stereo(audio)
    if sr != target_sr:
        import librosa

        audio = librosa.resample(audio, orig_sr=sr, target_sr=target_sr, axis=0)
    return to_stereo(audio)


class DecodeReport(logging.Handler):
    """Collect one run's decoder warnings and skipped files, then save them.

    Use it as a context manager around a batch of decodes.  While it is
    active it receives every "decoded with warnings" record; the batch adds
    files it gave up on with :meth:`skip`.  The terminal still shows each
    warning as it happens; :meth:`write` keeps them to read after the run.
    """

    def __init__(self) -> None:
        """Start an empty report."""
        super().__init__(logging.WARNING)
        self.warned: dict[str, str] = {}
        self.skipped: dict[str, str] = {}

    def emit(self, record: logging.LogRecord) -> None:
        """Record a decode warning that names its track."""
        track = getattr(record, "track", None)
        if track is not None:
            self.warned[track] = record.getMessage().partition("\n")[2]

    def skip(self, path: str | Path, reason: object) -> None:
        """Record that *path* failed and was left out, and why."""
        self.skipped[str(path)] = str(reason)

    def __enter__(self) -> DecodeReport:
        """Start collecting warnings from the shared loader."""
        logger.addHandler(self)
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop collecting warnings."""
        logger.removeHandler(self)

    def summary(self, log_path: Path) -> str | None:
        """Return one line counting warnings and skips, or ``None`` when there were none."""
        warned = len(self.warned.keys() - self.skipped.keys())
        if not warned and not self.skipped:
            return None
        parts = []
        if warned:
            parts.append(f"{warned} {'file' if warned == 1 else 'files'} decoded with warnings")
        if self.skipped:
            count = len(self.skipped)
            parts.append(f"{count} {'file' if count == 1 else 'files'} failed and were skipped")
        return f"{'; '.join(parts)}. Details: {log_path}"

    def write(self, log_path: Path) -> None:
        """Save the warnings and skips to *log_path*, replacing an older report.

        A run with nothing to report removes an older report instead, so the
        file always describes the latest run.
        """
        if not self.warned and not self.skipped:
            log_path.unlink(missing_ok=True)
            return
        lines: list[str] = []
        for path, reason in sorted(self.skipped.items()):
            lines.append(f"Skipped: {path}")
            lines.extend(f"  {line}" for line in reason.splitlines())
            if path in self.warned:
                lines.extend(f"  {line}" for line in self.warned[path].splitlines())
        for path, detail in sorted(self.warned.items()):
            if path not in self.skipped:
                lines.append(f"Decoded with warnings: {path}")
                lines.extend(f"  {line}" for line in detail.splitlines())
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
