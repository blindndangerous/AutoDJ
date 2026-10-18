"""Optional generic renderer safety and candidate selection checks."""

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
original_path = sys.path[:]
try:
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "render_documentation", SCRIPTS / "render_documentation.py"
    )
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
finally:
    sys.path[:] = original_path


def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "README.md").write_text("# Start\n\n[Details](detail.md#details)\n")
    (root / "detail.md").write_text("# Details\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=root,
        check=True,
    )
    return root


def test_preview_and_clean_base_are_explicit(tmp_path):
    root = repository(tmp_path)

    def working_tree_bytes():
        # Git maintenance may change internal lock files independently of the preview.
        return {
            path.relative_to(root): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(root).parts
        }

    before = working_tree_bytes()
    report, rendered = renderer.prepare(root)
    assert set(report["candidates"]) == {"README.md", "detail.md"}
    assert 'href="detail.md.html#details"' in rendered["README.md.html"]
    assert working_tree_bytes() == before
    with pytest.raises(ValueError, match="no candidate"):
        renderer.prepare(root, "HEAD")
    (root / "detail.md").write_text("# Details\n\nChanged.\n")
    assert set(renderer.prepare(root, "HEAD")[0]["candidates"]) == {"detail.md"}
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "commit",
            "-qm",
            "change",
        ],
        cwd=root,
        check=True,
    )
    assert set(renderer.prepare(root, "HEAD~1")[0]["candidates"]) == {"detail.md"}


def test_output_and_links_fail_before_writes(tmp_path):
    root = repository(tmp_path)
    for output in (root / "docs" / "generated", tmp_path):
        with pytest.raises(ValueError, match="new directory"):
            renderer.output_path(root, output)
    (root / "detail.md").write_text("# Details\n\n[Missing](gone.md#nope)\n")
    assert "missing/unsafe" in renderer.prepare(root)[0]["errors"][0]


def test_output_refuses_symlinked_parents(tmp_path):
    root = repository(tmp_path)
    try:
        (tmp_path / "shortcut").symlink_to(tmp_path, target_is_directory=True)
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(ValueError, match="symlinks"):
        renderer.output_path(root, tmp_path / "shortcut" / "render")


def test_malformed_cli_has_no_output(tmp_path):
    output = tmp_path / "render"
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_documentation.py"), "--write"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "requires --output" in result.stderr
    assert not output.exists()


def test_written_render_matches_manifest_bytes_with_windows_newlines(tmp_path, monkeypatch):
    root = repository(tmp_path)
    (root / "detail.md").write_text("# Details\n\nCafé 日本語.\n", encoding="utf-8")
    output = tmp_path / "render"
    write_text = Path.write_text

    def windows_write_text(path, content, **kwargs):
        return write_text(path, content, **(kwargs | {"newline": "\r\n"}))

    # Model Windows text writes even when this regression runs on POSIX.
    monkeypatch.setattr(Path, "write_text", windows_write_text)
    monkeypatch.setattr(renderer, "ROOT", root)
    monkeypatch.setattr(sys, "argv", ["render_documentation", "--write", "--output", str(output)])
    assert renderer.main() == 0
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["rendered_sha256"].items():
        content = (output / name).read_bytes()
        assert hashlib.sha256(content).hexdigest() == digest
        assert b"\r\n" not in content
    assert "Café 日本語.".encode() in (output / "detail.md.html").read_bytes()
