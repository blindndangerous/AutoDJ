"""Backup and restore round trip and the archives restore must refuse."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from autodj.backup import BackupError, create_backup, restore_backup
from autodj.catalog import LibraryCatalog
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
from autodj.version import current_version


def _config(root: Path) -> AutoDJConfig:
    (root / "music").mkdir(parents=True)
    cfg = AutoDJConfig(
        library=LibraryConfig(root / "music", None, ["flac"]),
        index=IndexConfig(root / "index", root / "models"),
        playback=PlaybackConfig(),
        model=ModelConfig(),
        huggingface=HuggingFaceConfig(None),
        config_path=root / "config.toml",
    )
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    with LibraryCatalog(cfg.index.library_dir / "library.db"):
        pass
    return cfg


def test_schedule_is_backed_up_and_restored(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    schedule = (cfg.index.stations_dir / "default") / "schedule.json"
    schedule.write_text('{"schema_version":1,"events":[],"runs":[]}', encoding="utf-8")
    archive = create_backup(cfg, tmp_path / "schedule.zip", force=False)
    with zipfile.ZipFile(archive) as backup:
        assert "index/schedule.json" in backup.namelist()
    schedule.unlink()
    restore_backup(cfg, archive, force=True)
    assert json.loads(schedule.read_text(encoding="utf-8"))["schema_version"] == 1


def test_clockwheels_are_backed_up_and_restored(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    folder = (cfg.index.stations_dir / "default") / "clockwheels"
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
    state = (cfg.index.stations_dir / "default") / "carts.json"
    state.write_text('{"schema_version":1,"active_page":1,"pages":[]}', encoding="utf-8")
    uploads = (cfg.index.stations_dir / "default") / "carts"
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
    state = (cfg.index.stations_dir / "default") / "time_checks.json"
    state.write_text(
        '{"version":1,"settings":{"enabled":true,"hour_format":"12","rule":"off"}}',
        encoding="utf-8",
    )
    clips = cfg.index.shared_dir / "time_checks"
    clips.mkdir()
    (clips / "03_05.wav").write_bytes(b"clip")
    # A voice pack kept in the time_checks folder is backed up; one elsewhere is not.
    (clips / "Studio").mkdir()
    (clips / "Studio" / "hr_03.wav").write_bytes(b"hour")
    # Unfinished uploads and set-aside packs stay out (#388).
    (clips / ".pack-upload-0123456789abcdef").mkdir()
    (clips / ".pack-upload-0123456789abcdef" / "hr_04.wav").write_bytes(b"partial")
    (clips / ".clip-upload-0123456789abcdef.wav").write_bytes(b"partial")
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
        assert not any("-upload-" in name for name in names)
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
    state = (cfg.index.stations_dir / "default") / "recording.json"
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


def _dj_meta(cfg: AutoDJConfig) -> sqlite3.Connection:
    conn = sqlite3.connect(cfg.index.library_dir / "dj_meta.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS cues(value TEXT)")
    return conn


def _catalog(cfg: AutoDJConfig) -> sqlite3.Connection:
    conn = sqlite3.connect(cfg.index.library_dir / "library.db")
    conn.execute(
        "INSERT INTO catalog_sources(id,path,kind,recursive,advanced) VALUES "
        "('folder','/music','music',1,0)"
    )
    conn.execute(
        "INSERT INTO catalog_entries(id,path,kind,title,points) "
        "VALUES ('track','/music/song.flac','music','Kept','{\"start\": 1}')"
    )
    conn.execute(
        "INSERT INTO play_history(event_id,title,artist,duration,played_at) "
        "VALUES ('play','Kept','Artist',1,'2026-10-13T12:00:00+00:00')"
    )
    conn.commit()
    return conn


def _archive(path: Path, files: dict[str, bytes], version: str | None = None) -> Path:
    manifest = {
        "backup_format": 2,
        "autodj_version": version or current_version(),
        "files": list(files),
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr("manifest.json", json.dumps(manifest))
    return path


def test_restore_reproduces_what_was_backed_up(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    active = cfg.index.stations_dir / "default"
    liners = active / "liners"
    profiles = active / "profiles"
    library = cfg.index.library_dir
    catalog = _catalog(cfg)
    (active / "web_state.json").write_text(
        '{"playback": {"nowplaying_show_next": false}}', encoding="utf-8"
    )
    (active / "listener_bans.json").write_text(
        '{"bans": [{"address": "192.0.2.1/32", "expires_at": null, "note": "abuse"}]}',
        encoding="utf-8",
    )
    (library / "related-artists.toml").write_text("user links", encoding="utf-8")
    (library / "related-tracks.toml").write_text("user groups", encoding="utf-8")
    liners.mkdir()
    (liners / "station.mp3").write_bytes(b"liner")
    (profiles / "night").mkdir(parents=True)
    (profiles / "night" / "profile.json").write_text("{}", encoding="utf-8")
    dj_meta = _dj_meta(cfg)
    dj_meta.execute("INSERT INTO cues VALUES ('in the WAL')")
    dj_meta.commit()
    with patch("autodj.cli._load_cfg_or_exit", return_value=cfg):
        backup = CliRunner().invoke(cli, ["backup", str(tmp_path / "backup.zip")])
    dj_meta.close()
    catalog.close()
    assert backup.exit_code == 0, backup.output

    with closing(sqlite3.connect(cfg.index.library_dir / "library.db")) as conn:
        conn.execute("UPDATE catalog_entries SET title='Replaced'")
        conn.execute("UPDATE play_history SET title='Replaced'")
        conn.commit()
    (active / "web_state.json").write_text('{"volume": 0}', encoding="utf-8")
    (active / "listener_bans.json").write_text('{"bans": []}', encoding="utf-8")
    (library / "related-artists.toml").write_text("replaced links", encoding="utf-8")
    (library / "related-tracks.toml").write_text("replaced groups", encoding="utf-8")
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
    with zipfile.ZipFile(tmp_path / "backup.zip") as backup:
        count = len(json.loads(backup.read("manifest.json"))["files"])
    assert f"Restored {count} files." in restored.output
    with zipfile.ZipFile(tmp_path / "backup.zip") as backup:
        members = backup.namelist()
    assert "index/index-manifest.json" not in members
    assert not any(
        name.startswith("index/tracks.g") or name.startswith("index/vectors.g") for name in members
    )
    assert (active / "web_state.json").read_text(encoding="utf-8") == (
        '{"playback": {"nowplaying_show_next": false}}'
    )
    assert (
        json.loads((active / "listener_bans.json").read_text(encoding="utf-8"))["bans"][0][
            "address"
        ]
        == "192.0.2.1/32"
    )
    assert (library / "related-artists.toml").read_text(encoding="utf-8") == "user links"
    assert (library / "related-tracks.toml").read_text(encoding="utf-8") == "user groups"
    assert sorted(path.name for path in liners.iterdir()) == ["station.mp3"]
    assert (profiles / "night" / "profile.json").read_text(encoding="utf-8") == "{}"
    with closing(sqlite3.connect(cfg.index.library_dir / "dj_meta.db")) as conn:
        assert conn.execute("SELECT value FROM cues").fetchall() == [("in the WAL",)]
    with closing(sqlite3.connect(cfg.index.library_dir / "library.db")) as conn:
        assert conn.execute("SELECT path FROM catalog_sources").fetchall() == [("/music",)]
        assert conn.execute("SELECT points FROM catalog_entries").fetchall() == [('{"start": 1}',)]
        assert conn.execute("SELECT title FROM catalog_entries").fetchall() == [("Kept",)]
        assert conn.execute("SELECT title FROM play_history").fetchall() == [("Kept",)]
    assert not list(active.parent.glob(".*restore-*"))


def test_restore_refuses_a_backup_without_catalog_before_changing_current_data(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    catalog = _catalog(cfg)
    catalog.close()
    archive = _archive(
        tmp_path / "old.zip",
        {
            "index/index-manifest.json": b'{"schema_version":2}',
            "index/tracks.g00000000000000000001.db": b"legacy tracks",
            "index/vectors.g00000000000000000001.index": b"legacy vectors",
        },
    )

    with pytest.raises(BackupError, match=r"no catalog database.*migrate-index --apply"):
        restore_backup(cfg, archive, force=True)

    with closing(sqlite3.connect(cfg.index.library_dir / "library.db")) as conn:
        assert conn.execute("SELECT title FROM play_history").fetchall() == [("Kept",)]


def test_restore_refuses_corrupt_catalog_before_overwrite(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    catalog = _catalog(cfg)
    catalog.close()
    archive = _archive(tmp_path / "corrupt.zip", {"index/library.db": b"not sqlite"})

    with pytest.raises(BackupError, match="integrity_check"):
        restore_backup(cfg, archive, force=True)

    with closing(sqlite3.connect(cfg.index.library_dir / "library.db")) as conn:
        assert conn.execute("SELECT title FROM play_history").fetchall() == [("Kept",)]


def test_restore_refuses_valid_sqlite_that_is_not_a_catalog_before_overwrite(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    catalog = _catalog(cfg)
    catalog.close()
    unrelated = sqlite3.connect(":memory:")
    unrelated.execute("CREATE TABLE unrelated (value TEXT)")
    archive = _archive(tmp_path / "non-catalog.zip", {"index/library.db": unrelated.serialize()})
    unrelated.close()

    with pytest.raises(BackupError, match=r"not a current AutoDJ catalog.*migrate-index --apply"):
        restore_backup(cfg, archive, force=True)

    with closing(sqlite3.connect(cfg.index.library_dir / "library.db")) as conn:
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
    ((cfg.index.stations_dir / "default") / "web_state.json").write_text("{}", encoding="utf-8")
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    major, minor = (int(part) for part in current_version().split(".")[:2])

    with (
        patch("autodj.backup.current_version", return_value=f"{major}.{minor + 1}.0"),
        pytest.raises(BackupError, match="same major and minor version"),
    ):
        restore_backup(cfg, archive, force=True)


def test_uploaded_stereo_tool_preset_survives_backup_restore(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    folder = cfg.index.shared_dir / "stereo-tool-presets"
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


def test_restore_refuses_legacy_generations_even_with_a_catalog(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    catalog = _catalog(cfg)
    catalog.close()
    files = {
        "index/library.db": (cfg.index.library_dir / "library.db").read_bytes(),
        "index/tracks.g00000000000000000001.db": b"old",
        "index/vectors.g00000000000000000001.index": b"old",
        "index/index-manifest.json": b'{"schema_version":2}',
    }
    archive = _archive(tmp_path / "old.zip", files)

    with pytest.raises(BackupError, match=r"3 legacy index files.*migrate-index --apply"):
        restore_backup(cfg, archive, force=True)

    assert not (cfg.index.library_dir / "index-manifest.json").exists()
    assert not list((cfg.index.stations_dir / "default").glob("tracks.g*"))
    with closing(sqlite3.connect(cfg.index.library_dir / "library.db")) as conn:
        assert conn.execute("SELECT title FROM catalog_entries").fetchall() == [("Kept",)]


def test_backup_ignores_legacy_generation_files(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    (cfg.index.library_dir / "tracks.g00000000000000000001.db").write_bytes(b"obsolete tracks")
    (cfg.index.library_dir / "vectors.g00000000000000000001.index").write_bytes(b"obsolete vectors")
    (cfg.index.library_dir / "index-manifest.json").write_text("{}", encoding="utf-8")

    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)

    with zipfile.ZipFile(archive) as backup:
        names = set(backup.namelist())
    assert "index/library.db" in names
    assert "index/index-manifest.json" not in names
    assert not any(
        name.startswith("index/tracks.g") or name.startswith("index/vectors.g") for name in names
    )


def test_backup_requires_a_current_catalog(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    (cfg.index.library_dir / "library.db").unlink()

    with pytest.raises(BackupError, match=r"no catalog database.*autodj index"):
        create_backup(cfg, tmp_path / "backup.zip", force=False)

    assert not (tmp_path / "backup.zip").exists()


def test_backup_refuses_valid_sqlite_that_is_not_a_catalog(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    database = cfg.index.library_dir / "library.db"
    database.unlink()
    with closing(sqlite3.connect(database)) as unrelated:
        unrelated.execute("CREATE TABLE unrelated (value TEXT)")

    with pytest.raises(BackupError, match=r"not a current AutoDJ catalog.*migrate-index --apply"):
        create_backup(cfg, tmp_path / "backup.zip", force=False)

    assert not (tmp_path / "backup.zip").exists()


def test_categories_are_backed_up_but_their_playlists_are_not(tmp_path: Path) -> None:
    """Playlist files come from a script or a music folder, so they stay out like the music."""
    cfg = _config(tmp_path)
    state = cfg.index.stations_dir / "default" / "categories.json"
    state.write_text('{"schema_version": 1, "categories": []}', encoding="utf-8")
    playlists = cfg.index.shared_dir / "playlists"
    playlists.mkdir()
    (playlists / "A.m3u8").write_text("song.flac\n", encoding="utf-8")
    archive = create_backup(cfg, tmp_path / "categories.zip", force=False)
    with zipfile.ZipFile(archive) as backup:
        assert backup.read("index/categories.json") == state.read_bytes()
        assert not [name for name in backup.namelist() if name.endswith(".m3u8")]
    state.unlink()
    restore_backup(cfg, archive, force=True)
    assert json.loads(state.read_text(encoding="utf-8"))["schema_version"] == 1


def test_liner_play_dates_are_backed_up_and_restored(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = (cfg.index.stations_dir / "default") / "liner_dates.json"
    state.write_text(
        '{"schema_version": 1, "liners": {"promo.mp3": {"not_after": "2026-12-26"}}}',
        encoding="utf-8",
    )
    archive = create_backup(cfg, tmp_path / "dates.zip", force=False)
    with zipfile.ZipFile(archive) as backup:
        assert backup.read("index/liner_dates.json") == state.read_bytes()
    state.unlink()
    restore_backup(cfg, archive, force=True)
    assert "promo.mp3" in json.loads(state.read_text(encoding="utf-8"))["liners"]


def test_ads_their_audio_and_the_ad_play_log_are_backed_up_and_restored(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = (cfg.index.stations_dir / "default") / "ads.json"
    state.write_text('{"schema_version": 1, "spots": {}, "breaks": {}}', encoding="utf-8")
    audio = (cfg.index.stations_dir / "default") / "ads" / ("spot-" + "a" * 32 + ".wav")
    audio.parent.mkdir()
    audio.write_bytes(b"RIFF")
    plays = (cfg.index.stations_dir / "default") / "ad_plays" / "2026-10.jsonl"
    plays.parent.mkdir()
    plays.write_bytes(b'{"v": 1}\n')
    archive = create_backup(cfg, tmp_path / "ads.zip", force=False)
    with zipfile.ZipFile(archive) as backup:
        assert backup.read("index/ads.json") == state.read_bytes()
        assert backup.read(f"ads/{audio.name}") == b"RIFF"
        assert backup.read("ad_plays/2026-10.jsonl") == b'{"v": 1}\n'
    state.unlink()
    audio.unlink()
    plays.unlink()
    restore_backup(cfg, archive, force=True)
    assert json.loads(state.read_text(encoding="utf-8"))["schema_version"] == 1
    assert audio.read_bytes() == b"RIFF"
    assert plays.read_bytes() == b'{"v": 1}\n'


def test_matched_intros_and_their_audio_are_backed_up_and_restored(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = (cfg.index.stations_dir / "default") / "matched_intros.json"
    state.write_text('{"schema_version": 1, "intros": {}}', encoding="utf-8")
    audio = (cfg.index.stations_dir / "default") / "intros" / ("intro-" + "a" * 32 + ".wav")
    audio.parent.mkdir()
    audio.write_bytes(b"RIFF")
    archive = create_backup(cfg, tmp_path / "intros.zip", force=False)
    with zipfile.ZipFile(archive) as backup:
        assert backup.read("index/matched_intros.json") == state.read_bytes()
        assert backup.read(f"intros/{audio.name}") == b"RIFF"
    state.unlink()
    audio.unlink()
    restore_backup(cfg, archive, force=True)
    assert json.loads(state.read_text(encoding="utf-8"))["schema_version"] == 1
    assert audio.read_bytes() == b"RIFF"


def test_shows_are_backed_up_and_restored(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = (cfg.index.stations_dir / "default") / "shows.json"
    state.write_text('{"schema_version": 1, "shows": {}}', encoding="utf-8")
    archive = create_backup(cfg, tmp_path / "shows.zip", force=False)
    with zipfile.ZipFile(archive) as backup:
        assert backup.read("index/shows.json") == state.read_bytes()
    state.unlink()
    restore_backup(cfg, archive, force=True)
    assert json.loads(state.read_text(encoding="utf-8")) == {"schema_version": 1, "shows": {}}
