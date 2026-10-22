"""Recent-play archive persistence through PlayerBridge and SQLite."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from autodj.clockwheel import least_recent
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


def _bridge_with_library(root: Path, current=None):
    cfg = AutoDJConfig(
        library=LibraryConfig(music_dir=root / "Music"),
        index=IndexConfig(index_dir=root / "index", model_dir=root / "models"),
        playback=PlaybackConfig(),
        model=ModelConfig(),
        huggingface=HuggingFaceConfig(),
        config_path=root / "config.toml",
    )
    cfg.library.music_dir.mkdir(parents=True, exist_ok=True)
    library = LibraryManager(cfg, _EmptySimilarity())
    library.catalog.add_source(cfg.library.music_dir)
    player = _make_player_mock(current)
    player._cfg = cfg
    player._settings.cfg = cfg
    bridge = PlayerBridge(player=player, sim=_make_sim_mock())
    bridge.library = library
    return bridge, library


def _archive_row(event_id: str, path: str, title: str, played_at: str) -> dict:
    return {
        "event_id": event_id,
        "path": path,
        "entry_id": None,
        "title": title,
        "artist": "Artist",
        "duration": 180.0,
        "played_at": played_at,
    }


def test_history_archive_reopens_in_order_while_view_stays_at_500(tmp_path) -> None:
    bridge, library = _bridge_with_library(tmp_path)
    repeated = _make_entry(7)
    expected_archive = []
    try:
        for entry in [repeated, repeated, *(_make_entry(i) for i in range(501))]:
            bridge.on_track_started(entry)
            expected_archive.append(bridge._open_play[1].copy())
        expected_view = bridge.history_snapshot()
        assert len(expected_view) == 500
        assert library.ledger.recent_history(limit=1000) == []
    finally:
        library.close()

    restored, library = _bridge_with_library(tmp_path)
    try:
        restored.load_persistent_state()
        archive = library.ledger.recent_history(limit=1000)
        assert len(archive) == 503
        assert [row["title"] for row in archive] == [
            "Song 7",
            "Song 7",
            *(f"Song {i}" for i in range(501)),
        ]
        assert len({row["event_id"] for row in archive}) == 503
        assert archive[0]["path"] == archive[1]["path"]
        assert [row["played_at"] for row in archive] == [
            row["played_at"] for row in expected_archive
        ]

        # The ledger stores absolute paths; "Z:/Music/..." is relative on POSIX.
        def same_path(row: dict) -> dict:
            return row | {"path": os.path.normcase(os.path.abspath(row["path"]))}

        assert [same_path(row) for row in restored.history_snapshot()] == [
            same_path(row) for row in expected_view
        ]
        assert all(
            set(row) == {"event_id", "path", "title", "artist", "duration", "played_at"}
            for row in restored.history_snapshot()
        )
    finally:
        library.close()


def test_forgetting_current_play_hides_it_but_keeps_interrupted_archive_row(tmp_path) -> None:
    bridge, library = _bridge_with_library(tmp_path)
    current = _make_entry(1)
    try:
        bridge.on_track_started(current)
        bridge.on_track_started(current)
        event_id = bridge._open_play[1]["event_id"]
        bridge.forget_track(current)
        assert len(bridge.history_snapshot()) == 1
    finally:
        library.close()

    restored, library = _bridge_with_library(tmp_path)
    try:
        restored.load_persistent_state()
        assert len(restored.history_snapshot()) == 1
        archived = library.ledger.store._db.execute(
            "SELECT interrupted FROM play_history WHERE event_id=?", (event_id,)
        ).fetchone()
        assert archived["interrupted"] == 1
        assert (
            library.ledger.store._db.execute("SELECT COUNT(*) FROM play_history").fetchone()[0] == 2
        )
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
        assert os.path.normcase(os.path.abspath(seed.path)) in bridge.library.ledger.last_played()
        assert (
            os.path.normcase(os.path.abspath(next_track.path))
            in bridge.library.ledger.last_played()
        )
    finally:
        library.close()


def test_old_last_played_survives_unbounded_archive_and_still_wins_least_recent(tmp_path) -> None:
    old_path, newer_path = tmp_path / "Music" / "old.flac", tmp_path / "Music" / "newer.flac"
    old_path.parent.mkdir()
    old_path.write_bytes(b"old")
    newer_path.write_bytes(b"newer")
    _, library = _bridge_with_library(tmp_path)
    old_entry = _make_entry(1)
    newer_entry = _make_entry(2)
    old_entry.path, newer_entry.path = str(old_path), str(newer_path)
    try:
        library.catalog.put_entry(old_entry)
        library.catalog.put_entry(newer_entry)
        library.refresh()
        older_time = datetime(2026, 9, 1, tzinfo=UTC)
        newer_time = datetime(2026, 10, 1, tzinfo=UTC)
        for entry, played_at in ((old_entry, older_time), (newer_entry, newer_time)):
            library.ledger.remember_play(entry.path, played_at.timestamp())
            library.ledger.remember_history(
                _archive_row(entry.path, entry.path, entry.title, played_at.isoformat())
            )
        for i in range(501):
            path = str(tmp_path / "Music" / f"other-{i}.flac")
            played_at = datetime(2026, 10, 2, tzinfo=UTC) + timedelta(seconds=i)
            library.ledger.remember_history(
                _archive_row(f"other-{i}", path, f"Other {i}", played_at.isoformat())
            )
        library.close()

        restored = LibraryManager(library.cfg, _EmptySimilarity())
        try:
            entries = restored.entries_snapshot()
            last_played = restored.ledger.last_played()
            assert len(restored.ledger.recent_history(limit=1000)) == 503
            old_key = os.path.normcase(os.path.abspath(old_path))
            newer_key = os.path.normcase(os.path.abspath(newer_path))
            assert last_played[old_key] == older_time.timestamp()
            assert last_played[newer_key] == newer_time.timestamp()
            assert least_recent(entries, last_played).path == old_key
        finally:
            restored.close()
    finally:
        library.close()


def test_startup_loads_saved_plays_into_the_repeat_rules(tmp_path, caplog) -> None:
    bridge, library = _bridge_with_library(tmp_path)
    try:
        bridge.load_persistent_state()
        bridge.player.restore_repeat_history.assert_called_once_with(library.ledger)

        # A failure there is logged; the server still starts.
        bridge, library2 = _bridge_with_library(tmp_path / "second")
        bridge.player.restore_repeat_history.side_effect = RuntimeError("boom")
        bridge.load_persistent_state()
        assert "Could not load earlier plays into the repeat rules" in caplog.text
        library2.close()
    finally:
        library.close()
