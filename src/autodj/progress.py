"""Terminal and log-based progress for long-running track phases."""

from __future__ import annotations

import logging
import sys
from typing import TextIO

from rich.console import Console
from rich.progress import Progress, TaskID, TextColumn

_PROGRESS_EVERY = 25


_progress_console: Console | None = None
_progress_stream: TextIO | None = None


def get_progress_console() -> Console:
    """Return the shared Rich console writing to the current stderr stream."""
    global _progress_console, _progress_stream
    if _progress_console is None or _progress_stream is not sys.stderr:
        _progress_console = Console(stderr=True)
        _progress_stream = sys.stderr
    return _progress_console


class TrackProgress:
    """Show one in-place terminal status or periodic plain log updates."""

    def __init__(self, phase: str, total: int, logger: logging.Logger) -> None:
        self.phase = phase
        self.total = total
        self.logger = logger
        self._progress: Progress | None = None
        self._task_id: TaskID | None = None

    def __enter__(self) -> TrackProgress:
        console = get_progress_console()
        if sys.stderr.isatty() and console.is_interactive:
            self._progress = Progress(
                TextColumn("{task.description}"),
                TextColumn("{task.completed}/{task.total}"),
                TextColumn("{task.percentage:>3.0f}%"),
                console=console,
                auto_refresh=False,
                transient=False,
            )
            self._progress.start()
            self._task_id = self._progress.add_task(self.phase, total=self.total)
        return self

    def update(self, done: int) -> None:
        if self._progress is not None and self._task_id is not None:
            self._progress.update(self._task_id, completed=done, refresh=True)
        elif done == self.total or done % _PROGRESS_EVERY == 0:
            self.logger.info("%s: %d of %d", self.phase, done, self.total)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._progress is not None:
            self._progress.stop()
