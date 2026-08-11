"""HTTP media range parsing and nonblocking file streaming helpers."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator, Iterator
from dataclasses import dataclass
from pathlib import Path

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


def _parse_decimal(value: str, header: str) -> int:
    if not value or not value.isascii() or not value.isdigit():
        raise RangeNotSatisfiable(header)
    try:
        return int(value)
    except ValueError as exc:
        # Python limits decimal conversion length.  Oversized HTTP integers
        # remain an unsatisfiable range rather than leaking that detail.
        raise RangeNotSatisfiable(header) from exc


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
        suffix = _parse_decimal(end_text, header)
        if suffix <= 0:
            raise RangeNotSatisfiable(header)
        return ByteRange(max(0, size - suffix), size - 1)

    start = _parse_decimal(start_text, header)
    if start >= size:
        raise RangeNotSatisfiable(header)
    end = size - 1 if not end_text else min(_parse_decimal(end_text, header), size - 1)
    if end < start:
        raise RangeNotSatisfiable(header)
    return ByteRange(start, end)


def iter_file_chunks(
    path: Path,
    byte_range: ByteRange | None = None,
    chunk_size: int = 256 * 1024,
) -> Generator[bytes]:
    """Yield a whole file or inclusive range and close it on iterator close."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    with path.open("rb") as handle:
        remaining: int | None = None
        if byte_range is not None:
            handle.seek(byte_range.start)
            remaining = byte_range.length
        while remaining is None or remaining > 0:
            amount = chunk_size if remaining is None else min(chunk_size, remaining)
            chunk = handle.read(amount)
            if not chunk:
                break
            yield chunk
            if remaining is not None:
                remaining -= len(chunk)


def _next_chunk(iterator: Iterator[bytes]) -> bytes | None:
    try:
        return next(iterator)
    except StopIteration:
        return None


async def stream_file_chunks(
    path: Path,
    byte_range: ByteRange | None = None,
    chunk_size: int = 256 * 1024,
) -> AsyncGenerator[bytes]:
    """Stream file chunks without running blocking reads on event loop."""
    iterator = iter_file_chunks(path, byte_range, chunk_size)
    try:
        while (chunk := await run_in_threadpool(_next_chunk, iterator)) is not None:
            yield chunk
    finally:
        iterator.close()
