"""Run the same pytest command in CI and local pre-commit."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COVERAGE_JSON = ROOT / "coverage.json"


def _coverage_floors() -> tuple[float, float]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    coverage = data["tool"]["autodj"]["coverage"]
    return float(coverage["line_fail_under"]), float(coverage["branch_fail_under"])


def _check_coverage() -> int:
    line_floor, branch_floor = _coverage_floors()
    totals = json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))["totals"]
    line_coverage = float(totals["percent_statements_covered"])
    branch_coverage = float(totals["percent_branches_covered"])

    failures = []
    if line_coverage < line_floor:
        failures.append(f"line coverage {line_coverage:.2f}% < {line_floor:.1f}%")
    if branch_coverage < branch_floor:
        failures.append(f"branch coverage {branch_coverage:.2f}% < {branch_floor:.1f}%")

    if failures:
        print("Coverage gate failed: " + "; ".join(failures), file=sys.stderr)
        return 1

    print(
        "Coverage gates passed: "
        f"lines {line_coverage:.2f}% >= {line_floor:.1f}%, "
        f"branches {branch_coverage:.2f}% >= {branch_floor:.1f}%"
    )
    return 0


def main() -> int:
    env = os.environ.copy()
    env.setdefault("OMP_NUM_THREADS", "1")
    env.setdefault("MKL_NUM_THREADS", "1")
    env.setdefault("OPENBLAS_NUM_THREADS", "1")
    env.setdefault("VECLIB_MAXIMUM_THREADS", "1")
    env.setdefault("NUMEXPR_NUM_THREADS", "1")
    if platform.system() == "Darwin":
        env.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

    default_workers = "auto" if platform.system() == "Windows" else "0"
    workers = os.environ.get("AUTODJ_PYTEST_WORKERS", default_workers)
    result = subprocess.call(
        [
            sys.executable,
            "-m",
            "pytest",
            "--tb=short",
            "--cov",
            "--cov-branch",
            "--cov-fail-under=0",
            f"--cov-report=json:{COVERAGE_JSON}",
            "--cov-report=xml",
            "--cov-report=term",
            "-n",
            workers,
            *sys.argv[1:],
        ],
        env=env,
    )
    if result != 0:
        return result
    return _check_coverage()


if __name__ == "__main__":
    raise SystemExit(main())
