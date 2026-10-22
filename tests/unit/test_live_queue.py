from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from autodj.live_queue import QueueEdit, VoiceQueueConflict, plan_queue_edit


@dataclass
class _Entry:
    path: str
    _prepared_occurrence_id: str | None = None
    kind: str = "music"


@dataclass
class _Break:
    break_id: str
    kind: str
    source: str
    keep_file: bool = False
    path: str = "voice.wav"


def _player(track, *, heard=False, queue=(), queued_next=None):
    return SimpleNamespace(
        live_mixing=True,
        _playing_render=track,
        _heard_next_entry=heard,
        _playout=SimpleNamespace(ready=lambda: None),
        _state=SimpleNamespace(queue=list(queue), queued_next=queued_next),
        _is_liner=lambda entry: entry.kind == "liner",
    )


def _track(*, entry=None, next_entry=None, **values):
    defaults = {
        "entry": entry or _Entry("outgoing"),
        "next_entry": next_entry or _Entry("old-next"),
        "from_queue": False,
        "reserved_liners": (),
        "next_reserved_liners": (),
        "voice_breaks": (),
        "incoming_intro": None,
        "break_events": (),
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def _conflict(player, candidate, **kwargs) -> VoiceQueueConflict:
    with pytest.raises(VoiceQueueConflict) as caught:
        plan_queue_edit(player, None, [candidate], **kwargs)
    return caught.value


def test_keep_copies_prepared_voice_and_preserves_independent_ad_and_news() -> None:
    """ADR-0015-09: keep retains eligible prepared speech for this handoff only."""
    old = _Entry("old-next")
    candidate = _Entry("new-next")
    prepared = _Break("take-1", "prepared", "prepared_edition")
    dj = _Break("dj-1", "dj", "announcer")
    ad = _Break("ad-1", "ad", "ads")
    news = _Break("news-1", "news", "voice_track")
    intro = SimpleNamespace(intro_id="intro-1", path="intro.wav")
    track = _track(next_entry=old, voice_breaks=(prepared, dj, ad, news), incoming_intro=intro)
    player = _player(track)

    conflict = _conflict(player, candidate)
    assert conflict.detail["code"] == "voice_track_conflict"
    assert len(conflict.detail["revision"]) == 64
    assert conflict.detail["message"] == "This change affects a voice track."

    result = plan_queue_edit(
        player,
        None,
        [candidate],
        voice_action="keep",
        voice_revision=conflict.detail["revision"],
    )
    assert isinstance(result, QueueEdit)
    assert result.track is track and result.candidate is candidate
    kept = result.breaks[0]
    assert kept is not prepared
    assert (kept.source, kept.keep_file, kept.path) == ("operator_keep", True, prepared.path)
    assert result.breaks[1] is dj
    assert result.breaks[2:] == (ad, news)
    assert result.intro is intro
    assert (prepared.source, prepared.keep_file) == ("prepared_edition", False)


def test_discard_drops_only_song_specific_speech_and_intro() -> None:
    """ADR-0015-09: discard removes speech from this handoff without mutating source."""
    candidate = _Entry("new-next")
    prepared = _Break("take-1", "prepared", "prepared_edition")
    ad = _Break("ad-1", "ad", "ads")
    news = _Break("news-1", "news", "voice_track")
    intro = SimpleNamespace(intro_id="intro-1", path="intro.wav")
    track = _track(voice_breaks=(prepared, ad, news), incoming_intro=intro)
    player = _player(track)
    conflict = _conflict(player, candidate)

    result = plan_queue_edit(
        player,
        None,
        [candidate],
        voice_action="discard",
        voice_revision=conflict.detail["revision"],
    )
    assert result.breaks == (ad, news)
    assert result.intro is None
    assert prepared.source == "prepared_edition"
    assert prepared.path == "voice.wav"


@pytest.mark.parametrize("action", [None, "cancel"])
def test_missing_or_cancelled_choice_is_a_conflict(action) -> None:
    """ADR-0015-09: absent or cancelled choice cannot authorize a live queue edit."""
    track = _track(voice_breaks=(_Break("dj", "dj", "announcer"),))
    with pytest.raises(VoiceQueueConflict):
        plan_queue_edit(_player(track), None, [_Entry("new")], voice_action=action)


def test_stale_voice_revision_is_rejected() -> None:
    track = _track(voice_breaks=(_Break("dj", "dj", "announcer"),))
    current = _Entry("same-path", "queue-a")
    player = _player(track, queue=(current,))
    candidate = _Entry("new")
    conflict = _conflict(player, candidate)
    player._state.queue[0] = _Entry("same-path", "queue-b")
    with pytest.raises(VoiceQueueConflict):
        plan_queue_edit(
            player,
            None,
            [candidate],
            voice_action="keep",
            voice_revision=conflict.detail["revision"],
        )


def test_audible_incoming_delegates_to_ready_following_handoff() -> None:
    audible = _track(next_entry=_Entry("already-audible"))
    following = _track(
        entry=_Entry("already-audible"),
        next_entry=_Entry("following-old"),
        incoming_intro=SimpleNamespace(intro_id="following-intro", path="f.wav"),
    )
    candidate = _Entry("following-new")
    player = _player(audible, heard=True)
    player._playout = SimpleNamespace(ready=lambda: following)

    conflict = _conflict(player, candidate)
    assert conflict.detail["code"] == "voice_track_conflict"
    result = plan_queue_edit(
        player,
        None,
        [candidate],
        voice_action="keep",
        voice_revision=conflict.detail["revision"],
    )
    assert result.track is following


def test_live_editable_handoff_selector_takes_precedence_over_callback_flag() -> None:
    stale_current = _track(next_entry=_Entry("current-next"))
    editable = _track(
        next_entry=_Entry("following-old"),
        voice_breaks=(_Break("dj", "dj", "announcer"),),
    )
    player = _player(stale_current, heard=False)
    player.live_editable_handoff = lambda: editable
    candidate = _Entry("following-new")

    conflict = _conflict(player, candidate)
    result = plan_queue_edit(
        player,
        None,
        [candidate],
        voice_action="discard",
        voice_revision=conflict.detail["revision"],
    )
    assert result.track is editable


def test_candidate_preserves_duplicate_occurrence_and_skips_reserved_liners() -> None:
    outgoing = _Entry("same-path", "occurrence-a")
    duplicate = _Entry("same-path", "occurrence-b")
    reserved = _Entry("reserved.wav", kind="liner")
    other_liner = _Entry("other.wav", kind="liner")
    track = _track(entry=outgoing, from_queue=True, next_reserved_liners=(reserved,))
    player = _player(track, queued_next=_Entry("old-queue-head"))

    result = plan_queue_edit(player, None, [outgoing, reserved, other_liner, duplicate])
    assert result.candidate is duplicate


def test_no_change_or_non_live_mode_has_no_queue_plan() -> None:
    current_next = _Entry("same", "one")
    track = _track(next_entry=current_next)
    player = _player(track)
    assert plan_queue_edit(player, current_next, []) is None
    player.live_mixing = False
    assert plan_queue_edit(player, None, [_Entry("different")]) is None


def test_empty_queue_keeps_automatic_next_and_its_voice_unchanged() -> None:
    automatic = _Entry("automatic-next")
    track = _track(next_entry=automatic, voice_breaks=(_Break("dj", "dj", "announcer"),))
    player = _player(track)

    # Clearing/reordering an already empty user queue leaves the handoff's
    # automatically selected next song and its speech untouched.
    assert plan_queue_edit(player, None, []) is None


def test_appending_after_unchanged_selected_head_does_not_prompt() -> None:
    head = _Entry("selected-head")
    later = _Entry("later")
    track = _track(next_entry=_Entry("automatic-next"))
    player = _player(track, queue=(head,))

    assert plan_queue_edit(player, None, [head, later]) is None


def test_duplicate_same_object_occurrence_skips_only_reserved_queue_occurrence() -> None:
    shared = _Entry("same-library-entry")
    old_next = _Entry("old-next")
    other = _Entry("other-current")
    track = _track(entry=shared, next_entry=old_next, from_queue=True)
    player = _player(track, queued_next=other)

    # The first `shared` occurrence is the outgoing reserved occurrence. The
    # second use of the exact same library object remains a valid queue item.
    result = plan_queue_edit(player, None, [shared, shared])
    assert result is not None
    assert result.candidate is shared


def test_break_desk_fallback_is_used_only_without_voice_breaks_field() -> None:
    candidate = _Entry("new-next")
    voice = _Break("legacy-dj", "dj", "announcer")
    track = _track()
    del track.voice_breaks
    track.break_events = ((0, "legacy-dj", 50),)
    player = _player(track)
    player.break_desk = SimpleNamespace(get=lambda key: voice if key == voice.break_id else None)

    conflict = _conflict(player, candidate)
    result = plan_queue_edit(
        player,
        None,
        [candidate],
        voice_action="discard",
        voice_revision=conflict.detail["revision"],
    )
    assert result.breaks == ()


def test_ready_handoff_change_is_detected_even_when_current_candidate_is_unchanged() -> None:
    a, b, c, e = (_Entry(x) for x in "ABCE")
    primary = _track(entry=a, next_entry=b)
    ready = _track(entry=b, next_entry=c, voice_breaks=(_Break("b-to-c", "dj", "announcer"),))
    player = _player(primary, queue=(b, c), queued_next=None)
    player._playout = SimpleNamespace(ready=lambda: ready)

    with pytest.raises(VoiceQueueConflict) as caught:
        plan_queue_edit(player, b, [e])
    result = plan_queue_edit(
        player, b, [e], voice_action="discard", voice_revision=caught.value.detail["revision"]
    )
    assert result.track is ready
    assert result.candidate is e
    assert result.breaks == ()


def test_ready_handoff_speech_is_carried_if_its_outgoing_occurrence_remains_queued() -> None:
    a, b, c, x = (_Entry(name) for name in "ABCX")
    primary = _track(entry=a, next_entry=b)
    ready = _track(entry=b, next_entry=c, voice_breaks=(_Break("b-to-c", "dj", "announcer"),))
    player = _player(primary, queue=(b, c))
    player._playout = SimpleNamespace(ready=lambda: ready)

    result = plan_queue_edit(player, x, [b, c])
    assert result.track is primary and result.candidate is x
    assert result.following[0].track is ready
    assert result.following[0].outgoing is b
    assert result.following[0].breaks[0].break_id == "b-to-c"


def test_ready_handoff_edit_keeps_duplicate_occurrences_distinct() -> None:
    a, b, c = (_Entry(name, "same" if name == "B" else None) for name in "ABC")
    b2 = _Entry("B", "other")
    primary = _track(entry=a, next_entry=b)
    ready = _track(entry=b, next_entry=c, voice_breaks=(_Break("b-to-c", "dj", "announcer"),))
    player = _player(primary, queue=(b, c))
    player._playout = SimpleNamespace(ready=lambda: ready)

    # A same-path duplicate is a different occurrence; the exact B that is
    # playing remains present, so its speech stays attached to B.
    result = plan_queue_edit(player, b2, [b, c])
    assert result.following[0].outgoing is b


def test_replacing_current_outgoing_transfers_following_voice_to_new_edge() -> None:
    a, b, c, x, e = (_Entry(name) for name in "ABCXE")
    current = _track(entry=a, next_entry=b)
    ready = _track(entry=b, next_entry=c, voice_breaks=(_Break("b-to-c", "dj", "announcer"),))
    player = _player(current, queue=(b, c))
    player._playout = SimpleNamespace(ready=lambda: ready)

    with pytest.raises(VoiceQueueConflict) as caught:
        plan_queue_edit(player, x, [e])
    result = plan_queue_edit(
        player, x, [e], voice_action="keep", voice_revision=caught.value.detail["revision"]
    )
    assert result.track is current and result.candidate is x
    assert result.following[0].track is ready
    assert result.following[0].outgoing is x
    assert result.following[0].candidate is e


def test_automatic_ready_successor_is_not_replanned_without_queue_occurrence() -> None:
    a, b, c = (_Entry(name) for name in "ABC")
    current = _track(entry=a, next_entry=b)
    ready = _track(entry=b, next_entry=c, voice_breaks=(_Break("automatic", "dj", "announcer"),))
    player = _player(current)
    player._playout = SimpleNamespace(ready=lambda: ready)
    assert plan_queue_edit(player, None, []) is None


def test_secondary_independent_context_is_not_lost_when_music_voice_unchanged() -> None:
    a, b, c, x = (_Entry(name) for name in "ABCX")
    ad = _Break("ad", "ad", "ads")
    current = _track(entry=a, next_entry=b)
    ready = _track(entry=b, next_entry=c, voice_breaks=(ad,))
    player = _player(current, queue=(b, c))
    player._playout = SimpleNamespace(ready=lambda: ready)

    result = plan_queue_edit(player, x, [b, c])
    assert result.candidate is x
    assert len(result.following) == 1
    assert result.following[0].track is ready
    assert result.following[0].breaks == (ad,)
