"""Exercise the ADR gate against valid documents and deliberate contract faults."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts" / "check_architecture_docs.py"


def _write(root: Path, path: str, text: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _fixture(root: Path) -> None:
    _write(
        root,
        "docs/adr/0001-first.md",
        "# First decision\n\nStatus: Accepted; dated amendments apply.\n\n"
        "## Current contracts\n\n"
        "| ID | Constraint | Method | Evidence |\n"
        "| --- | --- | --- | --- |\n"
        "| `ADR-0001-01` | Preview leaves live state untouched. | automated | "
        "`tests/test_preview.py::test_isolated` |\n"
        "| `ADR-0001-02` | Physical output remains usable. | manual | "
        "Run on the supported sound card; record hardware and results. |\n"
        "| `ADR-0001-03` | Recovery after a real power loss. | gap | "
        "No power-loss environment evidence yet. |\n",
    )
    _write(
        root,
        "docs/adr/README.md",
        "# Decisions\n\n| ADR | Status | Scope |\n| --- | --- | --- |\n"
        "| [First](0001-first.md) | Accepted | Preview |\n",
    )
    _write(
        root,
        "tests/test_preview.py",
        'def test_isolated():\n    """ADR-0001-01: preview changes no live state."""\n'
        "    assert True\n",
    )


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(root)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


def test_valid_contracts_are_read_only_and_do_not_claim_execution(tmp_path: Path) -> None:
    """ADR-0001-01: gate integrity is not behavioral verification or acceptance."""
    _fixture(tmp_path)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = _run(tmp_path)
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    assert result.returncode == 0, result.stdout + result.stderr
    assert after == before
    assert "MANUAL ADR-0001-02" in result.stdout
    assert "GAP ADR-0001-03" in result.stdout
    assert "does not execute tests or accept decisions" in result.stdout


def test_repository_architecture_contracts_are_consistent() -> None:
    """ADR-0001-01: real ADRs and referenced tests must remain discoverable."""
    result = _run(ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("path", "before", "after", "error"),
    [
        ("docs/adr/0001-first.md", "Status: Accepted", "Status: Mystery", "adr_status"),
        ("docs/adr/README.md", "| Accepted |", "| Proposed |", "index_status"),
        ("docs/adr/README.md", "0001-first.md", "0099-lost.md", "local_link"),
        (
            "docs/adr/README.md",
            "| [First](0001-first.md) | Accepted | Preview |",
            "",
            "index_entry",
        ),
        ("docs/adr/0001-first.md", "::test_isolated", "::test_missing", "test_reference"),
        ("docs/adr/0001-first.md", "| automated |", "| psychic |", "contract_method"),
        (
            "docs/adr/0001-first.md",
            "Preview leaves live state untouched.",
            "",
            "contract_description",
        ),
        ("tests/test_preview.py", "ADR-0001-01:", "ADR-0001-99:", "test_annotation"),
        ("tests/test_preview.py", "ADR-0001-01:", "Other:", "test_annotation"),
        ("docs/adr/0001-first.md", "`ADR-0001-02`", "`ADR-0001-01`", "duplicate_contract"),
        ("docs/adr/0001-first.md", "`ADR-0001-01`", "`ADR-0002-01`", "contract_owner"),
        (
            "docs/adr/0001-first.md",
            "No power-loss environment evidence yet.",
            "",
            "contract_evidence",
        ),
        ("docs/adr/0001-first.md", "## Current contracts", "## Old notes", "contract_section"),
    ],
)
def test_invalid_contracts_fail_with_actionable_diagnostics(
    tmp_path: Path, path: str, before: str, after: str, error: str
) -> None:
    """A structural regression must fail the real gate rather than merely print a report.

    ADR-0001-01.
    """
    _fixture(tmp_path)
    target = tmp_path / path
    assert before in target.read_text()
    target.write_text(target.read_text().replace(before, after), encoding="utf-8")

    result = _run(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"[{error}]" in result.stdout


def test_duplicate_adr_numbers_are_rejected(tmp_path: Path) -> None:
    _fixture(tmp_path)
    _write(tmp_path, "docs/adr/0001-other.md", "# Other\n\nStatus: Accepted\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "[duplicate_adr]" in result.stdout


@pytest.mark.parametrize(
    ("link", "error"),
    [
        ("[missing](missing.md)", "local_link"),
        ("[missing](\nmissing.md\n)", "local_link"),
        ("[anchor](\nguide.md#absent\n)", "local_anchor"),
        ("[wrong heading](guide.md#absent)", "local_anchor"),
        ("[escape](../outside.md)", "repository_escape"),
        ("[machine](/home/another/project.md)", "repository_escape"),
        ("[reference][missing]\n\n[missing]: nowhere.md", "local_link"),
    ],
)
def test_local_link_failures_are_rejected(tmp_path: Path, link: str, error: str) -> None:
    _fixture(tmp_path)
    _write(tmp_path, "guide.md", "# Guide\n\n## Existing\n")
    _write(tmp_path, "AGENTS.md", "# Guide\n\n" + link + "\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert f"[{error}]" in result.stdout


def test_links_accept_anchors_spaces_references_and_fenced_history(tmp_path: Path) -> None:
    _fixture(tmp_path)
    _write(tmp_path, "docs/guide with space.md", "# Guide\n\n## Same\n\n## Same\n")
    _write(
        tmp_path,
        "AGENTS.md",
        "# Guide\n\n[valid](<docs/guide with space.md#same-1>)\n"
        "[reference][guide]\n\n[guide]: docs/guide%20with%20space.md#same\n\n"
        "`[literal](absent.md)`\n\n````markdown\n```\n[example](absent.md)\n"
        "Status: Bogus\n| `ADR-0001-01` | ignored history |\n```\n````\n"
        "\n`powershell -Command Get voices;\n[System.Speech.Synthesis.SpeechSynthesizer]::new()`\n",
    )
    path = tmp_path / "docs/adr/0001-first.md"
    path.write_text(path.read_text() + "\n## History\n\n```markdown\nStatus: Ancient\n```\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_proposed_contracts_remain_proposed_and_retired_ids_need_no_tests(tmp_path: Path) -> None:
    """ADR-0001-01."""
    _fixture(tmp_path)
    for rel in ("docs/adr/0001-first.md", "docs/adr/README.md"):
        path = tmp_path / rel
        path.write_text(path.read_text().replace("Accepted", "Proposed"))
    path = tmp_path / "docs/adr/0001-first.md"
    path.write_text(
        path.read_text()
        + "\n| `ADR-0001-04` | Old output boundary. | retired | Replaced after review. |\n"
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Proposed" in path.read_text()
    assert "RETIRED ADR-0001-04" in result.stdout


def test_class_methods_resolve_without_importing_test_modules(tmp_path: Path) -> None:
    """ADR-0001-01."""
    _fixture(tmp_path)
    path = tmp_path / "docs/adr/0001-first.md"
    path.write_text(path.read_text().replace("::test_isolated", "::TestPreview::test_isolated"))
    _write(
        tmp_path,
        "tests/test_preview.py",
        "raise RuntimeError('must not import tests')\n\nclass TestPreview:\n"
        '    def test_isolated(self):\n        """ADR-0001-01: isolation."""\n',
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_javascript_contracts_require_literal_tests_with_adjacent_ids(tmp_path: Path) -> None:
    _fixture(tmp_path)
    path = tmp_path / "docs/adr/0001-first.md"
    path.write_text(
        path.read_text().replace(
            "tests/test_preview.py::test_isolated",
            "tests/jsmodules/preview.test.js::leaves live state alone",
        )
    )
    (tmp_path / "tests/test_preview.py").unlink()
    _write(
        tmp_path,
        "tests/jsmodules/preview.test.js",
        '// ADR-0001-01\nit("leaves live state alone", () => { throw new Error("do not execute"); });\n',
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    js = tmp_path / "tests/jsmodules/preview.test.js"
    js.write_text(js.read_text().replace("ADR-0001-01", "ADR-0001-99"))
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "[test_annotation]" in result.stdout


@pytest.mark.parametrize("filename", ["tests/helpers.py", "tests/helpers.js"])
def test_mapped_tests_must_match_native_discovery(tmp_path: Path, filename: str) -> None:
    _fixture(tmp_path)
    path = tmp_path / "docs/adr/0001-first.md"
    path.write_text(
        path.read_text().replace(
            "tests/test_preview.py::test_isolated", filename + "::test_isolated"
        )
    )
    (tmp_path / "tests/test_preview.py").unlink()
    source = (
        'def test_isolated():\n    """ADR-0001-01."""\n'
        if filename.endswith(".py")
        else '// ADR-0001-01\nit("test_isolated", () => {});\n'
    )
    _write(tmp_path, filename, source)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "[test_reference]" in result.stdout


@pytest.mark.parametrize(
    "filename", ["tests/orphan_test.py", "tests/jsmodules/nested/orphan.test.js"]
)
def test_orphan_annotations_are_found_in_all_native_test_paths(
    tmp_path: Path, filename: str
) -> None:
    _fixture(tmp_path)
    source = (
        'def test_orphan():\n    """ADR-0001-99."""\n'
        if filename.endswith(".py")
        else '// ADR-0001-99\nit("orphan", () => {});\n'
    )
    _write(tmp_path, filename, source)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "[test_annotation]" in result.stdout
