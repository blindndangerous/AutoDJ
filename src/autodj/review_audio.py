"""Shorten rendered programme audio without changing its actual joins.

All frame ranges are half-open on the full rendered programme timeline.
Input may be a disk-backed array; only retained output is copied into memory.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from autodj.stereo import reserve_audio


@dataclass(frozen=True)
class ReviewSettings:
    head_seconds: float = 10
    tail_seconds: float = 10
    clip_threshold_seconds: float = 300
    tone_seconds: float = 0.25

    def __post_init__(self) -> None:
        for name, maximum in (
            ("head_seconds", 3600),
            ("tail_seconds", 3600),
            ("clip_threshold_seconds", 86400),
            ("tone_seconds", 10),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < value <= maximum
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} must be finite, greater than 0 and at most {maximum}")


@dataclass(frozen=True)
class ReviewSpan:
    id: str
    label: str
    kind: str
    start_frame: int
    end_frame: int
    keep_complete: bool = False


@dataclass(frozen=True)
class ReviewPiece:
    programme_start: int | None
    programme_end: int | None
    preview_start: int
    preview_end: int
    kind: str


@dataclass(frozen=True)
class ReviewAudio:
    audio: np.ndarray
    pieces: tuple[ReviewPiece, ...]
    effective_range: tuple[int, int]
    item_positions: dict[str, tuple[int, int]]


def _range(value: tuple[int, int], frames: int) -> tuple[int, int]:
    if (
        len(value) != 2
        or any(isinstance(n, bool) or not isinstance(n, int) for n in value)
        or not 0 <= value[0] < value[1] <= frames
    ):
        raise ValueError("Frame range must contain integers with 0 <= start < end <= audio length")
    return value


def _union(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for first, last in sorted(ranges):
        if merged and first <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def _tone(frames: int, rate: int, channels: int) -> np.ndarray:
    # Two quiet notes and a short envelope distinguish an edit from a real segue.
    t = np.arange(frames, dtype=np.float64) / rate
    frequency = np.where(t < frames / rate / 2, min(880, rate / 5), min(1320, rate / 3))
    tone = 0.06 * np.sin(2 * np.pi * frequency * t)
    ramp = min(max(1, round(0.005 * rate)), frames // 2)
    if ramp:
        tone[:ramp] *= np.linspace(0, 1, ramp)
        tone[-ramp:] *= np.linspace(1, 0, ramp)
    return np.repeat(tone.astype(np.float32)[:, None], channels, axis=1)


def shorten_review(
    audio: np.ndarray,
    spans: Sequence[ReviewSpan],
    *,
    sample_rate: int,
    settings: ReviewSettings | None = None,
    protected: Sequence[tuple[int, int]] = (),
    selection: tuple[int, int] | None = None,
    output_path: Path | None = None,
) -> ReviewAudio:
    """Retain item edges and required joins, inserting a tone at each removed gap.

    With ``output_path``, retained PCM is disk-backed. The caller owns that
    temporary file and must remove it after consumption or any failure.
    """
    if (
        isinstance(sample_rate, bool)
        or not isinstance(sample_rate, int)
        or not 0 < sample_rate <= 384_000
    ):
        raise ValueError("Sample rate must be a positive integer at most 384000")
    if (
        not isinstance(audio, np.ndarray)
        or audio.ndim != 2
        or audio.shape[1] not in (1, 2)
        or not len(audio)
        or not np.issubdtype(audio.dtype, np.floating)
    ):
        raise ValueError("Audio must be a nonempty mono or stereo floating point array")
    settings = settings or ReviewSettings()
    first, last = _range(selection if selection is not None else (0, len(audio)), len(audio))
    required = [_range(interval, len(audio)) for interval in protected]
    ids: set[str] = set()
    complete: set[str] = set()
    for span in spans:
        _range((span.start_frame, span.end_frame), len(audio))
        if not span.id or span.id in ids:
            raise ValueError("Review occurrence IDs must be nonempty and unique")
        ids.add(span.id)
        if span.keep_complete or (
            span.kind != "song"
            and span.end_frame - span.start_frame <= settings.clip_threshold_seconds * sample_rate
        ):
            complete.add(span.id)
            required.append((span.start_frame, span.end_frame))
    # A clip crossing one edge may expose another crossing the expanded edge.
    # ponytail: bounded item scan; interval indexing only if very large reviews warrant it.
    while True:
        expanded = (first, last)
        for left, right in required:
            if left < last and right > first:
                first, last = min(first, left), max(last, right)
        if expanded == (first, last):
            break
    windows = [
        (max(first, left), min(last, right))
        for left, right in required
        if left < last and right > first
    ]
    head = max(1, round(settings.head_seconds * sample_rate))
    tail = max(1, round(settings.tail_seconds * sample_rate))
    for span in spans:
        left, right = max(first, span.start_frame), min(last, span.end_frame)
        if left >= right:
            continue
        if span.id in complete:
            windows.append((left, right))
        else:
            windows.extend(((left, min(right, left + head)), (max(left, right - tail), right)))
    intervals = _union(windows or [(first, last)])
    tone_frames = max(1, round(settings.tone_seconds * sample_rate))
    frames = sum(right - left for left, right in intervals) + tone_frames * (len(intervals) - 1)
    shape = (frames, audio.shape[1])
    if output_path is not None:
        reserve_audio(frames * audio.shape[1] * 4)
    output = (
        np.empty(shape, dtype=np.float32)
        if output_path is None
        else np.memmap(output_path, dtype=np.float32, mode="w+", shape=shape)
    )
    pieces: list[ReviewPiece] = []
    cursor = 0
    for index, (left, right) in enumerate(intervals):
        if index:
            output[cursor : cursor + tone_frames] = _tone(tone_frames, sample_rate, audio.shape[1])
            pieces.append(ReviewPiece(None, None, cursor, cursor + tone_frames, "tone"))
            cursor += tone_frames
        output[cursor : cursor + right - left] = audio[left:right]
        pieces.append(ReviewPiece(left, right, cursor, cursor + right - left, "audio"))
        cursor += right - left
    positions: dict[str, tuple[int, int]] = {}
    for span in spans:
        intersections = []
        for piece in pieces:
            if piece.programme_start is None or piece.programme_end is None:
                continue
            if span.start_frame < piece.programme_end and span.end_frame > piece.programme_start:
                intersections.append(
                    (
                        piece.preview_start + max(span.start_frame - piece.programme_start, 0),
                        piece.preview_end - max(piece.programme_end - span.end_frame, 0),
                    )
                )
        if intersections:
            positions[span.id] = (intersections[0][0], intersections[-1][1])
    if isinstance(output, np.memmap):
        output.flush()
    return ReviewAudio(output, tuple(pieces), (first, last), positions)
