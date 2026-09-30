"""Stereo helpers shared by the server-side mix path.

The server mix is stereo ``float32`` shaped ``(frames, 2)`` at
:data:`SAMPLE_RATE`.  Most DSP in AutoDJ was written for mono arrays; these
helpers let that code run on each channel without rewriting it.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 -- FFmpeg runs with a fixed argv and no shell
import tempfile
from collections.abc import Callable
from pathlib import Path

import numpy as np
import soundfile as sf

SAMPLE_RATE = 44_100

# MP4-container audio (ALAC, AAC).  libsndfile cannot open it and librosa 1.0
# has no other decoder, so FFmpeg decodes it.
FFMPEG_FORMATS = frozenset({".aac", ".m4a", ".mp4"})


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
    command = [ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0"]
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
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"FFmpeg could not decode {path}: {detail or 'unknown error'}")
        decoded.seek(0)
        audio, sr = sf.read(decoded, dtype="float32", always_2d=channels > 1)
    return audio, int(sr)


def load_stereo(
    path: str, target_sr: int = SAMPLE_RATE, max_seconds: float | None = None
) -> np.ndarray:
    """Load an audio file as stereo float32 at *target_sr*.

    Uses FFmpeg for MP4-container audio such as ALAC, and soundfile for
    the rest, falling back to librosa for formats soundfile cannot read.
    All paths resample to *target_sr*.

    A decoded track costs about 21 MB per minute (stereo float32 at
    44.1 kHz), so *max_seconds* refuses longer files before decoding
    them: soundfile reads the length from the header, and FFmpeg and the
    librosa fallback decode at most a second past the limit.

    Args:
        path: Audio file path.
        target_sr: Output sample rate in Hz.
        max_seconds: Longest track to load, or ``None`` for no limit.

    Returns:
        ``(frames, 2)`` float32 audio.

    Raises:
        RuntimeError: ``soundfile.LibsndfileError`` when neither decoder can
            read the file, or FFmpeg's error for MP4-container audio.
        TrackTooLongError: If the track is longer than *max_seconds*.
    """
    if Path(path).suffix.lower() in FFMPEG_FORMATS:
        duration = None if max_seconds is None else max_seconds + 1.0
        decoded, rate = load_with_ffmpeg(
            path, channels=2, sample_rate=target_sr, max_seconds=duration
        )
        if max_seconds is not None and len(decoded) > max_seconds * rate:
            raise TrackTooLongError(path, len(decoded) / rate)
        return to_stereo(decoded)
    try:
        with sf.SoundFile(path) as source:
            seconds = source.frames / source.samplerate
            if max_seconds is not None and seconds > max_seconds:
                raise TrackTooLongError(path, seconds)
            audio = source.read(dtype="float32", always_2d=True)
            sr = source.samplerate
    except TrackTooLongError:
        raise
    except Exception:
        import librosa

        duration = None if max_seconds is None else max_seconds + 1.0
        audio, sr = librosa.load(path, sr=None, mono=False, duration=duration)
        if max_seconds is not None and audio.shape[-1] > max_seconds * sr:
            raise TrackTooLongError(path, audio.shape[-1] / sr) from None
        audio = audio.T if audio.ndim == 2 else audio
    audio = to_stereo(audio)
    if int(sr) != target_sr:
        import librosa

        audio = librosa.resample(audio, orig_sr=int(sr), target_sr=target_sr, axis=0)
    return to_stereo(audio)
