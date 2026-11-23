"""Library duplication uses the control API without changing station ownership."""

import pytest
from fastapi.testclient import TestClient

from autodj._bridge import PlayerBridge
from tests._server import create_app
from tests.integration._helpers import _make_player_mock
from tests.unit.test_library_runtime import libraries as libraries


@pytest.fixture
def client(libraries):
    selected = libraries.get("main")
    selected.cfg.server.allowed_hosts = ["testserver"]
    selected.cfg.server.allowed_origins = ["http://testserver"]
    player = _make_player_mock()
    player._cfg = selected.cfg
    bridge = PlayerBridge(player=player, sim=selected.sim, library=selected.library)
    return TestClient(create_app(bridge, libraries=libraries))


def test_duplicate_route_publishes_a_library_without_rebinding_station(client, libraries):
    response = client.post(
        "/api/libraries/main/duplicate", json={"id": "copy", "name": "Copy of Main"}
    )
    assert response.status_code == 200, response.text
    assert {"id": "copy", "name": "Copy of Main"} in response.json()["libraries"]
    assert libraries.get("copy").library.catalog.path != libraries.get("main").library.catalog.path
    assert libraries.root.station.library_id == "main"
    assert libraries.registry.get("main").name == "Main"


@pytest.mark.parametrize(
    ("source", "body", "status"),
    [
        ("missing", {"id": "copy", "name": "Copy"}, 404),
        ("main", {"id": "other", "name": "Copy"}, 409),
        ("main", {"id": "copy", "name": "Other"}, 409),
        ("main", {"id": "../outside", "name": "Copy"}, 400),
    ],
)
def test_duplicate_route_rejects_unknown_source_and_conflicts(
    client, libraries, source, body, status
):
    before = libraries.registry.list()
    response = client.post(f"/api/libraries/{source}/duplicate", json=body)
    assert response.status_code == status, response.text
    assert libraries.registry.list() == before


def test_duplicate_route_reports_copy_failure_without_publishing(client, libraries, monkeypatch):
    def fail_snapshot(source, stage):
        (stage / "partial").write_text("unfinished")
        raise OSError("private local storage path")

    monkeypatch.setattr("autodj.library_runtime.snapshot_library", fail_snapshot)
    response = client.post("/api/libraries/main/duplicate", json={"id": "copy", "name": "Copy"})
    assert response.status_code == 500
    assert "private local storage path" not in response.text
    assert [item.id for item in libraries.registry.list()] == ["main", "other"]
    assert not (libraries.registry.root / "copy").exists()
    assert not list(libraries.registry.root.glob(".library-create-*"))
