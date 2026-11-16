"""Station deletion keeps recovery and owned-audio boundaries explicit (#800)."""

from __future__ import annotations

import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from autodj.config import load_config
from autodj.station_deletion import (
    StationDeletionError,
    complete_station_deletion,
    delete_archived_station,
    recover_station_deletions,
)
from autodj.stations import ExtraStation, StationControlError, StationPaths, StationRegistry


def _config(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[index]\nindex_dir = "{(tmp_path / "index").as_posix()}"\n'
        '[stream]\nstation_name = "Main"\n'
        '[recording]\nfolder = "recordings"\n',
        encoding="utf-8",
    )
    return load_config(config_path, environ={})


def test_delete_quarantines_then_retains_station_audio_and_journal(tmp_path: Path) -> None:
    """ADR-0005-07 and ADR-0010-10: committed deletion retains spoken assets and its journal."""
    cfg = _config(tmp_path)
    paths = StationPaths.for_station(cfg, "nightowl")
    paths.state_dir.mkdir(parents=True)
    (paths.state_dir / "settings.json").write_text("{}", encoding="utf-8")
    paths.announcements.mkdir()
    (paths.announcements / "welcome.wav").write_bytes(b"spoken")

    result = delete_archived_station(
        cfg, "nightowl", station_name="Night Owl", delete_voice_recordings=False
    )

    assert not paths.state_dir.exists()
    retained = Path(result["retained_audio"])
    assert (retained / "announcements" / "welcome.wav").read_bytes() == b"spoken"
    manifest = json.loads((retained / "retention.json").read_text(encoding="utf-8"))
    assert manifest["station_id"] == "nightowl"
    assert manifest["station_name"] == "Night Owl"
    assert manifest["retained_paths"] == [f"{retained.name}/announcements"]
    journal = Path(result["journal"])
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "complete"

    complete_station_deletion(cfg, journal)
    assert not journal.exists()


def test_delete_refuses_music_source_overlap_without_moving_station(tmp_path: Path) -> None:
    """ADR-0010-11: a music root prevents moving station data into quarantine."""
    cfg = _config(tmp_path)
    paths = StationPaths.for_station(cfg, "nightowl")
    paths.state_dir.mkdir(parents=True)
    (paths.state_dir / "settings.json").write_text("{}", encoding="utf-8")

    with pytest.raises(StationDeletionError, match="protected"):
        delete_archived_station(
            cfg,
            "nightowl",
            station_name="Night Owl",
            delete_voice_recordings=True,
            music_roots=(paths.state_dir.parent,),
        )

    assert (paths.state_dir / "settings.json").is_file()


def test_default_station_cannot_be_permanently_deleted(tmp_path: Path) -> None:
    """ADR-0005-06 and ADR-0010-11: the default remains protected."""
    cfg = _config(tmp_path)
    with pytest.raises(StationDeletionError, match="default station"):
        delete_archived_station(cfg, "default", station_name="Main", delete_voice_recordings=True)


def test_completed_journal_recovery_is_forward_only_and_repeatable(tmp_path: Path) -> None:
    """ADR-0005-07 and ADR-0010-10: recovery keeps a committed deletion moving forward."""
    cfg = _config(tmp_path)
    paths = StationPaths.for_station(cfg, "nightowl")
    paths.state_dir.mkdir(parents=True)
    (paths.state_dir / "settings.json").write_text("{}", encoding="utf-8")
    result = delete_archived_station(
        cfg, "nightowl", station_name="Night Owl", delete_voice_recordings=True
    )

    messages = recover_station_deletions(cfg)
    assert messages == ["Finished deleting station nightowl."]
    assert not paths.state_dir.exists()
    assert Path(result["journal"]).is_file()


def test_read_only_config_and_doctor_leave_pending_deletion_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0010-10: only serve startup recovers journals; diagnostics never purge data."""
    from autodj import station_deletion
    from autodj.doctor import _station_deletion_checks

    cfg = _config(tmp_path)
    paths = StationPaths.for_station(cfg, "nightowl")
    paths.state_dir.mkdir(parents=True)
    (paths.state_dir / "settings.json").write_text("keep", encoding="utf-8")

    monkeypatch.setattr(
        station_deletion,
        "_finish",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("simulated interrupted cleanup")),
    )
    with pytest.raises(StationDeletionError, match="committed"):
        delete_archived_station(
            cfg, "nightowl", station_name="Night Owl", delete_voice_recordings=True
        )

    journal_dir = cfg.index.index_dir / ".station-deletions"
    journal = next(journal_dir.glob("*.json"))
    quarantine = next((journal_dir / "quarantine").iterdir())
    original_contents = (quarantine / "settings.json").read_text(encoding="utf-8")
    loaded = load_config(cfg.config_path, environ={})
    checks = _station_deletion_checks(loaded)

    assert loaded.station_deletion_recovery_problem
    assert any(check.name == "station-deletion-recovery" for check in checks)
    assert journal.is_file()
    assert quarantine.is_dir()
    assert (quarantine / "settings.json").read_text(encoding="utf-8") == original_contents
    assert not paths.state_dir.exists()


def test_backup_and_deletion_serialize_station_audio_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0005-07 and ADR-0010-10: backup and deletion share the index publication lock."""
    from autodj import backup, station_deletion
    from autodj.catalog import LibraryCatalog
    from autodj.config import _build_station

    cfg = _config(tmp_path)
    spec = replace(
        _build_station("nightowl", {"stream": {"station_name": "Night Owl"}}, cfg),
        defined_in="web",
    )
    cfg.stations = (spec,)
    paths = StationPaths.for_station(spec.cfg, "nightowl")
    paths.announcements.mkdir(parents=True)
    asset = paths.announcements / "welcome.wav"
    asset.write_bytes(b"complete spoken audio")
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    with LibraryCatalog(cfg.index.library_dir / "library.db"):
        pass

    backup_entered = threading.Event()
    allow_backup = threading.Event()
    deletion_started = threading.Event()
    deletion_entered = threading.Event()
    real_backup = backup._create_backup
    real_delete = station_deletion._delete_archived_station_locked

    def paused_backup(*args, **kwargs):
        backup_entered.set()
        assert allow_backup.wait(10)
        return real_backup(*args, **kwargs)

    def observed_delete(*args, **kwargs):
        deletion_entered.set()
        return real_delete(*args, **kwargs)

    def start_delete():
        deletion_started.set()
        return delete_archived_station(
            cfg,
            "nightowl",
            station_name="Night Owl",
            delete_voice_recordings=False,
        )

    monkeypatch.setattr(backup, "_create_backup", paused_backup)
    monkeypatch.setattr(station_deletion, "_delete_archived_station_locked", observed_delete)
    archive_path = tmp_path / "before-delete.zip"
    with ThreadPoolExecutor(max_workers=2) as pool:
        backup_future = pool.submit(backup.create_backup, cfg, archive_path, force=False)
        assert backup_entered.wait(10)
        delete_future = pool.submit(start_delete)
        assert deletion_started.wait(10)
        assert not deletion_entered.wait(0.2)
        allow_backup.set()
        assert backup_future.result(timeout=15) == archive_path.resolve()
        deletion = delete_future.result(timeout=15)

    with zipfile.ZipFile(archive_path) as archive:
        assert (
            archive.read("stations/nightowl/announcements/welcome.wav") == b"complete spoken audio"
        )
        assert not any(name.startswith("retained-station-audio/") for name in archive.namelist())
    retained = Path(deletion["retained_audio"])
    assert (retained / "announcements" / "welcome.wav").read_bytes() == b"complete spoken audio"


async def test_archive_restore_preserves_autostart_without_starting(tmp_path: Path) -> None:
    """ADR-0005-06 and ADR-0010-09: restore preserves autostart without starting."""
    cfg = _config(tmp_path)
    default = ExtraStation(
        slug="default",
        name="Main",
        autostart=False,
        paths=StationPaths.default(cfg),
        preset="",
        build=lambda _registry: None,
    )
    nightowl = ExtraStation(
        slug="nightowl",
        name="Night Owl",
        autostart=True,
        paths=StationPaths.for_station(cfg, "nightowl"),
        preset="",
        build=lambda _registry: None,
    )
    registry = StationRegistry(default, [nightowl])

    assert await registry.archive("nightowl") == []
    assert registry._extras["nightowl"].archived is True
    assert registry._extras["nightowl"].autostart is True
    with pytest.raises(StationControlError, match="archived"):
        await registry.start("nightowl")

    await registry.restore("nightowl")
    assert registry._extras["nightowl"].archived is False
    assert registry._extras["nightowl"].autostart is True
    assert "nightowl" not in registry._running
    marker = json.loads(nightowl.paths.archive_marker.read_text(encoding="utf-8"))
    assert marker["archived"] is False


async def test_archive_stops_the_station_before_writing_its_marker(tmp_path: Path) -> None:
    """ADR-0005-06 and ADR-0010-09: archive requests a complete stop before changing availability."""
    cfg = _config(tmp_path)
    default = ExtraStation(
        slug="default",
        name="Main",
        autostart=False,
        paths=StationPaths.default(cfg),
        preset="",
        build=lambda _registry: None,
    )
    nightowl = ExtraStation(
        slug="nightowl",
        name="Night Owl",
        autostart=True,
        paths=StationPaths.for_station(cfg, "nightowl"),
        preset="",
        build=lambda _registry: None,
    )
    registry = StationRegistry(default, [nightowl])
    registry.stop = AsyncMock(return_value=[])

    await registry.archive("nightowl")

    registry.stop.assert_awaited_once_with("nightowl")
    assert registry._extras["nightowl"].archived is True


async def test_archive_refuses_a_stop_with_warnings_or_a_stuck_player(tmp_path: Path) -> None:
    """ADR-0005-06 and ADR-0010-09: incomplete shutdown never archives a station."""
    cfg = _config(tmp_path)
    default = ExtraStation(
        slug="default",
        name="Main",
        autostart=False,
        paths=StationPaths.default(cfg),
        preset="",
        build=lambda _registry: None,
    )
    nightowl = ExtraStation(
        slug="nightowl",
        name="Night Owl",
        autostart=True,
        paths=StationPaths.for_station(cfg, "nightowl"),
        preset="",
        build=lambda _registry: None,
    )
    registry = StationRegistry(default, [nightowl])
    registry.stop = AsyncMock(return_value=["player still running"])

    with pytest.raises(StationControlError, match="did not stop cleanly"):
        await registry.archive("nightowl")
    assert not nightowl.paths.archive_marker.exists()

    registry.stop = AsyncMock(return_value=[])
    registry._stuck["nightowl"] = object()
    with pytest.raises(StationControlError, match="still stopping"):
        await registry.archive("nightowl")
    assert not nightowl.paths.archive_marker.exists()


def test_archive_marker_is_loaded_after_configuration_reload(tmp_path: Path) -> None:
    """ADR-0010-09: startup reload sees the durable archived state."""
    cfg = _config(tmp_path)
    paths = StationPaths.default(cfg)
    paths.archive_marker.parent.mkdir(parents=True, exist_ok=True)
    paths.archive_marker.write_text(
        json.dumps({"version": 1, "archived": True, "autostart": True}),
        encoding="utf-8",
    )

    reloaded = load_config(cfg.config_path, environ={})

    assert reloaded.default_station.archived is True
    assert reloaded.default_station.autostart == cfg.default_station.autostart
