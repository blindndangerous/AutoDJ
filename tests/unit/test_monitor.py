"""Local monitor queues stay bounded and isolated from the mix bus."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj.monitor import MAX_MONITORS, MonitorClosed, MonitorOutput


def _block(value: float) -> np.ndarray:
    return np.full((882, 2), value, dtype=np.float32)


@pytest.mark.asyncio
async def test_monitor_payload_is_bounded_and_a_slow_client_drops_old_audio() -> None:
    """ADR-0016-01: a slow local monitor cannot backpressure or retain the bus."""
    output = MonitorOutput()
    loop = asyncio.get_running_loop()
    slow = output.subscribe(loop)
    fast = output.subscribe(loop)
    assert slow is not None and fast is not None

    fast_values = []
    for value in range(5):
        output.write(_block(float(value)))
        fast_values.append(np.frombuffer(await fast.receive(), dtype=np.float32)[0])

    # The fast browser can drain promptly; the slow one retains only four blocks.
    assert fast_values == [0, 1, 2, 3, 4]
    assert [np.frombuffer(await slow.receive(), dtype=np.float32)[0] for _ in range(4)] == [
        1,
        2,
        3,
        4,
    ]
    output.unsubscribe(fast)
    output.unsubscribe(slow)
    assert output.listener_count == 0
    with pytest.raises(MonitorClosed):
        await slow.receive()


def test_monitor_listener_cap_and_output_shutdown() -> None:
    """A station admits at most eight browsers and closes every queue on shutdown."""
    loop = asyncio.new_event_loop()
    output = MonitorOutput()
    clients = [output.subscribe(loop) for _ in range(MAX_MONITORS)]
    assert all(client is not None for client in clients)
    assert output.subscribe(loop) is None
    output.close()
    assert output.listener_count == 0
    assert output.subscribe(loop) is None
    loop.close()


def test_station_runtime_attaches_detaches_and_closes_monitor_output() -> None:
    """ADR-0016-03: runtime owns local output registration and stop cleanup."""
    from autodj.stations import StationRuntime

    class Bus:
        def __init__(self) -> None:
            self.outputs = set()

        def add_output(self, output) -> None:
            self.outputs.add(output)

        def remove_output(self, output) -> None:
            self.outputs.discard(output)

    bus = Bus()
    runtime = StationRuntime.__new__(StationRuntime)
    runtime.bridge = SimpleNamespace(player=SimpleNamespace(bus=bus))
    runtime.monitor_output = MonitorOutput()
    runtime.stopped = False

    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        first = runtime.subscribe_monitor(loop)
        second = runtime.subscribe_monitor(loop)
        assert first is not None and second is not None
        assert bus.outputs == {runtime.monitor_output}
        runtime.unsubscribe_monitor(first)
        assert bus.outputs == {runtime.monitor_output}
        runtime.unsubscribe_monitor(second)
        assert bus.outputs == set()
        third = runtime.subscribe_monitor(loop)
        assert third is not None
        assert bus.outputs == {runtime.monitor_output}
        runtime._close_monitor_output()
        assert bus.outputs == set()
        assert runtime.monitor_output.listener_count == 0
        with pytest.raises(MonitorClosed):
            await third.receive()

    asyncio.run(exercise())


def test_local_monitor_audience_starts_and_holds_set_through_idle_grace(
    tmp_path, monkeypatch
) -> None:
    """ADR-0016-03: stream factory wires monitor audience into the existing idle clock."""
    from autodj import relay as relay_module
    from autodj import relay_settings, stream_mounts
    from autodj import stream as stream_module
    from autodj.station import Station
    from autodj.stations import start_stream_station
    from tests.unit.test_station import _Rig

    rig = _Rig()
    output = MonitorOutput()
    rig.player.bus = rig.bus
    cfg = SimpleNamespace(
        stream=SimpleNamespace(
            relays={},
            bitrate=128,
            max_listeners=8,
            station_name="Test",
            genre="",
            description="",
            url="",
            idle_grace_seconds=30.0,
            continuous=False,
            aac_bitrate=0,
            opus_bitrate=0,
        ),
        playback=MagicMock(),
    )
    rig.player._cfg = cfg
    bridge = SimpleNamespace(
        player=rig.player,
        monitor_output=output,
        library=None,
        announce_station_event=rig.events.append,
        forget_track=rig.forget,
    )

    def attach_stream(**kwargs) -> None:
        for name, value in kwargs.items():
            setattr(bridge, name, value)

    bridge.attach_stream = attach_stream
    bridge.attach_liner_scheduler = lambda scheduler: setattr(bridge, "liner_scheduler", scheduler)

    class FakeOutput:
        listener_count = 0

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeMounts:
        listener_count = 0

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self, _bitrates) -> None:
            pass

    class FakeRelays:
        def __init__(self, *_args, **_kwargs) -> None:
            self.active = False
            self.outputs: list = []

        def start(self) -> None:
            pass

    class FakeLinerScheduler:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    monkeypatch.setattr(stream_module, "StreamOutput", FakeOutput)
    monkeypatch.setattr(stream_module, "FfmpegEncoder", object())
    monkeypatch.setattr(stream_module, "probe_encoder", lambda: None)
    monkeypatch.setattr(stream_mounts, "StreamMounts", FakeMounts)
    monkeypatch.setattr(relay_settings, "load_relays", lambda *_args: {})
    monkeypatch.setattr(relay_module, "RelayManager", FakeRelays)
    monkeypatch.setattr(relay_module, "relay_status_path", lambda *_args: "unused")
    monkeypatch.setattr("autodj.liner_scheduler.LinerScheduler", FakeLinerScheduler)
    server_loop = asyncio.new_event_loop()
    start_stream_station(
        bridge,
        tmp_path,
        server_loop,
        paths=SimpleNamespace(relays=tmp_path / "relays.toml", slug="default"),
    )
    server_loop.close()

    station: Station = bridge.station
    now = [0.0]
    station._clock = lambda: now[0]
    loop = asyncio.new_event_loop()
    try:
        first = output.subscribe(loop)
        assert first is not None
        station.tick()
        rig.bus.start_set.assert_called_once()

        second = output.subscribe(loop)
        assert second is not None
        station.tick()
        assert rig.bus.start_set.call_count == 1

        now[0] = 5.0
        output.unsubscribe(first)
        station.tick()
        now[0] = 34.9
        station.tick()
        rig.bus.stop_set.assert_not_called()

        output.unsubscribe(second)
        now[0] = 35.0
        station.tick()
        now[0] = 64.9
        station.tick()
        rig.bus.stop_set.assert_not_called()
        now[0] = 65.0
        station.tick()
        rig.bus.stop_set.assert_called_once()
    finally:
        loop.close()
