"""Speech routes use browser control and saved station ownership."""

import pytest
from fastapi.testclient import TestClient

from autodj.server import PlayerBridge, create_app
from tests._layout import point_index
from tests.integration._helpers import _make_player_mock, _make_sim_mock
from tests.integration.test_multi_station import rig  # noqa: F401


@pytest.fixture
def speech_client(tmp_path):
    player = _make_player_mock()
    point_index(player._cfg, tmp_path)
    app = create_app(PlayerBridge(player, _make_sim_mock()))
    return TestClient(app)


def test_provider_keys_are_write_only_and_station_routes_are_aliased(speech_client):
    """ADR-0013-01: shared credentials do not appear in station/browser views."""
    response = speech_client.put(
        "/api/speech/providers/local",
        json={
            "engine": "chatterbox",
            "name": "Local",
            "url": "http://127.0.0.1:4123/v1/audio/speech",
            "trusted_local": True,
            "api_key": "secret-speech-key",
        },
    )
    assert response.status_code == 200
    response = speech_client.get("/api/speech/providers")
    assert response.status_code == 200
    assert "secret-speech-key" not in response.text
    for prefix in ["/api", "/api/stations/default"]:
        state = speech_client.get(prefix + "/speech")
        assert state.status_code == 200
        assert state.json()["batches"] == {}
    assert speech_client.get("/api/stations/absent/speech").status_code == 404


def test_preflight_does_not_generate_and_unknown_fields_are_refused(speech_client):
    response = speech_client.post(
        "/api/speech/preflight",
        json={"text": "Station ID", "candidates": [{"connection": "absent", "voice": "v"}]},
    )
    assert response.status_code == 200
    assert response.json()["candidates"][0]["error"]
    assert speech_client.get("/api/speech").json()["batches"] == {}
    assert (
        speech_client.post(
            "/api/speech/batches",
            json={
                "text": "Hello",
                "candidates": [],
                "token": "sample",
                "url": "file:///etc/passwd",
            },
        ).status_code
        == 422
    )


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_stopped_station_can_use_speech_without_starting_playout(rig):  # noqa: F811
    client, _library, _cfg, _song, _closes = rig
    assert client.get("/api/stations/nightowl/speech").status_code == 200
    response = client.post(
        "/api/stations/nightowl/speech/preflight",
        json={"text": "Station ID", "candidates": [{"connection": "unconfigured", "voice": "v"}]},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200
    assert response.json()["candidates"][0]["error"]
    assert client.get("/api/stations").json()["stations"][1]["state"] == "stopped"


def test_speech_request_body_is_bounded_before_json_parsing(speech_client):
    assert (
        speech_client.post(
            "/api/speech/preflight",
            content=b"x" * 65537,
            headers={"Content-Type": "application/json"},
        ).status_code
        == 413
    )


def test_station_command_is_config_only_and_preflight_is_station_scoped(speech_client):
    station = speech_client.app.state.stations.configuration("default")
    station.time_checks.tts_command = ["operator-tts-secret-path", "{out}"]
    state = speech_client.get("/api/speech")
    assert state.json()["command_available"] is True
    assert "operator-tts-secret-path" not in state.text
    response = speech_client.post(
        "/api/speech/preflight",
        json={"text": "Station ID", "candidates": [{"connection": "station-command"}]},
    )
    assert response.status_code == 200
    assert response.json()["candidates"][0]["error"] is None
    assert speech_client.get("/api/speech").json()["batches"] == {}
    assert (
        speech_client.post(
            "/api/speech/preflight",
            json={
                "text": "Station ID",
                "candidates": [{"connection": "station-command", "command": ["evil"]}],
            },
        ).status_code
        == 422
    )
    assert speech_client.put("/api/speech/providers/station-command", json={}).status_code == 400
