"""Regression tests for the coverage-exclusion policy gate."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from scripts.check_coverage_policy import main

ALLOWED_EXCLUSIONS = (
    "pragma: no cover",
    "raise NotImplementedError",
    "if TYPE_CHECKING:",
    "@overload",
    r"if torch.cuda.is_available\(\):",
    r"if not torch.cuda.is_available\(\):",
)
EXPECTED_STARLETTE_VERSION = "1.6.0"


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


def _assert_starlette_dependency_contract(root: Path, project_text: str) -> None:
    import_locations: list[str] = []
    for module_path in sorted((root / "src").rglob("*.py")):
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        for node in ast.walk(tree):
            imported_modules: list[str] = []
            if isinstance(node, ast.Import):
                imported_modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules = [node.module]
            if any(module.partition(".")[0] == "starlette" for module in imported_modules):
                relative_path = module_path.relative_to(root).as_posix()
                import_locations.append(f"{relative_path}:{node.lineno}")

    assert import_locations, "Expected production source to import Starlette directly"

    project = tomllib.loads(project_text)
    requirements = [Requirement(item) for item in project["project"]["dependencies"]]
    matches = [req for req in requirements if canonicalize_name(req.name) == "starlette"]
    assert matches, "Direct Starlette imports must declare a direct dependency: " + ", ".join(
        import_locations
    )
    assert len(matches) == 1, "Starlette must have one canonical direct declaration"
    requirement = matches[0]
    assert requirement.marker is None, "Starlette direct dependency must be unconditional"
    assert not requirement.extras, "Starlette direct dependency must not request extras"

    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    locked_versions = [
        package["version"]
        for package in lock["package"]
        if canonicalize_name(package["name"]) == "starlette"
    ]
    assert locked_versions == [EXPECTED_STARLETTE_VERSION]
    assert str(requirement.specifier) == f"=={EXPECTED_STARLETTE_VERSION}"


def test_starlette_imports_have_exact_direct_dependency() -> None:
    """Direct Starlette imports must match the exact locked dependency."""
    root = Path(__file__).resolve().parents[2]
    project_text = (root / "pyproject.toml").read_text(encoding="utf-8")

    _assert_starlette_dependency_contract(root, project_text)


def test_starlette_dependency_contract_rejects_missing_declaration() -> None:
    """Removing the direct declaration must break the dependency contract."""
    root = Path(__file__).resolve().parents[2]
    project_text = (root / "pyproject.toml").read_text(encoding="utf-8")
    mutated_text = project_text.replace(
        f'    "starlette=={EXPECTED_STARLETTE_VERSION}",\n',
        "",
    )
    assert mutated_text != project_text

    with pytest.raises(AssertionError, match="must declare a direct dependency"):
        _assert_starlette_dependency_contract(root, mutated_text)


def test_starlette_dependency_contract_rejects_conditional_declaration() -> None:
    """An impossible environment marker must not satisfy the direct dependency."""
    root = Path(__file__).resolve().parents[2]
    project_text = (root / "pyproject.toml").read_text(encoding="utf-8")
    mutated_text = project_text.replace(
        f'"starlette=={EXPECTED_STARLETTE_VERSION}"',
        f"\"starlette=={EXPECTED_STARLETTE_VERSION}; python_version < '3.0'\"",
    )
    assert mutated_text != project_text

    with pytest.raises(AssertionError, match="must be unconditional"):
        _assert_starlette_dependency_contract(root, mutated_text)


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
