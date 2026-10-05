"""Recovery execution uses explicit actions, consent, and updated diagnostics."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from autodj.cli import cli
from autodj.config import load_config
from autodj.doctor import CheckStatus, DoctorCheck, DoctorReport
from autodj.doctor_commands import run_recovery_commands


def report_for(*actions: str) -> DoctorReport:
    return DoctorReport(
        tuple(DoctorCheck(action, CheckStatus.FAIL, "broken", repair=action) for action in actions)
    )


def config_for(tmp_path: Path):
    path = tmp_path / "custom config.toml"
    path.write_text("[index]\nname = 'workout'\n")
    cfg = load_config(path, environ={})
    cfg.index.index_dir = tmp_path / "index"
    return cfg


def test_rebuild_supersedes_index_and_rechecks_before_analyse(tmp_path: Path) -> None:
    cfg = config_for(tmp_path)
    initial = report_for("index", "rebuild-index", "analyse")
    healthy = DoctorReport(())
    with (
        patch("autodj.doctor_commands.click.confirm", return_value=True) as confirm,
        patch(
            "autodj.doctor_commands.subprocess.run", return_value=SimpleNamespace(returncode=0)
        ) as run,
        patch("autodj.doctor.run_doctor", return_value=healthy) as recheck,
    ):
        final, failed = run_recovery_commands(cfg, initial, auto_fix=False)
    assert final is healthy
    assert not failed
    assert confirm.call_count == 1
    recheck.assert_called_once_with(cfg)
    run.assert_called_once_with(
        [
            sys.executable,
            "-m",
            "autodj",
            "--config",
            str(cfg.config_path),
            "index",
            "--force",
            "--name",
            "workout",
        ],
        check=False,
    )


def test_declining_all_commands_does_not_execute_or_recheck(tmp_path: Path) -> None:
    initial = report_for("rebuild-index", "index", "analyse", "serve-lan")
    with (
        patch("autodj.doctor_commands.click.confirm", return_value=False) as confirm,
        patch("autodj.doctor_commands.subprocess.run") as run,
        patch("autodj.doctor.run_doctor") as recheck,
    ):
        final, failed = run_recovery_commands(config_for(tmp_path), initial, auto_fix=False)
    assert final is initial
    assert not failed
    assert confirm.call_count == 3
    run.assert_not_called()
    recheck.assert_not_called()


@pytest.mark.parametrize("failure", [SimpleNamespace(returncode=7), OSError("cannot launch")])
def test_failed_recovery_is_not_retried_and_exits_nonzero(tmp_path: Path, failure) -> None:
    cfg = config_for(tmp_path)
    initial = report_for("index")
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch("autodj.doctor.run_doctor", side_effect=[initial, DoctorReport(())]),
        patch(
            "autodj.doctor_commands.subprocess.run",
            **(
                {"side_effect": failure}
                if isinstance(failure, Exception)
                else {"return_value": failure}
            ),
        ) as run,
        patch("autodj.doctor_commands.click.confirm") as confirm,
    ):
        result = CliRunner().invoke(cli, ["doctor", "--fix"])
    assert result.exit_code == 1, result.output
    run.assert_called_once()
    confirm.assert_not_called()


def test_unresolved_successful_action_runs_only_once(tmp_path: Path) -> None:
    initial = report_for("index")
    with (
        patch(
            "autodj.doctor_commands.subprocess.run", return_value=SimpleNamespace(returncode=0)
        ) as run,
        patch("autodj.doctor.run_doctor", return_value=initial),
    ):
        final, failed = run_recovery_commands(config_for(tmp_path), initial, auto_fix=True)
    assert final is initial
    assert not failed
    run.assert_called_once()


@pytest.mark.parametrize("flags", [[], ["--no-fix"], ["--json"]])
def test_readonly_modes_never_run_recovery(tmp_path: Path, flags) -> None:
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=config_for(tmp_path)),
        patch("autodj.doctor.run_doctor", return_value=report_for("rebuild-index")),
        patch("autodj.doctor_commands.subprocess.run") as run,
    ):
        result = CliRunner().invoke(cli, ["doctor", *flags])
    assert result.exit_code == 1
    run.assert_not_called()


def test_interactive_doctor_offers_index_rebuild(tmp_path: Path) -> None:
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=config_for(tmp_path)),
        patch(
            "autodj.doctor.run_doctor", side_effect=[report_for("rebuild-index"), DoctorReport(())]
        ),
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=True),
        patch(
            "autodj.doctor_commands.subprocess.run", return_value=SimpleNamespace(returncode=0)
        ) as run,
    ):
        result = CliRunner().invoke(cli, ["doctor"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "index --force" in result.output
    assert "Run this command now?" in result.output
    run.assert_called_once()


def test_cache_backup_happens_before_analyse(tmp_path: Path) -> None:
    cfg = config_for(tmp_path)
    order = []
    with (
        patch(
            "autodj.doctor_cache.backup_dj_meta_cache",
            side_effect=lambda path: order.append("backup"),
        ),
        patch(
            "autodj.doctor_commands.subprocess.run",
            side_effect=lambda *args, **kwargs: (
                order.append("run") or SimpleNamespace(returncode=0)
            ),
        ),
        patch("autodj.doctor.run_doctor", return_value=DoctorReport(())),
    ):
        run_recovery_commands(cfg, report_for("rebuild-dj-meta"), auto_fix=True)
    assert order == ["backup", "run"]


def test_error_text_cannot_supply_commands(tmp_path: Path) -> None:
    report = DoctorReport(
        (DoctorCheck("index", CheckStatus.FAIL, "unknown", "run `evil command`"),)
    )
    with patch("autodj.doctor_commands.subprocess.run") as run:
        run_recovery_commands(config_for(tmp_path), report, auto_fix=True)
    run.assert_not_called()


def test_backup_failure_does_not_start_analyse(tmp_path: Path) -> None:
    initial = report_for("rebuild-dj-meta")
    with (
        patch("autodj.doctor_cache.backup_dj_meta_cache", side_effect=ValueError("not a file")),
        patch("autodj.doctor_commands.subprocess.run") as run,
        patch("autodj.doctor.run_doctor", return_value=initial),
    ):
        final, failed = run_recovery_commands(config_for(tmp_path), initial, auto_fix=True)
    assert final is initial
    assert failed
    run.assert_not_called()
