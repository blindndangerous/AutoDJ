"""Guided local installation. Bootstrap with uv run --no-project --python <.python-version>."""

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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from setup_activity import ensure_idle, updater_owner
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

from autodj.host_tools import ensure_ffmpeg
from autodj.terminal_text import format_text
from autodj.version import _project_version

#: Setup's read-only Doctor run gives up after this long.
DOCTOR_TIMEOUT_SECONDS = 600


def ask(prompt: str, default: str) -> str:
    """Ask a plain-text question with a visible default."""
    return input(f"{prompt} [{default}]: ").strip() or default


def confirm(prompt: str) -> bool:
    """Optional expensive actions always default to no."""
    return ask(prompt + " (y/n)", "n").lower() in {"y", "yes"}


def print_wrapped(
    message: str,
    *,
    initial_indent: str = "",
    subsequent_indent: str = "",
    file=None,
) -> None:
    """Print plain prose within the current terminal width."""
    print(
        format_text(
            message,
            initial_indent=initial_indent,
            subsequent_indent=subsequent_indent,
        ),
        file=file,
    )


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
        detail = check.get("detail")
        if isinstance(detail, str) and detail:
            findings.append(f"  {detail[:1000]}")
    return findings


def parser() -> argparse.ArgumentParser:
    """Describe the first-run flow and unattended options."""
    result = argparse.ArgumentParser(description="Set up AutoDJ locally, outside Docker.")
    result.add_argument("--backend", choices=("auto", "cpu", "nvidia", "amd"), default="auto")
    model = result.add_mutually_exclusive_group()
    model.add_argument(
        "--model",
        choices=("ask", "yes", "no"),
        default="ask",
        help="Enable and download MuQ Similarity analysis (yes/no); ask by default.",
    )
    model.add_argument(
        "--no-model",
        dest="model",
        action="store_const",
        const="no",
        help="Alias for --model no.",
    )
    result.add_argument(
        "--port", type=port_number, help="Local web port for a new configuration (8080)."
    )
    result.add_argument(
        "--environment",
        type=Path,
        help="Adopt an existing environment without changing its Python packages.",
    )
    result.add_argument(
        "--manage-environment",
        action="store_true",
        help="Let setup synchronize the selected existing environment's packages.",
    )
    result.add_argument(
        "--yes",
        action="store_true",
        help="Accept the setup plan without prompting; specify --model yes or --model no.",
    )
    result.add_argument(
        "--dry-run", action="store_true", help="Show the plan without installing or writing files."
    )
    result.add_argument(
        "--verbose", action="store_true", help="Show full package installation commands."
    )
    model.add_argument(
        "--test-import", dest="model", action="store_const", const="yes", help=argparse.SUPPRESS
    )
    result.add_argument(
        "--serve",
        action="store_true",
        help="Start the server in this terminal after successful setup.",
    )
    return result


def configure(args: argparse.Namespace, root: Path, interactive: bool) -> int:
    """Collect new settings while preserving existing base and local configuration."""
    if (root / "config.toml").exists():
        for name in ("config.toml", "config.local.toml"):
            if (root / name).exists():
                with (root / name).open("rb") as stream:
                    tomllib.load(stream)
        if args.port is not None:
            raise ValueError(
                "Configuration already exists. Edit config.toml or config.local.toml to change its port."
            )
        print("Keeping existing config.toml and any config.local.toml overrides.")
        return setup_values(root)["port"]
    if (root / "config.local.toml").exists():
        raise ValueError(
            "config.local.toml exists without config.toml. Create the base configuration before running setup."
        )
    port = args.port or 8080
    if args.port is None and interactive:
        port = port_number(ask("Local web port", str(port)))
    if not port_available(port):
        raise ValueError(f"Port {port} is already in use. Choose another with --port.")
    return port


def setup_values(root: Path) -> dict:
    """Read only bootstrap paths and port, before application packages exist."""
    sections: dict = {}
    for name in ("config.toml", "config.local.toml"):
        path = root / name
        if path.is_file():
            with path.open("rb") as stream:
                for section, values in tomllib.load(stream).items():
                    if section in {"server", "index", "model"} and isinstance(values, dict):
                        sections.setdefault(section, {}).update(values)
    server, index, model = (sections.get(name, {}) for name in ("server", "index", "model"))

    def folder(value: str) -> Path:
        return (root / Path(value).expanduser()).resolve()

    return {
        "port": port_number(str(os.environ.get("AUTODJ_PORT", server.get("port", 8080)))),
        "index_dir": folder(os.environ.get("AUTODJ_INDEX_DIR", index.get("index_dir", "index"))),
        "model_dir": folder(os.environ.get("AUTODJ_MODEL_DIR", index.get("model_dir", "models"))),
        "manual_path": folder(model["manual_path"]) if model.get("manual_path") else None,
    }


def choose_model(args: argparse.Namespace, interactive: bool) -> bool:
    """Ask once; unattended callers must explicitly choose whether to use MuQ."""
    if args.model != "ask":
        return args.model == "yes"
    if args.dry_run:
        print_wrapped("Preview only: choose --model yes or --model no when applying setup.")
        return True
    if not interactive:
        raise ValueError("Choose --model yes or --model no for unattended setup.")
    print_wrapped(
        "MuQ helps AutoDJ find songs that sound alike; Yes downloads the model (about 1.3 GB "
        "for the standard model, reusing existing files) and supporting software, while No "
        "keeps playback, clockwheels and beat/key/cue "
        "analysis without local Similarity analysis."
    )
    return confirm("Enable MuQ Similarity analysis?")


def execute(args: argparse.Namespace, root: Path = ROOT) -> int:
    """Collect a plan, then install and verify it before saving the selection."""
    interactive = sys.stdin.isatty() and not args.yes and not args.dry_run
    print_wrapped(f"Welcome to AutoDJ {_project_version(root / 'pyproject.toml')} setup.")
    print_wrapped("First, I'll check your computer and show you what needs to be installed.")
    hardware = detect_hardware()
    print_wrapped("Graphics: " + (", ".join(hardware.gpus) or "not identified"))
    if args.verbose or args.dry_run:
        print_wrapped(f"System: {hardware.system} ({hardware.machine})")
        print_wrapped(hardware.reason)
    previous = read_json(root / STATE)
    if "environment" in previous and not isinstance(previous["environment"], str):
        raise ValueError(
            "Invalid saved environment path in .uv/setup.json. Fix or remove that selection and rerun setup."
        )
    values = setup_values(root)
    # Updates refresh the saved runtime; they do not consent to a new model download.
    updater_pid = updater_owner(root) if args.yes and args.model == "ask" else None
    updating = updater_pid is not None
    if updating:
        if previous.get("backend") not in {"cpu", "nvidia", "amd", "no-model"}:
            raise ValueError("No verified runtime to update; run setup interactively first.")
        use_model = previous["backend"] != "no-model"
    else:
        use_model = choose_model(args, interactive)
    if not use_model and args.backend not in {"auto", "cpu"}:
        raise ValueError("--model no (--no-model) cannot be combined with GPU setup.")
    backend = args.backend if use_model else "no-model"
    if backend == "auto":
        saved_backend = previous.get("backend")
        backend = (
            saved_backend if saved_backend in {"cpu", "nvidia", "amd"} else hardware.recommendation
        )
    if not isinstance(backend, str) or backend not in {"cpu", "nvidia", "amd", "no-model"}:
        raise ValueError("Choose cpu, nvidia, or amd.")
    reuse = args.environment is not None or (
        previous.get("backend") == backend and isinstance(previous.get("environment"), str)
    )
    external = not args.manage_environment and (
        args.environment is not None
        or (previous.get("external") is True and previous.get("backend") == backend)
    )
    if backend not in {"cpu", "no-model"} and os.environ.get("AUTODJ_GPU") == "0":
        raise ValueError(
            "AUTODJ_GPU=0 disables GPU processing. Remove that environment override or choose --backend cpu."
        )
    if (
        not external
        and backend not in {"cpu", "no-model"}
        and hardware.machine.lower() not in {"amd64", "x86_64"}
    ):
        raise ValueError(
            "Automatic GPU installation currently supports x86-64 systems only. Use an existing compatible --environment or choose CPU."
        )
    if backend == "amd" and not external and getattr(hardware, "amd_arch", None) is None:
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
    port = configure(args, root, interactive)
    issues = prerequisites()
    plan = {
        "cpu": "Use CPU processing.",
        "nvidia": "Use NVIDIA graphics processing.",
        "amd": "Use AMD graphics processing.",
        "no-model": "Install playback without local model analysis.",
    }[backend]
    print("\nSetup plan:")
    print_wrapped(plan, initial_indent="  ")
    print("  Environment:")
    print("    " + str(environment))
    print(f"Web address: http://localhost:{port}")
    print_wrapped(f"Library storage: {values['index_dir']}")
    if use_model:
        print_wrapped(f"Model files: {values['manual_path'] or values['model_dir']}")
    notice = profiles(root)[backend].get("notice")
    if notice and (args.verbose or args.dry_run):
        print_wrapped(notice, initial_indent="  ")
    if external:
        print_wrapped(
            "Setup will verify this externally managed environment without changing its packages."
        )
    else:
        commands = install_commands(
            root,
            environment,
            backend,
            hardware.system,
            hardware.machine,
            getattr(hardware, "amd_arch", None),
        )
        if args.dry_run or args.verbose:
            for command in commands:
                print("Install: " + subprocess.list2cmdline(command))
        else:
            print_wrapped(
                "AutoDJ will install the runtime packages. Add --verbose to see the commands.",
                initial_indent="  ",
            )
        if backend != "no-model":
            print_wrapped("The download may be several GB.")
    print_wrapped(
        "Setup will verify the runtime and run AutoDJ doctor to check for problems.",
        initial_indent="  ",
    )
    print_wrapped(
        "Setup checks that AutoDJ is stopped before changing its software.",
        initial_indent="  ",
    )
    print_wrapped(
        "The model will be downloaded if needed and tested with a short generated sound."
        if use_model and not updating
        else "Existing model files and music will be kept.",
        initial_indent="  ",
    )
    print_wrapped(
        "Setup also checks FFmpeg; on Windows it can install it through WinGet.",
        initial_indent="  ",
    )
    if issues:
        print("\nPrerequisites to install:")
        for issue in issues:
            print_wrapped(issue, initial_indent="  ", subsequent_indent="    ")
    if args.dry_run:
        return 0
    if issues:
        return 1
    if not args.yes and (not interactive or not confirm("Apply this setup plan?")):
        print("Setup cancelled. Use --yes to accept the plan without prompts.")
        return 1
    ensure_idle(root, environment, port, updater_pid=updater_pid)
    try:
        ensure_ffmpeg(auto_install=args.yes)
    except (OSError, RuntimeError) as exc:
        print(f"FFmpeg installation failed: {exc}", file=sys.stderr)
    if not external:
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
    if backend == "no-model":
        print("Runtime: no-model (playback, streaming and basic library; no local model analysis)")
    else:
        runtime = probe_runtime(python, env=env)
        expected = {"nvidia": "cuda", "amd": "rocm"}.get(backend)
        gpu_failed = bool(expected) and (
            runtime.get("torch_version") is None
            or bool(runtime.get("error"))
            or runtime.get("backend") != expected
            or not runtime.get("gpu_available")
        )
        if gpu_failed:
            print(
                f"Runtime: selected {backend} GPU verification failed "
                f"(PyTorch {runtime.get('torch_version') or 'unavailable'})."
            )
        else:
            device = runtime.get("device_name") or "CPU"
            print(f"Runtime: {device} (PyTorch {runtime.get('torch_version') or 'unavailable'})")
        if gpu_failed:
            detail = str(runtime.get("error") or runtime.get("reason") or "")
            detail = detail.removesuffix("; using CPU.")
            if detail:
                print_wrapped(detail, initial_indent="  ")
        elif runtime.get("reason"):
            print_wrapped(str(runtime["reason"]), initial_indent="  ")
        if runtime.get("torch_version") is None or runtime.get("error"):
            if expected:
                raise ValueError(
                    "The GPU runtime check failed. Fix the reported problem or rerun setup with --backend cpu."
                )
            raise ValueError(
                "The Python runtime check failed. Fix the reported problem and rerun setup."
            )
        if gpu_failed:
            raise ValueError(
                "The selected GPU could not run a calculation. Check the driver and package compatibility, or rerun with --backend cpu."
            )
    if not (root / "config.toml").exists():
        create_configuration(root, port)
    initialize_paths(root, python, env)
    if use_model and not updating:
        print_wrapped(
            "Checking MuQ Similarity analysis; the first download and test may take a few minutes."
        )
        run([str(python), "-m", "autodj.setup_model"], root, env)
    # Doctor stays offline here: setup is not the place to ask about updates.
    doctor_env = {**env, "AUTODJ_UPDATE_CHECK": "0"}
    try:
        doctor = subprocess.run(
            [str(python), "-m", "autodj", "doctor", "--no-fix", "--json"],
            cwd=root,
            env=doctor_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=DOCTOR_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(
            f"Doctor did not finish within {DOCTOR_TIMEOUT_SECONDS // 60} minutes; setup will keep "
            "the verified runtime selection. Run `autodj doctor` to check."
        )
        doctor = None
    findings = doctor_findings(doctor.stdout) if doctor is not None else []
    if doctor is not None and (findings or doctor.returncode):
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
    print(
        "\nRuntime updated."
        if updating
        else (
            "\nReady with MuQ Similarity analysis."
            if use_model
            else "\nReady without local MuQ analysis."
        )
    )
    print("Start AutoDJ with:")
    print(".\\autodj.cmd serve" if os.name == "nt" else "./autodj serve")
    print("Manage music folders from the Library page in AutoDJ.")
    if args.serve:
        run([str(python), "-m", "autodj", "serve"], root, env)
    return 0


def main() -> int:
    """Render actionable failures without an installation traceback."""
    args = parser().parse_args()
    try:
        return execute(args)
    except (
        OSError,
        ValueError,
        RuntimeError,
        argparse.ArgumentTypeError,
        subprocess.TimeoutExpired,
    ) as exc:
        print_wrapped(f"Setup failed: {exc}", file=sys.stderr)
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
