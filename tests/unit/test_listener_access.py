"""Listener admission, uvicorn proxy resolution and durable bans."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from autodj.listener_access import (
    AddressLimitError,
    BannedAddressError,
    ListenerAccess,
    ListenerAuthLimiter,
    normalize_address,
    player_name,
    proxy_allowlist,
)


class FakeListener:
    closed = False

    def abort(self) -> None:
        self.closed = True


class FakeStream:
    def __init__(self) -> None:
        self.open: list[FakeListener] = []

    def add_listener(self, *, icy: bool) -> FakeListener:
        listener = FakeListener()
        self.open.append(listener)
        return listener

    def remove_listener(self, listener: FakeListener) -> None:
        listener.closed = True
        self.open.remove(listener)


@pytest.mark.parametrize(
    ("peer", "headers", "trusted", "expected"),
    [
        ("198.51.100.5", [(b"x-forwarded-for", b"192.0.2.4")], ["198.51.100.0/24"], "192.0.2.4"),
        ("198.51.100.5", [(b"x-forwarded-for", b"192.0.2.4")], [], "198.51.100.5"),
        (
            "203.0.113.5",
            [(b"x-forwarded-for", b"192.0.2.4")],
            ["198.51.100.0/24"],
            "203.0.113.5",
        ),
        (
            "198.51.100.5",
            [(b"x-forwarded-for", b"garbage, 203.0.113.8, 198.51.100.6")],
            ["198.51.100.0/24"],
            "203.0.113.8",
        ),
        (
            "198.51.100.5",
            [(b"x-forwarded-for", b"garbage"), (b"x-forwarded-for", b"203.0.113.8, 198.51.100.6")],
            ["198.51.100.0/24"],
            "203.0.113.8",
        ),
        (
            "198.51.100.5",
            [(b"x-forwarded-for", b"203.0.113.8:4567")],
            ["198.51.100.0/24"],
            "203.0.113.8",
        ),
        (
            "::ffff:198.51.100.5",
            [(b"x-forwarded-for", b"::ffff:192.0.2.4")],
            ["198.51.100.0/24"],
            "192.0.2.4",
        ),
        ("198.51.100.5", [(b"forwarded", b"for=192.0.2.4")], ["198.51.100.0/24"], "198.51.100.5"),
        ("198.51.100.5", [(b"forwarded", b"by=x")], ["198.51.100.0/24"], "198.51.100.5"),
        ("198.51.100.5", [(b"forwarded", b"for=unknown")], ["198.51.100.0/24"], "198.51.100.5"),
        ("198.51.100.5", [(b"forwarded", b"for=_hidden")], ["198.51.100.0/24"], "198.51.100.5"),
    ],
)
def test_uvicorn_client_address(
    peer: str, headers: list[tuple[bytes, bytes]], trusted: list[str], expected: str
) -> None:
    observed = []

    async def app(scope, _receive, _send):
        observed.append(scope["client"][0])

    async def receive():
        return {"type": "http.disconnect"}

    async def send(_message):
        return None

    scope = {"type": "http", "client": (peer, 1234), "headers": headers, "scheme": "http"}
    middleware = ProxyHeadersMiddleware(app, trusted_hosts=proxy_allowlist(trusted))
    asyncio.run(middleware(scope, receive, send))
    assert normalize_address(observed[0]) == expected


def test_player_name_is_short_and_printable() -> None:
    assert player_name("VLC\r\nSecret") == "VLC Secret"
    assert len(player_name("x" * 200)) == 80
    assert player_name("") == "Unknown player"


def test_address_limit_kick_and_ban_survive_restart(tmp_path: Path) -> None:
    now = [1000.0]
    path = tmp_path / "listener_bans.json"
    access = ListenerAccess(path, 2, clock=lambda: now[0])
    stream = FakeStream()
    first, listener = access.add(stream, "192.0.2.4", "VLC", icy=False)
    second, _ = access.add(stream, "192.0.2.4", "Winamp", icy=True)
    with pytest.raises(AddressLimitError):
        access.add(stream, "192.0.2.4", "Third", icy=False)
    assert access.kick(first) == "192.0.2.4"
    assert listener.closed
    assert access.kick(first) is None
    assert [item["id"] for item in access.listeners()] == [second]

    access.ban("192.0.2.0/24", 60, "temporary")
    assert not stream.open
    with pytest.raises(BannedAddressError):
        access.add(stream, "192.0.2.4", "VLC", icy=False)
    reloaded = ListenerAccess(path, 2, clock=lambda: now[0])
    assert reloaded.banned("192.0.2.5")
    assert reloaded.bans()[0]["note"] == "temporary"
    now[0] += 3601
    assert not reloaded.banned("192.0.2.5")
    previous_revision = reloaded.revision
    reloaded.prune_expired()
    assert reloaded.revision == previous_revision + 1
    assert json.loads(path.read_text(encoding="utf-8")) == {"bans": []}


def test_invalid_bans_file_is_quarantined(tmp_path: Path, caplog) -> None:
    path = tmp_path / "listener_bans.json"
    path.write_text('{"bans": [{"address": "bad", "expires_at": null, "note": ""}]}')
    access = ListenerAccess(path, 3)
    assert access.bans() == []
    assert not path.exists()
    assert len(list(tmp_path.glob("listener_bans.json.*.bad"))) == 1
    assert "starting with no bans" in caplog.text


def test_auth_limiter_resets_after_one_minute() -> None:
    now = [0.0]
    limiter = ListenerAuthLimiter(clock=lambda: now[0])
    for _ in range(5):
        limiter.failed("192.0.2.4")
    assert limiter.blocked("192.0.2.4")
    assert not limiter.blocked("192.0.2.5")
    now[0] = 60.0
    assert not limiter.blocked("192.0.2.4")


def test_mapped_ipv6_shares_ipv4_limit_and_ban(tmp_path: Path) -> None:
    assert proxy_allowlist(["::ffff:198.51.100.0/120"]) == proxy_allowlist(["198.51.100.0/24"])
    access = ListenerAccess(tmp_path / "bans.json", 1)
    stream = FakeStream()
    access.add(stream, "::ffff:192.0.2.4", "VLC", icy=False)
    assert access.listeners()[0]["address"] == "192.0.2.4"
    with pytest.raises(AddressLimitError):
        access.add(stream, "192.0.2.4", "VLC", icy=False)
    access.ban("::ffff:192.0.2.0/120", None)
    assert access.banned("192.0.2.4")
    assert access.bans()[0]["address"] == "192.0.2.0/24"


def test_pending_reservation_does_not_hold_registry_lock(tmp_path: Path) -> None:
    access = ListenerAccess(tmp_path / "bans.json", 1)
    started = threading.Event()
    release = threading.Event()

    class SlowStream(FakeStream):
        def add_listener(self, *, icy: bool) -> FakeListener:
            started.set()
            assert release.wait(5)
            return super().add_listener(icy=icy)

    stream = SlowStream()
    worker = threading.Thread(target=lambda: access.add(stream, "192.0.2.4", "VLC", icy=False))
    worker.start()
    assert started.wait(2)
    try:
        before = time.monotonic()
        assert not access.banned("192.0.2.4")
        assert time.monotonic() - before < 1
        with pytest.raises(AddressLimitError):
            access.add(stream, "192.0.2.4", "VLC", icy=False)
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
