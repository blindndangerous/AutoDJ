"""Persistent, playable library records independent of the FAISS index."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import uuid
from collections.abc import Collection, Iterable, Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from autodj.indexer import IndexEntry, file_fingerprint
from autodj.sqlite_utils import readonly_uri

_connect = sqlite3.connect

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
_POINTS = ("start", "intro", "outro", "segue", "end", "overlap_start")
_POINT_SOURCES = {"auto", "lyrics", "analysis", "user", "imported"}
# Track info shows and sends cue times to the nearest 0.01 s, so a cue saved
# unchanged can land up to 0.005 s past the length or a neighbouring cue, and
# an Overlap start up to 0.01 s past its Start-to-End room (#237).  A cue that
# far over is moved onto its limit instead of rejected; the epsilon absorbs
# float error in the rounded values.
_CUE_SLACK = 0.01 + 1e-6
#: Songs longer than this many minutes are left out of the advanced analysis
#: (slow, and memory-heavy for an hour-long mix) until the Library tools page
#: saves another value; 0 analyses every length.
ADVANCED_MAX_MINUTES = 15
ADVANCED_MAX_MINUTES_KEY = "advanced_max_minutes"
#: The largest value the page accepts: ten hours.
ADVANCED_MAX_MINUTES_LIMIT = 600
# The columns a tag edit from Track info may change (LibraryCatalog.retag).
_RETAG_FIELDS = frozenset({"title", "artist", "album", "genre", "year", "bpm", "key", "mode"})
# Fields set by hand in Track info that analysis keeps (catalog_entries.user_tags).
_USER_TAGS = ("bpm", "key")


def history_start(value: object) -> datetime | None:
    """Read stored play times, treating legacy naive times as local time."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.astimezone()


def _user_tags(value: Any, fingerprint: str) -> list[str]:
    """The hand-set fields that still apply to the file with *fingerprint*.

    ``user_tags`` maps each field to the fingerprint of the file it was set
    on.  A flag for other bytes is ignored, so a file changed by any program,
    including an older AutoDJ that does not know the column, loses it.
    Anything unreadable counts as none.
    """
    try:
        tags = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(tags, dict) or not fingerprint:
        return []
    return sorted(name for name in _USER_TAGS if tags.get(name) == fingerprint)


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
        fingerprint TEXT NOT NULL DEFAULT '', mtime_ns INTEGER NOT NULL DEFAULT 0,
        points TEXT NOT NULL DEFAULT '{}',
        point_sources TEXT NOT NULL DEFAULT '{}',
        intro_lift_db REAL,
        loudness TEXT NOT NULL DEFAULT '{}',
        user_tags TEXT NOT NULL DEFAULT '{}',
        last_played REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS catalog_settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS play_history (
        event_id TEXT PRIMARY KEY NOT NULL, entry_id TEXT, path TEXT,
        title TEXT NOT NULL, artist TEXT NOT NULL, duration REAL NOT NULL,
        played_at TEXT NOT NULL, interrupted INTEGER NOT NULL DEFAULT 0,
        performances INTEGER, station TEXT NOT NULL DEFAULT 'default',
        kind TEXT NOT NULL DEFAULT 'song', ended_at TEXT
    );
    CREATE TABLE IF NOT EXISTS station_plays (
        station TEXT NOT NULL, path TEXT NOT NULL, last_played REAL NOT NULL,
        PRIMARY KEY(station, path)
    );
    CREATE TABLE IF NOT EXISTS catalog_vectors (
        entry_id TEXT PRIMARY KEY REFERENCES catalog_entries(id) ON DELETE CASCADE,
        vector BLOB NOT NULL
    );
"""

# What the playable library is built from, each with a counter that SQLite
# triggers raise on every change, whichever process makes it.  A play, a
# play-log row, a setting or a loudness measurement changes none of the
# first three, so it never makes the server reload the library.
_CHANGE_KINDS = ("entries", "vectors", "sources", "records")
_RECORD_COLUMNS = (
    "allow_outro",
    "points",
    "point_sources",
    "intro_lift_db",
    "loudness",
)


def _bump(*kinds: str) -> str:
    names = ",".join(f"'{kind}'" for kind in kinds)
    return f"UPDATE catalog_changes SET version=version+1 WHERE name IN ({names});"  # nosec B608 -- constant change kinds


_CHANGE_TRACKING = f"""
    CREATE TABLE IF NOT EXISTS catalog_changes (
        name TEXT PRIMARY KEY, version INTEGER NOT NULL
    );
    INSERT OR IGNORE INTO catalog_changes VALUES
        ('entries', 0), ('vectors', 0), ('sources', 0), ('records', 0);
    CREATE TRIGGER IF NOT EXISTS catalog_entries_added AFTER INSERT ON catalog_entries
        BEGIN {_bump("entries", "records")} END;
    CREATE TRIGGER IF NOT EXISTS catalog_entries_removed AFTER DELETE ON catalog_entries
        BEGIN {_bump("entries", "records")} END;
    CREATE TRIGGER IF NOT EXISTS catalog_entries_changed
        AFTER UPDATE OF path, kind, {", ".join(_ENTRY_FIELDS)} ON catalog_entries
        BEGIN {_bump("entries", "records")} END;
    CREATE TRIGGER IF NOT EXISTS catalog_records_changed
        AFTER UPDATE OF {", ".join(_RECORD_COLUMNS)} ON catalog_entries
        BEGIN {_bump("records")} END;
    CREATE TRIGGER IF NOT EXISTS catalog_vectors_added AFTER INSERT ON catalog_vectors
        BEGIN {_bump("vectors")} END;
    CREATE TRIGGER IF NOT EXISTS catalog_vectors_removed AFTER DELETE ON catalog_vectors
        BEGIN {_bump("vectors")} END;
    CREATE TRIGGER IF NOT EXISTS catalog_vectors_changed AFTER UPDATE ON catalog_vectors
        BEGIN {_bump("vectors")} END;
    CREATE TRIGGER IF NOT EXISTS catalog_sources_added AFTER INSERT ON catalog_sources
        BEGIN {_bump("sources")} END;
    CREATE TRIGGER IF NOT EXISTS catalog_sources_removed AFTER DELETE ON catalog_sources
        BEGIN {_bump("sources")} END;
    CREATE TRIGGER IF NOT EXISTS catalog_sources_changed AFTER UPDATE ON catalog_sources
        BEGIN {_bump("sources")} END;
"""

# Rows of vectors read and handed on at a time, so a reload never holds every
# vector twice (as SQLite blobs and as one matrix) next to the FAISS copy.
VECTOR_CHUNK = 4096


# The station the root config sections describe (autodj.stations.DEFAULT_SLUG).
# Its plays keep their last-played time in catalog_entries; every other
# station's are in station_plays.  Play log rows name their station.
DEFAULT_STATION = "default"


def _path(path: str | Path) -> str:
    value = os.path.abspath(os.path.expanduser(os.fspath(path)))
    value = os.path.normpath(value)
    return os.path.normcase(value) if os.name == "nt" else value


def normalize_path(path: str | Path) -> str:
    """*path* spelled the way the catalog stores it (absolute, normalized, case-folded on Windows)."""
    return _path(path)


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


def catalog_counts(path: Path) -> tuple[int, int]:
    """Read stored music and analysis counts without modifying the catalog."""
    with closing(_connect(readonly_uri(path), uri=True)) as db:
        ready, analyzed = db.execute(
            "SELECT COUNT(*), COUNT(v.entry_id) FROM catalog_entries e "
            "LEFT JOIN catalog_vectors v ON v.entry_id=e.id WHERE e.kind='music'"
        ).fetchone()
        return ready, analyzed


def _prefix(root: str) -> str:
    return root if root.endswith(os.sep) else root + os.sep


def _covered_by(path: str, roots: Iterable[tuple[str, bool]]) -> bool:
    """:func:`_under` for normalized paths against ``(prefix, recursive)`` roots."""
    for prefix, recursive in roots:
        if path.startswith(prefix):
            rest = path[len(prefix) :]
            if rest and (recursive or os.sep not in rest):
                return True
    return False


class Coverage:
    """The registered sources, read once, deciding which records are playable.

    The same rules as :meth:`LibraryCatalog._active` without a file-system
    call per record: paths and roots are both stored normalized, so "under
    a root" is a prefix test.  A source whose folder is missing (an
    unmounted share) covers nothing, as every file under it would fail
    ``is_file``.  A registered liner root owns the files under it even
    when its folder is missing, as before.
    """

    def __init__(self, sources: Iterable[Sequence[Any]]) -> None:
        """Take ``(path, kind, recursive)`` rows."""
        self.music: list[tuple[str, bool]] = []
        self.liner: list[tuple[str, bool]] = []
        self.liner_owned: list[tuple[str, bool]] = []
        for root, kind, recursive in sources:
            item = (_prefix(root), bool(recursive))
            if kind == "liner":
                self.liner_owned.append(item)
            if not os.path.isdir(root):
                continue
            (self.liner if kind == "liner" else self.music).append(item)

    def covered(self, path: str, kind: str | None = None) -> bool:
        """Whether a present source folder (of *kind*, or of either) holds *path*."""
        if kind is None:
            return _covered_by(path, self.music) or _covered_by(path, self.liner)
        return _covered_by(path, self.liner if kind == "liner" else self.music)

    def active(self, path: str, kind: str) -> bool:
        if kind == "liner":
            return _covered_by(path, self.liner)
        return _covered_by(path, self.music) and not _covered_by(path, self.liner_owned)


class CatalogReader:
    """One consistent read of the catalog, on a read-only connection of its own.

    Everything read through one reader comes from the same transaction, so
    the change counters match the rows.  The catalog's own connection (and
    lock) stays free for the play log while a large reload runs.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def versions(self) -> dict[str, int]:
        return _versions(self._conn)

    def coverage(self) -> Coverage:
        return Coverage(self._conn.execute("SELECT path, kind, recursive FROM catalog_sources"))

    def entries(
        self, kind: str, coverage: Coverage, exclude: Collection[str] = ()
    ) -> tuple[IndexEntry, ...]:
        """Playable records of *kind* in path order, without *exclude* (normalized paths)."""
        rows = self._conn.execute(
            f"SELECT path,{','.join(_ENTRY_FIELDS)} FROM catalog_entries "  # nosec B608 -- constant field names
            "WHERE kind=? ORDER BY path",
            (kind,),
        )
        return tuple(
            IndexEntry(*row)
            for row in rows
            if row[0] not in exclude and coverage.active(row[0], kind)
        )

    def vector_paths(self) -> list[str]:
        """Paths of the music records that have a vector, in path order."""
        return [
            row[0]
            for row in self._conn.execute(
                "SELECT e.path FROM catalog_entries e JOIN catalog_vectors v "
                "ON v.entry_id=e.id WHERE e.kind='music' ORDER BY e.path"
            )
        ]

    def vectors(self, paths: Collection[str], chunk: int = VECTOR_CHUNK) -> Iterator[np.ndarray]:
        """Yield the vectors of *paths*, in path order, *chunk* rows at a time."""
        from autodj.indexer import FEATURE_DIM

        block = np.empty((chunk, FEATURE_DIM), dtype=np.float32)
        filled = 0
        for path, blob in self._conn.execute(
            "SELECT e.path, v.vector FROM catalog_entries e JOIN catalog_vectors v "
            "ON v.entry_id=e.id WHERE e.kind='music' ORDER BY e.path"
        ):
            if path not in paths:
                continue
            block[filled] = np.frombuffer(blob, dtype=np.float32)
            filled += 1
            if filled == chunk:
                yield block
                block = np.empty((chunk, FEATURE_DIM), dtype=np.float32)
                filled = 0
        if filled:
            yield block[:filled]

    def last_played(self, station: str = DEFAULT_STATION) -> dict[str, float]:
        """When *station* last played each track."""
        return _last_played(self._conn, station)


def _last_played(conn: sqlite3.Connection, station: str) -> dict[str, float]:
    if station == DEFAULT_STATION:
        rows = conn.execute("SELECT path,last_played FROM catalog_entries")
    else:
        rows = conn.execute(
            "SELECT path,last_played FROM station_plays WHERE station=?", (station,)
        )
    return {path: float(value) for path, value in rows}


def _versions(conn: sqlite3.Connection) -> dict[str, int]:
    found = dict(conn.execute("SELECT name, version FROM catalog_changes").fetchall())
    return {kind: int(found.get(kind, 0)) for kind in _CHANGE_KINDS}


def _tune(conn: sqlite3.Connection) -> None:
    """Make bulk reads cheaper: map the file and keep a larger page cache."""
    conn.execute("PRAGMA mmap_size=268435456")
    conn.execute("PRAGMA cache_size=-16384")


class LibraryCatalog:
    """SQLite source registry and playable track catalog for one library."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Paths whose tags are being rewritten now, with how many edits hold
        # each (see editing()).
        self._editing: dict[str, int] = {}
        # Writers wait for each other instead of failing while a reload or
        # another process (the analysis worker) holds the file.
        self._conn: sqlite3.Connection | None = _connect(
            self.path, check_same_thread=False, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA foreign_keys=ON")
            # WAL: a reload's long read never blocks the play log's writes,
            # and other processes (the analysis job) read alongside.  Only
            # this machine uses library.db: an index copied from another
            # machine brings the generation files and dj_meta.db, never it,
            # and a backup restore removes stale -wal and -shm files.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._add_missing_columns()
            self._conn.executescript(_CHANGE_TRACKING)
            if self._conn.execute(
                "SELECT 1 FROM catalog_vectors WHERE entry_id NOT IN "
                "(SELECT id FROM catalog_entries) LIMIT 1"
            ).fetchone():
                self._conn.execute(
                    "DELETE FROM catalog_vectors WHERE entry_id NOT IN (SELECT id FROM catalog_entries)"
                )
                self._conn.commit()
            _tune(self._conn)
        except BaseException:
            # Not a catalog (or unreadable): do not leave the file open.
            self._conn.close()
            self._conn = None
            raise

    def _add_missing_columns(self) -> None:
        """Add columns an older catalog file lacks.

        These are the liner ``allow_outro`` flag, the cached quiet-intro
        ``intro_lift_db``, loudness metadata, and the play log's
        ``performances`` count and ``station``.  A play logged before
        stations existed belongs to the default station.  The checks and the
        changes share one write transaction, so a second process opening the
        same old file waits, then finds the columns.
        """
        db = self._db()
        db.execute("BEGIN IMMEDIATE")
        try:
            entries = {row[1] for row in db.execute("PRAGMA table_info(catalog_entries)")}
            if "allow_outro" not in entries:
                db.execute(
                    "ALTER TABLE catalog_entries ADD COLUMN allow_outro INTEGER NOT NULL DEFAULT 0"
                )
            if "intro_lift_db" not in entries:
                db.execute("ALTER TABLE catalog_entries ADD COLUMN intro_lift_db REAL")
            if "loudness" not in entries:
                db.execute(
                    "ALTER TABLE catalog_entries ADD COLUMN loudness TEXT NOT NULL DEFAULT '{}'"
                )
            if "user_tags" not in entries:
                db.execute(
                    "ALTER TABLE catalog_entries ADD COLUMN user_tags TEXT NOT NULL DEFAULT '{}'"
                )
            if "mtime_ns" not in entries:
                db.execute(
                    "ALTER TABLE catalog_entries ADD COLUMN mtime_ns INTEGER NOT NULL DEFAULT 0"
                )
            have = {row[1] for row in db.execute("PRAGMA table_info(play_history)")}
            if "performances" not in have:
                db.execute("ALTER TABLE play_history ADD COLUMN performances INTEGER")
            if "station" not in have:
                db.execute(
                    "ALTER TABLE play_history ADD COLUMN station TEXT NOT NULL DEFAULT 'default'"
                )
            if "kind" not in have:
                db.execute("ALTER TABLE play_history ADD COLUMN kind TEXT NOT NULL DEFAULT 'song'")
            if "ended_at" not in have:
                db.execute("ALTER TABLE play_history ADD COLUMN ended_at TEXT")
            # Each station reads its own plays, newest first; SQLite keeps the
            # rowid in every index, so this one also orders them.
            db.execute("CREATE INDEX IF NOT EXISTS play_history_station ON play_history(station)")
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

    def migrate_legacy_history(self) -> None:
        """Explicitly import the old JSON log, refusing to discard invalid rows."""
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
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("legacy play-history JSON is invalid") from exc
                if not isinstance(rows, list):
                    raise ValueError("legacy play-history must be a JSON list")
                valid_rows = []
                invalid = 0
                for row in rows:
                    if not isinstance(row, dict):
                        invalid += 1
                        continue
                    duration = self._duration(row.get("duration"))
                    if (
                        not isinstance(row.get("title"), str)
                        or not isinstance(row.get("artist"), str)
                        or duration is None
                        or not self._valid_played_at(row.get("played_at"))
                        or ("path" in row and not isinstance(row["path"], str))
                        or (
                            "event_id" in row
                            and (not isinstance(row["event_id"], str) or not row["event_id"])
                        )
                        or (
                            "station" in row
                            and (not isinstance(row["station"], str) or not row["station"])
                        )
                        or ("interrupted" in row and type(row["interrupted"]) is not bool)
                        or ("performances" in row and self._count(row["performances"]) is None)
                    ):
                        invalid += 1
                        continue
                    valid_rows.append((row, duration))
                if invalid:
                    raise ValueError(
                        f"legacy play-history has {invalid} invalid row(s); no history was migrated"
                    )
                for row, duration in valid_rows:
                    path = _path(row["path"]) if isinstance(row.get("path"), str) else None
                    entry = (
                        db.execute(
                            "SELECT id FROM catalog_entries WHERE path=?", (path,)
                        ).fetchone()
                        if path
                        else None
                    )
                    event_id = row.get("event_id")
                    if not isinstance(event_id, str) or not event_id:
                        event_id = uuid.uuid4().hex
                    station = row.get("station", DEFAULT_STATION)
                    if not isinstance(station, str) or not station:
                        station = DEFAULT_STATION
                    interrupted = int(row.get("interrupted") is True)
                    performances = self._count(row.get("performances"))
                    db.execute(
                        "INSERT INTO play_history "
                        "(event_id,entry_id,path,title,artist,duration,played_at,interrupted,station,performances) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            event_id,
                            entry[0] if entry else None,
                            path,
                            row["title"],
                            row["artist"],
                            duration,
                            row["played_at"],
                            interrupted,
                            station,
                            performances,
                        ),
                    )
                db.execute("INSERT INTO catalog_settings VALUES ('play_history_migrated','true')")
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def write_history(
        self,
        rows: Sequence[dict],
        interrupted: Sequence[str] = (),
        station: str = DEFAULT_STATION,
    ) -> None:
        """Append unique play events of *station* and mark any supplied events interrupted.

        A row for an event already stored only raises its ``performances``,
        which grows as listeners join during the play; a live DJ's set
        (``kind`` ``"live"``, no path) is stored when it starts and again
        with its length and ``ended_at`` when it ends.
        """
        clean_rows = []
        for row in rows:
            event_id = row.get("event_id") if isinstance(row, dict) else None
            path = row.get("path") if isinstance(row, dict) else None
            duration = self._duration(row.get("duration")) if isinstance(row, dict) else None
            # A live DJ's set (#48) has no file.
            live = isinstance(row, dict) and row.get("kind") == "live"
            if (
                not isinstance(event_id, str)
                or not event_id
                or (not live and (not isinstance(path, str) or not path))
                or not isinstance(row.get("title"), str)
                or not isinstance(row.get("artist"), str)
                or duration is None
                or not self._valid_played_at(row.get("played_at"))
            ):
                continue
            normalized_path = None if live else _path(str(path))
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
                    station,
                    "live" if live else "song",
                    row.get("ended_at") if self._valid_played_at(row.get("ended_at")) else None,
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
                    "played_at,performances,station,kind,ended_at) "
                    "VALUES (?,(SELECT id FROM catalog_entries WHERE path=?),?,?,?,?,?,?,?,?,?) "
                    # A play is written again when a listener joins during it, a
                    # live set when it ends, and a song a live DJ cut off with
                    # its end (#48).  Only a live set's length and end change;
                    # a song's end is set once and its length never.
                    "ON CONFLICT(event_id) DO UPDATE SET performances=CASE "
                    "WHEN excluded.performances IS NULL THEN performances "
                    "ELSE max(COALESCE(performances,0),excluded.performances) END,"
                    "duration=CASE WHEN excluded.kind='live' "
                    "THEN excluded.duration ELSE duration END,"
                    "ended_at=CASE WHEN excluded.kind='live' "
                    "THEN excluded.ended_at ELSE COALESCE(ended_at,excluded.ended_at) END "
                    "WHERE excluded.performances IS NOT NULL OR excluded.kind='live' "
                    "OR excluded.ended_at IS NOT NULL",
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

    def recent_history(self, limit: int = 500, station: str = DEFAULT_STATION) -> list[dict]:
        """*station*'s latest *limit* completed plays, oldest first."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        with self._lock:
            rows = (
                self._db()
                .execute(
                    "SELECT event_id,entry_id,path,title,artist,duration,played_at,kind,ended_at FROM ("
                    "SELECT rowid,event_id,entry_id,path,title,artist,duration,played_at,kind,ended_at "
                    "FROM play_history WHERE interrupted=0 AND station=? "
                    "ORDER BY rowid DESC LIMIT ?) "
                    "ORDER BY rowid ASC",
                    (station, limit),
                )
                .fetchall()
            )
            return [dict(row) for row in rows]

    def recent_plays(self, limit: int, station: str = DEFAULT_STATION) -> list[dict]:
        """*station*'s latest *limit* plays that started, oldest first, with each album.

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
                    "WHERE h.interrupted=0 AND h.station=? AND h.path IS NOT NULL AND h.kind!='live' "
                    "AND (e.kind IS NULL OR e.kind='music') "
                    "ORDER BY h.rowid DESC LIMIT ?) ORDER BY row ASC",
                    (station, limit),
                )
                .fetchall()
            )
            return [dict(row) for row in rows]

    def play_log(
        self,
        station: str = DEFAULT_STATION,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict]:
        """Read completed plays in insertion order, optionally within a report range.

        Ranged rows include the next completed play's time, even outside the
        range, so reports retain their duration clipping. Date parsing keeps
        legacy local times and offset timestamps consistent with reports.
        """
        if (start is None) != (end is None):
            raise ValueError("Both start and end are required")
        if start is not None and end is not None and end <= start:
            raise ValueError("The end of the range must be after its start.")
        following, where = "", ""
        # Use a separate snapshot so report reads do not hold the writer lock.
        with self.reader() as reader:
            db = reader._conn
            db.row_factory = sqlite3.Row
            if start is not None and end is not None:

                def in_range(value: object) -> bool:
                    moment = history_start(value)
                    return moment is not None and start <= moment < end

                # shortcut: legacy times need per-row parsing; index normalized times if scans grow costly.
                db.create_function("in_report_range", 1, in_range)
                where = " AND in_report_range(h.played_at)"
                following = (
                    ", (SELECT n.played_at FROM play_history n "
                    "WHERE n.station=h.station AND n.interrupted=0 AND n.rowid>h.rowid "
                    "ORDER BY n.rowid LIMIT 1) AS following_at"
                )
            rows = db.execute(
                "SELECT h.played_at,h.duration,h.title,h.artist,"  # nosec B608 -- fixed SQL fragments; values bound
                "COALESCE(e.path,h.path) AS path,e.album,COALESCE(NULLIF(h.kind,'song'),e.kind) AS kind,"
                "h.performances,h.ended_at "
                + following
                + " FROM play_history h LEFT JOIN catalog_entries e ON e.id="  # nosec B608 -- fixed SQL fragments
                "COALESCE(h.entry_id,(SELECT id FROM catalog_entries WHERE path=h.path)) "
                "WHERE h.interrupted=0 AND h.station=?" + where + " ORDER BY h.rowid",
                (station,),
            ).fetchall()
            return [dict(row) for row in rows]

    def change_versions(self) -> dict[str, int]:
        """Counters of changes to records, vectors, sources and cue data.

        ``entries`` counts changes to the playable records themselves (new,
        removed, moved or re-read tracks); ``records`` also counts cue
        points, loudness and the other per-track data.  Plays, the play log
        and settings change none of them.  SQLite triggers keep them, so
        writes from other processes count too.
        """
        with self._lock:
            return _versions(self._db())

    @contextmanager
    def reader(self) -> Iterator[CatalogReader]:
        """Read one consistent snapshot on a separate read-only connection."""
        conn = _connect(readonly_uri(self.path), uri=True, timeout=30, check_same_thread=False)
        try:
            _tune(conn)
            conn.execute("BEGIN")
            yield CatalogReader(conn)
        finally:
            conn.close()

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

    def seed_sources(self, music_root: Path | str | None, liner_root: Path | str) -> bool:
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

    def seed_station_liner_source(self, station: str, root: Path | str) -> str | None:
        """Add *station*'s liner folder as a liner source once; return its id when added."""
        key = f"sources_seeded:{station}"
        with self._lock:
            self._db()
            if self.get_setting(key, False):
                return None
            Path(root).mkdir(parents=True, exist_ok=True)
            source = self._add_source(root, "liner", False, False, validate=False)
            self.set_setting(key, True)
            return str(source["id"])

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

    def remove_entries(
        self, paths: list[str], *, expected_versions: dict[str, int] | None = None
    ) -> int:
        """Remove records if the catalog still matches a previewed version snapshot."""
        normalized = list(dict.fromkeys(_path(path) for path in paths))
        if not normalized:
            return 0
        with self._lock:
            db = self._db()
            try:
                db.execute("BEGIN IMMEDIATE")
                if expected_versions is not None and _versions(db) != expected_versions:
                    raise RuntimeError("The catalog changed; preview the removal again")
                cursor = db.executemany(
                    "DELETE FROM catalog_entries WHERE path=?", ((p,) for p in normalized)
                )
                removed = cursor.rowcount
                db.commit()
                return removed
            except BaseException:
                db.rollback()
                raise

    def _put(
        self,
        entry: IndexEntry,
        kind: str,
        points: dict | None,
        point_source: str,
    ) -> str:
        if kind not in {"music", "liner"}:
            raise ValueError("kind must be 'music' or 'liner'")
        if point_source not in _POINT_SOURCES:
            raise ValueError("invalid point source")
        path = _path(entry.path)
        row = self._db().execute("SELECT * FROM catalog_entries WHERE path=?", (path,)).fetchone()
        if row and path in self._editing:
            # Its tags are being rewritten (retag): the new bytes would look
            # like a new recording and clear its analysis.  retag stores them.
            return str(row["id"])
        record_id = row["id"] if row else uuid.uuid4().hex
        values = [getattr(entry, name) for name in _ENTRY_FIELDS]
        try:
            mtime_ns = os.stat(path).st_mtime_ns
        except OSError:
            mtime_ns = 0
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
                    "UPDATE catalog_entries SET intro_lift_db=NULL, loudness='{}', points='{}', point_sources='{}', user_tags='{}' "
                    "WHERE id=?",
                    (record_id,),
                )
            else:
                # Same file: a key or BPM set by hand in Track info outlives
                # an analysis that measured its own.
                for name in _user_tags(row["user_tags"], row["fingerprint"]):
                    for column in ("key", "mode") if name == "key" else (name,):
                        values[_ENTRY_FIELDS.index(column)] = row[column]
        cols = ",".join(_ENTRY_FIELDS)
        placeholders = ",".join("?" for _ in _ENTRY_FIELDS)
        if row:
            self._db().execute(
                f"UPDATE catalog_entries SET kind=?, mtime_ns=?, {','.join(f'{n}=?' for n in _ENTRY_FIELDS)} WHERE path=?",  # nosec B608 -- constant field names; values are bound
                (kind, mtime_ns, *values, path),
            )
            if kind != "liner":
                self._db().execute("UPDATE catalog_entries SET allow_outro=0 WHERE path=?", (path,))
        else:
            self._db().execute(
                f"INSERT INTO catalog_entries (id,path,kind,mtime_ns,{cols}) VALUES (?,?,?,?,{placeholders})",  # nosec B608 -- constant field names; values are bound
                (record_id, path, kind, mtime_ns, *values),
            )
        self._merge_points(path, points or {}, point_source)
        return record_id

    def put_entry(
        self,
        entry: IndexEntry,
        kind: str = "music",
        points: dict | None = None,
        point_source: str = "auto",
    ) -> str:
        with self._lock:
            try:
                record_id = self._put(entry, kind, points, point_source)
                self._db().commit()
                return record_id
            except BaseException:
                self._db().rollback()
                raise

    def set_intro_lift(self, path: str | Path, lift_db: float) -> None:
        """Cache the source opening's maximum safe lift after analysis."""
        with self._lock:
            self._db().execute(
                "UPDATE catalog_entries SET intro_lift_db=? WHERE path=?",
                (lift_db, _path(path)),
            )
            self._db().commit()

    def set_loudness(self, path: str | Path, measurement: dict) -> None:
        """Store a current loudness analysis for a catalog entry."""
        if not isinstance(measurement, dict):
            raise ValueError("measurement must be a dict")
        version, kind = measurement.get("version"), measurement.get("kind")
        size, mtime_ns = measurement.get("size"), measurement.get("mtime_ns")
        lufs, gain_db = measurement.get("lufs"), measurement.get("gain_db")
        if (
            not isinstance(version, str)
            or kind not in ("music", "liner")
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or isinstance(mtime_ns, bool)
            or not isinstance(mtime_ns, int)
            or mtime_ns < 0
            or isinstance(lufs, bool)
            or not isinstance(lufs, int | float)
            or isinstance(gain_db, bool)
            or not isinstance(gain_db, int | float)
        ):
            raise ValueError("invalid loudness measurement")
        try:
            lufs, gain_db = float(lufs), float(gain_db)
        except OverflowError as exc:
            raise ValueError("invalid loudness measurement") from exc
        if not math.isfinite(lufs) or not math.isfinite(gain_db) or not -60 <= gain_db <= 18:
            raise ValueError("invalid loudness measurement")
        normalized = _path(path)
        with self._lock:
            db = self._db()
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT kind,size FROM catalog_entries WHERE path=?", (normalized,)
                ).fetchone()
                try:
                    stat = os.stat(normalized)
                except OSError:
                    stat = None
                if (
                    row is not None
                    and row["kind"] == kind
                    and row["size"] == size
                    and stat is not None
                    and stat.st_size == size
                    and stat.st_mtime_ns == mtime_ns
                ):
                    db.execute(
                        "UPDATE catalog_entries SET loudness=? WHERE path=?",
                        (
                            json.dumps(
                                {
                                    "version": version,
                                    "kind": kind,
                                    "size": size,
                                    "mtime_ns": mtime_ns,
                                    "lufs": lufs,
                                    "gain_db": gain_db,
                                },
                                allow_nan=False,
                            ),
                            normalized,
                        ),
                    )
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def seed_entries(self, entries: Iterable[IndexEntry]) -> int:
        """Import existing indexed records without replacing catalog metadata."""
        with self._lock:
            existing = {row[0] for row in self._db().execute("SELECT path FROM catalog_entries")}
            count = 0
            for entry in entries:
                path = _path(entry.path)
                if path not in existing:
                    self._put(entry, "music", None, "auto")
                    existing.add(path)
                    count += 1
            self._db().commit()
            return count

    def paths_with_vectors(self) -> set[str]:
        """Paths of every record that has a vector."""
        with self._lock:
            return {
                row[0]
                for row in self._db().execute(
                    "SELECT e.path FROM catalog_entries e JOIN catalog_vectors v ON v.entry_id=e.id"
                )
            }

    def seed_vectors(self, entries: Sequence[IndexEntry], vectors: np.ndarray) -> int:
        from autodj.indexer import FEATURE_DIM

        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.shape != (len(entries), FEATURE_DIM) or not np.isfinite(matrix).all():
            raise ValueError(f"vectors must be finite with shape ({len(entries)}, {FEATURE_DIM})")
        with self._lock:
            db, count = self._db(), 0
            ids = dict(db.execute("SELECT path, id FROM catalog_entries").fetchall())
            have = {row[0] for row in db.execute("SELECT entry_id FROM catalog_vectors")}
            for entry, vector in zip(entries, matrix, strict=True):
                record_id = ids.get(_path(entry.path))
                if record_id is None or record_id in have:
                    continue
                db.execute(
                    "INSERT INTO catalog_vectors VALUES (?,?)", (record_id, vector.tobytes())
                )
                have.add(record_id)
                count += 1
            db.commit()
            return count

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
        """Records of *kind* that registered, present sources cover, in path order."""
        with self.reader() as reader:
            return reader.entries(kind, reader.coverage())

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
            return self._record(row)

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        try:
            loudness = json.loads(row["loudness"])
        except (TypeError, json.JSONDecodeError):
            loudness = {}
        if not isinstance(loudness, dict):
            loudness = {}
        result = {
            "id": row["id"],
            "path": row["path"],
            "kind": row["kind"],
            "mtime_ns": row["mtime_ns"],
            "allow_outro": bool(row["allow_outro"]),
            "points": json.loads(row["points"]),
            "point_sources": json.loads(row["point_sources"]),
            "intro_lift_db": row["intro_lift_db"],
            "loudness": loudness,
            "last_played": row["last_played"],
            "user_tags": _user_tags(row["user_tags"], row["fingerprint"]),
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

    def sources_for_path(self, path: str | Path, kind: str | None = None) -> list[dict]:
        normalized = _path(path)
        return [
            source
            for source in self.sources(kind)
            if _under(normalized, source["path"], source["recursive"])
        ]

    def stored_entries(self) -> list[tuple[str, IndexEntry]]:
        """Every stored record as ``(kind, entry)``, missing files included, in one query."""
        with self._lock:
            rows = self._db().execute(
                f"SELECT kind,path,{','.join(_ENTRY_FIELDS)} FROM catalog_entries"  # nosec B608 -- constant field names
            )
            return [(row[0], IndexEntry(*tuple(row)[1:])) for row in rows]

    def advanced_max_minutes(self) -> int:
        """Longest song, in minutes, the advanced analysis takes; ``0`` takes every length.

        Saved from the Library tools page; :data:`ADVANCED_MAX_MINUTES`
        until it is.
        """
        value = self.get_setting(ADVANCED_MAX_MINUTES_KEY, ADVANCED_MAX_MINUTES)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return ADVANCED_MAX_MINUTES
        return value

    def _unembedded_paths(self, *, ordered: bool, too_long: bool = False) -> Iterator[str]:
        """Yield music paths with no vector under an advanced, non-liner source.

        Songs longer than :meth:`advanced_max_minutes` are left out, or,
        with *too_long*, are the only ones yielded.
        """
        with self._lock:
            db = self._db()
            sources = db.execute("SELECT path,kind,recursive,advanced FROM catalog_sources")
            advanced, liner = [], []
            for root, kind, recursive, is_advanced in sources:
                if kind == "liner":
                    liner.append((_prefix(root), bool(recursive)))
                elif is_advanced:
                    advanced.append((_prefix(root), bool(recursive)))
            if not advanced:
                return
            limit = self.advanced_max_minutes() * 60.0
            if too_long and not limit:
                return
            # A song of unknown length (0) is not known to be too long.
            length = (
                ""
                if not limit
                else " AND e.length>?"
                if too_long
                else " AND (e.length<=? OR e.length<=0)"
            )
            order = " ORDER BY e.path" if ordered else ""
            for (path,) in db.execute(
                "SELECT e.path FROM catalog_entries e LEFT JOIN catalog_vectors v "  # nosec B608 -- fixed fragments
                "ON v.entry_id=e.id WHERE e.kind='music' AND v.entry_id IS NULL" + length + order,
                (limit,) if limit else (),
            ):
                if _covered_by(path, advanced) and not _covered_by(path, liner):
                    yield path

    def unembedded_paths(self) -> list[str]:
        """Paths of music records with no vector under an advanced, non-liner source.

        Songs longer than :meth:`advanced_max_minutes` are left out.
        """
        return list(self._unembedded_paths(ordered=True))

    def unembedded_count(self) -> int:
        """Count pending music without sorting or materializing its paths."""
        return sum(1 for _ in self._unembedded_paths(ordered=False))

    def too_long_for_analysis_count(self) -> int:
        """Count songs left out of the advanced analysis for their length."""
        return sum(1 for _ in self._unembedded_paths(ordered=False, too_long=True))

    def unembedded_music(
        self, *, limit: int | None = None, exclude: Collection[str] = ()
    ) -> list[dict]:
        """Records the analysis job should embed, in path order.

        :meth:`unembedded_paths` whose file exists, leaving out *exclude*
        (normalized paths), at most *limit* of them.  A file check only for
        the records returned.
        """
        records: list[dict] = []
        for path in self.unembedded_paths():
            if limit is not None and len(records) >= limit:
                break
            if path in exclude or not Path(path).is_file():
                continue
            record = self.record_for_path(path)
            if record is not None:
                records.append(record)
        return records

    def store_analysis(
        self,
        path: str | Path,
        size: int,
        fingerprint: str,
        analysis: dict[str, float | int],
        vector: np.ndarray,
    ) -> bool:
        """Save a song's advanced analysis if it is still the file it was made from.

        The record must still be music with this *size* and *fingerprint*,
        and the file must still match those values.
        The analysed fields, the stale-check
        stamp and the vector change in one transaction, so the change
        counters show the server a complete result.

        Args:
            path: The song.
            size: File size the analysis was made from.
            fingerprint: :func:`~autodj.indexer.file_fingerprint` it was made from.
            analysis: ``energy``, ``key``, ``mode``, ``bpm`` and
                ``tempo_confidence``, as ``autodj index`` measures them.
            vector: The combined vector.

        Returns:
            ``False`` when the song is gone or its file changed (a stale
            result), ``True`` once stored.

        Raises:
            ValueError: The vector is not a finite ``FEATURE_DIM`` vector.
        """
        from autodj.indexer import FEATURE_DIM, _apply_analysis_metadata, source_mtime

        value = np.asarray(vector, dtype=np.float32)
        if value.shape != (FEATURE_DIM,) or not np.isfinite(value).all():
            raise ValueError(f"vector must be a finite float32 array of shape ({FEATURE_DIM},)")
        normalized = _path(path)
        if not size or not fingerprint or file_fingerprint(normalized) != (size, fingerprint):
            return False
        mtime = source_mtime(normalized)
        with self._lock:
            db = self._db()
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM catalog_entries WHERE path=?", (normalized,)
                ).fetchone()
                if (
                    row is None
                    or row["kind"] != "music"
                    or row["size"] != size
                    or row["fingerprint"] != fingerprint
                ):
                    db.rollback()
                    return False
                entry = self._entry(row)
                _apply_analysis_metadata(
                    entry, analysis, keep=_user_tags(row["user_tags"], row["fingerprint"])
                )
                db.execute(
                    "UPDATE catalog_entries SET energy=?, key=?, mode=?, bpm=?, "
                    "tempo_confidence=?, embedded_at=? WHERE id=?",
                    (
                        entry.energy,
                        entry.key,
                        entry.mode,
                        entry.bpm,
                        entry.tempo_confidence,
                        mtime,
                        row["id"],
                    ),
                )
                db.execute(
                    "INSERT INTO catalog_vectors VALUES (?,?) ON CONFLICT(entry_id) "
                    "DO UPDATE SET vector=excluded.vector",
                    (row["id"], value.tobytes()),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return True

    def all_entries(self, kind: str | None = None) -> tuple[dict, ...]:
        """Return every stored record, including currently uncovered/missing files."""
        with self._lock:
            sql = "SELECT * FROM catalog_entries"
            args: tuple[str, ...] = ()
            if kind is not None:
                sql, args = sql + " WHERE kind=?", (kind,)
            return tuple(
                self._record(row) for row in self._db().execute(sql + " ORDER BY path", args)
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
                    or (
                        value < -15
                        if name == "overlap_start" and row["kind"] == "liner"
                        else value < 0
                    )
                ):
                    raise ValueError(
                        f"{name} must be a finite time of at least -15 seconds or None"
                        if name == "overlap_start" and row["kind"] == "liner"
                        else f"{name} must be a finite non-negative time or None"
                    )
                if row["length"] > 0 and name != "overlap_start" and value > row["length"]:
                    if value - row["length"] > _CUE_SLACK:
                        raise ValueError(f"{name} exceeds track length")
                    value = row["length"]
            if sources.get(name) in {"user", "imported"} and source not in {"user", "imported"}:
                continue
            current[name], sources[name] = value, source
        start, end = current.get("start"), current.get("end")
        for name in ("intro", "outro", "segue"):
            value = current.get(name)
            if value is None:
                continue
            if start is not None and value < start:
                if start - value > _CUE_SLACK:
                    raise ValueError(f"{name} must fall between Start and End")
                current[name] = start
            elif end is not None and value > end:
                if value - end > _CUE_SLACK:
                    raise ValueError(f"{name} must fall between Start and End")
                current[name] = end
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
                    if (
                        "overlap_start" in values
                        and source in {"user", "imported"}
                        and overlap - limit > _CUE_SLACK
                    ):
                        raise ValueError("overlap_start exceeds the liner segue after Start")
                    current["overlap_start"], sources["overlap_start"] = max(0, limit), source
            elif start is not None and end is not None and overlap > end - start:
                if overlap - (end - start) > _CUE_SLACK:
                    raise ValueError("overlap_start exceeds the selected Start to End range")
                current["overlap_start"] = end - start
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

    @contextmanager
    def editing(self, path: str | Path) -> Iterator[None]:
        """Mark *path* as having its tags rewritten until the block ends.

        While marked, :meth:`put_entry` leaves its record alone, so a scan
        that reads the rewritten file before :meth:`retag` stores it cannot
        take it for a new recording and clear its analysis.  The mark is
        this process's only.
        """
        normalized = _path(path)
        with self._lock:
            self._editing[normalized] = self._editing.get(normalized, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._editing[normalized] -= 1
                if not self._editing[normalized]:
                    del self._editing[normalized]

    def retag(
        self, path: str | Path, fields: dict[str, Any], *, remember_manual: bool = True
    ) -> bool:
        """Store a recording's edited tags and its file's new identity (#53).

        Only the tags changed, not the audio, so the vector, cue points,
        prepared audio and intro lift stay.  The file's size, fingerprint,
        stale-check stamp and the loudness measurement's file stamp move to
        the rewritten file, so neither a scan nor ``autodj index`` takes it
        for a new recording and analyses it again.  A key or BPM set here is
        remembered in ``user_tags`` so a later analysis keeps it; clearing
        it hands it back to the analysis.  ``remember_manual=False`` is for
        imported embedded tags: manual BPM/key overrides stay untouched, and
        unchanged tags and file identity do not cause a database write.

        Args:
            path: The recording, rewritten on disk or left as it was.
            fields: New values for any of ``title``, ``artist``, ``album``,
                ``genre``, ``year``, ``bpm``, ``key`` and ``mode``.

        Returns:
            ``False`` when the catalog has no record for *path*.

        Raises:
            OSError: The file cannot be read; nothing is stored, so the
                record never takes an unknown identity.
        """
        if set(fields) - _RETAG_FIELDS:
            raise ValueError("only tag fields can be retagged")
        normalized = _path(path)
        # File reads stay outside the lock; the caller holds the file still.
        size, fingerprint = file_fingerprint(normalized)
        if not size or not fingerprint:
            raise OSError(f"cannot read {normalized}")
        stat = os.stat(normalized)
        with self._lock:
            db = self._db()
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT id,size,fingerprint,embedded_at,loudness,user_tags,"
                    "title,artist,album,genre,year,bpm,key,mode,mtime_ns FROM catalog_entries WHERE path=?",
                    (normalized,),
                ).fetchone()
                if row is None:
                    db.rollback()
                    return False
                manual_tags = set(_user_tags(row["user_tags"], row["fingerprint"]))
                if not remember_manual:
                    protected = manual_tags | ({"mode"} if "key" in manual_tags else set())
                    fields = {
                        name: value
                        for name, value in fields.items()
                        if name not in protected and value != row[name]
                    }
                    if not fields and (row["size"], row["fingerprint"], row["mtime_ns"]) == (
                        size,
                        fingerprint,
                        stat.st_mtime_ns,
                    ):
                        db.rollback()
                        return True
                values: dict[str, Any] = {
                    **fields,
                    "size": size,
                    "fingerprint": fingerprint,
                    "mtime_ns": stat.st_mtime_ns,
                }
                for name in _USER_TAGS:
                    if name in fields:
                        # A key is 0 to 11, -1 when cleared; a BPM 0 when cleared.
                        if remember_manual and (
                            fields[name] >= 0 if name == "key" else fields[name] > 0
                        ):
                            manual_tags.add(name)
                        else:
                            manual_tags.discard(name)
                values["user_tags"] = json.dumps(dict.fromkeys(sorted(manual_tags), fingerprint))
                if row["embedded_at"]:
                    values["embedded_at"] = stat.st_mtime
                try:
                    loudness = json.loads(row["loudness"])
                except (TypeError, json.JSONDecodeError):
                    loudness = None
                if isinstance(loudness, dict) and loudness.get("size") == row["size"]:
                    loudness.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
                    values["loudness"] = json.dumps(loudness, allow_nan=False)
                assignments = ",".join(f"{name}=?" for name in values)
                db.execute(
                    f"UPDATE catalog_entries SET {assignments} WHERE id=?",  # nosec B608 -- names checked against fixed columns above
                    (*values.values(), row["id"]),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return True

    def remember_play(
        self, path: str | Path, timestamp: float | None = None, station: str = DEFAULT_STATION
    ) -> None:
        """Record when *station* last played *path*.

        The default station keeps the time on the track's record, as
        always; any other station keeps it in ``station_plays``.
        """
        import time

        value = time.time() if timestamp is None else float(timestamp)
        if not math.isfinite(value):
            raise ValueError("timestamp must be finite")
        with self._lock:
            if station == DEFAULT_STATION:
                self._db().execute(
                    "UPDATE catalog_entries SET last_played=? WHERE path=?", (value, _path(path))
                )
            else:
                self._db().execute(
                    "INSERT INTO station_plays VALUES (?,?,?) ON CONFLICT(station, path) "
                    "DO UPDATE SET last_played=excluded.last_played",
                    (station, _path(path), value),
                )
            self._db().commit()

    def last_played(self, station: str = DEFAULT_STATION) -> dict[str, float]:
        """When *station* last played each track."""
        with self._lock:
            return _last_played(self._db(), station)

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

    def delete_setting(self, key: str) -> None:
        with self._lock:
            self._db().execute("DELETE FROM catalog_settings WHERE key=?", (key,))
            self._db().commit()

    def has_station_plays(self, station: str) -> bool:
        with self._lock:
            return (
                self._db()
                .execute("SELECT 1 FROM station_plays WHERE station=? LIMIT 1", (station,))
                .fetchone()
                is not None
            )

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
            # Other stations' last plays follow the file too.
            self._db().execute(
                "UPDATE OR REPLACE station_plays SET path=? WHERE path=?", (new_path, old_path)
            )
            self._db().commit()
            return True


class ReadOnlyCatalog(LibraryCatalog):
    """A catalog opened read-only, for a look beside a running server (``autodj simulate``).

    The connection is SQLite's ``mode=ro``: no schema set-up, no column or
    history migration, no journal-mode switch and no write lock, and any
    write method fails.  The database file itself is never written, though
    SQLite may create or touch the ``-wal`` and ``-shm`` files beside a
    write-ahead-log database, as every reader does.  A file that is not a
    catalog raises :class:`sqlite3.Error` on its first read.
    """

    def __init__(self, path: Path | str) -> None:
        """Open *path* read-only."""
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn = _connect(
            readonly_uri(self.path), uri=True, check_same_thread=False, timeout=30
        )
        self._conn.row_factory = sqlite3.Row


__all__ = ["DEFAULT_STATION", "LibraryCatalog", "ReadOnlyCatalog"]
