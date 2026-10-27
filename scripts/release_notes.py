"""Print one release's section of CHANGELOG.md as GitHub release notes.

The release workflow publishes this text as the body of the GitHub release,
which is the page Settings, Updates links for the Stable channel.  Links
relative to the repository (``docs/setup.md``) and to the changelog itself
(``#added``) would resolve against the release page and break there, so they
are rewritten to the files as they are at the release's tag.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPOSITORY_URL = "https://github.com/blindndangerous/AutoDJ"

#: GitHub refuses a release body over 125,000 characters.  The workflow adds
#: GitHub's list of merged pull requests after the section, so leave room.
MAX_SECTION_CHARACTERS = 100_000

_RELEASE_HEADING = re.compile(r"## \[(?P<version>[^\]]+)\]")
# Anything that ends a release's section: the next level-2 heading, or the
# link reference definitions at the bottom of the file.
_SECTION_END = re.compile(r"## |\[[^\]]+\]:\s")
_CODE_SPAN = re.compile(r"(`+).+?\1", re.DOTALL)
_INLINE_LINK = re.compile(
    r"(?P<image>!?)\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]+)(?P<title>\s+\"[^\"]*\")?\)"
)
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:|//")


def section(changelog: str, version: str) -> str:
    """The text under ``## [version]``, without its heading; raise if missing or empty."""
    lines: list[str] = []
    found = False
    for line in changelog.splitlines():
        if found and _SECTION_END.match(line):
            break
        heading = _RELEASE_HEADING.match(line)
        if heading is not None:
            found = heading["version"] == version
            continue
        if found:
            lines.append(line)
    text = "\n".join(lines).strip()
    while text.endswith("---"):
        text = text.removesuffix("---").strip()
    if not found:
        raise ValueError(f"CHANGELOG.md has no section for {version}")
    if not text:
        raise ValueError(f"CHANGELOG.md's section for {version} is empty")
    return text


def _absolute(target: str, tag: str, *, image: bool) -> str:
    """*target* as a URL that works from the release page."""
    if _SCHEME.match(target):
        return target
    if target.startswith("#"):
        return f"{REPOSITORY_URL}/blob/{tag}/CHANGELOG.md{target}"
    # /blob/ serves an HTML page; an image needs the file itself.
    kind = "raw" if image else "blob"
    return f"{REPOSITORY_URL}/{kind}/{tag}/{target.removeprefix('./').lstrip('/')}"


def absolute_links(text: str, tag: str) -> str:
    """Point relative links at the files as they are at *tag*, outside code spans."""

    def link(match: re.Match[str]) -> str:
        target = _absolute(match["target"], tag, image=bool(match["image"]))
        return f"{match['image']}[{match['text']}]({target}{match['title'] or ''})"

    pieces: list[str] = []
    start = 0
    for code in _CODE_SPAN.finditer(text):
        pieces.append(_INLINE_LINK.sub(link, text[start : code.start()]))
        pieces.append(code.group())
        start = code.end()
    pieces.append(_INLINE_LINK.sub(link, text[start:]))
    return "".join(pieces)


def release_notes(changelog: str, tag: str) -> str:
    """The release notes for *tag* (``vX.Y.Z``); raise if they cannot be published."""
    version = tag.removeprefix("v")
    notes = absolute_links(section(changelog, version), tag) + "\n"
    if len(notes) > MAX_SECTION_CHARACTERS:
        raise ValueError(
            f"CHANGELOG.md's section for {version} has {len(notes)} characters; GitHub "
            f"release notes allow about {MAX_SECTION_CHARACTERS} beside the pull request list"
        )
    return notes


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
