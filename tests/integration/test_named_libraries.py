"""Library management and station-scoped catalog routes."""

import json
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from autodj._bridge import PlayerBridge
from autodj.config import station_config
from tests._server import create_app
from tests.integration._helpers import _make_player_mock, patch_station_settings
from tests.unit.test_library import make_entry
from tests.unit.test_library_runtime import libraries as libraries


@pytest.fixture(autouse=True)
def browser_origin(libraries):
    libraries.root.server.allowed_hosts = ["testserver"]
    libraries.root.server.allowed_origins = ["http://testserver"]


def test_library_routes_use_active_station_or_explicit_library(libraries, monkeypatch):
    cfg = station_config(libraries.root, libraries.root)
    cfg.station.library_id = "other"
    selected = libraries.bind("default", cfg)
    song = libraries.root.library.music_dir / "song.flac"
    song.write_bytes(b"audio")
    for services, title in ((libraries.get("main"), "Main title"), (selected, "Other title")):
        services.library.catalog.add_source(song.parent)
        services.library.catalog.put_entry(make_entry(song, title=title))
        services.library.refresh()
    player = _make_player_mock()
    player._cfg = cfg
    player._library = selected.library
    bridge = PlayerBridge(
        player=player,
        sim=selected.sim,
        library=selected.library,
        ledger=selected.library.station_ledger("default"),
    )
    app = create_app(bridge, libraries=libraries)
    client = TestClient(app)
    assert client.get("/api/search?q=title").json()["results"][0]["title"] == "Other title"
    assert (
        client.get("/api/search?library=main&q=title").json()["results"][0]["title"] == "Main title"
    )
    assert client.get("/api/library/tracks").json()["results"][0]["title"] == "Other title"
    assert client.get("/api/search?library=missing").status_code == 404
    assert client.get("/api/search?station=missing").status_code == 404
    assert (
        client.get("/api/library/points", params={"path": str(song)}).json()["title"]
        == "Other title"
    )
    from autodj import timefmt

    monkeypatch.setattr(timefmt, "time_format", "24")
    definition = app.state.stations.default.paths.definition
    definition.parent.mkdir(parents=True, exist_ok=True)
    definition.write_text(json.dumps({"version": 1, "station": {"station": asdict(cfg.station)}}))
    response = patch_station_settings(client, {"time": {"time_format": "12"}})
    assert response.status_code == 200, response.text
    assert libraries.get("main").library.catalog.get_setting("time_format") == "12"
    assert selected.library.catalog.get_setting("time_format") is None


def test_create_and_rename_library_preserve_identity(libraries):
    selected = libraries.get("main")
    player = _make_player_mock()
    player._cfg = selected.cfg
    bridge = PlayerBridge(player=player, sim=selected.sim, library=selected.library)
    client = TestClient(create_app(bridge, libraries=libraries))
    created = client.post("/api/libraries", json={"id": "evening", "name": "Evening"})
    assert created.status_code == 200
    renamed = client.patch("/api/libraries/evening", json={"name": "After dark"})
    assert renamed.status_code == 200
    assert {"id": "evening", "name": "After dark"} in renamed.json()["libraries"]
    assert libraries.get("evening").cfg.index.library_dir.name == "evening"
    assert (
        client.post("/api/libraries", json={"id": "evening", "name": "Duplicate"}).status_code
        == 409
    )
    assert (
        client.post("/api/libraries", json={"id": "../outside", "name": "Bad"}).status_code == 400
    )


@pytest.mark.parametrize("library_id", [[], {}, None, 3, "../outside", "missing"])
def test_invalid_station_library_choice_does_not_change_binding(libraries, library_id):
    selected = libraries.get("main")
    player = _make_player_mock()
    player._cfg = selected.cfg
    bridge = PlayerBridge(player=player, sim=selected.sim, library=selected.library)
    client = TestClient(create_app(bridge, libraries=libraries))
    response = patch_station_settings(client, {"station": {"library_id": library_id}})
    assert response.status_code == 400, response.text
    assert selected.cfg.station.library_id == "main"
    assert bridge.library is selected.library


def test_tag_edits_target_one_catalog_and_shared_file_changes_arrive_on_rescan(libraries):
    """ADR-0023-07: catalog edits are isolated; file rewrites retain ADR 6 invalidation."""
    from dataclasses import replace

    from autodj.audio_meta import read_file_tags
    from autodj.indexer import file_fingerprint
    from tests.unit._tagged_audio import make_audio

    main = libraries.get("main").library
    other = libraries.get("other").library
    song = make_audio(libraries.root.library.music_dir, ".flac")
    size, fingerprint = file_fingerprint(song)
    for manager, title in ((main, "Main title"), (other, "Other title")):
        manager.catalog.add_source(song.parent, analysis="basic")
        manager.catalog.put_entry(
            replace(make_entry(song, title=title), size=size, fingerprint=fingerprint, length=0.1)
        )
        manager.catalog.set_points(str(song), {"intro": 0.05})
        manager.refresh()
    cfg = station_config(libraries.root, libraries.root)
    cfg.station.library_id = "other"
    selected = libraries.bind("default", cfg)
    player = _make_player_mock()
    player._cfg = cfg
    player._library = other
    bridge = PlayerBridge(
        player=player, sim=selected.sim, library=other, ledger=other.station_ledger("default")
    )
    client = TestClient(create_app(bridge, libraries=libraries))
    original_file = song.read_bytes()
    saved = client.post(
        "/api/library/tags",
        json={"path": str(song), "tags": {"title": "Catalog edit"}, "station": "default"},
    )
    assert saved.status_code == 200, saved.text
    assert other.catalog.record_for_path(song)["title"] == "Catalog edit"
    assert main.catalog.record_for_path(song)["title"] == "Main title"
    assert song.read_bytes() == original_file
    saved = client.post(
        "/api/library/tags",
        json={
            "path": str(song),
            "tags": {"title": "File edit"},
            "station": "default",
            "write_file": True,
        },
    )
    assert saved.status_code == 200, saved.text
    assert read_file_tags(song).title == "File edit"
    assert other.catalog.record_for_path(song)["title"] == "File edit"
    assert main.catalog.record_for_path(song)["title"] == "Main title"
    assert main.catalog.points(str(song))["intro"] == 0.05
    main.scan()
    assert main.catalog.record_for_path(song)["title"] == "File edit"
    assert other.catalog.record_for_path(song)["title"] == "File edit"
    # A rewrite outside this catalog invalidates file-bound cues under ADR 6.
    assert main.catalog.points(str(song))["intro"] is None
    assert other.catalog.points(str(song))["intro"] == 0.05
