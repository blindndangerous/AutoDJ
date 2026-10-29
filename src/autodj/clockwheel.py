"""Declarative repeating playout clock definitions."""

from __future__ import annotations

import copy
import functools
import heapq
import ntpath
import posixpath
import random
import re
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from autodj.clock_conditions import relative_date_now, uses_relative_dates
from autodj.hour_fit import FIT_CANDIDATES

if TYPE_CHECKING:
    from autodj.repeat_rules import SongVersions


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
    require_following: bool = False
    conditions: tuple[dict[str, Any], ...] = ()
    match: str = "all"
    max_spots: int = 3
    max_seconds: int = 0
    advertiser: str = ""
    ad_category: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.conditions, (list, tuple)) or len(self.conditions) > 32:
            raise ValueError("A slot allows a list of at most 32 conditions")
        if self.conditions:
            from autodj.clock_conditions import (
                validate_category_condition,
                validate_metadata_condition,
            )

            if self.kind not in {"music", "liner"} or self.folder:
                raise ValueError("Only music or liner slots can use conditions instead of a folder")
            for condition in self.conditions:
                if not isinstance(condition, dict):
                    raise ValueError("Each condition must be an object")
                if condition.get("field") == "category":
                    if self.kind != "music":
                        raise ValueError("Category conditions are only available for music steps")
                    validate_category_condition(condition)
                elif condition.get("field") == "folder":
                    if set(condition) - {"field", "folder", "recursive", "operator"} or not {
                        "field",
                        "folder",
                        "recursive",
                    } <= set(condition):
                        raise ValueError("Invalid resolved folder condition")
                    if condition.get("operator", "is") not in {"is", "is_not"}:
                        raise ValueError("Invalid folder condition operator")
                    ClockSlot(folder=condition["folder"], recursive=condition["recursive"])
                else:
                    validate_metadata_condition(condition)
            object.__setattr__(self, "conditions", tuple(self.conditions))
        if self.match not in ("all", "any"):
            raise ValueError("Choose Match all or Match any for the slot")
        if (
            not self.conditions
            and self.kind in {"music", "liner"}
            and (not isinstance(self.folder, str) or not self.folder.strip())
        ):
            raise ValueError("folder must be a non-empty string")
        if type(self.recursive) is not bool or type(self.enforce_rules) is not bool:
            raise ValueError("recursive and enforce_rules must be booleans")
        if type(self.require_following) is not bool:
            raise ValueError("require_following must be a boolean")
        if self.selection not in {"least_recent", "random"}:
            raise ValueError("selection must be 'least_recent' or 'random'")
        if self.kind not in {"music", "liner", "track", "settings", "category", "ad_break"}:
            raise ValueError("unknown clock slot kind")
        if self.kind == "ad_break":
            from autodj.ads import MAX_SPOTS_PER_BREAK, NAME_MAX

            if type(self.max_spots) is not int or not 1 <= self.max_spots <= MAX_SPOTS_PER_BREAK:
                raise ValueError("max_spots must be an integer from 1 to 10")
            if type(self.max_seconds) is not int or (
                self.max_seconds != 0 and not 10 <= self.max_seconds <= 900
            ):
                raise ValueError("max_seconds must be 0 or an integer from 10 to 900")
            if any(
                not isinstance(v, str) or len(v) > NAME_MAX
                for v in (self.advertiser, self.ad_category)
            ):
                raise ValueError("ad filters must be text of at most 100 characters")
        if self.kind == "track" and not self.path:
            raise ValueError("track step needs a path")
        if self.kind == "settings" and not self.preset:
            raise ValueError("settings step needs a preset")
        if self.kind == "category" and not self.category:
            raise ValueError("category step needs a category")
        if self.require_following and self.kind != "liner":
            raise ValueError("require_following is only valid for liner slots")


_CALL = re.compile(
    r"(?i)^(Dir|SubDir)\s*\[\s*(['\x22])(.*?)\2\s*\]"
    r"\s*\.\s*QueueBottom\s*\(\s*(smLRP|smrandom)\s*,\s*(EnforceRules|NoRules)\s*\)\s*;\s*$"
)
_CLEAR = re.compile(r"(?i)^Queue\.Clear\s*;\s*$")


@functools.lru_cache(maxsize=1 << 18)
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
    versions: SongVersions | None = None,
    fit: Callable[[Sequence[Any]], Any] | None = None,
    preferred_path: str | None = None,
) -> Any | None:
    """Choose the oldest allowed entry; prefer never-played and randomize ties.

    With *versions*, a song's age is that of its latest played version,
    and the oldest version of the oldest song wins.  With *fit* (hour
    fitting), it chooses among the oldest few allowed songs, oldest first.
    """
    return _least_recent(
        entries,
        last_played.get,
        rng,
        allowed=allowed,
        versions=versions,
        preferred_path=preferred_path,
        fit=fit,
    )


def _preferred_entry(entries: Sequence[Any], path: str | None) -> Any | None:
    if path is None:
        return None
    return next((entry for entry in entries if _normalized(entry.path) == _normalized(path)), None)


def _least_recent(
    entries: Sequence[Any],
    played: Callable[[str], Any],
    rng: Any = None,
    *,
    allowed: Callable[[Any], bool] | None = None,
    versions: SongVersions | None = None,
    fit: Callable[[Sequence[Any]], Any] | None = None,
    preferred_path: str | None = None,
) -> Any | None:
    if not entries:
        raise ValueError("entries must not be empty")
    chooser = rng if rng is not None else random
    song_played = versions.last_played(played) if versions else None
    song_order: dict[str, int] = {}
    if versions:
        songs = list(dict.fromkeys(versions.song(entry.path) for entry in entries))
        chooser.shuffle(songs)
        preferred = _preferred_entry(entries, preferred_path)
        preferred_song = versions.song(preferred.path) if preferred is not None else None
        if preferred_song is not None:
            songs.remove(preferred_song)
            songs.insert(0, preferred_song)
        song_order = {song: index for index, song in enumerate(songs)}

    def age(entry: Any) -> tuple[Any, ...]:
        # Never played sorts first, then the oldest play.
        stamp = played(entry.path)
        if song_played is None:
            return (stamp is not None, stamp)
        assert versions is not None
        song = song_played(entry.path)
        # Break equal song ages before considering versions, so extra copies add no odds.
        return (
            song is not None,
            song,
            song_order[versions.song(entry.path)],
            stamp is not None,
            stamp,
        )

    if allowed is not None or fit is not None:
        ordered = list(entries)
        chooser.shuffle(ordered)
        heap = [
            (
                *age(entry),
                0
                if preferred_path is not None
                and _normalized(entry.path) == _normalized(preferred_path)
                else 1,
                index,
                entry,
            )
            for index, entry in enumerate(ordered)
        ]
        heapq.heapify(heap)
        fitting: list[Any] = []
        fitted_songs: set[str] = set()
        while heap:
            entry = heapq.heappop(heap)[-1]
            if allowed is None or allowed(entry):
                if fit is None:
                    return entry
                if versions is not None:
                    song = versions.song(entry.path)
                    if song in fitted_songs:
                        continue
                    fitted_songs.add(song)
                fitting.append(entry)
                if len(fitting) == FIT_CANDIDATES:
                    break
        if fitting:
            assert fit is not None
            return fit(fitting)
        return None
    ages = [age(entry) for entry in entries]
    oldest = min(ages)
    candidates = [entry for entry, when in zip(entries, ages, strict=True) if when == oldest]
    preferred = _preferred_entry(candidates, preferred_path)
    if preferred is not None:
        return preferred
    return chooser.choice(candidates)


@dataclass(frozen=True)
class ClockPick:
    """A selected music entry, its optional preceding liner, and resume slot.

    ``preset`` is the last settings step passed on the way to the entry.
    ``step_index`` and ``liner_step_index`` identify the steps that selected them.
    The player attaches display snapshots from the same clock used for selection.
    """

    entry: Any | None
    liner: Any | None
    next_cursor: int
    skipped: list[str]
    preset: str | None = None
    liner_requires_following: bool = False
    step_index: int | None = None
    liner_step_index: int | None = None
    step_snapshot: dict[str, Any] | None = None
    liner_step_snapshot: dict[str, Any] | None = None
    ad_steps: tuple[int, ...] = ()
    ad_step_snapshots: tuple[dict[str, Any], ...] = ()


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


def condition_paths(
    slot: ClockSlot,
    entries: Sequence[Any],
    beets: Mapping[str, Mapping[str, Any]] | None = None,
    categories: Mapping[str, tuple[str, AbstractSet[str]]] | None = None,
    now: float | None = None,
) -> set[str]:
    """The paths of *entries* that pass a conditions slot's Match all or Match any.

    *now* is the wall-clock time a relative date condition (within the last
    N days) is judged at, the current time when not given.
    """
    from autodj.catalog import normalize_path
    from autodj.clock_conditions import condition_matcher

    found_paths: set[str] | None = None
    for condition in slot.conditions:
        if condition["field"] == "folder":
            matched = _slot_entries(
                ClockSlot(folder=condition["folder"], recursive=condition["recursive"]), entries
            )
            if condition.get("operator") == "is_not":
                paths = {item.path for item in matched}
                matched = [entry for entry in entries if entry.path not in paths]
        elif condition["field"] == "category":
            if not categories or condition["category"] not in categories:
                matched = []
            else:
                _, members = categories[condition["category"]]
                matched = [
                    entry
                    for entry in entries
                    if (entry.path in members) == (condition.get("operator") != "is_not")
                ]
        elif condition["field"] == "beets" and beets:
            test = condition_matcher(condition)
            matched = [
                entry
                for entry in entries
                if test(entry, beets.get(normalize_path(entry.path)), now)
            ]
        else:
            test = condition_matcher(condition)
            matched = [entry for entry in entries if test(entry, None, now)]
        paths = {entry.path for entry in matched}
        if found_paths is None:
            found_paths = paths
        elif slot.match == "all":
            found_paths &= paths
        else:
            found_paths |= paths
        if slot.match == "all" and not found_paths:
            break  # Nothing can pass every condition now.
    return found_paths or set()


class ConditionPaths:
    """Each conditions step's matching paths, cached until its source data changes.

    Build one from the music and liner entries a pick draws its pools from
    and the Beets values it reads; a pick then only looks each path up.  The
    entries are compared by identity, so a new one is needed whenever the
    library publishes new entries or Beets is read again. Category memberships
    are compared by value because the category index returns a new mapping per pick.
    When a step has a relative date condition (within the last N days), *now*,
    the time of the pick, is part of the key too, to the hour the condition
    moves by (:func:`autodj.clock_conditions.relative_date_now`).
    """

    def __init__(
        self,
        slots: Sequence[ClockSlot],
        entries: Sequence[Any],
        liners: Sequence[Any],
        beets: Mapping[str, Mapping[str, Any]] | None,
        categories: Mapping[str, tuple[str, AbstractSet[str]]] | None = None,
        now: float | None = None,
    ) -> None:
        self._key = (slots, entries, liners, beets)
        self._categories = categories
        self._relative = any(uses_relative_dates(slot.conditions) for slot in slots)
        self._now = self._hour(now)
        self._paths: dict[int, frozenset[str]] = {}

    def _hour(self, now: float | None) -> int | None:
        return relative_date_now(now) if self._relative else None

    def current(
        self,
        slots: Sequence[ClockSlot],
        entries: Sequence[Any],
        liners: Sequence[Any],
        beets: Mapping[str, Mapping[str, Any]] | None,
        categories: Mapping[str, tuple[str, AbstractSet[str]]] | None = None,
        now: float | None = None,
    ) -> bool:
        """Whether this was built from these very slots, entries and Beets values.

        With a relative date condition, also within the same hour of *now*.
        """
        return (
            self._categories == categories
            and all(
                old is new
                for old, new in zip(self._key, (slots, entries, liners, beets), strict=True)
            )
            and self._hour(now) == self._now
        )

    def copy_for(self, slots: Sequence[ClockSlot]) -> ConditionPaths | None:
        """A cache of its own for equal *slots*, starting with the steps worked out so far.

        A dry run loads its own slots from the same clockwheel; this lets it
        start from the live player's results instead of matching the whole
        library again.  ``None`` when the slots differ.  Steps either cache
        works out later stay in that cache.
        """
        if list(slots) != list(self._key[0]):
            return None
        twin = copy.copy(self)
        twin._key = (slots, *self._key[1:])
        twin._paths = dict(self._paths)
        return twin

    def paths(self, index: int) -> frozenset[str]:
        """The paths step *index* matches, worked out the first time it is asked for."""
        found = self._paths.get(index)
        if found is None:
            slots, entries, liners, beets = self._key
            slot = slots[index]
            pool = liners if slot.kind == "liner" else entries
            found = self._paths[index] = frozenset(
                condition_paths(slot, pool, beets, self._categories, self._now)
            )
        return found


def _slot_entries(
    slot: ClockSlot,
    entries: Sequence[Any],
    beets: Mapping[str, Mapping[str, Any]] | None = None,
    categories: Mapping[str, tuple[str, AbstractSet[str]]] | None = None,
) -> list[Any]:
    if slot.conditions:
        found_paths = condition_paths(slot, entries, beets, categories)
        return [entry for entry in entries if entry.path in found_paths]
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
    beets: Mapping[str, Mapping[str, Any]] | None = None,
    condition_errors: Mapping[int, str] | None = None,
    condition_cache: ConditionPaths | None = None,
    closed_categories: Mapping[str, str] | None = None,
    normalized_history: Mapping[str, Any] | None = None,
    versions: SongVersions | None = None,
    fit: Callable[[Sequence[Any]], Any] | None = None,
    preferred_path: str | None = None,
    preferred_for_step: Callable[[int], str | None] | None = None,
) -> ClockPick:
    """Scan one clock cycle and select its next eligible music entry.

    ``entries`` and ``liners`` are sequences of IndexEntry-like objects with
    a ``path`` attribute. The clock slot kind chooses which pool to scan.
    ``allowed(entry)`` is consulted only for slots that enforce rules. A
    preceding usable liner slot is paired with the selected music item.
    ``categories`` maps a category ID to its name and the paths of its
    songs; a category slot picks from the entries with those paths, and
    one whose category is missing or empty is skipped like an empty folder.
    ``closed_categories`` maps the ID of a category outside its allowed
    days and hours to the reason, and its step is skipped the same way.
    ``condition_cache``, built from these *slots* and the entries the
    pools were drawn from, saves working out a conditions step again.
    ``normalized_history`` can be shared across retry scans for the same pick.
    With ``versions`` (Count versions as one song), each song is as likely
    as any other however many versions it has, and a song's age for least
    recently played is that of its latest played version.
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
    pending_liner_requires_following = False
    pending_liner_index = None
    preset = None
    ad_steps: list[int] = []
    closed_paths = {
        path
        for category in (closed_categories or {})
        for path in (categories or {}).get(category, ("", frozenset()))[1]
    }
    # Build the normalized lookup lazily for direct callers without a shared one.
    played = normalized_history

    def choose(
        slot: ClockSlot, matches: Sequence[Any], label: str
    ) -> tuple[Any | None, str | None]:
        nonlocal played
        preference = (
            preferred_for_step(index)
            if preferred_for_step is not None and slot.kind != "liner"
            else preferred_path
        )
        if not matches:
            return None, f"{label}: no matching entries"
        if slot.selection == "least_recent":
            if played is None:
                played = {
                    _normalized(path): stamp
                    for path, stamp in last_played.items()
                    if isinstance(path, str)
                }
            played_by_path = played
            selected = _least_recent(
                matches,
                lambda path: played_by_path.get(_normalized(path)),
                chooser,
                allowed=allowed if slot.enforce_rules else None,
                versions=versions,
                fit=fit,
                preferred_path=preference,
            )
        else:
            candidates = [entry for entry in matches if not slot.enforce_rules or allowed(entry)]
            preferred = _preferred_entry(candidates, preference)
            if not candidates:
                selected = None
            elif fit is None:
                selected = (
                    preferred
                    if preferred is not None
                    else (
                        versions.choice(candidates, chooser)
                        if versions
                        else chooser.choice(candidates)
                    )
                )
            else:
                if versions:
                    songs: dict[str, list[Any]] = {}
                    for entry in candidates:
                        songs.setdefault(versions.song(entry.path), []).append(entry)
                    sampled_songs = chooser.sample(list(songs), min(FIT_CANDIDATES, len(songs)))
                    sampled = [chooser.choice(songs[song]) for song in sampled_songs]
                else:
                    sampled = chooser.sample(candidates, min(FIT_CANDIDATES, len(candidates)))
                if preferred is not None:
                    # Keep the remembered version first so an equally good fit stays put.
                    sampled = [
                        preferred,
                        *(
                            entry
                            for entry in sampled
                            if (
                                versions.song(entry.path) != versions.song(preferred.path)
                                if versions
                                else entry is not preferred
                            )
                        ),
                    ]
                    sampled = sampled[:FIT_CANDIDATES]
                selected = fit(sampled)
        if selected is None:
            return (
                None,
                f"{label}: all {len(matches)} matching entries blocked by rules",
            )
        return selected, None

    for step in range(len(slots)):
        index = (start + step) % len(slots)
        slot = slots[index]
        if condition_errors and index in condition_errors:
            skipped.append(f"Step {index + 1}: {condition_errors[index]}")
            if slot.kind == "liner":
                pending_liner = None
                pending_liner_requires_following = False
            continue
        if slot.kind == "settings":
            preset = slot.preset
            continue
        if slot.kind == "ad_break":
            ad_steps.append(index)
            continue
        if slot.kind == "track":
            matches = [
                entry for entry in entries if _normalized(entry.path) == _normalized(slot.path)
            ]
            candidates = [entry for entry in matches if allowed(entry)]
            if candidates:
                return ClockPick(
                    candidates[0],
                    pending_liner,
                    (index + 1) % len(slots),
                    skipped,
                    preset,
                    pending_liner_requires_following if pending_liner is not None else False,
                    index,
                    pending_liner_index,
                    ad_steps=tuple(ad_steps),
                )
            skipped.append(
                f"Step {index + 1}: track step {slot.path}: recording missing or blocked by rules"
            )
            continue
        if slot.kind == "category":
            found = (categories or {}).get(slot.category)
            if found is None:
                skipped.append(
                    f"Step {index + 1}: category step {slot.category}: the category no longer exists"
                )
                continue
            name, members = found
            closed = (closed_categories or {}).get(slot.category)
            if closed:
                skipped.append(f"Step {index + 1}: category {name}: {closed}")
                continue
            if not members:
                skipped.append(f"Step {index + 1}: category {name}: the category has no songs")
                continue
            selected, reason = choose(
                slot, [entry for entry in entries if entry.path in members], f"category {name}"
            )
            if reason:
                skipped.append(f"Step {index + 1}: {reason}")
            if selected is not None:
                return ClockPick(
                    selected,
                    pending_liner,
                    (index + 1) % len(slots),
                    skipped,
                    preset,
                    pending_liner_requires_following if pending_liner is not None else False,
                    index,
                    pending_liner_index,
                    ad_steps=tuple(ad_steps),
                )
            continue
        missing = next(
            (
                c["category"]
                for c in slot.conditions
                if c["field"] == "category" and c["category"] not in (categories or {})
            ),
            None,
        )
        if missing is not None:
            skipped.append(f"Step {index + 1}: category {missing}: the category no longer exists")
            continue
        pool = liners if slot.kind == "liner" else entries
        if slot.conditions and slot.kind == "music" and closed_paths:
            pool = [entry for entry in pool if entry.path not in closed_paths]
        if slot.conditions and condition_cache is not None:
            step_paths = condition_cache.paths(index)
            matches = [entry for entry in pool if entry.path in step_paths]
        else:
            matches = _slot_entries(slot, pool, beets, categories)
        selected, reason = choose(
            slot,
            matches,
            f"{slot.kind} conditions" if slot.conditions else f"{slot.kind} slot {slot.folder}",
        )
        if reason:
            skipped.append(f"Step {index + 1}: {reason}")
        if slot.kind == "liner":
            pending_liner = selected
            pending_liner_index = index if selected is not None else None
            pending_liner_requires_following = (
                slot.require_following if selected is not None else False
            )
            continue
        if selected is not None:
            return ClockPick(
                selected,
                pending_liner,
                (index + 1) % len(slots),
                skipped,
                preset,
                pending_liner_requires_following if pending_liner is not None else False,
                index,
                pending_liner_index,
                ad_steps=tuple(ad_steps),
            )

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
