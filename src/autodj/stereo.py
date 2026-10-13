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

import logging
import shutil
import subprocess  # nosec B404 -- FFmpeg runs with a fixed argv and no shell
import tempfile
from collections.abc import Callable
from pathlib import Path

import numpy as np
import soundfile as sf

SAMPLE_RATE = 44_100
logger = logging.getLogger(__name__)

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


def per_channel(fn: Callable[[np.ndarray], np.ndarray], audio: np.ndarray) -> np.ndarray:
    """Apply a mono function to each channel of *audio*.

    Args:
        fn: Function taking and returning a 1-D array.
        audio: Mono or ``(frames, channels)`` audio.

    Returns:
        ``fn(audio)`` for mono input; for 2-D input the per-channel results
        stacked on axis 1, each trimmed or zero-padded to the first
        channel's output length so the columns always line up.
    """
    if audio.ndim == 1:
        return fn(audio)
    columns = [np.asarray(fn(np.ascontiguousarray(audio[:, c]))) for c in range(audio.shape[1])]
    length = len(columns[0])
    fitted = []
    for column in columns:
        if len(column) >= length:
            fitted.append(column[:length])
        else:
            fitted.append(np.pad(column, (0, length - len(column))))
    return np.stack(fitted, axis=1).astype(np.float32)


def envelope(env: np.ndarray, like: np.ndarray) -> np.ndarray:
    """Shape a 1-D gain envelope so it broadcasts against *like*.

    Args:
        env: 1-D envelope, one gain per frame.
        like: The audio the envelope will multiply.

    Returns:
        *env* for mono audio, ``env[:, None]`` for 2-D audio.
    """
    return env if like.ndim == 1 else env[:, None]


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
    with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as decoded:
        result = subprocess.run(  # nosec B603 -- fixed argv, no shell
            command,
            stdout=decoded,
            stderr=subprocess.PIPE,
            check=False,
        )
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg could not decode {path}: {detail or 'unknown error'}")
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
        decoded.seek(0)
        audio, sr = sf.read(decoded, dtype="float32", always_2d=channels > 1)
    return audio, int(sr)


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
                return source.read(dtype="float32", always_2d=True), int(source.samplerate)
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
