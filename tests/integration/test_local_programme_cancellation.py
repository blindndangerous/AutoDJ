"""Ownership acquisition and cleanup stay paired across async cancellation."""

import asyncio
import contextlib
import json
import logging
import threading
import time
from types import SimpleNamespace
from typing import ClassVar

import pytest

from tests.integration.test_local_programme import PATH, fixture_app
from tests.unit.test_local_programme_playout import fixture


@pytest.mark.parametrize(
    "operation", ["enter_handshake", "claim", "prepare", "recover", "leave_handshake"]
)
def test_cancelled_control_waits_for_worker_before_cleanup(tmp_path, monkeypatch, operation):
    """ADR-0020-16: cancellation settles acquisition and cleanup before releasing the socket."""
    from autodj import local_programme_routes as routes

    app, _, _ = fixture_app(tmp_path)
    performance, controller, station, _ = fixture(tmp_path)
    station.stream_mode = True
    entered, release, finished = (threading.Event() for _ in range(3))
    target = performance if operation in {"prepare", "recover"} else controller
    original = getattr(target, operation)

    def blocked(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(3), "test did not release the control worker"
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(target, operation, blocked)
    monkeypatch.setattr(routes, "locality_allowed", lambda *_args: True)
    monkeypatch.setattr(routes, "resolve_station", lambda _connection: station)
    endpoint = next(route.endpoint for route in app.routes if route.path == PATH)

    class Socket:
        scope: ClassVar = {"type": "websocket"}
        cookies: ClassVar = {}
        claimed = False

        async def accept(self):
            pass

        async def close(self, **_kwargs):
            pass

        async def send_json(self, _data):
            pass

        async def receive(self):
            if self.claimed:
                return {"type": "websocket.disconnect"}
            self.claimed = True
            return {
                "type": "websocket.receive",
                "text": json.dumps({"type": "claim", "version": 1, "rate": 44100}),
            }

    async def run():
        task = asyncio.create_task(endpoint(Socket()))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            done, _ = await asyncio.wait([task], timeout=0.05)
            assert not done, "route cleanup escaped while its acquisition worker was running"
            task.cancel()
            done, _ = await asyncio.wait([task], timeout=0.05)
            assert not done, "repeated cancellation detached the control worker"
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 1)
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert controller._handshakes == 0
        assert controller._grant is None
        assert not performance.busy
        assert not getattr(station.player._state, "local_monitor_owned", False)

    asyncio.run(run())


def test_cancelled_control_logs_worker_failure_before_reraising(tmp_path, caplog):
    """ADR-0020-16: cancellation retains diagnostics from a failed control worker."""
    from autodj.local_programme_routes import _settle_control

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def worker():
            entered.set()
            await release.wait()
            raise OSError("ledger disk unavailable")

        task = asyncio.create_task(_settle_control(worker()))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.WARNING, logger="autodj.local_programme_routes"):
        asyncio.run(run())
    assert any(
        record.name == "autodj.local_programme_routes"
        and record.levelno >= logging.WARNING
        and record.exc_info
        and str(record.exc_info[1]) == "ledger disk unavailable"
        for record in caplog.records
    )


def test_queued_return_preparation_blocks_administrative_recovery(tmp_path, monkeypatch):
    """ADR-0020-07: Return reserves preparation before its worker can be scheduled."""
    from autodj import local_programme_routes as routes

    app, _, _ = fixture_app(tmp_path)
    performance, _controller, station, _ = fixture(tmp_path)
    station.stream_mode = True
    monkeypatch.setattr(routes, "locality_allowed", lambda *_args: True)
    monkeypatch.setattr(routes, "resolve_station", lambda _connection: station)
    endpoint = next(route.endpoint for route in app.routes if route.path == PATH)
    original_to_thread = asyncio.to_thread

    async def run():
        queued, release, disconnect = (asyncio.Event() for _ in range(3))

        async def delayed_worker(function, *args, **kwargs):
            if function == performance.prepare_return:
                queued.set()
                await release.wait()
            return await original_to_thread(function, *args, **kwargs)

        monkeypatch.setattr(routes.asyncio, "to_thread", delayed_worker)

        class Socket:
            scope: ClassVar = {"type": "websocket"}
            cookies: ClassVar = {}
            received = 0

            async def accept(self):
                pass

            async def close(self, **_kwargs):
                pass

            async def send_json(self, data):
                if data["type"] == "granted":
                    self.grant = data
                    performance.on_air = True

            async def receive(self):
                self.received += 1
                if self.received == 1:
                    data = {"type": "claim", "version": 1, "rate": 44100}
                elif self.received == 2:
                    data = {
                        "type": "prepare-return",
                        "version": 1,
                        **{key: self.grant[key] for key in ("epoch", "generation", "capability")},
                    }
                else:
                    await disconnect.wait()
                    return {"type": "websocket.disconnect"}
                return {"type": "websocket.receive", "text": json.dumps(data)}

        task = asyncio.create_task(endpoint(Socket()))
        recovery = None
        try:
            await asyncio.wait_for(queued.wait(), 1)
            intent_recorded = performance.returning and not performance._return_done.is_set()
            recovery = asyncio.create_task(original_to_thread(performance.recover))
            done, _ = await asyncio.wait([recovery], timeout=0.05)
            assert not done, "administrative recovery escaped a queued Return worker"
            assert intent_recorded
            with pytest.raises(routes.OwnershipError, match="progress"):
                performance.begin_return()
            assert performance.busy
        finally:
            release.set()
            disconnect.set()
            if recovery is not None:
                await recovery
            await task
        assert not performance.busy

    asyncio.run(run())


def test_delayed_administrative_recovery_preserves_replacement_owner(tmp_path, monkeypatch):
    """ADR-0020-01: delayed recovery cannot mutate a replacement generation."""
    from autodj import local_programme_routes as routes

    app, _, _ = fixture_app(tmp_path)
    performance, controller, station, _ = fixture(tmp_path)
    monkeypatch.setattr(routes, "resolve_station", lambda _connection: station)
    old = controller.claim("user", "old", time.monotonic())
    performance.prepare(old)
    controller.revoke(old.generation, "returned")
    performance.recover()
    returned, release = threading.Event(), threading.Event()
    original = controller.return_observed

    def delayed_return(*args):
        original(*args)
        returned.set()
        assert release.wait(3), "test did not release the old return request"

    monkeypatch.setattr(controller, "return_observed", delayed_return)
    endpoint = next(
        route.endpoint for route in app.routes if route.path == "/api/local-programme/return"
    )

    async def observed():
        return {"epoch": old.epoch, "generation": old.generation}

    async def run():
        task = asyncio.create_task(endpoint(SimpleNamespace(json=observed)))
        try:
            assert await asyncio.to_thread(returned.wait, 1)
            replacement = controller.claim("user", "replacement", time.monotonic())
            views = performance.prepare(replacement)
            audio = controller.audio
        finally:
            release.set()
        await task
        controller.validate(replacement, time.monotonic())
        assert performance.audio is audio
        assert performance.views() == views
        assert station.player._state.local_monitor_owned
        assert controller.live._local_owner is replacement

    asyncio.run(run())
