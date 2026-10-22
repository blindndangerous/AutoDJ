"""Progressive, bounded FFmpeg decoding for server audio."""

from __future__ import annotations

import contextlib
import logging
import math
import shutil
import subprocess  # nosec B404 -- fixed FFmpeg argv, no shell
import threading
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

from autodj.child_jobs import bind_to_autodj
from autodj.stereo import SAFE_FFMPEG_INPUT

logger = logging.getLogger(__name__)
SAMPLE_RATE = 44100
CHANNELS = 2
_FRAME_BYTES = CHANNELS * 4
_BUFFER_FRAMES = 2 * SAMPLE_RATE
_READ_FRAMES = 4096
_DIAGNOSTIC_BYTES = 4096


class StreamDecoder:
    """Decode one file into a two-second, backpressured stereo PCM buffer.

    ``read`` never waits: it returns up to the requested number of available
    frames, including an empty array during underrun. One producer thread owns
    pipe reads; callers own concurrency between decoder instances.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        start_seconds: float = 0,
        filters: str | None = None,
        end_seconds: float | None = None,
        playback_rate: float = 1,
    ) -> None:
        if not math.isfinite(start_seconds) or start_seconds < 0:
            raise ValueError("start_seconds must be finite and non-negative")
        if end_seconds is not None and (
            not math.isfinite(end_seconds) or end_seconds <= start_seconds
        ):
            raise ValueError("end_seconds must be finite and greater than start_seconds")
        if not math.isfinite(playback_rate) or playback_rate <= 0:
            raise ValueError("playback_rate must be finite and positive")

        self.path = Path(path)
        self._condition = threading.Condition()
        self._chunks: deque[np.ndarray] = deque()
        self._head_offset = 0
        self._buffered_frames = 0
        self._eof = False
        self._error: str | None = None
        self._closed = False
        self._stderr = bytearray()
        self._process: Any = None
        self._producer: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None

        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            self._fail(f"FFmpeg is required to decode {self.path}")
            return
        command = [ffmpeg, "-v", "warning", "-nostdin", *SAFE_FFMPEG_INPUT]
        if start_seconds:
            command += ["-ss", f"{start_seconds:.9f}"]
        if end_seconds is not None:
            command += ["-t", f"{end_seconds - start_seconds:.9f}"]
        command += ["-i", str(self.path), "-map", "0:a:0"]
        filters_list = [filters] if filters else []
        if playback_rate != 1:
            filters_list += [
                f"aresample={SAMPLE_RATE},asetrate={SAMPLE_RATE * playback_rate:.9f},"
                f"aresample={SAMPLE_RATE}"
            ]
        if filters_list:
            command += ["-af", ",".join(filters_list)]
        command += ["-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1"]
        try:
            self._process = subprocess.Popen(  # nosec B603 -- fixed argv, no shell
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            bind_to_autodj(self._process)
        except Exception as exc:
            self._fail(f"Could not start FFmpeg for {self.path}: {exc}")
            self._reap_process()
            return
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr, name="autodj-decode-stderr", daemon=True
        )
        self._producer = threading.Thread(
            target=self._produce, name="autodj-stream-decode", daemon=True
        )
        try:
            self._stderr_reader.start()
            self._producer.start()
        except Exception as exc:
            self._fail(f"Could not start FFmpeg decoder workers for {self.path}: {exc}")
            self._reap_process()
            for thread in (self._stderr_reader, self._producer):
                if thread.ident is not None and thread is not threading.current_thread():
                    thread.join(timeout=2)

    @property
    def eof(self) -> bool:
        with self._condition:
            return self._eof

    @property
    def error(self) -> str | None:
        with self._condition:
            return self._error

    @property
    def buffered_frames(self) -> int:
        with self._condition:
            return self._buffered_frames

    def wait_ready(self, timeout: float | None = None) -> bool:
        """Wait off the audio thread until data, EOF, error or timeout."""
        with self._condition:
            self._condition.wait_for(
                lambda: self._buffered_frames > 0 or self._eof or self._closed,
                timeout=timeout,
            )
            return self._buffered_frames > 0

    def read(self, frames: int) -> np.ndarray:
        """Return up to ``frames`` currently buffered samples without waiting."""
        if frames < 0:
            raise ValueError("frames must be non-negative")
        with self._condition:
            count = min(frames, self._buffered_frames)
            result = np.empty((count, CHANNELS), dtype=np.float32)
            copied = 0
            while copied < count:
                chunk = self._chunks[0]
                available = len(chunk) - self._head_offset
                take = min(available, count - copied)
                result[copied : copied + take] = chunk[self._head_offset : self._head_offset + take]
                copied += take
                self._head_offset += take
                if self._head_offset == len(chunk):
                    self._chunks.popleft()
                    self._head_offset = 0
            self._buffered_frames -= count
            if count:
                self._condition.notify_all()
            return result

    def close(self) -> None:
        """Stop decoding and join both pipe readers; safe to call repeatedly."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._eof = True
            self._chunks.clear()
            self._buffered_frames = 0
            self._condition.notify_all()
        process = self._process
        self._reap_process()
        for pipe in (getattr(process, "stdout", None), getattr(process, "stderr", None)):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()
        for thread in (self._producer, self._stderr_reader):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5)
                if thread.is_alive():
                    raise RuntimeError(f"FFmpeg decoder worker did not stop: {self.path}")

    def _reap_process(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            logger.exception("Could not stop FFmpeg process for %s", self.path)

    def _fail(self, message: str) -> None:
        with self._condition:
            if self._error is None:
                self._error = message
            self._eof = True
            self._condition.notify_all()

    def _drain_stderr(self) -> None:
        pipe = self._process.stderr
        while True:
            block = pipe.read(1024)
            if not block:
                return
            with self._condition:
                self._stderr.extend(block)
                if len(self._stderr) > _DIAGNOSTIC_BYTES:
                    del self._stderr[: len(self._stderr) - _DIAGNOSTIC_BYTES]

    def _produce(self) -> None:
        pipe = self._process.stdout
        pending = bytearray()
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._closed or self._buffered_frames < _BUFFER_FRAMES
                    )
                    if self._closed:
                        return
                    capacity = _BUFFER_FRAMES - self._buffered_frames
                block = pipe.read(min(_READ_FRAMES, capacity) * _FRAME_BYTES - len(pending))
                if not block:
                    break
                pending.extend(block)
                usable = len(pending) // _FRAME_BYTES * _FRAME_BYTES
                if not usable:
                    continue
                samples = np.frombuffer(pending[:usable], dtype="<f4").reshape(-1, CHANNELS)
                if not np.isfinite(samples).all():
                    raise ValueError("FFmpeg produced non-finite PCM samples")
                samples = np.array(samples, dtype=np.float32, copy=True)
                del pending[:usable]
                with self._condition:
                    if self._closed:
                        return
                    self._chunks.append(samples)
                    self._buffered_frames += len(samples)
                    self._condition.notify_all()
            if pending:
                raise ValueError("FFmpeg produced truncated PCM samples")
            returncode = self._process.wait()
            if self._stderr_reader is not None:
                self._stderr_reader.join()
            detail = bytes(self._stderr).decode("utf-8", errors="replace").strip()
            if returncode:
                self._fail(f"FFmpeg could not decode {self.path}: {detail or 'unknown error'}")
            else:
                if detail:
                    logger.debug("FFmpeg decode diagnostic for %s: %s", self.path, detail)
                with self._condition:
                    if not self._closed:
                        self._eof = True
                        self._condition.notify_all()
        except Exception as exc:
            self._reap_process()
            self._fail(f"FFmpeg could not decode {self.path}: {exc}")
