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
from autodj.similarity import SimilarityError, SimilarityIndex, _bpm_score

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unit_vec(dim: int = FEATURE_DIM, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _make_entry(i: int, bpm: float = 120.0) -> IndexEntry:
    return IndexEntry(
        path=f"Z:/Music/song_{i}.flac",
        title=f"Song {i}",
        artist=f"Artist {i % 3}",
        album="Album",
        genre="Rock",
        bpm=bpm,
        year=2000,
        length=180.0,
        energy=0.05,
        key=0,
        mode=1,
        tempo_confidence=0.8,
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
# find_next_for_path
# ---------------------------------------------------------------------------


class TestFindNextForPath:
    def test_returns_entry_for_known_path(self) -> None:
        sim, _ = _make_similarity_index(10)
        result = sim.find_next_for_path(
            current_path=sim.entries[0].path,
            recently_played=deque([sim.entries[0].path]),
        )
        assert isinstance(result, IndexEntry)
        assert result.path != sim.entries[0].path

    def test_raises_for_unknown_path(self) -> None:
        sim, _ = _make_similarity_index(5)
        with pytest.raises(SimilarityError, match="not in index"):
            sim.find_next_for_path(
                current_path="Z:/Music/unknown.flac",
                recently_played=deque(),
            )

    def test_result_consistent_with_find_next(self) -> None:
        """find_next_for_path and find_next with the reconstructed vector agree."""
        sim, _ = _make_similarity_index(20)
        path = sim.entries[3].path

        by_path = sim.find_next_for_path(path, recently_played=deque([path]))
        reconstructed = sim.faiss_index.reconstruct(3)
        by_vec = sim.find_next(reconstructed, recently_played=deque([path]))

        assert by_path.path == by_vec.path


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


class TestReloadFromDisk:
    def test_reload_picks_up_new_entries(self, tmp_path: Path) -> None:
        from autodj.indexer import save_index

        # Initial index with 3 tracks
        e1 = [_make_entry(i) for i in range(3)]
        v1 = np.array([_unit_vec(seed=i) for i in range(3)], dtype=np.float32)
        save_index(e1, v1, tmp_path)
        sim = SimilarityIndex.from_index_dir(tmp_path)
        assert sim.ntotal == 3

        # Concurrent indexer adds more — write a bigger snapshot
        e2 = [_make_entry(i) for i in range(7)]
        v2 = np.array([_unit_vec(seed=i) for i in range(7)], dtype=np.float32)
        save_index(e2, v2, tmp_path)

        new_total = sim.reload_from_disk(tmp_path)
        assert new_total == 7
        assert sim.ntotal == 7

    def test_reload_path_lookup_refreshed(self, tmp_path: Path) -> None:
        from autodj.indexer import save_index

        e1 = [_make_entry(0)]
        v1 = np.array([_unit_vec(seed=0)], dtype=np.float32)
        save_index(e1, v1, tmp_path)
        sim = SimilarityIndex.from_index_dir(tmp_path)
        # New entry that wasn't in the original index
        e2 = [_make_entry(0), _make_entry(99)]
        v2 = np.array([_unit_vec(seed=i) for i in (0, 99)], dtype=np.float32)
        save_index(e2, v2, tmp_path)
        sim.reload_from_disk(tmp_path)
        # _path_to_idx should now know about song_99
        assert "Z:/Music/song_99.flac" in sim._path_to_idx


# ---------------------------------------------------------------------------
# _bpm_score
# ---------------------------------------------------------------------------


class TestBpmScore:
    def test_perfect_match_returns_one(self) -> None:
        assert _bpm_score(120.0, 120.0) == pytest.approx(1.0)

    def test_unknown_bpm_returns_zero(self) -> None:
        assert _bpm_score(0.0, 120.0) == pytest.approx(0.0)

    def test_negative_bpm_returns_zero(self) -> None:
        assert _bpm_score(-1.0, 120.0) == pytest.approx(0.0)

    def test_distant_bpm_is_lower_than_close(self) -> None:
        close = _bpm_score(125.0, 120.0)
        distant = _bpm_score(160.0, 120.0)
        assert close > distant

    def test_score_in_range(self) -> None:
        for bpm in [80.0, 100.0, 120.0, 140.0, 180.0]:
            score = _bpm_score(bpm, 120.0)
            assert 0.0 <= score <= 1.0

    def test_sigma_affects_width(self) -> None:
        """A smaller sigma should penalise off-target BPM more harshly."""
        wide = _bpm_score(130.0, 120.0, sigma=30.0)
        narrow = _bpm_score(130.0, 120.0, sigma=5.0)
        assert wide > narrow


# ---------------------------------------------------------------------------
# BPM range filter
# ---------------------------------------------------------------------------


def _make_sim_with_bpms(bpms: list[float]) -> tuple[SimilarityIndex, np.ndarray]:
    """Build a SimilarityIndex where each entry has the given BPM."""
    vectors = np.array([_unit_vec(seed=i) for i in range(len(bpms))], dtype=np.float32)
    faiss_index = faiss.IndexFlatIP(FEATURE_DIM)
    faiss_index.add(vectors)
    entries = [_make_entry(i, bpm=bpm) for i, bpm in enumerate(bpms)]
    sim = SimilarityIndex(faiss_index=faiss_index, entries=entries)
    return sim, vectors


class TestBpmRangeFilter:
    def test_excludes_out_of_range_tracks(self) -> None:
        # Tracks: bpm=80 (out), 120 (in), 130 (in), 200 (out)
        bpms = [80.0, 120.0, 130.0, 200.0]
        sim, vectors = _make_sim_with_bpms(bpms)
        excluded = deque([sim.entries[0].path])  # exclude bpm=80 ourselves
        result = sim.find_next(
            query_vector=vectors[0],
            recently_played=excluded,
            bpm_range=(100.0, 150.0),
        )
        assert 100.0 <= result.bpm <= 150.0

    def test_unknown_bpm_passes_filter(self) -> None:
        # Tracks: bpm=200 (out of range), bpm=250 (out of range), bpm=0 (unknown — passes)
        bpms = [200.0, 250.0, 0.0]
        sim, vectors = _make_sim_with_bpms(bpms)
        # Exclude track 0 (the query track); remaining: track1(250) and track2(0.0)
        result = sim.find_next(
            query_vector=vectors[0],
            recently_played=deque([sim.entries[0].path]),
            bpm_range=(90.0, 130.0),
        )
        # Only track 2 (bpm=0.0) passes the filter; track 1 (bpm=250) is excluded by range
        assert result.bpm == 0.0

    def test_fallback_when_all_filtered(self) -> None:
        """When bpm_range excludes all non-excluded candidates, fall back to unfiltered."""
        # All known-BPM tracks are outside the range
        bpms = [200.0, 210.0, 220.0]
        sim, vectors = _make_sim_with_bpms(bpms)
        # This should not raise — fall back to unfiltered
        result = sim.find_next(
            query_vector=vectors[0],
            recently_played=deque([sim.entries[0].path]),
            bpm_range=(90.0, 130.0),
        )
        assert isinstance(result, IndexEntry)


# ---------------------------------------------------------------------------
# BPM re-ranking
# ---------------------------------------------------------------------------


class TestBpmReranking:
    def test_target_bpm_prefers_closer_bpm(self) -> None:
        """With a high bpm_weight, tracks closer to target_bpm rank higher."""
        # Two groups: bpm ~90 and bpm ~140; query targets 90
        bpms = [90.0, 91.0, 92.0, 140.0, 141.0, 142.0]
        sim, vectors = _make_sim_with_bpms(bpms)
        # Use the 140-bpm cluster as the "current" track to exclude
        excluded = deque([sim.entries[3].path, sim.entries[4].path, sim.entries[5].path])
        result = sim.find_next(
            query_vector=vectors[3],
            recently_played=excluded,
            n_candidates=10,
            target_bpm=90.0,
            bpm_weight=0.9,  # strong BPM preference
        )
        assert result.bpm < 100.0  # should pick a ~90 BPM track

    def test_no_reranking_when_target_bpm_none(self) -> None:
        """When target_bpm is None, result equals the nearest cosine neighbor."""
        sim, vectors = _make_similarity_index(10)
        excluded = deque([sim.entries[0].path])
        result = sim.find_next(
            query_vector=vectors[0],
            recently_played=excluded,
            n_candidates=10,
            target_bpm=None,
        )
        assert isinstance(result, IndexEntry)


# ---------------------------------------------------------------------------
# find_distant
# ---------------------------------------------------------------------------


class TestFindDistant:
    def test_returns_entry(self) -> None:
        sim, _ = _make_similarity_index(20)
        result = sim.find_distant(
            current_path=sim.entries[0].path,
            recently_played=deque([sim.entries[0].path]),
        )
        assert isinstance(result, IndexEntry)

    def test_does_not_return_current(self) -> None:
        sim, _ = _make_similarity_index(20)
        current_path = sim.entries[0].path
        result = sim.find_distant(
            current_path=current_path,
            recently_played=deque([current_path]),
        )
        assert result.path != current_path

    def test_does_not_return_recently_played(self) -> None:
        sim, _ = _make_similarity_index(20)
        # Exclude tracks 0-15, leaving only 16-19 available
        excluded = deque([sim.entries[i].path for i in range(16)])
        result = sim.find_distant(
            current_path=sim.entries[0].path,
            recently_played=excluded,
        )
        assert result.path not in set(excluded)

    def test_raises_for_unknown_path(self) -> None:
        sim, _ = _make_similarity_index(5)
        with pytest.raises(SimilarityError, match="not in index"):
            sim.find_distant(
                current_path="Z:/Music/unknown.flac",
                recently_played=deque(),
            )

    def test_raises_if_all_excluded(self) -> None:
        sim, _ = _make_similarity_index(5)
        all_excluded = deque([e.path for e in sim.entries])
        with pytest.raises(SimilarityError):
            sim.find_distant(
                current_path=sim.entries[0].path,
                recently_played=all_excluded,
            )
