from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

import pytest

from autodj import fsutil
from autodj.fsutil import atomic_write


@pytest.mark.parametrize(
    "naming,pattern",
    [
        ("prefix", r"state\.json\.bad-[0-9a-f]{8}"),
        ("timestamp", r"state\.json\.\d+\.bad"),
        ("uuid", r"state\.json\.[0-9a-f]{32}\.bad"),
    ],
)
def test_set_aside_preserves_recovery_filename_styles(tmp_path, naming, pattern):
    """ADR-0010-07: recovery preserves the operator-visible filename patterns."""
    path = tmp_path / "state.json"
    path.write_bytes(b"invalid state")
    assert fsutil.set_aside(path, "state file", "invalid JSON", naming=naming)
    [saved] = tmp_path.iterdir()
    assert re.fullmatch(pattern, saved.name)
    assert saved.read_bytes() == b"invalid state"
    assert not path.exists()


def test_set_aside_copy_preserves_the_live_schedule(tmp_path):
    """ADR-0010-07: a partially usable schedule remains available after set-aside."""
    path = tmp_path / "schedule.json"
    path.write_bytes(b"partially valid schedule")
    assert fsutil.set_aside(path, "schedule file", "bad event", copy=True)
    [saved] = tmp_path.glob("schedule.json.bad-*")
    assert saved.read_bytes() == path.read_bytes() == b"partially valid schedule"


@pytest.mark.parametrize("raise_on_error", [False, True])
@pytest.mark.parametrize("copy", [False, True])
def test_set_aside_failure_keeps_original_and_reports_no_success(
    tmp_path, monkeypatch, caplog, raise_on_error, copy
):
    path = tmp_path / "state.json"
    path.write_bytes(b"invalid state")

    def refuse(self, target):
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "write_bytes" if copy else "replace", refuse)
    if raise_on_error:
        with pytest.raises(PermissionError, match="locked"):
            fsutil.set_aside(path, "state file", "invalid", copy=copy, raise_on_error=True)
    else:
        assert not fsutil.set_aside(path, "state file", "invalid", copy=copy, raise_on_error=False)
    assert list(tmp_path.iterdir()) == [path]
    assert path.read_bytes() == b"invalid state"
    assert "Could not set aside" in caplog.text
    assert "set it aside as" not in caplog.text
    assert "kept a copy as" not in caplog.text


def test_atomic_write_replaces_text_and_bytes_without_leaving_temp(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("old", encoding="utf-8")

    atomic_write(path, "néw\n")
    assert path.read_bytes() == "néw\n".encode()

    atomic_write(path, b"\x00\x01")
    assert path.read_bytes() == b"\x00\x01"
    assert [item.name for item in tmp_path.iterdir()] == ["state.json"]


def test_failed_write_keeps_old_file_and_removes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0010-01."""
    path = tmp_path / "state.json"
    path.write_text("old", encoding="utf-8")

    def fail_fsync(_fd: int) -> None:
        raise OSError("flush failed")

    monkeypatch.setattr(os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="flush failed"):
        atomic_write(path, "new")

    assert path.read_text(encoding="utf-8") == "old"
    assert [item.name for item in tmp_path.iterdir()] == ["state.json"]


def test_directory_flush_failure_keeps_committed_write_and_warns(tmp_path, monkeypatch, caplog):
    """ADR-0010-08: directory durability failure cannot undo a completed replacement."""
    path = tmp_path / "state.json"
    path.write_text("old", encoding="utf-8")

    def refuse(directory):
        assert directory == path.parent
        assert path.read_text(encoding="utf-8") == "new"
        raise OSError("share refuses directory flush")

    monkeypatch.setattr(fsutil, "fsync_directory", refuse)
    atomic_write(path, "new")

    assert path.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.iterdir()) == [path]
    assert str(path) in caplog.text
    assert "share refuses directory flush" in caplog.text
    assert caplog.records[-1].levelname == "WARNING"


def test_directory_flush_does_not_suppress_non_os_errors(tmp_path, monkeypatch):
    def fail(directory):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(fsutil, "fsync_directory", fail)
    with pytest.raises(RuntimeError, match="unexpected failure"):
        atomic_write(tmp_path / "state.json", "new")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions and directory fsync")
def test_atomic_write_applies_mode_and_flushes_directory_after_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secret"
    events: list[str] = []
    real_replace = os.replace
    real_fsync_directory = fsutil.fsync_directory

    def record_replace(source: Path, destination: Path) -> None:
        events.append("replace")
        real_replace(source, destination)

    def record_fsync_directory(directory: Path) -> None:
        events.append("directory")
        real_fsync_directory(directory)

    monkeypatch.setattr(os, "replace", record_replace)
    monkeypatch.setattr(fsutil, "fsync_directory", record_fsync_directory)

    atomic_write(path, "token", mode=0o600)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert events == ["replace", "directory"]


def _flaky_replace(monkeypatch: pytest.MonkeyPatch, failures: int) -> list[int]:
    """Make os.replace raise PermissionError *failures* times, as Windows does."""
    calls: list[int] = []
    real_replace = os.replace

    def replace(source: Path, destination: Path) -> None:
        calls.append(1)
        if len(calls) <= failures:
            raise PermissionError(13, "Access is denied")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(fsutil.time, "sleep", lambda _seconds: None)
    return calls


def test_atomic_write_retries_while_windows_holds_the_file_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    path.write_text("old", encoding="utf-8")
    monkeypatch.setattr(fsutil, "_IS_WINDOWS", True)
    calls = _flaky_replace(monkeypatch, failures=3)

    atomic_write(path, "new")

    assert len(calls) == 4
    assert path.read_text(encoding="utf-8") == "new"
    assert [item.name for item in tmp_path.iterdir()] == ["state.json"]


def test_atomic_write_retry_is_bounded_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    path.write_text("old", encoding="utf-8")
    monkeypatch.setattr(fsutil, "_IS_WINDOWS", True)
    calls = _flaky_replace(monkeypatch, failures=1000)

    with pytest.raises(PermissionError):
        atomic_write(path, "new")

    assert len(calls) == len(fsutil._SHARING_RETRY_DELAYS) + 1
    assert path.read_text(encoding="utf-8") == "old"
    assert [item.name for item in tmp_path.iterdir()] == ["state.json"]


def test_atomic_write_does_not_retry_permission_errors_off_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    monkeypatch.setattr(fsutil, "_IS_WINDOWS", False)
    calls = _flaky_replace(monkeypatch, failures=1)

    with pytest.raises(PermissionError):
        atomic_write(path, "new")
    assert len(calls) == 1


def test_read_bytes_shared_retries_while_windows_replaces_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"saved")
    monkeypatch.setattr(fsutil, "_IS_WINDOWS", True)
    monkeypatch.setattr(fsutil.time, "sleep", lambda _seconds: None)
    real_read = Path.read_bytes
    calls: list[int] = []

    def read_bytes(self: Path) -> bytes:
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError(13, "Permission denied")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert fsutil.read_bytes_shared(path) == b"saved"
    assert len(calls) == 2


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing rules")
def test_atomic_write_waits_for_a_reader_that_has_the_file_open(tmp_path: Path) -> None:
    """Python's open() has no FILE_SHARE_DELETE, so a reader blocks the rename."""
    import threading

    path = tmp_path / "state.json"
    path.write_text("old", encoding="utf-8")
    handle = path.open("rb")
    timer = threading.Timer(0.05, handle.close)
    timer.start()
    try:
        atomic_write(path, "new")
    finally:
        timer.join()
        handle.close()
    assert path.read_text(encoding="utf-8") == "new"
