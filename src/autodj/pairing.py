"""Persistent identities for browsers paired with one AutoDJ server."""

from __future__ import annotations

import re
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DEVICE_ID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_DEVICE_NAME = 64


@dataclass(frozen=True)
class PairedDevice:
    """One browser authorized to control an AutoDJ instance."""

    device_id: str
    name: str
    paired_at: int
    last_seen_at: int
    revoked_at: int | None


class DeviceRegistry:
    """Store paired browser identities in a small transactional SQLite file."""

    def __init__(self, path: Path, *, now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._now = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

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
        with self._connect() as connection:
            row = connection.execute(
                "SELECT revoked_at FROM paired_devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        return row is not None and row[0] is None

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
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE paired_devices SET revoked_at = ? "
                "WHERE device_id = ? AND revoked_at IS NULL",
                (int(self._now()), device_id),
            ).rowcount
        return changed == 1

    def reset(self) -> int:
        """Revoke every active device and return number changed."""
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE paired_devices SET revoked_at = ? WHERE revoked_at IS NULL",
                (int(self._now()),),
            ).rowcount
        return changed
