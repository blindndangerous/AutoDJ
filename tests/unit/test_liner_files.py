from __future__ import annotations

import asyncio
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from autodj.liner_files import (
    InvalidLinerName,
    LinerConflictError,
    LinerTooLargeError,
    resolve_liner_path,
    store_liner_upload,
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


def _process_upload(
    root: str,
    payload: bytes,
    ready: multiprocessing.Queue,
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.Queue,
) -> None:
    ready.put(True)
    start.wait(timeout=10)
    try:
        asyncio.run(
            store_liner_upload(
                Path(root),
                "race.mp3",
                BytesReader(payload),
                max_bytes=100,
                replace=False,
            )
        )
    except LinerConflictError:
        results.put("conflict")
    else:
        results.put("stored")


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
        "CON.mp3",
        "con.MP3",
        "nul.wav",
        "LPT9.flac",
        "COM1.anything.mp3",
        "clip.mp3:stream",
        "clip?.mp3",
        'clip".mp3',
        "clip.mp3.",
        "clip.mp3 ",
    ],
)
def test_resolve_liner_path_rejects_escape_and_device_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(InvalidLinerName):
        resolve_liner_path(tmp_path / "liners", name)


def test_resolve_liner_path_accepts_plain_unicode_filename(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    assert resolve_liner_path(root, "statión-音.mp3") == root.resolve() / "statión-音.mp3"


def test_resolve_liner_path_requires_regular_file(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    root.mkdir()
    (root / "folder.mp3").mkdir()
    with pytest.raises(FileNotFoundError):
        resolve_liner_path(root, "folder.mp3", require_file=True)


def test_resolve_liner_path_rejects_symlink_target(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    root.mkdir()
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    try:
        (root / "clip.mp3").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(InvalidLinerName):
        resolve_liner_path(root, "clip.mp3", require_file=True)


def test_resolve_liner_path_rejects_symlink_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    root = tmp_path / "liners"
    try:
        root.symlink_to(actual, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(InvalidLinerName):
        resolve_liner_path(root, "clip.mp3")


@pytest.mark.asyncio
async def test_upload_reads_only_positive_bounded_chunks(tmp_path: Path) -> None:
    reader = BytesReader(b"x" * (1024 * 1024 + 2))
    _target, size = await store_liner_upload(
        tmp_path / "liners", "clip.mp3", reader, max_bytes=2 * 1024 * 1024, replace=False
    )
    assert size == 1024 * 1024 + 2
    assert reader.sizes
    assert all(0 < size <= 1024 * 1024 for size in reader.sizes)


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
async def test_zero_byte_upload_is_stored(tmp_path: Path) -> None:
    target, size = await store_liner_upload(
        tmp_path / "liners", "silence.wav", BytesReader(b""), max_bytes=1, replace=False
    )
    assert size == 0
    assert target.read_bytes() == b""


@pytest.mark.asyncio
async def test_oversized_upload_removes_only_owned_temporary_file(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    root.mkdir()
    unrelated = root / ".liner-upload-unrelated.tmp"
    unrelated.write_bytes(b"keep")
    with pytest.raises(LinerTooLargeError):
        await store_liner_upload(
            root, "clip.mp3", BytesReader(b"x" * 51), max_bytes=50, replace=False
        )
    assert unrelated.read_bytes() == b"keep"
    assert list(root.glob(".liner-upload-*")) == [unrelated]
    assert not (root / "clip.mp3").exists()


@pytest.mark.asyncio
async def test_base_exception_removes_owned_temporary_file(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    with pytest.raises(UploadAborted):
        await store_liner_upload(root, "clip.mp3", AbortingReader(), max_bytes=50, replace=False)
    assert list(root.glob(".liner-upload-*")) == []
    assert not (root / "clip.mp3").exists()


@pytest.mark.asyncio
async def test_conflict_requires_explicit_atomic_replace(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    root.mkdir()
    target = root / "clip.mp3"
    target.write_bytes(b"old")
    with pytest.raises(LinerConflictError):
        await store_liner_upload(
            root, target.name, BytesReader(b"new"), max_bytes=50, replace=False
        )
    assert target.read_bytes() == b"old"
    await store_liner_upload(root, target.name, BytesReader(b"new"), max_bytes=50, replace=True)
    assert target.read_bytes() == b"new"


@pytest.mark.asyncio
async def test_failed_replace_preserves_existing_target_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    root.mkdir()
    target = root / "clip.mp3"
    target.write_bytes(b"old")

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("promotion failed")

    monkeypatch.setattr(liner_files.os, "replace", fail_replace)
    with pytest.raises(OSError, match="promotion failed"):
        await store_liner_upload(root, target.name, BytesReader(b"new"), max_bytes=50, replace=True)
    assert target.read_bytes() == b"old"
    assert list(root.glob(".liner-upload-*")) == []


@pytest.mark.asyncio
async def test_cleanup_never_deletes_replacement_at_temporary_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj import liner_files

    root = tmp_path / "liners"
    captured_temporary: list[Path] = []

    def replace_temp_with_unowned_file(source: Path, destination: Path) -> None:
        temporary = Path(source)
        temporary.unlink()
        temporary.write_bytes(b"not-owned")
        captured_temporary.append(temporary)
        raise UploadAborted

    monkeypatch.setattr(liner_files.os, "replace", replace_temp_with_unowned_file)
    with pytest.raises(UploadAborted):
        await store_liner_upload(root, "clip.mp3", BytesReader(b"new"), max_bytes=50, replace=True)
    assert captured_temporary[0].read_bytes() == b"not-owned"
    assert not (root / "clip.mp3").exists()


def test_non_replace_upload_is_atomic_across_processes(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    root.mkdir()
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_process_upload,
            args=(str(root), payload, ready, start, results),
        )
        for payload in (b"first", b"second")
    ]
    for process in processes:
        process.start()
    assert ready.get(timeout=10) is True
    assert ready.get(timeout=10) is True
    start.set()
    outcomes = sorted(results.get(timeout=10) for _ in processes)
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert outcomes == ["conflict", "stored"]
    assert (root / "race.mp3").read_bytes() in {b"first", b"second"}
    assert list(root.glob(".liner-upload-*")) == []


def test_threaded_non_replace_upload_has_one_winner(tmp_path: Path) -> None:
    root = tmp_path / "liners"

    def upload(payload: bytes) -> str:
        try:
            asyncio.run(
                store_liner_upload(
                    root,
                    "thread-race.mp3",
                    BytesReader(payload),
                    max_bytes=50,
                    replace=False,
                )
            )
        except LinerConflictError:
            return "conflict"
        return "stored"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(upload, (b"one", b"two")))
    assert outcomes == ["conflict", "stored"]
    assert (root / "thread-race.mp3").read_bytes() in {b"one", b"two"}
