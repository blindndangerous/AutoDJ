"""Branch coverage tests for autodj.compute (GPU probe + caching)."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch


class TestCompute:
    def test_global_disabled_via_env_var(self) -> None:
        from autodj import compute

        compute.reset_probe_cache()
        with patch.dict(os.environ, {"AUTODJ_GPU": "0"}):
            assert compute.gpu_available() is False
            assert compute.device_string() == "cpu"
        compute.reset_probe_cache()

    def test_device_string_returns_cuda_when_gpu_available(self) -> None:
        from autodj import compute

        compute.reset_probe_cache()
        with patch.object(compute, "gpu_available", return_value=True):
            assert compute.device_string() == "cuda"

    def test_reset_probe_cache_clears_state(self) -> None:
        from autodj import compute

        compute._PROBE_CACHE = True
        compute.reset_probe_cache()
        assert compute._PROBE_CACHE is None

    def test_missing_torch_is_cached_as_cpu_only(self) -> None:
        from autodj import compute

        compute.reset_probe_cache()
        with patch.dict(sys.modules, {"torch": None}):
            assert compute.gpu_available() is False
        assert compute._PROBE_CACHE is False

    def test_successful_cuda_probe_is_reused(self) -> None:
        from autodj import compute

        compute.reset_probe_cache()
        torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True))
        with patch.dict(sys.modules, {"torch": torch}):
            assert compute.gpu_available() is True
            torch.cuda.is_available = lambda: False
            assert compute.gpu_available() is True
