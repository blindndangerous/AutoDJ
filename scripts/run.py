"""Run the selected local AutoDJ runtime without syncing its GPU packages."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Use one first-run/setup/runtime flow for every source checkout launcher."""
    os.environ["AUTODJ_BOOTSTRAP"] = "1"
    if len(sys.argv) == 1:
        sys.argv.append("serve")
    sys.argv[0] = "autodj"
    sys.path.insert(0, str(ROOT / "src"))
    from autodj.launcher import main as launch

    raise SystemExit(launch())


if __name__ == "__main__":
    main()
