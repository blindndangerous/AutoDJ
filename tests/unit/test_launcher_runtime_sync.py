"""Managed package synchronization runs before a server starts."""

import json
from unittest.mock import Mock

import pytest

from autodj import launcher, runtime_sync
from autodj.update_contract import try_lock, unlock
from autodj.version import STARTUP_WELCOME_ENV


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    for name in runtime_sync.INPUTS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("input", encoding="utf-8")
    environment = root / ".uv/runtime"
    python = launcher._python_path(environment)
    python.parent.mkdir(parents=True)
    python.write_text("interpreter", encoding="utf-8")
    state = root / launcher.STATE
    state.write_text(json.dumps({"environment": str(environment), "backend": "no-model"}))
    monkeypatch.setattr(launcher, "ROOT", root)
    monkeypatch.setenv("AUTODJ_RUNTIME", "")
    monkeypatch.setenv("AUTODJ_BOOTSTRAP", "1")
    monkeypatch.delenv(STARTUP_WELCOME_ENV, raising=False)
    return root, environment


@pytest.mark.parametrize("changed", runtime_sync.INPUTS)
def test_dependency_input_changes_require_sync(checkout, changed):
    """ADR-0012-13: unchanged starts are cheap; changed locked inputs need synchronization."""
    root, environment = checkout
    runtime_sync.mark_synced(root, environment, "no-model")
    assert not runtime_sync.needs_sync(root, environment, "no-model")
    (root / changed).write_text("changed", encoding="utf-8")
    assert runtime_sync.needs_sync(root, environment, "no-model")


def test_external_selection_is_not_claimed(checkout):
    root, environment = checkout
    (root / launcher.STATE).write_text(json.dumps({"external": True}))
    assert not runtime_sync.needs_sync(root, environment, "no-model")


def test_sync_completes_before_supervisor(checkout, monkeypatch):
    """ADR-0012-13: install before serving, then skip install at the next start."""
    events = []

    def synchronize(*args):
        events.append("sync")
        runtime_sync.mark_synced(*args)

    monkeypatch.setattr(launcher.runpy, "run_path", Mock(return_value={"synchronize": synchronize}))
    monkeypatch.setattr(launcher.sys, "argv", ["autodj", "serve"])
    monkeypatch.setattr(launcher, "_supervise", lambda *_: events.append("serve") or 0)
    assert launcher.main() == 0
    assert launcher.main() == 0
    assert events == ["sync", "serve", "serve"]


def test_install_failure_prevents_server_and_releases_lock(checkout, monkeypatch, capsys):
    """ADR-0012-13: installation failure keeps the server stopped and allows a retry."""
    root, environment = checkout
    helper = Mock(side_effect=RuntimeError("install failed"))
    monkeypatch.setattr(launcher.runpy, "run_path", Mock(return_value={"synchronize": helper}))
    monkeypatch.setattr(launcher.sys, "argv", ["autodj", "serve"])
    server = Mock()
    monkeypatch.setattr(launcher, "_supervise", server)
    assert launcher.main() == 1
    captured = capsys.readouterr()
    assert captured.out.startswith("Welcome to AutoDJ ")
    assert "install failed" in captured.err
    server.assert_not_called()
    assert runtime_sync.needs_sync(root, environment, "no-model")
    handle = try_lock(root / ".uv/update/lock")
    assert handle is not None
    unlock(handle)


def test_competing_update_prevents_sync(checkout, monkeypatch):
    root, environment = checkout
    handle = try_lock(root / ".uv/update/lock")
    helper = Mock()
    monkeypatch.setattr(launcher.runpy, "run_path", helper)
    try:
        assert not launcher.sync_runtime((environment, "no-model"))
        helper.assert_not_called()
    finally:
        unlock(handle)


@pytest.mark.parametrize("arguments", [["doctor"], ["--help"], ["serve", "--help"]])
def test_diagnostics_and_help_do_not_install(checkout, monkeypatch, arguments):
    monkeypatch.setattr(launcher.sys, "argv", ["autodj", *arguments])
    sync = Mock()
    monkeypatch.setattr(launcher, "sync_runtime", sync)
    monkeypatch.setattr(launcher.subprocess, "run", Mock(return_value=Mock(returncode=0)))
    monkeypatch.setattr(launcher, "_supervise", Mock(return_value=0))
    assert launcher.main() == 0
    sync.assert_not_called()
