"""Persistent identities for browsers paired with one AutoDJ server."""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DEVICE_ID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_DEVICE_NAME = 64
# How long a session check may reuse the answer for a device.  Every page
# checks its session once a second, and each check opened the database.  A
# revoke in this process forgets the answers at once; one from another
# process (``autodj devices revoke``) changes the file, which does too.
ACTIVE_CACHE_SECONDS = 10.0
_ACTIVE_CACHE_LIMIT = 256


@dataclass(frozen=True)
class PairedDevice:
    """One browser authorized to control an AutoDJ instance."""

    device_id: str
    name: str
    paired_at: int
    last_seen_at: int
    revoked_at: int | None


@dataclass(frozen=True)
class SSOBrowser:
    """One user's browser inventory record; it grants no native authorization."""

    device_id: str
    user_id: str
    name: str
    paired_at: int
    last_seen_at: int


class DeviceRegistry:
    """Store paired browser identities in a small transactional SQLite file."""

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = Path(path)
        self._now = now
        self._monotonic = monotonic
        # device_id -> (active, valid until (monotonic), file signature)
        self._active: dict[str, tuple[bool, float, tuple[int, int] | None]] = {}
        self._active_generation = 0
        self._active_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _signature(self) -> tuple[int, int] | None:
        """The registry file's modification time and size: a write changes it."""
        try:
            stat = os.stat(self.path)
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _forget_active(self) -> None:
        """Drop every cached answer; call after any change to who is active."""
        with self._active_lock:
            self._active.clear()
            self._active_generation += 1

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open one bounded registry transaction connection."""
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        """Create registry schema when this instance has no registry yet."""
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paired_devices (
                    device_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    paired_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    revoked_at INTEGER
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sso_browsers (
                    device_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    paired_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL
                )
                """
            )
            # At most one open pairing request: the time its code stops working.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pairing_request (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    open_until INTEGER NOT NULL
                )
                """
            )

    @staticmethod
    def _name(value: str) -> str:
        """Validate and normalize an operator-visible device name."""
        if not isinstance(value, str):
            raise ValueError("device name must be text")
        name = value.strip()
        if (
            not name
            or len(name) > _MAX_DEVICE_NAME
            or any(
                not character.isprintable() or unicodedata.category(character) in {"Zl", "Zp"}
                for character in name
            )
        ):
            raise ValueError("device name must contain 1 to 64 printable characters")
        return name

    def register_sso(
        self, user_id: str, user_name: str, browser_id: str | None = None
    ) -> SSOBrowser:
        """Remember a browser for its owner, preserving an existing custom label."""
        if (
            not isinstance(user_id, str)
            or not user_id.strip()
            or len(user_id) > 256
            or not user_id.isascii()
            or not user_id.isprintable()
        ):
            raise ValueError("SSO user ID must contain 1 to 256 printable ASCII characters")
        name = self._name((user_name or user_id)[:_MAX_DEVICE_NAME])
        timestamp = int(self._now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT device_id, user_id, name, paired_at, last_seen_at FROM sso_browsers "
                "WHERE device_id = ? AND user_id = ?",
                (browser_id, user_id),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE sso_browsers SET last_seen_at = ? WHERE device_id = ? AND user_id = ?",
                    (timestamp, browser_id, user_id),
                )
                return SSOBrowser(row[0], row[1], row[2], row[3], timestamp)
            device_id = uuid.uuid4().hex
            # Keep inventory bounded per owner, in the same transaction as insertion.
            connection.execute(
                "DELETE FROM sso_browsers WHERE device_id IN ("
                "SELECT device_id FROM sso_browsers WHERE user_id = ? "
                "ORDER BY last_seen_at DESC, rowid DESC LIMIT -1 OFFSET 19)",
                (user_id,),
            )
            connection.execute(
                "INSERT INTO sso_browsers VALUES (?, ?, ?, ?, ?)",
                (device_id, user_id, name, timestamp, timestamp),
            )
        return SSOBrowser(device_id, user_id, name, timestamp, timestamp)

    def sso_devices(self, user_id: str) -> list[SSOBrowser]:
        """List only this user's SSO browser inventory in creation order."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT device_id, user_id, name, paired_at, last_seen_at FROM sso_browsers "
                "WHERE user_id = ? ORDER BY paired_at, rowid",
                (user_id,),
            ).fetchall()
        return [SSOBrowser(*row) for row in rows]

    def rename_sso(self, user_id: str, device_id: str, name: str) -> str | None:
        """Rename this owner's browser without touching native authorization."""
        name = self._name(name)
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE sso_browsers SET name = ? WHERE device_id = ? AND user_id = ?",
                (name, device_id, user_id),
            ).rowcount
        return name if changed == 1 else None

    def forget_sso(self, user_id: str, device_id: str) -> bool:
        """Forget only this owner's SSO browser record."""
        with self._connect() as connection:
            changed = connection.execute(
                "DELETE FROM sso_browsers WHERE device_id = ? AND user_id = ?",
                (device_id, user_id),
            ).rowcount
        return changed == 1

    def pair(self, name: str) -> PairedDevice:
        """Create and persist a distinct authorized browser identity."""
        normalized = self._name(name)
        timestamp = int(self._now())
        device_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO paired_devices VALUES (?, ?, ?, ?, NULL)",
                (device_id, normalized, timestamp, timestamp),
            )
        self._forget_active()
        return PairedDevice(device_id, normalized, timestamp, timestamp, None)

    def open_pairing(self, seconds: int) -> None:
        """Let one device pair in the next *seconds*, replacing any open request.

        Pairing codes are derived from the server secret and the clock, so
        one always exists; this is what makes it usable.  The server and
        ``autodj devices pairing-code`` share this file, so a code requested
        from either one works on the running server.
        """
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO pairing_request (id, open_until) VALUES (1, ?)",
                (int(self._now()) + seconds,),
            )

    def pair_on_request(self, name: str) -> PairedDevice | None:
        """Pair a device if a pairing request is open, closing the request.

        Returns ``None`` when no request is open or it has expired.  Taking
        the request and adding the device are one transaction, so two
        browsers racing with one code cannot both pair.

        Raises:
            ValueError: *name* is not 1 to 64 printable characters.  The
                request stays open.
        """
        normalized = self._name(name)
        timestamp = int(self._now())
        device_id = uuid.uuid4().hex
        with self._connect() as connection:
            taken = connection.execute(
                "DELETE FROM pairing_request WHERE id = 1 AND open_until >= ?",
                (timestamp,),
            ).rowcount
            if taken != 1:
                return None
            connection.execute(
                "INSERT INTO paired_devices VALUES (?, ?, ?, ?, NULL)",
                (device_id, normalized, timestamp, timestamp),
            )
        self._forget_active()
        return PairedDevice(device_id, normalized, timestamp, timestamp, None)

    def has_active_devices(self) -> bool:
        """Return whether any device is paired and not revoked."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM paired_devices WHERE revoked_at IS NULL LIMIT 1"
            ).fetchone()
        return row is not None

    def is_active(self, device_id: str) -> bool:
        """Return whether device exists and has not been revoked."""
        if not isinstance(device_id, str) or DEVICE_ID.fullmatch(device_id) is None:
            return False
        signature = self._signature()
        now = self._monotonic()
        with self._active_lock:
            cached = self._active.get(device_id)
            if cached is not None and cached[1] > now and cached[2] == signature:
                return cached[0]
            generation = self._active_generation
        with self._connect() as connection:
            row = connection.execute(
                "SELECT revoked_at FROM paired_devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        active = row is not None and row[0] is None
        with self._active_lock:
            # A revoke while this read ran makes its answer unsafe to keep.
            if generation == self._active_generation:
                if len(self._active) >= _ACTIVE_CACHE_LIMIT:
                    self._active.clear()
                self._active[device_id] = (active, now + ACTIVE_CACHE_SECONDS, signature)
        return active

    def touch(self, device_id: str) -> bool:
        """Record recent use for an active paired device."""
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE paired_devices SET last_seen_at = ? "
                "WHERE device_id = ? AND revoked_at IS NULL",
                (int(self._now()), device_id),
            ).rowcount
        return changed == 1

    def list_devices(self) -> list[PairedDevice]:
        """Return all paired devices in stable creation order."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT device_id, name, paired_at, last_seen_at, revoked_at "
                "FROM paired_devices ORDER BY paired_at, rowid"
            ).fetchall()
        return [PairedDevice(*row) for row in rows]

    def rename(self, device_id: str, name: str) -> str | None:
        """Rename one active device and return its stored name, or ``None`` if not paired.

        Raises:
            ValueError: *name* is not 1 to 64 printable characters.
        """
        normalized = self._name(name)
        if not isinstance(device_id, str) or DEVICE_ID.fullmatch(device_id) is None:
            return None
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE paired_devices SET name = ? WHERE device_id = ? AND revoked_at IS NULL",
                (normalized, device_id),
            ).rowcount
        return normalized if changed == 1 else None

    def revoke(self, device_id: str) -> bool:
        """Revoke one device and report whether active state changed."""
        if not isinstance(device_id, str) or DEVICE_ID.fullmatch(device_id) is None:
            return False
        try:
            with self._connect() as connection:
                changed = connection.execute(
                    "UPDATE paired_devices SET revoked_at = ? "
                    "WHERE device_id = ? AND revoked_at IS NULL",
                    (int(self._now()), device_id),
                ).rowcount
        finally:
            self._forget_active()
        return changed == 1

    def reset(self) -> int:
        """Revoke every active device and return number changed."""
        try:
            with self._connect() as connection:
                changed = connection.execute(
                    "UPDATE paired_devices SET revoked_at = ? WHERE revoked_at IS NULL",
                    (int(self._now()),),
                ).rowcount
        finally:
            self._forget_active()
        return changed
