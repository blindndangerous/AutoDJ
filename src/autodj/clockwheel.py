"""Declarative repeating playout clock definitions."""

from __future__ import annotations

import ntpath
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class ClockSlot:
    folder: str
    recursive: bool = False
    selection: str = "least_recent"
    enforce_rules: bool = True
    kind: str = "music"

    def __post_init__(self) -> None:
        if not isinstance(self.folder, str) or not self.folder.strip():
            raise ValueError("folder must be a non-empty string")
        if type(self.recursive) is not bool or type(self.enforce_rules) is not bool:
            raise ValueError("recursive and enforce_rules must be booleans")
        if self.selection not in {"least_recent", "random"}:
            raise ValueError("selection must be 'least_recent' or 'random'")
        if self.kind not in {"music", "liner"}:
            raise ValueError("kind must be 'music' or 'liner'")

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ClockSlot:
        if not isinstance(value, Mapping):
            raise ValueError("clock slot JSON must be an object")
        allowed = {"folder", "recursive", "selection", "enforce_rules", "kind"}
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
    return ntpath.normcase(ntpath.normpath(path.replace("/", "\\"))).rstrip("\\")


def _under(path: str, root: str) -> bool:
    path, root = _normalized(path), _normalized(root)
    return path == root or path.startswith(root + "\\")


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


def least_recent(entries: Sequence[Any], last_played: Mapping[str, Any], rng: Any = None) -> Any:
    """Choose randomly among entries in the oldest play-time group; never-played wins."""
    if not entries:
        raise ValueError("entries must not be empty")
    chooser = rng if rng is not None else random
    oldest = min(
        (last_played.get(entry.path) for entry in entries),
        key=lambda stamp: (stamp is not None, stamp),
    )
    candidates = [entry for entry in entries if last_played.get(entry.path) == oldest]
    return chooser.choice(candidates)


@dataclass(frozen=True)
class ClockPick:
    """A selected music entry, its optional preceding liner, and resume slot."""

    entry: Any | None
    liner: Any | None
    next_cursor: int
    skipped: list[str]


def validate_clock(slots: Sequence[ClockSlot]) -> list[ClockSlot]:
    """Validate a repeating clock and return its slots as a list."""
    result = list(slots)
    if not result or not any(slot.kind == "music" for slot in result):
        raise ValueError("clock must contain at least one music slot")
    for index, slot in enumerate(result):
        if slot.kind == "liner" and result[(index + 1) % len(result)].kind == "liner":
            raise ValueError("clock cannot have consecutive liner slots, including across its end")
    return result


def _slot_entries(slot: ClockSlot, entries: Sequence[Any]) -> list[Any]:
    root = _normalized(slot.folder)
    found: list[Any] = []
    paths: set[str] = set()
    for entry in entries:
        path = getattr(entry, "path", None)
        if not isinstance(path, str):
            continue
        normalized = _normalized(path)
        if not normalized.startswith(root + "\\"):
            continue
        if not slot.recursive and _normalized(ntpath.dirname(normalized)) != root:
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
) -> ClockPick:
    """Scan one clock cycle and select its next eligible music entry.

    ``entries`` and ``liners`` are sequences of IndexEntry-like objects with
    a ``path`` attribute. The clock slot kind chooses which pool to scan.
    ``allowed(entry)`` is consulted only for slots that enforce rules. A
    preceding usable liner slot is paired with the selected music item.
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

    def choose(slot: ClockSlot, pool: Sequence[Any]) -> tuple[Any | None, str | None]:
        matches = _slot_entries(slot, pool)
        if not matches:
            return None, f"{slot.kind} slot {slot.folder}: no matching entries"
        candidates = [entry for entry in matches if not slot.enforce_rules or allowed(entry)]
        if not candidates:
            return (
                None,
                f"{slot.kind} slot {slot.folder}: all {len(matches)} matching entries blocked by rules",
            )
        if slot.selection == "least_recent":
            played_by_path = {
                _normalized(path): played
                for path, played in last_played.items()
                if isinstance(path, str)
            }
            history = {
                entry.path: played_by_path.get(_normalized(entry.path)) for entry in candidates
            }
            return least_recent(candidates, history, chooser), None
        return chooser.choice(candidates), None

    for step in range(len(slots)):
        index = (start + step) % len(slots)
        slot = slots[index]
        pool = liners if slot.kind == "liner" else entries
        selected, reason = choose(slot, pool)
        if reason:
            skipped.append(reason)
        if slot.kind == "liner":
            pending_liner = selected
            continue
        if selected is not None:
            return ClockPick(selected, pending_liner, (index + 1) % len(slots), skipped)

    return ClockPick(None, None, start, skipped)


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
