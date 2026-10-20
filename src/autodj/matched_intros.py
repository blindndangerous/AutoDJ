"""Matched intros: a voice clip tied to one song, or to an artist, that airs as that song starts (#127).

An operator records something like "here's the new one from Example Artist"
and attaches it in Track info, either to that song or to every song by the
song's artist.  When the song comes up, the clip is mixed into its song change:
detected speech cues use liner placement across the outgoing outro and
incoming intro, delaying the song when needed to finish before its vocals.
Only saved-point playback mixes them, as it does announcer breaks.

Each station keeps its own intros: ``matched_intros.json`` in its state
folder, by intro ID, and the audio in its ``intros`` folder as
``intro-<id><ext>``.  A song intro is keyed by the song's library ID, so a
tag edit or a move the scan recognises keeps it; an artist intro by the
artist's name.  The file is read again whenever it changes on disk.  One
intro in it that is not valid is skipped, logged and said in Track info
(:meth:`MatchedIntros.problem`), and written back as it was with every save,
so the others still play and nothing is lost.  A file that is not a file of
intros at all is set aside as ``matched_intros.json.bad-<id>``; one that
cannot be read just now is left as it is, the intros last read are used and
saves are refused, so nothing is ever written over it.

Choosing one (:meth:`MatchedIntros.choose`, on the render-ahead thread) tries
the song's own intros first, then its artist's.  An intro outside its play
dates (judged by the station's day when it airs), or one that aired, or is
about to, within its wait, is passed over; of the rest, the one heard longest
ago is taken when its chance comes up.  The chance is rolled on the store's
own random source, never the picker's, so the rotation tester and the hour
preview pick the same songs as the station.  The mix bus reports an intro on
the air with :meth:`MatchedIntros.heard`, which never waits; the station's
tick writes when it aired (:meth:`MatchedIntros.upkeep`).
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import date as Date
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from autodj.clip_dates import ClipDates, ClipDatesError, parse_dates
from autodj.fsutil import atomic_write, file_signature, set_aside
from autodj.liners import LINER_EXTS
from autodj.repeat_rules import ArtistSplitter, artist_key

logger = logging.getLogger(__name__)

SCHEMA_VERSION: Literal[1] = 1
INTROS_FILE = "matched_intros.json"
#: The most intros one station keeps.
MAX_INTROS = 2000
#: The longest intro, in seconds: past this an upload is refused.
MAX_INTRO_SECONDS = 60.0
#: The longest label, in characters.
MAX_LABEL = 120
#: The longest wait between plays, in minutes (a week).
MAX_COOLDOWN_MINUTES = 7 * 24 * 60
#: The wait between plays an upload gets when it names none.
DEFAULT_COOLDOWN_MINUTES = 60
#: An intro's ID, and the ID of the song it is for.
ID_PATTERN = r"^[0-9a-f]{32}$"
TRACK_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,64}$"
_EXTENSIONS = "|".join(ext.lstrip(".") for ext in LINER_EXTS)
_FILE = re.compile(rf"intro-([0-9a-f]{{32}})\.({_EXTENSIONS})")


class IntroError(ValueError):
    """An intro that cannot be saved, naming the field at fault (``""`` for none)."""

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(message)
        self.field = field_name
        self.message = message


def file_name(intro_id: str, suffix: str) -> str:
    """The audio file name for an intro: ``intro-<id><suffix>``."""
    return f"intro-{intro_id}{suffix.lower()}"


class Intro(BaseModel):
    """One matched intro as ``matched_intros.json`` keeps it.

    Exactly one of *track_id* (a library ID) and *artist* is set.
    """

    model_config = ConfigDict(extra="forbid")

    file: str
    track_id: str | None = Field(default=None, pattern=TRACK_ID_PATTERN)
    artist: str | None = Field(default=None, min_length=1, max_length=500)
    label: str = Field(default="", max_length=MAX_LABEL)
    chance: int = Field(default=100, ge=1, le=100)
    cooldown_minutes: int = Field(default=DEFAULT_COOLDOWN_MINUTES, ge=0, le=MAX_COOLDOWN_MINUTES)
    not_before: Date | None = None
    not_after: Date | None = None
    speech_s: float = Field(default=0.0, ge=0.0, le=MAX_INTRO_SECONDS + 1.0)
    created_at: float = 0.0
    last_aired: float | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not _FILE.fullmatch(self.file):
            raise ValueError("file must be intro-<id> with an audio extension")
        if (self.track_id is None) == (self.artist is None):
            raise ValueError("an intro is for one song or one artist")
        if self.artist is not None and not artist_key(self.artist):
            raise ValueError("artist must have a letter or digit")
        self.dates  # noqa: B018 -- raises ClipDatesError for dates out of order
        return self

    @property
    def dates(self) -> ClipDates:
        """Its play dates."""
        return ClipDates(not_before=self.not_before, not_after=self.not_after)

    @property
    def applies_to(self) -> str:
        """``"song"`` or ``"artist"``."""
        return "song" if self.track_id is not None else "artist"


class _IntrosFile(BaseModel):
    """The file's frame; each intro in it is checked on its own (:func:`_entry`)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    intros: dict[str, Any] = Field(default_factory=dict)


def _entry(intro_id: str, raw: Any) -> Intro:
    """One intro of the file, checked.

    Raises:
        ValueError: It is not a valid intro, or does not name its own file.
    """
    if not re.fullmatch(ID_PATTERN.strip("^$"), intro_id):
        raise ValueError("its ID is not 32 hexadecimal digits")
    intro = Intro.model_validate(raw)
    found = _FILE.fullmatch(intro.file)
    if found is None or found.group(1) != intro_id:
        raise ValueError("it does not name its own file")
    return intro


def parse_rules(values: dict[str, Any]) -> dict[str, Any]:
    """Check an upload's label, chance, wait and dates; the values to save.

    Empty text for an optional field means "not set".

    Raises:
        IntroError: A value is not valid; names its field.
    """

    class _Rules(BaseModel):
        model_config = ConfigDict(extra="forbid")
        label: str = Field(default="", max_length=MAX_LABEL)
        chance: int = Field(default=100, ge=1, le=100)
        cooldown_minutes: int = Field(
            default=DEFAULT_COOLDOWN_MINUTES, ge=0, le=MAX_COOLDOWN_MINUTES
        )
        not_before: Date | None = None
        not_after: Date | None = None

    labels = {
        "label": "Label",
        "chance": "Chance",
        "cooldown_minutes": "Wait between plays",
        "not_before": "Not before",
        "not_after": "Do not play after",
    }
    cleaned = {key: value for key, value in values.items() if value != ""}
    try:
        rules = _Rules.model_validate(cleaned)
    except ValidationError as exc:
        error = exc.errors()[0]
        location = [str(part) for part in error.get("loc", ())]
        name = location[0] if location else ""
        if error.get("type") == "extra_forbidden":
            raise IntroError(name, f"Unknown field {name}") from None
        raise IntroError(name, f"{labels.get(name, name)}: {error['msg']}") from None
    label = " ".join("".join(ch if ch.isprintable() else " " for ch in rules.label).split())
    try:
        parse_dates({"not_before": rules.not_before, "not_after": rules.not_after})
    except ClipDatesError as exc:
        raise IntroError(exc.field, exc.message) from None
    return {
        "label": label,
        "chance": rules.chance,
        "cooldown_minutes": rules.cooldown_minutes,
        "not_before": rules.not_before,
        "not_after": rules.not_after,
    }


def wait_words(minutes: int) -> str:
    """ "1 hour 30 minutes": a wait in words."""
    hours, rest = divmod(max(0, int(minutes)), 60)
    days, hours = divmod(hours, 24)
    parts = [
        f"{count} {unit}{'' if count == 1 else 's'}"
        for count, unit in ((days, "day"), (hours, "hour"), (rest, "minute"))
        if count
    ]
    return " ".join(parts) or "0 minutes"


def rules_text(intro: Intro) -> str:
    """How often an intro plays, in words, for the page."""
    comes_up = (
        "this song comes up" if intro.artist is None else f"a song by {intro.artist} comes up"
    )
    chance = (
        f"Plays every time {comes_up}"
        if intro.chance == 100
        else f"Plays about {intro.chance}% of the times {comes_up}"
    )
    if intro.cooldown_minutes == 0:
        return f"{chance}, with no wait between plays."
    return f"{chance}, at most once every {wait_words(intro.cooldown_minutes)}."


@dataclass(frozen=True)
class Chosen:
    """An intro chosen for a song change.

    Attributes:
        intro_id: Its ID.
        path: Its audio file.
        label: Its label, or what it is for, for logs and the hour preview.
    """

    intro_id: str
    path: Path
    label: str


class MatchedIntros:
    """One station's matched intros: the file, the audio folder and what has aired.

    Args:
        path: The station's ``matched_intros.json``.
        folder: The station's ``intros`` folder.
        day_at: The station's calendar day at a Unix time, for play dates.
        rng: The random source for the chance; a new one by default.
    """

    def __init__(
        self,
        path: Path,
        folder: Path,
        day_at: Callable[[float], Date] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.path = Path(path)
        self.folder = Path(folder)
        self._day_at = day_at or (lambda at: Date.fromtimestamp(at))
        # Its own random source: the picker's must not move (see the module notes).
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._frozen = False
        self._snapshot_loading = False
        self._stamp: tuple[int, int, int] | None = None
        self._intros: dict[str, Intro] = {}
        # Entries of the file that are not valid intros, kept as they are: they
        # are skipped, said on the page, and written back with every save.
        self._skipped: dict[str, Any] = {}
        # Whether this store has ever seen its file (see clip_dates.LinerDates).
        self._seen = False
        # When each intro aired, as heard, and when a render plans to air it.
        self._aired: dict[str, float] = {}
        self._reserved: dict[str, float] = {}
        # Aired times not written to the file yet.
        self._unwritten = False
        # Reports from the mix bus, taken in under the lock by the next call.
        self._heard: deque[tuple[str, float]] = deque()
        self._released: deque[str] = deque()
        # Audio files a delete could not remove yet (one open for a decode).
        self._doomed: set[Path] = set()
        self._splitter = ArtistSplitter()
        self.rules: Any = None

    # ------------------------------------------------------------------
    # The file
    # ------------------------------------------------------------------

    def _stat(self) -> tuple[int, int, int] | None:
        """The file's time, size and ID; ``None`` when there is no file.

        Raises:
            OSError: The file is there but cannot be looked at, or its
                folder is away.
        """
        return file_signature(self.path)

    def _keep_bad_copy(self, reason: str) -> None:
        set_aside(self.path, "matched intros file", reason)

    def _load_locked(self) -> dict[str, Intro]:
        """Every intro, read again if the file changed.  Caller holds ``_lock``.

        Raises:
            OSError: The file could not be looked at or read.
        """
        if self._frozen and not self._snapshot_loading:
            return dict(self._intros)
        stamp = self._stat()
        if stamp is not None:
            self._seen = True
        if stamp == self._stamp:
            return dict(self._intros)
        intros: dict[str, Intro] = {}
        skipped: dict[str, Any] = {}
        if stamp is not None:
            data = self.path.read_bytes()
            try:
                document = _IntrosFile.model_validate_json(data)
            except ValueError as exc:
                # Not a file of intros at all: set aside whole.
                if not self._frozen:
                    self._keep_bad_copy(str(exc).splitlines()[0])
                stamp = None
            else:
                # One bad intro never silences the others: it is skipped.
                for intro_id, raw in document.intros.items():
                    try:
                        intros[intro_id] = _entry(intro_id, raw)
                    except ValueError as exc:
                        skipped[intro_id] = raw
                        reason = str(exc).splitlines()[0]
                        logger.error(
                            "Matched intros file %s: skipping intro %r, which is not valid (%s)",
                            self.path,
                            intro_id[:40],
                            reason,
                        )
        self._stamp, self._intros, self._skipped = stamp, intros, skipped
        return dict(intros)

    def problem(self) -> str | None:
        """What is wrong with the file, in words, or ``None``: the intros it skips."""
        with self._lock:
            skipped = sorted(self._skipped)
        if not skipped:
            return None
        count = len(skipped)
        names = ", ".join(intro_id[:40] for intro_id in skipped[:5])
        more = f" and {count - 5} more" if count > 5 else ""
        return (
            f"{count} matched intro{'' if count == 1 else 's'} in {self.path.name} "
            f"{'is' if count == 1 else 'are'} not valid and skipped until the file is "
            f"corrected: {names}{more}. AutoDJ keeps {'it' if count == 1 else 'them'} in the "
            "file as written."
        )

    def _write_locked(self, intros: dict[str, Intro]) -> None:
        """Write *intros*.  Caller holds ``_lock``.

        Raises:
            OSError: The file could not be written.
        """
        if self._frozen:
            raise RuntimeError("cannot save a matched-intros snapshot")
        # Skipped entries go back as they were read, never dropped.
        entries: dict[str, Any] = {
            intro_id: intro.model_dump(mode="json", exclude_none=True)
            for intro_id, intro in intros.items()
        }
        for intro_id, raw in self._skipped.items():
            entries.setdefault(intro_id, raw)
        document = {"schema_version": SCHEMA_VERSION, "intros": entries}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        self._seen = True
        try:
            stamp = self._stat()
        except OSError:
            stamp = None
        self._stamp, self._intros = stamp, dict(intros)

    def _first_folder_locked(self) -> None:
        """Make the folder for a store that has never had a file.  Caller holds ``_lock``."""
        if not self._seen and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def all(self) -> dict[str, Intro]:
        """Every intro, read again if the file changed.

        When the file cannot be read just now, the intros last read are used
        and a warning logged; a save then refuses, so nothing is written over it.
        """
        with self._lock:
            try:
                return self._load_locked()
            except OSError as exc:
                logger.warning("Could not read the matched intros file %s: %s", self.path, exc)
                return dict(self._intros)

    def snapshot(self) -> Self:
        """An independent, read-only choice state for a preview."""
        rng = random.Random()
        clone = type(self)(self.path, self.folder, self._day_at, rng)
        with self._lock:
            clone._rng.setstate(self._rng.getstate())
            clone._frozen = True
            clone._aired = dict(self._aired)
            clone._reserved = dict(self._reserved)
            clone._unwritten = False
            clone._heard = deque(self._heard)
            clone._released = deque(self._released)
            has_cache = self._stamp is not None or self._seen or self._intros or self._skipped
            if has_cache:
                clone._stamp = self._stamp
                clone._intros = {
                    key: intro.model_copy(deep=True) for key, intro in self._intros.items()
                }
                clone._skipped = dict(self._skipped)
                clone._seen = self._seen
        if not has_cache:
            clone._snapshot_loading = True
            with clone._lock, suppress(OSError):
                clone._load_locked()
            clone._snapshot_loading = False
        if self.rules is not None:
            clone.rules = self.rules.snapshot(rng=clone._rng)
        return clone

    def add(self, intro_id: str, intro: Intro) -> None:
        """Save a new intro.

        Raises:
            IntroError: The station has :data:`MAX_INTROS` already.
            OSError: The file could not be read or written; nothing is saved.
        """
        with self._lock:
            self._first_folder_locked()
            current = self._load_locked()
            if intro_id not in current and len(current) + len(self._skipped) >= MAX_INTROS:
                raise IntroError("", f"A station can keep at most {MAX_INTROS} matched intros")
            current[intro_id] = intro
            self._write_locked(current)

    def remove(self, intro_id: str) -> Intro | None:
        """Forget one intro; it, or ``None`` when there is none by that ID.

        The audio file stays: the caller deletes it (:meth:`delete_file`).

        Raises:
            OSError: The file could not be read or written; nothing changes.
        """
        with self._lock:
            current = self._load_locked()
            removed = current.pop(intro_id, None)
            if removed is None:
                return None
            self._write_locked(current)
            self._aired.pop(intro_id, None)
            self._reserved.pop(intro_id, None)
            return removed

    def forget(self, intro_ids: Iterable[str]) -> list[str]:
        """Forget *intro_ids* in one write and delete their audio; the IDs that were there.

        Raises:
            OSError: The file could not be read or written; nothing changes.
        """
        with self._lock:
            current = self._load_locked()
            gone = sorted({intro_id for intro_id in intro_ids if intro_id in current})
            if not gone:
                return []
            removed = [current.pop(intro_id) for intro_id in gone]
            self._write_locked(current)
            for intro_id in gone:
                self._aired.pop(intro_id, None)
                self._reserved.pop(intro_id, None)
        for intro in removed:
            self.delete_file(intro)
        return gone

    def delete_file(self, intro: Intro) -> None:
        """Delete an intro's audio; one that cannot go now is tried again at upkeep."""
        path = self.folder / intro.file
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not delete %s yet; will try again", path.name, exc_info=True)
            with self._lock:
                self._doomed.add(path)

    # ------------------------------------------------------------------
    # Choosing and airing
    # ------------------------------------------------------------------

    def _apply_heard_locked(self) -> None:
        """Take in what the mix bus reported.  Caller holds ``_lock``."""
        while self._heard:
            intro_id, at = self._heard.popleft()
            self._reserved.pop(intro_id, None)
            if intro_id in self._intros:
                self._aired[intro_id] = max(at, self._aired.get(intro_id, at))
                self._unwritten = True
        while self._released:
            self._reserved.pop(self._released.popleft(), None)

    def _last_locked(self, intro_id: str, intro: Intro) -> float | None:
        """When *intro* last aired, or is planned to.  Caller holds ``_lock``."""
        times = [
            moment
            for moment in (
                intro.last_aired,
                self._aired.get(intro_id),
                self._reserved.get(intro_id),
            )
            if moment is not None
        ]
        return max(times) if times else None

    def _artist_keys(self, artist: str) -> frozenset[str]:
        return self._splitter.split(artist) if artist else frozenset()

    def candidates(self, track_id: str, artist: str) -> tuple[list[str], list[str]]:
        """The IDs of the intros for song *track_id*, and for its *artist*."""
        intros = self.all()
        keys = self._artist_keys(artist)
        song = [key for key, intro in intros.items() if track_id and intro.track_id == track_id]
        by_artist = [
            key
            for key, intro in intros.items()
            if intro.artist is not None and artist_key(intro.artist) in keys
        ]
        return song, by_artist

    def choose(
        self,
        track_id: str,
        artist: str,
        at: float,
        group: Literal["both", "song", "artist"] = "both",
    ) -> Chosen | None:
        """An intro for the song *track_id* by *artist*, airing at Unix time *at*, or ``None``.

        The song's own intros come first, then its artist's, unless *group*
        limits selection to one tier. The one chosen is held for this song change, so another
        render does not choose it within its wait; :meth:`release` lets it go.
        """
        song, by_artist = self.candidates(track_id, artist)
        if group == "song":
            by_artist = []
        elif group == "artist":
            song = []
        if not song and not by_artist:
            return None
        day = self._day_at(at)
        with self._lock:
            intros = dict(self._intros)
        # Only clips whose file is there; looked at with no lock held.
        present = {
            intro_id
            for intro_id in (*song, *by_artist)
            if intro_id in intros and (self.folder / intros[intro_id].file).is_file()
        }
        with self._lock:
            self._apply_heard_locked()
            for tier in (song, by_artist):
                ready: list[tuple[float, float, str, Intro]] = []
                for intro_id in tier:
                    intro = intros.get(intro_id)
                    if intro is None or intro_id not in present:
                        continue
                    if intro.dates.refusal(day) is not None:
                        continue
                    last = self._last_locked(intro_id, intro)
                    wait = intro.cooldown_minutes * 60.0
                    if last is not None and wait > 0 and abs(at - last) < wait:
                        continue
                    ready.append(
                        (
                            last if last is not None else float("-inf"),
                            self._rng.random(),
                            intro_id,
                            intro,
                        )
                    )
                if not ready:
                    continue
                _last, _tie, intro_id, intro = min(ready)
                if self._rng.random() * 100.0 >= intro.chance:
                    continue
                self._reserved[intro_id] = at
                label = intro.label or (
                    f"for {intro.artist}" if intro.artist is not None else "for this song"
                )
                return Chosen(intro_id, self.folder / intro.file, label)
        return None

    def release(self, intro_ids: Iterable[str]) -> None:
        """Let go of intros held for a song change that will not air."""
        intro_ids = tuple(intro_ids)
        if self.rules is not None:
            for intro_id in intro_ids:
                if intro_id.startswith("rule:"):
                    self.rules.release(intro_id[5:])
        with self._lock:
            self._apply_heard_locked()
            for intro_id in intro_ids:
                self._reserved.pop(intro_id, None)

    def reserve_chosen(self, intro_id: str, at: float) -> bool:
        """Reserve a frozen intro ID without rerunning its random choice."""
        intros = self.all()
        intro = intros.get(intro_id)
        if intro is None or not (self.folder / intro.file).is_file():
            return False
        day = self._day_at(at)
        if intro.dates.refusal(day) is not None:
            return False
        with self._lock:
            self._apply_heard_locked()
            last = self._last_locked(intro_id, intro)
            wait = intro.cooldown_minutes * 60.0
            if last is not None and wait > 0 and abs(at - last) < wait:
                return False
            self._reserved[intro_id] = at
        return True

    def heard(self, intro_id: str) -> None:
        """The mix bus reports *intro_id* on the air.  Never waits."""
        if intro_id.startswith("rule:") and self.rules is not None:
            self.rules.heard(intro_id[5:])
            return
        self._heard.append((intro_id, time.time()))

    def release_later(self, intro_ids: Iterable[str]) -> None:
        """Queue reservations the mix bus left without waiting for the store lock."""
        for intro_id in intro_ids:
            if intro_id.startswith("rule:") and self.rules is not None:
                self.rules.release_later(intro_id[5:])
            else:
                self._released.append(intro_id)

    def needs_upkeep(self) -> bool:
        """Whether :meth:`upkeep` has anything to do."""
        return bool(self._heard or self._released or self._unwritten or self._doomed) or (
            self.rules is not None and self.rules.needs_upkeep()
        )

    def upkeep(self) -> None:
        """Write when intros aired and delete audio a delete had to leave (the station's tick).

        A file that cannot be read or written just now is left for the next
        tick; the aired times stay in memory meanwhile.
        """
        if self._frozen:
            return
        with self._lock:
            self._apply_heard_locked()
            doomed, self._doomed = self._doomed, set()
            if self._unwritten:
                try:
                    current = self._load_locked()
                    changed = False
                    for intro_id, moment in self._aired.items():
                        intro = current.get(intro_id)
                        if intro is not None and (intro.last_aired or 0.0) < moment:
                            current[intro_id] = intro.model_copy(update={"last_aired": moment})
                            changed = True
                    if changed:
                        self._write_locked(current)
                    self._unwritten = False
                except OSError as exc:
                    logger.warning("Could not save when matched intros aired: %s", exc)
        for path in doomed:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                with self._lock:
                    self._doomed.add(path)
        if self.rules is not None:
            self.rules.upkeep()


__all__ = [
    "DEFAULT_COOLDOWN_MINUTES",
    "INTROS_FILE",
    "MAX_COOLDOWN_MINUTES",
    "MAX_INTROS",
    "MAX_INTRO_SECONDS",
    "Chosen",
    "Intro",
    "IntroError",
    "MatchedIntros",
    "file_name",
    "parse_rules",
    "rules_text",
    "wait_words",
]
