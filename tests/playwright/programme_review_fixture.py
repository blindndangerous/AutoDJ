"""Local synthetic review audit server; no model, hardware or normal app startup.

Uses native create_app routes/security, ReviewJobs, source capture and renderer.
Only station/catalog input, local TTS PCM and cancellation delay are test fixtures.
The page imports production review controls against production review markup.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import soundfile as sf


def tts(output: Path) -> None:
    sys.stdin.read()
    rate = 44100
    samples = 0.1 * np.sin(2 * np.pi * 1100 * np.arange(rate // 2) / rate)
    sf.write(output, np.repeat(samples[:, None], 2, axis=1), rate)


def verify(wav: Path, mp3: Path) -> None:
    from autodj.stereo import load_stereo

    samples, rate = sf.read(wav, dtype="float32", always_2d=True)
    decoded = load_stereo(str(mp3), rate)
    frames = min(len(samples), len(decoded))
    error = float(np.mean((samples[:frames] - decoded[:frames]) ** 2))
    assert frames > rate // 2 and abs(len(samples) - len(decoded)) < rate / 10
    assert error < 0.003 and np.max(np.abs(samples)) > 0.01
    spectrum = np.abs(np.fft.rfft(samples[:, 0]))
    frequency = np.fft.rfftfreq(len(samples), 1 / rate)
    for pitch in (220, 440):
        assert spectrum[np.abs(frequency - pitch) < 3].max() > 1, pitch
    print(
        json.dumps(
            {
                "frames": frames,
                "sample_rate": rate,
                "decoded_mp3_mse": error,
                "known_source_frequencies": [220, 440],
            }
        )
    )


def build(root: Path, port: int):
    sys.modules.setdefault("sounddevice", MagicMock())
    from fastapi import Body
    from fastapi.responses import HTMLResponse, Response

    from autodj import review_source
    from autodj._bridge import PlayerBridge
    from autodj.server import create_app
    from autodj.shows import Show, ShowStore
    from autodj.station_settings import StationSettings
    from autodj.stations import StationPaths
    from tests._layout import point_index
    from tests.integration._helpers import _make_player_mock, _make_sim_mock
    from tests.unit._rotation_station import make_station, song

    root.mkdir(parents=True, exist_ok=True)
    entries = []
    for number, frequency in enumerate((220, 440, 660)):
        path = root / f"song-{number}.wav"
        t = np.arange(4 * 44100) / 44100
        samples = 0.15 * np.sin(2 * np.pi * frequency * t)
        sf.write(path, np.repeat(samples[:, None], 2, axis=1), 44100, subtype="FLOAT")
        entries.append(replace(song(number, f"Audit artist {number}", length=4), path=str(path)))
    source = make_station(
        root / "station",
        entries,
        playback={
            "pick_mode": "random",
            "transition_mode": "fixed",
            "crossfade_seconds": 0.5,
            "loudness_processing": "off",
        },
    )
    paths = StationPaths.default(source.player._cfg)
    settings = StationSettings(source.player._cfg, source.library, source.player._sim, paths)
    settings.entry_for_path = source.library.entry_for_path
    store = ShowStore(paths.shows)
    identity, _ = store.add(Show(name="Browser audit", source="category", category="audit"))
    store.update(
        identity,
        lambda show: show.model_copy(
            update={
                "songs": [entries[0].path, entries[1].path, entries[0].path],
            }
        ),
    )
    from types import SimpleNamespace

    station = SimpleNamespace(
        player=None,
        cfg=settings.cfg,
        paths=paths,
        slug="default",
        timezone="UTC",
        bridge=settings,
        ledger=source.ledger,
        shows=store,
        schedule=None,
        time_checks=None,
        ads=None,
        carts=None,
        intros=None,
    )
    player = _make_player_mock()
    player._cfg.server.allowed_hosts = ["127.0.0.1"]
    player._cfg.server.allowed_origins = [f"http://127.0.0.1:{port}"]
    point_index(player._cfg.index, root / "server")
    app = create_app(PlayerBridge(player=player, sim=_make_sim_mock()))
    original_editable = app.state.stations.editable
    app.state.stations.editable = lambda slug: (
        station if slug == "default" else original_editable(slug)
    )
    static = Path(__file__).resolve().parents[2] / "src/autodj/static"
    html = re.sub(
        r"<script\b[^>]*>.*?</script>", "", (static / "index.html").read_text(), flags=re.S
    )
    html = html.replace("</body>", '<script type="module" src="/review-audit.js"></script></body>')

    @app.get("/review-audit", response_class=HTMLResponse)
    async def audit_page():
        return html

    @app.get("/review-audit.js")
    async def audit_script():
        return Response(
            'import { installProgrammeReview } from "/modules/programme-review.js";\n'
            "window.reviewAudit = installProgrammeReview();\n",
            media_type="text/javascript",
        )

    controls = {"slow": False}
    original_capture = review_source.capture_review

    def capture(station, parameters, *, cancelled=lambda: False):
        while controls["slow"]:
            if cancelled():
                raise InterruptedError("Audit cancellation")
            time.sleep(0.02)
        return original_capture(station, parameters, cancelled=cancelled)

    review_source.capture_review = capture

    @app.post("/audit-control")
    async def control(action: str = Body(embed=True)):
        if action == "edit":
            store.update(
                identity, lambda show: show.model_copy(update={"name": "Edited browser audit"})
            )
        elif action == "missing":
            Path(entries[1].path).rename(root / "missing.held")
            station.cfg.time_checks.tts_command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--tts",
                "{out}",
            ]
        elif action == "tts-failure":
            station.cfg.time_checks.tts_command = []
        elif action == "slow":
            controls["slow"] = True
        elif action == "fast":
            controls["slow"] = False
        else:
            raise ValueError("Unknown audit action")
        return {"ok": True}

    @asynccontextmanager
    async def lifespan(app):
        yield
        if getattr(app.state, "review_jobs", None) is not None:
            app.state.review_jobs.shutdown(5)

    app.router.lifespan_context = lifespan
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--port", type=int, default=8893)
    parser.add_argument("--tts", type=Path)
    parser.add_argument("--verify", type=Path, nargs=2, metavar=("WAV", "MP3"))
    args = parser.parse_args()
    if args.tts:
        tts(args.tts)
    elif args.verify:
        verify(*args.verify)
    else:
        import uvicorn

        uvicorn.run(
            build(args.root, args.port), host="127.0.0.1", port=args.port, log_level="warning"
        )
