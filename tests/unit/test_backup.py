"""Backup and restore round trip and the archives restore must refuse."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from click.testing import CliRunner

from autodj.backup import BackupError, create_backup, restore_backup
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
from autodj.index_manifest import read_manifest
from autodj.indexer import FEATURE_DIM, IndexEntry, save_index
from autodj.version import current_version


def _config(root: Path) -> AutoDJConfig:
    (root / "music").mkdir(parents=True)
    cfg = AutoDJConfig(
        library=LibraryConfig(root / "music", None, ["flac"]),
        index=IndexConfig(root / "index", root / "models", "default"),
        playback=PlaybackConfig(history_file=root / "history.jsonl"),
        model=ModelConfig(),
        huggingface=HuggingFaceConfig(None),
        config_path=root / "config.toml",
    )
    cfg.index.active_dir.mkdir(parents=True)
    return cfg


def _publish(cfg: AutoDJConfig, title: str) -> None:
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
    save_index([entry], vectors, cfg.index.active_dir, cfg.library.music_dir)


def _indexed_title(cfg: AutoDJConfig) -> str:
    manifest = read_manifest(cfg.index.active_dir)
    assert manifest is not None
    with closing(sqlite3.connect(cfg.index.active_dir / manifest.tracks_file)) as conn:
        return str(conn.execute("SELECT title FROM tracks").fetchone()[0])


def _dj_meta(cfg: AutoDJConfig) -> sqlite3.Connection:
    conn = sqlite3.connect(cfg.index.active_dir / "dj_meta.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS cues(value TEXT)")
    return conn


def _archive(path: Path, files: dict[str, bytes], version: str | None = None) -> Path:
    manifest = {"autodj_version": version or current_version(), "files": list(files)}
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr("manifest.json", json.dumps(manifest))
    return path


def test_restore_reproduces_what_was_backed_up(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    active = cfg.index.active_dir
    liners = active / "liners"
    profiles = active.parent / "profiles"
    _publish(cfg, "Kept")
    (active / "web_state.json").write_text('{"volume": 1}', encoding="utf-8")
    liners.mkdir()
    (liners / "station.mp3").write_bytes(b"liner")
    (profiles / "night").mkdir(parents=True)
    (profiles / "night" / "profile.json").write_text("{}", encoding="utf-8")
    assert cfg.playback.history_file is not None
    cfg.playback.history_file.write_text('{"track": "one"}\n', encoding="utf-8")
    dj_meta = _dj_meta(cfg)
    dj_meta.execute("INSERT INTO cues VALUES ('in the WAL')")
    dj_meta.commit()
    with patch("autodj.cli._load_cfg_or_exit", return_value=cfg):
        backup = CliRunner().invoke(cli, ["backup", str(tmp_path / "backup.zip")])
    dj_meta.close()
    assert backup.exit_code == 0, backup.output

    _publish(cfg, "Replaced")
    (active / "web_state.json").write_text('{"volume": 0}', encoding="utf-8")
    (liners / "extra.mp3").write_bytes(b"new")
    cfg.playback.history_file.unlink()
    with closing(_dj_meta(cfg)) as conn:
        conn.execute("DELETE FROM cues")
        conn.commit()

    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch("autodj.doctor.run_doctor", return_value=DoctorReport(())),
    ):
        refused = CliRunner().invoke(cli, ["restore", str(tmp_path / "backup.zip")])
        restored = CliRunner().invoke(cli, ["restore", "--force", str(tmp_path / "backup.zip")])

    assert "pass --force" in refused.output
    assert restored.exit_code == 0, restored.output
    assert "Restored 8 files." in restored.output
    assert _indexed_title(cfg) == "Kept"
    assert (active / "web_state.json").read_text(encoding="utf-8") == '{"volume": 1}'
    assert sorted(path.name for path in liners.iterdir()) == ["station.mp3"]
    assert (profiles / "night" / "profile.json").read_text(encoding="utf-8") == "{}"
    assert cfg.playback.history_file.read_text(encoding="utf-8") == '{"track": "one"}\n'
    with closing(sqlite3.connect(active / "dj_meta.db")) as conn:
        assert conn.execute("SELECT value FROM cues").fetchall() == [("in the WAL",)]
    assert not list(active.parent.glob(".*restore-*"))


def test_restore_says_not_to_serve_when_doctor_fails(tmp_path: Path) -> None:
    archive = tmp_path / "backup.zip"
    archive.touch()
    report = DoctorReport((DoctorCheck("index", CheckStatus.FAIL, "broken"),))
    with (
        patch("autodj.cli._load_cfg_or_exit"),
        patch("autodj.backup.restore_backup", return_value=1),
        patch("autodj.doctor.run_doctor", return_value=report),
    ):
        result = CliRunner().invoke(cli, ["restore", str(archive)])

    assert result.exit_code == 1
    assert "do not serve yet" in result.output


def test_restore_refuses_another_minor_version(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    (cfg.index.active_dir / "web_state.json").write_text("{}", encoding="utf-8")
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    major, minor = (int(part) for part in current_version().split(".")[:2])

    with (
        patch("autodj.backup.current_version", return_value=f"{major}.{minor + 1}.0"),
        pytest.raises(BackupError, match="same major and minor version"),
    ):
        restore_backup(cfg, archive, force=True)


@pytest.mark.parametrize(
    "name",
    ["../escape.mp3", "/abs.mp3", "C:/abs.mp3", "liners/../../escape.mp3", "liners\\..\\x.mp3"],
)
def test_restore_refuses_unsafe_member_names(tmp_path: Path, name: str) -> None:
    cfg = _config(tmp_path / "home")
    archive = _archive(tmp_path / "evil.zip", {name: b"x"})

    with pytest.raises(BackupError, match="unsafe file name"):
        restore_backup(cfg, archive, force=True)

    assert not list(tmp_path.rglob("*.mp3"))


def test_restore_refuses_an_old_format_index(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    old_manifest = json.dumps({"schema_version": 1}).encode()
    files = {
        "index/tracks.db": b"old",
        "index/vectors.index": b"old",
        "index/index-manifest.json": old_manifest,
    }
    archive = _archive(tmp_path / "old.zip", files)

    with pytest.raises(BackupError, match=r"this backup was made by an older AutoDJ.*--force`"):
        restore_backup(cfg, archive, force=True)

    assert not (cfg.index.active_dir / "tracks.db").exists()


def test_backup_refuses_an_old_format_index(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    (cfg.index.active_dir / "tracks.db").write_bytes(b"made before index manifests")

    with pytest.raises(BackupError, match=r"older AutoDJ.*autodj index --force"):
        create_backup(cfg, tmp_path / "backup.zip", force=False)

    assert not list(tmp_path.glob("*backup.zip*"))
