"""The launcher's update against real git repositories, with stand-ins for AutoDJ's commands.

Each test makes an ``origin`` repository with release tags, clones it, and
runs :class:`autodj.checkout_update.CheckoutUpdater` with its real process
runner.  Backup, restore, setup and Doctor are a small script; the new
server is a stand-in whose health the test decides.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from autodj import update_contract as contract
from autodj import updates
from autodj.checkout_update import Attempt, CheckoutUpdater
from autodj.update_contract import HealthProbe

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

FAKE_AUTODJ = """
import json, sys, zipfile
words = sys.argv[1:]
if "backup" in words:
    with zipfile.ZipFile(words[-1], "w") as archive:
        archive.writestr("manifest.json", "{}")
elif "doctor" in words:
    print(json.dumps({"exit_code": 0, "checks": []}))
"""


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=60
    )
    assert done.returncode == 0, f"git {args}: {done.stderr}"
    return done.stdout.strip()


def _version(root: Path, version: str) -> None:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "autodj"\nversion = "{version}"\n', encoding="utf-8"
    )


@pytest.fixture
def repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """``origin`` with v0.21.0, v0.22.0 and a later master; a clone at v0.21.0."""
    monkeypatch.delenv(updates.CHECK_ENV, raising=False)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "master")
    for key, value in (
        ("user.email", "night-owl@example.invalid"),
        ("user.name", "Night Owl Radio"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ):
        _git(origin, "config", key, value)
    (origin / ".gitignore").write_text(".uv/\n", encoding="utf-8")
    (origin / "scripts").mkdir()
    (origin / "scripts" / "setup.py").write_text("print('setup ok')\n", encoding="utf-8")
    for version in ("0.20.0", "0.21.0", "0.22.0"):
        _version(origin, version)
        _git(origin, "add", "-A")
        _git(origin, "commit", "-q", "-m", f"release {version}")
        _git(origin, "tag", "-a", f"v{version}", "-m", version)
    _version(origin, "0.23.0")
    _git(origin, "commit", "-q", "-am", "development")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    for key, value in (
        ("user.email", "night-owl@example.invalid"),
        ("user.name", "Night Owl Radio"),
        ("commit.gpgsign", "false"),
    ):
        _git(checkout, "config", key, value)
    _git(checkout, "switch", "-q", "--detach", "v0.21.0")
    (tmp_path / "fake_autodj.py").write_text(FAKE_AUTODJ, encoding="utf-8")
    return origin, checkout


class _Server:
    def __init__(self, exits: int | None = None) -> None:
        self.code = exits

    def poll(self) -> int | None:
        return self.code


def _updater(checkout: Path, channel: str, *, healthy: bool = True, action: str = "update"):
    contract.write(
        checkout / ".uv" / "update" / contract.REQUEST,
        {
            "id": "abcdef1234567890",
            "action": action,
            "channel": channel,
            "requested_at": 1.0,
            "health": HealthProbe("http", "127.0.0.1", 9, "127.0.0.1:9").to_dict(),
        },
    )
    fake = checkout.parent / "fake_autodj.py"
    return CheckoutUpdater(
        checkout,
        [],
        lambda: ([sys.executable, str(fake)], {}),
        probe=lambda _probe: healthy,
        setup_python=sys.executable,
        monotonic=iter(range(0, 10_000, 200)).__next__,
        sleep=lambda _seconds: None,
    )


def _run(checkout: Path, channel: str, **options: object) -> tuple[Attempt, CheckoutUpdater]:
    updater = _updater(checkout, channel, **options)  # type: ignore[arg-type]
    attempt = updater.update()
    if attempt.switched:
        updater.watch(_Server(None if options.get("healthy", True) else 1), lambda: False)  # type: ignore[arg-type]
    return attempt, updater


def _result(checkout: Path) -> dict:
    found = contract.read_result(checkout / ".uv" / "update")
    assert found is not None
    return found


def test_a_release_updates_detached_even_when_another_remote_tag_moved(repos) -> None:
    """ADR-0016-14: only the target tag is fetched."""
    origin, checkout = repos
    _git(origin, "tag", "-f", "-a", "v0.20.0", "-m", "moved", "HEAD")

    attempt, _updater_used = _run(checkout, "stable")

    assert attempt.switched is True
    assert _result(checkout)["outcome"] == "updated"
    assert _git(checkout, "rev-parse", "HEAD") == _git(checkout, "rev-parse", "v0.22.0^{commit}")
    assert (
        subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=checkout, check=False).returncode
        != 0
    )  # detached


def test_a_moved_target_tag_is_named(repos) -> None:
    """ADR-0016-14: the clash over the target tag says so, and nothing changes."""
    origin, checkout = repos
    before = _git(checkout, "rev-parse", "HEAD")
    _git(origin, "tag", "-f", "-a", "v0.22.0", "-m", "moved", "HEAD")

    attempt, _updater_used = _run(checkout, "stable")

    assert attempt == Attempt()
    result = _result(checkout)
    assert result["outcome"] == "refused"
    assert "The release tag v0.22.0 on the origin remote is not the v0.22.0" in result["reason"]
    assert _git(checkout, "rev-parse", "HEAD") == before


def test_development_fast_forwards_master_and_a_rollback_resets_it(repos) -> None:
    """ADR-0016-14, ADR-0016-16: master moves forward; a failed start puts it back."""
    origin, checkout = repos
    _git(checkout, "switch", "-q", "master")
    _git(checkout, "reset", "-q", "--hard", "v0.22.0")
    old = _git(checkout, "rev-parse", "HEAD")

    attempt, updater = _run(checkout, "development", healthy=False)

    assert attempt.switched is True
    assert updater.failed_start() == Attempt()
    assert _result(checkout)["outcome"] == "rolled-back"
    assert _git(checkout, "rev-parse", "refs/heads/master") == old
    assert _git(checkout, "symbolic-ref", "--short", "HEAD") == "master"
    assert _git(checkout, "status", "--porcelain") == ""

    healthy, _updater_used = _run(checkout, "development")
    assert healthy.switched is True
    assert _git(checkout, "rev-parse", "HEAD") == _git(origin, "rev-parse", "master")


def test_a_rollback_never_resets_a_branch_someone_committed_on(repos) -> None:
    """ADR-0016-16: the guard keeps a commit made from another shell during the update."""
    _origin, checkout = repos
    _git(checkout, "switch", "-q", "master")
    _git(checkout, "reset", "-q", "--hard", "v0.22.0")
    updater = _updater(checkout, "development", healthy=False)
    assert updater.update().switched is True
    (checkout / "mine.txt").write_text("mine", encoding="utf-8")
    _git(checkout, "add", "mine.txt")
    _git(checkout, "commit", "-q", "-m", "work from another shell")
    mine = _git(checkout, "rev-parse", "HEAD")
    updater.watch(_Server(1), lambda: False)  # type: ignore[arg-type]

    assert updater.failed_start() == Attempt(failed=True)
    assert _git(checkout, "rev-parse", "refs/heads/master") == mine
    result = _result(checkout)
    assert "The branch master moved while AutoDJ was updating" in result["message"]
    assert _git(checkout, "rev-parse", "HEAD") == _git(checkout, "rev-parse", "v0.22.0^{commit}")


def test_go_back_fits_development_but_not_a_release(repos) -> None:
    """ADR-0016-20: real tags decide whether the checkout still came from that release."""
    origin, checkout = repos
    _git(checkout, "tag", "-d", "v0.22.0")
    _git(origin, "tag", "-d", "v0.22.0")
    attempt, _updater_used = _run(checkout, "development")
    assert attempt.switched is True
    record = contract.read_go_back(checkout / ".uv" / "update")
    assert record is not None
    assert record.tag == "v0.21.0"
    installation = updates.detect_installation(root=checkout, version="0.23.0")
    assert updates.go_back_moved(installation, record.commit, record.version) is None

    # At a release itself, Go back is not offered.
    _git(origin, "tag", "-a", "v0.23.0", "-m", "0.23.0", "master")
    _git(checkout, "fetch", "-q", "origin", "refs/tags/v0.23.0:refs/tags/v0.23.0")
    installation = updates.detect_installation(root=checkout, version="0.23.0")
    reason = updates.go_back_moved(installation, record.commit, record.version)
    assert reason == (
        "This checkout runs release v0.23.0 now, so going back to 0.21.0 is not offered."
    )
    # Development past that release, waiting for the next: still offered.
    _version(origin, "0.24.0")
    _git(origin, "commit", "-q", "-am", "more development")
    _git(checkout, "pull", "-q", "--ff-only", "origin", "master")
    installation = updates.detect_installation(root=checkout, version="0.24.0")
    assert installation.tag is None
    assert updates.go_back_moved(installation, record.commit, record.version) is None


def test_a_detached_commit_updates_to_the_release_that_includes_it_and_back(repos) -> None:
    """ADR-0016-14, ADR-0016-16: from a detached commit that is not a release."""
    origin, checkout = repos
    _git(checkout, "tag", "-d", "v0.21.0")  # HEAD is now a plain detached commit
    old = _git(checkout, "rev-parse", "HEAD")

    attempt, updater = _run(checkout, "stable", healthy=False)

    assert attempt.switched is True
    assert updater.failed_start() == Attempt()
    assert _git(checkout, "rev-parse", "HEAD") == old
    assert subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=checkout).returncode != 0
    healthy, _updater_used = _run(checkout, "stable")
    assert healthy.switched is True
    assert _git(checkout, "rev-parse", "HEAD") == _git(origin, "rev-parse", "v0.22.0^{commit}")


def test_a_local_master_that_diverged_from_origin_is_refused_and_keeps_its_commit(repos) -> None:
    """ADR-0016-15: never a move that would leave a local commit behind."""
    origin, checkout = repos
    _git(checkout, "switch", "-q", "master")
    _git(checkout, "reset", "-q", "--hard", "v0.22.0")
    (checkout / "local.txt").write_text("local work", encoding="utf-8")
    _git(checkout, "add", "local.txt")
    _git(checkout, "commit", "-q", "-m", "local work")
    mine = _git(checkout, "rev-parse", "HEAD")

    attempt, _updater_used = _run(checkout, "development")

    assert attempt == Attempt()
    result = _result(checkout)
    assert result["outcome"] == "refused"
    assert "is not part of master's history" in result["reason"]
    assert _git(checkout, "rev-parse", "HEAD") == mine
    assert _git(checkout, "rev-parse", "refs/heads/master") == mine
    assert _git(checkout, "status", "--porcelain") == ""
    assert _git(origin, "rev-parse", "master") != mine


def test_a_rollback_from_a_release_puts_master_back(repos) -> None:
    """The switch to development moved master; a failed start moves it back."""
    _origin, checkout = repos
    _git(checkout, "branch", "-f", "master", "v0.22.0")
    master = _git(checkout, "rev-parse", "refs/heads/master")
    release = _git(checkout, "rev-parse", "HEAD")

    attempt, updater = _run(checkout, "development", healthy=False)

    assert attempt.switched is True
    assert updater.failed_start() == Attempt()
    assert _result(checkout)["outcome"] == "rolled-back"
    assert _git(checkout, "rev-parse", "HEAD") == release
    assert subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=checkout).returncode != 0
    assert _git(checkout, "rev-parse", "refs/heads/master") == master


def test_a_rollback_deletes_a_master_the_switch_created(repos) -> None:
    """Without a local master before the update, there is none after the rollback."""
    _origin, checkout = repos
    _git(checkout, "branch", "-D", "master")

    attempt, updater = _run(checkout, "development", healthy=False)

    assert attempt.switched is True
    assert updater.failed_start() == Attempt()
    missing = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "refs/heads/master"], cwd=checkout
    )
    assert missing.returncode != 0


def test_a_rollback_keeps_a_commit_made_on_master_meanwhile(repos) -> None:
    """Master is only put back from where the update left it."""
    _origin, checkout = repos
    _git(checkout, "branch", "-f", "master", "v0.22.0")
    updater = _updater(checkout, "development", healthy=False)
    assert updater.update().switched is True
    (checkout / "mine.txt").write_text("mine", encoding="utf-8")
    _git(checkout, "add", "mine.txt")
    _git(checkout, "commit", "-q", "-m", "work from another shell")
    mine = _git(checkout, "rev-parse", "HEAD")
    updater.watch(_Server(1), lambda: False)  # type: ignore[arg-type]

    assert updater.failed_start() == Attempt()
    assert _git(checkout, "rev-parse", "refs/heads/master") == mine


@pytest.mark.parametrize(
    "channel,healthy,outcome",
    [
        ("development", True, "updated"),
        ("development", False, "rolled-back"),
        ("stable", True, "updated"),
    ],
)
def test_the_request_is_deleted_when_the_update_ends(repos, channel, healthy, outcome) -> None:
    """Whatever the outcome, request.json does not outlive its update."""
    _origin, checkout = repos
    request = checkout / ".uv" / "update" / contract.REQUEST

    attempt, updater = _run(checkout, channel, healthy=healthy)
    if attempt.switched and not healthy:
        updater.failed_start()

    assert _result(checkout)["outcome"] == outcome
    assert _result(checkout)["id"] == "abcdef1234567890"
    assert not request.exists()


def test_a_refused_request_is_deleted_too(repos) -> None:
    """A refusal before anything changed answers the request as well."""
    _origin, checkout = repos
    (checkout / "untracked.txt").write_text("not ours", encoding="utf-8")

    attempt, _updater_used = _run(checkout, "stable")

    assert attempt == Attempt()
    assert _result(checkout)["outcome"] == "refused"
    assert not (checkout / ".uv" / "update" / contract.REQUEST).exists()
