"""Named, scoped bearer tokens for machines that talk to AutoDJ without a browser.

A machine token lets a program use one narrow part of the API: an
analysis worker (``autodj worker``) on another computer, or an announcer
program (radiodj) that sends DJ breaks, news and weather to one station
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


def token_id_of(token: str) -> str | None:
    """The id part of a well-formed token, or ``None``."""
    match = _TOKEN.fullmatch(token) if isinstance(token, str) else None
    return match.group(1) if match else None


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


class MachineTokenStore:
    """Create, list, check and revoke machine tokens in a small SQLite file."""

    def __init__(self, path: Path, *, now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._now = now
        self._touched: dict[str, int] = {}
        self._touch_lock = threading.Lock()
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
                db.execute("ALTER TABLE machine_tokens ADD COLUMN station TEXT")

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
        from a revoked token or a missing scope.
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
            return None
        found = self._token(row[:7])
        if not found.active or scope not in found.scopes:
            return None
        self._touch(token_id)
        return found

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
    "SCOPE_ANALYSIS_WORKER",
    "SCOPE_ANNOUNCER",
    "STATION_SCOPES",
    "MachineToken",
    "MachineTokenStore",
    "validate_station",
]
