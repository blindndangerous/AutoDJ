"""Leases on songs waiting for analysis, and the workers holding them."""

import pytest

from autodj.analysis_queue import (
    FAILED_RETRY_SECONDS,
    SERVER_HOLDER,
    STALE_RETRY_SECONDS,
    WORKER_ACTIVE_SECONDS,
    WORKER_LEASE_SECONDS,
    AnalysisQueue,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def queue(tmp_path):
    clock = Clock()
    q = AnalysisQueue(tmp_path / "library.db", now=clock)
    yield q, clock
    q.close()


SONGS = [(f"/music/{n}.flac", 100 + n, f"{n:064x}") for n in range(3)]


def test_remote_scan_claim_rechecks_pending_identity(queue):
    q, _clock = queue
    path, size, fingerprint = SONGS[0]
    q.defer_scan(path, size, fingerprint)
    q.refresh_scan(path, size + 1, "changed")
    assert q.claim("worker", [SONGS[0]], 600, require_scan={path}) == []
    assert q.claim("worker", [(path, size + 1, "changed")], 600, require_scan={path})


@pytest.mark.parametrize("refresh", [False, True])
def test_scan_failure_evidence_survives_restart_until_bytes_change(queue, refresh):
    """ADR-0008-19. Access failures cannot erase earlier non-access failure evidence."""
    q, clock = queue
    q.defer_scan(*SONGS[0], new_listing=True)
    assert q.claim_scan(*SONGS[0])
    q.fail_scan(*SONGS[0], non_access_failure=True)
    q.close()
    reopened = AnalysisQueue(q.path, now=clock)
    try:
        reopened.fail_scan(*SONGS[0])
        assert reopened.scan_failed_without_access_error(*SONGS[0])
        changed = (SONGS[0][0], 500, "changed")
        if refresh:
            assert reopened.refresh_scan(*changed)
        else:
            reopened.defer_scan(*changed)
        assert not reopened.scan_failed_without_access_error(*changed)
        assert reopened.claim_scan(*changed)
    finally:
        reopened.close()


def test_local_scan_retry_preserves_live_remote_lease(queue):
    """ADR-0008-09. Explicit retry cannot steal a live claim, even without a heartbeat."""
    q, clock = queue
    q.defer_scan(*SONGS[0])
    lease = q.claim("worker", [SONGS[0]], WORKER_LEASE_SECONDS)[0]
    clock.now += WORKER_ACTIVE_SECONDS + 1
    assert not q.worker_active(scan_decode=True)
    assert not q.claim_scan(*SONGS[0], retry_failed=True)
    assert q.lease(lease.lease_id, "worker") == lease


def test_busy_worker_recovery_does_not_walk_pending_backlog(queue, monkeypatch):
    """ADR-0008-20. Indexed handoffs and leases bound recovery work during worker progress."""
    q, clock = queue
    with q._write() as db:
        db.executemany(
            "INSERT INTO scan_pending(path,size,fingerprint,deadline) VALUES (?,100,'hash',?)",
            ((f"/music/backlog-{n}.flac", clock.now - 1) for n in range(80000)),
        )
    q.seen("worker", "Worker", scan_decode=True)
    q.claim("worker", [("/music/active.flac", 100, "hash")], 600)
    steps = []
    plans = []
    read = q._read

    def explain(sql, args=()):
        plans.extend(row[3] for row in read("EXPLAIN QUERY PLAN " + sql, args))
        return read(sql, args)

    monkeypatch.setattr(q, "_read", explain)
    q._conn.set_progress_handler(lambda: steps.append(1) or 0, 100)
    try:
        assert q.abandoned_scans(limit=1) == []
    finally:
        q._conn.set_progress_handler(None, 0)
    assert len(steps) < 100, "idle recovery walked the pending backlog"
    assert any("scan_pending_handoff" in plan for plan in plans)
    q.handoff_scan("/music/backlog-7.flac")
    assert q.abandoned_scans(limit=1) == ["/music/backlog-7.flac"]


def test_scan_candidate_queries_use_retry_index(queue, monkeypatch):
    """ADR-0008-08. Candidate searches skip exhausted rows and seek past backoff rows."""
    q, clock = queue
    for song in SONGS:
        q.defer_scan(*song)
    with q._write() as db:
        db.execute("UPDATE scan_pending SET attempts=3 WHERE path=?", (SONGS[0][0],))
        db.execute("UPDATE scan_pending SET retry_at=? WHERE path=?", (clock.now + 60, SONGS[1][0]))
    read = q._read
    plans = []

    def explain(sql, args=()):
        plans.append(read("EXPLAIN QUERY PLAN " + sql, args))
        return read(sql, args)

    monkeypatch.setattr(q, "_read", explain)
    assert q.abandoned_scans() == [SONGS[2][0]]
    assert q.claimable_scans(limit=1) == [SONGS[2][0]]
    assert len(plans) == 3
    for plan in plans[1:]:
        assert any("SEARCH p USING INDEX scan_pending_ready (retry_at<?)" in row[3] for row in plan)
        assert not any("TEMP B-TREE" in row[3] for row in plan)


def test_scan_deferral_uses_presence_leases_and_failed_holds(queue):
    q, clock = queue
    path, size, fingerprint = SONGS[0]
    q.seen("worker", "Worker", scan_decode=True)
    q.defer_scan(path, size, fingerprint)
    assert q.defer_scan_now(path)
    clock.now += WORKER_ACTIVE_SECONDS + 1
    assert not q.defer_scan_now(path)
    q.seen("worker", "Worker", scan_decode=True)
    lease = q.claim("worker", SONGS[:1], WORKER_LEASE_SECONDS)[0]
    q.finish(lease, "failed")
    clock.now += FAILED_RETRY_SECONDS - 1
    assert not q.defer_scan_now(path)
    clock.now += 2
    q.seen("worker", "Worker", scan_decode=True)
    assert not q.defer_scan_now(path)
    # Reopening preserves both pending work and capability negotiation.
    other = AnalysisQueue(q.path, now=clock)
    try:
        assert other.scan_pending(path)
        assert other.worker_active(scan_decode=True)
    finally:
        other.close()


def test_unclaimed_scan_deferral_has_a_deadline(queue):
    q, clock = queue
    path, size, fingerprint = SONGS[0]
    q.seen("worker", "Worker", scan_decode=True)
    q.defer_scan(path, size, fingerprint)
    clock.now += WORKER_LEASE_SECONDS + 1
    q.seen("worker", "Worker", scan_decode=True)
    q.defer_scan(path, size, fingerprint)
    assert not q.defer_scan_now(path)
    q.finish_scan(path, size, fingerprint)
    assert not q.scan_pending(path)


def test_scan_deadline_waits_while_capable_worker_claims_progress(queue):
    """ADR-0008-10. A busy worker retains its unclaimed scan backlog."""
    q, clock = queue
    for song in SONGS:
        q.defer_scan(*song)
    q.seen("worker", "Worker", scan_decode=True)
    for _ in range(4):
        clock.now += WORKER_LEASE_SECONDS / 2
        q.seen("worker", "Worker", scan_decode=True)
        lease = q.claim("worker", SONGS[:1], WORKER_LEASE_SECONDS)[0]
        q.finish(lease, "done")
        assert q.abandoned_scans(worker_active=True) == []
        assert q.defer_scan_now(SONGS[1][0])
    clock.now += WORKER_LEASE_SECONDS + 1
    q.seen("worker", "Worker", scan_decode=True)
    assert q.abandoned_scans(worker_active=True, limit=1) == [SONGS[0][0]]
    assert q.abandoned_scan_count(worker_active=True) == 3


@pytest.mark.parametrize("state", ["claimed", "failed"])
def test_vector_holds_do_not_block_scan_attempts(queue, state):
    """ADR-0008-11. Vector jobs and scan retry holds are independent."""
    q, _clock = queue
    song = SONGS[0]
    q.defer_scan(*song)
    lease = q.claim(SERVER_HOLDER, [song], 3600)[0]
    if state == "failed":
        q.finish(lease, "failed")
    assert not q.defer_scan_now(song[0], worker_active=False)
    assert q.abandoned_scans(worker_active=False) == [song[0]]
    assert q.claim_scan(*song)
    assert q._read("SELECT state FROM analysis_leases") == [(state,)]


def test_scan_attempts_do_not_hold_vector_jobs_and_stop_after_three(queue):
    """ADR-0008-11. Persistent retry budget gates scans only."""
    q, clock = queue
    song = SONGS[0]
    q.defer_scan(*song)
    for attempt in range(3):
        assert q.claim_scan(*song)
        assert q.fail_scan(*song) == (attempt == 2)
        lease = q.claim(SERVER_HOLDER, [song], 3600)[0]
        q.finish(lease, "done")
        assert q.abandoned_scans(worker_active=False) == []
        clock.now += FAILED_RETRY_SECONDS + 1
    assert not q.claim_scan(*song)
    assert q.abandoned_scans(worker_active=False) == []
    q.defer_scan(*song)
    assert not q.claim_scan(*song)
    changed = (song[0], song[1] + 1, song[2])
    q.refresh_scan(*changed)
    assert q.claim_scan(*changed)


@pytest.mark.parametrize("settling", [False, True], ids=["handoff", "settling"])
def test_refresh_scan_keeps_local_recovery_budget(queue, settling):
    """ADR-0008-11. Further copies cannot reopen handed-off or settling work's retry budget."""
    q, clock = queue
    song = SONGS[0]
    q.defer_scan(*song)
    q.handoff_scan(song[0])
    for attempt in range(3):
        assert q.claim_scan(*song)
        if settling:
            changed = (song[0], song[1] + 1, f"decode-{attempt}")
            assert q.reschedule_changed_scan(song[0], song[1:], changed[1:]) == (attempt == 2)
            song = changed
        else:
            q.fail_scan(*song)
        song = (song[0], song[1] + 1, f"refresh-{attempt}")
        assert q.refresh_scan(*song)
        assert not q.claim_scan(*song)
        assert q.claimable_scans(limit=1) == []
        clock.now += 61 if settling else FAILED_RETRY_SECONDS + 1
    assert not q.claim_scan(*song)
    assert q.abandoned_scans(worker_active=False) == []
    # A full scan retains its explicit access-only exhaustion reset.
    assert q.refresh_scan(*song, retry_access_errors=True)
    assert q.claim_scan(*song)
    q.finish_scan(*song)
    q.defer_scan(*song)
    assert q.claim_scan(*song)


def test_scan_queue_upgrades_existing_pending_work(tmp_path):
    import sqlite3
    from contextlib import closing

    path = tmp_path / "library.db"
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.executescript(
            "CREATE TABLE scan_pending (path TEXT PRIMARY KEY, size INTEGER NOT NULL, "
            "fingerprint TEXT NOT NULL, deadline REAL NOT NULL);"
            "CREATE TABLE scan_workers (token_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL);"
        )
        db.execute("INSERT INTO scan_pending VALUES (?, ?, ?, ?)", (*SONGS[0], 100))
        db.execute("INSERT INTO scan_workers VALUES ('worker', 1)")
    q = AnalysisQueue(path, now=lambda: 1000)
    try:
        assert q.pending_scans() == {SONGS[0][0]}
        assert not q.scan_created_listing(*SONGS[0])
        assert not q.scan_failed_without_access_error(*SONGS[0])
        assert q.claim_scan(*SONGS[0])
        assert not q.fail_scan(*SONGS[0])
    finally:
        q.close()
    q = AnalysisQueue(path, now=lambda: 1001)
    try:
        assert not q.claim_scan(*SONGS[0])
        assert q._read("SELECT attempts FROM scan_pending") == [(1,)]
    finally:
        q.close()


def test_remote_scan_failure_releases_vector_work_and_reserves_local_recovery(queue):
    """ADR-0008-11. A remote scan failure cannot impose an advanced-analysis hold."""
    q, _clock = queue
    song = SONGS[0]
    q.seen("worker", "Worker", scan_decode=True)
    q.defer_scan(*song)
    lease = q.claim("worker", [song], WORKER_LEASE_SECONDS, require_scan={song[0]})[0]
    q.finish(lease, "failed")
    assert song[0] not in q.held_paths()
    assert q.abandoned_scans(worker_active=True) == [song[0]]
    assert q.claim("worker", [song], WORKER_LEASE_SECONDS, require_scan={song[0]}) == []
    assert q.claim(SERVER_HOLDER, [song], 3600)
    assert q.claim_scan(*song)
    assert q.workers()[0]["songs_failed"] == 1


def test_local_scan_reservation_invalidates_expired_remote_claim(queue):
    q, clock = queue
    song = SONGS[0]
    q.defer_scan(*song)
    lease = q.claim("worker", [song], WORKER_LEASE_SECONDS)[0]
    clock.now += WORKER_LEASE_SECONDS + 1
    assert q.claim_scan(*song)
    assert q.renew("worker", WORKER_LEASE_SECONDS) == 0
    assert q.lease(lease.lease_id, "worker") is None


def test_completed_scan_cannot_be_recreated_by_claim_reconciliation(queue):
    q, _clock = queue
    path, size, fingerprint = SONGS[0]
    q.defer_scan(path, size, fingerprint)
    assert q.refresh_scan(path, size + 1, fingerprint)
    q.finish_scan(path, size + 1, fingerprint)
    assert not q.refresh_scan(path, size, fingerprint)
    assert q.claim("worker", SONGS[:1], WORKER_LEASE_SECONDS, require_scan={path}) == []
    assert not q.pending_scans()


def test_scan_recovery_ignores_vector_holds_but_respects_remote_claims(queue):
    q, clock = queue
    for path, size, fingerprint in SONGS:
        q.defer_scan(path, size, fingerprint)
    live, failed = q.claim("worker", SONGS[:2], WORKER_LEASE_SECONDS)
    local = q.claim(SERVER_HOLDER, SONGS[2:], 3600)[0]
    q.finish(failed, "failed")
    q.finish(local, "failed")
    assert q.abandoned_scans(worker_active=True) == [failed.path]
    assert not q.claim_scan(*SONGS[0])
    assert q.claim_scan(*SONGS[1])
    assert q.claim_scan(*SONGS[2])
    assert q.lease(live.lease_id, "worker") == live
    assert q.lease(failed.lease_id, "worker") is None
    q.fail_scan(*SONGS[1])
    q.fail_scan(*SONGS[2])
    assert q.abandoned_scans(worker_active=False) == []
    clock.now += WORKER_LEASE_SECONDS + 1
    assert q.abandoned_scans(worker_active=True) == [live.path]


def test_a_song_is_leased_to_one_holder_until_it_expires(queue):
    """ADR-0008-03."""
    q, clock = queue
    first = q.claim("worker-a", SONGS[:2], WORKER_LEASE_SECONDS)
    assert [lease.path for lease in first] == ["/music/0.flac", "/music/1.flac"]
    assert q.claim("worker-b", SONGS, WORKER_LEASE_SECONDS)[0].path == "/music/2.flac"
    assert q.claim(SERVER_HOLDER, SONGS, 3600) == []
    assert q.held_paths() == {path for path, _, _ in SONGS}
    assert q.leased_count() == 3
    # A dead worker never strands its songs: the lease runs out.
    clock.now += WORKER_LEASE_SECONDS + 1
    again = q.claim("worker-b", SONGS[:1], WORKER_LEASE_SECONDS)
    assert [lease.path for lease in again] == ["/music/0.flac"]
    assert q.lease(first[0].lease_id, "worker-a") is None
    # An expired lease nobody took still belongs to its worker.
    assert q.lease(first[1].lease_id, "worker-a") is not None
    assert q.lease(first[1].lease_id, "worker-b") is None


def test_heartbeat_renews_and_release_returns_songs(queue):
    q, clock = queue
    leases = q.claim("worker-a", SONGS, WORKER_LEASE_SECONDS)
    clock.now += WORKER_LEASE_SECONDS - 10
    assert q.renew("worker-a", WORKER_LEASE_SECONDS) == 3
    clock.now += 20
    assert q.claim("worker-b", SONGS, WORKER_LEASE_SECONDS) == []
    assert q.release("worker-b", [leases[0].lease_id]) == 0
    assert q.release("worker-a", [leases[0].lease_id]) == 1
    assert [lease.path for lease in q.claim("worker-b", SONGS, 60)] == ["/music/0.flac"]
    q.release_path("worker-a", "/music/1.flac")
    assert "/music/1.flac" not in q.held_paths()


def test_finishing_counts_and_holds_back_failed_songs(queue):
    """ADR-0008-03."""
    q, clock = queue
    q.seen("worker-a", "Studio PC", "RTX (CUDA GPU)")
    done, stale, failed = q.claim("worker-a", SONGS, WORKER_LEASE_SECONDS)
    clock.now += 30
    q.finish(done, "done")
    q.finish(stale, "stale")
    q.finish(failed, "failed")
    with pytest.raises(ValueError):
        q.finish(done, "lost")
    assert q.held_paths() == {failed.path, stale.path}
    assert q.lease(failed.lease_id, "worker-a") is None
    clock.now += STALE_RETRY_SECONDS + 1
    assert [lease.path for lease in q.claim("worker-b", SONGS, 60)] == [done.path, stale.path]
    assert failed.path in q.held_paths()
    clock.now += FAILED_RETRY_SECONDS
    assert [lease.path for lease in q.claim("worker-b", [SONGS[2]], 60)] == [failed.path]
    (worker,) = q.workers()
    assert worker["name"] == "Studio PC"
    assert worker["device"] == "RTX (CUDA GPU)"
    assert (worker["songs_done"], worker["songs_failed"], worker["songs_stale"]) == (1, 1, 1)


def test_presence_sessions_and_rate(queue):
    q, clock = queue
    assert not q.worker_active()
    q.seen("worker-a", "Studio PC")
    assert q.worker_active()
    for _ in range(6):
        lease = q.claim("worker-a", [SONGS[0]], WORKER_LEASE_SECONDS)[0]
        clock.now += 100
        q.seen("worker-a", "Studio PC")
        q.finish(lease, "done")
    (worker,) = q.workers()
    assert worker["connected"] is True
    assert worker["rate_per_hour"] == pytest.approx(36.0)
    clock.now += WORKER_ACTIVE_SECONDS + 1
    assert not q.worker_active()
    assert q.workers()[0]["connected"] is False
    # Coming back starts a new session; the totals stay.
    q.seen("worker-a", "Renamed box", "CPU")
    (worker,) = q.workers()
    assert worker["rate_per_hour"] == 0.0
    assert worker["songs_done"] == 6
    assert worker["name"] == "Renamed box"
    q.seen("worker-a", "Renamed box")
    assert q.workers()[0]["device"] == "CPU"


def test_forgetting_a_worker_returns_its_songs(queue):
    q, _clock = queue
    q.seen("worker-a", "Studio PC")
    q.claim("worker-a", SONGS, WORKER_LEASE_SECONDS)
    q.forget_worker("worker-a")
    assert q.workers() == []
    assert q.held_paths() == set()


def test_closed_queue_refuses_work(tmp_path):
    q = AnalysisQueue(tmp_path / "library.db")
    q.close()
    q.close()
    with pytest.raises(RuntimeError):
        q.held_paths()
    with pytest.raises(RuntimeError):
        q.renew("w", 1)
