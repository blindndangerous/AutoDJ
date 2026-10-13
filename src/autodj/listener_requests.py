"""Listener requests (#49): listeners ask for songs; the station decides.

A station with ``[requests] enabled`` serves a public page where anyone
who can reach the server searches the library and requests a song (see
:mod:`autodj.request_page`).  A request is checked here, in this order:

1. the form's token, so another site cannot post it for a visitor;
2. per-address limits: searches and form posts per minute, and requests
   per ``per_listener_minutes``;
3. the station's daily cap and how many requests may wait at once;
4. the song itself: music in the library, not in a blocked folder or by a
   blocked artist, not already playing, coming up or waiting;
5. where it would play: :meth:`PlayerBridge.request_slots` checks the
   repeat rules, the same-song window and, in DMCA mode, the DMCA limits
   at each place in the queue within ``window_minutes``.

With ``approval = "manual"`` an accepted request waits for the operator;
with ``"auto"`` it joins the queue at once.  Either way it goes to the
place that fits best: the one whose neighbours it sounds most like (the
embeddings), with a compatible key, a near tempo (half and double time
count) and a small energy step.  The operator's list says where and why.

A listener is told only roughly when a song may play ("within the next
hour"), never a set time: a station whose listeners pick when songs play
could count as an interactive service, which the US statutory webcasting
licence does not cover.  General information, not legal advice.

Nothing a listener sends reaches a file path: songs are named by an
opaque id (a keyed hash of the path, :class:`TrackIds`), and the page
shows only title, artist and album.  Listener addresses are kept in memory
for the limits only, never on disk.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from autodj.dj_meta import harmonic_compatible
from autodj.fsutil import atomic_write
from autodj.repeat_rules import ArtistSplitter, artist_key

if TYPE_CHECKING:
    from autodj._bridge import PlayerBridge, RequestSlot
    from autodj.config import RequestsConfig
    from autodj.indexer import IndexEntry

logger = logging.getLogger(__name__)

#: Longest search a listener may send, and the fewest letters it needs.
MAX_QUERY = 100
MIN_QUERY = 2
#: Songs one search shows.
SEARCH_RESULTS = 20
#: Longest name a listener may give.
MAX_NAME = 40
#: Searches one address may run per minute, and form posts per ten minutes.
SEARCHES_PER_MINUTE = 30
POSTS_PER_TEN_MINUTES = 10
#: A request still waiting this long is dropped.
PENDING_HOURS = 3
#: Decided requests the operator's list keeps.
KEEP_DECIDED = 50
#: A form token is good for this long.
TOKEN_SECONDS = 2 * 3600

TRACK_ID = re.compile(r"[0-9a-f]{24}\Z")
REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")
STATE_VERSION = 1

PENDING, QUEUED, REJECTED, EXPIRED = "pending", "queued", "rejected", "expired"


# ---------------------------------------------------------------------------
# Limits, form tokens and song ids
# ---------------------------------------------------------------------------


class AddressLimiter:
    """At most *limit* events per *window_s* seconds for each address.

    Each address keeps the times of its events in the window.  Addresses
    with nothing left in the window are forgotten, and at most *max_keys*
    are kept, oldest first out, so many addresses cannot grow it without end.
    """

    def __init__(
        self,
        limit: int,
        window_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 4096,
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._max_keys = max_keys
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _live(self, address: str, now: float) -> deque[float]:
        events = self._events.get(address)
        if events is None:
            return deque()
        while events and now - events[0] >= self.window_s:
            events.popleft()
        if not events:
            del self._events[address]
        return events

    def retry_after(self, address: str) -> int | None:
        """Seconds until *address* may go again, or ``None`` if it may now (not counted)."""
        with self._lock:
            now = self._clock()
            events = self._live(address, now)
            if len(events) < self.limit:
                return None
            return max(1, math.ceil(self.window_s - (now - events[0])))

    def hit(self, address: str) -> int | None:
        """Count one event for *address*; seconds to wait instead when it is over the limit."""
        with self._lock:
            now = self._clock()
            events = self._live(address, now)
            if len(events) >= self.limit:
                return max(1, math.ceil(self.window_s - (now - events[0])))
            if address not in self._events:
                while len(self._events) >= self._max_keys:
                    del self._events[next(iter(self._events))]
                self._events[address] = events
            events.append(now)
            return None


class FormTokens:
    """Signed, time-limited tokens for a public form that has no session.

    A token is ``<issued>.<mac>``, the mac an HMAC of the station's name and
    the time it was issued, under a key made when the server starts.  A
    form posted from another site cannot carry one (the page is
    same-origin), and a token from one station does not work on another.
    """

    def __init__(
        self,
        secret: bytes | None = None,
        *,
        clock: Callable[[], float] = time.time,
        max_age: float = TOKEN_SECONDS,
    ) -> None:
        self._secret = secret or secrets.token_bytes(32)
        self._clock = clock
        self._max_age = max_age

    def _mac(self, scope: str, issued: int) -> str:
        message = f"form:{scope}:{issued}".encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()[:32]

    def issue(self, scope: str) -> str:
        """A new token for the form of *scope* (a station's name)."""
        issued = int(self._clock())
        return f"{issued}.{self._mac(scope, issued)}"

    def valid(self, token: str, scope: str) -> bool:
        """Whether *token* was issued for *scope* within the last two hours."""
        issued_text, dot, mac = token.partition(".")
        if not dot or not issued_text.isdecimal() or len(issued_text) > 12 or len(mac) != 32:
            return False
        issued = int(issued_text)
        age = self._clock() - issued
        if not -60 <= age <= self._max_age:
            return False
        return hmac.compare_digest(mac, self._mac(scope, issued))


class TrackIds:
    """Opaque ids for songs, so no file path reaches or comes from a listener.

    An id is a keyed BLAKE2b hash of the path, under a key made when the
    server starts: it reveals nothing about the path and cannot be worked
    out from one.  Ids change when the server restarts.
    """

    def __init__(self, secret: bytes | None = None) -> None:
        self._secret = secret or secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._cache: tuple[object, dict[str, IndexEntry]] | None = None

    def id_for(self, path: str) -> str:
        """The id of the song at *path*."""
        digest = hashlib.blake2b(path.encode("utf-8"), key=self._secret, digest_size=12)
        return digest.hexdigest()

    def lookup(self, indexed: Sequence[tuple[IndexEntry, str]], track_id: str) -> IndexEntry | None:
        """The song with *track_id* among *indexed*; the map is kept until *indexed* changes."""
        if not TRACK_ID.fullmatch(track_id):
            return None
        with self._lock:
            cached = self._cache
            if cached is None or cached[0] is not indexed:
                cached = (indexed, {self.id_for(entry.path): entry for entry, _ in indexed})
                self._cache = cached
            return cached[1].get(track_id)


# ---------------------------------------------------------------------------
# What may be requested
# ---------------------------------------------------------------------------


class Blocklist:
    """The artists and folders a station keeps out of requests."""

    def __init__(self, artists: Iterable[str], folders: Iterable[str], music_dir: Path) -> None:
        self._artists = frozenset(key for key in map(artist_key, artists) if key)
        self._splitter = ArtistSplitter()
        self._folders = tuple(
            os.path.normcase(
                os.path.abspath(Path(folder) if Path(folder).is_absolute() else music_dir / folder)
            )
            for folder in folders
        )

    def blocks(self, entry: IndexEntry) -> bool:
        """Whether *entry* is by a blocked artist or in a blocked folder."""
        if self._artists:
            credited = {
                artist_key(entry.artist),
                *self._splitter.credited(entry.artist, entry.title),
            }
            if credited & self._artists:
                return True
        if self._folders:
            path = os.path.normcase(os.path.abspath(entry.path))
            return any(
                path == folder or path.startswith(folder + os.sep) for folder in self._folders
            )
        return False


# ---------------------------------------------------------------------------
# Where a request fits
# ---------------------------------------------------------------------------


def _tempo_gap(a: float, b: float) -> float | None:
    """BPM apart, counting half and double time as the same tempo; ``None`` if unknown."""
    if a <= 0 or b <= 0:
        return None
    return min(abs(a - b), abs(2 * a - b), abs(a - 2 * b))


@dataclass(frozen=True)
class Fit:
    """How smoothly one song follows another: a cost (lower is smoother) and why, in words."""

    cost: float
    words: tuple[str, ...]


def fit(
    before: IndexEntry, after: IndexEntry, similarity: Callable[[str, str], float | None]
) -> Fit:
    """How smoothly *after* follows *before*.

    The same signals the picker uses: sound (the embeddings), tempo (half
    and double time count as a match), Camelot key and the energy step.
    A signal that is not known costs half.
    """
    words: list[str] = []
    sound = similarity(before.path, after.path)
    if sound is None:
        cost = 0.5
    else:
        cost = max(0.0, 1.0 - sound)
        if sound >= 0.8:
            words.append("similar sound")
    gap = _tempo_gap(float(before.bpm or 0), float(after.bpm or 0))
    if gap is None:
        cost += 0.5
    else:
        cost += min(1.0, gap / 20.0)
        words.append(f"{round(gap)} BPM apart" if gap >= 0.5 else "same tempo")
    keys = (int(before.key), int(before.mode), int(after.key), int(after.mode))
    if min(keys) < 0:
        cost += 0.5
    elif harmonic_compatible(*keys, mode="strict"):
        words.append("same key")
    elif harmonic_compatible(*keys):
        cost += 0.2
        words.append("compatible key")
    else:
        cost += 1.0
        words.append("key change")
    energy_a, energy_b = float(before.energy or 0), float(after.energy or 0)
    cost += 0.5 if energy_a <= 0 or energy_b <= 0 else min(1.0, abs(energy_a - energy_b))
    return Fit(cost, tuple(words))


@dataclass(frozen=True)
class Placement:
    """The best place found for a request, or why there is none.

    Attributes:
        slot: Where it goes, or ``None`` when every place breaks a rule.
        note: For the operator: where and why ("Fits after Song by Artist:
            same key, 2 BPM apart."), or what stops it.
        problems: When there is no place, the rules the first place breaks.
    """

    slot: RequestSlot | None
    note: str
    problems: tuple[str, ...] = ()


def place(
    entry: IndexEntry,
    slots: Sequence[RequestSlot],
    similarity: Callable[[str, str], float | None],
) -> Placement:
    """Choose the slot where *entry* joins its neighbours most smoothly.

    Only slots that break no rule count; among equals, the sooner wins.
    """
    best: tuple[float, RequestSlot, Fit | None] | None = None
    for slot in slots:
        if slot.problems:
            continue
        cost = 0.02 * slot.position
        joined = fit(slot.before, entry, similarity) if slot.before is not None else None
        if joined is not None:
            cost += joined.cost
        if slot.after is not None:
            cost += fit(entry, slot.after, similarity).cost
        if best is None or cost < best[0]:
            best = (cost, slot, joined)
    if best is None:
        problems = slots[0].problems if slots else ()
        return Placement(
            None, f"No place in the window keeps the rules: {_join(problems)}.", problems
        )
    _cost, slot, joined = best
    if slot.before is None:
        note = "Plays first."
    elif joined is not None and joined.words:
        note = f"Fits after {slot.before.display_name}: {', '.join(joined.words)}."
    else:
        note = f"Fits after {slot.before.display_name}."
    return Placement(slot, note)


def _join(problems: Sequence[str]) -> str:
    return "; ".join(problems) if problems else "it breaks a rule"


def rough_wait(seconds: float) -> str:
    """When a song may play, never as a set time: "within the next half hour"."""
    if seconds < 30 * 60:
        return "within the next half hour"
    if seconds < 60 * 60:
        return "within the next hour"
    return "within the next few hours"


# ---------------------------------------------------------------------------
# The requests themselves
# ---------------------------------------------------------------------------


@dataclass
class ListenerRequest:
    """One request, as the operator sees it.  The path stays on the server."""

    id: str
    path: str
    title: str
    artist: str
    album: str
    name: str
    status: str
    requested_at: float
    decided_at: float | None = None
    note: str = ""

    def public(self) -> dict[str, Any]:
        """The fields the operator's page shows: no path."""
        data = asdict(self)
        del data["path"]
        return data


@dataclass
class RequestBook:
    """A station's requests, saved in ``listener_requests.json``.

    Waiting requests are kept until decided or expired; decided ones only
    the latest :data:`KEEP_DECIDED`.  A damaged file is set aside, not used.
    """

    path: Path
    timezone: str = "UTC"
    clock: Callable[[], float] = time.time
    requests: list[ListenerRequest] = field(default_factory=list)
    day: str = ""
    today: int = 0

    def __post_init__(self) -> None:
        self.lock = threading.RLock()
        self._zone = ZoneInfo(self.timezone)
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning("Setting aside the damaged listener request file %s", self.path)
            self._set_aside()
            return
        try:
            if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
                raise ValueError("unknown version")
            self.requests = [ListenerRequest(**row) for row in raw.get("requests", [])]
            self.day = str(raw.get("day", ""))
            self.today = int(raw.get("today", 0))
        except (TypeError, ValueError):
            logger.warning("Setting aside the damaged listener request file %s", self.path)
            self.requests, self.day, self.today = [], "", 0
            self._set_aside()

    def _set_aside(self) -> None:
        # The file may have vanished or be impossible to move; it is not used either way.
        with contextlib.suppress(OSError):
            os.replace(self.path, self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.bad"))

    def save(self) -> None:
        """Write the file (hold :attr:`lock`)."""
        data = {
            "version": STATE_VERSION,
            "day": self.day,
            "today": self.today,
            "requests": [asdict(request) for request in self.requests],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(data, indent=1))

    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock(), UTC).astimezone(self._zone).date().isoformat()

    def count_today(self) -> int:
        """Requests accepted since midnight, station time."""
        with self.lock:
            return self.today if self.day == self._today() else 0

    def tidy(self) -> bool:
        """Expire requests that waited too long and drop old decisions; whether anything changed."""
        with self.lock:
            now = self.clock()
            changed = False
            for request in self.requests:
                if request.status == PENDING and now - request.requested_at > PENDING_HOURS * 3600:
                    request.status, request.decided_at = EXPIRED, now
                    request.note = "Waited too long without a decision."
                    changed = True
            decided = [r for r in self.requests if r.status != PENDING]
            if len(decided) > KEEP_DECIDED:
                drop = {
                    id(r) for r in sorted(decided, key=lambda r: r.decided_at or 0)[:-KEEP_DECIDED]
                }
                self.requests = [r for r in self.requests if id(r) not in drop]
                changed = True
            return changed

    def pending(self) -> list[ListenerRequest]:
        """Requests waiting for the operator, oldest first."""
        with self.lock:
            return [r for r in self.requests if r.status == PENDING]

    def recent(self) -> list[ListenerRequest]:
        """Decided requests, newest first."""
        with self.lock:
            decided = [r for r in self.requests if r.status != PENDING]
            return sorted(decided, key=lambda r: r.decided_at or 0, reverse=True)

    def get(self, request_id: str) -> ListenerRequest | None:
        """The request with *request_id*, or ``None``."""
        with self.lock:
            return next((r for r in self.requests if r.id == request_id), None)

    def add(self, entry: IndexEntry, name: str, status: str, note: str) -> ListenerRequest:
        """Record an accepted request and count it for today; saved at once."""
        with self.lock:
            now = self.clock()
            day = self._today()
            if day != self.day:
                self.day, self.today = day, 0
            self.today += 1
            request = ListenerRequest(
                id=uuid.uuid4().hex,
                path=entry.path,
                title=str(entry.title or ""),
                artist=str(entry.artist or ""),
                album=str(entry.album or ""),
                name=name,
                status=status,
                requested_at=now,
                decided_at=now if status != PENDING else None,
                note=note,
            )
            self.requests.append(request)
            self.save()
            return request

    def decide(self, request: ListenerRequest, status: str, note: str) -> None:
        """Mark *request* queued or rejected; saved at once."""
        with self.lock:
            request.status, request.decided_at, request.note = status, self.clock(), note
            self.save()


# ---------------------------------------------------------------------------
# One station's request desk
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What the listener is told.

    Attributes:
        status: The HTTP status of the page that says it.
        headline: A short heading, also the start of the page title.
        detail: The rest, in a sentence or two.
        retry_after: For 429, seconds until the listener may try again.
    """

    status: int
    headline: str
    detail: str
    retry_after: int | None = None


def clean_name(value: str) -> str | None:
    """A listener's name, trimmed, or ``None`` when it is too long or has control characters."""
    name = " ".join(value.split())
    if len(name) > MAX_NAME or not name.isprintable():
        return None
    return name


class RequestDesk:
    """Takes a station's listener requests and the operator's decisions."""

    def __init__(
        self,
        slug: str,
        cfg: RequestsConfig,
        bridge: PlayerBridge,
        book: RequestBook,
        *,
        music_dir: Path,
        tokens: FormTokens,
        track_ids: TrackIds,
        monotonic: Callable[[], float] = time.monotonic,
        on_queued: Callable[[], object] = lambda: None,
    ) -> None:
        self.slug = slug
        # Called after a request joins the queue (refreshes the public feed).
        self._on_queued = on_queued
        self.cfg = cfg
        self.bridge = bridge
        self.book = book
        self.tokens = tokens
        self.track_ids = track_ids
        self.blocklist = Blocklist(cfg.blocked_artists, cfg.blocked_folders, music_dir)
        self.searches = AddressLimiter(SEARCHES_PER_MINUTE, 60.0, clock=monotonic)
        self.posts = AddressLimiter(POSTS_PER_TEN_MINUTES, 600.0, clock=monotonic)
        self.accepted = AddressLimiter(
            cfg.per_listener, cfg.per_listener_minutes * 60.0, clock=monotonic
        )
        self._lock = threading.Lock()

    # -- what listeners see -------------------------------------------------

    def search(self, query: str) -> list[tuple[str, IndexEntry]]:
        """Up to :data:`SEARCH_RESULTS` requestable songs matching every word of *query*."""
        tokens = query.lower().split()
        found: list[tuple[str, IndexEntry]] = []
        for entry, text in self.bridge.search_index():
            if all(token in text for token in tokens) and not self.blocklist.blocks(entry):
                found.append((self.track_ids.id_for(entry.path), entry))
                if len(found) >= SEARCH_RESULTS:
                    break
        return found

    def song(self, track_id: str) -> IndexEntry | None:
        """The requestable song with *track_id*, or ``None``."""
        entry = self.track_ids.lookup(self.bridge.search_index(), track_id)
        if entry is None or self.blocklist.blocks(entry):
            return None
        return entry

    def _horizon(self) -> float:
        return self.cfg.window_minutes * 60.0

    def _similarity(self, a: str, b: str) -> float | None:
        return self.bridge.sim.similarity(a, b)

    def _placement(self, entry: IndexEntry) -> Placement:
        return place(entry, self.bridge.request_slots(entry, self._horizon()), self._similarity)

    def _coming_up(self, entry: IndexEntry) -> bool:
        return any(e.path == entry.path for e in self.bridge.coming_up())

    def submit(self, track_id: str, name: str, address: str) -> Outcome:
        """Take one request from *address*; the form's token was checked already."""
        with self._lock:
            return self._submit(track_id, name, address)

    def _submit(self, track_id: str, name: str, address: str) -> Outcome:
        cfg = self.cfg
        if (wait := self.accepted.retry_after(address)) is not None:
            return Outcome(
                429,
                "Too many requests",
                f"You can request up to {_songs(cfg.per_listener)} every "
                f"{_minutes(cfg.per_listener_minutes)}. Please try again later.",
                retry_after=wait,
            )
        if self.book.count_today() >= cfg.daily_limit:
            return Outcome(
                429,
                "No more requests today",
                "The station has taken all the requests it can today. Please try again tomorrow.",
                retry_after=3600,
            )
        entry = self.song(track_id)
        if entry is None:
            return Outcome(
                404, "Song not found", "That song cannot be requested. Please search again."
            )
        self.book.tidy()
        if self._coming_up(entry) or any(r.path == entry.path for r in self.book.pending()):
            return Outcome(
                409,
                "Already on the way",
                f"{_song(entry)} is already playing, coming up or waiting to be played.",
            )
        auto = cfg.approval == "auto"
        if not auto and len(self.book.pending()) >= cfg.max_pending:
            return Outcome(
                429,
                "Too many requests waiting",
                "The station has many requests waiting already. Please try again later.",
                retry_after=600,
            )
        placement = self._placement(entry)
        if placement.slot is None:
            return Outcome(
                409,
                "Not right now",
                f"{_song(entry)} cannot play again so soon: the station keeps a gap between "
                "plays of a song, an artist and an album. Please try another song, or this one "
                "later.",
            )
        self.accepted.hit(address)
        if auto:
            slot = placement.slot
            self.bridge.queue_insert(entry.path, slot.position)
            self._on_queued()
            request = self.book.add(entry, name, QUEUED, placement.note)
            logger.info("Listener request queued: %s (%s)", entry.display_name, placement.note)
            return Outcome(
                200,
                "Request accepted",
                f"Thanks{_for(request.name)}. {_song(entry)} should play {rough_wait(slot.starts_in)}.",
            )
        request = self.book.add(entry, name, PENDING, placement.note)
        logger.info("Listener request waiting: %s", entry.display_name)
        return Outcome(
            200,
            "Request received",
            f"Thanks{_for(request.name)}. The station will look at your request for "
            f"{_song(entry)}. If it is approved, it should play within the next hour or so.",
        )

    # -- what the operator does ---------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """The operator's view: settings, today's count, waiting requests with where they would go."""
        if self.book.tidy():
            with self.book.lock:
                self.book.save()
        pending = []
        for request in self.book.pending():
            row = request.public()
            entry = self.bridge.entry_for_path(request.path)
            if entry is None:
                row["fit"] = "The song is no longer in the library."
                row["can_approve"] = False
            else:
                placement = self._placement(entry)
                row["fit"] = placement.note
                row["can_approve"] = placement.slot is not None
            pending.append(row)
        return {
            "enabled": self.cfg.enabled,
            "approval": self.cfg.approval,
            "today": self.book.count_today(),
            "daily_limit": self.cfg.daily_limit,
            "pending": pending,
            "recent": [request.public() for request in self.book.recent()[:20]],
        }

    def _waiting(self, request_id: str) -> tuple[ListenerRequest, IndexEntry]:
        if not REQUEST_ID.fullmatch(request_id):
            raise KeyError(request_id)
        request = self.book.get(request_id)
        if request is None or request.status != PENDING:
            raise KeyError(request_id)
        entry = self.bridge.entry_for_path(request.path)
        if entry is None:
            raise LookupError("The song is no longer in the library.")
        return request, entry

    def approve(self, request_id: str) -> ListenerRequest:
        """Queue a waiting request where it fits best.

        Raises:
            KeyError: No request with that id is waiting.
            LookupError: The song is gone from the library.
            ValueError: Every place within the window breaks a rule; says which.
        """
        with self._lock:
            request, entry = self._waiting(request_id)
            placement = self._placement(entry)
            if placement.slot is None:
                raise ValueError(
                    f"{entry.display_name} cannot be queued within the next "
                    f"{_minutes(self.cfg.window_minutes)}: {_join(placement.problems)}."
                )
            self.bridge.queue_insert(entry.path, placement.slot.position)
            self._on_queued()
            self.book.decide(request, QUEUED, placement.note)
            return request

    def play_next(self, request_id: str) -> ListenerRequest:
        """Queue a waiting request first, whatever fits; DMCA limits still hold.

        Raises:
            KeyError: No request with that id is waiting.
            LookupError: The song is gone from the library.
            ValueError: DMCA mode is on and playing it next would break a limit.
        """
        with self._lock:
            request, entry = self._waiting(request_id)
            broken = self.bridge.dmca_queue_limits(entry, now=False, append=False)
            if broken:
                raise ValueError(f"DMCA mode: playing this song next would mean {_join(broken)}.")
            self.bridge.queue_insert(entry.path, 0)
            self._on_queued()
            self.book.decide(request, QUEUED, "Play next, chosen by the operator.")
            return request

    def reject(self, request_id: str) -> ListenerRequest:
        """Turn a waiting request down.

        Raises:
            KeyError: No request with that id is waiting.
        """
        with self._lock:
            if not REQUEST_ID.fullmatch(request_id):
                raise KeyError(request_id)
            request = self.book.get(request_id)
            if request is None or request.status != PENDING:
                raise KeyError(request_id)
            self.book.decide(request, REJECTED, "Turned down by the operator.")
            return request


def _song(entry: IndexEntry) -> str:
    title = str(entry.title or "").strip() or "That song"
    artist = str(entry.artist or "").strip()
    return f"{title} by {artist}" if artist else title


def _songs(count: int) -> str:
    return f"{count} song{'' if count == 1 else 's'}"


def _for(name: str) -> str:
    return f", {name}" if name else ""


def _minutes(minutes: int) -> str:
    if minutes % 60 == 0:
        hours = minutes // 60
        return "hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} minutes"


#: Made when the server starts, shared by every station (a token names its station).
FORM_TOKENS = FormTokens()
TRACK_IDS = TrackIds()


__all__ = [
    "FORM_TOKENS",
    "TRACK_IDS",
    "AddressLimiter",
    "Blocklist",
    "FormTokens",
    "ListenerRequest",
    "Outcome",
    "Placement",
    "RequestBook",
    "RequestDesk",
    "TrackIds",
    "clean_name",
    "fit",
    "place",
    "rough_wait",
]
