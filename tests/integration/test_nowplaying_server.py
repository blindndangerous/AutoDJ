"""Public feed and paired webhook control routes."""

from __future__ import annotations

import threading
from unittest.mock import patch

from fastapi.testclient import TestClient

from autodj.config import ServerConfig, WebhookConfig
from autodj.server import create_app

from ._helpers import _make_entry


def test_nowplaying_is_public_and_path_free(bridge) -> None:
    bridge.player._cfg.server = ServerConfig(
        access_token="now-playing-test-access-token-32-bytes",
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
    )
    bridge.player._cfg.webhooks = (WebhookConfig("https://example.com/private-token"),)
    client = TestClient(create_app(bridge))
    response = client.get("/api/nowplaying")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.json()["current_track"]["title"] == "Song 0"
    assert "Z:/Music" not in response.text
    assert "private-token" not in response.text
    assert client.head("/api/nowplaying").status_code == 200
    assert client.post("/api/nowplaying").status_code == 405
    assert client.get("/api/webhooks").status_code == 401
    assert client.post("/api/webhooks/0/test").status_code == 401
    bridge.player._state.current_track = None
    bridge.announce_station_event("set_stopped")
    assert client.get("/api/nowplaying").json()["current_track"] is None
    bridge.player._pick_next.return_value = _make_entry(2)
    bridge.advance_now()
    fresh = client.get("/api/nowplaying").json()
    assert fresh["current_track"]["title"] == "Song 1"
    assert fresh["current_track"]["started_at"]


def test_paired_browser_can_send_redacted_webhook_test(bridge) -> None:
    bridge.player._cfg.server = ServerConfig(
        access_token="now-playing-test-access-token-32-bytes",
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
    )
    bridge.player._cfg.webhooks = (WebhookConfig("https://example.com/private-token"),)
    delivered = threading.Event()

    def send(_config, _payload):
        delivered.set()
        return True, None

    with (
        patch("autodj.webhooks._post", side_effect=send),
        TestClient(create_app(bridge)) as client,
    ):
        code = client.app.state.request_pairing_code()[0]
        assert (
            client.post("/api/pair", json={"code": code, "device_name": "Tester"}).status_code
            == 200
        )
        assert client.get("/api/webhooks").json()[0]["host"] == "example.com"
        assert "private-token" not in client.get("/api/webhooks").text
        assert client.post("/api/webhooks/0/test").json() == {"delivered": True}
        assert client.post("/api/webhooks/1/test").status_code == 404
        delivered.clear()
        bridge.on_track_started(bridge.player._state.current_track)
        assert delivered.wait(2)
        assert client.get("/api/nowplaying").json()["current_track"]["started_at"]


def test_status_page_is_public_plain_html_without_private_details(bridge) -> None:
    bridge.player._cfg.server = ServerConfig(
        access_token="now-playing-test-access-token-32-bytes",
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
    )
    bridge.player._cfg.webhooks = (WebhookConfig("https://example.com/private-token"),)
    client = TestClient(create_app(bridge))
    response = client.get("/status")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert '<html lang="en">' in response.text
    assert "Song 0 by" in response.text
    assert "Z:/Music" not in response.text
    assert "private-token" not in response.text
    assert "example.com" not in response.text
    assert client.head("/status").status_code == 200
    assert client.post("/status").status_code == 405
    # Its script and stylesheet load without pairing too; the app does not.
    assert client.get("/modules/status-page.js").status_code == 200
    assert client.get("/modules/status-page.css").headers["content-type"].startswith("text/css")
    assert client.get("/api/status").status_code == 401
    feed = client.get("/api/nowplaying").json()
    assert feed["schema"] == 3
    assert feed["peak_listeners_today"] >= feed["listener_count"]
    assert feed["uptime_seconds"] >= 0
