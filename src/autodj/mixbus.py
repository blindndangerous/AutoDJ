"""Clock-paced stereo mix bus for server-side playback."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import numpy as np

from autodj.eq import apply_eq, make_eq_filters, make_eq_state, reset_eq_state
from autodj.stereo import SAMPLE_RATE

if TYPE_CHECKING:
    from autodj.beatmatch import Glide
    from autodj.indexer import IndexEntry

logger = logging.getLogger(__name__)

_SKIP_FADE_FRAMES = int(0.15 * SAMPLE_RATE)
_DUCK_RAMP_FRAMES = int(0.04 * SAMPLE_RATE)
# Frames over which a skip effect that is installed late takes over.
_TAIL_BLEND_FRAMES = int(0.01 * SAMPLE_RATE)


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
        self._duck_gain = 1.0
        self._duck_target = 1.0
        self._duck_step = 0.0
        self._duck_remaining = 0
        self._eq_filters = make_eq_filters(SAMPLE_RATE)
        self._eq_state = make_eq_state(self._eq_filters, channels=2)
        self._eq_engaged = False

    @property
    def playing(self) -> bool:
        """Whether a set is active (started and not stopped)."""
        return self._playing

    def add_output(self, output: Output) -> None:
        """Register *output* to receive every subsequently rendered block."""
        with self._lock:
            self._outputs.append(output)

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

    def play_liner(self, audio: np.ndarray, duck_db: float, at: LinerCue | None = None) -> None:
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
        """
        audio = audio.astype(np.float32, copy=False)
        gain = float(10 ** (duck_db / 20.0))
        with self._lock:
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
        self._liner = audio
        self._liner_pos = 0
        self._start_duck_ramp(gain)

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

    def _drop_liner(self) -> None:
        """Forget any liner and put the music back at full level (lock held)."""
        self._liner = None
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
        """Fill *frames* of music from the current (and subsequent) tracks."""
        out = np.zeros((frames, 2), np.float32)
        filled = 0
        while filled < frames and self._playing:
            if self._current is None:
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
        (40 ms) regardless of how many blocks it is consumed across.
        """
        self._duck_target = target
        self._duck_step = (target - self._duck_gain) / _DUCK_RAMP_FRAMES
        self._duck_remaining = _DUCK_RAMP_FRAMES

    def _duck_ramp(self, frames: int) -> np.ndarray:
        """Return the next ``(frames,)`` samples of the duck-gain ramp.

        Moves linearly at the fixed rate :meth:`_start_duck_ramp` set, then
        holds at the target once the ramp is spent — never stepping
        instantly and clicking. Assumes *frames* never exceeds the ramp
        frames remaining while the ramp is still in progress, which holds
        here because ``_DUCK_RAMP_FRAMES`` is a whole multiple of
        ``MixBus.BLOCK`` and every call passes exactly one block.
        """
        if self._duck_remaining <= 0:
            return np.full(frames, self._duck_target, np.float32)
        steps = self._duck_gain + self._duck_step * np.arange(1, frames + 1, dtype=np.float64)
        self._duck_gain = float(steps[-1])
        self._duck_remaining -= frames
        if self._duck_remaining <= 0:
            self._duck_gain = self._duck_target  # ramp complete: eliminate float drift
        return steps.astype(np.float32)

    def render_block(self) -> np.ndarray:
        """Return the next ``(882, 2)`` block without pacing.

        Pure with respect to wall-clock time: advances internal state by
        exactly one block. Used by :meth:`run` and directly by tests.
        """
        pending: list[Callable[[], None]] = []
        with self._lock:
            if self._paused or not self._playing:
                return np.zeros((self.BLOCK, 2), np.float32)
            block = self._music(self.BLOCK, pending)
            low, mid, high = self._eq_gains()
            engaged = (low, mid, high) != (1.0, 1.0, 1.0)
            if engaged and not self._eq_engaged:
                reset_eq_state(self._eq_state)
            self._eq_engaged = engaged
            if engaged:
                block = apply_eq(block, self._eq_filters, low, mid, high, state=self._eq_state)
            duck = self._duck_ramp(len(block))
            block = block * duck[:, None]
            if self._liner is not None:
                piece = self._liner[self._liner_pos : self._liner_pos + self.BLOCK]
                block[: len(piece)] += piece
                self._liner_pos += self.BLOCK
                if self._liner_pos >= len(self._liner):
                    self._liner = None
                    self._start_duck_ramp(1.0)
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
            try:
                block = self.render_block()
            except Exception:
                logger.exception("Mix bus render_block failed; emitting silence")
                block = np.zeros((self.BLOCK, 2), np.float32)
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
            elif delay < -1.0:
                next_due = self._clock.now()
