"""Paired schedule routes and the built-in scheduler actions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from fastapi import Body, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

from autodj.liner_scheduler import LinerDecodeError
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
from autodj.station_routes import StationDep, StationRouter

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
    clock_names: Callable[[], Sequence[str]],
    select_clock: Callable[[str], Awaitable[Any]],
) -> list[Action]:
    """Actions whose backing features exist.

    Profile, song-picking preset, Stereo Tool preset, picking, liner, stream
    and discovery.
    """
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

    def clockwheels() -> list[tuple[str, str]]:
        return [(f"clock:{name}", f"Clockwheel {name}") for name in clock_names()]

    async def picking(params: dict[str, str]) -> None:
        mode = params["mode"]
        if mode.startswith("clock:"):
            try:
                await select_clock(mode.removeprefix("clock:"))
            except HTTPException as exc:
                raise ValueError(str(exc.detail)) from exc
            mode = "clock"
        if mode == "clock" and getattr(bridge.player, "_clock", None) is None:
            status = getattr(bridge.player, "_clock_status", None) or "No clockwheel is active"
            raise ValueError(f"The clockwheel cannot play: {status}")
        await asyncio.to_thread(
            bridge.set_playback_settings, PlaybackSettingsBody.model_validate({"pick_mode": mode})
        )
        bridge.save_persistent_state()

    async def liner(params: dict[str, str]) -> Callable[[], None]:
        worker = bridge.liner_scheduler
        if worker is None:
            raise ValueError("Scheduled liners need server audio or stream mode")
        if bridge.station is not None and bridge.station.state != "playing":
            raise ValueError("The stream must be playing to air a liner")
        refusal = await asyncio.to_thread(worker.refusal, params["name"])
        if refusal is not None:
            raise ValueError(f"Liner {params['name']} was skipped: it is {refusal}")
        try:
            audio = await asyncio.to_thread(worker.decode, params["name"])
        except LinerDecodeError as exc:
            raise ValueError(f"Liner {params['name']} could not be played") from exc
        return lambda: worker.play_audio(audio)

    async def stream(params: dict[str, str]) -> None:
        station = bridge.station
        if station is None:
            raise ValueError("Stream control needs stream mode")
        playing = {"start": True, "stop": False, "auto": None}[params["state"]]
        await asyncio.to_thread(station.set_scheduled_playing, playing)
        if playing and station.state == "idle":
            raise ValueError("The stream could not start: the library has no tracks")

    def stereo_tool_presets() -> list[str]:
        from autodj.stereotool_presets import list_presets

        return [str(item["name"]) for item in list_presets(Path(cfg.index.active_dir))]

    async def stereo_tool_preset(params: dict[str, str]) -> None:
        # Stereo Tool restarts with it on the next mix block; a preset
        # deleted since the event was saved fails the run in words.
        await asyncio.to_thread(
            bridge.set_playback_settings,
            PlaybackSettingsBody.model_validate({"stereotool_preset": params["name"]}),
        )
        bridge.save_persistent_state()

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
            "stereo_tool_preset",
            "Switch Stereo Tool preset",
            stereo_tool_preset,
            (ActionField("name", "Stereo Tool preset", names=stereo_tool_presets),),
            # The processor changes at once; the song playing need not end.
            interrupt="none",
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
                        ("clock", "Clockwheel, the active one"),
                        ("similarity", "Similar songs"),
                        ("random", "Random"),
                        ("least_recent", "Least recently played"),
                    ),
                    more=clockwheels,
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


def build_scheduler(
    bridge: Any,
    *,
    path: Path,
    timezone: str,
    apply_profile: Callable[[str], Awaitable[Any]],
    profile_names: Callable[[], Sequence[str]],
    liner_names: Callable[[], Sequence[str]],
    clock_names: Callable[[], Sequence[str]],
    select_clock: Callable[[str], Awaitable[Any]],
    publish: Callable[[dict[str, Any]], Awaitable[None]],
) -> Scheduler:
    """Create one station's scheduler, keeping its events in *path*, with the built-in actions.

    Later features register more actions on the returned scheduler before
    the station starts it.
    """
    scheduler = Scheduler(
        path,
        timezone,
        publish,
        interrupt=lambda: bridge.skip(),
        song_playing=lambda: bridge.player._state.current_track is not None,
        song_remaining=lambda: bridge.player._current_song_remaining_s(),
    )
    bridge.player._hour_fit_events = scheduler.hard_events
    bridge.player._hour_fit_timezone = timezone
    for action in _builtin_actions(
        bridge, apply_profile, profile_names, liner_names, clock_names, select_clock
    ):
        scheduler.register(action)
    return scheduler


def _event_view(scheduler: Scheduler, event: ScheduledEvent) -> dict[str, Any]:
    return {
        **event.model_dump(mode="json"),
        "repeat_text": describe_repeat(event),
        "action_text": scheduler.summary(event),
        # No run left: a one-time event that ran, or one past its end date.
        "ended": scheduler.next_time(event) is None,
    }


def _upcoming_view(scheduler: Scheduler, count: int) -> list[dict[str, Any]]:
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


def _parse(body: dict[str, Any]) -> EventSpec:
    try:
        return EventSpec.model_validate(body)
    except ValidationError as exc:
        raise EventError(*field_error(exc)) from exc


def _unknown() -> HTTPException:
    return HTTPException(status_code=404, detail="That event no longer exists")


def install_scheduler(router: StationRouter) -> None:
    """Add the paired schedule routes; each acts on the station its URL names."""

    @router.get("/schedule")
    async def schedule_list(station: StationDep) -> dict[str, Any]:
        """Return the actions, events, next runs and recent runs."""
        scheduler = station.schedule
        actions = await asyncio.to_thread(
            lambda: [action.metadata() for action in scheduler.actions.values()]
        )
        return {
            "timezone": scheduler.timezone,
            "actions": actions,
            "events": [_event_view(scheduler, event) for event in scheduler.events.values()],
            "upcoming": _upcoming_view(scheduler, 5),
            "runs": [run.model_dump(mode="json") for run in scheduler.runs[::-1][:RUNS_SHOWN]],
        }

    @router.get("/schedule/upcoming")
    async def schedule_upcoming(
        station: StationDep, count: int = Query(default=5, ge=1, le=50)
    ) -> dict[str, Any]:
        """Return the next *count* runs, soonest first."""
        scheduler = station.schedule
        return {"timezone": scheduler.timezone, "upcoming": _upcoming_view(scheduler, count)}

    @router.post("/schedule", status_code=201, response_model=None)
    async def schedule_create(
        station: StationDep, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any] | JSONResponse:
        """Add an event."""
        scheduler = station.schedule
        try:
            event = await scheduler.add(_parse(body))
        except EventError as exc:
            return _rejected(exc.field, str(exc))
        return _event_view(scheduler, event)

    @router.put("/schedule/{event_id}", response_model=None)
    async def schedule_update(
        station: StationDep, event_id: str, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any] | JSONResponse:
        """Replace an event."""
        scheduler = station.schedule
        try:
            event = await scheduler.replace(event_id, _parse(body))
        except KeyError:
            raise _unknown() from None
        except EventError as exc:
            return _rejected(exc.field, str(exc))
        return _event_view(scheduler, event)

    @router.delete("/schedule/{event_id}")
    async def schedule_delete(station: StationDep, event_id: str) -> dict[str, str]:
        """Delete an event."""
        scheduler = station.schedule
        try:
            event = await scheduler.remove(event_id)
        except KeyError:
            raise _unknown() from None
        return {"deleted": event.id, "name": event.name}

    @router.post("/schedule/{event_id}/enable", response_model=None)
    async def schedule_enable(
        station: StationDep, event_id: str, body: EnableBody
    ) -> dict[str, Any] | JSONResponse:
        """Enable or disable an event."""
        scheduler = station.schedule
        try:
            event = await scheduler.set_enabled(event_id, body.enabled)
        except KeyError:
            raise _unknown() from None
        except EventError as exc:
            return _rejected(exc.field, str(exc))
        return _event_view(scheduler, event)

    @router.post("/schedule/{event_id}/run")
    async def schedule_run_now(station: StationDep, event_id: str) -> dict[str, Any]:
        """Run an event's action now, to test it."""
        scheduler = station.schedule
        event = scheduler.events.get(event_id)
        if event is None:
            raise _unknown()
        record = await scheduler.run(event)
        return record.model_dump(mode="json")
