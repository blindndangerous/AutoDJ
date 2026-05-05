"""DJ-grade audio analysis: intro/outro, beat grid, harmonic mixing, sidecar cache.

This module powers the pro-DJ features layered on top of the basic
similarity engine:

- :func:`detect_intro_outro` — find the seconds at which the perceived
  intro ends and the outro starts, used for outro→intro-aligned crossfade.
- :func:`detect_beat_grid` — extract beat + downbeat positions, used for
  phrase-aligned crossfade (snap mix point to an 8-bar boundary).
- :func:`harmonic_compatible` — Camelot wheel test that lets the picker
  filter candidates to harmonically-compatible keys.
- :class:`DjMetaCache` — JSON-backed sidecar (``index/dj_meta.json``) so
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

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Intro / outro detection
# ---------------------------------------------------------------------------


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
    if ref <= 1e-6:
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
    if outro_start <= intro_end:
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

    try:
        import librosa
    except ImportError:  # pragma: no cover — librosa required by full install
        return []

    try:  # pragma: no cover — librosa internals
        _tempo, beat_frames = librosa.beat.beat_track(y=audio, sr=sr)
        beat_times = librosa.frames_to_time(beat_frames, sr=sr)
        return [float(t) for t in beat_times]
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
    else:
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
    if key not in table:
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


def harmonic_compatible(
    key_a: int,
    mode_a: int,
    key_b: int,
    mode_b: int,
    mode: str = "compatible",
) -> bool:
    """Return ``True`` when tracks A and B are harmonically mixable.

    The *mode* parameter selects which Camelot rule(s) to apply:

    - ``"off"``           — no filter; always True.
    - ``"compatible"``    — same position, ±1 around the wheel, OR the
      relative major/minor (same number, opposite side).  The classic
      "all green" Camelot rule, default for most DJs.
    - ``"strict"``        — same Camelot position only (e.g. 8A → 8A).
      The most conservative — perfectly key-locked sets.
    - ``"neighbour"``     — same side only, ±1 around the wheel (no
      relative mode swap).  Smooth same-mood progression.
    - ``"mood_change"``   — relative major/minor only (same number,
      opposite side).  Use it to punctuate a set with a mood flip.
    - ``"energy_boost"``  — same side, +2 around the wheel.  The
      energy-lift trick: each compatible track is two semitones up.

    Tracks with unknown key/mode (``-1``) always return ``True``.

    Args:
        key_a: Chromatic key of track A (0–11, or -1 unknown).
        mode_a: Mode of track A (1 major, 0 minor, -1 unknown).
        key_b: Chromatic key of track B.
        mode_b: Mode of track B.
        mode: Compatibility rule.  Unrecognised modes fall back to
            ``"compatible"``.

    Returns:
        ``True`` if the two tracks satisfy the selected rule, or if
        either side has unknown key/mode, or if mode is ``"off"``.
    """
    if mode == "off":
        return True
    if key_a < 0 or mode_a < 0 or key_b < 0 or mode_b < 0:
        return True

    pos_a = camelot_position(key_a, mode_a)
    pos_b = camelot_position(key_b, mode_b)
    if pos_a is None or pos_b is None:
        return True

    num_a, side_a = pos_a
    num_b, side_b = pos_b

    if mode == "strict":
        return pos_a == pos_b
    if mode == "mood_change":
        return num_a == num_b and side_a != side_b
    if mode == "neighbour":
        if side_a != side_b:
            return False
        diff = abs(num_a - num_b)
        return diff == 1 or diff == 11
    if mode == "energy_boost":
        if side_a != side_b:
            return False
        # +2 forward around the wheel (12 wraps to 1) — both directions
        # qualify so the picker has more candidates.
        diff = abs(num_a - num_b)
        return diff == 2 or diff == 10
    # "compatible" (default) — original union of the three classic rules.
    if pos_a == pos_b:
        return True
    if num_a == num_b and side_a != side_b:
        return True
    if side_a == side_b:
        diff = abs(num_a - num_b)
        if diff == 1 or diff == 11:
            return True
    return False


def camelot_label(key: int, mode: int) -> str:
    """Return a human label like ``"8A"`` (or ``"--"`` for unknown)."""
    pos = camelot_position(key, mode)
    if pos is None:
        return "--"
    return f"{pos[0]}{pos[1]}"


# ---------------------------------------------------------------------------
# Cache (JSON sidecar) — keyed by track path
# ---------------------------------------------------------------------------


@dataclass
class DjMeta:
    """Per-track DJ analysis cache entry.

    Attributes:
        intro_end_s: Seconds at which the intro ends.  ``0.0`` = no intro
            detected (or detection has not been run yet).
        outro_start_s: Seconds at which the outro starts.  ``0.0`` = no
            outro detected.
        beats: Beat-onset timestamps in seconds.  Empty list = unanalysed.
        analysed: ``True`` once detection has run, even if results are
            empty / zero — distinguishes "we tried and there's nothing"
            from "we haven't tried yet".
    """

    intro_end_s: float = 0.0
    outro_start_s: float = 0.0
    beats: list[float] = field(default_factory=list)
    analysed: bool = False


class DjMetaCache:
    """JSON-backed sidecar cache for :class:`DjMeta` keyed by track path.

    Stored at ``index/dj_meta.json`` next to the FAISS index.  A single
    process-wide instance is shared via :func:`get_cache`.  All operations
    are thread-safe (the player loads tracks on a background thread while
    the server reads cache state for the API).

    Example:
        >>> cache = DjMetaCache(Path("index/dj_meta.json"))
        >>> cache.get("song.flac")
        DjMeta(analysed=False, ...)
        >>> cache.set("song.flac", DjMeta(intro_end_s=12.3, analysed=True))
        >>> cache.flush()
    """

    def __init__(self, sidecar_path: Path) -> None:
        """Initialise the cache, loading any existing sidecar."""
        self._path = sidecar_path
        self._lock = threading.Lock()
        self._dirty = 0
        self._data: dict[str, DjMeta] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("DJ meta cache unreadable, starting fresh: %s", exc)
            return
        for k, v in raw.items():
            try:
                self._data[k] = DjMeta(**v)
            except TypeError:
                continue
        logger.info("Loaded DJ meta cache: %d entries", len(self._data))

    def get(self, path: str) -> DjMeta:
        """Return the cached :class:`DjMeta` for *path*, or a fresh empty one."""
        with self._lock:
            return self._data.get(path, DjMeta())

    def set(self, path: str, meta: DjMeta) -> None:
        """Store *meta* under *path* and mark the cache dirty."""
        with self._lock:
            self._data[path] = meta
            self._dirty += 1

    def flush(self, force: bool = False, batch: int = 25) -> None:
        """Write the cache to disk if at least *batch* entries are dirty.

        Uses atomic temp+rename so a partial write can't corrupt the file.
        Set *force* to flush regardless of pending count.
        """
        with self._lock:
            if not force and self._dirty < batch:
                return
            if not self._data:
                self._dirty = 0
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            payload = {k: asdict(v) for k, v in self._data.items()}
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
            self._dirty = 0


# Process-wide singleton — set by the player / server when they boot
_CACHE: DjMetaCache | None = None
_CACHE_LOCK = threading.Lock()


def get_cache(index_dir: Path | None = None) -> DjMetaCache | None:
    """Return the process-wide :class:`DjMetaCache` instance.

    Pass *index_dir* on the first call to initialise the cache; subsequent
    calls ignore the argument and return the same instance.

    Args:
        index_dir: Directory containing the FAISS index (the cache lives
            at ``<index_dir>/dj_meta.json``).  Required on first call.

    Returns:
        The shared cache, or ``None`` if uninitialised and no *index_dir*
        was provided.
    """
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is None and index_dir is not None:
            _CACHE = DjMetaCache(index_dir / "dj_meta.json")
        return _CACHE


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
    return DjMeta(
        intro_end_s=intro_end,
        outro_start_s=outro_start,
        beats=beats,
        analysed=True,
    )
