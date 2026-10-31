"""Read-only runtime requirements shared by Doctor and server startup."""

from __future__ import annotations

import json
import re
import subprocess  # nosec B404 -- list2cmdline only; installation belongs to setup
import sys
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_REQUIREMENT = re.compile(r"([\w.-]+)(?:\[[\w,.-]+\])?\s*(>=[\d.]+)\s*(?:;\s*(.*))?")
_RELEASE = re.compile(r"v?(?:(\d+)!)?(\d+(?:\.\d+)*)(?:[.-]?post[.-]?\d+)?(?:\+[\w.-]+)?")


@dataclass(frozen=True)
class PackageProblem:
    name: str
    required: str
    installed: str | None


def source_checkout() -> bool:
    """Only a checkout containing the lock and setup owns automatic package repair."""
    return (ROOT / "uv.lock").is_file() and (ROOT / "scripts/setup.py").is_file()


def runtime_selection() -> tuple[str, bool] | None:
    """Use a saved profile only when it describes this interpreter's environment."""
    if not source_checkout():
        return None
    try:
        state = json.loads((ROOT / ".uv/setup.json").read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return None
        backend = state.get("backend")
        environment = state.get("environment")
        external = state.get("external", False)
        if (
            backend not in ("cpu", "nvidia", "amd", "no-model")
            or not isinstance(environment, str)
            or not environment.strip()
            or not isinstance(external, bool)
            or (ROOT / environment).resolve() != Path(sys.prefix).resolve()
        ):
            return None
        return backend, external
    except (OSError, ValueError):
        return None


def _requirements(*, include_extras: bool = False) -> dict[str, str]:
    """Read this checkout's lock metadata, or the installed wheel's requirements.

    AutoDJ declares numeric lower bounds only (CONTRIBUTING.md). Reject other
    syntax explicitly so a future metadata change cannot silently omit a package.
    Development groups and extras without an identified profile are excluded.
    """
    selection = runtime_selection() if include_extras else None
    extra = ("play" if selection[0] == "no-model" else "all") if selection else ""
    lock = ROOT / "uv.lock"
    if source_checkout():
        data = tomllib.loads(lock.read_text(encoding="utf-8"))
        project = next(p for p in data["package"] if p["name"] == "autodj")
        entries = project["metadata"]["requires-dist"]
    else:
        entries = []
        for requirement in metadata.requires("autodj") or []:
            match = _REQUIREMENT.fullmatch(requirement)
            if match is None:
                raise ValueError(f"Unsupported runtime requirement: {requirement}")
            name, specifier, marker = match.groups()
            entries.append({"name": name, "specifier": specifier, "marker": marker})
    requirements = {}
    for entry in entries:
        marker = entry.get("marker")
        if marker:
            match = re.fullmatch(r"extra\s*==\s*['\"]([\w-]+)['\"]", marker)
            if match is None:
                raise ValueError(f"Unsupported runtime requirement marker: {marker}")
            if match[1] != extra:
                continue
        specifier = entry.get("specifier", "")
        if not re.fullmatch(r">=\d+(?:\.\d+)*", specifier):
            raise ValueError(f"Unsupported runtime requirement: {entry['name']}{specifier}")
        name = re.sub(r"[-_.]+", "-", entry["name"]).lower()
        requirements[name] = specifier
    if not requirements:
        raise ValueError("AutoDJ runtime requirements are unavailable")
    return requirements


def missing_runtime_packages(*, include_extras: bool = False) -> list[PackageProblem]:
    """Compare core versions, optionally including warn-only installed-profile extras."""
    try:
        requirements = _requirements(include_extras=include_extras)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        StopIteration,
        metadata.PackageNotFoundError,
    ) as exc:
        raise ValueError(f"Cannot read AutoDJ runtime requirements: {exc}") from exc
    problems = []
    for name, required in sorted(requirements.items()):
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            installed = None
        match = _RELEASE.fullmatch(installed) if installed else None
        minimum = tuple(map(int, required[2:].split(".")))
        release = tuple(map(int, match[2].split("."))) if match else ()
        width = max(len(minimum), len(release))
        # Stable/post/local releases satisfy a lower bound; prereleases do not.
        satisfies = match is not None and (
            int(match[1] or "0") > 0
            or release + (0,) * (width - len(release)) >= minimum + (0,) * (width - len(minimum))
        )
        if not satisfies:
            problems.append(PackageProblem(name, required, installed))
    return problems


def require_runtime_packages() -> None:
    """Refuse startup before configuration mutation, players, or listeners start."""
    problems = missing_runtime_packages()
    if problems:
        manual = (
            " This is a wheel installation; package repair is manual-only. "
            "Doctor prints the setup command to run from a source checkout."
            if not source_checkout()
            else ""
        )
        raise RuntimeError(
            "Missing or too-old required Python packages: "
            + ", ".join(p.name for p in problems)
            + "."
            + manual
            + "\nRun `autodj doctor --fix` to install the missing packages."
        )


def package_repair() -> tuple[list[str], str, bool]:
    """Return setup arguments, ownership explanation, and whether execution is safe."""
    selection = runtime_selection()
    if selection is None:
        installation = (
            "This is a wheel installation; package repair is manual-only. "
            if not source_checkout()
            else ""
        )
        return (
            ["--environment", sys.prefix, "--manage-environment"],
            installation
            + "Doctor cannot determine this environment's saved profile and ownership. "
            "Run the command in an AutoDJ source checkout and choose the model profile. "
            "--manage-environment permanently gives setup ownership of its packages; "
            "synchronization may remove packages outside that profile.",
            False,
        )
    backend, external = selection
    args = ["--yes", "--model", "no" if backend == "no-model" else "yes"]
    if backend != "no-model":
        args += ["--backend", backend]
    note = ""
    if external:
        args.append("--manage-environment")
        note = (
            "This adopted environment currently owns its packages. "
            "--manage-environment permanently gives setup ownership of its packages; "
            "synchronization may remove packages outside the selected profile."
        )
    available = (ROOT / "scripts/setup.py").is_file()
    if not available:
        note += " Guided setup requires an AutoDJ source checkout; repair cannot run here."
    return args, note, available


def setup_command(arguments: list[str]) -> str:
    return "autodj setup " + subprocess.list2cmdline(arguments)
