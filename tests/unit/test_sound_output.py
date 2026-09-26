"""Ring-buffered sound-card output."""

from __future__ import annotations

import numpy as np

from autodj.player import PlayerState
from autodj.sound_output import SoundDeviceOutput


def _output(state: PlayerState | None = None) -> SoundDeviceOutput:
    return SoundDeviceOutput(
        state or PlayerState(), device=None, buffer_blocks=3, stream_factory=lambda **_kw: None
    )


def test_fill_plays_buffered_blocks_in_order() -> None:
    out = _output()
    out.write(np.full((882, 2), 0.1, np.float32))
    out.write(np.full((882, 2), 0.2, np.float32))
    buf = np.zeros((882, 2), np.float32)
    out._fill(buf, 882)
    np.testing.assert_allclose(buf, 0.1)
    out._fill(buf, 882)
    np.testing.assert_allclose(buf, 0.2)


def test_underrun_pads_silence() -> None:
    out = _output()
    buf = np.ones((882, 2), np.float32)
    out._fill(buf, 882)
    np.testing.assert_array_equal(buf, 0.0)


def test_overrun_drops_oldest() -> None:
    out = _output()
    for value in (0.1, 0.2, 0.3, 0.4):
        out.write(np.full((882, 2), value, np.float32))
    buf = np.zeros((882, 2), np.float32)
    out._fill(buf, 882)
    np.testing.assert_allclose(buf, 0.2)


def test_volume_and_mute_are_applied() -> None:
    state = PlayerState()
    state.volume = 0.5
    out = _output(state)
    out.write(np.full((882, 2), 0.4, np.float32))
    buf = np.zeros((882, 2), np.float32)
    out._fill(buf, 882)
    np.testing.assert_allclose(buf, 0.2)
    state.is_muted = True
    out.write(np.full((882, 2), 0.4, np.float32))
    out._fill(buf, 882)
    np.testing.assert_array_equal(buf, 0.0)


def test_partial_frames_request() -> None:
    out = _output()
    out.write(np.arange(882 * 2, dtype=np.float32).reshape(882, 2) / 10_000)
    first = np.zeros((500, 2), np.float32)
    second = np.zeros((500, 2), np.float32)
    out._fill(first, 500)
    out._fill(second, 500)
    # second[381] is frame 881, the block's last; silence pads from 382 on.
    assert second[381, 0] == np.float32(1762 / 10_000)
    assert second[382, 0] == 0.0
    assert first[0, 1] == np.float32(1 / 10_000)


def test_close_without_a_stream_is_a_no_op() -> None:
    _output().close()
