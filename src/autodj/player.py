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
from rich.console import Console
from rich.live import Live
from rich.panel import Panel

from autodj.indexer import IndexEntry

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
    volume: float = 1.0       # 0.0 (silent) – 1.0 (full)
    is_muted: bool = False

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
        dry_run: If ``True``, print track selections without playing audio.
    """

    def __init__(
        self,
        cfg,
        sim_index,
        dry_run: bool = False,
    ) -> None:
        """Initialise the player with configuration and index.

        No model is needed at play time — next-track vectors are looked up
        directly from the pre-built FAISS index.

        Args:
            cfg: Full :class:`~autodj.config.AutoDJConfig` instance.
            sim_index: Loaded :class:`~autodj.similarity.SimilarityIndex`.
            dry_run: If ``True``, print track selections without playing audio.
        """
        self._cfg = cfg
        self._sim = sim_index
        self._dry_run = dry_run
        self._state = PlayerState(
            no_repeat_window=cfg.playback.no_repeat_window
        )
        self._crossfade_samples: dict[int, int] = {}  # populated per-track based on SR
        self._skip_event = threading.Event()
        self._lock = threading.Lock()
        # Shared playback position (samples) — written by callback, read/written by
        # keyboard seek handler.  Using a list so both sides share the same object.
        self._playback_pos: list[int] = [0]
        self._playback_len: int = 0   # length of the current audio array in samples
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

        # --- Line 2: next track + volume bar ---
        vol_pct = int(round(self._state.volume * 100))
        filled = int(round(self._state.volume * 10))
        bar = "█" * filled + "░" * (10 - filled)
        vol = (
            "[red]MUTED[/red]"
            if self._state.is_muted
            else f"[cyan]{bar} {vol_pct}%[/cyan]"
        )
        nxt = f"[dim]Next:[/dim] {next_t.display_name}  " if next_t else ""
        next_line = f"{nxt}[dim]Vol:[/dim] {vol}"

        # --- Line 3: controls hint ---
        controls = (
            "[dim]Space=Pause  N=Skip  Q=Quit"
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

        current = seed_entry
        self._state.current_track = current
        self._state.record_played(current)

        with Live(
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

                    if not self._dry_run:
                        self._play_with_crossfade(current, next_entry)
                    else:
                        _CONSOLE.print(f"  [dim]dry-run:[/dim] {current.display_name}")
                        time.sleep(0.1)

                    if self._state.should_stop:
                        break

                    self._state.record_played(next_entry)
                    current = next_entry
            finally:
                self._live = None

    def _pick_next(self, current: IndexEntry) -> IndexEntry:
        """Select the next track by looking up the current track's stored vector.

        Retrieves the pre-computed embedding from the FAISS index by path —
        no model inference required.

        If the no-repeat window covers the entire index (e.g. a small test
        index), falls back to only excluding the current track so playback
        can continue indefinitely.

        Args:
            current: The track currently playing.

        Returns:
            The recommended next :class:`~autodj.indexer.IndexEntry`.
        """
        from autodj.similarity import SimilarityError

        try:
            return self._sim.find_next_for_path(
                current_path=current.path,
                recently_played=self._state.recently_played,
                n_candidates=10,
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
                current_path=current.path,
                recently_played=deque([current.path]),
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
        Volume, mute, and seek are applied in real time via shared state.

        Args:
            audio: Mono float32 audio array.
            sr: Sample rate of *audio* in Hz.
        """
        self._current_sr = sr
        self._playback_len = len(audio)
        pos = self._playback_pos
        pos[0] = 0

        finished = threading.Event()

        def callback(outdata, frames, time_info, status):
            if self._state.is_paused or self._skip_event.is_set() or self._state.should_stop:
                outdata[:] = 0
                if self._skip_event.is_set() or self._state.should_stop:
                    raise sd.CallbackStop()
                return

            chunk = audio[pos[0] : pos[0] + frames]
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

        try:
            with sd.OutputStream(
                samplerate=sr,
                channels=1,
                dtype="float32",
                callback=callback,
                finished_callback=finished.set,
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

            def on_press(key):
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

            listener = keyboard.Listener(on_press=on_press)
            listener.daemon = True
            listener.start()
        except Exception as exc:
            logger.warning("Keyboard controls unavailable: %s", exc)

