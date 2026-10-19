"""Write and read catalog vectors directly, for tests that set up or check analysis results.

AutoDJ itself stores a vector only through analysis, which checks the file's
fingerprint first; these skip that.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from autodj.catalog import LibraryCatalog, _path
from autodj.indexer import FEATURE_DIM, IndexEntry


def set_vector(catalog: LibraryCatalog, path: str | Path, vector: np.ndarray) -> None:
    """Store *vector* for the catalog record at *path*, replacing any it had."""
    value = np.asarray(vector, dtype=np.float32)
    if value.shape != (FEATURE_DIM,) or not np.isfinite(value).all():
        raise ValueError(f"vector must be a finite float32 array of shape ({FEATURE_DIM},)")
    with catalog._lock:
        db = catalog._db()
        row = db.execute("SELECT id FROM catalog_entries WHERE path=?", (_path(path),)).fetchone()
        if row is None:
            raise KeyError(_path(path))
        db.execute(
            "INSERT INTO catalog_vectors VALUES (?,?) ON CONFLICT(entry_id) "
            "DO UPDATE SET vector=excluded.vector",
            (row["id"], value.tobytes()),
        )
        db.commit()


def vectors_snapshot(catalog: LibraryCatalog) -> tuple[list[IndexEntry], np.ndarray]:
    """The active music records that have a vector, and their vectors, in path order."""
    with catalog.reader() as reader:
        coverage = reader.coverage()
        by_path = {entry.path: entry for entry in reader.entries("music", coverage)}
        paths = [path for path in reader.vector_paths() if path in by_path]
        chunks = list(reader.vectors(set(paths)))
    vectors = np.concatenate(chunks) if chunks else np.empty((0, FEATURE_DIM), np.float32)
    return [by_path[path] for path in paths], vectors
