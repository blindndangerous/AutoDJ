"""Queue edits in server-audio mode reach the mix bus in the right order.

Uses a real Player, PlayerBridge, render-ahead worker and MixBus; only the
audio render is faked (short constant blocks) and the bus clock is fake.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj._bridge import PlayerBridge
from autodj.indexer import IndexEntry
from autodj.mixbus import BusEvents, MixBus, RenderedTrack
from autodj.player import Player
from tests.unit.test_mixbus import FakeClock
from tests.unit.test_player import _make_cfg_mock, _make_sim_index

WAIT = 2.0
FRAMES = MixBus.BLOCK * 20  # long enough that a 150 ms skip fade is audible


@dataclass
class Harness:
    player: Player
    bridge: PlayerBridge
    bus: MixBus
    entries: list[IndexEntry]

    def play_next_track(self) -> str:
        """Render blocks until the bus starts another track; return its path."""
        before = self.player._playing_render
        for _ in range(400):
            if self.bus._current is None:
                assert self.player._render_ahead.wait_ready(WAIT)
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

    def settle(self) -> RenderedTrack:
        """Wait for the worker's ready render and return it (not popped)."""
        assert self.player._render_ahead.wait_ready(WAIT)
        ready = self.player._render_ahead._ready
        assert ready is not None
        return ready

    def others(self, *exclude: IndexEntry) -> list[IndexEntry]:
        skip = {e.path for e in exclude}
        return [e for e in self.entries if e.path not in skip]


def _next_path(track: RenderedTrack) -> str | None:
    return track.next_entry.path if track.next_entry is not None else None


def _fake_render(current, nxt, offset):
    return RenderedTrack(
        current, np.full((FRAMES, 2), 0.1, np.float32), nxt, 0, "", start_offset=offset
    )


@pytest.fixture
def harness():
    sim = _make_sim_index(12)
    player = Player(_make_cfg_mock(), sim)
    player._render_track = _fake_render  # type: ignore[method-assign]
    player.load_lyrics_in_background = MagicMock()  # type: ignore[method-assign]
    bus = MixBus(
        BusEvents(player._on_track_start, player._on_position, player._render_ahead.pop),
        eq_gains=lambda: (1.0, 1.0, 1.0),
        clock=FakeClock(),
    )
    player.bus = bus
    seed = sim.entries[0]
    player._record_seed(seed)
    player.reset_render_ahead(seed, 0, pick_mode="seed")
    player._render_ahead.start()
    bus.start_set()
    h = Harness(player, PlayerBridge(player=player, sim=sim), bus, list(sim.entries))
    assert h.play_next_track() == seed.path
    h.settle()
    yield h
    player._render_ahead.stop(timeout=WAIT)


def test_play_now_plays_the_chosen_track_next(harness: Harness) -> None:
    x = harness.others(harness.entries[0], harness.upcoming())[-1]
    assert harness.bridge.play_next(x.path, now=True)
    assert harness.next_track_path() == x.path
    assert harness.play_next_track() == x.path
    assert harness.player._last_pick_mode == "queue"
    assert harness.player._state.queued_next is None


def test_play_next_lands_right_after_the_upcoming_track(harness: Harness) -> None:
    upcoming = harness.upcoming()
    x = harness.others(harness.entries[0], upcoming)[-1]
    assert harness.bridge.play_next(x.path)
    assert harness.next_track_path() == upcoming.path  # Up Next is still true
    assert harness.play_next_track() == upcoming.path
    assert harness.next_track_path() == x.path
    assert harness.play_next_track() == x.path


def test_up_next_matches_what_plays(harness: Harness) -> None:
    y = harness.others(harness.entries[0], harness.upcoming())[-1]
    for step in range(4):
        if step == 1:
            assert harness.bridge.queue_add(y.path)
        expected = harness.next_track_path()
        assert expected is not None
        assert harness.play_next_track() == expected


def test_queue_add_during_a_track_rerenders_the_ready_track(harness: Harness) -> None:
    upcoming = harness.upcoming()
    y = harness.others(harness.entries[0], upcoming)[-1]
    assert harness.bridge.queue_add(y.path)
    ready = harness.settle()
    assert ready.entry.path == upcoming.path
    assert ready.next_entry is not None and ready.next_entry.path == y.path
    assert ready.next_from_queue is True
    assert harness.play_next_track() == upcoming.path
    assert harness.play_next_track() == y.path


def test_discarded_queue_pick_goes_back_to_the_queue_front(harness: Harness) -> None:
    upcoming = harness.upcoming()
    a, b = harness.others(harness.entries[0], upcoming)[-2:]
    assert harness.bridge.queue_add(a.path)
    assert _next_path(harness.settle()) == a.path  # the ready render consumed A
    assert harness.player._state.queue == []
    assert harness.bridge.queue_add(b.path)
    # Discarding that render put A back in front of B; the re-render took A again.
    assert _next_path(harness.settle()) == a.path
    assert [e.path for e in harness.player._state.queue] == [b.path]
    assert [harness.play_next_track() for _ in range(3)] == [upcoming.path, a.path, b.path]


def test_queue_remove_reaches_a_pick_the_ready_render_consumed(harness: Harness) -> None:
    upcoming = harness.upcoming()
    a, b = harness.others(harness.entries[0], upcoming)[-2:]
    harness.bridge.queue_add(a.path)
    harness.bridge.queue_add(b.path)
    assert _next_path(harness.settle()) == a.path
    assert harness.bridge.queue_remove(a.path)
    assert _next_path(harness.settle()) == b.path
    assert [harness.play_next_track() for _ in range(2)] == [upcoming.path, b.path]


def test_queue_reorder_reaches_a_pick_the_ready_render_consumed(harness: Harness) -> None:
    upcoming = harness.upcoming()
    a, b = harness.others(harness.entries[0], upcoming)[-2:]
    harness.bridge.queue_add(a.path)
    harness.bridge.queue_add(b.path)
    assert _next_path(harness.settle()) == a.path
    assert harness.bridge.queue_reorder([b.path, a.path])
    assert _next_path(harness.settle()) == b.path
    assert [harness.play_next_track() for _ in range(3)] == [upcoming.path, b.path, a.path]


def test_reseed_random_plays_the_new_seed_next(harness: Harness, monkeypatch) -> None:
    x = harness.others(harness.entries[0], harness.upcoming())[-1]
    monkeypatch.setattr("random.choice", lambda _entries: x)
    assert harness.bridge.reseed_random()
    assert harness.play_next_track() == x.path
    assert harness.player._last_pick_mode == "seed"
