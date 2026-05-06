"""Crossfade audio player with keyboard controls and Rich terminal display.

Plays tracks in a continuous loop using two threads: one for playback and
one for pre-loading the next track.  When the current track has fewer than
``crossfade_seconds`` remaining, the player mixes in the start of the next
track with a linear fade (fade-out on the current track, fade-in on the next).

Keyboard controls (via ``pynput``):
- ``Space`` — pause / resume
- ``N`` — skip to next song immediately
- ``Q`` — quit

Example:
    >>> from autodj.config import load_config
    >>> from autodj.model import load_model, download_model_if_needed
    >>> from autodj.similarity import SimilarityIndex
    >>> from autodj.player import Player
    >>> cfg = load_config()
    >>> sim = SimilarityIndex.from_index_dir(cfg.index.index_dir)
    >>> wrapper = load_model(download_model_if_needed(cfg.model, cfg.index))
    >>> Player(cfg, sim, wrapper).run(seed_entry=None)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np
from rich.console import Console
from rich.live import Live
from rich.panel import Panel

# Heavy / platform-specific audio deps are imported with graceful None
# fallback so hosts without them (a NAS running `serve --no-playback`)
# can still construct a `Player` for track-picking.  Functions that
# need real audio import lazily on first use.
try:
    import soundfile as _sf_mod

    sf: Any = _sf_mod
except ImportError:  # pragma: no cover — minimal install path
    sf = None
try:
    import sounddevice as _sd_mod

    sd: Any = _sd_mod
except ImportError:  # pragma: no cover
    sd = None

from autodj.indexer import IndexEntry

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig
    from autodj.dj_meta import DjMeta
    from autodj.presets import Preset
    from autodj.similarity import SimilarityIndex

logger = logging.getLogger(__name__)

# Default output sample rate; sounddevice converts if the device differs.
_DEFAULT_SR = 44_100

# Keyboard seek step and volume increment
_SEEK_SECONDS = 10
_VOLUME_STEP = 0.05

# Shared Rich console — used for the Live status panel and transient log lines
_CONSOLE = Console()


def _fmt_time(seconds: float) -> str:
    """Format a duration in seconds as ``MM:SS``.

    Args:
        seconds: Non-negative duration in seconds.

    Returns:
        String of the form ``"03:47"``.
    """
    m, s = divmod(max(0, int(seconds)), 60)
    return f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Crossfade math (pure functions — testable without audio hardware)
# ---------------------------------------------------------------------------


def _make_fade_out(n_samples: int) -> np.ndarray:
    """Generate a linear fade-out envelope of length *n_samples*.

    The envelope starts at 1.0 and decreases linearly to 0.0.

    Args:
        n_samples: Number of samples in the envelope.

    Returns:
        float32 numpy array of shape ``(n_samples,)`` ranging ``[1.0, 0.0]``.
    """
    return np.linspace(1.0, 0.0, n_samples, dtype=np.float32)


def _make_fade_in(n_samples: int) -> np.ndarray:
    """Generate a linear fade-in envelope of length *n_samples*.

    The envelope starts at 0.0 and increases linearly to 1.0.

    Args:
        n_samples: Number of samples in the envelope.

    Returns:
        float32 numpy array of shape ``(n_samples,)`` ranging ``[0.0, 1.0]``.
    """
    return np.linspace(0.0, 1.0, n_samples, dtype=np.float32)


def _apply_crossfade(
    audio_a: np.ndarray,
    audio_b: np.ndarray,
    crossfade_samples: int,
) -> np.ndarray:
    """Mix the tail of *audio_a* with the head of *audio_b* using linear fades.

    The overlap region is ``crossfade_samples`` long.  In that region,
    *audio_a* fades out from 1.0 to 0.0 while *audio_b* fades in from 0.0
    to 1.0.  The two faded signals are summed in the overlap.

    Args:
        audio_a: Mono float32 audio array for the outgoing track.
        audio_b: Mono float32 audio array for the incoming track.
        crossfade_samples: Length of the overlap region in samples.
            Pass ``0`` for an instant cut (equivalent to concatenation).

    Returns:
        A new float32 array of length
        ``len(audio_a) + len(audio_b) - crossfade_samples``.

    Raises:
        ValueError: If *crossfade_samples* is longer than either input.
    """
    if crossfade_samples == 0:
        return np.concatenate([audio_a, audio_b]).astype(np.float32)

    if crossfade_samples > len(audio_a) or crossfade_samples > len(audio_b):
        raise ValueError(
            f"crossfade_samples ({crossfade_samples}) exceeds one or both audio arrays "
            f"(len_a={len(audio_a)}, len_b={len(audio_b)})"
        )

    fade_out = _make_fade_out(crossfade_samples)
    fade_in = _make_fade_in(crossfade_samples)

    # Regions
    a_body = audio_a[: len(audio_a) - crossfade_samples]
    a_tail = audio_a[len(audio_a) - crossfade_samples :]
    b_head = audio_b[:crossfade_samples]
    b_body = audio_b[crossfade_samples:]

    overlap = (a_tail * fade_out) + (b_head * fade_in)

    return np.concatenate([a_body, overlap, b_body]).astype(np.float32)


# ---------------------------------------------------------------------------
# EQ-ducked crossfade (pro-DJ style: bass low-pass on outgoing while
# incoming bass rises, prevents bass-clash mush in the overlap)
# ---------------------------------------------------------------------------


def _apply_crossfade_ducked(
    audio_a: np.ndarray,
    audio_b: np.ndarray,
    crossfade_samples: int,
    sample_rate: int,
    bass_cutoff_hz: float = 180.0,
) -> np.ndarray:
    """Crossfade with bass-frequency ducking on the outgoing track.

    During the overlap, the outgoing track's low frequencies are
    progressively attenuated via a Butterworth low-shelf cut (low-pass
    sweep), while the incoming track fades in normally.  This eliminates
    the muddy bass build-up that two simultaneously-playing tracks
    produce in the sub-200 Hz range — the core trick used by pro DJs
    when manually mixing.

    Falls back to plain :func:`_apply_crossfade` if scipy is unavailable
    or the crossfade region is too short for filter design.

    Args:
        audio_a: Mono float32 audio array for the outgoing track.
        audio_b: Mono float32 audio array for the incoming track.
        crossfade_samples: Length of the overlap region in samples.
        sample_rate: Sample rate of both audio arrays in Hz.
        bass_cutoff_hz: Frequency below which the outgoing track is
            progressively attenuated during the overlap.  Default 180 Hz
            covers kick drums and sub-bass.

    Returns:
        Float32 array with the EQ-ducked crossfade applied.
    """
    if crossfade_samples == 0:
        return np.concatenate([audio_a, audio_b]).astype(np.float32)
    if crossfade_samples > len(audio_a) or crossfade_samples > len(audio_b):
        return _apply_crossfade(audio_a, audio_b, crossfade_samples)

    try:
        from scipy.signal import butter, sosfilt
    except ImportError:  # pragma: no cover — scipy required by full install
        return _apply_crossfade(audio_a, audio_b, crossfade_samples)

    a_body = audio_a[: len(audio_a) - crossfade_samples]
    a_tail = audio_a[len(audio_a) - crossfade_samples :].astype(np.float32, copy=True)
    b_head = audio_b[:crossfade_samples].astype(np.float32, copy=False)
    b_body = audio_b[crossfade_samples:]

    # Build a 4th-order Butterworth high-pass at bass_cutoff_hz.  Applying
    # it gradually (mixed with the unfiltered tail) sweeps the bass out of
    # the outgoing track during the overlap.
    nyquist = sample_rate / 2.0
    cutoff_norm = max(1e-4, min(0.99, bass_cutoff_hz / nyquist))
    try:
        sos = butter(4, cutoff_norm, btype="high", output="sos")
        a_tail_hp = sosfilt(sos, a_tail).astype(np.float32)
    except (ValueError, RuntimeError):  # pragma: no cover — degenerate sample rate
        return _apply_crossfade(audio_a, audio_b, crossfade_samples)

    # Bass-duck envelope: 0.0 = full bass, 1.0 = bass fully removed.
    # Ramp follows a quarter-sine for a smoother feel than a straight line.
    t = np.linspace(0.0, 1.0, crossfade_samples, dtype=np.float32)
    bass_remove = np.sin(t * (np.pi / 2.0)).astype(np.float32)

    # Mix unfiltered tail with fully-bass-cut tail by the duck envelope
    a_ducked = a_tail * (1.0 - bass_remove) + a_tail_hp * bass_remove

    # Standard amplitude fades on top of the ducking
    fade_out = _make_fade_out(crossfade_samples)
    fade_in = _make_fade_in(crossfade_samples)

    overlap = (a_ducked * fade_out) + (b_head * fade_in)
    # Hard-limit to ±1.0 — even with bass-ducking, two bright tracks can sum
    # above full scale during the overlap on densely arranged music.
    np.clip(overlap, -1.0, 1.0, out=overlap)
    return np.concatenate([a_body, overlap, b_body]).astype(np.float32)


# ---------------------------------------------------------------------------
# Beatmatch (tempo-aligned crossfade)
# ---------------------------------------------------------------------------


def _time_stretch(audio: np.ndarray, ratio: float) -> np.ndarray:
    """Pitch-preserving time-stretch via librosa.

    Args:
        audio: Mono float32 audio array.
        ratio: Output_duration / input_duration.  Values >1 slow the
            track down (longer); values <1 speed it up (shorter).

    Returns:
        Stretched float32 array.  Falls back to the input array if
        librosa.effects.time_stretch raises (e.g. too-short audio).
    """
    if abs(ratio - 1.0) < 0.01:
        return audio
    try:
        import librosa

        # librosa's parameter is "rate" = playback speed = 1/ratio
        return librosa.effects.time_stretch(y=audio, rate=1.0 / ratio).astype(np.float32)
    except Exception as exc:
        logger.debug("Time-stretch failed (ratio=%.3f): %s", ratio, exc)
        return audio


def beatmatch_incoming(
    audio_b: np.ndarray,
    bpm_a: float,
    bpm_b: float,
    max_stretch: float = 0.08,
) -> tuple[np.ndarray, float]:
    """Time-stretch the incoming track so its BPM matches the outgoing track.

    Refuses to stretch beyond *max_stretch* (default ±8 %) — bigger
    adjustments sound noticeably warped and aren't typical DJ practice.
    Returns the stretched audio and the actual ratio applied (1.0 = no
    change).

    Args:
        audio_b: Mono float32 audio of the incoming track.
        bpm_a: BPM of the outgoing track.  Anything ``<= 0`` disables matching.
        bpm_b: BPM of the incoming track.  Anything ``<= 0`` disables matching.
        max_stretch: Maximum allowed ``|ratio - 1|``.  0.08 = ±8 %.

    Returns:
        Tuple ``(stretched_audio, ratio)``.
    """
    if bpm_a <= 0 or bpm_b <= 0:
        return audio_b, 1.0
    ratio = bpm_b / bpm_a
    if abs(ratio - 1.0) > max_stretch:
        return audio_b, 1.0
    return _time_stretch(audio_b, ratio), ratio


# ---------------------------------------------------------------------------
# Filter sweep (low-pass / high-pass automation)
# ---------------------------------------------------------------------------


def apply_filter_sweep(
    audio: np.ndarray,
    sample_rate: int,
    start_hz: float,
    end_hz: float,
    filter_type: str = "lowpass",
    n_steps: int = 32,
) -> np.ndarray:
    """Apply a swept-cutoff biquad filter to *audio*.

    Splits *audio* into *n_steps* equal-length blocks, designs a fresh
    Butterworth filter for each block at a cutoff that linearly
    interpolates between *start_hz* and *end_hz*, and concatenates the
    filtered blocks.  Step boundaries are smoothed with a 32-sample
    crossfade to hide any click.

    Falls back to the unfiltered input when scipy is unavailable or the
    cutoff range is invalid.

    Args:
        audio: Mono float32 audio array.
        sample_rate: Sample rate in Hz.
        start_hz: Cutoff at sample 0.
        end_hz: Cutoff at the last sample.
        filter_type: ``"lowpass"`` or ``"highpass"``.
        n_steps: Number of blocks.  More = smoother but slower.

    Returns:
        Filtered float32 array of the same length as *audio*.
    """
    if len(audio) == 0:
        return audio
    try:
        from scipy.signal import butter, sosfilt
    except ImportError:  # pragma: no cover — scipy required by full install
        return audio

    nyquist = sample_rate / 2.0
    block = max(1, len(audio) // n_steps)
    out = np.empty_like(audio)
    blend = min(32, block // 4)

    prev_tail: np.ndarray | None = None
    for i in range(n_steps):
        start_idx = i * block
        end_idx = (i + 1) * block if i < n_steps - 1 else len(audio)
        chunk = audio[start_idx:end_idx]
        if len(chunk) == 0:
            continue

        t = i / max(1, n_steps - 1)
        cutoff = start_hz + t * (end_hz - start_hz)
        cutoff_norm = max(1e-4, min(0.99, cutoff / nyquist))
        try:
            sos = butter(4, cutoff_norm, btype=filter_type, output="sos")
            filt = sosfilt(sos, chunk).astype(np.float32)
        except (ValueError, RuntimeError):
            filt = chunk

        # Smooth the boundary between the previous filter pass and this one
        if prev_tail is not None and blend > 0 and len(filt) >= blend:
            fade = np.linspace(0.0, 1.0, blend, dtype=np.float32)
            filt[:blend] = prev_tail * (1.0 - fade) + filt[:blend] * fade

        out[start_idx:end_idx] = filt
        prev_tail = filt[-blend:].copy() if len(filt) >= blend else None

    return out


# ---------------------------------------------------------------------------
# 3-band EQ (real-time, applied per output chunk)
# ---------------------------------------------------------------------------


def make_eq_filters(
    sample_rate: int,
    low_crossover_hz: float = 250.0,
    high_crossover_hz: float = 4000.0,
) -> dict[str, Any] | None:
    """Return SOS filter coefficients for a 3-band split (low / mid / high).

    The returned object is a dict ``{"low": sos, "mid_lp": sos, "mid_hp": sos,
    "high": sos}`` — band-pass for mid is built from a serial low-pass +
    high-pass pair (cheaper than designing a true band-pass).

    Args:
        sample_rate: Sample rate in Hz.
        low_crossover_hz: Boundary between low and mid bands.
        high_crossover_hz: Boundary between mid and high bands.

    Returns:
        Dict of SOS coefficients, or ``None`` when scipy is unavailable.
    """
    try:
        from scipy.signal import butter
    except ImportError:  # pragma: no cover — scipy required by full install
        return None

    nyquist = sample_rate / 2.0
    low_norm = max(1e-4, min(0.99, low_crossover_hz / nyquist))
    high_norm = max(1e-4, min(0.99, high_crossover_hz / nyquist))
    return {
        "low": butter(2, low_norm, btype="low", output="sos"),
        "mid_lp": butter(2, high_norm, btype="low", output="sos"),
        "mid_hp": butter(2, low_norm, btype="high", output="sos"),
        "high": butter(2, high_norm, btype="high", output="sos"),
    }


def apply_eq(
    chunk: np.ndarray,
    sos_filters: dict[str, Any] | None,
    low_gain: float,
    mid_gain: float,
    high_gain: float,
) -> np.ndarray:
    """Apply a 3-band gain-only EQ to a short audio chunk.

    Splits *chunk* into low / mid / high bands via the filters returned
    by :func:`make_eq_filters`, scales each by its gain, and sums them
    back together.  Designed to be called from a sounddevice output
    callback — uses ``sosfilt`` (stateless) which is fine for the short
    independent blocks the callback delivers.

    Args:
        chunk: Mono float32 audio chunk.
        sos_filters: Dict from :func:`make_eq_filters`.
        low_gain: Multiplier for the low band (1.0 = unity, 0.0 = kill).
        mid_gain: Multiplier for the mid band.
        high_gain: Multiplier for the high band.

    Returns:
        EQ-processed float32 chunk (same length, hard-clipped to ±1.0).
    """
    if sos_filters is None:
        return chunk
    try:
        from scipy.signal import sosfilt
    except ImportError:  # pragma: no cover — scipy required by full install
        return chunk

    low = sosfilt(sos_filters["low"], chunk)
    high = sosfilt(sos_filters["high"], chunk)
    mid = sosfilt(sos_filters["mid_lp"], sosfilt(sos_filters["mid_hp"], chunk))

    out = (low * low_gain + mid * mid_gain + high * high_gain).astype(np.float32)
    np.clip(out, -1.0, 1.0, out=out)
    return out


# ---------------------------------------------------------------------------
# Player state
# ---------------------------------------------------------------------------


@dataclass
class PlayerState:
    """Mutable shared state for the playback loop.

    Attributes:
        current_track: The track currently playing (``None`` before first play).
        next_track: The track pre-loaded and queued to play next.
        is_paused: Whether playback is currently paused.
        should_stop: Set to ``True`` to signal the playback loop to exit.
        recently_played: Deque of file path strings for recently played tracks,
            bounded to *no_repeat_window* entries.
        no_repeat_window: Maximum number of tracks kept in *recently_played*.
        track_number: Zero-based count of auto-picked tracks (seed = 0).
            Incremented after each track transition.
        discovery_enabled: Runtime toggle for discovery mode.  Must be ``True``
            AND ``Player._discovery_every`` must be set for discovery to fire.
    """

    current_track: IndexEntry | None = None
    next_track: IndexEntry | None = None
    queued_next: IndexEntry | None = None  # set by web UI "play next/now"
    queue: list[IndexEntry] = field(default_factory=list)  # web UI ordered queue
    is_paused: bool = False
    should_stop: bool = False
    no_repeat_window: int = 500
    artist_repeat_window: int = 3
    recently_played: deque = field(default_factory=deque)
    recently_played_artists: deque = field(default_factory=deque)
    recently_played_albums: deque = field(default_factory=deque)
    recently_played_titles: deque = field(default_factory=deque)
    volume: float = 1.0  # 0.0 (silent) – 1.0 (full)
    is_muted: bool = False
    track_number: int = 0
    discovery_enabled: bool = False

    def __post_init__(self) -> None:
        """Initialise the bounded recently-played deques."""
        self.recently_played = deque(maxlen=self.no_repeat_window)
        w = max(0, self.artist_repeat_window)
        self.recently_played_artists = deque(maxlen=w)
        self.recently_played_albums = deque(maxlen=w)
        self.recently_played_titles = deque(maxlen=w)

    def record_played(self, entry: IndexEntry) -> None:
        """Record a track as recently played.

        Tracks file path, artist, album, and title so the picker can
        avoid back-to-back same-artist sequences, two songs from the
        same album in a row, and re-runs of the same title (which
        catches different recordings / live versions of one song).

        Args:
            entry: The track that just started playing.
        """
        self.recently_played.append(entry.path)
        if entry.artist:
            self.recently_played_artists.append(entry.artist.lower())
        if entry.album:
            self.recently_played_albums.append(entry.album.lower())
        if entry.title:
            self.recently_played_titles.append(entry.title.lower())


# ---------------------------------------------------------------------------
# Audio loading
# ---------------------------------------------------------------------------


def load_audio(path: str, target_sr: int = _DEFAULT_SR) -> tuple[np.ndarray, int]:
    """Load an audio file as a mono float32 array.

    Uses ``soundfile`` for lossless formats (FLAC, WAV) and falls back to
    ``librosa`` for MP3 and M4A.

    Args:
        path: Absolute path to the audio file.
        target_sr: Target sample rate.  If the file's native rate differs,
            librosa resamples to *target_sr*.

    Returns:
        A tuple ``(audio, sample_rate)`` where *audio* is a mono float32
        array and *sample_rate* is the actual rate after any resampling.

    Raises:
        OSError: If the file cannot be read.
    """
    try:
        audio, sr = sf.read(path, dtype="float32", always_2d=False)
        # Mix down to mono if stereo
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        return audio, sr
    except Exception:
        # Fallback for MP3 / M4A which soundfile may not support without plugins
        import librosa

        audio, sr = librosa.load(path, sr=target_sr, mono=True)
        return audio, int(sr)


# ---------------------------------------------------------------------------
# M3U export helpers
# ---------------------------------------------------------------------------


def _write_m3u_header(path: Path) -> None:
    """Write (or overwrite) a new M3U file containing only the ``#EXTM3U`` header."""
    path.write_text("#EXTM3U\n", encoding="utf-8")


def _append_m3u_entry(path: Path, entry: IndexEntry) -> None:
    """Append a single ``#EXTINF`` + path line to an existing M3U file.

    Args:
        path: Path to the M3U file.
        entry: Track to append.
    """
    duration = int(entry.length) if entry.length > 0 else -1
    display = f"{entry.artist} - {entry.title}" if entry.artist else entry.title
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"#EXTINF:{duration},{display}\n{entry.path}\n")


def write_m3u(entries: list[IndexEntry], path: Path) -> None:
    """Write a complete M3U playlist file for *entries*.

    Overwrites *path* if it already exists.

    Args:
        entries: Ordered list of tracks for the playlist.
        path: Destination file path.
    """
    _write_m3u_header(path)
    for entry in entries:
        _append_m3u_entry(path, entry)


# ---------------------------------------------------------------------------
# Play history helpers
# ---------------------------------------------------------------------------


def _append_history_entry(path: Path, entry: IndexEntry, played_at: datetime) -> None:
    """Append a JSON Lines record to the play history file.

    Creates the file (and any missing parent directories) if it does not exist.

    Args:
        path: Path to the JSON Lines history file.
        entry: Track that was played.
        played_at: UTC/local timestamp when playback began.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": played_at.isoformat(timespec="seconds"),
        "path": entry.path,
        "title": entry.title,
        "artist": entry.artist,
        "album": entry.album,
        "bpm": entry.bpm,
        "length": entry.length,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------


class Player:
    """Continuous auto-DJ playback loop.

    Plays tracks sequentially, using the :class:`~autodj.similarity.SimilarityIndex`
    to select each next track based on sonic similarity to the current one.
    Crossfade audio is mixed in a background thread.

    Args:
        cfg: Full AutoDJ configuration (used for playback settings).
        sim_index: Loaded similarity index for next-track selection.
        dry_run: If ``True``, print track selections without playing audio.
        preset: Optional BPM-shaping preset.
        export_m3u: Optional path to write a live M3U playlist as tracks play.
        history_file: Optional path to append JSON Lines play history.
        discovery_every: Override discovery rate (tracks between injections).
            When ``None``, falls back to ``preset.discovery_every`` if set.
        bpm_range: Hard BPM filter ``(lo, hi)`` applied to every track pick.
    """

    def __init__(
        self,
        cfg: AutoDJConfig,
        sim_index: SimilarityIndex,
        dry_run: bool = False,
        preset: Preset | None = None,
        export_m3u: Path | None = None,
        history_file: Path | None = None,
        discovery_every: int | None = None,
        bpm_range: tuple[float, float] | None = None,
        smart_shuffle: bool = False,
        pure_shuffle: bool = False,
        anchor_to_seed: bool = False,
        no_keyboard: bool = False,
    ) -> None:
        """Initialise the player with configuration and index.

        No model is needed at play time — next-track vectors are looked up
        directly from the pre-built FAISS index.

        Args:
            cfg: Full :class:`~autodj.config.AutoDJConfig` instance.
            sim_index: Loaded :class:`~autodj.similarity.SimilarityIndex`.
            dry_run: If ``True``, print track selections without playing audio.
            preset: Optional :class:`~autodj.presets.Preset` for BPM shaping.
            export_m3u: Optional :class:`~pathlib.Path` for live M3U export.
            history_file: Optional :class:`~pathlib.Path` for JSON Lines history.
            discovery_every: Tracks between discovery injections.  Overrides
                ``preset.discovery_every`` when both are set.
            bpm_range: Hard ``(lo, hi)`` BPM filter for every track pick.
        """
        self._cfg = cfg
        self._sim = sim_index
        self._dry_run = dry_run
        self._no_keyboard = no_keyboard
        self._preset = preset
        self._export_m3u = export_m3u
        self._history_file = history_file
        self._bpm_range = bpm_range
        self._smart_shuffle = smart_shuffle
        # Anchored mode: when True, every similarity query uses the SEED
        # vector rather than the currently-playing track.  Prevents the
        # session from drifting away from where the user started — each
        # next track is similar to the seed, not to the previous track.
        # Off by default; toggle from web UI / `--anchor-seed` CLI flag.
        self._anchor_to_seed: bool = anchor_to_seed
        # Path of the seed track — set in run() / set externally by the
        # bridge when the user picks a fresh seed.  Used by anchored mode.
        self._seed_path: str | None = None
        # Pure shuffle: pick random next track, completely ignore similarity.
        # Distinct from smart_shuffle (which inverts similarity to find the
        # MOST distant track).  When the user disables pure-shuffle mid-set,
        # the next pick uses similarity from the current track — so they can
        # use shuffle to stumble onto a song they like, then "lock in" by
        # toggling shuffle off and let the auto-DJ continue from there.
        self._pure_shuffle = pure_shuffle
        # Lyrics for the current track — populated when each track loads,
        # consumed by the web UI via PlayerBridge.get_state().
        self._current_lyrics: list = []
        self._current_lyrics_plain: str = ""
        # 3-band EQ state (1.0 = unity), mutated by web UI / keyboard
        self._eq_low: float = 1.0
        self._eq_mid: float = 1.0
        self._eq_high: float = 1.0
        self._eq_filters: dict[str, Any] | None = None
        # Energy ramp target for the current pick (None = disabled)
        self._target_energy: float | None = None
        # Mood-arc state.  Lazy-init: set when the user enables the
        # arc via config / CLI / web UI so unattended playback ramps
        # warmup -> peak -> cool over a session-relative window.
        self._mood_arc: Any = None
        if getattr(cfg.playback, "enable_mood_arc", False):
            from autodj.mood_arc import make_default_arc

            self._mood_arc = make_default_arc(
                duration_hours=getattr(cfg.playback, "mood_arc_hours", 3.0),
            )
        # DJ meta cache — initialised lazily on first use so tests with
        # mock configs don't trip on the cache load.
        from autodj.dj_meta import DjMetaCache as _DjMetaCache

        self._dj_cache: _DjMetaCache | None = None
        self._dj_cache_initialised = False
        # Current track's beatmatch ratio (1.0 = no stretch) — exposed via state
        self._beatmatch_ratio: float = 1.0
        # Last transition effect applied (string name) — exposed via state
        self._last_transition_fx: str = "none"
        # Pick provenance — set by _pick_next describing HOW the current
        # track was selected.  Read by the bridge to build the
        # "why this track" sentence list shown in the web UI.
        self._last_pick_mode: str = "seed"
        # Previous track played — kept so the explainer can compute deltas
        # against the current pick.
        self._previous_track: IndexEntry | None = None
        # Discovery rate: CLI override takes precedence over preset
        self._discovery_every: int | None = (
            discovery_every
            if discovery_every is not None
            else (preset.discovery_every if preset and preset.discovery_every else None)
        )
        self._state = PlayerState(
            no_repeat_window=cfg.playback.no_repeat_window,
            artist_repeat_window=cfg.playback.artist_repeat_window,
        )
        self._crossfade_samples: dict[int, int] = {}  # populated per-track based on SR
        self._skip_event = threading.Event()
        self._lock = threading.Lock()
        # Shared playback position (samples) — written by callback, read/written by
        # keyboard seek handler.  Using a list so both sides share the same object.
        self._playback_pos: list[int] = [0]
        self._playback_len: int = 0  # length of the current audio array in samples
        self._current_sr: int = _DEFAULT_SR
        # Rich Live display — set inside run(), None between sessions
        self._live: Live | None = None

    def _build_status(self) -> Panel:
        """Build the Rich Panel rendered in the bottom status bar.

        Returns:
            A :class:`rich.panel.Panel` showing now-playing info, volume, and controls.
        """
        current = self._state.current_track
        next_t = self._state.next_track

        # --- Line 1: play state + current track + elapsed time ---
        if current:
            icon = "[yellow]⏸ PAUSED[/yellow]" if self._state.is_paused else "[green]▶[/green]"
            elapsed = self._playback_pos[0] / max(1, self._current_sr)
            total = current.length or 0.0
            elapsed = min(elapsed, total)
            bpm = f"  BPM {current.bpm:.0f}" if current.bpm else ""
            pos = f"  {_fmt_time(elapsed)} / {_fmt_time(total)}" if total > 0 else ""
            now_line = f"{icon} [bold]{current.display_name}[/bold][dim]{bpm}{pos}[/dim]"
        else:
            now_line = "[dim]Loading...[/dim]"

        # --- Line 2: next track + volume bar + discovery indicator ---
        vol_pct = round(self._state.volume * 100)
        filled = round(self._state.volume * 10)
        bar = "█" * filled + "░" * (10 - filled)
        vol = "[red]MUTED[/red]" if self._state.is_muted else f"[cyan]{bar} {vol_pct}%[/cyan]"
        nxt = f"[dim]Next:[/dim] {next_t.display_name}  " if next_t else ""
        disc_indicator = ""
        if self._discovery_every is not None:
            if self._state.discovery_enabled:
                disc_indicator = "  [bold cyan]\u25c8 Discovery[/bold cyan]"
            else:
                disc_indicator = "  [dim]\u25c8 Discovery[/dim]"
        next_line = f"{nxt}[dim]Vol:[/dim] {vol}{disc_indicator}"

        # --- Line 3: controls hint ---
        disc_key = "  D=Discovery" if self._discovery_every is not None else ""
        controls = (
            f"[dim]Space=Pause  N=Skip{disc_key}  Q=Quit"
            "  \u2190/\u2192=Seek\u00b110s  \u2191/\u2193=Volume  M=Mute[/dim]"
        )

        return Panel(
            f"{now_line}\n{next_line}\n{controls}",
            title="[bold blue]AutoDJ[/bold blue]",
            border_style="blue",
            padding=(0, 1),
        )

    def _refresh_status(self) -> None:
        """Push an updated status panel to the Live display if it is active."""
        if self._live is not None:
            self._live.update(self._build_status())

    def run(self, seed_entry: IndexEntry | None) -> None:
        """Start the playback loop.

        Plays *seed_entry* first (or picks a random track if ``None``), then
        queries the similarity index for each successive track.  Blocks until
        the user presses ``Q`` or :attr:`PlayerState.should_stop` is set.

        Args:
            seed_entry: The track to start from.  ``None`` selects a random
                track from the index.
        """
        import random

        if seed_entry is None:
            # Non-security seed pick — random.choice is fine here.
            seed_entry = random.choice(self._sim.entries)  # nosec B311
        # Remember the seed so anchored mode can keep coming back to it.
        self._seed_path = seed_entry.path

        # Skip keyboard setup in dry-run / headless mode — pynput may be
        # absent on a NAS, and there's no audio to control here anyway
        # (the browser drives playback in this mode).
        # pynput is a GLOBAL keyboard hook — captures keys typed in any
        # application, browser tab, etc.  Skip it when (1) running
        # headless / browser-driven OR (2) caller explicitly opted out
        # (e.g. `serve` mode where the browser handles all controls).
        if not self._dry_run and not self._no_keyboard:
            self._setup_keyboard()

        # External-cue importer (Mixxx / Rekordbox / Traktor).  Runs
        # synchronously on this thread so the FastAPI event loop in
        # serve mode is never blocked by SQLite / XML I/O.
        self._ensure_dj_cache()
        self._ensure_external_cues()

        # --- M3U / history: write seed track ---
        if self._export_m3u:
            _write_m3u_header(self._export_m3u)
            _append_m3u_entry(self._export_m3u, seed_entry)
        if self._history_file:
            _append_history_entry(self._history_file, seed_entry, datetime.now())

        current = seed_entry
        self._state.current_track = current
        self._state.record_played(current)

        # Headless / browser-driven mode: skip the Rich Live transport
        # panel entirely.  In headless serve, the only useful output is
        # the startup banner (already printed by cli.cmd_serve) and any
        # error logging.  No keyboard, no terminal panel, no per-track
        # noise.
        if self._dry_run:
            self._run_headless(current)
            return

        # Interactive Rich Live + audio-out loop — not exercised in CI
        # because it owns the terminal and a real sounddevice.  Headless
        # variant above is fully tested.
        with Live(  # pragma: no cover
            self._build_status(),
            console=_CONSOLE,
            refresh_per_second=2,
            vertical_overflow="visible",
        ) as live:
            self._live = live
            try:
                while not self._state.should_stop:
                    self._state.current_track = current
                    self._skip_event.clear()

                    next_entry = self._pick_next(current)
                    self._state.next_track = next_entry
                    self._refresh_status()

                    self._play_with_crossfade(current, next_entry)

                    if self._state.should_stop:
                        break

                    # If the user queued a specific track WHILE the
                    # current one was playing (search → Now), honour
                    # that pick — _pick_next had already chosen
                    # next_entry before queued_next was set.
                    if self._state.queued_next is not None:
                        next_entry = self._state.queued_next
                        self._state.queued_next = None

                    self._state.record_played(next_entry)
                    self._state.track_number += 1

                    # --- M3U / history: write each new track as it starts ---
                    if self._export_m3u:
                        _append_m3u_entry(self._export_m3u, next_entry)
                    if self._history_file:
                        _append_history_entry(self._history_file, next_entry, datetime.now())

                    self._previous_track = current
                    current = next_entry
            finally:
                self._live = None

    def _run_headless(self, current: IndexEntry) -> None:
        """Track-picking loop with no audio output and no terminal UI.

        Used by ``serve --no-playback`` (and auto-enabled when audio
        deps are missing).  Browser is responsible for actual playback;
        this loop just advances ``state.current_track`` /
        ``state.next_track`` so the WebSocket pushes stay accurate, and
        waits for the browser to POST ``/api/advance`` (which sets
        ``self._skip_event``) at end-of-track.

        Args:
            current: Initial seed track.
        """
        # One-shot init: seed current + pre-compute next so the WS push
        # has something to show on first connect.  After this, the
        # browser owns every state transition: it calls /api/advance
        # (or /api/skip) which routes through PlayerBridge.advance_now()
        # to mutate state synchronously.  This loop never advances on
        # its own — that was the source of the "song changed while page
        # idle" bug.
        self._state.current_track = current
        if self._state.next_track is None:
            self._state.next_track = self._pick_next(current)
        self._current_sr = _DEFAULT_SR
        self._playback_len = int(
            (current.length if current.length and current.length > 0 else 5.0) * _DEFAULT_SR,
        )
        self._playback_pos[0] = 0

        # Park until shutdown.  No fallback timer, no auto-advance.
        while not self._state.should_stop:  # pragma: no cover
            self._skip_event.wait(timeout=1.0)
            self._skip_event.clear()

    def _pick_next(self, current: IndexEntry) -> IndexEntry:
        """Select the next track by looking up the current track's stored vector.

        Selection priority:
        1. If a track is queued via the web UI (``state.queued_next``), use it.
        2. If discovery mode is enabled and it's time to fire, use
           :meth:`~autodj.similarity.SimilarityIndex.find_distant` (bypasses
           BPM shaping — discovery tracks are intentionally surprising).
        3. Normal FAISS similarity search, optionally biased by preset BPM.

        Falls back to excluding only the current track if the no-repeat window
        covers the entire index.

        Args:
            current: The track currently playing.

        Returns:
            The recommended next :class:`~autodj.indexer.IndexEntry`.
        """
        if self._state.queued_next is not None:
            entry = self._state.queued_next
            self._state.queued_next = None
            self._last_pick_mode = "queue"
            logger.info("Playing queued track: %s", entry.display_name)
            return entry

        # Pop the front of the user-ordered queue (web UI drag-reorder)
        if self._state.queue:
            entry = self._state.queue.pop(0)
            self._last_pick_mode = "queue"
            logger.info("Playing from queue: %s", entry.display_name)
            return entry

        # Pure shuffle: random pick, ignore similarity entirely.  Avoid
        # the recently-played window so we don't repeat ourselves.
        if self._pure_shuffle:
            import random as _rnd

            excluded = set(self._state.recently_played)
            pool = [e for e in self._sim.entries if e.path not in excluded]
            if not pool:
                pool = list(self._sim.entries)
            self._last_pick_mode = "pure_shuffle"
            return _rnd.choice(pool)  # nosec B311 — non-security

        from autodj.similarity import SimilarityError

        tn = self._state.track_number

        # --- Discovery injection ---
        # Fires when: rate is configured AND runtime toggle is ON AND not track 0
        if (
            self._discovery_every is not None
            and self._state.discovery_enabled
            and tn > 0
            and tn % self._discovery_every == 0
        ):
            try:
                entry = self._sim.find_distant(current.path, self._state.recently_played)
                _CONSOLE.print("  [bold cyan]\u25c8 Discovery track[/bold cyan]")
                self._last_pick_mode = "discovery"
                return entry
            except SimilarityError:
                pass  # fall through to normal selection

        # --- Compute BPM target ---
        # Priority order:
        #   1. Explicit user preset (set-relative ramp).
        #   2. Mood arc (set-relative envelope, anchored to start).
        #   3. Daypart (wall-clock, runs forever).
        # The chosen target is fed straight into the similarity scorer
        # alongside the existing energy/genre/key constraints.
        target_bpm: float | None = None
        bpm_weight: float = 0.2
        target_energy = self._target_energy

        if self._preset is not None:
            target_bpm = self._preset.target_bpm(tn)
            bpm_weight = self._preset.bpm_weight
        elif getattr(self._cfg.playback, "enable_mood_arc", False) and self._mood_arc:
            from autodj.mood_arc import current_arc_target

            target = current_arc_target(self._mood_arc)
            target_bpm = target.target_bpm
            bpm_weight = target.bpm_weight
            if target_energy is None:
                target_energy = target.target_energy
        elif getattr(self._cfg.playback, "enable_daypart", False):
            from autodj.daypart import current_daypart

            dp = current_daypart()
            target_bpm = dp.target_bpm
            bpm_weight = dp.bpm_weight
            if target_energy is None:
                target_energy = dp.target_energy

        # Wider candidate pool than vanilla nearest-neighbour avoids
        # falling into a 20-track sonic island.  50 with filter / 30
        # without strikes a balance between cohesion and variety.
        n_candidates = 50 if (target_bpm is not None or self._bpm_range is not None) else 30

        # Genre filter from preset (None / [] = no filter)
        genre_filter = self._preset.genres if self._preset and self._preset.genres else None
        harmonic_only = self._cfg.djmix.harmonic_mixing
        harmonic_mode = getattr(self._cfg.djmix, "harmonic_mode", "compatible")

        # Anchored mode: query similarity from the SEED, not the current
        # track.  Lets the user lock in to a sonic neighbourhood — every
        # next pick stays near the seed rather than drifting through
        # repeated similarity hops.  Discovery still injects distant
        # tracks when enabled.
        query_path = current.path
        if self._anchor_to_seed and self._seed_path:
            query_path = self._seed_path
            self._last_pick_mode = "anchored"
        elif self._smart_shuffle:
            self._last_pick_mode = "smart_shuffle"
        else:
            self._last_pick_mode = "similarity"

        # --- Normal similarity search ---
        try:
            return self._sim.find_next_for_path(
                current_path=query_path,
                recently_played=self._state.recently_played,
                n_candidates=n_candidates,
                target_bpm=target_bpm,
                bpm_weight=bpm_weight,
                bpm_range=self._bpm_range,
                genre_filter=genre_filter,
                invert=self._smart_shuffle,
                harmonic_only=harmonic_only,
                harmonic_mode=harmonic_mode,
                target_energy=target_energy,
                excluded_artists=set(self._state.recently_played_artists),
                excluded_albums=set(self._state.recently_played_albums),
                excluded_titles=set(self._state.recently_played_titles),
            )
        except SimilarityError:
            # The repeat window is >= index size — relax it to just the
            # current track so we can keep playing.
            logger.info(
                "No candidates after applying repeat window (%d tracks) — "
                "relaxing to avoid only the current track.",
                len(self._state.recently_played),
            )
            return self._sim.find_next_for_path(
                current_path=query_path,
                recently_played=deque([current.path]),
                n_candidates=n_candidates,
                target_bpm=target_bpm,
                bpm_weight=bpm_weight,
                bpm_range=self._bpm_range,
                genre_filter=genre_filter,
                invert=self._smart_shuffle,
                harmonic_only=harmonic_only,
                harmonic_mode=harmonic_mode,
                target_energy=target_energy,
                excluded_artists=set(self._state.recently_played_artists),
                excluded_albums=set(self._state.recently_played_albums),
                excluded_titles=set(self._state.recently_played_titles),
            )

    # ------------------------------------------------------------------
    # _play_with_crossfade helpers — broken out so the orchestrator stays
    # readable.  Each helper owns one phase of the crossfade pipeline.
    # ------------------------------------------------------------------

    def _apply_replaygain(self, audio: np.ndarray, path: str) -> np.ndarray:
        """Apply ReplayGain normalisation when enabled in config; else no-op."""
        if not self._cfg.replaygain.enabled:
            return audio
        from autodj.audio_meta import read_replaygain, replaygain_multiplier

        rg = read_replaygain(path)
        gain = replaygain_multiplier(
            rg,
            target_db=self._cfg.replaygain.target_db,
            max_clip_safe_gain=self._cfg.replaygain.max_clip_safe_gain,
        )
        if gain == 1.0:
            return audio
        return (audio * gain).astype(np.float32)

    def _load_lyrics(self, path: str) -> None:
        """Populate ``_current_lyrics`` + ``_current_lyrics_plain`` for *path*.

        Resolution order:
        1. Sibling ``.lrc`` file (timestamped, scrolls in the web UI).
        2. Beets DB ``lyrics`` field (plain text).
        3. Embedded ID3/Vorbis/MP4 lyric tags (USLT, LYRICS, ©lyr).
        """
        from autodj.audio_meta import load_lrc_for, read_plain_lyrics

        self._current_lyrics = []
        self._current_lyrics_plain = ""
        # Respect the lyric-display toggle — when off we skip ALL lyric
        # work so the CLI panel stays compact and the web UI hides its card.
        if not getattr(self._cfg.playback, "show_lyrics", True):
            return

        self._current_lyrics = load_lrc_for(path)
        if self._current_lyrics:
            return

        if self._cfg.library.beets_db:
            from autodj.beets import get_lyrics_for_path

            try:
                self._current_lyrics_plain = get_lyrics_for_path(
                    self._cfg.library.beets_db,
                    path,
                    music_dir=self._cfg.library.music_dir,
                )
            except (OSError, ValueError) as exc:
                logger.debug("Beets lyrics lookup failed: %s", exc)
                self._current_lyrics_plain = ""

        # Fall back to embedded tag lyrics when beets has none / no beets at all.
        if not self._current_lyrics_plain:
            try:
                self._current_lyrics_plain = read_plain_lyrics(path)
            except (OSError, ValueError) as exc:
                logger.debug("Embedded lyric tag read failed: %s", exc)
                self._current_lyrics_plain = ""

        # CLI: print plain lyrics block once per track so the user can see
        # them in the terminal too (web UI already renders them below the
        # now-playing card).  Skipped in dry-run / headless serve mode.
        if self._current_lyrics_plain and not self._dry_run and not self._no_keyboard:
            _CONSOLE.print(
                Panel(
                    self._current_lyrics_plain,
                    title="[bold]Lyrics[/bold]",
                    border_style="dim",
                    padding=(0, 1),
                ),
            )

    def _ensure_dj_cache(self) -> None:
        """Lazy-init the DJ-meta cache on first real use.

        Cheap by design: a single sidecar JSON read.  Safe to call from
        an asyncio handler (e.g. ``PlayerBridge.get_state``) without
        blocking the event loop.  External cue import is deliberately
        NOT done here -- see :meth:`_ensure_external_cues`.
        """
        if self._dj_cache_initialised:
            return
        self._dj_cache_initialised = True
        try:
            from autodj.dj_meta import get_cache

            if isinstance(self._cfg.index.active_dir, Path):
                self._dj_cache = get_cache(self._cfg.index.active_dir)
        except (OSError, ValueError) as exc:
            logger.debug("DJ cache unavailable: %s", exc)
            self._dj_cache = None

    def _ensure_external_cues(self) -> None:
        """One-shot import of cues from Mixxx / Rekordbox / Traktor.

        Runs synchronously on the *player thread* (called from
        :meth:`run`) so the asyncio event loop in the FastAPI server is
        never blocked by SQLite reads or XML parses.  Imported cues
        merge into each cached :class:`~autodj.dj_meta.DjMeta` lazily
        when a track is first analysed -- so we pay the importer cost
        exactly once per ``serve`` / ``play`` boot.
        """
        if getattr(self, "_external_cues_loaded", False):
            return
        self._external_cues_loaded = True
        self._external_cues: dict[str, list[Any]] = {}
        if not getattr(self._cfg.playback, "import_external_cues", True):
            return
        try:
            from autodj.dj_cues_import import auto_import_cues

            self._external_cues = auto_import_cues(
                library_root=self._cfg.library.music_dir
                if isinstance(self._cfg.library.music_dir, Path)
                else None,
            )
            if self._external_cues:
                logger.info(
                    "Imported cues for %d tracks from external DJ software",
                    len(self._external_cues),
                )
        except (OSError, ValueError, ImportError) as exc:
            logger.debug("External cue import failed: %s", exc)

    def _outgoing_meta(self, audio_a: np.ndarray, sr_a: int, path: str) -> DjMeta | None:
        """Get / compute DjMeta for the outgoing track when needed for alignment.

        Returns analysed meta when any of these features needs marker
        data: ``djmix.outro_intro_align``, ``djmix.phrase_align``, or any
        marker-driven transition_mode (everything except ``"fixed"``).
        """
        from autodj.dj_meta import analyse_audio

        cfg_dj = self._cfg.djmix
        marker_mode = self._cfg.playback.transition_mode != "fixed"
        if self._dj_cache is None or not (
            cfg_dj.outro_intro_align or cfg_dj.phrase_align or marker_mode
        ):
            return None
        meta = self._dj_cache.get(path)
        if not meta.analysed:
            meta = analyse_audio(audio_a, sr_a)
            self._merge_external_cues_into(meta, path)
            self._dj_cache.set(path, meta)
            self._dj_cache.flush(batch=10)
        return meta

    def _merge_external_cues_into(self, meta: DjMeta, path: str) -> None:
        """Merge externally-imported cues for *path* into *meta* in place.

        No-op when the importer found nothing for this track.  Uses
        :func:`autodj.dj_meta.merge_cues` so user / DJ-software cues
        win on conflict but auto-detected cues survive when they're
        the only source for a region of the track.
        """
        external = getattr(self, "_external_cues", {}).get(path)
        if not external:
            return
        from autodj.dj_meta import merge_cues

        meta.cues = merge_cues(meta.cues, external)

    def _peek_incoming_meta(self, next_entry: IndexEntry) -> DjMeta | None:
        """Cache-only DjMeta peek for the incoming track (no audio decode).

        Used by :meth:`_effective_crossfade_seconds` to read intro_end_s
        before the heavy audio load.  Returns ``None`` when the sidecar
        cache is uninitialised or the track has not been analysed yet.
        """
        if self._dj_cache is None:
            return None
        meta = self._dj_cache.get(next_entry.path)
        return meta if meta.analysed else None

    def _effective_crossfade_seconds(
        self,
        meta_a: DjMeta | None,
        meta_b: DjMeta | None,
        outgoing_length_s: float,
    ) -> float:
        """Resolve the active fade length for the configured transition_mode.

        Mirrors the browser's ``_resolveFadeSec`` in ``static/app.js`` so
        the CLI player and the web UI sound the same.

        Args:
            meta_a: Outgoing track's DJ-meta sidecar entry.
            meta_b: Incoming track's DJ-meta sidecar entry (may be None).
            outgoing_length_s: Outgoing track length in seconds (used to
                derive ``outro_len = length - outro_start_s``).

        Returns:
            Effective fade length in seconds.  Always >= 0.
        """
        base = float(self._cfg.playback.crossfade_seconds)
        mode = self._cfg.playback.transition_mode
        if mode == "fixed":
            return base
        outro_len: float | None = None
        if meta_a and meta_a.outro_start_s > 0 and outgoing_length_s > 0:
            outro_len = max(0.0, outgoing_length_s - meta_a.outro_start_s)
        intro_end: float | None = None
        if meta_b and meta_b.intro_end_s > 0:
            intro_end = float(meta_b.intro_end_s)

        def _clamp(v: float) -> float:
            return max(1.0, min(12.0, v))

        if mode == "full_intro_outro" and outro_len is not None and intro_end is not None:
            return _clamp(min(outro_len, intro_end))
        if mode == "outro_fade" and outro_len is not None:
            return _clamp(outro_len)
        # fixed_skip_silence + fallback for missing markers in the other modes
        return base

    def _crossfade_start_in_a(
        self,
        audio_a: np.ndarray,
        sr_a: int,
        meta_a: DjMeta | None,
        crossfade_samples: int,
    ) -> int:
        """Decide the sample offset where the crossfade begins in audio_a."""
        from autodj.dj_meta import nearest_phrase_boundary

        cfg_dj = self._cfg.djmix
        mode = self._cfg.playback.transition_mode
        marker_anchor = mode in ("full_intro_outro", "outro_fade")
        start = max(0, len(audio_a) - crossfade_samples)
        if (cfg_dj.outro_intro_align or marker_anchor) and meta_a and meta_a.outro_start_s > 0:
            target = int(meta_a.outro_start_s * sr_a)
            target = min(target, len(audio_a) - crossfade_samples)
            start = max(0, target)
        if cfg_dj.phrase_align and meta_a and meta_a.beats:
            snapped = nearest_phrase_boundary(
                meta_a.beats,
                start / sr_a,
                bars=cfg_dj.phrase_bars,
            )
            if snapped is not None:
                snapped_samples = int(snapped * sr_a)
                if 0 <= snapped_samples <= len(audio_a) - crossfade_samples:
                    start = snapped_samples
        return start

    def _load_incoming(
        self,
        next_entry: IndexEntry,
        sr_a: int,
        crossfade_samples: int,
    ) -> np.ndarray:
        """Load incoming track, resample to sr_a, ReplayGain — silence on failure."""
        try:
            audio_b, sr_b = load_audio(str(next_entry.path))
            if sr_b != sr_a:
                import librosa as _librosa

                audio_b = _librosa.resample(audio_b, orig_sr=sr_b, target_sr=sr_a)
            return self._apply_replaygain(audio_b, next_entry.path)
        except (OSError, ValueError, RuntimeError) as exc:
            logger.warning("Cannot pre-load next track (%s): %s", next_entry.path, exc)
            return np.zeros(crossfade_samples, dtype=np.float32)

    def _maybe_beatmatch(
        self,
        audio_b: np.ndarray,
        current: IndexEntry,
        next_entry: IndexEntry,
    ) -> np.ndarray:
        """Pitch-stretch audio_b to match the outgoing BPM (if configured)."""
        cfg_dj = self._cfg.djmix
        self._beatmatch_ratio = 1.0
        if not (cfg_dj.beatmatch and current.bpm > 0 and next_entry.bpm > 0):
            return audio_b
        audio_b, ratio = beatmatch_incoming(
            audio_b,
            bpm_a=current.bpm,
            bpm_b=next_entry.bpm,
            max_stretch=cfg_dj.beatmatch_max_stretch,
        )
        self._beatmatch_ratio = ratio
        return audio_b

    def _skip_incoming_intro(
        self,
        audio_b: np.ndarray,
        sr_a: int,
        next_entry: IndexEntry,
    ) -> np.ndarray:
        """Drop the incoming track's intro so we mix into the first downbeat.

        Triggered by either ``djmix.outro_intro_align`` or any marker-aware
        transition_mode (``full_intro_outro`` / ``fixed_skip_silence``).
        """
        from autodj.dj_meta import analyse_audio

        mode = self._cfg.playback.transition_mode
        marker_skip = mode in ("full_intro_outro", "fixed_skip_silence")
        if self._dj_cache is None or not (self._cfg.djmix.outro_intro_align or marker_skip):
            return audio_b
        meta_b = self._dj_cache.get(next_entry.path)
        if not meta_b.analysed:
            meta_b = analyse_audio(audio_b, sr_a)
            self._merge_external_cues_into(meta_b, next_entry.path)
            self._dj_cache.set(next_entry.path, meta_b)
            self._dj_cache.flush(batch=10)
        if meta_b.intro_end_s <= 0.5:
            return audio_b
        skip = min(int(meta_b.intro_end_s * sr_a), len(audio_b) // 2)
        return audio_b[skip:]

    def _apply_outgoing_filter_sweep(
        self,
        audio_a_trimmed: np.ndarray,
        sr_a: int,
        crossfade_samples: int,
    ) -> np.ndarray:
        """Low-pass sweep on the outgoing tail (when filter_sweep is on)."""
        cfg_dj = self._cfg.djmix
        if not cfg_dj.filter_sweep:
            return audio_a_trimmed
        tail = audio_a_trimmed[-crossfade_samples:].copy()
        tail = apply_filter_sweep(
            tail,
            sample_rate=sr_a,
            start_hz=sr_a / 2.0,
            end_hz=cfg_dj.filter_sweep_floor_hz,
            filter_type="lowpass",
        )
        return np.concatenate(
            [audio_a_trimmed[:-crossfade_samples], tail],
        ).astype(np.float32)

    # Industry-standard minimum effect lengths in seconds, sourced from
    # commercial DJ-tool defaults (Pioneer DJM, Reloop RMX, Numark NS,
    # Mixxx) and the Engineer's Reference for Live Sound.  These give
    # each effect enough runway to sound natural rather than rushed.
    _MIN_FX_DURATION_S: ClassVar[dict[str, float]] = {
        "tape_stop": 4.0,  # Reloop default ~50% rate over 4 s
        "backspin": 2.5,  # Pioneer Backspin / Numark Reverse Roll
        "forward_spin": 2.5,  # mirror of backspin
        "noise_riser": 4.0,  # 2-bar build @ 120 BPM
        "noise_drop": 3.0,  # shorter — drops feel snappier
        "reverb_tail": 4.0,  # mid-size hall
        "freeze": 4.0,  # granular hold needs space
        "glitch": 3.0,  # chaotic; longer becomes tedious
        "echo_out": 3.0,  # 1/4-note feedback over 8 bars
        "scratch": 2.0,  # 4-pass turntablist sweep
        "beat_repeat": 3.0,  # 8 retriggers
        "sidechain_pump": 4.0,  # 8 beats of pump @ 120 BPM
        "reverse_reverb": 3.0,  # swell-in needs time to build
        "air_horn": 3.0,  # full pitch sweep
    }

    def _apply_transition_effect(
        self,
        audio_a_trimmed: np.ndarray,
        audio_b: np.ndarray,
        b_head: np.ndarray,
        sr_a: int,
        crossfade_samples: int,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Apply the configured transition effect; return (a_trimmed, b_head, extra)."""
        from autodj.transitions import TransitionFx, apply_transition, pick_effect

        extra_layer = np.zeros(0, dtype=np.float32)
        fx_name = self._cfg.transitions.effect
        try:
            fx_mode = TransitionFx(fx_name)
        except ValueError:
            fx_mode = TransitionFx.NONE
        chosen_fx = pick_effect(fx_mode)
        self._last_transition_fx = chosen_fx.value
        if chosen_fx == TransitionFx.NONE:
            return audio_a_trimmed, b_head, extra_layer

        min_seconds = self._MIN_FX_DURATION_S.get(chosen_fx.value, 0.0)
        if min_seconds > 0:
            effect_samples = max(crossfade_samples, int(min_seconds * sr_a))
        else:
            effect_samples = crossfade_samples
        effect_samples = min(effect_samples, len(audio_a_trimmed))

        tail = audio_a_trimmed[-effect_samples:].copy()
        head_for_fx = audio_b[:effect_samples] if len(audio_b) >= effect_samples else b_head
        tail_fx, head_fx, extra_layer = apply_transition(
            tail,
            head_for_fx,
            sr_a,
            chosen_fx,
        )

        wet = max(0.0, min(1.0, self._cfg.transitions.wet_mix))
        if wet < 1.0:
            tail_fx = tail * (1.0 - wet) + tail_fx * wet
            head_fx = head_for_fx * (1.0 - wet) + head_fx * wet

        audio_a_trimmed = np.concatenate(
            [audio_a_trimmed[:-effect_samples], tail_fx],
        ).astype(np.float32)
        if len(head_fx) >= crossfade_samples:
            b_head = head_fx[:crossfade_samples].astype(np.float32)
        return audio_a_trimmed, b_head, extra_layer

    def _mix_overlap(
        self,
        audio_a_trimmed: np.ndarray,
        b_head: np.ndarray,
        crossfade_samples: int,
        sr_a: int,
        extra_layer: np.ndarray,
    ) -> np.ndarray:
        """Run the linear or EQ-ducked crossfade and overlay any extra layer."""
        cfg_pb = self._cfg.playback
        if cfg_pb.crossfade_eq_duck:
            mixed = _apply_crossfade_ducked(
                audio_a_trimmed,
                b_head,
                crossfade_samples,
                sample_rate=sr_a,
                bass_cutoff_hz=cfg_pb.crossfade_bass_cutoff_hz,
            )
        else:
            mixed = _apply_crossfade(audio_a_trimmed, b_head, crossfade_samples)

        if len(extra_layer) > 0:
            wet = max(0.0, min(1.0, self._cfg.transitions.wet_mix))
            overlap_end = len(audio_a_trimmed)
            ex_start = max(0, overlap_end - len(extra_layer))
            ex_len = overlap_end - ex_start
            if ex_len > 0 and overlap_end <= len(mixed):
                mixed[ex_start:overlap_end] += extra_layer[-ex_len:] * wet
                np.clip(
                    mixed[ex_start:overlap_end],
                    -1.0,
                    1.0,
                    out=mixed[ex_start:overlap_end],
                )
        return mixed

    def _play_with_crossfade(
        self,
        current: IndexEntry,
        next_entry: IndexEntry,
    ) -> None:
        """Load, crossfade, and play *current* into *next_entry*.

        Blocks until the track finishes (or a skip is requested).  Heavy
        lifting happens in the helper methods above; this method is the
        orchestrator that wires the phases together.

        Args:
            current: The outgoing track.
            next_entry: The incoming track (used only for the crossfade tail).
        """
        try:
            audio_a, sr_a = load_audio(str(current.path))
        except (OSError, ValueError, RuntimeError) as exc:
            logger.error("Cannot load %s: %s — skipping.", current.path, exc)
            return

        audio_a = self._apply_replaygain(audio_a, current.path)
        self._load_lyrics(current.path)
        self._ensure_dj_cache()
        meta_a = self._outgoing_meta(audio_a, sr_a, current.path)

        # Mixxx-style transition_mode resolution.  Derives the effective
        # crossfade length from the mode + DJ-meta markers; falls back to
        # cfg.playback.crossfade_seconds when markers are missing.
        meta_b = self._peek_incoming_meta(next_entry)
        eff_crossfade_s = self._effective_crossfade_seconds(
            meta_a,
            meta_b,
            current.length,
        )
        crossfade_samples = int(eff_crossfade_s * sr_a)
        a_crossfade_start = self._crossfade_start_in_a(
            audio_a,
            sr_a,
            meta_a,
            crossfade_samples,
        )

        audio_b = self._load_incoming(next_entry, sr_a, crossfade_samples)
        audio_b = self._maybe_beatmatch(audio_b, current, next_entry)
        audio_b = self._skip_incoming_intro(audio_b, sr_a, next_entry)

        # Trim audio_a so its tail begins at a_crossfade_start.
        if a_crossfade_start + crossfade_samples > len(audio_a):
            crossfade_samples = max(0, len(audio_a) - a_crossfade_start)
        audio_a_trimmed = audio_a[: a_crossfade_start + crossfade_samples]

        if crossfade_samples > 0 and len(audio_b) >= crossfade_samples:
            b_head = audio_b[:crossfade_samples]
            audio_a_trimmed = self._apply_outgoing_filter_sweep(
                audio_a_trimmed,
                sr_a,
                crossfade_samples,
            )
            audio_a_trimmed, b_head, extra_layer = self._apply_transition_effect(
                audio_a_trimmed,
                audio_b,
                b_head,
                sr_a,
                crossfade_samples,
            )
            mixed = self._mix_overlap(
                audio_a_trimmed,
                b_head,
                crossfade_samples,
                sr_a,
                extra_layer,
            )
        else:
            mixed = audio_a_trimmed

        self._stream_audio(mixed, sr_a)

    def _stream_audio(self, audio: np.ndarray, sr: int) -> None:
        """Stream a mono float32 audio array through sounddevice.

        Blocks until playback finishes, is skipped, or stopped.
        Volume, mute, and seek are applied in real time via shared state.

        Args:
            audio: Mono float32 audio array.
            sr: Sample rate of *audio* in Hz.
        """
        self._current_sr = sr
        self._playback_len = len(audio)
        pos = self._playback_pos
        pos[0] = 0

        # Build EQ filters for this sample rate (cheap; cached per-stream).
        # Skipped entirely when all 3 bands are at unity (the common case).
        self._eq_filters = make_eq_filters(sr)

        finished = threading.Event()

        def callback(outdata, frames, time_info, status) -> None:  # type: ignore[no-untyped-def]
            if self._state.is_paused or self._skip_event.is_set() or self._state.should_stop:
                outdata[:] = 0
                if self._skip_event.is_set() or self._state.should_stop:
                    raise sd.CallbackStop()
                return

            chunk = audio[pos[0] : pos[0] + frames]

            # 3-band EQ — skip when all bands at unity (no allocation, no filtering)
            if (
                self._eq_filters is not None
                and (self._eq_low != 1.0 or self._eq_mid != 1.0 or self._eq_high != 1.0)
                and len(chunk) > 0
            ):
                chunk = apply_eq(
                    chunk,
                    self._eq_filters,
                    self._eq_low,
                    self._eq_mid,
                    self._eq_high,
                )

            if len(chunk) < frames:
                outdata[: len(chunk), 0] = chunk
                outdata[len(chunk) :] = 0
            else:
                outdata[:, 0] = chunk

            # Apply volume / mute
            if self._state.is_muted:
                outdata[:] = 0
            elif self._state.volume < 1.0:
                outdata[:] *= self._state.volume

            pos[0] += frames
            if len(chunk) < frames:
                raise sd.CallbackStop()

        # Resolve the configured output device.  None = system default.
        device = getattr(self._cfg.playback, "audio_device", None) or None
        try:
            with sd.OutputStream(
                samplerate=sr,
                channels=1,
                dtype="float32",
                callback=callback,
                finished_callback=finished.set,
                device=device,
            ):
                while not finished.is_set():
                    if self._state.is_paused:
                        time.sleep(0.05)
                    else:
                        finished.wait(timeout=0.1)
        except Exception as exc:
            logger.error("Playback error: %s", exc)

    def _setup_keyboard(self) -> None:
        """Start the pynput keyboard listener in a background thread."""
        try:
            from pynput import keyboard

            def on_press(key) -> None:  # type: ignore[no-untyped-def]
                try:
                    char = key.char.lower() if hasattr(key, "char") and key.char else None
                except Exception:
                    char = None

                if key == keyboard.Key.space:
                    self._state.is_paused = not self._state.is_paused
                    self._refresh_status()

                elif char == "n":
                    _CONSOLE.print("  [dim]→ Skip[/dim]")
                    self._skip_event.set()

                elif char == "q":
                    _CONSOLE.print("  [dim]Quit[/dim]")
                    self._state.should_stop = True
                    self._skip_event.set()

                elif key == keyboard.Key.right:
                    seek = _SEEK_SECONDS * self._current_sr
                    self._playback_pos[0] = min(
                        self._playback_len - 1,
                        self._playback_pos[0] + seek,
                    )
                    _CONSOLE.print(f"  [dim]Seek +{_SEEK_SECONDS}s[/dim]")

                elif key == keyboard.Key.left:
                    seek = _SEEK_SECONDS * self._current_sr
                    self._playback_pos[0] = max(0, self._playback_pos[0] - seek)
                    _CONSOLE.print(f"  [dim]Seek -{_SEEK_SECONDS}s[/dim]")

                elif key == keyboard.Key.up:
                    self._state.volume = min(1.0, self._state.volume + _VOLUME_STEP)
                    self._refresh_status()

                elif key == keyboard.Key.down:
                    self._state.volume = max(0.0, self._state.volume - _VOLUME_STEP)
                    self._refresh_status()

                elif char == "m":
                    self._state.is_muted = not self._state.is_muted
                    self._refresh_status()

                elif char == "d" and self._discovery_every is not None:
                    self._state.discovery_enabled = not self._state.discovery_enabled
                    status = "ON" if self._state.discovery_enabled else "OFF"
                    _CONSOLE.print(f"  [bold cyan]\u25c8 Discovery {status}[/bold cyan]")
                    self._refresh_status()

            listener = keyboard.Listener(on_press=on_press)
            listener.daemon = True
            listener.start()
        except Exception as exc:
            logger.warning("Keyboard controls unavailable: %s", exc)
