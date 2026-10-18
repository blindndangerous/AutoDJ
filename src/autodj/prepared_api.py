"""Paired-browser prepared-edition editing, bounded imports and explicit approval."""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from autodj.breaks_api import BODY_DEADLINE_SECONDS, MIN_BODY_BYTES_PER_SECOND, decode_gate
from autodj.prepared_editions import (
    ID,
    MAX_UPLOAD_BYTES,
    Edition,
    EditionError,
    Occurrence,
    ReviewReceipt,
    edition_store,
    relevant_signature,
)
from autodj.station_routes import SavedStationDep, StationDep, StationRouter

UPLOAD_PATH = "/api/editions/{edition_id}/takes"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateBody(_Body):
    show_id: ID
    name: StrictStr | None = Field(default=None, min_length=1, max_length=100)


class EditBody(_Body):
    expected_revision: StrictInt = Field(ge=1)
    name: StrictStr | None = Field(default=None, min_length=1, max_length=100)
    occurrences: list[Occurrence] | None = Field(default=None, max_length=500)


class StartBody(_Body):
    now: StrictBool = False
    confirm_reair: StrictBool = False
    from_occurrence: ID | None = None


class ApproveBody(_Body):
    review_id: ID


class UploadFields(_Body):
    expected_revision: int = Field(ge=1)
    filename: str = Field(min_length=1, max_length=255)
    position: Literal["opening", "transition", "closing", "segment"]
    before_id: ID | None = None
    after_id: ID | None = None
    placement: Literal["auto", "talk_up", "standalone"] = "auto"
    allow_outro: bool = False
    take_id: ID | None = None
    index: int | None = Field(default=None, ge=0, le=500)
    label: str = Field(default="Speech", max_length=200)


async def _call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except EditionError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except OSError:
        raise HTTPException(
            status_code=503,
            detail="Edition files could not be saved; the previous edition is preserved",
        ) from None


async def _editable(station: Any, edition_id: str) -> None:
    runner = getattr(station, "show_runner", None)
    if runner is not None and not await asyncio.to_thread(runner.prepared_editable, edition_id):
        raise HTTPException(
            status_code=409,
            detail="This speech is already committed to playback; stop the edition before editing",
        )


async def _changed(station: Any) -> None:
    runner = getattr(station, "show_runner", None)
    if runner is not None:
        await asyncio.to_thread(runner.prepared_changed)


def _view(station: Any, edition: Edition) -> dict[str, Any]:
    store = edition_store(station)
    value = edition.model_dump(mode="json")
    value["assets"] = {
        key: {field: getattr(asset, field) for field in ("id", "seconds", "bytes", "label")}
        for key, asset in edition.assets.items()
    }
    estimate = 0.0
    for item, row in zip(edition.occurrences, value["occurrences"], strict=True):
        entry = (
            station.bridge.library.entry_for_path(item.path)
            if item.kind == "song" and station.bridge.library is not None
            else None
        )
        asset = edition.assets.get(item.asset_id or "")
        row["label"] = (
            entry.display_name if entry is not None else asset.label if asset else "Missing song"
        )
        row["seconds"] = (
            float(entry.length or 0) if entry is not None else asset.seconds if asset else 0.0
        )
        row["missing"] = entry is None if item.kind == "song" else asset is None
        estimate += row["seconds"]
    hashes: dict[Path, str] = {}
    for take, row in zip(edition.takes, value["takes"], strict=True):
        row["status"] = store.take_status(edition, take, station, hashes=hashes)
        try:
            row["dependency_hash"] = relevant_signature(edition, take, station, hashes=hashes)
        except (ValueError, OSError):
            row["dependency_hash"] = None
        if take.position != "segment":
            asset = edition.assets.get(take.asset_id)
            estimate += asset.seconds if asset else 0
    show = (
        station.shows.get(edition.show_id) if getattr(station, "shows", None) is not None else None
    )
    target = float(show.target_minutes) * 60 if show is not None else None
    runner = getattr(station, "show_runner", None)
    running = runner.status() if runner is not None else None
    run: dict[str, Any] | None = None
    if running is not None:
        candidate = (
            running
            if running.get("edition_id") == edition.id
            else running.get("interrupted_edition")
        )
        if isinstance(candidate, dict) and candidate.get("edition_id") == edition.id:
            run = {key: item for key, item in candidate.items() if key != "interrupted_edition"}
    value["run"] = run
    value["committed_audio_notice"] = run.get("committed_audio_notice") if run else None
    value.update(
        estimated_seconds=estimate,
        estimate_known=False,
        target_seconds=target,
        likely_overrun=target is not None and estimate > target,
    )
    return value


def install_prepared(
    router: StationRouter,
    *,
    receipt: Callable[[Any, str], Mapping[str, Any]],
    active: Callable[[Any, str], bool] = lambda station, identity: False,
) -> None:
    @router.get("/editions")
    async def editions(station: SavedStationDep, show_id: str | None = None) -> dict:
        values = await _call(edition_store(station).all)
        return {
            "editions": [
                await _call(_view, station, edition)
                for edition in values.values()
                if show_id is None or edition.show_id == show_id
            ]
        }

    @router.post("/editions", status_code=201)
    async def create(station: SavedStationDep, body: CreateBody) -> dict:
        show = await _call(station.shows.get, body.show_id)
        edition = await _call(edition_store(station).create, body.show_id, show, name=body.name)
        return await _call(_view, station, edition)

    @router.get("/editions/{edition_id}")
    async def get(station: SavedStationDep, edition_id: str) -> dict:
        return await _call(_view, station, await _call(edition_store(station).get, edition_id))

    @router.post("/editions/{edition_id}/start")
    async def start(station: StationDep, edition_id: ID, body: StartBody) -> dict:
        runner = getattr(station, "show_runner", None)
        if runner is None:
            raise HTTPException(status_code=503, detail="Shows are not available on this station")
        try:
            message = await runner.start_edition(
                edition_id,
                now=body.now,
                confirm_reair=body.confirm_reair,
                from_occurrence=body.from_occurrence,
            )
        except EditionError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except OSError:
            raise HTTPException(
                status_code=503, detail="The edition's start state could not be saved"
            ) from None
        return {"message": message}

    @router.put("/editions/{edition_id}")
    async def edit(station: SavedStationDep, edition_id: str, body: EditBody) -> dict:
        await _editable(station, edition_id)
        if body.occurrences is not None:
            original = await _call(edition_store(station).get, edition_id)
            old = {item.path for item in original.occurrences if item.kind == "song"}
            if any(
                item.kind == "song"
                and item.path not in old
                and station.bridge.library.entry_for_path(item.path) is None
                for item in body.occurrences
            ):
                raise HTTPException(
                    status_code=400, detail="Add songs from this station's music library"
                )
        edition = await _call(
            edition_store(station).edit,
            edition_id,
            body.expected_revision,
            name=body.name,
            occurrences=body.occurrences,
        )
        await _changed(station)
        return await _call(_view, station, edition)

    @router.delete("/editions/{edition_id}")
    async def delete(station: SavedStationDep, edition_id: str, expected_revision: int) -> dict:
        if active(station, edition_id):
            raise HTTPException(
                status_code=409, detail="Stop this edition before deleting its source audio"
            )
        await _call(edition_store(station).delete, edition_id, expected_revision)
        return {"deleted": edition_id}

    @router.post("/editions/{edition_id}/takes", status_code=201)
    async def upload(station: SavedStationDep, request: Request, edition_id: str) -> dict:
        try:
            fields = UploadFields.model_validate(dict(request.query_params))
            from autodj.break_audio import demuxer_for

            suffix = Path(fields.filename).suffix.lower()
            demuxer_for(Path(f"upload{suffix}"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        declared = request.headers.get("content-length")
        if declared and (not declared.isdecimal() or int(declared) > MAX_UPLOAD_BYTES):
            raise HTTPException(status_code=413, detail="The audio upload exceeds 512 MiB")
        await _editable(station, edition_id)
        store = edition_store(station)
        original = await _call(store.get, edition_id)
        if original.revision != fields.expected_revision:
            raise HTTPException(status_code=409, detail="Edition changed; reload before saving")
        store.audio.mkdir(parents=True, exist_ok=True)
        cancelled = threading.Event()
        with tempfile.TemporaryDirectory(prefix=".upload-", dir=store.audio) as scratch:
            source = Path(scratch) / f"source{suffix}"
            try:
                size = int(declared) if declared else MAX_UPLOAD_BYTES
                async with asyncio.timeout(
                    BODY_DEADLINE_SECONDS + size / MIN_BODY_BYTES_PER_SECOND
                ):
                    total = 0
                    with source.open("wb") as output:
                        async for chunk in request.stream():
                            total += len(chunk)
                            if total > MAX_UPLOAD_BYTES:
                                raise HTTPException(
                                    status_code=413, detail="The audio upload exceeds 512 MiB"
                                )
                            await asyncio.to_thread(output.write, chunk)
                options = fields.model_dump(exclude={"expected_revision", "filename"})
                async with decode_gate():
                    await _editable(station, edition_id)
                    task = asyncio.create_task(
                        _call(
                            store.import_take,
                            edition_id,
                            fields.expected_revision,
                            source,
                            **options,
                            cancelled=cancelled.is_set,
                        )
                    )
                    try:
                        edition = await asyncio.shield(task)
                    except asyncio.CancelledError:
                        cancelled.set()
                        with contextlib.suppress(Exception):
                            await asyncio.shield(task)
                        raise
            except TimeoutError:
                raise HTTPException(
                    status_code=408, detail="The upload arrived too slowly; send it again"
                ) from None
            finally:
                cancelled.set()
        await _changed(station)
        return await _call(_view, station, edition)

    @router.get("/editions/{edition_id}/assets/{asset_id}")
    async def asset(station: SavedStationDep, edition_id: str, asset_id: str) -> FileResponse:
        store = edition_store(station)
        edition = await _call(store.get, edition_id)
        if asset_id not in edition.assets:
            raise HTTPException(
                status_code=404, detail="That audio does not belong to this edition"
            )
        path = await _call(store.asset_path, asset_id)
        return FileResponse(
            path, media_type="audio/wav", headers={"Cache-Control": "private, no-store"}
        )

    @router.delete("/editions/{edition_id}/takes/{take_id}")
    async def remove_take(
        station: SavedStationDep, edition_id: str, take_id: str, expected_revision: int
    ) -> dict:
        await _editable(station, edition_id)
        edition = await _call(
            edition_store(station).remove_take, edition_id, take_id, expected_revision
        )
        await _changed(station)
        return await _call(_view, station, edition)

    @router.post("/editions/{edition_id}/takes/{take_id}/approve")
    async def approve(
        station: SavedStationDep, edition_id: str, take_id: str, body: ApproveBody
    ) -> dict:
        data = await _call(receipt, station, body.review_id)
        proof = next(
            (
                item
                for item in data.get("receipts", ())
                if item.get("edition_id") == edition_id and item.get("take_id") == take_id
            ),
            None,
        )
        if proof is None:
            raise HTTPException(
                status_code=409,
                detail="This review does not establish complete approval of that take",
            )
        try:
            evidence = ReviewReceipt.model_validate(proof)
        except ValueError:
            raise HTTPException(
                status_code=409,
                detail="The stored review proof is incomplete; review the take again",
            ) from None
        await _editable(station, edition_id)
        edition = await _call(
            edition_store(station).approve, edition_id, take_id, evidence, station
        )
        await _changed(station)
        return await _call(_view, station, edition)
