"""Book a voice track into the current streaming handoff before its outro."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from autodj.breaks import Break, BreakError, SlotPlan, TransitionItem, new_id
from autodj.live_audio import LiveAudio, splice_audio
from autodj.live_queue import QueueEdit
from autodj.stereo import SAMPLE_RATE

if TYPE_CHECKING:
    from autodj.mixbus import RenderedTrack
    from autodj.player import Player


def _cutoff(player: Player, track: RenderedTrack) -> int:
    points = player.saved_points(track.entry)
    outro = points.get("outro")
    if outro is None:
        outro = points.get("segue") or points.get("end") or track.entry.length
    frame = round((float(outro) * SAMPLE_RATE - track.start_offset) / track.playback_rate)
    return max(0, min(frame, player.prepared_tail_boundary(track)))


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
        cached = getattr(player, "_live_voice_slot", None)
        if cached is not None and cached[0] is track:
            plan = cached[1]
        else:
            plan = SlotPlan(
                new_id(),
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
                        "booked_voice",
                        item.source,
                        item.label or "Booked break",
                        committed=True,
                        editable=False,
                    )
                )
            plan.transition_items = tuple(items)
            plan.transition_seed = hashlib.sha256(plan.slot_id.encode()).hexdigest()
            player._live_voice_slot = (track, plan)
        plan.transition_revision = player._transition_revision(plan)
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
    """Prepare off the bus, then admit the result only before the same cutoff."""
    bus, desk = player.bus, player.break_desk
    if bus is None or desk is None:
        raise BreakError(409, "That song change is no longer open", "slot_closed")
    with player._clock_selection_lock:
        plan, _closes, airs = slot_status(player)
        if plan is None or plan.slot_id != item.slot_id:
            raise BreakError(409, "That song change is no longer open", "slot_closed")
        cached = player._live_voice_slot
        if cached is None or cached[1].slot_id != plan.slot_id:
            raise BreakError(409, "That song change is no longer open", "slot_closed")
        track = cached[0]
        cutoff = _cutoff(player, track)
        queue_revision = player._live_queue_revision
        added = False
        installed = False
        try:
            with bus.queue_edit_guard(), player._slot_lock:
                if (
                    player._live_queue_revision != queue_revision
                    or player._live_queue_dirty
                    or not bus.can_replace_unheard_tail(track, before_frame=cutoff)
                ):
                    raise BreakError(409, "That song change is no longer open", "slot_closed")
                plan = player._book_break_in_plan(item, plan, revision, choices)
                added = True
            taken = tuple(desk.take(plan.slot_id, time.time() + airs))
            if not any(take.break_id == item.break_id for take in taken):
                raise BreakError(409, "The recording expired before this song change", "expired")
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
            old_sources = player._render_live_sources
            player._render_live_sources = (
                track.entry,
                track.live_source,
                track.next_entry,
                track.next_live_source,
            )
            try:
                rendered = player._render_track(
                    track.entry,
                    track.next_entry,
                    track.start_offset,
                    glide_in=track.glide_in,
                    liners=liners,
                    breaks=(*track.voice_breaks, *taken),
                    intro=intro,
                    transition_order=plan.transition_order,
                    transition_items=plan.transition_items,
                )
            finally:
                player._render_live_sources = old_sources
            if (
                rendered is None
                or not isinstance(rendered.audio, LiveAudio)
                or not any(event[1] == item.break_id for event in rendered.break_events)
            ):
                raise BreakError(
                    409, "The voice track could not join this song change", "render_failed"
                )
            cutoff = min(cutoff, player.prepared_tail_boundary(rendered))
            with player._set_lock, bus.queue_edit_guard(), player._state.queue_lock:
                if (
                    player._live_queue_revision != queue_revision
                    or player._playing_render is not track
                    or not bus.can_replace_unheard_tail(track, before_frame=cutoff)
                ):
                    raise BreakError(409, "That song change is no longer open", "slot_closed")
                assert isinstance(track.audio, LiveAudio)
                replacement = replace(
                    track,
                    audio=splice_audio(track.audio, rendered.audio, bus.live_position(track)),
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
                player._live_voice_slot = (replacement, plan)
                installed = True
                ready = player._render_ahead.ready()
                if ready is not None:
                    player._live_queue_edits.setdefault(
                        id(ready.entry),
                        QueueEdit(
                            ready,
                            ready.next_entry,
                            None,
                            ready.voice_breaks,
                            ready.incoming_intro,
                            outgoing=ready.entry,
                            candidate_mode=ready.next_pick_mode,
                        ),
                    )
                player._render_ahead.reset(lambda: player._skip_past_render(replacement))
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
            return replace(plan)
        finally:
            if added and not installed:
                desk.drop(
                    item.break_id, "the voice-track booking did not reach the current handoff"
                )
