"""Regression tests for the CI pip-audit suppression expiry gate."""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "check_pip_audit_suppressions.py"


def _run_for(day: date) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--today", day.isoformat()],
        check=False,
        capture_output=True,
        text=True,
    )


def test_suppression_remains_valid_through_expiry_date() -> None:
    """The documented review date itself remains inside the approved window."""
    result = _run_for(date(2026, 11, 2))

    assert result.returncode == 0, result.stderr


def test_suppression_fails_after_expiry_with_actionable_message() -> None:
    """CI must demand evidence review on the first day after expiry."""
    result = _run_for(date(2026, 11, 3))

    assert result.returncode == 1
    assert "PYSEC-2022-42969" in result.stderr
    assert "expired on 2026-11-02" in result.stderr
    assert "re-review" in result.stderr
