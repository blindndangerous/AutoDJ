"""Resolve the AutoDJ version from source or installed metadata."""

from __future__ import annotations

import hashlib
import importlib.metadata
import tomllib
from functools import cache
from pathlib import Path

#: Files a complete frontend bundle must contain; shared by doctor and server.
REQUIRED_BUILT_ASSETS = (
    "index.html",
    "app.js",
    "app.css",
    "bitcrusher-worklet.js",
    "stutter-worklet.js",
    "freeze-worklet.js",
    "glitch-worklet.js",
)


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
    """Read and validate the project version from a pyproject file."""
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


def web_source_hash(package_dir: Path) -> str | None:
    """Hash the web bundle's build inputs, as ``vite.config.js`` stamps them.

    The inputs are every file under ``src/autodj/static`` plus
    ``vite.config.js``.  Each contributes one line, ``<path>\0<sha256>\n``,
    in path order, where the path is POSIX-style from the checkout root
    and the digest is of the file with CRLF line endings turned into LF
    (so a Windows checkout and a Linux one agree).  ``webSourceHash`` in
    ``vite.config.js`` must compute exactly the same thing.

    Args:
        package_dir: The ``autodj`` package directory.

    Returns:
        The SHA-256 hex digest, or ``None`` when *package_dir* is not in a
        source checkout (an installed wheel ships the built bundle but not
        ``vite.config.js``), so there is nothing to compare against.
    """
    root = package_dir.parent.parent
    config = root / "vite.config.js"
    static = package_dir / "static"
    if package_dir != root / "src" / "autodj" or not config.is_file() or not static.is_dir():
        return None
    files = [config, *(path for path in static.rglob("*") if path.is_file())]
    digest = hashlib.sha256()
    for relative, path in sorted((path.relative_to(root).as_posix(), path) for path in files):
        content = path.read_bytes().replace(b"\r\n", b"\n")
        digest.update(f"{relative}\0{hashlib.sha256(content).hexdigest()}\n".encode())
    return digest.hexdigest()


def stale_bundle_reason(stamp: object, package_dir: Path) -> str | None:
    """Say why a built bundle's stamp does not match the web sources beside it.

    Args:
        stamp: The parsed ``build-info.json``.
        package_dir: The ``autodj`` package directory holding the bundle.

    Returns:
        A sentence naming the problem, or ``None`` when the stamp carries a
        source hash that matches (or there are no sources to compare).
    """
    recorded = stamp.get("source_hash") if isinstance(stamp, dict) else None
    if not isinstance(recorded, str) or not recorded:
        return "build-info.json has no source_hash"
    current = web_source_hash(package_dir)
    if current is not None and current != recorded:
        return "the web sources changed since the bundle was built"
    return None
