"""A station's stream in every format: its MP3 stream plus AAC and Opus mounts (#50).

Each extra mount is its own :class:`~autodj.stream.StreamOutput` on the
station's mix bus: its own ffmpeg encoder, its own bounded PCM queue and
its own listeners, all fed the same mixed audio.  The bus hands every
output a block without waiting on it (``StreamOutput.write`` only queues
it, dropping the oldest block with a logged warning when an encoder falls
behind), so a slow or stalled mount never holds up the mix, the MP3
stream or the other mount.

A format that is on runs its encoder only while someone listens to it:
the first listener starts it (:meth:`StreamMounts.acquire`, off the event
loop), and :meth:`StreamMounts.tick` stops it once nobody has listened
for :data:`IDLE_SECONDS`.  So a format nobody uses costs no CPU.  The MP3
stream is always on, as before.

Locks: ``_lock`` only guards the table of mounts and the title, and is
never held while ffmpeg starts or stops, so :meth:`StreamMounts.set_title`
(on the mix thread) and :meth:`StreamMounts.get` (on the event loop) never
wait on an encoder.  ``_start_lock`` lets one start happen at a time.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from autodj.stream_formats import EXTRA_FORMATS, FORMATS, MP3, StreamFormat

logger = logging.getLogger(__name__)

#: Seconds an extra format's encoder keeps running after its last listener left.
IDLE_SECONDS = 60.0


class MountsClosedError(Exception):
    """The station's stream has stopped; no format can start."""


@dataclass
class _Mount:
    """One extra format that is on: its bitrate and, while heard, its output."""

    fmt: StreamFormat
    bitrate: int
    output: Any = None
    heard_at: float = 0.0


class StreamMounts:
    """The station's MP3 stream and whichever extra formats are on."""

    def __init__(
        self,
        main: Any,
        make_output: Callable[[StreamFormat, int], Any],
        attach: Callable[[Any], None],
        detach: Callable[[Any], None],
        probe: Callable[[StreamFormat, int], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Hold *main*, the MP3 stream, with every extra format off.

        Args:
            main: The station's MP3 :class:`~autodj.stream.StreamOutput`.
            make_output: Starts an output (and its encoder) in a format at
                a bitrate.
            attach: Adds an output to the mix bus.
            detach: Takes an output off the mix bus.
            probe: Checks once that ffmpeg can encode a format at a bitrate,
                raising with ffmpeg's reason when it cannot.
            clock: Monotonic seconds, for the idle timeout.
        """
        self.main = main
        self._make = make_output
        self._attach = attach
        self._detach = detach
        self._probe = probe
        self._clock = clock
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._mounts: dict[str, _Mount] = {}
        self._title: tuple[str, str] | None = None
        self._closed = False

    # -- reading (never waits on an encoder) -----------------------------------

    def _snapshot(self) -> dict[str, _Mount]:
        with self._lock:
            return {name: replace(mount) for name, mount in self._mounts.items()}

    @property
    def outputs(self) -> list[Any]:
        """The extra formats' running outputs (not the MP3 stream)."""
        return [m.output for m in self._snapshot().values() if m.output is not None]

    def enabled(self, name: str) -> bool:
        """Whether format *name* is on (MP3 always is), running or not."""
        if name == MP3.name:
            return True
        with self._lock:
            return name in self._mounts

    def get(self, name: str) -> Any | None:
        """The running output serving format *name*; ``None`` when off or not running."""
        if name == MP3.name:
            return self.main
        with self._lock:
            mount = self._mounts.get(name)
            return mount.output if mount is not None else None

    def bitrate(self, name: str) -> int:
        """Format *name*'s bitrate in kbps, 0 when it is off."""
        if name == MP3.name:
            return int(self.main.bitrate)
        with self._lock:
            mount = self._mounts.get(name)
            return mount.bitrate if mount is not None else 0

    def last_error(self, name: str) -> str | None:
        """The last thing format *name*'s running encoder said on stderr."""
        output = self.get(name)
        error = getattr(output, "last_error", None) if output is not None else None
        return error if isinstance(error, str) else None

    def counts(self) -> dict[str, int]:
        """Listeners on each format that is on, MP3 first."""
        result = {MP3.name: int(self.main.listener_count)}
        for name, mount in self._snapshot().items():
            result[name] = int(mount.output.listener_count) if mount.output is not None else 0
        return result

    @property
    def listener_count(self) -> int:
        """Listeners on every format together."""
        return sum(self.counts().values())

    def set_title(self, artist: str, title: str) -> None:
        """Retitle the running extra formats (the bridge retitles MP3); never blocks the mix."""
        with self._lock:
            self._title = (artist, title)
            outputs = [m.output for m in self._mounts.values() if m.output is not None]
        for output in outputs:
            output.set_title(artist, title)

    # -- turning formats on and off --------------------------------------------

    def _checked(self, changes: Mapping[str, int]) -> dict[str, StreamFormat]:
        formats = {}
        for name, bitrate in changes.items():
            fmt = FORMATS.get(name)
            if fmt is None or fmt is MP3:
                raise ValueError(f"not an extra stream format: {name!r}")
            if bitrate and bitrate not in fmt.bitrates:
                raise ValueError(f"{fmt.label} bitrate must be 0 or one of {fmt.bitrates}")
            formats[name] = fmt
        return formats

    def start(self, bitrates: Mapping[str, int]) -> None:
        """Turn on the extra formats whose bitrate is not 0, at the station's start.

        A format ffmpeg cannot encode is logged and left off; the MP3 stream
        and the other format still start.
        """
        for fmt in EXTRA_FORMATS:
            bitrate = int(bitrates.get(fmt.name, 0) or 0)
            if not bitrate:
                continue
            try:
                self.apply({fmt.name: bitrate})
            except Exception:
                logger.exception("Could not turn on the %s stream; it stays off", fmt.label)

    def set_bitrate(self, name: str, bitrate: int) -> None:
        """Turn format *name* on at *bitrate*, or off with 0 (see :meth:`apply`)."""
        self.apply({name: bitrate})

    def apply(self, changes: Mapping[str, int]) -> list[str]:
        """Turn formats on, off or to another bitrate: all of them or none.

        Every change is checked, and every format turned on or to a new
        bitrate is probed, before anything changes.  Then the table changes
        at once; an encoder running at the old bitrate, or for a format now
        off, stops, and its listeners reconnect.  Nothing here starts an
        encoder: the next listener does.

        Args:
            changes: Bitrate by format name (``"aac"``, ``"opus"``); 0 is off.

        Returns:
            The names of the formats that changed.

        Raises:
            ValueError: Not an extra format, or not one of its bitrates.
            EncoderUnavailableError: ffmpeg cannot encode one of them;
                nothing changed.
            MountsClosedError: The station's stream has stopped.
        """
        formats = self._checked(changes)
        with self._lock:
            if self._closed:
                raise MountsClosedError("the station's stream has stopped")
            wanted = {
                name: bitrate
                for name, bitrate in changes.items()
                if bitrate != (self._mounts[name].bitrate if name in self._mounts else 0)
            }
        if self._probe is not None:
            for name, bitrate in wanted.items():
                if bitrate:
                    self._probe(formats[name], bitrate)
        retired: list[tuple[StreamFormat, Any]] = []
        with self._lock:
            if self._closed:
                raise MountsClosedError("the station's stream has stopped")
            for name, bitrate in wanted.items():
                old = self._mounts.pop(name, None)
                if old is not None and old.output is not None:
                    retired.append((old.fmt, old.output))
                if bitrate:
                    self._mounts[name] = _Mount(formats[name], bitrate, heard_at=self._clock())
        for fmt, output in retired:
            self._retire(fmt, output)
        for name, bitrate in wanted.items():
            if bitrate:
                logger.info("The %s stream is on at %d kbps", formats[name].label, bitrate)
            else:
                logger.info("The %s stream is off", formats[name].label)
        return list(wanted)

    def acquire(self, name: str) -> Any | None:
        """The output serving format *name*, starting its encoder if it is not running.

        Call off the event loop: starting spawns ffmpeg.

        Returns:
            ``None`` when the format is off or the station's stream has stopped.
        """
        if name == MP3.name:
            return self.main
        with self._lock:
            mount = self._mounts.get(name)
            if mount is None or self._closed:
                return None
            mount.heard_at = self._clock()
            if mount.output is not None:
                return mount.output
        with self._start_lock:
            with self._lock:
                mount = self._mounts.get(name)
                if mount is None or self._closed:
                    return None
                if mount.output is not None:
                    return mount.output
                fmt, bitrate, title = mount.fmt, mount.bitrate, self._title
            output = self._make(fmt, bitrate)
            if title is not None:
                output.set_title(*title)
            try:
                self._attach(output)
            except BaseException:
                output.close()
                raise
            with self._lock:
                installed = not self._closed and self._mounts.get(name) is mount
                if installed:
                    mount.output = output
                    mount.heard_at = self._clock()
                latest = self._title
            if installed:
                if latest is not None and latest != title:
                    # The song changed while ffmpeg started.
                    output.set_title(*latest)
                logger.info("Started the %s encoder for a listener", fmt.label)
                return output
        # Turned off, changed or stopped while ffmpeg started: no orphan.
        self._retire(fmt, output)
        return None

    def tick(self) -> None:
        """Stop the encoders nobody has listened to for :data:`IDLE_SECONDS` (worker thread)."""
        now = self._clock()
        idle: list[tuple[StreamFormat, Any]] = []
        with self._lock:
            for mount in self._mounts.values():
                output = mount.output
                if output is None:
                    continue
                if output.listener_count > 0:
                    mount.heard_at = now
                elif now - mount.heard_at >= IDLE_SECONDS:
                    idle.append((mount.fmt, output))
                    mount.output = None
        for fmt, output in idle:
            logger.info("Stopped the %s encoder: nobody is listening", fmt.label)
            self._retire(fmt, output)

    def _retire(self, fmt: StreamFormat, output: Any) -> None:
        """Take *output* off the bus, then close it; each step runs even if one fails."""
        steps: tuple[tuple[str, Callable[[], None]], ...] = (
            ("detaching", lambda: self._detach(output)),
            ("closing", output.close),
        )
        for what, step in steps:
            try:
                step()
            except Exception:
                logger.exception("Stopping the %s stream: %s it failed", fmt.label, what)

    def close(self) -> None:
        """Stop every extra format for good (the MP3 stream is closed by its owner)."""
        with self._lock:
            self._closed = True
            mounts = list(self._mounts.values())
            self._mounts.clear()
        for mount in mounts:
            if mount.output is not None:
                self._retire(mount.fmt, mount.output)
