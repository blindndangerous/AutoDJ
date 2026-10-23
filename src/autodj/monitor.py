"""Bounded fan-out for read-only browser monitoring of the server mix."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from contextlib import suppress

import numpy as np

MAX_MONITORS = 8
_QUEUED_BLOCKS = 4


class MonitorClosed(Exception):
    """The station or monitor connection has closed."""


class MonitorClient:
    """One event-loop consumer backed by a bounded thread-safe audio queue."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._lock = threading.Lock()
        self._frames: deque[bytes] = deque(maxlen=_QUEUED_BLOCKS)
        self._ready = asyncio.Event()
        self._closed = False

    def offer(self, payload: bytes) -> None:
        wake = False
        with self._lock:
            if self._closed:
                return
            was_empty = not self._frames
            self._frames.append(payload)
            wake = was_empty
        if wake:
            try:
                self._loop.call_soon_threadsafe(self._ready.set)
            except RuntimeError:  # the server loop is already closing
                self.close()

    async def receive(self) -> bytes:
        while True:
            await self._ready.wait()
            with self._lock:
                if self._frames:
                    payload = self._frames.popleft()
                    if not self._frames:
                        self._ready.clear()
                    return payload
                if self._closed:
                    raise MonitorClosed
                self._ready.clear()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._frames.clear()
        with suppress(RuntimeError):
            self._loop.call_soon_threadsafe(self._ready.set)


class MonitorOutput:
    """Nonblocking post-master fan-out with bounded, isolated clients."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: set[MonitorClient] = set()
        self._closed = False

    @property
    def listener_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> MonitorClient | None:
        with self._lock:
            if self._closed or len(self._clients) >= MAX_MONITORS:
                return None
            client = MonitorClient(loop)
            self._clients.add(client)
            return client

    def unsubscribe(self, client: MonitorClient) -> None:
        with self._lock:
            self._clients.discard(client)
        client.close()

    def write(self, block: np.ndarray) -> None:
        """Copy one PCM block once, then offer it without waiting on clients."""
        payload = np.asarray(block, dtype="<f4").tobytes()
        with self._lock:
            clients = tuple(self._clients)
        for client in clients:
            client.offer(payload)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            clients, self._clients = tuple(self._clients), set()
        for client in clients:
            client.close()
