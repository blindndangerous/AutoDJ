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


def _gpu_onset_envelope(audio: np.ndarray, sr: int) -> tuple[np.ndarray, int] | None:
    """Compute a log-mel onset envelope on GPU via torchaudio.

    Returns ``(envelope, hop_length)`` so the caller can hand it to
    librosa's CPU-side beat tracker (the DP step is cheap; the mel
    spectrogram is the bulk of the cost).  Returns ``None`` when CUDA
    or torchaudio is unavailable, or the user has disabled GPU work
    (``AUTODJ_GPU=0`` global, ``AUTODJ_DJMETA_GPU=0`` per-step).
    """
    from autodj.compute import gpu_available

    if os.environ.get("AUTODJ_DJMETA_GPU", "1") == "0":
        return None
    if not gpu_available():
        return None
    try:
        import torch
        import torchaudio
    except ImportError:
        return None

    hop = 512
    n_fft = 2048
    try:
        mel = torchaudio.transforms.MelSpectrogram(
            sample_rate=sr,
            n_fft=n_fft,
            hop_length=hop,
            n_mels=128,
            power=1.0,
        ).to("cuda")
        with torch.no_grad():
            x = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32)).to("cuda")
            S = mel(x.unsqueeze(0)).clamp_min(1e-10).log()
            diff = (S[..., 1:] - S[..., :-1]).clamp_min(0.0)
            env = diff.mean(dim=1).squeeze(0).contiguous().cpu().numpy()
        return env.astype(np.float32, copy=False), hop
    except Exception as exc:  # pragma: no cover — GPU runtime errors
        logger.debug("GPU onset envelope failed, falling back to CPU: %s", exc)
        return None


def detect_beat_grid(audio: np.ndarray, sr: int) -> list[float]:
    """Return a list of beat-onset timestamps in seconds.

    Wraps :func:`librosa.beat.beat_track` with sane defaults.  The
    returned grid is dense — one entry per beat — so phrase-aligned
    crossfade can snap to any 8 / 16 / 32 -beat boundary.

    On hosts with CUDA + torchaudio, the mel-spectrogram / onset
    envelope step runs on GPU (typically the bulk of beat-track cost)
    and the cheap DP beat tracker still runs on CPU via librosa.
    Disable with ``AUTODJ_DJMETA_GPU=0``.

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
        logger.warning(
            "librosa is not installed; beat / cue detection skipped.  "
            "Install with: uv add librosa  (or: uv sync --extra all).",
        )
        return []

    gpu_env = _gpu_onset_envelope(audio, sr)
    try:  # pragma: no cover — librosa internals
        if gpu_env is not None:
            env, hop = gpu_env
            _tempo, beat_frames = librosa.beat.beat_track(onset_envelope=env, sr=sr, hop_length=hop)
            beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop)
        else:
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


def _hm_strict(pos_a: tuple[int, str], pos_b: tuple[int, str]) -> bool:
    """``strict`` rule: identical Camelot position."""
    return pos_a == pos_b


def _hm_mood_change(pos_a: tuple[int, str], pos_b: tuple[int, str]) -> bool:
    """``mood_change`` rule: relative major/minor (same number, opposite side)."""
    return pos_a[0] == pos_b[0] and pos_a[1] != pos_b[1]


def _hm_neighbour(pos_a: tuple[int, str], pos_b: tuple[int, str]) -> bool:
    """``neighbour`` rule: same side, ±1 around the wheel."""
    if pos_a[1] != pos_b[1]:
        return False
    diff = abs(pos_a[0] - pos_b[0])
    return diff in (1, 11)


def _hm_energy_boost(pos_a: tuple[int, str], pos_b: tuple[int, str]) -> bool:
    """``energy_boost`` rule: same side, ±2 around the wheel."""
    if pos_a[1] != pos_b[1]:
        return False
    diff = abs(pos_a[0] - pos_b[0])
    return diff in (2, 10)


def _hm_compatible(pos_a: tuple[int, str], pos_b: tuple[int, str]) -> bool:
    """``compatible`` rule: union of strict + mood_change + neighbour."""
    return _hm_strict(pos_a, pos_b) or _hm_mood_change(pos_a, pos_b) or _hm_neighbour(pos_a, pos_b)


_HARMONIC_RULES = {
    "strict": _hm_strict,
    "mood_change": _hm_mood_change,
    "neighbour": _hm_neighbour,
    "energy_boost": _hm_energy_boost,
    "compatible": _hm_compatible,
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
    if pos_a is None or pos_b is None:
        return True
    rule = _HARMONIC_RULES.get(mode, _hm_compatible)
    return rule(pos_a, pos_b)


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


# ---------------------------------------------------------------------------
# Cache (JSON sidecar) — keyed by track path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cue points — hot/memory cues, drops, breakdowns, phrase markers
# ---------------------------------------------------------------------------

# Recognised cue types.  Auto-detected types are emitted by
# :func:`detect_cues`; user / DJ-software types are emitted by the
# importer module ``autodj.dj_cues_import``.
CUE_TYPES: tuple[str, ...] = (
    "first_downbeat",
    "drop",
    "breakdown",
    "build",
    "phrase",
    "outro_downbeat",
    "user",
)


@dataclass
class Cue:
    """A single cue point on a track.

    Attributes:
        time_s: Cue timestamp in seconds from track start.
        type: One of :data:`CUE_TYPES` (or any custom string from a
            DJ-software import — the player only special-cases the
            built-ins; unknown types render as plain markers).
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
        intro_end_s: Seconds at which the intro ends.  ``0.0`` = no intro
            detected (or detection has not been run yet).
        outro_start_s: Seconds at which the outro starts.  ``0.0`` = no
            outro detected.
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

    def _load(self) -> None:  # pragma: no cover -- sidecar loader, exercised via cache tests
        """Read the sidecar JSON into the in-memory cache (no-op when missing)."""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("DJ meta cache unreadable, starting fresh: %s", exc)
            return
        for k, v in raw.items():
            try:
                # Cue dicts roundtrip from asdict(); rehydrate them
                # before constructing DjMeta so the ``cues`` field has
                # proper Cue instances rather than dicts.  Missing /
                # legacy entries (no ``cues`` key at all) fall through
                # to the field default of ``[]``.
                cue_dicts = v.pop("cues", []) if isinstance(v, dict) else []
                cues = [Cue(**cd) for cd in cue_dicts if isinstance(cd, dict)]
                # Tolerate forward-compatible new keys by stripping any
                # the current dataclass doesn't know.  Keeps old caches
                # readable when the sidecar schema gains fields.
                allowed = {f for f in DjMeta.__dataclass_fields__ if f != "cues"}
                clean = {fk: fv for fk, fv in v.items() if fk in allowed}
                self._data[k] = DjMeta(cues=cues, **clean)
            except (TypeError, ValueError):
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


def merge_cues(*sources: list[Cue]) -> list[Cue]:
    """Merge cue lists from multiple sources, sorted, dedup'd by time.

    When two cues fall within ~250 ms of each other, the one with the
    higher-priority source wins (user / DJ-software beats auto).

    Args:
        *sources: Lists of cues to merge.  Order does not matter.

    Returns:
        New sorted list of cues.
    """
    priority = {"user": 4, "mixxx": 3, "rekordbox": 3, "serato": 3, "traktor": 3, "auto": 1}
    flat: list[Cue] = []
    for src in sources:
        flat.extend(src)
    flat.sort(key=lambda c: c.time_s)
    out: list[Cue] = []
    for c in flat:
        if out and abs(c.time_s - out[-1].time_s) < 0.25:
            if priority.get(c.source, 0) > priority.get(out[-1].source, 0):
                out[-1] = c
            continue
        out.append(c)
    return out


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
        intro_end_s=intro_end,
        outro_start_s=outro_start,
        beats=beats,
        analysed=True,
        cues=cues,
    )
