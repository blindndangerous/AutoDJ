import os
import threading
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from autodj.catalog import LibraryCatalog
from autodj.config import (
    AutoDJConfig,
    HuggingFaceConfig,
    IndexConfig,
    LibraryConfig,
    ModelConfig,
    PlaybackConfig,
)
from autodj.indexer import FEATURE_DIM, IndexEntry
from autodj.library import LibraryManager, analyse_library


class _Faiss:
    def __init__(self, vectors: np.ndarray) -> None:
        self.vectors = vectors
        self.ntotal = len(vectors)

    def reconstruct(self, row: int) -> np.ndarray:
        return self.vectors[row]


class Similarity:
    def __init__(self, entries=(), vectors=None) -> None:
        self.entries = tuple(entries)
        self.faiss_index = _Faiss(
            np.empty((0, FEATURE_DIM), dtype=np.float32)
            if vectors is None
            else np.asarray(vectors, dtype=np.float32)
        )
        self.replaced = None

    def entries_snapshot(self):
        return self.entries

    def replace_catalog_index(self, entries, vectors):
        self.replaced = (entries, vectors.copy())


def make_config(root: Path, *, music: Path | None = None, liners: Path | None = None):
    music = root / "Music" if music is None else music
    music.mkdir(parents=True, exist_ok=True)
    index = root / "index" / "default"
    return AutoDJConfig(
        library=LibraryConfig(music_dir=music, supported_formats=["flac"]),
        index=IndexConfig(index_dir=index.parent, model_dir=root / "models"),
        playback=PlaybackConfig(liners_folder=str(liners) if liners else None),
        model=ModelConfig(),
        huggingface=HuggingFaceConfig(),
        config_path=root / "config.toml",
    )


def make_entry(path: Path, *, title: str | None = None, energy: float = 0.25) -> IndexEntry:
    return IndexEntry(
        str(path),
        title or path.stem,
        "Artist",
        "Album",
        "",
        120,
        2001,
        180.0,
        energy,
        -1,
        -1,
        0.0,
    )


@pytest.fixture
def manager_factory(tmp_path):
    managers = []

    def create(**kwargs):
        cfg = make_config(tmp_path, **kwargs)
        manager = LibraryManager(cfg, Similarity())
        managers.append(manager)
        return manager

    yield create
    for manager in managers:
        manager.close()


def fake_basic(path: Path, kind: str = "music"):
    del kind
    return make_entry(path), {
        "start": 0.0,
        "intro": 10.0,
        "outro": 165.0,
        "segue": 160.0,
        "end": 179.0,
    }


def fake_prepare(
    path: Path, cache_dir: Path, kind: str = "music", quiet_boost_db: float = 9.0
) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    result = cache_dir / (
        f"type-b-v1-music-{quiet_boost_db:g}-{path.stem}.wav"
        if kind == "music"
        else f"{kind}-{path.stem}.wav"
    )
    result.write_bytes(b"prepared")
    return result


def test_seeds_missing_legacy_roots_and_does_not_rewrite_index(tmp_path: Path) -> None:
    cfg = make_config(
        tmp_path, music=tmp_path / "missing-music", liners=tmp_path / "missing-liners"
    )
    sim = Similarity()
    manager = LibraryManager(cfg, sim)
    try:
        assert {row["path"] for row in manager.catalog.sources()} == {
            os.path.normcase(str(tmp_path / "missing-music")),
            os.path.normcase(str(tmp_path / "missing-liners")),
        }
        assert sim.replaced[1].shape == (0, FEATURE_DIM)
        assert not (cfg.index.active_dir / "index-manifest.json").exists()
    finally:
        manager.close()


def test_seeds_legacy_records_and_vectors_without_touching_faiss_generation(tmp_path: Path) -> None:
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "gold.flac"
    song.write_bytes(b"gold")
    original = make_entry(song, title="Gold title", energy=0.7)
    vector = np.arange(FEATURE_DIM, dtype=np.float32)
    cfg = make_config(tmp_path, music=music)
    sim = Similarity([original], vector[None, :])
    manager = LibraryManager(cfg, sim)
    try:
        loaded = manager.entry_for_path(song)
        assert loaded.title == "Gold title"
        assert loaded.energy == 0.7
        assert manager.catalog.has_vector(song)
        np.testing.assert_array_equal(sim.replaced[1][0], vector)
    finally:
        manager.close()


def test_constructor_closes_catalog_when_legacy_vectors_are_invalid(tmp_path, monkeypatch):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "gold.flac"
    song.write_bytes(b"gold")
    cfg = make_config(tmp_path, music=music)
    invalid = Similarity([make_entry(song)], np.zeros((1, FEATURE_DIM - 1), dtype=np.float32))
    opened = []
    catalog_factory = LibraryCatalog

    def capture_catalog(path):
        catalog = catalog_factory(path)
        opened.append(catalog)
        return catalog

    with (
        patch("autodj.library.LibraryCatalog", side_effect=capture_catalog),
        pytest.raises(ValueError, match="legacy vectors do not match"),
    ):
        LibraryManager(cfg, invalid)
    assert len(opened) == 1
    assert opened[0]._conn is None


def test_scan_imports_recursive_music_and_liners_and_reuses_preparation(manager_factory, tmp_path):
    music = tmp_path / "Music"
    nested = music / "nested"
    liner_dir = music / "voice"
    nested.mkdir(parents=True)
    liner_dir.mkdir()
    song, liner = nested / "song.flac", liner_dir / "liner.wav"
    song.write_bytes(b"music")
    liner.write_bytes(b"voice")
    manager = manager_factory(music=music, liners=liner_dir)
    analyze = patch("autodj.library.analyze_basic", side_effect=fake_basic)
    prepare = patch("autodj.library.prepare_source", side_effect=fake_prepare)
    with analyze as analyzed, prepare as prepared:
        manager.scan()
        assert manager.entry_for_path(song).title == "song"
        assert len(manager.entries_snapshot("liner")) == 1
        assert manager.entry_for_path(liner).title == "liner"
        assert manager.catalog.entry_for_path(liner).title == "liner"
        assert analyzed.call_count == prepared.call_count == 2
        manager.scan()
        assert analyzed.call_count == prepared.call_count == 2
        assert manager.snapshot()["import"]["errors"] == []


def test_scan_continues_after_per_file_failure(manager_factory, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    bad, good = music / "bad.flac", music / "good.flac"
    bad.write_bytes(b"bad")
    good.write_bytes(b"good")
    manager = manager_factory(music=music)

    def analyze(path, kind="music"):
        if path == bad:
            raise ValueError("decode failed")
        return fake_basic(path, kind)

    with (
        patch("autodj.library.analyze_basic", side_effect=analyze),
        patch("autodj.library.prepare_source", side_effect=fake_prepare),
    ):
        manager.scan()
    assert manager.entry_for_path(good) is not None
    assert manager.entry_for_path(bad) is None
    assert any("bad.flac" in error for error in manager.snapshot()["import"]["errors"])


def test_folder_scan_counts_only_requested_sources(manager_factory, tmp_path):
    manager = manager_factory()
    (manager.cfg.library.music_dir / "existing.flac").write_bytes(b"existing")
    holiday = tmp_path / "Holiday"
    holiday.mkdir()
    for name in ("first.flac", "second.flac"):
        (holiday / name).write_bytes(name.encode())
    nested = holiday / "nested"
    nested.mkdir()
    (nested / "excluded.flac").write_bytes(b"nested")
    source = manager.catalog.add_source(holiday, recursive=False)
    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic) as analyzed,
        patch("autodj.library.prepare_source", side_effect=fake_prepare),
    ):
        manager.scan({source["id"]})
        assert {item.args[0].name for item in analyzed.call_args_list} == {
            "first.flac",
            "second.flac",
        }
        assert manager.snapshot()["import"]["total"] == 2
        assert manager.snapshot()["import"]["done"] == 2
        manager.scan()
        assert manager.snapshot()["import"]["total"] == 3
        assert manager.entry_for_path(manager.cfg.library.music_dir / "existing.flac") is not None


@pytest.mark.parametrize("full_rescan", [False, True])
def test_added_folders_queue_once_without_expanding_to_other_sources(
    manager_factory, tmp_path, full_rescan
):
    manager = manager_factory()
    roots = []
    for name, count in (("First", 1), ("Second", 2), ("Third", 1)):
        folder = tmp_path / name
        folder.mkdir()
        for number in range(count):
            (folder / f"{number}.flac").write_bytes(f"{name}-{number}".encode())
        roots.append(manager.catalog.add_source(folder)["id"])
    entered, release = threading.Event(), threading.Event()
    observed = []
    original_scan = manager._scan

    def scoped_scan(source_ids):
        if not observed:
            entered.set()
            assert release.wait(timeout=3)
        original_scan(source_ids)
        observed.append((source_ids, manager.snapshot()["import"]["total"]))

    manager._scan = scoped_scan
    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.prepare_source", side_effect=fake_prepare),
    ):
        assert manager.start_scan({roots[0]})
        try:
            assert entered.wait(timeout=3)
            assert manager.start_scan({roots[1]})
            assert manager.start_scan({roots[2]})
            assert manager.start_scan({roots[2]})
            if full_rescan:
                assert manager.start_scan()
        finally:
            release.set()
        manager._thread.join(timeout=5)
        assert not manager._thread.is_alive()
    assert observed == [
        ({roots[0]}, 1),
        (None, 4) if full_rescan else ({roots[1], roots[2]}, 3),
    ]


def test_basic_entry_is_playable_while_audio_preparation_runs_and_analysis_can_start(
    manager_factory, tmp_path
):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"music")
    manager = manager_factory(music=music)
    entered = threading.Event()
    release = threading.Event()
    captured = {}

    def slow_prepare(path, cache_dir, kind="music", quiet_boost_db=9.0):
        del path, cache_dir, kind, quiet_boost_db
        entered.set()
        release.wait(timeout=3)
        return tmp_path / "prepared.wav"

    class Jobs:
        def configure(self, **kwargs):
            captured.update(kwargs)

        def start(self, command):
            assert command == "analyse-library"
            return True

    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.prepare_source", side_effect=slow_prepare),
        patch("autodj.jobs.get_manager", return_value=Jobs()),
    ):
        assert manager.start_scan()
        assert entered.wait(timeout=3)
        assert manager.entry_for_path(song) is not None
        assert manager.catalog.points(song)["end"] == 179.0
        assert manager.snapshot()["import"]["phase"] == "preparing audio"
        assert len(manager.analysis_pending()) == 1
        assert manager.schedule_analysis()
        release.set()
        manager._thread.join(timeout=3)
        assert not manager._thread.is_alive()
    assert captured["index_dir"] == manager.cfg.index.index_dir
    assert captured["index_dir"] != manager.cfg.index.active_dir


def test_invalid_automatic_point_does_not_block_basic_track(manager_factory, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"music")
    manager = manager_factory(music=music)
    invalid_points = {"start": 0.0, "intro": 40.0, "outro": None, "segue": None, "end": 20.0}
    with (
        patch("autodj.library.analyze_basic", return_value=(make_entry(song), invalid_points)),
        patch("autodj.library.prepare_source", side_effect=fake_prepare),
    ):
        manager.scan()
    assert manager.entry_for_path(song) is not None
    assert manager.catalog.points(song)["intro"] is None
    assert manager.catalog.points(song)["end"] == 20.0
    assert not any("song.flac" in error for error in manager.snapshot()["import"]["errors"])


@pytest.mark.parametrize("point_source", ["user", "imported"])
def test_replaced_track_resets_manual_cues_and_derived_cache(
    manager_factory, tmp_path, point_source
):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"original audio bytes")
    old_prepared = tmp_path / "old-prepared.wav"
    old_prepared.write_bytes(b"old prepared audio")
    manager = manager_factory(music=music)
    manager.catalog.put_entry(make_entry(song), prepared_path=str(old_prepared))
    manager.catalog.set_points(song, {"start": 0, "outro": 170, "end": 180}, source=point_source)
    manager.catalog.set_vector(song, np.ones(FEATURE_DIM, dtype=np.float32))

    song.write_bytes(b"replacement audio with different bytes and length")

    def short_track(path: Path, kind: str = "music"):
        del kind
        fresh = make_entry(path)
        fresh.length = 100.0
        return fresh, {
            "start": 0.0,
            "intro": 5.0,
            "outro": 90.0,
            "segue": 95.0,
            "end": 99.0,
        }

    with (
        patch("autodj.library.analyze_basic", side_effect=short_track),
        patch("autodj.library.prepare_source", side_effect=fake_prepare),
    ):
        manager.scan()

    record = manager.catalog.record_for_path(song)
    assert record["length"] == 100.0
    assert record["points"]["outro"] == 90.0
    assert record["point_sources"]["outro"] == "analysis"
    assert record["prepared_path"] != os.path.normcase(str(old_prepared))
    assert not manager.catalog.has_vector(song)
    assert not any("song.flac" in error for error in manager.snapshot()["import"]["errors"])


def test_confirmed_move_preserves_points_prepared_audio_vector_and_play_history(
    manager_factory, tmp_path
):
    music = tmp_path / "Music"
    music.mkdir()
    old, new = music / "old.flac", music / "nested" / "new.flac"
    old.write_bytes(b"unchanged bytes")
    prepared = tmp_path / "prepared.wav"
    prepared.write_bytes(b"prepared audio")
    manager = manager_factory(music=music)
    original = make_entry(old)
    manager.catalog.put_entry(
        original, points={"start": 0, "segue": 150, "end": 170}, prepared_path=str(prepared)
    )
    vector = np.ones(FEATURE_DIM, dtype=np.float32)
    manager.catalog.set_vector(old, vector)
    manager.remember_play(old, 42.0)
    manager.tick()
    record_id = manager.catalog.record_for_path(old)["id"]
    new.parent.mkdir()
    old.rename(new)
    with patch("autodj.library.analyze_basic", side_effect=fake_basic) as analyze:
        manager.scan()
    record = manager.catalog.record_for_path(new)
    assert record["id"] == record_id
    assert record["points"]["segue"] == 150
    assert record["prepared_path"] == os.path.normcase(str(prepared))
    assert manager.last_played()[os.path.normcase(str(new))] == 42.0
    assert analyze.call_count == 0
    np.testing.assert_array_equal(manager.catalog.vectors_snapshot()[1][0], vector)


def test_unchanged_liner_with_unset_analysis_segue_is_backfilled(manager_factory, tmp_path):
    liner_root = tmp_path / "liners"
    liner_root.mkdir()
    liner = liner_root / "liner.wav"
    liner.write_bytes(b"liner")
    manager = manager_factory(liners=liner_root)
    manager.catalog.put_entry(
        make_entry(liner),
        "liner",
        points={"start": 0.0, "end": 180.0, "segue": None, "overlap_start": 12.0},
        point_source="analysis",
    )
    manager.catalog.set_points(liner, {"overlap_start": 18.0}, source="user")

    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic) as analyzed,
        patch("autodj.library.prepare_source", side_effect=fake_prepare),
    ):
        manager.scan()

    analyzed.assert_called_once_with(liner, kind="liner")
    points = manager.catalog.points(liner)
    assert points["segue"] == 160.0
    assert points["end"] == 180.0
    assert points["overlap_start"] == 18.0


def test_liner_segue_backfill_clamps_to_saved_manual_range(manager_factory, tmp_path):
    liner_root = tmp_path / "liners"
    liner_root.mkdir()
    liner = liner_root / "liner.wav"
    liner.write_bytes(b"liner")
    manager = manager_factory(liners=liner_root)
    manager.catalog.put_entry(
        make_entry(liner),
        "liner",
        points={"start": 0.0, "end": 180.0, "segue": None},
        point_source="analysis",
    )
    manager.catalog.set_points(
        liner,
        {
            "start": 20.0,
            "end": 100.0,
            "intro": 30.0,
            "outro": 80.0,
            "overlap_start": 12.0,
        },
        source="user",
    )

    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic) as analyzed,
        patch("autodj.library.prepare_source", side_effect=fake_prepare),
    ):
        manager.scan()

    analyzed.assert_called_once_with(liner, kind="liner")
    assert manager.catalog.points(liner) == {
        "start": 20.0,
        "intro": 30.0,
        "outro": 80.0,
        "segue": 100.0,
        "end": 100.0,
        "overlap_start": 12.0,
    }
    assert manager.snapshot()["import"]["errors"] == []


@pytest.mark.parametrize("point_source", ["user", "imported"])
def test_unchanged_liner_preserves_manually_cleared_segue(manager_factory, tmp_path, point_source):
    liner_root = tmp_path / "liners"
    liner_root.mkdir()
    liner = liner_root / "liner.wav"
    liner.write_bytes(b"liner")
    manager = manager_factory(liners=liner_root)
    manager.catalog.put_entry(
        make_entry(liner),
        "liner",
        points={"start": 0.0, "end": 180.0, "segue": None},
        point_source="analysis",
    )
    manager.catalog.set_points(liner, {"segue": None}, source=point_source)

    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic) as analyzed,
        patch("autodj.library.prepare_source", side_effect=fake_prepare),
    ):
        manager.scan()

    analyzed.assert_not_called()
    assert manager.catalog.points(liner)["segue"] is None


def test_advanced_analysis_obeys_source_choice_and_uses_vector_presence(manager_factory, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"music")
    manager = manager_factory(music=music)
    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.prepare_source", side_effect=fake_prepare),
    ):
        manager.scan()
    assert len(manager.analysis_pending()) == 1
    source = manager.catalog.sources("music")[0]
    manager.catalog.update_source(source["id"], recursive=True, advanced=False)
    assert manager.analysis_pending() == []
    manager.catalog.update_source(source["id"], recursive=True, advanced=True)
    manager.catalog.set_vector(song, np.zeros(FEATURE_DIM, dtype=np.float32))
    assert manager.analysis_pending() == []


def test_basic_suggestions_include_legacy_intro_outro_without_overwriting_manual_points(
    manager_factory, tmp_path
):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"music")
    manager = manager_factory(music=music)
    manager.cfg.index.active_dir.mkdir(parents=True, exist_ok=True)
    (manager.cfg.index.active_dir / "dj_meta.db").write_bytes(b"placeholder")

    class Cache:
        def get(self, path):
            del path
            return type("Meta", (), {"intro_end_s": 8.0, "outro_start_s": 155.0})()

    manager.catalog.put_entry(make_entry(song), points={"outro": 140.0}, point_source="user")
    with (
        patch("autodj.dj_meta.get_cache", return_value=Cache()),
        patch(
            "autodj.library.analyze_basic",
            return_value=(make_entry(song), {"start": 0, "intro": None, "outro": None, "end": 179}),
        ),
        patch("autodj.library.prepare_source", side_effect=fake_prepare),
    ):
        manager.scan()
    assert manager.catalog.points(song)["intro"] == 8.0
    assert manager.catalog.points(song)["outro"] == 140.0


def test_play_history_and_recent_archive_are_buffered_until_tick_and_close(
    manager_factory, tmp_path
):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"music")
    manager = manager_factory(music=music)
    manager.catalog.put_entry(make_entry(song))
    manager.refresh()
    manager.remember_play(song, 99.5)
    first = {
        "event_id": "play-1",
        "path": str(song),
        "entry_id": None,
        "title": "Song",
        "artist": "Artist",
        "duration": 180.0,
        "played_at": "2026-01-01T00:00:00+00:00",
    }
    manager.remember_history(first)
    assert manager.catalog.last_played()[os.path.normcase(str(song))] == 0
    assert manager.last_played()[os.path.normcase(str(song))] == 99.5
    assert manager.catalog.recent_history() == []
    manager.tick()
    assert manager.catalog.last_played()[os.path.normcase(str(song))] == 99.5
    stored = manager.catalog.recent_history()
    assert len(stored) == 1
    assert (stored[0]["event_id"], stored[0]["title"], stored[0]["played_at"]) == (
        first["event_id"],
        first["title"],
        first["played_at"],
    )
    assert stored[0]["entry_id"] is not None

    second = {**first, "event_id": "play-2", "title": "Interrupted"}
    manager.remember_history(second)
    manager.interrupt_history(second["event_id"])
    assert len(manager.catalog.recent_history()) == 1
    manager.close()

    reopened = LibraryCatalog(manager.cfg.index.active_dir / "library.db")
    try:
        assert [row["event_id"] for row in reopened.recent_history()] == ["play-1"]
        rows = (
            reopened._db()
            .execute("SELECT event_id,interrupted FROM play_history ORDER BY rowid")
            .fetchall()
        )
        assert [(row["event_id"], row["interrupted"]) for row in rows] == [
            ("play-1", 0),
            ("play-2", 1),
        ]
    finally:
        reopened.close()


def test_history_events_queued_during_flush_are_persisted_next_tick(manager_factory, monkeypatch):
    manager = manager_factory()
    first = {
        "event_id": "play-1",
        "path": r"C:\Music\one.flac",
        "entry_id": None,
        "title": "One",
        "artist": "Artist",
        "duration": 180.0,
        "played_at": "2026-01-01T00:00:00+00:00",
    }
    second = {**first, "event_id": "play-2", "title": "Two"}
    manager.remember_history(first)
    write_history = manager.catalog.write_history
    enqueued = False

    def write_during_callback(rows, interrupted=()):
        nonlocal enqueued
        if not enqueued:
            enqueued = True
            manager.remember_history(second)
            manager.interrupt_history(first["event_id"])
        write_history(rows, interrupted)

    monkeypatch.setattr(manager.catalog, "write_history", write_during_callback)
    manager.tick()
    assert [row["event_id"] for row in manager.catalog.recent_history()] == ["play-1"]

    manager.tick()
    assert [row["event_id"] for row in manager.catalog.recent_history()] == ["play-2"]
    rows = (
        manager.catalog._db()
        .execute("SELECT event_id,interrupted FROM play_history ORDER BY rowid")
        .fetchall()
    )
    assert [(row["event_id"], row["interrupted"]) for row in rows] == [
        ("play-1", 1),
        ("play-2", 0),
    ]


def test_runtime_settings_are_buffered_until_tick_and_close(manager_factory, tmp_path):
    manager = manager_factory()
    manager.remember_setting("clock", {"cursor": 3, "enabled": True})
    assert manager.catalog.get_setting("clock") is None
    manager.tick()
    assert manager.catalog.get_setting("clock") == {"cursor": 3, "enabled": True}

    manager.remember_setting("clock", {"cursor": 4, "enabled": True})
    manager.close()
    reopened = LibraryCatalog(manager.cfg.index.active_dir / "library.db")
    try:
        assert reopened.get_setting("clock") == {"cursor": 4, "enabled": True}
    finally:
        reopened.close()


def test_start_scan_reruns_with_new_sources_when_requested_during_scan(manager_factory, tmp_path):
    manager = manager_factory()
    entered = threading.Event()
    release = threading.Event()
    repeated = threading.Event()
    second_root = tmp_path / "Second"
    second_root.mkdir()
    observed = []

    def pass_scan(source_ids):
        assert source_ids is None
        observed.append({source["path"] for source in manager.catalog.sources("music")})
        if len(observed) == 1:
            entered.set()
            release.wait(timeout=2)
        else:
            repeated.set()

    manager._scan = pass_scan
    assert manager.start_scan()
    assert entered.wait(timeout=1)
    manager.catalog.add_source(second_root)
    assert manager.start_scan()
    release.set()
    assert repeated.wait(timeout=1)
    manager._thread.join(timeout=1)
    assert len(observed) == 2
    assert os.path.normcase(str(second_root.resolve())) in observed[1]


def test_failed_scan_thread_start_releases_worker_for_retry(manager_factory):
    manager = manager_factory()
    with (
        patch("threading.Thread.start", side_effect=RuntimeError("cannot start thread")),
        pytest.raises(RuntimeError, match="cannot start thread"),
    ):
        manager.start_scan()
    assert not manager._scan_lock.locked()
    assert manager._thread is None
    manager.scan()
    assert manager.snapshot()["import"]["phase"] == "complete"


def test_snapshot_uses_cached_counts(manager_factory):
    manager = manager_factory()
    with patch.object(manager.catalog, "count_entries", side_effect=AssertionError("disk count")):
        snapshot = manager.snapshot()
    assert snapshot["tracks_ready"] == 0
    assert snapshot["tracks_indexed"] == 0


def test_discovery_rejects_file_symlink_outside_source(manager_factory, tmp_path):
    root = tmp_path / "Music"
    root.mkdir()
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"outside")
    try:
        (root / "link.flac").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlink unavailable: {exc}")
    manager = manager_factory(music=root)
    assert all(path.name != "link.flac" for _, path in manager._discover())


def test_advanced_worker_adds_vector_without_publishing_legacy_index(tmp_path: Path):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"music")
    cfg = make_config(tmp_path, music=music)
    initial = LibraryManager(cfg, Similarity())
    try:
        initial.catalog.put_entry(make_entry(song), points={"start": 0.0, "end": 170.0})
    finally:
        initial.close()

    vector = np.full(FEATURE_DIM, 0.5, dtype=np.float32)

    def embed(tracks, wrapper, workers, checkpoint, throttle_ms, report):
        del wrapper, workers, throttle_ms, report
        advanced = make_entry(tracks[0].path)
        advanced.bpm, advanced.key, advanced.mode = 121.0, 7, 1
        checkpoint([advanced], [vector])
        return [advanced], [vector]

    with patch("autodj.library._embed_new_tracks", side_effect=embed):
        assert analyse_library(cfg, object()) == 1
    check = LibraryManager(cfg, Similarity())
    try:
        assert check.catalog.has_vector(song)
        assert check.entry_for_path(song).bpm == 121
        assert not (cfg.index.active_dir / "index-manifest.json").exists()
    finally:
        check.close()


def _age(path: Path, seconds: float = 3600.0) -> Path:
    old = path.stat().st_mtime - seconds
    os.utime(path, (old, old))
    return path


def test_remove_stale_prepared_only_deletes_unused_autodj_cache_files(tmp_path):
    from autodj.processing import remove_stale_prepared

    cache = tmp_path / "prepared"
    cache.mkdir()
    key = "a" * 64

    def made(name: str, data: bytes = b"wav") -> Path:
        path = cache / name
        path.write_bytes(data)
        return _age(path)

    old_music = made(f"{key}.wav", b"12345")
    old_boost = made(f"type-b-v1-music-6-{'b' * 64}.wav", b"123")
    current = made(f"type-b-v1-music-9-{'c' * 64}.wav")
    in_use = made(f"{'d' * 64}.wav")
    source_named_like_cache = made(f"{'e' * 64}.wav")
    user_file = made("song.wav")
    notes = made("notes.txt")
    fresh = cache / f"{'f' * 64}.wav"
    fresh.write_bytes(b"new")
    nested = cache / "sub"
    nested.mkdir()
    deeper = made(f"sub/{'0' * 64}.wav")

    removed, freed = remove_stale_prepared(
        cache, [in_use, str(source_named_like_cache)], quiet_boost_db=9
    )

    assert (removed, freed) == (2, 8)
    assert not old_music.exists()
    assert not old_boost.exists()
    for kept in (current, in_use, source_named_like_cache, user_file, notes, fresh, deeper):
        assert kept.exists()
    assert remove_stale_prepared(tmp_path / "missing", []) == (0, 0)


def test_scan_removes_prepared_audio_left_by_older_settings(manager_factory, tmp_path, caplog):
    music = tmp_path / "Music"
    song = music / "song.flac"
    music.mkdir()
    song.write_bytes(b"music")
    manager = manager_factory(music=music)
    cache = manager.cfg.index.active_dir / "prepared"
    cache.mkdir(parents=True)
    stale = cache / f"{'a' * 64}.wav"
    stale.write_bytes(b"x" * 2_000_000)
    _age(stale)
    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.prepare_source", side_effect=fake_prepare),
        caplog.at_level("INFO", logger="autodj.library"),
    ):
        manager.scan()

    assert not stale.exists()
    assert song.exists()
    assert manager.catalog.record_for_path(song)["prepared_path"]
    assert "freeing 2.0 MB" in caplog.text
