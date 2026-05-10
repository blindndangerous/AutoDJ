"""Shared GPU/CPU device probe.

Single source of truth for "use CUDA when available, fall back to CPU
otherwise" across every subcommand that has GPU-eligible work
(``index``'s MuQ embed, ``analyse``'s beat grid).  Centralising the
probe here keeps detection logic, the env-var override, and the
diagnostic log line consistent — and means future GPU-eligible steps
opt in by calling one helper instead of re-rolling their own
``torch.cuda.is_available()`` + try/except dance.

The ``AUTODJ_GPU=0`` env var disables GPU for every step at once.
Per-step overrides (e.g. ``AUTODJ_DJMETA_GPU=0``) layer on top.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PROBE_CACHE: bool | None = None


def _global_disabled() -> bool:
    return os.environ.get("AUTODJ_GPU", "1") == "0"


def gpu_available() -> bool:
    """Return True iff CUDA is usable and the user hasn't disabled it.

    Cached after the first probe so CPU-only hosts pay the torch
    import cost once.  Honours ``AUTODJ_GPU=0`` on every call (cheap)
    so the kill-switch works without a restart.
    """
    global _PROBE_CACHE
    if _global_disabled():
        return False
    if _PROBE_CACHE is not None:
        return _PROBE_CACHE
    try:
        import torch
    except ImportError:
        _PROBE_CACHE = False
        return False
    _PROBE_CACHE = bool(torch.cuda.is_available())
    return _PROBE_CACHE


def device_string() -> str:
    """Return ``"cuda"`` when GPU is available, else ``"cpu"``."""
    return "cuda" if gpu_available() else "cpu"


def reset_probe_cache() -> None:
    """Clear the cached probe result (test hook)."""
    global _PROBE_CACHE
    _PROBE_CACHE = None
