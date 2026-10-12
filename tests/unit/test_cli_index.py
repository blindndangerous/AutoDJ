"""CLI tests for index post-passes, backup errors and device listing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from autodj.backup import BackupError
from autodj.cli import cli
from autodj.repeat_settings import RepeatSettings


def _cfg() -> MagicMock:
    cfg = MagicMock()
    cfg.library.beets_db = None
    cfg.library.music_dir = Path("Z:/Music")
    cfg.playback.repeat_rules = RepeatSettings()
    cfg.playback.crossfade_seconds = 3.0
    cfg.playback.discovery_every = None
    cfg.playback.pick_top_k = 1
    cfg.playback.pick_temperature = 0.3
    cfg.presets = {}
    return cfg


# ---------------------------------------------------------------------------
# stats --name validation
# ---------------------------------------------------------------------------


def _write_min_cfg(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[library]\nmusic_dir = "Z:/Music"\n'
        '[index]\nindex_dir = "Z:/idx"\nmodel_dir = "models"\n'
        "[playback]\ncrossfade_seconds = 3.0\nrepeat_rules = { track = { tracks = 50 } }\n"
        '[model]\nname = "OpenMuQ/MuQ-large-msd-iter"\n',
        encoding="utf-8",
    )
    return cfg


class TestStatsNameFlag:
    def test_stats_invalid_name_exits(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        result = CliRunner().invoke(cli, ["--config", str(cfg), "stats", "--name", "../bad"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# index command post-passes and failures
# ---------------------------------------------------------------------------


class TestIndexCommand:
    def test_index_help_keeps_options(self) -> None:
        result = CliRunner().invoke(cli, ["index", "--help"])
        assert result.exit_code == 0
        for option in ("--limit", "--force", "--workers", "--no-analyse", "--no-enrich"):
            assert option in result.output


class TestBackupErrors:
    def test_backup_error_becomes_click_error(self, tmp_path: Path) -> None:
        with (
            patch("autodj.cli._load_cfg_or_exit", return_value=_cfg()),
            patch(
                "autodj.backup.create_backup",
                side_effect=BackupError("snapshot unavailable"),
            ),
        ):
            result = CliRunner().invoke(cli, ["backup", str(tmp_path / "backup.zip")])

        assert result.exit_code == 1
        assert "snapshot unavailable" in result.output

    def test_restore_error_becomes_click_error(self, tmp_path: Path) -> None:
        archive = tmp_path / "backup.zip"
        archive.touch()
        with (
            patch("autodj.cli._load_cfg_or_exit", return_value=_cfg()),
            patch(
                "autodj.backup.restore_backup",
                side_effect=BackupError("archive invalid"),
            ),
        ):
            result = CliRunner().invoke(cli, ["restore", str(archive)])

        assert result.exit_code == 1
        assert "archive invalid" in result.output


# ---------------------------------------------------------------------------
# list-devices
# ---------------------------------------------------------------------------


class TestListDevices:
    def test_no_output_devices(self) -> None:
        sd_mock = MagicMock()
        sd_mock.default.device = (0, 0)
        sd_mock.query_devices.return_value = [
            {"name": "Mic", "max_output_channels": 0, "default_samplerate": 44100},
        ]
        with patch.dict("sys.modules", {"sounddevice": sd_mock}):
            result = CliRunner().invoke(cli, ["list-devices"])
        assert result.exit_code == 0
        assert "No output devices found" in result.output
