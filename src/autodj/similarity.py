"""FAISS-based next-song similarity engine.

Loads the pre-built FAISS index and provides :class:`SimilarityIndex`, which
wraps the index with recently-played exclusion and candidate ranking logic.

Cosine similarity is computed via inner product on L2-normalized vectors
(``IndexFlatIP``), so higher scores mean more similar tracks.

Example:
    >>> from autodj.similarity import SimilarityIndex
    >>> from collections import deque
    >>> sim = SimilarityIndex.from_index_dir(Path("index"))
    >>> next_track = sim.find_next(query_vec, recently_played=deque(), n_candidates=10)
    >>> print(next_track.display_name)
    Portishead — Mysterons
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import faiss
import numpy as np

from autodj.indexer import FEATURE_DIM, IndexEntry, load_index

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SimilarityError(RuntimeError):
    """Raised when a next-song candidate cannot be found."""


# ---------------------------------------------------------------------------
# SimilarityIndex
# ---------------------------------------------------------------------------


@dataclass
class SimilarityIndex:
    """Wraps a FAISS index with metadata and next-song selection logic.

    Attributes:
        faiss_index: The loaded FAISS ``IndexFlatIP``.
        entries: Ordered list of :class:`~autodj.indexer.IndexEntry` objects
            whose row positions correspond to FAISS vector positions.
    """

    faiss_index: faiss.IndexFlatIP
    entries: list[IndexEntry]

    def __post_init__(self) -> None:
        """Validate that the FAISS index and entries list are consistent."""
        if self.faiss_index.ntotal != len(self.entries):
            raise ValueError(
                f"Index/metadata mismatch: FAISS has {self.faiss_index.ntotal} vectors "
                f"but metadata has {len(self.entries)} entries."
            )

    @property
    def ntotal(self) -> int:
        """Total number of tracks in the index.

        Returns:
            Integer count of indexed tracks.
        """
        return len(self.entries)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_index_dir(cls, index_dir: Path) -> "SimilarityIndex":
        """Load a :class:`SimilarityIndex` from the index directory on disk.

        Args:
            index_dir: Directory containing ``vectors.index`` and
                ``metadata.json`` as written by
                :func:`autodj.indexer.save_index`.

        Returns:
            A fully populated :class:`SimilarityIndex`.

        Raises:
            FileNotFoundError: If *index_dir* or its files are missing.

        Example:
            >>> sim = SimilarityIndex.from_index_dir(Path("index"))
        """
        entries, faiss_index = load_index(index_dir)
        return cls(faiss_index=faiss_index, entries=entries)

    # ------------------------------------------------------------------
    # Core query
    # ------------------------------------------------------------------

    def find_next(
        self,
        query_vector: np.ndarray,
        recently_played: deque[str],
        n_candidates: int = 10,
    ) -> IndexEntry:
        """Find the best next track that isn't in *recently_played*.

        Queries FAISS for the top ``n_candidates + len(recently_played)``
        nearest neighbors (by cosine similarity), then filters out any track
        whose path appears in *recently_played*, and returns the highest-ranked
        remaining candidate.

        Args:
            query_vector: L2-normalized float32 array of shape
                ``(FEATURE_DIM,)`` representing the current track.
            recently_played: Deque of file path strings (as stored in
                :attr:`IndexEntry.path`) to exclude from results.
            n_candidates: Minimum number of candidate neighbors to retrieve
                before filtering.  Actual search fetches
                ``n_candidates + len(recently_played)`` to leave room after
                exclusions.

        Returns:
            The :class:`IndexEntry` for the recommended next track.

        Raises:
            SimilarityError: If all retrieved candidates were excluded by
                *recently_played* (library too small or window too large).

        Example:
            >>> next_track = sim.find_next(vec, recently_played=deque(["Z:/Music/a.flac"]))
            >>> print(next_track.title)
            Sour Times
        """
        excluded = set(recently_played)
        # Over-fetch so we have candidates left after filtering
        k = min(n_candidates + len(excluded) + 1, self.ntotal)

        query = query_vector.reshape(1, -1).astype(np.float32)
        _, indices = self.faiss_index.search(query, k)

        for idx in indices[0]:
            if idx < 0:
                continue  # FAISS returns -1 for padding
            entry = self.entries[idx]
            if entry.path not in excluded:
                logger.debug("Next track: %s (idx=%d)", entry.display_name, idx)
                return entry

        raise SimilarityError(
            f"No candidates available after excluding {len(excluded)} recently played tracks. "
            f"Try reducing [playback] no_repeat_window in config.toml."
        )
