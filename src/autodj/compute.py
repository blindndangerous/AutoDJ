"""Choose the PyTorch device for MuQ embedding.

PyTorch exposes both NVIDIA CUDA and AMD ROCm through ``torch.cuda`` and
the ``"cuda"`` device string. ROCm needs a compatible PyTorch installation;
see ``docs/windows-amd.md`` for the tested Windows setup.
"""

from __future__ import annotations

import os

import torch


def device_string() -> str:
    """Return ``"cuda"`` when PyTorch sees a CUDA or ROCm GPU, else ``"cpu"``."""
    return "cuda" if os.environ.get("AUTODJ_GPU") != "0" and torch.cuda.is_available() else "cpu"


def device_description() -> str:
    """Name the selected GPU and runtime so AMD is not mislabeled as NVIDIA."""
    if device_string() == "cpu":
        return "CPU"
    runtime = "ROCm" if torch.version.hip else "CUDA"
    return f"{torch.cuda.get_device_name(0)} ({runtime} GPU)"
