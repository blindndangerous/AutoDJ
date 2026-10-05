"""Small, dependency-free probes used by the setup bootstrap."""

from __future__ import annotations

import json
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_HARDWARE_TIMEOUT_SECONDS = 5
_RUNTIME_TIMEOUT_SECONDS = 30
_WINDOWS_GPU_COMMAND = (
    "Get-CimInstance Win32_VideoController | "
    "Select-Object -ExpandProperty Name | ConvertTo-Json -Compress"
)
_PROBE_MARKER = "__AUTODJ_SETUP_RUNTIME__"
_AMD_MODEL_TARGETS = {
    "gfx1201": (
        "RX 9070 XT",
        "RX 9070 GRE",
        "RX 9070",
        "Radeon AI PRO R9700",
        "Radeon AI PRO R9600D",
    ),
    "gfx1200": ("RX 9060 XT LP", "RX 9060 XT", "RX 9060"),
    "gfx1100": ("RX 7900 XTX", "RX 7900 XT", "RX 7900 GRE"),
    "gfx1101": ("RX 7800 XT", "RX 7700 XT", "RX 7700", "Radeon PRO V710", "Radeon PRO W7700"),
    "gfx1102": ("RX 7600",),
    "gfx1151": ("Radeon 8065S", "Radeon 8060S", "Radeon 8050S", "Radeon 8040S"),
    "gfx1150": ("Radeon 890M", "Radeon 880M"),
    "gfx1152": ("Radeon 860M", "Radeon 820M"),
    "gfx1103": ("Radeon 780M", "Radeon 760M", "Radeon 740M"),
}
_AMD_MODEL_PATTERNS = {
    target: tuple(
        re.compile(
            r"(?<![A-Z0-9])"
            + re.escape(model).replace(r"\ ", r"\s*").replace("Radeon", r"Radeon(?:\s*\(TM\))?")
            + r"(?![A-Z0-9])",
            re.IGNORECASE,
        )
        for model in models
    )
    for target, models in _AMD_MODEL_TARGETS.items()
}
_LSPCI_DISPLAY_DEVICE = re.compile(
    r"(?:VGA compatible controller|3D controller|Display controller)\s*:\s*(.+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Hardware:
    """Host details and the setup recommendation supported by this bootstrap."""

    system: str
    machine: str
    gpus: tuple[str, ...]
    recommendation: str
    reason: str
    amd_arch: str | None = None


def _run_capture(
    command: list[str], timeout: int
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    """Run a fixed probe command and return a safe, concise failure reason."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"command timed out after {timeout} seconds"
    except FileNotFoundError:
        return None, "command is unavailable"
    except (OSError, subprocess.SubprocessError):
        return None, "command could not run"

    if result.returncode != 0:
        return None, f"command exited with status {result.returncode}"
    return result, None


def _parse_windows_names(output: str) -> tuple[str, ...]:
    """Parse PowerShell's scalar-or-array JSON output from the fixed query."""
    if not isinstance(output, str):
        raise ValueError("PowerShell returned malformed output")
    if not output.strip():
        return ()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError("PowerShell returned malformed JSON") from exc

    if payload is None:
        return ()
    values = payload if isinstance(payload, list) else [payload]
    if not all(value is None or isinstance(value, str) for value in values):
        raise ValueError("PowerShell returned an unexpected JSON value")
    return tuple(
        dict.fromkeys(value.strip() for value in values if isinstance(value, str) and value.strip())
    )


def _windows_gpus() -> tuple[tuple[str, ...], str]:
    result, error = _run_capture(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _WINDOWS_GPU_COMMAND,
        ],
        _HARDWARE_TIMEOUT_SECONDS,
    )
    if error:
        return (), f"Windows GPU detection unavailable ({error})."
    assert result is not None
    try:
        names = _parse_windows_names(result.stdout)
    except ValueError as exc:
        return (), f"Windows GPU detection unavailable ({exc})."
    if not names:
        return (), "Windows reported no video controllers."
    return names, ""


def _parse_lspci_gpus(output: str) -> tuple[str, ...]:
    if not isinstance(output, str):
        return ()
    names: list[str] = []
    for line in output.splitlines():
        match = _LSPCI_DISPLAY_DEVICE.search(line.strip())
        if match:
            name = match.group(1).strip()
            if name and name not in names:
                names.append(name)
    return tuple(names)


def _linux_gpus() -> tuple[tuple[str, ...], str]:
    nvidia_result, nvidia_error = _run_capture(
        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
        _HARDWARE_TIMEOUT_SECONDS,
    )
    if nvidia_result is not None:
        output = nvidia_result.stdout
        names = (
            tuple(dict.fromkeys(line.strip() for line in output.splitlines() if line.strip()))
            if isinstance(output, str)
            else ()
        )
        if names and not any(
            name.casefold().startswith(("nvidia-smi", "error:")) for name in names
        ):
            return names, "nvidia-smi"
        nvidia_error = "command returned malformed or empty GPU names"

    lspci_result, lspci_error = _run_capture(["lspci", "-nn"], _HARDWARE_TIMEOUT_SECONDS)
    if lspci_result is not None:
        if not isinstance(lspci_result.stdout, str):
            return (), "Linux GPU detection unavailable (lspci returned malformed output)."
        names = _parse_lspci_gpus(lspci_result.stdout)
        if names:
            return names, ""
        return (), "lspci reported no display-class GPUs."

    failures = "; ".join(
        detail
        for detail in (
            f"nvidia-smi {nvidia_error}" if nvidia_error else None,
            f"lspci {lspci_error}" if lspci_error else None,
        )
        if detail
    )
    return (), f"Linux GPU detection unavailable ({failures or 'probes failed'})."


def _amd_target(name: str) -> str | None:
    """Return a ROCm target only for an explicitly mapped Radeon model name."""
    if "radeon" not in name.casefold():
        return None
    matches = [
        target
        for target, patterns in _AMD_MODEL_PATTERNS.items()
        if any(pattern.search(name) for pattern in patterns)
    ]
    if re.search(r"(?<![A-Z0-9])RX\s*7600\s+XT(?![A-Z0-9])", name, re.IGNORECASE):
        return None
    return matches[0] if len(matches) == 1 else None


def _is_amd_name(name: str) -> bool:
    folded = name.casefold()
    return "amd" in folded or "radeon" in folded or "[amd/ati]" in folded


def _supports_automatic_gpu(system: str, machine: str) -> bool:
    """Automatic GPU recipes are published for Windows/Linux x86-64 only."""
    return system in {"Windows", "Linux"} and machine.casefold() in {"amd64", "x86_64"}


def detect_hardware() -> Hardware:
    """Identify supported GPU setup paths using only local OS utilities."""
    system = platform.system() or "Unknown"
    machine = platform.machine() or "unknown"

    if system == "Windows":
        gpus, detection_reason = _windows_gpus()
    elif system == "Linux":
        gpus, detection_reason = _linux_gpus()
    elif system == "Darwin":
        return Hardware(
            system,
            machine,
            (),
            "cpu",
            "Apple accelerator setup is not configured in this first pass; using CPU.",
        )
    else:
        return Hardware(
            system,
            machine,
            (),
            "cpu",
            f"GPU detection is not configured for {system}; using CPU.",
        )

    if gpus and not _supports_automatic_gpu(system, machine):
        names = ", ".join(gpus)
        return Hardware(
            system,
            machine,
            gpus,
            "cpu",
            f"Detected GPU(s) ({names}), but automatic GPU setup supports Windows/Linux x86-64 only; using CPU.",
        )

    nvidia_gpus = tuple(name for name in gpus if "nvidia" in name.casefold())
    if nvidia_gpus or (system == "Linux" and detection_reason == "nvidia-smi" and gpus):
        return Hardware(
            system,
            machine,
            gpus,
            "nvidia",
            "NVIDIA GPU detected; runtime usability will be checked separately.",
        )

    amd_names = tuple(name for name in gpus if _is_amd_name(name))
    amd_targets = tuple(_amd_target(name) for name in amd_names)
    if amd_names and len(set(amd_targets)) == 1 and None not in amd_targets:
        amd_arch = amd_targets[0]
        return Hardware(
            system,
            machine,
            gpus,
            "amd",
            f"AMD Radeon hardware ({', '.join(amd_names)}) mapped to {amd_arch}; AMD runtime usability will be checked separately.",
            amd_arch,
        )

    if amd_names and any(target is None for target in amd_targets):
        names = ", ".join(amd_names)
        return Hardware(
            system,
            machine,
            gpus,
            "cpu",
            f"Detected AMD GPU(s) ({names}) without a complete supported device mapping; using CPU.",
        )

    if amd_names and len(set(amd_targets)) > 1:
        names = ", ".join(amd_names)
        return Hardware(
            system,
            machine,
            gpus,
            "cpu",
            f"Detected AMD GPUs with different ROCm targets ({names}); using CPU.",
        )

    if not gpus:
        reason = detection_reason or "No GPU was reported by the hardware probe."
        return Hardware(system, machine, (), "cpu", f"{reason} Using CPU.")

    names = ", ".join(gpus)
    return Hardware(
        system,
        machine,
        gpus,
        "cpu",
        f"Detected GPU(s) but none match a configured accelerated setup ({names}); using CPU.",
    )


_PROBE_CODE = r"""import json
import os

marker = "__AUTODJ_SETUP_RUNTIME__"

def emit(backend, gpu_available, device_name, torch_version, reason, error=None):
    result = {
        "backend": backend,
        "gpu_available": gpu_available,
        "device_name": device_name,
        "torch_version": torch_version,
        "reason": reason,
        "error": error,
    }
    print(marker + " " + json.dumps(result, separators=(",", ":")))

def exception_text(exc):
    detail = str(exc)[:180]
    return type(exc).__name__ + (": " + detail if detail else "")

def cpu_probe(torch, torch_version, reason, error=None):
    try:
        sample = torch.tensor([1.0], device="cpu")
        value = (sample + sample).item()
        if value != 2.0:
            raise RuntimeError("CPU tensor result was incorrect")
    except Exception as exc:
        cpu_error = "CPU tensor operation failed (" + exception_text(exc) + ")"
        full_error = cpu_error if error is None else error + "; " + cpu_error
        emit("cpu", False, None, torch_version, reason + " " + cpu_error + "; using CPU.", full_error)
    else:
        emit("cpu", False, None, torch_version, reason, error)

try:
    import torch
except ModuleNotFoundError as exc:
    if exc.name == "torch":
        reason = "PyTorch is not installed in this Python environment; using CPU."
        error = "PyTorch is not installed in this Python environment."
    else:
        error = "PyTorch import failed (" + exception_text(exc) + ")"
        reason = error + "; using CPU."
    emit("cpu", False, None, None, reason, error)
except Exception as exc:
    error = "PyTorch import failed (" + exception_text(exc) + ")"
    emit("cpu", False, None, None, error + "; using CPU.", error)
else:
    torch_version = str(getattr(torch, "__version__", "unknown"))[:80]
    if os.environ.get("AUTODJ_GPU") == "0":
        cpu_probe(
            torch,
            torch_version,
            "AUTODJ_GPU=0 override selected CPU; CPU tensor operation succeeded.",
        )
    else:
        torch_runtime = getattr(torch, "version", None)
        hip_version = getattr(torch_runtime, "hip", None)
        cuda_version = getattr(torch_runtime, "cuda", None)
        backend = "rocm" if hip_version else "cuda" if cuda_version else "cpu"
        cuda_api = getattr(torch, "cuda", None)

        if cuda_api is None:
            error = None if backend == "cpu" else "PyTorch does not expose a CUDA/ROCm device API."
            reason = "PyTorch does not expose a CUDA/ROCm device API; CPU tensor operation succeeded."
            cpu_probe(torch, torch_version, reason, error)
        else:
            try:
                available = bool(cuda_api.is_available())
            except Exception as exc:
                runtime_name = "ROCm" if backend == "rocm" else "CUDA"
                error = runtime_name + " initialization failed (" + exception_text(exc) + ")"
                cpu_probe(torch, torch_version, error + "; falling back to CPU.", error)
            else:
                if not available:
                    if backend == "cpu":
                        reason = "PyTorch has no CUDA/ROCm runtime configured; CPU tensor operation succeeded."
                        error = None
                    else:
                        runtime_name = "ROCm" if backend == "rocm" else "CUDA"
                        error = runtime_name + " runtime is present, but no usable GPU/driver was reported."
                        reason = error + " Falling back to CPU."
                    cpu_probe(torch, torch_version, reason, error)
                else:
                    if backend == "cpu":
                        backend = "cuda"
                    runtime_name = "ROCm" if backend == "rocm" else "CUDA"
                    device_name = None
                    try:
                        device_name = str(cuda_api.get_device_name(0))
                        sample = torch.ones(1, device="cuda")
                        value = (sample + sample).sum().item()
                        if value != 2.0:
                            raise RuntimeError("GPU tensor result was incorrect")
                        cuda_api.synchronize()
                    except Exception as exc:
                        error = runtime_name + " GPU tensor operation failed (" + exception_text(exc) + ")"
                        cpu_probe(torch, torch_version, error + "; falling back to CPU.", error)
                    else:
                        emit(
                            backend,
                            True,
                            device_name,
                            torch_version,
                            runtime_name + " tensor operation and synchronization succeeded.",
                        )
"""


def _cpu_probe_result(reason: str, *, error: str | None = None) -> dict[str, object]:
    return {
        "backend": "cpu",
        "gpu_available": False,
        "device_name": None,
        "torch_version": None,
        "reason": reason,
        "error": error,
    }


def _parse_probe_result(stdout: str) -> dict[str, object] | None:
    if not isinstance(stdout, str):
        return None
    prefix = f"{_PROBE_MARKER} "
    encoded = next(
        (line[len(prefix) :] for line in reversed(stdout.splitlines()) if line.startswith(prefix)),
        None,
    )
    if encoded is None:
        return None
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    backend = value.get("backend")
    gpu_available = value.get("gpu_available")
    device_name = value.get("device_name")
    torch_version = value.get("torch_version")
    reason = value.get("reason")
    error = value.get("error")
    if (
        backend not in {"cpu", "cuda", "rocm"}
        or not isinstance(gpu_available, bool)
        or not (device_name is None or isinstance(device_name, str))
        or not (torch_version is None or isinstance(torch_version, str))
        or not isinstance(reason, str)
        or not (error is None or isinstance(error, str))
        or (gpu_available and backend == "cpu")
    ):
        return None
    return {
        "backend": backend,
        "gpu_available": gpu_available,
        "device_name": device_name,
        "torch_version": torch_version,
        "reason": reason,
        "error": error,
    }


def probe_runtime(python: Path, *, env: dict[str, str] | None = None) -> dict[str, object]:
    """Probe PyTorch in a bounded child process so bootstrap stays dependency-free."""
    try:
        result = subprocess.run(
            [str(python), "-c", _PROBE_CODE],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_RUNTIME_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        reason = (
            f"PyTorch runtime probe timed out after {_RUNTIME_TIMEOUT_SECONDS} seconds; using CPU."
        )
        return _cpu_probe_result(
            reason,
            error=reason.removesuffix("; using CPU."),
        )
    except FileNotFoundError:
        reason = "The selected Python could not be started for the runtime probe; using CPU."
        return _cpu_probe_result(reason, error=reason.removesuffix("; using CPU."))
    except (OSError, subprocess.SubprocessError):
        reason = "The PyTorch runtime probe could not run; using CPU."
        return _cpu_probe_result(reason, error=reason.removesuffix("; using CPU."))

    if result.returncode != 0:
        error = f"PyTorch runtime probe process failed with exit status {result.returncode}"
        return _cpu_probe_result(
            error + "; using CPU.",
            error=error,
        )
    parsed = _parse_probe_result(result.stdout)
    if parsed is None:
        error = "PyTorch runtime probe returned malformed output"
        return _cpu_probe_result(error + "; using CPU.", error=error)
    return parsed
