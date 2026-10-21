"""DJ breaks, news and weather from an announcer program (#88).

An announcer program on the same host writes a spoken break for
one song change and hands it to AutoDJ through the breaks API
(:mod:`autodj.breaks_api`).  AutoDJ renders each song change one song
ahead, so a break cannot join the change that is already rendered: it
joins the *open slot*, the change after the next song.

The player plans that change as soon as the next song is rendered
(:meth:`autodj.player.Player._plan_slot`): it picks the song that will
follow, so the slot names both songs, the *ending* one and the *upcoming*
one.  A break booked for the slot pins that pair: the upcoming song stays
whatever the queue does, because the break names it.  When the slot's
change is rendered, its breaks are mixed in (talk-up over the upcoming
song's intro, or standalone between the songs) and they air about one
song later.

:class:`BreakDesk` keeps one station's breaks: their files in the
station's ``announcements`` folder (never in liner rotation), their state
(queued, rendered, on_air, aired, dropped, expired) and the reason a break
was dropped.  Its methods only take a short lock, so the mix-bus thread can
report a break on air; file work happens in :meth:`BreakDesk.maintain`,
which the station's tick runs off the event loop.
"""

from __future__ import annotations

import logging
import math
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from autodj.liners import LINER_EXTS
from autodj.stereo import SAMPLE_RATE

if TYPE_CHECKING:
    from autodj.config import BreaksConfig
    from autodj.indexer import IndexEntry

logger = logging.getLogger(__name__)

#: What a break is.  Only a DJ break may talk up a song; the rest play
#: between the songs.
KINDS: tuple[str, ...] = ("dj", "news", "weather", "music_news")
#: Where a break goes: ``auto`` follows ``[breaks] dj_placement``.
PLACEMENTS: tuple[str, ...] = ("auto", "talk_up", "standalone")
#: Break states, in the order a break normally passes through them.
QUEUED, RENDERED, ON_AIR, AIRED, DROPPED, EXPIRED = (
    "queued",
    "rendered",
    "on_air",
    "aired",
    "dropped",
    "expired",
)
TERMINAL = frozenset({AIRED, DROPPED, EXPIRED})
#: Where a break came from: an announcer program with a token, an
#: operator recording it in the browser's voice tracker (#128), or the
#: station's ad schedule (#51).
ANNOUNCER, VOICE_TRACK, AD_SCHEDULE = "announcer", "voice_track", "ads"
#: What the announcer and the voice tracker are told while breaks are off.
BREAKS_OFF = "Breaks are off for this station: turn them on in Settings, Breaks"
#: The kind of an ad spot booked by the ad schedule: it plays between the songs.
AD = "ad"
#: File types a break may be.
EXTENSIONS: tuple[str, ...] = LINER_EXTS
#: Finished breaks kept for status requests.
HISTORY = 200
#: Where a break's voice starts, at most this long before its first sound.
LEAD_SECONDS = 0.05
#: How much of the quiet after the last word a break keeps under the music.
TAIL_SECONDS = 0.4
#: A frame is quiet when it is this far below the loudest frame.
SILENCE_DB = -45.0
#: The shortest speech a break may hold.
MIN_SPEECH_SECONDS = 0.5
#: 20 ms frames measured at once by :func:`speech_cues` (about 10 s).
_LEVEL_BLOCK_FRAMES = 500

_ID = re.compile(r"[0-9a-f]{32}\Z")
_FILE = re.compile(r"brk-([0-9a-f]{32})\.[a-z0-9]{2,4}\Z")


def new_id() -> str:
    """A fresh break or slot id: 32 lower-case hex digits."""
    return secrets.token_hex(16)


def valid_id(value: object) -> bool:
    """Whether *value* looks like a break or slot id."""
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def file_name(break_id: str, suffix: str) -> str:
    """The announcements file name for a break: ``brk-<id><suffix>``."""
    return f"brk-{break_id}{suffix.lower()}"


@dataclass(frozen=True)
class BreakCues:
    """Where a break's voice is, in seconds of its own file.

    Attributes:
        start: The first audio to play, just before the first sound.
        segue: Where the speech ends; music may follow from here.
        end: The last audio kept, a little after *segue*.
        length: The whole file's length.
    """

    start: float
    segue: float
    end: float
    length: float

    @property
    def speech(self) -> float:
        """Seconds of speech, from *start* to *segue*."""
        return self.segue - self.start

    def points(self, talk_up: bool) -> dict[str, float | None]:
        """Saved points for the playout renderer.

        A talk-up break fits its speech before the upcoming song's vocal
        (``overlap_start`` 0); a standalone one lets the music in at its
        segue (``overlap_start`` unset).
        """
        return {
            "start": self.start,
            "segue": self.segue,
            "end": self.end,
            "overlap_start": 0.0 if talk_up else None,
        }


def speech_cues(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> BreakCues:
    """Find a break's speech: trim the silence before it and after it.

    Frames of 20 ms are quiet when they are :data:`SILENCE_DB` below the
    loudest frame.  Only the ends move: a pause inside the speech stays.

    Raises:
        ValueError: The audio is empty, silent, not finite, or holds less
            than :data:`MIN_SPEECH_SECONDS` of sound.
    """
    if audio.ndim != 2 or not len(audio):
        raise ValueError("the audio is empty or not finite")
    frame = max(1, round(sample_rate * 0.02))
    count = -(-len(audio) // frame)
    levels = np.empty(count, dtype=np.float64)
    # A few seconds at a time, so the work takes little more memory than
    # the audio itself even for the longest break allowed.
    step = _LEVEL_BLOCK_FRAMES
    for first_frame in range(0, count, step):
        block = audio[first_frame * frame : (first_frame + step) * frame]
        if not np.isfinite(block).all():
            raise ValueError("the audio is empty or not finite")
        frames = -(-len(block) // frame)
        padded = np.zeros((frames * frame, audio.shape[1]), dtype=np.float32)
        padded[: len(block)] = block
        squares = np.square(padded.reshape(frames, frame, audio.shape[1]), dtype=np.float64)
        levels[first_frame : first_frame + frames] = np.sqrt(squares.mean(axis=1)).max(axis=1)
    peak = float(levels.max())
    if peak < 1e-4:
        raise ValueError("the audio is silent")
    loud = np.flatnonzero(levels >= peak * 10 ** (SILENCE_DB / 20))
    first = int(loud[0]) * frame
    last = min(len(audio), (int(loud[-1]) + 1) * frame)
    length = len(audio) / sample_rate
    start = max(0.0, first / sample_rate - LEAD_SECONDS)
    segue = last / sample_rate
    if segue - first / sample_rate < MIN_SPEECH_SECONDS:
        raise ValueError(f"the audio holds less than {MIN_SPEECH_SECONDS:g} seconds of sound")
    end = min(length, segue + TAIL_SECONDS)
    return BreakCues(round(start, 4), round(segue, 4), round(end, 4), round(length, 4))


@dataclass
class Break:
    """One break and what has become of it.

    Attributes:
        break_id: Its id.
        kind: One of :data:`KINDS`.
        slot_id: The song change it was booked for.
        ending: The song it follows, as the announcer named it.
        upcoming: The song it leads into, as the announcer named it.
        placement: What the request asked for (:data:`PLACEMENTS`).
        allow_outro: Whether the voice may start over the ending song's outro.
        path: Its file in the announcements folder.
        size: Its file's size in bytes.
        cues: Where its speech is.
        label: A short description from the request, for logs and status.
        token_name: The name of the token that sent it.
        created_at: When it arrived (wall-clock seconds).
        expires_at: When it is too late to air (wall-clock seconds).
        state: Its state (see :data:`TERMINAL`).
        reason: Why it was dropped or expired, in words.
        mixed_as: ``"talk_up"`` or ``"standalone"`` once it is rendered.
        eta: When it was expected to air, once it is rendered.
        aired_at: When its voice started.
        updated_at: When its state last changed.
        source: :data:`ANNOUNCER`, :data:`VOICE_TRACK` or :data:`AD_SCHEDULE`.
        keep_file: The file is the station's own (an ad spot), not an upload:
            it is never deleted, and it takes no room in the upload caps.
        limit_s: The longest the file may be when it is decoded to air, or
            ``None`` for ``[breaks] max_seconds``.
    """

    break_id: str
    kind: str
    slot_id: str
    ending: str
    upcoming: str
    placement: str
    allow_outro: bool
    path: Path
    size: int
    cues: BreakCues
    label: str
    token_name: str
    created_at: float
    expires_at: float
    state: str = QUEUED
    reason: str = ""
    mixed_as: str = ""
    eta: float | None = None
    aired_at: float | None = None
    updated_at: float = 0.0
    source: str = ANNOUNCER
    keep_file: bool = False
    limit_s: float | None = None

    def view(self) -> dict[str, Any]:
        """What the API says about the break: never its file path or token."""
        return {
            "break_id": self.break_id,
            "kind": self.kind,
            "label": self.label,
            "state": self.state,
            "reason": self.reason or None,
            "slot_id": self.slot_id,
            "ending_id": self.ending,
            "upcoming_id": self.upcoming,
            "placement": self.placement,
            "mixed_as": self.mixed_as or None,
            "allow_outro": self.allow_outro,
            "speech_s": round(self.cues.speech, 3),
            "created_at": round(self.created_at, 3),
            "expires_at": round(self.expires_at, 3),
            "eta": None if self.eta is None else round(self.eta, 3),
            "aired_at": None if self.aired_at is None else round(self.aired_at, 3),
            "updated_at": round(self.updated_at, 3),
            "source": self.source,
        }


@dataclass(frozen=True)
class TransitionItem:
    """One frozen spoken item in an open song-change plan."""

    item_id: str
    kind: str
    source: str
    label: str
    entry: Any = None
    intro: Any = None
    committed: bool = False
    editable: bool = True

    def view(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "kind": self.kind,
            "source": self.source,
            "label": self.label,
            "committed": self.committed,
            "editable": self.editable,
        }


@dataclass
class SlotPlan:
    """The open song change: the player's pick for the song after the next one.

    Made on the render-ahead worker right after the next song (*before*)
    is rendered, and used when the worker renders *ending*.

    Attributes:
        slot_id: The change's id, which breaks are booked against.
        serial: The render cursor's serial when the plan was made; the plan
            holds only while the cursor has not moved.
        before: The rendered song that plays before *ending*.
        before_s: How long *before*'s render plays, in seconds.
        ending: The song the change leaves.
        upcoming: The song the change leads into.
        mode: How *upcoming* was picked (``"queue"``, ``"clock"``...).
        liners: The liners the pick put in the change, with whether each
            came from the queue.
        next_cursor: The clockwheel place after *upcoming*.
        boundary_before: The clock's next resync time before the pick.
        boundary_after: The same after the pick (it moves at a resync).
        queue_reserved: The queue pick *ending* holds, for staleness checks.
        reserved: Queue entries the pick passed over because earlier
            renders hold them.
        ending_offset_s: Seconds of *ending* already played when it starts.
        clock_name: The clockwheel *next_cursor* is a place in (#133).
        clock_step: The selected step snapshot for the Why display.
        ad_steps: Clockwheel ad steps passed before the selected song.
        pinned: A break is booked: keep the pair whatever the queue does.
        stale: A queue edit changed what the pick would be (unpinned only).
    """

    slot_id: str
    serial: int
    before: IndexEntry
    before_s: float
    ending: IndexEntry
    upcoming: IndexEntry
    mode: str
    liners: tuple[tuple[IndexEntry, bool], ...]
    next_cursor: int | None
    boundary_before: float | None
    boundary_after: float | None
    queue_reserved: IndexEntry | None
    reserved: tuple[IndexEntry, ...]
    ending_offset_s: float = 0.0
    clock_name: str | None = None
    clock_revision: int | None = None
    pinned: bool = False
    stale: bool = False
    clock_step: dict | None = None
    ad_steps: tuple[dict, ...] = ()
    queue_liner_paths: tuple[Path, ...] = ()
    clock_liner: Any = None
    transition_items: tuple[TransitionItem, ...] = ()
    transition_revision: str = ""
    transition_seed: str = ""
    transition_order: tuple[str, ...] = ()
    transition_replaced: tuple[str, ...] = ()
    transition_actions: tuple[tuple[str, str], ...] = ()


class BreakError(Exception):
    """A request the desk refuses: *status* and *detail* for the reply."""

    def __init__(self, status: int, detail: str, reason: str = "") -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.reason = reason


@dataclass
class _Counts:
    waiting: int = 0
    in_slot: int = 0
    waiting_bytes: int = 0


@dataclass
class _Reservation:
    """Room held for an upload that is still arriving or decoding."""

    slot_id: str
    size: int
    name: str


#: A rendered break that has not started this long after its expected air
#: time never will (its render was discarded): it expires.
RENDERED_GRACE_SECONDS = 600.0
#: A part file younger than this may still be arriving; the start-up sweep
#: leaves it.
PART_GRACE_SECONDS = 600.0
#: How long the desk waits before trying again to delete a file it could
#: not (still open in a decode, or held by another program).  It never
#: gives up: the station's tick keeps trying.
DELETE_RETRY_SECONDS = 30.0


class BreakDesk:
    """One station's breaks: booking, state, and their files.

    Every method takes only the desk's own short lock and logs after
    releasing it, except :meth:`maintain`, which deletes files and must run
    off the event loop and off the audio thread.  *on_slot_empty* (set by
    the player) hears, with no lock held, that a slot has no waiting
    breaks left, so the player can unpin it.
    """

    def __init__(
        self,
        config: Callable[[], BreaksConfig],
        folder: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Keep the breaks of the station whose ``[breaks]`` section *config* returns.

        Args:
            config: The station's breaks settings, read on every use.
            folder: The station's announcements folder.
            clock: Wall-clock time, for tests.
        """
        self._config = config
        self.folder = Path(folder)
        self._clock = clock
        self._lock = threading.Lock()
        self._breaks: OrderedDict[str, Break] = OrderedDict()
        self._reserved: dict[str, _Reservation] = {}
        # Files of finished breaks and abandoned uploads, deleted by maintain(),
        # with when to try each next (0: now, and it has not failed yet).
        self._doomed: dict[Path, float] = {}
        # Log lines decided under the lock and written after it.
        self._logs: list[tuple[int, str, tuple[Any, ...]]] = []
        self._swept = False
        self.on_slot_empty: Callable[[str], None] | None = None
        # Another feature that books breaks of its own (the ad schedule,
        # #51) says here whether the station must plan song changes for it.
        self.wants_slots: Callable[[], bool] | None = None

    @property
    def config(self) -> BreaksConfig:
        """The station's ``[breaks]`` settings."""
        return self._config()

    @property
    def enabled(self) -> bool:
        """Whether the station accepts external announcer uploads."""
        return self.config.enabled is True

    @property
    def plans_slots(self) -> bool:
        """Whether the station plans song changes for breaks: its own, or the ad schedule's."""
        if self.enabled:
            return True
        wants = self.wants_slots
        try:
            return wants is not None and bool(wants())
        except Exception:  # a broken hook must not stop the station picking
            logger.exception("Could not ask whether ad breaks need song changes planned")
            return False

    def _log(self, level: int, message: str, *args: Any) -> None:
        """Queue a log line (hold the lock); :meth:`_flush` writes it."""
        self._logs.append((level, message, args))

    def _flush(self, emptied: set[str] | None = None) -> None:
        """Write queued log lines and report emptied slots, with the lock released."""
        with self._lock:
            logs, self._logs = self._logs, []
            emptied = {
                slot
                for slot in emptied or ()
                if not any(
                    item.slot_id == slot and item.state == QUEUED for item in self._breaks.values()
                )
            }
        for level, message, args in logs:
            logger.log(level, message, *args)
        callback = self.on_slot_empty
        if callback is not None:
            for slot in emptied:
                callback(slot)

    # ------------------------------------------------------------------
    # Booking (API threads)
    # ------------------------------------------------------------------

    def _counts(self, slot_id: str) -> _Counts:
        counts = _Counts()
        for item in self._breaks.values():
            # The station's own files (ad spots) are not uploads waiting.
            if item.state in (QUEUED, RENDERED, ON_AIR) and not item.keep_file:
                counts.waiting += 1
                counts.waiting_bytes += item.size
                if item.slot_id == slot_id:
                    counts.in_slot += 1
        for held in self._reserved.values():
            counts.waiting += 1
            counts.waiting_bytes += held.size
            if held.slot_id == slot_id:
                counts.in_slot += 1
        return counts

    def _room_locked(self, slot_id: str, size: int) -> None:
        config = self.config
        counts = self._counts(slot_id)
        if counts.waiting >= config.max_queued:
            raise BreakError(
                429,
                f"This station already holds {config.max_queued} breaks waiting to air",
                "queue_full",
            )
        if counts.in_slot >= config.max_per_slot:
            raise BreakError(
                409,
                f"This song change already holds {config.max_per_slot} breaks",
                "slot_full",
            )
        if counts.waiting_bytes + size > config.quota_bytes:
            raise BreakError(
                507, "The waiting break files would pass this station's quota", "quota"
            )

    def reserve(self, break_id: str, slot_id: str, size: int, name: str) -> None:
        """Hold room for an upload before its body is read.

        *size* is the request's declared length (or the largest break the
        station takes).  :meth:`add` takes the place over; :meth:`release`
        gives it back.

        Raises:
            BreakError: 429 when the station already holds ``max_queued``
                breaks (uploads in progress included), 409 when the slot
                holds ``max_per_slot``, 507 when the files would pass the quota.
        """
        with self._lock:
            self._room_locked(slot_id, size)
            self._reserved[break_id] = _Reservation(slot_id, size, name)

    def retarget(self, break_id: str, slot_id: str, name: str) -> None:
        """Move an upload's held room to the slot its form named, under its file name.

        Raises:
            BreakError: As :meth:`reserve` (the slot is full); the room
                stays held for :meth:`release`.
        """
        with self._lock:
            held = self._reserved.pop(break_id)
            try:
                self._room_locked(slot_id, held.size)
            finally:
                self._reserved[break_id] = held
            held.slot_id = slot_id
            held.name = name

    def release(self, break_id: str, path: Path | None = None) -> None:
        """Give back an upload's room; its file, if any, is deleted later."""
        with self._lock:
            self._reserved.pop(break_id, None)
            if path is not None:
                self._doomed.setdefault(path, 0.0)

    def add(self, item: Break) -> None:
        """Keep a booked break.  Call with the player's slot lock held.

        Its reservation, if any, becomes the break.

        Raises:
            BreakError: As :meth:`reserve`; 409 for an announcer's break
                while external breaks are off. Checked under the lock
                :meth:`drop_waiting_breaks` takes, so an upload that was let in
                before breaks were turned off can never be booked after.
        """
        with self._lock:
            held = self._reserved.pop(item.break_id, None)
            try:
                if item.source == ANNOUNCER and not self.enabled:
                    raise BreakError(409, BREAKS_OFF, "disabled")
                if not item.keep_file:
                    self._room_locked(item.slot_id, item.size)
            except BreakError:
                if held is not None:
                    self._reserved[item.break_id] = held
                raise
            item.state = QUEUED
            item.updated_at = self._clock()
            self._breaks[item.break_id] = item
            self._log(
                logging.INFO,
                "Break %s (%s%s) booked for slot %s by %s; %.1f s of speech",
                item.break_id[:8],
                item.kind,
                f", {item.label}" if item.label else "",
                item.slot_id[:8],
                item.token_name,
                item.cues.speech,
            )
        self._flush()

    def get(self, break_id: str) -> Break | None:
        """A copy of the break with this id, or ``None``."""
        with self._lock:
            item = self._breaks.get(break_id)
            return replace(item) if item is not None else None

    def listing(self) -> list[Break]:
        """Copies of the breaks waiting to air, then the finished ones, newest first."""
        with self._lock:
            items = [replace(item) for item in self._breaks.values()]
        waiting = [item for item in items if item.state not in TERMINAL]
        done = [item for item in reversed(items) if item.state in TERMINAL]
        return waiting + done

    def cancel(
        self, break_id: str, reason: str = "cancelled by the announcer", source: str | None = None
    ) -> Break:
        """Drop a break that is still waiting for its song change, with *reason*.

        With *source*, only a break from that source is found.

        Raises:
            BreakError: 404 for no such break; 409 when it is already
                mixed into the stream, on the air or finished.
        """
        with self._lock:
            item = self._breaks.get(break_id)
            if item is None or (source is not None and item.source != source):
                raise BreakError(404, "No break has that id", "not_found")
            if item.state != QUEUED:
                raise BreakError(
                    409,
                    "That break is already "
                    + {
                        RENDERED: "mixed into the stream and will air",
                        ON_AIR: "on the air",
                    }.get(item.state, item.state.replace("_", " ")),
                    item.state,
                )
            self._finish_locked(item, DROPPED, reason)
            result = replace(item)
        self._flush({item.slot_id})
        return result

    def waiting_in(self, slot_id: str) -> int:
        """How many breaks are booked for *slot_id* and not yet rendered."""
        with self._lock:
            return sum(
                1
                for item in self._breaks.values()
                if item.slot_id == slot_id and item.state == QUEUED
            )

    # ------------------------------------------------------------------
    # The player (render worker and mix-bus threads)
    # ------------------------------------------------------------------

    def take(self, slot_id: str, airs_at: float) -> list[Break]:
        """The breaks booked for *slot_id*, now being rendered.

        A break whose song change would air after it expires is dropped
        here, as expired.  Standalone kinds come first and a DJ break last,
        so a talk-up ends on the upcoming song's vocal.
        """
        taken: list[Break] = []
        with self._lock:
            for item in self._breaks.values():
                if item.slot_id != slot_id or item.state != QUEUED:
                    continue
                if airs_at > item.expires_at:
                    self._finish_locked(item, EXPIRED, "its song change would air after it expires")
                    continue
                item.state = RENDERED
                item.eta = airs_at
                item.updated_at = self._clock()
                taken.append(item)
            taken.sort(key=lambda item: (item.kind == "dj", item.created_at))
            for item in taken:
                self._log(
                    logging.INFO,
                    "Break %s is mixed into the song change; it airs in about %d s",
                    item.break_id[:8],
                    max(0, round(airs_at - self._clock())),
                )
            copies = [replace(item) for item in taken]
        self._flush()
        return copies

    def mixed_as(self, break_id: str, placement: str) -> None:
        """Record how a rendered break was placed (talk_up or standalone)."""
        with self._lock:
            item = self._breaks.get(break_id)
            if item is not None:
                item.mixed_as = placement

    def drop(self, break_id: str, reason: str) -> None:
        """Drop one break that has not finished, with *reason*."""
        slots = set()
        with self._lock:
            item = self._breaks.get(break_id)
            if item is not None and item.state not in TERMINAL:
                self._finish_locked(item, DROPPED, reason)
                slots.add(item.slot_id)
        self._flush(slots)

    def drop_slot(self, slot_id: str, reason: str) -> int:
        """Drop the breaks still waiting for *slot_id*; returns how many."""
        with self._lock:
            items = [
                item
                for item in self._breaks.values()
                if item.slot_id == slot_id and item.state == QUEUED
            ]
            for item in items:
                self._finish_locked(item, DROPPED, reason)
        self._flush()
        return len(items)

    def drop_unaired(self, reason: str) -> int:
        """Drop every break that has not finished airing (the station jumped or stopped)."""
        with self._lock:
            items = [item for item in self._breaks.values() if item.state not in TERMINAL]
            for item in items:
                self._finish_locked(
                    item, DROPPED, f"cut short: {reason}" if item.state == ON_AIR else reason
                )
        self._flush()
        return len(items)

    def drop_waiting_breaks(self, reason: str) -> int:
        """Drop the announcer's breaks still waiting for their song change.

        Breaks turned off in Settings: a break already mixed into the next
        song change still airs, and ad spots booked by the ad schedule
        stay.  Returns how many were dropped.
        """
        with self._lock:
            items = [
                item
                for item in self._breaks.values()
                if item.state == QUEUED and item.source == ANNOUNCER
            ]
            for item in items:
                self._finish_locked(item, DROPPED, reason)
            slots = {item.slot_id for item in items}
        self._flush(slots)
        return len(items)

    def on_air(self, break_id: str) -> None:
        """The break's voice has started.  Safe on the mix-bus thread."""
        with self._lock:
            item = self._breaks.get(break_id)
            if item is not None and item.state in (QUEUED, RENDERED):
                item.state = ON_AIR
                item.aired_at = item.updated_at = self._clock()

    def aired(self, break_id: str) -> None:
        """The break's speech has ended.  Safe on the mix-bus thread."""
        with self._lock:
            item = self._breaks.get(break_id)
            if item is not None and item.state not in TERMINAL:
                if item.aired_at is None:
                    item.aired_at = self._clock()
                self._finish_locked(item, AIRED, "")
        self._flush()

    def _finish_locked(self, item: Break, state: str, reason: str) -> None:
        item.state = state
        item.reason = reason
        item.updated_at = self._clock()
        if not item.keep_file:
            self._doomed.setdefault(item.path, 0.0)
        if state == AIRED:
            self._log(logging.INFO, "Break %s aired", item.break_id[:8])
        else:
            self._log(logging.WARNING, "Break %s %s: %s", item.break_id[:8], state, reason)

    # ------------------------------------------------------------------
    # Upkeep (a worker thread, from the station's tick)
    # ------------------------------------------------------------------

    def needs_maintenance(self) -> bool:
        """Whether :meth:`maintain` has anything to do.  Cheap; safe on the event loop."""
        now = self._clock()
        with self._lock:
            return (
                not self._swept
                or any(due <= now for due in self._doomed.values())
                or len(self._breaks) > HISTORY
                or any(self._late_locked(item, now) for item in self._breaks.values())
            )

    @staticmethod
    def _late_locked(item: Break, now: float) -> bool:
        if item.state == QUEUED:
            return now > item.expires_at
        return (
            item.state == RENDERED
            and item.eta is not None
            and now > item.eta + RENDERED_GRACE_SECONDS
        )

    def maintain(self) -> None:
        """Expire late breaks, delete finished files and forget old history.

        A queued break past its expiry, and a rendered one long past its
        expected air time (its render was thrown away), expire.  The first
        run also deletes break files a previous run left behind, sparing
        uploads in progress: breaks do not survive a restart, as the slot
        plan does not.
        """
        now = self._clock()
        emptied: set[str] = set()
        with self._lock:
            for item in self._breaks.values():
                if self._late_locked(item, now):
                    reason = (
                        "it expired before its song change"
                        if item.state == QUEUED
                        else "it was mixed in but never reached the air"
                    )
                    if item.state == QUEUED:
                        emptied.add(item.slot_id)
                    self._finish_locked(item, EXPIRED, reason)
            doomed = {path: due for path, due in self._doomed.items() if due <= now}
            finished = [key for key, item in self._breaks.items() if item.state in TERMINAL]
            for key in finished[: max(0, len(self._breaks) - HISTORY)]:
                del self._breaks[key]
            keep = {
                item.path.name
                for item in self._breaks.values()
                if item.state not in TERMINAL and not item.keep_file
            }
            keep.update(held.name for held in self._reserved.values())
            sweep = not self._swept
            self._swept = True
        failed: dict[Path, float] = {}
        for path, due in doomed.items():
            try:
                path.unlink(missing_ok=True)
            except OSError:
                failed[path] = now + DELETE_RETRY_SECONDS
                if due == 0.0:  # said once; tried again every half minute
                    logger.warning(
                        "Could not delete the break file %s; trying again", path.name, exc_info=True
                    )
        with self._lock:
            for path in doomed:
                self._doomed.pop(path, None)
            self._doomed.update(failed)
        if sweep and self.folder.is_dir():
            # File times are the computer's, whatever clock the desk keeps.
            self._sweep(keep, time.time())
        self._flush(emptied)

    def _sweep(self, keep: set[str], now: float) -> None:
        """Delete break files a previous run left, sparing *keep* and young part files."""
        for path in self.folder.iterdir():
            name = path.name
            if _FILE.fullmatch(name):
                if name in keep:
                    continue
            elif name.startswith(".brk-") and name.endswith(".part"):
                if any(name.startswith(f".{kept}.") for kept in keep):
                    continue
                try:
                    if now - path.stat().st_mtime < PART_GRACE_SECONDS:
                        continue
                except OSError:
                    continue
            else:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not delete %s", name, exc_info=True)


def placement_for(
    kind: str,
    requested: str,
    setting: str,
    intro_s: float | None,
    min_intro_s: float,
) -> tuple[str, str]:
    """Where a break goes, and why when that is not what was asked.

    Args:
        kind: The break's kind.
        requested: The request's placement.
        setting: ``[breaks] dj_placement``.
        intro_s: Seconds of the upcoming song before its vocal, or ``None``
            when it has no intro point.
        min_intro_s: The shortest intro ``auto`` talks up.

    Returns:
        ``("talk_up" | "standalone", note)``; *note* is empty unless a
        talk-up could not be done.
    """
    if kind != "dj":
        return "standalone", ""
    wanted = setting if requested == "auto" else requested
    if wanted == "standalone":
        return "standalone", ""
    intro = intro_s if intro_s is not None and math.isfinite(intro_s) else 0.0
    if wanted == "auto" and intro < min_intro_s:
        return "standalone", (
            "the upcoming song has no intro point"
            if intro_s is None
            else f"the upcoming song's intro is shorter than {min_intro_s:g} s"
        )
    if intro <= 0:
        return "standalone", "the upcoming song has no intro point"
    return "talk_up", ""


__all__ = [
    "AD",
    "AD_SCHEDULE",
    "AIRED",
    "ANNOUNCER",
    "DROPPED",
    "EXPIRED",
    "EXTENSIONS",
    "KINDS",
    "ON_AIR",
    "PLACEMENTS",
    "QUEUED",
    "RENDERED",
    "TERMINAL",
    "VOICE_TRACK",
    "Break",
    "BreakCues",
    "BreakDesk",
    "BreakError",
    "SlotPlan",
    "TransitionItem",
    "file_name",
    "new_id",
    "placement_for",
    "speech_cues",
    "valid_id",
]
