"""FAISS index builder for the AutoDJ music library.

Walks the music library (via beets or filesystem), extracts MuQ embeddings
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

import contextlib
import json
import logging
import warnings
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import faiss
import numpy as np

from autodj.beets import BeetsNotFoundError, Track, get_all_tracks
from autodj.config import AutoDJConfig

# Heavy audio deps imported with graceful None fallback so the lighter
# commands (`enrich`, `prune`, `stats`, `playlist`) work on minimal
# installs that omit them.  build_index() guards against None at runtime.
try:
    import librosa as _librosa_mod

    librosa: Any = _librosa_mod
except ImportError:  # pragma: no cover — minimal install path
    librosa = None
try:
    import soundfile as _sf_mod

    sf: Any = _sf_mod
except ImportError:  # pragma: no cover
    sf = None
try:
    from tqdm import tqdm as _tqdm_real

    tqdm: Any = _tqdm_real
except ImportError:  # pragma: no cover

    class _TqdmFallback:
        """No-op stand-in for tqdm when the package is missing."""

        def __init__(self, it: Any = None, **_kw: Any) -> None:
            self._it = it

        def __iter__(self) -> Any:
            return iter(self._it) if self._it is not None else iter(())

        def update(self, _n: int = 1) -> None:
            return None

        def close(self) -> None:
            return None

        def __enter__(self) -> _TqdmFallback:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

    tqdm = _TqdmFallback


if TYPE_CHECKING:
    from autodj.model import MuqWrapper

logger = logging.getLogger(__name__)

# Embedding dimension is also referenced from minimal-install paths, so we
# duplicate the constant here rather than importing autodj.model (which
# pulls in torch).  Kept in sync with autodj.model.EMBEDDING_DIM.
EMBEDDING_DIM = 1024

# Librosa feature vector dimension (RMS, spectral centroid, ZCR, 12 chroma, onset)
_LIBROSA_DIM = 16

# Combined feature vector dimension: 1024 (MuQ) + 16 (librosa)
FEATURE_DIM = EMBEDDING_DIM + _LIBROSA_DIM


def _relativize_for_storage(abs_path: str, music_dir: Path | None) -> str:
    """Convert an absolute path to a forward-slashed string for ``metadata.json``.

    If *abs_path* lives under *music_dir*, the returned string is RELATIVE
    to *music_dir* — making the index portable across machines that mount
    the library at a different absolute path.  Otherwise the absolute path
    is returned (forward-slashed for cross-OS readability).

    Args:
        abs_path: Absolute path string from an :class:`IndexEntry` at runtime.
        music_dir: Library root.  ``None`` disables relativization.

    Returns:
        A forward-slashed path string suitable for JSON storage.
    """
    # Pure string-prefix match — never call Path.resolve() here.  resolve()
    # stat()s the file (and follows symlinks); for libraries on NFS/SMB this
    # turns the per-checkpoint loop over 70k+ entries into ~150 s of stat()
    # RPCs and dominates total indexing time.  Paths reaching this function
    # are already absolute (set by _resolve_for_runtime at startup) so the
    # only job left is to lop off the music_dir prefix.
    p = Path(abs_path)
    if music_dir is not None:
        md_str = music_dir.as_posix().rstrip("/") + "/"
        p_str = p.as_posix()
        if p_str.startswith(md_str):
            return p_str[len(md_str) :]
    return p.as_posix()


def _resolve_for_runtime(
    stored: str,
    music_dir: Path | None,
    path_remap: list[tuple[str, str]] | None,
) -> str:
    """Convert a stored path string into an absolute runtime path.

    Resolution order:
    1. If *stored* is absolute and *path_remap* matches a prefix, swap it.
    2. If *stored* is absolute, return as-is (with native separators).
    3. If *stored* is relative, join with *music_dir*.

    Args:
        stored: Path string as written in ``metadata.json`` (relative,
            absolute POSIX, or absolute Windows).
        music_dir: Library root for resolving relative paths.
        path_remap: Optional ``(from_prefix, to_prefix)`` swaps for absolute
            paths whose mount point differs on this machine.

    Returns:
        An absolute path string using native separators.
    """
    s = stored.replace("\\", "/")
    is_abs = s.startswith("/") or (len(s) >= 2 and s[1] == ":")

    if is_abs and path_remap:
        for from_pre, to_pre in path_remap:
            from_norm = from_pre.replace("\\", "/")
            if s.startswith(from_norm):
                s = to_pre.replace("\\", "/") + s[len(from_norm) :]
                break

    if is_abs:
        return str(Path(s))

    base = music_dir if music_dir is not None else Path()
    return str(base / s)


def _resolve_beets_path(path: Path, music_dir: Path) -> Path:
    """Resolve a beets-stored path to an absolute local path.

    Recent beets versions store track paths *relative* to the library
    ``directory`` setting (the ``relative_path`` migration).  For relative
    paths we prepend *music_dir* — which must be the local mount point of
    that beets ``directory`` — so the file can be opened on this machine.

    Absolute paths are returned unchanged: they are typically tracks living
    outside the main library tree (e.g. an extra mount), and the user is
    expected to have them reachable as-is.

    Args:
        path: Original path from the beets database (may be relative or absolute).
        music_dir: Local directory that corresponds to the beets library root.

    Returns:
        An absolute :class:`~pathlib.Path` pointing to the local file.
    """
    # Normalise to forward slashes for portable comparison
    raw = str(path).replace("\\", "/")

    # Heuristics for "absolute": POSIX root, or Windows drive letter (e.g. "Z:/...")
    is_absolute = raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":")
    if is_absolute:
        return path

    return music_dir / raw.lstrip("/")


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
        energy: RMS loudness, 0.0 = unknown / not yet enriched.
        key: Chromatic key 0–11 (C=0, C#=1, …, B=11), -1 = unknown.
        mode: 1 = major, 0 = minor, -1 = unknown / not yet enriched.
        tempo_confidence: Librosa beat-tracking confidence 0.0–1.0,
            0.0 = unknown / not yet enriched.
        embedded_at: Unix timestamp when this entry was embedded.  Used to
            detect replaced files: if ``file.mtime > embedded_at`` on the
            next ``index`` run the entry is dropped and re-embedded.
            ``0.0`` = legacy entry written before this field existed; on
            first encounter the indexer snapshots it to the file's current
            mtime so future replacements are detectable.
    """

    path: str
    title: str
    artist: str
    album: str
    genre: str
    bpm: float
    year: int
    length: float
    energy: float
    key: int
    mode: int
    tempo_confidence: float
    embedded_at: float = 0.0

    @classmethod
    def from_track(cls, track: Track) -> IndexEntry:
        """Create an :class:`IndexEntry` from a beets :class:`~autodj.beets.Track`.

        Args:
            track: A track loaded from the beets library.

        Returns:
            An :class:`IndexEntry` with the same metadata.  ``embedded_at``
            is stamped to the current wall-clock time so future replacements
            can be detected by mtime comparison.
        """
        import time as _time

        return cls(
            path=str(track.path),
            title=track.title,
            artist=track.artist,
            album=track.album,
            genre=track.genre,
            bpm=track.bpm,
            year=track.year,
            length=track.length,
            energy=0.0,
            key=-1,
            mode=-1,
            tempo_confidence=0.0,
            embedded_at=_time.time(),
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
# Audio loading
# ---------------------------------------------------------------------------

# Formats handled natively by soundfile (fast C library, no Python overhead).
# Everything else falls back to librosa (handles MP3, M4A via audioread).
_SOUNDFILE_FORMATS = {".flac", ".wav", ".ogg", ".aif", ".aiff"}


def _load_audio(path: Path) -> tuple[np.ndarray, int]:
    """Load an audio file to a mono float32 array at its native sample rate.

    Uses soundfile for FLAC/WAV (significantly faster than librosa for these
    formats).  Falls back to librosa for MP3, M4A, and other formats that
    soundfile does not support.

    Args:
        path: Path to the audio file.

    Returns:
        ``(audio, sample_rate)`` where *audio* is a 1-D float32 mono array.
    """
    if path.suffix.lower() in _SOUNDFILE_FORMATS:
        try:
            audio, sr = sf.read(str(path), dtype="float32", always_2d=False)
            if audio.ndim == 2:
                audio = audio.mean(axis=1)  # stereo → mono
            return audio, sr
        except sf.LibsndfileError as exc:
            # libsndfile chokes on some valid FLACs over NFS ("flac decoder lost
            # sync") and on streams it can't seek through cleanly.  librosa's
            # audioread/ffmpeg path decodes them fine — fall through.
            logger.debug("soundfile failed on %s, falling back to librosa: %s", path, exc)
    # Fallback: librosa handles MP3, M4A, and FLACs that libsndfile rejected.
    # Suppress the noisy "PySoundFile failed. Trying audioread instead." warning
    # that fires for every MP3/M4A — it's expected and harmless.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*PySoundFile failed.*", category=UserWarning)
        warnings.filterwarnings("ignore", message=".*audioread.*", category=UserWarning)
        audio, sr = librosa.load(str(path), sr=None, mono=True)
        return audio, int(sr)


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------


def _extract_librosa_features(
    audio_path: Path,
) -> tuple[np.ndarray, np.ndarray, int, dict]:
    """Extract 16 audio features from a track using librosa, plus extra metadata.

    Features extracted (stored in FAISS vector, 16 dims total):
    - RMS energy (1)
    - Spectral centroid mean (1)
    - Zero crossing rate mean (1)
    - Chroma mean per pitch class (12)
    - Onset strength mean (1)

    Extra metadata extracted (stored in ``IndexEntry`` only, not in vectors):
    - ``energy`` — RMS loudness (same as feature[0])
    - ``key`` — chromatic key 0–11 estimated from chroma template matching
    - ``mode`` — 1 = major, 0 = minor
    - ``tempo_confidence`` — ratio of detected beats to expected beats (0–1)

    Args:
        audio_path: Path to the audio file to analyse.

    Returns:
        A tuple of ``(feature_vector, audio_array, sample_rate, extra_meta)``
        where *feature_vector* is a float32 array of shape ``(16,)`` (not yet
        normalized), *audio_array* is the mono audio, *sample_rate* is the
        native sample rate, and *extra_meta* is a dict with keys
        ``energy``, ``key``, ``mode``, ``tempo_confidence``.
    """
    audio, sr = _load_audio(audio_path)

    if len(audio) == 0:
        raise ValueError("audio file contains no samples")

    with warnings.catch_warnings():
        # Suppress warnings that fire on short or silent tracks — they are
        # handled gracefully by librosa (clipped n_fft, zero chroma, etc.).
        warnings.filterwarnings("ignore", message=".*n_fft.*too large.*", category=UserWarning)
        warnings.filterwarnings("ignore", message=".*empty frequency set.*", category=UserWarning)

        rms = float(np.mean(librosa.feature.rms(y=audio)))
        spectral_centroid = float(np.mean(librosa.feature.spectral_centroid(y=audio, sr=sr)))
        zcr = float(np.mean(librosa.feature.zero_crossing_rate(y=audio)))
        chroma = np.mean(librosa.feature.chroma_stft(y=audio, sr=sr), axis=1)  # (12,)
        onset_strength = float(np.mean(librosa.onset.onset_strength(y=audio, sr=sr)))

        # --- Extra metadata (IndexEntry only, not included in FAISS vectors) ---

        # energy = reuse already-computed RMS
        energy = rms

        # key + mode via major/minor chromatic template matching
        _major = np.array([1, 0, 1, 0, 1, 1, 0, 1, 0, 1, 0, 1], dtype=np.float32)
        _minor = np.array([1, 0, 1, 1, 0, 1, 0, 1, 1, 0, 1, 0], dtype=np.float32)
        major_scores = [float(np.dot(np.roll(_major, k), chroma)) for k in range(12)]
        minor_scores = [float(np.dot(np.roll(_minor, k), chroma)) for k in range(12)]
        if max(major_scores) >= max(minor_scores):
            mode = 1
            key = int(np.argmax(major_scores))
        else:
            mode = 0
            key = int(np.argmax(minor_scores))

        # tempo confidence: detected beats / expected beats at estimated tempo
        try:
            tempo_arr, beat_frames = librosa.beat.beat_track(y=audio, sr=sr)
            tempo_val = float(np.atleast_1d(tempo_arr)[0])
            duration_sec = len(audio) / max(1, sr)
            expected = (tempo_val / 60.0) * duration_sec
            tempo_confidence = float(min(1.0, len(beat_frames) / max(1.0, expected)))
        except Exception:
            tempo_confidence = 0.0

    features = np.array(
        [rms, spectral_centroid, zcr, *chroma, onset_strength],
        dtype=np.float32,
    )
    extra_meta = {
        "energy": float(energy),
        "key": key,
        "mode": mode,
        "tempo_confidence": tempo_confidence,
    }
    return features, audio, sr, extra_meta


def _combine_features(
    embedding_vec: np.ndarray,
    librosa_vec: np.ndarray,
) -> np.ndarray:
    """Concatenate and L2-normalize the MuQ and librosa feature vectors.

    Each sub-vector is expected to be pre-normalized before concatenation.
    The final concatenated vector is re-normalized to ensure unit length for
    cosine similarity search via FAISS ``IndexFlatIP``.

    Args:
        embedding_vec: L2-normalized float32 array of shape ``(EMBEDDING_DIM,)``
            from the MuQ model.
        librosa_vec: float32 array of shape ``(16,)`` (raw, will be normalized
            in-place before concatenation).

    Returns:
        L2-normalized float32 array of shape ``(FEATURE_DIM,)``.
    """
    # Normalize the librosa sub-vector independently
    librosa_norm = np.linalg.norm(librosa_vec)
    if librosa_norm > 0:
        librosa_vec = librosa_vec / librosa_norm

    combined = np.concatenate([embedding_vec, librosa_vec]).astype(np.float32)
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


def _write_faiss_chunked(index: faiss.Index, path: Path, chunk_size: int = 1 << 20) -> None:
    """Write a FAISS index to *path* via chunked Python IO.

    FAISS's native ``write_index`` calls C++ ``fwrite`` which buffers
    aggressively and fails inscrutably on some SMB / NAS shares with
    "Invalid argument" after only a few KB.  This helper sidesteps that
    by:

    1. Serialising the index to a bytes buffer in memory
       (``faiss.serialize_index``).
    2. Writing the buffer to disk in *chunk_size* slices via Python's
       file-object ``write``, which the SMB driver handles much more
       reliably.
    3. ``fsync`` after the last chunk so the data is durably committed
       before the caller does an ``os.replace`` over the live file.

    Memory cost: an extra ~Nbytes copy of the index during write.  For a
    300 MB FAISS index that's 300 MB peak — easy on any reasonable host.

    Args:
        index: The FAISS index to save.
        path: Destination path (typically a ``*.tmp`` sibling that the
            caller will rename over the live file).
        chunk_size: Bytes per write call.  1 MB is a good balance —
            small enough that each ``write`` round-trips quickly on SMB,
            large enough to amortise per-call overhead.
    """
    buf = faiss.serialize_index(index)  # 1-D uint8 numpy array
    mv = memoryview(buf).cast("B")
    with open(path, "wb") as fh:
        for i in range(0, len(mv), chunk_size):
            fh.write(mv[i : i + chunk_size])
        fh.flush()
        try:
            import os as _os

            _os.fsync(fh.fileno())
        except OSError:
            # fsync may not be supported on every FS — best-effort
            pass


def save_index(
    entries: list[IndexEntry],
    vectors: np.ndarray,
    index_dir: Path,
    music_dir: Path | None = None,
) -> None:
    """Write the FAISS index and metadata to *index_dir* atomically.

    Files written:
    - ``vectors.index``  — FAISS binary file
    - ``metadata.json``  — JSON array of :class:`IndexEntry` dicts

    Both files are written to ``*.tmp`` siblings first and then renamed
    over the originals.  A failed write therefore leaves the existing
    on-disk index intact instead of corrupting it — important for
    network filesystems (SMB/NFS) where a partial write can occur
    on transient I/O errors.

    When *music_dir* is provided, paths under it are stored RELATIVE to
    *music_dir* (forward-slashed) — making the index portable across
    machines that mount the library at a different absolute path.  Paths
    outside *music_dir* are stored as forward-slashed absolute strings.
    Runtime ``entry.path`` values are not mutated.

    The row order of *entries* must match the row order of *vectors*.

    Args:
        entries: List of :class:`IndexEntry` objects, one per track.
        vectors: float32 array of shape ``(len(entries), FEATURE_DIM)``.
        index_dir: Directory to write files into (must already exist).
        music_dir: Library root — when set, paths are relativized for storage.
    """
    import os

    vectors_final = index_dir / "vectors.index"
    metadata_final = index_dir / "metadata.json"
    vectors_tmp = index_dir / "vectors.index.tmp"
    metadata_tmp = index_dir / "metadata.json.tmp"

    # --- write FAISS to temp, then rename ---
    try:
        faiss_index = build_faiss_index(vectors)
        _write_faiss_chunked(faiss_index, vectors_tmp)
        os.replace(vectors_tmp, vectors_final)
    except Exception:
        if vectors_tmp.exists():
            with contextlib.suppress(OSError):
                vectors_tmp.unlink()
        raise

    # --- write metadata to temp, then rename ---
    metadata = []
    for e in entries:
        row = asdict(e)
        row["path"] = _relativize_for_storage(e.path, music_dir)
        metadata.append(row)
    try:
        payload = json.dumps(metadata, indent=2, ensure_ascii=False)
        # Chunked write — same SMB resilience reason as _write_faiss_chunked.
        # Encode once so we can slice bytes (avoids re-encoding per chunk).
        data = payload.encode("utf-8")
        chunk = 1 << 20  # 1 MB
        with open(metadata_tmp, "wb") as fh:
            for i in range(0, len(data), chunk):
                fh.write(data[i : i + chunk])
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        os.replace(metadata_tmp, metadata_final)
    except Exception:
        if metadata_tmp.exists():
            with contextlib.suppress(OSError):
                metadata_tmp.unlink()
        raise

    logger.info("Saved index with %d tracks to %s", len(entries), index_dir)


class PruneSafetyError(RuntimeError):
    """Raised when prune would remove a suspiciously large fraction of entries.

    Almost always indicates a misconfigured ``music_dir`` / ``path_remap``
    rather than genuine library cleanup — refusing the operation prevents
    data loss.
    """


# Refuse to auto-prune more than this fraction of the index without
# explicit user opt-in via ``allow_mass_prune=True``.
PRUNE_SAFETY_THRESHOLD: float = 0.20


def _find_beets_row(
    entry_path: str,
    music_dir: Path | None,
    all_rows: dict[bytes, dict],
    _path_candidates: Any,
) -> dict | None:
    """Look up *entry_path* in the bulk-loaded beets row map (try every candidate)."""
    for candidate in _path_candidates(entry_path, music_dir):
        row = all_rows.get(candidate.encode("utf-8"))
        if row is not None:
            return row
    return None


def _apply_text_fields(entry: IndexEntry, row: dict, text_cols: tuple[str, ...]) -> bool:
    """Overwrite entry text fields with non-empty beets values; return True on any change."""
    changed = False
    for col in text_cols:
        v = str(row[col] or "")
        if v and getattr(entry, col) != v:
            setattr(entry, col, v)
            changed = True
    return changed


def _apply_numeric_fields(entry: IndexEntry, row: dict) -> bool:
    """Overwrite entry numeric fields (bpm/year/length) with positive beets values."""
    changed = False
    bpm_v = float(row["bpm"] or 0.0)
    if bpm_v > 0 and abs(entry.bpm - bpm_v) > 1e-3:
        entry.bpm = bpm_v
        changed = True
    year_v = int(row["year"] or 0)
    if year_v > 0 and entry.year != year_v:
        entry.year = year_v
        changed = True
    length_v = float(row["length"] or 0.0)
    if length_v > 0 and abs(entry.length - length_v) > 1e-3:
        entry.length = length_v
        changed = True
    return changed


def _apply_key_field(entry: IndexEntry, row: dict, parse_initial_key: Any) -> bool:
    """Overwrite entry key/mode from beets `initial_key`; return True when changed."""
    parsed = parse_initial_key(str(row["initial_key"] or ""))
    if parsed is None or (entry.key, entry.mode) == parsed:
        return False
    entry.key, entry.mode = parsed
    return True


def _apply_beets_row(
    entry: IndexEntry,
    row: dict,
    text_cols: tuple[str, ...],
    has_initial_key: bool,
    parse_initial_key: Any,
) -> bool:
    """Apply every overwrite rule for one entry; return True when something changed."""
    changed = _apply_text_fields(entry, row, text_cols)
    if _apply_numeric_fields(entry, row):
        changed = True
    if has_initial_key and _apply_key_field(entry, row, parse_initial_key):
        changed = True
    return changed


def enrich_from_beets(
    index_dir: Path,
    music_dir: Path | None,
    beets_db: Path,
    path_remap: list[tuple[str, str]] | None = None,
) -> tuple[int, int]:
    """Refresh existing index entries with whatever beets has on each track.

    Walks ``metadata.json``, looks up each track in *beets_db* by path,
    and overwrites a curated set of fields when beets has values for
    them.  This is the upgrade path for users who indexed without beets
    and later add a ``library.db`` — they get title / artist / album /
    genre / bpm / year / length backfill plus key/mode from
    ``initial_key`` if the keyfinder plugin ran.

    No re-embedding — only metadata.  Vectors are reloaded and saved
    unchanged via the atomic :func:`save_index`.

    Field-level rules:

    - ``title`` / ``artist`` / ``album`` / ``genre``: overwritten when
      beets value is non-empty AND differs from current.
    - ``bpm`` / ``year`` / ``length``: overwritten when beets value is
      > 0 AND differs from current.
    - ``key`` / ``mode``: overwritten when beets ``initial_key`` parses
      AND differs from current.

    Tracks without a beets row, or with no schema-present columns, are
    left untouched.

    Args:
        index_dir: Directory containing ``vectors.index`` + ``metadata.json``.
        music_dir: Library root (used to resolve relative stored paths).
        beets_db: Path to the beets ``library.db``.
        path_remap: Optional cross-OS prefix swaps for legacy absolute paths.

    Returns:
        ``(updated, total)`` — count of entries with at least one field
        changed, and the total entries scanned.
    """
    from autodj.beets import (
        BeetsNotFoundError,
        _items_columns,
        _open_db,
        _path_candidates,
        parse_initial_key,
    )

    metadata_file = index_dir / "metadata.json"
    faiss_file = index_dir / "vectors.index"
    if not metadata_file.exists() or not faiss_file.exists():
        return (0, 0)

    raw = json.loads(metadata_file.read_text(encoding="utf-8"))
    entries = [IndexEntry(**r) for r in raw]
    for e in entries:
        e.path = _resolve_for_runtime(e.path, music_dir, path_remap)

    try:
        conn = _open_db(beets_db)
    except BeetsNotFoundError:
        logger.warning("Beets DB not found at %s", beets_db)
        return (0, len(entries))

    text_cols = ("title", "artist", "album", "genre")
    num_cols = ("bpm", "year", "length")
    has_initial_key = False
    updated = 0
    try:
        cols = _items_columns(conn)
        select_cols = list(text_cols) + list(num_cols)
        if "initial_key" in cols:
            select_cols.append("initial_key")
            has_initial_key = True
        select_sql = ", ".join(select_cols)

        # Bulk-fetch every row once into a dict keyed by path bytes.  This
        # is dramatically faster than per-entry SELECT over SMB / NAS —
        # 71 k tracks completes in seconds vs 30+ minutes of round-trips.
        logger.info("Bulk-loading beets items into memory...")
        all_rows: dict[bytes, dict] = {}
        bulk_sql = f"SELECT path, {select_sql} FROM items"  # nosec B608
        for row in conn.execute(bulk_sql):
            all_rows[bytes(row["path"])] = {col: row[col] for col in select_cols}
        logger.info("Loaded %d beets items", len(all_rows))

        print(
            f"[AutoDJ] Phase: Enriching — scanning {len(entries)} tracks against beets.",
            flush=True,
        )
        for e in tqdm(
            entries,
            total=len(entries),
            desc="Enriching",
            unit="track",
            disable=False,
            dynamic_ncols=True,
        ):
            row = _find_beets_row(e.path, music_dir, all_rows, _path_candidates)
            if row is None:
                continue
            if _apply_beets_row(e, row, text_cols, has_initial_key, parse_initial_key):
                updated += 1
    finally:
        conn.close()

    if updated == 0:
        logger.info("Enrich: no changes from beets")
        return (0, len(entries))

    # Re-save with the same vectors (load + re-write metadata only)
    loaded = faiss.read_index(str(faiss_file))
    all_vectors = np.array(
        [loaded.reconstruct(i) for i in range(len(entries))],
        dtype=np.float32,
    )
    save_index(entries, all_vectors, index_dir, music_dir=music_dir)
    logger.info("Enrich: updated %d/%d tracks", updated, len(entries))
    return (updated, len(entries))


def _is_relative_storage(raw: list[dict]) -> bool:
    """True when every stored path string is already in relative form."""
    return all(
        not (
            r["path"].startswith("/")
            or (len(r["path"]) >= 2 and r["path"][1] == ":")
            or "\\" in r["path"]
        )
        for r in raw
    )


def _check_prune_safety(removed: int, total: int, allow_mass_prune: bool) -> None:
    """Raise PruneSafetyError when the prune ratio would cross the threshold."""
    if allow_mass_prune or total == 0 or removed / total <= PRUNE_SAFETY_THRESHOLD:
        return
    raise PruneSafetyError(
        f"Refusing to prune {removed}/{total} tracks "
        f"({removed / total:.0%} > {PRUNE_SAFETY_THRESHOLD:.0%} threshold).\n"
        "This usually means [library] music_dir or path_remap in your "
        "config does not match where the indexed files actually live.\n"
        "Fix the config first, then re-run.  If you really did delete "
        "this many tracks, pass allow_mass_prune=True (or "
        "`autodj prune --force` from the CLI)."
    )


def _delete_index_files(metadata_file: Path, faiss_file: Path) -> None:
    """Remove the index files (called when prune empties the library)."""
    metadata_file.unlink()
    faiss_file.unlink()


def _maybe_migrate_paths(
    loaded: faiss.IndexFlatIP,
    entries: list[IndexEntry],
    index_dir: Path,
    music_dir: Path | None,
    already_relative: bool,
) -> None:
    """Re-save metadata in relative form when storage is still absolute."""
    if music_dir is None or already_relative:
        return
    all_vectors = np.array(
        [loaded.reconstruct(i) for i in range(len(entries))],
        dtype=np.float32,
    )
    save_index(entries, all_vectors, index_dir, music_dir=music_dir)


def prune_index(
    index_dir: Path,
    music_dir: Path | None = None,
    path_remap: list[tuple[str, str]] | None = None,
    allow_mass_prune: bool = False,
    throttle_ms: float = 0.0,
    stat_workers: int = 8,
) -> tuple[int, int]:
    """Remove index entries whose audio files no longer exist on disk.

    Loads ``metadata.json`` and ``vectors.index``, resolves each stored
    path against *music_dir* + *path_remap*, drops every row whose audio
    file is missing, and rewrites both files via :func:`save_index`.  If
    every track is gone, the index files are deleted instead.

    Always rewrites ``metadata.json`` if any rows were stored as absolute
    paths under *music_dir* — converting them to portable relative paths.
    No-op when no index exists or no rewrite is needed.

    Safety: if more than :data:`PRUNE_SAFETY_THRESHOLD` of the entries
    would be removed, raises :class:`PruneSafetyError` instead of touching
    the index.  Override with ``allow_mass_prune=True`` (e.g. after
    confirming you really did delete most of your library).

    Args:
        index_dir: Directory containing ``vectors.index`` and ``metadata.json``.
        music_dir: Library root for resolving relative paths.
        path_remap: Optional cross-OS prefix swaps for legacy absolute paths.
        allow_mass_prune: If ``True``, skip the safety check and prune
            even if it would remove most of the index.

    Returns:
        ``(removed, kept)`` — count of entries dropped and count surviving.

    Raises:
        PruneSafetyError: If the prune would exceed the safety threshold
            and ``allow_mass_prune`` is not set.
    """
    metadata_file = index_dir / "metadata.json"
    faiss_file = index_dir / "vectors.index"
    if not metadata_file.exists() or not faiss_file.exists():
        return (0, 0)

    raw = json.loads(metadata_file.read_text(encoding="utf-8"))
    entries = [IndexEntry(**r) for r in raw]
    already_relative = music_dir is not None and _is_relative_storage(raw)
    for e in entries:
        e.path = _resolve_for_runtime(e.path, music_dir, path_remap)
    loaded = faiss.read_index(str(faiss_file))

    # Existence check is RTT-bound on NFS/SMB libraries — at 70k+ tracks the
    # serial loop dominates the whole `index` command.  Fan out across a
    # thread pool so the kernel can pipeline stat() RPCs.
    print(
        f"[AutoDJ] Phase: Pruning — checking {len(entries)} indexed files on disk.",
        flush=True,
    )
    import time as _time

    throttle_s = max(0.0, throttle_ms) / 1000.0
    pool_size = max(1, stat_workers)

    def _exists_throttled(e: IndexEntry) -> bool:
        if throttle_s:
            _time.sleep(throttle_s)
        return Path(e.path).exists()

    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        keep_mask = list(pool.map(_exists_throttled, entries))
    removed = sum(1 for k in keep_mask if not k)
    _check_prune_safety(removed, len(entries), allow_mass_prune)

    surviving_entries = [e for e, k in zip(entries, keep_mask, strict=False) if k]
    if not surviving_entries and removed:
        _delete_index_files(metadata_file, faiss_file)
        logger.info("Pruned all %d entries — index is now empty", removed)
        return (removed, 0)

    if removed == 0:
        _maybe_migrate_paths(loaded, entries, index_dir, music_dir, already_relative)
        return (0, len(entries))

    surviving_vectors = np.array(
        [loaded.reconstruct(i) for i, k in enumerate(keep_mask) if k],
        dtype=np.float32,
    )
    save_index(surviving_entries, surviving_vectors, index_dir, music_dir=music_dir)
    logger.info("Pruned %d missing tracks (%d remain)", removed, len(surviving_entries))
    return (removed, len(surviving_entries))


def _migrate_flat_index_if_needed(target_dir: Path) -> None:
    """Auto-migrate a pre-0.9 flat index into the named-index layout.

    Pre-0.9 builds wrote ``<index_dir>/vectors.index`` and
    ``<index_dir>/metadata.json`` directly.  Post-0.9 expects
    ``<index_dir>/<name>/...`` instead.  If *target_dir* doesn't have
    the new files but its parent has the old ones, move them across
    in-place so the user doesn't have to re-index after upgrading.

    Silent no-op when the migration doesn't apply.

    Args:
        target_dir: The named-index sub-directory (e.g. ``index/default``).
    """
    target_meta = target_dir / "metadata.json"
    target_vec = target_dir / "vectors.index"
    if target_meta.exists() and target_vec.exists():
        return  # Already migrated or fresh build — nothing to do
    parent = target_dir.parent
    src_meta = parent / "metadata.json"
    src_vec = parent / "vectors.index"
    if not (src_meta.exists() and src_vec.exists()):
        return  # Nothing to migrate
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        src_meta.replace(target_meta)
        src_vec.replace(target_vec)
        # Move sidecars too if present
        for sidecar in (
            "dj_meta.json",
            "dj_meta.db",
            "dj_meta.json.legacy.bak",
            "web_state.json",
            "runtime_state.json",
        ):
            old = parent / sidecar
            if old.exists() and not (target_dir / sidecar).exists():
                old.replace(target_dir / sidecar)
        logger.info(
            "Migrated flat index → %s (named-index layout)",
            target_dir,
        )
    except OSError as exc:
        logger.warning(
            "Auto-migration failed (%s); move manually:\n  mv %s %s\n  mv %s %s",
            exc,
            src_meta,
            target_meta,
            src_vec,
            target_vec,
        )


def load_index(
    index_dir: Path,
    music_dir: Path | None = None,
    path_remap: list[tuple[str, str]] | None = None,
) -> tuple[list[IndexEntry], faiss.IndexFlatIP]:
    """Load the FAISS index and metadata from *index_dir*.

    When *music_dir* is provided, relative stored paths are resolved
    against it and absolute paths optionally remapped via *path_remap*,
    so ``entry.path`` is always an absolute runtime path on return.

    Args:
        index_dir: Directory containing ``vectors.index`` and ``metadata.json``.
        music_dir: Library root for resolving relative paths.
        path_remap: Optional cross-OS prefix swaps for legacy absolute paths.

    Returns:
        A tuple of ``(entries, faiss_index)`` where *entries* is a list of
        :class:`IndexEntry` objects in the same row order as the FAISS index.

    Raises:
        FileNotFoundError: If *index_dir* or its required files are missing.
    """
    # Auto-migrate flat-layout indexes to the named-index layout.
    # Pre-0.9 builds wrote `<index_dir>/vectors.index` directly; the
    # named-index refactor moves them under `<index_dir>/<name>/`.  If
    # the old files are sitting at the parent dir AND the new dir is
    # empty, slide them across so the user doesn't have to re-index.
    _migrate_flat_index_if_needed(index_dir)

    index_file = index_dir / "vectors.index"
    metadata_file = index_dir / "metadata.json"

    if not index_dir.exists():
        raise FileNotFoundError(
            f"Index directory not found: {index_dir}\n"
            f"Expected layout: {index_dir}/vectors.index + {index_dir}/metadata.json.\n"
            "Run 'autodj index' to build the library index first.",
        )
    if not index_file.exists() or not metadata_file.exists():
        raise FileNotFoundError(
            f"Index files missing in {index_dir}.\n"
            f"Expected: {metadata_file} + {index_file}.\n"
            "Note: as of v0.9 each named index lives in its own sub-directory "
            "(<index_dir>/<name>/...).  Run 'autodj list-indexes' to see what's "
            "available, or 'autodj index' to build a fresh one.",
        )

    faiss_index = faiss.read_index(str(index_file))
    raw = json.loads(metadata_file.read_text(encoding="utf-8"))
    entries = [IndexEntry(**row) for row in raw]
    if music_dir is not None or path_remap:
        for e in entries:
            e.path = _resolve_for_runtime(e.path, music_dir, path_remap)
    logger.info("Loaded index with %d tracks from %s", len(entries), index_dir)
    return entries, faiss_index


# ---------------------------------------------------------------------------
# Public build entry point
# ---------------------------------------------------------------------------


def _analyse_one_track(path_str: str) -> tuple[str, Any | None, str | None]:
    """Worker: decode audio, run :func:`analyse_audio`, return ``(path, meta, err)``.

    Top-level (picklable) so a thread / process pool can dispatch it.
    Returns the resolved :class:`autodj.dj_meta.DjMeta` on success, or an
    error string on failure -- never raises.  Empty / unreadable files
    return ``(path, None, None)`` so the caller can skip silently.
    """
    from autodj.dj_meta import analyse_audio

    try:
        audio, sr = _load_audio(Path(path_str))
        if len(audio) == 0:
            return (path_str, None, None)
        return (path_str, analyse_audio(audio, sr), None)
    except Exception as exc:
        return (path_str, None, f"{type(exc).__name__}: {exc}")


def _backfill_dj_meta(
    entries: list[IndexEntry],
    index_dir: Path,
    workers: int | None = None,
    throttle_ms: float = 0.0,
) -> None:
    """Fill in DJ-meta for already-indexed tracks that have no sidecar entry.

    Decodes every track whose cache entry is missing or has
    ``analysed=False``, runs :func:`autodj.dj_meta.analyse_audio` in a
    worker pool, and writes the result.  librosa + soundfile release the
    GIL during their BLAS / decoder calls so a thread pool gets near-
    linear speedup on multi-core hosts.  Flushes every 25 results
    (atomic temp+rename) so a Ctrl+C never loses more than the current
    batch.

    Args:
        entries: Indexed track entries (with absolute paths already
            resolved by the caller).
        index_dir: Active index directory — receives ``dj_meta.json``.
        workers: Thread-pool size.  ``None`` = ``os.cpu_count()`` capped
            at 8 (more workers thrash NAS I/O without speeding the BLAS
            stages).  Pass ``1`` to force serial execution.
        throttle_ms: Optional idle gap (milliseconds) inserted before each
            new task submission / serial step.  ``0`` = no throttle.
            Use to give NAS spindles cool-down breathing room on long
            sustained passes — e.g. ``500`` cuts effective duty cycle
            sharply with little wall-clock cost when paired with a low
            worker count.
    """
    import os
    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    from autodj.dj_meta import get_cache

    cache = get_cache(index_dir)
    if cache is None:
        return
    pending = [e for e in entries if not cache.get(e.path).analysed]
    if not pending:
        print("[AutoDJ] DJ-meta cache already covers every indexed track.")
        return
    if workers is None:
        # NAS-friendly default: two concurrent decoders keep one librosa
        # thread busy in BLAS while the other waits on the next read.
        # Eight workers (the earlier default) saturated NAS spindles and
        # caused drive thermal shutdowns on long sustained passes.
        workers = min(2, max(1, (os.cpu_count() or 2)))
    total = len(pending)
    print(
        f"[AutoDJ] Phase: Analysing — DJ-meta backfill for {total} tracks ({workers} workers).",
        flush=True,
    )
    done = 0

    bar = tqdm(
        total=total,
        desc="Analysing",
        unit="track",
        disable=False,
        dynamic_ncols=True,
    )

    def _record(path_str: str, meta: Any | None, err: str | None) -> None:
        nonlocal done
        if err:
            logger.warning("DJ-meta backfill failed for %s: %s", path_str, err)
        elif meta is not None:
            cache.set(path_str, meta)
            done += 1
            if done % 25 == 0:
                cache.flush()
        with contextlib.suppress(Exception):
            bar.update(1)

    throttle_s = max(0.0, throttle_ms) / 1000.0

    try:
        if workers == 1:
            for entry in pending:
                if throttle_s:
                    _time.sleep(throttle_s)
                _, meta, err = _analyse_one_track(entry.path)
                _record(entry.path, meta, err)
        else:
            # Sliding-window submission — keeps at most ``workers * 2``
            # futures in flight so a Ctrl+C can drain quickly instead of
            # waiting for the executor to shut down 70 000 queued tasks.
            from collections import deque

            inflight: deque = deque()
            it = iter(pending)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                try:
                    for _ in range(workers * 2):
                        try:
                            entry = next(it)
                        except StopIteration:
                            break
                        if throttle_s:
                            _time.sleep(throttle_s)
                        inflight.append(pool.submit(_analyse_one_track, entry.path))

                    while inflight:
                        fut = inflight.popleft()
                        path_str, meta, err = fut.result()
                        _record(path_str, meta, err)
                        try:
                            entry = next(it)
                            if throttle_s:
                                _time.sleep(throttle_s)
                            inflight.append(pool.submit(_analyse_one_track, entry.path))
                        except StopIteration:
                            pass
                except KeyboardInterrupt:
                    with contextlib.suppress(Exception):
                        bar.close()
                    print(
                        "\n[AutoDJ] Ctrl+C — cancelling pending workers, flushing cache...",
                        flush=True,
                    )
                    for f in inflight:
                        f.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    cache.flush(force=True)
                    raise
    except KeyboardInterrupt:
        print(
            f"[AutoDJ] DJ-meta interrupted: {done}/{total} analysed.  "
            "Re-run `autodj analyse` to resume.",
            flush=True,
        )
        return
    finally:
        with contextlib.suppress(Exception):
            bar.close()

    cache.flush(force=True)
    print(f"[AutoDJ] DJ-meta backfill done: {done}/{total} tracks analysed.", flush=True)


def _detect_stale_entries(
    entries: list[IndexEntry],
    reindex_modified_since: float | None = None,
    throttle_ms: float = 0.0,
    stat_workers: int = 8,
) -> tuple[set[str], int]:
    """Find indexed entries whose audio file has been replaced on disk.

    For every entry whose file still exists, compare its mtime against the
    stored ``embedded_at``.  An entry is considered stale (the user replaced
    the file with a different version since indexing) when:

    * ``embedded_at > 0`` and ``file_mtime > embedded_at + 1.0`` (1 s margin
      absorbs filesystem timestamp granularity), OR
    * ``reindex_modified_since`` is set and ``file_mtime > that timestamp``.

    Legacy entries (``embedded_at == 0`` from before the field existed) are
    snapshotted IN PLACE to their current file mtime so future replacements
    are detectable, but are not themselves marked stale (we cannot know
    when they were originally embedded).

    Stat() calls are fanned out across a 32-thread pool because the typical
    case is an NFS/SMB-mounted library where each call costs an RTT.

    Args:
        entries: Existing index entries (with absolute paths already
            resolved by the caller).  Mutated in place: legacy entries get
            their ``embedded_at`` set to the file's current mtime.
        reindex_modified_since: Optional one-shot epoch timestamp.  Any
            entry whose file mtime exceeds this is marked stale regardless
            of ``embedded_at`` — useful as a backfill mechanism for files
            replaced before ``embedded_at`` was being tracked.

    Returns:
        ``(stale_paths, migrated)`` — set of entry paths to drop and the
        number of legacy entries that received a fresh snapshot.
    """

    import time as _time

    throttle_s = max(0.0, throttle_ms) / 1000.0
    pool_size = max(1, stat_workers)

    def _mtime(p: str) -> float | None:
        if throttle_s:
            _time.sleep(throttle_s)
        try:
            return Path(p).stat().st_mtime
        except OSError:
            return None

    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        mtimes = list(pool.map(_mtime, [e.path for e in entries]))

    stale: set[str] = set()
    migrated = 0
    for e, mt in zip(entries, mtimes, strict=False):
        if mt is None:
            continue  # missing file — prune handles it
        if e.embedded_at == 0.0:
            e.embedded_at = mt
            migrated += 1
        elif mt > e.embedded_at + 1.0:
            stale.add(e.path)
        if reindex_modified_since is not None and mt > reindex_modified_since:
            stale.add(e.path)
    return stale, migrated


def _load_existing_index(  # pragma: no cover -- exercised via build_index integration runs
    index_dir: Path,
    music_dir: Path,
    path_remap: list[tuple[str, str]] | None,
    force: bool,
    reindex_modified_since: float | None,
    throttle_ms: float = 0.0,
    stat_workers: int = 8,
) -> tuple[list[IndexEntry], list[np.ndarray], set[str]]:
    """Load existing entries + vectors, drop entries with replaced files."""
    if force:
        return [], [], set()

    try:
        removed, kept = prune_index(
            index_dir,
            music_dir=music_dir,
            path_remap=path_remap,
            throttle_ms=throttle_ms,
            stat_workers=stat_workers,
        )
        if removed:
            print(f"[AutoDJ] Pruned {removed} missing tracks ({kept} remain).")
    except PruneSafetyError as exc:
        print(f"[AutoDJ] Skipping auto-prune (safety check): {exc}")

    metadata_file = index_dir / "metadata.json"
    if not metadata_file.exists():
        return [], [], set()

    raw = json.loads(metadata_file.read_text(encoding="utf-8"))
    existing_entries: list[IndexEntry] = [IndexEntry(**r) for r in raw]
    for e in existing_entries:
        e.path = _resolve_for_runtime(e.path, music_dir, path_remap)

    existing_vectors: list[np.ndarray] = []
    faiss_file = index_dir / "vectors.index"
    if faiss_file.exists() and existing_entries:
        loaded = faiss.read_index(str(faiss_file))
        existing_vectors = [loaded.reconstruct(i) for i in range(loaded.ntotal)]
        logger.info("Incremental mode: %d tracks already indexed", len(existing_entries))

    print(
        f"[AutoDJ] Phase: Stale-check — comparing mtimes for {len(existing_entries)} entries.",
        flush=True,
    )
    stale, migrated = _detect_stale_entries(
        existing_entries,
        reindex_modified_since=reindex_modified_since,
        throttle_ms=throttle_ms,
        stat_workers=stat_workers,
    )
    if migrated:
        logger.info("Snapshotted embedded_at for %d legacy entries", migrated)
    if stale:
        print(
            f"[AutoDJ] Phase: Stale-check — dropping {len(stale)} replaced "
            "tracks; they will be re-embedded.",
            flush=True,
        )
        kept_pairs = [
            (e, v)
            for e, v in zip(existing_entries, existing_vectors, strict=False)
            if e.path not in stale
        ]
        existing_entries = [e for e, _ in kept_pairs]
        existing_vectors = [v for _, v in kept_pairs]

    return existing_entries, existing_vectors, {e.path for e in existing_entries}


def _collect_tracks_to_index(  # pragma: no cover -- exercised via build_index integration runs
    cfg: AutoDJConfig,
) -> list[Track]:
    """Read track list from beets if available, else filesystem scan."""
    tracks: list[Track] = []
    if cfg.library.beets_db and cfg.library.beets_db.exists():
        try:
            tracks = get_all_tracks(cfg.library.beets_db)
            logger.info("Loaded %d tracks from beets library", len(tracks))
        except BeetsNotFoundError:
            logger.warning("Beets DB not found, falling back to filesystem scan")

    if tracks:
        tracks = [
            Track(
                path=_resolve_beets_path(t.path, cfg.library.music_dir),
                title=t.title,
                artist=t.artist,
                album=t.album,
                genre=t.genre,
                bpm=t.bpm,
                year=t.year,
                length=t.length,
            )
            for t in tracks
        ]
        logger.info("Resolved beets paths against music_dir '%s'", cfg.library.music_dir)
        return tracks

    # No beets database — fall back to filesystem scan + ID3/Vorbis tag reads.
    from autodj.audio_meta import read_file_tags

    paths = walk_music_dir(cfg.library.music_dir, cfg.library.supported_formats)
    for p in paths:
        tags = read_file_tags(p)
        tracks.append(
            Track(
                path=p,
                title=tags.title or p.stem,
                artist=tags.artist,
                album=tags.album,
                genre=tags.genre,
                bpm=tags.bpm,
                year=tags.year,
                length=tags.length,
            )
        )
    logger.info("Filesystem scan + ID3 read found %d tracks", len(tracks))
    return tracks


def _embed_new_tracks(  # pragma: no cover -- threaded indexer pipeline
    new_tracks: list[Track],
    wrapper: MuqWrapper,
    workers: int | None,
    checkpoint: Callable[[list[IndexEntry], list[np.ndarray]], None],
    throttle_ms: float = 0.0,
) -> tuple[list[IndexEntry], list[np.ndarray]]:
    """Run the producer/consumer embedding loop with prefetch threadpool."""
    new_entries: list[IndexEntry] = []
    new_vectors: list[np.ndarray] = []

    import os as _os
    import time as _time

    if workers is None:
        workers = min(8, max(1, (_os.cpu_count() or 2)))
    PREFETCH = max(1, workers)
    throttle_s = max(0.0, throttle_ms) / 1000.0
    track_iter = iter(new_tracks)
    pending: deque[tuple[Track, Future]] = deque()

    with ThreadPoolExecutor(max_workers=PREFETCH) as pool:

        def _submit_next() -> None:
            try:
                t = next(track_iter)
                if throttle_s:
                    _time.sleep(throttle_s)
                pending.append((t, pool.submit(_extract_librosa_features, t.path)))
            except StopIteration:
                pass

        for _ in range(PREFETCH):
            _submit_next()

        for _ in tqdm(
            range(len(new_tracks)),
            total=len(new_tracks),
            desc="Indexing",
            unit="track",
            disable=False,
            dynamic_ncols=True,
        ):
            if not pending:
                break
            track, future = pending.popleft()
            _submit_next()

            try:
                librosa_vec, audio, sr, extra_meta = future.result()
                embedding_vec = wrapper.embed_array(audio, sample_rate=sr)
                combined = _combine_features(embedding_vec, librosa_vec)
                if not np.isfinite(combined).all():
                    raise ValueError(
                        "embedding contains NaN or Inf — track may be silent or corrupted"
                    )
                entry = IndexEntry.from_track(track)
                entry.energy = extra_meta["energy"]
                entry.key = extra_meta["key"]
                entry.mode = extra_meta["mode"]
                entry.tempo_confidence = extra_meta["tempo_confidence"]

                from autodj.beets import parse_initial_key as _parse_key

                if getattr(track, "initial_key", ""):
                    parsed = _parse_key(track.initial_key)
                    if parsed is not None:
                        entry.key, entry.mode = parsed
                new_entries.append(entry)
                new_vectors.append(combined)
                checkpoint(new_entries, new_vectors)
            except Exception as exc:
                logger.warning("Skipping %s: %s", track.path, exc)

    return new_entries, new_vectors


def build_index(  # pragma: no cover -- end-to-end pipeline, exercised by integration tests
    cfg: AutoDJConfig,
    wrapper: MuqWrapper,
    limit: int | None,
    force: bool,
    workers: int | None = None,
    reindex_modified_since: float | None = None,
    throttle_ms: float = 0.0,
    stat_workers: int = 8,
) -> None:
    """Build or incrementally update the FAISS index for the music library.

    Reads track list from beets (if configured) or walks the filesystem.
    Skips tracks already present in the existing index unless *force* is set.
    Writes updated index files on completion.

    Args:
        cfg: Full AutoDJ configuration.
        wrapper: A loaded :class:`~autodj.model.MuqWrapper` for embedding.
        limit: Maximum number of *new* tracks to embed. ``None`` means no limit.
        force: If ``True``, ignore any existing index and re-embed everything.
        workers: Audio-loader prefetch pool size.  ``None`` =
            ``min(8, cpu_count())``.  Pass ``1`` to force serial loading.
            More workers hide audio-decode latency behind GPU embed work
            on the indexing host; pin lower on slow NAS-mounted libraries
            to avoid thrashing the SMB pipe.

    Raises:
        FileNotFoundError: If the music directory does not exist and no beets
            database is configured.
    """
    index_dir = cfg.index.active_dir
    index_dir.mkdir(parents=True, exist_ok=True)
    music_dir = cfg.library.music_dir
    path_remap = cfg.library.path_remap

    existing_entries, existing_vectors, existing_paths = _load_existing_index(
        index_dir,
        music_dir,
        path_remap,
        force,
        reindex_modified_since,
        throttle_ms=throttle_ms,
        stat_workers=stat_workers,
    )

    tracks = _collect_tracks_to_index(cfg)

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

    print(
        f"[AutoDJ] Phase: Indexing — {len(new_tracks)} new tracks to embed.",
        flush=True,
    )

    # Save after every successfully embedded track so Ctrl+C never loses
    # progress and a parallel `serve` process can pick up newly-indexed
    # tracks immediately.  Atomic via tmp + os.replace.
    def _checkpoint(new_entries: list[IndexEntry], new_vectors: list[np.ndarray]) -> None:
        if not new_entries:
            return
        all_e = existing_entries + new_entries
        all_v = (
            np.vstack(
                [
                    np.array(existing_vectors, dtype=np.float32),
                    np.array(new_vectors, dtype=np.float32),
                ]
            )
            if existing_vectors
            else np.array(new_vectors, dtype=np.float32)
        )
        save_index(all_e, all_v, index_dir, music_dir=music_dir)
        logger.info("Checkpoint: %d new tracks saved (%d total)", len(new_entries), len(all_e))

    new_entries, new_vectors = _embed_new_tracks(
        new_tracks, wrapper, workers, _checkpoint, throttle_ms=throttle_ms
    )

    # --- merge and save ---
    if not new_entries:  # pragma: no cover -- empty / failed-indexing CLI report path
        if not existing_entries:
            print(
                "[AutoDJ] No tracks could be indexed. "
                "Check that [library] music_dir in config.toml points to the local "
                "mount point of your beets `directory` so relative paths resolve correctly."
            )
            return
        # All new tracks were skipped — nothing changed
        print(
            f"[AutoDJ] No new tracks indexed "
            f"({len(new_tracks)} attempted, all failed). "
            "Check warnings above for details."
        )
        return

    all_entries = existing_entries + new_entries

    if existing_vectors:
        all_vectors = np.vstack(
            [
                np.array(existing_vectors, dtype=np.float32),
                np.array(new_vectors, dtype=np.float32),
            ]
        )
    else:
        all_vectors = np.array(new_vectors, dtype=np.float32)

    save_index(all_entries, all_vectors, index_dir, music_dir=music_dir)
    print(f"[AutoDJ] Index updated: {len(new_entries)} new tracks added, {len(all_entries)} total.")
