"""Prepare one future handoff for live server playout without stale fallbacks."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from autodj.station_log import station_thread

logger = logging.getLogger(__name__)
Move = Callable[[], None]

_RETRY_SECONDS = 1.0


class LivePlayout[T]:
    """Own the background preparation of the next live handoff.

    Selection and preparation run on one worker. The bus-facing ``pop`` and
    ``ready`` calls only take this object's short condition lock. Cursor moves,
    maintenance and cleanup callbacks always run without it.
    Unlike local render-ahead, a changed live handoff has no stale fallback.
    """

    def __init__(
        self,
        prepare: Callable[[], T | None],
        rewind: Callable[[T], None],
        discard: Callable[[T], None] | None = None,
        maintain: Callable[[T | None], None] | None = None,
    ) -> None:
        self._prepare = prepare
        self._rewind = rewind
        self._discard = discard
        self._maintain = maintain
        self._cond = threading.Condition()
        self._ready: T | None = None
        self._thread: threading.Thread | None = None
        self._stopped = True
        self._busy = False
        self._generation = 0
        self._need_prepare = True
        self._maintenance_due = False
        self._refresh_inflight = False
        self._deferred: list[Move] = []

    def start(self) -> None:
        """Start the worker, or resume one that is finishing a stopped call."""
        with self._cond:
            self._stopped = False
            if self._thread is not None and self._thread.is_alive():
                self._cond.notify_all()
                return
            self._thread = station_thread(
                target=self._loop, name="autodj-live-playout", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop and optionally wait; in-flight preparation is discarded on return."""
        with self._cond:
            self._stopped = True
            self._generation += 1
            self._need_prepare = True
            self._refresh_inflight = False
            track, self._ready = self._ready, None
            self._cond.notify_all()
            thread = self._thread
        if track is not None:
            self._discard_track(track)
        if thread is not None and timeout > 0 and thread is not threading.current_thread():
            thread.join(timeout)

    def join(self, timeout: float | None = None) -> bool:
        """Wait for a stopped worker and report whether it exited."""
        with self._cond:
            thread = self._thread
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(None if timeout is None else max(0.0, timeout))
        return not thread.is_alive()

    def ready(self) -> T | None:
        """Return the ready handoff as an identity snapshot without consuming it."""
        with self._cond:
            return self._ready

    def pop(self) -> T | None:
        """Take the prepared handoff without waiting."""
        with self._cond:
            track, self._ready = self._ready, None
            if track is not None:
                self._need_prepare = True
                self._cond.notify_all()
            return track

    def replace_ready(self, expected: T, replacement: T) -> bool:
        """Atomically replace the exact ready handoff if it is still waiting."""
        with self._cond:
            if self._ready is not expected:
                return False
            self._ready = replacement
            self._cond.notify_all()
        return True

    def reset(self, prepare: Move) -> None:
        """Move the selection cursor and prepare again, deferring while busy."""
        with self._cond:
            self._generation += 1
            self._refresh_inflight = False
            track, self._ready = self._ready, None
            self._need_prepare = True
            immediate = not self._busy and not self._deferred
            if immediate:
                self._busy = True
            else:
                self._deferred.append(prepare)
            self._cond.notify_all()
        if track is not None:
            self._discard_track(track)
        if immediate:
            self._run_move(prepare)

    def request_plan(self) -> None:
        """Request worker-side maintenance, even when there is no ready handoff."""
        if self._maintain is None:
            return
        with self._cond:
            self._maintenance_due = True
            self._cond.notify_all()

    def wait_ready(self, timeout: float) -> bool:
        """Wait until a handoff is ready; there is no fallback result."""
        with self._cond:
            return self._cond.wait_for(lambda: self._ready is not None, max(0.0, timeout))

    def wait_rendered(self, timeout: float) -> bool:
        """Compatibility with playout callers: wait for a completed preparation."""
        return self.wait_ready(timeout)

    def invalidate_prepared(self) -> None:
        """Discard and rebuild the ready or currently preparing handoff."""
        self.refresh()

    def rerender(self) -> None:
        """Rewind the waiting handoff and prepare a fresh one, without fallback."""
        self.refresh()

    def refresh(self) -> None:
        """Withdraw stale ready work and rebuild; never expose it as a fallback."""
        with self._cond:
            track, self._ready = self._ready, None
            self._need_prepare = True
            self._generation += 1
            if track is None:
                if self._busy:
                    self._refresh_inflight = True
                self._cond.notify_all()
                return
            immediate = not self._busy
            if immediate:
                self._busy = True
            else:
                self._deferred.append(lambda: self._rewind(track))
            self._cond.notify_all()
        if immediate:
            try:
                self._rewind_track(track)
            finally:
                self._discard_track(track)
                self._finish_busy()
        else:
            self._discard_track(track)

    def _loop(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(
                    lambda: (
                        self._stopped
                        or (
                            not self._busy
                            and (
                                bool(self._deferred)
                                or self._maintenance_due
                                or (self._need_prepare and self._ready is None)
                            )
                        )
                    )
                )
                if self._stopped:
                    self._thread = None
                    self._cond.notify_all()
                    return
                if self._deferred:
                    moves, self._deferred = self._deferred, []
                    self._busy = True
                    action = "moves"
                elif self._maintenance_due:
                    self._maintenance_due = False
                    ready = self._ready
                    self._busy = True
                    action = "maintain"
                else:
                    self._busy = True
                    self._need_prepare = False
                    self._refresh_inflight = False
                    generation = self._generation
                    action = "prepare"
            if action == "moves":
                for move in moves:
                    self._run_move(move, keep_busy=True)
                self._finish_busy()
                continue
            if action == "maintain":
                self._run_maintenance(ready)
                self._finish_busy()
                continue
            self._run_prepare(generation)

    def _run_prepare(self, generation: int) -> None:
        track: T | None = None
        try:
            track = self._prepare()
        except Exception:
            logger.exception("Preparing the next live handoff failed")
        stale = False
        rewind = False
        with self._cond:
            if generation != self._generation or self._stopped:
                stale = track is not None
                rewind = self._refresh_inflight and not self._deferred and track is not None
                self._refresh_inflight = False
                self._need_prepare = True
            elif track is not None:
                self._ready = track
                self._need_prepare = False
                self._maintenance_due = self._maintain is not None
            else:
                self._need_prepare = True
            self._cond.notify_all()
        if stale and track is not None:
            if rewind:
                self._rewind_track(track)
            self._discard_track(track)
        self._finish_busy()
        if track is None and not stale:
            with self._cond:
                if not self._stopped and self._ready is None and not self._deferred:
                    self._cond.wait(_RETRY_SECONDS)

    def _run_maintenance(self, track: T | None) -> None:
        try:
            if self._maintain is not None:
                self._maintain(track)
        except Exception:
            logger.exception("Maintaining the live handoff failed")

    def _run_move(self, move: Move, *, keep_busy: bool = False) -> None:
        try:
            move()
        except Exception:
            logger.exception("Moving the live selection cursor failed")
        finally:
            if not keep_busy:
                self._finish_busy()

    def _rewind_track(self, track: T) -> None:
        try:
            self._rewind(track)
        except Exception:
            logger.exception("Rewinding a discarded live handoff failed")

    def _discard_track(self, track: T) -> None:
        if self._discard is None:
            return
        try:
            self._discard(track)
        except Exception:
            logger.exception("Cleaning up a discarded live handoff failed")

    def _finish_busy(self) -> None:
        with self._cond:
            self._busy = False
            self._cond.notify_all()
