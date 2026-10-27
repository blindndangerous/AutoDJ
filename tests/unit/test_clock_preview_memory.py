"""Clock preview memory follows occurrences and keeps temporarily omitted steps (#604)."""

from dataclasses import replace
from threading import RLock
from types import SimpleNamespace

import pytest

from autodj.hour_preview import MAX_PREDICTED, Decided, _ClockChoices


def _source(*, boundary=1000):
    liner = SimpleNamespace(path="/liners/drop.wav")
    current = SimpleNamespace(path="/music/current.flac")
    player = SimpleNamespace(
        _cfg=SimpleNamespace(playback=SimpleNamespace(pick_mode="clock")),
        _clock_steps=[{"type": "music", "folder": f"/music/{step}"} for step in range(3)],
        _clock_slots=[None] * 3,
        _clock_revision=1,
        _clock={"name": "Main", "cursor": 0, "resync_minutes": [] if boundary is None else [0]},
        _pending_clock_boundary=boundary,
        _clock_next_boundary=boundary,
        _state=SimpleNamespace(track_number=0, play_history=[]),
        _pick_lock=RLock(),
    )
    source = SimpleNamespace(
        player=player,
        library=SimpleNamespace(entries_snapshot=lambda kind: [liner] if kind == "liner" else []),
    )
    decided = Decided(
        now=100, last=current, clock_name="Main", clock_cursor=0, clock_boundary=boundary
    )
    return source, decided, liner


def _remember(memory, player, step, path, *, count=None):
    entry = SimpleNamespace(path=path)
    memory.remember(player, SimpleNamespace(entry=entry, step_index=step))
    position = memory.selected[0]
    if count is not None:
        memory.advance(player, entry, count)
    return position, entry


def test_dormant_choice_survives_multiple_rebuilds_at_the_same_anchor():
    source, decided, _liner = _source()
    memory = _ClockChoices.start(source, decided, None)
    position, entry = _remember(memory, source.player, 2, "/music/omitted.flac")
    memory.restart(2000)
    _remember(memory, source.player, 0, "/music/restarted.flac", count=1)

    for _ in range(3):
        memory.trim()
        memory = _ClockChoices.start(source, decided, memory)
        assert memory.paths[position] == entry.path
        assert memory.preferred(source.player, 2) == entry.path


def test_fixed_prefix_alignment_counts_music_and_preserves_the_following_choice():
    source, decided, liner = _source()
    memory = _ClockChoices.start(source, decided, None)
    consumed, first = _remember(memory, source.player, 0, "/music/first.flac", count=1)
    following, second = _remember(memory, source.player, 1, "/music/second.flac", count=2)
    fixed = replace(
        decided,
        last=first,
        clock_cursor=1,
        unplayed=((liner, 200), (first, 205)),
    )

    refreshed = _ClockChoices.start(source, fixed, memory)
    assert consumed not in refreshed.paths
    assert refreshed.paths[following] == second.path
    assert refreshed.preferred(source.player, 1) == second.path


def test_cycles_without_restarts_have_distinct_choices_and_follow_fixed_advancement():
    """ADR-0009-11."""
    source, decided, _liner = _source(boundary=None)
    memory = _ClockChoices.start(source, decided, None)
    first_lap, _ = _remember(memory, source.player, 0, "/music/lap-one.flac", count=1)
    _remember(memory, source.player, 1, "/music/middle.flac", count=2)
    _last_position, last = _remember(memory, source.player, 2, "/music/last.flac", count=3)
    assert memory.preferred(source.player, 0) is None
    second_lap, next_song = _remember(memory, source.player, 0, "/music/lap-two.flac", count=4)
    assert first_lap != second_lap

    source.player._state.track_number = 3
    source.player._state.play_history = [
        SimpleNamespace(path=path)
        for path in ("/music/lap-one.flac", "/music/middle.flac", last.path)
    ]
    fixed = replace(decided, last=last, clock_cursor=0)
    refreshed = _ClockChoices.start(source, fixed, memory)
    assert first_lap not in refreshed.paths
    assert refreshed.preferred(source.player, 0) == next_song.path


def test_matching_last_song_does_not_hide_a_different_played_sequence():
    source, decided, _liner = _source(boundary=None)
    memory = _ClockChoices.start(source, decided, None)
    _remember(memory, source.player, 0, "/music/first.flac", count=1)
    _position, last = _remember(memory, source.player, 1, "/music/last.flac", count=2)
    _remember(memory, source.player, 2, "/music/future.flac", count=3)
    source.player._state.track_number = 2
    source.player._state.play_history = [SimpleNamespace(path="/music/unexpected.flac"), last]
    fixed = replace(decided, last=last, clock_cursor=2)
    assert not _ClockChoices.start(source, fixed, memory).paths


@pytest.mark.parametrize("change", ["boundary", "edit", "switch", "unmatched-prefix"])
def test_obsolete_or_unmatched_occurrences_are_expired(change):
    source, decided, _liner = _source()
    memory = _ClockChoices.start(source, decided, None)
    _remember(memory, source.player, 0, "/music/remembered.flac", count=1)
    if change == "boundary":
        decided = replace(decided, now=1000)
    elif change == "edit":
        source.player._clock_steps[0] = {"type": "music", "folder": "/music/changed"}
    elif change == "switch":
        source.player._clock["name"] = "Other"
        decided = replace(decided, clock_name="Other")
    else:
        source.player._state.track_number = 1
        decided = replace(
            decided, last=SimpleNamespace(path="/music/unexpected.flac"), clock_cursor=1
        )

    assert not _ClockChoices.start(source, decided, memory).paths


def test_memory_is_bounded_and_keeps_realized_choices_before_dormant_attempts():
    source, decided, _liner = _source(boundary=None)
    memory = _ClockChoices.start(source, decided, None)
    realized, first = _remember(memory, source.player, 0, "/music/realized.flac", count=1)
    for lap in range(1, 2 * MAX_PREDICTED + 10):
        memory.paths[replace(realized, lap=lap)] = f"/music/dormant-{lap}.flac"
    newest = next(reversed(memory.paths))

    memory.trim()
    assert len(memory.paths) == 2 * MAX_PREDICTED
    assert memory.paths[realized] == first.path
    assert newest in memory.paths
