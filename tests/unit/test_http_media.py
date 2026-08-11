from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

import pytest

from autodj.http_media import (
    ByteRange,
    RangeNotSatisfiable,
    iter_file_chunks,
    parse_single_range,
    stream_file_chunks,
)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("bytes=0-99", (0, 99)),
        ("BYTES=0-99", (0, 99)),
        ("bytes=100-", (100, 999)),
        ("bytes=-100", (900, 999)),
        ("bytes=-2000", (0, 999)),
        ("bytes=900-2000", (900, 999)),
    ],
)
def test_parse_single_range_forms(header: str, expected: tuple[int, int]) -> None:
    assert parse_single_range(header, 1000) == ByteRange(*expected)


@pytest.mark.parametrize(
    "header",
    [
        "bytes=",
        "bytes=10-5",
        "bytes=1000-",
        "bytes=0-1,4-5",
        "kilobytes=0-5",
        "bytes=+1-5",
        "bytes=1-+5",
        "bytes= 1-5",
        "bytes=1 -5",
        "bytes=1- 5",
        "bytes=-0",
    ],
)
def test_invalid_or_unsupported_ranges_are_unsatisfiable(header: str) -> None:
    with pytest.raises(RangeNotSatisfiable):
        parse_single_range(header, 1000)


def test_ranges_are_unsatisfiable_for_an_empty_representation() -> None:
    with pytest.raises(RangeNotSatisfiable):
        parse_single_range("bytes=0-", 0)


def test_excessively_large_integer_is_unsatisfiable() -> None:
    with pytest.raises(RangeNotSatisfiable):
        parse_single_range(f"bytes={'9' * 10000}-", 1000)


def test_partial_consumer_close_closes_file_handle(monkeypatch) -> None:
    handle = BytesIO(bytes(range(100)))
    monkeypatch.setattr(Path, "open", lambda _self, _mode: handle)
    stream = iter_file_chunks(
        Path("track.bin"),
        ByteRange(10, 29),
        chunk_size=10,
    )

    assert next(stream) == bytes(range(10, 20))
    stream.close()

    assert handle.closed is True


@pytest.mark.parametrize("chunk_size", [0, -1])
def test_iter_file_chunks_rejects_nonpositive_chunk_size(chunk_size: int) -> None:
    stream = iter_file_chunks(Path("track.bin"), chunk_size=chunk_size)

    with pytest.raises(ValueError, match="chunk_size"):
        next(stream)


def test_truncated_file_stops_without_spinning(monkeypatch) -> None:
    handle = BytesIO(b"abc")
    monkeypatch.setattr(Path, "open", lambda _self, _mode: handle)

    chunks = list(
        iter_file_chunks(
            Path("track.bin"),
            ByteRange(0, 9),
            chunk_size=4,
        )
    )

    assert chunks == [b"abc"]
    assert handle.closed is True


async def test_stream_file_chunks_runs_each_next_in_threadpool(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_threadpool(function, *args):
        calls.append(function.__name__)
        await asyncio.sleep(0)
        return function(*args)

    monkeypatch.setattr("autodj.http_media.run_in_threadpool", fake_threadpool)
    handle = BytesIO(b"abcdef")
    monkeypatch.setattr(Path, "open", lambda _self, _mode: handle)

    chunks = [chunk async for chunk in stream_file_chunks(Path("track.bin"), chunk_size=3)]

    assert chunks == [b"abc", b"def"]
    assert calls == ["_next_chunk", "_next_chunk", "_next_chunk"]
    assert handle.closed is True


async def test_stream_file_chunks_closes_generator_when_consumer_closes(monkeypatch) -> None:
    handle = BytesIO(b"abcdef")
    monkeypatch.setattr(Path, "open", lambda _self, _mode: handle)
    stream = stream_file_chunks(Path("track.bin"), chunk_size=3)

    assert await anext(stream) == b"abc"
    await stream.aclose()

    assert handle.closed is True
