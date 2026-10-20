"""Intro rules (#127): a voice liner for the next song when it matches clockwheel conditions.

Each station keeps its rules in ``intro_rules.json`` in its state folder, by
rule ID.  A rule has the clockwheel's conditions (validated and matched by
the same code as clockwheel steps and categories, #204), registered liner
IDs, a registered liner folder or both, a chance, a wait between plays, play
dates and a priority next to the song's and artist's own matched intros.

The file is handled as ``matched_intros.json`` is: read again when it changes
on disk; a rule that is not valid is skipped, logged and written back as it
was; a file that is not a file of rules is set aside as ``.bad-<id>``; one
that cannot be read just now is left alone, the rules last read are used and
saves are refused.

:meth:`IntroRules.choose` runs on the render-ahead thread and rolls on the
matched intros' own random source, never the picker's.  A liner folder's
clips are gathered by :meth:`IntroRules.upkeep` on the station's tick, so a
render does not walk a large liner library; and at most :data:`MAX_TRIES`
of a large pool's clips are looked at for one song change.  The mix bus
reports a rule on the air through :meth:`IntroRules.heard`, which never
waits, and the tick writes when it aired, so its wait holds across restarts.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import math
import random
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from datetime import date as Date
from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from autodj.catalog import normalize_path
from autodj.clip_dates import ClipDates
from autodj.clock_conditions import condition_matcher
from autodj.clockwheel import ClockSlot, _normalized, condition_paths
from autodj.clockwheel_store import _condition, resolve_conditions
from autodj.fsutil import atomic_write, confirm_missing
from autodj.matched_intros import MAX_COOLDOWN_MINUTES, Chosen

logger = logging.getLogger(__name__)
MAX_RULES = 2000
#: How often the station's tick checks folder-rule clip pools, in seconds.
WARM_SECONDS = 5.0
#: The most clips of one large rule looked at for one song change.
MAX_TRIES = 256


FILENAME_FIELDS = frozenset({"year", "artist", "title", "album", "genre", "bpm"})


@lru_cache(maxsize=256)
def _filename_parts(pattern: str) -> tuple[tuple[str, str | None, str | None, str | None], ...]:
    if not pattern.strip() or len(pattern) > 240:
        raise ValueError("Enter a filename pattern of 1 to 240 characters")
    if any(char in pattern for char in ("/", "\\")) or any(ord(char) < 32 for char in pattern):
        raise ValueError("Match a filename, without folder separators or control characters")
    try:
        parts = tuple(Formatter().parse(pattern))
    except ValueError:
        raise ValueError("Use balanced braces around song fields, such as {year}") from None
    for _literal, field, spec, conversion in parts:
        if field is not None and field not in FILENAME_FIELDS:
            raise ValueError(
                f"Unknown song field {{{field}}}; use year, artist, title, album, genre or bpm"
            )
        if spec or conversion:
            raise ValueError("Song fields do not accept format specifications or conversions")
    return parts


def resolve_filename_pattern(
    pattern: str, entry: Any
) -> tuple[str | None, re.Pattern[str] | None, tuple[str, ...]]:
    """Expand song fields literally; only the pattern's own * and ? are wildcards."""
    display: list[str] = []
    glob: list[str] = []
    missing: list[str] = []
    for literal, field, _spec, _conversion in _filename_parts(pattern):
        display.append(literal)
        glob.append(literal.replace("[", "[[]"))
        if field is None:
            continue
        value = getattr(entry, field, None)
        if field in {"year", "bpm"}:
            try:
                number = float(value) if value is not None else 0.0
                value = (
                    str(math.floor(number + 0.5)) if math.isfinite(number) and number > 0 else ""
                )
            except (TypeError, ValueError, OverflowError):
                value = ""
        else:
            value = str(value).strip() if value is not None else ""
        if not value:
            if field not in missing:
                missing.append(field)
            continue
        display.append(value)
        glob.append(value.replace("[", "[[]").replace("*", "[*]").replace("?", "[?]"))
    if missing:
        return None, None, tuple(missing)
    return "".join(display), re.compile(fnmatch.translate("".join(glob)), re.IGNORECASE), ()


class IntroRule(BaseModel):
    """Conditions and registered liner clips for one next-song rule."""

    model_config = ConfigDict(extra="forbid")
    conditions: list[dict[str, Any]] = Field(min_length=1, max_length=32)
    match: Literal["all", "any"] = "all"
    priority: Literal["before_song", "before_artist", "after_artist"] = "after_artist"
    clip_ids: list[str] = Field(default_factory=list, max_length=100)
    liner_folder: str | None = None
    filename_pattern: str | None = Field(default=None, max_length=240)
    label: str = Field(default="", max_length=120)
    chance: int = Field(default=100, ge=1, le=100)
    cooldown_minutes: int = Field(default=60, ge=0, le=MAX_COOLDOWN_MINUTES)
    not_before: Date | None = None
    not_after: Date | None = None
    created_at: float = 0.0
    last_aired: float | None = None

    @field_validator("conditions")
    @classmethod
    def _conditions(cls, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        checked = []
        for number, value in enumerate(values, 1):
            try:
                checked.append(_condition(value))
            except ValueError as exc:
                raise ValueError(f"Condition {number}: {exc}") from None
        return checked

    @field_validator("clip_ids")
    @classmethod
    def _clip_ids(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 64 for value in values):
            raise ValueError("Each chosen liner must be a library ID")
        if len(set(values)) != len(values):
            raise ValueError("Choose each liner once")
        return values

    @field_validator("liner_folder")
    @classmethod
    def _liner_folder(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value) > 1000):
            raise ValueError("Choose a registered voice liner folder")
        return value

    @field_validator("filename_pattern")
    @classmethod
    def _filename_pattern(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        _filename_parts(value)
        if not info.data.get("liner_folder"):
            raise ValueError("Choose a registered liner folder for filename matching")
        if info.data.get("clip_ids"):
            raise ValueError("Choose filename matching or specific liners, not both")
        return value

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.clip_ids and not self.liner_folder:
            raise ValueError("Choose at least one voice liner or a liner folder")
        self.dates  # noqa: B018 -- raises ClipDatesError for dates out of order
        return self

    @property
    def dates(self) -> ClipDates:
        return ClipDates(not_before=self.not_before, not_after=self.not_after)


class IntroRules:
    """Per-station rules; unreadable files block writes and preserve last good state."""

    def __init__(
        self,
        path: Path,
        day_at: Any,
        rng: random.Random,
        library: Callable[[], Any] | None = None,
        paths: Any = None,
    ) -> None:
        self.path = path
        # The station's library, so the tick can warm folder pools.
        self.library = library
        self.paths = paths
        # Unknown until the file is read: the first tick reads it.
        self._has_folders = True
        self._warmed_at = float("-inf")
        self._day_at = day_at
        self._rng = rng
        self._lock = threading.Lock()
        self._frozen = False
        self._snapshot_loading = False
        self._stamp: tuple[int, int, int] | None = None
        self._seen = False
        self._rules: dict[str, IntroRule] = {}
        self._skipped: dict[str, Any] = {}
        self._compiled: dict[str, list[Any]] = {}
        self._liner_snapshot: Any = None
        self._folder_pools: dict[str, tuple[Any, ...]] = {}
        self._pattern_pools: dict[tuple[str, str], tuple[tuple[Any, ...], tuple[Any, ...]]] = {}
        self._aired: dict[str, float] = {}
        self._reserved: dict[str, float] = {}
        self._heard: deque[tuple[str, float]] = deque()
        self._released: deque[str] = deque()
        self._unwritten = False

    def _stat(self) -> tuple[int, int, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError as exc:
            confirm_missing(self.path, exc)
            return None
        return stat.st_mtime_ns, stat.st_size, stat.st_ino

    def _load(self) -> dict[str, IntroRule]:
        if self._frozen and not self._snapshot_loading:
            return dict(self._rules)
        stamp = self._stat()
        if stamp is not None:
            self._seen = True
        if stamp == self._stamp:
            return dict(self._rules)
        rules: dict[str, IntroRule] = {}
        skipped: dict[str, Any] = {}
        compiled: dict[str, list[Any]] = {}
        if stamp is not None:
            raw = self.path.read_bytes()
            try:
                document = json.loads(raw)
                if (
                    not isinstance(document, dict)
                    or document.get("schema_version") != 1
                    or not isinstance(document.get("rules"), dict)
                ):
                    raise ValueError("invalid intro rules document")
            except (ValueError, UnicodeError) as exc:
                if self._frozen:
                    logger.warning("Intro rules file %s is invalid (%s)", self.path, exc)
                else:
                    bad = self.path.with_name(f"{self.path.name}.bad-{uuid.uuid4().hex[:8]}")
                    try:
                        self.path.replace(bad)
                    except OSError:
                        logger.exception(
                            "Could not set aside invalid intro rules file %s", self.path
                        )
                        raise
                    logger.error(
                        "Intro rules file %s was invalid (%s); saved as %s", self.path, exc, bad
                    )
                stamp = None
            else:
                for key, value in document["rules"].items():
                    try:
                        if (
                            not isinstance(key, str)
                            or len(key) != 32
                            or any(c not in "0123456789abcdef" for c in key)
                        ):
                            raise ValueError("invalid rule ID")
                        rule = IntroRule.model_validate(value)
                        rules[key] = rule
                        compiled[key] = [
                            condition_matcher(c)
                            if c["field"] not in {"folder", "category"}
                            else None
                            for c in rule.conditions
                        ]
                    except ValueError as exc:
                        skipped[key] = value
                        logger.error("Skipping invalid intro rule %s: %s", key, exc)
        self._stamp, self._rules, self._skipped, self._compiled = stamp, rules, skipped, compiled
        self._has_folders = any(rule.liner_folder for rule in rules.values())
        return dict(rules)

    def all(self) -> dict[str, IntroRule]:
        with self._lock:
            try:
                return self._load()
            except OSError as exc:
                logger.warning("Could not read intro rules file %s: %s", self.path, exc)
                return dict(self._rules)

    def snapshot(self, *, rng: random.Random | None = None) -> Self:
        """An independent, read-only choice state for a preview."""
        copy_rng = rng is None
        rng = rng or random.Random()
        clone = type(self)(self.path, self._day_at, rng, self.library, self.paths)
        with self._lock:
            if copy_rng:
                clone._rng.setstate(self._rng.getstate())
            clone._frozen = True
            clone._aired = dict(self._aired)
            clone._reserved = dict(self._reserved)
            clone._heard = deque(self._heard)
            clone._released = deque(self._released)
            clone._unwritten = False
            has_cache = self._stamp is not None or self._seen or self._rules or self._skipped
            if has_cache:
                clone._stamp = self._stamp
                clone._seen = self._seen
                clone._rules = {
                    key: rule.model_copy(deep=True) for key, rule in self._rules.items()
                }
                clone._skipped = dict(self._skipped)
                clone._compiled = {key: list(tests) for key, tests in self._compiled.items()}
                clone._has_folders = self._has_folders
                clone._warmed_at = self._warmed_at
                clone._liner_snapshot = self._liner_snapshot
                clone._folder_pools = dict(self._folder_pools)
        if not has_cache:
            clone._snapshot_loading = True
            with clone._lock, suppress(OSError):
                clone._load()
            clone._snapshot_loading = False
        return clone

    def priorities(self) -> set[str]:
        return {rule.priority for rule in self.all().values()}

    def problem(self) -> str | None:
        with self._lock:
            return (
                f"{len(self._skipped)} invalid intro rules retained in {self.path.name}"
                if self._skipped
                else None
            )

    def _write(self, rules: dict[str, IntroRule]) -> None:
        if self._frozen:
            raise RuntimeError("cannot save an intro-rules snapshot")
        entries = {
            key: value.model_dump(mode="json", exclude_none=True) for key, value in rules.items()
        }
        entries.update(self._skipped)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            self.path, json.dumps({"schema_version": 1, "rules": entries}, indent=2) + "\n"
        )
        self._stamp = self._stat()
        self._seen = True
        self._rules = rules
        self._compiled = {
            key: [
                condition_matcher(c) if c["field"] not in {"folder", "category"} else None
                for c in rule.conditions
            ]
            for key, rule in rules.items()
        }
        self._has_folders = any(rule.liner_folder for rule in rules.values())

    def save(self, key: str, rule: IntroRule, *, create: bool) -> None:
        with self._lock:
            if not self._seen and not self.path.parent.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
            current = self._load()
            if (create and key in current) or (not create and key not in current):
                raise ValueError(
                    "intro rule already exists" if create else "intro rule does not exist"
                )
            if create and len(current) + len(self._skipped) >= MAX_RULES:
                raise ValueError(f"at most {MAX_RULES} intro rules per station")
            current[key] = rule
            self._write(current)
        if rule.liner_folder:
            self._warmed_at = float("-inf")  # the next tick builds its pool

    def remove(self, key: str) -> bool:
        with self._lock:
            current = self._load()
            if key not in current:
                return False
            del current[key]
            self._write(current)
            self._reserved.pop(key, None)
            return True

    def _drain(self) -> None:
        while self._heard:
            key, at = self._heard.popleft()
            self._reserved.pop(key, None)
            if key in self._rules:
                self._aired[key] = max(at, self._aired.get(key, at))
                self._unwritten = True
        while self._released:
            self._reserved.pop(self._released.popleft(), None)

    def heard(self, key: str) -> None:
        self._heard.append((key, time.time()))

    def release_later(self, key: str) -> None:
        self._released.append(key)

    def release(self, key: str) -> None:
        with self._lock:
            self._drain()
            self._reserved.pop(key, None)

    def reserve_chosen(self, key: str, at: float) -> bool:
        """Reserve a frozen rule choice without drawing a different clip."""
        with self._lock:
            self._drain()
            rules = self._load()
            rule = rules.get(key)
            if rule is None or rule.dates.refusal(self._day_at(at)):
                return False
            last = max(
                (
                    value
                    for value in (rule.last_aired, self._aired.get(key), self._reserved.get(key))
                    if value is not None
                ),
                default=None,
            )
            if (
                last is not None
                and rule.cooldown_minutes
                and abs(at - last) < rule.cooldown_minutes * 60
            ):
                return False
            self._reserved[key] = at
            return True

    def needs_upkeep(self) -> bool:
        return bool(self._heard or self._released or self._unwritten) or self.needs_warming()

    def upkeep(self) -> None:
        """Write when rules aired and warm folder pools (the station's tick, off the render path)."""
        if self._frozen:
            return
        with self._lock:
            self._drain()
            if self._unwritten:
                try:
                    current = self._load()
                    changed = False
                    for key, at in self._aired.items():
                        if key in current and (current[key].last_aired or 0) < at:
                            current[key] = current[key].model_copy(update={"last_aired": at})
                            changed = True
                    if changed:
                        self._write(current)
                    self._unwritten = False
                except OSError as exc:
                    logger.warning("Could not save intro rule airtimes: %s", exc)
        if self.needs_warming():
            self.warm()

    def _snapshot(self) -> tuple[dict[str, IntroRule], dict[str, list[Any]]]:
        """The rules and their compiled tests, read together under the lock."""
        with self._lock:
            try:
                self._load()
            except OSError as exc:
                logger.warning("Could not read intro rules file %s: %s", self.path, exc)
            return dict(self._rules), dict(self._compiled)

    @staticmethod
    def _pool(folder: str, liners: tuple[Any, ...]) -> tuple[Any, ...]:
        """The registered liners under *folder* (as clockwheel's ``_under`` judges)."""
        root = _normalized(folder)
        separator = "/" if root.startswith("/") and not root.startswith("//") else "\\"
        prefix = root.rstrip(separator) + separator
        return tuple(
            liner
            for liner in liners
            if (path := _normalized(liner.path)) == root or path.startswith(prefix)
        )

    def needs_warming(self) -> bool:
        """Whether folder-rule clip pools are due a check (asked on the station's tick)."""
        return self._has_folders and time.monotonic() - self._warmed_at >= WARM_SECONDS

    def warm(self) -> None:
        """Build folder-rule clip pools and read Beets fields off the render path.

        The station's tick calls this through :meth:`upkeep`, and a save
        calls it, so a render normally finds every pool ready.
        """
        library = self.library() if self.library is not None else None
        self._warmed_at = time.monotonic()
        if library is None:
            return
        rules, _compiled = self._snapshot()
        folders = {rule.liner_folder for rule in rules.values() if rule.liner_folder}
        fields = {
            c["name"] for rule in rules.values() for c in rule.conditions if c["field"] == "beets"
        }
        if fields and getattr(library, "clock_beets", None) is not None:
            library.clock_beets.snapshot(fields)
        if not folders:
            return
        liners = library.entries_snapshot("liner")
        with self._lock:
            known = self._folder_pools if liners is self._liner_snapshot else {}
            missing = folders - set(known)
        if not missing:
            return
        # Built with no lock held, so a render never waits for a large folder.
        built = {folder: self._pool(folder, liners) for folder in missing}
        with self._lock:
            if liners is not self._liner_snapshot:
                self._folder_pools = {}
                self._liner_snapshot = liners
            self._folder_pools.update(built)

    def _pattern_pool(self, pattern: str, entry: Any, pool: tuple[Any, ...]) -> tuple[Any, ...]:
        expanded, matcher, _missing = resolve_filename_pattern(pattern, entry)
        if matcher is None or expanded is None:
            return ()
        key = (pattern, matcher.pattern)
        cached = self._pattern_pools.get(key)
        if cached is not None and cached[0] is pool:
            return cached[1]
        matches = tuple(clip for clip in pool if matcher.fullmatch(Path(clip.path).name))
        # Repeated years reuse the filtered pool without retaining every artist/title forever.
        if len(self._pattern_pools) >= 64:
            self._pattern_pools.pop(next(iter(self._pattern_pools)))
        self._pattern_pools[key] = pool, matches
        return matches

    def _draw(self, count: int) -> Iterator[int]:
        """Distinct random indices below *count*: every one for a few clips.

        A large folder is sampled without copying it, and at most
        :data:`MAX_TRIES` of its clips are looked at for one song change,
        so a folder of unplayable files cannot hold up the render.
        """
        if count <= MAX_TRIES:
            order = list(range(count))
            while order:
                index = self._rng.randrange(len(order))
                order[index], order[-1] = order[-1], order[index]
                yield order.pop()
            return
        tried: set[int] = set()
        while len(tried) < MAX_TRIES:
            index = self._rng.randrange(count)
            if index not in tried:
                tried.add(index)
                yield index

    def choose(
        self,
        entry: Any,
        at: float,
        library: Any,
        priority: str = "after_artist",
        allowed: Callable[[Sequence[Any], float], Sequence[Any]] | None = None,
    ) -> Chosen | None:
        """A clip from the first ready rule of *priority* that *entry* matches, or ``None``.

        Runs on the render-ahead thread.  *allowed* is the station's liner
        filter (the liners it owns, and their play dates); each clip tried
        must pass it at *at*.
        """
        every, compiled = self._snapshot()
        rules = {key: rule for key, rule in every.items() if rule.priority == priority}
        if not rules:
            return None
        conditions = [c for rule in rules.values() for c in rule.conditions]
        fields = {c["name"] for c in conditions if c["field"] == "beets"}
        beets: Mapping[str, Any] | None = None
        if fields and getattr(library, "clock_beets", None) is not None:
            _known, items, error = library.clock_beets.snapshot(fields)
            if error is None:
                beets = items.get(normalize_path(entry.path))
        categories = None
        if any(c["field"] == "category" for c in conditions):
            if self.paths is None:
                index = getattr(library, "categories", None)
            else:
                categories_for = getattr(library, "categories_for", None)
                index = categories_for(self.paths) if categories_for is not None else None
            if index is not None:
                try:
                    categories = index.members()
                except Exception:
                    logger.exception("Could not read categories for intro rules")
        sources: dict[str, Any] = {}
        if any(c["field"] == "folder" for c in conditions):
            sources = {source["id"]: source for source in library.catalog.sources()}

        def matches(key: str, rule: IntroRule) -> bool:
            tests = compiled.get(key, ())
            if len(tests) != len(rule.conditions):
                return False  # compiled for another version of the rule
            hits = []
            for condition, test in zip(rule.conditions, tests, strict=True):
                if test is not None:
                    hits.append(test(entry, beets))
                    continue
                try:
                    resolved = (
                        resolve_conditions(
                            {"type": "music", "conditions": [condition]}, sources, 1
                        )[0]
                        if condition["field"] == "folder"
                        else condition
                    )
                    slot = ClockSlot(conditions=(resolved,))
                    hits.append(entry.path in condition_paths(slot, (entry,), None, categories))
                except ValueError:
                    hits.append(False)
            return all(hits) if rule.match == "all" else any(hits)

        day = self._day_at(at)
        rule_folders = any(rule.liner_folder for rule in rules.values())
        liners = library.entries_snapshot("liner") if rule_folders else ()
        present: dict[str, bool] = {}

        def playable(clip: Any, stale: bool) -> bool:
            if clip.path not in present:
                ok = Path(clip.path).is_file()
                if ok and stale:
                    # A pool from an older liner snapshot: still registered?
                    ok = library.entry_for_path(clip.path) is not None
                if ok and allowed is not None:
                    ok = bool(allowed((clip,), at))
                present[clip.path] = ok
            return present[clip.path]

        with self._lock:
            self._drain()
            ready = []
            for key, rule in rules.items():
                if rule.dates.refusal(day):
                    continue
                last = max(
                    (
                        v
                        for v in (rule.last_aired, self._aired.get(key), self._reserved.get(key))
                        if v is not None
                    ),
                    default=None,
                )
                if (
                    last is not None
                    and rule.cooldown_minutes
                    and abs(at - last) < rule.cooldown_minutes * 60
                ):
                    continue
                if matches(key, rule):
                    ready.append(
                        (last if last is not None else float("-inf"), self._rng.random(), key, rule)
                    )
            stale = bool(rule_folders) and liners is not self._liner_snapshot
            for _last, _tie, key, rule in sorted(ready):
                clips: list[Any] = []
                for clip_id in rule.clip_ids:
                    record = library.record_for_id(clip_id)
                    if record is not None and record.get("kind") == "liner":
                        liner = library.entry_for_path(record["path"])
                        if liner is not None:
                            clips.append(liner)
                pool: tuple[Any, ...] = ()
                if rule.liner_folder:
                    found = self._folder_pools.get(rule.liner_folder)
                    if found is not None:
                        pool = found
                    else:
                        # Only before the tick has warmed it: a render
                        # straight after start-up, say.
                        if stale:
                            self._folder_pools = {}
                            self._liner_snapshot = liners
                            stale = False
                        pool = self._pool(rule.liner_folder, liners)
                        self._folder_pools[rule.liner_folder] = pool
                if rule.filename_pattern:
                    pool = self._pattern_pool(rule.filename_pattern, entry, pool)
                for index in self._draw(len(clips) + len(pool)):
                    if index < len(clips):
                        clip, old = clips[index], False
                    else:
                        clip, old = pool[index - len(clips)], stale
                    if playable(clip, old):
                        if self._rng.random() * 100 >= rule.chance:
                            return None
                        self._reserved[key] = at
                        return Chosen(
                            f"rule:{key}", Path(clip.path), rule.label or "matched intro rule"
                        )
            return None
