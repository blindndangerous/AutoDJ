"""Clockwheel routes need a paired browser and keep strict request bodies."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from autodj.ads import Spot, new_id, spot_file_name
from autodj.catalog import LibraryCatalog
from autodj.server import PlayerBridge, create_app
from tests._layout import point_index

from ._helpers import _make_player_mock, _make_sim_mock

_SECRET = "pairing-secret-that-is-at-least-32-bytes"


def test_clockwheel_routes_need_pairing_and_strict_bodies(tmp_path):
    player = _make_player_mock()
    player._cfg.server.access_token = _SECRET
    point_index(player._cfg.index, tmp_path)
    player._cfg.index.index_dir = tmp_path
    player._cfg.presets = {}
    with LibraryCatalog(tmp_path / "library.sqlite") as catalog:
        bridge = PlayerBridge(player=player, sim=_make_sim_mock())
        bridge.library = SimpleNamespace(
            catalog=catalog,
            entry_for_path=lambda _path: None,
            entries_snapshot=lambda _kind="music": (),
        )
        app = create_app(bridge)
        app.state.security_policy.now = lambda: 1_000
        client = TestClient(app)
        assert client.get("/api/library/clock").status_code == 401
        assert client.post("/api/library/clock/active", json={"name": None}).status_code == 401

        code = app.state.request_pairing_code()[0]
        paired = client.post("/api/pair", json={"code": code, "device_name": "Desk"})
        assert paired.status_code == 200

        listed = client.get("/api/library/clock")
        assert listed.status_code == 200
        assert listed.json()["names"] == []
        extra = client.post("/api/library/clock/active", json={"name": None, "extra": True})
        assert extra.status_code == 422
        assert client.post("/api/library/clock/active", json={}).status_code == 422
        assert client.get("/api/library/clock/..%5Csecrets").status_code == 400
        assert client.get("/api/library/clock/CON").status_code == 400
        assert client.get("/api/library/clock/Nothing").status_code == 404
        counted = client.post(
            "/api/library/clock/check",
            json={
                "step": {
                    "type": "music",
                    "match": "all",
                    "conditions": [{"field": "genre", "operator": "is_not", "value": "Slow Jam"}],
                }
            },
        )
        assert counted.status_code == 200
        assert counted.json() == {"tracks": 0, "error": None}
        ad = {"type": "ad_break", "max_spots": 2, "max_seconds": 60, "advertiser": "Example Motors"}
        checked = client.post("/api/library/clock/check", json={"step": ad})
        assert checked.json() == {"tracks": None, "error": None}
        assert {"ad_advertisers", "ad_categories", "ads_reason"} <= listed.json().keys()
        station = app.state.stations.default
        station.ads.store._max_spots = lambda: 200
        for advertiser, category in (
            ("Example Motors", "Car dealers"),
            ("example motors", "car dealers"),
        ):
            key = new_id()
            station.ads.store.add_spot(
                key,
                Spot(
                    title="Sale",
                    advertiser=advertiser,
                    category=category,
                    file=spot_file_name(key, ".wav"),
                    cue_start=0,
                    cue_segue=1,
                    cue_end=1,
                    length_s=1,
                ),
            )
        ad_data = client.get("/api/library/clock").json()
        assert [name.casefold() for name in ad_data["ad_advertisers"]] == ["example motors"]
        assert [name.casefold() for name in ad_data["ad_categories"]] == ["car dealers"]
        assert ad_data["ads_reason"] == station.ads.refusal()
        created = client.post(
            "/api/library/clock",
            json={
                "version": 1,
                "name": "Ads",
                "resync_minutes": [0],
                "steps": [
                    ad,
                    {
                        "type": "music",
                        "conditions": [
                            {"field": "genre", "operator": "is_not", "value": "Slow Jam"}
                        ],
                        "match": "all",
                    },
                ],
            },
        )
        assert created.status_code == 200, created.text
        assert client.get("/api/library/clock/Ads").json()["steps"][0] == ad
        unknown = client.post(
            "/api/library/clock",
            json={"version": 1, "name": "X", "steps": [], "resync_minutes": [0], "path": "C:/"},
        )
        assert unknown.status_code == 422
