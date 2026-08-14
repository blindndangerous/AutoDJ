"""Coherent backup publication and transactional restore tests."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import stat
import struct
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote
from zipfile import ZipInfo

import numpy as np
import pytest
from click.testing import CliRunner

import autodj.backup as backup
from autodj.backup import (
    MAX_MANIFEST_BYTES,
    BackupError,
    RestoreResult,
    _new_backup_recovery,
    _new_failed_backup_path,
    _open_regular_source,
    _preflight_zip_metadata,
    _required_free_space,
    _revalidate_ancestor_identities,
    _sqlite_snapshot,
    _validate_member_info,
    create_backup,
    restore_backup,
)
from autodj.cli import cli
from autodj.config import (
    AutoDJConfig,
    HuggingFaceConfig,
    IndexConfig,
    LibraryConfig,
    ModelConfig,
    PlaybackConfig,
)
from autodj.doctor import CheckStatus, DoctorCheck, DoctorReport
from autodj.index_manifest import (
    IndexConsistencyError,
    IndexManifest,
    current_snapshot_token,
    read_manifest,
    tombstone_publication,
)
from autodj.indexer import FEATURE_DIM, IndexEntry, save_index
from autodj.version import current_version


def _config(root: Path) -> AutoDJConfig:
    music = root / "music"
    index = root / "index"
    models = root / "models"
    music.mkdir(parents=True)
    index.mkdir(parents=True)
    models.mkdir(parents=True)
    cfg = AutoDJConfig(
        library=LibraryConfig(music, None, ["flac"]),
        index=IndexConfig(index, models, "default"),
        playback=PlaybackConfig(),
        model=ModelConfig(),
        huggingface=HuggingFaceConfig(None),
        config_path=root / "config.toml",
    )
    cfg.index.active_dir.mkdir()
    return cfg


def _published_index(cfg: AutoDJConfig, *, title: str = "Song") -> None:
    entry = IndexEntry(
        path=str(cfg.library.music_dir / "song.flac"),
        title=title,
        artist="Artist",
        album="",
        genre="",
        bpm=0.0,
        year=0,
        length=1.0,
        energy=0.0,
        key=-1,
        mode=-1,
        tempo_confidence=0.0,
    )
    vectors = np.zeros((1, FEATURE_DIM), dtype=np.float32)
    vectors[0, 0] = 1.0
    save_index([entry], vectors, cfg.index.active_dir, cfg.library.music_dir)


def _sqlite(path: Path, value: str) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE data(value TEXT)")
        connection.execute("INSERT INTO data VALUES (?)", (value,))
        connection.commit()


def _item(
    archive_path: str,
    destination: str,
    payload: bytes,
    *,
    classification: str = "derived",
) -> dict[str, object]:
    return {
        "archive_path": archive_path,
        "classification": classification,
        "destination": destination,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _archive(
    path: Path,
    payloads: list[tuple[str, bytes]],
    *,
    items: object | None = None,
    schema: object = 1,
    version: object | None = None,
    manifest_overrides: dict[str, object] | None = None,
) -> Path:
    if items is None:
        items = [_item(name, f"active/{Path(name).name}", data) for name, data in payloads]
    manifest = {
        "schema_version": schema,
        "autodj_version": current_version() if version is None else version,
        "created_at": "2026-08-14T00:00:00+00:00",
        "index_name": "default",
        "mode": "stopped",
        "items": items,
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in payloads:
            zf.writestr(name, data)
        zf.writestr("manifest.json", json.dumps(manifest))
    return path


def _rewrite_archive_payloads(archive: Path, replacements: dict[str, bytes]) -> None:
    with zipfile.ZipFile(archive) as zf:
        payloads = {info.filename: zf.read(info) for info in zf.infolist()}
    manifest = json.loads(payloads["manifest.json"])
    for item in manifest["items"]:
        archive_path = item["archive_path"]
        replacement = replacements.get(archive_path)
        if replacement is None:
            continue
        item["size"] = len(replacement)
        item["sha256"] = hashlib.sha256(replacement).hexdigest()
    payloads.update(replacements)
    payloads["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, payload in payloads.items():
            zf.writestr(name, payload)


def test_stopped_backup_contains_derived_unique_and_schema_manifest(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    _sqlite(cfg.index.active_dir / "dj_meta.db", "cue")
    (cfg.index.active_dir / "web_state.json").write_text("{}", encoding="utf-8")
    liners = cfg.index.active_dir / "liners"
    liners.mkdir()
    (liners / "station.wav").write_bytes(b"liner")

    archive = create_backup(cfg, tmp_path / "backup.zip", online=False)

    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        names = set(zf.namelist())
    assert manifest["schema_version"] == 1
    assert {entry["classification"] for entry in manifest["items"]} == {
        "derived",
        "unique",
    }
    assert {
        "derived/tracks.db",
        "derived/vectors.index",
        "derived/index-manifest.json",
        "derived/dj_meta.db",
        "unique/liners/station.wav",
        "unique/web_state/web_state.json",
        "manifest.json",
    }.issubset(names)
    assert all(entry["size"] >= 0 for entry in manifest["items"])
    assert all(len(entry["sha256"]) == 64 for entry in manifest["items"])


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_stopped_backup_refuses_preexisting_sqlite_sidecar(tmp_path: Path, suffix: str) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"
    Path(f"{cfg.index.active_dir / 'tracks.db'}{suffix}").write_bytes(b"active")

    with pytest.raises(BackupError, match="--online"):
        create_backup(cfg, destination, online=False)

    assert not destination.exists()


def test_stopped_backup_refuses_sidecar_appearing_during_copy_and_preserves_destination(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    metadata = cfg.index.active_dir / "dj_meta.db"
    _sqlite(metadata, "cue")
    destination = tmp_path / "backup.zip"
    destination.write_bytes(b"prior")
    from shutil import copy2 as real_copy2

    def copy_then_activate(source: Path, target: Path) -> None:
        real_copy2(source, target)
        if source == metadata:
            Path(f"{metadata}-wal").write_bytes(b"active")

    with (
        patch("autodj.backup.shutil.copy2", side_effect=copy_then_activate),
        pytest.raises(BackupError, match="changed during stopped-mode snapshot"),
    ):
        create_backup(cfg, destination, online=False, force=True)

    assert destination.read_bytes() == b"prior"


def test_online_backup_sqlite_snapshot_contains_committed_wal_row(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    database = cfg.index.active_dir / "dj_meta.db"
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE data(value TEXT)")
    writer.execute("INSERT INTO data VALUES ('committed')")
    writer.commit()
    try:
        archive = create_backup(cfg, tmp_path / "backup.zip", online=True)
    finally:
        writer.close()
    restored = tmp_path / "restored.db"
    with zipfile.ZipFile(archive) as zf:
        restored.write_bytes(zf.read("derived/dj_meta.db"))
    with closing(sqlite3.connect(restored)) as connection:
        assert connection.execute("SELECT value FROM data").fetchone() == ("committed",)


def test_online_sqlite_snapshot_allows_source_to_change_after_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _sqlite(source, "first")
    real_connect = sqlite3.connect
    source_connection = real_connect(source)

    class SnapshotThenWrite:
        def backup(self, destination: sqlite3.Connection) -> None:
            source_connection.backup(destination)
            with source.open("ab") as changed:
                changed.write(b"source changed after consistent snapshot")

        def close(self) -> None:
            source_connection.close()

    connections: list[object] = [SnapshotThenWrite(), real_connect(target)]
    with patch("autodj.backup.sqlite3.connect", side_effect=connections):
        _sqlite_snapshot(source, target, tmp_path)
    with closing(real_connect(target)) as restored:
        assert restored.execute("SELECT value FROM data ORDER BY rowid").fetchall() == [("first",)]


def test_online_sqlite_snapshot_opens_encoded_read_only_uri_without_recreating_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "database with spaces.db"
    target = tmp_path / "target.db"
    _sqlite(source, "first")
    real_connect = sqlite3.connect
    resolved = source.resolve(strict=True)
    expected_uri = (
        f"file://{quote(resolved.as_posix(), safe='/')}?mode=ro"
        if resolved.as_posix().startswith("//")
        else f"{resolved.as_uri()}?mode=ro"
    )

    def disappear_before_connect(
        database: object, *args: object, **kwargs: object
    ) -> sqlite3.Connection:
        assert database == expected_uri
        assert kwargs.get("uri") is True
        source.unlink()
        return real_connect(database, *args, **kwargs)  # type: ignore[arg-type]

    with (
        patch("autodj.backup.sqlite3.connect", side_effect=disappear_before_connect),
        pytest.raises(BackupError, match="could not be opened read-only"),
    ):
        _sqlite_snapshot(source, target, tmp_path)

    assert not source.exists()


def test_unique_source_changed_after_eof_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "liner.wav"
    source.write_bytes(b"original")

    with (
        pytest.raises(BackupError, match="changed while it was read"),
        _open_regular_source(source, tmp_path) as handle,
    ):
        assert handle.read() == b"original"
        source.write_bytes(b"changed content")


@pytest.mark.parametrize("target_is_directory", [False, True])
def test_backup_rejects_file_and_directory_symlinks(
    tmp_path: Path, target_is_directory: bool
) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    liners = cfg.index.active_dir / "liners"
    liners.mkdir()
    outside = tmp_path / ("outside" if target_is_directory else "outside.wav")
    if target_is_directory:
        outside.mkdir()
        (outside / "secret.wav").write_bytes(b"secret")
    else:
        outside.write_bytes(b"secret")
    link = liners / ("linked" if target_is_directory else "linked.wav")
    try:
        link.symlink_to(outside, target_is_directory=target_is_directory)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(BackupError, match="symbolic link"):
        create_backup(cfg, tmp_path / "backup.zip", online=False)


def test_backup_rejects_unique_root_with_symlink_ancestor(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    outside = tmp_path / "outside-liners"
    outside.mkdir()
    (outside / "station.wav").write_bytes(b"liner")
    linked = tmp_path / "linked-liners"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    cfg = replace(cfg, playback=replace(cfg.playback, liners_folder=linked))

    with pytest.raises(BackupError, match=r"symbolic link|reparse"):
        create_backup(cfg, tmp_path / "backup.zip", online=False)


@pytest.mark.parametrize("label", ["profiles", "liners", "dayparts"])
def test_backup_rejects_existing_destination_inside_unique_source_root(
    tmp_path: Path, label: str
) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    if label == "profiles":
        root = cfg.index.active_dir.parent / "profiles"
    else:
        root = tmp_path / label
        field = "liners_folder" if label == "liners" else "dayparts_dir"
        cfg = replace(cfg, playback=replace(cfg.playback, **{field: root}))
    root.mkdir(parents=True, exist_ok=True)
    destination = root / "backup.zip"
    destination.write_bytes(b"prior archive")

    with (
        patch("autodj.backup._write_backup_archive") as write,
        pytest.raises(BackupError, match="inside configured unique backup source"),
    ):
        create_backup(cfg, destination, online=False, force=True)

    write.assert_not_called()
    assert destination.read_bytes() == b"prior archive"


@pytest.mark.parametrize("nested", [False, True])
def test_backup_rejects_identical_or_overlapping_unique_roots(tmp_path: Path, nested: bool) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    profiles = cfg.index.active_dir.parent / "profiles"
    profiles.mkdir()
    liners = profiles / "nested" if nested else profiles
    liners.mkdir(exist_ok=True)
    cfg = replace(cfg, playback=replace(cfg.playback, liners_folder=liners))

    with (
        patch("autodj.backup._write_backup_archive") as write,
        pytest.raises(BackupError, match="overlapping unique backup roots"),
    ):
        create_backup(cfg, tmp_path / "backup.zip", online=False)

    write.assert_not_called()


def test_failed_forced_create_preserves_existing_destination_and_cleans_temp(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    destination = tmp_path / "backup.zip"
    destination.write_bytes(b"prior archive")

    def fail_after_write(_cfg: AutoDJConfig, archive: Path, *, online: bool) -> None:
        assert online is False
        archive.write_bytes(b"partial")
        raise OSError("injected")

    with (
        patch("autodj.backup._write_backup_archive", side_effect=fail_after_write),
        pytest.raises(BackupError, match="backup creation failed"),
    ):
        create_backup(cfg, destination, online=False, force=True)

    assert destination.read_bytes() == b"prior archive"
    assert not list(tmp_path.glob(".backup.zip.backup-*.tmp"))


def test_create_without_force_rejects_and_force_replaces(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"
    destination.write_bytes(b"prior")
    with pytest.raises(BackupError, match="--force"):
        create_backup(cfg, destination, online=False)
    assert destination.read_bytes() == b"prior"

    create_backup(cfg, destination, online=False, force=True)

    with zipfile.ZipFile(destination) as zf:
        assert "manifest.json" in zf.namelist()


def test_forced_create_directory_fsync_failure_restores_existing_archive(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"
    destination.write_bytes(b"prior archive")
    sync_calls = 0

    def fail_published_sync(_path: Path) -> None:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 2:
            raise OSError("sync failed")

    with (
        patch("autodj.backup._fsync_directory", side_effect=fail_published_sync),
        pytest.raises(BackupError, match="directory sync"),
    ):
        create_backup(cfg, destination, online=False, force=True)

    assert destination.read_bytes() == b"prior archive"
    assert not list(tmp_path.glob(".backup.zip.backup-*.tmp"))
    assert not list(tmp_path.glob(".backup.zip.backup-old-*"))


def test_forced_create_keeps_recovery_reservation_until_atomic_replace(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"
    destination.write_bytes(b"prior archive")
    real_replace = os.replace
    real_unlink = Path.unlink
    old_was_replaced = False

    def observe_replace(source: Path, target: Path) -> None:
        nonlocal old_was_replaced
        if source == destination and ".backup-old-" in Path(target).name:
            assert Path(target).is_file()
            old_was_replaced = True
        real_replace(source, target)

    def reject_early_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if ".backup-old-" in path.name and not old_was_replaced:
            raise AssertionError("recovery reservation unlinked before replace")
        real_unlink(path, *args, **kwargs)

    with (
        patch("autodj.backup.os.replace", side_effect=observe_replace),
        patch("autodj.backup.Path.unlink", new=reject_early_unlink),
    ):
        create_backup(cfg, destination, online=False, force=True)

    assert old_was_replaced
    assert not list(tmp_path.glob(".backup.zip.backup-old-*"))


def test_failed_old_archive_move_never_installs_empty_recovery_reservation(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"
    destination.write_bytes(b"prior archive")
    real_replace = os.replace

    def fail_old_move(source: Path, target: Path) -> None:
        if source == destination and ".backup-old-" in Path(target).name:
            raise PermissionError("old move blocked")
        real_replace(source, target)

    with (
        patch("autodj.backup.os.replace", side_effect=fail_old_move),
        pytest.raises(BackupError, match="old move blocked"),
    ):
        create_backup(cfg, destination, online=False, force=True)

    assert destination.read_bytes() == b"prior archive"
    assert not list(tmp_path.glob(".backup.zip.backup-old-*"))


@pytest.mark.parametrize("phase", ["replace", "post-reconcile", "post-replace-sync"])
def test_forced_backup_reconciles_populated_recovery_after_control_exception(
    tmp_path: Path, phase: str
) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"
    destination.write_bytes(b"prior archive")
    real_replace = os.replace
    from autodj.backup import _reserved_move_completed as real_reconcile

    reconcile_interrupted = False
    sync_interrupted = False

    def replace_then_interrupt(source: Path, target: Path) -> None:
        real_replace(source, target)
        if phase == "replace" and source == destination and ".backup-old-" in Path(target).name:
            raise KeyboardInterrupt("stop after replace")

    def reconcile_then_interrupt(*args: object, **kwargs: object) -> bool:
        nonlocal reconcile_interrupted
        result = real_reconcile(*args, **kwargs)  # type: ignore[arg-type]
        if phase == "post-reconcile" and result and not reconcile_interrupted:
            reconcile_interrupted = True
            raise KeyboardInterrupt("stop after reconcile")
        return result

    def interrupt_post_replace_sync(_path: Path) -> None:
        nonlocal sync_interrupted
        if phase == "post-replace-sync" and not sync_interrupted:
            sync_interrupted = True
            raise KeyboardInterrupt("stop after guard")

    with (
        patch("autodj.backup.os.replace", side_effect=replace_then_interrupt),
        patch(
            "autodj.backup._reserved_move_completed",
            side_effect=reconcile_then_interrupt,
        ),
        patch("autodj.backup._fsync_directory", side_effect=interrupt_post_replace_sync),
        pytest.raises(KeyboardInterrupt, match="stop after"),
    ):
        create_backup(cfg, destination, online=False, force=True)

    assert destination.read_bytes() == b"prior archive"
    assert not list(tmp_path.glob(".backup.zip.backup-old-*"))


def test_failed_old_archive_move_reports_unremovable_empty_reservation(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"
    destination.write_bytes(b"prior archive")
    real_replace = os.replace
    real_unlink = Path.unlink

    def fail_old_move(source: Path, target: Path) -> None:
        if source == destination and ".backup-old-" in Path(target).name:
            raise PermissionError("old move blocked")
        real_replace(source, target)

    def fail_reservation_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        if ".backup-old-" in path.name:
            raise PermissionError("cleanup blocked")
        real_unlink(path, *args, **kwargs)

    with (
        patch("autodj.backup.os.replace", side_effect=fail_old_move),
        patch("autodj.backup.Path.unlink", new=fail_reservation_cleanup),
        pytest.raises(BackupError, match="empty recovery reservation retained at") as raised,
    ):
        create_backup(cfg, destination, online=False, force=True)

    reservations = list(tmp_path.glob(".backup.zip.backup-old-*"))
    assert len(reservations) == 1
    assert str(reservations[0]) in str(raised.value)
    assert destination.read_bytes() == b"prior archive"


def test_backup_recovery_cleanup_failure_rolls_back_to_old_destination(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"
    destination.write_bytes(b"prior archive")
    real_unlink = Path.unlink

    def fail_recovery_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        if ".backup-old-" in path.name:
            raise PermissionError("cleanup blocked")
        real_unlink(path, *args, **kwargs)

    with (
        patch("autodj.backup.Path.unlink", new=fail_recovery_cleanup),
        pytest.raises(BackupError, match="recovery cleanup failed"),
    ):
        create_backup(cfg, destination, online=False, force=True)

    assert destination.read_bytes() == b"prior archive"
    assert not list(tmp_path.glob(".backup.zip.backup-old-*"))


@pytest.mark.parametrize("phase", ["replace", "post-reconcile"])
def test_backup_cleanup_rollback_reconciles_control_after_successful_move(
    tmp_path: Path, phase: str
) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"
    destination.write_bytes(b"prior archive")
    real_replace = os.replace
    real_unlink = Path.unlink
    from autodj.backup import _backup_cleanup_rollback_completed as real_reconcile

    reconcile_interrupted = False

    def fail_recovery_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        if ".backup-old-" in path.name:
            raise PermissionError("cleanup blocked")
        real_unlink(path, *args, **kwargs)

    def restore_then_interrupt(source: Path, target: Path) -> None:
        real_replace(source, target)
        if phase == "replace" and ".backup-old-" in Path(source).name and target == destination:
            raise KeyboardInterrupt("stop after cleanup rollback")

    def reconcile_then_interrupt(*args: object, **kwargs: object) -> bool:
        nonlocal reconcile_interrupted
        result = real_reconcile(*args, **kwargs)  # type: ignore[arg-type]
        if phase == "post-reconcile" and result and not reconcile_interrupted:
            reconcile_interrupted = True
            raise KeyboardInterrupt("stop after cleanup rollback reconcile")
        return result

    with (
        patch("autodj.backup.Path.unlink", new=fail_recovery_cleanup),
        patch("autodj.backup.os.replace", side_effect=restore_then_interrupt),
        patch(
            "autodj.backup._backup_cleanup_rollback_completed",
            side_effect=reconcile_then_interrupt,
        ),
        pytest.raises(KeyboardInterrupt, match="stop after cleanup rollback"),
    ):
        create_backup(cfg, destination, online=False, force=True)

    assert destination.read_bytes() == b"prior archive"
    assert not list(tmp_path.glob(".backup.zip.backup-old-*"))


def test_backup_recovery_cleanup_and_rollback_failure_reports_retained_old_copy(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"
    destination.write_bytes(b"prior archive")
    real_replace = os.replace
    real_unlink = Path.unlink

    def fail_recovery_rollback(source: Path, target: Path) -> None:
        if ".backup-old-" in Path(source).name and target == destination:
            raise PermissionError("rollback blocked")
        real_replace(source, target)

    def fail_recovery_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        if ".backup-old-" in path.name:
            raise PermissionError("cleanup blocked")
        real_unlink(path, *args, **kwargs)

    with (
        patch("autodj.backup.os.replace", side_effect=fail_recovery_rollback),
        patch("autodj.backup.Path.unlink", new=fail_recovery_cleanup),
        pytest.raises(BackupError, match="recovery copy retained at") as raised,
    ):
        create_backup(cfg, destination, online=False, force=True)

    retained = list(tmp_path.glob(".backup.zip.backup-old-*"))
    assert len(retained) == 1
    assert retained[0].read_bytes() == b"prior archive"
    assert str(retained[0]) in str(raised.value)
    with zipfile.ZipFile(destination) as zf:
        assert "manifest.json" in zf.namelist()


@pytest.mark.parametrize("factory", [_new_backup_recovery, _new_failed_backup_path])
def test_backup_reservation_close_failure_cleans_placeholder(
    tmp_path: Path,
    factory: object,
) -> None:
    destination = tmp_path / "backup.zip"
    real_close = os.close

    def close_then_fail(descriptor: int) -> None:
        real_close(descriptor)
        raise OSError("close failed")

    with (
        patch("autodj.backup.os.close", side_effect=close_then_fail),
        pytest.raises(OSError, match="close failed"),
    ):
        factory(destination)  # type: ignore[operator]

    assert not list(tmp_path.glob(".backup.zip.backup-*-*"))


def test_first_create_directory_fsync_failure_removes_published_archive(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"

    with (
        patch("autodj.backup._fsync_directory", side_effect=OSError("sync failed")),
        pytest.raises(BackupError, match="directory sync"),
    ):
        create_backup(cfg, destination, online=False)

    assert not destination.exists()
    assert not list(tmp_path.glob(".backup.zip.backup-*.tmp"))
    assert not list(tmp_path.glob(".backup.zip.backup-old-*"))


def test_first_create_fsync_failure_moves_unremovable_archive_out_of_destination(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"
    real_unlink = Path.unlink
    real_replace = os.replace
    failed_was_replaced = False

    def block_destination_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if ".backup-failed-" in path.name and not failed_was_replaced:
            raise AssertionError("failed-backup reservation unlinked before replace")
        if path == destination:
            raise PermissionError("unlink blocked")
        real_unlink(path, *args, **kwargs)

    def observe_replace(source: Path, target: Path) -> None:
        nonlocal failed_was_replaced
        if source == destination and ".backup-failed-" in Path(target).name:
            assert Path(target).is_file()
            failed_was_replaced = True
        real_replace(source, target)

    with (
        patch("autodj.backup._fsync_directory", side_effect=OSError("sync failed")),
        patch("autodj.backup.Path.unlink", new=block_destination_unlink),
        patch("autodj.backup.os.replace", side_effect=observe_replace),
        pytest.raises(BackupError, match="new archive retained at") as raised,
    ):
        create_backup(cfg, destination, online=False)

    assert not destination.exists()
    retained = list(tmp_path.glob(".backup.zip.backup-failed-*"))
    assert len(retained) == 1
    assert failed_was_replaced
    assert str(retained[0]) in str(raised.value)
    assert not list(tmp_path.glob(".backup.zip.backup-*.tmp"))


def test_failed_quarantine_move_cleans_empty_reservation_and_reports_destination(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"
    real_replace = os.replace
    real_unlink = Path.unlink

    def block_destination_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == destination:
            raise PermissionError("unlink blocked")
        real_unlink(path, *args, **kwargs)

    def fail_quarantine_move(source: Path, target: Path) -> None:
        if source == destination and ".backup-failed-" in Path(target).name:
            raise PermissionError("quarantine move blocked")
        real_replace(source, target)

    with (
        patch("autodj.backup._fsync_directory", side_effect=OSError("sync failed")),
        patch("autodj.backup.Path.unlink", new=block_destination_unlink),
        patch("autodj.backup.os.replace", side_effect=fail_quarantine_move),
        pytest.raises(BackupError, match="new archive remains at") as raised,
    ):
        create_backup(cfg, destination, online=False)

    assert destination.is_file()
    assert str(destination) in str(raised.value)
    assert not list(tmp_path.glob(".backup.zip.backup-failed-*"))


def test_failed_quarantine_move_reports_unremovable_empty_reservation(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"
    real_replace = os.replace
    real_unlink = Path.unlink

    def block_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        if path == destination:
            raise PermissionError("unlink blocked")
        if ".backup-failed-" in path.name:
            raise PermissionError("cleanup blocked")
        real_unlink(path, *args, **kwargs)

    def fail_quarantine_move(source: Path, target: Path) -> None:
        if source == destination and ".backup-failed-" in Path(target).name:
            raise PermissionError("quarantine move blocked")
        real_replace(source, target)

    with (
        patch("autodj.backup._fsync_directory", side_effect=OSError("sync failed")),
        patch("autodj.backup.Path.unlink", new=block_cleanup),
        patch("autodj.backup.os.replace", side_effect=fail_quarantine_move),
        pytest.raises(BackupError, match="empty quarantine reservation retained at") as raised,
    ):
        create_backup(cfg, destination, online=False)

    reservations = list(tmp_path.glob(".backup.zip.backup-failed-*"))
    assert len(reservations) == 1
    assert str(destination) in str(raised.value)
    assert str(reservations[0]) in str(raised.value)


def test_backup_cli_passes_explicit_force_and_online_values(tmp_path: Path) -> None:
    destination = tmp_path / "backup.zip"
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=SimpleNamespace()),
        patch("autodj.backup.create_backup", return_value=destination) as create,
    ):
        result = CliRunner().invoke(cli, ["backup", str(destination), "--force"])
    assert result.exit_code == 0, result.output
    create.assert_called_once()
    assert create.call_args.kwargs == {"online": False, "force": True}
    assert str(destination) in result.output


def test_online_generation_change_retries_with_latest_generation(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    original = read_manifest(cfg.index.active_dir)
    assert original is not None
    from autodj.index_manifest import copy_published_snapshot as real_copy

    calls = 0
    advanced: IndexManifest | None = None

    def flaky_copy(
        source: Path,
        destination: Path,
        *,
        expected_generation: int | None = None,
    ) -> IndexManifest:
        nonlocal advanced, calls
        calls += 1
        if calls == 1:
            assert expected_generation == original.generation
            _published_index(cfg, title="Advanced")
            advanced = read_manifest(cfg.index.active_dir)
            assert advanced is not None
            assert advanced.generation > original.generation
            raise IndexConsistencyError(f"generation changed to {advanced.generation}")
        assert advanced is not None
        assert expected_generation == advanced.generation
        return real_copy(source, destination, expected_generation=expected_generation)

    with patch("autodj.backup.copy_published_snapshot", side_effect=flaky_copy):
        create_backup(cfg, tmp_path / "backup.zip", online=True)
    assert calls == 2


def test_online_continuous_generation_churn_aborts_after_three_and_preserves(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"
    destination.write_bytes(b"prior")
    with (
        patch(
            "autodj.backup.copy_published_snapshot",
            side_effect=IndexConsistencyError("generation changed"),
        ) as copy,
        pytest.raises(BackupError, match="changed during 3 snapshot attempts"),
    ):
        create_backup(cfg, destination, online=True, force=True)
    assert copy.call_count == 3
    assert destination.read_bytes() == b"prior"


@pytest.mark.parametrize(
    ("schema", "version", "message"),
    [
        (999, current_version(), "schema 999"),
        (1, "99.99.0", "AutoDJ version 99.99.0"),
    ],
)
def test_restore_rejects_schema_and_incompatible_version_before_writing(
    tmp_path: Path, schema: int, version: str, message: str
) -> None:
    cfg = _config(tmp_path)
    archive = _archive(tmp_path / "bad.zip", [], schema=schema, version=version)
    with pytest.raises(BackupError, match=message):
        restore_backup(cfg, archive, force=False)
    assert list(cfg.index.active_dir.iterdir()) == []


@pytest.mark.parametrize(
    "unsafe",
    [
        "derived/../escaped.db",
        "derived\\escaped.db",
        "/absolute.db",
        "./derived.db",
        "derived/./tracks.db",
        "derived/",
    ],
)
def test_restore_rejects_unsafe_central_member_before_writing(tmp_path: Path, unsafe: str) -> None:
    cfg = _config(tmp_path)
    archive = _archive(tmp_path / "unsafe.zip", [(unsafe, b"payload")])
    with pytest.raises(BackupError, match=r"unsafe restore path|regular file"):
        restore_backup(cfg, archive, force=True)
    assert list(cfg.index.active_dir.iterdir()) == []


def test_restore_rejects_destination_traversal_before_writing(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    payload = b"payload"
    items = [_item("derived/tracks.db", "active/../escaped.db", payload)]
    archive = _archive(tmp_path / "traversal.zip", [("derived/tracks.db", payload)], items=items)
    with pytest.raises(BackupError, match="unsafe restore path"):
        restore_backup(cfg, archive, force=True)
    assert not (cfg.index.index_dir / "escaped.db").exists()


@pytest.mark.parametrize(
    ("archive_path", "destination", "classification"),
    [
        ("derived/tracks.db", "active/vectors.index", "derived"),
        ("derived/not-canonical.db", "active/not-canonical.db", "derived"),
        ("unique/liners/station.wav", "profiles/station.wav", "unique"),
        ("unique/active/station.wav", "active/station.wav", "unique"),
    ],
)
def test_restore_requires_exact_classification_path_destination_binding(
    tmp_path: Path,
    archive_path: str,
    destination: str,
    classification: str,
) -> None:
    cfg = _config(tmp_path)
    payload = b"payload"
    items = [_item(archive_path, destination, payload, classification=classification)]
    archive = _archive(tmp_path / "binding.zip", [(archive_path, payload)], items=items)

    with (
        patch("autodj.backup._stage_payloads") as stage,
        pytest.raises(BackupError, match="canonical mapping"),
    ):
        restore_backup(cfg, archive, force=True)

    stage.assert_not_called()


@pytest.mark.parametrize(
    "unsafe",
    [
        "unique/liners/trailing.",
        "unique/liners/trailing ",
        "unique/liners/CON.txt",
        "unique/liners/com1",
        "unique/liners/stream:secret",
    ],
)
def test_restore_rejects_win32_unsafe_components_on_every_platform(
    tmp_path: Path, unsafe: str
) -> None:
    cfg = _config(tmp_path)
    payload = b"payload"
    relative = unsafe.removeprefix("unique/liners/")
    items = [_item(unsafe, f"liners/{relative}", payload, classification="unique")]
    archive = _archive(tmp_path / "unsafe-win32.zip", [(unsafe, payload)], items=items)

    with (
        patch("autodj.backup._stage_payloads") as stage,
        pytest.raises(BackupError, match=r"unsafe.*path"),
    ):
        restore_backup(cfg, archive, force=True)

    stage.assert_not_called()


def test_restore_rejects_case_normalized_member_collision(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    first = "unique/liners/Station.wav"
    second = "unique/liners/station.wav"
    payloads = [(first, b"first"), (second, b"second")]
    items = [
        _item(first, "liners/Station.wav", b"first", classification="unique"),
        _item(second, "liners/station.wav", b"second", classification="unique"),
    ]
    archive = _archive(tmp_path / "normalized.zip", payloads, items=items)

    with pytest.raises(BackupError, match="normalized archive member"):
        restore_backup(cfg, archive, force=True)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("created_at", 1),
        ("index_name", False),
        ("mode", "surprise"),
    ],
)
def test_restore_validates_creator_manifest_fields_before_staging(
    tmp_path: Path, field: str, value: object
) -> None:
    cfg = _config(tmp_path)
    archive = _archive(tmp_path / "creator-field.zip", [], manifest_overrides={field: value})

    with (
        patch("autodj.backup._stage_payloads") as stage,
        pytest.raises(BackupError, match=field),
    ):
        restore_backup(cfg, archive, force=True)

    stage.assert_not_called()


def test_restore_rejects_nonfinite_json_constant_before_staging(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    archive = tmp_path / "nonfinite.zip"
    raw = (
        '{"schema_version":1,"autodj_version":"'
        + current_version()
        + '","created_at":"now","index_name":"default","mode":"stopped",'
        '"items":[],"extra":NaN}'
    )
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("manifest.json", raw)

    with (
        patch("autodj.backup._stage_payloads") as stage,
        pytest.raises(BackupError, match="non-finite"),
    ):
        restore_backup(cfg, archive, force=True)

    stage.assert_not_called()


def test_restore_rejects_central_size_mismatch_before_staging(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    payload = b"payload"
    declared = _item("derived/tracks.db", "active/tracks.db", payload)
    declared["size"] = len(payload) + 1
    archive = _archive(
        tmp_path / "mismatch.zip", [("derived/tracks.db", payload)], items=[declared]
    )
    with (
        patch("autodj.backup._stage_payloads") as stage,
        pytest.raises(BackupError, match="central-directory size"),
    ):
        restore_backup(cfg, archive, force=True)
    stage.assert_not_called()


def test_restore_rejects_compressed_manifest_bomb_before_parsing(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    archive = tmp_path / "manifest-bomb.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", b" " * (MAX_MANIFEST_BYTES + 1))
    with (
        patch("autodj.backup._parse_items") as parse,
        pytest.raises(BackupError, match="manifest exceeds 16 MiB"),
    ):
        restore_backup(cfg, archive, force=True)
    parse.assert_not_called()


def test_restore_rejects_oversized_central_directory_before_zipfile(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    archive = tmp_path / "oversized-central-directory.zip"
    archive.write_bytes(
        struct.pack(
            "<4s4H2LH",
            b"PK\x05\x06",
            0,
            0,
            1,
            1,
            MAX_MANIFEST_BYTES + 1,
            0,
            0,
        )
    )

    with (
        patch("autodj.backup.zipfile.ZipFile", side_effect=AssertionError("opened ZipFile")) as zf,
        pytest.raises(BackupError, match="central-directory metadata exceeds"),
    ):
        restore_backup(cfg, archive, force=True)

    zf.assert_not_called()


def test_restore_preflights_and_parses_same_open_archive_handle(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    payload = b"original payload"
    archive = _archive(tmp_path / "original.zip", [("derived/tracks.db", payload)])
    replacement = _archive(tmp_path / "replacement.zip", [])
    original_bytes = archive.read_bytes()
    real_open = Path.open
    open_calls = 0

    def one_archive_open(path: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        nonlocal open_calls
        if path == archive and mode == "rb":
            open_calls += 1
            if open_calls > 1:
                raise AssertionError("archive pathname opened more than once")
            return io.BytesIO(original_bytes)
        return real_open(path, mode, *args, **kwargs)

    def preflight_then_replace(value: object) -> None:
        _preflight_zip_metadata(value)  # type: ignore[arg-type]
        os.replace(replacement, archive)

    with (
        patch("autodj.backup.Path.open", new=one_archive_open),
        patch("autodj.backup._preflight_zip_metadata", side_effect=preflight_then_replace),
    ):
        result = restore_backup(cfg, archive, force=True)

    assert result.restored == 1
    assert (cfg.index.active_dir / "tracks.db").read_bytes() == payload
    assert open_calls == 1


def test_restore_rejects_encrypted_directory_symlink_and_special_member() -> None:
    encrypted = ZipInfo("derived/encrypted.db")
    encrypted.flag_bits |= 0x1
    directory = ZipInfo("derived/directory/")
    symlink = ZipInfo("derived/link.db")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    fifo = ZipInfo("derived/fifo")
    fifo.create_system = 3
    fifo.external_attr = (stat.S_IFIFO | 0o600) << 16
    for info, message in (
        (encrypted, "encrypted"),
        (directory, "regular file"),
        (symlink, "regular file"),
        (fifo, "regular file"),
    ):
        with pytest.raises(BackupError, match=message):
            _validate_member_info(info)


def test_restore_preflights_free_space_before_staging(tmp_path: Path) -> None:
    source = _config(tmp_path / "source")
    _published_index(source)
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    target = _config(tmp_path / "target")
    with (
        patch("autodj.backup.shutil.disk_usage", return_value=SimpleNamespace(free=0)),
        patch("autodj.backup._stage_payloads") as stage,
        pytest.raises(BackupError, match="insufficient free space"),
    ):
        restore_backup(target, archive, force=True)
    stage.assert_not_called()


def test_restore_creates_missing_configured_root_parent_chain(tmp_path: Path) -> None:
    cfg = _config(tmp_path / "target")
    cfg.playback.liners_folder = str(tmp_path / "target" / "custom" / "deep" / "liners")
    payload = b"liner"
    items = [
        _item(
            "unique/liners/station.wav",
            "liners/station.wav",
            payload,
            classification="unique",
        )
    ]
    archive = _archive(
        tmp_path / "liners.zip", [("unique/liners/station.wav", payload)], items=items
    )

    result = restore_backup(cfg, archive, force=False)

    assert result.restored == 1
    assert (Path(cfg.playback.liners_folder) / "station.wav").read_bytes() == payload


def test_stage_creation_failure_cleans_new_parent_directories(tmp_path: Path) -> None:
    cfg = _config(tmp_path / "target")
    root = tmp_path / "target" / "custom" / "deep" / "liners"
    cfg.playback.liners_folder = str(root)
    payload = b"liner"
    items = [
        _item(
            "unique/liners/station.wav",
            "liners/station.wav",
            payload,
            classification="unique",
        )
    ]
    archive = _archive(
        tmp_path / "liners.zip", [("unique/liners/station.wav", payload)], items=items
    )
    with (
        patch("autodj.backup.tempfile.mkstemp", side_effect=OSError("stage failed")),
        pytest.raises(BackupError, match="extraction failed"),
    ):
        restore_backup(cfg, archive, force=False)
    assert not root.exists()


@pytest.mark.parametrize("failure", [BackupError("ancestor changed"), KeyboardInterrupt("stop")])
def test_post_mkstemp_validation_failure_reports_unremovable_provisional_stage(
    tmp_path: Path, failure: BaseException
) -> None:
    cfg = _config(tmp_path)
    payload = b"liner"
    member = "unique/liners/station.wav"
    archive = _archive(
        tmp_path / "provisional-stage.zip",
        [(member, payload)],
        items=[_item(member, "liners/station.wav", payload, classification="unique")],
    )
    real_unlink = Path.unlink
    validation_failed = False

    def fail_after_mkstemp(identities: object, *, target: Path) -> None:
        nonlocal validation_failed
        if list(target.parent.glob(".*.restore-stage-*")) and not validation_failed:
            validation_failed = True
            raise failure
        _revalidate_ancestor_identities(identities, target=target)  # type: ignore[arg-type]

    def retain_stage(path: Path, *args: object, **kwargs: object) -> None:
        if ".restore-stage-" in path.name:
            raise PermissionError("unlink blocked")
        real_unlink(path, *args, **kwargs)

    with (
        patch("autodj.backup._revalidate_ancestor_identities", side_effect=fail_after_mkstemp),
        patch("autodj.backup.Path.unlink", new=retain_stage),
        pytest.raises(BackupError, match="retained restore stages") as raised,
    ):
        restore_backup(cfg, archive, force=True)

    retained = list((cfg.index.active_dir / "liners").glob(".*.restore-stage-*"))
    assert len(retained) == 1
    assert str(retained[0]) in str(raised.value)


def test_stage_descriptor_identity_is_captured_before_payload_write(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    payload = b"payload must not be written"
    member = "unique/liners/station.wav"
    archive = _archive(
        tmp_path / "stage-fstat.zip",
        [(member, payload)],
        items=[_item(member, "liners/station.wav", payload, classification="unique")],
    )
    real_fstat = os.fstat
    failed = False

    def fail_stage_fstat(descriptor: int) -> os.stat_result:
        nonlocal failed
        metadata = real_fstat(descriptor)
        stages = list((cfg.index.active_dir / "liners").glob(".*.restore-stage-*"))
        if stages and not failed:
            stage_metadata = stages[0].lstat()
            if (metadata.st_dev, metadata.st_ino) == (
                stage_metadata.st_dev,
                stage_metadata.st_ino,
            ):
                failed = True
                raise OSError("stage fstat failed")
        return metadata

    with (
        patch("autodj.backup.os.fstat", side_effect=fail_stage_fstat),
        pytest.raises(BackupError, match="retained restore stage") as raised,
    ):
        restore_backup(cfg, archive, force=True)

    retained = list((cfg.index.active_dir / "liners").glob(".*.restore-stage-*"))
    assert len(retained) == 1
    assert retained[0].read_bytes() == b""
    assert str(retained[0]) in str(raised.value)


def test_stage_path_validation_precedes_payload_write_and_reports_cleanup_failure(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    payload = b"payload"
    member = "unique/liners/station.wav"
    archive = _archive(
        tmp_path / "stage-path-validation.zip",
        [(member, payload)],
        items=[_item(member, "liners/station.wav", payload, classification="unique")],
    )
    from autodj.backup import _validate_restore_file_identity as real_validate

    real_fdopen = os.fdopen
    real_unlink = Path.unlink
    validation_failed = False
    opened_before_validation = False

    def fail_first_path_validation(
        path: Path,
        expected: tuple[int, int, int, int, int] | None,
        *,
        description: str,
    ) -> None:
        nonlocal validation_failed
        if ".restore-stage-" in path.name and not validation_failed:
            validation_failed = True
            raise BackupError("stage path identity changed")
        real_validate(path, expected, description=description)

    def observe_fdopen(*args: object, **kwargs: object) -> object:
        nonlocal opened_before_validation
        if not validation_failed:
            opened_before_validation = True
            raise AssertionError("payload opened before stage path validation")
        return real_fdopen(*args, **kwargs)  # type: ignore[arg-type]

    def retain_stage(path: Path, *args: object, **kwargs: object) -> None:
        if ".restore-stage-" in path.name:
            raise PermissionError("unlink blocked")
        real_unlink(path, *args, **kwargs)

    with (
        patch(
            "autodj.backup._validate_restore_file_identity",
            side_effect=fail_first_path_validation,
        ),
        patch("autodj.backup.os.fdopen", side_effect=observe_fdopen),
        patch("autodj.backup.Path.unlink", new=retain_stage),
        pytest.raises(BackupError, match="retained restore stages") as raised,
    ):
        restore_backup(cfg, archive, force=True)

    assert not opened_before_validation
    retained = list((cfg.index.active_dir / "liners").glob(".*.restore-stage-*"))
    assert len(retained) == 1
    assert str(retained[0]) in str(raised.value)


def test_required_free_space_has_floor_and_cap() -> None:
    mib = 1024**2
    gib = 1024**3
    assert _required_free_space(1) == 1 + 64 * mib
    assert _required_free_space(100 * gib) == 101 * gib


@pytest.mark.parametrize(
    ("items", "message"),
    [
        ("not-a-list", "manifest items"),
        (["not-an-object"], "items must be objects"),
        (
            [
                _item("derived/tracks.db", "active/tracks.db", b"payload"),
                _item("derived/tracks.db", "active/other.db", b"payload"),
            ],
            "duplicate archive member",
        ),
        (
            [
                _item("derived/tracks.db", "active/tracks.db", b"payload"),
                _item("derived/other.db", "active/tracks.db", b"payload"),
            ],
            "duplicate restore destination",
        ),
    ],
)
def test_restore_rejects_invalid_and_duplicate_manifest_entries_before_writing(
    tmp_path: Path, items: object, message: str
) -> None:
    cfg = _config(tmp_path)
    archive = _archive(tmp_path / "invalid.zip", [("derived/tracks.db", b"payload")], items=items)
    with pytest.raises(BackupError, match=message):
        restore_backup(cfg, archive, force=True)
    assert list(cfg.index.active_dir.iterdir()) == []


def test_restore_requires_exact_manifest_member_set(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    payload = b"payload"
    items = [_item("derived/tracks.db", "active/tracks.db", payload)]
    archive = _archive(
        tmp_path / "unexpected.zip",
        [("derived/tracks.db", payload), ("derived/other.db", b"other")],
        items=items,
    )
    with pytest.raises(BackupError, match="unmanifested member"):
        restore_backup(cfg, archive, force=True)


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_missing_or_corrupt_later_member_leaves_every_target_unchanged(
    tmp_path: Path, failure: str
) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="New")
    good = create_backup(source, tmp_path / "good.zip", online=False)
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(broken, "w") as dst:
        for name in src.namelist():
            if name == "derived/tracks.db" and failure == "missing":
                continue
            data = b"corrupt" if name == "derived/tracks.db" else src.read(name)
            dst.writestr(name, data)
    target = _config(tmp_path / "target")
    _published_index(target, title="Old")
    active = target.index.active_dir
    before = {name: (active / name).read_bytes() for name in ("tracks.db", "vectors.index")}

    with pytest.raises(BackupError, match=r"missing|size|checksum"):
        restore_backup(target, broken, force=True)

    assert {name: (active / name).read_bytes() for name in before} == before
    assert not list(active.glob(".*.restore-stage-*"))


def test_corrupt_later_payload_reports_every_unremovable_stage(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    payloads = [("derived/tracks.db", b"tracks"), ("derived/vectors.index", b"vectors")]
    items = [
        _item("derived/tracks.db", "active/tracks.db", b"tracks"),
        _item("derived/vectors.index", "active/vectors.index", b"different"),
    ]
    items[1]["size"] = len(b"vectors")
    archive = _archive(tmp_path / "corrupt-stages.zip", payloads, items=items)
    real_unlink = Path.unlink

    def retain_stages(path: Path, *args: object, **kwargs: object) -> None:
        if ".restore-stage-" in path.name:
            raise PermissionError("stage cleanup blocked")
        real_unlink(path, *args, **kwargs)

    with (
        patch("autodj.backup.Path.unlink", new=retain_stages),
        pytest.raises(BackupError, match="retained restore stages") as raised,
    ):
        restore_backup(cfg, archive, force=True)

    retained = sorted(cfg.index.active_dir.glob(".*.restore-stage-*"))
    assert len(retained) == 2
    assert all(str(path) in str(raised.value) for path in retained)


def test_restore_detects_target_ancestor_swap_before_commit(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    liners = tmp_path / "liners"
    liners.mkdir()
    cfg = replace(cfg, playback=replace(cfg.playback, liners_folder=liners))
    payload = b"liner"
    member = "unique/liners/station.wav"
    archive = _archive(
        tmp_path / "ancestor-swap.zip",
        [(member, payload)],
        items=[_item(member, "liners/station.wav", payload, classification="unique")],
    )
    moved = tmp_path / "liners-moved"
    from autodj.backup import _stage_payloads as real_stage

    def stage_then_swap(zf: zipfile.ZipFile, targets: list[object]) -> list[object]:
        records = real_stage(zf, targets)  # type: ignore[arg-type]
        liners.rename(moved)
        liners.mkdir()
        (liners / "attacker.txt").write_bytes(b"untouched")
        return records  # type: ignore[return-value]

    with (
        patch("autodj.backup._stage_payloads", side_effect=stage_then_swap),
        pytest.raises(BackupError, match=r"ancestor.*changed"),
    ):
        restore_backup(cfg, archive, force=False)

    assert (liners / "attacker.txt").read_bytes() == b"untouched"
    assert not (liners / "station.wav").exists()
    assert list(moved.glob(".*.restore-stage-*"))


def test_install_failure_rolls_back_every_target(tmp_path: Path) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="New")
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    target = _config(tmp_path / "target")
    _published_index(target, title="Old")
    active = target.index.active_dir
    names = ("tracks.db", "vectors.index", "index-manifest.json")
    before = {name: (active / name).read_bytes() for name in names}
    real_replace = os.replace

    def fail_tracks_install(
        source_path: os.PathLike[str], destination_path: os.PathLike[str]
    ) -> None:
        if (
            ".restore-stage-" in Path(source_path).name
            and Path(destination_path).name == "tracks.db"
        ):
            raise OSError("injected install failure")
        real_replace(source_path, destination_path)

    with (
        patch("autodj.backup.os.replace", side_effect=fail_tracks_install),
        pytest.raises(BackupError, match="previous files restored"),
    ):
        restore_backup(target, archive, force=True)
    assert {name: (active / name).read_bytes() for name in names} == before


def test_rollback_refuses_to_overwrite_target_changed_after_install(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    archive = _archive(
        tmp_path / "rollback-identity.zip",
        [
            ("derived/tracks.db", b"new tracks"),
            ("derived/vectors.index", b"new vectors"),
        ],
    )
    tracks = cfg.index.active_dir / "tracks.db"
    vectors = cfg.index.active_dir / "vectors.index"
    tracks.write_bytes(b"old tracks")
    vectors.write_bytes(b"old vectors")
    real_replace = os.replace

    def mutate_then_fail_second_install(source: Path, target: Path) -> None:
        if ".restore-stage-" in Path(source).name and target == vectors:
            tracks.write_bytes(b"attacker replacement")
            raise OSError("second install blocked")
        real_replace(source, target)

    with (
        patch("autodj.backup.os.replace", side_effect=mutate_then_fail_second_install),
        pytest.raises(BackupError, match="rollback was incomplete") as raised,
    ):
        restore_backup(cfg, archive, force=True)

    assert tracks.read_bytes() == b"attacker replacement"
    retained = list(cfg.index.active_dir.glob(".*tracks.db.restore-old-*"))
    assert len(retained) == 1
    assert retained[0].read_bytes() == b"old tracks"
    assert str(retained[0]) in str(raised.value)


@pytest.mark.parametrize("control", [KeyboardInterrupt("stop"), SystemExit("stop")])
def test_process_control_exception_propagates_after_clean_rollback(
    tmp_path: Path, control: BaseException
) -> None:
    cfg = _config(tmp_path)
    archive = _archive(
        tmp_path / "control.zip",
        [
            ("derived/tracks.db", b"new first"),
            ("derived/vectors.index", b"new second"),
        ],
    )
    first = cfg.index.active_dir / "tracks.db"
    second = cfg.index.active_dir / "vectors.index"
    first.write_bytes(b"old first")
    second.write_bytes(b"old second")
    real_replace = os.replace

    def interrupt_second_install(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        if ".restore-stage-" in Path(source).name and Path(destination) == second:
            raise control
        real_replace(source, destination)

    with (
        patch("autodj.backup.os.replace", side_effect=interrupt_second_install),
        pytest.raises(type(control), match="stop"),
    ):
        restore_backup(cfg, archive, force=True)

    assert first.read_bytes() == b"old first"
    assert second.read_bytes() == b"old second"
    assert not list(cfg.index.active_dir.glob(".*.restore-stage-*"))
    assert not list(cfg.index.active_dir.glob(".*.restore-old-*"))


@pytest.mark.parametrize("phase", ["replace", "post-guard"])
def test_target_to_recovery_move_reconciles_control_exception_after_syscall(
    tmp_path: Path, phase: str
) -> None:
    cfg = _config(tmp_path)
    archive = _archive(tmp_path / "old-move-control.zip", [("derived/tracks.db", b"new")])
    target = cfg.index.active_dir / "tracks.db"
    target.write_bytes(b"old")
    real_replace = os.replace
    from autodj.backup import _validate_restore_guard as real_guard

    moved = False
    guard_interrupted = False

    def move_then_interrupt(source: Path, destination: Path) -> None:
        nonlocal moved
        real_replace(source, destination)
        if source == target and ".restore-old-" in Path(destination).name:
            moved = True
            if phase == "replace":
                raise KeyboardInterrupt("stop after old move")

    def interrupt_post_guard(record: object) -> None:
        nonlocal guard_interrupted
        if phase == "post-guard" and moved and not guard_interrupted:
            guard_interrupted = True
            raise KeyboardInterrupt("stop after old move guard")
        real_guard(record)  # type: ignore[arg-type]

    with (
        patch("autodj.backup.os.replace", side_effect=move_then_interrupt),
        patch("autodj.backup._validate_restore_guard", new=interrupt_post_guard),
        pytest.raises(KeyboardInterrupt, match="stop after old move"),
    ):
        restore_backup(cfg, archive, force=True)

    assert target.read_bytes() == b"old"
    assert not list(cfg.index.active_dir.glob(".*.restore-old-*"))


@pytest.mark.parametrize("publication", ["replace", "link", "post-guard"])
def test_restore_reconciles_install_completed_before_control_exception(
    tmp_path: Path, publication: str
) -> None:
    cfg = _config(tmp_path)
    archive = _archive(tmp_path / "install-control.zip", [("derived/tracks.db", b"new")])
    target = cfg.index.active_dir / "tracks.db"
    real_replace = os.replace
    real_link = os.link
    from autodj.backup import _validate_restore_guard as real_guard

    installed = False
    guard_interrupted = False

    def replace_then_interrupt(source: Path, destination: Path) -> None:
        nonlocal installed
        real_replace(source, destination)
        if ".restore-stage-" in Path(source).name and destination == target:
            installed = True
            if publication == "replace":
                raise KeyboardInterrupt("stop after install")

    def link_then_interrupt(source: Path, destination: Path) -> None:
        nonlocal installed
        real_link(source, destination)
        if destination == target:
            installed = True
            raise KeyboardInterrupt("stop after link")

    def interrupt_post_guard(record: object) -> None:
        nonlocal guard_interrupted
        if publication == "post-guard" and installed and not guard_interrupted:
            guard_interrupted = True
            raise KeyboardInterrupt("stop in post guard")
        real_guard(record)  # type: ignore[arg-type]

    patches = [patch("autodj.backup._validate_restore_guard", new=interrupt_post_guard)]
    if publication == "link":
        patches.append(patch("autodj.backup.os.link", side_effect=link_then_interrupt))
    else:
        patches.append(patch("autodj.backup.os.replace", side_effect=replace_then_interrupt))

    with patches[0], patches[1], pytest.raises(KeyboardInterrupt, match="stop"):
        restore_backup(cfg, archive, force=publication != "link")

    assert not target.exists()
    assert not list(cfg.index.active_dir.glob(".*.restore-stage-*"))
    assert not list(cfg.index.active_dir.glob(".*.restore-old-*"))


def test_process_control_with_incomplete_rollback_reports_retained_recovery(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    archive = _archive(
        tmp_path / "control.zip",
        [
            ("derived/tracks.db", b"new first"),
            ("derived/vectors.index", b"new second"),
        ],
    )
    first = cfg.index.active_dir / "tracks.db"
    second = cfg.index.active_dir / "vectors.index"
    first.write_bytes(b"old first")
    second.write_bytes(b"old second")
    real_replace = os.replace

    def interrupt_then_fail_rollback(
        source: os.PathLike[str], destination: os.PathLike[str]
    ) -> None:
        source_name = Path(source).name
        target = Path(destination)
        if ".restore-stage-" in source_name and target == second:
            raise SystemExit("stop")
        if ".restore-old-" in source_name and target == second:
            raise OSError("rollback blocked")
        real_replace(source, destination)

    with (
        patch("autodj.backup.os.replace", side_effect=interrupt_then_fail_rollback),
        pytest.raises(BackupError, match=r"rollback was incomplete.*retained recovery") as raised,
    ):
        restore_backup(cfg, archive, force=True)

    assert isinstance(raised.value.__cause__, SystemExit)
    assert first.read_bytes() == b"old first"
    assert not second.exists()
    retained = list(cfg.index.active_dir.glob(".*.restore-old-*"))
    assert len(retained) == 1
    assert retained[0].read_bytes() == b"old second"
    assert str(retained[0]) in str(raised.value)
    assert not list(cfg.index.active_dir.glob(".*.restore-stage-*"))


def test_old_copy_cleanup_failure_is_success_warning(tmp_path: Path) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="New")
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    target = _config(tmp_path / "target")
    _published_index(target, title="Old")
    real_unlink = Path.unlink

    def retain_recovery(path: Path, *args: object, **kwargs: object) -> None:
        if ".restore-old-" in path.name:
            raise OSError("injected cleanup failure")
        real_unlink(path, *args, **kwargs)

    with patch("autodj.backup.Path.unlink", new=retain_recovery):
        result = restore_backup(target, archive, force=True)
    assert result.restored >= 3
    assert any("recovery copy retained" in warning for warning in result.warnings)
    assert list(target.index.active_dir.glob(".*.restore-old-*"))


def test_post_install_directory_fsync_failure_is_success_warning(tmp_path: Path) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="New")
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    target = _config(tmp_path / "target")
    _published_index(target, title="Old")
    with patch("autodj.backup._fsync_directory", side_effect=OSError("sync failed")):
        result = restore_backup(target, archive, force=True)
    assert any("directory sync" in warning for warning in result.warnings)


def test_restore_requires_force_then_recreates_files(tmp_path: Path) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="New")
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    target = _config(tmp_path / "target")
    _published_index(target, title="Old")
    expected = (source.index.active_dir / "vectors.index").read_bytes()
    with pytest.raises(BackupError, match="--force"):
        restore_backup(target, archive, force=False)
    restore_backup(target, archive, force=True)
    assert (target.index.active_dir / "vectors.index").read_bytes() == expected


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("tracks_digest", "tracks SHA-256 mismatch"),
        ("vectors_digest", "vectors SHA-256 mismatch"),
        ("vector_count", "index count mismatch"),
        ("tracks_schema", "tracks schema does not match"),
        ("vectors_format", "index snapshot is invalid"),
    ],
)
def test_restore_rejects_corrupt_staged_index_publication_before_target_mutation(
    tmp_path: Path,
    corruption: str,
    message: str,
) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="Restored")
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    with zipfile.ZipFile(archive) as zf:
        inner_manifest = json.loads(zf.read("derived/index-manifest.json"))
    replacements: dict[str, bytes] = {}
    if corruption == "tracks_digest":
        inner_manifest["tracks_sha256"] = "0" * 64
    elif corruption == "vectors_digest":
        inner_manifest["vectors_sha256"] = "0" * 64
    elif corruption == "vector_count":
        inner_manifest["vector_count"] += 1
    elif corruption == "tracks_schema":
        corrupt_tracks = tmp_path / "corrupt-tracks.db"
        _sqlite(corrupt_tracks, "not the published schema")
        tracks_payload = corrupt_tracks.read_bytes()
        replacements["derived/tracks.db"] = tracks_payload
        inner_manifest["tracks_sha256"] = hashlib.sha256(tracks_payload).hexdigest()
    else:
        vectors_payload = b"not a FAISS index"
        replacements["derived/vectors.index"] = vectors_payload
        inner_manifest["vectors_sha256"] = hashlib.sha256(vectors_payload).hexdigest()
    replacements["derived/index-manifest.json"] = (
        json.dumps(inner_manifest, sort_keys=True) + "\n"
    ).encode()
    _rewrite_archive_payloads(archive, replacements)
    target = _config(tmp_path / "target")
    _published_index(target, title="Old")
    active = target.index.active_dir
    names = (
        "tracks.db",
        "vectors.index",
        "index-manifest.json",
        ".index-publication-state.json",
    )
    before = {name: (active / name).read_bytes() for name in names}

    with pytest.raises(BackupError, match=message):
        restore_backup(target, archive, force=True)

    assert {name: (active / name).read_bytes() for name in names} == before
    assert not list(active.glob(".*.restore-stage-*"))
    assert not list(active.glob(".*.restore-old-*"))


def test_restore_rejects_archive_revision_outside_generation_filename_range(
    tmp_path: Path,
) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="Restored")
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    with zipfile.ZipFile(archive) as zf:
        inner_manifest = json.loads(zf.read("derived/index-manifest.json"))
    oversized_revision = int("9" * 300)
    inner_manifest["generation"] = oversized_revision
    inner_manifest["state_revision"] = oversized_revision
    manifest_payload = (json.dumps(inner_manifest, sort_keys=True) + "\n").encode()
    _rewrite_archive_payloads(archive, {"derived/index-manifest.json": manifest_payload})
    target = _config(tmp_path / "target")

    with pytest.raises(BackupError, match="20-digit"):
        restore_backup(target, archive, force=False)

    assert not (target.index.active_dir / "tracks.db").exists()
    assert not (target.index.active_dir / "vectors.index").exists()
    assert not (target.index.active_dir / "index-manifest.json").exists()
    assert not (target.index.active_dir / ".index-publication-state.json").exists()
    assert not list(target.index.active_dir.glob(".*.restore-stage-*"))


def test_restore_rejects_rebase_when_target_exhausted_generation_filename_range(
    tmp_path: Path,
) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="Restored")
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    target = _config(tmp_path / "target")
    state_path = target.index.active_dir / ".index-publication-state.json"
    state_payload = (
        json.dumps(
            {
                "high_water": 10**20 - 1,
                "tombstone_revision": 10**20 - 1,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    state_path.write_bytes(state_payload)

    with pytest.raises(BackupError, match="20-digit"):
        restore_backup(target, archive, force=True)

    assert state_path.read_bytes() == state_payload
    assert not (target.index.active_dir / "tracks.db").exists()
    assert not (target.index.active_dir / "vectors.index").exists()
    assert not (target.index.active_dir / "index-manifest.json").exists()
    assert not list(target.index.active_dir.glob(".*.restore-stage-*"))


def test_restore_rejects_oversized_compressed_inner_index_manifest_before_reading(
    tmp_path: Path,
) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="Restored")
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    oversized = b" " * (MAX_MANIFEST_BYTES + 1)
    _rewrite_archive_payloads(archive, {"derived/index-manifest.json": oversized})
    with zipfile.ZipFile(archive) as zf:
        inner = zf.getinfo("derived/index-manifest.json")
    assert inner.file_size == MAX_MANIFEST_BYTES + 1
    assert inner.compress_size < inner.file_size // 100
    target = _config(tmp_path / "target")

    with pytest.raises(BackupError, match="index manifest exceeds 16 MiB"):
        restore_backup(target, archive, force=False)

    assert not (target.index.active_dir / "tracks.db").exists()
    assert not (target.index.active_dir / "vectors.index").exists()
    assert not (target.index.active_dir / "index-manifest.json").exists()
    assert not (target.index.active_dir / ".index-publication-state.json").exists()
    assert not list(target.index.active_dir.glob(".*.restore-stage-*"))


def test_forced_restore_supersedes_tombstone_with_monotonic_live_identity(
    tmp_path: Path,
) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="Restored")
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    target = _config(tmp_path / "target")
    _published_index(target, title="Deleted")
    tombstone_publication(target.index.active_dir)
    prior = current_snapshot_token(target.index.active_dir)

    restore_backup(target, archive, force=True)

    restored = read_manifest(target.index.active_dir)
    assert restored is not None
    assert restored.generation == restored.state_revision > prior.state_revision
    state = json.loads(
        (target.index.active_dir / ".index-publication-state.json").read_text(encoding="utf-8")
    )
    assert state == {
        "high_water": restored.generation,
        "tombstone_revision": 0,
    }


def test_restore_reconciles_lower_target_high_water_without_hiding_manifest(
    tmp_path: Path,
) -> None:
    source = _config(tmp_path / "source")
    for title in ("First", "Second", "Restored"):
        _published_index(source, title=title)
    source_manifest = read_manifest(source.index.active_dir)
    assert source_manifest is not None
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    target = _config(tmp_path / "target")
    _published_index(target, title="Old")
    prior = current_snapshot_token(target.index.active_dir)
    assert prior.generation < source_manifest.generation

    restore_backup(target, archive, force=True)

    restored = read_manifest(target.index.active_dir)
    assert restored is not None
    assert restored.generation == restored.state_revision == source_manifest.generation
    state = json.loads(
        (target.index.active_dir / ".index-publication-state.json").read_text(encoding="utf-8")
    )
    assert state == {
        "high_water": source_manifest.generation,
        "tombstone_revision": 0,
    }


def test_restore_rebases_older_archive_above_higher_target_high_water(tmp_path: Path) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="Restored")
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    target = _config(tmp_path / "target")
    for title in ("First", "Second", "Newest"):
        _published_index(target, title=title)
    prior = current_snapshot_token(target.index.active_dir)

    restore_backup(target, archive, force=True)

    restored = read_manifest(target.index.active_dir)
    assert restored is not None
    assert restored.generation == restored.state_revision > prior.generation
    state = json.loads(
        (target.index.active_dir / ".index-publication-state.json").read_text(encoding="utf-8")
    )
    assert state == {
        "high_water": restored.generation,
        "tombstone_revision": 0,
    }


def test_publication_state_install_failure_rolls_back_manifest_artifacts_and_state(
    tmp_path: Path,
) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="Restored")
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    target = _config(tmp_path / "target")
    _published_index(target, title="Old")
    active = target.index.active_dir
    names = (
        "tracks.db",
        "vectors.index",
        "index-manifest.json",
        ".index-publication-state.json",
    )
    before = {name: (active / name).read_bytes() for name in names}
    real_replace = os.replace

    def fail_state_install(
        source_path: os.PathLike[str], destination_path: os.PathLike[str]
    ) -> None:
        if (
            ".restore-stage-" in Path(source_path).name
            and Path(destination_path).name == ".index-publication-state.json"
        ):
            raise OSError("injected publication state install failure")
        real_replace(source_path, destination_path)

    with (
        patch("autodj.backup.os.replace", side_effect=fail_state_install),
        pytest.raises(BackupError, match="previous files restored"),
    ):
        restore_backup(target, archive, force=True)

    assert {name: (active / name).read_bytes() for name in names} == before
    assert read_manifest(active) is not None
    assert not list(active.glob(".*.restore-stage-*"))
    assert not list(active.glob(".*.restore-old-*"))


def test_publication_state_staging_validation_failure_cleans_internal_stage(
    tmp_path: Path,
) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="Restored")
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    target = _config(tmp_path / "target")
    active = target.index.active_dir
    path_type = type(active)
    real_lstat = path_type.lstat
    state_validation_failed = False

    def fail_state_validation_once(path: Path) -> os.stat_result:
        nonlocal state_validation_failed
        if (
            ".index-publication-state.json.restore-stage-" in path.name
            and not state_validation_failed
        ):
            state_validation_failed = True
            raise OSError("injected publication state staging validation failure")
        return real_lstat(path)

    with (
        patch.object(path_type, "lstat", autospec=True, side_effect=fail_state_validation_once),
        pytest.raises(BackupError, match="publication preparation"),
    ):
        restore_backup(target, archive, force=False)

    assert state_validation_failed
    assert not list(active.glob(".*.restore-stage-*"))
    assert not (active / ".index-publication-state.json").exists()


def test_backup_is_read_only_and_restore_derives_live_publication_state(tmp_path: Path) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="Restored")
    source_state = source.index.active_dir / ".index-publication-state.json"
    before_bytes = source_state.read_bytes()
    before_mtime = source_state.stat().st_mtime_ns

    archive = create_backup(source, tmp_path / "backup.zip", online=False)

    assert source_state.read_bytes() == before_bytes
    assert source_state.stat().st_mtime_ns == before_mtime
    with zipfile.ZipFile(archive) as zf:
        assert "derived/.index-publication-state.json" not in zf.namelist()
    target = _config(tmp_path / "target")
    restore_backup(target, archive, force=False)
    restored = read_manifest(target.index.active_dir)
    assert restored is not None
    state = json.loads(
        (target.index.active_dir / ".index-publication-state.json").read_text(encoding="utf-8")
    )
    assert state == {
        "high_water": restored.generation,
        "tombstone_revision": 0,
    }


def test_restore_rejects_invalid_target_publication_state_before_replacing_files(
    tmp_path: Path,
) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="Restored")
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    target = _config(tmp_path / "target")
    _published_index(target, title="Old")
    active = target.index.active_dir
    state_path = active / ".index-publication-state.json"
    state_path.write_text('{"high_water": -1, "tombstone_revision": 0}\n', encoding="utf-8")
    names = (
        "tracks.db",
        "vectors.index",
        "index-manifest.json",
        ".index-publication-state.json",
    )
    before = {name: (active / name).read_bytes() for name in names}

    with pytest.raises(BackupError, match="publication state"):
        restore_backup(target, archive, force=True)

    assert {name: (active / name).read_bytes() for name in names} == before
    assert not list(active.glob(".*.restore-stage-*"))
    assert not list(active.glob(".*.restore-old-*"))


def test_restore_does_not_accept_publication_state_from_archive(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    payload = b'{"high_water": 999, "tombstone_revision": 0}\n'
    member = "derived/.index-publication-state.json"
    archive = _archive(
        tmp_path / "state-injection.zip",
        [(member, payload)],
        items=[_item(member, "active/.index-publication-state.json", payload)],
    )

    with pytest.raises(BackupError, match="canonical mapping"):
        restore_backup(cfg, archive, force=False)

    assert not (cfg.index.active_dir / ".index-publication-state.json").exists()


def test_restore_rejects_manifest_artifacts_outside_canonical_archive_mapping(
    tmp_path: Path,
) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="Restored")
    active = source.index.active_dir
    manifest = json.loads((active / "index-manifest.json").read_text(encoding="utf-8"))
    generation = manifest["generation"]
    manifest["tracks_file"] = f"tracks.g{generation:020d}.db"
    manifest["vectors_file"] = f"vectors.g{generation:020d}.index"
    manifest_payload = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    archive = _archive(
        tmp_path / "noncanonical-index.zip",
        [
            ("derived/tracks.db", (active / "tracks.db").read_bytes()),
            ("derived/vectors.index", (active / "vectors.index").read_bytes()),
            ("derived/index-manifest.json", manifest_payload),
        ],
    )
    target = _config(tmp_path / "target")
    _published_index(target, title="Old")
    names = (
        "tracks.db",
        "vectors.index",
        "index-manifest.json",
        ".index-publication-state.json",
    )
    before = {name: (target.index.active_dir / name).read_bytes() for name in names}

    with pytest.raises(BackupError, match=r"canonical tracks\.db/vectors\.index"):
        restore_backup(target, archive, force=True)

    assert {name: (target.index.active_dir / name).read_bytes() for name in names} == before
    assert not list(target.index.active_dir.glob(".*.restore-stage-*"))


def test_restore_holds_publication_lock_across_index_transaction(tmp_path: Path) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="Restored")
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    target = _config(tmp_path / "target")
    _published_index(target, title="Old")
    install_started = Event()
    release_install = Event()
    tombstone_attempted = Event()
    tombstone_finished = Event()
    real_install = backup._install_stage

    def pause_first_index_install(record: backup._StagedRestore) -> None:
        if record.target.name == "tracks.db" and not install_started.is_set():
            install_started.set()
            assert release_install.wait(5)
        real_install(record)

    def tombstone_target() -> None:
        tombstone_attempted.set()
        tombstone_publication(target.index.active_dir)
        tombstone_finished.set()

    with (
        patch("autodj.backup._install_stage", side_effect=pause_first_index_install),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        restoring = pool.submit(restore_backup, target, archive, force=True)
        assert install_started.wait(5)
        tombstoning = pool.submit(tombstone_target)
        assert tombstone_attempted.wait(5)
        tombstone_was_blocked = not tombstone_finished.wait(0.2)
        release_install.set()
        restoring.result(timeout=10)
        tombstoning.result(timeout=10)

    assert tombstone_was_blocked
    assert read_manifest(target.index.active_dir) is None


def test_restore_no_force_race_preserves_racer_and_rolls_back_prior_install(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    archive = _archive(
        tmp_path / "race.zip",
        [("derived/tracks.db", b"first"), ("derived/vectors.index", b"second")],
    )
    first = cfg.index.active_dir / "tracks.db"
    second = cfg.index.active_dir / "vectors.index"
    real_link = os.link

    def racer_wins(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        target = Path(destination)
        if target == second:
            target.write_bytes(b"racer")
        real_link(source, destination)

    with (
        patch("autodj.backup.os.link", side_effect=racer_wins),
        pytest.raises(BackupError, match=r"appeared during restore|previous files restored"),
    ):
        restore_backup(cfg, archive, force=False)

    assert not first.exists()
    assert second.read_bytes() == b"racer"
    assert not list(cfg.index.active_dir.glob(".*.restore-stage-*"))
    assert not list(cfg.index.active_dir.glob(".*.restore-old-*"))


def test_restore_cli_prints_warnings_runs_doctor_and_refuses_serve_on_failure(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"placeholder")
    report = DoctorReport((DoctorCheck("index", CheckStatus.FAIL, "broken"),))
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=SimpleNamespace()),
        patch(
            "autodj.backup.restore_backup",
            return_value=RestoreResult(2, ("retained old copy",)),
        ) as restore,
        patch("autodj.doctor.run_doctor", return_value=report) as doctor,
    ):
        result = CliRunner().invoke(cli, ["restore", str(archive), "--force"])
    assert result.exit_code != 0
    restore.assert_called_once()
    assert restore.call_args.kwargs == {"force": True}
    doctor.assert_called_once()
    assert "Restored 2 files" in result.output
    assert "WARNING: retained old copy" in result.output
    assert "do not serve" in result.output
