"""Station database snapshots and restore rollback."""

from contextlib import ExitStack, closing

from autodj.backup import create_backup, restore_backup
from autodj.catalog import _path
from autodj.station_store import StationStore
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


def test_older_backup_format_is_refused_before_installation(tmp_path):
    import json
    import zipfile

    import pytest

    from autodj.backup import BackupError
    from autodj.version import current_version

    cfg = _config(tmp_path / "source")
    archive = tmp_path / "old.zip"
    with zipfile.ZipFile(archive, "w") as opened:
        opened.writestr(
            "manifest.json", json.dumps({"autodj_version": current_version(), "files": []})
        )
    database = cfg.index.library_dir / "library.db"
    before = database.read_bytes()
    with pytest.raises(BackupError, match="unsupported backup format"):
        restore_backup(cfg, archive, force=True)
    assert database.read_bytes() == before
