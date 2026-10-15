"""In-memory audio preparation and cue analysis for radio playback."""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess  # nosec B404 -- audio processing uses fixed FFmpeg argv, no shell
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from autodj.stereo import SAFE_FFMPEG_INPUT, SAMPLE_RATE, TrackTooLongError, load_stereo

if TYPE_CHECKING:
    from autodj.indexer import IndexEntry

logger = logging.getLogger(__name__)

PREPARATION_VERSION = "loudness-v1"
TARGET_LUFS = {"music": -28.0, "liner": -25.0}
MIN_GAIN_DB, MAX_GAIN_DB = -60.0, 18.0
_FFMPEG_TIMEOUT_S = 600
_READ_CHUNK_BYTES = 1 << 16
VOICE_FILTER = (
    "highpass=f=70,acompressor=threshold=0.063095734:ratio=3:attack=10:release=180:"
    "makeup=1:knee=4:link=maximum:detection=rms"
)
MASTER_FILTER = (
    "volume=6dB,acompressor=threshold=0.16:ratio=2:attack=30:release=400:"
    "makeup=1:knee=6:link=maximum:detection=rms,volume=9.01dB,"
    "aresample=176400,alimiter=limit=0.87096359:attack=5:release=100:"
    "level=0:latency=1,aresample=44100"
)

_LUFS = re.compile(r"\bI:\s*(-?\d+(?:\.\d+)?)\s* LUFS")


def _run_ffmpeg(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(  # nosec B603 -- fixed FFmpeg argv, no shell
            command,
            capture_output=True,
            check=False,
            timeout=_FFMPEG_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffmpeg timed out after {_FFMPEG_TIMEOUT_S}s: {command[0]}") from exc


def _stream_ffmpeg(command: list[str]) -> tuple[int, bytearray, bytes]:
    """Run FFmpeg and gather its stdout in one growing, writable buffer.

    subprocess.run returns stdout as immutable bytes, and writable samples
    built from that need a second full-size copy: two whole decoded tracks
    in memory at once.  A bytearray can back the samples directly.
    """
    proc = subprocess.Popen(  # nosec B603 -- fixed FFmpeg argv, no shell
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    stdout, stderr = proc.stdout, proc.stderr
    if stdout is None or stderr is None:  # pragma: no cover - PIPE always gives both
        proc.kill()
        raise RuntimeError("ffmpeg pipes unavailable")
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        proc.kill()

    errors: list[bytes] = []
    # Drain stderr alongside stdout so a chatty decode cannot fill its pipe and stall.
    drain = threading.Thread(target=lambda: errors.append(stderr.read()), daemon=True)
    timer = threading.Timer(_FFMPEG_TIMEOUT_S, expire)
    timer.daemon = True
    drain.start()
    timer.start()
    output = bytearray()
    try:
        while data := stdout.read(_READ_CHUNK_BYTES):
            output += data
        returncode = proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        drain.join()
        stdout.close()
        stderr.close()
    if timed_out.is_set():
        raise RuntimeError(f"ffmpeg timed out after {_FFMPEG_TIMEOUT_S}s: {command[0]}")
    return returncode, output, b"".join(errors)


def _kind(kind: str) -> float:
    try:
        return TARGET_LUFS[kind]
    except KeyError as exc:
        raise ValueError("kind must be 'music' or 'liner'") from exc


def analyze_loudness(source: Path, kind: str = "music") -> dict[str, object]:
    """Measure integrated source loudness for caller-owned metadata caching."""
    _kind(kind)
    source = Path(source)
    before = source.stat()
    filters = _processing_filters(kind)
    result = _run_ffmpeg(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-nostdin",
            *SAFE_FFMPEG_INPUT,
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            f"{filters},ebur128=peak=none",
            "-f",
            "null",
            "-",
        ],
    )
    log = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"ffmpeg loudness measurement failed: {log[-1000:]}")
    values = _LUFS.findall(log)
    if not values:
        raise ValueError(f"silent or invalid audio: {source}")
    lufs = float(values[-1])
    if not math.isfinite(lufs) or lufs <= -65:
        raise ValueError(f"silent or invalid audio: {source}")
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"source changed during loudness measurement: {source}")
    return _measurement(source, kind, lufs, before)


def loudness_matches(measurement: dict | None, source: Path, kind: str = "music") -> bool:
    """Return whether cached measurement belongs to the current source/settings."""
    if not isinstance(measurement, dict):
        return False
    try:
        target = _kind(kind)
        stat = Path(source).stat()
        lufs, gain = float(measurement["lufs"]), float(measurement["gain_db"])
        return (
            measurement["version"] == PREPARATION_VERSION
            and measurement["kind"] == kind
            and measurement["size"] == stat.st_size
            and measurement["mtime_ns"] == stat.st_mtime_ns
            and math.isfinite(lufs)
            and lufs > -65
            and math.isfinite(gain)
            and MIN_GAIN_DB <= gain <= MAX_GAIN_DB
            and math.isclose(
                gain,
                min(target - lufs, MAX_GAIN_DB) if kind == "liner" else target - lufs,
                abs_tol=0.01,
            )
        )
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return False


def load_processed_audio(
    source: Path, measurement: dict, kind: str = "music", max_seconds: float | None = None
) -> np.ndarray:
    """Decode the original source and apply its cached constant loudness gain."""
    _kind(kind)
    if not loudness_matches(measurement, source, kind):
        raise ValueError("stale or invalid loudness measurement")
    source = Path(source)
    gain_db = float(measurement["gain_db"])
    filters = f"{_processing_filters(kind)},volume={gain_db:.9f}dB"
    command = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner",
        "-v",
        "error",
        "-nostdin",
        *SAFE_FFMPEG_INPUT,
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-af",
        filters,
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "2",
    ]
    if max_seconds is not None:
        command += ["-t", str(max_seconds + 1.0)]
    command += ["-f", "f32le", "pipe:1"]
    returncode, output, stderr = _stream_ffmpeg(command)
    detail = stderr.decode(errors="replace").strip()
    if returncode:
        raise RuntimeError(f"ffmpeg audio processing failed for {source}: {detail[-1000:]}")
    if detail:
        logger.warning(
            "Decoded %s with warnings:\n%s", source, detail, extra={"track": str(source)}
        )
    # The bytearray is writable, so the samples use it in place with no copy.
    audio = np.frombuffer(output, dtype="<f4").reshape(-1, 2)
    if max_seconds is not None and len(audio) > max_seconds * SAMPLE_RATE:
        raise TrackTooLongError(str(source), len(audio) / SAMPLE_RATE)
    after = source.stat()
    if (after.st_size, after.st_mtime_ns) != (measurement["size"], measurement["mtime_ns"]):
        raise RuntimeError(f"source changed during audio decode: {source}")
    if not len(audio) or not np.isfinite(audio).all() or not np.any(audio):
        raise ValueError("empty, silent or invalid processed audio")
    return audio


def _processing_filters(kind: str) -> str:
    filters = "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo"
    if kind == "liner":
        filters += f",{VOICE_FILTER}"
    return filters


def _measurement(source: Path, kind: str, lufs: float, before: os.stat_result) -> dict:
    """Validate an integrated loudness and return the cacheable measurement."""
    if not math.isfinite(lufs) or lufs <= -65:
        raise ValueError(f"silent or invalid audio: {source}")
    gain_db = _kind(kind) - lufs
    if kind == "liner":
        gain_db = min(gain_db, MAX_GAIN_DB)
    if not MIN_GAIN_DB <= gain_db <= MAX_GAIN_DB:
        raise ValueError(f"required gain outside safe range: {source}")
    return {
        "version": PREPARATION_VERSION,
        "kind": kind,
        "size": before.st_size,
        "mtime_ns": before.st_mtime_ns,
        "lufs": lufs,
        "gain_db": gain_db,
    }


def load_and_measure_audio(
    source: Path,
    kind: str = "music",
    max_seconds: float | None = None,
    *,
    input_format: str | None = None,
) -> tuple[np.ndarray, dict[str, object]]:
    """Decode and measure *source* in one FFmpeg run, for a loudness cache miss.

    The same as :func:`analyze_loudness` followed by
    :func:`load_processed_audio`, with one decode instead of two: the
    ``ebur128`` filter passes the audio through unchanged while it
    measures, and the gain is applied here once the integrated loudness
    is known.  FFmpeg's ``volume`` filter multiplies float samples by the
    float32 gain too, so the samples are the same.  *input_format* names the
    demuxer, so FFmpeg probes nothing (an uploaded break, #88).

    Returns:
        ``(audio, measurement)``: the processed audio and the measurement
        :func:`load_processed_audio` takes, for the caller to cache.
    """
    _kind(kind)
    source = Path(source)
    before = source.stat()
    # metadata=1 moves ebur128's per-frame lines to verbose; its summary stays at info.
    filters = f"{_processing_filters(kind)},ebur128=peak=none:metadata=1"
    command = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-nostdin",
        "-loglevel",
        "level+info",
        *SAFE_FFMPEG_INPUT,
        *(("-f", input_format) if input_format else ()),
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-af",
        filters,
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "2",
    ]
    if max_seconds is not None:
        command += ["-t", str(max_seconds + 1.0)]
    command += ["-f", "f32le", "pipe:1"]
    returncode, output, stderr = _stream_ffmpeg(command)
    log = stderr.decode(errors="replace")
    # What load_processed_audio's "-v error" would have shown.
    detail = "\n".join(
        line for line in log.splitlines() if re.search(r"\[(error|fatal|panic)\]", line)
    ).strip()
    if returncode:
        raise RuntimeError(f"ffmpeg audio processing failed for {source}: {detail[-1000:]}")
    values = _LUFS.findall(log)
    if not values:
        raise ValueError(f"silent or invalid audio: {source}")
    measurement = _measurement(source, kind, float(values[-1]), before)
    if detail:
        logger.warning(
            "Decoded %s with warnings:\n%s", source, detail, extra={"track": str(source)}
        )
    audio = np.frombuffer(output, dtype="<f4").reshape(-1, 2)
    if max_seconds is not None and len(audio) > max_seconds * SAMPLE_RATE:
        raise TrackTooLongError(str(source), len(audio) / SAMPLE_RATE)
    after = source.stat()
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise RuntimeError(f"source changed during audio decode: {source}")
    # The gain load_processed_audio hands FFmpeg (to nine decimals), as float32.
    gain = np.float32(10 ** (float(f"{float(measurement['gain_db']):.9f}") / 20))
    audio *= gain
    if not len(audio) or not np.isfinite(audio).all() or not np.any(audio):
        raise ValueError("empty, silent or invalid processed audio")
    return audio, measurement


def probe_seconds(source: Path) -> float | None:
    """Return a header duration suitable for refusing an over-long file.

    MP3 without a known bitrate mode, raw AAC, and damaged MPEG headers may
    only provide estimates. Return ``None`` for those so callers use the
    bounded decode instead of rejecting a playable track from an estimate.
    """
    from mutagen.mp3 import MPEGInfo

    from autodj.audio_meta import _open_mutagen

    info = getattr(_open_mutagen(source), "info", None)
    if isinstance(info, MPEGInfo):
        from mutagen.mp3 import BitrateMode

        if info.sketchy or info.bitrate_mode == BitrateMode.UNKNOWN:
            return None
    elif source.suffix.lower() in {".mp3", ".mp2", ".aac"}:
        return None
    seconds = getattr(info, "length", 0.0)
    if math.isfinite(seconds) and seconds > 0:
        return float(seconds)
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        result = subprocess.run(  # nosec B603 -- fixed ffprobe argv, no shell
            [
                ffprobe,
                "-v",
                "error",
                *SAFE_FFMPEG_INPUT,
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(source),
            ],
            capture_output=True,
            check=False,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        seconds = float(result.stdout.decode(errors="replace").strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def load_for_analysis(source: Path, max_seconds: float | None = None) -> np.ndarray:
    """Decode *source* as stereo at ``SAMPLE_RATE`` for :func:`analyze_basic`.

    A track longer than *max_seconds* is refused from its header length
    before anything is decoded.

    Raises:
        TrackTooLongError: If the track is longer than *max_seconds*.
    """
    if max_seconds is not None:
        seconds = probe_seconds(source)
        if seconds is not None and seconds > max_seconds + 1.0:
            raise TrackTooLongError(str(source), seconds)
    return load_stereo(str(source), SAMPLE_RATE, max_seconds)


def analyze_basic(
    source: Path,
    kind: str = "music",
    max_seconds: float | None = None,
    audio: np.ndarray | None = None,
) -> tuple[IndexEntry, dict[str, float | None]]:
    """Return basic library metadata and conservative timing suggestions.

    Full beat-grid and embedding analysis remain separate background work.

    Args:
        source: Audio file.
        kind: ``"music"`` or ``"liner"``.
        max_seconds: Longest track to analyse, or ``None`` for no limit.
            The analysis holds the whole track in memory several times over
            (a three-hour mix needed about 10 GB), so a longer track is
            refused from its header length before anything is decoded, and
            the decode itself stops a second past the limit.
        audio: The track already decoded by :func:`load_for_analysis`, so
            the caller can reuse it; decoded here when ``None``.

    Raises:
        TrackTooLongError: If the track is longer than *max_seconds*.
    """
    from autodj.audio_meta import load_lrc_for, read_file_tags
    from autodj.dj_meta import detect_intro_outro, detect_intro_start
    from autodj.indexer import IndexEntry
    from autodj.vocals import first_vocal_s, sung_spans

    if kind not in {"music", "liner"}:
        raise ValueError("kind must be 'music' or 'liner'")
    source = Path(source)
    if audio is None:
        audio = load_for_analysis(source, max_seconds)
    length = len(audio) / SAMPLE_RATE
    mono = audio.mean(axis=1)
    tags = read_file_tags(source)
    energy = float(np.sqrt(np.mean(mono.astype(np.float64) ** 2)))
    start = detect_intro_start(audio, SAMPLE_RATE)
    cue_mono = mono
    window = SAMPLE_RATE // 2
    remainder = len(cue_mono) % window
    if remainder:
        cue_mono = np.pad(cue_mono, (0, window - remainder))
    detected_intro, detected_outro = detect_intro_outro(cue_mono, SAMPLE_RATE)
    audible = np.flatnonzero(np.max(np.abs(audio), axis=1) >= 10 ** (-60 / 20))
    end = (int(audible[-1]) + 1) / SAMPLE_RATE if len(audible) else 0.0
    lines = load_lrc_for(source)
    spans = sung_spans(lines)
    if kind == "liner":
        segue = _suggest_liner_segue(mono, SAMPLE_RATE)
        if segue is None or segue > end:
            segue = end
    else:
        segue = _suggest_segue(mono, SAMPLE_RATE)
    points = {
        "start": start,
        "end": end,
        "segue": segue,
        "intro": first_vocal_s(spans) if spans else (detected_intro or None),
        "outro": spans[-1][1] if spans else detected_outro if detected_outro < length else None,
    }
    entry = IndexEntry(
        path=str(source),
        title=tags.title or source.stem,
        artist=tags.artist,
        album=tags.album,
        genre=tags.genre,
        bpm=tags.bpm,
        year=tags.year,
        length=length,
        energy=energy,
        key=-1,
        mode=-1,
        tempo_confidence=0.0,
        embedded_at=source.stat().st_mtime,
        size=source.stat().st_size,
    )
    return entry, points


def _suggest_liner_segue(audio: np.ndarray, sample_rate: int) -> float | None:
    """Find a sustained final content drop in a short liner, never an interior pause."""
    window = max(1, round(sample_rate * 0.05))
    if len(audio) < window * 3:
        return None
    samples = np.nan_to_num(np.asarray(audio, dtype=np.float64), copy=False)
    bins = [samples[start : start + window] for start in range(0, len(samples), window)]
    rms = np.asarray([np.sqrt(np.mean(block * block)) for block in bins], dtype=np.float64)
    duration = len(samples) / sample_rate
    centers = (np.arange(len(rms)) + 0.5) * window / sample_rate
    body = rms[centers < max(0.0, duration - 0.5)]
    if len(body) == 0:
        return None
    reference = float(np.median(body))
    if not math.isfinite(reference) or reference <= 1e-6:
        return None

    below = rms <= reference * 10 ** (-10 / 20)
    index = len(below) - 1
    while index >= 0 and below[index]:
        index -= 1
    first = index + 1
    tail_duration = len(samples) - first * window
    point = first * window / sample_rate
    if tail_duration < sample_rate * 0.15 or point <= 0:
        return None
    return point if point < duration else None


def _suggest_segue(audio: np.ndarray, sample_rate: int) -> float | None:
    """Suggest only a sustained final energy drop, never a vocal cue."""
    window = sample_rate
    tail_start = max(0, len(audio) - 60 * sample_rate)
    tail = audio[tail_start:]
    if len(tail) < 6 * window:
        return None
    rms = np.asarray(
        [
            np.sqrt(np.mean(tail[start : start + window].astype(np.float64) ** 2) + 1e-12)
            for start in range(0, len(tail), window)
        ]
    )
    body = rms[:-5]
    reference = float(np.median(body))
    if reference <= 1e-6:
        return None
    threshold = reference * 10 ** (-10 / 20)
    index = len(rms) - 1
    while index >= 0 and rms[index] <= threshold:
        index -= 1
    point = (tail_start + (index + 1) * window) / sample_rate
    quiet_duration = len(tail) - (index + 1) * window
    return (
        point
        if quiet_duration >= 5 * sample_rate and 0 < point < len(audio) / sample_rate
        else None
    )
