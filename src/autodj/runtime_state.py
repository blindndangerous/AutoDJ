"""Persistent web-UI settings (``web_state.json``).

Settings the user toggles in the **browser** — preset, transition
effect, transition mode, DJ-mix toggles, smart shuffle, ReplayGain,
BPM range, discovery rate — are written to
``<index_dir>/<name>/web_state.json``
so the next `autodj serve` boot restores them.

This file is **owned by the web UI**.  CLI ``autodj play`` deliberately
does NOT read or write it — CLI playback is driven entirely by config
+ command-line flags.  Two surfaces, two state stores, no surprise
overrides.

The on-disk format mirrors the dict returned by
``PlayerBridge.get_settings()`` minus the ``available_presets`` list.
The liner source folder remains config-owned and is never copied into browser-owned state.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, TypedDict, TypeGuard

from autodj.dj_meta import HARMONIC_MODES
from autodj.fsutil import atomic_write
from autodj.liners import LINER_PICK_MODES
from autodj.transitions import TRANSITION_EFFECT_NAMES

logger = logging.getLogger(__name__)

#: Version 2 dropped ``harmonic_mixing``: ``harmonic_mode`` alone now turns
#: harmonic mixing on, so a version 1 file (which stored "compatible" next to
#: ``harmonic_mixing: false``) would silently switch key filtering on.
STATE_VERSION = 2


class PlaybackState(TypedDict, total=False):
    """Persisted playback options restored from browser-owned state."""

    crossfade_seconds: float
    fade_in_seconds: float
    crossfade_eq_duck: bool
    smart_shuffle: bool
    pure_shuffle: bool
    anchor_to_seed: bool
    replaygain_enabled: bool
    transition_mode: str
    post_queue_seed: str
    key_notation: str
    key_prefer_flats: bool
    show_lyrics: bool
    enable_daypart: bool
    enable_mood_arc: bool
    mood_arc_hours: float
    import_external_cues: bool
    beat_sync_fx: bool
    key_sync_fx: bool
    beatmatch_on_skip: bool
    prefetch_next_track: bool
    silence_trigger_crossfade: bool
    liners_enabled: bool
    liners_every_n_songs: int | None
    liners_every_minutes: float | None
    liners_random_min_minutes: float | None
    liners_random_max_minutes: float | None
    liners_pick_mode: str
    liners_duck_db: float
    stream_bitrate: int
    no_repeat_window: int
    artist_repeat_window: int
    transition_wet_mix: float
    replaygain_target_db: float
    volume: float
    is_muted: bool


DJMIX_BOOL_FIELDS = (
    "beatmatch",
    "phrase_align",
    "outro_intro_align",
    "filter_sweep",
)
PLAYBACK_CFG_BOOL_FIELDS = (
    "crossfade_eq_duck",
    "show_lyrics",
    "enable_daypart",
    "import_external_cues",
    "key_prefer_flats",
    "beat_sync_fx",
    "key_sync_fx",
    "beatmatch_on_skip",
    "prefetch_next_track",
    "silence_trigger_crossfade",
    "liners_enabled",
)
PLAYER_BOOL_FIELDS = {
    "smart_shuffle": "_smart_shuffle",
    "pure_shuffle": "_pure_shuffle",
    "anchor_to_seed": "_anchor_to_seed",
}
#: Playback keys mirrored into ``web_state.json``.  Derived from the
#: :class:`PlaybackState` schema so the two cannot drift apart.
PERSISTED_PLAYBACK_FIELDS = frozenset(PlaybackState.__annotations__)


def state_file_for(index_dir: Path | None) -> Path | None:
    """Return the canonical state-file path for *index_dir*, or ``None``."""
    if index_dir is None:
        return None
    return Path(index_dir) / "web_state.json"


def _warn(field: str, value: object) -> None:
    """Log that a persisted field has an invalid value."""
    logger.warning("ignoring invalid %s in web_state.json: %r", field, value)


def _read_bool(data: dict, field: str) -> bool | None:
    """Read a Boolean field or warn when its stored value is invalid."""
    if field not in data:
        return None
    value = data[field]
    if type(value) is bool:
        return value
    _warn(field, value)
    return None


def _is_finite_number(value: object) -> TypeGuard[int | float]:
    """Return whether value is a finite non-Boolean number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _read_number(data: dict, field: str, minimum: float) -> float | None:
    """Read a finite numeric field that satisfies its minimum value."""
    if field not in data:
        return None
    value = data[field]
    if _is_finite_number(value) and value >= minimum:
        return float(value)
    _warn(field, value)
    return None


def _restore_preset(player: Any, data: dict) -> None:
    """Restore a selected preset when its stored name is valid."""
    if "preset" not in data:
        return
    value = data["preset"]
    if value is None or value == "":
        player._preset = None
        return
    if not isinstance(value, str):
        _warn("preset", value)
        return

    from autodj.presets import get_preset

    try:
        player._preset = get_preset(value, player._cfg.presets)
    except ValueError:
        _warn("preset", value)


def _restore_djmix(cfg: Any, data: dict) -> None:
    """Restore validated DJ-mix settings from persisted state."""
    djmix = data.get("djmix")
    if not isinstance(djmix, dict):
        return
    for field in DJMIX_BOOL_FIELDS:
        value = _read_bool(djmix, field)
        if value is not None:
            setattr(cfg.djmix, field, value)
    if "harmonic_mode" in djmix:
        value = djmix["harmonic_mode"]
        if isinstance(value, str) and value in HARMONIC_MODES:
            cfg.djmix.harmonic_mode = value
        else:
            _warn("harmonic_mode", value)


def _read_int_in_range(data: dict, field: str, low: int, high: int) -> int | None:
    """Read a whole-number field inside ``[low, high]`` or warn."""
    if field not in data:
        return None
    value = data[field]
    if type(value) is int and low <= value <= high:
        return value
    _warn(field, value)
    return None


def _read_float_in_range(data: dict, field: str, low: float, high: float) -> float | None:
    """Read a finite number inside ``[low, high]`` or warn."""
    if field not in data:
        return None
    value = data[field]
    if _is_finite_number(value) and low <= value <= high:
        return float(value)
    _warn(field, value)
    return None


def _restore_mix_levels(cfg: Any, player: Any, data: dict, pb: dict) -> None:
    """Restore phrase length, repeat windows, effect level and loudness target.

    The ranges match the ones ``POST /api/playback-settings`` and
    ``POST /api/djmix`` accept.
    """
    djmix = data.get("djmix")
    if isinstance(djmix, dict):
        bars = _read_int_in_range(djmix, "phrase_bars", 1, 64)
        if bars is not None:
            cfg.djmix.phrase_bars = bars
    wet = _read_float_in_range(pb, "transition_wet_mix", 0.0, 1.0)
    if wet is not None:
        cfg.transitions.wet_mix = wet
    target = _read_float_in_range(pb, "replaygain_target_db", -30.0, 0.0)
    if target is not None:
        cfg.replaygain.target_db = target
    no_repeat = _read_int_in_range(pb, "no_repeat_window", 0, 100_000)
    artist = _read_int_in_range(pb, "artist_repeat_window", 0, 100)
    if no_repeat is None and artist is None:
        return
    if no_repeat is not None:
        cfg.playback.no_repeat_window = no_repeat
    if artist is not None:
        cfg.playback.artist_repeat_window = artist
    from autodj.player import apply_repeat_windows

    apply_repeat_windows(player)


def _restore_transition(cfg: Any, data: dict) -> None:
    """Restore a configured transition effect when it is supported."""
    if "transition" not in data:
        return
    value = data["transition"]
    if isinstance(value, str) and value.lower() in TRANSITION_EFFECT_NAMES:
        cfg.transitions.effect = value.lower()
    else:
        _warn("transition", value)


def _restore_playback_floats(cfg: Any, pb: dict) -> None:
    """Restore nonnegative floating-point playback settings."""
    for field, minimum in (
        ("crossfade_seconds", 0.0),
        ("fade_in_seconds", 0.0),
        ("mood_arc_hours", 0.25),
    ):
        value = _read_number(pb, field, minimum)
        if value is not None:
            setattr(cfg.playback, field, value)


def _restore_playback_bools(cfg: Any, player: Any, pb: dict) -> None:
    """Restore Boolean playback settings on configuration and player state."""
    for field in PLAYBACK_CFG_BOOL_FIELDS:
        value = _read_bool(pb, field)
        if value is not None:
            setattr(cfg.playback, field, value)
    for field, attribute in PLAYER_BOOL_FIELDS.items():
        value = _read_bool(pb, field)
        if value is not None:
            setattr(player, attribute, value)
    replaygain = _read_bool(pb, "replaygain_enabled")
    if replaygain is not None:
        cfg.replaygain.enabled = replaygain


def _restore_mood_arc(cfg: Any, player: Any, pb: dict) -> None:
    """Restore mood-arc enablement and create its default arc when enabled."""
    enabled = _read_bool(pb, "enable_mood_arc")
    if enabled is None:
        return
    cfg.playback.enable_mood_arc = enabled
    if not enabled:
        player._mood_arc = None
        return
    from autodj.mood_arc import make_default_arc

    player._mood_arc = make_default_arc(
        duration_hours=cfg.playback.mood_arc_hours,
    )


def _restore_validated_strings(cfg: Any, pb: dict) -> None:
    """Restore playback strings after validation by their configuration rules."""
    from autodj.config import (
        _validate_key_notation,
        _validate_post_queue_seed,
        _validate_transition_mode,
    )

    validators = {
        "transition_mode": _validate_transition_mode,
        "post_queue_seed": _validate_post_queue_seed,
        "key_notation": _validate_key_notation,
    }
    for field, validator in validators.items():
        if field not in pb:
            continue
        value = pb[field]
        if not isinstance(value, str):
            _warn(field, value)
            continue
        try:
            setattr(cfg.playback, field, validator(value))
        except ValueError:
            _warn(field, value)


def _restore_nullable_number(
    target: Any,
    pb: dict,
    field: str,
    *,
    integer: bool = False,
) -> None:
    """Restore a positive numeric field that may be explicitly null."""
    if field not in pb:
        return
    value = pb[field]
    if value is None:
        setattr(target, field, None)
        return
    valid_type = type(value) is int if integer else _is_finite_number(value)
    if not valid_type or value <= 0:
        _warn(field, value)
        return
    setattr(target, field, int(value) if integer else float(value))


def _read_random_liner_bound(
    playback: Any,
    pb: dict,
    field: str,
) -> tuple[bool, float | None]:
    """Read and validate one endpoint of the random liner interval."""
    present = field in pb
    value = pb[field] if present else getattr(playback, field, None)
    if value is None:
        return True, None
    if _is_finite_number(value) and value > 0:
        return True, float(value)
    if present:
        _warn(field, value)
    else:
        logger.warning("ignoring invalid current %s while restoring web_state.json", field)
    return False, None


def _restore_random_liner_window(playback: Any, pb: dict) -> None:
    """Restore a valid random liner interval without changing missing bounds."""
    min_field = "liners_random_min_minutes"
    max_field = "liners_random_max_minutes"
    min_present = min_field in pb
    max_present = max_field in pb
    if not min_present and not max_present:
        return

    min_valid, minimum = _read_random_liner_bound(playback, pb, min_field)
    max_valid, maximum = _read_random_liner_bound(playback, pb, max_field)
    if not min_valid or not max_valid:
        return
    if minimum is not None and maximum is not None and minimum > maximum:
        logger.warning(
            "ignoring invalid random liner window in web_state.json: min=%r max=%r",
            minimum,
            maximum,
        )
        return

    if min_present:
        playback.liners_random_min_minutes = minimum
    if max_present:
        playback.liners_random_max_minutes = maximum


def _restore_liners(cfg: Any, pb: dict) -> None:
    """Restore validated liner scheduling and ducking settings."""
    playback = cfg.playback
    _restore_nullable_number(playback, pb, "liners_every_n_songs", integer=True)
    _restore_nullable_number(playback, pb, "liners_every_minutes")
    _restore_random_liner_window(playback, pb)
    if "liners_pick_mode" in pb:
        value = pb["liners_pick_mode"]
        if isinstance(value, str) and value in LINER_PICK_MODES:
            playback.liners_pick_mode = value
        else:
            _warn("liners_pick_mode", value)
    if "liners_duck_db" in pb:
        value = pb["liners_duck_db"]
        if _is_finite_number(value) and -60 <= value <= 0:
            playback.liners_duck_db = float(value)
        else:
            _warn("liners_duck_db", value)


def _restore_stream_bitrate(cfg: Any, pb: dict) -> None:
    """Restore the radio-stream bitrate when it is one of the allowed values."""
    if "stream_bitrate" not in pb:
        return
    from autodj.config import STREAM_BITRATES

    value = pb["stream_bitrate"]
    if type(value) is int and value in STREAM_BITRATES:
        cfg.stream.bitrate = value
    else:
        _warn("stream_bitrate", value)


def _restore_volume(player: Any, pb: dict) -> None:
    """Restore the volume and mute, so a restart never comes back at full volume."""
    volume = _read_float_in_range(pb, "volume", 0.0, 1.0)
    if volume is not None:
        player._state.volume = volume
    muted = _read_bool(pb, "is_muted")
    if muted is not None:
        player._state.is_muted = muted


def _restore_bpm_range(player: Any, data: dict) -> None:
    """Restore a valid BPM range or clear it when explicitly null."""
    if "bpm_range" not in data:
        return
    value = data["bpm_range"]
    if value is None:
        player._bpm_range = None
        return
    if not isinstance(value, dict):
        _warn("bpm_range", value)
        return
    lo, hi = value.get("lo"), value.get("hi")
    if lo is None and hi is None:
        player._bpm_range = None
    elif _is_finite_number(lo) and _is_finite_number(hi) and lo < hi:
        player._bpm_range = (float(lo), float(hi))
    else:
        _warn("bpm_range", value)


def _restore_discovery(player: Any, data: dict) -> None:
    """Restore a nonnegative discovery interval or clear it when null."""
    if "discovery_every" not in data:
        return
    value = data["discovery_every"]
    if value is None:
        player._discovery_every = None
    elif type(value) is int and value >= 0:
        player._discovery_every = value or None
    else:
        _warn("discovery_every", value)


def load_into_player(player: Any, index_dir: Path | None) -> None:
    """Restore previously-saved settings into *player*.

    No-op when no state file exists, it's unreadable, or it lacks an
    integer ``schema_version`` of at least :data:`STATE_VERSION`.

    Args:
        player: A live :class:`autodj.player.Player` instance.
        index_dir: Directory housing ``web_state.json``.
    """
    path = state_file_for(index_dir)
    if path is None or not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning("web_state.json unreadable, ignoring: %s", exc)
        return

    if not isinstance(data, dict):
        logger.warning("web_state.json root is not an object, ignoring")
        return
    version = data.get("schema_version")
    if type(version) is not int or version < STATE_VERSION:
        logger.warning(
            "ignoring web_state.json: schema_version must be an integer of at least %d, got %r",
            STATE_VERSION,
            version,
        )
        return
    if version > STATE_VERSION:
        logger.warning(
            "web_state.json schema_version %d is newer than supported version %d; "
            "applying known fields",
            version,
            STATE_VERSION,
        )

    cfg = player._cfg
    _restore_preset(player, data)
    _restore_transition(cfg, data)
    _restore_djmix(cfg, data)
    playback = data.get("playback")
    if isinstance(playback, dict):
        _restore_playback_floats(cfg, playback)
        _restore_playback_bools(cfg, player, playback)
        _restore_mood_arc(cfg, player, playback)
        _restore_validated_strings(cfg, playback)
        _restore_liners(cfg, playback)
        _restore_stream_bitrate(cfg, playback)
        _restore_volume(player, playback)
    _restore_mix_levels(cfg, player, data, playback if isinstance(playback, dict) else {})
    _restore_bpm_range(player, data)
    _restore_discovery(player, data)


def save_from_player(settings: dict, index_dir: Path | None) -> None:
    """Write *settings* (PlayerBridge.get_settings shape) to disk atomically.

    The ``available_presets`` field is stripped — it's a derived view of
    ``cfg.presets`` plus the built-ins, not user state.

    Args:
        settings: Dict from ``PlayerBridge.get_settings()``.
        index_dir: Directory that should contain ``web_state.json``.
    """
    path = state_file_for(index_dir)
    if path is None:
        return
    playback = settings.get("playback", {})
    persisted_playback = (
        {key: value for key, value in playback.items() if key in PERSISTED_PLAYBACK_FIELDS}
        if isinstance(playback, dict)
        else {}
    )
    payload: dict[str, Any] = {
        "schema_version": STATE_VERSION,
        "preset": settings.get("preset"),
        "transition": settings.get("transition", "none"),
        "djmix": settings.get("djmix", {}),
        "playback": persisted_playback,
        "bpm_range": settings.get("bpm_range", {"lo": None, "hi": None}),
        "discovery_every": settings.get("discovery_every"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False))
    except OSError as exc:
        logger.warning("Failed to save web_state.json: %s", exc)
