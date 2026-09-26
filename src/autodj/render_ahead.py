"""Render the next track in the background so the mix bus never waits."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from autodj.mixbus import RenderedTrack

logger = logging.getLogger(__name__)


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
    """

    def __init__(
        self,
        render: Callable[[], RenderedTrack | None],
        retry_seconds: float = 1.0,
    ) -> None:
        """Create an idle worker; call :meth:`start` to begin rendering.

        Args:
            render: Produces the next track, or ``None`` when nothing
                could be rendered.  Called only on the worker thread.
            retry_seconds: Pause before trying again after *render*
                returned ``None`` or raised.
        """
        self._render = render
        self._retry_seconds = retry_seconds
        self._cond = threading.Condition()
        self._ready: RenderedTrack | None = None
        self._rendering = False
        self._generation = 0
        self._deferred: Callable[[], None] | None = None
        self._stopped = True
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the worker thread, unless it is already running."""
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

    def reset(self, prepare: Callable[[], None]) -> None:
        """Discard any ready track and restart rendering after *prepare*.

        *prepare* moves the render cursor (e.g. to the first track of a
        new set).  When no render is running it is applied right away;
        otherwise the in-flight render is discarded when it finishes and
        *prepare* runs on the worker thread just before the next one, so
        the old render cannot overwrite the new cursor.
        """
        with self._cond:
            self._ready = None
            self._generation += 1
            if self._rendering:
                self._deferred = prepare
            else:
                self._deferred = None
                prepare()
            self._cond.notify_all()

    def _loop(self) -> None:
        """Worker body: render whenever no track is waiting."""
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._stopped or self._ready is None)
                if self._stopped:
                    return
                self._rendering = True
                generation = self._generation
                prepare, self._deferred = self._deferred, None
            track: RenderedTrack | None = None
            try:
                if prepare is not None:
                    prepare()
                track = self._render()
            except Exception:
                logger.exception("Rendering the next track failed")
            with self._cond:
                self._rendering = False
                if generation != self._generation:
                    continue  # a reset arrived mid-render: this result is stale
                if track is not None:
                    self._ready = track
                    self._cond.notify_all()
                elif not self._stopped:
                    self._cond.wait(self._retry_seconds)
