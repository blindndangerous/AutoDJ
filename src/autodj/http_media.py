"""HTTP media range parsing and nonblocking file streaming helpers."""

from __future__ import annotations

import os
import stat
from collections.abc import AsyncGenerator, Generator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from starlette.concurrency import run_in_threadpool


class RangeNotSatisfiable(ValueError):
    """Raised when a Range header cannot identify one satisfiable byte range."""


@dataclass(frozen=True)
class ByteRange:
    """Inclusive byte offsets selected from a representation."""

    start: int
    end: int

    @property
    def length(self) -> int:
        """Return number of selected bytes."""
        return self.end - self.start + 1


@dataclass
class OpenedMediaFile:
    """One opened regular file and metadata captured from that same handle."""

    handle: BinaryIO
    size: int

    def close(self) -> None:
        """Close owned handle; repeated calls are harmless."""
        self.handle.close()


def open_media_file(path: Path) -> OpenedMediaFile:
    """Open *path* once and validate metadata from its owned handle."""
    handle = path.open("rb")
    try:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("media source is not a regular file")
        return OpenedMediaFile(handle=handle, size=metadata.st_size)
    except BaseException:
        handle.close()
        raise


def _parse_decimal(value: str, header: str, maximum: int) -> int:
    if not value or not value.isascii() or not value.isdigit():
        raise RangeNotSatisfiable(header)
    significant = value.lstrip("0") or "0"
    maximum_text = str(maximum)
    if len(significant) > len(maximum_text) or (
        len(significant) == len(maximum_text) and significant > maximum_text
    ):
        return maximum
    return int(significant)


def parse_single_range(header: str, size: int) -> ByteRange:
    """Parse one RFC 9110 byte range against a representation of *size*."""
    if not isinstance(header, str) or size <= 0:
        raise RangeNotSatisfiable(header)
    unit, separator, spec = header.partition("=")
    if not separator or unit.strip().lower() != "bytes":
        raise RangeNotSatisfiable(header)
    if not spec or spec != spec.strip() or any(char.isspace() for char in spec):
        raise RangeNotSatisfiable(header)
    if "," in spec or spec.count("-") != 1:
        raise RangeNotSatisfiable(header)

    start_text, end_text = spec.split("-", 1)
    if not start_text:
        suffix = _parse_decimal(end_text, header, size)
        if suffix <= 0:
            raise RangeNotSatisfiable(header)
        return ByteRange(max(0, size - suffix), size - 1)

    start = _parse_decimal(start_text, header, size)
    if start >= size:
        raise RangeNotSatisfiable(header)
    end = size - 1 if not end_text else _parse_decimal(end_text, header, size - 1)
    if end < start:
        raise RangeNotSatisfiable(header)
    return ByteRange(start, end)


def iter_file_chunks(
    source: OpenedMediaFile,
    byte_range: ByteRange | None = None,
    chunk_size: int = 256 * 1024,
) -> Generator[bytes]:
    """Yield a whole file or inclusive range and close it on iterator close."""
    try:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        handle = source.handle
        remaining = source.size
        if byte_range is not None:
            handle.seek(byte_range.start)
            remaining = byte_range.length
        else:
            handle.seek(0)
        while remaining > 0:
            amount = min(chunk_size, remaining)
            chunk = handle.read(amount)
            if not chunk:
                break
            yield chunk
            remaining -= len(chunk)
    finally:
        source.close()


def _next_chunk(iterator: Iterator[bytes]) -> bytes | None:
    try:
        return next(iterator)
    except StopIteration:
        return None


def _close_owned(iterator: Generator[bytes], source: OpenedMediaFile) -> None:
    try:
        iterator.close()
    finally:
        source.close()


async def stream_file_chunks(
    source: OpenedMediaFile,
    byte_range: ByteRange | None = None,
    chunk_size: int = 256 * 1024,
) -> AsyncGenerator[bytes]:
    """Stream file chunks without running blocking reads on event loop."""
    iterator = iter_file_chunks(source, byte_range, chunk_size)
    try:
        while (chunk := await run_in_threadpool(_next_chunk, iterator)) is not None:
            yield chunk
    finally:
        await run_in_threadpool(_close_owned, iterator, source)
