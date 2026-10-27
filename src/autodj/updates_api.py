"""Settings, Updates: the update check, its settings, restarting and updating AutoDJ.

``GET /api/updates`` answers with this installation's version and channel,
the update settings, the last check (checking again when it is stale),
whether AutoDJ can restart and update itself, and how the last update
went.  ``POST /api/updates/check`` checks now; ``PUT /api/updates/settings``
saves the settings; ``POST /api/server/restart`` restarts AutoDJ;
``POST /api/server/update`` asks the updater to update it, and
``POST /api/server/go-back`` to go back to the release it moved to
development from.  Every station shares them.

They are browser routes like every settings change: a paired browser
session when pairing is on, an allowed Origin for the writes, and machine
tokens never reach them.  Check, restart and their refusals are audited
under their own action names as well as by the request middleware.
Restart is refused when nothing would start AutoDJ again
(:func:`autodj.updates.restart_support`), and is limited to one a minute
and six an hour, counted in a file so a restart does not reset the count;
when that file cannot be written, restart is refused.  One update check
runs at a time: page loads during a slow check wait for it.

Update is refused unless :func:`autodj.updates.update_support` names an
updater and the last check found an update; Go back, unless the updater
recorded a usable backup when it moved a release to development.  They
take no options: the server writes ``request.json`` (an id, the action,
the channel it follows and where it listens; see
:mod:`autodj.update_contract`) and exits with
:data:`autodj.updates.UPDATE_EXIT_CODE`; the updater works out the target
itself (:mod:`autodj.checkout_update`).  Their own limiter file keeps them
to one a minute and six an hour, and only one update, go-back or restart
is pending.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool

from autodj import update_contract as contract
from autodj.security import emit_audit, new_request_id, seconds_text
from autodj.update_contract import HealthProbe
from autodj.updates import (
    RESTART_EXIT_CODE,
    UPDATE_EXIT_CODE,
    Installation,
    RestartSupport,
    UpdateCheck,
    UpdateChecker,
    UpdateSettings,
    UpdateSupport,
    channel_for,
    go_back_moved,
    restart_support,
    update_support,
)

logger = logging.getLogger(__name__)

#: Routes this module adds; each takes at most a small JSON body.
ROUTES = (
    "/api/updates/check",
    "/api/updates/settings",
    "/api/server/restart",
    "/api/server/update",
    "/api/server/go-back",
)
#: Seconds the page waits between two Check now presses.
CHECK_INTERVAL_SECONDS = 10.0
#: At most one restart (or update) a minute, and this many an hour.
RESTART_MIN_INTERVAL_SECONDS = 60.0
RESTART_MAX_PER_HOUR = 6
#: Seconds between the answer and the exit, so the answer reaches the page.
EXIT_DELAY_SECONDS = 1.0
#: How long the page shows the last update's result.
UPDATE_RESULT_SECONDS = 24 * 3600.0
#: Why Update is not offered when the updater could not check the new server.
NO_HEALTH_CHECK = (
    "AutoDJ cannot check its own health at the address it listens on, so it cannot update. "
    "Listen on an IP address, or a name this computer resolves, and allow a host name for it."
)
#: The answer while a restart or update is already on its way.
BUSY = "AutoDJ is already restarting or updating."


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

    def __init__(
        self,
        path: Callable[[], Path],
        clock: Callable[[], float] = time.time,
        *,
        action: str = "restart",
    ) -> None:
        """*path* gives the JSON file that keeps the recent times; *action* names them."""
        self._path = path
        self._clock = clock
        self._action = action
        self._lock = threading.Lock()

    def _recent(self, now: float) -> list[float]:
        """The restart times within the last hour; a missing file has none.

        Raises:
            LimitUnavailable: The file is there but cannot be read, or is not a
                list of times.  Counting it as empty would reset the limit.
        """
        path = self._path()
        unreadable = (
            f"AutoDJ cannot read {path}, so it did not {self._action}. Delete that file to "
            f"reset the {self._action} limit."
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
                logger.warning("Could not record the %s time: %s", self._action, exc)
                raise LimitUnavailable(
                    f"AutoDJ could not write {self._path()}, so it did not {self._action}. "
                    "Check that the index folder is writable."
                ) from exc
            return None


def last_update(folder: Path | None, now: float) -> dict[str, Any] | None:
    """The last update's result for the page, from ``result.json``, or ``None``.

    Only what the page shows: the id, the action, the outcome, the
    updater's sentence and the recovery commands.  A result older than
    UPDATE_RESULT_SECONDS is not shown.
    """
    record = contract.read_result(folder) if folder is not None else None
    if record is None:
        return None
    finished = record.get("finished_at")
    if not isinstance(finished, (int, float)) or now - finished > UPDATE_RESULT_SECONDS:
        return None
    return {
        "id": record.get("id"),
        "action": record["action"],
        "outcome": record["outcome"],
        "message": record["message"],
        "commands": list(record["commands"])[:20],
        "finished_at": finished,
    }


def go_back_offer(
    folder: Path | None, installation: Installation, channel: str, update: UpdateSupport
) -> dict[str, Any] | None:
    """Whether the page offers Go back, or ``None`` when there is nothing to say.

    Only on the stable channel, with the updater that recorded the backup
    when it moved that release to development, while the checkout still
    came from that release and has not reached a newer one
    (:func:`autodj.updates.go_back_moved`).  A recorded backup that is
    missing or unreadable, or a checkout moved by hand, is not offered,
    with one sentence saying why.
    """
    record = contract.read_go_back(folder) if folder is not None else None
    if record is None or channel != "stable" or installation.tag == record.tag:
        return None
    if not update.available:
        return None
    problem = contract.go_back_problem(record) or go_back_moved(
        installation, record.commit, record.version
    )
    return {
        "available": problem is None,
        "tag": record.tag,
        "version": record.version,
        "date": record.date,
        "message": problem or "",
    }


def updates_view(
    checker: UpdateChecker,
    support: RestartSupport,
    result: UpdateCheck | None,
    update: UpdateSupport | None = None,
    last: dict[str, Any] | None = None,
    folder: Path | None = None,
) -> dict[str, Any]:
    """Everything Settings, Updates shows, with *result* as the last check."""
    installation = checker.installation()
    settings = checker.settings()
    channel, source = channel_for(installation, settings)
    update = update or update_support(installation)
    return {
        "installation": installation.to_dict(),
        "settings": settings.to_dict(),
        "channel": {
            "name": channel,
            "source": source,
            "inferred": installation.inferred_channel,
        },
        "check": result.to_dict() if result is not None else None,
        "restart": support.to_dict(),
        "update": update.to_dict(),
        "go_back": go_back_offer(folder, installation, channel, update),
        "last_update": last,
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
    server, and ``app.state.health_probe`` to where it listens; without them
    (a test client, an embedding) restart and update are refused.  After a
    restart or update request, ``app.state.restart_requested`` is true and
    ``app.state.exit_status`` is the status ``serve`` exits with.
    ``app.state.update_dir`` names the update folder; by default the one
    the updater names, else ``updates`` in the index folder
    (:func:`autodj.update_contract.update_dir`).
    """
    checker = checker or UpdateChecker(index_dir)
    limiter = RestartLimiter(lambda: Path(index_dir()) / "logs" / "restart-requests.json")
    update_limiter = RestartLimiter(
        lambda: Path(index_dir()) / "logs" / "update-requests.json", action="update"
    )
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
    app.state.exit_status = None
    app.state.update_dir = None
    app.state.health_probe = None

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

    def claim(request: Request, audited: str, route: str) -> None:
        """Take the one pending restart or update, with no await before it is taken.

        Restart, Update and Go back share it, so only one is ever accepted;
        a request that is refused afterwards gives it back.
        """
        if app.state.restart_requested:
            audit(request, audited, "rejected", route, 409)
            raise HTTPException(status_code=409, detail=BUSY)
        app.state.restart_requested = True

    def current_support() -> RestartSupport:
        found = (support or restart_support)()
        if found.available and getattr(app.state, "server_exit", None) is None:
            return RestartSupport(None, "This server cannot stop itself, so it cannot restart.")
        return found

    def folder() -> Path | None:
        configured = getattr(app.state, "update_dir", None)
        if configured is not None:
            return Path(configured)
        return contract.update_dir(Path(index_dir()))

    def current_update_support() -> UpdateSupport:
        found = update_support(checker.installation())
        if not found.available:
            return found
        if getattr(app.state, "server_exit", None) is None:
            return UpdateSupport(
                None, "This server cannot stop itself, so it cannot update.", found.guide_url
            )
        probe = getattr(app.state, "health_probe", None)
        # The updater checks the probe again; one it would refuse is not offered.
        if not isinstance(probe, HealthProbe) or contract.parse_health(probe.to_dict()) is None:
            return UpdateSupport(None, NO_HEALTH_CHECK, found.guide_url)
        # This server's own launcher passes its process id: the server is not
        # always its direct child (a virtual environment's python.exe on Windows).
        others = contract.other_servers(folder(), contract.own_launcher())
        if others:
            return UpdateSupport(None, contract.other_servers_reason(others), found.guide_url)
        return found

    def view(found: RestartSupport, result: UpdateCheck | None) -> dict[str, Any]:
        where = folder()
        return updates_view(
            checker,
            found,
            result,
            current_update_support(),
            last_update(where, time.time()),
            where,
        )

    @app.get("/api/updates")
    async def api_updates() -> dict[str, Any]:
        """Version, channel, settings, the last check, Restart and Update, and the last update."""
        found = await asyncio.to_thread(current_support)
        result = await shared_check(force=False)
        return await asyncio.to_thread(view, found, result)

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
        return await asyncio.to_thread(view, found, result)

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
        return await asyncio.to_thread(view, found, cached)

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
        claim(request, "server-restart", route)
        accepted = False
        try:
            try:
                wait = await asyncio.to_thread(limiter.take)
            except LimitUnavailable as exc:
                audit(request, "server-restart", "failed", route, 503)
                raise HTTPException(status_code=503, detail=str(exc)) from None
            if wait is not None:
                audit(request, "server-restart", "rejected", route, 429)
                return JSONResponse(
                    {
                        "detail": f"AutoDJ restarted a moment ago. Try again in {seconds_text(wait)}."
                    },
                    status_code=429,
                    headers={"Retry-After": str(wait)},
                )
            audit(request, "server-restart", "accepted", route, 202)
            logger.warning("Restart requested from the web page; AutoDJ is restarting.")
            background.add_task(schedule_exit, app)
            accepted = True
            return {"restarting": True, "message": found.message}
        finally:
            if not accepted:
                app.state.restart_requested = False

    async def ask_updater(
        request: Request, background: BackgroundTasks, action: str, route: str, audited: str
    ) -> Any:
        """Write ``request.json`` for *action* and stop, so the updater takes over."""
        browser_only(request, audited, route)
        found = await asyncio.to_thread(current_update_support)
        if not found.available:
            audit(request, audited, "rejected", route, 409)
            raise HTTPException(status_code=409, detail=found.message)
        claim(request, audited, route)
        accepted = False
        try:
            result = await shared_check(force=False)
            installation = await asyncio.to_thread(checker.installation)
            where = await asyncio.to_thread(folder)
            if action == "update":
                if result.status != "available" or result.target is None:
                    audit(request, audited, "rejected", route, 409)
                    raise HTTPException(
                        status_code=409, detail=f"No update is available. {result.message}".strip()
                    )
                channel, target = result.channel, result.target
            else:
                channel = channel_for(installation, checker.settings())[0]
                offer = await asyncio.to_thread(go_back_offer, where, installation, channel, found)
                if offer is None or not offer["available"]:
                    audit(request, audited, "rejected", route, 409)
                    detail = (offer or {}).get("message") or "There is no release to go back to."
                    raise HTTPException(status_code=409, detail=detail)
                target = offer["tag"]
            probe = app.state.health_probe
            if where is None or not isinstance(probe, HealthProbe):
                audit(request, audited, "rejected", route, 409)
                raise HTTPException(status_code=409, detail="This server cannot update itself.")
            try:
                wait = await asyncio.to_thread(update_limiter.take)
            except LimitUnavailable as exc:
                audit(request, audited, "failed", route, 503)
                raise HTTPException(status_code=503, detail=str(exc)) from None
            if wait is not None:
                audit(request, audited, "rejected", route, 429)
                return JSONResponse(
                    {"detail": f"AutoDJ updated a moment ago. Try again in {seconds_text(wait)}."},
                    status_code=429,
                    headers={"Retry-After": str(wait)},
                )
            update_id = secrets.token_hex(8)
            record = {
                "id": update_id,
                "action": action,
                "channel": channel,
                "requested_at": time.time(),
                # For the record only: the updater works out the target itself.
                "target": target,
                "from": {"version": installation.version, "commit": installation.commit},
                "health": probe.to_dict(),
            }
            path = where / contract.REQUEST
            try:
                await asyncio.to_thread(contract.write, path, record)
            except OSError as exc:
                logger.warning("Could not write the update request %s: %s", path, exc)
                audit(request, audited, "failed", route, 503)
                raise HTTPException(
                    status_code=503, detail=f"AutoDJ could not write {path}, so it did not update."
                ) from None
            audit(request, audited, "accepted", route, 202)
            logger.warning("%s requested from the web page; AutoDJ is stopping.", action)
            background.add_task(schedule_exit, app, None, UPDATE_EXIT_CODE)
            accepted = True
            return {"updating": True, "id": update_id, "action": action, "target": target}
        finally:
            if not accepted:
                app.state.restart_requested = False

    @app.post("/api/server/update", status_code=202)
    async def api_server_update(
        request: Request, background: BackgroundTasks, _body: EmptyBody | None = None
    ) -> Any:
        """Stop AutoDJ so its updater updates it and starts it again.

        The target comes from the last check, never from the request; the
        updater checks again before it changes anything.
        """
        return await ask_updater(
            request, background, "update", "/api/server/update", "server-update"
        )

    @app.post("/api/server/go-back", status_code=202)
    async def api_server_go_back(
        request: Request, background: BackgroundTasks, _body: EmptyBody | None = None
    ) -> Any:
        """Stop AutoDJ so its updater goes back to the release it moved from, with its backup."""
        return await ask_updater(
            request, background, "go-back", "/api/server/go-back", "server-go-back"
        )

    return checker


async def schedule_exit(
    app: FastAPI, delay: float | None = None, status: int = RESTART_EXIT_CODE
) -> None:
    """Stop the server shortly after the answer has gone; ``serve`` then exits with *status*."""
    app.state.restart_requested = True
    app.state.exit_status = status
    loop = asyncio.get_running_loop()
    loop.call_later(EXIT_DELAY_SECONDS if delay is None else delay, app.state.server_exit)
