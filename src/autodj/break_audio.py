"""Decoding announcer breaks safely (#88).

A break file comes from a program over the network, so it is decoded with
limits a library file does not need:

* The extension names the demuxer FFmpeg must use (:data:`DEMUXERS`), so
  nothing is probed: a text body named ``.mp3`` is not read as an HLS
  playlist, and no protocol but ``file`` and ``pipe`` is allowed.
* The header is read first, and a file over :data:`MAX_RATE` Hz or
  :data:`MAX_CHANNELS` channels, or longer than allowed, is refused before
  any sample is decoded.  A small file can claim hours of eight-channel
  audio at a huge rate; reading it whole would take gigabytes.
* What is decoded is stereo at :data:`~autodj.stereo.SAMPLE_RATE`, and the
  FFmpeg output is read in chunks against a byte budget, so memory stays
  bounded whatever the file says.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 -- fixed FFmpeg argv, no shell
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from autodj.stereo import SAFE_FFMPEG_INPUT, SAMPLE_RATE, TrackTooLongError, to_stereo

#: The highest sample rate a break may have, in Hz.
MAX_RATE = 192_000
#: The most channels a break may have.
MAX_CHANNELS = 2
#: The FFmpeg demuxer for each break file type.
DEMUXERS: dict[str, str] = {
    ".mp3": "mp3",
    ".wav": "wav",
    ".ogg": "ogg",
    ".m4a": "mov",
    ".flac": "flac",
    ".aac": "aac",
}
#: What libsndfile calls each type it reads itself.
_SOUNDFILE_FORMATS: dict[str, frozenset[str]] = {
    ".wav": frozenset({"WAV", "WAVEX", "RF64"}),
    ".flac": frozenset({"FLAC"}),
    ".ogg": frozenset({"OGG"}),
    ".mp3": frozenset({"MP3"}),
}
#: Longest a decode may run, in seconds.
DECODE_TIMEOUT_SECONDS = 120.0
_CHUNK_BYTES = 1024 * 1024
_FRAME_BYTES = 2 * 4  # stereo float32


class BreakAudioError(ValueError):
    """A break file AutoDJ will not decode, with the reason in words."""


def demuxer_for(path: Path) -> str:
    """The FFmpeg demuxer for *path*'s extension.

    Raises:
        BreakAudioError: The extension is not a break type.
    """
    demuxer = DEMUXERS.get(Path(path).suffix.lower())
    if demuxer is None:
        raise BreakAudioError("a break must be one of these types: " + ", ".join(DEMUXERS))
    return demuxer


def _check(rate: int, channels: int, seconds: float | None, max_seconds: float, path: Path) -> None:
    if rate <= 0 or rate > MAX_RATE:
        raise BreakAudioError(f"its sample rate must be at most {MAX_RATE} Hz")
    if channels <= 0 or channels > MAX_CHANNELS:
        raise BreakAudioError("it must be mono or stereo")
    if seconds is not None and seconds > max_seconds:
        raise TrackTooLongError(str(path), seconds)


def _ffprobe(path: Path, demuxer: str, runner: Callable[..., Any]) -> tuple[int, int, float | None]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise BreakAudioError("reading this type needs FFmpeg's ffprobe")
    result = runner(
        [
            ffprobe,
            "-v",
            "error",
            *SAFE_FFMPEG_INPUT,
            "-f",
            demuxer,
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,channels:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    try:
        found = json.loads(result.stdout or b"{}")
        stream = found["streams"][0]
        rate, channels = int(stream["sample_rate"]), int(stream["channels"])
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise BreakAudioError("it is not audio AutoDJ can play") from exc
    try:
        seconds: float | None = float(found.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        seconds = None
    return rate, channels, seconds


def check_break_audio(
    path: Path,
    max_seconds: float,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    """Read *path*'s header and refuse what must not be decoded.

    Returns:
        ``"soundfile"`` when libsndfile can read the file at the playout
        rate, else ``"ffmpeg"``.

    Raises:
        BreakAudioError: The type, rate or channel count is not allowed, or
            the contents do not match the extension.
        TrackTooLongError: The header says it is longer than *max_seconds*.
    """
    path = Path(path)
    demuxer = demuxer_for(path)
    suffix = path.suffix.lower()
    try:
        info = sf.info(str(path))
    except (RuntimeError, OSError):
        info = None
    if info is not None:
        if info.format not in _SOUNDFILE_FORMATS.get(suffix, frozenset()):
            raise BreakAudioError(f"its contents are not {suffix} audio")
        seconds = info.frames / info.samplerate if info.samplerate else None
        _check(int(info.samplerate), int(info.channels), seconds, max_seconds, path)
        return "soundfile" if int(info.samplerate) == SAMPLE_RATE else "ffmpeg"
    rate, channels, seconds = _ffprobe(path, demuxer, runner)
    _check(rate, channels, seconds, max_seconds, path)
    return "ffmpeg"


def decode_break(
    path: Path,
    max_seconds: float,
    *,
    runner: Callable[..., Any] = subprocess.run,
    popen: Callable[..., Any] = subprocess.Popen,
) -> np.ndarray:
    """Decode a break to ``(frames, 2)`` float32 at the playout rate, within limits.

    Raises:
        BreakAudioError: As :func:`check_break_audio`, or the file does not
            decode.
        TrackTooLongError: It is longer than *max_seconds*.
    """
    path = Path(path)
    how = check_break_audio(path, max_seconds, runner=runner)
    if how == "soundfile":
        try:
            audio, _rate = sf.read(str(path), dtype="float32", always_2d=True)
        except (RuntimeError, OSError) as exc:
            raise BreakAudioError("it is not audio AutoDJ can play") from exc
        return to_stereo(audio)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise BreakAudioError("decoding this break needs FFmpeg")
    budget = int((max_seconds + 1.0) * SAMPLE_RATE) * _FRAME_BYTES
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        *SAFE_FFMPEG_INPUT,
        "-f",
        demuxer_for(path),
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "2",
        "-ar",
        str(SAMPLE_RATE),
        "-t",
        f"{max_seconds + 1.0:.3f}",
        "-f",
        "f32le",
        "pipe:1",
    ]
    process = popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = bytearray()
    # A demuxer that stops sending without exiting is killed, which ends the read.
    watchdog = threading.Timer(DECODE_TIMEOUT_SECONDS, process.kill)
    watchdog.daemon = True
    watchdog.start()
    try:
        while True:
            chunk = process.stdout.read(_CHUNK_BYTES)
            if not chunk:
                break
            output += chunk
            if len(output) > budget:
                raise TrackTooLongError(str(path), len(output) / _FRAME_BYTES / SAMPLE_RATE)
        code = process.wait(timeout=DECODE_TIMEOUT_SECONDS)
    finally:
        watchdog.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
    if code != 0 or not output or len(output) % _FRAME_BYTES:
        raise BreakAudioError("it is not audio AutoDJ can play")
    audio = np.frombuffer(bytes(output), dtype="<f4").reshape(-1, 2).copy()
    if len(audio) > max_seconds * SAMPLE_RATE:
        raise TrackTooLongError(str(path), len(audio) / SAMPLE_RATE)
    return audio


__all__ = [
    "DEMUXERS",
    "MAX_CHANNELS",
    "MAX_RATE",
    "BreakAudioError",
    "check_break_audio",
    "decode_break",
    "demuxer_for",
]
