"""Settings, Stream: relays and station details through the API (#185)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from autodj.config import RelayConfig, StreamConfig
from autodj.relay_settings import RelayControl
from autodj.server import create_app

from .test_stream_server import _lifespan_bridge, _stream_mock

PASSWORD = "Relay-Pa55word"


def _relay(**extra) -> dict:
    return {
        "name": "main",
        "type": "icecast",
        "host": "relay.example.org",
        "port": 8000,
        "mount": "/live",
        **extra,
    }


class FakeManager:
    def __init__(self) -> None:
        self.applied: list[RelayConfig] = []
        self.removed: list[str] = []
        self.reconnects = 0

    def apply(self, cfg: RelayConfig) -> None:
        self.applied.append(cfg)

    def remove(self, name: str) -> None:
        self.removed.append(name)

    def reconnect(self) -> None:
        self.reconnects += 1

    def status(self) -> list[dict]:
        return [{"name": c.name, "state": "connecting", "error": None} for c in self.applied]


@pytest.fixture
def relay_app(bridge, tmp_path: Path):
    bridge.player._cfg.index.active_dir = tmp_path
    bridge.player._cfg.index.index_dir = tmp_path
    bridge.player._cfg.stream.enabled = True
    bridge.attach_stream(stream=_stream_mock(), station=MagicMock(state="idle"))
    bridge.save_persistent_state = MagicMock()
    app = create_app(bridge)
    station = app.state.stations.default
    control = RelayControl(
        slug="default",
        stream=bridge.player._cfg.stream,
        paths=station.paths,
        environ={},
        resolve=lambda host, port, **_: [(2, 1, 6, "", ("192.0.2.10", port))],
    )
    control.load()
    manager = FakeManager()
    control.attach(manager)  # type: ignore[arg-type]
    station.relay_control = control
    return TestClient(app, client=("192.0.2.4", 50000)), manager


def test_relays_need_stream_mode(bridge) -> None:
    client = TestClient(create_app(bridge), client=("192.0.2.4", 50000))
    assert client.get("/api/stream/relays").status_code == 404
    assert client.post("/api/stream/relays", json=_relay()).status_code == 404


def test_add_edit_turn_off_and_remove_a_relay(relay_app, tmp_path: Path, caplog) -> None:
    client, manager = relay_app
    with caplog.at_level(logging.DEBUG):
        added = client.post("/api/stream/relays", json={**_relay(), "password": PASSWORD})
        assert added.status_code == 200, added.text
        relay = added.json()["relay"]
        assert relay["password_set"] is True and relay["password_source"] == "saved"
        assert relay["state"] == "connecting"
        assert manager.applied[-1].password == PASSWORD
        # Editing without a password keeps the one saved.
        edited = client.post("/api/stream/relays/edit", json={**_relay(), "public": True})
        assert edited.status_code == 200 and edited.json()["relay"]["public"] is True
        assert manager.applied[-1].password == PASSWORD
        off = client.post("/api/stream/relays/enabled", json={"name": "main", "enabled": False})
        assert off.json()["relay"]["enabled"] is False
        listed = client.get("/api/stream/relays")
        assert listed.headers["cache-control"] == "no-store"
        assert [item["name"] for item in listed.json()["relays"]] == ["main"]
        for response in (added, edited, off, listed, client.get("/api/stream")):
            assert PASSWORD not in response.text
        removed = client.post("/api/stream/relays/remove", json={"name": "main"})
        assert removed.json() == {"result": "removed", "name": "main", "warning": None}
        assert manager.removed == ["main"]
    assert PASSWORD not in caplog.text
    assert PASSWORD not in (tmp_path / "relays.json").read_text(encoding="utf-8")


def test_a_refused_field_is_named(relay_app) -> None:
    client, manager = relay_app
    response = client.post("/api/stream/relays", json={**_relay(port=70000), "password": "x"})
    assert response.status_code == 422
    assert response.json()["field"] == "port"
    missing = client.post("/api/stream/relays", json=_relay())
    assert missing.status_code == 422 and missing.json()["field"] == "password"
    unknown = client.post("/api/stream/relays/edit", json={**_relay(name="nope")})
    assert unknown.status_code == 404
    metadata = client.post(
        "/api/stream/relays", json={**_relay(host="2852039166"), "password": "x"}
    )
    assert metadata.status_code == 422 and metadata.json()["field"] == "host"
    assert manager.applied == []


def test_relay_bodies_are_capped(relay_app) -> None:
    client, _manager = relay_app
    for path in ("/api/stream/relays", "/api/stream/relays/edit", "/api/stream/relays/remove"):
        response = client.post(path, json={**_relay(), "password": "x" * 20_000})
        assert response.status_code == 413, path


def test_relays_need_a_session_when_pairing_is_on(bridge, tmp_path: Path) -> None:
    from autodj.config import ServerConfig

    bridge.player._cfg.server = ServerConfig(
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
        access_token="t" * 32,
    )
    bridge.player._cfg.index.active_dir = tmp_path
    bridge.attach_stream(stream=_stream_mock(), station=MagicMock(state="idle"))
    client = TestClient(create_app(bridge), client=("192.0.2.4", 50000))
    assert client.get("/api/stream/relays").status_code == 401
    origin = {"Origin": "http://testserver"}
    assert client.post("/api/stream/relays", json=_relay(), headers=origin).status_code == 401
    # A page from elsewhere is refused before anything else.
    evil = {"Origin": "http://evil.example"}
    assert client.post("/api/stream/relays", json=_relay(), headers=evil).status_code == 403


def test_station_details_are_saved_sent_and_reconnect_the_relays(relay_app, bridge) -> None:
    client, manager = relay_app
    details = {
        "station_genre": " Jazz ",
        "station_description": "Late-night jazz",
        "station_url": "https://radio.example.org/",
    }
    response = client.post("/api/stream/settings", json=details)
    assert response.status_code == 200
    info = response.json()
    assert info["station_genre"] == "Jazz" and info["station_url"] == "https://radio.example.org/"
    assert manager.reconnects == 1
    bridge.save_persistent_state.assert_called()
    # The same details again change nothing, so the relays stay connected.
    client.post("/api/stream/settings", json=details)
    assert manager.reconnects == 1
    playback = bridge.get_settings()["playback"]
    assert playback["stream_station_genre"] == "Jazz"
    assert playback["stream_station_description"] == "Late-night jazz"
    with client.stream("GET", "/stream.mp3", headers={"Icy-MetaData": "1"}) as stream:
        assert stream.headers["icy-genre"] == "Jazz"
        assert stream.headers["icy-url"] == "https://radio.example.org/"
        assert stream.headers["icy-description"] == "Late-night jazz"


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"station_url": "javascript:alert(1)"}, "station_url"),
        ({"station_url": "https://user:pw@radio.example.org/"}, "station_url"),
        ({"station_genre": "Jazz\r\nX-Evil: 1"}, "station_genre"),
        ({"station_description": "x" * 256}, "station_description"),
    ],
)
def test_station_details_are_checked(relay_app, body: dict, field: str) -> None:
    client, manager = relay_app
    response = client.post("/api/stream/settings", json=body)
    assert response.status_code == 422
    assert response.json()["field"] == field
    assert manager.reconnects == 0


def test_a_relay_added_while_playing_leaves_the_stream_alone(tmp_path: Path) -> None:
    """The real manager: a new bitrate gets its own encoder, removed again with the relay."""
    import socket

    from tests.unit._fakes import FakeEncoder

    closed_port = socket.socket()
    closed_port.bind(("127.0.0.1", 0))
    port = closed_port.getsockname()[1]
    closed_port.close()  # nothing listens there: the relay keeps retrying
    bridge = _lifespan_bridge()
    bridge.player._cfg.index.index_dir = tmp_path
    bridge.player._cfg.index.active_dir = tmp_path
    bridge.player._cfg.stream = StreamConfig(enabled=True)
    with (
        patch("autodj.stream.FfmpegEncoder", FakeEncoder),
        TestClient(create_app(bridge, stream_mode=True), client=("192.0.2.4", 50000)) as client,
    ):
        bus = bridge.player.bus
        stream = bridge.stream
        encoder = stream._encoder
        # A direct listener, connected before the relay changes.
        listener = stream.add_listener(icy=False)
        assert bus.add_output.call_count == 1
        assert bridge.station._continuous() is False
        body = {
            **_relay(host="127.0.0.1", port=port, bitrate=128),
            "password": PASSWORD,
        }
        assert client.post("/api/stream/relays", json=body).status_code == 200
        assert bus.add_output.call_count == 2
        extra = bus.add_output.call_args.args[0]
        assert extra.bitrate == 128
        assert bridge.station._continuous() is True
        # The stream's own encoder never restarted.
        assert stream._encoder is encoder and not stream._closed
        state = client.get("/api/status").json()
        assert [relay["name"] for relay in state["stream_relays"]] == ["main"]
        off = client.post("/api/stream/relays/enabled", json={"name": "main", "enabled": False})
        assert off.status_code == 200
        bus.remove_output.assert_called_with(extra)
        assert extra._closed and bridge.station._continuous() is False
        assert stream._encoder is encoder and not stream._closed
        assert not listener.closed and stream.listener_count == 1
        saved = json.loads((tmp_path / "relays.json").read_text(encoding="utf-8"))
        assert saved["relays"]["main"]["enabled"] is False
