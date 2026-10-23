"""The remote analysis worker API: token rules, leases, result checks, Settings routes."""

import base64
import json
import logging
import os
import secrets
import sys
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from fastapi.testclient import TestClient

from autodj.analysis_queue import WORKER_ACTIVE_SECONDS, WORKER_LEASE_SECONDS
from autodj.catalog import _path
from autodj.config import ServerConfig
from autodj.indexer import ANALYSIS_VERSION, FEATURE_DIM, analysis_identity
from autodj.library import LibraryManager
from autodj.machine_tokens import SCOPE_ANALYSIS_WORKER, SCOPE_ANNOUNCER
from autodj.worker_api import RESULT_BODY_MAX_BYTES, RateLimiter, decode_vector
from tests._server import create_app
from tests.unit._catalog_vectors import vectors_snapshot
from tests.unit.test_library import Similarity, make_config, make_entry

HTTPS = "https://127.0.0.1:8080"
COMMIT = "c" * 40
SCAN_CAPABILITY = {"scan_decode_v1": True, "scan_analysis_version": "1"}


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


def test_heartbeat_renews_only_reported_leases_and_connect_releases_orphans(server):
    """ADR-0008-14. A restart cannot keep leases the process never received alive."""
    token, secret = server.token()
    other, other_secret = server.token("Other")
    songs = claim(server, secret, batch=2)["songs"]
    other_song = claim(server, other_secret)["songs"][0]
    queue = server.manager.analysis_queue
    expires = queue.lease(songs[1]["lease"], token.token_id).expires_at
    server.clock.now += 30
    beat = server.call(
        "POST",
        "/api/worker/heartbeat",
        secret,
        json={
            "leases": [songs[0]["lease"], other_song["lease"]],
        },
    )
    assert beat.status_code == 200, beat.text
    assert beat.json()["renewed"] == 1
    assert queue.lease(songs[0]["lease"], token.token_id).expires_at > expires
    assert queue.lease(songs[1]["lease"], token.token_id).expires_at == expires
    assert (
        server.call("POST", "/api/worker/heartbeat", secret, json={"leases": []}).json()["renewed"]
        == 0
    )
    # Failed holds and another token's claim survive the process restart.
    queue.finish(queue.lease(songs[0]["lease"], token.token_id), "failed")
    restarted = server.call(
        "POST",
        "/api/worker/heartbeat",
        secret,
        json={
            "leases": [],
            "connect": True,
        },
    )
    assert restarted.status_code == 200, restarted.text
    assert queue.lease(songs[1]["lease"], token.token_id) is None
    assert queue.lease(other_song["lease"], other.token_id) is not None
    assert queue._read(
        "SELECT state FROM analysis_leases WHERE lease_id=?", (songs[0]["lease"],)
    ) == [("failed",)]
    assert claim(server, secret)["songs"][0]["title"] == songs[1]["title"]


def test_routine_worker_polls_are_audited_at_debug_and_real_work_at_info(server, caplog):
    """A heartbeat or an empty claim every 30 seconds must not fill the log at INFO."""
    _, secret = server.token()

    def audit_levels():
        levels = [
            (json.loads(record.message)["route"], record.levelno)
            for record in caplog.records
            if record.name == "autodj.audit"
        ]
        caplog.clear()
        return levels

    with caplog.at_level(logging.DEBUG, logger="autodj.audit"):
        connect = {"leases": [], "connect": True}
        assert server.call("POST", "/api/worker/heartbeat", secret, json=connect).status_code == 200
        assert audit_levels() == [("/api/worker/heartbeat", logging.INFO)]
        songs = claim(server, secret, batch=3)["songs"]
        assert len(songs) == 3
        assert audit_levels() == [("/api/worker/claim", logging.INFO)]
        assert claim(server, secret)["songs"] == []
        assert audit_levels() == [("/api/worker/claim", logging.DEBUG)]
        beat = {"leases": [song["lease"] for song in songs]}
        assert server.call("POST", "/api/worker/heartbeat", secret, json=beat).status_code == 200
        assert audit_levels() == [("/api/worker/heartbeat", logging.DEBUG)]
        release = {"leases": [songs[0]["lease"]]}
        assert server.call("POST", "/api/worker/release", secret, json=release).status_code == 200
        assert audit_levels() == [("/api/worker/release", logging.INFO)]
        refused = server.call("POST", "/api/worker/heartbeat", "wrong", json=beat)
        assert refused.status_code == 401
        assert audit_levels() == [("/api/worker/heartbeat", logging.WARNING)]
        invalid = server.call("POST", "/api/worker/heartbeat", secret, json={"leases": "x"})
        assert invalid.status_code == 422
        assert audit_levels() == [("/api/worker/heartbeat", logging.WARNING)]


def test_scan_mismatch_logs_once_per_token_across_version_strings(server, caplog):
    """ADR-0008-15. Arbitrary versions cannot grow a token's warning history."""
    _token, secret = server.token()
    for version in range(20):
        response = server.call(
            "POST",
            "/api/worker/heartbeat",
            secret,
            json={
                "scan_decode_v1": True,
                "scan_analysis_version": f"unsupported-{version}",
            },
        )
        assert response.status_code == 200
    assert caplog.text.count("incompatible scan analysis version") == 1


def test_scan_mismatch_warning_history_has_a_token_bound(server, monkeypatch, caplog):
    """ADR-0008-15. Warning history evicts the oldest token at its fixed capacity."""
    monkeypatch.setattr("autodj.worker_api._MAX_SCAN_MISMATCHES", 2)
    secrets = [server.token(f"Worker {i}")[1] for i in range(3)]
    for secret in [*secrets, secrets[2], secrets[0]]:
        response = server.call(
            "POST",
            "/api/worker/heartbeat",
            secret,
            json={
                "scan_decode_v1": True,
                "scan_analysis_version": "unsupported",
            },
        )
        assert response.status_code == 200
    assert caplog.text.count("incompatible scan analysis version") == 4


def scan_result():
    return {
        "length": 180.0,
        "energy": 0.25,
        "points": {"start": 0.1, "end": 179.0, "intro": 10.0, "outro": 165.0, "segue": 170.0},
        "intro_lift_db": 2.0,
        "loudness": None,
        "dj": {
            "intro_start_s": 0.1,
            "intro_end_s": 10.0,
            "outro_start_s": 165.0,
            "beats": [0.5, 1.0, 1.5],
            "analysed": True,
            "cues": [
                {
                    "time_s": 1.0,
                    "type": "first_downbeat",
                    "label": "",
                    "source": "auto",
                    "color": "",
                }
            ],
        },
    }


def test_claim_hands_identity_mismatch_to_local_scan(server):
    """ADR-0008-16. A skipped stale catalog identity cannot wait behind a busy worker."""
    _token, secret = server.token()
    queue = server.manager.analysis_queue
    record = server.manager.catalog.record_for_path(server.songs[0])
    queue.defer_scan(record["path"], record["size"], record["fingerprint"])
    server.songs[0].write_bytes(b"changed bytes")
    reply = server.call("POST", "/api/worker/claim", secret, json={**SCAN_CAPABILITY, "batch": 2})
    assert reply.status_code == 200, reply.text
    assert len(reply.json()["songs"]) == 2
    assert queue.abandoned_scans(worker_active=True) == [record["path"]]
    assert queue._read("SELECT deadline FROM scan_pending WHERE path=?", (record["path"],)) == [
        (0,)
    ]


def test_claim_hands_off_expired_pending_without_catalog_record(server):
    """ADR-0008-21. Only the scan owner can cancel an expired orphaned row."""
    _token, secret = server.token()
    manager = server.manager
    queue = manager.analysis_queue
    orphan = manager.cfg.library.music_dir / "aaa-orphan.flac"
    orphan.write_bytes(b"orphan")
    queue.defer_scan(_path(orphan), 6, "orphan")
    server.clock.now += 600
    response = server.call(
        "POST", "/api/worker/claim", secret, json={**SCAN_CAPABILITY, "batch": 1}
    )
    assert response.status_code == 200, response.text
    assert queue.scan_pending(_path(orphan))
    assert queue.abandoned_scans(worker_active=True) == [_path(orphan)]
    assert len(response.json()["songs"]) == 1
    with manager._scan_lock:
        assert manager._recover_scan_one(True)
    assert not queue.scan_pending(_path(orphan))


@pytest.mark.parametrize("expired", [False, True])
def test_claim_during_new_scan_tag_read_preserves_work(server, monkeypatch, expired):
    """ADR-0008-21. A claim between deferral and tag publication cannot strand a song."""
    from autodj.audio_meta import FileTags
    from tests.unit.test_library import fake_basic

    token, secret = server.token()
    manager = server.manager
    queue = manager.analysis_queue
    queue.seen(token.token_id, token.name, scan_decode=True)
    song = manager.cfg.library.music_dir / "aaa-new.flac"
    song.write_bytes(b"new song")
    monkeypatch.setattr(manager, "_discover", lambda sources: [("music", song)])
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: False)
    monkeypatch.setattr("autodj.library.analyze_basic", fake_basic)
    monkeypatch.setattr("autodj.intro_lift.measure_source_intro", lambda *args: 1.0)

    def read_tags(path):
        assert path == song
        assert manager.catalog.record_for_path(path) is None
        if expired:
            server.clock.now += 600
        response = server.call(
            "POST", "/api/worker/claim", secret, json={**SCAN_CAPABILITY, "batch": 1}
        )
        assert response.status_code == 200, response.text
        return FileTags(length=180.0)

    monkeypatch.setattr("autodj.audio_meta.read_file_tags", read_tags)
    manager.scan()
    record = manager.catalog.record_for_path(song)
    if expired:
        assert record["energy"] > 0
        assert not queue.scan_pending(_path(song))
    else:
        assert queue.scan_created_listing(_path(song), record["size"], record["fingerprint"])
        assert record["energy"] == 0
        response = server.call(
            "POST", "/api/worker/claim", secret, json={**SCAN_CAPABILITY, "batch": 1}
        )
        assert response.status_code == 200, response.text
        lease = queue.lease(response.json()["songs"][0]["lease"], token.token_id)
        assert lease.path == _path(song)


@pytest.mark.parametrize("unreadable", [False, True])
def test_claim_backs_off_dead_scans_and_reaches_live_song(server, monkeypatch, unreadable):
    """ADR-0008-21. Missing or unreadable rows cannot occupy every remote batch forever."""
    _token, secret = server.token()
    manager = server.manager
    queue = manager.analysis_queue
    dead = set()
    for n in range(20):
        path = manager.cfg.library.music_dir / f"dead-{n:02}.flac"
        path.write_bytes(b"unavailable audio")
        manager.catalog.put_entry(make_entry(path))
        record = manager.catalog.record_for_path(path)
        queue.defer_scan(record["path"], record["size"], record["fingerprint"])
        dead.add(record["path"])
        if not unreadable:
            path.unlink()
    if unreadable:
        original = Path.open

        def locked(path, *args, **kwargs):
            if _path(path) in dead:
                raise OSError("file locked")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", locked)
    for song in server.songs:
        record = manager.catalog.record_for_path(song)
        queue.defer_scan(record["path"], record["size"], record["fingerprint"])
    monkeypatch.setattr(
        queue, "pending_scans", Mock(side_effect=AssertionError("unbounded pending read"))
    )
    received = []
    for _ in range(3):
        response = server.call(
            "POST", "/api/worker/claim", secret, json={**SCAN_CAPABILITY, "batch": 16}
        )
        assert response.status_code == 200, response.text
        received.extend(response.json()["songs"])
        if received:
            break
    assert {song["title"] for song in received} == {song.stem for song in server.songs}
    for path in dead:
        assert manager.catalog.record_for_path(path) is not None
        assert (
            queue._read("SELECT retry_at FROM scan_pending WHERE path=?", (path,))[0][0]
            > server.clock.now
        )


@pytest.mark.parametrize("state", ["local", "backoff", "exhausted", "decoding", "leased"])
def test_claim_filters_ineligible_scans_before_file_work(server, monkeypatch, state):
    """ADR-0008-17. Ineligible scan rows cause no per-file reads or refresh writes."""
    from autodj.analysis_queue import SCAN_MAX_ATTEMPTS

    _token, secret = server.token()
    manager = server.manager
    queue = manager.analysis_queue
    for song in server.songs:
        record = manager.catalog.record_for_path(song)
        queue.defer_scan(record["path"], record["size"], record["fingerprint"])
    for song in server.songs[:2]:
        record = manager.catalog.record_for_path(song)
        if state == "leased":
            queue.claim("other", [(record["path"], record["size"], record["fingerprint"])], 600)
        elif state == "decoding":
            assert queue.claim_scan(record["path"], record["size"], record["fingerprint"])
        else:
            column, value = {
                "local": ("deadline", 0),
                "backoff": ("retry_at", server.clock.now + 3600),
                "exhausted": ("attempts", SCAN_MAX_ATTEMPTS),
            }[state]
            with queue._write() as db:
                db.execute(
                    f"UPDATE scan_pending SET {column}=? WHERE path=?", (value, record["path"])
                )
    from autodj.indexer import file_fingerprint

    identity = Mock(wraps=file_fingerprint)
    refresh = Mock(wraps=queue.refresh_scan)
    monkeypatch.setattr("autodj.worker_api.file_fingerprint", identity)
    monkeypatch.setattr(queue, "refresh_scan", refresh)
    response = server.call("POST", "/api/worker/claim", secret, json=SCAN_CAPABILITY)
    assert response.status_code == 200, response.text
    assert [song["title"] for song in response.json()["songs"]] == [server.songs[2].stem]
    assert identity.call_count == 1
    refresh.assert_not_called()


def claim_scan(server, secret):
    song = server.songs[0]
    record = server.manager.catalog.record_for_path(song)
    server.manager.analysis_queue.defer_scan(record["path"], record["size"], record["fingerprint"])
    reply = server.call("POST", "/api/worker/claim", secret, json=SCAN_CAPABILITY)
    assert reply.status_code == 200, reply.text
    return reply.json()["songs"][0]


def test_worker_combined_result_stores_scan_fields(server):
    """ADR-0008-06. A combined result completes scan work and makes music playable."""
    _token, secret = server.token()
    song = claim_scan(server, secret)
    assert song["scan"]["music_fade_drop_db"] == 10
    body, _vector = server.result(song, song["lease"], scan=scan_result())
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 200, reply.text
    record = server.manager.catalog.record_for_path(server.songs[0])
    assert record["energy"] > 0
    assert record["points"] == scan_result()["points"]
    assert record["intro_lift_db"] == 2.0
    assert server.manager._dj_cache().get(str(server.songs[0])).beats == [0.5, 1.0, 1.5]
    assert not server.manager.analysis_queue.pending_scans()


@pytest.mark.parametrize("retag_pending", [False, True])
@pytest.mark.parametrize("with_vector", [False, True])
def test_changed_playable_song_accepts_only_pending_bytes(server, with_vector, retag_pending):
    """ADR-0008-16. Claims and results use pending identity until atomic replacement."""
    import soundfile as sf
    from mutagen.flac import FLAC

    from autodj.indexer import file_fingerprint
    from tests.unit._catalog_vectors import set_vector

    manager = server.manager
    path = server.songs[0]
    entry = make_entry(path)
    entry.length = manager.advanced_max_minutes() * 60 + 1
    manager.catalog.put_entry(entry)
    manager.catalog.retag(path, {"bpm": 133.0, "key": 7, "mode": 1})
    manager.catalog.set_points(path, {"start": 0.0, "end": 178.0})
    set_vector(manager.catalog, path, np.ones(FEATURE_DIM, dtype=np.float32))
    before = manager.catalog.record_for_path(path)
    queue = manager.analysis_queue
    _token, secret = server.token()
    sf.write(path, np.ones((22050, 2)) * 0.1, 22050)
    tags = FLAC(path)
    for name, value in {
        "title": "Replacement",
        "artist": "New artist",
        "album": "New album",
        "genre": "Rock",
        "date": "2026",
        "bpm": "131",
    }.items():
        tags[name] = value
    tags.save()
    queue.defer_scan(_path(path), *file_fingerprint(path))
    if retag_pending:
        tags["title"] = "Edited replacement"
        tags.save()
        assert manager.catalog.retag(path, {"title": "Edited replacement"})
        before["title"] = "Edited replacement"
        assert queue.scan_identity(_path(path)) == file_fingerprint(path)
    reply = server.call("POST", "/api/worker/claim", secret, json=SCAN_CAPABILITY)
    assert reply.status_code == 200, reply.text
    song = reply.json()["songs"][0]
    assert "scan" in song
    assert (song["size"], song["fingerprint"]) == file_fingerprint(path)
    assert song["embed"] is True
    assert manager.catalog.record_for_path(path) == before
    measurements = scan_result()
    measurements.update(length=3.0, dj=None)
    measurements["points"] = {"start": 0.0, "end": 3.0, "intro": 0.5, "outro": 2.0, "segue": 2.5}
    body, vector = server.result(song, song["lease"], scan=measurements)
    if not with_vector:
        body["vector"] = None
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 200, reply.text
    after = manager.catalog.record_for_path(path)
    assert (after["size"], after["fingerprint"]) == file_fingerprint(path)
    assert after["energy"] == (body["energy"] if with_vector else measurements["energy"])
    assert after["length"] == 3.0
    assert after["title"] == ("Edited replacement" if retag_pending else "Replacement")
    assert after["artist"] == "New artist"
    assert after["album"] == "New album"
    assert after["genre"] == "Rock"
    assert after["year"] == 2026
    assert after["bpm"] == 131.0
    assert (after["key"], after["mode"]) == ((4, 0) if with_vector else (-1, -1))
    assert after["tempo_confidence"] == (0.8 if with_vector else 0.0)
    assert after["user_tags"] == []
    assert after["points"] == measurements["points"]
    if with_vector:
        np.testing.assert_array_equal(vectors_snapshot(manager.catalog)[1][0], vector)
    else:
        assert not vectors_snapshot(manager.catalog)[0]
    assert not queue.scan_pending(_path(path))


@pytest.mark.parametrize("stale", ["body", "disk", "pending", "second_scan", "during_store"])
def test_changed_pending_scan_rejects_stale_result_and_recovers(server, monkeypatch, stale):
    from autodj.indexer import file_fingerprint
    from tests.unit.test_library import fake_basic

    manager = server.manager
    path = server.songs[0]
    before = manager.catalog.record_for_path(path)
    queue = manager.analysis_queue
    _token, secret = server.token()
    path.write_bytes(b"first rewrite")
    queue.defer_scan(_path(path), *file_fingerprint(path))
    song = server.call("POST", "/api/worker/claim", secret, json=SCAN_CAPABILITY).json()["songs"][0]
    body, _ = server.result(song, song["lease"], scan=scan_result())
    if stale == "body":
        body.update(size=before["size"], fingerprint=before["fingerprint"])
    elif stale in {"disk", "second_scan"}:
        path.write_bytes(b"second rewrite during the lease")
        if stale == "second_scan":
            queue.refresh_scan(_path(path), *file_fingerprint(path))
    elif stale == "pending":
        queue.refresh_scan(_path(path), song["size"] + 1, "a" * 64)
    else:
        from autodj.audio_meta import FileTags

        def read_tags(path):
            Path(path).write_bytes(b"rewrite during result storage")
            return FileTags(title="Changed while storing")

        monkeypatch.setattr("autodj.audio_meta.read_file_tags", read_tags)
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 409, reply.text
    assert manager.catalog.record_for_path(path) == before
    assert queue.scan_pending(_path(path))
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: False)
    monkeypatch.setattr("autodj.library.analyze_basic", fake_basic)
    monkeypatch.setattr("autodj.intro_lift.measure_source_intro", lambda *args: 1.0)
    with manager._scan_lock:
        assert manager._recover_scan_one(False)
    after = manager.catalog.record_for_path(path)
    assert (after["size"], after["fingerprint"]) == file_fingerprint(path)
    assert after["energy"] > 0
    assert not queue.scan_pending(_path(path))


def test_worker_scan_only_preserves_advanced_metadata_and_vector(server):
    """ADR-0008-18. Scan-only work changes measured fields, preserving advanced analysis."""
    _token, secret = server.token()
    song = claim(server, secret)["songs"][0]
    body, vector = server.result(song, song["lease"])
    assert server.call("POST", "/api/worker/result", secret, json=body).status_code == 200
    before = server.manager.catalog.record_for_path(server.songs[0])
    song = claim_scan(server, secret)
    assert song["embed"] is False
    body, _unused = server.result(
        song,
        song["lease"],
        vector=None,
        scan=scan_result(),
        key=-1,
        mode=-1,
        bpm=0.0,
        tempo_confidence=0.0,
    )
    response = server.call("POST", "/api/worker/result", secret, json=body)
    assert response.status_code == 200, response.text
    after = server.manager.catalog.record_for_path(server.songs[0])
    for field in ("key", "mode", "bpm", "tempo_confidence", "embedded_at"):
        assert after[field] == before[field]
    assert after["energy"] == scan_result()["energy"]
    assert after["points"] == scan_result()["points"]
    _entries, vectors = vectors_snapshot(server.manager.catalog)
    np.testing.assert_array_equal(vectors[0], vector)


@pytest.mark.parametrize("capability", [{}, SCAN_CAPABILITY])
def test_advanced_lease_before_scan_deferral_stores_vector_and_retains_scan(server, capability):
    """ADR-0008-13. A scan deferral cannot invalidate an existing vector-only lease."""
    server.manager.catalog.put_entry(make_entry(server.songs[0], energy=0.0))
    token, secret = server.token()
    reply = server.call("POST", "/api/worker/claim", secret, json=capability)
    assert reply.status_code == 200, reply.text
    song = reply.json()["songs"][0]
    assert "scan" not in song
    queue = server.manager.analysis_queue
    record = server.manager.catalog.record_for_path(server.songs[0])
    queue.defer_scan(record["path"], record["size"], record["fingerprint"])
    body, vector = server.result(song, song["lease"])
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 200, reply.text
    assert queue.lease(song["lease"], token.token_id) is None
    assert queue.scan_pending(record["path"])
    assert record["path"] not in server.manager.catalog.unembedded_paths()
    entries, stored_vectors = vectors_snapshot(server.manager.catalog)
    paths = [entry.path for entry in entries]
    np.testing.assert_allclose(stored_vectors[paths.index(record["path"])], vector)


@pytest.mark.parametrize("version", ["", "future"])
def test_scan_version_mismatch_only_claims_advanced_and_logs_once(server, caplog, version):
    """ADR-0008-13. Algorithm mismatch disables scan work and logs once per worker/version."""
    _token, secret = server.token()
    record = server.manager.catalog.record_for_path(server.songs[0])
    queue = server.manager.analysis_queue
    queue.defer_scan(record["path"], record["size"], record["fingerprint"])
    capability = {"scan_decode_v1": True, "scan_analysis_version": version}
    with caplog.at_level(logging.WARNING, logger="autodj.worker_api"):
        for _ in range(2):
            heartbeat = server.call("POST", "/api/worker/heartbeat", secret, json=capability)
            assert heartbeat.status_code == 200, heartbeat.text
            assert heartbeat.json()["scan_analysis_version"] == "1"
        reply = server.call("POST", "/api/worker/claim", secret, json={**capability, "batch": 16})
    assert reply.status_code == 200, reply.text
    assert reply.json()["scan_analysis_version"] == "1"
    assert len(reply.json()["songs"]) == 2
    assert not queue.worker_active(scan_decode=True)
    assert sum("scan analysis version" in record.message for record in caplog.records) == 1


@pytest.mark.parametrize("length", [86400.1, 1e100, 190.0])
def test_worker_rejects_implausible_scan_duration_without_catalog_changes(server, length):
    """ADR-0008-13. Scan duration is bounded independently and against measured length."""
    if length > 86400:
        server.manager.catalog.put_entry(replace(make_entry(server.songs[0]), length=0.0))
    token, secret = server.token()
    song = claim_scan(server, secret)
    before = server.manager.catalog.record_for_path(server.songs[0])
    scan = {**scan_result(), "length": length}
    body, _ = server.result(song, song["lease"], scan=scan)
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 422, reply.text
    assert server.manager.catalog.record_for_path(server.songs[0]) == before
    queue = server.manager.analysis_queue
    assert queue.lease(song["lease"], token.token_id) is None
    assert queue.claimable_scans(limit=16) == []


@pytest.mark.parametrize("new_listing", [False, True])
def test_worker_accepts_tag_only_scan_with_inaccurate_duration(server, new_listing):
    """ADR-0008-13. Tag estimates allow a loose tolerance for decoded measurements."""
    path = server.songs[0]
    catalog = server.manager.catalog
    catalog.put_entry(replace(make_entry(path), energy=0.0, length=90.0))
    record = catalog.record_for_path(path)
    queue = server.manager.analysis_queue
    queue.defer_scan(record["path"], record["size"], record["fingerprint"], new_listing=new_listing)
    _token, secret = server.token()
    song = claim_scan(server, secret)
    body, _ = server.result(song, song["lease"], scan=scan_result())
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 200, reply.text
    after = catalog.record_for_path(path)
    assert after["length"] == 180.0 and after["energy"] > 0
    assert after["points"] == scan_result()["points"]
    assert not queue.scan_pending(record["path"])


@pytest.mark.parametrize("listing", ["new", "tag_replacement", "playable_replacement"])
@pytest.mark.parametrize(
    "tag_length, length, accepted",
    [
        (90.0, 42.0, True),
        (90.0, 183.0, True),
        (90.0, 41.9, False),
        (90.0, 183.1, False),
        (2.0, 6.0, True),
        (2.0, 7.1, False),
        (0.0, 3600.0, True),
        (0.0, 86400.1, False),
    ],
)
def test_worker_scan_tag_duration_bounds(
    server, monkeypatch, listing, tag_length, length, accepted
):
    """ADR-0008-13. Known tag estimates bound results; unknown estimates use the absolute limit."""
    from autodj.audio_meta import FileTags
    from autodj.indexer import file_fingerprint

    catalog = server.manager.catalog
    queue = server.manager.analysis_queue
    path = server.songs[0]
    if listing == "playable_replacement":
        path.write_bytes(b"replacement bytes")
        monkeypatch.setattr(
            "autodj.audio_meta.read_file_tags", lambda _: FileTags(length=tag_length)
        )
    else:
        catalog.put_entry(replace(make_entry(path), energy=0.0, length=tag_length))
    before = catalog.record_for_path(path)
    queue.defer_scan(_path(path), *file_fingerprint(path), new_listing=listing == "new")
    token, secret = server.token()
    reply = server.call("POST", "/api/worker/claim", secret, json=SCAN_CAPABILITY)
    song = reply.json()["songs"][0]
    scan = {**scan_result(), "length": length, "dj": None}
    body, _ = server.result(song, song["lease"], scan=scan)
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == (200 if accepted else 422), reply.text
    assert queue.lease(song["lease"], token.token_id) is None
    if accepted:
        assert catalog.record_for_path(path)["length"] == length
        assert not queue.scan_pending(_path(path))
    else:
        assert catalog.record_for_path(path) == before
        assert queue.claimable_scans(limit=16) == []
        assert queue.abandoned_scans(worker_active=True) == [_path(path)]


def test_vector_only_identity_rejection_has_no_failed_hold(server):
    """ADR-0008-13. Identity rejection leaves vector work available after worker release."""
    token, secret = server.token()
    song = claim(server, secret)["songs"][0]
    body, _ = server.result(song, song["lease"], model_commit="wrong")
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 409, reply.text
    released = server.call("POST", "/api/worker/release", secret, json={"leases": [song["lease"]]})
    assert released.status_code == 200, released.text
    queue = server.manager.analysis_queue
    assert queue.lease(song["lease"], token.token_id) is None
    path = _path(server.songs[0])
    assert path not in queue.held_paths()
    assert queue.claim("other-worker", [(path, song["size"], song["fingerprint"])], 600)


@pytest.mark.parametrize("rejection", ["model", "vector", "missing_dj", "malformed"])
def test_worker_rejected_result_fails_only_owned_lease(server, monkeypatch, rejection):
    """ADR-0008-13. Rejection hands scans to recovery without releasing another holder."""
    token, secret = server.token()
    other, other_secret = server.token("Other")
    song = claim_scan(server, secret)
    body, _ = server.result(song, song["lease"], scan=scan_result())
    if rejection == "model":
        body["model_commit"] = "wrong"
    elif rejection == "vector":
        body["vector"] = "invalid"
    elif rejection == "missing_dj":
        monkeypatch.setattr(server.manager, "dj_analysis_enabled", lambda: True)
        body["scan"]["dj"] = None
    else:
        body["scan"]["energy"] = True
    queue = server.manager.analysis_queue
    before = server.manager.catalog.record_for_path(server.songs[0])
    foreign = server.call("POST", "/api/worker/result", other_secret, json=body)
    assert foreign.status_code in {404, 409, 422}
    assert queue.lease(song["lease"], token.token_id) is not None
    assert queue.lease(song["lease"], other.token_id) is None
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == (409 if rejection == "model" else 422), reply.text
    assert server.manager.catalog.record_for_path(server.songs[0]) == before
    assert queue.lease(song["lease"], token.token_id) is None
    assert queue.claimable_scans(limit=16) == []
    assert queue.abandoned_scans(worker_active=True) == [before["path"]]


@pytest.mark.parametrize("intro_start, intro_end", [(0.1, 170.0), (11.0, 10.0)])
def test_worker_accepts_intro_after_outro_as_local_scan_does(server, intro_start, intro_end):
    _token, secret = server.token()
    song = claim_scan(server, secret)
    scan = scan_result()
    scan["points"].update(intro=170.0, outro=160.0)
    scan["dj"].update(intro_start_s=intro_start, intro_end_s=intro_end, outro_start_s=160.0)
    body, _ = server.result(song, song["lease"], scan=scan)
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 200, reply.text
    assert server.manager.catalog.points(server.songs[0])["intro"] == 170.0


@pytest.mark.parametrize(
    "points, expected_end",
    [
        ({"start": 0.0, "end": 181.0}, 180.0),
        ({"start": 10.0, "end": 9.0}, 180.0),
        ({"start": 0.0, "end": 179.0, "intro": -1.0}, 179.0),
        ({"start": 0.0, "end": 179.0, "intro": 180.0}, 179.0),
        ({"start": None, "end": None}, 180.0),
        ({}, 180.0),
    ],
)
def test_worker_normalizes_basic_points_like_local_scan(server, points, expected_end):
    """ADR-0008-13. Local and remote scans safely normalize imperfect automatic cues."""
    _token, secret = server.token()
    song = claim_scan(server, secret)
    scan = {**scan_result(), "points": points}
    expected = {"start": 0.0, "end": expected_end, "intro": None, "outro": None, "segue": None}
    assert LibraryManager._valid_basic_points(points, make_entry(server.songs[0])) == expected
    body, _ = server.result(song, song["lease"], scan=scan)
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 200, reply.text
    assert server.manager.catalog.record_for_path(server.songs[0])["points"] == expected


@pytest.mark.parametrize("amplitude", [0.2, 0.0001, 0.0], ids=["audible", "quiet", "silent"])
@pytest.mark.dj_analysis
def test_real_scan_analysis_round_trips_dj_and_optional_loudness(server, amplitude):
    """ADR-0008-13. Real audio, including silence, completes enabled DJ/loudness scan work."""
    from autodj.scan_analysis import analyse_scan

    path = server.cfg.library.music_dir / "generated.wav"
    sample_rate = 48000
    time = np.arange(sample_rate * 3) / sample_rate
    audio = (amplitude * np.sin(2 * np.pi * 440 * time) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(audio.tobytes())
    server.manager.catalog.put_entry(replace(make_entry(path), length=3.0))
    server.songs.insert(0, path)
    server.cfg.playback.loudness_processing = "smooth"
    server.manager.catalog.set_setting("dj_analysis", True)
    _token, secret = server.token()
    song = claim_scan(server, secret)
    assert song["scan"]["dj"] and song["scan"]["loudness"]
    measured = analyse_scan(path, song["scan"])
    assert measured["dj"]["analysed"] is True
    assert (measured["loudness"] is not None) == (amplitude == 0.2)
    body, _ = server.result(song, song["lease"], scan=measured)
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 200, reply.text
    stored = server.manager.catalog.record_for_path(path)
    assert stored["length"] == pytest.approx(3.0)
    assert stored["points"] == measured["points"]
    assert bool(stored["loudness"]) == (amplitude == 0.2)
    assert server.manager._dj_cache().get(str(path)).analysed
    assert not server.manager.analysis_queue.pending_scans()


@pytest.mark.parametrize(
    "field,value",
    [
        ("length", "180"),
        ("energy", True),
        ("intro_lift_db", 7.0),
        ("points", {"start": "0", "end": 179.0}),
        ("points", {"start": False, "end": 179.0}),
        ("points", {"start": 0.0, "end": 179.0, "intro": True}),
        ("loudness", {"lufs": -20.0}),
        ("dj", {"beats": [1.0] * 20001}),
    ],
)
def test_worker_rejects_malformed_scan_result(server, field, value):
    """ADR-0008-07. Untrusted scan values cannot partially update the catalog."""
    _token, secret = server.token()
    song = claim_scan(server, secret)
    before = server.manager.catalog.record_for_path(server.songs[0])
    scan = scan_result()
    scan[field] = value
    body, _vector = server.result(song, song["lease"], scan=scan)
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 422, reply.text
    assert server.manager.catalog.record_for_path(server.songs[0]) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("beats", [2.0, 1.0]),
        ("beats", [181.0]),
        ("beats", [True]),
        ("intro_start_s", 181.0),
        ("analysed", 1),
        ("cues", [{"time_s": 181.0, "type": "drop"}]),
        ("cues", [{"time_s": 1.0, "type": "drop", "source": "user"}]),
    ],
)
def test_worker_rejects_invalid_dj_scan_values(server, field, value):
    _token, secret = server.token()
    song = claim_scan(server, secret)
    scan = scan_result()
    scan["dj"][field] = value
    body, _vector = server.result(song, song["lease"], scan=scan)
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 422, reply.text


def test_worker_dj_storage_failure_retains_scan_without_vector_hold(server, monkeypatch):
    _token, secret = server.token()
    song = claim_scan(server, secret)
    monkeypatch.setattr(
        server.manager, "store_scan_dj", Mock(side_effect=OSError("cache unavailable"))
    )
    body, _vector = server.result(song, song["lease"], scan=scan_result())
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 422, reply.text
    queue = server.manager.analysis_queue
    assert queue.leased_count() == 0
    assert not queue.held_paths()
    assert queue.pending_scans()
    assert queue.workers()[0]["songs_failed"] == 1
    assert server.manager.catalog.has_vector(server.songs[0])
    assert (
        server.manager.catalog.record_for_path(server.songs[0])["points"] == scan_result()["points"]
    )


def test_worker_combined_result_rejects_changed_file(server):
    _token, secret = server.token()
    song = claim_scan(server, secret)
    before = server.manager.catalog.record_for_path(server.songs[0])
    server.songs[0].write_bytes(b"changed audio")
    body, _vector = server.result(song, song["lease"], scan=scan_result())
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 409, reply.text
    assert server.manager.catalog.record_for_path(server.songs[0]) == before


def test_old_worker_cannot_claim_deferred_scan_work(server):
    _token, secret = server.token()
    record = server.manager.catalog.record_for_path(server.songs[0])
    server.manager.analysis_queue.defer_scan(record["path"], record["size"], record["fingerprint"])
    songs = claim(server, secret, batch=16)["songs"]
    assert len(songs) == 2
    assert all(song["fingerprint"] != record["fingerprint"] for song in songs)


def test_worker_scan_finishes_after_pending_song_is_retagged(server):
    from autodj.indexer import file_fingerprint

    _token, secret = server.token()
    catalog = server.manager.catalog
    record = catalog.record_for_path(server.songs[0])
    server.manager.analysis_queue.defer_scan(record["path"], record["size"], record["fingerprint"])
    server.songs[0].write_bytes(b"new tags and audio")
    catalog.retag(server.songs[0], {"title": "Edited title"})
    reply = server.call("POST", "/api/worker/claim", secret, json=SCAN_CAPABILITY)
    assert reply.status_code == 200
    song = reply.json()["songs"][0]
    assert "scan" in song
    assert server.manager.analysis_queue.abandoned_scans(worker_active=True) == []
    body, _ = server.result(song, song["lease"], scan=scan_result())
    stored = server.call("POST", "/api/worker/result", secret, json=body)
    assert stored.status_code == 200, stored.text
    assert not server.manager.analysis_queue.pending_scans()
    assert catalog.record_for_path(server.songs[0])["title"] == "Edited title"
    after = catalog.record_for_path(server.songs[0])
    assert (after["size"], after["fingerprint"]) == file_fingerprint(server.songs[0])


def test_worker_cancels_pending_scan_when_music_becomes_liner(server):
    _token, secret = server.token()
    catalog = server.manager.catalog
    record = catalog.record_for_path(server.songs[0])
    server.manager.analysis_queue.defer_scan(record["path"], record["size"], record["fingerprint"])
    catalog.put_entry(make_entry(server.songs[0]), "liner")
    reply = server.call("POST", "/api/worker/claim", secret, json={**SCAN_CAPABILITY, "batch": 16})
    assert reply.status_code == 200, reply.text
    assert len(reply.json()["songs"]) == 2
    assert not server.manager.analysis_queue.pending_scans()


def test_worker_claims_pending_bytes_before_playable_record_replacement(server):
    from autodj.indexer import file_fingerprint

    _token, secret = server.token()
    record = server.manager.catalog.record_for_path(server.songs[0])
    server.songs[0].write_bytes(b"changed audio")
    size, fingerprint = file_fingerprint(server.songs[0])
    queue = server.manager.analysis_queue
    queue.defer_scan(record["path"], size, fingerprint)
    reply = server.call("POST", "/api/worker/claim", secret, json={**SCAN_CAPABILITY, "batch": 16})
    assert reply.status_code == 200, reply.text
    assert len(reply.json()["songs"]) == 3
    song = reply.json()["songs"][0]
    assert "scan" in song
    assert (song["size"], song["fingerprint"]) == (size, fingerprint)
    assert server.manager.catalog.record_for_path(server.songs[0]) == record
    assert queue.scan_pending(record["path"])
    queue.finish_scan(record["path"], size, fingerprint)
    assert not queue.pending_scans()


def test_worker_scan_preserves_user_cues_and_manual_tags(server):
    _token, secret = server.token()
    song = claim_scan(server, secret)
    catalog = server.manager.catalog
    catalog.set_points(server.songs[0], {"intro": 15.0}, source="user")
    catalog.retag(server.songs[0], {"bpm": 133.0, "key": 7, "mode": 1})
    body, _vector = server.result(song, song["lease"], scan=scan_result())
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 200, reply.text
    record = catalog.record_for_path(server.songs[0])
    assert record["points"]["intro"] == 15.0
    assert record["point_sources"]["intro"] == "user"
    assert (record["bpm"], record["key"], record["mode"]) == (133.0, 7, 1)


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize(
    "edit",
    [
        "points",
        "outside_suggestion",
        "trim_start",
        "trim_end",
        "manual_tags",
        "dj",
        "shorter",
        "shorter_intro_only",
        "invalid_start",
        "analysis",
    ],
)
@pytest.mark.parametrize("new_listing", [False, True])
def test_pending_replacement_keeps_current_edits(server, monkeypatch, fallback, edit, new_listing):
    """ADR-0008-23. Deferred measurements retain current edits on either completion path."""
    from autodj.dj_meta import Cue, DjMeta
    from autodj.indexer import file_fingerprint

    manager = server.manager
    catalog = manager.catalog
    path = server.songs[0]
    catalog.set_points(path, {"start": 0.0, "end": 180.0, "intro": 5.0}, source="analysis")
    path.write_bytes(b"replacement recording")
    if new_listing:
        catalog.put_entry(replace(make_entry(path), energy=0))
    queue = manager.analysis_queue
    queue.defer_scan(_path(path), *file_fingerprint(path), new_listing=new_listing)
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: True)
    _token, secret = server.token()
    song = server.call("POST", "/api/worker/claim", secret, json=SCAN_CAPABILITY).json()["songs"][0]
    measurements = scan_result()
    if edit == "points":
        catalog.set_points(path, {"start": 0.0, "end": 180.0, "intro": 179.5}, source="user")
        catalog.set_points(path, {"outro": None}, source="imported")
    elif edit == "outside_suggestion":
        catalog.set_points(path, {"intro": 179.5})
    elif edit == "trim_start":
        catalog.set_points(path, {"start": 179.5, "intro": None})
    elif edit == "trim_end":
        catalog.set_points(path, {"end": 0.05, "intro": None})
    elif edit == "manual_tags":
        catalog.retag(path, {"bpm": 133.0, "key": 7, "mode": 1})
    elif edit == "dj":
        catalog.set_points(path, {"intro": 25.0}, source="user")
        manager._dj_cache().set(
            str(path),
            DjMeta(
                intro_end_s=25.0,
                analysed=True,
                cues=[Cue(25.0, "intro_end", source="user"), Cue(40.0, "hot", "Current", "user")],
            ),
        )
    elif edit in {"shorter", "shorter_intro_only", "invalid_start"}:
        catalog.set_points(
            path,
            {"intro": 170.0}
            if edit == "shorter_intro_only"
            else {"start": 0.0, "end": 180.0, "intro": 170.0},
        )
        measurements.update(length=120.0, dj=None)
        measurements["points"] = {
            "start": 1.0,
            "end": 119.0,
            "intro": 10.0,
            "outro": 110.0,
            "segue": 115.0,
        }
        if edit == "invalid_start":
            catalog.set_points(path, {"start": 170.0, "end": 180.0})
        monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: False)

    if fallback:
        queue.release(_token.token_id, [song["lease"]])
        queue.forget_worker(_token.token_id)
        entry = make_entry(path)
        entry.length = measurements["length"]
        monkeypatch.setattr(
            "autodj.library.analyze_basic", lambda *a, **k: (entry, measurements["points"])
        )
        monkeypatch.setattr("autodj.library.load_for_analysis", lambda *a: np.ones(22050))
        monkeypatch.setattr("autodj.intro_lift.measure_source_intro", lambda *a: 1.0)
        monkeypatch.setattr(
            "autodj.dj_meta.analyse_decoded",
            lambda *a, **k: DjMeta(
                intro_end_s=10.0,
                outro_start_s=165.0,
                analysed=True,
            ),
        )
        with manager._scan_lock:
            assert manager._recover_scan_one(False)
    else:
        body, _ = server.result(song, song["lease"], scan=measurements)
        reply = server.call("POST", "/api/worker/result", secret, json=body)
        assert reply.status_code == 200, reply.text
    record = catalog.record_for_path(path)
    assert not queue.scan_pending(_path(path))
    if edit == "points":
        assert record["points"]["intro"] == 179.5
        assert record["points"]["end"] == 180.0
        assert record["points"]["outro"] is None
        assert record["point_sources"]["intro"] == "user"
        assert record["point_sources"]["outro"] == "imported"
        meta = manager._dj_cache().get(str(path))
        assert meta.intro_end_s == 179.5
        assert meta.outro_start_s == 180.0
    elif edit == "outside_suggestion":
        assert record["points"]["intro"] == 179.5
        assert record["points"]["end"] == 180.0
    elif edit == "trim_start":
        assert record["points"]["start"] == 179.5
        assert record["points"]["end"] == 180.0
    elif edit == "trim_end":
        assert record["points"]["start"] == 0.0
        assert record["points"]["end"] == 0.05
    elif edit == "manual_tags":
        assert (record["bpm"], record["key"], record["mode"]) == (133.0, 7, 1)
        assert set(record["user_tags"]) == {"key", "bpm"}
    elif edit == "dj":
        assert record["points"]["intro"] == 25.0
        meta = manager._dj_cache().get(str(path))
        assert meta.intro_end_s == 25.0
        assert Cue(40.0, "hot", "Current", "user") in meta.cues
    elif edit == "shorter":
        assert record["points"]["start"] == 0.0
        assert record["points"]["end"] == 120.0
        assert record["points"]["intro"] == 120.0
        assert record["point_sources"]["intro"] == "user"
    elif edit == "shorter_intro_only":
        assert record["points"]["intro"] == 119.0
        assert record["point_sources"]["intro"] == "user"
    elif edit == "invalid_start":
        assert record["points"]["start"] == 1.0
        assert record["point_sources"]["start"] == "analysis"
        catalog.set_points(path, {"start": 2.0}, source="analysis")
        assert catalog.points(path)["start"] == 2.0
    else:
        assert record["points"]["intro"] == 10.0
        assert record["point_sources"]["intro"] == "analysis"


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("timing", ["before", "after", "legacy"])
@pytest.mark.parametrize("source", ["user", "imported"])
def test_pending_completion_preserves_only_edits_after_defer(
    server, monkeypatch, fallback, timing, source
):
    """ADR-0008-23. Replacement edits need evidence that they changed after deferral."""
    from autodj.audio_meta import FileTags
    from autodj.indexer import file_fingerprint

    manager, path = server.manager, server.songs[0]
    catalog, queue = manager.catalog, manager.analysis_queue
    catalog.set_points(path, {"intro": 50.0}, source=source)
    catalog.retag(path, {"bpm": 99.0, "key": 7, "mode": 0})
    path.write_bytes(b"replacement recording")
    queue.defer_scan(_path(path), *file_fingerprint(path))
    if timing != "before":
        catalog.set_points(path, {"intro": 55.0}, source=source)
        catalog.retag(path, {"bpm": 101.0, "mode": 1})
    if timing == "legacy":
        with queue._write() as db:
            db.execute("UPDATE scan_pending SET edit_snapshot=NULL WHERE path=?", (_path(path),))
    # Repeated discovery must not overwrite the original edit baseline.
    queue.defer_scan(_path(path), *file_fingerprint(path))
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: False)
    monkeypatch.setattr(
        "autodj.audio_meta.read_file_tags", lambda p: FileTags(length=120.0, bpm=131.0)
    )
    assert manager.scan_options(path)["saved_points"] == (
        {"intro": 55.0} if timing == "after" else {}
    )
    measurements = scan_result()
    measurements["dj"] = None
    if fallback:
        entry = replace(make_entry(path), bpm=131.0, key=-1, mode=-1)
        monkeypatch.setattr(
            "autodj.library.analyze_basic", lambda *a, **k: (entry, measurements["points"])
        )
        monkeypatch.setattr("autodj.intro_lift.measure_source_intro", lambda *a: 1.0)
        with manager._scan_lock:
            assert manager._recover_scan_one(False)
    else:
        _token, secret = server.token()
        song = server.call("POST", "/api/worker/claim", secret, json=SCAN_CAPABILITY).json()[
            "songs"
        ][0]
        body, _ = server.result(song, song["lease"], vector=None, scan=measurements)
        reply = server.call("POST", "/api/worker/result", secret, json=body)
        assert reply.status_code == 200, reply.text
    record = catalog.record_for_path(path)
    assert record["points"]["intro"] == (55.0 if timing == "after" else 10.0)
    assert record["point_sources"]["intro"] == (source if timing == "after" else "analysis")
    assert record["bpm"] == (101.0 if timing == "after" else 131.0)
    assert (record["key"], record["mode"]) == ((7, 1) if timing == "after" else (-1, -1))
    assert set(record["user_tags"]) == ({"bpm", "key"} if timing == "after" else set())


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("source", ["user", "imported"])
def test_same_bytes_pending_completion_keeps_existing_edits(
    server, monkeypatch, fallback, legacy, source
):
    """ADR-0008-23. Same-byte rereads retain edits, including rows predating snapshots."""
    from autodj.analysis_queue import AnalysisQueue

    manager, path = server.manager, server.songs[0]
    catalog, queue = manager.catalog, manager.analysis_queue
    catalog.set_points(path, {"intro": 50.0, "outro": None}, source=source)
    catalog.retag(path, {"bpm": 99.0, "key": 7, "mode": 1})
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: False)
    monkeypatch.setattr(manager, "_scan_worker_available", lambda: True)
    manager._scan_pending = queue.pending_scans()
    assert manager._read_track_local("music", path, False, allow_defer=True)
    assert queue.scan_pending(_path(path))
    if legacy:
        with queue._write() as db:
            db.execute("ALTER TABLE scan_pending DROP COLUMN edit_snapshot")
        upgraded = AnalysisQueue(queue.path)
        try:
            assert upgraded._read("SELECT edit_snapshot FROM scan_pending") == [(None,)]
        finally:
            upgraded.close()
    measurements = scan_result()
    measurements["dj"] = None
    if fallback:
        monkeypatch.setattr(
            "autodj.library.analyze_basic",
            lambda *a, **k: (make_entry(path), measurements["points"]),
        )
        monkeypatch.setattr("autodj.intro_lift.measure_source_intro", lambda *a: 1.0)
        with manager._scan_lock:
            assert manager._recover_scan_one(False)
    else:
        _token, secret = server.token()
        song = server.call("POST", "/api/worker/claim", secret, json=SCAN_CAPABILITY).json()[
            "songs"
        ][0]
        assert song["scan"]["saved_points"] == {"intro": 50.0, "outro": None}
        body, _ = server.result(song, song["lease"], scan=measurements)
        reply = server.call("POST", "/api/worker/result", secret, json=body)
        assert reply.status_code == 200, reply.text
    record = catalog.record_for_path(path)
    assert record["points"]["intro"] == 50.0
    assert record["points"]["outro"] is None
    assert record["point_sources"]["intro"] == source
    assert record["point_sources"]["outro"] == source
    assert record["bpm"] == 99.0
    assert (record["key"], record["mode"]) == (7, 1)
    assert set(record["user_tags"]) == {"bpm", "key"}


@pytest.mark.parametrize("source", ["user", "mixxx"])
def test_scan_replaces_old_byte_markers_with_catalog_points(server, monkeypatch, source):
    """ADR-0008-23. Discarded marker edits cannot override analysis in the DJ cache."""
    from autodj.dj_meta import Cue, DjMeta
    from autodj.indexer import file_fingerprint

    manager, path = server.manager, server.songs[0]
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: True)
    manager.catalog.set_points(
        path,
        {"start": 5.0, "intro": 25.0, "outro": 140.0},
        source="user" if source == "user" else "imported",
    )
    hot = Cue(60.0, "hot", "Saved", source)
    manager._dj_cache().set(
        str(path),
        DjMeta(
            intro_start_s=5.0,
            intro_end_s=25.0,
            outro_start_s=140.0,
            analysed=True,
            cues=[
                Cue(5.0, "intro_start", source=source),
                Cue(25.0, "intro_end", source=source),
                Cue(140.0, "outro_start", source=source),
                hot,
            ],
        ),
    )
    path.write_bytes(b"replacement recording")
    manager.analysis_queue.defer_scan(_path(path), *file_fingerprint(path))
    _token, secret = server.token()
    song = server.call("POST", "/api/worker/claim", secret, json=SCAN_CAPABILITY).json()["songs"][0]
    body, _ = server.result(song, song["lease"], scan=scan_result())
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 200, reply.text
    record = manager.catalog.record_for_path(path)
    meta = manager._dj_cache().get(str(path))
    for point, marker, field in (
        ("start", "intro_start", "intro_start_s"),
        ("intro", "intro_end", "intro_end_s"),
        ("outro", "outro_start", "outro_start_s"),
    ):
        assert record["point_sources"][point] == "analysis"
        assert getattr(meta, field) == record["points"][point]
        assert all(
            cue.time_s == record["points"][point] and cue.source == "auto"
            for cue in meta.cues
            if cue.type == marker
        )
    assert hot in meta.cues


@pytest.mark.parametrize("cue_case", ["duplicate", "past_end"])
def test_scan_saved_cues_are_unique_and_within_duration(server, monkeypatch, caplog, cue_case):
    """ADR-0008-23. Saved markers merge after alignment; old cues cannot exceed new duration."""
    from autodj.dj_meta import Cue, DjMeta
    from autodj.indexer import file_fingerprint

    manager, path = server.manager, server.songs[0]
    path.write_bytes(b"replacement recording")
    manager.analysis_queue.defer_scan(_path(path), *file_fingerprint(path))
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: True)
    manager.catalog.set_points(path, {"intro": 25.0})
    manager._dj_cache().set(
        str(path),
        DjMeta(
            analysed=True,
            cues=[
                Cue(25.0, "intro_end", source="user"),
                Cue(30.0, "intro_end", source="mixxx"),
                Cue(180.0, "hot", "Boundary", "user"),
                Cue(200.0, "hot", "Old", "user"),
            ],
        ),
    )
    _token, secret = server.token()
    song = server.call("POST", "/api/worker/claim", secret, json=SCAN_CAPABILITY).json()["songs"][0]
    body, _ = server.result(song, song["lease"], scan=scan_result())
    with caplog.at_level(logging.DEBUG, logger="autodj.library"):
        reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 200, reply.text
    cues = manager._dj_cache().get(str(path)).cues
    if cue_case == "duplicate":
        assert [cue for cue in cues if cue.type == "intro_end"] == [
            Cue(25.0, "intro_end", source="user")
        ]
    else:
        assert all(cue.time_s <= 180.0 for cue in cues)
        assert Cue(180.0, "hot", "Boundary", "user") in cues
        assert any(
            record.levelno == logging.DEBUG and "200" in record.message for record in caplog.records
        )


@pytest.mark.parametrize("state", ["failed", "stale"])
def test_pending_move_preserves_lease_hold(server, state):
    """ADR-0008-24. Moving a held song cannot bypass its original lease expiry."""
    from autodj.indexer import file_fingerprint

    manager, old = server.manager, server.songs[0]
    queue = manager.analysis_queue
    identity = file_fingerprint(old)
    lease = queue.claim("worker", [(_path(old), *identity)], 600)[0]
    queue.finish(lease, state)
    queue.defer_scan(_path(old), *identity)
    expiry = queue._read("SELECT expires_at FROM analysis_leases WHERE path=?", (_path(old),))[0][0]
    new = old.with_name("moved.flac")
    old.rename(new)
    assert manager.catalog.rekey(old, new)
    assert queue._read(
        "SELECT state,expires_at FROM analysis_leases WHERE path=?", (_path(new),)
    ) == [(state, expiry)]
    assert queue.claim("worker", [(_path(new), *identity)], 600) == []
    server.clock.now = expiry + 1
    assert queue.claim("worker", [(_path(new), *identity)], 600)


@pytest.mark.parametrize("new_listing", [False, True])
def test_pending_move_rejects_old_worker_result(server, new_listing):
    """ADR-0008-24. Moves cancel old claims and preserve pending work and history."""
    from autodj.indexer import file_fingerprint

    manager = server.manager
    old = server.songs[0]
    if new_listing:
        manager.catalog.put_entry(replace(make_entry(old), energy=0))
    else:
        old.write_bytes(b"replacement recording")
    queue = manager.analysis_queue
    queue.defer_scan(_path(old), *file_fingerprint(old), new_listing=new_listing)
    manager.catalog.retag(old, {"bpm": 133.0})
    ledgers = [manager.ledger, manager.add_ledger("second")]
    for ledger in ledgers:
        ledger.remember_play(old, 42.0)
        ledger.remember_history(
            {
                "event_id": "played",
                "path": _path(old),
                "title": "Song",
                "artist": "Artist",
                "duration": 180.0,
                "played_at": "2026-10-22T12:00:00Z",
                "performances": 2,
            }
        )
        ledger.flush()
    before = manager.catalog.record_for_path(old)
    _token, secret = server.token()
    song = server.call("POST", "/api/worker/claim", secret, json=SCAN_CAPABILITY).json()["songs"][0]
    body, _ = server.result(song, song["lease"], scan=scan_result())
    new = old.with_name("renamed.flac")
    old.rename(new)
    manager._reconcile_moves([("music", new), *[("music", p) for p in server.songs[1:]]])
    moved = manager.catalog.record_for_path(new)
    assert moved is not None and moved["id"] == before["id"]
    assert moved["user_tags"] == before["user_tags"]
    for ledger in ledgers:
        assert ledger.last_played()[_path(new)] == 42.0
        history = ledger.store.play_log()
        assert len(history) == 1 and history[0]["path"] == _path(new)
        assert history[0]["performances"] == 2
    assert queue.scan_identity(_path(new)) == file_fingerprint(new)
    assert queue.scan_created_listing(_path(new), *file_fingerprint(new)) == new_listing
    assert not queue.scan_pending(_path(old))
    reply = server.call("POST", "/api/worker/result", secret, json=body)
    assert reply.status_code == 404, reply.text
    assert manager.catalog.record_for_path(new) == moved
    assert manager.catalog.record_for_path(old) is None
    reclaimed = server.call("POST", "/api/worker/claim", secret, json=SCAN_CAPABILITY).json()[
        "songs"
    ][0]
    assert reclaimed["lease"] != song["lease"]
    assert reclaimed["fingerprint"] == song["fingerprint"]


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

    def claim_with_race(holder, songs, seconds, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            real_claim("Other worker", songs, seconds, **kwargs)
            return []
        return real_claim(holder, songs, seconds, **kwargs)

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
    token, secret = server.token()
    song = claim(server, secret)["songs"][0]
    body, _ = server.result(song, song["lease"], vector=vector)
    assert server.call("POST", "/api/worker/result", secret, json=body).status_code == 422
    assert not server.manager.catalog.has_vector(server.songs[0])
    assert server.manager.analysis_queue.lease(song["lease"], token.token_id) is None
    assert all(
        candidate["fingerprint"] != song["fingerprint"]
        for candidate in claim(server, secret, batch=16)["songs"]
    )


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
        content=b"{" + b" " * RESULT_BODY_MAX_BYTES + b"}",
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


# ---------------------------------------------------------------------------
# Managing workers from Settings (#554): settings, pairing, refusal notices
# ---------------------------------------------------------------------------

ORIGIN = {"Origin": "http://127.0.0.1:8080"}


def put_settings(server, token_id, **settings):
    body = {"paused": False, "batch": None, "idle_release_minutes": None, **settings}
    return server.client.put(
        f"/api/analysis/workers/{token_id}/settings", json=body, headers=ORIGIN
    )


def managed_beat(server, secret, **body):
    return server.call(
        "POST", "/api/worker/heartbeat", secret, json={"worker_manage_v1": True, **body}
    )


def workers_view(server):
    return server.client.get("/api/analysis/workers").json()


def test_worker_settings_round_trip_with_a_revision_per_change(server):
    """ADR-0008-25. Settings are stored per token and sent in heartbeat and claim replies."""
    token, secret = server.token()
    saved = put_settings(server, token.token_id, paused=True, batch=2, idle_release_minutes=5)
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"paused": True, "batch": 2, "idle_release_minutes": 5, "revision": 1}
    beat = server.call("POST", "/api/worker/heartbeat", secret, json={}).json()
    assert beat["worker_manage_v1"] is True
    assert beat["settings"] == saved.json()
    assert claim(server, secret)["settings"] == saved.json()
    # The same values again change nothing, so a worker has nothing new to apply.
    again = put_settings(server, token.token_id, paused=True, batch=2, idle_release_minutes=5)
    assert again.json()["revision"] == 1
    assert put_settings(server, token.token_id, batch=2).json()["revision"] == 2
    (worker,) = workers_view(server)["workers"]
    assert worker["settings"]["revision"] == 2
    # This worker never said which revision it applied.
    assert worker["settings_applied"] is None
    managed_beat(server, secret, settings_revision=1)
    assert workers_view(server)["workers"][0]["settings_applied"] is False
    managed_beat(server, secret, settings_revision=2)
    assert workers_view(server)["workers"][0]["settings_applied"] is True


@pytest.mark.parametrize(
    "settings",
    [
        {"batch": 0},
        {"batch": 17},
        {"batch": 2.5},
        {"idle_release_minutes": 0},
        {"idle_release_minutes": 7 * 24 * 60 + 1},
        {"paused": "yes"},
        {"before_command": "rm -rf /"},
    ],
)
def test_worker_settings_out_of_range_are_refused(server, settings):
    """ADR-0008-28. Only pause, batch and idle release exist; no setting names a command."""
    token, _secret = server.token()
    assert put_settings(server, token.token_id, **settings).status_code == 422
    assert server.app.state.machine_tokens().worker_settings(token.token_id).revision == 0


def test_worker_settings_need_an_active_worker_token(server):
    token, _secret = server.token()
    announcer, _ = server.app.state.machine_tokens().create(
        "news-bot", [SCOPE_ANNOUNCER], "default"
    )
    assert put_settings(server, "0" * 32).status_code == 404
    assert put_settings(server, announcer.token_id).status_code == 404
    server.client.delete(f"/api/machine-tokens/{token.token_id}", headers=ORIGIN)
    assert put_settings(server, token.token_id).status_code == 404


def test_a_paused_worker_is_handed_no_songs_and_still_pauses_server_analysis(server):
    """ADR-0008-26. Pause holds for any worker version; the CPU job stays paused too."""
    token, secret = server.token()
    put_settings(server, token.token_id, paused=True)
    # An older worker's claim: no manage fields at all.
    paused = claim(server, secret, batch=4)
    assert paused["songs"] == []
    assert paused["settings"]["paused"] is True
    assert server.manager.analysis_queue.held_paths() == set()
    view = workers_view(server)
    assert view["server_paused"] is True
    assert view["workers"][0]["connected"] is True
    put_settings(server, token.token_id, paused=False)
    assert len(claim(server, secret, batch=4)["songs"]) == 3


def test_an_older_worker_keeps_working_with_a_managing_server(server):
    """ADR-0008-27. Legacy request shapes are accepted; new reply fields are only additions."""
    _token, secret = server.token()
    beat = server.call("POST", "/api/worker/heartbeat", secret, json={"device": "GPU"})
    assert beat.status_code == 200
    songs = claim(server, secret, batch=1)["songs"]
    body, _ = server.result(songs[0], songs[0]["lease"])
    assert server.call("POST", "/api/worker/result", secret, json=body).json() == {"stored": True}


def test_an_identity_mismatch_is_refused_noted_and_cleared_on_the_next_connect(server):
    """ADR-0008-29. The notice names the worker and what to set; a good connect clears it."""
    token, secret = server.token()
    wrong = {**server.identity(), "model_commit": "f" * 40}
    refused = managed_beat(server, secret, connect=True, identity=wrong)
    assert refused.status_code == 409
    assert f'revision = "{COMMIT}"' in refused.json()["detail"]
    (notice,) = workers_view(server)["refusals"]
    assert notice["token_id"] == token.token_id
    assert notice["name"] == "Studio PC"
    assert notice["kind"] == "identity"
    assert f'revision = "{COMMIT}"' in notice["message"]
    assert secret not in str(notice)
    # Before it has the model, a worker reports no weights; that passes.
    partial = {key: value for key, value in server.identity().items() if key != "model_commit"}
    assert managed_beat(server, secret, identity=partial).status_code == 200
    assert workers_view(server)["refusals"]
    # Its claims are refused the same way while the weights differ.
    refused_claim = server.call(
        "POST", "/api/worker/claim", secret, json={"worker_manage_v1": True, "identity": wrong}
    )
    assert refused_claim.status_code == 409
    ok = managed_beat(server, secret, connect=True, identity=server.identity())
    assert ok.status_code == 200
    assert workers_view(server)["refusals"] == []


def test_an_older_workers_mismatched_result_is_noted(server):
    """ADR-0008-29."""
    _token, secret = server.token()
    song = claim(server, secret)["songs"][0]
    body, _ = server.result(song, song["lease"], model_commit="f" * 40)
    assert server.call("POST", "/api/worker/result", secret, json=body).status_code == 409
    (notice,) = workers_view(server)["refusals"]
    assert notice["kind"] == "identity"


def test_a_scan_version_mismatch_is_noted_until_the_versions_match(server):
    """ADR-0008-29."""
    _token, secret = server.token()
    mismatch = {"scan_decode_v1": True, "scan_analysis_version": "unsupported"}
    assert managed_beat(server, secret, connect=True, **mismatch).status_code == 200
    (notice,) = workers_view(server)["refusals"]
    assert notice["kind"] == "scan_version"
    assert "unsupported" in notice["message"]
    assert "same release" in notice["message"]
    managed_beat(server, secret, connect=True, **SCAN_CAPABILITY)
    assert workers_view(server)["refusals"] == []


def test_token_refusals_are_noted_once_per_token_without_secrets(server):
    """ADR-0008-29. Revoked, wrong-secret and wrong-scope tokens of known ids; nothing else."""
    store = server.app.state.machine_tokens()
    revoked, revoked_secret = server.token("Old PC")
    _wrong, wrong_secret = server.token("Typo PC")
    _announcer, announcer_secret = store.create("news-bot", [SCOPE_ANNOUNCER], "default")
    store.revoke(revoked.token_id)
    for _ in range(20):
        assert server.call("POST", "/api/worker/claim", revoked_secret, json={}).status_code == 401
    bad = wrong_secret[:-1] + ("A" if wrong_secret[-1] != "A" else "B")
    assert server.call("POST", "/api/worker/claim", bad, json={}).status_code == 401
    assert server.call("POST", "/api/worker/claim", announcer_secret, json={}).status_code == 401
    unknown = f"adj1.{'0' * 32}.{'A' * 43}"
    assert server.call("POST", "/api/worker/claim", unknown, json={}).status_code == 401
    notices = workers_view(server)["refusals"]
    by_name = {notice["name"]: notice for notice in notices}
    assert set(by_name) == {"Old PC", "Typo PC", "news-bot"}
    assert by_name["Old PC"]["kind"] == "revoked"
    assert by_name["Old PC"]["revoked"] is True
    assert "Pair the worker again" in by_name["Old PC"]["message"]
    assert by_name["Typo PC"]["kind"] == "invalid"
    assert by_name["news-bot"]["kind"] == "scope"
    for secret in (revoked_secret, wrong_secret, announcer_secret, bad):
        assert secret.split(".")[-1] not in str(notices)
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM worker_refusals").fetchone()[0] == 3


def test_refusal_details_are_cut_short(server):
    """ADR-0008-29."""
    token, _secret = server.token()
    store = server.app.state.machine_tokens()
    store.record_refusal(token.token_id, "identity", "x" * 5000 + "\n\x1b")
    (notice,) = store.refusals()
    assert len(notice["detail"]) == 600
    store.record_refusal("not-an-id", "identity", "ignored")
    store.record_refusal(token.token_id, "made-up kind", "ignored")
    assert len(store.refusals()) == 1


def test_a_dismissed_refusal_stays_hidden_until_another_reason(server):
    """ADR-0008-29."""
    token, secret = server.token()
    store = server.app.state.machine_tokens()
    store.revoke(token.token_id)
    server.call("POST", "/api/worker/claim", secret, json={})
    gone = server.client.delete(f"/api/analysis/workers/{token.token_id}/refusal", headers=ORIGIN)
    assert gone.status_code == 200
    assert workers_view(server)["refusals"] == []
    store._refused.clear()  # past the once-a-minute write limit
    server.call("POST", "/api/worker/claim", secret, json={})
    assert workers_view(server)["refusals"] == []
    store.record_refusal(token.token_id, "invalid")
    assert [notice["kind"] for notice in workers_view(server)["refusals"]] == ["invalid"]
    missing = server.client.delete(f"/api/analysis/workers/{'0' * 32}/refusal", headers=ORIGIN)
    assert missing.status_code == 404


def pairing_code(server, name=""):
    reply = server.client.post(
        "/api/machine-tokens/pairing-code", json={"name": name}, headers=ORIGIN
    )
    assert reply.status_code == 201, reply.text
    assert reply.headers["cache-control"] == "no-store"
    return reply.json()


def pair(server, code, name="gpu-box", client=None):
    return (client or server.client).post("/api/worker-pair", json={"code": code, "name": name})


def _other(code):
    return f"{(int(code) + 1) % 100_000_000:08d}"


def test_a_pairing_code_trades_once_for_an_analysis_worker_token(server, caplog):
    """ADR-0007-14. Single use; the token is a worker token named by Settings or the host."""
    caplog.set_level("INFO")
    made = pairing_code(server, "Studio PC")
    assert len(made["code"]) == 8 and made["code"].isdecimal()
    assert made["valid_seconds"] == 600
    paired = pair(server, made["code"])
    assert paired.status_code == 201, paired.text
    assert paired.headers["cache-control"] == "no-store"
    secret = paired.json()["token"]
    assert paired.json()["name"] == "Studio PC"
    assert server.call("POST", "/api/worker/claim", secret, json={}).status_code == 200
    token = server.app.state.machine_tokens().get(paired.json()["token_id"])
    assert token.scopes == (SCOPE_ANALYSIS_WORKER,)
    # Used once; the same code again is refused.
    assert pair(server, made["code"]).status_code == 401
    # Without a name from Settings, the worker's host name names it.
    named = pair(server, pairing_code(server)["code"], name="gpu-box\x07").json()
    assert named["name"] == "gpu-box"
    assert made["code"] not in caplog.text
    assert secret not in caplog.text


def test_a_pairing_code_expires_and_a_new_one_replaces_it(server, monkeypatch):
    """ADR-0007-14."""
    clock = [1000.0]
    monkeypatch.setattr(server.app.state.worker_pairing, "_monotonic", lambda: clock[0])
    first = pairing_code(server)["code"]
    second = pairing_code(server)["code"]
    if first != second:
        assert pair(server, first).status_code == 401
    clock[0] += 601
    assert pair(server, second).status_code == 401


def test_wrong_pairing_codes_lock_out_a_client(server):
    """ADR-0007-14. The browser pairing's per-client limit, checked before comparing."""
    code = pairing_code(server)["code"]
    for _ in range(10):
        assert pair(server, _other(code)).status_code == 401
    locked = pair(server, code)
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0
    # A new code from Settings starts the counts again.
    assert pair(server, pairing_code(server)["code"]).status_code == 201


def test_many_wrong_pairing_codes_burn_the_code():
    """ADR-0007-14. The server-wide limit burns the code; with no code open nothing counts."""
    from autodj.worker_api import WorkerPairing

    pairing = WorkerPairing()
    assert not pairing.take("12345678", "a").paired
    assert pairing._clients == {}
    code, _ = pairing.open(None)
    for n in range(50):
        assert not pairing.take(_other(code), f"client-{n % 9}").paired
    assert not pairing.take(code, "someone-else").paired
    code, _ = pairing.open("Studio PC")
    outcome = pairing.take(code, "a")
    assert outcome.paired
    assert outcome.name == "Studio PC"


def test_the_pairing_route_takes_only_programs_over_https(server):
    """ADR-0007-14. No session is needed; a browser Origin or plain HTTP from afar is refused."""
    code = pairing_code(server)["code"]
    browser = server.client.post(
        "/api/worker-pair", json={"code": code, "name": "x"}, headers=ORIGIN
    )
    assert browser.status_code == 403
    plain = TestClient(server.app, base_url="http://127.0.0.1:8080")
    assert pair(server, code, client=plain).status_code == 403
    assert pair(server, "1234567").status_code == 422
    assert pair(server, code).status_code == 201


def test_a_worker_token_cannot_manage_workers_or_make_pairing_codes(tmp_path):
    """ADR-0007-14. These are paired-browser routes; a worker token opens none of them."""
    made = Server(tmp_path / "paired", access_token=secrets.token_urlsafe(32))
    open_server = Server(tmp_path / "open")
    try:
        settings = {"paused": True, "batch": None, "idle_release_minutes": None}

        def attempts(token_id):
            return [
                ("PUT", f"/api/analysis/workers/{token_id}/settings", settings),
                ("POST", "/api/machine-tokens/pairing-code", {"name": ""}),
                ("DELETE", f"/api/analysis/workers/{token_id}/refusal", None),
            ]

        token, secret = made.token()
        for method, path, body in [
            *attempts(token.token_id),
            ("GET", "/api/analysis/workers", None),
        ]:
            # Without an Origin an unsafe request stops even before the session check.
            assert made.call(method, path, secret, json=body).status_code in {401, 403}, path
            with_origin = made.call(method, path, secret, json=body, headers=ORIGIN)
            assert with_origin.status_code == 401, path
        # Without pairing, a program sending no Origin still cannot change anything.
        token, secret = open_server.token()
        for method, path, body in attempts(token.token_id):
            assert open_server.call(method, path, secret, json=body).status_code == 403, path
        assert open_server.app.state.machine_tokens().worker_settings(token.token_id).revision == 0
    finally:
        made.close()
        open_server.close()
