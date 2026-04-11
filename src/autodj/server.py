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
``POST /api/skip``          → skip to next track
``POST /api/pause``         → toggle pause / resume
``POST /api/volume``        → set volume (body: ``{"volume": 0.75}``)
``POST /api/mute``          → toggle mute
``GET  /api/search?q=``     → fuzzy search of indexed tracks
``POST /api/play-next``     → queue a track by path to play after the current one
``WS   /ws``                → push state JSON every 1 second
"""

import asyncio
import contextlib
import json as _json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# FastAPI imports at module level so that `from __future__ import annotations`
# does not break FastAPI's annotation-based dependency injection.  When these
# are imported inside a function, FastAPI resolves lazy string annotations
# against the *module* globals and fails to find WebSocket / BaseModel.
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class VolumeBody(BaseModel):
    """Request body for POST /api/volume."""

    volume: float


class PlayNextBody(BaseModel):
    """Request body for POST /api/play-next."""

    path: str
    now: bool = False  # if True, also skip the current track


# ---------------------------------------------------------------------------
# PlayerBridge
# ---------------------------------------------------------------------------


@dataclass
class PlayerBridge:
    """Thread-safe adapter between :class:`~autodj.player.Player` and FastAPI.

    Exposes playback control methods that can be called from the asyncio
    event loop while the Player runs in a separate thread.  All access to
    ``PlayerState`` is single-field reads/writes protected by Python's GIL —
    no additional locking is required.

    Attributes:
        player: The running :class:`~autodj.player.Player` instance.
        sim: The loaded :class:`~autodj.similarity.SimilarityIndex` (for search).
    """

    player: object  # autodj.player.Player — imported lazily to avoid heavy import at module load
    sim: object     # autodj.similarity.SimilarityIndex

    # ------------------------------------------------------------------
    # Controls
    # ------------------------------------------------------------------

    def skip(self) -> None:
        """Skip the current track immediately."""
        self.player._skip_event.set()  # type: ignore[attr-defined]

    def pause(self) -> bool:
        """Toggle pause/resume.

        Returns:
            New paused state (``True`` = paused).
        """
        state = self.player._state  # type: ignore[attr-defined]
        state.is_paused = not state.is_paused
        return state.is_paused

    def set_volume(self, volume: float) -> None:
        """Set playback volume.

        Args:
            volume: Float in ``[0.0, 1.0]``.  Clamped automatically.
        """
        self.player._state.volume = max(0.0, min(1.0, float(volume)))  # type: ignore[attr-defined]

    def toggle_mute(self) -> bool:
        """Toggle mute.

        Returns:
            New muted state (``True`` = muted).
        """
        state = self.player._state  # type: ignore[attr-defined]
        state.is_muted = not state.is_muted
        return state.is_muted

    # ------------------------------------------------------------------
    # State read
    # ------------------------------------------------------------------

    def get_state(self) -> dict:
        """Return a JSON-serialisable snapshot of the current player state.

        Returns:
            Dict with keys: ``current_track``, ``next_track``, ``is_paused``,
            ``volume``, ``is_muted``, ``elapsed``, ``duration``.
        """
        state = self.player._state  # type: ignore[attr-defined]
        pos = self.player._playback_pos[0]  # type: ignore[attr-defined]
        sr = self.player._current_sr  # type: ignore[attr-defined]

        def _track_dict(entry) -> dict | None:
            if entry is None:
                return None
            return {
                "title": entry.title,
                "artist": entry.artist,
                "album": entry.album,
                "path": entry.path,
                "bpm": entry.bpm,
                "length": entry.length,
                "display_name": entry.display_name,
            }

        elapsed = round(pos / max(1, sr), 1)

        return {
            "current_track": _track_dict(state.current_track),
            "next_track": _track_dict(state.next_track),
            "is_paused": state.is_paused,
            "volume": round(state.volume, 2),
            "is_muted": state.is_muted,
            "elapsed": elapsed,
            "duration": round(state.current_track.length, 1) if state.current_track and state.current_track.length else 0.0,
        }

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Search indexed tracks by title or artist.

        Args:
            query: Case-insensitive search string matched against title and artist.
            limit: Maximum number of results to return.

        Returns:
            List of track dicts (same shape as the ``current_track`` dict in
            :meth:`get_state`).
        """
        q = query.lower().strip()
        if not q:
            return []

        results = []
        for entry in self.sim.entries:  # type: ignore[attr-defined]
            if q in entry.title.lower() or q in entry.artist.lower():
                results.append({
                    "title": entry.title,
                    "artist": entry.artist,
                    "album": entry.album,
                    "path": entry.path,
                    "bpm": entry.bpm,
                    "length": entry.length,
                    "display_name": entry.display_name,
                })
                if len(results) >= limit:
                    break
        return results

    # ------------------------------------------------------------------
    # Queue control
    # ------------------------------------------------------------------

    def play_next(self, path: str, now: bool = False) -> bool:
        """Queue a track by path to play after the current one.

        Args:
            path: The :attr:`~autodj.indexer.IndexEntry.path` string of the
                track to queue.
            now: If ``True``, also skip the current track so the queued track
                starts immediately.

        Returns:
            ``True`` if the track was found in the index, ``False`` otherwise.
        """
        entry = next(
            (e for e in self.sim.entries if e.path == path),  # type: ignore[attr-defined]
            None,
        )
        if entry is None:
            return False
        self.player._state.queued_next = entry  # type: ignore[attr-defined]
        if now:
            self.player._skip_event.set()  # type: ignore[attr-defined]
        return True


# ---------------------------------------------------------------------------
# FastAPI application factory
# ---------------------------------------------------------------------------


def create_app(bridge: PlayerBridge):
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
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(_broadcast_loop())
        yield
        task.cancel()

    app = FastAPI(title="AutoDJ", version="0.1.0", lifespan=lifespan)

    # ------------------------------------------------------------------
    # Static HTML
    # ------------------------------------------------------------------

    _static_html_path = Path(__file__).parent / "static" / "index.html"

    @app.get("/", response_class=HTMLResponse)
    async def get_index():
        return HTMLResponse(content=_static_html_path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # REST API
    # ------------------------------------------------------------------

    @app.get("/api/status")
    async def api_status():
        return JSONResponse(bridge.get_state())

    @app.post("/api/skip")
    async def api_skip():
        bridge.skip()
        return {"ok": True}

    @app.post("/api/pause")
    async def api_pause():
        paused = bridge.pause()
        return {"paused": paused}

    @app.post("/api/volume")
    async def api_volume(body: VolumeBody):
        bridge.set_volume(body.volume)
        return {"volume": round(bridge.player._state.volume, 2)}  # type: ignore[attr-defined]

    @app.post("/api/mute")
    async def api_mute():
        muted = bridge.toggle_mute()
        return {"muted": muted}

    @app.post("/api/play-next")
    async def api_play_next(body: PlayNextBody):
        found = bridge.play_next(body.path, now=body.now)
        return {"ok": found}

    @app.get("/api/search")
    async def api_search(q: str = ""):
        results = bridge.search(q)
        return {"results": results}

    # ------------------------------------------------------------------
    # WebSocket broadcast
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        # Parameter renamed from 'ws' to 'websocket' to avoid FastAPI
        # mistaking the path segment '/ws' for a query parameter.
        await websocket.accept()
        async with _ws_lock:
            _ws_clients.add(websocket)
        try:
            while True:
                # Keep connection alive; data is pushed by _broadcast_loop.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            async with _ws_lock:
                _ws_clients.discard(websocket)

    async def _broadcast_loop():
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
                except Exception:
                    dead.append(client)
            if dead:
                async with _ws_lock:
                    for client in dead:
                        _ws_clients.discard(client)

    return app


# ---------------------------------------------------------------------------
# serve() — wires everything together
# ---------------------------------------------------------------------------


def serve(
    cfg,
    sim,
    seed_entry,
    host: str = "127.0.0.1",
    port: int = 8080,
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
    """
    import uvicorn
    from autodj.player import Player

    player = Player(cfg, sim, dry_run=False)
    bridge = PlayerBridge(player=player, sim=sim)

    # Start Player in a daemon thread — it blocks internally on playback
    player_thread = threading.Thread(
        target=player.run,
        kwargs={"seed_entry": seed_entry},
        name="autodj-player",
        daemon=True,
    )
    player_thread.start()

    app = create_app(bridge)

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
    )
