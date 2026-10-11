"""GET /api/history/export: the play log CSV download behind pairing."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

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


def test_stream_plays_count_listeners_at_start_and_at_peak() -> None:
    entry = _make_entry(3)
    library = _Library()
    bridge = PlayerBridge(player=_make_player_mock(entry), sim=_make_sim_mock(), library=library)
    bridge.note_listener_joined()  # not streaming: nothing to count
    bridge.stream = SimpleNamespace(listener_count=0, set_title=lambda *_: None)
    bridge.note_listener_joined()  # nothing playing yet
    bridge.stream.listener_count = 2
    bridge.on_track_started(entry)
    assert (library.rows[-1]["listeners"], library.rows[-1]["peak_listeners"]) == (2, 2)
    bridge.stream.listener_count = 5
    bridge.note_listener_joined()
    assert (library.rows[-1]["listeners"], library.rows[-1]["peak_listeners"]) == (2, 5)
    bridge.stream.listener_count = 4
    bridge.note_listener_joined()
    assert len(library.rows) == 2  # a lower count changes nothing
    bridge.library = None
    bridge.stream.listener_count = 6
    bridge.note_listener_joined()
    assert bridge._open_play[1]["peak_listeners"] == 6


def test_local_playback_counts_one_listener() -> None:
    library = _Library()
    bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock(), library=library)
    bridge.on_track_started(_make_entry(2))
    assert (library.rows[-1]["listeners"], library.rows[-1]["peak_listeners"]) == (1, 1)
