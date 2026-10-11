"""Relays against in-process fake Icecast and Shoutcast servers over real sockets.

The fakes speak just enough of each protocol to check what AutoDJ sends:
the source login, the audio bytes, and the title updates.  They are not
real Icecast or Shoutcast servers.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest

from autodj import relay as relay_module
from autodj.config import RelayConfig
from autodj.relay import RelayManager
from autodj.stream import StreamOutput

PASSWORD = "fake-server-password"
FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413


class FrameEncoder:
    """Stands in for FFmpeg: every PCM block becomes one MP3 frame."""

    def __init__(self, bitrate: int) -> None:
        self.bitrate = bitrate
        self.alive = True
        self._cond = threading.Condition()
        self._frames = 0

    def write(self, pcm: bytes) -> None:
        with self._cond:
            self._frames += 1
            self._cond.notify_all()

    def read(self, n: int) -> bytes:
        with self._cond:
            while not self._frames and self.alive:
                self._cond.wait(0.05)
            if not self._frames:
                return b""
            self._frames -= 1
            return FRAME

    def close(self) -> None:
        with self._cond:
            self.alive = False
            self._cond.notify_all()


def _read_head(conn: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(1024)
        if not chunk:
            break
        data += chunk
    return data


class FakeServer:
    """A fake Icecast (one port) or Shoutcast (port and port + 1) server."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.sockets = self._bind()
        self.port = self.sockets[0].getsockname()[1]
        self.audio = bytearray()
        self.titles: list[str] = []
        self.logins: list[bytes] = []
        self.sources: list[socket.socket] = []
        self.lock = threading.Lock()
        self.stopping = False
        self.threads = [
            threading.Thread(target=self._accept, args=(s,), daemon=True) for s in self.sockets
        ]
        for thread in self.threads:
            thread.start()

    def _bind(self) -> list[socket.socket]:
        for _ in range(50):
            first = socket.socket()
            first.bind(("127.0.0.1", 0))
            if self.kind == "icecast":
                first.listen()
                return [first]
            second = socket.socket()
            try:
                second.bind(("127.0.0.1", first.getsockname()[1] + 1))
            except OSError:
                first.close()
                second.close()
                continue
            first.listen()
            second.listen()
            return [first, second]
        pytest.skip("no two free neighbouring ports")

    def _accept(self, server: socket.socket) -> None:
        while not self.stopping:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn, server), daemon=True).start()

    def _handle(self, conn: socket.socket, server: socket.socket) -> None:
        with contextlib.suppress(OSError):
            if self.kind != "icecast" and server is self.sockets[1]:
                self._shoutcast_source(conn)
            else:
                self._http(conn)

    def _http(self, conn: socket.socket) -> None:
        head = _read_head(conn)
        line = head.split(b"\r\n", 1)[0].decode()
        method, target, _version = line.split(" ", 2)
        if method == "PUT":
            with self.lock:
                self.logins.append(head)
            expected = base64.b64encode(f"source:{PASSWORD}".encode())
            if b"Authorization: Basic " + expected not in head:
                conn.sendall(b"HTTP/1.0 401 Authentication Required\r\n\r\n")
                conn.close()
                return
            conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
            self._receive_audio(conn)
            return
        query = parse_qs(urlsplit(target).query)
        if self.kind == "icecast":
            assert target.startswith("/admin/metadata?")
            assert query["mount"] == ["/live"]
            expected = base64.b64encode(f"source:{PASSWORD}".encode())
            ok = b"Authorization: Basic " + expected in head
        else:
            assert target.startswith("/admin.cgi?")
            ok = query.get("pass") == [PASSWORD]
            if self.kind == "shoutcast2":
                ok = ok and query.get("sid") == ["2"]
        if ok:
            with self.lock:
                self.titles.append(query["song"][0])
        conn.sendall(
            b"HTTP/1.0 200 OK\r\nContent-Length: 0\r\n\r\n" if ok else b"HTTP/1.0 401 x\r\n\r\n"
        )
        conn.close()

    def _shoutcast_source(self, conn: socket.socket) -> None:
        line = b""
        while not line.endswith(b"\r\n"):
            line += conn.recv(1)
        with self.lock:
            self.logins.append(line)
        expected = PASSWORD + (":#2" if self.kind == "shoutcast2" else "")
        if line.strip().decode() != expected:
            conn.sendall(b"invalid password\r\n")
            conn.close()
            return
        conn.sendall(b"OK2\r\nicy-caps:11\r\n\r\n")
        head = _read_head(conn)
        assert b"content-type:audio/mpeg" in head
        self._receive_audio(conn)

    def _receive_audio(self, conn: socket.socket) -> None:
        with self.lock:
            self.sources.append(conn)
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                return
            with self.lock:
                self.audio += chunk

    def drop_sources(self) -> None:
        """Cut every source connection, like a server restart."""
        with self.lock:
            sources, self.sources = self.sources, []
        for conn in sources:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
            conn.close()

    def close(self) -> None:
        self.stopping = True
        self.drop_sources()
        for server in self.sockets:
            server.close()


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate()


def _config(kind: str, port: int) -> RelayConfig:
    data: dict = {"type": kind, "host": "127.0.0.1", "port": port, "password": PASSWORD}
    if kind == "icecast":
        data["mount"] = "/live"
    if kind == "shoutcast2":
        data["stream_id"] = 2
    return RelayConfig.from_table(kind, data)


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(relay_module, "BACKOFF_START", 0.05)
    monkeypatch.setattr(relay_module, "FIRST_TITLE_DELAY", 0.1)


@pytest.mark.parametrize("kind", ["icecast", "shoutcast1", "shoutcast2"])
def test_relay_streams_retitles_and_recovers(kind: str, fast) -> None:
    server = FakeServer(kind)
    loop = asyncio.new_event_loop()
    output = StreamOutput(128, 1, encoder_factory=FrameEncoder, loop=loop)
    manager = RelayManager(
        [_config(kind, server.port)],
        output,
        make_output=lambda bitrate: pytest.fail("no extra encoder expected"),
        station_name=lambda: "Test FM",
    )
    block = np.zeros((882, 2), np.float32)
    try:
        manager.start()
        _wait_until(lambda: manager.status()[0]["state"] == "live")
        output.set_title("First", "Song")
        for _ in range(5):
            output.write(block)
        _wait_until(lambda: len(server.audio) >= 5 * len(FRAME))
        assert bytes(server.audio[: len(FRAME)]) == FRAME  # starts on a whole frame
        _wait_until(lambda: server.titles == ["First - Song"])

        server.drop_sources()  # the server goes away mid-stream

        def back_live() -> bool:
            output.write(block)  # the mix keeps sending; a send notices the drop
            return len(server.logins) == 2 and manager.status()[0]["state"] == "live"

        _wait_until(back_live)
        before = len(server.audio)
        output.set_title("Second", "Song")
        for _ in range(5):
            output.write(block)
        _wait_until(lambda: len(server.audio) >= before + 5 * len(FRAME))
        _wait_until(lambda: server.titles[-1] == "Second - Song")
    finally:
        manager.close()
        output.close()
        server.close()
        loop.close()
    assert manager.status()[0]["state"] == "stopped"


def test_wrong_password_keeps_retrying_with_the_reason(fast) -> None:
    server = FakeServer("icecast")
    relay_cfg = RelayConfig.from_table(
        "bad",
        {
            "type": "icecast",
            "host": "127.0.0.1",
            "port": server.port,
            "mount": "/live",
            "password": "nope",
        },
    )
    loop = asyncio.new_event_loop()
    output = StreamOutput(128, 1, encoder_factory=FrameEncoder, loop=loop)
    manager = RelayManager(
        [relay_cfg], output, make_output=lambda b: None, station_name=lambda: "T"
    )
    try:
        manager.start()
        _wait_until(lambda: len(server.logins) >= 2)
        status = manager.status()[0]
        assert status["state"] == "retrying"
        assert status["error"] == "the server rejected the user name or password"
    finally:
        manager.close()
        output.close()
        server.close()
        loop.close()
