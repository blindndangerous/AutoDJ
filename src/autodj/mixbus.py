"""Clock-paced stereo mix bus for server-side playback."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import numpy as np

from autodj.eq import apply_eq, make_eq_filters, make_eq_state, ramp_eq, reset_eq_state
from autodj.kernels import duck_and_mix_in_place, nan_to_num_in_place
from autodj.stereo import SAMPLE_RATE

if TYPE_CHECKING:
    from autodj.beatmatch import Glide
    from autodj.indexer import IndexEntry
    from autodj.live import LiveDesk, LiveSource, PcmBuffer

logger = logging.getLogger(__name__)

_SKIP_FADE_FRAMES = int(0.15 * SAMPLE_RATE)
_DUCK_RAMP_FRAMES = int(0.25 * SAMPLE_RATE)
_LINER_FADE_FRAMES = int(0.04 * SAMPLE_RATE)
# Frames over which a skip effect that is installed late takes over.
_TAIL_BLEND_FRAMES = int(0.01 * SAMPLE_RATE)
# Blocks :meth:`MixBus.run` may render back to back to make up for a late
# block.  Kept well under the master processors' 16-18 block output cushion
# (see :mod:`autodj.mastering`), so a catch-up burst never drains it.
CATCH_UP_BLOCKS = 8
_NO_LINER = np.zeros((0, 2), np.float32)
# Live source audio buffered before it plays, and again after an underrun,
# so network jitter does not crackle (#48); a browser talker's, shorter.
_LIVE_PREROLL_FRAMES = int(0.3 * SAMPLE_RATE)
_MIC_PREROLL_FRAMES = int(0.06 * SAMPLE_RATE)


@dataclass(frozen=True)
class RenderedTrack:
    """One track's audio as the mix bus will play it.

    Attributes:
        entry: The track this audio belongs to.
        audio: ``(frames, 2)`` float32 audio at 44100 Hz — the track's body
            plus any overlap mixed in from *next_entry*.
        next_entry: The track mixed into the tail, or ``None`` when this is
            the last track (or there was no room for a crossfade).
        next_start_offset: Samples of *next_entry*'s audio already played in
            the overlap (including any skipped leading silence), so the following
            render call knows where to continue without replaying them.
            Expressed in *next_entry*'s own, as-loaded-fresh timeline.  After
            a beatmatch it is *next_glide*'s ``origin``: where the following
            render starts so that its positions match the file once the
            tempo glide is over.
        transition_fx: Name of the effect used for the overlap into
            *next_entry* (``"none"`` for a plain crossfade), or ``""``
            when there is no overlap.
        beatmatch_ratio: Stretch applied to *next_entry*'s audio during the
            overlap (output over input duration), or ``1.0`` when it was
            not stretched (or there is no overlap).  It describes the
            *next* track; see *mixed_in_ratio*.
        mixed_in_fx: The previous render's *transition_fx*: the effect
            this track came in with, ``""`` when it did not come in
            through an overlap (a set's first track, a jump).
        mixed_in_ratio: The previous render's *beatmatch_ratio*: how this
            track's opening was stretched while it was mixed in, ``1.0``
            when it was not.
        start_offset: Samples into *entry*'s own file where *audio*
            begins (the part the previous overlap already played), so
            elapsed time and seeks can be shown in the track's timeline.
        pick_mode: How *entry* was chosen (``"seed"``, ``"queue"``,
            ``"similarity"``...), for the "why this track" explanation.
        from_queue: Whether *entry* was peeked from the user queue; it is
            removed from the queue when this render starts playing.
        next_pick_mode: How *next_entry* was chosen.
        next_from_queue: Whether *next_entry* was peeked from the user
            queue (it stays queued until it starts).
        set_generation: The stream set the bus took this track for; a
            track start from a set that has since stopped is ignored.
        previous_entry: The track rendered just before this one (the one
            the bus plays before it), or ``None`` when not known.  A
            re-render of this track keeps excluding it from the next pick.
        next_glide: How *next_entry* is stretched from its entry back to
            its own tempo after a beatmatched overlap, or ``None``; the
            following render plays the rest of it.
        glide_in: The previous render's *next_glide*: how this track's
            opening returns to its own tempo (kept for re-renders).
        overlap_frames: Frames at the end of *audio* where *next_entry*
            is mixed in, ``0`` when there is no overlap.
        next_entry_offset: Sample of *next_entry*'s own file where it
            enters at the start of the overlap (after any skipped leading
            silence).
        break_events: ``(start, break_id, end)`` for each announcer break
            (#88) mixed into the handoff: the frames of *audio* where its
            voice starts and where its speech ends.  A render with breaks
            keeps its next track whatever the queue does, because the
            break names it.
        playback_rate: Playback speed of this track's music (source frames per
            output frame). Source positions remain in the original file's units.
        next_playback_rate: Playback speed used for *next_entry*'s music head
            in this buffer.
        intro_events: ``(start, intro_id, end, label)`` for the intro
            matched to *next_entry* (#127) mixed into the handoff: where its
            voice starts and its speech ends in *audio*, and its label.
        intro_assets: ``(intro_id, path)`` for exact selected audio files;
            paths stay internal unless the catalog identifies one as a liner.
    """

    entry: IndexEntry
    audio: np.ndarray
    next_entry: IndexEntry | None
    next_start_offset: int
    transition_fx: str
    beatmatch_ratio: float = 1.0
    start_offset: int = 0
    pick_mode: str = "similarity"
    from_queue: bool = False
    next_pick_mode: str = "similarity"
    next_from_queue: bool = False
    set_generation: int = 0
    previous_entry: IndexEntry | None = None
    mixed_in_fx: str = ""
    mixed_in_ratio: float = 1.0
    next_glide: Glide | None = None
    glide_in: Glide | None = None
    overlap_frames: int = 0
    next_entry_offset: int = 0
    liner_events: tuple[tuple[int, IndexEntry, bool], ...] = ()
    break_events: tuple[tuple[int, str, int], ...] = ()
    intro_events: tuple[tuple[int, str, int, str], ...] = ()
    intro_rng_traces: tuple[tuple[str, tuple[int | None, ...]], ...] = ()
    intro_assets: tuple[tuple[str, str], ...] = ()
    reserved_liners: tuple[IndexEntry, ...] = ()
    next_reserved_liners: tuple[IndexEntry, ...] = ()
    loudness_processing: str = ""
    clock_cursor: int | None = None
    next_clock_cursor: int | None = None
    # The outgoing cursor belongs to this wheel; an edit may change the incoming one.
    clock_name: str | None = None
    clock_boundary: float | None = None
    next_clock_boundary: float | None = None
    clock_step: dict | None = None
    next_clock_step: dict | None = None
    clock_revision: int | None = None
    next_clock_revision: int | None = None
    next_clock_name: str | None = None
    liner_plan_key: str = ""
    incoming_intro: object | None = None
    full_audio: np.ndarray | None = None
    source_start_offset: int = 0
    playback_rate: float = 1.0
    next_playback_rate: float = 1.0
    next_pick_signature: tuple[str, str, bool, int | None, int | None, bool | None] | None = None


@dataclass(frozen=True)
class SkipTail:
    """A skip effect prepared for the playing render (``[playback] skip_style``).

    Attributes:
        track: The render the effect was made from.
        start: Frame of *track*'s audio where the effect takes over.
        audio: ``(n, 2)`` float32 effect audio; the track ends with it.
    """

    track: RenderedTrack
    start: int
    audio: np.ndarray


@dataclass(eq=False)
class LinerCue:
    """Where a liner waits to start: *frame* frames into *track*'s audio.

    Attributes:
        track: The render the liner is timed against.
        frame: Frame of *track*'s audio where the liner starts.
        started: Set by the bus when the liner starts; a cue whose track
            stops playing first is dropped and never starts.
    """

    track: RenderedTrack
    frame: int
    started: bool = False


class Output(Protocol):
    """Destination for mixed blocks (e.g. an MP3 encoder or sounddevice sink).

    ``write`` must not block: :meth:`MixBus.run` calls every registered
    output's ``write`` in sequence, on its single pacing thread, so one
    slow or blocked output stalls delivery to every other output and
    delays the bus's own pacing.
    """

    def write(self, block: np.ndarray) -> None:
        """Receive one ``(882, 2)`` float32 block. Must not block."""

    def close(self) -> None:
        """Release resources held by this output."""


class MasterProcessor(Protocol):
    """Optional continuous audio processor attached to the paced run loop."""

    @property
    def startup_pending(self) -> bool: ...

    def process(self, block: np.ndarray, enabled: bool = True) -> np.ndarray: ...


class Clock(Protocol):
    """Time source used to pace :meth:`MixBus.run`."""

    def now(self) -> float:
        """Return the current time in monotonic seconds."""
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        """Block the caller for *seconds*."""


class SystemClock:
    """Real wall-clock time via :mod:`time`."""

    def now(self) -> float:
        """Return :func:`time.monotonic`."""
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        """Sleep for *seconds* with :func:`time.sleep`."""
        time.sleep(seconds)


@dataclass
class BusEvents:
    """Callbacks the mix bus makes while playing.

    ``on_need_track`` runs *while the bus's internal lock is held*, as
    part of producing the current block, so it must return immediately —
    a track that is already rendered and waiting (e.g. popped off a
    queue a worker thread fills in the background), or ``None`` when
    none is ready yet. It must never block or call back into the bus:
    doing either would stall every block the bus produces, and calling
    back into the bus would deadlock on its non-reentrant lock.

    ``on_track_start`` and ``on_position`` are collected while producing
    a block and invoked only after the lock has been released, but they
    must still not call back into the bus — ``MixBus`` does not support
    reentrant calls from any callback.

    Attributes:
        on_track_start: Called with a track the first time one of its
            blocks is emitted.
        on_position: Called after each non-silent, non-paused block with
            the number of frames played so far in the current track.
        on_need_track: Called, while the bus's lock is held, when the bus
            needs the next track to play. Must return immediately — a
            pre-rendered track or ``None`` — never block or call back
            into the bus.
    """

    on_track_start: Callable[[RenderedTrack], None]
    on_position: Callable[[int], None]
    on_need_track: Callable[[], RenderedTrack | None]


class MixBus:
    """Play rendered tracks continuously, one 20 ms block at a time.

    ``render_block`` is pure with respect to wall-clock time — it advances
    the bus by exactly one block and returns the mixed audio — while
    :meth:`run` is the paced loop that calls it in real time and fans the
    result out to every registered :class:`Output`.

    Thread-safety: in production, :meth:`run` is the only caller of
    :meth:`render_block`, and it runs on a single dedicated thread. The
    control methods (:meth:`pause`, :meth:`skip`, :meth:`seek`, :meth:`play_liner`,
    :meth:`start_set`, :meth:`stop_set`, :meth:`add_output`,
    :meth:`remove_output`) are safe to call from other threads; they take
    a plain (non-reentrant) internal lock shared with ``render_block``.
    ``on_need_track`` runs while that lock is held; ``on_track_start`` and
    ``on_position`` are collected while producing a block and invoked
    after the lock is released (see ``BusEvents``). No callback may call
    back into the bus — from ``on_need_track`` that would deadlock
    outright, and from the other two it would reenter ``render_block`` or
    a control method in ways this class does not support.
    """

    BLOCK = 882

    def __init__(
        self,
        events: BusEvents,
        eq_gains: Callable[[], tuple[float, float, float]],
        clock: Clock | None = None,
        skip_tail: Callable[[RenderedTrack, int], SkipTail | None] | None = None,
    ) -> None:
        """Create an idle bus.

        Args:
            events: Callbacks for track starts, position and track supply.
            eq_gains: Returns the current ``(low, mid, high)`` EQ gains.
            clock: Time source for :meth:`run`; defaults to
                :class:`SystemClock`.
            skip_tail: Prepares the skip effect for a render at a
                position, or returns ``None`` for the plain fade.  Called
                by :meth:`skip` on the caller's thread, never on the bus
                thread and never with the bus lock held.
        """
        self._events = events
        self._eq_gains = eq_gains
        self._clock: Clock = clock or SystemClock()
        self._lock = threading.Lock()
        self._outputs: list[Output] = []
        self._master: MasterProcessor | None = None
        self._master_enabled: Callable[[], bool] = lambda: False
        self._current: RenderedTrack | None = None
        self._pos = 0
        self._paused = False
        self._playing = False
        self._skip_fade = 0
        self._skip_tail_maker = skip_tail
        self._tail: SkipTail | None = None
        self._tail_pos = 0
        self._liner_cue: LinerCue | None = None
        self._cued_liner: np.ndarray | None = None
        self._cued_duck = 1.0
        self._liner: np.ndarray | None = None
        self._liner_pos = 0
        self._replaced_liner: np.ndarray | None = None
        self._replaced_liner_pos = 0
        self._duck_gain = 1.0
        self._duck_target = 1.0
        self._duck_step = 0.0
        self._duck_remaining = 0
        self._eq_filters = make_eq_filters(SAMPLE_RATE)
        self._eq_state = make_eq_state(self._eq_filters, channels=2)
        self._eq_previous_gains = (1.0, 1.0, 1.0)
        # Live input (#48): the desk, the crossfade level (0 automation,
        # 1 live) and the jitter buffers' state.  Read on the bus thread only.
        self._live: LiveDesk | None = None
        self._live_gain = 0.0
        self._live_feed: LiveSource | None = None
        self._live_primed = False
        self._mic_feed: PcmBuffer | None = None
        self._mic_primed = False
        self._mic_gain = 1.0
        self._deferred_liner: tuple[np.ndarray, float] | None = None
        self._advance_held = False

    def set_live(self, desk: LiveDesk) -> None:
        """Attach the station's live input (:class:`~autodj.live.LiveDesk`)."""
        with self._lock:
            self._live = desk

    @property
    def live_on_air(self) -> bool:
        """Whether a live source has taken over (automation is held or fading)."""
        return self._live_gain > 0.0

    def drop_held_track(self) -> None:
        """Forget the track held under a live source, so automation returns with the next.

        Called before the live source fades out: the bus then takes the
        next rendered track as the live audio fades, rather than resuming
        the song the DJ interrupted.  Its liner cue and skip effect go too.
        """
        with self._lock:
            self._current = None
            self._pos = 0
            self._tail = None
            self._skip_fade = 0
            self._drop_liner()

    def _live_block(self, desk: LiveDesk, feed: LiveSource) -> np.ndarray | None:
        """One block of *feed*, or ``None`` while it (re)buffers (lock held).

        A jitter buffer: playing starts once ``_LIVE_PREROLL_FRAMES`` are
        waiting, and an underrun goes back to buffering, inserting silence,
        so network bursts do not crackle.  An underrun of the connected
        source is only counted on it (``underruns``); its route logs it, as
        nothing on this thread may block on logging.
        """
        if feed is not self._live_feed:
            self._live_feed = feed
            self._live_primed = False
        waiting = feed.buffer.frames
        if not self._live_primed:
            if waiting < _LIVE_PREROLL_FRAMES:
                return None
            self._live_primed = True
            feed.primed = True
        if waiting < self.BLOCK:
            self._live_primed = False
            if feed is desk.source:
                feed.underruns += 1
            return None
        return feed.buffer.pop(self.BLOCK)

    def _live_taking_over(self) -> bool:
        """Whether a live source has the air, or takes it this block (lock held).

        From then on no new track starts: one ending under the crossfade
        leaves silence beneath the DJ rather than starting the next song,
        which waits, with everything planned for it, until the DJ leaves.
        """
        desk = self._live
        source = desk.source if desk is not None else None
        if source is None or not source.started:
            return False
        return source.primed or source.buffer.frames >= _LIVE_PREROLL_FRAMES

    def _mix_live(self, block: np.ndarray) -> np.ndarray:
        """Crossfade to or from the live source and add talkover (lock held).

        Reads only what the routes have already put in the desk's buffers;
        it never waits for the network or the decoder, and never takes the
        desk's lock.  The source and the music cross with equal power, so
        the level holds through the crossfade.
        """
        desk = self._live
        if desk is None:
            return block
        active = desk.source
        if active is not None and not active.started:
            active = None  # connected, but only silence so far: still off the air
        feed = active if active is not None else desk.return_source
        previous = self._live_gain
        live = None
        if feed is not None and (previous > 0.0 or feed is active):
            live = self._live_block(desk, feed)
        on_air = active is not None and active.primed
        step = self.BLOCK / (SAMPLE_RATE * desk.crossfade_seconds)
        self._live_gain = min(1.0, previous + step) if on_air else max(0.0, previous - step)
        if previous == self._live_gain == 1.0:
            block = live if live is not None else np.zeros_like(block)
        elif previous or self._live_gain:
            ramp = np.linspace(previous, self._live_gain, self.BLOCK, dtype=np.float32)[:, None]
            angle = ramp * np.float32(math.pi / 2)
            block = block * np.cos(angle)
            if live is not None:
                block += live * np.sin(angle)
        returning = desk.return_source
        if self._live_gain == 0.0 and returning is not None:
            desk.clear_return_source(returning)
        if previous and not self._live_gain and self._deferred_liner is not None:
            # The DJ has handed back: the liner that was due under them plays
            # once now, over the music (if any is playing).
            audio, gain = self._deferred_liner
            self._deferred_liner = None
            if self._playing and not self._paused:
                self._start_liner(audio, gain)
        return self._talkover(desk, block)

    def _talkover(self, desk: LiveDesk, block: np.ndarray) -> np.ndarray:
        """Duck *block* under a browser talker and add their voice (lock held)."""
        mic = desk.mic
        if mic is not self._mic_feed:
            self._mic_feed = mic
            self._mic_primed = False
        if mic is not None and not self._mic_primed and mic.frames >= _MIC_PREROLL_FRAMES:
            self._mic_primed = True
        talking = mic is not None and self._mic_primed
        settings = desk.settings
        old = self._mic_gain
        if talking:
            target = float(10 ** (-settings.mic_duck_db / 20))
            change = self.BLOCK / (SAMPLE_RATE * desk.attack_seconds)
            self._mic_gain = max(target, old - change) if old > target else target
        else:
            change = self.BLOCK / (SAMPLE_RATE * settings.mic_return_seconds)
            self._mic_gain = min(1.0, old + change)
        if old != self._mic_gain:
            block = block * np.linspace(old, self._mic_gain, self.BLOCK, dtype=np.float32)[:, None]
        elif old != 1.0:
            block = block * np.float32(old)
        if talking:
            assert mic is not None
            block = block + mic.pop(self.BLOCK)
        return block

    @property
    def playing(self) -> bool:
        """Whether a set is active (started and not stopped)."""
        return self._playing

    def add_output(self, output: Output) -> None:
        """Register *output* to receive every subsequently rendered block."""
        with self._lock:
            self._outputs.append(output)

    def set_master(
        self,
        master: MasterProcessor | None,
        enabled: Callable[[], bool] | bool = True,
    ) -> None:
        """Install the optional continuous processor used by :meth:`run`.

        The processor is owned and closed by its creator. When enabled,
        ``render_block`` leaves headroom/limiting to the master stage.
        """
        predicate = enabled if callable(enabled) else lambda: bool(enabled)
        with self._lock:
            self._master = master
            self._master_enabled = predicate if master is not None else lambda: False

    def _master_state(self) -> tuple[MasterProcessor | None, bool]:
        with self._lock:
            master, predicate = self._master, self._master_enabled
        if master is None:
            return None, False
        try:
            return master, bool(predicate())
        except Exception:
            logger.exception("Mix bus master enable check failed; bypassing master")
            return master, False

    def remove_output(self, output: Output) -> None:
        """Stop sending blocks to *output*, if it is registered."""
        with self._lock:
            if output in self._outputs:
                self._outputs.remove(output)

    def pause(self, paused: bool) -> None:
        """Emit silence and hold position while *paused* is true."""
        with self._lock:
            self._paused = paused

    def skip(self) -> None:
        """End the current track with the skip effect, then move to the next.

        The effect comes from the *skip_tail* callable given to the bus,
        computed here on the caller's thread with the lock released, so
        the bus thread keeps producing blocks meanwhile.  The tail takes
        over where the bus has got to when it is installed.  Without an
        effect (the ``"fade"`` style, no callable, a failure, or a track
        change while it was computed) the track fades out over 150 ms as
        before.  Either way the next track follows in the same block.
        """
        with self._lock:
            if self._current is None or self._skip_fade or self._tail is not None:
                return
            track, pos = self._current, self._pos
        tail = None
        if self._skip_tail_maker is not None:
            try:
                tail = self._skip_tail_maker(track, pos)
            except Exception:
                logger.exception("Skip effect failed; fading out instead")
        with self._lock:
            if self._current is None or self._skip_fade or self._tail is not None:
                return
            if tail is not None and tail.track is self._current:
                self._tail = self._join_tail(tail)
                self._tail_pos = 0
            if self._tail is None:
                self._skip_fade = _SKIP_FADE_FRAMES

    def _join_tail(self, tail: SkipTail) -> SkipTail | None:
        """Fit *tail* to where the bus has got to (lock held).

        The bus may already be past the effect's start, having played on
        while it was computed: the effect then takes over from here, with
        its first frames crossed from the dry audio, so nothing repeats or
        clicks.  ``None`` when it is too late for any of it.
        """
        late = self._pos - tail.start
        if late <= 0:
            return tail
        audio = tail.audio[late:].copy()
        if len(audio) == 0:
            return None
        blend = min(_TAIL_BLEND_FRAMES, len(audio))
        dry = np.zeros((blend, 2), np.float32)
        piece = tail.track.audio[self._pos : self._pos + blend]
        dry[: len(piece)] = piece
        mix = np.linspace(0.0, 1.0, blend, dtype=np.float32)[:, None]
        audio[:blend] = dry * (1.0 - mix) + audio[:blend] * mix
        return SkipTail(tail.track, self._pos, audio)

    def can_replace_unheard_tail(self, expected: RenderedTrack, *, before_frame: int) -> bool:
        """A control-thread preflight; the replacement repeats this atomic check."""
        with self._lock:
            return (
                self._current is expected
                and self._pos < before_frame
                and not self._skip_fade
                and self._tail is None
            )

    def replace_unheard_tail(
        self, expected: RenderedTrack, replacement: RenderedTrack, *, before_frame: int
    ) -> bool:
        """Swap a prepared future tail without seeking or replaying its prefix.

        The control thread renders the replacement and preserves the original
        prefix through ``before_frame`` before calling. No audio work or callbacks
        run under this lock. A raced track change, skip, or reached boundary keeps
        the original audio intact; already committed speech cannot be revoked.
        """
        if (
            replacement.entry is not expected.entry
            or replacement.start_offset != expected.start_offset
            or before_frame < 0
            or len(replacement.audio) < before_frame
        ):
            return False
        with self._lock:
            if (
                self._current is not expected
                or self._pos >= before_frame
                or self._skip_fade
                or self._tail is not None
            ):
                return False
            self._current = replacement
            cue = self._liner_cue
            if cue is not None and cue.track is expected and not cue.started:
                cue.track = replacement
            return True

    def seek(self, frames: int) -> None:
        """Jump to *frames* into the current track.

        Clamped to the track, stopping one frame short of its end so a
        seek never ends the track by itself.  Ignored when nothing is
        playing, and while a skip effect is ending the track (the effect
        was made from the audio where it starts).
        """
        with self._lock:
            if self._current is not None and self._tail is None:
                last = len(self._current.audio) - 1
                self._pos = max(0, min(int(frames), last))

    def play_liner(
        self,
        audio: np.ndarray,
        duck_db: float,
        at: LinerCue | None = None,
        *,
        after_live: bool = False,
    ) -> None:
        """Mix *audio* over the music, ducking the music by *duck_db* dB.

        The music ramps to the duck level — and, once the liner ends,
        back up to full — over 40 ms rather than stepping, so neither
        transition clicks.

        Args:
            audio: ``(n, 2)`` float32 liner audio to mix in starting on
                the next rendered block.
            duck_db: Decibel attenuation applied to the music bed while
                the liner plays (negative values quiet the music).
            at: Start the liner when the bus reaches this cue instead,
                and set its ``started``; a cue whose track stops playing
                first is dropped.  One cue waits at a time: a new one
                replaces it.  A cue already passed starts at once.
            after_live: While a live DJ has the air (#48) nothing is mixed
                over them.  ``True`` (a station ID or other liner) keeps
                the latest such liner to play once when the DJ hands back;
                ``False`` (a time check, which would be stale by then)
                drops it.  Either way the caller's thread logs it.
        """
        audio = audio.astype(np.float32, copy=False)
        gain = float(10 ** (duck_db / 20.0))
        with self._lock:
            under_live = self._live_gain > 0.0 or self._advance_held
            if under_live and after_live:
                self._deferred_liner = (audio, gain)
            if not under_live:
                self._play_liner_locked(audio, gain, at)
        # Logged here, with the bus lock released.
        if under_live and after_live:
            logger.info("A liner was due while a live DJ was on the air; it plays when they leave")
        elif under_live:
            logger.info("A time check was due while a live DJ was on the air; skipped as stale")

    def _play_liner_locked(self, audio: np.ndarray, gain: float, at: LinerCue | None) -> None:
        """Start or cue a liner (lock held)."""
        if at is not None and not (at.track is self._current and self._pos >= at.frame):
            self._liner_cue, self._cued_liner, self._cued_duck = at, audio, gain
            return
        if at is not None:
            at.started = True
            self._liner_cue = None
            self._cued_liner = None
        self._start_liner(audio, gain)

    def _start_liner(self, audio: np.ndarray, gain: float) -> None:
        """Start *audio* on the next block and duck the music to *gain* (lock held)."""
        if self._liner is not None:
            previous = self._replaced_liner
            previous_pos = self._replaced_liner_pos
            self._replaced_liner = np.zeros((_LINER_FADE_FRAMES, 2), dtype=np.float32)
            tail = self._liner[self._liner_pos : self._liner_pos + _LINER_FADE_FRAMES]
            self._replaced_liner[: len(tail)] = tail
            if previous is not None:
                frames = min(_LINER_FADE_FRAMES, len(previous) - previous_pos)
                positions = np.arange(previous_pos, previous_pos + frames)
                old_gain = (1.0 - positions / _LINER_FADE_FRAMES).astype(np.float32)[:, None]
                self._replaced_liner[:frames] *= 1.0 - old_gain
                self._replaced_liner[:frames] += (
                    previous[previous_pos : previous_pos + frames] * old_gain
                )
            self._replaced_liner_pos = 0
        self._liner = audio
        self._liner_pos = 0
        self._start_duck_ramp(gain)

    def stop_liner(self) -> None:
        """Fade out the liner that is playing; the music comes back up when it ends.

        The fade takes 40 ms so the stop does not click. A liner cued for
        later is left waiting.
        """
        with self._lock:
            if self._liner is None:
                return
            tail = self._liner[self._liner_pos : self._liner_pos + _LINER_FADE_FRAMES]
            fade = np.linspace(1.0, 0.0, len(tail), dtype=np.float32)[:, None]
            self._liner = tail * fade
            self._liner_pos = 0

    def _check_cue(self, track: RenderedTrack, pos: int) -> None:
        """Start the waiting liner once *track* has played to its cue (lock held)."""
        cue = self._liner_cue
        if cue is None or cue.track is not track or pos < cue.frame:
            return
        audio = self._cued_liner
        self._liner_cue = None
        self._cued_liner = None
        if audio is not None:
            cue.started = True
            self._start_liner(audio, self._cued_duck)

    def start_set(self) -> None:
        """Begin (or resume) pulling tracks from ``on_need_track``.

        A liner still waiting from before (queued while nothing was
        rendered) is dropped, so a new set never opens with a stale one.
        """
        with self._lock:
            self._playing = True
            self._paused = False
            self._drop_liner()

    def stop_set(self) -> None:
        """Drop the current track, any skip fade and liner; emit silence until restarted."""
        with self._lock:
            self._playing = False
            self._current = None
            self._pos = 0
            self._skip_fade = 0
            self._tail = None
            self._drop_liner()
            reset_eq_state(self._eq_state)
            self._eq_previous_gains = (1.0, 1.0, 1.0)

    def _drop_liner(self) -> None:
        """Forget any liner and put the music back at full level (lock held)."""
        self._liner = None
        self._replaced_liner = None
        self._liner_cue = None
        self._cued_liner = None
        self._duck_gain = 1.0
        self._duck_target = 1.0
        self._duck_step = 0.0
        self._duck_remaining = 0

    def _advance(self, pending: list[Callable[[], None]]) -> None:
        """Pull the next track and queue announcing it, holding the bus lock."""
        self._current = self._events.on_need_track()
        self._pos = 0
        self._skip_fade = 0
        self._tail = None
        if self._liner_cue is not None and self._liner_cue.track is not self._current:
            self._liner_cue = None  # its track ended before the cue
            self._cued_liner = None
        if self._current is not None:
            track = self._current
            pending.append(lambda: self._events.on_track_start(track))

    def _music(self, frames: int, pending: list[Callable[[], None]]) -> np.ndarray:
        """Fill *frames* of music from the current (and subsequent) tracks.

        A block that lies wholly inside the playing track, with no skip
        fade or effect, is returned as a read-only view of the track's
        audio: copying it would give up the GIL (NumPy releases it for
        copies this size), which costs a whole switch interval while
        another thread is busy.  Callers must not write to the result.
        """
        current = self._current
        if (
            self._playing
            and current is not None
            and self._tail is None
            and not self._skip_fade
            and self._pos + frames <= len(current.audio)
        ):
            chunk = current.audio[self._pos : self._pos + frames]
            self._pos += frames
            self._check_cue(current, self._pos)
            if self._pos >= len(current.audio):
                self._current = None
            return chunk
        out = np.zeros((frames, 2), np.float32)
        filled = 0
        while filled < frames and self._playing:
            if self._current is None:
                if self._advance_held:
                    break  # a live DJ is taking the air: the next song waits for them
                self._advance(pending)
                if self._current is None:
                    break
            if self._tail is not None and self._pos >= self._tail.start:
                filled += self._play_tail(out[filled:frames])
                continue
            audio = self._current.audio
            take = min(frames - filled, len(audio) - self._pos)
            if self._tail is not None:
                take = min(take, self._tail.start - self._pos)
            fading = self._skip_fade > 0
            if fading:
                take = min(take, self._skip_fade)
            chunk = audio[self._pos : self._pos + take]
            if fading:
                ramp = np.clip(
                    (self._skip_fade - np.arange(take)) / _SKIP_FADE_FRAMES, 0.0, 1.0
                ).astype(np.float32)
                chunk = chunk * ramp[:, None]
                self._skip_fade -= take
            out[filled : filled + take] = chunk
            self._pos += take
            filled += take
            self._check_cue(self._current, self._pos)
            if fading and self._skip_fade == 0:
                # Fade just completed: abandon the rest of this track now,
                # in the same block, so the next track's audio starts right
                # where the fade left off instead of leaving a silent gap.
                self._current = None
            elif self._pos >= len(audio):
                self._current = None
        return out

    def _play_tail(self, out: np.ndarray) -> int:
        """Fill *out* from the skip effect; end the track with it (lock held).

        Returns:
            The frames written.
        """
        tail = self._tail
        assert tail is not None and self._current is not None
        take = min(len(out), len(tail.audio) - self._tail_pos)
        out[:take] = tail.audio[self._tail_pos : self._tail_pos + take]
        self._tail_pos += take
        self._pos = min(self._pos + take, len(self._current.audio))
        self._check_cue(self._current, self._pos)
        if self._tail_pos >= len(tail.audio):
            # Like the end of a skip fade: the next track starts right here.
            self._tail = None
            self._current = None
        return take

    def _start_duck_ramp(self, target: float) -> None:
        """(Re)start a linear duck-gain ramp from the current gain to *target*.

        Fixes the per-sample step once, at the moment the target changes,
        so the full swing always takes exactly ``_DUCK_RAMP_FRAMES``
        (250 ms) regardless of how many blocks it is consumed across.
        """
        self._duck_target = target
        self._duck_step = (target - self._duck_gain) / _DUCK_RAMP_FRAMES
        self._duck_remaining = _DUCK_RAMP_FRAMES

    def _duck_and_mix(self, block: np.ndarray) -> None:
        """Apply the duck gain and add the liner to *block*, in place (lock held).

        The duck moves linearly at the fixed rate :meth:`_start_duck_ramp`
        set, then holds at the target once the ramp is spent — never
        stepping instantly and clicking.  Done in one compiled pass (see
        :mod:`autodj.kernels`); at full level with no liner it is skipped.
        A block may contain the ramp's final frames followed by its held
        target; ``ramp_frames`` bounds the compiled ramp to the exact end.
        """
        ramp_frames = max(0, min(self._duck_remaining, len(block)))
        if not ramp_frames and self._duck_target == 1.0 and self._liner is None:
            return
        liner = self._liner if self._liner is not None else _NO_LINER
        duck_and_mix_in_place(
            block,
            self._duck_gain,
            self._duck_step,
            ramp_frames,
            self._duck_target,
            liner,
            self._liner_pos,
        )
        if self._replaced_liner is not None:
            remaining = len(self._replaced_liner) - self._replaced_liner_pos
            frames = min(len(block), remaining)
            if frames:
                positions = np.arange(self._replaced_liner_pos, self._replaced_liner_pos + frames)
                old_gain = (1.0 - positions / _LINER_FADE_FRAMES).astype(np.float32)[:, None]
                block[:frames] += (
                    self._replaced_liner[
                        self._replaced_liner_pos : self._replaced_liner_pos + frames
                    ]
                    * old_gain
                )
                if self._liner is not None:
                    new = self._liner[self._liner_pos : self._liner_pos + frames]
                    block[: len(new)] -= new * old_gain[: len(new)]
            self._replaced_liner_pos += frames
            if self._replaced_liner_pos >= len(self._replaced_liner):
                self._replaced_liner = None
        if ramp_frames:
            self._duck_gain = self._duck_gain + self._duck_step * ramp_frames
            self._duck_remaining -= ramp_frames
            if self._duck_remaining <= 0:
                self._duck_gain = self._duck_target  # ramp complete: eliminate float drift
        if self._liner is not None:
            self._liner_pos += self.BLOCK
            if self._liner_pos >= len(self._liner):
                self._liner = None
                self._start_duck_ramp(1.0)

    def render_block(self, master_enabled: bool | None = None) -> np.ndarray:
        """Return the next ``(882, 2)`` block without pacing.

        Pure with respect to wall-clock time: advances internal state by
        exactly one block. Used by :meth:`run` and directly by tests.
        """
        if master_enabled is None:
            _master, master_enabled = self._master_state()
        pending: list[Callable[[], None]] = []
        with self._lock:
            desk = self._live
            live_input = desk is not None and (
                desk.active or desk.return_source is not None or self._mic_gain < 1.0
            )
            if (self._paused or not self._playing) and not live_input:
                return np.zeros((self.BLOCK, 2), np.float32)
            # While a live source has the air the automation is held where
            # it is: no track advances, so the clockwheel, the repeat
            # history and the planned song change stay as they were.
            self._advance_held = self._live_taking_over()
            if self._live_gain >= 1.0 or self._paused or not self._playing:
                # No music under live input: the EQ is skipped rather than
                # fed silence (its filters' decaying tails would go
                # denormal and cost a hundred times more), and starts clean.
                block = np.zeros((self.BLOCK, 2), np.float32)
                reset_eq_state(self._eq_state)
            else:
                block = self._music(self.BLOCK, pending)
                gains = self._eq_gains()
                # Keep filter memory current even at unity gain.  Both EQ calls
                # return a new array, so the music view is never written to.
                if gains != self._eq_previous_gains:
                    # Ramp slider changes over this 20 ms block to avoid clicks.
                    block = ramp_eq(
                        block, self._eq_filters, self._eq_previous_gains, gains, self._eq_state
                    )
                else:
                    block = apply_eq(block, self._eq_filters, *gains, state=self._eq_state)
                self._eq_previous_gains = gains
            self._duck_and_mix(block)
            block = self._mix_live(block)
            if not master_enabled:
                np.clip(block, -1.0, 1.0, out=block)
            if self._current is not None:
                pos = self._pos
                pending.append(lambda: self._events.on_position(pos))
            result = block.astype(np.float32, copy=False)
        for callback in pending:
            callback()
        return result

    def run(self, stop: threading.Event) -> None:
        """Emit blocks in real time until *stop* is set.

        Calls :meth:`render_block` once per 20 ms period, paced by the
        bus's clock, and writes the result to every registered output.
        A block that comes out late (another thread held the GIL, say) is
        made up by rendering the next ones back to back, up to
        :data:`CATCH_UP_BLOCKS` of them; a longer stall drops the rest of
        the backlog, so a burst never drains a master processor's cushion.
        If ``render_block`` itself raises — from a callback,
        ``on_need_track``, or the EQ path — the failure is logged and a
        silent block is substituted so pacing continues. An output whose
        ``write`` raises is logged and removed so the rest of the run
        keeps going.

        Args:
            stop: Set by another thread to end the loop.
        """
        period = self.BLOCK / SAMPLE_RATE
        next_due = self._clock.now()
        while not stop.is_set():
            master, master_enabled = self._master_state()
            try:
                # Warm the processor without advancing tracks, liners, or their callbacks.
                block = (
                    np.zeros((self.BLOCK, 2), np.float32)
                    if master is not None and master_enabled and master.startup_pending
                    else self.render_block(master_enabled=master_enabled)
                )
            except Exception:
                logger.exception("Mix bus render_block failed; emitting silence")
                block = np.zeros((self.BLOCK, 2), np.float32)
            if master is not None:
                try:
                    processed = np.asarray(
                        master.process(block, enabled=master_enabled), np.float32
                    )
                    if processed.shape != (self.BLOCK, 2):
                        raise ValueError("master returned an invalid block shape")
                    block = processed
                except Exception:
                    logger.exception("Mix bus master failed; emitting bounded dry audio")
                nan_to_num_in_place(block)
                np.clip(block, -1.0, 1.0, out=block)
            with self._lock:
                outputs = list(self._outputs)
            for output in outputs:
                try:
                    output.write(block)
                except Exception:
                    logger.exception("Mix bus output failed; removing it")
                    self.remove_output(output)
            next_due += period
            delay = next_due - self._clock.now()
            if delay > 0:
                self._clock.sleep(delay)
            elif delay < -CATCH_UP_BLOCKS * period:
                # Make up at most CATCH_UP_BLOCKS: a longer burst could drain
                # a healthy processor's output cushion.  Whole periods are
                # dropped, so the schedule keeps its phase.
                next_due += (math.ceil(-delay / period) - CATCH_UP_BLOCKS) * period
