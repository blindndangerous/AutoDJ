"""SQLite transaction and URI helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from urllib.parse import quote


def readonly_uri(path: Path | str, *, immutable: bool = False) -> str:
    """Build a read-only SQLite URI for *path*.

    The path is percent-encoded because SQLite parses ``?`` and ``#`` in a
    URI as query and fragment separators: a library living under a directory
    with either character (or a Windows UNC share) otherwise opens as "unable
    to open database file".

    Args:
        path: Filesystem path to the database.
        immutable: Add ``immutable=1`` for a snapshot that cannot change.

    Returns:
        A ``file:`` URI suitable for ``sqlite3.connect(..., uri=True)``.
    """
    encoded = quote(str(Path(path).resolve()), safe="/:\\")
    suffix = "&immutable=1" if immutable else ""
    return f"file:{encoded}?mode=ro{suffix}"


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Run a multi-statement mutation as one explicit SQLite transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        with suppress(BaseException):
            conn.rollback()
        raise
