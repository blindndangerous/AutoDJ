"""One-off conversion of an AutoDJ index from schema 3 to schema 4.

Schema 4 stores a size and a content fingerprint per track so moved files can
keep their analysis. Use ``autodj migrate-index`` to preview, then add ``--apply``.
Interactive ``autodj serve`` also offers this conversion. Vectors are copied unchanged.

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
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path, PurePosixPath

import faiss

from autodj.backup import _check_sqlite
from autodj.dj_meta import DjMetaCache
from autodj.fsutil import atomic_write, fsync_directory
from autodj.index_manifest import (
    MANIFEST_NAME,
    IndexConsistencyError,
    is_absolute_storage,
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
