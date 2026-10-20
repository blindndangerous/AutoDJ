"""The hour preview: what plays next, when it starts, and how the hour lands (#126).

The preview lists what a station plays over the next hour (from 15 minutes
to three hours), each item with its estimated start time, and says which
of it is already fixed and which is a guess:

* fixed: the song on the air; what the render-ahead worker has rendered,
  with the liners and announcer breaks mixed into it; the song picked to
  follow it; the open song change and the breaks booked on it (#88), ad
  spots among them (#51); and the request queue, which plays before any
  pick; and the songs of a show (#106): the rest of the one on the air, and a
  scheduled one's from the first song after its time;
* predicted: what the station's own picker would choose after that, from a
  dry run (:mod:`autodj.rotation_sim`) that starts where the fixed part
  ends, with the station's clockwheel place, history and schedule; and the
  ad breaks due in the span, each at the first predicted song change at or
  after its time, and clockwheel ad steps before their selected song, with
  spots guessed by the ad schedule's own rules.

It then backtimes to each fixed point in the span: the top of each hour,
each clockwheel restart, each scheduled event and each ad break.  For
each it says how early or late the nearest song change lands, and which
song is the reason.

Nothing on the air changes.  The live player is only read: its renders,
its render cursor, the open song change and the queue, under the locks
their writers hold or checked for a consistent read.  The dry run picks
with a copy of the player that has its own clock, play history, clockwheel
place and random generator, so a preview never draws the station's random
choices or moves its clockwheel. Previews run on one worker thread below
normal priority, never on the event loop; each is kept for a few seconds
while its timeline and picking inputs stay current, and stops at a song
limit and a time limit.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import hashlib
import logging
import random
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Query, Request

from autodj import timefmt
from autodj.fsutil import file_signature
from autodj.identity_map import attached
from autodj.rotation_sim import (
    _PICKING_ACTIONS,
    _UNSIMULATED_ACTIONS,
    PlayerSnapshot,
    SimulationSource,
    _apply_event,
    _copy_player,
    _quiet_logs,
    _Schedule,
    average_length,
    lower_thread_priority,
    music_rate,
    snapshot_player,
    song_seconds,
)
from autodj.station_routes import StationDep, StationRouter
from autodj.stereo import SAMPLE_RATE
from autodj.timefmt import length_text

logger = logging.getLogger(__name__)

#: The span a preview covers when none is asked for, in minutes.
DEFAULT_MINUTES = 60
#: The shortest and longest span a preview may cover, in minutes.
MIN_MINUTES = 15
MAX_MINUTES = 180
#: The most songs the dry run picks for one preview.
MAX_PREDICTED = 80
#: The most queued items a preview lists.
MAX_QUEUED = 100
#: How long the dry run may work for one preview, in seconds; it then stops
#: with what it has, and the preview says so.  A soft limit: it is checked
#: before each pick, so one slow pick can run past it.
BUDGET_SECONDS = 4.0
#: How long a preview is kept while what is fixed stays the same, in
#: seconds: longer than the page's refresh (``REFRESH_MS`` in
#: ``static/modules/hour-preview.js``), so an open card that finds nothing
#: new fixed costs no dry run.
KEEP_SECONDS = 45.0
#: The most previews that may wait for the worker at once (each station and
#: span waits once: later asks share its answer).
MAX_WAITING = 8
#: The most fixed points (hours, clockwheel restarts, events) one preview backtimes to.
MAX_POINTS = 24
#: A song change this close to a fixed point lands on time.
ON_TIME_SECONDS = 1.0
# Tries at a consistent read of the render worker's place.
_READ_TRIES = 5
# The pause after each predicted pick, so the audio and web threads get the
# interpreter (as the rotation tester does).
_PICK_PAUSE_S = 0.002

#: What each status says, in words; "fixed" or "predicted" is always in the text.
STATUS_WORDS = {
    "on_air": "Fixed: on the air",
    "ready": "Fixed: ready to play",
    "picked": "Fixed: picked",
    "booked": "Fixed: booked break",
    "queued": "Fixed: in the queue",
    "show": "Fixed: from a show",
    "skipped": "Skipped: it would break a DMCA limit",
    "predicted": "Predicted",
}

# The schedule's actions the dry run follows: the picking changes, and shows.
_PREVIEW_ACTIONS = (
    _PICKING_ACTIONS | _UNSIMULATED_ACTIONS | {"show", "liner", "play_cart", "time_check", "stream"}
)

BREAK_KINDS = {
    "dj": "DJ",
    "news": "News",
    "weather": "Weather",
    "music_news": "Music news",
    "ad": "Ad",
}

NOTES = (
    "Predicted rows come from a dry run of the picker and change as real picks are made. "
    "All times are estimates. Chance-based rules and random clip choices may differ on air. "
    "Unbooked voice tracks, live DJs and manual playback changes cannot be predicted.",
)

NOTHING_PLAYING = "Nothing is playing, so there is nothing to time. Start playback first."


class PreviewError(ValueError):
    """A preview the server refuses: a span out of range."""


# ---------------------------------------------------------------------------
# What the station has decided
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IncomingHandoff:
    """Frozen selections for the transition INTO a song, before timing projection."""

    liners: tuple[tuple[Any, bool], ...] = ()
    intro: Any = None
    ad_spots: tuple[tuple[str, Any], ...] = ()
    breaks: tuple[Any, ...] = ()
    transition_order: tuple[str, ...] = ()
    transition_items: tuple[Any, ...] = ()


@dataclass(frozen=True)
class Item:
    """One thing the station plays.

    Attributes:
        kind: ``"song"``, ``"liner"``, ``"break"`` or ``"intro"`` (an intro
            matched to the next song, #127).
        starts: When it starts, in wall-clock seconds.
        seconds: For a song, how long until the next song starts; for a
            liner or break, its cue-trimmed duration.
        status: A key of :data:`STATUS_WORDS`.
        entry: The song or liner; ``None`` for a break or an intro.
        label: A break's kind and description, or an intro's label.
    """

    kind: str
    starts: float
    seconds: float
    status: str
    entry: Any = None
    label: str = ""
    playback_rate: float = 1.0
    audio: Any = None
    audio_starts: float | None = None
    incoming_handoff: IncomingHandoff | None = None
    duration_unknown: bool = False
    playback_config: Any = None
    protected: tuple[tuple[float, float], ...] = ()
    incoming_offset: int = 0
    incoming_glide: Any = None
    in_handoff: bool = False
    occurrence_id: str = ""
    cut_at: float | None = None
    absolute_timing: bool = False
    asset_id: str | None = None
    details_entry: Any = None

    @property
    def fixed(self) -> bool:
        return self.status != "predicted"


@dataclass(frozen=True)
class Decided:
    """What the live station has already decided, read once.

    Attributes:
        now: When it was read.
        items: The fixed items, in time order.
        unplayed: Songs and liners among them that have not started, with
            their start times: the dry run counts them as played.
        last: The last fixed song: the dry run picks what follows it.
        handoff: When the song after *last* starts.
        clock_cursor: The clockwheel place the pick after *last* starts from.
        clock_name: The clockwheel that place is in.
        clock_boundary: The clockwheel's next restart for that pick.
        reason: Why there is nothing to time, or ``None``.
    """

    now: float
    items: tuple[Item, ...] = ()
    unplayed: tuple[tuple[Any, float], ...] = ()
    last: Any = None
    handoff: float = 0.0
    clock_cursor: int | None = None
    clock_name: str | None = None
    clock_boundary: float | None = None
    reason: str | None = None
    last_rate: float = 1.0
    speed_signature: str = ""
    ad_heard: tuple[Any, ...] = ()
    waiting: tuple[Item, ...] = ()

    def signature(self) -> str:
        """What is fixed, without the times: the same while nothing new is decided."""
        parts = []
        for item in (*self.items, *self.waiting):
            identity = (
                item.entry.path
                if item.entry is not None
                else item.details_entry.path
                if item.details_entry is not None
                else item.asset_id or item.label
            )
            intro_identity = repr((item.asset_id, item.label)) if item.kind == "intro" else ""
            parts.append(f"{item.kind}:{item.status}:{identity}{intro_identity}")
        parts.append(f"clock:{self.clock_name}:{self.clock_cursor}")
        parts.append(f"speed:{self.speed_signature}")
        return "\n".join(parts)


def _remaining(
    player: Any, entry: Any, offset_s: float, average: float, *, rate: float | None = None
) -> float:
    """How long *entry* plays from *offset_s* seconds into its file until the next song."""
    try:
        seconds = song_seconds(player, entry, average)
        if offset_s > 0:
            begin = 0.0
            if player._cfg.playback.transition_mode == "saved_points":
                begin = player._clock_music_span(entry)[0]
            seconds -= max(0.0, offset_s - begin)
    except Exception:
        # A cue that cannot be read must not stop the preview: the whole song counts.
        seconds = float(entry.length or average) - max(0.0, offset_s)
    return max(0.0, seconds) / (music_rate(player, entry) if rate is None else rate)


def _preview_liner_group(
    player: Any,
    outgoing: Any,
    incoming: Any,
    liners: Sequence[Any],
    moment: float,
    *,
    outgoing_rate: float | None = None,
    incoming_rate: float | None = None,
    voice: tuple[Any, dict[str, Any], bool] | None = None,
    extra_voices: Sequence[tuple[Any, dict[str, Any], bool]] = (),
) -> tuple[list[tuple[Any, float, float]], float]:
    """Place cue-trimmed liner rows and return the incoming song's extra delay."""
    from autodj.playout import liner_chain_placement, liner_chain_timing, liner_main_frames

    sr = SAMPLE_RATE
    if outgoing_rate is None:
        outgoing_rate = music_rate(player, outgoing) if outgoing is not None else 1.0
    if incoming_rate is None:
        incoming_rate = music_rate(player, incoming)
    outgoing_rate = max(0.01, float(outgoing_rate))
    incoming_rate = max(0.01, float(incoming_rate))
    chain: list[tuple[Any, int, int]] = []
    timing: list[tuple[int, int, float | None, bool]] = []
    frames = 0
    spoken = {id(clip): (cues, allow) for clip, cues, allow in extra_voices}
    if voice:
        spoken[id(voice[0])] = (voice[1], voice[2])
    for entry in (*liners, *(clip for clip, _, _ in extra_voices), *((voice[0],) if voice else ())):
        cues = spoken[id(entry)][0] if id(entry) in spoken else player.saved_points(entry)
        start = max(0.0, float(cues.get("start") or 0.0))
        end = min(float(entry.length or 0.0), float(cues.get("end") or entry.length or 0.0))
        first, last = round(start * sr), round(end * sr)
        clip_frames = max(0, last - first)
        speech = liner_main_frames(first, clip_frames, cues.get("segue"))
        offset = frames
        record = player._library.catalog.record_for_path(entry.path) if player._library else None
        allow_outro = (
            spoken[id(entry)][1]
            if id(entry) in spoken
            else bool(record and record.get("allow_outro", False))
        )
        timing.append((clip_frames, speech, cues.get("overlap_start"), allow_outro))
        chain.append((entry, offset, clip_frames))
        frames += clip_frames

    incoming_points = player.saved_points(incoming)
    incoming_start = float(incoming_points.get("start") or 0.0)
    intro = max(0.0, float(incoming_points.get("intro") or incoming_start) - incoming_start)
    intro_frames = round(intro * sr / incoming_rate)
    main_frames, release, limit = liner_chain_timing(timing, intro_frames / sr)
    release = round(release * sr) / sr

    if outgoing is None:
        return [
            (entry, moment + offset / sr, clip_frames / sr) for entry, offset, clip_frames in chain
        ], release
    outgoing_start, segue = player._clock_music_span(outgoing)
    points = player.saved_points(outgoing)
    start_frame = round(outgoing_start * sr / outgoing_rate)
    segue_frame = round(segue * sr / outgoing_rate)
    outro = points.get("outro")
    safe_outro = max(
        start_frame,
        min(
            segue_frame,
            round(float(outro) * sr / outgoing_rate) if outro is not None else segue_frame,
        ),
    )
    liner_start, incoming_frame = liner_chain_placement(
        start_frame,
        segue_frame,
        safe_outro,
        main_frames,
        intro_frames,
        release,
        limit,
    )
    base = moment + (liner_start - segue_frame) / sr
    rows = [(entry, base + offset / sr, length / sr) for entry, offset, length in chain]
    return rows, max(0, incoming_frame - segue_frame) / sr


def _break_label(desk: Any, break_id: str) -> str:
    item = desk.get(break_id) if desk is not None else None
    if item is None:
        return "Break"
    kind = BREAK_KINDS.get(item.kind, item.kind.replace("_", " ").capitalize())
    return f"{kind} break: {item.label}" if item.label else f"{kind} break"


def _registered_liner(player: Any, path: Any) -> Any:
    """Return the catalog liner for this exact file, never a private intro path."""
    library = getattr(player, "_library", None)
    if library is None or not path:
        return None
    try:
        path = str(path)
        record = library.catalog.record_for_path(path)
        if record is None or record.get("kind") != "liner":
            return None
        entry = library.catalog.entry_for_path(path)
        return entry if entry is not None and str(entry.path) == str(path) else None
    except (AttributeError, OSError, sqlite3.Error, TypeError, ValueError):
        return None


def _mixed_in(
    track: Any,
    base: float,
    now: float,
    desk: Any,
    status: str,
    queued: list[Any],
    *,
    player: Any = None,
) -> list[Item]:
    """The liners, breaks and matched intros mixed into *track*, whose audio starts at *base*.

    Ones already over are left out; one under way is on the air.  Liners
    taken from the queue are added to *queued*.
    """
    found: list[Item] = []
    for frame, entry, from_queue in getattr(track, "liner_events", ()):
        at = base + frame / SAMPLE_RATE
        length = float(entry.length or 0.0)
        if at + length <= now:
            continue
        found.append(Item("liner", at, length, "on_air" if at <= now else status, entry))
        if from_queue:
            queued.append(entry)
    for frame, break_id, end in getattr(track, "break_events", ()):
        at = base + frame / SAMPLE_RATE
        length = max(0.0, (end - frame) / SAMPLE_RATE)
        if at + length <= now:
            continue
        label = _break_label(desk, break_id)
        found.append(Item("break", at, length, "on_air" if at <= now else status, label=label))
    assets = dict(getattr(track, "intro_assets", ()))
    for frame, intro_id, end, intro_label in getattr(track, "intro_events", ()):
        at = base + frame / SAMPLE_RATE
        length = max(0.0, (end - frame) / SAMPLE_RATE)
        if at + length <= now:
            continue
        label = f"Matched intro: {intro_label}" if intro_label else "Matched intro"
        found.append(
            Item(
                "intro",
                at,
                length,
                "on_air" if at <= now else status,
                label=label,
                asset_id=intro_id,
                details_entry=_registered_liner(player, assets.get(intro_id)),
            )
        )
    return [replace(item, in_handoff=True) for item in found]


@dataclass
class _Live:
    """The render worker's place, read together."""

    playing: Any
    position: int
    ready: Any
    pending: Any
    offset: int
    cursor: int | None
    clock_name: str | None
    boundary: float | None
    plan: Any
    consistent: bool


def _read_live(player: Any) -> _Live:
    """Read the playing render, the ready one, the cursor and the open song change.

    The render worker moves its cursor without a lock the page can take,
    so the read is repeated until the cursor's serial and the playing
    render are the same before and after it.  When the worker never holds
    still, only the playing render is trusted.
    """
    live: _Live | None = None
    for _attempt in range(_READ_TRIES):
        serial = getattr(player, "_cursor_serial", 0)
        playing, position, steady = _read_position(player)
        render_ahead = getattr(player, "_render_ahead", None)
        ready = render_ahead.ready() if render_ahead is not None else None
        plan = None
        slot_lock = getattr(player, "_slot_lock", None)
        if slot_lock is not None:
            with slot_lock:
                held = getattr(player, "_slot_plan", None)
                if held is not None and held.serial == serial and (held.pinned or not held.stale):
                    plan = replace(held)
        live = _Live(
            playing=playing,
            position=position,
            ready=ready,
            pending=getattr(player, "_pending_entry", None),
            offset=int(getattr(player, "_pending_offset", 0) or 0),
            cursor=getattr(player, "_pending_clock_cursor", None),
            clock_name=getattr(player, "_pending_clock_name", None),
            boundary=getattr(player, "_pending_clock_boundary", None)
            or getattr(player, "_clock_next_boundary", None),
            plan=plan,
            consistent=True,
        )
        if (
            steady
            and getattr(player, "_cursor_serial", 0) == serial
            and getattr(player, "_playing_render", None) is playing
        ):
            return live
        time.sleep(0.005)
    assert live is not None
    live.consistent = False
    return live


def _read_position(player: Any) -> tuple[Any, int, bool]:
    """The playing render and the position in it, read so that they belong together.

    The mix bus writes the position with no lock the page can take, and
    replaces the render first when a track starts.  So the render is read
    before and after the position, and the position must fall inside that
    render's audio.  Returns ``(render, position, steady)``; *steady* is
    false when no read agreed.
    """
    playing = getattr(player, "_playing_render", None)
    position = int(player._playback_pos[0])
    for _attempt in range(_READ_TRIES):
        playing = getattr(player, "_playing_render", None)
        position = int(player._playback_pos[0])
        again = getattr(player, "_playing_render", None)
        playback_rate = max(0.01, float(getattr(playing, "playback_rate", 1.0) or 1.0))
        if playing is again and (
            playing is None
            or playing.start_offset
            <= position
            <= playing.start_offset + round(len(playing.audio) * playback_rate)
        ):
            return playing, position, True
        time.sleep(0.002)
    return playing, position, False


def _last_record(player: Any) -> Any:
    with player._pick_lock:
        history = player._state.play_history
        return history[-1] if history else None


def _waiting(player: Any) -> list[Any]:
    state = player._state
    with state.queue_lock:
        return ([state.queued_next] if state.queued_next else []) + list(state.queue)


def _liner_paths(player: Any) -> set[str]:
    library = getattr(player, "_library", None)
    if library is None:
        return set()
    return {entry.path for entry in library.entries_snapshot("liner")}


@dataclass(frozen=True)
class Head:
    """What the live station has fixed ahead of the queue's own songs, read once (#217).

    :func:`read_station` times the queue after it for the preview, and the
    queue checks (DMCA limits on queued songs, listener requests and shows)
    time a queue with one song put in it, with :func:`time_queue`.

    Attributes:
        now: When it was read.
        current: The song on the air; *started*: when it started.
        items: The fixed items, in time order.
        unplayed: Songs and liners among them that have not started, with
            their start times.
        taken: The queue entries the renders and the open song change hold,
            in queue order.
        last: The last fixed song; *handoff*: when the song after it starts.
        clock: The clockwheel place, its name and its next restart, for the
            pick after *last*.
        waiting: The "play next" song and the queue, as read.
        average: How long a song without a length counts as.
        liners: The liner library's paths.
    """

    now: float
    current: Any
    started: float
    items: tuple[Item, ...]
    unplayed: tuple[tuple[Any, float], ...]
    taken: tuple[Any, ...]
    last: Any
    handoff: float
    clock: tuple[int | None, str | None, float | None]
    waiting: tuple[Any, ...]
    average: float
    liners: frozenset[str]
    ad_heard: tuple[Any, ...] = ()


def read_head(
    player: Any, desk: Any = None, *, now: float | None = None, capture_audio: bool = False
) -> Head | None:
    """The fixed part of what *player* will play, read without changing anything.

    ``None`` when nothing is playing.  Takes ``queue_lock`` only for a copy
    of the queue; the library is read with no lock held.
    """
    now = time.time() if now is None else now
    current = player._state.current_track
    if current is None:
        return None
    average = average_length(player.playable_entries())
    ads = getattr(player, "ad_desk", None)
    heard = ads.heard_snapshot() if ads is not None else []
    if getattr(player, "bus", None) is not None:
        found = _read_bus(player, desk, now, average, ads, heard, capture_audio=capture_audio)
    else:
        found = _read_browser(player, now, average)
    if found is None:
        return None
    items, unplayed, queued, last, handoff, clock = found
    return Head(
        now=now,
        current=current,
        started=items[0].starts if items else now,
        items=tuple(items),
        unplayed=tuple(unplayed),
        taken=tuple(queued),
        last=last,
        handoff=handoff,
        clock=clock,
        waiting=tuple(_waiting(player)),
        average=average,
        liners=frozenset(_liner_paths(player)),
        ad_heard=tuple(heard),
    )


def rough_head(player: Any, *, now: float | None = None) -> Head:
    """A :class:`Head` for when :func:`read_head` finds none: nothing fixed but the song on the air.

    The song on the air (if any) is timed from its recorded start and its
    cues; the queue follows it.  For checks that must answer even while the
    station is stopped or between songs.
    """
    now = time.time() if now is None else now
    state = player._state
    current = state.current_track
    average = average_length(player.playable_entries())
    started = handoff = now
    if current is not None:
        record = _last_record(player)
        if record is not None and record.path == current.path:
            started = record.started
        handoff = max(now, started + _remaining(player, current, 0.0, average))
    return Head(
        now=now,
        current=current,
        started=started,
        items=(),
        unplayed=(),
        taken=(),
        last=current,
        handoff=handoff,
        clock=(None, None, None),
        waiting=tuple(_waiting(player)),
        average=average,
        liners=frozenset(_liner_paths(player)),
    )


@dataclass(frozen=True)
class Timed:
    """A queue timed after a :class:`Head` (see :func:`time_queue`).

    Attributes:
        items: The fixed items still fixed, then the queue's.
        unplayed: The songs and liners not yet started, with their starts.
        starts: When each entry of the queue starts, in the queue's order
            (with an inserted song in its place); ``None`` for one past the
            limit, or a liner a render mixes in.
        last: The last song; *handoff*: when the song after it starts;
            *last_rate*: its music speed.
        timed: How many queue entries after the fixed part were timed.
    """

    items: tuple[Item, ...]
    unplayed: tuple[tuple[Any, float], ...]
    starts: tuple[float | None, ...]
    last: Any
    handoff: float
    last_rate: float
    timed: int


_Places = tuple[list[int | None], list[int | None]]


def _taken_places(head: Head, waiting: Sequence[Any]) -> _Places:
    """For each of *head*'s taken entries: its place in *waiting*, and its place in ``unplayed``.

    Places in *waiting* are matched by path, the first free one each, as
    the player matches them; ``None`` when it is not waiting any more.  A
    liner mixed into a render has no place in ``unplayed``.
    """
    free = list(range(len(waiting)))
    in_waiting: list[int | None] = []
    for used in head.taken:
        found = next((number for number in free if waiting[number].path == used.path), None)
        if found is not None:
            free.remove(found)
        in_waiting.append(found)
    in_unplayed: list[int | None] = []
    pointer = 0
    for used in head.taken:
        hit = next(
            (n for n in range(pointer, len(head.unplayed)) if head.unplayed[n][0] is used), None
        )
        if hit is not None:
            pointer = hit + 1
        in_unplayed.append(hit)
    return in_waiting, in_unplayed


def _first_stale(head: Head, waiting: Sequence[Any], index: int, places: _Places) -> int:
    """Where in ``unplayed`` an insertion at *index* of *waiting* makes the renders pick again.

    A render that took a queue entry at or after *index* (or one no longer
    waiting) picks again, as does one that picked a song by itself when
    nothing was left in the queue for it: the queue comes first.  A render
    whose song was picked while the queue had songs left (one with a booked
    break, say) stays.
    """
    in_waiting, in_unplayed = places
    by_unplayed = {n: q for q, n in enumerate(in_unplayed) if n is not None}
    for number, (entry, _starts) in enumerate(head.unplayed):
        if entry.path in head.liners:
            continue
        taken = by_unplayed.get(number)
        if taken is not None:
            place = in_waiting[taken]
            if place is None or place >= index:
                return number
            continue
        used = sum(
            1
            for q, place in enumerate(in_waiting)
            if place is not None and ((hit := in_unplayed[q]) is None or hit < number)
        )
        if used >= len(waiting):
            return number
    return len(head.unplayed)


def time_queue(
    player: Any,
    head: Head,
    waiting: Sequence[Any] | None = None,
    *,
    insert: tuple[int, Any] | None = None,
    limit: int | None = MAX_QUEUED,
) -> Timed:
    """Time *waiting* (the head's own copy when ``None``) after *head*, each entry with its start.

    The one playout timing the preview and the queue checks share (#217):
    the fixed part keeps the renders' own handoffs, at each render's own
    music speed, with the playing song's position and the liners mixed in;
    queue entries no render holds yet are timed from their saved start and
    segue cues (or the crossfade) at the configured music speed, and liners
    at their own speed with their overlap cues.

    With *insert*, ``(index, entry)``, *entry* is put at that index of
    *waiting* first, as a queue edit would.  The renders after it then pick
    again (see :func:`_first_stale`): the fixed part ends where the first of
    them starts, and the rest is timed from cues.  The bus plays an old
    render when the new one is not ready in time; the timing assumes it is.
    At most *limit* entries after the fixed part are timed.
    """
    waiting = list(head.waiting if waiting is None else waiting)
    places = _taken_places(head, waiting)
    in_waiting, in_unplayed = places
    cut = len(head.unplayed)
    order = list(waiting)
    index = len(waiting) + 1
    if insert is not None:
        index = max(0, min(insert[0], len(waiting)))
        cut = _first_stale(head, waiting, index, places)
        order.insert(index, insert[1])

    def moved(place: int) -> int:
        return place + 1 if place >= index else place

    starts: list[float | None] = [None] * len(order)
    held: set[int] = set()
    for taken, place in enumerate(in_waiting):
        if place is None:
            continue
        hit = in_unplayed[taken]
        if hit is None:
            held.add(moved(place))
        elif hit < cut:
            held.add(moved(place))
            starts[moved(place)] = head.unplayed[hit][1]
    items = list(head.items)
    unplayed = list(head.unplayed)
    last, moment = head.last, head.handoff
    liners = head.liners
    if cut < len(head.unplayed):
        moment = head.unplayed[cut][1]
        unplayed = unplayed[:cut]
        items = [item for item in items if item.starts < moment]
        last = next(
            (song for song, _at in reversed(unplayed) if song.path not in liners), head.current
        )
    last_rate = next(
        (
            item.playback_rate
            for item in reversed(items)
            if item.kind == "song" and item.entry is last
        ),
        music_rate(player, last) if last is not None else 1.0,
    )
    rest = [place for place in range(len(order)) if place not in held]
    if limit is not None:
        rest = rest[:limit]
    queued = [order[place] for place in rest]
    number = 0
    while number < len(queued):
        entry = queued[number]
        if entry.path in liners:
            end = number + 1
            while end < len(queued) and queued[end].path in liners:
                end += 1
            group = queued[number:end]
            incoming = queued[end] if end < len(queued) else None
            if (
                incoming is not None
                and incoming.path not in liners
                and player._cfg.playback.transition_mode == "saved_points"
            ):
                rows, delay = _preview_liner_group(
                    player,
                    last,
                    incoming,
                    group,
                    moment,
                    outgoing_rate=last_rate,
                    incoming_rate=music_rate(player, incoming),
                )
            else:
                rows, delay = [], 0.0
                for item in group:
                    length = float(item.length or 0.0)
                    rows.append((item, moment + delay, length))
                    delay += length
            for offset, (liner, liner_starts, seconds) in enumerate(rows):
                items.append(Item("liner", liner_starts, seconds, "queued", liner))
                unplayed.append((liner, liner_starts))
                starts[rest[number + offset]] = liner_starts
            moment += delay
            number = end
            continue
        seconds = _remaining(player, entry, 0.0, head.average)
        items.append(
            Item("song", moment, seconds, "queued", entry, playback_rate=music_rate(player, entry))
        )
        last_rate = music_rate(player, entry)
        unplayed.append((entry, moment))
        starts[rest[number]] = moment
        last = entry
        moment += seconds
        number += 1
    return Timed(
        items=tuple(items),
        unplayed=tuple(unplayed),
        starts=tuple(starts),
        last=last,
        handoff=moment,
        last_rate=last_rate,
        timed=len(queued),
    )


def history_at(
    history: Sequence[Any], head: Head, unplayed: Sequence[tuple[Any, float]], when: float
) -> list[Any]:
    """*history* as it will stand when a song starting at *when* starts (#217).

    The song on the air counts from its start when its play is not recorded
    yet, and each song of *unplayed* (liners left out) that starts before
    *when* counts from its start.
    """
    from autodj.repeat_rules import PlayRecord

    records = list(history)
    planned = [
        (starts, entry)
        for entry, starts in unplayed
        if entry.path not in head.liners and starts < when
    ]
    current = head.current
    if current is not None and not (records and records[-1].path == current.path):
        planned.insert(0, (min(head.started, when), current))
    planned.sort(key=lambda play: play[0])
    records.extend(PlayRecord.of(entry, starts) for starts, entry in planned)
    return records


def read_station(
    player: Any,
    desk: Any = None,
    *,
    now: float | None = None,
    show: tuple[str, Sequence[Any]] | None = None,
    capture_audio: bool = False,
) -> Decided:
    """What *player* has decided, read without changing anything.

    *desk* is the station's :class:`~autodj.breaks.BreakDesk`, for the
    breaks' names and the ones booked on the open song change.  *show* is
    the show on the air, its name and its songs not yet queued (#106): they
    follow the queue, fixed.
    """
    head = read_head(player, desk, now=now, capture_audio=capture_audio)
    if head is None:
        return Decided(time.time() if now is None else now, reason=NOTHING_PLAYING)
    timed = time_queue(player, head)
    items = list(timed.items)
    unplayed = list(timed.unplayed)
    last, moment = timed.last, timed.handoff
    if show is not None:
        name, entries_left = show
        for entry in list(entries_left)[: max(0, MAX_QUEUED - timed.timed)]:
            # As on the air: a song that would break a DMCA limit is skipped.
            if _dmca_breaks(player, head, unplayed, moment, entry):
                items.append(Item("skipped", moment, 0.0, "skipped", entry, name))
                continue
            seconds = _remaining(player, entry, 0.0, head.average)
            items.append(Item("song", moment, seconds, "show", entry, name))
            unplayed.append((entry, moment))
            last = entry
            moment += seconds
    cursor, clock_name, boundary = head.clock
    marks = attached(player, "_request_clock_steps")
    for item in sorted(items, key=lambda item: item.starts):
        if item.kind == "song" and item.status != "on_air" and item.entry is not None:
            cursor = marks.get(item.entry, cursor)
    return Decided(
        now=head.now,
        items=tuple(sorted(items, key=lambda item: item.starts)),
        unplayed=tuple(unplayed),
        last=last,
        handoff=moment,
        clock_cursor=cursor,
        clock_name=clock_name,
        clock_boundary=boundary,
        last_rate=timed.last_rate,
        speed_signature=str(getattr(player._cfg.playback, "music_speed_percent", 0.0)),
        ad_heard=head.ad_heard,
    )


_Found = tuple[
    list[Item],
    list[tuple[Any, float]],
    list[Any],
    Any,
    float,
    tuple[int | None, str | None, float | None],
]


def _dmca_breaks(
    player: Any, head: Head, unplayed: Sequence[tuple[Any, float]], when: float, entry: Any
) -> tuple[str, ...]:
    """The DMCA limits *entry* would break at *when*, after *unplayed*; none when the mode is off."""
    if not player._cfg.dmca.enabled:
        return ()
    from autodj.dmca import complement

    with player._pick_lock:
        history = list(player._state.play_history)
    records = history_at(history, head, unplayed, when)
    return tuple(complement(records, when, player._track_keys()).broken(entry))


def _captured_render(track: Any, base: float, player: Any) -> dict[str, Any]:
    """Own finished immutable render PCM, after all live snapshot locks are released."""
    from autodj.playout import DUCK_RAMP_SECONDS
    from autodj.stereo import copy_audio

    # RenderedTrack audio is immutable once handed to the bus (_music returns
    # read-only views). Copy outside its locks; allocation respects review budgets.
    owned = copy_audio(track.audio)
    spans = []
    for frame, entry, _queued in getattr(track, "liner_events", ()):
        spans.append(
            (
                max(base, base + frame / SAMPLE_RATE - DUCK_RAMP_SECONDS),
                base + frame / SAMPLE_RATE + float(entry.length or 0) + DUCK_RAMP_SECONDS,
            )
        )
    for frame, _key, end, *_label in (
        *getattr(track, "break_events", ()),
        *getattr(track, "intro_events", ()),
    ):
        spans.append(
            (
                max(base, base + frame / SAMPLE_RATE - DUCK_RAMP_SECONDS),
                base + end / SAMPLE_RATE + DUCK_RAMP_SECONDS,
            )
        )
    overlap = getattr(track, "overlap_frames", 0)
    if overlap:
        spans.append(
            (
                base + (len(track.audio) - overlap) / SAMPLE_RATE,
                base + len(track.audio) / SAMPLE_RATE,
            )
        )
    return {
        "audio": owned,
        "audio_starts": base,
        "incoming_offset": track.start_offset,
        "incoming_glide": copy.deepcopy(track.glide_in),
        "protected": tuple(spans),
        "playback_config": copy.deepcopy(player._cfg),
    }


def _read_bus(
    player: Any,
    desk: Any,
    now: float,
    average: float,
    ads: Any = None,
    heard: list[Any] | None = None,
    *,
    capture_audio: bool = False,
) -> _Found | None:
    """The fixed part when the server mixes the audio (server audio or a stream)."""
    live = _read_live(player)
    playing = live.playing
    if playing is None or playing.entry is not player._state.current_track:
        return None
    items: list[Item] = []
    unplayed: list[tuple[Any, float]] = []
    queued: list[Any] = []
    playback_rate = max(0.01, float(getattr(playing, "playback_rate", 1.0) or 1.0))
    frame = min(
        max(0, round((live.position - playing.start_offset) / playback_rate)),
        len(playing.audio),
    )
    base = now - frame / SAMPLE_RATE
    total = len(playing.audio) / SAMPLE_RATE
    record = _last_record(player)
    started = record.started if record is not None and record.path == playing.entry.path else base
    following = playing.next_entry
    if following is not None:
        enters = base + (len(playing.audio) - playing.overlap_frames) / SAMPLE_RATE
    else:
        enters = base + total
    items.append(
        Item(
            "song",
            started,
            max(0.0, enters - started),
            "on_air",
            playing.entry,
            playback_rate=playback_rate,
            **(_captured_render(playing, base, player) if capture_audio else {}),
        )
    )
    items.extend(_mixed_in(playing, base, now, desk, "ready", queued, player=player))
    clock = (live.cursor, live.clock_name, live.boundary)
    if following is None:
        return items, unplayed, queued, playing.entry, base + total, clock
    if playing.next_from_queue:
        queued.append(following)
    ready_at = base + total
    ready = live.ready
    if not (
        live.consistent
        and ready is not None
        and ready.entry is following
        and ready.next_entry is not None
        and live.pending is ready.next_entry
    ):
        # The next song's head is in the playing render; its own render is
        # under way (or the worker moved while it was read).
        if live.consistent and live.pending is following:
            offset_s = live.offset / SAMPLE_RATE
        else:
            offset_s = playing.next_start_offset / SAMPLE_RATE
            clock = (None, None, live.boundary)
        following_rate = music_rate(player, following)
        handoff = ready_at + _remaining(player, following, offset_s, average, rate=following_rate)
        items.append(
            Item(
                "song",
                enters,
                max(0.0, handoff - enters),
                "picked",
                following,
                playback_rate=following_rate,
                incoming_offset=playing.next_start_offset if capture_audio else 0,
                incoming_glide=copy.deepcopy(playing.next_glide) if capture_audio else None,
            )
        )
        unplayed.append((following, enters))
        return items, unplayed, queued, following, handoff, clock
    # The next song is rendered, with the head of the one after it.
    after = ready.next_entry
    ready_rate = max(0.01, float(getattr(ready, "playback_rate", 1.0) or 1.0))
    after_rate = max(
        0.01,
        float(getattr(ready, "next_playback_rate", music_rate(player, after)) or 1.0),
    )
    after_enters = ready_at + (len(ready.audio) - ready.overlap_frames) / SAMPLE_RATE
    items.append(
        Item(
            "song",
            enters,
            max(0.0, after_enters - enters),
            "ready",
            following,
            playback_rate=ready_rate,
            **(_captured_render(ready, ready_at, player) if capture_audio else {}),
        )
    )
    unplayed.append((following, enters))
    items.extend(_mixed_in(ready, ready_at, now, desk, "ready", queued, player=player))
    after_starts = ready_at + len(ready.audio) / SAMPLE_RATE
    handoff = after_starts + _remaining(
        player, after, live.offset / SAMPLE_RATE, average, rate=after_rate
    )
    items.append(
        Item(
            "song",
            after_enters,
            max(0.0, handoff - after_enters),
            "picked",
            after,
            playback_rate=after_rate,
            incoming_offset=ready.next_start_offset if capture_audio else 0,
            incoming_glide=copy.deepcopy(ready.next_glide) if capture_audio else None,
        )
    )
    unplayed.append((after, after_enters))
    if ready.next_from_queue:
        queued.append(after)
    plan = live.plan
    if plan is None or plan.ending is not after:
        return items, unplayed, queued, after, handoff, clock
    # The open song change (#88): the song after that, its liners and booked breaks.
    moment = handoff
    selected_breaks: list[Any] = []
    ad_spots: list[tuple[str, Any]] = []
    booked_breaks: list[Any] = []
    if desk is not None:
        from autodj.breaks import QUEUED

        for booked in desk.listing():
            if booked.slot_id == plan.slot_id and booked.state == QUEUED:
                if capture_audio:
                    selected_breaks.append(copy.deepcopy(booked))
                booked_breaks.append(booked)
    frozen = _frozen_transition_contents(plan, booked_breaks)
    handoff_liners = tuple(plan.liners)
    handoff_intro = getattr(plan, "intro", None)
    handoff_order: tuple[str, ...] = ()
    handoff_transition_items: tuple[Any, ...] = ()
    if frozen is None:
        for liner, from_queue in plan.liners:
            length = float(liner.length or 0.0)
            items.append(Item("liner", moment, length, "picked", liner, in_handoff=True))
            unplayed.append((liner, moment))
            if from_queue:
                queued.append(liner)
            moment += length
        for booked in booked_breaks:
            speech = max(0.0, float(booked.cues.speech))
            label = _break_label(desk, booked.break_id)
            items.append(Item("break", moment, speech, "booked", label=label, in_handoff=True))
            moment += speech
    else:
        ordered, handoff_liners, handoff_intro = frozen
        handoff_order = tuple(plan.transition_order)
        handoff_transition_items = tuple(plan.transition_items)
        for kind, selected in ordered:
            if kind == "liner":
                liner = selected
                length = float(liner.length or 0.0)
                from_queue = any(
                    entry.path == liner.path and source for entry, source in handoff_liners
                )
                items.append(Item("liner", moment, length, "picked", liner, in_handoff=True))
                unplayed.append((liner, moment))
                if from_queue:
                    queued.append(liner)
                moment += length
            elif kind == "break":
                speech = max(0.0, float(selected.cues.speech))
                label = _break_label(desk, selected.break_id)
                items.append(Item("break", moment, speech, "booked", label=label, in_handoff=True))
                moment += speech
            else:
                intro = selected
                speech = max(0.0, float(getattr(intro, "speech_s", 0.0) or 0.0))
                entry = _registered_liner(player, intro.path)
                if entry is not None and speech <= 0:
                    cue = player.saved_points(entry)
                    speech = max(
                        0.0,
                        (cue.get("segue") or cue.get("end") or entry.length)
                        - (cue.get("start") or 0.0),
                    )
                items.append(
                    Item(
                        "intro",
                        moment,
                        speech,
                        "picked",
                        label=f"Matched intro: {intro.label}",
                        in_handoff=True,
                        asset_id=intro.intro_id,
                        details_entry=entry,
                        duration_unknown=speech <= 0,
                    )
                )
                moment += speech
    if ads is not None and ads.refusal() is None and plan.ad_steps and heard is not None:
        booked_ads = desk is not None and any(
            booked.slot_id == plan.slot_id and booked.kind == "ad" for booked in desk.listing()
        )
        if not booked_ads:
            change_at = moment
            for step in plan.ad_steps:
                forecast = ads.forecast_step(step, change_at, heard)
                if forecast is not None:
                    if capture_audio:
                        ad_spots.extend(getattr(forecast, "spots", ()))
                    items.append(
                        Item(
                            "break",
                            moment,
                            forecast.seconds,
                            "predicted",
                            label=forecast.label,
                            in_handoff=True,
                        )
                    )
                    moment += forecast.seconds
    upcoming = plan.upcoming
    seconds = _remaining(player, upcoming, 0.0, average)
    items.append(
        Item(
            "song",
            moment,
            seconds,
            "picked",
            upcoming,
            playback_rate=music_rate(player, upcoming),
            incoming_handoff=IncomingHandoff(
                handoff_liners,
                handoff_intro,
                tuple(ad_spots),
                tuple(selected_breaks),
                handoff_order,
                handoff_transition_items,
            )
            if capture_audio
            else None,
            playback_config=copy.deepcopy(player._cfg) if capture_audio else None,
        )
    )
    unplayed.append((upcoming, moment))
    if plan.mode == "queue":
        queued.append(upcoming)
    clock = (plan.next_cursor, plan.clock_name, plan.boundary_after or live.boundary)
    return items, unplayed, queued, upcoming, moment + seconds, clock


def _frozen_transition_contents(
    plan: Any, breaks: Sequence[Any]
) -> tuple[list[tuple[str, Any]], tuple[tuple[Any, bool], ...], Any] | None:
    """Resolve a customized slot's frozen item order without choosing anything live."""
    order = tuple(getattr(plan, "transition_order", ()))
    transition_items = tuple(getattr(plan, "transition_items", ()))
    if not order or not transition_items:
        return None
    replaced = set(getattr(plan, "transition_replaced", ()))
    planned = {item.item_id: item for item in transition_items}
    liners = tuple(
        (item.entry, item.source in {"queued", "clockwheel"})
        for item in transition_items
        if item.kind == "liner" and item.item_id not in replaced and item.entry is not None
    )
    intro_item = next(
        (
            item
            for item in transition_items
            if item.kind == "matched_intro"
            and item.item_id not in replaced
            and item.intro is not None
        ),
        None,
    )
    intro = intro_item.intro if intro_item is not None else None
    liner_by_id = {
        item.item_id: item.entry
        for item in transition_items
        if item.kind == "liner" and item.item_id not in replaced and item.entry is not None
    }
    break_by_id = {f"break:{item.break_id}": item for item in breaks}
    selected: list[tuple[str, Any]] = []
    handled_liners: set[str] = set()
    handled_breaks: set[str] = set()
    used_intro = False
    for key in order:
        item = planned.get(key)
        if item is not None and item.kind == "liner" and key in liner_by_id:
            selected.append(("liner", liner_by_id[key]))
            handled_liners.add(key)
        elif item is not None and item.kind == "matched_intro" and item.intro is not None:
            if item.item_id not in replaced:
                selected.append(("intro", item.intro))
                used_intro = True
        elif key in break_by_id:
            selected.append(("break", break_by_id[key]))
            handled_breaks.add(key)
    # Match the shared renderer's fallback order for any content omitted from
    # the explicit order: selected liners, queued breaks, then the intro.
    selected.extend(
        ("liner", entry) for item_id, entry in liner_by_id.items() if item_id not in handled_liners
    )
    selected.extend(
        ("break", item) for key, item in break_by_id.items() if key not in handled_breaks
    )
    if intro is not None and not used_intro:
        selected.append(("intro", intro))
    return selected, liners, intro


def _read_browser(player: Any, now: float, average: float) -> _Found | None:
    """The fixed part when the page plays the audio: the song playing and the next one."""
    state = player._state
    current = state.current_track
    current_rate = music_rate(player, current)
    record = _last_record(player)
    noted = getattr(player, "_browser_started", None)
    if noted is not None and noted[0] is current:
        started = float(noted[1])
    elif record is not None and record.path == current.path:
        started = record.started
    else:
        rate = max(1, int(getattr(player, "_current_sr", SAMPLE_RATE) or SAMPLE_RATE))
        started = now - player._playback_pos[0] / rate / current_rate
    source_offset = player._playback_pos[0] / max(
        1, int(getattr(player, "_current_sr", SAMPLE_RATE) or SAMPLE_RATE)
    )
    remaining = _remaining(player, current, source_offset, average, rate=current_rate)
    full_length = _remaining(player, current, 0.0, average, rate=current_rate)
    ends = max(
        now,
        now + remaining if source_offset > 0 else started + full_length,
    )
    items = [Item("song", started, ends - started, "on_air", current, playback_rate=current_rate)]
    unplayed: list[tuple[Any, float]] = []
    clock = (
        getattr(player, "_pending_clock_cursor", None),
        getattr(player, "_pending_clock_name", None),
        getattr(player, "_pending_clock_boundary", None)
        or getattr(player, "_clock_next_boundary", None),
    )
    following = state.next_track
    if following is None:
        return items, unplayed, [], current, ends, clock
    seconds = _remaining(player, following, 0.0, average)
    items.append(
        Item(
            "song", ends, seconds, "picked", following, playback_rate=music_rate(player, following)
        )
    )
    unplayed.append((following, ends))
    # After a queue edit the page's next song is the queue's head, which
    # stays queued until it plays: it is listed once, as the next song.
    waiting = _waiting(player)
    queued = [following] if waiting and waiting[0].path == following.path else []
    return items, unplayed, queued, following, ends + seconds, clock


# ---------------------------------------------------------------------------
# The prediction
# ---------------------------------------------------------------------------


def predict(
    source: SimulationSource,
    snapshot: PlayerSnapshot,
    decided: Decided,
    end: float,
    *,
    seed: int,
    budget: float = BUDGET_SECONDS,
    limit: int | None = None,
    ad_runs: Sequence[Any] = (),
    preferred_choices: dict[str, str] | None = None,
    on_choice: Callable[[str, str], None] | None = None,
    capture_audio: bool = False,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[list[Item], float, str | None]:
    """Pick on from where *decided* ends until *end*, with a copy of the station's player.

    Returns the predicted items, when the song after the last of them
    starts, and why the run stopped short of *end* (``None`` when it did
    not): it picks at most *limit* songs (:data:`MAX_PREDICTED`) and stops
    picking once it has worked *budget* seconds, a soft limit checked
    before each pick.  The copy has its own clock, history,
    clockwheel place and random generator (seeded with *seed*): nothing on
    the air changes.
    """
    with _quiet_logs():
        songs = MAX_PREDICTED if limit is None else limit
        return _predict(
            source,
            snapshot,
            decided,
            end,
            seed,
            budget,
            songs,
            preferred_choices or {},
            on_choice,
            ad_runs,
            capture_audio,
            cancelled,
        )


def _interrupt_at(source: SimulationSource, event: Any, at: float, handoff: float) -> float:
    if event.action == "stream":
        if event.params.get("state") == "start":
            return handoff
        return at if event.timing == "hard" else handoff
    interrupts = (source.action_interrupts or {}).get(
        event.action, "none" if event.action == "play_cart" else "before"
    )
    if event.timing == "hard" and interrupts != "none" and handoff - at > event.wait_seconds:
        return at
    return handoff


def _scheduled_audio(
    source: SimulationSource,
    player: Any,
    checks: Any,
    event: Any,
    at: float,
    *,
    capture_audio: bool = False,
) -> Item | None:
    """Read the same clip and play dates as the action, without running it."""
    seconds = 0.0
    captured: list[Any] = []
    from autodj.stereo import copy_audio

    label = event.name
    if event.action == "liner":
        worker = player.liner_scheduler
        if worker is None or worker.refusal(event.params["name"], at) is not None:
            return None
        decoded = worker.decode(event.params["name"])
        seconds = len(decoded) / SAMPLE_RATE
        if capture_audio:
            captured.append(copy_audio(decoded))
        label = f"Scheduled liner: {label}"
    elif event.action == "play_cart":
        if source.carts is None:
            return None
        cart = source.carts.store.slot(int(event.params["page"]), int(event.params["slot"]))
        if cart.dates().refusal(datetime.fromtimestamp(at, ZoneInfo(source.timezone)).date()):
            return None
        decoded = source.carts.decode(cart) if capture_audio else None
        if decoded is not None:
            captured.append(copy_audio(decoded))
        seconds = (
            len(decoded) / SAMPLE_RATE
            if decoded is not None
            else float(cart.duration)
            if cart.duration is not None
            else len(source.carts.decode(cart)) / SAMPLE_RATE
        )
        label = f"Cart: {cart.name}"
    elif event.action == "time_check":
        if checks is None:
            return None
        options = {"on_audio": captured.append} if capture_audio else {}
        duration = checks.forecast_seconds(
            datetime.fromtimestamp(at, UTC), scheduled=True, **options
        )
        if duration is None and capture_audio:
            return Item(
                "break",
                at,
                0.0,
                "predicted",
                label=f"Time check: {label}",
                duration_unknown=True,
                absolute_timing=True,
            )
        if duration is None:
            raise PreviewError(f"the duration of time check {label} is not known yet")
        seconds = duration
        label = f"Time check: {label}"
    if (
        seconds > 0
        and event.action in {"liner", "time_check"}
        and player.liner_scheduler is not None
    ):
        with player.liner_scheduler._state():
            player.liner_scheduler._fired_locked(at=at)
    return (
        Item(
            "break",
            at,
            seconds,
            "predicted",
            label=label,
            audio=captured[0] if captured else None,
            absolute_timing=True,
        )
        if seconds > 0
        else None
    )


def _before_interrupt(source: SimulationSource, decided: Decided, end: float) -> Decided:
    """A hard event invalidates render-ahead after the song it interrupts."""
    schedule = _Schedule(source.events, source.timezone, decided.now, _PREVIEW_ACTIONS)
    for at, event in schedule.due(min(end, decided.handoff)):
        song = next(
            (
                row
                for row in decided.items
                if row.kind == "song" and row.starts <= at < row.starts + row.seconds
            ),
            None,
        )
        if song is None or _interrupt_at(source, event, at, song.starts + song.seconds) != at:
            continue
        items = tuple(
            replace(
                row,
                seconds=min(row.seconds, at - row.starts),
                cut_at=at if row.kind == "song" and row.starts + row.seconds > at else row.cut_at,
            )
            for row in decided.items
            if row.starts < at
        )
        return replace(
            decided,
            items=items,
            unplayed=tuple((entry, starts) for entry, starts in decided.unplayed if starts < at),
            last=song.entry,
            last_rate=song.playback_rate,
            handoff=at,
        )
    return decided


def _predict(
    source: SimulationSource,
    snapshot: PlayerSnapshot,
    decided: Decided,
    end: float,
    seed: int,
    budget: float,
    limit: int,
    preferred_choices: dict[str, str],
    on_choice: Callable[[str, str], None] | None,
    ad_runs: Sequence[Any] = (),
    capture_audio: bool = False,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[list[Item], float, str | None]:
    began = time.perf_counter()
    # Ad breaks not yet booked (#51), soonest first: each joins the first
    # predicted song change at or after its time.
    ads = deque(sorted(ad_runs, key=lambda run: run.at))
    current = decided.last
    handoff = decided.handoff
    if (current is None and not (capture_audio and decided.waiting)) or handoff >= end:
        return [], handoff, None
    current_rate = (
        max(0.01, float(decided.last_rate or music_rate(source.player, current)))
        if current is not None
        else 1.0
    )
    player = _copy_player(source, snapshot, decided.now, random.Random(seed), _ignore)
    # The callback resolves stable prepared occurrences, never track paths.
    player.prepared_handoff = getattr(  # type: ignore[attr-defined]
        source.player, "prepared_preview_handoff", None
    )
    live_intros = getattr(source.player, "matched_intros", None)
    if live_intros is not None and getattr(source.player, "bus", None) is not None:
        player.matched_intros = live_intros.snapshot()
    # Decode each voice clip once per prediction, never render music or touch live caches.
    player._load_voice_audio = lru_cache(maxsize=8)(player._load_voice_audio)  # type: ignore[method-assign]
    live_liners = getattr(source.player, "liner_scheduler", None)
    if live_liners is not None:
        player.liner_scheduler = live_liners.snapshot(
            clock=lambda: player._sim_now, wall_clock=lambda: player._sim_now, cfg=player._cfg
        )
    checks = source.time_checks.snapshot() if source.time_checks is not None else None
    heard = list(decided.ad_heard) or (
        getattr(source.ads, "heard_snapshot", lambda: [])() if source.ads is not None else []
    )
    entries = tuple(entry for entry in player.playable_entries() if not entry.is_silent)
    if not entries:
        return [], handoff, "the library has no songs to pick"
    average = average_length(entries)
    liners = {entry.path for entry in source.library.entries_snapshot("liner")}
    state = player._state
    ledger = player._ledger

    def record(entry: Any, at: float, *, track_start: bool = True) -> None:
        player._sim_now = at
        ledger.remember_play(entry.path, at)
        if entry.path in liners:
            scheduler = player.liner_scheduler
            if scheduler is not None and scheduler._planned_key:
                planned = source.library.record_for_id(scheduler._planned_name)
                if planned and planned["path"] == entry.path:
                    scheduler.mark_planned_fired(scheduler._planned_key)
            return
        state.record_played(entry)
        state.track_number += 1
        if track_start and player.liner_scheduler is not None:
            player.liner_scheduler.on_track_start()
        player._previous_track = state.current_track
        state.current_track = entry

    state.current_track = _on_air_song(decided)
    for entry, at in decided.unplayed:
        record(entry, at)
    if decided.clock_name is not None and decided.clock_name == (player._clock or {}).get("name"):
        player._pending_clock_cursor = decided.clock_cursor
        player._pending_clock_name = decided.clock_name
        player._pending_clock_boundary = decided.clock_boundary
    from autodj._bridge import PlayerBridge

    bridge = PlayerBridge(player=player, sim=player._sim, library=source.library, ledger=ledger)
    schedule = _Schedule(source.events, source.timezone, decided.now, _PREVIEW_ACTIONS)
    shows = getattr(source, "shows", None)
    scheduled = getattr(shows, "scheduled", None) or {}
    pending_events: list[tuple[float, Any]] = []
    # The songs of a scheduled show that has started, still to play.
    show_name = ""
    show_songs: list[Any] = []
    waiting = deque(decided.waiting)
    found: list[Item] = []
    stopped: str | None = None
    songs = 0
    choice_occurrences: dict[str, int] = {}
    while handoff < end:
        if cancelled is not None and cancelled():
            stopped = "review cancelled"
            break
        if songs >= limit and not capture_audio:
            stopped = "it reached its limit of songs"
            break
        if time.perf_counter() - began > budget:
            stopped = "it reached its time limit"
            break
        # A prepared pair uses the saved-cue mixer even when ordinary music
        # joins use another mode. Resolve this finite queued edge before
        # dispatching events, so its real segue is the scheduling boundary.
        queued_next = next((row.entry for row in waiting if row.kind == "song"), None)
        queued_breaks = (
            player._prepared_breaks(current, queued_next)
            if current is not None and queued_next is not None
            else ()
        )
        if queued_breaks and player._cfg.playback.transition_mode != "saved_points":
            previous = next((row for row in reversed(found) if row.kind == "song"), None)
            if previous is not None and previous.entry is current and previous.cut_at is None:
                begin, segue = player._clock_music_span(current)
                handoff = previous.starts + max(0.0, segue - begin) / current_rate
                found[found.index(previous)] = replace(previous, seconds=handoff - previous.starts)
        # As the station does: the pick reads the clock at the playing song's
        # start, and dayparting the hours at the next song's.
        player._sim_now = (
            handoff - song_seconds(player, current, average) / current_rate
            if current is not None
            else handoff
        )
        pending_events.extend(schedule.due(handoff))
        cut = min(
            (_interrupt_at(source, event, at, handoff) for at, event in pending_events),
            default=handoff,
        )
        if cut < handoff:
            found = [
                replace(
                    row,
                    seconds=min(row.seconds, cut - row.starts),
                    cut_at=cut if row.kind == "song" else row.cut_at,
                )
                if row.starts < cut < row.starts + row.seconds
                else row
                for row in found
                if row.starts < cut
            ]
            handoff = cut
        due_events = [(at, event) for at, event in pending_events if at <= handoff]
        pending_events = [(at, event) for at, event in pending_events if at > handoff]
        for at, event in due_events:
            if event.action == "stream":
                state = event.params.get("state")
                if state != "start":
                    stopped = f"scheduled stream {state} changes whether the station is on air"
                    break
                continue
            if event.action in {"liner", "play_cart", "time_check"}:
                airs = at if event.timing == "hard" and event.action == "play_cart" else handoff
                try:
                    audio = _scheduled_audio(
                        source, player, checks, event, airs, capture_audio=capture_audio
                    )
                except Exception as exc:
                    if capture_audio:
                        found.append(
                            Item(
                                "break",
                                airs,
                                0.0,
                                "predicted",
                                label=f"Scheduled audio unavailable: {event.name}",
                                duration_unknown=True,
                                absolute_timing=True,
                            )
                        )
                        continue
                    stopped = f"scheduled audio {event.name} could not be predicted: {exc}"
                    break
                if audio is not None:
                    found.append(audio)
                continue
            if event.action == "show":
                # As on the air: a show replaces the one before it.
                show_id = str(event.params.get("show", ""))
                if show_id.startswith(("edition:", "edition-confirmed:")):
                    stopped = f"scheduled prepared edition {event.name} cannot yet be predicted"
                    break
                show_name, entries = scheduled.get(show_id, ("", ()))
                show_songs = list(entries)
            else:
                refusal = _apply_event(player, bridge, event)
                if refusal and event.action in _UNSIMULATED_ACTIONS:
                    stopped = f"scheduled event {event.name} cannot be predicted: {refusal}"
                    break
        if stopped:
            break
        if songs >= limit:
            stopped = "it reached its limit of songs"
            break
        added: tuple[tuple[Any, bool], ...] = ()
        queued_row = None
        while waiting and waiting[0].kind == "liner":
            added += ((waiting.popleft().entry, True),)
        if waiting:
            queued_row = waiting.popleft()
        from_show = queued_row is None and bool(show_songs)
        if queued_row is not None:
            entry = queued_row.entry
        elif show_songs:
            entry = show_songs.pop(0)
            # As on the air: a song that would break a DMCA limit is skipped.
            limits = player._pick_context(at=handoff).complement
            if limits is not None and limits.broken(entry):
                found.append(Item("skipped", handoff, 0.0, "skipped", entry, show_name))
                continue
        else:
            context = player._pick_context(at=handoff)
            choice_base = _choice_key(player, current)
            occurrence = choice_occurrences.get(choice_base, 0)
            choice_occurrences[choice_base] = occurrence + 1
            choice_key = f"{choice_base}:{occurrence}"
            player._preview_preferred_path = preferred_choices.get(choice_key)  # type: ignore[attr-defined]
            try:
                if player._cfg.playback.transition_mode == "saved_points":
                    entry, _mode, added, cursor, _key = player._choose_station_next(
                        current, context, auto_liner=False
                    )
                    player._pending_clock_cursor = cursor
                else:
                    entry, _mode = player._choose_next(current, context)
            except Exception as exc:
                stopped = f"no song could be picked after {current.display_name}: {exc}"
                break
            if on_choice is not None:
                on_choice(choice_key, entry.path)
        moment = handoff
        ad_spots: list[tuple[str, Any]] = []
        while ads and ads[0].at <= handoff:
            run = ads.popleft()
            if capture_audio:
                ad_spots.extend(getattr(run, "spots", ()))
            if run.seconds > 0:  # a break no spot can fill airs nothing
                found.append(
                    Item(
                        "break", moment, run.seconds, "predicted", label=run.label, in_handoff=True
                    )
                )
                moment += run.seconds
        if (
            source.ads is not None
            and hasattr(source.ads, "forecast_step")
            and source.ads.refusal() is None
        ):
            change_at = moment
            for step in getattr(player, "_chosen_clock_ad_steps", ()):
                forecast = source.ads.forecast_step(step, change_at, heard)
                if forecast is not None:
                    if capture_audio:
                        ad_spots.extend(getattr(forecast, "spots", ()))
                    found.append(
                        Item(
                            "break",
                            moment,
                            forecast.seconds,
                            "predicted",
                            label=forecast.label,
                            in_handoff=True,
                        )
                    )
                    moment += forecast.seconds
        intro = None
        liner_key = ""
        prepared_breaks = (
            queued_breaks
            if entry is queued_next
            else player._prepared_breaks(current, entry)
            if current is not None
            else ()
        )
        saved_handoff = player._cfg.playback.transition_mode == "saved_points" or bool(
            prepared_breaks
        )
        if saved_handoff:
            breaks: tuple[Any, ...] = prepared_breaks + (
                (SimpleNamespace(kind="ad"),) if moment > handoff else ()
            )
            if current is None:
                intro = player._matched_intro(entry, breaks, handoff) if not added else None
            else:
                added, liner_key, intro = player._transition_voice(
                    current, entry, added, breaks, handoff
                )
        selected_handoff = (
            IncomingHandoff(tuple(added), intro, tuple(ad_spots), prepared_breaks)
            if capture_audio
            else None
        )
        voice: tuple[Any, dict[str, Any], bool] | None = None
        intro_label = intro.label if intro is not None else ""
        if intro is not None:
            intro_liner = _registered_liner(player, intro.path)

            def capture_voice(
                _audio: Any,
                cues: dict[str, Any],
                length: float,
                allow_outro: bool,
                chosen: Any = intro,
            ) -> tuple[int, int]:
                nonlocal voice
                start = max(0.0, float(cues.get("start") or 0.0))
                end = min(float(length), float(cues.get("end") or length))
                first, last = round(start * SAMPLE_RATE), round(end * SAMPLE_RATE)
                clip_frames = max(0, last - first)
                from autodj.playout import liner_main_frames

                speech_frames = liner_main_frames(first, clip_frames, cues.get("segue"))
                clip = SimpleNamespace(
                    path=str(chosen.path),
                    length=length,
                    speech_seconds=speech_frames / SAMPLE_RATE,
                )
                voice = (clip, cues, allow_outro)
                return 0, speech_frames

            rendered_intro = player._add_intro(intro, capture_voice)
            if not rendered_intro:
                found.append(
                    Item(
                        "intro",
                        moment,
                        0.0,
                        "predicted",
                        label=f"Matched intro unavailable: {intro_label or 'Matched intro'}",
                        asset_id=intro.intro_id,
                        details_entry=intro_liner,
                    )
                )
            if voice is None:
                player.matched_intros.release((intro.intro_id,))
        prepared_voices: list[tuple[Any, dict[str, Any], bool]] = []
        if prepared_breaks:
            from autodj.player import _music_output_points

            def capture_prepared(
                _audio: Any,
                cues: dict[str, Any],
                length: float,
                allow_outro: bool,
                captured: list[tuple[Any, dict[str, Any], bool]] = prepared_voices,
            ) -> tuple[int, int]:
                clip = SimpleNamespace(path="", length=length)
                captured.append((clip, cues, allow_outro))
                return 0, round((float(cues["segue"]) - float(cues["start"])) * SAMPLE_RATE)

            spoken = player._add_breaks(
                prepared_breaks,
                _music_output_points(player.saved_points(entry), music_rate(player, entry)),
                capture_prepared,
            )
            by_id = {item.break_id: item for item in prepared_breaks}
            for (clip, _cues, _allow), (_at, break_id, _end) in zip(
                prepared_voices, spoken, strict=True
            ):
                clip.path = str(by_id[break_id].path)
                clip.label = by_id[break_id].label
            decoded = {break_id for _at, break_id, _end in spoken}
            for item in prepared_breaks:
                if item.break_id not in decoded:
                    found.append(
                        Item(
                            "break",
                            moment,
                            0.0,
                            "predicted",
                            label=f"Unavailable prepared speech: {item.label}",
                            duration_unknown=True,
                        )
                    )
        next_rate = music_rate(player, entry)
        if (added or voice or prepared_voices) and saved_handoff:
            rows, delay = _preview_liner_group(
                player,
                current,
                entry,
                [liner for liner, _ in added],
                moment,
                outgoing_rate=current_rate,
                incoming_rate=next_rate,
                voice=voice,
                extra_voices=prepared_voices,
            )
            for liner, starts, length in rows:
                if any(liner is clip for clip, _, _ in prepared_voices):
                    found.append(
                        Item(
                            "break", starts, length, "predicted", label=liner.label, in_handoff=True
                        )
                    )
                elif voice and liner is voice[0]:
                    assert intro is not None
                    speech = float(voice[0].speech_seconds)
                    found.append(
                        Item(
                            "intro",
                            starts,
                            speech,
                            "predicted",
                            label=f"Matched intro: {intro_label}",
                            in_handoff=True,
                            asset_id=intro.intro_id,
                            details_entry=intro_liner,
                        )
                    )
                else:
                    found.append(Item("liner", starts, length, "predicted", liner, in_handoff=True))
                    record(liner, starts)
            moment += delay
        else:
            for liner, _queued in added:
                length = float(liner.length or 0.0)
                found.append(Item("liner", moment, length, "predicted", liner, in_handoff=True))
                record(liner, moment)
                moment += length
        player._sim_now = moment
        if liner_key and added and player.liner_scheduler is not None:
            player.liner_scheduler.mark_planned_fired(liner_key)
        scheduler = player.liner_scheduler
        automatic = scheduler is not None and not scheduler._planner_mode()
        points = player.saved_points(entry)
        track = SimpleNamespace(
            entry=entry,
            start_offset=float(points.get("start", 0) or 0) * SAMPLE_RATE,
            glide_in=None,
            next_entry=None,
            overlap_frames=0,
        )
        time_check = 0.0
        if checks is not None and scheduler is not None:
            now = datetime.fromtimestamp(moment, UTC)
            checks.tick(now, True)
            # A planned talk-up already on air leaves the announcement pending.
            if not (liner_key and added):
                captured_check: list[Any] = []
                options = {"on_audio": captured_check.append} if capture_audio else {}
                duration = checks.forecast_seconds(now, **options)
                if duration is None and capture_audio:
                    found.append(
                        Item(
                            "break",
                            moment,
                            0.0,
                            "predicted",
                            label="Time check",
                            duration_unknown=True,
                        )
                    )
                    duration = 0.0
                if duration is None:
                    stopped = "the duration of the automatic time check is not known yet"
                    break
                time_check = duration
                if duration > 0:
                    cue = scheduler._intro_cue(track, round(duration * SAMPLE_RATE))
                    starts = moment + (cue.frame / SAMPLE_RATE if cue is not None else 0)
                    found.append(
                        Item(
                            "break",
                            starts,
                            duration,
                            "predicted",
                            label="Time check",
                            audio=captured_check[0] if captured_check else None,
                        )
                    )
        record(entry, moment, track_start=not automatic or time_check > 0)
        if time_check > 0:
            with scheduler._state():
                scheduler._fired_locked(at=moment)
        current_rate = next_rate
        seconds = song_seconds(player, entry, average) / current_rate
        if automatic:
            until = min(moment + seconds, end)
            upcoming_events = _Schedule(source.events, source.timezone, moment, _PREVIEW_ACTIONS)
            until = min(
                (
                    _interrupt_at(source, event, at, until)
                    for at, event in upcoming_events.due(until)
                ),
                default=until,
            )
            captured_liners: dict[tuple[float, str], Any] = {}

            def capture_liner(
                at: float,
                name: str,
                audio: Any,
                captured: dict[tuple[float, str], Any] = captured_liners,
            ) -> None:
                captured[(at, name)] = audio

            liner_options = {"on_audio": capture_liner} if capture_audio else {}
            for at, name, length in scheduler.forecast(
                moment, until, track=track, track_start=time_check <= 0, **liner_options
            ):
                found.append(
                    Item(
                        "break",
                        at,
                        length,
                        "predicted",
                        label=f"Automatic liner: {name}",
                        audio=captured_liners.get((at, name)),
                    )
                )
        found.append(
            Item(
                "song",
                moment,
                seconds,
                queued_row.status
                if queued_row is not None
                else "show"
                if from_show
                else "predicted",
                entry,
                queued_row.label if queued_row is not None else show_name if from_show else "",
                playback_rate=current_rate,
                incoming_handoff=selected_handoff,
                playback_config=copy.deepcopy(player._cfg) if capture_audio else None,
            )
        )
        current, handoff = entry, moment + seconds
        songs += 1
        time.sleep(_PICK_PAUSE_S)
    return found, handoff, stopped


def _ignore(_event: str, _detail: Any) -> None:
    """The dry run's pick observer: the preview counts nothing."""


def _choice_key(player: Any, current: Any) -> str:
    """Identify a logical pick position so changed fixed prefixes do not shift preferences."""
    if not player._pure_shuffle and player._cfg.playback.pick_mode == "clock":
        clock = player._clock or {}
        name = getattr(player, "_pending_clock_name", None) or clock.get("name")
        cursor = getattr(player, "_pending_clock_cursor", None)
        if cursor is None:
            cursor = clock.get("cursor")
        boundary = getattr(player, "_pending_clock_boundary", None)
        return f"clock:{name}:{cursor}:{boundary}"
    mode = "random" if player._pure_shuffle else player._cfg.playback.pick_mode
    return f"{mode}:{current.path}"


def _on_air_song(decided: Decided) -> Any:
    """The song on the air when *decided* was read, or ``None``."""
    return next(
        (item.entry for item in decided.items if item.kind == "song" and item.status == "on_air"),
        None,
    )


# ---------------------------------------------------------------------------
# Fixed points and backtiming
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Point:
    """A time the hour is timed to.

    Attributes:
        kind: ``"hour"`` (the top of the hour, when Fit songs to the hour
            is on), ``"restart"`` (the clockwheel restarts, at the top of
            the hour or not) or ``"event"`` (a scheduled event or ad break).
        at: When, in wall-clock seconds.
        label: Its name, such as "Clockwheel restart" or an event's name.
        timing: An event's ``"hard"`` or ``"soft"``; empty otherwise.
        detail: What an event does, in words.
        wait_seconds: How long a hard event lets the playing song finish.
    """

    kind: str
    at: float
    label: str
    timing: str = ""
    detail: str = ""
    wait_seconds: int = 0
    interrupts: bool = True


def fixed_points(
    now: float,
    end: float,
    zone: ZoneInfo,
    *,
    resync_minutes: Sequence[int] = (),
    events: Sequence[Any] = (),
    describe: Callable[[Any], str] | None = None,
    hours: bool = False,
    action_interrupts: Mapping[str, str] | None = None,
) -> list[Point]:
    """The clockwheel restarts and scheduled events after *now*, up to *end*.

    *resync_minutes* are the active clockwheel's restart minutes (none when
    the station is not playing a clockwheel).  *hours* adds each top of the
    hour, which the station aims at when Fit songs to the hour is on; a
    restart at the top of the hour replaces it.  Clockwheel restarts are read
    in the computer's time zone, as the player reads them.  Each kind is
    bounded by :data:`MAX_POINTS`; the preview times the first
    :data:`MAX_POINTS` of them all and says when there were more.
    """
    found: dict[float, Point] = {}
    top = datetime.fromtimestamp(now, zone).replace(minute=0, second=0, microsecond=0)
    moment = top.timestamp()
    while hours and len(found) < MAX_POINTS:
        moment += 3600
        for _ in range(61):
            wall = datetime.fromtimestamp(moment, zone)
            if wall.minute == 0 and wall.second == 0:
                break
            moment += 60
        else:
            break
        if moment > end:
            break
        if moment > now:
            found[moment] = Point("hour", moment, "Top of the hour")
    if resync_minutes:
        from autodj.clockwheel import next_boundary

        cursor = now
        for _ in range(MAX_POINTS):
            try:
                boundary = next_boundary(
                    datetime.fromtimestamp(cursor).astimezone(), list(resync_minutes)
                )
            except ValueError:
                break
            if boundary is None or boundary.timestamp() > end:
                break
            cursor = boundary.timestamp()
            label = (
                "Top of the hour, clockwheel restart"
                if boundary.minute == 0
                else "Clockwheel restart"
            )
            found[cursor] = Point("restart", cursor, label)
    points = sorted(found.values(), key=lambda point: point.at)
    from autodj.scheduler import next_run

    after = datetime.fromtimestamp(now, UTC)
    for event in events:
        if not getattr(event, "enabled", False):
            continue
        cursor_at = after
        for _ in range(MAX_POINTS):
            try:
                when = next_run(event, cursor_at, zone)
            except (ValueError, OverflowError, OSError):
                break
            if when is None or when.timestamp() > end:
                break
            detail = _described(event, describe)
            points.append(
                Point(
                    "event",
                    when.timestamp(),
                    event.name,
                    event.timing,
                    detail,
                    getattr(event, "wait_seconds", 0),
                    (action_interrupts or {}).get(event.action) != "none",
                )
            )
            cursor_at = when
    points.sort(key=lambda point: (point.at, point.kind != "event"))
    return points


def _described(event: Any, describe: Callable[[Any], str] | None) -> str:
    """What *event* does, in words; its action's name when that cannot be said."""
    if describe is not None:
        try:
            return describe(event)
        except Exception:  # a removed action or profile must not stop the preview
            logger.debug("Could not describe scheduled event %s", event.name, exc_info=True)
    return str(event.action)


#: A liner or break this close before a song opens that song's change.
_JOINED_S = 0.5


def song_changes(timeline: Sequence[Item]) -> list[tuple[float, Item]]:
    """Each song in *timeline* with the time its song change starts.

    Liners and breaks that run straight into a song open its change, so
    the change starts with the first of them; otherwise it starts with the
    song.  A liner mixed over a song (as rendered liners are) does not.
    """
    changes: list[tuple[float, Item]] = []
    run_start: float | None = None
    run_end = 0.0
    floor = float("-inf")
    for item in sorted(timeline, key=lambda found: found.starts):
        if item.kind != "song":
            if run_start is None or abs(item.starts - run_end) > _JOINED_S:
                run_start = item.starts
            run_end = item.starts + item.seconds
            continue
        joined = (
            run_start is not None and run_start >= floor and abs(item.starts - run_end) <= _JOINED_S
        )
        changes.append((run_start if joined and run_start is not None else item.starts, item))
        floor = item.starts
        run_start = None
    return changes


@dataclass(frozen=True)
class Landing:
    """How the song changes fall around a fixed point.

    Attributes:
        point: The fixed point.
        playing: The song playing at it (the reason it lands early or late).
        began: When the song change into *playing* started.
        starts: When the next song change after it starts, or ``None`` when
            the preview does not reach it.
        offset: Seconds early (negative) or late (positive) the hour lands
            there; 0 on time.
        cut: Seconds a hard event cuts *playing* short.
    """

    point: Point
    playing: Item | None
    began: float | None
    starts: float | None
    offset: float
    cut: float = 0.0


def land(point: Point, changes: Sequence[tuple[float, Item]], ends: float) -> Landing:
    """Backtime to *point* over *changes* (from :func:`song_changes`); the last song ends at *ends*.

    A soft event waits for the song playing to end, so it runs late by
    what is left of it.  A hard event runs on time and cuts that song
    short.  At a clockwheel restart (which the station makes at the
    nearer song change), the nearer song change
    counts: early when the change into the song playing started closer
    before it.
    """
    at = point.at
    playing: Item | None = None
    began: float | None = None
    following: float | None = None
    for change, song in changes:
        if change <= at:
            playing, began = song, change
        elif following is None:
            following = change
    if following is None and ends > at:
        following = ends
    if playing is None or began is None or following is None:
        return Landing(point, playing, began, following, 0.0)
    late = following - at
    early = at - began
    if (
        point.kind == "event"
        and point.timing == "hard"
        and point.interrupts
        and late > point.wait_seconds
    ):
        cut = late if late >= ON_TIME_SECONDS else 0.0
        return Landing(point, playing, began, following, 0.0, cut)
    offset = late if point.kind == "event" or late <= early else -early
    if abs(offset) < ON_TIME_SECONDS:
        offset = 0.0
    return Landing(point, playing, began, following, offset)


# ---------------------------------------------------------------------------
# Time text
# ---------------------------------------------------------------------------


def _wall(stamp: float, zone: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(round(stamp), zone)


def clock_text(stamp: float, zone: ZoneInfo) -> str:
    """ "14:04:30", or "14:00" on a whole minute."""
    moment = _wall(stamp, zone)
    return timefmt.clock_text(moment, seconds=bool(moment.second))


def item_title(item: Item) -> str:
    """ "Artist, Title" for a song (a comma, which a screen reader pauses on)."""
    if item.kind == "break":
        return item.label or "Break"
    if item.kind == "intro":
        return item.label or "Matched intro"
    entry = item.entry
    artist = (getattr(entry, "artist", "") or "").strip()
    title = (getattr(entry, "title", "") or "").strip()
    if not title:
        title = getattr(entry, "display_name", "") or "Unknown"
    name = f"{artist}, {title}" if artist else title
    return f"Liner: {name}" if item.kind == "liner" else name


def _who(item: Item) -> str:
    return f"{item_title(item)} ({STATUS_WORDS[item.status].lower()})"


def landing_words(landing: Landing, zone: ZoneInfo) -> tuple[str, str]:
    """``(short, sentence)``: numeric timing for *landing* and why it lands that way."""
    point = landing.point
    heading = landing_heading(landing, zone)
    playing = landing.playing
    if playing is None or landing.began is None or landing.starts is None:
        return "not known", f"{heading}: past the end of the preview."
    after = clock_text(landing.starts, zone)
    if point.kind == "event" and point.timing == "hard" and landing.offset == 0:
        if landing.cut:
            cut = length_text(landing.cut)
            return (
                f"on time, cuts {cut}",
                f"{heading}: expected on time, cuts {_who(playing)} by {cut}.",
            )
        return "on time", f"{heading}: expected on time, between songs."
    if landing.offset == 0:
        nearest = min((landing.began, landing.starts), key=lambda moment: abs(moment - point.at))
        return "on time", f"{heading}: expected on time, song change {clock_text(nearest, zone)}."
    amount = length_text(abs(landing.offset))
    if point.kind == "event":
        return (
            f"{amount} late",
            f"{heading}: expected {amount} late, after {_who(playing)} ends {after}.",
        )
    if landing.offset > 0:
        return (
            f"{amount} late",
            f"{heading}: expected {amount} late. {_who(playing)} runs over, next change {after}.",
        )
    past = length_text(landing.starts - point.at)
    return (
        f"{amount} early",
        f"{heading}: expected {amount} early. {_who(playing)} starts "
        f"{clock_text(landing.began, zone)}, runs {past} past.",
    )


def landing_heading(landing: Landing, zone: ZoneInfo) -> str:
    """ "Clockwheel restart, 13:30" or "News (Play a liner: news.mp3), 12:30, hard"."""
    point = landing.point
    when = clock_text(point.at, zone)
    if point.kind != "event":
        return f"{point.label}, {when}"
    name = f"{point.label} ({point.detail})" if point.detail else point.label
    return f"{name}, {when}, {point.timing}"


# ---------------------------------------------------------------------------
# The whole preview
# ---------------------------------------------------------------------------


def check_minutes(minutes: int) -> int:
    """*minutes*, or :class:`PreviewError` in words when it is out of range."""
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise PreviewError("The span must be a whole number of minutes")
    if not MIN_MINUTES <= minutes <= MAX_MINUTES:
        raise PreviewError(f"The span must be from {MIN_MINUTES} to {MAX_MINUTES} minutes")
    return minutes


def seed_for(signature: str) -> int:
    """The dry run's seed: the same while what is fixed stays the same, so a refresh is steady."""
    return int.from_bytes(hashlib.sha256(signature.encode("utf-8")).digest()[:4], "big")


def build_preview(
    source: SimulationSource,
    minutes: int = DEFAULT_MINUTES,
    *,
    desk: Any = None,
    now: float | None = None,
    decided: Decided | None = None,
    describe: Callable[[Any], str] | None = None,
    budget: float = BUDGET_SECONDS,
    preferred_choices: dict[str, str] | None = None,
    on_prediction: Callable[[tuple[tuple[str, str], ...]], None] | None = None,
    capture_audio: bool = False,
    on_items: Callable[[tuple[Item, ...]], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """The preview of *source*'s next *minutes*, as the page reads it (JSON shape).

    Blocking: it reads the player under its locks and runs the dry run.
    """
    minutes = check_minutes(minutes)
    zone = ZoneInfo(source.timezone)
    if decided is None:
        decided = read_station(
            source.player, desk, now=now, show=_on_air_show(source), capture_audio=capture_audio
        )
    now = decided.now
    end = now + minutes * 60
    # Queued songs are fixed choices, but their voice clips have not been rendered yet.
    if (
        capture_audio
        or getattr(source.player, "matched_intros", None) is not None
        or getattr(source.player, "liner_scheduler", None) is not None
    ):
        first = next(
            (i for i, row in enumerate(decided.items) if row.status in {"queued", "show"}), None
        )
        if first is not None:
            tail = decided.items[first:]
            previous = next(
                (row for row in reversed(decided.items[:first]) if row.kind == "song"), None
            )
            if (
                previous is not None
                and all(row.status in {"queued", "show"} for row in tail)
                and tail[-1].kind == "song"
            ):
                fixed = decided.items[:first]
                keep = {(id(row.entry), row.starts) for row in fixed}
                decided = replace(
                    decided,
                    items=fixed,
                    waiting=tail,
                    unplayed=tuple(
                        (entry, at) for entry, at in decided.unplayed if (id(entry), at) in keep
                    ),
                    last=previous.entry,
                    last_rate=previous.playback_rate,
                    handoff=previous.starts + previous.seconds,
                )
    decided = _before_interrupt(source, decided, end)
    view: dict[str, Any] = {
        "station": source.station,
        "minutes": minutes,
        "timezone": source.timezone,
        "generated_at": datetime.fromtimestamp(now, UTC).isoformat(),
        "start": _wall(now, zone).isoformat(),
        "end": _wall(end, zone).isoformat(),
        "reason": decided.reason,
        "summary": decided.reason or "",
        "items": [],
        "points": [],
        "counts": {"fixed": 0, "predicted": 0},
        "stopped": None,
        "points_note": None,
        "notes": list(NOTES),
    }
    if decided.reason is not None:
        return view
    snapshot = source.snapshot or snapshot_player(source.player)
    forecasts = _ad_forecasts(source, now, end)
    choices: list[tuple[str, str]] = []
    predicted, ends, stopped = predict(
        source,
        snapshot,
        decided,
        end,
        seed=seed_for(decided.signature()),
        budget=budget,
        ad_runs=forecasts,
        preferred_choices=preferred_choices,
        capture_audio=capture_audio,
        cancelled=cancelled,
        on_choice=(
            (lambda key, path: choices.append((key, path))) if on_prediction is not None else None
        ),
    )
    if on_prediction is not None:
        on_prediction(tuple(choices))
    timeline = sorted([*decided.items, *predicted], key=lambda item: item.starts)
    if on_items is not None:
        on_items(tuple(timeline))
    songs = [item for item in timeline if item.kind == "song"]
    # A skipped show song takes no time and opens no song change.
    changes = song_changes([item for item in timeline if item.kind != "skipped"])
    if not predicted and songs:
        last = songs[-1]
        ends = max(ends, last.starts + last.seconds)
    shown = [item for item in timeline if item.starts < end]
    view["items"] = [_item_view(item, zone) for item in shown]
    bridge_of = attached(source.player, "_request_bridge_of")
    for item, row in zip(shown, view["items"], strict=True):
        request = bridge_of.get(item.entry) if item.entry is not None else None
        if request is not None:
            row["bridge_to"] = request.display_name
            row["status_text"] += f"; bridge to {request.display_name}"
    view["counts"] = {
        "fixed": sum(1 for item in shown if item.fixed),
        "predicted": sum(1 for item in shown if not item.fixed),
    }
    clock = snapshot.clock or {}
    clock_on = (
        bool(clock.get("name"))
        and snapshot.cfg.playback.pick_mode == "clock"
        and not snapshot.settings.get("_pure_shuffle", False)
    )
    points = fixed_points(
        now,
        end,
        zone,
        resync_minutes=clock.get("resync_minutes", ()) if clock_on else (),
        events=source.events,
        describe=describe,
        hours=snapshot.cfg.playback.hour_fit is True,
        action_interrupts=source.action_interrupts,
    )
    points.extend(
        Point("event", run.at, run.label, "soft", run.detail)
        for run in forecasts
        if run.own_point and now < run.at <= end
    )
    points.sort(key=lambda point: (point.at, point.kind != "event"))
    view["points_note"] = (
        f"Only the first {MAX_POINTS} of {len(points)} fixed points are timed."
        if len(points) > MAX_POINTS
        else None
    )
    points = points[:MAX_POINTS]
    landings = [land(point, changes, ends) for point in points]
    view["points"] = [_point_view(landing, zone) for landing in landings]
    view["stopped"] = (
        f"The prediction stopped early: {stopped}. The list ends at the last song it picked."
        if stopped
        else None
    )
    view["counts_text"] = _counts_text(view, zone, end)
    view["summary"] = _summary(landings, zone)
    return view


def _ad_forecasts(source: SimulationSource, now: float, end: float) -> list[Any]:
    """The ad breaks the station expects from *now* to *end*, not yet booked (#51)."""
    ads = source.ads
    if ads is None:
        return []
    try:
        return list(ads.forecast(now, end, source.events))
    except Exception:  # a store that cannot be read must not stop the preview
        logger.exception("Could not forecast the ad breaks")
        return []


def _on_air_show(source: SimulationSource) -> tuple[str, Sequence[Any]] | None:
    """The show on the air and its songs not yet queued, from *source*, or ``None``."""
    shows = getattr(source, "shows", None)
    return getattr(shows, "on_air", None)


def _status_text(item: Item) -> str:
    """What an item's status says; a show's songs name the show."""
    if item.status == "show" and item.label:
        return f"Fixed: show {item.label}"
    if item.status == "skipped" and item.label:
        return f"Skipped from show {item.label}: it would break a DMCA limit"
    return STATUS_WORDS[item.status]


def _item_view(item: Item, zone: ZoneInfo) -> dict[str, Any]:
    path = getattr(item.entry, "path", None) if item.kind in {"song", "liner"} else None
    return {
        "kind": item.kind,
        "status": item.status,
        "fixed": item.fixed,
        "status_text": _status_text(item),
        "title": item_title(item),
        "path": str(path) if path else None,
        "asset_id": item.asset_id if item.kind == "intro" else None,
        "details_path": str(item.details_entry.path) if item.details_entry is not None else None,
        "details_kind": "liner" if item.details_entry is not None else None,
        "starts_at": _wall(item.starts, zone).isoformat(),
        "starts_text": clock_text(item.starts, zone),
        "seconds": round(item.seconds, 1),
        "length_text": length_text(item.seconds),
    }


def _point_view(landing: Landing, zone: ZoneInfo) -> dict[str, Any]:
    point = landing.point
    short, sentence = landing_words(landing, zone)
    return {
        "kind": point.kind,
        "label": point.label,
        "timing": point.timing or None,
        "detail": point.detail or None,
        "at": _wall(point.at, zone).isoformat(),
        "at_text": clock_text(point.at, zone),
        "offset_s": round(landing.offset),
        "cut_s": round(landing.cut),
        "landing": short,
        "reason_fixed": landing.playing.fixed if landing.playing is not None else None,
        "text": sentence,
    }


def _counts_text(view: dict[str, Any], zone: ZoneInfo, end: float) -> str:
    """ "4 fixed items, 23 predicted, through 14:10." for the card and a new span."""
    counts = view["counts"]
    fixed = counts["fixed"]
    return (
        f"{fixed} fixed item{'' if fixed == 1 else 's'}, {counts['predicted']} predicted, "
        f"through {clock_text(end, zone)}."
    )


def _summary(landings: Sequence[Landing], zone: ZoneInfo) -> str:
    """The next landing; the card shows the counts (:func:`_counts_text`)."""
    if not landings:
        return "No top of the hour, clockwheel restart or scheduled event."
    return _next_words(landings[0], zone)


def _next_words(landing: Landing, zone: ZoneInfo) -> str:
    """ "Next clockwheel restart, 13:30: expected 0:32 early." for the summary."""
    heading = landing_heading(landing, zone)
    if landing.point.kind != "event":
        heading = heading[0].lower() + heading[1:]
    short = landing_words(landing, zone)[0]
    lands = "past the end of the preview" if short == "not known" else f"expected {short}"
    return f"Next {heading}: {lands}."


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


class PreviewBusy(RuntimeError):
    """Too many previews are waiting for the worker; ask again shortly."""


class PreviewsClosed(RuntimeError):
    """The server is shutting down: no preview starts."""


_Key = tuple[str, int]


@dataclass
class _Job:
    key: _Key
    future: concurrent.futures.Future[dict[str, Any]]
    source: SimulationSource
    minutes: int
    desk: Any
    describe: Callable[[Any], str] | None
    force: bool = False


class HourPreviews:
    """The server's previews: one worker thread, and each station's latest preview per span.

    Asks for the same station and span share one answer: a preview being
    made, or waiting to be, is shared by everyone who asks meanwhile, and a
    finished one is reused for :data:`KEEP_SECONDS` while its timeline and
    picking inputs stay current. At most :data:`MAX_WAITING` previews wait for the worker; one
    more is refused with :class:`PreviewBusy`.
    """

    def __init__(self, *, keep: float = KEEP_SECONDS, waiting: int = MAX_WAITING) -> None:
        self._keep = keep
        self._limit = waiting
        self._cond = threading.Condition()
        self._kept: dict[_Key, tuple[float, str, dict[str, Any]]] = {}
        self._choices: dict[_Key, dict[str, str]] = {}
        self._asked: dict[_Key, concurrent.futures.Future[dict[str, Any]]] = {}
        self._forced: dict[_Key, concurrent.futures.Future[dict[str, Any]]] = {}
        self._waiting: deque[_Job] = deque()
        self._thread: threading.Thread | None = None
        self._closing = False

    def submit(
        self,
        source: SimulationSource,
        minutes: int,
        *,
        desk: Any = None,
        describe: Callable[[Any], str] | None = None,
        force: bool = False,
    ) -> concurrent.futures.Future[dict[str, Any]]:
        """The preview of *source*'s next *minutes*, made on the worker thread.

        Raises:
            PreviewsClosed: The server is shutting down.
            PreviewBusy: :data:`MAX_WAITING` previews are already waiting.
        """
        key = (source.station, minutes)
        with self._cond:
            if self._closing:
                raise PreviewsClosed("The server is shutting down")
            shared = self._forced.get(key) if force else self._asked.get(key)
            if shared is not None:
                return shared
            if len(self._waiting) >= self._limit:
                raise PreviewBusy("The hour preview is busy; try again in a few seconds")
            future: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()
            self._asked[key] = future
            if force:
                self._forced[key] = future
            self._waiting.append(_Job(key, future, source, minutes, desk, describe, force))
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._work, name="autodj-hour-preview", daemon=True
                )
                self._thread.start()
            self._cond.notify_all()
        return future

    def _work(self) -> None:
        """The worker: make the waiting previews one at a time until shutdown."""
        lower_thread_priority()
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._closing or bool(self._waiting))
                if self._closing:
                    return
                job = self._waiting.popleft()
            try:
                if job.future.set_running_or_notify_cancel():
                    try:
                        view = self.get(
                            job.source,
                            job.minutes,
                            desk=job.desk,
                            describe=job.describe,
                            force=job.force,
                        )
                    except BaseException as exc:
                        job.future.set_exception(exc)
                    else:
                        job.future.set_result(view)
            finally:
                with self._cond:
                    if self._asked.get(job.key) is job.future:
                        del self._asked[job.key]
                    if self._forced.get(job.key) is job.future:
                        del self._forced[job.key]

    def shutdown(self, timeout: float) -> bool:
        """Take no more previews, cancel the waiting ones and join the worker.

        Returns whether the worker stopped within *timeout* seconds.  A
        preview reads the library, so the server keeps the library open
        when it did not.
        """
        with self._cond:
            self._closing = True
            for job in self._waiting:
                job.future.cancel()
            self._waiting.clear()
            self._asked.clear()
            self._forced.clear()
            thread = self._thread
            self._cond.notify_all()
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def get(
        self,
        source: SimulationSource,
        minutes: int,
        *,
        desk: Any = None,
        describe: Callable[[Any], str] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """The preview of *source*, kept for :data:`KEEP_SECONDS` while nothing new is fixed.

        Blocking: the worker thread calls it.
        """
        decided = read_station(source.player, desk, show=_on_air_show(source))
        key = (source.station, minutes)
        player = source.player
        settings = tuple(
            (name, repr(getattr(player, name, None)))
            for name in (
                "_pure_shuffle",
                "_anchor_to_seed",
                "_seed_path",
                "_discovery_every",
                "_clock",
                "_clock_steps",
                "_pending_clock_update",
            )
        )
        catalog = getattr(getattr(player, "_library", None), "catalog", None)
        version_reader = getattr(catalog, "change_versions", None)
        versions = version_reader() if version_reader else None
        library = getattr(player, "_library", None)
        categories = (
            library.categories_for(getattr(player, "station_paths", None))
            if library is not None and hasattr(library, "categories_for")
            else getattr(library, "categories", None)
        )
        intros = getattr(player, "matched_intros", None)
        voice_files = []
        for store in (intros, getattr(intros, "rules", None)):
            if store is not None:
                try:
                    voice_files.append(file_signature(store.path))
                except OSError:
                    voice_files.append(None)
        category_signature = categories.cache_signature() if categories else None
        # The wall-clock handoff stays steady during playback but moves after a seek.
        signature = hashlib.sha256(
            repr(
                (
                    decided.signature(),
                    round(decided.handoff),
                    decided.clock_boundary,
                    repr(getattr(player, "_cfg", None)),
                    settings,
                    getattr(player._state, "discovery_enabled", None),
                    source.timezone,
                    repr(getattr(desk, "config", None)),
                    source.events,
                    versions,
                    category_signature,
                    voice_files,
                    getattr(source.ads, "revision", None),
                    source.shows.signature() if source.shows is not None else None,
                )
            ).encode("utf-8")
        ).hexdigest()
        with self._cond:
            kept = self._kept.get(key)
            previous_choices = self._choices.get(key, {})
        if (
            not force
            and kept is not None
            and kept[1] == signature
            and time.monotonic() - kept[0] < self._keep
        ):
            return kept[2]
        choices: dict[str, str] = {}

        def remember_choices(picks: tuple[tuple[str, str], ...]) -> None:
            choices.update(picks)

        view = build_preview(
            source,
            minutes,
            desk=desk,
            decided=decided,
            describe=describe,
            preferred_choices=previous_choices,
            on_prediction=remember_choices,
        )
        with self._cond:
            self._kept[key] = (time.monotonic(), signature, view)
            self._choices[key] = choices
        return view


def _previews(request: Request) -> HourPreviews:
    state = request.app.state
    previews = getattr(state, "hour_previews", None)
    if previews is None:
        previews = state.hour_previews = HourPreviews()
    return previews


def _source(station: Any) -> SimulationSource:
    """What a preview reads from a live *station*."""
    library = station.bridge.library
    if library is None:
        raise HTTPException(status_code=409, detail="The hour preview needs the music library")
    schedule = getattr(station, "schedule", None)
    events = (
        [event.model_copy(deep=True) for event in schedule.events.values()]
        if schedule is not None
        else []
    )
    return SimulationSource(
        player=station.player,
        library=library,
        station=station.slug,
        ledger=station.ledger,
        events=events,
        action_interrupts=(
            {
                name: "before" if action.hard is not None else action.interrupt
                for name, action in schedule.actions.items()
            }
            if schedule is not None
            else None
        ),
        timezone=station.timezone,
        liner_dates=station.paths.liner_dates,
        ads=getattr(station, "ads", None),
        carts=getattr(station, "carts", None),
        time_checks=getattr(station, "time_checks", None),
    )


def install_hour_preview(router: StationRouter) -> None:
    """Add the hour preview route; it acts on the station its URL names."""

    @router.get("/hour-preview")
    async def hour_preview(
        station: StationDep,
        request: Request,
        minutes: int = Query(DEFAULT_MINUTES, ge=MIN_MINUTES, le=MAX_MINUTES),
        refresh: bool = False,
    ) -> dict[str, Any]:
        """The station's next *minutes*: estimated start times, fixed or predicted, and backtiming."""
        source = _source(station)
        shows = getattr(station, "shows", None)
        if shows is not None:
            from autodj.shows import preview_shows

            # Reads the shows file: in a worker thread, never on the event loop.
            found = await asyncio.to_thread(
                preview_shows,
                shows,
                getattr(station, "show_runner", None),
                source.events,
                station.bridge.entry_for_path,
            )
            source = replace(source, shows=found)
        schedule = getattr(station, "schedule", None)
        describe = schedule.summary if schedule is not None else None
        previews = _previews(request)
        try:
            future = previews.submit(
                source,
                minutes,
                desk=getattr(station, "breaks", None),
                describe=describe,
                force=refresh,
            )
        except PreviewsClosed as exc:
            raise HTTPException(status_code=503, detail="The server is shutting down") from exc
        except PreviewBusy as exc:
            raise HTTPException(
                status_code=503, detail=str(exc), headers={"Retry-After": "5"}
            ) from exc
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            if not future.cancelled():
                raise  # the request itself went away
        # Cancelled because the server began shutting down while it waited.
        raise HTTPException(status_code=503, detail="The server is shutting down")


__all__ = [
    "DEFAULT_MINUTES",
    "MAX_MINUTES",
    "MIN_MINUTES",
    "Head",
    "HourPreviews",
    "PreviewBusy",
    "PreviewsClosed",
    "Timed",
    "build_preview",
    "history_at",
    "install_hour_preview",
    "read_head",
    "read_station",
    "rough_head",
    "time_queue",
]
