"""The rotation tester's routes: start, follow, cancel, and one run at a time (#125)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from autodj import rotation_api
from autodj.rotation_sim import SimulationRequest
from tests._layout import point_index
from tests._server import create_app
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
    point_index(bridge.player._cfg.index, tmp_path)
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
    _slow(monkeypatch)
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
    point_index(bridge.player._cfg.index, tmp_path)
    bridge.player._cfg.index.index_dir = tmp_path
    client = TestClient(create_app(bridge))
    refused = client.post("/api/rotation-test", json={"hours": 1})
    assert refused.status_code == 409
    assert refused.json()["detail"] == "The rotation test needs the music library"


def test_unpaired_browsers_cannot_start_a_run(bridge, tmp_path) -> None:
    bridge.player._cfg.server.access_token = "pairing-secret-that-is-at-least-32-bytes"
    point_index(bridge.player._cfg.index, tmp_path)
    bridge.player._cfg.index.index_dir = tmp_path
    client = TestClient(create_app(bridge))
    assert client.get("/api/rotation-test").status_code in (401, 403)
    assert client.post("/api/rotation-test", json={"hours": 1}).status_code in (401, 403)


def _slow(monkeypatch) -> None:
    """Runs that work until cancelled, then report what they have."""
    real = rotation_api.simulate

    def slow(source, request, *, progress=None, cancelled=None):
        while not cancelled():
            time.sleep(0.01)
        return real(source, SimulationRequest(picks=2, start=NOON, seed=1), cancelled=lambda: True)

    monkeypatch.setattr(rotation_api, "simulate", slow)


def test_a_run_whose_thread_cannot_start_gives_the_slot_back(client, monkeypatch) -> None:
    assert client.post("/api/rotation-test", json={"picks": 3, "seed": 1}).status_code == 202
    first = _finished(client)

    def refuse(_run) -> None:
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as patch:
        patch.setattr(rotation_api.RotationTestRun, "start", refuse)
        refused = client.post("/api/rotation-test", json={"picks": 3})
    assert refused.status_code == 503
    assert refused.json()["detail"] == "The rotation test could not start; try again"
    # The slot is free and the earlier result is back.
    assert client.get("/api/rotation-test").json() == first
    assert client.post("/api/rotation-test", json={"picks": 3, "seed": 2}).status_code == 202
    assert _finished(client)["report"]["seed"] == 2


def test_another_stations_run_holds_the_one_slot() -> None:
    tests = rotation_api.RotationTests()
    busy = rotation_api.RotationTestRun("nightowl", None, SimulationRequest(picks=1))
    busy.start = lambda: None  # The run itself is not needed.
    tests.start(busy)
    second = rotation_api.RotationTestRun("default", None, SimulationRequest(picks=1))
    with pytest.raises(HTTPException) as refused:
        tests.start(second)
    assert refused.value.status_code == 409
    assert refused.value.detail == "A rotation test is already running for station nightowl"
    assert tests.get("default") is None


def test_shutting_the_server_down_cancels_and_waits_for_a_run(
    bridge, tmp_path, monkeypatch
) -> None:
    point_index(bridge.player._cfg.index, tmp_path)
    bridge.player._cfg.index.index_dir = tmp_path
    source = _station(tmp_path)
    monkeypatch.setattr(rotation_api, "station_source", lambda _station: source)
    _slow(monkeypatch)
    app = create_app(bridge)
    with TestClient(app) as client:
        assert client.post("/api/rotation-test", json={"hours": 1}).status_code == 202
        run = app.state.rotation_tests.get("default")
        assert run.state == "running"
    # Leaving the block ran the server's shutdown.
    assert not run.thread.is_alive()
    assert run.state == "cancelled"


@pytest.mark.parametrize("path", ["/api/rotation-test", "/api/stations/default/rotation-test"])
def test_large_bodies_are_refused_before_parsing(client: TestClient, path: str) -> None:
    answer = client.post(
        path,
        content=b'{"hours": 1, "pad": "' + b"x" * 9000 + b'"}',
        headers={"Content-Type": "application/json"},
    )
    assert answer.status_code == 413
    assert client.get("/api/rotation-test").json()["state"] == "idle"


@pytest.mark.parametrize("start", ["1900-01-01T00:00", "9999-12-31T23:59", "0001-01-01T00:00"])
def test_a_start_out_of_range_is_refused_in_words(client: TestClient, start: str) -> None:
    answer = client.post("/api/rotation-test", json={"hours": 1, "start": start})
    assert answer.status_code == 400
    assert answer.json()["detail"] == (
        "The start must be between 1 January 2000 and 31 December 2099"
    )


def test_no_run_starts_once_shutdown_has_begun() -> None:
    tests = rotation_api.RotationTests()
    assert tests.shutdown(1.0) is True
    late = rotation_api.RotationTestRun("default", None, SimulationRequest(picks=1))
    with pytest.raises(HTTPException) as refused:
        tests.start(late)
    assert refused.value.status_code == 503
    assert refused.value.detail == "The server is shutting down"
    assert tests.get("default") is None


def test_a_run_that_outlives_shutdown_is_logged(bridge, tmp_path, monkeypatch, caplog) -> None:
    import logging

    point_index(bridge.player._cfg.index, tmp_path)
    bridge.player._cfg.index.index_dir = tmp_path
    app = create_app(bridge)
    app.state.rotation_tests = rotation_api.RotationTests()
    monkeypatch.setattr(app.state.rotation_tests, "shutdown", lambda _timeout: False)
    with caplog.at_level(logging.WARNING, logger="autodj.server"), TestClient(app):
        pass
    assert any(
        "A rotation test was still running" in record.getMessage() for record in caplog.records
    )


def test_starting_a_run_never_holds_up_other_requests(client: TestClient, monkeypatch) -> None:
    """The settings copy waits on the settings lock in a worker thread, not on the event loop."""
    import threading

    from autodj.player import SETTINGS_LOCK

    _slow(monkeypatch)
    answers: list[int] = []
    with SETTINGS_LOCK:  # A settings change in progress.
        poster = threading.Thread(
            target=lambda: answers.append(
                client.post("/api/rotation-test", json={"hours": 1}).status_code
            )
        )
        poster.start()
        time.sleep(0.2)
        # The start is still waiting for its copy, and the server answers meanwhile.
        assert answers == []
        assert client.get("/api/rotation-test").json()["state"] == "idle"
    poster.join(30)
    assert answers == [202]
    assert client.delete("/api/rotation-test").status_code == 200
    assert _finished(client)["state"] == "cancelled"
