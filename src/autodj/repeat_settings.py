"""Validated, independent repeat-rule settings."""

from __future__ import annotations

import logging
from collections.abc import Mapping
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

logger = logging.getLogger(__name__)


def ignore_unknown_repeat_keys(data: object, section: str = "playback.repeat_rules") -> object:
    """Drop unknown config keys while leaving recognized values for strict validation."""
    if not isinstance(data, Mapping):
        return data
    known = set(RepeatSettings.model_fields)
    unknown = set(data) - known
    if unknown:
        logger.warning("Ignoring unknown config keys in [%s]: %s", section, sorted(unknown))
    cleaned = {key: value for key, value in data.items() if key in known}
    for name in RULE_NAMES:
        rule = cleaned.get(name)
        if isinstance(rule, Mapping):
            rule_keys = set(RepeatRule.model_fields)
            unknown = set(rule) - rule_keys
            if unknown:
                logger.warning(
                    "Ignoring unknown config keys in [%s.%s]: %s", section, name, sorted(unknown)
                )
            cleaned[name] = {key: value for key, value in rule.items() if key in rule_keys}
    return cleaned


def removed_saved_repeat_keys(data: object) -> dict[str, tuple[str, ...]]:
    """Group removed repeat keys in saved web state by their playback section."""
    if not isinstance(data, dict) or not isinstance(data.get("playback"), dict):
        return {}
    playback = data["playback"]
    removed: dict[str, tuple[str, ...]] = {}
    direct = tuple(sorted(REMOVED_REPEAT_KEYS.intersection(playback)))
    if direct:
        removed["playback"] = direct
    rules = playback.get("repeat_rules")
    nested = (
        tuple(sorted(REMOVED_REPEAT_KEYS.intersection(rules))) if isinstance(rules, dict) else ()
    )
    if nested:
        removed["playback.repeat_rules"] = nested
    return removed


#: The rules that each have a track count and a minute limit.
RULE_NAMES = ("artist", "related_artist", "album", "track", "title", "related_track")


class RepeatRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracks: Annotated[int, Field(strict=True, ge=0, le=100000)] = 0
    minutes: Annotated[float, Field(strict=True, ge=0, le=10080, allow_inf_nan=False)] = 0.0


class RepeatSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artist: RepeatRule = RepeatRule(tracks=8)
    related_artist: RepeatRule = RepeatRule(tracks=8)
    album: RepeatRule = RepeatRule(tracks=0)
    track: RepeatRule = RepeatRule(tracks=50)
    title: RepeatRule = RepeatRule(tracks=0)
    related_track: RepeatRule = RepeatRule(tracks=20)
    use_related_artists: StrictBool = False
    use_related_tracks: StrictBool = False

    @model_validator(mode="before")
    @classmethod
    def related_artist_follows_artist(cls, data: object) -> object:
        """Settings saved before related artists had their own limits used the artist rule's."""
        if isinstance(data, Mapping) and "related_artist" not in data and "artist" in data:
            return {**data, "related_artist": data["artist"]}
        return data

    @model_validator(mode="after")
    def limit_group_windows(self) -> RepeatSettings:
        for name in ("artist", "related_artist", "album", "title", "related_track"):
            if getattr(self, name).tracks > 5000:
                raise ValueError(f"{name} tracks must be between 0 and 5000")
        return self
