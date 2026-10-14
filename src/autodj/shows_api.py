"""The shows routes (#106): plan a show's list of songs, edit it, and put it on the air.

Browser routes, with the signed-in session and, for a change, the page's
``Origin``, like every other station route.  They answer under
``/api/shows`` for the default station and ``/api/stations/<slug>/shows``
for any station by name:

* ``GET shows``: every show (without its songs), what a show's songs can
  come from, the show on the air and any shows the file skips.
* ``GET shows/<id>``: one show with its songs and their total length.
* ``POST shows``: add a show (``name``, ``source`` ``category`` or
  ``clockwheel``, the ``category`` ID or the ``clockwheel`` name, and
  ``target_minutes``); its list starts empty.
* ``PUT shows/<id>``: change its name, source or target; the list stays.
* ``PUT shows/<id>/songs``: save its list (``songs``, file paths in order).
  A song added must be in the library; one already in the list may stay
  after its file has gone, and is skipped on the air.
* ``POST shows/<id>/generate``: generate the list again with a dry run of
  the station's picker (optional ``start``, a station wall time, and
  ``seed``).  One at a time for the whole server; 409 while another runs.
* ``POST shows/<id>/start``: put it on the air after the song playing.
* ``DELETE shows/on-air``: take the show on the air off; its waiting songs
  leave the queue.
* ``DELETE shows/<id>``: remove a show.

A refused field answers 400 with ``detail`` and ``field``.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fastapi import Body, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from autodj.shows import (
    MAX_SHOWS,
    MAX_SONGS,
    MAX_TARGET_MINUTES,
    MIN_TARGET_MINUTES,
    Show,
    ShowBusy,
    ShowError,
    ShowGenerations,
    fresh_seed,
    generate,
    length_text,
    parse_spec,
    songs_view,
    with_songs,
    with_spec,
)
from autodj.station_routes import StationDep, StationRouter

if TYPE_CHECKING:
    from autodj.stations import StationRuntime

_ID_LENGTH = 32


class SongsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    songs: list[StrictStr] = Field(max_length=MAX_SONGS)


class GenerateBody(BaseModel):
    """``start`` is a station wall time such as ``2026-12-24T20:00``; empty is now."""

    model_config = ConfigDict(extra="forbid")

    start: datetime | None = None
    seed: StrictInt | None = Field(default=None, ge=0)


def _rejected(field: str, message: str, status: int = 400) -> JSONResponse:
    """A refusal whose ``field`` lets the page mark and focus the field at fault."""
    return JSONResponse(status_code=status, content={"detail": message, "field": field})


def _gone() -> HTTPException:
    return HTTPException(status_code=404, detail="That show no longer exists")


def _check_id(show_id: str) -> None:
    if len(show_id) != _ID_LENGTH or any(ch not in "0123456789abcdef" for ch in show_id):
        raise _gone()


def _store(station: StationRuntime) -> Any:
    store = getattr(station, "shows", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Shows are not available on this station")
    return store


def _generations(request: Request) -> ShowGenerations:
    state = request.app.state
    generations = getattr(state, "show_generations", None)
    if generations is None:
        generations = state.show_generations = ShowGenerations()
    return generations


def _iso(stamp: float | None, timezone: str) -> str | None:
    if stamp is None:
        return None
    return datetime.fromtimestamp(stamp, ZoneInfo(timezone)).isoformat(timespec="seconds")


def _source_names(station: StationRuntime) -> tuple[dict[str, str], list[str]]:
    """Category names by ID, and clockwheel names (blocking: reads files)."""
    from autodj.station_routes import _clock_store

    library = station.bridge.library
    index = getattr(library, "categories", None) if library is not None else None
    categories: dict[str, str] = {}
    if index is not None:
        try:
            categories = {item.id: item.name for item in index.categories()}
        except Exception:  # an unreadable file: the list says so by being empty
            categories = {}
    try:
        clocks = _clock_store(station).list_names()
    except OSError:
        clocks = []
    return categories, clocks


def _source_text(show: Show, categories: dict[str, str]) -> str:
    if show.source == "category":
        name = categories.get(show.category)
        return f"Category {name}" if name else "A category that no longer exists"
    return f"Clockwheel {show.clockwheel}"


def _events_for(station: StationRuntime, show_id: str) -> list[str]:
    schedule = getattr(station, "schedule", None)
    if schedule is None:
        return []
    return sorted(
        event.name
        for event in schedule.events.values()
        if event.action == "show" and event.params.get("show") == show_id
    )


def _show_view(
    station: StationRuntime,
    show_id: str,
    show: Show,
    categories: dict[str, str],
    *,
    songs: bool,
) -> dict[str, Any]:
    rows, total = songs_view(show, station.bridge.entry_for_path)
    view: dict[str, Any] = {
        "id": show_id,
        "name": show.name,
        "source": show.source,
        "category": show.category,
        "clockwheel": show.clockwheel,
        "source_text": _source_text(show, categories),
        "target_minutes": show.target_minutes,
        "count": len(show.songs),
        "missing": sum(1 for row in rows if row["missing"]),
        "total_seconds": round(total, 1),
        "total_text": length_text(total),
        "generated_at": _iso(show.generated_at, station.timezone),
        "note": show.note,
        "events": _events_for(station, show_id),
    }
    if songs:
        view["songs"] = rows
    return view


def _list_view(station: StationRuntime) -> dict[str, Any]:
    store = _store(station)
    shows = store.all()
    categories, clocks = _source_names(station)
    runner = getattr(station, "show_runner", None)
    on_air = runner.status() if runner is not None else None
    if on_air is not None:
        on_air = {**on_air, "started_at": _iso(on_air["started_at"], station.timezone)}
    return {
        "shows": sorted(
            (
                _show_view(station, show_id, show, categories, songs=False)
                for show_id, show in shows.items()
            ),
            key=lambda view: view["name"].casefold(),
        ),
        "categories": [
            {"id": key, "name": name}
            for key, name in sorted(categories.items(), key=lambda pair: pair[1].casefold())
        ],
        "clockwheels": clocks,
        "on_air": on_air,
        "problem": store.problem(),
        "limits": {
            "max_shows": MAX_SHOWS,
            "max_songs": MAX_SONGS,
            "min_minutes": MIN_TARGET_MINUTES,
            "max_minutes": MAX_TARGET_MINUTES,
        },
        "timezone": station.timezone,
    }


def _one_view(station: StationRuntime, show_id: str) -> dict[str, Any]:
    show = _store(station).get(show_id)
    if show is None:
        raise _gone()
    categories, _clocks = _source_names(station)
    return _show_view(station, show_id, show, categories, songs=True)


def _check_source_exists(station: StationRuntime, spec: Any) -> None:
    categories, clocks = _source_names(station)
    if spec.source == "category" and spec.category not in categories:
        raise ShowError("category", "That category no longer exists; choose another")
    if spec.source == "clockwheel" and spec.clockwheel not in clocks:
        raise ShowError("clockwheel", f"There is no clockwheel named {spec.clockwheel}")


def _start_time(body: GenerateBody, timezone: str) -> float | None:
    if body.start is None:
        return None
    moment = body.start
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(timezone))
    try:
        stamp = moment.astimezone(UTC).timestamp()
    except (OverflowError, OSError, ValueError):
        stamp = math.nan
    return stamp


def install_shows(router: StationRouter) -> None:
    """Add the paired shows routes; each acts on the station its URL names."""

    @router.get("/shows")
    async def shows_list(station: StationDep) -> dict[str, Any]:
        """Every show without its songs, the sources, and the show on the air."""
        return await asyncio.to_thread(_list_view, station)

    @router.post("/shows", status_code=201, response_model=None)
    async def shows_create(
        station: StationDep, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any] | JSONResponse:
        """Add a show with an empty list."""
        store = _store(station)

        def create() -> dict[str, Any]:
            spec = parse_spec(body)
            _check_source_exists(station, spec)
            show_id, _show = store.add(spec)
            return _one_view(station, show_id)

        try:
            return await asyncio.to_thread(create)
        except ShowError as exc:
            return _rejected(exc.field, exc.message)
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail="The shows file could not be saved"
            ) from exc

    @router.delete("/shows/on-air")
    async def shows_stop(station: StationDep) -> dict[str, Any]:
        """Take the show on the air off (or forget one waiting to resume).

        ``done`` is false while some of its songs could not be taken out of the
        queue yet; that is tried again at each song change.
        """
        runner = getattr(station, "show_runner", None)
        stopped = await runner.stop() if runner is not None else None
        if stopped is None:
            raise HTTPException(status_code=409, detail="No show is on the air")
        return {"stopped": stopped["name"], "done": stopped["done"]}

    @router.get("/shows/{show_id}")
    async def shows_get(station: StationDep, show_id: str) -> dict[str, Any]:
        """One show with its songs."""
        _check_id(show_id)
        return await asyncio.to_thread(_one_view, station, show_id)

    @router.put("/shows/{show_id}", response_model=None)
    async def shows_update(
        station: StationDep, show_id: str, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any] | JSONResponse:
        """Change a show's name, source or target; its list stays."""
        _check_id(show_id)
        store = _store(station)

        def change() -> dict[str, Any]:
            spec = parse_spec(body)
            current = store.get(show_id)
            if current is None:
                raise _gone()
            if (spec.source, spec.category, spec.clockwheel) != (
                current.source,
                current.category,
                current.clockwheel,
            ):
                _check_source_exists(station, spec)
            store.update(show_id, lambda show: with_spec(show, spec))
            return _one_view(station, show_id)

        try:
            return await asyncio.to_thread(change)
        except KeyError:
            raise _gone() from None
        except ShowError as exc:
            return _rejected(exc.field, exc.message)
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail="The shows file could not be saved"
            ) from exc

    @router.put("/shows/{show_id}/songs", response_model=None)
    async def shows_songs(
        station: StationDep, request: Request, show_id: str, body: SongsBody
    ) -> dict[str, Any] | JSONResponse:
        """Save a show's list, in order; 409 while the list is being generated."""
        _check_id(show_id)
        store = _store(station)

        def save() -> dict[str, Any]:
            current = store.get(show_id)
            if current is None:
                raise _gone()
            kept = set(current.songs)
            for path in body.songs:
                if path not in kept and station.bridge.entry_for_path(path) is None:
                    raise ShowError("songs", "A song you added is not in the library")
            store.update(show_id, lambda show: with_songs(show, body.songs))
            return _one_view(station, show_id)

        busy = (
            "The list is being generated or changed; your change was not saved. "
            "Try again when that is done."
        )
        try:
            # The check and the save are one step: a generation waits (409) meanwhile.
            with _generations(request).reserved(station.slug, show_id, busy):
                return await asyncio.to_thread(save)
        except ShowBusy as exc:
            return _rejected(exc.field, exc.message, 409)
        except KeyError:
            raise _gone() from None
        except ShowError as exc:
            return _rejected(exc.field, exc.message)
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail="The shows file could not be saved"
            ) from exc

    @router.post("/shows/{show_id}/generate", response_model=None)
    async def shows_generate(
        station: StationDep, request: Request, show_id: str, body: GenerateBody
    ) -> dict[str, Any] | JSONResponse:
        """Generate the list again with a dry run of the station's picker."""
        from autodj.rotation_api import station_source
        from autodj.rotation_sim import EARLIEST_START, LATEST_START, snapshot_player

        _check_id(show_id)
        store = _store(station)
        show = await asyncio.to_thread(store.get, show_id)
        if show is None:
            raise _gone()
        start = _start_time(body, station.timezone)
        if start is not None and not (EARLIEST_START <= start < LATEST_START):
            return _rejected("start", "Pick as if at must be between 2000 and 2099")
        seed = body.seed if body.seed is not None else fresh_seed()
        source = station_source(station)

        def work(cancelled: Any) -> Any:
            snapshot = snapshot_player(source.player)
            return generate(
                replace(source, snapshot=snapshot),
                show_id,
                show,
                start=start,
                seed=seed,
                cancelled=cancelled,
            )

        generations = _generations(request)

        def save(result: Any) -> dict[str, Any]:
            store.update(
                show_id,
                lambda current: with_songs(
                    current, result.songs, generated_at=time.time(), note=result.note
                ),
            )
            return _one_view(station, show_id)

        try:
            # Held until the new list is saved: edits to it wait (409) meanwhile.
            with generations.reserved(station.slug, show_id):
                result = await generations.run(work)
                view = await asyncio.to_thread(save, result)
        except ShowBusy as exc:
            return _rejected(exc.field, exc.message, 409)
        except ShowError as exc:
            return _rejected(exc.field, exc.message)
        except KeyError:
            raise _gone() from None
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail="The shows file could not be saved"
            ) from exc
        return {**view, "seed": seed}

    @router.post("/shows/{show_id}/start")
    async def shows_start(station: StationDep, show_id: str) -> dict[str, Any]:
        """Put a show on the air after the song playing."""
        _check_id(show_id)
        runner = getattr(station, "show_runner", None)
        if runner is None:
            raise HTTPException(status_code=503, detail="Shows are not available on this station")
        try:
            message = await runner.start(show_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {"message": message}

    @router.delete("/shows/{show_id}")
    async def shows_delete(station: StationDep, show_id: str) -> dict[str, Any]:
        """Remove a show; events that play it fail until changed."""
        _check_id(show_id)
        store = _store(station)
        try:
            show = await asyncio.to_thread(store.delete, show_id)
        except KeyError:
            raise _gone() from None
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail="The shows file could not be saved"
            ) from exc
        return {"deleted": show_id, "name": show.name}


__all__ = ["install_shows"]
