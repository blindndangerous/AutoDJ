"""Request bodies of the web settings routes, shared with ``web_state.json``.

``POST /api/playback-settings``, ``/api/djmix``, ``/api/bpm-range`` and the
other settings routes, including ``/api/eq``, validate their bodies with these models, and
:mod:`autodj.runtime_state` checks every saved setting against the same
field rules, so a value the page cannot set is never restored either.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from autodj.config import MAX_FADE_SECONDS
from autodj.repeat_settings import RepeatSettings


def validate_playback_choices(values: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` if any choice field in *values* is not allowed.

    The playback setters apply one field at a time, so a check made inside
    them let a request with one bad choice change every field ahead of it and
    then fail.  Callers run this first so a bad request changes nothing.
    ``None`` means "leave unchanged" and is skipped.
    """
    from autodj.config import (
        _validate_key_notation,
        _validate_loudness_processing,
        _validate_pick_mode,
        _validate_post_queue_seed,
        _validate_skip_style,
        _validate_transition_mode,
    )
    from autodj.liners import LINER_PICK_MODES

    checks: tuple[tuple[str, Callable[[str], str]], ...] = (
        ("transition_mode", _validate_transition_mode),
        ("pick_mode", _validate_pick_mode),
        ("loudness_processing", _validate_loudness_processing),
        ("post_queue_seed", _validate_post_queue_seed),
        ("key_notation", _validate_key_notation),
        ("skip_style", _validate_skip_style),
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
MusicSpeedPercent = Annotated[float, Field(ge=-10.0, le=10.0, allow_inf_nan=False)]


class VolumeBody(BaseModel):
    """Request body for POST /api/volume."""

    volume: FiniteFloat


EqGain = Annotated[float, Field(ge=0.0, le=2.0, allow_inf_nan=False)]
"""EQ band multiplier: 0 (cut) to 2 (boost), 1 is unity — the bridge's range."""


class EqBody(BaseModel):
    """Request body for POST /api/eq; a band outside 0 to 2 is refused."""

    low: EqGain | None = None
    mid: EqGain | None = None
    high: EqGain | None = None


class PresetBody(BaseModel):
    """Request body for POST /api/preset — empty / null name clears."""

    name: str | None = None


class RequestSettingsBody(BaseModel):
    """Partial listener request settings: on or off, approval, bridge songs and the give-up rule.

    The same limits as ``[requests]`` in config.toml (:class:`RequestsConfig`),
    which gives only the starting values (#49).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = Field(default=None, strict=True)
    approval: Literal["manual", "auto"] | None = None
    bridges_enabled: bool | None = Field(default=None, strict=True)
    max_bridges: Annotated[int, Field(ge=1, le=3, strict=True)] | None = None
    bridge_cost_threshold: Annotated[float, Field(ge=0.0, le=10.0, allow_inf_nan=False)] | None = (
        None
    )
    no_smooth_spot: Literal["best", "wait"] | None = None


class BreakSettingsBody(BaseModel):
    """Partial announcer break settings a station operator changes (#88).

    The same rules as ``[breaks]`` in config.toml (:class:`BreaksConfig`),
    which gives only the starting values.  The upload limits, quotas and
    waiting times stay in config.toml.  ``duck_db`` sent as ``null`` means
    "use the voice liner duck depth"; left out, it is left unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = Field(default=None, strict=True)
    dj_placement: Literal["auto", "talk_up", "standalone"] | None = None
    min_intro_seconds: (
        Annotated[float, Field(ge=0.0, le=60.0, allow_inf_nan=False, strict=True)] | None
    ) = None
    duck_db: Annotated[float, Field(ge=-30.0, le=0.0, allow_inf_nan=False, strict=True)] | None = (
        None
    )


class StreamSettingsBody(BaseModel):
    """Partial stream settings update: bitrates, continuous playback, name and statistics."""

    model_config = ConfigDict(extra="forbid")

    bitrate: Literal[128, 192, 256, 320] | None = None
    # The AAC and Opus streams (#50): 0 turns one off.
    aac_bitrate: Literal[0, 64, 96, 128, 192, 256] | None = None
    opus_bitrate: Literal[0, 48, 64, 96, 128, 192] | None = None
    # Days of listener statistics to keep; 0 keeps none.
    stats_days: Annotated[int, Field(ge=0, le=3650, strict=True)] | None = None
    continuous: bool | None = Field(default=None, strict=True)
    station_name: Annotated[str, Field(max_length=100)] | None = None
    genre: Annotated[str, Field(max_length=255)] | None = None
    description: Annotated[str, Field(max_length=255)] | None = None
    url: Annotated[str, Field(max_length=2048)] | None = None
    show_next: bool | None = Field(default=None, strict=True)
    max_listeners_per_address: Annotated[int, Field(ge=0, le=100, strict=True)] | None = None

    @field_validator("station_name")
    @classmethod
    def _check_station_name(cls, value: str | None) -> str | None:
        """Refuse a blank name, or one with control characters, as config.toml does."""
        if value is None:
            return None
        name = value.strip()
        if not name:
            raise ValueError("station_name must not be blank")
        if not name.isprintable():
            raise ValueError("station_name must not contain control characters")
        return name

    @field_validator("genre", "description", "url")
    @classmethod
    def _check_station_metadata(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        if not value.isprintable():
            raise ValueError(f"{info.field_name} must not contain control characters")
        if info.field_name == "url" and value:
            from urllib.parse import urlsplit

            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("url must be an HTTP or HTTPS URL")
        return value


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
    drop_mix: bool | None = None
    key_shift: bool | None = None
    phrase_align: bool | None = None
    outro_intro_align: bool | None = None
    filter_sweep: bool | None = None
    vocal_guard: bool | None = None
    phrase_bars: Annotated[int, Field(ge=1, le=64)] | None = None
    # The ranges here are the ones the Settings panel's number fields use.
    beatmatch_max_stretch: Annotated[float, Field(ge=0.0, le=0.5, allow_inf_nan=False)] | None = (
        None
    )
    beatmatch_glide_bars: Annotated[int, Field(ge=0, le=64)] | None = None
    filter_sweep_floor_hz: (
        Annotated[float, Field(ge=20.0, le=5000.0, allow_inf_nan=False)] | None
    ) = None

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
    music_speed_percent: MusicSpeedPercent | None = None
    crossfade_eq_duck: bool | None = None
    pick_mode: Literal["similarity", "random", "least_recent", "clock"] | None = None
    loudness_processing: Literal["off", "replaygain", "smooth", "stereotool"] | None = None
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
    skip_style: str | None = None
    liners_enabled: bool | None = None
    liners_talk_up: bool | None = None
    matched_intro_only_without_liner: bool | None = None
    # 0 turns a liner trigger off.
    liners_every_n_songs: Annotated[int, Field(ge=0)] | None = None
    liners_every_minutes: NonNegativeFloat | None = None
    liners_random_min_minutes: NonNegativeFloat | None = None
    liners_random_max_minutes: NonNegativeFloat | None = None
    liners_pick_mode: str | None = None
    liners_duck_db: Annotated[float, Field(ge=-30.0, le=0.0, allow_inf_nan=False)] | None = None
    # The ranges here are the ones the Settings panel's number fields use.
    repeat_rules: RepeatSettings | None = None
    transition_wet_mix: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)] | None = None
    replaygain_target_db: Annotated[float, Field(ge=-30.0, le=0.0, allow_inf_nan=False)] | None = (
        None
    )
    replaygain_max_clip_safe_gain: (
        Annotated[float, Field(ge=0.1, le=2.0, allow_inf_nan=False)] | None
    ) = None
    crossfade_bass_cutoff_hz: (
        Annotated[float, Field(ge=20.0, le=1000.0, allow_inf_nan=False)] | None
    ) = None
    pick_top_k: Annotated[int, Field(ge=1, le=100)] | None = None
    pick_temperature: Annotated[float, Field(ge=0.0, le=10.0, allow_inf_nan=False)] | None = None
    hour_fit: bool | None = Field(default=None, strict=True)
    hour_fit_window_minutes: Annotated[int, Field(ge=1, le=30, strict=True)] | None = None
    prefetch_next_track: bool | None = None
    silence_trigger_crossfade: bool | None = None
    intro_lift_max_db: Annotated[float, Field(ge=0.0, le=6.0, allow_inf_nan=False)] | None = None
    stereotool_input_trim_db: (
        Annotated[float, Field(ge=-12.0, le=6.0, allow_inf_nan=False)] | None
    ) = None
    stereotool_agc_drive_db: (
        Annotated[float, Field(ge=0.0, le=12.0, allow_inf_nan=False)] | None
    ) = None
    stereotool_preset: str | None = None

    @field_validator("music_speed_percent", mode="before")
    @classmethod
    def _speed_precision(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("music_speed_percent must be a number")
        try:
            precise = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return value  # the float field supplies the normal type error
        if not precise.is_finite() or abs(precise) > 10:
            return value  # range and finite checks are handled by the field
        if precise != Decimal(str(round(float(precise), 2))):
            raise ValueError("music_speed_percent may have at most 2 decimal places")
        return value

    @field_validator("stereotool_preset")
    @classmethod
    def _preset_name(cls, value: str | None) -> str | None:
        if value is not None:
            from autodj.stereotool_presets import validate_preset_name

            validate_preset_name(value)
        return value

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
