"""DJ-grade audio analysis: intro/outro, beat grid, harmonic mixing, sidecar cache.

This module powers the pro-DJ features layered on top of the basic
similarity engine:

- :func:`detect_intro_start` — find the first sound, where the incoming
  track starts during a crossfade (leading silence is skipped).
- :func:`detect_intro_outro` — find the seconds at which the perceived
  intro ends and the outro starts, used for outro→intro-aligned crossfade.
- :func:`detect_beat_grid` — extract beat positions, used for
  phrase-aligned crossfade (snap mix point to a phrase boundary).
  Downbeats are taken as every 4th beat
  (:func:`autodj.beat_sync.extract_downbeats`).
- :func:`harmonic_compatible` — Camelot wheel test that lets the picker
  filter candidates to harmonically-compatible keys, and
  :func:`key_shift_semitones`, the smallest key shift that makes two
  keys mix (``[djmix] key_shift``).
- :func:`mix_drops` and :func:`mix_anchors` — the drops and breakdowns
  reliable enough to time a drop-to-drop mix by (``[djmix] drop_mix``).
- :class:`DjMetaCache` — SQLite-backed cache
  (``<index_dir>/<name>/dj_meta.db``) so
  the heavy librosa analysis only runs once per track, then is reused.

All detection is opt-in (the player invokes it lazily when a feature that
needs it is enabled).  The standard FAISS index is unchanged — adding DJ
metadata never requires re-indexing the library.

Example:
    >>> from autodj.dj_meta import detect_intro_outro, detect_beat_grid
    >>> import soundfile as sf
    >>> audio, sr = sf.read("song.flac", dtype="float32", always_2d=False)
    >>> intro_end, outro_start = detect_intro_outro(audio, sr)
    >>> beats = detect_beat_grid(audio, sr)
    >>> beats[:4]
    [0.42, 0.93, 1.44, 1.95]
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from collections.abc import Set as AbstractSet
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import TracebackType

import numpy as np

from autodj.index_manifest import (
    OldDjMetaCacheError,
    first_absolute_path,
    relative_storage_path,
)
from autodj.sqlite_utils import immediate_transaction

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Intro / outro detection
# ---------------------------------------------------------------------------

# Mixxx's analyser treats samples below -60 dBFS as silence.
_SILENCE_AMPLITUDE = 10 ** (-60 / 20)


def detect_intro_start(audio: np.ndarray, sr: int) -> float:
    """Return the seconds of the first sample louder than -60 dBFS.

    This is where a track enters during a crossfade: only leading silence
    is skipped, never a quiet intro, so a verse sung over it stays whole.

    Args:
        audio: Float audio array, mono or ``(samples, channels)``.
        sr: Sample rate in Hz.

    Returns:
        The first-sound time in seconds, ``0.0`` for an empty or silent
        track.
    """
    if len(audio) == 0:
        return 0.0
    loud = np.abs(audio) >= _SILENCE_AMPLITUDE
    if loud.ndim > 1:
        loud = loud.any(axis=1)
    first = int(np.argmax(loud))
    if not loud[first]:
        return 0.0
    return first / max(1, sr)


def detect_intro_outro(
    audio: np.ndarray,
    sr: int,
    rms_window_s: float = 0.5,
    quiet_threshold: float = 0.35,
) -> tuple[float, float]:
    """Return ``(intro_end_seconds, outro_start_seconds)`` for an audio array.

    Algorithm: compute a smoothed RMS envelope of the track, normalise it
    to its 95th-percentile loudness, then walk forward from the start
    until the envelope first crosses *quiet_threshold* — that's the intro
    end.  Walk backward from the end the same way for the outro start.

    Tracks with no clear quiet intro / outro (a constant-loudness
    instrumental) collapse to ``intro_end == 0`` and
    ``outro_start == duration`` — the player treats this as "no special
    point, use the standard crossfade tail/head".

    Args:
        audio: Mono float32 audio array.
        sr: Sample rate in Hz.
        rms_window_s: Smoothing window length in seconds.  Default 0.5 s
            ≈ ½ bar at common tempos — coarse enough to ignore individual
            kicks but fine enough to catch a 1-bar intro.
        quiet_threshold: Fraction of the 95th-percentile loudness below
            which the track is considered "still in intro / already in
            outro".  0.35 catches typical 4-bar intros without being
            tricked by quiet verses.

    Returns:
        Tuple ``(intro_end_seconds, outro_start_seconds)``, both clamped
        to ``[0, duration]``.
    """
    if len(audio) == 0:
        return (0.0, 0.0)

    duration = len(audio) / max(1, sr)
    win = max(1, int(rms_window_s * sr))
    # Block-mean RMS — fast, no librosa import needed for this part
    n_blocks = max(1, len(audio) // win)
    blocks = audio[: n_blocks * win].reshape(n_blocks, win)
    rms = np.sqrt(np.mean(blocks**2, axis=1) + 1e-12)
    if rms.max() <= 1e-6:
        return (0.0, duration)

    # Normalise to 95th-percentile so a single loud transient doesn't crush the floor
    ref = float(np.percentile(rms, 95))
    if (
        ref <= 1e-6
    ):  # pragma: no cover — 95th-percentile floor; rms.max() guard above already covered the silent case
        return (0.0, duration)
    rms_norm = rms / ref

    # Forward walk for intro end
    intro_end = 0.0
    for i, v in enumerate(rms_norm):
        if v >= quiet_threshold:
            intro_end = i * rms_window_s
            break

    # Backward walk for outro start
    outro_start = duration
    for i in range(len(rms_norm) - 1, -1, -1):
        if rms_norm[i] >= quiet_threshold:
            outro_start = (i + 1) * rms_window_s
            break

    # Sanity: if outro_start <= intro_end the track is too short / weird.
    # Fall back to "no special points".
    if (
        outro_start <= intro_end
    ):  # pragma: no cover — both walks reach loud region; outro >= intro by construction unless all blocks below threshold (caught earlier)
        return (0.0, duration)
    return (max(0.0, intro_end), min(duration, outro_start))


# ---------------------------------------------------------------------------
# Beat grid
# ---------------------------------------------------------------------------


def detect_beat_grid(audio: np.ndarray, sr: int) -> list[float]:
    """Return a list of beat-onset timestamps in seconds.

    Wraps :func:`librosa.beat.beat_track` with sane defaults.  The
    returned grid is dense — one entry per beat — so phrase-aligned
    crossfade can snap to any 8 / 16 / 32 -beat boundary.

    Args:
        audio: Mono float32 audio array.
        sr: Sample rate in Hz.

    Returns:
        List of beat timestamps (seconds).  Empty list when beat
        detection fails (silent / very short track).
    """
    if len(audio) < sr:  # Less than 1 second of audio
        return []

    import librosa

    try:  # pragma: no cover — librosa internals
        _tempo, beat_frames = librosa.beat.beat_track(y=audio, sr=sr)
        return [float(t) for t in librosa.frames_to_time(beat_frames, sr=sr)]
    except Exception as exc:  # pragma: no cover — librosa internals
        logger.debug("Beat detection failed: %s", exc)
        return []


def nearest_phrase_boundary(
    beats: list[float],
    target_time_s: float,
    bars: int = 8,
    beats_per_bar: int = 4,
) -> float | None:
    """Return the beat-grid timestamp closest to *target_time_s* on a phrase boundary.

    A "phrase boundary" is every ``bars * beats_per_bar`` -th beat from
    the first detected beat (so 32 beats in for an 8-bar phrase at 4/4).
    Returns ``None`` when no phrase boundary lies within ½ phrase of
    *target_time_s* (or no beat grid available).

    Args:
        beats: Sorted list of beat timestamps from :func:`detect_beat_grid`.
        target_time_s: The time at which the crossfade would otherwise begin.
        bars: Number of bars per phrase (8 = pop, 16 = house typical).
        beats_per_bar: Beats per bar — 4 covers the vast majority of music.

    Returns:
        Snapped time in seconds, or ``None`` if no good boundary nearby.
    """
    if not beats:
        return None
    phrase_len_beats = bars * beats_per_bar
    if len(beats) < phrase_len_beats:
        return None

    # Approximate phrase length in seconds from average beat spacing
    if len(beats) >= 2:
        avg_beat_s = (beats[-1] - beats[0]) / max(1, len(beats) - 1)
    else:  # pragma: no cover — phrase_len_beats ≥ 32 guard above already rejects len(beats) < 2
        avg_beat_s = 0.5
    phrase_len_s = phrase_len_beats * avg_beat_s

    # Candidate boundaries: beats at indices 0, P, 2P, 3P, ...
    candidates = beats[::phrase_len_beats]
    # Pick closest to target_time_s
    best = min(candidates, key=lambda t: abs(t - target_time_s))
    if abs(best - target_time_s) > phrase_len_s / 2:
        return None
    return best


# ---------------------------------------------------------------------------
# Harmonic mixing (Camelot wheel)
# ---------------------------------------------------------------------------


# IndexEntry uses chromatic key 0-11 (C=0 ... B=11) and mode 1=major / 0=minor.
# Camelot wheel maps each (key, mode) to a position 1A-12B; tracks are
# harmonically compatible when their positions are equal, ±1 around the
# wheel, or paired across A/B at the same number.
#
# Camelot positions for major (B-side) and minor (A-side):
#   1B = B major,  1A = G# minor
#   2B = F# major, 2A = D# minor
#   3B = C# major, 3A = A# minor
#   ... etc.
#
# Build the mapping from (chromatic_key, mode) -> camelot position number.

_CAMELOT_MAJOR = {  # chromatic key -> Camelot number (B side)
    11: 1,  # B major
    6: 2,  # F# major
    1: 3,  # C# major
    8: 4,  # G# major
    3: 5,  # D# major
    10: 6,  # A# major
    5: 7,  # F major
    0: 8,  # C major
    7: 9,  # G major
    2: 10,  # D major
    9: 11,  # A major
    4: 12,  # E major
}
_CAMELOT_MINOR = {  # chromatic key -> Camelot number (A side)
    8: 1,  # G# minor
    3: 2,  # D# minor
    10: 3,  # A# minor
    5: 4,  # F minor
    0: 5,  # C minor
    7: 6,  # G minor
    2: 7,  # D minor
    9: 8,  # A minor
    4: 9,  # E minor
    11: 10,  # B minor
    6: 11,  # F# minor
    1: 12,  # C# minor
}


def camelot_position(key: int, mode: int) -> tuple[int, str] | None:
    """Convert a chromatic ``(key, mode)`` to a Camelot ``(number, side)``.

    Args:
        key: Chromatic key 0–11 (C=0, C#=1, …, B=11).  ``-1`` = unknown.
        mode: ``1`` = major, ``0`` = minor.  ``-1`` = unknown.

    Returns:
        ``(number, "A"|"B")`` or ``None`` for unknown / out-of-range values.
    """
    if not (0 <= key <= 11) or mode not in (0, 1):
        return None
    table = _CAMELOT_MAJOR if mode == 1 else _CAMELOT_MINOR
    side = "B" if mode == 1 else "A"
    if key not in table:  # pragma: no cover — both _CAMELOT_MAJOR / _MINOR contain all 12 keys
        return None
    return (table[key], side)


HARMONIC_MODES: tuple[str, ...] = (
    "off",
    "compatible",
    "strict",
    "energy_boost",
    "mood_change",
    "neighbour",
)


# Each harmonic mode is the set of ``(same Camelot side, steps)`` pairs it
# accepts, where steps is how far B is from A going up the wheel:
# ``(b - a) % 12``, so +1 is 1 and -1 is 11.
#   strict       — identical position.
#   mood_change  — relative major/minor: same number, opposite side.
#   neighbour    — same side, ±1 around the wheel.
#   energy_boost — same side, +2 around the wheel (two semitones up).
#   compatible   — union of strict + mood_change + neighbour.
_HARMONIC_RULES: dict[str, frozenset[tuple[bool, int]]] = {
    "strict": frozenset({(True, 0)}),
    "mood_change": frozenset({(False, 0)}),
    "neighbour": frozenset({(True, 1), (True, 11)}),
    "energy_boost": frozenset({(True, 2)}),
    "compatible": frozenset({(True, 0), (False, 0), (True, 1), (True, 11)}),
}


def harmonic_compatible(
    key_a: int,
    mode_a: int,
    key_b: int,
    mode_b: int,
    mode: str = "compatible",
) -> bool:
    """Return ``True`` when tracks A and B are harmonically mixable."""
    if mode == "off":
        return True
    if key_a < 0 or mode_a < 0 or key_b < 0 or mode_b < 0:
        return True
    pos_a = camelot_position(key_a, mode_a)
    pos_b = camelot_position(key_b, mode_b)
    if (
        pos_a is None or pos_b is None
    ):  # pragma: no cover — pre-validated keys always map to Camelot
        return True
    allowed = _HARMONIC_RULES.get(mode, _HARMONIC_RULES["compatible"])
    return (pos_a[1] == pos_b[1], (pos_b[0] - pos_a[0]) % 12) in allowed


# Key shifts tried by key_shift_semitones, in order: the smallest first,
# and down before up.
_KEY_SHIFTS = (-1, 1, -2, 2)


def key_shift_semitones(
    key_a: int, mode_a: int, key_b: int, mode_b: int, harmonic_mode: str = "off"
) -> int:
    """Semitones to move track B by so it mixes harmonically with track A.

    0 when either key is unknown, when the keys already mix (by the
    classic ``compatible`` rule, or by *harmonic_mode* when it is on, so
    an ``energy_boost`` pick is not undone), or when no shift of one or
    two semitones makes them mix.

    Args:
        key_a: Track A's chromatic key, 0-11 (-1 unknown).
        mode_a: Track A's mode, 1 major or 0 minor (-1 unknown).
        key_b: Track B's key.
        mode_b: Track B's mode.
        harmonic_mode: The ``[djmix] harmonic_mode`` setting.

    Returns:
        -2, -1, 1, 2 or 0.
    """
    if camelot_position(key_a, mode_a) is None or camelot_position(key_b, mode_b) is None:
        return 0

    def mixes(key: int) -> bool:
        return harmonic_compatible(key_a, mode_a, key, mode_b) or (
            harmonic_mode != "off"
            and harmonic_compatible(key_a, mode_a, key, mode_b, harmonic_mode)
        )

    if mixes(key_b):
        return 0
    return next((shift for shift in _KEY_SHIFTS if mixes((key_b + shift) % 12)), 0)


def camelot_label(key: int, mode: int) -> str:
    """Return a human label like ``"8A"`` (or ``"--"`` for unknown)."""
    pos = camelot_position(key, mode)
    if pos is None:
        return "--"
    return f"{pos[0]}{pos[1]}"


# Letter-name musical notation (the most universal display).  Two
# enharmonic spellings supported -- sharps match how most DJ catalogues
# store tags; flats match traditional music-theory teaching.  Picked
# via the ``prefer_flats`` argument (default False = sharps).
_MUSICAL_NAMES_SHARP = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_MUSICAL_NAMES_FLAT = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")


def musical_label(key: int, mode: int, *, prefer_flats: bool = False) -> str:
    """Return a letter-name label like ``"C"`` (major) or ``"Am"`` (minor).

    Args:
        key: Chromatic key 0-11.
        mode: ``1`` = major, ``0`` = minor.
        prefer_flats: When ``True``, render accidentals as flats
            (``Db`` instead of ``C#``).  Default ``False`` = sharps,
            which matches the spelling most DJ tag editors emit.

    Returns:
        Letter name + ``"m"`` suffix for minor, or ``"--"`` for unknown
        / out-of-range input.
    """
    if not (0 <= key <= 11) or mode not in (0, 1):
        return "--"
    table = _MUSICAL_NAMES_FLAT if prefer_flats else _MUSICAL_NAMES_SHARP
    name = table[key]
    return f"{name}m" if mode == 0 else name


def key_label(key: int, mode: int, notation: str = "camelot", *, prefer_flats: bool = False) -> str:
    """Return the active-notation label for ``(key, mode)``.

    Args:
        key: Chromatic key 0-11.  ``-1`` = unknown.
        mode: ``1`` = major, ``0`` = minor.  ``-1`` = unknown.
        notation: ``"camelot"`` (default) or ``"musical"``.  Unknown
            values fall back to Camelot.
        prefer_flats: Only meaningful when ``notation == "musical"``;
            picks flat accidentals (``Db``) over sharps (``C#``).

    Returns:
        Notation-appropriate label, or ``"--"`` for unknown input.
    """
    if notation == "musical":
        return musical_label(key, mode, prefer_flats=prefer_flats)
    return camelot_label(key, mode)


def key_spoken(
    key: int, mode: int, notation: str = "camelot", *, prefer_flats: bool = False
) -> str:
    """Return :func:`key_label` spelled out for a screen reader.

    NVDA reads ``"F#m"`` as "F number m" and ``"Bbm"`` as one word, so
    letter names become ``"F sharp minor"`` and ``"B flat minor"``.
    Camelot labels such as ``"8A"`` already read well and are returned
    as they are.

    Args:
        key: Chromatic key 0-11.  ``-1`` = unknown.
        mode: ``1`` = major, ``0`` = minor.  ``-1`` = unknown.
        notation: ``"camelot"`` (default) or ``"musical"``.
        prefer_flats: Only meaningful when ``notation == "musical"``.

    Returns:
        The spoken key, or ``""`` for unknown input.  The page leaves an
        unknown key out of the track-change announcement and says "Key
        unknown" for Shift+K; a bare "unknown" said nothing about what
        was unknown.
    """
    label = key_label(key, mode, notation, prefer_flats=prefer_flats)
    if label == "--":
        return ""
    if notation != "musical":
        return label
    table = _MUSICAL_NAMES_FLAT if prefer_flats else _MUSICAL_NAMES_SHARP
    name = table[key].replace("#", " sharp").replace("b", " flat")
    return f"{name} {'minor' if mode == 0 else 'major'}"


# ---------------------------------------------------------------------------
# Cache (SQLite) — keyed by track path relative to music_dir
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cue points — hot/memory cues, drops, breakdowns, phrase markers
# ---------------------------------------------------------------------------


@dataclass
class Cue:
    """A single cue point on a track.

    Attributes:
        time_s: Cue timestamp in seconds from track start.
        type: ``"first_downbeat"``, ``"drop"``, ``"breakdown"``,
            ``"build"``, ``"phrase"`` or ``"outro_downbeat"`` from
            :func:`detect_cues`, ``"user"``, the intro and outro markers
            of :data:`MARKER_CUE_TYPES` from a DJ-software import
            (``autodj.dj_cues_import``), or any custom string — the
            player only special-cases the built-ins; unknown types
            render as plain markers.
        label: Optional human label.  ``""`` for auto-detected cues.
        source: Provenance — ``"auto"`` (librosa), ``"mixxx"``,
            ``"rekordbox"``, ``"serato"``, ``"traktor"``, or ``"user"``.
        color: Optional ``"#rrggbb"`` for visual rendering, mainly to
            preserve the colours imported from DJ software.
    """

    time_s: float
    type: str = "user"
    label: str = ""
    source: str = "auto"
    color: str = ""


@dataclass
class DjMeta:
    """Per-track DJ analysis cache entry.

    Attributes:
        intro_start_s: Seconds of the first sound, where the track enters
            in a crossfade.  ``None`` = not measured (tracks analysed
            before this field existed); treat it as ``0.0``.
        intro_end_s: Seconds at which the intro ends.  ``0.0`` = no intro
            detected (or detection has not been run yet).
        outro_start_s: Seconds at which the outro starts; the track length
            when no outro was detected.  ``0.0`` = not analysed yet.
        beats: Beat-onset timestamps in seconds.  Empty list = unanalysed.
        analysed: ``True`` once detection has run, even if results are
            empty / zero — distinguishes "we tried and there's nothing"
            from "we haven't tried yet".
        cues: List of :class:`Cue` markers (auto-detected drops /
            breakdowns / phrase boundaries plus any imported from
            external DJ software).  Sorted by ``time_s`` ascending.
    """

    intro_end_s: float = 0.0
    outro_start_s: float = 0.0
    beats: list[float] = field(default_factory=list)
    analysed: bool = False
    cues: list[Cue] = field(default_factory=list)
    intro_start_s: float | None = None


class DjMetaCache:
    """SQLite-backed cache for :class:`DjMeta` keyed by track path.

    Stored at ``index/dj_meta.db`` next to the FAISS index.  Per-row
    UPSERTs on ``flush()`` touch only the dirty pages, so per-track
    checkpoints during a long ``analyse`` run stay cheap even on NAS
    mounts.

    A single process-wide instance is shared via :func:`get_cache`.  All
    operations are thread-safe (the player loads tracks on a background
    thread while the server reads cache state for the API).

    Analysed rows are kept in memory once read.  A track with no analysed
    row is looked up again after :attr:`MISS_TTL_S` seconds, because
    ``autodj analyse`` (run from the web Library panel) writes rows from
    another process while the server is running.

    After :meth:`close`, reads return an empty :class:`DjMeta` and writes
    are dropped, each logged once: a worker that outlives shutdown must
    not crash on the closed connection.

    Schema (one table)::

        CREATE TABLE dj_meta (
            path          TEXT PRIMARY KEY,
            intro_end_s   REAL NOT NULL DEFAULT 0,
            outro_start_s REAL NOT NULL DEFAULT 0,
            analysed      INTEGER NOT NULL DEFAULT 0,
            beats         TEXT,  -- JSON list of floats
            cues          TEXT,  -- JSON list of Cue dicts
            intro_start_s REAL   -- NULL = not measured yet
        );

    Beats + cues stay JSON-encoded inside a single row because they are
    small per track (typically <8 KB combined) and queries never project
    them individually.

    Example:
        >>> cache = DjMetaCache(Path("index/dj_meta.db"))
        >>> cache.get("song.flac")
        DjMeta(analysed=False, ...)
        >>> cache.set("song.flac", DjMeta(intro_end_s=12.3, analysed=True))
        >>> cache.flush()
    """

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS dj_meta (
            path          TEXT PRIMARY KEY,
            intro_end_s   REAL NOT NULL DEFAULT 0,
            outro_start_s REAL NOT NULL DEFAULT 0,
            analysed      INTEGER NOT NULL DEFAULT 0,
            beats         TEXT,
            cues          TEXT,
            intro_start_s REAL
        );
    """

    # Seconds before a track with no analysed row is looked up again.
    MISS_TTL_S = 5.0

    def __init__(
        self,
        sidecar_path: Path,
        music_dir: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialise the cache, opening or creating the SQLite store.

        Args:
            sidecar_path: Path to the SQLite database (``*.db``).
            music_dir: Optional library root used to store cache keys as
                portable relative paths.
            clock: Monotonic seconds, for the miss expiry.

        Raises:
            ValueError: If the cache stores an absolute track path, which only
                AutoDJ releases before relative keys wrote.
        """
        self._path = sidecar_path
        self._music_dir = music_dir
        self._lock = threading.Lock()
        self._dirty = 0
        # Pending writes — flushed in a single transaction by `flush()`.
        self._buf: dict[str, DjMeta] = {}
        # Read-through cache so repeat get() calls (e.g. server JSON
        # serialization) never hit SQLite twice for the same analysed row.
        self._mem_cache: dict[str, DjMeta] = {}
        # Tracks with no analysed row: (expiry, what the read returned).
        # Expiring them lets rows another process writes show up, while
        # get_state's few lookups a second stay off SQLite meanwhile.
        self._misses: dict[str, tuple[float, DjMeta]] = {}
        self._clock = clock
        self._warned_closed = False
        self._conn: sqlite3.Connection | None = None
        self._open()

    def __enter__(self) -> DjMetaCache:
        """Return this open cache for context manager use."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Flush successful work and close the cache connection."""
        del traceback
        try:
            if exc_type is None:
                self.flush(force=True)
        finally:
            self.close()

    def _key(self, path: str) -> str:
        """Return the canonical SQLite key for *path*.

        Runtime paths are absolute so audio can be opened directly, but cache
        identity is portable: keys are relative to ``music_dir``, matching
        ``tracks.db``.

        Raises:
            ValueError: If *path* is absolute and not under ``music_dir``.
        """
        return relative_storage_path(path, self._music_dir)

    def _open(self) -> None:
        """Open the SQLite connection and run the schema."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        first_init = not self._path.exists()
        # ``check_same_thread=False`` is safe because every read/write is
        # guarded by ``self._lock``; ``isolation_level=None`` puts the
        # connection in autocommit mode so our explicit
        # ``immediate_transaction`` blocks bracket each transaction cleanly.
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._conn.executescript(self._SCHEMA)
        # Caches written before intro_start_s existed lack the column; NULL
        # marks their rows as not measured.
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(dj_meta)")}
        if "intro_start_s" not in columns:
            self._conn.execute("ALTER TABLE dj_meta ADD COLUMN intro_start_s REAL")
        # WAL = concurrent reader while the writer flushes; NORMAL sync
        # is the standard pragma for long-running app stores (durability
        # trade-off is fine — we re-derive on missing rows anyway).
        with contextlib.suppress(sqlite3.DatabaseError):
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")

        if not first_init:
            absolute = first_absolute_path(self._conn, "dj_meta")
            if absolute is not None:
                self._conn.close()
                self._conn = None
                raise OldDjMetaCacheError(self._path, absolute)
            count = self._conn.execute("SELECT COUNT(*) FROM dj_meta").fetchone()[0]
            if count:
                logger.info("Loaded DJ meta cache: %d entries", count)

    def _row_to_meta(self, row: tuple) -> DjMeta:
        """Decode one SQLite row into DJ metadata."""
        intro, outro, analysed, beats_json, cues_json, intro_start = row
        try:
            beats = json.loads(beats_json or "[]")
            cue_dicts = json.loads(cues_json or "[]")
        except json.JSONDecodeError:
            beats, cue_dicts = [], []
        cues = [Cue(**cd) for cd in cue_dicts if isinstance(cd, dict)]
        return DjMeta(
            intro_end_s=float(intro or 0.0),
            outro_start_s=float(outro or 0.0),
            beats=[float(b) for b in beats],
            analysed=bool(analysed),
            cues=cues,
            intro_start_s=None if intro_start is None else float(intro_start),
        )

    def get(self, path: str) -> DjMeta:
        """Return the cached :class:`DjMeta` for *path*, or a fresh empty one."""
        key = self._key(path)
        with self._lock:
            if key in self._buf:
                return self._buf[key]
            if key in self._mem_cache:
                return self._mem_cache[key]
            now = self._clock()
            miss = self._misses.get(key)
            if miss is not None and now < miss[0]:
                return miss[1]
            if self._conn is None:
                self._warn_closed_locked("read")
                return DjMeta()
            row = self._conn.execute(
                "SELECT intro_end_s, outro_start_s, analysed, beats, cues, intro_start_s "
                "FROM dj_meta WHERE path = ?",
                (key,),
            ).fetchone()
            meta = self._row_to_meta(row) if row else DjMeta()
            if meta.analysed:
                self._mem_cache[key] = meta
                self._misses.pop(key, None)
            else:
                self._misses[key] = (now + self.MISS_TTL_S, meta)
            return meta

    def _warn_closed_locked(self, action: str) -> None:
        """Log, once, a *action* on the closed cache (hold the lock)."""
        if not self._warned_closed:
            self._warned_closed = True
            logger.warning("DJ meta cache %s after it was closed; ignoring it.", action)

    def set(self, path: str, meta: DjMeta) -> None:
        """Store *meta* under *path* and mark the cache dirty."""
        key = self._key(path)
        with self._lock:
            self._buf[key] = meta
            self._mem_cache[key] = meta
            self._misses.pop(key, None)
            self._dirty += 1

    def _write_buffer_locked(self) -> None:
        """UPSERT every buffered row.  Caller holds the lock and a transaction.

        The buffer itself is cleared by the caller once its transaction
        has committed, so a failed write leaves the pending rows intact.
        """
        assert self._conn is not None
        rows = [
            (
                path,
                float(meta.intro_end_s),
                float(meta.outro_start_s),
                int(bool(meta.analysed)),
                json.dumps([float(beat) for beat in meta.beats]),
                json.dumps([asdict(cue) for cue in meta.cues]),
                None if meta.intro_start_s is None else float(meta.intro_start_s),
            )
            for path, meta in self._buf.items()
        ]
        if rows:
            self._conn.executemany(
                "INSERT OR REPLACE INTO dj_meta "
                "(path, intro_end_s, outro_start_s, analysed, beats, cues, intro_start_s) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def flush(self, force: bool = False, batch: int = 25) -> None:
        """Persist pending writes if at least *batch* entries are dirty.

        UPSERT each pending row inside a single transaction, so a flush
        costs O(dirty) rather than O(total).

        Set *force* to flush regardless of pending count.
        """
        with self._lock:
            if not force and self._dirty < batch:
                return
            if not self._buf:
                self._dirty = 0
                return
            if self._conn is None:
                self._warn_closed_locked("written")
                self._buf.clear()
                self._dirty = 0
                return
            with immediate_transaction(self._conn):
                self._write_buffer_locked()
            self._buf.clear()
            self._dirty = 0

    def prune_to_paths(self, valid_paths: AbstractSet[str]) -> int:
        """Delete cache rows whose paths are not in *valid_paths*.

        Returns:
            Number of stale rows removed.
        """
        valid_keys = {self._key(path) for path in valid_paths}
        with self._lock:
            if self._conn is None:
                self._warn_closed_locked("pruned")
                return 0
            with immediate_transaction(self._conn):
                self._write_buffer_locked()
                existing = [str(row[0]) for row in self._conn.execute("SELECT path FROM dj_meta")]
                stale = [path for path in existing if path not in valid_keys]
                if stale:
                    self._conn.executemany(
                        "DELETE FROM dj_meta WHERE path = ?", [(path,) for path in stale]
                    )
            self._buf.clear()
            self._dirty = 0
            for path in stale:
                self._mem_cache.pop(path, None)
                self._misses.pop(path, None)
            return len(stale)

    def rekey_many(self, moves: Iterable[tuple[str, str]]) -> int:
        """Move cache rows to new paths after their files moved.

        Each ``(old, new)`` pair is applied only when a row exists under
        *old* and none under *new*, so running it again after a crash, or
        when the track was already analysed at *new*, changes nothing.  All
        pairs go in one transaction.

        Args:
            moves: ``(old, new)`` track paths, absolute or relative as
                :meth:`get` takes them.

        Returns:
            Number of rows re-keyed.
        """
        pairs = [(self._key(old), self._key(new)) for old, new in moves]
        with self._lock:
            if self._conn is None:
                self._warn_closed_locked("re-keyed")
                return 0
            rekeyed = 0
            with immediate_transaction(self._conn):
                self._write_buffer_locked()
                for old, new in pairs:
                    if old == new:
                        continue
                    taken = self._conn.execute(
                        "SELECT 1 FROM dj_meta WHERE path = ?", (new,)
                    ).fetchone()
                    if taken is not None:
                        continue
                    cur = self._conn.execute(
                        "UPDATE dj_meta SET path = ? WHERE path = ?", (new, old)
                    )
                    rekeyed += cur.rowcount
            self._buf.clear()
            self._dirty = 0
            for old, new in pairs:
                for key in (old, new):
                    self._mem_cache.pop(key, None)
                    self._misses.pop(key, None)
            return rekeyed

    def rekey(self, old: str, new: str) -> bool:
        """Move one cache row from *old* to *new*; see :meth:`rekey_many`.

        Returns:
            ``True`` when a row was re-keyed.
        """
        return self.rekey_many([(old, new)]) == 1

    def close(self) -> None:
        """Close the underlying SQLite connection.  Idempotent."""
        with self._lock:
            if self._conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.close()
                self._conn = None

    def __del__(self) -> None:
        """Best-effort cleanup for callers that drop a cache without closing."""
        with contextlib.suppress(Exception):
            self.close()


# Process-wide singleton — set by the player / server when they boot
_CACHE: DjMetaCache | None = None
_CACHE_LOCK = threading.Lock()


def get_cache(
    index_dir: Path | None = None,
    music_dir: Path | None = None,
) -> DjMetaCache | None:
    """Return the process-wide :class:`DjMetaCache` instance.

    Pass *index_dir* on the first call to initialise the cache; subsequent
    calls ignore the argument and return the same instance.

    Args:
        index_dir: Directory containing the FAISS index (the cache lives
            at ``<index_dir>/dj_meta.db``).  Required on first call.
        music_dir: Optional library root for portable relative cache keys.

    Returns:
        The shared cache, or ``None`` if uninitialised and no *index_dir*
        was provided.
    """
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is None and index_dir is not None:
            _CACHE = DjMetaCache(index_dir / "dj_meta.db", music_dir)
        return _CACHE


def close_cache() -> None:
    """Flush, close, and clear the process-wide cache; safe to call repeatedly."""
    global _CACHE
    with _CACHE_LOCK:
        cache, _CACHE = _CACHE, None
    if cache is None:
        return
    try:
        cache.flush(force=True)
    finally:
        cache.close()


atexit.register(close_cache)


_CUE_BLOCK_S = 0.5


def _block_rms(audio: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(rms, rolling_mean)`` for 0.5 s blocks of *audio*."""
    win = max(1, int(_CUE_BLOCK_S * sr))
    n_blocks = max(1, len(audio) // win)
    blocks = audio[: n_blocks * win].reshape(n_blocks, win)
    rms = np.sqrt(np.mean(blocks**2, axis=1) + 1e-12)
    window_blocks = max(1, int(2.0 / _CUE_BLOCK_S))
    kernel = np.ones(window_blocks) / window_blocks
    return rms, np.convolve(rms, kernel, mode="same")


def _detect_first_downbeat(beats: list[float], intro_end_s: float) -> Cue | None:
    """Return the first downbeat-aligned beat at or after *intro_end_s*."""
    for i, t in enumerate(beats):
        if i % 4 == 0 and t >= intro_end_s:
            return Cue(time_s=float(t), type="first_downbeat", source="auto")
    return None


def _detect_drop(
    rms: np.ndarray,
    rolling: np.ndarray,
    beats: list[float],
    intro_end_s: float,
    outro_start_s: float,
) -> Cue | None:
    """Return the loudest RMS spike (>=1.6× baseline) inside the body window."""
    intro_idx = int(intro_end_s / _CUE_BLOCK_S)
    outro_idx = int(outro_start_s / _CUE_BLOCK_S) if outro_start_s > 0 else len(rms)
    search_lo = max(intro_idx, 0)
    search_hi = min(outro_idx, len(rms))
    if search_hi - search_lo < 4:
        return None
    window = rms[search_lo:search_hi]
    roll_window = rolling[search_lo:search_hi]
    ratio = window / np.maximum(roll_window, 1e-6)
    peak_local = int(np.argmax(ratio))
    if ratio[peak_local] < 1.6:
        return None
    drop_t = (search_lo + peak_local) * _CUE_BLOCK_S
    if beats:
        drop_t = min(beats, key=lambda b: abs(b - drop_t))
    return Cue(time_s=float(drop_t), type="drop", source="auto")


def _longest_run(below: np.ndarray) -> tuple[int, int]:
    """Return the longest consecutive ``(start, end)`` of True values in *below*."""
    best_run = (0, 0)
    run_start = -1
    for i, b in enumerate(below):
        if b and run_start < 0:
            run_start = i
        elif not b and run_start >= 0:
            if i - run_start > best_run[1] - best_run[0]:
                best_run = (run_start, i)
            run_start = -1
    if run_start >= 0 and len(below) - run_start > best_run[1] - best_run[0]:
        best_run = (run_start, len(below))
    return best_run


def _detect_breakdown(rms: np.ndarray, intro_end_s: float, outro_start_s: float) -> Cue | None:
    """Return the deepest sustained dip in the middle third of the track."""
    third_lo = len(rms) // 3
    third_hi = 2 * len(rms) // 3
    body_lo = max(int(intro_end_s / _CUE_BLOCK_S), 0)
    body_hi = min(int(outro_start_s / _CUE_BLOCK_S), len(rms)) if outro_start_s > 0 else len(rms)
    body = rms[body_lo:body_hi] if body_hi > body_lo else rms
    body_median = float(np.median(body)) if len(body) > 0 else float(np.median(rms))
    if third_hi - third_lo < int(4.0 / _CUE_BLOCK_S):
        return None
    window = rms[third_lo:third_hi]
    below = window < (0.5 * body_median)
    best_run = _longest_run(below)
    if best_run[1] - best_run[0] < int(4.0 / _CUE_BLOCK_S):
        return None
    mid = (best_run[0] + best_run[1]) // 2
    return Cue(time_s=float((third_lo + mid) * _CUE_BLOCK_S), type="breakdown", source="auto")


def _detect_phrases(
    beats: list[float],
    intro_end_s: float,
    outro_start_s: float,
    duration: float,
) -> list[Cue]:
    """Return one cue per 32-beat phrase boundary inside the body window."""
    if len(beats) < 32:
        return []
    horizon = outro_start_s if outro_start_s > 0 else duration
    return [
        Cue(time_s=float(beats[i]), type="phrase", source="auto")
        for i in range(0, len(beats), 32)
        if intro_end_s <= beats[i] <= horizon
    ]


def _detect_outro_downbeat(
    beats: list[float],
    intro_end_s: float,
    outro_start_s: float,
) -> Cue | None:
    """Return the last downbeat-aligned beat before *outro_start_s*."""
    if not beats or outro_start_s <= 0:
        return None
    last_db = None
    for i, t in enumerate(beats):
        if i % 4 == 0 and t <= outro_start_s:
            last_db = t
    if last_db is None or last_db <= intro_end_s:
        return None
    return Cue(time_s=float(last_db), type="outro_downbeat", source="auto")


def detect_cues(
    audio: np.ndarray,
    sr: int,
    intro_end_s: float,
    outro_start_s: float,
    beats: list[float],
) -> list[Cue]:
    """Auto-detect cue points from a mono audio array."""
    if len(audio) < sr * 4:
        return []
    duration = len(audio) / max(1, sr)
    rms, rolling = _block_rms(audio, sr)
    if rms.max() <= 1e-6:
        return []

    cues: list[Cue] = []
    if (cue := _detect_first_downbeat(beats, intro_end_s)) is not None:
        cues.append(cue)
    if (cue := _detect_drop(rms, rolling, beats, intro_end_s, outro_start_s)) is not None:
        cues.append(cue)
    if (cue := _detect_breakdown(rms, intro_end_s, outro_start_s)) is not None:
        cues.append(cue)
    cues.extend(_detect_phrases(beats, intro_end_s, outro_start_s, duration))
    if (cue := _detect_outro_downbeat(beats, intro_end_s, outro_start_s)) is not None:
        cues.append(cue)
    cues.sort(key=lambda c: c.time_s)
    return cues


_CUE_PRIORITY: dict[str, int] = {
    "user": 4,
    "mixxx": 3,
    "rekordbox": 3,
    "serato": 3,
    "traktor": 3,
    "auto": 1,
}

MARKER_CUE_TYPES: frozenset[str] = frozenset(
    {"intro_start", "intro_end", "outro_start", "outro_end"}
)
"""Cue types that carry an intro or outro marker set in DJ software.

See :mod:`autodj.dj_cues_import` for which program provides which.
"""

# The DjMeta field each marker sets (DjMeta has no outro end).
_MARKER_FIELDS: dict[str, str] = {
    "intro_start": "intro_start_s",
    "intro_end": "intro_end_s",
    "outro_start": "outro_start_s",
}


def _cue_rank(cue: Cue) -> tuple[int, bool]:
    """Which of two cues at the same time to keep: source first, then a marker."""
    return (_CUE_PRIORITY.get(cue.source, 0), cue.type in MARKER_CUE_TYPES)


def merge_cues(*sources: list[Cue]) -> list[Cue]:
    """Merge cue lists from multiple sources, sorted, dedup'd by time.

    When two cues fall within ~250 ms of each other, the one with the
    higher-priority source wins (user / DJ-software beats auto); between
    equal sources an intro or outro marker beats any other cue, because
    Mixxx puts its main cue and intro start on the same first sound.

    Args:
        *sources: Lists of cues to merge.  Order does not matter.

    Returns:
        New sorted list of cues.
    """
    flat: list[Cue] = []
    for src in sources:
        flat.extend(src)
    flat.sort(key=lambda c: c.time_s)
    out: list[Cue] = []
    for c in flat:
        if out and abs(c.time_s - out[-1].time_s) < 0.25:
            if _cue_rank(c) > _cue_rank(out[-1]):
                out[-1] = c
            continue
        out.append(c)
    return out


def imported_markers(cues: list[Cue]) -> dict[str, float]:
    """The intro and outro markers the user or DJ software set, by cue type.

    Only cues of a :data:`MARKER_CUE_TYPES` type that were not
    auto-detected count.  When several sources set the same marker the
    priority of :func:`merge_cues` decides (user over DJ software), then
    the earliest time.

    Returns:
        ``{cue type: time in seconds}`` for each marker found.
    """
    best: dict[str, Cue] = {}
    for cue in cues:
        if cue.type not in MARKER_CUE_TYPES or cue.source == "auto":
            continue
        held = best.get(cue.type)
        rank = (_CUE_PRIORITY.get(cue.source, 0), -cue.time_s)
        if held is None or rank > (_CUE_PRIORITY.get(held.source, 0), -held.time_s):
            best[cue.type] = cue
    return {kind: cue.time_s for kind, cue in best.items()}


def apply_imported_markers(meta: DjMeta) -> None:
    """Let the user's intro and outro markers set *meta*'s mix points, in place.

    ``intro_start_s``, ``intro_end_s`` and ``outro_start_s`` take the
    times of the imported ``intro_start``, ``intro_end`` and
    ``outro_start`` cues (:func:`imported_markers`), so they decide where
    the crossfade starts and how long it is; fields without an imported
    marker keep the detected value.  A detected intro end at or before
    an imported intro start is dropped (``0.0``, no intro), as is a
    detected intro start at or after an imported intro end.
    """
    markers = imported_markers(meta.cues)
    for kind, field_name in _MARKER_FIELDS.items():
        if kind in markers:
            setattr(meta, field_name, markers[kind])
    if 0.0 < meta.intro_end_s <= (meta.intro_start_s or 0.0):
        if "intro_end" in markers and "intro_start" not in markers:
            meta.intro_start_s = 0.0
        else:
            meta.intro_end_s = 0.0


# ---------------------------------------------------------------------------
# Drops and breakdowns that can time a drop-to-drop mix
# ---------------------------------------------------------------------------

# A detected drop times a drop mix only when the second after its beat is
# at least this many times as loud (RMS) as the second before it: 18 dB.
# _detect_drop's floor, a half-second block 1.6 times its 2 s rolling
# mean, is a jump of 4 (12 dB) for a drop that starts on a block.
_CONFIDENT_JUMP = 8.0
# ... and when the level then holds (3/4 of that first second's) for
# the next three seconds.
_DROP_HOLD_S = 3.0
_DROP_LABEL = re.compile(r"\bdrop\b", re.IGNORECASE)
_BREAKDOWN_LABEL = re.compile(r"\bbreak(?:down)?\b", re.IGNORECASE)


def _set_by_hand(cue: Cue, kind: str, label: re.Pattern[str]) -> bool:
    """Whether *cue* is a *kind* cue set by the user or in DJ software.

    A cue of that type counts, and so does a hot or memory cue whose name
    says so ("Drop", "Breakdown 2").  Auto-detected cues never do.
    """
    if cue.source == "auto":
        return False
    return cue.type == kind or (
        cue.type not in MARKER_CUE_TYPES and label.search(cue.label) is not None
    )


def confident_drop(audio: np.ndarray, sr: int, at_s: float, beats: list[float]) -> float | None:
    """The beat a detected drop starts on, when it is clearly a drop.

    The detector's drop is the start of a half-second block, moved to the
    nearest beat, so the real onset can be a beat either side.  Each beat
    from 0.75 s before *at_s* to 1 s after it is measured: the level of
    the second after it over the second before it.  The drop is the beat
    where that jump is largest, and it counts only when the jump is at
    least :data:`_CONFIDENT_JUMP` and the level holds for
    :data:`_DROP_HOLD_S` more (a single loud hit is not a drop).

    Args:
        audio: The track, mono.
        sr: Its sample rate.
        at_s: The detected drop.
        beats: The track's beat grid in seconds.

    Returns:
        The drop's beat in seconds, or ``None`` when the drop is not clear.
    """

    def level(lo_s: float, hi_s: float) -> float:
        part = audio[max(0, round(lo_s * sr)) : max(0, round(hi_s * sr))]
        return float(np.sqrt(np.mean(part**2))) if len(part) else 0.0

    def jump(beat: float) -> float:
        return level(beat, beat + 1.0) / (level(beat - 1.0, beat) + 1e-9)

    candidates = [b for b in beats if at_s - 0.75 <= b <= at_s + 1.0 and b >= 1.0]
    if not candidates:
        return None
    onset = max(candidates, key=jump)
    held = level(onset + 1.0, onset + 1.0 + _DROP_HOLD_S)
    enough = (len(audio) - onset * sr) >= (1.0 + _DROP_HOLD_S) * sr
    if jump(onset) < _CONFIDENT_JUMP or not enough or held < 0.75 * level(onset, onset + 1.0):
        return None
    return onset


def mix_drops(cues: list[Cue], audio: np.ndarray, sr: int, beats: list[float]) -> list[float]:
    """The drops of a track reliable enough to time a drop mix by, ascending.

    Drops set by hand or in DJ software (:func:`_set_by_hand`) come first:
    when there are any, they are the only ones.  Otherwise a detected
    drop counts when :func:`confident_drop` finds it clear.

    Args:
        cues: The track's cues.
        audio: The track, mono, for checking a detected drop.
        sr: Its sample rate.
        beats: Its beat grid in seconds.

    Returns:
        Drop times in seconds.
    """
    marked = sorted(cue.time_s for cue in cues if _set_by_hand(cue, "drop", _DROP_LABEL))
    if marked:
        return marked
    found = (
        confident_drop(audio, sr, cue.time_s, beats)
        for cue in cues
        if cue.type == "drop" and cue.source == "auto"
    )
    return sorted(t for t in found if t is not None)


def mix_anchors(cues: list[Cue], audio: np.ndarray, sr: int, beats: list[float]) -> list[float]:
    """Section starts of a track that a drop mix can count phrases from, ascending.

    Its reliable drops (:func:`mix_drops`) and the breakdowns set by hand
    or in DJ software; a detected breakdown is the middle of a quiet
    stretch, not a section start, so it never counts.
    """
    breakdowns = [cue.time_s for cue in cues if _set_by_hand(cue, "breakdown", _BREAKDOWN_LABEL)]
    return sorted([*mix_drops(cues, audio, sr, beats), *breakdowns])


def analyse_audio(audio: np.ndarray, sr: int) -> DjMeta:
    """Run all DJ-meta detectors on *audio* and return a :class:`DjMeta`.

    Convenience wrapper used by the player on first encounter of a track.

    Args:
        audio: Mono float32 audio array.
        sr: Sample rate in Hz.

    Returns:
        A populated :class:`DjMeta` (always ``analysed=True``).
    """
    intro_end, outro_start = detect_intro_outro(audio, sr)
    beats = detect_beat_grid(audio, sr)
    cues = detect_cues(audio, sr, intro_end, outro_start, beats)
    return DjMeta(
        intro_start_s=detect_intro_start(audio, sr),
        intro_end_s=intro_end,
        outro_start_s=outro_start,
        beats=beats,
        analysed=True,
        cues=cues,
    )
