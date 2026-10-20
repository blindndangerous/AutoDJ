"""Real child processes verify import and serving without model downloads."""

import os
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import numpy as np
import pytest
import soundfile as sf

from autodj.catalog import LibraryCatalog
from autodj.config import load_config


@pytest.fixture
def runtime(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[library]\nsupported_formats = ["wav"]\n'
        f'[index]\nindex_dir = "{(tmp_path / "index").as_posix()}"\n'
        f'model_dir = "{(tmp_path / "models").as_posix()}"\n'
    )
    env = {name: value for name, value in os.environ.items() if not name.startswith("AUTODJ_")}
    env["AUTODJ_RUNTIME"] = "current"
    return config, load_config(config, environ={}), env


def test_real_index_imports_audio_without_model_analysis(runtime, tmp_path):
    config, cfg, env = runtime
    music = tmp_path / "music"
    music.mkdir()
    song = music / "tone.wav"
    rate = 24000
    sf.write(song, 0.1 * np.sin(2 * np.pi * 440 * np.arange(rate) / rate), rate)
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        catalog.add_source(music, advanced=False)
        catalog.set_setting("dj_analysis", False)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "autodj",
            "--config",
            str(config),
            "index",
            "--no-analyse",
            "--no-enrich",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 songs available" in result.stdout
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        assert catalog.record_for_path(song) is not None
        assert not catalog.has_vector(song)
    assert not list(cfg.index.model_dir.rglob("*"))


def test_real_empty_server_becomes_ready_and_stops(runtime, tmp_path):
    config, cfg, env = runtime
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    opener = build_opener(ProxyHandler({}))
    with (tmp_path / "server.log").open("w+") as log:
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "autodj",
                "--config",
                str(config),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=tmp_path,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and server.poll() is None:
                try:
                    with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=0.5) as reply:
                        assert b'"status":"ok"' in reply.read()
                    break
                except URLError:
                    time.sleep(0.05)
            else:
                log.seek(0)
                pytest.fail("Server did not become ready: " + log.read())
            assert (cfg.index.library_dir / "library.db").is_file()
            assert not list(cfg.index.model_dir.rglob("*"))
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
