"""Push the station to external Icecast and Shoutcast servers.

Each :class:`Relay` is a sink of a :class:`~autodj.stream.StreamOutput`:
the output's reader thread hands it every chunk of encoded MP3, with the
stream title that chunk belongs to, through :meth:`Relay.feed`, which only
queues and never blocks.  A slow or unreachable server therefore never
holds up the encoder or the mix bus's pacing thread.  A sender thread per
relay logs in to the server, sends the queued audio and, when anything
fails, reconnects with exponential backoff.  A metadata thread per relay
sends the now-playing title to the server's admin interface whenever the
title in the audio changes.

The password is written only to the server's own socket or request.
Every error text here comes from fixed wording, HTTP status numbers and
operating-system error descriptions, never from a URL or request, so
status, logs and the web page cannot leak it.
"""

from __future__ import annotations

import base64
import collections
import contextlib
import http.client
import json
import logging
import socket
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlencode

from autodj.fsutil import atomic_write
from autodj.icy import mp3_frame_offset
from autodj.station_log import station_thread

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig, RelayConfig
    from autodj.stations import StationPaths

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10.0
SEND_TIMEOUT = 10.0
#: The longest a server may take to answer a login, in total.
LOGIN_TIMEOUT = 10.0
#: How long stopping waits, in total, for every relay's threads to end.
CLOSE_TIMEOUT = 3.0
METADATA_TIMEOUT = 10.0
BACKOFF_START = 1.0
BACKOFF_MAX = 60.0
# A connection that stayed up this long resets the backoff.
STABLE_SECONDS = 30.0
# Servers ignore a title sent the instant a source logs in.
FIRST_TITLE_DELAY = 2.0
_QUEUE_SECONDS = 5.0
# autodj.stereo.SAMPLE_RATE, not imported: that module loads numpy and
# soundfile, and ``autodj doctor`` imports this one.
_SAMPLE_RATE = 44100
_REPLY_LIMIT = 8192
_USER_AGENT = "AutoDJ"
# Shoutcast 1 answers admin.cgi only for browser-like user agents.
_SHOUTCAST_USER_AGENT = "Mozilla/5.0 (compatible; AutoDJ)"

TYPE_LABELS = {"icecast": "Icecast", "shoutcast1": "Shoutcast 1", "shoutcast2": "Shoutcast 2"}


class RelayError(Exception):
    """A relay failure whose message is safe to show and log."""


@dataclass(frozen=True)
class StationDetails:
    """The station details a relay announces besides its name (``[stream]``).

    Each is empty when not set, and an empty one is not sent (Shoutcast
    always gets its genre and website lines, empty or not, as before).
    Shoutcast's source login has no place for the description.

    Attributes:
        genre: ``stream.station_genre``.
        url: ``stream.station_url``, the station's website.
        description: ``stream.station_description``.
    """

    genre: str = ""
    url: str = ""
    description: str = ""


NO_DETAILS = StationDetails()


def _header_value(text: str) -> str:
    """*text* with any control character dropped, so it cannot end a header line.

    The configuration and the web page already refuse control characters;
    this keeps a header intact whatever reaches it.
    """
    return "".join(character for character in text if character.isprintable())


class Connection(Protocol):
    """A logged-in source connection."""

    def send(self, data: bytes) -> None:
        """Send *data*, blocking at most the send timeout."""

    def close(self) -> None:
        """Close the connection; safe to call from another thread."""


def relay_status_path(cfg: AutoDJConfig, paths: StationPaths | None = None) -> Path:
    """Return the station's relay status file; the default station's is in the index folder.

    The running server rewrites it whenever a relay changes state, and
    ``autodj doctor`` reads it.
    """
    from autodj.stations import StationPaths

    return (paths or StationPaths.default(cfg)).relay_status


def describe_error(exc: BaseException) -> str:
    """Return a short, secret-free description of *exc*.

    Args:
        exc: A failure from connecting, logging in, sending or a title update.

    Returns:
        Plain wording for the web page, logs and ``doctor``.
    """
    if isinstance(exc, RelayError):
        return str(exc)
    if isinstance(exc, socket.gaierror):
        return "the server name could not be found"
    if isinstance(exc, TimeoutError):
        return "the server did not answer in time"
    if isinstance(exc, ConnectionRefusedError):
        return "the server refused the connection"
    if isinstance(exc, ConnectionError):
        return "the server closed the connection"
    if isinstance(exc, OSError) and exc.strerror:
        return f"network error: {exc.strerror}"
    if isinstance(exc, http.client.HTTPException):
        return "the server sent a reply AutoDJ could not read"
    return f"unexpected error ({type(exc).__name__})"


def describe_target(cfg: RelayConfig) -> str:
    """Return where *cfg* points, such as ``radio.example.com:8000/live``."""
    host = f"[{cfg.host}]" if ":" in cfg.host else cfg.host
    if cfg.type == "icecast":
        return f"{host}:{cfg.port}{cfg.mount}"
    if cfg.type == "shoutcast2":
        return f"{host}:{cfg.port} stream {cfg.stream_id}"
    return f"{host}:{cfg.port}"


def source_port(cfg: RelayConfig) -> int:
    """Return the port a source logs in on: Shoutcast uses the one above its own."""
    return cfg.port if cfg.type == "icecast" else cfg.port + 1


def _shut_down(sock: socket.socket) -> None:
    """Shut *sock* down, which ends a send or receive blocked in another thread."""
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


class SocketConnection:
    """A logged-in source socket."""

    def __init__(self, sock: socket.socket) -> None:
        """Wrap *sock*, which has already logged in."""
        self._sock = sock

    def send(self, data: bytes) -> None:
        """Send all of *data*; raises ``OSError`` on failure or timeout."""
        self._sock.sendall(data)

    def close(self) -> None:
        """Shut the socket down, which also ends a send blocked in another thread."""
        _shut_down(self._sock)
        with contextlib.suppress(OSError):
            self._sock.close()


def _read_until(sock: socket.socket, marker: bytes) -> bytes:
    """Read the server's reply until *marker* or the connection ends.

    The whole reply must arrive within ``LOGIN_TIMEOUT``, so a server
    sending a byte at a time cannot hold the relay for long.

    Raises:
        RelayError: The reply took too long or was too long.
    """
    deadline = time.monotonic() + LOGIN_TIMEOUT
    data = b""
    while marker not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RelayError("the server took too long to answer the login")
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(1024)
        except TimeoutError:
            raise RelayError("the server took too long to answer the login") from None
        if not chunk:
            break
        data += chunk
        if len(data) > _REPLY_LIMIT:
            raise RelayError("the server sent an overlong reply")
    return data


def _basic_auth(user: str, password: str) -> str:
    """Return an HTTP Basic ``Authorization`` value."""
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode("ascii")


def _host_header(host: str, port: int) -> str:
    """Return ``host:port`` for a Host header, bracketing an IPv6 address."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _icecast_login(
    sock: socket.socket,
    cfg: RelayConfig,
    bitrate: int,
    station_name: str,
    details: StationDetails = NO_DETAILS,
) -> None:
    """Start an Icecast 2.4 source with HTTP PUT.

    The station's genre, website and description go in Icecast's
    ``Ice-Genre``, ``Ice-Url`` and ``Ice-Description`` headers when set.

    Raises:
        RelayError: The server refused the source.
    """
    assert cfg.user is not None and cfg.mount is not None
    lines = [
        f"PUT {cfg.mount} HTTP/1.1",
        f"Host: {_host_header(cfg.host, cfg.port)}",
        f"Authorization: {_basic_auth(cfg.user, cfg.password)}",
        f"User-Agent: {_USER_AGENT}",
        "Content-Type: audio/mpeg",
        f"Ice-Name: {_header_value(station_name)}",
        f"Ice-Public: {int(cfg.public)}",
        f"Ice-Bitrate: {bitrate}",
        f"Ice-Audio-Info: bitrate={bitrate};samplerate={_SAMPLE_RATE};channels=2",
    ]
    for header, value in (
        ("Ice-Genre", details.genre),
        ("Ice-Url", details.url),
        ("Ice-Description", details.description),
    ):
        if value:
            lines.append(f"{header}: {_header_value(value)}")
    lines.append("Expect: 100-continue")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
    reply = _read_until(sock, b"\r\n\r\n")
    parts = reply.split(b"\r\n", 1)[0].split()
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/") or not parts[1].isdigit():
        raise RelayError(
            "the server closed the connection during login"
            if not reply
            else "the server did not answer like an Icecast server"
        )
    code = int(parts[1])
    if code in (100, 200):
        return
    if code == 401:
        raise RelayError("the server rejected the user name or password")
    if code == 403:
        raise RelayError(
            "the server refused the source (HTTP status 403); the mount point may already be in use"
        )
    raise RelayError(f"the server refused the source (HTTP status {code})")


def _shoutcast_login(
    sock: socket.socket,
    cfg: RelayConfig,
    bitrate: int,
    station_name: str,
    details: StationDetails = NO_DETAILS,
) -> None:
    """Log in with the Shoutcast 1 source protocol, which Shoutcast 2 also accepts.

    Shoutcast 2 picks the stream from a ``:#<id>`` suffix on the password.
    The station's genre and website go in ``icy-genre`` and ``icy-url``;
    the protocol has no description line.

    Raises:
        RelayError: The server refused the password or the connection.
    """
    password = cfg.password
    if cfg.type == "shoutcast2":
        password = f"{password}:#{cfg.stream_id}"
    sock.sendall(password.encode("utf-8") + b"\r\n")
    first = _read_until(sock, b"\n").split(b"\n", 1)[0].strip()
    if not first.upper().startswith(b"OK"):
        if b"password" in first.lower():
            raise RelayError("the server rejected the password")
        if not first:
            raise RelayError("the server closed the connection during login")
        raise RelayError("the server did not accept the source connection")
    headers = (
        f"icy-name:{_header_value(station_name)}\r\n"
        f"icy-genre:{_header_value(details.genre)}\r\n"
        f"icy-url:{_header_value(details.url)}\r\n"
        f"icy-pub:{int(cfg.public)}\r\n"
        f"icy-br:{bitrate}\r\n"
        "content-type:audio/mpeg\r\n"
        "\r\n"
    )
    sock.sendall(headers.encode("utf-8"))


def open_source(
    cfg: RelayConfig,
    *,
    bitrate: int,
    station_name: str,
    details: StationDetails = NO_DETAILS,
    connect: Callable[..., socket.socket] = socket.create_connection,
    on_socket: Callable[[socket.socket], None] | None = None,
) -> Connection:
    """Connect to *cfg*'s server and log in as its source.

    Args:
        cfg: The relay.
        bitrate: The MP3 bitrate that will be sent, in kbps.
        station_name: The station name to announce.
        details: The station's genre, website and description to announce.
        connect: ``socket.create_connection`` replacement for tests.
        on_socket: Called with the socket as soon as it is connected, so
            another thread can shut it down to end a login stuck waiting.

    Returns:
        The connection, ready for audio.

    Raises:
        RelayError: The server refused the login.
        OSError: The connection failed.
    """
    sock = connect((cfg.host, source_port(cfg)), timeout=CONNECT_TIMEOUT)
    try:
        if on_socket is not None:
            on_socket(sock)
        sock.settimeout(SEND_TIMEOUT)
        if cfg.type == "icecast":
            _icecast_login(sock, cfg, bitrate, station_name, details)
        else:
            _shoutcast_login(sock, cfg, bitrate, station_name, details)
        sock.settimeout(SEND_TIMEOUT)
    except BaseException:
        with contextlib.suppress(OSError):
            sock.close()
        raise
    return SocketConnection(sock)


def send_metadata(
    cfg: RelayConfig,
    title: str,
    *,
    connection_factory: Callable[..., http.client.HTTPConnection] = http.client.HTTPConnection,
) -> None:
    """Send the now-playing *title* to *cfg*'s server.

    Icecast takes it at ``/admin/metadata`` with the source login;
    Shoutcast at ``/admin.cgi`` with the password in the query, the only
    way Shoutcast accepts it.

    Args:
        cfg: The relay.
        title: The ``Artist - Title`` text.
        connection_factory: ``http.client.HTTPConnection`` replacement for tests.

    Raises:
        RelayError: The server refused the update.
        OSError: The request failed.
    """
    if cfg.type == "icecast":
        assert cfg.user is not None and cfg.mount is not None
        query = {"mode": "updinfo", "mount": cfg.mount, "song": title, "charset": "UTF-8"}
        path = "/admin/metadata?" + urlencode(query)
        headers = {
            "Authorization": _basic_auth(cfg.user, cfg.password),
            "User-Agent": _USER_AGENT,
        }
    else:
        query = {"mode": "updinfo", "song": title, "pass": cfg.password}
        if cfg.type == "shoutcast2":
            query["sid"] = str(cfg.stream_id)
        path = "/admin.cgi?" + urlencode(query)
        headers = {"User-Agent": _SHOUTCAST_USER_AGENT}
    conn = connection_factory(cfg.host, cfg.port, timeout=METADATA_TIMEOUT)
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        response.read(_REPLY_LIMIT)
        status = response.status
    finally:
        conn.close()
    if status in (401, 403):
        raise RelayError("the server rejected the password for title updates")
    if status != 200:
        raise RelayError(f"the title update got HTTP status {status}")


class Relay:
    """Push one output's MP3 to one server; reconnect whenever it fails.

    :meth:`feed` and :meth:`encoder_changed` run on the stream's reader
    thread and only touch the queue under a short lock.  The sender thread
    owns the connection; :meth:`close` shuts the connection's socket down
    from outside to end a blocked send at once.
    """

    def __init__(
        self,
        cfg: RelayConfig,
        *,
        bitrate: Callable[[], int],
        station_name: Callable[[], str],
        station_details: Callable[[], StationDetails] = lambda: NO_DETAILS,
        connect: Callable[..., Connection] = open_source,
        update_metadata: Callable[[RelayConfig, str], None] = send_metadata,
        on_change: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a stopped relay; :meth:`start` starts it.

        Args:
            cfg: The relay's configuration.
            bitrate: The current bitrate of the output it is a sink of.
            station_name: Returns the station name to announce; it can
                change while AutoDJ runs and is read at each login.
            station_details: Returns the station's genre, website and
                description, read at each login like the name.
            connect: Logs in and returns a connection (:func:`open_source`).
            update_metadata: Sends a title (:func:`send_metadata`).
            on_change: Called after every state change, on a relay thread.
            clock: Monotonic seconds, overridable for tests.
        """
        self.cfg = cfg
        self._bitrate = bitrate
        self._station_name = station_name
        self._station_details = station_details
        self._connect = connect
        self._update_metadata = update_metadata
        self._on_change = on_change
        self._clock = clock
        self._cond = threading.Condition()
        self._closed = threading.Event()
        self._title_event = threading.Event()
        self._queue: collections.deque[bytes] = collections.deque()
        self._size = 0
        self._max_bytes = 0
        self._connection: Connection | None = None
        # The socket of a login in progress, so close() can end it.
        self._login_socket: socket.socket | None = None
        self._live_bitrate = 0
        # Bumped on every login, so a title sent on an older connection
        # is not taken as sent on the new one.
        self._generation = 0
        self._live = False
        self._live_since = 0.0
        self._synced = False
        self._restart = False
        # Set by relogin(): the restart is a fresh login, without the wait
        # an encoder restart gets.
        self._relogin = False
        self._overflow = False
        self._title = ""
        self._sent_title: str | None = None
        self._state = "connecting"
        self._error: str | None = None
        self._title_error: str | None = None
        self._failures = 0
        self._logged_error: str | None = None
        self._threads = [
            station_thread(target=self._send_loop, name=f"autodj-relay-{cfg.name}", daemon=True),
            station_thread(
                target=self._title_loop, name=f"autodj-relay-title-{cfg.name}", daemon=True
            ),
        ]

    # -- sink interface (stream reader thread) -------------------------------

    def feed(self, data: bytes, title: str) -> None:
        """Queue *data* for the server if connected; note the title it carries.

        Never blocks.  While disconnected the audio is dropped, so a
        reconnect starts with live audio rather than a backlog.

        Args:
            data: The next chunk of encoded MP3.
            title: The stream title for that audio.
        """
        with self._cond:
            changed = title != self._title
            self._title = title
            if self._live and not self._overflow:
                if not self._synced:
                    # A new connection starts on a whole MP3 frame.
                    start = mp3_frame_offset(data)
                    data = data[start:] if start >= 0 else b""
                    self._synced = start >= 0
                if data:
                    self._queue.append(data)
                    self._size += len(data)
                    if self._size > self._max_bytes:
                        self._overflow = True
                    self._cond.notify()
        if changed:
            self._title_event.set()

    def encoder_changed(self) -> None:
        """Log in again: the output restarted its encoder, maybe at a new bitrate."""
        with self._cond:
            if self._live:
                self._restart = True
                self._cond.notify()

    def relogin(self) -> None:
        """Log in again at once, so the server gets the station's new details.

        Only a live relay logs in again; one still connecting reads the
        details at its next login anyway.
        """
        with self._cond:
            if self._live:
                self._restart = True
                self._relogin = True
                self._cond.notify()

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Start the sender and title threads."""
        for thread in self._threads:
            thread.start()

    def close(self) -> None:
        """Disconnect and stop both threads, waiting at most ``CLOSE_TIMEOUT``."""
        self.begin_close()
        self.finish_close(time.monotonic() + CLOSE_TIMEOUT)

    def begin_close(self) -> None:
        """Tell both threads to stop and cut any connection or login, without waiting."""
        with self._cond:
            self._closed.set()
            connection, login = self._connection, self._login_socket
            self._cond.notify_all()
        self._title_event.set()
        if connection is not None:
            connection.close()
        if login is not None:
            _shut_down(login)

    def finish_close(self, deadline: float) -> None:
        """Wait for both threads until *deadline* (``time.monotonic``), then show "stopped".

        A thread still stuck after the deadline (in a host name lookup,
        say) is left to end on its own; it changes nothing once closed.
        """
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._set_state("stopped", None)

    def status(self) -> dict[str, Any]:
        """Return this relay's state for the web page and the status file.

        ``state`` is ``"connecting"``, ``"live"``, ``"retrying"`` or
        ``"stopped"``; ``error`` is the last failure while not live, and
        ``title_error`` the last failed title update.  No password.
        """
        with self._cond:
            return {
                "name": self.cfg.name,
                "type": self.cfg.type,
                "target": describe_target(self.cfg),
                "state": self._state,
                "error": self._error,
                "title_error": self._title_error,
            }

    # -- sender thread -----------------------------------------------------------

    def _set_state(self, state: str, error: str | None) -> None:
        """Record a state change and tell the manager; only "stopped" once closed."""
        with self._cond:
            if self._closed.is_set() and state != "stopped":
                return
            if (self._state, self._error) == (state, error):
                return
            self._state, self._error = state, error
        try:
            self._on_change()
        except Exception:
            logger.debug("Relay status callback failed", exc_info=True)

    def _next_delay(self) -> float:
        """Count a failure and return how long to wait before trying again."""
        self._failures += 1
        return min(BACKOFF_START * 2 ** (self._failures - 1), BACKOFF_MAX)

    def _fail(self, error: str) -> None:
        """Log *error* (once while it repeats), then wait out the backoff."""
        delay = self._next_delay()
        self._set_state("retrying", error)
        if error != self._logged_error:
            logger.warning(
                "Relay %s (%s): %s; trying again in %.0f s, then less often while it fails",
                self.cfg.name,
                describe_target(self.cfg),
                error,
                delay,
            )
            self._logged_error = error
        else:
            logger.debug("Relay %s: %s; next try in %.0f s", self.cfg.name, error, delay)
        self._closed.wait(delay)

    def _send_loop(self) -> None:
        """Log in, send audio until something fails, wait, and repeat until closed."""
        while not self._closed.is_set():
            if self._state != "retrying":
                self._set_state("connecting", None)
            try:
                connection = self._connect(
                    self.cfg,
                    bitrate=self._bitrate(),
                    station_name=self._station_name(),
                    details=self._station_details(),
                    on_socket=self._login_started,
                )
            except Exception as exc:
                if not self._closed.is_set():
                    self._fail(describe_error(exc))
                continue
            finally:
                with self._cond:
                    self._login_socket = None
            if self._closed.is_set():
                connection.close()
                return
            started = self._clock()
            error = self._pump(connection)
            if self._closed.is_set():
                return
            if self._clock() - started >= STABLE_SECONDS:
                self._failures = 0
            with self._cond:
                relogin, self._relogin = self._relogin, False
            if error is not None:
                self._fail(error)
            elif relogin:
                logger.info(
                    "Relay %s: logging in again with the new station details", self.cfg.name
                )
            elif self._bitrate() == self._live_bitrate:
                # The encoder restarted at the same bitrate, as after a
                # crash: wait like after a failure, so an encoder that keeps
                # crashing does not cause a login per crash.
                delay = self._next_delay()
                self._set_state("connecting", None)
                logger.debug(
                    "Relay %s: the encoder restarted; logging in again in %.0f s",
                    self.cfg.name,
                    delay,
                )
                self._closed.wait(delay)

    def _login_started(self, sock: socket.socket) -> None:
        """Remember the socket of the login in progress, or cut it if closing."""
        with self._cond:
            if not self._closed.is_set():
                self._login_socket = sock
                return
        _shut_down(sock)

    def _go_live(self, connection: Connection) -> None:
        """Make *connection* current and empty the queue."""
        with self._cond:
            self._connection = connection
            self._queue.clear()
            self._size = 0
            self._live_bitrate = self._bitrate()
            self._max_bytes = int(self._live_bitrate * 1000 / 8 * _QUEUE_SECONDS)
            self._live, self._synced, self._restart, self._overflow = True, False, False, False
            self._generation += 1
            self._sent_title = None
            self._live_since = self._clock()
        self._set_state("live", None)
        self._title_event.set()
        if self._logged_error is not None or self._failures == 0:
            logger.info("Relay %s is live on %s", self.cfg.name, describe_target(self.cfg))
        self._logged_error = None

    def _pump(self, connection: Connection) -> str | None:
        """Send queued audio over *connection* until it fails or must restart.

        Returns:
            The failure, or ``None`` for a clean end (closed, or the
            encoder restarted and the relay should log in again at once).
        """
        self._go_live(connection)
        try:
            while True:
                with self._cond:
                    while not (
                        self._queue or self._closed.is_set() or self._restart or self._overflow
                    ):
                        self._cond.wait(0.5)
                    if self._closed.is_set() or self._restart:
                        return None
                    if self._overflow:
                        return "the server is not taking the audio fast enough"
                    data = b"".join(self._queue)
                    self._queue.clear()
                    self._size = 0
                connection.send(data)
        except Exception as exc:
            return f"lost the connection: {describe_error(exc)}"
        finally:
            with self._cond:
                self._live = False
                self._connection = None
                self._queue.clear()
                self._size = 0
            connection.close()

    # -- title thread ------------------------------------------------------------

    def _title_loop(self) -> None:
        """Send each new title to the server while connected."""
        while not self._closed.is_set():
            self._title_event.wait()
            self._title_event.clear()
            while not self._closed.is_set():
                with self._cond:
                    live, title, sent = self._live, self._title, self._sent_title
                    generation = self._generation
                    wait = self._live_since + FIRST_TITLE_DELAY - self._clock()
                if not live or not title or title == sent:
                    break
                if wait > 0:
                    self._closed.wait(wait)
                    continue
                self._send_title(title, generation)

    def _send_title(self, title: str, generation: int) -> None:
        """Send *title*; a failure is recorded and not retried until the next title."""
        try:
            self._update_metadata(self.cfg, title)
            error = None
        except Exception as exc:
            error = describe_error(exc)
        with self._cond:
            if self._closed.is_set():
                return
            if generation == self._generation:
                self._sent_title = title
            changed = error != self._title_error
            self._title_error = error
        if error is not None and changed:
            logger.warning("Relay %s: could not update the title: %s", self.cfg.name, error)
        if changed:
            try:
                self._on_change()
            except Exception:
                logger.debug("Relay status callback failed", exc_info=True)


class RelayManager:
    """The stream's relays, the extra encoders they need, and their status file.

    A relay without its own bitrate is a sink of the stream's own output
    and sends exactly the audio listeners get.  Relays with a fixed
    bitrate share one extra output per bitrate, which the server adds to
    the mix bus beside the stream's.

    Relays can be added, replaced and removed while the station plays
    (:meth:`apply`, :meth:`remove`).  Each change touches only the relay
    it names: the stream's own encoder, its listeners and the other relays
    carry on, an extra encoder is made only for a bitrate no relay used
    yet, and one no relay uses any more is detached and closed.
    """

    def __init__(
        self,
        relays: Iterable[RelayConfig],
        main: Any,
        *,
        make_output: Callable[[int], Any],
        station_name: Callable[[], str],
        station_details: Callable[[], StationDetails] = lambda: NO_DETAILS,
        status_path: Path | None = None,
        relay_factory: Callable[..., Relay] = Relay,
        attach_output: Callable[[Any], None] | None = None,
        detach_output: Callable[[Any], None] | None = None,
    ) -> None:
        """Build a relay for every enabled entry; :meth:`start` connects them.

        Args:
            relays: The configured relays, enabled or not.
            main: The stream's own output.
            make_output: Builds an extra output for a fixed bitrate.
            station_name: Returns the station name to announce; it can
                change while AutoDJ runs and is read at each login.
            station_details: Returns the station's genre, website and
                description, read at each login.
            status_path: Where to write the status file, or ``None``.
            relay_factory: Builds each :class:`Relay`, for tests.
            attach_output: Adds an extra output to the mix bus; called by
                :meth:`start` and for each one made after it.
            detach_output: Takes an extra output off the mix bus before it
                is closed.
        """
        self._main = main
        self._make_output = make_output
        self._station_name = station_name
        self._station_details = station_details
        self._status_path = status_path
        self._relay_factory = relay_factory
        self._attach_output = attach_output
        self._detach_output = detach_output
        self._status_lock = threading.Lock()
        # Held for a whole change (apply, remove, close), which may wait up
        # to CLOSE_TIMEOUT for the relay it replaces, so changes run one at
        # a time.
        self._change_lock = threading.Lock()
        # Held only briefly, around reading or changing the collections
        # below: a relay thread reporting its state reads them, and must
        # never wait for a change that is waiting for that thread.
        self._lock = threading.Lock()
        self._started = False
        self._closed = False
        self._title: tuple[str, str] | None = None
        self._configs: dict[str, RelayConfig] = {}
        self._relays: dict[str, Relay] = {}
        # The output each relay is a sink of.
        self._sources: dict[str, Any] = {}
        self._by_bitrate: dict[int, Any] = {}
        self.outputs: list[Any] = []
        for cfg in relays:
            self._configs[cfg.name] = cfg
            if cfg.enabled:
                self._add_relay(cfg, self._output_for(cfg.bitrate))

    @property
    def active(self) -> bool:
        """Whether any relay is enabled; the station then keeps playing."""
        return bool(self._relays)

    def start(self) -> None:
        """Put the extra outputs on the mix bus and start every relay connecting."""
        with self._change_lock:
            with self._lock:
                self._started = True
                outputs = list(self.outputs)
                relays = list(self._relays.values())
            if self._attach_output is not None:
                for output in outputs:
                    self._attach_output(output)
            self._write_status()
            for relay in relays:
                relay.start()

    def set_title(self, artist: str, title: str) -> None:
        """Retitle the extra outputs (the stream's own is retitled by the bridge)."""
        with self._lock:
            self._title = (artist, title)
            outputs = list(self.outputs)
        for output in outputs:
            output.set_title(artist, title)

    def status(self) -> list[dict[str, Any]]:
        """Return every configured relay's state, disabled ones as ``"off"``."""
        with self._lock:
            configs = list(self._configs.values())
            live = {name: relay.status() for name, relay in self._relays.items()}
        return [
            live.get(cfg.name)
            or {
                "name": cfg.name,
                "type": cfg.type,
                "target": describe_target(cfg),
                "state": "off",
                "error": None,
                "title_error": None,
            }
            for cfg in configs
        ]

    # -- changes while running ------------------------------------------------

    def apply(self, cfg: RelayConfig) -> None:
        """Add relay *cfg*, or replace the relay of that name with it.

        The new relay's encoder is found or made first, so when that fails
        the relay that was running keeps running.  Then the old relay is
        stopped (waiting at most ``CLOSE_TIMEOUT``) and the new one, when
        enabled, starts connecting.  Nothing else is touched.

        Raises:
            RelayError: The relays are stopping.
            Exception: Making the extra encoder failed; nothing changed.
        """
        with self._change_lock:
            self._refuse_once_closed()
            output = self._output_for(cfg.bitrate) if cfg.enabled else None
            self._stop_relay(cfg.name)
            with self._lock:
                self._configs[cfg.name] = cfg
            if output is not None:
                self._add_relay(cfg, output)
            self._release_unused()
        self._write_status()

    def remove(self, name: str) -> None:
        """Stop and forget relay *name*; an unknown name does nothing.

        Raises:
            RelayError: The relays are stopping.
        """
        with self._change_lock:
            self._refuse_once_closed()
            self._stop_relay(name)
            with self._lock:
                self._configs.pop(name, None)
            self._release_unused()
        self._write_status()

    def reconnect(self) -> None:
        """Make every live relay log in again, to send the station's new details."""
        with self._lock:
            relays = list(self._relays.values())
        for relay in relays:
            relay.relogin()

    def _refuse_once_closed(self) -> None:
        with self._lock:
            if self._closed:
                raise RelayError("the stream is stopping")

    def _output_for(self, bitrate: int | None) -> Any:
        """The output a relay at *bitrate* is a sink of; a new one is made and attached."""
        if bitrate is None:
            return self._main
        with self._lock:
            output = self._by_bitrate.get(bitrate)
            title, started = self._title, self._started
        if output is not None:
            return output
        output = self._make_output(bitrate)
        if title is not None:
            output.set_title(*title)
        with self._lock:
            self._by_bitrate[bitrate] = output
            self.outputs.append(output)
        if started and self._attach_output is not None:
            self._attach_output(output)
        return output

    def _add_relay(self, cfg: RelayConfig, output: Any) -> None:
        relay = self._relay_factory(
            cfg,
            bitrate=lambda output=output: int(output.bitrate),
            station_name=self._station_name,
            station_details=self._station_details,
            on_change=self._write_status,
        )
        output.add_sink(relay)
        with self._lock:
            self._relays[cfg.name] = relay
            self._sources[cfg.name] = output
            started = self._started
        if started:
            relay.start()

    def _stop_relay(self, name: str) -> None:
        """Take relay *name* off its output and stop it, if it runs."""
        with self._lock:
            relay = self._relays.pop(name, None)
            output = self._sources.pop(name, None)
        if relay is None:
            return
        if output is not None:
            output.remove_sink(relay)
        try:
            relay.close()
        except Exception:
            logger.exception("Stopping relay %s failed", name)

    def _release_unused(self) -> None:
        """Detach and close each extra output no relay is a sink of any more."""
        with self._lock:
            used = {id(output) for output in self._sources.values()}
            unused = [
                (bitrate, output)
                for bitrate, output in self._by_bitrate.items()
                if id(output) not in used
            ]
            for bitrate, output in unused:
                del self._by_bitrate[bitrate]
                self.outputs.remove(output)
        for _bitrate, output in unused:
            try:
                if self._detach_output is not None:
                    self._detach_output(output)
                output.close()
            except Exception:
                logger.exception("Closing a relay encoder failed")

    # -- shutdown ----------------------------------------------------------------

    def close(self) -> None:
        """Stop every relay, then the extra outputs.

        Every relay is told to stop first, then all are waited for against
        one shared deadline, so shutdown takes at most ``CLOSE_TIMEOUT``
        however many relays are stuck.  Changes asked for after this are
        refused.
        """
        with self._change_lock:
            with self._lock:
                self._closed = True
                relays = list(self._relays.values())
                outputs = list(self.outputs)
            self._close_all(relays, outputs)

    def _close_all(self, relays: list[Relay], outputs: list[Any]) -> None:
        for relay in relays:
            try:
                relay.begin_close()
            except Exception:
                logger.exception("Stopping relay %s failed", relay.cfg.name)
        deadline = time.monotonic() + CLOSE_TIMEOUT
        for relay in relays:
            try:
                relay.finish_close(deadline)
            except Exception:
                logger.exception("Stopping relay %s failed", relay.cfg.name)
        for output in outputs:
            try:
                if self._detach_output is not None:
                    self._detach_output(output)
                output.close()
            except Exception:
                logger.exception("Closing a relay encoder failed")

    def _write_status(self) -> None:
        """Rewrite the status file for ``autodj doctor``; failures are only logged.

        A station that never had a relay writes none.
        """
        if self._status_path is None:
            return
        with self._status_lock:
            relays = self.status()
            if not relays and not self._status_path.exists():
                return
            payload = {"updated": datetime.now(UTC).isoformat(timespec="seconds")}
            text = json.dumps({**payload, "relays": relays}, indent=2) + "\n"
            try:
                atomic_write(self._status_path, text)
            except OSError:
                logger.debug("Could not write the relay status file", exc_info=True)


def read_status_file(path: Path) -> Mapping[str, Any] | None:
    """Return the relay status file's contents, or ``None`` if unreadable.

    Args:
        path: The file :class:`RelayManager` writes.

    Returns:
        ``{"updated": <ISO time>, "relays": {name: status}}``, or ``None``.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        relays = {item["name"]: item for item in data["relays"] if isinstance(item, dict)}
        return {"updated": str(data["updated"]), "relays": relays}
    except (OSError, ValueError, KeyError, TypeError):
        return None
