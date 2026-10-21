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

Saved-point automatic liners are reserved for the shared cue-based handoff
renderer. The legacy ``[playback] liners_talk_up`` setting affects live time
checks and non-saved-point server liners, where a clip due as a track starts is
timed like a radio talk-up: it ends just before the incoming track's
vocal (:meth:`autodj.player.Player.vocal_start_s`).  When it fits the
intro it is cued there; when it is too long for the intro but fits once
started over the crossfade, it is cued in the outgoing track's tail as
that track starts, when the every-N-songs trigger will be due at the
next track; otherwise it plays as the track starts, as without talk-up.
"""

from __future__ import annotations

import contextlib
import copy
import io
import logging
import random
import subprocess  # nosec B404 -- ffmpeg decode fallback with fixed argv, no shell
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import soundfile as sf

from autodj.liner_files import open_liner_file
from autodj.liners import LinerLibrary, LinerTrigger
from autodj.mixbus import LinerCue
from autodj.stereo import SAFE_FFMPEG_INPUT, SAMPLE_RATE, to_stereo

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
    # A sequential pick's list, its size, and how far the cursor moved for it
    # (more than one when it passed over liners outside their play dates).
    rotation: tuple[str, int, int] | None = None


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
                *SAFE_FFMPEG_INPUT,
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


def decode_liner_path(
    path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> np.ndarray:
    """Decode a path supplied by the active registered-liner provider.

    This entry point deliberately accepts paths only for trusted callers
    that resolve them through the current liner catalog first. HTTP input
    must never be passed here as a path.
    """
    path = Path(path)
    if path.suffix.lower() not in {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"}:
        raise LinerDecodeError(f"unsupported liner type: {path.name}")
    data = path.read_bytes()
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
                *SAFE_FFMPEG_INPUT,
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
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0 or not result.stdout or len(result.stdout) % 8:
            raise LinerDecodeError(f"cannot decode liner {path.name!r}") from None
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

    Thread-safety: two locks.  ``_work_lock`` covers one whole
    due-check-decode-and-fire sequence (:meth:`on_track_start`,
    :meth:`tick`, :meth:`fire`, :meth:`decode`), so concurrent calls from
    different threads — e.g. the broadcast/tick thread and a
    ``POST /api/liners/test`` request handler — cannot both observe the
    same trigger as due and double-fire, and decodes never overlap.
    ``_lock`` guards the trigger counts, rotation and handoff reservation
    and is never held across a decode or a catalog or folder read, so the
    calls that must not wait — :meth:`mark_planned_fired` on the mix bus
    thread, the cart wall — never wait for a decode.  A sequence that
    decoded with ``_lock`` released checks the trigger again before it
    plays: a planned liner the bus aired meanwhile restarted it.
    :meth:`mark_planned_fired` does not even wait for ``_lock``: when it is
    busy, the reservation is committed by the thread holding it.
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
        registered_liners: Callable[[], list[tuple[str, Path]]] | None = None,
        path_decoder: Callable[[Path], np.ndarray] | None = None,
        play_dates: Callable[[str], str | None] | None = None,
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
            play_dates: Why the liner with this ID (catalog ID or file
                name) may not play today, from its play dates, or ``None``.
                Automatic picks pass over refused liners; :meth:`refusal`
                tells a scheduled event.  The station may set it later.
        """
        self._cfg = playback_cfg
        self._folder = folder
        self._bus = bus
        self._clock = clock
        self._wall_clock = lambda: time.time()
        self._rng = rng or random.Random()
        self._decoder = decoder
        self._can_fire = can_fire
        self._vocal_start = vocal_start
        self._registered_liners = registered_liners
        self._path_decoder = path_decoder or decode_liner_path
        self.play_dates = play_dates
        # The same, for a liner that airs at a given Unix time (a talk-up
        # planned ahead): ``(name, at)``.  The station sets it.
        self.play_dates_at: Callable[[str, float], str | None] | None = None
        self._registered_cursor = 0
        self._prepared: _Prepared | None = None
        # Rendered audio due at this song change instead of an automatic
        # liner, such as the time check after the hour (set by its owner).
        self.announcement: Callable[[], np.ndarray | None] | None = None
        self._lock = threading.Lock()
        self._work_lock = threading.Lock()
        # Reservations the mix bus heard while _lock was busy, with when.
        self._heard: deque[tuple[str, float]] = deque()
        # Liners fired so far: a decode that finds it changed is stale.
        self._fires = 0
        self._tracks_since = 0
        self._last_fire = clock()
        self._library = LinerLibrary.from_folder(folder)
        # Matches the browser (``liners.js``'s ``state.randomTarget``
        # starting ``null``): the random window is not armed until the
        # first liner actually plays, so a random-only config cannot
        # auto-fire before one has played (by the test route, or by
        # another trigger).
        self._random_target: float | None = None
        # At most one future render may own an automatic handoff pick.
        self._planned_key: str | None = None
        self._planned_name: str | None = None
        # How far the sequential cursor moves when the planned liner airs.
        self._planned_moves = 1
        self._forecast_only = False
        self._forecast_clock_aligned = False

    def _trigger(self) -> LinerTrigger:
        """Build a :class:`LinerTrigger` from the current config values."""
        return LinerTrigger(
            every_n_songs=self._cfg.liners_every_n_songs,
            every_minutes=self._cfg.liners_every_minutes,
            random_min_minutes=self._cfg.liners_random_min_minutes,
            random_max_minutes=self._cfg.liners_random_max_minutes,
            enabled=bool(self._cfg.liners_enabled),
        )

    def snapshot(
        self,
        *,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
        cfg: Any | None = None,
    ) -> LinerScheduler:
        """Return an isolated scheduler copy for read-only planning simulations.

        ``cfg`` may be the playback config or a full config exposing
        ``playback``. ``wall_clock`` supplies forecast Unix time for play-date
        checks. Provider callbacks remain shared and must be read-only.
        The clone has no bus or announcement, and its RNG, config and state
        are independent of the live scheduler.
        """
        with self._work_lock, self._lock:
            snapshot = copy.copy(self)
            settings = self._cfg if cfg is None else getattr(cfg, "playback", cfg)
            snapshot._cfg = copy.deepcopy(settings)
            snapshot._clock = self._clock if clock is None else clock
            snapshot._forecast_clock_aligned = clock is not None
            offset = 0.0
            if clock is not None:
                offset = clock() - self._clock()
                snapshot._last_fire += offset
            snapshot._wall_clock = self._wall_clock if wall_clock is None else wall_clock
            snapshot._rng = random.Random()
            snapshot._rng.setstate(self._rng.getstate())
            snapshot._library = copy.copy(self._library)
            snapshot._library.files = list(self._library.files)
            snapshot._prepared = copy.copy(self._prepared)
            if snapshot._prepared is not None and snapshot._prepared.cue is not None:
                snapshot._prepared.cue = copy.copy(snapshot._prepared.cue)
            snapshot._heard = deque((key, at + offset) for key, at in self._heard)
            snapshot._lock = threading.Lock()
            snapshot._work_lock = threading.Lock()
            snapshot._bus = None
            snapshot._can_fire = lambda: False
            snapshot.announcement = None
            snapshot._forecast_only = True
            return snapshot

    def forecast(
        self,
        start: float,
        end: float,
        *,
        track: RenderedTrack | None = None,
        track_start: bool = True,
        on_audio: Callable[[float, str, np.ndarray], None] | None = None,
    ) -> list[tuple[float, str, float]]:
        """Forecast automatic liners from one track start through *end*.

        A snapshot runs the normal trigger and decode paths, with a captured
        bus and simulated clock. Forecast state accumulates on that snapshot;
        calling this on a live scheduler makes a temporary snapshot. Planner-
        mode talk-ups are forecast by the handoff planner instead.
        """
        try:
            start, end = float(start), float(end)
        except (TypeError, ValueError):
            return []
        if not np.isfinite(start) or not np.isfinite(end) or end < start or self._planner_mode():
            return []

        simulation = self
        if not getattr(self, "_forecast_only", False):
            simulation = self.snapshot(clock=lambda: start, wall_clock=lambda: start)
        if not getattr(simulation, "_forecast_clock_aligned", False):
            offset = start - simulation._clock()
            simulation._last_fire += offset
            simulation._heard = deque((key, at + offset) for key, at in simulation._heard)
            simulation._forecast_clock_aligned = True
        now = [start]
        simulation._clock = lambda: now[0]
        simulation._wall_clock = lambda: now[0]
        original_bus = simulation._bus
        original_can_fire = simulation._can_fire
        original_play_dates = simulation.play_dates
        original_decode = simulation._decode
        had_decode_override = "_decode" in simulation.__dict__
        decode_override = simulation.__dict__.get("_decode")
        simulation._can_fire = lambda: True
        events: list[tuple[float, str, float]] = []
        names: dict[int, str] = {}

        class ForecastBus:
            def play_liner(
                _self, audio: np.ndarray, *, at: LinerCue | None = None, **_kwargs: Any
            ) -> None:
                name = names.get(id(audio))
                if name is None:
                    return
                at_time = now[0] + (at.frame / SAMPLE_RATE if at is not None else 0.0)
                if at_time < end:
                    events.append((at_time, name, len(audio) / SAMPLE_RATE))
                    if on_audio is not None:
                        from autodj.stereo import copy_audio

                        on_audio(at_time, name, copy_audio(audio))
                    if at is not None:
                        at.started = True

            def stop_liner(_self) -> None:
                return None

        simulation._bus = ForecastBus()
        if simulation.play_dates_at is not None:
            simulation.play_dates = lambda name: simulation.refusal(name, now[0])

        def decode_named(name: str | None) -> _Prepared | None:
            prepared = original_decode(name)
            if prepared is not None:
                names[id(prepared.audio)] = prepared.name
            return prepared

        simulation.__dict__["_decode"] = decode_named
        try:
            if track_start:
                simulation.on_track_start(track)
            tick_at = start + 1.0
            while tick_at < end:
                now[0] = tick_at
                simulation.tick()
                tick_at += 1.0
        finally:
            if had_decode_override:
                simulation.__dict__["_decode"] = decode_override
            else:
                simulation.__dict__.pop("_decode", None)
            simulation.play_dates = original_play_dates
            simulation._can_fire = original_can_fire
            simulation._bus = original_bus
        return sorted(events)

    def _refresh_library(self) -> None:
        """Rescan the liner folder in place, keeping the rotation cursor.

        Replacing ``self._library`` outright (as opposed to updating its
        ``files``) would reset :attr:`LinerLibrary.cursor`
        to 0 on every automatic pick, so ``"sequential"`` mode would
        always replay the first file instead of rotating.  The folder is
        read without the state lock held.
        """
        fresh = LinerLibrary.from_folder(self._folder)
        with self._state():
            self._library.files = fresh.files
            if self._library.files:
                self._library.cursor %= len(self._library.files)
            else:
                self._library.cursor = 0

    @contextlib.contextmanager
    def _state(self) -> Iterator[None]:
        """Hold the state lock, applying any reservation heard while it was busy."""
        with self._lock:
            self._apply_heard_locked()
            try:
                yield
            finally:
                self._apply_heard_locked()

    def _apply_heard_locked(self) -> None:
        """Commit reservations :meth:`mark_planned_fired` had to leave. Caller holds ``_lock``."""
        while self._heard:
            key, at = self._heard.popleft()
            self._commit_planned_locked(key, at)

    def _choose(
        self, name: str | None
    ) -> tuple[str, Path | None, tuple[str, int, int] | None] | None:
        """Pick what to decode: *name*, or the next pick in rotation.

        Caller holds ``_work_lock`` but not ``_lock``: the catalog and the
        folder are read without it, and only the rotation step takes it.

        Returns:
            ``(name, path, rotation)`` for a registered liner, ``(name,
            None, rotation)`` for one in the liner folder, or ``None``.
        """
        registered = self._registered_entries()
        if name is not None and registered:
            match = next(((key, path) for key, path in registered if key == name), None)
            if match is not None:
                return (*match, None)
        if name is None and registered:
            # Automatic picks pass over liners outside their play dates.  The
            # dates are read before the state lock, and sequential rotation
            # keeps its place in the full list, so a liner that expires (or
            # starts) does not shift which liner comes next.
            playable = [self.refusal(key) is None for key, _path in registered]
            if not any(playable):
                return None
            with self._state():
                mode = self._cfg.liners_pick_mode
                if mode == "sequential":
                    index, moved = self._next_playable_locked(playable)
                    key, path = registered[index]
                    self._registered_cursor = (index + 1) % len(registered)
                    rotation: tuple[str, int, int] | None = ("registered", len(registered), moved)
                else:
                    key, path = self._rng.choice(
                        [item for item, ok in zip(registered, playable, strict=True) if ok]
                    )
                    rotation = None
            return key, path, rotation
        if name is None:
            self._refresh_library()
            files = self._library.files
            refused = {path.name for path in files if self.refusal(path.name) is not None}
            with self._state():
                mode = self._cfg.liners_pick_mode
                before = self._library.cursor
                picked = self._library.pick(
                    mode, rng=self._rng, allowed=lambda path: path.name not in refused
                )
                size = len(self._library.files)
                rotation = (
                    ("folder", size, (self._library.cursor - before) % size or size)
                    if mode == "sequential" and size
                    else None
                )
            if picked is None:
                return None
            name = picked.name
        else:
            rotation = None
        return name, None, rotation

    def _decode(self, name: str | None) -> _Prepared | None:
        """Decode *name*, or the next pick, with ``_lock`` released.

        Caller holds ``_work_lock``, so decodes never overlap, but not
        ``_lock``: the mix bus thread's :meth:`mark_planned_fired` and the
        cart wall never wait for a decode.

        Returns:
            The liner, or ``None`` when the library is empty or the clip
            cannot be decoded (logged).
        """
        choice = self._choose(name)
        if choice is None:
            return None
        key, path, rotation = choice
        if path is not None:
            try:
                return _Prepared(key, self._path_decoder(path), rotation=rotation)
            except Exception:
                logger.warning(
                    "Skipping registered liner %s: it could not be decoded", key, exc_info=True
                )
                return None
        try:
            audio = self._decoder(self._folder, key)
        except Exception:
            logger.warning("Skipping liner %s: it could not be decoded", key, exc_info=True)
            return None
        return _Prepared(key, audio, rotation=rotation)

    def _undo_pick_locked(self, liner: _Prepared) -> None:
        """Undo a sequential pick that was discarded before playback."""
        if liner.rotation is None:
            return
        source, size, moved = liner.rotation
        if source == "registered":
            self._registered_cursor = (self._registered_cursor - moved) % size
        elif source == "folder":
            self._library.cursor = (self._library.cursor - moved) % size

    def _next_playable_locked(self, playable: list[bool]) -> tuple[int, int]:
        """The next playable registered liner from the cursor, and how far the cursor moves.

        Caller holds ``_lock``; *playable* has one flag per registered liner
        and at least one is true.
        """
        size = len(playable)
        start = self._registered_cursor % size
        step = next(step for step in range(size) if playable[(start + step) % size])
        return (start + step) % size, step + 1

    def refusal(self, name: str, at: float | None = None) -> str | None:
        """Why liner *name* (catalog ID or file name) may not play now, or ``None``.

        With *at*, a Unix time, judged by the day the liner airs then.
        """
        if at is not None and self.play_dates_at is not None:
            try:
                return self.play_dates_at(name, at)
            except Exception:
                logger.warning("Could not read the play dates of liner %s", name, exc_info=True)
                return None
        if self.play_dates is None:
            return None
        try:
            return self.play_dates(name)
        except Exception:
            logger.warning("Could not read the play dates of liner %s", name, exc_info=True)
            return None

    def _registered_entries(self) -> list[tuple[str, Path]]:
        """Return current active catalog liner ids and their paths."""
        if self._registered_liners is None:
            return []
        try:
            return [(str(key), Path(path)) for key, path in self._registered_liners()]
        except Exception:
            logger.warning("Could not refresh registered liner entries", exc_info=True)
            return []

    def _fired_locked(self, at: float | None = None) -> None:
        """Restart the trigger counts after a liner played. Caller holds ``_lock``."""
        self._tracks_since = 0
        self._last_fire = self._clock() if at is None else at
        self._random_target = self._trigger().roll_random_target(rng=self._rng)
        self._fires += 1

    def _play_locked(self, liner: _Prepared, cue: LinerCue | None) -> str:
        """Play decoded *liner* into the bus, at *cue* when given. Caller holds ``_lock``."""
        duck_db = float(self._cfg.liners_duck_db)
        # Under a live DJ (#48) a station ID or other liner waits to play once
        # when they hand back; a time check ("announcement") would be stale.
        after_live = liner.name != "announcement"
        if cue is None:
            self._bus.play_liner(liner.audio, duck_db=duck_db, after_live=after_live)
        else:
            self._bus.play_liner(liner.audio, duck_db=duck_db, at=cue, after_live=after_live)
        self._fired_locked()
        return liner.name

    def _talk_up(self) -> bool:
        """Whether talk-ups are on and can be timed."""
        return bool(getattr(self._cfg, "liners_talk_up", False)) and self._vocal_start is not None

    def _intro_cue(self, track: RenderedTrack | None, length: int) -> LinerCue | None:
        """Cue a *length*-frame liner to end just before *track*'s vocal.

        ``None`` when there is no track, talk-ups are off, the vocal start
        is unknown, or the liner does not fit before it.  Positions after a
        beatmatched entry are estimated at the faster of the two tempos, so
        the liner ends early rather than late.
        """
        if track is None or not self._talk_up():
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

    def _maybe_arm(self, track: RenderedTrack | None) -> None:
        """Get a talk-up ready for the track after *track*. Caller holds ``_work_lock``.

        When the trigger will be due at the next track start, the liner is
        decoded now; if it only fits before the next track's vocal when it
        starts during the crossfade, it is cued there in *track*'s tail.
        """
        if (
            track is None
            or not self._talk_up()
            or not self._can_fire()
            or self._planner_mode()
            or getattr(self._cfg, "pick_mode", None) == "clock"
        ):
            return
        upcoming = track.next_entry
        if upcoming is None or track.overlap_frames <= 0:
            return
        with self._state():
            due = self._trigger().should_fire(
                track_count=self._tracks_since + 1,
                minutes_since_last=(self._clock() - self._last_fire) / 60.0,
                random_target_minutes=self._random_target,
            )
            fires = self._fires
        if not due:
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
        liner = self._decode(None)
        if liner is None:
            return
        frame = int(vocal_frame - TALK_UP_MARGIN_S * SAMPLE_RATE - len(liner.audio))
        with self._state():
            if self._fires != fires:
                self._undo_pick_locked(liner)
                return  # a planned liner aired while this one decoded: not due now
            if overlap_start <= frame < len(track.audio):
                liner.cue = LinerCue(track, frame)
                duck_db = float(self._cfg.liners_duck_db)
                self._bus.play_liner(liner.audio, duck_db=duck_db, at=liner.cue)
            self._prepared = liner

    def _auto_due_locked(self) -> bool:
        """Whether an automatic liner is due now. Caller holds ``_lock``."""
        if self._planner_mode() or getattr(self._cfg, "pick_mode", None) == "clock":
            return False
        armed = self._prepared is not None and self._prepared.cue is not None
        if not self._can_fire() or armed:
            return False
        return self._trigger().should_fire(
            track_count=self._tracks_since,
            minutes_since_last=(self._clock() - self._last_fire) / 60.0,
            random_target_minutes=self._random_target,
        )

    def _maybe_fire(
        self, track: RenderedTrack | None = None, prepared: _Prepared | None = None
    ) -> None:
        """Fire when the trigger config says it is due. Caller holds ``_work_lock``.

        The clip is decoded with ``_lock`` released, then the trigger is
        checked again: a planned liner the bus aired meanwhile restarted it.
        """
        with self._state():
            if not self._auto_due_locked():
                return
            fires = self._fires
        liner = prepared or self._decode(None)
        if liner is None:
            return
        cue = self._intro_cue(track, len(liner.audio))
        with self._state():
            if self._fires == fires:
                self._play_locked(liner, cue)
            else:
                self._undo_pick_locked(liner)

    def on_track_start(self, track: RenderedTrack | None = None) -> None:
        """Count a track advance and fire a liner if a trigger is due.

        A due :attr:`announcement` takes the place of an automatic liner at
        this start, with the same duck and talk-up timing.  It is rendered
        before any lock is taken, and is not asked for while a talk-up
        liner from the crossfade is on air or automatic liners may not
        fire, so it stays due for a later start.

        Args:
            track: The render that just started, which talk-ups time
                against; ``None`` plays a due liner straight away.
        """
        with self._state():
            prepared = self._prepared
            on_air = prepared is not None and prepared.cue is not None and prepared.cue.started
        announcement = self.announcement
        audio = None
        if announcement is not None and not on_air and self._can_fire():
            try:
                audio = announcement()
            except Exception:
                logger.exception("Could not render the announcement due at this song")
        with self._work_lock:
            with self._state():
                self._tracks_since += 1
                prepared, self._prepared = self._prepared, None
                aired = (
                    audio is None
                    and prepared is not None
                    and prepared.cue is not None
                    and prepared.cue.started
                )
                if aired:
                    self._fired_locked()  # it played over the crossfade into this track
            if audio is not None:
                cue = self._intro_cue(track, len(audio))
                with self._state():
                    self._play_locked(_Prepared("announcement", audio), cue)
            elif not aired:
                if prepared is not None and prepared.cue is not None:
                    prepared.cue = None  # its track ended first (a skip): it is due now
                self._maybe_fire(track, prepared)
            self._maybe_arm(track)

    def tick(self) -> None:
        """Check timed triggers; call about once a second."""
        with self._work_lock:
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
        with self._work_lock:
            liner = self._decode(name)
            if liner is None:
                return None
            with self._state():
                return self._play_locked(liner, None)

    def decode(self, name: str) -> np.ndarray:
        """Decode liner *name*, a catalog ID or a liner-folder file name, as :meth:`fire` would.

        Raises:
            LinerDecodeError: There is no such liner, or it could not be decoded.
        """
        with self._work_lock:
            prepared = self._decode(name)
        if prepared is None:
            raise LinerDecodeError(f"cannot decode liner {name!r}")
        return prepared.audio

    def play_cart(self, audio: np.ndarray, duck_db: float) -> None:
        """Play a cart clip now through the liner path; it does not reset the liner triggers.

        Never waits for a liner decode in progress.
        """
        with self._state():
            self._bus.play_liner(audio, duck_db=duck_db)

    def stop_cart(self) -> None:
        """Fade out the clip on the liner path and bring the music back up."""
        with self._state():
            self._bus.stop_liner()

    def play_audio(self, audio: np.ndarray) -> None:
        """Play rendered *audio* now, as :meth:`fire` plays a named liner."""
        with self._state():
            self._play_locked(_Prepared("announcement", audio), None)

    def _planner_mode(self) -> bool:
        """Whether saved-point liners are owned by the cue-based renderer."""
        return getattr(self._cfg, "transition_mode", None) == "saved_points"

    def plan_for_handoff(self, key: str, tracks_ahead: int, seconds_ahead: float) -> str | None:
        """Reserve an automatic liner for a future saved-point handoff.

        The planner returns a stable catalog id (or legacy filename) without
        decoding or playing it. A reservation is consumed only when the
        corresponding handoff is heard via :meth:`mark_planned_fired`.  The
        catalog and liner folder are read with no lock held.
        """
        if not isinstance(key, str) or not key or tracks_ahead < 0:
            return None
        try:
            seconds = float(seconds_ahead)
        except (TypeError, ValueError):
            return None
        if not np.isfinite(seconds) or seconds < 0:
            return None

        def check_locked() -> str | None:
            """``""`` to go on picking; otherwise the answer (``None``: no liner)."""
            if not self._planner_mode() or not self._cfg.liners_enabled:
                return None
            if self._planned_key is not None:
                return self._planned_name if self._planned_key == key else None
            minutes = (self._clock() - self._last_fire + seconds) / 60.0
            if not self._trigger().should_fire(
                track_count=self._tracks_since + tracks_ahead,
                minutes_since_last=minutes,
                random_target_minutes=self._random_target,
            ):
                return None
            return ""

        with self._state():
            answer = check_locked()
        if answer != "":
            return answer
        registered = self._registered_entries()
        if not registered:
            self._refresh_library()
        # Liners outside their play dates are passed over, as automatic
        # picks pass over them; the dates are read before the state lock.
        names = [key for key, _path in registered] or [path.name for path in self._library.files]
        # Judged by the day the liner airs, at the handoff *seconds* from now.
        airs = self._wall_clock() + seconds
        playable = [self.refusal(name, airs) is None for name in names]
        with self._state():
            answer = check_locked()
            if answer != "":
                return answer
            if registered:
                if not any(playable):
                    return None
                if self._cfg.liners_pick_mode == "sequential":
                    index, moved = self._next_playable_locked(playable)
                    name = registered[index][0]
                else:
                    name = self._rng.choice(
                        [n for n, ok in zip(names, playable, strict=True) if ok]
                    )
                    moved = 1
            else:
                refused = {n for n, good in zip(names, playable, strict=True) if not good}
                files = [path.name for path in self._library.files]
                ok = [name not in refused for name in files]
                if not any(ok):
                    return None
                if self._cfg.liners_pick_mode == "sequential":
                    start = self._library.cursor % len(files)
                    step = next(
                        step for step in range(len(files)) if ok[(start + step) % len(files)]
                    )
                    name, moved = files[(start + step) % len(files)], step + 1
                else:
                    name = self._rng.choice([n for n, good in zip(files, ok, strict=True) if good])
                    moved = 1
            self._planned_key, self._planned_name = key, name
            self._planned_moves = moved
            return name

    def release_plan(self, key: str | None = None) -> bool:
        """Release a discarded render's reservation, if it owns the slot."""
        with self._state():
            if self._planned_key is None or (key is not None and key != self._planned_key):
                return False
            self._planned_key = self._planned_name = None
            return True

    def reserve_frozen_handoff(
        self, key: str, name: str, *, tracks_ahead: int, seconds_ahead: float
    ) -> bool:
        """Adopt a previously predicted handoff pick without drawing the RNG again."""
        try:
            seconds = float(seconds_ahead)
        except (TypeError, ValueError):
            return False
        if not np.isfinite(seconds) or seconds < 0 or not key or not name or tracks_ahead < 0:
            return False
        registered = self._registered_entries()
        files = [entry[0] for entry in registered]
        if registered:
            matches = [
                index for index, (candidate, _path) in enumerate(registered) if candidate == name
            ]
        else:
            self._refresh_library()
            files = [path.name for path in self._library.files]
            matches = [index for index, candidate in enumerate(files) if candidate == name]
        if not matches:
            return False
        airs = self._wall_clock() + seconds
        playable = [self.refusal(candidate, airs) is None for candidate in files]
        selected = matches[0]
        if not playable[selected]:
            return False
        with self._state():
            if not self._planner_mode() or not self._cfg.liners_enabled:
                return False
            if self._planned_key is not None:
                return self._planned_key == key and self._planned_name == name
            minutes = (self._clock() - self._last_fire + seconds) / 60.0
            if not self._trigger().should_fire(
                track_count=self._tracks_since + tracks_ahead,
                minutes_since_last=minutes,
                random_target_minutes=self._random_target,
            ):
                return False
            if self._cfg.liners_pick_mode == "sequential":
                start = (
                    self._registered_cursor % len(files)
                    if registered
                    else self._library.cursor % len(files)
                )
                moved = (selected - start) % len(files) + 1
            else:
                moved = 1
            self._planned_key, self._planned_name = key, name
            self._planned_moves = moved
            return True

    def mark_planned_fired(self, key: str) -> bool:
        """Commit a reservation when the planned liner actually becomes audible.

        Called on the mix bus thread, so it never waits: when another
        thread holds the state lock, the reservation is left for that
        thread to commit as it lets go (or for the next scheduler call),
        timed from now either way.

        Returns:
            Whether the reservation was committed now; ``False`` for a key
            that is not the reservation, or one left for later.
        """
        at = self._clock()
        if not self._lock.acquire(blocking=False):
            self._heard.append((key, at))
            # The holder may have let go between the two steps.
            if not self._lock.acquire(blocking=False):
                return False
            try:
                self._apply_heard_locked()
            finally:
                self._lock.release()
            return False
        try:
            self._apply_heard_locked()
            return self._commit_planned_locked(key, at)
        finally:
            self._lock.release()

    def _commit_planned_locked(self, key: str, at: float) -> bool:
        """Consume the reservation *key*, heard at *at*. Caller holds ``_lock``."""
        if self._planned_key != key:
            return False
        self._planned_key = self._planned_name = None
        # Past the planned liner and any outside their dates that it passed over.
        moved = self._planned_moves
        if self._registered_liners is not None and self._cfg.liners_pick_mode == "sequential":
            # Position callbacks must not perform provider or filesystem I/O.
            self._registered_cursor += moved
        elif self._cfg.liners_pick_mode == "sequential":
            self._library.cursor += moved
        self._fired_locked(at)
        return True
