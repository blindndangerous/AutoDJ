"""Deployment files may only pass flags the CLI actually defines.

Removing ``serve --no-playback`` from cli.py left six deployment call sites
(the container CMD, both compose services and three workflows) invoking a flag
Click no longer knew, which makes ``autodj serve`` exit 2 before it starts.
Nothing in the suite noticed, because nothing compared the two.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from autodj.cli import cli

ROOT = Path(__file__).resolve().parents[2]
_FLAG = re.compile(r"--[a-z][a-z0-9-]*")


def _container_invocations() -> list[list[str]]:
    """Return the argv lists the container image runs."""
    text = (ROOT / "Containerfile").read_text(encoding="utf-8")
    found = []
    for match in re.finditer(r"^(?:CMD|ENTRYPOINT)\s+(\[[^\]]*\])", text, re.MULTILINE):
        argv = json.loads(match.group(1))
        if argv and not argv[0].startswith("/"):
            found.append(argv)
    return found


def _compose_invocations() -> list[list[str]]:
    """Return the argv lists every compose service runs."""
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    found = []
    for service in (compose.get("services") or {}).values():
        command = service.get("command")
        if isinstance(command, list) and command:
            found.append([str(token) for token in command])
    return found


def _workflow_invocations() -> list[list[str]]:
    """Return every ``autodj <subcommand> ...`` shell line in the workflows."""
    found = []
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            match = re.search(r"\bautodj\s+([a-z][a-z-]*)(.*)$", line)
            if match is None:
                continue
            found.append([match.group(1), *_FLAG.findall(match.group(2))])
    return found


def _invocations() -> list[list[str]]:
    return [
        *_container_invocations(),
        *_compose_invocations(),
        *_workflow_invocations(),
    ]


def _declared_flags(subcommand: str) -> set[str]:
    """Return every option string the named subcommand accepts."""
    command = cli.commands[subcommand]
    return {opt for param in command.params for opt in param.opts + param.secondary_opts}


def test_extraction_actually_finds_the_deployment_invocations() -> None:
    """Guard the guard: an extractor that finds nothing would pass vacuously."""
    invocations = _invocations()
    assert len(invocations) >= 6
    assert any("--no-playback" in argv for argv in invocations)


@pytest.mark.parametrize("argv", _invocations(), ids=lambda argv: " ".join(argv))
def test_deployment_flags_exist_on_the_invoked_command(argv: list[str]) -> None:
    subcommand, *rest = argv
    assert subcommand in cli.commands, f"{subcommand} is not an autodj subcommand"
    declared = _declared_flags(subcommand)
    used = {token for token in rest if token.startswith("--")}
    assert used <= declared, f"{subcommand} does not accept {sorted(used - declared)}"


def test_serve_still_accepts_the_deprecated_no_playback_flag() -> None:
    result = CliRunner().invoke(cli, ["serve", "--no-playback", "--help"])
    assert result.exit_code == 0, result.output


def test_deprecated_no_playback_stays_out_of_the_help_text() -> None:
    result = CliRunner().invoke(cli, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--no-playback" not in result.output
