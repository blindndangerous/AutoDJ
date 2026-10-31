"""Opt-in programme socket and provenance captured before proxy rewriting."""

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import logging
import secrets
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from starlette.responses import FileResponse, JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from autodj.local_programme import OwnershipError
from autodj.local_programme_audio import PCMError
from autodj.security import COOKIE_NAME, SecurityPolicy, _raw_header_values
from autodj.station_routes import STATION_PREFIX, resolve_station

_RAW = "autodj.raw_transport"
logger = logging.getLogger(__name__)


async def _settle_control[T](operation: Coroutine[Any, Any, T]) -> T:
    """Keep acquisition and cleanup attached until their workers have finished."""
    task = asyncio.create_task(operation)
    cancelled = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            cancelled = exc
            continue
        except Exception:
            if cancelled is not None:
                logger.warning("Local control worker failed during cancellation", exc_info=True)
                raise cancelled from None
            raise
        if cancelled is not None:
            raise cancelled
        return result


class RawPeerMiddleware:
    """Must wrap, never sit inside, Uvicorn's ProxyHeadersMiddleware.

    Ordinary scope client/scheme values are untouched. Missing provenance fails
    local admission closed when an embedder bypasses the production wrapper.
    """

    def __init__(self, app: ASGIApp, *, proxy_configured: bool = False) -> None:
        self.app = app
        self.proxy_configured = proxy_configured

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            scope = dict(scope)
            scope[_RAW] = (
                scope.get("client"),
                scope.get("scheme"),
                scope.get("server"),
                self.proxy_configured,
            )
        await self.app(scope, receive, send)


def _loopback(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_loopback
    except ValueError:
        return False


def locality_allowed(scope: Scope, policy: SecurityPolicy) -> bool:
    """Only native, direct loopback sockets with exact same-origin authority."""
    config = policy.config
    if not config.local_programme_enabled or config.auth_mode != "native" or config.trusted_proxies:
        return False
    raw = scope.get(_RAW)
    if not raw:
        return False
    peer, scheme, server, proxy_configured = raw
    if not peer or not server or not _loopback(peer[0]) or proxy_configured:
        return False
    if any(
        name.lower() in {b"forwarded", b"x-real-ip"}
        or name.lower().startswith((b"x-forwarded-", b"x-autodj-"))
        for name, _ in scope["headers"]
    ):
        return False
    hosts, origins = (_raw_header_values(scope, name) for name in (b"host", b"origin"))
    if len(hosts) != 1 or len(origins) != 1:
        return False
    if not policy.host_allowed(hosts[0]) or not policy.origin_allowed(origins[0]):
        return False
    expected_scheme = "https" if scheme in {"https", "wss"} else "http"
    try:
        origin = urlsplit(origins[0])
        return (
            origins[0] == f"{expected_scheme}://{hosts[0]}"
            and (origin.hostname == "localhost" or _loopback(origin.hostname or ""))
            and (origin.port or (443 if expected_scheme == "https" else 80)) == server[1]
        )
    except ValueError:
        return False


def install_local_programme_routes(app: FastAPI, policy: SecurityPolicy) -> None:
    """Install nothing when disabled; existing installations retain their routes."""
    if not policy.config.local_programme_enabled:
        return

    async def capture_worklet() -> FileResponse:
        return FileResponse(
            Path(__file__).parent / "static" / "programme-worklet.js", media_type="text/javascript"
        )

    app.add_api_route("/programme-worklet.js", capture_worklet, methods=["GET"])

    async def ownership_error(_request: Request, exc: OwnershipError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    app.add_exception_handler(OwnershipError, ownership_error)  # type: ignore[arg-type]

    async def status(request: Request) -> dict[str, str | int]:
        return await asyncio.to_thread(resolve_station(request).local_programme.status)

    async def return_control(request: Request) -> dict[str, str | int]:
        controller = resolve_station(request).local_programme
        try:
            observed = await request.json()
        except ValueError:
            raise HTTPException(422, "Return requires the observed ownership") from None
        if (
            not isinstance(observed, dict)
            or set(observed) != {"epoch", "generation"}
            or not isinstance(observed["epoch"], str)
            or type(observed["generation"]) is not int
        ):
            raise HTTPException(422, "Return requires the observed epoch and generation")
        await asyncio.to_thread(
            controller.return_observed, observed["epoch"], observed["generation"]
        )
        if controller.performance is not None:
            # A timed-out renderer retains the local hold and reports the
            # safe silence state; callers can retry this observed return.
            with contextlib.suppress(OwnershipError):
                await asyncio.to_thread(
                    controller.performance.recover, expected_generation=observed["generation"]
                )
        return await asyncio.to_thread(controller.status)

    for prefix in ("/api", STATION_PREFIX):
        app.add_api_route(prefix + "/local-programme", status, methods=["GET"])
        app.add_api_route(prefix + "/local-programme/return", return_control, methods=["POST"])

    async def programme(websocket: WebSocket) -> None:
        if not locality_allowed(websocket.scope, policy):
            await websocket.close(code=4403)
            return
        if policy.authentication_required and not policy.browser_authenticated(websocket.scope):
            await websocket.close(code=4401)
            return
        try:
            station = resolve_station(websocket)
        except HTTPException as exc:
            await websocket.close(code=4000 + exc.status_code)
            return
        if not station.stream_mode or station.player.bus is None:
            await websocket.close(code=4409)
            return
        controller = station.local_programme
        performance = controller.performance
        admitted = False
        grant = None
        audio = None
        last_credit_limit = 0
        return_frame = None
        return_task = None
        return_deadline = None
        credit_event = asyncio.Event()
        send_lock = asyncio.Lock()
        credit_task = None
        loop = asyncio.get_running_loop()

        async def send_control(data: dict[str, Any]) -> None:
            async with send_lock:
                await websocket.send_json(data)

        async def send_credit(*, force: bool = False) -> None:
            nonlocal last_credit_limit
            async with send_lock:
                if audio is None or audio.failed:
                    return
                limit = audio.limit_frame
                if force or limit > last_credit_limit:
                    last_credit_limit = limit
                    await websocket.send_json(
                        {
                            "type": "credit",
                            "limit_frame": limit,
                            "state": "local"
                            if getattr(performance, "on_air", False)
                            else "preparing",
                        }
                    )

        def wake_credit() -> None:
            loop.call_soon_threadsafe(credit_event.set)

        async def publish_credit() -> None:
            assert audio is not None and grant is not None
            while True:
                await credit_event.wait()
                credit_event.clear()
                audio.acknowledge_credit_wakeup()
                if (
                    station.stopped
                    or audio.failed
                    or not policy.socket_session_is_valid(cookie, accepted)
                    or not controller.check(time.monotonic())
                ):
                    return
                await send_credit()

        async def enter_handshake() -> bool:
            nonlocal admitted
            admitted = await asyncio.to_thread(controller.enter_handshake)
            return admitted

        async def acquire(identity: str, now: float) -> list[dict[str, Any]]:
            nonlocal grant, audio
            grant = await asyncio.to_thread(controller.claim, identity, secrets.token_hex(16), now)
            audio = controller.audio
            return await asyncio.to_thread(performance.prepare, grant) if performance else []

        async def cleanup() -> None:
            if audio is not None:
                audio.set_credit_callback(None)
            if credit_task is not None:
                credit_task.cancel()
                await asyncio.gather(credit_task, return_exceptions=True)
            try:
                if grant is not None:
                    await asyncio.to_thread(
                        controller.revoke, grant.generation, "Owner socket closed"
                    )
                    if return_task is not None:
                        try:
                            await return_task
                        except Exception:
                            logger.exception("Local return preparation failed")
                    if performance is not None:
                        try:
                            await asyncio.to_thread(
                                performance.recover, expected_generation=grant.generation
                            )
                        except Exception:
                            logger.exception(
                                "Local recovery retained uncommitted consumption receipts"
                            )
            finally:
                if admitted:
                    await asyncio.to_thread(controller.leave_handshake)

        try:
            if not await _settle_control(enter_handshake()):
                await websocket.close(code=4429)
                return
            cookie = websocket.cookies.get(COOKIE_NAME)
            accepted = policy.monotonic_now()
            deadline = time.monotonic() + 2
            next_service = time.monotonic()
            await websocket.accept()
            while True:
                if credit_task is not None and credit_task.done():
                    await credit_task
                if return_task is not None and return_task.done():
                    ready = await return_task
                    return_task = None
                    if not ready:
                        raise OwnershipError("Automation did not become ready; recovering")
                    await send_control({"type": "return-ready"})
                now = time.monotonic()
                if return_deadline is not None and now >= return_deadline:
                    raise OwnershipError("Local return timed out; recovering automation")
                if station.stopped or not policy.socket_session_is_valid(cookie, accepted):
                    await websocket.close(code=4401)
                    break
                if (grant is None and now >= deadline) or (grant and not controller.check(now)):
                    await websocket.close(code=4408)
                    break
                if audio is not None and not audio.check(now):
                    await websocket.close(code=4408, reason=audio.failed)
                    break
                if (
                    return_frame is not None
                    and audio is not None
                    and audio.consumed_frame >= return_frame
                ):
                    await websocket.close(code=1000, reason="Returned to automation")
                    break
                if (
                    performance is not None
                    and grant is not None
                    and (
                        now >= next_service
                        or (
                            not getattr(performance, "on_air", False)
                            and audio is not None
                            and audio.ready
                        )
                    )
                ):
                    next_service = now + 0.1
                    for acknowledgement in await asyncio.to_thread(performance.service):
                        await send_control(acknowledgement)
                try:
                    message = await asyncio.wait_for(websocket.receive(), 0.1)
                except TimeoutError:
                    continue
                if message["type"] == "websocket.disconnect":
                    break
                if station.stopped or not policy.socket_session_is_valid(cookie, accepted):
                    await websocket.close(code=4401)
                    break
                if message.get("bytes") is not None and grant is not None:
                    if audio is None:
                        raise OwnershipError("Local programme input is no longer available")
                    controller.validate(grant, time.monotonic())
                    audio.accept_packet(grant, message["bytes"])
                    await send_credit(force=True)
                    continue
                text = message.get("text")
                if not isinstance(text, str) or len(text.encode()) > 4096:
                    await websocket.close(code=1009)
                    break
                now = time.monotonic()
                if not await asyncio.to_thread(controller.consume_control, now):
                    await websocket.close(code=1009)
                    break
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise ValueError("Expected a control object")
                if grant is None:
                    if data != {"type": "claim", "version": 1, "rate": 44100}:
                        raise ValueError("Expected version 1 claim at 44,100 Hz")
                    identity = hashlib.sha256((cookie or "native-anonymous").encode()).hexdigest()
                    occurrences = await _settle_control(acquire(identity, now))
                    assert grant is not None and audio is not None
                    audio.begin_capture()
                    last_credit_limit = 8820
                    await send_control(
                        {
                            "type": "granted",
                            "epoch": grant.epoch,
                            "generation": grant.generation,
                            "capability": grant.capability,
                            "state": "preparing",
                            "limit_frame": 8820,
                            **({"occurrences": occurrences} if performance else {}),
                        }
                    )
                    audio.set_credit_callback(wake_credit)
                    credit_task = asyncio.create_task(publish_credit())
                    continue
                controller.validate(grant, now)
                if (
                    set(data)
                    != (
                        {"type", "version", "epoch", "generation", "capability"}
                        | ({"event"} if data.get("type") == "marker" else set())
                        | (
                            {"frame"}
                            if data.get("type") == "release" and "frame" in data
                            else set()
                        )
                    )
                    or data["version"] != 1
                ):
                    raise ValueError("Invalid version or control fields")
                if any(
                    data.get(key) != getattr(grant, key)
                    for key in ("epoch", "generation", "capability")
                ):
                    raise OwnershipError("Local control lease is stale")
                if data.get("type") == "heartbeat":
                    await asyncio.to_thread(controller.renew, grant, now)
                    state = await asyncio.to_thread(controller.status)
                    occurrences = await asyncio.to_thread(performance.views) if performance else []
                    await send_control(
                        {
                            "type": "renewed",
                            "state": state["state"],
                            **({"occurrences": occurrences} if performance else {}),
                        }
                    )
                elif data.get("type") == "marker" and performance is not None:
                    if not isinstance(data["event"], dict):
                        raise ValueError("Invalid occurrence marker")
                    result = await asyncio.to_thread(performance.marker, data["event"])
                    next_service = now
                    await send_control({"type": result, "sequence": data["event"]["sequence"]})
                elif data.get("type") == "prepare-return" and performance is not None:
                    if (
                        return_task is None
                        and not performance.returning
                        and performance.begin_return()
                    ):
                        return_deadline = time.monotonic() + 3
                        return_task = asyncio.create_task(
                            asyncio.to_thread(performance.prepare_return)
                        )
                elif data.get("type") == "release":
                    if (
                        "frame" in data
                        and audio is not None
                        and performance is not None
                        and performance.on_air
                    ):
                        frame = data["frame"]
                        if (
                            type(frame) is not int
                            or not 0 <= frame <= audio.next_sequence * 441 + 44100
                        ):
                            raise ValueError("Invalid return boundary")
                        return_frame = frame
                        continue
                    await websocket.close(code=1000)
                    break
                else:
                    raise OwnershipError("On-air activation is not available in this slice")
        except OwnershipError as exc:
            await websocket.close(code=4409, reason=str(exc)[:100])
        except PCMError as exc:
            await websocket.close(code=exc.code, reason=str(exc)[:100])
        except (ValueError, RecursionError):
            await websocket.close(code=1003, reason="Invalid local programme control message")
        except WebSocketDisconnect:
            pass
        finally:
            await _settle_control(cleanup())

    app.add_api_websocket_route("/api/local-programme/ws", programme)
    app.add_api_websocket_route(STATION_PREFIX + "/local-programme/ws", programme)
