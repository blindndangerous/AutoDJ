"""Offline source preparation for smooth radio playback."""

from __future__ import annotations

import math
import re
import shutil
import subprocess  # nosec B404 -- audio processing uses fixed FFmpeg argv, no shell
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from autodj.stereo import SAMPLE_RATE, TrackTooLongError, load_stereo

if TYPE_CHECKING:
    from autodj.indexer import IndexEntry

PREPARATION_VERSION = "loudness-v1"
TARGET_LUFS = {"music": -28.0, "liner": -25.0}
MIN_GAIN_DB, MAX_GAIN_DB = -60.0, 18.0
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
    filters = "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo"
    if kind == "liner":
        filters += f",{VOICE_FILTER}"
    result = subprocess.run(
        [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-nostats", "-i", str(source),
         "-vn", "-af", f"{filters},ebur128=peak=none", "-f", "null", "-"],
        capture_output=True, check=False,
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
    gain_db = _kind(kind) - lufs
    if not MIN_GAIN_DB <= gain_db <= MAX_GAIN_DB:
        raise ValueError(f"required gain outside safe range: {source}")
    return {"version": PREPARATION_VERSION, "kind": kind, "size": before.st_size,
            "mtime_ns": before.st_mtime_ns, "lufs": lufs, "gain_db": gain_db}


def loudness_matches(measurement: dict | None, source: Path, kind: str = "music") -> bool:
    """Return whether cached measurement belongs to the current source/settings."""
    if not isinstance(measurement, dict):
        return False
    try:
        target = _kind(kind)
        stat = Path(source).stat()
        lufs, gain = float(measurement["lufs"]), float(measurement["gain_db"])
        return (measurement["version"] == PREPARATION_VERSION and measurement["kind"] == kind
                and measurement["size"] == stat.st_size and measurement["mtime_ns"] == stat.st_mtime_ns
                and math.isfinite(lufs) and lufs > -65 and math.isfinite(gain)
                and MIN_GAIN_DB <= gain <= MAX_GAIN_DB and math.isclose(gain, target - lufs, abs_tol=0.01))
    except (KeyError, TypeError, ValueError, OSError):
        return False


def load_processed_audio(source: Path, measurement: dict, kind: str = "music",
                         max_seconds: float | None = None) -> np.ndarray:
    """Decode the original source and apply its cached constant loudness gain."""
    _kind(kind)
    if not loudness_matches(measurement, source, kind):
        raise ValueError("stale or invalid loudness measurement")
    source = Path(source)
    gain_db = float(measurement["gain_db"])
    if kind == "music":
        audio = load_stereo(str(source), max_seconds=max_seconds)
        audio *= np.float32(10 ** (gain_db / 20))
    else:
        command = [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-v", "error", "-i", str(source),
                   "-vn", "-af", f"{VOICE_FILTER},volume={gain_db:.9f}dB",
                   "-ar", str(SAMPLE_RATE), "-ac", "2"]
        if max_seconds is not None:
            command += ["-t", str(max_seconds + 1.0)]
        command += ["-f", "f32le", "pipe:1"]
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError(f"ffmpeg liner processing failed: {result.stderr.decode(errors='replace')[-1000:]}")
        audio = np.frombuffer(result.stdout, dtype="<f4").reshape(-1, 2).copy()
        if max_seconds is not None and len(audio) > max_seconds * SAMPLE_RATE:
            raise TrackTooLongError(str(source), len(audio) / SAMPLE_RATE)
    after = source.stat()
    if (after.st_size, after.st_mtime_ns) != (measurement["size"], measurement["mtime_ns"]):
        raise RuntimeError(f"source changed during audio decode: {source}")
    if not len(audio) or not np.isfinite(audio).all() or not np.any(audio):
        raise ValueError("empty, silent or invalid processed audio")
    return audio


def analyze_basic(source: Path, kind: str = "music") -> tuple[IndexEntry, dict[str, float | None]]:
    """Return basic library metadata and conservative timing suggestions.

    Full beat-grid and embedding analysis remain separate background work.
    """
    from autodj.audio_meta import load_lrc_for, read_file_tags
    from autodj.dj_meta import detect_intro_outro, detect_intro_start
    from autodj.indexer import IndexEntry
    from autodj.vocals import first_vocal_s, sung_spans

    if kind not in {"music", "liner"}:
        raise ValueError("kind must be 'music' or 'liner'")
    source = Path(source)
    audio = load_stereo(str(source))
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
    bins = len(tail) // window
    if bins < 6:
        return None
    rms = np.sqrt(
        np.mean(
            tail[: bins * window].reshape(bins, window).astype(np.float64) ** 2,
            axis=1,
        )
        + 1e-12
    )
    body = rms[:-5]
    reference = float(np.median(body))
    if reference <= 1e-6:
        return None
    threshold = reference * 10 ** (-10 / 20)
    for index in range(max(0, bins - 5)):
        if np.all(rms[index : index + 5] <= threshold):
            point = (tail_start + index * window) / sample_rate
            return point if 0 < point < len(audio) / sample_rate else None
    return None
