"""Local input admission on ASGI and real TCP sockets, without model startup."""

import socket
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.sync.client import connect

from autodj.config import ServerConfig
from autodj.live import LiveDesk
from autodj.local_programme import LocalProgramme
from autodj.local_programme_routes import RawPeerMiddleware, install_local_programme_routes
from autodj.security import SecurityPolicy

BASE = "http://127.0.0.1:8080"
PATH = "/api/local-programme/ws"
WS = "ws://127.0.0.1:8080"


@contextmanager
def disconnected(code):
    with pytest.raises(WebSocketDisconnect) as exc:
        yield
    assert exc.value.code == code


def fixture_app(tmp_path, **settings):
    app = FastAPI()
    policy = SecurityPolicy(ServerConfig(local_programme_enabled=True, **settings))
    stations = {}
    for slug in ("default", "second"):
        live = LiveDesk(tmp_path / slug / "djs.json")
        stations[slug] = SimpleNamespace(
            stopped=False,
            stream_mode=True,
            player=SimpleNamespace(bus=object()),
            live=live,
            local_programme=LocalProgramme(live),
        )
    app.state.stations = SimpleNamespace(default=stations["default"], get=stations.__getitem__)
    install_local_programme_routes(app, policy)
    return app, policy, stations


def client_for(app):
    return TestClient(
        RawPeerMiddleware(app),
        base_url=BASE,
        client=("127.0.0.1", 123),
        headers={"Host": "127.0.0.1:8080", "Origin": BASE},
    )


def claim(ws):
    ws.send_json({"type": "claim", "version": 1, "rate": 44100})
    return ws.receive_json()


def control(grant, kind):
    return {
        "type": kind,
        "version": 1,
        **{key: grant[key] for key in ("epoch", "generation", "capability")},
    }


def test_administrative_return_rejects_an_old_observed_generation(tmp_path):
    """ADR-0019-01: a delayed Return cannot revoke a replacement owner."""
    app, _, stations = fixture_app(tmp_path)
    controller = stations["default"].local_programme
    first = controller.claim("user", "first", time.monotonic())
    controller.revoke(first.generation, "returned")
    second = controller.claim("user", "second", time.monotonic())
    client = client_for(app)
    response = client.post(
        "/api/local-programme/return",
        json={
            "epoch": first.epoch,
            "generation": first.generation,
        },
    )
    assert response.status_code == 409
    controller.validate(second, time.monotonic())


@pytest.mark.parametrize(
    "path",
    ["/api/recording/stop", "/api/recording/split"],
)
def test_local_ownership_preserves_private_review_and_recording_controls(path):
    """ADR-0019-04: private review and recording do not change programme ownership."""
    from starlette.requests import Request

    from autodj.station_routes import _fence_local_programming

    station = SimpleNamespace(
        local_programme=SimpleNamespace(performance=SimpleNamespace(busy=True))
    )
    _fence_local_programming(
        Request({"type": "http", "method": "POST", "path": path, "headers": [], "scheme": "http"}),
        station,
    )


def test_competing_tabs_station_isolation_release_and_stale_owner(tmp_path):
    """ADR-0019-16: socket identity wins over shared cookie; stations stay separate."""
    app, _, stations = fixture_app(tmp_path)
    client = client_for(app)
    with client.websocket_connect(WS + PATH, headers={"Origin": BASE}) as one:
        first = claim(one)
        assert first["state"] == "preparing"
        with (
            client.websocket_connect(WS + PATH, headers={"Origin": BASE}) as two,
            disconnected(4409),
        ):
            claim(two)
        with client.websocket_connect(
            WS + "/api/stations/second/local-programme/ws", headers={"Origin": BASE}
        ) as other:
            assert claim(other)["type"] == "granted"
        one.send_json(control(first, "release"))
        with pytest.raises(WebSocketDisconnect):
            one.receive_json()
    with client.websocket_connect(WS + PATH, headers={"Origin": BASE}) as new:
        second = claim(new)
        assert second["generation"] > first["generation"]
        new.send_json(control(first, "heartbeat"))
        with disconnected(4409):
            new.receive_json()
    assert stations["default"].local_programme.status()["state"] == "automation"


def test_revocation_without_incoming_messages_and_stop(tmp_path):
    """ADR-0019-16: a silent socket cannot keep a revoked session or stopped station."""
    app, policy, stations = fixture_app(tmp_path)
    client = client_for(app)
    with client.websocket_connect(WS + PATH, headers={"Origin": BASE}) as ws:
        claim(ws)
        policy.socket_session_is_valid = lambda *_: False
        with disconnected(4401):
            ws.receive_json()
    assert stations["default"].live.claim_mic() is not None


def test_revocation_while_receive_waits_cannot_claim(tmp_path, monkeypatch):
    """ADR-0019-16: revalidate authorization after receive, before any mutation."""
    from fastapi import WebSocket

    app, policy, stations = fixture_app(tmp_path)
    valid = True
    original = WebSocket.receive

    async def revoke_during_receive(ws):
        nonlocal valid
        message = await original(ws)
        if message["type"] == "websocket.receive":
            valid = False
        return message

    monkeypatch.setattr(WebSocket, "receive", revoke_during_receive)
    policy.socket_session_is_valid = lambda *_: valid
    with client_for(app).websocket_connect(WS + PATH, headers={"Origin": BASE}) as ws:
        ws.send_json({"type": "claim", "version": 1, "rate": 44100})
        with disconnected(4401):
            ws.receive_json()
    assert stations["default"].local_programme.status()["generation"] == 0


@pytest.mark.parametrize(
    "header,value",
    [
        ("Origin", "http://127.0.0.1:8081"),
        ("Host", "attacker.invalid"),
        ("X-Forwarded-For", "127.0.0.1"),
        ("Forwarded", "for=127.0.0.1"),
    ],
)
def test_socket_denies_forged_locality(tmp_path, header, value):
    """ADR-0019-02: forwarded loopback claims and cross-origin handshakes fail."""
    app, _, _ = fixture_app(tmp_path)
    with (
        disconnected(4403),
        client_for(app).websocket_connect(WS + PATH, headers={"Origin": BASE, header: value}),
    ):
        pass


def test_disabled_has_no_routes_and_missing_provenance_fails_closed(tmp_path):
    """ADR-0019-12: opt-out installs no routes; unsupported ASGI deployments fail closed."""
    app = FastAPI()
    before = list(app.routes)
    install_local_programme_routes(app, SecurityPolicy(ServerConfig()))
    assert app.routes == before
    app, _, _ = fixture_app(tmp_path)
    with (
        disconnected(4403),
        TestClient(app, base_url=BASE).websocket_connect(WS + PATH, headers={"Origin": BASE}),
    ):
        pass


def test_authenticated_admission_and_public_activation_unavailable(tmp_path):
    app, _, _ = fixture_app(tmp_path, access_token="a" * 32)
    with disconnected(4401), client_for(app).websocket_connect(WS + PATH, headers={"Origin": BASE}):
        pass
    app, _, _ = fixture_app(tmp_path)
    with client_for(app).websocket_connect(WS + PATH, headers={"Origin": BASE}) as ws:
        grant = claim(ws)
        ws.send_json(control(grant, "activate"))
        with disconnected(4409):
            ws.receive_json()


def test_preparation_pcm_credit_stall_and_malformed_release(tmp_path):
    """ADR-0019-03: zero PCM earns credit; malformed and stalled sockets release reservations."""
    from tests.unit.test_local_programme_audio import packet

    app, _, stations = fixture_app(tmp_path)
    with client_for(app).websocket_connect(WS + PATH, headers={"Origin": BASE}) as ws:
        grant = claim(ws)
        ws.send_bytes(packet(generation=grant["generation"], values=(0, 0)))
        assert ws.receive_json() == {"type": "credit", "limit_frame": 9261}
        with disconnected(4408):
            ws.receive_json()
    assert stations["default"].local_programme.status()["state"] == "automation"
    with client_for(app).websocket_connect(WS + PATH, headers={"Origin": BASE}) as ws:
        grant = claim(ws)
        ws.send_bytes(packet(generation=grant["generation"], rate=48000))
        with disconnected(1003):
            ws.receive_json()
    assert stations["default"].live.claim_mic() is not None


def test_control_protocol_unknown_fields_and_version_are_refused(tmp_path):
    app, _, _ = fixture_app(tmp_path)
    for data in (
        {"type": "claim", "rate": 44100},
        {"type": "claim", "version": 1, "rate": 44100, "extra": True},
    ):
        with client_for(app).websocket_connect(WS + PATH, headers={"Origin": BASE}) as ws:
            ws.send_json(data)
            with disconnected(1003):
                ws.receive_json()


def test_consumption_credit_arrives_without_another_packet(tmp_path):
    """ADR-0019-03: draining an internally armed input grants credit without inbound traffic."""
    from tests.unit.test_local_programme_audio import packet

    app, _, stations = fixture_app(tmp_path)
    with client_for(app).websocket_connect(WS + PATH, headers={"Origin": BASE}) as ws:
        grant = claim(ws)
        audio = stations["default"].local_programme.audio
        audio.arm()  # Fixture-only seam; the public protocol has no activate operation.
        for sequence in range(6):
            ws.send_bytes(packet(sequence, generation=grant["generation"]))
            assert ws.receive_json() == {"type": "credit", "limit_frame": 8820}
            time.sleep(0.01)
        audio.read_block(grant["generation"])
        assert ws.receive_json() == {"type": "credit", "limit_frame": 9702}


async def test_internal_selector_keeps_master_encoder_and_http_connection(tmp_path):
    """ADR-0019-13: real encoder and one HTTP response survive internal input selection.

    Synthetic Python PCM isolates the new receiver/bus seam. This is not evidence
    of browser feed delay, the future queue handoff or third-party mastering.
    """
    import asyncio
    import subprocess

    import httpx2 as httpx
    import numpy as np
    from fastapi.responses import StreamingResponse

    from autodj.local_programme_audio import LocalPCM
    from autodj.stream import FfmpegEncoder, StreamOutput
    from tests.playwright.local_programme_measure import measure, signal
    from tests.unit.test_local_programme_audio import GRANT, HEADER
    from tests.unit.test_mixbus import _bus, _track

    reference = signal(44100, 3)
    audio = LocalPCM(GRANT, now=0)
    audio.arm()
    audio.clock = lambda: audio.next_sequence * 0.01

    def feed():
        seq = audio.next_sequence
        samples = reference[seq * 441 : (seq + 1) * 441]
        if len(samples) == 441:
            payload = (
                HEADER.pack(b"ADJP", 1, 0, 2, 7, seq, seq * 441, 441, 44100, bytes(8))
                + samples.astype("<f4").tobytes()
            )
            audio.accept_packet(GRANT, payload, now=seq * 0.01)

    for _ in range(6):
        feed()
    bus, _, _ = _bus([_track(0.04, 44100 * 10)])
    from autodj.mixbus import SystemClock

    bus._clock = SystemClock()
    stop = threading.Event()

    class Master:
        startup_pending = False

        def __init__(self):
            self.blocks = []

        def process(self, block, *, enabled):
            assert enabled
            self.blocks.append(block.copy())
            return block * 0.5

    master = Master()
    stream = StreamOutput(320, 2, encoder_factory=FfmpegEncoder, loop=asyncio.get_running_loop())
    encoder = stream._encoder
    bus.set_master(master)
    bus.add_output(stream)
    captured = []

    class Capture:
        def write(self, block):
            captured.append(block.copy())
            count = len(captured)
            if count == 25:
                bus.take_local_programme(audio)
            elif 26 < count < 176:
                feed()
                feed()
            elif count == 176:
                bus.return_local_programme()
            elif count == 225:
                stop.set()

    bus.add_output(Capture())
    app = FastAPI()
    requests = []

    @app.get("/stream")
    async def stream_route():
        listener = stream.add_listener(icy=False)
        requests.append(listener)
        return StreamingResponse(listener.chunks(), media_type="audio/mpeg")

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        worker = None
        ending = None
        try:
            async with asyncio.timeout(15):
                while not server.started:
                    await asyncio.sleep(0.01)
                async with (
                    httpx.AsyncClient() as client,
                    client.stream("GET", f"http://127.0.0.1:{port}/stream") as response,
                ):
                    assert response.status_code == 200
                    bus.start_set()
                    worker = threading.Thread(target=bus.run, args=(stop,), daemon=True)
                    worker.start()

                    async def end_fixture():
                        await asyncio.to_thread(stop.wait)
                        # A live encoder deliberately has no EOF. End only the
                        # fixture listener after proving handoffs kept it open.
                        await asyncio.sleep(0.1)
                        assert len(requests) == 1 and not requests[0].closed
                        requests[0].close()

                    ending = asyncio.create_task(end_fixture())
                    encoded = bytearray()
                    async for chunk in response.aiter_bytes():
                        encoded.extend(chunk)
                    await ending
                assert len(requests) == 1
                assert stream._encoder is encoder and stream._encoder_generation == 0
                assert bus._master is master
                assert len(captured) == len(master.blocks) == 225
                assert audio.high_water_frames <= 2646
                assert not audio.failed
        finally:
            stop.set()
            if worker:
                await asyncio.to_thread(worker.join, 3)
            if ending:
                await ending
            stream.close()
            server.should_exit = True
            await serving
    decoded = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "mp3",
            "-i",
            "pipe:0",
            "-f",
            "f32le",
            "-ar",
            "44100",
            "-ac",
            "2",
            "pipe:1",
        ],
        input=bytes(encoded),
        capture_output=True,
        check=True,
        timeout=20,
    ).stdout
    pcm = np.frombuffer(decoded, dtype="<f4").reshape((-1, 2))
    # The production handoff applies a 20 ms fade-up to the first PCM block.
    expected = reference.copy()
    expected[:882] *= np.linspace(0, 1, 882, dtype=np.float32)[:, None]
    result = measure(expected, pcm, 44100)
    assert result["dropouts"] == 0
    assert not result["channel_swap"]
    assert abs(result["drift_ppm"]) < 100
    import json

    (tmp_path / "internal-stream.json").write_text(json.dumps(result, indent=2))


def test_real_socket_locality_and_proxy_refusal(tmp_path):
    """ADR-0019-02: actual TCP peer captured outside proxy rewriting admits only direct access."""
    import json

    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        app, _, _ = fixture_app(tmp_path, port=port)
        transport = RawPeerMiddleware(ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1"))
        server = uvicorn.Server(
            uvicorn.Config(
                transport,
                proxy_headers=False,
                lifespan="off",
                log_level="error",
                ws_max_size=65536,
                ws_max_queue=4,
                ws="websockets",
            )
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                assert thread.is_alive() and time.monotonic() < deadline
                time.sleep(0.01)
            for path in (PATH, "/api/stations/second/local-programme/ws"):
                with connect(
                    f"ws://127.0.0.1:{port}{path}", origin=f"http://127.0.0.1:{port}"
                ) as ws:
                    ws.send(json.dumps({"type": "claim", "version": 1, "rate": 44100}))
                    assert json.loads(ws.recv())["type"] == "granted"
            from websockets.exceptions import ConnectionClosedError, InvalidStatus

            with connect(f"ws://127.0.0.1:{port}{PATH}", origin=f"http://127.0.0.1:{port}") as ws:
                ws.send([bytes(32768)] * 3)
                with pytest.raises(ConnectionClosedError) as exc:
                    ws.recv()
                assert exc.value.rcvd.code == 1009

            with (
                pytest.raises(InvalidStatus),
                connect(
                    f"ws://127.0.0.1:{port}{PATH}",
                    origin=f"http://127.0.0.1:{port}",
                    additional_headers={"X-Forwarded-For": "127.0.0.1"},
                ),
            ):
                pass
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive()
