"""Persistence stores share atomic replacement, cleanup and private-file modes."""

import os
import stat
import sys
from functools import partial

import pytest

from autodj import fsutil
from autodj.carts import CartStore
from autodj.clockwheel_store import ClockwheelStore
from autodj.config import load_config
from autodj.live import DjCredentials, TalkoverSettings
from autodj.time_checks import TimeChecks


@pytest.fixture(params=["carts", "clockwheel", "live", "time_checks"])
def saved_store(request, tmp_path):
    if request.param == "carts":
        store = CartStore(tmp_path)
        save = store.save
        path = store.path
    elif request.param == "clockwheel":
        store = ClockwheelStore(tmp_path)
        document = {
            "version": 1,
            "name": "Morning",
            "steps": [{"type": "music", "source": "src1"}],
            "resync_minutes": [0],
        }
        save = partial(store.save, document)
        path = tmp_path / "Morning.json"
    elif request.param == "live":
        store = DjCredentials(tmp_path / "live_djs.json")
        save = partial(store.save_settings, TalkoverSettings())
        path = store.path
    else:
        store = TimeChecks(load_config(environ={"AUTODJ_INDEX_DIR": str(tmp_path)}), "UTC")
        save = partial(store.save, store.settings)
        path = store.state_path
    save()
    if request.param == "carts":
        store.wall.active_page = 2
    elif request.param == "clockwheel":
        document["resync_minutes"] = [0, 30]
    elif request.param == "live":
        save = partial(store.save_settings, TalkoverSettings(mic_return_seconds=2, mic_duck_db=20))
    else:
        save = partial(store.save, store.settings.model_copy(update={"enabled": True}))
    return path, save, request.param


@pytest.mark.parametrize("operation", ["replace", "fsync"])
def test_failed_store_write_keeps_old_file_and_cleans_temporary(
    saved_store, monkeypatch, operation
):
    """ADR-0010-06: every consolidated store cleans failed temporary output."""
    path, save, _ = saved_store
    original = path.read_bytes()
    before = set(path.parent.iterdir())

    def fail(*args):
        raise OSError("storage failure")

    monkeypatch.setattr(os, operation, fail)
    with pytest.raises(OSError, match="storage failure"):
        save()
    assert path.read_bytes() == original
    assert set(path.parent.iterdir()) == before


def test_store_write_retries_windows_sharing_violation(saved_store, monkeypatch):
    path, save, _ = saved_store
    original = path.read_bytes()
    replace = os.replace
    calls = 0

    def held_once(source, destination):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("reader holds file")
        replace(source, destination)

    monkeypatch.setattr(fsutil, "_IS_WINDOWS", True)
    monkeypatch.setattr(fsutil.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(os, "replace", held_once)
    save()
    assert calls == 2
    assert path.is_file()
    assert path.read_bytes() != original
    assert not list(path.parent.glob("*.tmp"))


def test_store_write_succeeds_when_directory_flush_is_refused(saved_store, monkeypatch):
    path, save, _ = saved_store
    original = path.read_bytes()

    def refuse(directory):
        raise OSError("directory flush refused")

    monkeypatch.setattr(fsutil, "fsync_directory", refuse)
    save()
    assert path.read_bytes() != original
    assert not list(path.parent.glob("*.tmp"))


def test_time_checks_publish_saved_state_when_directory_flush_is_refused(tmp_path, monkeypatch):
    """ADR-0010-08: a committed time-check save updates memory and the seen-file flag."""
    store = TimeChecks(load_config(environ={"AUTODJ_INDEX_DIR": str(tmp_path)}), "UTC")
    settings = store.settings.model_copy(update={"enabled": True})
    assert not store._seen

    def refuse(directory):
        raise OSError("directory flush refused")

    monkeypatch.setattr(fsutil, "fsync_directory", refuse)
    store.save(settings)
    assert store._seen
    assert store.settings == settings
    assert TimeChecks(store.cfg, "UTC").settings == settings


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions")
def test_store_write_preserves_private_permissions(saved_store):
    path, _, kind = saved_store
    if kind != "carts":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
