"""Recent-play history persistence through the real SQLite catalog."""

from __future__ import annotations

import math

import pytest

from autodj.config import (
    AutoDJConfig,
    HuggingFaceConfig,
    IndexConfig,
    LibraryConfig,
    ModelConfig,
    PlaybackConfig,
)
from autodj.library import LibraryManager, _EmptySimilarity
from autodj.server import PlayerBridge
from tests.integration._helpers import _make_entry, _make_player_mock, _make_sim_mock


def _bridge_with_library(root, current=None):
    cfg = AutoDJConfig(
        library=LibraryConfig(music_dir=root / "Music"),
        index=IndexConfig(index_dir=root / "index", model_dir=root / "models"),
        playback=PlaybackConfig(),
        model=ModelConfig(),
        huggingface=HuggingFaceConfig(),
        config_path=root / "config.toml",
    )
    library = LibraryManager(cfg, _EmptySimilarity())
    player = _make_player_mock(current)
    player._cfg = cfg
    bridge = PlayerBridge(player=player, sim=_make_sim_mock())
    bridge.library = library
    return bridge, library


def test_history_reopens_in_order_with_repeated_plays_and_500_row_cap(tmp_path) -> None:
    bridge, library = _bridge_with_library(tmp_path)
    repeated = _make_entry(7)
    try:
        for entry in [repeated, repeated, *(_make_entry(i) for i in range(499))]:
            bridge.on_track_started(entry)
        expected = bridge.history_snapshot()
        assert library.catalog.get_setting("play_history") is None
    finally:
        library.close()

    restored, library = _bridge_with_library(tmp_path)
    try:
        restored.load_persistent_state()
        assert restored.history_snapshot() == expected
        assert len(expected) == 500
        assert [row["title"] for row in expected] == [
            "Song 7",
            *(f"Song {i}" for i in range(499)),
        ]
    finally:
        library.close()


def test_forgetting_current_play_persists_removal(tmp_path) -> None:
    bridge, library = _bridge_with_library(tmp_path)
    current = _make_entry(1)
    try:
        bridge.on_track_started(current)
        bridge.on_track_started(current)
        bridge.forget_track(current)
        expected = bridge.history_snapshot()
    finally:
        library.close()

    restored, library = _bridge_with_library(tmp_path)
    try:
        restored.load_persistent_state()
        assert restored.history_snapshot() == expected
        assert len(expected) == 1
    finally:
        library.close()


def test_corrupt_saved_rows_are_ignored(tmp_path) -> None:
    bridge, library = _bridge_with_library(tmp_path)
    valid = {
        "title": "Valid",
        "artist": "Artist",
        "duration": 180.0,
        "played_at": "2026-01-01T00:00:00+00:00",
    }
    try:
        library.catalog.set_setting(
            "play_history",
            [
                None,
                {"title": "Missing fields"},
                {**valid, "duration": math.nan},
                {**valid, "duration": -1},
                {**valid, "played_at": "not a date"},
                valid,
            ],
        )
        bridge.load_persistent_state()
        assert bridge.history_snapshot() == [valid]
    finally:
        library.close()


def test_invalid_json_setting_does_not_block_startup(tmp_path) -> None:
    bridge, library = _bridge_with_library(tmp_path)
    try:
        library.catalog._db().execute(
            "INSERT INTO catalog_settings VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("play_history", "{"),
        )
        library.catalog._db().commit()
        bridge.load_persistent_state()
        assert bridge.history_snapshot() == []
    finally:
        library.close()


@pytest.mark.parametrize("record_seed_first", [False, True])
def test_restored_history_and_browser_seed_advance_do_not_duplicate_seed(
    tmp_path, record_seed_first: bool
) -> None:
    previous, library = _bridge_with_library(tmp_path)
    try:
        previous.on_track_started(_make_entry(9))
    finally:
        library.close()

    seed, next_track = _make_entry(0), _make_entry(1)
    bridge, library = _bridge_with_library(tmp_path, seed)
    try:
        bridge.player._state.next_track = next_track
        bridge.player._pick_next.side_effect = [_make_entry(2)]
        bridge.load_persistent_state()
        if record_seed_first:
            bridge.record_seed(seed)
        bridge.advance_now()

        assert [row["title"] for row in bridge.history_snapshot()] == [
            "Song 9",
            "Song 0",
            "Song 1",
        ]
        assert sum(row["title"] == seed.title for row in bridge.history_snapshot()) == 1
    finally:
        library.close()
