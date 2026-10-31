"""Live DJs (#48): the source mount on the wire, its checks, the DJ routes and talkover."""

from __future__ import annotations

import asyncio
import base64
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from autodj.catalog import LibraryCatalog
from autodj.config import ServerConfig
from autodj.listener_access import ListenerAuthLimiter
from autodj.live import LiveDesk, PcmBuffer
from autodj.live_protocol import IcecastSourceH11Protocol
from autodj.live_routes import SourceEndpoint
from autodj.playlog import plays_between, plays_csv, soundexchange_report
from tests._layout import point_index
from tests._server import create_app
from tests.integration._helpers import patch_station_settings

PASSWORD = "correct horse battery"
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def _basic(username: str = "alice", password: str = PASSWORD) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


def _encoded(seconds: float, codec: str = "libmp3lame", container: str = "mp3") -> bytes:
    """A 440 Hz tone, encoded as a source client would send it."""
    return subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            str(seconds),
            "-c:a",
            codec,
            "-b:a",
            "128k",
            "-f",
            container,
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=20,
    ).stdout


def _wait(condition: Callable[[], object], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def _app(tmp_path, bridge, *, stream: bool = True, **server: object) -> TestClient:
    point_index(bridge.player._cfg.index, tmp_path)
    bridge.player._cfg.index.index_dir = tmp_path
    if server:
        bridge.player._cfg.server = ServerConfig(
            allowed_hosts=["testserver"], allowed_origins=["http://testserver"], **server
        )
    if stream:
        bridge.player.bus = MagicMock()
        bridge.attach_stream(stream=MagicMock(listener_count=0), station=MagicMock(state="playing"))
    return TestClient(create_app(bridge, stream_mode=stream), client=("127.0.0.1", 1234))


class _FakeSource:
    """A connected source, as far as the routes and the desk can tell."""

    def __init__(self, username: str = "alice", name: str = "DJ Alice") -> None:
        self.username, self.name = username, name
        self.started = True
        self.buffer = PcmBuffer(2.0)
        self.closed = False

    def close(self) -> None:
        self.closed = True


# --- the decoder ------------------------------------------------------------------


@needs_ffmpeg
@pytest.mark.parametrize(
    ("mime", "container", "codec"),
    [
        ("audio/mpeg", "mp3", "libmp3lame"),
        ("audio/aac", "adts", "aac"),
        ("audio/ogg", "ogg", "libvorbis"),
    ],
)
def test_a_source_is_decoded_goes_on_the_air_and_hands_back(
    tmp_path, mime: str, container: str, codec: str
) -> None:
    """ADR-0004-05."""
    audio = _encoded(1.0, codec, container)
    events: list[str] = []
    desk = LiveDesk(
        tmp_path / "stations" / "default" / "live_djs.json",
        on_start=lambda: events.append(f"start:{desk.status()['dj']}"),
        before_return=lambda source: events.append(f"ready:{source.buffer.frames > 0}"),
        on_end=lambda: events.append("end"),
    )
    desk.credentials.save("alice", "DJ Alice", PASSWORD)
    station = SimpleNamespace(
        slug="default",
        stream_mode=True,
        player=SimpleNamespace(bus=object()),
        live=desk,
        stopped=False,
    )
    app = SimpleNamespace(state=SimpleNamespace(stations=SimpleNamespace(default=station)))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "SOURCE",
        "path": "/live",
        "root_path": "",
        "query_string": b"",
        "scheme": "http",
        "http_version": "1.0",
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8000),
        "app": app,
        "path_params": {},
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", mime.encode()),
            (b"authorization", _basic().encode()),
        ],
    }
    sent = []
    chunks = iter(audio[i : i + 2048] for i in range(0, len(audio), 2048))

    async def receive():
        try:
            chunk = next(chunks)
        except StopIteration:
            await asyncio.sleep(0.3)
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(0.005)
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message):
        sent.append(message)

    asyncio.run(SourceEndpoint()(scope, receive, send))
    assert sent[0]["status"] == 200
    assert (b"connection", b"close") in sent[0]["headers"]
    _wait(lambda: "end" in events)
    assert events[0] == "start:DJ Alice"
    assert events[1:] == ["ready:True", "end"] or events[1:] == ["ready:False", "end"]
    assert desk.source is None and desk.status() is None


# --- the source mount on the wire --------------------------------------------------


@contextmanager
def _serving(app) -> Iterator[tuple[str, int]]:
    """The app behind a real uvicorn, with the server's own source framing."""
    import uvicorn

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        config = uvicorn.Config(
            app, http=IcecastSourceH11Protocol, lifespan="off", log_level="error", access_log=False
        )
        server = uvicorn.Server(config)
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            _wait(lambda: server.started or not worker.is_alive(), 5)
            assert server.started
            yield listener.getsockname()
        finally:
            server.should_exit = True
            worker.join(timeout=10)


def _read_head(connection: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        more = connection.recv(4096)
        if not more:
            break
        data += more
    return data


def _read_to_close(connection: socket.socket) -> bytes:
    data = b""
    try:
        while more := connection.recv(4096):
            data += more
    except (ConnectionResetError, ConnectionAbortedError):
        pass
    return data


@needs_ffmpeg
@pytest.mark.parametrize(
    "head",
    [
        # BUTT and libshout (Mixxx): PUT, no length, asking to be told to go on.
        "PUT /live HTTP/1.1\r\nHost: testserver\r\nAuthorization: {auth}\r\n"
        "Content-Type: audio/mpeg\r\nIce-Name: Breakfast\r\nExpect: 100-continue\r\n\r\n",
        # Older clients: SOURCE over HTTP/1.0 with no Host at all.
        "SOURCE /live HTTP/1.0\r\nAuthorization: {auth}\r\nContent-Type: audio/mpeg\r\n\r\n",
    ],
)
def test_a_real_source_client_streams_until_it_hangs_up(tmp_path, bridge, head: str) -> None:
    client = _app(tmp_path, bridge)
    station = client.app.state.stations.default
    station.live.credentials.save("alice", "DJ Alice", PASSWORD)
    audio = _encoded(4.0)
    with _serving(client.app) as address, socket.create_connection(address, timeout=10) as conn:
        conn.sendall(head.format(auth=_basic()).encode())
        answer = _read_head(conn)
        if "Expect" in head:
            assert answer.startswith(b"HTTP/1.1 100 Continue\r\n")
        else:
            assert answer.startswith(b"HTTP/1.1 200 OK\r\n")
            assert b"connection: close" in answer.lower()
        for offset in range(0, len(audio), 4000):
            conn.sendall(audio[offset : offset + 4000])
            time.sleep(0.12)  # a little faster than real time, as a client catching up
            if station.live.status() is not None:
                break
        _wait(lambda: station.live.status() is not None)
        assert station.live.status()["dj"] == "DJ Alice"
        assert client.get("/api/status").json()["live"]["dj"] == "DJ Alice"
        history = client.get("/api/history").json()["items"]
        assert history[0]["kind"] == "live" and history[0]["artist"] == "DJ Alice"
        conn.shutdown(socket.SHUT_WR)  # the DJ stops BUTT
        _wait(lambda: station.live.source is None)
    assert station.live.status() is None
    row = client.get("/api/history").json()["items"][0]
    assert row["kind"] == "live" and row["ended_at"] is not None
    station.player.bus.drop_held_track.assert_called_once()


def test_a_refused_source_is_answered_once_and_disconnected(tmp_path, bridge) -> None:
    client = _app(tmp_path, bridge)
    client.app.state.stations.default.live.credentials.save("alice", "DJ Alice", PASSWORD)
    with _serving(client.app) as address, socket.create_connection(address, timeout=10) as conn:
        conn.sendall(
            f"PUT /live HTTP/1.1\r\nHost: testserver\r\nAuthorization: {_basic(password='nope')}"
            "\r\nContent-Type: audio/mpeg\r\n\r\n".encode()
            # What follows is the body: never a second request.
            + b"GET /api/status HTTP/1.1\r\nHost: testserver\r\n\r\n"
        )
        reply = _read_to_close(conn)
    assert reply.startswith(b"HTTP/1.1 401 ")
    assert reply.count(b"HTTP/1.1") == 1
    assert b'basic realm="autodj live"' in reply.lower()


def test_a_request_that_names_its_length_is_framed_by_h11_as_usual(tmp_path, bridge) -> None:
    client = _app(tmp_path, bridge)
    with _serving(client.app) as address, socket.create_connection(address, timeout=10) as conn:
        conn.sendall(
            b"GET /api/version HTTP/1.1\r\nHost: testserver\r\n\r\n"
            b"GET /api/version HTTP/1.1\r\nHost: testserver\r\nConnection: close\r\n\r\n"
        )
        reply = _read_to_close(conn)
    assert reply.count(b"HTTP/1.1 200 OK") == 2


# --- the source mount's checks ------------------------------------------------------


def _source(client: TestClient, path: str = "/live", **headers: str):
    return client.request(
        "SOURCE", path, headers={"Content-Type": "audio/mpeg", **headers}, content=b""
    )


def test_source_logins_are_checked_and_limited(tmp_path, bridge) -> None:
    client = _app(tmp_path, bridge)
    # Keep the failed-login sequence inside one limiter window.
    source_endpoint = next(route.endpoint for route in client.app.routes if route.path == "/live")
    source_endpoint.limiter = ListenerAuthLimiter(clock=lambda: 120.0)
    live = client.app.state.stations.default.live
    live.credentials.save("alice", "DJ Alice", PASSWORD)
    assert _source(client).status_code == 401
    assert _source(client, Authorization=_basic(password="wrong password")).status_code == 401
    assert _source(client, "/live", **{"Content-Type": "text/plain"}).status_code == 415
    live.source = _FakeSource()
    try:
        assert _source(client, Authorization=_basic()).status_code == 409
    finally:
        live.source = None
    for _ in range(3):
        assert _source(client, Authorization=_basic(password="wrong again")).status_code == 401
    refused = _source(client, Authorization=_basic())
    assert refused.status_code == 429 and refused.headers["Retry-After"] == "60"
    assert refused.headers["Connection"] == "close"
    # A DJ's password opens only the source mount, not the operator's routes.
    assert client.get("/api/live", headers={"Authorization": _basic()}).status_code == 200


@pytest.mark.parametrize(
    ("allow_lan", "peer", "headers", "status"),
    [
        (False, "192.168.1.20", {}, 403),
        (True, "192.168.1.20", {}, 401),
        (True, "fe80::1", {}, 401),
        (True, "8.8.8.8", {}, 403),
        (True, "100.64.0.9", {}, 403),
        (True, "192.168.1.20", {"X-Forwarded-For": "203.0.113.5"}, 403),
        (False, "127.0.0.1", {"X-Forwarded-Proto": "http"}, 403),
        (False, "127.0.0.1", {}, 401),
    ],
)
def test_plain_http_sources_come_from_this_machine_or_an_allowed_lan(
    tmp_path, bridge, allow_lan: bool, peer: str, headers: dict[str, str], status: int
) -> None:
    client = _app(tmp_path, bridge, live_plain_http_lan=allow_lan)
    remote = TestClient(client.app, client=(peer, 1234))
    assert _source(remote, **headers).status_code == status


def test_https_sources_may_come_from_anywhere(tmp_path, bridge) -> None:
    client = _app(tmp_path, bridge)
    remote = TestClient(client.app, base_url="https://testserver", client=("203.0.113.5", 1))
    assert _source(remote).status_code == 401  # past the transport check, to the password


def test_a_station_that_does_not_stream_takes_no_dj(tmp_path, bridge) -> None:
    client = _app(tmp_path, bridge, stream=False)
    assert _source(client, Authorization=_basic()).status_code == 503
    view = client.get("/api/live").json()
    assert view["available"] is False and view["reason"]


def test_the_source_mount_still_checks_a_host_it_is_given(tmp_path, bridge) -> None:
    client = _app(tmp_path, bridge)
    assert _source(client, Host="evil.example").status_code == 403


# --- the operator's routes ----------------------------------------------------------


def test_the_operator_manages_djs_and_talkover_without_seeing_passwords(tmp_path, bridge) -> None:
    client = _app(tmp_path, bridge)
    view = client.get("/api/live").json()
    assert view == {
        "available": True,
        "reason": None,
        "problem": None,
        "live": None,
        "talkover": False,
        "source_path": "/live",
        "plain_http_lan": False,
        "settings": {"mic_return_seconds": 0.5, "mic_duck_db": 12.0},
        "djs": [],
    }
    body = {"username": "alice", "password": PASSWORD, "display_name": "DJ Alice"}
    saved = client.post("/api/live/djs", json=body)
    assert saved.json() == {"username": "alice", "display_name": "DJ Alice", "password_set": True}
    assert PASSWORD not in (tmp_path / "stations" / "default" / "live_djs.json").read_text()
    renamed = client.post(
        "/api/live/djs", json={"username": "alice", "display_name": "Alice", "password": None}
    )
    assert renamed.status_code == 200
    assert client.get("/api/live").json()["djs"] == [
        {"username": "alice", "display_name": "Alice", "password_set": True}
    ]
    refused = client.post("/api/live/djs", json={"username": "bob", "display_name": "Bob"})
    assert refused.status_code == 400 and "Password" in refused.json()["detail"]
    assert client.post("/api/live/djs", json=[1]).status_code == 400
    assert (
        client.post(
            "/api/live/djs", json=body, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    settings = patch_station_settings(client, {"live": {"mic_return_seconds": 2, "mic_duck_db": 6}})
    assert settings.json()["settings"]["values"]["live"] == {
        "mic_return_seconds": 2.0,
        "mic_duck_db": 6.0,
    }
    assert client.app.state.stations.default.live.settings.mic_return_seconds == 2.0
    bad = patch_station_settings(client, {"live": {"mic_return_seconds": 0, "mic_duck_db": 6}})
    assert bad.status_code == 400 and "mic_return_seconds" in bad.json()["detail"]
    # Removing a DJ who is on the air takes them off.
    live = client.app.state.stations.default.live
    source = _FakeSource("alice", "Alice")
    live.source = source
    assert client.delete("/api/live/djs/alice").json() == {"ok": True}
    assert source.closed and live.source is None
    assert client.delete("/api/live/djs/alice").status_code == 404


def test_now_playing_and_status_name_the_live_dj(tmp_path, bridge) -> None:
    client = _app(tmp_path, bridge)
    station = client.app.state.stations.default
    source = _FakeSource()
    station.live.source = source
    station.live.since = datetime.now(UTC).isoformat()
    station.public_state_changed()
    feed = client.get("/api/nowplaying").json()
    assert feed["live"]["dj"] == "DJ Alice"
    assert feed["current_track"]["artist"] == "DJ Alice"
    assert feed["current_track"]["kind"] == "live"
    assert feed["next_track"] is None
    state = client.get("/api/status").json()
    assert state["live"] == {"dj": "DJ Alice", "since": station.live.since}
    assert state["talkover"] is False


def test_liners_and_carts_say_they_wait_for_the_dj(tmp_path, bridge) -> None:
    client = _app(tmp_path, bridge)
    station = client.app.state.stations.default
    bridge.liner_scheduler = MagicMock()
    station.live.source = _FakeSource()
    station.live.since = datetime.now(UTC).isoformat()
    reply = client.post("/api/liners/test", json={"name": "a.wav"}).json()
    assert reply == {
        "played": None,
        "message": "A live DJ is on the air, so the liner was not played.",
    }
    bridge.liner_scheduler.fire.assert_not_called()
    station.player.bus.live_on_air = True
    assert station.carts.not_on_air() == "A live DJ is on the air, so the cart was not played"


# --- talkover -----------------------------------------------------------------------


def test_one_browser_talks_at_a_time_with_int16_pcm(tmp_path, bridge) -> None:
    client = _app(tmp_path, bridge)
    live = client.app.state.stations.default.live
    voice = (np.full(4800, 0.25) * 32767).astype("<i2").tobytes()  # 0.1 s at 48 kHz
    with client.websocket_connect("/api/live/mic?rate=48000") as talker:
        talker.send_bytes(voice)
        _wait(lambda: live.mic is not None and live.mic.frames)
        assert abs(live.mic.frames - 4410) <= 2
        assert client.get("/api/status").json()["talkover"] is True
        with (
            pytest.raises(WebSocketDisconnect) as busy,
            client.websocket_connect("/api/live/mic?rate=48000") as second,
        ):
            second.receive_bytes()
        assert busy.value.code == 4409
    _wait(lambda: live.mic is None)
    with client.websocket_connect("/api/live/mic?rate=48000") as talker:
        talker.send_text("hello")
        with pytest.raises(WebSocketDisconnect) as bad:
            talker.receive_bytes()
        assert bad.value.code == 1003
    for path, headers, code in (
        ("/api/live/mic", {}, 1003),
        ("/api/live/mic?rate=1000", {}, 1003),
        ("/api/live/mic?rate=48000", {"Origin": "https://evil.example"}, 4403),
        ("/api/stations/nope/live/mic?rate=48000", {}, 4404),
    ):
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect(path, headers=headers) as socket_,
        ):
            socket_.receive_bytes()
        assert refused.value.code == code, path


def test_talkover_needs_server_playout(tmp_path, bridge) -> None:
    client = _app(tmp_path, bridge, stream=False)
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect("/api/live/mic?rate=48000") as talker,
    ):
        talker.receive_bytes()
    assert refused.value.code == 4409


# --- the play log ---------------------------------------------------------------------


def test_a_live_session_is_exported_but_not_reported_to_soundexchange(tmp_path) -> None:
    catalog = LibraryCatalog(tmp_path / "library.db")
    try:
        started = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
        ended = started + timedelta(minutes=4)
        row = {
            "event_id": "live-session",
            "path": None,
            "title": "Live",
            "artist": "DJ Alice",
            "duration": 0.0,
            "played_at": started.isoformat(),
            "ended_at": None,
            "performances": None,
            "kind": "live",
        }
        catalog.write_history([row])
        catalog.write_history([{**row, "duration": 240.0, "ended_at": ended.isoformat()}])
        plays = plays_between(
            catalog,
            started - timedelta(seconds=1),
            ended + timedelta(seconds=1),
            read_tags=lambda _: ("", ""),
        )
        assert len(plays) == 1
        assert plays[0].kind == "live"
        assert plays[0].end == ended
        assert "DJ Alice" in plays_csv(plays)
        assert "DJ Alice" not in soundexchange_report(plays, "AutoDJ", "A")
        assert catalog.recent_plays(10) == []
        (kept,) = catalog.recent_history()
        assert kept["kind"] == "live" and kept["ended_at"] == ended.isoformat()
    finally:
        catalog.close()


# --- review follow-ups ------------------------------------------------------------


def test_an_escaped_mount_gets_no_exemption_and_is_not_framed(tmp_path, bridge) -> None:
    """``/%6cive`` decodes to ``/live`` but is not the mount: the usual checks apply."""
    client = _app(tmp_path, bridge)
    client.app.state.stations.default.live.credentials.save("alice", "DJ Alice", PASSWORD)
    with _serving(client.app) as address, socket.create_connection(address, timeout=10) as conn:
        conn.sendall(
            f"PUT /%6cive HTTP/1.1\r\nHost: testserver\r\nAuthorization: {_basic()}\r\n"
            "Content-Type: audio/mpeg\r\n\r\n".encode()
        )
        reply = _read_to_close(conn)
    assert reply.startswith(b"HTTP/1.1 403 ")  # no Origin: refused as any other PUT
    assert b"connection: close" in reply.lower()
    assert client.app.state.stations.default.live.source is None


def test_a_refused_host_on_a_mount_closes_the_connection(tmp_path, bridge) -> None:
    client = _app(tmp_path, bridge)
    with _serving(client.app) as address, socket.create_connection(address, timeout=10) as conn:
        conn.sendall(
            b"SOURCE /live HTTP/1.0\r\nHost: evil.example\r\nContent-Type: audio/mpeg\r\n\r\n"
            + b"\xff\xfb" * 2000
        )
        reply = _read_to_close(conn)
    assert reply.startswith(b"HTTP/1.1 403 ")
    assert reply.count(b"HTTP/1.1") == 1 and b"connection: close" in reply.lower()


def test_a_trickled_first_head_is_cut_off(tmp_path, bridge, monkeypatch) -> None:
    monkeypatch.setattr("autodj.live_protocol.HEAD_DEADLINE_SECONDS", 0.3)
    client = _app(tmp_path, bridge)
    with _serving(client.app) as address, socket.create_connection(address, timeout=10) as conn:
        conn.sendall(b"PUT /live HTTP/1.1\r\nHost: testse")
        started = time.monotonic()
        assert _read_to_close(conn) == b""
        assert time.monotonic() - started < 5
    with _serving(client.app) as address, socket.create_connection(address, timeout=10) as conn:
        conn.sendall(b"GET /api/vers")  # an ordinary request trickled in too
        assert _read_to_close(conn) == b""
    # A complete request is not cut off, nor a WebSocket after its upgrade.
    with _serving(client.app) as address, socket.create_connection(address, timeout=10) as conn:
        conn.sendall(b"GET /api/version HTTP/1.1\r\nHost: testserver\r\n\r\n")
        assert _read_head(conn).startswith(b"HTTP/1.1 200 OK")


def test_logins_waiting_across_addresses_are_capped(tmp_path, bridge, monkeypatch) -> None:
    from autodj import live_routes

    monkeypatch.setattr(live_routes, "_AUTH_WAITING", 0)
    client = _app(tmp_path, bridge)
    refused = _source(client, Authorization=_basic())
    assert refused.status_code == 503 and refused.headers["Retry-After"] == "5"


def test_the_login_limit_keys_on_the_client_a_trusted_proxy_names(tmp_path, bridge) -> None:
    """The same trust as listener requests: uvicorn's proxy headers, [stream] trusted_proxies."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    client = _app(tmp_path, bridge)
    behind = TestClient(
        ProxyHeadersMiddleware(client.app, trusted_hosts=["127.0.0.1"]), client=("127.0.0.1", 1)
    )

    def login(address: str, password: str = "wrong password") -> int:
        headers = {"X-Forwarded-For": address, "X-Forwarded-Proto": "https"}
        return _source(behind, Authorization=_basic(password=password), **headers).status_code

    for _ in range(5):
        assert login("203.0.113.7") == 401
    assert login("203.0.113.7") == 429
    assert login("203.0.113.8") == 401  # another listener behind the same proxy
    # An untrusted peer's header is ignored: it is limited by its own address.
    stranger = TestClient(
        ProxyHeadersMiddleware(client.app, trusted_hosts=["127.0.0.1"]),
        base_url="https://testserver",
        client=("198.51.100.9", 1),
    )
    for _ in range(5):
        assert _source(stranger, **{"X-Forwarded-For": "203.0.113.9"}).status_code == 401
    assert _source(stranger, **{"X-Forwarded-For": "203.0.113.10"}).status_code == 429


def test_the_song_a_dj_cuts_off_is_closed_and_their_set_only_is_updated(tmp_path) -> None:
    catalog = LibraryCatalog(tmp_path / "library.db")
    try:
        start = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
        song = {
            "event_id": "song",
            "path": "/music/a.flac",
            "title": "A",
            "artist": "B",
            "duration": 200.0,
            "played_at": start.isoformat(),
            "performances": 3,
        }
        catalog.write_history([song])
        cut = (start + timedelta(seconds=60)).isoformat()
        catalog.write_history([{**song, "duration": 5.0, "ended_at": cut, "performances": None}])
        catalog.write_history([{**song, "ended_at": "2030-01-01T00:00:00+00:00"}])
        (row,) = catalog.recent_history()
        assert row["duration"] == 200.0  # a song's length never changes
        assert row["ended_at"] == cut  # and its end is set once
        plays = plays_between(
            catalog, start, start + timedelta(minutes=5), read_tags=lambda _: ("", "")
        )
        assert plays[0].performances == 3
    finally:
        catalog.close()


def test_a_dj_going_live_closes_the_interrupted_song_and_misses_breaks(tmp_path, bridge) -> None:
    client = _app(tmp_path, bridge)
    station = client.app.state.stations.default
    interrupted = {
        "event_id": "e1",
        "path": "/a.flac",
        "title": "A",
        "artist": "B",
        "duration": 200.0,
        "played_at": "2026-10-13T12:00:00+00:00",
        "performances": 1,
    }
    bridge._play_history.append(interrupted)
    bridge._open_play = (object(), interrupted)
    station.player.break_desk = MagicMock()
    station.player.break_desk.drop_unaired.return_value = 1
    station.live.source = _FakeSource()
    station.live.since = "2026-10-13T12:01:00+00:00"
    station._live_started()
    assert interrupted["ended_at"] == "2026-10-13T12:01:00+00:00"
    assert bridge._open_play is None
    station.player.break_desk.drop_unaired.assert_called_once_with("live DJ on air")


def test_the_connection_limit_leaves_room_for_every_stations_listeners() -> None:
    from autodj.config import StreamConfig
    from autodj.server import _CONNECTION_ROOM, _connection_limit

    cfg = SimpleNamespace(
        stream=StreamConfig(max_listeners=20),
        stations=[SimpleNamespace(cfg=SimpleNamespace(stream=StreamConfig(max_listeners=50)))],
    )
    assert _connection_limit(cfg) == _CONNECTION_ROOM + 70


def test_takeover_drops_future_voice_and_return_does_not_replay_it(tmp_path, bridge, monkeypatch):
    """ADR-0004-26: takeover invalidates future bindings before automation returns."""
    from tests.unit.test_live_voice import _book_future, _promote_pair

    client = _app(tmp_path, bridge)
    station = client.app.state.stations.default
    folder = tmp_path / "voice-station"
    folder.mkdir()
    player, entries, desk, item = _book_future(folder)
    bridge.player = player
    station.live.source = _FakeSource()
    station.live.since = "2026-10-30T12:00:00+00:00"
    old_run = player._voice_run_id
    station._live_started()
    assert player._voice_run_id != old_run
    assert not player._voice_targets
    assert desk.get(item.break_id).state == "dropped"
    assert desk.get(item.break_id).reason == "live DJ on air"
    monkeypatch.setattr(player, "prepare_live_return", lambda timeout: None)
    station._ready_automation(SimpleNamespace(buffer=SimpleNamespace(frames=0)))
    _promote_pair(player, entries)
    player._maintain_live_handoff(None)
    assert not player._playing_render.break_events
