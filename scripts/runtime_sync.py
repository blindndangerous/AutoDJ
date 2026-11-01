"""Synchronize a stale setup-managed runtime before the launcher serves."""

from __future__ import annotations

import platform
import runpy
import subprocess
import sys
from pathlib import Path


def synchronize(root: Path, environment: Path, backend: str) -> None:
    """Synchronize stale managed packages while AutoDJ is idle.

    The caller owns the checkout update lock. External environments are never
    modified, and the marker is published only after the install succeeds.
    """
    root = Path(root).resolve()
    environment = Path(environment).resolve()

    import json

    from autodj.runtime_sync import mark_synced, needs_sync

    state_path = root / ".uv" / "setup.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read saved runtime selection at {state_path}: {exc}") from exc
    if not isinstance(state, dict):
        raise ValueError(f"Invalid saved runtime selection at {state_path}.")
    saved_environment = state.get("environment")
    if not isinstance(saved_environment, str) or not saved_environment.strip():
        raise ValueError(f"Invalid saved environment path in {state_path}.")
    saved_path = Path(saved_environment)
    if not saved_path.is_absolute():
        saved_path = root / saved_path
    if saved_path.resolve() != environment:
        raise ValueError("Saved runtime environment changed before package synchronization.")
    if state.get("external") is True:
        return
    if state.get("backend") != backend:
        raise ValueError("Saved runtime backend changed before package synchronization.")
    if not needs_sync(root, environment, backend):
        return

    scripts = str(root / "scripts")
    sys.path.insert(0, scripts)
    try:
        import setup_support
        from setup_activity import ensure_idle
        from setup_hardware import detect_hardware

        setup = runpy.run_path(str(root / "scripts" / "setup.py"), run_name="_autodj_runtime_sync")
    finally:
        if sys.path and sys.path[0] == scripts:
            sys.path.pop(0)

    port = setup["setup_values"](root)["port"]
    ensure_idle(root, environment, port, launcher=True)
    log = root / ".uv" / "runtime-sync.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    print("Updating AutoDJ's packages...", flush=True)
    system = platform.system() or "Unknown"
    machine = platform.machine() or "unknown"
    amd_arch = None
    if backend == "amd":
        try:
            stamp = setup_support.read_json(root / ".uv" / "runtime-sync.json")
        except ValueError:
            stamp = {}
        saved_arch = stamp.get("amd_arch")
        amd_arch = saved_arch if isinstance(saved_arch, str) and saved_arch else None
        if amd_arch is None:
            amd_arch = detect_hardware().amd_arch
    try:
        # A failed install can leave packages partially changed. Invalidate the
        # old record so rolling the code back also retries installation.
        (root / ".uv" / "runtime-sync.json").unlink(missing_ok=True)
        setup_support.install_python(
            root,
            environment,
            backend,
            system,
            machine,
            amd_arch,
            log=log,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        with log.open("a", encoding="utf-8") as output:
            output.write(f"\n{exc}\n")
        raise RuntimeError(
            "Package update failed. Run AutoDJ again to retry. See details in .uv/runtime-sync.log."
        ) from exc
    mark_synced(
        root,
        environment,
        backend,
        amd_arch,
    )
