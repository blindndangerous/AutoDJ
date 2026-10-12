"""Paired web routes for rotation categories (Library tools, Categories).

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
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from autodj.categories import MAX_CATEGORIES as _MAX
from autodj.categories import (
    Category,
    CategoryError,
    CategoryIndex,
    CategorySpec,
    Resolution,
    list_playlists,
    new_id,
    parse_spec,
    summary,
)
from autodj.clockwheel_store import ClockwheelStore


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
            step["category"] for step in clock["steps"] if step["type"] == "category"
        ):
            uses.setdefault(category, []).append(name)
    return uses


def install_categories(app: FastAPI, bridge: Any) -> None:
    """Register the paired category routes; the library owns the category index."""
    write_lock = threading.Lock()

    def index() -> CategoryIndex:
        library = bridge.library
        found = getattr(library, "categories", None)
        if found is None:
            raise HTTPException(status_code=503, detail="Folder library is not attached")
        return found

    def clock_root() -> Path:
        return Path(bridge.player._cfg.index.active_dir) / "clockwheels"

    def listing(categories: CategoryIndex) -> dict[str, Any]:
        saved, results = categories.resolutions()
        uses = clockwheel_uses(clock_root())
        folder = categories.folder
        return {
            "categories": [
                {
                    **category.model_dump(),
                    **counts(results[category.id]),
                    "used_by": uses.get(category.id, []),
                }
                for category in saved
            ],
            "playlists_folder": str(folder),
            "playlists": list_playlists(folder),
            "beets": bridge.player._cfg.library.beets_db is not None,
        }

    def checked(categories: CategoryIndex, spec: CategorySpec) -> Resolution:
        """Work out *spec*, refusing what can never match (an unknown field, no file)."""
        result = categories.check(spec)
        if result.error:
            raise CategoryError("query" if spec.source == "query" else "playlist", result.error)
        return result

    def write(category_id: str | None, spec: CategorySpec) -> dict[str, Any]:
        categories = index()
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
            "category": {**category.model_dump(), **counts(result)},
            **listing(categories),
        }

    def remove(category_id: str) -> dict[str, Any]:
        categories = index()
        with write_lock:
            saved = list(categories.categories())
            kept = [c for c in saved if c.id != category_id]
            if len(kept) == len(saved):
                raise KeyError(category_id)
            categories.save(kept)
        return listing(categories)

    def unknown() -> HTTPException:
        return HTTPException(status_code=404, detail="That category no longer exists")

    @app.get("/api/categories")
    async def categories_list() -> dict[str, Any]:
        """Return the categories with their counts, and what the form chooses from."""
        return await asyncio.to_thread(listing, index())

    @app.post("/api/categories/check", response_model=None)
    async def categories_check(body: dict[str, Any] = Body(...)) -> dict[str, Any] | JSONResponse:
        """Count the songs an unsaved category holds."""
        categories = index()
        try:
            spec = parse_spec(body)
        except CategoryError as exc:
            return _rejected(exc.field, exc.message)
        return counts(await asyncio.to_thread(categories.check, spec))

    @app.post("/api/categories", status_code=201, response_model=None)
    async def categories_create(
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any] | JSONResponse:
        """Add a category."""
        try:
            return await asyncio.to_thread(write, None, parse_spec(body))
        except CategoryError as exc:
            return _rejected(exc.field, exc.message)

    @app.put("/api/categories/{category_id}", response_model=None)
    async def categories_update(
        category_id: str, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any] | JSONResponse:
        """Replace a category; clockwheel steps keep naming it by its ID."""
        try:
            return await asyncio.to_thread(write, category_id, parse_spec(body))
        except KeyError:
            raise unknown() from None
        except CategoryError as exc:
            return _rejected(exc.field, exc.message)

    @app.delete("/api/categories/{category_id}")
    async def categories_delete(category_id: str) -> dict[str, Any]:
        """Delete a category; a clockwheel step that names it is skipped from now on."""
        try:
            return await asyncio.to_thread(remove, category_id)
        except KeyError:
            raise unknown() from None
