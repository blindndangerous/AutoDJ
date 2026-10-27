"""Settings, Updates: the update check, its settings and Restart over HTTP (updates_api)."""

from __future__ import annotations

import json
import logging
import time
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from autodj import update_contract as contract
from autodj import updates, updates_api
from autodj.server import PlayerBridge
from autodj.update_contract import HealthProbe
from autodj.updates import Installation, RestartSupport, UpdateCheck, UpdateSupport
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
    """ADR-0016-06, ADR-0016-09: the page gets no update commands; check-update keeps them."""
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
    # The page lists no commands; a package is told to update the way it was installed.
    assert "commands" not in first.json()
    assert first.json()["update"]["available"] is False
    assert first.json()["update"]["message"] == "Update it the way you installed it."
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
    """ADR-0016-06, ADR-0016-18: session, Origin, and no machine token."""
    client = _client(tmp_path, token=_TOKEN)
    server_exit = Mock()
    client.app.state.server_exit = server_exit
    routes = [
        ("get", "/api/updates", None),
        ("post", "/api/updates/check", {}),
        ("put", "/api/updates/settings", {"check": True, "channel": "auto"}),
        ("post", "/api/server/restart", {}),
        ("post", "/api/server/update", {}),
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


@pytest.mark.usefixtures("supported")
def test_restart_is_refused_when_its_count_cannot_be_saved(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """ADR-0016-06: the limit fails closed."""
    import autodj.fsutil

    def unwritable(*_args, **_kwargs) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(autodj.fsutil, "atomic_write", unwritable)
    client = _client(tmp_path)
    server_exit = Mock()
    client.app.state.server_exit = server_exit
    with caplog.at_level(logging.INFO, logger="autodj.audit"):
        response = client.post("/api/server/restart", json={})
    assert response.status_code == 503
    assert "did not restart" in response.json()["detail"]
    server_exit.assert_not_called()
    assert [entry["outcome"] for entry in _audits(caplog, "server-restart")] == ["failed"]


def test_a_restart_time_ahead_of_the_clock_still_counts(tmp_path: Path) -> None:
    """ADR-0016-06: setting the clock back does not clear the limit."""
    path = tmp_path / "times.json"
    path.write_text(json.dumps([10_000.0 + 86_400]), encoding="utf-8")
    limiter = updates_api.RestartLimiter(lambda: path, clock=lambda: 10_000.0)
    wait = limiter.take()
    assert wait == int(updates_api.RESTART_MIN_INTERVAL_SECONDS)


def test_page_loads_during_a_slow_check_share_it(tmp_path: Path) -> None:
    """ADR-0016-03: one check in flight; other loads wait for it without starting more."""
    import threading

    client = _client(tmp_path)
    release = threading.Event()
    calls = []

    def slow(installation, settings) -> UpdateCheck:
        calls.append(1)
        release.wait(5)
        return UpdateCheck(
            status="current", channel="stable", channel_source="installation", message="ok"
        )

    client.app.state.update_checker._check = slow
    answers = []
    threads = [
        threading.Thread(target=lambda: answers.append(client.get("/api/updates")))
        for _ in range(4)
    ]
    with client:  # one event loop for every request, as in the server
        for thread in threads:
            thread.start()
        time.sleep(0.5)
        release.set()
        for thread in threads:
            thread.join(10)
    assert [answer.status_code for answer in answers] == [200] * 4
    assert len(calls) == 1


def test_shutting_down_stops_a_running_library_job(tmp_path: Path, monkeypatch) -> None:
    """ADR-0016-10: a restart does not leave an index or analysis job running."""
    import autodj.jobs

    manager = Mock()
    manager.shutdown.return_value = True
    monkeypatch.setattr(autodj.jobs, "get_manager", lambda: manager)
    client = _client(tmp_path)
    with client:
        manager.shutdown.assert_not_called()
    manager.shutdown.assert_called_once()


@pytest.mark.usefixtures("supported")
@pytest.mark.parametrize("content", ["not json", '{"times": []}', '["soon"]'])
def test_an_unreadable_restart_count_refuses_and_names_the_file(
    tmp_path: Path, content: str
) -> None:
    """ADR-0016-06: a damaged count fails closed instead of resetting the limit."""
    count = tmp_path / "logs" / "restart-requests.json"
    count.parent.mkdir(parents=True, exist_ok=True)
    count.write_text(content, encoding="utf-8")
    client = _client(tmp_path)
    server_exit = Mock()
    client.app.state.server_exit = server_exit
    response = client.post("/api/server/restart", json={})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert str(count) in detail
    assert "Delete that file" in detail
    server_exit.assert_not_called()
    assert count.read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

_OLD = "1" * 40
_LAUNCHER = UpdateSupport("launcher", "The AutoDJ launcher updates this checkout.", "guide")
_PROBE = HealthProbe("http", "127.0.0.1", 8080, "127.0.0.1:8080")


def _available() -> UpdateCheck:
    return UpdateCheck(
        status="available",
        channel="stable",
        channel_source="installation",
        message="Release v0.22.0 is available.",
        latest="v0.22.0",
        target="v0.22.0",
        checked_at=time.time(),
    )


def _updatable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: UpdateCheck | None = None
) -> TestClient:
    """A checkout under the updating launcher, served by a server that can stop."""
    monkeypatch.setattr(updates_api, "update_support", lambda _installation: _LAUNCHER)
    monkeypatch.setattr(updates_api, "EXIT_DELAY_SECONDS", 0.0)
    client = _client(tmp_path)
    checker = client.app.state.update_checker
    checker._installation = lambda: Installation(
        kind="git", version="0.21.0", commit=_OLD, tag="v0.21.0", root=tmp_path / "checkout"
    )
    checker._check = Mock(return_value=result or _available())
    client.app.state.server_exit = Mock()
    client.app.state.health_probe = _PROBE
    client.app.state.update_dir = tmp_path / "checkout" / ".uv" / "update"
    return client


def test_update_hands_the_launcher_a_request_and_exits_with_the_update_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """ADR-0016-18: audited, carries nothing from the request, and only one is pending."""
    client = _updatable(tmp_path, monkeypatch)
    view = client.get("/api/updates").json()
    assert view["update"] == {
        "available": True,
        "mechanism": "launcher",
        "message": "The AutoDJ launcher updates this checkout.",
        "guide_url": "guide",
    }
    with caplog.at_level(logging.INFO, logger="autodj.audit"), client:
        response = client.post("/api/server/update", json={})
        assert response.status_code == 202
        deadline = time.monotonic() + 5
        while not client.app.state.server_exit.called and time.monotonic() < deadline:
            time.sleep(0.01)
        again = client.post("/api/server/update", json={})
        restart = client.post("/api/server/restart", json={})
    client.app.state.server_exit.assert_called_once_with()
    assert client.app.state.exit_status == updates.UPDATE_EXIT_CODE
    body = response.json()
    assert body["updating"] is True
    assert body["target"] == "v0.22.0"
    request = contract.read_request(client.app.state.update_dir)
    assert request == contract.Request(
        body["id"], "update", "stable", pytest.approx(time.time(), abs=60), _PROBE
    )
    assert again.status_code == 409
    assert again.json()["detail"] == "AutoDJ is already restarting or updating."
    assert restart.status_code == 409
    assert [entry["outcome"] for entry in _audits(caplog, "server-update")] == [
        "accepted",
        "rejected",
    ]


def test_update_takes_no_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0016-18: the target is the server's own check, never the request body."""
    client = _updatable(tmp_path, monkeypatch)
    refused = client.post("/api/server/update", json={"target": "v9.9.9"})
    assert refused.status_code == 422
    client.app.state.server_exit.assert_not_called()


def test_update_is_refused_when_this_installation_cannot_update(tmp_path: Path, caplog) -> None:
    """ADR-0016-11: a package (or a container, for now) updates the way it was installed."""
    client = _client(tmp_path)
    client.app.state.server_exit = Mock()
    client.app.state.health_probe = _PROBE
    view = client.get("/api/updates").json()
    assert view["update"]["available"] is False
    assert view["update"]["guide_url"].endswith("#update-a-wheel-installation")
    with caplog.at_level(logging.INFO, logger="autodj.audit"):
        response = client.post("/api/server/update", json={})
    assert response.status_code == 409
    assert response.json()["detail"] == "Update it the way you installed it."
    client.app.state.server_exit.assert_not_called()
    assert [entry["outcome"] for entry in _audits(caplog, "server-update")] == ["rejected"]


def test_update_is_refused_without_an_update_or_a_server_that_can_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0016-18."""
    current = UpdateCheck("current", "stable", "installation", "This is the latest release.")
    client = _updatable(tmp_path, monkeypatch, current)
    response = client.post("/api/server/update", json={})
    assert response.status_code == 409
    assert response.json()["detail"] == "No update is available. This is the latest release."
    stopless = _updatable(tmp_path / "other", monkeypatch)
    stopless.app.state.server_exit = None
    assert stopless.get("/api/updates").json()["update"]["available"] is False
    assert stopless.post("/api/server/update", json={}).status_code == 409


def test_update_is_rate_limited_in_its_own_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0016-18: one a minute across a restart, and refused when the count cannot be read."""
    client = _updatable(tmp_path, monkeypatch)
    assert client.post("/api/server/update", json={}).status_code == 202
    assert (tmp_path / "logs" / "update-requests.json").is_file()
    assert not (tmp_path / "logs" / "restart-requests.json").exists()
    again = _updatable(tmp_path, monkeypatch)
    refused = again.post("/api/server/update", json={})
    assert refused.status_code == 429
    assert refused.json()["detail"].startswith("AutoDJ updated a moment ago.")
    again.app.state.server_exit.assert_not_called()
    (tmp_path / "logs" / "update-requests.json").write_text("not json", encoding="utf-8")
    third = _updatable(tmp_path, monkeypatch)
    damaged = third.post("/api/server/update", json={})
    assert damaged.status_code == 503
    assert "so it did not update" in damaged.json()["detail"]


def test_the_page_gets_the_last_update_result_for_a_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0016-12: only the id, action, outcome, sentence and recovery commands."""
    client = _updatable(tmp_path, monkeypatch)
    folder = client.app.state.update_dir
    result = {
        "id": "abcdef12",
        "action": "update",
        "outcome": "rolled-back",
        "finished_at": time.time(),
        "message": "The update failed, so AutoDJ went back to 0.21.0.",
        "commands": ["git switch --detach v0.22.0"],
        "steps": contract.new_steps(),
        "backup": "/somewhere/private.zip",
    }
    contract.write(folder / contract.RESULT, result)
    last = client.get("/api/updates").json()["last_update"]
    assert last == {
        "id": "abcdef12",
        "action": "update",
        "outcome": "rolled-back",
        "message": "The update failed, so AutoDJ went back to 0.21.0.",
        "commands": ["git switch --detach v0.22.0"],
        "finished_at": pytest.approx(time.time(), abs=60),
    }
    contract.write(folder / contract.RESULT, {**result, "finished_at": 1.0})
    assert client.get("/api/updates").json()["last_update"] is None


def test_without_an_updater_the_update_folder_is_in_the_index_folder(tmp_path: Path) -> None:
    """ADR-0016-12: where a container's helper would read and write the same files."""
    client = _client(tmp_path)
    contract.write(
        tmp_path / "updates" / contract.RESULT,
        {
            "id": "abcdef12",
            "action": "update",
            "outcome": "updated",
            "finished_at": time.time(),
            "message": "Updated to 0.22.0.",
            "commands": [],
            "steps": contract.new_steps(),
        },
    )
    assert client.get("/api/updates").json()["last_update"]["message"] == "Updated to 0.22.0."


# ---------------------------------------------------------------------------
# Go back
# ---------------------------------------------------------------------------


def _with_go_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, backup: bool = True
) -> TestClient:
    """A development checkout whose updater kept the backup of the release it came from."""
    waiting = UpdateCheck(
        "waiting-for-release",
        "stable",
        "setting",
        "You'll move to Stable when the next release includes your version.",
        latest="v0.21.0",
    )
    client = _updatable(tmp_path, monkeypatch, waiting)
    client.app.state.update_checker._installation = lambda: Installation(
        kind="git", version="0.22.0", commit="3" * 40, branch="master", root=tmp_path / "checkout"
    )
    client.app.state.update_checker.save(updates.UpdateSettings(check=True, channel="stable"))
    folder = client.app.state.update_dir
    kept = folder / contract.BACKUPS / "autodj-go-back-0.21.0-20261027-101500.zip"
    kept.parent.mkdir(parents=True, exist_ok=True)
    if backup:
        with zipfile.ZipFile(kept, "w") as archive:
            archive.writestr("manifest.json", "{}")
    contract.write(
        folder / contract.GO_BACK,
        {
            "tag": "v0.21.0",
            "version": "0.21.0",
            "commit": _OLD,
            "backup": str(kept),
            "made_at": 1_793_000_000.0,
        },
    )
    return client


def test_go_back_is_offered_with_its_date_and_hands_the_launcher_a_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """ADR-0016-20: its own audit action, the update's limit, and one pending at a time."""
    client = _with_go_back(tmp_path, monkeypatch)
    date = contract.GoBack("v0.21.0", "0.21.0", _OLD, Path("x"), 1_793_000_000.0).date
    offer = client.get("/api/updates").json()["go_back"]
    assert offer == {
        "available": True,
        "tag": "v0.21.0",
        "version": "0.21.0",
        "date": date,
        "message": "",
    }
    with caplog.at_level(logging.INFO, logger="autodj.audit"), client:
        response = client.post("/api/server/go-back", json={})
        assert response.status_code == 202
        again = client.post("/api/server/update", json={})
    assert response.json()["action"] == "go-back"
    request = contract.read_request(client.app.state.update_dir)
    assert request is not None
    assert request.action == "go-back"
    assert again.status_code == 409
    assert [entry["outcome"] for entry in _audits(caplog, "server-go-back")] == ["accepted"]
    # The same limit as Update.
    later = _with_go_back(tmp_path, monkeypatch)
    assert later.post("/api/server/go-back", json={}).status_code == 429
    assert later.post("/api/server/update", json={}).status_code == 409  # nothing available


def test_go_back_without_its_backup_is_explained_and_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0016-20."""
    client = _with_go_back(tmp_path, monkeypatch, backup=False)
    offer = client.get("/api/updates").json()["go_back"]
    assert offer["available"] is False
    assert offer["message"].startswith("Going back to 0.21.0 is not offered, because the backup")
    response = client.post("/api/server/go-back", json={})
    assert response.status_code == 409
    assert response.json()["detail"] == offer["message"]
    client.app.state.server_exit.assert_not_called()


def test_go_back_is_not_offered_off_the_stable_channel_or_without_a_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0016-20: never for a backup made by anything other than this flow."""
    client = _with_go_back(tmp_path, monkeypatch)
    client.app.state.update_checker.save(updates.UpdateSettings(check=True, channel="development"))
    assert client.get("/api/updates").json()["go_back"] is None
    client.app.state.update_checker.save(updates.UpdateSettings(check=True, channel="stable"))
    contract.remove(client.app.state.update_dir / contract.GO_BACK)
    assert client.get("/api/updates").json()["go_back"] is None
    assert client.post("/api/server/go-back", json={}).status_code == 409
