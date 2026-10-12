"""Declarative repeating playout clock definitions."""

from __future__ import annotations

import heapq
import ntpath
import posixpath
import random
import re
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class ClockSlot:
    folder: str = ""
    recursive: bool = False
    selection: str = "least_recent"
    enforce_rules: bool = True
    kind: str = "music"
    path: str = ""
    preset: str = ""
    category: str = ""

    def __post_init__(self) -> None:
        if self.kind in {"music", "liner"} and (
            not isinstance(self.folder, str) or not self.folder.strip()
        ):
            raise ValueError("folder must be a non-empty string")
        if type(self.recursive) is not bool or type(self.enforce_rules) is not bool:
            raise ValueError("recursive and enforce_rules must be booleans")
        if self.selection not in {"least_recent", "random"}:
            raise ValueError("selection must be 'least_recent' or 'random'")
        if self.kind not in {"music", "liner", "track", "settings", "category"}:
            raise ValueError("unknown clock slot kind")
        if self.kind == "track" and not self.path:
            raise ValueError("track step needs a path")
        if self.kind == "settings" and not self.preset:
            raise ValueError("settings step needs a preset")
        if self.kind == "category" and not self.category:
            raise ValueError("category step needs a category")

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ClockSlot:
        if not isinstance(value, Mapping):
            raise ValueError("clock slot JSON must be an object")
        allowed = {
            "folder",
            "recursive",
            "selection",
            "enforce_rules",
            "kind",
            "path",
            "preset",
            "category",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown clock slot field(s): {', '.join(sorted(unknown))}")
        try:
            return cls(**value)
        except TypeError as exc:
            raise ValueError(f"invalid clock slot fields: {exc}") from exc


_CALL = re.compile(
    r"(?i)^(Dir|SubDir)\s*\[\s*(['\x22])(.*?)\2\s*\]"
    r"\s*\.\s*QueueBottom\s*\(\s*(smLRP|smrandom)\s*,\s*(EnforceRules|NoRules)\s*\)\s*;\s*$"
)
_CLEAR = re.compile(r"(?i)^Queue\.Clear\s*;\s*$")


def _normalized(path: str) -> str:
    if path.startswith("/") and not path.startswith("//") and "\\" not in path:
        normalized = posixpath.normpath(path)
        return normalized if normalized == "/" else normalized.rstrip("/")
    return ntpath.normcase(ntpath.normpath(path.replace("/", "\\"))).rstrip("\\")


def _under(path: str, root: str) -> bool:
    path, root = _normalized(path), _normalized(root)
    posix = path.startswith("/") and not path.startswith("//")
    if posix != (root.startswith("/") and not root.startswith("//")):
        return False
    separator = "/" if posix else "\\"
    return path == root or path.startswith(root.rstrip(separator) + separator)


def parse_pal(text: str, liner_folders: Sequence[str] = ()) -> list[ClockSlot]:
    """Parse the supported queue-call subset of a PAL blueprint; never execute it."""
    if not isinstance(text, str):
        raise ValueError("PAL source must be text")
    slots: list[ClockSlot] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("//", "#")) or _CLEAR.fullmatch(line):
            continue
        match = _CALL.fullmatch(line)
        if not match:
            raise ValueError(f"unsupported PAL statement on line {number}: {line!r}")
        source, _, folder, mode, rules = match.groups()
        is_liner = any(_under(folder, root) for root in liner_folders)
        slots.append(
            ClockSlot(
                folder=folder,
                recursive=source.lower() == "subdir",
                selection="least_recent" if mode.lower() == "smlrp" else "random",
                enforce_rules=rules.lower() == "enforcerules",
                kind="liner" if is_liner else "music",
            )
        )
    return slots


def least_recent(
    entries: Sequence[Any],
    last_played: Mapping[str, Any],
    rng: Any = None,
    *,
    allowed: Callable[[Any], bool] | None = None,
) -> Any | None:
    """Choose the oldest allowed entry; prefer never-played and randomize ties."""
    if not entries:
        raise ValueError("entries must not be empty")
    chooser = rng if rng is not None else random
    if allowed is not None:
        ordered = list(entries)
        chooser.shuffle(ordered)
        heap = [
            ((stamp := last_played.get(entry.path)) is not None, stamp, index, entry)
            for index, entry in enumerate(ordered)
        ]
        heapq.heapify(heap)
        while heap:
            entry = heapq.heappop(heap)[-1]
            if allowed(entry):
                return entry
        return None
    oldest = min(
        (last_played.get(entry.path) for entry in entries),
        key=lambda stamp: (stamp is not None, stamp),
    )
    candidates = [entry for entry in entries if last_played.get(entry.path) == oldest]
    return chooser.choice(candidates)


@dataclass(frozen=True)
class ClockPick:
    """A selected music entry, its optional preceding liner, and resume slot.

    ``preset`` is the last settings step passed on the way to the entry.
    """

    entry: Any | None
    liner: Any | None
    next_cursor: int
    skipped: list[str]
    preset: str | None = None


#: Slot kinds that pick the music itself (a liner only rides on one).
MUSIC_KINDS = frozenset({"music", "track", "category"})


def validate_clock(slots: Sequence[ClockSlot]) -> list[ClockSlot]:
    """Validate a repeating clock and return its slots as a list."""
    result = list(slots)
    if not result or not any(slot.kind in MUSIC_KINDS for slot in result):
        raise ValueError("clock must contain at least one music, category or track slot")
    for index, slot in enumerate(result):
        if slot.kind == "liner" and result[(index + 1) % len(result)].kind == "liner":
            raise ValueError("clock cannot have consecutive liner slots, including across its end")
    return result


def _slot_entries(slot: ClockSlot, entries: Sequence[Any]) -> list[Any]:
    root = _normalized(slot.folder)
    separator = "/" if root.startswith("/") and not root.startswith("//") else "\\"
    found: list[Any] = []
    paths: set[str] = set()
    for entry in entries:
        path = getattr(entry, "path", None)
        if not isinstance(path, str):
            continue
        normalized = _normalized(path)
        if not _under(normalized, root) or normalized == root:
            continue
        parent = posixpath.dirname(normalized) if separator == "/" else ntpath.dirname(normalized)
        if not slot.recursive and _normalized(parent) != root:
            continue
        if normalized not in paths:
            paths.add(normalized)
            found.append(entry)
    return found


def pick_slot(
    slots: Sequence[ClockSlot],
    cursor: int,
    entries: Sequence[Any],
    last_played: Mapping[str, Any],
    allowed: Any,
    rng: Any = None,
    *,
    liners: Sequence[Any] = (),
    categories: Mapping[str, tuple[str, AbstractSet[str]]] | None = None,
) -> ClockPick:
    """Scan one clock cycle and select its next eligible music entry.

    ``entries`` and ``liners`` are sequences of IndexEntry-like objects with
    a ``path`` attribute. The clock slot kind chooses which pool to scan.
    ``allowed(entry)`` is consulted only for slots that enforce rules. A
    preceding usable liner slot is paired with the selected music item.
    ``categories`` maps a category ID to its name and the paths of its
    songs; a category slot picks from the entries with those paths, and
    one whose category is missing or empty is skipped like an empty folder.
    Nothing is written to playback history by this pure selection helper.
    """
    slots = validate_clock(slots)
    if type(cursor) is not int:
        raise ValueError("cursor must be an integer")
    if not callable(allowed):
        raise ValueError("allowed must be callable")
    start = cursor % len(slots)
    chooser = rng if rng is not None else random
    skipped: list[str] = []
    pending_liner = None
    preset = None

    def choose(
        slot: ClockSlot, matches: Sequence[Any], label: str
    ) -> tuple[Any | None, str | None]:
        if not matches:
            return None, f"{label}: no matching entries"
        if slot.selection == "least_recent":
            played_by_path = {
                _normalized(path): played
                for path, played in last_played.items()
                if isinstance(path, str)
            }
            history = {entry.path: played_by_path.get(_normalized(entry.path)) for entry in matches}
            selected = least_recent(
                matches,
                history,
                chooser,
                allowed=allowed if slot.enforce_rules else None,
            )
        else:
            candidates = [entry for entry in matches if not slot.enforce_rules or allowed(entry)]
            selected = chooser.choice(candidates) if candidates else None
        if selected is None:
            return (
                None,
                f"{label}: all {len(matches)} matching entries blocked by rules",
            )
        return selected, None

    for step in range(len(slots)):
        index = (start + step) % len(slots)
        slot = slots[index]
        if slot.kind == "settings":
            preset = slot.preset
            continue
        if slot.kind == "track":
            matches = [
                entry for entry in entries if _normalized(entry.path) == _normalized(slot.path)
            ]
            candidates = [entry for entry in matches if allowed(entry)]
            if candidates:
                return ClockPick(
                    candidates[0], pending_liner, (index + 1) % len(slots), skipped, preset
                )
            skipped.append(f"track step {slot.path}: recording missing or blocked by rules")
            continue
        if slot.kind == "category":
            found = (categories or {}).get(slot.category)
            if found is None:
                skipped.append(f"category step {slot.category}: the category no longer exists")
                continue
            name, members = found
            if not members:
                skipped.append(f"category {name}: the category has no songs")
                continue
            selected, reason = choose(
                slot, [entry for entry in entries if entry.path in members], f"category {name}"
            )
            if reason:
                skipped.append(reason)
            if selected is not None:
                return ClockPick(selected, pending_liner, (index + 1) % len(slots), skipped, preset)
            continue
        pool = liners if slot.kind == "liner" else entries
        selected, reason = choose(
            slot, _slot_entries(slot, pool), f"{slot.kind} slot {slot.folder}"
        )
        if reason:
            skipped.append(reason)
        if slot.kind == "liner":
            pending_liner = selected
            continue
        if selected is not None:
            return ClockPick(selected, pending_liner, (index + 1) % len(slots), skipped, preset)

    return ClockPick(None, None, start, skipped, preset)


def validate_resync_minutes(minutes: Sequence[int] | None = None) -> list[int]:
    """Validate and sort local-clock resync minutes; None defaults to the hour."""
    if minutes is None:
        return [0]
    if isinstance(minutes, (str, bytes)):
        raise ValueError("resync minutes must be a list of integers from 0 to 59")
    try:
        values = list(minutes)
    except TypeError as exc:
        raise ValueError("resync minutes must be a list of integers from 0 to 59") from exc
    if any(type(value) is not int or not 0 <= value <= 59 for value in values):
        raise ValueError("resync minutes must be integers from 0 to 59")
    return sorted(set(values))


def resync_due(entry_time: float, duration: float, boundary: float) -> bool:
    """Use the nearer natural handoff, without cutting the playing song early."""
    if entry_time >= boundary:
        return True
    after = entry_time + max(0.0, duration)
    return after >= boundary and boundary - entry_time <= after - boundary


def next_boundary(now: datetime, minutes: Sequence[int] | None = None) -> datetime | None:
    """Return the next configured wall-clock minute in ``now``'s timezone.

    Nonexistent daylight-saving times are skipped and repeated wall times are
    considered in both folds. An empty minute list disables resync boundaries.
    """
    minute_values = validate_resync_minutes(minutes)
    if not minute_values:
        return None
    if not isinstance(now, datetime):
        raise ValueError("now must be a datetime")
    timezone_info = now.tzinfo if now.utcoffset() is not None else None
    local = now.replace(tzinfo=None)
    first_hour = local.replace(minute=0, second=0, microsecond=0)
    for hours in range(73):
        hour = first_hour + timedelta(hours=hours)
        candidates: list[datetime] = []
        for minute in minute_values:
            wall = hour.replace(minute=minute)
            if timezone_info is None:
                if wall > local:
                    candidates.append(wall)
                continue
            for fold in (0, 1):
                candidate = wall.replace(tzinfo=timezone_info, fold=fold)
                round_trip = candidate.astimezone(UTC).astimezone(timezone_info)
                if round_trip.replace(tzinfo=None) != wall:
                    continue
                if candidate.timestamp() > now.timestamp():
                    candidates.append(candidate)
        if candidates:
            return min(candidates, key=lambda candidate: candidate.timestamp())
    raise ValueError("could not find a resync boundary within 73 hours")
