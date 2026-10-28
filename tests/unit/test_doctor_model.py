"""Explicit cache recovery does not depend on pending similarity jobs."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from autodj.cli import cli
from autodj.doctor import CheckStatus, DoctorCheck, DoctorReport
from autodj.doctor_commands import run_recovery_commands
from tests.unit.test_doctor_commands import config_for


@pytest.mark.parametrize("complete", [True, False])
def test_download_model_validates_without_loading_or_analyzing(tmp_path, complete):
    cfg = config_for(tmp_path)
    model = MagicMock()
    model.download_model_if_needed.return_value = tmp_path / "weights"
    model.inspect_model_cache.return_value = SimpleNamespace(complete=complete)
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch("autodj.model_identity.model_analysis_available", return_value=True),
        patch.dict(sys.modules, {"autodj.model": model}),
        patch("autodj.library.analyse_library") as analyze,
    ):
        result = CliRunner().invoke(cli, ["--config", str(cfg.config_path), "download-model"])
    assert result.exit_code == (0 if complete else 1)
    model.download_model_if_needed.assert_called_once_with(
        cfg.model, cfg.index, hf_token=cfg.huggingface.token
    )
    model.inspect_model_cache.assert_called_once_with(cfg.model, cfg.index)
    model.load_model.assert_not_called()
    analyze.assert_not_called()


@pytest.mark.parametrize("consent", [False, True])
def test_model_recovery_uses_selected_config_and_interpreter(tmp_path, consent):
    cfg = config_for(tmp_path)
    report = DoctorReport(
        (DoctorCheck("model-cache", CheckStatus.WARN, "missing", repair="download-model"),)
    )
    with (
        patch("autodj.doctor_commands.click.confirm", return_value=consent),
        patch(
            "autodj.doctor_commands.subprocess.run", return_value=SimpleNamespace(returncode=0)
        ) as run,
        patch("autodj.doctor.run_doctor", return_value=DoctorReport(())) as recheck,
    ):
        _, failed = run_recovery_commands(cfg, report, auto_fix=False)
    assert not failed
    if consent:
        run.assert_called_once_with(
            [sys.executable, "-m", "autodj", "--config", str(cfg.config_path), "download-model"],
            check=False,
        )
        recheck.assert_called_once_with(cfg, station=None, check_updates=False)
    else:
        run.assert_not_called()
        recheck.assert_not_called()
