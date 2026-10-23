"""Queue edits in server-audio mode reach the mix bus in the right order.

Uses a real Player, PlayerBridge, live playout worker and MixBus; progressive
decks and the bus clock are faked. Tracks are compared by path: the index
hands out frozen copies of entries.

Queue picks are peeked when a render is built and removed from the queue
only when the track starts, so the queue the listener sees stays true.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from autodj._bridge import PlayerBridge
from autodj.indexer import IndexEntry
from autodj.live_audio import AudioLayer, LiveAudio
from autodj.live_queue import VoiceQueueConflict
from autodj.mixbus import BusEvents, MixBus, RenderedTrack
from autodj.player import Player
from autodj.stereo import SAMPLE_RATE
from tests.unit._fakes import FakeClock
from tests.unit._fakes import make_cfg_mock as _make_cfg_mock
from tests.unit._fakes import make_sim_index as _make_sim_index
from tests.unit.test_live_playout import _UnconsumedDeck

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
        return self.player._playout

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


def _wait_for_handoff(h: Harness, expected: IndexEntry) -> RenderedTrack:
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        track = h.player._playing_render
        if (
            not h.player._live_queue_dirty
            and track is not None
            and track.next_entry is not None
            and track.next_entry.path == expected.path
        ):
            return track
        threading.Event().wait(0.005)
    raise AssertionError(f"live handoff did not select {expected.path}")


@pytest.fixture
def harness():
    sim = _make_sim_index(12)
    player = Player(_make_cfg_mock(), sim)
    decks = {
        entry.path: _UnconsumedDeck(round(entry.length * SAMPLE_RATE), i / 12)
        for i, entry in enumerate(sim.entries)
    }
    player._live_file = lambda entry, **_kwargs: decks[entry.path]
    gate = threading.Event()
    gate.set()

    def fake_render(current, nxt, offset, **_kw):
        assert gate.wait(WAIT)
        sources = player._render_live_sources
        assert sources is not None and sources[0] is current
        source = sources[1]
        assert source is not None
        audio = LiveAudio(FRAMES, (AudioLayer(source, 0, FRAMES, offset),))
        return RenderedTrack(
            current,
            audio,
            nxt,
            0,
            "",
            start_offset=offset,
            live_source=source,
            next_live_source=sources[3],
            full_audio=audio,
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
    player.reset_playout(seed, 0, pick_mode="seed")
    h = Harness(player, PlayerBridge(player=player, sim=sim), bus, list(sim.entries), gate)
    player.on_track_started = lambda _entry: h.queue_at_start.append(h.queue_paths())
    player._playout.start()
    bus.start_set()
    assert h.play_next_track() == seed.path
    h.settle()
    yield h
    gate.set()
    player._playout.stop(timeout=WAIT)


def _queue_behind_upcoming(h: Harness, entry: IndexEntry) -> None:
    """Queue *entry* and let it replace the current unheard incoming track."""
    assert h.bridge.queue_add(entry.path)
    _wait_for_handoff(h, entry)


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
        with h.player._playout._cond:
            assert h.player._playout._ready is track
            h.player._playout._ready = decorated
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
    current = harness.player._playing_render
    assert current is not None
    x = harness.others(current.entry, harness.upcoming())[-1]
    assert harness.bridge.play_next(x.path)
    _wait_for_handoff(harness, x)
    assert harness.next_track_path() == x.path
    assert harness.play_next_track() == x.path
    assert harness.player._state.queued_next is None  # committed when it started


def test_up_next_matches_what_plays(harness: Harness) -> None:
    y = harness.others(harness.entries[0], harness.upcoming())[-1]
    for step in range(4):
        if step == 1:
            assert harness.bridge.queue_add(y.path)
            _wait_for_handoff(harness, y)
        expected = harness.next_track_path()
        assert expected is not None
        assert harness.play_next_track() == expected


def test_queued_item_stays_visible_until_it_starts(harness: Harness) -> None:
    current = harness.player._playing_render
    assert current is not None
    a = harness.others(current.entry, harness.upcoming())[-1]
    assert harness.bridge.queue_add(a.path)
    _wait_for_handoff(harness, a)
    assert harness.queue_paths() == [a.path]  # peeked, not taken
    assert harness.play_next_track() == a.path
    assert harness.queue_at_start[-1] == []  # gone the moment it started


def test_adding_behind_the_peeked_head_does_not_rerender(harness: Harness) -> None:
    current = harness.player._playing_render
    assert current is not None
    a, b = harness.others(current.entry, harness.upcoming())[-2:]
    assert harness.bridge.queue_add(a.path)
    _wait_for_handoff(harness, a)
    assert harness.bridge.queue_add(b.path)
    _wait_for_handoff(harness, a)
    assert harness.play_next_track() == a.path
    _wait_for_handoff(harness, b)
    assert harness.play_next_track() == b.path


def test_removing_the_peeked_head_rerenders_and_it_never_plays(harness: Harness) -> None:
    current = harness.player._playing_render
    assert current is not None
    a, b = harness.others(current.entry, harness.upcoming())[-2:]
    harness.bridge.queue_add(a.path)
    harness.bridge.queue_add(b.path)
    _wait_for_handoff(harness, a)
    assert harness.bridge.queue_remove(a.path)
    _wait_for_handoff(harness, b)
    played = [harness.play_next_track()]
    assert played == [b.path]
    assert a.path not in played


def test_removing_the_track_already_mixed_in_still_plays_it(harness: Harness) -> None:
    """The started incoming track stays; edits select its successor."""
    u = harness.others(harness.entries[0], harness.upcoming())[-1]
    _queue_behind_upcoming(harness, u)
    assert harness.play_next_track() == u.path
    playing = harness.player._playing_render
    assert playing is not None
    b = harness.others(harness.entries[0], u, playing.next_entry)[-1]
    harness.bridge.queue_add(b.path)
    _wait_for_handoff(harness, b)
    assert harness.queue_paths() == [b.path]
    assert harness.play_next_track() == b.path
    assert harness.queue_paths() == []


def test_reorder_changes_the_next_pick(harness: Harness) -> None:
    current = harness.player._playing_render
    assert current is not None
    a, b = harness.others(current.entry, harness.upcoming())[-2:]
    harness.bridge.queue_add(a.path)
    _wait_for_handoff(harness, a)
    harness.bridge.queue_add(b.path)
    assert harness.bridge.queue_reorder([b.path, a.path])
    _wait_for_handoff(harness, b)
    assert [harness.play_next_track() for _ in range(2)] == [b.path, a.path]


def test_queue_edit_holds_the_boundary_until_its_replacement_is_ready(harness: Harness) -> None:
    """A displaced live handoff waits at its boundary instead of airing stale music."""
    current = harness.bus.current_render()
    assert current is not None
    y = harness.others(current.entry, current.next_entry)[-1]
    # Give this fake deck an overlap so the held boundary falls before EOF.
    held_track = replace(current, overlap_frames=MixBus.BLOCK * 2)
    harness.bus._current = held_track
    harness.player._playing_render = held_track
    current = held_track
    boundary = len(current.audio) - current.overlap_frames
    harness.bus._pos = boundary - MixBus.BLOCK
    harness.gate.clear()  # the replacement render will not finish in time
    assert harness.bridge.queue_add(y.path)
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline and not (
        harness.bus._handoff_hold is not None and harness.bus._handoff_hold[0] is current
    ):
        threading.Event().wait(0.005)
    assert harness.bus._handoff_hold is not None
    assert harness.bus._handoff_hold[0] is current
    harness.bus.render_block()
    assert harness.bus.live_position(current) == boundary
    assert harness.player._take_render() is None
    harness.gate.set()
    _wait_for_handoff(harness, y)
    assert harness.bus.live_position(harness.player._playing_render) == boundary


def test_duplicate_queue_entries_are_removed_one_at_a_time(harness: Harness) -> None:
    current = harness.player._playing_render
    assert current is not None
    a = harness.others(current.entry, harness.upcoming())[-1]
    harness.bridge.queue_add(a.path)
    _wait_for_handoff(harness, a)
    harness.bridge.queue_add(a.path)
    _wait_for_handoff(harness, a)
    assert harness.queue_paths() == [a.path, a.path]
    assert harness.play_next_track() == a.path
    assert harness.queue_paths() == [a.path]
    _wait_for_handoff(harness, a)
    assert harness.play_next_track() == a.path
    assert harness.queue_at_start[-1] == []


def test_reseed_random_plays_the_new_seed_next(harness: Harness, monkeypatch) -> None:
    x = harness.others(harness.entries[0], harness.upcoming())[-1]
    monkeypatch.setattr("random.choice", lambda _entries: x)
    assert harness.bridge.reseed_random()
    assert harness.play_next_track() == x.path
    assert harness.player._last_pick_mode == "seed"
