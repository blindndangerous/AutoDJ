"""Offline source preparation for smooth radio playback."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess  # nosec B404 -- audio preparation uses only fixed FFmpeg argv, no shell
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import soundfile as sf

from autodj.stereo import SAMPLE_RATE, load_stereo

if TYPE_CHECKING:
    from autodj.indexer import IndexEntry

PREPARATION_VERSION = "type-b-v1"
HEADROOM_DB = -6.0
PREPARED_LINER_GAIN = 10 ** (-1.5 / 20)
VOICE_FILTER = (
    "highpass=f=70,acompressor=threshold=0.063095734:ratio=3:attack=10:release=180:"
    "makeup=1:knee=4:link=maximum:detection=rms"
)
MUSIC_FILTER = "dynaudnorm=f=500:g=15:p=0.75:m=1.584893192:r=0.089125094:n=1:b=1:t=0"
MASTER_FILTER = (
    "volume=6dB,acompressor=threshold=0.16:ratio=2:attack=30:release=400:"
    "makeup=1:knee=6:link=maximum:detection=rms,volume=9.01dB,"
    "aresample=176400,alimiter=limit=0.87096359:attack=5:release=100:"
    "level=0:latency=1,aresample=44100"
)

_LOUDNESS = re.compile(r'\{\s*"input_i".*?\}', re.S)
_TIMEOUT_S = 600


def _run(command: list[str | Path]) -> str:
    args = [str(part) for part in command]
    try:
        result = subprocess.run(  # nosec B603 -- fixed FFmpeg argv, no shell
            args,
            capture_output=True,
            timeout=_TIMEOUT_S,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"audio processing failed: {args[0]}") from exc
    log = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {log[-2000:]}")
    return log


def _measure(path: Path, filters: str = "") -> float:
    audio_filter = f"{filters}," if filters else ""
    log = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            path,
            "-af",
            f"{audio_filter}loudnorm=I=-24:TP=-1:LRA=7:print_format=json",
            "-f",
            "null",
            "-",
        ]
    )
    blocks = _LOUDNESS.findall(log)
    if not blocks:
        raise ValueError(f"could not measure loudness: {path}")
    value = json.loads(blocks[-1]).get("input_i")
    try:
        lufs = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid loudness measurement: {path}") from exc
    if not math.isfinite(lufs):
        raise ValueError(f"silent or invalid audio: {path}")
    return lufs


def _validate_audio(path: Path, frames: int | None = None) -> int:
    info = sf.info(path)
    if (
        info.samplerate != SAMPLE_RATE
        or info.channels != 2
        or info.frames <= 0
        or (frames is not None and info.frames != frames)
    ):
        raise ValueError(f"invalid prepared audio: {path}")
    audible = False
    with sf.SoundFile(path) as audio:
        while True:
            block = audio.read(262_144, dtype="float32", always_2d=True)
            if not len(block):
                break
            if not np.isfinite(block).all():
                raise ValueError(f"non-finite audio: {path}")
            audible |= bool(np.any(block))
    if not audible:
        raise ValueError(f"silent audio: {path}")
    return info.frames


def _cache_key(source: Path, kind: str) -> str:
    stat = source.stat()
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    identity = (
        f"{PREPARATION_VERSION}\0{kind}\0{stat.st_size}\0{stat.st_mtime_ns}\0{digest.hexdigest()}"
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def prepare_source(source: Path, cache_dir: Path, kind: str = "music") -> Path:
    """Decode and prepare one immutable source into a deterministic WAV cache."""
    if kind not in ("music", "liner"):
        raise ValueError("kind must be 'music' or 'liner'")
    source = Path(source)
    cache_dir = Path(cache_dir)
    if not source.is_file():
        raise FileNotFoundError(source)
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / f"{_cache_key(source, kind)}.wav"
    if destination.is_file():
        try:
            _validate_audio(destination)
            return destination
        except (OSError, RuntimeError):
            pass
        except ValueError:
            pass
        destination.unlink(missing_ok=True)

    temp_paths: list[Path] = []

    def temporary() -> Path:
        fd, name = tempfile.mkstemp(dir=cache_dir, suffix=".wav", prefix=".prepare-")
        os.close(fd)
        path = Path(name)
        temp_paths.append(path)
        return path

    decoded, staged, final = temporary(), temporary(), temporary()
    try:
        _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-v",
                "error",
                "-y",
                "-i",
                source,
                "-vn",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                "2",
                "-c:a",
                "pcm_f32le",
                decoded,
            ]
        )
        frames = _validate_audio(decoded)

        voice = kind == "liner"
        target = -17.5 if voice else -22.0
        voice_filter = VOICE_FILTER if voice else ""
        gain = target - _measure(decoded, voice_filter)
        if not voice:
            _run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-nostats",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    decoded,
                    "-af",
                    f"volume={gain:.9f}dB,{MUSIC_FILTER}",
                    "-ar",
                    str(SAMPLE_RATE),
                    "-ac",
                    "2",
                    "-c:a",
                    "pcm_f32le",
                    staged,
                ]
            )
            correction = target - _measure(staged)
            input_path = staged
            filters = f"volume={correction:.9f}dB,volume={HEADROOM_DB}dB"
        else:
            input_path = decoded
            filters = f"{VOICE_FILTER},volume={gain:.9f}dB,volume={HEADROOM_DB}dB"
        _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-v",
                "error",
                "-y",
                "-i",
                input_path,
                "-af",
                filters,
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                "2",
                "-c:a",
                "pcm_f32le",
                final,
            ]
        )
        _validate_audio(final, frames)
        os.replace(final, destination)
        return destination
    finally:
        for path in temp_paths:
            path.unlink(missing_ok=True)


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
