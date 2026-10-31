"""Lease and raw-transport tests; no public on-air activation in this slice."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from autodj.config import ServerConfig
from autodj.live import LiveDesk
from autodj.local_programme import LocalProgramme, OwnershipError
from autodj.local_programme_routes import RawPeerMiddleware, locality_allowed
from autodj.security import SecurityPolicy


@pytest.fixture
def controller(tmp_path):
    return LocalProgramme(LiveDesk(tmp_path / "djs.json"))


def test_claim_race_and_stale_grants(controller):
    """ADR-0019-16: one socket wins, including tabs sharing a session."""

    def claim(i):
        try:
            return controller.claim("same-session", str(i), 0)
        except OwnershipError:
            return None

    with ThreadPoolExecutor(8) as pool:
        grants = [g for g in pool.map(claim, range(8)) if g is not None]
    assert len(grants) == 1
    first = grants[0]
    controller.renew(first, 1)
    with pytest.raises(OwnershipError):
        controller.renew(replace(first, capability="wrong"), 1)
    controller.revoke(first.generation, "test")
    second = controller.claim("same-session", "new-socket", 1)
    assert second.generation > first.generation
    assert second.capability != first.capability
    with pytest.raises(OwnershipError):
        controller.renew(first, 1)
    controller.revoke(first.generation, "stale disconnect")
    assert controller.status()["state"] == "preparing"


def test_deadlines_stop_and_preparation_cap(controller):
    """ADR-0019-16: lease, absolute preparation deadline and stop fence claims."""
    first = controller.claim("anonymous", "one", 0)
    assert controller.check(2.99)
    assert not controller.check(3)
    with pytest.raises(OwnershipError):
        controller.renew(first, 3)
    grant = controller.claim("anonymous", "two", 4)
    for now in range(5, 14):
        controller.renew(grant, now)
    assert not controller.check(14)
    controller.close()
    with pytest.raises(OwnershipError):
        controller.claim("anonymous", "three", 15)


def test_external_input_exclusion(controller):
    """ADR-0019-16: local preparation excludes microphone and decoder claims."""
    grant = controller.claim("user", "socket", 0)
    assert controller.live.claim_mic() is None
    assert controller.live.claim("user", "DJ", "mp3") is None
    controller.revoke(grant.generation, "release")
    mic = controller.live.claim_mic()
    assert mic is not None
    with pytest.raises(OwnershipError):
        controller.claim("user", "socket", 1)
    controller.live.release_mic(mic)
    # A source being constructed and a source still fading out both exclude claims.
    for name in ("_claiming", "return_source"):
        setattr(controller.live, name, True)
        with pytest.raises(OwnershipError):
            controller.claim("user", "socket", 1)
        setattr(controller.live, name, None)


def scope(**changes):
    result = {
        "type": "websocket",
        "client": ("127.0.0.1", 123),
        "server": ("127.0.0.1", 8080),
        "scheme": "ws",
        "path": "/",
        "headers": [(b"host", b"127.0.0.1:8080"), (b"origin", b"http://127.0.0.1:8080")],
    }
    result.update(changes)
    return result


def captured(value, **kwargs):
    results = []

    async def app(s, receive, send):
        results.append(s)

    asyncio.run(RawPeerMiddleware(app, **kwargs)(value, None, None))
    return results[0]


def test_locality_requires_raw_loopback_same_origin_and_no_proxy():
    """ADR-0019-02: no Host, Origin or forwarding header establishes locality."""
    policy = SecurityPolicy(ServerConfig(local_programme_enabled=True))
    assert not locality_allowed(scope(), policy)
    assert locality_allowed(captured(scope()), policy)
    assert locality_allowed(captured(scope(client=("::ffff:127.0.0.1", 1))), policy)
    assert not locality_allowed(captured(scope(client=("192.0.2.1", 1))), policy)
    for header in (b"forwarded", b"x-forwarded-for", b"x-real-ip", b"x-autodj-user-id"):
        s = scope()
        s["headers"].append((header, b"127.0.0.1"))
        assert not locality_allowed(captured(s), policy)
    for origin in (b"null", b"https://127.0.0.1:8080", b"http://127.0.0.1:8081"):
        s = scope()
        s["headers"][1] = (b"origin", origin)
        assert not locality_allowed(captured(s), policy)
    assert not locality_allowed(captured(scope(), proxy_configured=True), policy)
    policy.config.trusted_proxies = ["127.0.0.1"]
    assert not locality_allowed(captured(scope()), policy)


@pytest.mark.parametrize("trusted", ["127.0.0.1", "192.0.2.1"])
@pytest.mark.parametrize("kind,scheme", [("http", "http"), ("websocket", "ws")])
def test_raw_capture_preserves_ordinary_proxy_semantics(trusted, kind, scheme):
    """ADR-0019-02: ordinary routes retain Uvicorn client/scheme rewriting."""
    observations = []

    async def app(s, receive, send):
        observations.append((s["client"], s["scheme"]))

    def request():
        s = scope(type=kind, scheme=scheme)
        s["headers"] += [(b"x-forwarded-for", b"198.51.100.2"), (b"x-forwarded-proto", b"https")]
        return s

    proxy = ProxyHeadersMiddleware(app, trusted_hosts=trusted)
    asyncio.run(proxy(request(), None, None))
    asyncio.run(RawPeerMiddleware(proxy)(request(), None, None))
    assert observations[0] == observations[1]
    expected = (("198.51.100.2", 0), "https" if kind == "http" else "wss")
    assert observations[0] == (expected if trusted == "127.0.0.1" else (("127.0.0.1", 123), scheme))


def test_opt_in_is_strict_and_off_by_default():
    """ADR-0019-12: the server setting is strictly boolean and defaults off."""
    assert ServerConfig().local_programme_enabled is False
    with pytest.raises(TypeError):
        ServerConfig(local_programme_enabled="true")
