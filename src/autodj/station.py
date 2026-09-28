"""Stream-mode set lifecycle driven by the listener count.

The station starts idle.  The first listener starts a new set from the
start of a track (the user's next pick if there is one, otherwise a
shuffle pick); once nobody has listened for the idle grace period the set
stops, and the next listener starts a new one.  A paused set is held
whatever the listener count.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class Station:
    """Start a set on the first listener; stop it after the idle grace period.

    Thread-safety: :meth:`listener_changed` runs on whichever thread
    changed the listener set (often the asyncio loop) and :meth:`tick` on
    a worker thread, so both decide under one short lock.  Starting or
    stopping a set only moves the render cursor and flips the bus, which
    is quick, so neither call blocks for long.  The bus and the render
    cursor are never touched while the player's queue lock is held.
    """

    def __init__(
        self,
        bus: Any,
        stream: Any,
        player: Any,
        idle_grace: float,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[str], None] | None = None,
        forget_track: Callable[[Any], None] | None = None,
    ) -> None:
        """Create an idle station.

        Args:
            bus: The mix bus (``playing``, ``start_set``, ``stop_set``).
            stream: The stream output; its ``listener_count`` is re-read on
                every :meth:`tick` in case a notification was missed.
            player: The player (queue, ``begin_set``, ``end_set``,
                ``_random_start_entry``).
            idle_grace: Seconds without listeners before the set stops.
            clock: Monotonic seconds.
            on_event: Receives ``"set_started"`` and ``"set_stopped"``.
            forget_track: Removes a cut-short track from the session history.
        """
        self._bus = bus
        self._stream = stream
        self._player = player
        self._grace = float(idle_grace)
        self._clock = clock
        self._on_event = on_event or (lambda _event: None)
        self._forget = forget_track or (lambda _entry: None)
        self._lock = threading.Lock()
        self._listeners = 0
        self._empty_since: float | None = None
        self._warned_empty = False
        # Set while idle by "Play now", "Random" or --seed: the next set
        # starts with it instead of the queue or a shuffle pick.
        self._start_entry: Any = None
        self._start_mode = "queue"

    @property
    def state(self) -> str:
        """``"idle"``, ``"playing"`` or ``"paused"``."""
        if not self._bus.playing:
            return "idle"
        return "paused" if self._player._state.is_paused else "playing"

    def listener_changed(self, count: int) -> None:
        """React to the stream's listener count changing.

        Wired to ``StreamOutput.on_listener_change``.  Cheap: at most it
        starts a set, which never waits for a render.

        Args:
            count: The current number of listeners.
        """
        with self._lock:
            self._apply(count)

    def tick(self) -> None:
        """Catch up on the listener count and stop an unheard set.

        Call about once a second.  The set stops once nobody has listened
        for the idle grace period; time spent paused does not count, so a
        resumed set gets the full grace period again.
        """
        count = self._stream.listener_count
        with self._lock:
            self._apply(count)
            if self._listeners > 0 or not self._bus.playing:
                return
            now = self._clock()
            if self._player._state.is_paused or self._empty_since is None:
                self._empty_since = now
            elif now - self._empty_since >= self._grace:
                self._stop()

    def start_with(self, entry: Any, pick_mode: str) -> bool:
        """Make *entry* the first track of the next set, if the station is idle.

        Args:
            entry: The track the next set starts with (used once).
            pick_mode: How it was chosen (``"queue"`` or ``"seed"``).

        Returns:
            ``False`` when a set is already playing (nothing is changed),
            so the caller can play *entry* in that set instead.
        """
        with self._lock:
            if self._bus.playing:
                return False
            self._start_entry, self._start_mode = entry, pick_mode
            return True

    def _apply(self, count: int) -> None:
        """Record *count*; start a set for a listener, time an empty one (lock held)."""
        self._listeners = count
        if count > 0:
            self._empty_since = None
            if not self._bus.playing:
                self._start()
        elif self._empty_since is None:
            self._empty_since = self._clock()

    def _next_pick(self) -> Any:
        """Take the play-next pick or the queue head, if any (queue lock only)."""
        state = self._player._state
        with state.queue_lock:
            if state.queued_next is not None:
                entry, state.queued_next = state.queued_next, None
                return entry
            if state.queue:
                return state.queue.pop(0)
        return None

    def _start(self) -> None:
        """Begin a new set (station lock held, queue lock not held)."""
        entry, pick_mode = self._start_entry, self._start_mode
        if entry is None:
            entry, pick_mode = self._next_pick(), "queue"
        if entry is None:
            entry = self._player._random_start_entry()
            pick_mode = "seed"
        if entry is None:
            if not self._warned_empty:
                logger.warning("A listener is waiting but the library has no tracks yet")
                self._warned_empty = True
            return
        self._warned_empty = False
        self._start_entry = None
        self._player.begin_set(entry, pick_mode)
        self._bus.start_set()
        logger.info("Stream set started with %s", getattr(entry, "display_name", entry))
        self._on_event("set_started")

    def _stop(self) -> None:
        """End the set and forget the track it cut short (station lock held)."""
        self._bus.stop_set()
        current = self._player.end_set()
        if current is not None:
            self._forget(current)
        self._empty_since = None
        logger.info("Stream set stopped: no listeners for %.0f s", self._grace)
        self._on_event("set_stopped")
