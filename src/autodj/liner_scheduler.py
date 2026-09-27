"""Server-side voice-liner scheduling for server-mixed playback.

The browser evaluates liner triggers locally when it owns the audio
element (``autodj/static/modules/liners.js``).  In server-mixed modes
(``--server-audio``) the :class:`~autodj.mixbus.MixBus` owns the audio
clock instead, so the same trigger/pick logic needs a server-side home
that can decode a clip and hand it to the bus. This module is that home.

:class:`LinerScheduler` wraps :class:`~autodj.liners.LinerTrigger` and
:class:`~autodj.liners.LinerLibrary` — reusing their tested trigger and
rotation semantics — and adds the one thing they intentionally do not
do: decoding a file to the bus's stereo float32 format and calling
:meth:`~autodj.mixbus.MixBus.play_liner`.
"""

from __future__ import annotations

import io
import logging
import random
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from autodj.liner_files import open_liner_file
from autodj.liners import LinerLibrary, LinerTrigger
from autodj.stereo import SAMPLE_RATE, to_stereo

logger = logging.getLogger(__name__)


class LinerDecodeError(Exception):
    """A liner file could not be decoded by either soundfile or ffmpeg."""


def decode_liner(
    root: Path,
    name: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> np.ndarray:
    """Decode liner *name* from *root* to stereo float32 at 44.1 kHz.

    Reads the file through :func:`~autodj.liner_files.open_liner_file`
    (which bounds *name* to a single plain filename with a known liner
    extension inside *root*), then tries :mod:`soundfile` on the bytes
    first and falls back to an ``ffmpeg`` subprocess for formats
    soundfile cannot read.

    Args:
        root: Liner folder.
        name: Plain liner file name (as listed by
            :meth:`~autodj.liners.LinerLibrary.from_folder`).
        runner: ``subprocess.run`` replacement for tests.

    Returns:
        ``(frames, 2)`` float32 audio at :data:`~autodj.stereo.SAMPLE_RATE`.

    Raises:
        LinerDecodeError: If neither soundfile nor ffmpeg can decode it.
    """
    opened = open_liner_file(root, name)
    try:
        data = opened.file.read()
    finally:
        opened.file.close()
    try:
        audio, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
        audio = to_stereo(audio)
        if int(sr) != SAMPLE_RATE:
            import librosa

            audio = librosa.resample(audio, orig_sr=int(sr), target_sr=SAMPLE_RATE, axis=0)
        return to_stereo(audio)
    except Exception:
        result = runner(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                "pipe:0",
                "-f",
                "f32le",
                "-ac",
                "2",
                "-ar",
                str(SAMPLE_RATE),
                "pipe:1",
            ],
            input=data,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0 or not result.stdout:
            raise LinerDecodeError(f"cannot decode liner {name!r}") from None
        return np.frombuffer(result.stdout, dtype=np.float32).reshape(-1, 2).copy()


class LinerScheduler:
    """Fire voice liners into the mix bus on the configured triggers.

    Owns a :class:`~autodj.liners.LinerLibrary` (refreshed from disk on
    every automatic pick, so uploads/deletes take effect without a
    restart) and rebuilds a :class:`~autodj.liners.LinerTrigger` from
    the live config on every evaluation, so config changes applied
    through the settings API take effect on the next track/tick without
    recreating the scheduler.

    A liner that fails to decode is logged and skipped — it never
    reaches :meth:`~autodj.mixbus.MixBus.play_liner`, so a bad or
    unreachable clip cannot interrupt the music.
    """

    def __init__(
        self,
        playback_cfg: Any,
        folder: Path,
        bus: Any,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
        decoder: Callable[[Path, str], np.ndarray] = decode_liner,
    ) -> None:
        """Build a scheduler.

        Args:
            playback_cfg: ``cfg.playback`` (or an equivalent namespace)
                exposing the ``liners_*`` settings.
            folder: Liner folder.
            bus: Mix bus with ``play_liner(audio, duck_db=...)``.
            clock: Monotonic seconds; defaults to :func:`time.monotonic`.
            rng: Random source for picks and random-window rolls;
                defaults to a new :class:`random.Random`.
            decoder: Liner decoder; defaults to :func:`decode_liner`.
        """
        self._cfg = playback_cfg
        self._folder = folder
        self._bus = bus
        self._clock = clock
        self._rng = rng or random.Random()
        self._decoder = decoder
        self._tracks_since = 0
        self._last_fire = clock()
        self._library = LinerLibrary.from_folder(folder)
        self._random_target = self._trigger().roll_random_target(rng=self._rng)

    def _trigger(self) -> LinerTrigger:
        """Build a :class:`LinerTrigger` from the current config values."""
        return LinerTrigger(
            every_n_songs=self._cfg.liners_every_n_songs,
            every_minutes=self._cfg.liners_every_minutes,
            random_min_minutes=self._cfg.liners_random_min_minutes,
            random_max_minutes=self._cfg.liners_random_max_minutes,
            enabled=bool(self._cfg.liners_enabled),
        )

    def _maybe_fire(self) -> None:
        """Fire a liner when the current trigger config says it is due."""
        minutes = (self._clock() - self._last_fire) / 60.0
        if self._trigger().should_fire(
            track_count=self._tracks_since,
            minutes_since_last=minutes,
            random_target_minutes=self._random_target,
        ):
            self.fire()

    def on_track_start(self) -> None:
        """Count a track advance and fire a liner if a trigger is due."""
        self._tracks_since += 1
        self._maybe_fire()

    def tick(self) -> None:
        """Check timed triggers; call about once a second."""
        self._maybe_fire()

    def fire(self, name: str | None = None) -> str | None:
        """Play liner *name*, or the next library pick, into the mix bus.

        Args:
            name: Plain liner file name to play. ``None`` picks the next
                clip from the liner folder using the configured rotation
                mode (refreshed from disk first).

        Returns:
            The liner name played, or ``None`` when nothing played
            (empty library, or the clip could not be decoded).
        """
        if name is None:
            self._library = LinerLibrary.from_folder(self._folder)
            picked = self._library.pick(self._cfg.liners_pick_mode, rng=self._rng)
            if picked is None:
                return None
            name = picked.name
        try:
            audio = self._decoder(self._folder, name)
        except Exception:
            logger.warning("Skipping liner %s: it could not be decoded", name, exc_info=True)
            return None
        self._bus.play_liner(audio, duck_db=float(self._cfg.liners_duck_db))
        self._tracks_since = 0
        self._last_fire = self._clock()
        self._random_target = self._trigger().roll_random_target(rng=self._rng)
        return name
