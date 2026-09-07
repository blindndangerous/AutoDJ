from __future__ import annotations

import sqlite3

import pytest

from autodj.sqlite_utils import immediate_transaction


def test_immediate_transaction_rolls_back_after_destructive_statement() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE items (value TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO items VALUES ('before')")

    with (
        pytest.raises(RuntimeError, match="injected"),
        immediate_transaction(conn),
    ):
        conn.execute("DELETE FROM items")
        conn.execute("INSERT INTO items VALUES ('partial')")
        raise RuntimeError("injected")

    assert conn.execute("SELECT value FROM items").fetchall() == [("before",)]
    conn.close()


def test_immediate_transaction_rolls_back_when_commit_fails() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE children ("
        "parent_id INTEGER REFERENCES parents(id) DEFERRABLE INITIALLY DEFERRED)"
    )

    try:
        with (
            pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"),
            immediate_transaction(conn),
        ):
            conn.execute("INSERT INTO children VALUES (1)")

        assert not conn.in_transaction
        assert conn.execute("SELECT parent_id FROM children").fetchall() == []
    finally:
        conn.close()


def test_readonly_uri_escapes_characters_sqlite_would_parse() -> None:
    from pathlib import Path

    from autodj.sqlite_utils import readonly_uri

    uri = readonly_uri(Path("C:/music/best of #1/library.db"))
    assert "%23" in uri
    assert uri.endswith("?mode=ro")
    assert "immutable" not in uri


def test_readonly_uri_can_request_an_immutable_snapshot() -> None:
    from autodj.sqlite_utils import readonly_uri

    assert readonly_uri("library.db", immutable=True).endswith("?mode=ro&immutable=1")


def test_beets_opens_a_library_under_a_fragment_character(tmp_path) -> None:
    """``#`` in a directory name used to read as a URI fragment and fail."""
    from autodj.beets import _open_db

    folder = tmp_path / "best of #1"
    folder.mkdir()
    db = folder / "library.db"
    created = sqlite3.connect(db)
    created.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
    created.commit()
    created.close()

    conn = _open_db(db)
    try:
        assert tuple(conn.execute("SELECT 1").fetchone()) == (1,)
    finally:
        conn.close()
