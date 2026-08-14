"""Static typing regressions for SciPy signal call sites."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def _run_linux_pyright(*, scipy_module: str = "scipy.signal") -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix="autodj-pyright-sosfilt-") as raw_checkout:
        checkout = Path(raw_checkout)
        shutil.copy2(root / "pyproject.toml", checkout)
        shutil.copytree(root / "src", checkout / "src")
        if scipy_module != "scipy.signal":
            for module_name in ("player.py", "transitions.py"):
                module_path = checkout / "src" / "autodj" / module_name
                source = module_path.read_text(encoding="utf-8")
                module_path.write_text(
                    source.replace("from scipy.signal import", f"from {scipy_module} import"),
                    encoding="utf-8",
                )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pyright",
                "src/autodj/player.py",
                "src/autodj/transitions.py",
                "--pythonplatform",
                "Linux",
                "--pythonpath",
                sys.executable,
                "--warnings",
            ],
            cwd=checkout,
            capture_output=True,
            check=False,
            text=True,
            timeout=60,
        )
    return result


def test_linux_pyright_accepts_scipy_sosfilt_calls() -> None:
    """No-``zi`` ``sosfilt`` calls must narrow to their array return type."""
    result = _run_linux_pyright()

    output = result.stdout + result.stderr
    assert result.returncode == 0, f"Linux-target Pyright failed:\n{output}"


def test_linux_pyright_rejects_unresolved_scipy() -> None:
    """Regression gate must fail when SciPy imports cannot be resolved."""
    result = _run_linux_pyright(scipy_module="missing_scipy.signal")

    output = result.stdout + result.stderr
    assert result.returncode != 0, f"Pyright accepted unresolved SciPy:\n{output}"
    assert 'Import "missing_scipy.signal" could not be resolved' in output
