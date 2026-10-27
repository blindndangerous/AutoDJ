"""The launcher's half of Update: updating a source checkout between two runs of serve."""

from __future__ import annotations

import json
import signal
import subprocess
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from autodj import checkout_update, launcher, updates
from autodj.checkout_update import CheckoutUpdater
from autodj.updates import HealthProbe, Installation, UpdateCheck, UpdateSettings

OLD = "1" * 40
NEW = "2" * 40
MASTER = "3" * 40
BASE = ["py", "-m", "autodj"]
PROBE = HealthProbe("http", "127.0.0.1", 8080, "127.0.0.1:8080")


def _pyproject(version: str) -> str:
    return f'[project]\nname = "autodj"\nversion = "{version}"\n'


class FakeCheckout:
    """A checkout that answers git, backup, setup and Doctor like the real ones."""

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
    ) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        (root / "pyproject.toml").write_text(_pyproject(version), encoding="utf-8")
        (root / "scripts").mkdir(exist_ok=True)
        (root / "scripts" / "setup.py").write_text("", encoding="utf-8")
        (root / ".uv").mkdir(exist_ok=True)
        (root / ".uv" / "setup.json").write_text('{"backend": "cpu"}', encoding="utf-8")
        self.head = OLD
        self.branch = branch
        self.tag = tag
        self.dirty = dirty
        self.versions = {OLD: version, NEW: "0.22.0", MASTER: master_version}
        self.doctor = doctor if doctor is not None else {"exit_code": 0, "checks": []}
        self.fail = fail or set()
        self.calls: list[list[str]] = []
        self.on: dict[str, Callable[[], None]] = {}

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
        (self.root / "pyproject.toml").write_text(_pyproject(self.versions[commit]), "utf-8")

    def run(self, argv: list[str], **_options: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        name = self._name(argv)
        if name in self.on:
            self.on[name]()
        if name in self.fail:
            return subprocess.CompletedProcess(argv, 1, "", "")
        if argv[0] == "git":
            return self._git(argv, argv[1:])
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
            return ok()
        if args[:3] == ["rev-parse", "--verify", "--quiet"]:
            ref = args[3]
            found = {"refs/tags/v0.22.0^{commit}": NEW}.get(ref)
            if ref.startswith("refs/remotes/origin/master"):
                found = MASTER
            return ok(f"{found}\n") if found else ok(code=1)
        if args == ["rev-parse", "--verify", "HEAD^{commit}"]:
            return ok(f"{self.head}\n")
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return ok(code=1 if "not-ancestor" in self.fail else 0)
        if args[0] == "show":
            return ok(_pyproject(self.versions[MASTER]))
        if args[0] == "switch":
            where = [word for word in args[1:] if word != "--discard-changes"]
            if where[0] == "--detach":
                target = where[1]
                commit = NEW if target == "v0.22.0" else target
                if commit != OLD and "switch-new" in self.fail:
                    return ok(code=1)
                if commit == OLD and "restore" in self.fail:
                    return ok(code=1)
                self._move(commit, None, target if target.startswith("v") else None)
                return ok()
            if where[0] == "--force-create":
                if "restore" in self.fail:
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


def _updater(
    checkout: FakeCheckout,
    result: UpdateCheck | None = None,
    *,
    channel: str = "stable",
    probe: Callable[[HealthProbe], bool] = lambda _probe: True,
    request: dict[str, Any] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> CheckoutUpdater:
    record = {
        "state": "requested",
        "id": "abcdef12",
        "channel": channel,
        "probe": PROBE.to_dict(),
        # Never trusted: the launcher works out the target itself.
        "target": "v9.9.9",
    }
    updates.write_update_state(updates.update_state_path(checkout.root), request or record)
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
        clock=lambda: 1_000_000.0,
        monotonic=monotonic or (lambda: 0.0),
        sleep=lambda _seconds: None,
        setup_python="setup-py",
        windows=False,
    )
    updater.checked = seen  # type: ignore[attr-defined]
    return updater


def _record(checkout: FakeCheckout) -> dict[str, Any]:
    found = updates.read_update_state(updates.update_state_path(checkout.root))
    assert found is not None
    return found


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
    assert Path(backup[6]).parent == checkout.root / checkout_update.BACKUPS
    assert ["git", "fetch", "--tags", "origin"] in checkout.calls
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
    assert _record(checkout)["state"] == "running"
    assert (checkout.root / checkout_update.LOCK).exists()

    server = _Server()
    updater.watch(server, lambda: False)  # type: ignore[arg-type]

    assert updater.started_ok is True
    assert updater.pending is False
    record = _record(checkout)
    assert record["state"] == "updated"
    assert record["id"] == "abcdef12"
    assert record["message"] == "Updated to 0.22.0."
    assert record["from"]["version"] == "0.21.0"
    assert record["to"] == {"version": "0.22.0", "commit": NEW, "label": "0.22.0"}
    assert record["commands"] == []
    assert not (checkout.root / checkout_update.LOCK).exists()


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
    assert _record(checkout)["message"] == f"Updated to 0.23.0, commit {MASTER[:7]} on master."


def test_a_checkout_with_changes_is_refused_before_anything_changes(tmp_path: Path) -> None:
    """ADR-0016-14: any git status output refuses, with the commands that put the changes aside."""
    checkout = FakeCheckout(tmp_path / "checkout", dirty="?? notes.txt\n")
    updater = _updater(checkout)

    assert updater.update() == checkout_update.Attempt()
    assert checkout.names() == ["git status"]
    record = _record(checkout)
    assert record["state"] == "refused"
    assert record["message"].startswith("AutoDJ did not update: The AutoDJ folder has changes")
    assert record["commands"] == [
        "git status --short",
        'git stash push --include-untracked -m "Before updating AutoDJ"',
    ]
    assert not (checkout.root / checkout_update.LOCK).exists()


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
    (checkout.root / ".uv" / "setup.json").write_text('{"backend": "amd"}', encoding="utf-8")
    checkout.on["setup"] = lambda: (checkout.root / ".uv" / "setup.json").write_text(
        '{"backend": "changed"}', encoding="utf-8"
    )
    updater = _updater(checkout)

    assert updater.update() == checkout_update.Attempt()
    assert checkout.head == OLD
    assert ["git", "switch", "--detach", OLD] in checkout.calls
    assert checkout.names().count("setup") == 2  # the new version's, then the old one's again
    assert "migrate" not in " ".join(" ".join(call) for call in checkout.calls)
    record = _record(checkout)
    assert record["state"] == "rolled-back"
    assert record["message"].startswith(
        "The update failed, so AutoDJ went back to 0.21.0. The new version needs a library migration"
    )
    assert record["commands"] == [
        "git switch --detach v0.22.0",
        "./autodj --config 'night owl.toml' setup",
        "./autodj --config 'night owl.toml' migrate-index",
        "./autodj --config 'night owl.toml' migrate-index --apply",
        "./autodj --config 'night owl.toml' doctor --no-fix",
    ]


@pytest.mark.parametrize("how", ["exits", "never-answers"])
def test_a_new_version_that_is_not_healthy_is_rolled_back(tmp_path: Path, how: str) -> None:
    """ADR-0016-13, ADR-0016-16: healthy means still running and /healthz answering in time."""
    checkout = FakeCheckout(tmp_path / "checkout", branch="master", tag=None)
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
    record = _record(checkout)
    assert record["state"] == "rolled-back"
    expected = (
        "The new version stopped with exit status 1 before it was ready."
        if how == "exits"
        else "The new version did not answer within 180 seconds."
    )
    assert record["message"] == (
        f"The update failed, so AutoDJ went back to 0.21.0, commit {OLD[:7]} on master. {expected}"
    )
    assert record["commands"] == []


def test_a_rollback_that_fails_says_where_the_checkout_is_and_starts_nothing(
    tmp_path: Path,
) -> None:
    """ADR-0016-16."""
    checkout = FakeCheckout(tmp_path / "checkout", fail={"setup", "restore"})
    updater = _updater(checkout)

    attempt = updater.update()

    assert attempt == checkout_update.Attempt(failed=True)
    assert checkout.head == NEW
    record = _record(checkout)
    assert record["state"] == "failed"
    assert "could not go back to 0.21.0" in record["message"]
    assert f"The checkout is at {NEW[:7]}." in record["message"]
    assert "A backup from before the update is at" in record["message"]
    assert record["commands"] == [
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
    record = _record(checkout)
    assert record["state"] == "refused"
    assert words in record["reason"]
    if result.status != "current":
        assert "does not move to an older version" in record["reason"]


def test_development_older_than_the_installed_version_is_refused(tmp_path: Path) -> None:
    """ADR-0016-15: master's own version is compared too."""
    checkout = FakeCheckout(tmp_path / "checkout", master_version="0.20.0")
    updater = _updater(checkout, _available("master", "development"), channel="development")

    assert updater.update() == checkout_update.Attempt()
    assert "git switch" not in checkout.names()
    assert "master has version 0.20.0" in _record(checkout)["reason"]


@pytest.mark.parametrize(
    "request_record",
    [
        {"state": "requested", "id": "abcdef12", "channel": "nightly", "probe": PROBE.to_dict()},
        {"state": "requested", "id": "abcdef12", "channel": "stable", "probe": {"scheme": "ftp"}},
        {"state": "updated", "id": "abcdef12", "channel": "stable", "probe": PROBE.to_dict()},
    ],
)
def test_an_unreadable_request_changes_nothing(
    tmp_path: Path, request_record: dict[str, Any]
) -> None:
    """ADR-0016-15: only a valid request from the server starts an update."""
    checkout = FakeCheckout(tmp_path / "checkout")
    updater = _updater(checkout, request=request_record)

    assert updater.update() == checkout_update.Attempt()
    assert checkout.calls == []
    assert _record(checkout)["state"] == "refused"


def test_only_one_update_runs_on_a_checkout_at_a_time(tmp_path: Path) -> None:
    """ADR-0016-18: a second launcher on the same checkout does not update it too."""
    checkout = FakeCheckout(tmp_path / "checkout")
    lock = checkout.root / checkout_update.LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("1\n", encoding="utf-8")
    updater = _updater(checkout)
    updater._clock = lambda: lock.stat().st_mtime + 60  # type: ignore[method-assign]

    assert updater.update() == checkout_update.Attempt()
    assert checkout.calls == []
    assert "Another AutoDJ launcher is updating" in _record(checkout)["reason"]
    assert lock.exists()


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
    if during == "setup":
        # The runtime selection is put back as well as the checkout.
        assert selection.read_text(encoding="utf-8") == '{"backend": "cpu"}'
    assert checkout.names().count("setup") == (1 if during == "setup" else 0)
    if during == "backup":
        assert "git fetch" not in checkout.names()
    record = _record(checkout)
    assert record["state"] == "stopped"
    assert record["message"].startswith("AutoDJ was stopped during the update")
    assert not (checkout.root / checkout_update.LOCK).exists()


def test_a_stop_that_cannot_put_the_checkout_back_says_where_it_is(tmp_path: Path) -> None:
    """ADR-0016-17."""
    checkout = FakeCheckout(tmp_path / "checkout", fail={"restore"})
    updater = _updater(checkout)
    checkout.on["setup"] = lambda: updater._note_stop(signal.SIGINT, None)

    assert updater.update() == checkout_update.Attempt(stopped=signal.SIGINT)
    record = _record(checkout)
    assert record["state"] == "stopped"
    assert f"The checkout is at {NEW[:7]}." in record["message"]
    assert f"git switch --detach {OLD}" in record["commands"]


def test_an_update_a_killed_launcher_left_is_recorded_at_the_next_start(tmp_path: Path) -> None:
    """ADR-0016-17: the record says what state the checkout is in."""
    checkout = FakeCheckout(tmp_path / "checkout")
    checkout.head = NEW
    updates.write_update_state(
        updates.update_state_path(checkout.root),
        {"state": "running", "id": "abcdef12", "step": "setup", "from": {"commit": OLD}},
    )
    updater = _updater(checkout, request=None)
    updates.write_update_state(
        updates.update_state_path(checkout.root),
        {"state": "running", "id": "abcdef12", "step": "setup", "from": {"commit": OLD}},
    )

    updater.recover_interrupted()

    record = _record(checkout)
    assert record["state"] == "failed"
    assert "stopped during setup" in record["message"]
    assert f"git switch --detach {OLD}" in record["commands"]


# ---------------------------------------------------------------------------
# The supervise loop
# ---------------------------------------------------------------------------


class _StubUpdater:
    def __init__(self, attempt: checkout_update.Attempt, healthy: bool = True) -> None:
        self.attempt = attempt
        self.healthy = healthy
        self.pending = False
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

    assert started[0][1][updates.UPDATER_ENV] == "launcher"
    assert started[0][1][updates.SUPERVISOR_ENV] == "1"
    assert started[1] == (
        ["new", "serve"],
        {"B": "2", updates.SUPERVISOR_ENV: "1", updates.UPDATER_ENV: "launcher"},
        True,
    )
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

    assert (
        launcher.run_supervised(
            ["serve"],
            {},
            run_child=run_child,
            updater=stub,
            plan=lambda: (["serve"], {}),  # type: ignore[arg-type]
        )
        == 1
    )
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


# ---------------------------------------------------------------------------
# The health probe
# ---------------------------------------------------------------------------


def test_the_health_probe_needs_200_and_ok_from_healthz() -> None:
    """ADR-0016-13: asked on this computer, with the Host header the server allows."""
    seen: list[str | None] = []
    answers = iter([(200, b'{"status": "ok"}'), (403, b"Disallowed Host"), (200, b"{}")])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Host"))
            code, body = next(answers)
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        probe = HealthProbe("http", "127.0.0.1", port, f"localhost:{port}")
        assert checkout_update.probe_health(probe) is True
        assert checkout_update.probe_health(probe) is False
        assert checkout_update.probe_health(probe) is False
    finally:
        server.shutdown()
        server.server_close()
    assert seen == [f"localhost:{port}"] * 3
    assert checkout_update.probe_health(probe, timeout=0.5) is False
