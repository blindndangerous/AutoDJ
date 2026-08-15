"""Reject broad coverage exclusions that can hide real error paths."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_EXCLUSIONS = (
    "pragma: no cover",
    "raise NotImplementedError",
    "if TYPE_CHECKING:",
    "@overload",
    r"if torch.cuda.is_available\(\):",
    r"if not torch.cuda.is_available\(\):",
)


def main(repo_root: Path | None = None) -> int:
    """Return failure unless parsed coverage exclusions match the allowlist."""
    root = repo_root or ROOT
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    exclusions = config["tool"]["coverage"]["report"].get("exclude_lines", [])
    if not isinstance(exclusions, list) or not all(
        isinstance(exclusion, str) for exclusion in exclusions
    ):
        print(
            "Broad coverage exclusions are forbidden: exclude_lines must be a list of strings",
            file=sys.stderr,
        )
        return 1
    if tuple(exclusions) != ALLOWED_EXCLUSIONS:
        print(
            "Broad coverage exclusions are forbidden: exclude_lines must exactly match "
            "the approved allowlist",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
