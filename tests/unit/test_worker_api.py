"""The remote analysis worker API: token rules, leases, result checks, Settings routes."""

import base64
import logging
import os
import secrets
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from fastapi.testclient import TestClient

from autodj.analysis_queue import WORKER_ACTIVE_SECONDS, WORKER_LEASE_SECONDS
from autodj.config import ServerConfig
from autodj.indexer import ANALYSIS_VERSION, FEATURE_DIM, analysis_identity
from autodj.library import LibraryManager
from autodj.machine_tokens import SCOPE_ANALYSIS_WORKER, SCOPE_ANNOUNCER
from autodj.server import create_app
from autodj.worker_api import RateLimiter, decode_vector
from tests.unit._catalog_vectors import vectors_snapshot
from tests.unit.test_library import Similarity, make_config, make_entry

HTTPS = "https://127.0.0.1:8080"
COMMIT = "c" * 40


def fake_model_cache(cfg, commit=COMMIT):
    """A HuggingFace cache holding the configured model at *commit*, as a download leaves it."""
    repo = cfg.index.model_dir / f"models--{cfg.model.name.replace('/', '--')}"
    (repo / "refs").mkdir(parents=True, exist_ok=True)
    (repo / "refs" / cfg.model.revision).write_text(commit)
    snapshot = repo / "snapshots" / commit
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "config.json").write_text("{}")
    return snapshot


class Clock:
    def __init__(self) -> None:
        self.now = 2_000_000.0

    def __call__(self) -> float:
        return self.now


class Server:
    def __init__(self, tmp_path, *, songs=3, access_token=None, library=True, model=True):
        self.cfg = make_config(tmp_path)
        if model:
            fake_model_cache(self.cfg)
        if access_token:
            self.cfg.server = ServerConfig(access_token=access_token)
        self.manager = LibraryManager(self.cfg, Similarity()) if library else None
        self.clock = Clock()
        self.songs = []
        if self.manager is not None:
            self.manager.start_scan = Mock()
            self.manager.analysis_queue._now = self.clock
            self.manager.catalog.add_source(self.cfg.library.music_dir)
            for n in range(songs):
                song = self.cfg.library.music_dir / f"song-{n}.flac"
                song.write_bytes(os.urandom(1000 + n))
                self.manager.catalog.put_entry(make_entry(song))
                self.songs.append(song)
            self.manager.refresh()
        bridge = SimpleNamespace(player=SimpleNamespace(_cfg=self.cfg), library=self.manager)
        self.app = create_app(bridge)
        self.client = TestClient(self.app, base_url=HTTPS)

    def token(self, name="Studio PC", scopes=(SCOPE_ANALYSIS_WORKER,)):
        return self.app.state.machine_tokens().create(name, list(scopes))

    def call(self, method, path, secret, **kwargs):
        headers = {"Authorization": f"Bearer {secret}", **kwargs.pop("headers", {})}
        return self.client.request(method, path, headers=headers, **kwargs)

    def identity(self):
        return analysis_identity(self.cfg.model.name, COMMIT)

    def result(self, song, lease, **overrides):
        vector = np.random.default_rng(1).normal(size=FEATURE_DIM).astype(np.float32)
        vector /= np.linalg.norm(vector)
        body = {
            "lease": lease,
            **self.identity(),
            "size": song["size"],
            "fingerprint": song["fingerprint"],
            "vector": _vector_text(vector),
            "energy": 0.3,
            "key": 4,
            "mode": 0,
            "bpm": 128.0,
            "tempo_confidence": 0.8,
        }
        body.update(overrides)
        return body, vector

    def close(self):
        if self.manager is not None:
            self.manager.close()


@pytest.fixture
def server(tmp_path):
    made = Server(tmp_path)
    yield made
    made.close()


def claim(server, secret, batch=1):
    reply = server.call("POST", "/api/worker/claim", secret, json={"batch": batch})
    assert reply.status_code == 200, reply.text
    return reply.json()


# ---------------------------------------------------------------------------
# Authentication and transport
# ---------------------------------------------------------------------------


def test_worker_routes_need_a_valid_bearer_token(server):
    token, secret = server.token()
    missing = server.client.post("/api/worker/claim", json={})
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert server.call("POST", "/api/worker/claim", "adj1.wrong", json={}).status_code == 401
    basic = server.client.post("/api/worker/claim", json={}, headers={"Authorization": "Basic abc"})
    assert basic.status_code == 401
    assert server.call("POST", "/api/worker/claim", secret, json={}).status_code == 200
    server.app.state.machine_tokens().revoke(token.token_id)
    assert server.call("POST", "/api/worker/claim", secret, json={}).status_code == 401


def test_a_token_without_the_worker_scope_is_refused(server, monkeypatch):
    monkeypatch.setitem(
        __import__("autodj.machine_tokens").machine_tokens.KNOWN_SCOPES, "news-bot", "News bot"
    )
    _token, secret = server.token(scopes=("news-bot",))
    assert server.call("POST", "/api/worker/claim", secret, json={}).status_code == 401


def test_tokens_are_refused_over_plain_http_except_from_this_machine(server):
    _token, secret = server.token()
    headers = {"Authorization": f"Bearer {secret}"}
    remote = TestClient(server.app, base_url="http://127.0.0.1:8080", client=("192.168.1.9", 5))
    refused = remote.post("/api/worker/claim", json={}, headers=headers)
    assert refused.status_code == 403
    assert refused.json()["detail"] == "Machine tokens need HTTPS"
    local = TestClient(server.app, base_url="http://127.0.0.1:8080", client=("127.0.0.1", 5))
    assert local.post("/api/worker/claim", json={}, headers=headers).status_code == 200


def test_browser_requests_and_sessions_do_not_reach_worker_routes(server):
    _token, secret = server.token()
    browser = server.call("POST", "/api/worker/claim", secret, json={}, headers={"Origin": HTTPS})
    assert browser.status_code == 403


def test_a_machine_token_opens_nothing_but_the_worker_routes(tmp_path):
    """ADR-0007-02."""
    made = Server(tmp_path, access_token=secrets.token_urlsafe(32))
    try:
        _token, secret = made.token()
        assert made.call("GET", "/api/machine-tokens", secret).status_code == 401
        assert made.call("GET", "/api/status", secret).status_code == 401
        assert made.call("POST", "/api/worker/claim", secret, json={}).status_code == 200
    finally:
        made.close()


# ---------------------------------------------------------------------------
# Claims and audio
# ---------------------------------------------------------------------------


def test_claims_lease_each_song_to_one_worker_and_expired_leases_return(server):
    _a, first = server.token("A")
    _b, second = server.token("B")
    one = claim(server, first, batch=2)
    assert one["identity"] == server.identity()
    assert one["lease_seconds"] == WORKER_LEASE_SECONDS
    assert one["waiting"] == 3
    assert [song["suffix"] for song in one["songs"]] == [".flac", ".flac"]
    assert "path" not in one["songs"][0]
    two = claim(server, second, batch=16)
    assert len(two["songs"]) == 1
    assert claim(server, second, batch=4)["songs"] == []
    # A worker that died: its songs come back once the lease runs out.
    server.clock.now += WORKER_LEASE_SECONDS + 1
    again = claim(server, second, batch=4)
    assert len(again["songs"]) == 3


def test_claim_retries_after_other_worker_takes_selected_songs(server, monkeypatch):
    _worker, secret = server.token("Waiting worker")
    queue = server.manager.analysis_queue
    real_claim = queue.claim
    catalog = server.manager.catalog
    real_paths = catalog.unembedded_paths
    queries = []

    def counted_paths():
        queries.append(1)
        return real_paths()

    monkeypatch.setattr(catalog, "unembedded_paths", counted_paths)
    raced = False

    def claim_with_race(holder, songs, seconds):
        nonlocal raced
        if not raced:
            raced = True
            real_claim("Other worker", songs, seconds)
            return []
        return real_claim(holder, songs, seconds)

    monkeypatch.setattr(queue, "claim", claim_with_race)
    assert len(claim(server, secret, batch=2)["songs"]) == 1
    assert len(queries) == 1


def test_audio_is_served_only_for_a_song_this_worker_claimed(server):
    _a, first = server.token("A")
    _b, second = server.token("B")
    song = claim(server, first)["songs"][0]
    path = server.songs[0]
    reply = server.call("GET", "/api/worker/audio", first, params={"lease": song["lease"]})
    assert reply.status_code == 200
    assert reply.content == path.read_bytes()
    assert reply.headers["content-type"] == "application/octet-stream"
    other = server.call("GET", "/api/worker/audio", second, params={"lease": song["lease"]})
    assert other.status_code == 404
    unknown = server.call("GET", "/api/worker/audio", first, params={"lease": "0" * 32})
    assert unknown.status_code == 404
    bad = server.call("GET", "/api/worker/audio", first, params={"lease": "../etc"})
    assert bad.status_code == 422
    path.write_bytes(os.urandom(5000))
    changed = server.call("GET", "/api/worker/audio", first, params={"lease": song["lease"]})
    assert changed.status_code == 409


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("no_model", [False, True])
def test_a_valid_result_goes_into_the_live_catalog(server, monkeypatch, no_model):
    """ADR-0012-07: cached identity permits remote results without local inference."""
    monkeypatch.setenv("AUTODJ_NO_MODEL", "1" if no_model else "0")
    if no_model:
        monkeypatch.setitem(sys.modules, "autodj.model", None)
        monkeypatch.setitem(sys.modules, "torch", None)
    _token, secret = server.token()
    song = claim(server, secret)["songs"][0]
    body, vector = server.result(song, song["lease"])
    before = server.manager.snapshot()["tracks_indexed"]
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 200, reply.text
    catalog = server.manager.catalog
    record = catalog.record_for_path(server.songs[0])
    assert (record["energy"], record["key"], record["mode"]) == (0.3, 4, 0)
    # The tag BPM stays; librosa's estimate is only for songs without one.
    assert record["bpm"] == 120
    assert catalog.has_vector(server.songs[0])
    assert server.manager._changed()
    server.manager.refresh()
    assert server.manager.snapshot()["tracks_indexed"] == before + 1
    _entries, vectors = vectors_snapshot(catalog)
    np.testing.assert_array_equal(vectors[0], vector)
    (worker,) = server.manager.analysis_queue.workers()
    assert worker["songs_done"] == 1
    assert server.manager.analysis_queue.held_paths() == set()
    # The lease is used up.
    assert server.call("POST", "/api/worker/result", secret, json=body).status_code == 404


@pytest.mark.parametrize(
    "field,value,advice",
    [
        ("model", "OpenMuQ/other", "Set [model] name"),
        ("model_commit", "d" * 40, f'revision = "{COMMIT}"'),
        ("model_commit", "sha256:" + "e" * 64, "Update the model on the worker"),
        ("analysis_version", "old", ANALYSIS_VERSION),
        ("dimension", 1024, "Update AutoDJ on both machines"),
    ],
)
def test_results_from_other_weights_or_versions_are_refused(server, field, value, advice):
    """ADR-0006-04."""
    _token, secret = server.token()
    song = claim(server, secret)["songs"][0]
    body, _ = server.result(song, song["lease"], **{field: value})
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 409
    assert advice in reply.json()["detail"]
    assert not server.manager.catalog.has_vector(server.songs[0])


@pytest.mark.parametrize("no_model", [False, True])
def test_a_server_without_the_model_cannot_check_results(tmp_path, monkeypatch, no_model):
    """ADR-0012-07: absent identity is refused with usable cache recovery advice."""
    monkeypatch.setenv("AUTODJ_NO_MODEL", "1" if no_model else "0")
    made = Server(tmp_path, model=False)
    try:
        _token, secret = made.token()
        for path, body in (
            ("/api/worker/claim", {}),
            ("/api/worker/heartbeat", {}),
        ):
            reply = made.call("POST", path, secret, json=body)
            assert reply.status_code == 409
            detail = reply.json()["detail"]
            assert "no downloaded copy" in detail
            assert "existing compatible model cache" in detail
            assert "model-capable runtime" in detail
            assert "No-model indexing does not download weights" in detail
    finally:
        made.close()


def test_the_server_names_its_weights_by_snapshot_commit_not_branch(tmp_path):
    made = Server(tmp_path)
    try:
        _token, secret = made.token()
        assert claim(made, secret)["identity"]["model_commit"] == COMMIT
        assert "revision" not in claim(made, secret)["identity"]
    finally:
        made.close()


def _vector_text(values):
    return base64.b64encode(np.asarray(values, dtype="<f4").tobytes()).decode()


@pytest.mark.parametrize(
    "vector",
    [
        "not base64!",
        _vector_text(np.ones(FEATURE_DIM - 1) / np.sqrt(FEATURE_DIM - 1)),
        _vector_text([np.nan] + [0.0] * (FEATURE_DIM - 1)),
        _vector_text(np.ones(FEATURE_DIM)),
    ],
)
def test_invalid_vectors_are_refused(server, vector):
    """ADR-0006-04."""
    _token, secret = server.token()
    song = claim(server, secret)["songs"][0]
    body, _ = server.result(song, song["lease"], vector=vector)
    assert server.call("POST", "/api/worker/result", secret, json=body).status_code == 422
    assert not server.manager.catalog.has_vector(server.songs[0])


@pytest.mark.parametrize(
    "overrides", [{"key": 12}, {"mode": 2}, {"tempo_confidence": 1.5}, {"extra": 1}, {"size": 0}]
)
def test_out_of_range_fields_are_refused(server, overrides):
    _token, secret = server.token()
    song = claim(server, secret)["songs"][0]
    body, _ = server.result(song, song["lease"], **overrides)
    assert server.call("POST", "/api/worker/result", secret, json=body).status_code == 422


def test_a_result_for_a_changed_file_is_refused_as_stale(server):
    """ADR-0006-04."""
    _token, secret = server.token()
    song = claim(server, secret)["songs"][0]
    body, _ = server.result(song, song["lease"])
    server.songs[0].write_bytes(os.urandom(4000))
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 409
    assert "changed" in reply.json()["detail"]
    assert not server.manager.catalog.has_vector(server.songs[0])
    assert server.manager.analysis_queue.workers()[0]["songs_stale"] == 1


def test_a_result_naming_other_bytes_than_claimed_is_stale(server):
    _token, secret = server.token()
    song = claim(server, secret)["songs"][0]
    body, _ = server.result(song, song["lease"], fingerprint="f" * 64)
    assert server.call("POST", "/api/worker/result", secret, json=body).status_code == 409


def test_a_result_needs_the_lease_of_this_worker(server):
    _a, first = server.token("A")
    _b, second = server.token("B")
    song = claim(server, first)["songs"][0]
    body, _ = server.result(song, song["lease"])
    assert server.call("POST", "/api/worker/result", second, json=body).status_code == 404


def test_a_failed_song_is_held_back_and_logged(server, caplog):
    _token, secret = server.token()
    song = claim(server, secret)["songs"][0]
    with caplog.at_level(logging.WARNING):
        reply = server.call(
            "POST",
            "/api/worker/failed",
            secret,
            json={"lease": song["lease"], "reason": "cannot\ndecode"},
        )
    assert reply.status_code == 200
    assert "cannot decode" in caplog.text
    server.call("POST", "/api/worker/release", secret, json={"leases": []})
    assert len(claim(server, secret, batch=16)["songs"]) == 2
    missing = server.call("POST", "/api/worker/failed", secret, json={"lease": "0" * 32})
    assert missing.status_code == 404


def test_heartbeat_keeps_claims_and_release_gives_them_back(server):
    _a, first = server.token("A")
    _b, second = server.token("B")
    songs = claim(server, first, batch=3)["songs"]
    server.clock.now += WORKER_LEASE_SECONDS - 5
    beat = server.call("POST", "/api/worker/heartbeat", first, json={"device": "RTX (CUDA GPU)"})
    assert beat.json()["renewed"] == 3
    assert beat.json()["identity"] == server.identity()
    server.clock.now += 10
    assert claim(server, second, batch=3)["songs"] == []
    released = server.call(
        "POST", "/api/worker/release", first, json={"leases": [songs[0]["lease"]]}
    )
    assert released.json() == {"released": 1}
    assert len(claim(server, second, batch=3)["songs"]) == 1


def test_rate_and_size_limits(server, monkeypatch):
    _token, secret = server.token()
    huge = server.call(
        "POST",
        "/api/worker/result",
        secret,
        content=b"{" + b" " * (70 * 1024) + b"}",
        headers={"Content-Type": "application/json"},
    )
    assert huge.status_code == 413
    monkeypatch.setattr(RateLimiter, "retry_after", lambda self, key: 7)
    limited = server.call("POST", "/api/worker/claim", secret, json={})
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "7"


def test_rate_limiter_counts_a_sliding_window():
    now = [0.0]
    limiter = RateLimiter(2, 60.0, monotonic=lambda: now[0])
    assert limiter.retry_after("a") is None
    assert limiter.retry_after("a") is None
    assert limiter.retry_after("a") == 60
    assert limiter.retry_after("b") is None
    now[0] = 60.5
    assert limiter.retry_after("a") is None


def test_decode_vector_round_trips_exactly():
    vector = np.random.default_rng(3).normal(size=FEATURE_DIM).astype(np.float32)
    vector /= np.linalg.norm(vector)
    np.testing.assert_array_equal(decode_vector(_vector_text(vector)), vector)


def test_a_server_without_a_library_says_so(tmp_path):
    made = Server(tmp_path, library=False)
    _token, secret = made.token()
    assert made.call("POST", "/api/worker/claim", secret, json={}).status_code == 503
    assert made.client.get("/api/analysis/workers", headers={"Host": "127.0.0.1"}).json() == {
        "available": False,
        "workers": [],
        "waiting": 0,
        "analysing": 0,
    }


# ---------------------------------------------------------------------------
# Settings routes
# ---------------------------------------------------------------------------


def test_settings_create_list_and_revoke_tokens(server):
    created = server.client.post(
        "/api/machine-tokens",
        json={"name": "Studio PC", "scopes": [SCOPE_ANALYSIS_WORKER]},
        headers={"Origin": "http://127.0.0.1:8080"},
    )
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    secret = created.json()["secret"]
    token_id = created.json()["token"]["token_id"]
    listing = server.client.get("/api/machine-tokens").json()
    assert listing["scopes"][0] == {
        "id": SCOPE_ANALYSIS_WORKER,
        "label": "Analysis worker",
        "station": False,
    }
    assert {"id": SCOPE_ANNOUNCER, "station": True}.items() <= listing["scopes"][1].items()
    assert [s["slug"] for s in listing["stations"]] == ["default"]
    assert [t["name"] for t in listing["tokens"]] == ["Studio PC"]
    assert listing["tokens"][0]["station"] is None
    assert secret not in str(listing)
    claim(server, secret, batch=2)
    origin = {"Origin": "http://127.0.0.1:8080"}
    revoked = server.client.delete(f"/api/machine-tokens/{token_id}", headers=origin)
    assert revoked.json() == {"revoked": token_id, "name": "Studio PC"}
    assert server.manager.analysis_queue.held_paths() == set()
    assert server.manager.analysis_queue.workers() == []
    gone = server.client.delete(f"/api/machine-tokens/{token_id}", headers=origin)
    assert gone.status_code == 404
    bad = server.client.post(
        "/api/machine-tokens", json={"name": " ", "scopes": [SCOPE_ANALYSIS_WORKER]}, headers=origin
    )
    assert bad.status_code == 422
    unknown = server.client.post(
        "/api/machine-tokens", json={"name": "x", "scopes": ["admin"]}, headers=origin
    )
    assert unknown.status_code == 422


def test_settings_create_an_announcer_token_bound_to_a_station(server):
    origin = {"Origin": "http://127.0.0.1:8080"}

    def create(**body):
        return server.client.post(
            "/api/machine-tokens", json={"name": "news-bot", **body}, headers=origin
        )

    made = create(scopes=[SCOPE_ANNOUNCER], station="default")
    assert made.status_code == 201, made.text
    assert made.json()["token"]["station"] == "default"
    # An announcer token needs a station that exists; a worker token takes none.
    assert create(scopes=[SCOPE_ANNOUNCER]).status_code == 422
    assert create(scopes=[SCOPE_ANNOUNCER], station="nightowl").status_code == 422
    assert create(scopes=[SCOPE_ANALYSIS_WORKER], station="default").status_code == 422
    # It cannot use the worker routes.
    claimed = server.call("POST", "/api/worker/claim", made.json()["secret"], json={})
    assert claimed.status_code == 401


def test_settings_show_workers_and_the_queue(server):
    _token, secret = server.token()
    server.call("POST", "/api/worker/heartbeat", secret, json={"device": "RTX 3080 (CUDA GPU)"})
    song = claim(server, secret)["songs"][0]
    view = server.client.get("/api/analysis/workers").json()
    assert view["available"] is True
    assert view["waiting"] == 3
    assert view["analysing"] == 1
    assert view["server_paused"] is True
    (worker,) = view["workers"]
    assert worker["name"] == "Studio PC"
    assert worker["device"] == "RTX 3080 (CUDA GPU)"
    assert worker["connected"] is True
    assert worker["leased"] == 1
    body, _ = server.result(song, song["lease"])
    server.call("POST", "/api/worker/result", secret, json=body)
    server.clock.now += WORKER_ACTIVE_SECONDS + 1
    later = server.client.get("/api/analysis/workers").json()
    assert later["waiting"] == 2
    assert later["server_paused"] is False
    assert later["workers"][0]["songs_done"] == 1


def test_store_analysis_checks_the_record_inside_its_transaction(server):
    catalog = server.manager.catalog
    song = server.songs[0]
    record = catalog.record_for_path(song)
    vector = np.zeros(FEATURE_DIM, dtype=np.float32)
    vector[0] = 1
    fields = {"energy": 0.1, "key": 1, "mode": 1, "bpm": 99.0, "tempo_confidence": 0.5}
    with pytest.raises(ValueError):
        catalog.store_analysis(song, record["size"], record["fingerprint"], fields, vector[:3])
    # The file matches, but the record now names other bytes (a scan is behind).
    with catalog._lock:
        catalog._db().execute(
            "UPDATE catalog_entries SET fingerprint=? WHERE path=?", ("0" * 64, record["path"])
        )
        catalog._db().commit()
    assert not catalog.store_analysis(song, record["size"], record["fingerprint"], fields, vector)
    assert not catalog.store_analysis(song, 0, "", fields, vector)
    assert not catalog.has_vector(song)
    # A failure inside the transaction leaves nothing half written.
    with catalog._lock:
        catalog._db().execute(
            "UPDATE catalog_entries SET fingerprint=? WHERE path=?",
            (record["fingerprint"], record["path"]),
        )
        catalog._db().commit()
    with pytest.raises(KeyError):
        catalog.store_analysis(song, record["size"], record["fingerprint"], {}, vector)
    assert not catalog.has_vector(song)
    with catalog._lock:
        catalog._db().execute(
            "UPDATE catalog_entries SET mtime_ns=0 WHERE path=?", (record["path"],)
        )
        catalog._db().commit()
    assert catalog.store_analysis(song, record["size"], record["fingerprint"], fields, vector)
    assert catalog.record_for_path(song)["key"] == 1
