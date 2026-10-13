"""Where a listener request could join the queue, as the bridge projects it (#49)."""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import numpy as np
import pytest

from autodj._bridge import QueueChanged
from autodj.config import DmcaConfig
from autodj.indexer import IndexEntry
from autodj.repeat_rules import PlayRecord, RepeatRules, TrackKeyCache
from autodj.similarity import SimilarityIndex

from ._helpers import _make_entry


def _song(n: int, artist: str | None = None) -> IndexEntry:
    return replace(_make_entry(n), artist=artist or f"Artist {n}", album=f"Album {n}")


def _ready(bridge) -> None:
    bridge.player._track_keys.return_value = TrackKeyCache()
    state = bridge.player._state
    state.current_track = _song(0)
    state.queue.extend([_song(1), _song(2)])
    bridge.sim.entries = [_song(n) for n in range(8)]


def test_each_place_has_its_start_neighbours_and_problems(bridge) -> None:
    _ready(bridge)
    state = bridge.player._state
    state.set_repeat_rules(
        RepeatRules(track_window=10, artist_window=2, album_window=0, title_window=0)
    )
    slots = bridge.request_slots(_song(5, artist="Artist 1"), horizon_s=3600)
    assert [slot.position for slot in slots] == [0, 1, 2]
    assert [slot.before.title for slot in slots] == ["Song 0", "Song 1", "Song 2"]
    assert [slot.after.title if slot.after else None for slot in slots] == [
        "Song 1",
        "Song 2",
        None,
    ]
    # Song 1's artist plays at position 1, so the artist rule holds it near there.
    assert slots[1].problems == ("same artist",)
    assert slots[2].problems == ("same artist",)
    assert slots[0].problems == ()
    assert slots[0].starts_in <= slots[1].starts_in <= slots[2].starts_in
    assert slots[1].starts_in - slots[0].starts_in == 180.0


def test_places_beyond_the_window_are_left_out_but_the_first_always_counts(bridge) -> None:
    _ready(bridge)
    assert [slot.position for slot in bridge.request_slots(_song(5), horizon_s=0)] == [0]


def test_a_song_just_played_or_coming_up_breaks_the_track_rule(bridge) -> None:
    _ready(bridge)
    slots = bridge.request_slots(_song(2), horizon_s=3600)
    assert all("same song" in slot.problems for slot in slots[2:])
    playing = bridge.request_slots(_song(0), horizon_s=3600)
    assert "same song" in playing[0].problems


def test_dmca_limits_count_at_each_place(bridge) -> None:
    _ready(bridge)
    bridge.player._cfg.dmca = DmcaConfig(enabled=True)
    state = bridge.player._state
    now = time.time()
    album = [replace(_make_entry(n), artist=f"A{n}") for n in (5, 6, 7)]
    state.play_history.extend(
        PlayRecord.of(entry, now - 600 + 100 * i) for i, entry in enumerate(album)
    )
    state.queue.clear()
    state.current_track = replace(_make_entry(4), artist="A4")
    slots = bridge.request_slots(replace(_make_entry(3), artist="A3"), horizon_s=3600)
    assert any("Album" in problem for problem in slots[0].problems)


def test_coming_up_and_queue_insert(bridge) -> None:
    _ready(bridge)
    state = bridge.player._state
    state.queued_next = _song(3)
    assert [e.title for e in bridge.coming_up()] == ["Song 0", "Song 3", "Song 1", "Song 2"]
    assert bridge.queue_insert(_song(4).path, 1)
    assert [e.title for e in state.queue] == ["Song 1", "Song 4", "Song 2"]
    assert bridge.queue_insert(_song(5).path, 99)
    assert state.queue[-1].title == "Song 5"
    assert not bridge.queue_insert("Z:/Music/missing.flac", 0)
    state.current_track = None
    state.queued_next = None
    assert bridge.coming_up()[0].title == "Song 1"


def test_similarity_is_the_cosine_of_two_embeddings() -> None:
    entries = [_song(n) for n in range(3)]
    vectors = np.array([[1.0, 0.0], [0.6, 0.8], [0.0, 1.0]], dtype=np.float32)
    index = SimilarityIndex.empty()
    index.replace_catalog_index(entries, vectors)
    assert index.similarity(entries[0].path, entries[1].path) == np.float32(0.6)
    assert index.similarity(entries[0].path, entries[2].path) == 0.0
    assert index.similarity(entries[0].path, "missing") is None


def _lock_is_free(lock) -> bool:
    """Whether another thread could take *lock* now."""
    got: list[bool] = []

    def take() -> None:
        taken = lock.acquire(blocking=False)
        got.append(taken)
        if taken:
            lock.release()

    other = threading.Thread(target=take)
    other.start()
    other.join()
    return got == [True]


def test_insert_request_places_outside_the_queue_lock_and_inserts_checked(bridge) -> None:
    _ready(bridge)
    state = bridge.player._state
    seen = []

    def choose(slots):
        # The projection and the choice run with the queue's lock free, so
        # the mix bus never waits on them.
        assert _lock_is_free(state.queue_lock)
        seen.append([slot.position for slot in slots])
        return slots[1]

    chosen = bridge.insert_request(_song(5), 3600, choose)
    assert chosen.position == 1
    assert [e.title for e in state.queue] == ["Song 1", "Song 5", "Song 2"]
    assert seen == [[0, 1, 2]]
    assert bridge.insert_request(_song(6), 3600, lambda slots: None) is None
    assert len(state.queue) == 3


def test_a_queue_that_changed_meanwhile_is_placed_again(bridge) -> None:
    _ready(bridge)
    state = bridge.player._state
    calls = []

    def choose(slots):
        calls.append([slot.after.title if slot.after else None for slot in slots])
        if len(calls) == 1:
            # A track started meanwhile: Song 1 left the queue.
            with state.queue_lock:
                state.queue.pop(0)
        return slots[0]

    bridge.insert_request(_song(5), 3600, choose)
    # Placed again against the queue as it now is, then inserted there.
    assert calls == [["Song 1", "Song 2", None], ["Song 2", None]]
    assert [e.title for e in state.queue] == ["Song 5", "Song 2"]


def test_a_queue_that_keeps_changing_is_reported(bridge) -> None:
    _ready(bridge)
    state = bridge.player._state

    def choose(slots):
        with state.queue_lock:
            state.queue.append(_song(7))
        return slots[0]

    with pytest.raises(QueueChanged):
        bridge.insert_request(_song(5), 3600, choose)
    assert _song(5).path not in [e.path for e in state.queue]


def test_dmca_problems_are_kept_apart(bridge) -> None:
    _ready(bridge)
    bridge.player._cfg.dmca = DmcaConfig(enabled=True)
    state = bridge.player._state
    state.queue.clear()
    state.current_track = replace(_make_entry(4), artist="A4")
    state.queued_next = replace(_make_entry(5), artist="A5")
    (first,) = bridge.request_slots(replace(_make_entry(3), artist="A3"), horizon_s=0)
    assert first.dmca and set(first.dmca) <= set(first.problems)
    assert first.before.title == "Song 5"  # after the "play next" song
