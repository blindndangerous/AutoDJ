"""The launcher's half of Update: updating a source checkout between two runs of serve."""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from autodj import checkout_update, launcher, updates
from autodj import update_contract as contract
from autodj.checkout_update import CheckoutUpdater
from autodj.update_contract import HealthProbe
from autodj.updates import Installation, UpdateCheck, UpdateSettings

OLD = "1" * 40
NEW = "2" * 40
MASTER = "3" * 40
BASE = ["py", "-m", "autodj"]
PROBE = HealthProbe("http", "127.0.0.1", 8080, "127.0.0.1:8080")


def _pyproject(version: str) -> str:
    return f'[project]\nname = "autodj"\nversion = "{version}"\n'


class FakeCheckout:
    """A checkout that answers git, backup, restore, setup and Doctor like the real ones."""

    def __init__(
        self,
        root: Path,
        *,
        version: str = "0.21.0",
        branch: str | None = None,
        tag: str | None = "v0.21.0",
        dirty: str = "",
        master_version: str = "0.23.0",
        doctor: dict[str, Any] | None = None,
        fail: set[str] | None = None,
        head: str = OLD,
    ) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        (root / "scripts").mkdir(exist_ok=True)
        (root / "scripts" / "setup.py").write_text("", encoding="utf-8")
        (root / ".uv").mkdir(exist_ok=True)
        (root / ".uv" / "setup.json").write_text('{"backend": "cpu"}', encoding="utf-8")
        self.versions = {OLD: version, NEW: "0.22.0", MASTER: master_version}
        self.tags = {"v0.21.0": OLD, "v0.22.0": NEW}
        self.dirty = dirty
        self.doctor = doctor if doctor is not None else {"exit_code": 0, "checks": []}
        self.fail = fail or set()
        self.calls: list[list[str]] = []
        self.on: dict[str, Callable[[], None]] = {}
        #: The release tags ``git tag --merged HEAD`` lists.
        self.merged: list[str] = []
        #: Where each local branch points, and the options of every command.
        self.tips: dict[str, str] = {}
        self.options: list[dict[str, Any]] = []
        self._move(head, branch, tag)

    def installation(self) -> Installation:
        return Installation(
            kind="git",
            version=self.versions[self.head],
            commit=self.head,
            branch=self.branch,
            tag=self.tag,
            root=self.root,
        )

    def _move(self, commit: str, branch: str | None, tag: str | None = None) -> None:
        self.head, self.branch, self.tag = commit, branch, tag
        if branch:
            self.tips[branch] = commit
        (self.root / "pyproject.toml").write_text(_pyproject(self.versions[commit]), "utf-8")

    def run(self, argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        self.options.append(options)
        name = self._name(argv)
        if name in self.on:
            self.on[name]()
        if name in self.fail:
            return subprocess.CompletedProcess(argv, 1, "", "")
        if argv[0] == "git":
            return self._git(argv, argv[1:])
        if name == "backup":
            with zipfile.ZipFile(argv[-1], "w") as archive:
                archive.writestr("manifest.json", "{}")
        if name == "doctor":
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.doctor), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    @staticmethod
    def _name(argv: list[str]) -> str:
        if argv[0] == "git":
            return "git " + argv[1]
        if argv[0] == "setup-py":
            return "setup"
        return next(word for word in argv[3:] if not word.startswith("-") and "." not in word)

    def _git(self, argv: list[str], args: list[str]) -> subprocess.CompletedProcess[str]:
        def ok(stdout: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, code, stdout, "")

        if args[:2] == ["config", "--get"]:
            return ok(code=1)
        if args == ["status", "--porcelain"]:
            return ok(self.dirty)
        if args[0] == "fetch":
            if "fetch-clobber" in self.fail:
                return subprocess.CompletedProcess(
                    argv, 1, "", " ! [rejected] v0.22.0 -> v0.22.0  (would clobber existing tag)"
                )
            return ok()
        if args[:3] == ["rev-parse", "--verify", "--quiet"]:
            ref = args[3]
            if ref.startswith("refs/heads/"):
                tip = self.tips.get(ref.removeprefix("refs/heads/"))
                return ok(f"{tip}\n") if tip else ok(code=1)
            found = {f"refs/tags/{tag}^{{commit}}": sha for tag, sha in self.tags.items()}.get(ref)
            if ref.startswith("refs/remotes/origin/master"):
                found = MASTER
            return ok(f"{found}\n") if found else ok(code=1)
        if args == ["rev-parse", "--verify", "HEAD^{commit}"]:
            return ok(f"{self.head}\n")
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return ok(code=1 if "not-ancestor" in self.fail else 0)
        if args[:2] == ["tag", "--merged"]:
            return ok("\n".join(self.merged))
        if args[0] == "show":
            return ok(_pyproject(self.versions[MASTER]))
        if args[0] == "switch":
            where = [word for word in args[1:] if word != "--discard-changes"]
            if where[0] == "--detach":
                target = where[1]
                commit = self.tags.get(target, target)
                if commit != OLD and "switch-new" in self.fail:
                    return ok(code=1)
                if commit == OLD and "restore-checkout" in self.fail:
                    return ok(code=1)
                self._move(commit, None, target if target.startswith("v") else None)
                return ok()
            if where[0] == "--force-create":
                if "restore-checkout" in self.fail:
                    return ok(code=1)
                self._move(where[2], where[1])
                return ok()
            self._move(self.head, where[0])
            return ok()
        if args[0] == "merge":
            self._move(MASTER, "master")
            return ok()
        raise AssertionError(f"unexpected git {args}")

    def names(self) -> list[str]:
        return [self._name(call) for call in self.calls]


def _available(target: str = "v0.22.0", channel: str = "stable") -> UpdateCheck:
    return UpdateCheck(
        status="available",
        channel=channel,
        channel_source="installation",
        message="An update is available.",
        latest=target,
        target=target,
    )


def _folder(checkout: FakeCheckout) -> Path:
    return checkout.root / checkout_update.FOLDER


def _ask(checkout: FakeCheckout, action: str = "update", channel: str = "stable") -> None:
    contract.write(
        _folder(checkout) / contract.REQUEST,
        {
            "id": "abcdef12",
            "action": action,
            "channel": channel,
            "requested_at": 1_000.0,
            "health": PROBE.to_dict(),
            # Never trusted: the launcher works out the target itself.
            "target": "v9.9.9",
        },
    )


def _updater(
    checkout: FakeCheckout,
    result: UpdateCheck | None = None,
    *,
    channel: str = "stable",
    action: str = "update",
    probe: Callable[[HealthProbe], bool] = lambda _probe: True,
    monotonic: Callable[[], float] | None = None,
    ask: bool = True,
) -> CheckoutUpdater:
    if ask:
        _ask(checkout, action, channel)
    seen: list[UpdateSettings] = []

    def check(_installation: Installation, settings: UpdateSettings) -> UpdateCheck:
        seen.append(settings)
        return result or _available(channel=channel)

    updater = CheckoutUpdater(
        checkout.root,
        ["--config", "night owl.toml"],
        lambda: (list(BASE), {"A": "1"}),
        run=checkout.run,
        installation=checkout.installation,
        check=check,
        probe=probe,
        clock=lambda: 1_793_000_000.0,
        monotonic=monotonic or (lambda: 0.0),
        sleep=lambda _seconds: None,
        setup_python="setup-py",
        windows=False,
    )
    updater.checked = seen  # type: ignore[attr-defined]
    return updater


def _result(checkout: FakeCheckout) -> dict[str, Any]:
    found = contract.read_result(_folder(checkout))
    assert found is not None
    return found


def _steps(record: dict[str, Any]) -> dict[str, str]:
    return {step["name"]: step["status"] for step in record["steps"]}


class _Server:
    """A started server stand-in: still running until told otherwise."""

    def __init__(self, exits: int | None = None) -> None:
        self.code = exits
        self.signals: list[int] = []

    def poll(self) -> int | None:
        return self.code

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)
        self.code = -signum

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def wait(self, timeout: float | None = None) -> int:
        return self.code or 0

    def kill(self) -> None:
        self.code = -9


# ---------------------------------------------------------------------------
# The update itself
# ---------------------------------------------------------------------------


def test_a_stable_update_follows_the_upgrade_checklist(tmp_path: Path) -> None:
    """ADR-0016-14: check, clean tree, backup, fetch tags, switch, setup, Doctor; then healthy."""
    checkout = FakeCheckout(tmp_path / "checkout")
    updater = _updater(checkout)

    attempt = updater.update()

    assert attempt == checkout_update.Attempt(switched=True)
    assert checkout.names() == [
        "git status",
        "backup",
        "git config",  # the checkout's own ssh command, told not to ask
        "git fetch",
        "git rev-parse",
        "git switch",
        "git rev-parse",
        "setup",
        "doctor",
    ]
    backup = checkout.calls[1]
    assert backup[:6] == [*BASE, "--config", "night owl.toml", "backup"]
    assert Path(backup[6]).parent == _folder(checkout) / contract.BACKUPS
    # Only the target tag: another tag moved on the remote cannot stop it.
    assert [
        "git",
        "fetch",
        "--no-tags",
        "origin",
        "refs/tags/v0.22.0:refs/tags/v0.22.0",
    ] in checkout.calls
    assert ["git", "switch", "--detach", "v0.22.0"] in checkout.calls
    assert checkout.calls[7] == ["setup-py", str(checkout.root / "scripts" / "setup.py"), "--yes"]
    assert checkout.calls[8] == [
        *BASE,
        "--config",
        "night owl.toml",
        "doctor",
        "--no-fix",
        "--json",
        "--offline",
    ]
    assert updater.checked == [UpdateSettings(check=True, channel="stable")]  # type: ignore[attr-defined]
    state = contract.read(_folder(checkout) / contract.STATE)
    assert state is not None and state["step"] == "start"
    assert updater.locked

    updater.watch(_Server(), lambda: False)  # type: ignore[arg-type]

    assert updater.started_ok is True
    assert updater.pending is False
    result = _result(checkout)
    assert result["outcome"] == "updated"
    assert result["id"] == "abcdef12"
    assert result["action"] == "update"
    assert result["message"] == "Updated to 0.22.0."
    assert result["from"]["version"] == "0.21.0"
    assert result["to"] == {"version": "0.22.0", "commit": NEW, "label": "0.22.0"}
    assert result["commands"] == []
    assert _steps(result) == {
        "check": "done",
        "backup": "done",
        "fetch": "done",
        "switch": "done",
        "setup": "done",
        "restore": "skipped",
        "migration-check": "done",
        "start": "done",
        "health": "done",
        "rollback": "skipped",
    }
    assert not updater.locked
    # Not from a release to development, so there is nothing to go back to.
    assert contract.read_go_back(_folder(checkout)) is None


def test_a_development_update_fast_forwards_master(tmp_path: Path) -> None:
    """ADR-0016-14: development fetches master, switches to it and fast-forwards."""
    checkout = FakeCheckout(tmp_path / "checkout")
    updater = _updater(checkout, _available("master", "development"), channel="development")

    assert updater.update().switched is True
    assert ["git", "fetch", "origin", "master"] in checkout.calls
    assert ["git", "switch", "master"] in checkout.calls
    assert ["git", "merge", "--ff-only", "origin/master"] in checkout.calls
    assert checkout.head == MASTER
    updater.watch(_Server(), lambda: False)  # type: ignore[arg-type]
    assert _result(checkout)["message"] == f"Updated to 0.23.0, commit {MASTER[:7]} on master."


def test_a_checkout_with_changes_is_refused_before_anything_changes(tmp_path: Path) -> None:
    """ADR-0016-14: any git status output refuses, with the commands that put the changes aside."""
    checkout = FakeCheckout(tmp_path / "checkout", dirty="?? notes.txt\n")
    updater = _updater(checkout)

    assert updater.update() == checkout_update.Attempt()
    assert checkout.names() == ["git status"]
    result = _result(checkout)
    assert result["outcome"] == "refused"
    assert result["message"].startswith("AutoDJ did not update: The AutoDJ folder has changes")
    assert result["commands"] == [
        "git status --short",
        'git stash push --include-untracked -m "Before updating AutoDJ"',
    ]
    assert _steps(result)["check"] == "failed"
    assert _steps(result)["backup"] == "skipped"
    assert not updater.locked


def test_a_needed_migration_rolls_back_and_gives_the_commands(tmp_path: Path) -> None:
    """ADR-0016-14, ADR-0016-16: Doctor's migration finding is never migrated automatically."""
    doctor = {
        "exit_code": 1,
        "checks": [
            {
                "name": "index",
                "status": "fail",
                "summary": "library migration needed",
                "repair": "migrate-index",
            },
        ],
    }
    checkout = FakeCheckout(tmp_path / "checkout", doctor=doctor)
    updater = _updater(checkout)

    assert updater.update() == checkout_update.Attempt()
    assert checkout.head == OLD
    assert ["git", "switch", "--detach", OLD] in checkout.calls
    assert checkout.names().count("setup") == 2  # the new version's, then the old one's again
    assert "migrate" not in " ".join(" ".join(call) for call in checkout.calls)
    result = _result(checkout)
    assert result["outcome"] == "rolled-back"
    assert result["message"].startswith(
        "The update failed, so AutoDJ went back to 0.21.0. The new version needs a library migration"
    )
    assert result["commands"] == [
        "git switch --detach v0.22.0",
        "./autodj --config 'night owl.toml' setup",
        "./autodj --config 'night owl.toml' migrate-index",
        "./autodj --config 'night owl.toml' migrate-index --apply",
        "./autodj --config 'night owl.toml' doctor --no-fix",
    ]
    assert _steps(result)["migration-check"] == "failed"
    assert _steps(result)["rollback"] == "done"


@pytest.mark.parametrize("how", ["exits", "never-answers"])
def test_a_new_version_that_is_not_healthy_is_rolled_back(tmp_path: Path, how: str) -> None:
    """ADR-0016-13, ADR-0016-16: healthy means still running and /healthz answering in time.

    The new version ran, so the data goes back to the backup from just before.
    """
    checkout = FakeCheckout(tmp_path / "checkout", branch="master", tag=None, version="0.20.0")
    now = [0.0]

    def later() -> float:
        now[0] += 30.0
        return now[0]

    updater = _updater(checkout, probe=lambda _probe: False, monotonic=later)
    assert updater.update().switched is True
    server = _Server(exits=1 if how == "exits" else None)

    updater.watch(server, lambda: False)  # type: ignore[arg-type]

    assert updater.started_ok is None
    if how == "never-answers":
        assert server.signals == [signal.SIGTERM]
    attempt = updater.failed_start()
    assert attempt == checkout_update.Attempt()
    assert checkout.head == OLD
    assert checkout.branch == "master"
    assert ["git", "switch", "--force-create", "master", OLD] in checkout.calls
    # After the checkout and the old runtime are back: the old version restores its backup.
    assert checkout.names()[-3:] == ["git rev-parse", "setup", "restore"]
    assert checkout.calls[-1] == [
        *BASE,
        "--config",
        "night owl.toml",
        "restore",
        "--force",
        str(updater._backup),
    ]
    result = _result(checkout)
    assert result["outcome"] == "rolled-back"
    expected = (
        "The new version stopped with exit status 1 before it was ready."
        if how == "exits"
        else "The new version did not answer within 180 seconds."
    )
    assert result["message"] == (
        f"The update failed, so AutoDJ went back to 0.20.0, commit {OLD[:7]} on master. {expected}"
        " The data is back as it was before the update; changes the new version made while it"
        " ran are gone."
    )
    assert result["commands"] == []
    assert _steps(result)["health"] == "failed"


def test_a_rollback_before_the_start_leaves_the_data_alone(tmp_path: Path) -> None:
    """ADR-0016-16: the new version never ran, so nothing restores the backup."""
    checkout = FakeCheckout(tmp_path / "checkout", fail={"doctor"})
    updater = _updater(checkout)

    assert updater.update() == checkout_update.Attempt()
    assert "restore" not in checkout.names()
    assert "data is back" not in _result(checkout)["message"]


def test_a_restore_that_fails_after_the_start_reports_failed_with_the_commands(
    tmp_path: Path,
) -> None:
    """ADR-0016-16: the checkout is back, the data may not be; nothing starts."""
    checkout = FakeCheckout(tmp_path / "checkout", fail={"restore"})
    updater = _updater(checkout, probe=lambda _probe: False)
    assert updater.update().switched is True
    updater.watch(_Server(exits=1), lambda: False)  # type: ignore[arg-type]

    assert updater.failed_start() == checkout_update.Attempt(failed=True)
    assert checkout.head == OLD
    result = _result(checkout)
    assert result["outcome"] == "failed"
    assert f"Restoring {updater._backup} failed (exit status 1)." in result["message"]
    assert result["commands"] == [
        "git status --short",
        f"git switch --detach {OLD}",
        "./autodj --config 'night owl.toml' setup",
        f"./autodj --config 'night owl.toml' restore --force {shlex.quote(str(updater._backup))}",
        "./autodj --config 'night owl.toml' doctor --no-fix",
    ]


def test_a_rollback_that_fails_says_where_the_checkout_is_and_starts_nothing(
    tmp_path: Path,
) -> None:
    """ADR-0016-16."""
    checkout = FakeCheckout(tmp_path / "checkout", fail={"setup", "restore-checkout"})
    updater = _updater(checkout)

    attempt = updater.update()

    assert attempt == checkout_update.Attempt(failed=True)
    assert checkout.head == NEW
    result = _result(checkout)
    assert result["outcome"] == "failed"
    assert "could not go back to 0.21.0" in result["message"]
    assert f"The checkout is at {NEW[:7]}." in result["message"]
    assert "A backup from before the update is at" in result["message"]
    assert result["commands"] == [
        "git status --short",
        f"git switch --detach {OLD}",
        "./autodj --config 'night owl.toml' setup",
        "./autodj --config 'night owl.toml' doctor --no-fix",
    ]


@pytest.mark.parametrize(
    ("result", "channel", "fail", "words"),
    [
        (
            UpdateCheck(
                "ahead", "stable", "setting", "This is newer than the latest release, v0.20.0."
            ),
            "stable",
            set(),
            "This is newer than the latest release",
        ),
        (_available("v0.20.0"), "stable", set(), "v0.20.0 is not newer than the installed 0.21.0"),
        (_available("v0.21.0"), "stable", set(), "v0.21.0 is not newer than the installed 0.21.0"),
        (
            _available("master", "development"),
            "development",
            {"not-ancestor"},
            "not part of master",
        ),
        (
            UpdateCheck("current", "stable", "installation", "This is the latest release."),
            "stable",
            set(),
            "already the latest version",
        ),
    ],
)
def test_an_older_or_the_same_version_is_refused(
    tmp_path: Path, result: UpdateCheck, channel: str, fail: set[str], words: str
) -> None:
    """ADR-0016-15: the launcher's own check decides, and it never moves backwards."""
    checkout = FakeCheckout(tmp_path / "checkout", fail=fail)
    updater = _updater(checkout, result, channel=channel)

    assert updater.update() == checkout_update.Attempt()
    assert "git switch" not in checkout.names()
    record = _result(checkout)
    assert record["outcome"] == "refused"
    assert words in record["reason"]
    if result.status != "current":
        assert "does not move to an older version" in record["reason"]


def test_development_older_than_the_installed_version_is_refused(tmp_path: Path) -> None:
    """ADR-0016-15: master's own version is compared too."""
    checkout = FakeCheckout(tmp_path / "checkout", master_version="0.20.0")
    updater = _updater(checkout, _available("master", "development"), channel="development")

    assert updater.update() == checkout_update.Attempt()
    assert "git switch" not in checkout.names()
    assert "master has version 0.20.0" in _result(checkout)["reason"]


@pytest.mark.parametrize("included", [True, False])
def test_development_moves_to_stable_only_with_a_release_that_includes_it(
    tmp_path: Path, included: bool
) -> None:
    """ADR-0016-19: the same version number is fine when the release includes this commit."""
    checkout = FakeCheckout(
        tmp_path / "checkout",
        branch="master",
        tag=None,
        version="0.22.0",
        fail=set() if included else {"not-ancestor"},
    )
    updater = _updater(checkout, _available("v0.22.0"))

    attempt = updater.update()

    assert ["git", "merge-base", "--is-ancestor", "HEAD", "refs/tags/v0.22.0"] in checkout.calls
    if included:
        assert attempt.switched is True
        assert checkout.head == NEW
    else:
        assert attempt == checkout_update.Attempt()
        assert "git switch" not in checkout.names()
        assert "You'll move to Stable when a release includes it." in _result(checkout)["reason"]


def test_a_request_it_cannot_read_changes_nothing(tmp_path: Path) -> None:
    """ADR-0016-15: only a valid request from the server starts an update."""
    checkout = FakeCheckout(tmp_path / "checkout")
    contract.write(_folder(checkout) / contract.REQUEST, {"id": "abcdef12", "action": "update"})
    updater = _updater(checkout, ask=False)

    assert updater.update() == checkout_update.Attempt()
    assert checkout.calls == []
    assert _result(checkout)["outcome"] == "refused"


def test_only_one_update_runs_on_a_checkout_at_a_time(tmp_path: Path) -> None:
    """ADR-0016-18: a second launcher on the same checkout does not update it too."""
    checkout = FakeCheckout(tmp_path / "checkout")
    first = _updater(checkout)
    assert first.update().switched is True
    assert first.locked
    checkout.calls.clear()
    state = (_folder(checkout) / contract.STATE).read_bytes()
    second = _updater(checkout)

    assert second.update() == checkout_update.Attempt()
    assert checkout.calls == []
    # The first update's files are left alone, and it still owns the result.
    assert (_folder(checkout) / contract.STATE).read_bytes() == state
    assert contract.read_result(_folder(checkout)) is None
    # The first one finishes and lets go; a lock file left behind holds nothing.
    first.watch(_Server(), lambda: False)  # type: ignore[arg-type]
    assert not first.locked
    assert _result(checkout)["outcome"] == "updated"
    assert (_folder(checkout) / checkout_update.LOCK).exists()
    assert _updater(checkout, _available("v0.22.0")).update() == checkout_update.Attempt()
    assert "Another AutoDJ launcher" not in _result(checkout)["reason"]


@pytest.mark.parametrize("during", ["backup", "setup"])
def test_a_stop_signal_during_the_update_puts_the_checkout_back(
    tmp_path: Path, during: str
) -> None:
    """ADR-0016-17: the checkout goes back, the record says so, and the launcher stops."""
    checkout = FakeCheckout(tmp_path / "checkout")
    updater = _updater(checkout)
    selection = checkout.root / ".uv" / "setup.json"

    def stop() -> None:
        selection.write_text('{"backend": "changed by setup"}', encoding="utf-8")
        updater._note_stop(signal.SIGTERM, None)

    checkout.on[during] = stop

    attempt = updater.update()

    assert attempt == checkout_update.Attempt(stopped=signal.SIGTERM)
    assert checkout.head == OLD
    assert checkout.names().count("setup") == (1 if during == "setup" else 0)
    if during == "backup":
        assert "git fetch" not in checkout.names()
    else:
        # The runtime selection is put back as well as the checkout.
        assert selection.read_text(encoding="utf-8") == '{"backend": "cpu"}'
    result = _result(checkout)
    assert result["outcome"] == "stopped"
    assert result["message"].startswith("AutoDJ was stopped during the update")
    assert not updater.locked


def test_a_stop_after_the_new_version_started_says_to_restore_the_data(tmp_path: Path) -> None:
    """ADR-0016-17: no restore while stopping, but the result says so and gives the command."""
    checkout = FakeCheckout(tmp_path / "checkout")
    updater = _updater(checkout)
    assert updater.update().switched is True

    assert updater.stopped_while_starting(signal.SIGTERM) == checkout_update.Attempt(
        stopped=signal.SIGTERM
    )
    assert checkout.head == OLD
    assert "restore" not in checkout.names()
    result = _result(checkout)
    assert result["outcome"] == "stopped"
    assert "the new version had started and may have changed the data" in result["message"]
    assert result["commands"] == [
        f"./autodj --config 'night owl.toml' restore --force {shlex.quote(str(updater._backup))}"
    ]


def test_a_stop_that_cannot_put_the_checkout_back_says_where_it_is(tmp_path: Path) -> None:
    """ADR-0016-17."""
    checkout = FakeCheckout(tmp_path / "checkout", fail={"restore-checkout"})
    updater = _updater(checkout)
    checkout.on["setup"] = lambda: updater._note_stop(signal.SIGINT, None)

    assert updater.update() == checkout_update.Attempt(stopped=signal.SIGINT)
    result = _result(checkout)
    assert result["outcome"] == "stopped"
    assert f"The checkout is at {NEW[:7]}." in result["message"]
    assert f"git switch --detach {OLD}" in result["commands"]


def test_an_update_a_killed_launcher_left_is_recorded_at_the_next_start(tmp_path: Path) -> None:
    """ADR-0016-17: the record says what state the checkout is in."""
    checkout = FakeCheckout(tmp_path / "checkout", head=NEW, tag=None)
    contract.write(
        _folder(checkout) / contract.STATE,
        {
            "id": "abcdef12",
            "action": "update",
            "step": "setup",
            "from": {"commit": OLD},
            "steps": contract.new_steps(),
        },
    )
    updater = _updater(checkout, ask=False)

    updater.recover_interrupted()

    result = _result(checkout)
    assert result["outcome"] == "failed"
    assert "stopped during setup" in result["message"]
    assert f"git switch --detach {OLD}" in result["commands"]


# ---------------------------------------------------------------------------
# Going back to the release this installation came from
# ---------------------------------------------------------------------------


def _moved_to_development(tmp_path: Path) -> FakeCheckout:
    """A release that the updater moved to development: it keeps the backup for Go back."""
    checkout = FakeCheckout(tmp_path / "checkout")
    updater = _updater(checkout, _available("master", "development"), channel="development")
    assert updater.update().switched is True
    updater.watch(_Server(), lambda: False)  # type: ignore[arg-type]
    checkout.calls.clear()
    return checkout


def test_moving_a_release_to_development_keeps_its_backup_for_go_back(tmp_path: Path) -> None:
    """ADR-0016-20: the tag, the backup and the date, only from this flow."""
    checkout = _moved_to_development(tmp_path)
    record = contract.read_go_back(_folder(checkout))
    assert record is not None
    assert (record.tag, record.version, record.commit) == ("v0.21.0", "0.21.0", OLD)
    assert record.backup.name.startswith("autodj-go-back-0.21.0-")
    assert record.backup.is_file()
    assert record.made_at == 1_793_000_000.0
    assert contract.go_back_problem(record) is None
    # Later development updates keep it; old ordinary backups never delete it.
    for _round in range(checkout_update.KEEP_BACKUPS + 1):
        again = _updater(checkout, _available("master", "development"), channel="development")
        assert again.update().switched is True
        again.watch(_Server(), lambda: False)  # type: ignore[arg-type]
    assert contract.read_go_back(_folder(checkout)) == record
    ordinary = list((_folder(checkout) / contract.BACKUPS).glob("autodj-update-*.zip"))
    assert len(ordinary) <= checkout_update.KEEP_BACKUPS


def test_go_back_restores_the_backup_with_the_release_it_came_from(tmp_path: Path) -> None:
    """ADR-0016-20: switch to the tag, its setup, restore that backup, Doctor, then healthy."""
    checkout = _moved_to_development(tmp_path)
    kept = contract.read_go_back(_folder(checkout))
    assert kept is not None
    updater = _updater(checkout, action="go-back")

    attempt = updater.update()

    assert attempt == checkout_update.Attempt(switched=True)
    assert checkout.names() == [
        "git merge-base",  # the checkout still came from v0.21.0 ...
        "git tag",  # ... and has reached no newer release
        "git rev-parse",
        "git status",
        "backup",
        "git switch",
        "git rev-parse",
        "setup",
        "restore",
        "doctor",
    ]
    assert ["git", "switch", "--detach", "v0.21.0"] in checkout.calls
    restore = next(call for call in checkout.calls if "restore" in call)
    assert restore == [
        *BASE,
        "--config",
        "night owl.toml",
        "restore",
        "--force",
        str(kept.backup),
    ]
    assert checkout.head == OLD
    updater.watch(_Server(), lambda: False)  # type: ignore[arg-type]
    result = _result(checkout)
    assert result["outcome"] == "updated"
    assert result["action"] == "go-back"
    assert result["message"] == (
        f"Went back to 0.21.0. Settings, play history and library changes made since "
        f"{kept.date} were replaced by the backup from that day."
    )
    assert _steps(result)["fetch"] == "skipped"
    assert _steps(result)["restore"] == "done"
    # Used: it is not offered again.
    assert contract.read_go_back(_folder(checkout)) is None


def test_a_failed_go_back_restores_the_data_from_before_it(tmp_path: Path) -> None:
    """ADR-0016-20: rolled back like an update, and its data put back from its own backup."""
    checkout = _moved_to_development(tmp_path)
    updater = _updater(checkout, action="go-back", probe=lambda _probe: False)
    assert updater.update().switched is True
    safety = updater._backup
    updater.watch(_Server(exits=1), lambda: False)  # type: ignore[arg-type]

    assert updater.failed_start() == checkout_update.Attempt()
    assert checkout.head == MASTER
    restores = [call for call in checkout.calls if "restore" in call]
    assert restores[-1][-1] == str(safety)
    assert _result(checkout)["outcome"] == "rolled-back"
    assert contract.read_go_back(_folder(checkout)) is not None


def test_go_back_without_its_backup_changes_nothing(tmp_path: Path) -> None:
    """ADR-0016-20."""
    checkout = _moved_to_development(tmp_path)
    kept = contract.read_go_back(_folder(checkout))
    assert kept is not None
    kept.backup.unlink()
    updater = _updater(checkout, action="go-back")

    assert updater.update() == checkout_update.Attempt()
    assert checkout.calls == []
    assert _result(checkout)["reason"].endswith("is missing.")


def test_reaching_a_release_forgets_go_back(tmp_path: Path) -> None:
    """ADR-0016-19, ADR-0016-20: back on a release, the older one would be a downgrade."""
    checkout = _moved_to_development(tmp_path)
    checkout.tags["v0.24.0"] = "4" * 40
    checkout.versions["4" * 40] = "0.24.0"
    updater = _updater(checkout, _available("v0.24.0"))
    assert updater.update().switched is True
    updater.watch(_Server(), lambda: False)  # type: ignore[arg-type]
    assert contract.read_go_back(_folder(checkout)) is None


# ---------------------------------------------------------------------------
# The supervise loop
# ---------------------------------------------------------------------------


class _StubUpdater:
    def __init__(self, attempt: checkout_update.Attempt, healthy: bool = True) -> None:
        self.attempt = attempt
        self.healthy = healthy
        self.pending = False
        self.folder = Path("update-folder")
        self.events: list[str] = []

    def update(self) -> checkout_update.Attempt:
        self.events.append("update")
        self.pending = self.attempt.switched
        return self.attempt

    def watch(self, _process: object, _stopping: Callable[[], bool]) -> None:
        self.events.append("watch")
        if self.healthy:
            self.pending = False

    def failed_start(self) -> checkout_update.Attempt:
        self.events.append("rollback")
        self.pending = False
        return checkout_update.Attempt()

    def stopped_while_starting(self, signum: int) -> checkout_update.Attempt:
        self.events.append(f"stopped {signum}")
        return checkout_update.Attempt(stopped=signum)


@pytest.mark.parametrize("healthy", [True, False])
def test_the_loop_updates_while_no_server_runs_then_starts_and_watches_it(healthy: bool) -> None:
    """ADR-0016-14: the update runs between two servers; an unhealthy one is rolled back."""
    stub = _StubUpdater(checkout_update.Attempt(switched=True), healthy=healthy)
    statuses = iter([updates.UPDATE_EXIT_CODE, 1, 0])
    started: list[tuple[list[str], dict[str, str], bool]] = []

    def run_child(command, environment, watch=None) -> int:
        started.append((command, dict(environment), watch is not None))
        if watch is not None:
            watch(object(), lambda: False)
        return next(statuses)

    plans = iter([(["new", "serve"], {"B": "2"}), (["old", "serve"], {"B": "3"})])
    status = launcher.run_supervised(
        ["old", "serve"],
        {"A": "1"},
        run_child=run_child,
        clock=lambda: 0.0,
        updater=stub,  # type: ignore[arg-type]
        plan=lambda: next(plans),
    )

    marks = {
        updates.SUPERVISOR_ENV: "1",
        updates.UPDATER_ENV: "launcher",
        contract.UPDATE_DIR_ENV: "update-folder",
    }
    assert started[0][1] == {"A": "1", **marks}
    assert started[1] == (["new", "serve"], {"B": "2", **marks}, True)
    if healthy:
        assert stub.events == ["update", "watch"]
        assert status == 1
    else:
        assert stub.events == ["update", "watch", "rollback"]
        assert started[2][0] == ["old", "serve"]
        assert status == 0


def test_a_stop_while_the_new_version_starts_puts_the_checkout_back() -> None:
    """ADR-0016-17: the stop still wins, and ends the launcher with its signal."""
    stub = _StubUpdater(checkout_update.Attempt(switched=True), healthy=False)
    calls = iter([updates.UPDATE_EXIT_CODE])

    def run_child(command, environment, watch=None) -> int:
        if watch is not None:
            raise launcher.StopRequested(signal.SIGTERM, 0)
        return next(calls)

    ended: list[int] = []
    launcher.run_supervised(
        ["serve"],
        {},
        run_child=run_child,
        end_with_signal=lambda signum: ended.append(signum) or 128 + signum,
        updater=stub,  # type: ignore[arg-type]
        plan=lambda: (["serve"], {}),
    )
    assert stub.events == ["update", f"stopped {signal.SIGTERM}"]
    assert ended == [signal.SIGTERM]


def test_a_failed_rollback_ends_the_launcher_without_a_server() -> None:
    """ADR-0016-16."""
    stub = _StubUpdater(checkout_update.Attempt(failed=True))
    runs: list[int] = []

    def run_child(command, environment, watch=None) -> int:
        runs.append(1)
        return updates.UPDATE_EXIT_CODE

    status = launcher.run_supervised(
        ["serve"],
        {},
        run_child=run_child,
        updater=stub,  # type: ignore[arg-type]
        plan=lambda: (["serve"], {}),
    )
    assert status == 1
    assert runs == [1]


def test_without_an_updater_the_update_status_is_an_ordinary_exit() -> None:
    """ADR-0016-11: a server not told it may update cannot ask for one."""
    status = launcher.run_supervised(
        ["serve"], {}, run_child=lambda command, environment: updates.UPDATE_EXIT_CODE
    )
    assert status == updates.UPDATE_EXIT_CODE


def test_global_options_are_the_words_before_serve() -> None:
    """ADR-0016-14: backup and Doctor read the same configuration as the server."""
    assert launcher.global_options(["--config", "a.toml", "-v", "serve", "--lan"]) == [
        "--config",
        "a.toml",
        "-v",
    ]
    assert launcher.global_options(["serve"]) == []


def test_a_new_version_that_cannot_even_start_is_rolled_back() -> None:
    """ADR-0016-16: a server process that cannot start counts as an unhealthy start."""
    stub = _StubUpdater(checkout_update.Attempt(switched=True), healthy=False)
    statuses = iter([updates.UPDATE_EXIT_CODE, 0])

    def run_child(command, environment, watch=None) -> int:
        if watch is not None:
            raise FileNotFoundError(command[0])
        return next(statuses)

    status = launcher.run_supervised(
        ["old", "serve"],
        {},
        run_child=run_child,
        clock=lambda: 0.0,
        updater=stub,  # type: ignore[arg-type]
        plan=lambda: (["serve"], {}),
    )
    assert stub.events == ["update", "rollback"]
    assert status == 0


# ---------------------------------------------------------------------------
# Review fixes: other servers, fetching one tag, branch guard, stops
# ---------------------------------------------------------------------------


def test_an_update_is_refused_while_another_launcher_serves_from_this_checkout(
    tmp_path: Path,
) -> None:
    """ADR-0016-18: launcher B's server would keep running the old files under A's update."""
    checkout = FakeCheckout(tmp_path / "checkout")
    other = _updater(checkout, ask=False)
    other.hold_serving(pid=424242)
    assert other.serving
    updater = _updater(checkout)
    updater.hold_serving()  # its own does not count

    assert updater.update() == checkout_update.Attempt()
    assert checkout.calls == []
    result = _result(checkout)
    assert result["outcome"] == "refused"
    assert result["reason"] == (
        "Another AutoDJ server runs from this AutoDJ folder (launcher process 424242). "
        "Stop it before updating."
    )
    # Once that launcher has ended, its lock file holds nothing and is cleared.
    contract.unlock(other._serving)
    other._serving = None
    assert contract.other_servers(_folder(checkout), {os.getpid()}) == []
    assert not contract.serving_path(_folder(checkout), 424242).exists()


def test_a_moved_release_tag_is_named_instead_of_blaming_the_network(tmp_path: Path) -> None:
    """ADR-0016-14: only the target tag is fetched; a clash over it says so."""
    checkout = FakeCheckout(tmp_path / "checkout", fail={"fetch-clobber"})
    updater = _updater(checkout)

    assert updater.update() == checkout_update.Attempt()
    assert "git switch" not in checkout.names()
    result = _result(checkout)
    assert result["outcome"] == "refused"
    assert "The release tag v0.22.0 on the origin remote is not the v0.22.0" in result["reason"]
    assert "network" not in result["reason"]
    assert result["commands"] == ["git ls-remote origin refs/tags/v0.22.0", "git rev-parse v0.22.0"]


def test_a_branch_moved_during_the_update_is_never_reset(tmp_path: Path) -> None:
    """ADR-0016-16: someone committed on master meanwhile; rollback keeps their commit."""
    checkout = FakeCheckout(tmp_path / "checkout", branch="master", tag=None, version="0.20.0")
    updater = _updater(checkout, _available("master", "development"), channel="development")
    assert updater.update().switched is True
    checkout.tips["master"] = "9" * 40  # a commit made from another shell
    updater.watch(_Server(exits=1), lambda: False)  # type: ignore[arg-type]

    assert updater.failed_start() == checkout_update.Attempt(failed=True)
    assert not any("--force-create" in call for call in checkout.calls)
    assert checkout.tips["master"] == "9" * 40
    result = _result(checkout)
    assert result["outcome"] == "failed"
    assert (
        "The branch master moved while AutoDJ was updating (it is at 9999999 now)"
        in result["message"]
    )
    assert f"git switch --detach {OLD}" in result["commands"]


def test_reading_the_checkout_is_never_cut_short_by_a_stop(tmp_path: Path) -> None:
    """ADR-0016-17: a second signal during rollback cannot fake a failed restore."""
    checkout = FakeCheckout(tmp_path / "checkout")
    updater = _updater(checkout)
    updater.update()
    for call, options in zip(checkout.calls, checkout.options, strict=True):
        # Reading the ssh setting goes through the update check's runner, before
        # anything changes; every other git command is read or written in full.
        if call[0] == "git" and call[1] not in {"fetch", "config"}:
            assert options.get("interruptible") is False, call
    fetch = checkout.calls.index(
        next(call for call in checkout.calls if call[:2] == ["git", "fetch"])
    )
    assert checkout.options[fetch]["interruptible"] is True


def test_go_back_is_refused_when_the_checkout_moved_by_hand(tmp_path: Path) -> None:
    """ADR-0016-20: a newer release reached since, or a checkout that left that release."""
    checkout = _moved_to_development(tmp_path)
    checkout.merged = ["v0.21.0", "v0.22.0"]
    updater = _updater(checkout, action="go-back")

    assert updater.update() == checkout_update.Attempt()
    assert "git switch" not in checkout.names()
    assert _result(checkout)["reason"] == (
        "This checkout has reached release v0.22.0 since, so going back to 0.21.0 is not offered."
    )
    checkout.merged = []
    checkout.fail.add("not-ancestor")
    assert _updater(checkout, action="go-back").update() == checkout_update.Attempt()
    assert "has moved away from 0.21.0" in _result(checkout)["reason"]


def test_an_interrupted_update_suggests_its_branch(tmp_path: Path) -> None:
    """ADR-0016-17: the branch is in state.json, so the switch back keeps it."""
    checkout = FakeCheckout(tmp_path / "checkout", head=NEW, tag=None)
    contract.write(
        _folder(checkout) / contract.STATE,
        {
            "id": "abcdef12",
            "action": "update",
            "step": "setup",
            "from": {"commit": OLD, "branch": "master"},
            "steps": contract.new_steps(),
        },
    )
    _updater(checkout, ask=False).recover_interrupted()
    result = _result(checkout)
    assert "on master" in result["message"]
    assert f"git switch --force-create master {OLD}" in result["commands"]


def test_a_stop_ends_a_running_step_within_a_moment(tmp_path: Path) -> None:
    """ADR-0016-17: a real process; a step ended by a stop is a failure, not success."""
    import sys
    import threading
    import time

    updater = CheckoutUpdater(tmp_path, [], lambda: ([], {}))
    timer = threading.Timer(0.5, lambda: updater._stopped.append(signal.SIGTERM))
    started = time.monotonic()
    timer.start()
    try:
        done = updater._step(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=60,
            capture=False,
            interruptible=True,
        )
    finally:
        timer.cancel()
    assert time.monotonic() - started < 10
    assert done.returncode == -1
    # A step that must not be cut short finishes, and reports its own status.
    kept = updater._step(
        [sys.executable, "-c", "import time; time.sleep(1); raise SystemExit(3)"],
        timeout=60,
        interruptible=False,
    )
    assert kept.returncode == 3
