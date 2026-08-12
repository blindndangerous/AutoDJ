from __future__ import annotations

import importlib.metadata
import tomllib
from functools import cache
from pathlib import Path


def _source_pyproject() -> Path | None:
    """Return this module's checkout pyproject, excluding installed layouts."""
    module = Path(__file__).resolve()
    try:
        root = module.parents[2]
    except IndexError:
        return None
    source_module = root / "src" / "autodj" / "version.py"
    if not source_module.is_file() or source_module.resolve() != module:
        return None
    return root / "pyproject.toml"


def _project_version(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            project = tomllib.load(fh)["project"]
        if not isinstance(project, dict) or project.get("name") != "autodj":
            raise ValueError("project.name must be 'autodj'")
        version = project.get("version")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("project.version must be a non-empty string")
        return version
    except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"Unable to read AutoDJ version from {path}: {exc}") from exc


@cache
def current_version() -> str:
    """Return source metadata in a checkout, otherwise installed metadata."""
    source_pyproject = _source_pyproject()
    if source_pyproject is not None:
        return _project_version(source_pyproject)
    try:
        version = importlib.metadata.version("autodj")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError(
            "AutoDJ version unavailable: package metadata is missing and this is not "
            "an AutoDJ source checkout"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"AutoDJ installed version metadata is invalid: {exc}") from exc
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError(
            "AutoDJ installed version metadata is invalid: expected a non-empty string"
        )
    return version
