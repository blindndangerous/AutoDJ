"""The rotation rule tester: a dry run of a station's song picking (#125).

It answers questions such as "do long repeat rules starve the small rotation
categories?" before the rules go on air.  A simulation plays a span of time
(such as a week) or a number of songs through the station's own picker: the
clockwheel and its steps, the rotation categories and their days and hours,
the schedule's song-picking changes and their date ranges, the repeat rules
(related artists and related tracks included), the DMCA limits, every relaxation and the
fallbacks.  Nothing on air changes: the dry run picks with a copy of the
player that has its own configuration, play history, last-played times,
clock and random generator, and it never writes to the library.

What it does not reproduce:

- Time moves by each song's length from the catalog, from its saved start
  to its segue or outro in station (saved points) mode, less the crossfade
  otherwise; a song without a length counts as the library's average.
- A clockwheel liner adds its full length; automatic liners, time checks,
  carts and the user queue are left out.
- A scheduled change applies from the first pick after its time, so a
  "hard" event does not cut a song short.  Profile switches are not
  simulated; the report counts them.

The picks themselves go through :class:`~autodj.player.Player` unchanged,
so the dry run cannot drift from what the station does.
"""

from __future__ import annotations

import contextlib
import copy
import heapq
import logging
import math
import os
import random
import re
import sqlite3
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from autodj.categories import CategoryStore
from autodj.clip_dates import LinerDates
from autodj.player import Player, apply_repeat_windows
from autodj.repeat_rules import artist_key, prune_history

logger = logging.getLogger(__name__)

#: The longest span one run may cover, in hours (30 days).
MAX_HOURS = 30 * 24
#: The most songs one run may pick.
MAX_PICKS = 20_000
#: The span a run covers when neither a span nor a pick count is given.
DEFAULT_HOURS = 24.0
#: The longest a run may work, in seconds of real time; it then stops with
#: what it has, and the report says so.
MAX_RUN_SECONDS = 600.0
#: The earliest and latest start a run accepts (Unix seconds, 2000 to 2099).
EARLIEST_START = 946_684_800.0
LATEST_START = 4_102_444_800.0
# The pause after each pick, so the audio and web threads get the
# interpreter: a real sleep, which a zero-length one is not.
_PICK_PAUSE_S = 0.002
#: How many examples a list in the report gives.
SAMPLE = 10
# A pick that finds nothing is retried this much later (dead air).
_SILENCE_STEP_S = 60.0
# Seconds a song without a known length counts as, when the library has none.
_FALLBACK_LENGTH_S = 210.0

#: The rules a pick can break, in the order the report lists them.
RULES = (
    "same artist",
    "related artist",
    "same album",
    "same title",
    "related track",
    "same daypart",
    "same quarter hour",
    "track repeat window",
    "dayparting",
    "back to back",
)

APPROXIMATIONS = (
    "Time moves by each song's length from the catalog: its saved start to its segue or outro "
    "with saved-point transitions, less the crossfade otherwise; songs with no length count as "
    "the library's average.",
    "A clockwheel liner adds its full length; automatic liners, time checks, carts and the "
    "queue are left out.",
    "A scheduled change applies from the first pick after its time; hard events do not cut a "
    "song short, and profile switches are not simulated.",
)


class SimulationError(ValueError):
    """A request the tester refuses, or a station it cannot simulate."""


@dataclass(frozen=True)
class SimulationRequest:
    """What to simulate.

    Attributes:
        hours: The span to play, up to :data:`MAX_HOURS`.
        picks: Or the number of songs to pick, up to :data:`MAX_PICKS`.
            With neither, the run covers :data:`DEFAULT_HOURS`.
        start: When the run starts, in Unix seconds; ``None`` is now.
        seed: Seeds the random choices, so a run can be repeated exactly;
            ``None`` draws one, which the report gives.
    """

    hours: float | None = None
    picks: int | None = None
    start: float | None = None
    seed: int | None = None

    def __post_init__(self) -> None:
        """Refuse a span or count out of range, or both at once."""
        if self.hours is not None and self.picks is not None:
            raise SimulationError("Give a span or a number of songs, not both")
        if self.hours is not None and (
            isinstance(self.hours, bool)
            or not math.isfinite(self.hours)
            or not 0 < self.hours <= MAX_HOURS
        ):
            raise SimulationError(f"The span must be more than 0 and at most {MAX_HOURS} hours")
        if self.picks is not None and (
            isinstance(self.picks, bool)
            or not isinstance(self.picks, int)
            or not 1 <= self.picks <= MAX_PICKS
        ):
            raise SimulationError(f"The number of songs must be from 1 to {MAX_PICKS:,}")
        if self.seed is not None and (
            isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0
        ):
            raise SimulationError("The seed must be a whole number, 0 or more")
        if self.start is not None and (
            isinstance(self.start, bool)
            or not math.isfinite(self.start)
            or not EARLIEST_START <= self.start < LATEST_START
        ):
            raise SimulationError("The start must be between 1 January 2000 and 31 December 2099")


@dataclass
class SimulationSource:
    """The station a run copies: read, never changed.

    Attributes:
        player: The station's :class:`~autodj.player.Player`.  Its settings,
            play history, clockwheel and its place are copied.
        library: The shared :class:`~autodj.library.LibraryManager`.
        station: The station's name.
        ledger: The station's :class:`~autodj.station_ledger.StationLedger`,
            for when each song last played.
        events: The station's scheduled events.
        timezone: The station's time zone, which the schedule reads.
        liner_dates: The station's liner play dates file, if any.
    """

    player: Any
    library: Any
    station: str = "default"
    ledger: Any = None
    events: Sequence[Any] = ()
    timezone: str = "UTC"
    liner_dates: Path | None = None
    snapshot: PlayerSnapshot | None = None


#: The picker settings a run copies from the live player.
_COPIED = (
    "_preset",
    "_bpm_range",
    "_pure_shuffle",
    "_anchor_to_seed",
    "_seed_path",
    "_discovery_every",
)


@dataclass(frozen=True)
class PlayerSnapshot:
    """The live player's settings, history and clockwheel place at one moment."""

    cfg: Any
    sim: Any
    related_artists: Any
    settings: Mapping[str, Any]
    discovery_enabled: bool
    history: tuple[Any, ...]
    recent: tuple[str, ...]
    clock: Mapping[str, Any] | None
    clock_status: str | None
    liner_filter: Any
    station_paths: Any = None
    related_tracks: Any = None


def snapshot_player(player: Any) -> PlayerSnapshot:
    """Copy what a run needs from *player*, holding the locks its writers hold.

    Song-picking and clockwheel changes are swapped in under the player's
    settings lock (:func:`~autodj.player.settings_lock`), which only ever
    covers memory, so the copy waits a moment at most; the play history is
    guarded by the player's pick lock.  The server takes this copy in a
    worker thread, never on its event loop.
    """
    from autodj.player import settings_lock

    with settings_lock(player):
        cfg = copy.deepcopy(player._cfg)
        settings = {name: getattr(player, name) for name in _COPIED if hasattr(player, name)}
        clock = getattr(player, "_clock", None)
        # Deep, so nothing the copy holds (the restart minutes) is shared.
        snapshot_clock = copy.deepcopy(clock) if clock else None
        clock_status = getattr(player, "_clock_status", None)
        related = getattr(player, "_related_artists", None)
        related_tracks = getattr(player, "_related_tracks", None)
        liner_filter = getattr(player, "liner_filter", None)
        station_paths = getattr(player, "station_paths", None)
        discovery = bool(player._state.discovery_enabled)
    with player._pick_lock:
        history = tuple(player._state.play_history)
        recent = tuple(player._state.recently_played)
    return PlayerSnapshot(
        cfg=cfg,
        sim=player._sim,
        related_artists=related,
        settings=settings,
        discovery_enabled=discovery,
        history=history,
        recent=recent,
        clock=snapshot_clock,
        clock_status=clock_status,
        liner_filter=liner_filter,
        station_paths=station_paths,
        related_tracks=related_tracks,
    )


class LibrarySnapshot:
    """The music library read once from a read-only catalog, for ``autodj simulate``.

    It offers what the picker reads from the shared library (songs,
    liners, categories, Beets values, the station's plays) without the
    library manager, which opens the catalog for writing.  A categories
    file that cannot be read is skipped, never set aside.
    """

    def __init__(self, cfg: Any, catalog: Any, station: str) -> None:
        import faiss

        from autodj.catalog import normalize_path
        from autodj.categories import CategoryIndex
        from autodj.clock_condition_data import BeetsConditions
        from autodj.indexer import FEATURE_DIM
        from autodj.similarity import SimilarityIndex
        from autodj.station_ledger import StationLedger

        self.catalog = catalog
        self._normalize = normalize_path
        with catalog.reader() as reader:
            coverage = reader.coverage()
            self._music = reader.entries("music", coverage)
            self._liners = reader.entries("liner", coverage)
            by_path = {entry.path: entry for entry in self._music}
            vector_paths = [path for path in reader.vector_paths() if path in by_path]
            index = faiss.IndexFlatIP(FEATURE_DIM)
            for chunk in reader.vectors(set(vector_paths)):
                index.add(chunk)
            played = reader.last_played(station)
        self.sim = SimilarityIndex.empty()
        self.sim.replace_catalog_faiss([by_path[path] for path in vector_paths], index)
        self._by_path = {
            normalize_path(entry.path): entry for entry in (*self._music, *self._liners)
        }
        self.categories = CategoryIndex(cfg, self.entries_snapshot, lambda: self.catalog.sources())
        self.categories.store = _ReadOnlyCategoryStore(self.categories.store.path)
        self.clock_beets = BeetsConditions(cfg)
        self.ledger = StationLedger(catalog, station, self.entry_for_path)
        self.ledger.loaded(played)

    def entries_snapshot(self, kind: str = "music") -> tuple[Any, ...]:
        if kind == "music":
            return self._music
        return self._liners if kind == "liner" else ()

    def entry_for_path(self, path: str | Path) -> Any | None:
        return self._by_path.get(self._normalize(path))

    def record_for_id(self, record_id: str) -> dict | None:
        record = self.catalog.record_for_id(record_id)
        return record if record and self._normalize(record["path"]) in self._by_path else None


class _ReadOnlyCategoryStore(CategoryStore):
    """``categories.json``, only read: a file that cannot be read is skipped, never moved."""

    def _keep_bad_copy(self, reason: str) -> None:
        logger.warning("Rotation test: categories file %s skipped: %s", self.path, reason)


class _ReadOnlyLinerDates(LinerDates):
    """``liner_dates.json``, only read: a file that cannot be read is skipped, never moved."""

    def _keep_bad_copy(self, reason: str) -> None:
        logger.warning("Rotation test: liner dates file %s skipped: %s", self.path, reason)


@contextlib.contextmanager
def open_station(cfg: Any, station: str = "default") -> Iterator[SimulationSource]:
    """*station* as saved on disk, for a run outside the server (``autodj simulate``).

    Settings come from the configuration and the web page's saved
    settings, plays and the clockwheel place from the library, events from
    the schedule file.  The catalog is opened read-only (see
    :class:`~autodj.catalog.ReadOnlyCatalog`), so a running server keeps
    its write lock, nothing is migrated and ``library.db`` itself is never
    written; SQLite may still create the ``-wal`` and ``-shm`` files beside
    it, as any reader does.  It closes when the block ends.

    Raises:
        SimulationError: There is no library yet, it cannot be read (a
            damaged file, one that is not an AutoDJ library, or one left
            half written), or the schedule file cannot be read.
    """
    from autodj._bridge import PlayerBridge
    from autodj.catalog import ReadOnlyCatalog
    from autodj.runtime_state import load_into_bridge
    from autodj.scheduler import system_timezone
    from autodj.stations import DEFAULT_SLUG, StationPaths

    root = cfg
    if station == DEFAULT_SLUG:
        paths = StationPaths.default(cfg)
    else:
        from autodj.config import station_config

        spec = next((item for item in cfg.stations if item.slug == station), None)
        if spec is None:
            configured = ", ".join(item.slug for item in cfg.stations) or "none besides default"
            raise SimulationError(
                f"No station named {station} is configured. Stations: {configured}."
            )
        cfg = station_config(spec.cfg, root)
        paths = StationPaths.for_station(cfg, station)
    active = Path(root.index.active_dir)
    if not (active / "library.db").is_file():
        raise SimulationError(
            f"No music library in {active}. Start autodj serve or run autodj index first."
        )
    events = _read_events(paths.schedule)
    database = active / "library.db"
    try:
        catalog = ReadOnlyCatalog(database)
    except sqlite3.Error as exc:
        raise SimulationError(_unreadable(database, exc)) from exc
    try:
        try:
            library = LibrarySnapshot(cfg, catalog, station)
        except sqlite3.Error as exc:
            raise SimulationError(_unreadable(database, exc)) from exc
        ledger = library.ledger
        sim = library.sim
        player = Player(cfg, sim, dry_run=True)
        player._library = library
        player._ledger = ledger
        player.station_paths = paths
        bridge = PlayerBridge(player=player, sim=sim, library=library, ledger=ledger, paths=paths)
        load_into_bridge(bridge, paths.state_dir)
        player.restore_repeat_history(ledger)
        player.configure_clock()
        yield SimulationSource(
            player=player,
            library=library,
            station=station,
            ledger=ledger,
            events=events,
            timezone=cfg.server.station_timezone or system_timezone(),
            liner_dates=paths.liner_dates,
        )
    finally:
        catalog.close()


def _unreadable(database: Path, exc: sqlite3.Error) -> str:
    """Why the library cannot be tested, in words, with SQLite's own reason."""
    return (
        f"The music library {database} cannot be read ({exc}). It may be damaged, not an "
        "AutoDJ library, or left half written; start autodj serve once to check it."
    )


def _read_events(path: Path) -> list[Any]:
    """The events in the schedule file *path*; none when there is no file."""
    from autodj.scheduler import ScheduleFile

    if not path.exists():
        return []
    try:
        return list(ScheduleFile.model_validate_json(path.read_bytes()).events)
    except (OSError, ValueError) as exc:
        reason = str(exc).splitlines()[0]
        raise SimulationError(f"The schedule file {path} cannot be read: {reason}") from exc


# ---------------------------------------------------------------------------
# Quiet logs: the picker explains each relaxation in the log, which a dry
# run of thousands of picks would flood.  Records logged on a simulation
# thread are dropped; the report says the same things, counted.
# ---------------------------------------------------------------------------

_quiet = threading.local()
_QUIET_LOGGERS = (
    "autodj.player",
    "autodj.player.repeat",
    "autodj.similarity",
    "autodj.categories",
    "autodj.presets",
    "autodj._bridge",
    "autodj.repeat_rules",
    "autodj.related_artist_file",
)


class _QuietFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(_quiet, "active", False)


_QUIET_FILTER = _QuietFilter()
for _name in _QUIET_LOGGERS:
    logging.getLogger(_name).addFilter(_QUIET_FILTER)


@contextlib.contextmanager
def _quiet_logs() -> Iterator[None]:
    previous = getattr(_quiet, "active", False)
    _quiet.active = True
    try:
        yield
    finally:
        _quiet.active = previous


def lower_thread_priority() -> None:
    """Run the calling thread below normal priority, where the system allows it.

    Audio and the web server keep their place: a long run only uses the
    time they leave.  Below normal, not idle, so a run still finishes on a
    busy computer.
    """
    try:
        if sys.platform == "win32":
            import ctypes

            kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009 -- Windows only
            thread_priority_below_normal = -1
            kernel32.SetThreadPriority(kernel32.GetCurrentThread(), thread_priority_below_normal)
        elif hasattr(os, "setpriority") and sys.platform.startswith("linux"):
            # On Linux each thread has its own nice value.
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
    except (OSError, AttributeError):  # pragma: no cover - priority is a courtesy
        logger.debug("Could not lower the rotation test's priority", exc_info=True)


# ---------------------------------------------------------------------------
# The copy of the player
# ---------------------------------------------------------------------------


class _SimLedger:
    """The dry run's own last-played times, starting from the station's; writes nothing."""

    def __init__(self, real: Any, station: str, start: float) -> None:
        from autodj.catalog import normalize_path

        self._normalize = normalize_path
        self.station = station
        self._real = real
        played = real.last_played() if real is not None else {}
        self._played: dict[str, float] = {
            path: stamp for path, stamp in played.items() if stamp <= start
        }

    def setting_key(self, name: str) -> str:
        from autodj.station_ledger import setting_key

        return setting_key(self.station, name)

    def last_played(self) -> dict[str, float]:
        # The picks only read it, so the dry run hands out its own dict
        # rather than a copy per pick.
        return self._played

    def remember_play(self, path: str, timestamp: float | None = None) -> None:
        self._played[self._normalize(path)] = float(timestamp or 0.0)

    def remember_setting(self, name: str, value: Any) -> None:
        """The dry run's clockwheel place is its own; nothing is saved."""

    def pending_history(self) -> list[dict]:
        return []

    def get_setting(self, name: str, default: Any = None) -> Any:
        """The station's saved setting; the dry run keeps its own clock place in memory."""
        return self._real.get_setting(name, default) if self._real is not None else default


class _SimPlayer(Player):
    """A :class:`Player` whose clock is the dry run's."""

    _sim_now: float = 0.0

    def _now(self) -> float:
        return self._sim_now


def _copy_player(
    source: SimulationSource,
    snapshot: PlayerSnapshot,
    start: float,
    rng: random.Random,
    observer: Callable[[str, Any], None],
) -> _SimPlayer:
    """A dry-run player with the snapshot's settings, history and clockwheel place."""
    player = _SimPlayer(copy.deepcopy(snapshot.cfg), snapshot.sim, dry_run=True)
    # Its own settings lock: nothing the dry run changes waits on, or holds,
    # the live stations' lock.
    player.settings_lock = threading.RLock()
    # The station's own folders: another station keeps its own clockwheels.
    player.station_paths = snapshot.station_paths
    player._sim_now = start
    player._state.clock = lambda: player._sim_now
    if snapshot.related_artists is not None:
        player._related_artists = snapshot.related_artists
    if snapshot.related_tracks is not None:
        player._related_tracks = snapshot.related_tracks
    for name, value in snapshot.settings.items():
        setattr(player, name, value)
    player._state.discovery_enabled = snapshot.discovery_enabled
    player._library = source.library
    player._ledger = _SimLedger(source.ledger, source.station, start)
    player._random = rng
    player.pick_observer = observer
    player.liner_filter = _liner_filter(source, snapshot, player)
    apply_repeat_windows(player)
    records = list(snapshot.history)
    recent = list(snapshot.recent)
    if records and records[-1].started > start:
        # A run that starts before the latest play knows only the plays before it.
        records = [record for record in records if record.started <= start]
        recent = [record.path for record in records]
    state = player._state
    state.play_history = deque(records)
    state.recently_played = deque(recent, maxlen=state.recently_played.maxlen)
    prune_history(state.play_history, state.rules, start, state.dmca_seconds)
    clock = snapshot.clock
    if clock and clock.get("name"):
        player.activate_clock(clock["name"], clock.get("cursor", 0))
    else:
        player._clock = None
        player._clock_status = snapshot.clock_status or "No clockwheel is active"
    return player


def _liner_filter(source: SimulationSource, snapshot: PlayerSnapshot, player: _SimPlayer) -> Any:
    """The clockwheel's liner steps judge play dates by the dry run's day.

    The dates file is only read: one that cannot be read lets every liner
    play, and stays where it is.
    """
    if source.liner_dates is None:
        return snapshot.liner_filter
    from autodj.categories import station_wall
    from autodj.clip_dates import LinerGate

    library = source.library

    def liner_id(path: str) -> str:
        record = library.catalog.record_for_path(path) if library is not None else None
        return str(record["id"]) if record else Path(path).name

    gate = LinerGate(
        _ReadOnlyLinerDates(source.liner_dates),
        lambda: station_wall(player._cfg, player._sim_now).date(),
        liner_id,
        lambda at: station_wall(player._cfg, at).date(),
    )
    return gate.playable


# ---------------------------------------------------------------------------
# The schedule
# ---------------------------------------------------------------------------

# Actions that change how songs are picked, and so are simulated.
_PICKING_ACTIONS = frozenset({"pick_mode", "preset", "discovery"})
_UNSIMULATED_ACTIONS = frozenset({"profile"})


class _Schedule:
    """The station's enabled events that change song picking, in time order."""

    def __init__(self, events: Sequence[Any], timezone: str, start: float) -> None:
        from autodj.scheduler import next_run

        self._next_run = next_run
        self.zone = ZoneInfo(timezone)
        self._due: list[tuple[float, int, Any]] = []
        after = datetime.fromtimestamp(start, UTC)
        for number, event in enumerate(events):
            if not getattr(event, "enabled", False):
                continue
            if event.action not in _PICKING_ACTIONS | _UNSIMULATED_ACTIONS:
                continue
            when = self._after(event, after)
            if when is not None:
                heapq.heappush(self._due, (when.timestamp(), number, event))

    def _after(self, event: Any, moment: datetime) -> datetime | None:
        """*event*'s next run after *moment*; none when its rule cannot be worked out."""
        try:
            following: datetime | None = self._next_run(event, moment, self.zone)
        except (ValueError, OverflowError, OSError):
            return None
        return following

    def due(self, now: float) -> list[tuple[float, Any]]:
        """Every run at or before *now*, in order; each event's next run is lined up."""
        found: list[tuple[float, Any]] = []
        while self._due and self._due[0][0] <= now:
            when, number, event = heapq.heappop(self._due)
            found.append((when, event))
            following = self._after(event, datetime.fromtimestamp(when, UTC))
            if following is not None:
                heapq.heappush(self._due, (following.timestamp(), number, event))
        return found


def _apply_event(player: _SimPlayer, bridge: Any, event: Any) -> str | None:
    """Make *event*'s change on the dry-run player; why not, in words, when it cannot.

    An event the player refuses (a mode or preset that no longer exists)
    is a refusal in the report, never the end of the run, and it changes
    nothing: what the event had changed before it failed is put back.
    """
    own = vars(player)
    before = {name: own[name] for name in _EVENT_STATE if name in own}
    playback = copy.deepcopy(player._cfg.playback)
    discovery = player._state.discovery_enabled
    try:
        refusal = _make_change(player, bridge, event)
    except Exception as exc:
        refusal = f"it could not be applied: {exc}"
    if refusal:
        for name in _EVENT_STATE:
            if name in before:
                own[name] = before[name]
            else:
                own.pop(name, None)
        player._cfg.playback = playback
        player._state.discovery_enabled = discovery
    return refusal


# What a scheduled change can touch on the dry-run player, put back when it fails.
_EVENT_STATE = (
    "_clock",
    "_clock_slots",
    "_clock_status",
    "_clock_next_boundary",
    "_seed_clock_following",
    "_pending_clock_cursor",
    "_pending_clock_name",
    "_seed_clock_name",
    "_pending_clock_boundary",
    "_pure_shuffle",
    "_preset",
    "_discovery_every",
)


def _make_change(player: _SimPlayer, bridge: Any, event: Any) -> str | None:
    params = event.params
    if event.action == "pick_mode":
        from autodj.settings_bodies import PlaybackSettingsBody

        mode = str(params.get("mode", ""))
        if mode.startswith("clock:"):
            name = mode.removeprefix("clock:")
            # Choosing the clockwheel already playing keeps its place.
            if (player._clock or {}).get("name") != name:
                player.activate_clock(name, 0)
                player._pending_clock_cursor = None
                player._pending_clock_boundary = None
            mode = "clock"
        if mode == "clock" and player._clock is None:
            return f"the clockwheel cannot play: {player._clock_status}"
        bridge.set_playback_settings(PlaybackSettingsBody.model_validate({"pick_mode": mode}))
        return None
    if event.action == "preset":
        bridge.set_preset(str(params.get("name", "")))
        return None
    if event.action == "discovery":
        enabled = params.get("state") == "on"
        if enabled and player._discovery_every is None:
            return "no discovery rate is set"
        player._state.discovery_enabled = enabled
        return None
    return "profile switches are not simulated"


# ---------------------------------------------------------------------------
# What the run saw
# ---------------------------------------------------------------------------

_ALL_MATCHING = re.compile(r"all \d[\d,]* matching entries")


def _step_key(reason: str) -> str:
    """A skipped-step reason without the count that changes from pick to pick."""
    return _ALL_MATCHING.sub("all matching entries", re.sub(r"^Step \d+: ", "", reason))


def _dmca_limit(text: str) -> str:
    """The kind of DMCA limit *text* (from :meth:`Complement.broken`) names."""
    in_a_row = "in a row" in text
    if "box set" in text:
        kind = "box set or compilation"
    elif "from the album" in text:
        kind = "album"
    else:
        kind = "artist"
    return f"{kind}, {'in a row' if in_a_row else 'in three hours'}"


@dataclass
class _Tally:
    """Counts made while the run picks."""

    plays: Counter[str] = field(default_factory=Counter)
    liners: int = 0
    modes: Counter[str] = field(default_factory=Counter)
    broken: Counter[str] = field(default_factory=Counter)
    ignored: Counter[str] = field(default_factory=Counter)
    steps: Counter[str] = field(default_factory=Counter)
    dmca_replaced: int = 0
    dmca_broken: int = 0
    dmca_limits: Counter[str] = field(default_factory=Counter)
    dmca_examples: list[dict[str, Any]] = field(default_factory=list)
    failed_picks: list[dict[str, Any]] = field(default_factory=list)
    failed_count: int = 0
    silent_seconds: float = 0.0
    silences: list[dict[str, Any]] = field(default_factory=list)
    schedule_applied: list[dict[str, Any]] = field(default_factory=list)
    schedule_count: int = 0
    schedule_refused: Counter[str] = field(default_factory=Counter)
    pending_skips: list[str] = field(default_factory=list)

    def observe(self, event: str, detail: Any) -> None:
        if event == "clock_skipped":
            self.pending_skips = list(detail)
        elif event == "dmca_replaced":
            self.dmca_replaced += 1


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class Progress:
    """How far a run has got, for a progress bar: songs picked and the share done."""

    picks: int = 0
    fraction: float = 0.0


def average_length(entries: Iterable[Any]) -> float:
    """The average length of *entries* that have one: what a song without a length counts as."""
    lengths = [entry.length for entry in entries if entry.length > 0]
    return sum(lengths) / len(lengths) if lengths else _FALLBACK_LENGTH_S


def song_seconds(player: Player, entry: Any, average: float) -> float:
    """How long *entry* plays before the next song starts, as a dry run times it.

    From its saved start to its segue or outro with saved-point
    transitions, its length less the crossfade otherwise; *average* when
    that comes to a second or less.  The hour preview times its songs the
    same way (:mod:`autodj.hour_preview`).
    """
    playback = player._cfg.playback
    if playback.transition_mode == "saved_points":
        begin, segue = player._clock_music_span(entry)
        seconds = segue - begin
    else:
        crossfade = float(getattr(playback, "crossfade_seconds", 0.0) or 0.0)
        seconds = (entry.length or 0.0) - crossfade
    return seconds if seconds > 1.0 else average


def simulate(
    source: SimulationSource,
    request: SimulationRequest,
    *,
    progress: Callable[[Progress], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    budget: float = MAX_RUN_SECONDS,
    on_pick: Callable[[float, Any], None] | None = None,
) -> dict[str, Any]:
    """Run *request* against a copy of *source*'s picker; return the report (JSON shape).

    See :func:`report_text` for the same report in words.  *progress* is
    told how far the run has got about every hundred picks.  The run
    checks before every pick whether *cancelled* returns ``True`` or it
    has worked *budget* seconds, and then stops with what it has; the
    report says which.  *on_pick* hears each song or liner as it starts,
    with its start time.

    Raises:
        SimulationError: The library has no songs.
    """
    with _quiet_logs():
        return _simulate(source, request, progress, cancelled, budget, on_pick)


def _simulate(
    source: SimulationSource,
    request: SimulationRequest,
    progress: Callable[[Progress], None] | None,
    cancelled: Callable[[], bool] | None,
    budget: float,
    on_pick: Callable[[float, Any], None] | None,
) -> dict[str, Any]:
    began = time.perf_counter()
    start = request.start if request.start is not None else time.time()
    seed = request.seed if request.seed is not None else random.SystemRandom().randrange(10**9)
    rng = random.Random(seed)
    tally = _Tally()
    snapshot = source.snapshot or snapshot_player(source.player)
    player = _copy_player(source, snapshot, start, rng, tally.observe)
    library = source.library
    entries = tuple(entry for entry in player.playable_entries() if not entry.is_silent)
    if not entries:
        raise SimulationError("The library has no songs to pick")
    liner_paths = {entry.path for entry in library.entries_snapshot("liner")}
    hours = request.hours if request.picks is None else None
    if hours is None and request.picks is None:
        hours = DEFAULT_HOURS
    limit = request.picks if request.picks is not None else MAX_PICKS
    end = start + (hours if hours is not None else MAX_HOURS) * 3600.0
    average = average_length(entries)
    from autodj._bridge import PlayerBridge

    bridge = PlayerBridge(player=player, sim=player._sim, library=library, ledger=player._ledger)
    schedule = _Schedule(source.events, source.timezone, start)
    initial_mode = "random" if player._pure_shuffle else player._cfg.playback.pick_mode
    initial_clock = (player._clock or {}).get("name") if initial_mode == "clock" else None
    categories = player._category_members() or {}

    def station_mode() -> bool:
        return player._cfg.playback.transition_mode == "saved_points"

    def duration(entry: Any) -> float:
        return song_seconds(player, entry, average)

    def when(stamp: float) -> str:
        return datetime.fromtimestamp(stamp, schedule.zone).isoformat(timespec="minutes")

    def apply_schedule(now: float) -> None:
        for at, event in schedule.due(now):
            refusal = _apply_event(player, bridge, event)
            tally.schedule_count += 1
            if refusal:
                tally.schedule_refused[f"{event.name}: {refusal}"] += 1
            if len(tally.schedule_applied) < SAMPLE * 2:
                tally.schedule_applied.append(
                    {
                        "time": when(at),
                        "event": event.name,
                        "action": _event_words(event),
                        "result": refusal or "applied",
                    }
                )

    def start_fresh(now: float) -> Any | None:
        """A first song, as the station picks one when a set starts."""
        player._sim_now = now
        player._seed_clock_cursor = None
        player._seed_clock_name = None
        try:
            # A first song starts as it is picked, as the live start pick does.
            context = player._pick_context(at=now)
            entry = player._random_start_entry()
        except Exception:
            return None
        # As the render cursor takes a set's first song: its clock place and
        # the clockwheel that place is in (#133).
        player._pending_clock_cursor = player._seed_clock_cursor
        player._pending_clock_name = player._seed_clock_name
        if entry is not None and entry.path not in liner_paths:
            enforced = _step_enforces_rules(player, player._cfg.playback.pick_mode)
            _tally_rules(tally, context, entry, enforced, when(now))
        return entry

    def record(entry: Any, now: float) -> None:
        state = player._state
        state.record_played(entry)
        state.track_number += 1
        player._previous_track = state.current_track
        state.current_track = entry
        player._ledger.remember_play(entry.path, now)
        if on_pick is not None:
            on_pick(now, entry)

    def stopping() -> str | None:
        if cancelled is not None and cancelled():
            return "cancelled"
        if time.perf_counter() - began > budget:
            return "time limit"
        return None

    stopped: str | None = None
    now = start
    apply_schedule(now)
    current = start_fresh(now)
    while current is None and now < end and not (stopped := stopping()):
        if len(tally.silences) < SAMPLE:
            tally.silences.append({"time": when(now), "reason": player._clock_status})
        tally.silent_seconds += _SILENCE_STEP_S
        now += _SILENCE_STEP_S
        apply_schedule(now)
        current = start_fresh(now)
    picks = 0
    if current is not None:
        record(current, now)
        if current.path in liner_paths:
            tally.liners += 1
        else:
            picks += 1
            tally.plays[current.path] += 1
            tally.modes["start"] += 1
    reported = 0
    while current is not None and picks < limit:
        stopped = stopping()
        if stopped:
            break
        handoff = now + duration(current)
        if handoff >= end:
            break
        player._sim_now = now
        apply_schedule(now)
        tally.pending_skips = []
        # As the station does, dayparting reads the hours at the song's start.
        context = player._pick_context(at=handoff)
        liners: tuple[tuple[Any, bool], ...] = ()
        try:
            if station_mode():
                entry, mode, liners, cursor, _key = player._choose_station_next(current, context)
                player._pending_clock_cursor = cursor
            else:
                entry, mode = player._choose_next(current, context)
        except Exception as exc:
            tally.failed_count += 1
            if len(tally.failed_picks) < SAMPLE:
                tally.failed_picks.append({"time": when(handoff), "reason": str(exc)})
            # The song plays out alone and the station starts afresh.
            now = handoff
            fresh = start_fresh(now)
            while fresh is None and now < end and not (stopped := stopping()):
                if len(tally.silences) < SAMPLE:
                    tally.silences.append({"time": when(now), "reason": str(exc)})
                tally.silent_seconds += _SILENCE_STEP_S
                now += _SILENCE_STEP_S
                apply_schedule(now)
                fresh = start_fresh(now)
            if fresh is None:
                break
            current = fresh
            record(current, now)
            if current.path in liner_paths:
                tally.liners += 1
            else:
                picks += 1
                tally.plays[current.path] += 1
                tally.modes["start"] += 1
            continue
        for step in tally.pending_skips:
            tally.steps[_step_key(step)] += 1
        enforced = _step_enforces_rules(player, mode)
        _tally_rules(tally, context, entry, enforced, when(handoff))
        now = handoff
        for liner, _queued in liners:
            player._ledger.remember_play(liner.path, now)
            if on_pick is not None:
                on_pick(now, liner)
            tally.liners += 1
            now += max(liner.length or 0.0, 0.0)
        record(entry, now)
        current = entry
        picks += 1
        tally.plays[entry.path] += 1
        tally.modes[mode] += 1
        # Let the audio and web threads in between picks.
        time.sleep(_PICK_PAUSE_S)
        if progress is not None and picks - reported >= 100:
            reported = picks
            share = picks / limit if request.picks is not None else (now - start) / (end - start)
            progress(Progress(picks, min(1.0, share)))
    if progress is not None:
        progress(Progress(picks, 1.0))
    return _report(
        source=source,
        request=request,
        player=player,
        tally=tally,
        entries=entries,
        categories=categories,
        seed=seed,
        start=start,
        finish=now,
        zone=schedule.zone,
        initial_mode=initial_mode,
        initial_clock=initial_clock,
        hours=hours,
        stopped=stopped,
        elapsed=time.perf_counter() - began,
    )


def _step_enforces_rules(player: _SimPlayer, mode: str) -> bool:
    """Whether the clockwheel step that made the latest pick holds it to the repeat rules."""
    if mode != "clock" or player._clock is None:
        return True
    slots = getattr(player, "_clock_slots", None) or ()
    after = player._pending_clock_cursor
    if not slots or after is None:
        return True
    slot = slots[(after - 1) % len(slots)]
    return bool(getattr(slot, "enforce_rules", True))


def _tally_rules(tally: _Tally, context: Any, entry: Any, enforced: bool, at: str) -> None:
    """Count the rules *entry* breaks against the history the pick saw."""
    broken: list[str] = []
    why = context.repeat.reason(entry)
    if why:
        broken.append(why)
    if entry.path in set(context.recent):
        broken.append("track repeat window")
    target = tally.broken if enforced else tally.ignored
    for rule in broken:
        target[rule] += 1
    if entry.path in context.closed:
        tally.broken["dayparting"] += 1
    if entry.path in context.protected:
        tally.broken["back to back"] += 1
    if context.complement is not None:
        limits = context.complement.broken(entry)
        for text in limits:
            tally.dmca_limits[_dmca_limit(text)] += 1
        tally.dmca_broken += bool(limits)
        if limits and len(tally.dmca_examples) < SAMPLE:
            tally.dmca_examples.append(
                {"time": at, "song": entry.display_name, "limits": list(limits)}
            )


def _event_words(event: Any) -> str:
    params = event.params
    if event.action == "pick_mode":
        mode = str(params.get("mode", ""))
        if mode.startswith("clock:"):
            return f"song picking: clockwheel {mode.removeprefix('clock:')}"
        return f"song picking: {_MODE_WORDS.get(mode, mode)}"
    if event.action == "preset":
        return f"preset {params.get('name', '')}"
    if event.action == "discovery":
        return f"discovery {params.get('state', '')}"
    return f"profile {params.get('name', '')}"


_MODE_WORDS = {
    "similarity": "similar songs",
    "random": "random",
    "least_recent": "least recently played",
    "clock": "clockwheel",
    "fallback": "fallback to a random song",
    "start": "fresh start",
    "pure_shuffle": "random",
    "discovery": "discovery",
    "anchored": "anchored to the seed",
    "queue": "queue",
}


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _spread(values: Sequence[int]) -> dict[str, float]:
    if not values:
        return {"min": 0, "max": 0, "average": 0.0}
    return {
        "min": min(values),
        "max": max(values),
        "average": round(sum(values) / len(values), 2),
    }


def _report(
    *,
    source: SimulationSource,
    request: SimulationRequest,
    player: _SimPlayer,
    tally: _Tally,
    entries: Sequence[Any],
    categories: Mapping[str, tuple[str, Iterable[str]]],
    seed: int,
    start: float,
    finish: float,
    zone: ZoneInfo,
    initial_mode: str,
    initial_clock: str | None,
    hours: float | None,
    stopped: str | None,
    elapsed: float,
) -> dict[str, Any]:
    plays = tally.plays
    by_path = {entry.path: entry for entry in entries}
    # With Count versions as one song, a song's versions share one row, named
    # by its first version; otherwise each file is a song.
    versions = player._song_versions()
    songs: dict[str, tuple[Any, int]] = {}
    for entry in entries:
        song = versions.song(entry.path) if versions is not None else entry.path
        first, count = songs.get(song, (entry, 0))
        songs[song] = (first, count + plays.get(entry.path, 0))
    song_counts = [count for _first, count in songs.values()]
    never = [first for first, count in songs.values() if not count]
    most = sorted(
        ((first, count) for first, count in songs.values() if count),
        key=lambda pair: (-pair[1], pair[0].path),
    )[:5]

    artists: dict[str, tuple[str, int]] = {}
    for entry in entries:
        key = artist_key(entry.artist or "") or "(no artist)"
        name, count = artists.get(key, (entry.artist or "(no artist)", 0))
        artists[key] = (name, count + plays.get(entry.path, 0))
    artist_counts = [count for _name, count in artists.values()]
    top_artists = sorted(artists.values(), key=lambda pair: (-pair[1], pair[0]))[:5]

    category_rows = []
    for category_id, (name, members) in categories.items():
        member_entries = [by_path[path] for path in members if path in by_path]
        counts = [plays.get(entry.path, 0) for entry in member_entries]
        unplayed = [entry for entry in member_entries if not plays.get(entry.path)]
        label = f"category {name}:"
        category_rows.append(
            {
                "id": category_id,
                "name": name,
                "songs": len(member_entries),
                "plays": sum(counts),
                **_spread(counts),
                "never_played": len(unplayed),
                "never_played_sample": sorted(entry.display_name for entry in unplayed)[:SAMPLE],
                "skipped": sum(
                    count
                    for reason, count in tally.steps.items()
                    if re.sub(r"^Step \d+: ", "", reason).startswith(label)
                ),
            }
        )

    def iso(stamp: float) -> str:
        return datetime.fromtimestamp(stamp, zone).isoformat(timespec="minutes")

    clock = player._clock or {}
    return {
        "station": source.station,
        "seed": seed,
        "timezone": str(zone),
        "start": iso(start),
        "end": iso(finish),
        "requested": {"hours": hours} if hours is not None else {"picks": request.picks},
        "stopped_early": bool(stopped),
        "stopped_reason": stopped,
        "elapsed_seconds": round(elapsed, 2),
        "song_picking": {
            "mode": initial_mode,
            "clockwheel": initial_clock,
            "final_mode": "random" if player._pure_shuffle else player._cfg.playback.pick_mode,
            "final_clockwheel": clock.get("name"),
            "dmca": bool(player._state.dmca_enabled),
            "versions_as_one_song": bool(player._state.rules.versions_as_one_song),
        },
        "picks": sum(plays.values()),
        "liners": tally.liners,
        "modes": dict(tally.modes.most_common()),
        "songs": {
            "library": len(songs),
            "played": len(songs) - len(never),
            **_spread(song_counts),
            "never_played": len(never),
            "never_played_sample": sorted(entry.display_name for entry in never)[:SAMPLE],
            "most_played": [{"song": first.display_name, "plays": count} for first, count in most],
        },
        "artists": {
            "library": len(artists),
            "played": sum(1 for count in artist_counts if count),
            **_spread(artist_counts),
            "never_played": sum(1 for count in artist_counts if not count),
            "most_played": [
                {"artist": name, "plays": count} for name, count in top_artists if count
            ],
        },
        "categories": category_rows,
        "relaxed": {rule: tally.broken[rule] for rule in RULES if tally.broken[rule]},
        "ignored_by_steps": {rule: tally.ignored[rule] for rule in RULES if tally.ignored[rule]},
        "fallbacks": tally.modes.get("fallback", 0),
        "skipped_steps": [
            {"reason": reason, "count": count} for reason, count in tally.steps.most_common()
        ],
        "dmca": {
            "enabled": bool(player._state.dmca_enabled),
            "replaced": tally.dmca_replaced,
            "broken": tally.dmca_broken,
            "limits": dict(tally.dmca_limits.most_common()),
            "examples": tally.dmca_examples,
        },
        "dead_air": {
            "failed_picks": tally.failed_count,
            "failed_examples": tally.failed_picks,
            "silent_minutes": round(tally.silent_seconds / 60.0),
            "silences": tally.silences[:SAMPLE],
        },
        "schedule": {
            "applied": tally.schedule_count,
            "examples": tally.schedule_applied,
            "refused": dict(tally.schedule_refused.most_common()),
        },
        "approximations": list(APPROXIMATIONS),
    }


# ---------------------------------------------------------------------------
# The report in words
# ---------------------------------------------------------------------------

_SMALL = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def number_words(count: int) -> str:
    """*count* in words up to ten ("no" for 0), in figures with commas above."""
    return _SMALL[count] if 0 <= count <= 10 else f"{count:,}"


def counted(count: int, one: str, many: str | None = None) -> str:
    """ "no songs", "one song", "three songs", "1,204 songs"."""
    return f"{number_words(count)} {one if count == 1 else (many or one + 's')}"


def _figure(count: int) -> str:
    """*count* in words up to ten, "zero" for 0, in figures above."""
    return "zero" if count == 0 else number_words(count)


def _times(count: int) -> str:
    return {0: "never", 1: "once", 2: "twice"}.get(count, f"{number_words(count)} times")


def _average(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") if value else "0"


def _wall(text: str) -> str:
    """An ISO time from the report as "Monday 12 October 2026, 14:00"."""
    moment = datetime.fromisoformat(text)
    return f"{moment:%A} {moment.day} {moment:%B %Y, %H:%M}"


_STOPPED_WORDS = {
    "cancelled": "Stopped early: the run was cancelled.",
    "time limit": f"Stopped early: the run reached its time limit of "
    f"{MAX_RUN_SECONDS / 60:g} minutes. The figures cover the songs picked by then.",
}


def report_sections(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The report in words, as the page shows it: sections of short lines.

    Each section is ``{"title": ..., "lines": [...]}``; a line is a short
    sentence, or ``{"text": ..., "items": [...]}`` for a sentence with a
    list under it.  There are no tables.
    """
    sections: list[dict[str, Any]] = []

    def section(title: str, body: Iterable[Any]) -> None:
        lines = [line for line in body if line]
        if lines:
            sections.append({"title": title, "lines": lines})

    picking = report["song_picking"]
    mode = _MODE_WORDS.get(picking["mode"], picking["mode"])
    if picking.get("clockwheel"):
        mode = f"clockwheel {picking['clockwheel']}"
    requested = report["requested"]
    span = (
        f"{_average(requested['hours'])} hours"
        if "hours" in requested
        else counted(requested["picks"], "song")
    )
    section(
        "Summary",
        [
            f"Simulated {span}, from {_wall(report['start'])} to {_wall(report['end'])}, "
            f"station time ({report['timezone']}).",
            _STOPPED_WORDS.get(report.get("stopped_reason") or "", ""),
            f"Picked {counted(report['picks'], 'song')} and {counted(report['liners'], 'liner')}.",
            f"Song picking at the start: {mode}.",
            f"DMCA limits: {'on' if picking['dmca'] else 'off'}.",
            "Versions of a song count as one song." if picking.get("versions_as_one_song") else "",
            f"Seed {report['seed']}. Use the same seed to repeat this run exactly.",
            f"The run took {_average(report['elapsed_seconds'])} seconds. Nothing on air changed.",
        ],
    )
    modes = report["modes"]
    section(
        "How songs were picked",
        [
            f"{_MODE_WORDS.get(name, name)}: {counted(count, 'song')}."
            for name, count in modes.items()
        ],
    )
    songs = report["songs"]
    section(
        "Songs",
        [
            f"The library has {counted(songs['library'], 'song')}; "
            f"{_figure(songs['played'])} played and {_figure(songs['never_played'])} "
            "never played.",
            f"Plays per song: fewest {_figure(songs['min'])}, most "
            f"{_figure(songs['max'])}, average {_average(songs['average'])}.",
            *(
                f"Most played: {item['song']}, {_times(item['plays'])}."
                for item in songs["most_played"]
            ),
            {
                "text": f"Never played, first {_figure(len(songs['never_played_sample']))}:",
                "items": songs["never_played_sample"],
            }
            if songs["never_played_sample"]
            else "",
        ],
    )
    artists = report["artists"]
    section(
        "Artists",
        [
            f"{counted(artists['library'], 'artist').capitalize()}; "
            f"{_figure(artists['played'])} played and "
            f"{_figure(artists['never_played'])} never played.",
            f"Plays per artist: fewest {_figure(artists['min'])}, most "
            f"{_figure(artists['max'])}, average {_average(artists['average'])}.",
            *(
                f"Most played: {item['artist']}, {_times(item['plays'])}."
                for item in artists["most_played"]
            ),
        ],
    )
    category_lines: list[Any] = []
    for row in report["categories"]:
        sample = row["never_played_sample"]
        items = [
            f"Plays per song: fewest {_figure(row['min'])}, most {_figure(row['max'])}, "
            f"average {_average(row['average'])}.",
            f"Never played: {counted(row['never_played'], 'song')}."
            + (f" First {_figure(len(sample))}: {'; '.join(sample)}." if sample else ""),
            f"Its clockwheel step was skipped {_times(row['skipped'])}." if row["skipped"] else "",
        ]
        category_lines.append(
            {
                "text": f"{row['name']}: {counted(row['songs'], 'song')}, "
                f"{counted(row['plays'], 'play')}.",
                "items": [item for item in items if item],
            }
        )
    section("Categories", category_lines)
    relaxed = report["relaxed"]
    section(
        "Rules relaxed",
        [
            "No rule had to be relaxed." if not relaxed and not report["fallbacks"] else "",
            *(
                f"{rule.capitalize()}: relaxed for {counted(count, 'pick')}."
                for rule, count in relaxed.items()
            ),
            f"Fell back to a random song {_times(report['fallbacks'])}."
            if report["fallbacks"]
            else "",
            *(
                f"{rule.capitalize()}: not enforced by a clockwheel step, {counted(count, 'pick')}."
                for rule, count in report["ignored_by_steps"].items()
            ),
        ],
    )
    section(
        "Clockwheel steps skipped or empty",
        [f"{item['reason']}: {_times(item['count'])}." for item in report["skipped_steps"]],
    )
    dmca = report["dmca"]
    if dmca["enabled"]:
        section(
            "DMCA limits",
            [
                f"{counted(dmca['replaced'], 'song').capitalize()} replaced to keep the limits.",
                "No song broke a limit."
                if not dmca["limits"]
                else f"Limits broken: {_figure(sum(dmca['limits'].values()))}.",
                *(f"{kind}: {_times(count)}." for kind, count in dmca["limits"].items()),
                *(
                    f"{_wall(item['time'])}: {item['song']} broke {'; '.join(item['limits'])}."
                    for item in dmca["examples"]
                ),
            ],
        )
    dead = report["dead_air"]
    section(
        "Dead air",
        [
            "No moment had nothing to play."
            if not dead["silent_minutes"] and not dead["failed_picks"]
            else "",
            f"Picks that found nothing: {_figure(dead['failed_picks'])}; "
            "the station would start afresh after each."
            if dead["failed_picks"]
            else "",
            *(f"{_wall(item['time'])}: {item['reason']}." for item in dead["failed_examples"]),
            f"Silence: about {counted(dead['silent_minutes'], 'minute')} with nothing playable."
            if dead["silent_minutes"]
            else "",
            *(f"{_wall(item['time'])}: {item['reason']}." for item in dead["silences"]),
        ],
    )
    schedule = report["schedule"]
    section(
        "Schedule",
        [
            f"Song-picking changes applied: {_figure(schedule['applied'])}."
            if schedule["applied"]
            else "No scheduled event changed song picking.",
            *(
                f"{_wall(item['time'])}: {item['event']}, {item['action']}"
                + ("." if item["result"] == "applied" else f", not applied: {item['result']}.")
                for item in schedule["examples"]
            ),
        ],
    )
    section("Approximations", report["approximations"])
    return sections


def report_text(report: Mapping[str, Any]) -> str:
    """The report as short lines under headings, for a screen reader or a terminal.

    The title, then each section: a blank line, its heading, one short
    sentence per line, and a list's items as lines starting "- ".
    """
    lines = [f"Rotation test for station {report['station']}"]
    for part in report_sections(report):
        lines.extend(["", part["title"]])
        for line in part["lines"]:
            if isinstance(line, str):
                lines.append(line)
            else:
                lines.append(line["text"])
                lines.extend(f"- {item}" for item in line["items"])
    return "\n".join(lines) + "\n"


__all__ = [
    "MAX_HOURS",
    "MAX_PICKS",
    "Progress",
    "SimulationError",
    "SimulationRequest",
    "SimulationSource",
    "counted",
    "lower_thread_priority",
    "number_words",
    "open_station",
    "report_sections",
    "report_text",
    "simulate",
]
