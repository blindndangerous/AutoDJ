"""Auto-DJ player: next-track picking and the server-side crossfade mix.

``autodj serve`` drives the player; the web page is its control surface.
Server audio (``--server-audio`` or ``--stream``) plays through a
:class:`~autodj.mixbus.MixBus` fed by a render-ahead worker thread
(:class:`~autodj.render_ahead.RenderAhead`): each track is rendered in stereo
with the head of the next track crossfaded into its tail, and the bus streams
the result to the sound card (:class:`~autodj.sound_output.SoundDeviceOutput`)
or the stream encoder in 20 ms blocks.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

import numpy as np
from rich.console import Console
from scipy.signal import butter, sosfilt

# Heavy / platform-specific audio deps are imported with graceful None
# fallback so hosts without them (a NAS running browser-driven `serve`)
# can still construct a `Player` for track-picking.  Functions that
# need real audio import lazily on first use.
try:
    import soundfile as _sf_mod

    sf: Any = _sf_mod
except ImportError:  # pragma: no cover — minimal install path
    sf = None
# sounddevice is imported only by autodj.sound_output, lazily, when server
# audio actually opens a device.

from autodj.indexer import IndexEntry
from autodj.mixbus import BusEvents, MixBus, RenderedTrack
from autodj.render_ahead import RenderAhead
from autodj.stereo import (
    FFMPEG_FORMATS,
    SAMPLE_RATE,
    TrackTooLongError,
    envelope,
    load_stereo,
    load_with_ffmpeg,
    mono,
    per_channel,
    to_stereo,
)

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig
    from autodj.dj_meta import Cue, DjMeta
    from autodj.presets import Preset
    from autodj.similarity import SimilarityIndex

logger = logging.getLogger(__name__)

# Default output sample rate; sounddevice converts if the device differs.
_DEFAULT_SR = 44_100

# Rich console for transient terminal notes (the discovery marker).
_CONSOLE = Console()


# ---------------------------------------------------------------------------
# Crossfade math (pure functions — testable without audio hardware)
# ---------------------------------------------------------------------------


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
        audio_a: Mono or stereo float32 audio array for the outgoing track.
        audio_b: Mono or stereo float32 audio array for the incoming track.
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

    fade_out = np.linspace(1.0, 0.0, crossfade_samples, dtype=np.float32)
    fade_in = np.linspace(0.0, 1.0, crossfade_samples, dtype=np.float32)

    # Regions
    a_body = audio_a[: len(audio_a) - crossfade_samples]
    a_tail = audio_a[len(audio_a) - crossfade_samples :]
    b_head = audio_b[:crossfade_samples]
    b_body = audio_b[crossfade_samples:]

    overlap = (a_tail * envelope(fade_out, a_tail)) + (b_head * envelope(fade_in, b_head))

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
    progressively attenuated by blending in a Butterworth high-pass of
    the outgoing tail, while the incoming track fades in normally.  This eliminates
    the muddy bass build-up that two simultaneously-playing tracks
    produce in the sub-200 Hz range — the core trick used by pro DJs
    when manually mixing.

    Falls back to plain :func:`_apply_crossfade` if the crossfade region
    is too short for filter design.

    Args:
        audio_a: Mono or stereo float32 audio array for the outgoing track.
        audio_b: Mono or stereo float32 audio array for the incoming track.
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
        a_tail_hp = cast(np.ndarray, sosfilt(sos, a_tail, axis=0)).astype(np.float32)
    except (ValueError, RuntimeError):  # pragma: no cover — degenerate sample rate
        return _apply_crossfade(audio_a, audio_b, crossfade_samples)

    # Bass-duck envelope: 0.0 = full bass, 1.0 = bass fully removed.
    # Ramp follows a quarter-sine for a smoother feel than a straight line.
    t = np.linspace(0.0, 1.0, crossfade_samples, dtype=np.float32)
    bass_remove = np.sin(t * (np.pi / 2.0)).astype(np.float32)

    # Mix unfiltered tail with fully-bass-cut tail by the duck envelope
    duck = envelope(bass_remove, a_tail)
    a_ducked = a_tail * (1.0 - duck) + a_tail_hp * duck

    # Standard amplitude fades on top of the ducking
    fade_out = np.linspace(1.0, 0.0, crossfade_samples, dtype=np.float32)
    fade_in = np.linspace(0.0, 1.0, crossfade_samples, dtype=np.float32)

    overlap = (a_ducked * envelope(fade_out, a_ducked)) + (b_head * envelope(fade_in, b_head))
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
        audio: Mono or stereo float32 audio array.
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
        return per_channel(
            lambda channel: librosa.effects.time_stretch(y=channel, rate=1.0 / ratio),
            audio,
        ).astype(np.float32)
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
        audio_b: Mono or stereo float32 audio of the incoming track.
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

    Falls back to the unfiltered input when the cutoff range is invalid.

    Args:
        audio: Mono or stereo float32 audio array.
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
            filt = cast(np.ndarray, sosfilt(sos, chunk, axis=0)).astype(np.float32)
        except (ValueError, RuntimeError):
            filt = chunk

        # Smooth the boundary between the previous filter pass and this one
        if prev_tail is not None and blend > 0 and len(filt) >= blend:
            fade = envelope(np.linspace(0.0, 1.0, blend, dtype=np.float32), filt)
            filt[:blend] = prev_tail * (1.0 - fade) + filt[:blend] * fade

        out[start_idx:end_idx] = filt
        prev_tail = filt[-blend:].copy() if len(filt) >= blend else None

    return out


# ---------------------------------------------------------------------------
# Player state
# ---------------------------------------------------------------------------


def effective_no_repeat_window(configured: int, library_size: int) -> int:
    """Return the no-repeat window the picker can honour for *library_size*.

    A library of 200 tracks with a window of 500 would refuse to repeat any
    track once every track had played, then fall through the relaxation
    paths and either fail or return the same neighbour on every advance.
    At least 4 unplayed candidates (or a tenth of the library) are kept.
    """
    if library_size <= 0 or configured < library_size:
        return configured
    effective = max(1, library_size - max(4, library_size // 10))
    logger.warning(
        "playback.no_repeat_window=%d but library has only %d tracks; "
        "using %d so the picker always has candidates.",
        configured,
        library_size,
        effective,
    )
    return effective


def apply_repeat_windows(player: Any) -> None:
    """Resize *player*'s repeat windows to its configured values.

    Used when the web UI or restored web state changes
    ``playback.no_repeat_window`` or ``playback.artist_repeat_window`` on
    a running player.
    """
    pb = player._cfg.playback
    sim = player._sim
    library_size = int(sim.ntotal) if sim is not None else 0
    player._state.resize_repeat_windows(
        effective_no_repeat_window(int(pb.no_repeat_window), library_size),
        int(pb.artist_repeat_window),
    )


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
        queue_lock: Guards ``queue`` and ``queued_next``.  The web thread
            edits them while the server-audio thread pops from them, so
            every read-modify-write of either must hold this lock.
            Reentrant because some holders call into others.
        history_lock: Guards the recently-played deques and
            ``track_number``.  The mix bus records track starts, request
            threads record browser-mode advances and resize the windows,
            and picks copy the history (``Player._pick_lock`` is this
            lock).  A leaf lock: hold it only for the record, resize or
            copy itself.
    """

    current_track: IndexEntry | None = None
    next_track: IndexEntry | None = None
    queued_next: IndexEntry | None = None  # set by web UI "play next/now"
    queue: list[IndexEntry] = field(default_factory=list)  # web UI ordered queue
    # Track that was playing when the user first added to the queue.
    # Used only when [playback] post_queue_seed = "pre_queue" to seed
    # similarity after the queue drains.  Cleared when the queue
    # empties through advance or when manually emptied via remove /
    # reorder.
    pre_queue_seed: IndexEntry | None = None
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
    queue_lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)
    history_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Initialise the bounded recently-played deques."""
        self.recently_played = deque(maxlen=self.no_repeat_window)
        w = max(0, self.artist_repeat_window)
        self.recently_played_artists = deque(maxlen=w)
        self.recently_played_albums = deque(maxlen=w)
        self.recently_played_titles = deque(maxlen=w)

    def resize_repeat_windows(self, no_repeat_window: int, artist_repeat_window: int) -> None:
        """Change both repeat windows, keeping the most recent history.

        A shorter window keeps the newest entries; a longer one keeps all
        of them and simply remembers more from now on.  The deques are
        replaced under ``history_lock``, the lock every record and copy
        holds, so a track start is never recorded into a deque that is
        being thrown away, and a copy never iterates one being resized.
        """
        with self.history_lock:
            self.no_repeat_window = no_repeat_window
            self.artist_repeat_window = artist_repeat_window
            self.recently_played = deque(self.recently_played, maxlen=no_repeat_window)
            w = max(0, artist_repeat_window)
            self.recently_played_artists = deque(self.recently_played_artists, maxlen=w)
            self.recently_played_albums = deque(self.recently_played_albums, maxlen=w)
            self.recently_played_titles = deque(self.recently_played_titles, maxlen=w)

    def record_played(self, entry: IndexEntry) -> None:
        """Record a track as recently played.

        Tracks file path, artist, album, and title so the picker can
        avoid back-to-back same-artist sequences, two songs from the
        same album in a row, and re-runs of the same title (which
        catches different recordings / live versions of one song).

        Hold ``history_lock``.

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


@dataclass(frozen=True)
class _PickContext:
    """The shared state one pick reads, copied up front.

    The similarity search takes a while, and the mix bus records track
    starts meanwhile.  Copying the history once, under ``_pick_lock``,
    means the search never holds that lock and never sees the history
    change half way through.

    Attributes:
        recent: Paths the pick must not repeat.
        artists: Lower-cased artists to avoid.
        albums: Lower-cased albums to avoid.
        titles: Lower-cased titles to avoid.
        track_number: ``PlayerState.track_number`` when the copy was made.
        peek_queue: Peek at the user queue instead of popping it (render
            ahead: a queue pick leaves the queue when it starts).
        queue_reserved: A pending track peeked from the queue that has not
            started yet; its queue slot is skipped.
        protected: Paths no pick may return even after relaxing the repeat
            window: the track the pick follows, the track the bus is
            starting, the latest recorded track and the playing one.
    """

    recent: deque
    artists: set[str]
    albums: set[str]
    titles: set[str]
    track_number: int
    peek_queue: bool = False
    queue_reserved: IndexEntry | None = None
    protected: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# Audio loading
# ---------------------------------------------------------------------------


def load_audio(path: str, target_sr: int = _DEFAULT_SR) -> tuple[np.ndarray, int]:
    """Load an audio file as a mono float32 array.

    Uses FFmpeg for MP4-container audio such as ALAC, ``soundfile`` for
    the rest, and falls back to ``librosa`` for files soundfile rejects.

    Args:
        path: Absolute path to the audio file.
        target_sr: Target sample rate for the librosa fallback, which
            resamples to it.  A file soundfile reads keeps its native rate.

    Returns:
        A tuple ``(audio, sample_rate)`` where *audio* is a mono float32
        array and *sample_rate* is the actual rate after any resampling.

    Raises:
        RuntimeError: ``soundfile.LibsndfileError`` when neither decoder can
            read the file, or FFmpeg's error for MP4-container audio.
    """
    if Path(path).suffix.lower() in FFMPEG_FORMATS:
        return load_with_ffmpeg(path, channels=1)
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
        dry_run: Browser mode: pick tracks but play nothing on the server;
            the web page plays the audio.
        stream_mode: Serve the mix as a radio stream: the mix bus is built
            up front and a :class:`~autodj.station.Station` starts and stops
            sets as listeners come and go.
        server_audio_too: In stream mode, also play the mix on the sound card.
    """

    # The track rendered just before the render cursor's track: the bus is
    # starting it while the worker picks what follows the cursor's track.
    _pending_previous: IndexEntry | None = None
    # A queue pick the bus has taken (which removed it from the queue) and
    # not yet started: it goes back if its set stops first.  Only the bus
    # thread touches it (_take_render, then _on_track_start).
    _taken_queue_pick: RenderedTrack | None = None

    def __init__(
        self,
        cfg: AutoDJConfig,
        sim_index: SimilarityIndex,
        dry_run: bool = False,
        stream_mode: bool = False,
        server_audio_too: bool = False,
    ) -> None:
        """Initialise the player with configuration and index.

        No model is needed at play time — next-track vectors are looked up
        directly from the pre-built FAISS index.

        Args:
            cfg: Full :class:`~autodj.config.AutoDJConfig` instance.
            sim_index: Loaded :class:`~autodj.similarity.SimilarityIndex`.
            dry_run: Browser mode: pick tracks but play nothing on the server.
            stream_mode: Build the mix bus now and leave starting sets to
                the stream station (see :meth:`begin_set`).
            server_audio_too: In stream mode, also open the sound card.
        """
        self._cfg = cfg
        self._sim = sim_index
        self._dry_run = dry_run
        self._stream_mode = stream_mode
        self._server_audio_too = server_audio_too
        # Picker settings.  They start off (discovery at the configured
        # rate) and the web page's settings routes change them.
        self._preset: Preset | None = None
        self._bpm_range: tuple[float, float] | None = None
        # Anchored mode: when True, every similarity query uses the SEED
        # vector rather than the currently-playing track.  Prevents the
        # session from drifting away from where the user started — each
        # next track is similar to the seed, not to the previous track.
        # Off by default; toggled from the web UI.
        self._anchor_to_seed = False
        # Path of the seed track — set in run() / set externally by the
        # bridge when the user picks a fresh seed.  Used by anchored mode.
        self._seed_path: str | None = None
        # Pure shuffle: pick random next track, completely ignore similarity.
        # When the user disables pure-shuffle mid-set,
        # the next pick uses similarity from the current track — so they can
        # use shuffle to stumble onto a song they like, then "lock in" by
        # toggling shuffle off and let the auto-DJ continue from there.
        self._pure_shuffle = False
        # Lyrics for the current track — populated when each track loads,
        # consumed by the web UI via PlayerBridge.get_state().
        self._current_lyrics: list = []
        self._current_lyrics_plain: str = ""
        # 3-band EQ state (1.0 = unity), mutated by the web UI
        self._eq_low: float = 1.0
        self._eq_mid: float = 1.0
        self._eq_high: float = 1.0
        # DJ meta cache — initialised lazily on first use so tests with
        # mock configs don't trip on the cache load.
        from autodj.dj_meta import DjMetaCache as _DjMetaCache

        self._dj_cache: _DjMetaCache | None = None
        self._dj_cache_initialised = False
        # Browser-driven mode never renders on the server, so tracks are
        # analysed on background threads instead.  Track in-flight analyses
        # by path so a flurry of advances does not spawn duplicate
        # workers for the same track.  Lock guards the set; the heavy
        # I/O happens off-lock.
        self._bg_analysis_inflight: set[str] = set()
        self._bg_analysis_threads: set[threading.Thread] = set()
        self._bg_analysis_lock = threading.Lock()
        # Cues from DJ-software libraries: None until load_library_cues has
        # read them; tracks analysed before then are listed for a late merge.
        self._library_cues: dict[str, list[Cue]] | None = None
        self._analysed_before_library_cues: list[str] = []
        self._library_cues_lock = threading.Lock()
        self._bg_lyrics_inflight: set[str] = set()
        self._bg_lyrics_lock = threading.Lock()
        # Current track's beatmatch ratio (1.0 = no stretch) — exposed via state
        self._beatmatch_ratio: float = 1.0
        # Last transition effect applied (string name) — exposed via state
        self._last_transition_fx: str = "none"
        # Shared RNG for transition effects that use randomness, so a single
        # seed drives both stereo channels identically (see apply_transition).
        self._rng: np.random.Generator = np.random.default_rng()
        # Pick provenance — set by _pick_next describing HOW the current
        # track was selected.  Read by the bridge to build the
        # "why this track" sentence list shown in the web UI.
        self._last_pick_mode: str = "seed"
        # Previous track played — kept so the explainer can compute deltas
        # against the current pick.
        self._previous_track: IndexEntry | None = None
        self._discovery_every: int | None = cfg.playback.discovery_every
        # Clamp no_repeat_window to the library size so the picker never
        # has zero candidates available.  Without this, a library of 200
        # tracks with the default window of 500 would refuse to repeat
        # ANY track even after every track had been played, then
        # silently fall through filter-relaxation paths and either
        # raise SimilarityError or return the same neighbour every
        # advance — the "repeats too soon" symptom users hit on small
        # libraries.  Reserve at least 4 unplayed candidates so the
        # picker still has variety.
        library_size = len(getattr(sim_index, "entries", []) or [])
        effective = effective_no_repeat_window(
            int(cfg.playback.no_repeat_window),
            library_size,
        )
        self._state = PlayerState(
            no_repeat_window=effective,
            artist_repeat_window=cfg.playback.artist_repeat_window,
        )
        self._skip_event = threading.Event()
        self._lock = threading.Lock()
        # Shared playback position (samples) — written by the mix bus, read and
        # written by seeks.  Using a list so both sides share the same object.
        self._playback_pos: list[int] = [0]
        self._playback_len: int = 0  # length of the current audio array in samples
        self._current_sr: int = _DEFAULT_SR
        # Server audio: the mix bus that plays rendered tracks (built at the
        # end of __init__ unless dry-run), and a
        # hook the bridge sets to hear about each track the bus starts.
        self.bus: MixBus | None = None
        self.on_track_started: Callable[[IndexEntry], None] | None = None
        # Render cursor for the bus: the next track to render, how many of
        # its samples the previous track's overlap already played, and how
        # it was picked (for "why this track" once it starts).
        self._pending_entry: IndexEntry | None = None
        self._pending_offset: int = 0
        self._pending_pick_mode: str = "seed"
        # Whether the pending track was peeked from the user queue.  Queue
        # picks are peeked when rendered and removed from the queue only
        # when the track starts (see _take_render), so the queue the
        # listener sees stays true; until then this pending track reserves
        # its queue slot and the next peek skips it.
        self._pending_from_queue: bool = False
        # Keeps one rendered track ready so the bus never waits on a render.
        # (Late-bound so a replaced _next_rendered is honoured.)
        self._render_ahead = RenderAhead(
            lambda: self._next_rendered(),
            is_stale=self._queue_pick_is_stale,
            rewind=self._rewind_render_cursor,
            skip_past=self._skip_past_render,
        )
        # The render the bus is playing (set when it starts).
        self._playing_render: RenderedTrack | None = None
        # _pick_lock (PlayerState.history_lock) guards the recently-played
        # history and track_number: the mix bus thread records track starts
        # while a pick copies them (see _pick_context).  It is held only for
        # that copy or that record, never across a similarity search, so a
        # track start never waits for a pick.
        #
        # Lock order: the station lock, then _advance_lock (browser mode
        # only), then either _set_lock or queue_lock -- never both at once:
        # _on_track_start gives a queue pick back only after releasing
        # _set_lock -- then _pick_lock.  _pick_lock is a leaf: nothing else
        # is taken while it is held.  (The bus lock and the render-ahead
        # worker's lock come between the station lock and queue_lock; see
        # _take_render.)
        #
        # _advance_lock: in browser mode, advance_now (a request thread) and
        # the startup pick in _run_headless (the player thread) both change
        # the now-playing state; holding it makes each change whole.
        self._advance_lock = threading.Lock()
        # Set once the picker has fallen back to a random track, so one
        # episode of failed picks logs one warning (see _fallback_pick).
        self._pick_fallback_active = False
        self._dj_cache_lock = threading.Lock()
        self._dj_cache_retry_at = 0.0
        # The seed, recorded by run() before the bus starts it; its first
        # _on_track_start must not record it a second time.
        self._seed_awaiting_start: IndexEntry | None = None
        # Stream sets: begin_set / end_set bump the generation, and a track
        # start stamped with an older one is ignored.  The lock makes the
        # track-start bookkeeping and end_set atomic with respect to each
        # other.
        self._set_generation = 0
        self._set_lock = threading.Lock()
        # Server-side mixing (stream mode or server audio): the bus exists
        # from the start, so the server can register the stream output and
        # the liner scheduler on it.  It stays idle until a set starts.
        if not dry_run:
            self._build_bus()

    @property
    def _pick_lock(self) -> threading.Lock:
        """The history lock (``PlayerState.history_lock``); see :meth:`__init__`."""
        return self._state.history_lock

    def stop(self) -> None:
        """Wake and stop the playback loop, including empty-library waiting."""
        self._state.should_stop = True
        self._skip_event.set()
        self._render_ahead.stop(timeout=0)

    def run(self, seed_entry: IndexEntry | None) -> None:  # pragma: no cover -- end-to-end loop
        """Start the playback loop.

        Plays *seed_entry* first (or picks a random track if ``None``), then
        queries the similarity index for each successive track.  Blocks until
        :attr:`PlayerState.should_stop` is set.

        Args:
            seed_entry: The track to start from.  ``None`` selects a random
                track from the index.
        """
        if self._stream_mode:
            # No seed: the station starts each set when a listener arrives.
            self._run_stream()
            return
        if seed_entry is None:
            while not self._state.should_stop:
                seed_entry = self._random_start_entry()
                if seed_entry is not None:
                    break
                self._skip_event.wait(timeout=0.25)
                self._skip_event.clear()
            if self._state.should_stop:
                return
            assert seed_entry is not None
        # Remember the seed so anchored mode can keep coming back to it.
        self._seed_path = seed_entry.path

        self._ensure_dj_cache()

        self._record_seed(seed_entry)
        current = seed_entry

        # Browser mode: the page plays the audio; this thread only picks.
        if self._dry_run:
            self._run_headless(current)
            return

        self._run_server_audio(current)

    def _random_start_entry(self) -> IndexEntry | None:
        """Pick a starting track the way Shuffle does, or ``None`` if there is none."""
        return self._sim.random_entry()

    def _build_bus(self) -> MixBus:
        """Create the mix bus, fed by the render-ahead worker, as :attr:`bus`."""
        self.bus = MixBus(
            BusEvents(
                on_track_start=self._on_track_start,
                on_position=self._on_position,
                on_need_track=self._take_render,
            ),
            eq_gains=lambda: (self._eq_low, self._eq_mid, self._eq_high),
        )
        return self.bus

    def _run_stream(self) -> None:
        """Run the stream-mode mix bus until the player stops.

        The bus starts idle, emitting silence; the station starts and stops
        sets on it.  The render-ahead worker idles until a set gives it a
        track.  With ``server_audio_too`` the mix also plays on the sound
        card.  Blocks until :attr:`PlayerState.should_stop` is set.
        """
        from autodj.sound_output import SoundDeviceOutput

        bus = self.bus
        assert bus is not None  # built in __init__ for stream mode
        self._ensure_dj_cache()
        output = None
        if self._server_audio_too:
            output = SoundDeviceOutput(self._state, self._cfg.playback.audio_device or None)
            bus.add_output(output)
        stop = threading.Event()
        watcher = threading.Thread(
            target=self._stop_when_requested, args=(stop,), name="autodj-bus-stop", daemon=True
        )
        self._render_ahead.start()
        watcher.start()
        try:
            bus.run(stop)
        finally:
            self._render_ahead.stop()
            if output is not None:
                bus.remove_output(output)
                output.close()

    def _run_server_audio(self, current: IndexEntry) -> None:  # pragma: no cover -- sound card
        """Play the set from *current* through the sound card on the mix bus.

        A render-ahead worker keeps the next rendered track ready; the bus
        pulls it when the playing one ends.  Blocks until the player stops.
        Not exercised in CI because it needs a real sound device; its parts (the worker, the bus, the output, the callbacks)
        are tested on their own.
        """
        from autodj.sound_output import SoundDeviceOutput

        self.reset_render_ahead(current, 0, pick_mode="seed")
        assert self.bus is not None  # built in __init__ unless dry-run
        output = SoundDeviceOutput(self._state, self._cfg.playback.audio_device or None)
        self.bus.add_output(output)
        stop = threading.Event()
        watcher = threading.Thread(
            target=self._stop_when_requested, args=(stop,), name="autodj-bus-stop", daemon=True
        )
        try:
            self._render_ahead.start()
            deadline = time.monotonic() + 30.0
            while not self._render_ahead.wait_ready(0.25):
                if self._state.should_stop:
                    return
                if time.monotonic() >= deadline:
                    logger.warning("No track rendered within 30 s; starting on silence")
                    break
            self.bus.start_set()
            watcher.start()
            self.bus.run(stop)
        finally:
            self._render_ahead.stop()
            output.close()

    def _record_seed(self, seed: IndexEntry) -> None:
        """Make *seed* the current track and record it as played.

        The mix bus later announces the seed through :meth:`_on_track_start`,
        which skips recording it again.
        """
        self._state.current_track = seed
        with self._pick_lock:
            self._state.record_played(seed)
        self._seed_awaiting_start = seed

    def _stop_when_requested(self, stop: threading.Event) -> None:
        """Set *stop* once the player is asked to stop.

        Until then, mirror ``PlayerState.is_paused`` onto the bus every
        100 ms, so the web pause toggle (which only flips that flag) pauses
        server audio too.
        """
        while not self._state.should_stop:
            if self.bus is not None:
                self.bus.pause(self._state.is_paused)
            stop.wait(0.1)
        stop.set()

    def _next_rendered(self) -> RenderedTrack | None:
        """Pick and render the next track for the mix bus.

        Renders the pending track from the carried offset into a freshly
        picked successor, then moves the cursor on.  Runs on the
        render-ahead worker thread.

        The pick works on a copy of the history taken under ``_pick_lock``
        (see :meth:`_pick_context`) and never touches ``_last_pick_mode``
        -- that attribute describes the *playing* track -- carrying the
        modes on the render instead.  It treats the pending track as
        already played, because it only gets recorded when it starts.  A
        queue pick is *peeked*, not taken, skipping the pending track's
        own queue slot when it came from the queue; it leaves the queue
        when it starts.

        A track whose render raises (a decoder, analysis or cache error
        that ``load_stereo`` does not catch) is logged and skipped like
        one that cannot be loaded, so one bad file never stalls the
        station: retrying it would fail the same way forever.  A queue
        pick that cannot be rendered at all is dropped from the queue,
        since it will never start.

        A pick that fails outright (nothing at all is left to play, or an
        unexpected picker error) never fails the render either: the track
        is rendered with no successor, so the worker parks on an empty
        cursor instead of retrying the same failing pick every second.

        Returns:
            The rendered track, or ``None`` when nothing could be rendered.
        """
        for _attempt in range(5):
            # The stream set this render belongs to.  A reset (new set or
            # stop) discards a render in flight, so a render that survives
            # to be played carries the generation of the set playing it.
            generation = self._set_generation
            current = self._pending_entry
            if current is None:
                return None
            offset, pick_mode = self._pending_offset, self._pending_pick_mode
            current_from_queue = self._pending_from_queue
            previous = self._pending_previous
            context = self._pick_context(
                pending=current,
                starting=previous,
                peek_queue=True,
                queue_reserved=current if current_from_queue else None,
            )
            next_entry: IndexEntry | None
            try:
                next_entry, next_mode = self._choose_next(current, context)
            except Exception as exc:
                # A failed search already fell back to a random track, so
                # this is an empty (or all-silent) library: play this track
                # out and park on an empty cursor.
                logger.warning(
                    "No track can follow %s (%s); it plays out alone.", current.path, exc
                )
                next_entry, next_mode = None, "similarity"
            try:
                rendered = self._render_track(current, next_entry, offset)
            except Exception:
                logger.exception("Rendering %s failed; skipping it.", current.path)
                rendered = None
            if rendered is None and current_from_queue:
                with self._state.queue_lock:
                    self._commit_queue_pick(current)  # it will never start
            self._set_render_cursor(
                next_entry,
                rendered.next_start_offset if rendered else 0,
                next_mode,
                from_queue=next_mode == "queue",
                # A track that failed to render never starts.
                previous=current if rendered is not None else previous,
            )
            if rendered is not None:
                return replace(
                    rendered,
                    start_offset=offset,
                    pick_mode=pick_mode,
                    from_queue=current_from_queue,
                    next_pick_mode=next_mode,
                    next_from_queue=next_mode == "queue",
                    set_generation=generation,
                    previous_entry=previous,
                )
        return None

    def _set_render_cursor(
        self,
        entry: IndexEntry | None,
        offset: int,
        pick_mode: str,
        from_queue: bool = False,
        previous: IndexEntry | None = None,
    ) -> None:
        """Point the render-ahead cursor at *entry* (worker idle or queued).

        *previous* is the track rendered just before *entry*; ``None`` after
        a jump, where it is not known.
        """
        self._pending_entry = entry
        self._pending_offset = offset
        self._pending_pick_mode = pick_mode
        self._pending_from_queue = from_queue
        self._pending_previous = previous

    def _rewind_render_cursor(self, track: RenderedTrack) -> None:
        """Point the cursor back at *track*'s own start (to re-render it).

        The track before it is the one it was rendered after, so the
        re-render's pick still excludes the track the bus is starting.
        """
        self._set_render_cursor(
            track.entry,
            track.start_offset,
            track.pick_mode,
            track.from_queue,
            previous=track.previous_entry,
        )

    def _skip_past_render(self, track: RenderedTrack) -> None:
        """Point the cursor just after *track* (the bus took it as a fallback).

        *track* is the one starting now, so the next pick excludes it even
        before its start is recorded.
        """
        self._set_render_cursor(
            track.next_entry,
            track.next_start_offset,
            track.next_pick_mode,
            track.next_from_queue,
            previous=track.entry,
        )

    def reset_render_ahead(
        self, entry: IndexEntry | None, offset: int = 0, pick_mode: str = "queue"
    ) -> None:
        """Restart rendering from *entry*, discarding everything rendered.

        Used when a new set starts or the user picks a track to play now.
        Safe while a render is in flight: that render's result is dropped
        and the cursor moves before the next render begins.  Nothing is
        lost from the queue: renders only ever peek at it.

        Args:
            entry: First track to render, or ``None`` to park the worker
                (it renders nothing until the next reset).
            offset: Samples of *entry* to skip (already played).
            pick_mode: How *entry* was chosen, for "why this track".
        """
        with self._state.queue_lock:
            self._render_ahead.reset(lambda: self._set_render_cursor(entry, offset, pick_mode))

    def begin_set(self, entry: IndexEntry, pick_mode: str) -> None:
        """Point a new stream set at *entry*, from its first sample.

        Called by the stream station just before it starts the bus.  The
        set's first track becomes the anchor for anchored mode, and a new
        set always starts unpaused.

        Args:
            entry: The set's first track.
            pick_mode: How it was chosen (``"queue"`` or ``"seed"``).
        """
        with self._set_lock:
            self._set_generation += 1
            self._seed_path = entry.path
            self._state.is_paused = False
        self.reset_render_ahead(entry, 0, pick_mode)

    def end_set(self) -> IndexEntry | None:
        """Park the render-ahead worker and clear the now-playing state.

        Called by the stream station after it stops the bus, so no stale
        render survives into the next set and the page shows nothing
        playing while idle.  A track-start callback still pending from the
        stopped set is ignored (see :meth:`_on_track_start`).

        Returns:
            The track that was playing, which the stop cut short.
        """
        with self._set_lock:
            self._set_generation += 1
            cut_short = self._state.current_track
            self._playing_render = None
            self._state.current_track = None
            self._state.next_track = None
            self._playback_pos[0] = 0
            self._playback_len = 0
            self._current_lyrics = []
            self._current_lyrics_plain = ""
        self.reset_render_ahead(None)
        return cut_short

    def refresh_render_ahead(self) -> None:
        """Re-check the rendered next track after a queue edit.

        Only a render whose queue pick the edit changed is re-rendered
        (see :meth:`_queue_pick_is_stale`), and the old render stays
        playable until its replacement is ready, so an edit never causes
        dead air; if the bus needs it first, the edit lands a track later.
        Call after editing the queue.
        """
        with self._state.queue_lock:
            self._render_ahead.refresh()

    def play_now(self, entry: IndexEntry, pick_mode: str = "queue") -> None:
        """Fade out the playing track and play *entry* next, from its start.

        Queued tracks stay queued (renders only peek at the queue), so the
        track that was coming up plays after *entry*.  If *entry* is not
        rendered by the time the fade ends, the bus plays silence briefly
        until it is -- acceptable for an explicit jump.
        """
        self.reset_render_ahead(entry, 0, pick_mode)
        self._state.next_track = entry
        if self.bus is not None:
            self.bus.skip()

    def _take_render(self) -> RenderedTrack | None:
        """Hand the bus its next track and commit a queue pick as it starts.

        The mix bus's ``on_need_track``.  Popping and committing happen
        together under ``queue_lock``, so the render-ahead worker (woken by
        the pop) cannot peek the queue before the starting track has left
        it.  Lock order is bus lock, then queue lock, then the worker's
        lock; nothing takes the bus lock while holding the queue lock.
        """
        with self._state.queue_lock:
            track = self._render_ahead.pop()
            if track is not None and track.from_queue and self._commit_queue_pick(track.entry):
                # Given back if its set stops before it starts.
                self._taken_queue_pick = track
        return track

    def _commit_queue_pick(self, entry: IndexEntry) -> bool:
        """Remove one queued occurrence of *entry* now that it starts.

        ``queued_next`` is checked first (it is peeked first), then the
        first queue entry with the same path.  If the listener already
        removed it, there is nothing to do: it plays anyway only because
        it was already mixed into the previous track's tail.  Hold
        ``queue_lock``.

        Returns:
            Whether an occurrence was removed.
        """
        state = self._state
        if state.queued_next is not None and state.queued_next.path == entry.path:
            state.queued_next = None
            return True
        for index, queued in enumerate(state.queue):
            if queued.path == entry.path:
                del state.queue[index]
                return True
        return False

    def _peek_user_queue(self, reserved: IndexEntry | None) -> IndexEntry | None:
        """The queue head a render would pick, skipping *reserved*'s slot.

        *reserved* is a pending track that was itself peeked from the
        queue and has not started yet, so it still occupies a slot.  Hold
        ``queue_lock``.
        """
        state = self._state
        waiting = [state.queued_next] if state.queued_next is not None else []
        waiting.extend(state.queue)
        if reserved is not None:
            for index, queued in enumerate(waiting):
                if queued.path == reserved.path:
                    del waiting[index]
                    break
        return waiting[0] if waiting else None

    def _queue_pick_is_stale(self, track: RenderedTrack) -> bool:
        """Whether a queue edit changed what *track* should be followed by.

        A render that took its successor from the queue is stale when the
        queue head (skipping *track*'s own slot) is now something else or
        gone; a render that picked by similarity is stale once the queue
        has anything, because the queue takes priority.
        """
        with self._state.queue_lock:
            head = self._peek_user_queue(track.entry if track.from_queue else None)
        if track.next_from_queue:
            return head is None or track.next_entry is None or head.path != track.next_entry.path
        return head is not None

    def _on_track_start(self, rendered: RenderedTrack) -> None:
        """Update state when the mix bus starts playing *rendered*.

        Everything the UI shows about the playing track -- the pick mode,
        transition effect and beat-match ratio -- is set here, from the
        render, never while that render was being prepared ahead of time.
        Positions are in the track's own timeline: the render's audio
        begins ``rendered.start_offset`` samples into the file.

        A render taken for a stream set that has since stopped (its
        callback ran after :meth:`end_set`) is ignored entirely, except
        that a queue pick goes back to the head of the queue: the bus took
        it, which removed it from the queue, but it never played.  The hook
        is called with ``_set_lock`` released: it can reach the stream
        output, whose listener notifications may start a set.
        """
        entry = rendered.entry
        with self._set_lock:
            stale = rendered.set_generation != self._set_generation
            if not stale:
                self._start_playing(rendered)
        taken_from_queue = self._taken_queue_pick is rendered
        if taken_from_queue:
            self._taken_queue_pick = None  # do not hold its audio
        if stale:
            if taken_from_queue:
                # Outside _set_lock: queue_lock is never held together with it.
                with self._state.queue_lock:
                    self._state.queue.insert(0, entry)
            return
        self._current_lyrics = []
        self._current_lyrics_plain = ""
        self.load_lyrics_in_background(entry.path)
        if self.on_track_started is not None:
            self.on_track_started(entry)

    def _start_playing(self, rendered: RenderedTrack) -> None:
        """Make *rendered* the playing track and record it (hold ``_set_lock``)."""
        entry = rendered.entry
        self._playing_render = rendered
        if entry is not self._state.current_track:
            self._previous_track = self._state.current_track
        self._state.current_track = entry
        self._state.next_track = rendered.next_entry
        self._current_sr = SAMPLE_RATE
        self._playback_pos[0] = rendered.start_offset
        self._playback_len = rendered.start_offset + len(rendered.audio)
        self._last_transition_fx = rendered.transition_fx
        self._beatmatch_ratio = rendered.beatmatch_ratio
        self._last_pick_mode = rendered.pick_mode
        seed_start = entry is self._seed_awaiting_start
        with self._pick_lock:
            if seed_start:
                self._seed_awaiting_start = None  # run() already recorded it
            else:
                self._state.record_played(entry)
                self._state.track_number += 1

    def _on_position(self, frames: int) -> None:
        """Record mix-bus progress, in the playing track's own timeline."""
        playing = self._playing_render
        self._playback_pos[0] = frames + (playing.start_offset if playing else 0)

    def _run_headless(self, current: IndexEntry) -> None:
        """Track-picking loop with no server audio output.

        Used by browser-driven ``serve`` (the default unless
        ``--server-audio`` is passed, and forced when audio deps are
        missing).  Browser is responsible for actual playback;
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
        #
        # The page can POST /api/advance while the first pick is still
        # searching, so the pick runs unlocked and is committed under
        # _advance_lock only if the seed is still playing and nothing has
        # filled next_track meanwhile; an advance that got in first keeps
        # its state, timer included.
        state = self._state
        picked: tuple[IndexEntry, str] | None = None
        if state.next_track is None:
            try:
                picked = self._choose_next(current, self._pick_context())
            except Exception:
                logger.warning("No track can follow the seed yet", exc_info=True)
        with self._advance_lock:
            if state.current_track is None:
                state.current_track = current
            seed_playing = state.current_track is current
            if seed_playing:
                if picked is not None and state.next_track is None:
                    state.next_track, self._last_pick_mode = picked
                self._current_sr = _DEFAULT_SR
                self._playback_len = int(
                    (current.length if current.length and current.length > 0 else 5.0)
                    * _DEFAULT_SR,
                )
                self._playback_pos[0] = 0
        if seed_playing:
            # Browser-driven mode never renders on the server, so lyrics and
            # DJ meta (cue points, intro_end_s, outro_start_s, beat grid)
            # would otherwise stay empty for the seed track and the web UI
            # would hide its lyrics card / show an empty cue list even when
            # the data was easy to derive.  Both can touch slow NAS/local
            # audio files, so keep them off the control path.
            self.load_lyrics_in_background(current.path)
            self.analyse_track_in_background(current.path)

        # Park until shutdown.  No fallback timer, no auto-advance.
        while not self._state.should_stop:  # pragma: no cover
            self._skip_event.wait(timeout=1.0)
            self._skip_event.clear()

    def _pop_user_queue(
        self, peek: bool = False, reserved: IndexEntry | None = None
    ) -> IndexEntry | None:
        """Take the next queued / drag-reorder track, or return None.

        Args:
            peek: Only peek (rendering ahead): the track leaves the queue
                when it starts playing.
            reserved: With *peek*, a pending queue pick whose slot to skip.
        """
        with self._state.queue_lock:
            if peek:
                entry = self._peek_user_queue(reserved)
                if entry is None:
                    return None
                source = "Next from queue: %s"
            elif self._state.queued_next is not None:
                entry = self._state.queued_next
                self._state.queued_next = None
                source = "Playing queued track: %s"
            elif self._state.queue:
                entry = self._state.queue.pop(0)
                source = "Playing from queue: %s"
            else:
                return None
        logger.info(source, entry.display_name)
        return entry

    def _pick_context(
        self,
        pending: IndexEntry | None = None,
        *,
        starting: IndexEntry | None = None,
        peek_queue: bool = False,
        queue_reserved: IndexEntry | None = None,
    ) -> _PickContext:
        """Copy the history a pick reads, under ``_pick_lock``.

        While rendering ahead, *pending* (the track the pick follows,
        which has not started yet) is added as if it had just been
        recorded -- pushed through the same bounded windows, so the
        oldest entry drops out exactly as :meth:`PlayerState.record_played`
        would do it.  So is *starting*, the track before it, unless it is
        already the latest recorded track.

        Args:
            pending: A track to treat as just played.
            starting: The track the bus is starting.  The bus hands it over
                first and records it once the block that holds its start
                is done, and the hand-over is what wakes the render-ahead
                worker, so the pick usually runs before it is recorded.
                Without it, every pick could return to the track before,
                and two close tracks played back and forth.
            peek_queue: See :class:`_PickContext`.
            queue_reserved: See :class:`_PickContext`.
        """
        with self._pick_lock:
            paths, artists, albums, titles = self._recent_exclusions(pending, starting)
            track_number = self._state.track_number
            recorded = self._state.recently_played
            latest = recorded[-1] if recorded else None
        playing = self._state.current_track
        protected = {e.path for e in (pending, starting, playing) if e is not None}
        if latest is not None:
            protected.add(latest)
        return _PickContext(
            paths,
            artists,
            albums,
            titles,
            track_number,
            peek_queue,
            queue_reserved,
            frozenset(protected),
        )

    def _recent_exclusions(
        self, pending: IndexEntry | None = None, starting: IndexEntry | None = None
    ) -> tuple[deque, set[str], set[str], set[str]]:
        """Copy the history plus *starting*, then *pending* (hold ``_pick_lock``).

        *starting* is skipped when it is already the latest recorded track
        or is *pending* itself, so it never fills a window slot twice.

        Returns ``(paths, artists, albums, titles)``.
        """
        state = self._state
        recorded = state.recently_played
        if starting is not None and (
            (recorded and recorded[-1] == starting.path)
            or (pending is not None and starting.path == pending.path)
        ):
            starting = None
        added = [entry for entry in (starting, pending) if entry is not None]

        def window(history: deque, values: list[str]) -> deque:
            copy = deque(history, maxlen=history.maxlen)
            copy.extend(value for value in values if value)
            return copy

        def lowered(history: deque, field_name: str) -> set[str]:
            return set(window(history, [getattr(e, field_name).lower() for e in added]))

        return (
            window(recorded, [entry.path for entry in added]),
            lowered(state.recently_played_artists, "artist"),
            lowered(state.recently_played_albums, "album"),
            lowered(state.recently_played_titles, "title"),
        )

    def _pick_pure_shuffle(self, current: IndexEntry, context: _PickContext) -> IndexEntry:
        """Random pick from tracks not in *context*'s history, honouring the BPM range.

        Silent tracks are never picked.  When every eligible track was
        played recently, the history is relaxed to *current* and the
        protected tracks (the one starting and the one playing), so the
        pick never returns to any of them.
        """
        import random as _rnd

        from autodj.similarity import SimilarityError

        excluded = set(context.recent)

        def eligible(entry: IndexEntry) -> bool:
            if entry.is_silent:
                return False
            if self._bpm_range is None:
                return True
            lo, hi = self._bpm_range
            return entry.bpm > 0 and lo <= entry.bpm <= hi

        entries = self._sim.entries_snapshot()
        pool = [e for e in entries if e.path not in excluded and eligible(e)]
        if not pool:
            logger.warning(
                "Pure shuffle exhausted eligible non-recent tracks; relaxing recent-track exclusion"
            )
            protected = context.protected | {current.path}
            pool = [e for e in entries if e.path not in protected and eligible(e)]
        if not pool:
            raise SimilarityError("No candidates satisfy hard filters for pure shuffle.")
        return _rnd.choice(pool)  # nosec B311 -- non-security

    def _try_discovery(self, current: IndexEntry, context: _PickContext) -> IndexEntry | None:
        """Discovery injection when rate is set, toggle ON, and the track number aligns."""
        from autodj.similarity import SimilarityError

        tn = context.track_number
        if not (
            self._discovery_every is not None
            and self._state.discovery_enabled
            and tn > 0
            and tn % self._discovery_every == 0
        ):
            return None
        try:
            entry = self._sim.find_distant(current.path, context.recent)
            _CONSOLE.print("  [bold cyan]◈ Discovery track[/bold cyan]")
            return entry
        except SimilarityError:
            return None

    def _resolve_query_path(self, current_path: str) -> tuple[str, str]:
        """Choose the query path and its pick mode (anchored or similarity)."""
        if self._anchor_to_seed and self._seed_path:
            return self._seed_path, "anchored"
        return current_path, "similarity"

    def _pick_next(self, current: IndexEntry) -> IndexEntry:
        """Select the next track now and show how it was picked.

        The browser-driven path's pick: it pops the user queue and sets
        ``_last_pick_mode``.  Server audio picks through
        :meth:`_next_rendered` instead.

        Args:
            current: The track currently playing.

        Returns:
            The recommended next :class:`~autodj.indexer.IndexEntry`.
        """
        entry, mode = self._choose_next(current, self._pick_context())
        self._last_pick_mode = mode
        return entry

    def _choose_next(self, current: IndexEntry, context: _PickContext) -> tuple[IndexEntry, str]:
        """Select the track to follow *current* and say how it was chosen.

        Selection priority:
        1. A track waiting in the user queue.
        2. Pure shuffle, when on.
        3. Discovery, when enabled and due: :meth:`SimilarityIndex.find_distant`
           (bypasses BPM shaping -- discovery tracks are intentionally
           surprising).
        4. Normal FAISS similarity search, optionally biased by preset BPM.

        Relaxes the no-repeat window to the protected tracks if it covers
        the entire index.  When the pick still fails -- the hard filters
        (BPM range, harmonic mode) leave nothing, *current* has left the
        index after a reload, or anything else goes wrong in the search --
        a random track is played instead (see :meth:`_fallback_pick`), so
        a failed pick never stops the music.  Reads the history only from
        *context*, so it runs without ``_pick_lock``.

        Args:
            current: The track the pick follows.
            context: History copied by :meth:`_pick_context`.

        Returns:
            ``(entry, pick_mode)``.

        Raises:
            SimilarityError: The library has no track that is not silent.
        """
        queued = self._pop_user_queue(context.peek_queue, context.queue_reserved)
        if queued is not None:
            return queued, "queue"
        try:
            picked = self._choose_by_mode(current, context)
        except Exception as exc:
            return self._fallback_pick(current, context, exc), "fallback"
        if self._pick_fallback_active:
            self._pick_fallback_active = False
            logger.info("Picking the next track works again.")
        return picked

    def _choose_by_mode(self, current: IndexEntry, context: _PickContext) -> tuple[IndexEntry, str]:
        """Pick by pure shuffle, discovery or similarity (see :meth:`_choose_next`)."""
        if self._pure_shuffle:
            return self._pick_pure_shuffle(current, context), "pure_shuffle"

        from autodj.similarity import SimilarityError

        discovery = self._try_discovery(current, context)
        if discovery is not None:
            return discovery, "discovery"

        preset = self._preset
        target_bpm = preset.target_bpm(context.track_number) if preset else None
        bpm_weight = preset.bpm_weight if preset else 0.2
        n_candidates = 50 if (target_bpm is not None or self._bpm_range is not None) else 30
        harmonic_mode = self._cfg.djmix.harmonic_mode
        harmonic_only = harmonic_mode != "off"
        query_path, mode = self._resolve_query_path(current.path)

        search: dict[str, Any] = {
            "current_path": query_path,
            "n_candidates": n_candidates,
            "target_bpm": target_bpm,
            "bpm_weight": bpm_weight,
            "bpm_range": self._bpm_range,
            "harmonic_only": harmonic_only,
            "harmonic_mode": harmonic_mode,
            "excluded_artists": context.artists,
            "excluded_albums": context.albums,
            "excluded_titles": context.titles,
            "pick_top_k": self._cfg.playback.pick_top_k,
            "pick_temperature": self._cfg.playback.pick_temperature,
        }

        # --- Normal similarity search ---
        try:
            return self._sim.find_next_for_path(recently_played=context.recent, **search), mode
        except SimilarityError:
            # The repeat window is >= index size — relax it to the tracks
            # that must never follow (this one, the one starting, the one
            # playing), so we can keep playing without going back and forth.
            protected = context.protected | {current.path}
            logger.info(
                "No candidates after applying repeat window (%d tracks) — "
                "relaxing to avoid only the %d tracks around this one.",
                len(context.recent),
                len(protected),
            )
            relaxed = self._sim.find_next_for_path(
                recently_played=deque(sorted(protected)), **search
            )
            return relaxed, mode

    def _fallback_pick(
        self, current: IndexEntry, context: _PickContext, reason: Exception
    ) -> IndexEntry:
        """Pick a random track after the normal pick failed with *reason*.

        Ignores every filter (BPM range, harmonic mode, artist and album
        windows) but avoids repeats as far as the library allows: first a
        track outside the recent history, then one that is not protected
        (see :class:`_PickContext`), then anything but *current*, then
        *current* itself (a one-track library).  Silent tracks are never
        picked.  The first fallback of an episode logs a warning; later
        ones log at debug until a normal pick succeeds again.

        Raises:
            SimilarityError: The library has no track that is not silent.
        """
        import random as _rnd

        from autodj.similarity import SimilarityError

        if self._pick_fallback_active:
            logger.debug("Normal pick still failing (%s); picking at random.", reason)
        else:
            self._pick_fallback_active = True
            logger.warning(
                "Could not pick a track to follow %s (%s: %s); picking one at random "
                "instead until the normal pick works again.",
                current.display_name,
                type(reason).__name__,
                reason,
                exc_info=not isinstance(reason, SimilarityError),
            )
        audible = [entry for entry in self._sim.entries_snapshot() if not entry.is_silent]
        recent = set(context.recent) | {current.path}
        for excluded in (recent, context.protected | {current.path}, {current.path}, set()):
            pool = [entry for entry in audible if entry.path not in excluded]
            if pool:
                return _rnd.choice(pool)  # nosec B311 -- non-security
        raise SimilarityError("The library has no track that is not silent.")

    # ------------------------------------------------------------------
    # _render_track helpers — broken out so the renderer stays readable.
    # Each helper owns one phase of the crossfade pipeline.
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

    def _read_lyrics_for_path(self, path: str) -> tuple[list, str]:
        """Return ``(timestamped, plain)`` lyrics for *path* without mutating state.

        Resolution order:
        1. Sibling ``.lrc`` file (timestamped, scrolls in the web UI).
        2. Beets DB ``lyrics`` field.
        3. Embedded ID3/Vorbis/MP4 lyric tags (USLT, LYRICS, ©lyr).

        Sources 2 and 3 are parsed as LRC too: taggers routinely put the
        synced text in the tag rather than a sidecar.  Whichever source
        wins, timestamped text lands in the first element and untimed text
        in the second -- never both.
        """
        from autodj.audio_meta import (
            load_lrc_for,
            parse_embedded_lyrics,
            read_plain_lyrics,
        )

        # Respect the lyric-display toggle — when off we skip ALL lyric
        # work and the web UI hides its card.
        if not self._cfg.playback.show_lyrics:
            return [], ""

        lyrics = load_lrc_for(path)
        if lyrics:
            return lyrics, ""

        plain = ""
        if self._cfg.library.beets_db:
            from autodj.beets import get_lyrics_for_path

            try:
                plain = get_lyrics_for_path(
                    self._cfg.library.beets_db,
                    path,
                    music_dir=self._cfg.library.music_dir,
                )
            except (OSError, ValueError) as exc:
                logger.debug("Beets lyrics lookup failed: %s", exc)
                plain = ""

        # Fall back to embedded tag lyrics when beets has none / no beets at all.
        if not plain:
            try:
                plain = read_plain_lyrics(path)
            except (OSError, ValueError) as exc:
                logger.debug("Embedded lyric tag read failed: %s", exc)
                plain = ""

        # A "plain" lyric field very often *is* LRC: most taggers write the
        # synced text straight into LYRICS / USLT / ©lyr rather than to a
        # sidecar.  Returning it untouched printed raw "[00:17.49]" stamps
        # on screen, sent the whole song to the live region in one breath,
        # and made the current-line highlight impossible.
        #
        # The classification has to weigh the whole tag, not stop at the
        # first bracket that looks like a cue: deciding on one match threw
        # away every untimestamped line ("Written by X / [00:00.00]Intro /
        # Verse one" collapsed to "Intro") and mangled prose that merely
        # mentions a time.  parse_embedded_lyrics requires a clear majority
        # of leading stamps, and cleans the plain branch instead.
        return parse_embedded_lyrics(plain)

    def load_lyrics_in_background(self, path: str) -> None:
        """Load lyrics for *path* on a daemon thread.

        Browser-driven controls must return immediately; embedded tag reads
        can block for tens of seconds on some local/NAS files.  The worker
        publishes results only if the track is still current.
        """
        if not path:
            return
        self._current_lyrics = []
        self._current_lyrics_plain = ""
        if not self._cfg.playback.show_lyrics:
            return
        with self._bg_lyrics_lock:
            if path in self._bg_lyrics_inflight:
                return
            self._bg_lyrics_inflight.add(path)

        def _worker() -> None:
            try:
                lyrics, plain = self._read_lyrics_for_path(path)
                current = self._state.current_track
                if current is not None and current.path == path:
                    self._current_lyrics = lyrics
                    self._current_lyrics_plain = plain
            except Exception as exc:  # pragma: no cover -- defensive thread guard
                logger.debug("Background lyric load failed for %s: %s", path, exc)
            finally:
                with self._bg_lyrics_lock:
                    self._bg_lyrics_inflight.discard(path)

        thread = threading.Thread(
            target=_worker,
            name="autodj-lyrics",
            daemon=True,
        )
        thread.start()

    # Seconds between attempts to open a DJ-meta cache that failed to open.
    _DJ_CACHE_RETRY_S: ClassVar[float] = 30.0

    def _ensure_dj_cache(self) -> None:
        """Lazy-init the DJ-meta cache on first real use.

        Cheap by design: it only opens ``dj_meta.db``.  Safe to call from
        an asyncio handler (e.g. ``PlayerBridge.get_state``) without
        blocking the event loop.

        Thread-safe: a caller that arrives while another thread is opening
        the cache waits for it rather than carrying on without one.  The
        cache counts as initialised only once it opened; after a failure
        (logged once until it succeeds) it is tried again on a later call,
        at most every :attr:`_DJ_CACHE_RETRY_S` seconds.
        """
        if self._dj_cache_initialised:
            return
        with self._dj_cache_lock:
            if self._dj_cache_initialised or time.monotonic() < self._dj_cache_retry_at:
                return
            try:
                from autodj.dj_meta import get_cache

                if isinstance(self._cfg.index.active_dir, Path):
                    self._dj_cache = get_cache(
                        self._cfg.index.active_dir,
                        music_dir=self._cfg.library.music_dir
                        if isinstance(self._cfg.library.music_dir, Path)
                        else None,
                    )
            except (OSError, ValueError, sqlite3.Error) as exc:
                log = logger.debug if self._dj_cache_retry_at else logger.warning
                log("DJ cache unavailable: %s", exc)
                self._dj_cache = None
                self._dj_cache_retry_at = time.monotonic() + self._DJ_CACHE_RETRY_S
                return
            self._dj_cache_initialised = True

    def _outgoing_meta(self, audio_a: np.ndarray, sr_a: int, path: str) -> DjMeta | None:
        """Get / compute DjMeta for the outgoing track when needed for alignment.

        Returns analysed meta when any of these features needs marker
        data: ``djmix.outro_intro_align``, ``djmix.phrase_align``, or any
        marker-driven transition_mode (everything except ``"fixed"``).
        """
        cfg_dj = self._cfg.djmix
        marker_mode = self._cfg.playback.transition_mode != "fixed"
        if self._dj_cache is None or not (
            cfg_dj.outro_intro_align or cfg_dj.phrase_align or marker_mode
        ):
            return None
        meta = self._dj_cache.get(path)
        if not meta.analysed:
            meta = self._analyse(mono(audio_a), sr_a, path)
            self._dj_cache.flush(batch=10)
        return meta

    def _analyse(self, audio: np.ndarray, sr: int, path: str) -> DjMeta:
        """Analyse *path*'s audio the way ``autodj analyse`` does, and cache it.

        Runs :func:`autodj.dj_meta.analyse_audio`, then merges cues imported
        from DJ software through the same function as ``autodj analyse``
        when ``[playback] import_external_cues`` is on, and stores the
        result in the DJ-meta cache (the caller flushes).  It is stored as
        analysed, so ``autodj analyse`` later skips the track.

        Never reads the DJ-software libraries itself: it uses what
        :meth:`load_library_cues` has read.  A track analysed before that
        read finishes gets its library cues merged when it does.
        """
        from autodj.dj_meta import analyse_audio

        meta = analyse_audio(audio, sr)
        import_cues = self._cfg.playback.import_external_cues
        # Merge and store under the lock, so load_library_cues either sees
        # this track in the cache or has already handed over its cues.
        with self._library_cues_lock:
            if import_cues:
                from autodj.dj_cues_import import merge_imported_cues

                if self._library_cues is None:
                    self._analysed_before_library_cues.append(path)
                merge_imported_cues(meta, path, self._library_cues or {})
            if self._dj_cache is not None:
                self._dj_cache.set(path, meta)
        return meta

    def load_library_cues(self) -> None:
        """Read the Mixxx, Rekordbox and Traktor libraries once.

        ``autodj serve`` runs this on its own thread at startup when
        ``[playback] import_external_cues`` is on, so neither playback nor
        pacing waits on a large library file.  Tracks :meth:`_analyse`
        stored before the read finished have their library cues merged here.
        """
        from autodj.dj_cues_import import auto_import_cues
        from autodj.dj_meta import merge_cues

        music_dir = self._cfg.library.music_dir
        found = auto_import_cues(library_root=music_dir if isinstance(music_dir, Path) else None)
        with self._library_cues_lock:
            self._library_cues = found
            waiting, self._analysed_before_library_cues = self._analysed_before_library_cues, []
            cache = self._dj_cache
            late = [path for path in waiting if found.get(path)]
            if cache is None or not late:
                return
            for path in late:
                meta = cache.get(path)
                meta.cues = merge_cues(meta.cues, found[path])
                cache.set(path, meta)
        cache.flush(force=True)

    def analyse_track_in_background(self, path: str) -> None:
        """Run analyse_audio + detect_cues for *path* on a background thread.

        Browser-driven mode (``serve`` without ``--server-audio``) never
        renders a track on the server, so without this hook the DJ-meta
        cache for the playing track stays at ``analysed=False`` and the
        web UI's cue strip + screen-reader cue summary stay empty.

        The worker:

        1. No-ops when the cache already has analysed meta for *path*
           (sidecar hit, or a previous background pass populated it).
        2. No-ops when the path is already in flight on another thread.
        3. Loads the audio file, runs :func:`analyse_audio` (which calls
           :func:`detect_cues` internally) and merges cues imported from DJ
           software (see :meth:`_analyse`), then writes the result back to
           ``self._dj_cache`` and forces a flush so ``dj_meta.db`` grows
           track by track.

        A track analysed before ``intro_start_s`` was measured only gets
        that measured (see :meth:`_record_intro_start`); the rest of its
        analysis is kept.

        Errors at any stage (file gone, decode error, librosa failure)
        are logged at debug and swallowed -- the cue panel just stays
        empty for that track instead of crashing the advance.
        """
        if not path:
            return
        self._ensure_dj_cache()
        if self._dj_cache is None:
            return
        existing = self._dj_cache.get(path)
        if existing.analysed and existing.intro_start_s is not None:
            return
        intro_start_only = existing.analysed

        def _worker() -> None:  # pragma: no cover -- background thread
            try:
                audio, sr = load_audio(path)
                if intro_start_only:
                    self._record_intro_start(path, audio, sr)
                    return
                meta = self._analyse(audio, sr, path)
                if self._dj_cache is not None:
                    self._dj_cache.flush(force=True)
                # BPM + Camelot key live on the IndexEntry, not on
                # DjMeta -- look them up so the log line carries the
                # full picture (cues + intro/outro + tempo + key).
                from autodj.dj_meta import camelot_label

                similarity = self._sim
                e = similarity.entry_for_path(path)
                bpm_str = "BPM ?"
                cam = "--"
                if e is not None:
                    if e.bpm:
                        bpm_str = f"{e.bpm:.0f} BPM"
                    cam = camelot_label(e.key, e.mode)
                logger.info(
                    "Background analysis done: %s -> %d cues, "
                    "intro_end=%.1fs, outro_start=%.1fs, %s, %s",
                    Path(path).name,
                    len(meta.cues),
                    meta.intro_end_s or 0.0,
                    meta.outro_start_s or 0.0,
                    bpm_str,
                    cam,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                logger.warning("Background analysis failed for %s: %s", path, exc)
            finally:
                with self._bg_analysis_lock:
                    self._bg_analysis_inflight.discard(path)
                    self._bg_analysis_threads.discard(threading.current_thread())

        with self._bg_analysis_lock:
            if path in self._bg_analysis_inflight:
                return
            thread = threading.Thread(
                target=_worker,
                name=f"autodj-analyse-{Path(path).name}",
                daemon=True,
            )
            self._bg_analysis_inflight.add(path)
            self._bg_analysis_threads.add(thread)
            try:
                thread.start()
            except BaseException:
                self._bg_analysis_inflight.discard(path)
                self._bg_analysis_threads.discard(thread)
                raise

    def _record_intro_start(self, path: str, audio: np.ndarray, sr: int) -> None:
        """Store the first-sound time of an already analysed track.

        Tracks analysed before ``intro_start_s`` existed have ``None``
        there; measuring it is cheap, so it is filled in when the track
        comes up instead of re-running the whole analysis.
        """
        from autodj.dj_meta import detect_intro_start

        intro_start = detect_intro_start(audio, sr)
        cache = self._dj_cache
        if cache is None:
            return
        # Same lock as _analyse, so a library-cue merge is not overwritten.
        with self._library_cues_lock:
            cache.set(path, replace(cache.get(path), intro_start_s=intro_start))
        cache.flush(force=True)
        logger.debug("Measured first sound of %s at %.2fs", Path(path).name, intro_start)

    def wait_for_background_analysis(self, timeout: float | None = None) -> bool:
        """Wait for every DJ-metadata writer within one total timeout.

        The writers are the registered background-analysis workers and the
        render-ahead worker, which analyses a track it renders when the
        cache has no markers for it.  The render-ahead worker is told to
        stop first; a render in flight (which can spend a while in
        analysis) still finishes before it exits.

        Returns:
            Whether every writer finished in time, so the cache can close.
        """
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        self._render_ahead.stop(timeout=0)
        render_timeout = None if deadline is None else deadline - time.monotonic()
        if not self._render_ahead.join(render_timeout):
            return False
        while True:
            with self._bg_analysis_lock:
                workers = tuple(self._bg_analysis_threads)
            if not workers:
                return True
            for worker in workers:
                if deadline is None:
                    worker.join()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                worker.join(remaining)

    def _peek_incoming_meta(self, next_entry: IndexEntry) -> DjMeta | None:
        """Cache-only DjMeta peek for the incoming track (no audio decode).

        Used by :meth:`_effective_crossfade_seconds` to read the intro markers
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

        Mirrors the browser's ``_resolveFadeSec`` in
        ``static/modules/audio-engine.js`` so
        the server mix and the web UI sound the same.

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
        # The incoming intro runs from its first sound to intro_end_s.
        intro_len: float | None = None
        if meta_b and meta_b.intro_end_s > 0:
            intro_len = float(meta_b.intro_end_s) - float(meta_b.intro_start_s or 0.0)

        def _clamp(v: float) -> float:
            return max(1.0, min(12.0, v))

        if mode == "full_intro_outro" and outro_len is not None and intro_len is not None:
            return _clamp(min(outro_len, intro_len))
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

    def _max_render_seconds(self) -> float:
        """Longest track server-side mixing decodes (``server_max_track_minutes``)."""
        return float(self._cfg.playback.server_max_track_minutes) * 60.0

    def _load_incoming(
        self,
        next_entry: IndexEntry,
        sr_a: int,
        crossfade_samples: int,
    ) -> np.ndarray | None:
        """Load incoming track, resample to sr_a, ReplayGain — silence on failure.

        Returns ``None`` when the track is too long to mix: it is skipped
        (and logged) when its own render comes round.
        """
        try:
            audio_b = load_stereo(str(next_entry.path), sr_a, self._max_render_seconds())
            return self._apply_replaygain(audio_b, next_entry.path)
        except TrackTooLongError:
            return None
        except (OSError, ValueError, RuntimeError) as exc:
            logger.warning("Cannot pre-load next track (%s): %s", next_entry.path, exc)
            return np.zeros((crossfade_samples, 2), dtype=np.float32)

    def seek_to(self, seconds: float) -> float:
        """Seek the active track to *seconds* (absolute, from track start).

        Clamps to ``[0, length - 0.1 s]`` so the seek can never overshoot
        the buffer and trigger an end-of-track event.  Returns the
        actual clamped position in seconds so the caller can report it.
        Safe to call from any thread: it writes ``_playback_pos[0]`` and,
        when server audio is playing, moves the mix bus (which takes its
        own lock).
        """
        sr = max(1, self._current_sr)
        # On the mix bus the render starts partway into the file (the part
        # the previous crossfade already played); positions are in the
        # file's timeline, so the earliest reachable point is that offset.
        playing = self._playing_render if self.bus is not None else None
        start = playing.start_offset if playing is not None else 0
        max_samples = max(start, self._playback_len - int(0.1 * sr))
        target_samples = int(max(0.0, seconds) * sr)
        target_samples = max(start, min(target_samples, max_samples))
        self._playback_pos[0] = target_samples
        if self.bus is not None:
            self.bus.seek(target_samples - start)
        return target_samples / sr

    def seek_relative(self, delta_seconds: float) -> float:
        """Seek by ``delta_seconds`` from the current playback position.

        Convenience wrapper around :meth:`seek_to`.  Returns the new
        absolute position in seconds.
        """
        sr = max(1, self._current_sr)
        current = self._playback_pos[0] / sr
        return self.seek_to(current + delta_seconds)

    def _maybe_beatmatch(
        self,
        audio_b: np.ndarray,
        current: IndexEntry,
        next_entry: IndexEntry,
    ) -> np.ndarray:
        """Time-stretch audio_b to match the outgoing BPM (if configured).

        Leaves ``self._beatmatch_ratio`` alone: this runs while rendering
        ahead, and that attribute describes the *playing* track (see
        :meth:`_on_track_start`).
        """
        cfg_dj = self._cfg.djmix
        if not (cfg_dj.beatmatch and current.bpm > 0 and next_entry.bpm > 0):
            return audio_b
        audio_b, _ratio = beatmatch_incoming(
            audio_b,
            bpm_a=current.bpm,
            bpm_b=next_entry.bpm,
            max_stretch=cfg_dj.beatmatch_max_stretch,
        )
        return audio_b

    def _skip_incoming_silence_samples(self, audio_b: np.ndarray, sr_a: int) -> int:
        """Return how many of *audio_b*'s leading samples to drop as silence.

        ``djmix.outro_intro_align`` and the marker-aware transition modes
        (``full_intro_outro`` / ``fixed_skip_silence``) start the incoming
        track at its first sound, as Mixxx does.  The intro itself plays
        under the outgoing outro, so a verse sung over it stays whole.
        Measured on *audio_b* itself, so the offset stays right after a
        beatmatch stretch.
        """
        from autodj.dj_meta import detect_intro_start

        mode = self._cfg.playback.transition_mode
        marker_skip = mode in ("full_intro_outro", "fixed_skip_silence")
        if not (self._cfg.djmix.outro_intro_align or marker_skip):
            return 0
        first_sound = int(detect_intro_start(audio_b, sr_a) * sr_a)
        return min(first_sound, len(audio_b) // 2)

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
        # The same values as the browser's _MIN_FX_DURATION_S in
        # static/modules/audio-engine.js, kept in step by hand.
        "vinyl_rewind": 3.5,  # slow musical reverse + pitch drop
        "transformer": 2.5,  # syncopated 16-cps fader cuts
        "dub_siren": 3.0,  # smooth sine riser w/ vibrato
        "stutter_build": 3.0,  # accelerating gate 4 → 32 Hz
        "wow_flutter": 2.5,  # pitch wobble + amplitude tremolo
        "phaser": 3.0,  # 4-stage allpass cascade
        "ring_modulator": 2.5,  # signal x 173 Hz sine carrier
        "dub_delay": 4.0,  # long lowpass-feedback delay
        "halftime": 3.0,  # tempo halve, pitch preserved
    }

    def _apply_transition_effect(
        self,
        audio_a_trimmed: np.ndarray,
        audio_b: np.ndarray,
        b_head: np.ndarray,
        sr_a: int,
        crossfade_samples: int,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
        """Apply the configured transition effect.

        Returns the effect's name instead of storing it, because this runs
        while rendering ahead and ``self._last_transition_fx`` describes
        the playing track.

        Returns:
            ``(a_trimmed, b_head, extra_layer, effect_name)``.
        """
        from autodj.transitions import TransitionFx, apply_transition, pick_effect

        extra_shape = (0,) if audio_a_trimmed.ndim == 1 else (0, 2)
        extra_layer = np.zeros(extra_shape, dtype=np.float32)
        fx_name = self._cfg.transitions.effect
        try:
            fx_mode = TransitionFx(fx_name)
        except ValueError:
            fx_mode = TransitionFx.NONE
        chosen_fx = pick_effect(fx_mode)
        if chosen_fx == TransitionFx.NONE:
            return audio_a_trimmed, b_head, extra_layer, chosen_fx.value

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
            seed=int(self._rng.integers(2**31)),
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
        return audio_a_trimmed, b_head, extra_layer, chosen_fx.value

    def _mix_overlap(  # pragma: no cover -- crossfade engine, exercised by integration runs
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

    def _render_track(
        self,
        current: IndexEntry,
        next_entry: IndexEntry | None,
        start_offset: int,
    ) -> RenderedTrack | None:
        """Render *current* from *start_offset* into the head of *next_entry*.

        The overlap with *next_entry* is mixed in, and the returned
        ``next_start_offset`` tells the next call where the incoming track's
        audio continues, so no sample plays twice.

        Args:
            current: Track to render.
            next_entry: Track mixed into the tail, or ``None`` for no overlap.
            start_offset: Samples of *current* already played by the previous
                overlap (including any skipped leading silence).

        Returns:
            The rendered track, or ``None`` when *current* cannot be loaded,
            is longer than ``server_max_track_minutes`` (the whole track is
            held in memory while it plays), or has no audio left after
            *start_offset*.
        """
        try:
            audio_a_full = load_stereo(str(current.path), SAMPLE_RATE, self._max_render_seconds())
        except TrackTooLongError as exc:
            logger.warning(
                "Skipping %s: it is %.0f minutes long, and [playback] "
                "server_max_track_minutes is %g.",
                current.path,
                exc.seconds / 60.0,
                self._cfg.playback.server_max_track_minutes,
            )
            return None
        except (OSError, ValueError, RuntimeError) as exc:
            logger.error("Cannot load %s: %s — skipping.", current.path, exc)
            return None
        audio_a_full = self._apply_replaygain(audio_a_full, current.path)
        if start_offset >= len(audio_a_full):
            return None
        # No lyrics here: this runs ahead of time, and the lyrics shown must
        # be the playing track's (_on_track_start loads them).
        self._ensure_dj_cache()
        meta_a = self._outgoing_meta(audio_a_full, SAMPLE_RATE, current.path)
        audio_a = audio_a_full[start_offset:]
        if next_entry is None:
            return RenderedTrack(current, audio_a, None, 0, "", start_offset=start_offset)
        meta_b = self._peek_incoming_meta(next_entry)
        eff_s = self._effective_crossfade_seconds(meta_a, meta_b, current.length)
        crossfade = int(eff_s * SAMPLE_RATE)
        if crossfade >= len(audio_a):
            return RenderedTrack(current, audio_a, next_entry, 0, "", start_offset=start_offset)
        # meta_a's outro/beat markers are absolute positions in the FULL
        # track, so the crossfade start must be located there too -- not in
        # audio_a, which already had start_offset samples cut off the
        # front.  Re-express the result in audio_a's own coordinates after.
        a_start_full = self._crossfade_start_in_a(audio_a_full, SAMPLE_RATE, meta_a, crossfade)
        a_start = max(0, a_start_full - start_offset)
        audio_b_loaded = self._load_incoming(next_entry, SAMPLE_RATE, crossfade)
        if audio_b_loaded is None:
            # Too long to mix in: play this track out; the next render skips it.
            return RenderedTrack(current, audio_a, next_entry, 0, "", start_offset=start_offset)
        pre_stretch_len = len(audio_b_loaded)
        audio_b = self._maybe_beatmatch(audio_b_loaded, current, next_entry)
        post_stretch_len = len(audio_b)
        # Measure the actual stretch from the buffer lengths rather than
        # trusting the reported ratio -- beatmatch_incoming can report a
        # non-1.0 ratio even when it left the audio untouched (stretch below
        # its no-op threshold, or a failed time-stretch falling back to the
        # original).  Sample counts below are in audio_b's (possibly
        # stretched) timeline; next_entry is loaded fresh next call, so they
        # must be converted back to its own, unstretched sample count.
        measured_ratio = post_stretch_len / pre_stretch_len if pre_stretch_len else 1.0

        def _to_unstretched(n: int) -> int:
            if post_stretch_len == 0 or pre_stretch_len == post_stretch_len:
                return n
            return int(n * pre_stretch_len / post_stretch_len)

        intro = self._skip_incoming_silence_samples(audio_b, SAMPLE_RATE)
        audio_b = audio_b[intro:]
        if a_start + crossfade > len(audio_a):
            crossfade = max(0, len(audio_a) - a_start)
        a_trimmed = audio_a[: a_start + crossfade]
        if crossfade == 0 or len(audio_b) < crossfade:
            return RenderedTrack(
                current,
                a_trimmed,
                next_entry,
                _to_unstretched(intro),
                "",
                measured_ratio,
                start_offset=start_offset,
            )
        b_head = audio_b[:crossfade]
        a_trimmed = self._apply_outgoing_filter_sweep(a_trimmed, SAMPLE_RATE, crossfade)
        a_trimmed, b_head, extra, fx_name = self._apply_transition_effect(
            a_trimmed, audio_b, b_head, SAMPLE_RATE, crossfade
        )
        mixed = self._mix_overlap(a_trimmed, b_head, crossfade, SAMPLE_RATE, extra)
        return RenderedTrack(
            current,
            to_stereo(mixed),
            next_entry,
            _to_unstretched(intro + crossfade),
            fx_name,
            measured_ratio,
            start_offset=start_offset,
        )
