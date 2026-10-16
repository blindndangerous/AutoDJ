"""Run the frontend checks that the pre-push hooks need.

    python scripts/frontend_checks.py deadcode
    python scripts/frontend_checks.py test [CHANGED_FILE ...]

``deadcode`` runs knip over the whole project, as CI does.

``test`` runs only the Vitest tests related to files changed in the push:

* tests that import a changed file, directly or through other modules
  (Vitest's ``related`` mode);
* tests that mention a changed file's name, or import a helper that does.
  This catches tests that read ``index.html`` or ``app.css`` from disk, which
  the import graph cannot see.

CI runs the full suite.

The script calls Node directly instead of going through npm so it behaves the
same on Windows and Linux. Vitest gets the same 5 GB heap as CI's frontend job.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = ROOT / "tests" / "jsmodules"
HEAP_OPTION = "--max-old-space-size=5120"


def _fail(message: str) -> int:
    print(f"frontend checks: {message}", file=sys.stderr)
    return 1


def _bin_script(package: str) -> Path | None:
    """Return the JS file behind ``package``'s command, or None if not installed."""
    manifest = ROOT / "node_modules" / package / "package.json"
    try:
        bin_field = json.loads(manifest.read_text(encoding="utf-8")).get("bin")
    except (OSError, ValueError):
        return None
    if isinstance(bin_field, dict):
        bin_field = bin_field.get(package)
    if not isinstance(bin_field, str):
        return None
    script = manifest.parent / bin_field
    return script if script.is_file() else None


def _run_node(package: str, args: list[str]) -> int:
    script = _bin_script(package)
    if script is None:
        return _fail(
            f"{package} is not installed in node_modules. "
            "Run `npm ci --ignore-scripts` in the repository root, then push again."
        )
    node = shutil.which("node")
    if node is None:
        return _fail("Node.js is not on PATH. Install it to run the frontend checks.")
    env = os.environ.copy()
    if package == "vitest":
        node_options = env.get("NODE_OPTIONS", "")
        env["NODE_OPTIONS"] = f"{node_options} {HEAP_OPTION}".strip()
    return subprocess.call([node, str(script), *args], cwd=ROOT, env=env)


def _related_sources(changed: list[str]) -> list[str]:
    """Return the changed files plus test files that mention one by name."""
    names = {Path(path).name for path in changed}
    sources = {str(ROOT / path) for path in changed}
    for test_file in sorted(TEST_DIR.rglob("*.js")):
        try:
            text = test_file.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(name in text for name in names):
            sources.add(str(test_file))
    return sorted(sources)


def run_tests(changed: list[str]) -> int:
    if not changed:
        return _fail("no changed frontend files were supplied to Vitest")
    print(
        f"frontend checks: running Vitest on tests related to {len(changed)} changed "
        "file(s); CI runs the full suite",
        flush=True,
    )
    return _run_node(
        "vitest", ["related", "--run", "--passWithNoTests", *_related_sources(changed)]
    )


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"test", "deadcode"}:
        print(__doc__, file=sys.stderr)
        return 2
    if argv[0] == "deadcode":
        return _run_node("knip", [])
    return run_tests(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
