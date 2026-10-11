"""Recorder file lifecycle and failure isolation."""

from __future__ import annotations

import errno
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from autodj import recorder as recorder_module
from autodj.config import RecordingConfig
from autodj.recorder import Recorder, _disk_error, _LowSpaceError


def _wait_for(predicate, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("recorder did not reach the expected state")


def _recorder(
    tmp_path: Path,
    *,
    split: str = "show",
    floor: int = 0,
    name: str = "My / Station",
    zone: str = "UTC",
) -> Recorder:
    return Recorder(
        RecordingConfig(tmp_path / "archives", split, 0, floor),
        tmp_path / "index" / "recording.json",
        lambda: name,
        zone,
    )


def test_records_encoded_bytes_splits_and_restores_running_choice(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    try:
        recorder.start()
        recorder.feed(b"first", "ignored title")
        _wait_for(lambda: recorder.status()["bytes"] == 5)
        first = recorder.status()["file"]
        assert first is not None and first.startswith("My-Station-")
        recorder.split()
        recorder.feed(b"second", "")
        _wait_for(lambda: recorder.status()["bytes"] == 6)
        second = recorder.status()["file"]
        assert second != first
        assert (recorder.folder / str(first)).read_bytes() == b"first"
        assert (recorder.folder / str(second)).read_bytes() == b"second"
    finally:
        recorder.close()
    resumed = _recorder(tmp_path)
    try:
        assert resumed.status()["running"] is True
        resumed.stop()
        assert json.loads(resumed.state_path.read_text())["running"] is False
    finally:
        resumed.close()


def test_disk_floor_pauses_and_retries_without_harming_feed(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path, floor=1)
    try:
        recorder.start()
        with (
            patch("autodj.recorder.shutil.disk_usage") as usage,
            patch("autodj.recorder._RETRY_SECONDS", 0),
        ):
            usage.return_value.free = 0
            recorder.feed(b"missed", "")
            _wait_for(lambda: recorder.status()["error"] is not None)
            usage.return_value.free = 10_000_000
            recorder.feed(b"saved", "")
            _wait_for(lambda: recorder.status()["bytes"] == 5)
        assert recorder.status()["error"] is None
        assert (recorder.folder / str(recorder.status()["file"])).read_bytes() == b"saved"
    finally:
        recorder.close()


def test_retention_touches_only_dated_mp3_files(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    try:
        recorder.configure("show", 1, 0)
        recorder.folder.mkdir()
        expired = recorder.folder / "Station-2020-01-01_00-00-00.mp3"
        protected = recorder.folder / "notes.mp3"
        expired.write_bytes(b"old")
        protected.write_bytes(b"keep")
        old = time.time() - 3 * 86400
        os.utime(expired, (old, old))
        os.utime(protected, (old, old))
        recorder._prune()
        assert not expired.exists()
        assert protected.exists()
    finally:
        recorder.close()


@pytest.mark.parametrize("split,days,floor", [("daily", 0, 0), ("hour", -1, 0), ("hour", 1, True)])
def test_rejects_bad_settings(split: str, days: int, floor: int | bool, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        RecordingConfig(tmp_path, split, days, floor)


@pytest.mark.parametrize(
    "text",
    [
        '{"version":0,"running":true}',
        "not json",
        "[]",
        '{"version":1,"running":"yes","split":"hour","retention_days":0,"free_space_mb":0}',
        '{"version":1,"running":true,"split":"daily","retention_days":0,"free_space_mb":0}',
    ],
)
def test_damaged_state_is_moved_aside_and_recording_starts_stopped(
    tmp_path: Path, text: str
) -> None:
    """A bad recording.json must never keep the station from starting."""
    path = tmp_path / "index" / "recording.json"
    path.parent.mkdir()
    path.write_text(text)
    recorder = _recorder(tmp_path)
    try:
        assert recorder.status()["running"] is False
        assert recorder.split_mode == "show"
        assert not path.exists()
        assert [bad.read_text() for bad in path.parent.glob("recording.json.*.bad")] == [text]
    finally:
        recorder.close()


def test_disk_error_does_not_expose_archive_path() -> None:
    message = _disk_error(OSError(errno.ENOSPC, "No space left on device", "secret/recordings"))
    assert message == "Recording volume is full"
    assert "secret" not in message
    denied = _disk_error(PermissionError(errno.EACCES, "Access is denied", "secret/recordings"))
    assert denied == "Recording storage error: Access is denied"
    assert _disk_error(_LowSpaceError("Less than 5 MB free")) == "Less than 5 MB free"


def test_name_uses_station_time_zone_and_safe_station_name(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path, name="Радио", zone="Pacific/Auckland")
    try:
        recorder.start()
        recorder.feed(b"x", "")
        _wait_for(lambda: recorder.status()["bytes"] == 1)
        name = str(recorder.status()["file"])
        assert recorder_module._NAME.fullmatch(name)
        assert name.startswith("AutoDJ-")
        stamp = datetime.strptime(name[len("AutoDJ-") : -4], "%Y-%m-%d_%H-%M-%S")
        local = datetime.now(ZoneInfo("Pacific/Auckland")).replace(tzinfo=None)
        assert abs((local - stamp).total_seconds()) < 120
    finally:
        recorder.close()


def test_hour_split_closes_the_file_when_the_clock_hour_changes(tmp_path: Path) -> None:
    zone = ZoneInfo("UTC")
    wall = [datetime(2026, 1, 1, 12, 59, 59, tzinfo=zone)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return wall[0]

    recorder = _recorder(tmp_path, split="hour")
    try:
        recorder.start()
        with patch.object(recorder_module, "datetime", Clock):
            recorder.feed(b"before", "")
            _wait_for(lambda: recorder.status()["bytes"] == 6)
            first = str(recorder.status()["file"])
            wall[0] = datetime(2026, 1, 1, 13, 0, 0, tzinfo=zone)
            # The writer closes the old hour's file before new audio arrives.
            _wait_for(lambda: recorder.status()["file"] is None)
            recorder.feed(b"after", "")
            _wait_for(lambda: recorder.status()["file"] not in (None, first))
            _wait_for(lambda: recorder.status()["bytes"] == 5)
            second = str(recorder.status()["file"])
    finally:
        recorder.close()
    assert first.endswith("2026-01-01_12-59-59.mp3")
    assert second.endswith("2026-01-01_13-00-00.mp3")
    assert (recorder.folder / first).read_bytes() == b"before"
    assert (recorder.folder / second).read_bytes() == b"after"


def test_write_errors_log_once_per_streak_and_never_reach_the_encoder(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = _recorder(tmp_path)
    real_open = recorder._open
    failures = iter([OSError(errno.ENOSPC, "full"), OSError(errno.ENOSPC, "full")])

    def flaky_open():
        failure = next(failures, None)
        if failure is not None:
            raise failure
        return real_open()

    try:
        with (
            caplog.at_level(logging.ERROR, logger="autodj.recorder"),
            patch.object(recorder, "_open", side_effect=flaky_open),
            patch("autodj.recorder._RETRY_SECONDS", 0),
        ):
            recorder.start()
            recorder.feed(b"one", "")
            _wait_for(lambda: recorder.status()["error"] == "Recording volume is full")
            recorder.feed(b"two", "")
            recorder.feed(b"three", "")
            _wait_for(lambda: recorder.status()["bytes"] == 5)
        assert recorder.status()["error"] is None
        paused = [r for r in caplog.records if "Recording paused" in r.getMessage()]
        assert len(paused) == 1
    finally:
        recorder.close()


def test_full_queue_drops_audio_without_blocking_and_reports_it(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    release = threading.Event()
    real_open = recorder._open

    def slow_open():
        release.wait(2)
        return real_open()

    try:
        with patch.object(recorder, "_open", side_effect=slow_open):
            recorder.start()
            began = time.monotonic()
            for _ in range(recorder_module._QUEUE_CHUNKS + 10):
                recorder.feed(b"x" * 10, "")
            assert time.monotonic() - began < 0.5
            release.set()
            _wait_for(
                lambda: (
                    recorder.status()["error"] == "Recording writer fell behind; audio was dropped"
                )
            )
    finally:
        release.set()
        recorder.close()


def test_close_writes_queued_audio_and_keeps_running_choice(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    recorder.start()
    recorder.feed(b"first", "")
    _wait_for(lambda: recorder.status()["bytes"] == 5)
    name = str(recorder.status()["file"])

    real_get = recorder._queue.get

    def idle(block: bool = True, timeout: float | None = None) -> object:
        if not block:
            return real_get(block=False)
        time.sleep(timeout or 0)
        raise recorder_module.queue.Empty

    with patch.object(recorder._queue, "get", side_effect=idle):
        # The writer idles while a chunk waits; close must still write it.
        time.sleep(0.3)
        recorder.feed(b"-queued", "")
        recorder.close()
    assert (recorder.folder / name).read_bytes() == b"first-queued"
    assert json.loads(recorder.state_path.read_text())["running"] is True


def test_controls_take_effect_when_the_state_file_cannot_be_saved(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    try:
        recorder.start()
        with patch.object(recorder, "_save", side_effect=OSError("index volume full")):
            with pytest.raises(OSError):
                recorder.stop()
            assert recorder.status()["running"] is False
            with pytest.raises(OSError):
                recorder.start()
            assert recorder.status()["running"] is True
    finally:
        recorder.close()


def test_show_change_splits_only_a_running_show_recording(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path, split="hour")
    try:
        generation = recorder._generation
        recorder.show_changed()
        assert recorder._generation == generation
        recorder.start()
        generation = recorder._generation
        recorder.show_changed()
        assert recorder._generation == generation
        recorder.configure("show", 0, 0)
        generation = recorder._generation
        recorder.show_changed()
        assert recorder._generation == generation + 1
        recorder.stop()
        generation = recorder._generation
        recorder.show_changed()
        assert recorder._generation == generation
        with pytest.raises(ValueError, match="stopped"):
            recorder.split()
    finally:
        recorder.close()


def test_station_name_is_read_when_each_file_opens(tmp_path: Path) -> None:
    current = {"name": "First"}
    recorder = Recorder(
        RecordingConfig(tmp_path / "archives", "show", 0, 0),
        tmp_path / "index" / "recording.json",
        lambda: current["name"],
        "UTC",
    )
    try:
        recorder.start()
        recorder.feed(b"a", "")
        _wait_for(lambda: str(recorder.status()["file"]).startswith("First-"))
        current["name"] = "Second"
        recorder.split()
        recorder.feed(b"b", "")
        _wait_for(lambda: str(recorder.status()["file"]).startswith("Second-"))
    finally:
        recorder.close()


def test_slow_state_save_does_not_hold_encoder_feed(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def slow_save(*_args: object) -> None:
        entered.set()
        assert release.wait(2)

    try:
        recorder.start()
        with patch.object(recorder, "_save", side_effect=slow_save):
            worker = threading.Thread(target=recorder.configure, args=("hour", 1, 0))
            worker.start()
            assert entered.wait(2)
            began = time.monotonic()
            recorder.feed(b"audio", "")
            assert time.monotonic() - began < 0.5
            release.set()
            worker.join(2)
            assert not worker.is_alive()
    finally:
        release.set()
        recorder.close()
