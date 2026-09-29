"""Choose the PyTorch device for MuQ embedding.

PyTorch exposes both NVIDIA CUDA and AMD ROCm through ``torch.cuda`` and
the ``"cuda"`` device string. ROCm needs a compatible PyTorch installation;
see ``docs/windows-amd.md`` for the tested Windows setup.
"""

from __future__ import annotations

import torch


def device_string() -> str:
    """Return ``"cuda"`` when PyTorch sees a CUDA or ROCm GPU, else ``"cpu"``."""
    return "cuda" if torch.cuda.is_available() else "cpu"
