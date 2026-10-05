"""Doctor repairs require consent and revalidate the resulting configuration."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from autodj.cli import cli
from autodj.doctor import CheckStatus, DoctorCheck, DoctorReport


@pytest.mark.parametrize(
    "flags,answer,repairs",
    [
        ([], "", False),
        (["--no-fix"], "y\n", False),
        (["--fix"], "", True),
    ],
)
def test_unknown_key_consent(tmp_path: Path, flags, answer, repairs) -> None:
    config = tmp_path / "config.toml"
    original = b"[playback]\nenable_daypart = true\n"
    config.write_bytes(original)
    with (
        patch("autodj.doctor.run_doctor", return_value=DoctorReport(())) as run,
        patch("click.confirm") as confirm,
    ):
        result = CliRunner().invoke(cli, ["--config", str(config), "doctor", *flags], input=answer)
    assert result.exit_code == (0 if repairs else 1), result.output
    assert (config.read_bytes() != original) is repairs
    assert run.called is repairs
    if repairs:
        assert "Backup:" in result.output
    if "--fix" in flags:
        confirm.assert_not_called()


def test_interactive_default_offers_repair(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[playback]\nenable_daypart = true\n")
    with (
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=True),
        patch("autodj.doctor.run_doctor", return_value=DoctorReport(())),
    ):
        result = CliRunner().invoke(cli, ["--config", str(config), "doctor"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Apply this repair" in result.output


def test_interactive_default_declines_repair(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    original = b"[playback]\nenable_daypart = true\n"
    config.write_bytes(original)
    with (
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=True),
        patch("autodj.doctor.run_doctor", return_value=DoctorReport(())),
    ):
        result = CliRunner().invoke(cli, ["--config", str(config), "doctor"], input="n\n")
    assert result.exit_code == 1, result.output
    assert config.read_bytes() == original


@pytest.mark.parametrize("flags", [["--yes"], ["--fix", "--yes"], ["--json", "--fix"]])
def test_invalid_repair_options(flags) -> None:
    result = CliRunner().invoke(cli, ["doctor", *flags])
    assert result.exit_code == 2


def test_doctor_help_lists_repair_flags_without_yes() -> None:
    result = CliRunner().invoke(cli, ["doctor", "--help"])
    assert result.exit_code == 0, result.output
    assert "--fix" in result.output
    assert "--no-fix" in result.output
    assert "--yes" not in result.output


def test_json_never_repairs_unknown_keys(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    original = b"[playback]\nenable_daypart = true\n"
    config.write_bytes(original)
    result = CliRunner().invoke(cli, ["--config", str(config), "doctor", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["checks"][0]["name"] == "configuration"
    assert config.read_bytes() == original
    assert not list(tmp_path.glob("*.bak"))


def test_backup_failure_keeps_config_intact(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    original = b"[playback]\nenable_daypart = true\n"
    config.write_bytes(original)
    with patch(
        "autodj.doctor_repair.ConfigRepair._create_backup", side_effect=OSError("disk full")
    ):
        result = CliRunner().invoke(cli, ["--config", str(config), "doctor", "--fix"])
    assert result.exit_code == 1
    assert "Could not repair configuration" in result.output
    assert config.read_bytes() == original


def test_repairs_multiple_sections_and_rechecks_remaining_errors(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "[playback]\nenable_daypart = true\n[model]\nremoved = 1\n[server]\nport = -1\n"
    )
    original = config.read_bytes()
    result = CliRunner().invoke(cli, ["--config", str(config), "doctor", "--fix"])
    assert result.exit_code == 1
    assert "enable_daypart" not in config.read_text()
    assert "removed" not in config.read_text()
    assert "port = -1" in config.read_text()
    backups = list(tmp_path.glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


@pytest.mark.parametrize("approve", [True, False])
def test_batch_preview_and_single_confirmation_across_files(tmp_path: Path, approve: bool) -> None:
    config = tmp_path / "config.toml"
    local = tmp_path / "config.local.toml"
    original = b"[playback]\nenable_daypart = true\n[model]\nremoved = 1\n"
    overlay = b"[server]\nliner_upload_max_bytes = 1024\n"
    config.write_bytes(original)
    local.write_bytes(overlay)
    with (
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=True),
        patch("autodj.doctor.run_doctor", return_value=DoctorReport(())),
    ):
        result = CliRunner().invoke(
            cli, ["--config", str(config), "doctor"], input="y\n" if approve else "n\n"
        )
    assert result.exit_code == (0 if approve else 1), result.output
    assert result.output.count("Apply this repair") == 1
    preview = result.output.split("Apply this repair")[0]
    assert "[playback]: ['enable_daypart']" in preview
    assert "[model]: ['removed']" in preview
    assert "[server]: ['liner_upload_max_bytes']" in preview
    backups = list(tmp_path.glob("*.bak"))
    if approve:
        assert len(backups) == 2
        assert {backup.read_bytes() for backup in backups} == {original, overlay}
        assert "enable_daypart" not in config.read_text()
        assert "removed" not in config.read_text()
        assert "liner_upload_max_bytes" not in local.read_text()
    else:
        assert backups == []
        assert config.read_bytes() == original
        assert local.read_bytes() == overlay


def test_finds_unknown_keys_after_unrelated_validation_error(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[playback]\ncrossfade_seconds = -1\n[model]\nremoved = 1\n")
    result = CliRunner().invoke(cli, ["--config", str(config), "doctor", "--fix"])
    assert result.exit_code == 1, result.output
    assert "removed" not in config.read_text()
    assert "crossfade_seconds = -1" in config.read_text()
    assert len(list(tmp_path.glob("*.bak"))) == 1


def test_directory_repairs_recheck_and_preserve_failure_exit(tmp_path: Path) -> None:
    from autodj.config import load_config

    config = tmp_path / "config.toml"
    config.write_text("[index]\nindex_dir = 'missing-index'\nmodel_dir = 'missing-model'\n")
    cfg = load_config(config, environ={})
    cfg.index.index_dir = tmp_path / "missing-index"
    cfg.index.model_dir = tmp_path / "missing-model"
    initial = DoctorReport(
        tuple(
            DoctorCheck(name, CheckStatus.WARN, "missing") for name in ("index-path", "model-path")
        )
    )
    final = DoctorReport((DoctorCheck("python", CheckStatus.FAIL, "unsupported"),))
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch("autodj.doctor.run_doctor", side_effect=[initial, final]) as run,
        patch("click.confirm") as confirm,
    ):
        result = CliRunner().invoke(cli, ["doctor", "--fix"])
    assert result.exit_code == 1, result.output
    assert cfg.index.index_dir.is_dir()
    assert cfg.index.model_dir.is_dir()
    assert run.call_count == 2
    assert "Rechecking" in result.output
    confirm.assert_not_called()
