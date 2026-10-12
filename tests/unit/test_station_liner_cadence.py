"""Track-count liners on the station renderer's real handoff planner path."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from autodj.liner_scheduler import LinerScheduler
from autodj.player import Player


def test_every_three_station_tracks_commits_liner_after_tracks_three_and_six(
    tmp_path: Path,
) -> None:
    liner = SimpleNamespace(path=str(tmp_path / "liner.wav"), length=2.0)
    scheduler = LinerScheduler(
        SimpleNamespace(
            transition_mode="saved_points",
            liners_talk_up=True,
            liners_enabled=True,
            liners_every_n_songs=3,
            liners_every_minutes=None,
            liners_random_min_minutes=None,
            liners_random_max_minutes=None,
            liners_pick_mode="sequential",
            liners_duck_db=-10.0,
        ),
        tmp_path / "liners",
        MagicMock(),
        vocal_start=lambda _entry: 1.0,
        registered_liners=lambda: [("liner-id", Path(liner.path))],
    )

    player = Player.__new__(Player)
    player._cfg = SimpleNamespace(playback=SimpleNamespace(pick_mode="similarity"))
    player._state = SimpleNamespace(queue_lock=threading.Lock(), queued_next=None, queue=[])
    player._pending_reserved_liners = ()
    player._pending_clock_cursor = None
    player._pending_clock_boundary = None
    player.liner_scheduler = scheduler
    player.saved_points = lambda _entry: {}
    player._choose_by_mode = lambda _current, _context: (upcoming, "similarity")
    player._library = SimpleNamespace(
        entries_snapshot=lambda _kind="music": (),
        record_for_id=lambda _identifier: {"path": liner.path},
        entry_for_path=lambda _path: liner,
    )
    context = SimpleNamespace(queue_reserved=None, complement=None, closed=frozenset())

    committed_after: list[int] = []
    for song_number in range(1, 8):
        current = SimpleNamespace(path=f"/music/{song_number}.mp3", length=120.0)
        upcoming = SimpleNamespace(path=f"/music/{song_number + 1}.mp3", length=120.0)
        _entry, _mode, liners, _cursor, key = player._choose_station_next(current, context)

        # The upcoming render starts after its handoff plan was prepared.
        scheduler.on_track_start()
        if liners:
            assert scheduler.mark_planned_fired(key)
            committed_after.append(song_number)

    assert committed_after == [3, 6]
