"""The rotation tester's routes: start, follow, cancel, and one run at a time (#125)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from autodj import rotation_api
from autodj.rotation_sim import SimulationRequest
from autodj.server import create_app
from tests.unit._rotation_station import category_step, make_station, song

NOON = datetime(2026, 10, 12, 12, 0, tzinfo=UTC).timestamp()


def _station(tmp_path: Path):
    music = [song(n, f"Artist {n}") for n in range(30)]
    return make_station(
        tmp_path / "station",
        music,
        categories=[{"id": "all", "name": "Everything", "members": music}],
        clockwheels=[{"name": "Main", "steps": [category_step("all")]}],
        active_clock="Main",
        playback={"pick_mode": "clock"},
    )


@pytest.fixture
def client(bridge, tmp_path, monkeypatch) -> TestClient:
    bridge.player._cfg.index.active_dir = tmp_path
    bridge.player._cfg.index.index_dir = tmp_path
    source = _station(tmp_path)
    monkeypatch.setattr(rotation_api, "station_source", lambda _station: source)
    return TestClient(create_app(bridge))


def _finished(client: TestClient, path: str = "/api/rotation-test") -> dict[str, Any]:
    for _ in range(500):
        state = client.get(path).json()
        if state["state"] != "running":
            return state
        time.sleep(0.02)
    raise AssertionError("the rotation test did not finish")


def test_a_run_reports_in_json_and_in_words(client: TestClient) -> None:
    idle = client.get("/api/rotation-test").json()
    assert idle["state"] == "idle"
    assert idle["report"] is None
    started = client.post(
        "/api/rotation-test", json={"hours": 6, "seed": 3, "start": "2026-10-12T12:00"}
    )
    assert started.status_code == 202
    assert started.json()["state"] in {"running", "done"}
    done = _finished(client)
    assert done["state"] == "done"
    assert done["progress"]["percent"] == 100
    report = done["report"]
    assert report["seed"] == 3
    assert report["start"] == "2026-10-12T12:00+00:00"
    assert report["picks"] > 0
    assert done["text"].startswith("Rotation test for station default\n")
    titles = [section["title"] for section in done["sections"]]
    assert titles[:3] == ["Summary", "How songs were picked", "Songs"]
    # The same run answers at the station's own address.
    assert client.get("/api/stations/default/rotation-test").json()["report"] == report


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        ({"days": 31}, 400, "The span must be more than 0 and at most 720 hours"),
        ({"picks": 20001}, 400, "The number of songs must be from 1 to 20,000"),
        ({"hours": 1, "picks": 3}, 422, None),
        ({"hours": 1, "speed": "fast"}, 422, None),
    ],
)
def test_bad_requests_are_refused(client: TestClient, body: dict, status: int, detail) -> None:
    answer = client.post("/api/rotation-test", json=body)
    assert answer.status_code == status
    if detail:
        assert answer.json()["detail"] == detail
    assert client.get("/api/rotation-test").json()["state"] == "idle"


def test_one_run_at_a_time_and_cancel(client: TestClient, monkeypatch) -> None:
    real = rotation_api.simulate

    def slow(source, request, *, progress=None, cancelled=None):
        while not cancelled():
            time.sleep(0.01)
        return real(source, SimulationRequest(picks=2, start=NOON, seed=1), cancelled=lambda: True)

    monkeypatch.setattr(rotation_api, "simulate", slow)
    assert client.delete("/api/rotation-test").status_code == 409
    assert client.post("/api/rotation-test", json={"hours": 1}).status_code == 202
    # The server keeps answering while the run works.
    assert client.get("/api/rotation-test").json()["state"] == "running"
    second = client.post("/api/rotation-test", json={"picks": 5})
    assert second.status_code == 409
    assert second.json()["detail"] == "A rotation test is already running"
    assert client.delete("/api/rotation-test").status_code == 200
    stopped = _finished(client)
    assert stopped["state"] == "cancelled"
    assert stopped["report"]["stopped_early"] is True
    assert "Stopped early: the run was cancelled." in stopped["text"]
    assert client.delete("/api/rotation-test").status_code == 409


def test_a_station_without_a_library_cannot_be_tested(bridge, tmp_path) -> None:
    bridge.player._cfg.index.active_dir = tmp_path
    bridge.player._cfg.index.index_dir = tmp_path
    client = TestClient(create_app(bridge))
    refused = client.post("/api/rotation-test", json={"hours": 1})
    assert refused.status_code == 409
    assert refused.json()["detail"] == "The rotation test needs the music library"


def test_unpaired_browsers_cannot_start_a_run(bridge, tmp_path) -> None:
    bridge.player._cfg.server.access_token = "pairing-secret-that-is-at-least-32-bytes"
    bridge.player._cfg.index.active_dir = tmp_path
    bridge.player._cfg.index.index_dir = tmp_path
    client = TestClient(create_app(bridge))
    assert client.get("/api/rotation-test").status_code in (401, 403)
    assert client.post("/api/rotation-test", json={"hours": 1}).status_code in (401, 403)
