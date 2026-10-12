"""Named clockwheels in a versioned native format, one JSON file per clockwheel.

A clockwheel file looks like this::

    {"version": 1, "name": "Morning", "resync_minutes": [0], "steps": [
        {"type": "music", "source": "<source id>", "folder": "Rock",
         "recursive": false, "selection": "least_recent", "enforce_rules": true},
        {"type": "liner", "source": "<source id>", "folder": "", ...},
        {"type": "track", "source": "<source id>", "path": "Album/Song.flac"},
        {"type": "category", "category": "<category id>",
         "selection": "least_recent", "enforce_rules": true},
        {"type": "settings", "preset": "Night"}]}

Folder and track steps name a registered library source by its ID and a
path inside it written with forward slashes, so a file never holds an
absolute path and a browser never sends one.  ``folder`` is ``""`` for the
source folder itself.  A category step names a rotation category by its ID
(see :mod:`autodj.categories`), so renaming the category keeps the step.

Step types live in a registry, like the scheduler's actions.  Loading a
clockwheel with a type nobody registered fails with an error that names
the type.  A feature adds its own type (carts, time checks, ad breaks)
with :func:`register_step_type` before clockwheels load, and teaches the
player to play it in ``Player.configure_clock``.
"""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from autodj.clockwheel import MUSIC_KINDS, ClockSlot, validate_resync_minutes

VERSION = 1
MAX_STEPS = 256
_NAME = re.compile(r"^[A-Za-z0-9 _-]{1,64}$")
# Device names Windows refuses as file names, whatever the extension.
_RESERVED = {"con", "prn", "aux", "nul"} | {f"{p}{n}" for p in ("com", "lpt") for n in range(10)}
_SELECTIONS = ("least_recent", "random")
OLD_CLOCK_MESSAGE = (
    "The clockwheel saved by an older AutoDJ can no longer be read, so song picking cannot "
    "use a clockwheel. To bring it back, open the Library tools tab, Clockwheels: type a "
    "name, choose your .PAL file, press Import .PAL, then press Activate."
)
StepValidator = Callable[[Mapping[str, Any]], dict[str, Any]]
_STEP_TYPES: dict[str, StepValidator] = {}


def validate_name(name: Any) -> str:
    """Return *name* when it is safe as a file name on every platform."""
    if (
        not isinstance(name, str)
        or not _NAME.fullmatch(name)
        or name != name.strip()
        or name.casefold() in _RESERVED
    ):
        raise ValueError(
            "A clockwheel name needs 1 to 64 letters, digits, spaces, dashes or underscores, "
            "with no space at either end"
        )
    return name


def register_step_type(kind: str, validator: StepValidator) -> None:
    """Register a step type and the function that validates its JSON.

    The validator receives the step object, including ``type``, and returns
    the normalized step, or raises ``ValueError`` saying what is wrong.
    """
    if not re.fullmatch(r"[a-z][a-z_]*", kind):
        raise ValueError(f"Clockwheel step type {kind!r} needs lowercase letters and underscores")
    if kind in _STEP_TYPES:
        raise ValueError(f"Clockwheel step type {kind!r} is already registered")
    _STEP_TYPES[kind] = validator


def step_types() -> list[str]:
    """The step types this AutoDJ can load, sorted."""
    return sorted(_STEP_TYPES)


def relative_path(value: Any, *, label: str, allow_empty: bool = False) -> str:
    """Validate a path inside a source, written with forward slashes."""
    if not isinstance(value, str) or len(value) > 1024:
        raise ValueError(f"{label} must be text of at most 1024 characters")
    if not value:
        if allow_empty:
            return value
        raise ValueError(f"{label} must not be empty")
    parts = value.split("/")
    if (
        "\\" in value
        or ":" in value
        or "\0" in value
        or any(part in {"", ".", ".."} or part != part.strip() for part in parts)
    ):
        raise ValueError(
            f"{label} must be a path inside the source, with forward slashes and no '..': {value!r}"
        )
    return value


def _fields(value: Mapping[str, Any], allowed: set[str], required: set[str]) -> None:
    kind = value["type"]
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"A {kind} step has unknown fields: {', '.join(sorted(unknown))}")
    missing = required - set(value)
    if missing:
        raise ValueError(f"A {kind} step needs: {', '.join(sorted(missing))}")


def _source_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("A step's source must be the ID of a registered library folder")
    return value


def _selection(value: Mapping[str, Any]) -> tuple[str, bool]:
    """The step's ``selection`` and ``enforce_rules``, checked, with their defaults."""
    enforce_rules = value.get("enforce_rules", True)
    if type(enforce_rules) is not bool:
        raise ValueError("enforce_rules must be true or false")
    selection = value.get("selection", "least_recent")
    if selection not in _SELECTIONS:
        raise ValueError("selection must be least_recent or random")
    return selection, enforce_rules


def _condition(value: Any) -> dict[str, Any]:
    from autodj.clock_conditions import validate_metadata_condition

    if not isinstance(value, dict) or not isinstance(value.get("field"), str):
        raise ValueError("Each condition needs a field")
    if value["field"] != "folder":
        return validate_metadata_condition(value)
    if set(value) - {"field", "source", "folder", "recursive"}:
        raise ValueError("A folder condition has unknown fields")
    recursive = value.get("recursive", False)
    if type(recursive) is not bool:
        raise ValueError("A folder condition's recursive value must be true or false")
    return {
        "field": "folder",
        "source": _source_id(value.get("source")),
        "folder": relative_path(value.get("folder", ""), label="folder", allow_empty=True),
        "recursive": recursive,
    }


def _folder_step(value: Mapping[str, Any]) -> dict[str, Any]:
    common = {"type", "selection", "enforce_rules", "require_following"}
    conditional = "conditions" in value
    _fields(
        value,
        common | ({"conditions", "match"} if conditional else {"source", "folder", "recursive"}),
        {"type", "conditions"} if conditional else {"type", "source"},
    )
    selection, enforce_rules = _selection(value)
    require_following = value.get("require_following", False)
    if type(require_following) is not bool:
        raise ValueError("require_following must be true or false")
    if require_following and value["type"] != "liner":
        raise ValueError("require_following is only valid for liner steps")
    step = {"type": value["type"], "selection": selection, "enforce_rules": enforce_rules}
    if conditional:
        conditions = value["conditions"]
        if not isinstance(conditions, list) or not 1 <= len(conditions) <= 32:
            raise ValueError("A step needs between 1 and 32 conditions")
        match = value.get("match", "all")
        if match not in ("all", "any"):
            raise ValueError("Choose Match all or Match any for the step")
        step.update(match=match, conditions=[_condition(item) for item in conditions])
    else:
        condition = _condition(
            {
                "field": "folder",
                **{key: value[key] for key in ("source", "folder", "recursive") if key in value},
            }
        )
        step.update({key: item for key, item in condition.items() if key != "field"})
    if require_following:
        step["require_following"] = True
    return step


def step_conditions(step: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read an old folder step as its single condition, without rewriting its file."""
    if "conditions" in step:
        return step["conditions"]
    return [{"field": "folder", **{key: step[key] for key in ("source", "folder", "recursive")}}]


def resolve_conditions(
    step: Mapping[str, Any], sources: Mapping[str, Any], number: int
) -> tuple[dict[str, Any], ...]:
    """Check folder sources and resolve their paths for the player."""
    result = []
    for condition in step_conditions(step):
        if condition["field"] != "folder":
            result.append(condition)
            continue
        source = step_source({"type": step["type"], "source": condition["source"]}, sources, number)
        if condition["folder"] and not source["recursive"]:
            raise ValueError(
                f"Step {number}: {source['path']} is registered without subfolders, so "
                f"its subfolder {condition['folder']} is never scanned. Turn on Add "
                "subfolders for it under Music folders and Voice liner folders"
            )
        result.append(
            {
                "field": "folder",
                "folder": source_folder(source["path"], condition["folder"]),
                "recursive": condition["recursive"],
            }
        )
    return tuple(result)


def _track_step(value: Mapping[str, Any]) -> dict[str, Any]:
    _fields(value, {"type", "source", "path"}, {"type", "source", "path"})
    return {
        "type": "track",
        "source": _source_id(value["source"]),
        "path": relative_path(value["path"], label="track path"),
    }


def _settings_step(value: Mapping[str, Any]) -> dict[str, Any]:
    _fields(value, {"type", "preset"}, {"type", "preset"})
    preset = value["preset"]
    if not isinstance(preset, str) or not preset.strip() or len(preset) > 64:
        raise ValueError("A settings step needs the name of a preset")
    return {"type": "settings", "preset": preset.strip()}


def _category_step(value: Mapping[str, Any]) -> dict[str, Any]:
    _fields(value, {"type", "category", "selection", "enforce_rules"}, {"type", "category"})
    category = value["category"]
    if not isinstance(category, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", category):
        raise ValueError("A category step must name a saved category by its ID")
    selection, enforce_rules = _selection(value)
    return {
        "type": "category",
        "category": category,
        "selection": selection,
        "enforce_rules": enforce_rules,
    }


register_step_type("music", _folder_step)
register_step_type("liner", _folder_step)
register_step_type("track", _track_step)
register_step_type("settings", _settings_step)
register_step_type("category", _category_step)


def validate_step(value: Any) -> dict[str, Any]:
    """Validate one step through the registry."""
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise ValueError("Each clockwheel step must be an object with a type")
    kind = value["type"]
    validator = _STEP_TYPES.get(kind)
    if validator is None:
        raise ValueError(
            f"Unknown clockwheel step type {kind!r}. This AutoDJ knows "
            f"{', '.join(step_types())}; a clockwheel that uses {kind!r} needs the "
            "AutoDJ version that added it"
        )
    validated = validator(value)
    if not isinstance(validated, dict) or validated.get("type") != kind:
        raise ValueError(f"The {kind!r} step validator returned an invalid step")
    return validated


def validate_clockwheel(value: Any) -> dict[str, Any]:
    """Validate a whole clockwheel and return it normalized."""
    if not isinstance(value, dict):
        raise ValueError("A clockwheel must be a JSON object")
    expected = {"version", "name", "steps", "resync_minutes"}
    if set(value) != expected:
        unknown, missing = set(value) - expected, expected - set(value)
        problem = f"unknown {sorted(unknown)}" if unknown else f"missing {sorted(missing)}"
        raise ValueError(f"A clockwheel has version, name, steps and resync_minutes ({problem})")
    if type(value["version"]) is not int or value["version"] != VERSION:
        raise ValueError(f"Unsupported clockwheel version {value['version']!r}; expected {VERSION}")
    name = validate_name(value["name"])
    raw = value["steps"]
    if not isinstance(raw, list) or len(raw) > MAX_STEPS:
        raise ValueError(f"A clockwheel's steps must be a list of at most {MAX_STEPS}")
    steps = []
    for number, item in enumerate(raw, 1):
        try:
            steps.append(validate_step(item))
        except ValueError as exc:
            raise ValueError(f"Step {number}: {exc}") from exc
    if not any(step["type"] in MUSIC_KINDS for step in steps):
        raise ValueError("A clockwheel needs at least one music, category or track step")
    for index, step in enumerate(steps):
        if step["type"] == "liner" and steps[(index + 1) % len(steps)]["type"] == "liner":
            raise ValueError(
                f"Steps {index + 1} and {(index + 1) % len(steps) + 1} are both liners; "
                "put music between them"
            )
    return {
        "version": VERSION,
        "name": name,
        "steps": steps,
        "resync_minutes": validate_resync_minutes(value["resync_minutes"]),
    }


class ClockwheelStore:
    """Clockwheel files in one folder, named ``<name>.json``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, name: str) -> Path:
        return self.root / f"{validate_name(name)}.json"

    def list_names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        names = (p.stem for p in self.root.glob("*.json") if p.is_file())
        return sorted((n for n in names if _NAME.fullmatch(n)), key=str.casefold)

    def load(self, name: str) -> dict[str, Any]:
        path = self._path(name)
        if not path.is_file():
            raise ValueError(f"There is no clockwheel named {name!r}")
        try:
            clock = validate_clockwheel(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, OSError) as exc:
            raise ValueError(f"Clockwheel {name!r} could not be loaded: {exc}") from exc
        if clock["name"] != name:
            raise ValueError(
                f"Clockwheel file {path.name} holds the name {clock['name']!r}; "
                "the name inside must match the file name"
            )
        return clock

    def save(self, value: Any, *, overwrite: bool = True) -> dict[str, Any]:
        """Write a clockwheel atomically; without *overwrite*, refuse a taken name."""
        clock = validate_clockwheel(value)
        target = self._path(clock["name"])
        if not overwrite:
            taken = {n.casefold() for n in self.list_names()}
            if target.exists() or clock["name"].casefold() in taken:
                raise ValueError(f"A clockwheel named {clock['name']!r} already exists")
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".clock-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(clock, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return clock

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if not path.is_file():
            return False
        path.unlink()
        return True


def _split(path: str) -> tuple[Any, str]:
    """The path module for *path* and *path* normalized by it."""
    if path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return posixpath, posixpath.normpath(path)
    return ntpath, ntpath.normpath(path.replace("/", "\\"))


def locate_folder(folder: str, sources: Sequence[Mapping[str, Any]]) -> tuple[dict, str]:
    """Find the registered source holding *folder* and the folder's path inside it.

    The innermost source wins.  A folder below a source's own folder is
    only scanned when that source includes subfolders, so such a source
    is refused with a hint.
    """
    module, target = _split(folder)
    best: tuple[dict, str] | None = None
    for source in sources:
        root_module, root = _split(str(source["path"]))
        if root_module is not module:
            continue
        folded, folded_root = module.normcase(target), module.normcase(root).rstrip(module.sep)
        if folded != folded_root and not folded.startswith(folded_root + module.sep):
            continue
        inside = target[len(folded_root) :].strip(module.sep)
        if best is None or len(inside) < len(best[1]):
            best = (dict(source), inside)
    if best is None:
        raise ValueError(
            f"The folder {folder} is not inside a registered library folder. Add it, or a "
            "folder that holds it, under Music folders and Voice liner folders, then import again"
        )
    source, inside = best
    if inside and not source["recursive"]:
        raise ValueError(
            f"The folder {folder} is inside {source['path']}, which is registered without "
            "subfolders. Turn on Add subfolders for it, or register the folder itself, then "
            "import again"
        )
    return source, inside.replace(module.sep, "/")


def from_pal(
    name: str,
    slots: Sequence[ClockSlot],
    sources: Sequence[Mapping[str, Any]],
    minutes: Sequence[int],
) -> dict[str, Any]:
    """Convert parsed PAL slots, whose folders are absolute, into a native clockwheel."""
    steps = []
    for number, slot in enumerate(slots, 1):
        try:
            source, inside = locate_folder(
                slot.folder, [s for s in sources if s["kind"] == slot.kind]
            )
        except ValueError as exc:
            raise ValueError(f"PAL line for step {number}: {exc}") from exc
        steps.append(
            {
                "type": slot.kind,
                "source": source["id"],
                "folder": inside,
                "recursive": slot.recursive,
                "selection": slot.selection,
                "enforce_rules": slot.enforce_rules,
            }
        )
    return validate_clockwheel(
        {"version": VERSION, "name": name, "steps": steps, "resync_minutes": list(minutes)}
    )


def step_source(step: Mapping[str, Any], sources: Mapping[str, Any], number: int) -> Any:
    """The registered library folder a music, liner or track step uses."""
    wanted = "music" if step["type"] == "track" else step["type"]
    source = sources.get(step["source"])
    if source is None or source["kind"] != wanted:
        raise ValueError(
            f"Step {number}: its {wanted} folder is not registered; choose one in Library "
            "tools, Clockwheels"
        )
    return source


def source_folder(source_path: str, inside: str) -> str:
    """The absolute folder for a path inside a source, in the source's own style."""
    module, root = _split(source_path)
    return module.join(root, *inside.split("/")) if inside else root
