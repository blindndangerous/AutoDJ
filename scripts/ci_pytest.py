"""Run the same pytest command in CI and local pre-commit."""

from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "pytest",
            "--tb=short",
            "--cov",
            "--cov-report=xml",
            "--cov-report=term",
            "-n",
            "auto",
            *sys.argv[1:],
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
