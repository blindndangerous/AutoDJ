"""Every pick mode that applied repeat rules before honours the richer ones."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import faiss
import numpy as np
import pytest

from autodj.clockwheel import ClockSlot
from autodj.config import PlaybackConfig
from autodj.indexer import FEATURE_DIM
from autodj.player import Player, PlayerState, apply_repeat_windows
from autodj.repeat_rules import RepeatRules
from autodj.similarity import SimilarityIndex
from tests.unit._fakes import make_cfg_mock, make_entry

# (artist, title).  Track 0 has just played; only track 4 is allowed after it.
TRACKS = [
    ("Bruno Mars", "Uptown Funk"),
    ("Silk Sonic", "Leave the Door Open"),  # related to Bruno Mars (starter list)
    ("Mark Ronson feat. Bruno Mars", "Something Else"),  # same artist, split
    ("Other Artist", "Uptown Funk (Live)"),  # same title, another version
    ("Allowed Artist", "Fine Song"),
]
HELD_BACK = {1: "related artist", 2: "same artist", 3: "same title"}


def _sim(tracks: list[tuple[str, str]]) -> SimilarityIndex:
    rng = np.random.default_rng(7)
    vectors = rng.standard_normal((len(tracks), FEATURE_DIM)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    index = faiss.IndexFlatIP(FEATURE_DIM)
    index.add(vectors)
    entries = []
    for i, (artist, title) in enumerate(tracks):
        entry = make_entry(i)
        entry.artist, entry.title, entry.album = artist, title, f"Album {i}"
        entries.append(entry)
    return SimilarityIndex(faiss_index=index, entries=entries)


def _player(pick_mode: str = "similarity", tracks=TRACKS, **playback) -> Player:
    cfg = make_cfg_mock()
    settings = {"no_repeat_window": 1, "related_artists_starter": True, **playback}
    cfg.playback = PlaybackConfig(pick_mode=pick_mode, **settings)
    player = Player(cfg, _sim(tracks), dry_run=True)
    player._pure_shuffle = pick_mode == "random"
    return player


def _library(player: Player) -> SimpleNamespace:
    entries = player._sim.entries_snapshot()
    return SimpleNamespace(
        entries_snapshot=lambda kind="music": () if kind == "liner" else entries,
        last_played=lambda: {},
        remember_setting=lambda *_args: None,
    )


def _played_first(player: Player):
    first = player.playable_entries()[0]
    with player._pick_lock:
        player._state.record_played(first)
    return first


@pytest.mark.parametrize("mode", ["similarity", "random", "least_recent"])
def test_pick_modes_honour_artist_related_and_title_rules(mode: str) -> None:
    player = _player(mode)
    if mode == "least_recent":
        player._library = _library(player)
    current = _played_first(player)
    for _ in range(5):
        context = player._pick_context()
        picked, _how = player._choose_by_mode(current, context)
        assert picked.title == "Fine Song"
        if mode != "similarity":  # similarity only meets its nearest neighbours
            assert {player.playable_entries()[i].path: r for i, r in HELD_BACK.items()} == (
                context.repeat.held_back
            )


def test_anchored_similarity_honours_the_rules() -> None:
    player = _player()
    current = _played_first(player)
    player._anchor_to_seed, player._seed_path = True, current.path
    picked, how = player._choose_by_mode(current, player._pick_context())
    assert (picked.title, how) == ("Fine Song", "anchored")


def test_clock_pick_honours_the_rules() -> None:
    player = _player("clock")
    player._library = _library(player)
    player._clock = {"cursor": 0}
    player._clock_slots = [ClockSlot(folder="Z:/Music", selection="random")]
    _played_first(player)
    for _ in range(5):
        picked = player._clock_pick(player._pick_context(), 0)
        assert picked.entry.title == "Fine Song"


def test_clock_slot_without_rules_ignores_them() -> None:
    player = _player("clock")
    player._library = _library(player)
    player._clock = {"cursor": 0}
    player._clock_slots = [ClockSlot(folder="Z:/Music", selection="random", enforce_rules=False)]
    _played_first(player)
    titles = {player._clock_pick(player._pick_context(), 0).entry.title for _ in range(60)}
    assert "Leave the Door Open" in titles


def test_discovery_honours_the_rules() -> None:
    player = _player()
    current = _played_first(player)
    player._discovery_every, player._state.discovery_enabled = 1, True
    for track_number in range(1, 6):
        player._state.track_number = track_number
        picked, how = player._choose_by_mode(current, player._pick_context())
        assert (picked.title, how) == ("Fine Song", "discovery")


def test_queue_and_fallback_ignore_the_rules() -> None:
    player = _player()
    current = _played_first(player)
    blocked = player.playable_entries()[1]
    player._state.queue.append(blocked)
    picked, how = player._choose_next(current, player._pick_context())
    assert (picked, how) == (blocked, "queue")
    picked = player._fallback_pick(current, player._pick_context(), ValueError("test"))
    assert picked.path != current.path


def test_rules_relax_when_nothing_else_can_play() -> None:
    tracks = [("Same Artist", f"Song {i}") for i in range(4)]
    for mode in ("similarity", "random"):
        player = _player(mode, tracks)
        current = _played_first(player)
        picked, _how = player._choose_by_mode(current, player._pick_context())
        assert picked.path != current.path


def test_minute_windows_use_the_state_clock() -> None:
    now = [0.0]
    player = _player(
        "random",
        no_repeat_window=0,
        artist_repeat_window=0,
        title_repeat_window=0,
        artist_repeat_minutes=30,
        no_repeat_minutes=60,
    )
    player._state.clock = lambda: now[0]
    first = _played_first(player)
    now[0] = 29 * 60.0
    context = player._pick_context()
    assert first.path in context.recent
    assert context.repeat.reason(player.playable_entries()[2]) == "same artist"
    now[0] = 31 * 60.0
    context = player._pick_context()
    assert first.path in context.recent  # the track window is still 60 minutes
    assert context.repeat.reason(player.playable_entries()[2]) is None
    now[0] = 61 * 60.0
    assert first.path not in player._pick_context().recent


def test_track_count_window_and_minutes_combine() -> None:
    player = _player("random", no_repeat_minutes=60)
    player._state.clock = lambda: 0.0
    entries = player.playable_entries()
    with player._pick_lock:
        for entry in entries[:3]:
            player._state.record_played(entry)
    recent = list(player._pick_context().recent)
    # no_repeat_window = 1 keeps the last path; the minute window adds the others.
    assert sorted(recent) == sorted(entry.path for entry in entries[:3])


def test_the_pick_logs_which_rules_held_tracks_back(caplog) -> None:
    player = _player("random")
    current = _played_first(player)
    with caplog.at_level(logging.INFO, logger="autodj.player"):
        player._choose_by_mode(current, player._pick_context())
    assert "1 same artist, 1 related artist, 1 same title" in caplog.text


def test_related_artists_file_is_read_at_start(tmp_path: Path) -> None:
    path = tmp_path / "related.toml"
    path.write_bytes(b'[related]\n"Other Artist" = ["Allowed Artist"]\n')
    player = _player("random", related_artists_file=str(path))
    with player._pick_lock:
        player._state.record_played(player.playable_entries()[3])
    context = player._pick_context()
    assert context.repeat.reason(player.playable_entries()[4]) == "related artist"


def test_known_artists_split_one_word_duos() -> None:
    tracks = [("Rihanna", "One"), ("Drake", "Two"), ("Rihanna & Drake", "Three"), ("Zed", "Z")]
    player = _player("random", tracks, related_artists_starter=False)
    with player._pick_lock:
        player._state.record_played(player.playable_entries()[2])
    context = player._pick_context()
    assert context.repeat.reason(player.playable_entries()[0]) == "same artist"
    assert context.repeat.reason(player.playable_entries()[1]) == "same artist"
    assert player._artist_splitter() is player._artist_splitter()


def test_apply_repeat_windows_keeps_history_and_reads_new_keys() -> None:
    player = _player("random")
    _played_first(player)
    player._cfg.playback.title_repeat_window = 7
    player._cfg.playback.artist_repeat_minutes = 15.0
    apply_repeat_windows(player)
    rules = player._state.rules
    assert (rules.title_window, rules.artist_minutes) == (7, 15.0)
    assert rules.related.related_to("bruno mars") == {"silk sonic"}
    assert len(player._state.play_history) == 1
    before = player._state.play_history
    apply_repeat_windows(player)  # unchanged rules: nothing is rebuilt
    assert player._state.play_history is before


def test_player_state_built_from_rules_sets_the_old_windows() -> None:
    state = PlayerState(repeat_rules=RepeatRules(track_window=9, artist_window=4))
    assert (state.no_repeat_window, state.artist_repeat_window) == (9, 4)
    assert state.recently_played.maxlen == 9
    state.resize_repeat_windows(5, 2)
    assert (state.rules.track_window, state.rules.artist_window, state.rules.title_window) == (
        5,
        2,
        2,
    )
