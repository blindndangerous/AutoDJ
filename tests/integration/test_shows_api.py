"""Paired show routes, the Start show action, and the show in the public feed."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from fastapi.testclient import TestClient

from autodj import server
from autodj.catalog import LibraryCatalog, normalize_path
from autodj.clockwheel_store import ClockwheelStore
from autodj.repeat_rules import TrackKeyCache
from autodj.server import PlayerBridge, create_app
from autodj.show_runtime import RENDER_WAIT_S

from ._helpers import _make_entry, _make_player_mock, _make_sim_mock

_SECRET = "pairing-secret-that-is-at-least-32-bytes"


def _songs() -> list:
    songs = []
    for n in range(12):
        entry = _make_entry(n)
        entry.artist, entry.album, entry.title = f"Artist {n}", f"Album {n}", f"Song {n}"
        entry.year = 1995 + n
        entry.genre = "Rock" if n % 2 else "Pop"
        songs.append(entry)
    return songs


@dataclass
class Rig:
    tmp_path: Path
    player: MagicMock
    bridge: PlayerBridge
    client: TestClient
    catalog: LibraryCatalog | None = None
    submitted: list = field(default_factory=list)

    def add(self, **body) -> dict:
        reply = self.client.post("/api/shows", json={"name": "Remembering the 2000s", **body})
        assert reply.status_code == 201, reply.text
        return reply.json()["show"]


def _rig(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    library: bool = False,
    dry_run: bool = False,
    bus: bool = False,
) -> Rig:
    songs = _songs()
    player = _make_player_mock()
    player._cfg.index.active_dir = tmp_path
    player._cfg.index.index_dir = tmp_path
    player._cfg.library.beets_db = None
    player._cfg.server.station_timezone = "UTC"
    player._dry_run = dry_run
    player.bus = MagicMock() if bus else None
    player.playable_entries.side_effect = lambda: tuple(songs)
    player._track_keys.side_effect = TrackKeyCache
    player.show = None
    bridge = PlayerBridge(player=player, sim=_make_sim_mock(songs))
    catalog = None
    if library:
        catalog = LibraryCatalog(tmp_path / "library.sqlite")
        liners = tmp_path / "liners"
        (liners / "2000s").mkdir(parents=True)
        source = catalog.add_source(liners, "liner")
        entries = []
        for name in ("intro.wav", "2000s/id.wav"):
            entry = _make_entry(50 + len(entries))
            entry.path = str(liners / name)
            Path(entry.path).write_bytes(b"")
            catalog.put_entry(entry, "liner")
            entries.append(catalog.entry_for_path(entry.path))
        bridge.library = SimpleNamespace(
            catalog=catalog,
            entries_snapshot=lambda kind="music": tuple(entries) if kind == "liner" else (),
            entry_for_path=catalog.entry_for_path,
        )
        bridge.library.source = source
    submitted: list = []
    monkeypatch.setattr(
        server.WebhookWorker, "submit", lambda _self, payload, index=None: submitted.append(payload)
    )
    client = TestClient(
        create_app(bridge), headers={"Host": "testserver", "Origin": "http://testserver"}
    )
    return Rig(tmp_path, player, bridge, client, catalog, submitted)


@pytest.fixture
def make_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator:
    made: list[Rig] = []

    def make(**options) -> Rig:
        made.append(_rig(tmp_path, monkeypatch, **options))
        return made[-1]

    yield make
    for each in made:
        if each.catalog is not None:
            each.catalog.close()


@pytest.fixture
def rig(make_rig) -> Rig:
    return make_rig()


def test_show_routes_need_a_paired_browser(tmp_path: Path) -> None:
    player = _make_player_mock()
    player._cfg.server.access_token = _SECRET
    player._cfg.index.active_dir = tmp_path
    player._cfg.index.index_dir = tmp_path
    client = TestClient(create_app(PlayerBridge(player=player, sim=_make_sim_mock())))
    assert client.get("/api/shows").status_code in (401, 403)
    assert client.post("/api/shows", json={"name": "A"}).status_code in (401, 403)
    assert client.post("/api/shows/check", json={}).status_code in (401, 403)
    assert client.post("/api/shows/stop").status_code in (401, 403)


def test_add_list_edit_and_delete_a_show(rig: Rig) -> None:
    listed = rig.client.get("/api/shows").json()
    assert listed["shows"] == [] and listed["active"] is None
    assert listed["options"]["genres"] == ["Pop", "Rock"]
    reply = rig.client.post(
        "/api/shows",
        json={"name": "Remembering the 2000s", "year_from": 2000, "year_to": 2009},
    )
    assert reply.status_code == 201
    created = reply.json()
    assert created["check"]["count_text"] == "7 songs match"
    show = created["show"]
    assert show["summary"].startswith("First released 2000 to 2009.")
    assert [item["id"] for item in rig.client.get("/api/shows").json()["shows"]] == [show["id"]]
    edited = rig.client.put(f"/api/shows/{show['id']}", json={"name": "Nineties", "year_to": 1999})
    assert edited.status_code == 200
    assert edited.json()["show"]["name"] == "Nineties"
    assert rig.client.put("/api/shows/" + "f" * 32, json={"name": "X"}).status_code == 404
    deleted = rig.client.delete(f"/api/shows/{show['id']}")
    assert deleted.json() == {"deleted": show["id"], "name": "Nineties"}
    assert rig.client.delete(f"/api/shows/{show['id']}").status_code == 404
    saved = json.loads((rig.tmp_path / "shows.json").read_text(encoding="utf-8"))
    assert saved == {"schema_version": 1, "shows": [], "active": None}


@pytest.mark.parametrize(
    ("body", "field", "words"),
    [
        ({"name": ""}, "name", "Enter a name"),
        ({"name": "A", "query": "genre::rock"}, "query", "regular expressions"),
        ({"name": "A", "query": "mood:happy"}, "query", "field named mood"),
        ({"name": "A", "liner_path": "C:/secret"}, "liner_path", "Unknown field"),
        ({"name": "A", "intro": "../../etc/passwd"}, "intro", "voice liners"),
        ({"name": "A", "clockwheel": "Nope"}, "clockwheel", "no clockwheel named Nope"),
    ],
)
def test_a_bad_show_is_refused_naming_the_field(rig: Rig, body, field, words) -> None:
    reply = rig.client.post("/api/shows", json=body)
    assert reply.status_code == 400
    assert reply.json()["field"] == field
    assert words in reply.json()["detail"]


def test_check_counts_songs_and_warns_in_words(rig: Rig) -> None:
    reply = rig.client.post("/api/shows/check", json={"year_from": 2000, "minutes": 120})
    assert reply.status_code == 200
    report = reply.json()
    assert report["count"] == 7
    assert report["fallback_text"] == (
        "7 use the release year because the first-release year is missing"
    )
    # Two hours of three-minute songs is 40; the default track rule holds 500.
    assert report["warning"] == "about 40 songs needed; only 7 fit your track rule"
    bad = rig.client.post("/api/shows/check", json={"year_from": 2009, "year_to": 2000})
    assert bad.status_code == 400 and bad.json()["field"] == "year_to"
    extra = rig.client.post("/api/shows/check", json={"name": "x"})
    assert extra.status_code == 400 and extra.json()["detail"] == "Unknown field name"
    minutes = rig.client.post("/api/shows/check", json={"minutes": 0})
    assert minutes.json()["field"] == "minutes"


def test_start_now_stop_and_the_public_feed(rig: Rig) -> None:
    show = rig.add(year_from=2000, year_to=2009)
    assert rig.client.post("/api/shows/stop").status_code == 409
    assert rig.client.post(f"/api/shows/{show['id']}/start", json={"minutes": 0}).status_code == 422
    assert (
        rig.client.post("/api/shows/" + "f" * 32 + "/start", json={"minutes": 5}).status_code == 404
    )
    reply = rig.client.post(f"/api/shows/{show['id']}/start", json={"minutes": 90})
    assert reply.status_code == 200
    active = reply.json()
    assert active["name"] == "Remembering the 2000s" and active["count"] == 7
    assert rig.player.show is not None
    assert rig.player.show.paths == {song.path for song in _songs()[5:]}
    # Neither browser mode nor a bus here: nothing to pick again.
    rig.player.refresh_render_ahead.assert_not_called()
    feed = rig.client.get("/api/nowplaying").json()
    assert feed["show"] == {"name": "Remembering the 2000s", "ends_at": active["ends_at"]}
    page = rig.client.get("/status").text
    assert f"On air: Remembering the 2000s, until {active['ends_at'][11:16]}." in page
    assert rig.bridge.get_state()["show"]["name"] == "Remembering the 2000s"
    assert rig.submitted[-1]["event"] == "show_started"
    assert rig.submitted[-1]["show"]["name"] == "Remembering the 2000s"
    stopped = rig.client.post("/api/shows/stop")
    assert stopped.json() == {"stopped": show["id"], "name": "Remembering the 2000s"}
    assert rig.player.show is None
    assert rig.client.get("/api/nowplaying").json()["show"] is None
    assert 'id="status-show" hidden' in rig.client.get("/status").text


def test_a_show_that_matches_nothing_cannot_start(rig: Rig) -> None:
    show = rig.add(name="Future", year_from=2090)
    reply = rig.client.post(f"/api/shows/{show['id']}/start", json={"minutes": 30})
    assert reply.status_code == 400
    assert reply.json() == {"detail": "No song matches Future, so it cannot start", "field": ""}


def test_browser_mode_picks_the_next_song_again(make_rig, tmp_path, monkeypatch) -> None:
    rig = make_rig(dry_run=True)
    picked = []
    monkeypatch.setattr(rig.bridge, "repick_next", lambda *_args: picked.append(rig.player.show))
    show = rig.add(year_from=2000, start="hard")
    skipped = []
    monkeypatch.setattr(rig.bridge, "skip", lambda: skipped.append(True))
    rig.client.post(f"/api/shows/{show['id']}/start", json={"minutes": 10})
    assert picked and picked[0] is not None
    assert skipped == [True]
    rig.player.wait_for_render.assert_not_called()


def test_server_mixing_renders_again_and_a_hard_start_waits_then_fades(
    make_rig, tmp_path, monkeypatch
) -> None:
    rig = make_rig(bus=True)
    show = rig.add(year_from=2000, start="hard", end="hard")
    rig.client.post(f"/api/shows/{show['id']}/start", json={"minutes": 10})
    rig.player.refresh_render_ahead.assert_called()
    rig.player.wait_for_render.assert_called_with(RENDER_WAIT_S)
    rig.player.bus.skip.assert_called_once()
    rig.client.post("/api/shows/stop")
    assert rig.player.bus.skip.call_count == 2


def test_feed_failures_never_stop_a_show(make_rig, tmp_path, monkeypatch, caplog) -> None:
    rig = make_rig()
    show = rig.add(year_from=2000)

    def broken(_self):
        raise RuntimeError("feed down")

    monkeypatch.setattr(server.NowPlaying, "refresh", broken)
    assert (
        rig.client.post(f"/api/shows/{show['id']}/start", json={"minutes": 10}).status_code == 200
    )
    assert rig.client.post("/api/shows/stop").status_code == 200
    assert "Could not publish the show start" in caplog.text
    assert "Could not refresh now-playing state" in caplog.text


# ---------------------------------------------------------------------------
# The Schedule's Start show action
# ---------------------------------------------------------------------------


def test_a_scheduled_show_starts_and_ends_at_the_time_given(rig: Rig) -> None:
    show = rig.add(year_from=2000)
    actions = {item["name"]: item for item in rig.client.get("/api/schedule").json()["actions"]}
    start = actions["show_start"]
    assert start["own_timing"] is True
    assert start["fields"] == [
        {
            "name": "show",
            "label": "Show",
            "type": "select",
            "options": [{"value": show["id"], "label": "Remembering the 2000s"}],
        },
        {"name": "until", "label": "Ends at", "type": "time"},
    ]
    until = (datetime.now(UTC) + timedelta(hours=2)).strftime("%H:%M")
    event = {
        "name": "Saturday night",
        "action": "show_start",
        "params": {"show": show["id"], "until": until},
        "repeat": "daily",
        "time": "20:00",
    }
    bad = rig.client.post(
        "/api/schedule", json={**event, "params": {"show": show["id"], "until": "25:00"}}
    )
    assert bad.status_code == 400
    assert bad.json() == {"detail": "Ends at must be a time such as 21:00", "field": "params.until"}
    unknown = rig.client.post(
        "/api/schedule", json={**event, "params": {"show": "x", "until": until}}
    )
    assert unknown.json()["field"] == "params.show"
    created = rig.client.post("/api/schedule", json=event)
    assert created.status_code == 201, created.text
    assert created.json()["action_text"] == f"Start show: Remembering the 2000s, {until}"
    ran = rig.client.post(f"/api/schedule/{created.json()['id']}/run").json()
    assert ran["result"] == "ok", ran
    state = rig.bridge.show_state()
    assert state is not None and state["ends_at"][11:16] == until
    rig.client.delete(f"/api/shows/{show['id']}")
    failed = rig.client.post(f"/api/schedule/{created.json()['id']}/run").json()
    assert (failed["result"], failed["message"]) == ("error", "That show no longer exists")


def test_a_hard_event_leaves_the_fade_to_the_show(make_rig, tmp_path, monkeypatch) -> None:
    rig = make_rig(bus=True)
    show = rig.add(year_from=2000)  # a soft start
    until = (datetime.now(UTC) + timedelta(hours=1)).strftime("%H:%M")
    event = {
        "name": "Hard event",
        "action": "show_start",
        "params": {"show": show["id"], "until": until},
        "repeat": "daily",
        "time": "20:00",
        "timing": "hard",
    }
    created = rig.client.post("/api/schedule", json=event).json()
    ran = rig.client.post(f"/api/schedule/{created['id']}/run").json()
    assert ran["result"] == "ok"
    assert rig.bridge.show_state() is not None
    rig.player.bus.skip.assert_not_called()


# ---------------------------------------------------------------------------
# Clockwheels, liners and the liner folder, with a library
# ---------------------------------------------------------------------------


def _clockwheel(tmp_path: Path, name: str) -> None:
    ClockwheelStore(tmp_path / "clockwheels").save(
        {
            "version": 1,
            "name": name,
            "steps": [{"type": "music", "source": "music1"}],
            "resync_minutes": [0],
        }
    )


def test_a_show_clockwheel_switches_the_station_and_back(make_rig, tmp_path, monkeypatch) -> None:
    rig = make_rig(library=True)
    _clockwheel(tmp_path, "Decades")
    _clockwheel(tmp_path, "Station")
    assert rig.catalog is not None
    rig.catalog.set_setting("clock", {"name": "Station", "cursor": 4})
    rig.player._cfg.playback.pick_mode = "similarity"
    show = rig.add(year_from=2000, clockwheel="Decades")
    started = rig.client.post(f"/api/shows/{show['id']}/start", json={"minutes": 10})
    assert started.status_code == 200, started.text
    assert rig.catalog.get_setting("clock") == {"name": "Decades", "cursor": 0}
    assert rig.player._cfg.playback.pick_mode == "clock"
    rig.client.post("/api/shows/stop")
    assert rig.catalog.get_setting("clock") == {"name": "Station", "cursor": 4}
    assert rig.player._cfg.playback.pick_mode == "similarity"
    assert rig.player._cfg.playback.transition_mode == "full_intro_outro"


def test_a_station_clockwheel_that_is_gone_is_logged_and_picking_comes_back(
    make_rig, tmp_path, monkeypatch, caplog
) -> None:
    rig = make_rig(library=True)
    _clockwheel(tmp_path, "Decades")
    assert rig.catalog is not None
    rig.catalog.set_setting("clock", {"name": "Deleted", "cursor": "x"})
    show = rig.add(year_from=2000, clockwheel="Decades")
    rig.client.post(f"/api/shows/{show['id']}/start", json={"minutes": 10})
    rig.client.post("/api/shows/stop")
    assert "could not come back" in caplog.text
    assert rig.player._cfg.playback.pick_mode == "similarity"


def test_a_clockwheel_that_cannot_play_refuses_the_start(make_rig, tmp_path, monkeypatch) -> None:
    rig = make_rig(library=True)
    _clockwheel(tmp_path, "Decades")
    rig.player._clock = None
    rig.player._clock_status = "Step 1 is off"
    show = rig.add(year_from=2000, clockwheel="Decades")
    reply = rig.client.post(f"/api/shows/{show['id']}/start", json={"minutes": 10})
    assert reply.status_code == 400
    assert reply.json() == {
        "detail": "The clockwheel cannot play: Step 1 is off",
        "field": "clockwheel",
    }


def test_a_clockwheel_needs_the_library(rig: Rig) -> None:
    _clockwheel(rig.tmp_path, "Decades")
    show = rig.add(year_from=2000, clockwheel="Decades")
    reply = rig.client.post(f"/api/shows/{show['id']}/start", json={"minutes": 10})
    assert reply.json() == {"detail": "Folder library is not attached", "field": "clockwheel"}


def test_liners_come_by_library_id_and_the_show_folder_replaces_the_automatic_ones(
    make_rig, tmp_path, monkeypatch
) -> None:
    rig = make_rig(library=True)
    assert rig.catalog is not None
    options = rig.client.get("/api/shows").json()["options"]
    liners = {item["name"]: item["id"] for item in options["liners"]}
    assert set(liners) == {"intro.wav", "id.wav"}
    source = options["liner_folders"][0]["id"]
    show = rig.add(
        year_from=2000,
        intro=liners["intro.wav"],
        outro=liners["id.wav"],
        liner_folder={"source": source, "folder": "2000s"},
    )
    every = [name for _key, name in server._catalog_liners(rig.bridge)]
    assert len(every) == 2
    outside = server._registered_liners(rig.bridge)
    assert [path.name for _key, path in outside] == ["intro.wav"]
    rig.client.post(f"/api/shows/{show['id']}/start", json={"minutes": 10})
    inside = server._registered_liners(rig.bridge)
    assert [path.name for _key, path in inside] == ["id.wav"]
    monkeypatch.setattr(
        server, "_processed_liner", lambda _bridge, path: np.ones((3, 2)) * len(path.name)
    )
    shows = rig.bridge.show_liners.__self__  # the ShowManager
    assert shows.due_audio() is not None  # the intro, decoded by its library ID
    with shows._clip_lock:
        shows._pending_clip = "f" * 32
    assert shows.due_audio() is None  # not a registered liner


def test_without_a_library_there_are_no_liners(rig: Rig) -> None:
    assert server._catalog_liners(rig.bridge) == []
    options = rig.client.get("/api/shows").json()["options"]
    assert options["liners"] == [] and options["liner_folders"] == []


def test_the_lifespan_carries_on_with_the_show_and_plays_the_intro(
    make_rig, tmp_path, monkeypatch
) -> None:
    rig = make_rig()
    show = rig.add(year_from=2000)
    rig.client.post(f"/api/shows/{show['id']}/start", json={"minutes": 30})
    rig.player.show = None
    rig.player.bus = MagicMock()
    rig.player.bus.playing = True
    rig.player._cfg.playback.liners_folder = str(tmp_path)
    bridge = PlayerBridge(player=rig.player, sim=_make_sim_mock(_songs()))
    with TestClient(create_app(bridge, server_audio=True)):
        assert rig.player.show is not None  # carried on after the "restart"
        manager = bridge.show_liners.__self__
        assert bridge.liner_scheduler.announcement() is None  # no intro again
        with manager._clip_lock:
            manager._pending_clip = "f" * 32
        assert bridge.liner_scheduler.announcement() is None
        manager.doc.active = manager.doc.active.model_copy(
            update={"ends_at": datetime.now(UTC) - timedelta(seconds=1)}
        )
        deadline = time.monotonic() + 5
        while manager.state() is not None:
            assert time.monotonic() < deadline, "the show did not end at its end time"
            time.sleep(0.05)
    assert rig.player.show is None


def test_backup_keeps_the_shows_file() -> None:
    from autodj.backup import INDEX_FILES

    assert "shows.json" in INDEX_FILES


def test_paths_in_show_folders_are_catalog_paths(tmp_path) -> None:
    assert normalize_path(tmp_path / "a" / ".." / "b") == normalize_path(tmp_path / "b")


def test_an_idle_bus_fades_at_once_without_waiting_for_a_render(make_rig) -> None:
    rig = make_rig(bus=True)
    rig.player.bus.playing = False
    show = rig.add(year_from=2000, start="hard")
    rig.client.post(f"/api/shows/{show['id']}/start", json={"minutes": 10})
    rig.player.wait_for_render.assert_not_called()
    rig.player.bus.skip.assert_called_once()


def test_a_show_that_cannot_carry_on_never_stops_the_server(make_rig, monkeypatch, caplog) -> None:
    from autodj.show_runtime import ShowManager

    rig = make_rig()

    async def broken(_self) -> None:
        raise RuntimeError("disk gone")

    monkeypatch.setattr(ShowManager, "start_up", broken)
    with TestClient(create_app(rig.bridge)) as client:
        assert client.get("/api/shows").status_code == 200
    assert "Could not carry on with the show on air" in caplog.text
