"""The paired routes the operator manages listener requests with (#49).

Each answers at ``/api/requests...`` for the default station and at
``/api/stations/<slug>/requests...`` by name, like every station route.
A station that is not taking requests lists ``{"enabled": false}`` with
its settings and answers 404 to the actions.  ``POST requests/settings``
turns requests on or off and changes how they are approved and placed;
it is saved with the station's other settings in ``web_state.json``,
which wins over ``[requests]`` in config.toml from then on.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException

from autodj.listener_requests import RequestDesk, request_settings
from autodj.settings_bodies import RequestSettingsBody
from autodj.station_routes import StationDep, StationRouter, request_desk
from autodj.stations import StationRuntime

logger = logging.getLogger(__name__)


def taking_requests(station: StationRuntime) -> RequestDesk | None:
    """The station's request desk while it takes requests, or ``None``.

    A desk stays built once requests have been on, so turning them off
    and on again keeps the waiting requests; while they are off it serves
    nobody.
    """
    desk = station.requests
    if desk is None or desk.cfg.enabled is not True:
        return None
    return desk


def _desk(station: StationRuntime) -> RequestDesk:
    desk = taking_requests(station)
    if desk is None:
        raise HTTPException(status_code=404, detail="Listener requests are off for this station")
    return desk


def _view(station: StationRuntime) -> dict[str, Any]:
    """The operator's view (blocking: it reads the request book)."""
    desk = taking_requests(station)
    if desk is None:
        return {**request_settings(station.cfg.requests), "today": 0, "pending": [], "recent": []}
    return desk.snapshot()


async def _decide(station: StationRuntime, action: str, request_id: str) -> dict[str, Any]:
    desk = _desk(station)
    call = {"approve": desk.approve, "reject": desk.reject, "play-next": desk.play_next}[action]
    try:
        request = await asyncio.to_thread(call, request_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such request is waiting") from None
    except LookupError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"request": request.public(), **await asyncio.to_thread(desk.snapshot)}


def install_requests(router: StationRouter) -> None:
    """Register the paired listener request routes."""

    @router.get("/requests")
    async def list_requests(station: StationDep) -> dict[str, Any]:
        """The settings, the waiting requests with where each would fit, and the recent decisions."""
        return await asyncio.to_thread(_view, station)

    @router.post("/requests/settings")
    async def save_request_settings(
        station: StationDep, body: RequestSettingsBody
    ) -> dict[str, Any]:
        """Turn requests on or off and change how they are approved and placed; saved at once.

        Turning them off closes the public request page at once: it says
        requests are closed, and a request sent from a page opened before
        is turned away with the same words.
        """
        cfg = station.cfg.requests
        was_on = cfg.enabled is True
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(cfg, field, float(value) if field == "bridge_cost_threshold" else value)
        if cfg.enabled is True and station.requests is None:
            try:
                desk = await asyncio.to_thread(request_desk, station)
            except Exception:
                cfg.enabled = was_on
                logger.exception("Could not open listener requests")
                raise HTTPException(
                    status_code=500, detail="Could not open listener requests; see the log"
                ) from None
            if station.requests is None:
                station.requests = desk
                # A show that starts ahead of queued requests updates their notes.
                station.show_runner.on_started = desk.show_started
            else:  # another save built one first
                await asyncio.to_thread(desk.close)
        if (cfg.enabled is True) is not was_on:
            logger.info("Listener requests turned %s in Settings", "on" if cfg.enabled else "off")
        await asyncio.to_thread(station.bridge.save_persistent_state)
        return await asyncio.to_thread(_view, station)

    @router.post("/requests/{request_id}/approve")
    async def approve_request(station: StationDep, request_id: str) -> dict[str, Any]:
        """Queue a waiting request where it fits best; 409 says which rule stops it."""
        return await _decide(station, "approve", request_id)

    @router.post("/requests/{request_id}/play-next")
    async def play_request_next(station: StationDep, request_id: str) -> dict[str, Any]:
        """Queue a waiting request first; DMCA and clockwheel limits still hold."""
        return await _decide(station, "play-next", request_id)

    @router.post("/requests/{request_id}/reject")
    async def reject_request(station: StationDep, request_id: str) -> dict[str, Any]:
        """Turn a waiting request down."""
        return await _decide(station, "reject", request_id)
