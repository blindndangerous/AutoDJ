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
        playback=PlaybackConfig(),
        model=ModelConfig(),
        huggingface=HuggingFaceConfig(None),
        config_path=root / "config.toml",
    )
    cfg.index.active_dir.mkdir(parents=True)
    return cfg


def test_schedule_is_backed_up_and_restored(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    schedule = cfg.index.active_dir / "schedule.json"
    schedule.write_text('{"schema_version":1,"events":[],"runs":[]}', encoding="utf-8")
    archive = create_backup(cfg, tmp_path / "schedule.zip", force=False)
    with zipfile.ZipFile(archive) as backup:
        assert "index/schedule.json" in backup.namelist()
    schedule.unlink()
    restore_backup(cfg, archive, force=True)
    assert json.loads(schedule.read_text(encoding="utf-8"))["schema_version"] == 1


def test_clockwheels_are_backed_up_and_restored(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    folder = cfg.index.active_dir / "clockwheels"
    folder.mkdir()
    wheel = folder / "Morning.json"
    wheel.write_text(
        '{"version":1,"name":"Morning","steps":[],"resync_minutes":[0]}', encoding="utf-8"
    )
    archive = create_backup(cfg, tmp_path / "clockwheels.zip", force=False)
    with zipfile.ZipFile(archive) as backup:
        assert "clockwheels/Morning.json" in backup.namelist()
    wheel.unlink()
    restore_backup(cfg, archive, force=True)
    assert json.loads(wheel.read_text(encoding="utf-8"))["name"] == "Morning"


def test_carts_and_uploaded_clips_are_backed_up_and_restored(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = cfg.index.active_dir / "carts.json"
    state.write_text('{"schema_version":1,"active_page":1,"pages":[]}', encoding="utf-8")
    uploads = cfg.index.active_dir / "carts"
    uploads.mkdir()
    (uploads / "id.wav").write_bytes(b"audio")
    archive = create_backup(cfg, tmp_path / "carts.zip", force=False)
    with zipfile.ZipFile(archive) as backup:
        assert "index/carts.json" in backup.namelist()
        assert "carts/id.wav" in backup.namelist()
    state.unlink()
    (uploads / "id.wav").unlink()
    restore_backup(cfg, archive, force=True)
    assert state.exists()
    assert (uploads / "id.wav").read_bytes() == b"audio"


def test_time_checks_settings_and_recordings_survive_restore(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = cfg.index.active_dir / "time_checks.json"
    state.write_text(
        '{"version":1,"settings":{"enabled":true,"hour_format":"12","rule":"off"}}',
        encoding="utf-8",
    )
    clips = cfg.index.active_dir / "time_checks"
    clips.mkdir()
    (clips / "03_05.wav").write_bytes(b"clip")
    # A voice pack kept in the time_checks folder is backed up; one elsewhere is not.
    (clips / "Studio").mkdir()
    (clips / "Studio" / "hr_03.wav").write_bytes(b"hour")
    elsewhere = tmp_path / "voices" / "Night"
    elsewhere.mkdir(parents=True)
    (elsewhere / "hr_03.wav").write_bytes(b"hour")
    cfg.time_checks.voice_packs = [str(clips / "Studio"), str(elsewhere)]
    archive = create_backup(cfg, tmp_path / "time-checks.zip", force=False)
    with zipfile.ZipFile(archive) as backup:
        names = set(backup.namelist())
        assert {
            "index/time_checks.json",
            "time_checks/03_05.wav",
            "time_checks/Studio/hr_03.wav",
        } <= names
        assert not any("Night" in name for name in names)
    state.unlink()
    (clips / "03_05.wav").unlink()
    restore_backup(cfg, archive, force=True)
    assert json.loads(state.read_text(encoding="utf-8"))["settings"]["enabled"]
    assert (clips / "03_05.wav").read_bytes() == b"clip"


def test_recording_state_is_backed_up_but_recordings_are_not(tmp_path: Path) -> None:
    """Archives can be huge, and a restore must never replace the recordings folder."""
    cfg = _config(tmp_path)
    cfg.recording.folder = tmp_path / "recordings"
    cfg.recording.folder.mkdir()
    state = cfg.index.active_dir / "recording.json"
    recording = cfg.recording.folder / "Station-2026-01-01_12-00-00.mp3"
    state.write_text('{"version":1,"running":true}')
    recording.write_bytes(b"MP3")
    archive = create_backup(cfg, tmp_path / "recording.zip", force=False)
    with zipfile.ZipFile(archive) as backup:
        assert backup.read("index/recording.json") == state.read_bytes()
        assert not [name for name in backup.namelist() if name.endswith(".mp3")]
    state.unlink()
    newer = cfg.recording.folder / "Station-2026-01-02_12-00-00.mp3"
    newer.write_bytes(b"NEW")
    restore_backup(cfg, archive, force=True)
    assert state.exists()
    assert recording.read_bytes() == b"MP3"
    assert newer.read_bytes() == b"NEW"


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
    live = read_manifest(cfg.index.active_dir)
    base = 0 if live is None else live.generation
    save_index([entry], vectors, cfg.index.active_dir, cfg.library.music_dir, base_generation=base)


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


def _catalog(cfg: AutoDJConfig) -> sqlite3.Connection:
    conn = sqlite3.connect(cfg.index.active_dir / "library.db")
    conn.executescript(
        """
        CREATE TABLE catalog_sources(id TEXT PRIMARY KEY, path TEXT, kind TEXT);
        CREATE TABLE catalog_entries(id TEXT PRIMARY KEY, path TEXT, points TEXT);
        CREATE TABLE play_history(event_id TEXT PRIMARY KEY, title TEXT);
        """
    )
    conn.execute("INSERT INTO catalog_sources VALUES ('folder', '/music', 'music')")
    conn.execute(
        "INSERT INTO catalog_entries VALUES ('track', '/music/song.flac', '{\"start\": 1}')"
    )
    conn.execute("INSERT INTO play_history VALUES ('play', 'Kept')")
    conn.commit()
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
    (active / "web_state.json").write_text(
        '{"playback": {"nowplaying_show_next": false}}', encoding="utf-8"
    )
    (active / "listener_bans.json").write_text(
        '{"bans": [{"address": "192.0.2.1/32", "expires_at": null, "note": "abuse"}]}',
        encoding="utf-8",
    )
    (active / "related-artists.toml").write_text("user links", encoding="utf-8")
    liners.mkdir()
    (liners / "station.mp3").write_bytes(b"liner")
    (profiles / "night").mkdir(parents=True)
    (profiles / "night" / "profile.json").write_text("{}", encoding="utf-8")
    dj_meta = _dj_meta(cfg)
    dj_meta.execute("INSERT INTO cues VALUES ('in the WAL')")
    dj_meta.commit()
    catalog = _catalog(cfg)
    with patch("autodj.cli._load_cfg_or_exit", return_value=cfg):
        backup = CliRunner().invoke(cli, ["backup", str(tmp_path / "backup.zip")])
    dj_meta.close()
    catalog.close()
    assert backup.exit_code == 0, backup.output

    _publish(cfg, "Replaced")
    (active / "web_state.json").write_text('{"volume": 0}', encoding="utf-8")
    (active / "listener_bans.json").write_text('{"bans": []}', encoding="utf-8")
    (active / "related-artists.toml").write_text("replaced links", encoding="utf-8")
    (liners / "extra.mp3").write_bytes(b"new")
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
    assert "Restored 10 files." in restored.output
    assert _indexed_title(cfg) == "Kept"
    assert sorted(path.name for path in active.glob("*.g*")) == [
        "tracks.g00000000000000000001.db",
        "vectors.g00000000000000000001.index",
    ]
    assert (active / "web_state.json").read_text(encoding="utf-8") == (
        '{"playback": {"nowplaying_show_next": false}}'
    )
    assert (
        json.loads((active / "listener_bans.json").read_text(encoding="utf-8"))["bans"][0][
            "address"
        ]
        == "192.0.2.1/32"
    )
    assert (active / "related-artists.toml").read_text(encoding="utf-8") == "user links"
    assert sorted(path.name for path in liners.iterdir()) == ["station.mp3"]
    assert (profiles / "night" / "profile.json").read_text(encoding="utf-8") == "{}"
    with closing(sqlite3.connect(active / "dj_meta.db")) as conn:
        assert conn.execute("SELECT value FROM cues").fetchall() == [("in the WAL",)]
    with closing(sqlite3.connect(active / "library.db")) as conn:
        assert conn.execute("SELECT path FROM catalog_sources").fetchall() == [("/music",)]
        assert conn.execute("SELECT points FROM catalog_entries").fetchall() == [('{"start": 1}',)]
        assert conn.execute("SELECT title FROM play_history").fetchall() == [("Kept",)]
    assert not list(active.parent.glob(".*restore-*"))


def test_old_backup_without_catalog_preserves_current_catalog(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    archive = create_backup(cfg, tmp_path / "old.zip", force=False)
    catalog = _catalog(cfg)
    catalog.close()

    restore_backup(cfg, archive, force=True)

    with closing(sqlite3.connect(cfg.index.active_dir / "library.db")) as conn:
        assert conn.execute("SELECT title FROM play_history").fetchall() == [("Kept",)]


def test_restore_refuses_corrupt_catalog_before_overwrite(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    catalog = _catalog(cfg)
    catalog.close()
    archive = _archive(tmp_path / "corrupt.zip", {"index/library.db": b"not sqlite"})

    with pytest.raises(BackupError, match="integrity_check"):
        restore_backup(cfg, archive, force=True)

    with closing(sqlite3.connect(cfg.index.active_dir / "library.db")) as conn:
        assert conn.execute("SELECT title FROM play_history").fetchall() == [("Kept",)]


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


def test_uploaded_stereo_tool_preset_survives_backup_restore(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    folder = cfg.index.active_dir.parent / "stereo-tool-presets"
    folder.mkdir()
    preset = folder / "Night.sts"
    preset.write_bytes(b"[Processing]\nOutput Gain=0\n")
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    with zipfile.ZipFile(archive) as opened:
        assert opened.read("stereo-tool-presets/Night.sts") == preset.read_bytes()
    preset.unlink()
    restore_backup(cfg, archive, force=True)
    assert preset.read_bytes() == b"[Processing]\nOutput Gain=0\n"


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
    old_manifest = json.dumps({"schema_version": 2}).encode()
    files = {
        "index/tracks.g00000000000000000001.db": b"old",
        "index/vectors.g00000000000000000001.index": b"old",
        "index/index-manifest.json": old_manifest,
    }
    archive = _archive(tmp_path / "old.zip", files)

    with pytest.raises(BackupError, match=r"this backup was made by an older AutoDJ.*--force`"):
        restore_backup(cfg, archive, force=True)

    # Nothing from the archive is installed.  The index's own publication
    # lock file may remain: filelock keeps it on POSIX and removes it on Windows.
    leftovers = [p.name for p in cfg.index.active_dir.iterdir()]
    assert leftovers in ([], [".index-publication.lock"])


def test_backup_refuses_an_old_format_index(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    (cfg.index.active_dir / "tracks.db").write_bytes(b"made before index manifests")

    with pytest.raises(BackupError, match=r"older AutoDJ.*autodj index --force"):
        create_backup(cfg, tmp_path / "backup.zip", force=False)

    assert not list(tmp_path.glob("*backup.zip*"))
