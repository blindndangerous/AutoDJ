"""The themed show on air: starting, ending and taking over, and the paired routes.

One show runs at a time.  Starting one works out its songs (see
:func:`autodj.shows.match_library`) and hands them to the player as a
:class:`~autodj.player.ShowTheme`, so every pick mode picks inside them;
ending it hands back ``None``.  A show that names a clockwheel switches the
station to it, the way the Schedule's "Change song picking" action does,
and the station's own picking comes back when the run of shows ends.

Timing, per show:

- **Soft start** (the default): the song playing finishes; the next one is
  the first of the show, with the intro liner over it.
- **Hard start**: the playing song fades out at once.
- **Soft end**: no themed song is picked after the end time; the song
  playing finishes, and the outro plays over the next one.
- **Hard end**: the playing song fades out at the end time.

A show starting while another runs takes over: the old one's outro is
skipped, and the new one's intro plays.

The show on air is kept in ``shows.json``, so a restart before its end
carries on with it; one whose end passed while the server was down ends
quietly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from autodj.player import ShowTheme
from autodj.scheduler import Action, ActionField, Scheduler
from autodj.shows import (
    EXAMPLES,
    MAX_MINUTES,
    MAX_SHOWS,
    SCHEMA_VERSION,
    ActiveShow,
    BeetsSource,
    FilterResult,
    Resume,
    Show,
    ShowError,
    ShowsFile,
    ShowSpec,
    ShowStore,
    count_text,
    describe,
    fallback_text,
    field_error,
    library_genres,
    match_library,
    parse_spec,
    pool_warning,
)

logger = logging.getLogger(__name__)

#: How long a hard start or end waits for the next song to be picked again.
RENDER_WAIT_S = 15.0


class StartBody(BaseModel):
    """Start a show now, for a length in minutes."""

    model_config = ConfigDict(extra="forbid")

    minutes: StrictInt = Field(ge=1, le=MAX_MINUTES)


class CheckBody(BaseModel):
    """The filter part of the show form, for the Check button."""

    model_config = ConfigDict(extra="forbid")

    year_from: Any = None
    year_to: Any = None
    genres_include: Any = Field(default_factory=list)
    genres_exclude: Any = Field(default_factory=list)
    query: Any = ""
    minutes: StrictInt = Field(default=60, ge=1, le=MAX_MINUTES)


@dataclass
class ShowHooks:
    """What the show runtime needs from the station; the server fills it in.

    Attributes:
        entries: The library's music.
        beets: The beets database to read, or ``None`` when none is set.
        rules: The station's repeat rules.
        keys: The player's repeat-rule key cache.
        dmca: Whether DMCA mode is on.
        apply_theme: Give the player the show's songs (or ``None``) and
            have the next song picked again.  Called on a worker thread.
        interrupt: Fade out the playing song (a hard start or end), once
            the next song has been picked again.  Called on a worker thread.
        picking: How the station picks now, to come back to.
        use_clock: Switch to a clockwheel from its first step.
        restore: Go back to how the station picked before.
        liner_sources: The registered voice liner folders.
        liners: Every registered voice liner, as ``(id, path)``.
        clock_names: The saved clockwheels.
        decode: A voice liner's audio, by its ID.
        on_started: A show started (the webhook event).
        on_changed: The show on air changed (the public feed).
        publish: Tell every open page that the shows changed.
    """

    entries: Callable[[], Sequence[Any]]
    beets: Callable[[], BeetsSource | None]
    rules: Callable[[], Any]
    keys: Callable[[], Any]
    dmca: Callable[[], bool]
    apply_theme: Callable[[ShowTheme | None], None]
    interrupt: Callable[[], None]
    picking: Callable[[], Resume]
    use_clock: Callable[[str], Awaitable[None]]
    restore: Callable[[Resume], Awaitable[None]]
    liner_sources: Callable[[], list[dict[str, Any]]]
    liners: Callable[[], list[tuple[str, Path]]]
    clock_names: Callable[[], Sequence[str]]
    decode: Callable[[str], Any]
    on_started: Callable[[], None]
    on_changed: Callable[[], None]
    publish: Callable[[dict[str, Any]], Awaitable[None]]


def _inside(path: str, folder: str) -> bool:
    """Whether *path* is *folder* or below it (both catalog-normalized)."""
    base = folder.rstrip("\\/")
    return path == base or path.startswith(base + os.sep) or path.startswith(base + "/")


def end_at_wall_time(value: str, now: datetime, zone: ZoneInfo) -> datetime:
    """The next time the station clock reads *value* (``HH:MM``) after *now*.

    Raises:
        ShowError: *value* is the minute it is now, which would make a
            show of a whole day.
    """
    hours, minutes = (int(part) for part in value.split(":"))
    local = now.astimezone(zone)
    end = local.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    if (end.hour, end.minute) == (local.hour, local.minute):
        raise ShowError("until", "The show would end the minute it starts; choose a later time")
    if end <= local:
        end = (end.replace(tzinfo=None) + timedelta(days=1)).replace(tzinfo=zone)
    return end


class ShowManager:
    """Own the shows, the one on air and its intro or outro.

    Every method that changes something runs on the event loop and takes
    one lock, so a scheduled start, a button and the end time never
    interleave.  The player, the liner scheduler and the feed read
    :meth:`state`, :meth:`due_audio` and :meth:`liners_for_now` from their
    own threads; those only read references that are replaced whole.
    """

    def __init__(
        self,
        path: Path,
        timezone: str,
        hooks: ShowHooks,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = ShowStore(path)
        self.timezone = timezone
        self.zone = ZoneInfo(timezone)
        self.hooks = hooks
        self._clock = clock
        self.doc = ShowsFile(schema_version=SCHEMA_VERSION, shows=[])
        self._ops = asyncio.Lock()
        self._clip_lock = threading.Lock()
        self._pending_clip: str | None = None
        self._folders: dict[str, str] = {}
        self._match: FilterResult | None = None

    # -- storage --------------------------------------------------------

    def _save(self) -> None:
        self.store.save(self.doc)

    def _refresh_folders(self) -> None:
        """Work out each show's liner folder as a catalog path."""
        from autodj.catalog import normalize_path
        from autodj.clockwheel_store import source_folder

        sources = {source["id"]: source for source in self.hooks.liner_sources()}
        folders: dict[str, str] = {}
        for show in self.doc.shows:
            chosen = show.liner_folder
            source = sources.get(chosen.source) if chosen is not None else None
            if chosen is not None and source is not None:
                folders[show.id] = normalize_path(source_folder(source["path"], chosen.folder))
        self._folders = folders

    async def start_up(self) -> None:
        """Load the file; carry on with the show on air, or end it if its time passed."""
        async with self._ops:
            self.doc = await asyncio.to_thread(self.store.load)
            await asyncio.to_thread(self._refresh_folders)
            active = self.doc.active
            if active is None:
                return
            show = self._find(active.show_id)
            if show is None or active.ends_at <= self._clock():
                logger.info("Show %s ended while the server was not running", active.name)
                if active.resume is not None:
                    await self._restore(active.resume)
                self.doc.active = None
                await asyncio.to_thread(self._save)
                return
            # The station already plays the show's clockwheel, at its place.
            current_clock = show.clockwheel if active.resume is not None else None
            try:
                await self._apply(show, active, current_clock)
            except ShowError as exc:
                logger.warning("Show %s could not carry on: %s", show.name, exc)
                if active.resume is not None:
                    await self._restore(active.resume)
                self.doc.active = None
                await asyncio.to_thread(self._save)
                return
            logger.info("Show %s carries on until %s", show.name, self._local(active.ends_at))

    # -- reading --------------------------------------------------------

    def _find(self, show_id: str) -> Show | None:
        return next((show for show in self.doc.shows if show.id == show_id), None)

    def get(self, show_id: str) -> Show:
        """The show with *show_id*; ``KeyError`` when there is none."""
        show = self._find(show_id)
        if show is None:
            raise KeyError(show_id)
        return show

    def choices(self) -> list[tuple[str, str]]:
        """``(id, name)`` of every show, for the Schedule's Start show action."""
        return [(show.id, show.name) for show in self.doc.shows]

    def _local(self, when: datetime) -> str:
        return when.astimezone(self.zone).strftime("%H:%M")

    def state(self) -> dict[str, Any] | None:
        """The show on air for the pages, or ``None``.  Times are in the station zone."""
        active = self.doc.active
        if active is None:
            return None
        return {
            "id": active.show_id,
            "name": active.name,
            "started_at": active.started_at.astimezone(self.zone).isoformat(),
            "ends_at": active.ends_at.astimezone(self.zone).isoformat(),
        }

    def public_state(self) -> dict[str, str] | None:
        """The show on air for listeners: its name and end time, nothing else."""
        state = self.state()
        return None if state is None else {"name": state["name"], "ends_at": state["ends_at"]}

    def _liner_name(self, liner_id: str) -> str:
        for key, path in self.hooks.liners():
            if key == liner_id:
                return Path(path).name
        return "a liner that is no longer registered"

    def view(self, show: Show) -> dict[str, Any]:
        """A show for the Shows list, with its summary in words."""
        return {**show.model_dump(mode="json"), "summary": describe(show, self._liner_name)}

    def options(self) -> dict[str, Any]:
        """The genres, liners, liner folders and clockwheels the form chooses from."""
        return {
            "genres": library_genres(self.hooks.entries()),
            "liners": [
                {"id": key, "name": Path(path).name}
                for key, path in sorted(self.hooks.liners(), key=lambda item: Path(item[1]).name)
            ],
            "liner_folders": [
                {"id": source["id"], "path": source["path"]}
                for source in self.hooks.liner_sources()
            ],
            "clockwheels": list(self.hooks.clock_names()),
        }

    # -- checking -------------------------------------------------------

    def _check_live(self, spec: ShowSpec) -> None:
        """Refuse a clockwheel, liner or folder that does not exist now."""
        if spec.clockwheel is not None and spec.clockwheel not in self.hooks.clock_names():
            raise ShowError("clockwheel", f"There is no clockwheel named {spec.clockwheel}")
        ids = {key for key, _path in self.hooks.liners()}
        for name in ("intro", "outro"):
            value = getattr(spec, name)
            if value is not None and value not in ids:
                raise ShowError(name, "That voice liner is no longer registered; choose another")
        folder = spec.liner_folder
        if folder is not None and folder.source not in {
            source["id"] for source in self.hooks.liner_sources()
        }:
            raise ShowError(
                "liner_folder",
                "That voice liner folder is no longer registered; choose one from Library "
                "tools, Voice liner folders",
            )

    def _matching(self, spec: ShowSpec) -> FilterResult:
        return match_library(spec, self.hooks.entries(), self.hooks.beets())

    def _report(self, result: FilterResult, minutes: int) -> dict[str, Any]:
        """A filter's result in numbers and words, for the form."""
        warning = None
        if not result.errors:
            warning = pool_warning(
                result.matched,
                self.hooks.rules(),
                minutes,
                self.hooks.keys(),
                dmca=self.hooks.dmca(),
            )
        fallback = len(result.fallback)
        return {
            "count": result.count,
            "count_text": count_text(result.count),
            "fallback": fallback,
            "fallback_text": fallback_text(fallback),
            "fallback_examples": [
                getattr(entry, "display_name", entry.path) for entry in result.fallback[:EXAMPLES]
            ],
            "warning": warning,
            "errors": [{"field": name, "message": message} for name, message in result.errors],
            "beets": result.beets,
        }

    async def check(self, body: CheckBody) -> dict[str, Any]:
        """Count what a filter matches, the fallbacks and any warning, without saving."""
        filters = body.model_dump(exclude={"minutes"})
        spec = parse_spec({"name": "Check", **filters})
        result = await asyncio.to_thread(self._matching, spec)
        return await asyncio.to_thread(self._report, result, body.minutes)

    # -- editing --------------------------------------------------------

    async def _checked(self, spec: ShowSpec, show_id: str | None) -> tuple[FilterResult, dict]:
        taken = {show.name.casefold() for show in self.doc.shows if show.id != show_id}
        if spec.name.casefold() in taken:
            raise ShowError("name", f"A show named {spec.name} already exists")
        await asyncio.to_thread(self._check_live, spec)
        result = await asyncio.to_thread(self._matching, spec)
        if result.errors:
            raise ShowError(*result.errors[0])
        report = await asyncio.to_thread(self._report, result, spec.default_minutes)
        return result, report

    async def _changed(self) -> None:
        await asyncio.to_thread(self._save)
        await asyncio.to_thread(self._refresh_folders)
        await self.hooks.publish({"type": "shows_changed"})

    async def add(self, spec: ShowSpec) -> tuple[Show, dict[str, Any]]:
        """Save a new show; raise :class:`ShowError` when it is not valid."""
        async with self._ops:
            if len(self.doc.shows) >= MAX_SHOWS:
                raise ShowError("", f"AutoDJ keeps at most {MAX_SHOWS} shows")
            _result, report = await self._checked(spec, None)
            show = Show(**spec.model_dump(), id=uuid.uuid4().hex)
            self.doc.shows = [*self.doc.shows, show]
            await self._changed()
            return show, report

    async def replace(self, show_id: str, spec: ShowSpec) -> tuple[Show, dict[str, Any]]:
        """Replace a show; ``KeyError`` for an unknown ID.

        A show on air takes the change at once: its songs, clockwheel and
        liner folder, from the next song on, until the same end time.
        """
        async with self._ops:
            old = self.get(show_id)
            result, report = await self._checked(spec, show_id)
            active = self.doc.active
            on_air = active is not None and active.show_id == show_id
            if on_air and not result.count:
                raise ShowError(
                    "",
                    f"No song matches the changed filter, and {old.name} is on air. "
                    "Stop the show first, or change the filter",
                )
            show = Show(**spec.model_dump(), id=show_id)
            self.doc.shows = [show if item.id == show_id else item for item in self.doc.shows]
            if on_air and active is not None:
                run = active.model_copy(update={"name": show.name, "run_id": uuid.uuid4().hex})
                current_clock = old.clockwheel if active.resume is not None else None
                await self._apply(show, run, current_clock)
                self.hooks.on_changed()  # the feed names the show
            await self._changed()
            return show, report

    async def remove(self, show_id: str) -> Show:
        """Delete a show, ending it first when it is on air; ``KeyError`` for an unknown ID."""
        async with self._ops:
            show = self.get(show_id)
            active = self.doc.active
            if active is not None and active.show_id == show_id:
                await self._end(show, reason="deleted")
            self.doc.shows = [item for item in self.doc.shows if item.id != show_id]
            await self._changed()
            return show

    # -- running --------------------------------------------------------

    async def _restore(self, resume: Resume) -> None:
        try:
            await self.hooks.restore(resume)
        except Exception:
            logger.exception("Could not bring back the song picking from before the show")

    async def _apply(self, show: Show, run: ActiveShow, current_clock: str | None) -> None:
        """Make *run* of *show* the one on air: its songs, clockwheel and liners.

        *current_clock* is the clockwheel a show switched the station to,
        which keeps playing at its place when this show uses it too.

        Raises:
            ShowError: The filter matches nothing or cannot be read, or the
                clockwheel cannot play; nothing changed.
        """
        result = await asyncio.to_thread(self._matching, show)
        if result.errors:
            raise ShowError(*result.errors[0])
        if not result.count:
            raise ShowError("", f"No song matches {show.name}, so it cannot start")
        resume = run.resume
        if show.clockwheel:
            if resume is None:
                resume = self.hooks.picking()
            if current_clock != show.clockwheel:
                try:
                    await self.hooks.use_clock(show.clockwheel)
                except ShowError:
                    raise
                except Exception as exc:
                    raise ShowError("clockwheel", str(exc) or type(exc).__name__) from exc
        elif resume is not None:
            await self._restore(resume)
            resume = None
        run = run.model_copy(update={"resume": resume})
        self._match = result
        self.doc.active = run
        theme = ShowTheme(run.run_id, show.name, result.paths)
        await asyncio.to_thread(self.hooks.apply_theme, theme)
        if result.fallback:
            logger.info(
                "Show %s: %s",
                show.name,
                fallback_text(len(result.fallback)),
            )

    async def start(self, show_id: str, ends_at: datetime) -> ActiveShow:
        """Start *show_id* until *ends_at*, taking over from a show on air.

        Raises:
            KeyError: There is no such show.
            ShowError: It cannot start, in words.
        """
        async with self._ops:
            show = self.get(show_id)
            now = self._clock().replace(microsecond=0)
            ends_at = ends_at.replace(microsecond=0)
            if ends_at <= now:
                raise ShowError("minutes", "The end time has passed")
            previous = self.doc.active
            run = ActiveShow(
                show_id=show.id,
                run_id=uuid.uuid4().hex,
                name=show.name,
                started_at=now,
                ends_at=ends_at,
                resume=previous.resume if previous is not None else None,
            )
            current_clock = None
            if previous is not None and previous.resume is not None:
                before = self._find(previous.show_id)
                current_clock = before.clockwheel if before is not None else None
            await self._apply(show, run, current_clock)
            with self._clip_lock:
                # A takeover skips the old show's outro (and any intro not aired).
                self._pending_clip = show.intro
            await asyncio.to_thread(self._save)
            if previous is not None:
                logger.info("Show %s takes over from %s", show.name, previous.name)
            logger.info("Show %s starts, until %s", show.name, self._local(ends_at))
            if show.start == "hard":
                await asyncio.to_thread(self.hooks.interrupt)
            self.hooks.on_started()
            await self.hooks.publish({"type": "shows_changed"})
            assert self.doc.active is not None
            return self.doc.active

    async def _end(self, show: Show | None, *, reason: str) -> None:
        """End the show on air (hold ``_ops``)."""
        active = self.doc.active
        if active is None:
            return
        await asyncio.to_thread(self.hooks.apply_theme, None)
        if active.resume is not None:
            await self._restore(active.resume)
        self.doc.active = None
        self._match = None
        with self._clip_lock:
            self._pending_clip = show.outro if show is not None else None
        await asyncio.to_thread(self._save)
        logger.info("Show %s %s", active.name, reason)
        if show is not None and show.end == "hard":
            await asyncio.to_thread(self.hooks.interrupt)
        self.hooks.on_changed()

    async def stop(self) -> ActiveShow | None:
        """End the show on air now, as its end time would; ``None`` when none is on air."""
        async with self._ops:
            active = self.doc.active
            if active is None:
                return None
            await self._end(self._find(active.show_id), reason="stopped")
        await self.hooks.publish({"type": "shows_changed"})
        return active

    async def tick(self) -> None:
        """End the show on air once its end time comes (once a second)."""
        active = self.doc.active
        if active is None or self._clock() < active.ends_at:
            return
        async with self._ops:
            active = self.doc.active
            if active is None or self._clock() < active.ends_at:
                return
            await self._end(self._find(active.show_id), reason="ended")
        await self.hooks.publish({"type": "shows_changed"})

    # -- read by other threads -----------------------------------------

    def due_audio(self) -> Any:
        """The intro or outro waiting for the next song start, once (liner thread)."""
        with self._clip_lock:
            clip, self._pending_clip = self._pending_clip, None
        if clip is None:
            return None
        try:
            return self.hooks.decode(clip)
        except Exception:
            logger.warning("Could not play the show's intro or outro liner", exc_info=True)
            return None

    def liners_for_now(self, liners: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
        """The automatic liners: the show's folder while it runs; never a show's otherwise."""
        from autodj.catalog import normalize_path

        folders = self._folders
        if not folders:
            return liners
        active = self.doc.active
        own = folders.get(active.show_id) if active is not None else None
        if own is not None:
            return [item for item in liners if _inside(normalize_path(item[1]), own)]
        claimed = list(folders.values())
        return [
            item
            for item in liners
            if not any(_inside(normalize_path(item[1]), folder) for folder in claimed)
        ]

    def active_view(self) -> dict[str, Any] | None:
        """The show on air for the paired Shows panel."""
        state = self.state()
        if state is None:
            return None
        match = self._match
        return {
            **state,
            "count": match.count if match is not None else 0,
            "fallback": len(match.fallback) if match is not None else 0,
        }


def _rejected(field: str, message: str, status: int = 400) -> JSONResponse:
    """A 400 whose ``field`` lets the page focus the field at fault."""
    return JSONResponse(status_code=status, content={"detail": message, "field": field})


def install_shows(
    app: FastAPI, scheduler: Scheduler, path: Path, timezone: str, hooks: ShowHooks
) -> ShowManager:
    """Create the show manager, its paired routes and the Start show action.

    The app's lifespan calls :meth:`ShowManager.start_up`, and the
    schedule timer calls :meth:`ShowManager.tick`.
    """
    manager = ShowManager(path, timezone, hooks)

    async def scheduled(params: dict[str, str]) -> None:
        ends_at = end_at_wall_time(params["until"], datetime.now(UTC), manager.zone)
        try:
            await manager.start(params["show"], ends_at)
        except KeyError:
            raise ValueError("That show no longer exists") from None

    scheduler.register(
        Action(
            "show_start",
            "Start show",
            scheduled,
            (
                ActionField("show", "Show", more=manager.choices),
                ActionField("until", "Ends at", kind="time"),
            ),
            interrupt="none",
            own_timing=True,
        )
    )

    def unknown() -> HTTPException:
        return HTTPException(status_code=404, detail="That show no longer exists")

    def listing() -> dict[str, Any]:
        return {
            "timezone": manager.timezone,
            "shows": [manager.view(show) for show in manager.doc.shows],
            "active": manager.active_view(),
            "options": manager.options(),
        }

    @app.get("/api/shows")
    async def shows_list() -> dict[str, Any]:
        """Return the shows, the one on air and what the form chooses from."""
        return await asyncio.to_thread(listing)

    @app.post("/api/shows", status_code=201, response_model=None)
    async def shows_create(body: dict[str, Any] = Body(...)) -> dict[str, Any] | JSONResponse:
        """Add a show."""
        try:
            show, report = await manager.add(parse_spec(body))
        except ShowError as exc:
            return _rejected(exc.field, str(exc))
        return {"show": manager.view(show), "check": report}

    @app.post("/api/shows/check", response_model=None)
    async def shows_check(body: dict[str, Any] = Body(...)) -> dict[str, Any] | JSONResponse:
        """Count the songs a filter matches, without saving it."""
        try:
            checked = CheckBody.model_validate(body)
            return await manager.check(checked)
        except ValidationError as exc:
            return _rejected(*field_error(exc))
        except ShowError as exc:
            return _rejected(exc.field, str(exc))

    @app.post("/api/shows/stop")
    async def shows_stop() -> dict[str, Any]:
        """End the show on air now."""
        ended = await manager.stop()
        if ended is None:
            raise HTTPException(status_code=409, detail="No show is on air")
        return {"stopped": ended.show_id, "name": ended.name}

    @app.put("/api/shows/{show_id}", response_model=None)
    async def shows_update(
        show_id: str, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any] | JSONResponse:
        """Replace a show."""
        try:
            show, report = await manager.replace(show_id, parse_spec(body))
        except KeyError:
            raise unknown() from None
        except ShowError as exc:
            return _rejected(exc.field, str(exc))
        return {"show": manager.view(show), "check": report}

    @app.delete("/api/shows/{show_id}")
    async def shows_delete(show_id: str) -> dict[str, str]:
        """Delete a show."""
        try:
            show = await manager.remove(show_id)
        except KeyError:
            raise unknown() from None
        return {"deleted": show.id, "name": show.name}

    @app.post("/api/shows/{show_id}/start", response_model=None)
    async def shows_start(show_id: str, body: StartBody) -> dict[str, Any] | JSONResponse:
        """Start a show now for a length in minutes."""
        ends_at = datetime.now(UTC) + timedelta(minutes=body.minutes)
        try:
            await manager.start(show_id, ends_at)
        except KeyError:
            raise unknown() from None
        except ShowError as exc:
            return _rejected(exc.field, str(exc))
        return manager.active_view() or {}

    return manager
