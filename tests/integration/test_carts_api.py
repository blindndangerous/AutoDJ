"""Cart wall routes: clips by ID or uploaded name, playback in every mode, events."""

from __future__ import annotations

import io
import json
import time
import wave
from collections.abc import Callable, Iterator
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from autodj.carts import CartError, CartSlot, StationCarts, install_carts
from autodj.scheduler import Action
from autodj.server import PlayerBridge
from autodj.station_routes import StationRouter, current_station, editable_station
from autodj.stations import StationPaths
from tests._layout import layout_index, point_index
from tests._server import create_app

from ._helpers import _make_player_mock, _make_sim_mock


def _wav(seconds: float = 0.1) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(44100)
        writer.writeframes(b"\0\0" * round(44100 * seconds))
    return output.getvalue()


class Rig:
    """The cart routes on a bare app, with a fake station and the events it publishes."""

    def __init__(self, root: Path, **bridge: Any) -> None:
        self.root = root
        self.events: list[dict[str, Any]] = []
        cfg = SimpleNamespace(
            index=layout_index(str(root)),
            playback=SimpleNamespace(liners_duck_db=-12.0),
            station=SimpleNamespace(liner_upload_max_mib=1, timezone=None),
        )
        player = SimpleNamespace(
            _cfg=cfg,
            _state=SimpleNamespace(is_paused=bridge.pop("paused", False)),
            bus=SimpleNamespace(playing=bridge.pop("bus_playing", True)),
        )
        self.bridge = SimpleNamespace(
            player=player,
            liner_scheduler=bridge.pop("scheduler", None),
            station=bridge.pop("station", None),
            library=bridge.pop("library", None),
        )
        assert not bridge

        async def publish(message: dict[str, Any]) -> None:
            self.events.append(message)

        carts = StationCarts(self.bridge, publish, lambda: root / "liners", root)
        self.store, self.action = carts.store, carts.action
        app = FastAPI()
        install_carts(StationRouter(app.router, ("/api",)))
        fake_station = SimpleNamespace(carts=carts, cfg=cfg, paths=StationPaths.default(cfg))
        app.dependency_overrides[current_station] = lambda: fake_station
        app.dependency_overrides[editable_station] = lambda: fake_station
        self.client = TestClient(app)

    def upload(self, name: str = "id.wav", content: bytes | None = None) -> Any:
        return self.client.post(
            "/api/carts/upload", files={"file": (name, _wav() if content is None else content)}
        )

    def assign(self, slot: int = 3, page: int = 1, **body: Any) -> Any:
        return self.client.put(
            f"/api/carts/pages/{page}/slots/{slot}",
            json={"name": "Station ID", "source": "upload", "clip": "id.wav", **body},
        )

    def play(self, slot: int = 3, page: int = 1) -> Any:
        return self.client.post(f"/api/carts/pages/{page}/slots/{slot}/play")

    def carts(self) -> list[dict[str, Any]]:
        return [event for event in self.events if event["type"] == "cart"]

    def wait_for(self, check: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 5
        while not check():
            assert time.monotonic() < deadline, self.events
            time.sleep(0.02)


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path)
    with made.client:
        yield made


def _server_rig(tmp_path: Path, **bridge: Any) -> Rig:
    scheduler = MagicMock()
    scheduler.decode.return_value = np.zeros((4410, 2), np.float32)
    return Rig(tmp_path, scheduler=scheduler, **bridge)


def test_browser_playback_fetches_the_clip_and_hears_start_and_end(rig: Rig) -> None:
    assert rig.upload().json() == {"name": "id.wav", "size": len(_wav())}
    assigned = rig.assign(mode="replace", duration=99)
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["slot"]["duration"] == pytest.approx(0.1)  # measured, not trusted
    listed = rig.client.get("/api/carts").json()
    assert listed["uploads"] == ["id.wav"]
    assert listed["wall"]["pages"][0]["slots"][2]["name"] == "Station ID"

    played = rig.play().json()
    assert played == {
        "label": "Cart 3, Station ID",
        "mode": "replace",
        "duck_db": -12.0,
        "duration": pytest.approx(0.1),
        "url": "/api/carts/uploads/id.wav",
    }
    clip = rig.client.get(played["url"])
    assert clip.content == _wav()
    assert clip.headers["content-type"] == "application/octet-stream"
    rig.wait_for(lambda: len(rig.carts()) == 2)
    assert rig.carts() == [
        {"type": "cart", "event": event, "page": 1, "slot": 3, "label": "Cart 3, Station ID"}
        for event in ("start", "end")
    ]
    # Nothing is playing now, so Stop says so and announces nothing.
    assert rig.client.post("/api/carts/stop").json() == {"stopped": False}
    assert len(rig.carts()) == 2


def test_a_new_cart_ends_the_last_and_stop_ends_it_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    with rig.client:
        rig.upload("long.wav", _wav(5))
        rig.upload()
        rig.assign(slot=1, name="Bed", clip="long.wav")
        rig.assign(slot=2, name="Sweep")
        rig.play(1)
        rig.play(2)
        rig.wait_for(lambda: len(rig.carts()) == 4)
        assert [(event["event"], event["label"]) for event in rig.carts()] == [
            ("start", "Cart 1, Bed"),
            ("end", "Cart 1, Bed"),
            ("start", "Cart 2, Sweep"),
            ("end", "Cart 2, Sweep"),
        ]
        rig.events.clear()
        rig.play(1)
        assert rig.client.post("/api/carts/stop").json() == {"stopped": True}
        time.sleep(0.2)
    # The stopped cart's end timer was cancelled: no "ended" after "stopped".
    assert [(event["event"], event["label"]) for event in rig.carts()] == [
        ("start", "Cart 1, Bed"),
        ("stop", "Cart 1, Bed"),
    ]


@pytest.mark.parametrize(
    ("mode", "duck_db"), [("duck", -12.0), ("over", 0.0), ("replace", float("-inf"))]
)
def test_server_mix_plays_through_the_liner_scheduler(
    tmp_path: Path, mode: str, duck_db: float
) -> None:
    rig = _server_rig(tmp_path)
    with rig.client:
        assert rig.assign(source="liner", clip="record-id", mode=mode).status_code == 200
        played = rig.play()
        assert played.status_code == 200, played.text
        assert played.json()["url"] is None
        scheduler = rig.bridge.liner_scheduler
        scheduler.decode.assert_called_with("record-id")
        audio, level = scheduler.play_cart.call_args.args
        assert len(audio) == 4410
        assert level == duck_db
        assert rig.client.post("/api/carts/stop").json() == {"stopped": True}
        scheduler.stop_cart.assert_called_once_with()
        # Nothing is playing now, so the bus is left alone.
        rig.client.post("/api/carts/stop")
        scheduler.stop_cart.assert_called_once_with()


@pytest.mark.parametrize(
    ("bridge", "message"),
    [
        ({"station": SimpleNamespace(state="idle")}, "Nobody is listening"),
        ({"station": SimpleNamespace(state="paused"), "paused": True}, "Playback is paused"),
        ({"paused": True}, "Playback is paused"),
        ({"bus_playing": False}, "Start playback"),
    ],
)
def test_server_mix_refuses_a_cart_nobody_would_hear(
    tmp_path: Path, bridge: dict[str, Any], message: str
) -> None:
    rig = _server_rig(tmp_path, **bridge)
    with rig.client:
        rig.assign(source="liner", clip="a.wav")
        refused = rig.play()
    assert refused.status_code == 409
    assert message in refused.json()["detail"]
    rig.bridge.liner_scheduler.play_cart.assert_not_called()
    assert rig.carts() == []


def test_a_clip_that_fails_to_decode_is_not_played(tmp_path: Path) -> None:
    rig = _server_rig(tmp_path)
    with rig.client:
        rig.assign(source="liner", clip="a.wav")
        rig.bridge.liner_scheduler.decode.side_effect = ValueError("gone")
        missing = rig.play()
    assert missing.status_code == 404
    assert missing.json()["detail"] == "The clip for cart 3 is missing or unreadable"
    assert rig.carts() == []


def test_uploads_are_checked_and_never_overwrite(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.bridge.player._cfg.station.liner_upload_max_mib = 1
    with rig.client:
        assert rig.upload("../id.wav").status_code == 400
        assert rig.upload("C:id.wav").status_code == 400
        assert rig.upload("notes.txt", b"text").status_code == 400
        assert rig.upload("big.wav", b"\0" * (1024 * 1024 + 1)).status_code == 413
        undecodable = rig.upload("bad.wav", b"not audio")
        assert undecodable.status_code == 400
        assert undecodable.json()["detail"] == "bad.wav is not audio AutoDJ can play"
        assert rig.upload("ok.wav", _wav(0.001)).status_code == 200
        again = rig.upload("ok.wav", _wav(0.002))
    assert again.status_code == 409
    assert (tmp_path / "carts" / "ok.wav").read_bytes() == _wav(0.001)
    assert sorted(path.name for path in (tmp_path / "carts").iterdir()) == ["ok.wav"]
    assert not (tmp_path.parent / "id.wav").exists()


def test_clip_names_never_reach_outside_their_folders(rig: Rig) -> None:
    (rig.root / "secret.wav").write_bytes(_wav())
    outside = rig.assign(source="liner", clip="../secret.wav")
    assert outside.status_code == 400
    assert rig.assign(clip="../secret.wav").status_code == 422
    assert rig.client.get("/api/carts/uploads/..%5Csecret.wav").status_code == 400
    assert rig.client.get("/api/carts/uploads/missing.wav").status_code == 404
    assert rig.client.delete("/api/carts/uploads/..%5Csecret.wav").status_code == 400
    assert rig.client.delete("/api/carts/uploads/missing.wav").status_code == 404
    assert (rig.root / "secret.wav").exists()


def test_a_clip_on_a_cart_cannot_be_deleted(rig: Rig) -> None:
    rig.upload()
    rig.assign(page=4, slot=12)
    refused = rig.client.delete("/api/carts/uploads/id.wav")
    assert refused.status_code == 409
    assert refused.json()["detail"] == "Clear the carts that play id.wav first"
    assert rig.client.delete("/api/carts/pages/4/slots/12").json() == {"cleared": True}
    assert rig.client.delete("/api/carts/uploads/id.wav").json() == {"deleted": "id.wav"}
    assert rig.client.get("/api/carts").json()["uploads"] == []


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("put", "/api/carts/pages/5/slots/1"),
        ("put", "/api/carts/pages/1/slots/13"),
        ("delete", "/api/carts/pages/0/slots/1"),
        ("delete", "/api/carts/pages/1/slots/13"),
        ("post", "/api/carts/pages/1/slots/4/play"),
        ("post", "/api/carts/pages/9/slots/1/play"),
        ("put", "/api/carts/active-page/5"),
        ("put", "/api/carts/pages/5/name"),
    ],
)
def test_pages_and_carts_outside_the_wall_are_not_found(rig: Rig, method: str, url: str) -> None:
    body = {"name": "X", "source": "upload", "clip": "id.wav"} if "slots" in url else {"name": "X"}
    response = rig.client.request(method, url, json=body if method == "put" else None)
    assert response.status_code == 404, response.text


def test_pages_are_renamed_and_chosen_for_every_open_page(rig: Rig) -> None:
    assert rig.client.put("/api/carts/pages/2/name", json={"name": " Jingles "}).json() == {
        "name": "Jingles"
    }
    clash = rig.client.put("/api/carts/pages/1/name", json={"name": "JINGLES"})
    assert clash.status_code == 409
    assert rig.client.put("/api/carts/pages/1/name", json={"name": "  "}).status_code == 422
    assert rig.client.put("/api/carts/active-page/2").json() == {"active_page": 2}
    saved = Rig(rig.root).store.wall
    assert (saved.active_page, saved.pages[1].name) == (2, "Jingles")
    assert {"type": "cart_changed"} in rig.events


def test_liners_are_offered_by_catalog_id_or_folder_name(tmp_path: Path) -> None:
    library_clip = tmp_path / "music-liners" / "station-id.wav"
    library_clip.parent.mkdir()
    library_clip.write_bytes(_wav(0.5))
    record = {"id": "rec-1", "kind": "liner", "path": str(library_clip)}
    library = SimpleNamespace(
        record_for_id=lambda key: record if key == "rec-1" else None,
        entry_for_path=lambda path: object(),
        entries_snapshot=lambda kind: [SimpleNamespace(path=str(library_clip))],
        catalog=SimpleNamespace(record_for_path=lambda path: record),
    )
    folder = tmp_path / "liners"
    folder.mkdir()
    for name in ("station-id.wav", "sweep.wav"):
        (folder / name).write_bytes(_wav(0.2))
    (folder / "notes.txt").write_text("not a liner", encoding="utf-8")
    rig = Rig(tmp_path, library=library)
    with rig.client:
        assert rig.client.get("/api/carts").json()["liners"] == [
            {"id": "rec-1", "name": "station-id.wav"},
            {"id": "sweep.wav", "name": "sweep.wav"},
        ]
        catalog = rig.assign(slot=1, source="liner", clip="rec-1")
        folder_liner = rig.assign(slot=2, source="liner", clip="sweep.wav")
        assert catalog.json()["slot"]["duration"] == pytest.approx(0.5)
        assert folder_liner.json()["slot"]["duration"] == pytest.approx(0.2)
        assert rig.play(1).json()["url"] == "/api/liners/file/rec-1"
        library_clip.unlink()
        assert rig.assign(slot=1, source="liner", clip="rec-1").status_code == 400


def test_play_cart_action_needs_the_server_mix(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    action: Action = rig.action
    assert action.metadata()["fields"] == [
        {
            "name": "page",
            "label": "Page",
            "type": "select",
            "options": [{"value": str(n), "label": str(n)} for n in range(1, 5)],
        },
        {
            "name": "slot",
            "label": "Cart",
            "type": "select",
            "options": [{"value": str(n), "label": str(n)} for n in range(1, 13)],
        },
    ]
    with rig.client, pytest.raises(ValueError, match="need server audio or stream mode"):
        rig.client.portal.call(action.handle, {"page": "1", "slot": "3"})

    rig = _server_rig(tmp_path)
    with rig.client:
        rig.assign(source="liner", clip="a.wav", mode="over")
        rig.client.portal.call(rig.action.handle, {"page": "1", "slot": "3"})
        with pytest.raises(CartError, match="Cart 4 is empty"):
            rig.client.portal.call(rig.action.handle, {"page": "1", "slot": "4"})
    assert rig.bridge.liner_scheduler.play_cart.call_args.args[1] == 0.0
    assert rig.carts()[0]["event"] == "start"


@pytest.fixture
def app_client(tmp_path: Path) -> Iterator[TestClient]:
    player = _make_player_mock()
    point_index(player._cfg.index, str(tmp_path / "active"))
    player._cfg.station.liner_upload_max_mib = 1
    with TestClient(create_app(PlayerBridge(player=player, sim=_make_sim_mock()))) as client:
        yield client


def test_backwards_cart_dates_name_their_field(app_client: TestClient) -> None:
    body = {"name": "Promo", "source": "upload", "clip": "promo.wav"}
    backwards = app_client.put(
        "/api/carts/pages/1/slots/3",
        json={**body, "not_before": "2026-12-26", "not_after": "2026-11-27"},
    )
    # The words and the field, so the page links the error to Do not play after.
    assert backwards.status_code == 422
    assert backwards.json() == {
        "detail": "Do not play after must be on or after the Not before date",
        "field": "not_after",
    }
    # Other refusals keep their list of problems, without the input echoed.
    other = app_client.put("/api/carts/pages/1/slots/3", json={**body, "mode": "loud"})
    assert other.status_code == 422
    assert isinstance(other.json()["detail"], list)
    assert "field" not in other.json()
    # Another endpoint's 422 is the list it always was.
    elsewhere = app_client.put("/api/time-checks", json={"enabled": "yes please"})
    assert elsewhere.status_code == 422
    assert isinstance(elsewhere.json()["detail"], list)
    assert "field" not in elsewhere.json()


def test_a_profile_shows_its_cart_page(app_client: TestClient) -> None:
    assert app_client.put("/api/carts/pages/2/name", json={"name": "Breakfast"}).status_code == 200
    assert app_client.put("/api/carts/active-page/1").status_code == 200
    created = app_client.post("/api/profiles", json={"name": "Morning"})
    assert created.status_code == 200, created.text
    view = app_client.get("/api/settings", params={"profile": "Morning"})
    assert view.status_code == 200, view.text
    saved = app_client.patch(
        "/api/settings",
        json={
            "revision": view.json()["revision"],
            "profile": "Morning",
            "changes": {"cart": {"page": "Breakfast"}},
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["settings"]["values"]["cart"] == {"page": "Breakfast"}
    applied = app_client.post("/api/profiles/Morning/apply")
    assert applied.status_code == 200, applied.text
    assert "cart" in applied.json()["applied"]
    assert app_client.get("/api/carts").json()["wall"]["active_page"] == 2

    app_client.put("/api/carts/active-page/1")
    app_client.put("/api/carts/pages/2/name", json={"name": "Drive"})
    applied = app_client.post("/api/profiles/Morning/apply")
    assert applied.status_code == 200, applied.text
    assert app_client.get("/api/carts").json()["wall"]["active_page"] == 2
    view = app_client.get("/api/settings", params={"profile": "Morning"}).json()
    assert view["values"]["cart"]["page"] == "Drive"


def test_the_app_caps_cart_uploads_and_registers_the_action(app_client: TestClient) -> None:
    too_big = 1024 * 1024 + 128 * 1024
    response = app_client.post("/api/carts/upload", files={"file": ("big.wav", b"\0" * too_big)})
    assert response.status_code == 413
    actions = {item["name"] for item in app_client.get("/api/schedule").json()["actions"]}
    assert "play_cart" in actions


def test_browser_playback_fetches_from_the_carts_own_station(tmp_path: Path) -> None:
    async def publish(_message: dict[str, Any]) -> None:
        return None

    def carts(*root: str) -> StationCarts:
        return StationCarts(SimpleNamespace(), publish, lambda: tmp_path, tmp_path, *root)

    upload = CartSlot(name="ID", source="upload", clip="id.wav")
    liner = CartSlot(name="Sweep", source="liner", clip="rec 1")
    assert carts().clip_url(upload) == "/api/carts/uploads/id.wav"
    named = carts("/api/stations/nightowl")
    assert named.clip_url(upload) == "/api/stations/nightowl/carts/uploads/id.wav"
    assert named.clip_url(liner) == "/api/stations/nightowl/liners/file/rec%201"
    # The default station's wall keeps the addresses the page always used.
    bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock())
    assert create_app(bridge).state.stations.default.carts.api_root == "/api"


def test_cart_dates_restrict_scheduled_but_not_manual_play(tmp_path: Path) -> None:
    rig = _server_rig(tmp_path)
    with rig.client:
        rig.upload()
        assigned = rig.assign(not_after="2000-01-01")
        assert assigned.status_code == 200, assigned.text
        assert assigned.json()["slot"]["not_after"] == "2000-01-01"
        listed = rig.client.get("/api/carts").json()
        assert listed["today"] > "2000-01-01"
        assert listed["timezone"]  # the page works out the station day from it
        assert listed["wall"]["pages"][0]["slots"][2]["not_after"] == "2000-01-01"
        assert rig.play().status_code == 200
        rig.bridge.liner_scheduler.play_cart.assert_called_once()
        rig.bridge.liner_scheduler.play_cart.reset_mock()
        # A scheduled run observes the date restriction.
        with pytest.raises(Exception, match="expired after 1 January 2000"):
            rig.client.portal.call(rig.action.handle, {"page": "1", "slot": "3"})
        rig.bridge.liner_scheduler.play_cart.assert_not_called()

        rig.assign(not_before="2099-12-31")
        assert rig.play().status_code == 200
        rig.bridge.liner_scheduler.play_cart.reset_mock()
        with pytest.raises(Exception, match="not playable before 31 December 2099"):
            rig.client.portal.call(rig.action.handle, {"page": "1", "slot": "3"})
        rig.bridge.liner_scheduler.play_cart.assert_not_called()
        assert rig.assign(not_before="2026-12-26", not_after="2026-11-27").status_code == 422
        rig.assign()
        assert rig.play().status_code == 200
        saved = json.loads((tmp_path / "carts.json").read_text(encoding="utf-8"))
        # Without dates the slot is written as before, so an older AutoDJ reads it.
        assert "not_after" not in saved["pages"][0]["slots"][2]


def test_scheduled_cart_dates_include_station_local_boundary_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime

    import autodj.clip_dates

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            instant = datetime(2027, 1, 1, 0, 30, tzinfo=UTC)
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(autodj.clip_dates, "datetime", FrozenDateTime)
    rig = _server_rig(tmp_path)
    rig.bridge.player._cfg.station.timezone = "America/Los_Angeles"
    with rig.client:
        rig.upload()
        # 00:30 UTC on 1 January is still 31 December at the station.
        rig.assign(not_before="2026-12-31", not_after="2026-12-31")
        rig.client.portal.call(rig.action.handle, {"page": "1", "slot": "3"})
        rig.bridge.liner_scheduler.play_cart.assert_called_once()


def test_manual_browser_cart_api_overrides_play_dates(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    with rig.client:
        rig.upload()
        rig.assign(not_after="2000-01-01")
        played = rig.play()
        assert played.status_code == 200
        assert played.json()["url"] == "/api/carts/uploads/id.wav"


def test_every_registered_liner_is_offered(tmp_path: Path) -> None:
    own = tmp_path / "liners" / "own.wav"
    theirs = tmp_path / "liners" / "nightowl" / "owl.wav"
    records = {
        str(own): {"id": "rec-own", "kind": "liner", "path": str(own)},
        str(theirs): {"id": "rec-owl", "kind": "liner", "path": str(theirs)},
    }
    library = SimpleNamespace(
        entries_snapshot=lambda kind: [SimpleNamespace(path=path) for path in records],
        catalog=SimpleNamespace(record_for_path=records.get),
    )

    async def publish(_message: dict[str, Any]) -> None:
        return None

    carts = StationCarts(
        SimpleNamespace(library=library),
        publish,
        lambda: tmp_path / "liners",
        tmp_path,
    )
    # Every registered liner is every station's, in whatever folder.
    assert carts.clip_choices()["liners"] == [
        {"id": "rec-own", "name": "own.wav"},
        {"id": "rec-owl", "name": "owl.wav"},
    ]


def test_closing_the_carts_cancels_the_end_timer_without_announcing(tmp_path: Path) -> None:
    import asyncio

    events: list[dict[str, Any]] = []

    async def publish(message: dict[str, Any]) -> None:
        events.append(message)

    async def scenario() -> None:
        carts = StationCarts(SimpleNamespace(), publish, lambda: tmp_path, tmp_path)
        carts._cart = (1, 3, "Cart 3, Station ID")

        async def ends() -> None:
            await asyncio.sleep(0.05)
            await carts.finish("end")

        timer = asyncio.create_task(ends())
        carts._timer = timer
        await carts.close()
        assert timer.cancelled()
        await asyncio.sleep(0.1)
        await carts.close()  # nothing playing: nothing to do

    asyncio.run(scenario())
    assert events == []
