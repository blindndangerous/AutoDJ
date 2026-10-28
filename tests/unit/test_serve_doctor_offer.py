"""Failed serve commands can offer Doctor without masking the startup failure."""

import os
import sys
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from autodj import server
from autodj.cli import _ServeCommand
from autodj.update_contract import UPDATE_DIR_ENV
from autodj.updates import SUPERVISOR_ENV


@pytest.fixture(autouse=True)
def _not_started(monkeypatch):
    """Other tests run the app's lifespan, which marks startup complete."""
    monkeypatch.delenv(SUPERVISOR_ENV, raising=False)
    monkeypatch.delenv(UPDATE_DIR_ENV, raising=False)
    server.STARTUP_COMPLETE.clear()
    yield
    server.STARTUP_COMPLETE.clear()


@pytest.mark.parametrize(
    "interactive,answer,guard,run",
    [
        (True, "y\n", "", True),
        (True, "n\n", "", False),
        (False, "y\n", "", False),
        (True, "y\n", "1", False),
    ],
)
def test_offer_doctor(interactive, answer, guard, run):
    failure = RuntimeError("startup failed")

    @click.command(cls=_ServeCommand)
    def command():
        raise failure

    with (
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=interactive),
        patch.dict(os.environ, {"AUTODJ_DOCTOR_ACTIVE": guard}),
        patch("subprocess.run") as subprocess_run,
    ):
        result = CliRunner().invoke(
            command, [], obj={"config_path": "custom config.toml"}, input=answer
        )
    assert result.exception is failure
    assert subprocess_run.called is run
    if run:
        assert subprocess_run.call_args.args[0] == [
            sys.executable,
            "-m",
            "autodj",
            "--config",
            "custom config.toml",
            "doctor",
        ]
        environment = subprocess_run.call_args.kwargs["env"]
        assert environment["AUTODJ_RUNTIME"] == "current"
        assert environment["AUTODJ_DOCTOR_ACTIVE"] == "1"
        assert "run your serve command again" in result.output


@pytest.mark.parametrize("doctor_error", [OSError("cannot launch"), KeyboardInterrupt()])
def test_doctor_launch_failure_preserves_serve_failure(doctor_error):
    failure = RuntimeError("original failure")

    @click.command(cls=_ServeCommand)
    def command():
        raise failure

    with (
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=True),
        patch.dict(os.environ, {"AUTODJ_DOCTOR_ACTIVE": ""}),
        patch("subprocess.run", side_effect=doctor_error),
    ):
        result = CliRunner().invoke(command, input="y\n")
    assert result.exception is failure
    assert "Doctor did not finish" in result.output


def test_no_offer_after_startup_completed():
    """A crash mid-broadcast exits at once so a restart loop is never left waiting."""
    failure = RuntimeError("crashed while on air")

    @click.command(cls=_ServeCommand)
    def command():
        server.STARTUP_COMPLETE.set()
        raise failure

    with (
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=True),
        patch.dict(os.environ, {"AUTODJ_DOCTOR_ACTIVE": ""}),
        patch("subprocess.run") as subprocess_run,
    ):
        result = CliRunner().invoke(command, input="y\n")
    assert result.exception is failure
    assert not subprocess_run.called
    assert "Run AutoDJ Doctor now?" not in result.output
    assert "autodj doctor" in result.output


def test_a_requested_restart_exits_quietly_without_offering_doctor():
    """ADR-0016-10: exit status 75 after Restart is not a failed start."""
    from autodj.updates import RESTART_EXIT_CODE

    @click.command(cls=_ServeCommand)
    def command():
        raise click.exceptions.Exit(RESTART_EXIT_CODE)

    with (
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=True),
        patch("subprocess.run") as subprocess_run,
    ):
        result = CliRunner().invoke(command, [], obj={}, input="y\n")
    assert result.exit_code == RESTART_EXIT_CODE
    assert "troubleshoot" not in result.output
    subprocess_run.assert_not_called()


@pytest.mark.parametrize(
    "variable,value",
    [(SUPERVISOR_ENV, "1"), (UPDATE_DIR_ENV, "/srv/autodj/.uv/update")],
)
def test_no_offer_under_a_launcher_or_updater(variable, value):
    """A watched server fails at once, so an update rolls back on its exit status."""
    failure = RuntimeError("the new version cannot start")

    @click.command(cls=_ServeCommand)
    def command():
        raise failure

    with (
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=True),
        patch.dict(os.environ, {"AUTODJ_DOCTOR_ACTIVE": "", variable: value}),
        patch("subprocess.run") as subprocess_run,
    ):
        result = CliRunner().invoke(command, input="y\n")
    assert result.exception is failure
    assert not subprocess_run.called
    assert "Run AutoDJ Doctor now?" not in result.output
    assert "autodj doctor" in result.output
