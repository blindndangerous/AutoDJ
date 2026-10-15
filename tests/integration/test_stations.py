"""The station runtime, its paths and the registry of one server (#70)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autodj import stations
from autodj._bridge import PlayerBridge
from autodj.breaks import BreakDesk, new_id
from autodj.carts import CartStore
from autodj.config import AutoDJConfig, load_config
from autodj.server import create_app
from autodj.stations import SharedLibrary, StationPaths, StationRegistry, StationRuntime

from ._helpers import _make_player_mock, _make_sim_mock


def _config(tmp_path: Path, *, clips: str | None = None) -> AutoDJConfig:
    cfg = load_config(environ={})
    cfg.index.index_dir = tmp_path / "index"
    cfg.index.name = "main"
    cfg.time_checks.clips_folder = clips
    return cfg


def test_default_paths_are_where_the_state_has_always_been(tmp_path: Path) -> None:
    from autodj.relay import relay_status_path
    from autodj.time_checks import TimeChecks, clips_folder, state_path

    cfg = _config(tmp_path)
    index = tmp_path / "index"
    active = index / "main"
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
    assert paths.relay_status == index / "relay-status.json"
    assert paths.clockwheels == active / "clockwheels"
    assert paths.liners == active / "liners"
    # Profiles are shared by every named index of the index folder.
    assert paths.profiles == index / "profiles"

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
    assert paths.schedule == tmp_path / "elsewhere" / "main" / "schedule.json"


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
        relays=SimpleNamespace(
            outputs=[SimpleNamespace(name="relay encoder")],
            close=lambda: log.append("relays closed"),
        ),
        mounts=SimpleNamespace(close=lambda: log.append("extra formats closed")),
        liner_scheduler=None,
        station=None,
        library=None,
        scheduler_track_changed=None,
        shutdown_liner_worker=lambda: log.append("liner worker stopped"),
    )
    stream_out = SimpleNamespace(name="stream encoder", close=lambda: log.append("stream closed"))

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
    library: Any = SimpleNamespace(
        start_scan=lambda: None, tick=lambda: None, close=lambda: log.append("library closed")
    )
    registry = StationRegistry(runtime)
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
    await runtime.start()
    assert not leftover.exists()
    await registry.shutdown(shared, 1.0)


async def test_shutdown_keeps_the_order_of_the_single_station_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []
    runtime, registry, shared, bridge = _rig(tmp_path, monkeypatch, log)
    await runtime.start()
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
    runtime, registry, shared, _bridge = _rig(tmp_path, monkeypatch, log)
    monkeypatch.setattr(_Player, "_quiet", False)
    await runtime.start()
    await shared.start()

    assert await registry.shutdown(shared, 0.0) == (False, False)

    assert "DJ-meta cache closed" not in log
    assert log[-3:] == ["library closed", "master closed", "push stopped"]


async def test_a_reader_still_running_keeps_the_library_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An hour preview that did not stop must not have the library closed under it (#126)."""
    log: list[str] = []
    runtime, registry, shared, _bridge = _rig(tmp_path, monkeypatch, log)
    await runtime.start()
    await shared.start()

    assert await registry.shutdown(shared, 1.0, readers_stopped=False) == (True, True)

    assert "library closed" not in log
    assert log[-2:] == ["master closed", "push stopped"]
