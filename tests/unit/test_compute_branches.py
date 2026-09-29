"""Tests for autodj.compute device choice."""

from __future__ import annotations

import pytest
import torch

from autodj import compute


@pytest.mark.parametrize(("available", "expected"), [(True, "cuda"), (False, "cpu")])
def test_device_string_follows_torch_cuda(monkeypatch, available: bool, expected: str) -> None:
    monkeypatch.setattr(torch.cuda, "is_available", lambda: available)
    assert compute.device_string() == expected
