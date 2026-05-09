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
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def state_file_for(index_dir: Path | None) -> Path | None:
    """Return the canonical state-file path for *index_dir*, or ``None``."""
    if index_dir is None:
        return None
    return Path(index_dir) / "web_state.json"


def _restore_preset(player: Any, data: dict) -> None:
    """Restore the active preset (silent on lookup failure)."""
    if not data.get("preset"):
        return
    import contextlib

    from autodj.presets import get_preset

    with contextlib.suppress(ValueError):
        player._preset = get_preset(data["preset"], player._cfg.presets)


def _restore_djmix(cfg: Any, data: dict) -> None:
    """Restore the saved DJ-mix flags onto *cfg*."""
    djmix = data.get("djmix")
    if not isinstance(djmix, dict):
        return
    for k, v in djmix.items():
        if hasattr(cfg.djmix, k):
            setattr(cfg.djmix, k, bool(v))


def _restore_transition(cfg: Any, data: dict) -> None:
    """Restore the saved transition effect."""
    if isinstance(data.get("transition"), str):
        cfg.transitions.effect = data["transition"]


def _restore_playback_floats(cfg: Any, pb: dict) -> None:
    """Restore numeric playback fields (crossfade lengths, mood-arc hours)."""
    if "crossfade_seconds" in pb:
        cfg.playback.crossfade_seconds = max(0.0, float(pb["crossfade_seconds"]))
    if "fade_in_seconds" in pb:
        cfg.playback.fade_in_seconds = max(0.0, float(pb["fade_in_seconds"]))
    if "mood_arc_hours" in pb:
        cfg.playback.mood_arc_hours = max(0.25, float(pb["mood_arc_hours"]))


def _restore_playback_bools(cfg: Any, player: Any, pb: dict) -> None:
    """Restore boolean playback flags onto *cfg* / *player*."""
    if "crossfade_eq_duck" in pb:
        cfg.playback.crossfade_eq_duck = bool(pb["crossfade_eq_duck"])
    if "smart_shuffle" in pb:
        player._smart_shuffle = bool(pb["smart_shuffle"])
    if "pure_shuffle" in pb:
        player._pure_shuffle = bool(pb["pure_shuffle"])
    if "anchor_to_seed" in pb:
        player._anchor_to_seed = bool(pb["anchor_to_seed"])
    if "replaygain_enabled" in pb:
        cfg.replaygain.enabled = bool(pb["replaygain_enabled"])
    if "show_lyrics" in pb:
        cfg.playback.show_lyrics = bool(pb["show_lyrics"])
    if "enable_daypart" in pb:
        cfg.playback.enable_daypart = bool(pb["enable_daypart"])
    if "import_external_cues" in pb:
        cfg.playback.import_external_cues = bool(pb["import_external_cues"])
    if "key_prefer_flats" in pb:
        cfg.playback.key_prefer_flats = bool(pb["key_prefer_flats"])


def _restore_mood_arc(cfg: Any, player: Any, pb: dict) -> None:
    """Restore mood-arc enabled flag, re-anchoring the start time."""
    if "enable_mood_arc" not in pb:
        return
    cfg.playback.enable_mood_arc = bool(pb["enable_mood_arc"])
    if cfg.playback.enable_mood_arc:
        from autodj.mood_arc import make_default_arc

        player._mood_arc = make_default_arc(
            duration_hours=getattr(cfg.playback, "mood_arc_hours", 3.0),
        )


def _restore_validated_strings(cfg: Any, pb: dict) -> None:
    """Restore validator-gated string fields (transition_mode, key_notation)."""
    if "transition_mode" in pb:
        from autodj.config import _validate_transition_mode

        try:
            cfg.playback.transition_mode = _validate_transition_mode(str(pb["transition_mode"]))
        except ValueError as exc:
            logger.warning("ignoring invalid transition_mode in web_state.json: %s", exc)
    if "key_notation" in pb:
        from autodj.config import _validate_key_notation

        try:
            cfg.playback.key_notation = _validate_key_notation(str(pb["key_notation"]))
        except ValueError as exc:
            logger.warning("ignoring invalid key_notation in web_state.json: %s", exc)


def _restore_bpm_range(player: Any, data: dict) -> None:
    """Restore the saved BPM range on *player*."""
    bpm = data.get("bpm_range")
    if not isinstance(bpm, dict):
        return
    lo = bpm.get("lo")
    hi = bpm.get("hi")
    if lo is not None and hi is not None and lo < hi:
        player._bpm_range = (float(lo), float(hi))
    else:
        player._bpm_range = None


def _restore_discovery(player: Any, data: dict) -> None:
    """Restore the saved discovery interval on *player*."""
    if "discovery_every" not in data:
        return
    every = data["discovery_every"]
    if every and int(every) > 0:
        player._discovery_every = int(every)
    else:
        player._discovery_every = None


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
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("web_state.json unreadable, ignoring: %s", exc)
        return

    cfg = player._cfg
    _restore_preset(player, data)
    _restore_transition(cfg, data)
    _restore_djmix(cfg, data)
    pb = data.get("playback")
    if isinstance(pb, dict):
        _restore_playback_floats(cfg, pb)
        _restore_playback_bools(cfg, player, pb)
        _restore_mood_arc(cfg, player, pb)
        _restore_validated_strings(cfg, pb)
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
    payload = {k: v for k, v in settings.items() if k != "available_presets"}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Failed to save web_state.json: %s", exc)
