"""Render the next track in the background so the mix bus never waits."""

from __future__ import annotations

import functools
import logging
import threading
from collections.abc import Callable

from autodj.mixbus import RenderedTrack

logger = logging.getLogger(__name__)

TrackHook = Callable[[RenderedTrack], None]

# Pause before trying again after a render returned ``None`` or raised.
_RETRY_SECONDS = 1.0


class RenderAhead:
    """Keep at most one rendered track ready on a daemon thread.

    The mix bus asks for its next track while holding its lock, so
    ``on_need_track`` must return at once, but rendering a track (decode,
    beat-match, transition effect) takes seconds.  This worker renders
    the next track as soon as the previous one is taken, and :meth:`pop`
    only ever hands over a finished result -- or ``None`` when nothing is
    rendered yet, in which case the bus plays silence briefly.

    The *render* callable owns the "what comes next" cursor (for the
    player, ``Player._next_rendered``).  Two ways to change course:

    * :meth:`reset` -- a hard jump (new set, "play now"): everything
      rendered is dropped and the cursor moves before the next render.
      The bus may play silence until the new track is ready.
    * :meth:`refresh` -- a soft re-check after a queue edit: a rendered
      track that *is_stale* judges out of date is re-rendered, but kept
      as a fallback until its replacement is ready, so a refresh never
      causes dead air.  If the bus needs a track first it gets the
      fallback, the replacement is dropped, and the edit takes effect one
      track later.

    Hooks (all called with the worker's lock released, except *rewind*
    and *skip_past*, which only move the cursor):

    * *is_stale(track)*: whether a rendered track no longer matches what
      should follow (e.g. the queue head it picked was removed).
    * *rewind(track)*: point the cursor back at *track*'s own start, so
      the next render re-renders it with a fresh pick.
    * *skip_past(track)*: point the cursor just after *track* (used when
      the bus takes a fallback).
    """

    def __init__(
        self,
        render: Callable[[], RenderedTrack | None],
        is_stale: Callable[[RenderedTrack], bool],
        rewind: TrackHook,
        skip_past: TrackHook,
    ) -> None:
        """Create an idle worker; call :meth:`start` to begin rendering.

        Args:
            render: Produces the next track, or ``None`` when nothing
                could be rendered.  Called only on the worker thread.
            is_stale: See the class docstring.
            rewind: See the class docstring.
            skip_past: See the class docstring.
        """
        self._render = render
        self._is_stale = is_stale
        self._rewind = rewind
        self._skip_past = skip_past
        self._cond = threading.Condition()
        self._ready: RenderedTrack | None = None
        self._fallback: RenderedTrack | None = None
        self._rendering = False
        self._revalidate = False
        self._generation = 0
        self._deferred: list[Callable[[], None]] = []
        self._stopped = True
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the worker thread, unless one is already running.

        A worker that has been told to stop but is still finishing its
        render keeps going (it sees the cleared stop flag); one that has
        already decided to exit has cleared ``_thread``, so a fresh
        thread is started instead.
        """
        with self._cond:
            self._stopped = False
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._loop, name="autodj-render-ahead", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the worker to exit and wait up to *timeout* seconds for it.

        A render already in flight finishes first (it cannot be
        interrupted); pass ``timeout=0`` to signal without waiting.
        """
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None and timeout > 0 and thread is not threading.current_thread():
            thread.join(timeout)

    def pop(self) -> RenderedTrack | None:
        """Take the next track without blocking, or return ``None``.

        Returns the ready track; failing that, a fallback kept by
        :meth:`refresh` (whose replacement is then dropped, and the cursor
        moved past the fallback).  Safe as the mix bus's ``on_need_track``:
        it only takes this worker's own short lock.
        """
        with self._cond:
            track, self._ready = self._ready, None
            if track is None and self._fallback is not None:
                track, self._fallback = self._fallback, None
                # Anything rendering now re-renders the track that is about
                # to play: drop it and carry on from after the fallback.
                self._generation += 1
                fallback = track
                self._apply(lambda: self._skip_past(fallback))
            if track is not None:
                self._cond.notify_all()
            return track

    def wait_ready(self, timeout: float) -> bool:
        """Block up to *timeout* seconds until :meth:`pop` would return a track.

        Returns:
            Whether a track (ready or fallback) is available.
        """
        with self._cond:
            return self._cond.wait_for(
                lambda: self._ready is not None or self._fallback is not None, timeout
            )

    def reset(self, prepare: Callable[[], None]) -> None:
        """Hard jump: drop everything rendered and restart after *prepare*.

        *prepare* moves the render cursor (e.g. to the first track of a
        new set).  When the worker is idle it runs right away.  While a
        render is in flight (or earlier moves are still pending) it is
        queued: the in-flight render is discarded when it finishes, and
        the queued moves run in order on the worker thread just before
        the next render, so the old render cannot overwrite the cursor.
        """
        with self._cond:
            self._ready = None
            self._fallback = None
            self._generation += 1
            self._apply(prepare)
            self._cond.notify_all()

    def refresh(self) -> None:
        """Soft re-check after a queue edit; never causes dead air.

        A ready track that *is_stale* judges out of date becomes the
        fallback and is re-rendered from its own start.  A render in
        flight is judged when it finishes.  With nothing rendered there is
        nothing to do: the next render sees the edit anyway.
        """
        with self._cond:
            if self._rendering:
                self._revalidate = True
                return
            ready = self._ready
        if ready is None or not self._is_stale(ready):
            return
        with self._cond:
            if self._ready is not ready:
                return  # the bus took it while it was being judged
            self._ready = None
            self._fallback = ready
            self._apply(lambda: self._rewind(ready))
            self._cond.notify_all()

    def _apply(self, move: Callable[[], None]) -> None:
        """Run a cursor move now if the worker is idle, else queue it (lock held)."""
        if self._rendering or self._deferred:
            self._deferred.append(move)
        else:
            move()

    def _loop(self) -> None:
        """Worker body: render whenever no track is waiting."""
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._stopped or self._ready is None)
                if self._stopped:
                    # Only one worker is ever assigned at a time (start() never
                    # replaces a live one), so this is always our own entry;
                    # clearing it lets start() launch a fresh worker.
                    self._thread = None
                    return
                pending, self._deferred = self._deferred, []
                for move in pending:
                    move()
                self._rendering = True
                self._revalidate = False
                generation = self._generation
            track: RenderedTrack | None = None
            try:
                track = self._render()
            except Exception:
                logger.exception("Rendering the next track failed")
            self._settle(track, generation)

    def _settle(self, track: RenderedTrack | None, generation: int) -> None:
        """Decide what becomes of a finished render.

        ``_rendering`` stays set until the decision is made, so a
        :meth:`refresh` that arrives meanwhile is never lost: it flags the
        render for (re-)judging instead.  Judging runs with the lock
        released because *is_stale* takes the player's queue lock.
        """
        while True:
            with self._cond:
                if generation != self._generation:
                    self._rendering = False  # a reset or fallback pop made it stale
                    return
                if track is None or not self._revalidate:
                    self._rendering = False
                    if track is not None:
                        self._ready = track
                        self._fallback = None
                        self._cond.notify_all()
                    elif not self._stopped:
                        self._cond.wait(_RETRY_SECONDS)
                    return
                self._revalidate = False
            if self._is_stale(track):
                with self._cond:
                    self._rendering = False
                    if generation == self._generation:
                        # Out of date already: keep it to fall back on and
                        # render it again from its start.
                        self._fallback = track
                        self._apply(functools.partial(self._rewind, track))
                        self._cond.notify_all()
                return
