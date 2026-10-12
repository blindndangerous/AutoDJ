"""Leases on songs waiting for advanced analysis, and the workers taking them.

The analysis queue is the catalog's music without a vector
(:meth:`~autodj.catalog.LibraryCatalog.unembedded_paths`).  Whoever
analyses a song first takes a lease on it in ``library.db``: a remote
worker (``autodj worker``, keyed by its machine token) or the server's own
CPU job (holder :data:`SERVER_HOLDER`).  A lease runs out after a while
unless renewed, so the songs of a worker that died go back to the queue on
their own.  The server's job runs in another process; keeping the leases in
the same SQLite file is what lets the two see each other's.

A song whose analysis failed keeps a ``failed`` lease for
:data:`FAILED_RETRY_SECONDS`, and one whose file changed since the library
read it a ``stale`` lease for :data:`STALE_RETRY_SECONDS`, so neither is
handed out again and again.

Workers report in on every request; one seen within
:data:`WORKER_ACTIVE_SECONDS` counts as connected, and while one is, the
server's CPU job leaves the queue to it.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

#: The holder name of the server's own CPU analysis job.
SERVER_HOLDER = "server"
#: How long a worker's lease lasts without a heartbeat.
WORKER_LEASE_SECONDS = 600
#: How long the server's CPU job holds a song it is analysing.
SERVER_LEASE_SECONDS = 3600
#: How long a song that failed waits before anyone is given it again.
FAILED_RETRY_SECONDS = 6 * 3600
#: How long a song whose file changed since it was read waits: the next
#: library scan reads it again, and handing it out sooner only fails again.
STALE_RETRY_SECONDS = 3600
#: A worker seen this recently is connected.  Workers report every 30 s.
WORKER_ACTIVE_SECONDS = 120
#: Expired leases older than this are deleted.
_FORGET_SECONDS = 86_400

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS analysis_leases (
        path TEXT PRIMARY KEY,
        lease_id TEXT NOT NULL UNIQUE,
        holder TEXT NOT NULL,
        state TEXT NOT NULL,
        size INTEGER NOT NULL,
        fingerprint TEXT NOT NULL,
        expires_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS analysis_workers (
        token_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        device TEXT NOT NULL DEFAULT '',
        last_seen REAL NOT NULL,
        session_started REAL NOT NULL,
        session_done INTEGER NOT NULL DEFAULT 0,
        done INTEGER NOT NULL DEFAULT 0,
        failed INTEGER NOT NULL DEFAULT 0,
        stale INTEGER NOT NULL DEFAULT 0
    );
"""


@dataclass(frozen=True)
class Lease:
    """One song handed to one holder."""

    lease_id: str
    path: str
    holder: str
    size: int
    fingerprint: str
    expires_at: float


class AnalysisQueue:
    """Leases and worker presence, kept in the library's ``library.db``."""

    def __init__(self, path: Path | str, *, now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._now = now
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            self.path, timeout=30, check_same_thread=False, isolation_level=None
        )
        try:
            self._conn.executescript(_SCHEMA)
        except BaseException:
            self._conn.close()
            self._conn = None
            raise

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One write transaction, holding the lock."""
        with self._lock:
            if self._conn is None:
                raise RuntimeError("analysis queue is closed")
            db = self._conn
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    def _read(self, sql: str, args: Sequence[object] = ()) -> list[tuple]:
        with self._lock:
            if self._conn is None:
                raise RuntimeError("analysis queue is closed")
            return self._conn.execute(sql, args).fetchall()

    # ------------------------------------------------------------------
    # Leases
    # ------------------------------------------------------------------

    def held_paths(self) -> set[str]:
        """Paths someone holds now: claimed, or recently failed or stale."""
        return {
            row[0]
            for row in self._read(
                "SELECT path FROM analysis_leases WHERE expires_at >= ?", (self._now(),)
            )
        }

    def claim(
        self, holder: str, songs: Iterable[tuple[str, int, str]], seconds: float
    ) -> list[Lease]:
        """Lease each free ``(path, size, fingerprint)`` to *holder* for *seconds*.

        A song another holder has a live lease on is left out, so two
        workers, or a worker and the CPU job, never get the same song.
        """
        now = self._now()
        expires = now + seconds
        leases = []
        with self._write() as db:
            db.execute("DELETE FROM analysis_leases WHERE expires_at < ?", (now - _FORGET_SECONDS,))
            for path, size, fingerprint in songs:
                lease_id = uuid.uuid4().hex
                changed = db.execute(
                    "INSERT INTO analysis_leases VALUES (?, ?, ?, 'claimed', ?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET lease_id=excluded.lease_id, "
                    "holder=excluded.holder, state='claimed', size=excluded.size, "
                    "fingerprint=excluded.fingerprint, expires_at=excluded.expires_at "
                    "WHERE analysis_leases.expires_at < ?",
                    (path, lease_id, holder, size, fingerprint, expires, now),
                ).rowcount
                if changed == 1:
                    leases.append(Lease(lease_id, path, holder, size, fingerprint, expires))
        return leases

    def lease(self, lease_id: str, holder: str) -> Lease | None:
        """The claimed lease *lease_id* if *holder* holds it, expired or not.

        An expired lease nobody else has taken yet still counts, so a worker
        that was slow does not throw its result away.
        """
        rows = self._read(
            "SELECT lease_id, path, holder, size, fingerprint, expires_at FROM analysis_leases "
            "WHERE lease_id = ? AND holder = ? AND state = 'claimed'",
            (lease_id, holder),
        )
        return Lease(*rows[0]) if rows else None

    def renew(self, holder: str, seconds: float) -> int:
        """Extend every claimed lease *holder* has; returns how many."""
        with self._write() as db:
            return db.execute(
                "UPDATE analysis_leases SET expires_at = ? WHERE holder = ? AND state = 'claimed'",
                (self._now() + seconds, holder),
            ).rowcount

    def release(self, holder: str, lease_ids: Iterable[str]) -> int:
        """Give *holder*'s claimed leases back to the queue; returns how many."""
        released = 0
        with self._write() as db:
            for lease_id in lease_ids:
                released += db.execute(
                    "DELETE FROM analysis_leases "
                    "WHERE lease_id = ? AND holder = ? AND state = 'claimed'",
                    (lease_id, holder),
                ).rowcount
        return released

    def release_path(self, holder: str, path: str) -> None:
        """Drop *holder*'s claimed lease on *path*, if it has one."""
        with self._write() as db:
            db.execute(
                "DELETE FROM analysis_leases WHERE path = ? AND holder = ? AND state = 'claimed'",
                (path, holder),
            )

    def finish(self, lease: Lease, outcome: str) -> None:
        """Close *lease*: ``done``, ``stale`` (the file changed) or ``failed``.

        A failed or stale song is held back for :data:`FAILED_RETRY_SECONDS`
        or :data:`STALE_RETRY_SECONDS`.  The holder's counts change with it.
        """
        if outcome not in {"done", "stale", "failed"}:
            raise ValueError(f"unknown outcome: {outcome}")
        with self._write() as db:
            if outcome != "done":
                hold = FAILED_RETRY_SECONDS if outcome == "failed" else STALE_RETRY_SECONDS
                db.execute(
                    "UPDATE analysis_leases SET state = ?, expires_at = ? WHERE lease_id = ?",
                    (outcome, self._now() + hold, lease.lease_id),
                )
            else:
                db.execute("DELETE FROM analysis_leases WHERE lease_id = ?", (lease.lease_id,))
            session = ", session_done = session_done + 1" if outcome == "done" else ""
            db.execute(
                f"UPDATE analysis_workers SET {outcome} = {outcome} + 1{session} "  # nosec B608 -- outcome is one of three column names
                "WHERE token_id = ?",
                (lease.holder,),
            )

    def leased_count(self) -> int:
        """Songs being analysed now, by anyone."""
        return int(
            self._read(
                "SELECT COUNT(*) FROM analysis_leases WHERE state = 'claimed' AND expires_at >= ?",
                (self._now(),),
            )[0][0]
        )

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    def seen(self, token_id: str, name: str, device: str | None = None) -> None:
        """Note that the worker using *token_id* is here.

        A worker that comes back after :data:`WORKER_ACTIVE_SECONDS` starts
        a new session, which its rate is measured over.
        """
        now = self._now()
        with self._write() as db:
            row = db.execute(
                "SELECT last_seen FROM analysis_workers WHERE token_id = ?", (token_id,)
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO analysis_workers (token_id, name, device, last_seen, "
                    "session_started) VALUES (?, ?, ?, ?, ?)",
                    (token_id, name, device or "", now, now),
                )
                return
            if now - row[0] > WORKER_ACTIVE_SECONDS:
                db.execute(
                    "UPDATE analysis_workers SET session_started = ?, session_done = 0 "
                    "WHERE token_id = ?",
                    (now, token_id),
                )
            db.execute(
                "UPDATE analysis_workers SET name = ?, last_seen = ?, "
                "device = COALESCE(?, device) WHERE token_id = ?",
                (name, now, device, token_id),
            )

    def worker_active(self) -> bool:
        """Whether any worker was seen in the last :data:`WORKER_ACTIVE_SECONDS`."""
        return bool(
            self._read(
                "SELECT 1 FROM analysis_workers WHERE last_seen >= ? LIMIT 1",
                (self._now() - WORKER_ACTIVE_SECONDS,),
            )
        )

    def forget_worker(self, token_id: str) -> None:
        """Drop a worker and give its claimed songs back, as when its token is revoked."""
        with self._write() as db:
            db.execute(
                "DELETE FROM analysis_leases WHERE holder = ? AND state = 'claimed'", (token_id,)
            )
            db.execute("DELETE FROM analysis_workers WHERE token_id = ?", (token_id,))

    def workers(self) -> list[dict[str, object]]:
        """Every worker seen, most recent first, with its counts and rate.

        ``rate_per_hour`` is songs analysed per hour since the worker last
        connected, or ``0`` before it has finished one.
        """
        now = self._now()
        rows = self._read(
            "SELECT token_id, name, device, last_seen, session_started, session_done, done, "
            "failed, stale FROM analysis_workers ORDER BY last_seen DESC"
        )
        held = dict(
            self._read(
                "SELECT holder, COUNT(*) FROM analysis_leases "
                "WHERE state = 'claimed' AND expires_at >= ? GROUP BY holder",
                (now,),
            )
        )
        result: list[dict[str, object]] = []
        for token_id, name, device, last_seen, started, session_done, done, failed, stale in rows:
            elapsed = max(60.0, last_seen - started)
            result.append(
                {
                    "token_id": token_id,
                    "name": name,
                    "device": device,
                    "last_seen": last_seen,
                    "connected": now - last_seen <= WORKER_ACTIVE_SECONDS,
                    "songs_done": done,
                    "songs_failed": failed,
                    "songs_stale": stale,
                    "rate_per_hour": round(session_done * 3600 / elapsed, 1)
                    if session_done
                    else 0.0,
                    "leased": int(held.get(token_id, 0)),
                }
            )
        return result


__all__ = [
    "FAILED_RETRY_SECONDS",
    "SERVER_HOLDER",
    "SERVER_LEASE_SECONDS",
    "STALE_RETRY_SECONDS",
    "WORKER_ACTIVE_SECONDS",
    "WORKER_LEASE_SECONDS",
    "AnalysisQueue",
    "Lease",
]
