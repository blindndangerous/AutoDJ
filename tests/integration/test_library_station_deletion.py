"""Station deletion preserves music ownership across named libraries."""

import json

import pytest
from fastapi.testclient import TestClient

from autodj._bridge import PlayerBridge
from autodj.stations import StationPaths
from tests._server import create_app
from tests.integration._helpers import _make_player_mock
from tests.unit.test_library import make_entry
from tests.unit.test_library_runtime import libraries as libraries


@pytest.fixture(autouse=True)
def browser_origin(libraries):
    libraries.root.server.allowed_hosts = ["testserver"]
    libraries.root.server.allowed_origins = ["http://testserver"]


def _client(libraries):
    main = libraries.get("main")
    main.cfg.config_path.parent.mkdir(parents=True, exist_ok=True)
    main.cfg.config_path.write_text(
        "[index]\n"
        f"index_dir = {json.dumps(str(main.cfg.index.index_dir))}\n"
        "[library]\n"
        f"music_dir = {json.dumps(str(main.cfg.library.music_dir))}\n",
        encoding="utf-8",
    )
    player = _make_player_mock()
    player._cfg = main.cfg
    bridge = PlayerBridge(player=player, sim=main.sim, library=main.library)
    return TestClient(create_app(bridge, libraries=libraries))


def _create_archived_station(client, slug="nightowl"):
    created = client.post(
        "/api/stations", json={"slug": slug, "name": "Night Owl", "source": "default"}
    )
    assert created.status_code == 201, created.text
    archived = client.post(f"/api/stations/{slug}/archive")
    assert archived.status_code == 200, archived.text


def _delete(client, slug="nightowl"):
    return client.request("DELETE", f"/api/stations/{slug}", json={"confirmation": "DELETE"})


def test_archived_station_deletion_cleans_owned_liner_sources_in_every_library(libraries):
    client = _client(libraries)
    _create_archived_station(client)
    paths = StationPaths.for_station(libraries.root, "nightowl")
    paths.liners.mkdir(parents=True, exist_ok=True)

    song = libraries.root.library.music_dir / "shared-song.flac"
    song.parent.mkdir(parents=True, exist_ok=True)
    song.write_bytes(b"music remains owned by each catalog")
    for library_id in ("main", "other"):
        manager = libraries.get(library_id).library
        manager.catalog.add_source(song.parent)
        manager.catalog.put_entry(make_entry(song, title=f"{library_id} song"))
        manager.catalog.set_points(song, {"intro": 4.0})
        manager.catalog.add_source(paths.liners, kind="liner", recursive=False)

    response = _delete(client)
    assert response.status_code == 200, response.text
    assert response.json()["cleanup_pending"] is False
    assert song.read_bytes() == b"music remains owned by each catalog"
    for library_id in ("main", "other"):
        catalog = libraries.get(library_id).library.catalog
        assert catalog.entry_for_path(song).title == f"{library_id} song"
        assert catalog.points(song)["intro"] == 4.0
        assert not any(source["path"] == str(paths.liners) for source in catalog.sources("liner"))


def test_music_source_in_secondary_library_blocks_overlapping_station_delete(libraries):
    client = _client(libraries)
    _create_archived_station(client)
    paths = StationPaths.for_station(libraries.root, "nightowl")
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    (paths.state_dir / "owned-state.txt").write_text("keep", encoding="utf-8")
    libraries.get("other").library.catalog.add_source(paths.state_dir, kind="music")

    response = _delete(client)
    assert response.status_code == 409, response.text
    assert "protected" in response.json()["detail"].lower()
    assert (paths.state_dir / "owned-state.txt").read_text(encoding="utf-8") == "keep"


def test_busy_secondary_scan_blocks_delete_and_releases_main_scan_lock(libraries):
    client = _client(libraries)
    _create_archived_station(client)
    main_lock = libraries.get("main").library._scan_lock
    secondary_lock = libraries.get("other").library._scan_lock
    assert secondary_lock.acquire(blocking=False)
    try:
        response = _delete(client)
        assert response.status_code == 409, response.text
        assert "scans must finish" in response.json()["detail"].lower()
        assert main_lock.acquire(blocking=False), "the earlier library scan lock leaked"
        main_lock.release()
    finally:
        secondary_lock.release()
