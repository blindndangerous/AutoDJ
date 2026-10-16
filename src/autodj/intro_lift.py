"""Peak-safe gain for a quiet opening, returning to unity within eight seconds."""

from __future__ import annotations

import math
import subprocess  # nosec B404 -- fixed FFmpeg argv, no shell
from pathlib import Path

import numpy as np
import soundfile as sf

from autodj.stereo import SAFE_FFMPEG_INPUT

INTRO_SECONDS = 8.0
HOLD_SECONDS = 1.0
# Match the Stereo Tool output limiter ceiling; browser playback gets the same cap.
CEILING = 10 ** (-1.8 / 20)


def measure_source_intro(path: Path, sample_rate: int, track_rms: float) -> float:
    """Decode only the first eight seconds of a source during library analysis."""
    try:
        with sf.SoundFile(path) as source:
            opening = source.read(
                round(INTRO_SECONDS * source.samplerate), dtype="float32", always_2d=True
            )
            return intro_gain_db(opening, source.samplerate, track_rms, 6.0)
    except (OSError, RuntimeError):
        pass  # MP4/ALAC and formats unavailable to libsndfile use FFmpeg.
    result = subprocess.run(  # nosec B603 B607 -- fixed FFmpeg argv, no shell
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            *SAFE_FFMPEG_INPUT,
            "-i",
            str(path),
            "-t",
            str(INTRO_SECONDS),
            "-vn",
            "-ac",
            "2",
            "-ar",
            str(sample_rate),
            "-f",
            "f32le",
            "pipe:1",
        ],
        capture_output=True,
        timeout=45,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode or not result.stdout:
        raise RuntimeError(f"could not measure intro: {path}")
    samples = np.frombuffer(result.stdout, dtype=np.float32).reshape(-1, 2)
    return intro_gain_db(samples, sample_rate, track_rms, 6.0)


def intro_gain_db(audio: np.ndarray, sample_rate: int, track_rms: float, max_db: float) -> float:
    """Compare the opening RMS with the track RMS and respect every opening peak."""
    if (
        not math.isfinite(max_db)
        or not math.isfinite(track_rms)
        or max_db <= 0
        or track_rms <= 1e-6
        or sample_rate <= 0
    ):
        return 0.0
    opening = np.asarray(audio[: min(len(audio), round(3 * sample_rate))], dtype=np.float64)
    if not len(opening) or not np.isfinite(opening).all():
        return 0.0
    # The track's energy is the RMS of its mono mix, so measure the opening the same way.
    if opening.ndim == 2:
        opening = opening.mean(axis=1)
    rms = float(np.sqrt(np.mean(opening * opening)))
    if rms < 10 ** (-45 / 20):
        return 0.0
    wanted = min(max_db, max(0.0, 20 * math.log10(track_rms / rms)))
    if wanted <= 0:
        return 0.0
    head = np.asarray(audio[: min(len(audio), round(INTRO_SECONDS * sample_rate))])
    if not np.isfinite(head).all():
        return 0.0
    if head.ndim == 2:
        peaks = np.max(np.abs(head), axis=1)
    else:
        peaks = np.abs(head)
    weights = intro_weights(len(peaks), sample_rate)
    eligible = (weights > 0) & (peaks > 0)
    if np.any(eligible):
        limits = 20 * np.log10(CEILING / peaks[eligible]) / weights[eligible]
        wanted = min(wanted, max(0.0, float(np.min(limits))))
    # Round down, so the stored lift never exceeds the peak limit.
    return math.floor(wanted * 1000) / 1000


def intro_weights(frames: int, sample_rate: int) -> np.ndarray:
    """Pure hold-then-linear-release envelope at absolute track positions."""
    seconds = np.arange(frames, dtype=np.float64) / sample_rate
    return np.clip((INTRO_SECONDS - seconds) / (INTRO_SECONDS - HOLD_SECONDS), 0.0, 1.0)


def apply_intro_lift(
    audio: np.ndarray, sample_rate: int, lift_db: float, *, in_place: bool = False
) -> np.ndarray:
    """Apply a cached lift to decoded audio, bounded again for this decode.

    With *in_place* the caller's fresh decode is changed and returned;
    otherwise a copy is, so a whole long track is not copied for a lift on
    its first eight seconds.
    """
    if not math.isfinite(lift_db) or lift_db <= 0:
        return audio
    frames = min(len(audio), round(INTRO_SECONDS * sample_rate))
    if not frames:
        return audio
    head = audio[:frames]
    # Preparation can alter peaks. Compute the maximum safe lift on this copy.
    peak = np.max(np.abs(head), axis=1)
    weights = intro_weights(frames, sample_rate)
    eligible = (weights > 0) & (peak > 0)
    if np.any(eligible):
        lift_db = min(
            lift_db,
            max(0.0, float(np.min(20 * np.log10(CEILING / peak[eligible]) / weights[eligible]))),
        )
    if lift_db <= 0:
        return audio
    result = audio if in_place else audio.copy()
    result[:frames] *= (10 ** (lift_db * weights / 20))[:, None]
    return result
