"""Two stations on one server, both streaming, with fake audio (#70, PR 5).

The default station and Night Owl Radio (``nightowl``) are built the way
``serve()`` builds them: real players, mix buses, stream outputs, station
controllers and recorders, sharing one library.  ffmpeg is replaced by an
encoder that echoes PCM, and the mastering process by a pass-through, so
nothing outside the test process runs.
"""

from __future__ import annotations

import json
import logging
import os
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
        **values,
    }
    (tmp_path / "Music").mkdir(exist_ok=True)
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(root=tmp_path.as_posix(), **settings),
        encoding="utf-8",
    )
    return path


def _station_threads(slug: str) -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name.endswith(f"-{slug}")]


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
        "stream_path": "/stations/nightowl.mp3",
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


SOAK_SECONDS = float(os.environ.get("AUTODJ_SOAK_SECONDS", "0"))


@pytest.mark.skipif(
    not SOAK_SECONDS, reason="Set AUTODJ_SOAK_SECONDS to measure two stations rendering at once"
)
@pytest.mark.timeout(SOAK_SECONDS + 120)
def test_soak_two_stations_rendering_at_once_keep_their_buses_on_time(rig: Any) -> None:
    """Both mix buses keep time while both stations render, sharing one GIL.

    Each station's render-ahead work is stood in for by a decode-sized
    buffer time-stretched with scipy, back to back, for the whole run.
    Every 20 ms block each bus emits is counted; the bus drops blocks only
    after falling more than 8 behind, so a shortfall means an underrun.
    """
    from scipy.signal import resample_poly

    client, *_ = rig
    stations = client.app.state.stations.all()
    period = 882 / 44100
    stamps: dict[str, list[float]] = {station.slug: [] for station in stations}

    class Clock:
        def __init__(self, slug: str) -> None:
            self.slug = slug

        def write(self, _block: np.ndarray) -> None:
            stamps[self.slug].append(time.monotonic())

        def close(self) -> None:
            pass

    stop = threading.Event()
    renders = [0]

    def render() -> None:
        rng = np.random.default_rng(0)
        while not stop.is_set():
            track = rng.standard_normal((44100 * 240, 2), dtype=np.float32)
            stretched = resample_poly(track, 51, 50, axis=0)
            fade = np.linspace(0.0, 1.0, len(stretched), dtype=np.float32)
            stretched *= fade[:, None]
            renders[0] += 1

    outputs = {station.slug: Clock(station.slug) for station in stations}
    for station in stations:
        station.player.bus.add_output(outputs[station.slug])
    workers = [threading.Thread(target=render, daemon=True) for _ in stations]
    started = time.monotonic()
    for worker in workers:
        worker.start()
    time.sleep(SOAK_SECONDS)
    stop.set()
    elapsed = time.monotonic() - started
    for worker in workers:
        worker.join(60)
    for station in stations:
        station.player.bus.remove_output(outputs[station.slug])
    report = {}
    for slug, times in stamps.items():
        gaps = np.diff(times) if len(times) > 1 else np.array([0.0])
        report[slug] = (len(times), round(float(gaps.max()) * 1000), renders[0])
    print(f"\nsoak {elapsed:.0f} s, expected {elapsed / period:.0f} blocks each: {report}")
    for slug, times in stamps.items():
        assert len(times) >= 0.99 * (elapsed / period), (slug, report)
