"""Which station a thread, task or log line belongs to (#70).

Every station runs its own player, mix bus, encoder and helpers, so a log
line such as "Stream station tick failed" must say which station it came
from.  The station's slug lives in :data:`CURRENT_STATION`, a context
variable: asyncio tasks and :func:`asyncio.to_thread` carry it, and
:func:`station_thread` hands it to a new thread, so the slug follows the
station's work wherever it runs.  :func:`install_log_prefix` then starts
each log line of a station other than the default with ``[slug]``, and
:func:`thread_name` adds the slug to its threads' names
(``autodj-player-nightowl``).  The default station's lines and thread
names are unchanged.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
import weakref
from collections.abc import Callable, Mapping
from typing import Any

DEFAULT_SLUG = "default"

#: The station the running code works for; ``None`` outside any station.
CURRENT_STATION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "autodj_station", default=None
)

# Loggers whose lines are machine-read and already name what they are
# about: the audit log's JSON records name the route, station path included.
_UNPREFIXED = frozenset({"autodj.audit"})

_install_lock = threading.Lock()
_installed = False
_threads_lock = threading.Lock()
_station_threads: dict[str, weakref.WeakSet[threading.Thread]] = {}


def _labelled(slug: str | None) -> bool:
    return slug is not None and slug != DEFAULT_SLUG


def thread_name(base: str) -> str:
    """*base* with the current station's slug appended, unless it is the default."""
    slug = CURRENT_STATION.get()
    return f"{base}-{slug}" if _labelled(slug) else base


def station_thread(
    target: Callable[..., object],
    *,
    name: str,
    args: tuple[Any, ...] = (),
    kwargs: Mapping[str, Any] | None = None,
    daemon: bool = True,
) -> threading.Thread:
    """A thread named for, and running as, the current station (not started)."""
    thread = threading.Thread(
        target=target,
        name=thread_name(name),
        args=args,
        kwargs=dict(kwargs or {}),
        daemon=daemon,
        context=contextvars.copy_context(),
    )
    track_station_thread(thread, CURRENT_STATION.get())
    return thread


def track_station_thread(thread: threading.Thread, slug: str | None) -> None:
    """Track a station-owned thread without changing its existing name."""
    if slug is not None:
        with _threads_lock:
            _station_threads.setdefault(slug, weakref.WeakSet()).add(thread)


def wait_for_station_threads(slug: str, timeout: float) -> list[str]:
    """Join station threads against one deadline; return names still running."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        with _threads_lock:
            threads = tuple(_station_threads.get(slug, ()))
        running = [
            thread
            for thread in threads
            if thread is not threading.current_thread() and thread.is_alive()
        ]
        if not running:
            return []
        for thread in running:
            thread.join(max(0.0, deadline - time.monotonic()))
        if time.monotonic() >= deadline:
            with _threads_lock:
                threads = tuple(_station_threads.get(slug, ()))
            return sorted(thread.name for thread in threads if thread.is_alive())


def enter_station(slug: str | None) -> None:
    """Make the calling thread work for *slug*; a thread pool's ``initializer``."""
    CURRENT_STATION.set(slug)
    track_station_thread(threading.current_thread(), slug)


def install_log_prefix() -> None:
    """Start every log line of a station other than the default with ``[slug]``.

    Installed once per process, as a log-record factory, so the prefix is
    added where the line is logged, in the station's own context, and
    every handler and test capture sees the same text.
    """
    global _installed
    with _install_lock:
        if _installed:
            return
        previous = logging.getLogRecordFactory()

        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = previous(*args, **kwargs)
            slug = CURRENT_STATION.get()
            if _labelled(slug) and isinstance(record.msg, str) and record.name not in _UNPREFIXED:
                record.msg = f"[{slug}] {record.msg}"
            record.station = slug or DEFAULT_SLUG
            return record

        logging.setLogRecordFactory(factory)
        _installed = True
