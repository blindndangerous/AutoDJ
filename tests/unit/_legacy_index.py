"""Write the old file index (tracks.sqlite, vectors.index, manifest) for tests.

AutoDJ no longer writes this format; it only reads it to upgrade an old
library (``autodj migrate-index``).  Tests build one here to check that.
"""

from __future__ import annotations

from pathlib import Path

import faiss
import numpy as np

from autodj.fsutil import relative_storage_path
from autodj.index_manifest import IndexManifest, publish_generation
from autodj.indexer import FEATURE_DIM, IndexEntry, _write_tracks_file, build_faiss_index


def entry_to_row(entry: IndexEntry, music_dir: Path | None, vec_row: int) -> dict[str, object]:
    """One ``tracks`` row, its path relative to *music_dir*."""
    return {
        "vec_row": vec_row,
        "path": relative_storage_path(entry.path, music_dir),
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
        "size": int(entry.size),
        "fingerprint": entry.fingerprint,
    }


def write_vectors(vectors: np.ndarray, path: Path) -> None:
    """Write *vectors* as a FAISS index file."""
    faiss.write_index(build_faiss_index(vectors), str(path))


def save_index(
    entries: list[IndexEntry],
    vectors: np.ndarray,
    index_dir: Path,
    music_dir: Path | None = None,
    *,
    base_generation: int,
) -> IndexManifest:
    """Publish *entries* and *vectors* as the next old-format index generation."""
    rows = [entry_to_row(entry, music_dir, vec_row) for vec_row, entry in enumerate(entries)]
    matrix = np.asarray(vectors, dtype=np.float32).reshape(len(entries), FEATURE_DIM)

    def write_files(tracks_path: Path, vectors_path: Path) -> None:
        _write_tracks_file(rows, tracks_path)
        write_vectors(matrix, vectors_path)

    return publish_generation(
        index_dir,
        base_generation=base_generation,
        vector_count=len(entries),
        write_files=write_files,
    )
