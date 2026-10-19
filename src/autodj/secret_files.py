"""Private files under ``<index_dir>`` that LAN pairing keeps.

Holds the paths of the paired-devices database, the machine-token database
and the saved access token,
and the private-file helpers :mod:`autodj.lan` uses for the token and
:mod:`autodj.stereotool_setup` uses for a station's ``secrets.json``.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from autodj.config import AutoDJConfig


def paired_devices_path(cfg: AutoDJConfig) -> Path:
    """Return the paired-devices database path."""
    return Path(cfg.index.index_dir) / ".paired-devices.sqlite3"


def machine_tokens_path(cfg: AutoDJConfig) -> Path:
    """Return the machine-token database path, beside the paired-devices database."""
    return Path(cfg.index.index_dir) / ".machine-tokens.sqlite3"


def access_token_path(cfg: AutoDJConfig) -> Path:
    """Return the saved LAN access token path, beside the paired-devices database."""
    return Path(cfg.index.index_dir) / ".access-token"


if sys.platform == "win32":

    def _windows_user_sid() -> str:
        """Return the current process user's SID for private-file access lists."""
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]

        def fail() -> OSError:
            return ctypes.WinError(ctypes.get_last_error())

        token_query, token_user = 0x0008, 1
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), token_query, token):
            raise fail()
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, token_user, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(
                token, token_user, buffer, size, ctypes.byref(size)
            ):
                raise fail()
            # TOKEN_USER starts with SID_AND_ATTRIBUTES, which starts with the SID pointer.
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            text = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                raise fail()
            try:
                user = text.value
            finally:
                kernel32.LocalFree(text)
        finally:
            kernel32.CloseHandle(token)
        assert user is not None
        return user

    def _owner_only_windows(path: str) -> None:
        """Give *path* a protected DACL granting full control to the current user only.

        ``os.chmod`` on Windows only toggles the read-only flag, so without this
        the file would keep the permissions it inherits from its folder.

        Raises:
            OSError: The current user or the new permissions could not be set.
        """
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
        ]
        advapi32.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        user = _windows_user_sid()
        descriptor = ctypes.c_void_p()
        # D:P protects the DACL from inheritance; FA is full access.
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"D:P(A;;FA;;;{user})", 1, ctypes.byref(descriptor), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            dacl_security_information = 0x00000004
            protected_dacl_security_information = 0x80000000
            if not advapi32.SetFileSecurityW(
                path,
                dacl_security_information | protected_dacl_security_information,
                descriptor,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.LocalFree(descriptor)

    def _check_private_windows_file(fd: int) -> None:
        """Require the opened file's DACL to grant access only to the current user."""
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class ACL(ctypes.Structure):
            _fields_ = [
                ("revision", wintypes.BYTE),
                ("reserved", wintypes.BYTE),
                ("size", wintypes.WORD),
                ("ace_count", wintypes.WORD),
                ("reserved2", wintypes.WORD),
            ]

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        advapi32.GetSecurityInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.GetSecurityInfo.restype = wintypes.DWORD
        advapi32.GetAce.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        user = _windows_user_sid()
        descriptor, dacl = ctypes.c_void_p(), ctypes.c_void_p()
        # SE_FILE_OBJECT and DACL_SECURITY_INFORMATION, on the same handle we read.
        status = advapi32.GetSecurityInfo(
            msvcrt.get_osfhandle(fd),
            1,
            4,
            None,
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if status:
            raise ctypes.WinError(status)
        try:
            if not dacl.value:
                raise PermissionError("secret file requires an owner-only access list")
            acl = ctypes.cast(dacl, ctypes.POINTER(ACL)).contents
            for index in range(acl.ace_count):
                ace = ctypes.c_void_p()
                if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                    raise ctypes.WinError(ctypes.get_last_error())
                assert ace.value is not None
                kind = ctypes.cast(ace, ctypes.POINTER(wintypes.BYTE))[0]
                if kind == 1:  # ACCESS_DENIED_ACE grants no access.
                    continue
                if kind != 0:  # Only ordinary ACCESS_ALLOWED_ACE entries are supported.
                    raise PermissionError("secret file requires an owner-only access list")
                text = wintypes.LPWSTR()
                # ACCESS_ALLOWED_ACE contains a 4-byte header, 4-byte mask, then the SID.
                if not advapi32.ConvertSidToStringSidW(ace.value + 8, ctypes.byref(text)):
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if text.value != user:
                        raise PermissionError("secret file requires an owner-only access list")
                finally:
                    kernel32.LocalFree(text)
        finally:
            kernel32.LocalFree(descriptor)


def _check_private_file(fd: int) -> None:
    """Check the permissions of the opened secret, not a separately resolved path."""
    if sys.platform == "win32":
        _check_private_windows_file(fd)
    elif os.fstat(fd).st_mode & 0o077:
        raise PermissionError("secret file requires owner-only permissions (mode 0600 or 0400)")


def write_private_file(path: Path, text: str | bytes) -> None:
    """Write *text* to *path* atomically, readable only by the owner.

    That is mode 0600 on POSIX systems and, on Windows, a protected access
    list granting only the current user.

    Creates parent directories if needed, writes to a temporary file in the
    same directory first, then renames it over *path* so readers never see a
    partial file.

    Args:
        path: Destination file.
        text: ASCII text or existing file bytes to store.

    Raises:
        OSError: The file could not be written; no temporary file is left.
    """
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.")
        if sys.platform == "win32":
            # Before anything is written, and kept by the rename below.
            _owner_only_windows(tmp)
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("ascii") if isinstance(text, str) else text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        if tmp is not None:
            Path(tmp).unlink(missing_ok=True)
        raise


def read_secret(
    path: Path,
    *,
    valid: Callable[[str], bool],
    error: type[Exception],
    what: str,
    require_private: bool = False,
) -> str | None:
    """Return the secret saved at *path*, or ``None`` if it is missing or not *valid*.

    Raises:
        error: The file exists but cannot be read, or required private permissions fail.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            if require_private:
                _check_private_file(handle.fileno())
            text = handle.read().strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise error(f"cannot read {what} at {path}: {exc}") from exc
    return text if valid(text) else None


def write_secret(path: Path, value: str, *, error: type[Exception], what: str) -> None:
    """Save *value* to *path* with :func:`write_private_file`.

    Raises:
        error: The file cannot be written.
    """
    try:
        write_private_file(path, value + "\n")
    except OSError as exc:
        raise error(f"cannot write {what} at {path}: {exc}") from exc


def load_or_create_secret(
    path: Path, *, valid: Callable[[str], bool], error: type[Exception], what: str
) -> str:
    """Return the secret saved at *path*, creating or replacing it when missing or invalid.

    Raises:
        error: The file cannot be read or written.
    """
    value = read_secret(path, valid=valid, error=error, what=what)
    if value is None:
        value = secrets.token_urlsafe(32)
        write_secret(path, value, error=error, what=what)
    return value


#: Held while a private JSON file is read, changed and written again, so two
#: changes to one station's secrets.json in this process never undo each other.
private_json_lock = threading.RLock()


def read_private_json(path: Path) -> dict[str, Any]:
    """The version 1 JSON object at *path*, without ``version``; empty when there is no file.

    Raises:
        OSError: The file is there but cannot be read.
        ValueError: The file does not hold a version 1 JSON object.  The
            message names the file only, never what it holds.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError:
        raise ValueError(f"{path.name} is not text") from None
    try:
        document = json.loads(text)
    except ValueError:
        raise ValueError(f"{path.name} is not valid JSON") from None
    if not isinstance(document, dict) or document.pop("version", None) != 1:
        raise ValueError(f"{path.name} needs version 1")
    return document


def update_private_json(path: Path, change: Callable[[dict[str, Any]], bool]) -> bool:
    """Read the version 1 JSON object at *path*, let *change* edit it, and write it back.

    *change* returns whether it changed anything; only then is the file
    written, with :func:`write_private_file`, so everything else in it is
    kept.  A file left with nothing but its version is deleted.  The whole
    read, change and write happens under :data:`private_json_lock` in this process.

    Returns:
        Whether the file changed.

    Raises:
        OSError: The file cannot be read, written or deleted.
        ValueError: The file is there but does not hold a version 1 JSON
            object; it is left as it is.
    """
    with private_json_lock:
        document = read_private_json(path)
        if not change(document):
            return False
        if document:
            write_private_file(path, json.dumps({"version": 1, **document}, indent=2) + "\n")
        else:
            path.unlink(missing_ok=True)
        return True
