"""Check whether everything AutoDJ installs has wheels for the next Python minor.

    uv run --no-project python scripts/python_readiness.py

AutoDJ runs on one Python minor everywhere: .python-version picks it (for
example ``3.14``) for setup, the launchers and CI, and the container base
image follows it. This script asks python.org for the newest stable CPython
minor above that one. When there is none it says so and exits 0.

Otherwise it checks, without building anything from source:

- every package in uv.lock, with all extras and dependency groups, at its
  locked version, for Linux x86-64 and aarch64 (manylinux_2_28) and Windows
  x86-64. requires-python has no upper cap, so these locked versions are the
  ones a newer Python would install. Packages that the lock records with no
  wheels at all are built from source today; they are listed and not checked.
- torch, torchvision and torchaudio from each setup profile in
  scripts/setup-profiles.json and from container/torch-cpu.txt, at their
  pinned versions, on the vendor index the profile installs from, for each
  platform the profile lists. AMD device extras are checked too.

The report is plain text, one finding per line. Exit status: 0 when nothing is
missing (or there is no newer minor), 1 when something is missing, 2 when the
check itself fails. When GITHUB_OUTPUT is set, ``target`` and ``ready`` are
written to it for the workflow.

``--approve BODY_FILE`` is the workflow's second step: it ticks the box
that approves Renovate's "python minor release" update in a saved Dependency
Dashboard body, and prints approved, already-approved or not-offered.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASES_URL = (
    "https://www.python.org/api/v2/downloads/release/?is_published=true&pre_release=false"
)
#: Report label, then uv's --python-platform target.
LOCK_PLATFORMS = {
    "Linux x86-64": "x86_64-manylinux_2_28",
    "Linux aarch64": "aarch64-manylinux_2_28",
    "Windows x86-64": "x86_64-pc-windows-msvc",
}
#: Report label, then the wheel platform tags that install there.
WHEEL_PLATFORMS = {
    "Windows x86-64": re.compile(r"win_amd64"),
    "Linux x86-64": re.compile(r"(manylinux\w*|linux)_x86_64"),
    "Linux aarch64": re.compile(r"(manylinux\w*|linux)_aarch64"),
}
#: The platforms each setup profile system installs on.
PROFILE_SYSTEMS = {"Windows": ("Windows x86-64",), "Linux": ("Linux x86-64",)}
#: The branch of renovate.json's "python minor release" group.
RENOVATE_BRANCH = "renovate/python-minor-release"
CONTAINER_INDEX = "https://download.pytorch.org/whl/cpu/"
CONTAINER_PLATFORMS = ("Linux x86-64", "Linux aarch64")
UV_FAILURE = re.compile(
    r"Because (?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\{[^}]*\})?==(?P<version>\S+) "
    r"(?P<reason>has no .+?) and you require"
)

Minor = tuple[int, int]


@dataclass(frozen=True)
class Finding:
    """One line of the report."""

    ready: bool
    text: str


# --- Which Python -----------------------------------------------------------


def runtime_minor(python_version: str) -> Minor:
    """Return the minor that a .python-version file picks, such as (3, 14)."""
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", python_version.strip())
    if not match:
        raise ValueError(f".python-version {python_version.strip()!r} is not X.Y")
    return int(match[1]), int(match[2])


def newest_minor(release_names: Iterable[str], current: Minor) -> tuple[Minor, str] | None:
    """Return the newest stable minor above *current* and its newest release.

    *release_names* are python.org names such as "Python 3.15.2". Anything that
    is not a plain final X.Y.Z release is ignored.
    """
    best: tuple[int, int, int] | None = None
    for name in release_names:
        match = re.fullmatch(r"Python (\d+)\.(\d+)\.(\d+)", name.strip())
        if not match:
            continue
        version = (int(match[1]), int(match[2]), int(match[3]))
        if version[:2] > current and (best is None or version > best):
            best = version
    if best is None:
        return None
    return best[:2], ".".join(map(str, best))


def fetch_release_names() -> list[str]:
    """Return the names of published, final CPython releases from python.org."""
    with urllib.request.urlopen(RELEASES_URL, timeout=60) as response:
        releases = json.load(response)
    return [r["name"] for r in releases if r.get("is_published") and not r.get("pre_release")]


# --- Wheel tags -------------------------------------------------------------


def normalize(name: str) -> str:
    """Return a PEP 503 normalized project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_wheel(filename: str) -> tuple[str, str, list[str], list[str], list[str]] | None:
    """Return (name, version, python tags, ABI tags, platform tags) of a wheel file."""
    filename = urllib.parse.unquote(filename)
    if not filename.endswith(".whl"):
        return None
    parts = filename[: -len(".whl")].split("-")
    if len(parts) not in (5, 6):
        return None
    name, version = parts[0], parts[1]
    python, abi, platform = parts[-3:]
    return normalize(name), version, python.split("."), abi.split("."), platform.split(".")


def runs_on(python_tags: list[str], abi_tags: list[str], target: Minor) -> bool:
    """Return whether a wheel with these tags runs on the regular CPython *target*."""
    cp = f"cp{target[0]}{target[1]}"
    for python in python_tags:
        for abi in abi_tags:
            if python == cp and abi == cp:
                return True
            stable = re.fullmatch(rf"cp{target[0]}(\d+)", python)
            if abi == "abi3" and stable and int(stable[1]) <= target[1]:
                return True
            if python in (f"py{target[0]}", f"py{target[0]}{target[1]}") and abi == "none":
                return True
    return False


def wheel_versions(filenames: Iterable[str], name: str, target: Minor, platform: str) -> set[str]:
    """Return the versions of *name* that have a wheel for *target* on *platform*."""
    pattern = WHEEL_PLATFORMS[platform]
    found = set()
    for filename in filenames:
        wheel = parse_wheel(filename)
        if wheel is None or wheel[0] != normalize(name):
            continue
        _, version, pythons, abis, platforms = wheel
        fits = any(tag == "any" or pattern.fullmatch(tag) for tag in platforms)
        if fits and runs_on(pythons, abis, target):
            found.add(version)
    return found


def release_key(version: str) -> tuple[int, ...]:
    """Return the numeric release of a version for rough ordering (2.13.0+cpu -> (2, 13, 0))."""
    return tuple(int(part) for part in re.findall(r"\d+", version.partition("+")[0]))


# --- Setup profiles ---------------------------------------------------------


@functools.cache
def index_files(index: str, name: str) -> tuple[str, ...]:
    """Return the file names a PEP 503 simple index lists for *name*."""
    url = f"{index.rstrip('/')}/{normalize(name)}/"
    with urllib.request.urlopen(url, timeout=60) as response:
        page = response.read().decode("utf-8")
    hrefs = re.findall(r'href="([^"#]+)', page)
    return tuple(urllib.parse.unquote(href.rsplit("/", 1)[-1]) for href in hrefs)


def parse_spec(spec: str) -> tuple[str, list[str], str]:
    """Split "torch[device-gfx1201]==2.13.0+rocm7.14.0" into name, extras, version."""
    match = re.fullmatch(r"([A-Za-z0-9._-]+)(?:\[([^\]]*)\])?==(\S+)", spec.strip())
    if not match:
        raise ValueError(f"unexpected package spec {spec!r}")
    extras = [extra.strip() for extra in (match[2] or "").split(",") if extra.strip()]
    return match[1], extras, match[3]


def profile_checks(profiles: dict) -> list[tuple[str, str, str, list[str]]]:
    """Return (label, index, platform, specs) for every setup recipe."""
    checks = []
    for backend, profile in profiles.items():
        for system, recipe in profile.get("platforms", {}).items():
            groups = recipe.get("architectures") or {"": recipe.get("packages", [])}
            for architecture, specs in groups.items():
                label = " ".join(filter(None, (backend, system, architecture)))
                for platform in PROFILE_SYSTEMS[system]:
                    checks.append((label, recipe["index"], platform, specs))
    return checks


def container_specs(requirements: str) -> list[str]:
    """Return the torch family pins from container/torch-cpu.txt."""
    return re.findall(r"^(torch\w*==\S+)", requirements, re.MULTILINE)


@dataclass(frozen=True)
class WheelCheck:
    """Whether one pinned package has a wheel on one index for one platform."""

    package: str
    version: str
    platform: str
    index: str
    ready: bool
    #: The newest version that has a wheel, when the pinned one does not.
    newest: str = ""


def check_wheels(
    index: str,
    platform: str,
    specs: list[str],
    target: Minor,
    files: Callable[[str, str], Iterable[str]] = index_files,
) -> list[WheelCheck]:
    """Check that each pinned spec, and any AMD device package it pulls in, has a wheel."""
    checks = []
    for spec in specs:
        name, extras, version = parse_spec(spec)
        packages = [name] + [
            f"amd-{name}-{extra}" for extra in extras if extra.startswith("device-")
        ]
        for package in packages:
            versions = wheel_versions(files(index, package), package, target, platform)
            ready = version in versions
            newest = "" if ready or not versions else max(versions, key=release_key)
            checks.append(WheelCheck(package, version, platform, index, ready, newest))
    return checks


def summarize(results: Iterable[tuple[str, WheelCheck]], target: Minor) -> list[Finding]:
    """Return one report line per distinct check, naming every recipe that shares it."""
    labels: dict[WheelCheck, list[str]] = {}
    for label, check in results:
        labels.setdefault(check, [])
        if label not in labels[check]:
            labels[check].append(label)
    cp = f"cp{target[0]}{target[1]}"
    findings = []
    for check, used_by in labels.items():
        where = f"{check.package} {check.version} on {check.platform} ({', '.join(used_by)})"
        if check.ready:
            findings.append(Finding(True, f"ready: {where}"))
            continue
        hint = f"; {check.newest} has one, move the pin first" if check.newest else ""
        findings.append(Finding(False, f"missing: {where}: no {cp} wheel on {check.index}{hint}"))
    return findings


# --- Locked dependencies ----------------------------------------------------


def source_only(lock: dict) -> set[str]:
    """Return the registry packages uv.lock records without any wheel."""
    return {
        normalize(package["name"])
        for package in lock["package"]
        if "registry" in package.get("source", {}) and not package.get("wheels")
    }


def requirement_name(line: str) -> str:
    """Return the normalized name of a pinned requirement line."""
    return normalize(re.split(r"[=<>!~;\s\[]", line.strip(), maxsplit=1)[0])


def check_lock_platform(
    requirements: list[str],
    only_binary: list[str],
    target: Minor,
    label: str,
    uv_platform: str,
    compile_: Callable[[list[str], list[str], Minor, str], subprocess.CompletedProcess[str]],
) -> list[Finding]:
    """Resolve the locked pins for one platform; list every pin that has no wheel.

    uv stops at the first pin without a wheel, so drop that pin and resolve
    again until the rest resolves.
    """
    remaining = list(requirements)
    findings = []
    for _ in range(len(requirements) + 1):
        result = compile_(remaining, only_binary, target, uv_platform)
        if result.returncode == 0:
            break
        match = UV_FAILURE.search(result.stderr)
        if not match:
            first = next((line for line in result.stderr.splitlines() if line.strip()), "")
            findings.append(Finding(False, f"missing: locked dependencies on {label}: {first}"))
            return findings
        name = normalize(match["name"])
        reason = match["reason"].replace("`", "")
        findings.append(
            Finding(False, f"missing: {match['name']} {match['version']} on {label}: {reason}")
        )
        kept = [line for line in remaining if requirement_name(line) != name]
        if len(kept) == len(remaining):
            # uv blamed something that is not a pin; dropping nothing would loop.
            return findings
        remaining = kept
    if not findings:
        findings.append(Finding(True, f"ready: locked dependencies on {label}"))
    return findings


def uv_compile(
    requirements: list[str], only_binary: list[str], target: Minor, uv_platform: str
) -> subprocess.CompletedProcess[str]:
    """Run ``uv pip compile --no-deps`` over pinned requirements for one target."""
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "requirements.txt"
        path.write_text("\n".join(requirements) + "\n", encoding="utf-8")
        command = [
            "uv",
            "pip",
            "compile",
            str(path),
            "--no-deps",
            "--python-version",
            f"{target[0]}.{target[1]}",
            "--python-platform",
            uv_platform,
            *(["--only-binary", ",".join(only_binary)] if only_binary else []),
            "--no-header",
            "--no-annotate",
            "--color",
            "never",
            "--quiet",
            "--output-file",
            str(Path(scratch) / "resolved.txt"),
        ]
        return subprocess.run(command, capture_output=True, text=True, check=False)


def locked_requirements(root: Path) -> list[str]:
    """Return uv.lock as pinned requirement lines, every extra and group included."""
    command = [
        "uv",
        "export",
        "--frozen",
        "--all-extras",
        "--all-groups",
        "--no-hashes",
        "--no-emit-project",
        "--no-header",
        "--no-annotate",
        "--quiet",
    ]
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=True)
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "-"))
    ]


def check_lock(
    root: Path,
    target: Minor,
    compile_: Callable[
        [list[str], list[str], Minor, str], subprocess.CompletedProcess[str]
    ] = uv_compile,
) -> list[Finding]:
    """Check every locked package for wheels on each platform."""
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    built = source_only(lock)
    requirements = locked_requirements(root)
    only_binary = sorted({requirement_name(line) for line in requirements} - built)
    findings = [
        Finding(
            True, f"note: {name} has no wheels in uv.lock and is built from source; not checked"
        )
        for name in sorted(built)
    ]
    for label, uv_platform in LOCK_PLATFORMS.items():
        findings += check_lock_platform(
            requirements, only_binary, target, label, uv_platform, compile_
        )
    return findings


# --- Report -----------------------------------------------------------------


def check_profiles(root: Path, target: Minor) -> list[Finding]:
    """Check the setup profiles and the container's torch pins."""
    profiles = json.loads((root / "scripts" / "setup-profiles.json").read_text(encoding="utf-8"))
    recipes = profile_checks(profiles)
    container = container_specs((root / "container" / "torch-cpu.txt").read_text(encoding="utf-8"))
    recipes += [
        ("container", CONTAINER_INDEX, platform, container) for platform in CONTAINER_PLATFORMS
    ]
    results = [
        (label, check)
        for label, index, platform, specs in recipes
        for check in check_wheels(index, platform, specs, target)
    ]
    return summarize(results, target)


def write_outputs(target: str, ready: bool) -> None:
    """Pass the result to a GitHub Actions step when run there."""
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"target={target}\nready={'true' if ready else 'false'}\n")


def run(
    root: Path = ROOT,
    releases: Callable[[], list[str]] = fetch_release_names,
    lock: Callable[[Path, Minor], list[Finding]] = check_lock,
    profiles: Callable[[Path, Minor], list[Finding]] = check_profiles,
) -> int:
    """Print the readiness report and return the exit status."""
    minor = runtime_minor((root / ".python-version").read_text(encoding="utf-8"))
    current = f"{minor[0]}.{minor[1]}"
    newest = newest_minor(releases(), minor)
    if newest is None:
        print(f"No stable Python minor above {current} yet. Nothing to check.")
        write_outputs("", False)
        return 0
    target, release = newest
    name = f"{target[0]}.{target[1]}"
    print(f"Python readiness: {current} to {name} (newest release {release})")
    findings = lock(root, target) + profiles(root, target)
    for finding in findings:
        print(finding.text)
    missing = sum(not finding.ready for finding in findings)
    ready = missing == 0
    if ready:
        print(f"Result: ready, everything has wheels for Python {name}")
    else:
        print(f"Result: not ready, {missing} missing for Python {name}")
    write_outputs(name, ready)
    return 0 if ready else 1


# --- Renovate approval ------------------------------------------------------


def approve(body: str, branch: str = RENOVATE_BRANCH) -> tuple[str, str]:
    """Tick the Dependency Dashboard box that approves Renovate's Python update.

    The box is found by its approve-branch marker alone: a group's line is
    titled by the group and often names no version. The caller has already
    found the next minor ready. Return the new body and one of: "approved",
    "already-approved", or "not-offered" (no box for the branch).
    """
    marker = f"<!-- approve-branch={branch} -->"
    lines = body.splitlines(keepends=True)
    for number, line in enumerate(lines):
        if marker not in line:
            continue
        if line.lstrip().startswith("- [x]"):
            return body, "already-approved"
        if line.lstrip().startswith("- [ ]"):
            lines[number] = line.replace("- [ ]", "- [x]", 1)
            return "".join(lines), "approved"
    return body, "not-offered"


def approve_file(path: Path) -> int:
    """Approve in a saved dashboard body, print the outcome, and save any change."""
    body = path.read_text(encoding="utf-8")
    new_body, status = approve(body)
    if new_body != body:
        path.write_bytes(new_body.encode("utf-8"))
    print(status)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the check; report failures of the check itself with status 2."""
    parser = argparse.ArgumentParser(description=__doc__.partition("\n")[0])
    parser.add_argument(
        "--approve",
        metavar="BODY_FILE",
        type=Path,
        help="tick the Renovate approval box for the Python update in a saved dashboard body",
    )
    args = parser.parse_args(argv)
    try:
        if args.approve:
            return approve_file(args.approve)
        return run()
    except Exception as error:
        print(f"error: the readiness check failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
