"""Render the next track in the background so the mix bus never waits."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from autodj.mixbus import RenderedTrack

logger = logging.getLogger(__name__)

Prepare = Callable[[RenderedTrack | None], None]
"""Moves the render cursor; receives the render a reset threw away, if any."""


class RenderAhead:
    """Keep at most one rendered track ready on a daemon thread.

    The mix bus asks for its next track while holding its lock, so
    ``on_need_track`` must return at once, but rendering a track (decode,
    beat-match, transition effect) takes seconds.  This worker renders
    the next track as soon as the previous one is taken, and :meth:`pop`
    only ever hands over a finished result -- or ``None`` when the render
    is still running, in which case the bus plays silence briefly.

    The *render* callable owns the "what comes next" cursor (for the
    player, ``Player._next_rendered``).  :meth:`reset` moves that cursor
    safely even while a render is in flight: the in-flight result is
    thrown away and the move is applied before the next render starts.

    Lock order: the worker's lock is never held while calling
    *on_discard* or *render*, so those may take other locks (the player's
    queue lock) without risking a deadlock with :meth:`pop`.
    """

    def __init__(
        self,
        render: Callable[[], RenderedTrack | None],
        retry_seconds: float = 1.0,
        on_discard: Callable[[RenderedTrack], None] | None = None,
    ) -> None:
        """Create an idle worker; call :meth:`start` to begin rendering.

        Args:
            render: Produces the next track, or ``None`` when nothing
                could be rendered.  Called only on the worker thread.
            retry_seconds: Pause before trying again after *render*
                returned ``None`` or raised.
            on_discard: Called with every rendered track a reset throws
                away (ready or in flight), outside the worker's lock --
                e.g. to give a queue pick it consumed back to the queue.
        """
        self._render = render
        self._retry_seconds = retry_seconds
        self._on_discard = on_discard
        self._cond = threading.Condition()
        self._ready: RenderedTrack | None = None
        self._rendering = False
        self._generation = 0
        self._deferred: list[Prepare] = []
        self._stale: RenderedTrack | None = None
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
        """Take the ready track without blocking, or return ``None``.

        Safe as the mix bus's ``on_need_track``: it only takes this
        worker's own short lock and never waits on a render.
        """
        with self._cond:
            track, self._ready = self._ready, None
            if track is not None:
                self._cond.notify_all()
            return track

    def wait_ready(self, timeout: float) -> bool:
        """Block up to *timeout* seconds for a track to be ready.

        Returns:
            Whether a track is ready to :meth:`pop`.
        """
        with self._cond:
            return self._cond.wait_for(lambda: self._ready is not None, timeout)

    def reset(self, prepare: Prepare) -> None:
        """Discard any ready track and restart rendering after *prepare*.

        *prepare* moves the render cursor (e.g. to the first track of a
        new set) and receives the render this reset threw away, or
        ``None``.  When the worker is idle it runs right away with the
        discarded ready track.  While a render is in flight (or earlier
        resets are still pending) it is queued: the in-flight render is
        discarded when it finishes, and the queued prepares run in order
        on the worker thread just before the next render -- the first one
        receiving the discarded in-flight render -- so the old render
        cannot overwrite the new cursor.

        Callers that need queue order preserved should hold the queue
        lock around this call: *on_discard* then runs before the worker
        can pick from the queue again.
        """
        with self._cond:
            discarded, self._ready = self._ready, None
            self._generation += 1
            if self._rendering or self._deferred:
                self._deferred.append(prepare)
            else:
                prepare(discarded)
            self._cond.notify_all()
        if discarded is not None and self._on_discard is not None:
            self._on_discard(discarded)

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
                stale, self._stale = self._stale, None
                pending, self._deferred = self._deferred, []
                for index, prepare in enumerate(pending):
                    prepare(stale if index == 0 else None)
                self._rendering = True
                generation = self._generation
            track: RenderedTrack | None = None
            try:
                track = self._render()
            except Exception:
                logger.exception("Rendering the next track failed")
            discarded: RenderedTrack | None = None
            with self._cond:
                self._rendering = False
                if generation != self._generation:
                    # A reset arrived mid-render: this result is stale.
                    discarded = self._stale = track
                elif track is not None:
                    self._ready = track
                    self._cond.notify_all()
                elif not self._stopped:
                    self._cond.wait(self._retry_seconds)
            if discarded is not None and self._on_discard is not None:
                self._on_discard(discarded)
