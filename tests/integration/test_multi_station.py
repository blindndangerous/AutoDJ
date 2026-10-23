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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest
from fastapi.testclient import TestClient

from autodj import server
from autodj.config import load_config, move_station_tables
from autodj.dmca import complement
from autodj.library import LibraryManager
from autodj.repeat_rules import TrackKeyCache
from autodj.similarity import SimilarityIndex
from autodj.stream import Listener
from tests.integration._helpers import patch_station_settings
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
[stations.nightowl.player_page]
enabled = true
embed = true
embed_origins = ["https://nightowl.example"]
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
        self.startup_pending = False
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


def _running_threads(slug: str) -> list[str]:
    """The names of *slug*'s threads once its player has started its mix bus.

    The player thread starts the live playout worker and the bus's stop
    watcher only after it opens its cue cache, so a count taken as soon as
    the station starts can miss both; a slow machine (Windows CI) did.
    """
    started = {f"autodj-live-playout-{slug}", f"autodj-bus-stop-{slug}"}
    _wait_for(
        lambda: started <= {thread.name for thread in _station_threads(slug)},
        f"{slug}'s mix bus to start",
    )
    return sorted(thread.name for thread in _station_threads(slug))


def _without_liner_workers(names: list[str]) -> list[str]:
    # The liner worker starts with the first song, so a restart may not have it yet.
    return [name for name in names if not name.startswith("autodj-liner-")]


def _wait_for_new_threads_to_end(slug: str, already: set[threading.Thread], what: str) -> None:
    """Wait for *slug*'s threads started since *already* was taken to end."""

    def new() -> list[threading.Thread]:
        return [thread for thread in _station_threads(slug) if thread not in already]

    try:
        _wait_for(lambda: not new(), what)
    except AssertionError as exc:
        raise AssertionError(f"{exc}: {sorted(thread.name for thread in new())}") from None


def _wait_for(predicate: Any, what: str) -> None:
    deadline = time.monotonic() + WAIT
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Any:
    """Build the app as serve() does; yield (client, library, config); shut down after."""
    # Threads another test in this worker left behind are not this rig's to wait for.
    already = set(_station_threads("default")) | set(_station_threads("nightowl"))
    values = getattr(request, "param", {})
    FakeMaster.instances = []
    monkeypatch.setattr("autodj.stream.FfmpegEncoder", FakeEncoder)
    monkeypatch.setattr("autodj.server.shutil.which", lambda _name: "ffmpeg")
    monkeypatch.setattr("autodj.stream.probe_encoder", lambda *_args: None)
    monkeypatch.setattr("autodj.mastering.ContinuousMaster", FakeMaster)
    closes: list[str] = []
    real_close = __import__("autodj.dj_meta", fromlist=["close_cache"]).close_cache

    def close_cache() -> None:
        closes.append("closed")
        real_close()

    monkeypatch.setattr("autodj.dj_meta.close_cache", close_cache)
    cfg = load_config(_write_config(tmp_path, **values), environ={})
    if move_station_tables(cfg):
        cfg = load_config(cfg.config_path, environ={})
    sim = SimilarityIndex.empty()
    library = LibraryManager(cfg, sim)
    library.catalog.add_source(cfg.library.music_dir)
    # One known song, so queue routes have something to queue.
    song = make_entry(tmp_path / "Music" / "song.flac")
    monkeypatch.setattr(
        library, "entry_for_path", lambda path: song if str(path) == song.path else None
    )
    from autodj._bridge import LibraryServices

    default = server._extra_station(cfg, cfg.default_station, sim, library, "UTC")
    extras = [server._extra_station(cfg, spec, sim, library, "UTC") for spec in cfg.stations]
    app = server.create_app(
        services=LibraryServices(cfg, sim, library),
        default_station=default,
        extra_stations=extras,
    )
    with TestClient(app, client=("192.0.2.4", 50000)) as client:
        yield client, library, cfg, song, closes
    _wait_for_new_threads_to_end("default", already, "the default station's threads to end")
    _wait_for_new_threads_to_end("nightowl", already, "Night Owl's threads to end")
    assert closes == ["closed"]  # once, after both players were quiet


def _stations(client: TestClient) -> dict[str, dict[str, Any]]:
    response = client.get("/api/stations")
    assert response.status_code == 200
    return {entry["slug"]: entry for entry in response.json()["stations"]}


def test_track_info_uses_selected_station_play_history(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, library, _cfg, song, _closes = rig
    library.catalog.put_entry(song)
    other = make_entry(Path(song.path).with_name("never-played.flac"))
    library.catalog.put_entry(other)
    entries = {song.path: song, other.path: other}
    monkeypatch.setattr(library, "entry_for_path", lambda path: entries.get(str(path)))
    library.station_ledger("default").remember_play(song.path, 10)
    library.station_ledger("nightowl").remember_play(song.path, 20)

    response = client.get("/api/library/points", params={"path": song.path})
    assert response.status_code == 200, response.text
    assert response.json().get("last_played") == 10, response.text
    assert (
        client.get("/api/library/points", params={"path": song.path, "station": "nightowl"}).json()[
            "last_played"
        ]
        == 20
    )
    assert (
        client.get(
            "/api/library/points", params={"path": other.path, "station": "nightowl"}
        ).json()["last_played"]
        == 0
    )
    assert (
        client.get(
            "/api/library/points", params={"path": song.path, "station": "missing"}
        ).status_code
        == 404
    )


def test_create_station_registers_stopped_and_persists(rig: Any) -> None:
    client, library, cfg, _song, _closes = rig
    response = client.post(
        "/api/stations",
        json={"slug": "new", "name": "New Radio"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["state"] == "stopped"
    assert response.json()["defined_in"] == "web"
    assert _stations(client)["new"]["defined_in"] == "web"
    assert client.get("/api/stations/new/status").status_code == 409
    assert client.get("/stations/new/status").status_code == 503
    assert library.catalog.last_played("new") == {}
    assert {path.name for path in (cfg.index.stations_dir / "new").iterdir()} == {"station.json"}
    loaded = load_config(cfg.config_path, environ={})
    assert loaded.stations[-1].slug == "new"
    assert loaded.stations[-1].autostart is False
    assert (
        client.post("/api/stations/new/start", headers={"Origin": "http://testserver"}).status_code
        == 200
    )
    assert client.get("/stations/new/status").status_code == 200
    with client.websocket_connect("/api/stations/new/ws") as socket:
        assert socket.receive_json()
        assert len(client.app.state.stations.get("new").ws_clients) == 1


def test_start_with_the_server_is_saved_for_a_station_made_in_the_page(rig: Any) -> None:
    client, _library, cfg, _song, _closes = rig
    origin = {"Origin": "http://testserver"}
    assert client.post("/api/stations", json={"slug": "new", "name": "New Radio"}, headers=origin)
    assert _stations(client)["new"]["autostart"] is False
    assert _stations(client)["new"]["autostart_locked"] is False
    response = client.put("/api/stations/new/autostart", json={"enabled": True}, headers=origin)
    assert response.status_code == 200, response.text
    assert response.json()["autostart"] is True
    assert load_config(cfg.config_path, environ={}).stations[-1].autostart is True
    # The canonical default and migrated station definitions are editable.
    assert _stations(client)["default"]["autostart_locked"] is False
    default_update = client.put(
        "/api/stations/default/autostart", json={"enabled": False}, headers=origin
    )
    assert default_update.status_code == 200
    assert default_update.json()["autostart"] is False
    assert load_config(cfg.config_path, environ={}).default_station.autostart is False
    assert _stations(client)["nightowl"]["autostart_locked"] is False
    editable = client.put(
        "/api/stations/nightowl/autostart", json={"enabled": False}, headers=origin
    )
    assert editable.status_code == 200
    assert editable.json()["autostart"] is False
    missing = client.put("/api/stations/nobody/autostart", json={"enabled": True}, headers=origin)
    assert missing.status_code == 404


def test_create_station_rejects_conflicts_and_bad_flags(rig: Any) -> None:
    client, library, cfg, _song, _closes = rig
    headers = {"Origin": "http://testserver"}

    def post(slug: str, name: str, **options: Any) -> Any:
        return client.post(
            "/api/stations", json={"slug": slug, "name": name, **options}, headers=headers
        )

    assert post("default", "Other").status_code == 422
    assert post("Upper", "Other").status_code == 422
    assert post("new", "Other", copy_carts=True).status_code == 422
    assert post("nightowl", "Other").status_code == 409
    assert post("new", " NIGHT OWL RADIO ").status_code == 409
    assert post("new", "Other", source="missing").status_code == 404
    (cfg.index.stations_dir / "leftover").mkdir(parents=True)
    assert post("leftover", "Other").status_code == 409
    library.catalog.remember_play("old-song.flac", station="history")
    assert post("history", "Other").status_code == 409
    assert post("new", "Other").status_code == 201
    assert post("new", "Another").status_code == 409


def test_create_rolls_back_after_registration_fails(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, library, cfg, _song, _closes = rig
    library.station_ledger("nightowl").store.set_setting("clock", "Evening")

    def fail(_extra: Any) -> None:
        raise RuntimeError("injected failure")

    monkeypatch.setattr(client.app.state.stations, "add", fail)
    response = client.post(
        "/api/stations",
        json={
            "slug": "new",
            "name": "New Radio",
            "source": "nightowl",
            "copy_clockwheels": True,
        },
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 409
    assert not (cfg.index.stations_dir / "new").exists()
    assert library.station_ledger("new").get_setting("clock") is None
    assert "new" not in client.app.state.stations.slugs()


def test_create_checks_recording_conflict_and_station_limit(rig: Any) -> None:
    client, _library, cfg, _song, _closes = rig
    body = {"slug": "new", "name": "New Radio"}
    original = cfg.stations[0].cfg.recording.folder
    cfg.stations[0].cfg.recording.folder = cfg.recording.folder / "new"
    response = client.post("/api/stations", json=body, headers={"Origin": "http://testserver"})
    assert response.status_code == 409
    assert "recording folder" in response.json()["detail"]
    cfg.stations[0].cfg.recording.folder = original
    cfg.stations = cfg.stations * 8
    response = client.post("/api/stations", json=body, headers={"Origin": "http://testserver"})
    assert response.status_code == 409
    assert "At most 8" in response.json()["detail"]


def test_duplicate_copies_active_clock_setting(rig: Any) -> None:
    client, library, cfg, _song, _closes = rig
    library.station_ledger("nightowl").store.set_setting("clock", "Evening")
    response = client.post(
        "/api/stations",
        json={
            "slug": "new",
            "name": "New Radio",
            "source": "nightowl",
            "copy_clockwheels": True,
        },
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 201, response.text
    assert library.station_ledger("new").get_setting("clock") == "Evening"
    assert load_config(cfg.config_path, environ={}).stations[-1].defined_in == "web"


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
    assert owl.paths.state_dir == cfg.index.stations_dir / "nightowl"
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
        "defined_in": "web",
        "name": "Night Owl Radio",
        "state": "idle",
        "listeners": 0,
        "title": None,
        "status_path": "/stations/nightowl/status",
        "nowplaying_path": "/api/stations/nightowl/nowplaying",
        "stream_path": "/nightowl.mp3",
        "m3u_path": "/nightowl.m3u",
        "player_path": "/stations/nightowl/player",
        "embed_path": "/stations/nightowl/embed",
        "autostart": True,
        "autostart_locked": False,
    }
    assert listing["default"]["player_path"] is None
    assert listing["default"]["embed_path"] is None
    stream = client.get(f"{OWL}/stream").json()
    assert (stream["path"], stream["m3u_path"]) == (
        "/nightowl.mp3",
        "/nightowl.m3u",
    )
    assert stream["bitrate"] == 192
    assert client.get("/api/stream").json()["path"] == "/stream.mp3"
    assert client.get(f"{OWL}/nowplaying").json()["stream_path"] == "/nightowl.mp3"


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

    assert (
        patch_station_settings(client, {"transition": {"effect": "echo_out"}}, base=OWL).status_code
        == 200
    )
    assert owl.cfg.transitions.effect == "echo_out"
    assert cfg.transitions.effect == "none"
    saved = json.loads((owl.paths.state_dir / "web_state.json").read_text(encoding="utf-8"))
    assert saved["transition"] == "echo_out"
    assert not ((cfg.index.stations_dir / "default") / "web_state.json").exists()


def test_listeners_bans_and_recording_are_per_station(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _library, cfg, _song, _closes = rig
    stations = client.app.state.stations
    default, owl = stations.default, stations.get("nightowl")

    playlist = client.get("/nightowl.m3u")
    assert playlist.status_code == 200
    assert playlist.text == "http://testserver/nightowl.mp3\n"

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
    audio = client.get("/nightowl.mp3", headers={"Icy-MetaData": "1"})
    assert audio.status_code == 200
    assert audio.headers["icy-name"] == "Night Owl Radio"
    assert audio.headers["icy-br"] == "192"
    assert audio.content
    assert "content-encoding" not in audio.headers  # a stream is never gzipped
    assert heard == {"owl": 1, "default": 0, "listed": ["192.0.2.4"], "default listed": []}
    _wait_for(lambda: owl.bridge.stream.listener_count == 0, "the listener to leave")

    banned = client.post(f"{OWL}/stream/bans", json={"address": "192.0.2.4"})
    assert banned.status_code == 200
    assert client.get("/nightowl.mp3").status_code == 403
    assert client.head("/stream.mp3").status_code == 200
    assert (owl.paths.state_dir / "listener_bans.json").is_file()
    assert not ((cfg.index.stations_dir / "default") / "listener_bans.json").exists()

    assert client.post(f"{OWL}/recording/start").status_code == 200
    assert client.get(f"{OWL}/recording").json()["running"] is True
    assert client.get("/api/recording").json()["running"] is False
    assert (owl.paths.state_dir / "recording.json").is_file()
    assert client.post(f"{OWL}/recording/stop").status_code == 200


def test_unknown_station_streams_are_not_found(rig: Any) -> None:
    client, *_ = rig
    assert client.get("/daybreak.mp3").status_code == 404
    assert client.get("/stations/nightowl.ogg").status_code == 404
    assert client.get("/stations/nightowl").status_code == 404
    assert client.post("/api/stations/daybreak/start").status_code == 404
    assert client.post("/api/stations/daybreak/stop").status_code == 404


@pytest.mark.parametrize(
    "rig",
    [{"default_processing": "stereotool", "owl_processing": "stereotool"}],
    indirect=True,
)
def test_stop_and_start_clean_up_one_of_two_stereo_tool_stations(rig: Any) -> None:
    client, _library, _cfg, _song, _closes = rig
    stations = client.app.state.stations
    owl = stations.get("nightowl")
    master = owl.player._continuous_master
    default_master = stations.default.player._continuous_master
    encoder_stream = owl.bridge.stream
    with client.websocket_connect(f"{OWL}/ws") as socket:
        response = client.post(f"{OWL}/stop")
        assert response.status_code == 200
        assert response.json()["state"] == "stopped"
        with pytest.raises(Exception):  # noqa: B017 -- the server closed it (4409)
            socket.receive_json()
    assert _station_threads("nightowl") == []
    assert master.closed
    assert not default_master.closed
    assert encoder_stream.listener_count == 0
    assert stations.get("nightowl").stopped
    assert client.get(f"{OWL}/status").status_code == 409
    assert client.get("/nightowl.mp3").status_code == 503
    assert _stations(client)["nightowl"]["state"] == "stopped"
    # Stopping again, or a running station's start, changes nothing.
    assert client.post(f"{OWL}/stop").status_code == 200

    started = client.post(f"{OWL}/start")
    assert started.status_code == 200
    assert started.json()["state"] == "idle"
    fresh = stations.get("nightowl")
    assert fresh is not owl
    assert fresh.player._continuous_master is not master
    assert stations.default.player._continuous_master is default_master
    assert not default_master.closed
    assert client.get(f"{OWL}/status").status_code == 200
    assert client.post(f"{OWL}/start").json()["slug"] == "nightowl"
    assert stations.get("nightowl") is fresh

    assert client.post("/api/stations/default/stop").status_code == 200
    assert default_master.closed
    assert stations.get("nightowl") is fresh
    assert not fresh.stopped


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
def test_stopped_station_time_and_output_settings_round_trip(rig: Any) -> None:
    from autodj import timefmt

    client, _library, _cfg, _song, _closes = rig
    station_settings = f"{OWL}/station-settings"
    time_settings = f"{OWL}/time-settings"
    definition = client.app.state.stations._extras["nightowl"].paths.definition
    original_format = timefmt.time_format
    assert _stations(client)["nightowl"]["state"] == "stopped"

    time_response = patch_station_settings(
        client,
        {"time": {"timezone": "America/New_York", "time_format": "12"}},
        base=OWL,
        headers={"Origin": "http://testserver"},
    )
    assert time_response.status_code == 200
    time_values = time_response.json()["settings"]["values"]["time"]
    assert time_values["timezone"] == "America/New_York"
    assert time_values["time_format"] == "12"

    output_response = patch_station_settings(
        client,
        {
            "station": {
                "output": "stream",
                "server_audio_too": True,
                "liner_upload_max_mib": 12,
            }
        },
        base=OWL,
        headers={"Origin": "http://testserver"},
    )
    assert output_response.status_code == 200
    station_values = output_response.json()["settings"]["values"]["station"]
    assert station_values == {
        "liner_upload_max_mib": 12,
        "output": "stream",
        "server_audio_too": True,
    }
    assert client.get(time_settings).json()["timezone"] == "America/New_York"
    assert client.get(station_settings).json()["output"] == "stream"
    assert _stations(client)["nightowl"]["state"] == "stopped"

    unchanged = definition.read_bytes()
    invalid_updates = [
        {"station": {"liner_upload_max_mib": 0}},
        {"station": {"output": "headphones"}},
        {"station": {"timezone": "Mars/Olympus_Mons"}},
        {"time": {"timezone": "Mars/Olympus_Mons", "time_format": "24"}},
    ]
    for changes in invalid_updates:
        response = patch_station_settings(
            client, changes, base=OWL, headers={"Origin": "http://testserver"}
        )
        assert response.status_code == 400
        assert definition.read_bytes() == unchanged

    timefmt.time_format = original_format


def test_public_settings_save_live_and_while_stopped(rig: Any) -> None:
    client, _library, cfg, *_ = rig
    registry = client.app.state.stations
    definition = registry._extras["nightowl"].paths.definition
    headers = {"Origin": "http://testserver"}
    default_before = client.get("/api/public-settings").json()

    enabled = patch_station_settings(
        client, {"public": {"dmca_enabled": True}}, base=OWL, headers=headers
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["settings"]["values"]["public"]["dmca_enabled"] is True
    assert registry.get("nightowl").player._state.dmca_enabled is True
    assert client.get("/stations/nightowl/player").status_code == 200

    origin = patch_station_settings(
        client, {"public": {"embed_origins": ["https://example.org"]}}, base=OWL, headers=headers
    )
    assert origin.status_code == 200, origin.text
    assert (
        client.get("/stations/nightowl/embed")
        .headers["content-security-policy"]
        .endswith("frame-ancestors 'self' https://example.org")
    )
    wildcard = patch_station_settings(
        client, {"public": {"embed_origins": ["*"]}}, base=OWL, headers=headers
    )
    assert wildcard.status_code == 200, wildcard.text
    assert (
        client.get("/stations/nightowl/embed")
        .headers["content-security-policy"]
        .endswith("frame-ancestors *")
    )

    changed = patch_station_settings(
        client,
        {
            "public": {
                "player_page_enabled": False,
                "embed_enabled": False,
                "embed_origins": ["https://example.org"],
            }
        },
        base=OWL,
        headers=headers,
    )
    assert changed.status_code == 200, changed.text
    assert client.get("/stations/nightowl/player").status_code == 404
    assert client.get("/stations/nightowl/embed").status_code == 404
    assert client.get("/api/public-settings").json() == default_before
    saved = json.loads(definition.read_text(encoding="utf-8"))["station"]
    assert saved["dmca"] == {"enabled": True}
    assert saved["player_page"] == {
        "enabled": False,
        "embed": False,
        "embed_origins": ["https://example.org"],
    }

    unchanged = definition.read_bytes()
    for body in (
        {"dmca_enabled": "true"},
        {"embed_origins": ["ftp://example.org"]},
        {"embed_enabled": 1},
        {"unknown": True},
        {},
    ):
        invalid = patch_station_settings(client, {"public": body}, base=OWL, headers=headers)
        assert invalid.status_code in (400, 422), invalid.text
        if body == {"embed_origins": ["ftp://example.org"]}:
            assert invalid.json() == {
                "detail": "Sites allowed to embed: 'ftp://example.org' is not an http:// or "
                "https:// site address, such as https://radio.example.net.",
                "field": "embed_origins",
            }
        assert definition.read_bytes() == unchanged

    assert client.post(f"{OWL}/stop").status_code == 200
    stopped = patch_station_settings(
        client, {"public": {"player_page_enabled": True}}, base=OWL, headers=headers
    )
    assert stopped.status_code == 200, stopped.text
    assert client.get("/stations/nightowl/player").status_code == 503
    assert client.get("/stations/nightowl/embed").status_code == 404
    assert client.post(f"{OWL}/start").status_code == 200
    assert registry.get("nightowl").player._state.dmca_enabled is True
    assert client.get("/stations/nightowl/player").status_code == 200

    reloaded = load_config(cfg.config_path, environ={})
    nightowl = next(station for station in reloaded.stations if station.slug == "nightowl")
    assert nightowl.cfg.dmca.enabled is True
    assert nightowl.cfg.player_page.enabled is True
    assert nightowl.cfg.player_page.embed is False


def test_enabling_dmca_refreshes_public_feed_and_saved_play_limits(rig: Any) -> None:
    client, _library, _cfg, song, _closes = rig
    station = client.app.state.stations.get("nightowl")
    state = station.player._state
    station.cfg.nowplaying.show_next = True
    state.next_track = song
    station.nowplaying.refresh()
    assert station.nowplaying.snapshot()["next_track"]["title"] == song.title
    assert '<section id="status-next-section">' in client.get("/stations/nightowl/status").text

    # Saved plays outlive the short repeat-rule window while DMCA mode is off.
    assert state.rules.keep_seconds < 100 * 60
    saved_at = time.time()
    station.ledger.store.write_history(
        [
            {
                "event_id": f"dmca-{index}",
                "path": str(Path(song.path).with_name(f"played-{index}.flac")),
                "title": f"Played {index}",
                "artist": song.artist,
                "album": song.album,
                "duration": 180.0,
                "played_at": datetime.fromtimestamp(saved_at - minutes * 60, UTC).isoformat(),
            }
            for index, minutes in enumerate((120, 110, 100))
        ],
        [],
    )
    assert not any(record.path.endswith("played-0.flac") for record in state.play_history)

    response = patch_station_settings(
        client,
        {"public": {"dmca_enabled": True}},
        base=OWL,
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200, response.text
    assert station.nowplaying.snapshot()["next_track"] is None
    assert client.get(f"{OWL}/nowplaying").json()["next_track"] is None
    assert (
        '<section id="status-next-section" hidden>' in client.get("/stations/nightowl/status").text
    )
    assert {Path(record.path).stem for record in state.play_history} >= {
        "played-0",
        "played-1",
        "played-2",
    }
    assert any(
        "more than 3 songs from the album" in limit
        for limit in complement(list(state.play_history), time.time(), TrackKeyCache()).broken(song)
    )
    disabled = patch_station_settings(
        client,
        {"public": {"dmca_enabled": False}},
        base=OWL,
        headers={"Origin": "http://testserver"},
    )
    assert disabled.status_code == 200, disabled.text
    assert station.nowplaying.snapshot()["next_track"]["title"] == song.title


def test_a_restarted_station_runs_the_same_threads_each_once(rig: Any) -> None:
    client, *_ = rig
    running = _without_liner_workers(_running_threads("nightowl"))
    assert len(running) == len(set(running))
    for _ in range(3):
        assert client.post(f"{OWL}/stop").json()["state"] == "stopped"
        _wait_for(lambda: not _station_threads("nightowl"), "Night Owl's threads to end")
        assert client.post(f"{OWL}/start").status_code == 200
        assert _without_liner_workers(_running_threads("nightowl")) == running


@pytest.mark.parametrize(
    "rig",
    [
        {"autostart": "false"},
        {"autostart": "false", "default_processing": "stereotool"},
    ],
    indirect=True,
)
def test_a_station_that_fails_half_way_through_starting_leaves_nothing_running(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _library, cfg, *_ = rig
    default_master = client.app.state.stations.default.player._continuous_master

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
    assert not default_master.closed
    assert (
        client.app.state.stations.default.cfg.playback.loudness_processing
        == cfg.playback.loudness_processing
    )
    monkeypatch.setattr(autodj.recorder, "Recorder", working)
    assert client.post(f"{OWL}/start").status_code == 200


@pytest.mark.parametrize(
    "rig",
    [{"autostart": "false", "default_processing": "stereotool", "owl_processing": "stereotool"}],
    indirect=True,
)
def test_one_failed_stereo_tool_start_leaves_the_other_instance_running(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, *_ = rig
    default = client.app.state.stations.default
    default_master = default.player._continuous_master

    def broken_master(**kwargs: Any) -> None:
        if kwargs["mode"]() == "stereotool":
            raise OSError("the Stereo Tool processor could not start")
        raise AssertionError("the requested independent processor should be Stereo Tool")

    monkeypatch.setattr("autodj.mastering.ContinuousMaster", broken_master)
    failed = client.post(f"{OWL}/start")
    assert failed.status_code == 409
    assert "Stereo Tool processor could not start" in failed.json()["detail"]
    assert not default_master.closed
    assert default.cfg.playback.loudness_processing == "stereotool"
    assert client.app.state.stations.get("nightowl").stopped
    assert _station_threads("nightowl") == []


@pytest.mark.parametrize(
    "rig",
    [{"default_processing": "stereotool", "owl_processing": "stereotool"}],
    indirect=True,
)
def test_two_stations_can_start_with_independent_stereo_tool_instances(rig: Any) -> None:
    client, *_ = rig
    assert _stations(client)["default"]["state"] != "stopped"
    assert _stations(client)["nightowl"]["state"] != "stopped"
    assert _station_threads("nightowl")
    owl = client.app.state.stations.get("nightowl")
    assert owl.cfg.playback.loudness_processing == "stereotool"


@pytest.mark.parametrize("rig", [{"default_processing": "stereotool"}], indirect=True)
def test_two_running_stations_can_switch_to_stereo_tool_independently(rig: Any) -> None:
    client, *_ = rig
    owl = client.app.state.stations.get("nightowl")
    changed = patch_station_settings(
        client, {"playback": {"loudness_processing": "stereotool"}}, base=OWL
    )
    assert changed.status_code == 200, changed.text
    assert owl.cfg.playback.loudness_processing == "stereotool"
    kept = patch_station_settings(client, {"playback": {"loudness_processing": "stereotool"}})
    assert kept.status_code == 200, kept.text
    assert client.app.state.stations.default.cfg.playback.loudness_processing == "stereotool"


def test_a_preset_any_station_uses_cannot_be_deleted(rig: Any) -> None:
    client, *_ = rig
    upload = client.post(
        "/api/stereo-tool-presets/upload",
        files={"file": ("Owl.sts", b"[Stereo Tool]\nVersion=1\n", "text/plain")},
    )
    assert upload.status_code == 200, upload.text
    chosen = patch_station_settings(
        client, {"playback": {"stereotool_preset": "Owl.sts"}}, base=OWL
    )
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
    owl_running = _running_threads("nightowl")
    owl_threads_at_start = len(owl_running)
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
                # The restarted station runs the same threads, each once.
                assert _without_liner_workers(_running_threads("nightowl")) == (
                    _without_liner_workers(owl_running)
                )
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
    assert client.get("/").status_code == 200
    assert client.get("/api/stations").status_code == 200
    assert not client.app.state.stations.default.stopped


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
        "/nightowl.m3u": 200,
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
    assert '<a href="/nightowl.mp3">' in html
    assert 'download="nightowl.m3u"' in html
    default_page = client.get("/status").text
    assert "<title>AutoDJ: station status</title>" in default_page
    assert 'data-feed="/api/nowplaying"' in default_page
    assert '<a href="/stream.mp3">' in default_page

    feed = client.get("/api/stations/nightowl/nowplaying")
    assert feed.json()["station_name"] == "Night Owl Radio"
    assert feed.json()["stream_path"] == "/nightowl.mp3"
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
    # Its player pages, which only this station turns on.
    player = client.get("/stations/nightowl/player")
    assert player.status_code == 200
    assert "<title>Night Owl Radio: listen live</title>" in player.text
    assert 'data-stream="/nightowl.mp3"' in player.text
    assert "frame-ancestors 'none'" in player.headers["content-security-policy"]
    embed = client.get("/stations/nightowl/embed")
    assert embed.headers["content-security-policy"].endswith(
        "frame-ancestors 'self' https://nightowl.example"
    )
    assert '<a href="/stations/nightowl/player" target="_blank"' in embed.text
    for text in (player.text, embed.text):
        assert TOKEN not in text
        assert tmp_path.as_posix() not in text and "testserver" not in text
    for path in ("/player", "/embed", "/stations/default/player"):
        assert client.get(path).status_code == 404, path
    assert client.get("/stations/daybreak/player").status_code == 401

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
    assert client.get("/nightowl.mp3").status_code == 503
    off_requests = client.get("/stations/nightowl/requests")
    assert off_requests.status_code == 503
    assert "Night Owl Radio is off air right now, so it is not taking requests." in (
        off_requests.text
    )
    off_player = client.get("/stations/nightowl/player")
    assert off_player.status_code == 503
    assert "Night Owl Radio is off air right now." in off_player.text
    assert 'id="player-toggle"' in off_player.text
    assert client.get("/stations/nightowl/embed").status_code == 503
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
    for key in ("status_path", "nowplaying_path", "stream_path", "player_path", "embed_path"):
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
    assert client.head("/nightowl.mp3").status_code == 401
    good = base64.b64encode(b"owl:night-owl-listener-secret").decode()
    authorised = client.head("/nightowl.mp3", headers={"Authorization": f"Basic {good}"})
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
    # Live DJs (#48): the DJ on the air and since when, or null.
    "live",
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


def test_default_can_restart_and_shared_routes_survive_zero_stations(rig: Any) -> None:
    client, library, _cfg, _song, _closes = rig
    registry = client.app.state.stations
    original = registry.default
    other = registry.get("nightowl")
    headers = {"Origin": "http://testserver"}
    assert client.post("/api/stations/default/stop", headers=headers).status_code == 200
    assert registry.default.stopped
    assert registry.get("nightowl") is other
    assert not other.stopped
    assert client.get("/api/status").status_code == 409
    assert client.get("/api/search", params={"q": "song"}).status_code == 200
    assert client.post("/api/stations/nightowl/stop", headers=headers).status_code == 200
    assert registry.all() == ()
    assert client.get("/").status_code == 200
    assert client.get("/api/stations").status_code == 200
    assert client.get("/api/library/folders").status_code == 200
    assert client.post("/api/stations/default/start", headers=headers).status_code == 200
    assert registry.default is not original
    assert registry.default.ledger is library.ledger
    assert client.get("/api/status").status_code == 200
