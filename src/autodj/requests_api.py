"""The paired routes the operator manages listener requests with (#49).

Each answers at ``/api/requests...`` for the default station and at
``/api/stations/<slug>/requests...`` by name, like every station route.
A station without ``[requests] enabled`` lists ``{"enabled": false}`` and
answers 404 to the actions.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException

from autodj.listener_requests import RequestDesk
from autodj.station_routes import StationDep, StationRouter
from autodj.stations import StationRuntime


def _desk(station: StationRuntime) -> RequestDesk:
    if station.requests is None:
        raise HTTPException(status_code=404, detail="Listener requests are off for this station")
    return station.requests


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
        """Waiting requests with where each would fit, and the recent decisions."""
        if station.requests is None:
            return {"enabled": False, "pending": [], "recent": []}
        return await asyncio.to_thread(station.requests.snapshot)

    @router.post("/requests/{request_id}/approve")
    async def approve_request(station: StationDep, request_id: str) -> dict[str, Any]:
        """Queue a waiting request where it fits best; 409 says which rule stops it."""
        return await _decide(station, "approve", request_id)

    @router.post("/requests/{request_id}/play-next")
    async def play_request_next(station: StationDep, request_id: str) -> dict[str, Any]:
        """Queue a waiting request first, whatever fits; DMCA limits still hold."""
        return await _decide(station, "play-next", request_id)

    @router.post("/requests/{request_id}/reject")
    async def reject_request(station: StationDep, request_id: str) -> dict[str, Any]:
        """Turn a waiting request down."""
        return await _decide(station, "reject", request_id)
