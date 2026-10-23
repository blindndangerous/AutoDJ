"""Named, scoped bearer tokens for machines that talk to AutoDJ without a browser.

A machine token lets a program use one narrow part of the API: an
analysis worker (``autodj worker``) on another computer, or an announcer
program that sends DJ breaks, news and weather to one station
(#88).  Each token has a name the operator chose, a set of scopes naming
what it may do, and is shown once, when it is created.  An announcer token
is bound to one station and works for no other.  Only
a SHA-256 digest of its secret is stored, so a copy of the database does
not hand out working tokens.  Tokens are revoked, never deleted, so the
list keeps a record of what existed.

A token reads ``adj1.<id>.<secret>``: the id (32 hex digits) finds the row,
the secret (43 URL-safe characters, 256 random bits) proves possession.
The secret has full entropy, so a plain digest is enough; a slow password
hash would only slow every request down.

Scopes are plain words: :data:`SCOPE_ANALYSIS_WORKER` and
:data:`SCOPE_ANNOUNCER`.

The same file keeps what Settings sets for each analysis worker
(:class:`WorkerSettings`, #554) and the latest time the server refused
each worker token, with why (:meth:`MachineTokenStore.record_refusal`):
one row per token, so neither grows past the number of tokens.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

#: May claim songs to analyse, read the audio of the songs it claimed, and
#: post their analysis.  Nothing else.
SCOPE_ANALYSIS_WORKER = "analysis-worker"
#: May read its station's coming songs and send that station breaks (#88).
#: Nothing else.
SCOPE_ANNOUNCER = "announcer"

#: Every scope a token may carry, with the words Settings shows for it.
KNOWN_SCOPES: dict[str, str] = {
    SCOPE_ANALYSIS_WORKER: "Analysis worker",
    SCOPE_ANNOUNCER: "Announcer",
}
#: Scopes that bind a token to one station.
STATION_SCOPES = frozenset({SCOPE_ANNOUNCER})

TOKEN_PREFIX = "adj1"  # nosec B105 -- the token format version, not a secret
_TOKEN = re.compile(r"adj1\.([0-9a-f]{32})\.([A-Za-z0-9_-]{43})\Z")
_TOKEN_ID = re.compile(r"[0-9a-f]{32}\Z")
_STATION = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")
_MAX_NAME = 64
#: The last-used time is written at most this often per token, so a busy
#: worker does not write the database on every request.
LAST_USED_RESOLUTION_SECONDS = 60
#: The most songs a worker may claim at once, as ``autodj worker --batch``.
MAX_WORKER_BATCH = 16
#: The longest idle release Settings accepts, in minutes: one week.
MAX_IDLE_RELEASE_MINUTES = 7 * 24 * 60
#: Why the server refused a worker; the words for each are in worker_api.
REFUSAL_KINDS = frozenset({"identity", "scan_version", "revoked", "invalid", "scope"})
_MAX_REFUSAL_DETAIL = 600


@dataclass(frozen=True)
class WorkerSettings:
    """What Settings asks one analysis worker to do.

    ``None`` leaves a value to the worker: its command line, or its default.
    *revision* goes up on every change, so a worker applies each change once.
    """

    paused: bool = False
    batch: int | None = None
    idle_release_minutes: int | None = None
    revision: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "paused": self.paused,
            "batch": self.batch,
            "idle_release_minutes": self.idle_release_minutes,
            "revision": self.revision,
        }


def validate_worker_settings(
    paused: object, batch: object, idle_release_minutes: object
) -> tuple[bool, int | None, int | None]:
    """The settings as stored, or ``ValueError`` naming the first one out of range."""
    if not isinstance(paused, bool):
        raise ValueError("paused must be true or false")
    if batch is not None and (
        not isinstance(batch, int) or isinstance(batch, bool) or not 1 <= batch <= MAX_WORKER_BATCH
    ):
        raise ValueError(f"songs per claim must be a whole number from 1 to {MAX_WORKER_BATCH}")
    if idle_release_minutes is not None and (
        not isinstance(idle_release_minutes, int)
        or isinstance(idle_release_minutes, bool)
        or not 1 <= idle_release_minutes <= MAX_IDLE_RELEASE_MINUTES
    ):
        raise ValueError(
            f"idle release minutes must be a whole number from 1 to {MAX_IDLE_RELEASE_MINUTES}"
        )
    return paused, batch, idle_release_minutes


@dataclass(frozen=True)
class MachineToken:
    """One stored token, without its secret."""

    token_id: str
    name: str
    scopes: tuple[str, ...]
    created_at: int
    last_used_at: int | None
    revoked_at: int | None
    # The station an announcer token works for; None for the other scopes.
    station: str | None = None

    @property
    def active(self) -> bool:
        """Whether the token still works."""
        return self.revoked_at is None


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def validate_name(value: object) -> str:
    """Return *value* stripped, or raise ``ValueError`` unless it is 1 to 64 printable characters."""
    if not isinstance(value, str):
        raise ValueError("token name must be text")
    name = value.strip()
    if (
        not name
        or len(name) > _MAX_NAME
        or any(
            not character.isprintable() or unicodedata.category(character) in {"Zl", "Zp"}
            for character in name
        )
    ):
        raise ValueError("token name must contain 1 to 64 printable characters")
    return name


def validate_scopes(values: Iterable[object]) -> tuple[str, ...]:
    """Return the scopes sorted and deduplicated, or raise ``ValueError`` on an unknown one."""
    scopes = set()
    for value in values:
        if not isinstance(value, str) or value not in KNOWN_SCOPES:
            raise ValueError(f"unknown token scope: {value!r}")
        scopes.add(value)
    if not scopes:
        raise ValueError("a token needs at least one scope")
    return tuple(sorted(scopes))


def validate_station(scopes: Iterable[str], station: object) -> str | None:
    """The station a token with *scopes* is bound to, or ``None``.

    Raises:
        ValueError: An announcer token without a station name, or a station
            name on a token that cannot use one.
    """
    needs = bool(STATION_SCOPES.intersection(scopes))
    if station is None or station == "":
        if needs:
            raise ValueError("an announcer token needs the station it works for")
        return None
    if not needs:
        raise ValueError("only an announcer token is bound to a station")
    if not isinstance(station, str) or _STATION.fullmatch(station) is None:
        raise ValueError("not a station name")
    return station


def add_station_column(db: sqlite3.Connection) -> None:
    """Give an older token table its ``station`` column.

    The server and ``autodj tokens`` may both open an older file at once
    and both see the column missing; whichever adds it second finds it
    there, which is fine.
    """
    try:
        db.execute("ALTER TABLE machine_tokens ADD COLUMN station TEXT")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


class MachineTokenStore:
    """Create, list, check and revoke machine tokens in a small SQLite file."""

    def __init__(self, path: Path, *, now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._now = now
        self._touched: dict[str, int] = {}
        self._touch_lock = threading.Lock()
        # token_id -> (kind, detail, last written): repeated refusals of one
        # worker, a minute apart at most, write nothing.
        self._refused: dict[str, tuple[str, str, int]] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS machine_tokens (
                    token_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    secret_sha256 TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER,
                    revoked_at INTEGER
                )
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(machine_tokens)")}
            if "station" not in columns:
                add_station_column(db)
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_settings (
                    token_id TEXT PRIMARY KEY,
                    paused INTEGER NOT NULL DEFAULT 0,
                    batch INTEGER,
                    idle_release_minutes INTEGER,
                    revision INTEGER NOT NULL
                )
                """
            )
            # The latest refusal of each worker token.  A dismissed one stays
            # hidden until the worker is refused for another reason.
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_refusals (
                    token_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    refused_at INTEGER NOT NULL,
                    dismissed INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA busy_timeout = 5000")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _token(row: tuple) -> MachineToken:
        token_id, name, scopes, created, last_used, revoked, station = row
        return MachineToken(
            token_id, name, tuple(scopes.split()), created, last_used, revoked, station
        )

    def create(
        self, name: object, scopes: Iterable[object], station: object = None
    ) -> tuple[MachineToken, str]:
        """Store a new token and return it with the full token text, shown only now.

        *station* names the station an announcer token works for; the
        caller checks that such a station exists.

        Raises:
            ValueError: The name, a scope or the station is not valid.
        """
        clean_name = validate_name(name)
        clean_scopes = validate_scopes(scopes)
        clean_station = validate_station(clean_scopes, station)
        token_id = uuid.uuid4().hex
        secret = secrets.token_urlsafe(32)
        created = int(self._now())
        with self._connect() as db:
            db.execute(
                "INSERT INTO machine_tokens (token_id, name, scopes, secret_sha256, created_at, "
                "station) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    token_id,
                    clean_name,
                    " ".join(clean_scopes),
                    _digest(secret),
                    created,
                    clean_station,
                ),
            )
        token = MachineToken(token_id, clean_name, clean_scopes, created, None, None, clean_station)
        return token, f"{TOKEN_PREFIX}.{token_id}.{secret}"

    def list_tokens(self, *, include_revoked: bool = False) -> list[MachineToken]:
        """Tokens in creation order, the revoked ones only when asked for."""
        sql = (
            "SELECT token_id, name, scopes, created_at, last_used_at, revoked_at, station "
            "FROM machine_tokens"
        )
        if not include_revoked:
            sql += " WHERE revoked_at IS NULL"
        with self._connect() as db:
            rows = db.execute(sql + " ORDER BY created_at, rowid").fetchall()
        return [self._token(row) for row in rows]

    def get(self, token_id: str) -> MachineToken | None:
        """The token with this id, revoked or not."""
        if not isinstance(token_id, str) or _TOKEN_ID.fullmatch(token_id) is None:
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT token_id, name, scopes, created_at, last_used_at, revoked_at, station "
                "FROM machine_tokens WHERE token_id = ?",
                (token_id,),
            ).fetchone()
        return self._token(row) if row else None

    def revoke(self, token_id: str) -> bool:
        """Revoke one active token; ``False`` when there is no such active token."""
        if not isinstance(token_id, str) or _TOKEN_ID.fullmatch(token_id) is None:
            return False
        with self._connect() as db:
            changed = db.execute(
                "UPDATE machine_tokens SET revoked_at = ? "
                "WHERE token_id = ? AND revoked_at IS NULL",
                (int(self._now()), token_id),
            ).rowcount
        return changed == 1

    def authenticate(self, token: str, scope: str) -> MachineToken | None:
        """The active token *token* names, if its secret matches and it carries *scope*.

        Records when it was last used, at most once a minute.  Every failure
        returns ``None`` the same way, so a caller cannot tell a wrong secret
        from a revoked token or a missing scope.  For the analysis-worker
        scope, a refusal of a token id that exists is noted for Settings
        (:meth:`record_refusal`); ids are 128 random bits, so the extra write
        tells nobody which ids exist.
        """
        match = _TOKEN.fullmatch(token) if isinstance(token, str) else None
        if match is None:
            return None
        token_id, secret = match.groups()
        with self._connect() as db:
            row = db.execute(
                "SELECT token_id, name, scopes, created_at, last_used_at, revoked_at, station, "
                "secret_sha256 FROM machine_tokens WHERE token_id = ?",
                (token_id,),
            ).fetchone()
        # Compare even when there is no row, so timing says nothing about ids.
        stored = row[7] if row else "0" * 64
        if not secrets.compare_digest(_digest(secret), stored) or row is None:
            if row is not None and scope == SCOPE_ANALYSIS_WORKER:
                self.record_refusal(token_id, "invalid")
            return None
        found = self._token(row[:7])
        if not found.active or scope not in found.scopes:
            if scope == SCOPE_ANALYSIS_WORKER:
                self.record_refusal(token_id, "revoked" if not found.active else "scope")
            return None
        self._touch(token_id)
        return found

    # ------------------------------------------------------------------
    # Analysis workers: settings from Settings, and refusals
    # ------------------------------------------------------------------

    def _worker_token(self, db: sqlite3.Connection, token_id: str) -> bool:
        row = db.execute(
            "SELECT scopes FROM machine_tokens WHERE token_id = ? AND revoked_at IS NULL",
            (token_id,),
        ).fetchone()
        return row is not None and SCOPE_ANALYSIS_WORKER in row[0].split()

    def worker_settings(self, token_id: str) -> WorkerSettings:
        """What Settings asks of the worker using *token_id*; the defaults if nothing."""
        with self._connect() as db:
            row = db.execute(
                "SELECT paused, batch, idle_release_minutes, revision FROM worker_settings "
                "WHERE token_id = ?",
                (token_id,),
            ).fetchone()
        return WorkerSettings(bool(row[0]), row[1], row[2], row[3]) if row else WorkerSettings()

    def all_worker_settings(self) -> dict[str, WorkerSettings]:
        """Every stored worker's settings, by token id."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT token_id, paused, batch, idle_release_minutes, revision "
                "FROM worker_settings"
            ).fetchall()
        return {row[0]: WorkerSettings(bool(row[1]), row[2], row[3], row[4]) for row in rows}

    def set_worker_settings(
        self, token_id: str, paused: object, batch: object, idle_release_minutes: object
    ) -> WorkerSettings | None:
        """Store a worker's settings; ``None`` when no active worker token has that id.

        The revision goes up only when something changed.

        Raises:
            ValueError: A value is out of range (:func:`validate_worker_settings`).
        """
        values = validate_worker_settings(paused, batch, idle_release_minutes)
        if not isinstance(token_id, str) or _TOKEN_ID.fullmatch(token_id) is None:
            return None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._worker_token(db, token_id):
                return None
            row = db.execute(
                "SELECT paused, batch, idle_release_minutes, revision FROM worker_settings "
                "WHERE token_id = ?",
                (token_id,),
            ).fetchone()
            revision = row[3] if row else 0
            if row is None or (bool(row[0]), row[1], row[2]) != values:
                revision += 1
                db.execute(
                    "INSERT OR REPLACE INTO worker_settings (token_id, paused, batch, "
                    "idle_release_minutes, revision) VALUES (?, ?, ?, ?, ?)",
                    (token_id, int(values[0]), values[1], values[2], revision),
                )
        return WorkerSettings(*values, revision)

    def record_refusal(self, token_id: str, kind: str, detail: str = "") -> None:
        """Note that the server refused the worker using *token_id*, and why.

        One row per token: the latest refusal replaces the one before.  The
        same refusal again keeps a dismissed notice dismissed, and is written
        at most once a minute.  *detail* is server-made text, cut short.
        """
        if kind not in REFUSAL_KINDS or _TOKEN_ID.fullmatch(token_id) is None:
            return
        detail = "".join(ch if ch.isprintable() else " " for ch in detail)[:_MAX_REFUSAL_DETAIL]
        now = int(self._now())
        with self._touch_lock:
            seen = self._refused.get(token_id)
            if seen is not None and seen[:2] == (kind, detail) and now - seen[2] < 60:
                return
            if len(self._refused) >= 1024:
                self._refused.clear()
            self._refused[token_id] = (kind, detail, now)
        with self._connect() as db:
            if db.execute(
                "SELECT 1 FROM machine_tokens WHERE token_id = ?", (token_id,)
            ).fetchone():
                db.execute(
                    "INSERT INTO worker_refusals (token_id, kind, detail, refused_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(token_id) DO UPDATE SET "
                    "dismissed = CASE WHEN kind = excluded.kind AND detail = excluded.detail "
                    "THEN dismissed ELSE 0 END, kind = excluded.kind, detail = excluded.detail, "
                    "refused_at = excluded.refused_at",
                    (token_id, kind, detail, now),
                )

    def clear_refusal(self, token_id: str) -> None:
        """Forget a worker's refusal, as when it next connects successfully."""
        with self._touch_lock:
            self._refused.pop(token_id, None)
        with self._connect() as db:
            db.execute("DELETE FROM worker_refusals WHERE token_id = ?", (token_id,))

    def dismiss_refusal(self, token_id: str) -> bool:
        """Hide a worker's refusal notice; ``False`` when there is none to hide."""
        with self._connect() as db:
            changed = db.execute(
                "UPDATE worker_refusals SET dismissed = 1 WHERE token_id = ? AND dismissed = 0",
                (token_id,),
            ).rowcount
        return changed == 1

    def refusals(self) -> list[dict[str, object]]:
        """The refusals not dismissed, newest first, with each token's name."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT r.token_id, t.name, r.kind, r.detail, r.refused_at, t.revoked_at "
                "FROM worker_refusals r JOIN machine_tokens t ON t.token_id = r.token_id "
                "WHERE r.dismissed = 0 ORDER BY r.refused_at DESC, r.rowid DESC"
            ).fetchall()
        return [
            {
                "token_id": token_id,
                "name": name,
                "kind": kind,
                "detail": detail,
                "refused_at": refused_at,
                "revoked": revoked_at is not None,
            }
            for token_id, name, kind, detail, refused_at, revoked_at in rows
        ]

    def _touch(self, token_id: str) -> None:
        now = int(self._now())
        with self._touch_lock:
            if now - self._touched.get(token_id, -LAST_USED_RESOLUTION_SECONDS) < (
                LAST_USED_RESOLUTION_SECONDS
            ):
                return
            self._touched[token_id] = now
        with self._connect() as db:
            db.execute(
                "UPDATE machine_tokens SET last_used_at = ? WHERE token_id = ?", (now, token_id)
            )


__all__ = [
    "KNOWN_SCOPES",
    "MAX_IDLE_RELEASE_MINUTES",
    "MAX_WORKER_BATCH",
    "SCOPE_ANALYSIS_WORKER",
    "SCOPE_ANNOUNCER",
    "STATION_SCOPES",
    "MachineToken",
    "MachineTokenStore",
    "WorkerSettings",
    "validate_station",
    "validate_worker_settings",
]
