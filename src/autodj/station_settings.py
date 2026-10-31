"""Saved, editable settings for a station, independent of its audio runtime."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from autodj.settings_bodies import PAGE_SAVED_BREAKS, PAGE_SAVED_REQUESTS, PlaybackSettingsBody

logger = logging.getLogger(__name__)


def _request_settings(requests: Any) -> dict[str, Any]:
    """The listener request settings the operator's card saves (#49), when the station has them."""
    if requests is None:
        return {}
    return {
        "requests_bridges_enabled": bool(requests.bridges_enabled),
        "requests_max_bridges": int(requests.max_bridges),
        "requests_bridge_cost_threshold": float(requests.bridge_cost_threshold),
        "requests_no_smooth_spot": requests.no_smooth_spot,
    }


def _request_switches(requests: Any, page_saved: set[str]) -> dict[str, Any]:
    """Whether the station takes requests and how they are approved, once the card saved them.

    Until the Listener requests card has saved them for this station,
    ``[requests]`` in config.toml stays in charge: no other save (volume,
    EQ ...) writes them to ``web_state.json``.
    """
    if requests is None or PAGE_SAVED_REQUESTS not in page_saved:
        return {}
    return {
        "requests_enabled": bool(requests.enabled),
        "requests_approval": requests.approval,
    }


def _break_settings(breaks: Any, page_saved: set[str]) -> dict[str, Any]:
    """The announcer break settings, once Settings, Breaks saved them for the station (#88).

    Until then ``[breaks]`` in config.toml stays in charge, as for
    :func:`_request_switches`.
    """
    if breaks is None or PAGE_SAVED_BREAKS not in page_saved:
        return {}
    return {
        "breaks_enabled": bool(breaks.enabled),
        "breaks_dj_placement": breaks.dj_placement,
        "breaks_min_intro_seconds": float(breaks.min_intro_seconds),
        "breaks_duck_db": None if breaks.duck_db is None else float(breaks.duck_db),
    }


class StationSettings:
    """Own a station's config-backed and web-state settings.

    A Player can attach for effects that only make sense while running. The
    settings and their validation remain here when the station is stopped.
    """

    def __init__(self, cfg: Any, library: Any = None, sim: Any = None, paths: Any = None) -> None:
        self.cfg = cfg
        self.library = library
        self.sim = sim
        self.paths = paths
        self.player: Any = None
        self._preset: Any = None
        self._bpm_range: tuple[float, float] | None = None
        self._anchor_to_seed = False
        self._eq_low = self._eq_mid = self._eq_high = 1.0
        self._discovery_every = getattr(cfg.playback, "discovery_every", None)
        self._discovery_enabled = False
        self._volume = 1.0
        self._is_muted = False
        self.page_saved: set[str] = set()
        self.related_artists_problem: str | None = None
        self.related_tracks_problem: str | None = None
        self._lock = threading.RLock()

    def attach(self, player: Any) -> None:
        """Attach live-only effects and seed its PlayerState from saved values."""
        self.player = player
        self.library = self.library or getattr(player, "_library", None)
        self.sim = self.sim or getattr(player, "_sim", None)
        player._settings = self
        player._state.volume = self._volume
        player._state.is_muted = self._is_muted
        player._state.discovery_enabled = (
            self._discovery_enabled and self._discovery_every is not None
        )

    @property
    def state_dir(self) -> Path | None:
        if self.paths is not None:
            return Path(self.paths.state_dir)
        directory = getattr(getattr(self.cfg, "index", None), "stations_dir", None)
        return Path(directory) / "default" if directory is not None else None

    def load(self) -> None:
        from autodj.runtime_state import load_into_settings

        load_into_settings(self, self.state_dir)

    def load_persistent_state(self) -> None:
        """Compatibility name shared with PlayerBridge."""
        self.load()

    def save(self, *, strict: bool = False) -> None:
        from autodj.runtime_state import save_from_player

        save_from_player(self.get_settings(), self.state_dir, raise_on_error=strict)

    def save_persistent_state(self, *, strict: bool = False) -> None:
        """Compatibility name shared with PlayerBridge."""
        self.save(strict=strict)

    def get_settings(self) -> dict[str, Any]:
        """Return the same settings document shape served by PlayerBridge."""
        cfg, pb = self.cfg, self.cfg.playback
        from autodj.presets import BUILTIN_PRESETS

        names = sorted(set(BUILTIN_PRESETS) | set(cfg.presets))
        library_dir = cfg.index.library_dir
        related_problem = self.related_artists_problem
        tracks_problem = self.related_tracks_problem
        return {
            "preset": self._preset.name if self._preset else None,
            "available_presets": names,
            "transition": cfg.transitions.effect,
            "djmix": {
                "harmonic_mode": cfg.djmix.harmonic_mode,
                "beatmatch": cfg.djmix.beatmatch,
                "drop_mix": cfg.djmix.drop_mix,
                "key_shift": cfg.djmix.key_shift,
                "phrase_align": cfg.djmix.phrase_align,
                "outro_intro_align": cfg.djmix.outro_intro_align,
                "filter_sweep": cfg.djmix.filter_sweep,
                "vocal_guard": bool(cfg.djmix.vocal_guard),
                "phrase_bars": int(cfg.djmix.phrase_bars),
                "beatmatch_max_stretch": float(cfg.djmix.beatmatch_max_stretch),
                "beatmatch_glide_bars": int(cfg.djmix.beatmatch_glide_bars),
                "filter_sweep_floor_hz": float(cfg.djmix.filter_sweep_floor_hz),
            },
            "playback": {
                "crossfade_seconds": pb.crossfade_seconds,
                "fade_in_seconds": pb.fade_in_seconds,
                "music_speed_percent": float(pb.music_speed_percent),
                "crossfade_eq_duck": pb.crossfade_eq_duck,
                "pure_shuffle": pb.pick_mode == "random",
                "pick_mode": pb.pick_mode,
                "anchor_to_seed": self._anchor_to_seed,
                "replaygain_enabled": cfg.replaygain.enabled,
                "loudness_processing": pb.loudness_processing
                or ("replaygain" if cfg.replaygain.enabled else "off"),
                "transition_mode": pb.transition_mode,
                "post_queue_seed": pb.post_queue_seed,
                "key_notation": pb.key_notation,
                "key_prefer_flats": bool(pb.key_prefer_flats),
                "show_lyrics": pb.show_lyrics,
                "beat_sync_fx": bool(pb.beat_sync_fx),
                "repeat_rules": pb.repeat_rules.model_dump(),
                "related_artists_problem": related_problem
                if isinstance(related_problem, str)
                else None,
                "related_artists_default_file": str(
                    (Path(library_dir) / "related-artists.toml").resolve()
                )
                if library_dir is not None
                else "",
                "related_tracks_problem": tracks_problem
                if isinstance(tracks_problem, str)
                else None,
                "related_tracks_default_file": str(
                    (Path(library_dir) / "related-tracks.toml").resolve()
                )
                if library_dir is not None
                else "",
                "transition_wet_mix": float(cfg.transitions.wet_mix),
                "replaygain_target_db": float(cfg.replaygain.target_db),
                "replaygain_max_clip_safe_gain": float(cfg.replaygain.max_clip_safe_gain),
                "crossfade_bass_cutoff_hz": float(pb.crossfade_bass_cutoff_hz),
                "pick_top_k": int(pb.pick_top_k),
                "hour_fit": bool(pb.hour_fit),
                "hour_fit_window_minutes": int(pb.hour_fit_window_minutes),
                "pick_temperature": float(pb.pick_temperature),
                "stereotool_agc_drive_db": float(pb.stereotool_agc_drive_db),
                "intro_lift_max_db": float(pb.intro_lift_max_db),
                "stereotool_input_trim_db": float(pb.stereotool_input_trim_db),
                "stereotool_preset": pb.stereotool_preset,
                "library_size": len(self.library.entries_snapshot())
                if self.library is not None
                else int(self.sim.ntotal if self.sim else 0),
                "key_sync_fx": bool(pb.key_sync_fx),
                "beatmatch_on_skip": bool(pb.beatmatch_on_skip),
                "skip_style": pb.skip_style,
                "prefetch_next_track": pb.prefetch_next_track,
                "silence_trigger_crossfade": pb.silence_trigger_crossfade,
                "liners_enabled": bool(pb.liners_enabled),
                "liners_every_n_songs": pb.liners_every_n_songs,
                "liners_every_minutes": pb.liners_every_minutes,
                "liners_random_min_minutes": pb.liners_random_min_minutes,
                "liners_random_max_minutes": pb.liners_random_max_minutes,
                "liners_pick_mode": pb.liners_pick_mode,
                "liners_duck_db": pb.liners_duck_db,
                "liners_talk_up": bool(pb.liners_talk_up),
                "matched_intro_only_without_liner": bool(
                    getattr(pb, "matched_intro_only_without_liner", False)
                ),
                "stream_bitrate": int(cfg.stream.bitrate),
                "stream_continuous": bool(cfg.stream.continuous),
                "stream_station_name": cfg.stream.station_name,
                "stream_genre": getattr(cfg.stream, "genre", ""),
                "stream_description": getattr(cfg.stream, "description", ""),
                "stream_url": getattr(cfg.stream, "url", ""),
                "nowplaying_show_next": cfg.nowplaying.show_next,
                "dmca_enabled": cfg.dmca.enabled,
                "stream_max_listeners_per_address": cfg.stream.max_listeners_per_address,
                **_request_settings(getattr(cfg, "requests", None)),
                **_request_switches(getattr(cfg, "requests", None), self.page_saved),
                **_break_settings(getattr(cfg, "breaks", None), self.page_saved),
                "stream_aac_bitrate": int(cfg.stream.aac_bitrate),
                "stream_opus_bitrate": int(cfg.stream.opus_bitrate),
                "stream_stats_days": int(cfg.stream.stats_days),
                "volume": self._volume,
                "is_muted": self._is_muted,
            },
            "bpm_range": {
                "lo": self._bpm_range[0] if self._bpm_range else None,
                "hi": self._bpm_range[1] if self._bpm_range else None,
            },
            "discovery_every": self._discovery_every,
            "discovery_enabled": self._discovery_enabled,
            "eq": self.get_eq(),
        }

    def editable_values(self) -> dict[str, dict[str, Any]]:
        """Return stable bodies for editable groups, without runtime status fields."""
        from autodj.settings_bodies import DjMixBody, PlaybackSettingsBody

        settings = self.get_settings()
        playback = settings["playback"]
        return {
            "preset": {"name": settings["preset"]},
            "transition": {"effect": settings["transition"]},
            "djmix": {
                key: value
                for key, value in settings["djmix"].items()
                if key in DjMixBody.model_fields and value is not None
            },
            "playback": {
                key: value
                for key, value in playback.items()
                if key in PlaybackSettingsBody.model_fields and value is not None
            },
            "bpm_range": settings["bpm_range"],
            "discovery": {"every": settings["discovery_every"]},
            "eq": settings["eq"],
        }

    def validate_settings_patch(self, changes: Any) -> Any:
        """Validate all core settings and file-backed choices before mutation."""
        from autodj.presets import get_preset
        from autodj.settings_bodies import StationSettingsChangesBody
        from autodj.transitions import TRANSITION_EFFECT_NAMES

        patch = StationSettingsChangesBody.model_validate(changes)
        if not any(
            getattr(patch, name) is not None for name in StationSettingsChangesBody.model_fields
        ):
            raise ValueError("At least one settings group is required")
        if patch.preset is not None and patch.preset.name:
            get_preset(patch.preset.name, self.cfg.presets)
        if (
            patch.transition is not None
            and patch.transition.effect.lower() not in TRANSITION_EFFECT_NAMES
        ):
            raise ValueError(f"unknown transition effect: {patch.transition.effect}")
        if patch.playback is not None:
            if patch.playback.stereotool_preset:
                from autodj.stereotool_presets import preset_path

                preset_path(self.cfg.index.shared_dir, patch.playback.stereotool_preset)
            self._read_related_artists(patch.playback, restoring=False)
            self._read_related_tracks(patch.playback, restoring=False)
        return patch

    def apply_settings_patch(self, changes: Any) -> None:
        """Apply validated groups and persist them through the station settings store."""
        with self._lock:
            patch = self.validate_settings_patch(changes)
            # Playback is the only setter here that can reject file-backed choices;
            # run it before the simple in-memory groups so validation cannot leave a
            # subset applied.
            if patch.playback is not None:
                self.set_playback_settings(patch.playback, refresh=False)
            if patch.preset is not None:
                self.set_preset(patch.preset.name, refresh=False)
            if patch.transition is not None:
                self.set_transition(patch.transition.effect)
            if patch.djmix is not None:
                self.set_djmix(**patch.djmix.model_dump(exclude_none=True))
            if patch.bpm_range is not None:
                self.set_bpm_range(patch.bpm_range.lo, patch.bpm_range.hi)
            if patch.discovery is not None:
                self.set_discovery_every(patch.discovery.every, refresh=False)
            if patch.eq is not None:
                self.set_eq(**patch.eq.model_dump(exclude_none=True))
            self.save(strict=True)
            if (
                self.player is not None
                and any(
                    getattr(patch, name) is not None for name in ("playback", "preset", "discovery")
                )
                and (refresh_playout := getattr(self.player, "refresh_playout", None))
            ):
                refresh_playout()

    def set_preset(self, name: str | None, *, refresh: bool = True) -> None:
        from autodj.local_programme_playout import require_automation

        require_automation(self.player)
        if not name:
            self._preset = None
        else:
            from autodj.presets import get_preset

            try:
                self._preset = get_preset(name, self.cfg.presets)
            except ValueError:
                return
            if self._preset.discovery_every and self._discovery_every is None:
                self._discovery_every = self._preset.discovery_every
        if (
            refresh
            and self.player is not None
            and (refresh_playout := getattr(self.player, "refresh_playout", None))
        ):
            refresh_playout()

    def set_transition(self, effect: str) -> None:
        from autodj.local_programme_playout import require_automation

        require_automation(self.player)
        from autodj.transitions import TRANSITION_EFFECT_NAMES

        name = effect.lower()
        if name not in TRANSITION_EFFECT_NAMES:
            raise ValueError(f"unknown transition effect: {effect}")
        self.cfg.transitions.effect = name

    def set_djmix(self, **flags: Any) -> None:
        from autodj.local_programme_playout import require_automation

        require_automation(self.player)
        from autodj.dj_meta import HARMONIC_MODES

        for key, value in flags.items():
            if value is None:
                continue
            if key == "harmonic_mode":
                mode = str(value).lower()
                if mode in HARMONIC_MODES:
                    self.cfg.djmix.harmonic_mode = mode
            elif key in {"phrase_bars", "beatmatch_glide_bars"}:
                setattr(self.cfg.djmix, key, int(value))
            elif key in {"beatmatch_max_stretch", "filter_sweep_floor_hz"}:
                setattr(self.cfg.djmix, key, float(value))
            elif hasattr(self.cfg.djmix, key):
                setattr(self.cfg.djmix, key, bool(value))

    def set_playback_settings(
        self,
        body: PlaybackSettingsBody,
        *,
        restoring: bool = False,
        refresh: bool = True,
    ) -> None:
        from autodj.local_programme_playout import require_automation

        require_automation(self.player)
        values = body.model_dump(exclude_none=True)
        legacy_shuffle = values.pop("pure_shuffle", None)
        if legacy_shuffle is not None and "pick_mode" not in values:
            values["pick_mode"] = "random" if legacy_shuffle else "similarity"
        from autodj.settings_bodies import validate_playback_choices

        validate_playback_choices(values)
        if body.repeat_rules is not None:
            values["repeat_rules"] = body.repeat_rules
        if "stereotool_preset" in values:
            from autodj.stereotool_presets import preset_path

            preset_path(self.cfg.index.shared_dir, values["stereotool_preset"])
        related_artists = self._read_related_artists(body, restoring=restoring)
        related_tracks = self._read_related_tracks(body, restoring=restoring)
        from autodj.player import settings_lock

        player = self.player
        # File and history reads must not block the audio settings lock.
        with settings_lock(player) if player is not None else self._lock:
            if related_artists is not None:
                self._related_artists, self.related_artists_problem = related_artists
            if related_tracks is not None:
                self._related_tracks, self.related_tracks_problem = related_tracks
            targets = {
                "anchor_to_seed": (self, "_anchor_to_seed"),
                "replaygain_enabled": (self.cfg.replaygain, "enabled"),
                "replaygain_target_db": (self.cfg.replaygain, "target_db"),
                "replaygain_max_clip_safe_gain": (self.cfg.replaygain, "max_clip_safe_gain"),
                "transition_wet_mix": (self.cfg.transitions, "wet_mix"),
            }
            for key, value in values.items():
                target, attr = targets.get(key, (self.cfg.playback, key))
                setattr(target, attr, value)
            if values.get("pick_mode") == "clock":
                self.cfg.playback.transition_mode = "saved_points"
            if "loudness_processing" in values:
                self.cfg.replaygain.enabled = values["loudness_processing"] == "replaygain"
            elif "replaygain_enabled" in values:
                self.cfg.playback.loudness_processing = (
                    "replaygain" if values["replaygain_enabled"] else "off"
                )
            for key in (
                "liners_every_n_songs",
                "liners_every_minutes",
                "liners_random_min_minutes",
                "liners_random_max_minutes",
            ):
                if values.get(key) == 0:
                    setattr(self.cfg.playback, key, None)
            if player is not None:
                if related_tracks is not None:
                    player._related_tracks = related_tracks[0]
                if related_artists is not None:
                    player._related_artists = related_artists[0]
                    if hasattr(player, "_key_cache_lock"):
                        with player._key_cache_lock:
                            player._key_cache = None
                if (
                    values.get("anchor_to_seed")
                    and not getattr(player, "_seed_path", None)
                    and player._state.current_track
                ):
                    player._seed_path = player._state.current_track.path
                if "post_queue_seed" in values and self.cfg.playback.post_queue_seed != "pre_queue":
                    player._state.pre_queue_seed = None
                if values.get("show_lyrics") is False:
                    player._current_lyrics = []
                    player._current_lyrics_plain = ""
                if "repeat_rules" in values:
                    from autodj.player import apply_repeat_windows

                    apply_repeat_windows(player)
        if player is not None:
            picking_changed = any(hasattr(self.cfg.playback, key) for key in values) or bool(
                {"pick_mode", "anchor_to_seed"} & values.keys()
            )
            if (
                picking_changed
                and not restoring
                and refresh
                and (refresh_playout := getattr(player, "refresh_playout", None))
            ):
                refresh_playout()
            if (
                "repeat_rules" in values
                and (ledger := getattr(player, "_ledger", None)) is not None
                and hasattr(ledger, "recent_plays")
            ):
                player.restore_repeat_history(ledger, refresh=refresh)

    def _read_related_artists(self, body: PlaybackSettingsBody, *, restoring: bool) -> Any:
        if body.repeat_rules is None:
            return None
        from autodj.related_artist_file import load_related_file

        try:
            related, problem = load_related_file(
                self.cfg.index.library_dir,
                "artists",
                enabled=body.repeat_rules.use_related_artists,
                restoring=restoring,
            )
        except ValueError as exc:
            self.related_artists_problem = str(exc)
            raise
        if problem:
            logger.warning("Related artists file was not used: %s", problem)
        return related, problem

    def _read_related_tracks(self, body: PlaybackSettingsBody, *, restoring: bool) -> Any:
        if body.repeat_rules is None:
            return None
        from autodj.related_artist_file import load_related_file

        try:
            related, problem = load_related_file(
                self.cfg.index.library_dir,
                "tracks",
                enabled=body.repeat_rules.use_related_tracks,
                restoring=restoring,
            )
        except ValueError as exc:
            self.related_tracks_problem = str(exc)
            raise
        if problem:
            logger.warning("Related tracks file was not used: %s", problem)
        return related, problem

    def set_bpm_range(self, lo: float | None, hi: float | None) -> None:
        from autodj.local_programme_playout import require_automation

        require_automation(self.player)
        self._bpm_range = None if lo is None or hi is None or lo >= hi else (lo, hi)

    def set_discovery_every(
        self, every: int | None, *, restoring: bool = False, refresh: bool = True
    ) -> None:
        from autodj.local_programme_playout import require_automation

        require_automation(self.player)
        self._discovery_every = None if every is None or every <= 0 else int(every)
        if every is not None and every > 0:
            self._discovery_enabled = True
        else:
            self._discovery_enabled = False
        if self.player is not None:
            self.player._state.discovery_enabled = self._discovery_enabled
            if (
                not restoring
                and refresh
                and (refresh_playout := getattr(self.player, "refresh_playout", None))
            ):
                refresh_playout()

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))
        if self.player is not None:
            self.player._state.volume = self._volume

    def toggle_mute(self) -> bool:
        self._is_muted = not self._is_muted
        if self.player is not None:
            self.player._state.is_muted = self._is_muted
        return self._is_muted

    def set_muted(self, value: bool) -> None:
        self._is_muted = value
        if self.player is not None:
            self.player._state.is_muted = value

    def set_eq(
        self, low: float | None = None, mid: float | None = None, high: float | None = None
    ) -> dict[str, float]:
        from autodj.local_programme_playout import require_automation

        require_automation(self.player)
        if low is not None:
            self._eq_low = max(0.0, min(2.0, low))
        if mid is not None:
            self._eq_mid = max(0.0, min(2.0, mid))
        if high is not None:
            self._eq_high = max(0.0, min(2.0, high))
        return self.get_eq()

    def get_eq(self) -> dict[str, float]:
        return {
            "low": round(self._eq_low, 3),
            "mid": round(self._eq_mid, 3),
            "high": round(self._eq_high, 3),
        }

    def toggle_discovery(self) -> bool:
        self._discovery_enabled = not self._discovery_enabled
        if self.player is not None:
            self.player._state.discovery_enabled = self._discovery_enabled
        return self._discovery_enabled
