"""FAISS index builder for the AutoDJ music library.

Walks the music library (via beets or filesystem), extracts MERT embeddings
and librosa audio features per track, combines them into a single
L2-normalized vector, and stores the result in a FAISS nearest-neighbor index.

Index files written to ``index_dir``:
- ``vectors.index``  — FAISS binary index (``IndexFlatIP``, cosine similarity)
- ``metadata.json``  — list of :class:`IndexEntry` dicts, one per track

Subsequent runs are **incremental**: tracks already present in
``metadata.json`` are skipped.  Pass ``force=True`` to rebuild from scratch.

Example:
    >>> from autodj.config import load_config
    >>> from autodj.model import download_model_if_needed, load_model
    >>> from autodj.indexer import build_index
    >>> cfg = load_config()
    >>> model_path = download_model_if_needed(cfg.model, cfg.index)
    >>> wrapper = load_model(model_path)
    >>> build_index(cfg, wrapper=wrapper, limit=50, force=False)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import faiss
import librosa
import numpy as np
from tqdm import tqdm

from autodj.beets import BeetsNotFoundError, Track, get_all_tracks
from autodj.config import AutoDJConfig
from autodj.model import MertWrapper

logger = logging.getLogger(__name__)

# Combined feature vector dimension: 768 (MERT) + 16 (librosa)
FEATURE_DIM = 784

# Librosa feature vector dimension
_LIBROSA_DIM = 16


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class IndexEntry:
    """Serialisable metadata record stored alongside each FAISS vector.

    Attributes:
        path: String path to the audio file (stored as string for JSON compat).
        title: Track title.
        artist: Artist name.
        album: Album name.
        genre: Genre string.
        bpm: Beats per minute (from beets or estimated by librosa).
        year: Release year.
        length: Track duration in seconds.
    """

    path: str
    title: str
    artist: str
    album: str
    genre: str
    bpm: float
    year: int
    length: float

    @classmethod
    def from_track(cls, track: Track) -> "IndexEntry":
        """Create an :class:`IndexEntry` from a beets :class:`~autodj.beets.Track`.

        Args:
            track: A track loaded from the beets library.

        Returns:
            An :class:`IndexEntry` with the same metadata.
        """
        return cls(
            path=str(track.path),
            title=track.title,
            artist=track.artist,
            album=track.album,
            genre=track.genre,
            bpm=track.bpm,
            year=track.year,
            length=track.length,
        )

    @property
    def display_name(self) -> str:
        """Human-readable label for UI display.

        Returns:
            ``"Artist — Title"`` or just ``"Title"`` when artist is empty.
        """
        if self.artist:
            return f"{self.artist} \u2014 {self.title}"
        return self.title


# ---------------------------------------------------------------------------
# Filesystem walker
# ---------------------------------------------------------------------------


def walk_music_dir(music_dir: Path, formats: list[str]) -> list[Path]:
    """Recursively find all audio files under *music_dir* matching *formats*.

    Args:
        music_dir: Root directory to search.
        formats: List of file extensions to include (without the leading dot,
            e.g. ``["mp3", "flac", "m4a"]``).

    Returns:
        Sorted list of absolute :class:`~pathlib.Path` objects for each match.

    Raises:
        FileNotFoundError: If *music_dir* does not exist.
    """
    if not music_dir.exists():
        raise FileNotFoundError(f"Music directory not found: {music_dir}")

    extensions = {f".{ext.lower()}" for ext in formats}
    found: list[Path] = []
    for path in music_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in extensions:
            found.append(path)
    return sorted(found)


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------


def _extract_librosa_features(
    audio_path: Path,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Extract 16 audio features from a track using librosa.

    Features extracted:
    - RMS energy (1)
    - Spectral centroid mean (1)
    - Zero crossing rate mean (1)
    - Chroma mean per pitch class (12)
    - Onset strength mean (1)

    Args:
        audio_path: Path to the audio file to analyse.

    Returns:
        A tuple of ``(feature_vector, audio_array, sample_rate)`` where
        *feature_vector* is a float32 array of shape ``(16,)`` (not yet
        normalized), *audio_array* is the mono audio loaded by librosa, and
        *sample_rate* is the native sample rate of the file.
    """
    audio, sr = librosa.load(str(audio_path), sr=None, mono=True)

    rms = float(np.mean(librosa.feature.rms(y=audio)))
    spectral_centroid = float(np.mean(librosa.feature.spectral_centroid(y=audio, sr=sr)))
    zcr = float(np.mean(librosa.feature.zero_crossing_rate(y=audio)))
    chroma = np.mean(librosa.feature.chroma_stft(y=audio, sr=sr), axis=1)  # (12,)
    onset_strength = float(np.mean(librosa.onset.onset_strength(y=audio, sr=sr)))

    features = np.array(
        [rms, spectral_centroid, zcr, *chroma, onset_strength],
        dtype=np.float32,
    )
    return features, audio, sr


def _combine_features(
    mert_vec: np.ndarray,
    librosa_vec: np.ndarray,
) -> np.ndarray:
    """Concatenate and L2-normalize MERT and librosa feature vectors.

    Each sub-vector is expected to be pre-normalized before concatenation.
    The final concatenated vector is re-normalized to ensure unit length for
    cosine similarity search via FAISS ``IndexFlatIP``.

    Args:
        mert_vec: L2-normalized float32 array of shape ``(768,)``.
        librosa_vec: float32 array of shape ``(16,)`` (raw, will be normalized
            in-place before concatenation).

    Returns:
        L2-normalized float32 array of shape ``(784,)`` = ``FEATURE_DIM``.
    """
    # Normalize the librosa sub-vector independently
    librosa_norm = np.linalg.norm(librosa_vec)
    if librosa_norm > 0:
        librosa_vec = librosa_vec / librosa_norm

    combined = np.concatenate([mert_vec, librosa_vec]).astype(np.float32)
    norm = np.linalg.norm(combined)
    if norm > 0:
        combined = combined / norm
    return combined


# ---------------------------------------------------------------------------
# FAISS index construction
# ---------------------------------------------------------------------------


def build_faiss_index(vectors: np.ndarray) -> faiss.IndexFlatIP:
    """Build a FAISS inner-product index from a matrix of L2-normalized vectors.

    Using ``IndexFlatIP`` on L2-normalized vectors is equivalent to cosine
    similarity search — no approximation, exact results.

    Args:
        vectors: float32 array of shape ``(n_tracks, FEATURE_DIM)`` where
            each row is an L2-normalized feature vector.

    Returns:
        A populated :class:`faiss.IndexFlatIP` containing all *vectors*.
    """
    dim = vectors.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(vectors)
    return index


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_index(
    entries: list[IndexEntry],
    vectors: np.ndarray,
    index_dir: Path,
) -> None:
    """Write the FAISS index and metadata to *index_dir*.

    Files written:
    - ``vectors.index``  — FAISS binary file
    - ``metadata.json``  — JSON array of :class:`IndexEntry` dicts

    The row order of *entries* must match the row order of *vectors*.

    Args:
        entries: List of :class:`IndexEntry` objects, one per track.
        vectors: float32 array of shape ``(len(entries), FEATURE_DIM)``.
        index_dir: Directory to write files into (must already exist).
    """
    faiss_index = build_faiss_index(vectors)
    faiss.write_index(faiss_index, str(index_dir / "vectors.index"))

    metadata = [asdict(e) for e in entries]
    (index_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Saved index with %d tracks to %s", len(entries), index_dir)


def load_index(
    index_dir: Path,
) -> tuple[list[IndexEntry], faiss.IndexFlatIP]:
    """Load the FAISS index and metadata from *index_dir*.

    Args:
        index_dir: Directory containing ``vectors.index`` and ``metadata.json``.

    Returns:
        A tuple of ``(entries, faiss_index)`` where *entries* is a list of
        :class:`IndexEntry` objects in the same row order as the FAISS index.

    Raises:
        FileNotFoundError: If *index_dir* or its required files are missing.
    """
    index_file = index_dir / "vectors.index"
    metadata_file = index_dir / "metadata.json"

    if not index_dir.exists():
        raise FileNotFoundError(
            f"Index directory not found: {index_dir}\n"
            "Run 'autodj index' to build the library index first."
        )
    if not index_file.exists() or not metadata_file.exists():
        raise FileNotFoundError(
            f"Index files missing in {index_dir}. "
            "Run 'autodj index' to build the library index first."
        )

    faiss_index = faiss.read_index(str(index_file))
    raw = json.loads(metadata_file.read_text(encoding="utf-8"))
    entries = [IndexEntry(**row) for row in raw]
    logger.info("Loaded index with %d tracks from %s", len(entries), index_dir)
    return entries, faiss_index


# ---------------------------------------------------------------------------
# Public build entry point
# ---------------------------------------------------------------------------


def build_index(
    cfg: AutoDJConfig,
    wrapper: MertWrapper,
    limit: int | None,
    force: bool,
) -> None:
    """Build or incrementally update the FAISS index for the music library.

    Reads track list from beets (if configured) or walks the filesystem.
    Skips tracks already present in the existing index unless *force* is set.
    Writes updated index files on completion.

    Args:
        cfg: Full AutoDJ configuration.
        wrapper: A loaded :class:`~autodj.model.MertWrapper` for embedding.
        limit: Maximum number of *new* tracks to embed. ``None`` means no limit.
        force: If ``True``, ignore any existing index and re-embed everything.

    Raises:
        FileNotFoundError: If the music directory does not exist and no beets
            database is configured.
    """
    index_dir = cfg.index.index_dir
    index_dir.mkdir(parents=True, exist_ok=True)

    # --- load existing index (incremental mode) ---
    existing_entries: list[IndexEntry] = []
    existing_vectors: list[np.ndarray] = []
    existing_paths: set[str] = set()

    if not force:
        metadata_file = index_dir / "metadata.json"
        if metadata_file.exists():
            raw = json.loads(metadata_file.read_text(encoding="utf-8"))
            existing_entries = [IndexEntry(**r) for r in raw]
            existing_paths = {e.path for e in existing_entries}

            faiss_file = index_dir / "vectors.index"
            if faiss_file.exists() and existing_entries:
                loaded = faiss.read_index(str(faiss_file))
                existing_vectors = [
                    loaded.reconstruct(i) for i in range(loaded.ntotal)
                ]
                logger.info(
                    "Incremental mode: %d tracks already indexed", len(existing_entries)
                )

    # --- collect tracks to process ---
    tracks: list[Track] = []
    if cfg.library.beets_db and cfg.library.beets_db.exists():
        try:
            tracks = get_all_tracks(cfg.library.beets_db)
            logger.info("Loaded %d tracks from beets library", len(tracks))
        except BeetsNotFoundError:
            logger.warning("Beets DB not found, falling back to filesystem scan")

    if not tracks:
        paths = walk_music_dir(cfg.library.music_dir, cfg.library.supported_formats)
        tracks = [
            Track(
                path=p,
                title=p.stem,
                artist="",
                album="",
                genre="",
                bpm=0.0,
                year=0,
                length=0.0,
            )
            for p in paths
        ]
        logger.info("Filesystem scan found %d tracks", len(tracks))

    # Filter to unindexed tracks
    new_tracks = [t for t in tracks if str(t.path) not in existing_paths]
    if limit is not None:
        new_tracks = new_tracks[:limit]

    logger.info(
        "%d new tracks to index%s",
        len(new_tracks),
        f" (limit={limit})" if limit else "",
    )

    if not new_tracks:
        print("[AutoDJ] Index is up to date — nothing to do.")
        return

    # --- embed new tracks ---
    new_entries: list[IndexEntry] = []
    new_vectors: list[np.ndarray] = []

    for track in tqdm(new_tracks, desc="Indexing", unit="track"):
        try:
            librosa_vec, audio, sr = _extract_librosa_features(track.path)
            mert_vec = wrapper.embed_array(audio, sample_rate=sr)
            combined = _combine_features(mert_vec, librosa_vec)

            new_entries.append(IndexEntry.from_track(track))
            new_vectors.append(combined)
        except Exception as exc:
            logger.warning("Skipping %s: %s", track.path, exc)

    # --- merge and save ---
    all_entries = existing_entries + new_entries

    if existing_vectors:
        all_vectors = np.vstack(
            [np.array(existing_vectors, dtype=np.float32), np.array(new_vectors, dtype=np.float32)]
        )
    else:
        all_vectors = np.array(new_vectors, dtype=np.float32)

    save_index(all_entries, all_vectors, index_dir)
    print(
        f"[AutoDJ] Index updated: {len(new_entries)} new tracks added, "
        f"{len(all_entries)} total."
    )
