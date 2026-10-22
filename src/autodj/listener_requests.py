"""Listener requests (#49): listeners ask for songs; the station decides.

A station that takes requests (``[requests] enabled``, until the Listener
requests card saves its own switch for the station; from then on that saved
switch) serves a public page where anyone
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
   at each place in the queue within ``window_minutes``, timed as the hour
   preview times the queue, and the queued songs after each place again
   with the request ahead of them (#218).

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
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from autodj._bridge import QueueChanged, QueueView, RequestInsert
from autodj.dj_meta import harmonic_compatible
from autodj.fsutil import atomic_write
from autodj.repeat_rules import ArtistSplitter, artist_key
from autodj.station_log import track_station_thread

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
#: Listener names are dropped from decided requests after this many days.
NAME_DAYS = 30
#: How often a station's requests are tidied (expiries, old names) when
#: nothing else touches them.
TIDY_SECONDS = 3600.0
#: Song ids a station remembers from its searches, newest kept.
REMEMBERED_SONGS = 10000
#: How long, and how many, recent searches are answered from memory.
SEARCH_CACHE_SECONDS = 30.0
SEARCH_CACHE_SIZE = 256

TRACK_ID = re.compile(r"[0-9a-f]{24}\Z")
REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")
STATE_VERSION = 1

PENDING, QUEUED, REJECTED, EXPIRED = "pending", "queued", "rejected", "expired"


# ---------------------------------------------------------------------------
# Limits, form tokens and song ids
# ---------------------------------------------------------------------------


def limit_key(address: str, ipv6_prefix: int = 56) -> str:
    """The key an address's limits count under.

    An IPv4 address counts alone, and so does an IPv4-mapped IPv6 address,
    as its IPv4 address.  An IPv6 address counts with its whole network of
    *ipv6_prefix* bits (``[requests] ipv6_prefix``, /56 by default): a home
    is usually delegated a /56 or a /64 and can use any address in it, so
    counting single addresses would let it rotate past every limit.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network((ip, ipv6_prefix), strict=False))
    return str(ip)


class AddressLimiter:
    """At most *limit* events per *window_s* seconds for each address.

    Addresses are counted by :func:`limit_key`: an IPv6 address with its
    network of *ipv6_prefix* bits.

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
        ipv6_prefix: int = 56,
    ) -> None:
        self.limit = limit
        self.ipv6_prefix = ipv6_prefix
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
        address = limit_key(address, self.ipv6_prefix)
        with self._lock:
            now = self._clock()
            events = self._live(address, now)
            if len(events) < self.limit:
                return None
            return max(1, math.ceil(self.window_s - (now - events[0])))

    def hit(self, address: str) -> int | None:
        """Count one event for *address*; seconds to wait instead when it is over the limit."""
        address = limit_key(address, self.ipv6_prefix)
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

    A token is ``<issued>.<mac>``, the mac an HMAC of the station's name,
    the song's id and the time it was issued, under a key made when the
    server starts.  A form posted from another site cannot carry one (the
    page is same-origin), and a token works only for the station and the
    song it was issued for.
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

    def _mac(self, scope: str, track_id: str, issued: int) -> str:
        message = f"form:{scope}:{track_id}:{issued}".encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()[:32]

    def issue(self, scope: str, track_id: str) -> str:
        """A new token for the request form of *track_id* on *scope* (a station's name)."""
        issued = int(self._clock())
        return f"{issued}.{self._mac(scope, track_id, issued)}"

    def valid(self, token: str, scope: str, track_id: str) -> bool:
        """Whether *token* was issued for *track_id* on *scope* within the last two hours."""
        if not token.isascii():
            # compare_digest raises TypeError on non-ASCII text: refuse it as a bad token.
            return False
        issued_text, dot, mac = token.partition(".")
        if not dot or not issued_text.isdecimal() or len(issued_text) > 12 or len(mac) != 32:
            return False
        issued = int(issued_text)
        age = self._clock() - issued
        if not -60 <= age <= self._max_age:
            return False
        return hmac.compare_digest(mac, self._mac(scope, track_id, issued))


class TrackIds:
    """Opaque ids for songs, so no file path reaches or comes from a listener.

    An id is a keyed BLAKE2b hash of the path, under a key made when the
    server starts: it reveals nothing about the path and cannot be worked
    out from one.  Ids change when the server restarts.

    Ids are worked out on demand, one per song shown, and the songs a
    search showed are remembered by id (at most *remember* of them, newest
    kept), so a request needs no map of the whole library: only a song a
    search showed can be requested.
    """

    def __init__(self, secret: bytes | None = None, *, remember: int = REMEMBERED_SONGS) -> None:
        self._secret = secret or secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._remember = remember
        self._shown: OrderedDict[str, str] = OrderedDict()

    def id_for(self, path: str) -> str:
        """The id of the song at *path*."""
        digest = hashlib.blake2b(path.encode("utf-8"), key=self._secret, digest_size=12)
        return digest.hexdigest()

    def shown(self, path: str) -> str:
        """The id of the song at *path*, remembered so it can be requested."""
        track_id = self.id_for(path)
        with self._lock:
            self._shown[track_id] = path
            self._shown.move_to_end(track_id)
            while len(self._shown) > self._remember:
                self._shown.popitem(last=False)
        return track_id

    def path_for(self, track_id: str) -> str | None:
        """The path of a song a search showed under *track_id*, or ``None``."""
        if not TRACK_ID.fullmatch(track_id):
            return None
        with self._lock:
            return self._shown.get(track_id)


# ---------------------------------------------------------------------------
# What may be requested
# ---------------------------------------------------------------------------


class Blocklist:
    """The artists and folders a station keeps out of requests."""

    def __init__(
        self,
        artists: Iterable[str],
        folders: Iterable[str],
        music_dir: Path | None,
        music_roots: Callable[[], Iterable[Path | str]] | None = None,
    ) -> None:
        self._artists = frozenset(key for key in map(artist_key, artists) if key)
        self._splitter = ArtistSplitter()
        self._music_roots = music_roots
        absolute: list[str] = []
        relative: list[str] = []
        for folder in folders:
            path = Path(folder)
            if path.is_absolute():
                absolute.append(os.path.normcase(os.path.abspath(path)))
            elif music_dir is not None:
                # Keep the legacy configured-root meaning for existing stations.
                absolute.append(os.path.normcase(os.path.abspath(music_dir / path)))
            else:
                # Rootless stations resolve this against current catalog roots
                # when checking an entry, so source edits take effect at once.
                relative.append(os.path.normcase(os.path.normpath(str(path))))
        self._fixed_folders = tuple(absolute)
        self._relative_folders = tuple(relative)
        self._folders = self._fixed_folders
        self.refresh()

    def refresh(self) -> bool:
        """Resolve rootless relative folders against the current catalog roots."""
        roots = self._music_roots() if self._relative_folders and self._music_roots else ()
        resolved = tuple(
            os.path.normcase(os.path.abspath(Path(root) / relative))
            for root in roots
            for relative in self._relative_folders
        )
        folders = (*self._fixed_folders, *resolved)
        changed = folders != self._folders
        self._folders = folders
        return changed

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
            if any(path == folder or path.startswith(folder + os.sep) for folder in self._folders):
                return True
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
    cost: float = math.inf
    bridges: tuple[IndexEntry, ...] = ()
    bridge_clock_next: tuple[int | None, ...] = ()


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
    return Placement(slot, note, cost=_cost)


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
    bridges: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        """The fields the operator's page shows: no path."""
        data = asdict(self)
        del data["path"]
        data["reason"] = self.note
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
        # Expiries and old names are applied at once, not at the next use.
        self.tidy_and_save()

    def tidy_and_save(self) -> None:
        """:meth:`tidy`, and save when it changed anything."""
        with self.lock:
            if self.tidy():
                self.save()

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
        """Expire requests that waited too long, drop old decisions and old names.

        A name is kept :data:`NAME_DAYS` days after its request was decided,
        whatever the number of decisions.  Returns whether anything changed;
        the caller saves.
        """
        with self.lock:
            now = self.clock()
            changed = False
            for request in self.requests:
                if request.status == PENDING and now - request.requested_at > PENDING_HOURS * 3600:
                    request.status, request.decided_at = EXPIRED, now
                    request.note = "Waited too long without a decision."
                    changed = True
                if (
                    request.name
                    and request.decided_at is not None
                    and now - request.decided_at > NAME_DAYS * 86400
                ):
                    request.name = ""
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

    def add(
        self,
        entry: IndexEntry,
        name: str,
        status: str,
        note: str,
        bridges: Sequence[IndexEntry] = (),
    ) -> ListenerRequest:
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
                bridges=[song.display_name for song in bridges],
            )
            self.requests.append(request)
            self.save()
            return request

    def decide(
        self,
        request: ListenerRequest,
        status: str,
        note: str,
        bridges: Sequence[IndexEntry] = (),
    ) -> None:
        """Mark *request* queued or rejected; saved at once."""
        with self.lock:
            request.status, request.decided_at, request.note = status, self.clock(), note
            request.bridges = [song.display_name for song in bridges]
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


#: What a listener is told while the station is not taking requests.
CLOSED = Outcome(
    404,
    "Requests are closed",
    "The station is not taking requests right now. Please try again later.",
)


def request_settings(cfg: RequestsConfig) -> dict[str, Any]:
    """The settings the operator's Listener requests card shows and saves."""
    return {
        "enabled": cfg.enabled is True,
        "approval": cfg.approval,
        "bridges_enabled": cfg.bridges_enabled,
        "max_bridges": cfg.max_bridges,
        "bridge_cost_threshold": cfg.bridge_cost_threshold,
        "no_smooth_spot": cfg.no_smooth_spot,
        "daily_limit": cfg.daily_limit,
    }


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
        music_dir: Path | None,
        tokens: FormTokens,
        track_ids: TrackIds,
        monotonic: Callable[[], float] = time.monotonic,
        on_queued: Callable[[], object] = lambda: None,
        shows: Any = None,
    ) -> None:
        self.slug = slug
        # The station's ShowRunner (#106): a show on the air keeps its place
        # ahead of requests, and no request is promised a time it cannot keep.
        self.shows = shows
        # Called after a request joins the queue (refreshes the public feed).
        self._on_queued = on_queued
        self.cfg = cfg
        self.bridge = bridge
        self.book = book
        self.tokens = tokens
        self.track_ids = track_ids
        catalog = getattr(getattr(bridge, "library", None), "catalog", None)

        def music_roots() -> Iterable[Path | str]:
            return (
                (source["path"] for source in catalog.sources("music"))
                if catalog is not None
                else ()
            )

        self.blocklist = Blocklist(
            cfg.blocked_artists,
            cfg.blocked_folders,
            music_dir,
            music_roots=music_roots if music_dir is None else None,
        )
        prefix = cfg.ipv6_prefix
        self.searches = AddressLimiter(
            SEARCHES_PER_MINUTE, 60.0, clock=monotonic, ipv6_prefix=prefix
        )
        self.posts = AddressLimiter(
            POSTS_PER_TEN_MINUTES, 600.0, clock=monotonic, ipv6_prefix=prefix
        )
        self.accepted = AddressLimiter(
            cfg.per_listener, cfg.per_listener_minutes * 60.0, clock=monotonic, ipv6_prefix=prefix
        )
        # Tidies the book every TIDY_SECONDS (expiries, names past their
        # 30 days) even when no listener or operator touches it.
        self._closed = threading.Event()
        self._tidier = threading.Thread(
            target=self._tidy_now_and_then, name=f"autodj-requests-{slug}", daemon=True
        )
        track_station_thread(self._tidier, slug)
        self._tidier.start()
        self._lock = threading.Lock()
        self._monotonic = monotonic
        # Recent searches by their words, with the library snapshot they
        # were run on: (snapshot, when, results).
        self._searches: OrderedDict[str, tuple[object, float, list[tuple[str, IndexEntry]]]] = (
            OrderedDict()
        )
        self._search_lock = threading.Lock()

    def _tidy_now_and_then(self) -> None:
        while not self._closed.wait(TIDY_SECONDS):
            try:
                self.book.tidy_and_save()
            except OSError:
                logger.warning("Could not save the listener requests", exc_info=True)

    def close(self) -> None:
        """Stop tidying (the station is stopping)."""
        self._closed.set()
        self._tidier.join(timeout=5)

    # -- what listeners see -------------------------------------------------

    def search(self, query: str) -> list[tuple[str, IndexEntry]]:
        """Up to :data:`SEARCH_RESULTS` requestable songs matching every word of *query*.

        The same words within :data:`SEARCH_CACHE_SECONDS`, on the same
        library, are answered from memory.
        """
        tokens = query.lower().split()
        key = " ".join(tokens)
        indexed = self.bridge.search_index()
        now = self._monotonic()
        roots_changed = self.blocklist.refresh()
        with self._search_lock:
            if roots_changed:
                self._searches.clear()
            cached = self._searches.get(key)
            if (
                cached is not None
                and cached[0] is indexed
                and now - cached[1] < SEARCH_CACHE_SECONDS
            ):
                self._searches.move_to_end(key)
                return cached[2]
        found: list[tuple[str, IndexEntry]] = []
        for entry, text in indexed:
            if all(token in text for token in tokens) and not self.blocklist.blocks(entry):
                found.append((self.track_ids.shown(entry.path), entry))
                if len(found) >= SEARCH_RESULTS:
                    break
        with self._search_lock:
            self._searches[key] = (indexed, now, found)
            self._searches.move_to_end(key)
            while len(self._searches) > SEARCH_CACHE_SIZE:
                self._searches.popitem(last=False)
        return found

    def song(self, track_id: str) -> IndexEntry | None:
        """The requestable song a search showed under *track_id*, or ``None``."""
        path = self.track_ids.path_for(track_id)
        entry = self.bridge.entry_for_path(path) if path is not None else None
        if self.blocklist.refresh():
            with self._search_lock:
                self._searches.clear()
        if entry is None or self.blocklist.blocks(entry):
            return None
        return entry

    def _horizon(self) -> float:
        return self.cfg.window_minutes * 60.0

    def _similarity(self, a: str, b: str) -> float | None:
        return self.bridge.sim.similarity(a, b)

    def _show_on_air(self) -> str | None:
        """The name of the show on the air, or ``None``."""
        return self.shows.on_air_name() if self.shows is not None else None

    def _show_ahead(self) -> str | None:
        """A show on the air or due within the request window, or ``None``."""
        return self.shows.ahead(self._horizon()) if self.shows is not None else None

    def _after_show(self, slots: Sequence[RequestSlot]) -> list[RequestSlot]:
        """Only the places after the show's own waiting songs (#106): the show wins."""
        shows = self.shows
        if shows is None:
            return list(slots)
        last = max(
            (
                slot.position
                for slot in slots
                if slot.after is not None and shows.is_show_entry(slot.after)
            ),
            default=-1,
        )
        return [slot for slot in slots if slot.position > last]

    def _after_show_note(self, placement: Placement) -> Placement:
        """Say a placed request waits for the show on the air (#106)."""
        show = self._show_on_air()
        if show is None or placement.slot is None:
            return placement
        return replace(placement, note=f"After the show {show}. {placement.note}")

    def show_started(self, name: str) -> None:
        """A show went on the air ahead of the queued requests (#106): their notes say so.

        Requests still waiting in the queue now play after the show, so the
        operator's list stops promising where they fit.  Listeners were told
        only roughly, once, and are not told again.
        """
        waiting = {entry.path for entry in self.bridge.coming_up()}
        changed = False
        with self.book.lock:
            for request in self.book.recent():
                if (
                    request.status == QUEUED
                    and request.path in waiting
                    and not request.note.startswith("After the show")
                ):
                    request.note = f"After the show {name}. {request.note}".strip()
                    changed = True
            if changed:
                try:
                    self.book.save()
                except OSError:
                    logger.warning("Could not save the listener requests", exc_info=True)

    def _placement(self, entry: IndexEntry) -> Placement:
        """Where *entry* would go if approved now, without the bridge search.

        For the operator's list and a request that waits for approval: the
        list is read often, so the bridge search (up to six vector searches)
        runs only when a request is queued.
        """
        view = self.bridge._queue_view()
        slots = self.bridge.request_slots(entry, self._horizon(), view)
        return self._plan(entry, slots, view, search=False, operator=True)

    def _plan(
        self,
        entry: IndexEntry,
        slots: Sequence[RequestSlot],
        view: QueueView,
        *,
        search: bool = True,
        operator: bool = False,
    ) -> Placement:
        """The best place, a bridge into it, or what to do when no place is smooth.

        Only places after a show's own waiting songs count (#106): the show wins.

        Args:
            search: Look for bridge songs (outside every lock).
            operator: The operator is approving it, which uses the best
                legal place even when the station waits for a smooth one.
        """
        return self._after_show_note(
            self._plan_slots(entry, self._after_show(slots), view, search=search, operator=operator)
        )

    def _plan_slots(
        self,
        entry: IndexEntry,
        slots: Sequence[RequestSlot],
        view: QueueView,
        *,
        search: bool,
        operator: bool,
    ) -> Placement:
        chosen = place(entry, slots, self._similarity)
        slot = chosen.slot
        if slot is None:
            return chosen
        threshold = self.cfg.bridge_cost_threshold
        if chosen.cost <= threshold:
            return chosen
        bridging = self.cfg.bridges_enabled and slot.before is not None
        if bridging and not search:
            return Placement(
                slot,
                f"{chosen.note} It is far from the songs around it, so approving it looks for "
                f"up to {_counted(self.cfg.max_bridges, 'bridge song')} to lead into it first.",
                cost=chosen.cost,
            )
        if bridging:
            bridged = self._find_bridges(entry, slot, view)
            if bridged is not None:
                return bridged
        reason = f"No smooth spot within the next {_minutes(self.cfg.window_minutes)}."
        if self.cfg.no_smooth_spot == "wait" and not operator:
            return Placement(None, f"{reason} Keeping this request waiting.", ("no smooth spot",))
        how = "Approving it uses" if operator and not search else "Using"
        return Placement(
            slot, f"{chosen.note} {reason} {how} the best legal spot.", cost=chosen.cost
        )

    def _find_bridges(
        self, entry: IndexEntry, slot: RequestSlot, view: QueueView
    ) -> Placement | None:
        """Search at most three bounded nearest-neighbour pools, validating each projected play."""
        from dataclasses import replace

        sim = self.bridge.sim
        if not hasattr(sim, "bridge_candidates"):
            return None
        player = getattr(self.bridge, "player", None)
        is_liner = getattr(player, "_is_liner", None)
        before = slot.before
        assert before is not None
        current: IndexEntry = before
        used = {song.path for song in view.queue}
        used.update(song.path for song in (view.current, view.queued_next, entry) if song)
        used.update(request.path for request in self.book.pending())
        versions_reader = getattr(getattr(self.bridge, "player", None), "_song_versions", None)
        versions = versions_reader() if callable(versions_reader) else None
        if versions is not None:
            from autodj.repeat_rules import SongVersions

            if isinstance(versions, SongVersions):
                used = set(versions.with_versions(used))
        bridges: list[IndexEntry] = []
        next_steps: list[int | None] = []
        cursor: int | None = None
        direct = fit(current, entry, self._similarity).cost
        for _ in range(self.cfg.max_bridges):
            pool: dict[str, IndexEntry] = {}
            for song in (
                *sim.bridge_candidates(current.path, 24),
                *sim.bridge_candidates(entry.path, 24),
            ):
                if song.path in used:
                    continue
                live = self.bridge.entry_for_path(song.path)
                if (
                    live is not None
                    and not live.is_silent
                    and not self.blocklist.blocks(live)
                    and not (callable(is_liner) and is_liner(live) is True)
                ):
                    pool[live.path] = live
            ranked = sorted(
                pool.values(),
                key=lambda song: (
                    max(
                        fit(current, song, self._similarity).cost,
                        fit(song, entry, self._similarity).cost,
                    ),
                    fit(current, song, self._similarity).cost
                    + fit(song, entry, self._similarity).cost,
                ),
            )[:12]
            found: tuple[IndexEntry, RequestSlot] | None = None
            for candidate in ranked:
                first = fit(current, candidate, self._similarity).cost
                remaining = fit(candidate, entry, self._similarity).cost
                if first >= direct or remaining >= direct or first > self.cfg.bridge_cost_threshold:
                    continue
                synthetic = replace(
                    view,
                    queue=(*view.queue[: slot.position], *bridges, *view.queue[slot.position :]),
                )
                position = slot.position + len(bridges)
                options = self.bridge.request_slots(
                    candidate, self._horizon(), synthetic, cursor, only_position=position
                )
                if options and not options[0].problems:
                    found = candidate, options[0]
                    break
            if found is None:
                break
            candidate, candidate_slot = found
            bridges.append(candidate)
            next_steps.append(candidate_slot.clock_next)
            used.add(candidate.path)
            current = candidate
            direct = fit(current, entry, self._similarity).cost
            synthetic = replace(
                view, queue=(*view.queue[: slot.position], *bridges, *view.queue[slot.position :])
            )
            request_slots = self.bridge.request_slots(
                entry,
                self._horizon(),
                synthetic,
                candidate_slot.clock_next,
                only_position=slot.position + len(bridges),
            )
            if (
                direct <= self.cfg.bridge_cost_threshold
                and request_slots
                and not request_slots[0].problems
            ):
                final = request_slots[0]
                names = ", ".join(song.display_name for song in bridges)
                return Placement(
                    replace(slot, starts_in=final.starts_in, clock_next=final.clock_next),
                    f"Bridge to {entry.display_name} through {names}.",
                    cost=max(
                        fit(a, b, self._similarity).cost
                        for a, b in zip((before, *bridges), (*bridges, entry), strict=True)
                    ),
                    bridges=tuple(bridges),
                    bridge_clock_next=tuple(next_steps),
                )
            cursor = candidate_slot.clock_next
        return None

    def _queue_where_it_fits(
        self,
        entry: IndexEntry,
        *,
        operator: bool = False,
        voice_action: str | None = None,
        voice_revision: str | None = None,
    ) -> Placement:
        """Place *entry* and queue it there (:meth:`PlayerBridge.insert_request`).

        The place is checked against the queue as it is when the song is
        inserted, so it is never stale.

        Raises:
            QueueChanged: The queue kept changing meanwhile.
        """
        chosen: list[Placement] = []

        def choose(slots: list[RequestSlot], view: QueueView) -> RequestInsert | None:
            chosen.append(self._plan(entry, slots, view, operator=operator))
            placement = chosen[-1]
            return (
                RequestInsert(placement.slot, placement.bridges, placement.bridge_clock_next)
                if placement.slot is not None
                else None
            )

        self.bridge.insert_request(
            entry,
            self._horizon(),
            choose,
            with_view=True,
            voice_action=voice_action,
            voice_revision=voice_revision,
        )
        return chosen[-1]

    def _coming_up(self, entry: IndexEntry) -> bool:
        return any(e.path == entry.path for e in self.bridge.coming_up())

    def submit(self, track_id: str, name: str, address: str) -> Outcome:
        """Take one request from *address*; the form's token was checked already."""
        with self._lock:
            return self._submit(track_id, name, address)

    def _submit(self, track_id: str, name: str, address: str) -> Outcome:
        cfg = self.cfg
        if cfg.enabled is not True:
            # Turned off in Settings while this listener had the page open.
            return CLOSED
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
        from autodj.live_queue import VoiceQueueConflict

        try:
            placement = self._queue_where_it_fits(entry) if auto else self._placement(entry)
        except QueueChanged:
            return Outcome(
                503,
                "The station is busy",
                "The station's queue was changing. Please send the request again.",
                retry_after=5,
            )
        except VoiceQueueConflict:
            # Auto approval cannot make a voice choice on the listener's
            # behalf. Keep the request pending so the operator can decide.
            if len(self.book.pending()) >= cfg.max_pending:
                return Outcome(
                    429,
                    "Too many requests waiting",
                    "The station has many requests waiting already. Please try again later.",
                    retry_after=600,
                )
            note = (
                "Waiting for an operator: changing the queue would affect an upcoming voice track."
            )
            self.accepted.hit(address)
            self.book.add(entry, name, PENDING, note)
            logger.info("Listener request waiting for voice-track choice: %s", entry.display_name)
            return Outcome(
                200,
                "Request received",
                f"Thanks{_for(name)}. {_song(entry)} is waiting for an operator's choice "
                "because the change would affect an upcoming voice track.",
            )
        can_wait = placement.slot is None and (
            "no smooth spot" in placement.problems
            or any("clockwheel" in problem for problem in placement.problems)
        )
        if placement.slot is None and not can_wait:
            return Outcome(
                409,
                "Not right now",
                f"{_song(entry)} cannot play again so soon: the station keeps a gap between "
                "plays of a song, an artist and an album. Please try another song, or this one "
                "later.",
            )
        queue_now = auto and placement.slot is not None
        if not queue_now and len(self.book.pending()) >= cfg.max_pending:
            # Checked before the listener's quota is used: only accepted
            # requests count toward per_listener.
            return Outcome(
                429,
                "Too many requests waiting",
                "The station has many requests waiting already. Please try again later.",
                retry_after=600,
            )
        self.accepted.hit(address)
        if queue_now and placement.slot is not None:
            slot = placement.slot
            self._on_queued()
            request = self.book.add(entry, name, QUEUED, placement.note, placement.bridges)
            logger.info("Listener request queued: %s (%s)", entry.display_name, placement.note)
            # During a show, or with one about to start, the song waits for
            # the show's end: no time is promised.
            when = "after the station's show" if self._show_ahead() else rough_wait(slot.starts_in)
            return Outcome(
                200,
                "Request accepted",
                f"Thanks{_for(request.name)}. {_song(entry)} should play {when}.",
            )
        request = self.book.add(entry, name, PENDING, placement.note)
        logger.info("Listener request waiting: %s", entry.display_name)
        return Outcome(
            200,
            "Request received",
            f"Thanks{_for(request.name)}. The station will look at your request for "
            f"{_song(entry)}. If it is approved, it should play "
            + (
                "after the station's show."
                if self._show_ahead() is not None
                else "within the next hour or so."
            ),
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
                row["reason"] = row["fit"]
                row["can_approve"] = False
            else:
                placement = self._placement(entry)
                row["fit"] = placement.note
                row["reason"] = placement.note
                row["bridges"] = [song.display_name for song in placement.bridges]
                row["can_approve"] = placement.slot is not None
            pending.append(row)
        return {
            **request_settings(self.cfg),
            "today": self.book.count_today(),
            "pending": pending,
            "recent": [request.public() for request in self.book.recent()[:20]],
        }

    def _pending(self, request_id: str) -> ListenerRequest:
        """The waiting request with *request_id*; one past the 3-hour rule expires first.

        Raises:
            KeyError: No request with that id is waiting (or it just expired).
        """
        if not REQUEST_ID.fullmatch(request_id):
            raise KeyError(request_id)
        with self.book.lock:
            if self.book.tidy():
                self.book.save()
            request = self.book.get(request_id)
        if request is None or request.status != PENDING:
            raise KeyError(request_id)
        return request

    def _waiting(self, request_id: str) -> tuple[ListenerRequest, IndexEntry]:
        request = self._pending(request_id)
        entry = self.bridge.entry_for_path(request.path)
        if entry is None:
            raise LookupError("The song is no longer in the library.")
        return request, entry

    def approve(
        self,
        request_id: str,
        *,
        voice_action: str | None = None,
        voice_revision: str | None = None,
    ) -> ListenerRequest:
        """Queue a waiting request where it fits best.

        Raises:
            KeyError: No request with that id is waiting.
            LookupError: The song is gone from the library.
            ValueError: Every place within the window breaks a rule; says which.
        """
        with self._lock:
            request, entry = self._waiting(request_id)
            try:
                placement = self._queue_where_it_fits(
                    entry,
                    operator=True,
                    voice_action=voice_action,
                    voice_revision=voice_revision,
                )
            except QueueChanged as exc:
                raise ValueError(f"{exc} Try again.") from exc
            if placement.slot is None:
                raise ValueError(
                    f"{entry.display_name} cannot be queued within the next "
                    f"{_minutes(self.cfg.window_minutes)}: {_join(placement.problems)}."
                )
            self._on_queued()
            self.book.decide(request, QUEUED, placement.note, placement.bridges)
            return request

    def play_next(
        self,
        request_id: str,
        *,
        voice_action: str | None = None,
        voice_revision: str | None = None,
    ) -> ListenerRequest:
        """Queue a waiting request first, whatever fits; DMCA limits still hold.

        They hold for the queued songs after it too: a limit one of them
        would break only because of the request refuses it (#218).

        Raises:
            KeyError: No request with that id is waiting.
            LookupError: The song is gone from the library.
            ValueError: DMCA mode is on and playing it next would break a limit.
        """
        with self._lock:
            request, entry = self._waiting(request_id)
            # The first place, after the playing song and any "play next"
            # song; only DMCA limits stop it there.
            first: list[RequestSlot] = []

            def choose(slots: list[RequestSlot]) -> RequestSlot | None:
                first.append(slots[0])
                return (
                    None
                    if slots[0].dmca
                    or any("clockwheel" in problem for problem in slots[0].problems)
                    else slots[0]
                )

            try:
                self.bridge.insert_request(
                    entry,
                    0.0,
                    choose,
                    voice_action=voice_action,
                    voice_revision=voice_revision,
                )
            except QueueChanged as exc:
                raise ValueError(f"{exc} Try again.") from exc
            if first[-1].dmca:
                raise ValueError(
                    f"DMCA mode: playing this song next would mean {_join(first[-1].dmca)}."
                )
            if any("clockwheel" in problem for problem in first[-1].problems):
                raise ValueError("The next clockwheel step is not a matching music slot.")
            self._on_queued()
            self.book.decide(request, QUEUED, "Play next, chosen by the operator.")
            return request

    def reject(self, request_id: str) -> ListenerRequest:
        """Turn a waiting request down.

        Raises:
            KeyError: No request with that id is waiting.
        """
        with self._lock:
            request = self._pending(request_id)
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


def _counted(count: int, noun: str) -> str:
    words = ("no", "one", "two", "three")
    word = words[count] if 0 <= count < len(words) else str(count)
    return f"{word} {noun}" if count == 1 else f"{word} {noun}s"


def _minutes(minutes: int) -> str:
    if minutes % 60 == 0:
        hours = minutes // 60
        return "hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} minutes"


#: Made when the server starts, shared by every station (a token names its
#: station).  Every station's desk has its own TrackIds under this one key,
#: so a song has the same id on every station.
FORM_TOKENS = FormTokens()
TRACK_SECRET = secrets.token_bytes(32)
TRACK_IDS = TrackIds(TRACK_SECRET)


__all__ = [
    "FORM_TOKENS",
    "TRACK_IDS",
    "TRACK_SECRET",
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
    "limit_key",
    "place",
    "rough_wait",
]
