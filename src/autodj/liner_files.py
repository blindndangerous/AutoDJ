"""Liner clip file access for the HTTP routes: name checks, upload, read, delete.

Every name that arrives over HTTP must be one plain filename with a liner
audio extension, so a request can never reach outside the liner folder or
touch non-audio files that share it.  Local processes that can already
write to the liner folder are out of scope (see ``SECURITY.md``), so the
file operations themselves are plain ``open``/``os.replace``/``unlink``.
"""

from __future__ import annotations

import contextlib
import ntpath
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from autodj.liners import LINER_EXTS


class InvalidLinerName(ValueError):
    """Raised when a liner name is not one plain audio filename."""


class LinerConflictError(FileExistsError):
    """Raised when a non-replacing upload targets an existing liner."""


class LinerTooLargeError(ValueError):
    """Raised when a streamed upload crosses its configured byte limit."""


class AsyncReader(Protocol):
    """Describe the asynchronous byte reader accepted for liner uploads."""

    async def read(self, size: int = -1) -> bytes:
        """Read at most *size* bytes."""
        ...


@dataclass
class OpenedLiner:
    """An open regular liner file and its metadata."""

    file: BinaryIO
    stat_result: os.stat_result


_CHUNK_BYTES = 1024 * 1024
# Leaves room for the ".<name>.<16 hex>.part" upload file inside the
# common 255-byte filename limit.
MAX_LINER_NAME_BYTES = 200
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


def validate_liner_name(name: str) -> None:
    """Reject anything but one plain filename with a liner audio extension.

    Refuses path separators, ``.``/``..``, drive or stream colons, control
    and Windows-forbidden characters, trailing dots or spaces, Windows
    device names, names over :data:`MAX_LINER_NAME_BYTES` UTF-8 bytes, and
    extensions outside :data:`~autodj.liners.LINER_EXTS`.
    """
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ":" in name
        or any(character in _WINDOWS_FORBIDDEN or ord(character) < 32 for character in name)
        or name != name.rstrip(" .")
    ):
        raise InvalidLinerName("liner name must be one plain filename")
    if len(name.encode("utf-8")) > MAX_LINER_NAME_BYTES:
        raise InvalidLinerName(f"liner name must be at most {MAX_LINER_NAME_BYTES} bytes")
    device_stem = name.split(".", 1)[0].rstrip(" .").upper()
    is_reserved = getattr(ntpath, "isreserved", lambda _name: False)
    if device_stem in _WINDOWS_DEVICES or is_reserved(name):
        raise InvalidLinerName("reserved device filename")
    if os.path.splitext(name)[1].lower() not in LINER_EXTS:
        raise InvalidLinerName(
            f"liner name must use a supported audio extension: {', '.join(LINER_EXTS)}"
        )


async def store_liner_upload(
    root: Path,
    name: str,
    reader: AsyncReader,
    *,
    max_bytes: int,
    replace: bool,
) -> tuple[Path, int]:
    """Stream an upload to a hidden ``.part`` file, then move it into place.

    Refuses to overwrite an existing liner unless *replace* is true.  The
    ``.part`` file is removed on any failure.

    Returns:
        The final path and the number of bytes written.
    """
    validate_liner_name(name)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    root = Path(root)
    target = root / name
    if not replace and target.exists():
        raise LinerConflictError(name)
    root.mkdir(parents=True, exist_ok=True)
    part = root / f".{name}.{secrets.token_hex(8)}.part"
    total = 0
    try:
        with open(part, "xb") as out:
            while True:
                chunk = await reader.read(min(_CHUNK_BYTES, max_bytes - total + 1))
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise TypeError("upload reader must return bytes")
                total += len(chunk)
                if total > max_bytes:
                    raise LinerTooLargeError(f"upload exceeds {max_bytes} bytes")
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if not replace and target.exists():
            raise LinerConflictError(name)
        os.replace(part, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            part.unlink()
        raise
    return target, total


def open_liner_file(root: Path, name: str) -> OpenedLiner:
    """Open one regular liner file for reading.

    Raises:
        InvalidLinerName: *name* fails :func:`validate_liner_name`.
        FileNotFoundError: No regular file by that name.
    """
    validate_liner_name(name)
    file = open(Path(root) / name, "rb")  # noqa: SIM115 - the caller closes it
    try:
        metadata = os.fstat(file.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise FileNotFoundError(name)
    except BaseException:
        file.close()
        raise
    return OpenedLiner(file=file, stat_result=metadata)


def delete_liner_file(root: Path, name: str) -> None:
    """Delete one liner file after checking its name."""
    validate_liner_name(name)
    (Path(root) / name).unlink()


__all__ = [
    "MAX_LINER_NAME_BYTES",
    "AsyncReader",
    "InvalidLinerName",
    "LinerConflictError",
    "LinerTooLargeError",
    "OpenedLiner",
    "delete_liner_file",
    "open_liner_file",
    "store_liner_upload",
    "validate_liner_name",
]
