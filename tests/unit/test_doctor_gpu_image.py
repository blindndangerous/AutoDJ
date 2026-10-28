"""Doctor's ``gpu-image`` check: a container's image against the NVIDIA GPU it sees."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import autodj.doctor as doctor
from autodj import updates
from autodj.config import (
    AutoDJConfig,
    HuggingFaceConfig,
    IndexConfig,
    LibraryConfig,
    ModelConfig,
    PlaybackConfig,
    ServerConfig,
)
from autodj.doctor import CheckStatus, DoctorCheck


def _never() -> bool:
    raise AssertionError("not asked")


def _check(
    tmp_path: Path,
    *,
    variant: str | None,
    gpu: bool,
    torch_cuda: bool | None = None,
    container: bool = True,
) -> tuple[DoctorCheck, ...]:
    marker = tmp_path / ".dockerenv"
    if container:
        marker.touch()
    environ = {} if variant is None else {updates.IMAGE_VARIANT_ENV: variant}
    return doctor._gpu_image_check(
        environ=environ,
        markers=(marker,),
        gpu_visible=lambda: gpu,
        torch_sees_cuda=_never if torch_cuda is None else lambda: torch_cuda,
    )


def test_outside_a_container_or_without_a_gpu_a_cpu_image_gets_no_line(tmp_path: Path) -> None:
    """ADR-0016-23: bare metal is setup's business; a CPU image without a GPU has nothing to say."""
    assert _check(tmp_path, variant="cuda", gpu=True, container=False) == ()
    assert _check(tmp_path, variant="cpu", gpu=False) == ()
    assert _check(tmp_path, variant=None, gpu=False) == ()


@pytest.mark.parametrize("variant", ["cpu", None, "something else"])
def test_a_cpu_image_that_sees_a_gpu_is_told_about_the_cuda_image_and_the_worker(
    tmp_path: Path, variant: str | None
) -> None:
    """ADR-0016-23: one sentence, without importing torch."""
    (check,) = _check(tmp_path, variant=variant, gpu=True)
    assert (check.name, check.status) == ("gpu-image", CheckStatus.WARN)
    assert "-cuda image" in check.summary
    assert "GPU analysis worker" in check.summary
    assert ". " not in check.summary


def test_a_cuda_image_without_a_gpu_falls_back_to_the_cpu_and_says_how_to_pass_it_in(
    tmp_path: Path,
) -> None:
    """ADR-0016-23: torch is not imported when no GPU is visible."""
    (check,) = _check(tmp_path, variant="cuda", gpu=False)
    assert check.status is CheckStatus.WARN
    assert "falls back to the CPU" in check.summary
    assert "NVIDIA Container Toolkit" in str(check.detail)
    assert "compose.gpu.yaml" in str(check.detail)
    assert "Docker Desktop on Windows has GPU support built in" in str(check.detail)


def test_a_cuda_image_with_a_gpu_asks_pytorch_whether_it_can_use_it(tmp_path: Path) -> None:
    """ADR-0016-23."""
    (usable,) = _check(tmp_path, variant="cuda", gpu=True, torch_cuda=True)
    assert (usable.status, usable.summary) == (
        CheckStatus.PASS,
        "CUDA image; song analysis uses the NVIDIA GPU",
    )
    (unusable,) = _check(tmp_path, variant="cuda", gpu=True, torch_cuda=False)
    assert unusable.status is CheckStatus.WARN
    assert "falls back to the CPU" in unusable.summary
    assert "CUDA 13.2" in str(unusable.detail)


def test_a_gpu_is_visible_by_its_device_file_or_nvidia_smi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0016-23: read-only probes; nvidia-smi only when no device file is there."""
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "nvidiactl").touch()
    (dev / "nvidia-uvm").touch()
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    assert doctor._nvidia_gpu_visible(dev) is False

    (dev / "nvidia1").touch()
    monkeypatch.setattr(updates, "run_bounded", lambda *a, **k: pytest.fail("ran nvidia-smi"))
    assert doctor._nvidia_gpu_visible(dev) is True
    (dev / "nvidia1").unlink()

    calls: list[list[str]] = []

    def smi(argv: list[str], *, timeout: float) -> SimpleNamespace:
        calls.append(argv)
        assert timeout <= 5.0
        return SimpleNamespace(returncode=0, stdout="GPU 0: Example GPU (UUID: GPU-1234)\n")

    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(updates, "run_bounded", smi)
    assert doctor._nvidia_gpu_visible(dev) is True
    assert calls == [["/usr/bin/nvidia-smi", "-L"]]

    def failed(argv: list[str], *, timeout: float) -> SimpleNamespace:
        return SimpleNamespace(returncode=9, stdout="")

    monkeypatch.setattr(updates, "run_bounded", failed)
    assert doctor._nvidia_gpu_visible(dev) is False

    def hung(argv: list[str], *, timeout: float) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(updates, "run_bounded", hung)
    assert doctor._nvidia_gpu_visible(dev) is False


def test_pytorch_is_asked_in_a_child_with_a_time_limit_and_quietly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0016-23: a wedged driver cannot stall Doctor, and any failure means no GPU."""
    seen: list[tuple[list[str], float]] = []

    def answer(code: int) -> object:
        def run(argv: list[str], *, timeout: float) -> SimpleNamespace:
            seen.append((argv, timeout))
            return SimpleNamespace(returncode=code, stdout="", stderr="UserWarning: noise\n")

        return run

    monkeypatch.setattr(updates, "run_bounded", answer(0))
    assert doctor._torch_sees_cuda() is True
    argv, timeout = seen[0]
    # Isolated, so /config (the working directory) is not on the child's path; warnings off.
    assert argv[1:5] == ["-I", "-W", "ignore", "-c"]
    assert "torch.cuda.is_available()" in argv[5]
    assert timeout == doctor.TORCH_PROBE_SECONDS <= 20.0

    monkeypatch.setattr(updates, "run_bounded", answer(3))
    assert doctor._torch_sees_cuda() is False

    def wedged(argv: list[str], *, timeout: float) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(updates, "run_bounded", wedged)
    assert doctor._torch_sees_cuda() is None

    def broken(argv: list[str], *, timeout: float) -> SimpleNamespace:
        raise ValueError("anything")

    monkeypatch.setattr(updates, "run_bounded", broken)
    assert doctor._torch_sees_cuda() is False


def test_a_gpu_check_that_times_out_says_so_rather_than_blaming_the_driver(
    tmp_path: Path,
) -> None:
    """ADR-0016-23."""
    marker = tmp_path / ".dockerenv"
    marker.touch()
    (check,) = doctor._gpu_image_check(
        environ={updates.IMAGE_VARIANT_ENV: "cuda"},
        markers=(marker,),
        gpu_visible=lambda: True,
        torch_sees_cuda=lambda: None,
    )
    assert check.status is CheckStatus.WARN
    assert "the GPU check timed out" in check.summary
    assert "20 seconds" in str(check.detail)
    assert "580" not in str(check.detail)


def test_doctor_reports_the_gpu_image_check_inside_a_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0016-23: run_doctor includes the check, and only in a container."""
    music, index, models = (tmp_path / name for name in ("music", "index", "models"))
    for folder in (music, index, models):
        folder.mkdir()
    cfg = AutoDJConfig(
        library=LibraryConfig(music, None, ["flac"]),
        index=IndexConfig(index, models),
        playback=PlaybackConfig(),
        model=ModelConfig(),
        huggingface=HuggingFaceConfig(None),
        config_path=tmp_path / "config.toml",
        server=ServerConfig(host="127.0.0.1"),
        config_sources=("defaults",),
    )
    monkeypatch.setenv(updates.CHECK_ENV, "0")
    monkeypatch.setenv(updates.IMAGE_VARIANT_ENV, "cuda")
    monkeypatch.setattr(doctor, "_nvidia_gpu_visible", lambda: False)
    monkeypatch.setattr(doctor, "_torch_sees_cuda", _never)
    marker = tmp_path / ".dockerenv"
    monkeypatch.setattr(updates, "CONTAINER_MARKERS", (marker,))
    names = [check.name for check in doctor.run_doctor(cfg, check_updates=False).checks]
    assert "gpu-image" not in names

    marker.touch()
    checks = {check.name: check for check in doctor.run_doctor(cfg, check_updates=False).checks}
    assert checks["gpu-image"].status is CheckStatus.WARN
    assert "falls back to the CPU" in checks["gpu-image"].summary
