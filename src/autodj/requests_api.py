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
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, StrictStr

from autodj.listener_requests import RequestDesk, request_settings
from autodj.live_queue import VoiceQueueConflict
from autodj.settings_bodies import PAGE_SAVED_REQUESTS, RequestSettingsBody
from autodj.station_routes import SavedStationDep, StationDep, StationRouter, request_desk
from autodj.stations import StationRuntime

logger = logging.getLogger(__name__)


class ListenerQueueActionBody(BaseModel):
    """Optional operator choice for an upcoming voice-track conflict."""

    voice_action: Literal["keep", "discard"] | None = None
    voice_revision: StrictStr | None = None


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


async def _decide(
    station: StationRuntime,
    action: str,
    request_id: str,
    body: ListenerQueueActionBody | None = None,
) -> dict[str, Any]:
    desk = _desk(station)
    try:
        if action in {"approve", "play-next"}:
            call = desk.approve if action == "approve" else desk.play_next
            request = await asyncio.to_thread(
                call,
                request_id,
                voice_action=body.voice_action if body else None,
                voice_revision=body.voice_revision if body else None,
            )
        else:
            request = await asyncio.to_thread(desk.reject, request_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such request is waiting") from None
    except LookupError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except VoiceQueueConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    return {"request": request.public(), **await asyncio.to_thread(desk.snapshot)}


def install_requests(router: StationRouter) -> None:
    """Register the paired listener request routes."""

    @router.get("/requests")
    async def list_requests(station: SavedStationDep) -> dict[str, Any]:
        """The settings, the waiting requests with where each would fit, and the recent decisions."""
        return await asyncio.to_thread(_view, station)

    @router.post("/requests/settings")
    async def save_request_settings(
        station: SavedStationDep, body: RequestSettingsBody
    ) -> dict[str, Any]:
        """Turn requests on or off and change how they are approved and placed; saved at once.

        Turning them off closes the public request page at once: it says
        requests are closed, and a request sent from a page opened before
        is turned away with the same words.
        """
        cfg = station.cfg.requests
        was_on = cfg.enabled is True
        changes = body.model_dump(exclude_none=True)
        # Put back as they were if the request desk cannot be opened.
        before = {field: getattr(cfg, field) for field in changes}

        def put_back() -> None:
            for field, value in before.items():
                setattr(cfg, field, value)

        for field, value in changes.items():
            setattr(cfg, field, float(value) if field == "bridge_cost_threshold" else value)
        if not station.stopped and cfg.enabled is True and station.requests is None:
            try:
                desk = await asyncio.to_thread(request_desk, station)
            except Exception:
                put_back()
                logger.exception("Could not open listener requests")
                raise HTTPException(
                    status_code=500, detail="Could not open listener requests; see the log"
                ) from None
            if station.stopped:
                # Stopped while the desk was being built: its stop has already
                # closed what it had, so this desk would never be closed.
                await asyncio.to_thread(desk.close)
                put_back()
                raise HTTPException(status_code=409, detail="That station is stopped")
            if station.requests is None:
                station.requests = desk
                # A show that starts ahead of queued requests updates their notes.
                station.show_runner.on_started = desk.show_started
            else:  # another save built one first
                await asyncio.to_thread(desk.close)
        if body.model_fields_set & {"enabled", "approval"}:
            # From now on these are the card's, not config.toml's, for this station.
            station.bridge.page_saved.add(PAGE_SAVED_REQUESTS)
        if (cfg.enabled is True) is not was_on:
            logger.info("Listener requests turned %s in Settings", "on" if cfg.enabled else "off")
        await asyncio.to_thread(station.bridge.save_persistent_state)
        return await asyncio.to_thread(_view, station)

    @router.post("/requests/{request_id}/approve")
    async def approve_request(
        station: StationDep,
        request_id: str,
        body: ListenerQueueActionBody | None = None,
    ) -> dict[str, Any]:
        """Queue a waiting request where it fits best; 409 says which rule stops it."""
        return await _decide(station, "approve", request_id, body)

    @router.post("/requests/{request_id}/play-next")
    async def play_request_next(
        station: StationDep,
        request_id: str,
        body: ListenerQueueActionBody | None = None,
    ) -> dict[str, Any]:
        """Queue a waiting request first; DMCA and clockwheel limits still hold."""
        return await _decide(station, "play-next", request_id, body)

    @router.post("/requests/{request_id}/reject")
    async def reject_request(station: StationDep, request_id: str) -> dict[str, Any]:
        """Turn a waiting request down."""
        return await _decide(station, "reject", request_id)
