"""Settings, Updates: the update check, its settings, and restarting AutoDJ.

``GET /api/updates`` answers with this installation's version and channel,
the update settings, the last check (checking again when it is stale) and
whether AutoDJ can restart itself.  ``POST /api/updates/check`` checks now;
``PUT /api/updates/settings`` saves the settings; ``POST /api/server/restart``
restarts AutoDJ.  Every station shares them.

They are browser routes like every settings change: a paired browser
session when pairing is on, an allowed Origin for the writes, and machine
tokens never reach them.  Check, restart and their refusals are audited
under their own action names as well as by the request middleware.
Restart is refused when nothing would start AutoDJ again
(:func:`autodj.updates.restart_support`), and is limited to one a minute
and six an hour, counted in a file so a restart does not reset the count;
when that file cannot be written, restart is refused.  One update check
runs at a time: page loads during a slow check wait for it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool

from autodj.security import emit_audit, new_request_id, seconds_text
from autodj.updates import (
    RestartSupport,
    UpdateCheck,
    UpdateChecker,
    UpdateSettings,
    channel_for,
    restart_support,
    update_commands,
)

logger = logging.getLogger(__name__)

#: Routes this module adds; each takes at most a small JSON body.
ROUTES = ("/api/updates/check", "/api/updates/settings", "/api/server/restart")
#: Seconds the page waits between two Check now presses.
CHECK_INTERVAL_SECONDS = 10.0
#: At most one restart (or update) a minute, and this many an hour.
RESTART_MIN_INTERVAL_SECONDS = 60.0
RESTART_MAX_PER_HOUR = 6
#: Seconds between the answer and the exit, so the answer reaches the page.
EXIT_DELAY_SECONDS = 1.0


class SettingsBody(BaseModel):
    """Whether to check for updates, and which channel to follow."""

    model_config = ConfigDict(extra="forbid")

    check: StrictBool
    channel: Literal["auto", "stable", "development"]


class EmptyBody(BaseModel):
    """A POST that takes no options."""

    model_config = ConfigDict(extra="forbid")


class LimitUnavailable(Exception):
    """The restart count could not be saved, so a restart is refused."""


class RestartLimiter:
    """Counts accepted restarts in a file that outlives the process.

    It fails closed: when the count cannot be saved, the restart is refused,
    since a restart would otherwise reset the limit.
    """

    def __init__(self, path: Callable[[], Path], clock: Callable[[], float] = time.time) -> None:
        """*path* gives the JSON file that keeps the recent times."""
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()

    def _recent(self, now: float) -> list[float]:
        """The restart times within the last hour; a missing file has none.

        Raises:
            LimitUnavailable: The file is there but cannot be read, or is not a
                list of times.  Counting it as empty would reset the limit.
        """
        path = self._path()
        unreadable = (
            f"AutoDJ cannot read {path}, so it did not restart. Delete that file to reset "
            "the restart limit."
        )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise LimitUnavailable(unreadable) from exc
        if not isinstance(raw, list) or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in raw
        ):
            raise LimitUnavailable(unreadable)
        # A time ahead of the clock (the clock was set back) counts as now, so
        # setting the clock back cannot clear the limit.
        return sorted(
            min(float(value), now)
            for value in raw[-RESTART_MAX_PER_HOUR:]
            if now - 3600.0 < min(float(value), now)
        )

    def take(self) -> int | None:
        """Count one restart now; or, when over the limit, the seconds to wait instead.

        Raises:
            LimitUnavailable: The count could not be read or saved; its
                message says which file and what to do.
        """
        from autodj.fsutil import atomic_write

        with self._lock:
            now = self._clock()
            recent = self._recent(now)
            wait = 0.0
            if recent:
                wait = max(wait, recent[-1] + RESTART_MIN_INTERVAL_SECONDS - now)
            if len(recent) >= RESTART_MAX_PER_HOUR:
                wait = max(wait, recent[-RESTART_MAX_PER_HOUR] + 3600.0 - now)
            if wait > 0:
                return max(1, int(wait + 0.999))
            recent.append(now)
            try:
                atomic_write(self._path(), json.dumps(recent[-RESTART_MAX_PER_HOUR:]))
            except OSError as exc:
                logger.warning("Could not record the restart time: %s", exc)
                raise LimitUnavailable(
                    f"AutoDJ could not write {self._path()}, so it did not restart. Check "
                    "that the index folder is writable."
                ) from exc
            return None


def updates_view(
    checker: UpdateChecker, support: RestartSupport, result: UpdateCheck | None
) -> dict[str, Any]:
    """Everything Settings, Updates shows, with *result* as the last check."""
    installation = checker.installation()
    settings = checker.settings()
    channel, source = channel_for(installation, settings)
    return {
        "installation": installation.to_dict(),
        "settings": settings.to_dict(),
        "channel": {
            "name": channel,
            "source": source,
            "inferred": installation.inferred_channel,
        },
        "check": result.to_dict() if result is not None else None,
        "commands": update_commands(installation, result) if result is not None else [],
        "restart": support.to_dict(),
    }


def install_updates(
    app: FastAPI,
    index_dir: Callable[[], Path],
    *,
    checker: UpdateChecker | None = None,
    support: Callable[[], RestartSupport] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> UpdateChecker:
    """Register the update and restart routes; returns the checker they share.

    ``serve`` sets ``app.state.server_exit`` to a callable that stops the
    server; without it (a test client, an embedding) restart is refused.
    After a restart request, ``app.state.restart_requested`` is true.
    """
    checker = checker or UpdateChecker(index_dir)
    limiter = RestartLimiter(lambda: Path(index_dir()) / "logs" / "restart-requests.json")
    last_check: list[float] = []
    # The one check in flight.  Page loads while it runs await it instead of
    # each holding a worker thread for as long as git or GitHub takes.
    in_flight: dict[str, asyncio.Future[UpdateCheck]] = {}

    async def shared_check(*, force: bool) -> UpdateCheck:
        if not force:
            found = await asyncio.to_thread(checker.fresh)
            if found is not None:
                return found
        task = in_flight.get("check")
        if task is None or task.done():
            task = asyncio.ensure_future(asyncio.to_thread(checker.result, force=force))
            in_flight["check"] = task
        return await asyncio.shield(task)

    app.state.update_checker = checker
    app.state.restart_requested = False

    def audit(request: Request, action: str, outcome: str, route: str, code: int) -> None:
        emit_audit(
            getattr(request.state, "request_id", None) or new_request_id(),
            action,
            outcome,
            method=request.method,
            route=route,
            status=code,
            level=logging.INFO if code < 400 else logging.WARNING,
        )

    def browser_only(request: Request, action: str, route: str) -> None:
        if getattr(request.state, "machine_token", None) is not None:
            # Not reachable: machine tokens work only under their own prefixes.
            audit(request, action, "rejected", route, 403)
            raise HTTPException(status_code=403, detail="Only a paired browser can do this")

    def current_support() -> RestartSupport:
        found = (support or restart_support)()
        if found.available and getattr(app.state, "server_exit", None) is None:
            return RestartSupport(None, "This server cannot stop itself, so it cannot restart.")
        return found

    @app.get("/api/updates")
    async def api_updates() -> dict[str, Any]:
        """Version, channel, settings, the last check and whether Restart is possible."""
        found = await asyncio.to_thread(current_support)
        result = await shared_check(force=False)
        return await asyncio.to_thread(updates_view, checker, found, result)

    @app.post("/api/updates/check")
    async def api_updates_check(request: Request, _body: EmptyBody | None = None) -> Any:
        """Check for an update now, past the cache."""
        route = "/api/updates/check"
        browser_only(request, "update-check", route)
        now = clock()
        if last_check and now - last_check[0] < CHECK_INTERVAL_SECONDS:
            wait = int(CHECK_INTERVAL_SECONDS - (now - last_check[0]) + 0.999)
            audit(request, "update-check", "rejected", route, 429)
            return JSONResponse(
                {"detail": f"AutoDJ checked a moment ago. Try again in {seconds_text(wait)}."},
                status_code=429,
                headers={"Retry-After": str(wait)},
            )
        last_check[:] = [now]
        result = await shared_check(force=True)
        audit(
            request,
            "update-check",
            "failed" if result.status == "unavailable" else "success",
            route,
            200,
        )
        found = await asyncio.to_thread(current_support)
        return await asyncio.to_thread(updates_view, checker, found, result)

    @app.put("/api/updates/settings")
    async def api_updates_settings(request: Request, body: SettingsBody) -> dict[str, Any]:
        """Turn the update check on or off and choose the channel."""
        browser_only(request, "update-settings", "/api/updates/settings")
        settings = UpdateSettings(
            check=body.check, channel=None if body.channel == "auto" else body.channel
        )
        try:
            await asyncio.to_thread(checker.save, settings)
        except OSError as exc:
            logger.warning("Could not save the update settings: %s", exc)
            raise HTTPException(
                status_code=500, detail="Could not save the update settings."
            ) from None
        found = await asyncio.to_thread(current_support)
        cached = await asyncio.to_thread(checker.cached)
        return await asyncio.to_thread(updates_view, checker, found, cached)

    @app.post("/api/server/restart", status_code=202)
    async def api_server_restart(
        request: Request, background: BackgroundTasks, _body: EmptyBody | None = None
    ) -> Any:
        """Stop AutoDJ so its launcher, service manager or container starts it again."""
        route = "/api/server/restart"
        browser_only(request, "server-restart", route)
        found = await asyncio.to_thread(current_support)
        if not found.available:
            audit(request, "server-restart", "rejected", route, 409)
            raise HTTPException(status_code=409, detail=found.message)
        try:
            wait = await asyncio.to_thread(limiter.take)
        except LimitUnavailable as exc:
            audit(request, "server-restart", "failed", route, 503)
            raise HTTPException(status_code=503, detail=str(exc)) from None
        if wait is not None:
            audit(request, "server-restart", "rejected", route, 429)
            return JSONResponse(
                {"detail": f"AutoDJ restarted a moment ago. Try again in {seconds_text(wait)}."},
                status_code=429,
                headers={"Retry-After": str(wait)},
            )
        audit(request, "server-restart", "accepted", route, 202)
        logger.warning("Restart requested from the web page; AutoDJ is restarting.")
        background.add_task(schedule_exit, app)
        return {"restarting": True, "message": found.message}

    return checker


async def schedule_exit(app: FastAPI, delay: float | None = None) -> None:
    """Stop the server shortly after the answer has gone; ``serve`` then exits to restart."""
    app.state.restart_requested = True
    loop = asyncio.get_running_loop()
    loop.call_later(EXIT_DELAY_SECONDS if delay is None else delay, app.state.server_exit)
