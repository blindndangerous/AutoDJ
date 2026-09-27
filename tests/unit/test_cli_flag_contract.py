"""Deployment files may only pass flags the CLI actually defines.

The container CMD, the compose services and the workflows all invoke
``autodj``.  If one of them passes a flag Click does not know, ``autodj serve``
exits 2 before it starts, so every flag they use is checked against the options
the CLI declares.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

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
    assert _container_invocations() == [["serve"]]
    assert len(_compose_invocations()) == 3
    assert len(_workflow_invocations()) >= 3
    assert any("--insecure-lan" in argv for argv in _compose_invocations())


@pytest.mark.parametrize("argv", _invocations(), ids=lambda argv: " ".join(argv))
def test_deployment_flags_exist_on_the_invoked_command(argv: list[str]) -> None:
    subcommand, *rest = argv
    assert subcommand in cli.commands, f"{subcommand} is not an autodj subcommand"
    declared = _declared_flags(subcommand)
    used = {token for token in rest if token.startswith("--")}
    assert used <= declared, f"{subcommand} does not accept {sorted(used - declared)}"
