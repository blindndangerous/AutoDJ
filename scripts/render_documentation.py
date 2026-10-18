"""Optional, inert CommonMark rendering; preview by default, never GitHub evidence."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

from check_architecture_docs import anchors, links

ROOT = Path(__file__).resolve().parents[1]
SCOPE = "Generic CommonMark plus tables and neutral CSS; not GitHub rendering, app execution, manual accessibility, or live tutorial evidence; external links are not fetched."
CSS = "body{margin:24px;font:16px/1.6 system-ui;color:#202124}main{max-width:980px;margin:auto;overflow-wrap:anywhere}pre,table{display:block;overflow:auto}pre{padding:12px;background:#eee}th,td{border:1px solid #aaa;padding:6px}img{max-width:100%}"


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def prepare(root: Path, base: str | None = None) -> tuple[dict, dict[str, str]]:
    from importlib.metadata import version

    from markdown_it import MarkdownIt

    names = set(git(root, "ls-files", "-z").split("\0"))
    names.update(git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"))
    names = {name for name in names if name.endswith(".md") and (root / name).is_file()}
    for name in names:
        if not (root / name).resolve().is_relative_to(root):
            raise ValueError(f"source escapes repository: {name}")
    selected = names
    if base:
        git(root, "rev-parse", "--verify", f"{base}^{{commit}}")
        changed = set(git(root, "diff", "--name-only", "-z", base, "--").split("\0"))
        changed.update(git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"))
        selected = names & changed
    if not selected:
        raise ValueError(
            "no candidate Markdown pages; select an earlier --base or omit --base for full corpus"
        )
    sources = {name: (root / name).read_text(encoding="utf-8") for name in sorted(names)}
    errors = []
    for name in sorted(selected):
        for line, href in links(sources[name]):
            url = urlsplit(href)
            if url.scheme or url.netloc:
                continue
            target = (root / name).parent / unquote(url.path) if url.path else root / name
            target = target.resolve()
            if not target.is_relative_to(root) or not target.exists():
                errors.append(f"{name}:{line}: missing/unsafe local target {href}")
            elif (
                url.fragment
                and target.suffix == ".md"
                and unquote(url.fragment) not in anchors(target.read_text(encoding="utf-8"))
            ):
                errors.append(f"{name}:{line}: missing anchor {href}")
    md = MarkdownIt("commonmark", {"html": False}).enable("table")
    rendered = {}
    for name, source in sources.items():
        tokens = md.parse(source)
        lines = source.splitlines(keepends=True)
        for token in tokens:
            if token.type == "heading_open" and token.map:
                start, end = token.map
                ids = anchors("".join(lines[:end])) - anchors("".join(lines[:start]))
                if len(ids) == 1:
                    token.attrSet("id", ids.pop())
            if token.type == "inline":
                for child in token.children or []:
                    if child.type == "link_open":
                        href = child.attrGet("href") or ""
                        url = urlsplit(href)
                        if not url.scheme and not url.netloc and url.path.endswith(".md"):
                            child.attrSet("href", url._replace(path=url.path + ".html").geturl())
        body = md.renderer.render(tokens, md.options, {})
        rendered[name + ".html"] = (
            f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src \'none\'"><title>{html.escape(name)}</title><style>{CSS}</style><main>{body}</main></html>'
        )
    report = {
        "revision": git(root, "rev-parse", "HEAD"),
        "base": base,
        "selection": "base diff plus untracked" if base else "full corpus",
        "scope": SCOPE,
        "renderer": f"markdown-it-py {version('markdown-it-py')}; raw HTML disabled",
        "viewports": [375, 1280],
        "candidates": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in sorted(selected)
        },
        "errors": errors,
    }
    report["rendered_sha256"] = {
        name: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for name, content in rendered.items()
    }
    return report, rendered


def output_path(root: Path, output: Path) -> Path:
    # Reject existing paths and symlinked parents, even when their target is external.
    output = output.absolute()
    if any(path.is_symlink() for path in (output, *output.parents)):
        raise ValueError("output must not traverse symlinks")
    if output.exists() or output.resolve().is_relative_to(root.resolve()):
        raise ValueError("output must be a new directory outside the repository")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", help="compare candidate working-tree bytes to this commit; default: full corpus"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        if args.write and not args.output:
            raise ValueError("--write requires --output")
        output = output_path(ROOT, args.output) if args.output else None
        report, rendered = prepare(ROOT, args.base)
        print(json.dumps(report, indent=2))
        if report["errors"]:
            return 1
        if args.write:
            output.mkdir(parents=True)
            for name, content in rendered.items():
                target = output / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content.encode("utf-8"))
            (output / "manifest.json").write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8"
            )
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError, ImportError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
