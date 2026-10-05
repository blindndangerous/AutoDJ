"""Tests for the dependency-free first-run hardware and runtime probes."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest


@pytest.fixture
def setup_hardware() -> ModuleType:
    """Load the bootstrap module directly, without importing the application."""
    path = Path(__file__).parents[2] / "scripts" / "setup_hardware.py"
    name = "setup_hardware_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _completed(stdout: str = "", *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, "")


def _runtime_output(module: ModuleType, **values: object) -> str:
    values.setdefault("error", None)
    return f"{module._PROBE_MARKER} " + json.dumps(values)


def test_amd_recipes_cover_only_explicit_targets_on_supported_platforms(
    setup_hardware: ModuleType,
) -> None:
    profiles = json.loads(
        (Path(__file__).parents[2] / "scripts" / "setup-profiles.json").read_text(encoding="utf-8")
    )
    expected = set(setup_hardware._AMD_MODEL_TARGETS)

    for system in ("Windows", "Linux"):
        platform_recipe = profiles["amd"]["platforms"][system]
        assert set(platform_recipe["architectures"]) == expected
        for target, packages in platform_recipe["architectures"].items():
            assert packages == [
                f"torch[device-{target}]==2.12.0+rocm7.14.1",
                f"torchvision[device-{target}]==0.27.0+rocm7.14.1",
                "torchaudio==2.11.0+rocm7.14.1",
            ]


def test_windows_recommends_amd_for_exact_radeon_860m_token(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = setup_hardware
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module.platform, "machine", lambda: "AMD64")
    run = Mock(return_value=_completed('["AMD Radeon(TM) 860M Graphics"]'))
    monkeypatch.setattr(module.subprocess, "run", run)

    hardware = module.detect_hardware()

    assert hardware.system == "Windows"
    assert hardware.machine == "AMD64"
    assert hardware.gpus == ("AMD Radeon(TM) 860M Graphics",)
    assert hardware.recommendation == "amd"
    assert hardware.amd_arch == "gfx1152"
    assert "860M" in hardware.reason
    command = run.call_args.args[0]
    assert command[0] == "powershell.exe"
    assert "Get-CimInstance Win32_VideoController" in command[-1]
    assert "ConvertTo-Json" in command[-1]
    assert run.call_args.kwargs["timeout"] == module._HARDWARE_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("AMD Radeon RX 9070 XT", "gfx1201"),
        ("AMD Radeon RX 9060 XT", "gfx1200"),
        ("AMD Radeon RX 7900 XTX", "gfx1100"),
        ("AMD Radeon RX 7800 XT", "gfx1101"),
        ("AMD Radeon RX 7600", "gfx1102"),
        ("AMD Radeon 890M Graphics", "gfx1150"),
        ("AMD Radeon 8060S Graphics", "gfx1151"),
        ("AMD Radeon 820M Graphics", "gfx1152"),
        ("AMD Radeon 780M Graphics", "gfx1103"),
    ],
)
def test_windows_maps_supported_marketed_amd_models_to_explicit_targets(
    setup_hardware: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    target: str,
) -> None:
    module = setup_hardware
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(module.subprocess, "run", Mock(return_value=_completed(json.dumps(name))))

    hardware = module.detect_hardware()

    assert hardware.recommendation == "amd"
    assert hardware.amd_arch == target


@pytest.mark.parametrize(
    "name",
    [
        "AMD Radeon RX 8600M Graphics",
        "AMD Radeon RX 6950 XT",
        "AMD Radeon RX 7600 XT",
        "AMD Radeon 840M Graphics",
    ],
)
def test_amd_models_outside_explicit_mapping_fall_back_to_cpu(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    module = setup_hardware
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(module.subprocess, "run", Mock(return_value=_completed(json.dumps(name))))

    hardware = module.detect_hardware()

    assert hardware.recommendation == "cpu"
    assert hardware.amd_arch is None


def test_mixed_supported_amd_architectures_fall_back_to_cpu(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = setup_hardware
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        Mock(return_value=_completed(json.dumps(["AMD Radeon RX 9070 XT", "AMD Radeon RX 7600"]))),
    )

    hardware = module.detect_hardware()

    assert hardware.recommendation == "cpu"
    assert hardware.amd_arch is None
    assert "different ROCm targets" in hardware.reason


def test_mixed_supported_and_unmapped_amd_models_fall_back_to_cpu(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = setup_hardware
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        Mock(
            return_value=_completed(json.dumps(["AMD Radeon RX 9070 XT", "AMD Radeon RX 6950 XT"]))
        ),
    )

    hardware = module.detect_hardware()

    assert hardware.recommendation == "cpu"
    assert hardware.amd_arch is None
    assert "without a complete supported device mapping" in hardware.reason


@pytest.mark.parametrize("system", ["Windows", "Linux"])
def test_nvidia_is_recommended_only_on_x86_64(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch, system: str
) -> None:
    module = setup_hardware
    monkeypatch.setattr(module.platform, "system", lambda: system)
    monkeypatch.setattr(module.platform, "machine", lambda: "aarch64")
    if system == "Windows":
        monkeypatch.setattr(
            module.subprocess, "run", Mock(return_value=_completed('"NVIDIA RTX 4090"'))
        )
    else:
        monkeypatch.setattr(
            module.subprocess,
            "run",
            Mock(return_value=_completed("NVIDIA RTX 4090\n")),
        )

    hardware = module.detect_hardware()

    assert hardware.recommendation == "cpu"
    assert "Windows/Linux x86-64 only" in hardware.reason


def test_linux_amd_860m_maps_to_supported_architecture(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = setup_hardware
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        Mock(
            side_effect=[
                _completed("", returncode=9),
                _completed("03:00.0 VGA compatible controller: AMD Radeon 860M\n"),
            ]
        ),
    )

    hardware = module.detect_hardware()

    assert hardware.recommendation == "amd"
    assert hardware.amd_arch == "gfx1152"


@pytest.mark.parametrize(
    "name",
    ["AMD Radeon 8600M Graphics", "AMD Radeon 860M2 Graphics", "AMD Radeon 8600 Graphics"],
)
def test_windows_amd_match_requires_exact_860m_model_token(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    module = setup_hardware
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(module.subprocess, "run", Mock(return_value=_completed(json.dumps(name))))

    hardware = module.detect_hardware()

    assert hardware.gpus == (name,)
    assert hardware.recommendation == "cpu"
    assert "without a complete supported device mapping" in hardware.reason


def test_windows_nvidia_takes_priority_when_multiple_adapters_are_reported(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = setup_hardware
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        Mock(return_value=_completed(json.dumps(["AMD Radeon 860M", "NVIDIA GeForce RTX 4070"]))),
    )

    hardware = module.detect_hardware()

    assert hardware.recommendation == "nvidia"
    assert len(hardware.gpus) == 2


def test_windows_malformed_json_reports_detection_unavailable(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = setup_hardware
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(module.subprocess, "run", Mock(return_value=_completed("not-json")))

    hardware = module.detect_hardware()

    assert hardware.recommendation == "cpu"
    assert hardware.gpus == ()
    assert "detection unavailable" in hardware.reason
    assert "no video controllers" not in hardware.reason


def test_windows_missing_powershell_is_not_reported_as_no_gpu(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = setup_hardware
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(module.subprocess, "run", Mock(side_effect=FileNotFoundError))

    hardware = module.detect_hardware()

    assert hardware.recommendation == "cpu"
    assert "detection unavailable" in hardware.reason
    assert "no video controllers" not in hardware.reason


def test_linux_uses_nvidia_smi_names(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = setup_hardware
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    run = Mock(return_value=_completed("NVIDIA RTX 4090\nNVIDIA A10\n"))
    monkeypatch.setattr(module.subprocess, "run", run)

    hardware = module.detect_hardware()

    assert hardware.recommendation == "nvidia"
    assert hardware.gpus == ("NVIDIA RTX 4090", "NVIDIA A10")
    assert run.call_args.args[0] == [
        "nvidia-smi",
        "--query-gpu=name",
        "--format=csv,noheader",
    ]


def test_linux_nvidia_smi_is_authoritative_when_model_name_omits_vendor(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = setup_hardware
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        module.subprocess, "run", Mock(return_value=_completed("Tesla V100-SXM2-16GB\n"))
    )

    hardware = module.detect_hardware()

    assert hardware.gpus == ("Tesla V100-SXM2-16GB",)
    assert hardware.recommendation == "nvidia"


def test_linux_uses_lspci_fallback_for_nvidia(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = setup_hardware
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    run = Mock(
        side_effect=[
            _completed("", returncode=9),
            _completed(
                "00:02.0 VGA compatible controller: Intel Corporation UHD Graphics\n"
                "01:00.0 3D controller: NVIDIA Corporation AD104 [GeForce RTX 4070]\n"
            ),
        ]
    )
    monkeypatch.setattr(module.subprocess, "run", run)

    hardware = module.detect_hardware()

    assert hardware.recommendation == "nvidia"
    assert hardware.gpus == (
        "Intel Corporation UHD Graphics",
        "NVIDIA Corporation AD104 [GeForce RTX 4070]",
    )
    assert run.call_count == 2
    assert run.call_args_list[1].args[0] == ["lspci", "-nn"]


def test_linux_malformed_nvidia_smi_output_falls_back_to_lspci(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = setup_hardware
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    run = Mock(
        side_effect=[
            _completed("NVIDIA-SMI has failed to communicate with the driver\n"),
            _completed(
                "03:00.0 VGA compatible controller: Advanced Micro Devices, Inc. [AMD/ATI]\n"
            ),
        ]
    )
    monkeypatch.setattr(module.subprocess, "run", run)

    hardware = module.detect_hardware()

    assert hardware.recommendation == "cpu"
    assert hardware.gpus == ("Advanced Micro Devices, Inc. [AMD/ATI]",)
    assert "without a complete supported device mapping" in hardware.reason


def test_linux_successful_empty_lspci_is_reported_as_no_gpu(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = setup_hardware
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    run = Mock(
        side_effect=[
            _completed("", returncode=9),
            _completed("00:14.0 USB controller: Intel Corporation Device 7a60\n"),
        ]
    )
    monkeypatch.setattr(module.subprocess, "run", run)

    hardware = module.detect_hardware()

    assert hardware.recommendation == "cpu"
    assert "no display-class GPUs" in hardware.reason
    assert "detection unavailable" not in hardware.reason


def test_linux_unavailable_probes_are_not_reported_as_no_gpu(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = setup_hardware
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    run = Mock(side_effect=FileNotFoundError)
    monkeypatch.setattr(module.subprocess, "run", run)

    hardware = module.detect_hardware()

    assert run.call_count == 2
    assert hardware.recommendation == "cpu"
    assert "detection unavailable" in hardware.reason
    assert "no display-class GPUs" not in hardware.reason


def test_darwin_explains_apple_acceleration_is_not_configured(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = setup_hardware
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.platform, "machine", lambda: "arm64")
    run = Mock()
    monkeypatch.setattr(module.subprocess, "run", run)

    hardware = module.detect_hardware()

    assert hardware.recommendation == "cpu"
    assert "Apple accelerator setup is not configured" in hardware.reason
    assert "no GPU" not in hardware.reason
    run.assert_not_called()


def test_runtime_probe_preserves_rocm_backend_and_environment(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = setup_hardware
    run = Mock(
        return_value=_completed(
            _runtime_output(
                module,
                backend="rocm",
                gpu_available=True,
                device_name="AMD Radeon RX 7900 XTX",
                torch_version="2.8.0+rocm6.3",
                reason="ROCm tensor operation and synchronization succeeded.",
            )
        )
    )
    monkeypatch.setattr(module.subprocess, "run", run)
    python = tmp_path / "python"
    env = {"PYTHONPATH": str(tmp_path)}

    result = module.probe_runtime(python, env=env)

    assert result["backend"] == "rocm"
    assert result["gpu_available"] is True
    assert result["device_name"] == "AMD Radeon RX 7900 XTX"
    command = run.call_args.args[0]
    assert command[:2] == [str(python), "-c"]
    assert "hip_version = getattr" in command[2]
    assert "torch.ones" in command[2]
    assert "synchronize()" in command[2]
    assert "hip" in command[2]
    assert run.call_args.kwargs["env"] == env
    assert run.call_args.kwargs["timeout"] == module._RUNTIME_TIMEOUT_SECONDS
    assert result["error"] is None


@pytest.mark.parametrize(
    ("backend", "device_name", "torch_version", "reason", "error"),
    [
        (
            "cpu",
            None,
            None,
            "PyTorch is not installed in this Python environment; using CPU.",
            "PyTorch is not installed in this Python environment.",
        ),
        (
            "cuda",
            None,
            "2.8.0+cu128",
            "CUDA runtime is present, but no usable GPU/driver was reported; using CPU.",
            "CUDA runtime is present, but no usable GPU/driver was reported.",
        ),
        (
            "rocm",
            "AMD GPU",
            "2.8.0+rocm6.3",
            "ROCm GPU tensor operation failed (RuntimeError); using CPU.",
            "ROCm GPU tensor operation failed (RuntimeError)",
        ),
    ],
)
def test_runtime_probe_reports_missing_driver_and_operation_failures(
    setup_hardware: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    device_name: str | None,
    torch_version: str | None,
    reason: str,
    error: str,
) -> None:
    module = setup_hardware
    monkeypatch.setattr(
        module.subprocess,
        "run",
        Mock(
            return_value=_completed(
                _runtime_output(
                    module,
                    backend=backend,
                    gpu_available=False,
                    device_name=device_name,
                    torch_version=torch_version,
                    reason=reason,
                    error=error,
                )
            )
        ),
    )

    result = module.probe_runtime(Path("python"))

    assert result["backend"] == backend
    assert result["gpu_available"] is False
    assert result["reason"] == reason
    assert result["error"] == error


def test_runtime_probe_does_not_trust_gpu_success_with_cpu_backend(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = setup_hardware
    monkeypatch.setattr(
        module.subprocess,
        "run",
        Mock(
            return_value=_completed(
                _runtime_output(
                    module,
                    backend="cpu",
                    gpu_available=True,
                    device_name="Fake GPU",
                    torch_version="2.8.0",
                    reason="claimed success",
                )
            )
        ),
    )

    result = module.probe_runtime(Path("python"))

    assert result["backend"] == "cpu"
    assert result["gpu_available"] is False
    assert "malformed output" in result["reason"]


def test_runtime_probe_runs_cpu_tensor_when_gpu_is_overridden(
    setup_hardware: ModuleType, tmp_path: Path
) -> None:
    torch_package = tmp_path / "torch"
    torch_package.mkdir()
    (torch_package / "__init__.py").write_text(
        """
__version__ = 'fake-cuda-build'
class _Version:
    cuda = '12.8'
    hip = None
version = _Version()
class _Tensor:
    def __init__(self, value):
        self.value = value
    def __add__(self, other):
        return _Tensor(self.value + other.value)
    def item(self):
        return self.value
def tensor(values, device=None):
    if device != 'cpu':
        raise AssertionError('CPU override must use a CPU tensor')
    return _Tensor(values[0])
class _Cuda:
    def is_available(self):
        raise AssertionError('CPU override must skip GPU probing')
cuda = _Cuda()
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({"PYTHONPATH": str(tmp_path), "AUTODJ_GPU": "0"})

    result = setup_hardware.probe_runtime(Path(sys.executable), env=env)

    assert result["backend"] == "cpu"
    assert result["gpu_available"] is False
    assert result["torch_version"] == "fake-cuda-build"
    assert "CPU tensor operation succeeded" in result["reason"]
    assert result["error"] is None


@pytest.mark.parametrize(
    ("side_effect", "return_value", "expected"),
    [
        (subprocess.TimeoutExpired("python", 30), None, "timed out"),
        (FileNotFoundError(), None, "could not be started"),
        (None, _completed("not-json"), "malformed output"),
        (None, _completed("traceback", returncode=1), "exit status 1"),
    ],
)
def test_runtime_probe_handles_process_failures_safely(
    setup_hardware: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    side_effect: BaseException | None,
    return_value: subprocess.CompletedProcess[str] | None,
    expected: str,
) -> None:
    module = setup_hardware
    run = Mock(side_effect=side_effect, return_value=return_value)
    monkeypatch.setattr(module.subprocess, "run", run)

    result = module.probe_runtime(Path("python"))

    assert result["backend"] == "cpu"
    assert result["gpu_available"] is False
    assert expected in result["reason"]


def test_runtime_probe_rejects_malformed_result_types(
    setup_hardware: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = setup_hardware
    monkeypatch.setattr(
        module.subprocess,
        "run",
        Mock(
            return_value=_completed(
                _runtime_output(
                    module,
                    backend="cuda",
                    gpu_available="yes",
                    device_name="NVIDIA GPU",
                    torch_version="2.8.0",
                    reason="claimed success",
                )
            )
        ),
    )

    result = module.probe_runtime(Path("python"))

    assert result["backend"] == "cpu"
    assert result["gpu_available"] is False
    assert "malformed output" in result["reason"]
