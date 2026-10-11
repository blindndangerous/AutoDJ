"""Paired schedule routes and the built-in scheduler actions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

from autodj.scheduler import (
    Action,
    ActionField,
    EventError,
    EventSpec,
    ScheduledEvent,
    Scheduler,
    describe_repeat,
    field_error,
)
from autodj.settings_bodies import PlaybackSettingsBody

# Runs the Schedule panel lists, newest first.
RUNS_SHOWN = 50


class EnableBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


def _rejected(field: str, message: str) -> JSONResponse:
    """A 400 whose ``field`` lets the page focus the field at fault."""
    return JSONResponse(status_code=400, content={"detail": message, "field": field})


def _builtin_actions(
    bridge: Any,
    apply_profile: Callable[[str], Awaitable[Any]],
    profile_names: Callable[[], Sequence[str]],
    liner_names: Callable[[], Sequence[str]],
) -> list[Action]:
    """Actions whose backing features exist: profile, preset, picking, liner, stream, discovery."""
    cfg = bridge.player._cfg

    def preset_names() -> list[str]:
        from autodj.presets import BUILTIN_PRESETS

        return sorted(set(BUILTIN_PRESETS) | set(cfg.presets or {}))

    async def profile(params: dict[str, str]) -> None:
        try:
            await apply_profile(params["name"])
        except HTTPException as exc:
            raise ValueError(str(exc.detail)) from exc

    async def preset(params: dict[str, str]) -> None:
        from autodj.presets import get_preset

        get_preset(params["name"], cfg.presets)
        bridge.set_preset(params["name"])
        bridge.save_persistent_state()

    def clock_configured() -> bool:
        library = bridge.library
        return library is not None and bool(library.catalog.get_setting("clock", {}).get("slots"))

    async def picking(params: dict[str, str]) -> None:
        if params["mode"] == "clock" and not await asyncio.to_thread(clock_configured):
            raise ValueError("No clockwheel is configured; import one in Library tools")
        bridge.set_playback_settings(
            PlaybackSettingsBody.model_validate({"pick_mode": params["mode"]})
        )
        bridge.save_persistent_state()

    async def liner(params: dict[str, str]) -> None:
        worker = bridge.liner_scheduler
        if worker is None:
            raise ValueError("Scheduled liners need server audio or stream mode")
        if bridge.station is not None and bridge.station.state != "playing":
            raise ValueError("The stream must be playing to air a liner")
        if await asyncio.to_thread(worker.fire, params["name"]) is None:
            raise ValueError(f"Liner {params['name']} could not be played")

    async def stream(params: dict[str, str]) -> None:
        station = bridge.station
        if station is None:
            raise ValueError("Stream control needs stream mode")
        playing = {"start": True, "stop": False, "auto": None}[params["state"]]
        await asyncio.to_thread(station.set_scheduled_playing, playing)
        if playing and station.state == "idle":
            raise ValueError("The stream could not start: the library has no tracks")

    async def discovery(params: dict[str, str]) -> None:
        enabled = params["state"] == "on"
        if enabled and bridge.player._discovery_every is None:
            raise ValueError("Set a discovery rate in Song picking first")
        bridge.player._state.discovery_enabled = enabled
        bridge.save_persistent_state()

    return [
        Action(
            "profile",
            "Switch profile",
            profile,
            (ActionField("name", "Profile", names=profile_names),),
        ),
        Action(
            "preset",
            "Switch preset",
            preset,
            (ActionField("name", "Preset", names=preset_names),),
        ),
        Action(
            "pick_mode",
            "Change song picking",
            picking,
            (
                ActionField(
                    "mode",
                    "Song picking",
                    choices=(
                        ("clock", "Clockwheel"),
                        ("similarity", "Similar songs"),
                        ("random", "Random"),
                        ("least_recent", "Least recently played"),
                    ),
                ),
            ),
        ),
        Action(
            "liner",
            "Play a liner or station ID",
            liner,
            (ActionField("name", "Liner", names=liner_names),),
            interrupt="before",
        ),
        Action(
            "stream",
            "Start or stop the stream",
            stream,
            (
                ActionField(
                    "state",
                    "Stream",
                    choices=(
                        ("start", "Start, and keep playing without listeners"),
                        ("stop", "Stop, and stay off the air"),
                        ("auto", "Automatic, play while someone listens"),
                    ),
                ),
            ),
            interrupt="none",
        ),
        Action(
            "discovery",
            "Turn discovery on or off",
            discovery,
            (ActionField("state", "Discovery", choices=(("on", "On"), ("off", "Off"))),),
        ),
    ]


def install_scheduler(
    app: FastAPI,
    bridge: Any,
    *,
    timezone: str,
    apply_profile: Callable[[str], Awaitable[Any]],
    profile_names: Callable[[], Sequence[str]],
    liner_names: Callable[[], Sequence[str]],
    publish: Callable[[dict[str, Any]], Awaitable[None]],
) -> Scheduler:
    """Create the scheduler with the built-in actions and add its paired routes.

    Later features register more actions on the returned scheduler before
    the app starts; the app's lifespan calls :meth:`Scheduler.start`.
    """
    cfg = bridge.player._cfg
    scheduler = Scheduler(
        Path(cfg.index.active_dir) / "schedule.json",
        timezone,
        publish,
        interrupt=lambda: bridge.skip(),
        song_playing=lambda: bridge.player._state.current_track is not None,
    )
    for action in _builtin_actions(bridge, apply_profile, profile_names, liner_names):
        scheduler.register(action)

    def event_view(event: ScheduledEvent) -> dict[str, Any]:
        return {
            **event.model_dump(mode="json"),
            "repeat_text": describe_repeat(event),
            "action_text": scheduler.summary(event),
        }

    def upcoming_view(count: int) -> list[dict[str, Any]]:
        return [
            {
                "id": event.id,
                "name": event.name,
                "time": when.isoformat(),
                "repeat_text": describe_repeat(event),
                "action_text": scheduler.summary(event),
            }
            for when, event in scheduler.upcoming(count)
        ]

    def parse(body: dict[str, Any]) -> EventSpec:
        try:
            return EventSpec.model_validate(body)
        except ValidationError as exc:
            raise EventError(*field_error(exc)) from exc

    def unknown() -> HTTPException:
        return HTTPException(status_code=404, detail="That event no longer exists")

    @app.get("/api/schedule")
    async def schedule_list() -> dict[str, Any]:
        """Return the actions, events, next runs and recent runs."""
        actions = await asyncio.to_thread(
            lambda: [action.metadata() for action in scheduler.actions.values()]
        )
        return {
            "timezone": scheduler.timezone,
            "actions": actions,
            "events": [event_view(event) for event in scheduler.events.values()],
            "upcoming": upcoming_view(5),
            "runs": [run.model_dump(mode="json") for run in scheduler.runs[::-1][:RUNS_SHOWN]],
        }

    @app.get("/api/schedule/upcoming")
    async def schedule_upcoming(count: int = Query(default=5, ge=1, le=50)) -> dict[str, Any]:
        """Return the next *count* runs, soonest first."""
        return {"timezone": scheduler.timezone, "upcoming": upcoming_view(count)}

    @app.post("/api/schedule", status_code=201, response_model=None)
    async def schedule_create(body: dict[str, Any] = Body(...)) -> dict[str, Any] | JSONResponse:
        """Add an event."""
        try:
            event = await scheduler.add(parse(body))
        except EventError as exc:
            return _rejected(exc.field, str(exc))
        return event_view(event)

    @app.put("/api/schedule/{event_id}", response_model=None)
    async def schedule_update(
        event_id: str, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any] | JSONResponse:
        """Replace an event."""
        try:
            event = await scheduler.replace(event_id, parse(body))
        except KeyError:
            raise unknown() from None
        except EventError as exc:
            return _rejected(exc.field, str(exc))
        return event_view(event)

    @app.delete("/api/schedule/{event_id}")
    async def schedule_delete(event_id: str) -> dict[str, str]:
        """Delete an event."""
        try:
            event = await scheduler.remove(event_id)
        except KeyError:
            raise unknown() from None
        return {"deleted": event.id, "name": event.name}

    @app.post("/api/schedule/{event_id}/enable", response_model=None)
    async def schedule_enable(event_id: str, body: EnableBody) -> dict[str, Any] | JSONResponse:
        """Enable or disable an event."""
        try:
            event = await scheduler.set_enabled(event_id, body.enabled)
        except KeyError:
            raise unknown() from None
        except EventError as exc:
            return _rejected(exc.field, str(exc))
        return event_view(event)

    @app.post("/api/schedule/{event_id}/run")
    async def schedule_run_now(event_id: str) -> dict[str, Any]:
        """Run an event's action now, to test it."""
        event = scheduler.events.get(event_id)
        if event is None:
            raise unknown()
        record = await scheduler.run(event)
        return record.model_dump(mode="json")

    return scheduler
