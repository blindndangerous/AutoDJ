"""Standard-library setup operations shared by the wizard and saved launcher."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(".uv/setup.json")


def python_path(environment: Path) -> Path:
    """Locate an environment's interpreter on the current operating system."""
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def read_json(path: Path) -> dict:
    """Read a settings object, reporting corrupt files instead of resetting them."""
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def write_json(path: Path, value: dict) -> None:
    """Publish settings atomically after a successful operation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".setup-", suffix=".json")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def runtime_environment(environment: Path, backend: str) -> dict[str, str]:
    """Use the same device policy and cache locations for setup and later runs."""
    result = os.environ.copy()
    result["AUTODJ_RUNTIME"] = "current"
    if backend == "cpu":
        result["AUTODJ_GPU"] = "0"
    if backend == "amd":
        result["MIOPEN_USER_DB_PATH"] = str(environment / "miopen/db")
        result["MIOPEN_CUSTOM_CACHE_DIR"] = str(environment / "miopen/cache")
    return result


def prepare_caches(environment: Path, backend: str) -> None:
    """Create AMD's writable cache directories only when executing setup or run."""
    if backend == "amd":
        for directory in ("miopen/db", "miopen/cache"):
            (environment / directory).mkdir(parents=True, exist_ok=True)


def run(command: list[str], root: Path, env: dict[str, str] | None = None) -> None:
    """Run a visible command without a shell and stop on failure."""
    executable = shutil.which(command[0])
    if executable is None:
        raise ValueError(f"Command not found: {command[0]}")
    subprocess.run([executable, *command[1:]], cwd=root, env=env, check=True)


def fingerprint(root: Path, paths: tuple[str, ...], extra: str = "") -> str:
    """Identify installation inputs so reruns can skip completed installations."""
    digest = hashlib.sha256(extra.encode())
    for name in paths:
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def profiles(root: Path) -> dict:
    """Read the single installation recipe catalog."""
    return read_json(root / "scripts/setup-profiles.json")


def _platform_recipe(
    root: Path, backend: str, system: str, machine: str, amd_arch: str | None
) -> dict | None:
    """Select the exact supported wheel variant for this host."""
    recipe = profiles(root)[backend]
    platform_recipe = recipe.get("platforms", {}).get(system)
    if backend == "amd" and isinstance(platform_recipe, dict):
        packages = platform_recipe.get("architectures", {}).get(amd_arch)
        platform_recipe = (
            {"index": platform_recipe.get("index"), "packages": packages} if packages else None
        )
    if backend == "cpu" and machine.lower() not in {"amd64", "x86_64"}:
        return None
    return platform_recipe


def install_commands(
    root: Path,
    environment: Path,
    backend: str,
    system: str,
    machine: str = "x86_64",
    amd_arch: str | None = None,
) -> list[list[str]]:
    """Build a dependency installation plan without executing it."""
    commands: list[list[str]] = []
    if not python_path(environment).is_file():
        commands.append(["uv", "venv", "--python", "3.14", "--seed", str(environment)])
    platform_recipe = _platform_recipe(root, backend, system, machine, amd_arch)
    if backend != "cpu" and platform_recipe is None:
        raise ValueError(f"No automatic {backend} installation recipe for {system}.")
    if platform_recipe and platform_recipe.get("packages"):
        constraints = root / ".uv" / f"setup-{backend}-constraints.txt"
        commands.append(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python_path(environment)),
                "--index-url",
                platform_recipe["index"],
                *platform_recipe["packages"],
            ]
        )
        commands.append(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python_path(environment)),
                "--constraint",
                str(constraints),
                "-e",
                ".[all]",
            ]
        )
    else:
        commands.append(
            ["uv", "pip", "install", "--python", str(python_path(environment)), "-e", ".[all]"]
        )
    return commands


def install_python(
    root: Path,
    environment: Path,
    backend: str,
    system: str,
    machine: str = "x86_64",
    amd_arch: str | None = None,
) -> None:
    """Install into a dedicated environment, leaving the project's .venv alone."""
    stamp = environment / ".autodj-setup.json"
    signature = fingerprint(
        root,
        ("pyproject.toml", "scripts/setup-profiles.json"),
        f"{backend}:{system}:{machine}:{amd_arch or ''}",
    )
    if python_path(environment).exists() and read_json(stamp).get("dependencies") == signature:
        print("Python dependencies are already set up.")
        return
    env = os.environ.copy()
    env["AUTODJ_RUNTIME"] = "current"
    env["UV_PROJECT_ENVIRONMENT"] = str(environment)
    env.pop("VIRTUAL_ENV", None)
    platform_recipe = _platform_recipe(root, backend, system, machine, amd_arch)
    if platform_recipe and platform_recipe.get("packages"):
        constraints = root / ".uv" / f"setup-{backend}-constraints.txt"
        constraints.parent.mkdir(parents=True, exist_ok=True)
        pins = []
        for package in platform_recipe["packages"]:
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)(?:\[[^]]+\])?(.*)", package)
            if not match:
                raise ValueError(f"Invalid package pin in setup profile: {package}")
            pins.append(match.group(1) + match.group(2))
        constraints.write_text("\n".join(pins) + "\n", encoding="utf-8")
    for command in install_commands(root, environment, backend, system, machine, amd_arch):
        run(command, root, env)
    run(["uv", "pip", "check", "--python", str(python_path(environment))], root, env)
    write_json(stamp, {"dependencies": signature})


def create_configuration(root: Path, music: Path, port: int) -> None:
    """Create a minimal local configuration; never replace an existing file."""
    if not music.is_dir():
        raise ValueError(f"Music folder does not exist: {music}")
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    # JSON string escapes are valid TOML basic-string escapes for these values.
    quoted_music = json.dumps(music.as_posix(), ensure_ascii=False)
    content = (
        "# Created by AutoDJ setup. Edit this file to change your settings.\n"
        f'[library]\nmusic_dir = {quoted_music}\nbeets_db = ""\n\n'
        '[server]\nhost = "127.0.0.1"\n'
        f'port = {port}\nallowed_hosts = ["127.0.0.1", "localhost"]\n'
        f'allowed_origins = ["http://127.0.0.1:{port}", "http://localhost:{port}"]\n'
    )
    tomllib.loads(content)
    with (root / "config.toml").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def initialize_paths(root: Path, python: Path, env: dict[str, str]) -> None:
    """Check audio imports, then let AutoDJ resolve and validate its own paths."""
    code = (
        "import muq, torchaudio, sounddevice, soundfile; "
        "from autodj.config import load_config; c=load_config(); "
        "assert c.library.music_dir.is_dir(), f'Music folder does not exist: {c.library.music_dir}'; "
        "c.index.index_dir.mkdir(parents=True, exist_ok=True); "
        "c.index.model_dir.mkdir(parents=True, exist_ok=True)"
    )
    run([str(python), "-c", code], root, env)


def saved_selection(root: Path) -> tuple[Path, str]:
    """Load the verified environment selected by setup."""
    state = read_json(root / STATE)
    if state.get("backend") not in {"cpu", "amd", "nvidia"} or not isinstance(
        state.get("environment"), str
    ):
        raise ValueError("Run scripts/setup.py first to select an AutoDJ environment.")
    environment = (root / state["environment"]).resolve()
    if not python_path(environment).is_file():
        raise ValueError(f"Environment is missing: {environment}. Run setup again.")
    return environment, state["backend"]
