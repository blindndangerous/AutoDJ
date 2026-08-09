from __future__ import annotations

import asyncio
import contextlib
import errno
import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from autodj.liner_files import (
    InvalidLinerName,
    LinerConflictError,
    LinerTooLargeError,
    LinerUploadBodyLimitMiddleware,
    delete_liner_file,
    open_liner_file,
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
    monkeypatch.setattr(liner_files.ctypes, "get_last_error", lambda: 87)

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
    monkeypatch.setattr(liner_files.ctypes, "get_last_error", lambda: 87)
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
    monkeypatch.setattr(liner_files.ctypes, "get_last_error", lambda: winerror)
    liner_files._flush_windows_directory(123)


@pytest.mark.skipif(os.name != "nt", reason="Windows directory-flush regression")
def test_windows_unexpected_directory_flush_error_propagates_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import liner_files

    monkeypatch.setattr(liner_files._kernel32, "FlushFileBuffers", lambda _handle: False)
    monkeypatch.setattr(liner_files.ctypes, "get_last_error", lambda: 32)
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
    assert ready.get(timeout=10) is True
    assert ready.get(timeout=10) is True
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
