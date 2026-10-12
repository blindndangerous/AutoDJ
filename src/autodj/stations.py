"""Stations: one playout chain each, sharing one music library (#70).

A station is a player and everything that hangs off it: the mix bus, the
mastering process, the stream encoder and relays, the listener-driven set
controller (:class:`autodj.station.Station`, a different and older class),
the liner scheduler, the recorder, listener bans, the now-playing feed,
webhooks, the event scheduler, carts, time checks, the WebSocket clients
and the state files these keep.  The library, the similarity index,
pairing and the web server are shared.

Today a server runs exactly one station, ``default``, whose state lives
where it always has.  :class:`StationPaths` is the only code that knows
those locations, so a later station can keep the same files elsewhere.

Shutdown order matters, and :meth:`StationRegistry.shutdown` keeps it:
every station stops its stream, recorder, ticks and player, and waits
for the player's cache writers; only then does the shared library close
the DJ-meta cache and the catalog; last, each station closes its
mastering process and its WebSocket push.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from autodj._bridge import PlayerBridge
    from autodj.carts import StationCarts
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

DEFAULT_SLUG = "default"


class StationPaths:
    """Where one station keeps its state files and folders.

    Each location is worked out from the configuration when it is asked
    for, as the code it replaced did, so a configuration that only sets
    the index folder still answers for the files under it.
    """

    __slots__ = ("_cfg",)

    def __init__(self, cfg: AutoDJConfig) -> None:
        self._cfg = cfg

    @classmethod
    def default(cls, cfg: AutoDJConfig) -> StationPaths:
        """The default station's paths: the active index folder, as always."""
        return cls(cfg)

    @property
    def state_dir(self) -> Path:
        """The folder holding the station's state files."""
        return Path(self._cfg.index.active_dir)

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
    def listener_bans(self) -> Path:
        """Banned listener addresses."""
        return self.state_dir / "listener_bans.json"

    @property
    def recording(self) -> Path:
        """Whether a broadcast recording was running."""
        return self.state_dir / "recording.json"

    @property
    def relay_status(self) -> Path:
        """Relay states, for ``autodj doctor``; in the index folder, not the named index."""
        return Path(self._cfg.index.index_dir) / "relay-status.json"

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
        """Saved profiles; shared by every named index of the index folder."""
        return self.state_dir.parent / "profiles"


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
        registered_liners=functools.partial(_registered_liners, bridge)
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


def start_server_audio_liners(bridge: PlayerBridge, liner_folder: Path) -> None:
    """Fire voice liners into the plain ``--server-audio`` mix bus.

    Same triggers, rotation and ducking as stream mode; automatic
    liners fire only while a set is playing and not paused, like the
    station's "playing" state.

    Args:
        bridge: The bridge whose player mixes on the server.
        liner_folder: Where liners are read from.
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
        registered_liners=functools.partial(_registered_liners, bridge)
        if bridge.library is not None
        else None,
        path_decoder=functools.partial(_processed_liner, bridge)
        if bridge.library is not None
        else None,
    )
    bridge.attach_liner_scheduler(scheduler)


def _registered_liners(bridge: PlayerBridge) -> list[tuple[str, Path]]:
    return [
        (record["id"], Path(entry.path))
        for entry in bridge.library.entries_snapshot("liner")
        if (record := bridge.library.catalog.record_for_path(entry.path)) is not None
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
    ) -> None:
        """Build the station's feed, webhooks and listener access; start nothing.

        Args:
            bridge: The station's player bridge.
            timezone: The station time zone, for the feed, schedule and recordings.
            player_thread: The player's thread, joined when the station stops.
            stream_mode: Serve the mix as a stream (the player was built for it).
            server_audio: Plain ``--server-audio``: liners fire into the mix bus.
            first_track: In stream mode, the track the first set starts with.
        """
        from autodj._bridge import PlayerBridge
        from autodj.listener_access import ListenerAccess
        from autodj.nowplaying import NowPlaying
        from autodj.webhooks import WebhookWorker

        # Only the default station exists yet; its state lives where it always has.
        self.slug = DEFAULT_SLUG
        self.bridge = bridge
        self.timezone = timezone
        self.player_thread = player_thread
        self.stream_mode = stream_mode
        self.server_audio = server_audio
        self.first_track = first_track
        self.paths = StationPaths.default(bridge.player._cfg)
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

    @property
    def cfg(self) -> AutoDJConfig:
        """The station's configuration."""
        return self.bridge.player._cfg

    @property
    def player(self) -> Any:
        """The station's player."""
        return self.bridge.player

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
            )
            self._stream_ticker = asyncio.create_task(tick_stream_workers(bridge))
        elif self.server_audio:
            await asyncio.to_thread(start_server_audio_liners, bridge, self.paths.liners)
            self._stream_ticker = asyncio.create_task(tick_stream_workers(bridge))
        if bridge.liner_scheduler is not None:
            # The time check after the hour plays where an automatic liner would.
            bridge.liner_scheduler.announcement = self.time_checks.due_audio
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


class StationRegistry:
    """The stations of one server; today only the default station."""

    def __init__(self, default: StationRuntime) -> None:
        self._stations = {default.slug: default}
        self._default = default

    @property
    def default(self) -> StationRuntime:
        """The station the root config sections describe."""
        return self._default

    def get(self, slug: str) -> StationRuntime:
        """The station named *slug*.

        Raises:
            KeyError: No station has that name.
        """
        return self._stations[slug]

    def all(self) -> tuple[StationRuntime, ...]:
        """Every station, the default first."""
        return tuple(self._stations.values())

    def slugs(self) -> frozenset[str]:
        """The name of every configured station."""
        return frozenset(self._stations)

    async def shutdown(self, library: SharedLibrary, timeout_s: float) -> tuple[bool, bool]:
        """Stop every station, then close the shared library, then each master and push.

        Returns:
            Whether every cache writer stopped in time, and whether the
            DJ-meta cache closed.
        """
        writers_quiesced = True
        for station in self.all():
            writers_quiesced = await station.stop(timeout_s) and writers_quiesced
        cache_closed = await library.close(writers_quiesced)
        for station in self.all():
            await station.close()
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
            self.library.start_scan()
            self._ticker = asyncio.create_task(self._tick())

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
