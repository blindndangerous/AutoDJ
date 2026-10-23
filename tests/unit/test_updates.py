"""The update check, its channels and settings, and restart detection (autodj.updates)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from autodj import updates
from autodj.updates import Installation, UpdateChecker, UpdateSettings

MASTER_SHA = "a" * 40
LOCAL_SHA = "b" * 40


@pytest.fixture(autouse=True)
def _network_check_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests pass stand-ins for git and GitHub, so the check may run."""
    monkeypatch.delenv(updates.CHECK_ENV, raising=False)


def _done(stdout: str = "", returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


def _git_checkout(tmp_path: Path, **kwargs: object) -> Installation:
    root = tmp_path / "checkout"
    root.mkdir(exist_ok=True)
    values: dict[str, object] = {
        "kind": "git",
        "version": "0.21.0",
        "commit": LOCAL_SHA,
        "branch": "master",
        "tag": None,
        "root": root,
    }
    values.update(kwargs)
    return Installation(**values)  # type: ignore[arg-type]


def _ls_remote(tags: list[str] = (), master: str = MASTER_SHA, ancestor: bool = False) -> Mock:
    """A git stand-in answering ls-remote, cat-file and merge-base."""

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        args = command[1:]
        if args[:3] == ["ls-remote", "--tags", "--refs"]:
            return _done("".join(f"{'c' * 40}\trefs/tags/{tag}\n" for tag in tags))
        if args[:2] == ["ls-remote", "origin"]:
            return _done(f"{master}\trefs/heads/master\n")
        if args[:2] == ["cat-file", "-t"]:
            return _done("commit\n" if ancestor else "", 0 if ancestor else 128)
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return _done(returncode=0 if ancestor else 1)
        raise AssertionError(f"unexpected git {args}")

    return Mock(side_effect=run)


# ---------------------------------------------------------------------------
# Channel inference
# ---------------------------------------------------------------------------


def test_a_tag_checkout_follows_stable_and_master_follows_development(tmp_path: Path) -> None:
    """ADR-0016-01."""
    tagged = _git_checkout(tmp_path, branch=None, tag="v0.21.0")
    master = _git_checkout(tmp_path)
    assert tagged.inferred_channel == "stable"
    assert master.inferred_channel == "development"
    assert updates.channel_for(tagged, UpdateSettings()) == ("stable", "installation")
    assert updates.channel_for(master, UpdateSettings()) == ("development", "installation")


def test_a_saved_channel_overrides_the_inferred_one(tmp_path: Path) -> None:
    """ADR-0016-01."""
    master = _git_checkout(tmp_path)
    assert updates.channel_for(master, UpdateSettings(channel="stable")) == ("stable", "setting")


def test_detect_installation_reads_branch_and_release_tag_from_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0016-01."""
    monkeypatch.setattr(updates.shutil, "which", lambda name: "/usr/bin/" + name)
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        args = command[1:]
        if args[0] == "rev-parse":
            return _done(LOCAL_SHA + "\n")
        if args[0] == "symbolic-ref":
            return _done(returncode=1)
        if args[0] == "tag":
            return _done("not-a-release\nv0.19.1\nv0.21.0\n")
        raise AssertionError(args)

    found = updates.detect_installation(
        root=root, environ={}, markers=(), runner=Mock(side_effect=run), version="0.21.0"
    )
    assert (found.kind, found.branch, found.tag, found.commit) == (
        "git",
        None,
        "v0.21.0",
        LOCAL_SHA,
    )
    assert found.inferred_channel == "stable"


def test_a_container_follows_the_branch_of_its_mounted_checkout(tmp_path: Path) -> None:
    """ADR-0016-01."""
    marker = tmp_path / ".dockerenv"
    marker.touch()
    config = tmp_path / "config"
    (config / ".git").mkdir(parents=True)
    (config / ".git" / "HEAD").write_text("ref: refs/heads/master\n", encoding="ascii")

    found = updates.detect_installation(
        root=tmp_path / "no-checkout",
        environ={"AUTODJ_COMMIT": "0DB0A92"},
        markers=(marker,),
        version="0.21.0",
        working_dir=config,
    )
    assert (found.kind, found.branch, found.commit) == ("container", "master", "0db0a92")
    assert found.inferred_channel == "development"


def test_a_package_without_git_follows_stable(tmp_path: Path) -> None:
    """ADR-0016-01."""
    found = updates.detect_installation(
        root=tmp_path, environ={"AUTODJ_COMMIT": "not hex"}, markers=(), version="0.21.0"
    )
    assert (found.kind, found.commit, found.inferred_channel) == ("package", None, "stable")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_round_trip_and_damaged_files_give_defaults(tmp_path: Path) -> None:
    """ADR-0016-02."""
    assert updates.load_settings(tmp_path) == UpdateSettings()
    updates.save_settings(tmp_path, UpdateSettings(check=False, channel="development"))
    assert updates.load_settings(tmp_path) == UpdateSettings(check=False, channel="development")
    updates.settings_path(tmp_path).write_text('{"check": "yes"}', encoding="utf-8")
    assert updates.load_settings(tmp_path) == UpdateSettings()


@pytest.mark.parametrize(
    "raw",
    [[], {"check": 1}, {"channel": "beta"}, {"check": True, "channel": None, "url": "x"}],
)
def test_parse_settings_refuses_anything_else(raw: object) -> None:
    """ADR-0016-02."""
    with pytest.raises(ValueError):
        updates.parse_settings(raw)


# ---------------------------------------------------------------------------
# The check, offline and turned off
# ---------------------------------------------------------------------------


def test_a_check_turned_off_contacts_nothing(tmp_path: Path) -> None:
    """ADR-0016-02."""
    runner, fetch = Mock(), Mock()
    result = updates.check_for_update(
        _git_checkout(tmp_path), UpdateSettings(check=False), runner=runner, fetch_text=fetch
    )
    assert result.status == "off"
    runner.assert_not_called()
    fetch.assert_not_called()


@pytest.mark.parametrize("how", ["offline-option", "environment"])
def test_an_offline_check_contacts_nothing(tmp_path: Path, how: str) -> None:
    """ADR-0016-02."""
    runner, fetch = Mock(), Mock()
    result = updates.check_for_update(
        Installation(kind="package", version="0.21.0"),
        UpdateSettings(),
        network=how != "offline-option",
        environ={updates.CHECK_ENV: "0"} if how == "environment" else {},
        runner=runner,
        fetch_text=fetch,
    )
    assert result.status == "off"
    runner.assert_not_called()
    fetch.assert_not_called()


def test_an_unreachable_remote_is_a_warning_without_its_address(tmp_path: Path) -> None:
    """ADR-0016-03."""
    runner = Mock(
        return_value=_done(
            returncode=128, stderr="fatal: unable to access 'https://user:s3cret@example.test/'"
        )
    )
    result = updates.check_for_update(
        _git_checkout(tmp_path, branch=None, tag="v0.21.0"), UpdateSettings(), runner=runner
    )
    assert result.status == "unavailable"
    assert "s3cret" not in result.message
    assert "exit status 128" in result.message


def test_a_git_timeout_is_a_warning(tmp_path: Path) -> None:
    """ADR-0016-03."""
    runner = Mock(side_effect=subprocess.TimeoutExpired(["git"], 15))
    result = updates.check_for_update(_git_checkout(tmp_path), UpdateSettings(), runner=runner)
    assert result.status == "unavailable"
    assert runner.call_args.kwargs["timeout"] == updates.GIT_TIMEOUT_SECONDS


def test_an_offline_package_check_is_a_warning() -> None:
    """ADR-0016-03."""
    fetch = Mock(side_effect=OSError("network unreachable"))
    result = updates.check_for_update(
        Installation(kind="package", version="0.21.0"), UpdateSettings(), fetch_text=fetch
    )
    assert result.status == "unavailable"
    assert fetch.call_args.args[2] == updates.HTTP_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version,tag,expected",
    [
        ("0.21.0", "v0.21.0", "current"),
        ("0.19.1", "v0.19.1", "available"),
        ("0.22.0", None, "ahead"),
    ],
)
def test_stable_compares_with_the_newest_release_tag(
    tmp_path: Path, version: str, tag: str | None, expected: str
) -> None:
    """ADR-0016-03."""
    installation = _git_checkout(tmp_path, version=version, branch=None, tag=tag)
    result = updates.check_for_update(
        installation,
        UpdateSettings(),
        runner=_ls_remote(tags=["v0.9.0", "v0.21.0", "v0.19.1", "nightly"]),
    )
    assert result.status == expected
    assert result.latest == "v0.21.0"
    assert result.target == ("v0.21.0" if expected == "available" else None)


def test_master_after_a_release_is_ahead_of_it_on_the_stable_channel(tmp_path: Path) -> None:
    """ADR-0016-03: the same version number with commits after the tag."""
    result = updates.check_for_update(
        _git_checkout(tmp_path),
        UpdateSettings(channel="stable"),
        runner=_ls_remote(tags=["v0.21.0"]),
    )
    assert result.status == "ahead"
    assert "after the latest release" in result.message


@pytest.mark.parametrize(
    "master,ancestor,expected",
    [(LOCAL_SHA, False, "current"), (MASTER_SHA, False, "available"), (MASTER_SHA, True, "ahead")],
)
def test_development_compares_with_the_head_of_master(
    tmp_path: Path, master: str, ancestor: bool, expected: str
) -> None:
    """ADR-0016-03."""
    result = updates.check_for_update(
        _git_checkout(tmp_path),
        UpdateSettings(),
        runner=_ls_remote(master=master, ancestor=ancestor),
    )
    assert result.status == expected
    assert result.target == ("master" if expected == "available" else None)


def test_a_branch_other_than_master_is_named_and_not_compared(tmp_path: Path) -> None:
    """ADR-0016-03."""
    runner = Mock()
    result = updates.check_for_update(
        _git_checkout(tmp_path, branch="feat/mine"), UpdateSettings(), runner=runner
    )
    assert result.status == "branch"
    assert "feat/mine" in result.message
    runner.assert_not_called()


def test_a_package_asks_githubs_latest_release() -> None:
    """ADR-0016-03."""
    fetch = Mock(return_value=json.dumps({"tag_name": "v0.22.0"}))
    result = updates.check_for_update(
        Installation(kind="package", version="0.21.0"), UpdateSettings(), fetch_text=fetch
    )
    assert (result.status, result.latest) == ("available", "v0.22.0")
    assert fetch.call_args.args[0] == updates.REPOSITORY_API + "/releases/latest"


def test_a_container_compares_its_build_stamp_with_master() -> None:
    """ADR-0016-03."""
    fetch = Mock(return_value=MASTER_SHA)
    current = Installation(
        kind="container", version="0.21.0", commit=MASTER_SHA[:7], branch="master"
    )
    unknown = Installation(kind="container", version="0.21.0", branch="master")
    assert updates.check_for_update(current, UpdateSettings(), fetch_text=fetch).status == "current"
    assert updates.check_for_update(unknown, UpdateSettings(), fetch_text=fetch).status == "unknown"


def test_the_api_address_cannot_leave_the_repository() -> None:
    """ADR-0016-03."""
    with pytest.raises(ValueError):
        updates._fetch_text("https://example.test/releases/latest", {}, 1.0)


def test_the_checker_reuses_a_result_until_forced(tmp_path: Path) -> None:
    """ADR-0016-03."""
    clock = Mock(return_value=100.0)
    check = Mock(
        return_value=updates.UpdateCheck(
            status="current", channel="stable", channel_source="installation", message="ok"
        )
    )
    checker = UpdateChecker(
        lambda: tmp_path,
        installation=lambda: Installation(kind="package", version="0.21.0"),
        check=check,
        clock=clock,
    )
    assert checker.cached() is None
    checker.result()
    checker.result()
    assert check.call_count == 1
    checker.result(force=True)
    assert check.call_count == 2
    clock.return_value = 100.0 + updates.CACHE_SECONDS + 1
    checker.result()
    assert check.call_count == 3
    checker.save(UpdateSettings(check=False))
    assert checker.cached() is None


# ---------------------------------------------------------------------------
# Restart detection
# ---------------------------------------------------------------------------


def test_restart_detection_prefers_the_launcher_loop(tmp_path: Path) -> None:
    """ADR-0016-04."""
    found = updates.restart_support(
        {updates.SUPERVISOR_ENV: "1"}, markers=(), systemd_policy=Mock(return_value=None)
    )
    assert (found.mode, found.available) == ("launcher", True)


def test_restart_detection_trusts_a_container_only_with_its_restart_policy(
    tmp_path: Path,
) -> None:
    """ADR-0016-04: a container file alone does not prove anything restarts it."""
    marker = tmp_path / ".containerenv"
    marker.touch()
    declared = updates.restart_support(
        {updates.RESTART_POLICY_ENV: "unless-stopped"}, markers=(marker,)
    )
    assert declared.mode == "container"
    for environ in ({}, {updates.RESTART_POLICY_ENV: "no"}):
        found = updates.restart_support(environ, markers=(marker,))
        assert found.mode is None
        assert updates.RESTART_POLICY_ENV in found.message


@pytest.mark.parametrize(
    "policy,mode", [("always", "systemd"), ("on-failure", "systemd"), ("no", None), (None, None)]
)
def test_restart_detection_reads_the_systemd_restart_policy(
    policy: str | None, mode: str | None
) -> None:
    """ADR-0016-04."""
    found = updates.restart_support(
        {"INVOCATION_ID": "x"},
        markers=(),
        systemd_policy=Mock(return_value=policy),
        platform="linux",
    )
    assert found.mode == mode
    if policy == "no":
        assert "Restart=no" in found.message


def test_restart_detection_without_a_supervisor_offers_nothing() -> None:
    """ADR-0016-04."""
    found = updates.restart_support({}, markers=(), platform="win32")
    assert not found.available
    assert "the way you started it" in found.message


def test_systemd_policy_comes_from_the_unit_in_the_cgroup(tmp_path: Path, monkeypatch) -> None:
    """ADR-0016-04."""
    cgroup = tmp_path / "cgroup"
    cgroup.write_text(
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/autodj.service\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(updates.shutil, "which", lambda name: "/usr/bin/" + name)
    runner = Mock(return_value=_done("Restart=on-failure\nMainPID=4242\n"))
    policy = updates.systemd_restart_policy(
        {"INVOCATION_ID": "x"}, runner=runner, cgroup_file=cgroup, pids=(4242, 1)
    )
    assert policy == "on-failure"
    assert runner.call_args.args[0] == [
        "systemctl",
        "--user",
        "show",
        "--property=Restart",
        "--property=MainPID",
        "autodj.service",
    ]
    assert runner.call_args.kwargs["timeout"] == 3.0
    assert updates.systemd_restart_policy({}, runner=runner, cgroup_file=cgroup) is None


def test_systemd_restart_needs_autodj_to_be_the_units_main_process(
    tmp_path: Path, monkeypatch
) -> None:
    """ADR-0016-04: another main process exiting would leave AutoDJ off the air."""
    cgroup = tmp_path / "cgroup"
    cgroup.write_text("0::/system.slice/autodj.service\n", encoding="utf-8")
    monkeypatch.setattr(updates.shutil, "which", lambda name: "/usr/bin/" + name)
    runner = Mock(return_value=_done("Restart=always\nMainPID=99\n"))
    assert (
        updates.systemd_restart_policy(
            {"INVOCATION_ID": "x"}, runner=runner, cgroup_file=cgroup, pids=(4242, 1)
        )
        is None
    )
    assert "--user" not in runner.call_args.args[0]


# ---------------------------------------------------------------------------
# The notice: release notes and commands
# ---------------------------------------------------------------------------


def test_an_available_release_links_its_release_notes(tmp_path: Path) -> None:
    """ADR-0016-09."""
    result = updates.check_for_update(
        _git_checkout(tmp_path, version="0.19.1", branch=None, tag="v0.19.1"),
        UpdateSettings(),
        runner=_ls_remote(tags=["v0.21.0"]),
    )
    assert result.notes_url == updates.REPOSITORY_URL + "/releases/tag/v0.21.0"
    development = updates.check_for_update(
        _git_checkout(tmp_path), UpdateSettings(), runner=_ls_remote(master=MASTER_SHA)
    )
    assert development.notes_url == updates.REPOSITORY_URL + "/blob/master/CHANGELOG.md"
    assert updates.release_notes_url(None) is None
    assert updates.release_notes_url("v0.21.0; rm -rf /") is None


def test_the_notice_gives_the_commands_for_each_installation(tmp_path: Path) -> None:
    """ADR-0016-09: AutoDJ names the commands; it never runs them."""
    available = updates.UpdateCheck(
        status="available",
        channel="stable",
        channel_source="installation",
        message="Release v0.22.0 is available.",
        latest="v0.22.0",
        target="v0.22.0",
    )
    checkout = updates.update_commands(_git_checkout(tmp_path), available)
    assert checkout == [
        "git fetch --tags origin",
        "git switch --detach v0.22.0",
        "./autodj setup",
        "./autodj doctor --no-fix",
    ]
    container = updates.update_commands(Installation(kind="container", version="0.21.0"), available)
    assert container[-2:] == [
        "AUTODJ_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build",
        "AUTODJ_COMMIT=$(git rev-parse --short HEAD) podman compose up -d --build",
    ]
    package = updates.update_commands(Installation(kind="package", version="0.21.0"), available)
    assert package == [
        'uv pip install "autodj[all] @ '
        f'{updates.REPOSITORY_URL}/releases/download/v0.22.0/autodj-0.22.0-py3-none-any.whl"'
    ]
    master = replace(available, channel="development", latest="1234567", target="master")
    assert updates.update_commands(_git_checkout(tmp_path), master)[:3] == [
        "git fetch origin",
        "git switch master",
        "git merge --ff-only origin/master",
    ]
    assert updates.update_commands(Installation(kind="package", version="0.21.0"), master) == []
    assert (
        updates.update_commands(_git_checkout(tmp_path), replace(available, status="current")) == []
    )


# ---------------------------------------------------------------------------
# Bounded git and one check at a time
# ---------------------------------------------------------------------------


def test_a_timeout_ends_the_whole_process_tree_without_waiting_on_its_pipes() -> None:
    """ADR-0016-03: a grandchild holding the output pipe (git's transport) cannot hold the check."""
    import sys
    import time

    grandchild = "import time; time.sleep(30)"
    child = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); time.sleep(30)"
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        updates.run_bounded([sys.executable, "-c", child], timeout=1.0)
    assert time.monotonic() - started < 10


def test_git_never_asks_for_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0016-03."""
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    environment = updates._git_environment()
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GCM_INTERACTIVE"] == "never"
    assert environment["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i my-key")
    assert updates._git_environment()["GIT_SSH_COMMAND"] == "ssh -i my-key"


def test_callers_during_a_slow_check_wait_for_it_instead_of_starting_more(
    tmp_path: Path,
) -> None:
    """ADR-0016-03."""
    import itertools
    import threading
    import time

    ticks = itertools.count()
    release = threading.Event()
    calls = []

    def slow_check(installation: Installation, settings: UpdateSettings) -> updates.UpdateCheck:
        calls.append(1)
        release.wait(5)
        return updates.UpdateCheck(
            status="current", channel="stable", channel_source="installation", message="ok"
        )

    checker = UpdateChecker(
        lambda: tmp_path,
        installation=lambda: Installation(kind="package", version="0.21.0"),
        check=slow_check,
        clock=lambda: float(next(ticks)),
    )
    results = []
    threads = [
        threading.Thread(target=lambda force=force: results.append(checker.result(force=force)))
        for force in (False, True, False, True)
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.3)
    release.set()
    for thread in threads:
        thread.join(5)
    assert len(calls) == 1
    assert len(results) == 4
