"""Book a voice track into the current streaming handoff before its outro."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, TypeGuard, cast

from autodj.breaks import Break, BreakError, SlotPlan, TransitionItem, new_id
from autodj.live_audio import LiveAudio, splice_audio
from autodj.live_queue import QueueEdit
from autodj.stereo import SAMPLE_RATE

if TYPE_CHECKING:
    from autodj.live_playout import LivePlayout
    from autodj.mixbus import RenderedTrack
    from autodj.player import Player

_TRACK_INTRO = object()


def _cutoff(player: Player, track: RenderedTrack) -> int:
    points = player.saved_points(track.entry)
    outro = points.get("outro")
    if outro is None:
        outro = points.get("segue") or points.get("end") or track.entry.length
    frame = round((float(outro) * SAMPLE_RATE - track.start_offset) / track.playback_rate)
    return max(0, min(frame, player.prepared_tail_boundary(track)))


def _render_handoff(
    player: Player,
    track: RenderedTrack,
    breaks: Sequence[Break],
    transition_order: Sequence[str],
    transition_items: Sequence[TransitionItem],
    *,
    liners: Sequence[tuple[Any, bool]] | None = None,
    intro: Any = _TRACK_INTRO,
) -> RenderedTrack | None:
    """Render one replacement from the same current-to-next handoff."""
    old_sources = player._render_live_sources
    player._render_live_sources = (
        track.entry,
        track.live_source,
        track.next_entry,
        track.next_live_source,
    )
    try:
        return player._render_track(
            track.entry,
            track.next_entry,
            track.start_offset,
            glide_in=track.glide_in,
            liners=(
                tuple((entry, queued) for _at, entry, queued in track.liner_events)
                if liners is None
                else tuple(liners)
            ),
            breaks=tuple(breaks),
            intro=track.incoming_intro if intro is _TRACK_INTRO else intro,
            transition_order=tuple(transition_order),
            transition_items=tuple(transition_items),
        )
    finally:
        player._render_live_sources = old_sources


def _install_handoff(
    player: Player,
    track: RenderedTrack,
    rendered: RenderedTrack,
    *,
    cutoff: int,
    queue_revision: int,
    plan: SlotPlan | None = None,
    unheard_break_id: str | None = None,
) -> RenderedTrack:
    """Atomically splice a rendered handoff while preserving its heard prefix."""
    bus = player.bus
    assert bus is not None
    if not isinstance(rendered.audio, LiveAudio):
        raise BreakError(409, "The voice track could not join this song change", "render_failed")
    cutoff = min(cutoff, player.prepared_tail_boundary(rendered))
    with player._set_lock, bus.queue_edit_guard(), player._state.queue_lock:
        position = bus.live_position(track)
        target_started = unheard_break_id is not None and any(
            event[1] == unheard_break_id and event[0] <= position for event in track.break_events
        )
        if (
            player._live_queue_revision != queue_revision
            or player._live_queue_dirty
            or player._playing_render is not track
            or target_started
            or not bus.can_replace_unheard_tail(track, before_frame=cutoff)
        ):
            raise BreakError(409, "That song change is no longer open", "slot_closed")
        assert isinstance(track.audio, LiveAudio)
        replacement = replace(
            track,
            audio=splice_audio(track.audio, rendered.audio, position),
            full_audio=rendered.full_audio,
            next_start_offset=rendered.next_start_offset,
            next_entry_offset=rendered.next_entry_offset,
            overlap_frames=rendered.overlap_frames,
            liner_events=rendered.liner_events,
            break_events=rendered.break_events,
            intro_events=rendered.intro_events,
            intro_assets=rendered.intro_assets,
            intro_rng_traces=rendered.intro_rng_traces,
            next_reserved_liners=rendered.next_reserved_liners,
            incoming_intro=rendered.incoming_intro,
            voice_breaks=rendered.voice_breaks,
        )
        if not bus.replace_live_plan(track, replacement):
            raise BreakError(409, "That song change is no longer open", "slot_closed")
        player._playing_render = replacement
        player._playback_len = replacement.start_offset + round(
            len(replacement.audio) * replacement.playback_rate
        )
        if plan is not None:
            player._live_voice_slot = (replacement, plan)
        player._playout.reset(lambda: player._skip_past_render(replacement))
        retained_intros = {event[1] for event in replacement.intro_events}
        player._settle_intros(
            replace(
                track,
                intro_events=tuple(
                    event for event in track.intro_events if event[1] not in retained_intros
                ),
            )
        )
        if (
            track.liner_plan_key
            and player.liner_scheduler is not None
            and not any(not queued for _at, _entry, queued in replacement.liner_events)
        ):
            player.liner_scheduler.release_plan(track.liner_plan_key)
    return replacement


def _cancel_window(player: Player, item: Break) -> tuple[RenderedTrack, SlotPlan, int, str] | None:
    """Find the exact current or ready handoff that contains *item*."""
    desk, bus = player.break_desk, player.bus
    if desk is None or bus is None or not getattr(player, "live_mixing", False):
        return None
    playout = player._playout
    with bus.queue_edit_guard(), player._state.queue_lock:
        if player._live_queue_dirty:
            return None
        current = player._playing_render
        if _contains_break(current, item.break_id) and isinstance(current.audio, LiveAudio):
            starts = [
                start for start, identity, _end in current.break_events if identity == item.break_id
            ]
            position = bus.live_position(current)
            if (
                starts
                and position < min(starts)
                and bus.can_replace_unheard_tail(
                    current, before_frame=player.prepared_tail_boundary(current)
                )
                and bus.current_render() is current
            ):
                return (
                    current,
                    _plan_for_track(player, current),
                    player._live_queue_revision,
                    "current",
                )
        ready = playout.ready()
        if (
            _contains_break(ready, item.break_id)
            and not getattr(ready, "handoff_pending", False)
            and ready.next_entry is not None
            and isinstance(ready.audio, LiveAudio)
        ):
            return (
                ready,
                _plan_for_track(player, ready, cache=False),
                player._live_queue_revision,
                "ready",
            )
    return None


def _contains_break(track: RenderedTrack | None, break_id: str) -> TypeGuard[RenderedTrack]:
    return track is not None and any(item.break_id == break_id for item in track.voice_breaks)


def can_cancel(player: Player, break_id: str, *, source: str = "voice_track") -> bool:
    """Whether a voice booking is queued or an unheard current live take."""
    desk = player.break_desk
    item = desk.get(break_id) if desk is not None else None
    if item is None or item.source != source:
        return False
    if item.state == "queued":
        return True
    return item.state == "rendered" and _cancel_window(player, item) is not None


def cancel(
    player: Player,
    break_id: str,
    *,
    source: str = "voice_track",
    reason: str = "cancelled in the voice tracker",
) -> Break:
    """Cancel a queued take or remove an unheard take from the live handoff."""
    desk = player.break_desk
    if desk is None:
        raise BreakError(404, "No break has that id", "not_found")
    item = desk.get(break_id)
    if item is None or item.source != source:
        return desk.cancel(break_id, reason, source)
    if item.state == "queued":
        return desk.cancel(break_id, reason, source)
    if not getattr(player, "live_mixing", False):
        return desk.cancel(break_id, reason, source)
    if item.state != "rendered":
        return desk.cancel(break_id, reason, source)

    with player._clock_selection_lock:
        selected = _cancel_window(player, item)
        if selected is None:
            raise BreakError(
                409, "The voice track has started or can no longer be changed", item.state
            )
        track, plan, queue_revision, location = selected
        bus = player.bus
        assert bus is not None
        cutoff = player.prepared_tail_boundary(track) if location == "current" else len(track.audio)
        retained_breaks = tuple(
            voice for voice in track.voice_breaks if voice.break_id != item.break_id
        )
        key = f"break:{item.break_id}"
        items = tuple(content for content in plan.transition_items if content.item_id != key)
        order = tuple(content for content in plan.transition_order if content != key)
        updated_plan = replace(
            plan,
            transition_items=items,
            transition_order=order,
            transition_replaced=tuple(value for value in plan.transition_replaced if value != key),
            transition_actions=tuple(
                (identity, action)
                for identity, action in plan.transition_actions
                if identity != key
            ),
        )
        rendered = _render_handoff(
            player,
            track,
            retained_breaks,
            updated_plan.transition_order,
            updated_plan.transition_items,
        )
        if rendered is None or not isinstance(rendered.audio, LiveAudio):
            raise BreakError(
                409, "The voice track could not be removed from this handoff", "render_failed"
            )
        if location == "current":
            replacement = _install_handoff(
                player,
                track,
                rendered,
                cutoff=cutoff,
                queue_revision=queue_revision,
                plan=updated_plan,
                unheard_break_id=item.break_id,
            )
            desk.drop(item.break_id, reason)
            updated_plan.transition_revision = player._transition_revision(updated_plan)
            player._live_voice_slot = (replacement, updated_plan)
        else:
            _replace_ready_handoff(
                player,
                track,
                rendered,
                retained_breaks,
                queue_revision=queue_revision,
            )
            desk.drop(item.break_id, reason)
        return desk.get(item.break_id) or replace(item, state="dropped")


def _replace_ready_handoff(
    player: Player,
    expected: RenderedTrack,
    rendered: RenderedTrack,
    retained_breaks: Sequence[Break],
    *,
    queue_revision: int,
) -> None:
    """Replace only this exact future render, or reject if it was promoted."""
    bus = player.bus
    assert bus is not None
    playout = cast("LivePlayout[RenderedTrack]", player._playout)
    replacement = replace(
        expected,
        audio=rendered.audio,
        full_audio=rendered.full_audio,
        next_start_offset=rendered.next_start_offset,
        next_entry_offset=rendered.next_entry_offset,
        overlap_frames=rendered.overlap_frames,
        liner_events=rendered.liner_events,
        break_events=rendered.break_events,
        intro_events=rendered.intro_events,
        intro_assets=rendered.intro_assets,
        intro_rng_traces=rendered.intro_rng_traces,
        next_reserved_liners=rendered.next_reserved_liners,
        incoming_intro=rendered.incoming_intro,
        voice_breaks=rendered.voice_breaks,
    )
    key = id(expected.entry)
    previous = player._live_queue_edits.get(key)
    if previous is None:
        edit = QueueEdit(
            expected,
            expected.next_entry,
            None,
            tuple(retained_breaks),
            expected.incoming_intro,
            outgoing=expected.entry,
            candidate_mode=expected.next_pick_mode,
        )
    else:
        edit = replace(previous, breaks=tuple(retained_breaks))
    with bus.queue_edit_guard(), player._state.queue_lock:
        if player._live_queue_dirty or player._live_queue_revision != queue_revision:
            raise BreakError(409, "The ready song change has changed", "slot_closed")
        player._live_queue_edits[key] = edit
        if not playout.replace_ready(expected, replacement):
            if previous is None:
                player._live_queue_edits.pop(key, None)
            elif player._live_queue_edits.get(key) is edit:
                player._live_queue_edits[key] = previous
            raise BreakError(409, "The ready song change has started", "slot_closed")
        player._skip_past_render(replacement)


def _plan_for_track(player: Player, track: RenderedTrack, *, cache: bool = True) -> SlotPlan:
    """Get the transition snapshot attached to this exact live render."""
    assert track.next_entry is not None
    cached = getattr(player, "_live_voice_slot", None)
    if cache and cached is not None and cached[0] is track:
        plan = cached[1]
    else:
        plan = SlotPlan(
            track.handoff_id or new_id(),
            player._cursor_serial,
            track.entry,
            0.0,
            track.entry,
            track.next_entry,
            track.next_pick_mode,
            tuple((entry, queued) for _at, entry, queued in track.liner_events),
            track.next_clock_cursor,
            track.clock_boundary,
            track.next_clock_boundary,
            None,
            (),
            ending_offset_s=track.start_offset / SAMPLE_RATE,
            clock_name=track.next_clock_name,
            clock_revision=track.next_clock_revision,
            clock_step=track.next_clock_step,
            live=True,
            ad_steps=track.ad_steps,
        )
        items = []
        for index, (_at, entry, queued) in enumerate(track.liner_events):
            source = "queued" if queued else "automatic"
            items.append(
                TransitionItem(
                    f"liner:{index}",
                    "liner",
                    source,
                    entry.display_name,
                    entry=entry,
                    committed=queued,
                )
            )
        intro: Any = track.incoming_intro
        if intro is not None:
            items.append(
                TransitionItem(
                    f"intro:{intro.intro_id}",
                    "matched_intro",
                    "matched_intro",
                    intro.label,
                    intro=intro,
                )
            )
        for item in track.voice_breaks:
            items.append(
                TransitionItem(
                    f"break:{item.break_id}",
                    "booked_voice" if item.source == "voice_track" else "booked_break",
                    item.source,
                    item.label
                    or ("Booked voice" if item.source == "voice_track" else "Booked break"),
                    committed=True,
                    editable=False,
                )
            )
        plan.transition_items = tuple(items)
        plan.transition_seed = hashlib.sha256(
            f"{plan.slot_id}\0{id(track.next_entry)}\0{player._live_queue_revision}".encode()
        ).hexdigest()
        if cache:
            player._live_voice_slot = (track, plan)
        else:
            positions = [
                (at, f"liner:{index}")
                for index, (at, _entry, _queued) in enumerate(track.liner_events)
            ]
            positions.extend((at, f"break:{break_id}") for at, break_id, _end in track.break_events)
            positions.extend(
                (at, f"intro:{intro_id}") for at, intro_id, _end, _label in track.intro_events
            )
            valid = {item.item_id for item in plan.transition_items}
            plan.transition_order = tuple(key for _at, key in sorted(positions) if key in valid)
    plan.transition_revision = player._transition_revision(plan)
    return plan


def slot_status(player: Player) -> tuple[SlotPlan | None, float, float]:
    bus = player.bus
    if bus is None or not player._breaks_on() or player._live_queue_dirty:
        return None, 0.0, 0.0
    track = player._playing_render
    if track is None or track.next_entry is None or not isinstance(track.audio, LiveAudio):
        return None, 0.0, 0.0
    cutoff = _cutoff(player, track)
    points = player.saved_points(track.entry)
    segue = float(points.get("segue") or points.get("end") or track.entry.length)
    with bus.queue_edit_guard(), player._slot_lock:
        if not bus.can_replace_unheard_tail(track, before_frame=cutoff):
            return None, 0.0, 0.0
        position = bus.live_position(track)
        plan = _plan_for_track(player, track)
        closes = (cutoff - position) / SAMPLE_RATE
        airs = max(
            0.0,
            (segue - track.start_offset / SAMPLE_RATE) / track.playback_rate
            - position / SAMPLE_RATE,
        )
        return replace(plan), closes, airs


def book_voice(
    player: Player,
    item: Break,
    revision: str | None,
    choices: Sequence[Mapping[str, str]],
) -> SlotPlan:
    return book_live_breaks(player, (item,), item.slot_id, revision, voice_choices=choices)


def book_live_breaks(
    player: Player,
    items: Sequence[Break],
    slot_id: str,
    revision: str | None = None,
    *,
    voice_choices: Sequence[Mapping[str, str]] | None = None,
) -> SlotPlan:
    """Admit a complete speech batch to the exact current live handoff."""
    bus, desk = player.bus, player.break_desk
    if bus is None or desk is None:
        raise BreakError(409, "That song change is no longer open", "slot_closed")
    with player._clock_selection_lock:
        plan, _closes, airs = slot_status(player)
        if plan is None or plan.slot_id != slot_id:
            raise BreakError(409, "That song change is no longer open", "slot_closed")
        cached = player._live_voice_slot
        if cached is None or cached[1].slot_id != plan.slot_id:
            raise BreakError(409, "That song change is no longer open", "slot_closed")
        track = cached[0]
        cutoff = _cutoff(player, track)
        queue_revision = player._live_queue_revision
        added: list[Break] = []
        installed = False
        try:
            with bus.queue_edit_guard(), player._slot_lock:
                if (
                    player._live_queue_revision != queue_revision
                    or player._live_queue_dirty
                    or not bus.can_replace_unheard_tail(track, before_frame=cutoff)
                ):
                    raise BreakError(409, "That song change is no longer open", "slot_closed")
                if revision is not None and revision != player._transition_revision(plan):
                    raise BreakError(409, "The planned transition changed", "transition_changed")
                if voice_choices is not None:
                    assert len(items) == 1
                    plan = player._book_break_in_plan(items[0], plan, revision, voice_choices)
                    added.append(items[0])
                else:
                    for item in items:
                        if item.slot_id != slot_id:
                            raise BreakError(
                                409, "The speech batch names a different handoff", "slot_closed"
                            )
                        desk.add(item)
                        added.append(item)
                        plan.transition_items += (
                            TransitionItem(
                                f"break:{item.break_id}",
                                "booked_break",
                                item.source,
                                item.label or "Booked break",
                                committed=True,
                                editable=False,
                            ),
                        )
                        if plan.transition_order:
                            plan.transition_order += (f"break:{item.break_id}",)
                    plan.pinned = bool(added) or plan.pinned
            if not added:
                return replace(plan)
            taken = tuple(desk.take(plan.slot_id, time.time() + airs))
            expected = {item.break_id for item in added}
            if not expected.issubset({item.break_id for item in taken}):
                raise BreakError(409, "The speech expired before this song change", "expired")
            replaced = set(plan.transition_replaced)
            liners = tuple(
                (content.entry, content.source in {"queued", "clockwheel"})
                for content in plan.transition_items
                if content.kind == "liner" and content.item_id not in replaced
            )
            intro = next(
                (
                    content.intro
                    for content in plan.transition_items
                    if content.kind == "matched_intro" and content.item_id not in replaced
                ),
                None,
            )
            rendered = _render_handoff(
                player,
                track,
                (*track.voice_breaks, *taken),
                plan.transition_order,
                plan.transition_items,
                liners=liners,
                intro=intro,
            )
            if (
                rendered is None
                or not isinstance(rendered.audio, LiveAudio)
                or not expected.issubset({event[1] for event in rendered.break_events})
            ):
                raise BreakError(409, "The speech could not join this song change", "render_failed")
            first = min(event[0] for event in rendered.break_events)
            last = max(event[2] for event in rendered.break_events)
            starts_at = time.time() + max(0, first - bus.live_position(track)) / SAMPLE_RATE
            if player.voice_track_crosses_hard_boundary(starts_at, (last - first) / SAMPLE_RATE):
                raise BreakError(
                    409, "The speech chain crosses a hard scheduled boundary", "scheduled_boundary"
                )
            _install_handoff(
                player,
                track,
                rendered,
                cutoff=cutoff,
                queue_revision=queue_revision,
                plan=plan,
            )
            installed = True
            plan.transition_revision = player._transition_revision(plan)
            return replace(plan)
        finally:
            if not installed:
                for item in added:
                    desk.drop(item.break_id, "the speech batch did not reach the current handoff")
