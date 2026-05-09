"""FastAPI web server and PlayerBridge for the autodj serve command.

Architecture
------------
``autodj serve`` runs a single process:

- :class:`Player` runs in a background daemon thread (blocking loop).
- :class:`PlayerBridge` is a thin, thread-safe adapter between the Player and
  the FastAPI app.
- FastAPI + uvicorn run on the main thread (asyncio event loop).
- A startup asyncio task broadcasts the current player state to all connected
  WebSocket clients once per second.

Routes
------
``GET  /``                  → index.html
``GET  /api/status``        → JSON state snapshot
``GET  /api/version``       → {version, commit, built_at} for footer build stamp
``POST /api/skip``          → skip to next track
``POST /api/pause``         → toggle pause / resume
``POST /api/volume``        → set volume (body: ``{"volume": 0.75}``)
``POST /api/mute``          → toggle mute
``GET  /api/search?q=``     → fuzzy search of indexed tracks
``POST /api/play-next``     → queue a track by path to play after the current one
``WS   /ws``                → push state JSON every 1 second
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json as _json
import logging
import threading
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# PlayerBridge lives in autodj._bridge so neither file balloons over
# the 2000-line working budget.  Re-export here so the external API
# (``from autodj.server import PlayerBridge``) keeps working unchanged.
from autodj._bridge import PlayerBridge

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig
    from autodj.indexer import IndexEntry
    from autodj.presets import Preset
    from autodj.similarity import SimilarityIndex

logger = logging.getLogger(__name__)


@functools.cache
def _version_info() -> dict[str, str]:
    """Return {version, commit, built_at} for the running build.

    Cached on first call so the timestamp reflects when the currently
    installed bundle was produced (preferring static_dist/app.js mtime,
    falling back to the source tree, then to process start time).
    Commit is the short SHA from `git rev-parse` when the source tree
    is a git checkout, else "unknown".
    """
    import datetime as _dt
    import importlib.metadata as _md
    import subprocess as _sp  # nosec B404 - trusted invocation (git, fixed argv)

    here = Path(__file__).parent
    try:
        version = _md.version("autodj")
    except _md.PackageNotFoundError:
        version = "0.0.0"

    commit = "unknown"
    try:
        # Static argv, no shell, no untrusted input.  git on PATH is a
        # build-environment expectation; absence falls through to the
        # "unknown" placeholder.
        out = _sp.check_output(  # nosec B603 B607
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=here,
            stderr=_sp.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
        if out:
            commit = out
    except (OSError, _sp.SubprocessError):
        pass

    built_at: str | None = None
    for candidate in (here / "static_dist" / "app.js", here / "static" / "app.js"):
        if candidate.exists():
            built_at = _dt.datetime.fromtimestamp(candidate.stat().st_mtime, _dt.UTC).isoformat(
                timespec="seconds"
            )
            break
    if built_at is None:
        built_at = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")

    return {"version": version, "commit": commit, "built_at": built_at}


__all__ = ["PlayerBridge", "create_app", "serve"]


class VolumeBody(BaseModel):
    """Request body for POST /api/volume."""

    volume: float


class ProfileSaveBody(BaseModel):
    """Request body for POST /api/profiles.

    All snapshot fields are optional except ``name``; missing fields
    mean "do not override" when the profile is later applied.
    """

    name: str
    index_name: str | None = None
    preset: str | None = None
    bpm_lo: float | None = None
    bpm_hi: float | None = None
    harmonic_mode: str | None = None
    transition_mode: str | None = None
    beat_sync_fx: bool | None = None
    key_sync_fx: bool | None = None
    beatmatch_on_skip: bool | None = None
    crossfade_seconds: float | None = None
    fade_in_seconds: float | None = None
    smart_shuffle: bool | None = None
    pure_shuffle: bool | None = None
    anchor_to_seed: bool | None = None
    enable_daypart: bool | None = None
    enable_mood_arc: bool | None = None
    mood_arc_hours: float | None = None
    liners_enabled: bool | None = None
    liners_pick_mode: str | None = None


class SeekBody(BaseModel):
    """Request body for POST /api/seek.

    Either ``seconds`` (absolute) or ``delta`` (relative) is set.  When
    both are provided, ``seconds`` wins.
    """

    seconds: float | None = None
    delta: float | None = None


class PlayNextBody(BaseModel):
    """Request body for POST /api/play-next."""

    path: str
    now: bool = False  # if True, also skip the current track


class QueueReorderBody(BaseModel):
    """Request body for POST /api/queue/reorder.

    Attributes:
        paths: New ordering of the queue, expressed as a list of
            :attr:`~autodj.indexer.IndexEntry.path` strings.  The first
            entry plays after the current track finishes.
    """

    paths: list[str]


class EqBody(BaseModel):
    """Request body for POST /api/eq.

    Each gain is a linear multiplier in [0.0, 2.0]; 1.0 = unity.
    Omitted fields are left unchanged.
    """

    low: float | None = None
    mid: float | None = None
    high: float | None = None


class PresetBody(BaseModel):
    """Request body for POST /api/preset — empty / null name clears."""

    name: str | None = None


class TransitionBody(BaseModel):
    """Request body for POST /api/transition."""

    effect: str


class DjMixBody(BaseModel):
    """Request body for POST /api/djmix — only set fields are applied."""

    harmonic_mixing: bool | None = None
    harmonic_mode: str | None = None
    beatmatch: bool | None = None
    phrase_align: bool | None = None
    outro_intro_align: bool | None = None
    filter_sweep: bool | None = None


class PlaybackSettingsBody(BaseModel):
    """Request body for POST /api/playback-settings."""

    crossfade_seconds: float | None = None
    fade_in_seconds: float | None = None
    crossfade_eq_duck: bool | None = None
    smart_shuffle: bool | None = None
    pure_shuffle: bool | None = None
    anchor_to_seed: bool | None = None
    replaygain_enabled: bool | None = None
    transition_mode: str | None = None
    key_notation: str | None = None
    key_prefer_flats: bool | None = None
    show_lyrics: bool | None = None
    enable_daypart: bool | None = None
    enable_mood_arc: bool | None = None
    mood_arc_hours: float | None = None
    import_external_cues: bool | None = None
    beat_sync_fx: bool | None = None
    key_sync_fx: bool | None = None
    beatmatch_on_skip: bool | None = None
    liners_enabled: bool | None = None
    liners_folder: str | None = None
    liners_every_n_songs: int | None = None
    liners_every_minutes: float | None = None
    liners_random_min_minutes: float | None = None
    liners_random_max_minutes: float | None = None
    liners_pick_mode: str | None = None
    liners_duck_db: float | None = None


class BpmRangeBody(BaseModel):
    """Request body for POST /api/bpm-range — both null = clear filter."""

    lo: float | None = None
    hi: float | None = None


class DiscoveryBody(BaseModel):
    """Request body for POST /api/discovery — null disables."""

    every: int | None = None


class LibraryJobBody(BaseModel):
    """Request body for POST /api/library/run — invokes a CLI subcommand."""

    name: str
    args: list[str] = []


# ---------------------------------------------------------------------------
# FastAPI application factory
# ---------------------------------------------------------------------------


def create_app(bridge: PlayerBridge) -> FastAPI:
    """Create and return the FastAPI application.

    Args:
        bridge: A fully initialised :class:`PlayerBridge`.

    Returns:
        A configured :class:`fastapi.FastAPI` instance.
    """
    # Connected WebSocket clients — populated at runtime
    _ws_clients: set[WebSocket] = set()
    _ws_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifespan (replaces deprecated on_event)
    # ------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        broadcast = asyncio.create_task(_broadcast_loop())
        watcher = asyncio.create_task(_index_watcher_loop())
        try:
            yield
        finally:
            # Graceful teardown.  Order matters:
            #   1. Tell the Player thread to leave its wait-loop.  It is
            #      a daemon thread so the process can exit either way,
            #      but a clean stop lets pynput / sounddevice release
            #      OS handles instead of being torn down mid-call.
            #   2. Flush the DJ-meta sidecar so any cues / beat grids
            #      computed in background threads land on disk.
            #   3. Cancel the broadcast + watcher tasks last and await
            #      them with CancelledError suppressed so asyncio does
            #      not log "Task was destroyed but it is pending" on
            #      Ctrl+C exit.
            logger.info("Shutting down...")
            try:
                bridge.player._state.should_stop = True
                bridge.player._skip_event.set()
            except Exception:
                logger.debug("shutdown: player stop signal failed", exc_info=True)
            try:
                cache = getattr(bridge.player, "_dj_cache", None)
                if cache is not None:
                    cache.flush(force=True)
            except Exception:
                logger.debug("shutdown: dj-meta flush failed", exc_info=True)
            for task in (broadcast, watcher):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            logger.info("Server stopped cleanly.")

    app = FastAPI(title="AutoDJ", version="0.1.0", lifespan=lifespan)

    # ------------------------------------------------------------------
    # Static HTML
    # ------------------------------------------------------------------

    # Prefer the bundled / minified output from `npm run build` when it
    # exists; fall back to the raw sources for dev (no Node toolchain
    # required).  Both directories share filenames so the FastAPI
    # routes below resolve transparently regardless of which the user
    # has on disk.  See vite.config.js for the build pipeline.
    _static_src = Path(__file__).parent / "static"
    _static_built = Path(__file__).parent / "static_dist"
    _static_dir = _static_built if (_static_built / "index.html").exists() else _static_src
    if _static_dir is _static_built:
        logger.info("Serving built static assets from %s", _static_dir)
    _static_html_path = _static_dir / "index.html"

    # Cache-busting headers so Firefox / Chrome don't keep serving
    # stale HTML / JS / CSS across server upgrades.  In a single-user
    # NAS deployment we don't need browser caching — every page load
    # should pick up the latest static assets.
    _NO_CACHE = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    @app.get("/", response_class=HTMLResponse)
    async def get_index() -> HTMLResponse:
        return HTMLResponse(
            content=_static_html_path.read_text(encoding="utf-8"),
            headers=_NO_CACHE,
        )

    # Convenience aliases at the top level for `app.css` / `app.js` /
    # the AudioWorklet — bypass StaticFiles caching defaults.
    # Explicit routes MUST be registered before the /static mount,
    # otherwise the mount short-circuits with default headers.
    @app.get("/app.css")
    async def get_css() -> FileResponse:
        return FileResponse(_static_dir / "app.css", media_type="text/css", headers=_NO_CACHE)

    @app.get("/app.js")
    async def get_js() -> FileResponse:
        return FileResponse(_static_dir / "app.js", media_type="text/javascript", headers=_NO_CACHE)

    @app.get("/bitcrusher-worklet.js")
    async def get_worklet() -> FileResponse:
        return FileResponse(
            _static_dir / "bitcrusher-worklet.js",
            media_type="text/javascript",
            headers=_NO_CACHE,
        )

    @app.get("/stutter-worklet.js")
    async def get_stutter_worklet() -> FileResponse:
        return FileResponse(
            _static_dir / "stutter-worklet.js",
            media_type="text/javascript",
            headers=_NO_CACHE,
        )

    @app.get("/freeze-worklet.js")
    async def get_freeze_worklet() -> FileResponse:
        return FileResponse(
            _static_dir / "freeze-worklet.js",
            media_type="text/javascript",
            headers=_NO_CACHE,
        )

    @app.get("/glitch-worklet.js")
    async def get_glitch_worklet() -> FileResponse:
        return FileResponse(
            _static_dir / "glitch-worklet.js",
            media_type="text/javascript",
            headers=_NO_CACHE,
        )

    # ES module imports.  index.html loads /app.js as a module; that
    # script's `import "./modules/foo.js"` resolves against the script
    # URL, so the browser fetches /modules/foo.js -- which previously
    # had no route and 404'd in dev mode (no `npm run build`).
    # Production (static_dist/) does not need this because vite bundles
    # everything into a single /app.js, but the route is harmless when
    # the directory is empty.  Path is sanitised to prevent traversal.
    @app.get("/modules/{name}")
    async def get_module(name: str) -> FileResponse:
        # Reject anything outside the modules/ directory.
        target = (_static_dir / "modules" / name).resolve()
        modules_root = (_static_dir / "modules").resolve()
        try:
            target.relative_to(modules_root)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Not found") from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(
            target,
            media_type="text/javascript",
            headers=_NO_CACHE,
        )

    # Serve any other assets at /static/...
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    # ------------------------------------------------------------------
    # REST API
    # ------------------------------------------------------------------

    @app.get("/api/status")
    async def api_status() -> JSONResponse:
        return JSONResponse(bridge.get_state())

    @app.get("/api/version")
    async def api_version() -> JSONResponse:
        # Footer build stamp.  Lets the user verify which commit + bundle
        # the server is actually serving (browser cache vs. fresh build).
        return JSONResponse(_version_info())

    @app.get("/api/history")
    async def api_history(page: int = 1, per_page: int = 50) -> JSONResponse:
        items = list(reversed(bridge._play_history))
        total = len(items)
        pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, pages))
        start = (page - 1) * per_page
        return JSONResponse(
            {
                "items": items[start : start + per_page],
                "total": total,
                "page": page,
                "pages": pages,
            }
        )

    @app.post("/api/skip")
    async def api_skip() -> JSONResponse:
        bridge.skip()
        # Return fresh state so the browser updates its now-playing UI
        # without waiting up to 1 s for the next WS broadcast tick.
        return JSONResponse(bridge.get_state())

    @app.post("/api/seek")
    async def api_seek(body: SeekBody) -> dict[str, float]:
        new_pos = bridge.seek(seconds=body.seconds, delta=body.delta)
        return {"elapsed": round(new_pos, 2)}

    def _profile_store() -> Any:
        from pathlib import Path as _P

        from autodj.profiles import ProfileStore

        cfg = bridge.player._cfg
        # Default location: <index_dir>/../profiles to keep host config
        # local but adjacent to indexes.
        root = _P(cfg.index.active_dir).parent / "profiles"
        return ProfileStore(root)

    @app.get("/api/profiles")
    async def api_profiles() -> dict:
        store = _profile_store()
        return {"profiles": store.list_names(), "root": str(store.root)}

    @app.get("/api/profiles/{name}")
    async def api_profile_get(name: str) -> dict:
        from autodj.profiles import validate_name

        try:
            validate_name(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            snap = _profile_store().load(name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return snap.to_dict()

    @app.post("/api/profiles")
    async def api_profile_save(body: ProfileSaveBody) -> dict:
        from autodj.profiles import ProfileSnapshot, validate_name

        try:
            validate_name(body.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        snap = ProfileSnapshot(**body.model_dump())
        target = _profile_store().save(snap)
        return {"saved": snap.name, "path": str(target)}

    @app.delete("/api/profiles/{name}")
    async def api_profile_delete(name: str) -> dict:
        from autodj.profiles import validate_name

        try:
            validate_name(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ok = _profile_store().delete(name)
        if not ok:
            raise HTTPException(status_code=404, detail="Profile not found")
        return {"deleted": name}

    @app.post("/api/profiles/{name}/apply")
    async def api_profile_apply(name: str) -> dict:
        """Load a saved profile and push every set field through the bridge."""
        from autodj.profiles import validate_name

        try:
            validate_name(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            snap = _profile_store().load(name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        applied: list[str] = []
        # Playback flags
        kw: dict = {}
        for fld in (
            "transition_mode",
            "beat_sync_fx",
            "key_sync_fx",
            "beatmatch_on_skip",
            "crossfade_seconds",
            "fade_in_seconds",
            "smart_shuffle",
            "pure_shuffle",
            "anchor_to_seed",
            "enable_daypart",
            "enable_mood_arc",
            "mood_arc_hours",
            "liners_enabled",
            "liners_pick_mode",
        ):
            v = getattr(snap, fld, None)
            if v is not None:
                kw[fld] = v
                applied.append(fld)
        if kw:
            bridge.set_playback_settings(**kw)
        # BPM range
        if snap.bpm_lo is not None and snap.bpm_hi is not None:
            bridge.set_bpm_range(snap.bpm_lo, snap.bpm_hi)
            applied.append("bpm_range")
        # DJ-mix harmonic mode
        if snap.harmonic_mode is not None:
            bridge.set_djmix(harmonic_mode=snap.harmonic_mode)
            applied.append("harmonic_mode")
        # Preset
        if snap.preset is not None:
            with contextlib.suppress(Exception):
                bridge.set_preset(snap.preset)
                applied.append("preset")
        return {"applied": applied, "name": name}

    @app.get("/api/liners")
    async def api_liners() -> dict:
        """List discovered voice liners + current trigger config.

        The browser uses this to populate the Settings panel listing
        and to fetch raw liner bytes for ducking-overlay playback.
        """
        from autodj.liners import LinerLibrary

        cfg = bridge.player._cfg
        folder_str = cfg.playback.liners_folder
        if not folder_str:
            from pathlib import Path as _P

            folder_str = str(_P(cfg.index.active_dir) / "liners")
        from pathlib import Path as _P

        lib = LinerLibrary.from_folder(_P(folder_str))
        return {
            "folder": str(folder_str),
            "files": [f.name for f in lib.files],
            "count": len(lib.files),
            "config": {
                "enabled": bool(cfg.playback.liners_enabled),
                "every_n_songs": cfg.playback.liners_every_n_songs,
                "every_minutes": cfg.playback.liners_every_minutes,
                "random_min_minutes": cfg.playback.liners_random_min_minutes,
                "random_max_minutes": cfg.playback.liners_random_max_minutes,
                "pick_mode": cfg.playback.liners_pick_mode,
                "duck_db": cfg.playback.liners_duck_db,
            },
        }

    def _resolve_liner_folder() -> Path:
        """Return the configured liner folder, defaulting under index_dir."""
        from pathlib import Path as _P

        cfg = bridge.player._cfg
        folder_str = cfg.playback.liners_folder
        if not folder_str:
            folder_str = str(_P(cfg.index.active_dir) / "liners")
        return _P(folder_str)

    @app.post("/api/liners/upload")
    async def api_liner_upload(file: UploadFile = File(...)) -> dict:
        """Upload a new liner clip into the configured folder.

        Rejects files whose extension isn't in :data:`LINER_EXTS` so
        users can't drop arbitrary binaries into the served folder.
        Creates the folder when missing.
        """
        from autodj.liners import LINER_EXTS

        name = file.filename or ""
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if ext not in LINER_EXTS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported extension {ext!r}; allowed: {', '.join(LINER_EXTS)}",
            )
        # Strip path components from the filename so the upload always
        # lands directly in the liner folder regardless of what the
        # browser sent.
        from pathlib import PurePosixPath

        safe_name = PurePosixPath(name).name
        folder = _resolve_liner_folder()
        folder.mkdir(parents=True, exist_ok=True)
        target = (folder / safe_name).resolve()
        if not str(target).startswith(str(folder.resolve())):
            raise HTTPException(status_code=400, detail="Invalid filename")
        contents = await file.read()
        target.write_bytes(contents)
        return {"filename": safe_name, "size": len(contents)}

    @app.delete("/api/liners/file/{name}")
    async def api_liner_delete(name: str) -> dict:
        """Remove a liner clip identified by filename.

        Same path-traversal guard as the GET endpoint.
        """
        from pathlib import Path as _P

        folder = _resolve_liner_folder().resolve()
        target = (folder / name).resolve()
        if not str(target).startswith(str(folder)) or not target.is_file():
            raise HTTPException(status_code=404, detail="Liner not found")
        try:
            _P(target).unlink()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"deleted": target.name}

    @app.get("/api/liners/file/{name}")
    async def api_liner_file(name: str) -> FileResponse:
        """Stream the raw bytes of a liner clip identified by filename.

        Resolves *name* against the liners folder; rejects path
        traversal by ensuring the resolved file is inside that folder.
        """
        import mimetypes
        from pathlib import Path as _P

        cfg = bridge.player._cfg
        folder_str = cfg.playback.liners_folder
        if not folder_str:
            folder_str = str(_P(cfg.index.active_dir) / "liners")
        folder = _P(folder_str).resolve()
        target = (folder / name).resolve()
        if not str(target).startswith(str(folder)) or not target.is_file():
            raise HTTPException(status_code=404, detail="Liner not found")
        mime, _ = mimetypes.guess_type(str(target))
        return FileResponse(target, media_type=mime or "application/octet-stream")

    @app.post("/api/pause")
    async def api_pause() -> dict[str, bool]:
        paused = bridge.pause()
        return {"paused": paused}

    @app.post("/api/volume")
    async def api_volume(body: VolumeBody) -> dict[str, float]:
        bridge.set_volume(body.volume)
        return {"volume": round(bridge.player._state.volume, 2)}

    @app.post("/api/mute")
    async def api_mute() -> dict[str, bool]:
        muted = bridge.toggle_mute()
        return {"muted": muted}

    @app.post("/api/play-next")
    async def api_play_next(body: PlayNextBody) -> dict[str, bool]:
        found = bridge.play_next(body.path, now=body.now)
        return {"ok": found}

    @app.get("/api/search")
    async def api_search(q: str = "", limit: int = 100) -> dict[str, list]:
        results = bridge.search(q, limit=max(1, min(500, int(limit))))
        return {"results": results}

    # ------------------------------------------------------------------
    # Queue manipulation
    # ------------------------------------------------------------------

    @app.post("/api/queue/add")
    async def api_queue_add(body: PlayNextBody) -> dict[str, bool]:
        return {"ok": bridge.queue_add(body.path)}

    @app.post("/api/queue/remove")
    async def api_queue_remove(body: PlayNextBody) -> dict[str, bool]:
        return {"ok": bridge.queue_remove(body.path)}

    @app.post("/api/queue/reorder")
    async def api_queue_reorder(body: QueueReorderBody) -> dict[str, bool]:
        return {"ok": bridge.queue_reorder(body.paths)}

    # ------------------------------------------------------------------
    # Cover art and lyrics
    # ------------------------------------------------------------------

    @app.get("/api/art")
    async def api_art(path: str) -> Response:
        # Only serve art for tracks that exist in the index — prevents
        # arbitrary file access through the path parameter.
        known = any(e.path == path for e in bridge.sim.entries)
        if not known:
            raise HTTPException(status_code=404, detail="Track not in index")
        result = bridge.cover_art_for(path)
        if result is None:
            raise HTTPException(status_code=404, detail="No embedded cover art")
        data, mime = result
        return Response(content=data, media_type=mime)

    @app.get("/api/lyrics")
    async def api_lyrics() -> dict[str, list]:
        return {"lyrics": bridge.current_lyrics()}

    # ------------------------------------------------------------------
    # Browser-driven playback — stream audio bytes + advance trigger
    # ------------------------------------------------------------------

    _MIME_BY_SUFFIX = {
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".opus": "audio/ogg",
        ".wav": "audio/wav",
        ".aif": "audio/aiff",
        ".aiff": "audio/aiff",
    }

    def _audio_mime(p: Path) -> str:
        return _MIME_BY_SUFFIX.get(p.suffix.lower(), "application/octet-stream")

    def _is_alac(p: Path) -> bool:
        """True iff *p* is an ALAC stream (Apple Lossless inside .m4a)."""
        if p.suffix.lower() not in (".m4a", ".mp4"):
            return False
        try:
            from mutagen.mp4 import MP4

            info = MP4(str(p)).info
            codec = getattr(info, "codec", None) or ""
            return codec.lower() == "alac"
        except (OSError, ValueError, ImportError):
            return False

    async def _transcode_alac_to_mp3(p: Path) -> AsyncGenerator[bytes]:  # pragma: no cover
        """Yield MP3 bytes from an ALAC source via ffmpeg subprocess.

        Browser ALAC support is Safari-only, so for Chrome/Firefox we
        decode + re-encode to MP3 on the fly.  Range requests are not
        supported on transcoded streams (ffmpeg can't seek-then-encode
        cheaply); browser falls back to sequential playback which is
        fine for auto-DJ use.

        Excluded from coverage: requires a real ffmpeg subprocess with
        an ALAC source — neither is available in CI.
        """
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-loglevel",
            "error",
            "-i",
            str(p),
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            "-f",
            "mp3",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout = proc.stdout
            if stdout is None:
                return
            while True:
                chunk = await stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    @app.get("/api/audio")
    async def api_audio(path: str, request: Request) -> Response:
        """Stream the bytes of an indexed audio file with HTTP Range support.

        The browser <audio> element uses this to play whatever the server
        currently reports as ``current_track``.  Range support is required
        for seek bar scrubbing and for browsers that probe metadata via a
        small initial range request.

        Path-traversal is prevented by allowing only paths that appear
        verbatim in the loaded similarity index.
        """
        # Path validation — must be in the index
        known = any(e.path == path for e in bridge.sim.entries)
        if not known:
            raise HTTPException(status_code=404, detail="Track not in index")
        f = Path(path)
        if not f.exists() or not f.is_file():
            raise HTTPException(status_code=404, detail="File not found on disk")

        # Transcode ALAC → MP3 on the fly so non-Safari browsers can
        # play Apple Lossless tracks.  ffmpeg must be on PATH.
        if _is_alac(f):  # pragma: no cover — requires ALAC source + ffmpeg
            try:
                return StreamingResponse(
                    _transcode_alac_to_mp3(f),
                    media_type="audio/mpeg",
                    headers={"Accept-Ranges": "none"},
                )
            except FileNotFoundError:
                logger.warning(
                    "ffmpeg not on PATH; serving ALAC raw (browser may reject).",
                )

        file_size = f.stat().st_size
        mime = _audio_mime(f)
        range_header = request.headers.get("range") or request.headers.get("Range")

        # No Range — full body
        if not range_header:

            def _full_iter() -> AsyncGenerator[bytes]:
                async def _gen() -> AsyncGenerator[bytes]:
                    chunk = 64 * 1024
                    with open(f, "rb") as fh:
                        while True:
                            data = fh.read(chunk)
                            if not data:
                                break
                            yield data

                return _gen()

            return StreamingResponse(
                _full_iter(),
                media_type=mime,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(file_size),
                },
            )

        # Parse "bytes=START-END" — only single-range supported
        try:
            units, _, ranges = range_header.partition("=")
            if units.strip().lower() != "bytes":
                raise ValueError
            start_s, _, end_s = ranges.partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else file_size - 1
        except (ValueError, AttributeError):
            raise HTTPException(status_code=416, detail="Invalid Range header") from None

        if start < 0 or start >= file_size or end >= file_size or start > end:
            raise HTTPException(
                status_code=416,
                detail="Requested range not satisfiable",
                headers={"Content-Range": f"bytes */{file_size}"},
            )

        length = end - start + 1

        async def _range_gen() -> AsyncGenerator[bytes]:
            chunk = 64 * 1024
            remaining = length
            with open(f, "rb") as fh:
                fh.seek(start)
                while remaining > 0:
                    data = fh.read(min(chunk, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        return StreamingResponse(
            _range_gen(),
            status_code=206,
            media_type=mime,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(length),
            },
        )

    @app.post("/api/advance")
    async def api_advance() -> JSONResponse:
        """Browser signals end-of-track — server picks next track.

        Returns the fresh state synchronously so the browser can update
        ``current_track`` / ``next_track`` immediately without waiting
        for the 1 Hz WS broadcast.  Decouples advance latency from the
        broadcast cadence.
        """
        bridge.skip()
        return JSONResponse(bridge.get_state())

    @app.post("/api/repick-next")
    async def api_repick_next(request: Request) -> JSONResponse:
        """Replace next_track without advancing current.

        Browser POSTs this when its standby deck fails to load the
        prefetched next-track (bad codec, codec not supported on this
        client, network drop).  Server picks a different next-track so
        the currently-playing track keeps playing instead of being
        cascade-skipped along with the unplayable upcoming.

        Optional JSON body: ``{"blacklist": "path/that/failed.flac"}``
        adds the failed path to recently-played so similarity skips it.
        """
        blacklist = None
        try:
            body = await request.json()
            blacklist = body.get("blacklist") if isinstance(body, dict) else None
        except (ValueError, TypeError):
            # No JSON body or malformed payload — endpoint accepts either.
            logger.debug("repick_next: no JSON body, blacklist not set")
        bridge.repick_next(blacklist_path=blacklist)
        return JSONResponse(bridge.get_state())

    @app.post("/api/random-track")
    async def api_random_track() -> JSONResponse:
        """Reseed the auto-DJ from a fresh random track in the index.

        Status code distinguishes success from an empty index:
        ``200`` when a track was picked, ``409 Conflict`` when the
        index has nothing to reseed from.
        """
        if not bridge.reseed_random():
            raise HTTPException(status_code=409, detail="Index is empty")
        return JSONResponse(bridge.get_state())

    # ------------------------------------------------------------------
    # Settings (mirror of CLI flags)
    # ------------------------------------------------------------------

    @app.get("/api/settings")
    async def api_settings() -> dict:
        return bridge.get_settings()

    @app.post("/api/preset")
    async def api_preset(body: PresetBody) -> dict:
        bridge.set_preset(body.name)
        bridge.save_persistent_state()
        return bridge.get_settings()

    @app.post("/api/transition")
    async def api_transition(body: TransitionBody) -> dict:
        bridge.set_transition(body.effect)
        bridge.save_persistent_state()
        return bridge.get_settings()

    @app.post("/api/djmix")
    async def api_djmix(body: DjMixBody) -> dict:
        bridge.set_djmix(**body.model_dump(exclude_none=True))
        bridge.save_persistent_state()
        return bridge.get_settings()

    @app.post("/api/playback-settings")
    async def api_playback_settings(body: PlaybackSettingsBody) -> dict:
        bridge.set_playback_settings(**body.model_dump(exclude_none=True))
        bridge.save_persistent_state()
        return bridge.get_settings()

    @app.post("/api/bpm-range")
    async def api_bpm_range(body: BpmRangeBody) -> dict:
        bridge.set_bpm_range(body.lo, body.hi)
        bridge.save_persistent_state()
        return bridge.get_settings()

    @app.post("/api/discovery")
    async def api_discovery(body: DiscoveryBody) -> dict:
        bridge.set_discovery_every(body.every)
        bridge.save_persistent_state()
        return bridge.get_settings()

    # ------------------------------------------------------------------
    # 3-band EQ
    # ------------------------------------------------------------------

    @app.post("/api/eq")
    async def api_eq(body: EqBody) -> dict[str, float]:
        return bridge.set_eq(low=body.low, mid=body.mid, high=body.high)

    # ------------------------------------------------------------------
    # Library tools — index / enrich / prune / stats from the web UI
    # ------------------------------------------------------------------

    @app.get("/api/library/job")
    async def api_library_job_status() -> dict:
        from autodj.jobs import get_manager

        return get_manager().snapshot()

    @app.post("/api/library/run")
    async def api_library_run(body: LibraryJobBody) -> dict:
        from autodj.jobs import get_manager

        mgr = get_manager()
        ok = mgr.start(body.name, body.args)
        if not ok:
            raise HTTPException(
                status_code=409,
                detail="Job already running, or subcommand not allowed.",
            )
        return mgr.snapshot()

    @app.post("/api/library/stop")
    async def api_library_stop() -> dict[str, bool]:
        from autodj.jobs import get_manager

        return {"stopped": get_manager().stop()}

    @app.get("/api/library/stats")
    async def api_library_stats() -> dict:
        """Return summary stats about the loaded SimilarityIndex."""
        sim = bridge.sim
        entries = sim.entries
        n = len(entries)
        bpms = [e.bpm for e in entries if e.bpm > 0]
        avg_bpm = round(sum(bpms) / len(bpms), 1) if bpms else 0.0
        with_genre = sum(1 for e in entries if e.genre)
        with_key = sum(1 for e in entries if e.key >= 0 and e.mode >= 0)
        with_energy = sum(1 for e in entries if e.energy > 0)
        return {
            "track_count": n,
            "tracks_with_bpm": len(bpms),
            "average_bpm": avg_bpm,
            "tracks_with_genre": with_genre,
            "tracks_with_key": with_key,
            "tracks_with_energy": with_energy,
        }

    # ------------------------------------------------------------------
    # WebSocket broadcast
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        # Parameter renamed from 'ws' to 'websocket' to avoid FastAPI
        # mistaking the path segment '/ws' for a query parameter.
        await websocket.accept()
        client_host = getattr(getattr(websocket, "client", None), "host", "unknown")
        async with _ws_lock:
            _ws_clients.add(websocket)
            client_count = len(_ws_clients)
        logger.info(
            "WebSocket connected: %s (%d active client%s)",
            client_host,
            client_count,
            "" if client_count == 1 else "s",
        )
        try:
            while True:
                text = await websocket.receive_text()
                # Handle incoming control commands from the client
                try:
                    msg = _json.loads(text)
                    if isinstance(msg, dict) and msg.get("type") == "toggle_discovery":
                        bridge.toggle_discovery()
                except (_json.JSONDecodeError, AttributeError):
                    pass
        except WebSocketDisconnect:
            pass
        finally:
            async with _ws_lock:
                _ws_clients.discard(websocket)
                remaining = len(_ws_clients)
            logger.info(
                "WebSocket disconnected: %s (%d active client%s remain)",
                client_host,
                remaining,
                "" if remaining == 1 else "s",
            )

    async def _broadcast_loop() -> None:  # pragma: no cover — long-running task
        """Push state JSON to all connected WebSocket clients once per second."""
        while True:
            await asyncio.sleep(1)
            if not _ws_clients:
                continue
            payload = _json.dumps(bridge.get_state())
            dead: list[WebSocket] = []
            async with _ws_lock:
                clients = list(_ws_clients)
            for client in clients:
                try:
                    await client.send_text(payload)
                except (RuntimeError, WebSocketDisconnect, ConnectionError):
                    dead.append(client)
            if dead:
                async with _ws_lock:
                    for client in dead:
                        _ws_clients.discard(client)

    async def _index_watcher_loop() -> None:  # pragma: no cover — long-running task
        """Reload the FAISS index when ``metadata.json`` mtime changes.

        Runs every 10 seconds.  Lets a parallel ``autodj index`` add
        tracks while a long-running ``serve`` is up — the next track
        pick will see the new entries without restarting the server.
        """
        cfg = getattr(bridge.player, "_cfg", None)
        if cfg is None:
            return
        meta_path = cfg.index.active_dir / "metadata.json"
        last_mtime = meta_path.stat().st_mtime if meta_path.exists() else 0.0
        while True:
            await asyncio.sleep(10)
            try:
                if not meta_path.exists():
                    continue
                mtime = meta_path.stat().st_mtime
                if mtime > last_mtime:
                    last_mtime = mtime
                    new_total = await asyncio.to_thread(
                        bridge.reload_index_from_disk,
                    )
                    logger.info(
                        "Index reloaded — %d tracks now available",
                        new_total,
                    )
            except (OSError, ValueError) as exc:
                logger.debug("Index watcher: %s", exc)

    return app


# ---------------------------------------------------------------------------
# serve() — wires everything together
# ---------------------------------------------------------------------------


def serve(
    cfg: AutoDJConfig,
    sim: SimilarityIndex,
    seed_entry: IndexEntry | None,
    host: str = "127.0.0.1",
    port: int = 8080,
    preset: Preset | None = None,
    export_m3u: Path | None = None,
    history_file: Path | None = None,
    discovery_every: int | None = None,
    bpm_range: tuple[float, float] | None = None,
    smart_shuffle: bool = False,
    pure_shuffle: bool = False,
    anchor_to_seed: bool = False,
    no_playback: bool = False,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
) -> None:
    """Start the Player thread and the FastAPI/uvicorn web server.

    This function blocks until uvicorn is stopped (Ctrl+C).

    Args:
        cfg: Full :class:`~autodj.config.AutoDJConfig` instance.
        sim: Loaded :class:`~autodj.similarity.SimilarityIndex`.
        seed_entry: Starting :class:`~autodj.indexer.IndexEntry`, or ``None``
            for a random track.
        host: Interface to bind uvicorn to.
        port: Port to bind uvicorn to.
        preset: Optional BPM-shaping preset (forwarded to Player).
        export_m3u: Optional path for live M3U export (forwarded to Player).
        history_file: Optional path for play history (forwarded to Player).
        discovery_every: Discovery injection rate (forwarded to Player).
        bpm_range: Hard BPM filter ``(lo, hi)`` (forwarded to Player).
    """
    import importlib.util as _import_util

    import uvicorn

    from autodj.player import Player

    # Auto-detect missing audio deps and flip to no-playback so headless
    # hosts (NAS, server) don't spam "module not found" per track.
    if not no_playback:
        missing: list[str] = []
        for name in ("soundfile", "sounddevice"):
            try:
                if _import_util.find_spec(name) is None:
                    missing.append(name)
            except (ImportError, ValueError):
                # ValueError fires when the name is mocked / partially
                # installed (e.g. during tests).  Treat as available.
                pass
        if missing:  # pragma: no cover — minimal-install branch
            print(
                "[AutoDJ] Headless mode — browser handles audio output. "
                "Open the web UI from any device on your network.",
            )
            no_playback = True

    player = Player(
        cfg,
        sim,
        dry_run=no_playback,
        preset=preset,
        export_m3u=export_m3u,
        history_file=history_file,
        discovery_every=discovery_every,
        bpm_range=bpm_range,
        smart_shuffle=smart_shuffle,
        pure_shuffle=pure_shuffle,
        anchor_to_seed=anchor_to_seed,
        # The browser is the control surface in serve mode — disable
        # the global pynput keyboard hook so keys typed in OTHER apps
        # / tabs / windows don't accidentally pause / skip / mute the
        # player.
        no_keyboard=True,
    )
    bridge = PlayerBridge(player=player, sim=sim)

    # Restore previously-saved settings (preset, transition, EQ, etc.)
    # so the user doesn't have to re-tick everything on each `serve` restart.
    bridge.load_persistent_state()

    # Start Player in a daemon thread — it blocks internally on playback
    player_thread = threading.Thread(
        target=player.run,
        kwargs={"seed_entry": seed_entry},
        name="autodj-player",
        daemon=True,
    )
    player_thread.start()
    bridge.record_seed(seed_entry)

    app = create_app(bridge)

    scheme = "https" if (ssl_certfile and ssl_keyfile) else "http"
    pretty_host = (
        "localhost"
        if host in ("0.0.0.0", "127.0.0.1", "::")  # nosec B104 — display only
        else host
    )
    audio_mode = "server-audio" if not no_playback else "browser-driven"
    logger.info(
        "AutoDJ server ready: %s://%s:%d  (%s, %d indexed tracks, seed=%s)",
        scheme,
        pretty_host,
        port,
        audio_mode,
        len(getattr(sim, "entries", []) or []),
        getattr(seed_entry, "display_name", "random"),
    )
    logger.info(
        "Tip: pass `-v` (autodj -v serve) for debug logs.  "
        "Browser-side debug: append ?debug=1 to the URL or set "
        "localStorage.autodjDebug='1'.",
    )

    # ssl_certfile / ssl_keyfile flip uvicorn into HTTPS mode, which is
    # required by browsers to expose AudioWorklet on non-localhost hosts.
    # Without HTTPS the freeze / glitch / bitcrusher worklets fall back
    # to BufferSource / WaveShaper approximations.
    uvicorn_kwargs: dict = {
        "host": host,
        "port": port,
        "log_level": "warning",
    }
    if ssl_certfile and ssl_keyfile:
        uvicorn_kwargs["ssl_certfile"] = ssl_certfile
        uvicorn_kwargs["ssl_keyfile"] = ssl_keyfile
    # uvicorn normally swallows SIGINT and exits cleanly via the
    # FastAPI lifespan, but a second Ctrl+C (or a SIGINT received
    # mid-asyncio-teardown on Windows) can re-raise.  Suppress so the
    # CLI sees a clean return; the lifespan already logged
    # "Server stopped cleanly."
    with contextlib.suppress(KeyboardInterrupt):
        uvicorn.run(app, **uvicorn_kwargs)
