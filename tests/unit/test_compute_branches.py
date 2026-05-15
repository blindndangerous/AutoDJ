"""Branch coverage tests for autodj.compute (GPU probe + caching)."""

from __future__ import annotations

import os
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
