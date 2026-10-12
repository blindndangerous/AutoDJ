"""Category routes: paired only, strict bodies, counts, and clockwheel steps that use them."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from autodj.catalog import LibraryCatalog
from autodj.categories import CategoryIndex
from autodj.server import PlayerBridge, create_app

from ._helpers import _make_player_mock, _make_sim_mock

_SECRET = "pairing-secret-that-is-at-least-32-bytes"


def _song(path, genre, year):
    return SimpleNamespace(
        path=str(path),
        title=path.stem,
        artist="Artist",
        album="Album",
        genre=genre,
        year=year,
        bpm=120.0,
        length=200.0,
        is_silent=False,
    )


def _client(tmp_path, catalog):
    player = _make_player_mock()
    player._cfg.server.access_token = _SECRET
    player._cfg.index.active_dir = tmp_path
    player._cfg.index.index_dir = tmp_path
    player._cfg.presets = {}
    player._cfg.library.beets_db = None
    player._cfg.library.music_dir = tmp_path / "music"
    player._cfg.library.playlists_folder = tmp_path / "playlists"
    songs = (
        _song(tmp_path / "music" / "a.flac", "Rock", 2003),
        _song(tmp_path / "music" / "b.flac", "Rock", 1994),
        _song(tmp_path / "music" / "c.flac", "Pop", 2008),
    )
    bridge = PlayerBridge(player=player, sim=_make_sim_mock())
    bridge.library = SimpleNamespace(
        catalog=catalog,
        entry_for_path=lambda _path: None,
        entries_snapshot=lambda kind="music": songs if kind == "music" else (),
        categories=CategoryIndex(player._cfg, lambda: songs),
    )
    app = create_app(bridge)
    app.state.security_policy.now = lambda: 1_000
    client = TestClient(app)
    return app, client, songs


def _pair(app, client):
    code = app.state.request_pairing_code()[0]
    assert client.post("/api/pair", json={"code": code, "device_name": "Desk"}).status_code == 200


def test_category_routes_need_pairing(tmp_path):
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        _app, client, _songs = _client(tmp_path, catalog)
        assert client.get("/api/categories").status_code == 401
        body = {"name": "A", "source": "query", "query": "genre:rock"}
        assert client.post("/api/categories", json=body).status_code == 401
        assert client.post("/api/categories/check", json=body).status_code == 401
        assert client.put("/api/categories/x", json=body).status_code == 401
        assert client.delete("/api/categories/x").status_code == 401


def test_categories_are_checked_saved_used_by_clockwheels_and_deleted(tmp_path):
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        app, client, songs = _client(tmp_path, catalog)
        _pair(app, client)
        (tmp_path / "playlists").mkdir()
        (tmp_path / "playlists" / "A.m3u8").write_text(
            f"#EXTM3U\n{songs[0].path}\n../music/c.flac\ngone.flac\n", encoding="utf-8"
        )
        listed = client.get("/api/categories").json()
        assert listed["categories"] == []
        assert listed["playlists"] == ["A.m3u8"]
        assert listed["beets"] is False

        decade = {
            "name": "Remembering the 2000s",
            "source": "query",
            "query": "original_year:2000..2009",
        }
        checked = client.post("/api/categories/check", json=decade).json()
        assert (checked["tracks"], checked["fallback"]) == (2, 2)
        assert checked["summary"] == (
            "2 songs; 2 use the release year because the first-release year is missing"
        )
        bad = client.post("/api/categories/check", json={**decade, "query": "year::20"})
        assert bad.status_code == 400
        assert bad.json()["field"] == "query"
        assert "regular expressions" in bad.json()["detail"]

        created = client.post("/api/categories", json=decade)
        assert created.status_code == 201
        decade_id = created.json()["category"]["id"]
        assert created.json()["category"]["tracks"] == 2
        rotation = client.post(
            "/api/categories", json={"name": "A", "source": "m3u", "playlist": "A.m3u8"}
        ).json()["category"]
        assert (rotation["tracks"], rotation["missing"]) == (2, 1)
        assert rotation["missing_examples"] == ["gone.flac"]

        refused = [
            ({"name": "a", "source": "query", "query": "genre:pop"}, "name", "already exists"),
            ({"name": "B", "source": "m3u", "playlist": "B.m3u"}, "playlist", "is missing"),
            ({"name": "C", "source": "query", "query": "mood:sad"}, "query", "field named mood"),
            ({"name": "D", "source": "query", "query": "x", "path": "/"}, "name", "Unknown"),
        ]
        for body, field, words in refused:
            answer = client.post("/api/categories", json=body)
            assert answer.status_code == 400, body
            assert answer.json()["field"] == field
            assert words in answer.json()["detail"]

        steps = [{"type": "category", "category": decade_id, "selection": "random"}]
        clock = {"version": 1, "name": "Throwback", "steps": steps, "resync_minutes": [0]}
        assert client.post("/api/library/clock", json=clock).status_code == 200
        unknown = {**clock, "name": "Other", "steps": [{"type": "category", "category": "nope"}]}
        missing = client.post("/api/library/clock", json=unknown)
        assert missing.status_code == 400
        assert "Step 1: its category is not saved" in missing.json()["detail"]
        snapshot = client.get("/api/library/clock").json()
        assert {"id": decade_id, "name": "Remembering the 2000s"} in snapshot["categories"]
        assert "category" in snapshot["types"]

        renamed = client.put(
            f"/api/categories/{decade_id}", json={**decade, "name": "2000s gold"}
        ).json()
        assert renamed["category"]["id"] == decade_id
        found = {c["name"]: c for c in renamed["categories"]}
        assert found["2000s gold"]["used_by"] == ["Throwback"]
        assert found["A"]["used_by"] == []
        assert client.put("/api/categories/nope", json=decade).status_code == 404

        left = client.delete(f"/api/categories/{decade_id}").json()
        assert [c["name"] for c in left["categories"]] == ["A"]
        assert client.delete(f"/api/categories/{decade_id}").status_code == 404
        # The clockwheel still loads; its step is skipped when the station reaches it.
        assert client.get("/api/library/clock/Throwback").status_code == 200
