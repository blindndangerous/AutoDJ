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
handed out again and again.  A library scan that reads the song's new bytes
clears either hold at once.

Workers report in on every request; one seen within
:data:`WORKER_ACTIVE_SECONDS` counts as connected, and while one is, the
server's CPU job leaves the queue to it.

Remote scan decoding uses the same leases. ``scan_pending`` remembers deferred
music and independent local scan reservations/retries. ``scan_workers`` records
negotiated decoding support and claim progress, so an older worker never
suppresses local scan decoding and a busy capable worker keeps its backlog.
"""

from __future__ import annotations

import json
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
#: Local scan failures have a separate, persistent retry budget.
SCAN_MAX_ATTEMPTS = 3
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
    CREATE TABLE IF NOT EXISTS scan_pending (
        path TEXT PRIMARY KEY,
        size INTEGER NOT NULL,
        fingerprint TEXT NOT NULL,
        deadline REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        retry_at REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS scan_workers (
        token_id TEXT PRIMARY KEY,
        enabled INTEGER NOT NULL,
        last_claim REAL NOT NULL DEFAULT 0
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
            with self._write() as db:
                for table, column, definition in (
                    ("scan_pending", "edit_snapshot", "TEXT"),
                    ("scan_pending", "attempts", "INTEGER NOT NULL DEFAULT 0"),
                    ("scan_pending", "retry_at", "REAL NOT NULL DEFAULT 0"),
                    ("scan_pending", "new_listing", "INTEGER NOT NULL DEFAULT 0"),
                    ("scan_pending", "non_access_failure", "INTEGER NOT NULL DEFAULT 0"),
                    ("scan_workers", "last_claim", "REAL NOT NULL DEFAULT 0"),
                ):
                    if column not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                db.execute(
                    "CREATE INDEX IF NOT EXISTS scan_pending_ready ON scan_pending(retry_at, path) "
                    f"WHERE attempts < {SCAN_MAX_ATTEMPTS}"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS scan_pending_handoff ON scan_pending(retry_at, path) "
                    f"WHERE deadline=0 AND attempts < {SCAN_MAX_ATTEMPTS}"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS analysis_leases_state ON analysis_leases(state)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS analysis_leases_expiry ON analysis_leases(expires_at)"
                )
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
        self,
        holder: str,
        songs: Iterable[tuple[str, int, str]],
        seconds: float,
        *,
        require_scan: set[str] | None = None,
    ) -> list[Lease]:
        """Lease each free ``(path, size, fingerprint)`` to *holder* for *seconds*.

        A song another holder has a live lease on is left out, so two
        workers, or a worker and the CPU job, never get the same song.
        Paths in *require_scan* must still have pending scan work when the
        lease is taken, so a concurrent result cannot resurrect completed work.
        Scan retries and local scan reservations gate combined remote work only.
        """
        now = self._now()
        expires = now + seconds
        leases = []
        with self._write() as db:
            db.execute("DELETE FROM analysis_leases WHERE expires_at < ?", (now - _FORGET_SECONDS,))
            for path, size, fingerprint in songs:
                if (
                    require_scan
                    and path in require_scan
                    and not db.execute(
                        "SELECT 1 FROM scan_pending WHERE path=? AND size=? AND fingerprint=? "
                        "AND attempts<? AND retry_at<=? "
                        "AND deadline>0",
                        (path, size, fingerprint, SCAN_MAX_ATTEMPTS, now),
                    ).fetchone()
                ):
                    continue
                if (
                    holder != SERVER_HOLDER
                    and db.execute(
                        "SELECT 1 FROM scan_pending WHERE path=? "
                        "AND (attempts>=? OR retry_at>? OR deadline=0)",
                        (path, SCAN_MAX_ATTEMPTS, now),
                    ).fetchone()
                ):
                    continue
                lease_id = uuid.uuid4().hex
                changed = db.execute(
                    "INSERT INTO analysis_leases VALUES (?, ?, ?, 'claimed', ?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET lease_id=excluded.lease_id, "
                    "holder=excluded.holder, state='claimed', size=excluded.size, "
                    "fingerprint=excluded.fingerprint, expires_at=excluded.expires_at "
                    "WHERE analysis_leases.expires_at < ?",
                    (
                        path,
                        lease_id,
                        holder,
                        size,
                        fingerprint,
                        expires,
                        now,
                    ),
                ).rowcount
                if changed == 1:
                    leases.append(Lease(lease_id, path, holder, size, fingerprint, expires))
            if leases:
                db.execute(
                    "UPDATE scan_workers SET last_claim=? WHERE token_id=? AND enabled=1",
                    (now, holder),
                )
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

    def renew(self, holder: str, seconds: float, lease_ids: list[str] | None = None) -> int:
        """Extend reported claims; an omitted list preserves legacy holder-wide renewal."""
        with self._write() as db:
            if lease_ids is not None:
                return sum(
                    db.execute(
                        "UPDATE analysis_leases SET expires_at=? "
                        "WHERE holder=? AND state='claimed' AND lease_id=?",
                        (self._now() + seconds, holder, lease_id),
                    ).rowcount
                    for lease_id in set(lease_ids)
                )
            return db.execute(
                "UPDATE analysis_leases SET expires_at = ? WHERE holder = ? AND state = 'claimed'",
                (self._now() + seconds, holder),
            ).rowcount

    def release_claims(self, holder: str) -> None:
        """Release a previous process's claims when its token connects again."""
        with self._write() as db:
            db.execute("DELETE FROM analysis_leases WHERE holder=? AND state='claimed'", (holder,))

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

    def clear_holds(self, path: str, size: int, fingerprint: str) -> bool:
        """Free *path* from a failed or stale hold made on other bytes than these.

        A library scan calls this when it reads a song again: a file that
        changed is a new chance, so it need not wait out the hold.  A hold on
        the same bytes stays.  Returns whether a hold was cleared.
        """
        with self._write() as db:
            return (
                db.execute(
                    "DELETE FROM analysis_leases WHERE path = ? AND state IN ('failed', 'stale') "
                    "AND (size != ? OR fingerprint != ?)",
                    (path, size, fingerprint),
                ).rowcount
                > 0
            )

    def finish(self, lease: Lease, outcome: str) -> None:
        """Close *lease*: ``done``, ``stale`` (the file changed) or ``failed``.

        A failed or stale song is held back for :data:`FAILED_RETRY_SECONDS`
        or :data:`STALE_RETRY_SECONDS`.  The holder's counts change with it.
        Remote failures with pending scans instead request local recovery without
        holding the independent vector job. A zero deadline marks that handoff.
        """
        if outcome not in {"done", "stale", "failed"}:
            raise ValueError(f"unknown outcome: {outcome}")
        with self._write() as db:
            scan_failure = (
                outcome != "done"
                and lease.holder != SERVER_HOLDER
                and db.execute(
                    "UPDATE scan_pending SET deadline=0 WHERE path=? AND size=? AND fingerprint=? "
                    "AND EXISTS (SELECT 1 FROM analysis_leases WHERE lease_id=? AND state='claimed')",
                    (lease.path, lease.size, lease.fingerprint, lease.lease_id),
                ).rowcount
                == 1
            )
            if outcome != "done" and not scan_failure:
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

    def seen(
        self,
        token_id: str,
        name: str,
        device: str | None = None,
        *,
        scan_decode: bool | None = None,
    ) -> None:
        """Note that the worker using *token_id* is here.

        A worker that comes back after :data:`WORKER_ACTIVE_SECONDS` starts
        a new session, which its rate is measured over.
        """
        now = self._now()
        with self._write() as db:
            if scan_decode is not None:
                db.execute(
                    "INSERT INTO scan_workers (token_id, enabled) VALUES (?, ?) ON CONFLICT(token_id) "
                    "DO UPDATE SET enabled=excluded.enabled",
                    (token_id, int(scan_decode)),
                )
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

    def worker_active(self, *, scan_decode: bool = False) -> bool:
        """Whether any worker was seen in the last :data:`WORKER_ACTIVE_SECONDS`."""
        return bool(
            self._read(
                "SELECT 1 FROM analysis_workers WHERE last_seen >= ? "  # nosec B608 -- fixed clauses only; values are bound
                + (
                    "AND token_id IN (SELECT token_id FROM scan_workers WHERE enabled=1) "
                    if scan_decode
                    else ""
                )
                + "LIMIT 1",
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
            db.execute("DELETE FROM scan_workers WHERE token_id = ?", (token_id,))

    def defer_scan(
        self, path: str, size: int, fingerprint: str, *, new_listing: bool = False
    ) -> None:
        """Remember scan work durably, with a bounded wait even if nobody claims it."""
        with self._write() as db:
            snapshot = {}
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_entries'"
            ).fetchone():
                row = db.execute(
                    "SELECT points,point_sources,user_tags,bpm,key,mode FROM catalog_entries WHERE path=?",
                    (path,),
                ).fetchone()
                if row is not None:
                    snapshot = {
                        "points": json.loads(row[0]),
                        "point_sources": json.loads(row[1]),
                        "user_tags": json.loads(row[2]),
                        "bpm": row[3],
                        "key": row[4],
                        "mode": row[5],
                    }
            db.execute(
                "INSERT INTO scan_pending (path, size, fingerprint, deadline, new_listing, edit_snapshot) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) "
                "DO UPDATE SET size=excluded.size, fingerprint=excluded.fingerprint, "
                "deadline=excluded.deadline, attempts=0, retry_at=0, non_access_failure=0, "
                "edit_snapshot=excluded.edit_snapshot "
                "WHERE scan_pending.size != excluded.size "
                "OR scan_pending.fingerprint != excluded.fingerprint",
                (
                    path,
                    size,
                    fingerprint,
                    self._now() + WORKER_LEASE_SECONDS,
                    int(new_listing),
                    json.dumps(snapshot),
                ),
            )

    def scan_created_listing(self, path: str, size: int, fingerprint: str) -> bool:
        """Only explicitly new pending rows may retire their own unplayable tag listing."""
        return bool(
            self._read(
                "SELECT 1 FROM scan_pending WHERE path=? AND size=? AND fingerprint=? AND new_listing=1",
                (path, size, fingerprint),
            )
        )

    def pending_scans(self) -> set[str]:
        return {row[0] for row in self._read("SELECT path FROM scan_pending")}

    def claimable_scans(self, *, limit: int) -> list[str]:
        """Select a bounded remote batch before any file or catalog work."""
        now = self._now()
        return [
            row[0]
            for row in self._read(
                "SELECT p.path FROM scan_pending p WHERE p.attempts<? AND p.retry_at<=? "
                "AND p.deadline>0 AND NOT EXISTS (SELECT 1 FROM analysis_leases l "
                "WHERE l.path=p.path AND l.expires_at>=?) "
                "ORDER BY p.retry_at, p.path LIMIT ?",
                (SCAN_MAX_ATTEMPTS, now, now, max(1, limit)),
            )
        ]

    def handoff_scan(self, path: str, *, expired_only: bool = False) -> None:
        """Let the local scanner repair an identity a worker cannot safely claim."""
        with self._write() as db:
            db.execute(
                "UPDATE scan_pending SET deadline=0 WHERE path=? AND (? OR deadline<=?)",
                (path, not expired_only, self._now()),
            )

    def scan_worker_claiming(self) -> bool:
        """Whether a connected capable worker has made recent claim progress."""
        now = self._now()
        return bool(
            self._read(
                "SELECT 1 FROM scan_workers s JOIN analysis_workers w ON w.token_id=s.token_id "
                "WHERE s.enabled=1 AND w.last_seen>=? AND s.last_claim>=? LIMIT 1",
                (now - WORKER_ACTIVE_SECONDS, now - WORKER_LEASE_SECONDS),
            )
        )

    def _abandoned_scan_query(self, worker_active: bool | None) -> tuple[str, tuple]:
        now = self._now()
        progressing = worker_active is not False and self.scan_worker_claiming()
        if progressing:
            # Start from handoffs and failed/expired leases, never the ordinary backlog.
            return (
                " FROM scan_pending p INDEXED BY sqlite_autoindex_scan_pending_1 WHERE p.path IN ("  # nosec B608 -- fixed SQL and a constant; values are bound
                "SELECT path FROM scan_pending INDEXED BY scan_pending_handoff "
                f"WHERE deadline=0 AND attempts<{SCAN_MAX_ATTEMPTS} AND retry_at<=? "
                "UNION SELECT path FROM analysis_leases WHERE holder!=? "
                "AND (state IN ('failed','stale') OR expires_at<?)) "
                "AND p.attempts<? AND p.retry_at<=? AND NOT EXISTS ("
                "SELECT 1 FROM analysis_leases l WHERE l.path=p.path AND l.holder!=? "
                "AND l.state='claimed' AND l.expires_at>=?)",
                (
                    now,
                    SERVER_HOLDER,
                    now,
                    SCAN_MAX_ATTEMPTS,
                    now,
                    SERVER_HOLDER,
                    now,
                ),
            )
        return (
            " FROM scan_pending p LEFT JOIN analysis_leases l ON l.path=p.path "
            "WHERE p.attempts < ? AND p.retry_at <= ? "
            "AND NOT (COALESCE(l.holder, '') != ? AND COALESCE(l.state, '')='claimed' "
            "AND l.expires_at >= ?) "
            "AND (p.deadline=0 OR NOT COALESCE(?, EXISTS ("
            "SELECT 1 FROM scan_workers s JOIN analysis_workers w ON w.token_id=s.token_id "
            "WHERE s.enabled=1 AND w.last_seen>=?)) "
            "OR (l.holder != ? AND (l.state IN ('failed', 'stale') "
            "OR l.expires_at < ?)) OR (p.deadline < ? AND NOT EXISTS ("
            "SELECT 1 FROM scan_workers s JOIN analysis_workers w ON w.token_id=s.token_id "
            "WHERE s.enabled=1 AND w.last_seen>=? AND s.last_claim>=?)))",
            (
                SCAN_MAX_ATTEMPTS,
                now,
                SERVER_HOLDER,
                now,
                worker_active,
                now - WORKER_ACTIVE_SECONDS,
                SERVER_HOLDER,
                now,
                now,
                now - WORKER_ACTIVE_SECONDS,
                now - WORKER_LEASE_SECONDS,
            ),
        )

    def abandoned_scans(self, *, worker_active: bool | None = None, limit: int = 1) -> list[str]:
        """Read a bounded batch; connected workers making claims retain their backlog."""
        sql, args = self._abandoned_scan_query(worker_active)
        return [
            row[0]
            for row in self._read(
                "SELECT p.path" + sql + " ORDER BY p.retry_at, p.path LIMIT ?",
                (*args, max(1, limit)),
            )
        ]

    def abandoned_scan_count(self, *, worker_active: bool) -> int:
        sql, args = self._abandoned_scan_query(worker_active)
        return int(self._read("SELECT COUNT(*)" + sql, args)[0][0])

    def refresh_scan(
        self, path: str, size: int, fingerprint: str, *, retry_access_errors: bool = False
    ) -> bool:
        """Follow retagged bytes without resurrecting concurrently completed work."""
        with self._write() as db:
            refreshed = (
                db.execute(
                    "UPDATE scan_pending SET deadline=CASE WHEN deadline!=0 "
                    "AND (size!=? OR fingerprint!=?) "
                    "THEN ? ELSE deadline END, "
                    "attempts=CASE WHEN deadline!=0 AND (size!=? OR fingerprint!=?) "
                    "THEN 0 ELSE attempts END, "
                    "retry_at=CASE WHEN deadline!=0 AND (size!=? OR fingerprint!=?) "
                    "THEN 0 ELSE retry_at END, "
                    "non_access_failure=CASE WHEN size!=? OR fingerprint!=? "
                    "THEN 0 ELSE non_access_failure END, "
                    "size=?, fingerprint=? WHERE path=?",
                    (
                        size,
                        fingerprint,
                        self._now() + WORKER_LEASE_SECONDS,
                        size,
                        fingerprint,
                        size,
                        fingerprint,
                        size,
                        fingerprint,
                        size,
                        fingerprint,
                        path,
                    ),
                ).rowcount
                > 0
            )
            if refreshed and retry_access_errors:
                db.execute(
                    "UPDATE scan_pending SET attempts=0, retry_at=0 "
                    "WHERE path=? AND attempts>=? AND non_access_failure=0",
                    (path, SCAN_MAX_ATTEMPTS),
                )
            return refreshed

    def scan_pending(self, path: str) -> bool:
        return bool(self._read("SELECT 1 FROM scan_pending WHERE path=?", (path,)))

    def reschedule_changed_scan(
        self, path: str, previous: tuple[int, str], current: tuple[int, str]
    ) -> bool:
        """Keep the reserved attempt when decoding races a rewrite; report exhaustion."""
        with self._write() as db:
            row = db.execute(
                "UPDATE scan_pending SET size=?, fingerprint=?, deadline=0, retry_at=?, "
                "non_access_failure=0 WHERE path=? AND size=? AND fingerprint=? "
                "RETURNING attempts",
                (*current, self._now() + 60, path, *previous),
            ).fetchone()
            return row is not None and row[0] >= SCAN_MAX_ATTEMPTS

    def scan_identity(self, path: str) -> tuple[int, str] | None:
        """The bytes awaiting measurement, which may differ from the playable record."""
        rows = self._read("SELECT size, fingerprint FROM scan_pending WHERE path=?", (path,))
        return (rows[0][0], rows[0][1]) if rows else None

    def scan_identities(self) -> dict[str, tuple[int, str]]:
        """Pending byte identities for a scan's move reconciliation snapshot."""
        return {
            path: (size, fingerprint)
            for path, size, fingerprint in self._read(
                "SELECT path,size,fingerprint FROM scan_pending"
            )
        }

    def defer_scan_now(self, path: str, *, worker_active: bool | None = None) -> bool:
        """Wait for remote work or a scan retry, independently of CPU vector holds."""
        if worker_active is None:
            worker_active = self.worker_active(scan_decode=True)
        sql, args = self._abandoned_scan_query(worker_active)
        return self.scan_pending(path) and not bool(
            self._read("SELECT 1" + sql + " AND p.path=? LIMIT 1", (*args, path))
        )

    def claim_scan(
        self, path: str, size: int, fingerprint: str, *, retry_failed: bool = False
    ) -> bool:
        """Reserve one local decode without changing the independent vector lease.

        A scan holding the library's scan lock may retry failed work when no capable
        worker is available. Background recovery retains the automatic retry limits.
        """
        now = self._now()
        with self._write() as db:
            claimed = (
                db.execute(
                    "UPDATE scan_pending SET attempts=MIN(attempts+1, ?), retry_at=? "
                    "WHERE path=? AND size=? AND fingerprint=? "
                    "AND (? OR (attempts<? AND retry_at<=?)) "
                    "AND NOT EXISTS (SELECT 1 FROM analysis_leases WHERE path=? "
                    "AND holder!=? AND state='claimed' AND expires_at>=?)",
                    (
                        SCAN_MAX_ATTEMPTS,
                        now + SERVER_LEASE_SECONDS,
                        path,
                        size,
                        fingerprint,
                        retry_failed,
                        SCAN_MAX_ATTEMPTS,
                        now,
                        path,
                        SERVER_HOLDER,
                        now,
                    ),
                ).rowcount
                == 1
            )
            if claimed:
                db.execute(
                    "DELETE FROM analysis_leases WHERE path=? AND holder!=? "
                    "AND state='claimed' AND expires_at<?",
                    (path, SERVER_HOLDER, now),
                )
            return claimed

    def fail_scan(
        self,
        path: str,
        size: int | None = None,
        fingerprint: str | None = None,
        *,
        non_access_failure: bool = False,
    ) -> bool:
        """Back off this scan only; return whether its last attempt exhausted the budget."""
        with self._write() as db:
            row = db.execute(
                "UPDATE scan_pending SET retry_at=?, "
                "non_access_failure=MAX(non_access_failure, ?) WHERE path=? "
                "AND (? IS NULL OR (size=? AND fingerprint=?)) "
                "RETURNING attempts",
                (
                    self._now() + FAILED_RETRY_SECONDS,
                    non_access_failure,
                    path,
                    size,
                    size,
                    fingerprint,
                ),
            ).fetchone()
            return row is not None and row[0] >= SCAN_MAX_ATTEMPTS

    def scan_failed_without_access_error(self, path: str, size: int, fingerprint: str) -> bool:
        """Whether these bytes have any recorded non-access failure."""
        return bool(
            self._read(
                "SELECT 1 FROM scan_pending WHERE path=? AND size=? AND fingerprint=? "
                "AND non_access_failure=1",
                (path, size, fingerprint),
            )
        )

    def finish_scan(self, path: str, size: int, fingerprint: str) -> None:
        with self._write() as db:
            db.execute(
                "DELETE FROM scan_pending WHERE path=? AND size=? AND fingerprint=?",
                (path, size, fingerprint),
            )

    def cancel_scan(self, path: str) -> None:
        """Retire deferred work when the path no longer belongs to music."""
        with self._write() as db:
            db.execute("DELETE FROM scan_pending WHERE path=?", (path,))

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
