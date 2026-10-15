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
* What is decoded is stereo at :data:`~autodj.stereo.SAMPLE_RATE`, read in
  blocks (libsndfile) or chunks (FFmpeg) against a byte budget of
  ``(max_seconds + 1) * 44100 * 8`` bytes, whatever the header says, into
  one buffer the samples then use in place.  Peak memory for one break is
  about 0.35 MB per second of the limit (106 MB at the default 300 s).
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
    if suffix in _SOUNDFILE_FORMATS or not _looks_like(path, suffix):
        # libsndfile reads every one of its own types; for MP4 and AAC the
        # first bytes must say so.  Either way no other tool is needed to
        # say this is not audio.
        raise BreakAudioError("it is not audio AutoDJ can play")
    rate, channels, seconds = _ffprobe(path, demuxer, runner)
    _check(rate, channels, seconds, max_seconds, path)
    return "ffmpeg"


def check_playable(
    path: Path,
    suffix: str | None = None,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Refuse a file that is not audio AutoDJ can play (a voice liner, #323).

    The header check of :func:`check_break_audio` without a break's rate,
    channel and length limits.  *suffix* names the type when *path* is a
    staging file whose own name ends otherwise.  An MP4 or AAC file must
    start as one; its stream is read only where FFmpeg's ffprobe is
    installed, since the browser plays it without FFmpeg.

    Raises:
        BreakAudioError: The type is not a liner type, or the contents are
            not audio of that type.
    """
    path = Path(path)
    suffix = (suffix or path.suffix).lower()
    demuxer = DEMUXERS.get(suffix)
    if demuxer is None:
        raise BreakAudioError("it is not audio AutoDJ can play")
    try:
        info = sf.info(str(path))
    except (RuntimeError, OSError):
        info = None
    if info is not None:
        if info.format not in _SOUNDFILE_FORMATS.get(suffix, frozenset()):
            raise BreakAudioError(f"its contents are not {suffix} audio")
        return
    if suffix in _SOUNDFILE_FORMATS or not _looks_like(path, suffix):
        raise BreakAudioError("it is not audio AutoDJ can play")
    if shutil.which("ffprobe") is not None:
        _ffprobe(path, demuxer, runner)


def _looks_like(path: Path, suffix: str) -> bool:
    """Whether *path* starts as an MP4 (``.m4a``) or an AAC (``.aac``) file does."""
    try:
        with open(path, "rb") as file:
            head = file.read(12)
    except OSError:
        return False
    if suffix == ".m4a":
        return len(head) >= 8 and head[4:8] == b"ftyp"
    if suffix == ".aac":
        # ADTS frame sync, an ADIF header, or an ID3 tag before the frames.
        adts = len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF6) == 0xF0
        return adts or head.startswith((b"ADIF", b"ID3"))
    return False


#: Frames libsndfile reads at a time.
_BLOCK_FRAMES = 65_536


def _read_soundfile(path: Path, max_seconds: float) -> np.ndarray:
    """Read a playout-rate file in blocks, against the same budget as FFmpeg's output.

    A header can understate a file's length (an MP3's Xing frame count, an
    Ogg page), so the frames actually read are counted, not the header's.
    """
    budget = int((max_seconds + 1.0) * SAMPLE_RATE) * _FRAME_BYTES
    output = bytearray()
    try:
        with sf.SoundFile(str(path)) as source:
            channels = source.channels
            while True:
                block = source.read(_BLOCK_FRAMES, dtype="float32", always_2d=True)
                if not len(block):
                    break
                output += to_stereo(block).astype("<f4", copy=False).tobytes()
                if len(output) > budget:
                    raise TrackTooLongError(str(path), len(output) / _FRAME_BYTES / SAMPLE_RATE)
    except TrackTooLongError:
        raise
    except (RuntimeError, OSError, sf.LibsndfileError) as exc:
        raise BreakAudioError("it is not audio AutoDJ can play") from exc
    if not output or channels > MAX_CHANNELS:
        raise BreakAudioError("it is not audio AutoDJ can play")
    audio = np.frombuffer(output, dtype="<f4").reshape(-1, 2)
    if len(audio) > max_seconds * SAMPLE_RATE:
        raise TrackTooLongError(str(path), len(audio) / SAMPLE_RATE)
    return audio


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
        return _read_soundfile(path, max_seconds)
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
    # The bytearray is writable, so the samples use it in place: no second copy.
    audio = np.frombuffer(output, dtype="<f4").reshape(-1, 2)
    if len(audio) > max_seconds * SAMPLE_RATE:
        raise TrackTooLongError(str(path), len(audio) / SAMPLE_RATE)
    return audio


__all__ = [
    "DEMUXERS",
    "MAX_CHANNELS",
    "MAX_RATE",
    "BreakAudioError",
    "check_break_audio",
    "check_playable",
    "decode_break",
    "demuxer_for",
]
