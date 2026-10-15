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
import inspect
import json
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autodj.station_log import CURRENT_STATION, DEFAULT_SLUG
from autodj.stereo import SAMPLE_RATE

if TYPE_CHECKING:
    from autodj._bridge import PlayerBridge
    from autodj.ads import AdDesk
    from autodj.breaks import BreakDesk
    from autodj.carts import StationCarts
    from autodj.clip_dates import LinerGate
    from autodj.config import AutoDJConfig, PlayerPageConfig
    from autodj.indexer import IndexEntry
    from autodj.library import LibraryManager
    from autodj.listener_access import ListenerAccess
    from autodj.listener_requests import RequestDesk
    from autodj.matched_intros import MatchedIntros
    from autodj.recorder import Recorder
    from autodj.scheduler import Scheduler
    from autodj.shows import ShowRunner, ShowStore
    from autodj.stream import StreamOutput
    from autodj.stream_formats import StreamFormat
    from autodj.stream_mounts import StreamMounts
    from autodj.time_checks import TimeChecks
    from autodj.webhooks import WebhookWorker

logger = logging.getLogger(__name__)

# A station's name: lower-case letters, digits and inner hyphens, at most 32
# characters.  It names a folder and a URL path segment.
SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")


def public_feed_path(slug: str, *, by_name: bool = True) -> str:
    """The address of station *slug*'s public now-playing feed.

    By name it is ``/api/stations/<slug>/nowplaying``; the default
    station's own address, ``/api/nowplaying``, otherwise.
    """
    return f"/api/stations/{slug}/nowplaying" if by_name else "/api/nowplaying"


def public_page_path(slug: str, page: str, *, by_name: bool = True) -> str:
    """The address of station *slug*'s public *page* (``status``, ``player`` or ``embed``).

    By name it is ``/stations/<slug>/<page>``; the default station's own
    address, ``/<page>``, otherwise.
    """
    return f"/stations/{slug}/{page}" if by_name else f"/{page}"


def public_addresses(
    slug: str, *, streams: bool, player_page: PlayerPageConfig | None = None
) -> dict[str, str | None]:
    """Station *slug*'s public addresses: its pages, feed, stream and playlist.

    The default station keeps the addresses it always had (``/status``,
    ``/api/nowplaying``, ``/stream.mp3``); every other station's are
    under its name.  The stream and playlist are ``None`` for a station
    that does not stream, and the player page and embeddable player
    ``None`` unless *player_page* turns them on.
    """
    by_name = slug != DEFAULT_SLUG
    stream = f"/stations/{slug}.mp3" if by_name else "/stream.mp3"
    player = player_page is not None and player_page.enabled
    embed = player_page is not None and player_page.embed
    return {
        "status_path": public_page_path(slug, "status", by_name=by_name),
        "nowplaying_path": public_feed_path(slug, by_name=by_name),
        "stream_path": stream if streams else None,
        "m3u_path": stream.removesuffix(".mp3") + ".m3u" if streams else None,
        "player_path": public_page_path(slug, "player", by_name=by_name) if player else None,
        "embed_path": public_page_path(slug, "embed", by_name=by_name) if embed else None,
    }


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
    def stations_dir(self) -> Path:
        """The folder containing named stations' state folders."""
        return Path(self._cfg.index.active_dir) / "stations"

    @property
    def web_state(self) -> Path:
        """Settings saved from the web page."""
        return self.state_dir / "web_state.json"

    @property
    def definition(self) -> Path:
        """A web-created station's configuration definition."""
        return self.state_dir / "station.json"

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
    def announcements(self) -> Path:
        """Breaks from the announcer program (#88), never in liner rotation."""
        return self.state_dir / "announcements"

    @property
    def ads(self) -> Path:
        """Ad spots, ad breaks and their settings (#51)."""
        return self.state_dir / "ads.json"

    @property
    def ad_spots(self) -> Path:
        """The ad spots' audio, never in liner rotation."""
        return self.state_dir / "ads"

    @property
    def ad_plays(self) -> Path:
        """The ad play log, one file a month, for the Ads report."""
        return self.state_dir / "ad_plays"

    @property
    def liner_dates(self) -> Path:
        """The voice liners' play dates."""
        return self.state_dir / "liner_dates.json"

    @property
    def listener_requests(self) -> Path:
        """Listener requests: those waiting, and the recent decisions."""
        return self.state_dir / "listener_requests.json"

    @property
    def matched_intros(self) -> Path:
        """Intros matched to songs and artists (#127)."""
        return self.state_dir / "matched_intros.json"

    @property
    def shows(self) -> Path:
        """Shows: lists of songs prepared ahead (#106)."""
        return self.state_dir / "shows.json"

    @property
    def show_on_air(self) -> Path:
        """The show on the air and its place, so a restart resumes it (#106)."""
        return self.state_dir / "show_on_air.json"

    @property
    def intros(self) -> Path:
        """The audio of the matched intros."""
        return self.state_dir / "intros"

    @property
    def listener_bans(self) -> Path:
        """Banned listener addresses."""
        return self.state_dir / "listener_bans.json"

    @property
    def listener_stats(self) -> Path:
        """Daily listener counts: peaks, sessions and listening time, no addresses (#50)."""
        return self.state_dir / "listener_stats.json"

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
    def relays(self) -> Path:
        """Private web-managed relay definitions and source passwords."""
        return self.state_dir / "relays.json"

    @property
    def live_djs(self) -> Path:
        """Hashed credentials for this station's live source clients."""
        return self.state_dir / "live_djs.json"

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
    listener count and starts a set on the first listener.  The extra
    formats that are on (``[stream] aac_bitrate`` and ``opus_bitrate``)
    are checked with ffmpeg; each starts its own encoder on the same bus
    when its first listener comes, and their listeners count too
    (:mod:`autodj.stream_mounts`).  Configured
    relays (``[stream.relays.<name>]``) start connecting too; while any is
    enabled the station plays as if it always had a listener, because the
    relay's own listeners are invisible to AutoDJ.

    If any step fails, what was already made (encoders, relays, the liner
    worker) is closed before the error is raised, so a failed start leaves
    no ffmpeg process behind.

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
    from autodj.stream_mounts import StreamMounts

    player = bridge.player
    cfg = player._cfg
    from autodj.relay_settings import load_relays

    relay_paths = paths or StationPaths.default(cfg)
    cfg.stream.relays = load_relays(relay_paths.relays, cfg.stream.relays, relay_paths.slug)
    bus = player.bus
    # Every encoder made, the station's own first: each starts ffmpeg.
    made: list[StreamOutput] = []

    def make_output(bitrate: int, max_listeners: int = 0) -> StreamOutput:
        output = stream_module.StreamOutput(
            bitrate, max_listeners, encoder_factory=stream_module.FfmpegEncoder, loop=loop
        )
        made.append(output)
        return output

    def make_mount(fmt: StreamFormat, bitrate: int) -> StreamOutput:
        return stream_module.StreamOutput(
            bitrate,
            cfg.stream.max_listeners,
            encoder_factory=functools.partial(stream_module.FfmpegEncoder, fmt=fmt),
            loop=loop,
            fmt=fmt,
        )

    relays = None
    mounts = None
    try:
        stream_out = make_output(cfg.stream.bitrate, cfg.stream.max_listeners)
        mounts = StreamMounts(
            stream_out,
            make_mount,
            bus.add_output,
            bus.remove_output,
            probe=stream_module.probe_encoder,
        )
        from autodj.relay import RelayManager, relay_status_path

        relays = RelayManager(
            cfg.stream.relays.values(),
            stream_out,
            make_output=make_output,
            station_name=lambda: cfg.stream.station_name,
            station_metadata=lambda: {
                "name": cfg.stream.station_name,
                "genre": cfg.stream.genre,
                "description": cfg.stream.description,
                "url": cfg.stream.url,
            },
            status_path=relay_status_path(cfg, paths),
            add_output=bus.add_output,
            remove_output=bus.remove_output,
        )
        station = Station(
            bus,
            mounts,
            player,
            cfg.stream.idle_grace_seconds,
            on_event=bridge.announce_station_event,
            forget_track=bridge.forget_track,
            continuous=lambda: (
                cfg.stream.continuous or relays.active or bool(recorder and recorder.wanted)
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
        bridge.attach_stream(stream=stream_out, station=station, relays=relays, mounts=mounts)
        bridge.attach_liner_scheduler(scheduler)
        if recorder is not None:
            stream_out.add_sink(recorder)
        bus.add_output(stream_out)
        for output in relays.outputs:
            bus.add_output(output)
        relays.start()
        mounts.start({"aac": cfg.stream.aac_bitrate, "opus": cfg.stream.opus_bitrate})
    except BaseException:
        _undo_stream_start(bridge, made, relays, mounts)
        raise
    return stream_out


def _undo_stream_start(
    bridge: PlayerBridge,
    made: Sequence[StreamOutput],
    relays: Any | None,
    mounts: StreamMounts | None = None,
) -> None:
    """Close what a failed :func:`start_stream_station` made: relays, encoders, liner worker.

    Each step is attempted even if an earlier one fails; the start's own
    error is the one raised.
    """
    bus = bridge.player.bus
    steps: list[tuple[str, Callable[[], None]]] = []
    if mounts is not None:
        steps.append(("stopping the extra formats", mounts.close))
    # The relays close their own encoders.
    theirs = {id(output) for output in relays.outputs} if relays is not None else set()
    for output in made:
        steps.append(("detaching", functools.partial(bus.remove_output, output)))
    if relays is not None:
        steps.append(("stopping the relays", relays.close))
    steps += [("closing", output.close) for output in made if id(output) not in theirs]
    steps.append(("stopping the liner worker for", bridge.shutdown_liner_worker))
    for what, step in steps:
        try:
            step()
        except Exception:
            logger.exception("A stream that failed to start: %s it failed too", what)
    bridge.attach_stream(stream=None, station=None, relays=None, mounts=None)


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


def prune_matched_intros(station: StationRuntime) -> list[str]:
    """Forget *station*'s intros for songs that are gone, with their audio; the IDs forgotten.

    Called only after a clean scan (see :class:`SharedLibrary`).  A song is
    gone when that scan found its file missing from a folder that is there,
    so a share that is not mounted forgets nothing.  An intro whose song has
    no library record (its source removed, say) is kept, and so is every
    artist intro: the artist may come back.
    """
    store = getattr(station, "intros", None)
    library = station.bridge.library
    if store is None or library is None:
        return []
    intros = store.all()
    gone = []
    for intro_id, intro in intros.items():
        if intro.track_id is None:
            continue
        record = library.catalog.record_for_id(intro.track_id)
        if record is not None and library.is_gone(record["path"]):
            gone.append(intro_id)
    forgotten = store.forget(gone) if gone else []
    if forgotten:
        logger.info(
            "Station %s: removed %d matched intros whose songs are gone: %s",
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
    # The AAC and Opus mounts leave the bus and close their own encoders.
    mounts = getattr(bridge, "mounts", None)
    mount_steps: list[tuple[str, Callable[[], None]]] = (
        [("stopping the extra formats", mounts.close)] if mounts is not None else []
    )
    steps: tuple[tuple[str, Callable[[], None]], ...] = (
        *mount_steps,
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
    """Tick the station, the liner scheduler and the extra formats once a second, apart.

    Each has its own loop, so a slow liner decode (up to 30 s through
    ffmpeg) never holds up the station's idle check, and a failure in one
    never stops the other.  Runs until cancelled.

    Args:
        bridge: The bridge holding the station and liner scheduler.
        sleep: ``asyncio.sleep`` replacement for tests.
    """
    workers = (
        ("station", bridge.station),
        ("liner scheduler", bridge.liner_scheduler),
        # Stops an extra format's encoder once nobody listens to it (#50).
        ("extra formats", getattr(bridge, "mounts", None)),
    )
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


def _reusable(task: asyncio.Future[Any] | None) -> bool:
    """Whether *task* is a stop or close to wait for: one under way, or one that ended well."""
    if task is None:
        return False
    if not task.done():
        return True
    return not task.cancelled() and task.exception() is None


async def _step(what: str, call: Callable[[], Any], problems: list[str]) -> None:
    """Run one step of a stop, awaiting it if it is a coroutine; log a failure and go on.

    A failure is also added to *problems*, in plain words, for the reply.
    """
    try:
        result = call()
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.exception("Stopping the station: %s failed", what)
        problems.append(f"{what[0].upper()}{what[1:]} failed; the server log has the details.")


def _close_master(master: Any, where: str) -> bool:
    """Close *master*; return whether its processes have all ended.  Never raises."""
    if master is None:
        return True
    try:
        # A stand-in master's close returns nothing: it has no process.
        return master.close() is not False
    except Exception:
        logger.exception("%s: closing its mastering process failed", where)
        return False


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
    breaks: BreakDesk
    ads: AdDesk
    intros: MatchedIntros
    shows: ShowStore
    show_runner: ShowRunner
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
        from autodj.listener_stats import ListenerStats
        from autodj.nowplaying import NowPlaying
        from autodj.webhooks import WebhookWorker

        self.slug = slug
        self.bridge = bridge
        self.defined_in = "config"
        self.timezone = timezone
        self.player_thread = player_thread
        self.stream_mode = stream_mode
        self.server_audio = server_audio
        self.first_track = first_track
        self.paths = paths if paths is not None else StationPaths.default(bridge.player._cfg)
        self.liner_gate = _liner_gate(bridge, self.paths)
        # Keep both station ownership and the expected air time when filtering liners.
        # A plain function avoids deep-copying the store with player settings.
        bridge.player.liner_filter = lambda entries, at=None: self._playable_liners(entries, at)
        self.nowplaying = NowPlaying(bridge, timezone=timezone)
        from autodj.live import LiveDesk

        # Live DJ input (#48): the source mount, DJ accounts and talkover.
        self._live_row: dict[str, Any] | None = None
        self.live = LiveDesk(
            self.paths.live_djs,
            on_start=self._live_started,
            before_return=self._ready_automation,
            on_end=self._live_ended,
        )
        self.nowplaying.live_status = self.live.status
        self.nowplaying.live_title = self.live.title
        # Only stream mode has a mix bus to take the DJ.
        if callable(attach := getattr(getattr(bridge.player, "bus", None), "set_live", None)):
            attach(self.live)
        self.webhooks: WebhookWorker = WebhookWorker(getattr(bridge.player._cfg, "webhooks", ()))
        streams = stream_mode or (isinstance(bridge, PlayerBridge) and bridge.stream_mode)
        stream_cfg = bridge.player._cfg.stream
        self.listener_access: ListenerAccess | None = (
            ListenerAccess(
                self.paths.listener_bans,
                stream_cfg.max_listeners_per_address,
                max_total=stream_cfg.max_listeners,
            )
            if streams
            else None
        )
        # Daily listener counts, no addresses (#50).
        self.listener_stats: ListenerStats | None = (
            ListenerStats(
                self.paths.listener_stats,
                timezone=timezone,
                keep_days=lambda: int(self.cfg.stream.stats_days),
            )
            if streams
            else None
        )
        self.recorder: Recorder | None = None
        # Listener requests, when [requests] is on (built by equip_station).
        self.requests: RequestDesk | None = None
        self.stream_out: StreamOutput | None = None
        self.ws_clients: set[Any] = set()
        self.ws_lock = asyncio.Lock()
        self._schedule_ticker: asyncio.Task[None] | None = None
        self._stream_ticker: asyncio.Task[None] | None = None
        self._broadcast_task: asyncio.Task[None] | None = None
        # The station's one stop and one close: a second call (shutdown
        # after a stop, or a stop queued behind shutdown) waits for the
        # first and does nothing more.
        self._stop_task: asyncio.Future[bool] | None = None
        self._close_task: asyncio.Future[None] | None = None
        # What went wrong while stopping, in plain words, for the stop's reply.
        self.stop_problems: list[str] = []
        # Set when its mastering process outlived the close.
        self.master_left = False
        # Set while the station is stopped; its routes then answer 409.
        self.stopped = False
        # Whether a liner registered in the shared library is this station's
        # (set by the registry); with one station, every liner is.
        self.owns_liner: Callable[[str], bool] = _every_liner
        # Every station's entry, for the switcher in its pages' pushes (set
        # by the registry when there are several stations); none with one.
        self.station_list: Callable[[], list[dict[str, Any]]] | None = None
        # Scans in a row that found the liner folder empty (see prune_liner_dates).
        self.liner_folder_empty_scans = 0

    def _playable_liners(self, entries: Sequence[Any], at: float | None = None) -> Sequence[Any]:
        """This station's liners among *entries* that may play at the expected air time.

        *at* is the Unix time the pick expects to air, as the player's pick
        context gives it; ``None`` means now.
        """
        owns = self.owns_liner
        owned = [entry for entry in entries if owns(entry.path)]
        return self.liner_gate.playable(entries if len(owned) == len(entries) else owned, at)

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
            "defined_in": self.defined_in,
            "name": self.name,
            "state": state,
            "listeners": bridge.listener_total() if stream is not None else 0,
            "title": (
                f"{live['dj']}: {self.live.title}"
                if (live := self.live.status()) is not None
                else (getattr(track, "display_name", None) if track is not None else None)
            ),
            **public_addresses(
                self.slug, streams=bool(bridge.stream_mode), player_page=self.cfg.player_page
            ),
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

    def live_state(self) -> dict[str, Any]:
        """The live DJ and talkover state for the page's push and ``/api/status``."""
        return {"live": self.live.status(), "talkover": self.live.talkover}

    def _ready_automation(self, source: Any) -> None:
        """Ready automation to take back the air from a live DJ (#48).

        Runs on the releasing thread while the DJ's last audio is still
        playing.  The track rendered to follow the interrupted song waited
        through the live set, so it is rendered again for now (its liners
        and time checks were planned for then), keeping the old render to
        fall back on; this waits for the new one no longer than the DJ's
        buffered audio lasts, at most a second and a half.  Then the
        interrupted song is dropped, so automation comes back with the
        next song rather than the rest of that one.
        """
        player = self.player
        if self.stopped or player.bus is None or not player.bus.playing:
            return  # no set is held under the DJ: the station goes back to idle
        buffered = source.buffer.frames / SAMPLE_RATE
        try:
            player.prepare_live_return(min(1.5, buffered))
        finally:
            if player.bus is not None:
                player.bus.drop_held_track()

    def _live_started(self) -> None:
        """Record a DJ going on the air: History, the stream title and the feed."""
        bridge = self.bridge
        with bridge._history_lock:
            # Checked under the lock _live_ended takes, so a DJ who left at
            # once leaves no session open.
            status = self.live.status()
            if status is None:
                return
            row = {
                "event_id": secrets.token_hex(16),
                "path": None,
                "title": self.live.title,
                "artist": status["dj"],
                "kind": "live",
                "duration": 0.0,
                "played_at": status["since"],
                "ended_at": None,
                "performances": None,
            }
            self._live_row = row
            # The song the DJ cut off ends here; listeners who join now hear
            # the DJ, not it.
            interrupted = bridge._open_play[1] if bridge._open_play is not None else None
            bridge._open_play = None
            if interrupted is not None and interrupted.get("ended_at") is None:
                interrupted["ended_at"] = status["since"]
            bridge._play_history.append(row)
            if (ledger := self.ledger) is not None:
                if interrupted is not None:
                    ledger.remember_history(interrupted)
                ledger.remember_history(row)
        logger.info("Live DJ %s is on the air", status["dj"])
        # A voice break waiting for the next song change misses its slot (#88);
        # the missed and late rules of #215 will decide this in time.
        desk = getattr(self.player, "break_desk", None)
        if desk is not None:
            try:
                missed = desk.drop_unaired("live DJ on air")
                if isinstance(missed, int) and missed:
                    logger.info("%d planned break(s) missed: live DJ on air", missed)
            except Exception:
                logger.exception("Could not mark the planned breaks missed")
        for out in (bridge.stream, bridge.relays):
            if out is not None:
                try:
                    out.set_title(status["dj"], self.live.title)
                except Exception:
                    logger.exception("Could not set the stream title for the live DJ")
        self.public_state_changed()

    def _live_ended(self) -> None:
        """Close the live session's History row with its end time and length."""
        bridge = self.bridge
        with bridge._history_lock:
            row, self._live_row = self._live_row, None
            if row is not None:
                ended = datetime.now(UTC)
                row["ended_at"] = ended.isoformat()
                row["duration"] = max(
                    0.0, (ended - datetime.fromisoformat(row["played_at"])).total_seconds()
                )
                if (ledger := self.ledger) is not None:
                    ledger.remember_history(row)
        if row is not None:
            logger.info("Live DJ %s is off the air; automation is back", row["artist"])
        self.public_state_changed()

    async def start(self) -> None:
        """Start the schedule, the stream or liners, the WebSocket push and webhooks."""
        desk = getattr(self, "breaks", None)
        if desk is not None:
            try:
                await asyncio.to_thread(desk.maintain)
            except Exception:
                logger.exception("Break upkeep failed")
        ads = getattr(self, "ads", None)
        if ads is not None and not ads.enabled:
            try:
                # Ads off: no break is owed for this run when they come back on.
                await asyncio.to_thread(ads.station_started)
            except Exception:
                logger.exception("Ad schedule upkeep failed")
        self.stopped = False
        bridge = self.bridge
        cfg = self.cfg
        schedule = self.schedule
        schedule.start()
        loop = asyncio.get_running_loop()

        runner = getattr(self, "show_runner", None)

        def schedule_track_changed() -> None:
            # The mix-bus thread calls this; it only hands over to the loop.
            with contextlib.suppress(RuntimeError):  # the loop closed at shutdown
                loop.call_soon_threadsafe(schedule.track_changed)
                if runner is not None:
                    # A show on the air queues its next songs (#106).
                    loop.call_soon_threadsafe(runner.track_changed)

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
            if bridge.station is not None:
                bridge.station.live_active = lambda: self.live.active
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
        """Stop the station once; a later call waits for that stop and returns its result.

        See :meth:`_stop`.  The stop runs to its end even if the caller is
        cancelled, so it never leaves a half-stopped station.  A stop that
        ended in an error (it should not: every step logs and goes on) is
        not remembered, so the next call tries again.
        """
        if not _reusable(self._stop_task):
            self._stop_task = asyncio.ensure_future(self._stop(timeout_s))
        assert self._stop_task is not None
        return await asyncio.shield(self._stop_task)

    async def _stop(self, timeout_s: float) -> bool:
        """Stop the schedule, stream, recorder, liners and player, in that order.

        Listeners are disconnected and the encoder stopped first, then the
        station's ticks and the liner worker, then the player is told to
        leave its loop.  The player thread and every background analysis
        worker are then given up to *timeout_s* seconds to finish, because
        the shared DJ-meta cache must not close under a writer.  Each step
        is tried even if an earlier one failed, and nothing here raises:
        the rest of shutdown must still run.

        Returns:
            Whether every cache writer stopped in time.
        """
        self.stopped = True
        if (source := self.live.source) is not None:
            await _step(
                "disconnecting its live DJ",
                lambda: asyncio.to_thread(self.live.release, source),
                self.stop_problems,
            )
        problems = self.stop_problems
        bridge = self.bridge
        bridge.scheduler_track_changed = None
        await _step(
            "cancelling its schedule", lambda: _cancel(self._schedule_ticker, Exception), problems
        )
        # What the ad spots did up to now reaches the play log before the stop.
        ads = getattr(self, "ads", None)
        if ads is not None and ads.needs_flush():
            await _step(
                "writing its ad play log", functools.partial(asyncio.to_thread, ads.flush), problems
            )
        # A cart's end timer must not fire after the station has stopped.
        carts = getattr(self, "carts", None)
        if carts is not None:
            await _step("closing its carts", carts.close, problems)
        await _step("closing its webhooks", self.webhooks.close, problems)
        requests = getattr(self, "requests", None)
        if requests is not None:
            await _step("closing its listener requests", requests.close, problems)
        if self.stream_out is not None:
            await _step(
                "stopping its stream",
                functools.partial(stop_stream_station, bridge, self.stream_out),
                problems,
            )
        stats = getattr(self, "listener_stats", None)
        if stats is not None:
            await _step(
                "saving its listener statistics",
                functools.partial(asyncio.to_thread, stats.flush),
                problems,
            )
        if self.recorder is not None:
            await _step(
                "closing its recorder",
                functools.partial(asyncio.to_thread, self.recorder.close),
                problems,
            )
            self.recorder = None
        await _step("stopping its ticks", lambda: _cancel(self._stream_ticker, Exception), problems)
        if self.server_audio:
            await _step("stopping its liner worker", bridge.shutdown_liner_worker, problems)

        def signal_player() -> None:
            # A daemon thread, so the process can exit either way, but a clean
            # stop lets sounddevice release OS handles instead of being torn
            # down mid-call.
            bridge.player._state.should_stop = True
            bridge.player._skip_event.set()

        await _step("telling its player to stop", signal_player, problems)
        has_background_writers = hasattr(type(bridge.player), "wait_for_background_analysis")
        if self.player_thread is None and not has_background_writers:
            return True
        try:
            return await asyncio.to_thread(
                quiesce_cache_writers, bridge, self.player_thread, timeout_s
            )
        except Exception:
            logger.exception("Stopping the station: waiting for its player failed")
            problems.append("Waiting for its player failed; the server log has the details.")
            return False

    def player_running(self) -> bool:
        """Whether the station's player thread is still running."""
        thread = self.player_thread
        return thread is not None and thread.is_alive()

    def close_master(self) -> bool:
        """Close the mastering process (Stereo Tool or FFmpeg); never raises.

        Only once the player has ended: a player still running writes to it.

        Returns:
            Whether its processes have all ended.  When not, the station
            still holds Stereo Tool; call again until it returns ``True``.
        """
        gone = _close_master(
            getattr(self.bridge.player, "_continuous_master", None), f"Station {self.slug}"
        )
        self.master_left = not gone
        return gone

    def needs_holding(self) -> bool:
        """Whether its player or mastering process is still running after the close."""
        return self.player_running() or self.master_left

    async def close(self) -> None:
        """Close the mastering process and stop the WebSocket push, once; after :meth:`stop`.

        A player still running (it outlived the stop's timeout) keeps its
        mastering process; :class:`StationRegistry` closes it when the
        player ends, and the server kills any left at exit.
        """
        if not _reusable(self._close_task):
            self._close_task = asyncio.ensure_future(self._close())
        assert self._close_task is not None
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        try:
            if self.player_running():
                logger.warning(
                    "Station %s: its player is still running, so its mastering process "
                    "stays until the player ends",
                    self.slug,
                )
            elif getattr(
                self.bridge.player, "_continuous_master", None
            ) is not None and not await asyncio.to_thread(self.close_master):
                self.stop_problems.append(
                    "Its mastering process did not end at once; the station keeps Stereo "
                    "Tool until it does."
                )
        finally:
            # Awaited with its cancellation swallowed, so asyncio does not log
            # "Task was destroyed but it is pending" on a Ctrl+C exit.
            await _step(
                "stopping its page updates",
                lambda: _cancel(self._broadcast_task, Exception),
                self.stop_problems,
            )

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
            desk = getattr(self, "breaks", None)
            try:
                if desk is not None and desk.needs_maintenance():
                    await asyncio.to_thread(desk.maintain)
            except Exception:
                logger.exception("Break upkeep failed")
            ads = getattr(self, "ads", None)
            try:
                if ads is not None and ads.needs_tick():
                    await asyncio.to_thread(ads.tick)
            except Exception:
                logger.exception("Ad break upkeep failed")
            intros = getattr(self, "intros", None)
            try:
                if intros is not None and intros.needs_upkeep():
                    await asyncio.to_thread(intros.upkeep)
            except Exception:
                logger.exception("Matched intro upkeep failed")


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
    #: Whether its ``[requests]`` page is on, so the page answers while it is stopped.
    requests_enabled: bool = False
    #: Its ``[player_page]`` settings, so its pages answer while it is stopped.
    player_page: PlayerPageConfig | None = None
    defined_in: str = "config"


@dataclass
class AbandonedPlayer:
    """The player of a station that failed to start, which would not stop in time.

    Held by :meth:`StationRegistry.hold_until_player_ends` like a stuck
    station: it keeps its mastering process, and Stereo Tool, until it ends.
    """

    slug: str
    name: str
    player: Any
    player_thread: threading.Thread
    cfg: AutoDJConfig

    def player_running(self) -> bool:
        """Whether the player thread is still running."""
        return self.player_thread.is_alive()

    def uses_stereo_tool(self) -> bool:
        """Whether its mastering process may be Stereo Tool."""
        modes = {
            getattr(self.player, "_active_processing_mode", None),
            self.cfg.playback.loudness_processing,
        }
        return (
            getattr(self.player, "_continuous_master", None) is not None and "stereotool" in modes
        )

    def close_master(self) -> bool:
        """Close the mastering process; return whether its processes have ended.  Never raises."""
        return _close_master(
            getattr(self.player, "_continuous_master", None), "A station that failed to start"
        )


class StoppedStation:
    """What the registry answers for a configured station that is not running.

    Its routes answer 409 and its stream 503: ``stopped`` is always set.
    """

    stopped = True

    def __init__(
        self, extra: ExtraStation, *, state: str = "stopped", name: str | None = None
    ) -> None:
        self.slug = extra.slug
        self.defined_in = extra.defined_in
        # The name it last had: one changed in Settings is in its saved
        # settings, not in its configuration.
        self.name = name or extra.name
        self.requests_enabled = extra.requests_enabled
        self.player_page = extra.player_page
        # "stopped", "stopping" (closing down: its routes already answer
        # 409) or "stuck stopping" (its player outlived the stop and still
        # runs, with its mastering process, until it ends).
        self.state = state

    def summary(self) -> dict[str, Any]:
        """The station's entry in ``/api/stations``."""
        return {
            "slug": self.slug,
            "defined_in": self.defined_in,
            "name": self.name,
            "state": self.state,
            "listeners": 0,
            "title": None,
            **public_addresses(self.slug, streams=True, player_page=self.player_page),
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


@functools.lru_cache(maxsize=4096)
def _normalized(path: str | Path) -> str:
    """*path* as the file system resolves it: symbolic links and Windows short (8.3) names
    followed, case-folded on Windows.

    The catalog keeps a path as it was spelled when its folder was added,
    so a liner folder reached through a link or a short name must be
    resolved before two paths are compared.  Cached, up to 4096 paths: a
    liner check runs for every liner at each pick.  The cache is emptied at
    each library scan (:func:`forget_resolved_paths`), so a link changed
    since is followed again.
    """
    return os.path.normcase(os.path.realpath(os.path.expanduser(path)))


def forget_resolved_paths() -> None:
    """Empty the cache of resolved liner paths; the library calls it when it scans."""
    _normalized.cache_clear()


def _inside(path: str, root: str) -> bool:
    try:
        relative = os.path.relpath(path, root)
    except ValueError:  # another drive on Windows
        return False
    return relative != os.curdir and not (
        relative == os.pardir or relative.startswith(os.pardir + os.sep) or os.path.isabs(relative)
    )


def _saved_station_name(paths: StationPaths) -> str | None:
    """The station name a stopped station's saved settings give it, if any and valid.

    Settings, Stream saves a new name there, not in the station's configuration.
    """
    from pydantic import ValidationError

    from autodj.settings_bodies import StreamSettingsBody

    try:
        data = json.loads(paths.web_state.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    playback = data.get("playback") if isinstance(data, dict) else None
    name = playback.get("stream_station_name") if isinstance(playback, dict) else None
    if not isinstance(name, str):
        return None
    try:
        return StreamSettingsBody.model_validate({"station_name": name}, strict=True).station_name
    except ValidationError:
        return None


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
        # The name each stopped station shows: its saved one, read the first
        # time it is asked for, then the name it had when it last stopped (it
        # changes only while the station runs).
        self._stopped_names: dict[str, str] = {}
        self._running: dict[str, StationRuntime] = {default.slug: default}
        # One start or stop of a station at a time.  Each station has its
        # own lock, held for the whole start or stop, so one station's
        # slow stop never holds up another station's start.  Stereo Tool
        # is guarded by the claim below, not by these locks.
        self._locks: dict[str, asyncio.Lock] = {slug: asyncio.Lock() for slug in self._extras}
        # Set once shutdown begins: no station starts after that.
        self._closing = False
        self.relay_lock = threading.RLock()
        # Stations being stopped.  From the moment a stop is asked for,
        # their routes answer 409 at once and a start is refused at once,
        # instead of waiting while the player finishes (up to
        # stop_timeout_s).
        self._stopping: set[str] = set()
        # Mastering lifecycle bookkeeping, inspected by lifecycle tests and
        # held around changes to the four collections:
        # _running, _starting, _stopping_runtimes and _stuck.  So a check
        # sees each station in exactly one of them, never in none.
        self.stereo_tool_lock = threading.RLock()
        # A station being torn down, out of _running: it still holds
        # Stereo Tool until its mastering process has closed.
        self._stopping_runtimes: dict[str, StationRuntime] = {}
        # A station whose player outlived its stop: its player and
        # mastering process run on (closing the master under a running
        # player is unsafe), so it keeps Stereo Tool until the player ends.
        self._stuck: dict[str, StationRuntime | AbandonedPlayer] = {}
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
        # While a station is stuck stopping: how often the log says so,
        # and how often its mastering process is closed again.
        self.stuck_warning_s = 60.0
        self.stuck_retry_s = 5.0

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
        if running is not None and slug not in self._stopping:
            return running
        extra = self._extras[slug]
        name = self._stopped_names.get(slug)
        if name is None:
            name = self._stopped_names[slug] = _saved_station_name(extra.paths) or extra.name
        if slug in self._stopping:
            return StoppedStation(extra, state="stopping", name=name)
        if slug in self._stuck:
            return StoppedStation(extra, state="stuck stopping", name=name)
        return StoppedStation(extra, name=name)

    def all(self) -> tuple[StationRuntime, ...]:
        """Every running station, the default first."""
        return tuple(self._running.values())

    def relay_configs(self) -> dict[str, dict[str, Any]]:
        """Current relay definitions for every configured station, including stopped ones."""
        from autodj.relay_settings import load_relays

        result = {}
        for slug in self.slugs():
            runtime = self._running.get(slug)
            paths = (
                runtime.paths
                if runtime is not None
                else (
                    self._default.paths if slug == self._default.slug else self._extras[slug].paths
                )
            )
            baseline = (
                runtime.cfg.stream.relays if runtime is not None else paths._cfg.stream.relays
            )
            result[slug] = load_relays(paths.relays, baseline, slug)
        return result

    def slugs(self) -> frozenset[str]:
        """The name of every configured station, running or not."""
        return frozenset({self._default.slug, *self._extras})

    def add(self, extra: ExtraStation) -> None:
        """Register a stopped station without touching running stations."""
        if self._closing:
            raise StationControlError("The server is shutting down")
        if extra.slug in self.slugs():
            raise StationControlError(f"Station {extra.slug} already exists")
        self._extras[extra.slug] = extra
        self._stopped_names.pop(extra.slug, None)
        self._locks[extra.slug] = asyncio.Lock()

    def paths(self) -> tuple[StationPaths, ...]:
        """Every configured station's paths, the default first."""
        return (self._default.paths, *(extra.paths for extra in self._extras.values()))

    def summaries(self) -> list[dict[str, Any]]:
        """Every configured station's name, state, listeners, song and stream address."""
        return [self.get(slug).summary() for slug in (self._default.slug, *self._extras)]

    def owns_liner(self, slug: str, path: str) -> bool:
        """Whether the registered liner at *path* is station *slug*'s.

        A liner belongs to the station whose liner folder holds it most
        closely: when one station's folder sits inside another's, the
        inner folder's liners are the inner station's only.  The default
        station also has every liner that is in no station's folder, as
        before there were several stations.  Stations given the same
        folder share its liners.
        """
        if not self._extras:
            return True
        where = _normalized(path)
        roots = {
            self._default.slug: _normalized(self._default.paths.liners),
            **{other: _normalized(extra.paths.liners) for other, extra in self._extras.items()},
        }
        holding = [root for root in roots.values() if _inside(where, root)]
        if not holding:
            return slug == self._default.slug
        return roots.get(slug) == max(holding, key=len)

    def stereo_tool_user(self, exclude: str) -> str | None:
        """The station other than *exclude* that uses Stereo Tool, in words, or ``None``.

        A running station, one being started whose settings choose it, one
        still stopping, or one whose player outlived its stop (its Stereo
        Tool runs until then); for the last two, the words say so and to
        try again shortly.  The check holds :attr:`stereo_tool_lock`, as
        every move between these does, so a check from another thread
        always finds the station.
        """
        with self.stereo_tool_lock:
            for station in self.all():
                if station.slug != exclude and station.uses_stereo_tool():
                    return station.name
            for slug, cfg in self._starting.items():
                if slug != exclude and cfg.playback.loudness_processing == "stereotool":
                    return str(cfg.stream.station_name)
            for slug, station in self._stopping_runtimes.items():
                if slug != exclude and station.uses_stereo_tool():
                    return (
                        f"{station.name}, which is still stopping (try again in a few seconds, "
                        "once it has stopped)"
                    )
            for slug, held in self._stuck.items():
                if slug != exclude and held.uses_stereo_tool():
                    return (
                        f"{held.name}, which has not finished stopping: its player is still "
                        "running (try again shortly, or restart AutoDJ if this lasts)"
                    )
            return None

    def mark_starting(self, slug: str, cfg: AutoDJConfig) -> None:
        """Record station *slug* while its runtime is being built."""
        with self.stereo_tool_lock:
            self._starting[slug] = cfg

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
        self._refuse_while_stopping(extra)
        # Shielded: a request cancelled at shutdown must not leave a
        # half-built station running outside the registry.
        return await _shielded(self._start_locked(extra))

    def _refuse_while_stopping(self, extra: ExtraStation) -> None:
        """Refuse to start *extra* while its last run is still stopping."""
        if extra.slug in self._stopping:
            raise StationControlError(
                f"{extra.name} is still stopping. Start it again once it has stopped."
            )
        if extra.slug in self._stuck:
            raise StationControlError(
                f"{extra.name} has not finished stopping: its player is still running. "
                "Try again shortly, or restart AutoDJ if this lasts."
            )

    async def _start_locked(self, extra: ExtraStation) -> StationRuntime:
        slug = extra.slug
        async with self._locks[slug]:
            if self._closing:
                raise StationControlError("The server is shutting down.")
            # Again, now the lock is held: a stop may have begun meanwhile.
            self._refuse_while_stopping(extra)
            running = self._running.get(slug)
            if running is not None:
                return running
            try:
                running = await _run_as(slug, self._start_extra(extra))
                with self.stereo_tool_lock:
                    self._running[slug] = running
                    self._starting.pop(slug, None)
            finally:
                # A failed start gives up its claim.
                with self.stereo_tool_lock:
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

    async def stop(self, slug: str) -> list[str]:
        """Stop station *slug* completely: listeners, encoders, relays and player.

        Returns:
            What went wrong, in plain words; empty when everything stopped.

        Raises:
            KeyError: No station has that name.
            StationControlError: *slug* is the default station.
        """
        if slug == self._default.slug:
            raise StationControlError("The default station cannot be stopped.")
        extra = self._extras[slug]
        if slug in self._stopping:
            return []  # already stopping; its summary says so
        self._stopping.add(slug)
        # Shielded, like a start: a stop cut off half-way would leave its
        # player and mastering process running outside the registry.
        return await _shielded(self._stop_locked(extra))

    async def _stop_locked(self, extra: ExtraStation) -> list[str]:
        slug = extra.slug
        warnings: list[str] = []
        try:
            # Its pages learn at once (4409), not after the player has
            # finished; the station refuses new sockets from now on.
            current = self._running.get(slug)
            if current is not None:
                current.stopped = True
                await self._disconnect(current)
            async with self._locks[slug]:
                runtime = self._running.get(slug)
                if runtime is not None:
                    with self.stereo_tool_lock:
                        self._stopping_runtimes[slug] = runtime
                        del self._running[slug]
                    self._stopped_names[slug] = runtime.name
                    warnings = await _run_as(slug, self._stop_extra(runtime, extra))
        finally:
            with self.stereo_tool_lock:
                self._stopping_runtimes.pop(slug, None)
            self._stopping.discard(slug)
        return warnings

    async def _stop_extra(self, runtime: StationRuntime, extra: ExtraStation) -> list[str]:
        warnings = await self._teardown(runtime)
        if warnings:
            logger.warning(
                "Station %s (%s) stopped with problems: %s",
                extra.slug,
                extra.name,
                " ".join(warnings),
            )
        else:
            logger.info("Station %s (%s) stopped", extra.slug, extra.name)
        return warnings

    async def _teardown(self, runtime: StationRuntime) -> list[str]:
        """Stop and close *runtime*, then close its pages' sockets; never raises.

        The close runs even if the stop failed.  A player that outlives
        the stop, or a mastering process that outlives the close, keeps
        Stereo Tool: the station is held as "stuck stopping" until both
        have ended.

        Returns:
            What went wrong, in plain words, for the stop's reply.
        """
        warnings: list[str] = []
        try:
            if not await runtime.stop(self.stop_timeout_s):
                logger.warning(
                    "Station %s: the player did not finish within %.0f seconds",
                    runtime.slug,
                    self.stop_timeout_s,
                )
                if runtime.player_running():
                    warnings.append(
                        f"Its player did not finish within {self.stop_timeout_s:.0f} seconds. "
                        "It is listed as stuck stopping, and keeps its mastering process and "
                        "Stereo Tool, until it ends."
                    )
        except Exception:
            logger.exception("Station %s: stopping it failed", runtime.slug)
            warnings.append("Stopping it failed; the server log has the details.")
        finally:
            try:
                await runtime.close()
            except Exception:
                logger.exception("Station %s: closing it failed", runtime.slug)
                warnings.append("Closing it failed; the server log has the details.")
            try:
                if runtime.needs_holding():
                    self.hold_until_player_ends(runtime)
            except Exception:
                logger.exception(
                    "Station %s: holding it until its player ends failed", runtime.slug
                )
                warnings.append("Keeping it until its player ends failed; restart AutoDJ.")
            await self._disconnect(runtime)
        return [*getattr(runtime, "stop_problems", []), *warnings]

    def hold_until_player_ends(self, runtime: StationRuntime | AbandonedPlayer) -> None:
        """Keep *runtime* as "stuck stopping", holding Stereo Tool, until its player ends.

        Then its mastering process is closed, and closed again until every
        process of it has ended, and only then is the station free.  Safe
        from any thread.
        """
        slug = runtime.slug
        with self.stereo_tool_lock:
            self._stuck[slug] = runtime
        logger.warning(
            "Station %s: its player is still running; its mastering process and Stereo Tool "
            "stay until the player ends",
            slug,
        )

        def wait() -> None:
            # No time limit: only a restart frees a player that never ends.
            # Until then, a warning every stuck_warning_s says it is still stuck.
            thread = runtime.player_thread
            while thread is not None and thread.is_alive():
                thread.join(self.stuck_warning_s)
                if thread.is_alive():
                    logger.warning(
                        "Station %s is still stuck stopping: its player has not ended. "
                        "Restart AutoDJ to free it.",
                        slug,
                    )
            # The player has ended; its mastering process must end too
            # before Stereo Tool is free.
            last_warning = time.monotonic()
            while not runtime.close_master():
                if time.monotonic() - last_warning >= self.stuck_warning_s:
                    last_warning = time.monotonic()
                    logger.warning(
                        "Station %s is still stuck stopping: its mastering process has not "
                        "ended. Restart AutoDJ to free it.",
                        slug,
                    )
                time.sleep(self.stuck_retry_s)
            with self.stereo_tool_lock:
                if self._stuck.get(slug) is runtime:
                    del self._stuck[slug]
            logger.info("Station %s: its player has ended; its mastering process is closed", slug)

        threading.Thread(target=wait, name=f"autodj-stuck-{slug}", daemon=True).start()

    async def _disconnect(self, runtime: StationRuntime) -> None:
        """Close the sockets of *runtime*'s pages (4409); never raises."""
        if self.disconnect is not None:
            try:
                await self.disconnect(runtime)
            except Exception:
                logger.exception("Could not close the pages of station %s", runtime.slug)

    async def shutdown(
        self, library: SharedLibrary, timeout_s: float, *, readers_stopped: bool = True
    ) -> tuple[bool, bool]:
        """Stop every station, then close the shared library, then each master and push.

        With *readers_stopped* false (a reader such as an hour preview is
        still reading the library) the library is left open.

        Returns:
            Whether every cache writer stopped in time, and whether the
            DJ-meta cache closed.
        """
        # After any start or stop under way, so a station starting now is
        # stopped too and one stopping now has finished; none starts later.
        # A stop asked for after this finds the station already stopped
        # (a runtime stops and closes once).
        self._closing = True
        async with contextlib.AsyncExitStack() as held:
            for lock in self._locks.values():
                await held.enter_async_context(lock)
            writers_quiesced = True
            for station in self.all():
                quiet = await _run_as(station.slug, station.stop(timeout_s))
                writers_quiesced = quiet and writers_quiesced
            cache_closed = await library.close(writers_quiesced, readers_stopped=readers_stopped)
            for station in self.all():
                await _run_as(station.slug, station.close())
            # Nothing but the default station is running now.
            with self.stereo_tool_lock:
                for slug in list(self._running):
                    if slug != self._default.slug:
                        del self._running[slug]
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
        """After a clean scan (on its thread): forget liner dates and song intros now gone."""
        for station in self._stations.all():
            try:
                prune_liner_dates(station)
            except Exception:
                logger.exception("Station %s: could not tidy the liner dates", station.slug)
            try:
                prune_matched_intros(station)
            except Exception:
                logger.exception("Station %s: could not tidy the matched intros", station.slug)

    async def close(self, writers_quiesced: bool, *, readers_stopped: bool = True) -> bool:
        """Close the DJ-meta cache if no writer remains, then the library.

        The library stays open while a reader has not stopped
        (*readers_stopped* false): closing it under that reader would fail it.

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
            if readers_stopped:
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
