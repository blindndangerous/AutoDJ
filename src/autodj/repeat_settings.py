"""Validated, independent repeat-rule settings."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

LEGACY_REPEAT_FIELDS = frozenset(
    {
        "no_repeat_window",
        "no_repeat_minutes",
        "artist_repeat_window",
        "artist_repeat_minutes",
        "title_repeat_window",
        "title_repeat_minutes",
        "related_artists_file",
        "related_artists_starter",
    }
)


class RepeatRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracks: Annotated[int, Field(strict=True, ge=0, le=100000)] = 0
    minutes: Annotated[float, Field(strict=True, ge=0, le=10080, allow_inf_nan=False)] = 0.0


class RepeatSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artist: RepeatRule = RepeatRule(tracks=8)
    album: RepeatRule = RepeatRule(tracks=0)
    track: RepeatRule = RepeatRule(tracks=50)
    title: RepeatRule = RepeatRule(tracks=0)
    use_related_artists: StrictBool = False
    related_artists_file: StrictStr = ""


def migrate_repeat_settings(
    legacy: Mapping[str, object], base: RepeatSettings | None = None
) -> RepeatSettings:
    """Translate removed playback repeat keys into the current settings model."""
    result = (base or RepeatSettings()).model_dump()
    if isinstance(legacy.get("repeat_rules"), Mapping):
        return RepeatSettings.model_validate(legacy["repeat_rules"])
    else:
        track = _legacy_rule(legacy, "no_repeat_window", "no_repeat_minutes", result["track"])
        if track is not None:
            result["track"] = track
        artist = _legacy_rule(
            legacy, "artist_repeat_window", "artist_repeat_minutes", result["artist"]
        )
        if artist is not None:
            result["artist"] = artist
            result["album"] = artist.copy()
        title_default = {
            "tracks": artist["tracks"] if artist is not None else result["title"]["tracks"],
            "minutes": result["title"]["minutes"],
        }
        title = _legacy_rule(legacy, "title_repeat_window", "title_repeat_minutes", title_default)
        result["title"] = title if title is not None else title_default
    if "related_artists_file" in legacy:
        path = legacy["related_artists_file"]
        if path is not None and not isinstance(path, str):
            raise TypeError("playback.related_artists_file must be a file path")
        result["related_artists_file"] = path or ""
    if "related_artists_starter" in legacy and not isinstance(
        legacy["related_artists_starter"], bool
    ):
        raise TypeError("playback.related_artists_starter must be true or false")
    if legacy.get("related_artists_file") or legacy.get("related_artists_starter"):
        result["use_related_artists"] = True
    return RepeatSettings.model_validate(result)


def _legacy_rule(
    legacy: Mapping[str, object], window_key: str, minutes_key: str, default: dict
) -> dict | None:
    window, minutes = legacy.get(window_key), legacy.get(minutes_key)
    if window is None and minutes is None:
        return None
    if window is not None and (isinstance(window, bool) or not isinstance(window, int)):
        raise TypeError(f"playback.{window_key} must be a whole number of tracks")
    if window is not None and window < 0:
        raise ValueError(f"playback.{window_key} must be a non-negative whole number")
    if minutes is not None and (
        isinstance(minutes, bool)
        or not isinstance(minutes, int | float)
        or not math.isfinite(float(minutes))
        or not 0 <= float(minutes) <= 10080
    ):
        raise ValueError(f"playback.{minutes_key} must be between 0 and 10080 minutes")
    if window is None:
        window = default["tracks"]
    return {
        "tracks": window,
        "minutes": float(minutes if minutes is not None else default["minutes"]),
    }
