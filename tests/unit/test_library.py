import os
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pytest

from autodj.catalog import LibraryCatalog, _path
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

    def replace_catalog_entries(self, entries):
        if self.replaced is None or [e.path for e in self.replaced[0]] != [e.path for e in entries]:
            return False
        self.replaced = (entries, self.replaced[1])
        return True


def make_config(
    root: Path,
    *,
    music: Path | None = None,
    liners: Path | None = None,
    processing: str | None = None,
):
    music = root / "Music" if music is None else music
    music.mkdir(parents=True, exist_ok=True)
    index = root / "index" / "default"
    return AutoDJConfig(
        library=LibraryConfig(music_dir=music, supported_formats=["flac"]),
        index=IndexConfig(index_dir=index.parent, model_dir=root / "models"),
        playback=PlaybackConfig(
            liners_folder=str(liners) if liners else None, loudness_processing=processing
        ),
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


def fake_basic(path: Path, kind: str = "music", max_seconds: float | None = None):
    del kind, max_seconds
    return make_entry(path), {
        "start": 0.0,
        "intro": 10.0,
        "outro": 165.0,
        "segue": 160.0,
        "end": 179.0,
    }


def fake_loudness(path: Path, kind: str = "music") -> dict:
    from autodj.processing import PREPARATION_VERSION, TARGET_LUFS

    stat = path.stat()
    lufs = TARGET_LUFS[kind] - 2.0
    return {
        "version": PREPARATION_VERSION,
        "kind": kind,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "lufs": lufs,
        "gain_db": 2.0,
    }


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


def test_scan_imports_recursive_music_and_liners_and_reuses_loudness(manager_factory, tmp_path):
    music = tmp_path / "Music"
    nested = music / "nested"
    liner_dir = music / "voice"
    nested.mkdir(parents=True)
    liner_dir.mkdir()
    song, liner = nested / "song.flac", liner_dir / "liner.wav"
    song.write_bytes(b"music")
    liner.write_bytes(b"voice")
    manager = manager_factory(music=music, liners=liner_dir, processing="smooth")
    analyze = patch("autodj.library.analyze_basic", side_effect=fake_basic)
    loudness = patch("autodj.library.analyze_loudness", side_effect=fake_loudness)
    with (
        analyze as analyzed,
        loudness as measured,
        patch("autodj.audio_meta.read_file_tags") as read_tags,
    ):
        manager.scan()
        assert manager.entry_for_path(song).title == "song"
        assert len(manager.entries_snapshot("liner")) == 1
        assert manager.entry_for_path(liner).title == "liner"
        assert manager.catalog.entry_for_path(liner).title == "liner"
        assert analyzed.call_count == measured.call_count == 2
        manager.scan()
        assert analyzed.call_count == measured.call_count == 2
        assert manager.snapshot()["import"]["errors"] == []
        read_tags.assert_not_called()


@pytest.mark.parametrize(
    ("embedded_bpm", "manual_bpm", "expected_bpm", "expected_tags"),
    [
        (0, True, 124.5, ["bpm", "key"]),
        (124, True, 124.5, ["bpm", "key"]),
        (130, False, 130, ["key"]),
    ],
)
def test_scan_can_override_existing_tags_without_replacing_analysis(
    manager_factory, tmp_path, embedded_bpm, manual_bpm, expected_bpm, expected_tags
):
    from autodj.audio_meta import FileTags

    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"music")
    manager = manager_factory(music=music)
    manager.catalog.put_entry(
        make_entry(song, title="Old title", energy=0.42),
        points={"start": 0, "end": 180, "intro": 9},
    )
    manager.catalog.retag(song, {"key": 4, **({"bpm": 124.5} if manual_bpm else {})})
    manager.refresh()

    with (
        patch(
            "autodj.audio_meta.read_file_tags",
            return_value=FileTags(title="Tagged title", artist="Tagged artist", bpm=embedded_bpm),
        ) as read_tags,
        patch("autodj.library.analyze_basic", side_effect=AssertionError("must reuse analysis")),
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
    ):
        manager.scan(override_tags=True)

    entry = manager.entry_for_path(song)
    record = manager.catalog.record_for_path(song)
    assert entry.title == "Tagged title" and entry.artist == "Tagged artist"
    assert entry.bpm == expected_bpm and entry.key == 4  # missing embedded values keep saved tags
    assert entry.energy == 0.42
    assert record["points"]["intro"] == 9
    assert record["user_tags"] == expected_tags
    read_tags.assert_called_once_with(song)


def test_queued_tag_override_runs_before_loudness_scan_finishes(manager_factory, tmp_path):
    from autodj.audio_meta import FileTags

    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"music")
    manager = manager_factory(music=music, processing="smooth")
    requested = False

    def measure(path, kind="music"):
        nonlocal requested
        if not requested:
            requested = True
            assert manager.start_scan(override_tags=True)
        return fake_loudness(path, kind)

    with (
        patch(
            "autodj.audio_meta.read_file_tags", return_value=FileTags(title="Tagged")
        ) as read_tags,
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=measure),
    ):
        assert manager.start_scan()
        manager._thread.join(timeout=5)
        assert not manager._thread.is_alive()

    assert manager.entry_for_path(song).title == "Tagged"
    read_tags.assert_called_once_with(song)


def test_scan_continues_after_per_file_failure(manager_factory, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    bad, good = music / "bad.flac", music / "good.flac"
    bad.write_bytes(b"bad")
    good.write_bytes(b"good")
    manager = manager_factory(music=music)

    def analyze(path, kind="music", max_seconds=None):
        if path == bad:
            raise ValueError("decode failed")
        return fake_basic(path, kind)

    with (
        patch("autodj.library.analyze_basic", side_effect=analyze),
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
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
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
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
    manager = manager_factory(processing="smooth")
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

    def scoped_scan(source_ids, override_tags=False):
        if not observed:
            entered.set()
            assert release.wait(timeout=3)
        original_scan(source_ids, override_tags)
        observed.append((source_ids, manager.snapshot()["import"]["total"]))

    manager._scan = scoped_scan
    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
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
    assert observed == [({roots[0]}, 4)]
    assert {source["path"] for source in manager.catalog.sources("music")} == {
        os.path.normcase(str(manager.cfg.library.music_dir.resolve()))
    } | {
        os.path.normcase(str((tmp_path / name).resolve())) for name in ("First", "Second", "Third")
    }


def test_basic_entry_is_playable_while_loudness_runs_and_analysis_can_start(
    manager_factory, tmp_path
):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"music")
    manager = manager_factory(music=music, processing="stereotool")
    entered = threading.Event()
    release = threading.Event()
    captured = {}

    def slow_loudness(path, kind="music"):
        del kind
        entered.set()
        release.wait(timeout=3)
        return fake_loudness(path)

    class Jobs:
        def configure(self, **kwargs):
            captured.update(kwargs)

        def start(self, command):
            assert command == "analyse-library"
            return True

    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=slow_loudness),
        patch("autodj.jobs.get_manager", return_value=Jobs()),
    ):
        assert manager.start_scan()
        assert entered.wait(timeout=3)
        assert manager.entry_for_path(song) is not None
        assert manager.catalog.points(song)["end"] == 179.0
        assert manager.snapshot()["import"]["phase"] == "measuring loudness"
        assert len(manager.analysis_pending()) == 1
        assert manager.schedule_analysis()
        release.set()
        manager._thread.join(timeout=3)
        assert not manager._thread.is_alive()
    assert captured["index_dir"] == manager.cfg.index.index_dir
    assert captured["index_dir"] != manager.cfg.index.active_dir


def test_queued_folder_is_scanned_before_remaining_loudness(manager_factory, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    old_songs = [music / f"old-{number}.flac" for number in range(3)]
    for song in old_songs:
        song.write_bytes(b"music")
    manager = manager_factory(music=music, processing="smooth")
    new_folder = tmp_path / "New"
    new_folder.mkdir()
    new_song = new_folder / "new.flac"
    new_song.write_bytes(b"new music")
    events = []

    def measure(path, kind="music"):
        events.append(path.name)
        if path == old_songs[0]:
            source_id = manager.catalog.add_source(new_folder)["id"]
            assert manager.start_scan({source_id})
        elif path == new_song:
            second_folder = tmp_path / "Second"
            second_folder.mkdir()
            second_song = second_folder / "second.flac"
            second_song.write_bytes(b"second music")
            source_id = manager.catalog.add_source(second_folder)["id"]
            assert manager.start_scan({source_id})
        elif path == old_songs[1]:
            assert manager.entry_for_path(new_song) is not None
        return fake_loudness(path, kind)

    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=measure),
    ):
        assert manager.start_scan()
        manager._thread.join(timeout=5)
        assert not manager._thread.is_alive()

    assert events.index("new.flac") < events.index("second.flac") < events.index("old-1.flac")
    assert set(events) == {song.name for song in old_songs} | {"new.flac", "second.flac"}
    assert manager.catalog.record_for_path(new_song)["loudness"] is not None


def test_queued_folder_during_basic_is_ready_before_loudness(manager_factory, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    old_song = music / "old.flac"
    old_song.write_bytes(b"old")
    new_folder = tmp_path / "New"
    new_folder.mkdir()
    new_song = new_folder / "new.flac"
    new_song.write_bytes(b"new")
    manager = manager_factory(music=music, processing="smooth")
    events = []

    def basic(path, kind="music", max_seconds=None):
        events.append(("basic", path.name))
        if path == old_song:
            source_id = manager.catalog.add_source(new_folder)["id"]
            assert manager.start_scan({source_id})
        return fake_basic(path, kind, max_seconds)

    def measure(path, kind="music"):
        events.append(("loudness", path.name))
        return fake_loudness(path, kind)

    with (
        patch("autodj.library.analyze_basic", side_effect=basic),
        patch("autodj.library.analyze_loudness", side_effect=measure),
    ):
        assert manager.start_scan()
        manager._thread.join(timeout=5)
        assert not manager._thread.is_alive()

    assert events.index(("basic", "new.flac")) < events.index(("loudness", "old.flac"))
    assert manager.entry_for_path(new_song) is not None


def test_queued_rescan_remeasures_file_changed_during_loudness(manager_factory, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"before")
    manager = manager_factory(music=music, processing="smooth")
    measured = []

    def measure(path, kind="music"):
        measured.append(path.stat().st_size)
        if len(measured) == 1:
            path.write_bytes(b"after the first measurement")
            source_id = manager.catalog.sources("music")[0]["id"]
            assert manager.start_scan({source_id})
        return fake_loudness(path, kind)

    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=measure),
    ):
        assert manager.start_scan()
        manager._thread.join(timeout=5)
        assert not manager._thread.is_alive()

    assert measured == [len(b"before"), len(b"after the first measurement")]
    assert manager.catalog.record_for_path(song)["loudness"]["size"] == song.stat().st_size


def test_queued_advanced_folder_schedules_after_busy_scan(manager_factory, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    old_song = music / "old.flac"
    old_song.write_bytes(b"old")
    manager = manager_factory(music=music, processing="smooth")
    source = manager.catalog.sources("music")[0]
    manager.catalog.update_source(source["id"], recursive=True, advanced=False)
    new_folder = tmp_path / "New"
    new_folder.mkdir()
    new_song = new_folder / "new.flac"
    new_song.write_bytes(b"new")
    captured = []

    class Jobs:
        def configure(self, **kwargs):
            captured.append(kwargs)

        def start(self, command):
            assert command == "analyse-library"
            return True

    def measure(path, kind="music"):
        if path == old_song:
            source_id = manager.catalog.add_source(new_folder)["id"]
            manager.start_scan({source_id})
        return fake_loudness(path, kind)

    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=measure),
    ):
        assert manager.start_scan()
        manager._thread.join(timeout=5)
        assert not manager._thread.is_alive()

    assert manager.entry_for_path(new_song) is not None
    assert manager._scan_generation == 2
    with patch("autodj.jobs.get_manager", return_value=Jobs()):
        assert manager.schedule_analysis()
    assert manager._analysis_started_generation == manager._scan_generation
    assert len(captured) == 1


def test_invalid_automatic_point_does_not_block_basic_track(manager_factory, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"music")
    manager = manager_factory(music=music)
    invalid_points = {"start": 0.0, "intro": 40.0, "outro": None, "segue": None, "end": 20.0}
    with (
        patch("autodj.library.analyze_basic", return_value=(make_entry(song), invalid_points)),
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
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

    def short_track(path: Path, kind: str = "music", max_seconds: float | None = None):
        del kind, max_seconds
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
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
    ):
        manager.scan()

    record = manager.catalog.record_for_path(song)
    assert record["length"] == 100.0
    assert record["points"]["outro"] == 90.0
    assert record["point_sources"]["outro"] == "analysis"
    assert record["prepared_path"] is None
    assert old_prepared.exists()
    assert not manager.catalog.has_vector(song)
    assert not any("song.flac" in error for error in manager.snapshot()["import"]["errors"])


def test_confirmed_move_preserves_points_loudness_vector_and_play_history(
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
    manager.catalog.set_loudness(old, fake_loudness(old))
    vector = np.ones(FEATURE_DIM, dtype=np.float32)
    manager.catalog.set_vector(old, vector)
    manager.ledger.remember_play(old, 42.0)
    manager.tick()
    record_id = manager.catalog.record_for_path(old)["id"]
    new.parent.mkdir()
    old.rename(new)
    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic) as analyze,
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness) as measure,
    ):
        manager.scan()
    record = manager.catalog.record_for_path(new)
    assert record["id"] == record_id
    assert record["points"]["segue"] == 150
    assert record["prepared_path"] == os.path.normcase(str(prepared))
    assert record["loudness"] == fake_loudness(new)
    assert manager.ledger.last_played()[os.path.normcase(str(new))] == 42.0
    assert analyze.call_count == 0
    measure.assert_not_called()
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
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
    ):
        manager.scan()

    analyzed.assert_called_once_with(liner, kind="liner", max_seconds=15 * 60.0)
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
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
    ):
        manager.scan()

    analyzed.assert_called_once_with(liner, kind="liner", max_seconds=15 * 60.0)
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
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
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
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
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
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
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
    manager.ledger.remember_play(song, 99.5)
    first = {
        "event_id": "play-1",
        "path": str(song),
        "entry_id": None,
        "title": "Song",
        "artist": "Artist",
        "duration": 180.0,
        "played_at": "2026-01-01T00:00:00+00:00",
    }
    manager.ledger.remember_history(first)
    assert manager.catalog.last_played()[os.path.normcase(str(song))] == 0
    assert manager.ledger.last_played()[os.path.normcase(str(song))] == 99.5
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
    manager.ledger.remember_history(second)
    manager.ledger.interrupt_history(second["event_id"])
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


def test_pending_history_gives_unwritten_plays_with_album(manager_factory, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"music")
    manager = manager_factory(music=music)
    manager.catalog.put_entry(make_entry(song))
    manager.refresh()
    row = {
        "event_id": "play-1",
        "path": str(song),
        "title": "Song",
        "artist": "Artist",
        "played_at": "2026-01-01T00:00:00+00:00",
    }
    manager.ledger.remember_history(row)
    manager.ledger.remember_history({**row, "event_id": "play-2", "path": str(music / "gone.flac")})
    manager.ledger.remember_history({**row, "event_id": "play-3"})
    manager.ledger.interrupt_history("play-3")
    pending = manager.ledger.pending_history()
    assert [(item["event_id"], item["album"]) for item in pending] == [
        ("play-1", "Album"),
        ("play-2", ""),
    ]
    pending[0]["album"] = "changed"
    assert manager.ledger.pending_history()[0]["album"] == "Album"
    manager.tick()
    assert manager.ledger.pending_history() == []
    manager.close()


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
    manager.ledger.remember_history(first)
    write_history = manager.catalog.write_history
    enqueued = False

    def write_during_callback(rows, interrupted=(), station="default"):
        nonlocal enqueued
        if not enqueued:
            enqueued = True
            manager.ledger.remember_history(second)
            manager.ledger.interrupt_history(first["event_id"])
        write_history(rows, interrupted, station)

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
    manager.ledger.remember_setting("clock", {"cursor": 3, "enabled": True})
    assert manager.catalog.get_setting("clock") is None
    manager.tick()
    assert manager.catalog.get_setting("clock") == {"cursor": 3, "enabled": True}

    manager.ledger.remember_setting("clock", {"cursor": 4, "enabled": True})
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

    def pass_scan(source_ids, override_tags=False):
        assert source_ids is None
        assert not override_tags
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

    def embed(tracks, wrapper, workers, checkpoint, throttle_ms, report, gate=None):
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


def test_scan_preserves_prepared_reference_without_creating_audio(manager_factory, tmp_path):
    music = tmp_path / "Music"
    song = music / "song.flac"
    music.mkdir()
    song.write_bytes(b"music")
    manager = manager_factory(music=music)
    cache = manager.cfg.index.active_dir / "prepared"
    cache.mkdir(parents=True)
    prepared = cache / "legacy.wav"
    prepared.write_bytes(b"existing prepared audio")
    manager.catalog.put_entry(make_entry(song), prepared_path=str(prepared))
    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
    ):
        manager.scan()

    assert manager.catalog.record_for_path(song)["prepared_path"] == os.path.normcase(str(prepared))
    assert prepared.read_bytes() == b"existing prepared audio"
    assert list(cache.iterdir()) == [prepared]


def _count_reads(manager, monkeypatch) -> list[int]:
    """Count catalog snapshot reads (each library reload opens one)."""
    reads = [0]
    reader = manager.catalog.reader

    def counted():
        reads[0] += 1
        return reader()

    monkeypatch.setattr(manager.catalog, "reader", counted)
    return reads


def test_plays_history_settings_cues_and_loudness_do_not_reload_library(
    manager_factory, tmp_path, monkeypatch
):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"music")
    manager = manager_factory(music=music)
    manager.catalog.put_entry(make_entry(song), points={"start": 0.0, "end": 170.0})
    manager.catalog.set_vector(song, np.ones(FEATURE_DIM, dtype=np.float32))
    manager.tick()
    loaded = manager.sim.replaced
    reads = _count_reads(manager, monkeypatch)

    manager.ledger.remember_play(song, 50.0)
    manager.ledger.remember_history(
        {
            "event_id": "play-1",
            "path": str(song),
            "title": "Song",
            "artist": "Artist",
            "duration": 180.0,
            "played_at": "2026-01-01T00:00:00+00:00",
        }
    )
    manager.ledger.remember_setting("clock", {"cursor": 2})
    manager.catalog.set_loudness(song, fake_loudness(song))
    manager.catalog.set_points(song, {"intro": 12.0})
    manager.catalog.set_intro_lift(song, 1.5)
    manager.tick()
    manager.tick()

    assert reads == [0]
    assert manager.sim.replaced is loaded
    assert manager.catalog.last_played()[os.path.normcase(str(song))] == 50.0

    other = music / "other.flac"
    other.write_bytes(b"other")
    manager.catalog.put_entry(make_entry(other))
    manager.tick()
    assert reads == [1]
    assert manager.entry_for_path(other) is not None
    # A new track without a vector leaves the vectors as they were.
    assert manager.sim.replaced[1] is loaded[1]


def test_vectors_written_by_another_process_reach_the_similarity_index(manager_factory, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"music")
    manager = manager_factory(music=music)
    manager.catalog.put_entry(make_entry(song))
    manager.tick()
    assert manager.sim.replaced[1].shape == (0, FEATURE_DIM)

    vector = np.full(FEATURE_DIM, 0.25, dtype=np.float32)
    with LibraryCatalog(manager.catalog.path) as worker:  # e.g. the analysis job
        worker.set_vector(song, vector)
    manager.tick()

    entries, vectors = manager.sim.replaced
    assert [Path(entry.path) for entry in entries] == [song]
    np.testing.assert_array_equal(vectors[0], vector)
    assert manager.snapshot()["tracks_indexed"] == 1


def test_metadata_change_keeps_the_faiss_index_and_updates_its_entries(tmp_path):
    from autodj.similarity import SimilarityIndex

    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"music")
    sim = SimilarityIndex.empty()
    manager = LibraryManager(make_config(tmp_path, music=music), sim)
    try:
        manager.catalog.put_entry(make_entry(song, title="Old"))
        manager.catalog.set_vector(song, np.ones(FEATURE_DIM, dtype=np.float32))
        manager.tick()
        index = sim.faiss_index
        assert index.ntotal == 1
        manager.catalog.put_entry(make_entry(song, title="New"))
        manager.tick()
        assert sim.faiss_index is index
        assert sim.entries_snapshot()[0].title == "New"
        assert manager.entry_for_path(song).title == "New"
    finally:
        manager.close()


def test_reload_makes_no_file_system_call_per_track(manager_factory, tmp_path, monkeypatch):
    music = tmp_path / "Music"
    music.mkdir()
    songs = [music / f"{number}.flac" for number in range(5)]
    manager = manager_factory(music=music)
    for song in songs:
        song.write_bytes(song.name.encode())
        manager.catalog.put_entry(make_entry(song))
    calls = []
    real_isdir = os.path.isdir

    def isdir(path):
        calls.append(path)
        return real_isdir(path)

    monkeypatch.setattr(os.path, "isdir", isdir)
    with (
        patch.object(Path, "is_file", side_effect=AssertionError("stat per track")),
        patch("os.path.isfile", side_effect=AssertionError("stat per track")),
    ):
        manager.refresh()
    assert len(manager.entries_snapshot()) == 5
    assert len(calls) == len(manager.catalog.sources())  # once per source folder


def test_scan_drops_deleted_tracks_but_not_an_unmounted_share(manager_factory, tmp_path):
    music = tmp_path / "Music"
    share = tmp_path / "Share"
    music.mkdir()
    share.mkdir()
    kept, deleted = music / "kept.flac", music / "deleted.flac"
    remote = share / "remote.flac"
    for path in (kept, deleted, remote):
        path.write_bytes(path.name.encode())
    manager = manager_factory(music=music)
    manager.catalog.add_source(share)
    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
    ):
        manager.scan()
        assert len(manager.entries_snapshot()) == 3
        deleted.unlink()
        # The share goes away: its folder is missing, so it covers nothing,
        # but its track is not marked gone and comes back with the share.
        moved = tmp_path / "Share-unmounted"
        share.rename(moved)
        manager.scan()
        assert [Path(entry.path).name for entry in manager.entries_snapshot()] == ["kept.flac"]
        assert manager._missing == frozenset({os.path.normcase(str(deleted))})
        moved.rename(share)
        manager.catalog.update_source(manager.catalog.sources("music")[-1]["id"], True, True)
        manager.tick()
    assert {Path(entry.path).name for entry in manager.entries_snapshot()} == {
        "kept.flac",
        "remote.flac",
    }


@pytest.mark.parametrize("processing", [None, "off", "replaygain"])
def test_scan_measures_loudness_only_for_smooth_and_stereo_tool(
    manager_factory, tmp_path, processing
):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "song.flac").write_bytes(b"music")
    manager = manager_factory(music=music, processing=processing)
    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic) as analyzed,
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness) as measured,
    ):
        manager.scan()
    assert analyzed.call_count == 1
    measured.assert_not_called()
    state = manager.snapshot()["import"]
    assert (state["phase"], state["done"], state["total"]) == ("complete", 1, 1)


def test_publishing_scanned_tracks_does_not_scan_the_library(manager_factory, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    manager = manager_factory(music=music)
    for number in range(3):
        song = music / f"{number}.flac"
        song.write_bytes(song.name.encode())
        manager.catalog.put_entry(make_entry(song))
    manager.refresh()
    first = Path(manager.entries_snapshot()[0].path)
    with patch("autodj.library._path", wraps=_path) as norm:
        manager._publish_entry(make_entry(first, title="Retagged"), "music")
        manager._publish_entry(make_entry(music / "new.flac"), "music")
    assert norm.call_count == 2  # one per published track, none per library entry
    assert [entry.title for entry in manager.entries_snapshot()] == ["Retagged", "1", "2", "new"]
    assert manager.entry_for_path(music / "new.flac").title == "new"
    assert manager.snapshot()["tracks_ready"] == 4


def test_idle_analysis_check_runs_once_until_the_catalog_changes(manager_factory, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"music")
    manager = manager_factory(music=music)
    manager.catalog.put_entry(make_entry(song))
    manager.catalog.set_vector(song, np.ones(FEATURE_DIM, dtype=np.float32))
    started = []

    class Jobs:
        def configure(self, **kwargs):
            pass

        def start(self, command):
            started.append(command)
            return True

    unembedded = manager.catalog.unembedded_music
    with (
        patch.object(manager.catalog, "unembedded_music", wraps=unembedded) as check,
        patch("autodj.jobs.get_manager", return_value=Jobs()),
    ):
        for _ in range(3):
            assert not manager.schedule_analysis()
        manager.ledger.remember_play(song, 1.0)
        manager.catalog.set_loudness(song, fake_loudness(song))
        manager.catalog.set_points(song, {"intro": 4.0})
        manager.tick()
        assert check.call_count == 1
        other = music / "other.flac"
        other.write_bytes(b"other")
        manager.catalog.put_entry(make_entry(other))
        assert manager.schedule_analysis()
        assert check.call_count == 2
    assert started == ["analyse-library"]
    assert [Path(record["path"]) for record in manager.analysis_pending()] == [other]


def test_browse_and_search_answer_from_memory(manager_factory, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from autodj._bridge import PlayerBridge

    music = tmp_path / "Music"
    nested = music / "nested"
    nested.mkdir(parents=True)
    manager = manager_factory(music=music)
    for path, title in ((music / "b.flac", "Beta"), (nested / "a.flac", "Alpha")):
        path.write_bytes(title.encode())
        manager.catalog.put_entry(make_entry(path, title=title))
    manager.refresh()
    source = manager.catalog.sources("music")[0]
    monkeypatch.setattr(manager.catalog, "reader", Mock(side_effect=AssertionError("read")))
    with (
        patch.object(Path, "is_file", side_effect=AssertionError("stat per track")),
        patch("os.path.isfile", side_effect=AssertionError("stat per track")),
    ):
        page = manager.browse_tracks("music", None, "", 0, 1)
        assert (page["total"], [row["title"] for row in page["results"]]) == (2, ["Alpha"])
        assert manager.browse_tracks("music", source["id"], "a.flac")["total"] == 1
        bridge = SimpleNamespace(library=manager, sim=None)
        assert [row["title"] for row in PlayerBridge.search(bridge, "beta artist")] == ["Beta"]
    index = manager.search_index()
    assert manager.search_index() is index  # built once per library snapshot


def _generation(number: int):
    from autodj.index_manifest import IndexManifest

    return IndexManifest(1, number, 2, "2026-01-01T00:00:00Z", f"t{number}", f"v{number}")


def test_legacy_generation_is_imported_once_in_chunks(tmp_path, monkeypatch):
    import faiss

    from autodj.similarity import SimilarityIndex

    music = tmp_path / "Music"
    music.mkdir()
    songs = [music / "a.flac", music / "b.flac"]
    for song in songs:
        song.write_bytes(song.name.encode())
    vectors = np.eye(2, FEATURE_DIM, dtype=np.float32)
    cfg = make_config(tmp_path, music=music)

    def legacy(number):
        index = faiss.IndexFlatIP(FEATURE_DIM)
        index.add(vectors)
        sim = SimilarityIndex(index, [make_entry(song) for song in songs])
        sim._manifest = _generation(number)
        return sim

    # The first start imports the generation a chunk of rows at a time.
    sim = legacy(1)
    reconstruct = Mock(side_effect=AssertionError("one row at a time"))
    monkeypatch.setattr(sim.faiss_index, "reconstruct", reconstruct, raising=False)
    LibraryManager(cfg, sim).close()
    monkeypatch.undo()
    with LibraryCatalog(cfg.index.active_dir / "library.db") as catalog:
        assert catalog.get_setting("imported_index_generation")["generation"] == 1

    def no_seeding(*_args, **_kwargs):
        raise AssertionError("re-imported")

    # Once the catalog exists, even a newer generation is not imported again.
    monkeypatch.setattr(LibraryCatalog, "seed_entries", no_seeding)
    sim = SimilarityIndex.empty()
    manager = LibraryManager(cfg, sim)
    try:
        # The catalog's vectors reach sim.
        assert [Path(entry.path) for entry in sim.entries_snapshot()] == songs
        np.testing.assert_array_equal(sim.faiss_index.reconstruct_n(0, 2), vectors)
    finally:
        manager.close()
    LibraryManager(cfg, legacy(2)).close()


def test_library_database_uses_wal(tmp_path):
    with LibraryCatalog(tmp_path / "library.db") as catalog:
        mode = catalog._db().execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_moved_track_is_playable_at_its_new_path_before_the_scan_reads_tracks(
    manager_factory, tmp_path
):
    music = tmp_path / "Music"
    music.mkdir()
    old, new = music / "old.flac", music / "new.flac"
    old.write_bytes(b"same bytes")
    manager = manager_factory(music=music)
    manager.catalog.put_entry(make_entry(old))
    manager.refresh()
    old.rename(new)
    manager._reconcile_moves([("music", new)])
    assert manager.entry_for_path(new) is not None
    assert manager.entry_for_path(old) is None


# Reload triggers from another connection: cases after jage9's #102.


@pytest.fixture
def other_connection(manager_factory, monkeypatch):
    """A manager whose library has one track, and a second catalog connection."""
    manager = manager_factory()
    other = LibraryCatalog(manager.catalog.path)
    song = manager.cfg.library.music_dir / "song.flac"
    song.write_bytes(b"track")
    other.put_entry(make_entry(song))
    monkeypatch.setattr(manager, "schedule_analysis", lambda: False)
    yield manager, other, song
    other.close()


def test_write_through_another_connection_reloads_on_the_next_tick(other_connection):
    manager, other, song = other_connection
    assert manager.entry_for_path(song) is None
    manager.tick()
    assert manager.entry_for_path(song).title == "song"
    other.put_entry(make_entry(song, title="Retagged"))
    manager.tick()
    assert manager.entry_for_path(song).title == "Retagged"


@pytest.mark.parametrize("change", ["vector_add", "vector_update", "vector_delete", "folder"])
def test_vector_and_folder_changes_from_another_connection_reload(other_connection, change):
    manager, other, song = other_connection
    if change in {"vector_update", "vector_delete"}:
        other.set_vector(song, np.zeros(FEATURE_DIM, dtype=np.float32))
    manager.tick()
    before = manager.sim.replaced
    vector = np.ones(FEATURE_DIM, dtype=np.float32)
    if change in {"vector_add", "vector_update"}:
        other.set_vector(song, vector)
    elif change == "vector_delete":
        other._db().execute("DELETE FROM catalog_vectors")
        other._db().commit()
    else:
        assert other.remove_source(other.sources("music")[0]["id"])

    manager.tick()

    assert manager.sim.replaced is not before
    _entries, vectors = manager.sim.replaced
    if change in {"vector_add", "vector_update"}:
        assert manager.snapshot()["tracks_indexed"] == 1
        np.testing.assert_array_equal(vectors[0], vector)
    else:
        assert manager.snapshot()["tracks_indexed"] == 0
        assert vectors.shape == (0, FEATURE_DIM)
    if change == "folder":
        assert manager.snapshot()["tracks_ready"] == 0
        assert manager.entry_for_path(song) is None


@pytest.mark.parametrize("change", ["cue", "loudness", "setting", "play", "history"])
def test_cue_loudness_setting_and_play_writes_from_another_connection_do_not_reload(
    other_connection, monkeypatch, change
):
    manager, other, song = other_connection
    other.set_vector(song, np.zeros(FEATURE_DIM, dtype=np.float32))
    manager.tick()
    before = manager.sim.replaced
    reads = _count_reads(manager, monkeypatch)
    if change == "cue":
        other.set_points(song, {"intro": 12.0})
    elif change == "loudness":
        other.set_loudness(song, fake_loudness(song))
    elif change == "setting":
        other.set_setting("unrelated", True)
    elif change == "play":
        other.remember_play(song, 42.0)
    else:
        other.write_history(
            [
                {
                    "event_id": "external-play",
                    "path": str(song),
                    "title": "Song",
                    "artist": "Artist",
                    "duration": 12.0,
                    "played_at": "2026-01-01T00:00:00+00:00",
                }
            ]
        )

    manager.tick()

    assert reads == [0]
    assert manager.sim.replaced is before
    if change == "cue":
        assert manager.catalog.points(song)["intro"] == 12.0  # read live, not reloaded


def test_a_write_during_a_refresh_is_caught_on_the_next_tick(other_connection, monkeypatch):
    manager, other, _song = other_connection
    reader = manager.catalog.reader
    late = manager.cfg.library.music_dir / "during-refresh.flac"
    late.write_bytes(b"track")

    @contextmanager
    def reader_with_a_write_mid_read():
        with reader() as snapshot:
            snapshot.versions()  # the read transaction starts here
            other.put_entry(make_entry(late))  # commits after it
            yield snapshot

    monkeypatch.setattr(manager.catalog, "reader", reader_with_a_write_mid_read)
    manager.refresh()
    assert manager.entry_for_path(late) is None
    monkeypatch.setattr(manager.catalog, "reader", reader)
    manager.tick()
    assert manager.entry_for_path(late) is not None
