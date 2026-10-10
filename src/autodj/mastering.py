"""Continuous, non-blocking FFmpeg master processing for the mix bus."""

from __future__ import annotations

import collections
import contextlib
import logging
import subprocess
import threading
from typing import Final

import numpy as np

from autodj.processing import MASTER_FILTER
from autodj.stereo import SAMPLE_RATE

logger = logging.getLogger(__name__)

BLOCK_FRAMES: Final = 882
BLOCK_SECONDS: Final = BLOCK_FRAMES / SAMPLE_RATE
STARTUP_BLOCKS: Final = 12
_MEASURED_PIPELINE_BLOCKS: Final = 14
_QUEUE_BLOCKS: Final = STARTUP_BLOCKS + 8
_LIMIT: Final = 0.87096359
_MAKEUP: Final = 10 ** (9.01 / 20)


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _command() -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-nostdin",
        "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "2", "-i", "pipe:0",
        "-af", MASTER_FILTER, "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "2", "pipe:1",
    ]


class ContinuousMaster:
    """A persistent raw-PCM FFmpeg pipeline; ``process`` never waits on I/O.

    The input and output queues are bounded. A short output cushion absorbs
    FFmpeg scheduling jitter and establishes the advertised continuous delay.
    """

    def __init__(self, *, popen_factory=subprocess.Popen) -> None:
        self._factory = popen_factory
        self._lock = threading.RLock()
        self._changed = threading.Event()
        self._closed = False
        self._enabled = True
        self._generation = 0
        self._input: collections.deque[tuple[int, bytes]] = collections.deque()
        self._output: collections.deque[tuple[int, np.ndarray]] = collections.deque()
        self._output_cond = threading.Condition(self._lock)
        self._proc: subprocess.Popen | None = None
        self._ready = False
        self._failed = False
        self._error: str | None = None
        self._restarts = 0
        self._overflow_reported = False
        self._worker = threading.Thread(target=self._lifecycle, name="autodj-master", daemon=True)
        self._worker.start()

    @property
    def latency_seconds(self) -> float:
        # Measured with an impulse through the shipped filter chain: the
        # startup cushion plus FFmpeg's resampler/limiter buffering.
        return _MEASURED_PIPELINE_BLOCKS * BLOCK_SECONDS

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    @property
    def restarts(self) -> int:
        with self._lock:
            return self._restarts

    def process(self, block: np.ndarray, enabled: bool = True) -> np.ndarray:
        """Queue one bus block and return processed audio or safe warmup audio."""
        audio = np.asarray(block, dtype=np.float32)
        if audio.shape != (BLOCK_FRAMES, 2):
            raise ValueError(f"block must have shape ({BLOCK_FRAMES}, 2)")
        if not enabled:
            with self._lock:
                if self._enabled:
                    self._switch(False)
            return audio

        audio = np.nan_to_num(audio, copy=False)
        with self._lock:
            if self._closed:
                return self._fallback(audio)
            if not self._enabled:
                self._switch(True)
            generation = self._generation
            if len(self._input) >= _QUEUE_BLOCKS:
                self._input.popleft()
                self._set_error("master input queue overflow; dropped oldest block")
            self._input.append((generation, np.ascontiguousarray(audio).tobytes()))
            if len(self._output) > STARTUP_BLOCKS and self._output[0][0] == generation:
                return self._output.popleft()[1]
            if self._failed:
                return self._fallback(audio)
            return np.zeros_like(audio)

    def _switch(self, enabled: bool) -> None:
        self._enabled = enabled
        self._generation += 1
        self._input.clear()
        self._output.clear()
        self._ready = False
        self._failed = False
        self._changed.set()
        self._output_cond.notify_all()

    def _set_error(self, message: str) -> None:
        self._error = message
        if not self._overflow_reported:
            logger.warning("%s", message)
            self._overflow_reported = True

    @staticmethod
    def _fallback(audio: np.ndarray) -> np.ndarray:
        # Prepared sources retain 6 dB headroom; apply the chain's final
        # makeup and bound peaks while FFmpeg recovers.
        return np.clip(audio * _MAKEUP, -_LIMIT, _LIMIT).astype(np.float32, copy=False)

    def _lifecycle(self) -> None:
        active_generation = -1
        proc = None
        writer = reader = None
        while True:
            with self._lock:
                closed = self._closed
                enabled = self._enabled
                generation = self._generation
            if closed:
                self._stop_pipeline(proc, writer, reader)
                return
            if not enabled:
                if proc is not None:
                    self._stop_pipeline(proc, writer, reader)
                    proc = writer = reader = None
                    active_generation = -1
                self._changed.wait(0.1)
                self._changed.clear()
                continue
            if proc is None or generation != active_generation:
                if proc is not None:
                    self._stop_pipeline(proc, writer, reader)
                try:
                    proc = self._factory(
                        _command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, bufsize=0,
                        creationflags=_creation_flags(),
                    )
                    if proc.stdin is None or proc.stdout is None:
                        raise OSError("FFmpeg master pipes unavailable")
                except Exception as exc:
                    proc = None
                    with self._lock:
                        if self._closed:
                            return
                        self._failed = True
                        self._error = f"FFmpeg master unavailable: {exc}"
                        self._restarts += 1
                    self._changed.wait(0.5)
                    self._changed.clear()
                    continue
                active_generation = generation
                stop = threading.Event()
                writer = threading.Thread(
                    target=self._write_loop, args=(proc, active_generation, stop),
                    name="autodj-master-writer", daemon=True,
                )
                reader = threading.Thread(
                    target=self._read_loop, args=(proc, active_generation, stop),
                    name="autodj-master-reader", daemon=True,
                )
                stale = False
                with self._lock:
                    if active_generation == self._generation and self._enabled:
                        self._proc = proc
                        self._ready = True
                        self._failed = False
                        self._overflow_reported = False
                        self._error = None
                    else:
                        stale = True
                if stale:
                    self._stop_pipeline(proc, writer, reader)
                    proc = writer = reader = None
                    continue
                proc._autodj_stop = stop
                writer.start()
                reader.start()
            if proc.poll() is not None:
                self._stop_pipeline(proc, writer, reader)
                with self._lock:
                    self._proc = None
                    self._ready = False
                    self._failed = True
                    self._error = f"FFmpeg master exited ({proc.returncode}); using limited bypass"
                    self._restarts += 1
                proc = writer = reader = None
                active_generation = -1
                self._changed.wait(0.5)
                self._changed.clear()
                continue
            self._changed.wait(0.02)
            self._changed.clear()

    def _write_loop(self, proc: subprocess.Popen, generation: int, stop: threading.Event) -> None:
        while not stop.is_set():
            with self._lock:
                block = self._input.popleft() if self._input else None
            if block is None:
                stop.wait(0.005)
                continue
            if block[0] != generation:
                continue
            try:
                proc.stdin.write(block[1])
            except (BrokenPipeError, OSError, ValueError):
                return

    def _read_loop(self, proc: subprocess.Popen, generation: int, stop: threading.Event) -> None:
        frame_bytes = BLOCK_FRAMES * 2 * 4
        pending = bytearray()
        while not stop.is_set():
            try:
                data = proc.stdout.read(frame_bytes)
            except (OSError, ValueError):
                return
            if not data:
                return
            pending.extend(data)
            while len(pending) >= frame_bytes:
                raw = bytes(pending[:frame_bytes])
                del pending[:frame_bytes]
                audio = np.frombuffer(raw, dtype=np.float32).reshape(BLOCK_FRAMES, 2).copy()
                if not np.isfinite(audio).all():
                    audio = np.nan_to_num(audio)
                with self._lock:
                    if generation != self._generation or not self._enabled:
                        continue
                    if len(self._output) >= _QUEUE_BLOCKS:
                        self._output.popleft()
                        self._set_error("master output queue overflow; dropped oldest block")
                    self._output.append((generation, audio))
                    self._output_cond.notify_all()

    @staticmethod
    def _stop_pipeline(proc, writer, reader) -> None:
        if proc is None:
            return
        stop = getattr(proc, "_autodj_stop", None)
        if stop is not None:
            stop.set()
        with contextlib.suppress(OSError, ProcessLookupError):
            proc.terminate()
        try:
            proc.wait(timeout=0.5)
        except (subprocess.TimeoutExpired, OSError):
            with contextlib.suppress(OSError, ProcessLookupError):
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                proc.wait(timeout=0.5)
        for stream in (getattr(proc, "stdin", None), getattr(proc, "stdout", None)):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        for thread in (writer, reader):
            if thread is not None and thread.ident is not None:
                thread.join(timeout=0.25)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._changed.set()
        self._worker.join(timeout=2.0)

