"""Each station's plays, play log and settings in the shared library.db (#70)."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from autodj.catalog import DEFAULT_STATION, LibraryCatalog
from autodj.library import LibraryManager
from autodj.player import Player
from autodj.playlog import plays_between
from autodj.station_ledger import StationLedger, setting_key
from autodj.stations import DEFAULT_SLUG, SLUG_PATTERN
from tests.unit.test_library import Similarity, make_config, make_entry

T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)


def _row(event_id: str, path: Path, title: str, minutes: int = 0) -> dict:
    return {
        "event_id": event_id,
        "path": str(path),
        "title": title,
        "artist": "Artist",
        "duration": 180.0,
        "played_at": (T0 + timedelta(minutes=minutes)).isoformat(),
    }


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


@pytest.fixture
def catalog(tmp_path: Path):
    music = tmp_path / "Music"
    music.mkdir()
    songs = [music / "one.flac", music / "two.flac"]
    for song in songs:
        song.write_bytes(b"audio")
    with LibraryCatalog(tmp_path / "library.db") as opened:
        opened.add_source(music)
        for song in songs:
            opened.put_entry(make_entry(song))
        yield opened, songs


def test_the_default_station_keeps_its_names() -> None:
    assert DEFAULT_STATION == DEFAULT_SLUG
    assert setting_key(DEFAULT_SLUG, "clock") == "clock"
    assert setting_key("nightowl", "clock") == "clock:nightowl"
    assert StationLedger(SimpleNamespace(), "nightowl").setting_key("clock") == "clock:nightowl"
    assert SLUG_PATTERN.match("night-owl2")
    for bad in ("", "-x", "x-", "Owl", "a" * 33, "../x", "a_b"):
        assert SLUG_PATTERN.match(bad) is None, bad


def test_two_ledgers_on_one_catalog_keep_history_last_played_and_clock_apart(catalog) -> None:
    library, (one, two) = catalog
    main = StationLedger(library)
    owl = StationLedger(library, "nightowl")

    main.remember_play(one, 10.0)
    owl.remember_play(one, 20.0)
    owl.remember_play(two, 30.0)
    main.remember_history(_row("main-1", one, "Main one"))
    owl.remember_history(_row("owl-1", one, "Owl one", 1))
    owl.remember_history(_row("owl-2", two, "Owl two", 2))
    owl.interrupt_history("owl-2")
    main.remember_setting("clock", {"name": "Morning", "cursor": 1})
    owl.remember_setting("clock", {"name": "Night", "cursor": 4})
    # Nothing reaches SQLite before a flush, and each sees only its own plays.
    assert library.recent_history() == []
    assert main.last_played() == {_key(one): 10.0}
    assert owl.last_played() == {_key(one): 20.0, _key(two): 30.0}
    assert [row["event_id"] for row in owl.pending_history()] == ["owl-1"]

    main.flush()
    owl.flush()

    # The default station keeps its last plays on the track records ...
    assert library.last_played() == {_key(one): 10.0, _key(two): 0.0}
    # ... and any other station in station_plays.
    assert library.last_played("nightowl") == {_key(one): 20.0, _key(two): 30.0}
    assert [row["event_id"] for row in main.recent_history()] == ["main-1"]
    assert [row["event_id"] for row in owl.recent_history()] == ["owl-1"]
    assert [row["title"] for row in owl.recent_plays(10)] == ["Owl one"]
    assert [row["title"] for row in main.play_log()] == ["Main one"]
    assert [row["title"] for row in owl.play_log()] == ["Owl one"]
    assert library.get_setting("clock") == {"name": "Morning", "cursor": 1}
    assert library.get_setting("clock:nightowl") == {"name": "Night", "cursor": 4}
    assert main.get_setting("clock") == {"name": "Morning", "cursor": 1}
    assert owl.get_setting("clock") == {"name": "Night", "cursor": 4}
    assert owl.pending_history() == []


def test_the_default_station_reads_rows_written_before_stations(tmp_path: Path, catalog) -> None:
    library, (one, _two) = catalog
    library.remember_play(one, 99.0)
    library.close()
    # A play log from before stations: no station column, no index on it.
    with closing(sqlite3.connect(tmp_path / "library.db")) as db:
        db.execute("DROP INDEX play_history_station")
        db.execute("ALTER TABLE play_history DROP COLUMN station")
        db.execute(
            "INSERT INTO play_history (event_id,path,title,artist,duration,played_at) "
            "VALUES ('old', ?, 'Old play', 'Artist', 180, ?)",
            (_key(one), T0.isoformat()),
        )
        db.commit()

    with LibraryCatalog(tmp_path / "library.db") as reopened:
        main = StationLedger(reopened)
        assert [row["event_id"] for row in main.recent_history()] == ["old"]
        assert [row["title"] for row in main.play_log()] == ["Old play"]
        assert reopened.last_played()[_key(one)] == 99.0
        assert reopened.play_log("nightowl") == []
        assert reopened.last_played("nightowl") == {}
        columns = {row[1] for row in reopened._db().execute("PRAGMA table_info(play_history)")}
        assert "station" in columns


def test_the_play_log_export_is_per_station(catalog) -> None:
    library, (one, two) = catalog
    library.write_history([_row("main-1", one, "Main one")])
    library.write_history([_row("owl-1", two, "Owl one", 1)], station="nightowl")

    def titles(station: str = DEFAULT_STATION) -> list[str]:
        plays = plays_between(
            library, T0, T0 + timedelta(hours=1), read_tags=lambda _p: ("", ""), station=station
        )
        return [play.title for play in plays]

    assert titles() == ["Main one"]
    assert titles("nightowl") == ["Owl one"]
    assert titles("other") == []


def test_a_moved_file_keeps_every_stations_last_play(tmp_path: Path, catalog) -> None:
    library, (one, _two) = catalog
    library.remember_play(one, 5.0, station="nightowl")
    moved = tmp_path / "Music" / "moved.flac"
    one.rename(moved)
    assert library.rekey(one, moved)
    assert library.last_played("nightowl") == {_key(moved): 5.0}


def test_the_library_flushes_and_reloads_every_ledger(tmp_path: Path) -> None:
    music = tmp_path / "Music"
    song = music / "song.flac"
    manager = LibraryManager(make_config(tmp_path, music=music), Similarity())
    try:
        owl = manager.add_ledger("nightowl")
        assert manager.ledger.station == DEFAULT_SLUG
        manager.ledger.remember_play(song, 1.0)
        owl.remember_play(song, 2.0)
        owl.remember_history(_row("owl-1", song, "Owl"))
        manager.tick()
        assert manager.catalog.last_played("nightowl") == {_key(song): 2.0}
        assert [row["event_id"] for row in owl.recent_history()] == ["owl-1"]
        assert manager.catalog.recent_history() == []

        # A station added later starts from what is saved for it.
        again = manager.add_ledger("nightowl")
        assert again.last_played() == {_key(song): 2.0}
        owl.remember_play(song, 3.0)
    finally:
        manager.close()
    with LibraryCatalog(manager.cfg.index.active_dir / "library.db") as reopened:
        assert reopened.last_played("nightowl") == {_key(song): 3.0}


def test_a_player_records_into_its_own_station() -> None:
    player = Player.__new__(Player)
    library = SimpleNamespace(ledger=SimpleNamespace(station=DEFAULT_SLUG))
    assert player._ledger is None
    player._library = library
    assert player._ledger is library.ledger
    own = SimpleNamespace(station="nightowl")
    player._ledger = own
    assert player._ledger is own


def test_select_clock_drops_the_old_clockwheels_place_saved_during_and_after_it(
    catalog, monkeypatch
) -> None:
    # #133: until it reloads, the player keeps saving its place in the old
    # clockwheel; none of it may flush over the selection.
    library, _songs = catalog
    ledger = StationLedger(library)
    ledger.remember_setting("clock", {"name": "Morning", "cursor": 2})
    write = library.set_setting

    def write_then_player_saves(key, value):
        write(key, value)
        ledger.remember_setting("clock", {"name": "Morning", "cursor": 3})

    monkeypatch.setattr(library, "set_setting", write_then_player_saves)
    assert ledger.select_clock("Night") == {"name": "Night", "cursor": 0}
    monkeypatch.setattr(library, "set_setting", write)
    ledger.remember_setting("clock", {"name": "Morning", "cursor": 4})
    assert ledger.get_setting("clock") == {"name": "Night", "cursor": 0}
    ledger.flush()
    assert library.get_setting("clock") == {"name": "Night", "cursor": 0}
    # Once the player has loaded Night, its places are kept.
    ledger.remember_setting("clock", {"name": "Night", "cursor": 1})
    ledger.flush()
    assert library.get_setting("clock") == {"name": "Night", "cursor": 1}


def test_a_failed_selection_keeps_the_buffered_place(catalog, monkeypatch) -> None:
    library, _songs = catalog
    ledger = StationLedger(library)
    ledger.remember_setting("clock", {"name": "Morning", "cursor": 2})
    write = library.set_setting

    def locked(_key, _value):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(library, "set_setting", locked)
    with pytest.raises(sqlite3.OperationalError):
        ledger.select_clock("Night")
    monkeypatch.setattr(library, "set_setting", write)
    ledger.flush()
    assert library.get_setting("clock") == {"name": "Morning", "cursor": 2}
    # Morning is still the one selected, so the player's places still count.
    ledger.remember_setting("clock", {"name": "Morning", "cursor": 3})
    ledger.flush()
    assert library.get_setting("clock") == {"name": "Morning", "cursor": 3}


def test_reselecting_reads_and_writes_the_place_under_one_lock(catalog, monkeypatch) -> None:
    library, _songs = catalog
    library.set_setting("clock", {"name": "Morning", "cursor": 2})
    ledger = StationLedger(library)
    read = library.get_setting
    flushes = []

    def read_then_player_saves_and_flushes(key, default=None):
        value = read(key, default)
        ledger.remember_setting("clock", {"name": "Morning", "cursor": 5})
        flush = threading.Thread(target=ledger.flush)
        flush.start()
        flush.join(0.2)
        flushes.append((flush, flush.is_alive()))
        return value

    monkeypatch.setattr(library, "get_setting", read_then_player_saves_and_flushes)
    assert ledger.select_clock("Morning") == {"name": "Morning", "cursor": 2}
    monkeypatch.setattr(library, "get_setting", read)
    # The flush waited for the selection rather than writing between its
    # read and its write ...
    [(flush, waited)] = flushes
    flush.join(5)
    assert waited
    # ... and the player's newer place in the same clockwheel is kept.
    assert library.get_setting("clock") == {"name": "Morning", "cursor": 5}
