"""POST /api/library/tags through the app, with a real library and real files (#53)."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
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
    manager.catalog.add_source(cfg.library.music_dir)
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


def test_concurrent_beets_saves_keep_the_latest_tags_and_mtime(
    tagged, tmp_path: Path, monkeypatch
) -> None:
    from autodj import tags_api

    client, manager, song, _bridge = tagged
    manager.cfg.library.beets_db = _beets(tmp_path, song)
    first_beets_call = threading.Event()
    release_first_beets_call = threading.Event()
    second_lookup = threading.Event()
    lookup_count = 0
    lookup_lock = threading.Lock()
    real_entry_for_path = manager.entry_for_path
    real_update_beets = tags_api.update_beets

    def observe_lookup(path):
        nonlocal lookup_count
        with lookup_lock:
            lookup_count += 1
            if lookup_count == 2:
                second_lookup.set()
        return real_entry_for_path(path)

    beets_count = 0
    beets_lock = threading.Lock()

    def delay_first_beets(*args):
        nonlocal beets_count
        with beets_lock:
            beets_count += 1
            first = beets_count == 1
        if first:
            first_beets_call.set()
            assert tags_api._lock_for(song).locked()
            assert release_first_beets_call.wait(5)
        return real_update_beets(*args)

    monkeypatch.setattr(manager, "entry_for_path", observe_lookup)
    monkeypatch.setattr(tags_api, "update_beets", delay_first_beets)

    def save(artist: str):
        return client.post(
            "/api/library/tags",
            json={"path": str(song), "tags": {"artist": artist}, "beets": True},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(save, "First")
        try:
            assert first_beets_call.wait(5)
            second = pool.submit(save, "Second")
            assert second_lookup.wait(5)
            release_first_beets_call.set()
            assert first.result(timeout=10).status_code == 200
            assert second.result(timeout=10).status_code == 200
        finally:
            release_first_beets_call.set()

    conn = sqlite3.connect(manager.cfg.library.beets_db)
    artist, mtime = conn.execute("SELECT artist, mtime FROM items").fetchone()
    conn.close()
    assert artist == "Second"
    assert mtime == song.stat().st_mtime
    assert read_file_tags(song).artist == "Second"
    assert manager.catalog.record_for_path(song)["artist"] == "Second"


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


# ---------------------------------------------------------------------------
# Review fixes
# ---------------------------------------------------------------------------


def test_an_oversized_body_is_refused_before_it_is_read(tagged) -> None:
    from autodj.tags_api import TAG_BODY_MAX_BYTES

    client, _manager, song, _bridge = tagged
    before = song.read_bytes()
    response = client.post(
        "/api/library/tags",
        json={"path": str(song), "tags": {"title": "x" * (TAG_BODY_MAX_BYTES + 1)}},
    )
    assert response.status_code == 413
    assert song.read_bytes() == before


def test_the_largest_valid_body_fits(tagged) -> None:
    client, _manager, song, _bridge = tagged
    text = "é" * 500
    tags = {"title": text, "artist": text, "album": text, "genre": text}
    response = client.post("/api/library/tags", json={"path": str(song), "tags": tags})
    assert response.status_code == 200, response.text


def test_a_link_that_leaves_the_library_is_refused(tagged, tmp_path: Path) -> None:
    client, _manager, song, _bridge = tagged
    outside = make_audio(tmp_path, ".flac", stem="outside")
    before = outside.read_bytes()
    # The catalog held a real file here; it was later swapped for a link.
    song.unlink()
    try:
        song.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are not available here")
    response = client.post("/api/library/tags", json={"path": str(song), "tags": {"title": "x"}})
    assert response.status_code == 409
    assert "outside the library folders" in response.json()["detail"]
    assert outside.read_bytes() == before


@pytest.mark.parametrize("where", ["pending", "ready", "playing_next"])
def test_the_mix_bus_cursor_counts_as_up_next(tagged, where: str) -> None:
    from types import SimpleNamespace

    client, _manager, song, bridge = tagged
    before = song.read_bytes()
    entry = SimpleNamespace(path=str(song))
    player = bridge.player
    if where == "pending":
        player._pending_entry = entry
    elif where == "ready":
        player._render_ahead = SimpleNamespace(_ready=SimpleNamespace(entry=None, next_entry=entry))
    else:
        player._playing_render = SimpleNamespace(entry=None, next_entry=entry)
    response = client.post("/api/library/tags", json={"path": str(song), "tags": {"title": "x"}})
    assert response.status_code == 409
    assert "up next" in response.json()["detail"]
    assert song.read_bytes() == before


def test_another_spelling_of_the_path_still_counts_as_playing(tagged) -> None:
    from types import SimpleNamespace

    client, _manager, song, bridge = tagged
    spelled = str(song.parent / "." / song.name)
    bridge.player._state.current_track = SimpleNamespace(path=spelled)
    response = client.post("/api/library/tags", json={"path": str(song), "tags": {"title": "x"}})
    assert response.status_code == 409


def test_a_song_that_goes_on_air_during_the_write_is_not_replaced(tagged, monkeypatch) -> None:
    from types import SimpleNamespace

    from autodj import tags_api

    client, manager, song, bridge = tagged
    before = song.read_bytes()
    real_check = tags_api.playing_on
    calls = []

    def goes_on_air(stations, path):
        calls.append(path)
        if len(calls) == 2:  # the check made under the advance locks
            bridge.player._state.next_track = SimpleNamespace(path=str(song))
        return real_check(stations, path)

    monkeypatch.setattr(tags_api, "playing_on", goes_on_air)
    response = client.post("/api/library/tags", json={"path": str(song), "tags": {"title": "x"}})
    assert response.status_code == 409
    assert len(calls) == 2
    assert song.read_bytes() == before
    assert manager.catalog.record_for_path(song)["title"] == "Old Title"


def test_the_advance_lock_is_held_across_the_rename(tagged, monkeypatch) -> None:
    import threading

    from autodj import tag_edit

    client, _manager, song, bridge = tagged
    lock = threading.Lock()
    bridge.player._advance_lock = lock
    held = []
    real_replace = tag_edit.os.replace

    def replace(source, target):
        held.append(lock.locked())
        real_replace(source, target)

    monkeypatch.setattr(tag_edit.os, "replace", replace)
    response = client.post("/api/library/tags", json={"path": str(song), "tags": {"title": "x"}})
    assert response.status_code == 200
    assert held == [True]
    assert not lock.locked()


def test_a_catalog_failure_after_the_write_is_a_warning(tagged, monkeypatch) -> None:
    client, manager, song, _bridge = tagged

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(manager.catalog, "retag", broken)
    response = client.post("/api/library/tags", json={"path": str(song), "tags": {"title": "New"}})
    assert response.status_code == 200
    assert "could not update its library" in response.json()["warning"]
    assert read_file_tags(song).title == "New"


def test_a_reload_failure_after_the_write_is_a_warning(tagged, monkeypatch) -> None:
    client, manager, song, _bridge = tagged
    real_refresh = manager.refresh
    calls = []

    def broken():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("index busy")
        return real_refresh()

    monkeypatch.setattr(manager, "refresh", broken)
    response = client.post("/api/library/tags", json={"path": str(song), "tags": {"title": "New"}})
    assert response.status_code == 200
    assert "did not reload" in response.json()["warning"]
    assert manager.catalog.record_for_path(song)["title"] == "New"


def test_a_saved_edit_has_no_warning(tagged) -> None:
    client, _manager, song, _bridge = tagged
    response = client.post("/api/library/tags", json={"path": str(song), "tags": {"title": "New"}})
    assert response.json()["warning"] == ""


def test_two_edits_at_once_leave_the_catalog_matching_the_file(tagged) -> None:
    from concurrent.futures import ThreadPoolExecutor

    client, manager, song, _bridge = tagged

    def save(title: str) -> int:
        body = {"path": str(song), "tags": {"title": title}}
        return client.post("/api/library/tags", json=body).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(save, [f"Edit {n}" for n in range(8)]))
    assert statuses == [200] * 8
    stored = manager.catalog.record_for_path(song)
    assert (stored["size"], stored["fingerprint"]) == file_fingerprint(song)
    assert stored["title"] == read_file_tags(song).title
    assert manager.catalog.has_vector(song)


def test_a_damaged_file_still_opens_in_track_info(tagged, monkeypatch) -> None:
    import struct

    import mutagen

    client, _manager, song, _bridge = tagged

    def broken(*_args, **_kwargs):
        raise struct.error("unpack requires a buffer of 4 bytes")

    monkeypatch.setattr(mutagen, "File", broken)
    response = client.get("/api/library/points", params={"path": str(song)})
    assert response.status_code == 200
    assert response.json()["tags_writable"] is False
    assert response.json()["tags"]["isrc"] == ""


# ---------------------------------------------------------------------------
# Second review
# ---------------------------------------------------------------------------


def test_a_folder_flush_failure_after_the_rename_still_stores_the_edit(tagged, monkeypatch) -> None:
    from autodj import tag_edit

    client, manager, song, _bridge = tagged

    def refused(_folder):
        raise OSError(22, "this share cannot flush a folder")

    monkeypatch.setattr(tag_edit, "fsync_directory", refused)
    response = client.post("/api/library/tags", json={"path": str(song), "tags": {"title": "New"}})
    assert response.status_code == 200
    stored = manager.catalog.record_for_path(song)
    assert stored["title"] == "New"
    assert (stored["size"], stored["fingerprint"]) == file_fingerprint(song)
    assert manager.catalog.has_vector(song)


def test_a_scan_between_the_rename_and_retag_keeps_the_analysis(tagged, monkeypatch) -> None:
    client, manager, song, _bridge = tagged
    real_retag = manager.catalog.retag

    def scan_first(path, fields):
        # What a scan does when it reads the rewritten file first.
        manager.catalog.put_entry(make_entry(song, title="Read by the scan"))
        return real_retag(path, fields)

    monkeypatch.setattr(manager.catalog, "retag", scan_first)
    response = client.post("/api/library/tags", json={"path": str(song), "tags": {"title": "New"}})
    assert response.status_code == 200
    stored = manager.catalog.record_for_path(song)
    assert stored["title"] == "New"
    assert stored["points"] == {"start": 0.0, "end": 0.1}
    assert manager.catalog.has_vector(song)


def test_beets_without_the_changed_field_is_left_alone(tagged, tmp_path: Path) -> None:
    client, manager, song, _bridge = tagged
    manager.cfg.library.beets_db = _beets(tmp_path, song)
    response = client.post(
        "/api/library/tags",
        json={"path": str(song), "tags": {"isrc": "AB1CD2600001"}, "beets": True},
    )
    assert response.json()["beets"] == "unchanged"
    conn = sqlite3.connect(manager.cfg.library.beets_db)
    assert conn.execute("SELECT mtime FROM items").fetchone() == (None,)
    conn.close()
