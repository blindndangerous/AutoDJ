"""The cart wall: pages of numbered slots that play a clip at once.

A slot names a voice liner (its catalog ID, or its file name in the liner
folder, as ``/api/liners`` lists it) or a clip uploaded to the ``carts``
folder beside ``carts.json``.  Browser requests never carry a path.

Carts reuse the liner playback path.  When the server mixes the audio
(server audio or stream mode) the clip goes through
:class:`~autodj.liner_scheduler.LinerScheduler` into the mix bus; in
browser playback the page fetches the clip and plays it through its own
liner path.  The mode sets the music level while the cart plays: ``duck``
uses the voice liner duck depth, ``over`` leaves the music alone and
``replace`` fades it out.  The song keeps playing silently under a
replacing cart and fades back in when the cart ends.

A slot may have play dates (:mod:`autodj.clip_dates`): before its "not
before" day or after its "do not play after" day, in the station time zone,
the cart is skipped with the reason in words, whether pressed or scheduled.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import date as Date
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import numpy as np
from fastapi import File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from autodj.clip_dates import ClipDates, ClipDatesError, parse_dates, station_today, station_zone
from autodj.liner_files import (
    InvalidLinerName,
    LinerConflictError,
    LinerTooLargeError,
    open_liner_file,
    resolve_liner_path,
    store_liner_upload,
    validate_liner_name,
)
from autodj.liner_scheduler import decode_liner, decode_liner_path
from autodj.scheduler import Action, ActionField
from autodj.station_routes import SavedStationDep, StationDep, StationRouter
from autodj.stereo import SAMPLE_RATE

logger = logging.getLogger(__name__)

PAGE_COUNT = 4
SLOTS_PER_PAGE = 12
MAX_NAME = 80
STATE_FILE = "carts.json"
UPLOAD_FOLDER = "carts"


def _clean_name(value: str) -> str:
    """Trim *value*; refuse a blank name or one with control characters."""
    value = value.strip()
    if not value or any(ord(char) < 32 for char in value):
        raise ValueError("enter a name without control characters")
    return value


class CartDatesError(ClipDatesError):
    """A cart's play dates refused, naming the field.

    ``reports_field`` marks it for the server's 422 handler, which answers
    ``{"detail": ..., "field": ...}`` for it alone; every other 422 keeps
    its list of problems.
    """

    reports_field = True


class CartSlot(BaseModel):
    """One assigned clip and how it plays."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME)
    source: Literal["liner", "upload"]
    clip: str = Field(min_length=1, max_length=200)
    mode: Literal["duck", "over", "replace"] = "duck"
    # Seconds; measured by the server when the slot is saved.
    duration: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    # Play dates, both included; outside them the cart is skipped.
    not_before: Date | None = None
    not_after: Date | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _clean_name(value)

    @model_validator(mode="after")
    def _upload_name(self) -> CartSlot:
        if self.source == "upload":
            validate_liner_name(self.clip)
        # Do not play after must not come before Not before.  The error names
        # its field, so the cart routes' 422 can point the page at it.
        try:
            parse_dates({"not_before": self.not_before, "not_after": self.not_after})
        except ClipDatesError as exc:
            raise CartDatesError(exc.field, exc.message) from None
        return self

    def dates(self) -> ClipDates:
        """The slot's play dates."""
        return ClipDates(not_before=self.not_before, not_after=self.not_after)


class CartPage(BaseModel):
    """A named page of twelve slots."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME)
    slots: list[CartSlot | None] = Field(
        default_factory=lambda: list[CartSlot | None]([None] * SLOTS_PER_PAGE),
        min_length=SLOTS_PER_PAGE,
        max_length=SLOTS_PER_PAGE,
    )

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _clean_name(value)


class CartWall(BaseModel):
    """The saved wall.  ``schema_version`` changes when the format does."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    active_page: int = Field(default=1, ge=1, le=PAGE_COUNT)
    pages: list[CartPage] = Field(
        default_factory=lambda: [CartPage(name=f"Page {i}") for i in range(1, PAGE_COUNT + 1)],
        min_length=PAGE_COUNT,
        max_length=PAGE_COUNT,
    )

    @model_validator(mode="after")
    def _unique_names(self) -> CartWall:
        if len({page.name.casefold() for page in self.pages}) != len(self.pages):
            raise ValueError("page names must be unique")
        return self


class PageName(BaseModel):
    """Request body for renaming a page."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _clean_name(value)


class CartError(ValueError):
    """A cart request that cannot be done; the message is for the operator."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class CartStore:
    """``carts.json`` and the uploads folder.

    Loading never stops startup: a file that does not validate is renamed
    to ``carts.json.bad-<id>`` and the default wall is used.  Saves are
    atomic: a temporary file is written, flushed and moved over the old one.
    """

    def __init__(self, root: Path) -> None:
        self.path = root / STATE_FILE
        self.uploads = root / UPLOAD_FOLDER
        self.wall = self._load()

    def _load(self) -> CartWall:
        try:
            return CartWall.model_validate_json(self.path.read_bytes())
        except FileNotFoundError:
            return CartWall()
        except (OSError, ValueError):
            bad = self.path.with_name(f"{STATE_FILE}.bad-{uuid.uuid4().hex[:8]}")
            try:
                self.path.replace(bad)
            except OSError:
                logger.exception("Could not set aside the damaged cart wall %s", self.path)
            else:
                logger.warning(
                    "The cart wall %s could not be read; it was moved to %s and an empty"
                    " wall was loaded",
                    self.path,
                    bad.name,
                    exc_info=True,
                )
            return CartWall()

    def save(self) -> None:
        """Write the wall atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        part = self.path.with_name(f".{STATE_FILE}.{uuid.uuid4().hex}.tmp")
        try:
            document = self.wall.model_dump(mode="json")
            # A slot without play dates is written without the two keys, so
            # an older AutoDJ, which refuses unknown keys, still reads the wall.
            for page in document["pages"]:
                for assigned in page["slots"]:
                    for key in ("not_before", "not_after"):
                        if assigned is not None and assigned[key] is None:
                            del assigned[key]
            with part.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(part, self.path)
        finally:
            part.unlink(missing_ok=True)

    def page(self, page: int) -> CartPage:
        """Return page *page* (1-based)."""
        if not 1 <= page <= len(self.wall.pages):
            raise CartError(404, f"There is no cart page {page}")
        return self.wall.pages[page - 1]

    def check_slot(self, slot: int) -> None:
        """Refuse a slot number outside the page."""
        if not 1 <= slot <= SLOTS_PER_PAGE:
            raise CartError(404, f"There is no cart {slot}")

    def slot(self, page: int, slot: int) -> CartSlot:
        """Return the clip assigned to *slot* on *page*."""
        self.check_slot(slot)
        assigned = self.page(page).slots[slot - 1]
        if assigned is None:
            raise CartError(404, f"Cart {slot} is empty")
        return assigned

    def upload_names(self) -> list[str]:
        """Uploaded clips, by name; other files in the folder are left out."""
        if not self.uploads.is_dir():
            return []
        names = []
        for path in self.uploads.iterdir():
            try:
                validate_liner_name(path.name)
            except InvalidLinerName:
                continue
            if path.is_file():
                names.append(path.name)
        return sorted(names, key=str.casefold)

    def in_use(self, upload: str) -> bool:
        """Whether a slot plays uploaded clip *upload*."""
        return any(
            item is not None and item.source == "upload" and item.clip == upload
            for page in self.wall.pages
            for item in page.slots
        )


class StationCarts:
    """One station's cart wall, the cart playing on it and its Play cart action."""

    def __init__(
        self,
        bridge: Any,
        publish: Callable[[dict[str, Any]], Awaitable[None]],
        liner_folder: Callable[[], Path],
        state_dir: Path,
        api_root: str = "/api",
    ) -> None:
        """Load the wall from *state_dir*; *publish* tells the station's pages of changes.

        *api_root* is where the station's routes answer (``/api`` for the
        default station, ``/api/stations/<slug>`` for another), so a clip
        URL fetches from this station's carts and liners.
        """
        self.bridge = bridge
        self.publish = publish
        self.liner_folder = liner_folder
        self.api_root = api_root
        self.store = CartStore(state_dir)
        # The cart that is playing, as (page, slot, spoken label), and its end timer.
        self._cart: tuple[int, int, str] | None = None
        self._timer: asyncio.Task[None] | None = None
        self.action = Action(
            "play_cart",
            "Play cart",
            self._scheduled,
            (
                ActionField(
                    "page",
                    "Page",
                    names=lambda: [
                        str(number) for number in range(1, len(self.store.wall.pages) + 1)
                    ],
                ),
                ActionField(
                    "slot",
                    "Cart",
                    choices=tuple(
                        (str(number), str(number)) for number in range(1, SLOTS_PER_PAGE + 1)
                    ),
                ),
            ),
            interrupt="none",
        )

    def liner_file(self, clip: str) -> Path:
        """The file behind liner *clip*, found the way ``/api/liners/file`` finds it."""
        return resolve_liner_path(self.bridge.library, self.liner_folder(), clip)[0]

    def decode(self, assigned: CartSlot) -> np.ndarray:
        """Decode the audio the cart plays (blocking)."""
        if assigned.source == "upload":
            return decode_liner(self.store.uploads, assigned.clip)
        scheduler = self.bridge.liner_scheduler
        if scheduler is not None:
            return scheduler.decode(assigned.clip)
        return decode_liner_path(self.liner_file(assigned.clip))

    def clip_url(self, assigned: CartSlot) -> str:
        """Where browser playback fetches the clip, from this station's routes."""
        base = "/carts/uploads/" if assigned.source == "upload" else "/liners/file/"
        return self.api_root + base + quote(assigned.clip, safe="")

    def duck_db(self, mode: str) -> float:
        """The music level under a cart played in *mode*."""
        if mode == "duck":
            return float(self.bridge.player._cfg.playback.liners_duck_db)
        return 0.0 if mode == "over" else float("-inf")

    def not_on_air(self) -> str | None:
        """Why the server mix cannot air a cart now, or ``None``."""
        bridge = self.bridge
        station = bridge.station
        if station is not None and station.state == "idle":
            return "Nobody is listening, so the cart was not played"
        if bridge.player._state.is_paused:
            return "Playback is paused, so the cart was not played"
        if getattr(bridge.player.bus, "live_on_air", False) is True:
            # AutoDJ does not talk over a live DJ (#48).
            return "A live DJ is on the air, so the cart was not played"
        if station is None and not bridge.player.bus.playing:
            return "Start playback before playing a cart"
        return None

    async def finish(self, event: str) -> bool:
        """Announce that the cart playing ended or was stopped; ``False`` if none was."""
        timer = self._timer
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        self._timer = None
        cart = self._cart
        self._cart = None
        if cart is None:
            return False
        page, slot, label = cart
        await self.publish(
            {"type": "cart", "event": event, "page": page, "slot": slot, "label": label}
        )
        return True

    async def play(self, page: int, slot: int) -> dict[str, Any]:
        """Play the cart in *slot* on *page*.

        Raises:
            CartError: The slot is empty or missing, its clip will not
                decode, or the server mix cannot air it now.
        """
        assigned = self.store.slot(page, slot)
        dates = assigned.dates()
        refusal = None if dates.empty else dates.refusal(station_today(self.bridge.player._cfg))
        if refusal is not None:
            raise CartError(409, f"Cart {slot}, {assigned.name} was skipped: it is {refusal}")
        scheduler = self.bridge.liner_scheduler
        if scheduler is not None and (reason := self.not_on_air()) is not None:
            raise CartError(409, reason)
        try:
            audio = await asyncio.to_thread(self.decode, assigned)
        except Exception as exc:
            logger.warning("Cart %s on page %s could not be decoded", slot, page, exc_info=True)
            raise CartError(404, f"The clip for cart {slot} is missing or unreadable") from exc
        if scheduler is not None:
            await asyncio.to_thread(
                scheduler.play_cart, audio, self.duck_db(assigned.mode), title=assigned.name
            )
        await self.finish("end")
        label = f"Cart {slot}, {assigned.name}"
        seconds = len(audio) / SAMPLE_RATE
        self._cart = (page, slot, label)

        async def ends() -> None:
            await asyncio.sleep(seconds)
            await self.finish("end")

        self._timer = asyncio.create_task(ends())
        await self.publish(
            {"type": "cart", "event": "start", "page": page, "slot": slot, "label": label}
        )
        return {
            "label": label,
            "mode": assigned.mode,
            "duck_db": float(self.bridge.player._cfg.playback.liners_duck_db),
            "duration": seconds,
            # Only browser playback plays the clip on the page.
            "url": None if scheduler is not None else self.clip_url(assigned),
        }

    async def close(self) -> None:
        """Forget the cart playing and cancel its end timer, telling nobody.

        Called as the station stops, before its player goes: the timer
        must not fire, and announce the cart's end, after that.
        """
        timer, self._timer = self._timer, None
        self._cart = None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer

    async def stop(self) -> bool:
        """Stop the cart that is playing; ``False`` if none was."""
        scheduler = self.bridge.liner_scheduler
        if scheduler is not None and self._cart is not None:
            await asyncio.to_thread(scheduler.stop_cart)
        return await self.finish("stop")

    def clip_choices(self) -> dict[str, Any]:
        """Liners and uploads a slot can use (blocking)."""
        liners: list[dict[str, str]] = []
        library = self.bridge.library
        if library is not None:
            for entry in library.entries_snapshot("liner"):
                record = library.catalog.record_for_path(entry.path)
                if record and record.get("kind") == "liner":
                    liners.append({"id": record["id"], "name": Path(record["path"]).name})
        folder = self.liner_folder()
        if folder.is_dir():
            listed = {item["name"] for item in liners}
            for path in sorted(folder.iterdir(), key=lambda item: item.name.casefold()):
                try:
                    validate_liner_name(path.name)
                except InvalidLinerName:
                    continue
                if path.is_file() and path.name not in listed:
                    liners.append({"id": path.name, "name": path.name})
        return {"liners": liners, "uploads": self.store.upload_names()}

    async def changed(self) -> None:
        """Save the wall and tell the station's pages."""
        await asyncio.to_thread(self.store.save)
        await self.publish({"type": "cart_changed"})

    async def _scheduled(self, params: dict[str, str]) -> None:
        if self.bridge.liner_scheduler is None:
            raise ValueError("Scheduled carts need server audio or stream mode")
        await self.play(int(params["page"]), int(params["slot"]))


def _http(exc: CartError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


def install_carts(router: StationRouter) -> None:
    """Add the paired cart routes; each acts on the station its URL names."""

    @router.get("/carts")
    async def get_carts(station: SavedStationDep) -> dict[str, Any]:
        """The wall, and the clips a slot can use."""
        carts = station.carts
        choices = await asyncio.to_thread(carts.clip_choices)
        return {
            "wall": carts.store.wall.model_dump(mode="json"),
            # The station's day, so the page can say which carts are outside their dates.
            "today": station_today(station.cfg).isoformat(),
            # So a page left open past midnight can tell when the day changes.
            "timezone": station_zone(station.cfg),
            **choices,
        }

    @router.put("/carts/pages/{page}/slots/{slot}")
    async def set_slot(
        station: SavedStationDep, page: int, slot: int, body: CartSlot
    ) -> dict[str, Any]:
        """Assign a clip to a slot; the clip must decode."""
        carts = station.carts
        try:
            target = carts.store.page(page)
            carts.store.check_slot(slot)
        except CartError as exc:
            raise _http(exc) from exc
        try:
            audio = await asyncio.to_thread(carts.decode, body)
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail="That clip is missing or is not audio AutoDJ can play"
            ) from exc
        assigned = body.model_copy(update={"duration": round(len(audio) / SAMPLE_RATE, 3)})
        target.slots[slot - 1] = assigned
        await carts.changed()
        return {"slot": assigned.model_dump()}

    @router.delete("/carts/pages/{page}/slots/{slot}")
    async def clear_slot(station: SavedStationDep, page: int, slot: int) -> dict[str, bool]:
        """Empty a slot."""
        carts = station.carts
        try:
            target = carts.store.page(page)
            carts.store.check_slot(slot)
        except CartError as exc:
            raise _http(exc) from exc
        target.slots[slot - 1] = None
        await carts.changed()
        return {"cleared": True}

    @router.put("/carts/pages/{page}/name")
    async def rename_page(station: SavedStationDep, page: int, body: PageName) -> dict[str, str]:
        """Rename a page; names are unique, ignoring case."""
        carts = station.carts
        try:
            target = carts.store.page(page)
        except CartError as exc:
            raise _http(exc) from exc
        if any(
            other is not target and other.name.casefold() == body.name.casefold()
            for other in carts.store.wall.pages
        ):
            raise HTTPException(status_code=409, detail=f"Another page is named {body.name}")
        target.name = body.name
        await carts.changed()
        return {"name": body.name}

    @router.put("/carts/active-page/{page}")
    async def select_page(station: SavedStationDep, page: int) -> dict[str, int]:
        """Remember the page shown; every open page follows."""
        carts = station.carts
        try:
            carts.store.page(page)
        except CartError as exc:
            raise _http(exc) from exc
        carts.store.wall.active_page = page
        await carts.changed()
        return {"active_page": page}

    @router.post("/carts/upload")
    async def upload(station: SavedStationDep, file: UploadFile = File(...)) -> dict[str, Any]:
        """Store an uploaded clip; it must decode, and an existing clip is never replaced."""
        carts = station.carts
        uploads = carts.store.uploads
        name = file.filename or ""
        try:
            path, size = await store_liner_upload(
                uploads,
                name,
                file,
                max_bytes=(station.cfg.station.liner_upload_max_mib * 1024 * 1024),
                replace=False,
            )
        except InvalidLinerName as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except LinerConflictError as exc:
            raise HTTPException(
                status_code=409, detail=f"A clip named {name} is already uploaded"
            ) from exc
        except LinerTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        try:
            await asyncio.to_thread(decode_liner, uploads, name)
        except Exception as exc:
            path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400, detail=f"{name} is not audio AutoDJ can play"
            ) from exc
        await carts.publish({"type": "cart_changed"})
        return {"name": name, "size": size}

    @router.get("/carts/uploads/{name}")
    async def get_upload(station: SavedStationDep, name: str) -> FileResponse:
        """Serve an uploaded clip, for browser playback."""
        uploads = station.carts.store.uploads
        try:
            opened = await asyncio.to_thread(open_liner_file, uploads, name)
        except InvalidLinerName as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Clip not found") from exc
        await asyncio.to_thread(opened.file.close)
        # The page decodes the bytes itself; a guessed type could be one
        # that requestBinary refuses.
        return FileResponse(
            uploads / name,
            media_type="application/octet-stream",
            stat_result=opened.stat_result,
        )

    @router.delete("/carts/uploads/{name}")
    async def delete_upload(station: SavedStationDep, name: str) -> dict[str, str]:
        """Delete an uploaded clip that no slot uses."""
        carts = station.carts
        try:
            validate_liner_name(name)
        except InvalidLinerName as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if carts.store.in_use(name):
            raise HTTPException(status_code=409, detail=f"Clear the carts that play {name} first")
        try:
            await asyncio.to_thread((carts.store.uploads / name).unlink)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Clip not found") from exc
        await carts.publish({"type": "cart_changed"})
        return {"deleted": name}

    @router.post("/carts/pages/{page}/slots/{slot}/play")
    async def play_slot(station: StationDep, page: int, slot: int) -> dict[str, Any]:
        """Play a cart.  In browser playback the reply says where to fetch the clip."""
        try:
            return await station.carts.play(page, slot)
        except CartError as exc:
            raise _http(exc) from exc

    @router.post("/carts/stop")
    async def stop(station: StationDep) -> dict[str, bool]:
        """Stop the cart that is playing."""
        return {"stopped": await station.carts.stop()}
