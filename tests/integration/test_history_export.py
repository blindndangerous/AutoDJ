"""GET /api/history/export: the play log CSV download behind pairing."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from autodj.audience import StreamAudience
from autodj.catalog import LibraryCatalog
from autodj.config import ServerConfig
from autodj.indexer import IndexEntry
from autodj.playlog import COLUMNS
from autodj.server import PlayerBridge, create_app

from ._helpers import _make_entry, _make_player_mock, _make_sim_mock

_TOKEN = "history-export-test-token-32-bytes!"


@pytest.fixture
def catalog(tmp_path: Path):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"audio")
    with LibraryCatalog(tmp_path / "library.db") as opened:
        opened.add_source(music)
        opened.put_entry(
            IndexEntry(str(song), "Song", "Artist", "Album", "", 120, 2000, 180, 0.2, -1, -1, 0.0)
        )
        opened.write_history(
            [
                {
                    "event_id": "a",
                    "path": str(song),
                    "title": "@Song",
                    "artist": "Artist",
                    "duration": 180.0,
                    "played_at": datetime(2026, 9, 1, 12).astimezone().isoformat(),
                }
            ]
        )
        yield opened


def _client(library) -> TestClient:
    player = _make_player_mock()
    player._cfg.server = ServerConfig(
        access_token=_TOKEN,
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
    )
    bridge = PlayerBridge(player=player, sim=_make_sim_mock(), library=library)
    return TestClient(create_app(bridge), headers={"Host": "testserver"})


def _paired(library) -> TestClient:
    client = _client(library)
    code = client.app.state.request_pairing_code()[0]
    response = client.post(
        "/api/pair",
        json={"code": code, "device_name": "Test"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200
    return client


URL = "/api/history/export?from=2026-09-01&to=2026-09-01"


def test_export_needs_a_paired_session(catalog) -> None:
    response = _client(SimpleNamespace(catalog=catalog)).get(URL)
    assert response.status_code == 401
    assert "@Song" not in response.text


def test_export_refuses_a_foreign_host(catalog) -> None:
    client = _paired(SimpleNamespace(catalog=catalog))
    response = client.get(URL, headers={"Host": "evil.example"})
    assert response.status_code == 403


def test_export_downloads_the_csv(catalog) -> None:
    response = _paired(SimpleNamespace(catalog=catalog)).get(URL)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == (
        'attachment; filename="autodj-plays-2026-09-01-to-2026-09-01.csv"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-play-count"] == "1"
    assert response.headers["x-songs-missing-fields"] == "1"
    assert response.headers["x-songs-unmatchable"] == "1"  # no ISRC or label
    rows = list(csv.reader(io.StringIO(response.text, newline="")))
    assert tuple(rows[0]) == COLUMNS
    assert rows[1][4] == "'@Song"
    assert rows[1][9] == "song.flac"


def test_export_of_an_empty_range_is_just_the_header(catalog) -> None:
    response = _paired(SimpleNamespace(catalog=catalog)).get(
        "/api/history/export?from=2026-10-01&to=2026-10-31"
    )
    assert response.status_code == 200
    assert response.text == ",".join(COLUMNS) + "\r\n"
    assert response.headers["x-play-count"] == "0"
    assert response.headers["content-disposition"].endswith(
        'filename="autodj-plays-2026-10-01-to-2026-10-31.csv"'
    )


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("from=soon&to=2026-09-01", "YYYY-MM-DD"),
        ("from=2026-09-02&to=2026-09-01", "after its start"),
    ],
)
def test_export_rejects_bad_ranges(catalog, query: str, message: str) -> None:
    response = _paired(SimpleNamespace(catalog=catalog)).get(f"/api/history/export?{query}")
    assert response.status_code == 422
    assert message in response.json()["detail"]


def test_export_needs_both_dates(catalog) -> None:
    response = _paired(SimpleNamespace(catalog=catalog)).get("/api/history/export?from=2026-09-01")
    assert response.status_code == 422


def test_export_without_a_library_is_unavailable() -> None:
    response = _paired(None).get(URL)
    assert response.status_code == 503


def test_soundexchange_report_download(catalog) -> None:
    client = _paired(SimpleNamespace(catalog=catalog))
    response = client.get(f"{URL}&format=soundexchange&service=My%20Station&category=B")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=us-ascii"
    assert response.headers["content-disposition"] == (
        'attachment; filename="MyStation20260901-20260901.txt"'
    )
    assert response.text == "^My Station^|^B^|^Artist^|^@Song^||^Album^|||||1\r\n"
    missing = client.get(f"{URL}&format=soundexchange")
    assert missing.status_code == 422
    assert "name of the service" in missing.json()["detail"]
    assert client.get(f"{URL}&format=soundexchange&service=S&category=D").status_code == 422
    assert client.get(f"{URL}&format=xml").status_code == 422


class _Library:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def remember_history(self, row: dict) -> None:
        self.rows.append(dict(row))

    def remember_play(self, _path: str) -> None:
        pass


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _stream_bridge(entry, library):
    bridge = PlayerBridge(player=_make_player_mock(entry), sim=_make_sim_mock(), library=library)
    bridge.stream = SimpleNamespace(listener_count=0, set_title=lambda *_: None)
    clock = _Clock()
    bridge._audience = StreamAudience(clock)
    return bridge, clock


ALICE = ("192.0.2.1", "VLC/3.0")
BOB = ("192.0.2.2", "Sonos/80.1")


def test_stream_play_counts_listeners_at_start_plus_each_who_joins() -> None:
    entry = _make_entry(3)
    library = _Library()
    bridge, clock = _stream_bridge(entry, library)
    bridge.listener_connected(ALICE)  # nothing playing yet
    assert library.rows == []
    bridge.on_track_started(entry)
    assert library.rows[-1]["performances"] == 1
    bridge.listener_connected(BOB)
    assert library.rows[-1]["performances"] == 2
    # Alice leaves and a new listener on her address with another player
    # joins: that is someone else.
    bridge.listener_disconnected(ALICE)
    bridge.listener_connected((ALICE[0], "Firefox/140"))
    assert library.rows[-1]["performances"] == 3
    # Bob drops and reconnects within ten seconds: still one listener.
    bridge.listener_disconnected(BOB)
    clock.now += 9
    bridge.listener_connected(BOB)
    # A second connection while Bob's first is open is also Bob.
    bridge.listener_connected(BOB)
    assert len(library.rows) == 3
    assert bridge._open_play[1]["performances"] == 3
    # Alice comes back after more than ten seconds away: a new performance.
    clock.now += 11
    bridge.listener_connected(ALICE)
    assert library.rows[-1]["performances"] == 4
    # Peak concurrency would say 3 here (Alice, Firefox, Bob); the count
    # is the listeners who heard the play.
    bridge.library = None
    bridge.listener_connected(("192.0.2.9", "VLC/3.0"))
    assert bridge._open_play[1]["performances"] == 5


def test_next_play_starts_from_the_listeners_connected_now() -> None:
    entry = _make_entry(3)
    library = _Library()
    bridge, clock = _stream_bridge(entry, library)
    bridge.listener_connected(ALICE)
    bridge.listener_connected(BOB)
    bridge.listener_connected(BOB)  # one listener, two connections
    bridge.on_track_started(entry)
    assert library.rows[-1]["performances"] == 2
    bridge.listener_disconnected(ALICE)
    bridge.listener_disconnected(BOB)
    bridge.player._state.current_track = second = _make_entry(4)
    bridge.on_track_started(second)
    assert library.rows[-1]["performances"] == 1  # Bob is still connected once
    # Alice left during the last song; reconnecting in this one is her
    # first time hearing it, so it counts.
    clock.now += 2
    bridge.listener_connected(ALICE)
    assert library.rows[-1]["performances"] == 2
    bridge.listener_disconnected(("203.0.113.5", "never connected"))  # ignored


def test_counts_are_taken_under_the_history_lock() -> None:
    entry = _make_entry(3)
    bridge, _clock = _stream_bridge(entry, _Library())
    seen: list[bool] = []
    real = bridge._audience.connected

    def connected(client):
        seen.append(bridge._history_lock.locked())
        return real(client)

    bridge._audience.connected = connected
    start = bridge._audience.start_play

    def start_play():
        seen.append(bridge._history_lock.locked())
        return start()

    bridge._audience.start_play = start_play
    bridge.listener_connected(ALICE)
    bridge.on_track_started(entry)
    assert seen == [True, True]


def test_local_playback_counts_one_listener() -> None:
    library = _Library()
    bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock(), library=library)
    bridge.on_track_started(_make_entry(2))
    assert library.rows[-1]["performances"] == 1
