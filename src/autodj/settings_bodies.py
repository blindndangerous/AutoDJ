"""Request bodies of the web settings routes, shared with ``web_state.json``.

``POST /api/playback-settings``, ``/api/djmix``, ``/api/bpm-range`` and the
other settings routes validate their bodies with these models, and
:mod:`autodj.runtime_state` checks every saved setting against the same
field rules, so a value the page cannot set is never restored either.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from autodj.config import MAX_FADE_SECONDS


def validate_playback_choices(values: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` if any choice field in *values* is not allowed.

    The playback setters apply one field at a time, so a check made inside
    them let a request with one bad choice change every field ahead of it and
    then fail.  Callers run this first so a bad request changes nothing.
    ``None`` means "leave unchanged" and is skipped.
    """
    from autodj.config import (
        _validate_key_notation,
        _validate_post_queue_seed,
        _validate_transition_mode,
    )
    from autodj.liners import LINER_PICK_MODES

    checks: tuple[tuple[str, Callable[[str], str]], ...] = (
        ("transition_mode", _validate_transition_mode),
        ("post_queue_seed", _validate_post_queue_seed),
        ("key_notation", _validate_key_notation),
    )
    for key, check in checks:
        if (value := values.get(key)) is not None:
            check(str(value))
    pick_mode = values.get("liners_pick_mode")
    if pick_mode is not None and str(pick_mode) not in LINER_PICK_MODES:
        raise ValueError(
            f"playback.liners_pick_mode must be one of {LINER_PICK_MODES}, got {pick_mode!r}"
        )


FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
"""Float that refuses the ``NaN``/``Infinity`` JSON tokens.

pydantic accepts them by default, but they cannot be re-encoded as JSON:
one non-finite value stored in the player config turns every later
``/api/status`` and WebSocket frame into a 500 or an
unparseable payload until the process restarts.  Reject them at the edge.
"""
NonNegativeFloat = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
FadeSeconds = Annotated[float, Field(ge=0.0, le=MAX_FADE_SECONDS, allow_inf_nan=False)]
"""Crossfade or fade-in length: the range the Settings page and config.toml use."""


class VolumeBody(BaseModel):
    """Request body for POST /api/volume."""

    volume: FiniteFloat


class PresetBody(BaseModel):
    """Request body for POST /api/preset — empty / null name clears."""

    name: str | None = None


class StreamSettingsBody(BaseModel):
    """Request body for POST /api/stream/settings."""

    model_config = ConfigDict(extra="forbid")

    bitrate: Literal[128, 192, 256, 320]


class TransitionBody(BaseModel):
    """Request body for POST /api/transition."""

    effect: str


class DjMixBody(BaseModel):
    """Request body for POST /api/djmix — only set fields are applied.

    Unknown fields are rejected, so a stale client still sending the removed
    ``harmonic_mixing`` switch gets a 422 instead of being silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    harmonic_mode: str | None = None
    beatmatch: bool | None = None
    phrase_align: bool | None = None
    outro_intro_align: bool | None = None
    filter_sweep: bool | None = None
    phrase_bars: Annotated[int, Field(ge=1, le=64)] | None = None

    @field_validator("harmonic_mode")
    @classmethod
    def _check_harmonic_mode(cls, value: str | None) -> str | None:
        """Refuse a harmonic mode the picker does not have."""
        from autodj.dj_meta import HARMONIC_MODES

        if value is None:
            return None
        mode = value.lower()
        if mode not in HARMONIC_MODES:
            raise ValueError(f"harmonic_mode must be one of {HARMONIC_MODES}, got {value!r}")
        return mode


class PlaybackSettingsBody(BaseModel):
    """Request body for POST /api/playback-settings.

    Unknown fields are rejected rather than ignored.  In particular the liner
    root (``playback.liners_folder``) is configuration-only: the liner fetch
    and delete routes resolve names under it, so letting a request move it
    would let any client that can reach this route point those routes at the
    config or index directory.
    """

    model_config = ConfigDict(extra="forbid")

    crossfade_seconds: FadeSeconds | None = None
    fade_in_seconds: FadeSeconds | None = None
    crossfade_eq_duck: bool | None = None
    pure_shuffle: bool | None = None
    anchor_to_seed: bool | None = None
    replaygain_enabled: bool | None = None
    transition_mode: str | None = None
    post_queue_seed: str | None = None
    key_notation: str | None = None
    key_prefer_flats: bool | None = None
    show_lyrics: bool | None = None
    beat_sync_fx: bool | None = None
    key_sync_fx: bool | None = None
    beatmatch_on_skip: bool | None = None
    liners_enabled: bool | None = None
    # 0 turns a liner trigger off.
    liners_every_n_songs: Annotated[int, Field(ge=0)] | None = None
    liners_every_minutes: NonNegativeFloat | None = None
    liners_random_min_minutes: NonNegativeFloat | None = None
    liners_random_max_minutes: NonNegativeFloat | None = None
    liners_pick_mode: str | None = None
    liners_duck_db: Annotated[float, Field(ge=-30.0, le=0.0, allow_inf_nan=False)] | None = None
    # The ranges here are the ones the Settings panel's number fields use.
    no_repeat_window: Annotated[int, Field(ge=0, le=100_000)] | None = None
    artist_repeat_window: Annotated[int, Field(ge=0, le=100)] | None = None
    transition_wet_mix: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)] | None = None
    replaygain_target_db: Annotated[float, Field(ge=-30.0, le=0.0, allow_inf_nan=False)] | None = (
        None
    )

    @model_validator(mode="after")
    def _check_choices(self) -> PlaybackSettingsBody:
        """Reject an unknown choice before any field is applied."""
        validate_playback_choices(self.model_dump())
        return self


class BpmRangeBody(BaseModel):
    """Request body for POST /api/bpm-range — both null = clear filter."""

    lo: FiniteFloat | None = None
    hi: FiniteFloat | None = None


class DiscoveryBody(BaseModel):
    """Request body for POST /api/discovery — null disables."""

    every: int | None = None
