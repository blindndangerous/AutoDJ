"""Pure preflight for queue edits that replace a live prepared handoff."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, is_dataclass, replace
from typing import Any

_SONG_SPEECH_KINDS = frozenset({"dj", "prepared", "voice"})
_INDEPENDENT_SOURCES = frozenset({"ads", "ad_schedule", "news"})
_INDEPENDENT_KINDS = frozenset({"ad", "news"})


class VoiceQueueConflict(Exception):
    """The proposed queue edit changes music named by handoff speech."""

    def __init__(self, detail: dict[str, str]) -> None:
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
    following: tuple[QueueEdit, ...] = ()
    outgoing: Any = None


def _entry_identity(entry: Any) -> list[Any] | None:
    if entry is None:
        return None
    return [
        str(getattr(entry, "path", "")),
        getattr(entry, "_prepared_occurrence_id", None),
        getattr(entry, "occurrence_id", None),
        id(entry),
    ]


def _same_occurrence(left: Any, right: Any) -> bool:
    if left is right:
        return True
    left_id = getattr(left, "_prepared_occurrence_id", None)
    right_id = getattr(right, "_prepared_occurrence_id", None)
    if left_id is not None and right_id is not None:
        return left_id == right_id
    left_id = getattr(left, "occurrence_id", None)
    right_id = getattr(right, "occurrence_id", None)
    return left_id is not None and right_id is not None and left_id == right_id


def _occurrence_key(entry: Any) -> tuple[str, Any]:
    occurrence = getattr(entry, "_prepared_occurrence_id", None)
    if occurrence is None:
        occurrence = getattr(entry, "occurrence_id", None)
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


def _after_occurrence(player: Any, outgoing: Any, queue: list[Any] | tuple[Any, ...]) -> Any:
    """Find the projected successor of an exact queued occurrence, if present."""
    values = list(queue)
    for index, entry in enumerate(values):
        if _same_occurrence(entry, outgoing):
            probe = type("QueueProbe", (), {})()
            probe.entry = outgoing
            probe.from_queue = True
            probe.reserved_liners = ()
            probe.next_reserved_liners = ()
            return _candidate(player, probe, None, values[index + 1 :])
    return None


def _contains_occurrence(outgoing: Any, values: list[Any] | tuple[Any, ...]) -> bool:
    return any(_same_occurrence(item, outgoing) for item in values if item is not None)


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


def _outgoing(track: Any) -> Any:
    return getattr(track, "entry", None) or getattr(track, "ending", None)


def _slot_context(player: Any, plan: Any) -> tuple[tuple[Any, ...], Any]:
    desk = getattr(player, "break_desk", None)
    listing = getattr(desk, "listing", None)
    breaks = tuple(
        item
        for item in (listing() if callable(listing) else ())
        if getattr(item, "slot_id", None) == getattr(plan, "slot_id", None)
        and getattr(item, "state", None) == "queued"
    )
    intro = next(
        (
            item.intro
            for item in getattr(plan, "transition_items", ())
            if getattr(item, "kind", None) == "matched_intro"
            and getattr(item, "intro", None) is not None
        ),
        None,
    )
    return breaks, intro


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


def plan_queue_edit(
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
            ahead = getattr(player, "_render_ahead", None)
            track = ahead.ready() if ahead is not None else None
    if track is None:
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
    ahead = getattr(player, "_render_ahead", None)
    ready = ahead.ready() if ahead is not None else None
    following: list[QueueEdit] = []
    changed_contexts: list[tuple[Any, Any, tuple[Any, ...], Any]] = []
    outgoing_overrides: dict[int, Any] = {}
    if changed:
        changed_contexts.append(
            (track, candidate, _breaks_for(player, track), getattr(track, "incoming_intro", None))
        )
    proposed_values = ([queued_next] if queued_next is not None else []) + list(queue)
    previous_values = (
        (([state.queued_next] if state.queued_next is not None else []) + list(state.queue))
        if state is not None
        else []
    )
    if ready is not None and ready is not track:
        if _contains_occurrence(ready.entry, proposed_values):
            following_outgoing = ready.entry
            following_candidate = _after_occurrence(player, ready.entry, proposed_values)
            ready_changed = not (
                _same_occurrence(following_candidate, getattr(ready, "next_entry", None))
                or _same_occurrence(
                    following_candidate, _after_occurrence(player, ready.entry, previous_values)
                )
            )
            if not ready_changed:
                following_candidate = ready.next_entry
        elif changed:
            # B is displaced by the replacement X. Its voice belongs on the
            # new X->successor edge, never on the preceding A->X edge.
            following_outgoing = candidate
            following_candidate = (
                _after_occurrence(player, candidate, proposed_values)
                if candidate is not None
                else None
            )
            ready_changed = True
        else:
            # Automatic B->C has no queue occurrence to anchor a projection.
            # An unchanged empty requested queue leaves C selected.
            following_outgoing = ready.entry
            following_candidate = getattr(ready, "next_entry", None)
            ready_changed = False
        rb, ri = _breaks_for(player, ready), getattr(ready, "incoming_intro", None)
        if ready_changed:
            changed_contexts.append((ready, following_candidate, rb, ri))
            if not _same_occurrence(following_outgoing, ready.entry):
                outgoing_overrides[id(ready)] = following_outgoing
        elif changed and (rb or ri is not None):
            following.append(
                QueueEdit(ready, following_candidate, None, rb, ri, outgoing=following_outgoing)
            )
    slot = getattr(player, "_slot_plan", None)
    if slot is not None and getattr(slot, "pinned", False):
        if _contains_occurrence(slot.ending, proposed_values):
            projected = _after_occurrence(player, slot.ending, proposed_values)
            slot_outgoing = slot.ending
            changed_slot = not (
                _same_occurrence(projected, slot.upcoming)
                or _same_occurrence(
                    projected, _after_occurrence(player, slot.ending, previous_values)
                )
            )
        elif _contains_occurrence(slot.ending, previous_values):
            before = getattr(slot, "before", None)
            slot_outgoing = (
                _after_occurrence(player, before, proposed_values)
                if before is not None and _contains_occurrence(before, proposed_values)
                else candidate
            )
            projected = (
                _after_occurrence(player, slot_outgoing, proposed_values)
                if slot_outgoing is not None
                else None
            )
            changed_slot = not (
                _same_occurrence(slot_outgoing, slot.ending)
                and _same_occurrence(projected, slot.upcoming)
            )
        else:
            projected = None
            slot_outgoing = None
            changed_slot = False
        if changed_slot:
            sb, si = _slot_context(player, slot)
            changed_contexts.append((slot, projected, sb, si))
            outgoing_overrides[id(slot)] = slot_outgoing
    if not changed and not changed_contexts:
        return None

    affected_any = any(
        any(_song_specific(item) for item in bs) or intro is not None
        for _, _, bs, intro in changed_contexts
    )
    if not affected_any:
        primary_context = (
            changed_contexts[0]
            if changed_contexts
            else (
                track,
                candidate,
                _breaks_for(player, track),
                getattr(track, "incoming_intro", None),
            )
        )
        tail = [
            QueueEdit(
                ct,
                cc,
                None,
                cb,
                ci,
                outgoing=(
                    outgoing_overrides[id(ct)]
                    if id(ct) in outgoing_overrides
                    else _outgoing(ct)
                    if ct is not track
                    else None
                ),
            )
            for ct, cc, cb, ci in changed_contexts[1:]
        ]
        return QueueEdit(
            *primary_context[:2],
            None,
            primary_context[2],
            primary_context[3],
            (*tail, *following),
            (
                outgoing_overrides[id(changed_contexts[0][0])]
                if changed_contexts and id(changed_contexts[0][0]) in outgoing_overrides
                else _outgoing(changed_contexts[0][0])
                if changed_contexts and changed_contexts[0][0] is not track
                else None
            ),
        )

    # A single revision covers every affected song context so keep/discard is
    # one atomic decision across a replanned chain.
    revisions = [
        [
            id(t),
            _entry_identity(
                outgoing_overrides[id(t)] if id(t) in outgoing_overrides else _outgoing(t)
            ),
            _entry_identity(c),
            [_identity(item) for item in bs if _song_specific(item)],
            _identity(intro),
        ]
        for t, c, bs, intro in changed_contexts
    ]
    revision = hashlib.sha256(
        json.dumps(
            [revisions, _revision(player, track, candidate, (), None)], sort_keys=True, default=str
        ).encode()
    ).hexdigest()
    if voice_action not in {"keep", "discard"} or voice_revision != revision:
        raise VoiceQueueConflict(
            {
                "code": "voice_track_conflict",
                "revision": revision,
                "message": "This change affects a voice track.",
            }
        )

    edits: list[QueueEdit] = []
    for context_track, context_candidate, context_breaks, context_intro in changed_contexts:
        affected = {id(item) for item in context_breaks if _song_specific(item)}
        if voice_action == "keep":
            context_breaks = tuple(
                _copy_kept(item)
                if id(item) in affected and getattr(item, "source", None) == "prepared_edition"
                else item
                for item in context_breaks
            )
        else:
            context_breaks = tuple(item for item in context_breaks if id(item) not in affected)
            if context_intro is not None:
                context_intro = None
        edits.append(
            QueueEdit(
                context_track,
                context_candidate,
                voice_action,
                context_breaks,
                context_intro,
                outgoing=(
                    outgoing_overrides[id(context_track)]
                    if id(context_track) in outgoing_overrides
                    else _outgoing(context_track)
                    if context_track is not track
                    else None
                ),
            )
        )
    primary = edits[0]
    return QueueEdit(
        primary.track,
        primary.candidate,
        primary.voice_action,
        primary.breaks,
        primary.intro,
        (*edits[1:], *following),
        primary.outgoing,
    )
