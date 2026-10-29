"""Saved station defaults and profile overrides, separate from active playout."""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import fields
from typing import Any

from pydantic import BaseModel, ConfigDict

from autodj.fsutil import atomic_write
from autodj.profiles import ProfileSnapshot, ProfileStore
from autodj.settings_bodies import BreakSettingsBody

CORE_GROUPS = frozenset(
    {"preset", "transition", "djmix", "playback", "bpm_range", "discovery", "eq"}
)
PROFILE_GROUPS = CORE_GROUPS | {"breaks", "time_checks", "clock", "cart"}
LOCAL_PLAYBACK = frozenset(
    {"key_notation", "key_prefer_flats", "show_lyrics", "prefetch_next_track"}
)


class ClockChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None


class CartChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: str


def merge_values(base: dict, patch: dict) -> dict:
    """Merge explicit values, retaining false, zero and null."""
    result = copy.deepcopy(base)
    for key, value in patch.items():
        result[key] = (
            merge_values(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else copy.deepcopy(value)
        )
    return result


def overrides(base: dict, values: dict) -> dict:
    result = {}
    for key, value in values.items():
        if key in base and value == base[key]:
            continue
        difference = (
            overrides(base[key], value)
            if isinstance(value, dict) and isinstance(base.get(key), dict)
            else value
        )
        if difference != {}:
            result[key] = difference
    return result


def legacy_groups(snapshot: ProfileSnapshot) -> dict:
    """Keep old partial profiles' non-null choices as explicit overrides."""
    if snapshot.settings is not None:
        if not isinstance(snapshot.settings, dict):
            raise ValueError("Profile settings must be an object")
        return copy.deepcopy(snapshot.settings)
    groups: dict[str, Any] = {}
    special = {
        "name",
        "settings",
        "preset",
        "bpm_lo",
        "bpm_hi",
        "harmonic_mode",
        "clockwheel",
        "cart_page",
    }
    playback = {
        field.name: getattr(snapshot, field.name)
        for field in fields(snapshot)
        if field.name not in special and getattr(snapshot, field.name) is not None
    }
    if "pure_shuffle" in playback:
        shuffle = playback.pop("pure_shuffle")
        playback.setdefault("pick_mode", "random" if shuffle else "similarity")
    if playback:
        groups["playback"] = playback
    for field, group, key in (
        ("preset", "preset", "name"),
        ("harmonic_mode", "djmix", "harmonic_mode"),
        ("clockwheel", "clock", "name"),
        ("cart_page", "cart", "page"),
    ):
        if (value := getattr(snapshot, field)) is not None:
            groups[group] = {key: value}
    if snapshot.bpm_lo is not None or snapshot.bpm_hi is not None:
        groups["bpm_range"] = {
            key: value
            for key, value in (("lo", snapshot.bpm_lo), ("hi", snapshot.bpm_hi))
            if value is not None
        }
    return groups


def profile_values(values: dict) -> dict:
    result = {key: copy.deepcopy(value) for key, value in values.items() if key in PROFILE_GROUPS}
    for key in LOCAL_PLAYBACK | {"pure_shuffle", "replaygain_enabled"}:
        result.get("playback", {}).pop(key, None)
    return result


class ProfileSettings:
    """One station's stored editing targets and selected on-air profile."""

    def __init__(self, station: Any) -> None:
        self.station = station
        self.store = ProfileStore(station.paths.profiles)
        self.defaults_path = station.paths.profile_defaults
        self.selection_path = station.paths.profile_selection
        self.lock = threading.RLock()
        self.active: str | None = None

    @property
    def owner(self) -> Any:
        return getattr(self.station.bridge, "settings", self.station.bridge)

    def capture(self) -> dict:
        station = self.station
        values = self.owner.editable_values()
        # The playback setter uses zero to disable these nullable config values.
        for key in (
            "liners_every_n_songs",
            "liners_every_minutes",
            "liners_random_min_minutes",
            "liners_random_max_minutes",
        ):
            values["playback"][key] = getattr(station.cfg.playback, key) or 0
        values["breaks"] = {
            key: getattr(station.cfg.breaks, key) for key in BreakSettingsBody.model_fields
        }
        values["time_checks"] = station.time_checks.settings.model_dump()
        clock = station.ledger.get_setting("clock", {}) if station.ledger is not None else {}
        values["clock"] = {"name": clock.get("name") if isinstance(clock, dict) else None}
        wall = station.carts.store.wall
        values["cart"] = {"page": wall.pages[wall.active_page - 1].name}
        return profile_values(values)

    def defaults(self) -> dict:
        if not self.defaults_path.exists():
            values = self.capture()
            self._write(self.defaults_path, values)
            return values
        value = json.loads(self.defaults_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Station profile defaults must be an object")
        return value

    @staticmethod
    def _write(path: Any, values: dict) -> None:
        atomic_write(path, json.dumps(values, indent=2, sort_keys=True, allow_nan=False))

    def resolve(self, name: str) -> dict:
        with self.lock:
            defaults = self.defaults()
            return (
                merge_values(defaults, legacy_groups(self.store.load(name))) if name else defaults
            )

    def validate(self, values: dict) -> None:
        from autodj.clockwheel_store import ClockwheelStore
        from autodj.station_settings import StationSettings
        from autodj.time_checks import SettingsBody

        unknown = values.keys() - PROFILE_GROUPS
        if unknown:
            raise ValueError(f"Unknown profile settings: {sorted(unknown)}")
        if any(not isinstance(value, dict) for value in values.values()):
            raise ValueError("Profile settings groups must be objects")
        if LOCAL_PLAYBACK & values.get("playback", {}).keys():
            raise ValueError("Display and prefetch settings belong to the station, not a profile")
        # These patch setters treat null as "leave unchanged", which is not a
        # valid resolved override: it would retain the previously active profile.
        for group in ("playback", "djmix", "eq"):
            for key, value in values.get(group, {}).items():
                if value is None:
                    raise ValueError(
                        f"{group}.{key} cannot be null; remove the override to inherit defaults"
                    )
        core = {key: value for key, value in values.items() if key in CORE_GROUPS}
        # Validate through the same settings owner without attaching an audio runtime.
        scratch = StationSettings(self.station.cfg)
        if core:
            scratch.validate_settings_patch(core)
        if "breaks" in values:
            BreakSettingsBody.model_validate(values["breaks"])
        if "time_checks" in values:
            checks = SettingsBody.model_validate(values["time_checks"])
            for pack in checks.enabled_packs:
                if pack not in self.station.time_checks.packs:
                    raise ValueError(f"No voice pack named {pack}")
            if problem := self.station.time_checks.check_choices(checks):
                raise ValueError(problem)
        if "clock" in values:
            name = ClockChoice.model_validate(values["clock"]).name
            if name is not None:
                clock = ClockwheelStore(self.station.paths.clockwheels).load(name)
                if any("conditions" in step for step in clock["steps"]):
                    from autodj.station_routes import _validate_step_conditions

                    library = self.station.bridge.library
                    sources = {source["id"]: source for source in library.catalog.sources()}
                    for number, step in enumerate(clock["steps"], 1):
                        if "conditions" in step:
                            _validate_step_conditions(self.station, library, step, sources, number)
        if "cart" in values:
            page = CartChoice.model_validate(values["cart"]).page
            if not any(
                item.name.casefold() == page.casefold()
                for item in self.station.carts.store.wall.pages
            ):
                raise ValueError(f"Cart page {page!r} does not exist")

    def save(self, name: str, changes: dict) -> dict:
        with self.lock:
            # Partial groups need their saved values before schema validation.
            # Validate before computing overrides so invalid groups cannot disappear.
            values = merge_values(self.resolve(name), changes)
            self.validate(values)
            defaults = self.defaults()
            apply_changes: dict | None = None
            if name:
                self.store.save(ProfileSnapshot(name=name, settings=overrides(defaults, values)))
                if self.active == name:
                    apply_changes = {group: values[group] for group in changes}
            else:
                # Resolve the active profile against the candidate defaults and
                # validate the complete effective result before persisting either
                # defaults or applying any of it.
                if self.active:
                    profile_overrides = legacy_groups(self.store.load(self.active))
                    active_before = merge_values(defaults, profile_overrides)
                    active_after = merge_values(values, profile_overrides)
                    self.validate(active_after)
                    apply_changes = {
                        group: active_after[group]
                        for group in active_after
                        if active_before.get(group) != active_after.get(group)
                    }
                else:
                    apply_changes = {group: values[group] for group in changes}
                self._write(self.defaults_path, values)
            # Apply under the same lock as scheduled Apply, without changing the
            # active identity. A profile draft that went off air is storage only.
            if apply_changes:
                try:
                    # Apply only affected groups to avoid reselecting unchanged
                    # clockwheels or rewriting unrelated feature stores.
                    self.apply_values(apply_changes)
                except ValueError as exc:
                    raise ValueError(
                        "Settings were saved, but could not be fully applied. "
                        f"Some settings may already be active: {exc}"
                    ) from exc
            return values

    def create(self, snapshot: ProfileSnapshot) -> None:
        with self.lock:
            from autodj.profiles import validate_name

            name = validate_name(snapshot.name)
            if any(existing.casefold() == name.casefold() for existing in self.store.list_names()):
                raise ValueError(f"A profile named {name!r} already exists")
            groups = legacy_groups(snapshot)
            self.validate(merge_values(self.defaults(), groups))
            self.store.save(ProfileSnapshot(name=name, settings=groups))

    def reset(self, name: str) -> None:
        with self.lock:
            self.store.load(name)
            self.store.save(ProfileSnapshot(name=name, settings={}))

    def apply_values(self, values: dict) -> None:
        """Apply already validated groups; each persistence owner keeps its errors."""
        from autodj.breaks_api import TURNED_OFF
        from autodj.time_checks import SettingsBody

        station = self.station
        core = {key: value for key, value in values.items() if key in CORE_GROUPS}
        group = "playback"
        try:
            if core:
                self.owner.apply_settings_patch(core)
            group = "breaks"
            if "breaks" in values:
                body = BreakSettingsBody.model_validate(values["breaks"])
                was_on = station.cfg.breaks.enabled is True
                for key in body.model_fields_set:
                    setattr(station.cfg.breaks, key, getattr(body, key))
                self.owner.page_saved.add("breaks")
                self.owner.save(strict=True)
                if was_on and station.cfg.breaks.enabled is not True:
                    station.breaks.drop_waiting_breaks(TURNED_OFF)
            group = "time checks"
            if "time_checks" in values:
                station.time_checks.save(SettingsBody.model_validate(values["time_checks"]))
            group = "clockwheel"
            if "clock" in values and station.ledger is not None:
                station.ledger.select_clock(values["clock"].get("name"))
                if station.player is not None:
                    station.player.configure_clock_after_selection()
            group = "cart page"
            if "cart" in values:
                page = values["cart"]["page"]
                wall = station.carts.store.wall
                wall.active_page = next(
                    index
                    for index, item in enumerate(wall.pages, 1)
                    if item.name.casefold() == page.casefold()
                )
                station.carts.store.save()
        except (OSError, ValueError, StopIteration) as exc:
            raise ValueError(f"Could not apply {group} settings: {exc}") from exc

    def apply(self, name: str) -> dict:
        with self.lock:
            values = self.resolve(name)
            self.validate(values)
            self.apply_values(values)
            self._write(self.selection_path, {"profile": name or None})
            self.active = name or None
            return values

    def restore(self) -> None:
        if not self.selection_path.exists():
            if self.defaults_path.exists():
                self.apply("")
            return
        selection = json.loads(self.selection_path.read_text(encoding="utf-8"))
        name = selection.get("profile")
        if name is not None and not isinstance(name, str):
            raise ValueError("Selected profile must be a name or null")
        self.apply(name or "")


def profile_settings(station: Any) -> ProfileSettings:
    manager = getattr(station, "profile_settings", None)
    if manager is None:
        manager = station.profile_settings = ProfileSettings(station)
    return manager
