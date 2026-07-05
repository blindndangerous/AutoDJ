"""FAISS index builder for the AutoDJ music library.

Walks the music library (via beets or filesystem), extracts MuQ embeddings
and librosa audio features per track, combines them into a single
L2-normalized vector, and stores the result in a FAISS nearest-neighbor index.

Index files written to ``index_dir``:
- ``vectors.index``  — FAISS binary index (``IndexFlatIP``, cosine similarity)
- ``tracks.db``      — SQLite metadata, one row per indexed track

Subsequent runs are **incremental**: tracks already present in
``tracks.db`` are skipped.  Pass ``force=True`` to rebuild from scratch.

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
import logging
import os
import sqlite3
import warnings
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

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

# How often the embed loop rewrites the monolithic FAISS file during a
# long ``autodj index`` run.  The whole file (~290 MB at 70k tracks) is
# rebuilt and rewritten on every flush, so per-track rewrites pummel NAS
# spindles -- ~2.4 TB of writes over a full reindex.  tracks.db still
# flushes every track (cheap UPSERT), so a crash never loses metadata.
# On startup, ``_load_existing_index`` reconciles by trimming any
# metadata rows that lack a matching FAISS vector (max
# FAISS_CHECKPOINT_EVERY-1 trailing rows) -- those tracks simply get
# re-embedded on the next run.
FAISS_CHECKPOINT_EVERY: int = 100

# ---------------------------------------------------------------------------
# Tracks SQLite store
# ---------------------------------------------------------------------------
#
# Indexed track metadata lives in ``index/tracks.db`` (SQLite WAL mode).
# Per-track UPSERTs touch only the dirty pages, so a per-track checkpoint
# during a long reindex run no longer pummels the spindle.
#
# Schema mirrors :class:`IndexEntry` one-to-one.  The implicit ``rowid``
# preserves insertion order so ``load_index`` returns entries in the same
# row order as the FAISS index without needing an explicit ``vec_row``
# column (we always replace the full table when vectors change).

_TRACKS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS tracks (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        path             TEXT NOT NULL UNIQUE,
        title            TEXT NOT NULL DEFAULT '',
        artist           TEXT NOT NULL DEFAULT '',
        album            TEXT NOT NULL DEFAULT '',
        genre            TEXT NOT NULL DEFAULT '',
        bpm              REAL NOT NULL DEFAULT 0,
        year             INTEGER NOT NULL DEFAULT 0,
        length           REAL NOT NULL DEFAULT 0,
        energy           REAL NOT NULL DEFAULT 0,
        key              INTEGER NOT NULL DEFAULT -1,
        mode             INTEGER NOT NULL DEFAULT -1,
        tempo_confidence REAL NOT NULL DEFAULT 0,
        embedded_at      REAL NOT NULL DEFAULT 0
    );
"""

_TRACKS_INSERT_SQL = (
    "INSERT INTO tracks "
    "(path, title, artist, album, genre, bpm, year, length, energy, "
    "key, mode, tempo_confidence, embedded_at) "
    "VALUES (:path, :title, :artist, :album, :genre, :bpm, :year, "
    ":length, :energy, :key, :mode, :tempo_confidence, :embedded_at)"
)

_TRACKS_SELECT_SQL = (
    "SELECT path, title, artist, album, genre, bpm, year, length, energy, "
    "key, mode, tempo_confidence, embedded_at FROM tracks ORDER BY id ASC"
)


def _tracks_db_path(index_dir: Path) -> Path:
    """Return the SQLite tracks-db path for *index_dir*."""
    return index_dir / "tracks.db"


def _open_tracks_db(index_dir: Path) -> sqlite3.Connection:
    """Open (creating if needed) the tracks SQLite store for *index_dir*.

    Uses WAL journal mode + NORMAL sync — same trade-off as DjMetaCache:
    the index is re-derivable from the library if a crash corrupts an
    uncommitted write, so we prefer the throughput.
    """
    index_dir.mkdir(parents=True, exist_ok=True)
    db_path = _tracks_db_path(index_dir)
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.executescript(_TRACKS_SCHEMA)
    with contextlib.suppress(sqlite3.DatabaseError):
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _entry_to_row(entry: IndexEntry, music_dir: Path | None) -> dict:
    """Convert an :class:`IndexEntry` to a SQLite-bound row dict.

    Stored ``path`` is relativised against *music_dir* the same way the
    legacy JSON sidecar did so that an index built on one host stays
    portable to any other host that mounts the library at a different
    absolute path.
    """
    return {
        "path": _relativize_for_storage(entry.path, music_dir),
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
    }


def _row_to_entry(row: tuple) -> IndexEntry:
    """Build an :class:`IndexEntry` from a SELECT row tuple."""
    return IndexEntry(
        path=row[0],
        title=row[1] or "",
        artist=row[2] or "",
        album=row[3] or "",
        genre=row[4] or "",
        bpm=float(row[5] or 0.0),
        year=int(row[6] or 0),
        length=float(row[7] or 0.0),
        energy=float(row[8] or 0.0),
        key=int(row[9] if row[9] is not None else -1),
        mode=int(row[10] if row[10] is not None else -1),
        tempo_confidence=float(row[11] or 0.0),
        embedded_at=float(row[12] or 0.0),
    )


def _replace_tracks_rows(
    conn: sqlite3.Connection,
    entries: list[IndexEntry],
    music_dir: Path | None,
) -> None:
    """Atomically replace the entire ``tracks`` table contents.

    save_index always receives the full entries list, so we DELETE then
    INSERT inside a single transaction.  At 70k tracks this still writes
    less than the legacy whole-file JSON rewrite because SQLite WAL pages
    are smaller and rows that did not change get the same on-disk page.
    """
    rows = [_entry_to_row(e, music_dir) for e in entries]
    with conn:
        conn.execute("DELETE FROM tracks")
        if rows:
            conn.executemany(_TRACKS_INSERT_SQL, rows)


def _load_tracks_rows(conn: sqlite3.Connection) -> list[IndexEntry]:
    """SELECT every row from ``tracks`` in insertion order."""
    cur = conn.execute(_TRACKS_SELECT_SQL)
    return [_row_to_entry(r) for r in cur.fetchall()]


def _relativize_for_storage(abs_path: str, music_dir: Path | None) -> str:
    """Convert an absolute path to a forward-slashed string for ``tracks.db``.

    If *abs_path* lives under *music_dir*, the returned string is RELATIVE
    to *music_dir* — making the index portable across machines that mount
    the library at a different absolute path.  Otherwise the absolute path
    is returned (forward-slashed for cross-OS readability).

    Args:
        abs_path: Absolute path string from an :class:`IndexEntry` at runtime.
        music_dir: Library root.  ``None`` disables relativization.

    Returns:
        A forward-slashed path string suitable for SQLite storage.
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
        stored: Path string as written in ``tracks.db`` (relative,
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
        else:  # pragma: no cover — current major/minor templates make this LP-infeasible (proved unreachable)
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


def _save_vectors(vectors: np.ndarray, index_dir: Path) -> None:
    """Write only ``vectors.index`` atomically (tmp+rename).

    Used by the per-checkpoint pipeline so we can flush the SQLite tracks
    table on every track (cheap) but only rebuild + rewrite the FAISS
    file every ``checkpoint_every_faiss`` tracks (expensive).
    """
    index_dir.mkdir(parents=True, exist_ok=True)
    vectors_final = index_dir / "vectors.index"
    vectors_tmp = index_dir / "vectors.index.tmp"
    try:
        faiss_index = build_faiss_index(vectors)
        _write_faiss_chunked(faiss_index, vectors_tmp)
        os.replace(vectors_tmp, vectors_final)
    except Exception:
        if vectors_tmp.exists():
            with contextlib.suppress(OSError):
                vectors_tmp.unlink()
        raise


def _save_tracks_metadata(
    entries: list[IndexEntry],
    index_dir: Path,
    music_dir: Path | None,
) -> None:
    """Replace the ``tracks.db`` rows in one transaction.

    Per-checkpoint cost stays O(rows) (cheap UPSERT) instead of paying
    the FAISS whole-file rewrite on every track.
    """
    index_dir.mkdir(parents=True, exist_ok=True)
    conn = _open_tracks_db(index_dir)
    try:
        _replace_tracks_rows(conn, entries, music_dir)
    finally:
        conn.close()


def save_index(
    entries: list[IndexEntry],
    vectors: np.ndarray,
    index_dir: Path,
    music_dir: Path | None = None,
) -> None:
    """Write the FAISS index and tracks DB to *index_dir* atomically.

    Files written:
    - ``vectors.index``  — FAISS binary file
    - ``tracks.db``      — SQLite metadata (one row per track, in row order
      matching the FAISS index)

    The FAISS file is written to a ``*.tmp`` sibling and renamed over the
    original.  The tracks DB is updated inside a single SQLite transaction
    (``DELETE FROM tracks`` then bulk ``INSERT``), so a crash mid-write
    leaves the existing on-disk DB intact instead of corrupting it.  Same
    crash-safety guarantee as the older JSON sidecar, with no whole-file
    rewrite.

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
    _save_vectors(vectors, index_dir)
    _save_tracks_metadata(entries, index_dir, music_dir)
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

    Walks ``tracks.db``, looks up each track in *beets_db* by path, and
    overwrites a curated set of fields when beets has values for them.
    This is the upgrade path for users who indexed without beets and
    later add a ``library.db`` — they get title / artist / album /
    genre / bpm / year / length backfill plus key/mode from
    ``initial_key`` if the keyfinder plugin ran.

    No re-embedding — only metadata.  Vectors are not touched.

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
        index_dir: Directory containing ``vectors.index`` + ``tracks.db``.
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

    db_path = _tracks_db_path(index_dir)
    faiss_file = index_dir / "vectors.index"
    if not db_path.exists() or not faiss_file.exists():
        return (0, 0)

    conn_db = _open_tracks_db(index_dir)
    try:
        entries = _load_tracks_rows(conn_db)
    finally:
        conn_db.close()
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

    # Metadata-only update — no FAISS rewrite needed (vectors unchanged).
    # SQLite UPSERT per affected row inside a single transaction is far
    # cheaper than the legacy "reconstruct every vector + full save_index"
    # round-trip, which paid O(N) vector reconstruct cost just to re-emit
    # JSON.  Bonus: enrich no longer needs vectors.index on disk.
    conn_db = _open_tracks_db(index_dir)
    try:
        _replace_tracks_rows(conn_db, entries, music_dir)
    finally:
        conn_db.close()
    logger.info("Enrich: updated %d/%d tracks", updated, len(entries))
    return (updated, len(entries))


def _is_relative_storage(paths: list[str]) -> bool:
    """True when every stored path string is already in relative form."""
    return all(not (p.startswith("/") or (len(p) >= 2 and p[1] == ":") or "\\" in p) for p in paths)


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


def _delete_index_files(index_dir: Path) -> None:
    """Remove the index files (called when prune empties the library).

    Cleans up ``vectors.index``, ``tracks.db``, and the SQLite WAL / SHM
    sidecars so the next index run starts from a clean slate.
    """
    for name in (
        "vectors.index",
        "tracks.db",
        "tracks.db-wal",
        "tracks.db-shm",
    ):
        with contextlib.suppress(FileNotFoundError, OSError):
            (index_dir / name).unlink()


def _maybe_migrate_paths(
    loaded: faiss.IndexFlatIP,
    entries: list[IndexEntry],
    index_dir: Path,
    music_dir: Path | None,
    already_relative: bool,
) -> None:
    """Re-save tracks DB in relative path form when storage is still absolute.

    With the SQLite tracks store we can rewrite just the rows we care
    about; no need to reconstruct vectors or touch the FAISS file.  The
    legacy code path used save_index() here, which required a full
    O(N) vector reconstruct just to flip path strings — wasted work.
    """
    if music_dir is None or already_relative:
        return
    conn = _open_tracks_db(index_dir)
    try:
        _replace_tracks_rows(conn, entries, music_dir)
    finally:
        conn.close()


def prune_index(
    index_dir: Path,
    music_dir: Path | None = None,
    path_remap: list[tuple[str, str]] | None = None,
    allow_mass_prune: bool = False,
    throttle_ms: float = 0.0,
    stat_workers: int = 8,
) -> tuple[int, int]:
    """Remove index entries whose audio files no longer exist on disk.

    Loads ``tracks.db`` and ``vectors.index``, resolves each stored
    path against *music_dir* + *path_remap*, drops every row whose audio
    file is missing, and rewrites both files via :func:`save_index`.  If
    every track is gone, the index files are deleted instead.

    Always rewrites ``tracks.db`` if any rows were stored as absolute
    paths under *music_dir* — converting them to portable relative paths.
    No-op when no index exists or no rewrite is needed.

    Safety: if more than :data:`PRUNE_SAFETY_THRESHOLD` of the entries
    would be removed, raises :class:`PruneSafetyError` instead of touching
    the index.  Override with ``allow_mass_prune=True`` (e.g. after
    confirming you really did delete most of your library).

    Args:
        index_dir: Directory containing ``vectors.index`` and ``tracks.db``.
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
    db_path = _tracks_db_path(index_dir)
    faiss_file = index_dir / "vectors.index"
    if not db_path.exists() or not faiss_file.exists():
        return (0, 0)

    conn = _open_tracks_db(index_dir)
    try:
        entries = _load_tracks_rows(conn)
    finally:
        conn.close()
    already_relative = music_dir is not None and _is_relative_storage([e.path for e in entries])
    for e in entries:
        e.path = _resolve_for_runtime(e.path, music_dir, path_remap)
    # faiss-cpu >=1.14 stubs type read_index as the base Index; we only ever
    # persist an IndexFlatIP, so narrow back for the typed signatures below.
    loaded = cast("faiss.IndexFlatIP", faiss.read_index(str(faiss_file)))

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
        keep_mask = list(
            tqdm(
                pool.map(_exists_throttled, entries),
                total=len(entries),
                desc="Pruning",
                unit="file",
                dynamic_ncols=True,
            )
        )
    removed = sum(1 for k in keep_mask if not k)
    _check_prune_safety(removed, len(entries), allow_mass_prune)

    surviving_entries = [e for e, k in zip(entries, keep_mask, strict=False) if k]
    if not surviving_entries and removed:
        _delete_index_files(index_dir)
        logger.info("Pruned all %d entries — index is now empty", removed)
        return (removed, 0)

    if removed == 0:
        _maybe_migrate_paths(loaded, entries, index_dir, music_dir, already_relative)
        return (0, len(entries))

    # Batch reconstruct: one FAISS call returns the whole (N, dim) array,
    # then we slice with a numpy mask.  ~70 000 per-entry reconstruct()
    # calls were the dominant cost of prune on large libraries.
    all_vectors = loaded.reconstruct_n(0, loaded.ntotal)
    mask = np.fromiter(keep_mask, dtype=bool, count=len(keep_mask))
    surviving_vectors = np.asarray(all_vectors[mask], dtype=np.float32)
    save_index(surviving_entries, surviving_vectors, index_dir, music_dir=music_dir)
    logger.info("Pruned %d missing tracks (%d remain)", removed, len(surviving_entries))
    return (removed, len(surviving_entries))


def _migrate_flat_index_if_needed(target_dir: Path) -> None:
    """Auto-migrate a pre-0.9 flat index into the named-index layout.

    Pre-0.9 builds wrote ``<index_dir>/vectors.index`` and
    ``<index_dir>/tracks.db`` directly.  Post-0.9 expects
    ``<index_dir>/<name>/...`` instead.  If *target_dir* doesn't have
    the new files but its parent has the old ones, move them across
    in-place so the user doesn't have to re-index after upgrading.

    Silent no-op when the migration doesn't apply.

    Args:
        target_dir: The named-index sub-directory (e.g. ``index/default``).
    """
    target_vec = target_dir / "vectors.index"
    target_db = target_dir / "tracks.db"
    if target_vec.exists() and target_db.exists():
        return  # Already migrated or fresh build — nothing to do
    parent = target_dir.parent
    src_vec = parent / "vectors.index"
    src_db = parent / "tracks.db"
    if not src_vec.exists() or not src_db.exists():
        return  # Nothing to migrate
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        src_vec.replace(target_vec)
        src_db.replace(target_db)
        # Move sidecars too if present
        for sidecar in (
            "dj_meta.db",
            "tracks.db-wal",
            "tracks.db-shm",
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
            "Auto-migration failed (%s); move manually:\n  mv %s %s\n  (and tracks.db alongside)",
            exc,
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
        index_dir: Directory containing ``vectors.index`` and ``tracks.db``.
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
    db_path = _tracks_db_path(index_dir)

    if not index_dir.exists():
        raise FileNotFoundError(
            f"Index directory not found: {index_dir}\n"
            f"Expected layout: {index_dir}/vectors.index + {index_dir}/tracks.db.\n"
            "Run 'autodj index' to build the library index first.",
        )
    if not index_file.exists() or not db_path.exists():
        raise FileNotFoundError(
            f"Index files missing in {index_dir}.\n"
            f"Expected: {db_path} + {index_file}.\n"
            "Note: each named index lives in its own sub-directory "
            "(<index_dir>/<name>/...).  Track metadata lives in tracks.db "
            "(SQLite).  Run 'autodj list-indexes' to see what's available, "
            "or 'autodj index' to build a fresh one.",
        )

    faiss_index = cast("faiss.IndexFlatIP", faiss.read_index(str(index_file)))
    conn = _open_tracks_db(index_dir)
    try:
        entries = _load_tracks_rows(conn)
    finally:
        conn.close()
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
    music_dir: Path | None = None,
    path_remap: list[tuple[str, str]] | None = None,
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
        index_dir: Active index directory — receives ``dj_meta.db``.
        workers: Thread-pool size.  ``None`` = ``os.cpu_count()`` capped
            at 8 (more workers thrash NAS I/O without speeding the BLAS
            stages).  Pass ``1`` to force serial execution.
        throttle_ms: Optional idle gap (milliseconds) inserted before each
            new task submission / serial step.  ``0`` = no throttle.
            Use to give NAS spindles cool-down breathing room on long
            sustained passes — e.g. ``500`` cuts effective duty cycle
            sharply with little wall-clock cost when paired with a low
            worker count.
        music_dir: Library root used to store DJ-meta keys portably.
        path_remap: Optional absolute-prefix swaps for legacy cache rows.
    """
    import os
    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    from autodj.dj_meta import get_cache

    cache = get_cache(index_dir, music_dir=music_dir, path_remap=path_remap)
    if cache is None:
        return
    prune_to_paths = getattr(cache, "prune_to_paths", None)
    if callable(prune_to_paths):
        removed_stale = prune_to_paths({e.path for e in entries})
        if removed_stale:
            print(f"[AutoDJ] DJ-meta cache pruned {removed_stale} stale entries.", flush=True)
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

    # Adaptive throttle state.  We track the wall time between successive
    # task completions over a 20-sample rolling window.  The first 10
    # samples establish a baseline (per-completion cadence under healthy
    # I/O).  After that, if the rolling median grows past 2x baseline we
    # interpret it as drives thermally throttling or otherwise saturated,
    # and insert a proportional cool-down sleep before each new submission.
    # The throttle relaxes automatically when cadence recovers.  Manual
    # ``throttle_ms`` (if any) is treated as a floor.
    import statistics as _stats

    _intervals: deque[float] = deque(maxlen=20)
    _baseline: list[float | None] = [None]  # nonlocal-via-list trick
    _adaptive_s: list[float] = [0.0]
    _last_completion: list[float] = [0.0]
    _last_log_done: list[int] = [0]

    def _update_throttle() -> (
        None
    ):  # pragma: no cover - NAS thermal-throttle controller, exercised on real hardware only
        now = _time.monotonic()
        if _last_completion[0] > 0:
            _intervals.append(now - _last_completion[0])
        _last_completion[0] = now
        if _baseline[0] is None:
            if len(_intervals) >= 10:
                _baseline[0] = _stats.median(_intervals)
            return
        if len(_intervals) < 5:
            return
        current = _stats.median(_intervals)
        baseline = _baseline[0]
        if baseline <= 0:
            return
        ratio = current / baseline
        prev = _adaptive_s[0]
        if ratio > 2.0:
            # Proportional cool-down: aim to give drives roughly the
            # extra-latency budget back as idle time.  Capped at 5 s so
            # one transient stall can't park the whole run.
            target = min(5.0, (ratio - 1.5) * baseline)
            _adaptive_s[0] = max(prev, target)
        elif ratio < 1.3 and prev > 0:
            _adaptive_s[0] = prev * 0.5 if prev > 0.05 else 0.0
        # Log throttle transitions sparsely so the user can see what the
        # adaptive controller is doing without spam.
        if abs(_adaptive_s[0] - prev) > 0.05 and (done - _last_log_done[0]) >= 25:
            _last_log_done[0] = done
            logger.info(
                "Adaptive throttle: median %.2fs/track (baseline %.2fs, ratio %.2f) -> sleep %.2fs",
                current,
                baseline,
                ratio,
                _adaptive_s[0],
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
        _update_throttle()

    manual_throttle_s = max(0.0, throttle_ms) / 1000.0

    def _sleep_before_submit() -> None:
        gap = max(manual_throttle_s, _adaptive_s[0])
        if gap > 0:
            _time.sleep(gap)

    try:
        if workers == 1:
            for entry in pending:
                _sleep_before_submit()
                _, meta, err = _analyse_one_track(entry.path)
                _record(entry.path, meta, err)
        else:
            # Sliding-window submission — keeps at most ``workers * 2``
            # futures in flight so a Ctrl+C can drain quickly instead of
            # waiting for the executor to shut down 70 000 queued tasks.
            inflight: deque = deque()
            it = iter(pending)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                try:
                    for _ in range(workers * 2):
                        try:
                            entry = next(it)
                        except StopIteration:
                            break
                        _sleep_before_submit()
                        inflight.append(pool.submit(_analyse_one_track, entry.path))

                    while inflight:
                        fut = inflight.popleft()
                        path_str, meta, err = fut.result()
                        _record(path_str, meta, err)
                        try:
                            entry = next(it)
                            _sleep_before_submit()
                            inflight.append(pool.submit(_analyse_one_track, entry.path))
                        except StopIteration:
                            pass
                except KeyboardInterrupt:  # pragma: no cover - Ctrl+C drain path
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
    except KeyboardInterrupt:  # pragma: no cover - Ctrl+C drain path
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


def _stat_mtimes(
    entries: list[IndexEntry],
    *,
    throttle_ms: float = 0.0,
    stat_workers: int = 8,
    desc: str = "Stat",
) -> list[float | None]:
    """Fan out stat() across *entries*, return mtime-or-None per entry.

    None means the file is missing (OSError on stat).  One network RTT per
    file on NAS; the thread pool pipelines them.  Used by both the
    standalone prune path and the fused prune+stale-check in
    ``_load_existing_index``.
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
        return list(
            tqdm(
                pool.map(_mtime, [e.path for e in entries]),
                total=len(entries),
                desc=desc,
                unit="file",
                dynamic_ncols=True,
            )
        )


def _detect_stale_entries(
    entries: list[IndexEntry],
    reindex_modified_since: float | None = None,
    throttle_ms: float = 0.0,
    stat_workers: int = 8,
    mtimes: list[float | None] | None = None,
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

    if mtimes is None:
        mtimes = _stat_mtimes(
            entries,
            throttle_ms=throttle_ms,
            stat_workers=stat_workers,
            desc="Stale-check",
        )

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
    """Load existing entries + vectors, drop missing and replaced files.

    Fused single-pass: one stat() per entry returns both existence (None
    means missing -> prune) and mtime (compared against ``embedded_at``
    -> stale).  Halves NAS RTT vs. the legacy two-pass design that ran
    ``prune_index`` then ``_detect_stale_entries`` back-to-back.
    """
    if force:
        return [], [], set()

    db_path = _tracks_db_path(index_dir)
    if not db_path.exists():
        return [], [], set()

    conn = _open_tracks_db(index_dir)
    try:
        existing_entries: list[IndexEntry] = _load_tracks_rows(conn)
    finally:
        conn.close()
    already_relative = _is_relative_storage([e.path for e in existing_entries])
    for e in existing_entries:
        e.path = _resolve_for_runtime(e.path, music_dir, path_remap)

    existing_vectors: list[np.ndarray] = []
    faiss_file = index_dir / "vectors.index"
    if faiss_file.exists() and existing_entries:
        # faiss-cpu >=1.14 stubs type read_index as the base Index; we only ever
        # persist an IndexFlatIP, so narrow back for the typed signatures below.
        loaded = cast("faiss.IndexFlatIP", faiss.read_index(str(faiss_file)))
        # Reconcile: tracks.db UPSERTs every track but FAISS only flushes
        # every FAISS_CHECKPOINT_EVERY tracks (perf optimisation), so a
        # crash between the two writes leaves metadata rows without
        # matching FAISS vectors.  Drop the unmatched tail so those
        # tracks get re-embedded on this run.
        if loaded.ntotal < len(existing_entries):
            drop = len(existing_entries) - loaded.ntotal
            logger.warning(
                "Recovered from partial checkpoint: tracks.db had %d rows "
                "but vectors.index has %d -- dropping last %d entries so "
                "they get re-embedded.",
                len(existing_entries),
                loaded.ntotal,
                drop,
            )
            existing_entries = existing_entries[: loaded.ntotal]
            _save_tracks_metadata(existing_entries, index_dir, music_dir)
        # Batch reconstruct in one FAISS call — per-entry reconstruct()
        # at 70k tracks dominated incremental-index startup time.
        all_vectors = loaded.reconstruct_n(0, loaded.ntotal)
        # If FAISS has more vectors than tracks.db (shouldn't happen
        # given metadata-first ordering, but defend anyway), keep only
        # the prefix that lines up with metadata.
        if loaded.ntotal > len(existing_entries):
            logger.warning(
                "vectors.index has %d rows but tracks.db has only %d -- "
                "truncating FAISS to match metadata.",
                loaded.ntotal,
                len(existing_entries),
            )
            all_vectors = all_vectors[: len(existing_entries)]
        existing_vectors = [np.asarray(row, dtype=np.float32) for row in all_vectors]
        logger.info("Incremental mode: %d tracks already indexed", len(existing_entries))

    print(
        f"[AutoDJ] Phase: Prune + stale-check — stat'ing {len(existing_entries)} files.",
        flush=True,
    )
    mtimes = _stat_mtimes(
        existing_entries,
        throttle_ms=throttle_ms,
        stat_workers=stat_workers,
        desc="Prune+stale",
    )
    missing_paths = {e.path for e, mt in zip(existing_entries, mtimes, strict=False) if mt is None}
    try:
        _check_prune_safety(len(missing_paths), len(existing_entries), allow_mass_prune=False)
    except PruneSafetyError as exc:
        print(f"[AutoDJ] Skipping auto-prune (safety check): {exc}")
        missing_paths = set()  # keep everything; safety failure means user config is wrong

    stale, migrated = _detect_stale_entries(
        existing_entries,
        reindex_modified_since=reindex_modified_since,
        mtimes=mtimes,
    )
    if migrated:
        logger.info("Snapshotted embedded_at for %d legacy entries", migrated)

    drop_paths = missing_paths | stale
    if drop_paths:
        if missing_paths:
            print(
                f"[AutoDJ] Pruned {len(missing_paths)} missing tracks "
                f"({len(existing_entries) - len(missing_paths)} remain).",
                flush=True,
            )
        if stale:
            print(
                f"[AutoDJ] Stale-check — dropping {len(stale)} replaced "
                "tracks; they will be re-embedded.",
                flush=True,
            )
        kept_pairs = [
            (e, v)
            for e, v in zip(existing_entries, existing_vectors, strict=False)
            if e.path not in drop_paths
        ]
        existing_entries = [e for e, _ in kept_pairs]
        existing_vectors = [v for _, v in kept_pairs]
        if missing_paths:
            # Persist the prune (mirrors what standalone prune_index does).
            # Stale-only drops don't need this -- those entries will get
            # re-embedded and the indexer's normal checkpoint flow rewrites
            # the files.
            if not existing_entries:
                _delete_index_files(index_dir)
            else:
                vectors_arr = np.asarray(np.stack(existing_vectors), dtype=np.float32)
                save_index(existing_entries, vectors_arr, index_dir, music_dir=music_dir)
    elif not already_relative and existing_entries:
        # Legacy absolute-path storage detected; rewrite tracks.db in
        # portable relative form without touching FAISS.
        _save_tracks_metadata(existing_entries, index_dir, music_dir)

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

    # Per-track checkpoint policy:
    #   * tracks.db UPSERT every track -- cheap, durable, lets a parallel
    #     ``serve`` see new entries immediately.
    #   * FAISS file rewrite every FAISS_CHECKPOINT_EVERY tracks (or at
    #     loop end via the final save_index call) -- the monolithic file
    #     is expensive to rewrite, so we lump those writes together.
    # Order: metadata first, then FAISS.  A crash between the two leaves
    # tracks.db ahead of vectors.index; _load_existing_index trims the
    # mismatched tail on next startup so those tracks get re-embedded.
    cp_counter = [0]

    def _checkpoint(new_entries: list[IndexEntry], new_vectors: list[np.ndarray]) -> None:
        if not new_entries:
            return
        cp_counter[0] += 1
        all_e = existing_entries + new_entries
        # Metadata always: O(rows) UPSERT, no whole-file rewrite.
        _save_tracks_metadata(all_e, index_dir, music_dir)
        # FAISS only every N tracks (or at the very last entry of the run).
        last_track = cp_counter[0] == len(new_tracks)
        if last_track or cp_counter[0] % FAISS_CHECKPOINT_EVERY == 0:
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
            _save_vectors(all_v, index_dir)
            logger.info(
                "Checkpoint: %d new tracks saved (%d total) -- FAISS flushed",
                len(new_entries),
                len(all_e),
            )

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
