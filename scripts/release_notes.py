"""Print one release's section of CHANGELOG.md as GitHub release notes.

The release workflow publishes this text as the body of the GitHub release,
which is the page Settings, Updates links for the Stable channel.  Links
relative to the repository (``docs/setup.md``) would resolve against the
release page and break there, so they are rewritten to the files as they are
at the release's tag.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPOSITORY_URL = "https://github.com/blindndangerous/AutoDJ"
_HEADING = re.compile(r"## \[(?P<version>[^\]]+)\]")
_RELATIVE_LINK = re.compile(r"\]\((?!https?://|mailto:|#)(?P<target>[^)\s]+)\)")


def section(changelog: str, version: str) -> str:
    """The text under ``## [version]``, without its heading; raise if missing or empty."""
    lines: list[str] = []
    found = False
    for line in changelog.splitlines():
        heading = _HEADING.match(line)
        if heading is not None:
            if found:
                break
            found = heading["version"] == version
            continue
        if found:
            lines.append(line)
    text = "\n".join(lines).strip().removesuffix("---").strip()
    if not found:
        raise ValueError(f"CHANGELOG.md has no section for {version}")
    if not text:
        raise ValueError(f"CHANGELOG.md's section for {version} is empty")
    return text


def absolute_links(text: str, tag: str) -> str:
    """Point repository-relative links at the files as they are at *tag*."""
    return _RELATIVE_LINK.sub(
        lambda match: f"]({REPOSITORY_URL}/blob/{tag}/{match['target'].removeprefix('./')})",
        text,
    )


def release_notes(changelog: str, tag: str) -> str:
    """The release notes for *tag* (``vX.Y.Z``)."""
    return absolute_links(section(changelog, tag.removeprefix("v")), tag) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="the release tag, such as v0.22.0")
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    arguments = parser.parse_args(argv)
    try:
        notes = release_notes(arguments.changelog.read_text(encoding="utf-8"), arguments.tag)
    except (OSError, ValueError) as exc:
        print(f"release_notes: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
