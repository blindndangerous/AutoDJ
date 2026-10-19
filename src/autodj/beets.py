"""Beets music library database reader.

Reads track metadata from a beets ``library.db`` SQLite database without
requiring beets itself to be installed at runtime. Beets stores file paths
as UTF-8-encoded bytes, which this module decodes transparently.

Example:
    >>> from autodj.beets import search_tracks
    >>> tracks = search_tracks("C:/Users/you/.config/beets/library.db", "Portishead")
    >>> print(tracks[0].display_name)
    Portishead — Mysterons
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autodj.sqlite_utils import readonly_uri

_connect = sqlite3.connect

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
    initial_key: str = ""
    lyrics: str = ""

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


# ---------------------------------------------------------------------------
# Beets ``initial_key`` parsing — the keyfinder / acousticbrainz plugins
# write a free-text key string; we parse it so the indexer can prefer it
# over the librosa-detected key.
# ---------------------------------------------------------------------------

_NOTE_TO_KEY: dict[str, int] = {
    "C": 0,
    "C#": 1,
    "Db": 1,
    "D": 2,
    "D#": 3,
    "Eb": 3,
    "E": 4,
    "Fb": 4,
    "F": 5,
    "E#": 5,
    "F#": 6,
    "Gb": 6,
    "G": 7,
    "G#": 8,
    "Ab": 8,
    "A": 9,
    "A#": 10,
    "Bb": 10,
    "B": 11,
    "Cb": 11,
}


def _parse_camelot_key(raw: str) -> tuple[int, int] | None:
    """Parse a Camelot-format key (e.g. ``"8A"`` / ``"8B"``); return ``None`` when not Camelot."""
    cam = raw.upper().replace(" ", "")
    if not (len(cam) >= 2 and cam[-1] in ("A", "B") and cam[:-1].isdigit()):
        return None
    num = int(cam[:-1])
    side = cam[-1]
    if not (1 <= num <= 12):
        return None
    from autodj.dj_meta import _CAMELOT_MAJOR, _CAMELOT_MINOR

    table = _CAMELOT_MAJOR if side == "B" else _CAMELOT_MINOR
    # Both tables cover Camelot 1-12 exactly once, so the reverse lookup
    # always hits for the numbers accepted above.
    by_number = {n: chromatic for chromatic, n in table.items()}
    return (by_number[num], 1 if side == "B" else 0)


def _split_note_and_mode(raw: str) -> tuple[str, int]:
    """Split a textual key into ``(note, mode)`` where mode is 1=major / 0=minor."""
    lower = raw.lower()
    if "major" in lower:
        return lower.replace("major", "").strip(), 1
    if "minor" in lower:
        return lower.replace("minor", "").strip(), 0
    if raw.endswith("m") and len(raw) >= 2:
        return raw[:-1].strip(), 0
    return raw.strip(), 1


def parse_initial_key(s: str) -> tuple[int, int] | None:
    """Parse a beets ``initial_key`` text value into ``(key 0–11, mode)``."""
    if not s:
        return None
    raw = s.strip()
    if not raw:
        return None
    cam = _parse_camelot_key(raw)
    if cam is not None:
        return cam
    note_part, mode = _split_note_and_mode(raw)
    if not note_part:
        return None
    note = note_part[0].upper() + note_part[1:]
    if note in _NOTE_TO_KEY:
        return (_NOTE_TO_KEY[note], mode)
    return None


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


def _row_get(row: sqlite3.Row, key: str, default: object = None) -> object:
    """Safe row[key] that returns *default* when the column is absent.

    sqlite3.Row raises ``IndexError`` on unknown columns, but we want a
    graceful fallback so users without the keyfinder/lyrics/etc. plugins
    don't blow up.
    """
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _row_to_track(row: sqlite3.Row) -> Track:
    """Convert a SQLite row from the beets ``items`` table to a :class:`Track`.

    Reads the eight always-present columns plus two optional plugin
    columns (``initial_key``, ``lyrics``) when they exist.  Missing
    optional columns are silently treated as empty strings — keeps
    non-plugin beets installs working.

    Args:
        row: A :class:`sqlite3.Row` with at least the core beets columns.

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
        initial_key=str(_row_get(row, "initial_key", "") or ""),
        lyrics=str(_row_get(row, "lyrics", "") or ""),
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
    conn = _connect(readonly_uri(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # Trigger a real read so we fail fast if the file isn't a valid SQLite DB.
        conn.execute("SELECT 1")
    except sqlite3.DatabaseError:
        conn.close()
        raise
    return conn


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_CORE_COLS = ("path", "title", "artist", "album", "genre", "bpm", "year", "length")
_OPTIONAL_COLS = ("initial_key", "lyrics")


def _items_columns(conn: sqlite3.Connection) -> set[str]:
    """Return the set of column names present in the beets ``items`` table."""
    return {row[1] for row in conn.execute("PRAGMA table_info(items)")}


def _select_clause(present_cols: set[str]) -> str:
    """Build a column list for SELECT — always cores, optionals when present."""
    cols = list(_CORE_COLS) + [c for c in _OPTIONAL_COLS if c in present_cols]
    return ", ".join(cols)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    """Whether the database has a table called *name*."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def item_fields(db_path: str | Path) -> frozenset[str]:
    """Every field name beets keeps for items: its columns and its flexible attributes.

    Flexible attributes are the fields plugins and ``beet modify`` add
    (``mood``, ``rating`` and so on); beets keeps them in the
    ``item_attributes`` table, one row per item and field.

    Raises:
        BeetsNotFoundError: If the database file does not exist.
        sqlite3.DatabaseError: If the file is not a beets database.
    """
    conn = _open_db(Path(db_path))
    try:
        names = set(_items_columns(conn))
        if _has_table(conn, "item_attributes"):
            names.update(row[0] for row in conn.execute("SELECT DISTINCT key FROM item_attributes"))
        return frozenset(str(name) for name in names if name)
    finally:
        conn.close()


def _quoted(name: str) -> str:
    """Quote a column name for SQL (it comes from the table's own PRAGMA)."""
    return '"' + name.replace('"', '""') + '"'


def read_items(db_path: str | Path, fields: Iterable[str]) -> list[tuple[Path, dict[str, Any]]]:
    """Return each item's path and the values of *fields* that it has.

    Only the asked-for fields are read, so a query on two fields does not
    load every column (lyrics included) of a large library.  A field may
    be a column of the ``items`` table (``original_year``, ``genre``) or a
    flexible attribute; an item without a flexible attribute leaves it
    out of its dict.  The database is opened read-only, never written.

    Args:
        db_path: Path to the beets ``library.db`` SQLite file.
        fields: Field names to read; unknown names are ignored.

    Returns:
        ``(path, values)`` for every item, in database order.  The path is
        as beets stores it: absolute, or relative to its music directory.

    Raises:
        BeetsNotFoundError: If the database file does not exist.
        sqlite3.DatabaseError: If the file is not a beets database.
    """
    wanted = {str(name) for name in fields} - {"path", "id"}
    conn = _open_db(Path(db_path))
    try:
        columns = _items_columns(conn)
        picked = sorted(wanted & columns)
        flexible = sorted(wanted - columns)
        select = ", ".join(["id", "path", *(_quoted(name) for name in picked)])
        # Every name in *picked* is a column the table's own PRAGMA listed.
        rows = conn.execute(f"SELECT {select} FROM items").fetchall()  # nosec B608
        items: dict[int, tuple[Path, dict[str, Any]]] = {}
        for row in rows:
            values = {name: row[name] for name in picked}
            items[int(row["id"])] = (_decode_path(row["path"]), values)
        if flexible and _has_table(conn, "item_attributes"):
            marks = ", ".join("?" for _ in flexible)
            # Only placeholders are joined in; the keys are parameters.
            sql = f"SELECT entity_id, key, value FROM item_attributes WHERE key IN ({marks})"  # nosec B608
            for entity, key, value in conn.execute(sql, flexible):
                item = items.get(int(entity))
                if item is not None:
                    item[1][str(key)] = value
        return list(items.values())
    finally:
        conn.close()


def get_lyrics_for_path(
    db_path: str | Path,
    track_path: str,
    music_dir: Path | None = None,
) -> str:
    """Return the beets-stored lyrics for a single track, or empty string.

    Used by the player as a fallback when no ``.lrc`` sidecar exists.

    Beets stores paths in one of two forms depending on version:
    - **Absolute** (older beets / non-``relative_path`` setups)
    - **Relative-to-library-directory** (modern beets default)

    To handle both, the lookup first tries the absolute form, then the
    suffix relative to *music_dir* if provided.  In practice this is one
    extra round-trip on miss — cheap.

    Args:
        db_path: Path to the beets ``library.db`` SQLite file.
        track_path: Absolute path of the track at runtime (whatever the
            player resolved from the index).
        music_dir: Beets library root, used to strip the prefix when
            checking the relative-path form.  ``None`` skips that fallback.

    Returns:
        The lyrics string or ``""`` if not found / column absent.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return ""
    try:
        conn = _open_db(db_path)
    except (sqlite3.DatabaseError, BeetsNotFoundError):
        return ""
    try:
        cols = _items_columns(conn)
        if "lyrics" not in cols:
            return ""
        # Try absolute path first (beets historic / non-relative setups)
        for candidate in _path_candidates(track_path, music_dir):
            row = conn.execute(
                "SELECT lyrics FROM items WHERE path = ? LIMIT 1",
                (candidate.encode("utf-8"),),
            ).fetchone()
            if row and row["lyrics"]:
                return str(row["lyrics"])
        return ""
    except sqlite3.DatabaseError:
        # Corrupt / not-a-database file slips past _open_db when SQLite
        # fails lazily on the first PRAGMA — fall back to empty so the
        # caller's lyrics-lookup never explodes.
        return ""
    finally:
        conn.close()


def _path_candidates(track_path: str, music_dir: Path | None) -> list[str]:
    """Return possible beets-stored representations of *track_path*.

    Returns the absolute form first, then a relative-to-*music_dir* form
    (forward-slashed, then back-slashed) when *track_path* is under
    *music_dir*.  Used to look up a track by its on-disk path regardless
    of whether the beets installation stores absolute or relative paths.
    """
    candidates: list[str] = [track_path]
    if music_dir is None:
        return candidates
    try:
        rel = Path(track_path).resolve().relative_to(Path(music_dir).resolve())
    except (ValueError, OSError):
        return candidates
    rel_str = str(rel)
    candidates.append(rel_str)
    if "\\" in rel_str:
        candidates.append(rel_str.replace("\\", "/"))
    elif "/" in rel_str:
        candidates.append(rel_str.replace("/", "\\"))
    return candidates


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
        cols = _items_columns(conn)
        # _select_clause is a hardcoded column whitelist — not user input.
        select = _select_clause(cols)
        sql = f"SELECT {select} FROM items WHERE title LIKE ? OR artist LIKE ? OR album LIKE ?"  # nosec B608
        rows = conn.execute(sql, (pattern, pattern, pattern)).fetchall()
        return [_row_to_track(r) for r in rows]
    finally:
        conn.close()
