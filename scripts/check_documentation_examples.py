"""Parse bounded documented shell examples; optionally run reviewed config fixtures only."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = ("core-operations", "manage-access", "collect-diagnostics")


def shell_blocks(document: str, shell: str) -> list[str]:
    """Extract complete fences, preserving their content apart from list indentation."""
    blocks = []
    lines = document.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        match = re.fullmatch(r"( *)```" + shell + r"\s*\n?", lines[index])
        index += 1
        if not match:
            continue
        content = []
        while index < len(lines) and lines[index].strip() != "```":
            content.append(lines[index])
            index += 1
        if index == len(lines):
            raise ValueError(f"Unclosed {shell} fence")
        blocks.append(textwrap.dedent("".join(content)))
        index += 1
    return blocks


def shell_tool(shell: str) -> str:
    tool = shutil.which("bash" if shell == "bash" else "pwsh")
    if not tool:
        raise ValueError(f"Required {shell} tool missing: {'bash' if shell == 'bash' else 'pwsh'}")
    return tool


def clean_environment(scratch: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("AUTODJ_", "BASH_FUNC_"))
        and key.upper() not in {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"}
    }
    # Bash reads BASH_ENV even for noninteractive invocations; never source caller code.
    shell_home = scratch / "shell-home"
    shell_home.mkdir(exist_ok=True)
    env.update(
        HOME=str(shell_home),
        USERPROFILE=str(shell_home),
        POWERSHELL_TELEMETRY_OPTOUT="1",
        TMPDIR=str(scratch),
        TMP=str(scratch),
        TEMP=str(scratch),
    )
    return env


def check_syntax(block: str, shell: str, tool: str) -> None:
    with tempfile.TemporaryDirectory(prefix="autodj-doc-syntax-") as directory:
        scratch = Path(directory)
        script = scratch / ("example.sh" if shell == "bash" else "example.ps1")
        script.write_text(block, encoding="utf-8")
        if shell == "bash":
            command = [tool, "--noprofile", "--norc", "-n", str(script)]
        else:
            # This fixed parser program parses the file without invoking its script block.
            parser = (
                "$tokens=$null;$errors=$null;"
                "[System.Management.Automation.Language.Parser]::ParseFile("
                "$args[0],[ref]$tokens,[ref]$errors)|Out-Null;"
                "if($errors.Count){$errors|Out-String|Write-Error;exit 1}"
            )
            parser_file = scratch / "parse.ps1"
            parser_file.write_text(parser, encoding="utf-8")
            command = [
                tool,
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(parser_file),
                str(script),
            ]
        subprocess.run(command, check=True, cwd=scratch, env=clean_environment(scratch), timeout=20)


def safe_fixture(root: Path, shell: str) -> str:
    extension = "sh" if shell == "bash" else "ps1"
    fixture = (root / f"tests/fixtures/documentation/recovery-config.{extension}").read_text(
        encoding="utf-8"
    )
    blocks = shell_blocks(
        (root / "docs/guides/core-operations.md").read_text(encoding="utf-8"), shell
    )
    # Match the complete reviewed prefix, with its exact next command boundary.
    boundary = "./autodj --config " if shell == "bash" else ".\\autodj.cmd --config "
    if not fixture or sum(block.startswith(fixture + boundary) for block in blocks) != 1:
        raise ValueError(
            f"{shell} recovery configuration fragment drifted; review fixture and docs"
        )
    return fixture


def run_safe(root: Path, shell: str, tool: str) -> None:
    fixture = safe_fixture(root, shell)
    with tempfile.TemporaryDirectory(prefix="autodj-doc-safe-") as directory:
        scratch = Path(directory)
        production = scratch / "production"
        production.mkdir()
        sentinel = production / "keep"
        sentinel.write_bytes(b"unchanged")
        script = scratch / ("fixture.sh" if shell == "bash" else "fixture.ps1")
        script.write_text(fixture, encoding="utf-8")
        command = (
            [tool, "--noprofile", "--norc", "-eu", str(script)]
            if shell == "bash"
            else [tool, "-NoProfile", "-NonInteractive", "-File", str(script)]
        )
        subprocess.run(command, check=True, cwd=scratch, env=clean_environment(scratch), timeout=20)
        configs = list(scratch.glob("autodj-recovery*/recovery.toml"))
        if len(configs) != 1:
            raise ValueError("Safe fixture must create exactly one recovery configuration")
        config = configs[0]
        values = tomllib.loads(config.read_text(encoding="utf-8"))
        expected = {
            "index": {
                key: (config.parent / name).resolve()
                for key, name in (("index_dir", "index"), ("model_dir", "models"))
            }
        }
        if (
            set(values) != {"index"}
            or not isinstance(values["index"], dict)
            or set(values["index"]) != set(expected["index"])
            or any(
                not isinstance(value, str)
                or not Path(value).is_absolute()
                or Path(value).resolve() != expected["index"][key]
                for key, value in values["index"].items()
            )
        ):
            raise ValueError(
                "Recovery configuration must point only to isolated scratch paths; "
                f"actual: {values!r}; expected: {expected!r}"
            )
        expected_files = {script, sentinel, config}
        if {
            path
            for path in scratch.rglob("*")
            if path.is_file() and not path.is_relative_to(scratch / "shell-home")
        } != expected_files:
            raise ValueError("Safe fixture created unexpected files")
        if (
            set(config.parent.iterdir()) != {config}
            or set(production.iterdir()) != {sentinel}
            or sentinel.read_bytes() != b"unchanged"
        ):
            raise ValueError("Safe fixture changed state beyond its configuration")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shell", required=True, choices=("bash", "powershell"))
    parser.add_argument(
        "--run-safe", action="store_true", help="Also run the reviewed config-only fixture"
    )
    args = parser.parse_args()
    try:
        tool = shell_tool(args.shell)
        count = 0
        for page in PAGES:
            document = ROOT / f"docs/guides/{page}.md"
            blocks = shell_blocks(document.read_text(encoding="utf-8"), args.shell)
            for block in blocks:
                check_syntax(block, args.shell, tool)
                count += 1
        if not count:
            raise ValueError(f"No complete {args.shell} examples found in bounded pages")
        if args.run_safe:
            run_safe(ROOT, args.shell, tool)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Documentation example check failed: {error}\n")
    print(f"Parsed {count} complete {args.shell} blocks; safe config execution: {args.run_safe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
