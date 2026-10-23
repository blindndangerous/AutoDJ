"""The hour preview route: a station's next hour, by station, off the event loop (#126)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autodj import hour_preview
from autodj.repeat_rules import PlayRecord
from tests._layout import point_index
from tests._server import create_app
from tests.unit._rotation_station import category_step, make_station, song


@pytest.fixture
def source(tmp_path: Path):
    music = [song(n, f"Artist {n}") for n in range(30)]
    station = make_station(
        tmp_path / "station",
        music,
        categories=[{"id": "all", "name": "Everything", "members": music}],
        clockwheels=[{"name": "Main", "steps": [category_step("all")]}],
        active_clock="Main",
        playback={"pick_mode": "clock"},
    )
    state = station.player._state
    state.current_track = music[0]
    state.play_history.append(PlayRecord.of(music[0], time.time() - 40))
    state.next_track = music[1]
    return station


@pytest.fixture
def client(bridge, tmp_path, monkeypatch, source) -> TestClient:
    point_index(bridge.player._cfg.index, tmp_path)
    bridge.player._cfg.index.index_dir = tmp_path
    monkeypatch.setattr(hour_preview, "_source", lambda _station: source)
    return TestClient(create_app(bridge))


def test_the_preview_lists_fixed_and_predicted_items(client: TestClient) -> None:
    answer = client.get("/api/hour-preview")
    assert answer.status_code == 200
    view = answer.json()
    assert view["minutes"] == 60
    statuses = [item["status_text"] for item in view["items"]]
    assert statuses[:2] == ["Fixed: on the air", "Fixed: picked"]
    assert statuses[2:] and set(statuses[2:]) == {"Predicted"}
    first = view["items"][0]
    assert set(first) >= {"title", "starts_text", "length_text"}
    assert view["summary"]
    assert view["notes"]
    # The same preview answers at the station's own address, and a longer span is its own.
    named = client.get("/api/stations/default/hour-preview").json()
    assert named["items"][:2] == view["items"][:2]
    longer = client.get("/api/hour-preview", params={"minutes": 120}).json()
    assert longer["minutes"] == 120
    assert len(longer["items"]) > len(view["items"])


@pytest.mark.parametrize("minutes", [5, 181, "an hour"])
def test_a_span_out_of_range_is_refused(client: TestClient, minutes: object) -> None:
    assert client.get("/api/hour-preview", params={"minutes": minutes}).status_code == 422


def test_no_preview_starts_once_shutdown_has_begun(client: TestClient) -> None:
    previews = hour_preview.HourPreviews()
    client.app.state.hour_previews = previews
    assert previews.shutdown(1.0)
    answer = client.get("/api/hour-preview")
    assert answer.status_code == 503
    assert answer.json()["detail"] == "The server is shutting down"


def test_a_station_without_a_library_is_refused(bridge, tmp_path) -> None:
    point_index(bridge.player._cfg.index, tmp_path)
    bridge.player._cfg.index.index_dir = tmp_path
    bridge.library = None
    answer = TestClient(create_app(bridge)).get("/api/hour-preview")
    assert answer.status_code == 409
    assert answer.json()["detail"] == "The hour preview needs the music library"


def test_a_preview_cancelled_by_shutdown_is_refused_in_words(
    client: TestClient, monkeypatch
) -> None:
    import concurrent.futures

    class Cancelling(hour_preview.HourPreviews):
        def submit(self, *_args, **_kwargs):
            future: concurrent.futures.Future = concurrent.futures.Future()
            future.cancel()
            return future

    client.app.state.hour_previews = Cancelling()
    answer = client.get("/api/hour-preview")
    assert answer.status_code == 503
    assert answer.json()["detail"] == "The server is shutting down"


def test_a_busy_worker_is_refused_in_words(client: TestClient) -> None:
    class Busy(hour_preview.HourPreviews):
        def submit(self, *_args, **_kwargs):
            raise hour_preview.PreviewBusy("The hour preview is busy; try again in a few seconds")

    client.app.state.hour_previews = Busy()
    answer = client.get("/api/hour-preview")
    assert answer.status_code == 503
    assert answer.headers["Retry-After"] == "5"
    assert answer.json()["detail"] == "The hour preview is busy; try again in a few seconds"
