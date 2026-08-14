from __future__ import annotations

import asyncio
import contextlib
import errno
import io
import multiprocessing
import os
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO
from unittest.mock import MagicMock

import pytest

from autodj.liner_files import (
    InvalidLinerName,
    LinerConflictError,
    LinerRangeNotSatisfiable,
    LinerStorageUnsupportedError,
    LinerTooLargeError,
    LinerUploadBodyLimitMiddleware,
    MalformedLinerRange,
    OpenedLiner,
    delete_liner_file,
    iter_opened_liner,
    open_liner_file,
    parse_liner_range,
    resolve_liner_path,
    store_liner_upload,
)


class BytesReader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.sizes.append(size)
        payload, self._payload = self._payload[:size], self._payload[size:]
        return payload


class UploadAborted(BaseException):
    pass


class AbortingReader:
    def __init__(self) -> None:
        self._reads = 0

    async def read(self, size: int = -1) -> bytes:
        self._reads += 1
        if self._reads == 1:
            return b"partial"
        raise UploadAborted


class CloseFailingFile:
    def __init__(self, wrapped: BinaryIO) -> None:
        self._wrapped = wrapped

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    def close(self) -> None:
        self._wrapped.close()
        raise UploadAborted


@pytest.fixture(autouse=True)
def _portable_windows_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide patchable Windows bindings for platform-neutral unit simulations."""
    if os.name == "nt" and os.environ.get("AUTODJ_TEST_PORTABLE_WINDOWS_API") != "1":
        return
    from autodj import liner_files

    monkeypatch.setattr(liner_files, "_kernel32", MagicMock())
    monkeypatch.setattr(liner_files, "_ntdll", MagicMock())


def test_module_imports_when_windows_bindings_are_unavailable() -> None:
    probe = """\
import importlib.util
import os
import sys
import autodj.liner_files as native_module

name = 'autodj._liner_files_posix_probe'
spec = importlib.util.spec_from_file_location(name, native_module.__file__)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
original_name = os.name
try:
    os.name = 'posix'
    spec.loader.exec_module(module)
finally:
    os.name = original_name
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_portable_windows_fallbacks_report_unavailable_api() -> None:
    from autodj import liner_files

    with pytest.raises(LinerStorageUnsupportedError, match="Windows handle APIs"):
        liner_files._unsupported_osfhandle(12)

    error = liner_files._fallback_win_error(errno.EACCES)
    assert error.errno == errno.EACCES
    assert os.strerror(errno.EACCES) in str(error)


def test_windows_library_signatures_are_configured_portably() -> None:
    import ctypes
    from ctypes import wintypes

    from autodj import liner_files

    kernel32 = SimpleNamespace(
        CreateFileW=MagicMock(),
        CloseHandle=MagicMock(),
        FlushFileBuffers=MagicMock(),
        GetFileInformationByHandleEx=MagicMock(),
        GetFileInformationByHandle=MagicMock(),
        SetFileInformationByHandle=MagicMock(),
    )
    ntdll = SimpleNamespace(
        NtCreateFile=MagicMock(),
        NtSetInformationFile=MagicMock(),
        RtlNtStatusToDosError=MagicMock(),
    )

    liner_files._configure_windows_libraries(kernel32, ntdll)

    assert kernel32.CreateFileW.argtypes == [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    assert kernel32.CreateFileW.restype is wintypes.HANDLE
    assert kernel32.CloseHandle.argtypes == [wintypes.HANDLE]
    assert kernel32.CloseHandle.restype is wintypes.BOOL
    assert kernel32.FlushFileBuffers.argtypes == [wintypes.HANDLE]
    assert kernel32.FlushFileBuffers.restype is wintypes.BOOL
    assert kernel32.GetFileInformationByHandleEx.restype is wintypes.BOOL
    assert kernel32.GetFileInformationByHandle.restype is wintypes.BOOL
    assert kernel32.SetFileInformationByHandle.restype is wintypes.BOOL
    assert ntdll.NtCreateFile.restype is ctypes.c_long
    assert ntdll.NtSetInformationFile.restype is ctypes.c_long
    assert ntdll.RtlNtStatusToDosError.argtypes == [ctypes.c_long]
    assert ntdll.RtlNtStatusToDosError.restype is wintypes.ULONG


def _process_upload(
    root: str,
    payload: bytes,
    ready: multiprocessing.Queue,
    start: multiprocessing.synchronize.Event,
    publication_ready: multiprocessing.Queue,
    publish: multiprocessing.synchronize.Event,
    results: multiprocessing.Queue,
) -> None:
    from autodj import liner_files

    if os.name == "nt":
        original_publish_primitive = liner_files._windows_rename_by_handle

        def synchronized_publish_primitive(*args: Any, **kwargs: Any) -> None:
            publication_ready.put(True)
            if not publish.wait(timeout=10):
                raise TimeoutError("publication barrier timed out")
            original_publish_primitive(*args, **kwargs)

        liner_files._windows_rename_by_handle = synchronized_publish_primitive
    else:
        original_publish_primitive = liner_files._rename_noreplace_posix

        def synchronized_publish_primitive(*args: Any, **kwargs: Any) -> None:
            publication_ready.put(True)
            if not publish.wait(timeout=10):
                raise TimeoutError("publication barrier timed out")
            original_publish_primitive(*args, **kwargs)

        liner_files._rename_noreplace_posix = synchronized_publish_primitive

    ready.put(True)
    if not start.wait(timeout=10):
        results.put("start-timeout")
        return
    try:
        asyncio.run(
            store_liner_upload(
                Path(root),
                "race.mp3",
                BytesReader(payload),
                max_bytes=100,
                replace=False,
            )
        )
    except LinerConflictError:
        results.put("conflict")
    else:
        results.put("stored")


@pytest.mark.parametrize(
    "name",
    [
        "",
        "bad\x00.mp3",
        "bad\n.mp3",
        ".",
        "..",
        "../clip.mp3",
        r"..\liners-backup\clip.mp3",
        "sub/clip.mp3",
        r"sub\clip.mp3",
        "CON.mp3",
        "CONIN$",
        "conout$.wav",
        "con.MP3",
        "nul.wav",
        "LPT9.flac",
        "COM1.anything.mp3",
        "COM¹.mp3",
        "com².WAV",
        "COM³",
        "LPT¹.mp3",
        "lpt².wav",
        "LPT³.anything",
        "clip.mp3:stream",
        "clip?.mp3",
        'clip".mp3',
        "clip.mp3.",
        "clip.mp3 ",
    ],
)
def test_resolve_liner_path_rejects_escape_and_device_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(InvalidLinerName):
        resolve_liner_path(tmp_path / "liners", name)


def test_resolve_liner_path_accepts_plain_unicode_filename(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    assert resolve_liner_path(root, "statión-音.mp3") == root.resolve() / "statión-音.mp3"


def test_resolve_liner_path_requires_regular_file(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    root.mkdir()
    (root / "folder.mp3").mkdir()
    with pytest.raises(FileNotFoundError):
        resolve_liner_path(root, "folder.mp3", require_file=True)


def test_resolve_liner_path_rejects_symlink_target(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    root.mkdir()
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    try:
        (root / "clip.mp3").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(InvalidLinerName):
        resolve_liner_path(root, "clip.mp3", require_file=True)


def test_resolve_liner_path_rejects_symlink_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    root = tmp_path / "liners"
    try:
        root.symlink_to(actual, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(InvalidLinerName):
        resolve_liner_path(root, "clip.mp3")


def test_resolve_liner_path_rejects_target_escape_after_reparse_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    root.mkdir()
    candidate = root / "clip.mp3"
    outside = tmp_path / "outside.mp3"
    native_resolve = Path.resolve

    def raced_resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
        if path == candidate:
            return outside
        return native_resolve(path, *args, **kwargs)

    monkeypatch.setattr(liner_files, "_is_reparse_point", MagicMock(return_value=False))
    monkeypatch.setattr(Path, "resolve", raced_resolve)

    with pytest.raises(InvalidLinerName, match="escapes configured root"):
        resolve_liner_path(root, "clip.mp3")


@pytest.mark.asyncio
async def test_upload_reads_only_positive_bounded_chunks(tmp_path: Path) -> None:
    reader = BytesReader(b"x" * (1024 * 1024 + 2))
    _target, size = await store_liner_upload(
        tmp_path / "liners", "clip.mp3", reader, max_bytes=2 * 1024 * 1024, replace=False
    )
    assert size == 1024 * 1024 + 2
    assert reader.sizes
    assert all(0 < size <= 1024 * 1024 for size in reader.sizes)


@pytest.mark.asyncio
@pytest.mark.parametrize("max_bytes", [0, -1])
async def test_upload_rejects_non_positive_limit(tmp_path: Path, max_bytes: int) -> None:
    root = tmp_path / "liners"
    with pytest.raises(ValueError, match="positive"):
        await store_liner_upload(
            root, "clip.mp3", BytesReader(b""), max_bytes=max_bytes, replace=False
        )
    assert not root.exists()


@pytest.mark.asyncio
async def test_zero_byte_upload_is_stored(tmp_path: Path) -> None:
    target, size = await store_liner_upload(
        tmp_path / "liners", "silence.wav", BytesReader(b""), max_bytes=1, replace=False
    )
    assert size == 0
    assert target.read_bytes() == b""


@pytest.mark.asyncio
async def test_upload_creates_multiple_missing_root_components(tmp_path: Path) -> None:
    root = tmp_path / "missing-parent" / "missing-root"
    target, size = await store_liner_upload(
        root, "silence.wav", BytesReader(b""), max_bytes=1, replace=False
    )
    assert size == 0
    assert target == root / "silence.wav"
    assert target.read_bytes() == b""


@pytest.mark.skipif(os.name != "nt", reason="Windows SMB rename regression")
@pytest.mark.asyncio
async def test_windows_smb_fallback_keeps_source_handle_as_root_swap_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    moved = tmp_path / "swapped-liners"
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    original_rename = liner_files._windows_rename_by_handle
    calls: list[tuple[int, str]] = []
    swap_blocked = False

    def reject_relative_then_publish_full(
        source_handle: int, root_handle: int, name: str, *, replace: bool
    ) -> None:
        nonlocal swap_blocked
        calls.append((root_handle, name))
        if len(calls) == 1:
            raise OSError(32, "sharing violation", name, 32)
        assert root_handle == 0
        assert name.startswith("\\??\\")
        try:
            root.rename(moved)
        except PermissionError:
            swap_blocked = True
        else:
            root.mkdir()
            (root / "clip.mp3").write_bytes(b"outside-root")
        original_rename(source_handle, root_handle, name, replace=replace)

    monkeypatch.setattr(liner_files, "_windows_rename_by_handle", reject_relative_then_publish_full)
    target, size = await store_liner_upload(
        root, "clip.mp3", BytesReader(b"new"), max_bytes=50, replace=False
    )
    assert size == 3
    assert target.read_bytes() == b"new"
    assert len(calls) == 2
    assert swap_blocked
    assert not moved.exists()
    assert outside.read_bytes() == b"outside"


@pytest.mark.skipif(os.name != "nt", reason="Windows SMB rollback regression")
@pytest.mark.asyncio
async def test_windows_smb_postcommit_validation_failure_returns_committed_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    root.mkdir()
    target = root / "clip.mp3"
    target.write_bytes(b"old")
    original_rename = liner_files._windows_rename_by_handle
    original_open_root = liner_files._open_windows_root
    rename_calls = 0
    root_open_calls = 0

    def reject_relative_once(
        source_handle: int, root_handle: int, name: str, *, replace: bool
    ) -> None:
        nonlocal rename_calls
        rename_calls += 1
        if rename_calls == 1:
            raise OSError(32, "sharing violation", name, 32)
        original_rename(source_handle, root_handle, name, replace=replace)

    def fail_postcommit_reopen(path: Path, *, create: bool, write: bool):
        nonlocal root_open_calls
        root_open_calls += 1
        if root_open_calls == 3:
            raise UploadAborted
        return original_open_root(path, create=create, write=write)

    monkeypatch.setattr(liner_files, "_windows_rename_by_handle", reject_relative_once)
    monkeypatch.setattr(liner_files, "_open_windows_root", fail_postcommit_reopen)
    returned, size = await store_liner_upload(
        root, target.name, BytesReader(b"new"), max_bytes=50, replace=True
    )
    assert size == 3
    assert returned == target
    assert target.read_bytes() == b"new"


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership policy")
@pytest.mark.asyncio
async def test_posix_upload_rejects_shared_writable_root_before_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "liners"
    root.mkdir()
    root.chmod(0o777)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")

    with pytest.raises(InvalidLinerName, match="private owner-controlled"):
        await store_liner_upload(root, "clip.mp3", BytesReader(b"new"), max_bytes=50, replace=False)
    assert not (root / "clip.mp3").exists()
    assert list(root.glob(".liner-upload-*")) == []
    assert outside.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name != "posix", reason="POSIX stage-binding regression")
@pytest.mark.asyncio
async def test_posix_upload_rejects_stage_directory_swapped_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    root.mkdir(mode=0o700)
    replacement = tmp_path / "replacement-stage"
    replacement.mkdir(mode=0o700)
    sentinel = replacement / "outside.txt"
    sentinel.write_text("keep", encoding="utf-8")
    displaced = tmp_path / "displaced-created-stage"
    original_open = liner_files.os.open
    swapped = False

    def swap_before_stage_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        path_text = os.fsdecode(path)
        dir_fd = kwargs.get("dir_fd")
        if not swapped and path_text.startswith(".liner-upload-") and dir_fd is not None:
            swapped = True
            stage_path = root / path_text
            stage_path.rename(displaced)
            replacement.rename(stage_path)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(liner_files.os, "open", swap_before_stage_open)
    with pytest.raises(InvalidLinerName, match="changed during secure open"):
        await store_liner_upload(root, "clip.mp3", BytesReader(b"new"), max_bytes=50, replace=False)
    assert swapped
    assert not (root / "clip.mp3").exists()
    assert (next(root.glob(".liner-upload-*")) / "outside.txt").read_text(
        encoding="utf-8"
    ) == "keep"
    assert not (next(root.glob(".liner-upload-*")) / "upload.tmp").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor ownership")
def test_posix_root_parent_close_base_exception_still_closes_owned_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    original_open = liner_files.os.open
    original_close = liner_files.os.close
    opened: list[int] = []
    injected = False

    def capture_open(*args: Any, **kwargs: Any) -> int:
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def close_then_raise_once(fd: int) -> None:
        nonlocal injected
        original_close(fd)
        if not injected:
            injected = True
            raise UploadAborted

    monkeypatch.setattr(liner_files.os, "open", capture_open)
    monkeypatch.setattr(liner_files.os, "close", close_then_raise_once)
    with pytest.raises(UploadAborted):
        liner_files._open_posix_root(tmp_path, create=False, mutate=False)
    assert len(opened) >= 2
    with pytest.raises(OSError) as error:
        os.fstat(opened[1])
    assert error.value.errno == errno.EBADF


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor ownership")
def test_posix_rollback_probe_never_recloses_reused_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    quarantine = tmp_path / "quarantine"
    quarantine.mkdir(mode=0o700)
    quarantine_fd = os.open(quarantine, os.O_RDONLY | os.O_DIRECTORY)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    original_close = liner_files.os.close
    replacement_fd: int | None = None
    injected = False

    def close_reuse_then_raise(fd: int) -> None:
        nonlocal injected, replacement_fd
        original_close(fd)
        if not injected:
            injected = True
            replacement_fd = os.open(outside, os.O_RDONLY)
            assert replacement_fd == fd
            raise UploadAborted

    monkeypatch.setattr(liner_files.os, "close", close_reuse_then_raise)
    try:
        with pytest.raises(UploadAborted):
            liner_files._verify_posix_delete_rollback(quarantine_fd)
        assert replacement_fd is not None
        assert os.fstat(replacement_fd).st_size == len(b"outside")
    finally:
        monkeypatch.setattr(liner_files.os, "close", original_close)
        if replacement_fd is not None:
            with contextlib.suppress(OSError):
                original_close(replacement_fd)
        original_close(quarantine_fd)


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-rights regression")
def test_windows_root_reopens_anchor_before_creating_first_missing_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    accesses: list[int] = []
    mutable_handles: set[int] = set()
    file_add_subdirectory = 0x00000004

    def open_anchor(*args: Any) -> int:
        access = int(args[1])
        handle = 10 + len(accesses)
        accesses.append(access)
        if access & file_add_subdirectory:
            mutable_handles.add(handle)
        return handle

    def open_child(
        parent_handle: int,
        name: str,
        *,
        directory: bool,
        create: bool,
        access: int,
    ) -> int:
        assert directory
        assert name == "missing"
        if not create:
            raise FileNotFoundError(name)
        if parent_handle not in mutable_handles:
            raise PermissionError("parent lacks FILE_ADD_SUBDIRECTORY")
        return 20

    monkeypatch.setattr(liner_files._kernel32, "CreateFileW", open_anchor)
    monkeypatch.setattr(
        liner_files,
        "_windows_handle_attributes",
        lambda _handle: liner_files._FILE_ATTRIBUTE_DIRECTORY,
    )
    monkeypatch.setattr(liner_files, "_nt_create_relative", open_child)
    monkeypatch.setattr(liner_files, "_close_windows_handle", lambda _handle: None)
    monkeypatch.setattr(liner_files, "_windows_file_identity", lambda _handle: (1, b"same"))

    pinned = liner_files._open_windows_root(Path("Q:/missing"), create=True, write=True)
    assert pinned.handle == 20
    assert len(accesses) == 2
    assert not accesses[0] & file_add_subdirectory
    assert accesses[1] & file_add_subdirectory


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-identity regression")
def test_windows_root_rejects_swapped_parent_during_rights_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    handles = iter((10, 11))
    create_attempted = False

    def open_child(
        parent_handle: int,
        name: str,
        *,
        directory: bool,
        create: bool,
        access: int,
    ) -> int:
        nonlocal create_attempted
        if not create:
            raise FileNotFoundError(name)
        create_attempted = True
        return 20

    monkeypatch.setattr(liner_files._kernel32, "CreateFileW", lambda *args: next(handles))
    monkeypatch.setattr(
        liner_files,
        "_windows_handle_attributes",
        lambda _handle: liner_files._FILE_ATTRIBUTE_DIRECTORY,
    )
    monkeypatch.setattr(liner_files, "_nt_create_relative", open_child)
    monkeypatch.setattr(liner_files, "_close_windows_handle", lambda _handle: None)
    monkeypatch.setattr(
        liner_files,
        "_windows_file_identity",
        lambda handle: (1, bytes([handle])),
        raising=False,
    )

    with pytest.raises(InvalidLinerName, match="changed during secure open"):
        liner_files._open_windows_root(Path("Q:/missing"), create=True, write=True)
    assert not create_attempted


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC identity regression")
def test_windows_file_identity_falls_back_when_smb_rejects_file_id_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    def reject_file_id(*args: Any) -> bool:
        return False

    def provide_classic_identity(handle: int, pointer: Any) -> bool:
        assert handle == 123
        info = pointer._obj
        info.VolumeSerialNumber = 42
        info.FileIndexHigh = 0x01020304
        info.FileIndexLow = 0x05060708
        return True

    monkeypatch.setattr(liner_files._kernel32, "GetFileInformationByHandleEx", reject_file_id)
    monkeypatch.setattr(
        liner_files._kernel32,
        "GetFileInformationByHandle",
        provide_classic_identity,
    )
    monkeypatch.setattr(liner_files, "_get_last_error", lambda: 87)

    volume, identity = liner_files._windows_file_identity(123)
    assert volume == 42
    assert identity == b"file-index-64:\x08\x07\x06\x05\x04\x03\x02\x01"


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC identity regression")
def test_windows_classic_identity_fallback_detects_swapped_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    monkeypatch.setattr(
        liner_files._kernel32,
        "GetFileInformationByHandleEx",
        lambda *args: False,
    )

    def provide_distinct_identity(handle: int, pointer: Any) -> bool:
        info = pointer._obj
        info.VolumeSerialNumber = 42
        info.FileIndexHigh = 0
        info.FileIndexLow = handle
        return True

    monkeypatch.setattr(
        liner_files._kernel32,
        "GetFileInformationByHandle",
        provide_distinct_identity,
    )
    monkeypatch.setattr(liner_files, "_get_last_error", lambda: 87)
    assert liner_files._windows_file_identity(10) != liner_files._windows_file_identity(11)


@pytest.mark.asyncio
async def test_oversized_upload_removes_only_owned_temporary_file(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    root.mkdir()
    unrelated = root / ".liner-upload-unrelated.tmp"
    unrelated.write_bytes(b"keep")
    with pytest.raises(LinerTooLargeError):
        await store_liner_upload(
            root, "clip.mp3", BytesReader(b"x" * 51), max_bytes=50, replace=False
        )
    assert unrelated.read_bytes() == b"keep"
    assert list(root.glob(".liner-upload-*")) == [unrelated]
    assert not (root / "clip.mp3").exists()


@pytest.mark.asyncio
async def test_base_exception_removes_owned_temporary_file(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    with pytest.raises(UploadAborted):
        await store_liner_upload(root, "clip.mp3", AbortingReader(), max_bytes=50, replace=False)
    assert list(root.glob(".liner-upload-*")) == []
    assert not (root / "clip.mp3").exists()


@pytest.mark.asyncio
async def test_conflict_requires_explicit_atomic_replace(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    root.mkdir()
    target = root / "clip.mp3"
    target.write_bytes(b"old")
    with pytest.raises(LinerConflictError):
        await store_liner_upload(
            root, target.name, BytesReader(b"new"), max_bytes=50, replace=False
        )
    assert target.read_bytes() == b"old"
    await store_liner_upload(root, target.name, BytesReader(b"new"), max_bytes=50, replace=True)
    assert target.read_bytes() == b"new"


@pytest.mark.asyncio
async def test_failed_replace_preserves_existing_target_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    root.mkdir()
    target = root / "clip.mp3"
    target.write_bytes(b"old")

    def fail_replace(*args: Any, **kwargs: Any) -> None:
        raise OSError("promotion failed")

    monkeypatch.setattr(liner_files, "_publish_staged_file", fail_replace)
    with pytest.raises(OSError, match="promotion failed"):
        await store_liner_upload(root, target.name, BytesReader(b"new"), max_bytes=50, replace=True)
    assert target.read_bytes() == b"old"
    assert list(root.glob(".liner-upload-*")) == []


@pytest.mark.asyncio
async def test_fstat_failure_cleans_only_private_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    root.mkdir()
    sentinel = root / ".liner-upload-unrelated.tmp"
    sentinel.write_bytes(b"not-owned")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    original_make = liner_files._make_staged_upload
    original_fstat = liner_files.os.fstat
    upload_fd: int | None = None

    def capture_upload_fd(*args: Any, **kwargs: Any):
        nonlocal upload_fd
        staged = original_make(*args, **kwargs)
        upload_fd = staged.file.fileno()
        return staged

    def fail_fstat(fd: int) -> os.stat_result:
        if fd == upload_fd:
            raise OSError("fstat failed")
        return original_fstat(fd)

    monkeypatch.setattr(liner_files, "_make_staged_upload", capture_upload_fd)
    monkeypatch.setattr(liner_files.os, "fstat", fail_fstat)
    with pytest.raises(OSError, match="fstat failed"):
        await store_liner_upload(root, "clip.mp3", BytesReader(b"new"), max_bytes=50, replace=True)
    assert sentinel.read_bytes() == b"not-owned"
    assert list(root.glob(".liner-upload-*")) == [sentinel]
    assert not (root / "clip.mp3").exists()
    assert outside.read_bytes() == b"outside"


@pytest.mark.asyncio
async def test_upload_close_base_exception_still_removes_private_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    original_make = liner_files._make_staged_upload

    def wrap_file(*args: Any, **kwargs: Any):
        staged = original_make(*args, **kwargs)
        staged.file = CloseFailingFile(staged.file)
        return staged

    monkeypatch.setattr(liner_files, "_make_staged_upload", wrap_file)
    target, size = await store_liner_upload(
        root, "clip.mp3", BytesReader(b"new"), max_bytes=50, replace=False
    )
    assert size == 3
    assert target.read_bytes() == b"new"
    assert list(root.glob(".liner-upload-*")) == []


@pytest.mark.asyncio
async def test_fdopen_failure_removes_private_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"

    def fail_fdopen(*args: Any, **kwargs: Any) -> BinaryIO:
        raise OSError("fdopen failed")

    monkeypatch.setattr(liner_files.os, "fdopen", fail_fdopen)
    with pytest.raises(OSError, match="fdopen failed"):
        await store_liner_upload(root, "clip.mp3", BytesReader(b"new"), max_bytes=50, replace=True)
    assert list(root.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows descriptor cleanup regression")
@pytest.mark.asyncio
async def test_windows_fdopen_and_fd_close_failures_still_remove_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    original_close = liner_files.os.close

    def fail_fdopen(*args: Any, **kwargs: Any) -> BinaryIO:
        raise OSError("fdopen failed")

    def close_then_raise(fd: int) -> None:
        original_close(fd)
        raise UploadAborted

    monkeypatch.setattr(liner_files.os, "fdopen", fail_fdopen)
    monkeypatch.setattr(liner_files.os, "close", close_then_raise)
    with pytest.raises(OSError, match="fdopen failed"):
        await store_liner_upload(root, "clip.mp3", BytesReader(b"new"), max_bytes=50, replace=True)
    assert list(root.glob(".liner-upload-*")) == []


@pytest.mark.asyncio
async def test_upload_uses_pinned_root_when_configured_path_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    moved = tmp_path / "original-liners"
    root.mkdir()
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    original_publish = liner_files._publish_staged_file

    def swap_root_then_publish(*args: Any, **kwargs: Any) -> None:
        try:
            root.rename(moved)
        except PermissionError:
            original_publish(*args, **kwargs)
            return
        root.mkdir()
        (root / "clip.mp3").write_bytes(b"outside-root")
        original_publish(*args, **kwargs)

    monkeypatch.setattr(liner_files, "_publish_staged_file", swap_root_then_publish)
    await store_liner_upload(root, "clip.mp3", BytesReader(b"new"), max_bytes=50, replace=True)
    if moved.exists():
        assert (moved / "clip.mp3").read_bytes() == b"new"
        assert (root / "clip.mp3").read_bytes() == b"outside-root"
    else:
        assert (root / "clip.mp3").read_bytes() == b"new"
    assert outside.read_bytes() == b"outside"


def test_delete_uses_pinned_root_when_configured_path_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    moved = tmp_path / "original-liners"
    root.mkdir()
    (root / "clip.mp3").write_bytes(b"inside")
    original_delete = liner_files._delete_relative_file

    def swap_root_then_delete(*args: Any, **kwargs: Any) -> None:
        root.rename(moved)
        root.mkdir()
        (root / "clip.mp3").write_bytes(b"outside-root")
        original_delete(*args, **kwargs)

    monkeypatch.setattr(liner_files, "_delete_relative_file", swap_root_then_delete)
    delete_liner_file(root, "clip.mp3")
    assert not (moved / "clip.mp3").exists()
    assert (root / "clip.mp3").read_bytes() == b"outside-root"


def test_delete_opens_root_without_broad_write_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    calls: list[tuple[bool, bool, bool]] = []
    pinned = liner_files._PinnedRoot(Path("unused"), 123, os.name == "nt")

    def open_root(path: Path, *, create: bool, write: bool, mutate: bool):
        calls.append((create, write, mutate))
        return pinned

    monkeypatch.setattr(liner_files, "_open_pinned_root", open_root)
    monkeypatch.setattr(liner_files, "_delete_relative_file", lambda *_args: None)
    monkeypatch.setattr(liner_files, "_flush_directory", lambda *_args: None)
    monkeypatch.setattr(liner_files, "_close_root", lambda *_args: None)

    delete_liner_file(Path("unused"), "clip.mp3")
    assert calls == [(False, False, True)]


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership policy")
def test_posix_delete_rejects_shared_writable_root_before_mutation(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    root.mkdir()
    target = root / "clip.mp3"
    target.write_bytes(b"inside")
    root.chmod(0o777)

    with pytest.raises(InvalidLinerName, match="private owner-controlled"):
        delete_liner_file(root, target.name)
    assert target.read_bytes() == b"inside"
    assert list(root.glob(".liner-delete-*")) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-handle regression")
def test_windows_delete_close_failure_after_commit_returns_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    root.mkdir()
    target = root / "clip.mp3"
    target.write_bytes(b"inside")
    original_delete = liner_files._windows_delete_by_handle
    original_close = liner_files._close_windows_handle
    deleted = False
    injected = False

    def mark_deleted(handle: int) -> None:
        nonlocal deleted
        original_delete(handle)
        deleted = True

    def fail_first_close_after_delete(handle: int) -> None:
        nonlocal injected
        original_close(handle)
        if deleted and not injected:
            injected = True
            raise OSError("child close failed after deletion")

    monkeypatch.setattr(liner_files, "_windows_delete_by_handle", mark_deleted)
    monkeypatch.setattr(liner_files, "_close_windows_handle", fail_first_close_after_delete)

    delete_liner_file(root, target.name)
    assert injected
    assert not target.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX delete cleanup regression")
def test_posix_delete_quarantine_stat_failure_after_commit_returns_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    root.mkdir(mode=0o700)
    target = root / "clip.mp3"
    target.write_bytes(b"inside")
    original_stat = liner_files.os.stat
    quarantine_name_stats = 0

    def fail_cleanup_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal quarantine_name_stats
        if os.fsdecode(path).startswith(".liner-delete-"):
            quarantine_name_stats += 1
            if quarantine_name_stats == 3:
                raise UploadAborted
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(liner_files.os, "stat", fail_cleanup_stat)
    delete_liner_file(root, target.name)
    assert quarantine_name_stats == 3
    assert not target.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX delete rollback regression")
def test_posix_delete_requires_atomic_rollback_before_moving_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    root.mkdir(mode=0o700)
    target = root / "clip.mp3"
    target.write_bytes(b"inside")

    def unsupported_rename(*args: Any, **kwargs: Any) -> None:
        raise liner_files.LinerStorageUnsupportedError("renameat2 unavailable")

    def unsupported_link(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno.EOPNOTSUPP, "hard links unavailable")

    monkeypatch.setattr(liner_files, "_rename_noreplace_posix", unsupported_rename)
    monkeypatch.setattr(liner_files.os, "link", unsupported_link)
    with pytest.raises(liner_files.LinerStorageUnsupportedError, match="rollback unavailable"):
        delete_liner_file(root, target.name)
    assert target.read_bytes() == b"inside"
    assert list(root.glob(".liner-delete-*")) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX delete rollback regression")
def test_posix_delete_precommit_validation_failure_restores_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    root.mkdir(mode=0o700)
    target = root / "clip.mp3"
    target.write_bytes(b"inside")
    original_stat = liner_files.os.stat

    def fail_victim_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if os.fsdecode(path) == "victim" and kwargs.get("dir_fd") is not None:
            raise OSError("victim validation failed")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(liner_files.os, "stat", fail_victim_stat)
    with pytest.raises(OSError, match="victim validation failed"):
        delete_liner_file(root, target.name)
    assert target.read_bytes() == b"inside"
    assert list(root.glob(".liner-delete-*")) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX delete rollback regression")
def test_posix_delete_uses_atomic_link_rollback_when_renameat2_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    root.mkdir(mode=0o700)
    target = root / "clip.mp3"
    target.write_bytes(b"inside")
    original_stat = liner_files.os.stat

    def unsupported_rename(*args: Any, **kwargs: Any) -> None:
        raise liner_files.LinerStorageUnsupportedError("renameat2 unavailable")

    def fail_victim_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if os.fsdecode(path) == "victim" and kwargs.get("dir_fd") is not None:
            raise OSError("victim validation failed")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(liner_files, "_rename_noreplace_posix", unsupported_rename)
    monkeypatch.setattr(liner_files.os, "stat", fail_victim_stat)
    with pytest.raises(OSError, match="victim validation failed"):
        delete_liner_file(root, target.name)
    assert target.read_bytes() == b"inside"
    assert list(root.glob(".liner-delete-*")) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX delete rollback race")
def test_posix_delete_rollback_never_overwrites_concurrent_same_uid_creator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    root.mkdir(mode=0o700)
    target = root / "clip.mp3"
    target.write_bytes(b"original")
    original_stat = liner_files.os.stat
    publication = threading.Barrier(2, timeout=10)
    concurrent_created = threading.Event()

    def unsupported_rename(*args: Any, **kwargs: Any) -> None:
        raise liner_files.LinerStorageUnsupportedError("renameat2 unavailable")

    def create_concurrent_target(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if os.fsdecode(path) == "victim" and kwargs.get("dir_fd") is not None:
            publication.wait(timeout=10)
            if not concurrent_created.wait(timeout=10):
                raise TimeoutError("concurrent creator timed out")
            raise OSError("victim validation failed")
        return original_stat(path, *args, **kwargs)

    def create_target() -> None:
        publication.wait(timeout=10)
        target.write_bytes(b"concurrent")
        concurrent_created.set()

    monkeypatch.setattr(liner_files, "_rename_noreplace_posix", unsupported_rename)
    monkeypatch.setattr(liner_files.os, "stat", create_concurrent_target)
    creator = threading.Thread(target=create_target)
    creator.start()
    with pytest.raises(OSError, match="victim validation failed"):
        delete_liner_file(root, target.name)
    creator.join(timeout=10)
    assert not creator.is_alive()
    assert target.read_bytes() == b"concurrent"
    quarantines = list(root.glob(".liner-delete-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "victim").read_bytes() == b"original"


@pytest.mark.skipif(os.name != "posix", reason="POSIX quarantine behavior")
def test_posix_delete_target_swap_is_rejected_and_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    root.mkdir()
    target = root / "clip.mp3"
    moved = root / "opened-original.mp3"
    outside = tmp_path / "outside.mp3"
    target.write_bytes(b"inside")
    outside.write_bytes(b"outside")
    original_delete = liner_files._delete_opened_file

    def swap_target_then_delete(*args: Any, **kwargs: Any) -> None:
        target.rename(moved)
        try:
            target.symlink_to(outside)
        except OSError:
            target.write_bytes(b"outside-root")
        original_delete(*args, **kwargs)

    monkeypatch.setattr(liner_files, "_delete_opened_file", swap_target_then_delete)
    with pytest.raises(InvalidLinerName, match="changed during deletion"):
        delete_liner_file(root, target.name)
    assert outside.read_bytes() == b"outside"
    assert moved.read_bytes() == b"inside"
    assert target.is_symlink()
    assert target.resolve() == outside


@pytest.mark.skipif(os.name != "nt", reason="Windows held-handle behavior")
def test_windows_delete_target_swap_deletes_only_opened_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    root.mkdir()
    target = root / "clip.mp3"
    moved = root / "opened-original.mp3"
    outside = tmp_path / "outside.mp3"
    target.write_bytes(b"inside")
    outside.write_bytes(b"outside")
    original_delete = liner_files._delete_opened_file

    def swap_target_then_delete(*args: Any, **kwargs: Any) -> None:
        target.rename(moved)
        target.write_bytes(b"replacement")
        original_delete(*args, **kwargs)

    monkeypatch.setattr(liner_files, "_delete_opened_file", swap_target_then_delete)
    delete_liner_file(root, target.name)
    assert not moved.exists()
    assert target.read_bytes() == b"replacement"
    assert outside.read_bytes() == b"outside"


def test_open_uses_pinned_root_when_configured_path_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    moved = tmp_path / "original-liners"
    root.mkdir()
    (root / "clip.mp3").write_bytes(b"inside")
    original_open = liner_files._open_relative_file

    def swap_root_then_open(*args: Any, **kwargs: Any):
        root.rename(moved)
        root.mkdir()
        (root / "clip.mp3").write_bytes(b"outside-root")
        return original_open(*args, **kwargs)

    monkeypatch.setattr(liner_files, "_open_relative_file", swap_root_then_open)
    opened = open_liner_file(root, "clip.mp3")
    try:
        assert opened.file.read() == b"inside"
    finally:
        opened.file.close()
    assert (root / "clip.mp3").read_bytes() == b"outside-root"


@pytest.mark.asyncio
async def test_non_replace_does_not_depend_on_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsupported_hard_link(*args: Any, **kwargs: Any) -> None:
        raise OSError("hard links unsupported")

    monkeypatch.setattr("autodj.liner_files.os.link", unsupported_hard_link)
    target, _size = await store_liner_upload(
        tmp_path / "liners",
        "clip.mp3",
        BytesReader(b"new"),
        max_bytes=50,
        replace=False,
    )
    assert target.read_bytes() == b"new"


@pytest.mark.asyncio
async def test_post_publish_directory_flush_failure_returns_committed_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    root.mkdir()
    target = root / "clip.mp3"
    target.write_bytes(b"old")
    calls = 0
    original_flush = liner_files._flush_directory

    def fail_second_flush(handle: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("post-publish flush failed")
        original_flush(handle)

    monkeypatch.setattr(liner_files, "_flush_directory", fail_second_flush)
    returned, size = await store_liner_upload(
        root, "clip.mp3", BytesReader(b"new"), max_bytes=50, replace=True
    )
    assert size == 3
    assert returned.name == "clip.mp3"
    assert target.read_bytes() == b"new"


@pytest.mark.asyncio
async def test_post_publish_directory_flush_base_exception_returns_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    calls = 0
    original_flush = liner_files._flush_directory

    def fail_second_flush(handle: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise UploadAborted
        original_flush(handle)

    monkeypatch.setattr(liner_files, "_flush_directory", fail_second_flush)
    returned, size = await store_liner_upload(
        root, "clip.mp3", BytesReader(b"new"), max_bytes=50, replace=False
    )
    assert size == 3
    assert returned.read_bytes() == b"new"


@pytest.mark.asyncio
async def test_post_publish_cleanup_failure_returns_committed_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    original_cleanup = liner_files._cleanup_staged_upload

    def fail_cleanup(*args: Any, **kwargs: Any) -> None:
        original_cleanup(*args, **kwargs)
        raise OSError("cleanup failed after publish")

    monkeypatch.setattr(liner_files, "_cleanup_staged_upload", fail_cleanup)
    target, size = await store_liner_upload(
        root, "clip.mp3", BytesReader(b"new"), max_bytes=50, replace=False
    )
    assert size == 3
    assert target.read_bytes() == b"new"


def test_windows_directory_flush_dispatches_to_handle_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    class FlushReached(Exception):
        pass

    def reached(handle: int) -> None:
        assert handle == 123
        raise FlushReached

    monkeypatch.setattr(liner_files, "_flush_windows_directory", reached)
    pinned = liner_files._PinnedRoot(Path("unused"), 123, True)
    with pytest.raises(FlushReached):
        liner_files._flush_directory(pinned)


@pytest.mark.skipif(os.name != "nt", reason="Windows directory-flush regression")
@pytest.mark.parametrize("winerror", [1, 5, 6, 50])
def test_windows_unsupported_directory_flush_errors_are_best_effort(
    winerror: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    monkeypatch.setattr(liner_files._kernel32, "FlushFileBuffers", lambda _handle: False)
    monkeypatch.setattr(liner_files, "_get_last_error", lambda: winerror)
    liner_files._flush_windows_directory(123)


@pytest.mark.skipif(os.name != "nt", reason="Windows directory-flush regression")
def test_windows_unexpected_directory_flush_error_propagates_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    monkeypatch.setattr(liner_files._kernel32, "FlushFileBuffers", lambda _handle: False)
    monkeypatch.setattr(liner_files, "_get_last_error", lambda: 32)
    with pytest.raises(OSError):
        liner_files._flush_windows_directory(123)


def test_non_replace_race_reaches_publication_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    root.mkdir()
    barrier = threading.Barrier(2, timeout=10)
    original_publish = liner_files._publish_staged_file

    def synchronized_publish(*args: Any, **kwargs: Any) -> None:
        barrier.wait(timeout=10)
        original_publish(*args, **kwargs)

    monkeypatch.setattr(liner_files, "_publish_staged_file", synchronized_publish)

    def upload(payload: bytes) -> str:
        try:
            asyncio.run(
                store_liner_upload(
                    root,
                    "barrier-race.mp3",
                    BytesReader(payload),
                    max_bytes=50,
                    replace=False,
                )
            )
        except LinerConflictError:
            return "conflict"
        return "stored"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(upload, (b"one", b"two")))
    assert outcomes == ["conflict", "stored"]


def _upload_scope(content_length: str | None = None) -> dict[str, Any]:
    headers = [(b"content-type", b"multipart/form-data; boundary=x")]
    if content_length is not None:
        headers.append((b"content-length", content_length.encode("ascii")))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/liners/upload",
        "raw_path": b"/api/liners/upload",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }


@pytest.mark.asyncio
async def test_body_limit_rejects_large_declared_length_without_calling_parser() -> None:
    downstream_called = False
    receive_called = False
    sent: list[dict[str, Any]] = []

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        nonlocal downstream_called
        downstream_called = True

    async def receive() -> dict[str, Any]:
        nonlocal receive_called
        receive_called = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    middleware = LinerUploadBodyLimitMiddleware(
        downstream, max_file_bytes=lambda: 10, multipart_overhead_bytes=5
    )
    await middleware(_upload_scope("16"), receive, send)
    assert sent[0]["status"] == 413
    assert downstream_called is False
    assert receive_called is False


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_length", [None, "1"])
async def test_body_limit_stops_chunked_or_lying_body_before_parser_cap(
    declared_length: str | None,
) -> None:
    messages = iter(
        [
            {"type": "http.request", "body": b"12345678", "more_body": True},
            {"type": "http.request", "body": b"abcdefgh", "more_body": False},
        ]
    )
    parser_bytes = 0
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(messages)

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        nonlocal parser_bytes
        while True:
            message = await receive()
            parser_bytes += len(message.get("body", b""))
            if not message.get("more_body", False):
                break

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    middleware = LinerUploadBodyLimitMiddleware(
        downstream, max_file_bytes=lambda: 10, multipart_overhead_bytes=5
    )
    await middleware(_upload_scope(declared_length), receive, send)
    assert sent[0]["status"] == 413
    assert parser_bytes == 8


def test_non_replace_upload_is_atomic_across_processes(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    root.mkdir()
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    publication_ready = context.Queue()
    publish = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_process_upload,
            args=(
                str(root),
                payload,
                ready,
                start,
                publication_ready,
                publish,
                results,
            ),
        )
        for payload in (b"first", b"second")
    ]
    for process in processes:
        process.start()
    assert ready.get(timeout=30) is True
    assert ready.get(timeout=30) is True
    start.set()
    assert publication_ready.get(timeout=10) is True
    assert publication_ready.get(timeout=10) is True
    publish.set()
    outcomes = sorted(results.get(timeout=10) for _ in processes)
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert outcomes == ["conflict", "stored"]
    assert (root / "race.mp3").read_bytes() in {b"first", b"second"}
    assert list(root.glob(".liner-upload-*")) == []


def test_threaded_non_replace_upload_has_one_winner(tmp_path: Path) -> None:
    root = tmp_path / "liners"

    def upload(payload: bytes) -> str:
        try:
            asyncio.run(
                store_liner_upload(
                    root,
                    "thread-race.mp3",
                    BytesReader(payload),
                    max_bytes=50,
                    replace=False,
                )
            )
        except LinerConflictError:
            return "conflict"
        return "stored"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(upload, (b"one", b"two")))
    assert outcomes == ["conflict", "stored"]
    assert (root / "thread-race.mp3").read_bytes() in {b"one", b"two"}


@pytest.mark.parametrize(
    ("value", "file_size", "expected"),
    [
        ("bytes=2-5", 10, (2, 6)),
        ("BYTES=7-", 10, (7, 10)),
        ("bytes=-3", 10, (7, 10)),
        ("bytes=-30", 10, (0, 10)),
        ("bytes=2-99", 10, (2, 10)),
    ],
)
def test_parse_liner_range_accepts_single_bounded_ranges(
    value: str, file_size: int, expected: tuple[int, int]
) -> None:
    assert parse_liner_range(value, file_size) == expected


@pytest.mark.parametrize(
    "value",
    [
        "items=0-1",
        "bytes=0-1,3-4",
        "bytes=3",
        "bytes=-0",
        "bytes=abc-def",
        "bytes=-1-2",
        "bytes=5-3",
    ],
)
def test_parse_liner_range_rejects_malformed_ranges(value: str) -> None:
    with pytest.raises(MalformedLinerRange):
        parse_liner_range(value, 10)


@pytest.mark.parametrize(("value", "file_size"), [("bytes=0-1", 0), ("bytes=10-", 10)])
def test_parse_liner_range_rejects_unsatisfiable_ranges(value: str, file_size: int) -> None:
    with pytest.raises(LinerRangeNotSatisfiable) as exc_info:
        parse_liner_range(value, file_size)
    assert exc_info.value.args == (file_size,)


@pytest.mark.asyncio
async def test_iter_opened_liner_honors_bounds_and_closes_file() -> None:
    handle = io.BytesIO(b"0123456789")
    opened = OpenedLiner(file=handle, stat_result=os.stat_result((0,) * 10))

    chunks = [chunk async for chunk in iter_opened_liner(opened, start=2, end=6)]

    assert b"".join(chunks) == b"2345"
    assert handle.closed


@pytest.mark.asyncio
async def test_iter_opened_liner_reads_to_eof_and_closes_file() -> None:
    handle = io.BytesIO(b"payload")
    opened = OpenedLiner(file=handle, stat_result=os.stat_result((0,) * 10))

    chunks = [chunk async for chunk in iter_opened_liner(opened)]

    assert chunks == [b"payload"]
    assert handle.closed


def test_body_limit_requires_positive_multipart_overhead() -> None:
    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        return None

    with pytest.raises(ValueError, match="must be positive"):
        LinerUploadBodyLimitMiddleware(
            downstream, max_file_bytes=lambda: 1, multipart_overhead_bytes=0
        )


@pytest.mark.asyncio
async def test_body_limit_passes_non_upload_scope_through() -> None:
    called = False

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        nonlocal called
        called = True

    middleware = LinerUploadBodyLimitMiddleware(downstream, max_file_bytes=lambda: 1)
    await middleware({"type": "websocket"}, None, None)

    assert called


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_limit", [True, "10", 0, -1])
async def test_body_limit_falls_back_for_invalid_configured_limit(
    configured_limit: object,
) -> None:
    called = False

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        nonlocal called
        called = True

    middleware = LinerUploadBodyLimitMiddleware(downstream, max_file_bytes=lambda: configured_limit)
    await middleware(_upload_scope("1024"), None, None)

    assert called


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_length", ["-1", "not-an-integer"])
async def test_body_limit_rejects_invalid_declared_length(declared_length: str) -> None:
    sent: list[dict[str, Any]] = []

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        raise AssertionError("downstream must not run")

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    middleware = LinerUploadBodyLimitMiddleware(
        downstream, max_file_bytes=lambda: 10, multipart_overhead_bytes=5
    )
    await middleware(_upload_scope(declared_length), None, send)

    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_body_limit_does_not_replace_started_response() -> None:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"123456", "more_body": False}

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200})
        await receive()

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    middleware = LinerUploadBodyLimitMiddleware(
        downstream, max_file_bytes=lambda: 1, multipart_overhead_bytes=1
    )
    await middleware(_upload_scope(), receive, send)

    assert sent == [{"type": "http.response.start", "status": 200}]


def _fake_directory(*, uid: int = 42, mode: int = 0o700, inode: int = 2) -> Any:
    return SimpleNamespace(
        st_mode=stat.S_IFDIR | mode,
        st_uid=uid,
        st_dev=1,
        st_ino=inode,
    )


def test_posix_private_directory_policy_accepts_owner_only_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    monkeypatch.setattr(liner_files._posix_os, "geteuid", lambda: 42, raising=False)

    assert liner_files._require_private_posix_directory(
        _fake_directory(), description="staging"
    ) == (1, 2)


@pytest.mark.parametrize(
    "metadata",
    [
        SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=42, st_dev=1, st_ino=2),
        _fake_directory(uid=7),
        _fake_directory(mode=0o722),
    ],
)
def test_posix_private_directory_policy_rejects_unsafe_metadata(
    metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    monkeypatch.setattr(liner_files._posix_os, "geteuid", lambda: 42, raising=False)

    with pytest.raises(InvalidLinerName, match="private owner-controlled"):
        liner_files._require_private_posix_directory(metadata, description="staging")


def test_posix_cleanup_helpers_bind_actions_to_expected_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    metadata = _fake_directory()
    rmdir = MagicMock()
    unlink = MagicMock()
    close = MagicMock()
    monkeypatch.setattr(liner_files.os, "stat", MagicMock(return_value=metadata))
    monkeypatch.setattr(liner_files.os, "rmdir", rmdir)
    monkeypatch.setattr(liner_files.os, "unlink", unlink)
    monkeypatch.setattr(liner_files.os, "close", close)

    liner_files._remove_bound_posix_directory(10, "stage", (1, 2), description="stage")
    liner_files._close_posix_directory(11, description="stage")
    liner_files._unlink_posix_entry(10, "upload.tmp", description="upload")

    rmdir.assert_called_once_with("stage", dir_fd=10)
    close.assert_called_once_with(11)
    unlink.assert_called_once_with("upload.tmp", dir_fd=10)


@pytest.mark.parametrize("helper", ["remove", "close", "unlink"])
def test_posix_cleanup_helpers_absorb_cleanup_failures(
    helper: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from autodj import liner_files

    if helper == "remove":
        monkeypatch.setattr(liner_files.os, "stat", MagicMock(side_effect=OSError("busy")))
        liner_files._remove_bound_posix_directory(10, "stage", (1, 2), description="stage")
    elif helper == "close":
        monkeypatch.setattr(liner_files.os, "close", MagicMock(side_effect=OSError("busy")))
        liner_files._close_posix_directory(10, description="stage")
    else:
        monkeypatch.setattr(liner_files.os, "unlink", MagicMock(side_effect=OSError("busy")))
        liner_files._unlink_posix_entry(10, "upload.tmp", description="upload")

    assert "Unable to" in caplog.text


@pytest.mark.parametrize("helper", ["remove", "unlink"])
def test_posix_cleanup_helpers_ignore_missing_entries(
    helper: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    if helper == "remove":
        monkeypatch.setattr(
            liner_files.os, "stat", MagicMock(side_effect=FileNotFoundError("gone"))
        )
        liner_files._remove_bound_posix_directory(10, "stage", (1, 2), description="stage")
    else:
        monkeypatch.setattr(
            liner_files.os, "unlink", MagicMock(side_effect=FileNotFoundError("gone"))
        )
        liner_files._unlink_posix_entry(10, "upload.tmp", description="upload")


def test_make_bound_posix_directory_verifies_all_three_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    metadata = _fake_directory()
    root = liner_files._PinnedRoot(Path("root"), 10, False)
    monkeypatch.setattr(liner_files._posix_os, "geteuid", lambda: 42, raising=False)
    monkeypatch.setattr(liner_files._posix_os, "O_DIRECTORY", 0, raising=False)
    monkeypatch.setattr(liner_files._posix_os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(liner_files.secrets, "token_hex", lambda _size: "abc")
    monkeypatch.setattr(liner_files.os, "mkdir", MagicMock())
    monkeypatch.setattr(liner_files.os, "stat", MagicMock(return_value=metadata))
    monkeypatch.setattr(liner_files.os, "open", MagicMock(return_value=11))
    monkeypatch.setattr(liner_files.os, "fstat", MagicMock(return_value=metadata))

    result = liner_files._make_bound_posix_directory(root, prefix=".stage-", description="stage")

    assert result == (".stage-abc", 11, (1, 2))


def test_make_bound_posix_directory_cleans_identity_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    created = _fake_directory(inode=2)
    changed = _fake_directory(inode=3)
    root = liner_files._PinnedRoot(Path("root"), 10, False)
    close = MagicMock()
    remove = MagicMock()
    monkeypatch.setattr(liner_files._posix_os, "geteuid", lambda: 42, raising=False)
    monkeypatch.setattr(liner_files._posix_os, "O_DIRECTORY", 0, raising=False)
    monkeypatch.setattr(liner_files._posix_os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(liner_files.os, "mkdir", MagicMock())
    monkeypatch.setattr(liner_files.os, "stat", MagicMock(side_effect=[created, changed]))
    monkeypatch.setattr(liner_files.os, "open", MagicMock(return_value=11))
    monkeypatch.setattr(liner_files.os, "fstat", MagicMock(return_value=created))
    monkeypatch.setattr(liner_files, "_close_posix_directory", close)
    monkeypatch.setattr(liner_files, "_remove_bound_posix_directory", remove)

    with pytest.raises(InvalidLinerName, match="changed during secure open"):
        liner_files._make_bound_posix_directory(root, prefix=".stage-", description="stage")

    close.assert_called_once_with(11, description="stage")
    remove.assert_called_once()


def test_make_bound_posix_directory_rejects_exhausted_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("root"), 10, False)
    mkdir = MagicMock(side_effect=FileExistsError("collision"))
    monkeypatch.setattr(liner_files.os, "mkdir", mkdir)

    with pytest.raises(liner_files.LinerStorageUnsupportedError, match="unable to allocate"):
        liner_files._make_bound_posix_directory(root, prefix=".stage-", description="stage")

    assert mkdir.call_count == 128


def test_make_and_cleanup_mocked_posix_staged_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("root"), 10, False)
    file = io.BytesIO()
    monkeypatch.setattr(liner_files._posix_os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(
        liner_files,
        "_make_bound_posix_directory",
        MagicMock(return_value=(".stage", 11, (1, 2))),
    )
    monkeypatch.setattr(liner_files.os, "open", MagicMock(return_value=12))
    monkeypatch.setattr(liner_files.os, "fdopen", MagicMock(return_value=file))
    unlink = MagicMock()
    close = MagicMock()
    remove = MagicMock()
    monkeypatch.setattr(liner_files, "_unlink_posix_entry", unlink)
    monkeypatch.setattr(liner_files, "_close_posix_directory", close)
    monkeypatch.setattr(liner_files, "_remove_bound_posix_directory", remove)

    staged = liner_files._make_staged_upload(root)
    liner_files._cleanup_staged_upload(staged, published=False)

    assert staged.stage_identity == (1, 2)
    assert file.closed
    unlink.assert_called_once()
    close.assert_called_once()
    remove.assert_called_once()


def test_mocked_posix_staged_upload_cleans_fdopen_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("root"), 10, False)
    monkeypatch.setattr(liner_files._posix_os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(
        liner_files,
        "_make_bound_posix_directory",
        MagicMock(return_value=(".stage", 11, (1, 2))),
    )
    monkeypatch.setattr(liner_files.os, "open", MagicMock(return_value=12))
    monkeypatch.setattr(liner_files.os, "fdopen", MagicMock(side_effect=OSError("fdopen")))
    close_fd = MagicMock()
    monkeypatch.setattr(liner_files.os, "close", close_fd)
    monkeypatch.setattr(liner_files, "_unlink_posix_entry", MagicMock())
    monkeypatch.setattr(liner_files, "_close_posix_directory", MagicMock())
    monkeypatch.setattr(liner_files, "_remove_bound_posix_directory", MagicMock())

    with pytest.raises(OSError, match="fdopen"):
        liner_files._make_staged_upload(root)

    close_fd.assert_called_once_with(12)


@pytest.mark.parametrize(
    ("result", "error", "exception"),
    [
        (0, 0, None),
        (-1, errno.EEXIST, LinerConflictError),
        (-1, errno.ENOSYS, LinerStorageUnsupportedError),
        (-1, errno.EIO, OSError),
    ],
)
def test_posix_rename_noreplace_maps_native_results(
    result: int,
    error: int,
    exception: type[BaseException] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    rename = MagicMock(return_value=result)
    monkeypatch.setattr(
        liner_files.ctypes,
        "CDLL",
        MagicMock(return_value=SimpleNamespace(renameat2=rename)),
    )
    monkeypatch.setattr(liner_files.ctypes, "get_errno", lambda: error)

    if exception is None:
        liner_files._rename_noreplace_posix(1, "source", 2, "target")
    else:
        with pytest.raises(exception):
            liner_files._rename_noreplace_posix(1, "source", 2, "target")


def test_posix_rename_noreplace_requires_native_primitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    monkeypatch.setattr(liner_files.ctypes, "CDLL", MagicMock(return_value=SimpleNamespace()))

    with pytest.raises(LinerStorageUnsupportedError, match="unavailable"):
        liner_files._rename_noreplace_posix(1, "source", 2, "target")


def test_posix_publish_dispatches_replace_and_noreplace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("root"), 10, False)
    staged = liner_files._StagedUpload(root, 11, ".stage", "upload.tmp", io.BytesIO())
    replace = MagicMock()
    noreplace = MagicMock()
    monkeypatch.setattr(liner_files.os, "replace", replace)
    monkeypatch.setattr(liner_files, "_rename_noreplace_posix", noreplace)

    liner_files._publish_staged_file(staged, "clip.mp3", replace=True)
    liner_files._publish_staged_file(staged, "clip.mp3", replace=False)

    replace.assert_called_once_with("upload.tmp", "clip.mp3", src_dir_fd=11, dst_dir_fd=10)
    noreplace.assert_called_once_with(11, "upload.tmp", 10, "clip.mp3")


def test_posix_open_relative_file_rejects_nonregular_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("root"), 10, False)
    close = MagicMock()
    monkeypatch.setattr(liner_files._posix_os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(liner_files.os, "open", MagicMock(return_value=12))
    monkeypatch.setattr(liner_files.os, "fstat", MagicMock(return_value=_fake_directory()))
    monkeypatch.setattr(liner_files.os, "close", close)

    with pytest.raises(FileNotFoundError):
        liner_files._open_relative_file(root, "clip.mp3")

    close.assert_called_once_with(12)


def test_delete_reports_pre_and_post_commit_close_failures(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from autodj import liner_files

    pinned = liner_files._PinnedRoot(Path("root"), 10, False)
    monkeypatch.setattr(liner_files, "_open_pinned_root", MagicMock(return_value=pinned))
    monkeypatch.setattr(liner_files, "_flush_directory", MagicMock(side_effect=OSError("flush")))
    monkeypatch.setattr(liner_files, "_close_root", MagicMock(side_effect=OSError("close")))
    monkeypatch.setattr(liner_files, "_delete_relative_file", MagicMock())

    delete_liner_file(Path("root"), "clip.mp3")
    assert "after committed liner deletion" in caplog.text

    caplog.clear()
    monkeypatch.setattr(
        liner_files, "_delete_relative_file", MagicMock(side_effect=OSError("delete"))
    )
    with pytest.raises(OSError, match="delete"):
        delete_liner_file(Path("root"), "clip.mp3")
    assert "after failed liner deletion" in caplog.text


def test_open_posix_root_walks_and_closes_parent_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    close = MagicMock()
    monkeypatch.setattr(liner_files._posix_os, "O_DIRECTORY", 0, raising=False)
    monkeypatch.setattr(liner_files._posix_os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(liner_files.os, "open", MagicMock(side_effect=[10, 11]))
    monkeypatch.setattr(liner_files.os, "close", close)

    pinned = liner_files._open_posix_root(Path("C:/liners"), create=False, mutate=False)

    assert pinned.handle == 11
    assert not pinned.windows
    close.assert_called_once_with(10)


def test_open_posix_root_creates_missing_component_and_checks_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    metadata = _fake_directory()
    mkdir = MagicMock()
    monkeypatch.setattr(liner_files._posix_os, "O_DIRECTORY", 0, raising=False)
    monkeypatch.setattr(liner_files._posix_os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(
        liner_files.os,
        "open",
        MagicMock(side_effect=[10, FileNotFoundError("missing"), 11]),
    )
    monkeypatch.setattr(liner_files.os, "mkdir", mkdir)
    monkeypatch.setattr(liner_files.os, "close", MagicMock())
    monkeypatch.setattr(liner_files.os, "fstat", MagicMock(return_value=metadata))
    monkeypatch.setattr(liner_files._posix_os, "geteuid", lambda: 42, raising=False)

    pinned = liner_files._open_posix_root(Path("C:/liners"), create=True, mutate=True)

    assert pinned.handle == 11
    mkdir.assert_called_once_with("liners", mode=0o755, dir_fd=10)


def test_open_pinned_root_and_close_dispatch_to_posix_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    expected = liner_files._PinnedRoot(Path("root"), 10, False)
    open_posix = MagicMock(return_value=expected)
    close = MagicMock()
    monkeypatch.setattr(liner_files.os, "name", "posix")
    monkeypatch.setattr(liner_files, "_open_posix_root", open_posix)
    monkeypatch.setattr(liner_files.os, "close", close)

    actual = liner_files._open_pinned_root(Path("root"), create=True, write=True, mutate=True)
    liner_files._close_root(actual)

    assert actual is expected
    open_posix.assert_called_once_with(Path("root"), create=True, mutate=True)
    close.assert_called_once_with(10)


def _patch_posix_probe_descriptors(
    liner_files: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[MagicMock, MagicMock]:
    open_file = MagicMock(side_effect=[20, 21])
    close = MagicMock()
    monkeypatch.setattr(liner_files._posix_os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(liner_files.os, "open", open_file)
    monkeypatch.setattr(liner_files.os, "close", close)
    monkeypatch.setattr(liner_files, "_unlink_posix_entry", MagicMock())
    return open_file, close


def test_posix_delete_rollback_probe_accepts_native_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    _patch_posix_probe_descriptors(liner_files, monkeypatch)
    monkeypatch.setattr(
        liner_files,
        "_rename_noreplace_posix",
        MagicMock(side_effect=LinerConflictError("target")),
    )

    liner_files._verify_posix_delete_rollback(10)


def test_posix_delete_rollback_probe_accepts_hard_link_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    _patch_posix_probe_descriptors(liner_files, monkeypatch)
    monkeypatch.setattr(
        liner_files,
        "_rename_noreplace_posix",
        MagicMock(side_effect=LinerStorageUnsupportedError("unavailable")),
    )
    unlink = MagicMock()
    link = MagicMock()
    monkeypatch.setattr(liner_files.os, "unlink", unlink)
    monkeypatch.setattr(liner_files.os, "link", link)

    liner_files._verify_posix_delete_rollback(10)

    unlink.assert_called_once()
    link.assert_called_once()


def test_posix_delete_rollback_probe_maps_unsupported_hard_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    _patch_posix_probe_descriptors(liner_files, monkeypatch)
    monkeypatch.setattr(
        liner_files,
        "_rename_noreplace_posix",
        MagicMock(side_effect=LinerStorageUnsupportedError("unavailable")),
    )
    monkeypatch.setattr(liner_files.os, "unlink", MagicMock())
    monkeypatch.setattr(
        liner_files.os,
        "link",
        MagicMock(side_effect=OSError(errno.EOPNOTSUPP, "unsupported")),
    )

    with pytest.raises(LinerStorageUnsupportedError, match="rollback unavailable"):
        liner_files._verify_posix_delete_rollback(10)


def test_posix_delete_rollback_probe_preserves_unexpected_hard_link_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    _patch_posix_probe_descriptors(liner_files, monkeypatch)
    monkeypatch.setattr(
        liner_files,
        "_rename_noreplace_posix",
        MagicMock(side_effect=LinerStorageUnsupportedError("unavailable")),
    )
    monkeypatch.setattr(liner_files.os, "unlink", MagicMock())
    monkeypatch.setattr(
        liner_files.os,
        "link",
        MagicMock(side_effect=OSError(errno.EIO, "disk error")),
    )

    with pytest.raises(OSError, match="disk error"):
        liner_files._verify_posix_delete_rollback(10)


def test_posix_delete_rollback_probe_rejects_overwriting_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    _patch_posix_probe_descriptors(liner_files, monkeypatch)
    monkeypatch.setattr(liner_files, "_rename_noreplace_posix", MagicMock())

    with pytest.raises(LinerStorageUnsupportedError, match="did not preserve"):
        liner_files._verify_posix_delete_rollback(10)


def test_restore_posix_delete_uses_native_and_hard_link_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("root"), 10, False)
    rename = MagicMock()
    monkeypatch.setattr(liner_files, "_rename_noreplace_posix", rename)
    liner_files._restore_posix_delete(11, root, "clip.mp3")
    rename.assert_called_once()

    rename.side_effect = LinerStorageUnsupportedError("unavailable")
    link = MagicMock()
    unlink = MagicMock()
    monkeypatch.setattr(liner_files.os, "link", link)
    monkeypatch.setattr(liner_files.os, "unlink", unlink)
    liner_files._restore_posix_delete(11, root, "clip.mp3")
    link.assert_called_once()
    unlink.assert_called_once_with("victim", dir_fd=11)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FileExistsError("race"), LinerConflictError),
        (OSError(errno.EXDEV, "cross-device"), LinerStorageUnsupportedError),
        (OSError(errno.EIO, "disk"), OSError),
    ],
)
def test_restore_posix_delete_maps_link_failures(
    error: OSError,
    expected: type[BaseException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("root"), 10, False)
    monkeypatch.setattr(
        liner_files,
        "_rename_noreplace_posix",
        MagicMock(side_effect=LinerStorageUnsupportedError("unavailable")),
    )
    monkeypatch.setattr(liner_files.os, "link", MagicMock(side_effect=error))

    with pytest.raises(expected):
        liner_files._restore_posix_delete(11, root, "clip.mp3")


def test_delete_opened_posix_file_commits_and_cleans_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("root"), 10, False)
    metadata = SimpleNamespace(st_dev=1, st_ino=2)
    monkeypatch.setattr(liner_files.os, "fstat", MagicMock(return_value=metadata))
    monkeypatch.setattr(
        liner_files,
        "_make_bound_posix_directory",
        MagicMock(return_value=(".delete", 11, (1, 3))),
    )
    monkeypatch.setattr(liner_files, "_verify_posix_delete_rollback", MagicMock())
    rename = MagicMock()
    unlink = MagicMock()
    monkeypatch.setattr(liner_files.os, "rename", rename)
    monkeypatch.setattr(liner_files.os, "stat", MagicMock(return_value=metadata))
    monkeypatch.setattr(liner_files.os, "unlink", unlink)
    monkeypatch.setattr(liner_files, "_close_posix_directory", MagicMock())
    monkeypatch.setattr(liner_files, "_remove_bound_posix_directory", MagicMock())

    liner_files._delete_opened_file(root, "clip.mp3", 12)

    rename.assert_called_once()
    unlink.assert_called_once_with("victim", dir_fd=11)


def test_delete_opened_posix_file_restores_identity_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("root"), 10, False)
    opened = SimpleNamespace(st_dev=1, st_ino=2)
    moved = SimpleNamespace(st_dev=1, st_ino=3)
    restore = MagicMock()
    monkeypatch.setattr(liner_files.os, "fstat", MagicMock(return_value=opened))
    monkeypatch.setattr(
        liner_files,
        "_make_bound_posix_directory",
        MagicMock(return_value=(".delete", 11, (1, 4))),
    )
    monkeypatch.setattr(liner_files, "_verify_posix_delete_rollback", MagicMock())
    monkeypatch.setattr(liner_files.os, "rename", MagicMock())
    monkeypatch.setattr(liner_files.os, "stat", MagicMock(return_value=moved))
    monkeypatch.setattr(liner_files, "_restore_posix_delete", restore)
    monkeypatch.setattr(liner_files, "_close_posix_directory", MagicMock())
    monkeypatch.setattr(liner_files, "_remove_bound_posix_directory", MagicMock())

    with pytest.raises(InvalidLinerName, match="changed during deletion"):
        liner_files._delete_opened_file(root, "clip.mp3", 12)

    restore.assert_called_once_with(11, root, "clip.mp3")


def test_delete_relative_posix_rejects_nonregular_and_closes_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("root"), 10, False)
    close = MagicMock()
    monkeypatch.setattr(liner_files._posix_os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(liner_files.os, "open", MagicMock(return_value=12))
    monkeypatch.setattr(liner_files.os, "fstat", MagicMock(return_value=_fake_directory()))
    monkeypatch.setattr(liner_files.os, "close", close)

    with pytest.raises(FileNotFoundError):
        liner_files._delete_relative_file(root, "clip.mp3")

    close.assert_called_once_with(12)


@pytest.mark.asyncio
async def test_upload_rejects_nonbytes_reader_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    class TextReader:
        async def read(self, size: int = -1) -> Any:
            return "not bytes"

    pinned = liner_files._PinnedRoot(tmp_path, 10, False)
    staged_file = MagicMock()
    staged_file.fileno.return_value = 12
    staged = liner_files._StagedUpload(pinned, 11, ".stage", "upload.tmp", staged_file)
    monkeypatch.setattr(liner_files, "_open_pinned_root", MagicMock(return_value=pinned))
    monkeypatch.setattr(liner_files, "_make_staged_upload", MagicMock(return_value=staged))
    monkeypatch.setattr(
        liner_files.os,
        "fstat",
        MagicMock(return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o600)),
    )
    monkeypatch.setattr(liner_files, "_cleanup_staged_upload", MagicMock())
    monkeypatch.setattr(liner_files, "_close_root", MagicMock())

    with pytest.raises(TypeError, match="must return bytes"):
        await store_liner_upload(tmp_path, "clip.mp3", TextReader(), max_bytes=10, replace=False)


def test_resolve_liner_path_rejects_non_directory_root(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(InvalidLinerName, match="not a directory"):
        resolve_liner_path(root, "clip.mp3")


@pytest.mark.parametrize(
    ("reparse_results", "message"),
    [([True], "configured liner root"), ([False, True], "liner file")],
)
def test_resolve_liner_path_rejects_mocked_reparse_points(
    reparse_results: list[bool],
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    root.mkdir()
    monkeypatch.setattr(liner_files, "_is_reparse_point", MagicMock(side_effect=reparse_results))

    with pytest.raises(InvalidLinerName, match=message):
        resolve_liner_path(root, "clip.mp3")


def test_resolve_liner_path_closes_required_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    root.mkdir()
    file = MagicMock()
    opened = OpenedLiner(file=file, stat_result=os.stat_result((0,) * 10))
    monkeypatch.setattr(liner_files, "open_liner_file", MagicMock(return_value=opened))

    assert resolve_liner_path(root, "clip.mp3", require_file=True) == root / "clip.mp3"
    file.close.assert_called_once()


def test_open_posix_root_closes_anchor_when_child_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    close = MagicMock()
    monkeypatch.setattr(liner_files._posix_os, "O_DIRECTORY", 0, raising=False)
    monkeypatch.setattr(liner_files._posix_os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(
        liner_files.os, "open", MagicMock(side_effect=[10, FileNotFoundError("missing")])
    )
    monkeypatch.setattr(liner_files.os, "close", close)

    with pytest.raises(FileNotFoundError):
        liner_files._open_posix_root(Path("C:/liners"), create=False, mutate=False)

    close.assert_called_once_with(10)


def test_windows_handle_attributes_reports_api_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    monkeypatch.setattr(
        liner_files._kernel32, "GetFileInformationByHandleEx", MagicMock(return_value=False)
    )
    monkeypatch.setattr(liner_files, "_get_last_error", lambda: 5)

    with pytest.raises(OSError):
        liner_files._windows_handle_attributes(123)


def test_windows_file_identity_uses_modern_file_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    def provide_identity(handle: int, _class: int, pointer: Any, _size: int) -> bool:
        assert handle == 123
        info = pointer._obj
        info.VolumeSerialNumber = 42
        for index in range(16):
            info.FileId.Identifier[index] = index
        return True

    monkeypatch.setattr(liner_files._kernel32, "GetFileInformationByHandleEx", provide_identity)

    assert liner_files._windows_file_identity(123) == (
        42,
        b"file-id-128:" + bytes(range(16)),
    )


@pytest.mark.parametrize("failure", ["modern", "classic", "zero-index"])
def test_windows_file_identity_rejects_unstable_storage(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    monkeypatch.setattr(
        liner_files._kernel32, "GetFileInformationByHandleEx", MagicMock(return_value=False)
    )
    if failure == "modern":
        monkeypatch.setattr(liner_files, "_get_last_error", lambda: 32)
        with pytest.raises(OSError):
            liner_files._windows_file_identity(123)
        return

    def classic_identity(handle: int, pointer: Any) -> bool:
        if failure == "classic":
            return False
        info = pointer._obj
        info.VolumeSerialNumber = 42
        info.FileIndexHigh = 0
        info.FileIndexLow = 0
        return True

    monkeypatch.setattr(liner_files, "_get_last_error", lambda: 87)
    monkeypatch.setattr(liner_files._kernel32, "GetFileInformationByHandle", classic_identity)
    expected = OSError if failure == "classic" else LinerStorageUnsupportedError
    with pytest.raises(expected):
        liner_files._windows_file_identity(123)


def test_windows_anchor_rejects_api_failure_and_invalid_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    monkeypatch.setattr(
        liner_files._kernel32,
        "CreateFileW",
        MagicMock(return_value=liner_files._INVALID_HANDLE_VALUE),
    )
    monkeypatch.setattr(liner_files, "_get_last_error", lambda: 2)
    with pytest.raises(OSError):
        liner_files._open_windows_anchor("Q:\\", 0)

    close = MagicMock()
    monkeypatch.setattr(liner_files._kernel32, "CreateFileW", MagicMock(return_value=123))
    monkeypatch.setattr(liner_files, "_windows_handle_attributes", MagicMock(return_value=0))
    monkeypatch.setattr(liner_files, "_close_windows_handle", close)
    with pytest.raises(InvalidLinerName, match="non-reparse directory"):
        liner_files._open_windows_anchor("Q:\\", 0)
    close.assert_called_once_with(123)


@pytest.mark.parametrize(
    ("winerror", "directory", "create", "expected"),
    [
        (2, True, False, FileNotFoundError),
        (5, False, False, FileNotFoundError),
        (80, False, True, FileExistsError),
        (32, False, True, OSError),
    ],
)
def test_nt_create_relative_maps_native_errors(
    winerror: int,
    directory: bool,
    create: bool,
    expected: type[BaseException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    monkeypatch.setattr(liner_files._ntdll, "NtCreateFile", MagicMock(return_value=-1))
    monkeypatch.setattr(
        liner_files._ntdll, "RtlNtStatusToDosError", MagicMock(return_value=winerror)
    )

    with pytest.raises(expected):
        liner_files._nt_create_relative(10, "entry", directory=directory, create=create, access=0)


@pytest.mark.parametrize(
    ("attributes", "directory", "expected"),
    [
        (0x400, False, InvalidLinerName),
        (0, True, InvalidLinerName),
        (0x10, False, FileNotFoundError),
    ],
)
def test_nt_create_relative_validates_opened_entry_type(
    attributes: int,
    directory: bool,
    expected: type[BaseException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    def create(handle_pointer: Any, *_args: Any) -> int:
        handle_pointer._obj.value = 123
        return 0

    close = MagicMock()
    monkeypatch.setattr(liner_files._ntdll, "NtCreateFile", create)
    monkeypatch.setattr(
        liner_files, "_windows_handle_attributes", MagicMock(return_value=attributes)
    )
    monkeypatch.setattr(liner_files, "_close_windows_handle", close)

    with pytest.raises(expected):
        liner_files._nt_create_relative(10, "entry", directory=directory, create=False, access=0)
    close.assert_called_once_with(123)


def test_windows_delete_by_handle_reports_api_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    monkeypatch.setattr(
        liner_files._kernel32, "SetFileInformationByHandle", MagicMock(return_value=False)
    )
    monkeypatch.setattr(liner_files, "_get_last_error", lambda: 32)

    with pytest.raises(OSError):
        liner_files._windows_delete_by_handle(123)


@pytest.mark.parametrize(
    ("winerror", "expected"),
    [(50, LinerStorageUnsupportedError), (32, OSError)],
)
def test_windows_rename_by_handle_maps_native_errors(
    winerror: int,
    expected: type[BaseException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    monkeypatch.setattr(liner_files._ntdll, "NtSetInformationFile", MagicMock(return_value=-1))
    monkeypatch.setattr(
        liner_files._ntdll, "RtlNtStatusToDosError", MagicMock(return_value=winerror)
    )

    with pytest.raises(expected):
        liner_files._windows_rename_by_handle(1, 2, "clip.mp3", replace=False)


@pytest.mark.parametrize("failure", ["file", "stage-delete", "stage-close"])
def test_windows_staged_cleanup_preserves_first_failure(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("Q:/liners"), 10, True)
    file = MagicMock()
    file.closed = False
    file.fileno.return_value = 12
    staged = liner_files._StagedUpload(root, 11, ".stage", "upload.tmp", file)
    monkeypatch.setattr(liner_files, "_get_osfhandle", MagicMock(return_value=22))
    delete = MagicMock()
    close_handle = MagicMock()
    if failure == "file":
        delete.side_effect = [OSError("file delete"), None]
    elif failure == "stage-delete":
        delete.side_effect = OSError("stage delete")
        file.closed = True
    else:
        close_handle.side_effect = OSError("stage close")
        file.closed = True
    monkeypatch.setattr(liner_files, "_windows_delete_by_handle", delete)
    monkeypatch.setattr(liner_files, "_close_windows_handle", close_handle)

    with pytest.raises(OSError, match=failure.split("-")[0]):
        liner_files._cleanup_staged_upload(staged, published=False)


def test_posix_staged_cleanup_rethrows_file_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("root"), 10, False)
    file = MagicMock()
    file.closed = False
    file.close.side_effect = OSError("close failed")
    staged = liner_files._StagedUpload(root, 11, ".stage", "upload.tmp", file, (1, 2))
    monkeypatch.setattr(liner_files, "_unlink_posix_entry", MagicMock())
    monkeypatch.setattr(liner_files, "_close_posix_directory", MagicMock())
    monkeypatch.setattr(liner_files, "_remove_bound_posix_directory", MagicMock())

    with pytest.raises(OSError, match="close failed"):
        liner_files._cleanup_staged_upload(staged, published=False)


@pytest.mark.asyncio
async def test_upload_rejects_nonregular_staging_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    pinned = liner_files._PinnedRoot(tmp_path, 10, False)
    file = MagicMock()
    file.fileno.return_value = 12
    staged = liner_files._StagedUpload(pinned, 11, ".stage", "upload.tmp", file)
    monkeypatch.setattr(liner_files, "_open_pinned_root", MagicMock(return_value=pinned))
    monkeypatch.setattr(liner_files, "_make_staged_upload", MagicMock(return_value=staged))
    monkeypatch.setattr(liner_files.os, "fstat", MagicMock(return_value=_fake_directory()))
    monkeypatch.setattr(liner_files, "_cleanup_staged_upload", MagicMock())
    monkeypatch.setattr(liner_files, "_close_root", MagicMock())

    with pytest.raises(LinerStorageUnsupportedError, match="not a regular file"):
        await store_liner_upload(
            tmp_path, "clip.mp3", BytesReader(b"data"), max_bytes=10, replace=False
        )


def test_mocked_posix_staged_upload_preserves_fdopen_error_when_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("root"), 10, False)
    monkeypatch.setattr(liner_files._posix_os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(
        liner_files,
        "_make_bound_posix_directory",
        MagicMock(return_value=(".stage", 11, (1, 2))),
    )
    monkeypatch.setattr(liner_files.os, "open", MagicMock(return_value=12))
    monkeypatch.setattr(liner_files.os, "fdopen", MagicMock(side_effect=OSError("fdopen")))
    monkeypatch.setattr(liner_files.os, "close", MagicMock(side_effect=UploadAborted()))
    monkeypatch.setattr(liner_files, "_unlink_posix_entry", MagicMock())
    monkeypatch.setattr(liner_files, "_close_posix_directory", MagicMock())
    monkeypatch.setattr(liner_files, "_remove_bound_posix_directory", MagicMock())

    with pytest.raises(OSError, match="fdopen"):
        liner_files._make_staged_upload(root)


def test_mocked_posix_staged_upload_rejects_exhausted_stage_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("root"), 10, False)
    allocate = MagicMock(side_effect=FileExistsError("collision"))
    monkeypatch.setattr(liner_files, "_make_bound_posix_directory", allocate)

    with pytest.raises(LinerStorageUnsupportedError, match="staging directory"):
        liner_files._make_staged_upload(root)

    assert allocate.call_count == 128


def test_windows_full_nt_path_supports_drive_paths() -> None:
    from autodj import liner_files

    assert liner_files._windows_full_nt_path(Path("Q:/liners/clip.mp3")).startswith("\\??\\Q:")


def test_windows_full_nt_path_supports_unc_paths() -> None:
    from autodj import liner_files

    assert liner_files._windows_full_nt_path(Path("//server/share/clip.mp3")) == (
        "\\??\\UNC\\server\\share\\clip.mp3"
    )


def test_verify_windows_root_path_rejects_identity_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("Q:/liners"), 10, True)
    candidate = liner_files._PinnedRoot(root.path, 11, True)
    close = MagicMock()
    monkeypatch.setattr(liner_files, "_open_windows_root", MagicMock(return_value=candidate))
    monkeypatch.setattr(liner_files, "_windows_file_identity", MagicMock(return_value=(2, b"x")))
    monkeypatch.setattr(liner_files, "_close_root", close)

    with pytest.raises(InvalidLinerName, match="changed during secure publish"):
        liner_files._verify_windows_root_path(root, (1, b"y"))

    close.assert_called_once_with(candidate)


def _sharing_violation() -> OSError:
    return OSError(32, "sharing violation", "clip.mp3", 32)


def test_windows_smb_fallback_exhausts_retryable_renames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("Q:/liners"), 10, True)
    staged = liner_files._StagedUpload(root, 11, ".stage", "upload.tmp", MagicMock())
    monkeypatch.setattr(liner_files, "_windows_file_identity", MagicMock(return_value=(1, b"x")))
    monkeypatch.setattr(liner_files, "_verify_windows_root_path", MagicMock())
    monkeypatch.setattr(liner_files, "_close_windows_handle", MagicMock())
    monkeypatch.setattr(
        liner_files,
        "_windows_rename_by_handle",
        MagicMock(side_effect=_sharing_violation()),
    )
    monkeypatch.setattr(liner_files.time, "sleep", MagicMock())

    with pytest.raises(OSError, match="sharing violation"):
        liner_files._publish_windows_smb_fallback(
            staged, 12, "clip.mp3", replace=False, retry_errors={32}
        )


def test_windows_smb_fallback_preserves_nonretryable_rename_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("Q:/liners"), 10, True)
    staged = liner_files._StagedUpload(root, 11, ".stage", "upload.tmp", MagicMock())
    monkeypatch.setattr(liner_files, "_windows_file_identity", MagicMock(return_value=(1, b"x")))
    monkeypatch.setattr(liner_files, "_verify_windows_root_path", MagicMock())
    monkeypatch.setattr(liner_files, "_close_windows_handle", MagicMock())
    monkeypatch.setattr(
        liner_files,
        "_windows_rename_by_handle",
        MagicMock(side_effect=OSError(5, "access denied", "clip.mp3", 5)),
    )

    with pytest.raises(OSError, match="access denied"):
        liner_files._publish_windows_smb_fallback(
            staged, 12, "clip.mp3", replace=False, retry_errors={32}
        )


@pytest.mark.parametrize("mismatch", ["root", "target"])
def test_windows_smb_fallback_logs_postcommit_identity_mismatch(
    mismatch: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("Q:/liners"), 10, True)
    staged = liner_files._StagedUpload(root, 11, ".stage", "upload.tmp", MagicMock())
    reopened = liner_files._PinnedRoot(root.path, 20, True)
    identities = [(1, b"root"), (2, b"source")]
    if mismatch == "root":
        identities.append((3, b"changed"))
    else:
        identities.extend([(1, b"root"), (3, b"changed")])
    monkeypatch.setattr(liner_files, "_windows_file_identity", MagicMock(side_effect=identities))
    monkeypatch.setattr(liner_files, "_verify_windows_root_path", MagicMock())
    monkeypatch.setattr(liner_files, "_close_windows_handle", MagicMock())
    monkeypatch.setattr(liner_files, "_windows_rename_by_handle", MagicMock())
    monkeypatch.setattr(liner_files, "_open_windows_root", MagicMock(return_value=reopened))
    monkeypatch.setattr(liner_files, "_nt_create_relative", MagicMock(return_value=21))
    monkeypatch.setattr(liner_files, "_close_root", MagicMock())

    liner_files._publish_windows_smb_fallback(
        staged, 12, "clip.mp3", replace=False, retry_errors={32}
    )

    assert "post-publish identity validation failed" in caplog.text


def test_flush_directory_dispatches_to_posix_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    from autodj import liner_files

    fsync = MagicMock()
    monkeypatch.setattr(liner_files.os, "fsync", fsync)

    liner_files._flush_directory(liner_files._PinnedRoot(Path("root"), 10, False))

    fsync.assert_called_once_with(10)


@pytest.mark.asyncio
async def test_upload_returns_after_root_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from autodj import liner_files

    pinned = liner_files._PinnedRoot(tmp_path, 10, False)
    file = MagicMock()
    file.fileno.return_value = 12
    staged = liner_files._StagedUpload(pinned, 11, ".stage", "upload.tmp", file)
    monkeypatch.setattr(liner_files, "_open_pinned_root", MagicMock(return_value=pinned))
    monkeypatch.setattr(liner_files, "_make_staged_upload", MagicMock(return_value=staged))
    monkeypatch.setattr(
        liner_files.os,
        "fstat",
        MagicMock(return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o600)),
    )
    monkeypatch.setattr(liner_files.os, "fsync", MagicMock())
    monkeypatch.setattr(liner_files, "_flush_directory", MagicMock())
    monkeypatch.setattr(liner_files, "_publish_staged_file", MagicMock())
    monkeypatch.setattr(liner_files, "_cleanup_staged_upload", MagicMock())
    monkeypatch.setattr(liner_files, "_close_root", MagicMock(side_effect=OSError("close")))

    path, size = await store_liner_upload(
        tmp_path, "clip.mp3", BytesReader(b"data"), max_bytes=10, replace=False
    )

    assert (path, size) == (tmp_path / "clip.mp3", 4)
    assert "Unable to close pinned liner root" in caplog.text


def test_open_relative_windows_closes_raw_handle_when_fd_conversion_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("Q:/liners"), 10, True)
    close = MagicMock()
    monkeypatch.setattr(liner_files, "_nt_create_relative", MagicMock(return_value=12))
    monkeypatch.setattr(liner_files, "_open_osfhandle", MagicMock(side_effect=OSError("convert")))
    monkeypatch.setattr(liner_files, "_close_windows_handle", close)

    with pytest.raises(OSError, match="convert"):
        liner_files._open_relative_file(root, "clip.mp3")

    close.assert_called_once_with(12)


def test_open_liner_file_returns_after_root_close_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from autodj import liner_files

    pinned = liner_files._PinnedRoot(Path("root"), 10, False)
    opened = OpenedLiner(file=MagicMock(), stat_result=os.stat_result((0,) * 10))
    monkeypatch.setattr(liner_files, "_open_pinned_root", MagicMock(return_value=pinned))
    monkeypatch.setattr(liner_files, "_open_relative_file", MagicMock(return_value=opened))
    monkeypatch.setattr(liner_files, "_close_root", MagicMock(side_effect=OSError("close")))

    assert open_liner_file(Path("root"), "clip.mp3") is opened
    assert "Unable to close pinned liner root" in caplog.text


def test_delete_relative_posix_closes_after_success_and_close_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("root"), 10, False)
    monkeypatch.setattr(liner_files._posix_os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(liner_files.os, "open", MagicMock(return_value=12))
    monkeypatch.setattr(
        liner_files.os,
        "fstat",
        MagicMock(return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o600)),
    )
    delete = MagicMock()
    monkeypatch.setattr(liner_files, "_delete_opened_file", delete)
    monkeypatch.setattr(liner_files.os, "close", MagicMock(side_effect=OSError("close")))

    liner_files._delete_relative_file(root, "clip.mp3")

    delete.assert_called_once_with(root, "clip.mp3", 12)
    assert "Unable to close liner delete handle" in caplog.text


def test_delete_opened_posix_file_logs_restore_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from autodj import liner_files

    root = liner_files._PinnedRoot(Path("root"), 10, False)
    opened = SimpleNamespace(st_dev=1, st_ino=2)
    moved = SimpleNamespace(st_dev=1, st_ino=3)
    real_stat = liner_files.os.stat

    def victim_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if os.fsdecode(path) == "victim" and kwargs.get("dir_fd") == 11:
            return moved
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(liner_files.os, "fstat", MagicMock(return_value=opened))
    monkeypatch.setattr(
        liner_files,
        "_make_bound_posix_directory",
        MagicMock(return_value=(".delete", 11, (1, 4))),
    )
    monkeypatch.setattr(liner_files, "_verify_posix_delete_rollback", MagicMock())
    monkeypatch.setattr(liner_files.os, "rename", MagicMock())
    monkeypatch.setattr(liner_files.os, "stat", victim_stat)
    monkeypatch.setattr(
        liner_files, "_restore_posix_delete", MagicMock(side_effect=OSError("restore"))
    )
    monkeypatch.setattr(liner_files, "_close_posix_directory", MagicMock())
    monkeypatch.setattr(liner_files, "_remove_bound_posix_directory", MagicMock())

    with pytest.raises(InvalidLinerName, match="changed during deletion"):
        liner_files._delete_opened_file(root, "clip.mp3", 12)

    assert "Unable to restore liner entry" in caplog.text
