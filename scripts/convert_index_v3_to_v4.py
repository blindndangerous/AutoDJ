"""One-off conversion of an AutoDJ index from schema 3 to schema 4.

Schema 4 stores a size and a content fingerprint per track, so `autodj index`
can tell a moved file from a new one.  AutoDJ itself refuses a schema 3 index;
this script is the only way to carry one over without re-embedding.  Vectors
are reused unchanged.

Usage (stop `autodj serve` and `autodj index` first)::

    uv run python scripts/convert_index_v3_to_v4.py \\
        --index-dir /path/to/index --music-dir /path/to/music [--map moves.json] [--apply]

Without ``--apply`` it only reports what it would do and writes nothing.

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
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import faiss
import numpy as np

from autodj.dj_meta import DjMetaCache
from autodj.fsutil import atomic_write, fsync_directory
from autodj.index_manifest import MANIFEST_NAME, publication_lock, publish_generation, sha256_file
from autodj.indexer import (
    _write_faiss_chunked,
    _write_tracks_file,
    build_faiss_index,
    file_fingerprint,
)

_PROGRESS_EVERY = 5000
_V3_COLUMNS = (
    "SELECT vec_row, path, title, artist, album, genre, bpm, year, length, energy, "
    "key, mode, tempo_confidence, embedded_at FROM tracks ORDER BY vec_row ASC"
)


def _read_v3(index_dir: Path) -> tuple[dict, list[dict], np.ndarray]:
    """Return the schema 3 manifest, its track rows and its vectors, checked."""
    manifest = json.loads((index_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 3:
        raise SystemExit(
            f"{index_dir} has schema {manifest.get('schema_version')!r}; this converts schema 3 only."
        )
    gen = f"g{manifest['generation']:020d}"
    tracks_path = index_dir / f"tracks.{gen}.db"
    vectors_path = index_dir / f"vectors.{gen}.index"
    for path, digest in (
        (tracks_path, manifest["tracks_sha256"]),
        (vectors_path, manifest["vectors_sha256"]),
    ):
        if not path.is_file() or sha256_file(path) != digest:
            raise SystemExit(f"{path} is missing or does not match the manifest.")
    conn = sqlite3.connect(f"file:{tracks_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(_V3_COLUMNS)]
    finally:
        conn.close()
    index = faiss.read_index(str(vectors_path))
    if len(rows) != manifest["vector_count"] or index.ntotal != len(rows):
        raise SystemExit("Track and vector counts do not match the manifest.")
    return manifest, rows, index.reconstruct_n(0, index.ntotal)


def _identify(paths: tuple[Path, Path]) -> tuple[int, str]:
    """Fingerprint the file at the new path, else at the old one."""
    for path in paths:
        size, fingerprint = file_fingerprint(path)
        if fingerprint:
            return size, fingerprint
    return 0, ""


def convert(index_dir: Path, music_dir: Path, moves: dict[str, str], apply: bool) -> int:
    """Convert the index; return a process exit code."""
    manifest, rows, vectors = _read_v3(index_dir)
    stored = {r["path"] for r in rows}
    unknown = [old for old in moves if old not in stored]
    if unknown:
        print(f"{len(unknown)} map entries name no indexed track, e.g. {unknown[0]}")
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
        f"{len(rows)} tracks, {len(moves) - len(unknown)} paths re-mapped, "
        f"{len(unreadable)} files unreadable (stored with size 0 and no fingerprint)."
    )
    for path in unreadable[:20]:
        print(f"  unreadable: {path}")
    if len(unreadable) > 20:
        print(f"  ... and {len(unreadable) - 20} more")
    if not apply:
        print("Dry run: nothing written.  Add --apply to convert.")
        return 0

    pairs = [(str(music_dir / old), str(music_dir / new)) for old, new in moves.items()]
    if pairs:
        # Idempotent, so a crash before publication is safe to re-run.
        with DjMetaCache(index_dir / "dj_meta.db", music_dir) as cache:
            print(f"Re-keyed {cache.rekey_many(pairs)} dj_meta rows.")

    def write_files(tracks_path: Path, vectors_path: Path) -> None:
        _write_tracks_file(rows, tracks_path)
        _write_faiss_chunked(build_faiss_index(vectors.astype(np.float32)), vectors_path)

    # publish_generation refuses an old live manifest, so stage a generation 1
    # in a scratch folder and install it as generation N+1 under the lock.
    with tempfile.TemporaryDirectory(dir=index_dir) as scratch:
        staged = publish_generation(
            Path(scratch), base_generation=0, vector_count=len(rows), write_files=write_files
        )
        with publication_lock(index_dir):
            live = json.loads((index_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
            if live["generation"] != manifest["generation"]:
                raise SystemExit("The index changed while converting; run this again.")
            final = dataclasses.replace(staged, generation=manifest["generation"] + 1)
            os.replace(Path(scratch) / staged.tracks_file, index_dir / final.tracks_file)
            os.replace(Path(scratch) / staged.vectors_file, index_dir / final.vectors_file)
            fsync_directory(index_dir)
            atomic_write(
                index_dir / MANIFEST_NAME, json.dumps(dataclasses.asdict(final), indent=2) + "\n"
            )
    print(f"Published schema 4 generation {final.generation}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the conversion."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--index-dir", type=Path, required=True)
    parser.add_argument("--music-dir", type=Path, required=True)
    parser.add_argument("--map", type=Path, help="JSON object of old to new relative paths")
    parser.add_argument("--apply", action="store_true", help="write; the default is a dry run")
    args = parser.parse_args(argv)
    moves: dict[str, str] = {}
    if args.map:
        moves = json.loads(args.map.read_text(encoding="utf-8"))
    return convert(args.index_dir, args.music_dir, moves, args.apply)


if __name__ == "__main__":
    sys.exit(main())
