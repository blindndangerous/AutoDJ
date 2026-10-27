"""Create a stopped station from validated configuration and selected state."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tomllib
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autodj.config import STATION_SECTION_TYPES, AutoDJConfig
from autodj.fsutil import atomic_write
from autodj.stations import StationPaths


class CreationConflict(ValueError):
    """A source file or destination prevents a complete copy."""


def source_table(root: AutoDJConfig, slug: str) -> dict[str, Any]:
    """Read only the source's written configuration, never secret overlays."""
    spec = next((item for item in root.stations if item.slug == slug), None)
    if spec is not None and spec.defined_in == "web":
        definition = StationPaths.for_station(root, slug).definition
        if not _regular(definition):
            raise CreationConflict(f"Cannot copy {definition}: it is not a regular file")
        try:
            table = json.loads(definition.read_text(encoding="utf-8"))["station"]
            if not isinstance(table, dict):
                raise ValueError("station is not a table")
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            raise CreationConflict(f"Cannot read source definition {definition}: {exc}") from exc
        return deepcopy(table)
    if root.config_path is None:
        return {}
    with root.config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    if slug == "default":
        return deepcopy(
            {
                key: raw[key]
                for key in ("presets", *(section.SECTION for section in STATION_SECTION_TYPES))
                if key in raw
            }
        )
    return deepcopy(raw.get("stations", {}).get(slug, {}))


def duplicate_table(
    root: AutoDJConfig, source: str | None, slug: str, name: str, autostart: bool
) -> dict[str, Any]:
    table = source_table(root, source) if source is not None else {}
    for key in ("autostart", "webhooks"):
        table.pop(key, None)
    for section, keys in {
        "stream": ("relays", "listener_user", "listener_password", "enabled", "trusted_proxies"),
        "recording": ("folder",),
        "time_checks": ("clips_folder",),
        # Another station's log label would be refused: labels tell stations apart.
        "station": ("log_label",),
    }.items():
        value = table.get(section)
        if isinstance(value, dict):
            for key in keys:
                value.pop(key, None)
    table["autostart"] = autostart
    table.setdefault("stream", {})["station_name"] = name
    table.setdefault("recording", {})["folder"] = str(Path(root.recording.folder) / slug)
    return table


def _regular(path: Path) -> bool:
    """Refuse links, junctions, and other special files when copying."""
    if any(part.is_symlink() or part.is_junction() for part in (path, *path.parents)):
        return False
    info = path.stat()
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def _copy_file(source: Path, target: Path) -> bool:
    if not source.exists() and not source.is_symlink() and not source.is_junction():
        return False
    if not _regular(source):
        raise CreationConflict(f"Cannot copy {source}: it is not a regular file")
    shutil.copyfile(source, target)
    return True


def _copy_folder(source: Path, target: Path) -> bool:
    if not source.exists() and not source.is_symlink() and not source.is_junction():
        return False
    if (
        any(part.is_symlink() or part.is_junction() for part in (source, *source.parents))
        or not source.is_dir()
    ):
        raise CreationConflict(f"Cannot copy {source}: it is not a plain folder")
    target.mkdir()
    for item in source.iterdir():
        if not _regular(item):
            raise CreationConflict(f"Cannot copy {item}: it is not a regular file")
        shutil.copyfile(item, target / item.name)
    return True


def stage_station(
    root: AutoDJConfig,
    slug: str,
    source: str | None,
    table: dict[str, Any],
    *,
    copy_clockwheels: bool,
    copy_schedule: bool,
    copy_carts: bool,
) -> tuple[Path, Path, list[str]]:
    """Stage an explicit allow-list, publish the folder, and return copied items."""
    destination = StationPaths.for_station(root, slug).state_dir
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    for stale in parent.glob(".creating-*"):
        if (
            stale.is_dir()
            and not stale.is_symlink()
            and not stale.is_junction()
            and datetime.now(UTC).timestamp() - stale.stat().st_mtime > 3600
        ):
            shutil.rmtree(stale)
    if destination.exists() or destination.is_symlink():
        raise CreationConflict(
            f"A folder from an earlier station with ID {slug} exists; choose another ID or move that folder away"
        )
    stage = parent / f".creating-{slug}-{uuid.uuid4().hex}"
    stage.mkdir()
    copied: list[str] = []
    try:
        if source is not None:
            old = (
                StationPaths.default(root)
                if source == "default"
                else StationPaths.for_station(root, source)
            )
            if any(
                part.is_symlink() or part.is_junction()
                for part in (old.state_dir, *old.state_dir.parents)
            ):
                raise CreationConflict(f"Cannot copy {old.state_dir}: it is not a plain folder")
            # Only these files carry settings or chosen assets. New state files stay out.
            # relays.json and secrets.json, with the passwords, webhooks and the
            # station's own Stereo Tool key (one licence per instance), never come.
            if old.web_state.exists() or old.web_state.is_symlink() or old.web_state.is_junction():
                if not _regular(old.web_state):
                    raise CreationConflict(f"Cannot copy {old.web_state}: it is not a regular file")
                try:
                    saved = json.loads(old.web_state.read_text(encoding="utf-8"))
                    if not isinstance(saved, dict):
                        raise ValueError("root is not an object")
                except (OSError, UnicodeError, ValueError) as exc:
                    raise CreationConflict(
                        f"Cannot read saved settings in {old.web_state}: {exc}"
                    ) from exc
                playback = saved.get("playback")
                if isinstance(playback, dict) and "stream_station_name" in playback:
                    playback["stream_station_name"] = table["stream"]["station_name"]
                atomic_write(stage / old.web_state.name, json.dumps(saved))
                copied.append("saved settings")
            if _copy_file(old.time_checks, stage / old.time_checks.name):
                state = stage / old.time_checks.name
                try:
                    data = json.loads(state.read_text(encoding="utf-8"))
                    if (
                        not isinstance(data, dict)
                        or data.get("version") != 1
                        or not isinstance(data.get("settings"), dict)
                    ):
                        raise ValueError("invalid time-check settings")
                    # A clip chosen by name is a file in the old station's own
                    # time checks folder, which is not copied (#388); "" (none)
                    # is kept.
                    settings = {
                        key: value
                        for key, value in data["settings"].items()
                        if not (key in ("recorded_folder", "intro", "outro") and value)
                    }
                    atomic_write(state, json.dumps({"version": 1, "settings": settings}))
                except (OSError, UnicodeError, ValueError) as exc:
                    raise CreationConflict(f"Cannot copy {old.time_checks}: {exc}") from exc
                if "saved settings" not in copied:
                    copied.append("saved settings")
            if copy_schedule and _copy_file(old.schedule, stage / old.schedule.name):
                schedule = stage / old.schedule.name
                try:
                    document = json.loads(schedule.read_text(encoding="utf-8"))
                    if not isinstance(document, dict) or not isinstance(document.get("runs"), list):
                        raise ValueError("invalid schedule")
                    document["runs"] = []
                    atomic_write(schedule, json.dumps(document))
                except (OSError, UnicodeError, ValueError) as exc:
                    raise CreationConflict(f"Cannot copy {old.schedule}: {exc}") from exc
                copied.append("schedule")
            if copy_carts:
                found = _copy_file(old.carts, stage / old.carts.name)
                found = _copy_folder(old.cart_uploads, stage / old.cart_uploads.name) or found
                if found:
                    copied.append("carts")
            if copy_clockwheels and _copy_folder(old.clockwheels, stage / old.clockwheels.name):
                copied.append("clockwheels")
        definition = {
            "version": 1,
            "created": datetime.now(UTC).isoformat(),
            "source": source,
            "station": table,
        }
        atomic_write(
            stage / StationPaths.for_station(root, slug).definition.name,
            json.dumps(definition, indent=2) + "\n",
        )
        # os.rename is no-replace on Windows; refuse a competing folder on every platform.
        if destination.exists() or destination.is_symlink():
            raise CreationConflict(f"Station folder {destination} appeared during creation")
        os.rename(stage, destination)
        return stage, destination, copied
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
