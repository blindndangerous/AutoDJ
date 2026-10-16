"""Browser-only speech routes, installed once for shared and station ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from autodj.config import AutoDJConfig
from autodj.speech import SpeechService
from autodj.speech_providers import ENGINES, ProviderStore
from autodj.station_routes import SavedStationDep, StationRouter
from autodj.stations import StationPaths, StationRegistry

SPEECH_BODY_LIMIT = 64 * 1024
WRITE_ROUTES = (
    "/api/speech/providers/{name}",
    "/api/speech/preflight",
    "/api/speech/batches",
    "/api/speech/batches/{token}/retry",
    "/api/speech/batches/{token}/save",
    "/api/speech/cancel",
    "/api/speech/saved/{name}/publish",
    "/api/speech/presets/{name}",
    "/api/speech/default",
    "/api/speech/time-inventory",
    "/api/speech/time-sets",
    "/api/speech/active-set",
)


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection: StrictStr
    voice: StrictStr = ""
    model: StrictStr = ""
    speed: Annotated[float, Field(strict=True, allow_inf_nan=False)] = 1
    instructions: StrictStr = ""


class CompareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Annotated[StrictStr, Field(min_length=1, max_length=5000)]
    candidates: Annotated[list[Candidate], Field(min_length=1, max_length=8)]


class GenerateBody(CompareBody):
    token: StrictStr
    fresh: StrictBool = False


class RetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    indices: Annotated[list[StrictInt], Field(min_length=1, max_length=1440)]
    token: StrictStr


class SaveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: StrictInt
    label: StrictStr


class TimeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preset: StrictStr
    hour_format: StrictStr
    token: StrictStr = ""
    fingerprint: StrictStr = ""


class ActivateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr | None
    reviewed: StrictBool = False


async def _call(action: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return await asyncio.to_thread(action, *args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except OSError:
        raise HTTPException(
            status_code=503, detail="Speech files could not be accessed; try again"
        ) from None


def install_speech(
    app: FastAPI, router: StationRouter, cfg: AutoDJConfig, stations: StationRegistry
) -> None:
    """Install shared connections and saved-state station operations (ADR 13)."""
    providers = ProviderStore(Path(cfg.index.index_dir) / "speech_providers.json")
    service = SpeechService(
        providers,
        lambda: [
            StationPaths.for_station(stations.configuration(slug), slug).speech
            for slug in stations.slugs()
        ],
        command=lambda root: next(
            (
                stations.configuration(slug).time_checks.tts_command
                for slug in stations.slugs()
                if StationPaths.for_station(stations.configuration(slug), slug).speech == root
            ),
            [],
        ),
    )
    app.state.speech = service

    @app.get("/api/speech/providers")
    async def connections() -> dict:
        return {"connections": await _call(providers.public), "engines": ENGINES}

    @app.put("/api/speech/providers/{name}")
    async def connection(name: str, body: dict = Body(...)) -> dict:
        await _call(providers.save, name, body)
        return {"saved": True}

    @router.get("/speech")
    async def state(station: SavedStationDep) -> dict:
        return await _call(service.view, station.paths.speech)

    @router.post("/speech/preflight")
    async def preflight(station: SavedStationDep, body: CompareBody) -> dict:
        return await _call(
            service.preflight,
            body.text,
            [c.model_dump() for c in body.candidates],
            root=station.paths.speech,
        )

    @router.post("/speech/batches")
    async def generate(station: SavedStationDep, body: GenerateBody) -> dict:
        token = await _call(
            service.submit,
            station.paths.speech,
            body.text,
            [c.model_dump() for c in body.candidates],
            body.token,
            fresh=body.fresh,
        )
        return {"id": token}

    @router.post("/speech/batches/{token}/retry")
    async def retry(station: SavedStationDep, token: str, body: RetryBody) -> dict:
        await _call(service.retry, station.paths.speech, token, body.indices, attempt=body.token)
        return {"started": True}

    @router.delete("/speech/batches/{token}")
    async def remove(station: SavedStationDep, token: str) -> dict:
        await _call(service.remove, station.paths.speech, token)
        return {"removed": True}

    @router.post("/speech/cancel")
    async def cancel(station: SavedStationDep) -> dict:
        await _call(service.cancel, station.paths.speech)
        return {"cancelled": True}

    @router.get("/speech/batches/{token}/samples/{index}")
    async def sample(station: SavedStationDep, token: str, index: int) -> FileResponse:
        path = await _call(service.sample, station.paths.speech, token, index)
        return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @router.post("/speech/batches/{token}/save")
    async def save(station: SavedStationDep, token: str, body: SaveBody) -> dict:
        name = await _call(service.save, station.paths.speech, token, body.index, body.label)
        return {"id": name}

    @router.post("/speech/saved/{name}/publish")
    async def publish(station: SavedStationDep, name: str) -> dict:
        path = await _call(service.publish, station.paths.speech, name, station.paths.liners)
        return {"name": path.name}

    @router.get("/speech/saved/{name}/audio")
    async def saved_audio(station: SavedStationDep, name: str) -> FileResponse:
        path = await _call(service.saved_audio, station.paths.speech, name)
        return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @router.get("/speech/time-sets/{name}/samples/{minute}")
    async def set_sample(station: SavedStationDep, name: str, minute: str) -> FileResponse:
        path = await _call(service.set_audio, station.paths.speech, name, minute)
        return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @router.put("/speech/presets/{name}")
    async def preset(station: SavedStationDep, name: str, body: Candidate) -> dict:
        await _call(service.preset, station.paths.speech, name, body.model_dump())
        return {"saved": True}

    @router.put("/speech/default")
    async def default(
        station: SavedStationDep, name: Annotated[StrictStr, Body(embed=True)]
    ) -> dict:
        await _call(service.default, station.paths.speech, name)
        return {"saved": True}

    @router.post("/speech/time-inventory")
    async def inventory(station: SavedStationDep, body: TimeBody) -> dict:
        return await _call(
            service.time_inventory, station.paths.speech, body.preset, body.hour_format
        )

    @router.post("/speech/time-sets")
    async def times(station: SavedStationDep, body: TimeBody) -> dict:
        name = await _call(
            service.generate_times,
            station.paths.speech,
            body.preset,
            body.hour_format,
            body.token,
            expected=body.fingerprint,
        )
        return {"id": name}

    @router.put("/speech/active-set")
    async def activate(station: SavedStationDep, body: ActivateBody) -> dict:
        await _call(service.activate, station.paths.speech, body.name, body.reviewed)
        return {"saved": True}
