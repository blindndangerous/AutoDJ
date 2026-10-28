"""The update check, its channels and settings, and restart detection (autodj.updates)."""

from __future__ import annotations

import json
import subprocess
import sys
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
        if args[:2] == ["config", "--get"]:
            return _done(returncode=1)
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


def test_a_published_image_follows_the_channel_it_was_published_for(tmp_path: Path) -> None:
    """ADR-0016-01: not the branch of the checkout that holds compose.yaml."""
    marker = tmp_path / ".dockerenv"
    marker.touch()
    config = tmp_path / "config"
    (config / ".git").mkdir(parents=True)
    (config / ".git" / "HEAD").write_text("ref: refs/heads/master\n", encoding="ascii")
    image = updates.IMAGE_REPOSITORY + ":stable"

    found = updates.detect_installation(
        root=tmp_path / "no-checkout",
        environ={
            "AUTODJ_COMMIT": MASTER_SHA,
            "AUTODJ_IMAGE": image,
            "AUTODJ_IMAGE_CHANNEL": "stable",
        },
        markers=(marker,),
        version="0.22.0",
        working_dir=config,
    )
    assert (found.kind, found.branch, found.image, found.image_channel) == (
        "container",
        None,
        image,
        "stable",
    )
    assert found.inferred_channel == "stable"

    development = updates.detect_installation(
        root=tmp_path / "no-checkout",
        environ={"AUTODJ_IMAGE": "ghcr.io/a/b:x; rm -rf /", "AUTODJ_IMAGE_CHANNEL": "development"},
        markers=(marker,),
        version="0.22.0",
        working_dir=tmp_path,
    )
    assert (development.image, development.inferred_channel) == (None, "development")


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
        _git_checkout(tmp_path, branch=None),
        UpdateSettings(),
        runner=_ls_remote(tags=["v0.21.0"]),
    )
    assert result.status == "ahead"
    assert "after the latest release" in result.message


@pytest.mark.parametrize("version", ["0.21.0", "0.22.0"])
def test_development_on_a_saved_stable_channel_waits_for_the_next_release(
    tmp_path: Path, version: str
) -> None:
    """ADR-0016-19: never a downgrade; the release that includes this commit is the update."""
    waiting = updates.check_for_update(
        _git_checkout(tmp_path, version=version),
        UpdateSettings(channel="stable"),
        runner=_ls_remote(tags=["v0.21.0"]),
    )
    assert waiting.status == "waiting-for-release"
    assert waiting.target is None
    assert waiting.message == (
        "You'll move to Stable when the next release includes your version. "
        f"Installed: {version}, commit {LOCAL_SHA[:7]}, on master. Latest release: v0.21.0."
    )
    released = updates.check_for_update(
        _git_checkout(tmp_path, version="0.21.0"),
        UpdateSettings(channel="stable"),
        runner=_ls_remote(tags=["v0.21.0"], ancestor=True),
    )
    assert released.status == "available"
    assert released.target == "v0.21.0"
    # A newer release that does not include this commit does not count either.
    later = updates.check_for_update(
        _git_checkout(tmp_path, version="0.21.0"),
        UpdateSettings(channel="stable"),
        runner=_ls_remote(tags=["v0.22.0"], ancestor=False),
    )
    assert later.status == "waiting-for-release"


def test_an_installation_without_git_asks_github_whether_the_release_includes_it(
    tmp_path: Path,
) -> None:
    """ADR-0016-19: GitHub's compare view says whether the commit is behind the tag."""
    asked: list[str] = []

    def fetch(url: str, _headers: object, _timeout: float) -> str:
        asked.append(url)
        if url.endswith("/releases/latest"):
            return json.dumps({"tag_name": "v0.21.0"})
        return json.dumps({"status": status})

    container = Installation(kind="container", version="0.21.0", commit="abcdef1", branch="master")
    status = "ahead"
    waiting = updates.check_for_update(
        container, UpdateSettings(channel="stable"), fetch_text=fetch
    )
    assert waiting.status == "waiting-for-release"
    assert asked[-1] == updates.REPOSITORY_API + "/compare/v0.21.0...abcdef1"
    status = "behind"
    assert updates.contained_in_release(container, "v0.21.0", fetch_text=fetch) is True
    assert updates.contained_in_release(container, "nightly", fetch_text=fetch) is None


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


def test_systemd_counts_the_parent_only_when_it_is_autodjs_launcher(
    tmp_path: Path, monkeypatch
) -> None:
    """ADR-0016-04: a wrapper that does not pass on status 75 gets no button."""
    cgroup = tmp_path / "cgroup"
    cgroup.write_text("0::/system.slice/autodj.service\n", encoding="utf-8")
    monkeypatch.setattr(updates.shutil, "which", lambda name: "/usr/bin/" + name)
    parent = updates.os.getppid()
    runner = Mock(return_value=_done(f"Restart=on-failure\nMainPID={parent}\n"))
    wrapped = {"INVOCATION_ID": "x"}
    launched = {"INVOCATION_ID": "x", updates.SUPERVISOR_ENV: "1"}
    assert updates.systemd_restart_policy(wrapped, runner=runner, cgroup_file=cgroup) is None
    assert (
        updates.systemd_restart_policy(launched, runner=runner, cgroup_file=cgroup) == "on-failure"
    )


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
    # The changelog's Unreleased section stays empty between releases, so
    # development links the commits between this one and master.
    assert development.notes_url == (f"{updates.REPOSITORY_URL}/compare/{LOCAL_SHA}...master")
    unknown = updates.check_for_update(
        _git_checkout(tmp_path, commit=None), UpdateSettings(), runner=_ls_remote()
    )
    assert unknown.status == "unknown"
    assert updates.release_notes_url("master", None) == updates.REPOSITORY_URL + "/commits/master"
    assert updates.release_notes_url("master", "abc1234") == (
        updates.REPOSITORY_URL + "/commits/master"
    )
    assert (
        updates.release_notes_url("master", "x" * 40) == updates.REPOSITORY_URL + "/commits/master"
    )
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
    built_here = updates.update_commands(
        Installation(kind="container", version="0.21.0", image="autodj:local"), available
    )
    assert built_here == [
        "git fetch --tags origin",
        "git switch --detach v0.22.0",
        "AUTODJ_COMMIT=$(git rev-parse --short HEAD) "
        "docker compose -f compose.yaml -f compose.build.yaml up -d --build",
        "AUTODJ_COMMIT=$(git rev-parse --short HEAD) "
        "podman compose -f compose.yaml -f compose.build.yaml up -d --build",
    ]
    published = Installation(
        kind="container",
        version="0.21.0",
        image=updates.IMAGE_REPOSITORY + ":stable",
        image_channel="stable",
    )
    assert updates.update_commands(published, available) == [
        "docker compose pull && docker compose up -d",
        "podman compose pull && podman compose up -d",
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
    assert updates.update_commands(replace(published, image_channel="development"), master)[
        -2:
    ] == [
        "docker compose pull && docker compose up -d",
        "podman compose pull && podman compose up -d",
    ]
    assert (
        updates.update_commands(_git_checkout(tmp_path), replace(available, status="current")) == []
    )


def test_a_container_on_another_channel_or_pinned_is_told_which_image_to_name() -> None:
    """ADR-0016-09: pulling updates only a moving tag on the image's own channel."""
    release = updates.UpdateCheck(
        status="available",
        channel="stable",
        channel_source="installation",
        message="Release v0.22.0 is available.",
        latest="v0.22.0",
        target="v0.22.0",
    )
    development = replace(release, channel="development", latest="1234567", target="master")
    repository = "registry.example:5000/radio/autodj"

    def switch(image: str | None, channel: str, result: updates.UpdateCheck) -> str | None:
        container = Installation(
            kind="container", version="0.21.0", image=image, image_channel=channel
        )
        commands = updates.update_commands(container, result)
        assert commands[-2:] == [
            "docker compose pull && docker compose up -d",
            "podman compose pull && podman compose up -d",
        ]
        prefix = "# In .env beside compose.yaml, set AUTODJ_IMAGE="
        return commands[0].removeprefix(prefix) if commands[0].startswith(prefix) else None

    assert switch(f"{repository}:stable", "stable", release) is None
    assert switch(f"{repository}:latest", "stable", release) is None
    assert switch(f"{repository}:dev", "development", development) is None
    assert switch(None, "stable", release) is None
    assert switch(None, "development", development) is None
    assert switch(f"{repository}:0.21.0", "stable", release) == f"{repository}:0.22.0"
    assert switch(f"{repository}@sha256:{'c' * 64}", "stable", release) == f"{repository}:0.22.0"
    assert switch(f"{repository}:stable", "stable", development) == f"{repository}:dev"
    assert switch(f"{repository}:dev", "development", release) == f"{repository}:stable"
    assert switch(f"{repository}:sha-abcdef0", "development", development) == (
        f"{repository}:sha-1234567"
    )
    assert switch(None, "stable", development) == updates.IMAGE_REPOSITORY + ":dev"


def test_a_cuda_image_reads_its_variant_and_a_cpu_image_has_none_to_lose(tmp_path: Path) -> None:
    """ADR-0016-22: the Containerfile names the variant; anything else is unknown."""
    marker = tmp_path / ".dockerenv"
    marker.touch()

    def variant(environ: dict[str, str]) -> str | None:
        found = updates.detect_installation(
            root=tmp_path / "no-checkout",
            environ=environ,
            markers=(marker,),
            version="0.22.0",
            working_dir=tmp_path,
        )
        return found.image_variant

    assert variant({"AUTODJ_IMAGE_CHANNEL": "stable", "AUTODJ_IMAGE_VARIANT": "cuda"}) == "cuda"
    assert variant({"AUTODJ_IMAGE_CHANNEL": "development", "AUTODJ_IMAGE_VARIANT": "cpu"}) == "cpu"
    assert variant({"AUTODJ_IMAGE_VARIANT": "cuda"}) == "cuda"
    assert variant({"AUTODJ_IMAGE_CHANNEL": "stable", "AUTODJ_IMAGE_VARIANT": "rocm"}) is None
    assert variant({"AUTODJ_IMAGE_CHANNEL": "stable"}) is None


def test_a_cuda_image_is_told_its_own_cuda_tag_and_a_cpu_image_never_is() -> None:
    """ADR-0016-22: an update never swaps the CUDA image for the CPU one, or back."""
    release = updates.UpdateCheck(
        status="available",
        channel="stable",
        channel_source="installation",
        message="Release v0.22.0 is available.",
        latest="v0.22.0",
        target="v0.22.0",
    )
    development = replace(release, channel="development", latest="1234567", target="master")
    repository = "registry.example:5000/radio/autodj"

    def switch(
        image: str | None, channel: str, variant: str | None, result: updates.UpdateCheck
    ) -> str | None:
        container = Installation(
            kind="container",
            version="0.21.0",
            image=image,
            image_channel=channel,
            image_variant=variant,
        )
        commands = updates.update_commands(container, result)
        assert commands[-2:] == [
            "docker compose pull && docker compose up -d",
            "podman compose pull && podman compose up -d",
        ]
        prefix = "# In .env beside compose.yaml, set AUTODJ_IMAGE="
        return commands[0].removeprefix(prefix) if commands[0].startswith(prefix) else None

    # Pulling a moving CUDA tag on its own channel is enough.
    assert switch(f"{repository}:stable-cuda", "stable", "cuda", release) is None
    assert switch(f"{repository}:dev-cuda", "development", "cuda", development) is None
    assert switch(None, "stable", "cuda", release) is None
    assert switch(None, "development", "cuda", development) is None
    # Pinned or on the other channel: the new tag keeps the suffix.
    assert switch(f"{repository}:0.21.0-cuda", "stable", "cuda", release) == (
        f"{repository}:0.22.0-cuda"
    )
    assert switch(f"{repository}@sha256:{'c' * 64}", "stable", "cuda", release) == (
        f"{repository}:0.22.0-cuda"
    )
    assert switch(f"{repository}:stable-cuda", "stable", "cuda", development) == (
        f"{repository}:dev-cuda"
    )
    assert switch(f"{repository}:dev-cuda", "development", "cuda", release) == (
        f"{repository}:stable-cuda"
    )
    assert switch(f"{repository}:sha-abcdef0-cuda", "development", "cuda", development) == (
        f"{repository}:sha-1234567-cuda"
    )
    assert switch(None, "stable", "cuda", development) == updates.IMAGE_REPOSITORY + ":dev-cuda"
    # There is no latest-cuda; the CUDA image's moving release tag is stable-cuda.
    assert switch(f"{repository}:latest", "stable", "cuda", release) == (
        f"{repository}:stable-cuda"
    )
    # The image says what it is, whatever .env names: never the other variant's tag.
    assert switch(f"{repository}:stable", "stable", "cuda", release) == (
        f"{repository}:stable-cuda"
    )
    assert switch(f"{repository}:stable-cuda", "stable", "cpu", release) == f"{repository}:stable"
    assert switch(f"{repository}:0.21.0-cuda", "stable", "cpu", release) == f"{repository}:0.22.0"
    assert switch(f"{repository}:latest", "stable", "cpu", release) is None
    assert switch(f"{repository}:dev", "development", "cpu", release) == f"{repository}:stable"
    # An image that does not say its variant keeps its tag's.
    assert switch(f"{repository}:0.21.0-cuda", "stable", None, release) == (
        f"{repository}:0.22.0-cuda"
    )
    assert switch(f"{repository}:0.21.0", "stable", None, release) == f"{repository}:0.22.0"


def test_a_pinned_cuda_tag_may_still_be_building_and_a_local_cuda_build_keeps_its_gpu() -> None:
    """ADR-0016-22: the CUDA build is longer, and -f overrides COMPOSE_FILE."""
    release = updates.UpdateCheck(
        status="available",
        channel="stable",
        channel_source="installation",
        message="Release v0.22.0 is available.",
        latest="v0.22.0",
        target="v0.22.0",
    )
    development = replace(release, channel="development", latest="1234567", target="master")
    repository = "registry.example:5000/radio/autodj"

    def notice(image: str, channel: str, variant: str, result: updates.UpdateCheck) -> list[str]:
        container = Installation(
            kind="container",
            version="0.21.0",
            image=image,
            image_channel=channel,
            image_variant=variant,
        )
        return updates.update_commands(container, result)[:-2]

    building = "may still be building; if pull says manifest unknown, try again later"
    pinned = notice(f"{repository}:0.21.0-cuda", "stable", "cuda", release)
    assert pinned[0].endswith(f"={repository}:0.22.0-cuda")
    assert pinned[1] == f"# The CUDA image 0.22.0-cuda {building}"
    commit = notice(f"{repository}:sha-abcdef0-cuda", "development", "cuda", development)
    assert commit[1] == f"# The CUDA image sha-1234567-cuda {building}"
    # A moving tag pulls the previous image meanwhile, and the CPU image is not delayed.
    assert len(notice(f"{repository}:dev-cuda", "development", "cuda", release)) == 1
    assert len(notice(f"{repository}:stable-cuda", "stable", "cuda", development)) == 1
    assert len(notice(f"{repository}:0.21.0", "stable", "cpu", release)) == 1

    def local(variant: str | None) -> list[str]:
        built = Installation(
            kind="container", version="0.21.0", image="autodj:local", image_variant=variant
        )
        return updates.update_commands(built, release)[-2:]

    build = "AUTODJ_COMMIT=$(git rev-parse --short HEAD)"
    assert local("cuda") == [
        f"{build} docker compose -f compose.yaml -f compose.gpu.yaml -f compose.build.yaml "
        "up -d --build",
        f"{build} podman compose -f compose.yaml -f compose.gpu.yaml -f compose.build.yaml "
        "up -d --build",
    ]
    for variant in ("cpu", None):
        assert local(variant) == [
            f"{build} docker compose -f compose.yaml -f compose.build.yaml up -d --build",
            f"{build} podman compose -f compose.yaml -f compose.build.yaml up -d --build",
        ]


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
    environment = updates._git_environment("ssh -o BatchMode=yes")
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GCM_INTERACTIVE"] == "never"
    assert environment["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i my-key")
    assert updates._git_environment("ssh -o BatchMode=yes")["GIT_SSH_COMMAND"] == "ssh -i my-key"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("ssh -i ~/.ssh/station", "ssh -i ~/.ssh/station -o BatchMode=yes"), (None, None)],
)
def test_the_checkouts_own_ssh_command_is_kept_and_told_not_to_ask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: str | None, expected: str | None
) -> None:
    """ADR-0016-03: core.sshCommand still applies; only BatchMode is added."""
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    seen: dict[str, str] = {}

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        args = command[1:]
        if args == ["config", "--get", "core.sshCommand"]:
            return _done(configured + "\n" if configured else "", 0 if configured else 1)
        if args[:3] == ["ls-remote", "--tags", "--refs"]:
            seen.update(kwargs["env"])  # type: ignore[arg-type]
            return _done(f"{'c' * 40}\trefs/tags/v0.21.0\n")
        raise AssertionError(args)

    updates.check_for_update(
        _git_checkout(tmp_path, branch=None, tag="v0.21.0"),
        UpdateSettings(),
        runner=Mock(side_effect=run),
    )
    assert seen["GIT_SSH_COMMAND"] == (expected or "ssh -o BatchMode=yes")


def test_the_operators_git_ssh_command_wins(tmp_path: Path, monkeypatch) -> None:
    """ADR-0016-03."""
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -F my-config")
    runner = Mock(return_value=_done(f"{'c' * 40}\trefs/tags/v0.21.0\n"))
    updates.check_for_update(
        _git_checkout(tmp_path, branch=None, tag="v0.21.0"), UpdateSettings(), runner=runner
    )
    (call,) = runner.call_args_list
    assert call.args[0][1] == "ls-remote"
    assert call.kwargs["env"]["GIT_SSH_COMMAND"] == "ssh -F my-config"


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


def test_a_timeout_does_not_wait_for_a_pipe_holder_that_left_the_tree() -> None:
    """ADR-0016-03: a holder whose parent has exited still cannot hold the check."""
    import sys
    import time

    holder = "import time; time.sleep(25)"
    middle = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {holder!r}])"
    child = (
        "import subprocess, sys, time; "
        f"subprocess.run([sys.executable, '-c', {middle!r}]); time.sleep(60)"
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        updates.run_bounded([sys.executable, "-c", child], timeout=2.0)
    assert time.monotonic() - started < 5


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_closing_a_checks_job_ends_exactly_the_processes_in_it() -> None:
    """ADR-0016-03: a per-check job object, not taskkill /T by parent process IDs."""
    from autodj.child_jobs import end_job, job_for_tree

    inside = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    outside = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        job = job_for_tree(inside)
        assert job is not None
        end_job(job)
        inside.wait(timeout=5)  # ended at once, not after its 30 s
        assert outside.poll() is None
    finally:
        for process in (inside, outside):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


# ---------------------------------------------------------------------------
# Updating from the web page
# ---------------------------------------------------------------------------


def test_only_a_checkout_under_an_updating_launcher_can_update(tmp_path: Path) -> None:
    """ADR-0016-11: other installations update the way they were installed."""
    checkout = _git_checkout(tmp_path)
    launched = {updates.SUPERVISOR_ENV: "1", updates.UPDATER_ENV: "launcher"}
    support = updates.update_support(checkout, launched)
    assert support.available is True
    assert support.mechanism == "launcher"
    # A launcher from before the web update restarts but cannot update.
    older = updates.update_support(checkout, {updates.SUPERVISOR_ENV: "1"})
    assert older.available is False
    assert older.message == "Update it the way you installed it."
    assert older.guide_url.endswith("backups-and-updates.md#update-a-source-checkout")
    container = updates.update_support(Installation(kind="container", version="0.21.0"), launched)
    assert container.available is False
    assert container.guide_url.endswith("containers-and-windows.md#update-the-container")
    package = updates.update_support(Installation(kind="package", version="0.21.0"), launched)
    assert package.available is False
    assert package.guide_url.endswith("backups-and-updates.md#update-a-wheel-installation")
    assert support.to_dict()["available"] is True
    assert updates.UPDATE_EXIT_CODE not in (0, updates.RESTART_EXIT_CODE)


@pytest.mark.parametrize(
    ("ancestor", "words"),
    [(0, None), (1, "has moved away from 0.21.0"), (128, "cannot tell whether")],
)
def test_go_back_fits_only_while_the_checkout_came_from_that_release(
    tmp_path: Path, ancestor: int, words: str | None
) -> None:
    """ADR-0016-20: a checkout moved by hand drops the offer, with one sentence.

    Development that has passed a newer release still fits; a checkout at a
    release does not.
    """
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(command[1:])
        if command[1:3] == ["merge-base", "--is-ancestor"]:
            return _done(returncode=ancestor)
        raise AssertionError(command)

    reason = updates.go_back_moved(_git_checkout(tmp_path), LOCAL_SHA, "0.21.0", runner=run)
    assert calls == [["merge-base", "--is-ancestor", LOCAL_SHA, "HEAD"]]
    if words is None:
        assert reason is None
    else:
        assert reason is not None and words in reason
    released = _git_checkout(tmp_path, branch=None, tag="v0.22.0")
    assert updates.go_back_moved(released, LOCAL_SHA, "0.21.0", runner=run) == (
        "This checkout runs release v0.22.0 now, so going back to 0.21.0 is not offered."
    )
    package = Installation(kind="package", version="0.22.0")
    assert "cannot tell" in (updates.go_back_moved(package, LOCAL_SHA, "0.21.0", runner=run) or "")
