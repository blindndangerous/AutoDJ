"""Pure preflight for queue edits that replace a live prepared handoff."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, is_dataclass, replace
from itertools import pairwise
from typing import Any

_SONG_SPEECH_KINDS = frozenset({"dj", "prepared", "voice"})
_INDEPENDENT_SOURCES = frozenset({"ads", "ad_schedule", "news"})
_INDEPENDENT_KINDS = frozenset({"ad", "news"})
# Keep a complete 180-row confirmation prefix stable across conflict retries.
_MAX_OCCURRENCE_PROPOSALS = 256
_CATALOG_SNAPSHOT: ContextVar[tuple[Any, Any] | None] = ContextVar(
    "voice_catalog_snapshot", default=None
)


def catalog_versions(player: Any) -> Any:
    snapshot = _CATALOG_SNAPSHOT.get()
    if snapshot is not None and snapshot[0] is player:
        return snapshot[1]
    catalog = getattr(getattr(player, "_library", None), "catalog", None)
    versions = getattr(catalog, "change_versions", None)
    return versions() if callable(versions) else None


@contextmanager
def catalog_revision_snapshot(player: Any) -> Iterator[None]:
    """Read database versions before taking the audio boundary guard.

    Nested guarded operations share this control operation's snapshot. Queue,
    booking and schedule revisions are still read at each validation point.
    A catalog edit between this read and guard acquisition is an accepted window:
    no database read occurs under the bus guard. The next target refresh resolves
    against live catalog versions, changing the revision for stale requests.
    promote_future_voice also calls future_plan/resolve_voice_target outside this
    snapshot, then checks frozen candidates against their current owners before
    installing audio. An unrelated catalog edit need not discard a valid booking.
    This snapshot does not make catalog edits and audio installation atomic.
    """
    token = _CATALOG_SNAPSHOT.set((player, catalog_versions(player)))
    try:
        yield
    finally:
        _CATALOG_SNAPSHOT.reset(token)


def queue_occurrence(entry: Any) -> Any:
    """Make a new placement without changing catalog or source ownership."""
    entry = copy.copy(entry)
    # Runtime catalog records may be frozen. Only annotate this private copy.
    object.__setattr__(entry, "occurrence_id", uuid.uuid4().hex)
    return entry


def queue_matches(left: Any, right: Any) -> bool:
    """Use exact placement identity, retaining legacy unidentified queue support."""
    if left is None or right is None:
        return left is right
    if getattr(left, "occurrence_id", None) or getattr(right, "occurrence_id", None):
        return _same_occurrence(left, right)
    if getattr(left, "_prepared_occurrence_id", None) or getattr(
        right, "_prepared_occurrence_id", None
    ):
        return _same_occurrence(left, right)
    return left.path == right.path


@dataclass(frozen=True)
class VoiceTarget:
    """A bounded preview snapshot, or a confirmed pair of live placements."""

    target_id: str
    run_id: str
    outgoing: Any
    incoming: Any
    outgoing_id: str
    incoming_id: str
    revision: str
    prefix: tuple[Any, ...]
    selection: str
    estimated_airtime: float | None
    eligible: bool = True
    reason: str | None = None
    expires: float = 0.0
    transition_items: tuple[Any, ...] = ()
    songs_to_queue: int = 0

    def view(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "run_id": self.run_id,
            "outgoing_occurrence_id": self.outgoing_id,
            "incoming_occurrence_id": self.incoming_id,
            "ending": {"title": self.outgoing.title, "artist": self.outgoing.artist},
            "upcoming": {"title": self.incoming.title, "artist": self.incoming.artist},
            "revision": self.revision,
            "selection": self.selection,
            "estimated_airtime": self.estimated_airtime,
            "eligible": self.eligible,
            "reason": self.reason,
            "songs_to_queue": self.songs_to_queue if self.selection == "predicted" else 0,
        }


def occurrence_ref(player: Any, entry: Any) -> str:
    """Read an opaque run-local reference without mutating a live entry."""
    identity = getattr(entry, "occurrence_id", None) or f"object:{id(entry)}"
    return hashlib.sha256(f"{player._voice_run_id}:{identity}".encode()).hexdigest()[:32]


def target_revision(player: Any) -> str:
    state = player._state
    desk = getattr(player, "break_desk", None)
    listing = getattr(desk, "listing", None)
    bookings = (
        [(item.break_id, item.slot_id, item.state, item.updated_at) for item in listing()]
        if callable(listing)
        else []
    )
    track = getattr(player, "_playing_render", None)
    value = [
        player._voice_run_id,
        getattr(player, "_live_queue_revision", 0),
        _entry_identity(state.current_track),
        _entry_identity(getattr(track, "next_entry", None)),
        _entry_identity(state.queued_next),
        [_entry_identity(entry) for entry in state.queue],
        repr(player._cfg),
        getattr(player, "_clock_revision", None),
        catalog_versions(player),
        bookings,
        state.is_paused,
        schedule_signature(player),
    ]
    return hashlib.sha256(json.dumps(value, default=str).encode()).hexdigest()


def schedule_events(player: Any) -> tuple[Any, ...]:
    events = getattr(player, "_voice_schedule_events", None)
    if events is None:
        events = getattr(player, "_hour_fit_events", ())
    return tuple((events() if callable(events) else events) or ())


def schedule_signature(player: Any) -> str:
    return repr(schedule_events(player))


def target_schedule_risk(player: Any, target: VoiceTarget) -> str | None:
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from autodj.scheduler import next_run, system_timezone

    if player._state.is_paused:
        return None
    now = time.time()
    until = target.estimated_airtime
    if until is None:
        return "Estimated airtime is unavailable; check the schedule before booking."
    zone = ZoneInfo(player._hour_fit_timezone or player._cfg.station.timezone or system_timezone())
    for event in schedule_events(player):
        if not event.enabled or (
            event.timing != "hard" and event.action not in {"show", "profile", "pick_mode"}
        ):
            continue
        due = next_run(event, datetime.fromtimestamp(now, UTC), zone)
        if due is not None and now <= due.timestamp() <= until:
            return "At risk: a schedule boundary precedes this song change."
    return None


def capture_voice_targets(
    player: Any,
    rows: tuple[Any, ...],
    *,
    now: float,
    boundary: float | None = None,
    revision: str | None = None,
    snapshot: str | None = None,
    expires: float | None = None,
) -> tuple[VoiceTarget, ...]:
    """Retain bounded immutable forecast references; never reserve music or speech."""
    if (
        not getattr(player, "live_mixing", False)
        or player._state.should_stop
        or getattr(player, "_voice_live_takeover", False)
    ):
        return ()
    with player._state.queue_lock:
        current_revision = target_revision(player)
        if revision is not None and revision != current_revision:
            return ()
        waiting = ([player._state.queued_next] if player._state.queued_next else []) + list(
            player._state.queue
        )
        track = getattr(player, "_playing_render", None)
        current = getattr(track, "entry", None) or player._state.current_track
        successor = getattr(track, "next_entry", None)
        if current is None:
            return ()
        known = {id(entry) for entry in [current, successor, *waiting] if entry is not None}
        snapshot = snapshot or uuid.uuid4().hex
        previous = None
        prefix: list[Any] = []
        new_songs = 0
        result = []
        unavailable = []
        for number, row in enumerate(rows[:180]):
            if row.kind == "liner":
                if id(row.entry) in known:
                    prefix.append(row.entry)
                continue
            if row.kind != "song":
                continue
            entry = row.entry
            if entry is None or row.starts > now + 900:
                break
            reason = None
            if boundary is not None and row.starts >= boundary:
                reason = "A scheduled programme or clock boundary precedes this change."
            elif row.duration_unknown or row.seconds <= 0:
                reason = "This song's duration is unknown."
            elif getattr(entry, "_prepared_occurrence_id", None):
                reason = "Use Prepared editions for this prepared occurrence."
            elif row.status == "show" and id(entry) not in known:
                reason = "This show's future songs have not been queued yet."
            if reason:
                if previous is not None:
                    outgoing, outgoing_id = previous
                    ref = hashlib.sha256(f"{snapshot}:{number}".encode()).hexdigest()[:32]
                    identity = hashlib.sha256(
                        f"{player._voice_run_id}:{outgoing_id}:{ref}".encode()
                    ).hexdigest()[:32]
                    unavailable.append(
                        VoiceTarget(
                            identity,
                            player._voice_run_id,
                            outgoing,
                            entry,
                            outgoing_id,
                            ref,
                            current_revision,
                            (),
                            "predicted",
                            None,
                            eligible=False,
                            reason=reason,
                            expires=expires if expires is not None else time.monotonic() + 45,
                        )
                    )
                break
            if entry is current and previous is None:
                previous = (entry, occurrence_ref(player, entry))
                continue
            if row.status == "predicted":
                entry = queue_occurrence(entry)
                object.__setattr__(
                    entry,
                    "occurrence_id",
                    hashlib.sha256(f"{snapshot}:{number}".encode()).hexdigest()[:32],
                )
            ref = (
                occurrence_ref(player, entry)
                if id(entry) in known
                else hashlib.sha256(f"{snapshot}:{number}".encode()).hexdigest()[:32]
            )
            prefix.append(entry)
            if id(entry) not in known:
                new_songs += 1
            if previous is not None and previous[0] is not current:
                outgoing, outgoing_id = previous
                confirmed = id(outgoing) in known and id(entry) in known
                identity = hashlib.sha256(
                    f"{player._voice_run_id}:{outgoing_id}:{ref}".encode()
                ).hexdigest()[:32]
                handoff = row.incoming_handoff
                target = VoiceTarget(
                    identity,
                    player._voice_run_id,
                    outgoing,
                    entry,
                    outgoing_id,
                    ref,
                    current_revision,
                    tuple(prefix),
                    "confirmed" if confirmed else "predicted",
                    row.starts,
                    expires=expires if expires is not None else time.monotonic() + 45,
                    transition_items=tuple(handoff.transition_items) if handoff else (),
                    songs_to_queue=new_songs,
                )
                result.append(target)
            previous = (entry, ref)
        # Confirmed bindings survive preview expiry; predictions are bounded by one snapshot.
        player._voice_targets = {
            key: value
            for key, value in player._voice_targets.items()
            if value.selection == "confirmed" and _target_adjacent(player, value)
        }
        player._voice_targets.update(
            (target.target_id, target) for target in (*result, *unavailable)
        )
        return tuple(result)


def _target_adjacent(player: Any, target: VoiceTarget) -> bool:
    entries = music_sequence(player)
    return any(
        _same_occurrence(a, target.outgoing) and _same_occurrence(b, target.incoming)
        for a, b in pairwise(entries)
    )


def music_sequence(player: Any, queued_next: Any = ..., queue: Any = ...) -> list[Any]:
    state = player._state
    next_entry = state.queued_next if queued_next is ... else queued_next
    entries = list(state.queue if queue is ... else queue)
    if next_entry is not None:
        entries.insert(0, next_entry)
    track = getattr(player, "_playing_render", None)
    current = getattr(track, "entry", None) or state.current_track
    selected = getattr(track, "next_entry", None)
    if (
        selected is not None
        and not getattr(track, "next_from_queue", False)
        and not any(_same_occurrence(selected, entry) for entry in entries)
    ):
        entries.insert(0, selected)
    if current is not None and not any(_same_occurrence(current, entry) for entry in entries):
        entries.insert(0, current)
    return [entry for entry in entries if not player._is_liner(entry)]


def resolve_voice_target(player: Any, target_id: str, revision: str | None = None) -> VoiceTarget:
    from autodj.breaks import BreakError

    target = player._voice_targets.get(target_id)
    current = target_revision(player)
    if (
        player._state.should_stop
        or getattr(player, "_voice_live_takeover", False)
        or target is None
        or target.run_id != player._voice_run_id
        or (revision is not None and revision != current)
        or not target.eligible
        or (
            target.selection == "predicted"
            and (target.revision != current or time.monotonic() >= target.expires)
        )
        or (target.selection == "confirmed" and not _target_adjacent(player, target))
    ):
        raise BreakError(
            409, "That song change changed; refresh and choose again", "target_changed"
        )
    return replace(
        target,
        revision=current,
        estimated_airtime=None if player._state.is_paused else target.estimated_airtime,
    )


def confirm_voice_target(
    player: Any,
    target_id: str,
    revision: str,
    *,
    voice_action: str | None = None,
    voice_revision: str | None = None,
) -> VoiceTarget:
    """Validate the whole snapshot, then install its prefix in one queue mutation."""
    from autodj.breaks import BreakError

    with player._clock_selection_lock, occurrence_proposals(player):
        with player._state.queue_lock:
            target = resolve_voice_target(player, target_id, revision)
            if target.selection == "confirmed":
                return target
            lookup = getattr(getattr(player, "_library", None), "entry_for_path", None)
            if len([entry for entry in target.prefix if not player._is_liner(entry)]) < 2 or any(
                not math.isfinite(float(entry.length or 0))
                or float(entry.length or 0) <= 0
                or (callable(lookup) and lookup(entry.path) is None)
                for entry in target.prefix
            ):
                raise BreakError(409, "The complete prefix is no longer playable", "target_changed")

        with player.live_queue_guard(), player._state.queue_lock:
            target = resolve_voice_target(player, target_id, revision)
            state = player._state
            existing = ([state.queued_next] if state.queued_next else []) + list(state.queue)
            track = player._playing_render
            selected = getattr(track, "next_entry", None)
            known = {id(entry): entry for entry in existing}
            if selected is not None:
                known[id(selected)] = selected
            prefix = [
                known.get(id(entry))
                or proposed_occurrence(player, entry, f"confirm:{target_id}:{index}")
                for index, entry in enumerate(target.prefix)
            ]
            kept = [entry for entry in prefix if any(entry is old for old in existing)]
            if kept != existing[: len(kept)]:
                raise BreakError(
                    409, "The queue changed; refresh and choose again", "target_changed"
                )
            proposed = prefix + existing[len(kept) :]
            decision = plan_queue_edit(
                player, None, proposed, voice_action=voice_action, voice_revision=voice_revision
            )
            replacement = (
                replace(track, next_from_queue=True)
                if selected is not None and any(entry is selected for entry in prefix)
                else track
            )
            if (
                replacement is not track
                and player.bus is not None
                and not player.bus.replace_live_plan(track, replacement)
            ):
                raise BreakError(
                    409, "The current song changed; refresh and choose again", "target_changed"
                )
            if not existing and state.pre_queue_seed is None:
                state.pre_queue_seed = state.current_track
            state.queued_next = None
            state.queue[:] = proposed
            # Adopt the already selected successor; do not pick it or advance its clock again.
            if replacement is not track:
                player._playing_render = replacement
                player._playout.reset(lambda: player._skip_past_render(replacement))
            if replacement is track:
                player.refresh_playout()
            else:
                player._live_queue_revision += 1
            player.accept_live_queue_edit(decision)
            outgoing, incoming = [entry for entry in prefix if not player._is_liner(entry)][-2:]
            outgoing_id, incoming_id = (
                occurrence_ref(player, outgoing),
                occurrence_ref(player, incoming),
            )
            identity = hashlib.sha256(
                f"{player._voice_run_id}:{outgoing_id}:{incoming_id}".encode()
            ).hexdigest()[:32]
            confirmed = replace(
                target,
                target_id=identity,
                outgoing=outgoing,
                incoming=incoming,
                outgoing_id=outgoing_id,
                incoming_id=incoming_id,
                prefix=tuple(prefix),
                selection="confirmed",
                revision=target_revision(player),
            )
            player._voice_targets[identity] = confirmed
            return confirmed


class VoiceQueueConflict(Exception):
    """The proposed queue edit changes music named by handoff speech."""

    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(detail["message"])


@dataclass(frozen=True)
class QueueEdit:
    """The decision needed to atomically apply one queue edit."""

    track: Any
    candidate: Any
    voice_action: str | None
    breaks: tuple[Any, ...]
    intro: Any
    outgoing: Any = None
    candidate_mode: str | None = None
    future: tuple[tuple[Any, VoiceTarget | None], ...] = ()


def _entry_identity(entry: Any) -> list[Any] | None:
    if entry is None:
        return None
    return [
        str(getattr(entry, "path", "")),
        getattr(entry, "_prepared_occurrence_id", None),
        getattr(entry, "occurrence_id", None),
        None if getattr(entry, "occurrence_id", None) else id(entry),
    ]


def _same_occurrence(left: Any, right: Any) -> bool:
    if left is right:
        return True
    left_id = getattr(left, "occurrence_id", None)
    right_id = getattr(right, "occurrence_id", None)
    if left_id is not None and right_id is not None:
        return left_id == right_id
    left_id = getattr(left, "_prepared_occurrence_id", None)
    right_id = getattr(right, "_prepared_occurrence_id", None)
    return left_id is not None and right_id is not None and left_id == right_id


def _occurrence_key(entry: Any) -> tuple[str, Any]:
    occurrence = getattr(entry, "occurrence_id", None)
    if occurrence is None:
        occurrence = getattr(entry, "_prepared_occurrence_id", None)
    return ("occurrence", occurrence) if occurrence is not None else ("object", id(entry))


def _candidate(
    player: Any, track: Any, queued_next: Any, queue: list[Any] | tuple[Any, ...]
) -> Any:
    values = ([queued_next] if queued_next is not None else []) + list(queue)
    reserved = Counter(
        _occurrence_key(item)
        for item in (
            *getattr(track, "reserved_liners", ()),
            *getattr(track, "next_reserved_liners", ()),
        )
    )
    skip_entry = bool(getattr(track, "from_queue", False))
    is_liner = getattr(player, "_is_liner", None)
    for entry in values:
        if entry is None:
            continue
        key = _occurrence_key(entry)
        if reserved[key]:
            reserved[key] -= 1
            continue
        if skip_entry and _same_occurrence(entry, track.entry):
            skip_entry = False
            continue
        if is_liner is not None and is_liner(entry):
            continue
        return entry
    return None


def _breaks_for(player: Any, track: Any) -> tuple[Any, ...]:
    if hasattr(track, "voice_breaks"):
        return tuple(track.voice_breaks or ())
    desk = getattr(player, "break_desk", None)
    get = getattr(desk, "get", None)
    if get is None:
        return ()
    found = []
    for _start, break_id, *_tail in getattr(track, "break_events", ()):
        item = get(break_id)
        if item is not None:
            found.append(item)
    return tuple(found)


def _song_specific(item: Any) -> bool:
    if (
        getattr(item, "source", None) in _INDEPENDENT_SOURCES
        or getattr(item, "kind", None) in _INDEPENDENT_KINDS
    ):
        return False
    return getattr(item, "kind", None) in _SONG_SPEECH_KINDS or getattr(item, "source", None) in {
        "voice_track",
        "prepared_edition",
    }


def _identity(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "break_id"):
        return [
            value.break_id,
            getattr(value, "kind", None),
            getattr(value, "source", None),
            str(getattr(value, "path", "")),
            repr(getattr(value, "cues", None)),
            getattr(value, "placement", None),
            getattr(value, "allow_outro", None),
        ]
    if hasattr(value, "intro_id"):
        return [value.intro_id, str(getattr(value, "path", ""))]
    if hasattr(value, "path"):
        return _entry_identity(value)
    return [
        getattr(value, "break_id", None),
        getattr(value, "kind", None),
        getattr(value, "source", None),
        getattr(value, "intro_id", None),
        getattr(value, "path", None) and str(value.path),
        id(value),
    ]


def _revision(player: Any, track: Any, candidate: Any, breaks: tuple[Any, ...], intro: Any) -> str:
    state = getattr(player, "_state", None)
    current_queue: list[Any] = []
    if state is not None:
        if state.queued_next is not None:
            current_queue.append(["next", _entry_identity(state.queued_next)])
        current_queue.extend(
            ["queue", position, _entry_identity(entry)]
            for position, entry in enumerate(state.queue)
        )
    selected = {
        "track": id(track),
        "entry": _entry_identity(getattr(track, "entry", None)),
        "next": _entry_identity(getattr(track, "next_entry", None)),
        "current_queue": current_queue,
        "candidate": _entry_identity(candidate),
        "voices": [_identity(item) for item in breaks if _song_specific(item)],
        "intro": _identity(intro),
    }
    encoded = json.dumps(selected, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _copy_kept(item: Any) -> Any:
    if is_dataclass(item) and not isinstance(item, type):
        return replace(item, source="operator_keep", keep_file=True)
    import copy

    kept = copy.copy(item)
    kept.source = "operator_keep"
    kept.keep_file = True
    return kept


def _plan_current_edit(
    player: Any,
    queued_next: Any,
    queue: list[Any] | tuple[Any, ...],
    *,
    voice_action: str | None = None,
    voice_revision: str | None = None,
) -> QueueEdit | None:
    """Preflight the handoff changed by a proposed queue replacement.

    This function only inspects state and returns the choice the caller must
    commit with its queue mutation. It never changes the player, queue or media.
    """
    if not getattr(player, "live_mixing", False):
        return None
    select_editable = getattr(player, "live_editable_handoff", None)
    if callable(select_editable):
        track = select_editable()
    else:
        track = getattr(player, "_playing_render", None)
        if track is None or getattr(player, "_heard_next_entry", False):
            ahead = getattr(player, "_playout", None)
            track = ahead.ready() if ahead is not None else None
    if track is None:
        return None
    # A pending cursor has no selected successor; its handoff is planned when
    # the bus needs it, against the then-current queue.
    if getattr(track, "handoff_pending", False):
        return None

    candidate = _candidate(player, track, queued_next, queue)
    state = getattr(player, "_state", None)
    requested = (
        _candidate(player, track, state.queued_next, state.queue) if state is not None else None
    )
    changed = not (
        _same_occurrence(candidate, requested)
        or _same_occurrence(candidate, getattr(track, "next_entry", None))
    )
    if not changed:
        return None

    breaks = _breaks_for(player, track)
    intro = getattr(track, "incoming_intro", None)
    if not any(_song_specific(item) for item in breaks) and intro is None:
        return QueueEdit(track, candidate, None, breaks, intro)

    revision = _revision(player, track, candidate, breaks, intro)
    if voice_action not in {"keep", "discard"} or voice_revision != revision:
        raise VoiceQueueConflict(
            {
                "code": "voice_track_conflict",
                "revision": revision,
                "message": "This change affects a voice track.",
                "affected": [
                    {
                        "break_id": getattr(item, "break_id", None),
                        "label": getattr(item, "label", "Matched intro"),
                        "old_pair": [
                            getattr(track.entry, "display_name", str(track.entry.path)),
                            getattr(track.next_entry, "display_name", str(track.next_entry.path)),
                        ],
                        "proposed_pair": [
                            getattr(track.entry, "display_name", str(track.entry.path)),
                            getattr(candidate, "display_name", str(candidate.path)),
                        ]
                        if candidate is not None
                        else None,
                    }
                    for item in (
                        *[item for item in breaks if _song_specific(item)],
                        *([intro] if intro is not None else []),
                    )
                ],
            }
        )

    affected = {id(item) for item in breaks if _song_specific(item)}
    if voice_action == "keep":
        breaks = tuple(
            _copy_kept(item)
            if id(item) in affected and getattr(item, "source", None) == "prepared_edition"
            else item
            for item in breaks
        )
    else:
        breaks = tuple(item for item in breaks if id(item) not in affected)
        intro = None
    return QueueEdit(track, candidate, voice_action, breaks, intro)


def plan_queue_edit(
    player: Any,
    queued_next: Any,
    queue: list[Any] | tuple[Any, ...],
    *,
    voice_action: str | None = None,
    voice_revision: str | None = None,
) -> QueueEdit | None:
    """One revision and decision for every affected current and future voice."""
    desk = getattr(player, "break_desk", None)
    waiting = (
        [
            item
            for item in desk.listing()
            if getattr(item, "run_id", None) == getattr(player, "_voice_run_id", None)
            and getattr(item, "run_id", None) is not None
            and item.state == "queued"
        ]
        if desk is not None
        and callable(getattr(desk, "listing", None))
        and hasattr(player, "_voice_run_id")
        else []
    )
    changes = []
    if waiting and getattr(player, "live_mixing", False):
        proposed = music_sequence(player, queued_next, queue)
        refs = [occurrence_ref(player, entry) for entry in proposed]
        for item in waiting:
            a, b = item.outgoing_occurrence_id, item.incoming_occurrence_id
            if any(left == a and right == b for left, right in pairwise(refs)):
                continue
            pair = None
            if a in refs and refs.index(a) + 1 < len(refs):
                index = refs.index(a)
                pair = (proposed[index], proposed[index + 1])
            elif b in refs and refs.index(b) > 0:
                index = refs.index(b)
                pair = (proposed[index - 1], proposed[index])
            old = player._voice_targets.get(item.slot_id)
            replacement = None
            if (
                pair is not None
                and old is not None
                and not any(getattr(entry, "_prepared_occurrence_id", None) for entry in pair)
            ):
                left, right = (occurrence_ref(player, entry) for entry in pair)
                identity = hashlib.sha256(
                    f"{player._voice_run_id}:{left}:{right}".encode()
                ).hexdigest()[:32]
                replacement = replace(
                    old,
                    target_id=identity,
                    outgoing=pair[0],
                    incoming=pair[1],
                    outgoing_id=left,
                    incoming_id=right,
                    prefix=(),
                    estimated_airtime=None,
                    transition_items=old.transition_items,
                )
            changes.append((item, replacement))
    if not changes:
        return _plan_current_edit(
            player, queued_next, queue, voice_action=voice_action, voice_revision=voice_revision
        )
    assert desk is not None
    current_revision = None
    current_affected = []
    try:
        current = _plan_current_edit(player, queued_next, queue)
    except VoiceQueueConflict as exc:
        current = None
        current_revision = exc.detail["revision"]
        current_affected = exc.detail.get("affected", [])
    encoded = [
        target_revision(player),
        current_revision,
        _entry_identity(queued_next),
        [_entry_identity(entry) for entry in queue],
        [(item.break_id, target.target_id if target else None) for item, target in changes],
    ]
    revision = hashlib.sha256(json.dumps(encoded).encode()).hexdigest()
    destinations = {item.break_id: target.target_id if target else None for item, target in changes}
    merged_frozen = any(
        destination is not None
        and len(
            {
                item.slot_id
                for item in waiting
                if destinations.get(item.break_id, item.slot_id) == destination
            }
        )
        > 1
        and any(
            content.kind not in {"booked_voice", "booked_break"}
            for item in waiting
            if destinations.get(item.break_id, item.slot_id) == destination
            for content in item.transition_items
        )
        for destination in destinations.values()
    )
    can_keep = (
        not merged_frozen
        and all(target is not None for _, target in changes)
        and desk.can_rebind_voice(destinations)
    )
    if (
        voice_revision != revision
        or voice_action not in {"keep", "discard"}
        or (voice_action == "keep" and not can_keep)
    ):
        raise VoiceQueueConflict(
            {
                "code": "voice_track_conflict",
                "revision": revision,
                "message": "This change affects a voice track.",
                "can_keep": can_keep,
                "keep_reason": None
                if can_keep
                else "Frozen transition chains cannot be combined without changing their audio order."
                if merged_frozen
                else "No eligible replacement pair, or the pair's recording limit would be exceeded.",
                "affected": current_affected
                + [
                    {
                        "break_id": item.break_id,
                        "label": item.label,
                        "old_pair": [
                            old.outgoing.display_name,
                            old.incoming.display_name,
                        ]
                        if (old := player._voice_targets.get(item.slot_id)) is not None
                        else [item.ending, item.upcoming],
                        "proposed_pair": [
                            target.outgoing.display_name,
                            target.incoming.display_name,
                        ]
                        if target
                        else None,
                    }
                    for item, target in changes
                ],
            }
        )
    if current_revision is not None:
        current = _plan_current_edit(
            player, queued_next, queue, voice_action=voice_action, voice_revision=current_revision
        )
    return replace(
        current or QueueEdit(None, None, voice_action, (), None),
        future=tuple(
            (item, target if voice_action == "keep" else None) for item, target in changes
        ),
    )


def apply_future_edit(player: Any, decision: QueueEdit) -> None:
    """Commit the preflighted associations under the queue/control guard."""
    if not decision.future:
        return
    for item, target in decision.future:
        if target is None:
            player.break_desk.drop(item.break_id, "discarded from this airing by a queue edit")
        else:
            player._voice_targets[target.target_id] = target
            player.break_desk.rebind_voice(item.break_id, target)


@contextmanager
def occurrence_proposals(player: Any) -> Iterator[None]:
    """Retain IDs for a conflict retry, releasing them after apply or refusal.

    Bridge edits clear under queue_lock. Confirmation clears after its queue_lock
    blocks, while still holding _clock_selection_lock.
    """
    retry = False
    try:
        yield
    except VoiceQueueConflict:
        retry = True
        raise
    finally:
        if not retry:
            player._voice_proposals = {}


def proposed_occurrence(player: Any, entry: Any, operation: str) -> Any:
    """Allocate a placement once per outstanding operation and revision."""
    with player._state.queue_lock:
        revision = target_revision(player)
        proposals = getattr(player, "_voice_proposals", {})
        player._voice_proposals = proposals
        for old_key in tuple(proposals):
            if old_key[0] != revision:
                del proposals[old_key]
        key = (revision, operation, str(entry.path))
        identity = proposals.pop(key, None) or uuid.uuid4().hex
        while len(proposals) >= _MAX_OCCURRENCE_PROPOSALS:
            del proposals[next(iter(proposals))]
        proposals[key] = identity
        result = copy.copy(entry)
        object.__setattr__(result, "occurrence_id", identity)
        return result
