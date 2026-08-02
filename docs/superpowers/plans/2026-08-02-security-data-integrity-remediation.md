# Security and Data Integrity Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make index/cache publication crash-safe, constrain liner files to their configured root, and require authenticated, origin-checked access whenever the web server is exposed beyond loopback.

**Architecture:** Explicit SQLite transactions and stable `vec_row` identities separate cheap metadata checkpoints from intentional full reorder/prune operations. A durable pointer manifest references immutable generation-specific SQLite/FAISS files by SHA-256; readers validate the same manifest before and after loading and swap one candidate under a lock. Model caches and liner uploads use staged atomic promotion, while a new `ServerConfig` and security middleware derive bind policy from the effective host/port and enforce signed sessions, host/origin checks, request IDs, and audit events.

**Tech Stack:** Python 3.14, sqlite3, FAISS, FastAPI/Starlette, Click, Hugging Face Hub, pytest/pytest-asyncio, Vitest, standard-library HMAC/secrets/ipaddress/pathlib.

---

## File structure

- Create `src/autodj/sqlite_utils.py`: one explicit `BEGIN IMMEDIATE` transaction primitive.
- Create `src/autodj/index_manifest.py`: manifest schema, durable read/write, and consistency errors.
- Create `src/autodj/liner_files.py`: filename containment and streamed atomic upload.
- Create `src/autodj/security.py`: bind validation, sessions, host/origin enforcement, request IDs, and audit logging.
- Create `src/autodj/static/modules/auth.js`: accessible token-login interaction.
- Create `tests/unit/test_sqlite_utils.py`, `tests/unit/test_index_manifest.py`, `tests/unit/test_liner_files.py`, `tests/unit/test_security.py`, and `tests/jsmodules/auth.test.js`.
- Modify `src/autodj/indexer.py`: stable vector rows, transactional full replacement, delta UPSERT, manifest publication.
- Modify `src/autodj/dj_meta.py`: transactional mutations and deterministic close/context management.
- Modify `src/autodj/similarity.py`, `src/autodj/_bridge.py`, and `src/autodj/server.py`: coherent reload and locked readers.
- Modify `src/autodj/model.py`: inspected cache invariant, staged download, process lock, atomic promotion.
- Modify `src/autodj/config.py`, `src/autodj/cli.py`, and `config.toml.example`: `ServerConfig` and ordinary TOML/CLI security settings. The later delivery plan depends on this API and may add no-config startup and generalized environment overlay; this plan must not add that overlay.
- Modify `src/autodj/static/index.html` and `src/autodj/static/app.js`: login dialog and 401 bootstrap.
- Modify existing focused tests under `tests/unit/`, `tests/integration/`, and `tests/smoke/` to cover failure injection and secured route behavior.
- Modify `README.md`: loopback, authenticated LAN, and insecure acknowledgement usage. Do not modify `compose.yaml`; container defaults belong to the later delivery plan.

## Specification coverage map

- Storage transactions: Tasks 1-3 provide explicit rollback, deterministic DJ-cache closure,
  stable vector rows, and incremental UPSERTs.
- Coherent index publication: Tasks 4-5 provide immutable artifacts, SHA-256 and count validation,
  last-valid snapshot retention, retry semantics, and one reader/reload lock.
- Model cache durability: Task 6 provides the exact shared inspection API, completion invariant,
  one-download lock, staging cleanup, and atomic promotion.
- Liner filesystem boundary: Tasks 7-8 provide one path helper, bounded streaming, atomic
  replacement, status mapping, and the configurable 50 MiB default.
- Network authentication: Tasks 8-11 provide `ServerConfig`, direct-server bind enforcement,
  constant-time login, signed cookie sessions, Host/Origin enforcement, pre-accept WebSocket
  checks, request IDs, audit records, and the accessible browser login.
- Error handling: Each subsystem task includes injected failure/status regressions and an exact
  user-visible or logged failure contract.
- Testing: Every production change in Tasks 1-11 starts red, records the expected failure, runs a
  focused green check, and ends with a scoped commit; Task 12 runs the complete matrix.
- Out of scope: Task 12 documents the trusted-private-network boundary; generic environment
  overlay, Compose, multi-user identity, roles, and public-internet hosting remain excluded.

### Task 1: Explicit SQLite transaction primitive and tracks rollback

**Files:**
- Create: `src/autodj/sqlite_utils.py`
- Create: `tests/unit/test_sqlite_utils.py`
- Modify: `src/autodj/indexer.py:167-250`
- Test: `tests/unit/test_indexer_more.py:439-486`

- [ ] **Step 1: Write failing transaction and tracks rollback tests**

```python
# tests/unit/test_sqlite_utils.py
from __future__ import annotations

import sqlite3

import pytest

from autodj.sqlite_utils import immediate_transaction


def test_immediate_transaction_rolls_back_after_destructive_statement() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE items (value TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO items VALUES ('before')")

    with pytest.raises(RuntimeError, match="injected"):
        with immediate_transaction(conn):
            conn.execute("DELETE FROM items")
            conn.execute("INSERT INTO items VALUES ('partial')")
            raise RuntimeError("injected")

    assert conn.execute("SELECT value FROM items").fetchall() == [("before",)]
    conn.close()
```

Append this regression to `TestSaveIndexErrorPaths`:

```python
def test_replace_tracks_rows_rolls_back_mid_batch(self, tmp_path: Path) -> None:
    import sqlite3

    from autodj.indexer import _open_tracks_db, _replace_tracks_rows

    original, _ = self._entries_vectors(2)
    replacement, _ = self._entries_vectors(3)
    replacement[1].path = "Z:/explode.flac"
    conn = _open_tracks_db(tmp_path)
    try:
        _replace_tracks_rows(conn, original, music_dir=None)
        conn.execute(
            "CREATE TRIGGER reject_explode BEFORE INSERT ON tracks "
            "WHEN NEW.path = 'Z:/explode.flac' BEGIN "
            "SELECT RAISE(ABORT, 'injected'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            _replace_tracks_rows(conn, replacement, music_dir=None)
        paths = conn.execute("SELECT path FROM tracks ORDER BY id").fetchall()
    finally:
        conn.close()
    assert paths == [(original[0].path,), (original[1].path,)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_sqlite_utils.py tests/unit/test_indexer_more.py::TestSaveIndexErrorPaths::test_replace_tracks_rows_rolls_back_mid_batch -q`

Expected: FAIL because `autodj.sqlite_utils` does not exist; on the current implementation the tracks test would leave a partial table because `isolation_level=None` makes `with conn:` non-transactional.

- [ ] **Step 3: Add the explicit transaction and use it for full tracks replacement**

```python
# src/autodj/sqlite_utils.py
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Run a multi-statement mutation as one explicit SQLite transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
```

Replace `_replace_tracks_rows`' mutation block in `src/autodj/indexer.py` and add the import:

```python
from autodj.sqlite_utils import immediate_transaction


def _replace_tracks_rows(
    conn: sqlite3.Connection,
    entries: list[IndexEntry],
    music_dir: Path | None,
) -> None:
    """Atomically replace rows when vector order intentionally changes."""
    rows = [_entry_to_row(e, music_dir) for e in entries]
    with immediate_transaction(conn):
        conn.execute("DELETE FROM tracks")
        if rows:
            conn.executemany(_TRACKS_INSERT_SQL, rows)
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `uv run pytest tests/unit/test_sqlite_utils.py tests/unit/test_indexer_more.py::TestSaveIndexErrorPaths::test_replace_tracks_rows_rolls_back_mid_batch -q`

Expected: `2 passed` and no partial rows.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/sqlite_utils.py src/autodj/indexer.py tests/unit/test_sqlite_utils.py tests/unit/test_indexer_more.py
git commit -m "fix: make track replacements transactional"
```

### Task 2: Transactional and deterministically closed DJ metadata cache

**Files:**
- Modify: `src/autodj/dj_meta.py:627-817`
- Modify: `src/autodj/server.py:296-331`
- Modify: `tests/conftest.py`
- Test: `tests/unit/test_dj_meta.py:199-410`
- Test: `tests/unit/test_dj_cues.py:199-238`

- [ ] **Step 1: Write failing rollback and context-manager tests**

Add to `TestDjMetaCache`:

```python
def test_flush_rolls_back_mid_batch(self, tmp_path) -> None:
    path = tmp_path / "cache.db"
    with DjMetaCache(path) as cache:
        cache.set("before.flac", DjMeta(analysed=True))
        cache.flush(force=True)
        assert cache._conn is not None
        cache._conn.execute(
            "CREATE TRIGGER reject_bad BEFORE INSERT ON dj_meta "
            "WHEN NEW.path = 'bad.flac' BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        cache.set("good.flac", DjMeta(analysed=True))
        cache.set("bad.flac", DjMeta(analysed=True))
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            cache.flush(force=True)
        rows = cache._conn.execute("SELECT path FROM dj_meta ORDER BY path").fetchall()
        assert rows == [("before.flac",)]


def test_context_manager_closes_connection(self, tmp_path) -> None:
    cache = DjMetaCache(tmp_path / "cache.db")
    with cache:
        assert cache._conn is not None
    assert cache._conn is None


def test_close_cache_flushes_closes_and_clears_singleton(tmp_path) -> None:
    from autodj.dj_meta import close_cache, get_cache

    cache = get_cache(tmp_path)
    assert cache is not None
    cache.set("pending.flac", DjMeta(analysed=True))
    close_cache()
    assert cache._conn is None
    assert get_cache() is None

    reopened = DjMetaCache(tmp_path / "dj_meta.db")
    try:
        assert reopened.get("pending.flac").analysed is True
    finally:
        reopened.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_dj_meta.py::TestDjMetaCache::test_flush_rolls_back_mid_batch tests/unit/test_dj_meta.py::TestDjMetaCache::test_context_manager_closes_connection -q`

Expected: FAIL because `DjMetaCache` has no context-manager protocol and batched UPSERTs autocommit.

- [ ] **Step 3: Make every multi-statement cache mutation explicit and add context management**

Add `from types import TracebackType` and
`import atexit`, plus `from autodj.sqlite_utils import immediate_transaction`. Add the context-manager methods and
replace the three mutation methods with these complete definitions:

```python
def __enter__(self) -> DjMetaCache:
    return self


def __exit__(
    self,
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    traceback: TracebackType | None,
) -> None:
    try:
        if exc_type is None:
            self.flush(force=True)
    finally:
        self.close()


def _migrate_legacy_keys(self) -> None:
    assert self._conn is not None
    rows = self._conn.execute(
        "SELECT path, intro_end_s, outro_start_s, analysed, beats, cues FROM dj_meta"
    ).fetchall()
    moved = 0
    removed = 0
    with immediate_transaction(self._conn):
        for row in rows:
            old = str(row[0])
            new = self._key(old)
            if new == old:
                continue
            target_row = self._conn.execute(
                "SELECT analysed FROM dj_meta WHERE path = ?", (new,)
            ).fetchone()
            if target_row is None:
                self._conn.execute("UPDATE dj_meta SET path = ? WHERE path = ?", (new, old))
                moved += 1
                continue
            if not bool(target_row[0]) and bool(row[3]):
                self._conn.execute(
                    "UPDATE dj_meta SET intro_end_s = ?, outro_start_s = ?, "
                    "analysed = ?, beats = ?, cues = ? WHERE path = ?",
                    (row[1], row[2], row[3], row[4], row[5], new),
                )
            self._conn.execute("DELETE FROM dj_meta WHERE path = ?", (old,))
            removed += 1
    if moved or removed:
        logger.info(
            "Migrated DJ meta cache to portable keys: %d moved, %d duplicates removed",
            moved,
            removed,
        )


def flush(self, force: bool = False, batch: int = 25) -> None:
    with self._lock:
        if not force and self._dirty < batch:
            return
        if not self._buf:
            self._dirty = 0
            return
        rows = [
            (
                path,
                float(meta.intro_end_s),
                float(meta.outro_start_s),
                int(bool(meta.analysed)),
                json.dumps([float(beat) for beat in meta.beats]),
                json.dumps([asdict(cue) for cue in meta.cues]),
            )
            for path, meta in self._buf.items()
        ]
        assert self._conn is not None
        with immediate_transaction(self._conn):
            self._conn.executemany(
                "INSERT OR REPLACE INTO dj_meta "
                "(path, intro_end_s, outro_start_s, analysed, beats, cues) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        self._buf.clear()
        self._dirty = 0


def prune_to_paths(self, valid_paths: AbstractSet[str]) -> int:
    valid_keys = {self._key(path) for path in valid_paths}
    with self._lock:
        assert self._conn is not None
        rows = [
            (
                path,
                float(meta.intro_end_s),
                float(meta.outro_start_s),
                int(bool(meta.analysed)),
                json.dumps([float(beat) for beat in meta.beats]),
                json.dumps([asdict(cue) for cue in meta.cues]),
            )
            for path, meta in self._buf.items()
        ]
        with immediate_transaction(self._conn):
            if rows:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO dj_meta "
                    "(path, intro_end_s, outro_start_s, analysed, beats, cues) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    rows,
                )
            existing = [
                str(row[0]) for row in self._conn.execute("SELECT path FROM dj_meta")
            ]
            stale = [path for path in existing if path not in valid_keys]
            if stale:
                self._conn.executemany(
                    "DELETE FROM dj_meta WHERE path = ?", [(path,) for path in stale]
                )
        self._buf.clear()
        self._dirty = 0
        for path in stale:
            self._mem_cache.pop(path, None)
        return len(stale)


def close_cache() -> None:
    """Flush, close, and clear the process-wide cache; safe to call repeatedly."""
    global _CACHE
    with _CACHE_LOCK:
        cache, _CACHE = _CACHE, None
    if cache is None:
        return
    try:
        cache.flush(force=True)
    finally:
        cache.close()


atexit.register(close_cache)
```

In the server lifespan `finally`, replace the current player-cache-only flush block with:

```python
try:
    from autodj.dj_meta import close_cache

    close_cache()
except Exception:  # pragma: no cover -- defensive shutdown logging
    logger.debug("shutdown: DJ-meta close failed", exc_info=True)
```

Add this root autouse fixture to `tests/conftest.py`, importing `pytest`:

```python
@pytest.fixture(autouse=True)
def _close_global_dj_cache_between_tests():
    from autodj.dj_meta import close_cache

    close_cache()
    yield
    close_cache()
```

For every direct cache construction in `tests/unit/test_dj_meta.py` and
`tests/unit/test_dj_cues.py`, use `with DjMetaCache(path) as cache:` and place that test's cache
operations inside the block. The explicit-close regression remains direct construction and ends
with `assert cache._conn is None`.

- [ ] **Step 4: Run DJ cache tests with ResourceWarnings promoted**

Run: `uv run pytest tests/unit/test_dj_meta.py tests/unit/test_dj_cues.py -W error::ResourceWarning -q`

Expected: all tests PASS with no unclosed SQLite warnings.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/dj_meta.py src/autodj/server.py tests/conftest.py tests/unit/test_dj_meta.py tests/unit/test_dj_cues.py
git commit -m "fix: transact and close DJ metadata cache"
```

### Task 3: Stable vector-row identity and incremental metadata UPSERTs

**Files:**
- Modify: `src/autodj/indexer.py:129-250,704-779,1864-2002`
- Test: `tests/unit/test_indexer.py:1181-1305`
- Test: `tests/unit/test_indexer_more.py:439-486`
- Test: `tests/integration/test_index_pipeline.py`

- [ ] **Step 1: Write failing schema and write-count tests**

Add to `TestThrottledFaissCheckpoint`:

```python
def test_delta_checkpoint_upserts_one_stable_vector_row(
    self,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autodj.indexer as indexer
    from autodj.indexer import _load_tracks_rows, _open_tracks_db, _upsert_tracks_metadata

    entries, _ = self._make_entries(3)
    statements: list[str] = []

    def traced_open(index_dir: Path):
        conn = _open_tracks_db(index_dir)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(indexer, "_open_tracks_db", traced_open)

    _upsert_tracks_metadata(entries[:2], tmp_path, first_vec_row=0, music_dir=None)
    statements.clear()
    _upsert_tracks_metadata(entries[2:], tmp_path, first_vec_row=2, music_dir=None)

    conn = _open_tracks_db(tmp_path)
    try:
        rows = conn.execute("SELECT vec_row, path FROM tracks ORDER BY vec_row").fetchall()
        loaded = _load_tracks_rows(conn)
    finally:
        conn.close()
    assert rows == [(0, entries[0].path), (1, entries[1].path), (2, entries[2].path)]
    assert [entry.path for entry in loaded] == [entry.path for entry in entries]
    assert not any(statement.lstrip().upper().startswith("DELETE FROM TRACKS") for statement in statements)


def test_checkpoint_buffers_metadata_until_vectors_are_flushed(
    self,
    tmp_path: Path,
) -> None:
    from autodj.indexer import IncrementalCheckpoint

    entries, vectors = self._make_entries(2)
    checkpoint = IncrementalCheckpoint(
        index_dir=tmp_path,
        music_dir=None,
        existing_entries=[],
        existing_vectors=[],
        total_new=2,
        flush_every=2,
    )
    checkpoint.write(entries[:1], [vectors[0]])
    assert not (tmp_path / "tracks.db").exists()
    assert not (tmp_path / "vectors.index").exists()

    checkpoint.write(entries, [vectors[0], vectors[1]])
    conn = _open_tracks_db(tmp_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 2
    finally:
        conn.close()
    assert (tmp_path / "vectors.index").exists()
```

Add this migration regression beside the checkpoint test:

```python
def test_open_tracks_db_backfills_vec_row_in_legacy_id_order(tmp_path: Path) -> None:
    import sqlite3

    from autodj.indexer import _open_tracks_db

    conn = sqlite3.connect(tmp_path / "tracks.db")
    conn.execute(
        "CREATE TABLE tracks (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "path TEXT NOT NULL UNIQUE)"
    )
    conn.executemany(
        "INSERT INTO tracks(path) VALUES (?)",
        [("second.flac",), ("first.flac",)],
    )
    conn.commit()
    conn.close()

    migrated = _open_tracks_db(tmp_path)
    try:
        rows = migrated.execute("SELECT vec_row, path FROM tracks ORDER BY vec_row").fetchall()
        vec_info = next(
            row for row in migrated.execute("PRAGMA table_info(tracks)") if row[1] == "vec_row"
        )
        with pytest.raises(sqlite3.IntegrityError):
            migrated.execute("INSERT INTO tracks(vec_row, path) VALUES (NULL, 'bad.flac')")
    finally:
        migrated.close()
    assert rows == [(0, "second.flac"), (1, "first.flac")]
    assert vec_info[3] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_indexer.py -k 'delta_checkpoint or checkpoint_buffers or backfills_vec_row' -q`

Expected: FAIL because `vec_row` and `_upsert_tracks_metadata` do not exist.

- [ ] **Step 3: Add `vec_row`, migration, full replacement, and delta UPSERT**

Use this schema and ordering:

```python
_TRACKS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS tracks (
        vec_row          INTEGER NOT NULL UNIQUE,
        path             TEXT NOT NULL UNIQUE,
        title            TEXT NOT NULL DEFAULT '',
        artist           TEXT NOT NULL DEFAULT '',
        album            TEXT NOT NULL DEFAULT '',
        genre            TEXT NOT NULL DEFAULT '',
        bpm              REAL NOT NULL DEFAULT 0,
        year             INTEGER NOT NULL DEFAULT 0,
        length           REAL NOT NULL DEFAULT 0,
        energy           REAL NOT NULL DEFAULT 0,
        key              INTEGER NOT NULL DEFAULT -1,
        mode             INTEGER NOT NULL DEFAULT -1,
        tempo_confidence REAL NOT NULL DEFAULT 0,
        embedded_at      REAL NOT NULL DEFAULT 0
    );
"""

_TRACKS_SELECT_SQL = (
    "SELECT path, title, artist, album, genre, bpm, year, length, energy, "
    "key, mode, tempo_confidence, embedded_at FROM tracks ORDER BY vec_row ASC"
)
```

After schema creation, migrate old databases exactly once:

```python
def _ensure_vec_row_schema(conn: sqlite3.Connection) -> None:
    info = {str(row[1]): row for row in conn.execute("PRAGMA table_info(tracks)")}
    vec_info = info.get("vec_row")
    if vec_info is not None and int(vec_info[3]) == 1:
        return
    names = set(info)
    order_by = (
        "CASE WHEN vec_row IS NULL THEN 1 ELSE 0 END, vec_row, rowid"
        if "vec_row" in names
        else ("id, rowid" if "id" in names else "rowid")
    )
    defaults = {
        "title": "''", "artist": "''", "album": "''", "genre": "''",
        "bpm": "0", "year": "0", "length": "0", "energy": "0",
        "key": "-1", "mode": "-1", "tempo_confidence": "0", "embedded_at": "0",
    }
    value_sql = [
        f"ROW_NUMBER() OVER (ORDER BY {order_by}) - 1",
        "path",
        *(name if name in names else default for name, default in defaults.items()),
    ]
    with immediate_transaction(conn):
        conn.execute(
            """CREATE TABLE tracks_new (
                vec_row INTEGER NOT NULL UNIQUE, path TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '', artist TEXT NOT NULL DEFAULT '',
                album TEXT NOT NULL DEFAULT '', genre TEXT NOT NULL DEFAULT '',
                bpm REAL NOT NULL DEFAULT 0, year INTEGER NOT NULL DEFAULT 0,
                length REAL NOT NULL DEFAULT 0, energy REAL NOT NULL DEFAULT 0,
                key INTEGER NOT NULL DEFAULT -1, mode INTEGER NOT NULL DEFAULT -1,
                tempo_confidence REAL NOT NULL DEFAULT 0,
                embedded_at REAL NOT NULL DEFAULT 0
            )"""
        )
        columns = (
            "vec_row, path, title, artist, album, genre, bpm, year, length, energy, "
            "key, mode, tempo_confidence, embedded_at"
        )
        conn.execute(  # nosec B608 -- expressions come only from the fixed column/default map
            f"INSERT INTO tracks_new ({columns}) SELECT {', '.join(value_sql)} "
            f"FROM tracks ORDER BY {order_by}"
        )
        conn.execute("DROP TABLE tracks")
        conn.execute("ALTER TABLE tracks_new RENAME TO tracks")
```

Replace the insert SQL and row converter with these complete definitions, call
`_ensure_vec_row_schema(conn)` immediately after `conn.executescript(_TRACKS_SCHEMA)`, and
enumerate entries in `_replace_tracks_rows`:

```python
_TRACKS_INSERT_SQL = (
    "INSERT INTO tracks "
    "(vec_row, path, title, artist, album, genre, bpm, year, length, energy, "
    "key, mode, tempo_confidence, embedded_at) "
    "VALUES (:vec_row, :path, :title, :artist, :album, :genre, :bpm, :year, "
    ":length, :energy, :key, :mode, :tempo_confidence, :embedded_at)"
)


def _entry_to_row(
    entry: IndexEntry,
    music_dir: Path | None,
    vec_row: int,
) -> dict[str, object]:
    return {
        "vec_row": vec_row,
        "path": _relativize_for_storage(entry.path, music_dir),
        "title": entry.title,
        "artist": entry.artist,
        "album": entry.album,
        "genre": entry.genre,
        "bpm": float(entry.bpm),
        "year": int(entry.year),
        "length": float(entry.length),
        "energy": float(entry.energy),
        "key": int(entry.key),
        "mode": int(entry.mode),
        "tempo_confidence": float(entry.tempo_confidence),
        "embedded_at": float(entry.embedded_at),
    }


def _replace_tracks_rows(
    conn: sqlite3.Connection,
    entries: list[IndexEntry],
    music_dir: Path | None,
) -> None:
    rows = [_entry_to_row(entry, music_dir, vec_row) for vec_row, entry in enumerate(entries)]
    with immediate_transaction(conn):
        conn.execute("DELETE FROM tracks")
        if rows:
            conn.executemany(_TRACKS_INSERT_SQL, rows)
```

Add the delta writer:

```python
_TRACKS_UPSERT_SQL = _TRACKS_INSERT_SQL + (
    " ON CONFLICT(vec_row) DO UPDATE SET "
    "path=excluded.path, title=excluded.title, artist=excluded.artist, "
    "album=excluded.album, genre=excluded.genre, bpm=excluded.bpm, "
    "year=excluded.year, length=excluded.length, energy=excluded.energy, "
    "key=excluded.key, mode=excluded.mode, "
    "tempo_confidence=excluded.tempo_confidence, embedded_at=excluded.embedded_at"
)


def _upsert_tracks_metadata(
    entries: list[IndexEntry],
    index_dir: Path,
    first_vec_row: int,
    music_dir: Path | None,
) -> None:
    rows = [
        _entry_to_row(entry, music_dir, first_vec_row + offset)
        for offset, entry in enumerate(entries)
    ]
    conn = _open_tracks_db(index_dir)
    try:
        with immediate_transaction(conn):
            conn.executemany(_TRACKS_UPSERT_SQL, rows)
    finally:
        conn.close()
```

Add this checkpoint writer above `build_index` and pass `checkpoint.write` to
`_embed_new_tracks`. This buffers metadata until the matching FAISS snapshot is also due, then
writes only metadata rows not included in the preceding durable checkpoint:

```python
@dataclass
class IncrementalCheckpoint:
    index_dir: Path
    music_dir: Path | None
    existing_entries: list[IndexEntry]
    existing_vectors: list[np.ndarray]
    total_new: int
    flush_every: int = FAISS_CHECKPOINT_EVERY
    published_new_count: int = 0

    def write(
        self,
        new_entries: list[IndexEntry],
        new_vectors: list[np.ndarray],
    ) -> None:
        if not new_entries:
            return
        due = len(new_entries) == self.total_new or len(new_entries) % self.flush_every == 0
        if not due:
            return
        all_vectors = (
            np.vstack(
                [
                    np.asarray(self.existing_vectors, dtype=np.float32),
                    np.asarray(new_vectors, dtype=np.float32),
                ]
            )
            if self.existing_vectors
            else np.asarray(new_vectors, dtype=np.float32)
        )
        _save_vectors(all_vectors, self.index_dir)
        pending = new_entries[self.published_new_count :]
        _upsert_tracks_metadata(
            pending,
            self.index_dir,
            first_vec_row=len(self.existing_entries) + self.published_new_count,
            music_dir=self.music_dir,
        )
        self.published_new_count = len(new_entries)
```

Replace the nested `_checkpoint` and `cp_counter` in `build_index` with:

```python
checkpoint = IncrementalCheckpoint(
    index_dir=index_dir,
    music_dir=music_dir,
    existing_entries=existing_entries,
    existing_vectors=existing_vectors,
    total_new=len(new_tracks),
)
new_entries, new_vectors = _embed_new_tracks(
    new_tracks,
    wrapper,
    workers,
    checkpoint.write,
    throttle_ms=throttle_ms,
)
```

Keep `_replace_tracks_rows` only in full save, reorder, prune, migration, and enrich paths.
Change all three existing track-order test queries in `tests/unit/test_indexer.py` and
`tests/unit/test_indexer_more.py` from `ORDER BY id ASC` to `ORDER BY vec_row ASC`. Also change
Task 1's new rollback query from `ORDER BY id` to `ORDER BY vec_row` now that the legacy primary
key has been replaced.

- [ ] **Step 4: Run index persistence tests**

Run: `uv run pytest tests/unit/test_indexer.py tests/unit/test_indexer_more.py tests/integration/test_index_pipeline.py -q`

Expected: all tests PASS; the delta test observes no `DELETE FROM tracks`.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/indexer.py tests/unit/test_indexer.py tests/unit/test_indexer_more.py tests/integration/test_index_pipeline.py
git commit -m "perf: upsert incremental track metadata"
```

### Task 4: Durable generation manifest and validated load boundary

**Files:**
- Create: `src/autodj/index_manifest.py`
- Create: `tests/unit/test_index_manifest.py`
- Modify: `src/autodj/indexer.py:704-779,864-987,1049-1152,1206-1265,1942-2002`
- Test: `tests/unit/test_indexer.py:240-290,1181-1305`

- [ ] **Step 1: Write failing manifest durability and mismatch tests**

```python
# tests/unit/test_index_manifest.py
from __future__ import annotations

import json
import os
import sqlite3
import stat
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import faiss
import numpy as np
import pytest

from autodj.index_manifest import (
    IndexConsistencyError,
    IndexManifest,
    publish_manifest,
    read_manifest,
    sha256_file,
)


def _write_working_artifacts(index_dir, count: int) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_dir / "tracks.db", isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS tracks (vec_row INTEGER, path TEXT)")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM tracks")
        conn.executemany(
            "INSERT INTO tracks VALUES (?, ?)",
            [(row, f"song-{row}.flac") for row in range(count)],
        )
        conn.commit()
    finally:
        conn.close()
    vectors = faiss.IndexFlatIP(2)
    if count:
        vectors.add(np.ones((count, 2), dtype=np.float32))
    faiss.write_index(vectors, str(index_dir / "vectors.index"))


def _publish_once(index_dir_text: str, count: int) -> int:
    return publish_manifest(Path(index_dir_text), count).generation


def test_publish_manifest_is_monotonic_atomic_and_retains_two_generations(tmp_path) -> None:
    _write_working_artifacts(tmp_path, 3)
    first = publish_manifest(tmp_path, vector_count=3)
    _write_working_artifacts(tmp_path, 5)
    second = publish_manifest(tmp_path, vector_count=5)
    _write_working_artifacts(tmp_path, 7)
    third = publish_manifest(tmp_path, vector_count=7)
    assert first.generation == 1
    assert second.generation == 2
    assert third.generation == 3
    assert read_manifest(tmp_path) == third
    names = {path.name for path in tmp_path.iterdir()}
    assert first.tracks_file not in names
    assert first.vectors_file not in names
    assert {second.tracks_file, second.vectors_file, third.tracks_file, third.vectors_file} <= names
    assert list(tmp_path.glob(".*.tmp")) == []


def test_publish_manifest_serializes_concurrent_generation_numbers(tmp_path) -> None:
    _write_working_artifacts(tmp_path, 3)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish_manifest, tmp_path, 3) for _ in range(2)]
    assert {future.result().generation for future in futures} == {1, 2}


def test_publish_manifest_serializes_concurrent_processes(tmp_path) -> None:
    _write_working_artifacts(tmp_path, 3)
    with ProcessPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_publish_once, str(tmp_path), 3) for _ in range(2)]
    assert {future.result(timeout=10) for future in futures} == {1, 2}


@pytest.mark.skipif(os.name == "nt", reason="opening a directory for fsync is POSIX-only")
def test_manifest_replace_fsyncs_parent_directory(tmp_path, monkeypatch) -> None:
    _write_working_artifacts(tmp_path, 1)
    real_fsync = os.fsync
    directory_fsyncs = 0

    def recording_fsync(fd: int) -> None:
        nonlocal directory_fsyncs
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_fsyncs += 1
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    publish_manifest(tmp_path, 1)
    assert directory_fsyncs >= 1


def test_publish_checkpoints_wal_before_hashing(tmp_path) -> None:
    _write_working_artifacts(tmp_path, 2)
    manifest = publish_manifest(tmp_path, 2)
    wal = tmp_path / "tracks.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0
    assert manifest.tracks_sha256 == sha256_file(tmp_path / manifest.tracks_file)


def test_copy_published_snapshot_produces_valid_canonical_backup(tmp_path) -> None:
    from autodj.index_manifest import copy_published_snapshot

    source = tmp_path / "source"
    destination = tmp_path / "backup"
    _write_working_artifacts(source, 2)
    published = publish_manifest(source, 2)
    copied = copy_published_snapshot(
        source,
        destination,
        expected_generation=published.generation,
    )
    assert copied.generation == published.generation
    assert copied.tracks_file == "tracks.db"
    assert copied.vectors_file == "vectors.index"
    assert read_manifest(destination) == copied
    assert sha256_file(destination / "tracks.db") == copied.tracks_sha256
    assert sha256_file(destination / "vectors.index") == copied.vectors_sha256


def test_copy_published_snapshot_rejects_generation_race(tmp_path, monkeypatch) -> None:
    import autodj.index_manifest as manifest_module

    source = tmp_path / "source"
    destination = tmp_path / "backup"
    _write_working_artifacts(source, 2)
    publish_manifest(source, 2)
    real_validate = manifest_module._validate_snapshot_files
    raced = False

    def validate_then_publish(root, manifest) -> None:
        nonlocal raced
        real_validate(root, manifest)
        if root == source and not raced:
            raced = True
            publish_manifest(source, 2)

    monkeypatch.setattr(manifest_module, "_validate_snapshot_files", validate_then_publish)
    with pytest.raises(IndexConsistencyError, match="changed while copying"):
        manifest_module.copy_published_snapshot(source, destination)
    assert not destination.exists()


def test_corrupt_manifest_raises_consistency_error(tmp_path) -> None:
    (tmp_path / "index-manifest.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(IndexConsistencyError, match="manifest"):
        read_manifest(tmp_path)


def test_manifest_rejects_wrong_schema(tmp_path) -> None:
    payload = {"schema_version": 99, "generation": 1, "vector_count": 2,
               "published_at": "2026-08-02T00:00:00+00:00",
               "tracks_file": "tracks.g00000000000000000001.db",
               "vectors_file": "vectors.g00000000000000000001.index",
               "tracks_sha256": "0" * 64, "vectors_sha256": "1" * 64}
    (tmp_path / "index-manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(IndexConsistencyError, match="schema"):
        read_manifest(tmp_path)
```

Add these regressions to `TestSaveLoadIndex` in `tests/unit/test_indexer.py`:

```python
def test_load_ignores_unpublished_metadata_ahead_crash(self, tmp_path: Path) -> None:
    from autodj.indexer import _save_tracks_metadata

    entries, vectors = self._make_entries(4)
    save_index(entries[:3], vectors[:3], tmp_path)
    _save_tracks_metadata(entries, tmp_path, music_dir=None)
    loaded, faiss_index = load_index(tmp_path)
    assert len(loaded) == faiss_index.ntotal == 3


def test_load_ignores_unpublished_vectors_ahead_crash(self, tmp_path: Path) -> None:
    from autodj.indexer import _save_vectors

    entries, vectors = self._make_entries(4)
    save_index(entries[:3], vectors[:3], tmp_path)
    _save_vectors(vectors, tmp_path)
    loaded, faiss_index = load_index(tmp_path)
    assert len(loaded) == faiss_index.ntotal == 3


def test_restart_restores_canonical_working_files_from_live_generation(
    self,
    tmp_path: Path,
) -> None:
    from autodj.index_manifest import read_manifest, sha256_file
    from autodj.indexer import _load_existing_artifacts, _save_vectors

    entries, vectors = self._make_entries(3)
    save_index(entries, vectors, tmp_path)
    manifest = read_manifest(tmp_path)
    assert manifest is not None
    _save_vectors(np.flip(vectors, axis=0).copy(), tmp_path)
    _load_existing_artifacts(tmp_path, tmp_path, None)
    assert sha256_file(tmp_path / "tracks.db") == manifest.tracks_sha256
    assert sha256_file(tmp_path / "vectors.index") == manifest.vectors_sha256
    assert not (tmp_path / "tracks.db-wal").exists()


def test_load_rejects_same_count_vector_mix(self, tmp_path: Path) -> None:
    from autodj.index_manifest import IndexConsistencyError, read_manifest
    from autodj.indexer import _save_vectors

    entries, vectors = self._make_entries(3)
    save_index(entries, vectors, tmp_path)
    manifest = read_manifest(tmp_path)
    assert manifest is not None
    _save_vectors(np.flip(vectors, axis=0).copy(), tmp_path)
    (tmp_path / manifest.vectors_file).write_bytes((tmp_path / "vectors.index").read_bytes())
    with pytest.raises(IndexConsistencyError, match="vectors SHA-256"):
        load_index(tmp_path)


def test_load_rejects_same_count_metadata_mix(self, tmp_path: Path) -> None:
    import sqlite3

    from autodj.index_manifest import IndexConsistencyError, read_manifest

    old_entries, vectors = self._make_entries(3)
    save_index(old_entries, vectors, tmp_path)
    manifest = read_manifest(tmp_path)
    assert manifest is not None
    mixed_entries, _ = self._make_entries(3)
    for index, entry in enumerate(mixed_entries):
        entry.path = f"Z:/Other/song_{index}.flac"
    _save_tracks_metadata(mixed_entries, tmp_path, music_dir=None)
    conn = sqlite3.connect(tmp_path / "tracks.db", isolation_level=None)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    (tmp_path / manifest.tracks_file).write_bytes((tmp_path / "tracks.db").read_bytes())
    with pytest.raises(IndexConsistencyError, match="tracks SHA-256"):
        load_index(tmp_path)


def test_legacy_index_without_manifest_still_loads(self, tmp_path: Path) -> None:
    entries, vectors = self._make_entries(3)
    save_index(entries, vectors, tmp_path)
    (tmp_path / "index-manifest.json").unlink()
    loaded, faiss_index = load_index(tmp_path)
    assert len(loaded) == 3
    assert faiss_index.ntotal == 3


def test_failed_second_save_does_not_publish_generation(self, tmp_path: Path) -> None:
    from autodj.index_manifest import read_manifest

    entries, vectors = self._make_entries(4)
    save_index(entries[:3], vectors[:3], tmp_path)
    first = read_manifest(tmp_path)
    assert first is not None
    with (
        patch("autodj.indexer._save_tracks_metadata", side_effect=OSError("injected")),
        pytest.raises(OSError, match="injected"),
    ):
        save_index(entries, vectors, tmp_path)
    current = read_manifest(tmp_path)
    assert current is not None
    assert current.generation == first.generation == 1
    loaded, loaded_faiss = load_index(tmp_path)
    assert len(loaded) == loaded_faiss.ntotal == 3


def test_load_rejects_manifest_change_during_artifact_read(self, tmp_path: Path) -> None:
    from autodj.index_manifest import IndexConsistencyError, publish_manifest

    entries, vectors = self._make_entries(3)
    save_index(entries, vectors, tmp_path)
    real_read_index = faiss.read_index
    raced = False

    def read_then_publish(path: str):
        nonlocal raced
        loaded = real_read_index(path)
        if not raced:
            raced = True
            publish_manifest(tmp_path, 3)
        return loaded

    with (
        patch("autodj.indexer.faiss.read_index", side_effect=read_then_publish),
        pytest.raises(IndexConsistencyError, match="changed during load"),
    ):
        load_index(tmp_path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_index_manifest.py tests/unit/test_indexer.py -k 'manifest or unpublished or same_count or legacy_without or snapshot or wal or concurrent' -q`

Expected: FAIL because the manifest API and validation do not exist.

- [ ] **Step 3: Implement the complete manifest API**

```python
# src/autodj/index_manifest.py
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

SCHEMA_VERSION = 1
MANIFEST_NAME = "index-manifest.json"
_GENERATION_RE = re.compile(r"^(tracks|vectors)\.g(\d{20})\.(db|index)$")
_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
logger = logging.getLogger(__name__)


class _LockState(threading.local):
    def __init__(self) -> None:
        self.paths: set[Path] = set()


_HELD_LOCKS = _LockState()


class IndexConsistencyError(RuntimeError):
    """Raised when published index artifacts do not describe one snapshot."""


@dataclass(frozen=True)
class IndexManifest:
    schema_version: int
    generation: int
    vector_count: int
    published_at: str
    tracks_file: str
    vectors_file: str
    tracks_sha256: str
    vectors_sha256: str


def read_manifest(index_dir: Path) -> IndexManifest | None:
    path = index_dir / MANIFEST_NAME
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        manifest = IndexManifest(
            schema_version=int(raw["schema_version"]),
            generation=int(raw["generation"]),
            vector_count=int(raw["vector_count"]),
            published_at=str(raw["published_at"]),
            tracks_file=str(raw["tracks_file"]),
            vectors_file=str(raw["vectors_file"]),
            tracks_sha256=str(raw["tracks_sha256"]),
            vectors_sha256=str(raw["vectors_sha256"]),
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise IndexConsistencyError(f"invalid index manifest: {exc}") from exc
    if manifest.schema_version != SCHEMA_VERSION:
        raise IndexConsistencyError(
            f"unsupported manifest schema {manifest.schema_version}; expected {SCHEMA_VERSION}"
        )
    if manifest.generation < 1 or manifest.vector_count < 0:
        raise IndexConsistencyError("manifest generation/count must be non-negative")
    for name in (manifest.tracks_file, manifest.vectors_file):
        if Path(name).name != name:
            raise IndexConsistencyError("manifest artifact must be one plain filename")
    for digest in (manifest.tracks_sha256, manifest.vectors_sha256):
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise IndexConsistencyError("manifest contains an invalid SHA-256 digest")
    return manifest


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _thread_lock(path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.RLock())


def _acquire_os_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release_os_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def publication_lock(index_dir: Path) -> Iterator[None]:
    index_dir = index_dir.resolve()
    index_dir.mkdir(parents=True, exist_ok=True)
    local_lock = _thread_lock(index_dir)
    with local_lock:
        held = _HELD_LOCKS.paths
        if index_dir in held:
            yield
            return
        lock_path = index_dir / ".index-publication.lock"
        with lock_path.open("a+b") as handle:
            _acquire_os_lock(handle)
            _HELD_LOCKS.paths = {*held, index_dir}
            try:
                yield
            finally:
                _HELD_LOCKS.paths = held
                _release_os_lock(handle)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_copy(source: Path, destination: Path) -> None:
    tmp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as reader, tmp.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)


def _checkpoint_working_tracks(index_dir: Path) -> None:
    db_path = index_dir / "tracks.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        busy, remaining, _checkpointed = conn.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
    finally:
        conn.close()
    if busy or remaining:
        raise IndexConsistencyError(
            f"tracks WAL checkpoint incomplete: busy={busy}, remaining={remaining}"
        )
    wal = db_path.with_name(f"{db_path.name}-wal")
    if wal.exists() and wal.stat().st_size:
        raise IndexConsistencyError("tracks WAL remains non-empty after checkpoint")


def _write_manifest(path: Path, manifest: IndexManifest) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(asdict(manifest), handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _cleanup_generations(index_dir: Path, keep: set[int]) -> None:
    for path in index_dir.iterdir():
        match = _GENERATION_RE.fullmatch(path.name)
        if match is None or int(match.group(2)) in keep:
            continue
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("Could not remove old index generation %s: %s", path.name, exc)
    _fsync_directory(index_dir)


def publish_manifest(index_dir: Path, vector_count: int) -> IndexManifest:
    with publication_lock(index_dir):
        previous = read_manifest(index_dir)
        generation = 1 if previous is None else previous.generation + 1
        _checkpoint_working_tracks(index_dir)
        tracks_name = f"tracks.g{generation:020d}.db"
        vectors_name = f"vectors.g{generation:020d}.index"
        tracks_path = index_dir / tracks_name
        vectors_path = index_dir / vectors_name
        _durable_copy(index_dir / "tracks.db", tracks_path)
        _durable_copy(index_dir / "vectors.index", vectors_path)
        manifest = IndexManifest(
            schema_version=SCHEMA_VERSION,
            generation=generation,
            vector_count=vector_count,
            published_at=datetime.now(UTC).isoformat(timespec="seconds"),
            tracks_file=tracks_name,
            vectors_file=vectors_name,
            tracks_sha256=sha256_file(tracks_path),
            vectors_sha256=sha256_file(vectors_path),
        )
        _validate_snapshot_files(index_dir, manifest)
        _write_manifest(index_dir / MANIFEST_NAME, manifest)
        keep = {generation}
        if previous is not None:
            keep.add(previous.generation)
        _cleanup_generations(index_dir, keep)
        return manifest


def restore_working_snapshot(
    index_dir: Path,
    *,
    expected_generation: int | None = None,
) -> IndexManifest:
    with publication_lock(index_dir):
        manifest = read_manifest(index_dir)
        if manifest is None:
            raise IndexConsistencyError("published manifest is missing")
        if expected_generation is not None and manifest.generation != expected_generation:
            raise IndexConsistencyError(
                f"expected generation {expected_generation}, got {manifest.generation}"
            )
        _validate_snapshot_files(index_dir, manifest)
        (index_dir / "tracks.db-wal").unlink(missing_ok=True)
        (index_dir / "tracks.db-shm").unlink(missing_ok=True)
        _durable_copy(index_dir / manifest.tracks_file, index_dir / "tracks.db")
        _durable_copy(index_dir / manifest.vectors_file, index_dir / "vectors.index")
        _fsync_directory(index_dir)
        return manifest


def _validate_snapshot_files(root: Path, manifest: IndexManifest) -> None:
    tracks = root / manifest.tracks_file
    vectors = root / manifest.vectors_file
    if sha256_file(tracks) != manifest.tracks_sha256:
        raise IndexConsistencyError("tracks SHA-256 mismatch")
    if sha256_file(vectors) != manifest.vectors_sha256:
        raise IndexConsistencyError("vectors SHA-256 mismatch")
    conn = sqlite3.connect(f"file:{tracks.as_posix()}?mode=ro", uri=True)
    try:
        sqlite_count = int(conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0])
    finally:
        conn.close()
    import faiss

    faiss_count = int(faiss.read_index(str(vectors)).ntotal)
    if sqlite_count != manifest.vector_count or faiss_count != manifest.vector_count:
        raise IndexConsistencyError(
            f"index count mismatch: manifest={manifest.vector_count}, "
            f"sqlite={sqlite_count}, faiss={faiss_count}"
        )


def copy_published_snapshot(
    index_dir: Path,
    destination: Path,
    *,
    expected_generation: int | None = None,
) -> IndexManifest:
    with publication_lock(index_dir):
        before = read_manifest(index_dir)
        if before is None:
            raise IndexConsistencyError("published manifest is missing")
        if expected_generation is not None and before.generation != expected_generation:
            raise IndexConsistencyError(
                f"expected generation {expected_generation}, got {before.generation}"
            )
        _validate_snapshot_files(index_dir, before)
        if destination.exists():
            raise FileExistsError(destination)
        staging = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            staging.mkdir(parents=True)
            _durable_copy(index_dir / before.tracks_file, staging / "tracks.db")
            _durable_copy(index_dir / before.vectors_file, staging / "vectors.index")
            copied = replace(before, tracks_file="tracks.db", vectors_file="vectors.index")
            _write_manifest(staging / MANIFEST_NAME, copied)
            _validate_snapshot_files(staging, copied)
            after = read_manifest(index_dir)
            if after != before:
                raise IndexConsistencyError("generation changed while copying snapshot")
            os.replace(staging, destination)
            _fsync_directory(destination.parent)
            return copied
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
```

This plan owns the exact cross-plan API
`copy_published_snapshot(index_dir: Path, destination: Path, *, expected_generation: int | None = None) -> IndexManifest`.
The delivery plan must import it rather than duplicating snapshot logic. It produces a new
destination containing canonical `tracks.db`, `vectors.index`, and `index-manifest.json`, validates
digests/counts, and re-reads the source generation before promotion; a mismatch raises
`IndexConsistencyError`.

Replace `load_index` with this complete immutable-generation loader. It reads the manifest before
and after loading, hashes each artifact both before and after parsing, and holds the same
cross-process publication lock as writers:

```python
def load_index(
    index_dir: Path,
    music_dir: Path | None = None,
    path_remap: list[tuple[str, str]] | None = None,
    *,
    expected_generation: int | None = None,
) -> tuple[list[IndexEntry], faiss.IndexFlatIP]:
    from autodj.index_manifest import (
        IndexConsistencyError,
        publication_lock,
        read_manifest,
        sha256_file,
    )

    _migrate_flat_index_if_needed(index_dir)
    with publication_lock(index_dir):
        before = read_manifest(index_dir)
        if expected_generation is not None and (
            before is None or before.generation != expected_generation
        ):
            raise IndexConsistencyError(
                f"expected generation {expected_generation}, got "
                f"{getattr(before, 'generation', None)}"
            )
        tracks_path = (
            index_dir / before.tracks_file
            if before is not None
            else index_dir / "tracks.db"
        )
        vectors_path = (
            index_dir / before.vectors_file
            if before is not None
            else index_dir / "vectors.index"
        )
        if not tracks_path.is_file() or not vectors_path.is_file():
            raise FileNotFoundError(
                f"Index files missing: {tracks_path.name} + {vectors_path.name}"
            )
        pre_hashes = (
            (sha256_file(tracks_path), sha256_file(vectors_path))
            if before is not None
            else None
        )

        faiss_index = cast("faiss.IndexFlatIP", faiss.read_index(str(vectors_path)))
        uri = f"file:{tracks_path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            entries = _load_tracks_rows(conn)
        finally:
            conn.close()
        if music_dir is not None or path_remap:
            for entry in entries:
                entry.path = _resolve_for_runtime(entry.path, music_dir, path_remap)

        post_hashes = (
            (sha256_file(tracks_path), sha256_file(vectors_path))
            if before is not None
            else None
        )
        after = read_manifest(index_dir)
        if after != before:
            raise IndexConsistencyError("manifest changed during load; retry generation")
        if pre_hashes != post_hashes:
            raise IndexConsistencyError("index artifact changed during load; retry generation")

        sqlite_count = len(entries)
        faiss_count = int(faiss_index.ntotal)
        expected_count = before.vector_count if before is not None else sqlite_count
        if sqlite_count != expected_count or faiss_count != expected_count:
            raise IndexConsistencyError(
                f"index count mismatch: manifest={getattr(before, 'vector_count', None)}, "
                f"sqlite={sqlite_count}, faiss={faiss_count}"
            )
        if before is not None and pre_hashes != (
            before.tracks_sha256,
            before.vectors_sha256,
        ):
            assert pre_hashes is not None
            tracks_hash, vectors_hash = pre_hashes
            if tracks_hash != before.tracks_sha256:
                raise IndexConsistencyError("tracks SHA-256 mismatch")
            if vectors_hash != before.vectors_sha256:
                raise IndexConsistencyError("vectors SHA-256 mismatch")
        logger.info(
            "Loaded index generation %s with %d tracks from %s",
            0 if before is None else before.generation,
            len(entries),
            index_dir,
        )
        return entries, faiss_index
```

Import `publication_lock`, `publish_manifest`, `read_manifest`, and
`restore_working_snapshot` from `index_manifest`. Add these two complete publication helpers and
route all full/metadata-only saves through them:

```python
def _publish_full_snapshot(
    entries: list[IndexEntry],
    vectors: np.ndarray,
    index_dir: Path,
    music_dir: Path | None,
) -> None:
    with publication_lock(index_dir):
        _save_vectors(vectors, index_dir)
        _save_tracks_metadata(entries, index_dir, music_dir)
        publish_manifest(index_dir, len(entries))


def _publish_metadata_snapshot(
    entries: list[IndexEntry],
    index_dir: Path,
    music_dir: Path | None,
) -> None:
    with publication_lock(index_dir):
        current = read_manifest(index_dir)
        if current is not None:
            # Pair metadata with the last published vectors, not mutable working files left
            # ahead by a failed full/incremental checkpoint.
            restore_working_snapshot(
                index_dir,
                expected_generation=current.generation,
            )
        _save_tracks_metadata(entries, index_dir, music_dir)
        publish_manifest(index_dir, len(entries))


def save_index(
    entries: list[IndexEntry],
    vectors: np.ndarray,
    index_dir: Path,
    music_dir: Path | None = None,
) -> None:
    _publish_full_snapshot(entries, vectors, index_dir, music_dir)
    logger.info("Saved index with %d tracks to %s", len(entries), index_dir)
```

Replace `IncrementalCheckpoint.write` from Task 3 with this manifest-publishing version:

```python
def write(
    self,
    new_entries: list[IndexEntry],
    new_vectors: list[np.ndarray],
) -> None:
    if not new_entries:
        return
    due = len(new_entries) == self.total_new or len(new_entries) % self.flush_every == 0
    if not due:
        return
    all_vectors = (
        np.vstack(
            [
                np.asarray(self.existing_vectors, dtype=np.float32),
                np.asarray(new_vectors, dtype=np.float32),
            ]
        )
        if self.existing_vectors
        else np.asarray(new_vectors, dtype=np.float32)
    )
    pending = new_entries[self.published_new_count :]
    with publication_lock(self.index_dir):
        _save_vectors(all_vectors, self.index_dir)
        _upsert_tracks_metadata(
            pending,
            self.index_dir,
            first_vec_row=len(self.existing_entries) + self.published_new_count,
            music_dir=self.music_dir,
        )
        publish_manifest(self.index_dir, len(self.existing_entries) + len(new_entries))
    self.published_new_count = len(new_entries)
```

In `enrich_from_beets` and the legacy path migration branch, replace direct
`_save_tracks_metadata` calls with `_publish_metadata_snapshot(entries, index_dir, music_dir)`.
In prune/reorder paths, retain `save_index`, which now publishes immutable artifacts.

Add this artifact loader and use it at the start of `_load_existing_index` instead of directly
opening the mutable working files:

```python
def _load_existing_artifacts(
    index_dir: Path,
    music_dir: Path,
    path_remap: list[tuple[str, str]] | None,
) -> tuple[list[IndexEntry], list[np.ndarray], bool]:
    with publication_lock(index_dir):
        manifest = read_manifest(index_dir)
        if manifest is not None:
            entries, loaded = load_index(
                index_dir,
                music_dir=music_dir,
                path_remap=path_remap,
                expected_generation=manifest.generation,
            )
            vectors = [
                np.asarray(row, dtype=np.float32)
                for row in loaded.reconstruct_n(0, loaded.ntotal)
            ]
            # Restart always begins future checkpoints from the live generation. Remove stale
            # SQLite sidecars only while every AutoDJ writer is excluded and all local DB
            # connections are closed, then restore both canonical working artifacts.
            restore_working_snapshot(index_dir, expected_generation=manifest.generation)
            return entries, vectors, True

    if not (index_dir / "tracks.db").is_file() or not (index_dir / "vectors.index").is_file():
        return [], [], False

    conn = _open_tracks_db(index_dir)
    try:
        entries = _load_tracks_rows(conn)
    finally:
        conn.close()
    already_relative = _is_relative_storage([entry.path for entry in entries])
    loaded = cast("faiss.IndexFlatIP", faiss.read_index(str(index_dir / "vectors.index")))
    entry_count = len(entries)
    vector_count = int(loaded.ntotal)
    common = min(entry_count, vector_count)
    if common == 0:
        return [], [], already_relative
    entries = entries[:common]
    vectors = [
        np.asarray(row, dtype=np.float32)
        for row in loaded.reconstruct_n(0, common)
    ]
    for entry in entries:
        entry.path = _resolve_for_runtime(entry.path, music_dir, path_remap)
    if common != entry_count or common != vector_count:
        save_index(entries, np.asarray(vectors, dtype=np.float32), index_dir, music_dir)
    return entries, vectors, already_relative
```

In `_load_existing_index`, replace the block from `db_path = _tracks_db_path(index_dir)` through
the existing "Incremental mode" log with:

```python
existing_entries, existing_vectors, already_relative = _load_existing_artifacts(
    index_dir,
    music_dir,
    path_remap,
)
if not existing_entries:
    return [], [], set()
logger.info("Incremental mode: %d tracks already indexed", len(existing_entries))
```

The existing prune/stale block follows unchanged. Never publish from `_save_vectors`,
`_save_tracks_metadata`, or `_upsert_tracks_metadata` alone.

- [ ] **Step 4: Run manifest and index tests**

Run: `uv run pytest tests/unit/test_index_manifest.py tests/unit/test_indexer.py tests/unit/test_indexer_more.py tests/integration/test_index_pipeline.py -q`

Expected: all tests PASS, including legacy startup without a manifest.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/index_manifest.py src/autodj/indexer.py tests/unit/test_index_manifest.py tests/unit/test_indexer.py
git commit -m "feat: publish coherent index generations"
```

### Task 5: Locked coherent reload and manifest watcher retries

**Files:**
- Modify: `src/autodj/similarity.py:117-220,354-605`
- Modify: `src/autodj/_bridge.py:604-646,695-785,1191-1209`
- Modify: `src/autodj/player.py:887,1035-1037,1443`
- Modify: `src/autodj/cli.py:112-138,1796`
- Modify: `src/autodj/server.py:1139-1171,1236-1272`
- Test: `tests/unit/test_similarity.py:305-338`
- Test: `tests/unit/test_player.py`
- Test: `tests/integration/test_server.py:2170-2187`
- Test: `tests/integration/test_server_branches.py:90-140`

- [ ] **Step 1: Write failing mismatch, retry, and concurrent-reader tests**

Add tests that:

```python
def test_failed_reload_keeps_previous_snapshot(self, tmp_path) -> None:
    from autodj.index_manifest import IndexConsistencyError
    from autodj.indexer import save_index

    sim, _vectors = _make_similarity_index(3)
    save_index(sim.entries, _vectors, tmp_path)
    save_index(sim.entries, _vectors, tmp_path)
    old_paths = tuple(entry.path for entry in sim.entries_snapshot())
    with patch("autodj.similarity.load_index", side_effect=IndexConsistencyError("counts")):
        with pytest.raises(IndexConsistencyError, match="counts"):
            sim.reload_from_disk(tmp_path, expected_generation=2)
    assert tuple(entry.path for entry in sim.entries_snapshot()) == old_paths
```

```python
@pytest.mark.asyncio
async def test_reload_generation_retries_same_generation_after_mismatch(bridge, tmp_path) -> None:
    from autodj.index_manifest import IndexConsistencyError, IndexManifest

    manifest = IndexManifest(
        schema_version=1,
        generation=1,
        vector_count=7,
        published_at="2026-08-02T00:00:00+00:00",
        tracks_file="tracks.g00000000000000000001.db",
        vectors_file="vectors.g00000000000000000001.index",
        tracks_sha256="0" * 64,
        vectors_sha256="1" * 64,
    )
    bridge.player._cfg.index.active_dir = tmp_path
    bridge.reload_index_from_disk = MagicMock(side_effect=[IndexConsistencyError("counts"), 7])
    observed = 0
    with patch("autodj.index_manifest.read_manifest", return_value=manifest):
        with pytest.raises(IndexConsistencyError):
            await reload_published_generation_once(bridge, observed)
        observed = await reload_published_generation_once(bridge, observed)
    assert observed == 1
    assert bridge.reload_index_from_disk.call_count == 2
```

Add this deterministic lock-sharing regression (import `threading`):

```python
def test_reload_waits_for_active_read_snapshot(self, tmp_path) -> None:
    sim, _old_vectors = _make_similarity_index(3)
    replacement, _new_vectors = _make_similarity_index(5)
    loaded = threading.Event()
    finished = threading.Event()

    def fake_load(*args, **kwargs):
        loaded.set()
        return replacement.entries, replacement.faiss_index

    def reload() -> None:
        sim.reload_from_disk(tmp_path)
        finished.set()

    sim._reload_lock.acquire()
    released = False
    try:
        with patch("autodj.similarity.load_index", side_effect=fake_load):
            worker = threading.Thread(target=reload)
            worker.start()
            assert loaded.wait(timeout=2)
            assert not finished.wait(timeout=0.1)
            sim._reload_lock.release()
            released = True
            worker.join(timeout=2)
    finally:
        if not released:
            sim._reload_lock.release()
    assert not worker.is_alive()
    assert len(sim.entries_snapshot()) == sim.ntotal == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_similarity.py tests/integration/test_server_branches.py -k '(reload and snapshot) or reload_generation' -q`

Expected: FAIL because reload has no expected generation, read snapshot, lock, or retry helper.

- [ ] **Step 3: Add one reload/read lock and validate before swap**

In `SimilarityIndex.__post_init__`, create `self._reload_lock = threading.RLock()` and `self._generation = 0`. Add:

```python
def entries_snapshot(self) -> tuple[IndexEntry, ...]:
    with self._reload_lock:
        return tuple(self.entries)


def entry_for_path(self, path: str) -> IndexEntry | None:
    with self._reload_lock:
        index = self._path_to_idx.get(path)
        return None if index is None else self.entries[index]


@property
def ntotal(self) -> int:
    with self._reload_lock:
        return len(self.entries)
```

Wrap the complete bodies of `find_next`, `find_next_for_path`, and `find_distant` in the same re-entrant lock. Replace reload with:

```python
def reload_from_disk(
    self,
    index_dir: Path,
    music_dir: Path | None = None,
    path_remap: list[tuple[str, str]] | None = None,
    expected_generation: int | None = None,
) -> int:
    from autodj.index_manifest import IndexConsistencyError, read_manifest

    manifest = read_manifest(index_dir)
    if expected_generation is not None:
        if manifest is None or manifest.generation != expected_generation:
            raise IndexConsistencyError(
                f"expected generation {expected_generation}, got "
                f"{getattr(manifest, 'generation', None)}"
            )
    entries, faiss_index = load_index(
        index_dir,
        music_dir=music_dir,
        path_remap=path_remap,
        expected_generation=expected_generation,
    )
    candidate = SimilarityIndex(faiss_index=faiss_index, entries=entries)
    with self._reload_lock:
        self.entries = candidate.entries
        self.faiss_index = candidate.faiss_index
        self._path_to_idx = candidate._path_to_idx
        self._generation = 0 if manifest is None else manifest.generation
    return len(entries)
```

Apply these exact replacements at the direct-reader sites; each expression obtains one immutable
snapshot for that operation:

```python
# _bridge.py search
for entry in self.sim.entries_snapshot():

# _bridge.py single-path lookups
entry = self.sim.entry_for_path(path)

# _bridge.py random/library operations
entries = self.sim.entries_snapshot()

# _bridge.py settings
"library_size": self.sim.ntotal,

# player.py initial seed
seed_entry = random.choice(self._sim.entries_snapshot())  # nosec B311

# player.py pure shuffle
entries = self._sim.entries_snapshot()
pool = [entry for entry in entries if entry.path not in excluded]
if not pool:
    pool = list(entries)

# player.py background analysis
entry = self._sim.entry_for_path(path)
if entry is not None:
    bpm_str = f"{entry.bpm:.0f} BPM" if entry.bpm else "BPM ?"
    cam = camelot_label(entry.key, entry.mode)

# cli.py resolver/search/reseed sites
entries = sim.entries_snapshot()

# server.py art/audio membership
known = bridge.sim.entry_for_path(path) is not None

# server.py statistics
entries = bridge.sim.entries_snapshot()
```

At each `cli.py` site, use the local `entries` tuple for the existing comprehension,
`next`, or `random.choice`. At each `_bridge.py` site, use the exact snapshot declared above for
the existing loop/comprehension. After these replacements,
`rg -n "\.sim\.entries|sim\.entries|self\._sim\.entries" src/autodj --glob "*.py"` must return no
reader outside `similarity.py`.

Add this module-level server helper and use it in the watcher:

```python
async def reload_published_generation_once(bridge: PlayerBridge, observed: int) -> int:
    from autodj.index_manifest import read_manifest

    cfg = bridge.player._cfg
    manifest = await asyncio.to_thread(read_manifest, cfg.index.active_dir)
    if manifest is None or manifest.generation <= observed:
        return observed
    await asyncio.to_thread(bridge.reload_index_from_disk, manifest.generation)
    return manifest.generation
```

Replace the bridge method with:

```python
def reload_index_from_disk(self, expected_generation: int | None = None) -> int:
    cfg = getattr(self.player, "_cfg", None)
    if cfg is None:
        return self.sim.ntotal
    return self.sim.reload_from_disk(
        cfg.index.active_dir,
        music_dir=cfg.library.music_dir,
        path_remap=cfg.library.path_remap,
        expected_generation=expected_generation,
    )
```

Replace `_index_watcher_loop` with:

```python
async def _index_watcher_loop() -> None:  # pragma: no cover -- long-running task
    from autodj.index_manifest import IndexConsistencyError, read_manifest

    cfg = getattr(bridge.player, "_cfg", None)
    if cfg is None:
        return
    initial = await asyncio.to_thread(read_manifest, cfg.index.active_dir)
    observed = 0 if initial is None else initial.generation
    while True:
        await asyncio.sleep(10)
        try:
            observed = await reload_published_generation_once(bridge, observed)
        except (IndexConsistencyError, OSError, ValueError) as exc:
            logger.debug("Index watcher will retry generation %d: %s", observed + 1, exc)
```

Assignment occurs only after a successful helper return, so a transient mismatch leaves
`observed` unchanged and retries the same generation.

- [ ] **Step 4: Run reload and server tests**

Run: `uv run pytest tests/unit/test_similarity.py tests/unit/test_player.py tests/integration/test_server.py tests/integration/test_server_branches.py -k 'reload or watcher or library_stats or audio or art or seed' -q`

Expected: all selected tests PASS; transient mismatch test calls reload twice for the same generation.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/similarity.py src/autodj/_bridge.py src/autodj/player.py src/autodj/cli.py src/autodj/server.py tests/unit/test_similarity.py tests/unit/test_player.py tests/integration/test_server.py tests/integration/test_server_branches.py
git commit -m "fix: reload indexes as coherent snapshots"
```

### Task 6: Durable inspected model cache and atomic promotion

**Files:**
- Modify: `src/autodj/config.py` (`ModelConfig.revision`)
- Modify: `src/autodj/model.py:27-232`
- Test: `tests/unit/test_config.py`
- Test: `tests/unit/test_model.py:22-177`

- [ ] **Step 1: Write failing cache-status, partial, failure, and concurrency tests**

Use the exact cross-plan API:

```python
import json

from huggingface_hub import constants as hf_constants

from autodj.model import ModelCacheStatus, inspect_model_cache, model_cache_path


def _write_manual_model(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text("{}", encoding="utf-8")
    (path / "model.safetensors").write_bytes(b"weights")


def _write_complete_model(
    path: Path,
    *,
    repo_id: str = "OpenMuQ/MuQ-large-msd-iter",
    revision: str = "main",
) -> None:
    _write_manual_model(path)
    (path / ".autodj-complete").write_text(
        json.dumps({"repo_id": repo_id, "revision": revision}) + "\n",
        encoding="utf-8",
    )


def _populate_download(*, local_dir: str, **_kwargs: object) -> str:
    _write_manual_model(Path(local_dir))
    return local_dir


def test_inspect_model_cache_reports_partial(model_config_auto, index_config) -> None:
    cache = model_cache_path(model_config_auto, index_config)
    cache.mkdir(parents=True)
    (cache / "config.json").write_text("{}", encoding="utf-8")
    assert inspect_model_cache(model_config_auto, index_config) == ModelCacheStatus(
        path=cache, complete=False, reason="missing model weights"
    )
```

Add these tests (and import `threading` and `ThreadPoolExecutor`):

```python
def test_missing_marker_is_not_a_complete_auto_cache(
    model_config_auto: ModelConfig,
    index_config: IndexConfig,
) -> None:
    cache = model_cache_path(model_config_auto, index_config)
    cache.mkdir(parents=True)
    (cache / "config.json").write_text("{}", encoding="utf-8")
    (cache / "model.safetensors").write_bytes(b"weights")
    assert inspect_model_cache(model_config_auto, index_config).reason == (
        "missing completion marker"
    )


def test_failed_download_removes_owned_staging(
    model_config_auto: ModelConfig,
    index_config: IndexConfig,
) -> None:
    with (
        patch("autodj.model.snapshot_download", side_effect=TimeoutError("injected")),
        pytest.raises(ModelLoadError, match="injected"),
    ):
        download_model_if_needed(model_config_auto, index_config)
    assert list(index_config.model_dir.glob(".*.staging-*")) == []
    assert not inspect_model_cache(model_config_auto, index_config).complete


def test_successful_download_is_promoted_with_marker(
    model_config_auto: ModelConfig,
    index_config: IndexConfig,
) -> None:
    def populate(*, local_dir: str, **kwargs: object) -> str:
        staging = Path(local_dir)
        (staging / "config.json").write_text("{}", encoding="utf-8")
        (staging / "model.safetensors").write_bytes(b"weights")
        return local_dir

    with patch("autodj.model.snapshot_download", side_effect=populate) as download:
        result = download_model_if_needed(model_config_auto, index_config)
    assert download.call_count == 1
    assert inspect_model_cache(model_config_auto, index_config).complete
    assert result == model_cache_path(model_config_auto, index_config)
    assert list(index_config.model_dir.glob(".*.staging-*")) == []


def test_concurrent_callers_share_one_download(
    model_config_auto: ModelConfig,
    index_config: IndexConfig,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def populate(*, local_dir: str, **kwargs: object) -> str:
        entered.set()
        assert release.wait(timeout=2)
        staging = Path(local_dir)
        (staging / "config.json").write_text("{}", encoding="utf-8")
        (staging / "model.safetensors").write_bytes(b"weights")
        return local_dir

    with (
        patch("autodj.model.snapshot_download", side_effect=populate) as download,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(download_model_if_needed, model_config_auto, index_config)
        assert entered.wait(timeout=2)
        second = pool.submit(download_model_if_needed, model_config_auto, index_config)
        release.set()
        assert first.result(timeout=2) == second.result(timeout=2)
    assert download.call_count == 1
```

Add identity, marker, sharded-layout, timeout, and durability tests:

```python
def test_cache_identity_includes_repo_and_revision(index_config: IndexConfig) -> None:
    first = ModelConfig(name="org-a/shared-leaf", revision="main")
    second = ModelConfig(name="org-b/shared-leaf", revision="main")
    revised = ModelConfig(name="org-a/shared-leaf", revision="v2")
    assert model_cache_path(first, index_config) != model_cache_path(second, index_config)
    assert model_cache_path(first, index_config) != model_cache_path(revised, index_config)


def test_marker_identity_must_match_requested_model(
    model_config_auto: ModelConfig,
    index_config: IndexConfig,
) -> None:
    cache = model_cache_path(model_config_auto, index_config)
    _write_complete_model(cache, repo_id="other/repo")
    status = inspect_model_cache(model_config_auto, index_config)
    assert not status.complete
    assert status.reason == "completion marker identity mismatch"


@pytest.mark.parametrize(
    ("index_name", "weight_name"),
    [
        ("model.safetensors.index.json", "model-00002-of-00002.safetensors"),
        ("pytorch_model.bin.index.json", "pytorch_model-00002-of-00002.bin"),
    ],
)
def test_sharded_cache_requires_every_indexed_weight(
    tmp_path: Path,
    index_name: str,
    weight_name: str,
) -> None:
    model_dir = tmp_path / "manual"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / index_name).write_text(
        json.dumps({"weight_map": {"a": weight_name}}), encoding="utf-8"
    )
    status = inspect_model_cache(
        ModelConfig(name="org/model", manual_path=model_dir), IndexConfig()
    )
    assert not status.complete
    assert status.reason == f"missing indexed weight: {weight_name}"


def test_unindexed_shard_is_not_accepted(tmp_path: Path) -> None:
    model_dir = tmp_path / "manual"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "model-00001-of-00002.safetensors").write_bytes(b"partial")
    status = inspect_model_cache(
        ModelConfig(name="org/model", manual_path=model_dir), IndexConfig()
    )
    assert status.reason == "missing model weights or shard index"


def test_download_uses_supported_hub_timeouts_without_mutating_global(
    model_config_auto: ModelConfig,
    index_config: IndexConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hf_constants, "HF_HUB_DOWNLOAD_TIMEOUT", 23)

    def populate(*, local_dir: str, **_kwargs: object) -> str:
        _write_manual_model(Path(local_dir))
        return local_dir

    with patch("autodj.model.snapshot_download", side_effect=populate) as download:
        download_model_if_needed(model_config_auto, index_config)
    assert download.call_args.kwargs["etag_timeout"] == _DEFAULT_TIMEOUT_SECONDS
    assert hf_constants.HF_HUB_DOWNLOAD_TIMEOUT == 23


def test_promotion_fsyncs_staging_tree_and_parent(
    model_config_auto: ModelConfig,
    index_config: IndexConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synced_files: list[Path] = []
    synced_dirs: list[Path] = []
    monkeypatch.setattr("autodj.model._fsync_file", synced_files.append)
    monkeypatch.setattr("autodj.model._fsync_directory", synced_dirs.append)
    with patch("autodj.model.snapshot_download", side_effect=_populate_download):
        result = download_model_if_needed(model_config_auto, index_config)
    assert {path.name for path in synced_files} >= {
        "config.json", "model.safetensors", ".autodj-complete"
    }
    assert any(path.name.startswith(f".{result.name}.staging-") for path in synced_dirs)
    assert result.parent in synced_dirs
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_model.py -k 'model or cache or shard or promotion or timeout' -q`

Expected: FAIL because `ModelCacheStatus`, `inspect_model_cache`, marker validation, and locking do not exist.

- [ ] **Step 3: Replace abandoned timeout threads with inspected staged downloads**

Add `revision: str = "main"` to `ModelConfig`, parse optional `model.revision` in
`ModelConfig.from_dict`, and add the exact public API and invariant. Import `hashlib`, `json`,
`os`, `shutil`, `tempfile`, and `huggingface_hub.constants as hf_constants`:

```python
@dataclass(frozen=True)
class ModelCacheStatus:
    path: Path
    complete: bool
    reason: str


_MODEL_CACHE_LOCKS: dict[Path, threading.Lock] = {}
_MODEL_CACHE_LOCKS_GUARD = threading.Lock()
_COMPLETE_MARKER = ".autodj-complete"


def model_cache_path(model_cfg: ModelConfig, index_cfg: IndexConfig) -> Path:
    if model_cfg.manual_path is not None:
        return model_cfg.manual_path
    identity = f"{model_cfg.name}@{model_cfg.revision}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    leaf = model_cfg.name.rstrip("/").split("/")[-1]
    return index_cfg.model_dir / f"{leaf}-{digest}"


def _indexed_weights(path: Path, index_name: str) -> tuple[set[Path], str | None]:
    index_path = path / index_name
    if not index_path.is_file():
        return set(), None
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        names = set(payload["weight_map"].values())
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return set(), f"invalid shard index: {index_name}"
    if not names or not all(isinstance(name, str) and Path(name).name == name for name in names):
        return set(), f"invalid shard index: {index_name}"
    weights = {path / name for name in names}
    missing = sorted(weight.name for weight in weights if not weight.is_file())
    return weights, (f"missing indexed weight: {missing[0]}" if missing else None)


def inspect_model_cache(model_cfg: ModelConfig, index_cfg: IndexConfig) -> ModelCacheStatus:
    path = model_cache_path(model_cfg, index_cfg)
    if not path.is_dir():
        return ModelCacheStatus(path, False, "cache directory missing")
    if not (path / "config.json").is_file():
        return ModelCacheStatus(path, False, "missing config.json")
    weights: set[Path] = set()
    for index_name in ("model.safetensors.index.json", "pytorch_model.bin.index.json"):
        indexed, error = _indexed_weights(path, index_name)
        if error is not None:
            return ModelCacheStatus(path, False, error)
        weights.update(indexed)
    for single in (path / "model.safetensors", path / "pytorch_model.bin"):
        if single.is_file():
            weights.add(single)
    if not weights:
        return ModelCacheStatus(path, False, "missing model weights or shard index")
    if model_cfg.manual_path is None:
        marker = path / _COMPLETE_MARKER
        if not marker.is_file():
            return ModelCacheStatus(path, False, "missing completion marker")
        try:
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ModelCacheStatus(path, False, "invalid completion marker")
        expected = {"repo_id": model_cfg.name, "revision": model_cfg.revision}
        if marker_data != expected:
            return ModelCacheStatus(path, False, "completion marker identity mismatch")
    return ModelCacheStatus(path, True, "complete")


def _cache_lock(path: Path) -> threading.Lock:
    with _MODEL_CACHE_LOCKS_GUARD:
        return _MODEL_CACHE_LOCKS.setdefault(path, threading.Lock())


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_tree(root: Path) -> None:
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        _fsync_file(path)
    directories = sorted(
        (candidate for candidate in root.rglob("*") if candidate.is_dir()),
        key=lambda candidate: len(candidate.parts),
        reverse=True,
    )
    for path in directories:
        _fsync_directory(path)
    _fsync_directory(root)
```

Delete `_snapshot_download_with_timeout`, retry sleeps, and daemon threads. Implement `download_model_if_needed` with one Hub call using supported `etag_timeout`:

```python
def download_model_if_needed(
    model_cfg: ModelConfig,
    index_cfg: IndexConfig,
    hf_token: str | None = None,
) -> Path:
    status = inspect_model_cache(model_cfg, index_cfg)
    if model_cfg.manual_path is not None:
        if not status.complete:
            raise ModelLoadError(f"manual_path is incomplete: {status.path} ({status.reason})")
        return status.path

    cache_dir = status.path
    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    with _cache_lock(cache_dir):
        status = inspect_model_cache(model_cfg, index_cfg)
        if status.complete:
            return status.path
        staging = Path(tempfile.mkdtemp(prefix=f".{cache_dir.name}.staging-", dir=cache_dir.parent))
        try:
            snapshot_download(
                repo_id=model_cfg.name,
                revision=model_cfg.revision,
                local_dir=str(staging),
                ignore_patterns=["*.msgpack", "flax_model*", "tf_model*", "rust_model*"],
                token=hf_token,
                etag_timeout=_DEFAULT_TIMEOUT_SECONDS,
            )
            staged_cfg = ModelConfig(
                name=model_cfg.name,
                revision=model_cfg.revision,
                manual_path=staging,
            )
            staged_status = inspect_model_cache(staged_cfg, index_cfg)
            if not staged_status.complete:
                raise ModelLoadError(f"download incomplete: {staged_status.reason}")
            marker = staging / _COMPLETE_MARKER
            with marker.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {"repo_id": model_cfg.name, "revision": model_cfg.revision},
                    handle,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_tree(staging)
            if cache_dir.exists():
                shutil.rmtree(cache_dir)
            os.replace(staging, cache_dir)
            _fsync_directory(cache_dir.parent)
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            if isinstance(exc, ModelLoadError):
                raise
            raise ModelLoadError(f"Failed to download model '{model_cfg.name}': {exc}") from exc
        return cache_dir
```

`huggingface_hub==1.22.0` exposes `etag_timeout` per `snapshot_download` call. Model-body
streaming uses its supported `huggingface_hub.constants.HF_HUB_DOWNLOAD_TIMEOUT`, which an
operator may set through Hugging Face's documented environment variable before process start.
AutoDJ must not mutate that constant, environment variables, or global socket state, and must not
create timeout threads. Promotion fsyncs every staged file (including all shards and the marker),
descendant directories, the staging root, and the cache parent after `os.replace`. A complete
existing cache is never removed; only an already-inspected incomplete target may be discarded
immediately before the locked promotion.

Import `dataclass`, `os`, `shutil`, and `tempfile`; retain `_DEFAULT_TIMEOUT_SECONDS`. Delete the
obsolete tests `test_retries_on_timeout`, `test_raises_after_all_retries_exhausted`, and
`test_retry_count_equals_max_retries`. Add this line to `model_config_manual` after writing its
config:

```python
(model_dir / "model.safetensors").write_bytes(b"weights")
```

Replace the two remaining obsolete helper patches exactly:

```python
# test_calls_snapshot_download_if_not_cached
with patch("autodj.model.snapshot_download", side_effect=_populate_download) as mock_dl:
    download_model_if_needed(model_config_auto, index_config)
mock_dl.assert_called_once()
assert mock_dl.call_args.kwargs["repo_id"] == model_config_auto.name
assert mock_dl.call_args.kwargs["revision"] == model_config_auto.revision

# test_raises_model_load_error_on_download_failure
with (
    patch("autodj.model.snapshot_download", side_effect=Exception("network error")),
    pytest.raises(ModelLoadError, match="network error"),
):
    download_model_if_needed(model_config_auto, index_config)
```

In `test_returns_cached_path_if_exists`, add both required cache files before calling the function:

```python
_write_complete_model(
    cache_dir,
    repo_id=model_config_auto.name,
    revision=model_config_auto.revision,
)
```

- [ ] **Step 4: Run the full model test module**

Run: `uv run pytest tests/unit/test_model.py -q`

Expected: all tests PASS; no retry-thread tests remain, and the concurrent test performs one download.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/config.py src/autodj/model.py tests/unit/test_config.py tests/unit/test_model.py
git commit -m "fix: promote only complete model caches"
```

### Task 7: Shared liner boundary and bounded atomic upload

**Files:**
- Create: `src/autodj/liner_files.py`
- Create: `tests/unit/test_liner_files.py`
- Modify: `src/autodj/server.py:628-742`
- Test: `tests/integration/test_server.py:216-350`
- Test: `tests/integration/test_server_branches.py:261-324`

- [ ] **Step 1: Write failing path and upload contract tests**

```python
# tests/unit/test_liner_files.py
from __future__ import annotations

from pathlib import Path

import pytest

from autodj.liner_files import InvalidLinerName, resolve_liner_path


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "../clip.mp3", r"..\liners-backup\clip.mp3", "sub/clip.mp3",
     r"sub\clip.mp3", "CON.mp3", "nul.wav", "clip.mp3:stream", "clip.mp3.",
     "clip.mp3 "],
)
def test_resolve_liner_path_rejects_escape_and_device_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(InvalidLinerName):
        resolve_liner_path(tmp_path / "liners", name)


def test_resolve_liner_path_accepts_plain_filename(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    assert resolve_liner_path(root, "station-id.mp3") == root.resolve() / "station-id.mp3"
```

Add these storage tests to the same unit module:

```python
class BytesReader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def read(self, size: int = -1) -> bytes:
        payload, self._payload = self._payload[:size], self._payload[size:]
        return payload


@pytest.mark.asyncio
async def test_oversized_upload_removes_temporary_file(tmp_path: Path) -> None:
    from autodj.liner_files import LinerTooLargeError, store_liner_upload

    root = tmp_path / "liners"
    with pytest.raises(LinerTooLargeError):
        await store_liner_upload(
            root, "clip.mp3", BytesReader(b"x" * 51), max_bytes=50, replace=False
        )
    assert list(root.glob(".liner-upload-*")) == []
    assert not (root / "clip.mp3").exists()


@pytest.mark.asyncio
async def test_conflict_requires_explicit_atomic_replace(tmp_path: Path) -> None:
    from autodj.liner_files import LinerConflictError, store_liner_upload

    root = tmp_path / "liners"
    root.mkdir()
    target = root / "clip.mp3"
    target.write_bytes(b"old")
    with pytest.raises(LinerConflictError):
        await store_liner_upload(
            root, target.name, BytesReader(b"new"), max_bytes=50, replace=False
        )
    assert target.read_bytes() == b"old"
    await store_liner_upload(
        root, target.name, BytesReader(b"new"), max_bytes=50, replace=True
    )
    assert target.read_bytes() == b"new"
```

In `tests/integration/test_server.py`, post a 51-byte `files={"file":
("clip.mp3", b"x" * 51, "audio/mpeg")}` payload with the fixture limit set to 50 and assert 413;
repeat against an existing `clip.mp3` and assert 409, then add `?replace=true` and assert 200.
In `tests/integration/test_server_branches.py`, add exact route-boundary coverage. The `{escaped:path}`
catch-all makes ASGI-decoded slashes reach validation rather than falling through as a router 404:

```python
@pytest.mark.parametrize(
    "escaped",
    ["..%2Fclip.mp3", "sub%2Fclip.mp3", "clip.mp3%3Astream", "clip.mp3.", "clip.mp3%20"],
)
@pytest.mark.parametrize("method", ["get", "delete"])
def test_liner_file_routes_reject_encoded_or_windows_aliases(
    client: TestClient,
    escaped: str,
    method: str,
) -> None:
    response = getattr(client, method)(f"/api/liners/file/{escaped}")
    assert response.status_code == 400


@pytest.mark.parametrize(
    "name",
    ["../clip.mp3", r"..\liners-backup\clip.mp3", r"sub\clip.mp3",
     "clip.mp3:stream", "clip.mp3.", "clip.mp3 "],
)
def test_liner_upload_rejects_non_plain_names(client: TestClient, name: str) -> None:
    response = client.post(
        "/api/liners/upload", files={"file": (name, b"audio", "audio/mpeg")}
    )
    assert response.status_code == 400


def test_missing_plain_liner_is_404(client: TestClient) -> None:
    assert client.get("/api/liners/file/missing.mp3").status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_liner_files.py tests/integration/test_server.py -k liner -q`

Expected: FAIL because the shared helper, limit, conflict, and replacement behavior do not exist.

- [ ] **Step 3: Implement containment and streamed upload**

```python
# src/autodj/liner_files.py
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol


class InvalidLinerName(ValueError):
    """Raised when a liner name is not one contained plain filename."""


class LinerConflictError(FileExistsError):
    """Raised when a non-replacing upload targets an existing liner."""


class LinerTooLargeError(ValueError):
    """Raised when a streamed upload crosses its configured byte limit."""


class AsyncReader(Protocol):
    async def read(self, size: int = -1) -> bytes:
        raise NotImplementedError


_WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10))}


def resolve_liner_path(root: Path, name: str, *, require_file: bool = False) -> Path:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ":" in name
        or name != name.rstrip(" .")
    ):
        raise InvalidLinerName("liner name must be one plain filename")
    if Path(name).stem.rstrip(". ").upper() in _WINDOWS_DEVICES:
        raise InvalidLinerName("reserved device filename")
    root = root.resolve()
    target = (root / name).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise InvalidLinerName("liner path escapes configured root") from exc
    if require_file and not target.is_file():
        raise FileNotFoundError(name)
    return target


async def store_liner_upload(
    root: Path,
    name: str,
    reader: AsyncReader,
    *,
    max_bytes: int,
    replace: bool,
) -> tuple[Path, int]:
    target = resolve_liner_path(root, name)
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".liner-upload-", suffix=".tmp", dir=root)
    tmp = Path(tmp_name)
    total = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            while chunk := await reader.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise LinerTooLargeError(f"upload exceeds {max_bytes} bytes")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(tmp, target)
        else:
            try:
                os.link(tmp, target)
            except FileExistsError as exc:
                raise LinerConflictError(name) from exc
            tmp.unlink()
        return target, total
    finally:
        tmp.unlink(missing_ok=True)
```

Replace the three file routes with these definitions (retain the existing
`_resolve_liner_folder` helper and import `mimetypes` at module scope):

```python
@app.post("/api/liners/upload")
async def api_liner_upload(
    file: UploadFile = File(),
    replace: bool = False,
) -> dict[str, str | int]:
    from autodj.liner_files import (
        InvalidLinerName,
        LinerConflictError,
        LinerTooLargeError,
        store_liner_upload,
    )
    from autodj.liners import LINER_EXTS

    name = file.filename or ""
    extension = Path(name).suffix.lower()
    if extension not in LINER_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported extension {extension!r}; allowed: {', '.join(LINER_EXTS)}",
        )
    try:
        server_cfg = getattr(bridge.player._cfg, "server", None)
        configured_limit = getattr(server_cfg, "liner_upload_max_bytes", None)
        max_bytes = (
            configured_limit
            if isinstance(configured_limit, int)
            else 50 * 1024 * 1024
        )
        target, size = await store_liner_upload(
            _resolve_liner_folder(),
            name,
            file,
            max_bytes=max_bytes,
            replace=replace,
        )
    except InvalidLinerName as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LinerConflictError as exc:
        raise HTTPException(status_code=409, detail="Liner already exists") from exc
    except LinerTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    return {"filename": target.name, "size": size}


@app.delete("/api/liners/file/{name}")
async def api_liner_delete(name: str) -> dict[str, str]:
    from autodj.liner_files import InvalidLinerName, resolve_liner_path

    try:
        target = resolve_liner_path(_resolve_liner_folder(), name, require_file=True)
    except InvalidLinerName as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Liner not found") from exc
    try:
        target.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Unable to delete liner") from exc
    return {"deleted": target.name}


@app.get("/api/liners/file/{name}")
async def api_liner_file(name: str) -> FileResponse:
    from autodj.liner_files import InvalidLinerName, resolve_liner_path

    try:
        target = resolve_liner_path(_resolve_liner_folder(), name, require_file=True)
    except InvalidLinerName as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Liner not found") from exc
    media_type, _encoding = mimetypes.guess_type(str(target))
    return FileResponse(target, media_type=media_type or "application/octet-stream")


# Register these after the plain-name routes. Starlette chooses the earlier route for ordinary
# names; decoded slash-containing paths reach these handlers and receive the required 400.
@app.get("/api/liners/file/{escaped:path}", include_in_schema=False)
async def api_liner_file_reject_path(escaped: str) -> None:
    raise HTTPException(status_code=400, detail="liner name must be one plain filename")


@app.delete("/api/liners/file/{escaped:path}", include_in_schema=False)
async def api_liner_delete_reject_path(escaped: str) -> None:
    raise HTTPException(status_code=400, detail="liner name must be one plain filename")
```

- [ ] **Step 4: Run liner tests**

Run: `uv run pytest tests/unit/test_liner_files.py tests/integration/test_server.py tests/integration/test_server_branches.py -k liner -q`

Expected: all selected tests PASS and oversize/conflict paths leave no `.liner-upload-*` file.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/liner_files.py src/autodj/server.py tests/unit/test_liner_files.py tests/integration/test_server.py tests/integration/test_server_branches.py
git commit -m "fix: contain and bound liner uploads"
```

### Task 8: `ServerConfig`, ordinary TOML parsing, CLI overrides, and LAN startup gate

**Files:**
- Modify: `src/autodj/config.py:622-675,699-770`
- Modify: `src/autodj/cli.py:1348-1685`
- Modify: `src/autodj/server.py:1282-1422`
- Modify: `config.toml.example:54-64`
- Test: `tests/unit/test_config.py`
- Test: `tests/unit/test_cli.py`
- Test: `tests/integration/test_server.py:972-1019`

- [ ] **Step 1: Write failing config and CLI exposure tests**

```python
def test_server_config_defaults_to_loopback() -> None:
    cfg = ServerConfig.from_dict({})
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8080
    assert cfg.access_token is None
    assert cfg.insecure_lan is False
    assert cfg.liner_upload_max_bytes == 50 * 1024 * 1024
    assert cfg.effective_allowed_hosts() == ["127.0.0.1"]
    assert cfg.effective_allowed_origins() == ["http://127.0.0.1:8080"]


def test_server_config_derives_policy_from_custom_bind() -> None:
    cfg = ServerConfig.from_dict({"host": "127.0.0.2", "port": 9090})
    assert cfg.effective_allowed_hosts() == ["127.0.0.2"]
    assert cfg.effective_allowed_origins() == ["http://127.0.0.2:9090"]


def test_server_config_canonicalizes_derived_default_port() -> None:
    cfg = ServerConfig.from_dict({"host": "127.0.0.2", "port": 80})
    assert cfg.effective_allowed_origins() == ["http://127.0.0.2"]


def test_server_config_parses_security_fields() -> None:
    cfg = ServerConfig.from_dict({
        "host": "192.168.1.10", "port": 9000, "access_token": "s" * 32,
        "allowed_hosts": ["radio.local"], "allowed_origins": ["https://radio.local:9000"],
        "session_ttl_seconds": 3600, "liner_upload_max_mib": 25,
    })
    assert cfg.allowed_hosts == ["radio.local"]
    assert cfg.liner_upload_max_bytes == 25 * 1024 * 1024


def test_server_config_rejects_weak_token_without_echoing_it() -> None:
    weak = "do-not-print-me"
    with pytest.raises(ValueError) as raised:
        ServerConfig.from_dict({"access_token": weak})
    assert "at least 32 UTF-8 bytes" in str(raised.value)
    assert weak not in str(raised.value)


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://radio.local",
        "https://user:pass@radio.local",
        "https://radio.local/private",
        "https://radio.local?token=x",
        "https://radio.local#fragment",
    ],
)
def test_server_config_rejects_non_origin_urls(origin: str) -> None:
    with pytest.raises(ValueError, match="allowed_origins"):
        ServerConfig.from_dict({"allowed_origins": [origin]})


def test_server_config_canonicalizes_allowed_origins() -> None:
    cfg = ServerConfig.from_dict({
        "allowed_origins": ["HTTPS://Radio.Local:8443/", "https://Radio.Local:443/"]
    })
    assert cfg.allowed_origins == ["https://radio.local:8443", "https://radio.local"]
```

Add `cfg.server = ServerConfig()` to `_make_cfg`, then add these tests to `TestCmdServe`:

```python
def test_lan_bind_requires_token_or_acknowledgement(self) -> None:
    cfg = _make_cfg()
    with (
        patch("autodj.config.load_config", return_value=cfg),
        patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=_make_sim()),
        patch("autodj.server.serve") as serve_mock,
    ):
        result = CliRunner().invoke(cli, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 1
    assert "LAN binding requires" in result.output
    serve_mock.assert_not_called()


def test_authenticated_lan_overrides_reach_server_without_token_output(self) -> None:
    cfg = _make_cfg()
    with (
        patch("autodj.config.load_config", return_value=cfg),
        patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=_make_sim()),
        patch("autodj.server.serve") as serve_mock,
    ):
        result = CliRunner().invoke(
            cli,
            [
                "serve", "--host", "0.0.0.0", "--access-token", "s" * 32,
                "--allowed-host", "radio.local",
                "--allowed-origin", "http://radio.local:8080",
            ],
        )
    assert result.exit_code == 0
    assert "s" * 32 not in result.output
    assert cfg.server.access_token == "s" * 32
    assert cfg.server.allowed_hosts == ["radio.local"]
    serve_mock.assert_called_once()


def test_weak_cli_token_is_rejected_without_echoing_it(self) -> None:
    cfg = _make_cfg()
    weak = "do-not-print-me"
    with (
        patch("autodj.config.load_config", return_value=cfg),
        patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=_make_sim()),
        patch("autodj.server.serve") as serve_mock,
    ):
        result = CliRunner().invoke(
            cli,
            [
                "serve", "--host", "0.0.0.0", "--access-token", weak,
                "--allowed-host", "radio.local",
                "--allowed-origin", "https://radio.local:8080",
            ],
        )
    assert result.exit_code == 1
    assert "at least 32 UTF-8 bytes" in result.output
    assert weak not in result.output
    serve_mock.assert_not_called()


def test_cli_rejects_allowed_origin_with_path(self) -> None:
    cfg = _make_cfg()
    with (
        patch("autodj.config.load_config", return_value=cfg),
        patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=_make_sim()),
        patch("autodj.server.serve") as serve_mock,
    ):
        result = CliRunner().invoke(
            cli,
            [
                "serve", "--host", "0.0.0.0", "--insecure-lan",
                "--allowed-host", "radio.local",
                "--allowed-origin", "https://radio.local/private",
            ],
        )
    assert result.exit_code == 1
    assert "without userinfo, path, query, or fragment" in result.output
    serve_mock.assert_not_called()


def test_insecure_lan_is_explicit_and_warned(self) -> None:
    cfg = _make_cfg()
    with (
        patch("autodj.config.load_config", return_value=cfg),
        patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=_make_sim()),
        patch("autodj.server.serve") as serve_mock,
    ):
        result = CliRunner().invoke(
            cli,
            [
                "serve", "--host", "0.0.0.0", "--insecure-lan",
                "--allowed-host", "radio.local",
                "--allowed-origin", "http://radio.local:8080",
            ],
        )
    assert result.exit_code == 0
    assert "WARNING" in result.output
    serve_mock.assert_called_once()


def test_serve_uses_toml_host_and_port_when_flags_are_omitted(self) -> None:
    cfg = _make_cfg()
    cfg.server = ServerConfig(host="127.0.0.2", port=9090)
    with (
        patch("autodj.config.load_config", return_value=cfg),
        patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=_make_sim()),
        patch("autodj.server.serve") as serve_mock,
    ):
        result = CliRunner().invoke(cli, ["serve"])
    assert result.exit_code == 0
    assert serve_mock.call_args.kwargs["host"] == "127.0.0.2"
    assert serve_mock.call_args.kwargs["port"] == 9090
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_cli.py -k 'server_config or insecure_lan or access_token or lan_requires' -q`

Expected: FAIL because `ServerConfig`, token/origin validation, and CLI security options do not exist.

- [ ] **Step 3: Add config, parsing, overrides, and bind validation**

```python
MIN_ACCESS_TOKEN_BYTES = 32


def validate_access_token(token: str | None) -> None:
    if token is not None and len(token.encode("utf-8")) < MIN_ACCESS_TOKEN_BYTES:
        raise ValueError("server.access_token must be at least 32 UTF-8 bytes")


def canonicalize_allowed_origin(value: str) -> str:
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("server.allowed_origins contains an invalid origin") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "server.allowed_origins entries must be HTTP(S) origins without "
            "userinfo, path, query, or fragment"
        )
    hostname = parsed.hostname.lower()
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    suffix = "" if port is None or port == default_port else f":{port}"
    return f"{parsed.scheme.lower()}://{rendered_host}{suffix}"


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    access_token: str | None = None
    insecure_lan: bool = False
    # None means derive from the effective host/port after TOML and CLI overrides.
    allowed_hosts: list[str] | None = None
    allowed_origins: list[str] | None = None
    session_ttl_seconds: int = 24 * 60 * 60
    liner_upload_max_bytes: int = 50 * 1024 * 1024

    def effective_allowed_hosts(self) -> list[str]:
        if self.allowed_hosts is not None:
            return self.allowed_hosts
        return [] if self.host in {"0.0.0.0", "::"} else [self.host.lower()]

    def effective_allowed_origins(self) -> list[str]:
        if self.allowed_origins is not None:
            return self.allowed_origins
        if self.host in {"0.0.0.0", "::"}:
            return []
        host = self.host.lower()
        origin_host = f"[{host}]" if ":" in host else host
        return [canonicalize_allowed_origin(f"http://{origin_host}:{self.port}")]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServerConfig:
        port = int(data.get("port", 8080))
        ttl = int(data.get("session_ttl_seconds", 24 * 60 * 60))
        max_mib = int(data.get("liner_upload_max_mib", 50))
        access_token = str(data["access_token"]) if data.get("access_token") else None
        validate_access_token(access_token)
        if not 1 <= port <= 65535:
            raise ValueError("server.port must be between 1 and 65535")
        if ttl < 60:
            raise ValueError("server.session_ttl_seconds must be at least 60")
        if max_mib < 1:
            raise ValueError("server.liner_upload_max_mib must be at least 1")
        return cls(
            host=str(data.get("host", "127.0.0.1")), port=port,
            access_token=access_token,
            insecure_lan=bool(data.get("insecure_lan", False)),
            allowed_hosts=(
                [str(value).lower() for value in data["allowed_hosts"]]
                if "allowed_hosts" in data else None
            ),
            allowed_origins=(
                [canonicalize_allowed_origin(str(value)) for value in data["allowed_origins"]]
                if "allowed_origins" in data else None
            ),
            session_ttl_seconds=ttl,
            liner_upload_max_bytes=max_mib * 1024 * 1024,
        )
```

Replace the root field list and loader return with these complete definitions:

```python
@dataclass
class AutoDJConfig:
    library: LibraryConfig
    index: IndexConfig
    playback: PlaybackConfig
    model: ModelConfig
    huggingface: HuggingFaceConfig
    config_path: Path
    presets: dict[str, Preset] = field(default_factory=dict)
    replaygain: ReplayGainConfig = field(default_factory=ReplayGainConfig)
    djmix: DjMixConfig = field(default_factory=DjMixConfig)
    transitions: TransitionsConfig = field(default_factory=TransitionsConfig)
    server: ServerConfig = field(default_factory=ServerConfig)


return AutoDJConfig(
    library=LibraryConfig.from_dict(raw.get("library", {})),
    index=IndexConfig.from_dict(raw.get("index", {})),
    playback=PlaybackConfig.from_dict(raw.get("playback", {})),
    model=ModelConfig.from_dict(raw.get("model", {})),
    huggingface=HuggingFaceConfig.from_dict(raw.get("huggingface", {})),
    replaygain=ReplayGainConfig.from_dict(raw.get("replaygain", {})),
    djmix=DjMixConfig.from_dict(raw.get("djmix", {})),
    transitions=TransitionsConfig.from_dict(raw.get("transitions", {})),
    presets=load_user_presets(presets_raw),
    config_path=config_path,
    server=ServerConfig.from_dict(raw.get("server", {})),
)
```

Replace the existing host/port Click decorators and add the security decorators:

```python
@click.option("--host", default=None, type=str, help="Interface to bind; defaults to [server].host.")
@click.option("--port", default=None, type=click.IntRange(1, 65535), help="Port; defaults to [server].port.")
@click.option("--access-token", default=None, type=str, help="Token required for LAN clients.")
@click.option(
    "--insecure-lan",
    is_flag=True,
    default=None,
    help="Acknowledge unauthenticated LAN exposure.",
)
@click.option("--allowed-host", "allowed_hosts", multiple=True, help="Allowed HTTP Host name.")
@click.option(
    "--allowed-origin",
    "allowed_origins",
    multiple=True,
    help="Allowed browser origin including scheme and port.",
)
```

Change the corresponding `cmd_serve` annotations to:

```python
host: str | None,
port: int | None,
access_token: str | None,
insecure_lan: bool | None,
allowed_hosts: tuple[str, ...],
allowed_origins: tuple[str, ...],
```

Immediately after loading `cfg`, apply only explicit overrides, validate before loading the index,
and then use the resolved values for the banner and `serve` call:

```python
if host is not None:
    cfg.server.host = host
if port is not None:
    cfg.server.port = port
if access_token is not None:
    cfg.server.access_token = access_token
if insecure_lan is not None:
    cfg.server.insecure_lan = insecure_lan
if allowed_hosts:
    cfg.server.allowed_hosts = [value.lower() for value in allowed_hosts]
if allowed_origins:
    cfg.server.allowed_origins = list(allowed_origins)
try:
    validate_server_exposure(cfg.server)
except ValueError as exc:
    raise click.ClickException(str(exc)) from exc
if cfg.server.insecure_lan and not is_loopback_bind(cfg.server.host):
    console.print("[yellow]WARNING: LAN access is unauthenticated (--insecure-lan).[/]")
host = cfg.server.host
port = cfg.server.port
```

Add these pure helpers to `config.py` so both CLI and direct server entry points enforce the same
boundary:

```python
def is_loopback_bind(host: str) -> bool:
    import ipaddress

    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_server_exposure(cfg: ServerConfig) -> None:
    validate_access_token(cfg.access_token)
    if cfg.allowed_origins is not None:
        cfg.allowed_origins = [
            canonicalize_allowed_origin(value) for value in cfg.allowed_origins
        ]
    if not cfg.effective_allowed_hosts() or not cfg.effective_allowed_origins():
        raise ValueError(
            "wildcard binding requires explicit allowed_hosts and allowed_origins"
        )
    if not is_loopback_bind(cfg.host) and not cfg.access_token and not cfg.insecure_lan:
        raise ValueError(
            "LAN binding requires [server] access_token/--access-token or explicit --insecure-lan"
        )
```

Change `server.serve`'s `host`/`port` defaults and add this code before constructing `Player`:

```python
# Signature parameters
host: str | None = None,
port: int | None = None,

# First lines of the body
host = cfg.server.host if host is None else host
port = cfg.server.port if port is None else port
cfg.server.host = host
cfg.server.port = port
validate_server_exposure(cfg.server)
```

Import `ServerConfig`, `is_loopback_bind`, and `validate_server_exposure` from `autodj.config` in
`cli.py`; add `validate_server_exposure` to the existing `autodj.config` import in `server.py`.

Append this ordinary TOML section to `config.toml.example`; do not add generic environment
parsing or edit Compose:

```toml
[server]
host = "127.0.0.1"
port = 8080
# access_token = "generate-at-least-32-random-bytes"
insecure_lan = false
# Wildcard/LAN binds must set both, for example:
# allowed_hosts = ["radio.local"]
# allowed_origins = ["https://radio.local:8080"]
session_ttl_seconds = 86400
liner_upload_max_mib = 50
```

Omit `allowed_hosts` and `allowed_origins` from the checked-in loopback example so the example
demonstrates dynamic derivation. Add commented LAN examples instead; wildcard binds must supply
both explicit lists. In `SecurityPolicy`, consume `config.effective_allowed_hosts()` and
`config.effective_allowed_origins()`, never the nullable storage fields directly.

Add an integration test constructing `ServerConfig(host="127.0.0.2", port=9090)`, creating a
`TestClient(..., base_url="http://127.0.0.2:9090")`, and asserting `/api/version` returns 200.
This proves custom loopback host/port defaults remain usable without stale 127.0.0.1:8080 policy.

- [ ] **Step 4: Run config, CLI, and serve tests**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_cli.py tests/unit/test_cli_more.py tests/integration/test_server.py -k 'serve or server_config or lan or token' -q`

Expected: all selected tests PASS; LAN without either explicit security choice exits 1, weak
tokens and malformed origins are rejected without echoing secrets, and custom loopback policy is
derived from its effective host/port.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/config.py src/autodj/cli.py src/autodj/server.py config.toml.example tests/unit/test_config.py tests/unit/test_cli.py tests/integration/test_server.py
git commit -m "feat: gate explicit LAN server exposure"
```

### Task 9: Signed sessions, constant-time token checks, and host/origin policy

**Files:**
- Create: `src/autodj/security.py`
- Create: `tests/unit/test_security.py`

- [ ] **Step 1: Write failing policy tests**

```python
from dataclasses import replace
import re
import secrets

from autodj.config import ServerConfig
from autodj.security import SecurityPolicy, new_request_id


def _server(**changes: object) -> ServerConfig:
    return replace(
        ServerConfig(
            allowed_hosts=["radio.local", "::1"],
            allowed_origins=["https://radio.local:8080"],
        ),
        **changes,
    )


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("radio.local", True),
        ("radio.local:8080", True),
        ("[::1]:8080", True),
        ("evil-radio.local", False),
        ("radio.local.evil.example", False),
        ("evil.example@radio.local", False),
        (None, False),
    ],
)
def test_host_policy_requires_an_exact_hostname(host: str | None, allowed: bool) -> None:
    assert SecurityPolicy(_server()).host_allowed(host) is allowed


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        ("https://radio.local:8080", True),
        ("https://radio.local:8080/", True),
        ("http://radio.local:8080", False),
        ("https://radio.local", False),
        ("https://radio.local:8080.evil.example", False),
        (None, False),
    ],
)
def test_origin_policy_is_exact(origin: str | None, allowed: bool) -> None:
    assert SecurityPolicy(_server()).origin_allowed(origin) is allowed


def test_session_round_trip_and_expiry() -> None:
    policy = SecurityPolicy(_server(access_token="secret", session_ttl_seconds=60), now=lambda: 1000)
    cookie = policy.issue_session()
    assert policy.verify_session(cookie) is True
    expired = SecurityPolicy(_server(access_token="secret"), now=lambda: 1061)
    assert expired.verify_session(cookie) is False


def test_token_comparison_is_constant_time(monkeypatch) -> None:
    seen: list[tuple[bytes, bytes]] = []
    monkeypatch.setattr(secrets, "compare_digest", lambda left, right: seen.append((left, right)) or True)
    assert SecurityPolicy(_server(access_token="secret")).verify_access_token("candidate")
    assert seen == [(b"candidate", b"secret")]


def test_unicode_candidate_is_compared_as_bytes_without_type_error() -> None:
    policy = SecurityPolicy(_server(access_token="secret"))
    assert not policy.verify_access_token("not-secret-\N{LOCK}")


def test_tampered_session_and_request_id() -> None:
    policy = SecurityPolicy(_server(access_token="secret"), now=lambda: 1000)
    cookie = policy.issue_session()
    assert not policy.verify_session(cookie + "0")
    assert re.fullmatch(r"[0-9a-f]{32}", new_request_id())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_security.py -q`

Expected: FAIL because `autodj.security` does not exist.

- [ ] **Step 3: Implement the security policy as a pure tested module**

Implement `SecurityPolicy` with these exact public methods:

```python
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from autodj.config import ServerConfig

COOKIE_NAME = "autodj_session"


@dataclass
class SecurityPolicy:
    config: ServerConfig
    secure_cookie: bool = False
    now: Callable[[], float] = time.time

    @property
    def authentication_required(self) -> bool:
        return self.config.access_token is not None

    def verify_access_token(self, candidate: str) -> bool:
        expected = self.config.access_token
        return expected is not None and secrets.compare_digest(
            candidate.encode("utf-8"), expected.encode("utf-8")
        )

    def issue_session(self) -> str:
        if self.config.access_token is None:
            raise RuntimeError("access token is not configured")
        expires = int(self.now()) + self.config.session_ttl_seconds
        nonce = secrets.token_hex(16)
        payload = f"{expires}.{nonce}"
        signature = hmac.new(
            self.config.access_token.encode("utf-8"), payload.encode("ascii"), hashlib.sha256
        ).hexdigest()
        return f"{payload}.{signature}"

    def verify_session(self, value: str | None) -> bool:
        if value is None or self.config.access_token is None:
            return False
        try:
            expires_text, nonce, signature = value.split(".", 2)
            expires = int(expires_text)
        except (ValueError, AttributeError):
            return False
        payload = f"{expires}.{nonce}"
        expected = hmac.new(
            self.config.access_token.encode("utf-8"), payload.encode("ascii"), hashlib.sha256
        ).hexdigest()
        return expires >= int(self.now()) and secrets.compare_digest(
            signature.encode("ascii"), expected.encode("ascii")
        )

    def host_allowed(self, host_header: str | None) -> bool:
        if not host_header:
            return False
        try:
            parsed = urlsplit(f"//{host_header}")
            hostname = parsed.hostname
            _port = parsed.port
        except ValueError:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        return hostname is not None and hostname.lower() in set(
            self.config.effective_allowed_hosts()
        )

    def origin_allowed(self, origin: str | None) -> bool:
        return origin is not None and origin.rstrip("/") in set(
            self.config.effective_allowed_origins()
        )
```

Add these imports and exact helpers; their closed field list prevents tokens, request bodies, query
strings, and concrete filesystem paths from entering audit JSON:

```python
import json
import uuid


def new_request_id() -> str:
    return uuid.uuid4().hex


def audit_record(
    request_id: str,
    action: str,
    outcome: str,
    method: str | None = None,
    route: str | None = None,
    status: int | None = None,
) -> str:
    record: dict[str, str | int] = {
        "action": action,
        "outcome": outcome,
        "request_id": request_id,
    }
    if method is not None:
        record["method"] = method
    if route is not None:
        record["route"] = route
    if status is not None:
        record["status"] = status
    return json.dumps(record, separators=(",", ":"), sort_keys=True)
```

- [ ] **Step 4: Run policy tests**

Run: `uv run pytest tests/unit/test_security.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/security.py tests/unit/test_security.py
git commit -m "feat: add signed server security policy"
```

### Task 10: HTTP/WebSocket enforcement, login, request IDs, and audit events

**Files:**
- Modify: `src/autodj/security.py`
- Modify: `src/autodj/server.py:40-52,279-333,461-1171,1177-1214,1378`
- Modify: `tests/integration/conftest.py:18-35`
- Modify: `tests/integration/_helpers.py:73-113`
- Test: `tests/integration/test_server.py`
- Test: `tests/integration/test_server_branches.py`

- [ ] **Step 1: Write failing HTTP, WebSocket, origin, and audit tests**

Add this builder and focused tests to `tests/integration/test_server_branches.py` (import
`json`, `logging`, `pytest`, `TestClient`, `WebSocketDisconnect`, `ServerConfig`, and
`COOKIE_NAME`, plus the existing `_make_player_mock`/`_make_sim_mock` helpers):

```python
def _security_client(*, secure_cookie: bool = False) -> TestClient:
    scheme = "https" if secure_cookie else "http"
    origin = f"{scheme}://testserver"
    player = _make_player_mock()
    player._cfg.server = ServerConfig(
        access_token="secret",
        allowed_hosts=["testserver"],
        allowed_origins=[origin],
    )
    bridge = PlayerBridge(player=player, sim=_make_sim_mock())
    return TestClient(
        create_app(bridge, secure_cookie=secure_cookie),
        base_url=origin,
        headers={"Host": "testserver", "Origin": origin},
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/status",
        "/api/audio?path=Z%3A%2FMusic%2Fsong_0.flac",
        "/api/library/job",
        "/api/profiles",
        "/api/liners",
    ],
)
def test_secured_route_categories_require_session(path: str) -> None:
    response = _security_client().get(path)
    assert response.status_code == 401
    assert response.headers["X-Request-ID"]


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("post", "/api/skip", {}),                         # playback/state
        ("post", "/api/profiles", {"json": {}}),         # profiles
        ("delete", "/api/profiles/default", {}),          # profiles delete
        ("post", "/api/liners/upload", {                  # liner storage
            "files": {"file": ("id.mp3", b"x", "audio/mpeg")}
        }),
        ("delete", "/api/liners/file/id.mp3", {}),        # liner delete
        ("post", "/api/library/run", {"json": {}}),      # background job
    ],
)
def test_unsafe_route_categories_reject_before_parsing_body(
    method: str,
    path: str,
    kwargs: dict[str, object],
) -> None:
    response = getattr(_security_client(), method)(path, **kwargs)
    assert response.status_code == 401


def test_login_sets_hardened_cookie_and_unlocks_api() -> None:
    client = _security_client(secure_cookie=True)
    assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
    response = client.post("/api/login", json={"token": "secret"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Secure" in cookie
    assert client.get("/api/status").status_code == 200


def test_tampered_cookie_is_rejected() -> None:
    client = _security_client()
    assert client.post("/api/login", json={"token": "secret"}).status_code == 200
    client.cookies.set(COOKIE_NAME, "tampered")
    assert client.get("/api/status").status_code == 401


def test_anonymous_loopback_fixture_remains_usable(client: TestClient) -> None:
    assert client.get("/api/status").status_code == 200


def test_host_and_origin_are_rejected_before_route() -> None:
    client = _security_client()
    assert client.get("/api/version", headers={"Host": "evil.example"}).status_code == 403
    assert client.post(
        "/api/login",
        headers={"Origin": "http://evil.example"},
        json={"token": "secret"},
    ).status_code == 403


def test_websocket_rejects_origin_then_missing_cookie(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _security_client()
    with caplog.at_level(logging.WARNING, logger="autodj.audit"):
        with pytest.raises(WebSocketDisconnect) as wrong_origin:
            with client.websocket_connect("/ws", headers={"Origin": "http://evil.example"}):
                raise AssertionError("handshake unexpectedly succeeded")
        assert wrong_origin.value.code == 4403
        with pytest.raises(WebSocketDisconnect) as missing_cookie:
            with client.websocket_connect("/ws"):
                raise AssertionError("handshake unexpectedly succeeded")
        assert missing_cookie.value.code == 4401
    rejected = [
        json.loads(item.message) for item in caplog.records
        if item.name == "autodj.audit" and json.loads(item.message)["outcome"] == "rejected"
    ]
    assert [record["status"] for record in rejected[-2:]] == [403, 401]
    assert all(record["route"] == "/ws" for record in rejected[-2:])


def test_audit_record_is_structured_and_redacted(caplog: pytest.LogCaptureFixture) -> None:
    client = _security_client()
    private_path = "Z:/Private/Music/secret.flac"
    with caplog.at_level(logging.INFO, logger="autodj.audit"):
        response = client.post(
            "/api/login",
            json={"token": "wrong", "path": private_path},
        )
    assert response.status_code == 401
    audit_messages = [item.message for item in caplog.records if item.name == "autodj.audit"]
    record = json.loads(audit_messages[-1])
    assert record["request_id"] == response.headers["X-Request-ID"]
    assert record["method"] == "POST"
    assert record["route"] == "/api/login"
    assert record["status"] == 401
    assert record["outcome"] == "rejected"
    assert "wrong" not in caplog.text
    assert private_path not in caplog.text


def test_websocket_audits_connect_mutation_and_disconnect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _security_client()
    assert client.post("/api/login", json={"token": "secret"}).status_code == 200
    with caplog.at_level(logging.INFO, logger="autodj.audit"):
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "toggle_discovery"})
    records = [
        json.loads(item.message) for item in caplog.records if item.name == "autodj.audit"
    ]
    assert [record["outcome"] for record in records[-3:]] == [
        "connected", "success", "disconnected"
    ]
    assert records[-2]["action"] == "toggle_discovery"
    assert all(record["route"] == "/ws" for record in records[-3:])
```

Add `assert client.get("/api/status").status_code == 200` to the existing anonymous `client`
fixture regression in `tests/integration/test_server.py`. Add a cookie-tampering integration
assertion by logging in, overwriting `autodj_session` with `"tampered"`, and expecting 401 from
`/api/status`; expiry itself stays deterministic in Task 9's injected-clock unit test.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_server.py tests/integration/test_server_branches.py -k 'auth or origin or host or request_id or audit or websocket_security' -q`

Expected: FAIL because routes are currently anonymous and no middleware/login/audit layer exists.

- [ ] **Step 3: Add middleware and login/logout/status routes**

Add these imports and the complete middleware to `security.py`:

```python
import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.routing import Match
from starlette.types import ASGIApp

_AUDIT_LOGGER = logging.getLogger("autodj.audit")
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_PUBLIC_FILES = frozenset({
    "/", "/app.css", "/app.js", "/bitcrusher-worklet.js",
    "/stutter-worklet.js", "/freeze-worklet.js", "/glitch-worklet.js",
    "/api/version", "/api/auth/status", "/api/login",
})


def emit_audit(
    request_id: str,
    action: str,
    outcome: str,
    *,
    method: str | None = None,
    route: str | None = None,
    status: int | None = None,
    level: int = logging.INFO,
) -> None:
    _AUDIT_LOGGER.log(
        level,
        audit_record(request_id, action, outcome, method, route, status),
    )


def _is_public_path(path: str) -> bool:
    return path in _PUBLIC_FILES or path.startswith(("/static/", "/modules/"))


def _route_template(request: Request) -> str:
    for route in request.app.routes:
        match, _child_scope = route.matches(request.scope)
        if match is Match.FULL:
            return str(getattr(route, "path", "<mounted>"))
    return "<unmatched>"


class SecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, policy: SecurityPolicy) -> None:
        super().__init__(app)
        self._policy = policy

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request_id = new_request_id()
        request.state.request_id = request_id
        method = request.method.upper()
        route = _route_template(request)

        if not self._policy.host_allowed(request.headers.get("host")):
            response: Response = JSONResponse({"detail": "Disallowed Host"}, status_code=403)
        elif method in _UNSAFE_METHODS and not self._policy.origin_allowed(
            request.headers.get("origin")
        ):
            response = JSONResponse({"detail": "Disallowed Origin"}, status_code=403)
        elif (
            self._policy.authentication_required
            and request.url.path.startswith("/api/")
            and not _is_public_path(request.url.path)
            and not self._policy.verify_session(request.cookies.get(COOKIE_NAME))
        ):
            response = JSONResponse({"detail": "Authentication required"}, status_code=401)
        else:
            response = await call_next(request)

        response.headers["X-Request-ID"] = request_id
        if method in _UNSAFE_METHODS:
            outcome = "success" if response.status_code < 400 else "rejected"
            emit_audit(
                request_id,
                action=route,
                outcome=outcome,
                method=method,
                route=route,
                status=response.status_code,
            )
        return response
```

Add this import to `server.py`:

```python
from autodj.security import (
    COOKIE_NAME,
    SecurityMiddleware,
    SecurityPolicy,
    emit_audit,
    new_request_id,
)
```

Add models/routes to `server.py`:

```python
class LoginBody(BaseModel):
    token: str


@app.get("/api/auth/status")
async def api_auth_status(request: Request) -> dict[str, bool]:
    policy = request.app.state.security_policy
    return {
        "required": policy.authentication_required,
        "authenticated": (
            not policy.authentication_required
            or policy.verify_session(request.cookies.get(COOKIE_NAME))
        ),
    }


@app.post("/api/login")
async def api_login(body: LoginBody, request: Request) -> Response:
    policy = request.app.state.security_policy
    if not policy.verify_access_token(body.token):
        raise HTTPException(status_code=401, detail="Invalid access token")
    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        COOKIE_NAME, policy.issue_session(), httponly=True, samesite="strict",
        secure=policy.secure_cookie, max_age=policy.config.session_ttl_seconds, path="/",
    )
    return response


@app.post("/api/logout")
async def api_logout() -> Response:
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response
```

Change the existing factory signature to
`def create_app(bridge: PlayerBridge, *, secure_cookie: bool = False) -> FastAPI:`. Immediately
after the existing FastAPI `app` assignment, insert:

```python
policy = SecurityPolicy(bridge.player._cfg.server, secure_cookie=secure_cookie)
app.state.security_policy = policy
app.add_middleware(SecurityMiddleware, policy=policy)
```

In `server.serve`, replace its factory call with:

```python
app = create_app(
    bridge,
    secure_cookie=bool(ssl_certfile and ssl_keyfile),
)
```

Add `LoginBody` beside the existing Pydantic request models and register the three routes shown
above after `app` is created.

Replace the beginning and logging portions of `websocket_endpoint` with this complete route:

```python
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    request_id = new_request_id()
    route = "/ws"
    if not policy.host_allowed(websocket.headers.get("host")):
        emit_audit(request_id, route, "rejected", route=route, status=403,
                   level=logging.WARNING)
        await websocket.close(code=4403)
        return
    if not policy.origin_allowed(websocket.headers.get("origin")):
        emit_audit(request_id, route, "rejected", route=route, status=403,
                   level=logging.WARNING)
        await websocket.close(code=4403)
        return
    if policy.authentication_required and not policy.verify_session(
        websocket.cookies.get(COOKIE_NAME)
    ):
        emit_audit(request_id, route, "rejected", route=route, status=401,
                   level=logging.WARNING)
        await websocket.close(code=4401)
        return

    await websocket.accept()
    async with _ws_lock:
        _ws_clients.add(websocket)
        client_count = len(_ws_clients)
    emit_audit(request_id, route, "connected", route=route, status=101)
    try:
        while True:
            text = await websocket.receive_text()
            try:
                message = _json.loads(text)
                if isinstance(message, dict) and message.get("type") == "toggle_discovery":
                    bridge.toggle_discovery()
                    emit_audit(
                        request_id,
                        "toggle_discovery",
                        "success",
                        method="WS",
                        route=route,
                        status=200,
                    )
            except (_json.JSONDecodeError, AttributeError):
                continue
    except WebSocketDisconnect:
        return
    finally:
        async with _ws_lock:
            _ws_clients.discard(websocket)
        emit_audit(request_id, route, "disconnected", route=route, status=1000)
```

In `tests/integration/_helpers.py`, add the import and concrete test configuration:

```python
from autodj.config import ServerConfig

# In _make_player_mock, after cfg = MagicMock():
cfg.server = ServerConfig(
    allowed_hosts=["testserver"],
    allowed_origins=["http://testserver"],
)
```

In the `client` fixture in `tests/integration/conftest.py`, replace its return statement with:

```python
return TestClient(
    create_app(bridge),
    headers={"Host": "testserver", "Origin": "http://testserver"},
)
```

- [ ] **Step 4: Run server integration tests**

Run: `uv run pytest tests/integration/test_server.py tests/integration/test_server_branches.py tests/integration/test_server_recent.py -q`

Expected: all tests PASS; secured tests cover every required route category and WebSocket pre-accept rejection.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/security.py src/autodj/server.py tests/integration/conftest.py tests/integration/_helpers.py tests/integration/test_server.py tests/integration/test_server_branches.py
git commit -m "feat: enforce authenticated LAN requests"
```

### Task 11: Accessible browser login flow

**Files:**
- Create: `src/autodj/static/modules/auth.js`
- Create: `tests/jsmodules/auth.test.js`
- Modify: `src/autodj/static/index.html`
- Modify: `src/autodj/static/app.js:818-860,1303-1345`

- [ ] **Step 1: Write failing login module tests**

```javascript
// tests/jsmodules/auth.test.js
import { describe, expect, it, vi } from "vitest";
import {
  bootstrapAuthenticatedApp,
  initAuthDialog,
} from "../../src/autodj/static/modules/auth.js";

describe("initAuthDialog", () => {
  it("posts the token, clears it, and reloads after success", async () => {
    document.body.innerHTML = `<dialog id="auth-dialog"><form id="auth-form">
      <input id="auth-token"><p id="auth-error"></p><button>Log in</button></form></dialog>`;
    const dialog = document.querySelector("#auth-dialog");
    dialog.showModal = vi.fn();
    const fetchImpl = vi.fn().mockResolvedValue({ ok: true, status: 200 });
    const onSuccess = vi.fn();
    const auth = initAuthDialog({ document, fetchImpl, onSuccess });
    document.querySelector("#auth-token").value = "secret";
    await auth.submit();
    expect(fetchImpl).toHaveBeenCalledWith("/api/login", expect.objectContaining({ method: "POST" }));
    expect(document.querySelector("#auth-token").value).toBe("");
    expect(onSuccess).toHaveBeenCalledOnce();
  });

  it("shows an announced error and never stores the token", async () => {
    document.body.innerHTML = `<dialog id="auth-dialog"><form id="auth-form">
      <input id="auth-token"><p id="auth-error" role="alert"></p><button>Log in</button></form></dialog>`;
    const auth = initAuthDialog({ document, fetchImpl: vi.fn().mockResolvedValue({ ok: false, status: 401 }), onSuccess: vi.fn() });
    document.querySelector("#auth-token").value = "bad";
    await auth.submit();
    expect(document.querySelector("#auth-error").textContent).toContain("not accepted");
    expect(document.querySelector("#auth-token").value).toBe("");
  });
});

describe("bootstrapAuthenticatedApp", () => {
  it("starts no protected transport or module while login is required", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ required: true, authenticated: false }),
    });
    const auth = { show: vi.fn() };
    const startAuthenticatedApp = vi.fn();
    await bootstrapAuthenticatedApp({ fetchImpl, auth, startAuthenticatedApp });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(fetchImpl).toHaveBeenCalledWith("/api/auth/status");
    expect(auth.show).toHaveBeenCalledOnce();
    expect(startAuthenticatedApp).not.toHaveBeenCalled();
  });

  it("fetches state then starts authenticated modules exactly once", async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ required: true, authenticated: true }),
      })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ paused: false }) });
    const startAuthenticatedApp = vi.fn();
    await bootstrapAuthenticatedApp({
      fetchImpl,
      auth: { show: vi.fn() },
      startAuthenticatedApp,
    });
    expect(fetchImpl.mock.calls.map(call => call[0])).toEqual([
      "/api/auth/status", "/api/status",
    ]);
    expect(startAuthenticatedApp).toHaveBeenCalledOnce();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npm test -- --run tests/jsmodules/auth.test.js`

Expected: FAIL because the auth module does not exist.

- [ ] **Step 3: Add semantic login dialog and 401 bootstrap**

```javascript
// src/autodj/static/modules/auth.js
export function initAuthDialog({ document, fetchImpl = fetch, onSuccess = () => location.reload() }) {
  const dialog = document.querySelector("#auth-dialog");
  const form = document.querySelector("#auth-form");
  const token = document.querySelector("#auth-token");
  const error = document.querySelector("#auth-error");

  async function submit() {
    error.textContent = "";
    const candidate = token.value;
    token.value = "";
    let response;
    try {
      response = await fetchImpl("/api/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: candidate }),
      });
    } catch (_errorValue) {
      error.textContent = "Login failed. Check the server and try again.";
      token.focus();
      return false;
    }
    if (!response.ok) {
      error.textContent = response.status === 401
        ? "That access token was not accepted."
        : "Login failed. Check the server and try again.";
      token.focus();
      return false;
    }
    onSuccess();
    return true;
  }

  form.addEventListener("submit", event => { event.preventDefault(); void submit(); });
  return { show: () => { dialog.showModal(); token.focus(); }, submit };
}


export async function bootstrapAuthenticatedApp({
  fetchImpl = fetch,
  auth,
  startAuthenticatedApp,
  onError = () => {},
}) {
  try {
    const authResponse = await fetchImpl("/api/auth/status");
    if (!authResponse.ok) throw new Error(`/api/auth/status returned ${authResponse.status}`);
    const authState = await authResponse.json();
    if (authState.required && !authState.authenticated) {
      auth.show();
      return false;
    }
    const stateResponse = await fetchImpl("/api/status");
    if (stateResponse.status === 401) {
      auth.show();
      return false;
    }
    if (!stateResponse.ok) throw new Error(`/api/status returned ${stateResponse.status}`);
    startAuthenticatedApp(await stateResponse.json());
    return true;
  } catch (errorValue) {
    onError(errorValue);
    return false;
  }
}
```

Insert this dialog immediately after the opening `<body>` in `index.html`:

```html
<dialog id="auth-dialog" aria-labelledby="auth-title" aria-describedby="auth-help">
  <form id="auth-form" method="dialog">
    <h2 id="auth-title">Connect to AutoDJ</h2>
    <p id="auth-help">Enter the access token supplied by the server operator.</p>
    <label for="auth-token">Access token</label>
    <input id="auth-token" name="token" type="password"
           autocomplete="current-password" required>
    <p id="auth-error" role="alert" aria-live="assertive"></p>
    <button type="submit">Log in</button>
  </form>
</dialog>
```

Add this import near the other module imports in `app.js`:

```javascript
import { bootstrapAuthenticatedApp, initAuthDialog } from "./modules/auth.js";
```

Remove the module-scope `connectWS()`, `installLibraryJobs(_libEls)`, and `installLiners` calls.
Re-register them with the exact one-shot gate below; event-only wiring and the public
`/api/version` fetch may remain outside it:

```javascript
const auth = initAuthDialog({ document });
let authenticatedAppStarted = false;

function startAuthenticatedApp(initialState) {
  if (authenticatedAppStarted) return;
  authenticatedAppStarted = true;
  installLibraryJobs(_libEls);
  installLiners(_linerEls, {
    postSettings: (url, body) => postSettings(url, body),
    canPlay: () => !!_ctx && !!_lastBrowserPlayback,
    playLiner: async (arrayBuf, duckDb) => {
      if (!_ctx) return false;
      const audioBuf = await _ctx.decodeAudioData(arrayBuf);
      const src = _ctx.createBufferSource();
      src.buffer = audioBuf;
      const gain = _ctx.createGain();
      gain.gain.value = 1.0;
      src.connect(gain);
      gain.connect(_ctx.destination);
      const duckLin = Math.pow(10, duckDb / 20);
      const dur = audioBuf.duration;
      const t0 = _ctx.currentTime;
      const active = decks[activeIdx];
      active.gain.gain.cancelScheduledValues(t0);
      active.gain.gain.setValueAtTime(active.gain.gain.value, t0);
      active.gain.gain.linearRampToValueAtTime(_volume * duckLin, t0 + 0.2);
      active.gain.gain.setValueAtTime(_volume * duckLin, t0 + dur - 0.2);
      active.gain.gain.linearRampToValueAtTime(_volume, t0 + dur + 0.2);
      src.start(t0);
      return true;
    },
  });
  applyState(initialState);
  connectWS();
}

void bootstrapAuthenticatedApp({
  auth,
  startAuthenticatedApp,
  onError: errorValue => {
    setConnStatus("error", `Cannot reach server: ${errorValue.message}`);
    npAnnounce.textContent = `Cannot reach server: ${errorValue.message}`;
  },
});
```

The bootstrap test proves that no protected fetch beyond
`/api/auth/status`, WebSocket connection, liner poll/timer, or library stats load begins until the
cookie is authenticated; this prevents the current three-second WebSocket rejection loop. Do not
add token persistence: after login the module reloads the page, so same-origin fetch, audio
elements, and WebSocket use only the HttpOnly cookie.

- [ ] **Step 4: Run JS unit, lint, and build checks**

Run: `npm test -- --run tests/jsmodules/auth.test.js && npm run lint && npm run build`

Expected: auth tests PASS, ESLint exits 0, and Vite build exits 0.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/static/modules/auth.js src/autodj/static/index.html src/autodj/static/app.js tests/jsmodules/auth.test.js
git commit -m "feat: add accessible token login"
```

### Task 12: Documentation, security regression matrix, and final verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Run the completed acceptance matrix before documenting it**

Run:

```bash
uv run pytest \
  tests/unit/test_sqlite_utils.py \
  tests/unit/test_index_manifest.py \
  tests/unit/test_dj_meta.py \
  tests/unit/test_dj_cues.py \
  tests/unit/test_indexer.py \
  tests/unit/test_indexer_more.py \
  tests/unit/test_similarity.py \
  tests/unit/test_player.py \
  tests/unit/test_model.py \
  tests/unit/test_liner_files.py \
  tests/unit/test_security.py \
  tests/unit/test_config.py \
  tests/unit/test_cli.py \
  tests/unit/test_cli_more.py \
  tests/integration/test_server.py \
  tests/integration/test_server_branches.py \
  tests/integration/test_server_recent.py \
  tests/integration/test_index_pipeline.py \
  tests/smoke/test_cli_smoke.py -q
npm test -- --run tests/jsmodules/auth.test.js
```

Expected: all acceptance tests added in Tasks 1-11 PASS, including generation failure, last-valid
reload, loopback anonymous access, authenticated LAN access, refused unauthenticated LAN access,
and token-redaction assertions.

- [ ] **Step 2: Document exact deployment contracts**

Add README sections with these commands and meanings:

Anonymous loopback:

```bash
uv run autodj serve
```

Authenticated LAN: put the secret in ignored `config.toml`, never a CLI argument:

```toml
[server]
host = "0.0.0.0"
access_token = "generate-at-least-32-random-bytes"
allowed_hosts = ["radio.local"]
allowed_origins = ["https://radio.local:8080"]
```

```bash
uv run autodj serve --ssl-certfile radio.pem --ssl-keyfile radio-key.pem
```

Explicit trusted-LAN acknowledgement without authentication:

```bash
uv run autodj serve --host 0.0.0.0 --insecure-lan \
  --allowed-host radio.local --allowed-origin http://radio.local:8080
```

Explain: authenticated serving rejects access tokens shorter than 32 UTF-8 bytes. Generate a
random secret rather than copying the example value. `config.toml` and its local variants are
gitignored; secrets must not be supplied as CLI
arguments because shell history and process listings can expose them. Tokens are compared in
constant time and exchanged for an HttpOnly cookie; TLS is required to protect a token on the
wire; `--insecure-lan` disables authentication but not host/origin checks; manifests are the only
live-reload publication signal; incomplete model directories are ignored. State that the later
delivery plan builds on `ServerConfig` to add no-config defaults and a secret-safe environment
source, and owns Compose changes.
State explicitly that multi-user accounts, roles, cloud identity, and public-internet hosting remain
unsupported; this boundary is for a trusted private network.

- [ ] **Step 3: Run complete verification**

Run: `uv run pytest -q`

Expected: full Python suite PASS and configured coverage threshold met.

Run: `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/autodj/ && uv run bandit -r src/ -c pyproject.toml`

Expected: all commands exit 0.

Run: `npm test -- --run && npm run lint && npm run build`

Expected: Vitest PASS, ESLint exits 0, Vite build exits 0.

Run: `git status --short`

Expected: only intentional implementation/docs changes are present. The committed Claude-tooling
cleanup at `d245c9d` remains intact and must not be reverted or reintroduced.

- [ ] **Step 4: Commit documentation**

```bash
git add README.md
git commit -m "docs: describe secure server operation"
```

## Self-review checklist

- [ ] Storage: injected failures after destructive statements and mid-batch preserve old rows; DJ caches close deterministically.
- [ ] Incremental metadata: stable `vec_row`, delta UPSERT, no per-track full delete/reinsert.
- [ ] Publication: immutable generation files, SHA-256, WAL truncation, parent fsync, serialized writers, two-generation retention, and pointer-last publication; legacy startup remains supported.
- [ ] Reload: metadata-ahead, vectors-ahead, corrupt manifest, failed retry, and concurrent readers retain the last valid snapshot.
- [ ] Model cache: repo/revision identity, exact shard validation, supported Hub timeouts, staged tree fsync, cleanup, and atomic promotion.
- [ ] Liners: Windows/POSIX separators, ADS/trailing aliases, decoded slash catch-all, device names, 50 MiB default, 400/404/409/413, cleanup, explicit replace.
- [ ] Network: dynamically derived loopback policy, LAN refusal, 32-byte token minimum without secret echo, canonical HTTP(S) origins, token/insecure acknowledgement, bytewise constant-time checks, signed/expired cookies, Host/Origin, and WebSocket pre-accept checks.
- [ ] Observability: request IDs on responses and structured mutation/WebSocket audit records without tokens or full private paths.
- [ ] Boundary: this plan creates ordinary `ServerConfig`; later delivery extends it with no-config/environment overlay and Compose. No generic environment overlay or Compose edit is included here.
