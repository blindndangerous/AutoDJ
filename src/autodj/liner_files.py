"""Safe path and atomic storage boundary for uploaded liner clips."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Protocol


class InvalidLinerName(ValueError):
    """Raised when a liner name is not one contained plain filename."""


class LinerConflictError(FileExistsError):
    """Raised when a non-replacing upload targets an existing liner."""


class LinerTooLargeError(ValueError):
    """Raised when a streamed upload crosses its configured byte limit."""


class AsyncReader(Protocol):
    async def read(self, size: int = -1) -> bytes:
        """Read at most *size* bytes."""


_CHUNK_BYTES = 1024 * 1024
_WINDOWS_FORBIDDEN = frozenset('<>"|?*')
_WINDOWS_DEVICES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
    )


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
    if device_stem in _WINDOWS_DEVICES:
        raise InvalidLinerName("reserved device filename")


def resolve_liner_path(root: Path, name: str, *, require_file: bool = False) -> Path:
    """Resolve one plain liner filename without following reparse points."""
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
    if require_file and not target.is_file():
        raise FileNotFoundError(name)
    return target


def _path_identity(path: Path) -> tuple[int, int]:
    metadata = path.stat(follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino


def _unlink_owned_temporary(path: Path, identity: tuple[int, int]) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISREG(metadata.st_mode)
        and not _is_reparse_point(path)
        and (metadata.st_dev, metadata.st_ino) == identity
    ):
        path.unlink()


def _assert_stable_root(
    configured_root: Path,
    resolved_root: Path,
    identity: tuple[int, int],
) -> None:
    if _is_reparse_point(configured_root):
        raise InvalidLinerName("configured liner root became a reparse point")
    try:
        still_resolved = configured_root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise InvalidLinerName("configured liner root changed during upload") from exc
    if still_resolved != resolved_root or _path_identity(resolved_root) != identity:
        raise InvalidLinerName("configured liner root changed during upload")


def _fsync_directory(root: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(root, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


async def store_liner_upload(
    root: Path,
    name: str,
    reader: AsyncReader,
    *,
    max_bytes: int,
    replace: bool,
) -> tuple[Path, int]:
    """Stream, durably flush, and atomically publish one liner upload."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")

    configured_root = Path(root)
    resolve_liner_path(configured_root, name)
    configured_root.mkdir(parents=True, exist_ok=True)
    target = resolve_liner_path(configured_root, name)
    resolved_root = configured_root.resolve(strict=True)
    identity = _path_identity(resolved_root)

    fd, temporary_name = tempfile.mkstemp(prefix=".liner-upload-", suffix=".tmp", dir=resolved_root)
    temporary = Path(temporary_name)
    temporary_identity = _path_identity(temporary)
    total = 0
    try:
        with os.fdopen(fd, "wb") as handle:
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
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())

        _assert_stable_root(configured_root, resolved_root, identity)
        checked_target = resolve_liner_path(configured_root, name)
        if checked_target != target:
            raise InvalidLinerName("liner target changed during upload")
        if replace:
            os.replace(temporary, target)
        else:
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise LinerConflictError(name) from exc
            _unlink_owned_temporary(temporary, temporary_identity)
        _fsync_directory(resolved_root)
        return target, total
    finally:
        _unlink_owned_temporary(temporary, temporary_identity)
