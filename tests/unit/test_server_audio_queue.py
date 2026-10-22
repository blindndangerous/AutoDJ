"""Queue edits in server-audio mode reach the mix bus in the right order.

Uses a real Player, PlayerBridge, render-ahead worker and MixBus; only the
audio render is faked (short constant blocks) and the bus clock is fake.
Tracks are compared by path: the index hands out frozen copies of entries.

Queue picks are peeked when a render is built and removed from the queue
only when the track starts, so the queue the listener sees stays true.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj._bridge import PlayerBridge
from autodj.indexer import IndexEntry
from autodj.live_queue import VoiceQueueConflict
from autodj.mixbus import BusEvents, MixBus, RenderedTrack
from autodj.player import Player
from tests.unit._fakes import FakeClock
from tests.unit._fakes import make_cfg_mock as _make_cfg_mock
from tests.unit._fakes import make_sim_index as _make_sim_index

WAIT = 2.0
FRAMES = MixBus.BLOCK * 20  # long enough that a 150 ms skip fade is audible


@dataclass
class Harness:
    player: Player
    bridge: PlayerBridge
    bus: MixBus
    entries: list[IndexEntry]
    gate: threading.Event
    queue_at_start: list[list[str]] = field(default_factory=list)

    @property
    def worker(self):
        return self.player._render_ahead

    def play_next_track(self) -> str:
        """Render blocks until the bus starts another track; return its path."""
        before = self.player._playing_render
        for _ in range(400):
            if self.bus._current is None:
                assert self.worker.wait_ready(WAIT)
            self.bus.render_block()
            playing = self.player._playing_render
            if playing is not before:
                assert playing is not None
                return playing.entry.path
        raise AssertionError("the bus never started another track")

    def upcoming(self) -> IndexEntry:
        playing = self.player._playing_render
        assert playing is not None and playing.next_entry is not None
        return playing.next_entry

    def next_track_path(self) -> str | None:
        track = self.player._state.next_track
        return track.path if track is not None else None

    def queue_paths(self) -> list[str]:
        return [e.path for e in self.player._state.queue]

    def settle(self) -> RenderedTrack:
        """Wait for a freshly rendered ready track and return it (not popped)."""
        worker = self.worker
        with worker._cond:
            assert worker._cond.wait_for(lambda: worker._ready is not None, WAIT)
            ready = worker._ready
        assert ready is not None
        return ready

    def others(self, *exclude: IndexEntry) -> list[IndexEntry]:
        skip = {e.path for e in exclude}
        return [e for e in self.entries if e.path not in skip]


def _next_path(track: RenderedTrack) -> str | None:
    return track.next_entry.path if track.next_entry is not None else None


@pytest.fixture
def harness():
    sim = _make_sim_index(12)
    player = Player(_make_cfg_mock(), sim)
    gate = threading.Event()
    gate.set()

    def fake_render(current, nxt, offset, **_kw):
        assert gate.wait(WAIT)
        return RenderedTrack(
            current, np.full((FRAMES, 2), 0.1, np.float32), nxt, 0, "", start_offset=offset
        )

    player._render_track = fake_render  # type: ignore[method-assign]
    player.load_lyrics_in_background = MagicMock()  # type: ignore[method-assign]
    bus = MixBus(
        BusEvents(player._on_track_start, player._on_position, player._take_render),
        eq_gains=lambda: (1.0, 1.0, 1.0),
        clock=FakeClock(),
    )
    player.bus = bus
    seed = sim.entries[0]
    player._record_seed(seed)
    player.reset_render_ahead(seed, 0, pick_mode="seed")
    h = Harness(player, PlayerBridge(player=player, sim=sim), bus, list(sim.entries), gate)
    player.on_track_started = lambda _entry: h.queue_at_start.append(h.queue_paths())
    player._render_ahead.start()
    bus.start_set()
    assert h.play_next_track() == seed.path
    h.settle()
    yield h
    gate.set()
    player._render_ahead.stop(timeout=WAIT)


def _queue_behind_upcoming(h: Harness, entry: IndexEntry) -> None:
    """Queue *entry* and let it become the playing track's baked-in next."""
    assert h.bridge.queue_add(entry.path)
    assert _next_path(h.settle()) == entry.path
    h.play_next_track()
    assert h.upcoming().path == entry.path


def _add_voice_to_current_handoff(h: Harness):
    h.player.live_mixing = True
    track = h.player.live_editable_handoff()
    assert track is not None
    voice = SimpleNamespace(
        break_id="test-prepared-break",
        kind="prepared",
        source="prepared_edition",
        keep_file=False,
        path="voice.wav",
    )
    decorated = replace(track, voice_breaks=(voice,))
    if h.player._playing_render is track:
        h.player._playing_render = decorated
    else:
        with h.player._render_ahead._cond:
            assert h.player._render_ahead._ready is track
            h.player._render_ahead._ready = decorated
    return decorated, voice


def test_bridge_queue_add_requires_a_fresh_voice_choice_and_keeps_speech(harness: Harness) -> None:
    """ADR-0015-09: the bridge holds live mutation until a revision-bound keep choice."""
    track, voice = _add_voice_to_current_handoff(harness)
    entry = harness.others(track.entry, track.next_entry)[-1]
    original_queue = harness.queue_paths()

    with pytest.raises(VoiceQueueConflict) as caught:
        harness.bridge.queue_add(entry.path)
    assert caught.value.detail["code"] == "voice_track_conflict"
    revision = caught.value.detail["revision"]
    assert harness.queue_paths() == original_queue

    assert harness.bridge.queue_add(entry.path, voice_action="keep", voice_revision=revision)
    decision = harness.player._live_queue_edits[id(track.entry)]
    assert decision.voice_action == "keep"
    (kept,) = decision.breaks
    assert kept is not voice
    assert (kept.source, kept.keep_file) == ("operator_keep", True)


def test_bridge_queue_add_discards_voice_only_after_matching_choice(harness: Harness) -> None:
    """ADR-0015-09: discard is applied only after the fresh handoff revision is supplied."""
    track, voice = _add_voice_to_current_handoff(harness)
    entry = harness.others(track.entry, track.next_entry)[-1]

    with pytest.raises(VoiceQueueConflict) as caught:
        harness.bridge.queue_add(entry.path)
    revision = caught.value.detail["revision"]
    assert harness.bridge.queue_add(entry.path, voice_action="discard", voice_revision=revision)
    decision = harness.player._live_queue_edits[id(track.entry)]
    assert decision.voice_action == "discard"
    assert decision.breaks == ()
    assert voice.source == "prepared_edition"


def test_cancelled_bridge_queue_add_has_no_mutation_or_approval(harness: Harness) -> None:
    """ADR-0015-09: cancel leaves the actual Player queue and prepared voice unchanged."""
    track, _voice = _add_voice_to_current_handoff(harness)
    entry = harness.others(track.entry, track.next_entry)[-1]
    before = harness.queue_paths()

    with pytest.raises(VoiceQueueConflict):
        harness.bridge.queue_add(entry.path, voice_action="cancel")

    assert harness.queue_paths() == before
    assert id(track.entry) not in harness.player._live_queue_edits


def test_internal_show_queue_insert_defers_when_it_needs_a_voice_choice(harness: Harness) -> None:
    """ADR-0004-09: show insertion defers rather than implicitly choosing for speech."""
    track, _voice = _add_voice_to_current_handoff(harness)
    entry = harness.others(track.entry, track.next_entry)[-1]
    view = harness.bridge._queue_view()

    assert harness.bridge.queue_insert_if(entry, 0, view) is False
    assert harness.queue_paths() == []
    assert id(track.entry) not in harness.player._live_queue_edits


def test_internal_show_queue_remove_defers_when_it_needs_a_voice_choice(harness: Harness) -> None:
    """ADR-0004-09: show removal preserves the queue when there is no operator choice."""
    track, _voice = _add_voice_to_current_handoff(harness)
    entry = harness.others(track.entry, track.next_entry)[-1]
    with harness.player._state.queue_lock:
        harness.player._state.queue.append(entry)
    view = harness.bridge._queue_view()

    assert harness.bridge.queue_remove_if(0, view) is False
    assert harness.queue_paths() == [entry.path]
    assert id(track.entry) not in harness.player._live_queue_edits


def test_play_now_plays_the_chosen_track_next(harness: Harness) -> None:
    x = harness.others(harness.entries[0], harness.upcoming())[-1]
    assert harness.bridge.play_next(x.path, now=True)
    assert harness.next_track_path() == x.path
    assert harness.play_next_track() == x.path
    assert harness.player._last_pick_mode == "queue"
    assert harness.player._state.queued_next is None


def test_play_now_keeps_a_queued_upcoming_track_for_after_it(harness: Harness) -> None:
    u = harness.others(harness.entries[0], harness.upcoming())[-1]
    _queue_behind_upcoming(harness, u)  # U is baked into the playing track's tail
    x = harness.others(harness.entries[0], u)[-2]
    assert harness.bridge.play_next(x.path, now=True)
    assert harness.queue_paths() == [u.path]  # U was never dropped
    assert harness.play_next_track() == x.path
    assert harness.next_track_path() == u.path
    assert harness.play_next_track() == u.path
    assert harness.queue_paths() == []


def test_play_next_lands_right_after_the_upcoming_track(harness: Harness) -> None:
    upcoming = harness.upcoming()
    x = harness.others(harness.entries[0], upcoming)[-1]
    assert harness.bridge.play_next(x.path)
    assert harness.next_track_path() == upcoming.path  # Up Next is still true
    # play_next() only refreshes the *ready* render, which the worker may
    # still be re-rendering (with X now baked in as its next) in the
    # background.  Without waiting for that fresh render, the bus can take
    # the *fallback* -- the old render for `upcoming`, whose own next is
    # still whatever was picked before X was queued -- which is valid
    # (RenderAhead's documented "one track later" behaviour) but makes the
    # assertions below flaky under load.  Wait for the fresh render instead
    # of racing it.
    assert _next_path(harness.settle()) == x.path
    assert harness.play_next_track() == upcoming.path
    assert harness.next_track_path() == x.path
    assert harness.play_next_track() == x.path
    assert harness.player._state.queued_next is None  # committed when it started


def test_up_next_matches_what_plays(harness: Harness) -> None:
    y = harness.others(harness.entries[0], harness.upcoming())[-1]
    for step in range(4):
        if step == 1:
            assert harness.bridge.queue_add(y.path)
        expected = harness.next_track_path()
        assert expected is not None
        assert harness.play_next_track() == expected


def test_queued_item_stays_visible_until_it_starts(harness: Harness) -> None:
    upcoming = harness.upcoming()
    a = harness.others(harness.entries[0], upcoming)[-1]
    assert harness.bridge.queue_add(a.path)
    ready = harness.settle()
    assert ready.entry.path == upcoming.path
    assert _next_path(ready) == a.path
    assert ready.next_from_queue is True
    assert harness.queue_paths() == [a.path]  # peeked, not taken
    assert harness.play_next_track() == upcoming.path
    assert harness.queue_paths() == [a.path]  # still waiting: it plays next
    assert harness.play_next_track() == a.path
    assert harness.queue_at_start[-1] == []  # gone the moment it started


def test_adding_behind_the_peeked_head_does_not_rerender(harness: Harness) -> None:
    upcoming = harness.upcoming()
    a, b = harness.others(harness.entries[0], upcoming)[-2:]
    assert harness.bridge.queue_add(a.path)
    ready = harness.settle()
    assert harness.bridge.queue_add(b.path)
    assert harness.worker._ready is ready  # the head it peeked is unchanged
    assert [harness.play_next_track() for _ in range(3)] == [upcoming.path, a.path, b.path]


def test_removing_the_peeked_head_rerenders_and_it_never_plays(harness: Harness) -> None:
    upcoming = harness.upcoming()
    a, b = harness.others(harness.entries[0], upcoming)[-2:]
    harness.bridge.queue_add(a.path)
    harness.bridge.queue_add(b.path)
    assert _next_path(harness.settle()) == a.path
    assert harness.bridge.queue_remove(a.path)
    assert _next_path(harness.settle()) == b.path
    played = [harness.play_next_track() for _ in range(3)]
    assert played[:2] == [upcoming.path, b.path]
    assert a.path not in played


def test_removing_the_track_already_mixed_in_still_plays_it(harness: Harness) -> None:
    """The upcoming track is already on its way: its head is in the playing tail."""
    u = harness.others(harness.entries[0], harness.upcoming())[-1]
    _queue_behind_upcoming(harness, u)
    playing = harness.player._playing_render
    assert playing is not None
    b = harness.others(harness.entries[0], u, playing.entry)[-1]
    harness.bridge.queue_add(b.path)
    assert harness.queue_paths() == [u.path, b.path]
    ready = harness.settle()
    assert harness.bridge.queue_remove(u.path)
    assert harness.worker._ready is ready  # nothing it peeked changed
    assert harness.play_next_track() == u.path
    assert harness.play_next_track() == b.path
    assert harness.queue_paths() == []


def test_reorder_changes_the_next_pick(harness: Harness) -> None:
    upcoming = harness.upcoming()
    a, b = harness.others(harness.entries[0], upcoming)[-2:]
    harness.bridge.queue_add(a.path)
    harness.bridge.queue_add(b.path)
    assert _next_path(harness.settle()) == a.path
    assert harness.bridge.queue_reorder([b.path, a.path])
    assert _next_path(harness.settle()) == b.path
    assert [harness.play_next_track() for _ in range(3)] == [upcoming.path, b.path, a.path]


def test_queue_edit_near_the_end_of_a_track_never_causes_dead_air(harness: Harness) -> None:
    upcoming = harness.upcoming()
    y = harness.others(harness.entries[0], upcoming)[-1]
    old = harness.settle()
    harness.gate.clear()  # the replacement render will not finish in time
    assert harness.bridge.queue_add(y.path)
    assert harness.worker._fallback is old
    # The playing track ends now: the bus gets the old render, not silence.
    with harness.player._state.queue_lock:
        taken = harness.player._take_render()
    assert taken is old
    harness.gate.set()
    # The edit takes effect one track later: Y follows the old render's next.
    after = harness.settle()
    assert after.entry.path == _next_path(old)
    assert _next_path(after) == y.path


def test_duplicate_queue_entries_are_removed_one_at_a_time(harness: Harness) -> None:
    upcoming = harness.upcoming()
    a = harness.others(harness.entries[0], upcoming)[-1]
    harness.bridge.queue_add(a.path)
    harness.bridge.queue_add(a.path)
    assert _next_path(harness.settle()) == a.path
    assert harness.play_next_track() == upcoming.path
    assert harness.queue_paths() == [a.path, a.path]
    assert harness.play_next_track() == a.path
    assert harness.queue_at_start[-1] == [a.path]
    assert harness.play_next_track() == a.path
    assert harness.queue_at_start[-1] == []


def test_reseed_random_plays_the_new_seed_next(harness: Harness, monkeypatch) -> None:
    x = harness.others(harness.entries[0], harness.upcoming())[-1]
    monkeypatch.setattr("random.choice", lambda _entries: x)
    assert harness.bridge.reseed_random()
    assert harness.play_next_track() == x.path
    assert harness.player._last_pick_mode == "seed"
