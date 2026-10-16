"""Failed serve commands can offer Doctor without masking the startup failure."""

import os
import sys
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from autodj import server
from autodj.cli import _ServeCommand


@pytest.fixture(autouse=True)
def _not_started():
    """Other tests run the app's lifespan, which marks startup complete."""
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
