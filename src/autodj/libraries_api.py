"""Library names and station library preflight."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from autodj.library_registry import LibraryConflict
from autodj.stations import DEFAULT_SLUG


class LibraryNameBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)


class CreateLibraryBody(LibraryNameBody):
    id: str = Field(min_length=1, max_length=32)


def install_libraries(app: FastAPI, libraries: Any, stations: Any) -> None:
    def available() -> None:
        if libraries is None:
            raise HTTPException(status_code=503, detail="Library management is not available")

    def inventory() -> dict:
        if libraries is None:
            return {"libraries": [{"id": "main", "name": "Main"}]}
        return {
            "libraries": [{"id": item.id, "name": item.name} for item in libraries.registry.list()]
        }

    @app.get("/api/libraries")
    async def list_libraries() -> dict:
        return await asyncio.to_thread(inventory)

    @app.post("/api/libraries")
    async def create_library(body: CreateLibraryBody) -> dict:
        available()
        try:
            await asyncio.to_thread(libraries.registry.create, body.id, body.name)
            await asyncio.to_thread(libraries.get, body.id)
        except LibraryConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if libraries._stations is not None:
            await libraries.start()
        return await asyncio.to_thread(inventory)

    @app.patch("/api/libraries/{library_id}")
    async def rename_library(library_id: str, body: LibraryNameBody) -> dict:
        available()
        try:
            await asyncio.to_thread(libraries.registry.rename, library_id, body.name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Library not found") from exc
        except LibraryConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await asyncio.to_thread(inventory)

    @app.get("/api/libraries/{library_id}/check")
    async def check_library(library_id: str, request: Request) -> dict:
        available()
        slug = request.query_params.get("station", DEFAULT_SLUG)
        if slug not in stations.slugs():
            raise HTTPException(status_code=404, detail="Station not found")
        try:
            selected = libraries.get(library_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Library not found") from exc
        station = stations.editable(slug)

        def check() -> dict:
            from autodj.library_switch import library_warnings

            return {
                "running": not station.stopped,
                "saved_library_id": stations.configuration(slug).station.library_id,
                "active_library_id": station.cfg.index.library_id,
                "warnings": library_warnings(station, selected.library),
            }

        return await asyncio.to_thread(check)
