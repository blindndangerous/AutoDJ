"""Let Icecast source clients stream to the web server's own port (#48).

BUTT, Mixxx (through libshout) and other Icecast source clients open a
connection, send ``PUT /mount HTTP/1.1`` (or ``SOURCE /mount HTTP/1.0``,
or ``ICE/1.0`` from old ones) with neither ``Content-Length`` nor
``Transfer-Encoding``, and then send audio until they hang up.  To HTTP
that is a request with an empty body followed by garbage: h11 would answer
the request and then fail on the first bytes of audio.

:class:`IcecastSourceH11Protocol` is uvicorn's h11 protocol with one
change.  When the *first* request on a connection is such a source
request to a live mount (``/live`` or ``/stations/<slug>/live``), it adds
``Transfer-Encoding: chunked`` to the request's head and then wraps every
byte the client sends after the head as one chunk.  The ASGI app then
sees an ordinary streaming request body, and nothing the client sends can
be read as another request: the connection carries this one request until
it closes (the live route answers with ``Connection: close``).  Every
other request, and any request after the first, is left exactly as h11
parses it, so a request that names its own length is framed by h11 as
usual and a malformed one is refused by h11 as usual.

It runs on TLS connections too: uvicorn hands it the decrypted bytes.
"""

from __future__ import annotations

import asyncio
import re

from uvicorn.protocols.http.h11_impl import H11Protocol

#: Longest request head held while deciding; past it h11 decides (and refuses).
_MAX_HEAD_BYTES = 16 * 1024
#: A connection whose first request head is not complete by then is closed,
#: so a client trickling its first head (slow-loris) cannot hold it open.
HEAD_DEADLINE_SECONDS = 10.0
_METHODS = (b"PUT ", b"SOURCE ")
_LIVE_MOUNT = re.compile(rb"/live|/stations/[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?/live")
_VERSIONS = {b"HTTP/1.0": b"HTTP/1.0", b"HTTP/1.1": b"HTTP/1.1", b"ICE/1.0": b"HTTP/1.0"}


def close_delimited_source_head(head: bytes) -> bytes | None:
    """The request head to give h11 for a close-delimited source request, else ``None``.

    *head* is the request line and header lines, without the blank line
    that ends them.  ``None`` means "leave the request to h11": not a
    source request to a live mount, or one that frames its own body.
    """
    lines = head.split(b"\r\n")
    parts = lines[0].split(b" ")
    if len(parts) != 3:
        return None
    method, target, version = parts
    if method not in (b"PUT", b"SOURCE") or version not in _VERSIONS:
        return None
    if not _LIVE_MOUNT.fullmatch(target.split(b"?", 1)[0]):
        return None
    for line in lines[1:]:
        if not line or line[:1] in (b" ", b"\t"):
            return None  # an empty line or a folded header: h11 decides
        name = line.split(b":", 1)[0].strip().lower()
        if name in (b"content-length", b"transfer-encoding"):
            return None
    request_line = b" ".join((method, target, _VERSIONS[version]))
    return b"\r\n".join((request_line, *lines[1:], b"Transfer-Encoding: chunked"))


class IcecastSourceH11Protocol(H11Protocol):
    """uvicorn's h11 protocol, framing a close-delimited live source body as chunks."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        # "head": deciding on the first request; "pass": plain HTTP from
        # now on; "stream": every byte is source audio until the end.
        self._source_mode = "head"
        self._source_head = b""
        self._head_deadline: asyncio.TimerHandle | None = None

    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
        """Start the clock on the connection's first request head."""
        super().connection_made(transport)
        self._head_deadline = self.loop.call_later(HEAD_DEADLINE_SECONDS, self._head_too_slow)

    def connection_lost(self, exc: Exception | None) -> None:
        """Stop the clock with the connection."""
        if self._head_deadline is not None:
            self._head_deadline.cancel()
            self._head_deadline = None
        super().connection_lost(exc)

    def _head_too_slow(self) -> None:
        """Close a connection that has not sent one complete request head."""
        self._head_deadline = None
        transport = self.transport
        if self.cycle is not None or transport.is_closing():
            return
        try:
            protocol = transport.get_protocol()
        except AttributeError:  # TLS transport has detached its SSL protocol
            return
        if protocol is self:  # not handed to a WebSocket
            transport.close()

    def data_received(self, data: bytes) -> None:
        """Feed h11, wrapping a live source's audio as chunks."""
        if self._source_mode == "pass":
            super().data_received(data)
            return
        if self._source_mode == "stream":
            if data:
                super().data_received(b"%x\r\n%b\r\n" % (len(data), data))
            return
        head = self._source_head + data
        if not any(
            head.startswith(method) or method.startswith(head[: len(method)]) for method in _METHODS
        ):
            self._pass(head)
            return
        end = head.find(b"\r\n\r\n")
        if end < 0:
            if len(head) > _MAX_HEAD_BYTES:
                self._pass(head)
            else:
                self._source_head = head
            return
        framed = close_delimited_source_head(head[:end])
        if framed is None:
            self._pass(head)
            return
        self._source_mode = "stream"
        self._source_head = b""
        super().data_received(framed + b"\r\n\r\n")
        rest = head[end + 4 :]
        if rest and self._source_mode == "stream":
            super().data_received(b"%x\r\n%b\r\n" % (len(rest), rest))

    def _pass(self, data: bytes) -> None:
        self._source_mode = "pass"
        self._source_head = b""
        super().data_received(data)
