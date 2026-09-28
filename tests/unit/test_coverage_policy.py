"""Regression tests for the coverage-exclusion policy gate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_coverage_policy import main

ALLOWED_EXCLUSIONS = (
    "pragma: no cover",
    "raise NotImplementedError",
    "if TYPE_CHECKING:",
    "@overload",
    r"if torch.cuda.is_available\(\):",
    r"if not torch.cuda.is_available\(\):",
)


def _write_coverage_config(root: Path, exclusions: list[str], *, comment: str = "") -> None:
    rendered = ",\n    ".join(json.dumps(exclusion) for exclusion in exclusions)
    (root / "pyproject.toml").write_text(
        f"[tool.coverage.report]\nexclude_lines = [\n    {rendered}\n]\n{comment}\n",
        encoding="utf-8",
    )


def test_repository_coverage_exclusions_pass_policy() -> None:
    """The checked-in coverage configuration must remain narrowly scoped."""
    assert main() == 0


def test_configured_vulture_gate_reports_no_dead_code() -> None:
    """Configured Vulture gate must report no dead code."""
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "vulture"],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, f"Vulture found dead code:\n{output}"


def test_dj_meta_cache_exit_accepts_traceback_keyword(tmp_path: Path) -> None:
    """DjMetaCache.__exit__ must preserve context-manager keyword compatibility."""
    from autodj.dj_meta import DjMetaCache

    cache = DjMetaCache(tmp_path / "cache.db")
    try:
        cache.__exit__(exc_type=None, exc=None, traceback=None)
        assert cache._conn is None
    finally:
        cache.close()


def test_exact_coverage_exclusion_allowlist_passes(tmp_path: Path) -> None:
    _write_coverage_config(tmp_path, list(ALLOWED_EXCLUSIONS))

    assert main(tmp_path) == 0


@pytest.mark.parametrize(
    "broad_pattern",
    ["except .*Error", r"sys[.]exit", "if .*:"],
)
def test_broad_regex_variants_fail_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], broad_pattern: str
) -> None:
    _write_coverage_config(tmp_path, [*ALLOWED_EXCLUSIONS, broad_pattern])

    assert main(tmp_path) == 1
    assert capsys.readouterr().err.startswith("Broad coverage exclusions are forbidden:")


def test_missing_allowlist_entry_fails_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_coverage_config(tmp_path, list(ALLOWED_EXCLUSIONS[:-1]))

    assert main(tmp_path) == 1
    assert capsys.readouterr().err.startswith("Broad coverage exclusions are forbidden:")


@pytest.mark.parametrize(
    "rendered",
    [
        'exclude_lines = "pragma: no cover"',
        'exclude_lines = ["pragma: no cover", 7]',
    ],
)
def test_non_string_list_configuration_fails_policy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    rendered: str,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.coverage.report]\n" + rendered + "\n",
        encoding="utf-8",
    )

    assert main(tmp_path) == 1
    assert capsys.readouterr().err.startswith("Broad coverage exclusions are forbidden:")


def test_toml_comments_do_not_trigger_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Policy checks parsed exclusions rather than forbidden words in comments."""
    _write_coverage_config(
        tmp_path,
        list(ALLOWED_EXCLUSIONS),
        comment="# except Exception and sys\\.exit are forbidden examples",
    )

    assert main(tmp_path) == 0
    assert capsys.readouterr().err == ""
