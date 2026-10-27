"""Shared bounded librosa features and DJ metadata for one audio input."""

from __future__ import annotations

import math
import warnings
from typing import TYPE_CHECKING, Annotated

import librosa
import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from autodj.processing import TrackWindows
from autodj.stereo import ANALYSIS_WINDOW_SECONDS, LONG_TRACK_SECONDS, SAMPLE_RATE

ANALYSIS_VERSION = "librosa16-44100+head-tail-5m+shared-beats/1"
if TYPE_CHECKING:
    from autodj.dj_meta import DjMeta
Finite = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Feature = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=0)]


class AudioAnalysis(BaseModel):
    """Versioned feature vector and metadata computed from decoded audio."""

    model_config = ConfigDict(extra="forbid", strict=True)
    version: str = Field(max_length=64)
    features: list[Feature] = Field(min_length=16, max_length=16)
    energy: Finite = Field(ge=0)
    key: int = Field(ge=-1, le=11)
    mode: int = Field(ge=-1, le=1)
    bpm: Finite = Field(ge=0, le=400)
    tempo_confidence: Finite = Field(ge=0, le=1)

    @field_validator("version")
    @classmethod
    def check_version(cls, value: str) -> str:
        if value != ANALYSIS_VERSION:
            raise ValueError("unsupported audio analysis version")
        return value

    @model_validator(mode="after")
    def check_metadata(self) -> AudioAnalysis:
        if (self.key == -1) != (self.mode == -1):
            raise ValueError("key and mode must both be known or unknown")
        if not math.isclose(self.energy, self.features[0], abs_tol=1e-6):
            raise ValueError("energy must match feature zero")
        return self


def _mono(audio: np.ndarray) -> np.ndarray:
    if audio.ndim == 2:
        return audio.mean(axis=1).astype(np.float32)
    return np.asarray(audio, dtype=np.float32)


def analyse_audio_features(
    audio: np.ndarray | TrackWindows,
    sr: int,
    intro_end_s: float | None = None,
    outro_start_s: float | None = None,
) -> tuple[AudioAnalysis, DjMeta]:
    """Compute the 16-vector and DJ metadata, sharing beat tracking exactly once per window.

    For inputs over 15 minutes, head and tail windows (5 minutes each) are
    analyzed separately. Feature means combine those sampled windows; tempo
    confidence uses their combined observed and expected beat counts.
    """
    from autodj.dj_meta import analyse_audio
    from autodj.indexer import _estimate_key_from_chroma

    if sr <= 0:
        raise ValueError("audio and sample rate must be non-empty and positive")
    if isinstance(audio, TrackWindows):
        head, tail, tail_start_s = _mono(audio.head), _mono(audio.tail), audio.tail_start_s
    else:
        duration = len(audio) / max(sr, 1)
        if duration > LONG_TRACK_SECONDS:
            n = round(ANALYSIS_WINDOW_SECONDS * sr)
            head, tail = _mono(audio[:n]), _mono(audio[-n:])
            tail_start_s = (len(audio) - len(tail)) / sr
        else:
            head, tail, tail_start_s = _mono(audio), np.empty(0, dtype=np.float32), 0.0
    if not len(head):
        raise ValueError("audio and sample rate must be non-empty and positive")
    if sr != SAMPLE_RATE:
        head = librosa.resample(head, orig_sr=sr, target_sr=SAMPLE_RATE)
        if len(tail):
            tail = librosa.resample(tail, orig_sr=sr, target_sr=SAMPLE_RATE)
        sr = SAMPLE_RATE
    windows = [head] + ([tail] if len(tail) else [])
    feature_parts: list[list[float]] = []
    tempos: list[float] = []
    beat_counts = 0
    beat_parts: list[list[float]] = []
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*n_fft.*too large.*", category=UserWarning)
        warnings.filterwarnings("ignore", message=".*empty frequency set.*", category=UserWarning)
        for part in windows:
            rms = float(np.mean(librosa.feature.rms(y=part))) if len(part) else 0.0
            centroid = (
                float(np.mean(librosa.feature.spectral_centroid(y=part, sr=sr)))
                if len(part)
                else 0.0
            )
            zcr = float(np.mean(librosa.feature.zero_crossing_rate(y=part))) if len(part) else 0.0
            chroma = (
                np.mean(librosa.feature.chroma_stft(y=part, sr=sr), axis=1)
                if len(part)
                else np.zeros(12)
            )
            onset = (
                float(np.mean(librosa.onset.onset_strength(y=part, sr=sr))) if len(part) else 0.0
            )
            feature_parts.append([rms, centroid, zcr, *map(float, chroma), onset])
            try:
                if len(part) >= sr:
                    tempo, frames = librosa.beat.beat_track(y=part, sr=sr)
                else:
                    tempo, frames = 0.0, np.empty(0, dtype=int)
            except Exception:
                tempo, frames = 0.0, np.empty(0, dtype=int)
            tempo = float(np.atleast_1d(tempo)[0])
            if math.isfinite(tempo) and tempo > 0:
                tempos.append(tempo)
            times = [float(t) for t in librosa.frames_to_time(frames, sr=sr)] if len(frames) else []
            beat_parts.append(times)
            beat_counts += len(times)
    features = np.mean(np.asarray(feature_parts, dtype=np.float64), axis=0).astype(np.float32)
    key, mode = _estimate_key_from_chroma(features[3:15])
    bpm = float(np.median(tempos)) if tempos else 0.0
    analyzed_duration = sum(len(p) for p in windows) / max(sr, 1)
    expected = bpm / 60 * analyzed_duration
    confidence = min(1.0, beat_counts / max(1.0, expected)) if bpm else 0.0
    model = AudioAnalysis(
        version=ANALYSIS_VERSION,
        features=features.tolist(),
        energy=float(features[0]),
        key=key,
        mode=mode,
        bpm=bpm,
        tempo_confidence=confidence,
    )
    if len(tail):
        from autodj.dj_meta import analyse_windows

        meta = analyse_windows(
            head,
            tail,
            tail_start_s,
            sr,
            intro_end_s,
            outro_start_s,
            beats=(beat_parts[0], beat_parts[1]),
        )
    else:
        meta = analyse_audio(head, sr, intro_end_s, outro_start_s, beats=beat_parts[0])
    return model, meta
