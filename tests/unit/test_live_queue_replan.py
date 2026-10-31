from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj.config import PlaybackConfig
from autodj.live_audio import AudioLayer, BufferedFile, LiveAudio
from autodj.live_queue import QueueEdit, plan_queue_edit
from autodj.mixbus import BusEvents, MixBus, RenderedTrack
from autodj.player import Player
from tests.unit._fakes import FakeClock, make_cfg_mock, make_entry, make_sim_index


@pytest.fixture
def player(monkeypatch):
    entries = [make_entry(i) for i in range(4)]
    cfg = make_cfg_mock()
    cfg.playback = PlaybackConfig(
        transition_mode="saved_points",
        crossfade_seconds=1.0,
        loudness_processing="off",
        pick_mode="random",
    )
    result = Player(cfg, make_sim_index(2), dry_run=True)
    result.live_mixing = True
    result.bus = None
    return result, entries


def _audio(frames: int, value: float, *, split: int | None = None, tail: float | None = None):
    samples = np.full((frames, 2), value, np.float32)
    if split is not None and tail is not None:
        samples[split:] = tail
    return LiveAudio(frames, (AudioLayer(samples, 0, frames),))


def _render(entry, next_entry, audio, *, overlap: int) -> RenderedTrack:
    return RenderedTrack(
        entry,
        audio,
        next_entry,
        0,
        "none",
        overlap_frames=overlap,
        full_audio=audio,
    )


def _install_bus(player: Player, track: RenderedTrack) -> None:
    player._playout.ready = lambda: track
    player._playout.pop = lambda: track
    player.bus = MixBus(
        BusEvents(
            on_track_start=lambda started: setattr(player, "_playing_render", started),
            on_position=lambda _position: None,
            on_need_track=player._take_render,
        ),
        eq_gains=lambda: (1.0, 1.0, 1.0),
        clock=FakeClock(),
    )


def _accept_changed_next(player: Player, track: RenderedTrack, chosen) -> None:
    decision = plan_queue_edit(player, chosen, [])
    assert decision is not None
    with player._state.queue_lock:
        player._state.queued_next = chosen
        player.refresh_playout()
    player.accept_live_queue_edit(decision)


def test_repeated_occurrence_edit_never_consumes_its_sibling(player):
    """ADR-0004-22: repeated occurrence edit never consumes its sibling."""
    player, (song, _, _, _) = player
    first = replace(song)
    second = replace(song)
    first.occurrence_id = "first"
    second.occurrence_id = "second"
    player._state.queued_next = first
    player._state.queue = [second]

    assert player._peek_user_queue(second) is first
    assert player._commit_queue_pick(second)
    assert player._state.queued_next is first
    assert player._state.queue == []
    assert not player._commit_queue_pick(second)
    player._state.queue = [second]
    player._discard_failed_queue_pick(second)
    assert player._state.queued_next is first
    assert player._state.queue == []


def test_confirmation_materializes_prefix_once_without_replacing_owned_entries(player):
    """ADR-0004-22: confirmation materializes prefix once without replacing owned entries."""
    from autodj.hour_preview import Item
    from autodj.live_queue import capture_voice_targets

    player, (current, selected, queued, predicted) = player
    player._state.current_track = current
    player._state.queue = [queued]
    player._playing_render = _render(current, selected, _audio(100, 0.2), overlap=0)
    rows = tuple(
        Item("song", float(i * 100), 100.0, status, entry)
        for i, (status, entry) in enumerate(
            [
                ("on_air", current),
                ("picked", selected),
                ("queued", queued),
                ("predicted", predicted),
                ("predicted", predicted),
            ]
        )
    )
    targets = capture_voice_targets(player, rows, now=0.0)
    assert player._state.queue == [queued]
    assert not hasattr(predicted, "occurrence_id")
    target = targets[-1]
    confirmed = player.confirm_voice_target(target.target_id, target.revision)
    waiting = player._state.queue
    assert len(waiting) == 4
    assert waiting[0] is selected and waiting[1] is queued
    assert waiting[2].path == waiting[3].path == predicted.path
    assert waiting[2].occurrence_id != waiting[3].occurrence_id
    assert confirmed.selection == "confirmed"
    assert confirmed.estimated_airtime == 400.0
    assert player._playing_render.next_entry is selected
    again = player.confirm_voice_target(confirmed.target_id, confirmed.revision)
    assert again.target_id == confirmed.target_id
    assert len(player._state.queue) == 4


def test_confirmation_refuses_stale_snapshot_without_partial_queue_changes(player):
    from autodj.breaks import BreakError
    from autodj.hour_preview import Item
    from autodj.live_queue import capture_voice_targets

    player, (current, selected, predicted, changed) = player
    player._state.current_track = current
    player._playing_render = _render(current, selected, _audio(100, 0.2), overlap=0)
    targets = capture_voice_targets(
        player,
        tuple(
            Item("song", i * 100.0, 100, "predicted", entry)
            for i, entry in enumerate((current, selected, predicted))
        ),
        now=0.0,
    )
    player._state.queue.append(changed)
    with pytest.raises(BreakError):
        player.confirm_voice_target(targets[-1].target_id, targets[-1].revision)
    assert player._state.queue == [changed]


@pytest.mark.parametrize("action", ["keep", "discard"])
def test_future_pair_edits_require_one_fresh_voice_choice(tmp_path, action):
    """ADR-0004-24: future pair edits require one fresh voice choice."""
    from autodj.live_queue import VoiceQueueConflict
    from tests.unit.test_live_voice import _book_future

    player, entries, desk, item = _book_future(tmp_path)
    outgoing, incoming = entries[-2:]
    inserted = make_entry(55)
    proposed = [entries[1], outgoing, inserted, incoming]
    before = list(player._state.queue)
    with pytest.raises(VoiceQueueConflict) as caught:
        plan_queue_edit(player, None, proposed)
    detail = caught.value.detail
    assert detail["affected"][0]["proposed_pair"][1] == inserted.display_name
    assert player._state.queue == before
    with pytest.raises(VoiceQueueConflict):
        plan_queue_edit(player, None, proposed, voice_action=action, voice_revision="stale")
    decision = plan_queue_edit(
        player, None, proposed, voice_action=action, voice_revision=detail["revision"]
    )
    player._state.queue[:] = proposed
    player.accept_live_queue_edit(decision)
    updated = desk.get(item.break_id)
    if action == "keep":
        from autodj.live_queue import occurrence_ref

        assert updated.outgoing_occurrence_id == occurrence_ref(player, outgoing)
        assert updated.incoming_occurrence_id == occurrence_ref(player, inserted)
        assert updated.state == "queued"
    else:
        assert updated.state == "dropped"


def test_intact_future_pair_move_needs_no_voice_choice(tmp_path):
    """ADR-0004-24: intact future pair move needs no voice choice."""
    from tests.unit.test_live_voice import _book_future

    player, entries, desk, item = _book_future(tmp_path)
    proposed = [make_entry(56), *entries[1:]]
    decision = plan_queue_edit(player, None, proposed)
    player._state.queue[:] = proposed
    player.accept_live_queue_edit(decision)
    assert desk.get(item.break_id).state == "queued"


def test_pruned_future_target_still_allows_discard_conflict(tmp_path):
    from autodj.live_queue import VoiceQueueConflict
    from tests.unit.test_live_voice import _book_future

    player, entries, desk, item = _book_future(tmp_path)
    player._voice_targets.clear()
    with pytest.raises(VoiceQueueConflict) as caught:
        plan_queue_edit(player, None, [entries[1]])
    detail = caught.value.detail
    assert not detail["can_keep"] and detail["affected"][0]["break_id"] == item.break_id
    decision = plan_queue_edit(
        player, None, [entries[1]], voice_action="discard", voice_revision=detail["revision"]
    )
    player.accept_live_queue_edit(decision)
    assert desk.get(item.break_id).state == "dropped"


def test_future_only_decision_clears_stale_current_edit_target(player):
    player, entries = player
    player._live_queue_target = _render(entries[0], entries[1], _audio(100, 0.2), overlap=0)
    player._playing_render = _render(entries[2], entries[3], _audio(100, 0.3), overlap=0)
    player.accept_live_queue_edit(QueueEdit(None, None, "discard", (), None))
    assert player._live_queue_target is None


def test_removing_both_future_endpoints_disables_keep(tmp_path):
    """ADR-0004-24: removing both future endpoints disables keep."""
    from autodj.live_queue import VoiceQueueConflict
    from tests.unit.test_live_voice import _book_future

    player, entries, _desk, _item = _book_future(tmp_path)
    with pytest.raises(VoiceQueueConflict) as caught:
        plan_queue_edit(player, None, [entries[1]])
    assert caught.value.detail["can_keep"] is False
    with pytest.raises(VoiceQueueConflict):
        plan_queue_edit(
            player,
            None,
            [entries[1]],
            voice_action="keep",
            voice_revision=caught.value.detail["revision"],
        )


def test_combined_current_and_future_conflict_names_every_take(tmp_path):
    """ADR-0004-24: combined current and future conflict names every take."""
    from autodj.live_queue import VoiceQueueConflict
    from tests.unit.test_live_voice import _book_future, _voice

    player, entries, desk, future = _book_future(tmp_path)
    plan, _closes, _airs = player.voice_slot_status()
    current = _voice(desk, plan.slot_id)
    player.book_break(current, transition_revision=plan.transition_revision)
    with pytest.raises(VoiceQueueConflict) as caught:
        plan_queue_edit(player, None, [make_entry(80), entries[1]])
    assert {row["break_id"] for row in caught.value.detail["affected"]} == {
        current.break_id,
        future.break_id,
    }
    assert all("proposed_pair" in row for row in caught.value.detail["affected"])


def test_confirmation_preserves_queued_liner_but_binds_only_music(player, monkeypatch):
    from autodj.hour_preview import Item
    from autodj.live_queue import capture_voice_targets

    player, (current, selected, incoming, liner) = player
    player._state.current_track = current
    player._state.queue[:] = [selected, liner]
    player._playing_render = _render(current, selected, _audio(100, 0.2), overlap=0)
    monkeypatch.setattr(player, "_is_liner", lambda entry: entry is liner)
    targets = capture_voice_targets(
        player,
        (
            Item("song", 0, 100, "on_air", current),
            Item("song", 100, 100, "queued", selected),
            Item("liner", 198, 2, "queued", liner),
            Item("song", 200, 100, "predicted", incoming),
        ),
        now=0,
    )
    target = player.confirm_voice_target(targets[0].target_id, targets[0].revision)
    assert target.outgoing is selected
    assert target.incoming.path == incoming.path
    assert player._state.queue[1] is liner


def test_confirmation_revalidates_entire_prefix_before_mutating(player):
    """ADR-0004-22: confirmation revalidates entire prefix before mutating."""
    from autodj.breaks import BreakError
    from autodj.hour_preview import Item
    from autodj.live_queue import capture_voice_targets

    player, entries = player
    player._state.current_track = entries[0]
    target = capture_voice_targets(
        player,
        tuple(Item("song", i * 100, 100, "predicted", entry) for i, entry in enumerate(entries)),
        now=0,
    )[-1]
    object.__setattr__(target.prefix[0], "length", 0)
    with pytest.raises(BreakError):
        player.confirm_voice_target(target.target_id, target.revision)
    assert not player._state.queue


def test_confirmation_wakes_control_when_current_successor_is_not_selected(player, monkeypatch):
    from autodj.hour_preview import Item
    from autodj.live_queue import capture_voice_targets

    player, entries = player
    player._state.current_track = entries[0]
    player._playing_render = replace(
        _render(entries[0], None, _audio(100, 0.2), overlap=0), handoff_pending=True
    )
    requested = MagicMock()
    monkeypatch.setattr(player._playout, "request_plan", requested)
    target = capture_voice_targets(
        player,
        tuple(Item("song", i * 100, 100, "predicted", entry) for i, entry in enumerate(entries)),
        now=0,
    )[-1]
    player.confirm_voice_target(target.target_id, target.revision)
    assert player._live_queue_dirty
    requested.assert_called_once()


def test_two_confirmation_editors_cannot_install_the_same_prefix_twice(player):
    """ADR-0004-22: two confirmation editors cannot install the same prefix twice."""
    from concurrent.futures import ThreadPoolExecutor

    from autodj.breaks import BreakError
    from autodj.hour_preview import Item
    from autodj.live_queue import capture_voice_targets

    player, entries = player
    player._state.current_track = entries[0]
    target = capture_voice_targets(
        player,
        tuple(Item("song", i * 100, 100, "predicted", entry) for i, entry in enumerate(entries)),
        now=0,
    )[-1]

    def confirm(_):
        try:
            return player.confirm_voice_target(target.target_id, target.revision)
        except BreakError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(confirm, range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(player._state.queue) == len(entries) - 1


def test_live_edit_preserves_heard_prefix_and_bus_cursor(player):
    """ADR-0004-09, ADR-0015-09: heard samples and cursor persist as only the unheard tail changes."""
    player, (current, old_next, new_next, _) = player
    block = MixBus.BLOCK
    old = _render(
        current, old_next, _audio(block * 12, 0.2, split=block * 8, tail=0.8), overlap=block * 4
    )
    replacement = _render(current, new_next, _audio(block * 12, 0.4), overlap=block * 4)
    _install_bus(player, old)
    player.bus.start_set()
    aired = np.concatenate([player.bus.render_block() for _ in range(2)])
    heard_position = player.bus.live_position(old)
    assert heard_position == block * 2
    _accept_changed_next(player, old, new_next)
    player._rewind_render_cursor = lambda _track: None
    player._next_rendered_serial = lambda: replacement
    player._playout.reset = MagicMock(side_effect=lambda prepare: None)

    player._refresh_live_queue()

    installed = player._playing_render
    assert installed is not old
    assert player.bus.live_position(installed) == heard_position
    prefix = installed.audio.read(0, heard_position)
    np.testing.assert_array_equal(prefix, aired)
    outgoing_block = player.bus.render_block()
    np.testing.assert_allclose(outgoing_block, 0.4)


@pytest.mark.parametrize("prepared_at", [4, 6])
def test_continuation_admits_effect_only_before_its_first_frame(player, monkeypatch, prepared_at):
    from autodj.live_transitions import build_live_transition
    from tests.unit.test_live_playout import _UnconsumedDeck
    from tests.unit.test_live_transitions import _parity_player

    player, (current, upcoming, _, _) = player
    samples = np.full((8 * 44_100, 2), 0.2, np.float32)
    renderer, a, b, _requests = _parity_player(
        monkeypatch, {"out.wav": samples, "in.wav": samples}, 1.0, "echo_out", None
    )
    renderer._MIN_FX_DURATION_S = {"echo_out": 3.0}
    renderer._live_file = lambda _entry: _UnconsumedDeck(len(samples), 0.2)
    rendered = build_live_transition(renderer, a, b, 0)
    assert rendered is not None
    replacement = replace(
        rendered,
        entry=current,
        next_entry=upcoming,
        next_live_source=_UnconsumedDeck(len(samples), 0.2),
    )
    original = replace(
        _render(current, None, _audio(len(samples), 0.2), overlap=0), handoff_pending=True
    )
    _install_bus(player, original)
    player.bus.start_set()
    for _ in range(prepared_at * 50):
        player.bus.render_block()
    player._rewind_render_cursor = lambda _track: None
    player._next_rendered_serial = lambda: replacement
    player._playout.reset = MagicMock()
    player._live_queue_dirty = True

    player._refresh_live_queue()

    installed = player._playing_render
    if prepared_at == 6:
        # The effect began at 5 s, even though the crossfade starts at 7.8 s.
        np.testing.assert_allclose(player.bus.render_block(), 0.2)
        assert installed.overlap_frames == 0
        assert installed.next_start_offset == replacement.next_entry_offset
    else:
        assert installed.overlap_frames == replacement.overlap_frames
        for _ in range(50):
            np.testing.assert_allclose(player.bus.render_block(), 0.2)
        np.testing.assert_allclose(player.bus.render_block(), 0.1)


def test_after_incoming_is_audible_queue_edit_waits_for_continuation_promotion(player):
    """ADR-0004-09: A ready body has no successor to replan until it becomes current."""
    player, (current, old_next, chosen, later) = player
    block = MixBus.BLOCK
    current_track = _render(current, old_next, _audio(block * 12, 0.1), overlap=block * 4)
    following = replace(
        _render(old_next, chosen, _audio(block * 8, 0.2), overlap=block * 2),
        handoff_pending=True,
        next_entry=None,
    )
    _install_bus(player, current_track)
    player.bus.start_set()
    for _ in range(9):
        player.bus.render_block()
    assert player.bus.incoming_started(current_track)
    player._playout.ready = lambda: following

    assert player.live_editable_handoff() is following
    decision = plan_queue_edit(player, later, [])
    assert decision is None
    with player._state.queue_lock:
        player._state.queued_next = later
        player.refresh_playout()
    player._rewind_render_cursor = MagicMock()
    player._next_rendered_serial = MagicMock(
        side_effect=AssertionError("current handoff is audible")
    )
    player._playout.reset = MagicMock(side_effect=lambda prepare: prepare())

    player._refresh_live_queue()

    player._playout.reset.assert_called_once()
    assert player._playing_render is current_track
    assert player.bus._current is current_track
    player._rewind_render_cursor.assert_called_once_with(following)


def test_concurrent_queue_revision_does_not_install_stale_replacement(player):
    """ADR-0004-09: a replacement rendered from a stale queue revision is rejected."""
    player, (current, old_next, new_next, _) = player
    block = MixBus.BLOCK
    old = _render(current, old_next, _audio(block * 12, 0.2), overlap=block * 4)
    stale = _render(current, new_next, _audio(block * 12, 0.7), overlap=block * 4)
    _install_bus(player, old)
    player.bus.start_set()
    player.bus.render_block()
    _accept_changed_next(player, old, new_next)
    player._rewind_render_cursor = lambda _track: None
    requested = []
    player._playout.request_plan = lambda: requested.append(True)

    def render_and_race():
        player._live_queue_revision += 1
        return stale

    player._next_rendered_serial = render_and_race
    player._playout.reset = MagicMock()
    before = player.bus.live_position(old)

    player._refresh_live_queue()

    assert player._playing_render is old
    assert player.bus._current is old
    assert player.bus.live_position(old) == before
    assert requested == [True]
    player._playout.reset.assert_not_called()


def test_failed_replacement_leaves_boundary_held(player):
    """ADR-0004-09: a failed live replacement holds its boundary instead of falling back."""
    player, (current, old_next, new_next, _) = player
    block = MixBus.BLOCK
    boundary = block * 4
    old = _render(
        current, old_next, _audio(block * 6, 0.2, split=boundary, tail=0.8), overlap=block * 2
    )
    _install_bus(player, old)
    player.bus.start_set()
    np.testing.assert_allclose(player.bus.render_block(), 0.2)
    _accept_changed_next(player, old, new_next)
    player._rewind_render_cursor = lambda _track: None
    player._next_rendered_serial = lambda: None
    requests = []
    player._playout.request_plan = lambda: requests.append(True)

    player._refresh_live_queue()
    assert player._live_queue_dirty
    assert requests == [True]
    for _ in range(3):
        np.testing.assert_allclose(player.bus.render_block(), 0.2)
    assert player.bus.live_position(old) == boundary
    np.testing.assert_array_equal(player.bus.render_block(), np.zeros((block, 2), np.float32))
    assert player.bus.live_position(old) == boundary


def test_take_render_does_not_pop_or_commit_queue_while_live_audio_pending(player):
    player, (current, next_entry, *_rest) = player

    class Pending(BufferedFile):
        def ready(self, position, frames):
            return False

    source = Pending.__new__(Pending)
    audio = LiveAudio(100, (AudioLayer(source, 0, 100),))
    rendered = _render(current, next_entry, audio, overlap=0)
    rendered = replace(rendered, from_queue=True)
    player._playout.ready = MagicMock(return_value=rendered)
    player._playout.pop = MagicMock(return_value=rendered)
    player._commit_queue_pick = MagicMock(return_value=True)

    assert player._take_render() is None
    player._playout.pop.assert_not_called()
    player._commit_queue_pick.assert_not_called()


def test_ready_only_queue_edit_waits_for_later_selection_and_preserves_current_audio(player):
    """ADR-0004-09: future cursor edits preserve current audio until promotion."""
    player, (a, b, c, e) = player
    block = MixBus.BLOCK
    current = _render(a, b, _audio(block * 12, 0.25), overlap=block * 4)
    ready = replace(
        _render(b, c, _audio(block * 8, 0.5), overlap=block * 2),
        handoff_pending=True,
        next_entry=None,
    )
    _install_bus(player, current)
    player.bus.start_set()
    heard = np.concatenate([player.bus.render_block() for _ in range(9)])
    position = player.bus.live_position(current)
    assert player.bus.incoming_started(current)
    player._playout.ready = lambda: ready
    player._state.queued_next = b
    player._state.queue = [c]

    # B's future successor is selected from the changed queue after promotion.
    decision = plan_queue_edit(player, b, [e])
    assert decision is None
    with player._state.queue_lock:
        player._state.queued_next = b
        player._state.queue = [e]
        player.refresh_playout()
    rewound = []
    player._rewind_render_cursor = lambda track: rewound.append(track)
    player._playout.reset = MagicMock(side_effect=lambda prepare: prepare())

    player._refresh_live_queue()

    assert player._playing_render is current
    assert player.bus._current is current
    assert player.bus.live_position(current) == position
    np.testing.assert_array_equal(current.audio.read(0, len(heard)), heard)
    assert rewound == [ready]


def test_accept_live_queue_edit_maps_only_the_current_handoff(player):
    player, (a, b, x, e) = player
    current = _render(a, b, _audio(100, 0.1), overlap=0)
    ready = _render(b, e, _audio(100, 0.2), overlap=0)
    player._playing_render = current
    decision = QueueEdit(
        current,
        x,
        None,
        (),
        None,
    )

    player.accept_live_queue_edit(decision)

    assert player._live_queue_edits[id(a)] is decision
    assert id(x) not in player._live_queue_edits
    assert id(ready.entry) not in player._live_queue_edits


def test_later_ready_edit_does_not_override_pending_current_target(player):
    player, (a, b, c, e) = player
    current = _render(a, b, _audio(100, 0.1), overlap=0)
    ready = replace(
        _render(b, c, _audio(100, 0.2), overlap=0),
        handoff_pending=True,
        next_entry=None,
    )
    player._playing_render = current
    current_decision = QueueEdit(current, c, None, (), None)
    player.accept_live_queue_edit(current_decision)
    player.live_editable_handoff = lambda: ready
    ready_decision = plan_queue_edit(player, e, [])

    assert ready_decision is None

    assert player._live_queue_target is current
    assert id(ready.entry) not in player._live_queue_edits


def test_merging_frozen_future_chains_requires_discard(tmp_path):
    """ADR-0004-24: Keep cannot silently omit either frozen chain when pairs merge."""
    from autodj.breaks import TransitionItem
    from autodj.live_queue import VoiceQueueConflict, resolve_voice_target
    from tests.unit.test_live_voice import _book_future, _voice

    player, entries, desk, later = _book_future(tmp_path)
    target = next(row for row in player._voice_targets.values() if row.incoming is entries[2])
    target = resolve_voice_target(player, target.target_id)
    earlier = _voice(desk, target.target_id)
    earlier.run_id = target.run_id
    player.book_break(earlier, transition_revision=target.revision)
    later.transition_items += (
        TransitionItem("liner:kept", "liner", "automatic", "Frozen clip", entry=entries[0]),
    )
    with pytest.raises(VoiceQueueConflict) as conflict:
        plan_queue_edit(player, None, [entries[1], entries[3]])
    assert conflict.value.detail["can_keep"] is False
    assert "frozen" in conflict.value.detail["keep_reason"].lower()


def test_confirmation_counts_only_new_songs_not_owned_music_or_liners(player):
    """ADR-0004-22: confirmation counts only new songs not owned music or liners."""
    from autodj.hour_preview import Item
    from autodj.live_queue import capture_voice_targets

    player, (current, queued, liner, predicted) = player
    player._state.current_track = current
    player._state.queue[:] = [queued, liner]
    player._is_liner = lambda entry: entry is liner
    rows = (
        Item("song", 0, 100, "on_air", current),
        Item("song", 100, 100, "queued", queued),
        Item("liner", 200, 5, "queued", liner),
        Item("song", 205, 100, "predicted", predicted),
    )
    target = capture_voice_targets(player, rows, now=0)[-1]
    assert target.view()["songs_to_queue"] == 1


def test_confirmation_refuses_missing_duration_without_type_error(player):
    """ADR-0004-27: confirmation refuses missing duration without type error."""
    from autodj.breaks import BreakError
    from autodj.hour_preview import Item
    from autodj.live_queue import capture_voice_targets

    player, entries = player
    player._state.current_track = entries[0]
    target = capture_voice_targets(
        player,
        tuple(Item("song", i * 100, 100, "predicted", entry) for i, entry in enumerate(entries)),
        now=0,
    )[-1]
    object.__setattr__(target.prefix[0], "length", None)
    with pytest.raises(BreakError, match="no longer playable") as failure:
        player.confirm_voice_target(target.target_id, target.revision)
    assert failure.value.status == 409
    assert player._state.queue == []


def test_confirmation_catalog_reads_precede_bus_guard(player, monkeypatch):
    """ADR-0004-22: confirmation catalog reads precede bus guard."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    from autodj.hour_preview import Item
    from autodj.live_queue import capture_voice_targets

    player, entries = player
    held = False
    queries = []

    @contextmanager
    def guard():
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False

    def versions():
        assert not held, "catalog query under audio boundary guard"
        queries.append(True)
        return (1, 2)

    def lookup(path):
        assert not held, "catalog lookup under audio boundary guard"
        return next(entry for entry in entries if entry.path == path)

    player._library = SimpleNamespace(
        catalog=SimpleNamespace(change_versions=versions),
        entry_for_path=lookup,
        entries_snapshot=lambda _kind: (),
    )
    player._state.current_track = entries[0]
    target = capture_voice_targets(
        player,
        tuple(Item("song", i * 100, 100, "predicted", entry) for i, entry in enumerate(entries)),
        now=0,
    )[-1]
    player.bus = SimpleNamespace(queue_edit_guard=guard)
    monkeypatch.setattr(player, "refresh_playout", lambda: None)
    confirmed = player.confirm_voice_target(target.target_id, target.revision)
    assert confirmed.selection == "confirmed"
    assert queries


def test_proposal_revision_change_prunes_cache_before_allocating(player, monkeypatch):
    """ADR-0004-22: proposal revision change prunes cache before allocating."""
    from autodj import live_queue

    player, entries = player
    live_queue.proposed_occurrence(player, entries[0], "first")
    old_cache = player._voice_proposals
    player._live_queue_revision += 1
    new_id = live_queue.uuid.uuid4

    def check_pruned():
        assert player._voice_proposals == {}
        assert old_cache == {}
        return new_id()

    monkeypatch.setattr(live_queue.uuid, "uuid4", check_pruned)
    live_queue.proposed_occurrence(player, entries[0], "second")
    assert len(player._voice_proposals) == 1


def test_proposal_cache_is_bounded_and_retains_recent_conflict_retries(player):
    """ADR-0004-22: proposal cache is bounded and retains recent conflict retries."""
    from autodj.live_queue import proposed_occurrence

    player, entries = player
    for index in range(300):
        proposed_occurrence(player, entries[0], f"edit:{index}")
    assert len(player._voice_proposals) <= 256
    recent = proposed_occurrence(player, entries[0], "edit:299")
    retry = proposed_occurrence(player, entries[0], "edit:299")
    assert recent.occurrence_id == retry.occurrence_id
    # A full 180-row confirmation fits without evicting its own first placement.
    first = proposed_occurrence(player, entries[0], "confirm:0")
    for index in range(1, 180):
        proposed_occurrence(player, entries[0], f"confirm:{index}")
    assert proposed_occurrence(player, entries[0], "confirm:0").occurrence_id == first.occurrence_id
