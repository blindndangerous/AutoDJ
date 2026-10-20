"""Validation must reject missing or skipped required evidence."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("workers", [0, 2])
@pytest.mark.parametrize(
    "outcome", ["pass", "skip", "partial", "deselect", "partial-deselect", "missing"]
)
def test_required_checks_must_execute(tmp_path, outcome, workers):
    """ADR-0001-04: incomplete required evidence cannot yield a passing gate."""
    source = tmp_path / "test_candidate.py"
    source.write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('value', [False, True])\n"
        "def test_required(value):\n"
        + (
            "    if value: pytest.skip('unavailable')\n"
            if outcome == "partial"
            else "    pytest.skip('unavailable')\n"
            if outcome == "skip"
            else "    pass\n"
        )
        + "def test_optional():\n    pytest.skip('optional platform')\n"
        + "def test_other():\n    pass\n"
    )
    required = str(source) + ("::test_missing" if outcome == "missing" else "::test_required")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "scripts.ci_pytest",
            "--require-executed",
            required,
            "-o",
            "addopts=",
            "--rootdir",
            str(tmp_path),
            "--confcutdir",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            "-n",
            str(workers),
            str(source),
            *(
                ["-k", "other"]
                if outcome == "deselect"
                else ["-k", "False"]
                if outcome == "partial-deselect"
                else []
            ),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == (0 if outcome == "pass" else 1), result.stdout + result.stderr
    if outcome != "pass":
        assert "Required checks did not execute" in result.stdout


def test_no_model_profile_refuses_installed_model_packages_before_collection(tmp_path):
    package = tmp_path / "muq"
    package.mkdir()
    (package / "__init__.py").write_text("raise RuntimeError('model was imported')\n")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "scripts/ci_pytest.py", "--profile", "no-model"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "muq" in result.stderr
    assert "No tests executed" in result.stderr
    assert "model was imported" not in result.stderr


def test_integration_mock_state_is_private_to_each_process():
    paths = []
    for _ in range(2):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from tests.integration._helpers import NO_INDEX_DIR; print(NO_INDEX_DIR)",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        paths.append(Path(result.stdout.strip()))
    assert paths[0] != paths[1]
    assert not any(path.exists() for path in paths)


@pytest.mark.parametrize("system", ["Windows", "Linux"])
def test_runner_keeps_windows_scratch_on_checkout_drive_and_preserves_unix_tmp(
    tmp_path, monkeypatch, system
):
    from scripts import ci_pytest

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(ci_pytest.platform, "system", lambda: system)
    checkout_drive = ci_pytest.ntpath.splitdrive(str(checkout))[0].casefold()
    other_drive = "Y:" if checkout_drive == "z:" else "Z:"
    monkeypatch.setattr(
        ci_pytest.tempfile,
        "gettempdir",
        lambda: other_drive + "\\system-temp" if system == "Windows" else str(inherited),
    )
    monkeypatch.setattr(sys, "argv", ["ci_pytest.py", "--profile", "smoke"])
    observed = []

    def run_pytest(command, *, env):
        scratch = Path(env["TMPDIR"])
        assert scratch.parent == (checkout / ".tmp" if system == "Windows" else inherited)
        assert scratch.is_dir()
        assert Path(command[command.index("--basetemp") + 1]).parent == scratch
        observed.append(scratch)
        return 0

    monkeypatch.setattr(ci_pytest.subprocess, "call", run_pytest)
    assert ci_pytest.main() == 0
    assert len(observed) == 1
    assert not observed[0].exists()
