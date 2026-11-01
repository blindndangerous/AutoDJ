"""Cheap dependency fingerprints for a setup-managed runtime."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from autodj.fsutil import atomic_write

STAMP = Path(".uv/runtime-sync.json")
INPUTS = (
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "scripts/setup-profiles.json",
    "scripts/setup_support.py",
    "scripts/runtime_sync.py",
)


def runtime_fingerprint(
    root: Path, environment: Path, backend: str, amd_arch: str | None = None
) -> str:
    """Identify the locked installer inputs and the environment they apply to."""
    digest = hashlib.sha256()
    digest.update(json.dumps([str(environment.resolve()), backend, amd_arch]).encode())
    for name in INPUTS:
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def needs_sync(root: Path, environment: Path, backend: str) -> bool:
    """An external runtime is never automatically claimed by the launcher."""
    state = json.loads((root / ".uv/setup.json").read_text(encoding="utf-8"))
    if state.get("external") is True:
        return False
    try:
        stamp = json.loads((root / STAMP).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return True
    if not isinstance(stamp, dict):
        return True
    return stamp.get("fingerprint") != runtime_fingerprint(
        root, environment, backend, stamp.get("amd_arch")
    )


def mark_synced(root: Path, environment: Path, backend: str, amd_arch: str | None = None) -> None:
    """Publish a successful install only after the installer checks its packages."""
    atomic_write(
        root / STAMP,
        json.dumps(
            {
                "fingerprint": runtime_fingerprint(root, environment, backend, amd_arch),
                "amd_arch": amd_arch,
            }
        )
        + "\n",
    )
