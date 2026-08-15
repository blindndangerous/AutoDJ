"""Behavior checks for narrowly targeted pytest warning gates."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run_probe(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    probe = tmp_path / "test_warning_probe.py"
    probe.write_text(source, encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(ROOT / "pyproject.toml"),
            str(probe),
            "--no-cov",
            "-q",
            f"--confcutdir={tmp_path}",
            f"--basetemp={tmp_path / 'pytest'}",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def test_unclosed_sqlite_finalizer_is_a_blocking_warning(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        """\
import gc
import sqlite3

def test_leak():
    connection = sqlite3.connect(\":memory:\")
    del connection
    gc.collect()
""",
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "PytestUnraisableExceptionWarning" in result.stdout + result.stderr
    assert "unclosed database" in result.stdout + result.stderr


def test_unrelated_unraisable_warning_remains_non_blocking(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        """\
import gc

class BrokenFinalizer:
    def __del__(self):
        raise ValueError(\"unrelated finalizer\")

def test_unrelated():
    value = BrokenFinalizer()
    del value
    gc.collect()
""",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PytestUnraisableExceptionWarning" in result.stdout + result.stderr
    assert "unrelated finalizer" in result.stdout + result.stderr
