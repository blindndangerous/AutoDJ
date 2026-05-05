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


def parse_initial_key(s: str) -> tuple[int, int] | None:
    """Parse a beets ``initial_key`` text value into ``(key 0–11, mode)``.

    Accepts the common formats written by beets plugins, MP3Tag, and
    DJ software:

    - Bare note → major: ``"C"``, ``"D#"``, ``"Bb"``
    - Trailing ``m`` → minor: ``"Cm"``, ``"D#m"``, ``"Bbm"``
    - Spelled out: ``"C major"``, ``"A minor"`` (case-insensitive)
    - Camelot codes: ``"8A"``, ``"8B"`` (any valid 1-12 + A/B)

    Args:
        s: Raw text value.  ``None`` and ``""`` return ``None``.

    Returns:
        ``(key, mode)`` where ``mode`` is 1 = major, 0 = minor, or
        ``None`` if the string cannot be parsed.
    """
    if not s:
        return None
    raw = s.strip()
    if not raw:
        return None

    # Camelot form first
    cam = raw.upper().replace(" ", "")
    if len(cam) >= 2 and cam[-1] in ("A", "B") and cam[:-1].isdigit():
        num = int(cam[:-1])
        side = cam[-1]
        if 1 <= num <= 12:
            from autodj.dj_meta import _CAMELOT_MAJOR, _CAMELOT_MINOR

            table = _CAMELOT_MAJOR if side == "B" else _CAMELOT_MINOR
            for chromatic, n in table.items():
                if n == num:
                    return (chromatic, 1 if side == "B" else 0)
            return None

    # Word form
    lower = raw.lower()
    mode: int
    if "major" in lower:
        mode = 1
        note_part = lower.replace("major", "").strip()
    elif "minor" in lower:
        mode = 0
        note_part = lower.replace("minor", "").strip()
    elif raw.endswith("m") and len(raw) >= 2:
        mode = 0
        note_part = raw[:-1].strip()
    else:
        mode = 1
        note_part = raw.strip()

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
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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


def get_all_tracks(db_path: str | Path) -> list[Track]:
    """Return every track in the beets library database.

    Reads the eight core fields plus two optional plugin fields
    (``initial_key`` and ``lyrics``) if those columns exist in the
    schema.  Older beets installs without those plugins are handled
    transparently — the optional fields default to empty strings.

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
        cols = _items_columns(conn)
        # _select_clause returns a hardcoded internal column whitelist.
        sql = f"SELECT {_select_clause(cols)} FROM items"  # nosec B608
        rows = conn.execute(sql).fetchall()
        return [_row_to_track(r) for r in rows]
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
