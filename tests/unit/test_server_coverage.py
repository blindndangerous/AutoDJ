from __future__ import annotations

import asyncio
import io
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from autodj.http_media import OpenedMediaFile
from autodj.index_manifest import IndexSnapshotToken
from autodj.server import (
    _close_alac_stream,
    _close_failed_websocket,
    _close_websocket_client,
    _is_alac,
    _kill_and_wait,
    _prefetch_alac_output,
    _terminate_alac_process,
    _transcode_alac_to_mp3,
    _websocket_session_is_valid,
    _WebSocketClient,
    reload_published_generation_once,
)


@pytest.mark.asyncio
async def test_websocket_close_is_idempotent() -> None:
    websocket = MagicMock()
    websocket.close = AsyncMock()
    client = _WebSocketClient(websocket=websocket, close_started=True)

    assert await _close_websocket_client(
        client,
        code=1000,
        timeout_seconds=1,
        failure_action="close",
    )
    websocket.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_websocket_close_failure_without_request_id_returns_false() -> None:
    websocket = MagicMock()
    websocket.close = AsyncMock(side_effect=OSError("closed"))
    client = _WebSocketClient(websocket=websocket)

    assert not await _close_websocket_client(
        client,
        code=1013,
        timeout_seconds=1,
        failure_action="close",
    )


@pytest.mark.asyncio
async def test_failed_websocket_close_cancels_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autodj.server as server

    handler = MagicMock()
    handler.done.return_value = False
    client = _WebSocketClient(websocket=MagicMock(), handler_task=handler)
    monkeypatch.setattr(server, "_close_websocket_client", AsyncMock(return_value=False))

    await _close_failed_websocket(client, 1)

    handler.cancel.assert_called_once()


def test_websocket_session_validation_defaults_true_and_contains_callback_failure() -> None:
    assert _websocket_session_is_valid(_WebSocketClient(websocket=MagicMock()))
    client = _WebSocketClient(
        websocket=MagicMock(),
        session_is_valid=MagicMock(side_effect=RuntimeError("session backend")),
    )
    assert not _websocket_session_is_valid(client)


@pytest.mark.asyncio
async def test_reload_generation_without_config_preserves_observed() -> None:
    observed = IndexSnapshotToken(1, 1)
    bridge = MagicMock()
    bridge.player = object()

    assert await reload_published_generation_once(bridge, observed) == observed


@pytest.mark.asyncio
async def test_reload_generation_skips_unchanged_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autodj.server as server

    observed = IndexSnapshotToken(1, 1)
    bridge = MagicMock()
    bridge.player._cfg.index.active_dir = "index"
    monkeypatch.setattr(server, "current_snapshot_token", MagicMock(return_value=observed))

    assert await reload_published_generation_once(bridge, observed) == observed
    bridge.reload_index_from_disk.assert_not_called()


def _mutagen_modules(mp4_factory: object) -> dict[str, ModuleType]:
    class FakeMutagenError(Exception):
        pass

    mutagen = ModuleType("mutagen")
    mutagen.MutagenError = FakeMutagenError
    mp4 = ModuleType("mutagen.mp4")
    mp4.MP4 = mp4_factory
    return {"mutagen": mutagen, "mutagen.mp4": mp4}


def test_alac_detection_reads_codec_and_rewinds_source() -> None:
    handle = io.BytesIO(b"audio")
    source = OpenedMediaFile(handle=handle, size=5)
    factory = MagicMock(return_value=SimpleNamespace(info=SimpleNamespace(codec="ALAC")))

    with patch.dict(sys.modules, _mutagen_modules(factory)):
        assert _is_alac(source, ".m4a")

    assert handle.tell() == 0


def test_alac_detection_contains_parser_error_and_rewinds_source() -> None:
    handle = io.BytesIO(b"audio")
    source = OpenedMediaFile(handle=handle, size=5)
    factory = MagicMock(side_effect=ValueError("bad mp4"))

    with patch.dict(sys.modules, _mutagen_modules(factory)):
        assert not _is_alac(source, ".mp4")

    assert handle.tell() == 0


def test_alac_detection_contains_incomplete_mutagen_module() -> None:
    handle = io.BytesIO(b"audio")
    source = OpenedMediaFile(handle=handle, size=5)
    modules = _mutagen_modules(MagicMock())
    del modules["mutagen"].MutagenError

    with patch.dict(sys.modules, modules):
        assert not _is_alac(source, ".m4a")


@pytest.mark.asyncio
async def test_transcoder_without_stdout_is_reaped_without_output() -> None:
    process = MagicMock(stdout=None)
    process._autodj_cleanup_task = None
    process.kill = MagicMock()
    process.wait = AsyncMock(return_value=0)

    chunks = [chunk async for chunk in _transcode_alac_to_mp3(process)]

    assert chunks == []
    process.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_transcoder_yields_prefetch_and_stream_chunks() -> None:
    process = MagicMock()
    process._autodj_cleanup_task = None
    process.stdout.read = AsyncMock(side_effect=[b"second", b""])
    process.kill = MagicMock()
    process.wait = AsyncMock(return_value=0)

    chunks = [chunk async for chunk in _transcode_alac_to_mp3(process, b"first")]

    assert chunks == [b"first", b"second"]
    process.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_kill_and_wait_ignores_already_exited_process() -> None:
    process = MagicMock()
    process.kill.side_effect = ProcessLookupError
    process.wait = AsyncMock(return_value=0)

    await _kill_and_wait(process)

    process.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminate_alac_process_reuses_cleanup_task() -> None:
    process = MagicMock()
    cleanup = asyncio.create_task(asyncio.sleep(0))
    process._autodj_cleanup_task = cleanup

    await _terminate_alac_process(process)

    assert cleanup.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["missing", "timeout", "error", "empty", "success"])
async def test_prefetch_alac_output_handles_startup_outcomes(
    outcome: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autodj.server as server

    process = MagicMock()
    terminate = AsyncMock()
    monkeypatch.setattr(server, "_terminate_alac_process", terminate)
    if outcome == "missing":
        process.stdout = None
    elif outcome == "timeout":
        process.stdout.read = AsyncMock(side_effect=TimeoutError)
    elif outcome == "error":
        process.stdout.read = AsyncMock(side_effect=OSError("read"))
    elif outcome == "empty":
        process.stdout.read = AsyncMock(return_value=b"")
    else:
        process.stdout.read = AsyncMock(return_value=b"mp3")

    result = await _prefetch_alac_output(process)

    assert result == (b"mp3" if outcome == "success" else None)
    if outcome == "success":
        terminate.assert_not_awaited()
    else:
        terminate.assert_awaited_once_with(process)


@pytest.mark.asyncio
async def test_prefetch_alac_output_reaps_before_propagating_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autodj.server as server

    process = MagicMock()
    process.stdout.read = AsyncMock(side_effect=asyncio.CancelledError)
    terminate = AsyncMock()
    monkeypatch.setattr(server, "_terminate_alac_process", terminate)

    with pytest.raises(asyncio.CancelledError):
        await _prefetch_alac_output(process)

    terminate.assert_awaited_once_with(process)


@pytest.mark.asyncio
async def test_close_alac_stream_reaps_after_close_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autodj.server as server

    stream = MagicMock()
    stream.aclose = AsyncMock(side_effect=OSError("close"))
    process = MagicMock()
    terminate = AsyncMock()
    monkeypatch.setattr(server, "_terminate_alac_process", terminate)

    with pytest.raises(OSError, match="close"):
        await _close_alac_stream(stream, process)

    terminate.assert_awaited_once_with(process)
