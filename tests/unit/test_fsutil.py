from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from autodj import fsutil
from autodj.fsutil import atomic_write


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
    path = tmp_path / "state.json"
    path.write_text("old", encoding="utf-8")

    def fail_fsync(_fd: int) -> None:
        raise OSError("flush failed")

    monkeypatch.setattr(os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="flush failed"):
        atomic_write(path, "new")

    assert path.read_text(encoding="utf-8") == "old"
    assert [item.name for item in tmp_path.iterdir()] == ["state.json"]


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
