"""Policy tests for the CI pytest runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def ci_pytest_module() -> object:
    """Load the standalone runner without invoking its ``__main__`` block."""
    path = Path(__file__).parents[2] / "scripts" / "ci_pytest.py"
    spec = importlib.util.spec_from_file_location("ci_pytest_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_defaults_to_serial_workers(
    ci_pytest_module: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = ci_pytest_module
    call = Mock(return_value=1)
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module.subprocess, "call", call)
    monkeypatch.delenv("AUTODJ_PYTEST_WORKERS", raising=False)

    assert module.main() == 1
    assert call.call_args.args[0][call.call_args.args[0].index("-n") + 1] == "0"


def test_explicit_worker_override_is_preserved(
    ci_pytest_module: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = ci_pytest_module
    call = Mock(return_value=1)
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module.subprocess, "call", call)
    monkeypatch.setenv("AUTODJ_PYTEST_WORKERS", "3")

    assert module.main() == 1
    assert call.call_args.args[0][call.call_args.args[0].index("-n") + 1] == "3"
