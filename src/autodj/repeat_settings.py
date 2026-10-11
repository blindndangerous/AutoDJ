"""Validated, independent repeat-rule settings."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

REMOVED_REPEAT_KEYS = frozenset(
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

    @model_validator(mode="after")
    def limit_group_windows(self) -> RepeatSettings:
        for name in ("artist", "album", "title"):
            if getattr(self, name).tracks > 5000:
                raise ValueError(f"{name} tracks must be between 0 and 5000")
        return self
