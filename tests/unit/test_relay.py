"""Relays to Icecast and Shoutcast, with fake sockets and fake servers."""

from __future__ import annotations

import itertools
import json
import logging
import socket
import threading
import time
from pathlib import Path
from typing import Any, ClassVar

import pytest

from autodj import relay as relay_module
from autodj.config import RelayConfig
from autodj.relay import (
    Relay,
    RelayError,
    RelayManager,
    describe_error,
    open_source,
    read_status_file,
    send_metadata,
)

PASSWORD = "s3cret-Pa55"
# One MPEG-1 Layer III frame (128 kbps, 44.1 kHz): 417 bytes.
FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413


def _icecast(**extra: Any) -> RelayConfig:
    data = {"type": "icecast", "host": "radio.example.com", "mount": "/live", "password": PASSWORD}
    return RelayConfig.from_table("main", {**data, **extra})


def _shoutcast(kind: str = "shoutcast1", **extra: Any) -> RelayConfig:
    data = {"type": kind, "host": "sc.example.com", "port": 8010, "password": PASSWORD}
    return RelayConfig.from_table("sc", {**data, **extra})


def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


class FakeSocket:
    """Records what a login sends and replies with a script."""

    def __init__(self, reply: bytes) -> None:
        self.reply = reply
        self.sent = b""
        self.closed = False
        self.timeout: float | None = None

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, n: int) -> bytes:
        data, self.reply = self.reply[:n], self.reply[n:]
        return data

    def shutdown(self, _how: int) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _connector(sock: FakeSocket, seen: list):
    def connect(address, timeout):
        seen.append((address, timeout))
        return sock

    return connect


# -- logging in -----------------------------------------------------------------


@pytest.mark.parametrize("reply", [b"HTTP/1.1 100 Continue\r\n\r\n", b"HTTP/1.0 200 OK\r\n\r\n"])
def test_icecast_login_sends_put_with_basic_auth(reply: bytes) -> None:
    sock, seen = FakeSocket(reply), []
    conn = open_source(
        _icecast(public=True), bitrate=320, station_name="Café FM", connect=_connector(sock, seen)
    )
    assert seen == [(("radio.example.com", 8000), relay_module.CONNECT_TIMEOUT)]
    head = sock.sent.decode("utf-8")
    assert head.startswith("PUT /live HTTP/1.1\r\n")
    assert "Host: radio.example.com:8000\r\n" in head
    assert "Authorization: Basic c291cmNlOnMzY3JldC1QYTU1\r\n" in head  # source:s3cret-Pa55
    assert "Content-Type: audio/mpeg\r\n" in head
    assert "Ice-Name: Café FM\r\n" in head
    assert "Ice-Public: 1\r\n" in head
    assert "Ice-Bitrate: 320\r\n" in head
    assert head.endswith("\r\n\r\n")
    conn.send(b"audio")
    assert sock.sent.endswith(b"audio")
    conn.close()
    assert sock.closed


def test_icecast_ipv6_host_header_is_bracketed() -> None:
    sock = FakeSocket(b"HTTP/1.1 100 Continue\r\n\r\n")
    open_source(_icecast(host="::1"), bitrate=128, station_name="A", connect=_connector(sock, []))
    assert b"Host: [::1]:8000\r\n" in sock.sent


def test_icecast_login_sends_station_metadata_as_utf8_headers() -> None:
    sock = FakeSocket(b"HTTP/1.1 100 Continue\r\n\r\n")
    open_source(
        _icecast(),
        bitrate=128,
        station_name="Radio",
        station_metadata={
            "name": "Café Radio",
            "genre": "Jazz",
            "description": "Late night",
            "url": "https://radio.example/",
        },
        connect=_connector(sock, []),
    )
    head = sock.sent.decode("utf-8")
    assert "Ice-Name: Café Radio\r\n" in head
    assert "Ice-Genre: Jazz\r\n" in head
    assert "Ice-Description: Late night\r\n" in head
    assert "Ice-Url: https://radio.example/\r\n" in head


def test_shoutcast_login_sends_supported_station_metadata() -> None:
    sock = FakeSocket(b"OK2\r\nicy-caps:11\r\n\r\n")
    open_source(
        _shoutcast(),
        bitrate=128,
        station_name="Radio",
        station_metadata={
            "name": "Café Radio",
            "genre": "Jazz",
            "description": "not supported",
            "url": "https://radio.example/",
        },
        connect=_connector(sock, []),
    )
    assert (
        "icy-name:Café Radio\r\nicy-genre:Jazz\r\nicy-url:https://radio.example/\r\n".encode()
        in sock.sent
    )
    assert b"not supported" not in sock.sent


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (b"HTTP/1.0 401 Authentication Required\r\n\r\n", "rejected the user name or password"),
        (b"HTTP/1.0 403 Mountpoint in use\r\n\r\n", "mount point may already be in use"),
        (b"HTTP/1.0 500 Internal Server Error\r\n\r\n", "HTTP status 500"),
        (b"ICY 200 OK\r\n\r\n", "did not answer like an Icecast server"),
        (b"", "closed the connection during login"),
    ],
)
def test_icecast_refusals(reply: bytes, message: str) -> None:
    sock = FakeSocket(reply)
    with pytest.raises(RelayError, match=message) as caught:
        open_source(_icecast(), bitrate=320, station_name="A", connect=_connector(sock, []))
    assert sock.closed
    assert PASSWORD not in str(caught.value)


def test_overlong_reply_is_refused() -> None:
    sock = FakeSocket(b"HTTP/1.1 200 OK\r\n" + b"X-Pad: y\r\n" * 2000)
    with pytest.raises(RelayError, match="overlong"):
        open_source(_icecast(), bitrate=320, station_name="A", connect=_connector(sock, []))


def test_shoutcast1_login_uses_the_port_above() -> None:
    sock, seen = FakeSocket(b"OK2\r\nicy-caps:11\r\n\r\n"), []
    open_source(_shoutcast(), bitrate=192, station_name="Radio", connect=_connector(sock, seen))
    assert seen[0][0] == ("sc.example.com", 8011)
    assert sock.sent.startswith(f"{PASSWORD}\r\n".encode())
    assert b"icy-name:Radio\r\n" in sock.sent
    assert b"icy-br:192\r\n" in sock.sent
    assert b"icy-pub:0\r\n" in sock.sent
    assert sock.sent.endswith(b"content-type:audio/mpeg\r\n\r\n")


def test_shoutcast2_login_names_the_stream_id() -> None:
    sock = FakeSocket(b"OK2\r\nicy-caps:11\r\n\r\n")
    open_source(
        _shoutcast("shoutcast2", stream_id=3),
        bitrate=320,
        station_name="Radio",
        connect=_connector(sock, []),
    )
    assert sock.sent.startswith(f"{PASSWORD}:#3\r\n".encode())


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (b"invalid password\r\n", "rejected the password"),
        (b"Stream in use\r\n", "did not accept the source"),
        (b"", "closed the connection during login"),
    ],
)
def test_shoutcast_refusals(reply: bytes, message: str) -> None:
    sock = FakeSocket(reply)
    with pytest.raises(RelayError, match=message):
        open_source(_shoutcast(), bitrate=320, station_name="A", connect=_connector(sock, []))
    assert sock.closed


# -- title updates ------------------------------------------------------------------


class FakeHTTP:
    """Records one metadata request."""

    made: ClassVar[list[FakeHTTP]] = []

    def __init__(self, host: str, port: int, timeout: float, status: int = 200) -> None:
        self.args = (host, port, timeout)
        self.status = status
        self.request_args: tuple = ()
        self.closed = False
        FakeHTTP.made.append(self)

    def request(self, method: str, path: str, headers: dict) -> None:
        self.request_args = (method, path, headers)

    def getresponse(self) -> FakeHTTP:
        return self

    def read(self, _n: int) -> bytes:
        return b"<iceresponse/>"

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_http():
    FakeHTTP.made = []
    return FakeHTTP


def test_icecast_title_update(fake_http) -> None:
    send_metadata(_icecast(), "Ünïcode - Song & Co", connection_factory=fake_http)
    call = fake_http.made[0]
    assert call.args == ("radio.example.com", 8000, relay_module.METADATA_TIMEOUT)
    method, path, headers = call.request_args
    assert method == "GET"
    assert path.startswith("/admin/metadata?mode=updinfo&mount=%2Flive&song=")
    assert "song=%C3%9Cn%C3%AFcode+-+Song+%26+Co" in path
    assert "charset=UTF-8" in path
    assert PASSWORD not in path
    assert headers["Authorization"] == "Basic c291cmNlOnMzY3JldC1QYTU1"
    assert call.closed


@pytest.mark.parametrize(("kind", "sid"), [("shoutcast1", None), ("shoutcast2", "2")])
def test_shoutcast_title_update(fake_http, kind: str, sid: str | None) -> None:
    extra = {"stream_id": 2} if sid else {}
    send_metadata(_shoutcast(kind, **extra), "A - B", connection_factory=fake_http)
    call = fake_http.made[0]
    assert call.args[:2] == ("sc.example.com", 8010)
    _method, path, headers = call.request_args
    assert path.startswith("/admin.cgi?mode=updinfo&song=A+-+B&pass=")
    assert ("&sid=2" in path) is bool(sid)
    assert "Mozilla" in headers["User-Agent"]


@pytest.mark.parametrize(
    ("status", "message"), [(401, "rejected the password"), (404, "HTTP status 404")]
)
def test_title_update_refusals(status: int, message: str) -> None:
    def factory(host, port, timeout):
        return FakeHTTP(host, port, timeout, status=status)

    with pytest.raises(RelayError, match=message):
        send_metadata(_icecast(), "A - B", connection_factory=factory)


# -- error wording ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "text"),
    [
        (RelayError("custom"), "custom"),
        (socket.gaierror(11001, "getaddrinfo failed"), "name could not be found"),
        (TimeoutError(), "did not answer in time"),
        (ConnectionRefusedError(111, "refused"), "refused the connection"),
        (ConnectionResetError(104, "reset"), "closed the connection"),
        (OSError(5, "Input/output error"), "network error: Input/output error"),
        (OSError("bare"), "unexpected error (OSError)"),
        (ValueError("x"), "unexpected error (ValueError)"),
    ],
)
def test_describe_error(exc: BaseException, text: str) -> None:
    assert text in describe_error(exc)


def test_describe_error_for_bad_http_reply() -> None:
    import http.client

    assert "could not read" in describe_error(http.client.BadStatusLine("x"))


# -- the relay loop ----------------------------------------------------------------


class FakeConnection:
    def __init__(self, fail_after: int | None = None) -> None:
        self.sent: list[bytes] = []
        self.closed = False
        self.fail_after = fail_after

    def send(self, data: bytes) -> None:
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise ConnectionResetError(104, "reset")
        self.sent.append(data)

    def close(self) -> None:
        self.closed = True


class Script:
    """A connect function that fails or succeeds in a scripted order."""

    def __init__(self, *steps: Any) -> None:
        self.steps = list(steps)
        self.calls: list[dict] = []
        self.connections: list[FakeConnection] = []

    def __call__(self, cfg, *, bitrate, station_name, on_socket=None):
        self.calls.append({"bitrate": bitrate, "station_name": station_name})
        step = self.steps.pop(0) if self.steps else FakeConnection()
        if isinstance(step, BaseException):
            raise step
        self.connections.append(step)
        return step


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(relay_module, "BACKOFF_START", 0.01)
    monkeypatch.setattr(relay_module, "BACKOFF_MAX", 0.04)
    monkeypatch.setattr(relay_module, "FIRST_TITLE_DELAY", 0.0)


def _relay(connect, titles: list | None = None, **kwargs: Any) -> Relay:
    def update(cfg, title):
        if titles is None:
            return
        titles.append(title)

    return Relay(
        kwargs.pop("cfg", _icecast()),
        bitrate=kwargs.pop("bitrate", lambda: 128),
        station_name=lambda: "Radio",
        connect=connect,
        update_metadata=kwargs.pop("update_metadata", update),
        **kwargs,
    )


def test_relay_sends_audio_from_a_frame_boundary(fast) -> None:
    script = Script()
    relay = _relay(script)
    relay.start()
    try:
        _wait_until(lambda: relay.status()["state"] == "live")
        relay.feed(b"junk" + FRAME + FRAME, "A - B")
        relay.feed(FRAME, "A - B")
        _wait_until(lambda: b"".join(script.connections[0].sent) == FRAME * 3)
        assert script.calls[0] == {"bitrate": 128, "station_name": "Radio"}
    finally:
        relay.close()
    assert relay.status()["state"] == "stopped"
    assert script.connections[0].closed


def test_audio_while_disconnected_is_dropped(fast) -> None:
    relay = _relay(Script())
    relay.feed(FRAME, "")
    assert not relay._queue


def test_relay_retries_with_backoff_and_recovers(fast, caplog) -> None:
    changes: list[str] = []
    script = Script(ConnectionRefusedError(111, "refused"), ConnectionRefusedError(111, "refused"))
    relay = _relay(script, on_change=lambda: changes.append(relay.status()["state"]))
    caplog.set_level(logging.DEBUG, logger="autodj.relay")
    relay.start()
    try:
        _wait_until(lambda: relay.status()["state"] == "live")
        assert len(script.calls) == 3
        assert "retrying" in changes and changes[-1] == "live"
        assert relay.status()["error"] is None
    finally:
        relay.close()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # the same error is logged once, then at debug level
    assert "refused the connection" in warnings[0].getMessage()
    assert any("is live" in r.getMessage() for r in caplog.records)
    assert PASSWORD not in caplog.text


def test_backoff_doubles_up_to_the_cap() -> None:
    relay = _relay(Script())
    delays = [relay._next_delay() for _ in range(9)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]


def test_a_stable_connection_resets_the_backoff(fast) -> None:
    now = [0.0]
    first = FakeConnection(fail_after=0)
    script = Script(first)
    relay = _relay(script, clock=lambda: now[0])
    relay._failures = 5
    relay.start()
    try:
        _wait_until(lambda: relay.status()["state"] == "live")
        now[0] = relay_module.STABLE_SECONDS + 1
        relay.feed(FRAME, "")
        _wait_until(lambda: len(script.connections) == 2)
        assert relay._failures <= 1
    finally:
        relay.close()


def test_dropped_connection_reconnects(fast) -> None:
    script = Script(FakeConnection(fail_after=0))
    relay = _relay(script)
    relay.start()
    try:
        _wait_until(lambda: relay.status()["state"] == "live")
        relay.feed(FRAME, "")
        _wait_until(lambda: len(script.connections) == 2 and relay.status()["state"] == "live")
        assert script.connections[0].closed
    finally:
        relay.close()


def test_server_that_falls_behind_is_dropped(fast, caplog) -> None:
    script = Script()
    relay = _relay(script)
    relay.start()
    try:
        _wait_until(lambda: relay.status()["state"] == "live")
        with relay._cond:  # hold the sender off while the queue overflows
            relay._max_bytes = len(FRAME)
            relay.feed(FRAME + FRAME, "")
            assert relay._overflow
        _wait_until(lambda: len(script.connections) == 2)
        assert "the server is not taking the audio fast enough" in caplog.text
    finally:
        relay.close()


def test_encoder_change_logs_in_again_at_the_new_bitrate(fast) -> None:
    rate = [128]
    script = Script()
    relay = _relay(script, bitrate=lambda: rate[0])
    relay.start()
    try:
        _wait_until(lambda: relay.status()["state"] == "live")
        rate[0] = 320
        relay.encoder_changed()
        _wait_until(lambda: len(script.calls) == 2)
        assert script.calls[1]["bitrate"] == 320
        assert relay.status()["error"] is None
    finally:
        relay.close()


def test_encoder_change_while_disconnected_is_ignored() -> None:
    relay = _relay(Script())
    relay.encoder_changed()
    assert not relay._restart


def test_titles_are_sent_on_login_and_on_each_change(fast) -> None:
    titles: list[str] = []
    script = Script()
    relay = _relay(script, titles)
    relay.feed(FRAME, "First - Song")  # before login: remembered, not sent
    relay.start()
    try:
        _wait_until(lambda: titles == ["First - Song"])
        relay.feed(FRAME, "First - Song")
        relay.feed(FRAME, "Second - Song")
        _wait_until(lambda: titles == ["First - Song", "Second - Song"])
    finally:
        relay.close()


def test_failed_title_update_is_shown_and_not_retried(fast, caplog) -> None:
    calls: list[str] = []

    def update(cfg, title):
        calls.append(title)
        raise RelayError("the server rejected the password for title updates")

    relay = _relay(Script(), update_metadata=update)
    relay.start()
    try:
        relay.feed(FRAME, "A - B")
        _wait_until(lambda: relay.status()["title_error"] is not None)
        time.sleep(0.05)
        assert calls == ["A - B"]
        assert "could not update the title" in caplog.text
    finally:
        relay.close()


def test_close_ends_a_blocked_send(fast) -> None:
    release = threading.Event()

    class Stuck(FakeConnection):
        def send(self, data: bytes) -> None:
            release.wait(5)
            raise OSError(9, "Bad file descriptor")

        def close(self) -> None:
            super().close()
            release.set()

    script = Script(Stuck())
    relay = _relay(script)
    relay.start()
    _wait_until(lambda: relay.status()["state"] == "live")
    relay.feed(FRAME, "")
    started = time.monotonic()
    relay.close()
    assert time.monotonic() - started < 2
    assert relay.status()["state"] == "stopped"


def test_status_never_contains_the_password(fast) -> None:
    script = Script(RelayError("the server rejected the user name or password"))
    statuses: list[dict[str, Any]] = []
    retrying = threading.Event()

    def record() -> None:
        # Called on the relay thread at each change: polling could miss the
        # retry, which lasts only the 10 ms backoff before the next login.
        statuses.append(relay.status())
        if statuses[-1]["state"] == "retrying":
            retrying.set()

    relay = _relay(script, on_change=record)
    relay.start()
    try:
        assert retrying.wait(timeout=10)
    finally:
        relay.close()
    statuses.append(relay.status())
    status = next(status for status in statuses if status["state"] == "retrying")
    assert status["target"] == "radio.example.com:8000/live"
    assert status["error"] == "the server rejected the user name or password"
    for status in statuses:
        assert PASSWORD not in json.dumps(status)


# -- the manager ----------------------------------------------------------------------


class FakeOutput:
    def __init__(self, bitrate: int) -> None:
        self.bitrate = bitrate
        self.sinks: list = []
        self.titles: list = []
        self.closed = False

    def add_sink(self, sink) -> None:
        self.sinks.append(sink)

    def remove_sink(self, sink) -> None:
        if sink in self.sinks:
            self.sinks.remove(sink)

    def set_title(self, artist: str, title: str) -> None:
        self.titles.append((artist, title))

    def close(self) -> None:
        self.closed = True


class FakeRelay:
    def __init__(self, cfg, *, bitrate, station_name, on_change, fail_start_paused=False) -> None:
        self.cfg = cfg
        self.bitrate = bitrate
        self.on_change = on_change
        self.started = self.closed = False
        self.staged = False
        self.fail_start_paused = fail_start_paused

    def start(self) -> None:
        self.started = True

    def start_paused(self) -> None:
        self.staged = True
        if self.fail_start_paused:
            raise RuntimeError("thread start failed")

    def resume(self) -> None:
        self.started = True

    def begin_close(self) -> None:
        pass

    def finish_close(self, deadline: float) -> None:
        self.closed = True

    def status(self) -> dict:
        return {"name": self.cfg.name, "state": "live"}


def test_manager_shares_encoders_by_bitrate(tmp_path: Path) -> None:
    main = FakeOutput(320)
    made: list[FakeOutput] = []

    def make_output(bitrate: int) -> FakeOutput:
        made.append(FakeOutput(bitrate))
        return made[-1]

    configs = [
        RelayConfig.from_table("a", _table()),
        RelayConfig.from_table("b", {**_table(), "bitrate": 128}),
        RelayConfig.from_table("c", {**_table(), "bitrate": 128}),
        RelayConfig.from_table("d", {**_table(), "enabled": False}),
    ]
    manager = RelayManager(
        configs,
        main,
        make_output=make_output,
        station_name=lambda: "Radio",
        status_path=tmp_path / "relay-status.json",
        relay_factory=FakeRelay,
    )
    assert manager.active
    assert [o.bitrate for o in manager.outputs] == [128]
    assert len(main.sinks) == 1 and len(made[0].sinks) == 2
    assert made[0].sinks[0].bitrate() == 128
    manager.set_title("Artist", "Song")
    assert made[0].titles == [("Artist", "Song")] and main.titles == []
    manager.start()
    assert all(relay.started for relay in main.sinks + made[0].sinks)
    status = manager.status()
    assert [s["name"] for s in status] == ["a", "b", "c", "d"]
    assert status[3]["state"] == "off"
    saved = read_status_file(tmp_path / "relay-status.json")
    assert saved is not None and saved["relays"]["d"]["state"] == "off"
    assert PASSWORD not in (tmp_path / "relay-status.json").read_text(encoding="utf-8")
    manager.close()
    assert made[0].closed and all(relay.closed for relay in main.sinks)


def _table() -> dict:
    return {"type": "icecast", "host": "h.example", "mount": "/m", "password": PASSWORD}


def test_manager_with_only_disabled_relays_is_inactive() -> None:
    cfg = RelayConfig.from_table("d", {**_table(), "enabled": False})
    manager = RelayManager([cfg], FakeOutput(320), make_output=FakeOutput, station_name=lambda: "R")
    assert not manager.active
    manager.start()
    manager.close()


def test_manager_reconfigures_only_changed_relays_and_releases_unused_outputs() -> None:
    main = FakeOutput(320)
    bus: list[FakeOutput] = []
    made: list[FakeOutput] = []

    def make_output(rate: int) -> FakeOutput:
        output = FakeOutput(rate)
        made.append(output)
        return output

    configs = [
        RelayConfig.from_table("keep", _table()),
        RelayConfig.from_table("change", _table()),
        RelayConfig.from_table("remove", {**_table(), "bitrate": 128}),
    ]
    manager = RelayManager(
        configs,
        main,
        make_output=make_output,
        station_name=lambda: "Radio",
        relay_factory=FakeRelay,
        add_output=bus.append,
        remove_output=bus.remove,
    )
    bus.extend(manager.outputs)
    manager.start()
    keep = manager._by_name["keep"][0]
    changed = manager._by_name["change"][0]
    manager.reconfigure(
        [
            configs[0],
            RelayConfig.from_table("change", {**_table(), "host": "other.example"}),
            RelayConfig.from_table("remove", {**_table(), "bitrate": 128, "enabled": False}),
            RelayConfig.from_table("add", {**_table(), "bitrate": 192}),
        ]
    )
    assert manager._by_name["keep"][0] is keep
    assert changed.closed
    assert manager._by_name["change"][0].started
    assert manager._by_name["add"][0].started
    assert bus == [made[1]] and manager.outputs == [made[1]]
    assert made[0].closed
    manager.close()


def test_manager_keeps_old_relays_when_staged_replacement_start_fails() -> None:
    main = FakeOutput(320)
    old_configs = [
        RelayConfig.from_table("first", _table()),
        RelayConfig.from_table("second", _table()),
    ]
    made: list[FakeRelay] = []

    def factory(cfg, **kwargs):
        relay = FakeRelay(cfg, **kwargs, fail_start_paused=len(made) == 3)
        made.append(relay)
        return relay

    manager = RelayManager(
        old_configs,
        main,
        make_output=FakeOutput,
        station_name=lambda: "Radio",
        relay_factory=factory,
    )
    manager.start()
    old_relays = {name: manager._by_name[name][0] for name in ("first", "second")}
    new_configs = [
        RelayConfig.from_table("first", {**_table(), "host": "new-one.example"}),
        RelayConfig.from_table("second", {**_table(), "host": "new-two.example"}),
    ]

    with pytest.raises(RuntimeError, match="thread start failed"):
        manager.reconfigure(new_configs)

    assert {name: manager._by_name[name][0] for name in old_relays} == old_relays
    assert all(not relay.closed for relay in old_relays.values())
    staged = made[2:]
    assert len(staged) == 2
    assert all(relay.closed for relay in staged)
    assert all(relay not in main.sinks for relay in staged)
    assert [cfg.host for cfg in manager._configs] == [cfg.host for cfg in old_configs]
    manager.close()


def test_manager_survives_failures(tmp_path: Path, caplog) -> None:
    class Broken(FakeRelay):
        def begin_close(self) -> None:
            raise RuntimeError("boom")

        def finish_close(self, deadline: float) -> None:
            raise RuntimeError("boom")

    class BrokenOutput(FakeOutput):
        def close(self) -> None:
            raise RuntimeError("boom")

    configs = [RelayConfig.from_table("b", {**_table(), "bitrate": 128})]
    manager = RelayManager(
        configs,
        FakeOutput(320),
        make_output=BrokenOutput,
        station_name=lambda: "R",
        status_path=tmp_path / "missing-dir" / "relay-status.json",
        relay_factory=Broken,
    )
    manager.start()  # the status file cannot be written: only logged
    manager.close()
    assert "Stopping relay b failed" in caplog.text
    assert "Closing a relay encoder failed" in caplog.text


@pytest.mark.parametrize("content", [None, "not json", '{"relays": "x"}', "[]"])
def test_unreadable_status_file(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "relay-status.json"
    if content is not None:
        path.write_bytes(content.encode())
    assert read_status_file(path) is None


# -- review fixes: stopping, slow logins, encoder restarts ------------------------


class DribbleSocket(FakeSocket):
    """Answers one byte at a time, slowly."""

    def __init__(self, reply: bytes, pause: float) -> None:
        super().__init__(reply)
        self.pause = pause
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        super().settimeout(value)
        self.timeouts.append(value)

    def recv(self, n: int) -> bytes:
        time.sleep(self.pause)
        return super().recv(1)


def test_a_login_reply_has_one_deadline_for_the_whole_reply(monkeypatch) -> None:
    monkeypatch.setattr(relay_module, "LOGIN_TIMEOUT", 0.2)
    sock = DribbleSocket(b"HTTP/1.1 100 Continue" + b" " * 500 + b"\r\n\r\n", 0.02)
    started = time.monotonic()
    with pytest.raises(RelayError, match="took too long to answer the login"):
        open_source(_icecast(), bitrate=128, station_name="A", connect=_connector(sock, []))
    assert time.monotonic() - started < 1
    reads = sock.timeouts[1:]  # the first is the send timeout
    assert reads and all(b < a for a, b in itertools.pairwise(reads))
    assert sock.closed


def test_a_receive_timeout_during_login_is_reported(monkeypatch) -> None:
    class Silent(FakeSocket):
        def recv(self, n: int) -> bytes:
            raise TimeoutError("timed out")

    with pytest.raises(RelayError, match="took too long"):
        open_source(
            _shoutcast(), bitrate=128, station_name="A", connect=_connector(Silent(b""), [])
        )


def test_after_login_the_socket_gets_the_send_timeout() -> None:
    sock = FakeSocket(b"HTTP/1.1 100 Continue\r\n\r\n")
    open_source(_icecast(), bitrate=128, station_name="A", connect=_connector(sock, []))
    assert sock.timeout == relay_module.SEND_TIMEOUT


def test_open_source_publishes_the_socket_before_logging_in() -> None:
    sock, seen = FakeSocket(b"HTTP/1.1 100 Continue\r\n\r\n"), []

    def on_socket(s) -> None:
        seen.append((s, s.sent))

    open_source(
        _icecast(), bitrate=128, station_name="A", connect=_connector(sock, []), on_socket=on_socket
    )
    assert seen == [(sock, b"")]


def test_close_cuts_a_login_that_is_waiting_for_the_server(fast) -> None:
    cut = threading.Event()

    class Waiting(FakeSocket):
        def shutdown(self, _how: int) -> None:
            cut.set()

    def connect(cfg, *, bitrate, station_name, on_socket):
        on_socket(Waiting(b""))
        cut.wait(5)  # like a recv() that only a shutdown ends
        raise OSError(10053, "connection aborted")

    relay = _relay(connect)
    relay.start()
    _wait_until(lambda: relay._login_socket is not None)
    started = time.monotonic()
    relay.close()
    assert time.monotonic() - started < 1
    assert cut.is_set()
    assert relay.status()["state"] == "stopped"


def test_a_login_socket_published_after_close_is_cut_at_once() -> None:
    relay = _relay(Script())
    relay.begin_close()
    sock = FakeSocket(b"")
    cut: list[int] = []
    sock.shutdown = cut.append  # type: ignore[method-assign]
    relay._login_started(sock)
    assert cut and relay._login_socket is None


def test_a_relay_stuck_past_close_never_shows_live_again(fast, monkeypatch) -> None:
    monkeypatch.setattr(relay_module, "CLOSE_TIMEOUT", 0.1)
    release = threading.Event()
    connection = FakeConnection()

    def connect(cfg, *, bitrate, station_name, on_socket):
        release.wait(5)  # stuck where nothing can interrupt it, like a DNS lookup
        return connection

    titles: list[str] = []
    relay = _relay(connect, titles)
    relay.feed(FRAME, "A - B")
    relay.start()
    relay.close()
    assert relay.status()["state"] == "stopped"
    release.set()
    _wait_until(lambda: connection.closed)
    for thread in relay._threads:
        thread.join(2)
    assert relay.status() == {**relay.status(), "state": "stopped", "error": None}
    assert titles == []


def test_a_title_failing_after_close_is_not_recorded(fast) -> None:
    relay = _relay(Script())
    relay.begin_close()
    relay._send_title("A - B", relay._generation)
    assert relay.status()["title_error"] is None
    assert relay._sent_title is None


def test_manager_waits_for_all_relays_against_one_deadline(monkeypatch) -> None:
    monkeypatch.setattr(relay_module, "CLOSE_TIMEOUT", 0.3)
    release = threading.Event()

    def stuck(cfg, *, bitrate, station_name, on_socket):
        release.wait(5)
        raise OSError(11001, "host not found")

    configs = [RelayConfig.from_table(name, _table()) for name in ("a", "b", "c")]
    manager = RelayManager(
        configs,
        FakeOutput(320),
        make_output=FakeOutput,
        station_name=lambda: "R",
        relay_factory=lambda cfg, **kw: Relay(cfg, connect=stuck, **kw),
    )
    manager.start()
    started = time.monotonic()
    try:
        manager.close()
        took = time.monotonic() - started
    finally:
        release.set()
    assert took < 0.6  # one shared 0.3 s, not 0.3 s per relay
    assert [s["state"] for s in manager.status()] == ["stopped"] * 3


def test_an_encoder_restart_at_the_same_bitrate_backs_off(fast, monkeypatch) -> None:
    script = Script()
    relay = _relay(script)
    relay.start()
    try:
        for logins in (2, 3):
            _wait_until(lambda: relay.status()["state"] == "live")
            relay.encoder_changed()
            _wait_until(lambda logins=logins: len(script.calls) == logins)
        assert relay._failures == 2  # each restart counts like a failure
        monkeypatch.setattr(relay_module, "BACKOFF_START", 5.0)
        monkeypatch.setattr(relay_module, "BACKOFF_MAX", 5.0)
        _wait_until(lambda: relay.status()["state"] == "live")
        relay.encoder_changed()
        _wait_until(lambda: relay.status()["state"] == "connecting")
        time.sleep(0.1)
        assert len(script.calls) == 3  # still waiting before logging in again
        assert relay.status()["error"] is None
    finally:
        started = time.monotonic()
        relay.close()
    assert time.monotonic() - started < 1  # the wait ends at once on close
