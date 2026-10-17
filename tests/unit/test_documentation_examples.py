"""Validate selected complete configuration examples without running shell fences."""

import re
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
