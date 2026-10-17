"""Read-only ADR/link/test-reference gate; never imports or executes test modules."""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
ID = re.compile(r"\bADR-\d{4}-\d{2}\b")
ADR_FILE = re.compile(r"(\d{4})-[a-z0-9-]+\.md\Z")
STATUSES = {"proposed", "accepted", "rejected", "deprecated", "superseded"}
METHODS = {"automated", "manual", "gap", "retired"}


def prose(text: str) -> list[tuple[int, str]]:
    """Keep authored lines, excluding fenced examples (including longer fences)."""
    result = []
    fence = ""
    for number, line in enumerate(text.splitlines(), 1):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if match:
            marker, rest = match.groups()
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not rest.strip():
                fence = ""
            continue
        if not fence:
            result.append((number, line))
    return result


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _status(value: str) -> str:
    match = re.match(r"([a-z]+)\b", value.lower())
    return match[1] if match and match[1] in STATUSES else ""


def _without_code(line: str) -> str:
    return re.sub(r"(`+).*?\1", lambda match: "\n" * match[0].count("\n"), line, flags=re.S)


def anchors(text: str) -> set[str]:
    result: set[str] = set()
    counts: Counter[str] = Counter()
    for _, line in prose(text):
        result.update(re.findall(r"<a\s+(?:id|name)=[\"\']([^\"\']+)", line))
        match = re.match(r"^ {0,3}#{1,6}\s+(.+?)(?:\s+#+)?$", line)
        if not match:
            continue
        title = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", match[1]).lower()
        slug = "".join(
            char for char in title if char in " _-" or unicodedata.category(char)[0] in "LN"
        ).replace(" ", "-")
        suffix = f"-{counts[slug]}" if counts[slug] else ""
        counts[slug] += 1
        result.add(slug + suffix)
    return result


def _destination(text: str) -> str:
    text = text.lstrip()
    if text.startswith("<"):
        return text[1:].split(">", 1)[0]
    result = []
    depth = 0
    escaped = False
    for char in text:
        if escaped:
            result.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "(":
            depth += 1
            result.append(char)
        elif char == ")" and depth:
            depth -= 1
            result.append(char)
        elif char == ")" or char.isspace():
            break
        else:
            result.append(char)
    return "".join(result)


def links(text: str) -> list[tuple[int, str]]:
    result = []
    authored = prose(text)
    clean = _without_code("\n".join(line for _, line in authored))
    for match in re.finditer(r"(?m)^ {0,3}\[[^\]]+\]:[ \t]*(.*)$", clean):
        number = authored[clean.count("\n", 0, match.start())][0]
        result.append((number, _destination(match[1])))
    for match in re.finditer(r"(?<!\\)\[(?:\\.|[^\]\\])*\]\(\s*", clean):
        number = authored[clean.count("\n", 0, match.start())][0]
        result.append((number, _destination(clean[match.end() :])))
    return result


def _tests(path: Path) -> dict[str, set[str]]:
    """Resolve native Python node IDs or annotated literal JS it/test titles."""
    text = path.read_text(encoding="utf-8")
    found = {}
    if path.suffix == ".py":

        def visit(nodes: list[ast.stmt], parents: tuple[str, ...] = ()) -> None:
            for node in nodes:
                if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                    visit(node.body, (*parents, node.name))
                elif isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and node.name.startswith("test_"):
                    found["::".join((*parents, node.name))] = set(
                        ID.findall(ast.get_docstring(node) or "")
                    )

        visit(ast.parse(text, filename=str(path)).body)
    elif path.suffix == ".js":
        # Native convention: one adjacent // ADR-... line before a literal test.
        pattern = r'(?m)^\s*(?://(?P<ids>[^\n]*)\n\s*)?(?:it|test)\(\s*(?P<quote>["\'])(?P<title>[^\n]*?)(?P=quote)\s*,'
        for match in re.finditer(pattern, text):
            title = match["title"]
            if title in found:
                raise ValueError(f"ambiguous JavaScript test title: {title}")
            found[title] = set(ID.findall(match["ids"] or ""))
    else:
        raise ValueError(
            "test selectors support Python functions/methods and literal JavaScript tests"
        )
    return found


def check(root: Path) -> tuple[list[str], list[str]]:
    """Return structural errors and explicit non-execution coverage reports."""
    root = root.resolve()
    errors = []
    reports = []
    tracked = None
    if (root / ".git").exists():
        tracked = {
            root / name
            for name in subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
            .decode("utf-8")
            .split("\0")
            if name
        }

    def included(path: Path) -> bool:
        return tracked is None or path in tracked

    def error(path: Path, line: int, code: str, message: str) -> None:
        errors.append(f"{path.relative_to(root).as_posix()}:{line} [{code}] {message}")

    def local(path: Path) -> bool:
        return path.resolve().is_relative_to(root)

    adr_dir = root / "docs/adr"
    files = sorted(p for p in adr_dir.glob("*.md") if included(p) and ADR_FILE.fullmatch(p.name))
    numbers = Counter(path.name[:4] for path in files)
    for number, count in numbers.items():
        if count > 1:
            error(adr_dir, 1, "duplicate_adr", f"ADR {number} has {count} files")
    index = adr_dir / "README.md"
    index_rows = {}
    if index.is_file():
        for line, text in prose(index.read_text(encoding="utf-8")):
            if not text.startswith("|"):
                continue
            cells = _cells(text)
            match = re.search(r"\]\((\d{4}-[a-z0-9-]+\.md)\)", cells[0])
            if match and len(cells) >= 2:
                if match[1] in index_rows:
                    error(index, line, "index_entry", f"duplicate index entry {match[1]}")
                index_rows[match[1]] = (line, _status(cells[1]))
    else:
        error(index, 1, "index_entry", "ADR index is missing")
    if not files:
        error(adr_dir, 1, "adr_files", "no numbered ADR records found")

    contracts = {}
    references: dict[str, set[str]] = {}
    for path in files:
        lines = prose(path.read_text(encoding="utf-8"))
        statuses = [
            (n, _status(s.removeprefix("Status:").strip()))
            for n, s in lines
            if s.startswith("Status:")
        ]
        status = statuses[0][1] if len(statuses) == 1 else ""
        if not status:
            error(path, 1, "adr_status", "expected one Status: with a recognized lifecycle state")
        if path.name not in index_rows:
            error(path, 1, "index_entry", "ADR is absent from the index")
        elif not index_rows[path.name][1] or index_rows[path.name][1] != status:
            error(
                index,
                index_rows[path.name][0],
                "index_status",
                f"status disagrees with {path.name}",
            )
        section = False
        seen_section = False
        rows = 0
        for line, text in lines:
            if text.startswith("## "):
                section = text == "## Current contracts"
                seen_section |= section
            if not section or not text.startswith("|"):
                continue
            cells = _cells(text)
            if not cells[0].strip("`").startswith("ADR-"):
                continue
            rows += 1
            key = cells[0].strip("`")
            if not ID.fullmatch(key) or key[4:8] != path.name[:4]:
                error(path, line, "contract_owner", f"invalid or wrong-owner constraint ID {key}")
            if key in contracts:
                error(path, line, "duplicate_contract", f"duplicate constraint {key}")
            if len(cells) != 4:
                error(
                    path, line, "contract_shape", "expected ID, Constraint, Method, Evidence cells"
                )
                continue
            _, description, method, evidence = cells
            contracts[key] = (path, line, method)
            if not description:
                error(path, line, "contract_description", f"{key} needs an observable constraint")
            if method not in METHODS:
                error(
                    path, line, "contract_method", f"{key} has unknown verification method {method}"
                )
            if not evidence:
                error(
                    path,
                    line,
                    "contract_evidence",
                    f"{key} needs test selectors, a procedure, or a gap explanation",
                )
            if method == "automated":
                selectors = re.findall(r"`([^`]+)`", evidence)
                if not selectors:
                    error(path, line, "test_reference", f"{key} needs specific test selectors")
                references[key] = set(selectors)
            else:
                reports.append(f"{method.upper()} {key}: {evidence}")
        if status in {"accepted", "proposed"} and (not seen_section or not rows):
            error(
                path, 1, "contract_section", "active/proposed ADR needs a Current contracts table"
            )

    test_cache = {}
    candidates = set()
    for key, selectors in references.items():
        path, line, _ = contracts[key]
        for selector in sorted(selectors):
            filename, separator, name = selector.partition("::")
            test_path = root / filename
            discoverable = (
                test_path.suffix == ".py"
                and (test_path.name.startswith("test_") or test_path.name.endswith("_test.py"))
            ) or (filename.startswith("tests/jsmodules/") and test_path.name.endswith(".test.js"))
            if (
                not separator
                or not filename.startswith("tests/")
                or not discoverable
                or not local(test_path)
                or not test_path.is_file()
            ):
                error(path, line, "test_reference", f"{key}: missing/invalid selector {selector}")
                continue
            candidates.add(test_path)
            try:
                if test_path not in test_cache:
                    test_cache[test_path] = _tests(test_path)
                tests = test_cache[test_path]
                if name not in tests:
                    error(path, line, "test_reference", f"{key}: test not found: {selector}")
                elif key not in tests[name]:
                    error(
                        path,
                        line,
                        "test_annotation",
                        f"{selector} must reference {key} in its docstring/adjacent JS comment",
                    )
            except (SyntaxError, ValueError) as exc:
                error(path, line, "test_reference", f"{key}: cannot inspect {selector}: {exc}")
    candidates.update(
        p
        for pattern in ("test_*.py", "*_test.py")
        for p in (root / "tests").rglob(pattern)
        if included(p) and local(p) and "ADR-" in p.read_text(encoding="utf-8")
    )
    candidates.update(
        p
        for p in (root / "tests/jsmodules").rglob("*.test.js")
        if included(p) and local(p) and "ADR-" in p.read_text(encoding="utf-8")
    )
    for path in sorted(candidates):
        try:
            tests = test_cache[path] if path in test_cache else _tests(path)
            for name, keys in tests.items():
                for key in sorted(keys):
                    selector = f"{path.relative_to(root).as_posix()}::{name}"
                    if selector not in references.get(key, set()):
                        error(
                            path,
                            1,
                            "test_annotation",
                            f"{key} has no reciprocal automated mapping for {selector}",
                        )
        except (SyntaxError, ValueError) as exc:
            error(path, 1, "test_reference", str(exc))

    documents = set(root.glob("*.md")) | set((root / "docs").rglob("*.md"))
    documents |= set((root / ".github").rglob("*.md")) | set((root / ".cursor/rules").glob("*.mdc"))
    anchor_cache = {}
    for path in sorted(p for p in documents if included(p)):
        if not local(path):
            error(path, 1, "repository_escape", "document resolves outside repository")
            continue
        for line, target in links(path.read_text(encoding="utf-8")):
            parts = urlsplit(target)
            if parts.scheme or parts.netloc:
                continue
            destination = (path.parent / unquote(parts.path) if parts.path else path).resolve()
            if not local(destination):
                error(path, line, "repository_escape", "local link resolves outside repository")
            elif not (destination.is_file() or destination.is_dir()):
                error(path, line, "local_link", f"target not found: {target}")
            elif parts.fragment and destination.is_file() and destination.suffix == ".md":
                if destination not in anchor_cache:
                    anchor_cache[destination] = anchors(destination.read_text(encoding="utf-8"))
                if unquote(parts.fragment) not in anchor_cache[destination]:
                    error(path, line, "local_anchor", f"heading not found: {target}")
    return sorted(errors), sorted(reports)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="repository root (default: this checkout)"
    )
    args = parser.parse_args()
    errors, reports = check(args.root)
    for item in reports:
        print(item)
    for item in errors:
        print("ERROR", item)
    if not errors:
        print(
            "Structure and traceability OK; this check does not execute tests or accept decisions."
        )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
