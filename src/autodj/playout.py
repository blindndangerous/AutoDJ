"""Saved-point music handoff planning and outgoing-only rendering."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from .stereo import SAMPLE_RATE, audio_buffer


@dataclass(frozen=True)
class Handoff:
    """One current-track chunk and the tail to overlay after music entry.

    ``segue_s``, ``incoming_s``, ``liner_s``, and ``fade_start_s`` use the
    outgoing file's time axis. ``next_overlay[0]`` aligns with ``incoming_s``.
    *audio* and *next_overlay* are consecutive views of *full_audio*, so a
    caller that adds the incoming track into *next_overlay* in place has
    the whole render without copying the track again.
    """

    audio: np.ndarray
    next_overlay: np.ndarray
    next_start_offset: int
    start_offset: int
    segue_s: float
    incoming_s: float
    liner_s: float | None
    fade_start_s: float
    full_audio: np.ndarray
    overlay_gain: np.ndarray | None = None


#: How long the music takes to drop under a ducked voice, and to come back.
DUCK_RAMP_SECONDS = 0.25


def duck_envelope(
    frames: int, spans: tuple[tuple[int, int], ...], gain: float, ramp: int
) -> np.ndarray | None:
    """A per-frame music gain: *gain* inside each span, 1 outside, with *ramp*-frame slopes.

    Returns ``None`` when nothing is ducked (no spans, or a gain of 1).
    The slopes sit outside the spans, so the music is fully down before a
    voice starts and stays down until it ends.
    """
    if frames <= 0 or gain >= 1.0 or not spans:
        return None
    envelope = np.ones(frames, dtype=np.float32)
    ramp = max(1, ramp)
    for first, last in spans:
        first, last = max(0, first), min(frames, last)
        if last <= first:
            continue
        envelope[first:last] = np.minimum(envelope[first:last], gain)
        slope = np.linspace(1.0, gain, ramp + 1, dtype=np.float32)[1:]
        lead_from = max(0, first - ramp)
        lead = slope[ramp - (first - lead_from) :]
        envelope[lead_from:first] = np.minimum(envelope[lead_from:first], lead)
        tail_to = min(frames, last + ramp)
        tail = slope[::-1][: tail_to - last]
        envelope[last:tail_to] = np.minimum(envelope[last:tail_to], tail)
    return envelope


def _point(points: dict[str, Any], name: str) -> float | None:
    value = points.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} point must be a finite number or None")
    return float(value)


def _frames(seconds: float, name: str) -> int:
    frames = seconds * SAMPLE_RATE
    if not math.isfinite(frames):
        raise ValueError(f"{name} is too large")
    return round(frames)


def liner_release_frames(main_frames: int, intro_frames: int, overlap_start: float | None) -> int:
    """Return incoming-music delay from a liner-chain start, including negative cues."""
    if overlap_start is None:
        return main_frames
    if overlap_start < 0:
        requested = _frames(-overlap_start, "overlap_start")
        return (
            -min(requested, intro_frames - main_frames)
            if main_frames <= intro_frames
            else main_frames - intro_frames
        )
    if overlap_start == 0:
        return max(0, main_frames - intro_frames)
    return max(
        main_frames - intro_frames, min(main_frames, _frames(overlap_start, "overlap_start"))
    )


def liner_main_frames(first: int, frames: int, segue: float | None) -> int:
    """Speech/main duration within a trimmed clip; no Segue uses its full length."""
    return frames if segue is None else min(frames, max(0, round(segue * SAMPLE_RATE) - first))


def liner_chain_timing(
    clips: Sequence[tuple[int, int, float | None, bool]], incoming_intro: float
) -> tuple[int, float, float | None]:
    """Return chain main frames, music delay, and outro limit (both in seconds).

    Each clip supplies trimmed frames, main frames, Overlap start, and whether
    it may start over the outgoing outro. Audio and catalog access stay outside.
    """
    frames = main_frames = fit_main_frames = 0
    release = 0.0
    negative, zero = [], []
    ordinary = False
    outro_limit = None
    for length, main, overlap, allow_outro in clips:
        main_frames = frames + main
        if overlap is not None and overlap < 0:
            negative.append((frames, overlap, main))
        elif overlap == 0:
            zero.append((frames, main))
            fit_main_frames = max(fit_main_frames, main_frames)
        else:
            ordinary = True
            delay = main / SAMPLE_RATE if overlap is None else min(overlap, main / SAMPLE_RATE)
            if delay > 0 or overlap is None:
                release = max(release, frames / SAMPLE_RATE + delay)
        if not allow_outro and outro_limit is None:
            outro_limit = frames / SAMPLE_RATE
        frames += length
    if negative:
        intro_frames = round(incoming_intro * SAMPLE_RATE)
        constraints = [
            offset / SAMPLE_RATE + liner_release_frames(main, intro_frames, cue) / SAMPLE_RATE
            for offset, cue, main in negative
        ]
        constraints.extend((offset + main - intro_frames) / SAMPLE_RATE for offset, main in zero)
        if ordinary:
            constraints.append(release)
        if clips[0][2] is None or clips[0][2] >= 0:
            constraints.append(0.0)
        release = max(constraints)
    elif fit_main_frames:
        release = max(release, fit_main_frames / SAMPLE_RATE - incoming_intro)
    # Every clip's main content must finish before the incoming vocals.
    return main_frames, max(release, main_frames / SAMPLE_RATE - incoming_intro), outro_limit


def liner_chain_placement(
    start_frame: int,
    segue_frame: int,
    safe_outro_frame: int,
    main_frames: int,
    intro_frames: int,
    overlap_start: float | None,
    outro_lead_limit: float | None = None,
) -> tuple[int, int]:
    """Place a cue-trimmed liner chain and incoming track without audio arrays."""
    if outro_lead_limit is not None:
        safe_outro_frame = max(
            safe_outro_frame, segue_frame - _frames(outro_lead_limit, "outro_lead_limit")
        )
    if overlap_start is not None and overlap_start < 0:
        release = liner_release_frames(main_frames, intro_frames, overlap_start)
        liner_start = max(safe_outro_frame, segue_frame - release)
        incoming_frame = max(segue_frame, liner_start + release)
    elif overlap_start == 0:
        liner_start = max(
            safe_outro_frame, min(segue_frame, segue_frame + intro_frames - main_frames)
        )
        incoming_frame = max(segue_frame, liner_start + main_frames - intro_frames)
    else:
        release = liner_release_frames(main_frames, intro_frames, overlap_start)
        liner_start = max(safe_outro_frame, segue_frame - release)
        incoming_frame = max(segue_frame, liner_start + release)
    return liner_start, max(incoming_frame, start_frame)


def _audio(value: np.ndarray | None, name: str, *, optional: bool = False) -> np.ndarray | None:
    if value is None and optional:
        return None
    if (
        not isinstance(value, np.ndarray)
        or value.ndim != 2
        or value.shape[1] != 2
        or not len(value)
    ):
        raise ValueError(f"{name} must be a non-empty stereo array")
    if not np.isfinite(value).all():
        raise ValueError(f"{name} must contain only finite samples")
    return value


def render_handoff(
    outgoing: np.ndarray,
    outgoing_points: dict[str, Any],
    incoming_points: dict[str, Any],
    *,
    start_offset: int = 0,
    liner_start_offset: int = 0,
    fade_seconds: float = 1.5,
    liner: np.ndarray | None = None,
    overlap_start: float | None = None,
    liner_segue: float | None = None,
    outro_lead_limit: float | None = 0.0,
    duck_spans: tuple[tuple[float, float], ...] = (),
    duck_db: float = 0.0,
    liner_duck_spans: tuple[tuple[float, float], ...] = (),
    liner_duck_db: float = 0.0,
) -> Handoff:
    """Plan a handoff from saved points without rendering incoming music.

    The current chunk ends at the actual incoming music entry. Any outgoing
    fade tail and the remaining voice tail are returned as ``next_overlay``.
    Point times are seconds in their source files; offsets are sample frames.
    ``liner_start_offset`` excludes music already played by the preceding render.
    ``liner_segue`` is relative to the retained liner audio, excluding its tail.
    Blank overlap uses that segue; zero permits fitting under the incoming intro.
    A negative overlap starts the liner after music, capped by the remaining intro.
    ``outro_lead_limit`` bounds how early a liner chain may start before the song
    segue: zero for an ID, None for voices, or an ID's offset after preceding voices.
    """
    checked = _audio(outgoing, "outgoing")
    assert checked is not None
    outgoing = checked
    liner = _audio(liner, "liner", optional=True)
    if not isinstance(outgoing_points, dict) or not isinstance(incoming_points, dict):
        raise ValueError("point sets must be dictionaries")
    if type(start_offset) is not int or start_offset < 0:
        raise ValueError("start_offset must be a non-negative sample offset")
    if type(liner_start_offset) is not int or liner_start_offset < 0:
        raise ValueError("liner_start_offset must be a non-negative sample offset")
    if (
        isinstance(fade_seconds, bool)
        or not isinstance(fade_seconds, (int, float))
        or not math.isfinite(fade_seconds)
        or fade_seconds < 0
    ):
        raise ValueError("fade_seconds must be a non-negative finite number")
    for name, value in (("overlap_start", overlap_start), ("outro_lead_limit", outro_lead_limit)):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or (name == "outro_lead_limit" and value < 0)
            or (name == "overlap_start" and value < -15)
        ):
            raise ValueError(
                "overlap_start must be a finite number of at least -15 or None"
                if name == "overlap_start"
                else "outro_lead_limit must be a non-negative finite number or None"
            )

    frame_count = len(outgoing)
    start_frame = min(
        _frames(max(0.0, _point(outgoing_points, "start") or 0.0), "start"), frame_count
    )
    end_point = _point(outgoing_points, "end")
    end_frame = min(
        max(start_frame, _frames(end_point, "end")) if end_point is not None else frame_count,
        frame_count,
    )
    segue_point = _point(outgoing_points, "segue")
    if segue_point is None:
        segue_point = _point(outgoing_points, "outro")
    if segue_point is None:
        segue_point = end_frame / SAMPLE_RATE
    segue_frame = min(max(_frames(segue_point, "segue"), start_frame), end_frame)

    incoming_start = max(0.0, _point(incoming_points, "start") or 0.0)
    incoming_intro = _point(incoming_points, "intro")
    incoming_intro_frames = _frames(
        max(0.0, (incoming_intro - incoming_start) if incoming_intro is not None else 0.0), "intro"
    )
    next_start_offset = _frames(incoming_start, "start")

    liner_frames = len(liner) if liner is not None else 0
    main_end = _point({"liner_segue": liner_segue}, "liner_segue")
    main_frames = (
        liner_frames
        if main_end is None
        else min(liner_frames, max(0, _frames(main_end, "liner_segue")))
    )
    if liner is None:
        liner_start = None
        incoming_frame = segue_frame
    else:
        outro_point = _point(outgoing_points, "outro")
        safe_outro = max(
            start_frame,
            start_offset,
            liner_start_offset,
            min(
                segue_frame,
                _frames(outro_point, "outro") if outro_point is not None else segue_frame,
            ),
        )
        liner_start, incoming_frame = liner_chain_placement(
            start_frame,
            segue_frame,
            safe_outro,
            main_frames,
            incoming_intro_frames,
            overlap_start,
            outro_lead_limit,
        )

    fade_frames = _frames(float(fade_seconds), "fade_seconds")
    fade_start = max(start_frame, segue_frame - fade_frames // 3)
    fade_end = min(end_frame, fade_start + fade_frames)
    fade_start_s = fade_start / SAMPLE_RATE
    incoming_frame = max(incoming_frame, start_frame)
    chunk_start = min(max(start_offset, start_frame), incoming_frame)

    def outgoing_segment(first: int, last: int) -> np.ndarray:
        # A long track's chunk goes to a mapped file, not ordinary memory.
        result = audio_buffer(max(0, last - first))
        source_end = min(last, end_frame)
        copy_end = min(source_end, fade_start)
        if copy_end > first:
            result[: copy_end - first] = outgoing[first:copy_end]
        fade_first = max(first, fade_start)
        fade_last = min(source_end, fade_end)
        if fade_last > fade_first:
            count = fade_end - fade_start
            time = np.linspace(0.0, 1.0, count, dtype=np.float64)
            # Keep the approved curve used by the listening examples.
            gain = (1.0 - np.expm1(3.0 * time) / np.expm1(3.0)).astype(np.float32)
            if count == 1:
                gain[0] = 0.0
            result[fade_first - first : fade_last - first] = (
                outgoing[fade_first:fade_last]
                * gain[fade_first - fade_start : fade_last - fade_start, None]
            )
        return result

    overlay_end = max(
        incoming_frame,
        fade_end,
        (liner_start + liner_frames) if liner_start is not None else incoming_frame,
    )
    if liner_start is not None:
        for spans, depth in ((duck_spans, duck_db), (liner_duck_spans, liner_duck_db)):
            if depth < 0 and spans:
                # Keep the incoming head through release so the next chunk cannot jump up.
                overlay_end = max(
                    overlay_end,
                    liner_start
                    + max(_frames(last, "duck span") for _, last in spans)
                    + _frames(DUCK_RAMP_SECONDS, "duck ramp"),
                )
    # One buffer for the chunk and the overlay, split at the incoming frame.
    full = outgoing_segment(chunk_start, overlay_end)
    split = max(0, incoming_frame - chunk_start)
    envelope = None
    if liner is not None and liner_start is not None:
        for spans, depth in ((duck_spans, duck_db), (liner_duck_spans, liner_duck_db)):
            if not math.isfinite(depth):
                raise ValueError("duck_db must be finite")
            if not spans or depth >= 0:
                continue
            gain = duck_envelope(
                len(full),
                tuple(
                    (
                        liner_start - chunk_start + _frames(first, "duck span"),
                        liner_start - chunk_start + _frames(last, "duck span"),
                    )
                    for first, last in spans
                ),
                float(10 ** (depth / 20)),
                _frames(DUCK_RAMP_SECONDS, "duck ramp"),
            )
            if gain is not None:
                # Adjacent voice/ID ramps use the deeper duck, never a doubled drop.
                envelope = gain if envelope is None else np.minimum(envelope, gain)
        if envelope is not None:
            full *= envelope[:, None]
    if liner is not None and liner_start is not None:
        first = max(chunk_start, liner_start)
        last = min(overlay_end, liner_start + liner_frames)
        if last > first:
            full[first - chunk_start : last - chunk_start] += liner[
                first - liner_start : last - liner_start
            ]

    return Handoff(
        audio=full[:split],
        next_overlay=full[split:],
        next_start_offset=next_start_offset,
        start_offset=chunk_start,
        segue_s=segue_frame / SAMPLE_RATE,
        incoming_s=incoming_frame / SAMPLE_RATE,
        liner_s=None if liner_start is None else liner_start / SAMPLE_RATE,
        fade_start_s=fade_start_s,
        full_audio=full,
        overlay_gain=None if envelope is None else envelope[split:],
    )
