"""Guided local installation. Bootstrap with uv run --no-project --python 3.14."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tomllib
from pathlib import Path

from setup_hardware import detect_hardware, probe_runtime
from setup_support import (
    ROOT,
    STATE,
    create_configuration,
    initialize_paths,
    install_commands,
    install_python,
    prepare_caches,
    profiles,
    python_path,
    read_json,
    run,
    runtime_environment,
    write_json,
)


def ask(prompt: str, default: str) -> str:
    """Ask a plain-text question with a visible default."""
    return input(f"{prompt} [{default}]: ").strip() or default


def confirm(prompt: str) -> bool:
    """Optional expensive actions always default to no."""
    return ask(prompt + " (y/n)", "n").lower() in {"y", "yes"}


def prerequisites() -> list[str]:
    """Report missing host tools without installing system software."""
    issues = []
    for executable, hint in (
        ("uv", "Install uv: https://docs.astral.sh/uv/getting-started/installation/"),
    ):
        if shutil.which(executable) is None:
            issues.append(f"Missing {executable}. {hint}")
    return issues


def port_number(value: str) -> int:
    """Validate user input before any installation starts."""
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Port must be a number.") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("Port must be between 1 and 65535.")
    return port


def port_available(port: int) -> bool:
    """Check the loopback port without changing an existing server."""
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def doctor_findings(stdout: str) -> list[str] | None:
    """Render concise doctor warnings and failures, or None for malformed JSON."""
    try:
        report = json.loads(stdout)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(report, dict) or not isinstance(report.get("checks"), list):
        return None
    findings = []
    for check in report["checks"]:
        if not isinstance(check, dict):
            continue
        status = check.get("status")
        name = check.get("name")
        summary = check.get("summary")
        if (
            status not in {"warn", "fail"}
            or not isinstance(name, str)
            or not isinstance(summary, str)
        ):
            continue
        findings.append(f"{status.upper()} {name}: {summary[:300]}")
    return findings


def parser() -> argparse.ArgumentParser:
    """Describe the first-run flow and unattended options."""
    result = argparse.ArgumentParser(description="Set up AutoDJ locally, outside Docker.")
    result.add_argument("--backend", choices=("auto", "cpu", "nvidia", "amd"), default="auto")
    result.add_argument("--music-dir", type=Path, help="Music folder for a new configuration.")
    result.add_argument(
        "--port", type=port_number, help="Local web port for a new configuration (8080)."
    )
    result.add_argument(
        "--environment",
        type=Path,
        help="Use an existing environment without changing its Python packages.",
    )
    result.add_argument(
        "--yes",
        action="store_true",
        help="Accept the setup plan; optional import/server actions still require their flags.",
    )
    result.add_argument(
        "--dry-run", action="store_true", help="Show the plan without installing or writing files."
    )
    result.add_argument(
        "--test-import",
        action="store_true",
        help="Import up to three tracks; may download model weights.",
    )
    result.add_argument(
        "--serve",
        action="store_true",
        help="Start the server in this terminal after successful setup.",
    )
    return result


def configure(args: argparse.Namespace, root: Path, interactive: bool) -> tuple[Path | None, int]:
    """Collect new settings while preserving existing base and local configuration."""
    if (root / "config.toml").exists():
        for name in ("config.toml", "config.local.toml"):
            if (root / name).exists():
                with (root / name).open("rb") as stream:
                    tomllib.load(stream)
        if args.music_dir is not None or args.port is not None:
            raise ValueError(
                "Configuration already exists. Edit config.toml or config.local.toml to change its music folder or port."
            )
        print("Keeping existing config.toml and any config.local.toml overrides.")
        return None, 8080
    if (root / "config.local.toml").exists():
        raise ValueError(
            "config.local.toml exists without config.toml. Create the base configuration before running setup."
        )
    music = args.music_dir
    if music is None and interactive:
        music = Path(ask("Path to your music folder", str(root / "music")))
    if music is None:
        if not args.dry_run:
            raise ValueError(
                "Supply --music-dir for a new installation when running without prompts."
            )
        music = root / "music"
    music = (root / music.expanduser()).resolve()
    if not music.is_dir() and not args.dry_run:
        raise ValueError(
            f"Music folder does not exist: {music}. Create it or choose another folder."
        )
    port = args.port or 8080
    if args.port is None and interactive:
        port = port_number(ask("Local web port", str(port)))
    if not port_available(port):
        raise ValueError(f"Port {port} is already in use. Choose another with --port.")
    return music, port


def execute(args: argparse.Namespace, root: Path = ROOT) -> int:
    """Collect a plan, then install and verify it before saving the selection."""
    interactive = sys.stdin.isatty() and not args.yes and not args.dry_run
    hardware = detect_hardware()
    print(f"System: {hardware.system} ({hardware.machine})")
    print("Graphics: " + (", ".join(hardware.gpus) or "not identified"))
    print(hardware.reason)
    previous = read_json(root / STATE)
    if "environment" in previous and not isinstance(previous["environment"], str):
        raise ValueError(
            "Invalid saved environment path in .uv/setup.json. Fix or remove that selection and rerun setup."
        )
    backend = args.backend
    if backend == "auto":
        backend = previous.get("backend", hardware.recommendation)
    if not isinstance(backend, str) or backend not in {"cpu", "nvidia", "amd"}:
        raise ValueError("Choose cpu, nvidia, or amd.")
    reuse = args.environment is not None or (
        previous.get("backend") == backend and isinstance(previous.get("environment"), str)
    )
    external = args.environment is not None or (
        previous.get("external") is True and previous.get("backend") == backend
    )
    if backend != "cpu" and os.environ.get("AUTODJ_GPU") == "0":
        raise ValueError(
            "AUTODJ_GPU=0 disables GPU processing. Remove that environment override or choose --backend cpu."
        )
    if not reuse and backend != "cpu" and hardware.machine.lower() not in {"amd64", "x86_64"}:
        raise ValueError(
            "Automatic GPU installation currently supports x86-64 systems only. Use an existing compatible --environment or choose CPU."
        )
    if backend == "amd" and not reuse and getattr(hardware, "amd_arch", None) is None:
        raise ValueError(
            f"Automatic AMD installation is not available for this detected hardware. {hardware.reason} Use a compatible --environment or choose CPU."
        )
    selected = args.environment or Path(previous.get("environment", f".uv/setup-{backend}"))
    if args.environment is None and previous.get("backend") != backend:
        selected = Path(f".uv/setup-{backend}")
    environment = (root / selected).resolve()
    python = python_path(environment)
    if reuse and not python.is_file():
        raise ValueError(f"No Python interpreter in {environment}.")
    music, port = configure(args, root, interactive)
    issues = prerequisites()
    print(f"\nProcessing: {backend}\nEnvironment: {environment}")
    if music is not None:
        print(f"Music: {music}\nWeb address: http://localhost:{port}")
    notice = profiles(root)[backend].get("notice")
    if notice:
        print(notice)
    if reuse:
        print("Use existing Python packages; check that AutoDJ and the selected backend work.")
    else:
        for command in install_commands(
            root,
            environment,
            backend,
            hardware.system,
            hardware.machine,
            getattr(hardware, "amd_arch", None),
        ):
            print("Install: " + subprocess.list2cmdline(command))
        print("Installing PyTorch and audio dependencies can download several GB.")
    print("Check the runtime and run AutoDJ doctor.")
    print(
        "Close any running AutoDJ server or import jobs before applying setup so doctor can check the databases."
    )
    print("Model downloads and track imports only run when you request a test import.")
    if issues:
        print("\nPrerequisites to install:\n" + "\n".join(issues))
    if shutil.which("ffmpeg") is None:
        print(
            "FFmpeg was not found; MP3 indexing, AAC/M4A decoding, and streaming need it on PATH."
        )
    if args.dry_run:
        return 0
    if issues:
        return 1
    if not args.yes and (not interactive or not confirm("Apply this setup plan?")):
        print("Setup cancelled. Use --yes to accept the plan without prompts.")
        return 1
    if not reuse:
        install_python(
            root,
            environment,
            backend,
            hardware.system,
            hardware.machine,
            getattr(hardware, "amd_arch", None),
        )
    prepare_caches(environment, backend)
    env = runtime_environment(environment, backend)
    runtime = probe_runtime(python, env=env)
    device = runtime.get("device_name") or "CPU"
    print(f"Runtime: {device} (PyTorch {runtime.get('torch_version') or 'unavailable'})")
    print(str(runtime.get("reason", "")))
    expected = {"nvidia": "cuda", "amd": "rocm"}.get(backend)
    if runtime.get("torch_version") is None or runtime.get("error"):
        if expected:
            raise ValueError(
                "The GPU runtime check failed. Fix the reported problem or rerun setup with --backend cpu."
            )
        raise ValueError(
            "The Python runtime check failed. Fix the reported problem and rerun setup."
        )
    if expected and (runtime.get("backend") != expected or not runtime.get("gpu_available")):
        raise ValueError(
            "The selected GPU could not run a calculation. Check the driver and package compatibility, or rerun with --backend cpu."
        )
    if music is not None:
        create_configuration(root, music, port)
    initialize_paths(root, python, env)
    doctor = subprocess.run(
        [str(python), "-m", "autodj", "doctor", "--no-fix", "--json"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    findings = doctor_findings(doctor.stdout)
    if findings or doctor.returncode:
        print("Doctor reported an issue; setup will keep the verified runtime selection.")
        if findings is not None:
            for finding in findings:
                print(f"  {finding}")
            print("Run `autodj doctor` for details and repair guidance.")
        else:
            if doctor.stdout:
                print(doctor.stdout[:3000].rstrip())
                if len(doctor.stdout) > 3000:
                    print("Doctor output truncated.")
            if doctor.stderr:
                print(doctor.stderr[:3000].rstrip(), file=sys.stderr)
                if len(doctor.stderr) > 3000:
                    print("Doctor error output truncated.", file=sys.stderr)
    write_json(
        root / STATE, {"environment": str(environment), "backend": backend, "external": external}
    )
    print("\nSetup verified. Start AutoDJ with:")
    print(".\\autodj.cmd serve" if os.name == "nt" else "./autodj serve")
    test_import = args.test_import or (
        interactive
        and confirm("Test importing up to three tracks? This may download a large audio model.")
    )
    if test_import:
        run(
            [str(python), "-m", "autodj", "index", "--limit", "3", "--no-enrich", "--no-analyse"],
            root,
            env,
        )
    if args.serve:
        run([str(python), "-m", "autodj", "serve"], root, env)
    return 0


def main() -> int:
    """Render actionable failures without an installation traceback."""
    args = parser().parse_args()
    try:
        return execute(args)
    except (OSError, ValueError, argparse.ArgumentTypeError, subprocess.TimeoutExpired) as exc:
        print(f"Setup: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(
            f"Setup stopped: command failed with exit code {exc.returncode}. Fix the error above and rerun setup.",
            file=sys.stderr,
        )
        return exc.returncode
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
