"""FAISS-based next-song similarity engine.

Loads the pre-built FAISS index and provides :class:`SimilarityIndex`, which
wraps the index with recently-played exclusion and candidate ranking logic.

Cosine similarity is computed via inner product on L2-normalized vectors
(``IndexFlatIP``), so higher scores mean more similar tracks.

Vectors are pre-computed during ``autodj index`` and stored in the FAISS index.
Playback looks them up by path — no model inference needed at play time.

Example:
    >>> from autodj.similarity import SimilarityIndex
    >>> from collections import deque
    >>> sim = SimilarityIndex.from_index_dir(Path("index"))
    >>> next_track = sim.find_next_for_path("Z:/Music/song.flac", recently_played=deque())
    >>> print(next_track.display_name)
    Portishead — Mysterons
"""

from __future__ import annotations

import logging
import math
import random
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import faiss
import numpy as np

from autodj.indexer import IndexEntry, load_index

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BPM scoring helper
# ---------------------------------------------------------------------------


def _softmax_pick(
    scored: list[tuple[float, IndexEntry]],
    top_k: int,
    temperature: float,
) -> IndexEntry:
    """Pick one entry from *scored* by softmax-weighted random sampling.

    *scored* must be sorted by score descending.  Picks deterministically
    (entry with highest score) when ``top_k <= 1`` or ``temperature <= 0``.

    Args:
        scored: Candidates as ``(score, entry)`` tuples, score-descending.
        top_k: Cap candidate pool to this many top entries.
        temperature: Softmax temperature.  Higher = more uniform; 0 = deterministic.

    Returns:
        Chosen :class:`IndexEntry`.
    """
    if not scored:
        raise ValueError("_softmax_pick called with empty list")
    if top_k <= 1 or temperature <= 0.0:
        return scored[0][1]

    pool = scored[: max(1, top_k)]
    scores = np.array([s for s, _ in pool], dtype=np.float64)
    # Subtract max for numerical stability before exp().
    z = (scores - scores.max()) / max(temperature, 1e-6)
    weights = np.exp(z)
    total = float(weights.sum())
    if not np.isfinite(total) or total <= 0.0:  # pragma: no cover
        return pool[0][1]
    probs = weights / total
    # Non-security weighted pick across nearest neighbours.
    idx = int(np.random.choice(len(pool), p=probs))  # nosec B311
    return pool[idx][1]


def _bpm_score(entry_bpm: float, target_bpm: float, sigma: float = 15.0) -> float:
    """Return a Gaussian similarity score between *entry_bpm* and *target_bpm*.

    Returns 0.0 for unknown BPM (entry_bpm == 0.0) so unknown-BPM tracks
    get a neutral boost — they are neither promoted nor penalised.

    Args:
        entry_bpm: BPM of the candidate track (0.0 = unknown).
        target_bpm: Desired BPM for the current session position.
        sigma: Standard deviation of the Gaussian window in BPM units.
            Default 15.0 ≈ ±15 BPM half-width at half-maximum.

    Returns:
        Float in ``[0.0, 1.0]``.  1.0 means perfect BPM match.
    """
    if entry_bpm <= 0.0:
        return 0.0
    return math.exp(-0.5 * ((entry_bpm - target_bpm) / sigma) ** 2)


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
        """Validate consistency and build the path → FAISS index position map."""
        if self.faiss_index.ntotal != len(self.entries):
            raise ValueError(
                f"Index/metadata mismatch: FAISS has {self.faiss_index.ntotal} vectors "
                f"but metadata has {len(self.entries)} entries."
            )
        # Maps each track's path string to its row position in the FAISS index
        self._path_to_idx: dict[str, int] = {e.path: i for i, e in enumerate(self.entries)}

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

    def reload_from_disk(
        self,
        index_dir: Path,
        music_dir: Path | None = None,
        path_remap: list[tuple[str, str]] | None = None,
    ) -> int:
        """Re-read the index from disk, replacing in-memory state in place.

        Used by the server's background watcher so a long-running
        ``autodj serve`` picks up tracks that ``autodj index`` (running
        in parallel) has just finished embedding.  Index writes are
        atomic (tmp + ``os.replace``) so a reload always sees a
        consistent snapshot.

        Args:
            index_dir: Directory containing ``vectors.index`` and
                ``metadata.json``.
            music_dir: Library root for resolving relative paths.
            path_remap: Cross-OS prefix swaps for legacy absolute paths.

        Returns:
            New track count after reload.
        """
        entries, faiss_index = load_index(
            index_dir,
            music_dir=music_dir,
            path_remap=path_remap,
        )
        self.entries = entries
        self.faiss_index = faiss_index
        # Rebuild the path → row index lookup
        self._path_to_idx = {e.path: i for i, e in enumerate(self.entries)}
        return len(entries)

    @classmethod
    def from_index_dir(
        cls,
        index_dir: Path,
        music_dir: Path | None = None,
        path_remap: list[tuple[str, str]] | None = None,
    ) -> SimilarityIndex:
        """Load a :class:`SimilarityIndex` from the index directory on disk.

        When *music_dir* is provided, relative paths stored in
        ``metadata.json`` are resolved against it; *path_remap* applies
        cross-OS prefix swaps to absolute paths.  This makes a single
        index portable across machines that mount the library at
        different absolute locations.

        Args:
            index_dir: Directory containing ``vectors.index`` and
                ``metadata.json`` as written by
                :func:`autodj.indexer.save_index`.
            music_dir: Library root for resolving relative paths.
            path_remap: Optional ``(from_prefix, to_prefix)`` swaps for
                legacy absolute paths from another host.

        Returns:
            A fully populated :class:`SimilarityIndex`.

        Raises:
            FileNotFoundError: If *index_dir* or its files are missing.

        Example:
            >>> sim = SimilarityIndex.from_index_dir(Path("index"))
        """
        entries, faiss_index = load_index(index_dir, music_dir=music_dir, path_remap=path_remap)
        return cls(faiss_index=faiss_index, entries=entries)

    # ------------------------------------------------------------------
    # Core query
    # ------------------------------------------------------------------

    def find_next(
        self,
        query_vector: np.ndarray,
        recently_played: deque[str],
        n_candidates: int = 10,
        target_bpm: float | None = None,
        bpm_weight: float = 0.2,
        bpm_range: tuple[float, float] | None = None,
        genre_filter: list[str] | None = None,
        invert: bool = False,
        harmonic_from: tuple[int, int] | None = None,
        harmonic_mode: str = "compatible",
        target_energy: float | None = None,
        energy_weight: float = 0.15,
        excluded_artists: set[str] | None = None,
        excluded_albums: set[str] | None = None,
        excluded_titles: set[str] | None = None,
        pick_top_k: int = 1,
        pick_temperature: float = 0.0,
    ) -> IndexEntry:
        """Find the best next track that isn't in *recently_played*.

        Queries FAISS for the top ``n_candidates + len(recently_played)``
        nearest neighbors (by cosine similarity), then filters out any track
        whose path appears in *recently_played*, and returns the highest-ranked
        remaining candidate.

        When *target_bpm* or *bpm_range* is provided, the candidate pool is
        expanded to at least 25 results and candidates are optionally re-ranked
        or filtered by BPM.

        Args:
            query_vector: L2-normalized float32 array of shape
                ``(FEATURE_DIM,)`` representing the current track.
            recently_played: Deque of file path strings (as stored in
                :attr:`IndexEntry.path`) to exclude from results.
            n_candidates: Minimum number of candidate neighbors to retrieve
                before filtering.  Automatic increased to 25 when BPM features
                are active.
            target_bpm: Desired BPM for re-ranking.  ``None`` skips BPM
                scoring entirely (fast path — no behaviour change).
            bpm_weight: Weight for BPM score vs cosine score when
                *target_bpm* is set.  ``final = cosine*(1-w) + bpm_score*w``.
            bpm_range: Hard ``(lo, hi)`` BPM filter.  Tracks with known BPM
                outside this range are excluded.  Tracks with unknown BPM
                (``bpm == 0.0``) always pass.  If filtering leaves nothing,
                the filter is relaxed with a warning.

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

        # Smart-shuffle (invert) needs the *opposite* end of the distance
        # spectrum, so fetch many more candidates and pick from the bottom.
        if invert:
            n_fetch = max(200, n_candidates)
        else:
            n_fetch = (
                max(25, n_candidates)
                if (target_bpm is not None or bpm_range is not None)
                else n_candidates
            )

        # Over-fetch so we have candidates left after filtering
        k = min(n_fetch + len(excluded) + 1, self.ntotal)

        query = query_vector.reshape(1, -1).astype(np.float32)
        scores_2d, indices_2d = self.faiss_index.search(query, k)
        raw_scores = scores_2d[0]
        raw_indices = indices_2d[0]

        # Canonicalise the user-supplied filter once, then match against
        # the canonical form of every candidate's free-text genre.  This
        # lets `genres = ["Electronic"]` match "Electronic / Trance",
        # "Synthwave", "EDM", "IDM", etc. — see autodj.genres.
        from autodj.genres import canonicalise_list, matches

        canonical_filter = canonicalise_list(genre_filter)

        def _genre_ok(entry: IndexEntry) -> bool:
            if not canonical_filter:
                return True
            return matches(entry.genre, canonical_filter)

        def _harmonic_ok(entry: IndexEntry) -> bool:
            if harmonic_from is None:
                return True
            from autodj.dj_meta import harmonic_compatible

            return harmonic_compatible(
                harmonic_from[0],
                harmonic_from[1],
                entry.key,
                entry.mode,
                mode=harmonic_mode,
            )

        # Build candidate list with optional BPM range + genre + harmonic
        # + recent-artist / album / title filtering.  Lower-cased sets so
        # "MGMT" and "mgmt" don't sneak past each other.
        ex_art = {a.lower() for a in (excluded_artists or set()) if a}
        ex_alb = {a.lower() for a in (excluded_albums or set()) if a}
        ex_ttl = {t.lower() for t in (excluded_titles or set()) if t}
        candidates: list[tuple[float, IndexEntry]] = []
        for score, idx in zip(raw_scores, raw_indices, strict=False):
            if idx < 0:  # pragma: no cover -- FAISS empty-slot sentinel
                continue
            entry = self.entries[idx]
            if entry.path in excluded:
                continue
            if bpm_range is not None:
                lo, hi = bpm_range
                if entry.bpm > 0 and not (lo <= entry.bpm <= hi):
                    continue
            if not _genre_ok(entry):
                continue
            if not _harmonic_ok(entry):
                continue
            if ex_art and entry.artist and entry.artist.lower() in ex_art:
                continue
            if ex_alb and entry.album and entry.album.lower() in ex_alb:
                continue
            if ex_ttl and entry.title and entry.title.lower() in ex_ttl:
                continue
            candidates.append((float(score), entry))

        # Fallback if filters left nothing — relax progressively
        if not candidates:
            logger.warning(
                "No candidates after BPM/genre filters; relaxing filters",
            )
            for score, idx in zip(raw_scores, raw_indices, strict=False):
                if idx < 0:  # pragma: no cover -- FAISS empty-slot sentinel
                    continue
                entry = self.entries[idx]
                if entry.path not in excluded:
                    candidates.append((float(score), entry))

        if not candidates:
            raise SimilarityError(
                f"No candidates available after excluding {len(excluded)} recently played tracks. "
                f"Try reducing [playback] no_repeat_window in config.toml."
            )

        # Smart-shuffle: invert the score so least-similar wins.  No BPM
        # re-ranking applies (entropy mode is intentionally far from current).
        if invert:
            candidates.sort(key=lambda x: x[0])  # ascending = least similar first
            best = candidates[0][1]
            logger.debug("Smart-shuffle next: %s", best.display_name)
            return best

        # Fast path: no BPM / energy re-ranking needed
        if target_bpm is None and target_energy is None:
            candidates.sort(key=lambda x: x[0], reverse=True)
            best = _softmax_pick(candidates, pick_top_k, pick_temperature)
            logger.debug("Next track: %s", best.display_name)
            return best

        # Re-rank with optional BPM + energy components blended into cosine
        reranked: list[tuple[float, IndexEntry]] = []
        cosine_w = (
            1.0
            - (bpm_weight if target_bpm is not None else 0.0)
            - (energy_weight if target_energy is not None else 0.0)
        )
        cosine_w = max(0.0, cosine_w)
        for cosine_score, entry in candidates:
            blended = cosine_score * cosine_w
            if target_bpm is not None:
                blended += _bpm_score(entry.bpm, target_bpm) * bpm_weight
            if target_energy is not None:
                # Gaussian distance in normalised energy units (energy is RMS, ~0–1)
                if entry.energy > 0:
                    diff = abs(entry.energy - target_energy) / 0.15  # sigma=0.15
                    e_score = float(np.exp(-0.5 * diff * diff))
                else:
                    e_score = 0.0
                blended += e_score * energy_weight
            reranked.append((blended, entry))
        reranked.sort(key=lambda x: x[0], reverse=True)

        best = _softmax_pick(reranked, pick_top_k, pick_temperature)
        logger.debug(
            "Next track (BPM re-ranked): %s (bpm=%.0f, target=%.0f)",
            best.display_name,
            best.bpm,
            target_bpm,
        )
        return best

    def find_next_for_path(
        self,
        current_path: str,
        recently_played: deque[str],
        n_candidates: int = 10,
        target_bpm: float | None = None,
        bpm_weight: float = 0.2,
        bpm_range: tuple[float, float] | None = None,
        genre_filter: list[str] | None = None,
        invert: bool = False,
        harmonic_only: bool = False,
        harmonic_mode: str = "compatible",
        target_energy: float | None = None,
        energy_weight: float = 0.15,
        excluded_artists: set[str] | None = None,
        excluded_albums: set[str] | None = None,
        excluded_titles: set[str] | None = None,
        pick_top_k: int = 1,
        pick_temperature: float = 0.0,
    ) -> IndexEntry:
        """Find the next track using the pre-computed vector for *current_path*.

        Reconstructs the stored embedding vector from the FAISS index by path,
        then delegates to :meth:`find_next`.  No model inference is needed —
        vectors are looked up from the index built by ``autodj index``.

        All keyword arguments are forwarded to :meth:`find_next`.

        Args:
            current_path: The file path string of the currently playing track,
                as stored in :attr:`IndexEntry.path`.
            recently_played: Deque of file path strings to exclude from results.
            n_candidates: Number of candidates to retrieve before filtering.
            target_bpm: Desired BPM for re-ranking (forwarded to find_next).
            bpm_weight: BPM vs cosine blend weight (forwarded to find_next).
            bpm_range: Hard BPM filter ``(lo, hi)`` (forwarded to find_next).

        Returns:
            The :class:`IndexEntry` for the recommended next track.

        Raises:
            SimilarityError: If *current_path* is not in the index, or if no
                candidates remain after exclusions.

        Example:
            >>> next_track = sim.find_next_for_path(
            ...     "Z:/Music/Portishead/Dummy/01 - Mysterons.flac",
            ...     recently_played=deque(),
            ... )
        """
        idx = self._path_to_idx.get(current_path)
        if idx is None:
            raise SimilarityError(
                f"Track not in index: {current_path}\nRun 'autodj index' to add it, then retry."
            )
        query_vector = self.faiss_index.reconstruct(idx)
        # Resolve harmonic_from from the current entry's key/mode if requested
        harmonic_from: tuple[int, int] | None = None
        if harmonic_only:
            cur = self.entries[idx]
            harmonic_from = (cur.key, cur.mode)
        return self.find_next(
            query_vector,
            recently_played,
            n_candidates,
            target_bpm=target_bpm,
            bpm_weight=bpm_weight,
            bpm_range=bpm_range,
            genre_filter=genre_filter,
            invert=invert,
            harmonic_from=harmonic_from,
            harmonic_mode=harmonic_mode,
            target_energy=target_energy,
            energy_weight=energy_weight,
            excluded_artists=excluded_artists,
            excluded_albums=excluded_albums,
            excluded_titles=excluded_titles,
            pick_top_k=pick_top_k,
            pick_temperature=pick_temperature,
        )

    def find_distant(
        self,
        current_path: str,
        recently_played: deque[str],
    ) -> IndexEntry:
        """Find a sonically *distant* track for discovery mode injection.

        Queries the full index from the current track's vector, then picks
        a random non-excluded entry from the bottom quartile by cosine score
        (i.e., the least similar tracks).  Falls back to a random non-excluded
        entry from the entire library if the bottom quartile is fully excluded.

        Args:
            current_path: Path of the currently playing track.
            recently_played: Tracks to exclude from the result.

        Returns:
            A :class:`IndexEntry` that is sonically distant from the current track.

        Raises:
            SimilarityError: If *current_path* is not in the index or if no
                non-excluded track exists.
        """
        excluded = set(recently_played)

        idx = self._path_to_idx.get(current_path)
        if idx is None:
            raise SimilarityError(f"Track not in index: {current_path}")

        query_vector = self.faiss_index.reconstruct(idx).reshape(1, -1).astype(np.float32)
        scores_2d, indices_2d = self.faiss_index.search(query_vector, self.ntotal)
        raw_scores = scores_2d[0]
        raw_indices = indices_2d[0]

        # Results come back highest-similarity first; reverse for most-distant-first
        # Skip invalid padding indices (-1)
        all_valid = [
            (float(raw_scores[j]), int(raw_indices[j]))
            for j in range(len(raw_indices))
            if raw_indices[j] >= 0
        ]

        # Bottom quartile = last 25% of the sorted-by-similarity list
        n_total = len(all_valid)
        bottom_start = max(0, int(n_total * 0.75))
        bottom_quartile = all_valid[bottom_start:]

        distant_candidates = [
            self.entries[i] for _, i in bottom_quartile if self.entries[i].path not in excluded
        ]

        if distant_candidates:
            # Non-security discovery pick — random.choice is fine here.
            chosen = random.choice(distant_candidates)  # nosec B311
            logger.debug("Discovery track: %s", chosen.display_name)
            return chosen

        # Fallback: any non-excluded track (full library)
        fallback = [e for e in self.entries if e.path not in excluded]
        if fallback:
            # Non-security fallback pick.
            return random.choice(fallback)  # nosec B311

        raise SimilarityError(
            "No candidates available for discovery — all tracks are in recently_played."
        )
