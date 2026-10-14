"""Live DJ routes (#48): the Icecast source mount, DJ accounts and talkover.

* ``PUT``/``SOURCE /live`` (the default station) and
  ``/stations/<slug>/live``: an Icecast source client streams MP3, AAC or
  Ogg here with HTTP Basic credentials from the station's DJ accounts.
  These skip the browser session and Origin checks (source clients have
  neither) and make their own: one Basic header, scrypt in a bounded
  worker pool, five failures a minute per address (per /64 for IPv6),
  one attempt in flight per address, and the credential only over HTTPS
  or from this machine, or from the LAN when ``[server]
  live_plain_http_lan`` allows plain HTTP there.  A close-delimited body
  (what BUTT and Mixxx send) reaches this route through
  :mod:`autodj.live_protocol`.
* ``GET /api/live``, ``PUT /api/live/settings``, ``POST /api/live/djs``,
  ``DELETE /api/live/djs/<username>``: the operator's DJ accounts and
  talkover settings, with the usual session and Origin checks.
* ``WS /api/live/mic``: a paired browser's push-to-talk microphone.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections import deque
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from autodj.listener_access import ListenerAuthLimiter, normalize_address
from autodj.live import (
    MAX_SOURCE_BITRATE,
    MIC_IDLE_SECONDS,
    MIC_MAX_MESSAGE_BYTES,
    MIC_RATES,
    SOURCE_BURST_BYTES,
    SOURCE_FORMATS,
    SOURCE_IDLE_SECONDS,
    SOURCE_SILENCE_SECONDS,
    SOURCE_STANDBY_SECONDS,
    MicResampler,
    TalkoverSettings,
)
from autodj.security import (
    COOKIE_NAME,
    SecurityPolicy,
    _raw_header_values,
    is_canonical_path,
    machine_transport_allowed,
)
from autodj.station_routes import STATION_PREFIX, StationDep, StationRouter, resolve_station

logger = logging.getLogger(__name__)

#: Source mounts on the server port, outside /api.
SOURCE_MOUNTS = ("/live", "/stations/{slug}/live")
#: The local networks ``[server] live_plain_http_lan`` admits plain HTTP from:
#: the private IPv4 ranges, IPv6 unique local addresses, and link-local.
_LAN_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "fc00::/7",
        "fe80::/10",
    )
)
#: Password checks run at once, across every address: each costs one
#: scrypt run (16 MiB, tens of milliseconds), so this bounds a flood.
_AUTH_CONCURRENCY = 2
#: Logins checked or waiting at once, across every address; past it, 503.
_AUTH_WAITING = 8
_CLOSE = (b"connection", b"close")


def source_path(slug: str) -> str:
    """The source mount for station *slug*."""
    return "/live" if slug == "default" else f"/stations/{slug}/live"


def _limit_key(address: str) -> str:
    """The rate-limit key for *address*: itself, or its /64 for IPv6."""
    parsed = ipaddress.ip_address(address)
    if isinstance(parsed, ipaddress.IPv6Address):
        return str(ipaddress.IPv6Network((parsed, 64), strict=False))
    return address


def _lan_plain_http(scope: Scope) -> bool:
    """Plain HTTP straight from a private (LAN) address, with no proxy in between."""
    if scope.get("scheme") != "http":
        return False
    names = {key.lower() for key, _value in scope.get("headers", ())}
    if any(name in names for name in (b"forwarded", b"x-real-ip")) or any(
        name.startswith(b"x-forwarded-") for name in names
    ):
        return False
    client = scope.get("client")
    try:
        address = ipaddress.ip_address(str(client[0])) if client else None
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address is not None and any(address in network for network in _LAN_NETWORKS)


class SourceEndpoint:
    """The Icecast source mount, as a plain ASGI app.

    Answers before reading the body, then reads the audio as it arrives
    and queues it for the station's decoder; nothing here waits on the
    decoder or the mix bus.
    """

    def __init__(self, allow_lan_plain_http: bool = False) -> None:
        self.allow_lan_plain_http = allow_lan_plain_http
        self.limiter = ListenerAuthLimiter()
        self._checking: set[str] = set()
        self._auth = asyncio.Semaphore(_AUTH_CONCURRENCY)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)

        async def reject(status: int, detail: str, headers: dict[str, str] | None = None) -> None:
            response = JSONResponse(
                {"detail": detail},
                status_code=status,
                headers={"Connection": "close", **(headers or {})},
            )
            await response(scope, receive, send)

        try:
            station = resolve_station(request)
        except HTTPException as exc:
            await reject(exc.status_code, str(exc.detail))
            return
        if not is_canonical_path(scope):
            await reject(400, "Use the mount's address exactly, without escapes")
            return
        if not station.stream_mode or station.player.bus is None:
            await reject(503, "This station does not stream, so it cannot take a live DJ")
            return
        if not (
            machine_transport_allowed(scope)
            or (self.allow_lan_plain_http and _lan_plain_http(scope))
        ):
            await reject(
                403, "Live source credentials need HTTPS, or a connection from this machine"
            )
            return
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        demuxer = SOURCE_FORMATS.get(content_type)
        if demuxer is None:
            await reject(415, "Send MP3, AAC or Ogg audio")
            return
        try:
            key = _limit_key(normalize_address(request.client.host if request.client else ""))
        except ValueError:
            await reject(400, "Invalid source address")
            return
        if len(self._checking) >= _AUTH_WAITING:
            await reject(
                503, "Too many source logins at once; try again shortly", {"Retry-After": "5"}
            )
            return
        if self.limiter.blocked(key) or key in self._checking:
            await reject(
                429, "Too many source logins; try again in a minute", {"Retry-After": "60"}
            )
            return
        authorization = _raw_header_values(scope, b"authorization")
        self._checking.add(key)
        try:
            async with self._auth:
                account = (
                    await asyncio.to_thread(station.live.credentials.authenticate, authorization[0])
                    if len(authorization) == 1
                    else None
                )
        finally:
            self._checking.discard(key)
        if account is None:
            self.limiter.failed(key)
            logger.info("Refused a live source login on %s from %s", source_path(station.slug), key)
            await reject(
                401,
                "Live DJ username or password not accepted",
                {"WWW-Authenticate": 'Basic realm="AutoDJ live", charset="UTF-8"'},
            )
            return
        username, name = account
        try:
            source = await asyncio.to_thread(station.live.claim, username, name, demuxer)
        except OSError:
            logger.exception("Could not start the live decoder")
            await reject(503, "The live decoder could not start")
            return
        if source is None:
            await reject(409, "Another live DJ is connected to this station")
            return
        logger.info("Live DJ %s connected to %s", name, source_path(station.slug))
        try:
            await self._stream(scope, receive, send, station, source)
        finally:
            await asyncio.to_thread(source.finish)
            await asyncio.to_thread(station.live.release, source)
            logger.info("Live DJ %s disconnected", name)

    async def _stream(
        self, scope: Scope, receive: Receive, send: Send, station: Any, source: Any
    ) -> None:
        """Answer the client, then queue its audio until it stops or breaks a limit."""
        headers = [(b"content-type", b"text/plain"), (b"cache-control", b"no-store"), _CLOSE]
        expects = any(
            value.strip().lower() == "100-continue"
            for value in _raw_header_values(scope, b"expect")
        )
        started = False
        if not expects:
            # As Icecast does: the answer comes first, then the audio.  A
            # client that asked to be told to go on is told so by the
            # server when the body is first read, and gets this at the end.
            await send({"type": "http.response.start", "status": 200, "headers": headers})
            started = True
        window: deque[tuple[float, int]] = deque()
        window_bytes = 0
        last_bytes = time.monotonic()
        underruns = 0
        reason = "the client hung up"
        try:
            while not station.stopped:
                try:
                    message = await asyncio.wait_for(receive(), 1.0)
                except TimeoutError:
                    message = None
                now = time.monotonic()
                if source.underruns != underruns:
                    # Counted by the mix bus, said here: no logging on the audio thread.
                    logger.warning(
                        "Live source audio on %s ran out %d time(s); silence played meanwhile",
                        source_path(station.slug),
                        source.underruns - underruns,
                    )
                    underruns = source.underruns
                if message is not None and message["type"] == "http.disconnect":
                    break
                chunk = message.get("body", b"") if message is not None else b""
                if chunk:
                    last_bytes = now
                    window.append((now, len(chunk)))
                    window_bytes += len(chunk)
                    while window and window[0][0] < now - 5:
                        window_bytes -= window.popleft()[1]
                    if window_bytes > MAX_SOURCE_BITRATE * 5 // 8 + SOURCE_BURST_BYTES:
                        reason = "it sent faster than 512 kbit/s"
                        break
                    try:
                        source.write(chunk)
                    except ValueError:
                        reason = "it reached the input limit"
                        break
                    except OverflowError:
                        reason = "the decoder fell behind"
                        break
                if now - last_bytes > SOURCE_IDLE_SECONDS:
                    reason = f"it sent nothing for {SOURCE_IDLE_SECONDS:.0f} seconds"
                    break
                if source.started and now - source.last_audio > SOURCE_SILENCE_SECONDS:
                    reason = f"it was silent on the air for {SOURCE_SILENCE_SECONDS:.0f} seconds"
                    break
                if not source.started and now - source.opened > SOURCE_STANDBY_SECONDS:
                    reason = "it sent only silence for 30 minutes"
                    break
                if source.process.poll() is not None:
                    reason = "the decoder could not read its audio"
                    break
                if message is not None and not message.get("more_body", False):
                    break
            logger.info("Live source on %s ended: %s", source_path(station.slug), reason)
        finally:
            if not started:
                await send({"type": "http.response.start", "status": 200, "headers": headers})
            await send({"type": "http.response.body", "body": b"", "more_body": False})


def install_live_api(router: StationRouter) -> None:
    """The operator's DJ accounts and talkover settings, for each station."""

    def view(station: Any) -> dict[str, Any]:
        available = bool(station.stream_mode and station.player.bus is not None)
        try:
            djs = station.live.credentials.accounts()
            problem = None
        except (OSError, ValueError) as exc:
            djs, problem = [], f"The live DJ file could not be read: {exc}"
        return {
            "available": available,
            "reason": None
            if available
            else "Live DJs need stream mode: the station plays on the server.",
            "problem": problem,
            **station.live_state(),
            "source_path": source_path(station.slug),
            "plain_http_lan": bool(getattr(station.cfg.server, "live_plain_http_lan", False)),
            "settings": station.live.settings.as_dict(),
            "djs": djs,
        }

    @router.get("/live")
    async def live_view(station: StationDep) -> dict[str, Any]:
        """The live DJ on the air, the DJ accounts and the talkover settings."""
        return await asyncio.to_thread(view, station)

    @router.put("/live/settings")
    async def live_settings(station: StationDep, body: Any = Body(...)) -> dict[str, float]:
        """Save the talkover return time and duck amount; they apply at once."""
        try:
            settings = TalkoverSettings.parse(body)
            await asyncio.to_thread(station.live.credentials.save_settings, settings)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except OSError as exc:
            raise HTTPException(status_code=503, detail="The settings could not be saved") from exc
        station.live.settings = settings
        return settings.as_dict()

    @router.post("/live/djs")
    async def live_dj_save(station: StationDep, body: Any = Body(...)) -> dict[str, Any]:
        """Add a DJ or change one; a password is never sent back."""
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Expected an object")
        try:
            return await asyncio.to_thread(
                station.live.credentials.save,
                body.get("username"),
                body.get("display_name"),
                body.get("password"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except OSError as exc:
            raise HTTPException(status_code=503, detail="The DJ could not be saved") from exc

    @router.delete("/live/djs/{username}")
    async def live_dj_delete(station: StationDep, username: str) -> dict[str, bool]:
        """Remove a DJ, disconnecting them if they are on the air."""
        try:
            removed = await asyncio.to_thread(station.live.credentials.delete, username)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="The DJ could not be removed") from exc
        if not removed:
            raise HTTPException(status_code=404, detail="No DJ has that username")
        await asyncio.to_thread(station.live.release_username, username)
        return {"ok": True}


def install_live_routes(
    app: FastAPI, policy: SecurityPolicy, *, allow_lan_plain_http: bool
) -> None:
    """Add the source mounts and the talkover socket."""
    endpoint = SourceEndpoint(allow_lan_plain_http)
    for path in SOURCE_MOUNTS:
        app.router.routes.append(Route(path, endpoint, methods=["SOURCE", "PUT"]))

    async def microphone(websocket: WebSocket) -> None:
        """Push-to-talk: Int16 mono PCM at the declared rate, while the talker holds."""
        hosts = _raw_header_values(websocket.scope, b"host")
        origins = _raw_header_values(websocket.scope, b"origin")
        if len(hosts) != 1 or not policy.host_allowed(hosts[0]):
            await websocket.close(code=4403)
            return
        if len(origins) != 1 or not policy.origin_allowed(origins[0]):
            await websocket.close(code=4403)
            return
        cookie = websocket.cookies.get(COOKIE_NAME)
        if policy.authentication_required and not policy.verify_session(cookie):
            await websocket.close(code=4401)
            return
        try:
            station = resolve_station(websocket)
        except HTTPException as exc:
            await websocket.close(code=4000 + exc.status_code)
            return
        rate_text = websocket.query_params.get("rate", "")
        if not rate_text.isdecimal() or int(rate_text) not in MIC_RATES:
            await websocket.close(code=1003)
            return
        rate = int(rate_text)
        if not station.stream_mode or station.player.bus is None:
            await websocket.close(code=4409)
            return
        mic = station.live.claim_mic()
        if mic is None:
            await websocket.close(code=4409)
            return
        resampler = MicResampler(rate)
        # Twice real time over a second, plus one message: room for jitter.
        budget = rate * 2 * 2 + MIC_MAX_MESSAGE_BYTES
        window_start = time.monotonic()
        window_bytes = 0
        try:
            await websocket.accept()
            while not station.stopped:
                if policy.authentication_required and not policy.verify_session(cookie):
                    await websocket.close(code=4401)
                    break
                try:
                    message = await asyncio.wait_for(websocket.receive(), MIC_IDLE_SECONDS)
                except TimeoutError:
                    await websocket.close(code=4000)
                    break
                if message["type"] == "websocket.disconnect":
                    break
                pcm = message.get("bytes")
                if (
                    not isinstance(pcm, bytes)
                    or not pcm
                    or len(pcm) > MIC_MAX_MESSAGE_BYTES
                    or len(pcm) % 2
                ):
                    await websocket.close(code=1003)
                    break
                now = time.monotonic()
                if now - window_start >= 1.0:
                    window_start, window_bytes = now, 0
                window_bytes += len(pcm)
                if window_bytes > budget:
                    await websocket.close(code=1009)
                    break
                mic.push_frames(resampler.convert(pcm))
        finally:
            station.live.release_mic(mic)

    app.add_api_websocket_route("/api/live/mic", microphone)
    app.add_api_websocket_route(STATION_PREFIX + "/live/mic", microphone)
