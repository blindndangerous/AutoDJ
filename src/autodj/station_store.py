"""Persistent play, history and setting data for one station."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from autodj.catalog import DEFAULT_STATION, LibraryCatalog, _path
from autodj.history_kinds import INSERT_KINDS, STANDALONE_KINDS
from autodj.sqlite_utils import readonly_uri

_SCHEMA = """
CREATE TABLE IF NOT EXISTS station_plays (
    station TEXT NOT NULL, path TEXT NOT NULL, last_played REAL NOT NULL,
    PRIMARY KEY(station,path)
);
CREATE TABLE IF NOT EXISTS play_history (
    event_id TEXT PRIMARY KEY NOT NULL, entry_id TEXT, path TEXT,
    title TEXT NOT NULL, artist TEXT NOT NULL, duration REAL NOT NULL,
    played_at TEXT NOT NULL, interrupted INTEGER NOT NULL DEFAULT 0,
    performances INTEGER, station TEXT NOT NULL DEFAULT 'default',
    kind TEXT NOT NULL DEFAULT 'song', ended_at TEXT,
    album TEXT NOT NULL DEFAULT '', track_kind TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS catalog_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS station_migrations (name TEXT PRIMARY KEY, complete INTEGER NOT NULL);
"""

# The rows that are songs or live sets, in SQL.
_PLAY_ROWS = "kind IN ('song','live')"
# The rows that end the song before them in a report: songs, live sets and
# what airs on its own between them, never a liner mixed over a song's end.
_BOUNDARY_ROWS = (
    "kind IN ("
    + ",".join(f"'{kind}'" for kind in sorted({"song", "live", *STANDALONE_KINDS}))
    + ")"
)


class StationStore:
    """A station database that opens and reads without the shared catalog."""

    def __init__(self, path: Path | str, station: str) -> None:
        from autodj.stations import SLUG_PATTERN

        if not SLUG_PATTERN.fullmatch(station):
            raise ValueError(f"invalid station ID: {station!r}")
        self.path = Path(path)
        self.station = station
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db: sqlite3.Connection = sqlite3.connect(
            self.path, timeout=30, check_same_thread=False
        )
        self._closed = False
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)

    def last_played(self) -> dict[str, float]:
        with self._lock:
            return {
                row[0]: float(row[1])
                for row in self._db.execute(
                    "SELECT path,last_played FROM station_plays WHERE station=?", (self.station,)
                )
            }

    def remember_play(self, path: str, timestamp: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO station_plays VALUES (?,?,?) ON CONFLICT(station,path) "
                "DO UPDATE SET last_played=excluded.last_played",
                (self.station, _path(path), timestamp),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM catalog_settings WHERE key=?", (key,)
            ).fetchone()
            return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO catalog_settings VALUES (?,?) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def delete_setting(self, key: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM catalog_settings WHERE key=?", (key,))

    def migration_complete(self, name: str) -> bool:
        with self._lock:
            return (
                self._db.execute(
                    "SELECT 1 FROM station_migrations WHERE name=? AND complete=1", (name,)
                ).fetchone()
                is not None
            )

    def mark_migration(self, name: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO station_migrations VALUES (?,1) ON CONFLICT(name) "
                "DO UPDATE SET complete=1",
                (name,),
            )

    def rekey(self, old: str | Path, new: str | Path) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE OR REPLACE station_plays SET path=? WHERE station=? AND path=?",
                (_path(new), self.station, _path(old)),
            )
            self._db.execute(
                "UPDATE play_history SET path=? WHERE path=?", (_path(new), _path(old))
            )

    def write_history(self, rows: list[dict], interrupted: list[str]) -> None:
        with self._lock, self._db:
            for row in rows:
                values = _history_values(row, self.station)
                if values is None:
                    continue
                self._db.execute(
                    "INSERT INTO play_history(event_id,path,title,artist,duration,played_at,"
                    "performances,station,kind,ended_at,album,track_kind) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET "
                    "performances=CASE WHEN excluded.performances IS NULL THEN performances "
                    "ELSE max(COALESCE(performances,0),excluded.performances) END, "
                    "duration=CASE WHEN excluded.kind='live' THEN excluded.duration ELSE duration END, "
                    "ended_at=CASE WHEN excluded.kind='live' THEN excluded.ended_at "
                    "ELSE COALESCE(ended_at,excluded.ended_at) END "
                    "WHERE excluded.performances IS NOT NULL OR excluded.kind='live' "
                    "OR excluded.ended_at IS NOT NULL",
                    values,
                )
            self._db.executemany(
                "UPDATE play_history SET interrupted=1 WHERE event_id=?",
                ((event_id,) for event_id in interrupted if isinstance(event_id, str) and event_id),
            )

    def recent_history(self, limit: int, inserts: int = 0) -> list[dict]:
        """The latest *limit* songs and live sets, oldest first.

        With *inserts*, also up to that many of the latest liners and other
        rows of :data:`~autodj.history_kinds.INSERT_KINDS`, in airing order
        among them: each has its own room, so inserted rows never push
        songs out.
        """
        _check_limit(limit)
        _check_limit(inserts)
        columns = (
            "rowid,event_id,entry_id,path,title,artist,duration,played_at,kind,ended_at,track_kind"
        )
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM (SELECT " + columns + " FROM play_history "  # nosec B608 - fixed SQL
                "WHERE interrupted=0 AND " + _PLAY_ROWS + " ORDER BY rowid DESC LIMIT ?) "
                "UNION ALL SELECT * FROM (SELECT " + columns + " FROM play_history "
                "WHERE interrupted=0 AND NOT " + _PLAY_ROWS + " ORDER BY rowid DESC LIMIT ?) "
                "ORDER BY rowid ASC",
                (limit, inserts),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            del item["rowid"]
            result.append(item)
        return result

    def recent_plays(self, limit: int) -> list[dict]:
        _check_limit(limit)
        with self._lock:
            rows = self._db.execute(
                "SELECT path,title,artist,album,played_at FROM (SELECT rowid AS row,path,title,artist,"
                "album,played_at FROM play_history WHERE interrupted=0 AND path IS NOT NULL "
                "AND kind='song' AND (track_kind='' OR track_kind='music') "
                "ORDER BY rowid DESC LIMIT ?) ORDER BY row ASC",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def play_log(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        include_inserts: bool = False,
    ) -> list[dict]:
        """Completed plays in insertion order, optionally within a report range.

        Songs and live sets only, unless *include_inserts* adds the liners,
        ads and other audio of :data:`INSERT_KINDS`.  A ranged row has the
        time the next song, live set or standalone segment started
        (``following_at``), which ends it in a report; audio mixed over the
        songs has none and keeps its length.
        """
        if (start is None) != (end is None):
            raise ValueError("Both start and end are required")
        if start is not None and end is not None and end <= start:
            raise ValueError("The end of the range must be after its start.")
        following = ""
        where = "" if include_inserts else " AND h." + _PLAY_ROWS
        if start is not None and end is not None:
            from autodj.catalog import history_start

            def in_range(value: object) -> bool:
                moment = history_start(value)
                return moment is not None and start <= moment < end

            where += " AND in_report_range(played_at)"
            following = (
                ", CASE WHEN h." + _BOUNDARY_ROWS + " THEN (SELECT n.played_at FROM play_history n "
                "WHERE n.interrupted=0 AND n." + _BOUNDARY_ROWS + " AND n.rowid>h.rowid "
                "ORDER BY n.rowid LIMIT 1) END AS following_at"
            )
        # Keep report parsing and its following-play lookup on one reader snapshot.
        with closing(sqlite3.connect(readonly_uri(self.path), uri=True, timeout=30)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            if start is not None and end is not None:
                db.create_function("in_report_range", 1, in_range)
            rows = db.execute(
                "SELECT h.played_at,h.duration,h.title,h.artist,h.path,h.album,"  # nosec B608 - fixed SQL fragments only
                "CASE WHEN h.kind!='song' THEN h.kind ELSE COALESCE(NULLIF(h.track_kind,''),h.kind) END AS kind,"
                "h.performances,h.ended_at" + following + " FROM play_history h "
                "WHERE h.interrupted=0" + where + " ORDER BY h.rowid"
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        db = getattr(self, "_db", None)
        if db is not None and not getattr(self, "_closed", True):
            db.close()
            self._closed = True

    def __del__(self) -> None:
        self.close()


def migrate_legacy(catalog: LibraryCatalog, store: StationStore) -> None:
    """Copy one station from the legacy catalog, atomically with its marker."""
    if store.migration_complete("catalog-v1"):
        return
    station = store.station
    with catalog._lock:
        source = catalog._db()
        settings = source.execute("SELECT key,value FROM catalog_settings").fetchall()
        rows = source.execute(
            "SELECT h.event_id,NULL,h.path,h.title,h.artist,h.duration,h.played_at,h.interrupted,"
            "h.performances,h.station,h.kind,h.ended_at,COALESCE(e.album,''),COALESCE(e.kind,'') "
            "FROM play_history h LEFT JOIN catalog_entries e ON e.path=h.path "
            "WHERE h.station=? ORDER BY h.rowid",
            (station,),
        ).fetchall()
        if station == DEFAULT_STATION:
            plays = source.execute("SELECT path,last_played FROM catalog_entries").fetchall()
        else:
            plays = source.execute(
                "SELECT path,last_played FROM station_plays WHERE station=?", (station,)
            ).fetchall()
    with store._lock:
        db = store._db
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute(
                "SELECT 1 FROM station_migrations WHERE name='catalog-v1' AND complete=1"
            ).fetchone():
                db.rollback()
                return
            db.executemany(
                "INSERT OR IGNORE INTO play_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [tuple(row) for row in rows],
            )
            db.executemany(
                "INSERT OR IGNORE INTO station_plays VALUES (?,?,?)",
                [(station, *tuple(row)) for row in plays],
            )
            for key, value in settings:
                name = _station_setting_name(key, station)
                if name is not None:
                    db.execute("INSERT OR IGNORE INTO catalog_settings VALUES (?,?)", (name, value))
            db.execute("INSERT INTO station_migrations VALUES ('catalog-v1',1)")
            db.commit()
        except BaseException:
            db.rollback()
            raise


def discover_stations(catalog: LibraryCatalog) -> set[str]:
    """Return every valid legacy station ID, including IDs absent from config."""
    from autodj.stations import SLUG_PATTERN

    with catalog._lock:
        db = catalog._db()
        names = {row[0] for row in db.execute("SELECT station FROM play_history")}
        names.update(row[0] for row in db.execute("SELECT DISTINCT station FROM station_plays"))
        keys = [row[0] for row in db.execute("SELECT key FROM catalog_settings")]
    for key in keys:
        if key.startswith("clock:"):
            names.add(key.removeprefix("clock:"))
    invalid = names - {
        name for name in names if isinstance(name, str) and SLUG_PATTERN.fullmatch(name)
    }
    if invalid:
        raise ValueError(f"invalid station IDs in library catalog: {sorted(map(str, invalid))}")
    return names


def discover_station_folders(directory: Path | str) -> set[str]:
    """Return station IDs whose databases already live under the station root."""
    from autodj.stations import SLUG_PATTERN

    root = Path(directory)
    return (
        {
            path.name
            for path in root.iterdir()
            if path.is_dir()
            and SLUG_PATTERN.fullmatch(path.name)
            and (path / "station.db").is_file()
        }
        if root.is_dir()
        else set()
    )


def _history_values(row: dict, station: str) -> tuple | None:
    event_id, path = row.get("event_id"), row.get("path")
    kind = row.get("kind")
    live = kind == "live"
    # Live sets and inserted audio, such as a time check, may have no file.
    pathless = live or kind in INSERT_KINDS
    duration = row.get("duration")
    if duration is None:
        return None
    try:
        duration = float(duration)
    except (TypeError, ValueError, OverflowError):
        return None
    played_at = row.get("played_at")
    if (
        not isinstance(event_id, str)
        or not event_id
        or (not pathless and (not isinstance(path, str) or not path))
        or (path is not None and not isinstance(path, str))
        or not isinstance(row.get("title"), str)
        or not isinstance(row.get("artist"), str)
        or isinstance(row.get("duration"), bool)
        or not math.isfinite(duration)
        or duration < 0
        or not isinstance(played_at, str)
    ):
        return None
    try:
        datetime.fromisoformat(played_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    ended_at = row.get("ended_at")
    try:
        if not isinstance(ended_at, str):
            ended_at = None
        else:
            datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
    except ValueError:
        ended_at = None
    performances = row.get("performances")
    if isinstance(performances, bool) or not isinstance(performances, int) or performances < 0:
        performances = None
    return (
        event_id,
        _path(path) if path else None,
        row["title"],
        row["artist"],
        duration,
        played_at,
        performances,
        station,
        kind if live or kind in INSERT_KINDS else "song",
        ended_at,
        row.get("album") or "",
        row.get("track_kind") or "",
    )


def _station_setting_name(key: str, station: str) -> str | None:
    if station == DEFAULT_STATION:
        return key if key == "clock" else None
    return "clock" if key == f"clock:{station}" else None


def _check_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
