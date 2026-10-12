"""Each station's plays, play log and settings in the shared library.db (#70)."""

from __future__ import annotations

import os
import sqlite3
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
    assert setting_key("icecold", "clock") == "clock:icecold"
    assert StationLedger(SimpleNamespace(), "icecold").setting_key("clock") == "clock:icecold"
    assert SLUG_PATTERN.match("ice-cold2")
    for bad in ("", "-x", "x-", "Ice", "a" * 33, "../x", "a_b"):
        assert SLUG_PATTERN.match(bad) is None, bad


def test_two_ledgers_on_one_catalog_keep_history_last_played_and_clock_apart(catalog) -> None:
    library, (one, two) = catalog
    main = StationLedger(library)
    ice = StationLedger(library, "icecold")

    main.remember_play(one, 10.0)
    ice.remember_play(one, 20.0)
    ice.remember_play(two, 30.0)
    main.remember_history(_row("main-1", one, "Main one"))
    ice.remember_history(_row("ice-1", one, "Ice one", 1))
    ice.remember_history(_row("ice-2", two, "Ice two", 2))
    ice.interrupt_history("ice-2")
    main.remember_setting("clock", {"name": "Morning", "cursor": 1})
    ice.remember_setting("clock", {"name": "Night", "cursor": 4})
    # Nothing reaches SQLite before a flush, and each sees only its own plays.
    assert library.recent_history() == []
    assert main.last_played() == {_key(one): 10.0}
    assert ice.last_played() == {_key(one): 20.0, _key(two): 30.0}
    assert [row["event_id"] for row in ice.pending_history()] == ["ice-1"]

    main.flush()
    ice.flush()

    # The default station keeps its last plays on the track records ...
    assert library.last_played() == {_key(one): 10.0, _key(two): 0.0}
    # ... and any other station in station_plays.
    assert library.last_played("icecold") == {_key(one): 20.0, _key(two): 30.0}
    assert [row["event_id"] for row in main.recent_history()] == ["main-1"]
    assert [row["event_id"] for row in ice.recent_history()] == ["ice-1"]
    assert [row["title"] for row in ice.recent_plays(10)] == ["Ice one"]
    assert [row["title"] for row in main.play_log()] == ["Main one"]
    assert [row["title"] for row in ice.play_log()] == ["Ice one"]
    assert library.get_setting("clock") == {"name": "Morning", "cursor": 1}
    assert library.get_setting("clock:icecold") == {"name": "Night", "cursor": 4}
    assert main.get_setting("clock") == {"name": "Morning", "cursor": 1}
    assert ice.get_setting("clock") == {"name": "Night", "cursor": 4}
    assert ice.pending_history() == []


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
        assert reopened.play_log("icecold") == []
        assert reopened.last_played("icecold") == {}
        columns = {row[1] for row in reopened._db().execute("PRAGMA table_info(play_history)")}
        assert "station" in columns


def test_the_play_log_export_is_per_station(catalog) -> None:
    library, (one, two) = catalog
    library.write_history([_row("main-1", one, "Main one")])
    library.write_history([_row("ice-1", two, "Ice one", 1)], station="icecold")

    def titles(station: str = DEFAULT_STATION) -> list[str]:
        plays = plays_between(
            library, T0, T0 + timedelta(hours=1), read_tags=lambda _p: ("", ""), station=station
        )
        return [play.title for play in plays]

    assert titles() == ["Main one"]
    assert titles("icecold") == ["Ice one"]
    assert titles("other") == []


def test_a_moved_file_keeps_every_stations_last_play(tmp_path: Path, catalog) -> None:
    library, (one, _two) = catalog
    library.remember_play(one, 5.0, station="icecold")
    moved = tmp_path / "Music" / "moved.flac"
    one.rename(moved)
    assert library.rekey(one, moved)
    assert library.last_played("icecold") == {_key(moved): 5.0}


def test_the_library_flushes_and_reloads_every_ledger(tmp_path: Path) -> None:
    music = tmp_path / "Music"
    song = music / "song.flac"
    manager = LibraryManager(make_config(tmp_path, music=music), Similarity())
    try:
        ice = manager.add_ledger("icecold")
        assert manager.ledger.station == DEFAULT_SLUG
        manager.ledger.remember_play(song, 1.0)
        ice.remember_play(song, 2.0)
        ice.remember_history(_row("ice-1", song, "Ice"))
        manager.tick()
        assert manager.catalog.last_played("icecold") == {_key(song): 2.0}
        assert [row["event_id"] for row in ice.recent_history()] == ["ice-1"]
        assert manager.catalog.recent_history() == []

        # A station added later starts from what is saved for it.
        again = manager.add_ledger("icecold")
        assert again.last_played() == {_key(song): 2.0}
        ice.remember_play(song, 3.0)
    finally:
        manager.close()
    with LibraryCatalog(manager.cfg.index.active_dir / "library.db") as reopened:
        assert reopened.last_played("icecold") == {_key(song): 3.0}


def test_a_player_records_into_its_own_station() -> None:
    player = Player.__new__(Player)
    library = SimpleNamespace(ledger=SimpleNamespace(station=DEFAULT_SLUG))
    assert player._ledger is None
    player._library = library
    assert player._ledger is library.ledger
    own = SimpleNamespace(station="icecold")
    player._ledger = own
    assert player._ledger is own
