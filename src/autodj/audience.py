"""Count the stream listeners who hear each play, for royalty reports.

A performance is one transmission of a recording to one listener.  A play's
count is the listeners connected when it starts, plus each listener who
connects while it plays.

Players drop and reopen the stream (a Wi-Fi blip, a speaker regrouping), so
a listener is a client address and User-Agent pair.  A new connection from a
pair already counted for the play is the same listener when that pair is
still connected, or when its last connection ended at most
:data:`REJOIN_SECONDS` ago.  Two players behind one public address that
send the same User-Agent therefore count once while both are connected.

:class:`StreamAudience` is not thread-safe; the bridge calls it under its
history lock, so a count can never land on the wrong play.
"""

from __future__ import annotations

import time
from collections.abc import Callable

REJOIN_SECONDS = 10.0

Client = tuple[str, str]  # (address, User-Agent)


class StreamAudience:
    """Track connected stream clients and who has heard the current play."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._open: dict[Client, int] = {}
        self._left_at: dict[Client, float] = {}
        self._heard: set[Client] = set()

    def start_play(self) -> int:
        """Begin counting a new play; return the listeners connected now."""
        self._heard = set(self._open)
        return len(self._heard)

    def connected(self, client: Client) -> bool:
        """Note a new connection; return whether it adds a listener to the play."""
        now = self._clock()
        self._forget_old(now)
        same = client in self._heard and (client in self._open or client in self._left_at)
        self._open[client] = self._open.get(client, 0) + 1
        self._left_at.pop(client, None)
        self._heard.add(client)
        return not same

    def disconnected(self, client: Client) -> None:
        """Note that one of *client*'s connections ended."""
        count = self._open.get(client, 0)
        if count > 1:
            self._open[client] = count - 1
        elif count == 1:
            del self._open[client]
            self._left_at[client] = self._clock()

    def _forget_old(self, now: float) -> None:
        for client, left in list(self._left_at.items()):
            if now - left > REJOIN_SECONDS:
                del self._left_at[client]
