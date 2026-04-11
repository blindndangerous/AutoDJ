"""Beets music library database reader.

Reads track metadata from a beets ``library.db`` SQLite database without
requiring beets itself to be installed at runtime. Beets stores file paths
as UTF-8-encoded bytes, which this module decodes transparently.

If the beets database is unavailable, callers should fall back to
:func:`autodj.indexer.walk_music_dir`.

Example:
    >>> from autodj.beets import get_all_tracks
    >>> tracks = get_all_tracks("C:/Users/you/.config/beets/library.db")
    >>> print(tracks[0].display_name)
    Portishead — Mysterons
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BeetsNotFoundError(FileNotFoundError):
    """Raised when the beets library database file does not exist."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class Track:
    """Metadata for a single track from the beets library.

    Attributes:
        path: Absolute path to the audio file on disk.
        title: Track title.
        artist: Track artist.
        album: Album name.
        genre: Genre string (may be empty).
        bpm: Beats per minute as analysed by beets (0.0 if unknown).
        year: Release year (0 if unknown).
        length: Duration in seconds (0.0 if unknown).
    """

    path: Path
    title: str
    artist: str
    album: str
    genre: str
    bpm: float
    year: int
    length: float

    @property
    def display_name(self) -> str:
        """Human-readable track name for UI display.

        Returns:
            ``"Artist — Title"`` when an artist is present, or just ``"Title"``
            when the artist field is empty.
        """
        if self.artist:
            return f"{self.artist} \u2014 {self.title}"
        return self.title


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _decode_path(raw: bytes | str) -> Path:
    """Decode a beets path column value to a :class:`~pathlib.Path`.

    Beets stores paths as UTF-8 bytes in some versions and as plain strings
    in others.  This function handles both transparently.

    Args:
        raw: The raw value from the SQLite ``path`` column.

    Returns:
        A :class:`~pathlib.Path` representing the audio file location.
    """
    if isinstance(raw, bytes):
        return Path(raw.decode("utf-8"))
    return Path(raw)


def _row_to_track(row: sqlite3.Row) -> Track:
    """Convert a SQLite row from the beets ``items`` table to a :class:`Track`.

    Args:
        row: A :class:`sqlite3.Row` with columns matching the beets schema.

    Returns:
        A populated :class:`Track` instance.
    """
    return Track(
        path=_decode_path(row["path"]),
        title=row["title"] or "",
        artist=row["artist"] or "",
        album=row["album"] or "",
        genre=row["genre"] or "",
        bpm=float(row["bpm"] or 0.0),
        year=int(row["year"] or 0),
        length=float(row["length"] or 0.0),
    )


def _open_db(db_path: Path) -> sqlite3.Connection:
    """Open the beets SQLite database in read-only mode.

    Args:
        db_path: Path to the ``library.db`` file.

    Returns:
        An open :class:`sqlite3.Connection`.

    Raises:
        BeetsNotFoundError: If the file does not exist.
        sqlite3.DatabaseError: If the file is not a valid SQLite database.
    """
    if not db_path.exists():
        raise BeetsNotFoundError(
            f"Beets library not found: {db_path}\n"
            "Set [library] beets_db in config.toml or leave it blank to use "
            "filesystem scanning instead."
        )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # Trigger a real read so we fail fast if the file isn't a valid SQLite DB.
    conn.execute("SELECT 1")
    return conn


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_all_tracks(db_path: str | Path) -> list[Track]:
    """Return every track in the beets library database.

    Args:
        db_path: Path to the beets ``library.db`` SQLite file.

    Returns:
        A list of :class:`Track` objects, one per row in the beets ``items``
        table.  Returns an empty list if the library has no tracks.

    Raises:
        BeetsNotFoundError: If the database file does not exist.
        sqlite3.DatabaseError: If the file is corrupt or not a SQLite database.

    Example:
        >>> tracks = get_all_tracks("~/.config/beets/library.db")
        >>> len(tracks)
        12483
    """
    db_path = Path(db_path)
    conn = _open_db(db_path)
    try:
        rows = conn.execute(
            "SELECT path, title, artist, album, genre, bpm, year, length FROM items"
        ).fetchall()
        return [_row_to_track(r) for r in rows]
    finally:
        conn.close()


def search_tracks(db_path: str | Path, query: str) -> list[Track]:
    """Search tracks by a free-text query matched against title, artist, and album.

    The search is case-insensitive and matches partial strings (substring match).

    Args:
        db_path: Path to the beets ``library.db`` SQLite file.
        query: Search string to match against title, artist, and album fields.

    Returns:
        A list of matching :class:`Track` objects, ordered by the database
        row order (typically insertion order).  Returns an empty list when
        nothing matches.

    Raises:
        BeetsNotFoundError: If the database file does not exist.
        sqlite3.DatabaseError: If the file is corrupt or not a SQLite database.

    Example:
        >>> results = search_tracks("library.db", "Portishead")
        >>> results[0].display_name
        'Portishead — Mysterons'
    """
    db_path = Path(db_path)
    conn = _open_db(db_path)
    pattern = f"%{query}%"
    try:
        rows = conn.execute(
            """
            SELECT path, title, artist, album, genre, bpm, year, length
            FROM items
            WHERE title LIKE ? OR artist LIKE ? OR album LIKE ?
            """,
            (pattern, pattern, pattern),
        ).fetchall()
        return [_row_to_track(r) for r in rows]
    finally:
        conn.close()
