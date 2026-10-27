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
