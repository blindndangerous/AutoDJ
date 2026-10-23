"""Paired-operator WebSockets for listening to the station's server mix."""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import FastAPI, HTTPException, WebSocket
from starlette.websockets import WebSocketDisconnect

from autodj.monitor import MonitorClosed
from autodj.security import COOKIE_NAME, SecurityPolicy, _raw_header_values
from autodj.station_routes import STATION_PREFIX, resolve_station


def install_monitor_routes(app: FastAPI, policy: SecurityPolicy) -> None:
    """Install the default and named-station read-only monitor sockets."""

    async def monitor(websocket: WebSocket) -> None:
        hosts = _raw_header_values(websocket.scope, b"host")
        origins = _raw_header_values(websocket.scope, b"origin")
        if len(hosts) != 1 or not policy.host_allowed(hosts[0]):
            await websocket.close(code=4403)
            return
        if len(origins) != 1 or not policy.origin_allowed(origins[0]):
            await websocket.close(code=4403)
            return
        cookie = websocket.cookies.get(COOKIE_NAME)
        if policy.authentication_required and not policy.browser_authenticated(websocket.scope):
            await websocket.close(code=4401)
            return
        try:
            station = resolve_station(websocket)
        except HTTPException as exc:
            await websocket.close(code=4000 + exc.status_code)
            return
        if not station.monitor_available:
            await websocket.close(code=4409)
            return
        client = station.subscribe_monitor(asyncio.get_running_loop())
        if client is None:
            await websocket.close(code=4409)
            return
        accepted_at = policy.monotonic_now()
        receive_task: asyncio.Task | None = None
        audio_task: asyncio.Task | None = None
        try:
            await websocket.accept()
            receive_task = asyncio.create_task(websocket.receive())
            audio_task = asyncio.create_task(client.receive())
            while True:
                if station.stopped:
                    await websocket.close(code=4409)
                    break
                if not policy.socket_session_is_valid(cookie, accepted_at):
                    await websocket.close(code=4401)
                    break
                done, _ = await asyncio.wait(
                    (receive_task, audio_task), timeout=1.0, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    continue
                if not policy.socket_session_is_valid(cookie, accepted_at):
                    await websocket.close(code=4401)
                    break
                if receive_task in done:
                    message = receive_task.result()
                    if message["type"] == "websocket.disconnect":
                        break
                    await websocket.close(code=1003)
                    break
                try:
                    payload = audio_task.result()
                except MonitorClosed:
                    await websocket.close(code=4409)
                    break
                try:
                    await asyncio.wait_for(websocket.send_bytes(payload), timeout=1.0)
                except TimeoutError:
                    with contextlib.suppress(
                        TimeoutError, OSError, RuntimeError, WebSocketDisconnect
                    ):
                        await asyncio.wait_for(websocket.close(code=1013), timeout=1.0)
                    break
                except (OSError, WebSocketDisconnect):
                    break
                audio_task = asyncio.create_task(client.receive())
        finally:
            # Detach synchronously before awaiting task cancellation. TestClient
            # and server shutdown can cancel this scope as it exits; cleanup
            # must not leave a dead listener attached to the shared bus.
            station.unsubscribe_monitor(client)
            for task in (receive_task, audio_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (receive_task, audio_task) if task is not None),
                return_exceptions=True,
            )

    app.add_api_websocket_route("/api/monitor", monitor)
    app.add_api_websocket_route(STATION_PREFIX + "/monitor", monitor)
