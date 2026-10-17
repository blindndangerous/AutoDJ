"""Validate selected complete configuration examples without running shell fences."""

import importlib.util
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from autodj.config import load_config

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("page", ["network", "domain-https", "library"])
def test_documented_config_loads_without_writing_state(tmp_path: Path, page: str) -> None:
    document = ROOT / "docs" / "guides" / "operations" / f"{page}.md"
    examples = re.findall(r"^```toml\n(.*?)^```", document.read_text(), re.M | re.S)
    assert examples, f"No complete TOML example remains in {document}"
    for example in examples:
        config = tmp_path / "example.toml"
        config.write_text(example, encoding="utf-8")
        before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*")}
        load_config(config, environ={})
        after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*")}
        assert after == before, "Reading a documentation example must not create station state"


# Import by path: scripts are runnable from a checkout without installing a package.
_spec = importlib.util.spec_from_file_location(
    "documentation_examples", ROOT / "scripts/check_documentation_examples.py"
)
assert _spec and _spec.loader
examples = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(examples)


def test_complete_indented_fences_and_unclosed_fence() -> None:
    assert examples.shell_blocks("   ```bash\n   echo ok\n   ```\n", "bash") == ["echo ok\n"]
    with pytest.raises(ValueError, match="Unclosed"):
        examples.shell_blocks("```bash\necho unfinished\n", "bash")


def test_clean_shell_environment_drops_overrides_and_startup_code(
    tmp_path: Path, monkeypatch
) -> None:
    for key in (
        "AUTODJ_INDEX_DIR",
        "BASH_ENV",
        "ENV",
        "BASH_FUNC_cat%%",
        "Autodj_Model_Dir",
        "Bash_Env",
    ):
        monkeypatch.setenv(key, "untrusted")
    env = examples.clean_environment(tmp_path)
    assert not any(
        key in env
        for key in (
            "AUTODJ_INDEX_DIR",
            "BASH_ENV",
            "ENV",
            "BASH_FUNC_cat%%",
            "Autodj_Model_Dir",
            "Bash_Env",
        )
    )
    assert env["TMPDIR"] == str(tmp_path)


def test_required_missing_shell_fails(monkeypatch) -> None:
    monkeypatch.setattr(examples.shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="Required powershell tool missing"):
        examples.shell_tool("powershell")


def test_native_documentation_is_syntax_only_and_safe_fixture_is_isolated(tmp_path: Path) -> None:
    shell = "powershell" if os.name == "nt" else "bash"
    tool = shutil.which("pwsh" if shell == "powershell" else "bash")
    if not tool:
        pytest.skip(f"Native {shell} unavailable; explicit --shell lane requires its tool")
    sentinel = tmp_path / "must-not-exist"
    harmless = (
        f"[IO.File]::WriteAllText('{sentinel}', 'unexpected')\n"
        if shell == "powershell"
        else f"touch '{sentinel}'\n"
    )
    examples.check_syntax(harmless, shell, tool)
    assert not sentinel.exists()
    malformed = "if ($true) {\n" if shell == "powershell" else "if then\n"
    with pytest.raises(subprocess.CalledProcessError):
        examples.check_syntax(malformed, shell, tool)
    count = 0
    for page in examples.PAGES:
        blocks = examples.shell_blocks((ROOT / f"docs/guides/{page}.md").read_text(), shell)
        for block in blocks:
            examples.check_syntax(block, shell, tool)
            count += 1
    assert count, f"No complete {shell} examples remain"
    examples.run_safe(ROOT, shell, tool)


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_reviewed_fragment_drift_rejected_before_execution(
    tmp_path: Path, shell: str, monkeypatch
) -> None:
    page = tmp_path / "docs/guides/core-operations.md"
    page.parent.mkdir(parents=True)
    text = (ROOT / "docs/guides/core-operations.md").read_text()
    page.write_text(text.replace("[index]", "[unreviewed]"))
    fixtures = tmp_path / "tests/fixtures/documentation"
    fixtures.mkdir(parents=True)
    for source in (ROOT / "tests/fixtures/documentation").iterdir():
        (fixtures / source.name).write_bytes(source.read_bytes())
    monkeypatch.setattr(
        examples.subprocess, "run", lambda *a, **kw: pytest.fail("Executed drifted docs")
    )
    with pytest.raises(ValueError, match="fragment drifted"):
        examples.run_safe(tmp_path, shell, "unused")


def test_malformed_documented_configuration_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "invalid.toml"
    config.write_text("[index\nindex_dir = 'broken'\n")
    before = config.read_bytes()
    with pytest.raises(tomllib.TOMLDecodeError):
        load_config(config, environ={})
    assert config.read_bytes() == before
    assert list(tmp_path.iterdir()) == [config]


@pytest.mark.parametrize(
    "variant", ["equivalent", "external", "relative", "wrong-type", "extra-key", "extra-table"]
)
def test_safe_fixture_validates_paths_and_exact_configuration(
    tmp_path: Path, monkeypatch, variant: str
) -> None:
    def write_configuration(*args, cwd: Path, **kwargs) -> None:
        recovery = cwd / "autodj-recovery-test"
        recovery.mkdir()
        # Native resolution accepts dot segments; Windows also accepts case/separator aliases.
        index = str(recovery / "unused" / ".." / "index")
        if os.name == "nt":
            index = index.upper().replace("\\", "/")
        if variant == "external":
            index = str(tmp_path / "outside" / "index")
        if variant == "relative":
            index = "index"
        text = f"[index]\nindex_dir = '{index}'\nmodel_dir = '{recovery / 'models'}'\n"
        if variant == "wrong-type":
            text = text.replace(f"index_dir = '{index}'", "index_dir = 123")
        if variant == "extra-key":
            text += "unexpected = 'value'\n"
        if variant == "extra-table":
            text += "[unexpected]\nvalue = true\n"
        (recovery / "recovery.toml").write_text(text, encoding="utf-8")

    monkeypatch.setattr(examples.subprocess, "run", write_configuration)
    if variant == "equivalent":
        examples.run_safe(ROOT, "powershell", "unused")
    else:
        with pytest.raises(ValueError, match="only to isolated scratch paths"):
            examples.run_safe(ROOT, "powershell", "unused")
