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
import os
from pathlib import Path
from typing import Any, NotRequired, TypedDict, TypeGuard, cast

logger = logging.getLogger(__name__)

STATE_VERSION = 1
HARMONIC_MODES = frozenset(
    {"off", "compatible", "strict", "energy_boost", "mood_change", "neighbour"}
)
LINER_PICK_MODES = frozenset({"random", "sequential", "weighted"})
TRANSITION_EFFECTS = frozenset(
    {
        "none",
        "echo_out",
        "reverb_tail",
        "highpass_sweep",
        "lowpass_sweep",
        "tape_stop",
        "gate_stutter",
        "noise_riser",
        "noise_drop",
        "backspin",
        "forward_spin",
        "cross_eq_swap",
        "bitcrusher",
        "flanger",
        "pitch_swell",
        "telephone",
        "chorus",
        "submerge",
        "vinyl_wow",
        "freeze",
        "glitch",
        "scratch",
        "beat_repeat",
        "sidechain_pump",
        "reverse_reverb",
        "air_horn",
        "random",
        "rotate",
    }
)
SESSION_ONLY_PLAYBACK_FIELDS = frozenset({"no_repeat_window", "library_size"})
CONFIG_ONLY_PLAYBACK_FIELDS = frozenset({"liners_folder"})


class DJMixState(TypedDict, total=False):
    harmonic_mixing: bool
    harmonic_mode: str
    beatmatch: bool
    phrase_align: bool
    outro_intro_align: bool
    filter_sweep: bool


class PlaybackState(TypedDict, total=False):
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


class PersistedState(TypedDict):
    schema_version: int
    preset: NotRequired[str | None]
    transition: NotRequired[str]
    djmix: NotRequired[DJMixState]
    playback: NotRequired[PlaybackState]
    bpm_range: NotRequired[dict[str, float | None] | None]
    discovery_every: NotRequired[int | None]


DJMIX_BOOL_FIELDS = (
    "harmonic_mixing",
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
PERSISTED_PLAYBACK_FIELDS = frozenset(
    {
        "crossfade_seconds",
        "fade_in_seconds",
        "crossfade_eq_duck",
        "smart_shuffle",
        "pure_shuffle",
        "anchor_to_seed",
        "replaygain_enabled",
        "transition_mode",
        "post_queue_seed",
        "key_notation",
        "key_prefer_flats",
        "show_lyrics",
        "enable_daypart",
        "enable_mood_arc",
        "mood_arc_hours",
        "import_external_cues",
        "beat_sync_fx",
        "key_sync_fx",
        "beatmatch_on_skip",
        "prefetch_next_track",
        "silence_trigger_crossfade",
        "liners_enabled",
        "liners_every_n_songs",
        "liners_every_minutes",
        "liners_random_min_minutes",
        "liners_random_max_minutes",
        "liners_pick_mode",
        "liners_duck_db",
    }
)


def state_file_for(index_dir: Path | None) -> Path | None:
    """Return the canonical state-file path for *index_dir*, or ``None``."""
    if index_dir is None:
        return None
    return Path(index_dir) / "web_state.json"


def _fsync_directory(path: Path) -> None:
    """Durably publish a directory entry where the filesystem supports it."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _warn(field: str, value: object) -> None:
    logger.warning("ignoring invalid %s in web_state.json: %r", field, value)


def _read_bool(data: dict, field: str) -> bool | None:
    if field not in data:
        return None
    value = data[field]
    if type(value) is bool:
        return value
    _warn(field, value)
    return None


def _is_finite_number(value: object) -> TypeGuard[int | float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _read_number(data: dict, field: str, minimum: float) -> float | None:
    if field not in data:
        return None
    value = data[field]
    if _is_finite_number(value) and value >= minimum:
        return float(value)
    _warn(field, value)
    return None


def _restore_preset(player: Any, data: dict) -> None:
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


def _restore_transition(cfg: Any, data: dict) -> None:
    if "transition" not in data:
        return
    value = data["transition"]
    if isinstance(value, str) and value.lower() in TRANSITION_EFFECTS:
        cfg.transitions.effect = value.lower()
    else:
        _warn("transition", value)


def _restore_playback_floats(cfg: Any, pb: dict) -> None:
    for field, minimum in (
        ("crossfade_seconds", 0.0),
        ("fade_in_seconds", 0.0),
        ("mood_arc_hours", 0.25),
    ):
        value = _read_number(pb, field, minimum)
        if value is not None:
            setattr(cfg.playback, field, value)


def _restore_playback_bools(cfg: Any, player: Any, pb: dict) -> None:
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


def _restore_bpm_range(player: Any, data: dict) -> None:
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

    No-op when no state file exists or it's unreadable.

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
    version = data.get("schema_version", 0)
    if type(version) is not int or version < 0:
        _warn("schema_version", version)
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
    payload: PersistedState = {
        "schema_version": STATE_VERSION,
        "preset": settings.get("preset"),
        "transition": settings.get("transition", "none"),
        "djmix": settings.get("djmix", {}),
        "playback": cast("PlaybackState", persisted_playback),
        "bpm_range": settings.get("bpm_range", {"lo": None, "hi": None}),
        "discovery_every": settings.get("discovery_every"),
    }
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            _fsync_directory(path.parent)
        except OSError as exc:
            logger.warning(
                "Saved web_state.json but parent directory durability flush failed: %s",
                exc,
            )
    except OSError as exc:
        logger.warning("Failed to save web_state.json: %s", exc)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to clean temporary web_state.json: %s", exc)
