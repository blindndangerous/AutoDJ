"""Bounded stereo protocol and input selection, with no wall-clock sleeps."""

import struct
from dataclasses import replace

import numpy as np
import pytest

from autodj.local_programme import Grant
from autodj.local_programme_audio import LocalPCM, PCMError
from tests.unit.test_mixbus import _bus, _track

GRANT = Grant("epoch", 7, "secret", "identity", "socket")
HEADER = struct.Struct("<4sBBHQQQII8s")


def packet(sequence=0, *, generation=7, rate=44100, frames=441, values=(0.25, -0.5)):
    pcm = np.tile(np.array(values, dtype="<f4"), (frames, 1))
    return (
        HEADER.pack(b"ADJP", 1, 0, 2, generation, sequence, sequence * 441, frames, rate, bytes(8))
        + pcm.tobytes()
    )


def filled(count=6):
    audio = LocalPCM(GRANT, now=0)
    audio.arm()
    for seq in range(count):
        audio.accept_packet(GRANT, packet(seq), now=seq * 0.01)
    return audio


def test_public_handoff_ramps_at_boundaries_and_keeps_outputs():
    """ADR-0019-05: programme handoff uses the existing continuous output chain."""
    audio = filled(10)
    audio.clock = lambda: 0.1
    bus, _, _ = _bus([_track(0.1, 44100)])
    bus.start_set()
    bus.render_block()
    outputs = bus._outputs
    bus.take_local_programme(audio)
    down = bus.render_block()
    up = bus.render_block()
    assert abs(down[-1, 0]) < 1e-6
    assert abs(up[0, 0]) < 1e-6
    assert up[-1, 0] == pytest.approx(0.25)
    assert bus._outputs is outputs
    assert bus._local_programme is audio


def test_local_fade_drains_music_and_liner_callbacks_outside_bus_lock():
    """ADR-0019-05: the last automation block retains its accounting callbacks."""
    audio = filled(10)
    bus, _, _ = _bus([])
    callbacks = []

    def record(kind):
        assert not bus._lock._is_owned()
        callbacks.append(kind)

    def music(frames, pending):
        pending.append(lambda: record("music"))
        bus._liner_started.append(lambda: record("liner"))
        return np.ones((frames, 2), np.float32)

    bus._music = music
    bus.take_local_programme(audio)
    bus.render_block()
    assert callbacks == ["music", "liner"]
    assert bus._liner_started == []


def test_handoff_prevalidation_runs_outside_bus_lock(tmp_path, monkeypatch):
    """ADR-0019-05: admission filesystem work cannot stall the render lock."""
    from pathlib import Path

    audio = filled(10)
    bus, _, _ = _bus([])
    path = tmp_path / "source.wav"
    path.write_bytes(b"song")
    original = Path.stat
    observed = []

    def stat(source, *args, **kwargs):
        observed.append(bus._lock._is_owned())
        return original(source, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    bus.take_local_programme(audio, validate=lambda: path.stat())
    assert observed == [False]


def test_handoff_starts_pcm_without_holding_bus_lock():
    """ADR-0019-05: taking local ownership never nests the PCM lock under the bus."""
    audio = filled(10)
    bus, _, _ = _bus([])
    start = audio.start
    observed = []

    def checked_start():
        observed.append(bus._lock._is_owned())
        start()

    audio.start = checked_start
    bus.take_local_programme(audio)
    assert observed == [False]


def test_prearm_load_failure_completes_from_discarded_silence():
    """ADR-0019-06: failed loads free reservations before an audible start is armed."""
    audio = LocalPCM(GRANT, now=0)
    audio.events.reserve("failed")
    marker = {"sequence": 0, "kind": "load-failure", "occurrence": "failed", "frame": 0}
    audio.events.submit(marker, 0)
    audio.accept_packet(GRANT, packet(values=(0, 0)), now=0)
    assert audio.queued_frames == 0
    assert audio.events.receipts() == [marker]
    audio.events.complete(marker)
    assert audio.events.submit(marker, 441) == "completed"


def test_sustained_high_occupancy_returns_before_the_hard_cap():
    """ADR-0019-03: five seconds outside 20–160 ms is explicit clock-drift failure."""
    audio = filled(18)
    audio.clock = lambda: 0.17
    audio.start()
    for seq in range(18, 530):
        now = seq * 0.01
        audio.read_block(GRANT.generation, 441, now=now)
        if audio.failed:
            break
        audio.accept_packet(GRANT, packet(seq), now=now)
    assert "drift" in audio.failed
    assert now < 5.3


def test_framing_credit_and_timeout_bounds():
    """ADR-0019-03: exact stereo frames, bounded credit, consumed spans and stalls."""
    audio = filled(20)
    assert audio.queued_frames == audio.high_water_frames == 8820
    assert audio.credit_frames == 0
    with pytest.raises(PCMError, match="credit"):
        audio.accept_packet(GRANT, packet(20), now=0.2)
    block, spans = audio.read_block(7, now=0.2)
    np.testing.assert_array_equal(block, np.tile([0.25, -0.5], (882, 1)))
    assert spans == [(0, 441), (441, 441)]
    assert audio.credit_frames == 882
    audio.accept_packet(GRANT, packet(20), now=0.2)
    assert audio.queued_frames == 8379
    assert not audio.check(0.7)
    assert audio.queued_frames == 0
    np.testing.assert_array_equal(audio.read_block(7, now=0.7)[0], np.zeros((882, 2)))


@pytest.mark.parametrize(
    "bad",
    [
        packet(rate=48000),
        packet(rate=22050),
        packet(frames=440),
        packet(frames=442),
        packet(generation=6),
        packet(sequence=1),
        packet(values=(float("nan"), 0)),
        packet(values=(0, float("inf"))),
        packet(values=(16.1, 0)),
        b"",
        packet()[:-1],
        packet() + b"x",
        b"NOPE" + packet()[4:],
        packet()[:5] + b"\x01" + packet()[6:],
    ],
)
def test_rejects_malformed_without_advancing(bad):
    """ADR-0019-03: invalid format, generation, sequence and samples never enter the ring."""
    audio = LocalPCM(GRANT, now=0)
    audio.arm()
    with pytest.raises(PCMError):
        audio.accept_packet(GRANT, bad, now=0)
    assert audio.queued_frames == 0
    assert audio.next_sequence == 0


def test_preparation_silence_rate_flood_and_grant_fence():
    """ADR-0019-03: preparation discards zero PCM; credit cannot excuse a flood."""
    audio = LocalPCM(GRANT, now=0)
    with pytest.raises(PCMError, match="preparation"):
        audio.accept_packet(GRANT, packet(), now=0)
    for seq in range(100):
        audio.accept_packet(GRANT, packet(seq, values=(0, 0)), now=seq * 0.01)
    assert audio.queued_frames == 0
    assert audio.credit_frames == 8820
    assert audio.check(1)
    with pytest.raises(PCMError, match="grant"):
        audio.accept_packet(replace(GRANT, socket_id="other"), packet(100), now=1)
    audio = LocalPCM(GRANT, now=0)
    for seq in range(20):
        audio.accept_packet(GRANT, packet(seq, values=(0, 0)), now=0)
    with pytest.raises(PCMError, match="rate"):
        audio.accept_packet(GRANT, packet(20, values=(0, 0)), now=0)


def test_receive_jitter_batch_keeps_the_same_pcm_credit_bound():
    """ADR-0019-03: a bounded arrival batch is not a sustained-rate flood."""
    audio = LocalPCM(GRANT, now=0)
    audio.arm()
    # A 100 Hz sender can arrive in batches after receiver scheduling delays.
    for seq in range(20):
        audio.accept_packet(GRANT, packet(seq), now=0.2)
    assert audio.queued_frames == 8820
    assert audio.credit_frames == 0
    with pytest.raises(PCMError, match="credit"):
        audio.accept_packet(GRANT, packet(20), now=0.2)
    assert audio.next_sequence == 20


def test_receive_jitter_allowance_does_not_expand_one_second_rate_budget():
    """ADR-0019-03: silence cannot use burst headroom to evade the rolling cap."""
    audio = LocalPCM(GRANT, now=0)
    for seq in range(125):
        audio.accept_packet(GRANT, packet(seq, values=(0, 0)), now=max(0, (seq - 19) * 0.0082))
    with pytest.raises(PCMError, match="rate"):
        audio.accept_packet(GRANT, packet(125, values=(0, 0)), now=0.96)
    assert audio.next_sequence == 125
    assert audio.queued_frames == 0
    audio.accept_packet(GRANT, packet(125, values=(0, 0)), now=1.001)
    assert audio.next_sequence == 126


def test_prefill_underrun_and_revocation_never_replay():
    """ADR-0019-03: 60 ms prefill, zero shortages, third short block fences input."""
    audio = filled(5)
    assert not audio.ready
    with pytest.raises(PCMError, match="prefill"):
        audio.start()
    audio.accept_packet(GRANT, packet(5), now=0.05)
    audio.start()
    for _ in range(3):
        assert audio.read_block(7, now=0.06)[1]
    for i in range(3):
        block, spans = audio.read_block(7, now=0.08 + i * 0.02)
        assert not spans
        assert not block.any()
    assert audio.failed
    with pytest.raises(PCMError):
        audio.accept_packet(GRANT, packet(6), now=0.14)
    audio = filled()
    audio.close("revoked")
    assert not audio.read_block(7, now=0.06)[0].any()
    assert audio.queued_frames == 0


def test_partial_packet_spans_and_stale_generation():
    audio = filled()
    block, spans = audio.read_block(7, frames=500, now=0.06)
    assert len(block) == 500 and spans == [(0, 441), (441, 59)]
    _, spans = audio.read_block(7, frames=500, now=0.06)
    assert spans == [(500, 382), (882, 118)]
    assert not audio.read_block(6, now=0.06)[0].any()


def test_selector_holds_automation_and_bypasses_eq_liners():
    """ADR-0019-13: input selector holds automation and retains master/output objects."""
    bus, started, positions = _bus([_track(0.1, 100000)], gains=(0, 0, 0))
    bus.start_set()
    bus.render_block()
    position = bus.live_position(bus.current_render())
    audio = filled()
    bus._select_local_programme(audio)
    bus.play_liner(np.ones((10000, 2), np.float32), -12)
    # A controlled test clock avoids mixing this fixture's time with monotonic time.
    audio.clock = lambda: 0.06
    np.testing.assert_array_equal(bus.render_block(), np.tile([0.25, -0.5], (882, 1)))
    assert bus.live_position(bus.current_render()) == position
    assert len(started) == len(positions) == 1
    bus._select_local_programme(None)
    bus.render_block()
    assert bus.live_position(bus.current_render()) > position
