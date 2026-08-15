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
