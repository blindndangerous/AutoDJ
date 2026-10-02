"""Server-side voice-liner scheduling for server-mixed playback.

The browser evaluates liner triggers locally when it owns the audio
element (``autodj/static/modules/liners.js``).  In server-mixed modes
(``--server-audio`` or ``--stream``) the :class:`~autodj.mixbus.MixBus`
owns the audio clock instead, so the same trigger/pick logic needs a server-side home
that can decode a clip and hand it to the bus. This module is that home.

:class:`LinerScheduler` wraps :class:`~autodj.liners.LinerTrigger` and
:class:`~autodj.liners.LinerLibrary` — reusing their tested trigger and
rotation semantics — and adds the one thing they intentionally do not
do: decoding a file to the bus's stereo float32 format and calling
:meth:`~autodj.mixbus.MixBus.play_liner`.

With ``[playback] liners_talk_up`` on, a liner due as a track starts is
timed like a radio talk-up: it ends just before the incoming track's
vocal (:meth:`autodj.player.Player.vocal_start_s`).  When it fits the
intro it is cued there; when it is too long for the intro but fits once
started over the crossfade, it is cued in the outgoing track's tail as
that track starts, when the every-N-songs trigger will be due at the
next track; otherwise it plays as the track starts, as without talk-up.
"""

from __future__ import annotations

import io
import logging
import random
import subprocess  # nosec B404 -- ffmpeg decode fallback with fixed argv, no shell
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import soundfile as sf

from autodj.liner_files import open_liner_file
from autodj.liners import LinerLibrary, LinerTrigger
from autodj.mixbus import LinerCue
from autodj.stereo import SAMPLE_RATE, to_stereo

if TYPE_CHECKING:
    from autodj.indexer import IndexEntry
    from autodj.mixbus import RenderedTrack

logger = logging.getLogger(__name__)

#: A talk-up ends this long before the vocal, in seconds.
TALK_UP_MARGIN_S = 0.25


@dataclass
class _Prepared:
    """A liner picked and decoded ahead of the track start it is due at.

    Attributes:
        name: Liner file name.
        audio: Decoded audio.
        cue: Where it waits in the outgoing track's tail, when it was cued
            over the crossfade; ``None`` when it waits for the track start.
    """

    name: str
    audio: np.ndarray
    cue: LinerCue | None = None


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
        InvalidLinerName: If *name* is not a plain liner file name.
        FileNotFoundError: If the file is gone, or ffmpeg is not installed.
        subprocess.TimeoutExpired: If ffmpeg takes longer than 30 seconds.
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

    Owns one :class:`~autodj.liners.LinerLibrary` for the process
    lifetime — its file list is rescanned from disk before every
    automatic pick (so uploads/deletes take effect without a restart)
    but its sequential-rotation ``cursor`` is preserved across fires,
    matching ``liners.js``'s persistent ``seqCursor``. Rebuilds a
    :class:`~autodj.liners.LinerTrigger` from the live config on every
    evaluation, so config changes applied through the settings API take
    effect on the next track/tick without recreating the scheduler.

    A liner that fails to decode is logged and skipped — it never
    reaches :meth:`~autodj.mixbus.MixBus.play_liner`, so a bad or
    unreachable clip cannot interrupt the music.

    Thread-safety: :meth:`on_track_start`, :meth:`tick` and :meth:`fire`
    all take one internal lock covering the whole due-check-and-fire
    sequence (including the decode and the ``play_liner`` call), so
    concurrent calls from different threads — e.g. the broadcast/tick
    thread and a ``POST /api/liners/test`` request handler — cannot both
    observe the same trigger as due and double-fire, and a slow decode
    on one thread cannot interleave with another thread's fire.
    """

    def __init__(
        self,
        playback_cfg: Any,
        folder: Path,
        bus: Any,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
        decoder: Callable[[Path, str], np.ndarray] = decode_liner,
        can_fire: Callable[[], bool] = lambda: True,
        vocal_start: Callable[[IndexEntry], float | None] | None = None,
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
            can_fire: Whether automatic triggers may fire now.  Stream
                mode passes "a set is playing and not paused": the bus
                renders nothing while idle or paused, so a liner fired
                then would only wait and open the next set.  Like the
                browser's ``canPlay`` check, a skipped trigger stays due.
            vocal_start: Seconds into a track's file where its vocal
                starts, or ``None`` when unknown; talk-ups need it.
        """
        self._cfg = playback_cfg
        self._folder = folder
        self._bus = bus
        self._clock = clock
        self._rng = rng or random.Random()
        self._decoder = decoder
        self._can_fire = can_fire
        self._vocal_start = vocal_start
        self._prepared: _Prepared | None = None
        self._lock = threading.Lock()
        self._tracks_since = 0
        self._last_fire = clock()
        self._library = LinerLibrary.from_folder(folder)
        # Matches the browser (``liners.js``'s ``state.randomTarget``
        # starting ``null``): the random window is not armed until the
        # first liner actually plays, so a random-only config cannot
        # auto-fire before one has played (by the test route, or by
        # another trigger).
        self._random_target: float | None = None

    def _trigger(self) -> LinerTrigger:
        """Build a :class:`LinerTrigger` from the current config values."""
        return LinerTrigger(
            every_n_songs=self._cfg.liners_every_n_songs,
            every_minutes=self._cfg.liners_every_minutes,
            random_min_minutes=self._cfg.liners_random_min_minutes,
            random_max_minutes=self._cfg.liners_random_max_minutes,
            enabled=bool(self._cfg.liners_enabled),
        )

    def _refresh_library(self) -> None:
        """Rescan the liner folder in place, keeping the rotation cursor.

        Replacing ``self._library`` outright (as opposed to updating its
        ``files``) would reset :attr:`LinerLibrary.cursor`
        to 0 on every automatic pick, so ``"sequential"`` mode would
        always replay the first file instead of rotating.
        """
        fresh = LinerLibrary.from_folder(self._folder)
        self._library.files = fresh.files
        if self._library.files:
            self._library.cursor %= len(self._library.files)
        else:
            self._library.cursor = 0

    def _decode_locked(self, name: str | None) -> _Prepared | None:
        """Decode *name*, or the next pick. Caller holds ``_lock``.

        Returns:
            The liner, or ``None`` when the library is empty or the clip
            cannot be decoded (logged).
        """
        if name is None:
            self._refresh_library()
            picked = self._library.pick(self._cfg.liners_pick_mode, rng=self._rng)
            if picked is None:
                return None
            name = picked.name
        try:
            audio = self._decoder(self._folder, name)
        except Exception:
            logger.warning("Skipping liner %s: it could not be decoded", name, exc_info=True)
            return None
        return _Prepared(name, audio)

    def _fired_locked(self) -> None:
        """Restart the trigger counts after a liner played. Caller holds ``_lock``."""
        self._tracks_since = 0
        self._last_fire = self._clock()
        self._random_target = self._trigger().roll_random_target(rng=self._rng)

    def _fire_locked(
        self,
        name: str | None,
        track: RenderedTrack | None = None,
        prepared: _Prepared | None = None,
    ) -> str | None:
        """Play *name*, *prepared* or the next pick into the bus. Caller holds ``_lock``.

        With talk-ups on and the *track* that just started, the liner is
        cued to end before its vocal when it fits; otherwise it starts now.
        """
        liner = prepared or self._decode_locked(name)
        if liner is None:
            return None
        duck_db = float(self._cfg.liners_duck_db)
        cue = self._intro_cue(track, len(liner.audio)) if track is not None else None
        if cue is None:
            self._bus.play_liner(liner.audio, duck_db=duck_db)
        else:
            self._bus.play_liner(liner.audio, duck_db=duck_db, at=cue)
        self._fired_locked()
        return liner.name

    def _talk_up(self) -> bool:
        """Whether talk-ups are on and can be timed."""
        return bool(getattr(self._cfg, "liners_talk_up", False)) and self._vocal_start is not None

    def _intro_cue(self, track: RenderedTrack, length: int) -> LinerCue | None:
        """Cue a *length*-frame liner to end just before *track*'s vocal.

        ``None`` when talk-ups are off, the vocal start is unknown, or the
        liner does not fit before it.  Positions after a beatmatched entry
        are estimated at the faster of the two tempos, so the liner ends
        early rather than late.
        """
        if not self._talk_up():
            return None
        assert self._vocal_start is not None
        vocal = self._vocal_start(track.entry)
        if vocal is None:
            return None
        glide = track.glide_in
        ratio = min(1.0, glide.ratio) if glide is not None else 1.0
        vocal_frame = (vocal * SAMPLE_RATE - track.start_offset) * ratio
        frame = int(vocal_frame - TALK_UP_MARGIN_S * SAMPLE_RATE - length)
        return LinerCue(track, frame) if frame > 0 else None

    def _maybe_arm_locked(self, track: RenderedTrack | None) -> None:
        """Get a talk-up ready for the track after *track*. Caller holds ``_lock``.

        When the trigger will be due at the next track start, the liner is
        decoded now; if it only fits before the next track's vocal when it
        starts during the crossfade, it is cued there in *track*'s tail.
        """
        if track is None or not self._talk_up() or not self._can_fire():
            return
        upcoming = track.next_entry
        if upcoming is None or track.overlap_frames <= 0:
            return
        minutes = (self._clock() - self._last_fire) / 60.0
        if not self._trigger().should_fire(
            track_count=self._tracks_since + 1,
            minutes_since_last=minutes,
            random_target_minutes=self._random_target,
        ):
            return
        assert self._vocal_start is not None
        vocal = self._vocal_start(upcoming)
        if vocal is None:
            return
        into = vocal * SAMPLE_RATE - track.next_entry_offset
        if into < 0:
            return
        ratio = track.beatmatch_ratio if track.beatmatch_ratio > 0 else 1.0
        overlap_start = len(track.audio) - track.overlap_frames
        heard_in_overlap = track.overlap_frames / ratio
        if into <= heard_in_overlap:
            vocal_frame = overlap_start + into * ratio
        else:
            vocal_frame = len(track.audio) + (into - heard_in_overlap) * min(1.0, ratio)
        liner = self._decode_locked(None)
        if liner is None:
            return
        frame = int(vocal_frame - TALK_UP_MARGIN_S * SAMPLE_RATE - len(liner.audio))
        if overlap_start <= frame < len(track.audio):
            liner.cue = LinerCue(track, frame)
            duck_db = float(self._cfg.liners_duck_db)
            self._bus.play_liner(liner.audio, duck_db=duck_db, at=liner.cue)
        self._prepared = liner

    def _armed_cue_waiting(self) -> bool:
        """Whether a talk-up is cued in the playing track's tail. Caller holds ``_lock``."""
        return self._prepared is not None and self._prepared.cue is not None

    def _maybe_fire_locked(
        self, track: RenderedTrack | None = None, prepared: _Prepared | None = None
    ) -> None:
        """Fire when the current trigger config says it is due. Caller holds ``_lock``."""
        if not self._can_fire() or self._armed_cue_waiting():
            return
        minutes = (self._clock() - self._last_fire) / 60.0
        if self._trigger().should_fire(
            track_count=self._tracks_since,
            minutes_since_last=minutes,
            random_target_minutes=self._random_target,
        ):
            self._fire_locked(None, track, prepared)

    def on_track_start(self, track: RenderedTrack | None = None) -> None:
        """Count a track advance and fire a liner if a trigger is due.

        Args:
            track: The render that just started, which talk-ups time
                against; ``None`` plays a due liner straight away.
        """
        with self._lock:
            self._tracks_since += 1
            prepared, self._prepared = self._prepared, None
            if prepared is not None and prepared.cue is not None and prepared.cue.started:
                self._fired_locked()  # it played over the crossfade into this track
            else:
                if prepared is not None and prepared.cue is not None:
                    prepared.cue = None  # its track ended first (a skip): it is due now
                self._maybe_fire_locked(track, prepared)
            self._maybe_arm_locked(track)

    def tick(self) -> None:
        """Check timed triggers; call about once a second."""
        with self._lock:
            self._maybe_fire_locked()

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
        with self._lock:
            return self._fire_locked(name)
