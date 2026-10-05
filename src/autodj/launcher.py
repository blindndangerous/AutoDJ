"""Dispatch AutoDJ through the saved local runtime selection."""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- explicit Python interpreter and argv, never a shell
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
STATE = Path(".uv/setup.json")
BACKENDS = {"amd", "nvidia", "cpu"}


def _python_path(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _selection(root: Path) -> tuple[Path, str] | None:
    """Return the selected environment, or None when no local selection applies."""
    # Installed wheels may be launched from arbitrary working directories. Only
    # a source checkout owns the setup state file.
    if not (root / "pyproject.toml").is_file():
        return None
    state_path = root / STATE
    if not state_path.exists():
        return None
    try:
        state: Any = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read saved runtime selection at {state_path}: {exc}") from exc
    if not isinstance(state, dict):
        raise ValueError(
            f"Invalid saved runtime selection at {state_path}: expected a JSON object."
        )
    environment_value = state.get("environment")
    backend = state.get("backend")
    if not isinstance(environment_value, str) or not environment_value.strip():
        raise ValueError(f"Invalid saved environment path in {state_path}. Run setup again.")
    if not isinstance(backend, str) or backend not in BACKENDS:
        raise ValueError(
            f"Invalid saved backend in {state_path}. Choose cpu, nvidia, or amd and run setup again."
        )
    environment_path = Path(environment_value)
    if not environment_path.is_absolute():
        environment_path = root / environment_path
    environment = environment_path.resolve()
    python = _python_path(environment)
    if not python.is_file():
        raise ValueError(f"Saved environment interpreter is missing: {python}. Run setup again.")
    return environment, backend


def _cli() -> int:
    from autodj.cli import cli

    result = cli()
    return result if isinstance(result, int) else 0


def run_setup(arguments: list[str]) -> int:
    """Bootstrap hardware-aware setup without importing application dependencies."""
    setup = ROOT / "scripts/setup.py"
    if not setup.is_file():
        print("Guided setup requires an AutoDJ source checkout.", file=sys.stderr)
        return 1
    environment = os.environ.copy()
    environment["AUTODJ_RUNTIME"] = "current"
    try:
        return subprocess.run(  # nosec B603 -- local setup script and argv, no shell
            [sys.executable, str(setup), *arguments], env=environment, check=False
        ).returncode
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"Could not start AutoDJ setup: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    """Run AutoDJ with the selected local runtime when one has been saved."""
    arguments = sys.argv[1:]
    if arguments[:1] == ["setup"]:
        return run_setup(arguments[1:])
    if os.environ.get("AUTODJ_RUNTIME", "").casefold() == "current":
        return _cli()
    try:
        selected = _selection(ROOT)
        if selected is None and (ROOT / "scripts/setup.py").is_file():
            bootstrap = os.environ.get("AUTODJ_BOOTSTRAP") == "1"
            help_requested = not arguments or arguments[0] in {"--help", "-h"}
            if not help_requested and sys.stdin.isatty():
                print(
                    "Welcome to AutoDJ. Let's detect your hardware and set up your music library."
                )
                status = run_setup([])
                if status:
                    return status
                selected = _selection(ROOT)
                if selected is None:
                    print(
                        "Setup did not select a runtime. Run `autodj setup` to retry.",
                        file=sys.stderr,
                    )
                    return 1
            elif bootstrap:
                print(
                    "AutoDJ: run `autodj setup` to detect your hardware and install its runtime.\n"
                    "Then use the same launcher for serve, doctor, index, or analyse.\n"
                    "For unattended setup, use `autodj setup --help` for the required options."
                )
                return 0 if help_requested else 1
        if selected is not None:
            environment, backend = selected
            child_environment = os.environ.copy()
            if backend == "cpu":
                child_environment["AUTODJ_GPU"] = "0"
            elif backend == "amd":
                for relative, variable in (
                    ("miopen/db", "MIOPEN_USER_DB_PATH"),
                    ("miopen/cache", "MIOPEN_CUSTOM_CACHE_DIR"),
                ):
                    cache = environment / relative
                    cache.mkdir(parents=True, exist_ok=True)
                    child_environment[variable] = str(cache)
            # Compare prefixes, since venv executables can be symlinks.
            if environment != Path(sys.prefix).resolve():
                completed = subprocess.run(  # nosec B603 -- user-selected runtime, argv only
                    [str(_python_path(environment)), "-m", "autodj", *sys.argv[1:]],
                    env=child_environment,
                    check=False,
                )
                return completed.returncode
            # Apply device/cache policy even when already in the selected runtime.
            for variable in ("AUTODJ_GPU", "MIOPEN_USER_DB_PATH", "MIOPEN_CUSTOM_CACHE_DIR"):
                if variable in child_environment:
                    os.environ[variable] = child_environment[variable]
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as exc:
        print(f"AutoDJ: {exc}", file=sys.stderr)
        return 1
    return _cli()
