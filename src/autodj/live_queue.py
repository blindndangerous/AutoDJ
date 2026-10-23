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
    outgoing: Any = None
    candidate_mode: str | None = None


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
