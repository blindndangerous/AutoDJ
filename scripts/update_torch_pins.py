"""Point the PyTorch-index pins at the torch family that uv.lock records.

Run after an upgrade moves torch, torchvision or torchaudio in uv.lock:

    uv run --frozen --no-sync python scripts/update_torch_pins.py

It rewrites the hashes in container/torch-cpu.txt (manylinux_2_28 wheels,
x86_64 then aarch64) and the CPU and NVIDIA install specs in
scripts/setup-profiles.json, after checking that every wheel those files name
exists on download.pytorch.org. The wheels are for the Python minor that
.python-version picks, so after a Python move it also moves the container
hashes to the new cpXYZ wheels. AMD recipes follow AMD's
own index and stay a manual edit. Nothing is written when a wheel is missing.
"""

from __future__ import annotations

import functools
import json
import re
import sys
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = "https://download.pytorch.org/whl"
FAMILY = ("torch", "torchaudio", "torchvision")
#: The setup backends that install from PyTorch's index, and the platform tag
#: each recipe's system needs.
PROFILE_PLATFORMS = {"Windows": "win_amd64", "Linux": "manylinux_2_28_x86_64"}
CONTAINER_PLATFORMS = ("manylinux_2_28_x86_64", "manylinux_2_28_aarch64")


def python_tag(root: Path) -> str:
    """Return the CPython wheel tag for the minor .python-version picks, such as cp314."""
    version = (root / ".python-version").read_text(encoding="utf-8").strip()
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", version)
    if not match:
        raise SystemExit(f".python-version {version!r} is not X.Y")
    return f"cp{match[1]}{match[2]}"


def locked_versions(root: Path) -> dict[str, str]:
    """Return the torch family's versions from uv.lock."""
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    return {p["name"]: p["version"] for p in lock["package"] if p["name"] in FAMILY}


@functools.cache
def _index_page(variant: str, name: str) -> str:
    """Return the simple-index page that lists *name*'s wheels for one variant."""
    with urllib.request.urlopen(f"{INDEX}/{variant}/{name}/", timeout=60) as response:
        return response.read().decode("utf-8")


def wheel_hashes(variant: str, name: str, version: str, tag: str) -> dict[str, str]:
    """Return {platform tag: sha256} for the *tag* (such as cp314) wheels of *name*."""
    page = _index_page(variant, name)
    pattern = re.compile(
        rf"{re.escape(name)}-{re.escape(version)}(?:%2B|\+){re.escape(variant)}"
        rf"-{tag}-{tag}-([a-z0-9_]+)\.whl#sha256=([0-9a-f]{{64}})"
    )
    return dict(pattern.findall(page))


def container_requirements(versions: dict[str, str], tag: str) -> str:
    """Return the requirement lines for container/torch-cpu.txt."""
    lines = []
    for name in FAMILY:
        hashes = wheel_hashes("cpu", name, versions[name], tag)
        missing = [tag for tag in CONTAINER_PLATFORMS if tag not in hashes]
        if missing:
            raise SystemExit(f"{name}=={versions[name]}+cpu has no wheel for {missing}")
        lines.append(f"{name}=={versions[name]}+cpu \\")
        lines.extend(f"    --hash=sha256:{hashes[tag]} \\" for tag in CONTAINER_PLATFORMS)
        lines[-1] = lines[-1].removesuffix(" \\")
        lines.append("    # x86_64, then aarch64")
    return "\n".join(lines) + "\n"


def updated_profiles(text: str, versions: dict[str, str], tag: str) -> str:
    """Return setup-profiles.json with CPU and NVIDIA specs on the locked versions."""
    profiles = json.loads(text)
    for backend in ("cpu", "nvidia"):
        for system, recipe in profiles[backend]["platforms"].items():
            for spec in recipe["packages"]:
                name, _, pinned = spec.partition("==")
                variant = pinned.partition("+")[2]
                new = f"{name}=={versions[name]}+{variant}"
                platform = PROFILE_PLATFORMS[system]
                if platform not in wheel_hashes(variant, name, versions[name], tag):
                    raise SystemExit(f"{new} has no {tag} {platform} wheel on {INDEX}/{variant}/")
                # Replace in the text so the file's one-line arrays keep their layout.
                text = text.replace(f'"{spec}"', f'"{new}"')
    return text


def main(root: Path = ROOT) -> int:
    """Rewrite both pin files from uv.lock; return 0 on success."""
    versions = locked_versions(root)
    tag = python_tag(root)
    container = root / "container" / "torch-cpu.txt"
    header = container.read_text(encoding="utf-8").partition("\ntorch==")[0]
    header = re.sub(r"\bcp3\d+\b", tag, header)
    container_text = f"{header}\n{container_requirements(versions, tag)}"
    profiles = root / "scripts" / "setup-profiles.json"
    profiles_text = updated_profiles(profiles.read_text(encoding="utf-8"), versions, tag)
    container.write_bytes(container_text.encode("utf-8"))
    profiles.write_bytes(profiles_text.encode("utf-8"))
    print("Pinned " + ", ".join(f"{name} {versions[name]}" for name in FAMILY), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
