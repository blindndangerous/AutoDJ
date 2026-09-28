from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from autodj.liner_files import (
    MAX_LINER_NAME_BYTES,
    InvalidLinerName,
    LinerConflictError,
    LinerTooLargeError,
    delete_liner_file,
    open_liner_file,
    store_liner_upload,
    validate_liner_name,
)


class BytesReader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.sizes.append(size)
        payload, self._payload = self._payload[:size], self._payload[size:]
        return payload


class UploadAborted(BaseException):
    pass


class AbortingReader:
    def __init__(self) -> None:
        self._reads = 0

    async def read(self, size: int = -1) -> bytes:
        self._reads += 1
        if self._reads == 1:
            return b"partial"
        raise UploadAborted


def _leftovers(root: Path) -> list[Path]:
    return list(root.glob("*.part"))


@pytest.mark.parametrize(
    "name",
    [
        "",
        "bad\x00.mp3",
        "bad\n.mp3",
        ".",
        "..",
        "../clip.mp3",
        r"..\liners-backup\clip.mp3",
        "sub/clip.mp3",
        r"sub\clip.mp3",
        "C:clip.mp3",
        "CON.mp3",
        "CONIN$",
        "conout$.wav",
        "con.MP3",
        "nul.wav",
        "LPT9.flac",
        "COM1.anything.mp3",
        "COM¹.mp3",
        "com².WAV",
        "LPT¹.mp3",
        "clip.mp3:stream",
        "clip?.mp3",
        'clip".mp3',
        "clip.mp3.",
        "clip.mp3 ",
        "config.toml",
        "tracks.db",
        "README",
        "clip.mp3.bak",
        "x" * MAX_LINER_NAME_BYTES + ".mp3",
    ],
)
def test_liner_name_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(InvalidLinerName):
        validate_liner_name(name)


@pytest.mark.parametrize("name", ["statión-音.mp3", "ID.WAV", "x" * 196 + ".mp3"])
def test_liner_name_accepts_plain_audio_filenames(name: str) -> None:
    validate_liner_name(name)


@pytest.mark.parametrize("name", ["config.toml", "../clip.mp3"])
def test_open_and_delete_refuse_invalid_names(tmp_path: Path, name: str) -> None:
    root = tmp_path / "liners"
    root.mkdir()
    (tmp_path / "clip.mp3").write_bytes(b"outside")
    (root / "config.toml").write_bytes(b"secret")
    with pytest.raises(InvalidLinerName):
        open_liner_file(root, name)
    with pytest.raises(InvalidLinerName):
        delete_liner_file(root, name)
    assert (root / "config.toml").read_bytes() == b"secret"
    assert (tmp_path / "clip.mp3").read_bytes() == b"outside"


def test_open_reads_and_delete_removes(tmp_path: Path) -> None:
    (tmp_path / "ID.WAV").write_bytes(b"riff")
    opened = open_liner_file(tmp_path, "ID.WAV")
    try:
        assert opened.file.read() == b"riff"
        assert opened.stat_result.st_size == 4
    finally:
        opened.file.close()
    delete_liner_file(tmp_path, "ID.WAV")
    assert not (tmp_path / "ID.WAV").exists()
    with pytest.raises(FileNotFoundError):
        delete_liner_file(tmp_path, "ID.WAV")


def test_open_refuses_a_directory_with_an_audio_name(tmp_path: Path) -> None:
    (tmp_path / "folder.mp3").mkdir()
    with pytest.raises(OSError):
        open_liner_file(tmp_path, "folder.mp3")


@pytest.mark.asyncio
async def test_upload_reads_bounded_chunks_and_creates_root(tmp_path: Path) -> None:
    root = tmp_path / "missing-parent" / "liners"
    reader = BytesReader(b"x" * (1024 * 1024 + 2))
    target, size = await store_liner_upload(
        root, "clip.mp3", reader, max_bytes=2 * 1024 * 1024, replace=False
    )
    assert target == root / "clip.mp3"
    assert size == 1024 * 1024 + 2
    assert target.stat().st_size == size
    assert all(0 < requested <= 1024 * 1024 for requested in reader.sizes)
    assert _leftovers(root) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("max_bytes", [0, -1])
async def test_upload_rejects_non_positive_limit(tmp_path: Path, max_bytes: int) -> None:
    root = tmp_path / "liners"
    with pytest.raises(ValueError, match="positive"):
        await store_liner_upload(
            root, "clip.mp3", BytesReader(b""), max_bytes=max_bytes, replace=False
        )
    assert not root.exists()


@pytest.mark.asyncio
async def test_oversized_upload_is_refused_and_cleaned_up(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    root.mkdir()
    unrelated = root / ".other.mp3.0000.part"
    unrelated.write_bytes(b"keep")
    with pytest.raises(LinerTooLargeError):
        await store_liner_upload(
            root, "clip.mp3", BytesReader(b"x" * 51), max_bytes=50, replace=False
        )
    assert _leftovers(root) == [unrelated]
    assert not (root / "clip.mp3").exists()


@pytest.mark.asyncio
async def test_aborted_upload_removes_part_file(tmp_path: Path) -> None:
    with pytest.raises(UploadAborted):
        await store_liner_upload(
            tmp_path, "clip.mp3", AbortingReader(), max_bytes=50, replace=False
        )
    assert _leftovers(tmp_path) == []
    assert not (tmp_path / "clip.mp3").exists()


@pytest.mark.asyncio
async def test_upload_rejects_nonbytes_reader_result(tmp_path: Path) -> None:
    class TextReader:
        async def read(self, size: int = -1) -> Any:
            return "not bytes"

    with pytest.raises(TypeError):
        await store_liner_upload(tmp_path, "clip.mp3", TextReader(), max_bytes=50, replace=False)
    assert _leftovers(tmp_path) == []


@pytest.mark.asyncio
async def test_existing_liner_needs_explicit_replace(tmp_path: Path) -> None:
    target = tmp_path / "clip.mp3"
    target.write_bytes(b"old")
    with pytest.raises(LinerConflictError):
        await store_liner_upload(
            tmp_path, target.name, BytesReader(b"new"), max_bytes=50, replace=False
        )
    assert target.read_bytes() == b"old"
    await store_liner_upload(tmp_path, target.name, BytesReader(b"new"), max_bytes=50, replace=True)
    assert target.read_bytes() == b"new"
    assert _leftovers(tmp_path) == []


@pytest.mark.asyncio
async def test_failed_replace_keeps_existing_liner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "clip.mp3"
    target.write_bytes(b"old")

    def fail_replace(*_args: Any) -> None:
        raise OSError("promotion failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="promotion failed"):
        await store_liner_upload(
            tmp_path, target.name, BytesReader(b"new"), max_bytes=50, replace=True
        )
    assert target.read_bytes() == b"old"
    assert _leftovers(tmp_path) == []
