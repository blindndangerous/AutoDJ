"""Which station a thread, task or log line belongs to (#70).

Every station runs its own player, mix bus, encoder and helpers, so a log
line such as "Stream station tick failed" must say which station it came
from.  The station's slug lives in :data:`CURRENT_STATION`, a context
variable: asyncio tasks and :func:`asyncio.to_thread` carry it, and
:func:`station_thread` hands it to a new thread, so the slug follows the
station's work wherever it runs.  :func:`install_log_prefix` then starts
each log line in a station context with ``[slug]``, and
:func:`thread_name` adds the slug to its threads' names
(``autodj-player-nightowl``).  Default station thread names are unchanged;
its log lines include ``[default]``.

A station may have a log label (``station.log_label``), such as ``nor``
for Night Owl Radio.  Only the visible prefix uses it (``[nor]``): the
context variable, ``record.station``, thread names and scopes keep the
slug.  :func:`set_log_label` changes a label at once, without a restart.
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

# Slug -> log label, for stations that have one.  Replaced whole under the
# lock and read without it: the log-record factory runs on every thread.
_labels_lock = threading.Lock()
_log_labels: dict[str, str] = {}


class StationThreadScope:
    """Threads belonging to one run of a station."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.threads: weakref.WeakSet[threading.Thread] = weakref.WeakSet()


CURRENT_STATION_SCOPE: contextvars.ContextVar[StationThreadScope | None] = contextvars.ContextVar(
    "autodj_station_scope", default=None
)
_station_scopes: dict[str, StationThreadScope] = {}
_thread_scopes: weakref.WeakKeyDictionary[threading.Thread, StationThreadScope] = (
    weakref.WeakKeyDictionary()
)


def new_station_scope(slug: str) -> StationThreadScope:
    """Start tracking a new run of *slug*."""
    scope = StationThreadScope(slug)
    with _threads_lock:
        for previous in set(_thread_scopes.values()):
            _prune_threads(previous)
        _station_scopes[slug] = scope
    return scope


def station_scope(slug: str | None) -> StationThreadScope | None:
    """Find the run owning work in this context or thread."""
    if slug is None:
        return None
    scope = CURRENT_STATION_SCOPE.get()
    if scope is not None and scope.slug == slug:
        return scope
    with _threads_lock:
        scope = _thread_scopes.get(threading.current_thread())
        if scope is not None and scope.slug == slug:
            return scope
        scope = _station_scopes.get(slug)
        if scope is None:
            scope = _station_scopes[slug] = StationThreadScope(slug)
        return scope


def _prune_threads(scope: StationThreadScope) -> None:
    for thread in tuple(scope.threads):
        if thread.ident is not None and not thread.is_alive():
            scope.threads.discard(thread)
            _thread_scopes.pop(thread, None)


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
    slug = CURRENT_STATION.get()
    scope = station_scope(slug)
    context = contextvars.copy_context()
    context.run(CURRENT_STATION_SCOPE.set, scope)
    thread = threading.Thread(
        target=target,
        name=thread_name(name),
        args=args,
        kwargs=dict(kwargs or {}),
        daemon=daemon,
        context=context,
    )
    track_station_thread(thread, slug)
    return thread


def track_station_thread(thread: threading.Thread, slug: str | None) -> None:
    """Track a station-owned thread without changing its existing name."""
    if (scope := station_scope(slug)) is not None:
        with _threads_lock:
            _prune_threads(scope)
            previous = _thread_scopes.get(thread)
            if previous is not None and previous is not scope:
                previous.threads.discard(thread)
            _thread_scopes[thread] = scope
            scope.threads.add(thread)


def wait_for_station_threads(scope: StationThreadScope, timeout: float) -> list[str]:
    """Join one run's threads against one deadline; return names still running."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        with _threads_lock:
            _prune_threads(scope)
            threads = tuple(scope.threads)
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
                _prune_threads(scope)
                threads = tuple(scope.threads)
            return sorted(thread.name for thread in threads if thread.is_alive())


def enter_station(slug: str | None, scope: StationThreadScope | None = None) -> None:
    """Make the calling thread work for *slug*; a thread pool's ``initializer``."""
    CURRENT_STATION.set(slug)
    CURRENT_STATION_SCOPE.set(scope or station_scope(slug))
    track_station_thread(threading.current_thread(), slug)


def log_label(slug: str) -> str:
    """What *slug*'s log lines show in brackets: its log label, else the slug."""
    return _log_labels.get(slug) or slug


def set_log_label(slug: str, label: str | None) -> None:
    """Show *label* in *slug*'s log lines from now on; empty or ``None`` shows the slug."""
    global _log_labels
    with _labels_lock:
        labels = dict(_log_labels)
        if label:
            labels[slug] = label
        else:
            labels.pop(slug, None)
        _log_labels = labels


def set_log_labels(labels: Mapping[str, str | None]) -> None:
    """Replace every station's log label: the server's stations as configured."""
    global _log_labels
    with _labels_lock:
        _log_labels = {slug: label for slug, label in labels.items() if label}


def install_log_prefix() -> None:
    """Start every station log line with ``[slug]``, or ``[label]`` when it has one.

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
            if slug is not None and isinstance(record.msg, str) and record.name not in _UNPREFIXED:
                record.msg = f"[{log_label(slug)}] {record.msg}"
            record.station = slug or DEFAULT_SLUG
            return record

        logging.setLogRecordFactory(factory)
        _installed = True
