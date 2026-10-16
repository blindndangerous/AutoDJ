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
