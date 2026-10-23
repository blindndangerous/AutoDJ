"""Persistent web-UI settings (``web_state.json``).

Settings the user changes in the **browser** — preset, transition
effect, DJ-mix options, playback settings, volume and mute, stream
bitrate and station name, BPM range, discovery rate, EQ, whether discovery
is on, and whether the station takes listener requests and announcer breaks
and how — are written to
``<index_dir>/<name>/web_state.json`` so the next `autodj serve` boot
restores them.

The on-disk format mirrors the dict returned by
``PlayerBridge.get_settings()``, keeping only the fields a settings route
can set.  On restore every saved field is checked on its own against the
rule of the route that sets it (:mod:`autodj.settings_bodies`) and applied
through the same :class:`~autodj._bridge.PlayerBridge` setter: a bad value
is skipped with a warning and the other saved settings still apply.  A
saved value wins over config.toml, which gives only the starting value.
Whether the station takes listener requests and breaks, and how, is saved
only once its own card has saved it for the station; until then other
saves leave it out and config.toml stays in charge.  The
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
from autodj.repeat_settings import ignore_unknown_repeat_keys
from autodj.settings_bodies import (
    PAGE_SAVED_BREAKS,
    PAGE_SAVED_REQUESTS,
    BpmRangeBody,
    BreakSettingsBody,
    DiscoveryBody,
    DjMixBody,
    EqBody,
    PlaybackSettingsBody,
    PresetBody,
    RequestSettingsBody,
    StreamSettingsBody,
    TransitionBody,
    VolumeBody,
)

if TYPE_CHECKING:
    from autodj._bridge import PlayerBridge

logger = logging.getLogger(__name__)


#: Version 2 dropped the redundant ``harmonic_mixing`` toggle. Version 1
#: migration honors that toggle so ``harmonic_mixing: false`` cannot silently
#: enable key filtering through a stored ``harmonic_mode`` value.
STATE_VERSION = 2

#: Listener request settings the operator's Listener requests card saves
#: (``/api/requests/settings``), by their ``[requests]`` names.
REQUEST_SETTING_KEYS = (
    "requests_enabled",
    "requests_approval",
    "requests_bridges_enabled",
    "requests_max_bridges",
    "requests_bridge_cost_threshold",
    "requests_no_smooth_spot",
)
#: The two of them that config.toml keeps until the card first saves them.
_REQUEST_SWITCH_KEYS = frozenset({"requests_enabled", "requests_approval"})
#: Announcer break settings Settings, Breaks saves (``/api/breaks/settings``),
#: by their ``[breaks]`` names.  ``breaks_duck_db`` may be null: the voice
#: liner duck depth.
BREAK_SETTING_KEYS = (
    "breaks_enabled",
    "breaks_dj_placement",
    "breaks_min_intro_seconds",
    "breaks_duck_db",
)

#: Saved under ``playback`` but set by their own routes (``/api/volume``,
#: ``/api/mute``, ``/api/stream/settings``).
_OWN_ROUTE_PLAYBACK_FIELDS = frozenset(
    {
        "volume",
        "is_muted",
        "stream_bitrate",
        "stream_continuous",
        "stream_station_name",
        "stream_genre",
        "stream_description",
        "stream_url",
        "nowplaying_show_next",
        "stream_max_listeners_per_address",
        *REQUEST_SETTING_KEYS,
        *BREAK_SETTING_KEYS,
        "stream_aac_bitrate",
        "stream_opus_bitrate",
        "stream_stats_days",
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


def migrate_state_file(path: Path, *, apply: bool = False) -> bool:
    """Upgrade a v1 web-state file explicitly, preserving its original bytes.

    Return whether the file needs migration. Preview mode validates and reports
    without writing; applying writes the v1 recovery copy before replacing the
    file. This is used by ``autodj migrate-index`` so serving can treat saved
    state as current-format data only.
    """
    try:
        original = path.read_bytes()
        data = json.loads(original)
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    version = data.get("schema_version")
    if type(version) is int and version == STATE_VERSION:
        return False
    if type(version) is not int or version != 1:
        raise ValueError(f"{path} has unsupported schema_version {version!r}")
    if not apply:
        return True

    backup = path.with_name(f"{path.name}.v1.bak")
    if backup.exists():
        if backup.read_bytes() != original:
            raise ValueError(f"recovery copy already exists with different data: {backup}")
    else:
        atomic_write(backup, original)
    atomic_write(path, json.dumps(_migrate_v1(data), indent=2, ensure_ascii=False))
    return True


def _can_save_state(path: Path) -> bool:
    """Do not overwrite old settings that have not been explicitly migrated."""
    try:
        data = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return True
    if (
        isinstance(data, dict)
        and type(data.get("schema_version")) is int
        and data["schema_version"] == 1
    ):
        logger.warning("Keeping old web_state.json; run `autodj migrate-index --apply` first")
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
    if field not in model.model_fields:
        logger.warning("ignoring unknown %s in web_state.json", saved_as or field)
        return _INVALID
    if model is PlaybackSettingsBody and field == "repeat_rules":
        value = ignore_unknown_repeat_keys(value, "web_state.json playback.repeat_rules")
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


def _restore_preset(settings: Any, value: object) -> None:
    """Restore the saved preset, or none; an unknown preset name is skipped."""
    name = _checked(PresetBody, "name", value, "preset")
    if name is _INVALID:
        return
    if not name:
        settings._preset = None
        return
    from autodj.presets import get_preset

    try:
        settings._preset = get_preset(name, settings.cfg.presets)
    except ValueError:
        _warn("preset", value)


def _restore_playback(settings: Any, playback: dict[str, Any]) -> None:
    """Restore the playback section through the playback-settings route's setter."""
    if "volume" in playback:
        volume = _checked(VolumeBody, "volume", playback["volume"])
        if volume is not _INVALID:
            settings.set_volume(volume)
    if "is_muted" in playback:
        try:
            settings.set_muted(_STRICT_BOOL.validate_python(playback["is_muted"], strict=True))
        except ValidationError:
            _warn("is_muted", playback["is_muted"])
    if "stream_bitrate" in playback:
        # The stream is not running yet, so only the configured rate changes.
        bitrate = _checked(
            StreamSettingsBody, "bitrate", playback["stream_bitrate"], "stream_bitrate"
        )
        if bitrate is not _INVALID:
            settings.cfg.stream.bitrate = bitrate
    if "stream_continuous" in playback:
        continuous = _checked(
            StreamSettingsBody, "continuous", playback["stream_continuous"], "stream_continuous"
        )
        if continuous is not _INVALID:
            settings.cfg.stream.continuous = continuous
    if "stream_station_name" in playback:
        name = _checked(
            StreamSettingsBody,
            "station_name",
            playback["stream_station_name"],
            "stream_station_name",
        )
        if name is not _INVALID and name is not None:
            settings.cfg.stream.station_name = name
    for field, key in (
        ("genre", "stream_genre"),
        ("description", "stream_description"),
        ("url", "stream_url"),
    ):
        if key in playback:
            value = _checked(StreamSettingsBody, field, playback[key], key)
            if value is not _INVALID and value is not None:
                setattr(settings.cfg.stream, field, value)
    if "nowplaying_show_next" in playback:
        show_next = _checked(
            StreamSettingsBody,
            "show_next",
            playback["nowplaying_show_next"],
            "nowplaying_show_next",
        )
        if show_next is not _INVALID:
            settings.cfg.nowplaying.show_next = show_next
    if "stream_max_listeners_per_address" in playback:
        limit = _checked(
            StreamSettingsBody,
            "max_listeners_per_address",
            playback["stream_max_listeners_per_address"],
            "stream_max_listeners_per_address",
        )
        if limit is not _INVALID and limit is not None:
            settings.cfg.stream.max_listeners_per_address = limit
    for field in ("aac_bitrate", "opus_bitrate", "stats_days"):
        key = f"stream_{field}"
        if key in playback:
            # The stream is not running yet, so only the configured value changes.
            value = _checked(StreamSettingsBody, field, playback[key], key)
            if value is not _INVALID and value is not None:
                setattr(settings.cfg.stream, field, value)

    for key in REQUEST_SETTING_KEYS:
        if key in playback:
            field = key.removeprefix("requests_")
            value = _checked(RequestSettingsBody, field, playback[key], key)
            if value is not _INVALID and value is not None:
                if key in _REQUEST_SWITCH_KEYS:
                    # Saved by the Listener requests card: kept on later saves.
                    settings.page_saved.add(PAGE_SAVED_REQUESTS)
                    _note_switch(settings, key, getattr(settings.cfg.requests, field), value)
                setattr(settings.cfg.requests, field, value)
    _restore_breaks(settings, getattr(settings.cfg, "breaks", None), playback)

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
            preset_path(settings.cfg.index.shared_dir, values["stereotool_preset"])
        except (ValueError, FileNotFoundError):
            _warn("stereotool_preset", values.pop("stereotool_preset"))
    settings.set_playback_settings(PlaybackSettingsBody.model_validate(values), restoring=True)


#: How the log names each saved switch and its values.
_SWITCH_WORDS: dict[str, tuple[str, dict[object, str]]] = {
    "requests_enabled": ("Listener requests", {True: "on", False: "off"}),
    "requests_approval": (
        "Listener request approval",
        {"auto": "automatic", "manual": "each one approved"},
    ),
    "breaks_enabled": ("Breaks", {True: "on", False: "off"}),
}


def _note_switch(bridge: Any, key: str, configured: object, saved: object) -> None:
    """Log, once at start, a saved switch that differs from config.toml's."""
    words = _SWITCH_WORDS.get(key)
    if words is None or configured == saved:
        return
    what, values = words
    slug = getattr(getattr(bridge, "paths", None), "slug", None) or "default"
    logger.info(
        "%s %s for %s (saved in Settings; config.toml says %s)",
        what,
        values.get(saved, saved),
        slug,
        values.get(configured, configured),
    )


def _restore_breaks(settings: Any, breaks: Any, playback: dict[str, Any]) -> None:
    """Restore the saved announcer break settings over the ``[breaks]`` ones.

    Any of them saved marks the group as Settings, Breaks', so later saves
    keep writing it.
    """
    if breaks is None:
        return
    for key in BREAK_SETTING_KEYS:
        if key not in playback:
            continue
        field = key.removeprefix("breaks_")
        saved = playback[key]
        if field == "duck_db" and saved is None:
            # Null: the voice liner duck depth, as an unset [breaks] duck_db.
            breaks.duck_db = None
            settings.page_saved.add(PAGE_SAVED_BREAKS)
            continue
        value = _checked(BreakSettingsBody, field, saved, key)
        if value is not _INVALID and value is not None:
            settings.page_saved.add(PAGE_SAVED_BREAKS)
            _note_switch(settings, key, getattr(breaks, field), value)
            setattr(breaks, field, value)


def _restore_eq(settings: Any, eq: dict[str, Any]) -> None:
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
        settings.set_eq(**values)


def load_into_settings(settings: Any, index_dir: Path | None) -> None:
    """Restore saved settings through a Player-independent settings owner.

    Version 1 state needs explicit migration. Unsupported versions are ignored;
    current fields continue to be validated individually.

    Args:
        settings: The station's :class:`~autodj.station_settings.StationSettings`.
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
    if type(version) is int and version == 1:
        raise ValueError(f"{path} needs migration; run `autodj migrate-index --apply` first")
    elif type(version) is not int or version != STATE_VERSION:
        logger.warning(
            "ignoring web_state.json: schema_version must be %d, got %r",
            STATE_VERSION,
            version,
        )
        return
    for field in data.keys() - _TOP_LEVEL_FIELDS:
        logger.warning("ignoring unknown %s in web_state.json", field)

    if "preset" in data:
        _restore_preset(settings, data["preset"])
    if "transition" in data:
        effect = _checked(TransitionBody, "effect", data["transition"], "transition")
        if effect is not _INVALID:
            try:
                settings.set_transition(effect)
            except ValueError:
                _warn("transition", effect)
    djmix: dict[str, Any] = {}
    for field, value in _section(data, "djmix").items():
        checked = _checked(DjMixBody, field, value)
        if checked is not _INVALID:
            djmix[field] = checked
    settings.set_djmix(**djmix)
    _restore_playback(settings, _section(data, "playback"))
    if "bpm_range" in data:
        try:
            bpm = BpmRangeBody.model_validate(data["bpm_range"], strict=True)
        except ValidationError:
            _warn("bpm_range", data["bpm_range"])
        else:
            settings.set_bpm_range(bpm.lo, bpm.hi)
    if "discovery_every" in data:
        every = _checked(DiscoveryBody, "every", data["discovery_every"], "discovery_every")
        if every is not _INVALID:
            # The same setter as the route, so a saved rate also turns the
            # Discovery button on and the rate fires after a restart.
            settings.set_discovery_every(every, restoring=True)
    if "discovery_enabled" in data:
        # The Discovery button has no request body, so like ``is_muted`` the
        # saved value only has to be a real Boolean.  It stays off without a rate.
        try:
            enabled = _STRICT_BOOL.validate_python(data["discovery_enabled"], strict=True)
        except ValidationError:
            _warn("discovery_enabled", data["discovery_enabled"])
        else:
            settings._discovery_enabled = enabled and settings._discovery_every is not None
            if settings.player is not None:
                settings.player._state.discovery_enabled = settings._discovery_enabled
    _restore_eq(settings, _section(data, "eq"))


def load_into_bridge(bridge: PlayerBridge, index_dir: Path | None) -> None:
    """Compatibility wrapper for callers that still hold a PlayerBridge."""
    bridge.settings.paths = bridge.paths or bridge.settings.paths
    load_into_settings(bridge.settings, index_dir)


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
        {
            key: value
            for key, value in playback.items()
            if key in PERSISTED_PLAYBACK_FIELDS and key != "pure_shuffle"
        }
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
        if path.exists() and not _can_save_state(path):
            return
        atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False))
    except OSError as exc:
        logger.warning("Failed to save web_state.json: %s", exc)
