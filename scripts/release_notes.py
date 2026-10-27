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
# A code span opens and closes with backtick runs of the same length and never
# crosses a blank line, so a lone backtick cannot hide the next paragraph's links.
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(?:(?!\n[ \t]*\n).)*?(?<!`)\1(?!`)", re.DOTALL)
# CommonMark fences: three or more backticks (with no backtick after them on
# the line) or tildes, closed by the same character at least as many times.
_FENCE_OPEN = re.compile(r" {0,3}(?P<fence>`{3,}(?=[^`]*$)|~{3,})")
_INLINE_LINK = re.compile(
    r"(?P<image>!?)\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]+)(?P<title>\s+\"[^\"]*\")?\)"
)
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:|//")


def _in_fence(lines: list[str]) -> tuple[list[bool], bool]:
    """For each line, whether it belongs to a fenced code block, its fences included.

    Also returns whether the last block is still open at the end.
    """
    marks: list[bool] = []
    fence: str | None = None
    for line in lines:
        if fence is None:
            opening = _FENCE_OPEN.match(line)
            fence = opening["fence"] if opening else None
            marks.append(fence is not None)
        else:
            marks.append(True)
            stripped = line.strip()
            if (
                len(line) - len(line.lstrip(" ")) <= 3
                and stripped.startswith(fence)
                and set(stripped) == {fence[0]}
            ):
                fence = None
    return marks, fence is not None


def section(changelog: str, version: str) -> str:
    """The text under ``## [version]``, without its heading; raise if missing or empty."""
    every = changelog.splitlines()
    marks, left_open = _in_fence(every)
    lines: list[str] = []
    found = False
    for line, fenced in zip(every, marks, strict=True):
        if not fenced:
            if found and _SECTION_END.match(line):
                break
            heading = _RELEASE_HEADING.match(line)
            if heading is not None:
                found = heading["version"] == version
                continue
        if found:
            lines.append(line)
    if _in_fence(lines)[1]:
        raise ValueError(f"CHANGELOG.md's section for {version} has an unclosed code fence")
    if not found and left_open:
        # An earlier unclosed fence hides every heading after it.
        raise ValueError(f"CHANGELOG.md has an unclosed code fence before {version}")
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
    """Point relative links at the files as they are at *tag*, outside code."""

    def link(match: re.Match[str]) -> str:
        target = _absolute(match["target"], tag, image=bool(match["image"]))
        return f"{match['image']}[{match['text']}]({target}{match['title'] or ''})"

    def prose(chunk: str) -> str:
        pieces: list[str] = []
        start = 0
        for code in _CODE_SPAN.finditer(chunk):
            pieces.append(_INLINE_LINK.sub(link, chunk[start : code.start()]))
            pieces.append(code.group())
            start = code.end()
        pieces.append(_INLINE_LINK.sub(link, chunk[start:]))
        return "".join(pieces)

    lines = text.split("\n")
    out: list[str] = []
    run: list[str] = []
    for line, fenced in zip(lines, _in_fence(lines)[0], strict=True):
        if fenced:
            if run:
                out.append(prose("\n".join(run)))
                run = []
            out.append(line)
        else:
            run.append(line)
    if run:
        out.append(prose("\n".join(run)))
    return "\n".join(out)


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
