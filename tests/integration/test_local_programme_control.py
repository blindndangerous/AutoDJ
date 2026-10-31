"""Socket control regressions without a browser or optional Node packages."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import Mock

import pytest
from fastapi import WebSocket

from tests.integration.test_local_programme import (
    BASE,
    PATH,
    WS,
    claim,
    client_for,
    disconnected,
    fixture_app,
)


def test_nested_control_closes_with_unsupported_data(tmp_path, monkeypatch):
    """ADR-0020-03: recursion-limited parsers reject nested controls with 1003."""
    from autodj import local_programme_routes as routes

    app, _, _ = fixture_app(tmp_path)
    original = json.loads

    def bounded_parser(text):
        # CPython versions differ in their JSON recursion ceiling.
        if text.startswith("[["):
            raise RecursionError("maximum recursion depth exceeded")
        return original(text)

    monkeypatch.setattr(routes, "json", SimpleNamespace(loads=bounded_parser))
    with client_for(app).websocket_connect(WS + PATH, headers={"Origin": BASE}) as ws:
        ws.send_text("[" * 1500 + "0" + "]" * 1500)
        with disconnected(1003):
            ws.receive_json()


def test_handshake_cleanup_covers_cookie_failure(tmp_path, monkeypatch):
    """ADR-0020-16: admission slots survive exceptions before socket acceptance."""
    from autodj import local_programme_routes as routes

    app, _, stations = fixture_app(tmp_path)
    controller = stations["default"].local_programme

    def broken_cookie(_self):
        raise RuntimeError("cookie parsing failed")

    monkeypatch.setattr(WebSocket, "cookies", property(broken_cookie))
    monkeypatch.setattr(routes, "locality_allowed", lambda *_args: True)
    monkeypatch.setattr(routes, "resolve_station", lambda _socket: stations["default"])
    endpoint = next(route.endpoint for route in app.routes if route.path == PATH)
    with pytest.raises(RuntimeError, match="cookie parsing failed"):
        asyncio.run(endpoint(WebSocket({"type": "websocket"}, None, None)))
    assert controller._handshakes == 0


def test_control_bucket_survives_reconnect(tmp_path, monkeypatch):
    """ADR-0020-03: reconnecting does not refill the station's control budget."""
    from autodj import local_programme_routes as routes

    app, _, stations = fixture_app(tmp_path)
    controller = stations["default"].local_programme
    now = time.monotonic()
    monkeypatch.setattr(routes, "time", SimpleNamespace(monotonic=lambda: now))
    assert all(controller.consume_control(now) for _ in range(10))
    assert not controller.consume_control(now)
    with (
        client_for(app).websocket_connect(WS + PATH, headers={"Origin": BASE}) as ws,
        disconnected(1009),
    ):
        claim(ws)
    assert controller.consume_control(now + 0.051)


def test_pcm_packet_rate_does_not_drive_service(tmp_path, monkeypatch):
    """ADR-0020-06: PCM ingress does not multiply receipt service work."""
    from autodj import local_programme_routes as routes

    app, _, stations = fixture_app(tmp_path)
    controller = stations["default"].local_programme
    clock = [time.monotonic()]
    performance = SimpleNamespace(
        busy=False,
        admit=lambda: None,
        prepare=lambda _grant: [],
        service=Mock(return_value=[]),
        recover=lambda **_kwargs: None,
    )
    controller.performance = performance
    monkeypatch.setattr(routes, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    loop_thread = threading.get_ident()
    validation_threads = []
    claimed = False
    original_validate, original_check = controller.validate, controller.check

    def validate(*args):
        validation_threads.append(threading.get_ident())
        return original_validate(*args)

    def check(*args):
        if claimed:
            validation_threads.append(threading.get_ident())
        return original_check(*args)

    monkeypatch.setattr(controller, "validate", validate)
    monkeypatch.setattr(controller, "check", check)

    class Socket:
        scope: ClassVar = {"type": "websocket"}
        cookies: ClassVar = {}
        packets = 0

        async def accept(self):
            pass

        async def close(self, **_kwargs):
            pass

        async def send_json(self, data):
            nonlocal claimed
            if data["type"] == "granted":
                claimed = True
                controller.audio.check = lambda _now: True
                controller.audio.accept_packet = lambda *_args: None

        async def receive(self):
            self.packets += 1
            if self.packets == 1:
                return {
                    "type": "websocket.receive",
                    "text": json.dumps({"type": "claim", "version": 1, "rate": 44100}),
                }
            clock[0] += 0.01
            if self.packets > 101:
                return {"type": "websocket.disconnect"}
            return {"type": "websocket.receive", "bytes": b"pcm"}

    monkeypatch.setattr(routes, "locality_allowed", lambda *_args: True)
    monkeypatch.setattr(routes, "resolve_station", lambda _socket: stations["default"])
    endpoint = next(route.endpoint for route in app.routes if route.path == PATH)
    asyncio.run(endpoint(Socket()))
    assert 8 <= performance.service.call_count <= 11
    assert validation_threads and set(validation_threads) == {loop_thread}


@pytest.mark.parametrize("operation", ["claim", "marker"])
def test_blocking_control_leaves_event_loop_responsive(tmp_path, monkeypatch, operation):
    """ADR-0020-16: lock-taking control operations run outside the ASGI loop."""
    from autodj import local_programme_routes as routes

    app, _, stations = fixture_app(tmp_path)
    controller = stations["default"].local_programme
    loop_thread = threading.get_ident()
    observed = []
    original_claim = controller.claim

    def blocking_claim(*args):
        if operation == "claim":
            observed.append(threading.get_ident())
        return original_claim(*args)

    def blocking_marker(_event):
        observed.append(threading.get_ident())
        return "accepted"

    controller.claim = blocking_claim
    controller.performance = SimpleNamespace(
        busy=False,
        admit=lambda: None,
        prepare=lambda _grant: [],
        service=lambda: [],
        recover=lambda **_kwargs: None,
        marker=blocking_marker,
    )

    class Socket:
        scope: ClassVar = {"type": "websocket"}
        cookies: ClassVar = {}

        async def accept(self):
            pass

        async def close(self, **_kwargs):
            pass

        async def send_json(self, data):
            if data["type"] == "granted" and operation == "marker":
                self.messages.append(
                    {
                        "type": "websocket.receive",
                        "text": json.dumps(
                            {
                                "type": "marker",
                                "version": 1,
                                "event": {"sequence": 0},
                                **{key: data[key] for key in ("epoch", "generation", "capability")},
                            }
                        ),
                    }
                )

        async def receive(self):
            return self.messages.pop(0) if self.messages else {"type": "websocket.disconnect"}

    socket = Socket()
    socket.messages = [
        {
            "type": "websocket.receive",
            "text": json.dumps({"type": "claim", "version": 1, "rate": 44100}),
        }
    ]
    monkeypatch.setattr(routes, "locality_allowed", lambda *_args: True)
    monkeypatch.setattr(routes, "resolve_station", lambda _socket: stations["default"])
    endpoint = next(route.endpoint for route in app.routes if route.path == PATH)
    asyncio.run(endpoint(socket))
    assert observed and all(thread != loop_thread for thread in observed)


def test_review_mutation_is_fenced_without_a_live_placement_guard():
    """ADR-0020-01: review writes require returning to automation first."""
    from fastapi import HTTPException
    from starlette.requests import Request

    from autodj.station_routes import _fence_local_programming

    station = SimpleNamespace(
        local_programme=SimpleNamespace(performance=SimpleNamespace(busy=True))
    )
    request = Request(
        {"type": "http", "method": "POST", "path": "/api/reviews", "headers": [], "scheme": "http"}
    )
    with pytest.raises(HTTPException) as error:
        _fence_local_programming(request, station)
    assert error.value.status_code == 409


def test_slow_return_does_not_block_other_requests(tmp_path):
    """ADR-0020-07: an automation render wait does not stall the event loop."""
    app, _, stations = fixture_app(tmp_path)
    controller = stations["default"].local_programme
    grant = controller.claim("user", "socket", time.monotonic())
    controller.performance = SimpleNamespace(busy=False, recover=lambda **_kwargs: time.sleep(0.2))
    endpoint = next(
        route.endpoint for route in app.routes if route.path == "/api/local-programme/return"
    )

    class Request:
        app = None
        path_params: ClassVar = {}

        async def json(self):
            return {"epoch": grant.epoch, "generation": grant.generation}

    request = Request()
    request.app = app

    async def run():
        task = asyncio.create_task(endpoint(request))
        started = time.monotonic()
        await asyncio.sleep(0.025)
        elapsed = time.monotonic() - started
        await task
        assert elapsed < 0.15

    asyncio.run(run())
