"""Real Player, local ownership, SQLite ledger, mix bus and FFmpeg audit fixture.

Synthetic catalog and WAV sources replace library discovery. The production page
markup loads the production engine/owner/control modules through a small audit
shell; normal application startup and hardware routing are outside this fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import json
import os
import secrets
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from autodj._bridge import PlayerBridge
from autodj.live import LiveDesk
from autodj.local_programme import LocalProgramme
from autodj.local_programme_playout import LocalPerformance
from autodj.local_programme_routes import RawPeerMiddleware, install_local_programme_routes
from autodj.player import Player
from autodj.security import COOKIE_NAME, SecurityPolicy
from autodj.station_ledger import StationLedger
from autodj.station_store import StationStore
from autodj.stream import FfmpegEncoder, StreamOutput
from tests.playwright.local_programme_measure import signal
from tests.unit._rotation_station import make_station, song

STATIC = Path(__file__).resolve().parents[2] / "src/autodj/static"


async def run(root: Path) -> None:
    faulthandler.dump_traceback_later(90)
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    # Different segments of one deterministic stereo code identify both decks.
    codes = signal(44100, 90)
    for index in range(3):
        path = root / f"programme-{index}.wav"
        sf.write(path, codes[index * 30 * 44100 : (index + 1) * 30 * 44100], 44100, subtype="FLOAT")
        entries.append(replace(song(index, "Audit artist", length=30), path=str(path)))
    source = make_station(
        root / "station",
        entries,
        playback={
            "pick_mode": "random",
            "transition_mode": "fixed",
            "crossfade_seconds": 0.25,
            "loudness_processing": "off",
            "liners_enabled": False,
        },
    )
    cfg = source.player._cfg
    cfg.server.local_programme_enabled = True
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        cfg.server.port = port
        cfg.server.allowed_hosts = ["127.0.0.1"]
        cfg.server.allowed_origins = [f"http://127.0.0.1:{port}"]
        player = Player(cfg, source.player._sim, stream_mode=True)
        player._library = source.library
        store = StationStore(root / "ledger.db", "default")
        ledger = StationLedger(store, source.library.entry_for_path)
        bridge = PlayerBridge(player=player, sim=player._sim, library=source.library, ledger=ledger)
        player.on_track_started = bridge.on_track_started
        live = LiveDesk(root / "live.json")
        captures = []

        class AuditController(LocalProgramme):
            def claim(self, identity, socket_id, now):
                grant = super().claim(identity, socket_id, now)
                capture = {
                    "generation": grant.generation,
                    "writer": sf.SoundFile(
                        root / f"feed-{grant.generation}.wav", "w", 44100, 2, subtype="FLOAT"
                    ),
                    "times": [],
                    "audio": self.audio,
                }
                captures.append(capture)
                original = self.audio.accept_packet

                def accept(grant, packet):
                    try:
                        original(grant, packet)
                    except ValueError as exc:
                        pcm = np.frombuffer(packet, dtype="<f4", offset=48).reshape(-1, 2)
                        nonzero = np.flatnonzero(np.any(pcm != 0, axis=1))
                        (root / "rejected.json").write_text(
                            json.dumps(
                                {
                                    "reason": str(exc),
                                    "armed": self.audio._armed,
                                    "arm_frame": self.audio._arm_frame,
                                    "first": self.audio.next_sequence * 441,
                                    "first_nonzero": int(nonzero[0]) if len(nonzero) else None,
                                    "now": time.monotonic(),
                                    "last_budget": self.audio._last_budget,
                                    "packet_tokens": self.audio._packets,
                                    "byte_tokens": self.audio._bytes,
                                    "recent_accept_times": capture["times"][-100:],
                                }
                            ),
                            encoding="utf-8",
                        )
                        raise
                    capture["writer"].write(
                        np.frombuffer(packet, dtype="<f4", offset=48).reshape(-1, 2)
                    )
                    capture["times"].append(time.monotonic())

                self.audio.accept_packet = accept
                return grant

        station = SimpleNamespace(
            player=player,
            bridge=bridge,
            local_programme=AuditController(live),
            stream_mode=True,
            stopped=False,
            slug="default",
            schedule=SimpleNamespace(events={}, pending=[], _tasks=set()),
            intros=None,
            show_runner=None,
        )
        performance = LocalPerformance(station)
        station.local_programme.performance = performance
        player.local_performance = performance
        stream = StreamOutput(
            320, 2, encoder_factory=FfmpegEncoder, loop=asyncio.get_running_loop()
        )
        bridge.stream = stream
        player.bus.add_output(stream)
        bus_capture = sf.SoundFile(root / "bus.wav", "w", 44100, 2, subtype="FLOAT")
        block_times = []

        class Capture:
            def write(self, block):
                bus_capture.write(block)
                block_times.append(time.monotonic())

        player.bus.add_output(Capture())
        player._state.queue = entries[1:]
        player.begin_set(entries[0], "seed")
        player._playout.start()
        if not await asyncio.to_thread(player._playout.wait_ready, 10):
            raise RuntimeError("Automation fixture did not prepare audio")
        stop = threading.Event()
        player.bus.start_set()
        worker = threading.Thread(target=player.bus.run, args=(stop,), daemon=True)
        worker.start()
        app = FastAPI()
        app.state.stations = SimpleNamespace(default=station)
        cfg.server.access_token = secrets.token_hex(32)
        device_active = True
        policy = SecurityPolicy(cfg.server, device_is_active=lambda _device: device_active)
        if diagnostic_file := os.environ.get("LOCAL_PROGRAMME_DIAGNOSTICS_FILE"):

            @app.get("/programme-worklet.js")
            async def diagnostic_worklet():
                return FileResponse(diagnostic_file, media_type="text/javascript")

        install_local_programme_routes(app, policy)
        app.mount("/modules", StaticFiles(directory=STATIC / "modules"))
        connections = []

        @app.get("/api/audio")
        async def audio(path: str):
            if path not in {e.path for e in entries}:
                raise HTTPException(404)
            return FileResponse(path, media_type="audio/wav")

        @app.get("/app.css")
        async def css():
            return FileResponse(STATIC / "app.css", media_type="text/css")

        @app.get("/{name}-worklet.js")
        async def effect(name: str):
            worklets = {
                "bitcrusher": STATIC / "bitcrusher-worklet.js",
                "stutter": STATIC / "stutter-worklet.js",
                "freeze": STATIC / "freeze-worklet.js",
                "glitch": STATIC / "glitch-worklet.js",
                "programme": STATIC / "programme-worklet.js",
            }
            path = worklets.get(name)
            if path is None:
                raise HTTPException(404)
            return FileResponse(path, media_type="text/javascript")

        @app.get("/stream")
        async def listen():
            listener = stream.add_listener(icy=False)
            connections.append(listener)
            return StreamingResponse(listener.chunks(), media_type="audio/mpeg")

        @app.get("/audit-state")
        async def state():
            return {
                "ownership": station.local_programme.status(),
                "connections": len(connections),
                "open_connections": sum(not c.closed for c in connections),
                "encoder_generation": stream._encoder_generation,
                "current": player._state.current_track.path
                if player._state.current_track
                else None,
                "queue": [e.path for e in player._state.queue],
                "history": list(bridge._play_history),
                "reason": performance.reason,
                "blocks": len(block_times),
                "high_water_frames": station.local_programme.audio.high_water_frames
                if station.local_programme.audio is not None
                else None,
            }

        @app.get("/audit-clock")
        async def clock():
            return {"monotonic_ms": time.monotonic() * 1000}

        @app.get("/audit", response_class=HTMLResponse)
        async def page():
            response = HTMLResponse((STATIC / "index.html").read_bytes())
            response.set_cookie(COOKIE_NAME, policy.issue_device_session("a" * 32), httponly=True)
            return response

        @app.post("/audit-revoke-session")
        async def revoke_session():
            nonlocal device_active
            device_active = False
            return {"ok": True}

        @app.get("/app.js")
        async def script():
            return Response(
                """
import * as engine from "/modules/audio-engine.js";
import { ProgrammeOwner } from "/modules/local-programme.js";
import { installLocalProgrammeControls } from "/modules/local-programme-controls.js";
import { announceStatus } from "/modules/live-region.js";
const owner = new ProgrammeOwner({ engine });
const controls = installLocalProgrammeControls({ owner,
  wrap: document.getElementById("local-programme-controls"),
  take: document.getElementById("take-local-control"),
  returnButton: document.getElementById("return-to-automation"),
  status: document.getElementById("local-programme-status"),
  announce: text => announceStatus(document.getElementById("sr-status"), text, {mirror:false}),
});
setInterval(async () => controls.apply({stream_mode:true,
  local_programme: await (await fetch("/api/local-programme")).json()}), 100);
document.getElementById("btn-skip").onclick = () => owner.transition().catch(e => owner.stop(e.message));
document.getElementById("btn-mute").onclick = () => engine.setMuted(true);
document.getElementById("vol").oninput = e => engine.setVolume(Number(e.target.value) / 100);
window.audit = {owner, engine};
engine.setVolume(1);
""",
                media_type="text/javascript",
            )

        @app.post("/shutdown")
        async def shutdown():
            for connection in connections:
                connection.close()
            server.should_exit = True
            return {"ok": True}

        server = uvicorn.Server(
            uvicorn.Config(
                RawPeerMiddleware(app),
                proxy_headers=False,
                lifespan="off",
                log_level="warning",
                ws="websockets",
                ws_max_size=65536,
                ws_max_queue=4,
            )
        )
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            while not server.started:
                if serving.done():
                    await serving
                    raise RuntimeError("Fixture server stopped before listening")
                await asyncio.sleep(0.01)
            print(json.dumps({"port": port}), flush=True)
            await serving
        finally:
            faulthandler.cancel_dump_traceback_later()
            station.stopped = True
            station.local_programme.close()
            stop.set()
            await asyncio.to_thread(worker.join, 3)
            player._playout.stop()
            stream.close()
            ledger.flush()
            store.close()
            bus_capture.close()
            metadata = {"block_times": block_times, "captures": []}
            for capture in captures:
                name = f"feed-{capture['generation']}.wav"
                capture["writer"].close()
                metadata["captures"].append(
                    {
                        "file": name,
                        "times": capture["times"],
                        "high_water_frames": capture["audio"].high_water_frames,
                    }
                )
            (root / "timing.json").write_text(json.dumps(metadata), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    asyncio.run(run(parser.parse_args().root.resolve()))
