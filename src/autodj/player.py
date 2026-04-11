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

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import sounddevice as sd
import soundfile as sf

from autodj.indexer import IndexEntry

logger = logging.getLogger(__name__)

# Default output sample rate; sounddevice converts if the device differs.
_DEFAULT_SR = 44_100


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
    """

    current_track: Optional[IndexEntry] = None
    next_track: Optional[IndexEntry] = None
    is_paused: bool = False
    should_stop: bool = False
    no_repeat_window: int = 50
    recently_played: deque = field(default_factory=deque)

    def __post_init__(self) -> None:
        """Initialise the bounded recently-played deque."""
        self.recently_played = deque(maxlen=self.no_repeat_window)

    def record_played(self, entry: IndexEntry) -> None:
        """Record a track as recently played.

        Automatically evicts the oldest entry when the deque is full.

        Args:
            entry: The track that just started playing.
        """
        self.recently_played.append(entry.path)


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
        return audio, sr


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
        wrapper: Loaded MERT model wrapper for embedding tracks.
        dry_run: If ``True``, print track selections without playing audio.
    """

    def __init__(
        self,
        cfg,
        sim_index,
        wrapper,
        dry_run: bool = False,
    ) -> None:
        """Initialise the player with configuration, index, and model.

        Args:
            cfg: Full :class:`~autodj.config.AutoDJConfig` instance.
            sim_index: Loaded :class:`~autodj.similarity.SimilarityIndex`.
            wrapper: Loaded :class:`~autodj.model.MertWrapper` for embedding.
            dry_run: If ``True``, print track selections without playing audio.
        """
        from autodj.config import AutoDJConfig
        from autodj.similarity import SimilarityIndex
        from autodj.model import MertWrapper

        self._cfg = cfg
        self._sim = sim_index
        self._wrapper = wrapper
        self._dry_run = dry_run
        self._state = PlayerState(
            no_repeat_window=cfg.playback.no_repeat_window
        )
        self._crossfade_samples: dict[int, int] = {}  # populated per-track based on SR
        self._skip_event = threading.Event()
        self._lock = threading.Lock()

    def run(self, seed_entry: Optional[IndexEntry]) -> None:
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
            seed_entry = random.choice(self._sim.entries)

        self._setup_keyboard()
        self._print_controls()

        current = seed_entry
        self._state.record_played(current)

        while not self._state.should_stop:
            self._state.current_track = current
            self._skip_event.clear()

            # Embed current track and pre-select the next one
            next_entry = self._pick_next(current)
            self._state.next_track = next_entry
            self._display_now_playing(current, next_entry)

            if not self._dry_run:
                self._play_with_crossfade(current, next_entry)
            else:
                print(f"[dry-run] Would play: {current.display_name}")
                time.sleep(0.1)

            if self._state.should_stop:
                break

            self._state.record_played(next_entry)
            current = next_entry

    def _pick_next(self, current: IndexEntry) -> IndexEntry:
        """Select the next track via the similarity index.

        Args:
            current: The track currently playing.

        Returns:
            The recommended next :class:`~autodj.indexer.IndexEntry`.
        """
        try:
            audio, sr = load_audio(str(current.path))
            query_vec = self._wrapper.embed_array(audio, sample_rate=sr)
        except Exception as exc:
            logger.warning("Could not embed current track (%s): %s", current.path, exc)
            query_vec = np.random.randn(768).astype(np.float32)
            # Pad or trim to FEATURE_DIM
            from autodj.indexer import FEATURE_DIM
            full_vec = np.zeros(FEATURE_DIM, dtype=np.float32)
            full_vec[:768] = query_vec
            query_vec = full_vec / np.linalg.norm(full_vec)

        return self._sim.find_next(
            query_vector=query_vec,
            recently_played=self._state.recently_played,
            n_candidates=10,
        )

    def _play_with_crossfade(
        self,
        current: IndexEntry,
        next_entry: IndexEntry,
    ) -> None:
        """Load, crossfade, and play *current* into *next_entry*.

        Blocks until the track finishes (or a skip is requested).

        Args:
            current: The outgoing track.
            next_entry: The incoming track (used only for the crossfade tail).
        """
        crossfade_sec = self._cfg.playback.crossfade_seconds

        try:
            audio_a, sr_a = load_audio(str(current.path))
        except Exception as exc:
            logger.error("Cannot load %s: %s — skipping.", current.path, exc)
            return

        crossfade_samples = int(crossfade_sec * sr_a)

        # Pre-load the next track for the crossfade tail
        try:
            audio_b, sr_b = load_audio(str(next_entry.path))
            if sr_b != sr_a:
                import librosa
                audio_b = librosa.resample(audio_b, orig_sr=sr_b, target_sr=sr_a)
        except Exception as exc:
            logger.warning("Cannot pre-load next track (%s): %s", next_entry.path, exc)
            audio_b = np.zeros(crossfade_samples, dtype=np.float32)

        if crossfade_samples > 0 and len(audio_b) >= crossfade_samples:
            mixed = _apply_crossfade(audio_a, audio_b[:crossfade_samples + len(audio_a)], crossfade_samples)
        else:
            mixed = audio_a

        self._stream_audio(mixed, sr_a)

    def _stream_audio(self, audio: np.ndarray, sr: int) -> None:
        """Stream a mono float32 audio array through sounddevice.

        Blocks until playback finishes, is skipped, or stopped.

        Args:
            audio: Mono float32 audio array.
            sr: Sample rate of *audio* in Hz.
        """
        finished = threading.Event()

        def callback(outdata, frames, time_info, status):
            nonlocal offset
            if self._state.is_paused or self._skip_event.is_set() or self._state.should_stop:
                outdata[:] = 0
                if self._skip_event.is_set() or self._state.should_stop:
                    raise sd.CallbackStop()
                return
            chunk = audio[offset : offset + frames]
            if len(chunk) < frames:
                outdata[: len(chunk), 0] = chunk
                outdata[len(chunk) :] = 0
                raise sd.CallbackStop()
            outdata[:, 0] = chunk
            offset += frames

        offset = 0

        try:
            with sd.OutputStream(
                samplerate=sr,
                channels=1,
                dtype="float32",
                callback=callback,
                finished_callback=finished.set,
            ):
                while not finished.is_set():
                    # Spin-wait with short sleep, handling pause
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

            def on_press(key):
                try:
                    char = key.char.lower() if hasattr(key, "char") and key.char else None
                except Exception:
                    char = None

                if key == keyboard.Key.space:
                    self._state.is_paused = not self._state.is_paused
                    status = "Paused" if self._state.is_paused else "Resumed"
                    print(f"\n[{status}] Press Space to toggle.")
                elif char == "n":
                    print("\n[Skip] Moving to next track...")
                    self._skip_event.set()
                elif char == "q":
                    print("\n[Quit] Stopping AutoDJ...")
                    self._state.should_stop = True
                    self._skip_event.set()

            listener = keyboard.Listener(on_press=on_press)
            listener.daemon = True
            listener.start()
        except Exception as exc:
            logger.warning("Keyboard controls unavailable: %s", exc)

    def _display_now_playing(
        self, current: IndexEntry, next_entry: Optional[IndexEntry]
    ) -> None:
        """Print the now-playing status to the terminal.

        Args:
            current: The track currently starting playback.
            next_entry: The track queued to play next (may be ``None``).
        """
        print(f"\n{'─' * 60}")
        print(f"  Now playing : {current.display_name}")
        if current.bpm:
            print(f"  BPM         : {current.bpm:.0f}")
        if next_entry:
            print(f"  Up next     : {next_entry.display_name}")
        print(f"{'─' * 60}")

    def _print_controls(self) -> None:
        """Print keyboard control hints to the terminal."""
        print("\nControls: Space=Pause/Resume  N=Skip  Q=Quit\n")
