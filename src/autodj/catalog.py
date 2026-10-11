"""Persistent, playable library records independent of the FAISS index."""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from autodj.indexer import IndexEntry, file_fingerprint

_ENTRY_FIELDS = (
    "title",
    "artist",
    "album",
    "genre",
    "bpm",
    "year",
    "length",
    "energy",
    "key",
    "mode",
    "tempo_confidence",
    "embedded_at",
    "size",
    "fingerprint",
)

logger = logging.getLogger(__name__)
_POINTS = ("start", "intro", "outro", "segue", "end", "overlap_start")
_POINT_SOURCES = {"auto", "lyrics", "analysis", "user", "imported"}


_SCHEMA = """
    CREATE TABLE IF NOT EXISTS catalog_sources (
        id TEXT PRIMARY KEY, path TEXT NOT NULL, kind TEXT NOT NULL,
        recursive INTEGER NOT NULL, advanced INTEGER NOT NULL,
        UNIQUE(kind, path)
    );
    CREATE TABLE IF NOT EXISTS catalog_entries (
        id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
        allow_outro INTEGER NOT NULL DEFAULT 0,
        title TEXT NOT NULL DEFAULT '', artist TEXT NOT NULL DEFAULT '',
        album TEXT NOT NULL DEFAULT '', genre TEXT NOT NULL DEFAULT '',
        bpm REAL NOT NULL DEFAULT 0, year INTEGER NOT NULL DEFAULT 0,
        length REAL NOT NULL DEFAULT 0, energy REAL NOT NULL DEFAULT 0,
        key INTEGER NOT NULL DEFAULT -1, mode INTEGER NOT NULL DEFAULT -1,
        tempo_confidence REAL NOT NULL DEFAULT 0,
        embedded_at REAL NOT NULL DEFAULT 0, size INTEGER NOT NULL DEFAULT 0,
        fingerprint TEXT NOT NULL DEFAULT '', points TEXT NOT NULL DEFAULT '{}',
        point_sources TEXT NOT NULL DEFAULT '{}', prepared_path TEXT,
        last_played REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS catalog_settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS play_history (
        event_id TEXT PRIMARY KEY NOT NULL, entry_id TEXT, path TEXT,
        title TEXT NOT NULL, artist TEXT NOT NULL, duration REAL NOT NULL,
        played_at TEXT NOT NULL, interrupted INTEGER NOT NULL DEFAULT 0,
        performances INTEGER
    );
    CREATE TABLE IF NOT EXISTS catalog_vectors (
        entry_id TEXT PRIMARY KEY REFERENCES catalog_entries(id) ON DELETE CASCADE,
        vector BLOB NOT NULL
    );
"""


def _path(path: str | Path) -> str:
    value = os.path.abspath(os.path.expanduser(os.fspath(path)))
    value = os.path.normpath(value)
    return os.path.normcase(value) if os.name == "nt" else value


def _under(path: str, root: str, recursive: bool) -> bool:
    try:
        relative = os.path.relpath(path, root)
    except ValueError:
        return False
    if relative == os.curdir:
        return False
    if relative == os.pardir or relative.startswith(os.pardir + os.sep) or os.path.isabs(relative):
        return False
    return recursive or os.sep not in relative


class LibraryCatalog:
    """SQLite source registry and playable track catalog for one library."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.executescript(_SCHEMA)
            self._add_missing_columns()
            self._migrate_legacy_history()
        except BaseException:
            # Not a catalog (or unreadable): do not leave the file open.
            self._conn.close()
            self._conn = None
            raise

    def _add_missing_columns(self) -> None:
        """Add columns an older catalog file lacks.

        These are the liner ``allow_outro`` flag and the play log's
        ``performances`` count.  The checks and the changes share one write transaction, so a second
        process opening the same old file waits, then finds the columns.
        """
        db = self._db()
        db.execute("BEGIN IMMEDIATE")
        try:
            entries = {row[1] for row in db.execute("PRAGMA table_info(catalog_entries)")}
            if "allow_outro" not in entries:
                db.execute(
                    "ALTER TABLE catalog_entries ADD COLUMN allow_outro INTEGER NOT NULL DEFAULT 0"
                )
            have = {row[1] for row in db.execute("PRAGMA table_info(play_history)")}
            if "performances" not in have:
                db.execute("ALTER TABLE play_history ADD COLUMN performances INTEGER")
            db.commit()
        except BaseException:
            db.rollback()
            raise

    @staticmethod
    def _count(value: Any) -> int | None:
        """Return a performance count, or ``None`` when *value* is not one."""
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    @staticmethod
    def _valid_played_at(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return True

    @staticmethod
    def _duration(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        try:
            value = float(value)
        except OverflowError:
            return None
        return value if math.isfinite(value) and value >= 0 else None

    def _migrate_legacy_history(self) -> None:
        db = self._db()
        with self._lock:
            try:
                db.execute("BEGIN IMMEDIATE")
                if db.execute(
                    "SELECT 1 FROM catalog_settings WHERE key='play_history_migrated'"
                ).fetchone():
                    db.commit()
                    return
                legacy = db.execute(
                    "SELECT value FROM catalog_settings WHERE key='play_history'"
                ).fetchone()
                if legacy is None:
                    db.commit()
                    return
                try:
                    rows = json.loads(legacy[0])
                except (TypeError, json.JSONDecodeError):
                    rows = []
                if isinstance(rows, list):
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        duration = self._duration(row.get("duration"))
                        if (
                            not isinstance(row.get("title"), str)
                            or not isinstance(row.get("artist"), str)
                            or duration is None
                            or not self._valid_played_at(row.get("played_at"))
                        ):
                            continue
                        db.execute(
                            "INSERT INTO play_history "
                            "(event_id,title,artist,duration,played_at) VALUES (?,?,?,?,?)",
                            (
                                uuid.uuid4().hex,
                                row["title"],
                                row["artist"],
                                duration,
                                row["played_at"],
                            ),
                        )
                db.execute("INSERT INTO catalog_settings VALUES ('play_history_migrated','true')")
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def write_history(self, rows: Sequence[dict], interrupted: Sequence[str] = ()) -> None:
        """Append unique play events and mark any supplied events interrupted.

        A row for an event already stored only raises its ``performances``,
        which grows as listeners join during the play.
        """
        clean_rows = []
        for row in rows:
            event_id = row.get("event_id") if isinstance(row, dict) else None
            path = row.get("path") if isinstance(row, dict) else None
            duration = self._duration(row.get("duration")) if isinstance(row, dict) else None
            if (
                not isinstance(event_id, str)
                or not event_id
                or not isinstance(path, str)
                or not path
                or not isinstance(row.get("title"), str)
                or not isinstance(row.get("artist"), str)
                or duration is None
                or not self._valid_played_at(row.get("played_at"))
            ):
                continue
            normalized_path = _path(path)
            clean_rows.append(
                (
                    event_id,
                    normalized_path,
                    normalized_path,
                    row["title"],
                    row["artist"],
                    duration,
                    row["played_at"],
                    self._count(row.get("performances")),
                )
            )
        interrupted = tuple(
            event_id for event_id in interrupted if isinstance(event_id, str) and event_id
        )
        with self._lock:
            db = self._db()
            try:
                db.execute("BEGIN")
                db.executemany(
                    "INSERT INTO play_history (event_id,entry_id,path,title,artist,duration,"
                    "played_at,performances) "
                    "VALUES (?,(SELECT id FROM catalog_entries WHERE path=?),?,?,?,?,?,?) "
                    # A play is written again when a listener joins during it.
                    "ON CONFLICT(event_id) DO UPDATE SET performances="
                    "max(COALESCE(performances,0),excluded.performances) "
                    "WHERE excluded.performances IS NOT NULL",
                    clean_rows,
                )
                db.executemany(
                    "UPDATE play_history SET interrupted=1 WHERE event_id=?",
                    ((event_id,) for event_id in dict.fromkeys(interrupted)),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def recent_history(self, limit: int = 500) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        with self._lock:
            rows = (
                self._db()
                .execute(
                    "SELECT event_id,entry_id,path,title,artist,duration,played_at FROM ("
                    "SELECT rowid,event_id,entry_id,path,title,artist,duration,played_at "
                    "FROM play_history WHERE interrupted=0 ORDER BY rowid DESC LIMIT ?) "
                    "ORDER BY rowid ASC",
                    (limit,),
                )
                .fetchall()
            )
            return [dict(row) for row in rows]

    def recent_plays(self, limit: int) -> list[dict]:
        """The latest *limit* plays that started, oldest first, with each track's album.

        For the repeat rules.  A play a set stop cut short (``interrupted``)
        is left out, as in :meth:`recent_history`, and so is a liner.  The
        album comes from the catalog entry, because play rows do not store
        it; a track no longer in the catalog has an empty album.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        with self._lock:
            rows = (
                self._db()
                .execute(
                    "SELECT path,title,artist,album,played_at FROM ("
                    "SELECT h.rowid AS row,h.path,h.title,h.artist,"
                    "COALESCE(e.album,'') AS album,h.played_at "
                    "FROM play_history h LEFT JOIN catalog_entries e ON e.path=h.path "
                    "WHERE h.interrupted=0 AND h.path IS NOT NULL "
                    "AND (e.kind IS NULL OR e.kind='music') "
                    "ORDER BY h.rowid DESC LIMIT ?) ORDER BY row ASC",
                    (limit,),
                )
                .fetchall()
            )
            return [dict(row) for row in rows]

    def play_log(self) -> list[dict]:
        """Return every completed play, oldest first, with its record's details.

        ``path``, ``album`` and ``kind`` come from the catalog record the
        play refers to (by id, so a moved file gives its new path), or from
        the record at the played path.  ``album`` and ``kind`` are ``None``
        when neither exists, as for plays migrated from the old
        settings-based history, which also have no path.  ``performances``
        is how many listeners heard the play (see :mod:`autodj.playlog`), or
        ``None`` for plays logged before AutoDJ counted them.
        """
        with self._lock:
            rows = (
                self._db()
                .execute(
                    "SELECT h.played_at,h.duration,h.title,h.artist,"
                    "COALESCE(e.path,h.path) AS path,e.album,e.kind,"
                    "h.performances "
                    "FROM play_history h LEFT JOIN catalog_entries e ON e.id="
                    "COALESCE(h.entry_id,(SELECT id FROM catalog_entries WHERE path=h.path)) "
                    "WHERE h.interrupted=0 ORDER BY h.rowid"
                )
                .fetchall()
            )
            return [dict(row) for row in rows]

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("catalog is closed")
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> LibraryCatalog:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def seed_sources(self, music_root: Path | str, liner_root: Path | str) -> bool:
        """Import legacy config roots once; an intentional empty list stays empty."""
        with self._lock:
            self._db()
            if self.get_setting("sources_seeded", False):
                return False
            for root, kind, recursive, advanced in (
                (music_root, "music", True, True),
                (liner_root, "liner", False, False),
            ):
                if root:
                    self._add_source(root, kind, recursive, advanced, validate=False)
            self.set_setting("sources_seeded", True)
            return True

    def _add_source(
        self, path: Path | str, kind: str, recursive: bool, advanced: bool, *, validate: bool = True
    ) -> dict:
        if kind not in {"music", "liner"}:
            raise ValueError("kind must be 'music' or 'liner'")
        normalized = _path(path)
        if validate and not Path(normalized).is_dir():
            raise ValueError(f"source directory does not exist: {path}")
        db = self._db()
        row = db.execute(
            "SELECT * FROM catalog_sources WHERE kind=? AND path=?", (kind, normalized)
        ).fetchone()
        if row:
            db.execute(
                "UPDATE catalog_sources SET recursive=?, advanced=? WHERE id=?",
                (int(recursive), int(advanced), row["id"]),
            )
            sid = row["id"]
        else:
            sid = uuid.uuid4().hex
            db.execute(
                "INSERT INTO catalog_sources VALUES (?, ?, ?, ?, ?)",
                (sid, normalized, kind, int(recursive), int(advanced)),
            )
        db.commit()
        return {
            "id": sid,
            "path": normalized,
            "kind": kind,
            "recursive": bool(recursive),
            "advanced": bool(advanced),
        }

    def add_source(
        self, path: Path | str, kind: str = "music", recursive: bool = True, advanced: bool = True
    ) -> dict:
        with self._lock:
            return self._add_source(path, kind, recursive, advanced)

    def sources(self, kind: str | None = None) -> list[dict]:
        with self._lock:
            sql = "SELECT * FROM catalog_sources"
            args: tuple[str, ...] = ()
            if kind is not None:
                sql, args = sql + " WHERE kind=?", (kind,)
            rows = self._db().execute(sql + " ORDER BY path", args).fetchall()
            return [
                {
                    "id": r["id"],
                    "path": r["path"],
                    "kind": r["kind"],
                    "recursive": bool(r["recursive"]),
                    "advanced": bool(r["advanced"]),
                }
                for r in rows
            ]

    def update_source(self, source_id: str, recursive: bool, advanced: bool) -> bool:
        with self._lock:
            cur = self._db().execute(
                "UPDATE catalog_sources SET recursive=?, advanced=? WHERE id=?",
                (int(recursive), int(advanced), source_id),
            )
            self._db().commit()
            return cur.rowcount > 0

    def remove_source(self, source_id: str) -> bool:
        with self._lock:
            cur = self._db().execute("DELETE FROM catalog_sources WHERE id=?", (source_id,))
            self._db().commit()
            return cur.rowcount > 0

    def _put(
        self,
        entry: IndexEntry,
        kind: str,
        points: dict | None,
        point_source: str,
        prepared_path: str | None,
    ) -> str:
        if kind not in {"music", "liner"}:
            raise ValueError("kind must be 'music' or 'liner'")
        if point_source not in _POINT_SOURCES:
            raise ValueError("invalid point source")
        path = _path(entry.path)
        row = self._db().execute("SELECT * FROM catalog_entries WHERE path=?", (path,)).fetchone()
        record_id = row["id"] if row else uuid.uuid4().hex
        values = [getattr(entry, name) for name in _ENTRY_FIELDS]
        if not entry.size or not entry.fingerprint:
            size, fingerprint = file_fingerprint(path)
            values[_ENTRY_FIELDS.index("size")] = entry.size or size
            values[_ENTRY_FIELDS.index("fingerprint")] = entry.fingerprint or fingerprint
        if row:
            old_identity = (row["size"], row["fingerprint"])
            new_identity = (
                values[_ENTRY_FIELDS.index("size")],
                values[_ENTRY_FIELDS.index("fingerprint")],
            )
            if all(old_identity) and all(new_identity) and old_identity != new_identity:
                # Changed bytes, including retagging, invalidate cues and derived audio too.
                self._db().execute("DELETE FROM catalog_vectors WHERE entry_id=?", (record_id,))
                self._db().execute(
                    "UPDATE catalog_entries SET prepared_path=NULL, points='{}', point_sources='{}' "
                    "WHERE id=?",
                    (record_id,),
                )
        cols = ",".join(_ENTRY_FIELDS)
        placeholders = ",".join("?" for _ in _ENTRY_FIELDS)
        if row:
            self._db().execute(
                f"UPDATE catalog_entries SET kind=?, {','.join(f'{n}=?' for n in _ENTRY_FIELDS)} WHERE path=?",  # nosec B608 -- constant field names; values are bound
                (kind, *values, path),
            )
            if kind != "liner":
                self._db().execute("UPDATE catalog_entries SET allow_outro=0 WHERE path=?", (path,))
        else:
            self._db().execute(
                f"INSERT INTO catalog_entries (id,path,kind,{cols}) VALUES (?,?,?,{placeholders})",  # nosec B608 -- constant field names; values are bound
                (record_id, path, kind, *values),
            )
        if prepared_path is not None:
            self._db().execute(
                "UPDATE catalog_entries SET prepared_path=? WHERE path=?",
                (_path(prepared_path), path),
            )
        self._merge_points(path, points or {}, point_source)
        return record_id

    def put_entry(
        self,
        entry: IndexEntry,
        kind: str = "music",
        points: dict | None = None,
        point_source: str = "auto",
        prepared_path: str | None = None,
    ) -> str:
        with self._lock:
            try:
                record_id = self._put(entry, kind, points, point_source, prepared_path)
                self._db().commit()
                return record_id
            except BaseException:
                self._db().rollback()
                raise

    def seed_entries(self, entries: Iterable[IndexEntry]) -> int:
        """Import existing indexed records without replacing catalog metadata."""
        with self._lock:
            count = 0
            for entry in entries:
                path = _path(entry.path)
                exists = (
                    self._db()
                    .execute("SELECT 1 FROM catalog_entries WHERE path=?", (path,))
                    .fetchone()
                )
                if not exists:
                    self._put(entry, "music", None, "auto", None)
                    count += 1
            self._db().commit()
            return count

    def set_vector(self, path: str | Path, vector: np.ndarray) -> None:
        from autodj.indexer import FEATURE_DIM

        value = np.asarray(vector, dtype=np.float32)
        if value.shape != (FEATURE_DIM,) or not np.isfinite(value).all():
            raise ValueError(f"vector must be a finite float32 array of shape ({FEATURE_DIM},)")
        with self._lock:
            row = (
                self._db()
                .execute("SELECT id FROM catalog_entries WHERE path=?", (_path(path),))
                .fetchone()
            )
            if row is None:
                raise KeyError(_path(path))
            self._db().execute(
                "INSERT INTO catalog_vectors VALUES (?,?) ON CONFLICT(entry_id) "
                "DO UPDATE SET vector=excluded.vector",
                (row["id"], value.tobytes()),
            )
            self._db().commit()

    def seed_vectors(self, entries: Sequence[IndexEntry], vectors: np.ndarray) -> int:
        from autodj.indexer import FEATURE_DIM

        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.shape != (len(entries), FEATURE_DIM) or not np.isfinite(matrix).all():
            raise ValueError(f"vectors must be finite with shape ({len(entries)}, {FEATURE_DIM})")
        with self._lock:
            db, count = self._db(), 0
            for entry, vector in zip(entries, matrix, strict=True):
                row = db.execute(
                    "SELECT id FROM catalog_entries WHERE path=?", (_path(entry.path),)
                ).fetchone()
                if row is None:
                    continue
                exists = db.execute(
                    "SELECT 1 FROM catalog_vectors WHERE entry_id=?", (row["id"],)
                ).fetchone()
                if not exists:
                    db.execute(
                        "INSERT INTO catalog_vectors VALUES (?,?)", (row["id"], vector.tobytes())
                    )
                    count += 1
            db.commit()
            return count

    def vectors_snapshot(self) -> tuple[list[IndexEntry], np.ndarray]:
        from autodj.indexer import FEATURE_DIM

        with self._lock:
            rows = (
                self._db()
                .execute(
                    "SELECT e.*, v.vector FROM catalog_entries e "
                    "JOIN catalog_vectors v ON v.entry_id=e.id WHERE e.kind='music' ORDER BY e.path"
                )
                .fetchall()
            )
            rows = [row for row in rows if self._active(row["path"], "music")]
            entries = [self._entry(row) for row in rows]
            vectors = np.empty((len(rows), FEATURE_DIM), dtype=np.float32)
            for i, row in enumerate(rows):
                vectors[i] = np.frombuffer(row["vector"], dtype=np.float32)
            return entries, vectors

    def _coverage(self, path: str, kind: str) -> bool:
        return any(
            _under(path, s["path"], s["recursive"])
            for s in self._db().execute(
                "SELECT path,recursive FROM catalog_sources WHERE kind=?", (kind,)
            )
        )

    def _active(self, path: str, kind: str) -> bool:
        if not Path(path).is_file() or not self._coverage(path, kind):
            return False
        # Registered liner roots own their files even when nested under music.
        if kind == "liner":
            return True
        return not self._coverage(path, "liner")

    def entries_snapshot(self, kind: str = "music") -> tuple[IndexEntry, ...]:
        with self._lock:
            rows = (
                self._db()
                .execute("SELECT * FROM catalog_entries WHERE kind=? ORDER BY path", (kind,))
                .fetchall()
            )
            return tuple(self._entry(row) for row in rows if self._active(row["path"], kind))

    def browse_tracks(
        self,
        kind: str = "music",
        source_id: str | None = None,
        q: str = "",
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List active playable entries, optionally limited to one registered source."""
        if kind not in {"music", "liner"}:
            raise ValueError("kind must be 'music' or 'liner'")
        with self._lock:
            db = self._db()
            sources = db.execute("SELECT id, path, kind, recursive FROM catalog_sources").fetchall()
            source = None
            if source_id is not None:
                source = next((row for row in sources if row["id"] == source_id), None)
                if source is None:
                    raise KeyError(source_id)
                if source["kind"] != kind:
                    raise ValueError("source kind does not match requested kind")
            rows = db.execute("SELECT * FROM catalog_entries WHERE kind=?", (kind,)).fetchall()

            tokens = q.casefold().split()
            matches = []
            for row in rows:
                path = row["path"]
                if not Path(path).is_file():
                    continue
                if source is not None:
                    covered = _under(path, source["path"], bool(source["recursive"]))
                else:
                    covered = any(
                        item["kind"] == kind and _under(path, item["path"], bool(item["recursive"]))
                        for item in sources
                    )
                if not covered:
                    continue
                if kind == "music" and any(
                    item["kind"] == "liner" and _under(path, item["path"], bool(item["recursive"]))
                    for item in sources
                ):
                    continue
                entry = self._entry(row)
                searchable = " ".join(
                    (entry.artist, entry.title, entry.album, Path(path).name)
                ).casefold()
                if all(token in searchable for token in tokens):
                    matches.append(entry)

        matches.sort(
            key=lambda entry: (
                entry.artist.casefold(),
                entry.title.casefold(),
                entry.path.casefold(),
            )
        )
        return {
            "results": [
                {
                    "path": entry.path,
                    "title": entry.title,
                    "artist": entry.artist,
                    "album": entry.album,
                    "bpm": entry.bpm,
                    "length": entry.length,
                    "display_name": entry.display_name,
                    "kind": kind,
                }
                for entry in matches[offset : offset + limit]
            ],
            "total": len(matches),
            "offset": offset,
            "limit": limit,
        }

    @staticmethod
    def _entry(row: sqlite3.Row) -> IndexEntry:
        return IndexEntry(path=row["path"], **{n: row[n] for n in _ENTRY_FIELDS})

    def entry_for_path(self, path: str | Path) -> IndexEntry | None:
        with self._lock:
            normalized = _path(path)
            row = (
                self._db()
                .execute("SELECT * FROM catalog_entries WHERE path=?", (normalized,))
                .fetchone()
            )
            if row is None or not self._active(normalized, row["kind"]):
                return None
            return self._entry(row)

    def record_for_path(self, path: str | Path) -> dict | None:
        with self._lock:
            normalized = _path(path)
            row = (
                self._db()
                .execute("SELECT * FROM catalog_entries WHERE path=?", (normalized,))
                .fetchone()
            )
            if row is None:
                return None
            result = {
                "id": row["id"],
                "path": normalized,
                "kind": row["kind"],
                "allow_outro": bool(row["allow_outro"]),
                "points": json.loads(row["points"]),
                "point_sources": json.loads(row["point_sources"]),
                "prepared_path": row["prepared_path"],
                "last_played": row["last_played"],
            }
            result.update({n: row[n] for n in _ENTRY_FIELDS})
            return result

    def record_for_id(self, record_id: str) -> dict | None:
        with self._lock:
            row = (
                self._db()
                .execute("SELECT path FROM catalog_entries WHERE id=?", (record_id,))
                .fetchone()
            )
            return self.record_for_path(row[0]) if row else None

    def has_vector(self, path: str | Path) -> bool:
        with self._lock:
            return bool(
                self._db()
                .execute(
                    "SELECT 1 FROM catalog_entries e JOIN catalog_vectors v ON v.entry_id=e.id "
                    "WHERE e.path=?",
                    (_path(path),),
                )
                .fetchone()
            )

    def count_entries(
        self, kind: str = "music", *, vectors: bool = False, active: bool = False
    ) -> int:
        with self._lock:
            if vectors:
                rows = (
                    self._db()
                    .execute(
                        "SELECT e.path FROM catalog_entries e JOIN catalog_vectors v "
                        "ON v.entry_id=e.id WHERE e.kind=?",
                        (kind,),
                    )
                    .fetchall()
                )
            else:
                rows = (
                    self._db()
                    .execute("SELECT path FROM catalog_entries WHERE kind=?", (kind,))
                    .fetchall()
                )
            return sum(1 for row in rows if not active or self._active(row["path"], kind))

    def sources_for_path(self, path: str | Path, kind: str | None = None) -> list[dict]:
        normalized = _path(path)
        return [
            source
            for source in self.sources(kind)
            if _under(normalized, source["path"], source["recursive"])
        ]

    def all_entries(self, kind: str | None = None) -> tuple[dict, ...]:
        """Return every stored record, including currently uncovered/missing files."""
        with self._lock:
            sql = "SELECT path FROM catalog_entries"
            args: tuple[str, ...] = ()
            if kind is not None:
                sql, args = sql + " WHERE kind=?", (kind,)
            paths = [row[0] for row in self._db().execute(sql + " ORDER BY path", args)]
            return tuple(
                record for path in paths if (record := self.record_for_path(path)) is not None
            )

    def points(self, path: str | Path) -> dict:
        record = self.record_for_path(path)
        return {p: (record["points"].get(p) if record else None) for p in _POINTS}

    def _merge_points(self, path: str, values: dict, source: str) -> None:
        if not values:
            return
        if source not in _POINT_SOURCES:
            raise ValueError("source must be auto, lyrics, analysis, user, or imported")
        row = (
            self._db()
            .execute(
                "SELECT points,point_sources,length,kind FROM catalog_entries WHERE path=?", (path,)
            )
            .fetchone()
        )
        current, sources = json.loads(row["points"]), json.loads(row["point_sources"])
        for name, value in values.items():
            if name not in _POINTS:
                raise ValueError(f"unknown point: {name}")
            if value is not None:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int | float)
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ValueError(f"{name} must be a finite non-negative time or None")
                if row["length"] > 0 and name != "overlap_start" and value > row["length"]:
                    raise ValueError(f"{name} exceeds track length")
            if sources.get(name) in {"user", "imported"} and source not in {"user", "imported"}:
                continue
            current[name], sources[name] = value, source
        start, end = current.get("start"), current.get("end")
        for name in ("intro", "outro", "segue"):
            value = current.get(name)
            if value is not None and (
                (start is not None and value < start) or (end is not None and value > end)
            ):
                raise ValueError(f"{name} must fall between Start and End")
        if start is not None and end is not None and start >= end:
            raise ValueError("Start must precede End")
        overlap = current.get("overlap_start")
        if overlap is not None:
            if row["kind"] == "liner":
                limit_point = current.get("segue")
                if limit_point is None:
                    limit_point = current.get("end")
                if limit_point is None and row["length"] > 0:
                    limit_point = row["length"]
                limit = None if limit_point is None else limit_point - (start or 0)
                if limit is not None and overlap > limit:
                    if "overlap_start" in values and source in {"user", "imported"}:
                        raise ValueError("overlap_start exceeds the liner segue after Start")
                    if sources.get("overlap_start") == "user":
                        # Keep what the user typed; playback clamps it to the segue.
                        logger.warning(
                            "Liner %s: Overlap start %.2f s is now past its segue (%.2f s); "
                            "playback will use the segue",
                            path,
                            overlap,
                            max(0, limit),
                        )
                    else:
                        current["overlap_start"] = max(0, limit)
                        sources.setdefault("overlap_start", source)
            elif start is not None and end is not None and overlap > end - start:
                raise ValueError("overlap_start exceeds the selected Start to End range")
        self._db().execute(
            "UPDATE catalog_entries SET points=?,point_sources=? WHERE path=?",
            (json.dumps(current), json.dumps(sources), path),
        )

    def set_points(
        self,
        path: str | Path,
        values: dict,
        source: str = "user",
        *,
        allow_outro: bool | None = None,
    ) -> dict:
        with self._lock:
            normalized = _path(path)
            row = (
                self._db()
                .execute("SELECT kind FROM catalog_entries WHERE path=?", (normalized,))
                .fetchone()
            )
            if not row:
                raise KeyError(normalized)
            if allow_outro is not None and type(allow_outro) is not bool:
                raise ValueError("allow_outro must be a boolean")
            if allow_outro is not None and row["kind"] != "liner":
                raise ValueError("allow_outro can only be set for liners")
            try:
                self._merge_points(normalized, values, source)
                if allow_outro is not None:
                    self._db().execute(
                        "UPDATE catalog_entries SET allow_outro=? WHERE path=?",
                        (int(allow_outro), normalized),
                    )
                self._db().commit()
                return self.points(normalized)
            except BaseException:
                self._db().rollback()
                raise

    def remember_play(self, path: str | Path, timestamp: float | None = None) -> None:
        import time

        value = time.time() if timestamp is None else float(timestamp)
        if not math.isfinite(value):
            raise ValueError("timestamp must be finite")
        with self._lock:
            self._db().execute(
                "UPDATE catalog_entries SET last_played=? WHERE path=?", (value, _path(path))
            )
            self._db().commit()

    def last_played(self) -> dict[str, float]:
        with self._lock:
            return {
                r["path"]: float(r["last_played"])
                for r in self._db().execute("SELECT path,last_played FROM catalog_entries")
            }

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = (
                self._db()
                .execute("SELECT value FROM catalog_settings WHERE key=?", (key,))
                .fetchone()
            )
            return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._db().execute(
                "INSERT INTO catalog_settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            self._db().commit()

    def rekey(self, old: str | Path, new: str | Path) -> bool:
        """Move a confirmed record key, retaining user work and playback history."""
        with self._lock:
            old_path, new_path = _path(old), _path(new)
            old_row = (
                self._db()
                .execute("SELECT * FROM catalog_entries WHERE path=?", (old_path,))
                .fetchone()
            )
            if (
                old_row is None
                or self._db()
                .execute("SELECT 1 FROM catalog_entries WHERE path=?", (new_path,))
                .fetchone()
            ):
                return False
            if not Path(new_path).is_file():
                return False
            self._db().execute(
                "UPDATE catalog_entries SET path=? WHERE id=?", (new_path, old_row["id"])
            )
            self._db().commit()
            return True


__all__ = ["LibraryCatalog"]
