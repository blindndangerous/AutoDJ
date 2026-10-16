"""Recording options and the config-only archive path."""

from pathlib import Path

import pytest

from autodj.config import load_config, move_station_tables


@pytest.fixture(autouse=True)
def _isolate_default_index(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)


def _load_migrated_config(path: Path):
    cfg = load_config(path, environ={})
    move_station_tables(cfg)
    return load_config(path, environ={})


def test_recording_options_load_from_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[recording]\nfolder = "archives"\nsplit = "show"\n'
        "retention_days = 14\nfree_space_mb = 250\n",
        encoding="utf-8",
    )
    cfg = _load_migrated_config(path)
    assert cfg.default_station is not None
    recording = cfg.default_station.cfg.recording
    assert recording.folder == Path("archives")
    assert recording.split == "show"
    assert recording.retention_days == 14
    assert recording.free_space_mb == 250


def test_local_config_sets_this_machine_recording_folder(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[recording]\nfolder = "shared"\nsplit = "show"\n', encoding="utf-8")
    (tmp_path / "config.local.toml").write_text(
        '[recording]\nfolder = "~/elsewhere"\n', encoding="utf-8"
    )
    cfg = _load_migrated_config(path)
    assert cfg.default_station is not None
    recording = cfg.default_station.cfg.recording
    assert recording.folder == Path("~/elsewhere").expanduser()
    assert recording.split == "show"


def test_recording_folder_must_not_be_empty(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[recording]\nfolder = " "\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"recording\.folder must be a non-empty path"):
        _load_migrated_config(path)
