"""Filesystem helpers shared by index, backup and state code."""

from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

logger = logging.getLogger(__name__)

# Windows refuses to replace, or to open, a file another handle has open
# without FILE_SHARE_DELETE (Python's open() never passes it), or one being
# replaced at that moment.  These waits, about half a second in all, let a
# reader in another thread or process finish first.
_SHARING_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.1, 0.1, 0.1)
# Whether those retries apply; tests change this, never sys.platform.
_IS_WINDOWS = sys.platform == "win32"


def retry_sharing_violation[T](action: Callable[[], T]) -> T:
    """Run *action*, retrying briefly on Windows while the file is held open.

    On other systems a ``PermissionError`` is final and is raised at once.

    Raises:
        OSError: What *action* raised, after the last try on Windows.
    """
    if not _IS_WINDOWS:
        return action()
    for delay in _SHARING_RETRY_DELAYS:
        try:
            return action()
        except PermissionError:
            time.sleep(delay)
    return action()


def read_bytes_shared(path: Path) -> bytes:
    """``path.read_bytes()``, retried briefly on Windows while a save replaces the file."""
    return retry_sharing_violation(path.read_bytes)


def is_absolute_storage(path: str) -> bool:
    """Whether stored *path* is absolute on any platform."""
    return path.startswith(("/", "\\")) or path[1:2] == ":"


def relative_storage_path(path: str | os.PathLike[str], music_dir: Path | None) -> str:
    """Return *path* as a forward-slashed path relative to *music_dir*."""
    parts = Path(os.path.normpath(path)).parts
    if music_dir is not None:
        root = Path(os.path.normpath(music_dir)).parts
        if (
            root
            and len(parts) > len(root)
            and all(
                os.path.normcase(a) == os.path.normcase(b)
                for a, b in zip(parts, root, strict=False)
            )
        ):
            return "/".join(parts[len(root) :])
    stored = Path(path).as_posix()
    if is_absolute_storage(stored):
        raise ValueError(f"{path} is not under music_dir {music_dir}")
    return stored


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

    On Windows the rename is retried for about half a second while another
    handle (a reader) has *path* open, which Windows refuses to replace.

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
        retry_sharing_violation(lambda: os.replace(temporary, path))
    except BaseException:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
    fsync_directory(path.parent)


def confirm_missing(path: Path, error: FileNotFoundError) -> None:
    """Re-raise *error* unless *path* is truly not there.

    ``FileNotFoundError`` also comes from a share that dropped: Windows maps
    ``ERROR_BAD_NETPATH`` (53) and ``ERROR_BAD_NET_NAME`` (67) to ``ENOENT``,
    as it does a missing file.  A file counts as missing only when the
    folder that should hold it is there and can be opened; otherwise the
    error is raised as it was, so a caller never takes "cannot see it" for
    "it is not there" and writes over saved state.

    Raises:
        FileNotFoundError: *error*, when the folder is not there or cannot
            be opened.
    """
    try:
        with os.scandir(path.parent):
            pass
    except OSError:
        raise error from None


def file_signature(path: Path) -> tuple[int, int, int] | None:
    """The file's time, size and ID; ``None`` when it is truly not there.

    The ID too: every save replaces the file, so a new file of the same size
    written within one tick of the clock is still noticed.

    Raises:
        OSError: The file is there but cannot be looked at, or its folder is
            away (see :func:`confirm_missing`).
    """
    try:
        found = path.stat()
    except FileNotFoundError as exc:
        confirm_missing(path, exc)
        return None
    return found.st_mtime_ns, found.st_size, found.st_ino


def set_aside(path: Path, what: str, reason: str) -> bool:
    """Rename the invalid *path* to ``<name>.bad-<random>`` beside it, and log why.

    *what* names the file in the log, such as ``"ads file"``.  Returns
    whether it was moved; a failure is logged with its traceback.
    """
    bad = path.with_name(f"{path.name}.bad-{uuid.uuid4().hex[:8]}")
    try:
        path.replace(bad)
    except OSError:
        logger.exception("Could not set aside the invalid %s %s", what, path)
        return False
    logger.error("%s %s is invalid (%s); set it aside as %s", what.capitalize(), path, reason, bad)
    return True
