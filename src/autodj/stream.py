"""MP3 radio stream output: one encoder, many HTTP listeners.

:class:`StreamOutput` implements the mix bus's ``Output`` protocol: its
``write`` is called on the bus's pacing thread and must never block, even
if the encoder stalls or a listener stops reading.  PCM handed to
``write`` is queued and fed to the encoder by a dedicated writer thread;
a second reader thread pulls encoded MP3 bytes off the encoder and fans
them out to every listener's own bounded queue.  A listener that falls
behind is dropped rather than allowed to slow down the others.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import io
import logging
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Protocol, cast

import numpy as np

from autodj.icy import IcyInterleaver, format_stream_title, mp3_frame_offset
from autodj.stereo import SAMPLE_RATE

logger = logging.getLogger(__name__)

_BURST_SECONDS = 2.0
_QUEUE_SECONDS = 5.0
_FAILURE_LIMIT = 5
_FAILURE_WINDOW = 60.0
_COOLDOWN = 60.0
_BLOCK_SECONDS = 882 / SAMPLE_RATE
_PCM_QUEUE_SECONDS = 2.0


class ListenerLimitError(Exception):
    """The listener limit has been reached."""


class EncoderUnavailableError(Exception):
    """The encoder failed repeatedly and is cooling down."""


class Encoder(Protocol):
    """PCM-in, MP3-out encoder."""

    @property
    def alive(self) -> bool:
        """Whether the encoder process is still running."""
        raise NotImplementedError

    def write(self, pcm: bytes) -> None:
        """Feed raw f32le stereo PCM."""

    def read(self, n: int) -> bytes:
        """Read up to *n* encoded bytes; ``b""`` once the encoder has exited."""
        raise NotImplementedError

    def close_input(self) -> None:
        """Close the encoder's input so it flushes."""

    def close(self) -> None:
        """Stop the encoder and release resources."""


class _FfmpegEncoder:  # pragma: no cover -- exercised by tests/integration/test_stream_ffmpeg.py
    """ffmpeg subprocess encoder."""

    def __init__(self, bitrate: int) -> None:
        """Start an ffmpeg subprocess that encodes f32le stereo PCM to MP3.

        Args:
            bitrate: The MP3 bitrate in kbps.
        """
        self.bitrate = bitrate
        self._proc = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "f32le",
                "-ac",
                "2",
                "-ar",
                str(SAMPLE_RATE),
                "-i",
                "pipe:0",
                "-c:a",
                "libmp3lame",
                "-b:a",
                f"{bitrate}k",
                "-f",
                "mp3",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    @property
    def alive(self) -> bool:
        """Whether the ffmpeg process is still running."""
        return self._proc.poll() is None

    def write(self, pcm: bytes) -> None:
        """Write raw PCM to ffmpeg's stdin."""
        assert self._proc.stdin is not None
        self._proc.stdin.write(pcm)
        self._proc.stdin.flush()

    def read(self, n: int) -> bytes:
        """Read up to *n* encoded bytes from ffmpeg's stdout."""
        assert self._proc.stdout is not None
        stdout = cast(io.BufferedReader, self._proc.stdout)
        return stdout.read1(n)

    def close_input(self) -> None:
        """Close ffmpeg's stdin so it flushes and exits."""
        if self._proc.stdin and not self._proc.stdin.closed:
            self._proc.stdin.close()

    def close(self) -> None:
        """Terminate ffmpeg, then release its pipes once it is dead.

        Closing stdin *before* the process is dead can hang: a writer
        thread blocked inside ``stdin.write()`` on a stalled pipe holds
        the ``BufferedWriter``'s internal lock, and ``stdin.close()``
        waits on that same lock. Terminating (or killing) the process
        first unblocks any such write with a broken pipe, so the pipes
        can be closed safely afterwards.
        """
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            with contextlib.suppress(Exception):
                self._proc.kill()
            with contextlib.suppress(Exception):
                self._proc.wait(timeout=5)
        for pipe in (self._proc.stdin, self._proc.stdout):
            if pipe is not None:
                with contextlib.suppress(Exception):
                    pipe.close()


def ffmpeg_encoder(bitrate: int) -> Encoder:
    """Start a real ffmpeg MP3 encoder at *bitrate* kbps.

    Args:
        bitrate: The MP3 bitrate in kbps.

    Returns:
        A live :class:`Encoder` backed by an ffmpeg subprocess.
    """
    return _FfmpegEncoder(bitrate)  # pragma: no cover


class Listener:
    """One HTTP listener's bounded byte queue.

    ``push`` runs on the stream's reader thread; ``chunks`` runs on the
    asyncio event loop. A lock guards the deque and its size so the two
    threads never race, and ``call_soon_threadsafe`` is used for anything
    that touches the asyncio ``Event``.
    """

    def __init__(self, icy: bool, max_bytes: int, loop: asyncio.AbstractEventLoop) -> None:
        """Create a listener; *icy* adds ICY metadata to its bytes.

        Args:
            icy: Whether to interleave ICY metadata blocks into this
                listener's bytes.
            max_bytes: The queue size, in bytes, above which the listener
                is considered too far behind.
            loop: The asyncio event loop ``chunks`` will run on.
        """
        self._icy = IcyInterleaver() if icy else None
        self._max_bytes = max_bytes
        self._loop = loop
        self._lock = threading.Lock()
        self._queue: collections.deque[bytes] = collections.deque()
        self._size = 0
        self._event = asyncio.Event()
        self.closed = False

    def push(self, data: bytes, title: str) -> bool:
        """Queue *data*; returns ``False`` when the listener is too far behind.

        Args:
            data: The next chunk of encoded MP3 bytes.
            title: The current ICY stream title.

        Returns:
            ``True`` if the listener's queue is still within its bound,
            ``False`` if it should be dropped. Always ``False`` once the
            listener is closed (the data is discarded, not queued).
        """
        if self.closed:
            return False
        if self._icy is not None:
            data = self._icy.feed(data, title)
        with self._lock:
            self._queue.append(data)
            self._size += len(data)
            size = self._size
        self._loop.call_soon_threadsafe(self._event.set)
        return size <= self._max_bytes

    async def chunks(self) -> AsyncIterator[bytes]:
        """Yield queued bytes until the listener is closed and drained."""
        while True:
            while True:
                with self._lock:
                    if not self._queue:
                        break
                    data = self._queue.popleft()
                    self._size -= len(data)
                yield data
            if self.closed:
                return
            self._event.clear()
            await self._event.wait()

    def close(self) -> None:
        """Stop the listener after it drains what it has."""
        self.closed = True
        self._loop.call_soon_threadsafe(self._event.set)


class StreamOutput:
    """Encode mix-bus blocks once and fan the MP3 out to listeners."""

    def __init__(
        self,
        bitrate: int,
        max_listeners: int,
        encoder_factory: Callable[[int], Encoder] = ffmpeg_encoder,
        clock: Callable[[], float] = time.monotonic,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Start the encoder and its writer and reader threads.

        Args:
            bitrate: The initial MP3 bitrate in kbps.
            max_listeners: The maximum number of concurrent listeners.
            encoder_factory: Builds a fresh :class:`Encoder` for a given
                bitrate; defaults to the real ffmpeg encoder.
            clock: Monotonic time source, overridable for tests.
            loop: The asyncio event loop listeners run on. If omitted, the
                currently running event loop is used.

        Raises:
            RuntimeError: *loop* was omitted and there is no running event
                loop to fall back on.
        """
        if loop is not None:
            self._loop = loop
        else:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError as exc:
                raise RuntimeError(
                    "StreamOutput requires a running event loop; pass loop= "
                    "explicitly or construct it from inside an async context"
                ) from exc

        self._bitrate = bitrate
        self._max_listeners = max_listeners
        self._factory = encoder_factory
        self._clock = clock
        self._lock = threading.RLock()
        self._lock_depth = threading.local()
        self._listeners: list[Listener] = []
        self._burst: collections.deque[bytes] = collections.deque()
        self._burst_size = 0
        self._failures: collections.deque[float] = collections.deque()
        self._cooldown_until = 0.0
        self._pcm_in = 0
        self._mp3_out = 0
        self._title = ""
        self._pending_titles: collections.deque[tuple[int, str]] = collections.deque()
        self._closed = False
        self.on_listener_change: Callable[[int], None] | None = None

        self._notify_lock = threading.Lock()
        self._notify_due = False
        self._delivering = threading.local()

        self._pcm_queue_max = max(1, int(_PCM_QUEUE_SECONDS / _BLOCK_SECONDS))
        self._pcm_queue: collections.deque[bytes] = collections.deque()
        self._pcm_cond = threading.Condition()
        self._pcm_stall_logged = False
        self._pcm_write_problem_logged = False

        self._encoder: Encoder = self._factory(bitrate)
        self._reader = self._start_reader(self._encoder)
        self._writer = threading.Thread(
            target=self._write_loop, name="autodj-stream-writer", daemon=True
        )
        self._writer.start()

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Acquire ``self._lock``; deliver any due notification once released.

        Every internal critical section goes through this instead of
        ``self._lock`` directly. It is reentrant, like the ``RLock`` it
        wraps: a nested call (e.g. ``disconnect_all`` invoked from
        ``_restart_encoder``/``set_bitrate``/``close``) can mark a
        notification as due via ``self._notify_due`` but never delivers it
        itself. Only the call that is outermost on this thread delivers —
        and only after the lock has actually been released, never while
        any nesting level still holds it — so a slow
        ``on_listener_change`` callback can never stall some other thread
        that needs ``self._lock`` for something unrelated.
        """
        depth = getattr(self._lock_depth, "n", 0)
        self._lock.acquire()
        self._lock_depth.n = depth + 1
        try:
            yield
        finally:
            self._lock_depth.n = depth
            deliver = depth == 0 and self._notify_due
            self._lock.release()
            if deliver:
                self._deliver_notification()

    def _deliver_notification(self) -> None:
        """Tell ``on_listener_change`` the current listener count.

        Runs serialized under ``self._notify_lock`` — never while holding
        ``self._lock`` — so two deliveries can never interleave or run
        concurrently. Each iteration re-reads the count fresh, under
        ``self._lock``, from inside the notify lock (the acquisition order
        is always notify lock then main lock, never the reverse), so
        whichever iteration actually runs last always reports the true
        state as of that moment — there is no stale value to accidentally
        deliver out of order.

        The callback itself can safely call ``add_listener``/
        ``remove_listener``/``disconnect_all`` (e.g. to react to its own
        notification) without deadlocking: ``self._notify_lock`` is a
        plain, non-reentrant ``Lock``, so a naive reentrant call would
        block forever on itself. A thread-local "delivering" flag detects
        that case — the nested change just marks another notification due
        and returns immediately instead of trying to acquire the lock
        again — and the loop below picks it up once the in-flight callback
        returns, re-reading the count fresh and calling the callback
        again, until nothing is left due. This bounds the recursion to
        this method's own loop; it does not help a callback that blocks
        waiting on a *different* thread which itself needs to change the
        listener set (for example, calling
        ``asyncio.run_coroutine_threadsafe(coro, loop).result()`` and
        having that coroutine call back into ``add_listener``/
        ``remove_listener`` on the event loop) — that is a genuine
        cross-thread deadlock this method cannot detect, so callbacks must
        not block on another thread that might be adding or removing
        listeners.

        A callback that raises is logged and otherwise ignored: letting it
        propagate from here would surface it from whichever unrelated
        ``_locked()`` call happened to trigger delivery, potentially
        masking an exception that call itself was already raising, and
        would also skip releasing ``self._notify_lock``.
        """
        if getattr(self._delivering, "active", False):
            return
        self._notify_lock.acquire()
        try:
            self._delivering.active = True
            try:
                while True:
                    with self._lock:
                        if not self._notify_due:
                            break
                        self._notify_due = False
                        count = len(self._listeners)
                    callback = self.on_listener_change
                    if callback is None:
                        continue
                    try:
                        callback(count)
                    except Exception:
                        logger.exception("Stream listener-count callback raised")
            finally:
                self._delivering.active = False
        finally:
            self._notify_lock.release()

    # -- encoder lifecycle -------------------------------------------------

    def _start_reader(self, encoder: Encoder) -> threading.Thread:
        """Start the daemon thread that drains *encoder*'s output."""
        thread = threading.Thread(
            target=self._read_loop, args=(encoder,), name="autodj-stream-reader", daemon=True
        )
        thread.start()
        return thread

    def _record_failure(self) -> None:
        """Note an encoder failure and start a cooldown after too many."""
        now = self._clock()
        self._failures.append(now)
        while self._failures and now - self._failures[0] > _FAILURE_WINDOW:
            self._failures.popleft()
        if len(self._failures) >= _FAILURE_LIMIT:
            self._cooldown_until = now + _COOLDOWN
            self._failures.clear()
            logger.error(
                "Stream encoder failed %d times in a minute; pausing it for 60 s", _FAILURE_LIMIT
            )

    def _flush_pending_titles(self) -> None:
        """Apply the latest pending title and drop the rest.

        Pending title marks are positions in the *old* encoder's byte
        stream. Once that encoder is replaced, ``_pcm_in``/``_mp3_out``
        reset to 0, so a stale mark (usually far larger than anything the
        fresh encoder will ever produce) would sit at the front of the
        queue and block every later title from ever taking effect. The
        most recently requested title is the one that should be showing
        once the new encoder starts producing output, so apply it now.
        """
        if self._pending_titles:
            self._title = self._pending_titles[-1][1]
            self._pending_titles.clear()

    def _restart_encoder(self, dead: Encoder | None = None) -> None:
        """Replace *dead* with a fresh encoder, counting the failure.

        Args:
            dead: The encoder that exited or is being retired. Defaults to
                whatever is currently active. If it is no longer the
                current encoder (another restart already replaced it),
                this is a no-op: acting on a stale reference here would
                double-count the failure and could stomp a newer encoder.
        """
        with self._locked():
            current = self._encoder
            if dead is None:
                dead = current
            if dead is not current:
                return
            self._record_failure()
            self.disconnect_all()
            try:
                self._encoder.close()
            except Exception:
                logger.debug("Closing the failed encoder raised", exc_info=True)
            if self._clock() < self._cooldown_until or self._closed:
                return
            try:
                new_encoder = self._factory(self._bitrate)
            except Exception:
                logger.error(
                    "Stream encoder factory failed; will retry on the next "
                    "restart or listener connection",
                    exc_info=True,
                )
                self._record_failure()
                return
            self._encoder = new_encoder
            self._flush_pending_titles()
            self._pcm_in = self._mp3_out = 0
            self._reader = self._start_reader(self._encoder)

    def _read_loop(self, encoder: Encoder) -> None:
        """Drain *encoder*'s MP3 output and restart it if it exits."""
        while True:
            data = encoder.read(4096)
            if not data:
                break
            self._on_encoded(encoder, data)
        if not self._closed and encoder is self._encoder:
            logger.warning("Stream encoder exited; restarting")
            self._restart_encoder(encoder)

    def _log_write_problem_once(self, message: str) -> None:
        """Log *message* once per stall episode, then stay quiet.

        Args:
            message: The warning to log the first time in an episode.
        """
        if not self._pcm_write_problem_logged:
            logger.warning(message)
            self._pcm_write_problem_logged = True

    def _write_loop(self) -> None:
        """Feed queued PCM to the current encoder, one block at a time."""
        while True:
            with self._pcm_cond:
                while not self._pcm_queue and not self._closed:
                    self._pcm_cond.wait(0.5)
                if not self._pcm_queue:
                    return  # closed with nothing left to flush
                pcm = self._pcm_queue.popleft()
            with self._locked():
                encoder = self._encoder
            if not encoder.alive:
                self._log_write_problem_once("Stream encoder is not alive; dropping queued audio")
                continue
            try:
                encoder.write(pcm)
            except (BrokenPipeError, OSError, ValueError):
                self._log_write_problem_once("Stream encoder rejected a write; it may have exited")
                continue
            self._pcm_write_problem_logged = False
            with self._locked():
                self._pcm_in += len(pcm)

    # -- data path ---------------------------------------------------------

    def write(self, block: np.ndarray) -> None:
        """Queue one mix-bus block for the encoder without blocking.

        A no-op once the stream is closed.

        Args:
            block: A ``(882, 2)`` float32 PCM block from the mix bus.
        """
        if self._closed:
            return
        pcm = np.ascontiguousarray(block, dtype=np.float32).tobytes()
        with self._pcm_cond:
            if len(self._pcm_queue) >= self._pcm_queue_max:
                self._pcm_queue.popleft()
                if not self._pcm_stall_logged:
                    logger.warning(
                        "Stream encoder is falling behind; dropping the oldest queued audio block"
                    )
                    self._pcm_stall_logged = True
            else:
                self._pcm_stall_logged = False
            self._pcm_queue.append(pcm)
            self._pcm_cond.notify()

    def set_title(self, artist: str, title: str) -> None:
        """Switch the ICY title once audio written from now reaches listeners.

        Args:
            artist: The track artist.
            title: The track title.
        """
        with self._locked():
            self._pending_titles.append((self._pcm_in, format_stream_title(artist, title)))

    def _on_encoded(self, encoder: Encoder, data: bytes) -> None:
        """Advance the title cursor and burst buffer, then fan *data* out.

        Args:
            encoder: The encoder *data* came from. Dropped without effect
                if it is no longer the current encoder: a superseded
                encoder's reader thread can still be draining trailing
                output after :meth:`set_bitrate` or a restart swapped in a
                new one, and that stale data must not corrupt the new
                encoder's byte-position bookkeeping.
            data: The newly encoded MP3 bytes.
        """
        with self._locked():
            if encoder is not self._encoder:
                return
            self._mp3_out += len(data)
            ratio = (self._bitrate * 1000 / 8) / (SAMPLE_RATE * 2 * 4)
            while self._pending_titles and self._mp3_out >= self._pending_titles[0][0] * ratio:
                self._title = self._pending_titles.popleft()[1]
            burst_limit = int(self._bitrate * 1000 / 8 * _BURST_SECONDS)
            self._burst.append(data)
            self._burst_size += len(data)
            while self._burst and self._burst_size - len(self._burst[0]) >= burst_limit:
                self._burst_size -= len(self._burst.popleft())
            listeners = list(self._listeners)
            title = self._title
        for listener in listeners:
            if not listener.push(data, title):
                logger.info("Dropping a stream listener that fell behind")
                self.remove_listener(listener)

    # -- listeners ---------------------------------------------------------

    @property
    def listener_count(self) -> int:
        """Current number of listeners."""
        with self._locked():
            return len(self._listeners)

    def add_listener(self, icy: bool) -> Listener:
        """Register a listener and give it the recent burst.

        Args:
            icy: Whether the listener wants ICY metadata interleaved.

        Returns:
            The new :class:`Listener`.

        Raises:
            ListenerLimitError: At the listener limit.
            EncoderUnavailableError: The stream is closed, the encoder is
                cooling down, or a dead encoder could not be replaced.
        """
        with self._locked():
            if self._closed:
                raise EncoderUnavailableError("stream output is closed")
            if self._clock() < self._cooldown_until:
                raise EncoderUnavailableError("stream encoder failed")
            if not self._encoder.alive:
                dead = self._encoder
                try:
                    self._encoder = self._factory(self._bitrate)
                except Exception as exc:
                    self._record_failure()
                    raise EncoderUnavailableError("stream encoder failed") from exc
                try:
                    dead.close()
                except Exception:
                    logger.debug("Closing the dead encoder raised", exc_info=True)
                self._flush_pending_titles()
                self._pcm_in = self._mp3_out = 0
                self._reader = self._start_reader(self._encoder)
            if len(self._listeners) >= self._max_listeners:
                raise ListenerLimitError("listener limit reached")
            max_bytes = int(self._bitrate * 1000 / 8 * _QUEUE_SECONDS)
            listener = Listener(icy, max_bytes, self._loop)
            burst = b"".join(self._burst)
            start = mp3_frame_offset(burst)
            if start >= 0:
                listener.push(burst[start:], self._title)
            self._listeners.append(listener)
            self._notify_due = True
        return listener

    def remove_listener(self, listener: Listener) -> None:
        """Unregister and close *listener*; a no-op if already removed.

        Args:
            listener: The listener to remove.
        """
        with self._locked():
            if listener not in self._listeners:
                return
            self._listeners.remove(listener)
            listener.close()
            self._notify_due = True

    def disconnect_all(self) -> None:
        """Close every listener."""
        with self._locked():
            for listener in list(self._listeners):
                listener.close()
            self._listeners.clear()
            self._notify_due = True

    def set_bitrate(self, bitrate: int) -> None:
        """Restart the encoder at *bitrate*; listeners reconnect on their own.

        The replacement encoder is built first: if the factory raises,
        the current bitrate, encoder and listeners are all left exactly
        as they were and the failure is surfaced to the caller instead of
        silently disconnecting everyone and then failing.

        Args:
            bitrate: The new MP3 bitrate in kbps.

        Raises:
            EncoderUnavailableError: The replacement encoder could not be
                built.
        """
        with self._locked():
            try:
                new_encoder = self._factory(bitrate)
            except Exception as exc:
                raise EncoderUnavailableError("stream encoder failed") from exc
            self._bitrate = bitrate
            self.disconnect_all()
            old = self._encoder
            self._encoder = new_encoder
            self._flush_pending_titles()
            self._pcm_in = self._mp3_out = 0
            self._burst.clear()
            self._burst_size = 0
            self._reader = self._start_reader(self._encoder)
        old.close()

    def close(self) -> None:
        """Disconnect listeners and stop the encoder and its threads."""
        with self._locked():
            self._closed = True
            self.disconnect_all()
            encoder = self._encoder
            reader = self._reader
        with self._pcm_cond:
            self._pcm_cond.notify_all()
        try:
            encoder.close()
        except Exception:
            logger.debug("Closing the stream encoder raised", exc_info=True)
        self._writer.join(timeout=5)
        if self._writer.is_alive():
            logger.debug("Stream writer thread did not stop within the close timeout")
        reader.join(timeout=5)
        if reader.is_alive():
            logger.debug("Stream reader thread did not stop within the close timeout")
