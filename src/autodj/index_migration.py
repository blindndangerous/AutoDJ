"""One-off import of old AutoDJ indexes and legacy catalog-owned state.

Use ``autodj migrate-index`` to preview, then add ``--apply``. Published
schema-3 and schema-4 indexes store paths relative to the music root, so
``--music-dir`` is required to import one. Settings and play-history upgrades
in an existing catalog do not need a music root. Vectors are copied unchanged.

Usage (stop `autodj serve` and `autodj index` first)::

    uv run python scripts/convert_index_v3_to_v4.py \\
        --index-dir /path/to/index --music-dir /path/to/music [--map moves.json] [--apply]

Without ``--apply`` it only reports what it would do and writes nothing. Applying
retains a verified recovery folder beside the index before replacing any live data.

``--map`` is a JSON object of old path to new path, both relative to
``--music-dir`` with forward slashes, for a library move (for example
`beet move`) that has not happened yet or has just happened.  Stored paths
are rewritten to the new ones, and the same rows in ``dj_meta.db`` are
re-keyed.  Each file is read from its new path, or from its old path when the
move has not happened yet.

A track whose file cannot be read gets size 0 and fingerprint "" (never
matches a move) and is listed in the report.  The new generation is published
as generation N+1 and the schema 3 generation N stays until the next
publication removes it.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path, PurePosixPath

import faiss
import numpy as np

from autodj.backup import _check_sqlite
from autodj.dj_meta import DjMetaCache
from autodj.fsutil import atomic_write, fsync_directory, is_absolute_storage
from autodj.index_manifest import (
    MANIFEST_NAME,
    SCHEMA_VERSION,
    IndexConsistencyError,
    has_legacy_index,
    publication_lock,
    publish_generation,
    sha256_file,
)
from autodj.indexer import _write_tracks_file, file_fingerprint, load_index
from autodj.sqlite_utils import readonly_uri

_PROGRESS_EVERY = 5000
_V3_COLUMNS = (
    "SELECT vec_row, path, title, artist, album, genre, bpm, year, length, energy, "
    "key, mode, tempo_confidence, embedded_at FROM tracks ORDER BY vec_row ASC"
)


def _copy_database(source: Path, destination: Path) -> None:
    with (
        closing(sqlite3.connect(readonly_uri(source), uri=True)) as src,
        closing(sqlite3.connect(destination)) as dst,
    ):
        src.backup(dst)
    _check_sqlite(destination)


def _read_v3(index_dir: Path) -> tuple[dict, list[dict], Path]:
    """Return the schema 3 manifest, its track rows and its vectors, checked."""
    manifest = json.loads((index_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise IndexConsistencyError("The index manifest must be a JSON object.")
    if manifest.get("schema_version") != 3:
        raise IndexConsistencyError(
            f"{index_dir} has schema {manifest.get('schema_version')!r}; this converts schema 3 only."
        )
    if type(manifest.get("generation")) is not int or manifest["generation"] < 1:
        raise IndexConsistencyError("The schema 3 generation is invalid.")
    if type(manifest.get("vector_count")) is not int or manifest["vector_count"] < 0:
        raise IndexConsistencyError("The schema 3 vector count is invalid.")
    gen = f"g{manifest['generation']:020d}"
    tracks_path = index_dir / f"tracks.{gen}.db"
    vectors_path = index_dir / f"vectors.{gen}.index"
    for path, digest in (
        (tracks_path, manifest.get("tracks_sha256")),
        (vectors_path, manifest.get("vectors_sha256")),
    ):
        if not path.is_file() or sha256_file(path) != digest:
            raise IndexConsistencyError(f"{path} is missing or does not match the manifest.")
    conn = sqlite3.connect(readonly_uri(tracks_path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(_V3_COLUMNS)]
    finally:
        conn.close()
    index = faiss.read_index(str(vectors_path))
    if len(rows) != manifest["vector_count"] or index.ntotal != len(rows):
        raise IndexConsistencyError("Track and vector counts do not match the manifest.")
    if [r["vec_row"] for r in rows] != list(range(len(rows))):
        raise IndexConsistencyError("Track rows do not match vector positions.")
    return manifest, rows, vectors_path


def _validate_moves(rows: list[dict], moves: dict[str, str]) -> None:
    if not isinstance(moves, dict):
        raise ValueError("The move map must be a JSON object of old paths to new paths.")
    for path in [*(r["path"] for r in rows), *moves, *moves.values()]:
        if (
            not isinstance(path, str)
            or not path
            or is_absolute_storage(path)
            or "\\" in path
            or ".." in PurePosixPath(path).parts
            or PurePosixPath(path).as_posix() != path
            or path == "."
        ):
            raise ValueError("Track and map paths must be relative paths with forward slashes.")
    stored = {r["path"] for r in rows}
    if moves.keys() - stored:
        raise ValueError("The move map names a track that is not in this index.")
    destinations = [moves.get(r["path"], r["path"]) for r in rows]
    if len(set(destinations)) != len(destinations):
        raise ValueError("The move map sends more than one track to the same path.")
    if any(new in stored and old != new for old, new in moves.items()):
        raise ValueError("Move destinations must not be existing indexed paths.")


def _recovery_copy(index_dir: Path, manifest: dict) -> Path:
    """Retain every file conversion can change, plus station settings and history."""
    backup_root = index_dir.parent / ".migration-backups"
    backup_root.mkdir(exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix=f"{index_dir.name}.schema3-backup-", dir=backup_root))
    print(f"Saving migration recovery copy to {backup}", flush=True)
    gen = f"g{manifest['generation']:020d}"
    for name, digest in (
        (f"tracks.{gen}.db", manifest["tracks_sha256"]),
        (f"vectors.{gen}.index", manifest["vectors_sha256"]),
    ):
        shutil.copyfile(index_dir / name, backup / name)
        if sha256_file(backup / name) != digest:
            raise IndexConsistencyError(f"Recovery copy verification failed: {name}")
    for name in ("dj_meta.db", "library.db"):
        if (index_dir / name).is_file():
            _copy_database(index_dir / name, backup / name)
    if (index_dir / "web_state.json").is_file():
        shutil.copyfile(index_dir / "web_state.json", backup / "web_state.json")
    atomic_write(backup / MANIFEST_NAME, json.dumps(manifest, indent=2) + "\n")
    return backup


def _web_state_paths(index_dir: Path, configured: Iterable[Path] = ()) -> tuple[Path, ...]:
    """Return default and configured station state files without duplicates."""
    return tuple(dict.fromkeys((index_dir / "web_state.json", *(Path(p) for p in configured))))


def _recovery_catalog_copy(index_dir: Path, web_states: Iterable[Path] = ()) -> Path:
    """Keep a recoverable copy of catalog-owned files before any data upgrade."""
    backup_root = index_dir.parent / ".migration-backups"
    backup_root.mkdir(exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix=f"{index_dir.name}.catalog-backup-", dir=backup_root))
    for name in ("dj_meta.db", "library.db"):
        if (index_dir / name).is_file():
            _copy_database(index_dir / name, backup / name)
    for name in ("web_state.json", "index-manifest.json"):
        if (index_dir / name).is_file():
            shutil.copyfile(index_dir / name, backup / name)
    try:
        manifest = json.loads((index_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        manifest = None
    if (
        isinstance(manifest, dict)
        and type(manifest.get("generation")) is int
        and manifest["generation"] > 0
        and manifest.get("schema_version") in (3, 4)
    ):
        gen = f"g{manifest['generation']:020d}"
        for name, digest in (
            (f"tracks.{gen}.db", manifest.get("tracks_sha256")),
            (f"vectors.{gen}.index", manifest.get("vectors_sha256")),
        ):
            source = index_dir / name
            if source.is_file() and isinstance(digest, str) and sha256_file(source) == digest:
                shutil.copyfile(source, backup / name)
                if sha256_file(backup / name) != digest:
                    raise IndexConsistencyError(f"Recovery copy verification failed: {name}")
    station_states = [
        Path(path) for path in web_states if Path(path) != index_dir / "web_state.json"
    ]
    station_states = [path for path in station_states if path.is_file()]
    if station_states:
        state_dir = backup / "station-web-state"
        state_dir.mkdir()
        mapping = []
        for position, path in enumerate(station_states):
            name = f"state-{position}.json"
            shutil.copyfile(path, state_dir / name)
            mapping.append({"source": str(path), "recovery_file": f"station-web-state/{name}"})
        atomic_write(backup / "station-web-state.json", json.dumps(mapping, indent=2) + "\n")
    print(f"Saving migration recovery copy to {backup}", flush=True)
    return backup


def _old_catalog_data(index_dir: Path, *, immutable: bool = False) -> tuple[bool, bool]:
    """Read whether library.db has old JSON history or unseeded folder roots."""
    path = index_dir / "library.db"
    if not path.is_file():
        return False, True
    with closing(sqlite3.connect(readonly_uri(path, immutable=immutable), uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "catalog_settings" not in tables:
            return False, True
        settings = dict(db.execute("SELECT key,value FROM catalog_settings"))
        has_history = "play_history" in settings and "play_history_migrated" not in settings
        if (
            "catalog_sources" in tables
            and db.execute("SELECT 1 FROM catalog_sources LIMIT 1").fetchone()
        ):
            sources_needed = False
        else:
            sources_needed = settings.get("sources_seeded") not in ("true", "1")
    return has_history, sources_needed


def _validate_legacy_history(index_dir: Path) -> None:
    """Refuse to start a conversion that would silently drop legacy history rows."""
    path = index_dir / "library.db"
    if not path.is_file():
        return
    with closing(sqlite3.connect(readonly_uri(path), uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "catalog_settings" not in tables:
            return
        settings = dict(db.execute("SELECT key,value FROM catalog_settings"))
    if "play_history" not in settings or "play_history_migrated" in settings:
        return
    try:
        rows = json.loads(settings["play_history"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("legacy play-history JSON is invalid; no files were changed") from exc
    if not isinstance(rows, list):
        raise ValueError("legacy play-history must be a JSON list; no files were changed")
    from autodj.catalog import _legacy_history_duration

    invalid = sum(_legacy_history_duration(row) is None for row in rows)
    if invalid:
        raise ValueError(f"legacy play-history has {invalid} invalid row(s); no files were changed")


def _has_pending_import(index_dir: Path, *, immutable: bool = False) -> bool:
    """Whether this command previously started, but did not finish, an import."""
    path = index_dir / "library.db"
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(readonly_uri(path, immutable=immutable), uri=True)) as db:
            row = db.execute(
                "SELECT value FROM catalog_settings WHERE key='published_index_import_state'"
            ).fetchone()
        state = json.loads(row[0]) if row else None
    except (OSError, sqlite3.Error, TypeError, json.JSONDecodeError):
        return False
    return isinstance(state, dict) and state.get("status") == "pending"


def _has_current_catalog(index_dir: Path, *, immutable: bool = False) -> bool:
    """Whether the path already holds the current library schema."""
    path = index_dir / "library.db"
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(readonly_uri(path, immutable=immutable), uri=True)) as db:
            return (
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_entries'"
                ).fetchone()
                is not None
            )
    except (OSError, sqlite3.Error):
        return False


def _catalog_migration_state(index_dir: Path) -> tuple[bool, bool, bool, bool]:
    """Read in place; only an orphaned WAL needs private sidecar reconstruction."""
    catalog_path = index_dir / "library.db"
    if not catalog_path.is_file():
        return False, False, False, True

    def read(directory: Path, *, immutable: bool) -> tuple[bool, bool, bool, bool]:
        with closing(
            sqlite3.connect(readonly_uri(directory / "library.db", immutable=immutable), uri=True)
        ) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
        # Current catalogs use SQLite's default (unversioned) schema marker.
        if version > 0:
            raise IndexConsistencyError(
                f"The catalog in {index_dir} has newer schema {version}; "
                "use the AutoDJ version that wrote it or restore a compatible backup."
            )
        return (
            _has_current_catalog(directory, immutable=immutable),
            _has_pending_import(directory, immutable=immutable),
            *_old_catalog_data(directory, immutable=immutable),
        )

    wal = index_dir / "library.db-wal"
    if not wal.exists():
        # No WAL to ignore: immutable mode avoids creating empty -wal/-shm files.
        return read(index_dir, immutable=True)
    if (index_dir / "library.db-shm").is_file():
        return read(index_dir, immutable=False)
    # SQLite must reconstruct missing shared memory to read an orphaned WAL.
    # Reconstruct privately; immutable mode would miss committed WAL changes.
    with tempfile.TemporaryDirectory(prefix="autodj-catalog-check-") as temporary:
        snapshot = Path(temporary)
        shutil.copyfile(catalog_path, snapshot / "library.db")
        shutil.copyfile(wal, snapshot / wal.name)
        return read(snapshot, immutable=False)


def migration_required(
    index_dir: Path, station_web_states: Iterable[Path] = ()
) -> tuple[bool, str]:
    """Return whether supported legacy application data needs explicit import."""
    index_dir = Path(index_dir)
    manifest_path = index_dir / MANIFEST_NAME
    catalog_path = index_dir / "library.db"
    current_catalog, pending_import, history, sources = _catalog_migration_state(index_dir)
    schema = None
    if (not current_catalog or pending_import) and manifest_path.is_file():
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            if not catalog_path.exists() and isinstance(exc, json.JSONDecodeError):
                # Startup only identifies legacy data; migration validates its contents.
                return True, "an older published index is present with an unreadable manifest"
            raise IndexConsistencyError(
                f"cannot read {manifest_path}: {exc}; inspect with `autodj migrate-index` "
                "or restore a compatible backup before starting AutoDJ."
            ) from exc
        if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int:
            raise IndexConsistencyError(f"{manifest_path} has no valid schema_version")
        schema = raw["schema_version"]
        if schema > SCHEMA_VERSION:
            raise IndexConsistencyError(
                f"The library in {index_dir} has newer schema {schema}; "
                "use the AutoDJ version that wrote it or restore a compatible backup."
            )
    from autodj.runtime_state import migrate_state_file

    settings = False
    for path in _web_state_paths(index_dir, station_web_states):
        try:
            settings = migrate_state_file(path, apply=False) or settings
        except ValueError:
            # Unsupported or corrupt settings are diagnosed by the settings loader;
            # this command only claims files it knows how to upgrade safely.
            continue
    needs_catalog_import = schema in (3, 4) and not current_catalog
    if current_catalog:
        needs_catalog_import = pending_import
    reasons = []
    if not catalog_path.is_file() and has_legacy_index(index_dir) and not needs_catalog_import:
        reasons.append("an older published index is present")
    if needs_catalog_import:
        if pending_import:
            reasons.append("published-index import was interrupted")
        elif schema in (3, 4):
            reasons.append(f"schema-{schema} published index has not been imported into library.db")
    if settings:
        reasons.append("web_state.json schema 1")
    if history:
        reasons.append("legacy play-history JSON")
    if sources and (index_dir / "library.db").is_file():
        reasons.append("initial folder registration is incomplete")
    return bool(reasons), "; ".join(reasons)


def _upgrade_settings_and_sources(
    index_dir: Path,
    music_dir: Path | None,
    liner_dir: Path,
    *,
    apply: bool,
    station_web_states: Iterable[Path] = (),
) -> tuple[bool, bool, bool]:
    """Preview or apply the web-state, play-history, and initial-root upgrades."""
    from autodj.runtime_state import migrate_state_file

    settings_changed = False
    for settings_path in _web_state_paths(index_dir, station_web_states):
        settings_changed = migrate_state_file(settings_path, apply=apply) or settings_changed
    history_changed, sources_changed = _old_catalog_data(index_dir)
    if apply:
        from autodj.catalog import LibraryCatalog

        with LibraryCatalog(index_dir / "library.db") as catalog:
            catalog.migrate_legacy_history()
            if sources_changed:
                sources_changed = catalog.seed_sources(music_dir, liner_dir) or sources_changed
    return settings_changed, history_changed, sources_changed


def _import_published_generation(index_dir: Path, music_dir: Path) -> tuple[int, int]:
    """Import missing published records/vectors into library.db without analysis."""
    from autodj.catalog import LibraryCatalog

    entries, vectors, manifest = load_index(index_dir, music_dir=music_dir)
    catalog_path = index_dir / "library.db"
    with LibraryCatalog(catalog_path) as catalog:
        signature = {
            "generation": manifest.generation,
            "tracks_sha256": manifest.tracks_sha256,
            "vectors_sha256": manifest.vectors_sha256,
        }
        catalog.set_setting("published_index_import_state", {**signature, "status": "pending"})
        # This insertion is idempotent: existing rows and vectors are not replaced.
        catalog.seed_entries(entries)
        have = catalog.paths_with_vectors()
        imported = 0
        for start in range(0, len(entries), 1024):
            stop = min(len(entries), start + 1024)
            wanted = [
                i for i in range(start, stop) if os.path.normcase(entries[i].path) not in have
            ]
            if not wanted:
                continue
            matrix = np.asarray(vectors.reconstruct_n(start, stop - start), dtype=np.float32)
            selected = [entries[i] for i in wanted]
            selected_vectors = matrix[[i - start for i in wanted]]
            imported += catalog.seed_vectors(selected, selected_vectors)

        # Older indexes kept intro/outro suggestions in dj_meta.db. Keep the
        # cache intact and import these points as analysis suggestions only.
        if (index_dir / "dj_meta.db").is_file():
            from autodj.dj_meta import DjMetaCache

            with DjMetaCache(index_dir / "dj_meta.db", music_dir) as cache:
                for entry in entries:
                    try:
                        meta = cache.get(entry.path)
                    except ValueError:
                        continue
                    points = {
                        name: value
                        for name, value in (
                            ("intro", meta.intro_end_s),
                            ("outro", meta.outro_start_s),
                        )
                        if value is not None and value > 0
                    }
                    if points:
                        try:
                            catalog.set_points(entry.path, points, source="analysis")
                        except ValueError as exc:
                            print(f"Skipped analysis points for {entry.path}: {exc}")
        catalog.set_setting("imported_index_generation", signature)
        catalog.set_setting("published_index_import_state", {**signature, "status": "complete"})
    return len(entries), imported


def _mark_import_pending(index_dir: Path, music_dir: Path) -> None:
    """Mark the catalog before any multi-step conversion can be interrupted."""
    from autodj.catalog import LibraryCatalog

    _, _, manifest = load_index(index_dir, music_dir=music_dir)
    signature = {
        "generation": manifest.generation,
        "tracks_sha256": manifest.tracks_sha256,
        "vectors_sha256": manifest.vectors_sha256,
    }
    with LibraryCatalog(index_dir / "library.db") as catalog:
        catalog.set_setting("published_index_import_state", {**signature, "status": "pending"})


def _migrate_catalog(
    index_dir: Path,
    music_dir: Path | None,
    liner_dir: Path,
    moves: dict[str, str],
    *,
    apply: bool,
    station_web_states: Iterable[Path] = (),
) -> int:
    """Upgrade supported published indexes and catalog-owned legacy data.

    Schema 3 is first published as schema 4 with the existing checked
    converter. Schema 4 vectors are then imported into the current catalog.
    All writes are opt-in and repeatable; seed operations never replace
    current catalog records or vectors.
    """
    index_dir = index_dir.resolve()
    music_dir = music_dir.resolve() if music_dir is not None else None
    liner_dir = liner_dir.resolve()
    catalog_exists = _has_current_catalog(index_dir)
    pending_import = _has_pending_import(index_dir)
    manifest_path = index_dir / MANIFEST_NAME
    schema: int | None = None
    if (not catalog_exists or pending_import) and manifest_path.is_file():
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise IndexConsistencyError(f"cannot read {manifest_path}: {exc}") from exc
        if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int:
            raise IndexConsistencyError(f"{manifest_path} has no valid schema_version")
        schema = raw["schema_version"]
        if schema not in (3, 4):
            raise IndexConsistencyError(
                f"unsupported index schema {schema}; supported schemas are 3 and 4"
            )
        if moves and schema != 3:
            raise ValueError("--map is only supported when converting a schema-3 index")

    import_published = schema in (3, 4) and (not catalog_exists or pending_import)
    if import_published and music_dir is None:
        raise ValueError("--music-dir is required to import a published schema-3 or schema-4 index")
    history_pending, _ = _old_catalog_data(index_dir)
    if history_pending:
        _validate_legacy_history(index_dir)
    if not import_published:
        schema = None  # Current catalogs ignore leftover published generations.

    if schema == 3:
        assert music_dir is not None
        convert(index_dir, music_dir, moves, apply)
        if not apply:
            settings, history, sources = _upgrade_settings_and_sources(
                index_dir,
                music_dir,
                liner_dir,
                apply=False,
                station_web_states=station_web_states,
            )
            print(
                "Would import the converted vectors and tracks into library.db without reanalysis; "
                f"legacy data: web settings={settings}, play history={history}, folders={sources}."
            )
            return 0
        schema = 4

    if schema == 4:
        assert music_dir is not None
        # Validate the complete generation and map before changing catalog state.
        entries, _, _ = load_index(index_dir, music_dir=music_dir)
        history, sources = _old_catalog_data(index_dir)
        from autodj.runtime_state import migrate_state_file

        settings = False
        for path in _web_state_paths(index_dir, station_web_states):
            settings = migrate_state_file(path, apply=False) or settings
        print(
            f"{len(entries)} tracks; will import published vectors into library.db without "
            f"reanalysis. Legacy data: web settings={settings}, play history={history}, folders={sources}."
        )
        if not apply:
            print("Dry run: nothing written. Add --apply to complete the catalog migration.")
            return 0

    if not apply:
        settings, history, sources = _upgrade_settings_and_sources(
            index_dir,
            music_dir,
            liner_dir,
            apply=False,
            station_web_states=station_web_states,
        )
        if schema is None:
            print(
                "No published index; catalog data only. "
                f"Legacy data: web settings={settings}, play history={history}, folders={sources}."
            )
            print("Dry run: nothing written. Add --apply to upgrade.")
        return 0

    _recovery_catalog_copy(index_dir, station_web_states)
    if schema == 4:
        assert music_dir is not None
        _mark_import_pending(index_dir, music_dir)
    settings, history, sources = _upgrade_settings_and_sources(
        index_dir,
        music_dir,
        liner_dir,
        apply=True,
        station_web_states=station_web_states,
    )
    if schema == 4:
        assert music_dir is not None
        count, vectors = _import_published_generation(index_dir, music_dir)
        print(
            f"Imported {count} tracks and {vectors} vectors into library.db; no analysis was run."
        )
    print(
        f"Upgraded legacy data: web settings={settings}, play history={history}, folders={sources}."
    )
    print(
        "Recovery copy retained. It is safe to rerun migrate-index after an interrupted migration."
    )
    return 0


def migrate_catalog(
    index_dir: Path,
    music_dir: Path | None,
    liner_dir: Path,
    moves: dict[str, str],
    *,
    apply: bool,
    station_web_states: Iterable[Path] = (),
) -> int:
    """Preview freely; hold the serve lock for every migration write."""
    if not apply:
        return _migrate_catalog(
            index_dir,
            music_dir,
            liner_dir,
            moves,
            apply=False,
            station_web_states=station_web_states,
        )
    from autodj.backup import hold_serve_lock

    index_dir = Path(index_dir).resolve()
    with hold_serve_lock(index_dir):
        return _migrate_catalog(
            index_dir,
            music_dir,
            liner_dir,
            moves,
            apply=True,
            station_web_states=station_web_states,
        )


def _identify(paths: tuple[Path, Path]) -> tuple[int, str]:
    """Fingerprint the file at the new path, else at the old one."""
    for path in paths:
        size, fingerprint = file_fingerprint(path)
        if fingerprint:
            return size, fingerprint
    return 0, ""


def convert(index_dir: Path, music_dir: Path, moves: dict[str, str], apply: bool) -> int:
    """Convert the index; return a process exit code."""
    index_dir = index_dir.resolve()
    music_dir = music_dir.resolve()
    manifest, rows, vectors_path = _read_v3(index_dir)
    _validate_moves(rows, moves)
    jobs = [(music_dir / moves.get(r["path"], r["path"]), music_dir / r["path"]) for r in rows]
    print(f"Fingerprinting {len(rows)} tracks under {music_dir} ...", flush=True)
    results: list[tuple[int, str]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for result in pool.map(_identify, jobs):
            results.append(result)
            if len(results) % _PROGRESS_EVERY == 0:
                print(f"  {len(results)} of {len(rows)}", flush=True)
    unreadable = [r["path"] for r, (_, fp) in zip(rows, results, strict=True) if not fp]
    for row, (size, fingerprint) in zip(rows, results, strict=True):
        row["path"] = moves.get(row["path"], row["path"])
        row["size"] = size
        row["fingerprint"] = fingerprint
    print(
        f"{len(rows)} tracks, {len(moves)} paths re-mapped, "
        f"{len(unreadable)} files unreadable (stored with size 0 and no fingerprint)."
    )
    for path in unreadable[:20]:
        print(f"  unreadable: {path}")
    if len(unreadable) > 20:
        print(f"  ... and {len(unreadable) - 20} more")
    if not apply:
        print("Dry run: nothing written.  Add --apply to convert.")
        return 0

    def write_files(tracks_path: Path, vectors_path: Path) -> None:
        _write_tracks_file(rows, tracks_path)
        shutil.copyfile(original_vectors, vectors_path)

    original_vectors = vectors_path
    # publish_generation refuses an old live manifest, so stage a generation 1
    # in a scratch folder and install it as generation N+1 under the lock.
    with tempfile.TemporaryDirectory(dir=index_dir) as scratch:
        staged = publish_generation(
            Path(scratch), base_generation=0, vector_count=len(rows), write_files=write_files
        )
        load_index(Path(scratch), music_dir=music_dir)
        with publication_lock(index_dir):
            live = json.loads((index_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
            if live != manifest:
                raise IndexConsistencyError("The index changed while converting; run this again.")
            backup = _recovery_copy(index_dir, manifest)
            pairs = [(str(music_dir / old), str(music_dir / new)) for old, new in moves.items()]
            staged_cues = Path(scratch) / "dj_meta.db"
            if pairs and (backup / "dj_meta.db").is_file():
                shutil.copyfile(backup / "dj_meta.db", staged_cues)
                with DjMetaCache(staged_cues, music_dir) as cache:
                    print(f"Re-keyed {cache.rekey_many(pairs)} dj_meta rows.")
            final = dataclasses.replace(staged, generation=manifest["generation"] + 1)
            os.replace(Path(scratch) / staged.tracks_file, index_dir / final.tracks_file)
            os.replace(Path(scratch) / staged.vectors_file, index_dir / final.vectors_file)
            fsync_directory(index_dir)
            try:
                if staged_cues.is_file():
                    _copy_database(staged_cues, index_dir / "dj_meta.db")
                atomic_write(
                    index_dir / MANIFEST_NAME,
                    json.dumps(dataclasses.asdict(final), indent=2) + "\n",
                )
            except BaseException:
                # A failed directory flush can happen after the manifest was replaced.
                published = json.loads((index_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
                if published == manifest and pairs and (backup / "dj_meta.db").is_file():
                    _copy_database(backup / "dj_meta.db", index_dir / "dj_meta.db")
                raise
    print(f"Published schema 4 generation {final.generation}.")
    print(f"Recovery copy retained at {backup}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the conversion."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--index-dir", type=Path, required=True)
    parser.add_argument("--music-dir", type=Path, required=True)
    parser.add_argument("--map", type=Path, help="JSON object of old to new relative paths")
    parser.add_argument("--apply", action="store_true", help="write; the default is a dry run")
    args = parser.parse_args(argv)
    try:
        moves = json.loads(args.map.read_text(encoding="utf-8")) if args.map else {}
        return convert(args.index_dir, args.music_dir, moves, args.apply)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        parser.exit(1, f"Conversion failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
