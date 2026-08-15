"""Regression tests for the configured docstring coverage gate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def _close_global_dj_cache_between_tests() -> None:
    """Keep this subprocess policy test independent of runtime imports."""


def test_configured_interrogate_gate_passes() -> None:
    """Public API docstring coverage must remain at 100 percent."""
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "interrogate",
            "-c",
            "pyproject.toml",
            "--fail-under=100",
            "src/autodj",
        ],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, f"Interrogate docstring coverage failed:\n{output}"
