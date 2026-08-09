"""Secure filesystem boundary for liner clips and uploads."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import logging
import ntpath
import os
import secrets
import stat
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol

logger = logging.getLogger(__name__)


class InvalidLinerName(ValueError):
    """Raised when a liner name is not one contained plain filename."""


class LinerConflictError(FileExistsError):
    """Raised when a non-replacing upload targets an existing liner."""


class LinerTooLargeError(ValueError):
    """Raised when a streamed upload crosses its configured byte limit."""


class LinerStorageUnsupportedError(OSError):
    """Raised when storage cannot provide required atomic operations."""


class MalformedLinerRange(ValueError):
    """Raised when a byte-range header is malformed or unsupported."""


class LinerRangeNotSatisfiable(ValueError):
    """Raised when a byte range starts beyond the held file."""


class AsyncReader(Protocol):
    async def read(self, size: int = -1) -> bytes:
        """Read at most *size* bytes."""


@dataclass
class OpenedLiner:
    """Held regular-file handle and metadata for race-free streaming."""

    file: BinaryIO
    stat_result: os.stat_result


@dataclass
class _PinnedRoot:
    path: Path
    handle: int
    windows: bool


@dataclass
class _StagedUpload:
    root: _PinnedRoot
    stage_handle: int
    stage_name: str
    file_name: str
    file: BinaryIO
    stage_identity: tuple[int, int] | None = None


_CHUNK_BYTES = 1024 * 1024
DEFAULT_MULTIPART_OVERHEAD_BYTES = 64 * 1024
_WINDOWS_FORBIDDEN = frozenset('<>"|?*')
_WINDOWS_DEVICES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CONIN$",
    "CONOUT$",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
    *(f"COM{i}" for i in "¹²³"),
    *(f"LPT{i}" for i in "¹²³"),
}
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _validate_name(name: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ":" in name
        or "\x00" in name
        or any(character in _WINDOWS_FORBIDDEN or ord(character) < 32 for character in name)
        or name != name.rstrip(" .")
    ):
        raise InvalidLinerName("liner name must be one plain filename")
    device_stem = name.split(".", 1)[0].rstrip(" .").upper()
    is_reserved = getattr(ntpath, "isreserved", lambda _name: False)
    if device_stem in _WINDOWS_DEVICES or is_reserved(name):
        raise InvalidLinerName("reserved device filename")


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
    )


def resolve_liner_path(root: Path, name: str, *, require_file: bool = False) -> Path:
    """Validate one name and return its display path inside *root*.

    Security-sensitive I/O uses pinned handle-relative functions below; callers
    must not use this returned path for mutation or deferred opening.
    """
    _validate_name(name)
    root = Path(root)
    if _is_reparse_point(root):
        raise InvalidLinerName("configured liner root cannot be a reparse point")
    if root.exists() and not root.is_dir():
        raise InvalidLinerName("configured liner root is not a directory")
    resolved_root = root.resolve()
    candidate = resolved_root / name
    if _is_reparse_point(candidate):
        raise InvalidLinerName("liner file cannot be a reparse point")
    target = candidate.resolve()
    try:
        target.relative_to(resolved_root)
    except ValueError as exc:
        raise InvalidLinerName("liner path escapes configured root") from exc
    if require_file:
        opened = open_liner_file(root, name)
        opened.file.close()
    return target


if os.name == "nt":
    import msvcrt
    from ctypes import wintypes

    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_ALL = 0x00000007
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _DELETE = 0x00010000
    _SYNCHRONIZE = 0x00100000
    _FILE_READ_DATA = 0x0001
    _FILE_WRITE_DATA = 0x0002
    _FILE_ADD_SUBDIRECTORY = 0x0004
    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_CREATE = 2
    _FILE_OPEN = 1
    _FILE_DIRECTORY_FILE = 0x00000001
    _FILE_NON_DIRECTORY_FILE = 0x00000040
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    _OBJ_CASE_INSENSITIVE = 0x00000040
    _FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    _FILE_ID_INFO_CLASS = 18
    _FILE_DISPOSITION_INFO_CLASS = 4
    _FILE_RENAME_INFORMATION_NT = 10

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    class _FileId128(ctypes.Structure):
        _fields_ = [("Identifier", wintypes.BYTE * 16)]

    class _FileIdInfo(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", _FileId128),
        ]

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("CreationTime", wintypes.FILETIME),
            ("LastAccessTime", wintypes.FILETIME),
            ("LastWriteTime", wintypes.FILETIME),
            ("VolumeSerialNumber", wintypes.DWORD),
            ("FileSizeHigh", wintypes.DWORD),
            ("FileSizeLow", wintypes.DWORD),
            ("NumberOfLinks", wintypes.DWORD),
            ("FileIndexHigh", wintypes.DWORD),
            ("FileIndexLow", wintypes.DWORD),
        ]

    class _FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", wintypes.BOOLEAN),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    class _FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _kernel32.FlushFileBuffers.restype = wintypes.BOOL
    _kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    _kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    _ntdll.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    ]
    _ntdll.NtCreateFile.restype = ctypes.c_long
    _ntdll.NtSetInformationFile.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
    ]
    _ntdll.NtSetInformationFile.restype = ctypes.c_long
    _ntdll.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
    _ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG


def _close_windows_handle(handle: int) -> None:
    if os.name == "nt" and handle not in (0, _INVALID_HANDLE_VALUE):
        _kernel32.CloseHandle(handle)


def _windows_handle_attributes(handle: int) -> int:
    info = _FileAttributeTagInfo()
    if not _kernel32.GetFileInformationByHandleEx(
        handle, _FILE_ATTRIBUTE_TAG_INFO_CLASS, ctypes.byref(info), ctypes.sizeof(info)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(info.FileAttributes)


def _windows_file_identity(handle: int) -> tuple[int, bytes]:
    info = _FileIdInfo()
    if _kernel32.GetFileInformationByHandleEx(
        handle, _FILE_ID_INFO_CLASS, ctypes.byref(info), ctypes.sizeof(info)
    ):
        return int(info.VolumeSerialNumber), b"file-id-128:" + bytes(info.FileId.Identifier)
    winerror = ctypes.get_last_error()
    if winerror not in {1, 50, 87}:
        raise ctypes.WinError(winerror)
    classic = _ByHandleFileInformation()
    if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(classic)):
        raise ctypes.WinError(ctypes.get_last_error())
    file_index = (int(classic.FileIndexHigh) << 32) | int(classic.FileIndexLow)
    if file_index == 0:
        raise LinerStorageUnsupportedError(
            "storage did not provide a stable liner directory identity"
        )
    return int(classic.VolumeSerialNumber), b"file-index-64:" + file_index.to_bytes(8, "little")


def _open_windows_root(path: Path, *, create: bool, write: bool) -> _PinnedRoot:
    absolute = path.absolute()
    relative_parts = absolute.parts[1:]
    anchor_access = _GENERIC_READ | (_GENERIC_WRITE if write and not relative_parts else 0)
    handles = [_open_windows_anchor(absolute.anchor, anchor_access)]
    try:
        for index, part in enumerate(relative_parts):
            needs_write = write and index == len(relative_parts) - 1
            requested_access = _GENERIC_READ | (_GENERIC_WRITE if needs_write else 0)
            try:
                child_handle = _nt_create_relative(
                    handles[-1],
                    part,
                    directory=True,
                    create=False,
                    access=requested_access,
                )
            except FileNotFoundError:
                if not create:
                    raise
                elevated_parent = _reopen_windows_parent_for_create(
                    absolute.anchor, handles, relative_parts, index
                )
                try:
                    if _windows_file_identity(handles[-1]) != _windows_file_identity(
                        elevated_parent
                    ):
                        raise InvalidLinerName("liner root changed during secure open")
                except BaseException:
                    _close_windows_handle(elevated_parent)
                    raise
                _close_windows_handle(handles[-1])
                handles[-1] = elevated_parent
                try:
                    child_handle = _nt_create_relative(
                        handles[-1],
                        part,
                        directory=True,
                        create=True,
                        access=_GENERIC_READ | _GENERIC_WRITE | _DELETE,
                    )
                except FileExistsError:
                    child_handle = _nt_create_relative(
                        handles[-1],
                        part,
                        directory=True,
                        create=False,
                        access=requested_access,
                    )
            handles.append(child_handle)
        for ancestor_handle in handles[:-1]:
            _close_windows_handle(ancestor_handle)
        return _PinnedRoot(path=absolute, handle=handles[-1], windows=True)
    except BaseException:
        for open_handle in handles:
            _close_windows_handle(open_handle)
        raise


def _open_windows_anchor(anchor: str, access: int) -> int:
    handle = _kernel32.CreateFileW(
        anchor,
        access,
        _FILE_SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    result = int(handle)
    try:
        attributes = _windows_handle_attributes(result)
        if attributes & _REPARSE_ATTRIBUTE or not attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise InvalidLinerName("configured liner root must be a non-reparse directory")
        return result
    except BaseException:
        _close_windows_handle(result)
        raise


def _reopen_windows_parent_for_create(
    anchor: str,
    handles: list[int],
    relative_parts: tuple[str, ...],
    missing_index: int,
) -> int:
    access = _GENERIC_READ | _FILE_ADD_SUBDIRECTORY
    if missing_index == 0:
        return _open_windows_anchor(anchor, access)
    return _nt_create_relative(
        handles[-2],
        relative_parts[missing_index - 1],
        directory=True,
        create=False,
        access=access,
    )


def _nt_create_relative(
    parent_handle: int,
    name: str,
    *,
    directory: bool,
    create: bool,
    access: int,
) -> int:
    name_buffer = ctypes.create_unicode_buffer(name)
    encoded_length = len(name.encode("utf-16-le"))
    unicode_name = _UnicodeString(
        encoded_length, encoded_length, ctypes.cast(name_buffer, wintypes.LPWSTR)
    )
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        parent_handle,
        ctypes.pointer(unicode_name),
        _OBJ_CASE_INSENSITIVE,
        None,
        None,
    )
    io_status = _IoStatusBlock()
    handle = wintypes.HANDLE()
    options = (
        (_FILE_DIRECTORY_FILE if directory else _FILE_NON_DIRECTORY_FILE)
        | _FILE_SYNCHRONOUS_IO_NONALERT
        | _FILE_FLAG_OPEN_REPARSE_POINT
    )
    status = _ntdll.NtCreateFile(
        ctypes.byref(handle),
        access | _SYNCHRONIZE | _FILE_READ_ATTRIBUTES,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        _FILE_ATTRIBUTE_DIRECTORY if directory else _FILE_ATTRIBUTE_NORMAL,
        _FILE_SHARE_ALL,
        _FILE_CREATE if create else _FILE_OPEN,
        options,
        None,
        0,
    )
    if status < 0:
        winerror = int(_ntdll.RtlNtStatusToDosError(status))
        if winerror in {2, 3}:
            raise FileNotFoundError(winerror, os.strerror(winerror), name)
        if winerror == 5 and not directory and not create:
            raise FileNotFoundError(winerror, "not a regular file", name)
        if winerror in {80, 183}:
            raise FileExistsError(winerror, os.strerror(winerror), name)
        raise ctypes.WinError(winerror)
    result = int(handle.value)
    try:
        attributes_value = _windows_handle_attributes(result)
        if attributes_value & _REPARSE_ATTRIBUTE:
            raise InvalidLinerName("liner filesystem entry cannot be a reparse point")
        if directory != bool(attributes_value & _FILE_ATTRIBUTE_DIRECTORY):
            if directory:
                raise InvalidLinerName("liner staging entry is not a directory")
            raise FileNotFoundError(name)
        return result
    except BaseException:
        _close_windows_handle(result)
        raise


def _windows_delete_by_handle(handle: int) -> None:
    disposition = _FileDispositionInfo(True)
    if not _kernel32.SetFileInformationByHandle(
        handle,
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_rename_by_handle(
    source_handle: int, root_handle: int, name: str, *, replace: bool
) -> None:
    encoded_name = name.encode("utf-16-le")
    filename_offset = _FileRenameInfo.FileName.offset
    buffer = ctypes.create_string_buffer(filename_offset + len(encoded_name))
    rename_info = _FileRenameInfo.from_buffer(buffer)
    rename_info.ReplaceIfExists = replace
    rename_info.RootDirectory = root_handle
    rename_info.FileNameLength = len(encoded_name)
    ctypes.memmove(ctypes.addressof(buffer) + filename_offset, encoded_name, len(encoded_name))
    io_status = _IoStatusBlock()
    status = _ntdll.NtSetInformationFile(
        source_handle,
        ctypes.byref(io_status),
        buffer,
        len(buffer),
        _FILE_RENAME_INFORMATION_NT,
    )
    if status < 0:
        winerror = int(_ntdll.RtlNtStatusToDosError(status))
        if not replace and winerror in {80, 183}:
            raise LinerConflictError(name)
        if winerror in {1, 50}:
            raise LinerStorageUnsupportedError(
                winerror, "storage does not support atomic rename-no-replace", name
            )
        raise ctypes.WinError(winerror)


def _posix_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _require_private_posix_directory(
    metadata: os.stat_result, *, description: str
) -> tuple[int, int]:
    # Threat boundary: processes with this effective UID (and root) are trusted.
    # Other writers are excluded before any handle-relative mutation begins.
    effective_uid = os.geteuid()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != effective_uid
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise InvalidLinerName(f"{description} must be a private owner-controlled directory")
    return _posix_identity(metadata)


def _open_posix_root(path: Path, *, create: bool, mutate: bool) -> _PinnedRoot:
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    handle = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=handle)
            except FileNotFoundError:
                if not create:
                    raise
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, mode=0o755, dir_fd=handle)
                child = os.open(part, flags, dir_fd=handle)
            parent = handle
            handle = child
            os.close(parent)
        if mutate:
            _require_private_posix_directory(os.fstat(handle), description="configured liner root")
        return _PinnedRoot(path=absolute, handle=handle, windows=False)
    except BaseException:
        os.close(handle)
        raise


def _open_pinned_root(
    path: Path, *, create: bool, write: bool, mutate: bool = False
) -> _PinnedRoot:
    _validate_name("root-placeholder")
    root_path = Path(path)
    if os.name == "nt":
        return _open_windows_root(root_path, create=create, write=write)
    return _open_posix_root(root_path, create=create, mutate=mutate)


def _close_root(root: _PinnedRoot) -> None:
    if root.windows:
        _close_windows_handle(root.handle)
    else:
        os.close(root.handle)


def _remove_bound_posix_directory(
    root_handle: int,
    name: str,
    identity: tuple[int, int],
    *,
    description: str,
) -> None:
    try:
        named = os.stat(name, dir_fd=root_handle, follow_symlinks=False)
        if stat.S_ISDIR(named.st_mode) and _posix_identity(named) == identity:
            os.rmdir(name, dir_fd=root_handle)
    except FileNotFoundError:
        return
    except BaseException:
        logger.warning("Unable to remove private %s", description, exc_info=True)


def _close_posix_directory(handle: int, *, description: str) -> None:
    try:
        os.close(handle)
    except BaseException:
        logger.warning("Unable to close private %s", description, exc_info=True)


def _unlink_posix_entry(handle: int, name: str, *, description: str) -> None:
    try:
        os.unlink(name, dir_fd=handle)
    except FileNotFoundError:
        return
    except BaseException:
        logger.warning("Unable to unlink private %s", description, exc_info=True)


def _make_bound_posix_directory(
    root: _PinnedRoot, *, prefix: str, description: str
) -> tuple[str, int, tuple[int, int]]:
    for _attempt in range(128):
        name = f"{prefix}{secrets.token_hex(16)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=root.handle)
        except FileExistsError:
            continue
        created_identity: tuple[int, int] | None = None
        handle = -1
        try:
            created = os.stat(name, dir_fd=root.handle, follow_symlinks=False)
            created_identity = _require_private_posix_directory(created, description=description)
            handle = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root.handle,
            )
            opened_identity = _require_private_posix_directory(
                os.fstat(handle), description=description
            )
            named_identity = _require_private_posix_directory(
                os.stat(name, dir_fd=root.handle, follow_symlinks=False),
                description=description,
            )
            if not created_identity == opened_identity == named_identity:
                raise InvalidLinerName(f"{description} changed during secure open")
            return name, handle, opened_identity
        except BaseException:
            if handle >= 0:
                _close_posix_directory(handle, description=description)
            if created_identity is not None:
                _remove_bound_posix_directory(
                    root.handle,
                    name,
                    created_identity,
                    description=description,
                )
            raise
    raise LinerStorageUnsupportedError(f"unable to allocate private {description}")


def _make_staged_upload(root: _PinnedRoot) -> _StagedUpload:
    for _attempt in range(128):
        stage_name = f".liner-upload-{secrets.token_hex(16)}"
        try:
            if root.windows:
                stage_handle = _nt_create_relative(
                    root.handle,
                    stage_name,
                    directory=True,
                    create=True,
                    access=_DELETE | _FILE_WRITE_DATA,
                )
                raw_file_handle: int | None = None
                fd: int | None = None
                try:
                    raw_file_handle = _nt_create_relative(
                        stage_handle,
                        "upload.tmp",
                        directory=False,
                        create=True,
                        access=_DELETE | _FILE_WRITE_DATA,
                    )
                    fd = msvcrt.open_osfhandle(raw_file_handle, os.O_WRONLY | os.O_BINARY)
                    raw_file_handle = None
                    file = os.fdopen(fd, "wb")
                    fd = None
                except BaseException:
                    if fd is not None:
                        try:
                            _windows_delete_by_handle(msvcrt.get_osfhandle(fd))
                        except BaseException:
                            logger.warning(
                                "Unable to delete failed liner upload file",
                                exc_info=True,
                            )
                        try:
                            os.close(fd)
                        except BaseException:
                            logger.warning(
                                "Unable to close failed liner upload descriptor",
                                exc_info=True,
                            )
                    if raw_file_handle is not None:
                        try:
                            _windows_delete_by_handle(raw_file_handle)
                        except BaseException:
                            logger.warning(
                                "Unable to delete failed raw liner upload handle",
                                exc_info=True,
                            )
                        try:
                            _close_windows_handle(raw_file_handle)
                        except BaseException:
                            logger.warning(
                                "Unable to close failed raw liner upload handle",
                                exc_info=True,
                            )
                    try:
                        _windows_delete_by_handle(stage_handle)
                    except BaseException:
                        logger.warning(
                            "Unable to delete failed liner staging directory",
                            exc_info=True,
                        )
                    try:
                        _close_windows_handle(stage_handle)
                    except BaseException:
                        logger.warning(
                            "Unable to close failed liner staging directory",
                            exc_info=True,
                        )
                    raise
            else:
                stage_name, stage_handle, stage_identity = _make_bound_posix_directory(
                    root,
                    prefix=".liner-upload-",
                    description="liner upload staging directory",
                )
                fd = -1
                try:
                    fd = os.open(
                        "upload.tmp",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=stage_handle,
                    )
                    file = os.fdopen(fd, "wb")
                    fd = -1
                except BaseException:
                    if fd >= 0:
                        try:
                            os.close(fd)
                        except BaseException:
                            logger.warning(
                                "Unable to close private liner upload file",
                                exc_info=True,
                            )
                    _unlink_posix_entry(
                        stage_handle,
                        "upload.tmp",
                        description="liner upload staging file",
                    )
                    _close_posix_directory(
                        stage_handle, description="liner upload staging directory"
                    )
                    _remove_bound_posix_directory(
                        root.handle,
                        stage_name,
                        stage_identity,
                        description="liner upload staging directory",
                    )
                    raise
            return _StagedUpload(
                root,
                stage_handle,
                stage_name,
                "upload.tmp",
                file,
                None if root.windows else stage_identity,
            )
        except FileExistsError:
            continue
    raise LinerStorageUnsupportedError("unable to allocate private liner staging directory")


def _cleanup_staged_upload(staged: _StagedUpload, *, published: bool) -> None:
    if staged.root.windows:
        first_error: BaseException | None = None
        try:
            if not staged.file.closed:
                handle = msvcrt.get_osfhandle(staged.file.fileno())
                if not published:
                    try:
                        _windows_delete_by_handle(handle)
                    except BaseException as exc:
                        first_error = exc
                try:
                    staged.file.close()
                except BaseException as exc:
                    first_error = first_error or exc
        except BaseException as exc:
            first_error = first_error or exc
        try:
            _windows_delete_by_handle(staged.stage_handle)
        except BaseException as exc:
            first_error = first_error or exc
        try:
            _close_windows_handle(staged.stage_handle)
        except BaseException as exc:
            first_error = first_error or exc
        if first_error is not None:
            raise first_error
        return

    first_error = None
    try:
        if not staged.file.closed:
            staged.file.close()
    except BaseException as exc:
        first_error = exc
    if not published:
        _unlink_posix_entry(
            staged.stage_handle,
            staged.file_name,
            description="liner upload staging file",
        )
    _close_posix_directory(staged.stage_handle, description="liner upload staging directory")
    if staged.stage_identity is not None:
        _remove_bound_posix_directory(
            staged.root.handle,
            staged.stage_name,
            staged.stage_identity,
            description="liner upload staging directory",
        )
    if first_error is not None:
        raise first_error


def _rename_noreplace_posix(
    source_dir_fd: int, source: str, target_dir_fd: int, target: str
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise LinerStorageUnsupportedError("atomic rename-no-replace unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_dir_fd,
        os.fsencode(source),
        target_dir_fd,
        os.fsencode(target),
        1,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise LinerConflictError(target)
    if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP}:
        raise LinerStorageUnsupportedError(error, "atomic rename-no-replace unavailable", target)
    raise OSError(error, os.strerror(error), target)


def _publish_staged_file(staged: _StagedUpload, name: str, *, replace: bool) -> None:
    if staged.root.windows:
        handle = msvcrt.get_osfhandle(staged.file.fileno())
        _windows_rename_by_handle(handle, staged.root.handle, name, replace=replace)
    elif replace:
        os.replace(
            staged.file_name,
            name,
            src_dir_fd=staged.stage_handle,
            dst_dir_fd=staged.root.handle,
        )
    else:
        _rename_noreplace_posix(staged.stage_handle, staged.file_name, staged.root.handle, name)


def _flush_windows_directory(handle: int) -> None:
    if not _kernel32.FlushFileBuffers(handle):
        winerror = ctypes.get_last_error()
        if winerror in {1, 5, 6, 50}:
            logger.warning("Storage does not support directory durability flush")
            return
        raise ctypes.WinError(winerror)


def _flush_directory(root: _PinnedRoot) -> None:
    if root.windows:
        _flush_windows_directory(root.handle)
    else:
        os.fsync(root.handle)


async def store_liner_upload(
    root: Path,
    name: str,
    reader: AsyncReader,
    *,
    max_bytes: int,
    replace: bool,
) -> tuple[Path, int]:
    """Stream and atomically publish one upload relative to a pinned root."""
    _validate_name(name)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")

    pinned = _open_pinned_root(Path(root), create=True, write=True, mutate=True)
    staged: _StagedUpload | None = None
    published = False
    total = 0
    try:
        staged = _make_staged_upload(pinned)
        if not stat.S_ISREG(os.fstat(staged.file.fileno()).st_mode):
            raise LinerStorageUnsupportedError("staging handle is not a regular file")
        while True:
            requested = min(_CHUNK_BYTES, max_bytes - total + 1)
            chunk = await reader.read(requested)
            if not chunk:
                break
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise TypeError("upload reader must return bytes")
            total += len(chunk)
            if total > max_bytes:
                raise LinerTooLargeError(f"upload exceeds {max_bytes} bytes")
            staged.file.write(chunk)
        staged.file.flush()
        os.fsync(staged.file.fileno())
        _flush_directory(pinned)
        _publish_staged_file(staged, name, replace=replace)
        published = True
        try:
            _flush_directory(pinned)
        except BaseException:
            logger.warning("Liner published but directory durability flush failed", exc_info=True)
        return pinned.path / name, total
    finally:
        if staged is not None:
            try:
                _cleanup_staged_upload(staged, published=published)
            except BaseException:
                logger.warning("Unable to finish private liner staging cleanup", exc_info=True)
        try:
            _close_root(pinned)
        except BaseException:
            logger.warning("Unable to close pinned liner root", exc_info=True)


def _open_relative_file(root: _PinnedRoot, name: str) -> OpenedLiner:
    if root.windows:
        raw_handle = _nt_create_relative(
            root.handle,
            name,
            directory=False,
            create=False,
            access=_FILE_READ_DATA,
        )
        try:
            fd = msvcrt.open_osfhandle(raw_handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _close_windows_handle(raw_handle)
            raise
    else:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root.handle)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise FileNotFoundError(name)
        file = os.fdopen(fd, "rb")
        return OpenedLiner(file=file, stat_result=metadata)
    except BaseException:
        os.close(fd)
        raise


def open_liner_file(root: Path, name: str) -> OpenedLiner:
    """Open and retain one regular liner file relative to a pinned root."""
    _validate_name(name)
    pinned = _open_pinned_root(Path(root), create=False, write=False, mutate=False)
    try:
        return _open_relative_file(pinned, name)
    finally:
        try:
            _close_root(pinned)
        except BaseException:
            logger.warning("Unable to close pinned liner root after deletion", exc_info=True)


def _delete_relative_file(root: _PinnedRoot, name: str) -> None:
    if root.windows:
        handle = _nt_create_relative(
            root.handle,
            name,
            directory=False,
            create=False,
            access=_DELETE,
        )
        try:
            _delete_opened_file(root, name, handle)
        finally:
            try:
                _close_windows_handle(handle)
            except BaseException:
                logger.warning("Unable to close committed liner delete handle", exc_info=True)
    else:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root.handle)
        try:
            if not stat.S_ISREG(os.fstat(handle).st_mode):
                raise FileNotFoundError(name)
            _delete_opened_file(root, name, handle)
        finally:
            try:
                os.close(handle)
            except BaseException:
                logger.warning("Unable to close liner delete handle", exc_info=True)


def _verify_posix_delete_rollback(quarantine: int) -> None:
    source_name = f"rollback-source-{secrets.token_hex(16)}.probe"
    target_name = f"rollback-target-{secrets.token_hex(16)}.probe"
    source = -1
    target = -1
    try:
        source = os.open(
            source_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=quarantine,
        )
        target = os.open(
            target_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=quarantine,
        )
        source_to_close = source
        source = -1
        os.close(source_to_close)
        target_to_close = target
        target = -1
        os.close(target_to_close)
        try:
            _rename_noreplace_posix(quarantine, source_name, quarantine, target_name)
        except LinerConflictError:
            return
        except LinerStorageUnsupportedError:
            os.unlink(target_name, dir_fd=quarantine)
            try:
                os.link(
                    source_name,
                    target_name,
                    src_dir_fd=quarantine,
                    dst_dir_fd=quarantine,
                    follow_symlinks=False,
                )
            except OSError as exc:
                if exc.errno in {
                    errno.ENOSYS,
                    errno.EINVAL,
                    errno.EOPNOTSUPP,
                    errno.ENOTSUP,
                    errno.EXDEV,
                }:
                    raise LinerStorageUnsupportedError(
                        exc.errno,
                        "atomic delete rollback unavailable",
                    ) from exc
                raise
            return
        raise LinerStorageUnsupportedError(
            "atomic delete rollback probe did not preserve its existing target"
        )
    finally:
        if source >= 0:
            try:
                os.close(source)
            except BaseException:
                logger.warning("Unable to close delete rollback probe", exc_info=True)
        if target >= 0:
            try:
                os.close(target)
            except BaseException:
                logger.warning("Unable to close delete rollback probe", exc_info=True)
        _unlink_posix_entry(
            quarantine, source_name, description="liner delete rollback source probe"
        )
        _unlink_posix_entry(
            quarantine, target_name, description="liner delete rollback target probe"
        )


def _restore_posix_delete(quarantine: int, root: _PinnedRoot, name: str) -> None:
    try:
        _rename_noreplace_posix(quarantine, "victim", root.handle, name)
        return
    except LinerStorageUnsupportedError:
        try:
            os.link(
                "victim",
                name,
                src_dir_fd=quarantine,
                dst_dir_fd=root.handle,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise LinerConflictError(name) from exc
        except OSError as exc:
            if exc.errno in {
                errno.ENOSYS,
                errno.EINVAL,
                errno.EOPNOTSUPP,
                errno.ENOTSUP,
                errno.EXDEV,
            }:
                raise LinerStorageUnsupportedError(
                    exc.errno,
                    "atomic delete rollback unavailable",
                    name,
                ) from exc
            raise
        os.unlink("victim", dir_fd=quarantine)


def _delete_opened_file(root: _PinnedRoot, name: str, handle: int) -> None:
    if root.windows:
        _windows_delete_by_handle(handle)
        return

    opened = os.fstat(handle)
    quarantine_name, quarantine, quarantine_identity = _make_bound_posix_directory(
        root,
        prefix=".liner-delete-",
        description="liner delete quarantine",
    )
    moved = False
    try:
        _verify_posix_delete_rollback(quarantine)
        os.rename(name, "victim", src_dir_fd=root.handle, dst_dir_fd=quarantine)
        moved = True
        moved_metadata = os.stat("victim", dir_fd=quarantine, follow_symlinks=False)
        if _posix_identity(moved_metadata) != _posix_identity(opened):
            raise InvalidLinerName("liner changed during deletion")
        os.unlink("victim", dir_fd=quarantine)
        moved = False
    except BaseException:
        if moved:
            try:
                _restore_posix_delete(quarantine, root, name)
                moved = False
            except BaseException:
                logger.error("Unable to restore liner entry raced during deletion", exc_info=True)
        raise
    finally:
        _close_posix_directory(quarantine, description="liner delete quarantine")
        _remove_bound_posix_directory(
            root.handle,
            quarantine_name,
            quarantine_identity,
            description="liner delete quarantine",
        )


def delete_liner_file(root: Path, name: str) -> None:
    """Delete one regular liner file relative to a pinned root."""
    _validate_name(name)
    pinned = _open_pinned_root(Path(root), create=False, write=False, mutate=True)
    committed = False
    try:
        _delete_relative_file(pinned, name)
        committed = True
        try:
            _flush_directory(pinned)
        except BaseException:
            logger.warning("Liner deleted but directory durability flush failed", exc_info=True)
    finally:
        try:
            _close_root(pinned)
        except BaseException:
            if committed:
                logger.warning("Unable to close root after committed liner deletion", exc_info=True)
            else:
                logger.warning("Unable to close root after failed liner deletion", exc_info=True)


class _BodyLimitExceeded(Exception):
    pass


class LinerUploadBodyLimitMiddleware:
    """Reject oversized liner request bodies before multipart parsing/spooling."""

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        max_file_bytes: Callable[[], int],
        multipart_overhead_bytes: int = DEFAULT_MULTIPART_OVERHEAD_BYTES,
    ) -> None:
        if multipart_overhead_bytes <= 0:
            raise ValueError("multipart_overhead_bytes must be positive")
        self.app = app
        self.max_file_bytes = max_file_bytes
        self.multipart_overhead_bytes = multipart_overhead_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != "/api/liners/upload"
        ):
            await self.app(scope, receive, send)
            return
        max_file_bytes = self.max_file_bytes()
        if (
            isinstance(max_file_bytes, bool)
            or not isinstance(max_file_bytes, int)
            or max_file_bytes <= 0
        ):
            max_file_bytes = 50 * 1024 * 1024
        request_cap = max_file_bytes + self.multipart_overhead_bytes
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
                if declared_size < 0 or declared_size > request_cap:
                    await self._send_too_large(send)
                    return
            except ValueError:
                await self._send_too_large(send)
                return

        consumed = 0

        async def limited_receive() -> dict[str, Any]:
            nonlocal consumed
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                if consumed + len(body) > request_cap:
                    raise _BodyLimitExceeded
                consumed += len(body)
            return message

        response_started = False

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _BodyLimitExceeded:
            if not response_started:
                await self._send_too_large(send)

    @staticmethod
    async def _send_too_large(send: Any) -> None:
        body = b'{"detail":"Request body too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def parse_liner_range(value: str, file_size: int) -> tuple[int, int]:
    """Parse one HTTP bytes range into a half-open interval."""
    if file_size <= 0:
        raise LinerRangeNotSatisfiable(file_size)
    if not value.lower().startswith("bytes=") or "," in value:
        raise MalformedLinerRange(value)
    requested = value.split("=", 1)[1].strip()
    if "-" not in requested:
        raise MalformedLinerRange(value)
    start_text, end_text = (part.strip() for part in requested.split("-", 1))
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                raise MalformedLinerRange(value)
            return max(file_size - suffix, 0), file_size
        start = int(start_text)
        end = min(int(end_text) + 1, file_size) if end_text else file_size
    except ValueError as exc:
        raise MalformedLinerRange(value) from exc
    if start < 0:
        raise MalformedLinerRange(value)
    if start >= file_size:
        raise LinerRangeNotSatisfiable(file_size)
    if end <= start:
        raise MalformedLinerRange(value)
    return start, end


async def iter_opened_liner(
    opened: OpenedLiner, *, start: int = 0, end: int | None = None
) -> AsyncIterator[bytes]:
    """Yield held-file content and close its descriptor on every exit path."""
    try:
        opened.file.seek(start)
        remaining = None if end is None else end - start
        while remaining is None or remaining > 0:
            chunk = await _read_file_chunk(
                opened.file, 64 * 1024 if remaining is None else min(64 * 1024, remaining)
            )
            if not chunk:
                break
            yield chunk
            if remaining is not None:
                remaining -= len(chunk)
    finally:
        opened.file.close()


async def _read_file_chunk(file: BinaryIO, size: int) -> bytes:
    import asyncio

    return await asyncio.to_thread(file.read, size)
