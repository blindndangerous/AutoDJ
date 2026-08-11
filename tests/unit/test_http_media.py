from __future__ import annotations

import asyncio
import threading
import time
from io import BytesIO
from pathlib import Path

import pytest

from autodj.http_media import (
    ByteRange,
    OpenedMediaFile,
    RangeNotSatisfiable,
    iter_file_chunks,
    open_media_file,
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
        "bytes=١-5",
        "bytes=1-٥",
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


@pytest.mark.parametrize(
    "header",
    [
        f"bytes=0-{'9' * 10000}",
        f"bytes=-{'9' * 10000}",
    ],
)
def test_huge_valid_end_or_suffix_saturates_to_representation(header: str) -> None:
    assert parse_single_range(header, 1000) == ByteRange(0, 999)


def test_partial_consumer_close_closes_file_handle(monkeypatch) -> None:
    handle = BytesIO(bytes(range(100)))
    stream = iter_file_chunks(
        OpenedMediaFile(handle, 100),
        ByteRange(10, 29),
        chunk_size=10,
    )

    assert next(stream) == bytes(range(10, 20))
    stream.close()

    assert handle.closed is True


@pytest.mark.parametrize("chunk_size", [0, -1])
def test_iter_file_chunks_rejects_nonpositive_chunk_size(chunk_size: int) -> None:
    handle = BytesIO(b"abc")
    stream = iter_file_chunks(OpenedMediaFile(handle, 3), chunk_size=chunk_size)

    with pytest.raises(ValueError, match="chunk_size"):
        next(stream)

    assert handle.closed is True


def test_truncated_file_stops_without_spinning(monkeypatch) -> None:
    handle = BytesIO(b"abc")

    chunks = list(
        iter_file_chunks(
            OpenedMediaFile(handle, 10),
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

    chunks = [chunk async for chunk in stream_file_chunks(OpenedMediaFile(handle, 6), chunk_size=3)]

    assert chunks == [b"abc", b"def"]
    assert calls == ["_next_chunk", "_next_chunk", "_next_chunk", "_close_owned"]
    assert handle.closed is True


async def test_stream_file_chunks_closes_generator_when_consumer_closes(monkeypatch) -> None:
    handle = BytesIO(b"abcdef")
    stream = stream_file_chunks(OpenedMediaFile(handle, 6), chunk_size=3)

    assert await anext(stream) == b"abc"
    await stream.aclose()

    assert handle.closed is True


async def test_stream_file_chunks_runs_blocking_close_off_event_loop(monkeypatch) -> None:
    close_started = threading.Event()
    release_close = threading.Event()
    close_count = 0

    def blocking_chunks(*_args, **_kwargs):
        nonlocal close_count
        try:
            yield b"chunk"
            yield b"unused"
        finally:
            close_count += 1
            close_started.set()
            release_close.wait(2.0)

    monkeypatch.setattr("autodj.http_media.iter_file_chunks", blocking_chunks)
    stream = stream_file_chunks(OpenedMediaFile(BytesIO(b"chunkunused"), 11))
    assert await anext(stream) == b"chunk"
    watchdog = threading.Timer(0.5, release_close.set)
    watchdog.start()
    started = time.monotonic()

    close_task = asyncio.create_task(stream.aclose())
    try:
        await asyncio.sleep(0.05)
        assert time.monotonic() - started < 0.2
        assert await asyncio.to_thread(close_started.wait, 0.2) is True
    finally:
        release_close.set()
        await close_task
        watchdog.cancel()

    await stream.aclose()
    assert close_count == 1


def test_open_media_file_uses_fstat_size_and_rejects_nonregular(tmp_path, monkeypatch) -> None:
    import os
    import stat
    from types import SimpleNamespace

    path = tmp_path / "audio.flac"
    path.write_bytes(b"abcd")
    opened = open_media_file(path)
    try:
        assert opened.size == 4
        assert opened.handle.read() == b"abcd"
    finally:
        opened.close()

    handle = BytesIO(b"directory-like")
    handle.fileno = lambda: 42  # type: ignore[attr-defined]
    monkeypatch.setattr(Path, "open", lambda _self, _mode: handle)
    monkeypatch.setattr(
        os,
        "fstat",
        lambda _fd: SimpleNamespace(st_mode=stat.S_IFDIR, st_size=14),
    )

    with pytest.raises(OSError, match="regular"):
        open_media_file(Path("not-regular"))

    assert handle.closed is True
