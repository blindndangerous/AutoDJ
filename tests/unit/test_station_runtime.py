"""Another station's paths, logs, registry and bridge (#70, PR 5)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
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
    assert disconnected == ["nightowl"]


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
