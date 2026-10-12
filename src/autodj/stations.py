"""Stations: one playout chain each, sharing one music library (#70).

A station is a player and everything that hangs off it: the mix bus, the
mastering process, the stream encoder and relays, the listener-driven set
controller (:class:`autodj.station.Station`, a different and older class),
the liner scheduler, the recorder, listener bans, the now-playing feed,
webhooks, the event scheduler, carts, time checks, the WebSocket clients
and the state files these keep.  The library, the similarity index,
pairing and the web server are shared.

The default station's state lives where it always has; any other
station (``[stations.<slug>]``) keeps the same files under
``<index_dir>/<name>/stations/<slug>/``.  :class:`StationPaths` is the
only code that knows those locations.  :class:`StationRegistry` builds,
starts and stops the other stations.

Shutdown order matters, and :meth:`StationRegistry.shutdown` keeps it:
every station stops its stream, recorder, ticks and player, and waits
for the player's cache writers; only then does the shared library close
the DJ-meta cache and the catalog; last, each station closes its
mastering process and its WebSocket push.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import json
import logging
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autodj.station_log import CURRENT_STATION, DEFAULT_SLUG

if TYPE_CHECKING:
    from autodj._bridge import PlayerBridge
    from autodj.carts import StationCarts
    from autodj.clip_dates import LinerGate
    from autodj.config import AutoDJConfig
    from autodj.indexer import IndexEntry
    from autodj.library import LibraryManager
    from autodj.listener_access import ListenerAccess
    from autodj.recorder import Recorder
    from autodj.scheduler import Scheduler
    from autodj.stream import StreamOutput
    from autodj.time_checks import TimeChecks
    from autodj.webhooks import WebhookWorker

logger = logging.getLogger(__name__)

# A station's name: lower-case letters, digits and inner hyphens, at most 32
# characters.  It names a folder and a URL path segment.
SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")


class StationPaths:
    """Where one station keeps its state files and folders.

    Each location is worked out from the configuration when it is asked
    for, as the code it replaced did, so a configuration that only sets
    the index folder still answers for the files under it.
    """

    __slots__ = ("_cfg", "slug")

    def __init__(self, cfg: AutoDJConfig, slug: str = DEFAULT_SLUG) -> None:
        self._cfg = cfg
        self.slug = slug

    @classmethod
    def default(cls, cfg: AutoDJConfig) -> StationPaths:
        """The default station's paths: the active index folder, as always."""
        return cls(cfg)

    @classmethod
    def for_station(cls, cfg: AutoDJConfig, slug: str) -> StationPaths:
        """Another station's paths: the same names under ``stations/<slug>/``.

        Raises:
            ValueError: *slug* is the default station's or not a station name,
                so it can never name a folder outside ``stations/``.
        """
        if slug == DEFAULT_SLUG or not SLUG_PATTERN.match(slug):
            raise ValueError(f"not another station's name: {slug!r}")
        return cls(cfg, slug)

    @property
    def is_default(self) -> bool:
        """Whether these are the default station's paths."""
        return self.slug == DEFAULT_SLUG

    @property
    def state_dir(self) -> Path:
        """The folder holding the station's state files."""
        active = Path(self._cfg.index.active_dir)
        return active if self.is_default else active / "stations" / self.slug

    @property
    def web_state(self) -> Path:
        """Settings saved from the web page."""
        return self.state_dir / "web_state.json"

    @property
    def schedule(self) -> Path:
        """Scheduled events."""
        return self.state_dir / "schedule.json"

    @property
    def carts(self) -> Path:
        """The cart wall."""
        return self.state_dir / "carts.json"

    @property
    def cart_uploads(self) -> Path:
        """Audio uploaded to carts."""
        return self.state_dir / "carts"

    @property
    def time_checks(self) -> Path:
        """The time check switches saved from Settings."""
        return self.state_dir / "time_checks.json"

    @property
    def time_checks_cache(self) -> Path:
        """Spoken time checks made by the text-to-speech program."""
        return self.state_dir / "time_checks_cache"

    @property
    def time_check_clips(self) -> Path:
        """Recorded time check clips: ``[time_checks] clips_folder`` or the default."""
        return Path(self._cfg.time_checks.clips_folder or self.state_dir / "time_checks")

    @property
    def liner_dates(self) -> Path:
        """The voice liners' play dates."""
        return self.state_dir / "liner_dates.json"

    @property
    def listener_bans(self) -> Path:
        """Banned listener addresses."""
        return self.state_dir / "listener_bans.json"

    @property
    def recording(self) -> Path:
        """Whether a broadcast recording was running."""
        return self.state_dir / "recording.json"

    @property
    def relay_status(self) -> Path:
        """Relay states, for ``autodj doctor``.

        The default station's is in the index folder, not the named index;
        another station's is in its own folder.
        """
        if self.is_default:
            return Path(self._cfg.index.index_dir) / "relay-status.json"
        return self.state_dir / "relay-status.json"

    @property
    def clockwheels(self) -> Path:
        """Saved clockwheels."""
        return self.state_dir / "clockwheels"

    @property
    def liners(self) -> Path:
        """Liner clips: ``[playback] liners_folder`` or the default."""
        return Path(self._cfg.playback.liners_folder or self.state_dir / "liners")

    @property
    def profiles(self) -> Path:
        """Saved profiles.

        The default station's are shared by every named index of the index
        folder; another station's are its own.
        """
        if self.is_default:
            return self.state_dir.parent / "profiles"
        return self.state_dir / "profiles"


# ---------------------------------------------------------------------------
# Stream output, liners and ticks
# ---------------------------------------------------------------------------


def start_stream_station(
    bridge: PlayerBridge,
    liner_folder: Path,
    loop: asyncio.AbstractEventLoop,
    first_track: IndexEntry | None = None,
    recorder: Recorder | None = None,
    paths: StationPaths | None = None,
    owns_liner: Callable[[str], bool] | None = None,
) -> StreamOutput:
    """Build the stream output, liner scheduler and station; attach them.

    Starts the MP3 encoder, registers the stream as an output of the
    player's mix bus, and hands the stream to the station, which reads its
    listener count and starts a set on the first listener.  Configured
    relays (``[stream.relays.<name>]``) start connecting too; while any is
    enabled the station plays as if it always had a listener, because the
    relay's own listeners are invisible to AutoDJ.

    Args:
        bridge: The bridge whose player was built in stream mode.
        liner_folder: Where liners are read from.
        loop: The event loop listeners are served on.
        first_track: The first set's first track (``--seed``), if given.
        recorder: Receives the stream's audio while a recording runs.
        paths: The station's state files; the default station's when omitted.
        owns_liner: Whether a registered liner belongs to this station;
            every one does when omitted.

    Returns:
        The stream output (closed by :func:`stop_stream_station`).
    """
    from autodj import stream as stream_module
    from autodj.liner_scheduler import LinerScheduler
    from autodj.station import Station

    player = bridge.player
    cfg = player._cfg
    bus = player.bus
    stream_out = stream_module.StreamOutput(
        cfg.stream.bitrate,
        cfg.stream.max_listeners,
        encoder_factory=stream_module.FfmpegEncoder,
        loop=loop,
    )
    relays = None
    if cfg.stream.relays:
        from autodj.relay import RelayManager, relay_status_path

        relays = RelayManager(
            cfg.stream.relays.values(),
            stream_out,
            make_output=lambda bitrate: stream_module.StreamOutput(
                bitrate, 0, encoder_factory=stream_module.FfmpegEncoder, loop=loop
            ),
            station_name=lambda: cfg.stream.station_name,
            status_path=relay_status_path(cfg, paths),
        )
    relays_active = relays is not None and relays.active
    station = Station(
        bus,
        stream_out,
        player,
        cfg.stream.idle_grace_seconds,
        on_event=bridge.announce_station_event,
        forget_track=bridge.forget_track,
        continuous=lambda: (
            cfg.stream.continuous or relays_active or bool(recorder and recorder.wanted)
        ),
    )
    scheduler = LinerScheduler(
        cfg.playback,
        liner_folder,
        bus,
        can_fire=lambda: station.state == "playing",
        vocal_start=player.vocal_start_s,
        registered_liners=functools.partial(_registered_liners, bridge, owns_liner)
        if bridge.library is not None
        else None,
        path_decoder=functools.partial(_processed_liner, bridge)
        if bridge.library is not None
        else None,
    )
    if first_track is not None:
        station.start_with(first_track, "seed")
    bridge.attach_stream(stream=stream_out, station=station, relays=relays)
    bridge.attach_liner_scheduler(scheduler)
    if recorder is not None:
        stream_out.add_sink(recorder)
    bus.add_output(stream_out)
    if relays is not None:
        for output in relays.outputs:
            bus.add_output(output)
        relays.start()
    return stream_out


def start_server_audio_liners(
    bridge: PlayerBridge, liner_folder: Path, owns_liner: Callable[[str], bool] | None = None
) -> None:
    """Fire voice liners into the plain ``--server-audio`` mix bus.

    Same triggers, rotation and ducking as stream mode; automatic
    liners fire only while a set is playing and not paused, like the
    station's "playing" state.

    Args:
        bridge: The bridge whose player mixes on the server.
        liner_folder: Where liners are read from.
        owns_liner: Whether a registered liner belongs to this station.
    """
    from autodj.liner_scheduler import LinerScheduler

    player = bridge.player
    bus = player.bus
    scheduler = LinerScheduler(
        player._cfg.playback,
        liner_folder,
        bus,
        can_fire=lambda: bus.playing and not player._state.is_paused,
        vocal_start=player.vocal_start_s,
        registered_liners=functools.partial(_registered_liners, bridge, owns_liner)
        if bridge.library is not None
        else None,
        path_decoder=functools.partial(_processed_liner, bridge)
        if bridge.library is not None
        else None,
    )
    bridge.attach_liner_scheduler(scheduler)


def _liner_gate(bridge: PlayerBridge, paths: StationPaths) -> LinerGate:
    """The station's liner play dates, with the catalog naming registered liners."""
    from autodj.categories import station_wall
    from autodj.clip_dates import LinerDates, LinerGate, station_today

    cfg = bridge.player._cfg

    def liner_id(path: str) -> str:
        library = bridge.library
        record = library.catalog.record_for_path(path) if library is not None else None
        return str(record["id"]) if record else Path(path).name

    return LinerGate(
        LinerDates(paths.liner_dates),
        lambda: station_today(cfg),
        liner_id,
        lambda at: station_wall(cfg, at).date(),
    )


#: A library (catalog) record ID: 32 hexadecimal digits.  A liner folder
#: file name always has an extension, so the two never meet.
_CATALOG_ID = re.compile(r"[0-9a-f]{32}")


def prune_liner_dates(station: StationRuntime) -> list[str]:
    """Forget the play dates of *station*'s liners that are gone; the IDs forgotten.

    A liner deleted outside AutoDJ would otherwise keep its dates, and a new
    file that took its name (or its catalog place) would start with them.
    A registered liner is gone when the latest scan found its file missing
    from a folder that is there; one in the liner folder, when the folder is
    there and the file is not.  A share that is not mounted forgets nothing.
    """
    store = station.liner_gate.store
    dates = store.all()
    if not dates:
        return []
    library = station.bridge.library
    # The liner folder, listed once.  A folder that cannot be listed (a share
    # that is away, a moment of trouble) forgets nothing from it.
    names: set[str] | None
    try:
        with os.scandir(station.paths.liners) as listing:
            names = {item.name for item in listing if item.is_file()}
    except OSError as exc:
        logger.info(
            "Station %s: liner folder not listed (%s); keeping its dates", station.slug, exc
        )
        names = None
    file_keyed = [
        liner_id
        for liner_id in dates
        if not _CATALOG_ID.fullmatch(liner_id) and Path(liner_id).name == liner_id
    ]
    if names is not None and not names and file_keyed:
        # A readable but empty folder: a share can show one for a moment.
        # Its dates go only when two scans in a row find it empty.
        station.liner_folder_empty_scans = getattr(station, "liner_folder_empty_scans", 0) + 1
        if station.liner_folder_empty_scans < 2:
            logger.info(
                "Station %s: the liner folder is empty; its liners' dates are kept until "
                "the next scan finds it empty too",
                station.slug,
            )
            names = None
    elif names is not None:
        station.liner_folder_empty_scans = 0
    # Compared as the file system compares names (without case on Windows).
    listed = {os.path.normcase(name) for name in names} if names is not None else None
    gone = []
    for liner_id in dates:
        record = library.catalog.record_for_id(liner_id) if library is not None else None
        if record is not None:
            # A registered liner: gone when the scan found its file missing.
            if (
                record.get("kind") == "liner"
                and library is not None
                and library.is_gone(record["path"])
            ):
                gone.append(liner_id)
        elif _CATALOG_ID.fullmatch(liner_id):
            # A library ID without a record (a source removed, say): kept,
            # because it is not a file name in the liner folder.
            continue
        elif (
            listed is not None
            and Path(liner_id).name == liner_id
            and os.path.normcase(liner_id) not in listed
        ):
            gone.append(liner_id)  # one in the liner folder, gone from it
    forgotten = store.forget(gone)
    if forgotten:
        logger.info(
            "Station %s: forgot the play dates of %d liners that are gone: %s",
            station.slug,
            len(forgotten),
            ", ".join(forgotten),
        )
    return forgotten


def _registered_liners(
    bridge: PlayerBridge, owns: Callable[[str], bool] | None = None
) -> list[tuple[str, Path]]:
    return [
        (record["id"], Path(entry.path))
        for entry in bridge.library.entries_snapshot("liner")
        if (owns is None or owns(entry.path))
        and (record := bridge.library.catalog.record_for_path(entry.path)) is not None
    ]


def _processed_liner(bridge: PlayerBridge, path: Path) -> Any:
    from autodj.stereo import SAMPLE_RATE

    entry = bridge.library.entry_for_path(path)
    if entry is None:
        raise ValueError("The liner is no longer registered")
    audio = bridge.player._load_playout_audio(entry)
    points = bridge.player.saved_points(entry)
    first = round((points.get("start") or 0.0) * SAMPLE_RATE)
    last = round((points.get("end") or entry.length) * SAMPLE_RATE)
    return audio[first:last]


async def stop_stream_station(bridge: PlayerBridge, stream_out: StreamOutput) -> None:
    """Disconnect listeners, stop the encoder and the liner worker.

    Each step is attempted even if an earlier one fails, and nothing is
    raised: the rest of shutdown (player stop, cache flush) must still run.
    """
    remove_output = bridge.player.bus.remove_output
    relay_steps: list[tuple[str, Callable[[], None]]] = []
    if bridge.relays is not None:
        relay_steps = [
            ("detaching a relay encoder", functools.partial(remove_output, output))
            for output in bridge.relays.outputs
        ]
        relay_steps.append(("stopping the relays", bridge.relays.close))
    steps: tuple[tuple[str, Callable[[], None]], ...] = (
        *relay_steps,
        ("detaching", functools.partial(bridge.player.bus.remove_output, stream_out)),
        ("closing", stream_out.close),
        ("stopping the liner worker for", bridge.shutdown_liner_worker),
    )
    for what, step in steps:
        try:
            await asyncio.to_thread(step)
        except Exception:
            logger.exception("Stopping the stream: %s it failed", what)


async def tick_every_second(
    name: str, worker: Any, sleep: Callable[[float], Awaitable[None]]
) -> None:
    """Call ``worker.tick`` off the event loop about once a second, forever.

    A failed tick is logged and the next one still runs.
    """
    while True:
        await sleep(1)
        try:
            await asyncio.to_thread(worker.tick)
        except Exception:
            logger.exception("Stream %s tick failed", name)


async def tick_stream_workers(
    bridge: PlayerBridge, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
) -> None:
    """Tick the station and the liner scheduler once a second, independently.

    Each has its own loop, so a slow liner decode (up to 30 s through
    ffmpeg) never holds up the station's idle check, and a failure in one
    never stops the other.  Runs until cancelled.

    Args:
        bridge: The bridge holding the station and liner scheduler.
        sleep: ``asyncio.sleep`` replacement for tests.
    """
    workers = (("station", bridge.station), ("liner scheduler", bridge.liner_scheduler))
    async with asyncio.TaskGroup() as group:
        for name, worker in workers:
            if worker is not None:
                group.create_task(tick_every_second(name, worker, sleep))


def quiesce_cache_writers(
    bridge: PlayerBridge,
    player_thread: threading.Thread | None,
    timeout_s: float,
) -> bool:
    """Wait up to one shared deadline for every possible cache writer."""
    deadline = time.monotonic() + timeout_s
    if player_thread is not None:
        player_thread.join(max(0.0, deadline - time.monotonic()))
        if player_thread.is_alive():
            return False

    wait_for_analysis = None
    if hasattr(type(bridge.player), "wait_for_background_analysis"):
        wait_for_analysis = bridge.player.wait_for_background_analysis
    if not callable(wait_for_analysis):
        return True
    return bool(wait_for_analysis(timeout=max(0.0, deadline - time.monotonic())))


async def _cancel(task: asyncio.Task[None] | None, *quiet: type[BaseException]) -> None:
    """Cancel *task* and wait for it, swallowing its cancellation and *quiet*."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, *quiet):
        await task


def _every_liner(_path: str) -> bool:
    return True


# ---------------------------------------------------------------------------
# One station
# ---------------------------------------------------------------------------


class StationRuntime:
    """One playout chain: a player and the station state around it.

    The app builds the scheduler, time checks, cart wall and WebSocket
    push (:func:`autodj.station_routes.equip_station`) and sets them here
    before it starts the station; the station routes find them here.
    """

    schedule: Scheduler
    time_checks: TimeChecks
    carts: StationCarts
    # Pushes the state to this station's WebSocket clients once a second.
    broadcast: Callable[[], Coroutine[Any, Any, None]]

    def __init__(
        self,
        bridge: PlayerBridge,
        *,
        timezone: str,
        player_thread: threading.Thread | None = None,
        stream_mode: bool = False,
        server_audio: bool = False,
        first_track: IndexEntry | None = None,
        slug: str = DEFAULT_SLUG,
        paths: StationPaths | None = None,
    ) -> None:
        """Build the station's feed, webhooks and listener access; start nothing.

        Args:
            bridge: The station's player bridge.
            timezone: The station time zone, for the feed, schedule and recordings.
            player_thread: The player's thread, joined when the station stops.
            stream_mode: Serve the mix as a stream (the player was built for it).
            server_audio: Plain ``--server-audio``: liners fire into the mix bus.
            first_track: In stream mode, the track the first set starts with.
            slug: The station's name; the default station's when omitted.
            paths: The station's state files; the default station's when omitted.
        """
        from autodj._bridge import PlayerBridge
        from autodj.listener_access import ListenerAccess
        from autodj.nowplaying import NowPlaying
        from autodj.webhooks import WebhookWorker

        self.slug = slug
        self.bridge = bridge
        self.timezone = timezone
        self.player_thread = player_thread
        self.stream_mode = stream_mode
        self.server_audio = server_audio
        self.first_track = first_track
        self.paths = paths if paths is not None else StationPaths.default(bridge.player._cfg)
        self.liner_gate = _liner_gate(bridge, self.paths)
        # The clockwheel's liner steps pass over liners outside their dates.
        # A plain function, so copying the player's settings never copies the store.
        gate = self.liner_gate
        bridge.player.liner_filter = lambda entries, at=None: gate.playable(entries, at)
        self.nowplaying = NowPlaying(bridge, timezone=timezone)
        self.webhooks: WebhookWorker = WebhookWorker(getattr(bridge.player._cfg, "webhooks", ()))
        self.listener_access: ListenerAccess | None = (
            ListenerAccess(
                self.paths.listener_bans,
                bridge.player._cfg.stream.max_listeners_per_address,
            )
            if stream_mode or (isinstance(bridge, PlayerBridge) and bridge.stream_mode)
            else None
        )
        self.recorder: Recorder | None = None
        self.stream_out: StreamOutput | None = None
        self.ws_clients: set[Any] = set()
        self.ws_lock = asyncio.Lock()
        self._schedule_ticker: asyncio.Task[None] | None = None
        self._stream_ticker: asyncio.Task[None] | None = None
        self._broadcast_task: asyncio.Task[None] | None = None
        # Set while the station is stopped; its routes then answer 409.
        self.stopped = False
        # Whether a liner registered in the shared library is this station's
        # (set by the registry); with one station, every liner is.
        self.owns_liner: Callable[[str], bool] = _every_liner
        # Scans in a row that found the liner folder empty (see prune_liner_dates).
        self.liner_folder_empty_scans = 0

    @property
    def name(self) -> str:
        """The station's name, as listeners see it."""
        return str(self.cfg.stream.station_name)

    def uses_stereo_tool(self) -> bool:
        """Whether Stereo Tool processes this station's mix, or will from the next song.

        Only a station that mixes on the server runs a mastering process.
        The song playing keeps the processing it was rendered with, so a
        station that just left Stereo Tool still counts until it ends.
        """
        player = self.player
        if getattr(player, "bus", None) is None:
            return False
        if getattr(player, "_continuous_master", None) is None:
            return False
        modes = {
            getattr(player, "_active_processing_mode", None),
            self.cfg.playback.loudness_processing,
        }
        return "stereotool" in modes

    def summary(self) -> dict[str, Any]:
        """The station's name, state, listeners, song and stream address, for ``/api/stations``."""
        bridge = self.bridge
        player_state = bridge.player._state
        controller = bridge.station
        if self.stopped:
            state = "stopped"
        elif controller is not None:
            state = str(controller.state)
        elif player_state.is_paused:
            state = "paused"
        else:
            state = "playing" if player_state.current_track is not None else "idle"
        track = player_state.current_track
        stream = bridge.stream
        return {
            "slug": self.slug,
            "name": self.name,
            "state": state,
            "listeners": int(stream.listener_count) if stream is not None else 0,
            "title": getattr(track, "display_name", None) if track is not None else None,
            "stream_path": bridge.stream_path if bridge.stream_mode else None,
        }

    @property
    def cfg(self) -> AutoDJConfig:
        """The station's configuration."""
        return self.bridge.player._cfg

    @property
    def player(self) -> Any:
        """The station's player."""
        return self.bridge.player

    @property
    def ledger(self) -> Any:
        """The station's plays and settings in the shared library, or ``None`` without one."""
        return self.bridge._ledger()

    # Both run on playout threads (mix bus, station), so a fault here is
    # logged and never reaches playback.
    def track_started(self) -> None:
        """Refresh the feed and send it to the webhooks."""
        try:
            self.webhooks.submit(self.nowplaying.refresh())
        except Exception:
            logger.exception("Could not publish now-playing state")

    def public_state_changed(self) -> None:
        """Refresh the feed."""
        try:
            self.nowplaying.refresh()
        except Exception:
            logger.exception("Could not refresh now-playing state")

    async def start(self) -> None:
        """Start the schedule, the stream or liners, the WebSocket push and webhooks."""
        self.stopped = False
        bridge = self.bridge
        cfg = self.cfg
        schedule = self.schedule
        schedule.start()
        loop = asyncio.get_running_loop()

        def schedule_track_changed() -> None:
            # The mix-bus thread calls this; it only hands over to the loop.
            with contextlib.suppress(RuntimeError):  # the loop closed at shutdown
                loop.call_soon_threadsafe(schedule.track_changed)

        bridge.scheduler_track_changed = schedule_track_changed
        self._schedule_ticker = asyncio.create_task(self._tick_schedule())
        if self.stream_mode:
            from autodj.recorder import Recorder

            self.recorder = await asyncio.to_thread(
                Recorder,
                cfg.recording,
                self.paths.recording,
                lambda: cfg.stream.station_name,
                self.timezone,
            )
            self.stream_out = await asyncio.to_thread(
                start_stream_station,
                bridge,
                self.paths.liners,
                asyncio.get_running_loop(),
                self.first_track,
                self.recorder,
                self.paths,
                self.owns_liner,
            )
            self._stream_ticker = asyncio.create_task(tick_stream_workers(bridge))
        elif self.server_audio:
            await asyncio.to_thread(
                start_server_audio_liners, bridge, self.paths.liners, self.owns_liner
            )
            self._stream_ticker = asyncio.create_task(tick_stream_workers(bridge))
        if bridge.liner_scheduler is not None:
            # The time check after the hour plays where an automatic liner would.
            bridge.liner_scheduler.announcement = self.time_checks.due_audio
            # Automatic liners pass over the ones outside their play dates.
            bridge.liner_scheduler.play_dates = self.liner_gate.refusal
            # A talk-up planned ahead is judged by the day it airs.
            bridge.liner_scheduler.play_dates_at = self.liner_gate.refusal
        self._broadcast_task = asyncio.create_task(self.broadcast())
        self.webhooks.start()

    async def stop(self, timeout_s: float) -> bool:
        """Stop the schedule, stream, recorder, liners and player, in that order.

        Listeners are disconnected and the encoder stopped first, then the
        station's ticks and the liner worker, then the player is told to
        leave its loop.  The player thread and every background analysis
        worker are then given up to *timeout_s* seconds to finish, because
        the shared DJ-meta cache must not close under a writer.  Nothing
        here raises: the rest of shutdown must still run.

        Returns:
            Whether every cache writer stopped in time.
        """
        self.stopped = True
        bridge = self.bridge
        bridge.scheduler_track_changed = None
        await _cancel(self._schedule_ticker)
        self.webhooks.close()
        if self.stream_out is not None:
            await stop_stream_station(bridge, self.stream_out)
        if self.recorder is not None:
            await asyncio.to_thread(self.recorder.close)
            self.recorder = None
        await _cancel(self._stream_ticker, Exception)
        if self.server_audio:
            bridge.shutdown_liner_worker()
        try:
            # A daemon thread, so the process can exit either way, but a clean
            # stop lets sounddevice release OS handles instead of being torn
            # down mid-call.
            bridge.player._state.should_stop = True
            bridge.player._skip_event.set()
        except Exception:  # pragma: no cover -- defensive log-only path
            logger.debug("shutdown: player stop signal failed", exc_info=True)
        has_background_writers = hasattr(type(bridge.player), "wait_for_background_analysis")
        if self.player_thread is None and not has_background_writers:
            return True
        return await asyncio.to_thread(quiesce_cache_writers, bridge, self.player_thread, timeout_s)

    async def close(self) -> None:
        """Close the mastering process and stop the WebSocket push; after :meth:`stop`."""
        master = getattr(self.bridge.player, "_continuous_master", None)
        if master is not None:
            await asyncio.to_thread(master.close)
        # Awaited with its cancellation swallowed, so asyncio does not log
        # "Task was destroyed but it is pending" on a Ctrl+C exit.
        await _cancel(self._broadcast_task, Exception)

    async def _tick_schedule(self) -> None:
        """Run due scheduled events once a second; a failure never stops the timer."""
        while True:
            await asyncio.sleep(1)
            try:
                await self.schedule.tick()
            except Exception:
                logger.exception("Scheduler tick failed")
            checks = self.time_checks
            try:
                checks.tick(
                    datetime.now(checks.zone), self.bridge.player._state.current_track is not None
                )
            except Exception:
                logger.exception("Time check tick failed")


# ---------------------------------------------------------------------------
# The shared library and the stations of one server
# ---------------------------------------------------------------------------


class StationControlError(RuntimeError):
    """A station could not be started or stopped; the message says why, in words."""


@dataclass(frozen=True)
class ExtraStation:
    """A station besides the default one (``[stations.<slug>]``) and how to build it.

    Attributes:
        slug: The station's name.
        name: Its name as listeners see it, shown while it is stopped.
        autostart: Start it with the server.
        paths: Where it keeps its state.
        preset: Its configured Stereo Tool preset, for a stopped station
            whose saved settings name none.
        build: Builds a fresh, unstarted runtime with its player thread
            running.  Called in a worker thread, as the station (its log
            lines and threads carry its name).  It raises
            :class:`StationControlError` when the station must not start.
    """

    slug: str
    name: str
    autostart: bool
    paths: StationPaths
    preset: str
    build: Callable[[StationRegistry], StationRuntime]


class StoppedStation:
    """What the registry answers for a configured station that is not running.

    Its routes answer 409 and its stream 503: ``stopped`` is always set.
    """

    stopped = True

    def __init__(self, extra: ExtraStation) -> None:
        self.slug = extra.slug
        self.name = extra.name

    def summary(self) -> dict[str, Any]:
        """The station's entry in ``/api/stations``."""
        return {
            "slug": self.slug,
            "name": self.name,
            "state": "stopped",
            "listeners": 0,
            "title": None,
            "stream_path": f"/stations/{self.slug}.mp3",
        }


async def _run_as[T](slug: str, coro: Coroutine[Any, Any, T]) -> T:
    """Run *coro* as station *slug*: its tasks, threads and log lines carry the name."""
    context = contextvars.copy_context()
    context.run(CURRENT_STATION.set, slug)
    return await asyncio.create_task(coro, context=context)


def _retrieve(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()  # retrieved, so a failure nobody awaits is not logged twice


async def _shielded[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run *coro* to its end even if the caller is cancelled, and return its result."""
    task = asyncio.ensure_future(coro)
    task.add_done_callback(_retrieve)
    return await asyncio.shield(task)


def _normalized(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(path))))


def _inside(path: str, root: str) -> bool:
    try:
        relative = os.path.relpath(path, root)
    except ValueError:  # another drive on Windows
        return False
    return relative != os.curdir and not (
        relative == os.pardir or relative.startswith(os.pardir + os.sep) or os.path.isabs(relative)
    )


def _saved_preset(paths: StationPaths) -> str | None:
    """The Stereo Tool preset a stopped station's saved settings name, if any."""
    try:
        data = json.loads(paths.web_state.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    playback = data.get("playback") if isinstance(data, dict) else None
    preset = playback.get("stereotool_preset") if isinstance(playback, dict) else None
    return preset if isinstance(preset, str) else None


class StationRegistry:
    """The stations of one server: the default one, and any others configured.

    The default station always runs.  Another station runs from when it
    is started (with the server when it has ``autostart``, or from
    ``POST /api/stations/<slug>/start``) until it is stopped; while it is
    stopped, :meth:`get` answers a :class:`StoppedStation` and nothing of
    it runs or stays in memory.  Starting it again builds a fresh runtime.
    """

    def __init__(self, default: StationRuntime, extras: Sequence[ExtraStation] = ()) -> None:
        self._default = default
        self._extras = {extra.slug: extra for extra in extras}
        self._running: dict[str, StationRuntime] = {default.slug: default}
        # One start or stop at a time, so two starts cannot both find
        # Stereo Tool free.
        self._lock = asyncio.Lock()
        # Set once shutdown begins: no station starts after that.
        self._closing = False
        # Held while a station checks that Stereo Tool is free and takes
        # it: a station being built (in a worker thread) or one switching
        # to it in its settings (also in a worker thread).
        self.stereo_tool_lock = threading.RLock()
        # The config of each station being built, which may already have
        # taken Stereo Tool, until it runs or has failed to start.
        self._starting: dict[str, AutoDJConfig] = {}
        # Set by the app: wires a built station to the app (its WebSocket
        # push, schedule, carts and time checks) before it starts, and
        # closes its pages' sockets once it has stopped.
        self.prepare: Callable[[StationRuntime], None] = lambda _station: None
        self.disconnect: Callable[[StationRuntime], Awaitable[None]] | None = None
        # How long a stopping station's player may take to finish.
        self.stop_timeout_s = 10.0

    @property
    def default(self) -> StationRuntime:
        """The station the root config sections describe."""
        return self._default

    def get(self, slug: str) -> StationRuntime | StoppedStation:
        """The station named *slug*, or its stand-in while it is stopped.

        Raises:
            KeyError: No station has that name.
        """
        running = self._running.get(slug)
        if running is not None:
            return running
        return StoppedStation(self._extras[slug])

    def all(self) -> tuple[StationRuntime, ...]:
        """Every running station, the default first."""
        return tuple(self._running.values())

    def slugs(self) -> frozenset[str]:
        """The name of every configured station, running or not."""
        return frozenset({self._default.slug, *self._extras})

    def paths(self) -> tuple[StationPaths, ...]:
        """Every configured station's paths, the default first."""
        return (self._default.paths, *(extra.paths for extra in self._extras.values()))

    def summaries(self) -> list[dict[str, Any]]:
        """Every configured station's name, state, listeners, song and stream address."""
        return [self.get(slug).summary() for slug in (self._default.slug, *self._extras)]

    def owns_liner(self, slug: str, path: str) -> bool:
        """Whether the registered liner at *path* is station *slug*'s.

        Another station's liners are those in its liner folder.  The
        default station's are every other liner, as before there were
        several stations, except those in another station's folder.
        """
        if not self._extras:
            return True
        where = _normalized(path)
        roots = {other: _normalized(extra.paths.liners) for other, extra in self._extras.items()}
        if slug in roots:
            return _inside(where, roots[slug])
        own = _normalized(self._default.paths.liners)
        return not any(_inside(where, root) for root in roots.values() if root != own)

    def stereo_tool_user(self, exclude: str) -> str | None:
        """The name of a station other than *exclude* that uses Stereo Tool.

        A running station, or one being started whose settings choose it.
        """
        for station in self.all():
            if station.slug != exclude and station.uses_stereo_tool():
                return station.name
        for slug, cfg in list(self._starting.items()):
            if slug != exclude and cfg.playback.loudness_processing == "stereotool":
                return str(cfg.stream.station_name)
        return None

    def claim_stereo_tool(self, slug: str, cfg: AutoDJConfig) -> str | None:
        """Mark station *slug*, about to start with *cfg*, as starting.

        Checking and marking happen under :attr:`stereo_tool_lock`, so a
        station switching to Stereo Tool at the same moment sees this one.

        Returns:
            ``None``, or the name of another station that already uses
            Stereo Tool when *cfg* chooses it too; the station is then not
            marked and must not start.
        """
        with self.stereo_tool_lock:
            if cfg.playback.loudness_processing == "stereotool":
                other = self.stereo_tool_user(exclude=slug)
                if other is not None:
                    return other
            self._starting[slug] = cfg
            return None

    def stereo_tool_presets(self) -> list[tuple[str, str]]:
        """(station name, Stereo Tool preset) for every configured station.

        A running station's is its live setting; a stopped one's is the
        preset its saved settings name, else its configured one.
        """
        presets = [
            (station.name, str(station.cfg.playback.stereotool_preset)) for station in self.all()
        ]
        for slug, extra in self._extras.items():
            if slug not in self._running:
                presets.append((extra.name, _saved_preset(extra.paths) or extra.preset))
        return presets

    async def start_all(self) -> None:
        """Start the default station, then every other station with ``autostart``.

        A station that cannot start is logged and left stopped; the
        server, and the default station, run on.
        """
        await _run_as(self._default.slug, self._default.start())
        for slug, extra in self._extras.items():
            if not extra.autostart:
                continue
            try:
                await self.start(slug)
            except StationControlError as exc:
                logger.error("Station %s did not start: %s", slug, exc)

    async def start(self, slug: str) -> StationRuntime:
        """Build and start station *slug*; a running station is left as it is.

        Raises:
            KeyError: No station has that name.
            StationControlError: The station could not start.
        """
        if slug == self._default.slug:
            return self._default
        extra = self._extras[slug]
        # Shielded: a request cancelled at shutdown must not leave a
        # half-built station running outside the registry.
        return await _shielded(self._start_locked(extra))

    async def _start_locked(self, extra: ExtraStation) -> StationRuntime:
        slug = extra.slug
        async with self._lock:
            running = self._running.get(slug)
            if running is not None:
                return running
            if self._closing:
                raise StationControlError("The server is shutting down.")
            try:
                running = await _run_as(slug, self._start_extra(extra))
                self._running[slug] = running
            finally:
                self._starting.pop(slug, None)
            return running

    async def _start_extra(self, extra: ExtraStation) -> StationRuntime:
        try:
            runtime = await asyncio.to_thread(extra.build, self)
        except StationControlError:
            raise
        except Exception as exc:
            logger.exception("Could not build station %s", extra.slug)
            raise StationControlError(f"{extra.name} could not start: {exc}") from exc
        try:
            self.prepare(runtime)
            await runtime.start()
        except Exception as exc:
            logger.exception("Could not start station %s", extra.slug)
            await self._teardown(runtime)
            raise StationControlError(f"{extra.name} could not start: {exc}") from exc
        logger.info("Station %s (%s) started", extra.slug, extra.name)
        return runtime

    async def stop(self, slug: str) -> None:
        """Stop station *slug* completely: listeners, encoders, relays and player.

        Raises:
            KeyError: No station has that name.
            StationControlError: *slug* is the default station.
        """
        if slug == self._default.slug:
            raise StationControlError("The default station cannot be stopped.")
        extra = self._extras[slug]
        # Shielded, like a start: a stop cut off half-way would leave its
        # player and mastering process running outside the registry.
        await _shielded(self._stop_locked(extra))

    async def _stop_locked(self, extra: ExtraStation) -> None:
        async with self._lock:
            runtime = self._running.pop(extra.slug, None)
            if runtime is not None:
                await _run_as(extra.slug, self._stop_extra(runtime, extra))

    async def _stop_extra(self, runtime: StationRuntime, extra: ExtraStation) -> None:
        await self._teardown(runtime)
        logger.info("Station %s (%s) stopped", extra.slug, extra.name)

    async def _teardown(self, runtime: StationRuntime) -> None:
        """Stop and close *runtime*, then close its pages' sockets; never raises."""
        if not await runtime.stop(self.stop_timeout_s):
            logger.warning(
                "Station %s: the player did not finish within %.0f seconds",
                runtime.slug,
                self.stop_timeout_s,
            )
        await runtime.close()
        if self.disconnect is not None:
            try:
                await self.disconnect(runtime)
            except Exception:
                logger.exception("Could not close the pages of station %s", runtime.slug)

    async def shutdown(self, library: SharedLibrary, timeout_s: float) -> tuple[bool, bool]:
        """Stop every station, then close the shared library, then each master and push.

        Returns:
            Whether every cache writer stopped in time, and whether the
            DJ-meta cache closed.
        """
        # After any start or stop under way, so a station starting now is
        # stopped too and one stopping now has finished; none starts later.
        async with self._lock:
            self._closing = True
            writers_quiesced = True
            for station in self.all():
                quiet = await _run_as(station.slug, station.stop(timeout_s))
                writers_quiesced = quiet and writers_quiesced
            cache_closed = await library.close(writers_quiesced)
            for station in self.all():
                await _run_as(station.slug, station.close())
            return writers_quiesced, cache_closed


class SharedLibrary:
    """The music library every station plays from: its scan, tick and close."""

    def __init__(self, library: LibraryManager | None, stations: StationRegistry) -> None:
        self.library = library
        self._stations = stations
        self._ticker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the library's scan and its two-second tick."""
        if self.library is not None:
            self.library.on_scan_done = self._scan_done
            self.library.start_scan()
            self._ticker = asyncio.create_task(self._tick())

    def _scan_done(self) -> None:
        """After a scan (on its thread): forget the play dates of liners now gone."""
        for station in self._stations.all():
            try:
                prune_liner_dates(station)
            except Exception:
                logger.exception("Station %s: could not tidy the liner dates", station.slug)

    async def close(self, writers_quiesced: bool) -> bool:
        """Close the DJ-meta cache if no writer remains, then the library.

        Returns:
            Whether the DJ-meta cache closed.
        """
        cache_closed = False
        if writers_quiesced:
            try:
                from autodj.dj_meta import close_cache

                close_cache()
                cache_closed = True
            except Exception:  # pragma: no cover -- defensive shutdown logging
                logger.debug("shutdown: DJ-meta close failed", exc_info=True)
        if self._ticker is not None and self.library is not None:
            await _cancel(self._ticker)
            await asyncio.to_thread(self.library.close)
        return cache_closed

    async def _tick(self) -> None:
        from autodj.player import apply_repeat_windows

        library = self.library
        assert library is not None

        def tick() -> None:
            library.tick()
            for station in self._stations.all():
                apply_repeat_windows(station.player)

        while True:
            await asyncio.to_thread(tick)
            await asyncio.sleep(2)
