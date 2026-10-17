"""Operator review routes validate inputs and keep files behind station access."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from autodj.review_api import ReviewBody
from autodj.server import create_app
from tests._layout import point_index


@pytest.mark.parametrize(
    "change",
    [
        {"head_seconds": 0},
        {"tail_seconds": -1},
        {"clip_threshold_seconds": float("inf")},
        {"tone_seconds": float("nan")},
        {"minutes": 0},
        {"selection_start": -1},
        {"selection_start": 10, "selection_end": 5},
        {"source": "path", "path": "/etc/passwd"},
        {"source": "show"},
        {"keep_complete": ["../secret"]},
    ],
)
def test_invalid_review_input_is_rejected(change):
    with pytest.raises(ValidationError):
        ReviewBody.model_validate({"source": "forecast", **change})


def test_review_routes_are_available_with_default_and_named_station(bridge, tmp_path):
    point_index(bridge.player._cfg.index, tmp_path)
    app = create_app(bridge)
    client = TestClient(app)
    for prefix in ["/api", "/api/stations/default"]:
        response = client.get(prefix + "/reviews")
        assert response.status_code == 200
        assert response.json() == {"results": [], "job": None}
        assert client.get(prefix + "/reviews/" + "f" * 32).status_code == 404
        assert client.get(prefix + "/reviews/" + "f" * 32 + "/audio/wav").status_code == 404
    assert client.get("/api/stations/missing/reviews").status_code == 404
    assert client.post("/api/reviews", json={"source": "show"}).status_code == 422
    assert app.state.review_jobs.shutdown(1)


def test_review_download_rejects_invalid_identity_and_format(bridge, tmp_path):
    point_index(bridge.player._cfg.index, tmp_path)
    app = create_app(bridge)
    client = TestClient(app)
    assert client.get("/api/reviews/not-an-id/audio/mp3").status_code == 404
    assert client.get("/api/reviews/" + "a" * 32 + "/audio/../../secrets").status_code == 404
    assert not hasattr(app.state, "review_jobs")


def test_review_generation_download_and_stale_snapshot_journey(bridge, tmp_path, monkeypatch):
    import time

    from tests.unit.test_review_source import station_content

    (tmp_path / "content").mkdir()
    station, show_id, _entries = station_content(tmp_path / "content")
    point_index(bridge.player._cfg.index, tmp_path / "server")
    app = create_app(bridge)
    registry = app.state.stations
    real_editable = registry.editable
    monkeypatch.setattr(
        registry, "editable", lambda slug: station if slug == "default" else real_editable(slug)
    )
    client = TestClient(app)
    submitted = client.post("/api/reviews", json={"source": "show", "show_id": show_id})
    assert submitted.status_code == 200, submitted.text
    job_id = submitted.json()["id"]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = client.get("/api/reviews/jobs/" + job_id).json()
        if job["status"] != "running":
            break
        time.sleep(0.02)
    assert job["status"] == "done", job
    result = client.get("/api/reviews/" + job_id).json()
    assert result["stale"] is False
    assert result["items"]
    assert not any(key.startswith("_") for key in result)
    wav = client.get("/api/reviews/" + job_id + "/audio/wav")
    mp3 = client.get("/api/reviews/" + job_id + "/audio/mp3")
    assert wav.status_code == mp3.status_code == 200
    assert wav.content.startswith(b"RIFF") and len(mp3.content) > 100
    assert wav.headers["cache-control"] == "private, no-store"
    station.shows.update(show_id, lambda show: show.model_copy(update={"name": "Changed"}))
    assert client.get("/api/reviews/" + job_id).json()["stale"] is True
    assert client.get("/api/reviews/" + job_id + "/audio/wav").content == wav.content
    assert client.delete("/api/reviews/" + job_id).status_code == 200
    assert client.get("/api/reviews").json()["results"] == []
    assert app.state.review_jobs.shutdown(1)


@pytest.mark.parametrize("path", ["/api/reviews", "/api/stations/default/reviews"])
def test_review_access_needs_paired_browser_and_origin(path):
    """ADR-0014-06: reviews and exports remain on the operator browser boundary."""
    from tests.integration.test_server_security import _pair, _security_client

    client = _security_client()
    assert client.get(path).status_code == 401
    assert client.post(path, json={"source": "forecast"}).status_code == 401
    assert client.get(path + "/" + "a" * 32 + "/audio/wav").status_code == 401
    assert _pair(client).status_code == 200
    assert client.get(path).status_code == 200
    assert client.post(path, json={"source": "forecast"}, headers={"Origin": ""}).status_code == 403
    assert client.app.state.review_jobs.shutdown(1)


@pytest.mark.parametrize("scope", ["analysis-worker", "announcer"])
def test_machine_credentials_cannot_generate_or_download_reviews(scope):
    """ADR-0014-06: narrow machine credentials never grant operator review access."""
    from tests.integration.test_server_security import _security_client

    client = _security_client()
    _token, secret = client.app.state.machine_tokens().create(
        "Review boundary " + scope, [scope], station="default" if scope == "announcer" else None
    )
    headers = {"Authorization": "Bearer " + secret, "Origin": ""}
    for prefix in ["/api", "/api/stations/default"]:
        assert client.get(prefix + "/reviews", headers=headers).status_code in {401, 403}
        assert client.post(
            prefix + "/reviews", json={"source": "forecast"}, headers=headers
        ).status_code in {401, 403}
        assert client.get(
            prefix + "/reviews/" + "a" * 32 + "/audio/wav", headers=headers
        ).status_code in {401, 403}
