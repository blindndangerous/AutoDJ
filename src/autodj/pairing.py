"""Persistent identities for browsers paired with one AutoDJ server."""

from __future__ import annotations

import math
import re
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_DEVICE_ID = re.compile(r"[0-9a-f]{32}\Z")
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

    def _timestamp(self) -> int:
        """Return validated whole seconds from configured wall clock."""
        value = self._now()
        if type(value) not in {int, float} or not math.isfinite(float(value)):
            raise ValueError("device registry clock returned an invalid value")
        timestamp = int(value)
        if timestamp < 0:
            raise ValueError("device registry clock returned an invalid value")
        return timestamp

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
        timestamp = self._timestamp()
        device_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO paired_devices VALUES (?, ?, ?, ?, NULL)",
                (device_id, normalized, timestamp, timestamp),
            )
        return PairedDevice(device_id, normalized, timestamp, timestamp, None)

    def is_active(self, device_id: str) -> bool:
        """Return whether device exists and has not been revoked."""
        if not isinstance(device_id, str) or _DEVICE_ID.fullmatch(device_id) is None:
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT revoked_at FROM paired_devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        return row is not None and row[0] is None

    def touch(self, device_id: str) -> bool:
        """Record recent use for an active paired device."""
        if not self.is_active(device_id):
            return False
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE paired_devices SET last_seen_at = ? "
                "WHERE device_id = ? AND revoked_at IS NULL",
                (self._timestamp(), device_id),
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

    def revoke(self, device_id: str) -> bool:
        """Revoke one device and report whether active state changed."""
        if not isinstance(device_id, str) or _DEVICE_ID.fullmatch(device_id) is None:
            return False
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE paired_devices SET revoked_at = ? "
                "WHERE device_id = ? AND revoked_at IS NULL",
                (self._timestamp(), device_id),
            ).rowcount
        return changed == 1

    def reset(self) -> int:
        """Revoke every active device and return number changed."""
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE paired_devices SET revoked_at = ? WHERE revoked_at IS NULL",
                (self._timestamp(),),
            ).rowcount
        return changed
