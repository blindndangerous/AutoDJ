"""Render an isolated programme snapshot with the existing playout mixer.

The renderer owns its player, catalog view, DJ metadata and PCM. It never starts
an output bus, books a clip, writes play history or persists audio measurements.
Shortening happens only after the full programme's real joins have been mixed.
"""

from __future__ import annotations

import copy
import random
import shutil
import sqlite3
import subprocess  # nosec B404 -- fixed encoder arguments, no shell
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack, closing, suppress
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import soundfile as sf

from autodj.child_jobs import bind_to_autodj
from autodj.hour_preview import Item, item_title
from autodj.mixbus import BusEvents, MixBus, RenderedTrack
from autodj.review_audio import ReviewSettings, ReviewSpan, shorten_review
from autodj.review_jobs import ReviewError
from autodj.rotation_sim import SimulationSource, _copy_player, snapshot_player
from autodj.sqlite_utils import readonly_uri
from autodj.stereo import (
    SAMPLE_RATE,
    AudioSpool,
    audio_buffer,
    audio_workspace,
    join_audio,
    load_stereo,
)


class _Catalog:
    def __init__(self, real: Any, entries: Sequence[Any]) -> None:
        self._records = {}
        self._points = {}
        for entry in entries:
            self._records[entry.path] = (
                copy.deepcopy(real.record_for_path(entry.path)) if real else None
            )
            self._points[entry.path] = copy.deepcopy(real.points(entry.path)) if real else {}

    def record_for_path(self, path: str) -> Any:
        return copy.deepcopy(self._records.get(path))

    def points(self, path: str) -> dict[str, Any]:
        return dict(self._points.get(path, {}))

    def set_loudness(self, path: Path, measurement: Any) -> None:
        # The render may measure a source, but cannot persist in the live catalog.
        pass


class _Metadata:
    def __init__(
        self, real: Any, entries: Sequence[Any], cfg: Any, *, shared: dict | None = None
    ) -> None:
        from autodj.dj_meta import DjMeta, DjMetaCache

        self._empty = DjMeta
        self._values = {}
        if shared is None:
            shared = {}
        if real is not None:
            with real._lock:
                for entry in entries:
                    key = real._key(entry.path)
                    value = real._buf.get(key, real._mem_cache.get(key))
                    miss = real._misses.get(key)
                    if value is None and miss is not None and real._clock() < miss[0]:
                        value = miss[1]
                    if value is not None:
                        self._values[entry.path] = copy.deepcopy(value)
        if len(self._values) < len({entry.path for entry in entries}):
            path = cfg.index.library_dir / "dj_meta.db"
            if not path.is_file():
                return
            # Reuse existing key compatibility and row decoding without invoking
            # the cache constructor, which creates/migrates a writable database.
            decoder = object.__new__(DjMetaCache)
            decoder._music_dir = cfg.library.music_dir
            with (
                suppress(sqlite3.DatabaseError, OSError, ValueError),
                closing(sqlite3.connect(readonly_uri(path), uri=True, timeout=1)) as conn,
            ):
                conn.execute("BEGIN")
                decoder._conn = conn
                # Legacy Windows keys need a table-wide path lookup. Reuse it
                # across this response's results, never across browser requests.
                decoder._normalized_paths = shared.get("dj_normalized_paths")
                decoder._normalized_paths_version = int(
                    conn.execute("PRAGMA data_version").fetchone()[0]
                )
                for entry in entries:
                    if entry.path in self._values:
                        continue
                    keys = (
                        decoder._key(entry.path),
                        decoder._absolute_alias(entry.path),
                        decoder._legacy_key(entry.path),
                    )
                    for key in keys:
                        if key is None:
                            continue
                        stored = decoder._stored_key_locked(key)
                        if stored is None:
                            continue
                        row = conn.execute(
                            "SELECT intro_end_s,outro_start_s,analysed,beats,cues,intro_start_s "
                            "FROM dj_meta WHERE path=?",
                            (stored,),
                        ).fetchone()
                        if row is not None:
                            self._values[entry.path] = decoder._row_to_meta(row)
                            break
                shared["dj_normalized_paths"] = decoder._normalized_paths

    def get(self, path: str) -> Any:
        return copy.deepcopy(self._values.get(path, self._empty()))

    def set(self, path: str, value: Any) -> None:
        self._values[path] = copy.deepcopy(value)

    def flush(self, **kwargs: Any) -> None:
        pass


class _Cancellation(threading.Event):
    def __init__(self, cancelled: Callable[[], bool]) -> None:
        super().__init__()
        self._cancelled = cancelled

    def is_set(self) -> bool:
        return self._cancelled()


def _check(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise InterruptedError("Review cancelled or timed out")


def _pcm(value: Any, *, copy_audio: bool = True) -> np.ndarray:
    if (
        not isinstance(value, np.ndarray)
        or value.ndim != 2
        or value.shape[1] != 2
        or not len(value)
    ):
        raise ReviewError("A review clip has no usable stereo audio")
    result = audio_buffer(len(value)) if copy_audio else value
    for start in range(0, len(value), 10 * SAMPLE_RATE):
        part = value[start : start + 10 * SAMPLE_RATE]
        if not np.isfinite(part).all():
            raise ReviewError("A review clip contains invalid samples")
        if copy_audio:
            result[start : start + len(part)] = part
    return result


def _entries(items: Sequence[Item]) -> list[Any]:
    found = {}
    for item in items:
        if item.entry is not None:
            found[item.entry.path] = item.entry
        handoff = item.incoming_handoff
        if handoff is not None:
            for entry, _queued in handoff.liners:
                found[entry.path] = entry
    return list(found.values())


def _label(item: Item) -> str:
    return item.label if item.kind == "clip" and item.label else item_title(item)


def _player(source: SimulationSource, items: Sequence[Item], start: float) -> Any:
    entries = _entries(items)
    snapshot = source.snapshot or snapshot_player(source.player)
    library = copy.copy(source.library) if source.library is not None else SimpleNamespace()
    library.catalog = _Catalog(getattr(source.library, "catalog", None), entries)
    liners = tuple(source.library.entries_snapshot("liner")) if source.library is not None else ()
    voice = tuple(item.entry for item in items if item.kind == "clip" and item.entry is not None)
    liners = tuple({entry.path: entry for entry in (*liners, *voice)}.values())
    for entry in voice:
        record = library.catalog._records.get(entry.path) or {}
        library.catalog._records[entry.path] = {**record, "kind": "liner"}
    music = tuple(entry for entry in entries if entry.path not in {liner.path for liner in liners})
    library.entries_snapshot = lambda kind="music": liners if kind == "liner" else music
    isolated = replace(source, library=library, liner_dates=None)
    player = _copy_player(isolated, snapshot, start, random.Random(0), lambda *_: None)
    player._playout_lock = threading.Lock()
    player._playout_slot = None
    player._rng = np.random.default_rng(0)
    player._library_cues = copy.deepcopy(getattr(source.player, "_library_cues", None))
    player._dj_cache = cast(
        Any, _Metadata(getattr(source.player, "_dj_cache", None), entries, player._cfg)
    )
    player._dj_cache_initialised = True
    player.break_desk = SimpleNamespace(
        config=copy.deepcopy(player._cfg.breaks),
        mixed_as=lambda *_: None,
        drop=lambda *_: None,
    )
    return player


def _ad_breaks(source: SimulationSource, handoff: Any) -> tuple[Any, ...]:
    from autodj.breaks import AD, AD_SCHEDULE, Break, BreakCues

    result = list(copy.deepcopy(handoff.breaks))
    for key, spot in handoff.ad_spots:
        folder = source.ads.store.folder if source.ads is not None else None
        if folder is None:
            raise ReviewError("The selected ad audio is unavailable; capture the review again")
        result.append(
            Break(
                break_id=key,
                kind=AD,
                slot_id="review",
                ending="",
                upcoming="",
                placement="standalone",
                allow_outro=False,
                path=folder / spot.file,
                size=0,
                cues=BreakCues(spot.cue_start, spot.cue_segue, spot.cue_end, spot.length_s),
                label=spot.label,
                token_name="",  # nosec B106 -- offline ad has no API credential
                created_at=0,
                expires_at=0,
                source=AD_SCHEDULE,
                keep_file=True,
                limit_s=max(float(spot.length_s), 1),
            )
        )
    return tuple(result)


def _placeholder(
    source: SimulationSource,
    text: str,
    path: Path,
    cancelled: Callable[[], bool],
    supplied: Callable[[str, Path], np.ndarray] | None,
) -> np.ndarray:
    _check(cancelled)
    if supplied is not None:
        result = supplied(text, path)
    else:
        from autodj.tts_command import run_command

        command = source.player._cfg.time_checks.tts_command
        if not command:
            raise ReviewError(f"{text}. Configure default TTS before generating this review")
        try:
            code = run_command(
                list(command),
                text,
                path,
                cancel=_Cancellation(cancelled),
                timeout=20,
                max_bytes=20 * SAMPLE_RATE * 8,
            )
            if code != 0:
                raise ReviewError(
                    f"{text}. Default TTS failed; correct its configuration and retry"
                )
            result = load_stereo(str(path), SAMPLE_RATE, max_seconds=20)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            _check(cancelled)
            raise ReviewError(
                f"{text}. Default TTS failed; correct its configuration and retry"
            ) from exc
    _check(cancelled)
    return _pcm(result)


def _encode(output: Path, cancelled: Callable[[], bool]) -> None:
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise ReviewError("Install FFmpeg to export review MP3 audio")
    command = [
        executable,
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(output / "review.wav"),
        "-map_metadata",
        "-1",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "192k",
        str(output / "review.mp3"),
    ]
    try:
        with subprocess.Popen(  # nosec B603 -- fixed argv, server-owned output path
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ) as child:
            bind_to_autodj(child)
            deadline = time.monotonic() + 120
            try:
                while child.poll() is None:
                    _check(cancelled)
                    if time.monotonic() >= deadline:
                        raise ReviewError("MP3 export timed out; retry a shorter review")
                    with suppress(subprocess.TimeoutExpired):
                        child.wait(timeout=0.1)
                _check(cancelled)
                if child.returncode:
                    raise ReviewError("MP3 export failed; check FFmpeg and available storage")
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait()
    except OSError as exc:
        raise ReviewError("MP3 export could not start; check FFmpeg and available storage") from exc


def _mix_overlays(
    audio: np.ndarray,
    overlays: list[tuple[int, np.ndarray]],
    duck_db: float,
    cancelled: Callable[[], bool],
) -> None:
    if not overlays:
        return
    overlays.sort(key=lambda item: item[0])
    bus = MixBus(BusEvents(lambda _: None, lambda _: None, lambda: None), lambda: (1, 1, 1))
    cursor, upcoming = 0, 0
    while cursor < len(audio):
        _check(cancelled)
        while upcoming < len(overlays) and overlays[upcoming][0] <= cursor:
            bus.play_liner(overlays[upcoming][1], duck_db)
            upcoming += 1
        until = min(len(audio), cursor + 882)
        if upcoming < len(overlays):
            until = min(until, overlays[upcoming][0])
        bus.BLOCK = until - cursor
        bus._duck_and_mix(audio[cursor:until])
        cursor = until


def _hard_cut(
    player: Any,
    track: RenderedTrack,
    audio: np.ndarray,
    at: int,
    cancelled: Callable[[], bool],
) -> np.ndarray:
    """The live bus's configured skip fade/effect, with no output or live callbacks."""
    at = max(0, min(at, len(audio)))
    if at == len(audio):
        return audio
    track = replace(track, audio=audio)
    bus = MixBus(
        BusEvents(lambda _: None, lambda _: None, lambda: None),
        lambda: (1, 1, 1),
        skip_tail=player._plan_skip,
    )
    bus.start_set()
    bus._current, bus._pos = track, at
    bus.skip()
    parts = [audio[:at]]
    while bus._current is not None:
        _check(cancelled)
        take = min(882, len(audio) - bus._pos)
        if bus._tail is not None:
            if bus._pos < bus._tail.start:
                take = min(take, bus._tail.start - bus._pos)
            else:
                take = min(882, len(bus._tail.audio) - bus._tail_pos)
        elif bus._skip_fade:
            take = min(take, bus._skip_fade)
        if take <= 0:
            break
        parts.append(bus._music(take, []))
    return join_audio(parts)


def render_review(
    source: SimulationSource,
    items: Sequence[Item],
    output: Path,
    *,
    settings: ReviewSettings | None = None,
    selection: tuple[float, float] | None = None,
    keep_complete: Sequence[str] = (),
    cancelled: Callable[[], bool] = lambda: False,
    placeholder: Callable[[str, Path], np.ndarray] | None = None,
) -> dict[str, Any]:
    """Mix frozen items privately, shorten the result, and export matching WAV/MP3."""
    _check(cancelled)
    settings = settings or ReviewSettings()
    if not items:
        raise ReviewError("Choose programme material to review")
    origin = min(item.starts for item in items)
    primary = [
        (index, item)
        for index, item in enumerate(items)
        if item.entry is not None
        and not getattr(item, "in_handoff", False)
        and item.kind in {"song", "liner", "clip"}
    ]
    expanded: list[tuple[int, Item]] = []
    previous_cut = False
    for index, item in primary:
        handoff = item.incoming_handoff
        if (
            (not expanded or previous_cut)
            and handoff is not None
            and (handoff.liners or handoff.breaks or handoff.ad_spots or handoff.intro is not None)
        ):
            # A one-frame silent predecessor lets the same saved-point mixer
            # place opening speech, with no live track or separate join algorithm.
            expanded.append(
                (-index - 1, replace(item, entry=None, incoming_handoff=None, cut_at=None))
            )
        expanded.append((index, item))
        previous_cut = item.cut_at is not None
    primary = expanded
    spans: list[ReviewSpan] = []
    protected: list[tuple[int, int]] = []
    positions: dict[int, int] = {}
    chunk_positions: dict[int, int] = {}
    overlays: list[tuple[int, np.ndarray]] = []
    omissions: list[dict[str, Any]] = []
    unknown_at: list[int] = []
    source_paths = {entry.path for entry in _entries(items)}
    source_extents: dict[str, tuple[int, int]] = {}
    cursor, offset, glide = 0, 0, None
    previous_overlap = 0

    def identity(index: int, item: Item) -> str:
        return getattr(item, "occurrence_id", "") or str(index)

    with (
        tempfile.TemporaryDirectory(prefix=".review-", dir=output) as scratch_name,
        ExitStack() as resources,
    ):
        scratch = Path(scratch_name)
        resources.enter_context(audio_workspace(scratch))
        player = _player(source, items, origin)
        spool = AudioSpool()
        resources.callback(spool.close)
        for position, (index, item) in enumerate(primary):
            if index >= 0:
                continue
            first = primary[position + 1][1]
            silent_path = scratch / f"opening{-index}.wav"
            sf.write(silent_path, np.zeros((1, 2), np.float32), SAMPLE_RATE, subtype="FLOAT")
            silence = replace(first.entry, path=str(silent_path), length=1 / SAMPLE_RATE)
            player._library.catalog._points[silence.path] = {"segue": 0.0, "end": 1 / SAMPLE_RATE}
            primary[position] = (index, replace(item, entry=silence))

        def missing(label: str, at: int, target_id: str, kind: str = "break") -> None:
            text = f"Missing review audio: {label}"
            voice = _placeholder(
                source, text, scratch / f"placeholder-{len(omissions)}.wav", cancelled, placeholder
            )
            overlays.append((at, voice))
            spans.append(ReviewSpan(target_id, label, kind, at, at + len(voice), True))
            protected.append((at, at + len(voice) + round(0.25 * SAMPLE_RATE)))
            omissions.append(
                {"id": target_id, "label": label, "reason": "Audio could not be resolved"}
            )

        for number, (index, item) in enumerate(primary):
            _check(cancelled)
            if item.playback_config is not None:
                player._cfg = copy.deepcopy(item.playback_config)
            positions[index] = cursor - previous_overlap
            chunk_positions[index] = cursor
            following = primary[number + 1][1] if number + 1 < len(primary) else None
            hard = item.cut_at is not None
            handoff = following.incoming_handoff if following is not None and not hard else None
            breaks = _ad_breaks(source, handoff) if handoff is not None else ()
            if item.audio is not None:
                chunk = _pcm(item.audio)
                rendered = None
                offset = following.incoming_offset if following is not None else 0
                glide = following.incoming_glide if following is not None else None
                # Captured PCM already includes the incoming head up to this buffer's end.
                next_start = round(
                    (
                        (
                            following.starts
                            if following is not None
                            else item.starts + len(chunk) / SAMPLE_RATE
                        )
                        - (item.audio_starts if item.audio_starts is not None else item.starts)
                    )
                    * SAMPLE_RATE
                )
                previous_overlap = max(0, len(chunk) - next_start) if following is not None else 0
            else:
                rendered = player._render_track(
                    item.entry,
                    following.entry if following is not None and not hard else None,
                    offset,
                    glide_in=glide,
                    liners=handoff.liners if handoff is not None else (),
                    breaks=breaks,
                    intro=handoff.intro if handoff is not None else None,
                    transition_order=(handoff.transition_order if handoff is not None else ()),
                    transition_items=(handoff.transition_items if handoff is not None else ()),
                )
                _check(cancelled)
                if rendered is None:
                    chunk = _placeholder(
                        source,
                        f"Missing review audio: {_label(item)}",
                        scratch / f"song-{index}.wav",
                        cancelled,
                        placeholder,
                    )
                    omissions.append(
                        {
                            "id": identity(index, item),
                            "label": _label(item),
                            "reason": "Source audio could not be decoded",
                        }
                    )
                    unknown_at.append(cursor)
                    offset, glide, previous_overlap = 0, None, 0
                else:
                    chunk = _pcm(rendered.audio, copy_audio=False)
                    offset, glide = rendered.next_start_offset, rendered.next_glide
                    previous_overlap = rendered.overlap_frames
            if hard:
                assert item.cut_at is not None
                cut_frame = round((item.cut_at - origin) * SAMPLE_RATE) - cursor
                skip_track = (
                    rendered
                    if rendered is not None
                    else RenderedTrack(
                        item.entry,
                        chunk,
                        None,
                        0,
                        "",
                        start_offset=item.incoming_offset,
                        glide_in=item.incoming_glide,
                        playback_rate=item.playback_rate,
                    )
                )
                chunk = _hard_cut(player, skip_track, chunk, cut_frame, cancelled)
                protected.append((cursor + max(0, cut_frame), cursor + len(chunk)))
                offset, glide, previous_overlap = 0, None, 0
            end = cursor + len(chunk)
            if (
                rendered is not None
                and getattr(item.entry, "_prepared_clip", False)
                and not hard
                and rendered.playback_rate == 1
            ):
                # A primary's navigation span also includes its outgoing join.
                # Prove retention against the actual normalized source instead.
                info = sf.info(item.entry.path)
                source_frames = round(info.frames * SAMPLE_RATE / info.samplerate)
                source_start = cursor - rendered.start_offset
                if source_start == positions[index] and source_start + source_frames <= end:
                    source_extents[identity(index, item)] = (
                        source_start,
                        source_start + source_frames,
                    )
            if index >= 0:
                spans.append(
                    ReviewSpan(
                        identity(index, item),
                        _label(item),
                        item.kind,
                        positions[index],
                        end,
                        identity(index, item) in keep_complete,
                    )
                )
            if previous_overlap:
                protected.append((end - previous_overlap, end))
            if rendered is not None:
                for event_number, (start, entry, _queued) in enumerate(rendered.liner_events):
                    points = player.saved_points(entry)
                    length = round(
                        ((points.get("end") or entry.length) - (points.get("start") or 0))
                        * SAMPLE_RATE
                    )
                    at = cursor + max(0, start)
                    clip_id = f"{identity(index, item)}-liner-{event_number}"
                    spans.append(
                        ReviewSpan(
                            clip_id,
                            entry.title or "Liner",
                            "liner",
                            at,
                            min(end, at + length),
                            clip_id in keep_complete,
                        )
                    )
                    protected.extend(
                        (
                            (at, at + round(0.25 * SAMPLE_RATE)),
                            (at + length, at + length + round(0.25 * SAMPLE_RATE)),
                        )
                    )
                voice_events = [
                    (
                        start,
                        key,
                        stop,
                        "break",
                        next((b.label for b in breaks if b.break_id == key), "Speech"),
                    )
                    for start, key, stop in rendered.break_events
                ]
                voice_events.extend(
                    (start, key, stop, "intro", label)
                    for start, key, stop, label in rendered.intro_events
                )
                for event_number, (start, key, stop, kind, label) in enumerate(voice_events):
                    at, until = cursor + max(0, start), cursor + min(len(chunk), stop)
                    if until > at:
                        prepared = any(
                            part.break_id == key and part.source == "prepared_edition"
                            for part in breaks
                        )
                        event_id = (
                            key if prepared else f"{identity(index, item)}-voice-{event_number}"
                        )
                        spans.append(
                            ReviewSpan(
                                event_id,
                                label,
                                kind,
                                at,
                                until,
                                key in keep_complete or event_id in keep_complete,
                            )
                        )
                        protected.extend(
                            (
                                (at, at + round(0.25 * SAMPLE_RATE)),
                                (until, until + round(0.4 * SAMPLE_RATE)),
                            )
                        )
                rendered_liners = {entry.path for _, entry, _ in rendered.liner_events}
                rendered_breaks = {key for _, key, _ in rendered.break_events}
                if handoff is not None:
                    for event_number, (entry, _) in enumerate(handoff.liners):
                        if entry.path not in rendered_liners:
                            missing(
                                entry.title or "Liner",
                                end - previous_overlap,
                                f"{index}-missing-liner-{event_number}",
                                "liner",
                            )
                    for entry in breaks:
                        source_paths.add(str(entry.path))
                        if entry.break_id not in rendered_breaks:
                            missing(entry.label or "Speech", end - previous_overlap, entry.break_id)
                    if handoff.intro is not None:
                        source_paths.add(str(handoff.intro.path))
                        if not rendered.intro_events:
                            missing(
                                handoff.intro.label,
                                end - previous_overlap,
                                handoff.intro.intro_id,
                                "intro",
                            )
            for start in range(0, len(chunk), 10 * SAMPLE_RATE):
                _check(cancelled)
                spool.write(chunk[start : start + 10 * SAMPLE_RATE].tobytes())
            cursor = end

        def location(item: Item) -> int:
            if getattr(item, "absolute_timing", False):
                return max(0, round((item.starts - origin) * SAMPLE_RATE))
            preceding = [
                (row.starts, positions[index])
                for index, row in primary
                if index in positions and row.starts <= item.starts
            ]
            when, at = max(preceding, default=(origin, 0))
            return max(0, at + round((item.starts - when) * SAMPLE_RATE))

        for index, item in enumerate(items):
            if index in positions:
                continue
            if getattr(item, "in_handoff", False) or item.kind == "skipped":
                continue
            at = location(item)
            if item.audio is not None:
                voice = _pcm(item.audio)
                overlays.append((at, voice))
                target_id = identity(index, item)
                spans.append(
                    ReviewSpan(
                        target_id,
                        _label(item),
                        item.kind,
                        at,
                        at + len(voice),
                        target_id in keep_complete,
                    )
                )
                protected.extend(
                    (
                        (at, at + round(0.25 * SAMPLE_RATE)),
                        (at + len(voice), at + len(voice) + round(0.25 * SAMPLE_RATE)),
                    )
                )
            else:
                missing(_label(item), at, identity(index, item), item.kind)
            if item.duration_unknown:
                unknown_at.append(at)
        frames = max(cursor, max((at + len(voice) for at, voice in overlays), default=0))
        if not frames:
            raise ReviewError("No playable audio was found for this review")
        padding = frames - cursor
        while padding:
            _check(cancelled)
            take = min(padding, 10 * SAMPLE_RATE)
            spool.write(bytes(take * 8))
            padding -= take
        raw = spool.buffer()
        if hasattr(raw, "close"):
            resources.callback(raw.close)
        full = np.ndarray((frames, 2), dtype=np.float32, buffer=raw)
        _mix_overlays(full, overlays, float(player._cfg.playback.liners_duck_db), cancelled)
        protected.extend(
            (
                chunk_positions[index]
                + round(
                    (left - (item.audio_starts if item.audio_starts is not None else item.starts))
                    * SAMPLE_RATE
                ),
                chunk_positions[index]
                + round(
                    (right - (item.audio_starts if item.audio_starts is not None else item.starts))
                    * SAMPLE_RATE
                ),
            )
            for index, item in enumerate(items)
            if index in chunk_positions
            for left, right in item.protected
        )
        protected = [
            (max(0, left), min(frames, right))
            for left, right in protected
            if min(frames, right) > max(0, left)
        ]
        selected = (
            None
            if selection is None
            else (round(selection[0] * SAMPLE_RATE), round(selection[1] * SAMPLE_RATE))
        )
        retained = shorten_review(
            full,
            spans,
            sample_rate=SAMPLE_RATE,
            settings=settings,
            protected=protected,
            selection=selected,
            output_path=scratch / "retained.pcm",
        )
        if isinstance(retained.audio, np.memmap):
            resources.callback(cast(Any, retained.audio)._mmap.close)
        _check(cancelled)
        sf.write(output / "review.wav", retained.audio, SAMPLE_RATE, subtype="PCM_24")
        _encode(output, cancelled)
        _check(cancelled)
        rows = []
        for span in spans:
            span_position = retained.item_positions.get(span.id)
            if span_position is None:
                continue
            source_proof = {}
            if (extent := source_extents.get(span.id)) is not None:
                source_first, source_last = extent
                covered = sum(
                    max(
                        0,
                        min(source_last, piece.programme_end)
                        - max(source_first, piece.programme_start),
                    )
                    for piece in retained.pieces
                    if piece.programme_start is not None and piece.programme_end is not None
                )
                source_proof = {
                    "source_seconds": (source_last - source_first) / SAMPLE_RATE,
                    "source_complete": covered == source_last - source_first,
                }
            rows.append(
                {
                    "id": span.id,
                    "label": span.label,
                    "kind": span.kind,
                    "complete": span.keep_complete,
                    **source_proof,
                    "programme_start": None
                    if any(at <= span.start_frame for at in unknown_at)
                    else span.start_frame / SAMPLE_RATE,
                    "programme_end": None
                    if any(at <= span.end_frame for at in unknown_at)
                    else span.end_frame / SAMPLE_RATE,
                    "preview_start": span_position[0] / SAMPLE_RATE,
                    "preview_end": span_position[1] / SAMPLE_RATE,
                }
            )
        pieces = []
        for piece in retained.pieces:
            values = asdict(piece)
            for key, value in values.items():
                if key != "kind" and value is not None:
                    values[key] = value / SAMPLE_RATE
            for key in ("programme_start", "programme_end"):
                frame = getattr(piece, key)
                if frame is not None and any(at <= frame for at in unknown_at):
                    values[key] = None
            pieces.append(values)
        effective = [
            None if any(at <= value for at in unknown_at) else value / SAMPLE_RATE
            for value in retained.effective_range
        ]
        duration = len(retained.audio) / SAMPLE_RATE
        del retained
        return {
            "items": sorted(rows, key=lambda row: row["preview_start"]),
            "pieces": pieces,
            "effective_range": effective,
            "duration": duration,
            "incomplete": bool(omissions),
            "omissions": omissions,
            "pre_mastering": True,
            "_source_paths": sorted(source_paths),
        }
