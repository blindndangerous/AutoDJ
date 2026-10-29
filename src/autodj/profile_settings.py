"""Saved station defaults and profile overrides, separate from active playout."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from autodj.fsutil import atomic_write
from autodj.profiles import ProfileSnapshot, ProfileStore
from autodj.settings_bodies import BreakSettingsBody

logger = logging.getLogger(__name__)

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
        self.problem: str | None = None

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
            if not self.selection_path.exists():
                self._remember_selection(values)
            return values
        value = json.loads(self.defaults_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Station profile defaults must be an object")
        return value

    @staticmethod
    def _write(path: Any, values: dict) -> None:
        atomic_write(path, json.dumps(values, indent=2, sort_keys=True, allow_nan=False))

    @staticmethod
    def _revision(values: dict) -> str:
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _remember_selection(self, values: dict) -> None:
        self._write(
            self.selection_path, {"profile": self.active, "revision": self._revision(values)}
        )

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
            body = BreakSettingsBody.model_validate(values["breaks"])
            for key in body.model_fields_set - {"duck_db"}:
                if getattr(body, key) is None:
                    raise ValueError(
                        f"breaks.{key} cannot be null; remove the override to inherit defaults"
                    )
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
            before = self.resolve(name)
            values = merge_values(before, changes)
            self.validate(values)
            defaults = self.defaults()
            apply_changes: dict | None = None
            if name:
                self.store.save(ProfileSnapshot(name=name, settings=overrides(defaults, values)))
                if self.active == name:
                    apply_changes = overrides(before, values)
            else:
                # Resolve the active profile against the candidate defaults and
                # validate the complete effective result before persisting either
                # defaults or applying any of it.
                if self.active:
                    profile_overrides = legacy_groups(self.store.load(self.active))
                    active_before = merge_values(defaults, profile_overrides)
                    active_after = merge_values(values, profile_overrides)
                    self.validate(active_after)
                    apply_changes = overrides(active_before, active_after)
                else:
                    apply_changes = overrides(before, values)
                self._write(self.defaults_path, values)
            # Apply under the same lock as scheduled Apply, without changing the
            # active identity. A profile draft that went off air is storage only.
            if apply_changes:
                try:
                    # Apply only affected groups to avoid reselecting unchanged
                    # clockwheels or rewriting unrelated feature stores.
                    # These two stores require a complete group. Merge changed
                    # fields with live values, not an older saved profile.
                    if "time_checks" in apply_changes:
                        apply_changes["time_checks"] = merge_values(
                            self.station.time_checks.settings.model_dump(),
                            apply_changes["time_checks"],
                        )
                    if "bpm_range" in apply_changes:
                        apply_changes["bpm_range"] = merge_values(
                            self.owner.editable_values()["bpm_range"], apply_changes["bpm_range"]
                        )
                    self.apply_values(apply_changes)
                except ValueError as exc:
                    raise ValueError(
                        "Settings were saved, but could not be fully applied. "
                        f"Some settings may already be active: {exc}"
                    ) from exc
            # A failed restore leaves active unset. Saving is not an instruction
            # to replace the saved selection; explicit Activate finishes recovery.
            if self.problem is None and (not name or self.active == name):
                self._remember_selection(self.resolve(self.active or ""))
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
            if self.active == name:
                self._remember_selection(self.resolve(name))

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
                before = {key: getattr(station.cfg.breaks, key) for key in body.model_fields_set}
                page_was_saved = "breaks" in self.owner.page_saved
                try:
                    for key in body.model_fields_set:
                        setattr(station.cfg.breaks, key, getattr(body, key))
                    self.owner.page_saved.add("breaks")
                    self.owner.save(strict=True)
                except Exception:
                    for key, value in before.items():
                        setattr(station.cfg.breaks, key, value)
                    if not page_was_saved:
                        self.owner.page_saved.discard("breaks")
                    raise
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
            self._write(
                self.selection_path, {"profile": name or None, "revision": self._revision(values)}
            )
            self.active = name or None
            self.problem = None
            return values

    def delete(self, name: str) -> bool:
        with self.lock:
            if self.active and self.active.casefold() == name.casefold():
                raise ValueError(
                    "Activate another profile or Station defaults before deleting the active profile"
                )
            return self.store.delete(name)

    @contextmanager
    def changing_reference(self, kind: str, old: str, new: str | None) -> Iterator[None]:
        """Keep saved references consistent with an explicit rename or deletion.

        Individual files are atomic writes. If the resource operation fails,
        put back the affected documents before reporting the failure.
        """
        with self.lock:
            documents: list[tuple[Path, dict, str | None]] = []
            if self.defaults_path.exists():
                documents.append((self.defaults_path, self.defaults(), None))
            for profile_name in self.store.list_names():
                snapshot = self.store.load(profile_name)
                documents.append(
                    (
                        self.store.root / f"{profile_name}.json",
                        legacy_groups(snapshot),
                        snapshot.name,
                    )
                )
            previous = []
            try:
                for path, values, name in documents:
                    updated = copy.deepcopy(values)
                    if kind == "pack":
                        checks = updated.get("time_checks", {})
                        if old in checks.get("enabled_packs", []):
                            checks["enabled_packs"] = [
                                item for item in checks["enabled_packs"] if item != old
                            ]
                    else:
                        group, key = {
                            "clock": ("clock", "name"),
                            "cart": ("cart", "page"),
                            "preset": ("playback", "stereotool_preset"),
                        }[kind]
                        value = updated.get(group, {}).get(key)
                        if isinstance(value, str) and value.casefold() == old.casefold():
                            updated[group][key] = new
                    if updated != values:
                        previous.append((path, path.read_text(encoding="utf-8")))
                        self._write(
                            path,
                            ProfileSnapshot(name=name, settings=updated).to_dict()
                            if name
                            else updated,
                        )
                if self.selection_path.exists() and self.problem is None:
                    previous.append(
                        (self.selection_path, self.selection_path.read_text(encoding="utf-8"))
                    )
                    self._remember_selection(self.resolve(self.active or ""))
                yield
            except Exception:
                for path, content in reversed(previous):
                    atomic_write(path, content)
                raise

    def restore(self) -> None:
        # Validate the entire saved selection before touching the last saved
        # station state. Invalid JSON or missing resources must not lock its
        # editors out of the repair path.
        applying = False
        try:
            if not self.selection_path.exists():
                if not self.defaults_path.exists():
                    return
                name = ""
                selection = {}
            else:
                selection = json.loads(self.selection_path.read_text(encoding="utf-8"))
                if not isinstance(selection, dict):
                    raise ValueError("Saved profile selection must be an object")
                selected_name = selection.get("profile")
                if selected_name is not None and not isinstance(selected_name, str):
                    raise ValueError("Selected profile must be a name or null")
                name = selected_name or ""
            values = self.resolve(name)
            self.validate(values)
            if selection.get("revision") == self._revision(values):
                # Ordinary restarts keep the persisted live state. An offline edit
                # to defaults or the selected profile changes this revision and is
                # deliberately applied below. Older selection files migrate once.
                self.active = name or None
                self.problem = None
                return
            # Applying writes each group's store, which may fail like the reads.
            applying = True
            self.apply(name)
        except (OSError, ValueError) as exc:
            effect = (
                "Some settings may already be active."
                if applying
                else "Using the last saved station settings."
            )
            self.problem = (
                f"Could not restore the saved profile: {exc}. {effect} "
                "Activate a profile or Station defaults to finish recovery."
            )
            logger.warning("%s", self.problem)


def profile_settings(station: Any) -> ProfileSettings:
    manager = getattr(station, "profile_settings", None)
    if manager is None:
        manager = station.profile_settings = ProfileSettings(station)
    return manager
