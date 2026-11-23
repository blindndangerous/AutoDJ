"""Consistent snapshots used to create an independent library copy."""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from autodj.backup import _check_catalog, _single_link, _snapshot_database, _within_root

_COPIED_FILES = ("dj_meta.db", "related-artists.toml", "related-tracks.toml")
_COPIED_SETTINGS = ("auto_rescan_minutes", "music_fade_drop_db", "advanced_max_minutes")
_TRANSIENT_TABLES = ("analysis_leases", "analysis_workers", "scan_pending", "scan_workers")


def snapshot_library(source: Path, stage: Path) -> None:
    """Copy persistent catalog state into *stage*, excluding station/transient state.

    SQLite's online backup API includes committed WAL data. Files are validated
    against the source library root before they are read.
    """
    database = source / "library.db"
    _snapshot_database(database, stage / "library.db")
    for name in _COPIED_FILES:
        original = source / name
        if not original.exists():
            continue
        _within_root(original, source)
        if not original.is_file():
            raise OSError(f"library state is not a regular file: {original}")
        _single_link(original)
        target = stage / name
        if name.endswith(".db"):
            _snapshot_database(original, target)
        else:
            shutil.copyfile(original, target)
            shutil.copymode(original, target)

    with closing(sqlite3.connect(stage / "library.db")) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        conn.execute("BEGIN IMMEDIATE")
        for table in _TRANSIENT_TABLES:
            if table in tables:
                # Table names come only from the fixed internal tuple above.
                conn.execute(f'DELETE FROM "{table}"')  # nosec B608
        conn.execute("DELETE FROM play_history")
        conn.execute("DELETE FROM station_plays")
        conn.execute("UPDATE catalog_entries SET last_played=0 WHERE last_played!=0")
        rows = conn.execute(
            "SELECT key, value FROM catalog_settings WHERE key IN (?, ?, ?)",
            _COPIED_SETTINGS,
        ).fetchall()
        conn.execute("DELETE FROM catalog_settings")
        conn.executemany("INSERT INTO catalog_settings(key, value) VALUES (?, ?)", rows)
        conn.commit()
    # Reclaim pages freed when potentially large station history was removed.
    with closing(sqlite3.connect(stage / "library.db")) as conn:
        if conn.execute("PRAGMA freelist_count").fetchone()[0]:
            conn.execute("VACUUM")
    _check_catalog(stage / "library.db")
