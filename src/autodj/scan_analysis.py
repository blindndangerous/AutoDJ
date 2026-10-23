"""Bounded scan results and the same per-song measurements used by local scans."""

from __future__ import annotations

import logging
import math
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from autodj.processing import MAX_GAIN_DB, MIN_GAIN_DB, PREPARATION_VERSION, TARGET_LUFS

SCAN_ANALYSIS_VERSION = "1"
MAX_SCAN_SECONDS = 24 * 60 * 60
logger = logging.getLogger(__name__)

Finite = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Time = Annotated[Finite, Field(ge=0)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ScanPoints(_StrictModel):
    start: Finite | None = None
    end: Finite | None = None
    intro: Finite | None = None
    outro: Finite | None = None
    segue: Finite | None = None


def normalize_basic_points(points: dict[str, Any], length: float) -> dict[str, Any]:
    """Keep imperfect automatic cue suggestions from rejecting a playable file."""
    length = max(float(length), 0.001)
    start = points.get("start")
    end = points.get("end")
    start = 0.0 if start is None else float(start)
    end = length if end is None else float(end)
    if not 0 <= start < end <= length:
        start, end = 0.0, length
    result: dict[str, Any] = {"start": start, "end": end}
    for field in ("intro", "outro", "segue"):
        value = points.get(field)
        if value is None:
            result[field] = None
            continue
        value = float(value)
        result[field] = value if start <= value <= end else None
    return result


class ScanLoudness(_StrictModel):
    version: str = Field(max_length=40)
    kind: Literal["music"]
    lufs: Finite = Field(gt=-65)
    gain_db: Finite = Field(ge=MIN_GAIN_DB, le=MAX_GAIN_DB)

    @model_validator(mode="after")
    def check_measurement(self) -> Self:
        if self.version != PREPARATION_VERSION or not math.isclose(
            self.gain_db, TARGET_LUFS["music"] - self.lufs, abs_tol=0.01
        ):
            raise ValueError("invalid loudness version or gain")
        return self


class ScanCue(_StrictModel):
    time_s: Time
    type: str = Field(max_length=40)
    label: str = Field(default="", max_length=200)
    source: Literal["auto"] = "auto"
    color: str = Field(default="", pattern=r"^(#[0-9a-fA-F]{6})?$")


class ScanDj(_StrictModel):
    intro_start_s: Time | None
    intro_end_s: Time
    outro_start_s: Time
    beats: list[Time] = Field(max_length=20000)
    analysed: StrictBool
    cues: list[ScanCue] = Field(max_length=4096)


class ScanResult(_StrictModel):
    length: Finite = Field(gt=0, le=MAX_SCAN_SECONDS)
    energy: Finite = Field(ge=0)
    points: ScanPoints
    intro_lift_db: Finite = Field(ge=0, le=6)
    loudness: ScanLoudness | None
    dj: ScanDj | None

    @model_validator(mode="after")
    def check_times(self) -> Self:
        self.points = ScanPoints.model_validate(
            normalize_basic_points(self.points.model_dump(), self.length)
        )
        if self.dj is not None:
            dj = self.dj
            if not dj.analysed:
                raise ValueError("DJ analysis must be complete")
            times = [*(dj.beats), *(cue.time_s for cue in dj.cues), dj.outro_start_s]
            if dj.intro_start_s is not None:
                times.append(dj.intro_start_s)
            times.append(dj.intro_end_s)
            if any(value > self.length for value in times):
                raise ValueError("DJ time exceeds song length")
            if any(a >= b for a, b in zip(dj.beats, dj.beats[1:], strict=False)):
                raise ValueError("beats must increase")
            if any(a.time_s > b.time_s for a, b in zip(dj.cues, dj.cues[1:], strict=False)):
                raise ValueError("DJ cues must be ordered")
        return self


def analyse_scan(path: Path, options: dict[str, Any]) -> dict[str, Any]:
    """Measure downloaded music, retaining the server's scan settings and cue hints."""
    from autodj.dj_meta import analyse_decoded
    from autodj.intro_lift import measure_source_intro
    from autodj.processing import analyze_basic, analyze_loudness, load_for_analysis
    from autodj.stereo import SAMPLE_RATE

    audio = load_for_analysis(path)
    entry, points = analyze_basic(
        path, audio=audio, music_fade_drop_db=options["music_fade_drop_db"]
    )
    points.update(options.get("lyrics", {}))
    for name, value in options.get("suggestions", {}).items():
        if points.get(name) is None:
            points[name] = value
    points = normalize_basic_points(points, entry.length)
    effective = {**points, **options.get("saved_points", {})}
    dj = None
    if options["dj"]:
        dj = asdict(
            analyse_decoded(
                audio,
                SAMPLE_RATE,
                intro_end_s=effective.get("intro") or 0.0,
                outro_start_s=effective.get("outro") or entry.length,
            )
        )
    del audio
    try:
        lift = measure_source_intro(path, SAMPLE_RATE, entry.energy)
    except (OSError, RuntimeError, ValueError):
        lift = 0.0
    loudness = None
    if options["loudness"]:
        try:
            measured = analyze_loudness(path)
            loudness = {key: measured[key] for key in ("version", "kind", "lufs", "gain_db")}
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("Loudness analysis failed for %s: %s", path, exc)
    result = {
        "length": entry.length,
        "energy": entry.energy,
        "points": points,
        "intro_lift_db": lift,
        "loudness": loudness,
        "dj": dj,
    }
    return ScanResult.model_validate(result).model_dump()
