"""Settings, Updates: the update check, its settings and Restart over HTTP (updates_api)."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from autodj import updates_api
from autodj.server import PlayerBridge
from autodj.updates import Installation, RestartSupport, UpdateCheck
from tests._layout import point_index
from tests._server import create_app

from ._helpers import _make_player_mock, _make_sim_mock

_TOKEN = "updates-routes-token-is-at-least-32-bytes"
_SUPPORTED = RestartSupport("launcher", "The AutoDJ launcher starts AutoDJ again.")


def _client(tmp_path: Path, *, token: str | None = None) -> TestClient:
    player = _make_player_mock()
    cfg = player._cfg
    point_index(cfg.index, tmp_path)
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    if token is not None:
        cfg.server.access_token = token
    client = TestClient(create_app(PlayerBridge(player=player, sim=_make_sim_mock())))
    checker = client.app.state.update_checker
    checker._installation = lambda: Installation(kind="package", version="0.21.0")
    return client


def _audits(caplog: pytest.LogCaptureFixture, action: str) -> list[dict]:
    return [
        entry
        for entry in (
            json.loads(record.getMessage())
            for record in caplog.records
            if record.name == "autodj.audit"
        )
        if entry["action"] == action
    ]


@pytest.fixture
def supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updates_api, "restart_support", lambda: _SUPPORTED)
    monkeypatch.setattr(updates_api, "EXIT_DELAY_SECONDS", 0.0)


def test_the_view_names_version_channel_and_restart(tmp_path: Path) -> None:
    """ADR-0016-05: without a server to stop, Restart is not offered."""
    client = _client(tmp_path)
    view = client.get("/api/updates").json()
    assert view["installation"]["version"] == "0.21.0"
    assert view["channel"] == {"name": "stable", "source": "installation", "inferred": "stable"}
    assert view["settings"] == {"check": True, "channel": None}
    assert view["restart"]["available"] is False
    # The test process turns the network check off (tests/conftest.py).
    assert view["check"]["status"] == "off"


def test_settings_are_saved_and_validated(tmp_path: Path) -> None:
    """ADR-0016-02."""
    client = _client(tmp_path)
    response = client.put("/api/updates/settings", json={"check": False, "channel": "development"})
    assert response.status_code == 200
    assert response.json()["settings"] == {"check": False, "channel": "development"}
    assert response.json()["channel"]["source"] == "setting"
    saved = json.loads((tmp_path / "shared" / "updates.json").read_text(encoding="utf-8"))
    assert saved == {"check": False, "channel": "development"}
    assert response.json()["check"] is None
    refused = client.put("/api/updates/settings", json={"check": True, "channel": "nightly"})
    assert refused.status_code == 422


def test_check_now_is_audited_and_rate_limited(tmp_path: Path, caplog) -> None:
    """ADR-0016-06, ADR-0016-09: the answer carries the commands for this installation."""
    client = _client(tmp_path)
    found = UpdateCheck(
        status="available",
        channel="stable",
        channel_source="installation",
        message="Release v0.22.0 is available.",
        latest="v0.22.0",
        target="v0.22.0",
        checked_at=time.time(),
    )
    check = Mock(return_value=found)
    client.app.state.update_checker._check = check
    with caplog.at_level(logging.INFO, logger="autodj.audit"):
        first = client.post("/api/updates/check", json={})
        second = client.post("/api/updates/check", json={})
    assert first.status_code == 200
    assert first.json()["check"]["target"] == "v0.22.0"
    assert first.json()["commands"] == [
        'uv pip install "autodj[all] @ https://github.com/blindndangerous/AutoDJ/releases/'
        'download/v0.22.0/autodj-0.22.0-py3-none-any.whl"'
    ]
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) >= 1
    assert check.call_count == 1
    outcomes = [entry["outcome"] for entry in _audits(caplog, "update-check")]
    assert outcomes == ["success", "rejected"]


@pytest.mark.usefixtures("supported")
def test_restart_stops_the_server_and_is_audited(tmp_path: Path, caplog) -> None:
    """ADR-0016-04, ADR-0016-06."""
    client = _client(tmp_path)
    server_exit = Mock()
    client.app.state.server_exit = server_exit
    with caplog.at_level(logging.INFO, logger="autodj.audit"), client:
        response = client.post("/api/server/restart", json={})
        assert response.status_code == 202
        deadline = time.monotonic() + 5
        while not server_exit.called and time.monotonic() < deadline:
            time.sleep(0.01)
    server_exit.assert_called_once_with()
    assert client.app.state.restart_requested is True
    assert [entry["outcome"] for entry in _audits(caplog, "server-restart")] == ["accepted"]


def test_restart_is_refused_when_nothing_would_start_autodj_again(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """ADR-0016-04: no button and no restart without a supervisor."""
    monkeypatch.setattr(
        updates_api, "restart_support", lambda: RestartSupport(None, "Restart it yourself.")
    )
    client = _client(tmp_path)
    server_exit = Mock()
    client.app.state.server_exit = server_exit
    assert client.get("/api/updates").json()["restart"] == {
        "available": False,
        "mode": None,
        "message": "Restart it yourself.",
    }
    with caplog.at_level(logging.INFO, logger="autodj.audit"):
        response = client.post("/api/server/restart", json={})
    assert response.status_code == 409
    assert response.json()["detail"] == "Restart it yourself."
    server_exit.assert_not_called()
    assert [entry["outcome"] for entry in _audits(caplog, "server-restart")] == ["rejected"]


@pytest.mark.usefixtures("supported")
def test_restart_is_rate_limited_across_a_restart(tmp_path: Path) -> None:
    """ADR-0016-06: the count lives in the index folder, not the process."""
    client = _client(tmp_path)
    client.app.state.server_exit = Mock()
    assert client.post("/api/server/restart", json={}).status_code == 202
    again = _client(tmp_path)
    again.app.state.server_exit = Mock()
    refused = again.post("/api/server/restart", json={})
    assert refused.status_code == 429
    assert 1 <= int(refused.headers["Retry-After"]) <= 60
    again.app.state.server_exit.assert_not_called()


def test_the_restart_limiter_allows_six_an_hour(tmp_path: Path) -> None:
    """ADR-0016-06."""
    now = [10_000.0]
    limiter = updates_api.RestartLimiter(lambda: tmp_path / "times.json", clock=lambda: now[0])
    for _ in range(updates_api.RESTART_MAX_PER_HOUR):
        assert limiter.take() is None
        now[0] += updates_api.RESTART_MIN_INTERVAL_SECONDS
    wait = limiter.take()
    assert wait is not None and wait > updates_api.RESTART_MIN_INTERVAL_SECONDS
    now[0] += 3600
    assert limiter.take() is None


@pytest.mark.usefixtures("supported")
def test_only_a_paired_browser_on_this_site_reaches_the_update_routes(tmp_path: Path) -> None:
    """ADR-0016-06: session, Origin, and no machine token."""
    client = _client(tmp_path, token=_TOKEN)
    server_exit = Mock()
    client.app.state.server_exit = server_exit
    routes = [
        ("get", "/api/updates", None),
        ("post", "/api/updates/check", {}),
        ("put", "/api/updates/settings", {"check": True, "channel": "auto"}),
        ("post", "/api/server/restart", {}),
    ]
    for method, url, body in routes:
        kwargs = {"json": body} if body is not None else {}
        assert getattr(client, method)(url, **kwargs).status_code == 401
        bearer = getattr(client, method)(
            url, headers={"Authorization": f"Bearer {_TOKEN}"}, **kwargs
        )
        assert bearer.status_code == 401
    code = client.app.state.request_pairing_code()[0]
    assert client.post("/api/pair", json={"code": code, "device_name": "Desk"}).status_code == 200
    foreign = client.post(
        "/api/server/restart", json={}, headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403
    server_exit.assert_not_called()
    assert client.get("/api/updates").status_code == 200
