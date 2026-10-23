"""Run explicit smoke, no-model, or full validation and require real execution."""

from __future__ import annotations

import argparse
import importlib.util
import ntpath
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

MODEL_PACKAGES = ("torch", "muq", "transformers")
SMOKE = ("tests/smoke",)
NO_MODEL = (
    *SMOKE,
    "tests/unit/test_cli.py::test_model_commands_reject_no_model_before_import_or_connection",
    "tests/unit/test_index_catalog_cli.py::test_index_without_a_model_keeps_playable_tracks_and_pending_analysis",
    "tests/unit/test_worker_api.py::test_a_valid_result_goes_into_the_live_catalog",
    "tests/unit/test_worker_api.py::test_a_server_without_the_model_cannot_check_results",
)
DESELECTED: set[str] = set()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--require-executed", action="append", default=[], metavar="SELECTOR")


def pytest_sessionstart(session: pytest.Session) -> None:
    DESELECTED.clear()


def pytest_deselected(items: list[pytest.Item]) -> None:
    if not items:
        return
    nodeids = [item.nodeid for item in items]
    config = items[0].config
    if hasattr(config, "workeroutput"):
        config.workeroutput.setdefault("required_deselected", []).extend(nodeids)
    else:
        DESELECTED.update(nodeids)


def pytest_testnodedown(node: object, error: object) -> None:
    DESELECTED.update(getattr(node, "workeroutput", {}).get("required_deselected", []))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Required evidence cannot disappear through skips, deselection or renames."""
    required = session.config.getoption("--require-executed")
    if not required or exitstatus or hasattr(session.config, "workerinput"):
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        print(
            "Cannot verify required execution without pytest's terminal reporter", file=sys.stderr
        )
        session.exitstatus = 1
        return
    incomplete = []
    for selector in required:
        path, _, suffix = selector.partition("::")
        prefix = Path(os.path.relpath(path, session.config.rootpath)).as_posix()
        if suffix:
            prefix += "::" + suffix

        def matches(report: object, prefix: str = prefix) -> bool:
            nodeid = report if isinstance(report, str) else getattr(report, "nodeid", "")
            return nodeid == prefix or nodeid.startswith(
                (prefix + "/", prefix + "::", prefix + "[")
            )

        passed = any(matches(report) for report in reporter.stats.get("passed", []))
        omitted = any(matches(nodeid) for nodeid in DESELECTED) or any(
            matches(report)
            for outcome in ("skipped", "xfailed", "deselected")
            for report in reporter.stats.get(outcome, [])
        )
        if not passed or omitted:
            incomplete.append(selector)
    if incomplete:
        reporter.write_sep("=", "Required checks did not execute: " + ", ".join(incomplete))
        session.exitstatus = 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("smoke", "no-model", "full"), default="full")
    args, extra = parser.parse_known_args()
    present = [name for name in MODEL_PACKAGES if importlib.util.find_spec(name) is not None]
    if args.profile == "no-model" and present:
        print(
            f"No-model validation requires model packages absent; found {', '.join(present)}. "
            "Use a base environment with the test group. No tests executed.",
            file=sys.stderr,
        )
        return 2
    if args.profile == "full" and len(present) != len(MODEL_PACKAGES):
        missing = sorted(set(MODEL_PACKAGES) - set(present))
        print(
            f"Full validation requires {', '.join(missing)}; "
            "run uv sync --frozen --all-extras. No tests executed.",
            file=sys.stderr,
        )
        return 2
    tools = ("pytest", "pytest_cov", "pytest_timeout", "xdist", "pytest_asyncio", "httpx2")
    missing = [name for name in tools if importlib.util.find_spec(name) is None]
    if missing:
        print(
            f"Missing test tools: {', '.join(missing)}. Sync the test group. No tests executed.",
            file=sys.stderr,
        )
        return 2
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
    except OSError as exc:
        print(f"Loopback access unavailable: {exc}. No tests executed.", file=sys.stderr)
        return 2

    env = os.environ.copy()
    for variable in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        env.setdefault(variable, "1")
    if platform.system() == "Darwin":
        env.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    workers = os.environ.get("AUTODJ_PYTEST_WORKERS", "0") if args.profile == "full" else "0"
    selected = NO_MODEL if args.profile == "no-model" else SMOKE
    options = ["--cov", "--cov-report=term"] if args.profile == "full" else ["--no-cov"]
    if args.profile != "full":
        options.extend(selected)
        for selector in selected:
            options.extend(["--require-executed", selector])
    scratch_parent = None
    if platform.system() == "Windows" and (
        ntpath.splitdrive(tempfile.gettempdir())[0].casefold()
        != ntpath.splitdrive(str(Path.cwd()))[0].casefold()
    ):
        # Synthetic rooted paths must resolve on the checkout's drive, as before.
        scratch_parent = Path.cwd() / ".tmp"
        scratch_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="autodj-pytest-", dir=scratch_parent) as folder:
        scratch = Path(folder)
        if args.profile == "full" and shutil.disk_usage(scratch).free < 2 * 1024**3 + 64 * 1024**2:
            print(
                "Full validation needs 2 GiB plus 64 MiB free scratch space. No tests executed.",
                file=sys.stderr,
            )
            return 2
        for variable in ("TMPDIR", "TEMP", "TMP"):
            env[variable] = folder
        print(
            f"Validation profile: {args.profile}; workers: {workers}; scratch: {folder}", flush=True
        )
        return subprocess.call(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "scripts.ci_pytest",
                "--tb=short",
                "--basetemp",
                str(scratch / "pytest"),
                "-n",
                workers,
                *options,
                *extra,
            ],
            env=env,
        )


if __name__ == "__main__":
    raise SystemExit(main())
