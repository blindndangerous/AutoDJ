"""Cached Beets metadata used by clockwheel conditions."""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from autodj.beets_paths import PathCheck
from autodj.categories import beets_source

_FIELD = re.compile(r"[a-z_][a-z0-9_]*\Z")
_NO_BEETS = "Configure a Beets database to use Beets field conditions."


class BeetsConditions:
    """Read requested Beets values once per database/configuration fingerprint.

    One instance serves the library: the clockwheel conditions and every
    station's categories read Beets through it (#584).  Each read takes
    every field asked for since the server started, so once the database
    or the library changes, the first reader reads Beets once for all of
    them.
    """

    def __init__(self, cfg: Any, entries: Callable[[], Sequence[Any]] | None = None) -> None:
        # The library's music entries, to match beets' paths against and to
        # find the folder relative beets paths start in (autodj.beets_paths).
        self._cfg = cfg
        self._entries = entries
        self._lock = threading.RLock()
        self._key: tuple[Any, ...] | None = None
        self._cached_entries: Sequence[Any] | None = None
        self._fields: frozenset[str] = frozenset()
        self._discovered = False
        self._loaded: set[str] = set()
        self._items: dict[str, dict[str, Any]] = {}
        self._check: PathCheck | None = None
        # Every valid field asked for or announced so far, kept across reloads.
        self._wanted: set[str] = set()

    @staticmethod
    def _fingerprint(path: Path) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
        def stat(candidate: Path) -> tuple[int, int] | None:
            try:
                value = candidate.stat()
                return value.st_mtime_ns, value.st_size
            except OSError:
                return None

        return stat(path), stat(Path(str(path) + "-wal"))

    @staticmethod
    def _valid(fields: Iterable[str]) -> set[str]:
        return {str(name) for name in fields if _FIELD.fullmatch(str(name))} - {"id", "path"}

    def want(self, fields: Iterable[str]) -> None:
        """Announce *fields* a later reader needs, so the next read takes them too."""
        with self._lock:
            self._wanted |= self._valid(fields)

    def _reset(self) -> None:
        self._fields, self._loaded, self._items = frozenset(), set(), {}
        self._discovered = False
        self._check = None

    def read(
        self, fields: Iterable[str] = (), entries: Sequence[Any] | None = None
    ) -> tuple[frozenset[str], dict[str, dict[str, Any]], PathCheck | None] | None:
        """Beets' field names, the values of *fields* by library path, and the path check.

        ``None`` without a Beets database.  *entries* are the library's
        music entries the values are matched against, the current ones
        when not given; the values are read again when they or the
        database change.  The returned mapping stays the same object
        until then, so caches keyed on it hold.

        Raises:
            OSError, sqlite3.Error: The database could not be read; the
                next call tries again.
        """
        with self._lock:
            library = getattr(self._cfg, "library", None)
            db = getattr(library, "beets_db", None) if library is not None else None
            source = beets_source(self._cfg) if db else None
            if source is None:
                self._key = None
                self._cached_entries = None
                self._reset()
                return None

            db_path = Path(str(db))
            music_dir = getattr(library, "music_dir", None)
            if entries is None and self._entries is not None:
                entries = self._entries()
            key = (
                str(db_path),
                str(music_dir) if music_dir is not None else None,
                *self._fingerprint(db_path),
            )
            if key != self._key or entries is not self._cached_entries:
                self._key = key
                self._cached_entries = entries
                self._reset()

            requested = self._valid(fields)
            self._wanted |= requested
            read_fields = set(self._wanted)
            if "original_year" in read_fields:
                read_fields.add("year")
            try:
                if not self._discovered:
                    self._fields = frozenset(
                        name
                        for name in source.fields()
                        if _FIELD.fullmatch(name) and name not in {"id", "path"}
                    )
                    self._discovered = True
                missing = read_fields - self._loaded
                if missing:
                    if entries is None:
                        fresh, check = source.items(missing), None
                    else:
                        fresh, check = source.checked_items(
                            missing,
                            [e.path for e in entries if not getattr(e, "is_silent", False)],
                        )
                    if self._items:
                        merged = {path: dict(values) for path, values in self._items.items()}
                        for path, values in fresh.items():
                            merged.setdefault(path, {}).update(values)
                        self._items = merged
                    else:
                        self._items = fresh
                    self._loaded.update(missing)
                    if check is not None:
                        self._check = check
                return self._fields, self._items, self._check
            except (OSError, sqlite3.Error):
                self._key = None  # Failed reads are retried; stale values never escape.
                self._reset()
                raise

    def snapshot(
        self, fields: Iterable[str] = ()
    ) -> tuple[frozenset[str], dict[str, dict[str, Any]], str | None]:
        """Like :meth:`read`, with a failure or a missing database said in words."""
        try:
            found = self.read(fields)
        except (OSError, sqlite3.Error) as exc:
            return frozenset(), {}, f"Could not read Beets database: {exc}"
        if found is None:
            return frozenset(), {}, _NO_BEETS
        return found[0], found[1], None
