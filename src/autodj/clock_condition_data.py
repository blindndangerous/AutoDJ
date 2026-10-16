"""Cached Beets metadata used by clockwheel conditions."""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from autodj.categories import beets_source

_FIELD = re.compile(r"[a-z_][a-z0-9_]*\Z")
_NO_BEETS = "Configure a Beets database to use Beets field conditions."


class BeetsConditions:
    """Read requested Beets values once per database/configuration fingerprint."""

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

    @staticmethod
    def _fingerprint(path: Path) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
        def stat(candidate: Path) -> tuple[int, int] | None:
            try:
                value = candidate.stat()
                return value.st_mtime_ns, value.st_size
            except OSError:
                return None

        return stat(path), stat(Path(str(path) + "-wal"))

    def snapshot(
        self, fields: Iterable[str] = ()
    ) -> tuple[frozenset[str], dict[str, dict[str, Any]], str | None]:
        with self._lock:
            library = getattr(self._cfg, "library", None)
            db = getattr(library, "beets_db", None) if library is not None else None
            if not db:
                self._key = None
                self._cached_entries = None
                self._fields, self._loaded, self._items = frozenset(), set(), {}
                self._discovered = False
                return self._fields, {}, _NO_BEETS

            db_path = Path(db)
            music_dir = getattr(library, "music_dir", None)
            entries = self._entries() if self._entries is not None else None
            key = (
                str(db_path),
                str(music_dir) if music_dir is not None else None,
                *self._fingerprint(db_path),
            )
            if key != self._key or entries is not self._cached_entries:
                self._key = key
                self._cached_entries = entries
                self._fields, self._loaded, self._items = frozenset(), set(), {}
                self._discovered = False

            requested = {str(name) for name in fields if _FIELD.fullmatch(str(name))} - {
                "id",
                "path",
            }
            read_fields = requested | ({"year"} if "original_year" in requested else set())
            try:
                source = beets_source(self._cfg)
                if source is None:
                    return frozenset(), {}, _NO_BEETS
                if not self._discovered:
                    self._fields = frozenset(
                        name
                        for name in source.fields()
                        if _FIELD.fullmatch(name) and name not in {"id", "path"}
                    )
                    self._discovered = True
                missing = read_fields - self._loaded
                if missing:
                    fresh = (
                        source.items(missing)
                        if entries is None
                        else source.checked_items(
                            missing,
                            [e.path for e in entries if not getattr(e, "is_silent", False)],
                        )[0]
                    )
                    merged = {path: dict(values) for path, values in self._items.items()}
                    for path, values in fresh.items():
                        merged.setdefault(path, {}).update(values)
                    self._items = merged
                    self._loaded.update(missing)
                return self._fields, self._items, None
            except (OSError, sqlite3.Error) as exc:
                self._key = None  # Failed reads are retried; stale values never escape.
                self._fields, self._loaded, self._items = frozenset(), set(), {}
                self._discovered = False
                return frozenset(), {}, f"Could not read Beets database: {exc}"
