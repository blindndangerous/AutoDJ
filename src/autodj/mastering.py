"""Continuous, non-blocking master processing for the mix bus."""

from __future__ import annotations

import collections
import contextlib
import logging
import math
import subprocess  # nosec B404 -- processors use fixed argv pipelines, never a shell
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final

import numpy as np

from autodj.config import DEFAULT_STEREOTOOL_PATH
from autodj.processing import MASTER_FILTER
from autodj.stereo import SAMPLE_RATE
from autodj.stereotool_presets import INCLUDED_PATH, agc_drive_preset_data

logger = logging.getLogger(__name__)

BLOCK_FRAMES: Final = 882
BLOCK_SECONDS: Final = BLOCK_FRAMES / SAMPLE_RATE
STARTUP_BLOCKS: Final = 12
# Keep six jitter blocks above the FFmpeg filter's measured 14-block delay.
# The extra cushion absorbs short Windows pipe/thread scheduling gaps, including
# the first blocks after a restarted FFmpeg process.
_OUTPUT_GUARD_BLOCKS: Final = STARTUP_BLOCKS + 6
# Stereo Tool buffers about two blocks more than FFmpeg, so it needs two fewer
# guard blocks for the same 24-block (480 ms) delay.  Its FFmpeg output limiter
# stage adds no sample delay (alimiter compensates its look-ahead; see the
# tests) and, fed in real time, its pipe added at most one block before the
# first output and none in steady state, so the total stays 480-500 ms.
_STEREOTOOL_GUARD_BLOCKS: Final = 16
_MEASURED_PIPELINE_BLOCKS: Final = _OUTPUT_GUARD_BLOCKS + 6
_QUEUE_BLOCKS: Final = _OUTPUT_GUARD_BLOCKS + 20
_LIMIT: Final = 0.87096359
_MAKEUP: Final = 10 ** (9.01 / 20)
# The limiter works at 176.4 kHz; the final 44.1 kHz resample adds up to about
# 0.55 dB of inter-sample overshoot on hot noise and square waves, so a -1.8 dBFS
# ceiling keeps the resampled true peak at or below -1.2 dBTP.
_STEREOTOOL_CEILING: Final = 10 ** (-1.8 / 20)
_STEREOTOOL_OUTPUT_FILTER: Final = (
    f"volume=5.8dB,aresample=176400,alimiter=limit={_STEREOTOOL_CEILING:.8f}:attack=5:"
    "release=100:level=0:latency=1,aresample=44100"
)


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _command(
    mode: str = "smooth",
    stereotool_path: str = DEFAULT_STEREOTOOL_PATH,
    preset: Path | None = None,
) -> list[str]:
    if mode == "stereotool":
        return [
            stereotool_path,
            "-",
            "-",
            "-b",
            "32f",
            "-r",
            str(SAMPLE_RATE),
            "-s",
            str(preset or INCLUDED_PATH),
            "-l",
            "1024",
            "-q",
        ]
    return _ffmpeg_command(MASTER_FILTER)


def _ffmpeg_command(audio_filter: str) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-nostdin",
        "-f",
        "f32le",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "2",
        "-i",
        "pipe:0",
        "-af",
        audio_filter,
        "-f",
        "f32le",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "2",
        "pipe:1",
    ]


class ContinuousMaster:
    """A persistent raw-PCM processor; ``process`` never waits on I/O.

    The input and output queues are bounded. A short output cushion absorbs
    scheduling jitter and establishes the advertised continuous delay.
    """

    def __init__(
        self,
        *,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        mode: Callable[[], str] = lambda: "smooth",
        stereotool_path: str = DEFAULT_STEREOTOOL_PATH,
        preset: Callable[[], Path] | None = None,
        input_trim_db: Callable[[], float] = lambda: 0.0,
        agc_drive_db: Callable[[], float] = lambda: 7.0,
    ) -> None:
        self._factory = popen_factory
        self._select_mode = mode
        self._mode = mode()
        self._stereotool_path = stereotool_path
        self._select_preset = preset or (lambda: INCLUDED_PATH)
        self._select_agc_drive = agc_drive_db
        self._preset_temp = tempfile.TemporaryDirectory(prefix="autodj-stereo-tool-")
        self._preset = self._stereotool_signature() if self._mode == "stereotool" else None
        self._input_trim_db = input_trim_db
        self._output_guard_blocks = (
            _STEREOTOOL_GUARD_BLOCKS if self._mode == "stereotool" else _OUTPUT_GUARD_BLOCKS
        )
        self._lock = threading.RLock()
        self._changed = threading.Event()
        self._closed = False
        self._enabled = self._mode in {"smooth", "stereotool"}
        self._generation = 0
        self._input: collections.deque[tuple[int, bytes]] = collections.deque()
        self._output: collections.deque[tuple[int, np.ndarray]] = collections.deque()
        # Keep a fixed-latency copy of input so a failed/restarting
        # process can bypass without shifting or repeating the timeline.
        self._fallback_delay: collections.deque[np.ndarray] = collections.deque(
            maxlen=_MEASURED_PIPELINE_BLOCKS + 1
        )
        self._output_cond = threading.Condition(self._lock)
        self._proc: subprocess.Popen[bytes] | None = None
        self._post_proc: subprocess.Popen[bytes] | None = None
        self._ready = False
        self._failed = False
        self._recovering = False
        self._recovery_outputs = 0
        self._healthy_outputs = 0
        self._error: str | None = None
        self._restarts = 0
        self._overflow_reported = False
        self._worker = threading.Thread(target=self._lifecycle, name="autodj-master", daemon=True)
        self._worker.start()

    @property
    def latency_seconds(self) -> float:
        # Measured pipeline delay, including processor buffering and output cushion.
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
        mode = self._select_mode()
        preset = self._stereotool_signature() if mode == "stereotool" else None
        with self._lock:
            if self._closed:
                return self._fallback(audio)
            if not self._enabled or mode != self._mode or preset != self._preset:
                self._switch(True, mode)
            if mode == "stereotool":
                audio = audio * (10 ** (self._input_trim_db() / 20))
            audio = np.ascontiguousarray(audio)
            self._fallback_delay.append(audio.copy())
            if self._failed:
                return self._delayed_fallback()
            generation = self._generation
            if len(self._input) >= _QUEUE_BLOCKS:
                self._input.popleft()
                self._set_error("master input queue overflow; dropped oldest block")
            self._input.append((generation, audio.tobytes()))
            if not self._ready and len(self._output) > self._output_guard_blocks:
                self._ready = True
            if self._ready and self._output and self._output[0][0] == generation:
                output = self._output.popleft()[1]
                self._output_cond.notify_all()
                if self._recovering:
                    if not np.any(output):
                        fallback = self._delayed_fallback()
                        self._recovery_outputs += 1
                        if self._recovery_outputs >= self._output_guard_blocks:
                            self._recovering = False
                            self._error = None
                            self._overflow_reported = False
                        return fallback
                    if self._fallback_delay:
                        self._fallback_delay.popleft()
                    self._recovering = False
                    self._error = None
                    self._overflow_reported = False
                elif self._fallback_delay:
                    self._fallback_delay.popleft()
                if self._error and "overflow" in self._error:
                    self._healthy_outputs += 1
                    if self._healthy_outputs >= round(1.0 / BLOCK_SECONDS):
                        self._error = None
                        self._overflow_reported = False
                        self._healthy_outputs = 0
                return output
            if self._ready:
                self._set_error("master output underrun; restarting processor")
                self._restarts += 1
                self._switch(True)
            if self._recovering:
                return self._delayed_fallback()
            return np.zeros_like(audio)

    def _switch(self, enabled: bool, mode: str | None = None) -> None:
        changing_processor = enabled and self._enabled
        self._enabled = enabled
        if mode is not None:
            self._mode = mode
            self._preset = self._stereotool_signature() if mode == "stereotool" else None
        self._output_guard_blocks = (
            _STEREOTOOL_GUARD_BLOCKS if self._mode == "stereotool" else _OUTPUT_GUARD_BLOCKS
        )
        self._generation += 1
        self._input.clear()
        self._output.clear()
        self._ready = False
        self._failed = False
        self._recovering = changing_processor
        self._recovery_outputs = 0
        if not changing_processor:
            self._fallback_delay.clear()
        self._changed.set()
        self._output_cond.notify_all()

    def _stereotool_signature(self) -> tuple[Path, float]:
        source = self._select_preset()
        drive_db = float(self._select_agc_drive())
        if not math.isfinite(drive_db) or not 0 <= drive_db <= 12:
            raise ValueError("playback.stereotool_agc_drive_db must be between 0 and 12")
        return source, drive_db

    def _runtime_stereotool_preset(self, signature: tuple[Path, float]) -> Path:
        source, drive_db = signature
        try:
            data = agc_drive_preset_data(source.read_bytes(), drive_db)
        except (OSError, ValueError) as exc:
            if source == INCLUDED_PATH:
                raise
            logger.warning(
                "Stereo Tool preset %s cannot apply AGC drive (%s); using included preset",
                source,
                exc,
            )
            source = INCLUDED_PATH
            data = agc_drive_preset_data(source.read_bytes(), drive_db)
        if drive_db == 0:
            return source
        target = Path(self._preset_temp.name) / "runtime.sts"
        target.write_bytes(data)
        return target

    def _set_error(self, message: str) -> None:
        self._error = message
        self._healthy_outputs = 0
        if not self._overflow_reported:
            logger.warning("%s", message)
            self._overflow_reported = True

    def _delayed_fallback(self) -> np.ndarray:
        """Return bounded makeup audio at the pipeline's established delay."""
        if len(self._fallback_delay) <= _MEASURED_PIPELINE_BLOCKS:
            return np.zeros((BLOCK_FRAMES, 2), dtype=np.float32)
        return self._fallback(self._fallback_delay.popleft())

    @staticmethod
    def _fallback(audio: np.ndarray) -> np.ndarray:
        # Prepared sources retain 6 dB headroom; apply the chain's final
        # makeup and bound peaks while the processor recovers.
        return np.clip(audio * _MAKEUP, -_LIMIT, _LIMIT).astype(np.float32, copy=False)

    def _lifecycle(self) -> None:
        active_generation = -1
        proc = post_proc = None
        writer = reader = None
        stop: threading.Event | None = None
        while True:
            with self._lock:
                closed = self._closed
                enabled = self._enabled
                generation = self._generation
                mode = self._mode
                preset = self._preset
            name = "Stereo Tool" if mode == "stereotool" else "FFmpeg"
            if closed:
                self._stop_pipeline(proc, writer, reader, stop, post_proc)
                self._preset_temp.cleanup()
                return
            if not enabled:
                if proc is not None:
                    self._stop_pipeline(proc, writer, reader, stop, post_proc)
                    proc = post_proc = writer = reader = None
                    stop = None
                    active_generation = -1
                self._changed.wait(0.1)
                self._changed.clear()
                continue
            if proc is None or generation != active_generation:
                if proc is not None:
                    self._stop_pipeline(proc, writer, reader, stop, post_proc)
                if mode == "stereotool" and preset is not None:
                    # Build after stopping the old child so one temp file can be reused safely.
                    preset = self._runtime_stereotool_preset(preset)
                try:
                    proc = self._factory(  # nosec B603 -- configured executable, fixed argv, no shell
                        _command(mode, self._stereotool_path, preset),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        bufsize=0,
                        creationflags=_creation_flags(),
                    )
                    if proc.stdin is None or proc.stdout is None:
                        raise OSError(f"{name} master pipes unavailable")
                    post_proc = None
                    if mode == "stereotool":
                        post_proc = self._factory(  # nosec B603 -- fixed FFmpeg argv, no shell
                            _ffmpeg_command(_STEREOTOOL_OUTPUT_FILTER),
                            stdin=proc.stdout,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            bufsize=0,
                            creationflags=_creation_flags(),
                        )
                        if post_proc.stdout is None:
                            raise OSError("Stereo Tool output limiter pipe unavailable")
                        proc.stdout.close()
                except Exception as exc:
                    self._stop_pipeline(proc, None, None, None, post_proc)
                    proc = post_proc = None
                    with self._lock:
                        if self._closed:
                            return
                        self._failed = True
                        self._recovering = True
                        self._recovery_outputs = 0
                        self._input.clear()
                        self._output.clear()
                        self._error = f"{name} master unavailable: {exc}"
                        self._restarts += 1
                    self._changed.wait(0.5)
                    self._changed.clear()
                    continue
                active_generation = generation
                stop = threading.Event()
                writer = threading.Thread(
                    target=self._write_loop,
                    args=(proc, active_generation, stop),
                    name="autodj-master-writer",
                    daemon=True,
                )
                reader = threading.Thread(
                    target=self._read_loop,
                    args=(post_proc if post_proc is not None else proc, active_generation, stop),
                    name="autodj-master-reader",
                    daemon=True,
                )
                stale = False
                with self._lock:
                    if active_generation == self._generation and self._enabled:
                        self._proc = proc
                        self._post_proc = post_proc
                        self._ready = False
                        self._failed = False
                        self._overflow_reported = False
                        if not self._recovering:
                            self._error = None
                    else:
                        stale = True
                if stale:
                    self._stop_pipeline(proc, writer, reader, stop, post_proc)
                    proc = post_proc = writer = reader = None
                    stop = None
                    continue
                writer.start()
                reader.start()
            exited = next(
                (
                    stage
                    for stage in (proc, post_proc)
                    if stage is not None and stage.poll() is not None
                ),
                None,
            )
            if exited is not None:
                self._stop_pipeline(proc, writer, reader, stop, post_proc)
                with self._lock:
                    self._proc = self._post_proc = None
                    self._ready = False
                    self._failed = True
                    self._recovering = True
                    self._recovery_outputs = 0
                    self._input.clear()
                    self._output.clear()
                    self._error = (
                        f"{name} master exited ({exited.returncode}); using limited bypass"
                    )
                    self._restarts += 1
                proc = post_proc = writer = reader = None
                stop = None
                active_generation = -1
                self._changed.wait(0.5)
                self._changed.clear()
                continue
            self._changed.wait(0.02)
            self._changed.clear()

    def _write_loop(
        self, proc: subprocess.Popen[bytes], generation: int, stop: threading.Event
    ) -> None:
        stdin = proc.stdin
        if stdin is None:
            return
        # Stereo Tool's CLI drops samples on short input chunks; feed full 1024-frame blocks.
        write_bytes = (1024 if self._mode == "stereotool" else BLOCK_FRAMES) * 2 * 4
        pending = bytearray()
        while not stop.is_set():
            with self._lock:
                if generation != self._generation or not self._enabled:
                    return
                block = self._input.popleft() if self._input else None
            if block is None:
                stop.wait(0.005)
                continue
            if block[0] != generation:
                continue
            pending.extend(block[1])
            try:
                while len(pending) >= write_bytes:
                    payload = memoryview(bytes(pending[:write_bytes]))
                    del pending[:write_bytes]
                    while payload:
                        written = stdin.write(payload)
                        if not written:
                            return
                        payload = payload[written:]
            except (BrokenPipeError, OSError, ValueError):
                return

    def _read_loop(
        self, proc: subprocess.Popen[bytes], generation: int, stop: threading.Event
    ) -> None:
        stdout = proc.stdout
        if stdout is None:
            return
        frame_bytes = BLOCK_FRAMES * 2 * 4
        pending = bytearray()
        while not stop.is_set():
            try:
                data = stdout.read(frame_bytes)
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
                with self._output_cond:
                    while (
                        len(self._output) >= _QUEUE_BLOCKS
                        and not stop.is_set()
                        and generation == self._generation
                        and self._enabled
                    ):
                        # Keep the queue bounded without discarding timeline
                        # blocks when the bus is briefly delayed. Backpressure
                        # propagates through the processor's pipe until consumption
                        # resumes.
                        self._output_cond.wait(timeout=0.1)
                    if stop.is_set():
                        return
                    if generation != self._generation or not self._enabled:
                        continue
                    self._output.append((generation, audio))
                    self._output_cond.notify_all()

    @staticmethod
    def _stop_pipeline(
        proc: subprocess.Popen[bytes] | None,
        writer: threading.Thread | None,
        reader: threading.Thread | None,
        stop: threading.Event | None,
        post_proc: subprocess.Popen[bytes] | None = None,
    ) -> None:
        if stop is not None:
            stop.set()
        processes = [stage for stage in (proc, post_proc) if stage is not None]
        for stage in processes:
            with contextlib.suppress(OSError, ProcessLookupError):
                stage.terminate()
        for stage in processes:
            try:
                stage.wait(timeout=0.5)
            except (subprocess.TimeoutExpired, OSError):
                with contextlib.suppress(OSError, ProcessLookupError):
                    stage.kill()
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    stage.wait(timeout=0.5)
            for stream in (getattr(stage, "stdin", None), getattr(stage, "stdout", None)):
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
