"""Settings, Sound: getting Stereo Tool and giving it a licence key.

The page reads one station's view (``GET /api/stereo-tool``): whether its
Stereo Tool plugin library is there and its version, whether AutoDJ can
download one for this computer, which licence it uses, and what the
running Stereo Tool said of that licence.  The licence key is
write-only: ``PUT /api/stereo-tool/license`` saves one for every station or
for this station only, ``DELETE`` clears it, and no answer ever holds a key.
A key that comes from the server's configuration or environment is
reported, and the page cannot change it.

``POST /api/stereo-tool/download`` is shared by every station: it fetches
Thimeo's plugin zip from thimeo.com and installs the one library this
computer needs (:func:`autodj.stereotool_setup.install`).
It is a browser route like every settings change: it needs a paired
browser session when pairing is on and an allowed Origin, and machine
tokens (analysis workers, the announcer) never reach it.  It takes no file
and no address, so it can only ever install Thimeo's own library.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from autodj import stereotool_setup as setup
from autodj.security import emit_audit, new_request_id
from autodj.station_routes import StationDep, StationRouter

if TYPE_CHECKING:
    from autodj.stations import StationRuntime

logger = logging.getLogger(__name__)

Scope = Literal["all", "station"]


class LicenseBody(BaseModel):
    """A licence key to save; validation errors never repeat it."""

    model_config = ConfigDict(extra="forbid")

    key: StrictStr = Field(max_length=4096)
    scope: Scope = "all"


class DownloadBody(BaseModel):
    """The operator's acceptance of Thimeo's terms, which a download needs."""

    model_config = ConfigDict(extra="forbid")

    accept_terms: StrictBool


def _several_stations(request: Request) -> bool:
    stations = getattr(request.app.state, "stations", None)
    try:
        return stations is not None and len(stations.slugs()) > 1
    except (AttributeError, TypeError):
        return False


def _running(station: StationRuntime) -> dict[str, object] | None:
    """What this station's running Stereo Tool reported, or ``None``.

    ``None`` too when the report is from a Stereo Tool started before the
    last key or library change: it says nothing about the new key yet.
    """
    master = getattr(station.bridge.player, "_continuous_master", None)
    report = getattr(master, "stereotool_report", None)
    if not isinstance(report, dict) or not report:
        return None
    current = setup.generation(station.paths.state_dir)
    return dict(report) if report.get("generation") == current else None


def status(station: StationRuntime, several: bool) -> dict[str, Any]:
    """One station's Stereo Tool library and licence, with no path and no key."""
    cfg = station.bridge.player._cfg
    licence = setup.license_state(
        index_dir=cfg.index.index_dir,
        state_dir=station.paths.state_dir,
        configured_file=cfg.playback.stereotool_license_file,
    )
    running = _running(station)
    licensed = (running or {}).get("licensed")
    unlicensed = (running or {}).get("unlicensed")
    return {
        **setup.program_status(cfg.playback.stereotool_path, cfg.index.index_dir, running=running),
        "license": licence.public(),
        # Stereo Tool's own verdict on the current key once audio has passed;
        # null until the Stereo Tool started with this key has said.
        "running": {
            "licensed": licensed,
            "unlicensed": unlicensed if isinstance(unlicensed, str) else None,
        }
        if isinstance(licensed, bool)
        else None,
        "several_stations": several,
        "terms_url": setup.TERMS_URL,
        "licensing_url": setup.LICENSING_URL,
    }


def _license_path(station: StationRuntime, scope: Scope) -> Path:
    """The file the page may change for *scope*.

    Raises:
        HTTPException: 409 when the configuration or environment sets that licence.
    """
    cfg = station.bridge.player._cfg
    if scope == "station":
        if cfg.playback.stereotool_license_file:
            raise HTTPException(
                status_code=409,
                detail="This station's licence comes from the licence file named in the server "
                "configuration (stereotool_license_file); change it there.",
            )
        return setup.station_license_path(station.paths.state_dir)
    state = setup.license_state(index_dir=cfg.index.index_dir, state_dir=station.paths.state_dir)
    if state.install == "environment":
        raise HTTPException(
            status_code=409,
            detail=f"The licence for every station comes from {setup.LICENSE_ENV} or "
            f"{setup.LICENSE_FILE_ENV} on the server; change it there.",
        )
    return setup.install_license_path(cfg.index.index_dir)


def install_stereo_tool(router: StationRouter) -> None:
    """Register the routes that read and set one station's licence."""

    @router.get("/stereo-tool")
    async def get_stereo_tool(station: StationDep, request: Request) -> dict[str, Any]:
        """Whether Stereo Tool is installed, its version, and which licence this station uses."""
        return await asyncio.to_thread(status, station, _several_stations(request))

    @router.put("/stereo-tool/license")
    async def put_license(
        station: StationDep, body: LicenseBody, request: Request
    ) -> dict[str, Any]:
        """Save a licence key readable only by AutoDJ's user; the answer never holds it."""
        try:
            key = setup.validate_key(body.key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        path = await asyncio.to_thread(_license_path, station, body.scope)
        own = station.paths.state_dir if body.scope == "station" else None
        try:
            changed = await asyncio.to_thread(setup.save_license, path, key, station_dir=own)
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Could not save the licence key: {exc.strerror or type(exc).__name__}.",
            ) from None
        if changed:
            logger.info(
                "Stereo Tool licence saved for %s",
                "this station" if body.scope == "station" else "every station",
            )
        result = await asyncio.to_thread(status, station, _several_stations(request))
        return {**result, "changed": changed}

    @router.delete("/stereo-tool/license")
    async def delete_license(
        station: StationDep, request: Request, scope: Scope = Query("all")
    ) -> dict[str, Any]:
        """Delete a saved licence key; Stereo Tool restarts without it."""
        path = await asyncio.to_thread(_license_path, station, scope)
        own = station.paths.state_dir if scope == "station" else None
        try:
            cleared = await asyncio.to_thread(setup.clear_license, path, station_dir=own)
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Could not clear the licence key: {exc.strerror or type(exc).__name__}.",
            ) from None
        if cleared:
            logger.info(
                "Stereo Tool licence cleared for %s",
                "this station" if scope == "station" else "every station",
            )
        result = await asyncio.to_thread(status, station, _several_stations(request))
        return {**result, "cleared": cleared}


def install_download(app: FastAPI, index_dir: Callable[[], Path | str]) -> None:
    """Register the shared route that installs Stereo Tool from thimeo.com."""

    @app.post("/api/stereo-tool/download")
    async def api_stereo_tool_download(body: DownloadBody, request: Request) -> dict[str, Any]:
        """Download Thimeo's Stereo Tool plugin and install this computer's library."""
        request_id = getattr(request.state, "request_id", None) or new_request_id()
        route = "/api/stereo-tool/download"

        def audit(outcome: str, code: int) -> None:
            emit_audit(
                request_id,
                "stereo-tool-download",
                outcome,
                method="POST",
                route=route,
                status=code,
                level=logging.INFO if code < 400 else logging.WARNING,
            )

        if getattr(request.state, "machine_token", None) is not None:
            # Not reachable: machine tokens work only under their own prefixes.
            audit("rejected", 403)
            raise HTTPException(status_code=403, detail="Only a paired browser can do this")
        if body.accept_terms is not True:
            audit("rejected", 400)
            raise HTTPException(
                status_code=400,
                detail="Tick that you accept Thimeo's terms for Stereo Tool first.",
            )
        try:
            installed = await asyncio.to_thread(setup.install, index_dir())
        except (setup.DownloadBusy, setup.NoBuildError) as exc:
            audit("rejected", 409)
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except setup.DownloadError as exc:
            audit("failed", 502)
            logger.warning("Stereo Tool download failed: %s", exc)
            raise HTTPException(status_code=502, detail=str(exc)) from None
        audit("success", 200)
        return {"version": installed.version}
