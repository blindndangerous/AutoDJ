"""44.1 kHz stereo float32 input with fixed storage and consumption credit."""

import struct
import threading
import time
from collections import deque

import numpy as np

from autodj.local_programme import Grant
from autodj.local_programme_events import ProgrammeEvents

RATE = 44100
PACKET_FRAMES = 441
CAPACITY = 8820
PREFILL = 2646
HEADER = struct.Struct("<4sBBHQQQII8s")
PACKET_BYTES = HEADER.size + PACKET_FRAMES * 8


class PCMError(ValueError):
    """Reject a protocol violation without admitting any of its samples."""

    def __init__(self, message: str, code: int = 1003) -> None:
        super().__init__(message)
        self.code = code


class LocalPCM:
    """One generation's bounded ring. Lock order: controller/bus, then PCM.

    The reader never calls into ownership or the bus while holding this lock.
    Revocation closes and clears this object before releasing its reservation.
    """

    def __init__(self, grant: Grant, *, now: float | None = None) -> None:
        self.grant = grant
        self.clock = time.monotonic
        now = self.clock() if now is None else now
        self._lock = threading.RLock()
        self._ring = np.empty((CAPACITY, 2), np.float32)
        self._read = 0
        self._count = 0
        self._spans: deque[tuple[int, int]] = deque()
        self.next_sequence = 0
        self.high_water_frames = 0
        self._armed = False
        self._started = False
        self._failed = ""
        self._last_pcm = now
        self._last_budget = now
        self._packets = float(CAPACITY // PACKET_FRAMES)
        self._bytes = float(PACKET_BYTES * (CAPACITY // PACKET_FRAMES))
        self._recent_packets: deque[float] = deque()
        self._short = 0
        self.events = ProgrammeEvents()
        self._arm_frame = 0
        self._consumed_frame = 0
        self._occupancy_since: float | None = None

    @property
    def consumed_frame(self) -> int:
        with self._lock:
            return self._consumed_frame

    @property
    def limit_frame(self) -> int:
        """Cumulative credit, read atomically with the accepted frame cursor."""
        with self._lock:
            return self.next_sequence * PACKET_FRAMES + CAPACITY - self._count

    @property
    def ready(self) -> bool:
        with self._lock:
            return not self._failed and self._count >= PREFILL

    @property
    def failed(self) -> str:
        with self._lock:
            return self._failed

    def arm(self, first_frame: int | None = None) -> None:
        """Internal boundary for fixtures; no route exposes this before accounting."""
        with self._lock:
            if self._failed:
                raise PCMError(self._failed)
            if first_frame is not None:
                if type(first_frame) is not int or not (
                    self.next_sequence * PACKET_FRAMES
                    <= first_frame
                    <= self.next_sequence * PACKET_FRAMES + RATE
                ):
                    raise PCMError("Invalid future programme boundary")
                self._arm_frame = first_frame
            self._armed = True

    def start(self) -> None:
        with self._lock:
            if not self.ready:
                raise PCMError("Local programme needs 60 ms prefill")
            self._started = True

    def close(self, reason: str) -> None:
        with self._lock:
            self._failed = reason
            self._count = 0
            self._spans.clear()
            self.events.cancel()

    def check(self, now: float | None = None) -> bool:
        with self._lock:
            now = self.clock() if now is None else now
            if now >= self._last_pcm + 0.5:
                self.close("Local programme capture stalled")
            if self._started and not self._failed:
                if 882 <= self._count <= 7056:
                    self._occupancy_since = None
                elif self._occupancy_since is None:
                    self._occupancy_since = now
                elif now - self._occupancy_since >= 5:
                    self.close("Local programme clock drift exceeded the occupancy window")
            return not self._failed

    def accept_packet(self, grant: Grant, packet: bytes, *, now: float | None = None) -> None:
        with self._lock:
            now = self.clock() if now is None else now
            if grant != self.grant:
                raise PCMError("Stale local programme grant")
            if not self.check(now):
                raise PCMError(self._failed)
            if len(packet) != PACKET_BYTES:
                raise PCMError(
                    "Expected one 441-frame stereo packet",
                    1009 if len(packet) > PACKET_BYTES else 1003,
                )
            magic, version, flags, channels, generation, sequence, first, count, rate, reserved = (
                HEADER.unpack_from(packet)
            )
            if (
                magic != b"ADJP"
                or version != 1
                or flags
                or channels != 2
                or generation != grant.generation
                or sequence != self.next_sequence
                or first != sequence * PACKET_FRAMES
                or count != PACKET_FRAMES
                or rate != RATE
                or reserved != bytes(8)
            ):
                raise PCMError("Invalid local programme header or sequence")
            pcm = np.frombuffer(packet, dtype="<f4", offset=HEADER.size).reshape((-1, 2))
            if not np.isfinite(pcm).all() or np.max(np.abs(pcm)) > 16:
                raise PCMError("Local programme samples must be finite and within headroom")
            before = count if not self._armed else min(count, max(0, self._arm_frame - first))
            if pcm[:before].any():
                raise PCMError("Only zero PCM is allowed during preparation")
            if count > CAPACITY - self._count:
                raise PCMError(
                    "Local programme credit exhausted (clock drift or stalled reader)", 1009
                )
            elapsed = max(0, now - self._last_budget)
            # ASGI delivery can bunch correctly paced messages after a scheduler
            # delay. Allow one existing PCM capacity of burst headroom, while a
            # separate rolling window retains the original one-second budget.
            burst = CAPACITY // PACKET_FRAMES
            packets = min(float(burst), self._packets + elapsed * 125)
            byte_budget = min(PACKET_BYTES * burst, self._bytes + elapsed * RATE * 8 * 1.25)
            while self._recent_packets and self._recent_packets[0] <= now - 1:
                self._recent_packets.popleft()
            window_packets = len(self._recent_packets) + 1
            if (
                packets < 1
                or byte_budget < len(packet)
                or window_packets > 127
                or window_packets * PACKET_BYTES > RATE * 8 * 1.25 + PACKET_BYTES * 2
            ):
                raise PCMError("Local programme rate limit exceeded", 1009)
            self._packets, self._bytes = packets - 1, byte_budget - len(packet)
            self._recent_packets.append(now)
            self._last_budget = self._last_pcm = now
            self.next_sequence += 1
            if before:
                # Preparation PCM is verified silence. In particular, a failed
                # load must complete and release its reservation before arming.
                self.events.consume([(first, before)])
            if before == count:
                return
            pcm = pcm[before:]
            count -= before
            first += before
            write = (self._read + self._count) % CAPACITY
            tail = min(count, CAPACITY - write)
            self._ring[write : write + tail] = pcm[:tail]
            self._ring[: count - tail] = pcm[tail:]
            self._count += count
            self.high_water_frames = max(self.high_water_frames, self._count)
            self._spans.append((first, count))

    def read_block(
        self, generation: int, frames: int = 882, *, now: float | None = None
    ) -> tuple[np.ndarray, list[tuple[int, int]]]:
        if not 1 <= frames <= 882:
            raise ValueError("Read at most one bus block")
        block = np.zeros((frames, 2), np.float32)
        consumed: list[tuple[int, int]] = []
        with self._lock:
            if generation != self.grant.generation or not self.check(now):
                return block, consumed
            count = min(frames, self._count)
            tail = min(count, CAPACITY - self._read)
            block[:tail] = self._ring[self._read : self._read + tail]
            block[tail:count] = self._ring[: count - tail]
            self._read = (self._read + count) % CAPACITY
            self._count -= count
            remaining = count
            while remaining:
                first, length = self._spans.popleft()
                take = min(remaining, length)
                consumed.append((first, take))
                self._consumed_frame = first + take
                if take < length:
                    self._spans.appendleft((first + take, length - take))
                remaining -= take
            try:
                self.events.consume(consumed)
            except ValueError as exc:
                self.close(str(exc))
                block.fill(0)
                return block, []
            if self._started:
                self._short = self._short + 1 if count < frames else 0
                if self._short >= 3:
                    self.close("Local programme underrun (clock drift or stalled capture)")
        return block, consumed
