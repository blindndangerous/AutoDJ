"""The shows routes (#106): plan a list, edit it, generate it, schedule and air it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autodj import rotation_api, shows_api
from tests._layout import point_index
from tests._server import create_app
from tests.unit._rotation_station import category_step, make_station, song


@pytest.fixture
def station(tmp_path: Path):
    hits = [song(n, f"Hits {n}", length=180.0) for n in range(20)]
    gold = [song(100 + n, f"Gold {n}", length=240.0) for n in range(20)]
    source = make_station(
        tmp_path / "station",
        [*hits, *gold],
        categories=[
            {"id": "hits", "name": "Remembering the 2000s", "members": hits},
            {"id": "gold", "name": "Gold", "members": gold},
        ],
        clockwheels=[{"name": "Throwback", "steps": [category_step("hits")]}],
        active_clock="Throwback",
        playback={"pick_mode": "clock"},
    )
    return source, hits, gold


@pytest.fixture
def client(bridge, tmp_path, monkeypatch, station) -> TestClient:
    source, _hits, _gold = station
    point_index(bridge.player._cfg.index, tmp_path)
    bridge.player._cfg.index.index_dir = tmp_path
    bridge.entry_for_path = source.library.entry_for_path
    monkeypatch.setattr(rotation_api, "station_source", lambda _station: source)
    monkeypatch.setattr(
        shows_api,
        "_source_names",
        lambda _station: ({"hits": "Remembering the 2000s", "gold": "Gold"}, ["Throwback"]),
    )
    return TestClient(create_app(bridge))


SHOW = {"name": "Remembering the 2000s", "source": "category", "category": "hits"}


def test_plan_edit_generate_and_remove_a_show(client: TestClient, station, tmp_path) -> None:
    _source, hits, gold = station
    created = client.post("/api/shows", json={**SHOW, "target_minutes": 30})
    assert created.status_code == 201, created.text
    show = created.json()
    assert (show["source_text"], show["count"], show["songs"], show["events"]) == (
        "Category Remembering the 2000s",
        0,
        [],
        [],
    )
    url = f"/api/shows/{show['id']}"
    saved = json.loads(
        (tmp_path / "stations" / "default" / "shows.json").read_text(encoding="utf-8")
    )
    assert list(saved["shows"]) == [show["id"]]

    generated = client.post(f"{url}/generate", json={"start": "2026-10-12T20:00", "seed": 7})
    assert generated.status_code == 200, generated.text
    view = generated.json()
    assert view["seed"] == 7
    members = {entry.path for entry in hits}
    paths = [row["path"] for row in view["songs"]]
    assert paths and set(paths) <= members
    assert view["total_text"] == "30:00"
    assert view["total_text"] == "30:00"
    assert view["generated_at"]

    # Reorder, remove one and add one from the library.
    edited = [*reversed(paths[1:]), gold[0].path]
    answer = client.put(f"{url}/songs", json={"songs": edited})
    assert answer.status_code == 200, answer.text
    assert [row["path"] for row in answer.json()["songs"]] == edited
    assert answer.json()["songs"][-1]["title"] == "Gold 0, Song 100"
    refused = client.put(f"{url}/songs", json={"songs": [*edited, "/music/nowhere.flac"]})
    assert refused.status_code == 400
    assert refused.json()["field"] == "songs"

    renamed = client.put(url, json={**SHOW, "name": "The 2000s", "target_minutes": 45})
    assert renamed.status_code == 200, renamed.text
    assert (renamed.json()["name"], renamed.json()["target_minutes"]) == ("The 2000s", 45)
    assert [row["path"] for row in renamed.json()["songs"]] == edited

    listed = client.get("/api/stations/default/shows").json()
    assert [item["name"] for item in listed["shows"]] == ["The 2000s"]
    assert "songs" not in listed["shows"][0]
    assert listed["categories"] == [
        {"id": "gold", "name": "Gold"},
        {"id": "hits", "name": "Remembering the 2000s"},
    ]
    assert listed["clockwheels"] == ["Throwback"]
    assert listed["on_air"] is None
    assert listed["limits"]["max_songs"] == 500

    assert client.delete(url).json() == {"deleted": show["id"], "name": "The 2000s"}
    assert client.get(url).status_code == 404
    assert client.get("/api/shows/not-an-id").status_code == 404


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({**SHOW, "name": ""}, "name"),
        ({**SHOW, "category": "gone"}, "category"),
        ({"name": "A", "source": "clockwheel", "clockwheel": "Gone"}, "clockwheel"),
        ({**SHOW, "target_minutes": 1000}, "target_minutes"),
    ],
)
def test_a_refused_field_is_named(client: TestClient, body, field) -> None:
    answer = client.post("/api/shows", json=body)
    assert answer.status_code == 400
    assert answer.json()["field"] == field


def test_a_taken_name_and_a_bad_start_are_refused(client: TestClient) -> None:
    show = client.post("/api/shows", json=SHOW).json()
    taken = client.post("/api/shows", json={**SHOW, "name": "remembering the 2000s"})
    assert (taken.status_code, taken.json()["field"]) == (400, "name")
    late = client.post(f"/api/shows/{show['id']}/generate", json={"start": "2150-01-01T00:00"})
    assert (late.status_code, late.json()["field"]) == (400, "start")


def test_the_schedule_can_play_a_show(client: TestClient) -> None:
    show = client.post("/api/shows", json=SHOW).json()
    actions = {action["name"]: action for action in client.get("/api/schedule").json()["actions"]}
    field = actions["show"]["fields"][0]
    assert actions["show"]["description"] == "Play a show"
    assert field["options"] == [{"value": show["id"], "label": "Remembering the 2000s"}]
    event = client.post(
        "/api/schedule",
        json={
            "name": "Saturday throwback",
            "action": "show",
            "params": {"show": show["id"]},
            "repeat": "days",
            "days": [5],
            "time": "20:00",
            "timing": "hard",
        },
    )
    assert event.status_code == 201, event.text
    assert event.json()["action_text"] == "Play a show: Remembering the 2000s"
    assert client.get(f"/api/shows/{show['id']}").json()["events"] == ["Saturday throwback"]
    unknown = client.post(
        "/api/schedule",
        json={
            "name": "Nothing",
            "action": "show",
            "params": {"show": "f" * 32},
            "repeat": "daily",
            "time": "20:00",
        },
    )
    assert unknown.status_code == 400
    assert unknown.json()["field"] == "params.show"


def test_starting_and_stopping_say_why_not(client: TestClient) -> None:
    show = client.post("/api/shows", json=SHOW).json()
    empty = client.post(f"/api/shows/{show['id']}/start")
    assert empty.status_code == 409
    assert "generate its list first" in empty.json()["detail"]
    assert client.post(f"/api/shows/{'f' * 32}/start").status_code == 409
    stopped = client.delete("/api/shows/on-air")
    assert (stopped.status_code, stopped.json()["detail"]) == (409, "No show is on the air")


def test_the_list_cannot_be_changed_while_it_is_generated(client: TestClient, station) -> None:
    from autodj.shows import ShowGenerations

    _source, hits, _gold = station
    show = client.post("/api/shows", json=SHOW).json()
    generations = client.app.state.show_generations = ShowGenerations()
    with generations.reserved("default", show["id"]):
        refused = client.put(f"/api/shows/{show['id']}/songs", json={"songs": [hits[0].path]})
        assert refused.status_code == 409
        assert "being generated" in refused.json()["detail"]
        again = client.post(f"/api/shows/{show['id']}/generate", json={})
        assert again.status_code == 409
    saved = client.put(f"/api/shows/{show['id']}/songs", json={"songs": [hits[0].path]})
    assert saved.status_code == 200
