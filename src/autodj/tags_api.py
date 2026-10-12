"""The paired route that edits a recording's tags from Track info (#53).

``POST /api/library/tags`` takes the recording's path and only the tags the
page changed.  It refuses a song that is playing or up next on any station,
writes the file safely (:func:`autodj.tag_edit.write_tags`), stores the new
values and the file's new identity in the catalog without analysing the
audio again, and reloads the library.  A refused field answers 400 with
``detail`` and the ``field`` at fault, so the page can focus it.

The library is shared by every station, so this is a shared route.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from autodj.catalog import normalize_path
from autodj.stations import DEFAULT_SLUG
from autodj.tag_edit import (
    TagError,
    TagValues,
    read_isrc,
    tags_writable,
    update_beets,
    validate_tags,
    write_tags,
)

logger = logging.getLogger(__name__)


class TagEditBody(BaseModel):
    """The recording and the tags to change; tags left out stay as they are."""

    model_config = ConfigDict(extra="forbid")
    path: str
    tags: dict[str, StrictStr]
    beets: StrictBool = False


def _bpm_text(bpm: float) -> str:
    return f"{bpm:.2f}".rstrip("0").rstrip(".") if bpm > 0 else ""


def tag_form(library: Any, record: dict[str, Any]) -> dict[str, Any]:
    """What the Track info tag form starts from, for :func:`api_library_points`.

    ``tags`` holds each field as the text the form shows; ``tags_writable``
    says whether AutoDJ can write this kind of file; ``beets_writeback``
    whether the page may offer to update beets too.
    """
    from autodj.dj_meta import musical_label

    path = record["path"]
    key = musical_label(
        record.get("key", -1),
        record.get("mode", -1),
        prefer_flats=library.cfg.playback.key_prefer_flats,
    )
    year = record.get("year") or 0
    beets_db = library.cfg.library.beets_db
    return {
        "tags": {
            "title": record.get("title") or "",
            "artist": record.get("artist") or "",
            "album": record.get("album") or "",
            "genre": record.get("genre") or "",
            "year": str(year) if year else "",
            "bpm": _bpm_text(float(record.get("bpm") or 0)),
            "key": "" if key == "--" else key,
            "isrc": read_isrc(path),
        },
        "tags_writable": tags_writable(path),
        "beets_writeback": bool(beets_db is not None and Path(beets_db).is_file()),
    }


def _catalog_fields(values: TagValues) -> dict[str, Any]:
    """The catalog columns an edit changes; ISRC lives only in the file."""
    fields: dict[str, Any] = {
        name: getattr(values, name)
        for name in ("title", "artist", "album", "genre", "year", "bpm")
        if name in values.changed
    }
    if "key" in values.changed:
        fields["key"], fields["mode"] = values.key if values.key is not None else (-1, -1)
    return fields


def playing_on(stations: Any, path: str) -> str | None:
    """Why *path* cannot be rewritten now, or ``None`` when it can.

    A song playing or up next on any station is refused: in browser
    playback the page reads the file while it plays, and a rewritten file
    would shift the audio under it.
    """
    wanted = normalize_path(path)
    for runtime in stations.all():
        state = getattr(runtime.player, "_state", None)
        for attribute, words in (("current_track", "playing now"), ("next_track", "up next")):
            track = getattr(state, attribute, None)
            track_path = getattr(track, "path", None)
            if isinstance(track_path, str | Path) and normalize_path(track_path) == wanted:
                where = "" if runtime.slug == DEFAULT_SLUG else f" on {runtime.slug}"
                return f"This song is {words}{where}. Save its tags once it has played."
    return None


def install_tag_editor(
    app: FastAPI, bridge: Any, describe: Callable[[Any, str], Awaitable[dict[str, Any]]]
) -> None:
    """Register ``POST /api/library/tags``; the stations come from ``app.state``.

    *describe* gives the Track info record the answer carries, the same one
    ``GET /api/library/points`` answers with.
    """

    @app.post("/api/library/tags")
    async def api_library_save_tags(body: TagEditBody) -> Any:
        library = bridge.library
        if library is None:
            raise HTTPException(status_code=503, detail="Folder library is not attached")
        if library.entry_for_path(body.path) is None:
            raise HTTPException(status_code=404, detail="Recording not in library")
        path = normalize_path(body.path)
        try:
            values = validate_tags(body.tags)
        except TagError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc), "field": exc.field})
        busy = playing_on(app.state.stations, path)
        if busy is not None:
            raise HTTPException(status_code=409, detail=busy)
        try:
            await asyncio.to_thread(write_tags, path, values)
        except TagError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await asyncio.to_thread(library.catalog.retag, path, _catalog_fields(values))
        await asyncio.to_thread(library.refresh)
        beets = "off"
        beets_error = ""
        beets_db = library.cfg.library.beets_db
        if body.beets and beets_db is not None and Path(beets_db).is_file():
            try:
                found = await asyncio.to_thread(
                    update_beets, Path(beets_db), path, library.cfg.library.music_dir, values
                )
                beets = "updated" if found else "not_found"
            except (sqlite3.Error, OSError) as exc:
                logger.warning("Could not update beets for %s: %s", path, exc)
                beets, beets_error = "failed", str(exc)
        record = await describe(library, path)
        record["beets"] = beets
        record["beets_error"] = beets_error
        return record


__all__ = ["TagEditBody", "install_tag_editor", "playing_on", "tag_form"]
