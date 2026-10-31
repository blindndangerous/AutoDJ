"""Station-owned operator review routes, also usable while playout is stopped."""

from __future__ import annotations

import asyncio
import contextlib
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, overload

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    field_validator,
    model_validator,
)

from autodj.review_audio import ReviewSettings
from autodj.review_jobs import ReviewError, ReviewJobs
from autodj.station_routes import SavedStationDep, StationRouter

Finite = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Target = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9:_-]{1,80}$")]


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["show", "queue", "forecast", "edition"]
    show_id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{32}$")] | None = None
    edition_id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{32}$")] | None = None
    target_take_id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{32}$")] | None = None
    include_pending: StrictBool = False
    start: datetime | None = None
    minutes: Annotated[int, Field(strict=True, ge=1, le=60)] = 60
    selection_start: Annotated[Finite, Field(ge=0)] | None = None
    selection_end: Annotated[Finite, Field(gt=0)] | None = None
    head_seconds: Annotated[Finite, Field(gt=0, le=3600)] = 10
    tail_seconds: Annotated[Finite, Field(gt=0, le=3600)] = 10
    clip_threshold_seconds: Annotated[Finite, Field(gt=0, le=86400)] = 300
    tone_seconds: Annotated[Finite, Field(gt=0, le=10)] = 0.25
    keep_complete: Annotated[list[Target], Field(max_length=500)] = []

    @field_validator("start", mode="before")
    @classmethod
    def valid_start(cls, value: Any) -> datetime | None:
        if value is None or isinstance(value, datetime):
            return value
        if isinstance(value, str):
            with contextlib.suppress(ValueError):
                return datetime.fromisoformat(value)
        raise ReviewError("Enter a valid intended start time", field="start")

    @field_validator("show_id", mode="before")
    @classmethod
    def valid_show_id(cls, value: Any) -> str | None:
        if value is None or (isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value)):
            return value
        raise ReviewError("Choose a saved show", field="show_id")

    @model_validator(mode="after")
    def valid_range(self) -> ReviewBody:
        if self.source == "show" and self.show_id is None:
            raise ReviewError("Choose a saved show", field="show_id")
        if self.source == "edition" and self.edition_id is None:
            raise ReviewError("Choose a prepared edition", field="edition_id")
        if self.source != "edition" and (self.target_take_id is not None or self.include_pending):
            raise ReviewError(
                "Pending speech and approval targets require a prepared edition", field="source"
            )
        if self.selection_end is not None and self.selection_end <= (self.selection_start or 0):
            raise ValueError("Review end must follow review start")
        return self


def _jobs(request: Request) -> ReviewJobs:
    # Request handlers run on one event loop: one shared service across stations.
    service = getattr(request.app.state, "review_jobs", None)
    if service is None:
        service = request.app.state.review_jobs = ReviewJobs()
    return service


def _check_id(identity: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise HTTPException(status_code=404, detail="Review not found")


async def _call(action: Any, *args: Any) -> Any:
    try:
        return await asyncio.to_thread(action, *args)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Review not found") from None
    except ReviewError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except RuntimeError:
        raise HTTPException(
            status_code=409, detail="Review worker is busy or shutting down"
        ) from None
    except OSError:
        raise HTTPException(status_code=503, detail="Review files could not be accessed") from None


@overload
def _job_view(job: dict) -> dict: ...


@overload
def _job_view(job: None) -> None: ...


def _job_view(job: dict | None) -> dict | None:
    if job is None:
        return None
    return {
        **job,
        **{
            key: datetime.fromtimestamp(job[key], UTC).isoformat()
            for key in ("started_at", "finished_at")
            if key in job
        },
        "status": job["state"],
        **({"result_id": job["id"]} if job["state"] == "done" else {}),
    }


def _result_view(station: Any, result: dict, shared: dict | None = None) -> dict:
    from autodj.review_source import fingerprint

    metadata = result["metadata"]
    stale = True
    with contextlib.suppress(OSError, ValueError, KeyError):
        stale = (
            fingerprint(
                station, metadata["_request"], metadata.get("_source_paths", ()), shared=shared
            )
            != metadata["_fingerprint"]
        )
    return {
        **{key: value for key, value in metadata.items() if not key.startswith("_")},
        "id": result["id"],
        "created_at": datetime.fromtimestamp(result["created_at"], UTC).isoformat(),
        "stale": stale,
    }


def install_reviews(router: StationRouter) -> None:
    @router.get("/reviews")
    async def reviews(station: SavedStationDep, request: Request) -> dict:
        service = _jobs(request)
        root = station.paths.reviews
        results = await _call(service.results, root)
        shared: dict = {}
        views = await _call(lambda: [_result_view(station, result, shared) for result in results])
        return {"results": views, "job": _job_view(await _call(service.current, root))}

    @router.post("/reviews")
    async def generate_review(station: SavedStationDep, request: Request, body: ReviewBody) -> dict:
        from autodj.review_render import render_review
        from autodj.review_source import capture_review, fingerprint

        service = _jobs(request)
        parameters = body.model_dump(mode="json")
        if body.target_take_id is not None:
            parameters["keep_complete"] = list(
                dict.fromkeys([*body.keep_complete, body.target_take_id])
            )
        settings = ReviewSettings(
            body.head_seconds, body.tail_seconds, body.clip_threshold_seconds, body.tone_seconds
        )

        def render(output: Any, cancelled: Any) -> dict:
            from autodj.stereo import audio_workspace

            with (
                tempfile.TemporaryDirectory(prefix=".capture-", dir=output) as scratch,
                audio_workspace(Path(scratch), max_bytes=service.max_scratch_bytes),
            ):
                plan = capture_review(station, parameters, cancelled=cancelled)
                selection = None
                if body.selection_start is not None or body.selection_end is not None:
                    end = body.selection_end
                    if end is None:
                        end = max(row.starts + row.seconds for row in plan.items) - plan.start
                    selection = (body.selection_start or 0, end)
                static_before = fingerprint(station, plan.request, include_live_state=False)
                metadata = render_review(
                    plan.source,
                    plan.items,
                    output,
                    settings=settings,
                    selection=selection,
                    keep_complete=parameters["keep_complete"],
                    cancelled=cancelled,
                )
                if fingerprint(station, plan.request, include_live_state=False) != static_before:
                    raise ReviewError(
                        "Programme media or settings changed during rendering; generate again"
                    )
                metadata["_request"] = plan.request
                # Capture validates a coherent input revision. Live playout may advance
                # while rendering: keep that result and report it as out of date.
                metadata["_fingerprint"] = plan.fingerprint
                metadata["notes"] = list(plan.notes)
                metadata["source"] = body.source
                if body.source == "edition":
                    from autodj.prepared_review import review_receipts

                    metadata["_prepared_receipts"] = review_receipts(
                        plan.request, metadata, output.name.removeprefix(".staging-")
                    )
                    metadata["approval_review_complete"] = any(
                        receipt["keep_complete"] and not receipt["omitted"]
                        for receipt in metadata["_prepared_receipts"]
                    )
                    metadata["edition_id"] = body.edition_id
                    metadata["target_take_id"] = body.target_take_id
                    metadata["edition_revision"] = plan.request.get("_prepared_target", {}).get(
                        "reviewed_revision"
                    )
                    metadata["includes_pending_speech"] = body.include_pending
                return metadata

        # Admit the job before any source work; capture fingerprints inside the
        # single bounded worker, so repeated clicks cannot scan the library.
        return _job_view(await _call(service.submit, station.paths.reviews, "", render))

    @router.get("/reviews/jobs/{job_id}")
    async def review_status(station: SavedStationDep, request: Request, job_id: str) -> dict:
        _check_id(job_id)
        return _job_view(await _call(_jobs(request).status, station.paths.reviews, job_id))

    @router.post("/reviews/jobs/{job_id}/cancel")
    async def cancel_review(station: SavedStationDep, request: Request, job_id: str) -> dict:
        _check_id(job_id)
        return _job_view(await _call(_jobs(request).cancel, station.paths.reviews, job_id))

    @router.get("/reviews/{review_id}")
    async def review(station: SavedStationDep, request: Request, review_id: str) -> dict:
        _check_id(review_id)
        result = await _call(_jobs(request).result, station.paths.reviews, review_id)
        return await _call(_result_view, station, result)

    @router.get("/reviews/{review_id}/audio/{format}")
    async def review_audio(
        station: SavedStationDep, request: Request, review_id: str, format: Literal["mp3", "wav"]
    ) -> FileResponse:
        _check_id(review_id)
        path = await _call(_jobs(request).audio_path, station.paths.reviews, review_id, format)
        return FileResponse(
            path,
            media_type="audio/mpeg" if format == "mp3" else "audio/wav",
            filename=f"programme-review-{review_id}.{format}",
            headers={"Cache-Control": "private, no-store"},
        )

    @router.delete("/reviews/{review_id}")
    async def delete_review(station: SavedStationDep, request: Request, review_id: str) -> dict:
        _check_id(review_id)
        await _call(_jobs(request).delete, station.paths.reviews, review_id)
        return {"deleted": review_id}
