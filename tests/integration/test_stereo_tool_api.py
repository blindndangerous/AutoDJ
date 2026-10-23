"""Settings, Sound: Stereo Tool's download and licence routes.

No test downloads the real Stereo Tool: :func:`autodj.stereotool_setup.install`
is replaced, or given stand-in HTTP answers.
"""

from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autodj import stereotool_setup as setup
from autodj.server import PlayerBridge
from tests._layout import point_index
from tests._server import create_app

from ._helpers import _make_player_mock, _make_sim_mock

KEY = "<STEREO-TOOL-KEY-0123456789-SECRET>"
_TOKEN = "stereo-tool-routes-token-is-32-bytes-long"


def _client(tmp_path: Path, *, token: str | None = None) -> TestClient:
    player = _make_player_mock()
    cfg = player._cfg
    point_index(cfg.index, tmp_path)
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    if token is not None:
        cfg.server.access_token = token
    return TestClient(create_app(PlayerBridge(player=player, sim=_make_sim_mock())))


@pytest.fixture(autouse=True)
def _no_environment_licence(monkeypatch) -> None:
    monkeypatch.delenv(setup.LICENSE_ENV, raising=False)


def _everything_said(response, caplog) -> str:
    """The answer and every log line, to check none holds the key."""
    return response.text + "\n".join(record.getMessage() for record in caplog.records)


def test_status_says_not_installed_and_no_licence(tmp_path: Path) -> None:
    body = _client(tmp_path).get("/api/stereo-tool").json()
    assert body["installed"] is False
    assert body["version"] is None
    assert body["license"] == {"in_use": None, "install": None, "station": None, "problem": None}
    assert body["several_stations"] is False
    assert body["terms_url"] == "https://www.thimeo.com/terms-and-conditions/"


def test_a_saved_key_is_never_sent_back_or_logged(tmp_path: Path, caplog) -> None:
    client = _client(tmp_path)
    with caplog.at_level(logging.DEBUG):
        saved = client.put("/api/stereo-tool/license", json={"key": f" {KEY} "})
        shown = client.get("/api/stereo-tool")
    assert saved.status_code == 200
    assert saved.json()["license"]["in_use"] == "install"
    assert shown.json()["license"] == {
        "in_use": "install",
        "install": "saved",
        "station": None,
        "problem": None,
    }
    for response in (saved, shown):
        assert KEY not in _everything_said(response, caplog)
        assert "SECRET" not in response.text
    path = setup.install_license_path(tmp_path)
    assert path.read_text(encoding="ascii") == KEY + "\n"
    # The audit line names the route and the status, never the body.
    audits = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "autodj.audit"
    ]
    assert any(entry["route"] == "/api/stereo-tool/license" for entry in audits)


def test_a_key_for_this_station_only_is_kept_in_its_secrets_json(tmp_path: Path, caplog) -> None:
    client = _client(tmp_path)
    station = tmp_path / "stations" / "default"
    secrets = setup.station_secrets_path(station)
    others = {"webhooks": [{"url": "https://example.com/hook"}]}
    secrets.write_text(json.dumps({"version": 1, **others}), encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        response = client.put("/api/stereo-tool/license", json={"key": KEY, "scope": "station"})
        shown = client.get("/api/stereo-tool")
    assert response.json()["license"]["in_use"] == "station"
    assert shown.json()["license"]["station"] == "saved"
    for answer in (response, shown):
        assert KEY not in _everything_said(answer, caplog)
    assert json.loads(secrets.read_text(encoding="utf-8")) == {
        "version": 1,
        **others,
        "stereo_tool": {"license_key": KEY},
    }
    assert not setup.old_station_license_path(station).exists()
    assert not setup.install_license_path(tmp_path).exists()
    cleared = client.delete("/api/stereo-tool/license", params={"scope": "station"})
    assert cleared.json()["cleared"] is True
    assert cleared.json()["license"]["in_use"] is None
    assert json.loads(secrets.read_text(encoding="utf-8")) == {"version": 1, **others}


def test_an_old_station_key_file_is_moved_without_the_key_being_shown(
    tmp_path: Path, caplog
) -> None:
    client = _client(tmp_path)
    station = tmp_path / "stations" / "default"
    old = setup.old_station_license_path(station)
    old.write_text(KEY + "\n", encoding="ascii")
    with caplog.at_level(logging.DEBUG):
        shown = client.get("/api/stereo-tool")
    assert shown.json()["license"]["in_use"] == "station"
    assert KEY not in _everything_said(shown, caplog)
    assert not old.exists()
    document = json.loads(setup.station_secrets_path(station).read_text(encoding="utf-8"))
    assert document["stereo_tool"] == {"license_key": KEY}


def test_a_damaged_secrets_json_fails_the_save_without_the_key(tmp_path: Path, caplog) -> None:
    client = _client(tmp_path)
    secrets = setup.station_secrets_path(tmp_path / "stations" / "default")
    secrets.write_text("{damaged", encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        response = client.put("/api/stereo-tool/license", json={"key": KEY, "scope": "station"})
    assert response.status_code == 500
    assert response.json()["detail"] == (
        "Could not save the licence key: secrets.json is not valid JSON."
    )
    assert KEY not in _everything_said(response, caplog)
    assert secrets.read_text(encoding="utf-8") == "{damaged"


@pytest.mark.parametrize(
    "body",
    [
        {"key": "has spaces " + KEY},
        {"key": KEY + "\n" + KEY},
        {"key": KEY, "extra": KEY},
        {"key": KEY * 200},
        {"key": 12345678},
        {"key": KEY, "scope": KEY},
    ],
)
def test_a_refused_key_is_never_repeated(tmp_path: Path, body, caplog) -> None:
    client = _client(tmp_path)
    with caplog.at_level(logging.DEBUG):
        response = client.put("/api/stereo-tool/license", json=body)
    assert response.status_code in {400, 413, 422}
    assert KEY not in _everything_said(response, caplog)
    assert "SECRET" not in response.text
    assert not setup.install_license_path(tmp_path).exists()


def test_a_licence_from_the_environment_cannot_be_changed_here(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(setup.LICENSE_ENV, KEY)
    client = _client(tmp_path)
    status = client.get("/api/stereo-tool").json()["license"]
    assert status == {
        "in_use": "environment",
        "install": "environment",
        "station": None,
        "problem": None,
    }
    refused = client.put("/api/stereo-tool/license", json={"key": "OTHER-KEY-1"})
    assert refused.status_code == 409
    assert "AUTODJ_STEREOTOOL_LICENSE_KEY" in refused.json()["detail"]
    assert client.delete("/api/stereo-tool/license").status_code == 409
    assert KEY not in client.get("/api/stereo-tool").text
    # A station may still have its own.
    own = client.put("/api/stereo-tool/license", json={"key": "OTHER-KEY-1", "scope": "station"})
    assert own.json()["license"]["in_use"] == "station"


def test_download_needs_the_terms_accepted(tmp_path: Path, monkeypatch) -> None:
    called = []
    monkeypatch.setattr(setup, "install", lambda *args, **kwargs: called.append(args))
    client = _client(tmp_path)
    refused = client.post("/api/stereo-tool/download", json={"accept_terms": False})
    assert refused.status_code == 400
    assert "accept Thimeo's terms" in refused.json()["detail"]
    assert client.post("/api/stereo-tool/download", json={}).status_code == 422
    assert client.post("/api/stereo-tool/download", json={"accept_terms": "yes"}).status_code == 422
    assert called == []


def test_download_installs_into_the_index_folder_and_audits(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    calls = []

    def install(index_dir):
        calls.append(index_dir)
        return setup.Installed(setup.managed_path(index_dir), "11.05")

    monkeypatch.setattr(setup, "install", install)
    client = _client(tmp_path)
    with caplog.at_level(logging.INFO, logger="autodj.audit"):
        response = client.post("/api/stereo-tool/download", json={"accept_terms": True})
    assert response.status_code == 200
    assert response.json() == {"version": "11.05"}
    assert calls == [tmp_path]
    actions = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "autodj.audit"
    ]
    assert {"action": "stereo-tool-download", "outcome": "success", "status": 200}.items() <= next(
        entry for entry in actions if entry["action"] == "stereo-tool-download"
    ).items()


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (setup.DownloadError("Refused to download from mirror.example.net"), 502),
        (setup.DownloadError("The download is larger than 200 MB, so it is not Stereo Tool."), 502),
        (setup.DownloadBusy("Stereo Tool is already downloading."), 409),
        (setup.NoBuildError("Thimeo offers no command-line Stereo Tool for this computer"), 409),
    ],
)
def test_download_failures_are_said_in_words(tmp_path: Path, monkeypatch, error, status) -> None:
    def install(_index_dir):
        raise error

    monkeypatch.setattr(setup, "install", install)
    response = _client(tmp_path).post("/api/stereo-tool/download", json={"accept_terms": True})
    assert response.status_code == status
    assert response.json()["detail"] == str(error)


def test_download_through_the_route_refuses_a_redirect_off_thimeo(
    tmp_path: Path, monkeypatch
) -> None:
    """The real install code, with a stand-in thimeo.com that redirects elsewhere."""
    import urllib.error
    from email.message import Message

    build = setup.platform_build("win32" if os.name == "nt" else "linux", "x86_64")
    assert build is not None
    monkeypatch.setattr(setup, "platform_build", lambda: build)
    asked = []

    def opened(url, _timeout):
        asked.append(url)
        headers = Message()
        headers["Location"] = "https://evil.example/Stereo_Tool_Generic_plugin.zip"
        raise urllib.error.HTTPError(url, 302, "Found", headers, io.BytesIO(b""))

    monkeypatch.setattr(setup, "_open", opened)
    response = _client(tmp_path).post("/api/stereo-tool/download", json={"accept_terms": True})
    assert response.status_code == 502
    assert "evil.example" in response.json()["detail"]
    assert asked == [setup.PLUGIN_URL]
    assert not setup.managed_path(tmp_path).exists()
    assert list(setup.tools_dir(tmp_path).iterdir()) == []


def test_only_a_paired_browser_reaches_the_stereo_tool_routes(tmp_path: Path, monkeypatch) -> None:
    called = []
    monkeypatch.setattr(setup, "install", lambda *args, **kwargs: called.append(args))
    client = _client(tmp_path, token=_TOKEN)
    for method, url, body in [
        ("post", "/api/stereo-tool/download", {"accept_terms": True}),
        ("put", "/api/stereo-tool/license", {"key": KEY}),
        ("get", "/api/stereo-tool", None),
    ]:
        kwargs = {"json": body} if body is not None else {}
        assert getattr(client, method)(url, **kwargs).status_code == 401
        # Neither the server's own token nor any machine token stands in for a session.
        bearer = getattr(client, method)(
            url, headers={"Authorization": f"Bearer {_TOKEN}"}, **kwargs
        )
        assert bearer.status_code == 401
    assert called == []
    assert not setup.install_license_path(tmp_path).exists()
    # A page on another site cannot press Download, even for a paired browser.
    code = client.app.state.request_pairing_code()[0]
    assert client.post("/api/pair", json={"code": code, "device_name": "Desk"}).status_code == 200
    foreign = client.post(
        "/api/stereo-tool/download",
        json={"accept_terms": True},
        headers={"Origin": "https://evil.example"},
    )
    assert foreign.status_code == 403
    assert called == []


def test_a_paired_browser_can_download(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        setup, "install", lambda index_dir: setup.Installed(setup.managed_path(index_dir), "11.05")
    )
    client = _client(tmp_path, token=_TOKEN)
    client.app.state.security_policy.now = lambda: 1_000
    code = client.app.state.request_pairing_code()[0]
    assert client.post("/api/pair", json={"code": code, "device_name": "Desk"}).status_code == 200
    response = client.post("/api/stereo-tool/download", json={"accept_terms": True})
    assert response.status_code == 200


def test_a_program_without_a_version_is_reported_as_unknown(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        setup, "install", lambda index_dir: setup.Installed(setup.managed_path(index_dir), None)
    )
    response = _client(tmp_path).post("/api/stereo-tool/download", json={"accept_terms": True})
    assert response.status_code == 200
    assert response.json() == {"version": None}


def test_a_computer_without_a_build_is_told_why(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(setup, "platform_build", lambda: None)
    note = setup.unsupported_note("darwin", "arm64")
    monkeypatch.setattr(setup, "unsupported_note", lambda: note)
    client = _client(tmp_path)
    download = client.get("/api/stereo-tool").json()["download"]
    assert download["available"] is False
    assert download["note"] == note
    refused = client.post("/api/stereo-tool/download", json={"accept_terms": True})
    assert refused.status_code == 409
    assert refused.json()["detail"] == note


def test_saving_the_same_key_again_changes_nothing(tmp_path: Path) -> None:
    client = _client(tmp_path)
    state_dir = tmp_path / "stations" / "default"
    first = client.put("/api/stereo-tool/license", json={"key": KEY, "scope": "station"})
    assert first.json()["changed"] is True
    before = (setup.generation(), setup.generation(state_dir))
    again = client.put("/api/stereo-tool/license", json={"key": KEY, "scope": "station"})
    assert again.json()["changed"] is False
    assert (setup.generation(), setup.generation(state_dir)) == before


def test_status_says_what_the_running_stereo_tool_said_of_its_licence(tmp_path: Path) -> None:
    client = _client(tmp_path)
    player = client.app.state.stations.default.bridge.player
    state_dir = tmp_path / "stations" / "default"
    report = {
        "generation": setup.generation(state_dir),
        "version": "11.05",
        "licensed": False,
        "unlicensed": "Declipper",
    }
    player._continuous_master = type("Master", (), {"stereotool_report": report})()
    body = client.get("/api/stereo-tool").json()
    assert body["running"] == {"licensed": False, "unlicensed": "Declipper"}
    # Saving a key makes that verdict old news until the restarted Stereo Tool says again.
    saved = client.put("/api/stereo-tool/license", json={"key": KEY, "scope": "station"})
    assert saved.json()["running"] is None
    assert client.get("/api/stereo-tool").json()["running"] is None
    report["generation"] = setup.generation(state_dir)
    assert client.get("/api/stereo-tool").json()["running"] == {
        "licensed": False,
        "unlicensed": "Declipper",
    }
    # A library put there by hand takes the version the running Stereo Tool reports.
    managed = setup.managed_path(tmp_path)
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"library")
    assert client.get("/api/stereo-tool").json()["version"] == "11.05"
