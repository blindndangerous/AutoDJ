"""Track-count liners on the station renderer's real handoff planner path."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj.liner_scheduler import LinerScheduler
from autodj.player import Player
from autodj.stereo import SAMPLE_RATE as SR
from tests.unit._break_station import make_station
from tests.unit._fakes import make_entry


def test_every_three_station_tracks_commits_liner_after_tracks_three_and_six(
    tmp_path: Path,
) -> None:
    liner = SimpleNamespace(path=str(tmp_path / "liner.wav"), length=2.0)
    scheduler = LinerScheduler(
        SimpleNamespace(
            transition_mode="saved_points",
            liners_talk_up=False,
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
        registered_liners=lambda: [("liner-id", Path(liner.path))],
    )

    player = Player.__new__(Player)
    player._clock_lock = threading.RLock()
    player._clock_revision = 0
    player._cfg = SimpleNamespace(playback=SimpleNamespace(pick_mode="similarity"))
    player._state = SimpleNamespace(queue_lock=threading.Lock(), queued_next=None, queue=[])
    player._pending_reserved_liners = ()
    player._pending_clock_cursor = None
    player._pending_clock_boundary = None
    player._liner_entries_snapshot = None
    player._liner_paths_lock = threading.Lock()
    player._liner_paths = frozenset()
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


@pytest.mark.parametrize(
    ("overlap_start", "allow_outro", "expected_start", "expected_entry"),
    [(0, True, 9, 10), (0, False, 10, 11), (2, True, 8, 10)],
    ids=["over-outro-default-overlap", "segue-start-without-outro", "explicit-overlap"],
)
def test_automatic_liners_use_saved_cues_without_talk_up(
    tmp_path: Path,
    overlap_start: float,
    allow_outro: bool,
    expected_start: float,
    expected_entry: float,
) -> None:
    """ADR-0004-07: automatic saved-point liners share queued-liner cue timing."""
    player, (outgoing, incoming, _c, _d), points, _desk = make_station(tmp_path)
    liner = make_entry(4)
    liner.path = str(tmp_path / "liner.wav")
    liner.length = 6
    (tmp_path / "liner.wav").touch()
    points[liner.path] = {
        "start": 1,
        "segue": 4,
        "end": 5,
        "overlap_start": overlap_start,
    }
    liner_audio = np.zeros((6 * SR, 2), dtype=np.float32)
    liner_audio[:, 1] = 0.5
    load_music = player._load_playout_audio
    player._load_playout_audio = lambda entry: (
        liner_audio.copy() if entry.path == liner.path else load_music(entry)
    )
    library = player._library
    record_for_path = library.catalog.record_for_path
    library.catalog.record_for_path = lambda path: (
        {"id": "liner-id", "kind": "liner", "allow_outro": allow_outro}
        if path == liner.path
        else record_for_path(path)
    )
    library.record_for_id = lambda _identifier: {"path": liner.path}
    library.entry_for_path = lambda path: liner if path == liner.path else None
    player._cfg.playback.liners_enabled = True
    player._cfg.playback.liners_every_n_songs = 1
    player._cfg.playback.liners_talk_up = False
    scheduler = LinerScheduler(
        player._cfg.playback,
        tmp_path / "liners",
        None,
        registered_liners=lambda: [("liner-id", Path(liner.path))],
    )
    player.liner_scheduler = scheduler

    automatic, key = player._auto_liner(outgoing, incoming)
    assert automatic == ((liner, False),)
    assert key
    rendered = player._render_track(outgoing, incoming, 0, liners=automatic)
    queued = player._render_track(outgoing, incoming, 0, liners=((liner, True),))

    assert rendered is not None and queued is not None
    np.testing.assert_array_equal(rendered.audio, queued.audio)
    ((event_frame, event_entry, queued_flag),) = rendered.liner_events
    start = (event_frame + rendered.start_offset) / SR
    entry = (len(rendered.audio) - rendered.overlap_frames + rendered.start_offset) / SR
    assert event_entry is liner and not queued_flag
    assert start == pytest.approx(expected_start, abs=1e-3)
    assert entry == pytest.approx(expected_entry, abs=1e-3)
    tail_frame = round((start + 3.5) * SR) - rendered.start_offset
    assert rendered.audio[tail_frame, 1] == pytest.approx(0.5)
