"""The paired route that edits a recording's tags from Track info (#53).

``POST /api/library/tags`` takes the recording's path and only the tags the
page changed.  By default it stores them in the catalog only and leaves the
file alone (#258).  With ``write_file`` it also refuses a song that is
playing or up next on any station, writes the file safely
(:func:`autodj.tag_edit.write_tags`) and stores the file's new identity.
Neither analyses the audio again; both reload the library.  A refused field answers 400 with
``detail`` and the ``field`` at fault, so the page can focus it.

The library is shared by every station, so this is a shared route.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Iterator
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

# The largest body the route reads: eight fields of at most 500 characters,
# even with every character escaped, plus the path.
TAG_BODY_MAX_BYTES = 32 * 1024


class TagEditBody(BaseModel):
    """The recording and the tags to change; tags left out stay as they are."""

    model_config = ConfigDict(extra="forbid")
    path: str
    tags: dict[str, StrictStr]
    beets: StrictBool = False
    write_file: StrictBool = False


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


def _same_file(first: str | Path, second: str | Path) -> bool:
    """Whether two paths name one file, through links and letter case."""
    try:
        return os.path.samefile(first, second)
    except OSError:
        return normalize_path(os.path.realpath(first)) == normalize_path(os.path.realpath(second))


def _on_air(player: Any) -> Iterator[tuple[str, Any]]:
    """Each track a station is playing or about to read, with how to say it.

    Browser playback reads ``current_track`` and ``next_track`` while they
    play.  The mix bus also reads the render cursor (``_pending_entry``),
    the render playing and the renders waiting in the render-ahead worker.
    """
    state = getattr(player, "_state", None)
    playing = getattr(player, "_playing_render", None)
    yield "playing now", getattr(state, "current_track", None)
    yield "playing now", getattr(playing, "entry", None)
    yield "up next", getattr(state, "next_track", None)
    yield "up next", getattr(playing, "next_entry", None)
    yield "up next", getattr(player, "_pending_entry", None)
    ahead = getattr(player, "_render_ahead", None)
    for name in ("_ready", "_fallback"):
        render = getattr(ahead, name, None)
        yield "up next", getattr(render, "entry", None)
        yield "up next", getattr(render, "next_entry", None)


def playing_on(stations: Any, path: str | Path) -> str | None:
    """Why *path* cannot be rewritten now, or ``None`` when it can.

    A song playing or up next on any station is refused: in browser
    playback the page reads the file while it plays, and a rewritten file
    would shift the audio under it.  Paths are compared as files, so a link
    or a different letter case names the same song.
    """
    for runtime in stations.all():
        for words, track in _on_air(runtime.player):
            track_path = getattr(track, "path", None)
            if isinstance(track_path, str | Path) and _same_file(track_path, path):
                where = "" if runtime.slug == DEFAULT_SLUG else f" on {runtime.slug}"
                return f"This song is {words}{where}. Save its tags once it has played."
    return None


@contextlib.contextmanager
def _held_still(stations: Any, target: Path) -> Iterator[None]:
    """Keep every station from moving on while *target* is checked and renamed.

    Browser playback promotes the next song under ``Player._advance_lock``;
    holding each station's lock (in station order, never two at once
    anywhere else) closes the gap between the last check and the rename.
    A song that becomes current or next after the rename reads the new file
    whole, which is safe.
    """
    with contextlib.ExitStack() as stack:
        for runtime in stations.all():
            lock = getattr(runtime.player, "_advance_lock", None)
            if lock is not None:
                stack.enter_context(lock)
        busy = playing_on(stations, target)
        if busy is not None:
            raise TagError(busy)
        yield


_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def _lock_for(path: str | Path) -> threading.Lock:
    """One lock per file, held across its write and its catalog update."""
    key = normalize_path(os.path.realpath(path))
    with _path_locks_guard:
        return _path_locks.setdefault(key, threading.Lock())


def _save(
    library: Any,
    stations: Any,
    path: str,
    values: TagValues,
    beets_db: Path | None,
    write_file: bool,
) -> tuple[str, str, str]:
    """Store the tags in the catalog, and the file and beets when asked, per file."""
    warning = ""
    beets = "off"
    beets_error = ""

    roots = [source["path"] for source in library.catalog.sources()]
    # The edit mark keeps a scan from storing the rewritten file as a new
    # recording in the moment between the rename and retag.
    with _lock_for(path), library.catalog.editing(path):
        if not write_file:
            try:
                stored = library.catalog.retag(path, _catalog_fields(values))
            except OSError as exc:
                raise TagError(f"AutoDJ could not read the file: {exc}") from exc
            if not stored:
                raise TagError("This recording is no longer in the library.")
        else:
            write_tags(
                path, values, roots=roots, guard=lambda target: _held_still(stations, target)
            )
            try:
                library.catalog.retag(path, _catalog_fields(values))
            except Exception as exc:  # the file is written: say so, do not fail
                logger.exception("Tags of %s were written but the catalog was not updated", path)
                warning = (
                    f"The tags were saved in the file, but AutoDJ could not update its library "
                    f"({exc}). A rescan reads the new tags and analyses the song again."
                )
        if beets_db is not None:
            try:
                beets = update_beets(beets_db, path, library.cfg.library.music_dir, values)
            except (sqlite3.Error, OSError) as exc:
                logger.warning("Could not update beets for %s: %s", path, exc)
                beets, beets_error = "failed", str(exc)
    try:
        library.refresh()
    except Exception as exc:
        logger.exception("Library reload after a tag edit of %s failed", path)
        warning = warning or (
            f"The tags were saved, but the library did not reload ({exc}). "
            "They show after the next reload or rescan."
        )
    return warning, beets, beets_error


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
        if await asyncio.to_thread(library.entry_for_path, body.path) is None:
            raise HTTPException(status_code=404, detail="Recording not in library")
        path = normalize_path(body.path)
        try:
            values = validate_tags(body.tags)
        except TagError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc), "field": exc.field})
        if not body.write_file and "isrc" in values.changed:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "ISRC is kept only in the file. Check Also save the tags to "
                    "the file to change it.",
                    "field": "isrc",
                },
            )
        stations = app.state.stations
        # Only a file rewrite disturbs a song on air; AutoDJ's own copy can change.
        if body.write_file:
            busy = await asyncio.to_thread(playing_on, stations, path)
            if busy is not None:
                raise HTTPException(status_code=409, detail=busy)
        beets_db = library.cfg.library.beets_db
        if not body.beets or beets_db is None or not Path(beets_db).is_file():
            beets_db = None
        try:
            warning, beets, beets_error = await asyncio.to_thread(
                _save,
                library,
                stations,
                path,
                values,
                Path(beets_db) if beets_db is not None else None,
                body.write_file,
            )
        except TagError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        record = await describe(library, path)
        record["beets"] = beets
        record["beets_error"] = beets_error
        record["warning"] = warning
        return record


__all__ = [
    "TAG_BODY_MAX_BYTES",
    "TagEditBody",
    "install_tag_editor",
    "playing_on",
    "tag_form",
]
