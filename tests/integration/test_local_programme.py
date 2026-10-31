"""Local input admission on ASGI and real TCP sockets, without model startup."""

import asyncio
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


async def _wait_for_encoded_idle(encoded):
    """Drain a live 320 kbps capture without relying on an encoder EOF."""
    deadline = time.monotonic() + 10
    last_growth = time.monotonic()
    size = len(encoded)
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        now = time.monotonic()
        if len(encoded) != size:
            size = len(encoded)
            last_growth = now
        # At least three seconds at 320 kbps must cover the programme stimulus.
        if size >= 120000 and now - last_growth >= 0.5:
            return
    raise AssertionError(f"Encoded capture did not settle within 10 s ({size} bytes)")


@pytest.mark.parametrize("delivery", ["delayed", "empty", "continuous"])
async def test_encoded_capture_waits_for_idle_with_a_deadline(monkeypatch, delivery):
    from tests.integration import test_local_programme as module

    clock = [0.0]
    encoded = bytearray()
    arrivals = [0.3, 0.8, 1.1]

    async def sleep(delay):
        clock[0] += delay
        if delivery == "continuous":
            encoded.extend(bytes(40000))
        elif delivery == "delayed":
            while arrivals and clock[0] >= arrivals[0]:
                arrivals.pop(0)
                encoded.extend(bytes(40000))

    monkeypatch.setattr(module, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    if delivery == "delayed":
        await _wait_for_encoded_idle(encoded)
        assert len(encoded) == 120000
        assert 1.6 <= clock[0] < 2
    else:
        with pytest.raises(AssertionError, match="capture"):
            await _wait_for_encoded_idle(encoded)
        assert 10 <= clock[0] < 10.2


def test_administrative_return_rejects_an_old_observed_generation(tmp_path):
    """ADR-0020-01: a delayed Return cannot revoke a replacement owner."""
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
    """ADR-0020-04: private review and recording do not change programme ownership."""
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
    """ADR-0020-16: socket identity wins over shared cookie; stations stay separate."""
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
    """ADR-0020-16: a silent socket cannot keep a revoked session or stopped station."""
    app, policy, stations = fixture_app(tmp_path)
    client = client_for(app)
    with client.websocket_connect(WS + PATH, headers={"Origin": BASE}) as ws:
        claim(ws)
        policy.socket_session_is_valid = lambda *_: False
        with disconnected(4401):
            ws.receive_json()
    assert stations["default"].live.claim_mic() is not None


def test_revocation_while_receive_waits_cannot_claim(tmp_path, monkeypatch):
    """ADR-0020-16: revalidate authorization after receive, before any mutation."""
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
    """ADR-0020-02: forwarded loopback claims and cross-origin handshakes fail."""
    app, _, _ = fixture_app(tmp_path)
    with (
        disconnected(4403),
        client_for(app).websocket_connect(WS + PATH, headers={"Origin": BASE, header: value}),
    ):
        pass


def test_disabled_has_no_routes_and_missing_provenance_fails_closed(tmp_path):
    """ADR-0020-12: opt-out installs no routes; unsupported ASGI deployments fail closed."""
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
    """ADR-0020-03: zero PCM earns credit; malformed and stalled sockets release reservations."""
    from tests.unit.test_local_programme_audio import packet

    app, _, stations = fixture_app(tmp_path)
    with client_for(app).websocket_connect(WS + PATH, headers={"Origin": BASE}) as ws:
        grant = claim(ws)
        ws.send_bytes(packet(generation=grant["generation"], values=(0, 0)))
        assert ws.receive_json() == {"type": "credit", "limit_frame": 9261, "state": "preparing"}
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
    """ADR-0020-03: draining an internally armed input grants credit without inbound traffic."""
    from tests.unit.test_local_programme_audio import packet

    app, _, stations = fixture_app(tmp_path)
    with client_for(app).websocket_connect(WS + PATH, headers={"Origin": BASE}) as ws:
        grant = claim(ws)
        audio = stations["default"].local_programme.audio
        audio.arm()  # Fixture-only seam; the public protocol has no activate operation.
        for sequence in range(6):
            ws.send_bytes(packet(sequence, generation=grant["generation"]))
            assert ws.receive_json() == {
                "type": "credit",
                "limit_frame": 8820,
                "state": "preparing",
            }
            time.sleep(0.01)
        audio.read_block(grant["generation"])
        assert ws.receive_json() == {"type": "credit", "limit_frame": 9702, "state": "preparing"}


@pytest.mark.parametrize("startup_blocks", [0, 275], ids=["ready", "late-input"])
async def test_internal_selector_keeps_master_encoder_and_http_connection(tmp_path, startup_blocks):
    """ADR-0020-13: real encoder and one HTTP response survive internal input selection.

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
    # Keep the fake bus clock: this proves selection/output continuity, not
    # scheduler throughput. Bounded batches below wait for actual encoder IO.
    bus, _, _ = _bus([_track(0.04, 44100 * 30)])
    stop = threading.Event()

    class Master:
        startup_pending = False

        def __init__(self):
            self.blocks = []

        def process(self, block, *, enabled):
            assert enabled
            self.blocks.append(block.copy())
            return block * 0.5

    class StartupEncoder(FfmpegEncoder):
        """Simulate unavailable startup input, beyond the old 225-block fixture.

        Discard only this test's automation pre-roll, without buffering it or
        sleeping. Once input is available, use the real encoder unchanged.
        """

        def __init__(self, bitrate):
            super().__init__(bitrate)
            self.seen = 0

        def write(self, pcm):
            self.seen += 1
            if self.seen > startup_blocks:
                super().write(pcm)

    master = Master()
    stream = StreamOutput(320, 2, encoder_factory=StartupEncoder, loop=asyncio.get_running_loop())
    encoder = stream._encoder
    bus.set_master(master)
    bus.add_output(stream)
    captured = []
    batch_end = 0
    local_blocks = None

    class Capture:
        def write(self, block):
            nonlocal local_blocks
            captured.append(block.copy())
            if local_blocks is not None:
                local_blocks += 1
                # The first selected block fades down automation. Every later
                # block consumes 882 frames; replenish exactly that amount.
                if local_blocks > 1:
                    feed()
                    feed()
            if len(captured) == batch_end:
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
        reading = None
        encoded = bytearray()

        async def render_batch(count=25):
            nonlocal batch_end
            assert 0 < count <= 25  # Below StreamOutput's 100-block queue cap.
            assert len(captured) < 1000, "Encoder/listener made no progress within 1000 blocks"
            batch_end = len(captured) + count
            stop.clear()

            # Drive bounded fake-clock work synchronously, then yield for IO.
            # A separate pacing thread and per-block writer wakeups turn this
            # fixture into a scheduler benchmark on one contended core.
            with stream._pcm_cond:
                bus.run(stop)
            assert len(captured) == batch_end
            # Do not overrun the live output's bounded, intentionally lossy
            # queue. Also let HTTP consume everything published so far before
            # supplying another batch. Neither wait assumes a startup latency.
            while stream._pcm_in < len(captured) * 882 * 2 * 4 or len(encoded) < stream._mp3_out:
                if reading.done():
                    await reading
                    raise AssertionError("HTTP stream closed before the handoff finished")
                await asyncio.sleep(0.01)

        try:
            async with asyncio.timeout(25):
                while not server.started:
                    await asyncio.sleep(0.01)
                async with (
                    httpx.AsyncClient(timeout=httpx.Timeout(None)) as client,
                    client.stream("GET", f"http://127.0.0.1:{port}/stream") as response,
                ):
                    assert response.status_code == 200
                    assert client.timeout.read is None
                    bus.start_set()

                    async def read_stream():
                        async for chunk in response.aiter_bytes():
                            encoded.extend(chunk)

                    reading = asyncio.create_task(read_stream())
                    # A live encoder has no EOF to flush a finite startup clip.
                    # Keep pre-roll flowing until real audio bytes reach HTTP,
                    # including when startup discarded more than 225 blocks.
                    while len(encoded) < 4096:
                        await render_batch()
                    warmup_blocks = len(captured)
                    assert encoder.seen > startup_blocks
                    # Count from encoder input, not the currently received
                    # prefix: startup bytes can still be in flight at takeover.
                    # At 320 kbps each 20 ms block represents 800 encoded bytes.
                    byte_target = (warmup_blocks - startup_blocks + 151 + 50) * 800
                    bus.take_local_programme(audio)
                    local_blocks = 0
                    for _ in range(6):
                        await render_batch()
                    await render_batch(1)  # One fade-down + 150 local PCM blocks.
                    assert local_blocks == 151
                    assert audio.consumed_frame == len(reference)
                    local_blocks = None
                    bus.return_local_programme()
                    # Keep automation supplying the encoder until the listener
                    # has the programme and return tail, not merely until some
                    # number of wall-clock periods has elapsed.
                    while len(encoded) < byte_target:
                        await render_batch()
                    await _wait_for_encoded_idle(encoded)
                    assert len(requests) == 1 and not requests[0].closed
                    requests[0].close()
                    await reading
                assert len(requests) == 1
                assert stream._encoder is encoder and stream._encoder_generation == 0
                assert bus._master is master
                assert len(captured) == len(master.blocks)
                assert len(captured) > warmup_blocks + 151
                assert audio.high_water_frames <= 2646
                assert not audio.failed
        finally:
            stop.set()
            for listener in requests:
                listener.close()
            if reading:
                if not reading.done():
                    reading.cancel()
                await asyncio.gather(reading, return_exceptions=True)
            stream.close()
            server.should_exit = True
            await serving
    assert len(encoded) >= 120000, f"Encoded capture too short: {len(encoded)} bytes"
    encoded_path = tmp_path / "selector.mp3"
    decoded_path = tmp_path / "selector.f32"
    encoded_path.write_bytes(encoded)
    # File IO keeps this verification decode independent of subprocess stdin/
    # stdout pump scheduling on a contended host. The live capture above still
    # exercises the production encoder pipes and the real HTTP response.
    with (tmp_path / "decode.stderr").open("w+b") as errors:
        decoded = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-threads",
                "1",
                "-filter_threads",
                "1",
                "-f",
                "mp3",
                "-i",
                str(encoded_path),
                "-f",
                "f32le",
                "-ar",
                "44100",
                "-ac",
                "2",
                str(decoded_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            check=False,
            timeout=20,
        )
        errors.seek(0)
        assert decoded.returncode == 0, errors.read().decode("utf-8", "replace")
    pcm = np.fromfile(decoded_path, dtype="<f4").reshape((-1, 2))
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
    """ADR-0020-02: actual TCP peer captured outside proxy rewriting admits only direct access."""
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
