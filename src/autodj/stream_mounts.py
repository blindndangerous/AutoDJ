"""A station's stream in every format: its MP3 stream plus AAC and Opus mounts (#50).

Each extra mount is its own :class:`~autodj.stream.StreamOutput` on the
station's mix bus: its own ffmpeg encoder, its own bounded PCM queue and
its own listeners, all fed the same mixed audio.  The bus hands every
output a block without waiting on it (``StreamOutput.write`` only queues
it, dropping the oldest block with a logged warning when an encoder falls
behind), so a slow or stalled mount never holds up the mix, the MP3
stream or the other mount.

A mount can be turned on, off or to another bitrate while the station
plays; only its own listeners reconnect.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

from autodj.stream_formats import EXTRA_FORMATS, FORMATS, MP3, StreamFormat

logger = logging.getLogger(__name__)


class StreamMounts:
    """The station's MP3 stream and whichever extra formats are on.

    Thread-safety: :meth:`set_bitrate` runs on request worker threads and
    is serialised by a lock; readers (:meth:`get`, the counts) take a
    snapshot of the mounts under it.
    """

    def __init__(
        self,
        main: Any,
        make_output: Callable[[StreamFormat, int], Any],
        attach: Callable[[Any], None],
        detach: Callable[[Any], None],
    ) -> None:
        """Hold *main*, the MP3 stream, with no extra mount yet.

        Args:
            main: The station's MP3 :class:`~autodj.stream.StreamOutput`.
            make_output: Starts an output (and its encoder) in a format at
                a bitrate.
            attach: Adds an output to the mix bus.
            detach: Takes an output off the mix bus.
        """
        self.main = main
        self._make = make_output
        self._attach = attach
        self._detach = detach
        self._lock = threading.Lock()
        self._extra: dict[str, Any] = {}
        self._title: tuple[str, str] | None = None

    def start(self, bitrates: Mapping[str, int]) -> None:
        """Start the extra mounts whose bitrate is not 0.

        A mount that cannot start is logged and left off; the MP3 stream
        and the other mount still start.

        Args:
            bitrates: Bitrate by format name; 0 or missing leaves it off.
        """
        for fmt in EXTRA_FORMATS:
            bitrate = int(bitrates.get(fmt.name, 0) or 0)
            if not bitrate:
                continue
            try:
                self.set_bitrate(fmt.name, bitrate)
            except Exception:
                logger.exception("Could not start the %s stream; it stays off", fmt.label)

    @property
    def outputs(self) -> list[Any]:
        """The extra mounts' outputs (not the MP3 stream)."""
        with self._lock:
            return list(self._extra.values())

    def get(self, name: str) -> Any | None:
        """The output serving format *name*, or ``None`` when that format is off."""
        if name == MP3.name:
            return self.main
        with self._lock:
            return self._extra.get(name)

    def bitrate(self, name: str) -> int:
        """Format *name*'s bitrate in kbps, 0 when it is off."""
        output = self.get(name)
        return int(output.bitrate) if output is not None else 0

    def counts(self) -> dict[str, int]:
        """Listeners on each format that is on, MP3 first."""
        with self._lock:
            extra = dict(self._extra)
        result = {MP3.name: int(self.main.listener_count)}
        for name, output in extra.items():
            result[name] = int(output.listener_count)
        return result

    @property
    def listener_count(self) -> int:
        """Listeners on every format together."""
        return sum(self.counts().values())

    def set_title(self, artist: str, title: str) -> None:
        """Retitle the extra mounts (the bridge retitles the MP3 stream)."""
        with self._lock:
            self._title = (artist, title)
            extra = list(self._extra.values())
        for output in extra:
            output.set_title(artist, title)

    def set_bitrate(self, name: str, bitrate: int) -> None:
        """Turn format *name* on at *bitrate*, or off with 0.

        A mount turned on starts its encoder before it joins the mix bus;
        one changed restarts its encoder (its listeners reconnect); one
        turned off leaves the bus, then disconnects its listeners and stops
        its encoder.

        Args:
            name: ``"aac"`` or ``"opus"``.
            bitrate: One of the format's bitrates, or 0 for off.

        Raises:
            ValueError: Not an extra format, or not one of its bitrates.
            EncoderUnavailableError: The encoder could not be started;
                nothing changed.
        """
        fmt = FORMATS.get(name)
        if fmt is None or fmt is MP3:
            raise ValueError(f"not an extra stream format: {name!r}")
        if bitrate and bitrate not in fmt.bitrates:
            raise ValueError(f"{fmt.label} bitrate must be 0 or one of {fmt.bitrates}")
        with self._lock:
            current = self._extra.get(name)
            if current is not None and bitrate == current.bitrate:
                return
            if current is not None and bitrate:
                current.set_bitrate(bitrate)
                logger.info("The %s stream now runs at %d kbps", fmt.label, bitrate)
                return
            if current is None and not bitrate:
                return
            if bitrate:
                output = self._make(fmt, bitrate)
                if self._title is not None:
                    output.set_title(*self._title)
                try:
                    self._attach(output)
                except BaseException:
                    output.close()
                    raise
                self._extra[name] = output
                logger.info("Started the %s stream at %d kbps", fmt.label, bitrate)
                return
            del self._extra[name]
        self._retire(fmt, current)
        logger.info("Stopped the %s stream", fmt.label)

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
        """Stop every extra mount (the MP3 stream is closed by its owner)."""
        with self._lock:
            extra = list(self._extra.items())
            self._extra.clear()
        for name, output in extra:
            self._retire(FORMATS[name], output)
