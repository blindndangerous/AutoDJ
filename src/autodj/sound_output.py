"""Long-lived stereo sound-card output fed by the mix bus."""

from __future__ import annotations

import collections
import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np

from autodj.stereo import SAMPLE_RATE

if TYPE_CHECKING:
    from autodj.player import PlayerState

logger = logging.getLogger(__name__)


class SoundDeviceOutput:
    """Play mix-bus blocks through one open sounddevice stream.

    A small ring buffer absorbs drift between the sound card's clock and the
    mix bus clock: an empty buffer plays silence and a full one drops its
    oldest block.
    """

    def __init__(
        self,
        state: PlayerState,
        device: str | int | None,
        buffer_blocks: int = 10,
        stream_factory: Callable[..., Any] | None = None,
    ) -> None:
        """Open the output stream.

        Args:
            state: Player state for volume and mute.
            device: sounddevice device name or index, or ``None`` for the
                default.
            buffer_blocks: Ring buffer size in 20 ms blocks.
            stream_factory: Builds the stream; defaults to ``sounddevice.OutputStream``.
        """
        self._state = state
        self._blocks: collections.deque[np.ndarray] = collections.deque(maxlen=buffer_blocks)
        self._head: np.ndarray | None = None
        self._head_pos = 0
        self._lock = threading.Lock()
        if stream_factory is None:  # pragma: no cover -- real audio hardware
            import sounddevice as sd

            stream_factory = sd.OutputStream
        self._stream = stream_factory(
            samplerate=SAMPLE_RATE,
            channels=2,
            dtype="float32",
            callback=self._callback,
            device=device,
        )
        if self._stream is not None:  # pragma: no cover -- real audio hardware
            self._stream.start()

    def write(self, block: np.ndarray) -> None:
        """Queue *block*; drops the oldest block when the buffer is full."""
        with self._lock:
            self._blocks.append(block)

    def _fill(self, outdata: np.ndarray, frames: int) -> None:
        """Copy *frames* buffered frames into *outdata*, padding with silence."""
        filled = 0
        with self._lock:
            while filled < frames:
                if self._head is None or self._head_pos >= len(self._head):
                    if not self._blocks:
                        break
                    self._head = self._blocks.popleft()
                    self._head_pos = 0
                take = min(frames - filled, len(self._head) - self._head_pos)
                outdata[filled : filled + take] = self._head[self._head_pos : self._head_pos + take]
                self._head_pos += take
                filled += take
        outdata[filled:frames] = 0.0
        if self._state.is_muted:
            outdata[:frames] = 0.0
        elif self._state.volume < 1.0:
            outdata[:frames] *= self._state.volume

    def _callback(self, outdata, frames, _time_info, _status) -> None:  # type: ignore[no-untyped-def]  # pragma: no cover
        """sounddevice's audio callback: fill the device buffer."""
        self._fill(outdata, frames)

    def close(self) -> None:
        """Stop and close the stream."""
        if self._stream is not None:  # pragma: no cover -- real audio hardware
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                logger.exception("Closing the sound output failed")
