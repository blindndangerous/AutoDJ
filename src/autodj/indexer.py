"""Audio analysis, track records and FAISS helpers for the music library.

Current analysis writes vectors to library.db. Published-generation readers and
serialization helpers remain here only for explicit migration of older libraries.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import sqlite3
import time
import warnings
from collections import deque
from collections.abc import Callable, Collection, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import faiss
import librosa
import numpy as np

from autodj.beets import Track
from autodj.config import AutoDJConfig
from autodj.index_manifest import (
    REBUILD_COMMAND,
    IndexConsistencyError,
    IndexManifest,
    UnsupportedIndexError,
    is_absolute_storage,
    publication_lock,
    publish_generation,
    relative_storage_path,
    require_manifest,
    sha256_file,
)
from autodj.progress import TrackProgress
from autodj.sqlite_utils import readonly_uri

# soundfile is an optional extra, so the lighter commands (`enrich`,
# `prune`, `stats`) work on minimal installs that omit it.
# _load_audio() guards against None at runtime so minimal commands can still
# import this module without the playback/indexing extra.
try:
    import soundfile as _sf_mod

    sf: Any = _sf_mod
except ImportError:  # pragma: no cover
    sf = None


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

# Per-track work (embedding, DJ analysis) logs one progress line per this
# many tracks; file checks log one per _STAT_PROGRESS_EVERY files.
PROGRESS_EVERY: int = 25
_STAT_PROGRESS_EVERY: int = 5000


def _log_progress(phase: str, done: int, total: int, every: int = PROGRESS_EVERY) -> None:
    """Log ``phase: done of total`` every *every* items and at the end."""
    if done == total or done % every == 0:
        logger.info("%s: %d of %d", phase, done, total)


# ---------------------------------------------------------------------------
# Tracks SQLite store
# ---------------------------------------------------------------------------
#
# Schema mirrors :class:`IndexEntry` one-to-one.  ``vec_row`` is the stable
# identity linking each metadata row to its corresponding FAISS vector.

_TRACKS_SCHEMA = """
    CREATE TABLE tracks (
        vec_row INTEGER NOT NULL UNIQUE,
        path TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL DEFAULT '', artist TEXT NOT NULL DEFAULT '',
        album TEXT NOT NULL DEFAULT '', genre TEXT NOT NULL DEFAULT '',
        bpm REAL NOT NULL DEFAULT 0, year INTEGER NOT NULL DEFAULT 0,
        length REAL NOT NULL DEFAULT 0, energy REAL NOT NULL DEFAULT 0,
        key INTEGER NOT NULL DEFAULT -1, mode INTEGER NOT NULL DEFAULT -1,
        tempo_confidence REAL NOT NULL DEFAULT 0,
        embedded_at REAL NOT NULL DEFAULT 0,
        size INTEGER NOT NULL DEFAULT 0,
        fingerprint TEXT NOT NULL DEFAULT ''
    );
"""

_TRACKS_INSERT_SQL = (
    "INSERT INTO tracks (vec_row, path, title, artist, album, genre, bpm, year, "
    "length, energy, key, mode, tempo_confidence, embedded_at, size, fingerprint) "
    "VALUES (:vec_row, :path, :title, :artist, :album, :genre, :bpm, :year, "
    ":length, :energy, :key, :mode, :tempo_confidence, :embedded_at, :size, :fingerprint)"
)

_TRACKS_SELECT_SQL = (
    "SELECT path, title, artist, album, genre, bpm, year, length, energy, "
    "key, mode, tempo_confidence, embedded_at, size, fingerprint "
    "FROM tracks ORDER BY vec_row ASC"
)


def _entry_to_row(entry: IndexEntry, music_dir: Path | None, vec_row: int) -> dict[str, object]:
    """Convert an :class:`IndexEntry` to a SQLite-bound row dict.

    Stored ``path`` is relative to *music_dir*, so an index built on one host
    works on any host that mounts the library somewhere else.
    """
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
        size=int(row[13] or 0),
        fingerprint=row[14] or "",
    )


def _write_tracks_file(rows: list[dict[str, object]], path: Path) -> None:
    """Create a new SQLite tracks database at *path* holding *rows*."""
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.executescript(_TRACKS_SCHEMA)
        conn.execute("BEGIN")
        conn.executemany(_TRACKS_INSERT_SQL, rows)
        conn.execute("COMMIT")
    finally:
        conn.close()


def _load_tracks_rows(conn: sqlite3.Connection) -> list[IndexEntry]:
    """SELECT every row from ``tracks`` in FAISS vector order."""
    cur = conn.execute(_TRACKS_SELECT_SQL)
    return [_row_to_entry(r) for r in cur.fetchall()]


def _resolve_beets_path(path: Path, music_dir: Path) -> Path:
    """Resolve a beets-stored path to an absolute local path.

    Beets stores track paths relative to its library ``directory``, or
    absolute on setups without its ``relative_path`` option.  Relative paths
    are joined to *music_dir*, the local mount point of that ``directory``.
    Absolute paths are returned unchanged; the indexer skips any that fall
    outside *music_dir*.

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


def source_mtime(path: str | Path) -> float:
    """Return the file's modification time, or the local clock when absent.

    Stale detection compares a stored stamp against ``st_mtime``, so both
    sides have to come from the machine that owns the file.

    The ``OSError`` fallback covers a file that vanished or became unreadable
    between the scan and this call.  The local clock is the wrong clock, but
    the alternatives are worse: ``0.0`` means "never stamped", which
    a scan treats the file as changed on every run, and raising would
    abort a whole indexing run over one bad file.  A local-clock stamp at worst
    makes that single track look stale once.

    Args:
        path: Path to the source audio file.

    Returns:
        The file's ``st_mtime``, or the local wall clock if it cannot be read.
    """
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return time.time()


#: Bytes read from the middle of a file for its fingerprint.
FINGERPRINT_BYTES: int = 64 * 1024


def file_fingerprint(path: str | Path) -> tuple[int, str]:
    """Return ``(size, fingerprint)`` identifying a file's bytes, wherever it lives.

    The fingerprint is the SHA-256 hex digest of up to
    :data:`FINGERPRINT_BYTES` read from the middle of the file, so it costs
    one small read however long the track is, and a move or rename keeps it.
    Retagging a file changes its size or the bytes at its middle, which makes
    it a different file here.

    Args:
        path: Path to the source audio file.

    Returns:
        ``(size, hex digest)``, or ``(0, "")`` when the file cannot be read;
        an unknown identity never matches anything.
    """
    try:
        with Path(path).open("rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            fh.seek(max(0, size // 2 - FINGERPRINT_BYTES // 2))
            return size, hashlib.sha256(fh.read(FINGERPRINT_BYTES)).hexdigest()
    except OSError:
        return 0, ""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

#: Mean RMS (about -80 dBFS) below which a track counts as silent: pregap
#: filler and "[silence]" tracks.  A digitally silent file measures 0.0 and
#: dithered silence stays far under it, while the quietest music sits
#: around 0.001 (-60 dBFS), so quiet music is never taken for silence.
SILENT_ENERGY: float = 1e-4


@dataclass
class IndexEntry:
    """Serialisable metadata record stored alongside each FAISS vector.

    Attributes:
        path: String path to the audio file.
        title: Track title.
        artist: Artist name.
        album: Album name.
        genre: Genre string.
        bpm: Beats per minute (from beets or estimated by librosa).
        year: Release year.
        length: Track duration in seconds.
        energy: Mean RMS loudness of the whole track, measured when it is
            indexed.  Every indexed track has one: a track whose analysis
            fails is not indexed.  Below :data:`SILENT_ENERGY` the track is
            silent (see :attr:`is_silent`).
        key: Chromatic key 0–11 (C=0, C#=1, …, B=11), -1 = unknown.
        mode: 1 = major, 0 = minor, -1 = unknown.
        tempo_confidence: Librosa beat-tracking confidence 0.0–1.0,
            0.0 = unknown (analysis failed).
        embedded_at: The source file's mtime when this entry was embedded.
            Used to detect replaced files: if ``file.mtime > embedded_at +
            1`` on the next ``index`` run the entry is dropped and
            re-embedded.
            ``0.0`` = never stamped; the next ``index`` run re-embeds it.
        size: The source file's size in bytes when it was embedded, 0 = unknown.
        fingerprint: :func:`file_fingerprint` of the file when it was embedded,
            ``""`` = unknown.  With *size* it identifies the file's bytes, so
            ``autodj index`` can tell a moved file from a new one.
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
    size: int = 0
    fingerprint: str = ""

    @classmethod
    def from_track(
        cls,
        track: Track,
        embedded_at: float | None = None,
        size: int = 0,
        fingerprint: str = "",
    ) -> IndexEntry:
        """Create an :class:`IndexEntry` from a beets :class:`~autodj.beets.Track`.

        Args:
            track: A track loaded from the beets library.
            embedded_at: Stale-check stamp to store.  Defaults to the source
                file's own modification time.
            size: The file's size in bytes, from :func:`file_fingerprint`.
            fingerprint: The file's :func:`file_fingerprint` digest.

        Returns:
            An :class:`IndexEntry` with the same metadata.  ``embedded_at``
            comes from the file server's clock rather than the indexing
            host's, because the scanner compares it against
            ``st_mtime``.  A NAS clock running ahead used to make every
            freshly embedded track look replaced on the next run, so the whole
            library was re-embedded forever.
        """
        if embedded_at is None:
            embedded_at = source_mtime(track.path)

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
            embedded_at=embedded_at,
            size=size,
            fingerprint=fingerprint,
        )

    @property
    def is_silent(self) -> bool:
        """Whether the track is silent, so AutoDJ never picks it by itself.

        Search can still play it when the user asks for it.
        """
        return self.energy < SILENT_ENERGY

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


def walk_music_dir(music_dir: Path, formats: list[str]) -> Iterator[Path]:
    """Yield the audio files under *music_dir* matching *formats*.

    The walk is lazy, folder by folder in name order, so a caller that
    needs only a few files can stop without listing a whole library.

    Args:
        music_dir: Root directory to search.
        formats: List of file extensions to include (without the leading dot,
            e.g. ``["mp3", "flac", "m4a"]``).

    Returns:
        An iterator of :class:`~pathlib.Path` objects under *music_dir*.

    Raises:
        FileNotFoundError: If *music_dir* does not exist.
    """
    if not music_dir.exists():
        raise FileNotFoundError(f"Music directory not found: {music_dir}")
    extensions = {f".{ext.lower()}" for ext in formats}

    def walk() -> Iterator[Path]:
        for root, dirs, files in os.walk(music_dir):
            dirs.sort()
            for name in sorted(files):
                if os.path.splitext(name)[1].lower() in extensions:
                    yield Path(root, name)

    return walk()


# ---------------------------------------------------------------------------
# Audio loading
# ---------------------------------------------------------------------------

# Each phase saves the files it decoded with warnings or skipped here, in the
# index directory, so they can be read after the progress line has moved on.
INDEX_DECODE_LOG = "decode-indexing.log"
ANALYSE_DECODE_LOG = "decode-analysing.log"
LIBRARY_DECODE_LOG = "decode-library.log"


def _load_audio(path: Path) -> tuple[np.ndarray, int]:
    """Load an audio file to a mono float32 array at its native sample rate.

    Decodes through :func:`autodj.stereo.load_mono`, the loader playback
    uses too.

    Args:
        path: Path to the audio file.

    Returns:
        ``(audio, sample_rate)`` where *audio* is a 1-D float32 mono array.
    """
    if sf is None:
        raise RuntimeError("soundfile is required for audio indexing")
    # autodj.stereo needs soundfile, so it is imported only once that is known.
    from autodj.stereo import load_mono

    return load_mono(path)


def _finish_decode_report(report: Any, log_path: Path) -> None:
    """Save *report* to *log_path* and print its one-line summary, if any."""
    report.write(log_path)
    summary = report.summary(log_path)
    if summary is not None:
        print(f"[AutoDJ] {summary}", flush=True)


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------


_MAJOR_KEY_PROFILE = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88],
    dtype=np.float32,
)
_MINOR_KEY_PROFILE = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17],
    dtype=np.float32,
)


def _estimate_key_from_chroma(chroma: np.ndarray) -> tuple[int, int]:
    """Return ``(pitch_class, mode)`` or ``(-1, -1)`` when evidence is unusable."""
    values = np.asarray(chroma, dtype=np.float32).reshape(-1)
    if (
        values.shape != (12,)
        or not np.isfinite(values).all()
        or np.any(values < 0)
        or float(values.sum()) <= 1e-6
    ):
        return (-1, -1)
    peak_share = float(values.max() / values.sum())
    if peak_share < 0.12:
        return (-1, -1)
    centred = values - float(values.mean())
    norm = float(np.linalg.norm(centred))
    if norm <= 1e-6:
        return (-1, -1)
    centred /= norm

    scored: list[tuple[float, int, int]] = []
    for mode, profile in ((1, _MAJOR_KEY_PROFILE), (0, _MINOR_KEY_PROFILE)):
        profile_centred = profile - float(profile.mean())
        profile_centred /= float(np.linalg.norm(profile_centred))
        for key in range(12):
            scored.append((float(np.dot(np.roll(profile_centred, key), centred)), key, mode))
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, key, mode = scored[0]
    margin = best_score - scored[1][0]
    if best_score < 0.50 or margin < 0.08:
        return (-1, -1)
    return (key, mode)


def _apply_analysis_metadata(
    entry: IndexEntry, extra_meta: dict[str, float | int], keep: Collection[str] = ()
) -> None:
    """Apply derived analysis fields without replacing a valid source BPM.

    *keep* names fields set by hand (``"key"``, ``"bpm"``, from Track info's
    tag editor) that the analysis must leave as they are.
    """
    entry.energy = float(extra_meta["energy"])
    if "key" not in keep:
        entry.key = int(extra_meta["key"])
        entry.mode = int(extra_meta["mode"])
    entry.tempo_confidence = float(extra_meta["tempo_confidence"])
    estimated_bpm = float(extra_meta["bpm"])
    if "bpm" in keep:
        return
    if not np.isfinite(entry.bpm) or entry.bpm <= 0.0:
        entry.bpm = 0.0
        if np.isfinite(estimated_bpm) and estimated_bpm > 0.0:
            entry.bpm = estimated_bpm


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
    - ``bpm`` — librosa beat-tracker tempo, or 0.0 when unavailable
    - ``tempo_confidence`` — ratio of detected beats to expected beats (0–1)

    Args:
        audio_path: Path to the audio file to analyse.

    Returns:
        A tuple of ``(feature_vector, audio_array, sample_rate, extra_meta)``
        where *feature_vector* is a float32 array of shape ``(16,)`` (not yet
        normalized), *audio_array* is the mono audio, *sample_rate* is the
        native sample rate, and *extra_meta* is a dict with keys
        ``energy``, ``key``, ``mode``, ``bpm``, ``tempo_confidence``.
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

        key, mode = _estimate_key_from_chroma(chroma)

        # tempo confidence: detected beats / expected beats at estimated tempo
        tempo_val = 0.0
        try:
            tempo_arr, beat_frames = librosa.beat.beat_track(y=audio, sr=sr)
            tempo_val = float(np.atleast_1d(tempo_arr)[0])
            if not np.isfinite(tempo_val) or tempo_val <= 0:
                tempo_val = 0.0
                tempo_confidence = 0.0
            else:
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
        "bpm": tempo_val,
        "tempo_confidence": tempo_confidence,
    }
    return features, audio, sr, extra_meta


#: Names how a song's vector is made: MuQ mean-pooled over 30-second chunks
#: at 24 kHz, then the 16 librosa features, each part and the whole
#: L2-normalized.  A change to any of that changes this, and the server then
#: refuses results from workers still running the old code.
ANALYSIS_VERSION = "muq-mean-30s-24k+librosa16/1"


def analysis_identity(model_name: str, model_commit: str | None) -> dict[str, object]:
    """What a remote analysis result must have been made with to be accepted here.

    Args:
        model_name: ``[model] name``, the HuggingFace model id.
        model_commit: The weights in use, from
            :func:`autodj.model_identity.local_model_commit`: the snapshot
            commit, or a digest of the files.  ``None`` when this machine has
            no copy of the model.

    Returns:
        The model, its weights, :data:`ANALYSIS_VERSION` and the vector size.
    """
    return {
        "model": model_name,
        "model_commit": model_commit,
        "analysis_version": ANALYSIS_VERSION,
        "dimension": FEATURE_DIM,
    }


@dataclass(frozen=True)
class TrackAnalysis:
    """One song's advanced analysis: its search vector and the measured fields.

    ``bpm`` is librosa's estimate, used only when the song has no BPM tag.
    """

    vector: np.ndarray
    energy: float
    key: int
    mode: int
    bpm: float
    tempo_confidence: float

    def metadata(self) -> dict[str, float | int]:
        """The measured fields, as :func:`_apply_analysis_metadata` takes them."""
        return {
            "energy": self.energy,
            "key": self.key,
            "mode": self.mode,
            "bpm": self.bpm,
            "tempo_confidence": self.tempo_confidence,
        }


def finish_analysis(
    wrapper: MuqWrapper, features: tuple[np.ndarray, np.ndarray, int, dict]
) -> TrackAnalysis:
    """Embed a decoded song and combine it with its librosa features.

    *features* is what :func:`_extract_librosa_features` returned for the
    song.  ``autodj index``, the server's analysis job and ``autodj
    worker`` all finish every song here, so their vectors are the same.

    Raises:
        ValueError: The vector is not finite (a silent or corrupt file).
    """
    librosa_vec, audio, sr, extra_meta = features
    embedding_vec = wrapper.embed_array(audio, sample_rate=sr)
    combined = _combine_features(embedding_vec, librosa_vec)
    if not np.isfinite(combined).all():
        raise ValueError("embedding contains NaN or Inf — track may be silent or corrupted")
    return TrackAnalysis(
        vector=combined,
        energy=float(extra_meta["energy"]),
        key=int(extra_meta["key"]),
        mode=int(extra_meta["mode"]),
        bpm=float(extra_meta["bpm"]),
        tempo_confidence=float(extra_meta["tempo_confidence"]),
    )


def analyse_file(path: Path, wrapper: MuqWrapper) -> TrackAnalysis:
    """Decode one audio file and analyse it the way ``autodj index`` does."""
    return finish_analysis(wrapper, _extract_librosa_features(path))


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
       before the caller renames it to its generation name.

    Memory cost: an extra ~Nbytes copy of the index during write.  For a
    300 MB FAISS index that's 300 MB peak — easy on any reasonable host.

    Args:
        index: The FAISS index to save.
        path: Destination path (typically a ``*.tmp`` file that the caller
            renames to the new generation's name).
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
        # fsync may not be supported on every FS — best-effort
        with contextlib.suppress(OSError):
            os.fsync(fh.fileno())


def save_index(
    entries: list[IndexEntry],
    vectors: np.ndarray,
    index_dir: Path,
    music_dir: Path | None = None,
    *,
    base_generation: int,
) -> IndexManifest:
    """Publish *entries* and *vectors* as the next index generation.

    Paths are stored relative to *music_dir* (forward-slashed), making the
    index portable across machines that mount the library at a different
    absolute path.  A path outside *music_dir* raises ``ValueError``.
    Runtime ``entry.path`` values are not mutated.

    Args:
        entries: One :class:`IndexEntry` per track, in the row order of *vectors*.
        vectors: float32 array of shape ``(len(entries), FEATURE_DIM)``.
        index_dir: The index directory.
        music_dir: Library root — when set, paths are relativized for storage.
        base_generation: The generation *entries* were built from, or 0.

    Returns:
        The new manifest.

    Raises:
        IndexConsistencyError: If another command published since
            *base_generation*.
    """
    rows = [_entry_to_row(entry, music_dir, vec_row) for vec_row, entry in enumerate(entries)]
    matrix = np.asarray(vectors, dtype=np.float32).reshape(len(entries), FEATURE_DIM)

    def write_files(tracks_path: Path, vectors_path: Path) -> None:
        _write_tracks_file(rows, tracks_path)
        _write_faiss_chunked(build_faiss_index(matrix), vectors_path)

    manifest = publish_generation(
        index_dir,
        base_generation=base_generation,
        vector_count=len(entries),
        write_files=write_files,
    )
    logger.info(
        "Published index generation %d with %d tracks to %s",
        manifest.generation,
        len(entries),
        index_dir,
    )
    return manifest


class PruneSafetyError(RuntimeError):
    """Raised when prune would remove a suspiciously large fraction of entries.

    Almost always indicates a misconfigured ``music_dir`` rather than
    genuine library cleanup — refusing the operation prevents
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
        row = all_rows.get(os.path.normcase(candidate.encode("utf-8")))
        if row is not None:
            return row
    return None


def _apply_beets_row(
    entry: IndexEntry,
    row: dict,
    text_cols: tuple[str, ...],
    has_initial_key: bool,
    parse_initial_key: Any,
) -> bool:
    """Apply every overwrite rule for one entry; return True when something changed.

    Text columns win when beets has a non-empty value, numeric columns when
    beets has a positive one, and key/mode when ``initial_key`` parses to
    something different.

    Args:
        entry: Index entry mutated in place.
        row: Beets row for this entry.
        text_cols: Text columns to copy across.
        has_initial_key: Whether the beets schema carries ``initial_key``.
        parse_initial_key: Callable turning a key string into ``(key, mode)``.

    Returns:
        True when any field was overwritten.
    """
    changed = False
    for col in text_cols:
        text = str(row[col] or "")
        if text and getattr(entry, col) != text:
            setattr(entry, col, text)
            changed = True
    bpm = float(row["bpm"] or 0.0)
    if bpm > 0 and abs(entry.bpm - bpm) > 1e-3:
        entry.bpm = bpm
        changed = True
    year = int(row["year"] or 0)
    if year > 0 and entry.year != year:
        entry.year = year
        changed = True
    length = float(row["length"] or 0.0)
    if length > 0 and abs(entry.length - length) > 1e-3:
        entry.length = length
        changed = True
    if has_initial_key:
        parsed = parse_initial_key(str(row["initial_key"] or ""))
        if parsed is not None and (entry.key, entry.mode) != parsed:
            entry.key, entry.mode = parsed
            changed = True
    return changed


def enrich_from_beets(
    index_dir: Path,
    music_dir: Path | None,
    beets_db: Path,
) -> tuple[int, int]:
    """Refresh current catalog entries from beets without re-embedding.

    Walks the music entries, looks up each track in *beets_db* by path, and
    overwrites a curated set of fields when beets has values for them.
    The current catalog is authoritative; legacy published-index files are
    never loaded or rewritten by this operation.

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
        index_dir: The active library directory.
        music_dir: Library root (used to resolve relative beets paths).
        beets_db: Path to the beets ``library.db``.

    Returns:
        ``(updated, total)`` — count of entries with at least one field
        changed, and the total entries scanned.

    Raises:
        BeetsNotFoundError: If *beets_db* does not exist.
    """
    from autodj.catalog import LibraryCatalog

    catalog_path = index_dir / "library.db"
    if not catalog_path.is_file():
        return (0, 0)
    with LibraryCatalog(catalog_path) as catalog:
        entries = list(catalog.entries_snapshot())
        changed = _enrich_entries(entries, music_dir, beets_db)
        for entry in changed:
            catalog.put_entry(entry)
        return len(changed), len(entries)


def _enrich_entries(
    entries: list[IndexEntry], music_dir: Path | None, beets_db: Path
) -> list[IndexEntry]:
    """Apply beets metadata to a catalog snapshot."""
    from autodj.beets import _items_columns, _open_db, _path_candidates, parse_initial_key

    conn = _open_db(beets_db)
    text_cols: tuple[str, ...] = ("title", "artist", "album", "genre")
    num_cols: tuple[str, ...] = ("bpm", "year", "length")
    has_initial_key = False
    changed = []
    try:
        cols = _items_columns(conn)
        select_cols: list[str] = []
        select_cols.extend(text_cols)
        select_cols.extend(num_cols)
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
            all_rows[os.path.normcase(bytes(row["path"]))] = {col: row[col] for col in select_cols}
        logger.info("Loaded %d beets items", len(all_rows))

        print(
            f"[AutoDJ] Phase: Enriching — scanning {len(entries)} tracks against beets.",
            flush=True,
        )
        for e in entries:
            row = _find_beets_row(e.path, music_dir, all_rows, _path_candidates)
            if row is None:
                continue
            if _apply_beets_row(e, row, text_cols, has_initial_key, parse_initial_key):
                changed.append(e)
    finally:
        conn.close()

    return changed


def _check_prune_safety(removed: int, total: int, allow_mass_prune: bool) -> None:
    """Raise PruneSafetyError when the prune ratio would cross the threshold."""
    if allow_mass_prune or total == 0 or removed / total <= PRUNE_SAFETY_THRESHOLD:
        return
    raise PruneSafetyError(
        f"Refusing to prune {removed}/{total} tracks "
        f"({removed / total:.0%} > {PRUNE_SAFETY_THRESHOLD:.0%} threshold).\n"
        "This usually means [library] music_dir in your config does not "
        "match where the indexed files actually live.\n"
        "Fix the config first, then re-run.  If you really did delete "
        "this many tracks, pass allow_mass_prune=True (or "
        "`autodj prune --force` from the CLI)."
    )


def prune_index(
    index_dir: Path,
    music_dir: Path | None = None,
    allow_mass_prune: bool = False,
) -> tuple[int, int]:
    """Remove index entries whose audio files no longer exist on disk.

    Resolves each stored path against *music_dir*, drops every row whose
    audio file is missing, and publishes the rest as a new generation (an
    empty one when every track is gone).  No-op when no index exists or
    nothing is missing.

    Safety: if more than :data:`PRUNE_SAFETY_THRESHOLD` of the entries
    would be removed, raises :class:`PruneSafetyError` instead of touching
    the index.  Override with ``allow_mass_prune=True`` (e.g. after
    confirming you really did delete most of your library).

    Args:
        index_dir: The index directory.
        music_dir: Library root for resolving relative paths.
        allow_mass_prune: If ``True``, skip the safety check and prune
            even if it would remove most of the index.

    Returns:
        ``(removed, kept)`` — count of entries dropped and count surviving.

    Raises:
        PruneSafetyError: If the prune would exceed the safety threshold
            and ``allow_mass_prune`` is not set.
    """
    try:
        entries, loaded, manifest = load_index(index_dir, music_dir=music_dir)
    except FileNotFoundError:
        return (0, 0)

    print(
        f"[AutoDJ] Phase: Pruning — checking {len(entries)} indexed files on disk.",
        flush=True,
    )
    keep_mask = [mtime is not None for mtime in _stat_mtimes(entries)]
    removed = keep_mask.count(False)
    _check_prune_safety(removed, len(entries), allow_mass_prune)
    if removed == 0:
        return (0, len(entries))

    surviving_entries = [e for e, k in zip(entries, keep_mask, strict=True) if k]
    # One FAISS call returns the whole (N, dim) array; a numpy mask slices it.
    all_vectors = loaded.reconstruct_n(0, loaded.ntotal)
    surviving_vectors = all_vectors[np.asarray(keep_mask, dtype=bool)]
    save_index(
        surviving_entries,
        surviving_vectors,
        index_dir,
        music_dir,
        base_generation=manifest.generation,
    )
    logger.info("Pruned %d missing tracks (%d remain)", removed, len(surviving_entries))
    return (removed, len(surviving_entries))


def load_index(
    index_dir: Path,
    music_dir: Path | None = None,
) -> tuple[list[IndexEntry], faiss.IndexFlatIP, IndexManifest]:
    """Load the live generation named by ``index-manifest.json``.

    Both files are checked against the manifest's SHA-256 digests and track
    count.  When *music_dir* is provided, the relative stored paths are
    joined to it, so ``entry.path`` is an absolute runtime path on return.

    Args:
        index_dir: The index directory.
        music_dir: Library root for resolving relative paths.

    Returns:
        ``(entries, faiss_index, manifest)``, entries in FAISS row order.

    Raises:
        FileNotFoundError: If the directory holds no index.
        UnsupportedIndexError: If the index was made by an older AutoDJ.
        IndexConsistencyError: If a file the manifest names is missing or
            does not match it.
    """
    with publication_lock(index_dir):
        manifest = require_manifest(index_dir)
        tracks_path = index_dir / manifest.tracks_file
        vectors_path = index_dir / manifest.vectors_file
        for path, digest in (
            (tracks_path, manifest.tracks_sha256),
            (vectors_path, manifest.vectors_sha256),
        ):
            if not path.is_file():
                problem = "is missing"
            elif sha256_file(path) != digest:
                problem = "does not match its SHA-256 in the manifest"
            else:
                continue
            raise IndexConsistencyError(
                f"{path} {problem}; copy the index again, or rebuild it with `{REBUILD_COMMAND}`"
            )
        faiss_index = cast("faiss.IndexFlatIP", faiss.read_index(str(vectors_path)))
        conn = sqlite3.connect(readonly_uri(tracks_path, immutable=True), uri=True)
        try:
            entries = _load_tracks_rows(conn)
        finally:
            conn.close()
    if len(entries) != manifest.vector_count or faiss_index.ntotal != manifest.vector_count:
        raise IndexConsistencyError(
            f"index count mismatch: manifest={manifest.vector_count}, "
            f"tracks={len(entries)}, vectors={faiss_index.ntotal}"
        )
    absolute = next((e.path for e in entries if is_absolute_storage(e.path)), None)
    if absolute is not None:
        raise UnsupportedIndexError(index_dir, f"it stores the absolute path {absolute}")
    if music_dir is not None:
        for entry in entries:
            entry.path = str(music_dir / entry.path)
    logger.info(
        "Loaded index generation %d with %d tracks from %s",
        manifest.generation,
        len(entries),
        index_dir,
    )
    return entries, faiss_index, manifest


# ---------------------------------------------------------------------------
# Public build entry point
# ---------------------------------------------------------------------------


def _analyse_one_track(path: str) -> Any | None:
    """Decode *path* and return its :class:`autodj.dj_meta.DjMeta`, or ``None`` when silent."""
    from autodj.dj_meta import analyse_audio

    audio, sr = _load_audio(Path(path))
    if len(audio) == 0:
        return None
    return analyse_audio(audio, sr)


def backfill_dj_meta(
    cfg: AutoDJConfig,
    entries: list[IndexEntry],
    *,
    limit: int | None = None,
    prune: bool = True,
) -> None:
    """Analyse DJ metadata (intro/outro, beats, cues) for tracks that lack it.

    First drops ``dj_meta.db`` rows of tracks no longer in *entries*.  Then
    decodes each track whose row is missing or not analysed, one at a
    time, runs :func:`autodj.dj_meta.analyse_audio`, merges cues imported
    from DJ software when ``[playback] import_external_cues`` is on, and
    stores the result.  Rows are flushed every 25 tracks, so an interrupted
    run loses at most that many.  ``[index] throttle_ms`` pauses before each
    track.

    Args:
        cfg: Configuration naming the index, the library and the settings above.
        entries: Every track in the index, with absolute paths.
        limit: Analyse at most this many of the tracks that need it.
        prune: Remove rows outside entries; disable when passing only eligible folders.
    """
    from autodj.dj_meta import get_cache

    cache = get_cache(cfg.index.active_dir, music_dir=cfg.library.music_dir)
    if cache is None:  # pragma: no cover - get_cache with an index_dir always builds one
        return
    removed_stale = cache.prune_to_paths({e.path for e in entries}) if prune else 0
    if removed_stale:
        print(f"[AutoDJ] DJ-meta cache pruned {removed_stale} stale entries.", flush=True)
    pending = [e.path for e in entries if not cache.get(e.path).analysed]
    if limit is not None:
        pending = pending[:limit]
    if not pending:
        print("[AutoDJ] DJ-meta cache already covers every indexed track.")
        return
    library_cues: dict[str, list[Any]] = {}
    import_cues = cfg.playback.import_external_cues
    if import_cues:
        from autodj.dj_cues_import import auto_import_cues, merge_imported_cues

        library_cues = auto_import_cues(library_root=cfg.library.music_dir)
        logger.info("Imported cues for %d tracks from DJ software", len(library_cues))
    total = len(pending)
    print(f"[AutoDJ] Phase: Analysing — DJ-meta backfill for {total} tracks.", flush=True)
    throttle_s = cfg.index.throttle_ms / 1000.0
    done = 0
    from autodj.stereo import DecodeReport

    report = DecodeReport()
    try:
        with report, TrackProgress("Analysing", total, logger) as progress:
            for count, path in enumerate(pending, start=1):
                if throttle_s:
                    time.sleep(throttle_s)
                try:
                    meta = _analyse_one_track(path)
                except Exception as exc:
                    logger.warning("Could not analyse %s, skipped: %s", path, exc)
                    report.skip(path, exc)
                    meta = None
                if meta is not None:
                    if import_cues:
                        merge_imported_cues(meta, path, library_cues)
                    cache.set(path, meta)
                    done += 1
                    cache.flush()
                progress.update(count)
    except KeyboardInterrupt:  # pragma: no cover - Ctrl+C
        cache.flush(force=True)
        _finish_decode_report(report, cfg.index.active_dir / ANALYSE_DECODE_LOG)
        print(
            f"[AutoDJ] DJ-meta interrupted: {done}/{total} analysed.  "
            "Re-run `autodj analyse` to resume.",
            flush=True,
        )
        return
    cache.flush(force=True)
    _finish_decode_report(report, cfg.index.active_dir / ANALYSE_DECODE_LOG)
    print(f"[AutoDJ] DJ-meta backfill done: {done}/{total} tracks analysed.", flush=True)


def _stat_mtimes(entries: list[IndexEntry]) -> list[float | None]:
    """Return each entry's file mtime, or ``None`` when the file is missing.

    One network round trip per file on a NAS; eight threads pipeline them.
    """

    def _mtime(p: str) -> float | None:
        try:
            return Path(p).stat().st_mtime
        except OSError:
            return None

    mtimes: list[float | None] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for mtime in pool.map(_mtime, [e.path for e in entries]):
            mtimes.append(mtime)
            _log_progress("Checking files", len(mtimes), len(entries), _STAT_PROGRESS_EVERY)
    return mtimes


def _stat_size_mtime(path: str) -> tuple[int, float] | None:
    """Return ``(size, mtime)`` of *path*, or ``None`` when it cannot be read."""
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime


def _detect_moves(
    entries: list[IndexEntry],
    missing_paths: set[str],
    candidates: list[Track],
) -> dict[str, tuple[Track, float]]:
    """Match entries whose file vanished to new files with the same bytes.

    An entry and a candidate match when they share ``(size, fingerprint)``
    and nothing else does: exactly one missing entry and exactly one
    candidate hold that identity, and no entry whose file still exists holds
    it (that candidate is a copy, not a move).  Anything else is left alone,
    so the caller prunes and embeds as usual.  Fingerprints are read only for
    candidates whose size equals the size of some missing entry.

    Args:
        entries: Existing index entries (absolute paths).
        missing_paths: Paths of *entries* whose file is gone.
        candidates: Files on disk that are not in the index.

    Returns:
        Old entry path mapped to ``(candidate track, candidate mtime)``.
    """
    missing = [e for e in entries if e.path in missing_paths and e.size and e.fingerprint]
    if not missing or not candidates:
        return {}
    missing_sizes = {e.size for e in missing}
    present = {
        (e.size, e.fingerprint)
        for e in entries
        if e.path not in missing_paths and e.size and e.fingerprint
    }

    def _identify(track: Track) -> tuple[Track, float, tuple[int, str]] | None:
        stat = _stat_size_mtime(str(track.path))
        if stat is None or stat[0] not in missing_sizes:
            return None
        return track, stat[1], file_fingerprint(track.path)

    by_id_missing: dict[tuple[int, str], list[IndexEntry]] = {}
    for e in missing:
        by_id_missing.setdefault((e.size, e.fingerprint), []).append(e)
    by_id_new: dict[tuple[int, str], list[tuple[Track, float]]] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for done, found in enumerate(pool.map(_identify, candidates), start=1):
            _log_progress("Checking new files", done, len(candidates), _STAT_PROGRESS_EVERY)
            if found is None or not found[2][1]:
                continue
            by_id_new.setdefault(found[2], []).append((found[0], found[1]))
    moves: dict[str, tuple[Track, float]] = {}
    for identity, olds in by_id_missing.items():
        news = by_id_new.get(identity, [])
        if len(olds) == 1 and len(news) == 1 and identity not in present:
            moves[olds[0].path] = news[0]
    return moves


def _embed_new_tracks(  # pragma: no cover -- threaded indexer pipeline
    new_tracks: list[Track],
    wrapper: MuqWrapper,
    workers: int | None,
    checkpoint: Callable[[list[IndexEntry], list[np.ndarray]], None],
    throttle_ms: float,
    report: Any,
    gate: Callable[[Track], bool | None] | None = None,
) -> tuple[list[IndexEntry], list[np.ndarray]]:
    """Embed *new_tracks*, decoding the next ones on a prefetch pool meanwhile.

    *gate*, when given, is asked before each track is decoded: ``True``
    analyses it, ``False`` skips it, and ``None`` stops taking tracks (those
    already decoding still finish).
    """
    new_entries: list[IndexEntry] = []
    new_vectors: list[np.ndarray] = []

    if workers is None:
        # Full-track spectral analysis can consume several GiB for long mixes
        # and high-rate masters.  Parallel decoding caused the kernel OOM
        # killer to terminate otherwise healthy full-library runs.
        workers = 1
    prefetch = max(1, workers)
    throttle_s = throttle_ms / 1000.0
    track_iter = iter(new_tracks)
    pending: deque[tuple[Track, float, tuple[int, str], Future]] = deque()

    with ThreadPoolExecutor(max_workers=prefetch) as pool:

        def _next_track() -> Track | None:
            nonlocal track_iter
            for t in track_iter:
                decision = True if gate is None else gate(t)
                if decision is None:
                    track_iter = iter(())
                    return None
                if decision:
                    return t
            return None

        def _submit_next() -> None:
            t = _next_track()
            if t is None:
                return
            if throttle_s:
                time.sleep(throttle_s)
            # Stat before decoding so a file edited mid-run is still seen
            # as stale on the next pass.
            pending.append(
                (
                    t,
                    source_mtime(t.path),
                    file_fingerprint(t.path),
                    pool.submit(_extract_librosa_features, t.path),
                )
            )

        for _ in range(prefetch):
            _submit_next()

        done = 0
        with TrackProgress("Indexing", len(new_tracks), logger) as progress:
            while pending:
                track, mtime, (size, fingerprint), future = pending.popleft()
                _submit_next()
                done += 1
                try:
                    analysis = finish_analysis(wrapper, future.result())
                    entry = IndexEntry.from_track(
                        track, embedded_at=mtime, size=size, fingerprint=fingerprint
                    )
                    _apply_analysis_metadata(entry, analysis.metadata())

                    from autodj.beets import parse_initial_key as _parse_key

                    if getattr(track, "initial_key", ""):
                        parsed = _parse_key(track.initial_key)
                        if parsed is not None:
                            entry.key, entry.mode = parsed
                    new_entries.append(entry)
                    new_vectors.append(analysis.vector)
                except Exception as exc:
                    logger.warning("Could not index %s, skipped: %s", track.path, exc)
                    report.skip(track.path, exc)
                    continue
                finally:
                    progress.update(done)
                checkpoint(new_entries, new_vectors)

    return new_entries, new_vectors
