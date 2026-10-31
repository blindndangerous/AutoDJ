"""The station runtime, its paths and the registry of one server (#70)."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from autodj import stations
from autodj._bridge import PlayerBridge
from autodj.breaks import BreakDesk, new_id
from autodj.carts import CartStore
from autodj.config import AutoDJConfig, load_config
from autodj.station_name import listener_name
from autodj.stations import (
    ExtraStation,
    SharedLibrary,
    StationPaths,
    StationRegistry,
    StationRuntime,
)
from autodj.stream import StreamOutput
from autodj.stream_mounts import StreamMounts
from tests._server import create_app
from tests.unit._fakes import FakeEncoder

from ._helpers import _make_player_mock, _make_sim_mock


def test_live_dj_title_reaches_extra_mounts() -> None:
    """A live DJ retitles every mount and ends future voice targets (ADR-0004-26)."""
    history: list[dict] = []
    bridge = SimpleNamespace(
        _history_lock=threading.Lock(),
        _open_play=None,
        _play_history=history,
        _keep_history_row=history.append,
        stream=MagicMock(),
        relays=MagicMock(),
        mounts=MagicMock(),
    )
    live = SimpleNamespace(status=lambda: {"dj": "DJ Kay", "since": 0.0}, title="Friday Mix")
    runtime = SimpleNamespace(
        bridge=bridge,
        live=live,
        ledger=None,
        _live_row=None,
        player=SimpleNamespace(break_desk=None, invalidate_voice_targets=MagicMock()),
        public_state_changed=lambda: None,
    )
    StationRuntime._live_started(runtime)
    bridge.mounts.set_title.assert_called_once_with("DJ Kay", "Friday Mix")
    runtime.player.invalidate_voice_targets.assert_called_once_with()
    assert [row["kind"] for row in history] == ["live"]


def _config(tmp_path: Path, *, clips: str | None = None) -> AutoDJConfig:
    cfg = load_config(environ={})
    cfg.index.index_dir = tmp_path / "index"
    cfg.time_checks.clips_folder = clips
    return cfg


def test_the_default_station_has_a_folder_like_every_other(tmp_path: Path) -> None:
    from autodj.relay import relay_status_path
    from autodj.time_checks import TimeChecks, clips_folder, state_path

    cfg = _config(tmp_path)
    index = tmp_path / "index"
    active = index / "stations" / "default"
    paths = StationPaths.default(cfg)

    assert paths.state_dir == active
    assert paths.web_state == active / "web_state.json"
    assert paths.schedule == active / "schedule.json"
    assert paths.carts == active / "carts.json"
    assert paths.cart_uploads == active / "carts"
    assert paths.time_checks == active / "time_checks.json"
    assert paths.time_checks_cache == active / "time_checks_cache"
    assert paths.time_check_clips == active / "time_checks"
    assert paths.listener_bans == active / "listener_bans.json"
    assert paths.recording == active / "recording.json"
    assert paths.relay_status == active / "relay-status.json"
    assert paths.clockwheels == active / "clockwheels"
    assert paths.liners == active / "liners"
    assert paths.profiles == active / "profiles"

    # The modules that keep these files ask StationPaths and get the same answer.
    store = CartStore(paths.state_dir)
    assert (store.path, store.uploads) == (paths.carts, paths.cart_uploads)
    assert state_path(cfg) == paths.time_checks
    assert clips_folder(cfg) == paths.time_check_clips
    assert relay_status_path(cfg) == paths.relay_status
    assert TimeChecks(cfg, "UTC").cache == paths.time_checks_cache


def test_a_configured_clip_folder_wins(tmp_path: Path) -> None:
    clips = tmp_path / "voice" / "clock"
    paths = StationPaths.default(_config(tmp_path, clips=str(clips)))
    assert paths.time_check_clips == clips


def test_paths_follow_a_changed_index_folder(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    paths = StationPaths.default(cfg)
    cfg.index.index_dir = tmp_path / "elsewhere"
    assert paths.schedule == tmp_path / "elsewhere" / "stations" / "default" / "schedule.json"


def test_the_app_registers_exactly_the_default_station() -> None:
    bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock())
    registry = create_app(bridge).state.stations
    assert [station.slug for station in registry.all()] == ["default"]
    assert registry.get("default") is registry.default
    assert registry.default.bridge is bridge
    assert registry.default.player is bridge.player
    assert registry.default.cfg is bridge.player._cfg
    with pytest.raises(KeyError):
        registry.get("nightowl")


async def test_registry_shutdown_stops_and_closes_every_stream_output() -> None:
    """ADR-0004-17. Registry shutdown reaches and then closes every station encoder."""
    bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock())
    registry = create_app(bridge).state.stations
    loop = asyncio.get_running_loop()
    main = StreamOutput(64, 8, encoder_factory=FakeEncoder, loop=loop)
    mounts = StreamMounts(
        main,
        lambda fmt, bitrate: StreamOutput(
            bitrate, 8, encoder_factory=FakeEncoder, loop=loop, fmt=fmt
        ),
        lambda _output: None,
        lambda _output: None,
    )
    relay = StreamOutput(64, 8, encoder_factory=FakeEncoder, loop=loop)
    mounts.start({"aac": 96})
    extra = mounts.acquire("aac")
    assert extra is not None
    bridge.attach_stream(
        stream=main,
        station=registry.default,
        relays=SimpleNamespace(outputs=[relay]),
        mounts=mounts,
    )

    registry.begin_shutdown()

    assert main._stop_recovery and extra._stop_recovery and relay._stop_recovery
    mounts.close()
    main.close()
    relay.close()
    assert not main._encoder.alive and not extra._encoder.alive and not relay._encoder.alive


class _Player:
    """Records being told to stop, and the wait for background analysis."""

    def __init__(self, log: list[str], cfg: AutoDJConfig) -> None:
        self._cfg = cfg
        self._log = log
        self._state = SimpleNamespace(should_stop=False, current_track=None)
        self._skip_event = SimpleNamespace(set=lambda: log.append("player told to stop"))
        self.bus = SimpleNamespace(remove_output=lambda out: log.append(f"{out.name} detached"))
        self._continuous_master = SimpleNamespace(close=lambda: log.append("master closed"))

    def wait_for_background_analysis(self, timeout: float) -> bool:
        self._log.append("background analysis waited for")
        return self._quiet

    _quiet = True


class _Thread:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def join(self, _timeout: float) -> None:
        self._log.append("player thread joined")

    def is_alive(self) -> bool:
        return False


def _rig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, log: list[str]
) -> tuple[StationRuntime, StationRegistry, SharedLibrary, Any]:
    """A started-up stream station with every part replaced by a recorder of calls."""
    player = _Player(log, _config(tmp_path))
    bridge: Any = SimpleNamespace(
        player=player,
        stream_mode=True,
        stream=None,
        relays=SimpleNamespace(
            outputs=[SimpleNamespace(name="relay encoder", stop_recovery=lambda: None)],
            close=lambda: log.append("relays closed"),
        ),
        mounts=SimpleNamespace(
            stop_recovery=lambda: None,
            close=lambda: log.append("extra formats closed"),
        ),
        liner_scheduler=None,
        station=None,
        library=None,
        scheduler_track_changed=None,
        shutdown_liner_worker=lambda: log.append("liner worker stopped"),
    )
    stream_out = SimpleNamespace(
        name="stream encoder",
        stop_recovery=lambda: None,
        close=lambda: log.append("stream closed"),
    )
    bridge.stream = stream_out

    class Recorder:
        def __init__(self, *_args: Any) -> None:
            pass

        def close(self) -> None:
            log.append("recorder closed")

    monkeypatch.setattr("autodj.recorder.Recorder", Recorder)
    monkeypatch.setattr(stations, "start_stream_station", lambda *_args: stream_out)
    monkeypatch.setattr("autodj.player.apply_repeat_windows", lambda _player: None)
    monkeypatch.setattr("autodj.dj_meta.close_cache", lambda: log.append("DJ-meta cache closed"))

    runtime = StationRuntime(bridge, timezone="UTC", player_thread=_Thread(log), stream_mode=True)

    async def tick() -> None:
        pass

    runtime.schedule = SimpleNamespace(start=lambda: None, track_changed=lambda: None, tick=tick)
    runtime.webhooks = SimpleNamespace(
        start=lambda: None, close=lambda: log.append("webhooks closed")
    )

    async def broadcast() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            log.append("push stopped")

    runtime.broadcast = broadcast

    def stop_scan(_timeout: float) -> bool:
        log.append("library scan stopped")
        return True

    library: Any = SimpleNamespace(
        start_scan=lambda: None,
        tick=lambda: None,
        stop_scan=stop_scan,
        flush_ledgers=lambda: log.append("plays saved"),
        close=lambda: log.append("library closed"),
    )
    registry = StationRegistry(
        ExtraStation(
            slug="default",
            name=runtime.name,
            autostart=True,
            paths=runtime.paths,
            preset="included",
            build=lambda _registry: runtime,
        )
    )
    return runtime, registry, SharedLibrary(library, registry), bridge


async def test_station_start_sweeps_old_break_files_before_starting_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []
    runtime, registry, shared, _bridge = _rig(tmp_path, monkeypatch, log)
    folder = tmp_path / "announcements"
    folder.mkdir()
    leftover = folder / f"brk-{new_id()}.wav"
    leftover.write_bytes(b"old break")
    runtime.breaks = BreakDesk(lambda: runtime.cfg.breaks, folder)

    def start_schedule() -> None:
        assert not leftover.exists()

    runtime.schedule.start = start_schedule
    await registry.start_all()
    assert not leftover.exists()
    await registry.shutdown(shared, 1.0)


async def test_relays_connect_with_the_name_on_the_air_and_reconnect_when_it_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0009-08: after a restart relays start with the name of what is on the air."""
    log: list[str] = []
    runtime, registry, shared, bridge = _rig(tmp_path, monkeypatch, log)
    bridge.player.clock_station_name = lambda: "Example FM Night Shift"
    seen: list[str] = []

    def start_stream(*_args: Any) -> Any:
        seen.append(listener_name(bridge))
        return SimpleNamespace(name="stream encoder", close=lambda: log.append("stream closed"))

    monkeypatch.setattr(stations, "start_stream_station", start_stream)
    reconnected: list[tuple[bool, bool]] = []

    def reconfigure(_configs: Any, *, restart_all: bool = False) -> None:
        # Another thread cannot take the relay lock while this runs.
        taken: list[bool] = []
        other = threading.Thread(target=lambda: taken.append(runtime.relay_lock.acquire(False)))
        other.start()
        other.join()
        reconnected.append((restart_all, taken == [False]))

    bridge.relays.reconfigure = reconfigure
    runtime.public_state_changed = lambda: log.append("feed refreshed")
    await registry.start_all()
    # The ticker would also notice the change below; this test makes it once.
    runtime._schedule_ticker.cancel()
    assert seen == ["Example FM Night Shift"]
    # Relays not yet connected when the name was set: they start with it.
    assert reconnected == []
    assert runtime.relay_reconnect_due(now=math.inf) is False
    # A wheel switch, then a show just after it: the feed follows each at
    # once, the relays reconnect once, after the name settles, with the last.
    bridge.player.clock_station_name = lambda: ""
    assert runtime.sync_station_name() is True
    runtime.show_runner = SimpleNamespace(on_air_station_name=lambda: "Example FM Live")
    assert runtime.sync_station_name() is True
    assert log.count("feed refreshed") == 3
    assert reconnected == []
    assert runtime.reconnect_relays_if_due() is False  # not settled yet
    assert await asyncio.to_thread(runtime.reconnect_relays_if_due, math.inf) is True
    assert reconnected == [(True, True)]
    assert runtime.relay_name == "Example FM Live"
    assert runtime.reconnect_relays_if_due(math.inf) is False  # nothing more booked
    # A name that comes back before the reconnect is due costs no reconnect.
    runtime.show_runner = SimpleNamespace(on_air_station_name=lambda: "")
    assert runtime.sync_station_name() is True
    runtime.show_runner = SimpleNamespace(on_air_station_name=lambda: "Example FM Live")
    assert runtime.sync_station_name() is True
    assert runtime.reconnect_relays_if_due(math.inf) is False
    assert reconnected == [(True, True)]
    await registry.shutdown(shared, 1.0)


async def test_shutdown_keeps_the_order_of_the_single_station_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []
    runtime, registry, shared, bridge = _rig(tmp_path, monkeypatch, log)
    await registry.start_all()
    await shared.start()
    assert bridge.scheduler_track_changed is not None

    assert await registry.shutdown(shared, 1.0) == (True, True)

    assert log == [
        "webhooks closed",
        "extra formats closed",
        "relay encoder detached",
        "relays closed",
        "stream encoder detached",
        "stream closed",
        "liner worker stopped",
        "recorder closed",
        "player told to stop",
        "player thread joined",
        "background analysis waited for",
        "library scan stopped",
        "DJ-meta cache closed",
        "library closed",
        "master closed",
        "push stopped",
    ]
    assert bridge.scheduler_track_changed is None
    assert bridge.player._state.should_stop is True
    assert runtime.recorder is None


async def test_a_busy_writer_leaves_the_cache_open_but_still_closes_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []
    _runtime, registry, shared, _bridge = _rig(tmp_path, monkeypatch, log)
    monkeypatch.setattr(_Player, "_quiet", False)
    await registry.start_all()
    await shared.start()

    assert await registry.shutdown(shared, 0.0) == (False, False)

    assert "DJ-meta cache closed" not in log
    assert log[-3:] == ["library closed", "master closed", "push stopped"]


async def test_a_reader_still_running_keeps_the_library_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An hour preview that did not stop must not have the library closed under it (#126)."""
    log: list[str] = []
    _runtime, registry, shared, _bridge = _rig(tmp_path, monkeypatch, log)
    await registry.start_all()
    await shared.start()

    assert await registry.shutdown(shared, 1.0, readers_stopped=False) == (True, True)

    assert "library closed" not in log
    assert log[-3:] == ["plays saved", "master closed", "push stopped"]


async def test_a_scan_that_will_not_stop_keeps_the_cache_and_library_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ADR-0008-31. Closing the DJ-meta cache under the scan would lose its writes.

    The plays since the last tick are saved all the same.
    """
    log: list[str] = []
    _runtime, registry, shared, _bridge = _rig(tmp_path, monkeypatch, log)
    waits: list[float] = []

    def stop_scan(timeout: float) -> bool:
        waits.append(timeout)
        return False

    shared.library.stop_scan = stop_scan
    await registry.start_all()
    await shared.start()
    shared.stop_scan(0.5)

    assert await registry.shutdown(shared, 1.0) == (True, False)

    # Asked once, when shutdown began: the wait is not added to the stations' stop.
    assert waits == [0.5]
    assert "DJ-meta cache closed" not in log
    assert "library closed" not in log
    assert "plays saved" in log
    assert "The library scan was still running 0.50 seconds" in caplog.text


async def test_each_shutdown_step_logs_how_long_it_took(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ADR-0008-31. Quick steps are logged at DEBUG; one slower than the threshold at INFO."""
    caplog.set_level(logging.DEBUG, logger="autodj.stations")

    async def step() -> str:
        return "done"

    assert await stations.timed_shutdown_step("stopping something quick", step()) == "done"
    monkeypatch.setattr(stations, "SLOW_SHUTDOWN_STEP_S", -1.0)
    assert await stations.timed_shutdown_step("stopping something slow", step()) == "done"

    levels = {
        record.getMessage().split(" took ")[0]: record.levelno
        for record in caplog.records
        if record.getMessage().startswith("Shutdown step ")
    }
    assert levels == {
        "Shutdown step stopping something quick": logging.DEBUG,
        "Shutdown step stopping something slow": logging.INFO,
    }


def _scan_rig(monkeypatch: pytest.MonkeyPatch, players: list[Any]) -> tuple[SharedLibrary, list]:
    scans: list[str] = []
    monkeypatch.setattr("autodj.player.apply_repeat_windows", lambda _player: None)
    library: Any = SimpleNamespace(
        start_scan=lambda: scans.append("scan"),
        tick=lambda: None,
        stop_scan=lambda _timeout: True,
    )
    registry: Any = SimpleNamespace(all=lambda: [SimpleNamespace(player=p) for p in players])
    return SharedLibrary(library, registry), scans


async def test_the_startup_scan_waits_for_every_station_s_first_song(monkeypatch) -> None:
    """ADR-0008-35. The scan starts once each station with a mix bus has started a song."""
    first, second = (
        SimpleNamespace(bus=object(), first_song_started=threading.Event()) for _ in range(2)
    )
    browser = SimpleNamespace(bus=None, first_song_started=threading.Event())
    shared, scans = _scan_rig(monkeypatch, [first, second, browser])
    await shared.start()
    await asyncio.sleep(0.4)
    first.first_song_started.set()
    await asyncio.sleep(0.4)
    assert scans == []
    second.first_song_started.set()
    await asyncio.sleep(0.4)
    assert scans == ["scan"]
    assert shared._ticker is not None
    shared._ticker.cancel()


async def test_the_startup_scan_starts_anyway_after_the_wait(monkeypatch) -> None:
    """ADR-0008-35. A station that never plays delays the scan by the wait at most."""
    monkeypatch.setattr(stations, "FIRST_SONG_WAIT_S", 0.3)
    idle = SimpleNamespace(bus=object(), first_song_started=threading.Event())
    shared, scans = _scan_rig(monkeypatch, [idle])
    await shared.start()
    await asyncio.sleep(0.8)
    assert scans == ["scan"]
    assert shared._ticker is not None
    shared._ticker.cancel()


async def test_a_shutdown_before_the_first_song_starts_no_scan(monkeypatch) -> None:
    """ADR-0008-35. Shutdown cancels a scan still waiting for the first songs."""
    waiting = SimpleNamespace(bus=object(), first_song_started=threading.Event())
    shared, scans = _scan_rig(monkeypatch, [waiting])
    await shared.start()
    shared.stop_scan(1.0)
    waiting.first_song_started.set()
    await asyncio.sleep(0.4)
    assert scans == []
    assert shared._ticker is not None
    shared._ticker.cancel()
