"""Fail CI when a reviewed pip-audit suppression reaches its expiry."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime

SUPPRESSIONS = {
    "PYSEC-2022-42969": date(2026, 11, 2),
}


def _parse_date(value: str) -> date:
    """Parse an ISO calendar date for deterministic regression tests."""
    return date.fromisoformat(value)


def check_suppressions(today: date) -> list[str]:
    """Return actionable errors for every expired suppression."""
    return [
        (
            f"{vulnerability} suppression expired on {expiry.isoformat()}; "
            "re-review OSV evidence, remove the suppression if fixed, or renew "
            "the documented expiry and this guard."
        )
        for vulnerability, expiry in SUPPRESSIONS.items()
        if today > expiry
    ]


def main(argv: list[str] | None = None) -> int:
    """Check suppressions against current UTC date or an explicit test date."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--today", type=_parse_date, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    today = args.today or datetime.now(UTC).date()
    errors = check_suppressions(today)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
