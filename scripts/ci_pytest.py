"""Run the same pytest command in CI and local pre-commit.

The line-coverage floor is ``fail_under`` in pyproject's
``[tool.coverage.report]``; pytest-cov fails the run below it.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys


def main() -> int:
    env = os.environ.copy()
    env.setdefault("OMP_NUM_THREADS", "1")
    env.setdefault("MKL_NUM_THREADS", "1")
    env.setdefault("OPENBLAS_NUM_THREADS", "1")
    env.setdefault("VECLIB_MAXIMUM_THREADS", "1")
    env.setdefault("NUMEXPR_NUM_THREADS", "1")
    if platform.system() == "Darwin":
        env.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

    default_workers = "0"
    workers = os.environ.get("AUTODJ_PYTEST_WORKERS", default_workers)
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "pytest",
            "--tb=short",
            "--cov",
            "--cov-report=term",
            "-n",
            workers,
            *sys.argv[1:],
        ],
        env=env,
    )


if __name__ == "__main__":
    raise SystemExit(main())
