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

import contextlib
import logging
import math
import os
import random
import re
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from concurrent.futures import Future, wait
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from itertools import chain
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

from autodj.beatmatch import (
    HOP,
    NO_STRETCH,
    ON_BEAT_S,
    Glide,
    downbeats,
    drop_landing,
    local_period,
    phase_locked_start,
    tempo_ratio,
)
from autodj.breaks import Break, BreakError, SlotPlan, new_id, placement_for, speech_cues
from autodj.dmca import WINDOW_SECONDS, Complement, complement, relaxed
from autodj.identity_map import attached
from autodj.indexer import IndexEntry
from autodj.mixbus import BusEvents, MixBus, RenderedTrack, SkipTail
from autodj.music_speed import resample_music
from autodj.processing import TrackWindows
from autodj.render_ahead import RenderAhead
from autodj.repeat_rules import (
    RELAXED_RULES,
    ArtistSplitter,
    PlayRecord,
    RelatedArtists,
    RelatedTracks,
    RepeatFilter,
    RepeatRules,
    SongVersions,
    TrackKeyCache,
    artist_key,
    build_filter,
    plays_to_load,
    prune_history,
    records_from_plays,
)
from autodj.station_log import station_thread
from autodj.stereo import (
    LONG_TRACK_SECONDS,
    SAMPLE_RATE,
    envelope,
    join_audio,
    load_mono,
    load_stereo,
    mono,
    to_stereo,
)

if TYPE_CHECKING:
    from datetime import datetime

    from autodj.audio_meta import LyricLine
    from autodj.config import AutoDJConfig
    from autodj.dj_meta import Cue, DjMeta
    from autodj.similarity import SimilarityIndex
    from autodj.transitions import BeatSync, TransitionFx

logger = logging.getLogger(__name__)
repeat_logger = logging.getLogger(f"{__name__}.repeat")

#: Held while a station's song-picking settings or clockwheel are swapped
#: in, and while a rotation test copies them (autodj.rotation_sim), so the
#: copy never sees a change half made.  Only in-memory swaps happen under
#: it: files and the catalog are read before it is taken.  A dry-run player
#: has a lock of its own (see :func:`settings_lock`).
SETTINGS_LOCK = threading.RLock()


def settings_lock(player: Any) -> Any:
    """The lock *player*'s settings changes hold: its own, or :data:`SETTINGS_LOCK`."""
    lock = getattr(player, "settings_lock", None)
    return lock if lock is not None else SETTINGS_LOCK


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
        return join_audio([audio_a, audio_b])

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

    return join_audio([a_body, overlap, b_body])


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
        return join_audio([audio_a, audio_b])
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
    return join_audio([a_body, overlap, b_body])


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


def effective_no_repeat_window(configured: int, library_size: int, *, player: Any) -> int:
    """Return the no-repeat window the picker can honour for *library_size*.

    A library of 200 tracks with a window of 500 would refuse to repeat any
    track once every track had played, then fall through the relaxation
    paths and either fail or return the same neighbour on every advance.
    At least 4 unplayed candidates (or a tenth of the library) are kept.
    """
    if library_size <= 0 or configured < library_size:
        return configured
    effective = max(1, library_size - max(4, library_size // 10))
    warning = (configured, library_size, effective)
    if getattr(player, "_last_repeat_cap_warning", None) != warning:
        logger.warning(
            "playback.repeat_rules.track.tracks=%d but library has only %d tracks; "
            "using %d so the picker always has candidates.",
            *warning,
        )
        player._last_repeat_cap_warning = warning
    return effective


def apply_repeat_windows(player: Any) -> None:
    """Give *player* the repeat rules its configuration now asks for.

    Used when the web UI or restored web state changes
    ``playback.repeat_rules`` on
    a running player, and when the library size changes.  Cheap when
    nothing changed: :meth:`PlayerState.set_repeat_rules` keeps equal
    rules as they are.
    """
    pb = player._cfg.playback
    sim = player._sim
    library = getattr(player, "_library", None)
    library_size = (
        len(library.entries_snapshot())
        if library is not None
        else int(sim.ntotal)
        if sim is not None
        else 0
    )
    player._state.set_repeat_rules(
        RepeatRules.from_playback(
            pb,
            track_window=effective_no_repeat_window(
                pb.repeat_rules.track.tracks, library_size, player=player
            ),
            related=getattr(player, "_related_artists", None),
            related_tracks=getattr(player, "_related_tracks", None),
        )
    )


#: "outside its hours", "outside its days" or "outside its dates" in a reason.
_OUTSIDE_ITS = re.compile(r"outside its (hours|days|dates)\b")


def _closure_words(context: _PickContext) -> str:
    """What holds the closed categories back, in words, such as "hours" or "days or dates".

    Read from the reasons the category index gives (``"outside its dates: ..."``),
    in the order days, hours, dates, joined with commas and a final "or";
    "hours" when no reason says.
    """
    found = sorted(
        {
            match.group(1)
            for reason in context.closed_categories.values()
            if (match := _OUTSIDE_ITS.search(reason))
        },
        key=("days", "hours", "dates").index,
    )
    if not found:
        return "hours"
    return found[0] if len(found) == 1 else f"{', '.join(found[:-1])} or {found[-1]}"


def _music_output_frame(source_frame: int, rate: float) -> int:
    """Map a source-file frame to its position in a rate-adjusted buffer."""
    return round(source_frame / rate)


def _music_source_frame(output_frame: int, rate: float) -> int:
    """Map a rate-adjusted output frame back to source-file coordinates."""
    return round(output_frame * rate)


def _music_segment(audio: np.ndarray, rate: float, output_frames: int) -> np.ndarray:
    """Rate-adjust a music segment to an exact wall-clock frame count."""
    result = resample_music(audio, rate)
    if len(result) > output_frames:
        return result[:output_frames]
    if len(result) < output_frames:
        return np.pad(result, ((0, output_frames - len(result)), (0, 0)))
    return result


def _music_output_points(points: dict[str, Any], rate: float) -> dict[str, Any]:
    """Copy source-time music cues into a rate-adjusted output timeline."""
    result = dict(points)
    for key in ("start", "end", "intro", "segue", "outro"):
        value = result.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            result[key] = float(value) / rate
    return result


def _log_repeat_blocks(repeat: RepeatFilter, picked: IndexEntry) -> None:
    """Log which repeat rules held candidates back before *picked* was chosen."""
    for path, reason in repeat.held_back.items():
        repeat_logger.debug(
            "Candidate excluded from pool by repeat rule (rules may be relaxed): "
            "candidate=%s reason=%s selected=%s",
            path,
            reason,
            picked.path,
        )
    summary = repeat.summary()
    if summary:
        logger.info(
            "Repeat rules held back %d candidates (%s) before picking %s",
            len(repeat.held_back),
            summary,
            picked.display_name,
        )


def _join_recent(saved: list[str], recent: list[str]) -> list[str]:
    """The track window's paths after putting *saved* plays ahead of *recent*.

    Both are oldest first.  *recent* (the window in memory) always stays
    whole at the end, so loading saved plays never shortens it.  Saved
    plays it already holds, as a run inside it or as the run it starts
    with, are not added twice.  Only positions whose first path matches
    are compared, so a long window costs about one pass.
    """
    if not recent:
        return list(saved)
    if not saved:
        return list(recent)
    size = len(saved)
    first = saved[0]
    for index in range(len(recent) - size + 1):
        if recent[index] == first and recent[index : index + size] == saved:
            return list(recent)
    start_path = recent[0]
    for start, path in enumerate(saved):
        # The earliest match is the longest overlap.
        if path == start_path and saved[start:] == recent[: size - start]:
            return [*saved[:start], *recent]
    return [*saved, *recent]


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
        artist_repeat_window: Picks before an artist may repeat.
        repeat_rules: Every repeat rule. Left out, it uses the track and
            artist windows, with album and title disabled. When given, it
            sets the two windows.
        play_history: The track starts the artist, album and title rules
            and the minute windows look at, oldest first (see
            :class:`~autodj.repeat_rules.PlayRecord`).
        clock: Where the minute windows read the time: wall-clock
            seconds (``time.time``), so plays saved before a restart
            compare with new ones.  Tests pass a fake.
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
    no_repeat_window: int = 50
    artist_repeat_window: int = 8
    repeat_rules: RepeatRules | None = None
    recently_played: deque = field(default_factory=deque)
    play_history: deque[PlayRecord] = field(default_factory=deque)
    dmca_enabled: bool = False
    clock: Callable[[], float] = field(default=time.time, repr=False, compare=False)
    volume: float = 1.0  # 0.0 (silent) – 1.0 (full)
    is_muted: bool = False
    track_number: int = 0
    discovery_enabled: bool = False
    queue_lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)
    history_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Initialise the repeat rules and the bounded recently-played deque."""
        if self.repeat_rules is None:
            self.repeat_rules = RepeatRules(
                track_window=self.no_repeat_window,
                artist_window=self.artist_repeat_window,
                album_window=0,
                title_window=0,
            )
        self.no_repeat_window = self.repeat_rules.track_window
        self.artist_repeat_window = self.repeat_rules.artist_window
        self.recently_played = deque(maxlen=self.no_repeat_window)
        self.play_history = deque()

    @property
    def rules(self) -> RepeatRules:
        """The repeat rules (always set once the state is built)."""
        return cast(RepeatRules, self.repeat_rules)

    @property
    def dmca_seconds(self) -> float:
        """Seconds of plays the history keeps for DMCA mode (0 when it is off)."""
        return WINDOW_SECONDS if self.dmca_enabled else 0.0

    def set_repeat_rules(self, rules: RepeatRules) -> None:
        """Change the repeat rules, keeping the most recent history.

        A shorter window keeps the newest entries; a longer one keeps all
        of them and simply remembers more from now on.  The history is
        replaced under ``history_lock``, the lock every record and copy
        holds, so a track start is never recorded into a deque that is
        being thrown away, and a copy never iterates one being resized.
        Rules equal to the current ones change nothing.
        """
        with self.history_lock:
            if rules == self.repeat_rules:
                return
            self.repeat_rules = rules
            self.no_repeat_window = rules.track_window
            self.artist_repeat_window = rules.artist_window
            self.recently_played = deque(self.recently_played, maxlen=rules.track_window)
            self.play_history = deque(self.play_history)
            prune_history(self.play_history, rules, self.clock(), self.dmca_seconds)

    def restore_history(self, records: list[PlayRecord]) -> None:
        """Put plays from before a restart (oldest first) ahead of the history.

        Fills the track window's paths and the artist, album and title
        history, so the rules hold after a restart as they did before.
        Run again after a settings save, it never shortens the track
        window: the paths in memory stay, and saved plays only go ahead of
        them (see :func:`_join_recent`).  Takes ``history_lock``.
        """
        with self.history_lock:
            maxlen = self.recently_played.maxlen
            if self.play_history:
                first = self.play_history[0]
                records = [record for record in records if record.started < first.started]
                # The catalog and playback loop use separate timestamps for one start.
                if (
                    records
                    and records[-1].path == first.path
                    and first.started - records[-1].started < 5.0
                ):
                    records.pop()
            paths = _join_recent([record.path for record in records], list(self.recently_played))
            self.play_history = deque([*records, *self.play_history])
            self.recently_played = deque(paths, maxlen=maxlen)
            prune_history(self.play_history, self.rules, self.clock(), self.dmca_seconds)

    def record_played(self, entry: IndexEntry) -> None:
        """Record a track as recently played.

        Keeps the file path for the track window, and the artist, album,
        title and start time for the other rules, so the picker can avoid
        back-to-back same-artist sequences, two songs from the same album
        in a row, and re-runs of the same song in another version.

        Hold ``history_lock``.

        Args:
            entry: The track that just started playing.
        """
        now = self.clock()
        self.recently_played.append(entry.path)
        self.play_history.append(PlayRecord.of(entry, now))
        prune_history(self.play_history, self.rules, now, self.dmca_seconds)


@dataclass(frozen=True)
class _PickContext:
    """The shared state one pick reads, copied up front.

    The similarity search takes a while, and the mix bus records track
    starts meanwhile.  Copying the history once, under ``_pick_lock``,
    means the search never holds that lock and never sees the history
    change half way through.

    Attributes:
        recent: Paths the pick must not repeat: the track window, plus
            the tracks its minute window still holds back.
        repeat: The artist, album and title rules; call it with a
            candidate.  It counts what it holds back, for the log.
        track_number: ``PlayerState.track_number`` when the copy was made.
        peek_queue: Peek at the user queue instead of popping it (render
            ahead: a queue pick leaves the queue when it starts).
        queue_reserved: A pending track peeked from the queue that has not
            started yet; its queue slot is skipped.
        protected: Paths no pick may return even after relaxing the repeat
            window: the track the pick follows, the track the bus is
            starting, the latest recorded track and the playing one.  Only
            a clock with nothing else left falls back to them (all but
            *follows*), so the music never stops.
        complement: In DMCA mode, what the last three hours of plays allow
            next (see :mod:`autodj.dmca`); ``None`` when the mode is off.
        follows: The path of the track the pick follows (the one playing
            at the handoff), which no pick returns.
        closed: Paths of the songs in a category outside its allowed days
            and hours now (dayparting).  No relaxation picks them; only
            when nothing else at all can play does one play, with a warning.
        closed_categories: The ID of each such category and the reason, in
            words, for the clockwheel to skip its steps.
        at: When the picked song is expected to start, in wall-clock
            seconds, or ``None`` for now; clockwheel steps with days and
            hours are checked then, and the liner steps judge the liners'
            play dates by that day.
    """

    recent: deque
    repeat: RepeatFilter
    track_number: int
    peek_queue: bool = False
    queue_reserved: IndexEntry | None = None
    protected: frozenset[str] = frozenset()
    complement: Complement | None = None
    follows: str | None = None
    closed: frozenset[str] = frozenset()
    closed_categories: Mapping[str, str] = field(default_factory=dict)
    at: float | None = None

    def allows(self, entry: IndexEntry) -> bool:
        """True when *entry* keeps the DMCA complement (always, with the mode off)."""
        return self.complement is None or self.complement.allows(entry)


@dataclass(frozen=True)
class _BeatmatchPlan:
    """How the incoming track is beatmatched into one crossfade.

    Attributes:
        ratio: Stretch of the incoming track during the overlap (output
            over input duration), 1.0 for none.
        fade_start: Sample of the outgoing track where the fade starts.
        ramp_frames: Length of the glide back to the native tempo, in
            vocoder frames.
        beats: The incoming track's beats as samples, when its grid is
            trusted (transients are kept on them); empty otherwise.
        semitones: Key shift of the incoming track during the overlap
            (``[djmix] key_shift``), 0 for none.
    """

    ratio: float
    fade_start: int
    ramp_frames: int = 0
    beats: tuple[int, ...] = ()
    semitones: int = 0


@dataclass(frozen=True)
class _DropMix:
    """A drop-to-drop mix (``[djmix] drop_mix``): the overlap ends on the incoming drop.

    Attributes:
        entry: Sample of the incoming track where it enters.
        overlap: Overlap length in samples.
        match: The beatmatch plan; its fade start puts the incoming drop
            on the outgoing landing (:func:`~autodj.beatmatch.drop_landing`).
    """

    entry: int
    overlap: int
    match: _BeatmatchPlan


@dataclass(frozen=True)
class _FxPlan:
    """The transition effect chosen for one crossfade, and what it syncs to.

    Attributes:
        name: The concrete effect (a :class:`~autodj.transitions.TransitionFx`
            value, never a meta-mode).
        sync: Tempo and keys for the synced effects, or ``None``.
        downbeats: The outgoing track's downbeats, in its own timeline.
        origin_s: Where in that timeline the rendered audio starts.
    """

    name: str
    sync: BeatSync | None = None
    downbeats: tuple[float, ...] = ()
    origin_s: float = 0.0


def _effect_mode(name: object) -> TransitionFx:
    """The :class:`~autodj.transitions.TransitionFx` called *name*, NONE when unknown."""
    from autodj.transitions import TransitionFx

    try:
        return TransitionFx(str(name).lower())
    except ValueError:
        return TransitionFx.NONE


def _mixed_in_by(rendered: RenderedTrack | None) -> tuple[str, float]:
    """The effect and stretch that *rendered*'s next track comes in with.

    ``("", 1.0)`` when *rendered* has no overlap with it: the next track
    then starts on its own.
    """
    if rendered is None or not rendered.transition_fx:
        return ("", 1.0)
    return (rendered.transition_fx, rendered.beatmatch_ratio)


def _grid_anchor(meta: DjMeta) -> float | None:
    """A time *meta*'s track has a downbeat on, set in DJ software, or ``None``.

    An imported first-downbeat (main / load) cue counts first, then an
    imported intro start; auto-detected cues never do.
    """
    from autodj.dj_meta import imported_markers

    for cue in meta.cues:
        if cue.type == "first_downbeat" and cue.source != "auto":
            return cue.time_s
    return imported_markers(meta.cues).get("intro_start")


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
    # The shared music library, and this station's plays and settings in it
    # (a StationLedger).  Without a ledger of its own, the player records
    # into the library's default-station ledger.
    _library: Any = None
    _station_ledger: Any = None
    # Where this station keeps its state (a StationPaths); the default
    # station's folders when not set.
    station_paths: Any = None
    # A queue pick the bus has taken (which removed it from the queue) and
    # not yet started: it goes back if its set stops first.  Only the bus
    # thread touches it (_take_render, then _on_track_start).
    _taken_queue_pick: RenderedTrack | None = None
    # How the render cursor's track comes in: the previous render's
    # transition effect and stretch (RenderedTrack.mixed_in_fx / _ratio).
    _pending_mixed_in: tuple[str, float] = ("", 1.0)
    # How the render cursor's track is stretched back to its own tempo
    # after the overlap (RenderedTrack.next_glide), or None.
    _pending_glide: Glide | None = None
    _pending_reserved_liners: tuple[IndexEntry, ...] = ()
    _pending_clock_cursor: int | None = None
    # The clockwheel _pending_clock_cursor is a place in (#133).
    _pending_clock_name: str | None = None
    _pending_clock_boundary: float | None = None
    _clock: dict[str, Any] | None = None
    _clock_next_boundary: float | None = None
    # Hour fitting's hard events (or a callable giving the scheduler's
    # current ones) and the time zone they run in (#126).
    _hour_fit_events: Sequence[Any] | Callable[[], Sequence[Any]] = ()
    _hour_fit_timezone: str | None = None
    # The clockwheel _seed_clock_cursor is a place in (#133).
    _seed_clock_name: str | None = None
    _seed_clock_revision: int | None = None
    _seed_clock_step: dict | None = None
    _seed_clock_following: tuple[IndexEntry, IndexEntry, int, dict | None] | None = None
    liner_scheduler: Any = None
    # This station's intros matched to songs and artists (a MatchedIntros,
    # #127), or None.  Saved-point playback mixes one into a song change.
    matched_intros: Any = None
    # This station's announcer breaks (a BreakDesk, #88), or None; see break_desk.
    _break_desk: Any = None
    # The open song change, planned one song ahead for breaks (#88), and
    # the lock that guards it.  A leaf lock: only the desk's own lock is
    # taken while it is held.
    _slot_plan: SlotPlan | None = None
    _slot_lock: threading.Lock = threading.Lock()
    # Counts every move of the render cursor, so a plan knows it still holds.
    _cursor_serial: int = 0
    # The station's liner play dates: passes over the liners a clockwheel's
    # liner steps may not play today (see autodj.clip_dates.LinerGate).
    liner_filter: Callable[[Any], Any] | None = None
    _skip_next_intro: bool = False
    _continuous_master: Any = None
    # Guards the per-pair effect plans of planned_transition.
    _fx_plan_lock: ClassVar[threading.Lock] = threading.Lock()
    # Plans kept, most recent last: a few pairs cover the playing track,
    # the one rendered ahead and a queue edit's re-render.
    _FX_PLANS_KEPT: ClassVar[int] = 8
    # Browser mode: (path, pick mode) of the last pick made for next_track.
    _next_pick: tuple[str, str] | None = None
    # Creates the sung-span cache of the mix features on first use.
    _vocal_cache_lock: ClassVar[threading.Lock] = threading.Lock()
    # How far ahead of the playing position a skip effect starts, so it is
    # ready before the bus gets there: 0.1 s.
    _SKIP_LOOKAHEAD: ClassVar[int] = SAMPLE_RATE // 10

    @property
    def _preset(self) -> Any:
        return self._settings._preset

    @_preset.setter
    def _preset(self, value: Any) -> None:
        self._settings._preset = value

    @property
    def _bpm_range(self) -> tuple[float, float] | None:
        return self._settings._bpm_range

    @_bpm_range.setter
    def _bpm_range(self, value: tuple[float, float] | None) -> None:
        self._settings._bpm_range = value

    @property
    def _pure_shuffle(self) -> bool:
        return self._settings._pure_shuffle

    @_pure_shuffle.setter
    def _pure_shuffle(self, value: bool) -> None:
        self._settings._pure_shuffle = value

    @property
    def _anchor_to_seed(self) -> bool:
        return self._settings._anchor_to_seed

    @_anchor_to_seed.setter
    def _anchor_to_seed(self, value: bool) -> None:
        self._settings._anchor_to_seed = value

    @property
    def _eq_low(self) -> float:
        return self._settings._eq_low

    @_eq_low.setter
    def _eq_low(self, value: float) -> None:
        self._settings._eq_low = value

    @property
    def _eq_mid(self) -> float:
        return self._settings._eq_mid

    @_eq_mid.setter
    def _eq_mid(self, value: float) -> None:
        self._settings._eq_mid = value

    @property
    def _eq_high(self) -> float:
        return self._settings._eq_high

    @_eq_high.setter
    def _eq_high(self, value: float) -> None:
        self._settings._eq_high = value

    @property
    def _discovery_every(self) -> int | None:
        return self._settings._discovery_every

    @_discovery_every.setter
    def _discovery_every(self, value: int | None) -> None:
        self._settings._discovery_every = value

    # A dry run of the picker (autodj.rotation_sim) gives the player its own
    # seeded random.Random, a clock of its own (see _now) and a pick_observer
    # that hears what each pick passed over.  On air the random module
    # chooses and nothing observes.
    _random: random.Random | None = None
    pick_observer: Callable[[str, Any], None] | None = None
    # The lock settings changes hold (see settings_lock); None is the shared one.
    settings_lock: Any = None

    def __init__(
        self,
        cfg: AutoDJConfig,
        sim_index: SimilarityIndex,
        dry_run: bool = False,
        stream_mode: bool = False,
        server_audio_too: bool = False,
        settings: Any = None,
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
        from autodj.station_settings import StationSettings

        self._settings = settings or StationSettings(cfg, sim=sim_index)
        self._cfg = cfg
        self._sim = sim_index
        self._dry_run = dry_run
        self._stream_mode = stream_mode
        self._server_audio_too = server_audio_too
        # Picker settings.  They start off (discovery at the configured
        # rate) and the web page's settings routes change them.
        # Anchored mode: when True, every similarity query uses the SEED
        # vector rather than the currently-playing track.  Prevents the
        # session from drifting away from where the user started — each
        # next track is similar to the seed, not to the previous track.
        # Off by default; toggled from the web UI.
        # Path of the seed track — set in run() / set externally by the
        # bridge when the user picks a fresh seed.  Used by anchored mode.
        self._seed_path: str | None = None
        # Pure shuffle: pick random next track, completely ignore similarity.
        # When the user disables pure-shuffle mid-set,
        # the next pick uses similarity from the current track — so they can
        # use shuffle to stumble onto a song they like, then "lock in" by
        # toggling shuffle off and let the auto-DJ continue from there.
        # Lyrics for the current track — populated when each track loads,
        # consumed by the web UI via PlayerBridge.get_state().
        self._current_lyrics: list = []
        self._current_lyrics_plain: str = ""
        # 3-band EQ state (1.0 = unity), mutated by the web UI
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
        # How the playing track's opening was stretched while it was mixed
        # in (1.0 = not stretched) — exposed via state
        self._beatmatch_ratio: float = 1.0
        # The transition effect the playing track came in with — exposed
        # via state
        self._last_transition_fx: str = "none"
        # Effect chosen per (outgoing, incoming) path pair, with the
        # [transitions] effect setting it was chosen under.
        self._fx_plans: dict[tuple[str, str], tuple[str, str]] = {}
        # Tracks whose stored library cues were re-merged this session.
        self._cues_refreshed: set[str] = set()
        # Shared RNG for transition effects that use randomness, so a single
        # seed drives both stereo channels identically (see apply_transition).
        self._rng: np.random.Generator = np.random.default_rng()
        # Pick provenance — set by _pick_next describing HOW the current
        # track was selected.  Read by the bridge to build the
        # "why this track" sentence list shown in the web UI.
        self._last_pick_mode: str = "seed"
        self._last_clock_step: dict | None = None
        self._chosen_clock_ad_steps: tuple[dict, ...] = ()
        self._clock_has_ad_steps = False
        self._clock_has_pending_ad_steps = False
        self._clock_revision = 0
        # Edits wait for worker picks/renders, never for audio callbacks.
        self._clock_selection_lock = threading.RLock()
        # Short published clock writes only: selection -> clock -> settings;
        # callbacks take set -> clock, and never take the selection lock.
        self._clock_lock = threading.RLock()
        self._pending_clock_update: tuple[dict[str, Any], list[Any], int] | None = None
        self._pending_clock_name: str | None = None
        self._chosen_clock_revision: int | None = None
        self._pending_clock_cursor_revision: int | None = None
        # Previous track played — kept so the explainer can compute deltas
        # against the current pick.
        self._previous_track: IndexEntry | None = None
        self._last_repeat_cap_warning: tuple[int, int, int] | None = None
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
            cfg.playback.repeat_rules.track.tracks,
            library_size,
            player=self,
        )
        from autodj.related_artist_file import (
            load_configured_related_artists,
            load_configured_related_tracks,
            unused_related_artists_message,
        )

        try:
            self._related_artists = (
                load_configured_related_artists(cfg.index.library_dir)
                if cfg.playback.repeat_rules.use_related_artists
                else RelatedArtists()
            )
            self.related_artists_problem = None
        except (OSError, ValueError) as exc:
            self._related_artists = RelatedArtists()
            self.related_artists_problem = unused_related_artists_message(exc)
            logger.warning("Related artists file was not used: %s", exc)
        try:
            self._related_tracks = (
                load_configured_related_tracks(cfg.index.library_dir)
                if cfg.playback.repeat_rules.use_related_tracks
                else RelatedTracks()
            )
            self.related_tracks_problem = None
        except (OSError, ValueError) as exc:
            self._related_tracks = RelatedTracks()
            self.related_tracks_problem = unused_related_artists_message(exc, "tracks")
            logger.warning("Related tracks file was not used: %s", exc)
        # (library entries, their keys), rebuilt when the library changes;
        # see _track_keys.  Its own lock: the build can take a while on a
        # large library, and _pick_lock must never make a track start wait.
        self._key_cache: tuple[tuple[IndexEntry, ...], TrackKeyCache] | None = None
        self._key_cache_lock = threading.Lock()
        self._state = PlayerState(
            dmca_enabled=cfg.dmca.enabled,
            repeat_rules=RepeatRules.from_playback(
                cfg.playback,
                track_window=effective,
                related=self._related_artists,
                related_tracks=self._related_tracks,
            ),
        )
        self._settings.attach(self)
        self._skip_event = threading.Event()
        self._lock = threading.Lock()
        # Shared playback position (samples) — written by the mix bus, read and
        # written by seeks.  Using a list so both sides share the same object.
        self._playback_pos: list[int] = [0]
        self._playback_output_pos: list[int] = [0]
        self._playback_len: int = 0  # end position in source-file samples
        self._current_sr: int = _DEFAULT_SR
        self._liner_entries_snapshot: tuple[IndexEntry, ...] | None = None
        self._liner_paths: frozenset[str] = frozenset()
        self._liner_paths_lock = threading.Lock()
        # Server audio: the mix bus that plays rendered tracks (built at the
        # end of __init__ unless dry-run), and a
        # hook the bridge sets to hear about each track the bus starts.
        self.bus: MixBus | None = None
        self.on_track_started: Callable[[IndexEntry], None] | None = None
        # Render cursor for the bus: the next track to render, how many of
        # its samples the previous track's overlap already played, and how
        # it was picked (for "why this track" once it starts).
        self._pending_entry: IndexEntry | None = None
        self._voice_tracking_requested = False
        self._pending_offset: int = 0
        self._pending_pick_mode: str = "seed"
        self._pending_clock_step: dict | None = None
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
            plan=lambda track: self._plan_slot(track),
            discard=lambda track: self._discard_render(track),
        )
        self._slot_lock = threading.Lock()
        self._heard_break_starts: set[str] = set()
        self._heard_break_ends: set[str] = set()
        self._heard_intros: set[str] = set()
        self._playout_slot = None
        self._playout_lock = threading.Lock()
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

    def _chooser(self) -> Any:
        """What makes the picks' random choices: the dry run's generator or the random module."""
        return self._random if self._random is not None else random

    def _now(self) -> float:
        """Wall-clock seconds the clockwheel and dayparting read; a dry run moves its own."""
        return time.time()

    def _observe(self, event: str, detail: Any) -> None:
        """Tell the dry run's observer, if any, what a pick just met."""
        observer = self.pick_observer
        if observer is not None:
            observer(event, detail)

    def stop(self) -> None:
        """Wake and stop the playback loop, including empty-library waiting."""
        self._voice_tracking_requested = False
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
        """Pick a starting track the way Shuffle does, or ``None`` if there is none.

        In DMCA mode it keeps the complement too (see :meth:`_dmca_checked`).
        """
        self._seed_clock_following = None
        self._seed_clock_step = None
        self._chosen_clock_ad_steps = ()
        chosen: IndexEntry | None
        dmca = self._pick_context() if self._state.dmca_enabled else None
        if self._library is not None:
            entries = [e for e in self.playable_entries() if not e.is_silent]
            if not entries:
                return None
            if self._cfg.playback.pick_mode == "clock" and self._clock is not None:
                boundary = self._clock_next_boundary
                now = self._now()
                if boundary is not None and boundary <= now:
                    from datetime import datetime

                    from autodj.clockwheel import next_boundary

                    following = next_boundary(
                        datetime.fromtimestamp(now).astimezone(),
                        self._clock.get("resync_minutes", [0]),
                    )
                    self._clock_next_boundary = boundary = (
                        following.timestamp() if following else None
                    )
                context = dmca or self._pick_context()
                # Taken before the pick, so a clockwheel selected meanwhile
                # leaves the seed's place out rather than mislabelled.
                self._seed_clock_name = self._clock_name()
                self._seed_clock_revision = self._clock_revision
                picked = self._clock_pick(context, None)
                if picked.entry is None:
                    return self._empty_category_fallback(None, context)
                upcoming = self._dmca_checked(picked.entry, context)
                song_step = picked.step_snapshot
                self._seed_clock_step = (
                    picked.liner_step_snapshot if picked.liner is not None else picked.step_snapshot
                )
                if picked.liner is not None:
                    if picked.liner_requires_following:
                        if not self._clock_liner_fits(
                            None,
                            picked.liner,
                            upcoming,
                            now,
                            boundary,
                        ):
                            self._seed_clock_cursor = picked.next_cursor
                            self._seed_clock_step = song_step
                            return upcoming
                        # Keep the song whose length we checked, including on a re-render.
                        self._seed_clock_following = (
                            picked.liner,
                            upcoming,
                            picked.next_cursor,
                            song_step,
                        )
                    self._seed_clock_cursor = (picked.next_cursor - 1) % len(self._clock_slots)
                    return picked.liner
                self._seed_clock_cursor = picked.next_cursor
                return upcoming
            context = dmca or self._pick_context()
            try:
                chosen = (
                    self._clock_fallback_pick(None, context)
                    if self._cfg.playback.pick_mode == "clock" and self._clock is None
                    else self._pick_pure_shuffle(None, context)
                )
            except Exception as exc:
                chosen = self._fallback_pick(None, context, exc)
        else:
            if self._cfg.playback.pick_mode == "clock" and self._clock is None:
                context = dmca or self._pick_context()
                try:
                    chosen = self._clock_fallback_pick(None, context)
                except Exception as exc:
                    chosen = self._fallback_pick(None, context, exc)
            else:
                extra = {"rng": self._random} if self._random is not None else {}
                chosen = self._sim.random_entry(**extra)
        if dmca is None or chosen is None:
            return chosen
        return self._dmca_checked(chosen, dmca)

    def playable_entries(self) -> tuple[IndexEntry, ...]:
        """Return basic-ready music, or the legacy index when no catalog is attached."""
        collection = self._library if self._library is not None else self._sim
        return collection.entries_snapshot()

    def _is_liner(self, entry: IndexEntry) -> bool:
        """Whether *entry* is voice audio, using the library's cached snapshot."""
        library = self._library
        if library is None:
            return False
        entries = library.entries_snapshot("liner")
        if entries is not self._liner_entries_snapshot:
            with self._liner_paths_lock:
                entries = library.entries_snapshot("liner")
                if entries is not self._liner_entries_snapshot:
                    self._liner_entries_snapshot = entries
                    self._liner_paths = frozenset(item.path for item in entries)
        return entry.path in self._liner_paths

    def music_playback_rate(self, entry: IndexEntry | None) -> float:
        """The configured pitch-changing playback rate for music, or unity for voice."""
        if entry is None or getattr(entry, "_prepared_clip", False) or self._is_liner(entry):
            return 1.0
        playback = getattr(self._cfg, "playback", None)
        raw_percent = getattr(playback, "music_speed_percent", 0.0)
        if isinstance(raw_percent, bool) or not isinstance(raw_percent, int | float):
            return 1.0
        percent = float(raw_percent)
        rate = 1.0 + percent / 100.0
        return rate if math.isfinite(rate) and rate > 0.0 else 1.0

    @property
    def _ledger(self) -> Any:
        """This station's :class:`~autodj.station_ledger.StationLedger`, or ``None``."""
        if self._station_ledger is not None:
            return self._station_ledger
        return getattr(self._library, "ledger", None) if self._library is not None else None

    @_ledger.setter
    def _ledger(self, ledger: Any) -> None:
        self._station_ledger = ledger

    def _clock_name(self) -> str | None:
        """The name of the loaded clockwheel, or ``None`` without one."""
        return (self._clock or {}).get("name")

    def _forget_other_clock_place(self) -> None:
        """Drop the render cursor's clock place and resync time if another clockwheel's.

        Both were worked out in the clockwheel selected before, so they mean
        nothing in the one loaded now, which starts from its own saved place
        and resync times (#133).  Runs on the render worker, which owns the
        render cursor.
        """
        clock = self._clock_name()
        pending_revision = getattr(self, "_pending_clock_cursor_revision", None)
        clock_revision = getattr(self, "_clock_revision", 0)
        if self._pending_clock_name != clock or pending_revision != clock_revision:
            self._pending_clock_cursor = None
            self._pending_clock_boundary = None
            self._pending_clock_name = clock
            self._pending_clock_cursor_revision = clock_revision

    def configure_clock(self) -> None:
        """Load the active clockwheel without marking any selected music played.

        A clockwheel that cannot load leaves the clock off, says why in the
        clock status and the log, and lets the station start anyway.  The
        setting and the clockwheel file are read first; only the swap
        holds the settings lock.
        """
        with self._clock_selection_lock:
            Player._configure_clock(self)  # also for the test doubles that borrow it

    def configure_clock_after_selection(self) -> None:
        """Load the latest saved wheel after an explicit selection."""
        with self._clock_selection_lock:
            with self._clock_lock:
                self._pending_clock_update = None
            self.configure_clock()
        self.refresh_render_ahead()

    def _configure_clock(self) -> None:
        if self._library is None:
            self._clock_off("Clockwheel waiting for library")
            return
        # The station's own key ("clock" for the default station).
        ledger = getattr(self, "_ledger", None)
        # Through the ledger: a place it has not written yet is the latest.
        setting = ledger.get_setting("clock", {}) if ledger is not None else {}
        if not isinstance(setting, dict):
            setting = {}
        with self._clock_lock:
            if self._pending_clock_update is not None and setting.get("name") == self._clock_name():
                return
            self._pending_clock_update = None
        if "slots" in setting:
            from autodj.clockwheel_store import OLD_CLOCK_MESSAGE

            logger.error(OLD_CLOCK_MESSAGE)
            self._clock_off(OLD_CLOCK_MESSAGE)
            return
        name = setting.get("name")
        if not name:
            self._clock_off("No clockwheel is active")
            return
        pending_reset = bool(setting.get("pending_restart_reset"))
        self.activate_clock(name, 0 if pending_reset else setting.get("cursor", 0))
        if pending_reset and ledger is not None and self._clock is not None:
            ledger.remember_setting("clock", self._clock)

    def _clock_off(self, status: str) -> None:
        """Turn the clockwheel off, with *status* saying why."""
        with self._clock_selection_lock, self._clock_lock, settings_lock(self):
            if getattr(self, "_clock_fallback_reason", None) != status or self._clock is not None:
                self._clock_fallback_warned = False
            self._clock_fallback_reason: str | None = status
            self._pending_clock_update = None
            self._clock = None
            self._clock_has_ad_steps = False
            self._clock_has_pending_ad_steps = False
            self._seed_clock_following = None
            self._clock_next_boundary = None
            self._clock_status = (
                Player._clock_fallback_status(self)
                if getattr(getattr(self._cfg, "playback", None), "pick_mode", None) == "clock"
                else status
            )

    def _clock_fallback_status(self) -> str:
        reason = getattr(self, "_clock_fallback_reason", None) or "no wheel selected"
        if reason == "No clockwheel is active":
            reason = "no wheel selected"
        return f"Clockwheel off ({reason}); playing ordinary picks"

    def _clock_fallback_pick(self, current: IndexEntry | None, context: _PickContext) -> IndexEntry:
        with self._clock_lock:
            warn = not getattr(self, "_clock_fallback_warned", False) and not getattr(
                self, "_dry_run", False
            )
            self._clock_fallback_warned = True
            self._clock_status = self._clock_fallback_status()
        if warn:
            logger.warning("%s", self._clock_fallback_status())
        return self._pick_pure_shuffle(current, context)

    def activate_clock(self, name: str, cursor: Any = 0) -> None:
        """Play clockwheel *name* from step *cursor*; one that cannot load leaves the clock off."""
        try:
            clock, slots = self._load_clockwheel(name)
        except (ValueError, OSError) as exc:
            status = f"Clockwheel {name} is off: failed to load: {exc}"
            logger.error("%s", status)
            self._clock_off(status)
            return
        from datetime import datetime

        from autodj.clockwheel import next_boundary

        boundary = next_boundary(
            datetime.fromtimestamp(self._now()).astimezone(), clock["resync_minutes"]
        )
        from autodj.categories import parse_step_hours

        # Each step's days and hours, by step index (None for a step without).
        step_hours = tuple(parse_step_hours(step) for step in clock["steps"])
        with self._clock_selection_lock, self._clock_lock, settings_lock(self):
            if (
                Player._clock_name(self) != name
                or getattr(self, "_clock_definition", None) != clock
            ):
                self._clock_revision += 1
                self._pending_clock_cursor = None
                self._pending_clock_boundary = None
            self._clock_definition = clock
            self._clock_steps = clock["steps"]
            self._clock_step_hours = step_hours
            self._pending_clock_cursor_revision = self._clock_revision
            self._pending_clock_update = None
            self._clock = {
                "name": name,
                "cursor": cursor,
                "resync_minutes": clock["resync_minutes"],
            }
            self._clock_slots = slots
            self._clock_has_ad_steps = any(
                getattr(slot, "kind", None) == "ad_break" for slot in slots
            )
            self._clock_has_pending_ad_steps = False
            self._clock_status = "Ready"
            self._clock_fallback_reason = None
            self._clock_fallback_warned = False
            self._seed_clock_following = None
            self._clock_next_boundary = boundary.timestamp() if boundary else None

    def apply_clockwheel_update(self, name: str, timing: str = "immediate") -> None:
        """Apply a saved active wheel now or at its next restart boundary."""
        clock, slots = self._load_clockwheel(name)
        with self._clock_selection_lock, self._clock_lock, settings_lock(self):
            refresh = self._apply_loaded_clockwheel_update(name, timing, clock, slots)
        if refresh:
            self.refresh_render_ahead()

    def _apply_loaded_clockwheel_update(
        self, name: str, timing: str, clock: dict[str, Any], slots: list[Any]
    ) -> bool:
        if self._clock_name() != name:
            return False
        old_name = self._clock_name()
        changed = old_name != name or getattr(self, "_clock_definition", None) != clock
        if not changed:
            self._pending_clock_update = None
            self._clock_has_pending_ad_steps = False
            if self._clock is not None and self._clock.get("pending_restart_reset"):
                self._clock = {
                    key: value
                    for key, value in self._clock.items()
                    if key != "pending_restart_reset"
                }
                if (ledger := self._ledger) is not None:
                    ledger.remember_setting("clock", self._clock)
            return False
        revision = self._clock_revision + 1
        if timing == "next_restart" and old_name == name and self._clock is not None:
            self._pending_clock_update = (clock, slots, revision)
            self._clock_has_pending_ad_steps = any(
                getattr(slot, "kind", None) == "ad_break" for slot in slots
            )
            self._pending_clock_name = name
            self._clock = {**self._clock, "pending_restart_reset": True}
            if (ledger := self._ledger) is not None:
                ledger.remember_setting("clock", self._clock)
            return False
        cursor = 0
        if timing == "continue" and old_name == name and self._clock is not None:
            cursor = self._clock.get("cursor", 0)
            playing = getattr(self, "_playing_render", None)
            if (
                playing is not None
                and playing.next_clock_name == name
                and playing.next_clock_revision == self._clock_revision
                and playing.next_clock_cursor is not None
            ):
                cursor = playing.next_clock_cursor
            if not isinstance(cursor, int) or not 0 <= cursor < len(clock["steps"]):
                cursor = 0
        self._pending_clock_update = None
        self._clock_has_pending_ad_steps = False
        self._clock_revision = revision
        self._clock_definition = clock
        self._seed_clock_following = None
        self._clock_slots = slots
        self._clock_has_ad_steps = any(getattr(slot, "kind", None) == "ad_break" for slot in slots)
        self._clock_steps = clock["steps"]
        from autodj.categories import parse_step_hours

        self._clock_step_hours = tuple(parse_step_hours(step) for step in clock["steps"])
        self._clock = {"name": name, "cursor": cursor, "resync_minutes": clock["resync_minutes"]}
        self._clock_status = "Ready"
        self._clock_fallback_reason = None
        self._clock_fallback_warned = False
        from datetime import datetime

        from autodj.clockwheel import next_boundary

        boundary = next_boundary(
            datetime.fromtimestamp(self._now()).astimezone(), clock["resync_minutes"]
        )
        self._clock_next_boundary = boundary.timestamp() if boundary else None
        if (ledger := self._ledger) is not None:
            ledger.remember_setting("clock", self._clock)
        self._pending_clock_cursor = None
        self._pending_clock_boundary = None
        self._pending_clock_name = name
        self._pending_clock_cursor_revision = self._clock_revision
        return True

    def _load_clockwheel(self, name: str) -> tuple[dict[str, Any], list[Any]]:
        """Read clockwheel *name* and turn its steps into playable slots."""
        from autodj.clockwheel import ClockSlot, validate_clock
        from autodj.clockwheel_store import (
            ClockwheelStore,
            resolve_conditions,
            source_folder,
            step_source,
        )
        from autodj.presets import get_preset
        from autodj.stations import StationPaths

        library = self._library
        if library is None:
            raise ValueError("the library is not attached")
        paths = getattr(self, "station_paths", None) or StationPaths.default(self._cfg)
        clock = ClockwheelStore(paths.clockwheels).load(name)
        sources = {source["id"]: source for source in library.catalog.sources()}
        slots = []
        for number, step in enumerate(clock["steps"], 1):
            kind = step["type"]
            if kind in {"music", "liner"}:
                if "conditions" in step:
                    conditions = resolve_conditions(step, sources, number)
                    slots.append(
                        ClockSlot(
                            kind=kind,
                            conditions=conditions,
                            match=step["match"],
                            selection=step["selection"],
                            enforce_rules=step["enforce_rules"],
                            require_following=step.get("require_following", False),
                        )
                    )
                else:
                    root = step_source(step, sources, number)["path"]
                    slots.append(
                        ClockSlot(
                            folder=source_folder(root, step["folder"]),
                            recursive=step["recursive"],
                            selection=step["selection"],
                            enforce_rules=step["enforce_rules"],
                            kind=kind,
                            require_following=step.get("require_following", False),
                        )
                    )
            elif kind == "track":
                root = step_source(step, sources, number)["path"]
                slots.append(ClockSlot(kind="track", path=source_folder(root, step["path"])))
            elif kind == "settings":
                get_preset(step["preset"], self._cfg.presets)
                slots.append(ClockSlot(kind="settings", preset=step["preset"]))
            elif kind == "ad_break":
                slots.append(
                    ClockSlot(
                        kind="ad_break",
                        max_spots=step["max_spots"],
                        max_seconds=step["max_seconds"],
                        advertiser=step.get("advertiser", ""),
                        ad_category=step.get("category", ""),
                    )
                )
            elif kind == "category":
                # A category deleted or emptied later is skipped when it is
                # reached, like an empty folder, so it never stops loading.
                slots.append(
                    ClockSlot(
                        kind="category",
                        category=step["category"],
                        selection=step["selection"],
                        enforce_rules=step["enforce_rules"],
                    )
                )
            else:
                raise ValueError(f"Step {number} has type {kind!r}, which the player cannot play")
        return clock, validate_clock(slots)

    def _clock_pick(self, context: _PickContext, cursor: int | None) -> Any:
        from autodj.clockwheel import ClockPick, ConditionPaths, _normalized, pick_slot

        fit = self._hour_fit_choice(context)

        with settings_lock(self):
            clock = self._clock
            slots = getattr(self, "_clock_slots", ())
            steps = getattr(self, "_clock_steps", ())
            step_hours = getattr(self, "_clock_step_hours", ())
        if clock is None:
            return ClockPick(
                None, None, 0, [getattr(self, "_clock_status", None) or "No clockwheel is active"]
            )
        position = clock.get("cursor", 0) if cursor is None else cursor

        recent = set(context.recent)
        entries = self.playable_entries()
        closed = context.closed
        eligible = self._eligible_entries(entries, context.protected, closed)
        # DMCA mode holds every slot to the complement, even one that does
        # not enforce the repeat rules.
        complying = (
            eligible
            if context.complement is None or context.complement.blocks_nothing
            else tuple(entry for entry in eligible if context.allows(entry))
        )
        ledger = self._ledger
        last_played = ledger.last_played() if ledger is not None else {}
        normalized_history = (
            {
                _normalized(path): stamp
                for path, stamp in last_played.items()
                if isinstance(path, str)
            }
            if any(slot.selection == "least_recent" for slot in slots)
            else None
        )
        liners = self._library.entries_snapshot("liner")
        # Liner steps pass over liners outside their play dates; the step
        # conditions are still worked out from every liner, so they stay cached.
        liner_filter = getattr(self, "liner_filter", None)
        # Judged by the day the liner airs, before the song being picked.
        playable_liners = liner_filter(liners, context.at) if liner_filter is not None else liners
        categories = self._category_members()
        self._clock_category_error = "Categories could not be read" if categories is None else None
        categories = categories or {}
        beets_items: Mapping[str, Mapping[str, Any]] | None = None
        condition_errors = {}
        if self._clock_category_error:
            for index, slot in enumerate(slots):
                if slot.kind == "category" or any(
                    condition["field"] == "category" for condition in slot.conditions
                ):
                    condition_errors[index] = self._clock_category_error
        fields = {
            condition["name"]
            for slot in slots
            for condition in slot.conditions
            if condition["field"] == "beets"
        }
        if fields:
            known, items, error = self._library.clock_beets.snapshot(fields)
            # The same mapping until Beets is read again, so the cache below holds.
            beets_items = items or None
            for index, slot in enumerate(slots):
                for condition in slot.conditions:
                    if condition["field"] == "beets":
                        if error:
                            condition_errors[index] = error
                        elif condition["name"] not in known:
                            condition_errors[index] = (
                                f"Beets has no field named {condition['name']}"
                            )
        if condition_errors:
            reasons = tuple(sorted(condition_errors.items()))
            if reasons != getattr(self, "_clock_condition_errors", ()):
                logger.warning("Clock conditions: %s", "; ".join(condition_errors.values()))
            self._clock_condition_errors = reasons
        else:
            self._clock_condition_errors = ()
        cache = getattr(self, "_clock_condition_cache", None)
        if cache is None or not cache.current(slots, entries, liners, beets_items, categories):
            cache = ConditionPaths(slots, entries, liners, beets_items, categories)
            self._clock_condition_cache = cache

        closed_steps = self._closed_clock_steps(context, step_hours)

        def pick(
            use_rules: bool,
            use_recent: bool,
            use_dmca: bool = True,
            protected: frozenset[str] = context.protected,
            hours: bool = True,
        ) -> Any:
            shut = closed if hours else frozenset()

            def allowed(entry: IndexEntry) -> bool:
                return (
                    not entry.is_silent
                    and entry.path not in protected
                    and entry.path not in shut
                    and not (use_recent and entry.path in recent)
                    and (not use_rules or context.repeat(entry))
                )

            if protected is context.protected and hours:
                pool = complying if use_dmca else eligible
            else:
                pool = tuple(entry for entry in entries if allowed(entry))
            return pick_slot(
                slots,
                position,
                pool,
                last_played,
                allowed,
                self._random,
                liners=playable_liners,
                categories=categories,
                beets=beets_items,
                # A step outside its days and hours is skipped like a step
                # whose conditions cannot be read.
                condition_errors={**condition_errors, **closed_steps}
                if closed_steps
                else condition_errors,
                condition_cache=cache,
                closed_categories=context.closed_categories if hours else None,
                normalized_history=normalized_history,
                versions=self._song_versions(),
                fit=fit,
                preferred_path=getattr(self, "_preview_preferred_path", None),
            )

        # Like the other pick modes, relax rather than stop the music: first
        # the artist, album and title rules, then the track window, then the
        # DMCA complement (_dmca_checked then looks for a track that keeps
        # it in the whole library).
        picked = pick(True, True)
        self._observe("clock_skipped", picked.skipped)
        if picked.entry is None and context.repeat:
            logger.info(
                "Clock: every matching track is held back (%s); relaxing %s for this pick",
                context.repeat.summary() or "repeat rules",
                RELAXED_RULES,
            )
            picked = pick(False, True)
        if picked.entry is None and recent:
            logger.info(
                "Clock: every matching track played recently; relaxing the track "
                "window for this pick"
            )
            picked = pick(False, False)
        if picked.entry is None and context.complement is not None:
            logger.info(
                "Clock: every matching track would break the DMCA complement; "
                "looking further for this pick"
            )
            picked = pick(False, False, use_dmca=False)
        follows_only = frozenset({context.follows} if context.follows else ())
        if picked.entry is None and context.protected:
            # Last resort: a slot whose only songs are protected (a small
            # folder, or a track step for a song just played) would leave
            # the station silent, so allow them, except the one playing.
            picked = pick(False, False, use_dmca=False, protected=follows_only)
            if picked.entry is not None and not getattr(self, "_clock_protected_warned", False):
                logger.warning(
                    "Clock: every matching track is playing or about to play; "
                    "repeating %s rather than stopping the music",
                    picked.entry.display_name,
                )
            self._clock_protected_warned = picked.entry is not None
        elif picked.entry is not None:
            self._clock_protected_warned = False
        # Whether only dayparting left the clock with nothing: a pick that
        # ignored the hours would find a song.  The caller then plays an
        # allowed song from the whole library rather than stopping the music
        # (see _empty_category_fallback); pick_slot is pure, so this changes
        # nothing else.
        self._clock_closed_only = (
            picked.entry is None
            and bool(closed or context.closed_categories)
            and pick(False, False, use_dmca=False, protected=follows_only, hours=False).entry
            is not None
        )
        self._clock_status = "; ".join(picked.skipped) or "Ready"
        if self._clock_closed_only:
            self._clock_status += (
                f"; every song this clockwheel can play is outside its {_closure_words(context)}"
            )
        self._log_clock_skips(picked.skipped)
        if picked.entry is not None and picked.preset:
            from autodj.presets import get_preset

            try:
                self._preset = get_preset(picked.preset, self._cfg.presets)
            except ValueError as exc:
                self._clock_status = f"Settings step skipped: {exc}"
        if picked.entry is not None:
            _log_repeat_blocks(context.repeat, picked.entry)
        return replace(
            picked,
            step_snapshot=self._clock_step_snapshot(picked.step_index, steps),
            liner_step_snapshot=self._clock_step_snapshot(picked.liner_step_index, steps),
            ad_step_snapshots=tuple(
                {
                    "index": index,
                    "number": index + 1,
                    "clock": clock["name"],
                    "step": deepcopy(steps[index]),
                }
                for index in picked.ad_steps
            )
            if picked.entry is not None
            else (),
        )

    def _promote_pending_clock(self) -> None:
        with self._clock_lock, settings_lock(self):
            self._promote_pending_clock_locked()

    def _promote_pending_clock_locked(self) -> None:
        pending = self._pending_clock_update
        if pending is None:
            return
        clock, slots, revision = pending
        name = (self._clock or {}).get("name")
        self._clock_definition = clock
        self._clock_slots = slots
        self._clock_has_ad_steps = any(getattr(slot, "kind", None) == "ad_break" for slot in slots)
        self._clock_steps = clock["steps"]
        from autodj.categories import parse_step_hours

        self._clock_step_hours = tuple(parse_step_hours(step) for step in clock["steps"])
        self._clock_revision = revision
        self._seed_clock_following = None
        self._clock = {"name": name, "cursor": 0, "resync_minutes": clock["resync_minutes"]}
        self._pending_clock_update = None
        self._clock_has_pending_ad_steps = False
        from datetime import datetime

        from autodj.clockwheel import next_boundary

        boundary = next_boundary(
            datetime.fromtimestamp(self._now()).astimezone(), clock["resync_minutes"]
        )
        self._clock_next_boundary = boundary.timestamp() if boundary else None
        self._pending_clock_cursor = 0
        self._pending_clock_name = name
        self._pending_clock_cursor_revision = revision
        self._pending_clock_boundary = self._clock_next_boundary
        if (ledger := self._ledger) is not None:
            ledger.remember_setting("clock", self._clock)

    def _clock_step_snapshot(
        self, index: int | None, steps: Sequence[dict] | None = None
    ) -> dict | None:
        """Freeze the selected native step and labels for the Why display."""
        if steps is None:
            steps = getattr(self, "_clock_steps", ())
        if index is None or not 0 <= index < len(steps):
            return None
        step = deepcopy(steps[index])
        source_ids = {step["source"]} if "source" in step else set()
        source_ids.update(
            condition["source"]
            for condition in step.get("conditions", ())
            if condition.get("field") == "folder"
        )
        try:
            sources = (
                [
                    {"id": source["id"], "path": Path(source["path"]).name}
                    for source in self._library.catalog.sources()
                    if source["id"] in source_ids
                ]
                if source_ids
                else []
            )
        except Exception:
            sources = []
        category_ids = {step["category"]} if step["type"] == "category" else set()
        category_ids.update(
            condition["category"]
            for condition in step.get("conditions", ())
            if condition.get("field") == "category"
        )
        category_index = self._category_index()
        try:
            categories = (
                [
                    {"id": category.id, "name": category.name}
                    for category in category_index.categories()
                    if category.id in category_ids
                ]
                if category_index is not None and category_ids
                else []
            )
        except Exception:
            categories = []
        return {"number": index + 1, "step": step, "sources": sources, "categories": categories}

    def _closed_clock_steps(
        self, context: _PickContext, step_hours: Sequence[Any]
    ) -> dict[int, str]:
        """The clockwheel steps outside their days and hours, by index, and why.

        Checked at the expected start of the song being picked, as the
        categories' hours are.
        """
        if not any(step_hours):
            return {}
        wall = self._station_now(context.at)
        closed = {}
        for index, hours in enumerate(step_hours):
            reason = hours.refusal(wall) if hours is not None else None
            if reason:
                closed[index] = reason
        return closed

    def _eligible_entries(
        self,
        entries: tuple[IndexEntry, ...],
        protected: AbstractSet[str],
        closed: AbstractSet[str],
    ) -> tuple[IndexEntry, ...]:
        """*entries* that are not silent, protected or outside their hours, in library order.

        A clock pick needs this for the whole library on every pick.  The
        songs left after the silent and closed ones are kept until the
        library or the closed songs change, so a pick only cuts the few
        protected ones out of them.
        """
        cached = getattr(self, "_eligible_cache", None)
        if (
            cached is None
            or cached[0] is not entries
            or (cached[1] is not closed and cached[1] != closed)
        ):
            base = tuple(
                entry for entry in entries if entry.path not in closed and not entry.is_silent
            )
            where: dict[str, int] | None = {entry.path: index for index, entry in enumerate(base)}
            if where is not None and len(where) != len(base):
                where = None
            cached = (entries, closed, base, where)
            self._eligible_cache = cached
        base, where = cached[2], cached[3]
        if where is None:  # Two entries share a path: cut by path, one by one.
            return tuple(entry for entry in base if entry.path not in protected)
        cuts = sorted(where[path] for path in protected if path in where)
        if not cuts:
            return base
        parts = []
        begin = 0
        for cut in cuts:
            parts.append(base[begin:cut])
            begin = cut + 1
        parts.append(base[begin:])
        return tuple(chain.from_iterable(parts))

    def _category_index(self) -> Any:
        """This station's rotation categories, or ``None`` without a library."""
        library = self._library
        if library is None:
            return None
        pick = getattr(library, "categories_for", None)
        if not callable(pick):
            return getattr(library, "categories", None)
        return pick(getattr(self, "station_paths", None))

    def _category_members(self) -> dict[str, tuple[str, frozenset[str]]] | None:
        """Return category membership, or None when the index cannot be read."""
        index = self._category_index()
        if index is None:
            return {}
        try:
            members: dict[str, tuple[str, frozenset[str]]] = index.members()
        except Exception:
            logger.exception("Clock: the rotation categories could not be read")
            return None
        return members

    def _empty_category_fallback(
        self, current: IndexEntry | None, context: _PickContext
    ) -> IndexEntry | None:
        """A song from the whole library when legacy category steps found none.

        A category emptied by a failed playlist script or a deleted category
        must not silence a clockwheel made of category steps, so the pick
        falls back as the other pick modes do (the clock keeps its place).
        A clock with folders, tracks or explicit conditions is left as it was,
        unless dayparting alone holds its songs back.
        """
        slots = getattr(self, "_clock_slots", None) or ()
        # Dayparting alone emptied the clock: its songs exist but are outside
        # their hours, so an allowed song plays, whatever the steps are.
        if not getattr(self, "_clock_closed_only", False):
            if any(getattr(slot, "conditions", ()) for slot in slots):
                return None
            if not any(getattr(slot, "kind", None) == "category" for slot in slots):
                return None
        reason = ValueError(f"the clockwheel has no song to play: {self._clock_status}")
        try:
            chosen = self._fallback_pick(current, context, reason)
        except Exception:
            return None
        return self._dmca_checked(chosen, context)

    def _log_clock_skips(self, skipped: list[str]) -> None:
        """Log skipped clock steps once until the reasons change or clear."""
        if getattr(self, "_dry_run", False):
            return
        reasons = tuple(skipped)
        if reasons and reasons != getattr(self, "_clock_skips_logged", ()):
            blocked = [
                reason
                for reason in reasons
                if "blocked by rules" in reason and "missing or blocked by rules" not in reason
            ]
            other = [reason for reason in reasons if reason not in blocked]
            if other:
                logger.warning("Clock selection skipped: %s", "; ".join(other))
            if blocked:
                logger.info("Clock selection skipped: %s", "; ".join(blocked))
        self._clock_skips_logged = reasons

    def _candidate_filter(self, context: _PickContext) -> Callable[[IndexEntry], bool]:
        """The repeat rules and, in DMCA mode, the complement, as one filter."""
        if context.complement is None:
            return context.repeat

        def allowed(entry: IndexEntry) -> bool:
            return context.repeat(entry) and context.allows(entry)

        return allowed

    def _dmca_checked(
        self, entry: IndexEntry, context: _PickContext, *, queued: bool = False
    ) -> IndexEntry:
        """*entry*, or in DMCA mode, when it would break the complement, a track that keeps it.

        A song from the queue plays anyway (the operator confirmed it), and
        the limits it breaks are logged.  Otherwise the replacement comes
        from the whole library, relaxing as little as possible (see
        :func:`~autodj.dmca.relaxed`): a track the repeat rules allow, then
        any track not played recently, then any but the protected ones.
        When no track keeps every limit, one that breaks the fewest plays and
        a warning names the limits it breaks: the music never stops.
        """
        if not queued and entry.path in context.closed:
            entry = self._daypart_replacement(entry, context)
        limits = context.complement
        if limits is None or not (broken := limits.broken(entry)):
            return entry
        if queued:
            logger.warning(
                "DMCA mode: queued %s plays although it breaks the limit: %s",
                entry.display_name,
                "; ".join(broken),
            )
            return entry
        audible = [
            other
            for other in self.playable_entries()
            if not other.is_silent
            and other.path not in context.protected
            and other.path not in context.closed
        ]
        recent = set(context.recent)
        fresh = [other for other in audible if other.path not in recent]
        # The repeat rules, the daypart and quarter-hour ones among them, only
        # choose among tracks that keep the DMCA limits: relaxed() takes the
        # first tier with such a track and goes to a wider tier before it gives
        # one that breaks a limit, so preferring tracks the rules allow never
        # weakens DMCA mode.
        choices = relaxed(
            ([other for other in fresh if context.repeat(other)], fresh, audible), limits.broken
        )
        if not choices:
            logger.warning(
                "DMCA mode: %s plays although it breaks the limit (%s): nothing else can play",
                entry.display_name,
                "; ".join(broken),
            )
            return entry
        chosen, still = self._chooser().choice(choices)  # nosec B311 -- music selection
        self._observe("dmca_replaced", broken)
        if still:
            logger.warning(
                "DMCA mode: no track keeps every limit; %s plays although it breaks %s",
                chosen.display_name,
                "; ".join(still),
            )
        else:
            logger.info(
                "DMCA mode: %s would break the limit (%s); %s plays instead",
                entry.display_name,
                "; ".join(broken),
                chosen.display_name,
            )
        return chosen

    def _daypart_replacement(self, entry: IndexEntry, context: _PickContext) -> IndexEntry:
        """A song to play instead of *entry*, whose category is outside its hours.

        Relaxes as little as possible, as the DMCA check does: a song the
        repeat rules allow, then any not played recently, then any but the
        protected ones; never one outside its own hours.  When every song is
        outside its hours, *entry* plays and a warning says so.
        """
        audible = [
            other
            for other in self.playable_entries()
            if not other.is_silent
            and other.path not in context.protected
            and other.path not in context.closed
        ]
        recent = set(context.recent)
        fresh = [other for other in audible if other.path not in recent]
        for pool in ([other for other in fresh if context.repeat(other)], fresh, audible):
            if pool:
                chosen = self._choose(pool)
                logger.info(
                    "Dayparting: %s is outside its category's %s; %s plays instead",
                    entry.display_name,
                    _closure_words(context),
                    chosen.display_name,
                )
                return chosen
        self._warn_all_closed(
            context.closed,
            f"Dayparting: {entry.display_name} plays outside its category's "
            f"{_closure_words(context)}: "
            "no other song can play",
        )
        return entry

    def _warn_all_closed(self, closed: frozenset[str], message: str) -> None:
        """Warn that a song plays outside its hours, once while the closed songs stay the same.

        A pick can meet this twice (the fallback, then the final check), and
        every pick meets it again until a category's hours open, so only the
        first is a warning; the rest go to the debug log.
        """
        warned = getattr(self, "_all_closed_warned", None)
        if warned is not None and (warned is closed or warned == closed):
            logger.debug(message)
            return
        self._all_closed_warned: frozenset[str] | None = closed
        logger.warning(message)

    def _station_queue(
        self,
        reserved: IndexEntry | None,
        reserved_liners: tuple[IndexEntry, ...],
    ) -> tuple[list[IndexEntry], IndexEntry | None]:
        with self._state.queue_lock:
            waiting = ([self._state.queued_next] if self._state.queued_next else []) + list(
                self._state.queue
            )
            for used in (*reserved_liners, *((reserved,) if reserved else ())):
                for number, item in enumerate(waiting):
                    if item.path == used.path:
                        del waiting[number]
                        break
        liner_paths = {entry.path for entry in self._library.entries_snapshot("liner")}
        liners: list[IndexEntry] = []
        for entry in waiting:
            if entry.path not in liner_paths:
                return liners, entry
            liners.append(entry)
        return liners, None

    def _clock_music_span(self, entry: IndexEntry) -> tuple[float, float]:
        """The source start and next handoff, excluding silence and unused tails."""
        points = self.saved_points(entry)
        start = points.get("start") or 0.0
        end = points.get("end")
        end = entry.length if end is None else min(end, entry.length)
        segue = next(
            (points[key] for key in ("segue", "outro") if points.get(key) is not None), end
        )
        return start, max(start, min(segue, end))

    def _browser_handoff(self, current: IndexEntry) -> float:
        """When the track after *current* starts in browser playback, in wall-clock seconds.

        Now when *current* is no longer the track playing.  Otherwise its
        handoff less what of it has already played (the page reports a
        start through :meth:`note_browser_start`), so a pick made half way
        through a song, after a skip of the next track say, is not timed
        from the song's start.
        """
        now = self._now()
        if self._state.current_track is not current:
            return now
        started = getattr(self, "_browser_started", None)
        elapsed = now - started[1] if started is not None and started[0] is current else 0.0
        return max(now, self._handoff_time(current) - max(0.0, elapsed))

    def note_browser_start(self, entry: IndexEntry) -> None:
        """Remember that *entry* started playing in the browser now."""
        self._browser_started = (entry, self._now())

    def _handoff_time(self, current: IndexEntry, *, lead_s: float | None = None) -> float:
        """When the song after *current* is expected to start, in wall-clock seconds.

        The next song is picked while *current* is still ahead (rendering
        ahead) or has just started, so this is when *current*, from the
        offset its render starts at, reaches its handoff, plus whatever of
        the song before it is still to play.  *lead_s*, when given, is how
        long until *current* starts (a song change planned a song early,
        #88); otherwise the playing render tells.  Dayparting checks the
        hours at this time, not at the pick, so a song does not start after
        its window has closed.
        """
        try:
            start, segue = self._clock_music_span(current)
        except Exception:
            # A cue that cannot be read must not stop the pick: the whole song plays.
            logger.debug("No saved cues for %s; timing its whole length", current.path)
            start, segue = 0.0, float(current.length or 0.0)
        offset = getattr(self, "_pending_offset", 0)
        when = self._now() + max(0.0, segue - max(start, offset / SAMPLE_RATE)) / max(
            self.music_playback_rate(current), 1e-9
        )
        playing = getattr(self, "_playing_render", None)
        if lead_s is not None:
            when += max(0.0, lead_s)
        elif playing is not None and playing.next_entry is current:
            when += self._playing_remaining_s()
        return when

    def _clock_liner_fits(
        self,
        current: IndexEntry | None,
        liner: IndexEntry,
        upcoming: IndexEntry,
        when: float,
        boundary: float | None,
    ) -> bool:
        """Estimate the pair from saved cues, without decoding audio to choose a song."""
        if boundary is None:
            return True
        start, segue = self._clock_music_span(upcoming)
        cues = self.saved_points(liner)
        liner_start = cues.get("start") or 0.0
        liner_end = cues.get("end")
        liner_end = liner.length if liner_end is None else min(liner_end, liner.length)
        main_end = cues.get("segue")
        main = max(
            0.0, min(liner_end, main_end if main_end is not None else liner_end) - liner_start
        )
        overlap = cues.get("overlap_start")
        from autodj.playout import liner_release_frames

        rate = max(self.music_playback_rate(upcoming), 1e-9)
        intro = max(0.0, (self.saved_points(upcoming).get("intro") or start) - start) / rate
        release = (
            liner_release_frames(round(main * SAMPLE_RATE), round(intro * SAMPLE_RATE), overlap)
            / SAMPLE_RATE
        )
        lead = 0.0
        record = self._library.catalog.record_for_path(liner.path) if self._library else None
        if current is not None and record and record.get("allow_outro", False):
            current_start, current_segue = self._clock_music_span(current)
            outro = self.saved_points(current).get("outro")
            if outro is not None:
                lead = max(0.0, current_segue - max(current_start, outro)) / max(
                    self.music_playback_rate(current), 1e-9
                )
        if segue > start and when + max(0.0, release - lead) + (segue - start) / rate <= boundary:
            return True
        self._clock_status = (
            "Skipping liner: the liner and following track do not fit before the reset"
        )
        logger.info("%s (%s -> %s)", self._clock_status, liner.display_name, upcoming.display_name)
        return False

    def _choose_station_next(
        self,
        current: IndexEntry,
        context: _PickContext,
        *,
        also_reserved: tuple[IndexEntry, ...] = (),
        lead_s: float | None = None,
        auto_liner: bool = True,
    ) -> tuple[IndexEntry, str, tuple[tuple[IndexEntry, bool], ...], int | None, str]:
        """Pick what follows *current* in saved-point playback, with its liners.

        *also_reserved* are queue entries earlier renders hold, besides the
        pending track's own liners.  *lead_s* is how long until *current*
        starts, when the pick is made a song early (#88); otherwise the
        playing render tells.  Without *auto_liner* the liner scheduler is
        not asked for an automatic liner.
        """
        liners, queued = self._station_queue(
            context.queue_reserved, (*self._pending_reserved_liners, *also_reserved)
        )
        self._chosen_clock_step = None
        self._chosen_clock_ad_steps = ()
        self._chosen_clock_revision = self._clock_revision
        self._forget_other_clock_place()
        cursor = self._pending_clock_cursor
        seed = self._seed_clock_following
        if seed is not None and seed[0].path != current.path:
            self._seed_clock_following = seed = None
        if queued is not None:
            upcoming, mode = self._dmca_checked(queued, context, queued=True), "queue"
            marked_cursor = attached(self, "_request_clock_steps").get(queued)
            if (
                marked_cursor is not None
                and self._cfg.playback.pick_mode == "clock"
                and self._clock is not None
            ):
                starting_cursor = (self._clock or {}).get("cursor", 0) if cursor is None else cursor
                picked = self._clock_pick(context, starting_cursor)
                expected_step = (marked_cursor - 1) % len(self._clock_slots)
                if picked.step_index == expected_step:
                    self._chosen_clock_step = picked.step_snapshot
                    boundary = self._pending_clock_boundary or getattr(
                        self, "_clock_next_boundary", None
                    )
                    if picked.liner is not None and (
                        not picked.liner_requires_following
                        or self._clock_liner_fits(
                            current,
                            picked.liner,
                            upcoming,
                            self._handoff_time(current, lead_s=lead_s) if boundary else 0.0,
                            boundary,
                        )
                    ):
                        liners.append(picked.liner)
                cursor = marked_cursor
        elif self._cfg.playback.pick_mode == "clock" and self._clock is not None:
            if seed is not None:
                self._chosen_clock_step = (
                    seed[3] if len(seed) > 3 else getattr(self, "_seed_clock_step", None)
                )
                return (
                    self._dmca_checked(seed[1], context),
                    "clock",
                    tuple((entry, True) for entry in liners),
                    seed[2],
                    "",
                )
            starting_cursor = (self._clock or {}).get("cursor", 0) if cursor is None else cursor
            boundary = self._pending_clock_boundary or getattr(self, "_clock_next_boundary", None)
            # Carry the restart time on with the cursor: a set's first render
            # has none of its own, and a pick that did not restart would
            # otherwise pass none on, so the wheel would never restart (#357).
            self._pending_clock_boundary = boundary
            natural_restart = self._pending_clock_update is not None and boundary is None
            if natural_restart and starting_cursor == 0:
                self._promote_pending_clock()
                self._chosen_clock_revision = self._clock_revision
                cursor = 0
            picked = self._clock_pick(context, cursor)
            if (
                natural_restart
                and picked.entry is not None
                and picked.step_index is not None
                and picked.step_index < starting_cursor
            ):
                self._promote_pending_clock()
                self._chosen_clock_revision = self._clock_revision
                picked = self._clock_pick(context, 0)
            self._chosen_clock_step = picked.step_snapshot
            self._chosen_clock_ad_steps = picked.ad_step_snapshots
            if picked.entry is None:
                # Only empty or deleted categories left nothing to play.
                rescue = self._empty_category_fallback(current, context)
                if rescue is None:
                    raise ValueError(self._clock_status)
                upcoming, mode = rescue, "fallback"
            else:
                restart = False
                if boundary is not None:
                    from datetime import datetime

                    from autodj.clockwheel import next_boundary, resync_due

                    when = self._handoff_time(current, lead_s=lead_s)
                    track_start, track_segue = self._clock_music_span(picked.entry)
                    duration_s = (track_segue - track_start) / self.music_playback_rate(
                        picked.entry
                    )
                    if resync_due(when, duration_s, boundary):
                        restart = True
                if restart:
                    assert boundary is not None
                    self._promote_pending_clock()
                    reset = self._clock_pick(context, 0)
                    self._chosen_clock_revision = self._clock_revision
                    if reset.entry is None:
                        rescue = self._empty_category_fallback(current, context)
                        if rescue is None:
                            raise ValueError(self._clock_status)
                        self._chosen_clock_step = None
                        self._chosen_clock_ad_steps = ()
                        return (
                            rescue,
                            "fallback",
                            tuple((entry, True) for entry in liners),
                            None,
                            "",
                        )
                    picked = reset
                    self._chosen_clock_step = reset.step_snapshot
                    self._chosen_clock_ad_steps = reset.ad_step_snapshots
                    self._clock_status = "Restarting the clock at the nearest song handoff"
                    following = next_boundary(
                        datetime.fromtimestamp(max(boundary, when)).astimezone(),
                        (self._clock or {}).get("resync_minutes", [0]),
                    )
                    boundary = following.timestamp() if following else None
                    self._pending_clock_boundary = boundary
                upcoming = self._dmca_checked(picked.entry, context)
                mode, cursor = "clock", picked.next_cursor
                self._log_clock_pick(
                    picked, upcoming, boundary, when if boundary else None, restart
                )
                if (
                    picked.liner is not None
                    and not liners
                    and (
                        not picked.liner_requires_following
                        or self._clock_liner_fits(
                            current,
                            picked.liner,
                            upcoming,
                            when if boundary is not None else 0.0,
                            boundary,
                        )
                    )
                ):
                    return upcoming, mode, ((picked.liner, False),), cursor, ""
        else:
            try:
                if self._cfg.playback.pick_mode == "clock" and self._clock is None:
                    upcoming, mode = self._clock_fallback_pick(current, context), "fallback"
                else:
                    upcoming, mode = self._choose_by_mode(current, context)
            except Exception as exc:
                upcoming, mode = self._fallback_pick(current, context, exc), "fallback"
            upcoming = self._dmca_checked(upcoming, context)
        selected = tuple((entry, True) for entry in liners)
        key = ""
        if not selected and auto_liner:
            selected, key = self._auto_liner(current, upcoming)
        return upcoming, mode, selected, cursor, key

    def _log_clock_pick(
        self,
        picked: Any,
        upcoming: IndexEntry,
        boundary: float | None,
        when: float | None,
        restart: bool,
    ) -> None:
        """Log a clockwheel pick and the restart decision behind it, in one line."""
        from datetime import datetime

        def clock(stamp: float) -> str:
            return datetime.fromtimestamp(stamp).strftime("%H:%M:%S")

        step = "" if picked.step_index is None else f"step {picked.step_index + 1}: "
        liner = f" after {picked.liner.display_name}" if picked.liner is not None else ""
        if boundary is None or when is None:
            timing = "no restart time"
        elif restart:
            timing = f"restarted the wheel for the restart due {clock(boundary)}"
        else:
            timing = f"starts {clock(when)}, restart due {clock(boundary)} not yet"
        logger.info(
            "Clockwheel %s%s%s (%s)",
            step,
            upcoming.display_name,
            liner,
            timing,
        )

    def _miss_unbooked_ad_steps(self, steps: tuple[dict, ...], slot_id: str = "") -> None:
        desk = getattr(self, "ad_desk", None)
        if desk is None:
            return
        if not desk.enabled:
            self._log_clock_skips(
                [
                    f"Step {step['number']}: ad break skipped: ad breaks are off for this station"
                    for step in steps
                ]
            )
        elif not slot_id or desk.handled_count(slot_id) < len(steps):
            missed = steps if not slot_id else steps[desk.handled_count(slot_id) :]
            desk.missed_steps(
                missed,
                desk.refusal() or "its song change was rendered before its spots could be booked",
            )

    def _auto_liner(
        self, current: IndexEntry, upcoming: IndexEntry
    ) -> tuple[tuple[tuple[IndexEntry, bool], ...], str]:
        """An automatic liner for the handoff, outside the clock, and its plan key."""
        scheduler = self.liner_scheduler
        if scheduler is None or (
            self._cfg.playback.pick_mode == "clock" and self._clock is not None
        ):
            return (), ""
        plan = getattr(scheduler, "plan_for_handoff", None)
        if plan is None:
            return (), ""
        key = f"{current.path}\n{upcoming.path}"
        points = self.saved_points(current)
        seconds = float(points.get("segue") or points.get("outro") or current.length) / max(
            self.music_playback_rate(current), 1e-9
        )
        name = plan(key, tracks_ahead=1, seconds_ahead=seconds)
        if name:
            record = self._library.record_for_id(name)
            entry = self._library.entry_for_path(record["path"]) if record else None
            if entry is not None:
                return ((entry, False),), key
        return (), key

    def _transition_voice(
        self,
        current: IndexEntry,
        upcoming: IndexEntry,
        liners: tuple[tuple[IndexEntry, bool], ...],
        breaks: tuple[Any, ...],
        at: float | None,
    ) -> tuple[tuple[tuple[IndexEntry, bool], ...], str, Any]:
        """Choose handoff voice using the same precedence for playback and preview."""
        if getattr(current, "_prepared_clip", False) or getattr(upcoming, "_prepared_clip", False):
            return liners, "", None
        key = ""
        if self._cfg.playback.matched_intro_only_without_liner:
            if not breaks and not liners:
                liners, key = self._auto_liner(current, upcoming)
            intro = None if liners else self._matched_intro(upcoming, breaks, at)
        else:
            intro = self._matched_intro(upcoming, breaks, at)
            if not breaks and not liners and intro is None:
                liners, key = self._auto_liner(current, upcoming)
        return liners, key, intro

    def _matched_intro(
        self, upcoming: IndexEntry | None, breaks: tuple[Break, ...], at: float | None
    ) -> Any:
        """The intro matched to *upcoming* for its song change, airing at *at*, or ``None`` (#127).

        A DJ break booked for the change wins: the DJ already talks the song
        up.  The station's store rolls the chance on its own random source,
        so the picker's choices, and with them the rotation tester's and the
        hour preview's, stay the same.
        """
        store = self.matched_intros
        if store is None or upcoming is None or self._library is None:
            return None
        if any(item.kind == "dj" for item in breaks):
            return None
        try:
            record = self._library.catalog.record_for_path(upcoming.path)
            if record is None or record.get("kind", "music") != "music":
                return None
            artist = str(getattr(upcoming, "artist", "") or record.get("artist") or "")
            when = at if at is not None else self._now()
            song_id = str(record.get("id") or "")
            rules = getattr(store, "rules", None)
            if rules is None:
                return store.choose(song_id, artist, when)
            # Intro rules (#127) come after the song's and the artist's own
            # intros unless a rule asks to go before one of them.  Their
            # clips must pass the station's liner filter: its own liners,
            # within their play dates.
            allowed = getattr(self, "liner_filter", None)

            def by_rule(priority: str) -> Any:
                return rules.choose(upcoming, when, self._library, priority, allowed)

            priorities = rules.priorities()
            if not priorities.intersection({"before_song", "before_artist"}):
                return store.choose(song_id, artist, when) or by_rule("after_artist")
            if "before_song" in priorities and (chosen := by_rule("before_song")) is not None:
                return chosen
            if (chosen := store.choose(song_id, artist, when, "song")) is not None:
                return chosen
            if "before_artist" in priorities and (chosen := by_rule("before_artist")) is not None:
                return chosen
            return store.choose(song_id, artist, when, "artist") or by_rule("after_artist")
        except Exception:
            # A store that cannot be read must not stop the render.
            logger.exception("Could not choose a matched intro for %s", upcoming.path)
            return None

    def _release_intros(self, track: RenderedTrack) -> None:
        """Let go of the intros held for *track*'s song change, which will not air."""
        store = self.matched_intros
        if store is not None and track.intro_events:
            store.release(intro_id for _at, intro_id, _end, _label in track.intro_events)

    def _settle_intros(self, track: RenderedTrack | None) -> None:
        """Queue release of intros in a playing render that ended before they aired."""
        store = self.matched_intros
        if store is not None and track is not None and track.intro_events:
            heard = self._heard_intros
            store.release_later(
                intro_id
                for _at, intro_id, _end, _label in track.intro_events
                if intro_id not in heard
            )

    def _build_bus(self) -> MixBus:
        """Create the mix bus, fed by the render-ahead worker, as :attr:`bus`."""
        self.bus = MixBus(
            BusEvents(
                on_track_start=self._on_track_start,
                on_position=self._on_position,
                on_need_track=self._take_render,
            ),
            eq_gains=lambda: (self._eq_low, self._eq_mid, self._eq_high),
            skip_tail=self._plan_skip,
        )
        return self.bus

    def _plan_skip(self, track: RenderedTrack, pos: int) -> SkipTail | None:
        """The ``[playback] skip_style`` effect for skipping *track* at *pos*.

        The mix bus's ``skip_tail``: it runs on the thread that asked for
        the skip, never on the bus thread.  The effect starts a moment
        ahead of *pos* (:attr:`_SKIP_LOOKAHEAD`) and is timed by the
        track's tempo when :func:`~autodj.transitions.trusted_bpm` trusts
        it, ending on a beat of its cached grid (never a fresh analysis).

        Returns:
            The effect, or ``None`` for the plain fade (the ``"fade"``
            style, or too close to the end of the track).
        """
        from autodj.skip_fx import SKIP_STYLES, render_tail, tail_frames
        from autodj.transitions import trusted_bpm

        style = self._cfg.playback.skip_style
        start = pos + self._SKIP_LOOKAHEAD
        if style == "fade" or style not in SKIP_STYLES or start >= len(track.audio):
            return None
        entry = track.entry
        bpm = trusted_bpm(entry.bpm, getattr(entry, "tempo_confidence", 0.0))
        effective_bpm = bpm * track.playback_rate if bpm else 0.0
        beats: list[int] = []
        if bpm:
            meta = self._peek_incoming_meta(entry)
            if meta is not None:
                beats = [
                    _music_output_frame(
                        round(t * SAMPLE_RATE) - track.start_offset, track.playback_rate
                    )
                    for t in meta.beats
                ]
        frames = tail_frames(start, effective_bpm, beats)
        beat = round(60.0 / effective_bpm * SAMPLE_RATE) if effective_bpm else 0
        return SkipTail(track, start, render_tail(style, track.audio, start, frames, beat))

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
        watcher = station_thread(
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
        watcher = station_thread(
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
        with self._clock_selection_lock:
            return self._next_rendered_serial()

    def _next_rendered_serial(self) -> RenderedTrack | None:
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
                if self._library is not None and self.bus is not None and self.bus.playing:
                    current = self._random_start_entry()
                    if current is not None:
                        self._set_render_cursor(
                            current,
                            0,
                            "seed",
                            clock_cursor=getattr(self, "_seed_clock_cursor", None),
                            clock_name=self._seed_clock_name,
                            clock_step=self._seed_clock_step,
                            clock_revision=self._seed_clock_revision,
                        )
                    self._seed_clock_step = None
                    self._seed_clock_revision = None
                if current is None:
                    return None
            offset, pick_mode = self._pending_offset, self._pending_pick_mode
            clock_step = getattr(self, "_pending_clock_step", None)
            current_from_queue = self._pending_from_queue
            previous = self._pending_previous
            mixed_in_fx, mixed_in_ratio = self._pending_mixed_in
            glide = self._pending_glide
            reserved_liners = self._pending_reserved_liners
            station_mode = (
                self._library is not None and self._cfg.playback.transition_mode == "saved_points"
            )
            pick_signature = self._pick_signature()
            liners: tuple[tuple[IndexEntry, bool], ...] = ()
            next_cursor, liner_key = None, ""
            next_clock_step = None
            context = self._pick_context(
                pending=current,
                starting=previous,
                peek_queue=True,
                queue_reserved=current if current_from_queue else None,
                at=self._handoff_time(current),
            )
            next_entry: IndexEntry | None
            self._forget_other_clock_place()
            clock_name = self._pending_clock_name
            clock_revision = self._pending_clock_cursor_revision
            clock_cursor = self._pending_clock_cursor
            clock_boundary = self._pending_clock_boundary or getattr(
                self, "_clock_next_boundary", None
            )
            next_clock_name = clock_name
            next_clock_revision = clock_revision
            next_clock_boundary = clock_boundary
            next_cursor, liner_key = clock_cursor, ""
            next_clock_step = None
            plan = self._claim_slot_plan(current) if station_mode else None
            breaks: tuple[Break, ...] = ()
            unbooked_ad_steps: tuple[dict, ...] = ()
            unbooked_slot_id = ""
            # An intro matched to the next song (#127) takes the automatic
            # liner's place at this change; the liner trigger stays due.
            intro = None
            prepared = self._prepared_plan()
            try:
                if plan is not None:
                    unbooked_ad_steps, unbooked_slot_id = plan.ad_steps, plan.slot_id
                    next_entry, next_mode, liners, next_cursor = (
                        plan.upcoming,
                        plan.mode,
                        plan.liners,
                        plan.next_cursor,
                    )
                    next_clock_step = plan.clock_step
                    next_clock_revision = getattr(plan, "clock_revision", None)
                    next_clock_name = plan.clock_name
                    next_clock_boundary = plan.boundary_after
                    breaks = self._prepared_breaks(
                        current, next_entry, plan=prepared
                    ) + self._take_breaks(plan)
                    liners, liner_key, intro = self._transition_voice(
                        current, next_entry, liners, breaks, context.at
                    )
                elif station_mode:
                    next_entry, next_mode, liners, next_cursor, liner_key = (
                        self._choose_station_next(current, context, auto_liner=False)
                    )
                    next_clock_step = self._chosen_clock_step
                    unbooked_ad_steps = getattr(self, "_chosen_clock_ad_steps", ())
                    if next_mode == "clock":
                        next_clock_name = self._pending_clock_name
                        next_clock_revision = self._chosen_clock_revision
                        next_clock_boundary = self._pending_clock_boundary
                    breaks = self._prepared_breaks(current, next_entry, plan=prepared)
                    liners, liner_key, intro = self._transition_voice(
                        current, next_entry, liners, breaks, context.at
                    )
                else:
                    next_entry, next_mode = self._choose_next(current, context)
                    unbooked_ad_steps = getattr(self, "_chosen_clock_ad_steps", ())
                    breaks = self._prepared_breaks(current, next_entry, plan=prepared)
                    if breaks:
                        liners, liner_key, intro = self._transition_voice(
                            current, next_entry, liners, breaks, context.at
                        )
            except Exception as exc:
                logger.warning(
                    "No track can follow %s (%s); it plays out alone.", current.path, exc
                )
                next_entry, next_mode = None, "similarity"
            try:
                if next_entry is not None:
                    # Plan the effect knowing how next_entry was picked;
                    # the render then finds this plan.
                    self.planned_transition(current, next_entry, next_mode)
                voice: dict[str, Any] = {"intro": intro} if intro is not None else {}
                if prepared is not None:
                    voice["prepared_plan"] = prepared
                if breaks:
                    rendered = self._render_track(
                        current,
                        next_entry,
                        offset,
                        glide_in=glide,
                        liners=liners,
                        breaks=breaks,
                        **voice,
                    )
                elif liners:
                    rendered = self._render_track(
                        current, next_entry, offset, glide_in=glide, liners=liners, **voice
                    )
                else:
                    rendered = self._render_track(
                        current, next_entry, offset, glide_in=glide, **voice
                    )
            except Exception:
                logger.exception("Rendering %s failed; skipping it.", current.path)
                rendered = None
            if rendered is not None and next_entry is not None and unbooked_ad_steps:
                self._miss_unbooked_ad_steps(unbooked_ad_steps, unbooked_slot_id)
            if intro is not None and (
                rendered is None
                or intro.intro_id not in {event[1] for event in rendered.intro_events}
            ):
                self.matched_intros.release((intro.intro_id,))
            if breaks and self.break_desk is not None:
                mixed = {event[1] for event in rendered.break_events} if rendered else set()
                for item in breaks:
                    if item.break_id not in mixed:
                        self.break_desk.drop(item.break_id, "its song change could not be rendered")
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
                mixed_in=_mixed_in_by(rendered),
                glide=rendered.next_glide if rendered else None,
                reserved_liners=rendered.next_reserved_liners if rendered else (),
                clock_cursor=next_cursor,
                clock_boundary=next_clock_boundary,
                clock_name=next_clock_name,
                clock_step=next_clock_step,
                clock_revision=next_clock_revision,
            )
            if rendered is not None:
                committed = getattr(self, "prepared_render_committed", None)
                if committed is not None:
                    committed(current, next_entry)
                return replace(
                    rendered,
                    start_offset=rendered.start_offset
                    if (
                        station_mode
                        or breaks
                        or getattr(current, "_prepared_clip", False)
                        or getattr(next_entry, "_prepared_clip", False)
                    )
                    else offset,
                    pick_mode=pick_mode,
                    from_queue=current_from_queue,
                    next_pick_mode=next_mode,
                    next_pick_signature=pick_signature,
                    next_from_queue=next_mode == "queue",
                    set_generation=generation,
                    previous_entry=previous,
                    mixed_in_fx=mixed_in_fx,
                    mixed_in_ratio=mixed_in_ratio,
                    glide_in=glide,
                    reserved_liners=reserved_liners,
                    clock_cursor=clock_cursor,
                    next_clock_cursor=next_cursor,
                    clock_step=clock_step,
                    next_clock_step=next_clock_step,
                    clock_name=clock_name,
                    clock_revision=clock_revision,
                    next_clock_name=next_clock_name,
                    next_clock_revision=next_clock_revision,
                    clock_boundary=clock_boundary,
                    next_clock_boundary=next_clock_boundary,
                    liner_plan_key=liner_key,
                )
        return None

    def _set_render_cursor(
        self,
        entry: IndexEntry | None,
        offset: int,
        pick_mode: str,
        from_queue: bool = False,
        previous: IndexEntry | None = None,
        mixed_in: tuple[str, float] = ("", 1.0),
        glide: Glide | None = None,
        reserved_liners: tuple[IndexEntry, ...] = (),
        clock_cursor: int | None = None,
        clock_boundary: float | None = None,
        clock_name: str | None = None,
        clock_step: dict | None = None,
        clock_revision: int | None = None,
    ) -> None:
        """Point the render-ahead cursor at *entry* (worker idle or queued).

        *previous* is the track rendered just before *entry*; ``None`` after
        a jump, where it is not known.  *mixed_in* is the effect and
        stretch *entry* comes in with (see ``RenderedTrack.mixed_in_fx``);
        the default is a track that starts on its own.  *glide* is how
        *entry* returns to its own tempo after a beatmatched overlap (see
        ``RenderedTrack.next_glide``).  *clock_name* is the clockwheel
        *clock_cursor* is a place in.
        """
        self._cursor_serial += 1
        self._pending_entry = entry
        self._pending_offset = offset
        self._pending_pick_mode = pick_mode
        self._pending_from_queue = from_queue
        self._pending_previous = previous
        self._pending_mixed_in = mixed_in
        self._pending_glide = glide
        self._pending_reserved_liners = reserved_liners
        self._pending_clock_cursor = clock_cursor
        self._pending_clock_name = clock_name
        self._pending_clock_cursor_revision = clock_revision
        self._pending_clock_step = clock_step
        self._pending_clock_boundary = clock_boundary

    def _rewind_render_cursor(self, track: RenderedTrack) -> None:
        """Point the cursor back at *track*'s own start (to re-render it).

        The track before it is the one it was rendered after, so the
        re-render's pick still excludes the track the bus is starting.
        """
        if track.liner_plan_key and self.liner_scheduler is not None:
            self.liner_scheduler.release_plan(track.liner_plan_key)
        self._release_intros(track)
        self._set_render_cursor(
            track.entry,
            track.start_offset,
            track.pick_mode,
            track.from_queue,
            previous=track.previous_entry,
            mixed_in=(track.mixed_in_fx, track.mixed_in_ratio),
            glide=track.glide_in,
            reserved_liners=track.reserved_liners,
            clock_cursor=track.clock_cursor,
            clock_boundary=track.clock_boundary,
            clock_name=track.clock_name,
            clock_step=track.clock_step,
            clock_revision=track.clock_revision,
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
            mixed_in=_mixed_in_by(track),
            glide=track.next_glide,
            reserved_liners=track.next_reserved_liners,
            clock_cursor=track.next_clock_cursor,
            clock_boundary=track.next_clock_boundary,
            clock_name=track.next_clock_name,
            clock_step=track.next_clock_step,
            clock_revision=track.next_clock_revision,
        )

    # ------------------------------------------------------------------
    # Announcer breaks (#88): the open song change, planned a song ahead
    # ------------------------------------------------------------------

    @property
    def break_desk(self) -> Any:
        """This station's announcer breaks (a BreakDesk, #88), or ``None``."""
        return self._break_desk

    @break_desk.setter
    def break_desk(self, desk: Any) -> None:
        self._break_desk = desk
        if desk is not None:
            # A slot whose last waiting break went is unpinned again.
            desk.on_slot_empty = self._unpin_slot

    def _prepared_breaks(
        self,
        current: IndexEntry,
        upcoming: IndexEntry | None,
        *,
        plan: Any = None,
    ) -> tuple[Break, ...]:
        callback = getattr(self, "prepared_handoff", None)
        if callback is None or upcoming is None:
            return ()
        try:
            return (
                callback(current, upcoming, plan=plan)
                if plan is not None
                else callback(current, upcoming)
            )
        except (OSError, ValueError):
            logger.warning("Prepared speech is unavailable; music continues", exc_info=True)
            return ()

    def _prepared_plan(self) -> Any:
        callback = getattr(self, "prepared_plan", None)
        try:
            return callback() if callback is not None else None
        except (OSError, ValueError):
            logger.warning("Prepared speech is unavailable; music continues", exc_info=True)
            return None

    def _unpin_slot(self, slot_id: str) -> None:
        """Let the queue move the open slot again once no break waits in it."""
        desk = self.break_desk
        with self._slot_lock:
            plan = self._slot_plan
            if (
                plan is not None
                and plan.slot_id == slot_id
                and plan.pinned
                and (desk is None or desk.waiting_in(slot_id) == 0)
            ):
                plan.pinned = False
                logger.info("The open song change has no breaks left; the queue may change it")

    def _discard_plan(self, plan: SlotPlan, reason: str) -> None:
        """Throw *plan* away on the worker: undo its clock move and drop its breaks."""
        if plan.serial == self._cursor_serial:
            self._pending_clock_boundary = plan.boundary_before
        if plan.pinned and self.break_desk is not None:
            self.break_desk.drop_slot(plan.slot_id, reason)

    def _discard_render(self, track: RenderedTrack) -> None:
        """A render was thrown away unplayed: its breaks and matched intro will not air.

        Only a reset, a jump or a stop throws away a render that carries
        breaks, and those end the slot too.  Such a render is never judged
        stale by a queue edit (:meth:`_queue_pick_is_stale`), so it never
        becomes a fallback replaced by a re-render that could take the
        breaks again.
        """
        desk = self.break_desk
        if desk is not None:
            for _at, break_id, _end in track.break_events:
                desk.drop(break_id, "its render was thrown away before it played")
        self._release_intros(track)

    def _breaks_on(self) -> bool:
        """Whether this station plans song changes for breaks.

        Only saved-point playback with a library can: breaks are mixed into
        its handoffs.  With breaks off nothing is planned early, so songs
        are picked exactly as before.
        """
        desk = self.break_desk
        return (
            desk is not None
            and (desk.plans_slots or self._voice_tracking_requested)
            and self._library is not None
            and self._cfg.playback.transition_mode == "saved_points"
        )

    def request_voice_track_slot(self) -> bool:
        """Start planning slots on demand for the server voice tracker."""
        if (
            self._dry_run
            or self._library is None
            or self._cfg.playback.transition_mode != "saved_points"
            or self.break_desk is None
        ):
            return False
        self._voice_tracking_requested = True
        self._render_ahead.request_plan()
        return True

    def _playing_remaining_s(self) -> float:
        """Seconds left of the playing render, overlap included."""
        playing = self._playing_render
        if playing is None:
            return 0.0
        return max(0.0, (len(playing.audio) - self._playback_output_pos[0]) / SAMPLE_RATE)

    def _current_song_remaining_s(self) -> float:
        """Estimated seconds until the current song ends, for scheduled events."""
        if self._playing_render is not None:
            return self._playing_remaining_s()
        current = self._state.current_track
        started = getattr(self, "_browser_started", None)
        if current is None or started is None or started[0] is not current:
            return float("inf")
        return max(0.0, float(current.length) - (self._now() - started[1]))

    def _plan_slot(self, ready: RenderedTrack) -> None:
        with self._clock_selection_lock:
            self._plan_slot_serial(ready)

    def _plan_slot_serial(self, ready: RenderedTrack) -> None:
        """Plan the song change after *ready*: pick what follows its next track.

        The render-ahead worker's plan hook, run once *ready* waits for the
        bus.  The pick is the one the next render would make, made a song
        early with *ready* treated as started; the next render takes it
        (:meth:`_claim_slot_plan`).  A plan a break is booked on survives
        a re-render of *ready* that keeps the same ending song; otherwise
        its breaks are dropped.
        """
        if not self._breaks_on():
            return
        current = self._pending_entry
        if (
            current is None
            or ready.next_entry is not current
            or ready.set_generation != self._set_generation
        ):
            return
        serial = self._cursor_serial
        with self._slot_lock:
            old = self._slot_plan
            if old is not None and old.serial == serial and not old.stale:
                return
            if old is not None and old.pinned and old.ending.path == current.path:
                self._slot_plan = replace(
                    old,
                    serial=serial,
                    ending=current,
                    before=ready.entry,
                    before_s=len(ready.audio) / SAMPLE_RATE,
                    ending_offset_s=self._pending_offset / SAMPLE_RATE,
                )
                return
            self._slot_plan = None
        if old is not None:
            # Planning again from the same cursor undoes what the old pick moved.
            self._discard_plan(old, "the song before it changed")
        also_reserved = (*((ready.entry,) if ready.from_queue else ()), *ready.reserved_liners)
        queue_reserved = current if self._pending_from_queue else None
        # The song after *current* starts once what plays now, *ready* and
        # *current* have played: its hours and play dates are judged then.
        lead_s = self._playing_remaining_s() + len(ready.audio) / SAMPLE_RATE
        context = self._pick_context(
            pending=current,
            starting=ready.entry,
            peek_queue=True,
            queue_reserved=queue_reserved,
            at=self._handoff_time(current, lead_s=lead_s),
        )
        boundary_before = self._pending_clock_boundary
        reserved = (*self._pending_reserved_liners, *also_reserved)
        try:
            upcoming, mode, liners, cursor, _key = self._choose_station_next(
                current,
                context,
                also_reserved=also_reserved,
                lead_s=lead_s,
                auto_liner=False,
            )
        except Exception as exc:
            self._pending_clock_boundary = boundary_before
            logger.warning("Could not plan the song change after %s: %s", current.path, exc)
            return
        plan = SlotPlan(
            slot_id=new_id(),
            serial=serial,
            before=ready.entry,
            before_s=len(ready.audio) / SAMPLE_RATE,
            ending=current,
            upcoming=upcoming,
            mode=mode,
            liners=liners,
            next_cursor=cursor,
            boundary_before=boundary_before,
            boundary_after=self._pending_clock_boundary,
            queue_reserved=queue_reserved,
            reserved=reserved,
            ending_offset_s=self._pending_offset / SAMPLE_RATE,
            clock_name=self._pending_clock_name,
            clock_revision=getattr(self, "_chosen_clock_revision", self._clock_revision),
            clock_step=getattr(self, "_chosen_clock_step", None),
            ad_steps=self._chosen_clock_ad_steps if mode == "clock" else (),
        )
        with self._slot_lock:
            self._slot_plan = plan
        logger.info(
            "Open song change for breaks: %s into %s",
            current.display_name,
            upcoming.display_name,
        )

    def _claim_slot_plan(self, current: IndexEntry) -> SlotPlan | None:
        """Take the plan for *current*'s song change, if it still holds (worker thread).

        Only the render the plan was made for takes it: the cursor has not
        moved since.  Any other render (a re-render of the track before,
        after a queue edit) leaves it for :meth:`_plan_slot`, which keeps
        it or drops its breaks once that render is done.
        """
        with self._slot_lock:
            plan = self._slot_plan
            if plan is None or plan.serial != self._cursor_serial:
                # Not this render's plan: the cursor that plan's pick moved has
                # been replaced, so there is nothing of it to undo here.
                return None
            self._slot_plan = None
        if plan.clock_name != self._pending_clock_name or (
            plan.mode == "clock" and plan.clock_revision != self._clock_revision
        ):
            # Another clockwheel was selected (#133): the plan's place and
            # resync time belong to the old one, which the cursor already
            # forgot, so only its breaks go.
            if plan.pinned and self.break_desk is not None:
                self.break_desk.drop_slot(plan.slot_id, "the clockwheel changed")
            return None
        if plan.ending is not current:  # pragma: no cover -- the serial says it is
            self._discard_plan(plan, "the station's next songs changed")
            return None
        if plan.stale and not plan.pinned:
            self._discard_plan(plan, "")
            return None
        return plan

    def _take_breaks(self, plan: SlotPlan) -> tuple[Break, ...]:
        """The breaks booked for *plan*'s song change, as it is rendered."""
        desk = self.break_desk
        if desk is None or not plan.pinned:
            return ()
        playing = self._playing_render
        if playing is not None and playing.entry is plan.before:
            lead = self._playing_remaining_s()
        else:
            lead = self._playing_remaining_s() + plan.before_s
        start, segue = self._clock_music_span(plan.ending)
        lead += max(0.0, segue - max(start, plan.ending_offset_s)) / max(
            self.music_playback_rate(plan.ending), 1e-9
        )
        return tuple(desk.take(plan.slot_id, time.time() + lead))

    def _pinned_before(self, track: RenderedTrack) -> bool:
        """Whether a booked break pins *track*'s next track (it then stays)."""
        with self._slot_lock:
            plan = self._slot_plan
            return (
                plan is not None
                and plan.pinned
                and track.next_entry is not None
                and track.next_entry is plan.ending
            )

    def _recheck_slot_plan(self) -> None:
        """After a queue edit, plan again if the edit changed an unpinned plan's pick."""
        with self._slot_lock:
            plan = self._slot_plan
        if plan is None or plan.pinned or plan.stale or self._library is None:
            return
        liners, head = self._station_queue(plan.queue_reserved, plan.reserved)
        queued = tuple(entry.path for entry, from_queue in plan.liners if from_queue)
        if tuple(entry.path for entry in liners) != queued:
            stale = True
        elif plan.mode == "queue":
            stale = head is None or head.path != plan.upcoming.path
        else:
            stale = head is not None
        if not stale:
            return
        with self._slot_lock:
            if self._slot_plan is not plan or plan.pinned:
                return
            plan.stale = True
        self._render_ahead.request_plan()

    def _settle_breaks(self, finished: RenderedTrack | None) -> None:
        """Account for the breaks of a render the bus has left (mix-bus thread).

        A break whose speech ended within the last half second counts as
        aired: the bus reports positions a block at a time.
        """
        desk = self.break_desk
        if finished is None or not finished.break_events or desk is None:
            return
        reached = self._playback_output_pos[0]
        for _at, break_id, end in finished.break_events:
            if break_id in self._heard_break_ends:
                continue
            if reached >= end - SAMPLE_RATE // 2:
                desk.aired(break_id)
            elif break_id in self._heard_break_starts:
                desk.drop(break_id, "cut short: the song was skipped")
            else:
                desk.drop(break_id, "skipped before it aired")

    def slot_status(self) -> tuple[SlotPlan | None, float, float]:
        """The open song change, when its slot closes and when it airs.

        Returns ``(plan, closes_in_s, airs_in_s)``: a copy of the plan, or
        ``None`` when no change is open (nothing playing, breaks off, or
        the next song is still being rendered); the seconds until its
        change is rendered (zero while it is being rendered); and the
        seconds until the change itself, roughly.
        """
        with self._slot_lock:
            plan = self._slot_plan
            if plan is not None and plan.serial != self._cursor_serial:
                plan = None  # the cursor moved; the plan is decided after the render
            plan = replace(plan) if plan is not None else None
        if plan is None or (plan.stale and not plan.pinned):
            if plan is None and self._breaks_on():
                self._render_ahead.request_plan()
            return None, 0.0, 0.0
        remaining = self._playing_remaining_s()
        start, segue = self._clock_music_span(plan.ending)
        body = max(0.0, segue - max(start, plan.ending_offset_s)) / max(
            self.music_playback_rate(plan.ending), 1e-9
        )
        playing = self._playing_render
        if playing is not None and playing.entry is plan.before:
            return plan, 0.0, remaining + body
        return plan, remaining, remaining + plan.before_s + body

    def book_break(self, item: Break) -> SlotPlan:
        """Book *item* for the open song change and pin its pair (an API thread).

        Raises:
            BreakError: 409 when *item*'s slot is no longer the open one,
                or the desk has no room (see
                :meth:`~autodj.breaks.BreakDesk.reserve`).
        """
        desk = self.break_desk
        if desk is None:
            raise BreakError(409, "Breaks are off for this station", "disabled")
        with self._slot_lock:
            plan = self._slot_plan
            if (
                plan is None
                or plan.slot_id != item.slot_id
                or plan.serial != self._cursor_serial
                or (plan.stale and not plan.pinned)
            ):
                raise BreakError(409, "That song change is no longer open", "slot_closed")
            desk.add(item)
            plan.pinned = True
            return replace(plan)

    def book_breaks(self, items: Sequence[Break], slot_id: str) -> SlotPlan:
        """Book *items* together for the open song change *slot_id* and pin its pair.

        All of them or none: the ad schedule books a whole ad break (#51).

        Raises:
            BreakError: 409 when *slot_id* is no longer the open one.
        """
        desk = self.break_desk
        if desk is None:
            raise BreakError(409, "Breaks are off for this station", "disabled")
        with self._slot_lock:
            plan = self._slot_plan
            if (
                plan is None
                or plan.slot_id != slot_id
                or plan.serial != self._cursor_serial
                or (plan.stale and not plan.pinned)
            ):
                raise BreakError(409, "That song change is no longer open", "slot_closed")
            added: list[Break] = []
            failed: BreakError | None = None
            for item in items:
                try:
                    desk.add(item)
                except BreakError as exc:
                    failed = exc
                    break
                added.append(item)
            if failed is None and added:
                plan.pinned = True
            booked = replace(plan)
        if failed is not None:
            # Dropped with the slot lock released: the desk may unpin the slot.
            for item in added:
                desk.drop(item.break_id, "the rest of its ad break could not be booked")
            raise failed
        return booked

    def reset_render_ahead(
        self,
        entry: IndexEntry | None,
        offset: int = 0,
        pick_mode: str = "queue",
        *,
        reason: str = "the station jumped to another song",
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
            reason: Why breaks waiting to air are dropped (#88): every
                planned song change is gone.
        """
        with self._slot_lock:
            self._slot_plan = None
        if self.break_desk is not None:
            self.break_desk.drop_unaired(reason)
        with self._state.queue_lock:
            self._skip_next_intro = False
            cursor = getattr(self, "_seed_clock_cursor", None)
            self._seed_clock_cursor = None
            clock = self._seed_clock_name
            clock_revision = self._seed_clock_revision
            clock_step = self._seed_clock_step
            self._seed_clock_step = None
            self._seed_clock_revision = None
            self._render_ahead.reset(
                lambda: self._set_render_cursor(
                    entry,
                    offset,
                    pick_mode,
                    clock_cursor=cursor,
                    clock_name=clock,
                    clock_step=clock_step,
                    clock_revision=clock_revision,
                )
            )
        if self.liner_scheduler is not None and hasattr(self.liner_scheduler, "release_plan"):
            self.liner_scheduler.release_plan()

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
        self.reset_render_ahead(entry, 0, pick_mode, reason="a new set started")

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
            self._settle_intros(self._playing_render)
            self._playing_render = None
            self._state.current_track = None
            self._state.next_track = None
            self._last_clock_step = None
            self._playback_pos[0] = 0
            self._playback_output_pos[0] = 0
            self._playback_len = 0
            self._current_lyrics = []
            self._current_lyrics_plain = ""
        self.reset_render_ahead(None, reason="the station stopped")
        return cut_short

    @staticmethod
    def prepared_tail_boundary(track: RenderedTrack) -> int:
        """First potentially affected frame, including saved-point duck ramps."""
        from autodj.playout import DUCK_RAMP_SECONDS

        if getattr(track.entry, "_prepared_clip", False):
            return 0
        boundaries = [len(track.audio)]
        if track.overlap_frames:
            boundaries.append(len(track.audio) - track.overlap_frames)
        boundaries.extend(
            at - round(DUCK_RAMP_SECONDS * SAMPLE_RATE)
            for at, _identity, _end in track.break_events
        )
        return max(0, min(boundaries))

    def refresh_prepared_tail(self, prepared_plan: Any = None) -> None:
        """Replace a prepared future handoff; decoding never runs on the bus thread.

        Unlike ordinary queue refresh, an invalid prepared render is never kept
        as a fallback. A voice that crossed its commit boundary keeps playing.
        """
        if prepared_plan is None:
            prepared_plan = self._prepared_plan()
        with self._clock_selection_lock:
            track = self._playing_render
            if track is None or self.bus is None:
                self._render_ahead.invalidate_prepared()
                return
            boundary = self.prepared_tail_boundary(track)
            if not self.bus.can_replace_unheard_tail(track, before_frame=boundary):
                committed_notice = getattr(self, "prepared_audio_committed", None)
                if committed_notice is not None:
                    committed_notice()
                # Following audio still must be rebuilt without invalid speech.
                self._render_ahead.reset(lambda: self._skip_past_render(track))
                return
            token_for = getattr(self, "prepared_render_token", None)
            token = token_for(prepared_plan) if token_for is not None else ""
            next_entry = track.next_entry
            if not self._prepared_primary_valid(next_entry, plan=prepared_plan):
                with self._state.queue_lock:
                    next_entry = self._peek_user_queue(track.entry)
            breaks = self._prepared_breaks(track.entry, next_entry, plan=prepared_plan)
            if self.break_desk is not None:
                ordinary = tuple(
                    item
                    for _at, identity, _end in track.break_events
                    if (item := self.break_desk.get(identity)) is not None
                    and item.source != "prepared_edition"
                )
                breaks += ordinary
            rendered = self._render_track(
                track.entry,
                next_entry,
                track.start_offset,
                glide_in=track.glide_in,
                liners=tuple((entry, queued) for _at, entry, queued in track.liner_events),
                breaks=breaks,
                intro=track.incoming_intro,
                prepared_plan=prepared_plan,
            )
            if rendered is None or rendered.playback_rate != track.playback_rate:
                self._render_ahead.reset(lambda: self._skip_past_render(track))
                return
            boundary = min(boundary, self.prepared_tail_boundary(rendered), len(rendered.audio))
            from autodj.stereo import audio_buffer

            owned = audio_buffer(len(rendered.audio))
            for first in range(0, len(owned), 65536):
                owned[first : first + 65536] = rendered.audio[first : first + 65536]
            for first in range(0, boundary, 65536):
                end = min(boundary, first + 65536)
                owned[first:end] = track.audio[first:end]
            replacement = replace(
                track,
                audio=owned,
                next_entry=rendered.next_entry,
                next_start_offset=rendered.next_start_offset,
                next_glide=rendered.next_glide,
                overlap_frames=rendered.overlap_frames,
                next_entry_offset=rendered.next_entry_offset,
                break_events=rendered.break_events,
                liner_events=rendered.liner_events,
                intro_events=rendered.intro_events,
                full_audio=rendered.full_audio,
                transition_fx=rendered.transition_fx,
                beatmatch_ratio=rendered.beatmatch_ratio,
            )
            guard_for = getattr(self, "prepared_render_guard", None)
            guard = guard_for(token) if guard_for is not None else contextlib.nullcontext(True)
            with guard as unchanged:
                if not unchanged:
                    requested = getattr(self, "request_prepared_refresh", None)
                    if requested is not None:
                        requested()
                    return
                with self._set_lock:
                    swapped = self._playing_render is track and self.bus.replace_unheard_tail(
                        track, replacement, before_frame=boundary
                    )
                    if swapped:
                        self._playing_render = replacement
                        self._state.next_track = replacement.next_entry
            self._render_ahead.reset(
                lambda: self._skip_past_render(replacement if swapped else track)
            )
            if not swapped:
                committed_notice = getattr(self, "prepared_audio_committed", None)
                if committed_notice is not None:
                    committed_notice()

    def refresh_render_ahead(self) -> None:
        """Re-check the rendered next track after a queue or picking edit.

        Only a render whose pick the edit changed is re-rendered
        (see :meth:`_queue_pick_is_stale`), and the old render stays
        playable until its replacement is ready, so an edit never causes
        dead air; if the bus needs it first, the edit lands a track later.
        Call after editing the queue or song-picking settings.
        """
        # Queue-edit callers hold queue_lock. Only enqueue a coalesced control
        # task here; validation/rendering must never run while that lock is held.
        requested = getattr(self, "request_prepared_refresh", None)
        if requested is not None:
            requested()
        with self._state.queue_lock:
            self._render_ahead.refresh()
        self._recheck_slot_plan()

    def prepare_live_return(self, timeout: float) -> None:
        """Render the track after a live DJ's set again, for now (#48).

        The track rendered to follow the song a live DJ interrupted waited
        through the set; its liners and time checks were planned for
        then.  It is rendered again from its own start, the old render
        kept to fall back on, and this waits up to *timeout* seconds for
        the new one.  The clockwheel place, the queue and the planned song
        change are those it had: nothing advanced while the DJ was on.
        """
        self._render_ahead.rerender()
        if not self._render_ahead.wait_rendered(timeout):
            logger.info("Automation returns with the song readied before the live set")

    def play_now(self, entry: IndexEntry, pick_mode: str = "queue") -> None:
        """Fade out the playing track and play *entry* next, from its start.

        Queued tracks stay queued (renders only peek at the queue), so the
        track that was coming up plays after *entry*.  If *entry* is not
        rendered by the time the fade ends, the bus plays silence briefly
        until it is -- acceptable for an explicit jump.
        """
        self.reset_render_ahead(entry, 0, pick_mode, reason="a song was played at once")
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
            if track is not None and self._skip_next_intro:
                self._skip_next_intro = False
                if track.full_audio is not None:
                    prefix = _music_output_frame(
                        track.start_offset - track.source_start_offset, track.playback_rate
                    )
                    track = replace(
                        track,
                        audio=track.full_audio,
                        start_offset=track.source_start_offset,
                        liner_events=tuple(
                            (at + prefix, entry, queued) for at, entry, queued in track.liner_events
                        ),
                        break_events=tuple(
                            (at + prefix, break_id, end + prefix)
                            for at, break_id, end in track.break_events
                        ),
                        intro_events=tuple(
                            (at + prefix, intro_id, end + prefix, label)
                            for at, intro_id, end, label in track.intro_events
                        ),
                    )
            if track is not None and track.from_queue and self._commit_queue_pick(track.entry):
                # Given back if its set stops before it starts.
                self._taken_queue_pick = track
        return track

    def skip_current(self) -> None:
        """An early skip starts the next song at Start, before any unheard overlap."""
        track = self._playing_render
        if track is not None and track.full_audio is not None:
            relative = self._playback_output_pos[0]
            self._skip_next_intro = relative < len(track.audio) - track.overlap_frames
        if self.bus is not None:
            self.bus.skip()

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

    def _pick_signature(self) -> tuple[str, str, bool, int | None, int | None, bool | None]:
        clock_mode = self._cfg.playback.pick_mode == "clock"
        return (
            repr(self._cfg.playback),
            repr(getattr(self, "_preset", None)),
            self._state.discovery_enabled,
            getattr(self, "_discovery_every", None),
            self._clock_revision if clock_mode else None,
            self._clock is not None if clock_mode else None,
        )

    def _queue_pick_is_stale(self, track: RenderedTrack) -> bool:
        """Whether a queue or picking edit changed what follows *track*.

        A render that took its successor from the queue is stale when the
        queue head (skipping *track*'s own slot) is now something else or
        gone; a render that picked by similarity is stale once the queue
        has anything, because the queue takes priority.  A render with
        announcer breaks is never stale: the breaks name its next track.
        """
        if track.next_pick_mode == "clock" and track.next_clock_revision != self._clock_revision:
            return True
        # A booked break has already committed its next track.
        if track.break_events or self._pinned_before(track):
            return False
        if (
            track.next_pick_signature is not None
            and not track.next_from_queue
            and track.next_pick_signature != self._pick_signature()
        ):
            return True
        if track.playback_rate != self.music_playback_rate(track.entry):
            return True
        if (
            track.overlap_frames > 0
            and track.next_entry is not None
            and track.next_playback_rate != self.music_playback_rate(track.next_entry)
        ):
            return True
        saved_points = (
            self._library is not None and self._cfg.playback.transition_mode == "saved_points"
        )
        if saved_points:
            liners, head = self._station_queue(
                track.entry if track.from_queue else None, track.reserved_liners
            )
            if tuple(e.path for e in liners) != tuple(e.path for e in track.next_reserved_liners):
                return True
            if track.next_from_queue:
                return (
                    head is None or track.next_entry is None or head.path != track.next_entry.path
                )
            return head is not None
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
        finished = self._playing_render
        self._settle_breaks(finished)
        self._settle_intros(finished)
        self._playing_render = rendered
        self._playback_output_pos[0] = 0
        self._heard_liner_events: set[int] = set()
        self._heard_break_starts = set()
        self._heard_break_ends = set()
        self._heard_intros = set()
        self._heard_next_entry = False
        self._active_processing_mode = rendered.loudness_processing
        # A track rendered before another clockwheel was selected carries a
        # place in the old one, which must not move the new one (#133).
        with self._clock_lock:
            if (
                self._clock is not None
                and rendered.clock_cursor is not None
                and rendered.clock_name == self._clock.get("name")
                and rendered.clock_revision == self._clock_revision
            ):
                self._clock = {**self._clock, "cursor": rendered.clock_cursor}
                if (ledger := self._ledger) is not None:
                    ledger.remember_setting("clock", self._clock)
                self._clock_next_boundary = rendered.clock_boundary
        if entry is not self._state.current_track:
            self._previous_track = self._state.current_track
        self._state.current_track = entry
        self._state.next_track = rendered.next_entry
        self._current_sr = SAMPLE_RATE
        self._playback_pos[0] = rendered.start_offset
        self._playback_len = rendered.start_offset + round(
            len(rendered.audio) * rendered.playback_rate
        )
        # How this track came in, from the previous render's overlap: the
        # render's own transition_fx and beatmatch_ratio describe the
        # NEXT track, which its tail mixes in.
        self._last_transition_fx = rendered.mixed_in_fx
        self._beatmatch_ratio = rendered.mixed_in_ratio
        self._last_pick_mode = rendered.pick_mode
        self._last_clock_step = rendered.clock_step
        seed_start = entry is self._seed_awaiting_start
        with self._pick_lock:
            if seed_start:
                self._seed_awaiting_start = None  # run() already recorded it
            else:
                self._state.record_played(entry)
                self._state.track_number += 1

    def _on_position(self, frames: int) -> None:
        """Record output progress and its source-file position."""
        playing = self._playing_render
        self._playback_output_pos[0] = frames
        self._playback_pos[0] = (
            playing.start_offset + round(frames * playing.playback_rate) if playing else frames
        )
        if playing is not None:
            if (
                playing.next_entry is not None
                and playing.overlap_frames > 0
                and frames > len(playing.audio) - playing.overlap_frames
                and not self._heard_next_entry
            ):
                self._heard_next_entry = True
                if (ledger := self._ledger) is not None:
                    ledger.remember_play(playing.next_entry.path)
                    with self._clock_lock:
                        if (
                            self._clock is not None
                            and playing.next_clock_cursor is not None
                            and (playing.next_clock_name or playing.clock_name)
                            == self._clock.get("name")
                            and playing.next_clock_revision == self._clock_revision
                        ):
                            self._clock = {
                                **self._clock,
                                "cursor": playing.next_clock_cursor,
                            }
                            ledger.remember_setting("clock", self._clock)
                            self._clock_next_boundary = playing.next_clock_boundary
            for index, (at, entry, queued) in enumerate(playing.liner_events):
                if frames > at and index not in self._heard_liner_events:
                    self._heard_liner_events.add(index)
                    if queued:
                        with self._state.queue_lock:
                            self._commit_queue_pick(entry)
                    if (ledger := self._ledger) is not None:
                        ledger.remember_play(entry.path)
                    scheduler = self.liner_scheduler
                    if scheduler is not None and hasattr(scheduler, "mark_planned_fired"):
                        scheduler.mark_planned_fired(playing.liner_plan_key)
            if playing.break_events and (desk := self.break_desk) is not None:
                for at, break_id, end in playing.break_events:
                    if frames > at and break_id not in self._heard_break_starts:
                        self._heard_break_starts.add(break_id)
                        desk.on_air(break_id)
                    if frames >= end and break_id not in self._heard_break_ends:
                        self._heard_break_ends.add(break_id)
                        desk.aired(break_id)
            if playing.intro_events and (intros := self.matched_intros) is not None:
                for at, intro_id, _end, _label in playing.intro_events:
                    if frames > at and intro_id not in self._heard_intros:
                        self._heard_intros.add(intro_id)
                        intros.heard(intro_id)  # never waits

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
                picked = self._choose_next(
                    current, self._pick_context(at=self._handoff_time(current))
                )
            except Exception:
                logger.warning("No track can follow the seed yet", exc_info=True)
        with self._advance_lock:
            if state.current_track is None:
                state.current_track = current
                self.note_browser_start(current)
            seed_playing = state.current_track is current
            if seed_playing:
                if picked is not None and state.next_track is None:
                    state.next_track, self._last_pick_mode = picked
                    self._next_pick = (picked[0].path, picked[1])
                self._current_sr = _DEFAULT_SR
                self._playback_len = int(
                    (current.length if current.length and current.length > 0 else 5.0)
                    * _DEFAULT_SR,
                )
                self._playback_pos[0] = 0
                self._playback_output_pos[0] = 0
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
            if not peek:
                self._last_request_clock_cursor = attached(self, "_request_clock_steps").pop(entry)
        logger.info(source, entry.display_name)
        return entry

    def _pick_context(
        self,
        pending: IndexEntry | None = None,
        *,
        starting: IndexEntry | None = None,
        peek_queue: bool = False,
        queue_reserved: IndexEntry | None = None,
        at: float | None = None,
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
            at: When the picked song is expected to start (see
                :meth:`_handoff_time`), in wall-clock seconds; dayparting
                reads the station's hours then.  Now when not given.
        """
        with self._pick_lock:
            paths, history = self._recent_exclusions(pending, starting)
            rules = self._state.rules
            now = self._state.clock()
            track_number = self._state.track_number
            recorded = self._state.recently_played
            latest = recorded[-1] if recorded else None
        # The daypart and quarter-hour rules judge the hours the song starts
        # in, as dayparting does.
        timed = bool(rules.daypart_days or rules.hourpart_plays)
        repeat, minute_paths = build_filter(
            history,
            rules,
            now,
            self._track_keys(),
            at=(self._now() if at is None else at) if timed else None,
            zone=self._station_zone() if timed else UTC,
        )
        if minute_paths:
            held = set(paths)
            paths = deque([*paths, *(path for path in minute_paths if path not in held)])
        versions = self._song_versions()
        if versions is not None:
            # The track rule holds back every version of a song that played.
            paths = deque(versions.with_versions(paths))
        playing = self._state.current_track
        protected = {e.path for e in (pending, starting, playing) if e is not None}
        if latest is not None:
            protected.add(latest)
        closed, closed_categories = self._dayparts(at)
        warned = getattr(self, "_all_closed_warned", None)
        if warned is not None and warned is not closed and warned != closed:
            self._all_closed_warned = None  # The hours changed: warn again if it recurs.
        return _PickContext(
            paths,
            repeat,
            track_number,
            peek_queue,
            queue_reserved,
            frozenset(protected),
            complement(history, now, self._track_keys()) if self._state.dmca_enabled else None,
            follows.path if (follows := pending or playing) is not None else None,
            closed,
            closed_categories,
            at,
        )

    def _station_now(self, at: float | None = None) -> datetime:
        """The station's local wall time, naive (``[station] timezone``), now or *at*."""
        from autodj.categories import station_wall

        return station_wall(self._cfg, self._now() if at is None else at)

    def _station_zone(self) -> tzinfo:
        """The station's time zone (``[station] timezone``, else the system's)."""
        from zoneinfo import ZoneInfo

        from autodj.scheduler import system_timezone

        return ZoneInfo(self._cfg.station.timezone or system_timezone())

    def _dayparts(self, at: float | None = None) -> tuple[frozenset[str], dict[str, str]]:
        """The songs whose categories are outside their days and hours, and why.

        Read now, or at *at* (the expected start of the song being picked).
        Categories that cannot be read limit nothing; the failure is logged.
        """
        index = self._category_index()
        if index is None:
            return frozenset(), {}
        try:
            closed: tuple[frozenset[str], dict[str, str]] = index.closed_now(self._station_now(at))
        except Exception:
            logger.exception("Dayparting: the rotation categories could not be read")
            return frozenset(), {}
        return closed

    def _recent_exclusions(
        self, pending: IndexEntry | None = None, starting: IndexEntry | None = None
    ) -> tuple[deque, list[PlayRecord]]:
        """Copy the history plus *starting*, then *pending* (hold ``_pick_lock``).

        *starting* is skipped when it is already the latest recorded track
        or is *pending* itself, so it never fills a window slot twice.
        Both count as starting now.

        Returns ``(paths, history)``: the track window's paths and the
        play history, oldest first.
        """
        state = self._state
        recorded = state.recently_played
        if starting is not None and (
            (recorded and recorded[-1] == starting.path)
            or (pending is not None and starting.path == pending.path)
        ):
            starting = None
        added = [entry for entry in (starting, pending) if entry is not None]

        paths = deque(recorded, maxlen=recorded.maxlen)
        paths.extend(entry.path for entry in added)
        now = state.clock()
        history = [*state.play_history, *(PlayRecord.of(entry, now) for entry in added)]
        return paths, history

    def _track_keys(self) -> TrackKeyCache:
        """Each track's repeat-rule keys, with a splitter that knows the library's artists.

        A track's keys are worked out once and every later pick only looks
        them up.  The library hands out a new snapshot whenever its
        catalog changes (each play is written to it), so a new snapshot
        keeps the cache unless its set of artists changed.
        """
        library = getattr(self, "_library", None)
        collection = library if library is not None else getattr(self, "_sim", None)
        entries = collection.entries_snapshot() if collection is not None else ()
        lock = getattr(self, "_key_cache_lock", None) or threading.Lock()
        with lock:
            cached = getattr(self, "_key_cache", None)
            if cached is None or cached[0] is not entries:
                known = frozenset(artist_key(entry.artist) for entry in entries if entry.artist)
                if cached is not None and cached[1].splitter.known == known:
                    cached = (entries, cached[1])
                else:
                    related = (
                        getattr(self, "_related_artists", None) or RelatedArtists()
                        if self._cfg.playback.repeat_rules.use_related_artists
                        else RelatedArtists()
                    )
                    splitter = ArtistSplitter(known, related.single_acts)
                    cached = (entries, TrackKeyCache(splitter))
                self._key_cache = cached
            return cached[1]

    def _song_versions(self) -> SongVersions | None:
        """The library's songs with several versions, with Count versions as one song on.

        ``None`` with the setting off or when no song has a second version.
        Built once per library snapshot, from the cached track keys.
        """
        if not self._state.rules.versions_as_one_song:
            return None
        library = getattr(self, "_library", None)
        collection = library if library is not None else getattr(self, "_sim", None)
        entries = collection.entries_snapshot() if collection is not None else ()
        cached = getattr(self, "_versions_cache", None)
        if cached is None or cached[0] is not entries:
            cached = (entries, SongVersions(entries, self._track_keys()))
            self._versions_cache = cached
        return cached[1] or None

    def _choose(self, pool: Sequence[IndexEntry]) -> IndexEntry:
        """A random entry of *pool*; with Count versions as one song, each song equally likely."""
        versions = self._song_versions()
        if versions is None:
            return self._chooser().choice(pool)  # nosec B311 -- music selection
        return versions.choice(pool, self._chooser())

    def _hour_fit_choice(
        self, context: _PickContext
    ) -> Callable[[Sequence[IndexEntry]], IndexEntry] | None:
        """Return a fitting choice only when the next handoff is near a fixed point.

        Off (and with no fixed point near) it is ``None``, so picking, and
        what it draws from the random generator, is just as without it.
        """
        playback = self._cfg.playback
        # Only a real True turns it on (config validation makes it a bool).
        if playback.hour_fit is not True:
            return None
        from autodj.hour_fit import fit_chooser, next_target
        from autodj.scheduler import system_timezone

        at = self._now() if context.at is None else context.at
        events = self._hour_fit_events
        if callable(events):
            events = events()
        boundary = None
        if playback.pick_mode == "clock":
            boundary = self._pending_clock_boundary or self._clock_next_boundary
        target = next_target(
            at,
            playback.hour_fit_window_minutes,
            self._hour_fit_timezone or self._cfg.station.timezone or system_timezone(),
            boundary,
            events,
        )
        if target is None:
            return None

        def seconds(entry: IndexEntry) -> float:
            try:
                if playback.transition_mode == "saved_points":
                    start, segue = self._clock_music_span(entry)
                    span = segue - start
                else:
                    span = float(entry.length) - playback.crossfade_seconds
            except Exception:
                span = float(entry.length)
            return span / self.music_playback_rate(entry)

        return fit_chooser(target.at - at, seconds, hard=target.hard)

    def voice_track_crosses_hard_boundary(self, starts_at: float, duration: float) -> bool:
        """Check a whole voice chain against clock restarts and hard events."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from autodj.scheduler import next_run, system_timezone

        ends_at = starts_at + duration
        playback = self._cfg.playback
        boundary = (
            self._pending_clock_boundary or self._clock_next_boundary
            if playback.pick_mode == "clock"
            else None
        )
        if boundary is not None and starts_at <= boundary <= ends_at:
            return True
        events = self._hour_fit_events
        if callable(events):
            events = events()
        timezone = self._hour_fit_timezone or self._cfg.station.timezone or system_timezone()
        zone = ZoneInfo(timezone)
        after = datetime.fromtimestamp(max(0.0, starts_at - 0.000001), UTC)
        for event in events or ():
            if not event.enabled or event.timing != "hard":
                continue
            due = next_run(event, after, zone)
            if due is not None and starts_at <= due.timestamp() <= ends_at:
                return True
        return False

    def restore_repeat_history(self, ledger: Any, *, refresh: bool = False) -> int:
        """Load the plays from before a restart into the repeat rules.

        Reads only as many saved plays as the windows can look at (see
        :func:`~autodj.repeat_rules.plays_to_load`).  Plays a set stop cut
        short are not counted, nor are plays dated after now.

        Args:
            ledger: Gives the saved plays (``recent_plays``): the station's
                :class:`~autodj.station_ledger.StationLedger`.

        Returns:
            How many plays were loaded.
        """
        if getattr(self, "_repeat_history_restored", False) and not refresh:
            return 0
        self._repeat_history_restored = True
        limit = plays_to_load(self._state.rules, self._state.dmca_seconds)
        if limit <= 0:
            return 0
        rows = ledger.recent_plays(limit)
        # Plays not written to SQLite yet count too.
        rows.extend(ledger.pending_history())
        records = records_from_plays(rows, time.time())
        if records:
            self._state.restore_history(records)
            logger.info("Repeat rules: %d plays from before the restart count", len(records))
        return len(records)

    def _pick_pure_shuffle(self, current: IndexEntry | None, context: _PickContext) -> IndexEntry:
        """Random pick from tracks not in *context*'s history, honouring the BPM range.

        Silent tracks are never picked.  When every eligible track was
        played recently, the history is relaxed to *current* and the protected
        tracks. Startup passes no *current*, so no arbitrary entry is excluded.
        """
        from autodj.similarity import SimilarityError

        protected = context.protected | ({current.path} if current is not None else set())
        excluded = set(context.recent) | protected

        def eligible(entry: IndexEntry) -> bool:
            if entry.is_silent:
                return False
            if self._bpm_range is None:
                return True
            lo, hi = self._bpm_range
            return entry.bpm > 0 and lo <= entry.bpm <= hi

        entries = self.playable_entries()
        repeat = context.repeat
        versions = self._song_versions()
        ledger = self._ledger
        history = (
            ledger.last_played()
            if self._cfg.playback.pick_mode == "least_recent" and ledger is not None
            else None
        )
        fit = self._hour_fit_choice(context)

        def choose(blocked: set[str] | frozenset[str], use_rules: bool = True) -> IndexEntry | None:
            def allowed(entry: IndexEntry) -> bool:
                return (
                    entry.path not in blocked
                    and entry.path not in context.closed
                    and eligible(entry)
                    and (not use_rules or repeat(entry))
                    and context.allows(entry)
                )

            if history is not None and entries:
                from autodj.clockwheel import least_recent

                return least_recent(
                    entries,
                    history,
                    self._random,
                    allowed=allowed,
                    versions=versions,
                    preferred_path=getattr(self, "_preview_preferred_path", None),
                    fit=fit,
                )
            pool = [entry for entry in entries if allowed(entry)]
            if not pool:
                return None
            if fit is not None:
                from autodj.hour_fit import FIT_CANDIDATES

                if versions is not None:
                    songs: dict[str, list[IndexEntry]] = {}
                    for entry in pool:
                        songs.setdefault(versions.song(entry.path), []).append(entry)
                    names = self._chooser().sample(list(songs), min(FIT_CANDIDATES, len(songs)))
                    candidates = [self._chooser().choice(songs[name]) for name in names]
                else:
                    candidates = self._chooser().sample(pool, min(FIT_CANDIDATES, len(pool)))
                return fit(candidates)
            return self._choose(pool)

        chosen = choose(excluded)
        if chosen is None:
            logger.warning(
                "Pure shuffle exhausted eligible non-recent tracks; relaxing recent-track exclusion"
            )
            chosen = choose(protected)
        if chosen is None and repeat:
            logger.warning(
                "Pure shuffle found no track the repeat rules allow; relaxing %s", RELAXED_RULES
            )
            chosen = choose(protected, use_rules=False)
        if chosen is None:
            raise SimilarityError("No candidates satisfy hard filters for pure shuffle.")
        _log_repeat_blocks(repeat, chosen)
        return chosen

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
            protected = context.protected | {current.path}
            # The search takes these as a set, so the closed songs (often most
            # of a library at night) go in unsorted; only the few others are sorted.
            recent = deque(
                [*context.recent, *sorted(protected - set(context.recent)), *context.closed]
            )
            extra = {"rng": self._random} if self._random is not None else {}
            entry = self._sim.find_distant(
                current.path,
                recent,
                allowed=self._candidate_filter(context),
                fit=self._hour_fit_choice(context),
                **extra,
            )
            _log_repeat_blocks(context.repeat, entry)
            if self.pick_observer is None:
                _CONSOLE.print("  [bold cyan]◈ Discovery track[/bold cyan]")
            return entry
        except SimilarityError:
            return None

    def _resolve_query_path(self, current_path: str) -> tuple[str, str]:
        """Choose the query path and its pick mode (anchored or similarity)."""
        if self._anchor_to_seed and self._seed_path:
            return self._seed_path, "anchored"
        return current_path, "similarity"

    def _pick_next(self, current: IndexEntry, *, at: float | None = None) -> IndexEntry:
        """Select the next track now and show how it was picked.

        The browser-driven path's pick: it pops the user queue and sets
        ``_last_pick_mode``.  Server audio picks through
        :meth:`_next_rendered` instead.

        Args:
            current: The track currently playing.
            at: When the picked track will start, in wall-clock seconds;
                by default when *current* reaches its handoff (see
                :meth:`_browser_handoff`).  Dayparting reads the hours then.

        Returns:
            The recommended next :class:`~autodj.indexer.IndexEntry`.
        """
        when = self._browser_handoff(current) if at is None else at
        entry, mode = self._choose_next(current, self._pick_context(at=when))
        if self._chosen_clock_ad_steps:
            self._miss_unbooked_ad_steps(self._chosen_clock_ad_steps)
        self._last_pick_mode = mode
        self._next_pick = (entry.path, mode)
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
        self._chosen_clock_ad_steps = ()
        queued = self._pop_user_queue(context.peek_queue, context.queue_reserved)
        if queued is not None:
            marked_cursor = (
                attached(self, "_request_clock_steps").get(queued)
                if context.peek_queue
                else getattr(self, "_last_request_clock_cursor", None)
            )
            if marked_cursor is not None and self._cfg.playback.pick_mode == "clock":
                clock = getattr(self, "_clock", None)
                if isinstance(clock, dict) and not context.peek_queue:
                    clock["cursor"] = marked_cursor
            return self._dmca_checked(queued, context, queued=True), "queue"
        try:
            if self._cfg.playback.pick_mode == "clock" and self._clock is None:
                entry, mode = self._clock_fallback_pick(current, context), "fallback"
            else:
                entry, mode = self._choose_by_mode(current, context)
        except Exception as exc:
            return self._dmca_checked(self._fallback_pick(current, context, exc), context), (
                "fallback"
            )
        if self._pick_fallback_active:
            self._pick_fallback_active = False
            logger.info("Picking the next track works again.")
        return self._dmca_checked(entry, context), mode

    def _choose_by_mode(self, current: IndexEntry, context: _PickContext) -> tuple[IndexEntry, str]:
        """Pick by pure shuffle, discovery or similarity (see :meth:`_choose_next`)."""
        if self._pure_shuffle or self._cfg.playback.pick_mode in {"random", "least_recent"}:
            mode = (
                "least_recent" if self._cfg.playback.pick_mode == "least_recent" else "pure_shuffle"
            )
            return self._pick_pure_shuffle(current, context), mode

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
            "repeat_filter": self._candidate_filter(context),
            "pick_top_k": self._cfg.playback.pick_top_k,
            "pick_temperature": self._cfg.playback.pick_temperature,
            "fit": self._hour_fit_choice(context),
        }
        if self._random is not None:
            search["rng"] = self._random
        versions = self._song_versions()
        if versions is not None:
            search["song_of"] = versions.song

        # --- Normal similarity search ---
        try:
            protected = context.protected | {current.path}
            # The search takes these as a set, so the closed songs (often most
            # of a library at night) go in unsorted; only the few others are sorted.
            recent = deque(
                [*context.recent, *sorted(protected - set(context.recent)), *context.closed]
            )
            picked = self._sim.find_next_for_path(recently_played=recent, **search)
            _log_repeat_blocks(context.repeat, picked)
            return picked, mode
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
                recently_played=deque([*sorted(protected), *context.closed]), **search
            )
            return relaxed, mode

    def _fallback_pick(
        self, current: IndexEntry | None, context: _PickContext, reason: Exception
    ) -> IndexEntry:
        """Pick a random track after the normal pick failed with *reason*.

        Ignores the soft filters (harmonic mode, artist and album windows)
        but avoids repeats as far as the library allows: first a track
        outside the recent history, then one that is not protected (see
        :class:`_PickContext`), then anything but *current*, then *current*
        itself (a one-track library). At startup there is no *current*, so
        the fallback can use any audible track. Every step is tried first
        among tracks inside the hard BPM range, the same rule pure shuffle uses;
        only when no track at all is inside the range are the steps tried
        again without it.  Silent tracks are never picked.

        The first fallback of an episode logs a warning, saying when the
        BPM range had to be ignored; later ones log at debug until a
        normal pick succeeds again.

        Raises:
            SimilarityError: The library has no track that is not silent.
        """
        from autodj.similarity import SimilarityError

        everything = [entry for entry in self.playable_entries() if not entry.is_silent]
        # Songs outside their category's hours only when nothing else can play.
        audible = [entry for entry in everything if entry.path not in context.closed] or everything
        if audible is everything and context.closed:
            self._warn_all_closed(
                context.closed,
                f"Dayparting: every song is outside its category's {_closure_words(context)}; "
                "playing one anyway rather than stopping the music",
            )
        bpm_range = self._bpm_range
        in_range = audible
        if bpm_range is not None:
            lo, hi = bpm_range
            in_range = [entry for entry in audible if entry.bpm > 0 and lo <= entry.bpm <= hi]
        current_paths = {current.path} if current is not None else set()
        recent = set(context.recent) | current_paths
        tiers: tuple[AbstractSet[str], ...] = (
            recent,
            context.protected | current_paths,
            current_paths,
            set(),
        )
        chosen: IndexEntry | None = None
        range_ignored = False
        for candidates in (in_range, audible):
            for excluded in tiers:
                pool = [entry for entry in candidates if entry.path not in excluded]
                if pool:
                    chosen = self._choose(pool)
                    break
            if chosen is not None:
                break
            range_ignored = bpm_range is not None
        ignored_note = " The BPM range was ignored: no track is inside it." if range_ignored else ""
        if self._pick_fallback_active:
            logger.debug(
                "Normal pick still failing (%s); picking at random.%s", reason, ignored_note
            )
        else:
            self._pick_fallback_active = True
            logger.warning(
                "Could not pick a track to follow %s (%s: %s); picking one at random "
                "instead until the normal pick works again.%s",
                current.display_name if current is not None else "the automatic start",
                type(reason).__name__,
                reason,
                ignored_note,
                # A reason that was never raised (an empty clock) has no traceback.
                exc_info=not isinstance(reason, SimilarityError)
                and reason.__traceback__ is not None,
            )
        if chosen is None:
            raise SimilarityError("The library has no track that is not silent.")
        return chosen

    # ------------------------------------------------------------------
    # _render_track helpers — broken out so the renderer stays readable.
    # Each helper owns one phase of the crossfade pipeline.
    # ------------------------------------------------------------------

    def _apply_replaygain(self, audio: np.ndarray, path: str) -> np.ndarray:
        """Apply ReplayGain normalisation when enabled in config; else no-op."""
        if (
            self._cfg.playback.loudness_processing in {"off", "smooth", "stereotool"}
            or not self._cfg.replaygain.enabled
        ):
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
        # In place: the decode is fresh, and a copy of a long track is large.
        if audio.dtype == np.float32 and audio.flags.writeable:
            audio *= np.float32(gain)
            return audio
        return (audio * gain).astype(np.float32)

    def _prepared_primary_valid(self, entry: IndexEntry | None, *, plan: Any = None) -> bool:
        if not getattr(entry, "_prepared_clip", False):
            return True
        validate = getattr(self, "prepared_primary_valid", None)
        return validate is None or (
            validate(entry, plan=plan) if plan is not None else validate(entry)
        )

    def _load_playout_audio(self, entry: IndexEntry) -> np.ndarray:
        """Decode originals on the render worker, preserving source cue positions.

        Any length plays: a decode longer than
        :data:`~autodj.stereo.SPILL_FRAMES` is kept in a temporary file
        mapped into memory, not in ordinary memory.
        """
        if getattr(entry, "_prepared_clip", False):
            if self._cfg.playback.loudness_processing in {"smooth", "stereotool"}:
                from autodj.processing import load_and_measure_audio

                audio, _measurement = load_and_measure_audio(Path(entry.path), "voice", 1801)
                return audio
            return load_stereo(entry.path, SAMPLE_RATE, max_seconds=1800)
        if self._cfg.playback.loudness_processing not in {"smooth", "stereotool"}:
            audio = load_stereo(entry.path, SAMPLE_RATE)
            return self._lift_intro(self._apply_replaygain(audio, entry.path), entry)
        from autodj.processing import load_and_measure_audio, load_processed_audio, loudness_matches

        record = self._library.catalog.record_for_path(entry.path) if self._library else None
        kind = record["kind"] if record else "music"
        source = Path(entry.path)
        measurement = record.get("loudness", {}) if record else {}
        # The continuous master handles quiet passages; don't stack an intro-only lift.
        if loudness_matches(measurement, source, kind):
            return load_processed_audio(source, measurement, kind)
        # First play: measure in the same FFmpeg run as the decode.
        audio, measurement = load_and_measure_audio(source, kind)
        if record and not getattr(self, "_dry_run", False):
            self._library.catalog.set_loudness(source, measurement)
        return audio

    # One-slot cache of the incoming track's decode: each render decodes the
    # track it mixes in, and the next render plays that track, so it takes
    # the decode from here instead of running FFmpeg again.
    # ``(path, future of (key, audio))``; see _playout_audio.  Class-level
    # defaults so a Player made without __init__ (tests) still renders.
    _playout_slot: tuple[str, Future[tuple[tuple | None, np.ndarray]]] | None = None
    _playout_lock: threading.Lock = threading.Lock()

    def _playout_key(self, entry: IndexEntry) -> tuple | None:
        """Everything :meth:`_load_playout_audio`'s result depends on, or ``None``.

        The file's size and modification time, the catalog values it reads
        (kind, loudness measurement, intro lift) and the settings it uses.
        ``None`` when the file cannot be read: such a decode is never reused.
        """
        try:
            stat = os.stat(entry.path)
        except OSError:
            return None
        from autodj.processing import loudness_matches

        record = self._library.catalog.record_for_path(entry.path) if self._library else None
        playback, replaygain = self._cfg.playback, self._cfg.replaygain
        kind = record.get("kind", "music") if record else "music"
        measurement = record.get("loudness") if record else None
        measurement_key = (
            repr(measurement) if loudness_matches(measurement, Path(entry.path), kind) else None
        )
        return (
            entry.path,
            stat.st_size,
            stat.st_mtime_ns,
            (kind, record.get("intro_lift_db") if record else None),
            playback.loudness_processing,
            playback.intro_lift_max_db,
            replaygain.enabled,
            replaygain.target_db,
            replaygain.max_clip_safe_gain,
            measurement_key,
        )

    def _prefetch_playout_audio(self, entry: IndexEntry) -> Future[tuple[tuple | None, np.ndarray]]:
        """Start decoding *entry* on its own thread and keep it for its render.

        The render that mixes *entry* in takes the audio from the returned
        future; the render that plays it next finds it in the one-slot
        cache (:meth:`_take_prefetched`), so the track is decoded once.
        Started before the outgoing track's own decode, the two decodes
        run side by side (two FFmpeg processes) when neither is cached,
        as on "Play now".
        """
        future: Future[tuple[tuple | None, np.ndarray]] = Future()

        def decode() -> None:
            try:
                before = self._playout_key(entry)
                audio = self._load_playout_audio(entry)
                after = self._playout_key(entry)
                # A first decode fills missing loudness data; other changes invalidate reuse.
                stable = (
                    before is not None
                    and after is not None
                    and before[:-1] == after[:-1]
                    and (before[-1] is None or before[-1] == after[-1])
                )
                future.set_result((after if stable else None, audio))
            except BaseException as exc:
                future.set_exception(exc)

        with self._playout_lock:
            self._playout_slot = (entry.path, future)
        station_thread(target=decode, name="autodj-decode-next", daemon=True).start()
        return future

    def _take_prefetched(self, entry: IndexEntry) -> Future[tuple[tuple | None, np.ndarray]] | None:
        """Take *entry*'s decode out of the cache, if it is the one there.

        Taken, not copied: the render that takes it owns the samples (a
        tempo glide writes into them), and nothing else holds them.
        """
        with self._playout_lock:
            slot = self._playout_slot
            if slot is None or slot[0] != entry.path:
                return None
            self._playout_slot = None
        return slot[1]

    def _playout_audio(
        self,
        entry: IndexEntry,
        prefetched: Future[tuple[tuple | None, np.ndarray]] | None,
    ) -> np.ndarray:
        """*entry*'s audio from its *prefetched* decode, or a fresh decode.

        The prefetched samples are used only when nothing they depend on
        changed since (:meth:`_playout_key`), so they are the samples a
        fresh decode would give.
        """
        if prefetched is not None:
            try:
                key, audio = prefetched.result()
            except Exception:  # the fresh decode below reports the failure
                key = None
            if key is not None and key == self._playout_key(entry):
                return audio
        return self._load_playout_audio(entry)

    def _lift_intro(self, audio: np.ndarray, entry: IndexEntry) -> np.ndarray:
        mode = self._cfg.playback.loudness_processing or (
            "replaygain" if self._cfg.replaygain.enabled else "off"
        )
        if mode == "off":
            return audio
        max_db = self._cfg.playback.intro_lift_max_db
        if max_db <= 0:
            return audio
        record = self._library.catalog.record_for_path(entry.path) if self._library else None
        if record is not None and record["kind"] == "liner":
            return audio
        from autodj.intro_lift import apply_intro_lift

        lift = record.get("intro_lift_db") if record else None
        writable = audio.dtype == np.float32 and audio.flags.writeable
        return apply_intro_lift(audio, SAMPLE_RATE, min(max_db, lift or 0.0), in_place=writable)

    def saved_points(self, entry: IndexEntry) -> dict[str, Any]:
        """Source-file cue times, with no assumed safe talk-over space."""
        prepared = getattr(entry, "_prepared_points", None)
        if prepared is not None:
            return dict(prepared)
        points = self._library.catalog.points(entry.path) if self._library else {}
        return {"start": 0.0, "end": entry.length, **points}

    def _render_saved_track(
        self,
        current: IndexEntry,
        next_entry: IndexEntry | None,
        start_offset: int,
        liners: tuple[tuple[IndexEntry, bool], ...] = (),
        *,
        breaks: tuple[Break, ...] = (),
        intro: Any = None,
        source_decode: Future[tuple[tuple | None, np.ndarray]] | None = None,
        incoming_decode: Future[tuple[tuple | None, np.ndarray]] | None = None,
    ) -> RenderedTrack | None:
        from autodj.playout import liner_chain_timing, liner_main_frames, render_handoff

        source = self._playout_audio(current, source_decode)
        points = self.saved_points(current)
        source_start = round((points.get("start") or 0.0) * SAMPLE_RATE)
        start = max(start_offset, source_start)
        end = min(len(source), round((points.get("end") or current.length) * SAMPLE_RATE))
        if start >= end:
            return None
        rate_a = self.music_playback_rate(current)
        source = resample_music(source, rate_a)
        source_start_out = _music_output_frame(source_start, rate_a)
        start_out = _music_output_frame(start, rate_a)
        end_out = min(len(source), _music_output_frame(end, rate_a))
        points_out = _music_output_points(points, rate_a)
        processing = self._cfg.playback.loudness_processing or ""
        if next_entry is None:
            return RenderedTrack(
                current,
                source[start_out:end_out],
                None,
                0,
                "",
                start_offset=start,
                loudness_processing=processing,
                full_audio=source[source_start_out:end_out],
                source_start_offset=source_start,
                playback_rate=rate_a,
            )
        is_liner = self._is_liner(current)
        clips, events = [], []
        liner_spans = []
        timing: list[tuple[int, int, float | None, bool]] = []
        frames = 0
        if is_liner:
            clips.append(source[source_start:end])
            frames = end - source_start
            liner_spans.append((0.0, frames / SAMPLE_RATE))
            timing.append(
                (
                    frames,
                    liner_main_frames(source_start, frames, points.get("segue")),
                    points.get("overlap_start"),
                    False,
                )
            )

        def add_clip(
            audio: np.ndarray, cues: dict[str, Any], length: float, allow_outro: bool
        ) -> tuple[int, int] | None:
            """Append one voice to the chain; its first frame and its speech's frames."""
            nonlocal frames
            first = round((cues.get("start") or 0) * SAMPLE_RATE)
            last = round((cues.get("end") or length) * SAMPLE_RATE)
            clip = audio[first:last]
            if not len(clip):
                return None
            main = liner_main_frames(first, len(clip), cues.get("segue"))
            timing.append((len(clip), main, cues.get("overlap_start"), allow_outro))
            at = frames
            clips.append(clip)
            frames += len(clip)
            return at, main

        for liner_entry, queued in liners:
            try:
                record = (
                    self._library.catalog.record_for_path(liner_entry.path)
                    if self._library
                    else None
                )
                added = add_clip(
                    self._load_playout_audio(liner_entry),
                    self.saved_points(liner_entry),
                    liner_entry.length,
                    bool(record and record.get("allow_outro", False)),
                )
                if added is not None:
                    events.append((added[0], liner_entry, queued))
                    liner_spans.append((added[0] / SAMPLE_RATE, frames / SAMPLE_RATE))
            except (OSError, ValueError, RuntimeError):
                logger.exception(
                    "Cannot prepare liner %s; leaving music playable", liner_entry.path
                )
                if queued:
                    with self._state.queue_lock:
                        self._commit_queue_pick(liner_entry)
        incoming_points = self.saved_points(next_entry)
        rate_b = self.music_playback_rate(next_entry)
        incoming_points_out = _music_output_points(incoming_points, rate_b)
        spoken: list[tuple[int, str, int]] = []
        if breaks:
            outro_point = points.get("outro")
            segue = points.get("segue") or points.get("end") or current.length
            safe_outro = max(
                points.get("start") or 0.0,
                start_offset / SAMPLE_RATE,
                segue if outro_point is None else outro_point,
            )
            spoken = self._add_breaks(
                breaks,
                incoming_points_out,
                add_clip,
                preceding_clip_s=frames / SAMPLE_RATE,
                outgoing_outro_s=max(0.0, segue - safe_outro) / rate_a,
            )
        # The matched intro leads into the song and keeps its native voice speed.
        introduced = self._add_intro(intro, add_clip) if intro is not None else []
        voiced = [*spoken, *((at, intro_id, end) for at, intro_id, end, _label in introduced)]
        duck_db = self._break_duck_db() if voiced else 0.0
        incoming_intro = max(
            0.0,
            (incoming_points_out.get("intro") or 0.0) - (incoming_points_out.get("start") or 0.0),
        )
        main_frames, release, outro_lead_limit = liner_chain_timing(timing, incoming_intro)
        handoff = render_handoff(
            np.zeros((1, 2), dtype=np.float32) if is_liner else source,
            {"start": 0, "end": 0, "segue": 0} if is_liner else points_out,
            incoming_points_out,
            start_offset=0 if is_liner else source_start_out,
            liner_start_offset=0 if is_liner else start_out,
            fade_seconds=self._cfg.playback.crossfade_seconds,
            liner=np.concatenate(clips) if clips else None,
            overlap_start=release,
            liner_segue=main_frames / SAMPLE_RATE if clips else None,
            outro_lead_limit=outro_lead_limit,
            duck_spans=tuple((at / SAMPLE_RATE, end / SAMPLE_RATE) for at, _id, end in voiced),
            duck_db=duck_db,
            liner_duck_spans=tuple(liner_spans),
            liner_duck_db=self._cfg.playback.liners_duck_db,
        )
        overlap = len(handoff.next_overlay)
        # A view of handoff.full_audio: adding the head here completes the render.
        tail = handoff.next_overlay
        next_offset = handoff.next_start_offset + overlap
        next_source_offset = _music_source_frame(next_offset, rate_b)
        if overlap:
            try:
                incoming_source = (
                    incoming_decode.result()[1]
                    if incoming_decode is not None
                    else self._load_playout_audio(next_entry)
                )
                incoming = resample_music(incoming_source, rate_b)
                incoming_end = min(
                    len(incoming),
                    _music_output_frame(
                        round((incoming_points.get("end") or next_entry.length) * SAMPLE_RATE),
                        rate_b,
                    ),
                )
                head = incoming[handoff.next_start_offset : min(next_offset, incoming_end)]
                if handoff.overlay_gain is not None:
                    head = head * handoff.overlay_gain[: len(head), None]
                tail[: len(head)] += head
                next_offset = handoff.next_start_offset + len(head)
                next_source_offset = min(
                    len(incoming_source), _music_source_frame(next_offset, rate_b)
                )
            except (OSError, ValueError, RuntimeError):
                logger.exception("Cannot prepare incoming track %s", next_entry.path)
        full_audio = handoff.full_audio
        audio = full_audio[start_out - source_start_out :]
        liner_start = (
            0
            if handoff.liner_s is None
            else round(handoff.liner_s * SAMPLE_RATE)
            - handoff.start_offset
            - (start_out - source_start_out)
        )
        return RenderedTrack(
            current,
            audio,
            next_entry,
            next_source_offset,
            "none",
            start_offset=start,
            overlap_frames=overlap,
            next_entry_offset=_music_source_frame(handoff.next_start_offset, rate_b),
            liner_events=tuple(
                (liner_start + frame, entry, queued) for frame, entry, queued in events
            ),
            break_events=tuple(
                (liner_start + at, break_id, liner_start + end) for at, break_id, end in spoken
            ),
            intro_events=tuple(
                (liner_start + at, intro_id, liner_start + end, label)
                for at, intro_id, end, label in introduced
            ),
            intro_assets=(
                ((intro.intro_id, str(intro.path)),) if introduced and intro is not None else ()
            ),
            next_reserved_liners=tuple(entry for _, entry, queued in events if queued),
            loudness_processing=processing,
            full_audio=full_audio,
            source_start_offset=source_start,
            playback_rate=rate_a,
            next_playback_rate=rate_b,
        )

    def _add_breaks(
        self,
        breaks: tuple[Break, ...],
        incoming_points: dict[str, Any],
        add_clip: Callable[[np.ndarray, dict[str, Any], float, bool], tuple[int, int] | None],
        *,
        preceding_clip_s: float = 0.0,
        outgoing_outro_s: float = 0.0,
    ) -> list[tuple[int, str, int]]:
        """Decode each break and append it to the handoff's voice chain (#88).

        Only the last break may talk up the upcoming song: one before it
        plays between the songs.  A break that cannot be decoded is dropped
        and the music plays on without it.

        Returns:
            ``(first frame, break id, end of speech)`` in the chain, per break.
        """
        desk = self.break_desk
        config = desk.config if desk is not None else self._cfg.breaks
        start = incoming_points.get("start") or 0.0
        intro = incoming_points.get("intro")
        intro_s = None if intro is None else max(0.0, float(intro) - float(start))
        spoken: list[tuple[int, str, int]] = []
        for index, item in enumerate(breaks):
            manual_fit = item.source == "voice_track" and item.placement != "standalone"
            if manual_fit:
                placement, note = "talk_up", ""
            elif index < len(breaks) - 1 and item.kind == "dj":
                placement, note = "standalone", "another break follows it"
            else:
                placement, note = placement_for(
                    item.kind,
                    item.placement,
                    config.dj_placement,
                    intro_s,
                    config.min_intro_seconds,
                )
            try:
                audio = self._load_break_audio(item)
                # Found again in the audio that plays: processing can move the
                # speech's edges, and its level decides where they are.
                cues = speech_cues(audio)
                if item.source == "prepared_edition":
                    # Audibility is still validated above; prepared reviews and
                    # playout retain the complete approved source, including padding.
                    cues = item.cues
            except Exception:
                logger.exception("Cannot decode break %s; the music plays on", item.break_id[:8])
                self._drop_prepared_or_break(
                    item, "its file could not be decoded or holds no speech"
                )
                continue
            if item.source == "voice_track" and item.eta is not None:
                manual_fit = item.placement != "standalone"
                queued_clip_s = sum(
                    max(0.0, prior.cues.end - prior.cues.start) for prior in breaks[:index]
                )
                clip_s = max(0.0, cues.end - cues.start)
                if manual_fit:
                    starts_at = item.eta - outgoing_outro_s
                    duration = outgoing_outro_s + preceding_clip_s + queued_clip_s + clip_s
                else:
                    starts_at = item.eta + preceding_clip_s + queued_clip_s
                    duration = clip_s
                try:
                    crosses = self.voice_track_crosses_hard_boundary(starts_at, duration)
                except Exception:
                    logger.exception(
                        "Cannot check hard event timing for voice track %s", item.break_id[:8]
                    )
                    crosses = True
                if crosses:
                    self._drop_prepared_or_break(
                        item,
                        "its voice chain would cross or could not be checked against a hard scheduled boundary",
                    )
                    continue
            added = add_clip(
                audio,
                cues.points(placement == "talk_up"),
                cues.length,
                manual_fit or (item.source != "voice_track" and item.allow_outro),
            )
            if added is None:
                self._drop_prepared_or_break(item, "it holds no audio")
                continue
            at, speech = added
            if desk is not None:
                desk.mixed_as(item.break_id, placement)
            if note:
                logger.info("Break %s plays standalone: %s", item.break_id[:8], note)
            spoken.append((at, item.break_id, at + speech))
        return spoken

    def _drop_prepared_or_break(self, item: Break, reason: str) -> None:
        if item.source == "prepared_edition":
            callback = getattr(self, "prepared_speech_failed", None)
            if callback is not None:
                callback(item.break_id, reason)
        elif self.break_desk is not None:
            self.break_desk.drop(item.break_id, reason)

    def _load_break_audio(self, item: Break) -> np.ndarray:
        """A break's audio, lightly compressed and leveled when processing is on."""
        limit = item.limit_s if item.limit_s is not None else self.break_desk.config.max_seconds
        return self._load_voice_audio(item.path, float(limit))

    def _load_voice_audio(self, path: Path, limit: float) -> np.ndarray:
        """A spoken clip's audio (a break or a matched intro), at most *limit* seconds.

        Lightly compressed and leveled when station processing is on.  The
        header is checked again before anything is decoded
        (:mod:`autodj.break_audio`), and FFmpeg is told the demuxer, so it
        probes nothing.
        """
        from autodj.break_audio import check_break_audio, decode_break, demuxer_for

        if self._cfg.playback.loudness_processing in {"smooth", "stereotool"}:
            from autodj.processing import load_and_measure_audio

            check_break_audio(path, limit)
            audio, _measurement = load_and_measure_audio(
                path, "voice", limit + 1.0, input_format=demuxer_for(path)
            )
            return audio
        return decode_break(path, limit)

    def _add_intro(
        self,
        intro: Any,
        add_clip: Callable[[np.ndarray, dict[str, Any], float, bool], tuple[int, int] | None],
    ) -> list[tuple[int, str, int, str]]:
        """Decode the matched intro and append it to the handoff's voice chain (#127).

        A registered liner chosen by an intro rule uses its saved liner cues.
        A private song or artist upload uses detected speech cues and the
        same liner placement, allowing outgoing outro and incoming intro
        overlap. An unavailable clip is left out.

        Returns:
            ``[(first frame, intro id, end of speech, label)]`` in the chain,
            or ``[]``.
        """
        from autodj.matched_intros import MAX_INTRO_SECONDS

        rule_liner = intro.intro_id.startswith("rule:")
        try:
            if rule_liner:
                library = self._library
                if library is None:
                    return []
                record = library.catalog.record_for_path(intro.path)
                entry = library.catalog.entry_for_path(intro.path)
                if (
                    record is None
                    or record.get("kind") != "liner"
                    or entry is None
                    or str(entry.path) != str(intro.path)
                ):
                    return []
                audio = self._load_playout_audio(entry)
                cues = self.saved_points(entry)
                allow_outro = bool(record.get("allow_outro", False))
                clip_length = entry.length
                placement = "uses its saved liner cues"
            else:
                audio = self._load_voice_audio(intro.path, MAX_INTRO_SECONDS)
                spoken = speech_cues(audio)
                cues = spoken.points(True)
                allow_outro = True
                clip_length = spoken.length
                placement = "uses detected speech cues with liner placement"
        except Exception:
            logger.exception("Cannot prepare matched intro %s; the music plays on", intro.label)
            return []
        added = add_clip(audio, cues, clip_length, allow_outro)
        if added is None:
            return []
        at, speech = added
        logger.info("Matched intro %s %s", intro.label, placement)
        return [(at, intro.intro_id, at + speech, intro.label)]

    def _break_duck_db(self) -> float:
        """How far the music drops under a spoken voice: ``[breaks] duck_db`` or the liners'."""
        desk = self.break_desk
        configured = desk.config.duck_db if desk is not None else None
        return float(configured if configured is not None else self._cfg.playback.liners_duck_db)

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
        # Respect the lyric-display toggle — when off we skip ALL lyric
        # work and the web UI hides its card.
        if not self._cfg.playback.show_lyrics:
            return [], ""
        return self._read_any_lyrics(path)

    def _read_any_lyrics(self, path: str) -> tuple[list, str]:
        """:meth:`_read_lyrics_for_path` whether or not lyrics are shown.

        The mix reads lyric timing for itself (:meth:`_sung_spans`).
        """
        from autodj.audio_meta import read_library_lyrics

        return read_library_lyrics(path, self._cfg.library)

    def _timed_lyrics(self, path: str) -> list[LyricLine]:
        """Synced lyric lines of *path* for the mix; empty when none or unreadable."""
        try:
            return list(self._read_any_lyrics(path)[0])
        except Exception as exc:
            logger.debug("Lyric timing unavailable for %s: %s", path, exc)
            return []

    def _sung_spans(self, path: str) -> tuple[tuple[float, float], ...]:
        """Sung spans of *path* (:func:`~autodj.vocals.sung_spans`), cached."""
        from autodj.vocals import VocalCache

        with self._vocal_cache_lock:
            cache = self.__dict__.get("_vocal_cache")
            if cache is None:
                cache = self.__dict__["_vocal_cache"] = VocalCache(self._timed_lyrics)
        return cache.spans(str(path))

    def vocal_start_s(self, entry: IndexEntry) -> float | None:
        """Seconds into *entry*'s file where its vocal starts, if known.

        Its first synced lyric line, else its detected intro end, else
        ``None``.  Used to time liner talk-ups.
        """
        if self._library is not None:
            record = self._library.catalog.record_for_path(entry.path)
            if record is not None:
                return record["points"].get("intro")

        from autodj.vocals import first_vocal_s

        first = first_vocal_s(self._sung_spans(entry.path))
        if first is not None:
            return first
        meta = self._peek_incoming_meta(entry)
        if meta is not None and meta.intro_end_s > 0:
            return float(meta.intro_end_s)
        return None

    def _guard_vocals(
        self,
        current: IndexEntry,
        next_entry: IndexEntry,
        meta_a: DjMeta | None,
        match: _BeatmatchPlan,
        *,
        entry: int,
        crossfade: int,
        earliest: int,
        length: int,
    ) -> tuple[int, int]:
        """Fade start and length that keep the two tracks' vocals apart.

        ``[djmix] vocal_guard``: with synced lyrics on both tracks the
        fade is shortened or moved (:func:`~autodj.vocals.guard_fade`) so
        the outgoing track's last sung line ends before the incoming one
        starts singing.  Moves go in whole beats of *meta_a*'s grid, so a
        beatmatched fade stays phase locked.

        Args:
            current: The outgoing track.
            next_entry: The incoming track.
            meta_a: The outgoing track's DJ meta.
            match: The beatmatch plan (its fade start and stretch).
            entry: Sample of the incoming track where it enters.
            crossfade: Fade length in samples.
            earliest: Earliest allowed fade start (already played before it).
            length: The outgoing track's length in samples.

        Returns:
            ``(fade_start, crossfade)`` in samples; the plan's own when
            the guard is off or changes nothing.
        """
        from autodj.vocals import first_vocal_s, guard_fade

        fade_start = match.fade_start
        if self._cfg.djmix.vocal_guard is not True:
            return fade_start, crossfade
        out_spans = self._sung_spans(current.path)
        in_spans = self._sung_spans(next_entry.path) if out_spans else ()
        in_first = first_vocal_s(in_spans, after=entry / SAMPLE_RATE)
        if in_first is None:
            return fade_start, crossfade
        ratio = match.ratio if abs(match.ratio - 1.0) > NO_STRETCH else 1.0
        beat_s = local_period(meta_a.beats, fade_start / SAMPLE_RATE) if meta_a else None
        guarded = guard_fade(
            out_spans,
            in_first,
            start_s=fade_start / SAMPLE_RATE,
            fade_s=crossfade / SAMPLE_RATE,
            entry_s=entry / SAMPLE_RATE,
            ratio=ratio,
            earliest_s=earliest / SAMPLE_RATE,
            end_s=length / SAMPLE_RATE,
            beat_s=beat_s,
        )
        if guarded is None:
            return fade_start, crossfade
        start = (
            fade_start
            if guarded[0] == fade_start / SAMPLE_RATE
            else round(guarded[0] * SAMPLE_RATE)
        )
        start = max(earliest, start)
        fade = max(0, min(crossfade, round(guarded[1] * SAMPLE_RATE), length - start))
        logger.info(
            "Vocal guard: %s -> %s fades for %.1f s at %.1f s (was %.1f s at %.1f s)",
            current.path,
            next_entry.path,
            fade / SAMPLE_RATE,
            start / SAMPLE_RATE,
            crossfade / SAMPLE_RATE,
            fade_start / SAMPLE_RATE,
        )
        return start, fade

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

        thread = station_thread(
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

                if isinstance(self._cfg.index.library_dir, Path):
                    self._dj_cache = get_cache(
                        self._cfg.index.library_dir,
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
        try:
            meta = self._dj_cache.get(path)
        except ValueError:
            # Additional source folders do not share the legacy cache root.
            return None
        if not meta.analysed:
            meta = self._analyse(audio_a, sr_a, path)
            self._dj_cache.flush(batch=10)
        return self._refresh_library_cues(path, meta)

    def _analyse(self, audio: np.ndarray | TrackWindows, sr: int, path: str) -> DjMeta:
        """Analyse *path*'s audio the way ``autodj analyse`` does, and cache it.

        *audio* is the whole track, mono or stereo, or a long track's start
        and end (:class:`~autodj.processing.TrackWindows`); a long track is
        analysed from its start and end
        (:func:`~autodj.dj_meta.analyse_track_audio`).

        Runs :func:`autodj.dj_meta.analyse_audio`, then merges cues imported
        from DJ software through the same function as ``autodj analyse``
        when ``[playback] import_external_cues`` is on, and stores the
        result in the DJ-meta cache (the caller flushes).  It is stored as
        analysed, so ``autodj analyse`` later skips the track.

        Never reads the DJ-software libraries itself: it uses what
        :meth:`load_library_cues` has read.  A track analysed before that
        read finishes gets its library cues merged when it does.
        """
        from autodj.dj_meta import analyse_decoded

        meta = analyse_decoded(audio, sr)
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
        stored before the read finished have their library cues merged here,
        intro and outro markers included.
        """
        from autodj.dj_cues_import import auto_import_cues, remerge_library_cues

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
                updated = remerge_library_cues(cache.get(path), found[path])
                if updated is not None:
                    cache.set(path, updated)
                self._cues_refreshed.add(path)
        cache.flush(force=True)

    def _refresh_library_cues(self, path: str, meta: DjMeta) -> DjMeta:
        """Bring a stored track's library cues up to date, once a session.

        A track analysed before AutoDJ read intro and outro markers keeps
        its old cues (and mix points) in the cache; the first time it
        comes up in a session its library cues are merged again (see
        :func:`autodj.dj_cues_import.remerge_library_cues`).  Only tracks
        that play are touched, so a large DJ library costs nothing extra.

        Returns:
            The meta to use: the updated one, or *meta* unchanged.
        """
        cache = self._dj_cache
        library = getattr(self, "_library_cues", None)
        refreshed = getattr(self, "_cues_refreshed", None)
        if cache is None or not library or refreshed is None or path in refreshed:
            return meta
        cues = library.get(path)
        if not cues or not meta.analysed:
            refreshed.add(path)
            return meta
        from autodj.dj_cues_import import remerge_library_cues

        with self._library_cues_lock:
            refreshed.add(path)
            updated = remerge_library_cues(cache.get(path), cues)
            if updated is None:
                return meta
            cache.set(path, updated)
        cache.flush()
        return updated

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
        try:
            existing = self._dj_cache.get(path)
        except ValueError:
            return
        if existing.analysed:
            existing = self._refresh_library_cues(path, existing)
        if existing.analysed and existing.intro_start_s is not None:
            return
        intro_start_only = existing.analysed

        def _worker() -> None:  # pragma: no cover -- background thread
            try:
                audio, sr = self._background_audio(path)
                if intro_start_only:
                    head = audio.head if isinstance(audio, TrackWindows) else audio
                    self._record_intro_start(path, mono(head), sr)
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
            thread = station_thread(
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

    @staticmethod
    def _background_audio(path: str) -> tuple[np.ndarray | TrackWindows, int]:
        """*path* decoded for background analysis: mono, or a long track's ends.

        A track the header says is long is read once as it streams, keeping
        only its start and end (:func:`~autodj.processing.read_windows`);
        any other is decoded whole, mono, at its own rate, as before.
        """
        from autodj.processing import estimated_seconds, read_windows

        seconds = estimated_seconds(Path(path))
        if seconds is not None and seconds > LONG_TRACK_SECONDS:
            return read_windows(Path(path)), SAMPLE_RATE
        return load_mono(path)

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
        try:
            meta = self._dj_cache.get(next_entry.path)
        except ValueError:
            return None
        return self._refresh_library_cues(next_entry.path, meta) if meta.analysed else None

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

    def _load_incoming(
        self,
        next_entry: IndexEntry,
        sr_a: int,
        crossfade_samples: int,
        prefetched: Future[tuple[tuple | None, np.ndarray]] | None = None,
    ) -> np.ndarray:
        """Load incoming track, resample to sr_a, ReplayGain — silence on failure.

        Uses the *prefetched* decode when given.
        """
        try:
            if prefetched is not None:
                return prefetched.result()[1]
            return self._load_playout_audio(next_entry)
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
        output_target = (
            _music_output_frame(target_samples - start, playing.playback_rate)
            if playing is not None
            else target_samples
        )
        self._playback_output_pos[0] = output_target
        if self.bus is not None:
            self.bus.seek(output_target)
        return target_samples / sr

    def seek_relative(self, delta_seconds: float) -> float:
        """Seek by ``delta_seconds`` from the current playback position.

        Convenience wrapper around :meth:`seek_to`.  Returns the new
        absolute position in seconds.
        """
        sr = max(1, self._current_sr)
        current = self._playback_pos[0] / sr
        return self.seek_to(current + delta_seconds)

    # Longest tempo glide after a beatmatched overlap, in seconds.
    _MAX_GLIDE_S: ClassVar[float] = 30.0

    def _beatmatch_plan(
        self,
        current: IndexEntry,
        next_entry: IndexEntry,
        meta_a: DjMeta | None,
        meta_b: DjMeta | None,
        *,
        entry: int,
        fade_start: int,
        crossfade: int,
        earliest: int,
        latest: int,
    ) -> _BeatmatchPlan:
        """How to beatmatch *next_entry* into *current* (``[djmix] beatmatch``).

        With both tempos trusted (:func:`~autodj.transitions.trusted_bpm`)
        and both beat grids steady around the mix, the stretch comes from
        the grids and the fade start moves so the downbeats coincide
        (:func:`~autodj.beatmatch.phase_locked_start`).  Otherwise the
        stretch comes from the indexed BPMs and the fade keeps its start,
        as before phase lock existed.  Half and double time match too.

        Args:
            current: The outgoing track.
            next_entry: The incoming track.
            meta_a: The outgoing track's DJ meta, when already at hand; the
                cache is peeked otherwise (never a fresh analysis).
            meta_b: The incoming track's DJ meta.
            entry: Sample where the incoming track enters.
            fade_start: Sample of the outgoing track where the fade starts.
            crossfade: Fade length in samples.
            earliest: Earliest allowed fade start (already played before it).
            latest: Latest allowed fade start.

        Returns:
            The plan; a ratio of 1.0 and the given fade start when the
            tracks are not beatmatched.
        """
        cfg_dj = self._cfg.djmix
        if not cfg_dj.beatmatch:
            return _BeatmatchPlan(1.0, fade_start)
        max_stretch = float(cfg_dj.beatmatch_max_stretch)
        if meta_a is None:
            meta_a = self._peek_incoming_meta(current)
        ratio: float | None = None
        in_period: float | None = None
        beats: tuple[int, ...] = ()
        matched = self._grid_match(
            current,
            next_entry,
            meta_a,
            meta_b,
            out_at_s=(fade_start + crossfade / 2) / SAMPLE_RATE,
            in_at_s=entry / SAMPLE_RATE,
        )
        if matched is not None and meta_a is not None and meta_b is not None:
            ratio, in_period = matched
            if ratio is not None:
                entry_s = entry / SAMPLE_RATE
                phrase = meta_a.beats[:: cfg_dj.phrase_bars * 4] if cfg_dj.phrase_align else []
                locked = phase_locked_start(
                    downbeats(meta_a.beats, _grid_anchor(meta_a)),
                    downbeats(meta_b.beats, _grid_anchor(meta_b)),
                    entry_s=entry_s,
                    ratio=ratio,
                    start_s=fade_start / SAMPLE_RATE,
                    fade_s=crossfade / SAMPLE_RATE,
                    earliest_s=earliest / SAMPLE_RATE,
                    latest_s=latest / SAMPLE_RATE,
                    phrase_starts=phrase,
                )
                if locked is not None:
                    fade_start = round(locked * SAMPLE_RATE)
                beats = tuple(
                    round(t * SAMPLE_RATE) for t in meta_b.beats if t >= entry_s - ON_BEAT_S
                )
        if ratio is None:
            in_period = None
            if current.bpm > 0 and next_entry.bpm > 0:
                ratio = tempo_ratio(60.0 / current.bpm, 60.0 / next_entry.bpm, max_stretch)
        if ratio is None:
            return _BeatmatchPlan(1.0, fade_start)
        return _BeatmatchPlan(ratio, fade_start, self._glide_frames(next_entry, in_period), beats)

    def _grid_match(
        self,
        current: IndexEntry,
        next_entry: IndexEntry,
        meta_a: DjMeta | None,
        meta_b: DjMeta | None,
        *,
        out_at_s: float,
        in_at_s: float,
    ) -> tuple[float | None, float] | None:
        """The stretch the two beat grids call for, when both can be trusted.

        Both tempos must be trusted (:func:`~autodj.transitions.trusted_bpm`)
        and both grids steady around the mix (*out_at_s* in the outgoing
        track, *in_at_s* in the incoming one).

        Returns:
            ``(ratio, incoming seconds per beat)``, the ratio ``None`` when
            the tempos are further apart than ``beatmatch_max_stretch``;
            ``None`` when a tempo or grid is not trusted.
        """
        from autodj.transitions import trusted_bpm

        trusted = all(
            trusted_bpm(entry_.bpm, getattr(entry_, "tempo_confidence", 0.0))
            for entry_ in (current, next_entry)
        )
        if not trusted or meta_a is None or meta_b is None:
            return None
        out_period = local_period(meta_a.beats, out_at_s)
        in_period = local_period(meta_b.beats, in_at_s)
        if out_period is None or in_period is None:
            return None
        max_stretch = float(self._cfg.djmix.beatmatch_max_stretch)
        return (tempo_ratio(out_period, in_period, max_stretch), in_period)

    def _glide_frames(self, next_entry: IndexEntry, in_period: float | None) -> int:
        """Vocoder frames of the glide back to *next_entry*'s own tempo and key.

        ``beatmatch_glide_bars`` bars, at the grid's tempo when it is
        trusted (*in_period*) or the indexed BPM, at most
        :attr:`_MAX_GLIDE_S`; 0 when neither tempo is known.
        """
        bpm = getattr(next_entry, "bpm", 0.0)
        period = in_period or (60.0 / bpm if isinstance(bpm, int | float) and bpm > 0 else 0.0)
        bars = max(0, int(self._cfg.djmix.beatmatch_glide_bars))
        glide_s = min(self._MAX_GLIDE_S, bars * 4.0 * period)
        return round(glide_s * SAMPLE_RATE / HOP)

    def _key_shift(self, current: IndexEntry, next_entry: IndexEntry) -> int:
        """Semitones *next_entry* is moved by while it mixes in (``[djmix] key_shift``).

        See :func:`~autodj.dj_meta.key_shift_semitones`; 0 when the
        setting is off or a key is not a known one.
        """
        from autodj.dj_meta import key_shift_semitones

        cfg_dj = self._cfg.djmix
        if cfg_dj.key_shift is not True:
            return 0
        key_a, mode_a, key_b, mode_b = (
            getattr(entry, field, -1)
            for entry in (current, next_entry)
            for field in ("key", "mode")
        )
        if not all(isinstance(value, int) for value in (key_a, mode_a, key_b, mode_b)):
            return 0
        return key_shift_semitones(key_a, mode_a, key_b, mode_b, str(cfg_dj.harmonic_mode))

    # Drop mixing ([djmix] drop_mix): the latest point of the incoming
    # track it may enter at, the shortest and longest overlap, and how much
    # earlier than usual the outgoing track may end.
    _DROP_MAX_ENTRY_S: ClassVar[float] = 90.0
    _DROP_MIN_OVERLAP_S: ClassVar[float] = 4.0
    _DROP_MAX_OVERLAP_S: ClassVar[float] = 30.0
    _DROP_MAX_EARLY_S: ClassVar[float] = 32.0

    def _drop_mix(
        self,
        current: IndexEntry,
        next_entry: IndexEntry,
        meta_a: DjMeta | None,
        meta_b: DjMeta | None,
        audio_a: np.ndarray,
        audio_b: np.ndarray,
        *,
        entry: int,
        fade_start: int,
        crossfade: int,
        earliest: int,
    ) -> _DropMix | None:
        """A drop-to-drop mix of *next_entry* into *current* (``[djmix] drop_mix``).

        The incoming track's build-up plays under the end of an outgoing
        section and its drop lands on the outgoing track's next section
        start (:func:`~autodj.beatmatch.drop_landing`), where the overlap
        ends: the incoming drop comes in at full level just as the
        outgoing track has faded out, never two drops on top of each
        other.  The overlap is the usual crossfade length (at most
        :attr:`_DROP_MAX_OVERLAP_S`), shorter when the incoming drop
        comes sooner after its first sound, and never under
        :attr:`_DROP_MIN_OVERLAP_S`.

        It needs beatmatch with both grids trusted (phase lock), a reliable
        drop in the incoming track (:func:`~autodj.dj_meta.mix_drops`) that
        lets it enter within :attr:`_DROP_MAX_ENTRY_S`, and a drop or
        breakdown in the outgoing track
        (:func:`~autodj.dj_meta.mix_anchors`) giving a landing no more than
        :attr:`_DROP_MAX_EARLY_S` before the fade would otherwise end.

        Args:
            current: The outgoing track.
            next_entry: The incoming track.
            meta_a: The outgoing track's DJ meta, or ``None`` to peek the cache.
            meta_b: The incoming track's DJ meta.
            audio_a: The outgoing track, whole, as decoded.
            audio_b: The incoming track, whole, as decoded.
            entry: Where the incoming track would enter (its first sound).
            fade_start: Where the fade would start in the outgoing track.
            crossfade: The fade length in samples.
            earliest: Earliest allowed fade start.

        Returns:
            The plan, or ``None`` to mix as usual.
        """
        from autodj.dj_meta import mix_anchors, mix_drops

        cfg_dj = self._cfg.djmix
        if cfg_dj.drop_mix is not True or not cfg_dj.beatmatch:
            return None
        if meta_a is None:
            meta_a = self._peek_incoming_meta(current)
        if meta_a is None or meta_b is None:
            return None
        drops = mix_drops(meta_b.cues, audio_b, SAMPLE_RATE, meta_b.beats)
        if not drops:
            return None
        matched = self._grid_match(
            current,
            next_entry,
            meta_a,
            meta_b,
            out_at_s=(fade_start + crossfade / 2) / SAMPLE_RATE,
            in_at_s=drops[0],
        )
        found, in_period = matched or (None, None)
        if found is None:
            return None
        # A stretch too small to apply is not applied, so the drop has to
        # land by the unstretched track.
        ratio = 1.0 if abs(found - 1.0) <= NO_STRETCH else found
        anchors = mix_anchors(meta_a.cues, audio_a, SAMPLE_RATE, meta_a.beats)
        longest = min(crossfade, round(self._DROP_MAX_OVERLAP_S * SAMPLE_RATE))
        shortest = min(crossfade, round(self._DROP_MIN_OVERLAP_S * SAMPLE_RATE))
        target = fade_start + crossfade
        for drop_s in drops:
            drop = round(drop_s * SAMPLE_RATE)
            overlap = min(longest, int((drop - entry) * ratio))
            enter = drop - round(overlap / ratio)
            if enter > self._DROP_MAX_ENTRY_S * SAMPLE_RATE:
                break
            if overlap < max(1, shortest):
                continue
            landing = drop_landing(
                meta_a.beats,
                anchors,
                phrase_beats=4 * int(cfg_dj.phrase_bars),
                overlap_s=overlap / SAMPLE_RATE,
                target_s=target / SAMPLE_RATE,
                earliest_s=max(earliest + overlap, target - self._DROP_MAX_EARLY_S * SAMPLE_RATE)
                / SAMPLE_RATE,
                latest_s=len(audio_a) / SAMPLE_RATE,
            )
            if landing is None:
                continue
            beats = {round(t * SAMPLE_RATE) for t in meta_b.beats if t * SAMPLE_RATE >= enter}
            match = _BeatmatchPlan(
                ratio,
                round(landing * SAMPLE_RATE) - overlap,
                self._glide_frames(next_entry, in_period),
                tuple(sorted(beats | {drop})),
            )
            logger.info(
                "Drop mix: %s drops at %.1f s, on %s at %.1f s.",
                next_entry.path,
                drop_s,
                current.path,
                landing,
            )
            return _DropMix(enter, overlap, match)
        return None

    def _with_key_shift(
        self,
        match: _BeatmatchPlan,
        current: IndexEntry,
        next_entry: IndexEntry,
        meta_b: DjMeta | None,
        entry: int,
    ) -> _BeatmatchPlan:
        """*match* with the key shift of ``[djmix] key_shift``, when one is due.

        Without beatmatching the plan has no beats or glide yet: the
        incoming grid's beats are added when it is steady, so transients
        stay in place, and the key glides back over
        ``beatmatch_glide_bars`` bars.
        """
        shift = self._key_shift(current, next_entry)
        if not shift:
            return match
        beats, ramp = match.beats, match.ramp_frames
        entry_s = entry / SAMPLE_RATE
        if not beats and meta_b is not None and local_period(meta_b.beats, entry_s) is not None:
            beats = tuple(round(t * SAMPLE_RATE) for t in meta_b.beats if t >= entry_s - ON_BEAT_S)
        if not ramp:
            ramp = self._glide_frames(next_entry, None)
        return replace(match, ramp_frames=ramp, beats=beats, semitones=shift)

    @staticmethod
    def _incoming_glide(
        audio_b: np.ndarray, entry: int, plan: _BeatmatchPlan, crossfade: int
    ) -> Glide | None:
        """The stretch for *audio_b*, or ``None`` when none is needed or fits.

        A track too short for the whole glide gets the shortest one; one
        too short even for that plays unstretched.  A key shift
        (:attr:`_BeatmatchPlan.semitones`) is dropped before the stretch
        is: a track too short for the shortest key glide mixes in at its
        own key.
        """
        stretch = abs(plan.ratio - 1.0) > NO_STRETCH
        tries = [(ramp, plan.semitones) for ramp in (plan.ramp_frames, 0) if plan.semitones]
        if stretch:
            tries += [(plan.ramp_frames, 0), (0, 0)]
        for ramp, semitones in tries:
            glide = Glide(
                entry=entry,
                ratio=plan.ratio if stretch else 1.0,
                played=crossfade,
                ramp_frames=ramp,
                beats=plan.beats,
                semitones=semitones,
            )
            if glide.origin >= 0 and glide.fits(len(audio_b)):
                return glide
        return None

    @staticmethod
    def _play_glide_in(audio: np.ndarray, start_offset: int, glide: Glide | None) -> None:
        """Write the rest of *glide* into *audio*, in place.

        *audio* is this track as decoded for its own render, and *glide*
        how the previous render brought it in.  Everything after the
        overlap up to the end of the glide is replaced by the stretched
        audio, so the render, which starts at *start_offset* (the glide's
        :attr:`~autodj.beatmatch.Glide.origin`), plays on with no sample
        repeated or skipped.  When the glide cannot be computed the track
        plays on at its own tempo from there.
        """
        if glide is None or glide.origin != start_offset or not glide.fits(len(audio)):
            return
        try:
            audio[glide.origin : glide.source_end] = glide.continuation(audio)
        except Exception as exc:
            logger.warning("Tempo glide failed; the track plays at its own tempo: %s", exc)

    def _skip_incoming_silence_samples(
        self,
        audio_b: np.ndarray,
        sr_a: int,
        meta_b: DjMeta | None = None,
    ) -> int:
        """Return how many of *audio_b*'s leading samples to drop as silence.

        ``djmix.outro_intro_align`` and the marker-aware transition modes
        (``full_intro_outro`` / ``fixed_skip_silence``) start the incoming
        track at its first sound, as Mixxx does.  The intro itself plays
        under the outgoing outro, so a verse sung over it stays whole.
        An intro start set in DJ software (an imported ``intro_start`` cue
        in *meta_b*) is used instead of the measurement.  *audio_b* is the
        track as decoded: a beatmatch stretch starts from this point.
        """
        from autodj.dj_meta import detect_intro_start, imported_markers

        mode = self._cfg.playback.transition_mode
        marker_skip = mode in ("full_intro_outro", "fixed_skip_silence")
        if not (self._cfg.djmix.outro_intro_align or marker_skip):
            return 0
        marked = imported_markers(meta_b.cues).get("intro_start") if meta_b else None
        if marked is not None:
            first_sound = int(marked * sr_a)
        else:
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
        return join_audio([audio_a_trimmed[:-crossfade_samples], tail])

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

    def _pick_effect(self, mode: Any, rng: Any = None, *, advance: bool = False) -> Any:
        from autodj.transitions import TransitionFx, pick_effect

        if mode == TransitionFx.ROTATE:
            cursor = self.__dict__.get("_rotate_cursor", 0)
            if advance:
                self._rotate_cursor = cursor + 1
            return pick_effect(mode, rng, rotate_index=cursor)
        return pick_effect(mode, rng)

    def planned_transition(
        self, current: IndexEntry, upcoming: IndexEntry, pick_mode: str = ""
    ) -> str:
        """The concrete effect for the crossfade from *current* into *upcoming*.

        Resolves the ``[transitions] effect`` setting (``auto`` with
        :func:`~autodj.transitions.choose_auto_effect`, ``random`` and
        ``rotate`` with :func:`~autodj.transitions.pick_effect`) once per
        pair and remembers it, so the server mix and the web state agree:
        the render ahead uses this choice and ``next_transition_fx`` in the
        state reports it.  A changed setting chooses again.

        Args:
            current: The outgoing track.
            upcoming: The incoming track.
            pick_mode: How *upcoming* was chosen (``"discovery"`` steers
                ``auto``); only the first call for a pair uses it.

        With ``[djmix] key_shift`` on, ``auto`` judges the incoming key as
        it will be heard (:meth:`_key_shift`), so a pair the shift makes
        compatible is not treated as a clash.

        Returns:
            A :class:`~autodj.transitions.TransitionFx` value, never a
            meta-mode.
        """
        if self._cfg.playback.transition_mode == "saved_points":
            return "none"
        from autodj.transitions import TransitionFx, choose_auto_effect

        shift = self._key_shift(current, upcoming)
        setting = (str(self._cfg.transitions.effect).lower(), shift)
        key = (str(current.path), str(upcoming.path))
        rng = getattr(self, "_rng", None)
        with self._fx_plan_lock:
            plans = self.__dict__.setdefault("_fx_plans", {})
            planned = plans.pop(key, None)
            if planned is None or planned[0] != setting:
                mode = _effect_mode(setting[0])
                if mode == TransitionFx.AUTO:
                    chosen = choose_auto_effect(current, upcoming, pick_mode, rng, key_shift=shift)
                else:
                    chosen = self._pick_effect(mode, rng)
                planned = (setting, chosen.value)
            plans[key] = planned  # most recent last
            while len(plans) > self._FX_PLANS_KEPT:
                del plans[next(iter(plans))]
        return planned[1]

    def _plan_drop_riser(self, current: IndexEntry, next_entry: IndexEntry) -> None:
        """Make a drop mix's ``auto`` effect a noise riser, which ends on the drop.

        A synthesised layer ends where the overlap ends, and a drop mix
        ends the overlap on the incoming drop.  An effect chosen by name
        is kept.
        """
        from autodj.transitions import TransitionFx

        setting = (str(self._cfg.transitions.effect).lower(), self._key_shift(current, next_entry))
        if _effect_mode(setting[0]) != TransitionFx.AUTO:
            return
        with self._fx_plan_lock:
            plans = self.__dict__.setdefault("_fx_plans", {})
            plans[(str(current.path), str(next_entry.path))] = (
                setting,
                TransitionFx.NOISE_RISER.value,
            )

    def _fx_plan(
        self,
        current: IndexEntry,
        next_entry: IndexEntry,
        meta_a: DjMeta | None,
        start_offset: int,
        semitones: int = 0,
    ) -> _FxPlan:
        """The planned effect for *current* → *next_entry*, with its sync data.

        The tempo counts only when ``[playback] beat_sync_fx`` is on and
        :func:`~autodj.transitions.trusted_bpm` accepts it, and the keys
        only with ``key_sync_fx``; the beat grid comes from *meta_a* or,
        without it, the DJ-meta cache (never a fresh analysis).  The
        incoming key is the one heard, *semitones* away from its own.
        """
        from autodj.beat_sync import extract_downbeats, key_to_hz
        from autodj.transitions import BeatSync, TransitionFx, trusted_bpm

        name = self.planned_transition(current, next_entry)
        if _effect_mode(self._cfg.transitions.effect) == TransitionFx.ROTATE:
            with self._fx_plan_lock:
                name = self._pick_effect(TransitionFx.ROTATE, advance=True).value
                key = (str(current.path), str(next_entry.path))
                setting = (
                    str(self._cfg.transitions.effect).lower(),
                    self._key_shift(current, next_entry),
                )
                self.__dict__.setdefault("_fx_plans", {})[key] = (setting, name)
        playback = self._cfg.playback
        bpm = 0.0
        if bool(playback.beat_sync_fx):
            bpm = trusted_bpm(current.bpm, getattr(current, "tempo_confidence", 0.0))
        roots: list[float | None] = [None, None]
        if bool(playback.key_sync_fx):
            pairs: tuple[tuple[Any, int], ...] = ((current, 0), (next_entry, semitones))
            for i, (track, shift) in enumerate(pairs):
                track_key = getattr(track, "key", -1)
                heard = (
                    (track_key + shift) % 12
                    if isinstance(track_key, int) and track_key >= 0
                    else track_key
                )
                roots[i] = key_to_hz(heard) if isinstance(heard, int) else None
        if not bpm and roots == [None, None]:
            return _FxPlan(name)
        if meta_a is None and bpm:
            meta_a = self._peek_incoming_meta(current)
        beats = meta_a.beats if meta_a is not None and bpm else []
        return _FxPlan(
            name,
            BeatSync(bpm=bpm, out_root_hz=roots[0], in_root_hz=roots[1]),
            downbeats=tuple(extract_downbeats(beats)),
            origin_s=start_offset / SAMPLE_RATE,
        )

    def _apply_transition_effect(
        self,
        audio_a_trimmed: np.ndarray,
        audio_b: np.ndarray,
        b_head: np.ndarray,
        sr_a: int,
        crossfade_samples: int,
        plan: _FxPlan | None = None,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
        """Apply the transition effect *plan* names (by default the configured one).

        Returns the effect's name instead of storing it, because this runs
        while rendering ahead and ``self._last_transition_fx`` describes
        the playing track.  With the plan's beat grid, the synced effects
        start on the first downbeat of the treated tail.

        Returns:
            ``(a_trimmed, b_head, extra_layer, effect_name)``.
        """
        from autodj.beat_sync import downbeat_offset
        from autodj.transitions import TransitionFx, apply_transition

        extra_shape = (0,) if audio_a_trimmed.ndim == 1 else (0, 2)
        extra_layer = np.zeros(extra_shape, dtype=np.float32)
        if plan is None:
            with self._fx_plan_lock:
                plan = _FxPlan(
                    self._pick_effect(
                        _effect_mode(self._cfg.transitions.effect), advance=True
                    ).value
                )
        chosen_fx = _effect_mode(plan.name)
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
        sync = plan.sync
        if sync is not None and plan.downbeats:
            tail_start_s = plan.origin_s + (len(audio_a_trimmed) - effect_samples) / sr_a
            sync = replace(sync, downbeat_s=downbeat_offset(plan.downbeats, tail_start_s))
        tail_fx, head_fx, extra_layer = apply_transition(
            tail,
            head_for_fx,
            sr_a,
            chosen_fx,
            seed=int(self._rng.integers(2**31)),
            sync=sync,
        )

        wet = max(0.0, min(1.0, self._cfg.transitions.wet_mix))
        if wet < 1.0:
            tail_fx = tail * (1.0 - wet) + tail_fx * wet
            head_fx = head_for_fx * (1.0 - wet) + head_fx * wet

        audio_a_trimmed = join_audio([audio_a_trimmed[:-effect_samples], tail_fx])
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
        *,
        glide_in: Glide | None = None,
        liners: tuple[tuple[IndexEntry, bool], ...] = (),
        breaks: tuple[Break, ...] = (),
        intro: Any = None,
        prepared_plan: Any = None,
        omit_prepared: bool = False,
    ) -> RenderedTrack | None:
        """Render *current* from *start_offset* into the head of *next_entry*.

        The overlap with *next_entry* is mixed in, and the returned
        ``next_start_offset`` (with ``next_glide`` after a beatmatch) tells
        the next call where the incoming track's audio continues, so no
        sample plays twice.

        Args:
            current: Track to render.
            next_entry: Track mixed into the tail, or ``None`` for no overlap.
            start_offset: Samples of *current* already played by the previous
                overlap (including any skipped leading silence).
            glide_in: How the previous overlap stretched *current*
                (``RenderedTrack.next_glide``); the render opens with the
                rest of that stretch, back to the track's own tempo.
            liners: Liners in the handoff, with whether each is a queue pick.
            breaks: Announcer breaks in the handoff, after the liners
                (saved-point playback only, #88).
            intro: The intro matched to *next_entry*, last in the handoff
                (a :class:`~autodj.matched_intros.Chosen`; saved-point
                playback only, #127).

        Returns:
            The rendered track, or ``None`` when *current* cannot be loaded
            or has no audio left after *start_offset*.
        """
        # This track's decode, when the previous render mixed it in, and the
        # next track's, started now so it runs alongside this one's.
        if omit_prepared:
            if getattr(current, "_prepared_clip", False):
                return None
            if getattr(next_entry, "_prepared_clip", False):
                next_entry = None
            breaks = tuple(item for item in breaks if item.source != "prepared_edition")
            prepared_plan = None
        elif prepared_plan is None:
            prepared_plan = self._prepared_plan()
        if not self._prepared_primary_valid(current, plan=prepared_plan):
            return None
        if not self._prepared_primary_valid(next_entry, plan=prepared_plan):
            next_entry = None
        source_decode = self._take_prefetched(current)
        incoming_decode = (
            self._prefetch_playout_audio(next_entry) if next_entry is not None else None
        )
        try:
            rendered = self._render_decoded(
                current,
                next_entry,
                start_offset,
                glide_in=glide_in,
                liners=liners,
                breaks=breaks,
                intro=intro,
                source_decode=source_decode,
                incoming_decode=incoming_decode,
            )
            valid_plan = getattr(self, "prepared_plan_valid", None)
            unchanged = True
            if prepared_plan is not None and valid_plan is not None:
                try:
                    unchanged = valid_plan(prepared_plan)
                except (OSError, ValueError):
                    logger.warning("Prepared speech is unavailable; music continues", exc_info=True)
                    unchanged = False
                    omit_prepared = True
            if not unchanged:
                fresh = None if omit_prepared else self._prepared_plan()
                return self._render_track(
                    current,
                    next_entry,
                    start_offset,
                    glide_in=glide_in,
                    liners=liners,
                    breaks=tuple(item for item in breaks if item.source != "prepared_edition")
                    + (
                        ()
                        if omit_prepared
                        else self._prepared_breaks(current, next_entry, plan=fresh)
                    ),
                    intro=intro,
                    prepared_plan=fresh,
                    omit_prepared=omit_prepared,
                )
            selected_prepared = tuple(
                item for item in breaks if getattr(item, "source", None) == "prepared_edition"
            )
            if selected_prepared and getattr(self, "prepared_handoff", None) is not None:
                fresh = self._prepared_breaks(current, next_entry, plan=prepared_plan)

                def identity(item: Break) -> tuple:
                    return (
                        item.break_id,
                        item.path,
                        repr(item.cues),
                        item.placement,
                        item.allow_outro,
                    )

                if {identity(item) for item in selected_prepared} != {
                    identity(item) for item in fresh
                }:
                    return self._render_track(
                        current,
                        next_entry,
                        start_offset,
                        glide_in=glide_in,
                        liners=liners,
                        breaks=tuple(item for item in breaks if item not in selected_prepared),
                        intro=intro,
                    )
            if not self._prepared_primary_valid(current, plan=prepared_plan):
                return None
            if not self._prepared_primary_valid(next_entry, plan=prepared_plan):
                return self._render_track(
                    current, None, start_offset, glide_in=glide_in, liners=liners, breaks=breaks
                )
            return replace(rendered, incoming_intro=intro) if rendered is not None else None
        finally:
            # The next render takes this decode; never leave it running on.
            if incoming_decode is not None:
                wait([incoming_decode])

    def _render_decoded(
        self,
        current: IndexEntry,
        next_entry: IndexEntry | None,
        start_offset: int,
        *,
        glide_in: Glide | None,
        liners: tuple[tuple[IndexEntry, bool], ...],
        source_decode: Future[tuple[tuple | None, np.ndarray]] | None,
        incoming_decode: Future[tuple[tuple | None, np.ndarray]] | None,
        breaks: tuple[Break, ...] = (),
        intro: Any = None,
    ) -> RenderedTrack | None:
        """:meth:`_render_track` with the two decodes under way."""
        try:
            if (
                self._cfg.playback.transition_mode == "saved_points"
                or getattr(current, "_prepared_clip", False)
                or getattr(next_entry, "_prepared_clip", False)
                or any(getattr(item, "source", None) == "prepared_edition" for item in breaks)
            ):
                return self._render_saved_track(
                    current,
                    next_entry,
                    start_offset,
                    liners,
                    breaks=breaks,
                    intro=intro,
                    source_decode=source_decode,
                    incoming_decode=incoming_decode,
                )
            audio_a_full = self._playout_audio(current, source_decode)
        except (OSError, ValueError, RuntimeError) as exc:
            logger.error("Cannot load %s: %s — skipping.", current.path, exc)
            return None
        if start_offset >= len(audio_a_full):
            return None
        rate_a = self.music_playback_rate(current)
        # No lyrics here: this runs ahead of time, and the lyrics shown must
        # be the playing track's (_on_track_start loads them).
        self._ensure_dj_cache()
        meta_a = self._outgoing_meta(audio_a_full, SAMPLE_RATE, current.path)
        self._play_glide_in(audio_a_full, start_offset, glide_in)
        audio_a = audio_a_full[start_offset:]
        processing = self._cfg.playback.loudness_processing or ""
        if next_entry is None:
            return RenderedTrack(
                current,
                resample_music(audio_a, rate_a),
                None,
                0,
                "",
                start_offset=start_offset,
                loudness_processing=processing,
                playback_rate=rate_a,
            )
        meta_b = self._peek_incoming_meta(next_entry)
        eff_s = self._effective_crossfade_seconds(meta_a, meta_b, current.length)
        crossfade = int(eff_s * SAMPLE_RATE)
        if crossfade >= len(audio_a):
            return RenderedTrack(
                current,
                resample_music(audio_a, rate_a),
                next_entry,
                0,
                "",
                start_offset=start_offset,
                loudness_processing=processing,
                playback_rate=rate_a,
            )
        # meta_a's outro/beat markers are absolute positions in the FULL
        # track, so the crossfade start must be located there too -- not in
        # audio_a, which already had start_offset samples cut off the
        # front.  Re-express the result in audio_a's own coordinates after.
        a_start_full = self._crossfade_start_in_a(audio_a_full, SAMPLE_RATE, meta_a, crossfade)
        audio_b_loaded = self._load_incoming(next_entry, SAMPLE_RATE, crossfade, incoming_decode)
        intro = self._skip_incoming_silence_samples(audio_b_loaded, SAMPLE_RATE, meta_b=meta_b)
        liner_a = self._is_liner(current)
        liner_b = self._is_liner(next_entry)
        rate_b = self.music_playback_rate(next_entry)
        if (liner_a or liner_b) and (rate_a != 1.0 or rate_b != 1.0):
            # The regular planner works in source coordinates, so keep its
            # chosen fade point and wall-clock crossfade. Do not run voice
            # through tempo matching or transition effects: rate-adjust only
            # each music stem and use a plain fade across the shared overlap.
            a_start = max(0, a_start_full - start_offset)
            available_b = len(audio_b_loaded) - intro
            if not liner_b:
                available_b = round(available_b / rate_b)
            if liner_a:
                crossfade = min(crossfade, max(0, len(audio_a) - a_start))
            crossfade = min(crossfade, max(0, available_b))
            if crossfade > 0:
                if liner_a:
                    a_prefix = audio_a[:a_start]
                    a_tail = audio_a[a_start : a_start + crossfade]
                    current_rate = 1.0
                else:
                    a_tail_source_frames = min(len(audio_a), max(0, round(crossfade * rate_a)))
                    # Move the fade earlier if its source-time tail would
                    # otherwise run past the end of the outgoing track.
                    a_start = min(a_start, len(audio_a) - a_tail_source_frames)
                    a_prefix = resample_music(audio_a[:a_start], rate_a)
                    a_tail = _music_segment(
                        audio_a[a_start : a_start + a_tail_source_frames], rate_a, crossfade
                    )
                    current_rate = rate_a
                if liner_b:
                    b_head = audio_b_loaded[intro : intro + crossfade]
                    incoming_consumed = crossfade
                else:
                    incoming_consumed = min(
                        len(audio_b_loaded) - intro, max(0, round(crossfade * rate_b))
                    )
                    b_head = _music_segment(
                        audio_b_loaded[intro : intro + incoming_consumed],
                        rate_b,
                        crossfade,
                    )
                a_trimmed = join_audio([a_prefix, a_tail])
                mixed = _apply_crossfade(a_trimmed, b_head, crossfade)
                return RenderedTrack(
                    current,
                    mixed,
                    next_entry,
                    intro + incoming_consumed,
                    "none",
                    start_offset=start_offset,
                    overlap_frames=crossfade,
                    next_entry_offset=intro,
                    loudness_processing=processing,
                    playback_rate=current_rate,
                    next_playback_rate=rate_b,
                )
            return RenderedTrack(
                current,
                audio_a if liner_a else resample_music(audio_a, rate_a),
                next_entry,
                0,
                "none",
                start_offset=start_offset,
                next_entry_offset=intro,
                loudness_processing=processing,
                playback_rate=rate_a,
                next_playback_rate=rate_b,
            )
        drop = self._drop_mix(
            current,
            next_entry,
            meta_a,
            meta_b,
            audio_a_full,
            audio_b_loaded,
            entry=intro,
            fade_start=a_start_full,
            crossfade=crossfade,
            earliest=start_offset,
        )
        if drop is not None:
            # The drop mix sets the fade to end on the incoming drop; the
            # vocal guard would move it off the drop, so it stays out.
            intro, crossfade, match = drop.entry, drop.overlap, drop.match
            fade_start = match.fade_start
            self._plan_drop_riser(current, next_entry)
        else:
            match = self._beatmatch_plan(
                current,
                next_entry,
                meta_a,
                meta_b,
                entry=intro,
                fade_start=a_start_full,
                crossfade=crossfade,
                earliest=start_offset,
                latest=len(audio_a_full) - crossfade,
            )
            fade_start, crossfade = self._guard_vocals(
                current,
                next_entry,
                meta_a,
                match,
                entry=intro,
                crossfade=crossfade,
                earliest=start_offset,
                length=len(audio_a_full),
            )
        match = self._with_key_shift(match, current, next_entry, meta_b, intro)
        a_start = max(0, fade_start - start_offset)
        audio_b = audio_b_loaded[intro:]
        if a_start + crossfade > len(audio_a):
            crossfade = max(0, len(audio_a) - a_start)
        a_trimmed = audio_a[: a_start + crossfade]
        if crossfade == 0 or len(audio_b) < crossfade:
            return RenderedTrack(
                current,
                resample_music(a_trimmed, rate_a),
                next_entry,
                intro,
                "",
                start_offset=start_offset,
                loudness_processing=processing,
                playback_rate=rate_a,
            )
        glide = self._incoming_glide(audio_b_loaded, intro, match, crossfade)
        if glide is not None:
            # Only the overlap (and what a long effect reaches past it) is
            # stretched here; the next render plays the rest of the glide.
            head = max(crossfade, int(max(self._MIN_FX_DURATION_S.values()) * SAMPLE_RATE))
            try:
                audio_b = glide.head(audio_b_loaded, head)
            except Exception as exc:
                logger.warning("Beatmatch stretch failed; mixing unstretched: %s", exc)
                glide = None
            if glide is not None and glide.semitones:
                logger.info(
                    "Key shift: %s mixes in %+d semitones from its own key.",
                    next_entry.path,
                    int(glide.semitones),
                )
        b_head = audio_b[:crossfade]
        a_trimmed = self._apply_outgoing_filter_sweep(a_trimmed, SAMPLE_RATE, crossfade)
        a_trimmed, b_head, extra, fx_name = self._apply_transition_effect(
            a_trimmed,
            audio_b,
            b_head,
            SAMPLE_RATE,
            crossfade,
            plan=self._fx_plan(
                current,
                next_entry,
                meta_a,
                start_offset,
                int(glide.semitones) if glide is not None else 0,
            ),
        )
        mixed = self._mix_overlap(a_trimmed, b_head, crossfade, SAMPLE_RATE, extra)
        output_audio = resample_music(to_stereo(mixed), rate_a)
        return RenderedTrack(
            current,
            output_audio,
            next_entry,
            glide.origin if glide else intro + crossfade,
            fx_name,
            glide.ratio if glide else 1.0,
            start_offset=start_offset,
            next_glide=glide,
            overlap_frames=round(crossfade / rate_a),
            next_entry_offset=intro,
            loudness_processing=processing,
            playback_rate=rate_a,
            next_playback_rate=rate_a,
        )
