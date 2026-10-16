"""Station database snapshots and migration of pre-ledger backups."""

from contextlib import ExitStack, closing

from autodj.backup import create_backup, restore_backup
from autodj.catalog import LibraryCatalog, _path
from autodj.station_store import StationStore
from tests.unit.test_library import make_entry
from tests.unit.test_station_backup import _config


def test_station_backup_includes_live_wal_and_unconfigured_station(tmp_path):
    cfg = _config(tmp_path / "source")
    song = str(tmp_path / "song.flac")
    with ExitStack() as stack:
        stores = {
            slug: stack.enter_context(
                closing(StationStore(cfg.index.stations_dir / slug / "station.db", slug))
            )
            for slug in ("default", "nightowl", "removed")
        }
        for number, (slug, store) in enumerate(stores.items(), 1):
            store.remember_play(song, number)
            store.set_setting("clock", slug)
            store.write_history(
                [
                    {
                        "event_id": slug,
                        "path": song,
                        "title": slug,
                        "artist": "Artist",
                        "duration": 30,
                        "played_at": "2026-10-01T12:00:00+00:00",
                        "album": "Album",
                    }
                ],
                [],
            )
        archive = create_backup(cfg, tmp_path / "stations.zip", force=False)
        for store in stores.values():
            store.remember_play(song, 999)

    destination = _config(tmp_path / "destination")
    restore_backup(destination, archive, force=True)
    for number, slug in enumerate(("default", "nightowl", "removed"), 1):
        with closing(
            StationStore(destination.index.stations_dir / slug / "station.db", slug)
        ) as store:
            assert store.last_played()[_path(song)] == number
            assert store.get_setting("clock") == slug
            assert store.play_log()[0]["album"] == "Album"
    assert not (destination.index.stations_dir / "removed" / "station.json").exists()


def test_restoring_legacy_backup_replaces_newer_station_database(tmp_path):
    cfg = _config(tmp_path / "source")
    song = cfg.library.music_dir / "song.flac"
    song.write_bytes(b"audio")
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        catalog.add_source(cfg.library.music_dir)
        catalog.put_entry(make_entry(song))
        catalog.remember_play(song, 10)
        catalog.remember_play(song, 20, station="nightowl")
        catalog.set_setting("clock", "Main")
        catalog.set_setting("clock:nightowl", "Night")
        catalog.write_history(
            [
                {
                    "event_id": "old",
                    "path": str(song),
                    "title": "Old",
                    "artist": "Artist",
                    "duration": 30,
                    "played_at": "2026-10-01T12:00:00+00:00",
                }
            ],
            [],
            station="nightowl",
        )
    archive = create_backup(cfg, tmp_path / "legacy.zip", force=False)
    for slug in ("default", "nightowl"):
        with closing(StationStore(cfg.index.stations_dir / slug / "station.db", slug)) as store:
            store.remember_play(str(song), 999)
            store.set_setting("clock", "Newer")
    for _retry in range(2):
        restore_backup(cfg, archive, force=True)
        for slug, timestamp, clock in (("default", 10, "Main"), ("nightowl", 20, "Night")):
            with closing(StationStore(cfg.index.stations_dir / slug / "station.db", slug)) as store:
                assert store.last_played()[_path(song)] == timestamp
                assert store.get_setting("clock") == clock
                if slug == "nightowl":
                    assert len(store.play_log()) == 1


def test_failed_restore_rolls_back_station_database(tmp_path, monkeypatch):
    import pytest

    from autodj import backup

    cfg = _config(tmp_path / "source")
    path = cfg.index.stations_dir / "default" / "station.db"
    with closing(StationStore(path, "default")) as store:
        store.remember_play("song", 1)
    archive = create_backup(cfg, tmp_path / "stations.zip", force=False)
    with closing(StationStore(path, "default")) as store:
        store.remember_play("song", 2)
    put = backup._Swap.put

    def fail_after_database(swap, source, target):
        put(swap, source, target)
        if target == path:
            raise OSError("injected install failure")

    monkeypatch.setattr(backup._Swap, "put", fail_after_database)
    with pytest.raises(backup.BackupError):
        restore_backup(cfg, archive, force=True)
    with closing(StationStore(path, "default")) as store:
        assert store.last_played()[_path("song")] == 2
    assert backup.pending_restore_state(cfg.index.library_dir) is None
