"""Picking inside a themed show: every pick mode, the relax order, DMCA and staleness."""

from __future__ import annotations

import logging
import threading
from collections import deque
from types import SimpleNamespace

import faiss
import numpy as np
import pytest

from autodj.clockwheel import ClockSlot
from autodj.config import DmcaConfig, PlaybackConfig
from autodj.indexer import FEATURE_DIM
from autodj.mixbus import RenderedTrack
from autodj.player import _THEME, Player, ShowTheme
from autodj.render_ahead import RenderAhead
from autodj.repeat_settings import RepeatSettings
from autodj.similarity import SimilarityIndex
from tests.unit._fakes import make_cfg_mock, make_entry

# (folder, artist, album, title, year).  The show is the 2000s: songs 3 to 6.
TRACKS = [
    ("Old", "Eighties Band", "Old One", "Eighties One", 1985),
    ("Old", "Nineties Band", "Old Two", "Nineties One", 1995),
    ("Old", "Nineties Duo", "Old Three", "Nineties Two", 1996),
    ("New", "Noughties A", "New One", "Two Thousand", 2000),
    ("New", "Noughties B", "New Two", "Two Thousand Three", 2003),
    ("New", "Noughties C", "New Three", "Two Thousand Five", 2005),
    ("New", "Noughties D", "New Four", "Two Thousand Nine", 2009),
]


def _sim(tracks) -> SimilarityIndex:
    rng = np.random.default_rng(5)
    vectors = rng.standard_normal((len(tracks), FEATURE_DIM)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    index = faiss.IndexFlatIP(FEATURE_DIM)
    index.add(vectors)
    entries = []
    for n, (folder, artist, album, title, year) in enumerate(tracks):
        entry = make_entry(n)
        entry.path = f"Z:/Music/{folder}/song_{n}.flac"
        entry.artist, entry.album, entry.title, entry.year = artist, album, title, year
        entries.append(entry)
    return SimilarityIndex(faiss_index=index, entries=entries)


def _player(pick_mode: str = "similarity", tracks=TRACKS, *, dmca: bool = False, rules=None):
    cfg = make_cfg_mock()
    cfg.playback = PlaybackConfig(
        pick_mode=pick_mode,
        repeat_rules=RepeatSettings.model_validate(
            rules or {"track": {"tracks": 1}, "artist": {"tracks": 0}, "album": {"tracks": 0}}
        ),
    )
    cfg.dmca = DmcaConfig(enabled=dmca)
    player = Player(cfg, _sim(tracks), dry_run=True)
    player._pure_shuffle = pick_mode == "random"
    return player


def _with_library(player: Player, liners=()) -> Player:
    entries = player._sim.entries_snapshot()
    player._library = SimpleNamespace(
        entries_snapshot=lambda kind="music": tuple(liners) if kind == "liner" else entries,
        last_played=lambda: {},
        pending_history=lambda: [],
        remember_setting=lambda *_args: None,
    )
    return player


def _show(player: Player, years=(2000, 2009), name="Remembering the 2000s") -> ShowTheme:
    paths = frozenset(
        entry.path for entry in player._sim.entries_snapshot() if years[0] <= entry.year <= years[1]
    )
    player.show = ShowTheme("r" * 32, name, paths)
    return player.show


def _played(player: Player, *numbers: int):
    entries = player._sim.entries_snapshot()
    with player._pick_lock:
        for number in numbers:
            player._state.record_played(entries[number])
    return entries[numbers[-1]]


# ---------------------------------------------------------------------------
# Every pick mode picks inside the show
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["similarity", "random", "least_recent"])
def test_every_pick_mode_plays_only_the_show(mode: str) -> None:
    player = _player(mode)
    if mode == "least_recent":
        _with_library(player)
    show = _show(player)
    current = _played(player, 0)
    for _ in range(12):
        picked, _how = player._choose_next(current, player._pick_context())
        assert picked.path in show.paths
    # The narrowing never outlives the pick.
    assert _THEME.get() is None
    assert len(player.playable_entries()) == len(TRACKS)


def test_discovery_plays_only_the_show() -> None:
    player = _player()
    show = _show(player)
    current = _played(player, 0)
    player._discovery_every, player._state.discovery_enabled = 1, True
    for track_number in range(1, 8):
        player._state.track_number = track_number
        picked, how = player._choose_next(current, player._pick_context())
        assert picked.path in show.paths
        assert how == "discovery"


def test_the_clockwheel_plays_only_the_show() -> None:
    player = _with_library(_player("clock"))
    player._clock = {"cursor": 0, "resync_minutes": [0]}
    player._clock_slots = [ClockSlot(folder="Z:/Music", selection="random", recursive=True)]
    player._clock_next_boundary = None
    show = _show(player)
    current = _played(player, 0)
    for _ in range(10):
        picked, mode, _liners, _cursor, _key = player._choose_station_next(
            current, player._pick_context()
        )
        assert (picked.path in show.paths, mode) == (True, "clock")


def test_without_a_show_nothing_is_narrowed() -> None:
    player = _player("random")
    current = _played(player, 3)
    titles = {player._choose_next(current, player._pick_context())[0].title for _ in range(80)}
    assert "Eighties One" in titles


def test_a_queued_song_plays_even_when_it_is_not_in_the_show() -> None:
    player = _player()
    _show(player)
    current = _played(player, 3)
    old = player._sim.entries_snapshot()[0]
    player._state.queue.append(old)
    picked, how = player._choose_next(current, player._pick_context())
    assert (picked, how) == (old, "queue")


# ---------------------------------------------------------------------------
# Relaxing keeps the theme; only nothing at all falls back
# ---------------------------------------------------------------------------


def test_the_repeat_rules_relax_inside_the_show_before_leaving_it(caplog) -> None:
    # Every show song is by one artist, and the artist rule holds it back.
    tracks = [*TRACKS[:3], *(("New", "One Artist", f"A{n}", f"Song {n}", 2004) for n in range(3))]
    for mode in ("similarity", "random"):
        player = _player(mode, tracks, rules={"track": {"tracks": 1}, "artist": {"tracks": 5}})
        show = _show(player)
        current = _played(player, 3)
        with caplog.at_level(logging.WARNING, logger="autodj.player"):
            picked, _how = player._choose_next(current, player._pick_context())
        assert picked.path in show.paths
        assert "nothing in the show fits" not in caplog.text


def test_the_track_window_relaxes_inside_the_show() -> None:
    player = _player("random", rules={"track": {"tracks": 6}})
    show = _show(player)
    # Every show song played recently; the last one is protected.
    current = _played(player, 3, 4, 5, 6)
    for _ in range(10):
        picked, _how = player._choose_next(current, player._pick_context())
        assert picked.path in show.paths - {current.path}


def test_when_nothing_in_the_show_can_play_a_normal_song_plays_and_it_is_logged(caplog) -> None:
    player = _player("random")
    show = _show(player, years=(2009, 2009))  # one song
    current = _played(player, 6)  # and it is playing
    with caplog.at_level(logging.WARNING, logger="autodj.player"):
        picked, _how = player._choose_next(current, player._pick_context())
    assert picked.path not in show.paths
    assert "Show Remembering the 2000s: nothing in the show fits" in caplog.text
    assert "playing a song from the whole library" in caplog.text


def test_dmca_limits_are_never_relaxed_for_the_show(caplog) -> None:
    # The show is one album; three of its songs just played, so the album
    # limit holds the fourth back.  A song outside the show plays instead.
    tracks = [
        *(("New", "Band", "Same Album", f"Song {n}", 2004) for n in range(4)),
        ("Old", "Other Band", "Other", "Safe", 1990),
    ]
    for mode in ("similarity", "random", "least_recent"):
        player = _player(mode, tracks, dmca=True)
        if mode == "least_recent":
            _with_library(player)
        show = _show(player)
        current = _played(player, 0, 1, 2)
        with caplog.at_level(logging.WARNING, logger="autodj.player"):
            picked, _how = player._choose_next(current, player._pick_context())
        assert picked.title == "Safe", mode
        assert picked.path not in show.paths
        assert "nothing in the show fits" in caplog.text


def test_dmca_limits_hold_in_the_clockwheel_show_too() -> None:
    tracks = [
        *(("New", "Band", "Same Album", f"Song {n}", 2004) for n in range(4)),
        ("Old", "Other Band", "Other", "Safe", 1990),
    ]
    player = _with_library(_player("clock", tracks, dmca=True))
    player._clock = {"cursor": 0, "resync_minutes": [0]}
    player._clock_slots = [ClockSlot(folder="Z:/Music/New", selection="random")]
    player._clock_next_boundary = None
    _show(player)
    current = _played(player, 0, 1, 2)
    picked, _mode, _liners, _cursor, _key = player._choose_station_next(
        current, player._pick_context()
    )
    assert picked.title == "Safe"


def test_a_refused_station_pick_gives_back_its_liner_reservation(monkeypatch) -> None:
    player = _player()
    _show(player)
    entries = player._sim.entries_snapshot()
    released = []
    player.liner_scheduler = SimpleNamespace(release_plan=released.append)
    results = iter(
        [
            (entries[0], "similarity", (), None, "plan-1"),  # not in the show
            (entries[1], "similarity", (), None, ""),
        ]
    )
    monkeypatch.setattr(player, "_choose_station_next_any", lambda *_args: next(results))
    picked = player._choose_station_next(entries[3], player._pick_context())
    assert picked[0] == entries[1]
    assert released == ["plan-1"]


def test_a_queue_pick_from_the_station_is_kept(monkeypatch) -> None:
    player = _player()
    _show(player)
    entries = player._sim.entries_snapshot()
    monkeypatch.setattr(
        player,
        "_choose_station_next_any",
        lambda *_args: (entries[0], "queue", (), None, ""),
    )
    assert player._choose_station_next(entries[3], player._pick_context())[1] == "queue"


# ---------------------------------------------------------------------------
# The first song of a set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["random", "least_recent", "clock"])
def test_a_set_starts_inside_the_show(mode: str) -> None:
    player = _with_library(_player(mode))
    if mode == "clock":
        player._clock = {"cursor": 0, "resync_minutes": [0]}
        player._clock_slots = [ClockSlot(folder="Z:/Music", selection="random", recursive=True)]
    show = _show(player)
    for _ in range(8):
        assert player._random_start_entry().path in show.paths


def test_a_set_may_start_with_a_clockwheel_liner_during_a_show() -> None:
    liner = make_entry(99)
    liner.path = "Z:/Liners/id.mp3"
    player = _with_library(_player("clock"), liners=[liner])
    player._clock = {"cursor": 0, "resync_minutes": [0]}
    player._clock_slots = [
        ClockSlot(folder="Z:/Liners", kind="liner"),
        ClockSlot(folder="Z:/Music", selection="random", recursive=True),
    ]
    _show(player)
    assert player._random_start_entry().path == liner.path


def test_a_set_start_with_dmca_keeps_the_show() -> None:
    player = _with_library(_player("random", dmca=True))
    show = _show(player)
    assert player._random_start_entry().path in show.paths


# ---------------------------------------------------------------------------
# Rendered songs from before a show changed are picked again
# ---------------------------------------------------------------------------


def _render(entry, next_entry, *, next_show: str = "", from_queue: bool = False):
    return RenderedTrack(
        entry=entry,
        audio=np.zeros((10, 2), dtype=np.float32),
        next_entry=next_entry,
        next_start_offset=0,
        transition_fx="none",
        next_from_queue=from_queue,
        next_show=next_show,
    )


def test_a_render_from_before_the_show_changed_is_stale() -> None:
    player = _player()
    entries = player._sim.entries_snapshot()
    render = _render(entries[0], entries[1])
    assert not player._queue_pick_is_stale(render)
    show = _show(player)
    assert player._queue_pick_is_stale(render)
    assert not player._queue_pick_is_stale(_render(entries[0], entries[3], next_show=show.run_id))
    player.show = None
    assert player._queue_pick_is_stale(_render(entries[0], entries[3], next_show=show.run_id))
    assert not player._queue_pick_is_stale(_render(entries[0], None, next_show=show.run_id))


def test_wait_for_render_waits_for_a_fresh_track_not_a_fallback() -> None:
    ready = threading.Event()
    rendered: list = []

    def render():
        ready.wait(2)
        track = _render(make_entry(0), None)
        rendered.append(track)
        return track

    worker = RenderAhead(render, lambda _t: False, lambda _t: None, lambda _t: None)
    assert not worker.wait_fresh(0.01)
    worker.start()
    ready.set()
    assert worker.wait_fresh(2)
    worker.stop()
    player = _player()
    player._render_ahead = worker
    assert player.wait_for_render(0.01)


# ---------------------------------------------------------------------------
# The similarity index's hard filter
# ---------------------------------------------------------------------------


def test_similarity_only_returns_allowed_paths_even_when_rules_relax() -> None:
    sim = _sim(TRACKS)
    entries = sim.entries_snapshot()
    only = frozenset(entry.path for entry in entries[3:5])
    for _ in range(5):
        picked = sim.find_next_for_path(
            entries[0].path, deque([entries[0].path]), only=only, repeat_filter=lambda _e: False
        )
        assert picked.path in only
    for _ in range(5):
        assert sim.find_distant(entries[0].path, deque(), only=only).path in only
        assert (
            sim.find_distant(entries[0].path, deque(), allowed=lambda _e: False, only=only).path
            in only
        )
