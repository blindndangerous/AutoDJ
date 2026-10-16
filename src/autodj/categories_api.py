"""Paired web routes for a station's rotation categories (Library tools, Categories).

Each station has its own categories, like its clockwheels: the routes are
station routes, ``/api/categories`` for the default station and
``/api/stations/<slug>/categories`` for another.

``GET /api/categories`` lists the categories with their song, missing-line
and year-fallback counts, the clockwheels that use each, and the playlist
files the form chooses from.  ``POST /api/categories/check`` counts an
unsaved category for the Check button.  ``POST``, ``PUT`` and ``DELETE``
change the saved ones; a refused body answers 400 with ``detail`` and the
``field`` at fault, so the page can focus it.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Body, HTTPException
from fastapi.responses import JSONResponse

from autodj.categories import MAX_CATEGORIES as _MAX
from autodj.categories import (
    Category,
    CategoryError,
    CategoryIndex,
    CategorySpec,
    Resolution,
    describe_daypart,
    list_playlists,
    new_id,
    parse_spec,
    station_now,
    summary,
)
from autodj.clockwheel_store import ClockwheelStore, step_categories
from autodj.station_routes import StationDep, _station_categories

if TYPE_CHECKING:
    from autodj.station_routes import StationRouter
    from autodj.stations import StationRuntime


def _rejected(field: str, message: str, status: int = 400) -> JSONResponse:
    """A 400 whose ``field`` lets the page focus the field at fault."""
    return JSONResponse(status_code=status, content={"detail": message, "field": field})


def counts(resolution: Resolution) -> dict[str, Any]:
    """The counts of one category, as numbers and as one sentence."""
    return {
        "tracks": resolution.count,
        "missing": resolution.missing_count,
        "missing_examples": list(resolution.missing),
        "fallback": resolution.fallback,
        "error": resolution.error,
        "summary": summary(resolution),
    }


def clockwheel_uses(root: Path) -> dict[str, list[str]]:
    """Each category ID mapped to the names of the clockwheels with a step for it."""
    store = ClockwheelStore(root)
    uses: dict[str, list[str]] = {}
    for name in store.list_names():
        try:
            clock = store.load(name)
        except ValueError:
            continue
        for category in dict.fromkeys(
            category for step in clock["steps"] for category in step_categories(step)
        ):
            uses.setdefault(category, []).append(name)
    return uses


def install_categories(router: StationRouter) -> None:
    """Register the paired category routes of every station.

    A category shows the station's own clockwheels that use it.
    """
    write_lock = threading.Lock()
    genres_seen: dict[int, tuple[Any, list[str]]] = {}

    def index(station: StationRuntime) -> CategoryIndex:
        found = _station_categories(station, station.bridge.library)
        if found is None:
            raise HTTPException(status_code=503, detail="Folder library is not attached")
        return found

    def daypart(station: StationRuntime, category: CategorySpec) -> dict[str, Any]:
        """The allowed days, hours and dates in words, whether they allow now, and if not, which."""
        from autodj.categories import daypart_refusal

        refused = daypart_refusal(category, station_now(station.cfg))
        return {
            "daypart_text": describe_daypart(category),
            "allowed_now": refused is None,
            # "hours", "days" or "dates": what holds the songs back now; None when nothing does.
            "outside_now": refused,
        }

    def listing(station: StationRuntime, categories: CategoryIndex) -> dict[str, Any]:
        saved, results = categories.resolutions()
        uses = clockwheel_uses(station.paths.clockwheels)
        folder = categories.folder
        library = station.bridge.library
        from autodj.clock_conditions import genres

        source = getattr(library, "clock_beets", None)
        known, _, beets_error = source.snapshot() if source else ((), None, None)
        entries = library.entries_snapshot()
        seen = genres_seen.get(id(library))
        if seen is None or seen[0] is not entries:
            names = sorted(
                {value for entry in entries for value in genres(entry.genre)}, key=str.casefold
            )
            genres_seen[id(library)] = seen = (entries, names)
        genre_names = seen[1]
        return {
            "categories": [
                {
                    **category.model_dump(mode="json"),
                    **counts(results[category.id]),
                    **daypart(station, category),
                    "used_by": uses.get(category.id, []),
                }
                for category in saved
            ],
            "playlists_folder": str(folder),
            "playlists": list_playlists(folder),
            "folders": categories.folders(),
            "genres": genre_names,
            "beets_fields": sorted(known),
            "beets_error": beets_error,
            "beets": station.cfg.library.beets_db is not None,
        }

    def checked(categories: CategoryIndex, spec: CategorySpec) -> Resolution:
        """Work out *spec*, refusing what can never match (an unknown field, no file)."""
        result = categories.check(spec)
        if result.error:
            field = (
                "conditions"
                if spec.source == "filters"
                else "query"
                if spec.source == "query"
                else "playlist"
            )
            raise CategoryError(field, result.error)
        return result

    def write(
        station: StationRuntime, category_id: str | None, spec: CategorySpec
    ) -> dict[str, Any]:
        categories = index(station)
        with write_lock:
            saved = list(categories.categories())
            others = [c for c in saved if c.id != category_id]
            if category_id is not None and len(others) == len(saved):
                raise KeyError(category_id)
            if any(c.name.casefold() == spec.name.casefold() for c in others):
                raise CategoryError("name", f"A category named {spec.name} already exists")
            if category_id is None and len(saved) >= _MAX:
                raise CategoryError("name", f"AutoDJ keeps at most {_MAX} categories")
            result = checked(categories, spec)
            category = Category(id=category_id or new_id(), **spec.model_dump())
            if category_id is None:
                saved.append(category)
            else:
                saved = [category if c.id == category_id else c for c in saved]
            categories.save(saved)
        return {
            "category": {
                **category.model_dump(mode="json"),
                **counts(result),
                **daypart(station, category),
            },
            **listing(station, categories),
        }

    def remove(station: StationRuntime, category_id: str) -> dict[str, Any]:
        categories = index(station)
        with write_lock:
            saved = list(categories.categories())
            kept = [c for c in saved if c.id != category_id]
            if len(kept) == len(saved):
                raise KeyError(category_id)
            categories.save(kept)
        return listing(station, categories)

    def unknown() -> HTTPException:
        return HTTPException(status_code=404, detail="That category no longer exists")

    @router.get("/categories")
    async def categories_list(station: StationDep) -> dict[str, Any]:
        """Return the categories with their counts, and what the form chooses from."""
        return await asyncio.to_thread(listing, station, index(station))

    @router.post("/categories/check", response_model=None)
    async def categories_check(
        station: StationDep, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any] | JSONResponse:
        """Count the songs an unsaved category holds."""
        categories = index(station)
        try:
            spec = parse_spec(body)
        except CategoryError as exc:
            return _rejected(exc.field, exc.message)
        return counts(await asyncio.to_thread(categories.check, spec))

    @router.post("/categories", status_code=201, response_model=None)
    async def categories_create(
        station: StationDep,
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any] | JSONResponse:
        """Add a category."""
        try:
            return await asyncio.to_thread(write, station, None, parse_spec(body))
        except CategoryError as exc:
            return _rejected(exc.field, exc.message)

    @router.put("/categories/{category_id}", response_model=None)
    async def categories_update(
        station: StationDep, category_id: str, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any] | JSONResponse:
        """Replace a category; clockwheel steps keep naming it by its ID."""
        try:
            return await asyncio.to_thread(write, station, category_id, parse_spec(body))
        except KeyError:
            raise unknown() from None
        except CategoryError as exc:
            return _rejected(exc.field, exc.message)

    @router.delete("/categories/{category_id}")
    async def categories_delete(station: StationDep, category_id: str) -> dict[str, Any]:
        """Delete a category; a clockwheel step that names it is skipped from now on."""
        try:
            return await asyncio.to_thread(remove, station, category_id)
        except KeyError:
            raise unknown() from None
