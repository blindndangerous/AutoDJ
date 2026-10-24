"""Automatic library rescan timing and catalog persistence."""

from unittest.mock import Mock, call

import pytest

from autodj.library import LibraryManager
from tests.unit.test_library import Similarity, make_config


@pytest.fixture
def timed_manager(tmp_path):
    now = [100.0]
    manager = LibraryManager(make_config(tmp_path), Similarity(), clock=lambda: now[0])
    manager.flush_ledgers = Mock()
    manager._changed = Mock(return_value=False)
    manager.schedule_analysis = Mock()
    manager.start_scan = Mock(return_value=True)
    yield manager, now
    manager.close()


def test_auto_rescan_off_never_starts(timed_manager):
    manager, now = timed_manager
    now[0] += 100_000
    manager.tick()
    manager.start_scan.assert_not_called()


def test_auto_rescan_starts_at_interval_without_tag_override(timed_manager):
    manager, now = timed_manager
    manager.set_auto_rescan_minutes(10)
    now[0] += 599
    manager.tick()
    manager.start_scan.assert_not_called()
    now[0] += 1
    manager.tick()
    manager.start_scan.assert_called_once_with(override_tags=False)


def test_auto_rescan_waits_for_running_scan(timed_manager):
    manager, now = timed_manager
    manager.set_auto_rescan_minutes(10)
    now[0] += 600
    manager._scan_lock.acquire()
    try:
        manager.tick()
        manager.start_scan.assert_not_called()
    finally:
        manager._scan_lock.release()
    manager.tick()
    manager.start_scan.assert_called_once_with(override_tags=False)


def test_scan_completion_resets_auto_rescan_timer(timed_manager):
    manager, now = timed_manager
    manager.set_auto_rescan_minutes(10)
    now[0] += 500
    manager._scan = Mock()
    manager._scan_lock.acquire()
    manager._scan_queued()
    assert not manager._scan_lock.locked()
    now[0] += 599
    manager.tick()
    manager.start_scan.assert_not_called()
    now[0] += 1
    manager.tick()
    manager.start_scan.assert_called_once_with(override_tags=False)


def test_failed_scan_still_resets_auto_rescan_timer(timed_manager):
    manager, now = timed_manager
    manager.set_auto_rescan_minutes(10)
    now[0] += 500
    manager._scan = Mock(side_effect=RuntimeError("scan failed"))
    manager._scan_lock.acquire()
    with pytest.raises(RuntimeError, match="scan failed"):
        manager._scan_queued()
    assert not manager._scan_lock.locked()
    now[0] += 599
    manager.tick()
    manager.start_scan.assert_not_called()


def test_rejected_auto_rescan_retries_next_tick(timed_manager):
    manager, now = timed_manager
    manager.set_auto_rescan_minutes(10)
    manager.start_scan.side_effect = [False, True]
    now[0] += 600
    manager.tick()
    now[0] += 2
    manager.tick()
    assert manager.start_scan.call_args_list == [
        call(override_tags=False),
        call(override_tags=False),
    ]


def test_auto_rescan_setting_survives_manager_reopen(tmp_path):
    cfg = make_config(tmp_path)
    first = LibraryManager(cfg, Similarity(), clock=lambda: 0.0)
    assert first.auto_rescan_minutes() == 0
    first.set_auto_rescan_minutes(90)
    assert first.catalog.get_setting("auto_rescan_minutes") == 90
    first.close()
    second = LibraryManager(cfg, Similarity(), clock=lambda: 0.0)
    try:
        assert second.auto_rescan_minutes() == 90
    finally:
        second.close()
