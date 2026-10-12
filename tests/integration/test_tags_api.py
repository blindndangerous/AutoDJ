"""POST /api/library/tags through the app, with a real library and real files (#53)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from autodj.audio_meta import read_file_tags, read_reporting_tags
from autodj.indexer import FEATURE_DIM, IndexEntry, file_fingerprint
from autodj.library import LibraryManager
from autodj.server import PlayerBridge, create_app
from tests.unit._tagged_audio import make_audio
from tests.unit.test_library import Similarity, make_config, make_entry

from ._helpers import _make_player_mock, _make_sim_mock


@pytest.fixture
def tagged(tmp_path: Path):
    """A client, its library and one analysed FLAC song in the music folder."""
    cfg = make_config(tmp_path)
    manager = LibraryManager(cfg, Similarity())
    song = make_audio(cfg.library.music_dir, ".flac")
    entry = make_entry(song, title="Old Title")
    manager.catalog.put_entry(entry, points={"start": 0.0, "end": 0.1}, point_source="user")
    manager.catalog.set_vector(song, np.ones(FEATURE_DIM, dtype=np.float32))
    manager.refresh()
    bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock())
    bridge.library = manager
    client = TestClient(create_app(bridge))
    yield client, manager, song, bridge
    manager.close()


def test_track_info_offers_the_tags_to_edit(tagged) -> None:
    client, _manager, song, _bridge = tagged
    record = client.get("/api/library/points", params={"path": str(song)}).json()
    assert record["tags"] == {
        "title": "Old Title",
        "artist": "Artist",
        "album": "Album",
        "genre": "",
        "year": "2001",
        "bpm": "120",
        "key": "",
        "isrc": "",
    }
    assert record["tags_writable"] is True
    assert record["beets_writeback"] is False


def test_saving_tags_writes_the_file_and_keeps_the_analysis(tagged) -> None:
    client, manager, song, _bridge = tagged
    response = client.post(
        "/api/library/tags",
        json={
            "path": str(song),
            "tags": {"title": "New Title", "key": "8A", "bpm": "126", "isrc": "AB1CD2600001"},
        },
    )
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["title"] == "New Title"
    assert record["tags"]["key"] == "Am"
    assert record["key_label"] == "8A"
    assert record["tags"]["isrc"] == "AB1CD2600001"
    assert record["points"] == {"start": 0.0, "end": 0.1}
    assert record["analysis"] == "ready"
    assert record["beets"] == "off"
    # The file, the catalog and the loaded library agree.
    assert read_file_tags(song).title == "New Title"
    assert read_reporting_tags(song)[0] == "AB1CD2600001"
    stored = manager.catalog.record_for_path(song)
    assert (stored["size"], stored["fingerprint"]) == file_fingerprint(song)
    assert stored["artist"] == "Artist"
    assert manager.entry_for_path(song).title == "New Title"
    assert manager.catalog.has_vector(song)


def test_a_refused_field_answers_400_with_the_field(tagged) -> None:
    client, _manager, song, _bridge = tagged
    before = song.read_bytes()
    response = client.post("/api/library/tags", json={"path": str(song), "tags": {"year": "99"}})
    assert response.status_code == 400
    assert response.json()["field"] == "year"
    assert "four digits" in response.json()["detail"]
    assert song.read_bytes() == before


@pytest.mark.parametrize(
    "body",
    [
        {"tags": {"title": "x"}},
        {"path": "x", "tags": {"title": 5}},
        {"path": "x", "tags": {}, "beets": "yes"},
        {"path": "x", "tags": {}, "extra": 1},
    ],
)
def test_malformed_bodies_are_refused(tagged, body) -> None:
    client, *_ = tagged
    assert client.post("/api/library/tags", json=body).status_code == 422


def test_an_unknown_recording_is_404(tagged, tmp_path: Path) -> None:
    client, *_ = tagged
    response = client.post(
        "/api/library/tags", json={"path": str(tmp_path / "nope.flac"), "tags": {"title": "x"}}
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("attribute", "words"), [("current_track", "playing now"), ("next_track", "up next")]
)
def test_a_song_on_air_is_not_rewritten(tagged, attribute: str, words: str) -> None:
    client, _manager, song, bridge = tagged
    before = song.read_bytes()
    setattr(
        bridge.player._state,
        attribute,
        IndexEntry(str(song), "Old Title", "", "", "", 0, 0, 1.0, 0, -1, -1, 0),
    )
    response = client.post("/api/library/tags", json={"path": str(song), "tags": {"title": "x"}})
    assert response.status_code == 409
    assert words in response.json()["detail"]
    assert song.read_bytes() == before


def test_a_write_failure_is_409_and_keeps_the_file(tagged, monkeypatch) -> None:
    from autodj import tag_edit

    client, manager, song, _bridge = tagged
    before = song.read_bytes()

    def busy(*_args):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(tag_edit, "_REPLACE_WAIT_S", 0)
    monkeypatch.setattr(tag_edit.os, "replace", busy)
    response = client.post("/api/library/tags", json={"path": str(song), "tags": {"title": "x"}})
    assert response.status_code == 409
    assert "open in another program" in response.json()["detail"]
    assert song.read_bytes() == before
    assert manager.catalog.record_for_path(song)["title"] == "Old Title"


def _beets(tmp_path: Path, song: Path) -> Path:
    db = tmp_path / "beets.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB, title TEXT, artist TEXT, "
        "album TEXT, genre TEXT, year INTEGER, bpm INTEGER, initial_key TEXT, mtime REAL)"
    )
    conn.execute("INSERT INTO items (path, title) VALUES (?, 'Old Title')", (str(song).encode(),))
    conn.commit()
    conn.close()
    return db


def test_beets_is_updated_when_asked(tagged, tmp_path: Path) -> None:
    client, manager, song, _bridge = tagged
    manager.cfg.library.beets_db = _beets(tmp_path, song)
    assert client.get("/api/library/points", params={"path": str(song)}).json()["beets_writeback"]

    response = client.post(
        "/api/library/tags", json={"path": str(song), "tags": {"artist": "Band"}, "beets": True}
    )
    assert response.json()["beets"] == "updated"
    conn = sqlite3.connect(manager.cfg.library.beets_db)
    assert conn.execute("SELECT artist FROM items").fetchone() == ("Band",)
    conn.close()

    # Not asked: beets stays as it was.
    client.post("/api/library/tags", json={"path": str(song), "tags": {"artist": "Other"}})
    conn = sqlite3.connect(manager.cfg.library.beets_db)
    assert conn.execute("SELECT artist FROM items").fetchone() == ("Band",)
    conn.close()


def test_a_beets_failure_still_saves_the_file(tagged, tmp_path: Path) -> None:
    client, manager, song, _bridge = tagged
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not a database at all, not even close" * 10)
    manager.cfg.library.beets_db = broken
    response = client.post(
        "/api/library/tags", json={"path": str(song), "tags": {"artist": "Band"}, "beets": True}
    )
    assert response.status_code == 200
    assert response.json()["beets"] == "failed"
    assert response.json()["beets_error"]
    assert read_file_tags(song).artist == "Band"


def test_a_song_beets_does_not_know_says_so(tagged, tmp_path: Path) -> None:
    client, manager, song, _bridge = tagged
    other = make_audio(tmp_path, ".flac", stem="elsewhere")
    manager.cfg.library.beets_db = _beets(tmp_path, other)
    response = client.post(
        "/api/library/tags", json={"path": str(song), "tags": {"artist": "Band"}, "beets": True}
    )
    assert response.json()["beets"] == "not_found"


def test_no_library_is_503(tmp_path: Path) -> None:
    bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock())
    bridge.library = None
    client = TestClient(create_app(bridge))
    response = client.post("/api/library/tags", json={"path": "x", "tags": {"title": "x"}})
    assert response.status_code == 503
