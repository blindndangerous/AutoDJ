"""Persistent web-UI settings (``web_state.json``).

Settings the user changes in the **browser** — preset, transition
effect, DJ-mix options, playback settings, volume and mute, stream
bitrate and station name, BPM range, discovery rate, EQ and whether discovery
is on — are written to
``<index_dir>/<name>/web_state.json`` so the next `autodj serve` boot
restores them.

The on-disk format mirrors the dict returned by
``PlayerBridge.get_settings()``, keeping only the fields a settings route
can set.  On restore every saved field is checked on its own against the
rule of the route that sets it (:mod:`autodj.settings_bodies`) and applied
through the same :class:`~autodj._bridge.PlayerBridge` setter: a bad value
is skipped with a warning and the other saved settings still apply.  The
Library source folders are catalog-owned. The configured liner upload
destination remains config-owned; neither is copied into browser-owned state.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from autodj.fsutil import atomic_write
from autodj.repeat_settings import removed_saved_repeat_keys
from autodj.settings_bodies import (
    BpmRangeBody,
    DiscoveryBody,
    DjMixBody,
    EqBody,
    PlaybackSettingsBody,
    PresetBody,
    StreamSettingsBody,
    TransitionBody,
    VolumeBody,
)

if TYPE_CHECKING:
    from autodj._bridge import PlayerBridge

logger = logging.getLogger(__name__)


class SavedStateError(ValueError):
    """``web_state.json`` holds settings AutoDJ no longer reads; fix it by hand."""


#: Version 2 dropped the redundant ``harmonic_mixing`` toggle. Version 1
#: migration honors that toggle so ``harmonic_mixing: false`` cannot silently
#: enable key filtering through a stored ``harmonic_mode`` value.
STATE_VERSION = 2

#: Saved under ``playback`` but set by their own routes (``/api/volume``,
#: ``/api/mute``, ``/api/stream/settings``).
_OWN_ROUTE_PLAYBACK_FIELDS = frozenset(
    {
        "volume",
        "is_muted",
        "stream_bitrate",
        "stream_continuous",
        "stream_station_name",
        "nowplaying_show_next",
        "stream_max_listeners_per_address",
    }
)
#: Playback keys mirrored into ``web_state.json``.
PERSISTED_PLAYBACK_FIELDS = frozenset(PlaybackSettingsBody.model_fields) | (
    _OWN_ROUTE_PLAYBACK_FIELDS
)
#: Liner triggers the page turns off with 0; the saved settings store off as null.
_LINER_TRIGGERS = frozenset(
    {
        "liners_every_n_songs",
        "liners_every_minutes",
        "liners_random_min_minutes",
        "liners_random_max_minutes",
    }
)
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "preset",
        "transition",
        "djmix",
        "playback",
        "bpm_range",
        "discovery_every",
        "discovery_enabled",
        "eq",
    }
)
_INVALID = object()
_STRICT_BOOL = TypeAdapter(bool)


def _migrate_v1(data: dict[str, Any]) -> dict[str, Any]:
    """Migrate v1 state while honoring its independent harmonic switch."""
    migrated = data.copy()
    djmix = migrated.get("djmix")
    if isinstance(djmix, dict):
        djmix = djmix.copy()
        legacy_toggle = djmix.pop("harmonic_mixing", _INVALID)
        if legacy_toggle is not _INVALID:
            try:
                enabled = _STRICT_BOOL.validate_python(legacy_toggle, strict=True)
            except ValidationError:
                _warn("djmix.harmonic_mixing", legacy_toggle)
                djmix["harmonic_mode"] = "off"
            else:
                mode = djmix.get("harmonic_mode")
                if not enabled or mode is None:
                    djmix["harmonic_mode"] = "off"
                else:
                    if _checked(DjMixBody, "harmonic_mode", mode) is _INVALID:
                        djmix["harmonic_mode"] = "off"
        migrated["djmix"] = djmix
    migrated["schema_version"] = STATE_VERSION
    return migrated


def _backup_v1_state(path: Path) -> bool:
    """Preserve a v1 file once before replacing it; false means do not save."""
    backup = path.with_name(f"{path.name}.v1.bak")
    if backup.exists():
        return True
    try:
        original = path.read_bytes()
        data = json.loads(original)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return True
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int:
        return True
    if data["schema_version"] != 1:
        return True
    try:
        atomic_write(backup, original)
    except OSError as exc:
        logger.warning("Failed to back up v1 web_state.json; keeping original: %s", exc)
        return False
    return True


def state_file_for(index_dir: Path | None) -> Path | None:
    """Return the canonical state-file path for *index_dir*, or ``None``."""
    if index_dir is None:
        return None
    return Path(index_dir) / "web_state.json"


def _warn(field: str, value: object) -> None:
    """Log that a persisted field has an invalid value."""
    logger.warning("ignoring invalid %s in web_state.json: %r", field, value)


def _checked(model: type[BaseModel], field: str, value: object, saved_as: str | None = None) -> Any:
    """Return *value* as *model* accepts it for *field*, or ``_INVALID`` after a warning.

    Strict, so a saved ``"true"`` or ``1`` is not taken for a Boolean.
    """
    try:
        return getattr(model.model_validate({field: value}, strict=True), field)
    except ValidationError:
        _warn(saved_as or field, value)
        return _INVALID


def _section(data: dict, name: str) -> dict[str, Any]:
    """Return saved section *name*, or an empty one when it is not an object."""
    section = data.get(name, {})
    if not isinstance(section, dict):
        _warn(name, section)
        return {}
    return section


def _restore_preset(bridge: PlayerBridge, value: object) -> None:
    """Restore the saved preset, or none; an unknown preset name is skipped."""
    name = _checked(PresetBody, "name", value, "preset")
    if name is _INVALID:
        return
    player = bridge.player
    if not name:
        player._preset = None
        return
    from autodj.presets import get_preset

    try:
        player._preset = get_preset(name, player._cfg.presets)
    except ValueError:
        _warn("preset", value)


def _restore_playback(bridge: PlayerBridge, playback: dict[str, Any]) -> None:
    """Restore the playback section through the playback-settings route's setter."""
    player = bridge.player
    if "volume" in playback:
        volume = _checked(VolumeBody, "volume", playback["volume"])
        if volume is not _INVALID:
            bridge.set_volume(volume)
    if "is_muted" in playback:
        try:
            player._state.is_muted = _STRICT_BOOL.validate_python(playback["is_muted"], strict=True)
        except ValidationError:
            _warn("is_muted", playback["is_muted"])
    if "stream_bitrate" in playback:
        # The stream is not running yet, so only the configured rate changes.
        bitrate = _checked(
            StreamSettingsBody, "bitrate", playback["stream_bitrate"], "stream_bitrate"
        )
        if bitrate is not _INVALID:
            player._cfg.stream.bitrate = bitrate
    if "stream_continuous" in playback:
        continuous = _checked(
            StreamSettingsBody, "continuous", playback["stream_continuous"], "stream_continuous"
        )
        if continuous is not _INVALID:
            player._cfg.stream.continuous = continuous
    if "stream_station_name" in playback:
        name = _checked(
            StreamSettingsBody,
            "station_name",
            playback["stream_station_name"],
            "stream_station_name",
        )
        if name is not _INVALID and name is not None:
            player._cfg.stream.station_name = name
    if "nowplaying_show_next" in playback:
        show_next = _checked(
            StreamSettingsBody,
            "show_next",
            playback["nowplaying_show_next"],
            "nowplaying_show_next",
        )
        if show_next is not _INVALID:
            player._cfg.nowplaying.show_next = show_next
    if "stream_max_listeners_per_address" in playback:
        limit = _checked(
            StreamSettingsBody,
            "max_listeners_per_address",
            playback["stream_max_listeners_per_address"],
            "stream_max_listeners_per_address",
        )
        if limit is not _INVALID and limit is not None:
            player._cfg.stream.max_listeners_per_address = limit

    values: dict[str, Any] = {}
    for field, saved in playback.items():
        if field in _OWN_ROUTE_PLAYBACK_FIELDS:
            continue
        value = 0 if saved is None and field in _LINER_TRIGGERS else saved
        if value is None:
            _warn(field, value)
            continue
        checked = _checked(PlaybackSettingsBody, field, value)
        if checked is not _INVALID:
            values[field] = checked
    if "stereotool_preset" in values:
        from autodj.stereotool_presets import preset_path

        try:
            preset_path(player._cfg.index.active_dir, values["stereotool_preset"])
        except (ValueError, FileNotFoundError):
            _warn("stereotool_preset", values.pop("stereotool_preset"))
    bridge.set_playback_settings(PlaybackSettingsBody.model_validate(values), restoring=True)


def _restore_eq(bridge: PlayerBridge, eq: dict[str, Any]) -> None:
    """Restore valid EQ multipliers; omitted or invalid bands keep their defaults."""
    values: dict[str, float] = {}
    for band in ("low", "mid", "high"):
        value = eq.get(band, _INVALID)
        if value is _INVALID:
            continue
        checked = _checked(EqBody, band, value, f"eq.{band}")
        if checked is not _INVALID and checked is not None:
            values[band] = checked
    if values:
        bridge.set_eq(**values)


def load_into_bridge(bridge: PlayerBridge, index_dir: Path | None) -> None:
    """Restore previously-saved settings through *bridge*.

    Version 1 state is migrated conservatively; unsupported versions are
    ignored. Other saved fields continue to be validated individually.

    Raises:
        SavedStateError: The file holds removed repeat keys.

    Args:
        bridge: The :class:`~autodj._bridge.PlayerBridge` of a live player.
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
    removed = removed_saved_repeat_keys(data)
    if removed:
        old = [
            f"{group.removeprefix('playback.')}.{key}" if group != "playback" else key
            for group, keys in removed.items()
            for key in keys
        ]
        raise SavedStateError(
            f"{path}: removed playback repeat keys {', '.join(old)}; "
            "remove them by hand and re-enter limits in Settings > Picking > Repeats"
        )
    version = data.get("schema_version")
    if type(version) is int and version == 1:
        data = _migrate_v1(data)
    elif type(version) is not int or version != STATE_VERSION:
        logger.warning(
            "ignoring web_state.json: schema_version must be %d, got %r",
            STATE_VERSION,
            version,
        )
        return
    for field in data.keys() - _TOP_LEVEL_FIELDS:
        _warn(field, data[field])

    if "preset" in data:
        _restore_preset(bridge, data["preset"])
    if "transition" in data:
        effect = _checked(TransitionBody, "effect", data["transition"], "transition")
        if effect is not _INVALID:
            try:
                bridge.set_transition(effect)
            except ValueError:
                _warn("transition", effect)
    djmix: dict[str, Any] = {}
    for field, value in _section(data, "djmix").items():
        checked = _checked(DjMixBody, field, value)
        if checked is not _INVALID:
            djmix[field] = checked
    bridge.set_djmix(**djmix)
    _restore_playback(bridge, _section(data, "playback"))
    if "bpm_range" in data:
        try:
            bpm = BpmRangeBody.model_validate(data["bpm_range"], strict=True)
        except ValidationError:
            _warn("bpm_range", data["bpm_range"])
        else:
            bridge.set_bpm_range(bpm.lo, bpm.hi)
    if "discovery_every" in data:
        every = _checked(DiscoveryBody, "every", data["discovery_every"], "discovery_every")
        if every is not _INVALID:
            # The same setter as the route, so a saved rate also turns the
            # Discovery button on and the rate fires after a restart.
            bridge.set_discovery_every(every)
    if "discovery_enabled" in data:
        # The Discovery button has no request body, so like ``is_muted`` the
        # saved value only has to be a real Boolean.  It stays off without a rate.
        try:
            enabled = _STRICT_BOOL.validate_python(data["discovery_enabled"], strict=True)
        except ValidationError:
            _warn("discovery_enabled", data["discovery_enabled"])
        else:
            player = bridge.player
            player._state.discovery_enabled = enabled and player._discovery_every is not None
    _restore_eq(bridge, _section(data, "eq"))


def save_from_player(settings: dict, index_dir: Path | None) -> None:
    """Write *settings* (PlayerBridge.get_settings shape) to disk atomically.

    The ``available_presets`` list and the playback values no settings route
    can set (``library_size`` and the config-only options) are left out.

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
        "discovery_enabled": settings.get("discovery_enabled", False),
        "eq": settings.get("eq", {"low": 1.0, "mid": 1.0, "high": 1.0}),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not _backup_v1_state(path):
            return
        atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False))
    except OSError as exc:
        logger.warning("Failed to save web_state.json: %s", exc)
