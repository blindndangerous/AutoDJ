"""The player page and embeddable player over HTTP: off by default, public, no secrets (#52)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from autodj.config import PlayerPageConfig, ServerConfig, StreamConfig, WebhookConfig
from autodj.server import create_app
from tests._layout import point_index

_TOKEN = "player-page-test-access-token-32-bytes"


def _client(bridge, *, token: bool = True, **settings: object) -> TestClient:
    bridge.player._cfg.server = ServerConfig(
        access_token=_TOKEN if token else None,
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
    )
    bridge.player._cfg.player_page = PlayerPageConfig(**settings)  # type: ignore[arg-type]
    bridge.player._cfg.webhooks = (WebhookConfig("https://example.com/private-token"),)
    return TestClient(create_app(bridge), client=("192.0.2.4", 50000))


def _streaming(bridge, tmp_path: Path) -> None:
    point_index(bridge.player._cfg.index, tmp_path)
    bridge.player._cfg.stream = StreamConfig(
        enabled=True, listener_user="owl", listener_password="listener-secret-pw"
    )
    bridge.attach_stream(stream=MagicMock(listener_count=0), station=MagicMock(state="idle"))


@pytest.mark.parametrize("path", ["/player", "/embed", "/stations/default/player"])
def test_both_pages_are_off_until_turned_on(bridge, path: str) -> None:
    client = _client(bridge)
    # Public, so not 401, but not there either.
    assert client.get(path).status_code == 404
    assert client.head(path).status_code == 404


def test_the_player_page_is_public_strict_and_never_framed(bridge, tmp_path: Path) -> None:
    _streaming(bridge, tmp_path)
    client = _client(bridge, enabled=True)
    response = client.get("/player")
    assert response.status_code == 200, response.text
    headers = response.headers
    assert headers["content-type"].startswith("text/html")
    assert headers["cache-control"] == "no-cache"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-frame-options"] == "DENY"
    policy = headers["content-security-policy"]
    assert "frame-ancestors 'none'" in policy
    assert "media-src 'self'" in policy
    assert "default-src 'none'" in policy
    html = response.text
    assert "<title>AutoDJ: listen live</title>" in html
    assert 'data-feed="/api/nowplaying"' in html
    assert 'data-stream="/stream.mp3"' in html
    assert "Song 0 by" in html
    # Nothing private: no credentials, tokens, webhook hosts or file paths.
    for secret in (_TOKEN, "listener-secret-pw", "private-token", "example.com", "Z:/Music"):
        assert secret not in html
    assert str(tmp_path) not in html and tmp_path.as_posix() not in html
    assert client.head("/player").status_code == 200
    assert client.post("/player").status_code == 405
    # Its script and stylesheet are public; the controls are not.
    assert client.get("/modules/player-page.js").status_code == 200
    assert client.get("/modules/player-page.css").headers["content-type"].startswith("text/css")
    assert client.get("/api/status").status_code == 401
    # The embed stays off until it is turned on too.
    assert client.get("/embed").status_code == 404


def test_the_embed_is_framed_only_by_the_listed_sites(bridge, tmp_path: Path) -> None:
    _streaming(bridge, tmp_path)
    client = _client(bridge, embed=True, embed_origins=["https://blog.example.org"])
    response = client.get("/embed?theme=dark")
    assert response.status_code == 200, response.text
    policy = response.headers["content-security-policy"]
    assert policy.endswith("frame-ancestors 'self' https://blog.example.org")
    assert "x-frame-options" not in response.headers
    assert '<html lang="en" data-theme="dark">' in response.text
    # The full player is off, so the embed does not link to it.
    assert "<a " not in response.text
    assert 'data-theme="' not in client.get("/embed?theme=<b>").text
    anywhere = _client(bridge, embed=True, embed_origins=["*"])
    assert anywhere.get("/embed").headers["content-security-policy"].endswith("frame-ancestors *")
    only_here = _client(bridge, embed=True)
    assert (
        only_here.get("/embed")
        .headers["content-security-policy"]
        .endswith("frame-ancestors 'self'")
    )


def test_a_station_that_does_not_stream_says_so(bridge) -> None:
    client = _client(bridge, enabled=True, embed=True)
    for path in ("/player", "/embed"):
        html = client.get(path).text
        assert "AutoDJ is not streaming right now, so it cannot be played here." in html
        assert "<audio" not in html


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/stations/default/player", 200),
        ("/stations/default/embed", 200),
        ("/stations/nope/player", 401),
        ("/stations/nope/embed", 401),
        ("/stations/default/player/x", 401),
        ("/stations//player", 401),
        ("/player/x", 401),
    ],
)
def test_station_player_pages_are_public_only_for_a_configured_station(
    bridge, path: str, status: int
) -> None:
    client = _client(bridge, enabled=True, embed=True)
    assert client.get(path).status_code == status


def test_an_open_server_answers_404_for_an_unknown_station(bridge) -> None:
    client = _client(bridge, token=False, enabled=True)
    assert client.get("/stations/nope/player").status_code == 404
    page = client.get("/stations/default/player").text
    assert 'data-feed="/api/stations/default/nowplaying"' in page
    assert '<a href="/stations/default/status">Station status</a>' in page
