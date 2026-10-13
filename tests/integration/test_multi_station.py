"""Two stations on one server, both streaming, with fake audio (#70, PR 5).

The default station and Night Owl Radio (``nightowl``) are built the way
``serve()`` builds them: real players, mix buses, stream outputs, station
controllers and recorders, sharing one library.  ffmpeg is replaced by an
encoder that echoes PCM, and the mastering process by a pass-through, so
nothing outside the test process runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest
from fastapi.testclient import TestClient

from autodj import server
from autodj._bridge import PlayerBridge
from autodj.config import load_config
from autodj.library import LibraryManager
from autodj.player import Player
from autodj.similarity import SimilarityIndex
from autodj.station_log import station_thread
from autodj.stream import Listener
from tests.unit._fakes import FakeEncoder
from tests.unit.test_library import make_entry

OWL = "/api/stations/nightowl"
WAIT = 10.0

CONFIG = """
[library]
music_dir = "{root}/Music"

[index]
index_dir = "{root}/index"
model_dir = "{root}/models"

[server]
allowed_hosts = ["testserver"]
allowed_origins = ["http://testserver"]
station_timezone = "UTC"
{server_extra}

[stream]
enabled = true
station_name = "AutoDJ"

[playback]
loudness_processing = "{default_processing}"
import_external_cues = false

[recording]
folder = "{root}/recordings/default"

[stations.nightowl]
autostart = {autostart}

[stations.nightowl.stream]
station_name = "Night Owl Radio"
bitrate = 192

[stations.nightowl.playback]
loudness_processing = "{owl_processing}"
import_external_cues = false

[stations.nightowl.recording]
folder = "{root}/recordings/nightowl"

[stations.nightowl.requests]
enabled = true
approval = "auto"
"""


class FakeMaster:
    """Passes the mix through; remembers how it was asked to process."""

    instances: ClassVar[list[FakeMaster]] = []

    def __init__(self, *, mode: Any, **_kwargs: Any) -> None:
        self.mode = mode
        self.closed = False
        self.error = None
        self.preset_warning = None
        self.latency_seconds = 0.0
        self.restarts = 0
        FakeMaster.instances.append(self)

    def process(self, block: np.ndarray, enabled: bool = True) -> np.ndarray:
        return block

    def close(self) -> None:
        self.closed = True


def _write_config(tmp_path: Path, **values: str) -> Path:
    settings = {
        "default_processing": "smooth",
        "owl_processing": "smooth",
        "autostart": "true",
        "server_extra": "",
        **values,
    }
    local = settings.pop("local", None)
    if local is not None:
        (tmp_path / "config.local.toml").write_text(local, encoding="utf-8")
    (tmp_path / "Music").mkdir(exist_ok=True)
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(root=tmp_path.as_posix(), **settings),
        encoding="utf-8",
    )
    return path


def _station_threads(slug: str) -> list[threading.Thread]:
    # A thread pool's workers add "_<n>" after the name (autodj-liner-nightowl_0).
    named = re.compile(rf".*-{re.escape(slug)}(?:_\d+)?\Z")
    return [thread for thread in threading.enumerate() if named.match(thread.name)]


def _wait_for(predicate: Any, what: str) -> None:
    deadline = time.monotonic() + WAIT
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Any:
    """Build the app as serve() does; yield (client, library, config); shut down after."""
    values = getattr(request, "param", {})
    FakeMaster.instances = []
    monkeypatch.setattr("autodj.stream.FfmpegEncoder", FakeEncoder)
    monkeypatch.setattr("autodj.mastering.ContinuousMaster", FakeMaster)
    closes: list[str] = []
    real_close = __import__("autodj.dj_meta", fromlist=["close_cache"]).close_cache

    def close_cache() -> None:
        closes.append("closed")
        real_close()

    monkeypatch.setattr("autodj.dj_meta.close_cache", close_cache)
    cfg = load_config(_write_config(tmp_path, **values), environ={})
    sim = SimilarityIndex.empty()
    library = LibraryManager(cfg, sim)
    # One known song, so queue routes have something to queue.
    song = make_entry(tmp_path / "Music" / "song.flac")
    monkeypatch.setattr(
        library, "entry_for_path", lambda path: song if str(path) == song.path else None
    )
    player = Player(cfg, sim, stream_mode=True, server_audio_too=False)
    player._library = library
    bridge = PlayerBridge(player=player, sim=sim, library=library)
    player.on_track_started = bridge.on_track_started
    bridge.load_persistent_state()
    player.configure_clock()
    server._attach_master(player, cfg)
    player_thread = station_thread(
        target=player.run, kwargs={"seed_entry": None}, name="autodj-player"
    )
    player_thread.start()
    extras = [server._extra_station(cfg, spec, sim, library, "UTC") for spec in cfg.stations]
    app = server.create_app(
        bridge, player_thread=player_thread, stream_mode=True, extra_stations=extras
    )
    with TestClient(app, client=("192.0.2.4", 50000)) as client:
        yield client, library, cfg, song, closes
    assert not player_thread.is_alive()
    assert _station_threads("nightowl") == []
    assert closes == ["closed"]  # once, after both players were quiet


def _stations(client: TestClient) -> dict[str, dict[str, Any]]:
    response = client.get("/api/stations")
    assert response.status_code == 200
    return {entry["slug"]: entry for entry in response.json()["stations"]}


def test_two_stations_run_side_by_side(rig: Any) -> None:
    client, library, cfg, _song, _closes = rig
    stations = client.app.state.stations
    default, owl = stations.default, stations.get("nightowl")
    assert {station.slug for station in stations.all()} == {"default", "nightowl"}
    # Each has its own player, mix bus, encoder, master and state folder.
    assert default.player is not owl.player
    assert default.player.bus is not owl.player.bus
    assert default.bridge.stream is not owl.bridge.stream
    assert default.player._continuous_master is not owl.player._continuous_master
    assert owl.paths.state_dir == cfg.index.active_dir / "stations" / "nightowl"
    assert owl.player.station_paths is owl.paths
    assert owl.ledger is library.station_ledger("nightowl")
    assert default.ledger is library.ledger
    # Its threads carry its name; the default station's do not.
    names = {thread.name for thread in _station_threads("nightowl")}
    assert {"autodj-player-nightowl", "autodj-stream-writer-nightowl"} <= names
    assert "autodj-master" not in names  # the fake master starts no thread
    assert any(thread.name == "autodj-player" for thread in threading.enumerate())

    listing = _stations(client)
    assert listing["default"]["name"] == "AutoDJ"
    assert listing["default"]["stream_path"] == "/stream.mp3"
    assert listing["nightowl"] == {
        "slug": "nightowl",
        "name": "Night Owl Radio",
        "state": "idle",
        "listeners": 0,
        "title": None,
        "status_path": "/stations/nightowl/status",
        "nowplaying_path": "/api/stations/nightowl/nowplaying",
        "stream_path": "/stations/nightowl.mp3",
        "m3u_path": "/stations/nightowl.m3u",
    }
    stream = client.get(f"{OWL}/stream").json()
    assert (stream["path"], stream["m3u_path"]) == (
        "/stations/nightowl.mp3",
        "/stations/nightowl.m3u",
    )
    assert stream["bitrate"] == 192
    assert client.get("/api/stream").json()["path"] == "/stream.mp3"
    assert client.get(f"{OWL}/nowplaying").json()["stream_path"] == "/stations/nightowl.mp3"


def test_queue_pause_and_settings_stay_in_their_station(rig: Any) -> None:
    client, _library, cfg, song, _closes = rig
    stations = client.app.state.stations
    default, owl = stations.default, stations.get("nightowl")
    assert client.post(f"{OWL}/queue/add", json={"path": song.path}).status_code == 200
    assert [entry.path for entry in owl.player._state.queue] == [song.path]
    assert list(default.player._state.queue) == []

    assert client.post(f"{OWL}/pause").json() == {"paused": True}
    assert owl.player._state.is_paused
    assert not default.player._state.is_paused

    assert client.post(f"{OWL}/transition", json={"effect": "echo_out"}).status_code == 200
    assert owl.cfg.transitions.effect == "echo_out"
    assert cfg.transitions.effect == "none"
    saved = json.loads((owl.paths.state_dir / "web_state.json").read_text(encoding="utf-8"))
    assert saved["transition"] == "echo_out"
    assert not (cfg.index.active_dir / "web_state.json").exists()


def test_listeners_bans_and_recording_are_per_station(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _library, cfg, _song, _closes = rig
    stations = client.app.state.stations
    default, owl = stations.default, stations.get("nightowl")

    playlist = client.get("/stations/nightowl.m3u")
    assert playlist.status_code == 200
    assert playlist.text == "http://testserver/stations/nightowl.mp3\n"

    # The test client reads a response to its end, so each listener hears
    # one chunk of the live stream; what the stations count is noted then.
    heard: dict[str, Any] = {}
    real_chunks = Listener.chunks

    async def one_chunk(listener: Listener) -> AsyncIterator[bytes]:
        heard["owl"] = owl.summary()["listeners"]
        heard["default"] = default.summary()["listeners"]
        heard["listed"] = [row["address"] for row in owl.listener_access.listeners()]
        heard["default listed"] = default.listener_access.listeners()
        async for chunk in real_chunks(listener):
            yield chunk
            return

    monkeypatch.setattr(Listener, "chunks", one_chunk)
    audio = client.get("/stations/nightowl.mp3", headers={"Icy-MetaData": "1"})
    assert audio.status_code == 200
    assert audio.headers["icy-name"] == "Night Owl Radio"
    assert audio.headers["icy-br"] == "192"
    assert audio.content
    assert "content-encoding" not in audio.headers  # a stream is never gzipped
    assert heard == {"owl": 1, "default": 0, "listed": ["192.0.2.4"], "default listed": []}
    _wait_for(lambda: owl.bridge.stream.listener_count == 0, "the listener to leave")

    banned = client.post(f"{OWL}/stream/bans", json={"address": "192.0.2.4"})
    assert banned.status_code == 200
    assert client.get("/stations/nightowl.mp3").status_code == 403
    assert client.head("/stream.mp3").status_code == 200
    assert (owl.paths.state_dir / "listener_bans.json").is_file()
    assert not (cfg.index.active_dir / "listener_bans.json").exists()

    assert client.post(f"{OWL}/recording/start").status_code == 200
    assert client.get(f"{OWL}/recording").json()["running"] is True
    assert client.get("/api/recording").json()["running"] is False
    assert (owl.paths.state_dir / "recording.json").is_file()
    assert client.post(f"{OWL}/recording/stop").status_code == 200


def test_unknown_station_streams_are_not_found(rig: Any) -> None:
    client, *_ = rig
    assert client.get("/stations/daybreak.mp3").status_code == 404
    assert client.get("/stations/nightowl.ogg").status_code == 404
    assert client.get("/stations/nightowl").status_code == 404
    assert client.post("/api/stations/daybreak/start").status_code == 404
    assert client.post("/api/stations/daybreak/stop").status_code == 404


def test_stop_and_start_clean_up_the_station(rig: Any) -> None:
    client, _library, _cfg, _song, _closes = rig
    stations = client.app.state.stations
    owl = stations.get("nightowl")
    master = owl.player._continuous_master
    encoder_stream = owl.bridge.stream
    with client.websocket_connect(f"{OWL}/ws") as socket:
        response = client.post(f"{OWL}/stop")
        assert response.status_code == 200
        assert response.json()["state"] == "stopped"
        with pytest.raises(Exception):  # noqa: B017 -- the server closed it (4409)
            socket.receive_json()
    assert _station_threads("nightowl") == []
    assert master.closed
    assert encoder_stream.listener_count == 0
    assert stations.get("nightowl").stopped
    assert client.get(f"{OWL}/status").status_code == 409
    assert client.get("/stations/nightowl.mp3").status_code == 503
    assert _stations(client)["nightowl"]["state"] == "stopped"
    # Stopping again, or a running station's start, changes nothing.
    assert client.post(f"{OWL}/stop").status_code == 200

    started = client.post(f"{OWL}/start")
    assert started.status_code == 200
    assert started.json()["state"] == "idle"
    fresh = stations.get("nightowl")
    assert fresh is not owl
    assert fresh.player._continuous_master is not master
    assert client.get(f"{OWL}/status").status_code == 200
    assert client.post(f"{OWL}/start").json()["slug"] == "nightowl"
    assert stations.get("nightowl") is fresh

    refused = client.post("/api/stations/default/stop")
    assert refused.status_code == 409
    assert "cannot be stopped" in refused.json()["detail"]


def test_station_log_lines_name_the_station(rig: Any, caplog: pytest.LogCaptureFixture) -> None:
    client, *_ = rig
    with caplog.at_level(logging.INFO):
        client.post(f"{OWL}/stop")
        client.post(f"{OWL}/start")
        client.post(f"{OWL}/mute")
    messages = [record.getMessage() for record in caplog.records]
    assert "[nightowl] Station nightowl (Night Owl Radio) stopped" in messages
    assert "[nightowl] Station nightowl (Night Owl Radio) started" in messages
    # The audit log stays one JSON object per line; its route names the station.
    audit = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "autodj.audit"
    ]
    assert audit[-1]["route"] == "/api/stations/{slug}/mute"


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_a_station_without_autostart_waits_to_be_started(rig: Any) -> None:
    client, *_ = rig
    assert _stations(client)["nightowl"]["state"] == "stopped"
    assert _station_threads("nightowl") == []
    assert client.get(f"{OWL}/status").status_code == 409
    assert client.post(f"{OWL}/start").status_code == 200
    _wait_for(lambda: _station_threads("nightowl"), "the station's threads")


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_a_station_that_fails_half_way_through_starting_leaves_nothing_running(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, *_ = rig

    def broken_recorder(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("the recording state could not be read")

    import autodj.recorder

    working = autodj.recorder.Recorder
    # The player, its thread and the mastering process exist by then.
    monkeypatch.setattr(autodj.recorder, "Recorder", broken_recorder)
    refused = client.post(f"{OWL}/start")
    assert refused.status_code == 409
    assert "the recording state could not be read" in refused.json()["detail"]
    assert FakeMaster.instances[-1].closed
    _wait_for(lambda: not _station_threads("nightowl"), "the station's threads to end")
    assert _stations(client)["nightowl"]["state"] == "stopped"
    monkeypatch.setattr(autodj.recorder, "Recorder", working)
    assert client.post(f"{OWL}/start").status_code == 200


@pytest.mark.parametrize(
    "rig",
    [{"default_processing": "stereotool", "owl_processing": "stereotool"}],
    indirect=True,
)
def test_two_stations_cannot_both_use_stereo_tool(rig: Any) -> None:
    client, *_ = rig
    # Started with the server: refused, logged, and left stopped.
    assert _stations(client)["nightowl"]["state"] == "stopped"
    refused = client.post(f"{OWL}/start")
    assert refused.status_code == 409
    assert "Stereo Tool, which is already processing AutoDJ" in refused.json()["detail"]
    assert _station_threads("nightowl") == []


@pytest.mark.parametrize("rig", [{"default_processing": "stereotool"}], indirect=True)
def test_a_running_station_cannot_switch_to_stereo_tool_in_use(rig: Any) -> None:
    client, *_ = rig
    owl = client.app.state.stations.get("nightowl")
    refused = client.post(f"{OWL}/playback-settings", json={"loudness_processing": "stereotool"})
    assert refused.status_code == 400
    assert "Stereo Tool is already processing AutoDJ" in refused.json()["detail"]
    assert owl.cfg.playback.loudness_processing == "smooth"
    # The default station keeps it, and may still change its own settings.
    kept = client.post("/api/playback-settings", json={"loudness_processing": "stereotool"})
    assert kept.status_code == 200


def test_a_preset_any_station_uses_cannot_be_deleted(rig: Any) -> None:
    client, *_ = rig
    upload = client.post(
        "/api/stereo-tool-presets/upload",
        files={"file": ("Owl.sts", b"[Stereo Tool]\nVersion=1\n", "text/plain")},
    )
    assert upload.status_code == 200, upload.text
    chosen = client.post(f"{OWL}/playback-settings", json={"stereotool_preset": "Owl.sts"})
    assert chosen.status_code == 200
    refused = client.delete("/api/stereo-tool-presets/Owl.sts")
    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        "select another preset on Night Owl Radio before deleting this one"
    )
    # Still refused while the station is stopped: its saved settings name it.
    assert client.post(f"{OWL}/stop").status_code == 200
    assert client.delete("/api/stereo-tool-presets/Owl.sts").status_code == 409


# How long the soak runs: a few seconds in every test run, or as long as
# AUTODJ_SOAK_SECONDS says (hours on the machine that will run the stations).
SOAK_SECONDS = float(os.environ.get("AUTODJ_SOAK_SECONDS", "0") or 0)
_BRIEF_SOAK_SECONDS = 4.0


def _soak_setting(name: str, brief: float, long: float) -> float:
    value = os.environ.get(name, "")
    return float(value) if value else (long if SOAK_SECONDS else brief)


def _rss_mb() -> float | None:
    """The test process's resident memory in MB; Linux only, else ``None``."""
    try:
        with open("/proc/self/statm", encoding="ascii") as statm:
            pages = int(statm.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / 2**20, 1)
    except (OSError, ValueError, AttributeError, IndexError):
        return None


class _BusClock:
    """A mix-bus output that counts the 20 ms blocks it is sent and the longest gap."""

    def __init__(self) -> None:
        self.blocks = 0
        self.longest_gap = 0.0
        self._last: float | None = None
        self.seconds = 0.0  # how long it was attached, once detached

    def write(self, _block: np.ndarray) -> None:
        now = time.monotonic()
        if self._last is not None:
            self.longest_gap = max(self.longest_gap, now - self._last)
        self._last = now
        self.blocks += 1

    def close(self) -> None:
        pass


@pytest.mark.timeout(max(SOAK_SECONDS, _BRIEF_SOAK_SECONDS) * 2 + 120)
def test_soak_two_stations_keep_time_through_renders_and_restarts(rig: Any) -> None:
    """Both mix buses keep time while both stations render and one restarts.

    Each station's render-ahead work is stood in for by a decode-sized
    buffer time-stretched with scipy, back to back, for the whole run,
    while Night Owl is stopped and started again every cycle.  Every 20 ms
    block each bus emits is counted; the bus drops blocks only after
    falling more than 8 behind, so a shortfall means an underrun.  Each
    stop must leave none of the station's threads or encoders behind.

    It runs for a few seconds in every test run.  For the long run on the
    machine that hosts the stations, see "Several stations" in
    docs/operations.md: ``AUTODJ_SOAK_SECONDS`` sets the length,
    ``AUTODJ_SOAK_CYCLE_SECONDS`` how often Night Owl restarts,
    ``AUTODJ_SOAK_SAMPLE_SECONDS`` how often memory and CPU are sampled,
    and ``AUTODJ_SOAK_REPORT`` a file the samples are written to, one JSON
    object per line.
    """
    from scipy.signal import resample_poly

    client, *_ = rig
    registry = client.app.state.stations
    seconds = SOAK_SECONDS or _BRIEF_SOAK_SECONDS
    cycle = _soak_setting("AUTODJ_SOAK_CYCLE_SECONDS", seconds / 2, 600.0)
    sample_every = _soak_setting("AUTODJ_SOAK_SAMPLE_SECONDS", 0.5, 30.0)
    # A four-minute song in the long run; shorter in the brief one, to spare CI.
    track_frames = 44100 * (240 if SOAK_SECONDS else 20)
    # The bus timing is measured only in a run asked for: a few seconds on
    # a shared CI machine says little about underruns, and would be flaky.
    # The brief run still checks restarts and leaks.
    least = 0.99 if SOAK_SECONDS else 0.0
    report_path = os.environ.get("AUTODJ_SOAK_REPORT")
    period = 882 / 44100

    stop = threading.Event()
    renders = [0]

    def render() -> None:
        rng = np.random.default_rng(0)
        while not stop.is_set():
            track = rng.standard_normal((track_frames, 2), dtype=np.float32)
            stretched = resample_poly(track, 51, 50, axis=0)
            fade = np.linspace(0.0, 1.0, len(stretched), dtype=np.float32)
            stretched *= fade[:, None]
            renders[0] += 1

    default_clock = _BusClock()
    registry.default.player.bus.add_output(default_clock)
    owl_clocks: list[_BusClock] = []

    def attach_owl() -> tuple[Any, float]:
        owl = registry.get("nightowl")
        owl_clocks.append(_BusClock())
        owl.player.bus.add_output(owl_clocks[-1])
        return owl, time.monotonic()

    def detach_owl(owl: Any, since: float) -> None:
        owl.player.bus.remove_output(owl_clocks[-1])
        owl_clocks[-1].seconds = time.monotonic() - since

    samples: list[dict[str, Any]] = []
    # Night Owl's threads, running; the same after every restart.  (The
    # process's whole count also has the render stand-ins and threads
    # other tests left to finish.)
    owl_threads_at_start = len(_station_threads("nightowl"))
    workers = [threading.Thread(target=render, daemon=True) for _ in range(2)]
    started = time.monotonic()
    cpu_then, wall_then = time.process_time(), started
    owl, owl_since = attach_owl()
    next_cycle, next_sample = started + cycle, started
    restarts = 0
    for worker in workers:
        worker.start()
    try:
        while (now := time.monotonic()) < started + seconds:
            if now >= next_sample:
                cpu_now = time.process_time()
                samples.append(
                    {
                        "seconds": round(now - started, 1),
                        "rss_mb": _rss_mb(),
                        "cpu_percent": round(
                            100 * (cpu_now - cpu_then) / max(now - wall_then, 1e-6)
                        ),
                        "threads": threading.active_count(),
                        "restarts": restarts,
                    }
                )
                cpu_then, wall_then = cpu_now, now
                next_sample = now + sample_every
            if now >= next_cycle:
                detach_owl(owl, owl_since)
                old = owl
                assert client.post(f"{OWL}/stop").json()["state"] == "stopped"
                # Nothing of the stopped station is left running.
                _wait_for(lambda: not _station_threads("nightowl"), "Night Owl's threads to end")
                assert not old.bridge.stream._encoder.alive
                assert old.player._continuous_master.closed
                assert client.post(f"{OWL}/start").status_code == 200
                owl, owl_since = attach_owl()
                restarts += 1
                next_cycle = time.monotonic() + cycle
            time.sleep(0.05)
    finally:
        stop.set()
        for worker in workers:
            worker.join(60)
    detach_owl(owl, owl_since)
    elapsed = time.monotonic() - started
    registry.default.player.bus.remove_output(default_clock)
    default_clock.seconds = elapsed

    owl_blocks = sum(clock.blocks for clock in owl_clocks)
    owl_seconds = sum(clock.seconds for clock in owl_clocks)
    summary = {
        "seconds": round(elapsed),
        "restarts": restarts,
        "renders": renders[0],
        "default": (default_clock.blocks, round(elapsed / period)),
        "nightowl": (owl_blocks, round(owl_seconds / period)),
        "longest_gap_ms": {
            "default": round(default_clock.longest_gap * 1000),
            "nightowl": round(max(clock.longest_gap for clock in owl_clocks) * 1000),
        },
        "rss_mb": [sample["rss_mb"] for sample in (samples[0], samples[-1])],
        "nightowl_threads": (owl_threads_at_start, len(_station_threads("nightowl"))),
    }
    print(f"\nsoak: {json.dumps(summary)}")
    if report_path:
        with open(report_path, "a", encoding="utf-8") as report:
            for sample in samples:
                report.write(json.dumps(sample) + "\n")
            report.write(json.dumps({"summary": summary}) + "\n")
    assert restarts >= 1
    assert default_clock.blocks >= least * (elapsed / period), summary
    assert owl_blocks >= least * (owl_seconds / period), summary
    # Restarting leaks no threads (one more is allowed: the liner worker
    # starts with the first song).
    assert len(_station_threads("nightowl")) <= owl_threads_at_start + 1, summary


def test_a_socket_accepted_as_its_station_stops_is_closed(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.websockets import WebSocketDisconnect

    client, *_ = rig
    owl = client.app.state.stations.get("nightowl")
    real = server.resolve_station

    def resolve_then_stop(connection: Any) -> Any:
        station = real(connection)
        # The station starts stopping while this socket is being accepted,
        # after its pages' sockets were gathered to be closed.
        station.stopped = True
        return station

    monkeypatch.setattr(server, "resolve_station", resolve_then_stop)
    try:
        with (
            client.websocket_connect(f"{OWL}/ws") as socket,
            pytest.raises(WebSocketDisconnect) as closed,
        ):
            socket.receive_json()
        assert closed.value.code == 4409
        assert owl.ws_clients == set()
    finally:
        owl.stopped = False


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_a_start_that_fails_after_the_encoder_started_closes_it(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, *_ = rig
    encoders: list[FakeEncoder] = []

    class CountedEncoder(FakeEncoder):
        def __init__(self, bitrate: int) -> None:
            super().__init__(bitrate)
            encoders.append(self)

    def broken_liners(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("the liner folder could not be read")

    import autodj.liner_scheduler

    monkeypatch.setattr("autodj.stream.FfmpegEncoder", CountedEncoder)
    working = autodj.liner_scheduler.LinerScheduler
    # The stream's encoder is running by the time the liners are set up.
    monkeypatch.setattr(autodj.liner_scheduler, "LinerScheduler", broken_liners)
    refused = client.post(f"{OWL}/start")
    assert refused.status_code == 409
    assert "the liner folder could not be read" in refused.json()["detail"]
    assert len(encoders) == 1
    assert not encoders[0].alive
    assert FakeMaster.instances[-1].closed
    _wait_for(lambda: not _station_threads("nightowl"), "the station's threads to end")
    monkeypatch.setattr(autodj.liner_scheduler, "LinerScheduler", working)
    assert client.post(f"{OWL}/start").status_code == 200


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_a_station_that_fails_to_build_stops_its_player_and_master(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, *_ = rig

    def broken_runtime(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("the listener bans could not be read")

    # The mastering process and the player thread are running by then.
    monkeypatch.setattr(server, "StationRuntime", broken_runtime)
    refused = client.post(f"{OWL}/start")
    assert refused.status_code == 409
    assert "the listener bans could not be read" in refused.json()["detail"]
    assert FakeMaster.instances[-1].closed
    assert _station_threads("nightowl") == []
    assert _stations(client)["nightowl"]["state"] == "stopped"


def test_stopping_a_station_cancels_its_cart_timer(rig: Any) -> None:
    client, *_ = rig
    owl = client.app.state.stations.get("nightowl")
    carts = owl.carts
    fired: list[str] = []

    async def arm() -> None:
        async def ends() -> None:
            await asyncio.sleep(0.2)
            fired.append("end")

        carts._cart = (1, 1, "Cart 1, Station ID")
        carts._timer = asyncio.create_task(ends())

    client.portal.call(arm)
    timer = carts._timer
    assert client.post(f"{OWL}/stop").status_code == 200
    assert timer.cancelled()
    time.sleep(0.3)
    assert fired == []


TOKEN = "multi-station-public-test-token-32b"


@pytest.mark.parametrize("rig", [{"server_extra": f'access_token = "{TOKEN}"'}], indirect=True)
def test_each_station_has_its_own_public_status_page_and_feed(rig: Any, tmp_path: Path) -> None:
    """What a listener sees, unpaired: each station's page, feed and stream; nothing private."""
    client, *_ = rig
    registry = client.app.state.stations
    public = {
        "/status": 200,
        "/stations/default/status": 200,
        "/stations/nightowl/status": 200,
        "/api/nowplaying": 200,
        "/api/stations/default/nowplaying": 200,
        "/api/stations/nightowl/nowplaying": 200,
        "/stations/nightowl.m3u": 200,
        # Only a configured station's public addresses are public.
        "/stations/daybreak/status": 401,
        "/api/stations/daybreak/nowplaying": 401,
        "/stations/nightowl/status/x": 401,
        "/stations/nightowl/stream": 401,
        "/api/stations/nightowl/status": 401,
        "/api/stations/nightowl/stream": 401,
        "/api/stations": 401,
    }
    for path, status in public.items():
        assert client.get(path).status_code == status, path
    assert client.head("/stations/nightowl/status").status_code == 200
    assert client.post("/stations/nightowl/status").status_code == 405  # as /status

    page = client.get("/stations/nightowl/status")
    assert page.headers["content-type"].startswith("text/html")
    assert "script-src 'self'" in page.headers["content-security-policy"]
    html = page.text
    assert "<title>Night Owl Radio: station status</title>" in html
    assert 'data-feed="/api/stations/nightowl/nowplaying"' in html
    assert '<a href="/stations/nightowl.mp3">' in html
    assert 'download="nightowl.m3u"' in html
    default_page = client.get("/status").text
    assert "<title>AutoDJ: station status</title>" in default_page
    assert 'data-feed="/api/nowplaying"' in default_page
    assert '<a href="/stream.mp3">' in default_page

    feed = client.get("/api/stations/nightowl/nowplaying")
    assert feed.json()["station_name"] == "Night Owl Radio"
    assert feed.json()["stream_path"] == "/stations/nightowl.mp3"
    assert client.get("/api/nowplaying").json()["station_name"] == "AutoDJ"
    for text in (html, default_page, feed.text):
        assert TOKEN not in text
        assert str(tmp_path) not in text and tmp_path.as_posix() not in text
        assert "testserver" not in text

    # Its request page, which only this station turns on.
    requests = client.get("/stations/nightowl/requests")
    assert requests.status_code == 200
    assert "<title>Night Owl Radio: request a song</title>" in requests.text
    assert '<form method="get" action="/stations/nightowl/requests" role="search">' in requests.text
    assert client.get("/requests").status_code == 404
    assert client.get("/stations/default/requests").status_code == 404
    assert client.get("/stations/daybreak/requests").status_code == 401

    # Stopped: every public address says "off air" (503); only its name shows.
    client.portal.call(registry.stop, "nightowl")
    off = client.get("/stations/nightowl/status")
    assert off.status_code == 503
    assert "Night Owl Radio is off air right now." in off.text
    assert 'data-feed="/api/stations/nightowl/nowplaying"' in off.text
    stopped_feed = client.get("/api/stations/nightowl/nowplaying")
    assert stopped_feed.status_code == 503
    assert stopped_feed.json() == {"detail": "That station is off air"}
    assert stopped_feed.headers["access-control-allow-origin"] == "*"
    assert client.get("/stations/nightowl.mp3").status_code == 503
    off_requests = client.get("/stations/nightowl/requests")
    assert off_requests.status_code == 503
    assert "Night Owl Radio is off air right now, so it is not taking requests." in (
        off_requests.text
    )
    assert client.get("/stations/daybreak/status").status_code == 401
    # The default station is untouched.
    assert client.get("/status").status_code == 200
    assert client.get("/api/nowplaying").status_code == 200


def test_the_station_list_reports_each_stations_public_addresses(rig: Any) -> None:
    client, *_ = rig
    # Each station's requests are its own: only Night Owl takes them.
    owl_requests = client.get(f"{OWL}/requests").json()
    assert (owl_requests["enabled"], owl_requests["approval"]) == (True, "auto")
    assert client.get("/api/requests").json()["enabled"] is False
    listing = _stations(client)
    assert {
        key: listing["default"][key] for key in ("state", "status_path", "nowplaying_path")
    } == {
        "state": "idle",
        "status_path": "/status",
        "nowplaying_path": "/api/nowplaying",
    }
    assert (listing["default"]["stream_path"], listing["default"]["m3u_path"]) == (
        "/stream.mp3",
        "/stream.m3u",
    )
    assert client.post(f"{OWL}/stop").status_code == 200
    owl = _stations(client)["nightowl"]
    assert owl["state"] == "stopped"
    assert owl["status_path"] == "/stations/nightowl/status"
    assert owl["nowplaying_path"] == "/api/stations/nightowl/nowplaying"
    # Each listed address answers as the station's state says.
    for key in ("status_path", "nowplaying_path", "stream_path"):
        assert client.get(owl[key]).status_code == 503, key
    for key in ("status_path", "nowplaying_path", "m3u_path"):
        assert client.get(listing["default"][key]).status_code == 200, key


OWL_SECRETS = """
[stations.nightowl.stream]
listener_user = "owl"
listener_password = "night-owl-listener-secret"

[[stations.nightowl.webhooks]]
url = "https://hooks.example.net/night-owl-private-path"
"""


@pytest.mark.parametrize("rig", [{"local": OWL_SECRETS}], indirect=True)
def test_listener_credentials_and_webhooks_are_each_stations_own(rig: Any) -> None:
    import base64

    client, *_ = rig
    stations = client.app.state.stations
    owl = stations.get("nightowl")
    # Night Owl's listener password guards its stream only.
    assert client.head("/stations/nightowl.mp3").status_code == 401
    good = base64.b64encode(b"owl:night-owl-listener-secret").decode()
    authorised = client.head("/stations/nightowl.mp3", headers={"Authorization": f"Basic {good}"})
    assert authorised.status_code == 200
    assert client.head("/stream.mp3").status_code == 200
    # Its status page and feed stay public, and say nothing of the secrets.
    for path in ("/stations/nightowl/status", "/api/stations/nightowl/nowplaying"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "night-owl-listener-secret" not in response.text
        assert "night-owl-private-path" not in response.text
        assert "hooks.example.net" not in response.text
    # Night Owl's webhook is its own: the default station has none.
    assert client.get(f"{OWL}/webhooks").json()[0]["host"] == "hooks.example.net"
    assert client.get("/api/webhooks").json() == []
    assert [config.url for config in owl.webhooks.configs] == [
        "https://hooks.example.net/night-owl-private-path"
    ]
    assert stations.default.webhooks.configs == ()


# Every field the public feed may carry, and in each song and history row.
FEED_FIELDS = {
    "schema",
    "station_name",
    "current_track",
    "next_track",
    "history",
    "listener_count",
    "stream_path",
    "peak_listeners_today",
    "uptime_seconds",
}
TRACK_FIELDS = {"artist", "title", "album", "duration", "started_at", "elapsed"}
HISTORY_FIELDS = {"artist", "title", "duration", "played_at"}


def test_every_public_feed_carries_only_public_fields_to_any_site(rig: Any) -> None:
    client, _library, _cfg, song, _closes = rig
    owl = client.app.state.stations.get("nightowl")
    # A song playing and one in the history, so every part of the feed is filled.
    owl.player._state.current_track = song
    owl.bridge.on_track_started(song)
    owl.nowplaying.refresh()
    for path in ("/api/nowplaying", "/api/stations/default/nowplaying", f"{OWL}/nowplaying"):
        for method in ("GET", "HEAD"):
            response = client.request(method, path)
            assert response.status_code == 200, (method, path)
            assert response.headers["access-control-allow-origin"] == "*", (method, path)
            assert response.headers["cache-control"] == "no-cache", (method, path)
        feed = client.get(path).json()
        assert set(feed) == FEED_FIELDS, path
        for track in (feed["current_track"], feed["next_track"]):
            assert track is None or set(track) <= TRACK_FIELDS, path
        for row in feed["history"]:
            assert set(row) <= HISTORY_FIELDS, path
    owl_feed = client.get(f"{OWL}/nowplaying").json()
    assert owl_feed["current_track"]["title"] == song.title
    assert song.path not in client.get(f"{OWL}/nowplaying").text


def test_a_stop_that_meets_a_problem_still_stops_and_says_what_failed(
    rig: Any, caplog: pytest.LogCaptureFixture
) -> None:
    client, *_ = rig
    owl = client.app.state.stations.get("nightowl")
    recorder = owl.recorder
    real_close = recorder.close

    def broken_close() -> None:
        real_close()
        raise OSError("the recording state could not be saved")

    recorder.close = broken_close
    with caplog.at_level(logging.INFO):
        response = client.post(f"{OWL}/stop")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "stopped"
    assert body["warnings"] == ["Closing its recorder failed; the server log has the details."]
    messages = [record.getMessage() for record in caplog.records]
    assert any("stopped with problems: Closing its recorder failed" in m for m in messages)
    assert not any(m.endswith("Station nightowl (Night Owl Radio) stopped") for m in messages)
    # A clean stop has no warnings.
    assert client.post(f"{OWL}/start").status_code == 200
    assert client.post(f"{OWL}/stop").json()["warnings"] == []
