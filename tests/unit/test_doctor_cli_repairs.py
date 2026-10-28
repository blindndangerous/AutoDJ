"""Doctor repairs require consent and revalidate the resulting configuration."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from autodj.cli import cli
from autodj.doctor import CheckStatus, DoctorCheck, DoctorReport


@pytest.fixture(autouse=True)
def _isolate_default_index(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)


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
    original = b"[server]\nold_option = true\n"
    config.write_bytes(original)
    with (
        patch("autodj.doctor.run_doctor", return_value=DoctorReport(())) as run,
        patch("click.confirm") as confirm,
    ):
        result = CliRunner().invoke(cli, ["--config", str(config), "doctor", *flags], input=answer)
    assert result.exit_code == 0, result.output
    assert (config.read_bytes() != original) is repairs
    assert run.called
    if repairs:
        assert "Backup:" in result.output
    if "--fix" in flags:
        confirm.assert_not_called()


def test_interactive_default_offers_repair(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[server]\nold_option = true\n")
    with (
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=True),
        patch("autodj.doctor.run_doctor", return_value=DoctorReport(())),
    ):
        result = CliRunner().invoke(cli, ["--config", str(config), "doctor"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Apply this repair" in result.output


def test_interactive_default_declines_repair(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    original = b"[server]\nold_option = true\n"
    config.write_bytes(original)
    with (
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=True),
        patch("autodj.doctor.run_doctor", return_value=DoctorReport(())),
    ):
        result = CliRunner().invoke(cli, ["--config", str(config), "doctor"], input="n\n")
    assert result.exit_code == 0, result.output
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
    original = b"[server]\nold_option = true\n"
    config.write_bytes(original)
    with patch("autodj.doctor.run_doctor", return_value=DoctorReport(())):
        result = CliRunner().invoke(cli, ["--config", str(config), "doctor", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["exit_code"] == 0
    assert config.read_bytes() == original
    assert not list(tmp_path.glob("*.bak"))


def test_backup_failure_keeps_config_intact(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    original = b"[server]\nold_option = true\n"
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
    config.write_text("[server]\nold_option = true\nport = -1\n[model]\nremoved = 1\n")
    original = config.read_bytes()
    result = CliRunner().invoke(cli, ["--config", str(config), "doctor", "--fix"])
    assert result.exit_code == 1
    assert "old_option" not in config.read_text()
    assert "removed" not in config.read_text()
    assert "port = -1" in config.read_text()
    backups = list(tmp_path.glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


@pytest.mark.parametrize("approve", [True, False])
def test_batch_preview_and_single_confirmation_across_files(tmp_path: Path, approve: bool) -> None:
    config = tmp_path / "config.toml"
    local = tmp_path / "config.local.toml"
    original = b"[server]\nold_option = true\n[model]\nremoved = 1\n"
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
    assert result.exit_code == 0, result.output
    assert result.output.count("Apply this repair") == 1
    preview = result.output.split("Apply this repair")[0]
    assert "[server]: ['old_option']" in preview
    assert "[model]: ['removed']" in preview
    assert "[server]: ['liner_upload_max_bytes']" in preview
    backups = list(tmp_path.glob("*.bak"))
    if approve:
        assert len(backups) == 2
        assert {backup.read_bytes() for backup in backups} == {original, overlay}
        assert "old_option" not in config.read_text()
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
    initial = DoctorReport((DoctorCheck("index-path", CheckStatus.WARN, "missing"),))
    final = DoctorReport((DoctorCheck("python", CheckStatus.FAIL, "unsupported"),))
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch("autodj.doctor.run_doctor", side_effect=[initial, final]) as run,
        patch("click.confirm") as confirm,
    ):
        result = CliRunner().invoke(cli, ["doctor", "--fix"])
    assert result.exit_code == 1, result.output
    assert cfg.index.index_dir.is_dir()
    assert not cfg.index.model_dir.exists()
    assert run.call_count == 2
    assert "Rechecking" in result.output
    confirm.assert_not_called()


@pytest.mark.parametrize(
    "flags,interactive,answer,repaired",
    [
        ([], False, "", False),
        (["--no-fix"], True, "y\n", False),
        (["--json"], True, "y\n", False),
        ([], True, "n\n", False),
        ([], True, "y\n", True),
        (["--fix"], False, "", True),
    ],
)
def test_saved_settings_repair_consent(tmp_path, flags, interactive, answer, repaired):
    from autodj.config import load_config
    from autodj.doctor import _saved_settings_check

    config = tmp_path / "config.toml"
    config.write_text("")
    cfg = load_config(config, environ={})
    cfg.index.index_dir = tmp_path / "index"
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    state = (cfg.index.stations_dir / "default") / "web_state.json"
    original = json.dumps(
        {
            "schema_version": 2,
            "playback": {
                "repeat_rules": {
                    "related_artists_file": "",
                    "artist": {"minutes": 40, "tracks": 0},
                    "title": {"minutes": 460, "tracks": 0},
                }
            },
            "eq": {"low": 1.41},
        }
    ).encode()
    state.write_bytes(original)
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch(
            "autodj.doctor.run_doctor",
            side_effect=lambda cfg, station=None, check_updates=True: DoctorReport(
                (_saved_settings_check(cfg),)
            ),
        ),
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=interactive),
    ):
        result = CliRunner().invoke(cli, ["doctor", *flags], input=answer)
    assert result.exit_code == 0, result.output
    assert (state.read_bytes() != original) is repaired
    backups = list((cfg.index.stations_dir / "default").glob("*.bak"))
    if repaired:
        assert len(backups) == 1 and backups[0].read_bytes() == original
        expected = json.loads(original)
        del expected["playback"]["repeat_rules"]["related_artists_file"]
        assert json.loads(state.read_bytes()) == expected
    else:
        assert not backups
    if "--json" in flags:
        assert json.loads(result.stdout)["exit_code"] == 0


@pytest.mark.parametrize("failure_kind", ["custom-path", "backup-error"])
def test_saved_settings_unsafe_or_failed_repair_keeps_file(tmp_path, failure_kind):
    from autodj.config import load_config
    from autodj.doctor import _saved_settings_check

    config = tmp_path / "config.toml"
    config.write_text("")
    cfg = load_config(config, environ={})
    cfg.index.index_dir = tmp_path / "index"
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    state = (cfg.index.stations_dir / "default") / "web_state.json"
    original = json.dumps(
        {
            "schema_version": 2,
            "playback": {
                "repeat_rules": {
                    "related_artists_file": "custom.toml" if failure_kind == "custom-path" else ""
                }
            },
        }
    ).encode()
    state.write_bytes(original)
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch(
            "autodj.doctor.run_doctor",
            side_effect=lambda cfg, station=None, check_updates=True: DoctorReport(
                (_saved_settings_check(cfg),)
            ),
        ),
        patch(
            "autodj.doctor_repair.ConfigRepair._create_backup", side_effect=OSError("disk full")
        ) as backup,
    ):
        result = CliRunner().invoke(cli, ["doctor", "--fix"])
    assert result.exit_code == (0 if failure_kind == "custom-path" else 1), result.output
    assert state.read_bytes() == original
    assert not list((cfg.index.stations_dir / "default").glob("*.bak"))
    if failure_kind == "custom-path":
        backup.assert_not_called()
        assert "No safe automatic edit" in result.output
    else:
        assert "Could not repair saved settings" in result.output
