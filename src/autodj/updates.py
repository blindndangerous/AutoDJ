"""Whether a newer AutoDJ exists, and whether this one can restart itself.

One module for Doctor, ``autodj check-update`` and Settings, Updates in the
web page.  It imports only the standard library, so the launcher can use
its constants without the application's dependencies.

Two channels:

- **stable** follows the release tags ``vX.Y.Z``;
- **development** follows the ``master`` branch.

An installation infers its channel from what it runs: a checkout of a
release tag is stable, a checkout of ``master`` is development.  The saved
setting (``<index_dir>/shared/updates.json``, written from the web page)
can name a channel instead, or turn the check off.  With the check off,
nothing here contacts the network.

A source checkout asks its ``origin`` remote with ``git ls-remote``: that
reads the same references ``git fetch`` would, but changes nothing in the
checkout, so Doctor keeps its promise to write no data.  The update itself
fetches.  An installation without git (a wheel, the container image) asks
GitHub's REST API.  Every network call has a bounded timeout, and a
failure is reported as "could not check", never raised.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess  # nosec B404 -- fixed git/systemctl argv lists, never a shell
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The project's GitHub repository, for installations without git.  A
#: constant, so neither a setting nor a request can point the check elsewhere.
REPOSITORY_API = "https://api.github.com/repos/blindndangerous/AutoDJ"
#: Where a person reads about a release; the notice links these.
REPOSITORY_URL = "https://github.com/blindndangerous/AutoDJ"
#: The branch the development channel follows.
DEVELOPMENT_BRANCH = "master"
CHANNELS = ("stable", "development")
#: A release tag.  Anything else is never offered, switched to or requested.
TAG_PATTERN = re.compile(r"v(\d{1,6})\.(\d{1,6})\.(\d{1,6})\Z")
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_SHORT_SHA_PATTERN = re.compile(r"[0-9a-f]{7,40}\Z")
_BRANCH_PATTERN = re.compile(r"[A-Za-z0-9._/-]{1,200}\Z")

#: The exit status that asks the launcher, a service manager or a container
#: runtime to start AutoDJ again (``EX_TEMPFAIL``).
RESTART_EXIT_CODE = 75
#: Set to ``1`` by the launcher for a ``serve`` it restarts on RESTART_EXIT_CODE.
SUPERVISOR_ENV = "AUTODJ_SUPERVISOR"
#: ``0`` turns the network check off for this process.  The tests set it, and
#: so do setup's and restore's Doctor runs.
CHECK_ENV = "AUTODJ_UPDATE_CHECK"
#: Files that only exist inside a Docker or Podman container.
CONTAINER_MARKERS = (Path("/.dockerenv"), Path("/run/.containerenv"))
#: Set by compose.yaml beside ``restart:``: the container's restart policy.  A
#: container is only trusted to come back when this names one that restarts it.
RESTART_POLICY_ENV = "AUTODJ_RESTART_POLICY"
_CONTAINER_POLICIES = frozenset({"always", "unless-stopped", "on-failure"})

GIT_TIMEOUT_SECONDS = 15.0
HTTP_TIMEOUT_SECONDS = 10.0
#: How long a successful check is reused before the page asks again.
CACHE_SECONDS = 6 * 3600.0
#: How long a failed check is reused, so an offline server does not retry
#: on every page load.
FAILED_CACHE_SECONDS = 600.0
_SETTINGS_MAX_BYTES = 4096

Runner = Callable[..., subprocess.CompletedProcess[str]]
FetchText = Callable[[str, Mapping[str, str], float], str]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpdateSettings:
    """The saved choices: whether to check, and a channel that overrides the inferred one."""

    check: bool = True
    channel: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The JSON form saved and sent to the page; ``channel`` ``None`` means automatic."""
        return {"check": self.check, "channel": self.channel}


def settings_path(index_dir: Path) -> Path:
    """Where the update settings live: shared by every station."""
    return Path(index_dir) / "shared" / "updates.json"


def parse_settings(raw: object) -> UpdateSettings:
    """Validate a decoded settings document.

    Raises:
        ValueError: The document is not an object with a boolean ``check`` and
            a ``channel`` that is ``null``, ``stable`` or ``development``.
    """
    if not isinstance(raw, dict) or set(raw) - {"check", "channel"}:
        raise ValueError("update settings must be an object with check and channel")
    check = raw.get("check", True)
    channel = raw.get("channel")
    if not isinstance(check, bool):
        raise ValueError("check must be true or false")
    if channel is not None and channel not in CHANNELS:
        raise ValueError("channel must be null, stable or development")
    return UpdateSettings(check=check, channel=channel)


def load_settings(index_dir: Path) -> UpdateSettings:
    """Read the saved settings; a missing or damaged file gives the defaults."""
    path = settings_path(index_dir)
    try:
        with path.open("rb") as handle:
            data = handle.read(_SETTINGS_MAX_BYTES + 1)
    except FileNotFoundError:
        return UpdateSettings()
    except OSError as exc:
        logger.warning("Cannot read update settings %s: %s", path, exc)
        return UpdateSettings()
    try:
        if len(data) > _SETTINGS_MAX_BYTES:
            raise ValueError("file too large")
        return parse_settings(json.loads(data.decode("utf-8")))
    except (UnicodeDecodeError, ValueError) as exc:
        logger.warning("Ignoring damaged update settings %s: %s", path, exc)
        return UpdateSettings()


def save_settings(index_dir: Path, settings: UpdateSettings) -> None:
    """Replace the saved settings atomically."""
    from autodj.fsutil import atomic_write

    atomic_write(settings_path(index_dir), json.dumps(settings.to_dict(), indent=2) + "\n")


# ---------------------------------------------------------------------------
# What this installation is
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Installation:
    """How AutoDJ is installed and which version it runs.

    ``kind`` is ``git`` (a source checkout with a working git), ``container``
    (a Docker or Podman image) or ``package`` (a wheel or tool install).
    ``commit`` is the full commit of a checkout, or the build stamp of an
    image; ``branch`` and ``tag`` describe a checkout's HEAD.
    """

    kind: str
    version: str
    commit: str | None = None
    branch: str | None = None
    tag: str | None = None
    root: Path | None = None

    @property
    def inferred_channel(self) -> str:
        """The channel this installation follows when no setting names one."""
        return "development" if self.branch == DEVELOPMENT_BRANCH else "stable"

    @property
    def short_commit(self) -> str | None:
        """The first seven digits of the commit, or ``None``."""
        return self.commit[:7] if self.commit else None

    def describe(self) -> str:
        """``0.21.0, commit 0db0a92, on master`` and the like."""
        parts = [self.version]
        if self.short_commit:
            parts.append(f"commit {self.short_commit}")
        if self.kind == "git":
            if self.tag:
                parts.append(f"release tag {self.tag}")
            elif self.branch:
                parts.append(f"on {self.branch}")
            else:
                parts.append("not on a branch")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """The JSON form for the page, without the checkout's path."""
        return {
            "kind": self.kind,
            "version": self.version,
            "commit": self.short_commit,
            "branch": self.branch,
            "tag": self.tag,
        }


def _source_root() -> Path | None:
    """This module's checkout, or ``None`` for an installed package."""
    module = Path(__file__).resolve()
    root = module.parents[2]
    if (root / "src" / "autodj" / "updates.py").resolve() == module and (
        root / "pyproject.toml"
    ).is_file():
        return root
    return None


_GIT_LOCATION = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_PREFIX",
    }
)


def _git_environment() -> dict[str, str]:
    """The environment for git: never ask for a password, never page output."""
    # A git hook's GIT_DIR and friends would point git at another repository.
    environment = {key: value for key, value in os.environ.items() if key not in _GIT_LOCATION}
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            # Git Credential Manager would otherwise open a sign-in window.
            "GCM_INTERACTIVE": "never",
            "GIT_PAGER": "cat",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    if "GIT_SSH_COMMAND" not in environment and "GIT_SSH" not in environment:
        # ssh would otherwise ask for a passphrase or a host key on the terminal.
        environment["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    return environment


def _kill_tree(process: subprocess.Popen[str]) -> None:
    """Kill *process* and everything it started.

    git runs its transport as a grandchild (``git-remote-https``, ``ssh``),
    which keeps the output pipes open after git itself is killed, so killing
    only git leaves the reader waiting for the transport's own timeout.
    """
    if sys.platform == "win32":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(  # nosec B603 B607 -- fixed argv, pid from Popen
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
    else:
        import signal

        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        process.kill()


def run_bounded(
    argv: list[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *argv* (no shell) and kill its whole process tree after *timeout* seconds.

    The child runs in its own process group (POSIX session, Windows process
    group), so a timeout kills the programs it started too.  After a
    timeout the output pipes are closed rather than read to their end: a
    survivor holding them cannot keep the caller waiting.

    Raises:
        OSError: the program could not start.
        subprocess.TimeoutExpired: it ran past *timeout*.
    """
    options: dict[str, Any] = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(  # nosec B603 -- argv list from the callers here, no shell
        argv,
        cwd=cwd,
        env=None if env is None else dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **options,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        raise subprocess.TimeoutExpired(argv, timeout) from None
    except BaseException:
        _kill_tree(process)
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def git(
    root: Path,
    *args: str,
    runner: Runner = run_bounded,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in *root* with a timeout; never through a shell.

    Raises:
        OSError: git is not installed.
        subprocess.TimeoutExpired: git ran past *timeout*.
    """
    return runner(  # nosec B603 B607 -- fixed argv list, no shell
        ["git", *args], cwd=root, timeout=timeout, env=_git_environment()
    )


def _git_value(root: Path, *args: str, runner: Runner) -> str | None:
    """The first output line of a successful git command, or ``None``."""
    try:
        done = git(root, *args, runner=runner, timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    line = done.stdout.strip().splitlines()
    return line[0].strip() if line else None


def read_head_file(git_dir: Path) -> tuple[str | None, str | None]:
    """``(branch, commit)`` from a ``.git/HEAD`` file, without running git.

    Used inside the container image, which has no git but can see the
    checkout the Compose file mounts.  Either value is ``None`` when unknown.
    """
    try:
        text = (git_dir / "HEAD").read_text(encoding="ascii", errors="strict")[:300].strip()
    except (OSError, UnicodeDecodeError):
        return None, None
    if text.startswith("ref: refs/heads/"):
        branch = text.removeprefix("ref: refs/heads/")
        return (branch if _BRANCH_PATTERN.fullmatch(branch) else None), None
    return None, (text if _SHA_PATTERN.fullmatch(text) else None)


def detect_installation(
    *,
    root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    markers: tuple[Path, ...] = CONTAINER_MARKERS,
    runner: Runner = run_bounded,
    version: str | None = None,
    working_dir: Path | None = None,
) -> Installation:
    """Find out how AutoDJ is installed; reads only local files and git."""
    from autodj.version import current_version

    environment = os.environ if environ is None else environ
    version = version or current_version()
    root = _source_root() if root is None else root
    if root is not None and (root / ".git").exists() and shutil.which("git") is not None:
        commit = _git_value(root, "rev-parse", "--verify", "HEAD^{commit}", runner=runner)
        if commit is not None and _SHA_PATTERN.fullmatch(commit):
            branch = _git_value(root, "symbolic-ref", "--quiet", "--short", "HEAD", runner=runner)
            tags = _git_value_lines(root, "tag", "--points-at", "HEAD", runner=runner)
            release_tags = sorted(
                (tag for tag in tags if TAG_PATTERN.fullmatch(tag)), key=version_key
            )
            return Installation(
                kind="git",
                version=version,
                commit=commit,
                branch=branch if branch and _BRANCH_PATTERN.fullmatch(branch) else None,
                tag=release_tags[-1] if release_tags else None,
                root=root,
            )
    stamp = (environment.get("AUTODJ_COMMIT") or "").strip().lower()
    commit = stamp if _SHORT_SHA_PATTERN.fullmatch(stamp) else None
    if any(marker.exists() for marker in markers):
        # The Compose file mounts the checkout it was built from as the
        # working directory, so its HEAD says which channel it follows.
        branch, head = read_head_file((working_dir or Path.cwd()) / ".git")
        return Installation(kind="container", version=version, commit=commit or head, branch=branch)
    return Installation(kind="package", version=version, commit=commit)


def _git_value_lines(root: Path, *args: str, runner: Runner) -> list[str]:
    """Every output line of a successful git command; empty on failure."""
    try:
        done = git(root, *args, runner=runner, timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def version_key(text: str) -> tuple[int, int, int]:
    """``v0.21.0`` or ``0.21.0`` (or ``0.22.0.dev1``) as ``(0, 21, 0)``; unknown is ``(0, 0, 0)``."""
    match = re.match(r"v?(\d{1,6})\.(\d{1,6})\.(\d{1,6})", text.strip())
    if match is None:
        return (0, 0, 0)
    major, minor, patch = (int(part) for part in match.groups())
    return (major, minor, patch)


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpdateCheck:
    """The answer to "is there a newer AutoDJ?".

    ``status`` is one of:

    - ``current``: this is the latest version on its channel;
    - ``available``: a newer one exists, named by ``latest`` (and ``target``,
      what an update would switch to: a tag, or ``master``), with
      ``notes_url`` linking its release notes;
    - ``ahead``: this is newer than the latest release, or has commits
      ``master`` does not have yet;
    - ``branch``: a checkout on a branch other than ``master``, which no
      channel follows;
    - ``unknown``: no commit to compare with ``master``;
    - ``off``: the check is turned off, so nothing was asked;
    - ``unavailable``: the check failed (offline, timed out); AutoDJ is fine.
    """

    status: str
    channel: str
    channel_source: str
    message: str
    latest: str | None = None
    target: str | None = None
    checked_at: float | None = None
    notes_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The JSON form for the page and ``check-update --json``."""
        return asdict(self)


def release_notes_url(target: str | None) -> str | None:
    """The GitHub release page of a tag, or the changelog on ``master``; else ``None``."""
    if target is not None and TAG_PATTERN.fullmatch(target):
        return f"{REPOSITORY_URL}/releases/tag/{target}"
    if target == DEVELOPMENT_BRANCH:
        return f"{REPOSITORY_URL}/blob/{DEVELOPMENT_BRANCH}/CHANGELOG.md"
    return None


def update_commands(installation: Installation, result: UpdateCheck) -> list[str]:
    """The commands that update this installation to ``result.target``, one per line.

    AutoDJ does not run them: the person who installed it does, after a
    backup, as the upgrade checklist says.  Empty when no update is
    available or the installation has no command for it (a package on
    the development channel).
    """
    target = result.target
    if result.status != "available" or target is None:
        return []
    if target == DEVELOPMENT_BRANCH:
        checkout = [
            "git fetch origin",
            f"git switch {DEVELOPMENT_BRANCH}",
            f"git merge --ff-only origin/{DEVELOPMENT_BRANCH}",
        ]
    else:
        checkout = ["git fetch --tags origin", f"git switch --detach {target}"]
    if installation.kind == "git":
        return [*checkout, "./autodj setup", "./autodj doctor --no-fix"]
    if installation.kind == "container":
        build = "AUTODJ_COMMIT=$(git rev-parse --short HEAD)"
        return [
            *checkout,
            f"{build} docker compose up -d --build",
            f"{build} podman compose up -d --build",
        ]
    if TAG_PATTERN.fullmatch(target):
        version = target.removeprefix("v")
        wheel = f"{REPOSITORY_URL}/releases/download/{target}/autodj-{version}-py3-none-any.whl"
        return [f'uv pip install "autodj[all] @ {wheel}"']
    return []


def channel_for(installation: Installation, settings: UpdateSettings) -> tuple[str, str]:
    """``(channel, source)``: the setting's channel, else the inferred one."""
    if settings.channel in CHANNELS:
        return settings.channel, "setting"
    return installation.inferred_channel, "installation"


def _fetch_text(url: str, headers: Mapping[str, str], timeout: float) -> str:
    """GET *url* over HTTPS and return its body as text (at most 1 MiB)."""
    if not url.startswith(REPOSITORY_API + "/"):
        raise ValueError("refusing an address outside the project's repository")
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        return response.read(1024 * 1024).decode("utf-8")


def _api_headers(version: str, accept: str) -> dict[str, str]:
    return {
        "Accept": accept,
        "User-Agent": f"AutoDJ/{version} (update check)",
        "X-GitHub-Api-Version": "2022-11-28",
    }


class CheckFailed(Exception):
    """The check could not finish; the message is safe to show (no URLs, no output)."""


def latest_release_tag(
    installation: Installation,
    *,
    runner: Runner = run_bounded,
    fetch_text: FetchText | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> str:
    """The newest release tag: from ``origin`` for a checkout, else GitHub's latest release.

    Raises:
        CheckFailed: The remote could not be reached or answered nothing usable.
    """
    if installation.kind == "git" and installation.root is not None:
        done = _remote(installation.root, "ls-remote", "--tags", "--refs", "origin", runner=runner)
        tags = [
            line.split("\t", 1)[1].removeprefix("refs/tags/")
            for line in done.splitlines()
            if "\t" in line
        ]
        releases = [tag for tag in tags if TAG_PATTERN.fullmatch(tag)]
        if not releases:
            raise CheckFailed("the origin remote lists no release tags")
        return max(releases, key=version_key)
    fetch_text = fetch_text or _fetch_text
    try:
        body = fetch_text(
            f"{REPOSITORY_API}/releases/latest",
            _api_headers(installation.version, "application/vnd.github+json"),
            timeout,
        )
        tag = json.loads(body).get("tag_name")
    except (OSError, ValueError, AttributeError) as exc:
        raise CheckFailed(f"GitHub did not answer ({type(exc).__name__})") from exc
    if not isinstance(tag, str) or not TAG_PATTERN.fullmatch(tag):
        raise CheckFailed("GitHub's latest release has no release tag")
    return tag


def development_head(
    installation: Installation,
    *,
    runner: Runner = run_bounded,
    fetch_text: FetchText | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> str:
    """The commit at the head of ``master``.

    Raises:
        CheckFailed: The remote could not be reached or answered nothing usable.
    """
    if installation.kind == "git" and installation.root is not None:
        done = _remote(
            installation.root,
            "ls-remote",
            "origin",
            f"refs/heads/{DEVELOPMENT_BRANCH}",
            runner=runner,
        )
        sha = done.split("\t", 1)[0].strip() if done.strip() else ""
    else:
        fetch_text = fetch_text or _fetch_text
        try:
            sha = fetch_text(
                f"{REPOSITORY_API}/commits/{DEVELOPMENT_BRANCH}",
                _api_headers(installation.version, "application/vnd.github.sha"),
                timeout,
            ).strip()
        except (OSError, ValueError) as exc:
            raise CheckFailed(f"GitHub did not answer ({type(exc).__name__})") from exc
    if not _SHA_PATTERN.fullmatch(sha):
        raise CheckFailed(f"the remote has no {DEVELOPMENT_BRANCH} branch")
    return sha


def _remote(root: Path, *args: str, runner: Runner) -> str:
    """Run a git command that reaches the remote; its output, or CheckFailed.

    git's own messages are not passed on: they can hold the remote's
    address, and an address can hold a password.
    """
    try:
        done = git(root, *args, runner=runner, timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise CheckFailed(f"git did not answer within {GIT_TIMEOUT_SECONDS:.0f} seconds") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckFailed(f"git could not run ({type(exc).__name__})") from exc
    if done.returncode != 0:
        raise CheckFailed(f"git could not reach the origin remote (exit status {done.returncode})")
    return done.stdout


def network_allowed(settings: UpdateSettings, environ: Mapping[str, str] | None = None) -> bool:
    """Whether a check may contact the network: the setting is on and not vetoed."""
    environment = os.environ if environ is None else environ
    return settings.check and environment.get(CHECK_ENV, "").strip() != "0"


def check_for_update(
    installation: Installation,
    settings: UpdateSettings,
    *,
    network: bool = True,
    environ: Mapping[str, str] | None = None,
    runner: Runner = run_bounded,
    fetch_text: FetchText | None = None,
    clock: Callable[[], float] = time.time,
) -> UpdateCheck:
    """Compare this installation with the newest version on its channel; never raises."""
    channel, source = channel_for(installation, settings)
    base = UpdateCheck(status="off", channel=channel, channel_source=source, message="")
    if not settings.check:
        return replace(
            base, message="The update check is off, so AutoDJ does not look for updates."
        )
    if not network or not network_allowed(settings, environ):
        return replace(base, message="The update check was skipped; nothing was asked.")
    if (
        installation.kind == "git"
        and installation.branch is not None
        and installation.branch != DEVELOPMENT_BRANCH
    ):
        return replace(
            base,
            status="branch",
            message=(
                f"This checkout is on branch {installation.branch}, not {DEVELOPMENT_BRANCH}. "
                "Updates follow master or a release tag; switch to one of them to get updates."
            ),
            checked_at=clock(),
        )
    try:
        if channel == "stable":
            result = _compare_release(
                installation, latest_release_tag(installation, runner=runner, fetch_text=fetch_text)
            )
        else:
            result = _compare_development(
                installation,
                development_head(installation, runner=runner, fetch_text=fetch_text),
                runner=runner,
            )
    except CheckFailed as exc:
        return replace(
            base,
            status="unavailable",
            message=f"Could not check for updates: {exc}. AutoDJ keeps working; check again later.",
            checked_at=clock(),
        )
    status, message, latest, target = result
    return replace(
        base,
        status=status,
        message=message,
        latest=latest,
        target=target,
        checked_at=clock(),
        notes_url=release_notes_url(target),
    )


def _compare_release(
    installation: Installation, latest: str
) -> tuple[str, str, str | None, str | None]:
    """Compare with the newest release tag."""
    mine, newest = version_key(installation.version), version_key(latest)
    if mine > newest:
        return "ahead", f"This is newer than the latest release, {latest}.", latest, None
    if mine == newest:
        if installation.kind == "git" and installation.tag != latest:
            return (
                "ahead",
                f"This checkout has changes after the latest release, {latest}.",
                latest,
                None,
            )
        return "current", f"This is the latest release, {latest}.", latest, None
    return "available", f"Release {latest} is available.", latest, latest


def _compare_development(
    installation: Installation, head: str, *, runner: Runner
) -> tuple[str, str, str | None, str | None]:
    """Compare with the head of ``master``."""
    short = head[:7]
    if installation.commit is None:
        return (
            "unknown",
            f"{DEVELOPMENT_BRANCH} is at {short}, but this installation does not record "
            "its commit, so AutoDJ cannot compare them.",
            short,
            None,
        )
    if head.startswith(installation.commit.lower()):
        return "current", f"This is the latest {DEVELOPMENT_BRANCH}, {short}.", short, None
    if installation.kind == "git" and installation.root is not None:
        known = _git_value(installation.root, "cat-file", "-t", head, runner=runner) == "commit"
        if known:
            try:
                ancestor = git(
                    installation.root,
                    "merge-base",
                    "--is-ancestor",
                    head,
                    "HEAD",
                    runner=runner,
                    timeout=5.0,
                ).returncode
            except (OSError, subprocess.SubprocessError):
                ancestor = 1
            if ancestor == 0:
                return (
                    "ahead",
                    f"This checkout has commits {DEVELOPMENT_BRANCH} ({short}) does not have.",
                    short,
                    None,
                )
    return (
        "available",
        f"{DEVELOPMENT_BRANCH} has new commits, up to {short}.",
        short,
        DEVELOPMENT_BRANCH,
    )


class UpdateChecker:
    """Caches one check result for the web page; Check now refreshes it.

    One check runs at a time.  A caller that arrives while one is running
    waits for it and gets its result, rather than starting another.
    """

    def __init__(
        self,
        index_dir: Callable[[], Path],
        *,
        installation: Callable[[], Installation] = detect_installation,
        check: Callable[..., UpdateCheck] = check_for_update,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """*index_dir* gives the folder whose ``shared/updates.json`` holds the settings."""
        import threading

        self._index_dir = index_dir
        self._installation = installation
        self._check = check
        self._clock = clock
        self._lock = threading.Lock()
        # Held while a check runs; a second caller waits here for its result.
        self._checking = threading.Lock()
        self._cached: tuple[float, tuple[str, bool], UpdateCheck] | None = None
        self._installed: Installation | None = None

    def installation(self, *, refresh: bool = False) -> Installation:
        """This installation; read again only when *refresh* or not read yet."""
        with self._lock:
            if self._installed is None or refresh:
                self._installed = self._installation()
            return self._installed

    def settings(self) -> UpdateSettings:
        """The saved settings."""
        return load_settings(self._index_dir())

    def save(self, settings: UpdateSettings) -> None:
        """Save new settings and forget a result made under the old ones."""
        save_settings(self._index_dir(), settings)
        with self._lock:
            self._cached = None

    def cached(self) -> UpdateCheck | None:
        """The last result for the current settings, however old, or ``None``."""
        settings = self.settings()
        key = (channel_for(self.installation(), settings)[0], settings.check)
        with self._lock:
            if self._cached is not None and self._cached[1] == key:
                return self._cached[2]
        return None

    def fresh(self) -> UpdateCheck | None:
        """The cached result when it is still fresh for the current settings; never checks."""
        settings = self.settings()
        key = (channel_for(self.installation(), settings)[0], settings.check)
        with self._lock:
            cached = self._cached
        if cached is None or cached[1] != key:
            return None
        limit = FAILED_CACHE_SECONDS if cached[2].status == "unavailable" else CACHE_SECONDS
        return cached[2] if self._clock() - cached[0] < limit else None

    def result(self, *, force: bool = False) -> UpdateCheck:
        """A fresh enough result: from the cache unless stale or *force*.

        A check that finished while this call waited for the running one
        counts as fresh, even with *force*.
        """
        if not force and (found := self.fresh()) is not None:
            return found
        asked = self._clock()
        with self._checking:
            with self._lock:
                cached = self._cached
            if cached is not None and cached[0] > asked:
                return cached[2]
            if not force and (found := self.fresh()) is not None:
                return found
            settings = self.settings()
            installation = self.installation(refresh=force)
            key = (channel_for(installation, settings)[0], settings.check)
            result = self._check(installation, settings)
            with self._lock:
                self._cached = (self._clock(), key, result)
            return result


# ---------------------------------------------------------------------------
# Restarting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RestartSupport:
    """How AutoDJ comes back after it exits with RESTART_EXIT_CODE, if it does.

    ``mode`` is ``launcher`` (the launcher's restart loop), ``systemd``
    (a service whose ``Restart=`` brings it back), ``container`` (the
    container's restart policy), or ``None``: nothing would start it again.
    """

    mode: str | None
    message: str

    @property
    def available(self) -> bool:
        """Whether the web page may offer Restart."""
        return self.mode is not None

    def to_dict(self) -> dict[str, Any]:
        """The JSON form for the page."""
        return {"available": self.available, "mode": self.mode, "message": self.message}


def _systemd_unit(cgroup_file: Path = Path("/proc/self/cgroup")) -> tuple[str, bool] | None:
    """``(unit, user)`` for the systemd service this process runs in, or ``None``."""
    try:
        lines = cgroup_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        path = line.split(":", 2)[-1]
        parts = [part for part in path.split("/") if part]
        services = [part for part in parts if part.endswith(".service")]
        if services:
            unit = services[-1]
            if re.fullmatch(r"[A-Za-z0-9:_.@\\-]{1,200}\.service", unit) is None:
                return None
            return unit, any(part.startswith("user@") for part in parts)
    return None


def systemd_restart_policy(
    environ: Mapping[str, str],
    *,
    runner: Runner = run_bounded,
    cgroup_file: Path = Path("/proc/self/cgroup"),
    pids: tuple[int, ...] | None = None,
) -> str | None:
    """The ``Restart=`` setting of the systemd service whose main process is AutoDJ.

    ``None`` when AutoDJ is not in a service, or when the service's main
    process is neither AutoDJ nor its parent (``uv run``, say): systemd
    restarts a service only when its main process exits, so another
    process in the unit exiting would leave AutoDJ off the air.
    """
    if not environ.get("INVOCATION_ID") or shutil.which("systemctl") is None:
        return None
    found = _systemd_unit(cgroup_file)
    if found is None:
        return None
    unit, user = found
    command = [
        "systemctl",
        *(["--user"] if user else []),
        "show",
        "--property=Restart",
        "--property=MainPID",
    ]
    try:
        done = runner([*command, unit], timeout=3.0)  # nosec B603 B607 -- unit name validated
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    values = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
    main = values.get("MainPID", "").strip()
    allowed = pids if pids is not None else (os.getpid(), os.getppid())
    if not main.isdigit() or int(main) not in allowed:
        return None
    return values.get("Restart", "").strip() or None


def restart_support(
    environ: Mapping[str, str] | None = None,
    *,
    markers: tuple[Path, ...] = CONTAINER_MARKERS,
    systemd_policy: Callable[[Mapping[str, str]], str | None] = systemd_restart_policy,
    platform: str = sys.platform,
) -> RestartSupport:
    """Find out whether something would start AutoDJ again after a restart exit."""
    environment = os.environ if environ is None else environ
    if environment.get(SUPERVISOR_ENV) == "1":
        return RestartSupport("launcher", "The AutoDJ launcher starts AutoDJ again.")
    if any(marker.exists() for marker in markers):
        # A container file alone says nothing about the restart policy;
        # compose.yaml says which one it set, beside `restart:`.
        declared = environment.get(RESTART_POLICY_ENV, "").strip()
        if declared in _CONTAINER_POLICIES:
            return RestartSupport(
                "container", f"The container's restart policy ({declared}) starts AutoDJ again."
            )
        return RestartSupport(
            None,
            "AutoDJ runs in a container that does not say it restarts. Give the service "
            f"restart: unless-stopped and the environment variable {RESTART_POLICY_ENV}="
            "unless-stopped, as compose.yaml does, or restart the container yourself.",
        )
    if platform.startswith("linux"):
        policy = systemd_policy(environment)
        if policy in {"always", "on-failure"}:
            return RestartSupport(
                "systemd", f"The systemd service (Restart={policy}) starts AutoDJ again."
            )
        if policy is not None:
            return RestartSupport(
                None,
                f"AutoDJ runs as a systemd service with Restart={policy}, which would not start "
                "it again. Set Restart=on-failure or always, or restart the service yourself.",
            )
    return RestartSupport(
        None,
        "AutoDJ was not started by its launcher, a service with a restart policy or a "
        "container, so it cannot start itself again. Restart it the way you started it.",
    )


def channel_label(channel: str) -> str:
    """``Stable`` or ``Development``."""
    return "Development" if channel == "development" else "Stable"


def describe_check(installation: Installation, result: UpdateCheck) -> list[str]:
    """Plain lines for the terminal: version, channel, the result, notes and commands."""
    source = "set in Settings" if result.channel_source == "setting" else "from this installation"
    lines = [
        f"Running AutoDJ {installation.describe()}.",
        f"Channel: {channel_label(result.channel)} ({source}).",
        result.message,
    ]
    if result.notes_url:
        lines.append(f"Release notes: {result.notes_url}")
    commands = update_commands(installation, result)
    if commands:
        lines.append("Make a backup, then update with:")
        lines += [f"  {command}" for command in commands]
    return lines


__all__ = [
    "CHANNELS",
    "CHECK_ENV",
    "RESTART_EXIT_CODE",
    "SUPERVISOR_ENV",
    "TAG_PATTERN",
    "Installation",
    "RestartSupport",
    "UpdateCheck",
    "UpdateChecker",
    "UpdateSettings",
    "check_for_update",
    "detect_installation",
    "load_settings",
    "restart_support",
    "save_settings",
]
