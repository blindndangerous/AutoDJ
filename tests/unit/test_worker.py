"""``autodj worker``: the loop against a real server app, hooks, devices, and the CPU job yielding."""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import numpy as np
import pytest
import soundfile as sf
from click.testing import CliRunner

from autodj.analysis_queue import SERVER_HOLDER, WORKER_LEASE_SECONDS, AnalysisQueue
from autodj.cli import cli
from autodj.indexer import EMBEDDING_DIM, FEATURE_DIM, analyse_file, file_fingerprint
from autodj.library import LibraryManager, analyse_library
from autodj.worker import (
    ApiError,
    HttpTransport,
    NetworkError,
    ServerApi,
    Worker,
    WorkerError,
    choose_device,
    release_gpu,
    run_hook,
)
from tests.unit._catalog_vectors import vectors_snapshot
from tests.unit.test_library import Similarity, make_config, make_entry
from tests.unit.test_worker_api import Server


class FakeModel:
    """Stands in for MuQ: a deterministic embedding of the audio it is given."""

    device = "cpu"

    def __init__(self) -> None:
        self.calls = 0

    def embed_array(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        self.calls += 1
        spectrum = np.abs(np.fft.rfft(audio[: 8 * EMBEDDING_DIM]))
        vec = np.resize(spectrum, EMBEDDING_DIM).astype(np.float32) + sample_rate / 1e6
        return vec / np.linalg.norm(vec)


@pytest.fixture
def simulated_model_runtime(monkeypatch):
    """Command tests exercise orchestration with an explicitly simulated model."""
    model = MagicMock(ModelLoadError=RuntimeError, MuqWrapper=FakeModel)
    monkeypatch.setitem(sys.modules, "autodj.model", model)
    monkeypatch.setitem(
        sys.modules, "autodj.compute", MagicMock(device_description=Mock(return_value="CPU"))
    )
    monkeypatch.setattr("autodj.model_identity.model_analysis_available", lambda: True)


class ClientTransport:
    """The worker's transport, over a Starlette TestClient instead of the network."""

    def __init__(self, server: Server, secret: str) -> None:
        self.server = server
        self.secret = secret
        self.paths: list[str] = []

    def request(self, method, path, body=None, sink=None):
        self.paths.append(path)
        reply = self.server.call(method, path, self.secret, json=body)
        if sink is not None and 200 <= reply.status_code < 300:
            sink.write(reply.content)
            return reply.status_code, dict(reply.headers), b""
        return reply.status_code, dict(reply.headers), reply.content


def write_tone(path: Path, frequency: float) -> None:
    rate = 22050
    t = np.arange(rate * 2) / rate
    sf.write(path, (0.3 * np.sin(2 * np.pi * frequency * t)).astype(np.float32), rate)


@pytest.fixture(autouse=True)
def _isolated_config_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def server(tmp_path):
    made = Server(tmp_path / "server", songs=0)
    for n, frequency in enumerate((220.0, 440.0, 660.0)):
        song = made.cfg.library.music_dir / f"tone-{n}.flac"
        write_tone(song, frequency)
        made.manager.catalog.put_entry(make_entry(song))
        made.songs.append(song)
    made.manager.refresh()
    yield made
    made.close()


def make_worker(server, *, analyse=None, **kwargs):
    _token, secret = server.token()
    model = FakeModel()
    transport = ClientTransport(server, secret)
    worker = Worker(
        ServerApi(transport),
        analyse or (lambda path: analyse_file(path, model)),
        server.identity(),
        idle_seconds=0,
        **kwargs,
    )
    return worker, transport, model


def stored_vectors(manager: LibraryManager) -> dict[str, np.ndarray]:
    entries, vectors = vectors_snapshot(manager.catalog)
    return {Path(e.path).name: v for e, v in zip(entries, vectors, strict=True)}


def test_worker_results_match_local_analysis_exactly(server, tmp_path):
    worker, transport, model = make_worker(server, once=True, batch=2)
    stats = worker.run()
    assert (stats.done, stats.failed, stats.stale) == (3, 0, 0)
    assert model.calls == 3
    assert not any("/library" in path for path in transport.paths)
    remote = stored_vectors(server.manager)
    remote_records = {Path(r["path"]).name: r for r in server.manager.catalog.all_entries("music")}

    # The same songs analysed on this machine by the server's own job.
    local_cfg = make_config(tmp_path / "local")
    seed = LibraryManager(local_cfg, Similarity())
    seed.catalog.add_source(local_cfg.library.music_dir)
    for song in server.songs:
        copy = local_cfg.library.music_dir / song.name
        copy.write_bytes(song.read_bytes())
        seed.catalog.put_entry(make_entry(copy))
    assert len(seed.analysis_pending()) == 3
    seed.close()
    assert analyse_library(local_cfg, FakeModel()) == 3
    local = LibraryManager(local_cfg, Similarity())
    try:
        local_vectors = stored_vectors(local)
        local_records = {Path(r["path"]).name: r for r in local.catalog.all_entries("music")}
    finally:
        local.close()
    assert sorted(remote) == sorted(local_vectors) == ["tone-0.flac", "tone-1.flac", "tone-2.flac"]
    for name, vector in remote.items():
        assert vector.shape == (FEATURE_DIM,)
        np.testing.assert_array_equal(vector, local_vectors[name])
        for field in ("energy", "key", "mode", "bpm", "tempo_confidence"):
            assert remote_records[name][field] == local_records[name][field], field
    # Picked up by the live library without a restart.
    assert server.manager._changed()
    server.manager.tick()
    assert server.manager.snapshot()["tracks_indexed"] == 3


def test_ctrl_c_finishes_the_song_in_hand_and_gives_the_rest_back(server):
    stop = threading.Event()
    model = FakeModel()

    def analyse(path):
        stop.set()
        return analyse_file(path, model)

    worker, transport, _ = make_worker(server, analyse=analyse, batch=3, stop=stop)
    stats = worker.run()
    assert stats.done == 1
    assert "/api/worker/release" in transport.paths
    assert server.manager.analysis_queue.held_paths() == set()
    assert len(server.manager.catalog.unembedded_paths()) == 2


def test_a_song_that_fails_is_reported_and_held_back(server):
    def analyse(path):
        raise ValueError("cannot decode")

    worker, transport, _ = make_worker(server, analyse=analyse, once=True, batch=4)
    stats = worker.run()
    assert stats.failed == 3
    assert transport.paths.count("/api/worker/failed") == 3
    assert len(server.manager.analysis_queue.held_paths()) == 3
    assert server.manager.analysis_queue.workers()[0]["songs_failed"] == 3


def test_a_song_that_changed_on_the_server_is_skipped(server):
    model = FakeModel()

    def analyse(path):
        server.songs[0].write_bytes(os.urandom(3000))
        return analyse_file(path, model)

    worker, _transport, _ = make_worker(server, analyse=analyse, once=True, batch=1)
    stats = worker.run()
    assert stats.stale >= 1
    assert not server.manager.catalog.has_vector(server.songs[0])


def test_a_model_mismatch_stops_the_worker_before_any_download(server):
    worker, transport, _ = make_worker(server, once=True)
    worker.identity = {**worker.identity, "model_commit": "f" * 40}
    with pytest.raises(WorkerError, match=r"Update the model on the worker") as refused:
        worker.run()
    assert 'revision = "' in str(refused.value)
    assert transport.paths == ["/api/worker/heartbeat"]


def test_a_refused_token_stops_the_worker(server):
    transport = ClientTransport(server, "adj1.wrong")
    worker = Worker(ServerApi(transport), Mock(), server.identity(), once=True)
    with pytest.raises(WorkerError, match="refused"):
        worker.run()


class FlakyApi:
    """A server that is down for the first few calls."""

    def __init__(self, failures, identity):
        self.failures = list(failures)
        self.identity = identity
        self.claims = 0

    def _maybe_fail(self):
        if self.failures:
            raise self.failures.pop(0)

    def heartbeat(self, device=""):
        self._maybe_fail()
        return {"identity": self.identity, "heartbeat_seconds": 30}

    def claim(self, batch, device=""):
        self._maybe_fail()
        self.claims += 1
        return {"identity": self.identity, "songs": []}


def test_network_errors_back_off_and_recover(server):
    identity = server.identity()
    api = FlakyApi(
        [
            NetworkError("down"),
            NetworkError("down"),
            ApiError(503, "busy"),
            ApiError(429, "slow", 9),
        ],
        identity,
    )
    worker = Worker(api, Mock(), identity, once=True)
    waits = []
    worker._wait = waits.append
    worker.run()
    assert waits == [2.0, 4.0, 8.0, 9]
    assert api.claims == 1


def test_a_stop_during_an_outage_ends_the_loop(server):
    identity = server.identity()
    stop = threading.Event()
    api = FlakyApi([], identity)
    api.claim = Mock(side_effect=NetworkError("down"))
    worker = Worker(api, Mock(), identity, stop=stop)
    worker._wait = lambda seconds: stop.set()
    assert worker.run().done == 0


def test_an_unreachable_server_at_start_is_reported(server):
    identity = server.identity()
    api = FlakyApi([ApiError(404, "Not Found")], identity)
    with pytest.raises(WorkerError, match="Could not start"):
        Worker(api, Mock(), identity).run()


class QueueApi:
    """A server whose claims answer from a script: a list of songs per claim, then none."""

    def __init__(self, identity, claims=(), on_claim=None):
        self.identity = identity
        self.claims = list(claims)
        self.claimed = 0
        self.on_claim = on_claim

    def heartbeat(self, device=""):
        return {"identity": self.identity, "heartbeat_seconds": 30}

    def claim(self, batch, device=""):
        self.claimed += 1
        if self.on_claim is not None:
            self.on_claim(self.claimed)
        songs = self.claims.pop(0) if self.claims else []
        return {"identity": self.identity, "songs": songs, "waiting": len(songs)}

    def release(self, leases):
        pass


def idle_worker(api, identity, clock, **kwargs):
    """A worker whose waits move *clock* on instead of sleeping."""
    worker = Worker(api, Mock(), identity, idle_seconds=30, **kwargs)
    worker._now = lambda: clock[0]

    def wait(seconds):
        clock[0] += seconds

    worker._wait = wait
    return worker


def test_idle_exit_waits_for_the_window_then_stops(server, caplog):
    caplog.set_level("INFO", logger="autodj.worker")
    identity = server.identity()
    api = QueueApi(identity)
    clock = [100.0]
    worker = idle_worker(api, identity, clock, idle_exit=600)
    worker.run()
    # Asked at once and every half minute for ten minutes.
    assert api.claimed == 21
    assert clock[0] == 700.0
    assert "Queue empty; waiting up to 10 minutes for new songs." in caplog.text
    assert "Queue empty for 10 minutes; stopping." in caplog.text


def test_idle_exit_starts_the_wait_again_after_new_songs(server, caplog):
    caplog.set_level("INFO", logger="autodj.worker")
    identity = server.identity()
    # Empty, empty, then a song turns up just as the window would close.
    api = QueueApi(identity, [[], [], [{"lease": "a" * 32}]])
    clock = [0.0]
    worker = idle_worker(api, identity, clock, idle_exit=60)
    processed = []
    worker._process = lambda song, folder, number: processed.append((song["lease"], clock[0]))
    worker.run()
    assert processed == [("a" * 32, 60.0)]
    # The full minute again from the song, not what was left of the first wait.
    assert clock[0] == 120.0
    assert api.claimed == 6
    assert caplog.text.count("Queue empty; waiting up to 1 minute for new songs.") == 2


def test_idle_exit_zero_and_once_stop_at_the_first_empty_claim(server):
    identity = server.identity()
    for kwargs in ({"once": True}, {"idle_exit": 0}):
        api = QueueApi(identity)
        clock = [0.0]
        idle_worker(api, identity, clock, **kwargs).run()
        assert (api.claimed, clock[0]) == (1, 0.0)
    # --idle-exit wins over --once.
    api = QueueApi(identity)
    clock = [0.0]
    idle_worker(api, identity, clock, once=True, idle_exit=60).run()
    assert (api.claimed, clock[0]) == (3, 60.0)


# ---------------------------------------------------------------------------
# Hooks, devices, transport
# ---------------------------------------------------------------------------


def _python(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def test_hooks_run_without_a_shell_and_never_raise(tmp_path, caplog):
    marker = tmp_path / "ran.txt"
    code = f"open(r'{marker}', 'w').write('yes')"
    assert run_hook(_python(code), "Before-command")
    assert marker.read_text() == "yes"
    assert run_hook(None, "After-command")
    assert run_hook("", "After-command")
    assert not run_hook(_python("raise SystemExit(3)"), "After-command")
    assert "exited with code 3" in caplog.text
    assert not run_hook("no-such-program-for-autodj", "Before-command")
    assert not run_hook(_python("import time; time.sleep(5)"), "Before-command", timeout=0.2)
    # Shell syntax is just text handed to the program: no shell runs it.
    argv_file = tmp_path / "argv.txt"
    code = f"import sys; open(r'{argv_file}', 'w').write(repr(sys.argv[1:]))"
    assert run_hook(_python(code) + " && echo hi", "Before-command")
    assert argv_file.read_text() == "['&&', 'echo', 'hi']"
    assert not run_hook('"unclosed', "Before-command")


def test_device_choice_uses_cuda_whenever_torch_sees_it():
    pytest.importorskip("torch")
    with patch("torch.cuda.is_available", return_value=True):
        assert choose_device("auto") == "cuda"
        assert choose_device("cuda") == "cuda"
        assert choose_device("cpu") == "cpu"
    with patch("torch.cuda.is_available", return_value=False):
        assert choose_device("auto") == "cpu"
        with pytest.raises(WorkerError, match="is_available"):
            choose_device("cuda")


def test_release_gpu_moves_the_model_off_and_empties_the_cache():
    pytest.importorskip("torch")
    wrapper = Mock(device="cuda", model=Mock())
    with (
        patch("torch.cuda.is_available", return_value=True),
        patch("torch.cuda.empty_cache") as empty,
    ):
        release_gpu(wrapper)
    wrapper.model.to.assert_called_once_with("cpu")
    assert wrapper.device == "cpu"
    empty.assert_called_once()


def test_http_transport_sends_the_token_only_where_it_is_safe():
    with pytest.raises(WorkerError, match="HTTPS"):
        HttpTransport("http://192.168.1.5:8080", "t")
    with pytest.raises(WorkerError):
        HttpTransport("ftp://nas", "t")
    HttpTransport("https://nas.local:8080", "t")
    HttpTransport("http://localhost:8080", "t")
    HttpTransport("http://127.0.0.1:8080", "t")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, status, body, headers=()):
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/worker/audio"):
            self._reply(200, b"x" * 100)
        elif self.path == "/moved":
            self._reply(302, b"", [("Location", "http://127.0.0.1:1/steal")])
        else:
            self._reply(404, json.dumps({"detail": "nope"}).encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        auth = self.headers.get("Authorization", "")
        if self.path == "/api/worker/heartbeat":
            self._reply(200, json.dumps({"auth": auth, "body": body}).encode())
        else:
            self._reply(429, b'{"detail": "slow"}', [("Retry-After", "4")])


@pytest.fixture
def http_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_http_transport_talks_to_a_real_socket(http_server, tmp_path):
    transport = HttpTransport(http_server, "adj1.secret", timeout=5)
    api = ServerApi(transport)
    assert api.heartbeat("GPU") == {"auth": "Bearer adj1.secret", "body": {"device": "GPU"}}
    target = tmp_path / "song.flac"
    api.download("a" * 32, target, 100)
    assert target.read_bytes() == b"x" * 100
    with pytest.raises(ValueError):
        api.download("a" * 32, target, 50)
    with pytest.raises(ApiError) as limited:
        api.claim(1)
    assert (limited.value.status, limited.value.retry_after) == (429, 4.0)
    assert limited.value.transient
    status, _headers, _body = transport.request("GET", "/moved")
    assert status == 302
    with pytest.raises(ApiError) as missing:
        api.submit({})
    assert not ApiError(404, "x").transient
    assert missing.value.status == 429
    with pytest.raises(NetworkError):
        HttpTransport("http://127.0.0.1:1", "t", timeout=2).request("GET", "/")


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def test_worker_command_runs_hooks_around_the_model_and_frees_the_gpu(
    tmp_path, simulated_model_runtime
):
    order = []
    fake_worker = MagicMock()
    fake_worker.connect.side_effect = lambda: order.append("connect")
    fake_worker.run.side_effect = lambda: order.append("run")
    with (
        patch("autodj.worker.Worker", return_value=fake_worker),
        patch("autodj.worker.choose_device", return_value="cuda"),
        patch("autodj.compute.device_description", return_value="RTX (CUDA GPU)"),
        patch("autodj.worker.run_hook", side_effect=lambda cmd, label: order.append(label)),
        patch("autodj.worker.release_gpu", side_effect=lambda w: order.append("release")),
        patch("autodj.model.download_model_if_needed", return_value=tmp_path),
        patch("autodj.model.load_model", return_value=Mock()) as load,
    ):
        result = CliRunner().invoke(
            cli,
            [
                "worker",
                "--server",
                "https://nas.local:8080",
                "--token",
                "adj1.x",
                "--once",
                "--before-command",
                "podman stop tts-service",
                "--after-command",
                "podman start tts-service",
            ],
        )
    assert result.exit_code == 0, result.output
    assert order == ["connect", "Before-command", "run", "release", "After-command"]
    assert load.call_args.kwargs == {"device": "cuda"}


def test_worker_command_runs_the_after_command_on_errors(tmp_path, simulated_model_runtime):
    order = []
    fake_worker = MagicMock()
    fake_worker.run.side_effect = WorkerError("token revoked")
    with (
        patch("autodj.worker.Worker", return_value=fake_worker),
        patch("autodj.worker.choose_device", return_value="cpu"),
        patch("autodj.worker.run_hook", side_effect=lambda cmd, label: order.append(label)),
        patch("autodj.worker.release_gpu", side_effect=lambda w: order.append("release")),
        patch("autodj.model.download_model_if_needed", return_value=tmp_path),
        patch("autodj.model.load_model", return_value=Mock()),
    ):
        result = CliRunner().invoke(
            cli, ["worker", "--server", "https://nas:1", "--token", "adj1.x"]
        )
    assert result.exit_code == 1
    assert "token revoked" in result.output
    assert order == ["Before-command", "release", "After-command"]


def _run_idle_command(tmp_path, api, order, *args, record_model=False, load_error=None):
    """Run ``autodj worker`` with a real Worker against *api*, noting hooks in *order*."""
    clock = [0.0]

    def wait(self, seconds):
        clock[0] += seconds

    def load(*args, **kwargs):
        if record_model:
            order.append("load")
        if load_error is not None:
            raise load_error
        return FakeModel()

    with (
        patch("autodj.worker.ServerApi", return_value=api),
        patch.object(Worker, "check_identity"),
        patch.object(Worker, "_now", lambda self: clock[0]),
        patch.object(Worker, "_wait", wait),
        patch("autodj.worker.choose_device", return_value="cuda"),
        patch("autodj.compute.device_description", return_value="RTX (CUDA GPU)"),
        patch("autodj.worker.run_hook", side_effect=lambda cmd, label: order.append(label)),
        patch("autodj.worker.release_gpu", side_effect=lambda w: order.append("release")),
        patch("autodj.model.download_model_if_needed", return_value=tmp_path),
        patch("autodj.model.load_model", side_effect=load),
    ):
        result = CliRunner().invoke(
            cli,
            [
                "worker",
                "--server",
                "https://nas.local:8080",
                "--token",
                "adj1.x",
                *args,
                "--before-command",
                "podman stop tts-service",
                "--after-command",
                "podman start tts-service",
            ],
        )
    return result


def test_idle_release_keeps_an_empty_worker_unloaded(server, tmp_path, simulated_model_runtime):
    order = []
    api = QueueApi(server.identity(), on_claim=lambda n: order.append("claim"))
    result = _run_idle_command(
        tmp_path, api, order, "--idle-release", "1", "--idle-exit", "2", record_model=True
    )
    assert result.exit_code == 0, result.output
    assert order == ["claim"] * 5


def test_idle_release_frees_the_model_and_reloads_for_new_songs(
    server, tmp_path, simulated_model_runtime
):
    order = []
    song = {"lease": "a" * 32}
    api = QueueApi(
        server.identity(),
        [[], [song], [], [], [], [], [song]],
        on_claim=lambda n: order.append("claim"),
    )
    with patch.object(Worker, "_process", side_effect=lambda *args: order.append("analyse")):
        result = _run_idle_command(
            tmp_path, api, order, "--idle-release", "1", "--idle-exit", "2", record_model=True
        )
    assert result.exit_code == 0, result.output
    cycle = ["Before-command", "load", "analyse", *["claim"] * 3, "release", "After-command"]
    assert order == ["claim", "claim", *cycle, "claim", "claim", *cycle, "claim", "claim"]
    assert result.output.count("Queue empty for 1 minute; released analysis resources.") == 2


@pytest.mark.parametrize("exit_args", [("--once",), ("--idle-exit", "1")])
def test_idle_exit_wins_over_release_and_cleans_up_once(
    server, tmp_path, exit_args, simulated_model_runtime
):
    order = []
    api = QueueApi(server.identity(), [[{"lease": "a" * 32}]])
    with patch.object(Worker, "_process", side_effect=lambda *args: order.append("analyse")):
        result = _run_idle_command(
            tmp_path, api, order, "--idle-release", "1", *exit_args, record_model=True
        )
    assert result.exit_code == 0, result.output
    assert order == ["Before-command", "load", "analyse", "release", "After-command"]
    assert "released analysis resources" not in result.output


@pytest.mark.parametrize("loaded", [False, True])
@pytest.mark.parametrize("error", [KeyboardInterrupt(), WorkerError("token revoked")])
def test_idle_release_exit_only_restores_an_acquired_model(
    server, tmp_path, loaded, error, simulated_model_runtime
):
    order = []

    def fail(count):
        if count == (2 if loaded else 1):
            raise error

    api = QueueApi(server.identity(), [[{"lease": "a" * 32}]] if loaded else [], fail)
    with patch.object(Worker, "_process", side_effect=lambda *args: order.append("analyse")):
        result = _run_idle_command(tmp_path, api, order, "--idle-release", "1", record_model=True)
    assert result.exit_code == (0 if isinstance(error, KeyboardInterrupt) else 1), result.output
    assert order == (
        ["Before-command", "load", "analyse", "release", "After-command"] if loaded else []
    )


def test_idle_release_restores_the_other_service_when_model_loading_fails(
    server, tmp_path, simulated_model_runtime
):
    from autodj.model import ModelLoadError

    order = []
    api = QueueApi(server.identity(), [[{"lease": "a" * 32}]])
    api.release = lambda leases: order.append(("leases", leases))
    result = _run_idle_command(
        tmp_path,
        api,
        order,
        "--idle-release",
        "1",
        record_model=True,
        load_error=ModelLoadError("no memory"),
    )
    assert result.exit_code == 1
    assert "no memory" in result.output
    assert order == ["Before-command", "load", ("leases", ["a" * 32]), "After-command"]


def test_heartbeats_keep_claims_alive_while_preparing_the_model(server):
    loading = threading.Event()
    renewed = threading.Event()

    def prepare():
        server.clock.now += WORKER_LEASE_SECONDS - 1
        loading.set()
        assert renewed.wait(5), "No heartbeat renewed the claimed songs during preparation"
        server.clock.now += 2
        assert len(server.manager.analysis_queue.held_paths()) == 3

    worker, _, model = make_worker(server, once=True, idle_release=60, prepare=prepare)
    heartbeat = worker.api.heartbeat

    def beat(device=""):
        reply = heartbeat(device)
        if loading.is_set() and reply["renewed"] == 3:
            renewed.set()
        return reply

    worker.api.heartbeat = beat
    worker.connect()
    worker._heartbeat_every = 0.01
    stats = worker.run()
    assert stats.done == model.calls == 3


def test_idle_exit_runs_each_hook_once_around_all_the_empty_polls(
    server, tmp_path, simulated_model_runtime
):
    identity = server.identity()
    order = []
    api = QueueApi(identity, on_claim=lambda n: order.append("claim"))
    result = _run_idle_command(tmp_path, api, order, "--idle-exit", "2")
    assert result.exit_code == 0, result.output
    # Asked at once and every half minute for two minutes, the hooks only around it all.
    assert order == ["Before-command", *["claim"] * 5, "release", "After-command"]


def test_ctrl_c_while_waiting_for_new_songs_still_runs_the_after_command(
    server, tmp_path, simulated_model_runtime
):
    import signal

    identity = server.identity()

    def press_ctrl_c(count):
        if count == 3:
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)

    api = QueueApi(identity, on_claim=press_ctrl_c)
    order = []
    result = _run_idle_command(tmp_path, api, order, "--idle-exit", "10", "--once")
    assert result.exit_code == 0, result.output
    assert "Stopping after the current song" in result.output
    assert api.claimed == 3
    assert order == ["Before-command", "release", "After-command"]


def test_worker_command_refuses_plain_http_to_another_machine(simulated_model_runtime):
    result = CliRunner().invoke(
        cli, ["worker", "--server", "http://192.168.1.5:8080", "--token", "adj1.x"]
    )
    assert result.exit_code == 1
    assert "HTTPS" in result.output


# ---------------------------------------------------------------------------
# The server's CPU job yields to a connected worker
# ---------------------------------------------------------------------------


def _pending_manager(tmp_path, count=2):
    cfg = make_config(tmp_path)
    manager = LibraryManager(cfg, Similarity())
    manager.catalog.add_source(cfg.library.music_dir)
    songs = []
    for n in range(count):
        song = cfg.library.music_dir / f"song-{n}.flac"
        song.write_bytes(os.urandom(500 + n))
        manager.catalog.put_entry(make_entry(song))
        songs.append(song)
    manager.refresh()
    return cfg, manager, songs


def test_cpu_analysis_waits_while_a_worker_is_connected_and_resumes_after(tmp_path):
    _cfg, manager, _songs = _pending_manager(tmp_path)
    clock = [1_000_000.0]
    manager.analysis_queue._now = lambda: clock[0]
    jobs = Mock()
    jobs.start.return_value = True
    try:
        with (
            patch("autodj.model_identity.model_analysis_available", return_value=True),
            patch("autodj.jobs.get_manager", return_value=jobs),
        ):
            assert manager.schedule_analysis() is True
            assert jobs.start.call_count == 1
            manager.analysis_queue.seen("token", "Studio PC")
            assert manager.schedule_analysis() is False
            assert jobs.start.call_count == 1
            clock[0] += 3600
            # The worker went away with songs left: the CPU job runs again.
            assert manager.schedule_analysis() is True
            assert jobs.start.call_count == 2
    finally:
        manager.close()


def _gated_embed(decisions):
    def embed(tracks, wrapper, workers, checkpoint, throttle_ms, report, gate=None):
        entries, vectors = [], []
        for track in tracks:
            decision = gate(track)
            decisions.append(decision)
            if decision is None:
                break
            if not decision:
                continue
            entries.append(make_entry(track.path))
            entries[-1].size, entries[-1].fingerprint = file_fingerprint(track.path)
            vector = np.zeros(FEATURE_DIM, dtype=np.float32)
            vector[0] = 1
            vectors.append(vector)
            checkpoint(entries, vectors)
        return entries, vectors

    return embed


def test_the_cpu_job_stops_taking_songs_when_a_worker_connects(tmp_path):
    cfg, manager, _songs = _pending_manager(tmp_path)
    manager.analysis_queue.seen("token", "Studio PC")
    manager.close()
    decisions = []
    with patch("autodj.library._embed_new_tracks", side_effect=_gated_embed(decisions)):
        assert analyse_library(cfg, object()) == 0
    assert decisions == [None]
    # --force still analyses everything, leasing as it goes.
    decisions.clear()
    with patch("autodj.library._embed_new_tracks", side_effect=_gated_embed(decisions)):
        assert analyse_library(cfg, object(), force=True) == 2
    assert decisions == [True, True]


def test_the_cpu_job_skips_songs_a_worker_holds_and_leases_its_own(tmp_path):
    cfg, manager, songs = _pending_manager(tmp_path)
    record = manager.catalog.record_for_path(songs[0])
    manager.analysis_queue.claim(
        "token", [(record["path"], record["size"], record["fingerprint"])], WORKER_LEASE_SECONDS
    )
    manager.close()
    decisions = []
    seen_leases = []

    def embed(tracks, wrapper, workers, checkpoint, throttle_ms, report, gate=None):
        result = _gated_embed(decisions)
        queue = AnalysisQueue(cfg.index.library_dir / "library.db")
        try:
            out = result(tracks, wrapper, workers, lambda e, v: None, throttle_ms, report, gate)
            seen_leases.append(queue.held_paths())
        finally:
            queue.close()
        for i in range(len(out[0])):
            checkpoint(out[0][: i + 1], out[1][: i + 1])
        return out

    with patch("autodj.library._embed_new_tracks", side_effect=embed):
        assert analyse_library(cfg, object()) == 1
    assert decisions == [False, True]
    # While analysing, the CPU job held the song it took.
    assert seen_leases[0] == {record["path"], _norm(songs[1])}
    check = AnalysisQueue(cfg.index.library_dir / "library.db")
    try:
        # Done: its own lease is gone, the worker's remains.
        assert check.held_paths() == {record["path"]}
        assert not check.claim(SERVER_HOLDER, [(record["path"], 1, "x")], 60)
    finally:
        check.close()


def _norm(path):
    from autodj.catalog import _path

    return _path(path)


def test_worker_command_checks_the_weights_before_stopping_anything(
    tmp_path, simulated_model_runtime
):
    order = []
    fake_worker = MagicMock()
    fake_worker.check_identity.side_effect = WorkerError("Update the model on the worker")
    with (
        patch("autodj.worker.Worker", return_value=fake_worker),
        patch("autodj.worker.choose_device", return_value="cpu"),
        patch("autodj.worker.run_hook", side_effect=lambda cmd, label: order.append(label)),
        patch("autodj.model.download_model_if_needed", return_value=tmp_path),
        patch("autodj.model.load_model") as load,
    ):
        result = CliRunner().invoke(
            cli, ["worker", "--server", "https://nas:1", "--token", "adj1.x"]
        )
    assert result.exit_code == 1
    assert "Update the model" in result.output
    assert order == []
    load.assert_not_called()


def test_a_rescan_clears_holds_on_songs_whose_file_changed(tmp_path):
    from autodj.catalog import _path

    cfg = make_config(tmp_path)
    changed = cfg.library.music_dir / "changed.flac"
    same = cfg.library.music_dir / "same.flac"
    write_tone(changed, 220.0)
    write_tone(same, 330.0)
    manager = LibraryManager(cfg, Similarity())
    try:
        manager.catalog.add_source(cfg.library.music_dir)
        manager.scan()
        queue = manager.analysis_queue
        songs = []
        for path in (changed, same):
            record = manager.catalog.record_for_path(path)
            songs.append((record["path"], record["size"], record["fingerprint"]))
        stale, failed = queue.claim("token", songs, WORKER_LEASE_SECONDS)
        queue.finish(stale, "stale")
        queue.finish(failed, "failed")
        assert queue.held_paths() == {_path(changed), _path(same)}
        write_tone(changed, 880.0)
        manager.scan()
        # The changed song is a new chance; the unchanged failure still waits.
        assert queue.held_paths() == {_path(same)}
        assert not queue.clear_holds(_path(same), songs[1][1], songs[1][2])
    finally:
        manager.close()
