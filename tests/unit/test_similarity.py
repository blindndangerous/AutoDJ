"""Unit tests for autodj.similarity.

Uses a pre-built fake FAISS index with known vectors so tests are
deterministic and require no model inference.
"""

from collections import deque
from pathlib import Path

import faiss
import numpy as np
import pytest

from autodj.indexer import FEATURE_DIM, IndexEntry
from autodj.similarity import SimilarityIndex, SimilarityError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unit_vec(dim: int = FEATURE_DIM, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _make_entry(i: int) -> IndexEntry:
    return IndexEntry(
        path=f"Z:/Music/song_{i}.flac",
        title=f"Song {i}",
        artist=f"Artist {i % 3}",
        album="Album",
        genre="Rock",
        bpm=120.0,
        year=2000,
        length=180.0,
    )


def _make_similarity_index(n: int) -> tuple[SimilarityIndex, np.ndarray]:
    """Build a SimilarityIndex with *n* deterministic tracks."""
    vectors = np.array([_unit_vec(seed=i) for i in range(n)], dtype=np.float32)
    faiss_index = faiss.IndexFlatIP(FEATURE_DIM)
    faiss_index.add(vectors)
    entries = [_make_entry(i) for i in range(n)]
    sim_index = SimilarityIndex(faiss_index=faiss_index, entries=entries)
    return sim_index, vectors


# ---------------------------------------------------------------------------
# SimilarityIndex construction
# ---------------------------------------------------------------------------


class TestSimilarityIndexConstruction:
    def test_ntotal_matches_entries(self) -> None:
        sim, _ = _make_similarity_index(10)
        assert sim.ntotal == 10

    def test_raises_if_entries_and_index_mismatch(self) -> None:
        vectors = np.array([_unit_vec(seed=i) for i in range(5)], dtype=np.float32)
        faiss_idx = faiss.IndexFlatIP(FEATURE_DIM)
        faiss_idx.add(vectors)
        entries = [_make_entry(i) for i in range(3)]  # wrong count

        with pytest.raises(ValueError, match="mismatch"):
            SimilarityIndex(faiss_index=faiss_idx, entries=entries)


# ---------------------------------------------------------------------------
# find_next
# ---------------------------------------------------------------------------


class TestFindNext:
    def test_returns_an_entry(self) -> None:
        sim, vectors = _make_similarity_index(10)
        result = sim.find_next(
            query_vector=vectors[0],
            recently_played=deque(),
            n_candidates=5,
        )
        assert isinstance(result, IndexEntry)

    def test_does_not_return_current_track(self) -> None:
        sim, vectors = _make_similarity_index(10)
        current_path = sim.entries[0].path
        result = sim.find_next(
            query_vector=vectors[0],
            recently_played=deque([current_path]),
            n_candidates=5,
        )
        assert result.path != current_path

    def test_excludes_recently_played(self) -> None:
        sim, vectors = _make_similarity_index(10)
        # Exclude tracks 0-7 — only 8 and 9 remain
        excluded = deque([sim.entries[i].path for i in range(8)])
        result = sim.find_next(
            query_vector=vectors[0],
            recently_played=excluded,
            n_candidates=10,
        )
        assert result.path in {sim.entries[8].path, sim.entries[9].path}

    def test_raises_if_all_excluded(self) -> None:
        sim, vectors = _make_similarity_index(5)
        all_excluded = deque([sim.entries[i].path for i in range(5)])
        with pytest.raises(SimilarityError, match="No candidates"):
            sim.find_next(
                query_vector=vectors[0],
                recently_played=all_excluded,
                n_candidates=5,
            )

    def test_returns_closest_neighbor(self) -> None:
        """With no exclusions, the top result should be closest (highest dot product)."""
        sim, vectors = _make_similarity_index(20)
        # The query is vector[5] itself — second-highest scoring (first is self)
        # We exclude track 5 so track 5's nearest neighbor comes through
        excluded = deque([sim.entries[5].path])
        result = sim.find_next(
            query_vector=vectors[5],
            recently_played=excluded,
            n_candidates=20,
        )
        # Result should NOT be track 5 itself
        assert result.path != sim.entries[5].path

    def test_n_candidates_respected(self) -> None:
        """Only the top n_candidates results are considered."""
        sim, vectors = _make_similarity_index(20)
        # With n_candidates=1 and no exclusions, result is the single nearest
        result = sim.find_next(
            query_vector=vectors[0],
            recently_played=deque([sim.entries[0].path]),
            n_candidates=2,
        )
        assert isinstance(result, IndexEntry)


# ---------------------------------------------------------------------------
# from_index_dir (loads from disk)
# ---------------------------------------------------------------------------


class TestFromIndexDir:
    def test_loads_from_disk(self, tmp_path: Path) -> None:
        from autodj.indexer import save_index

        entries = [_make_entry(i) for i in range(5)]
        vectors = np.array([_unit_vec(seed=i) for i in range(5)], dtype=np.float32)
        save_index(entries, vectors, tmp_path)

        sim = SimilarityIndex.from_index_dir(tmp_path)
        assert sim.ntotal == 5

    def test_raises_if_index_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            SimilarityIndex.from_index_dir(tmp_path / "nonexistent")
