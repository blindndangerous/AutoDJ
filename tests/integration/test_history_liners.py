"""GET /api/history with and without liners (#601), and the play log export's choice."""

from __future__ import annotations

import csv
import io
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from autodj.catalog import LibraryCatalog
from autodj.config import ServerConfig
from autodj.indexer import IndexEntry
from autodj.server import PlayerBridge
from autodj.station_store import StationStore
from tests._server import create_app
from tests.integration.test_multi_station import rig  # noqa: F401

from ._helpers import _make_player_mock, _make_sim_mock

T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _row(index: int, kind: str = "song", **fields) -> dict:
    row = {
        "event_id": f"{kind}-{index}",
        "path": f"/music/{index}.flac",
        "title": f"{kind} {index}",
        "artist": "Artist",
        "duration": 60.0,
        "played_at": (T0 + timedelta(minutes=index)).isoformat(),
    }
    if kind != "song":
        row["kind"] = kind
    return {**row, **fields}


def _client(rows: list[dict]) -> TestClient:
    bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock())
    bridge._play_history = deque(rows, maxlen=2000)
    return TestClient(create_app(bridge))


def _mixed(songs: int) -> list[dict]:
    """*songs* songs, each followed by a liner; every third also by a matched intro."""
    rows: list[dict] = []
    for index in range(songs):
        rows.append(_row(index * 3))
        rows.append(_row(index * 3 + 1, "liner", path=None))
        if index % 3 == 0:
            rows.append(_row(index * 3 + 2, "matched_intro", path=None))
    return rows


def test_history_shows_music_only_by_default() -> None:
    rows = [
        _row(0),
        _row(1, "liner", path="/liners/id.wav"),
        # A liner file played as a track, restored from the station database.
        _row(2, track_kind="liner", kind="song"),
        _row(3, "live", path=None, ended_at=None),
        _row(4, "ad", path=None),
        _row(5, track_kind="music", kind="song"),
    ]
    data = _client(rows).get("/api/history").json()
    assert [item["event_id"] for item in data["items"]] == ["song-5", "live-3", "song-0"]
    assert [item["kind"] for item in data["items"]] == ["song", "live", "song"]
    assert data["total"] == 3


def test_history_with_liners_shows_everything_that_aired_with_its_kind() -> None:
    rows = [
        _row(0),
        _row(1, "liner", path="/liners/id.wav"),
        _row(2, track_kind="liner", kind="song"),
        _row(3, "time_check", path=None),
        _row(4, "ad", path=None),
    ]
    data = _client(rows).get("/api/history?liners=true").json()
    assert [(item["title"], item["kind"]) for item in data["items"]] == [
        ("ad 4", "ad"),
        ("time_check 3", "time_check"),
        ("song 2", "liner"),
        ("liner 1", "liner"),
        ("song 0", "song"),
    ]
    assert data["total"] == 5


def test_pages_are_counted_for_the_view_shown() -> None:
    client = _client(_mixed(30))  # 30 songs, 30 liners, 10 intros
    music = client.get("/api/history?per_page=10").json()
    assert (music["total"], music["pages"]) == (30, 3)
    everything = client.get("/api/history?per_page=10&liners=1").json()
    assert (everything["total"], everything["pages"]) == (70, 7)


def test_showing_liners_finds_the_page_that_holds_the_first_song_shown() -> None:
    client = _client(_mixed(30))
    music = client.get("/api/history?per_page=10&page=2").json()
    anchor = music["items"][0]["event_id"]
    assert anchor == "song-57"
    shown = client.get(f"/api/history?per_page=10&page=2&liners=1&anchor={anchor}").json()
    ids = [item["event_id"] for item in shown["items"]]
    assert anchor in ids
    assert shown["page"] == 3


def test_hiding_liners_finds_the_page_with_the_next_older_song() -> None:
    client = _client(_mixed(30))
    everything = client.get("/api/history?per_page=10&page=4&liners=1").json()
    first = everything["items"][0]
    assert first["kind"] != "song"
    hidden = client.get(f"/api/history?per_page=10&page=4&anchor={first['event_id']}").json()
    older_song = next(item["event_id"] for item in everything["items"] if item["kind"] == "song")
    assert older_song == "song-48"
    assert older_song in [item["event_id"] for item in hidden["items"]]
    assert hidden["page"] == 2


def test_an_unknown_anchor_keeps_the_page_asked_for() -> None:
    client = _client(_mixed(30))
    data = client.get("/api/history?per_page=10&page=2&anchor=gone").json()
    assert data["page"] == 2


def test_an_anchor_older_than_the_view_gives_its_last_page() -> None:
    rows = [_row(0, "liner", path=None), *(_row(index) for index in range(1, 25))]
    data = _client(rows).get("/api/history?per_page=10&page=1&anchor=liner-0").json()
    assert data["page"] == data["pages"] == 3


def test_each_view_keeps_the_latest_500_rows() -> None:
    client = _client(_mixed(400))  # 400 songs, 400 liners, 134 intros
    music = client.get("/api/history?per_page=500").json()
    assert music["total"] == 400
    everything = client.get("/api/history?per_page=500&liners=1").json()
    assert everything["total"] == 500
    assert everything["items"][0]["event_id"] == "matched_intro-1199"


def test_each_station_reports_what_it_airs_to_its_own_history(rig) -> None:  # noqa: F811
    client, *_rest = rig
    stations = client.app.state.stations
    for station in (stations.default, stations.get("nightowl")):
        bridge = station.bridge
        assert station.player.on_item_aired == bridge.record_aired
        assert bridge.liner_scheduler.on_aired == bridge.record_aired
    stations.get("nightowl").bridge.record_aired("time_check", title="Time check", duration=3)
    owl = client.get("/api/stations/nightowl/history?liners=1").json()
    assert [item["kind"] for item in owl["items"]][:1] == ["time_check"]
    main = client.get("/api/history?liners=1").json()
    assert "time_check" not in [item["kind"] for item in main["items"]]


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_a_stopped_station_reads_liners_from_its_database(rig) -> None:  # noqa: F811
    client, library, _cfg, song, _closes = rig
    library.station_ledger("nightowl").store.write_history(
        [
            _row(0, path=song.path),
            _row(1, "matched_intro", path=None),
            _row(2, path=song.path),
        ],
        [],
    )
    assert client.app.state.stations.editable("nightowl").stopped
    owl = "/api/stations/nightowl/history"
    music = client.get(owl).json()
    assert [item["event_id"] for item in music["items"]] == ["song-2", "song-0"]
    everything = client.get(owl + "?liners=1").json()
    assert [item["kind"] for item in everything["items"]] == ["song", "matched_intro", "song"]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

_TOKEN = "history-liners-test-token-32-bytes!!"


@pytest.fixture
def library(tmp_path: Path):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"audio")
    with LibraryCatalog(tmp_path / "library.db") as catalog:
        catalog.add_source(music)
        catalog.put_entry(
            IndexEntry(str(song), "Song", "Artist", "Album", "", 120, 2000, 180, 0.2, -1, -1, 0.0)
        )
        store = StationStore(tmp_path / "station.db", "default")
        aired = datetime(2026, 9, 1, 12).astimezone()
        store.write_history(
            [
                {
                    "event_id": "a",
                    "path": str(song),
                    "title": "Song",
                    "artist": "Artist",
                    "duration": 180.0,
                    "played_at": aired.isoformat(),
                },
                {
                    "event_id": "b",
                    "path": None,
                    "title": "Intro for Song",
                    "artist": "",
                    "duration": 4.0,
                    "played_at": (aired + timedelta(seconds=170)).isoformat(),
                    "kind": "matched_intro",
                },
                {
                    "event_id": "c",
                    "path": None,
                    "title": "Example spot",
                    "artist": "",
                    "duration": 30.0,
                    "played_at": (aired + timedelta(seconds=200)).isoformat(),
                    "kind": "ad",
                },
            ],
            [],
        )
        try:
            yield SimpleNamespace(catalog=catalog, store=store)
        finally:
            store.close()


def _paired(library) -> TestClient:
    player = _make_player_mock()
    player._cfg.server = ServerConfig(
        access_token=_TOKEN,
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
    )
    bridge = PlayerBridge(
        player=player,
        sim=_make_sim_mock(),
        library=SimpleNamespace(catalog=library.catalog),
        ledger=SimpleNamespace(store=library.store),
    )
    client = TestClient(create_app(bridge), headers={"Host": "testserver"})
    code = client.app.state.request_pairing_code()[0]
    response = client.post(
        "/api/pair",
        json={"code": code, "device_name": "Test"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200
    return client


URL = "/api/history/export?from=2026-09-01&to=2026-09-01"


def test_the_export_lists_songs_unless_liners_are_asked_for(library) -> None:
    client = _paired(library)
    songs = client.get(URL)
    assert songs.headers["x-play-count"] == "1"
    rows = list(csv.reader(io.StringIO(songs.text, newline="")))
    assert [row[8] for row in rows[1:]] == ["song"]
    # The intro does not shorten the song in either export.
    assert rows[1][2] == "180"

    everything = client.get(URL + "&liners=true")
    assert everything.headers["x-play-count"] == "2"
    rows = list(csv.reader(io.StringIO(everything.text, newline="")))
    assert [(row[4], row[8], row[2]) for row in rows[1:]] == [
        ("Song", "song", "180"),
        ("Intro for Song", "matched_intro", "4"),
    ]
    # Ads come from the ad play log alone, so the history's ad row is not repeated.
    assert "Example spot" not in everything.text


def test_soundexchange_ignores_the_liners_choice(library) -> None:
    client = _paired(library)
    plain = client.get(URL + "&format=soundexchange&service=Example")
    asked = client.get(URL + "&format=soundexchange&service=Example&liners=true")
    assert asked.status_code == 200
    assert asked.text == plain.text
    assert asked.headers["x-play-count"] == plain.headers["x-play-count"] == "1"
    assert "Intro" not in asked.text
