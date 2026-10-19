"""Exercise stopped-station production, real review and explicit approval."""

import time

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from autodj.server import create_app
from tests._layout import point_index
from tests.unit.test_review_source import station_content


@pytest.fixture
def prepared_client(bridge, tmp_path, monkeypatch):
    (tmp_path / "content").mkdir()
    station, show_id, _ = station_content(tmp_path / "content", seconds=2)
    point_index(bridge.player._cfg.index, tmp_path / "server")
    app = create_app(bridge)
    real = app.state.stations.editable
    monkeypatch.setattr(
        app.state.stations, "editable", lambda slug: station if slug == "default" else real(slug)
    )
    client = TestClient(app)
    yield client, station, show_id
    if hasattr(app.state, "review_jobs"):
        assert app.state.review_jobs.shutdown(2)


def test_stopped_edition_import_real_review_explicit_approval_and_retained_audio(
    prepared_client, tmp_path
):
    """ADR-0015-03: public APIs consume a private full-target receipt explicitly."""
    client, station, show_id = prepared_client
    created = client.post("/api/editions", json={"show_id": show_id, "name": "Tomorrow"})
    assert created.status_code in {200, 201}, created.text
    edition = created.json()
    wav = tmp_path / "speech.wav"
    sf.write(wav, np.full(44100, 0.15, np.float32), 44100)
    url = f"/api/editions/{edition['id']}"
    uploaded = client.post(
        url + "/takes",
        params={
            "expected_revision": edition["revision"],
            "filename": wav.name,
            "position": "transition",
            "placement": "standalone",
            "before_id": edition["occurrences"][0]["id"],
            "after_id": edition["occurrences"][1]["id"],
        },
        content=wav.read_bytes(),
        headers={"Content-Type": "audio/wav"},
    )
    assert uploaded.status_code == 201, uploaded.text
    edition = uploaded.json()
    take = edition["takes"][0]
    assert take["status"] == "needs_review"
    asset = client.get(url + "/assets/" + take["asset_id"])
    assert asset.status_code == 200 and asset.content.startswith(b"RIFF")
    submitted = client.post(
        "/api/reviews",
        json={
            "source": "edition",
            "edition_id": edition["id"],
            "target_take_id": take["id"],
            "include_pending": True,
            "head_seconds": 0.01,
            "tail_seconds": 0.01,
        },
    )
    assert submitted.status_code == 200, submitted.text
    review_id = submitted.json()["id"]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = client.get("/api/reviews/jobs/" + review_id).json()
        if job["status"] != "running":
            break
        time.sleep(0.02)
    assert job["status"] == "done", job
    reviewed = client.get("/api/reviews/" + review_id).json()
    assert reviewed["approval_review_complete"] is True
    assert not any(key.startswith("_") for key in reviewed)
    assert client.get(url).json()["takes"][0]["approval"] is None
    approved = client.post(url + f"/takes/{take['id']}/approve", json={"review_id": review_id})
    assert approved.status_code == 200, approved.text
    assert approved.json()["takes"][0]["status"] == "ready"
    assert client.get(url + "/assets/" + take["asset_id"]).content == asset.content
    assert station.player is None


@pytest.mark.parametrize("prefix", ["/api", "/api/stations/default"])
def test_edition_access_and_early_upload_cap(prefix):
    """ADR-0015-06: assets and edits require paired browser authorization."""
    from tests.integration.test_server_security import _pair, _security_client

    client = _security_client()
    assert client.get(prefix + "/editions").status_code == 401
    assert client.get(prefix + "/editions/" + "a" * 32 + "/assets/" + "b" * 32).status_code == 401
    assert _pair(client).status_code == 200
    assert client.get(prefix + "/editions").status_code == 200
    assert (
        client.post(
            prefix + "/editions", json={"show_id": "a" * 32}, headers={"Origin": ""}
        ).status_code
        == 403
    )
    oversized = client.post(
        prefix + "/editions/" + "a" * 32 + "/takes",
        content=b"",
        headers={"Content-Length": str(512 * 1024**2 + 1)},
    )
    assert oversized.status_code == 413


@pytest.mark.parametrize("prefix", ["/api", "/api/stations/default"])
def test_reattach_body_is_capped_before_json_parsing(prepared_client, prefix):
    from autodj.worker_api import SMALL_BODY_MAX_BYTES

    client, _, _ = prepared_client
    response = client.post(
        f"{prefix}/editions/{'a' * 32}/takes/{'b' * 32}/reattach",
        content=b" " * SMALL_BODY_MAX_BYTES + b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
