"""CLI failures use stderr without mixing errors into command data."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from autodj.cli import cli
from tests.unit.test_library import make_config


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["stats"], "Current library not found:"),
        (["enrich"], "No [library] beets_db in config"),
        (["analyse"], "No current library at"),
        (["export-plays", "--from", "2026-01-01", "--to", "2026-01-02"], "No play log found:"),
        (["simulate", "--picks", "1"], "No music library in"),
    ],
)
def test_missing_inputs_are_click_errors_on_stderr(tmp_path: Path, arguments, message) -> None:
    """ADR-0022-03: application failures use stderr and exit one."""
    cfg = make_config(tmp_path)
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch("autodj.cli._can_import", return_value=True),
    ):
        result = CliRunner().invoke(cli, arguments)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("Error: ")
    assert message in result.stderr


def test_config_error_preserves_literal_brackets_on_stderr() -> None:
    with patch("autodj.config.load_config", side_effect=ValueError("bad [server] setting")):
        result = CliRunner().invoke(cli, ["stats"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "Error: Config not found or invalid: bad [server] setting\n"


def test_layout_error_is_on_stderr() -> None:
    from autodj.layout import LayoutError

    with patch("autodj.config.load_config", side_effect=LayoutError("cannot move [library]")):
        result = CliRunner().invoke(cli, ["stats"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "Error: cannot move [library]\n"


def test_missing_sounddevice_is_on_stderr() -> None:
    with patch.dict("sys.modules", {"sounddevice": None}):
        result = CliRunner().invoke(cli, ["list-devices"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("Error: sounddevice is not installed.")
    assert "uv sync --extra play" in result.stderr
    assert "[bold" not in result.stderr


@pytest.mark.parametrize("command", ["enrich", "analyse"])
def test_processing_failure_is_on_stderr(tmp_path: Path, command: str) -> None:
    cfg = make_config(tmp_path)
    cfg.index.library_dir.mkdir(parents=True)
    (cfg.index.library_dir / "library.db").touch()
    cfg.library.beets_db = tmp_path / "beets.db"
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch("autodj.cli._can_import", return_value=True),
        patch("autodj.indexer.enrich_from_beets", side_effect=RuntimeError("bad [input]")),
        patch("autodj.catalog.LibraryCatalog", side_effect=RuntimeError("bad [input]")),
    ):
        result = CliRunner().invoke(cli, [command])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == f"Error: {command.capitalize()} failed: bad [input]\n"


def test_missing_analysis_packages_are_on_stderr() -> None:
    with patch("autodj.cli._can_import", return_value=False):
        result = CliRunner().invoke(cli, ["analyse"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("Error: Cannot analyse")
    assert "librosa, soundfile" in result.stderr


@pytest.mark.parametrize(
    "arguments",
    [["doctor", "--json", "--fix"], ["simulate", "--hours", "1", "--picks", "1"]],
)
def test_application_option_conflicts_exit_two(arguments) -> None:
    """ADR-0022-03: usage errors use stderr and exit two."""
    result = CliRunner().invoke(cli, arguments)
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Error: " in result.stderr


@pytest.mark.parametrize("status", [2, 9, 130])
def test_setup_failure_preserves_child_status_on_stderr(status) -> None:
    """ADR-0022-04: setup preserves argument-error and interruption child statuses."""
    with patch("autodj.launcher.run_setup", return_value=status):
        result = CliRunner().invoke(cli, ["setup", "--dry-run"])
    assert result.exit_code == status
    assert result.stdout == ""
    assert result.stderr == f"Error: Setup failed with exit code {status}.\n"
