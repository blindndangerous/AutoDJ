"""Another station's paths, logs, registry and bridge (#70, PR 5)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autodj._bridge import PlayerBridge
from autodj.config import load_config, station_config
from autodj.settings_bodies import PlaybackSettingsBody
from autodj.station_log import (
    CURRENT_STATION,
    install_log_prefix,
    station_thread,
    thread_name,
)
from autodj.stations import (
    ExtraStation,
    StationControlError,
    StationPaths,
    StationRegistry,
    StoppedStation,
)
from autodj.stereotool_presets import PresetInUseError, delete_preset, preset_folder

CONFIG = """
[index]
index_dir = "{root}/index"
name = "main"

[stations.nightowl.recording]
folder = "{root}/recordings/nightowl"

[stations.daybreak.recording]
folder = "{root}/recordings/daybreak"

[stations.daybreak.playback]
liners_folder = "{root}/voice/daybreak"
stereotool_preset = "Day.sts"
"""


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(root=tmp_path.as_posix()), encoding="utf-8")
    return load_config(path, environ={})


def _spec(cfg: Any, slug: str) -> Any:
    return next(spec for spec in cfg.stations if spec.slug == slug)


# -- paths -------------------------------------------------------------------


def test_another_stations_state_is_under_its_own_folder(cfg: Any, tmp_path: Path) -> None:
    index = tmp_path / "index"
    own = index / "main" / "stations" / "nightowl"
    paths = StationPaths.for_station(_spec(cfg, "nightowl").cfg, "nightowl")
    assert paths.slug == "nightowl"
    assert not paths.is_default
    assert paths.state_dir == own
    for name, expected in (
        ("web_state", own / "web_state.json"),
        ("schedule", own / "schedule.json"),
        ("carts", own / "carts.json"),
        ("cart_uploads", own / "carts"),
        ("time_checks", own / "time_checks.json"),
        ("time_checks_cache", own / "time_checks_cache"),
        ("time_check_clips", own / "time_checks"),
        ("listener_bans", own / "listener_bans.json"),
        ("recording", own / "recording.json"),
        ("relay_status", own / "relay-status.json"),
        ("clockwheels", own / "clockwheels"),
        ("liners", own / "liners"),
        ("profiles", own / "profiles"),
    ):
        assert getattr(paths, name) == expected, name
    # The default station's files stay where they were.
    default = StationPaths.default(cfg)
    assert default.state_dir == index / "main"
    assert default.relay_status == index / "relay-status.json"
    assert default.profiles == index / "profiles"
    # A configured liner folder wins, as for the default station.
    daybreak = StationPaths.for_station(_spec(cfg, "daybreak").cfg, "daybreak")
    assert daybreak.liners == tmp_path / "voice" / "daybreak"


@pytest.mark.parametrize("slug", ["default", "../x", "Owl", "", "a/b"])
def test_only_another_stations_name_makes_extra_paths(cfg: Any, slug: str) -> None:
    with pytest.raises(ValueError, match="not another station"):
        StationPaths.for_station(cfg, slug)


def test_each_run_gets_a_fresh_copy_of_the_station_config(cfg: Any) -> None:
    spec = _spec(cfg, "nightowl")
    first = station_config(spec.cfg, cfg)
    first.playback.crossfade_seconds = 9.0
    second = station_config(spec.cfg, cfg)
    assert second.playback.crossfade_seconds == 3.0
    assert spec.cfg.playback.crossfade_seconds == 3.0
    # Shared sections follow the root, even one replaced after loading.
    cfg.server = type(cfg.server)(port=9000)
    assert station_config(spec.cfg, cfg).server is cfg.server
    assert second.index is cfg.index


# -- logs and threads ----------------------------------------------------------


def test_threads_and_log_lines_carry_the_station_name(caplog: pytest.LogCaptureFixture) -> None:
    install_log_prefix()
    install_log_prefix()  # once per process, however often asked
    log = logging.getLogger("autodj.test_station_runtime")
    seen: dict[str, Any] = {}

    def work() -> None:
        seen["name"] = threading.current_thread().name
        seen["inner"] = thread_name("autodj-inner")
        log.warning("tick failed")

    def as_station(slug: str | None) -> None:
        CURRENT_STATION.set(slug)
        thread = station_thread(target=work, name="autodj-worker")
        assert thread.daemon
        thread.start()
        thread.join(5)

    with caplog.at_level(logging.WARNING, logger="autodj.test_station_runtime"):
        runner = threading.Thread(target=as_station, args=("nightowl",))
        runner.start()
        runner.join(5)
        assert seen == {"name": "autodj-worker-nightowl", "inner": "autodj-inner-nightowl"}
        assert caplog.records[-1].getMessage() == "[nightowl] tick failed"
        assert caplog.records[-1].station == "nightowl"
        for slug in ("default", None):
            caplog.clear()
            runner = threading.Thread(target=as_station, args=(slug,))
            runner.start()
            runner.join(5)
            assert seen["name"] == "autodj-worker"
            assert caplog.records[-1].getMessage() == "tick failed"
    assert thread_name("autodj-player") == "autodj-player"


# -- the registry ---------------------------------------------------------------


class _Runtime:
    """A station runtime that records being started and stopped."""

    def __init__(self, slug: str, log: list[str], *, stereo: bool = False, fail: bool = False):
        self.slug = slug
        self.name = slug.title()
        self.stopped = False
        self._log = log
        self._stereo = stereo
        self._fail = fail
        self.paths = SimpleNamespace(liners=Path("liners") / slug)
        self.cfg = SimpleNamespace(playback=SimpleNamespace(stereotool_preset=f"{slug}.sts"))

    def uses_stereo_tool(self) -> bool:
        return self._stereo

    def player_running(self) -> bool:
        return False

    def summary(self) -> dict[str, Any]:
        return {"slug": self.slug, "state": "idle"}

    async def start(self) -> None:
        self._log.append(f"{self.slug} started")
        if self._fail:
            raise RuntimeError("the encoder would not start")

    async def stop(self, _timeout: float) -> bool:
        self.stopped = True
        self._log.append(f"{self.slug} stopped")
        return True

    async def close(self) -> None:
        self._log.append(f"{self.slug} closed")


def _extra(slug: str, build: Any, tmp_path: Path, autostart: bool = True) -> ExtraStation:
    paths = SimpleNamespace(
        liners=tmp_path / slug / "liners", web_state=tmp_path / slug / "web_state.json"
    )
    return ExtraStation(
        slug=slug,
        name=slug.title(),
        autostart=autostart,
        paths=paths,  # type: ignore[arg-type]
        preset="included",
        build=build,
    )


def test_a_stopped_station_has_a_stand_in(tmp_path: Path) -> None:
    log: list[str] = []
    registry = StationRegistry(
        _Runtime("default", log),  # type: ignore[arg-type]
        [_extra("nightowl", lambda _r: _Runtime("nightowl", log), tmp_path)],
    )
    assert registry.slugs() == {"default", "nightowl"}
    stand_in = registry.get("nightowl")
    assert isinstance(stand_in, StoppedStation)
    assert stand_in.stopped
    assert [station.slug for station in registry.all()] == ["default"]
    assert registry.summaries()[1] == {
        "slug": "nightowl",
        "name": "Nightowl",
        "state": "stopped",
        "listeners": 0,
        "title": None,
        "stream_path": "/stations/nightowl.mp3",
    }
    with pytest.raises(KeyError):
        registry.get("daybreak")


def test_start_and_stop_build_and_tear_down_a_station(tmp_path: Path) -> None:
    log: list[str] = []
    built: list[_Runtime] = []
    prepared: list[str] = []
    disconnected: list[str] = []

    def build(_registry: StationRegistry) -> _Runtime:
        built.append(_Runtime("nightowl", log))
        return built[-1]

    async def disconnect(station: Any) -> None:
        disconnected.append(station.slug)

    registry = StationRegistry(
        _Runtime("default", log),  # type: ignore[arg-type]
        [_extra("nightowl", build, tmp_path, autostart=False)],
    )
    registry.prepare = lambda station: prepared.append(station.slug)
    registry.disconnect = disconnect

    async def scenario() -> None:
        await registry.start_all()
        assert log == ["default started"]
        first = await registry.start("nightowl")
        assert await registry.start("nightowl") is first  # already running
        assert registry.get("nightowl") is first
        await registry.stop("nightowl")
        await registry.stop("nightowl")  # already stopped
        assert registry.get("nightowl").stopped
        assert await registry.start("nightowl") is not first
        with pytest.raises(StationControlError, match="cannot be stopped"):
            await registry.stop("default")
        with pytest.raises(KeyError):
            await registry.start("daybreak")

    asyncio.run(scenario())
    assert log == [
        "default started",
        "nightowl started",
        "nightowl stopped",
        "nightowl closed",
        "nightowl started",
    ]
    assert prepared == ["nightowl", "nightowl"]
    # Once as the stop is asked for, and again after the teardown in case
    # a page connected in between.
    assert disconnected == ["nightowl", "nightowl"]


def test_a_station_that_fails_to_start_is_torn_down_and_left_stopped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log: list[str] = []

    def refuse(_registry: StationRegistry) -> _Runtime:
        raise StationControlError("Daybreak is set to use Stereo Tool")

    registry = StationRegistry(
        _Runtime("default", log),  # type: ignore[arg-type]
        [
            _extra("nightowl", lambda _r: _Runtime("nightowl", log, fail=True), tmp_path),
            _extra("daybreak", refuse, tmp_path),
        ],
    )
    with caplog.at_level(logging.ERROR, logger="autodj.stations"):
        asyncio.run(registry.start_all())
    # The default station runs on; each failure is logged, by station.
    assert [station.slug for station in registry.all()] == ["default"]
    assert log == ["default started", "nightowl started", "nightowl stopped", "nightowl closed"]
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "Station nightowl did not start: Nightowl could not start: the encoder would not start"
        in message
        for message in messages
    )
    assert "Station daybreak did not start: Daybreak is set to use Stereo Tool" in messages


def test_one_stereo_tool_and_every_stations_preset(tmp_path: Path) -> None:
    log: list[str] = []
    registry = StationRegistry(
        _Runtime("default", log, stereo=True),  # type: ignore[arg-type]
        [
            _extra("nightowl", lambda _r: _Runtime("nightowl", log), tmp_path),
            _extra("daybreak", lambda _r: _Runtime("daybreak", log), tmp_path),
        ],
    )
    assert registry.stereo_tool_user(exclude="nightowl") == "Default"
    assert registry.stereo_tool_user(exclude="default") is None
    saved = tmp_path / "nightowl" / "web_state.json"
    saved.parent.mkdir(parents=True)
    saved.write_text(json.dumps({"playback": {"stereotool_preset": "Owl.sts"}}), "utf-8")
    # Running: the live setting.  Stopped: the saved one, else the configured one.
    assert registry.stereo_tool_presets() == [
        ("Default", "default.sts"),
        ("Nightowl", "Owl.sts"),
        ("Daybreak", "included"),
    ]


def test_liners_belong_to_the_station_whose_folder_holds_them(tmp_path: Path) -> None:
    log: list[str] = []
    default = _Runtime("default", log)
    default.paths = SimpleNamespace(liners=tmp_path / "liners")
    registry = StationRegistry(
        default,  # type: ignore[arg-type]
        [_extra("nightowl", lambda _r: _Runtime("nightowl", log), tmp_path)],
    )
    owl_liner = str(tmp_path / "nightowl" / "liners" / "id.mp3")
    own_liner = str(tmp_path / "liners" / "id.mp3")
    added_folder = str(tmp_path / "elsewhere" / "id.mp3")
    assert registry.owns_liner("nightowl", owl_liner)
    assert not registry.owns_liner("nightowl", own_liner)
    assert not registry.owns_liner("nightowl", added_folder)
    assert registry.owns_liner("default", own_liner)
    assert registry.owns_liner("default", added_folder)  # as before: every other liner
    assert not registry.owns_liner("default", owl_liner)
    alone = StationRegistry(default)  # type: ignore[arg-type]
    assert alone.owns_liner("default", owl_liner)


# -- presets ----------------------------------------------------------------------


def test_a_preset_any_station_names_cannot_be_deleted(tmp_path: Path) -> None:
    folder = preset_folder(tmp_path)
    folder.mkdir(parents=True)
    (folder / "Owl.sts").write_text("[Processing]\nGain=0\n", encoding="utf-8")
    with pytest.raises(PresetInUseError) as refused:
        delete_preset(tmp_path, "Owl.sts", active=["included", "bad/name.sts", "Owl.sts"])
    assert refused.value.args == ("Owl.sts",)
    delete_preset(tmp_path, "Owl.sts", active=["included", "Other.sts"])
    assert not (folder / "Owl.sts").exists()


# -- the bridge --------------------------------------------------------------------


def _bridge(tmp_path: Path, **kwargs: Any) -> PlayerBridge:
    from tests.integration._helpers import _make_player_mock, _make_sim_mock

    player = _make_player_mock()
    player._cfg.index.active_dir = tmp_path / "main"
    return PlayerBridge(player=player, sim=_make_sim_mock(), **kwargs)


def test_another_stations_settings_are_saved_in_its_folder(tmp_path: Path, cfg: Any) -> None:
    paths = StationPaths.for_station(_spec(cfg, "nightowl").cfg, "nightowl")
    paths.state_dir.mkdir(parents=True)
    bridge = _bridge(tmp_path, paths=paths, stream_path="/stations/nightowl.mp3")
    bridge.save_persistent_state()
    assert paths.web_state.is_file()
    assert not (tmp_path / "main" / "web_state.json").exists()
    assert bridge.m3u_path == "/stations/nightowl.m3u"
    assert _bridge(tmp_path).m3u_path == "/stream.m3u"


def test_stereo_tool_in_use_elsewhere_is_refused_before_any_change(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, stereo_tool_elsewhere=lambda: "AutoDJ")
    bridge.player._cfg.playback.loudness_processing = "smooth"
    body = PlaybackSettingsBody(loudness_processing="stereotool", crossfade_seconds=5.0)
    with pytest.raises(ValueError, match="Stereo Tool is already processing AutoDJ"):
        bridge.set_playback_settings(body)
    assert bridge.player._cfg.playback.loudness_processing == "smooth"
    # Restoring saved settings is checked when the station starts instead.
    bridge.set_playback_settings(body, restoring=True)
    assert bridge.player._cfg.playback.loudness_processing == "stereotool"
    # Free: allowed.
    free = _bridge(tmp_path, stereo_tool_elsewhere=lambda: None)
    free.set_playback_settings(PlaybackSettingsBody(loudness_processing="stereotool"))


def test_two_stations_switching_to_stereo_tool_at_once_cannot_both_get_it(
    tmp_path: Path,
) -> None:
    lock = threading.RLock()
    first = _bridge(tmp_path, stereo_tool_lock=lock)
    second = _bridge(tmp_path, stereo_tool_lock=lock)
    for bridge in (first, second):
        bridge.player._cfg.playback.loudness_processing = "smooth"

    def user(bridge: PlayerBridge, name: str) -> str | None:
        return name if bridge.player._cfg.playback.loudness_processing == "stereotool" else None

    first.stereo_tool_elsewhere = lambda: user(second, "Second")
    second.stereo_tool_elsewhere = lambda: user(first, "First")
    # The first station has passed its check and is half-way through
    # applying when the second asks.
    entered, go = threading.Event(), threading.Event()
    apply = first._apply_playback_settings

    def slow_apply(body: Any, *, restoring: bool) -> None:
        entered.set()
        go.wait(5)
        apply(body, restoring=restoring)

    first._apply_playback_settings = slow_apply  # type: ignore[method-assign]
    body = PlaybackSettingsBody(loudness_processing="stereotool")
    errors: list[Exception] = []

    def switch(bridge: PlayerBridge) -> None:
        try:
            bridge.set_playback_settings(body)
        except ValueError as exc:
            errors.append(exc)

    one = threading.Thread(target=switch, args=(first,))
    one.start()
    assert entered.wait(5)
    two = threading.Thread(target=switch, args=(second,))
    two.start()
    two.join(0.2)  # without the lock, the second would finish here, unchecked
    go.set()
    one.join(5)
    two.join(5)
    assert first.player._cfg.playback.loudness_processing == "stereotool"
    assert second.player._cfg.playback.loudness_processing == "smooth"
    assert [str(exc) for exc in errors] == [
        "Stereo Tool is already processing First, and AutoDJ runs one Stereo Tool at a time. "
        "Choose Smooth for this station, or move the other station off Stereo Tool first."
    ]


def _playback(processing: str, name: str) -> Any:
    return SimpleNamespace(
        playback=SimpleNamespace(loudness_processing=processing),
        stream=SimpleNamespace(station_name=name),
    )


def test_a_station_being_started_holds_stereo_tool(tmp_path: Path) -> None:
    log: list[str] = []
    claims: list[str | None] = []

    def build(registry: StationRegistry) -> _Runtime:
        claims.append(registry.claim_stereo_tool("nightowl", _playback("stereotool", "Owl")))
        # While it starts, nobody else may take Stereo Tool.
        assert registry.stereo_tool_user(exclude="default") == "Owl"
        claims.append(registry.claim_stereo_tool("daybreak", _playback("stereotool", "Day")))
        return _Runtime("nightowl", log, stereo=True)

    registry = StationRegistry(
        _Runtime("default", log),  # type: ignore[arg-type]
        [_extra("nightowl", build, tmp_path, autostart=False)],
    )
    asyncio.run(registry.start("nightowl"))
    assert claims == [None, "Owl"]
    # Running now: it counts as a running station, no longer as a starting one.
    assert registry._starting == {}
    assert registry.stereo_tool_user(exclude="default") == "Nightowl"


class _Library:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def close(self, _quiesced: bool) -> bool:
        self._log.append("library closed")
        return True


def test_a_start_cut_off_at_shutdown_still_ends_stopped(tmp_path: Path) -> None:
    log: list[str] = []
    release = threading.Event()

    def build(_registry: StationRegistry) -> _Runtime:
        release.wait(5)
        return _Runtime("nightowl", log)

    registry = StationRegistry(
        _Runtime("default", log),  # type: ignore[arg-type]
        [
            _extra("nightowl", build, tmp_path, autostart=False),
            _extra("daybreak", lambda _r: _Runtime("daybreak", log), tmp_path, autostart=False),
        ],
    )

    async def scenario() -> None:
        # Uvicorn cancels a request still running when shutdown begins.
        request = asyncio.create_task(registry.start("nightowl"))
        await asyncio.sleep(0.05)
        request.cancel()
        shutdown = asyncio.create_task(registry.shutdown(_Library(log), 1.0))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)
        assert not shutdown.done()  # it waits for the start under way
        release.set()
        assert await shutdown == (True, True)
        with pytest.raises(StationControlError, match="shutting down"):
            await registry.start("daybreak")  # nothing starts once shutdown began

    asyncio.run(scenario())
    # The station the cut-off request built was started, then stopped
    # before the library closed, and closed with the others.
    assert log == [
        "nightowl started",
        "default stopped",
        "nightowl stopped",
        "library closed",
        "default closed",
        "nightowl closed",
    ]


def test_a_stop_cut_off_half_way_still_finishes(tmp_path: Path) -> None:
    log: list[str] = []
    registry = StationRegistry(
        _Runtime("default", log),  # type: ignore[arg-type]
        [_extra("nightowl", lambda _r: _Runtime("nightowl", log), tmp_path, autostart=False)],
    )

    async def scenario() -> None:
        owl = await registry.start("nightowl")
        gate = asyncio.Event()
        stop = owl.stop

        async def slow_stop(timeout: float) -> bool:
            await gate.wait()
            return await stop(timeout)

        owl.stop = slow_stop  # type: ignore[method-assign]
        request = asyncio.create_task(registry.stop("nightowl"))
        await asyncio.sleep(0.05)
        request.cancel()
        await asyncio.sleep(0.05)
        gate.set()
        await registry.shutdown(_Library(log), 1.0)  # type: ignore[arg-type]

    asyncio.run(scenario())
    assert log == [
        "nightowl started",
        "nightowl stopped",
        "nightowl closed",
        "default stopped",
        "library closed",
        "default closed",
    ]


def test_a_station_folder_inside_another_keeps_its_liners_to_itself(tmp_path: Path) -> None:
    log: list[str] = []
    default = _Runtime("default", log)
    default.paths = SimpleNamespace(liners=tmp_path / "liners")
    owl = _extra("nightowl", lambda _r: _Runtime("nightowl", log), tmp_path)
    day = _extra("daybreak", lambda _r: _Runtime("daybreak", log), tmp_path)
    # Night Owl's folder is inside the default station's, and Daybreak's inside Night Owl's.
    object.__setattr__(owl, "paths", SimpleNamespace(liners=tmp_path / "liners" / "owl"))
    object.__setattr__(day, "paths", SimpleNamespace(liners=tmp_path / "liners" / "owl" / "day"))
    registry = StationRegistry(default, [owl, day])  # type: ignore[arg-type]
    where = {
        "default": str(tmp_path / "liners" / "id.mp3"),
        "nightowl": str(tmp_path / "liners" / "owl" / "id.mp3"),
        "daybreak": str(tmp_path / "liners" / "owl" / "day" / "id.mp3"),
        "elsewhere": str(tmp_path / "added" / "id.mp3"),
    }
    owners = {
        place: {slug for slug in registry.slugs() if registry.owns_liner(slug, path)}
        for place, path in where.items()
    }
    assert owners == {
        "default": {"default"},
        "nightowl": {"nightowl"},
        "daybreak": {"daybreak"},
        "elsewhere": {"default"},
    }


def test_a_stations_clockwheel_picks_only_its_own_liners(tmp_path: Path) -> None:
    from autodj.stations import StationRuntime

    owned = SimpleNamespace(path="mine.mp3")
    other = SimpleNamespace(path="theirs.mp3")
    runtime = StationRuntime.__new__(StationRuntime)
    runtime.liner_gate = SimpleNamespace(playable=lambda entries: entries)  # type: ignore[assignment]
    runtime.owns_liner = lambda path: path == "mine.mp3"
    assert runtime._playable_liners([owned, other]) == [owned]
    everything = [owned, other]
    runtime.owns_liner = lambda _path: True
    assert runtime._playable_liners(everything) is everything  # unchanged with one station


def test_a_stopping_station_answers_at_once(tmp_path: Path) -> None:
    log: list[str] = []
    registry = StationRegistry(
        _Runtime("default", log),  # type: ignore[arg-type]
        [_extra("nightowl", lambda _r: _Runtime("nightowl", log), tmp_path, autostart=False)],
    )

    async def scenario() -> None:
        owl = await registry.start("nightowl")
        gate = asyncio.Event()
        stop = owl.stop

        async def slow_stop(timeout: float) -> bool:
            # The player taking its time to finish (up to stop_timeout_s).
            await gate.wait()
            return await stop(timeout)

        owl.stop = slow_stop  # type: ignore[method-assign]
        stopping = asyncio.create_task(registry.stop("nightowl"))
        await asyncio.sleep(0.05)
        # Its routes see a stopped station, its listing says "stopping", and
        # neither a start nor a second stop waits for the player.
        stand_in = registry.get("nightowl")
        assert stand_in.stopped
        assert stand_in.summary()["state"] == "stopping"
        with pytest.raises(StationControlError, match="Nightowl is still stopping"):
            await asyncio.wait_for(registry.start("nightowl"), 0.5)
        await asyncio.wait_for(registry.stop("nightowl"), 0.5)
        assert not stopping.done()
        gate.set()
        await stopping
        assert registry.get("nightowl").summary()["state"] == "stopped"
        assert (await registry.start("nightowl")).slug == "nightowl"

    asyncio.run(scenario())
    assert log == ["nightowl started", "nightowl stopped", "nightowl closed", "nightowl started"]


def test_a_stopping_stations_pages_are_closed_before_its_player_finishes(tmp_path: Path) -> None:
    log: list[str] = []
    registry = StationRegistry(
        _Runtime("default", log),  # type: ignore[arg-type]
        [
            _extra("nightowl", lambda _r: _Runtime("nightowl", log), tmp_path, autostart=False),
            _extra("daybreak", lambda _r: _Runtime("daybreak", log), tmp_path, autostart=False),
        ],
    )

    async def disconnect(station: Any) -> None:
        log.append(f"{station.slug} pages closed")

    registry.disconnect = disconnect

    async def scenario() -> None:
        owl = await registry.start("nightowl")
        gate = asyncio.Event()
        stop = owl.stop

        async def slow_stop(timeout: float) -> bool:
            await gate.wait()
            return await stop(timeout)

        owl.stop = slow_stop  # type: ignore[method-assign]
        stopping = asyncio.create_task(registry.stop("nightowl"))
        await asyncio.sleep(0.05)
        # Its pages are told at once, and it refuses new ones.
        assert log[-1] == "nightowl pages closed"
        assert owl.stopped
        # Another station starts and stops meanwhile: each station has its own lock.
        day = await asyncio.wait_for(registry.start("daybreak"), 0.5)
        assert day.slug == "daybreak"
        await asyncio.wait_for(registry.stop("daybreak"), 0.5)
        assert not stopping.done()
        gate.set()
        await stopping

    asyncio.run(scenario())
    assert log == [
        "nightowl started",
        "nightowl pages closed",
        "daybreak started",
        "daybreak pages closed",
        "daybreak stopped",
        "daybreak closed",
        "daybreak pages closed",
        "nightowl stopped",
        "nightowl closed",
        "nightowl pages closed",
    ]


def test_stereo_tool_held_by_a_stopping_station_says_so(tmp_path: Path) -> None:
    log: list[str] = []
    registry = StationRegistry(
        _Runtime("default", log),  # type: ignore[arg-type]
        [
            _extra(
                "nightowl",
                lambda _r: _Runtime("nightowl", log, stereo=True),
                tmp_path,
                autostart=False,
            )
        ],
    )

    async def scenario() -> None:
        owl = await registry.start("nightowl")
        assert registry.stereo_tool_user(exclude="default") == "Nightowl"
        gate = asyncio.Event()
        stop = owl.stop

        async def slow_stop(timeout: float) -> bool:
            await gate.wait()
            return await stop(timeout)

        owl.stop = slow_stop  # type: ignore[method-assign]
        stopping = asyncio.create_task(registry.stop("nightowl"))
        await asyncio.sleep(0.05)
        # Its Stereo Tool runs until it has stopped: still taken, and said so.
        held = registry.stereo_tool_user(exclude="default")
        assert held == (
            "Nightowl, which is still stopping (try again in a few seconds, once it has stopped)"
        )
        assert registry.claim_stereo_tool("default", _playback("stereotool", "AutoDJ")) == held
        gate.set()
        await stopping
        assert registry.stereo_tool_user(exclude="default") is None

    asyncio.run(scenario())


def test_a_runtime_stops_and_closes_once(tmp_path: Path) -> None:
    from autodj.stations import StationRuntime

    calls: list[str] = []
    runtime = StationRuntime.__new__(StationRuntime)
    runtime._stop_task = None
    runtime._close_task = None

    async def stop(_timeout: float) -> bool:
        calls.append("stop")
        await asyncio.sleep(0.01)
        return True

    async def close() -> None:
        calls.append("close")

    runtime._stop = stop  # type: ignore[method-assign]
    runtime._close = close  # type: ignore[method-assign]

    async def scenario() -> None:
        # Shutdown and a queued stop at once, then again: one stop, one close.
        assert await asyncio.gather(runtime.stop(1.0), runtime.stop(1.0)) == [True, True]
        assert await runtime.stop(1.0)
        await asyncio.gather(runtime.close(), runtime.close())

    asyncio.run(scenario())
    assert calls == ["stop", "close"]


def test_a_shutdown_then_a_queued_stop_tears_down_once(tmp_path: Path) -> None:
    log: list[str] = []

    class _Once(_Runtime):
        """Stops and closes once, as StationRuntime does."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._done: set[str] = set()

        async def stop(self, timeout: float) -> bool:
            if "stop" not in self._done:
                self._done.add("stop")
                await super().stop(timeout)
            return True

        async def close(self) -> None:
            if "close" not in self._done:
                self._done.add("close")
                await super().close()

    registry = StationRegistry(
        _Once("default", log),  # type: ignore[arg-type]
        [_extra("nightowl", lambda _r: _Once("nightowl", log), tmp_path, autostart=False)],
    )

    async def scenario() -> None:
        await registry.start("nightowl")
        shutdown = asyncio.create_task(registry.shutdown(_Library(log), 1.0))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        await registry.stop("nightowl")  # queued behind the shutdown
        await shutdown

    asyncio.run(scenario())
    assert log.count("nightowl stopped") == 1
    assert log.count("nightowl closed") == 1


def test_liner_folders_are_compared_as_the_file_system_resolves_them(tmp_path: Path) -> None:
    import os

    real = tmp_path / "voice"
    (real / "owl").mkdir(parents=True)
    link = tmp_path / "voice-link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this system cannot make a symbolic link")
    log: list[str] = []
    default = _Runtime("default", log)
    default.paths = SimpleNamespace(liners=real)
    owl = _extra("nightowl", lambda _r: _Runtime("nightowl", log), tmp_path)
    # Night Owl's folder is configured through the link; the library spelled it directly.
    object.__setattr__(owl, "paths", SimpleNamespace(liners=link / "owl"))
    registry = StationRegistry(default, [owl])  # type: ignore[arg-type]
    liner = str(real / "owl" / "id.mp3")
    assert registry.owns_liner("nightowl", liner)
    assert not registry.owns_liner("default", liner)
    via_link = str(link / "id.mp3")
    assert registry.owns_liner("default", via_link)
    assert not registry.owns_liner("nightowl", via_link)


def test_a_failed_build_leaves_a_player_that_will_not_stop_and_its_master(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from autodj.server import _abandon_player

    release = threading.Event()
    closed: list[str] = []
    player = SimpleNamespace(
        _state=SimpleNamespace(should_stop=False),
        _skip_event=threading.Event(),
        _continuous_master=SimpleNamespace(close=lambda: closed.append("master")),
    )
    stuck = threading.Thread(target=release.wait, args=(5,), daemon=True)
    stuck.start()
    try:
        with caplog.at_level(logging.WARNING, logger="autodj.server"):
            _abandon_player(player, stuck, 0.05)
        # Still running: it may still write to the master, so both are left.
        assert closed == []
        assert "left running" in caplog.records[-1].getMessage()
    finally:
        release.set()
        stuck.join(5)
    _abandon_player(player, stuck, 0.05)  # finished: the master is closed
    assert closed == ["master"]
    assert player._state.should_stop and player._skip_event.is_set()


@pytest.mark.skipif(not hasattr(__import__("ctypes"), "windll"), reason="Windows short names")
def test_liner_folders_given_by_a_windows_short_name_match(tmp_path: Path) -> None:
    import ctypes

    long_folder = tmp_path / "Night Owl voice liners"
    (long_folder / "owl").mkdir(parents=True)
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(long_folder), buffer, 1024)
    short = Path(buffer.value)
    if not buffer.value or short.name == long_folder.name:
        pytest.skip("short (8.3) names are off on this drive")
    log: list[str] = []
    default = _Runtime("default", log)
    default.paths = SimpleNamespace(liners=long_folder)
    owl = _extra("nightowl", lambda _r: _Runtime("nightowl", log), tmp_path)
    object.__setattr__(owl, "paths", SimpleNamespace(liners=short / "owl"))
    registry = StationRegistry(default, [owl])  # type: ignore[arg-type]
    liner = str(long_folder / "owl" / "id.mp3")
    assert registry.owns_liner("nightowl", liner)
    assert not registry.owns_liner("default", liner)


# -- the Stereo Tool licence across threads ------------------------------------


class _SlowCheck(_Runtime):
    """A default station whose Stereo Tool check is slow, widening every race window."""

    def uses_stereo_tool(self) -> bool:
        time.sleep(0.0005)
        return False


def test_a_check_from_another_thread_always_finds_the_licence_holder(tmp_path: Path) -> None:
    """Start and stop a Stereo Tool station over and over while a worker thread checks.

    From the moment its build claims Stereo Tool until it closes its
    mastering process, a check from another thread (a station switching
    to Stereo Tool in its settings) must always find it: starting,
    running or stopping.
    """
    log: list[str] = []
    # Set from the claim until the station closes its mastering process.
    held = threading.Event()
    cfg = _playback("stereotool", "Nightowl")

    class _Licensed(_Runtime):
        async def close(self) -> None:
            held.clear()  # Stereo Tool is gone from here on
            await super().close()

    def build(registry: StationRegistry) -> _Runtime:
        assert registry.claim_stereo_tool("nightowl", cfg) is None
        held.set()
        return _Licensed("nightowl", log, stereo=True)

    registry = StationRegistry(
        _SlowCheck("default", log),  # type: ignore[arg-type]
        [_extra("nightowl", build, tmp_path, autostart=False)],
    )
    misses: list[int] = []
    done = threading.Event()

    def check() -> None:
        while not done.is_set():
            before = held.is_set()
            found = registry.stereo_tool_user(exclude="default")
            if before and held.is_set() and found is None:
                misses.append(1)

    checker = threading.Thread(target=check, daemon=True)
    checker.start()

    async def scenario() -> None:
        for _ in range(150):
            await registry.start("nightowl")
            await asyncio.sleep(0)
            await registry.stop("nightowl")

    try:
        asyncio.run(scenario())
    finally:
        done.set()
        checker.join(5)
    assert misses == []
    assert registry.stereo_tool_user(exclude="default") is None


def test_a_station_whose_player_will_not_stop_keeps_its_master_and_licence(
    tmp_path: Path,
) -> None:
    log: list[str] = []
    release = threading.Event()
    closed: list[str] = []

    class _Stuck(_Runtime):
        """A station whose player thread outlives the stop."""

        def __init__(self) -> None:
            super().__init__("nightowl", log, stereo=True)
            self.player_thread = threading.Thread(target=release.wait, args=(10,), daemon=True)
            self.player_thread.start()

        async def stop(self, _timeout: float) -> bool:
            log.append("nightowl stopped")
            return False  # the player did not finish in time

        def player_running(self) -> bool:
            return self.player_thread.is_alive()

        async def close(self) -> None:
            # As StationRuntime.close: the master stays while the player runs.
            log.append("nightowl closed")

        def close_master(self) -> None:
            closed.append("master")

    stuck = _Stuck()
    registry = StationRegistry(
        _Runtime("default", log),  # type: ignore[arg-type]
        [_extra("nightowl", lambda _r: stuck, tmp_path, autostart=False)],
    )

    async def scenario() -> None:
        await registry.start("nightowl")
        await registry.stop("nightowl")
        # Its player runs on: listed as stuck, still holding Stereo Tool,
        # refusing a start, and its mastering process left open.
        assert registry.get("nightowl").summary()["state"] == "stuck stopping"
        assert "has not finished stopping: its player is still running" in (
            registry.stereo_tool_user(exclude="default") or ""
        )
        with pytest.raises(StationControlError, match="has not finished stopping"):
            await registry.start("nightowl")
        assert closed == []
        # The player ends: the master closes and the station is free (a worker thread).
        release.set()
        for _ in range(200):
            if registry.get("nightowl").summary()["state"] == "stopped":
                break
            await asyncio.sleep(0.01)
        assert closed == ["master"]
        assert registry.stereo_tool_user(exclude="default") is None

    asyncio.run(scenario())


def test_a_failed_build_with_a_stuck_player_is_held_too(tmp_path: Path) -> None:
    from autodj.server import _abandon_player
    from autodj.stations import AbandonedPlayer

    log: list[str] = []
    registry = StationRegistry(
        _Runtime("default", log),  # type: ignore[arg-type]
        [_extra("nightowl", lambda _r: _Runtime("nightowl", log), tmp_path, autostart=False)],
    )
    release = threading.Event()
    closed: list[str] = []
    player = SimpleNamespace(
        _state=SimpleNamespace(should_stop=False),
        _skip_event=threading.Event(),
        _active_processing_mode="stereotool",
        _continuous_master=SimpleNamespace(close=lambda: closed.append("master")),
    )
    thread = threading.Thread(target=release.wait, args=(10,), daemon=True)
    thread.start()
    held = AbandonedPlayer(
        "nightowl", "Night Owl Radio", player, thread, _playback("smooth", "Night Owl Radio")
    )
    _abandon_player(player, thread, 0.05, on_stuck=lambda: registry.hold_until_player_ends(held))
    assert closed == []
    assert registry.get("nightowl").summary()["state"] == "stuck stopping"
    assert (registry.stereo_tool_user(exclude="default") or "").startswith("Night Owl Radio")
    release.set()
    thread.join(5)
    deadline = time.monotonic() + 5
    while registry.get("nightowl").summary()["state"] != "stopped":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert closed == ["master"]


def test_a_start_queued_behind_another_refuses_once_a_stop_begins(tmp_path: Path) -> None:
    log: list[str] = []
    release = threading.Event()

    def build(_registry: StationRegistry) -> _Runtime:
        release.wait(5)
        return _Runtime("nightowl", log)

    registry = StationRegistry(
        _Runtime("default", log),  # type: ignore[arg-type]
        [_extra("nightowl", build, tmp_path, autostart=False)],
    )

    async def scenario() -> None:
        first = asyncio.create_task(registry.start("nightowl"))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(registry.start("nightowl"))  # waits for the first
        await asyncio.sleep(0.05)
        stopping = asyncio.create_task(registry.stop("nightowl"))
        await asyncio.sleep(0.05)
        release.set()
        await first
        with pytest.raises(StationControlError, match="still stopping"):
            await second
        await stopping
        # Shutdown leaves only the default station in the running list.
        await registry.start("nightowl")
        await registry.shutdown(_Library(log), 1.0)  # type: ignore[arg-type]
        assert [station.slug for station in registry.all()] == ["default"]

    asyncio.run(scenario())


def test_every_stop_step_is_tried_and_a_failed_stop_is_not_remembered() -> None:
    from autodj.stations import StationRuntime

    def broken() -> None:
        raise OSError("this step failed")

    async def broken_async() -> None:
        raise OSError("this step failed")

    stopped_liners: list[str] = []
    player = SimpleNamespace(
        _state=SimpleNamespace(should_stop=False), _skip_event=threading.Event()
    )
    runtime = StationRuntime.__new__(StationRuntime)
    runtime.stopped = False
    runtime.bridge = SimpleNamespace(  # type: ignore[assignment]
        scheduler_track_changed=None,
        player=player,
        shutdown_liner_worker=lambda: stopped_liners.append("liners"),
    )
    runtime._schedule_ticker = None
    runtime._stream_ticker = None
    runtime.carts = SimpleNamespace(close=broken_async)  # type: ignore[assignment]
    runtime.webhooks = SimpleNamespace(close=broken)  # type: ignore[assignment]
    runtime.stream_out = None
    runtime.recorder = SimpleNamespace(close=broken)  # type: ignore[assignment]
    runtime.server_audio = True
    runtime.player_thread = None
    runtime._stop_task = None
    assert asyncio.run(runtime.stop(1.0)) is True
    # Every later step still ran: the liner worker stopped and the player was told.
    assert stopped_liners == ["liners"]
    assert player._state.should_stop and player._skip_event.is_set()

    calls: list[int] = []

    async def fails_once(_timeout: float) -> bool:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("unexpected")
        return True

    again = StationRuntime.__new__(StationRuntime)
    again._stop_task = None
    again._stop = fails_once  # type: ignore[method-assign]

    async def twice() -> bool:
        with pytest.raises(RuntimeError):
            await again.stop(1.0)
        return await again.stop(1.0)

    assert asyncio.run(twice()) is True
    assert calls == [1, 1]


def test_leftover_mastering_processes_are_killed_at_shutdown() -> None:
    from autodj import mastering

    class _Proc:
        def __init__(self, running: bool) -> None:
            self.running = running
            self.killed = False

        def poll(self) -> int | None:
            return None if self.running else 0

        def kill(self) -> None:
            self.killed = True
            self.running = False

        def wait(self, timeout: float) -> int:
            return 0

    procs = [_Proc(True), _Proc(False)]
    start = mastering._tracked(lambda *_args, **_kwargs: procs.pop(0))
    alive, ended = start(), start()
    try:
        assert {alive, ended} <= mastering._LIVE_PROCESSES
        assert mastering.kill_leftover_processes() == 1
        assert alive.killed and not ended.killed
        assert alive not in mastering._LIVE_PROCESSES
        assert mastering.kill_leftover_processes() == 0
    finally:
        mastering._LIVE_PROCESSES.discard(alive)
        mastering._LIVE_PROCESSES.discard(ended)
