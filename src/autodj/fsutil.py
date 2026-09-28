"""Durable file replacement shared by the index, backup and state writers."""

from __future__ import annotations

import os
import sys
import uuid
from contextlib import suppress
from pathlib import Path


def fsync_directory(path: Path) -> None:
    """Flush a directory's entries to disk.

    A no-op on Windows, which cannot open a directory for fsync.

    Raises:
        OSError: If the directory cannot be opened or flushed.
    """
    if sys.platform == "win32":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, data: str | bytes, *, mode: int = 0o666) -> None:
    """Replace *path* with *data* so readers see the old file or the new one, never a mix.

    The data goes to a flushed temporary file named ``.<name>.<32 hex>.tmp``
    beside *path*, which is then renamed over it, and the directory is
    flushed.  Text is written as UTF-8 with the newlines it contains.

    Args:
        path: File to replace.
        data: New contents.
        mode: Permission bits for a newly created file, e.g. ``0o600``;
            the umask still applies.  Windows only honours the read-only bit.

    Raises:
        OSError: If writing, renaming or flushing fails.  The temporary file
            is removed.
    """
    payload = data.encode("utf-8") if isinstance(data, str) else data
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        with os.fdopen(os.open(temporary, flags, mode), "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
    fsync_directory(path.parent)
