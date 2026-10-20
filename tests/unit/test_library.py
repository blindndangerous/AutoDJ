import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC
from starlette.responses import JSONResponse

from autodj.catalog import LibraryCatalog, _path
from autodj.config import (
    AutoDJConfig,
    HuggingFaceConfig,
    IndexConfig,
    LibraryConfig,
    ModelConfig,
    PlaybackConfig,
    StationSpec,
)
from autodj.indexer import FEATURE_DIM, FINGERPRINT_BYTES, IndexEntry, file_fingerprint
from autodj.library import LibraryManager, analyse_library
from autodj.station_store import StationStore
from autodj.stations import DEFAULT_SLUG
from tests.unit._catalog_vectors import set_vector, vectors_snapshot


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
    processing: str | None = None,
):
    music = root / "Music" if music is None else music
    music.mkdir(parents=True, exist_ok=True)
    return AutoDJConfig(
        library=LibraryConfig(music_dir=music, supported_formats=["flac"]),
        index=IndexConfig(index_dir=root / "index", model_dir=root / "models"),
        playback=PlaybackConfig(loudness_processing=processing),
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

    def create(liners=None, **kwargs):
        cfg = make_config(tmp_path, **kwargs)
        # The station's own liners folder, registered on a new library.
        ((cfg.index.stations_dir / "default") / "liners").mkdir(parents=True, exist_ok=True)
        manager = LibraryManager(cfg, Similarity())
        manager.catalog.add_source(cfg.library.music_dir)
        if liners is not None:
            manager.catalog.add_source(liners, "liner")
        managers.append(manager)
        return manager

    yield create
    for manager in managers:
        manager.close()


def fake_basic(path: Path, kind: str = "music", audio=None):
    del kind, audio
    return make_entry(path), {
        "start": 0.0,
        "intro": 10.0,
        "outro": 165.0,
        "segue": 160.0,
        "end": 179.0,
    }


def fake_loudness(path: Path, kind: str = "music") -> dict:
    from autodj.processing import LINER_PREPARATION_VERSION, PREPARATION_VERSION, TARGET_LUFS

    stat = path.stat()
    lufs = TARGET_LUFS[kind] - 2.0
    return {
        "version": PREPARATION_VERSION if kind == "music" else LINER_PREPARATION_VERSION,
        "kind": kind,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "lufs": lufs,
        "gain_db": 2.0,
    }


def test_new_library_does_not_register_legacy_music_root(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, music=tmp_path / "missing-music")
    sim = Similarity()
    manager = LibraryManager(cfg, sim)
    try:
        # Only the station's own liners folder is registered.
        assert {row["path"] for row in manager.catalog.sources()} == {
            os.path.normcase(str((cfg.index.stations_dir / "default") / "liners")),
        }
        assert sim.replaced[1].shape == (0, FEATURE_DIM)
        assert not (cfg.index.library_dir / "index-manifest.json").exists()
    finally:
        manager.close()


@pytest.mark.parametrize("legacy_root", [False, True])
def test_empty_library_requires_explicit_folder_and_keeps_it_after_restart(tmp_path, legacy_root):
    cfg = make_config(tmp_path)
    music = cfg.library.music_dir
    song = music / "song.flac"
    song.write_bytes(b"audio")
    if not legacy_root:
        cfg.library.music_dir = None
    manager = LibraryManager(cfg, Similarity())
    try:
        with (
            patch("autodj.library.analyze_basic", side_effect=fake_basic),
            patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
            patch("autodj.intro_lift.measure_source_intro", return_value=0.0),
        ):
            manager.scan()
            assert manager.catalog.sources("music") == []
            assert manager.entry_for_path(song) is None
            manager.catalog.add_source(music, advanced=False)
            manager.scan()
            assert manager.entry_for_path(song) is not None
    finally:
        manager.close()
    reopened = LibraryManager(cfg, Similarity())
    try:
        assert len(reopened.catalog.sources("music")) == 1
        assert reopened.entry_for_path(song) is not None
    finally:
        reopened.close()


def test_constructor_refuses_legacy_settings_in_another_station(tmp_path):
    from autodj.index_manifest import IndexConsistencyError
    from autodj.stations import StationPaths

    cfg = make_config(tmp_path)
    cfg.stations = [StationSpec(slug="secondary", autostart=False, cfg=cfg)]
    state = StationPaths.for_station(cfg, "secondary").web_state
    state.parent.mkdir(parents=True)
    original = '{"schema_version":1,"djmix":{"harmonic_mixing":false}}'
    state.write_text(original, encoding="utf-8")
    with pytest.raises(IndexConsistencyError, match="needs migration"):
        LibraryManager(cfg, Similarity())
    assert not (cfg.index.library_dir / "library.db").exists()
    assert state.read_text(encoding="utf-8") == original


def test_constructor_closes_catalog_when_refresh_fails(tmp_path):
    cfg = make_config(tmp_path)
    opened = []
    catalog_factory = LibraryCatalog

    def capture_catalog(path):
        catalog = catalog_factory(path)
        opened.append(catalog)
        return catalog

    with (
        patch("autodj.library.LibraryCatalog", side_effect=capture_catalog),
        patch.object(LibraryManager, "refresh", side_effect=ValueError("invalid catalog")),
        pytest.raises(ValueError, match="invalid catalog"),
    ):
        LibraryManager(cfg, Similarity())
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
        # New music is listed from its tags first.
        read_tags.assert_called_once_with(song)


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
    read_tags.assert_called_with(song)


def test_scan_continues_after_per_file_failure(manager_factory, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    bad, good = music / "bad.flac", music / "good.flac"
    bad.write_bytes(b"bad")
    good.write_bytes(b"good")
    manager = manager_factory(music=music)

    def analyze(path, kind="music", audio=None):
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
        patch("autodj.model_identity.model_analysis_available", return_value=True),
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
    assert captured["index_dir"] != (manager.cfg.index.stations_dir / "default")


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
        # Fake audio must not launch FFmpeg for unrelated intro analysis.
        patch("autodj.intro_lift.measure_source_intro", return_value=0.0),
    ):
        assert manager.start_scan()
        # Wait for completion; pytest's timeout guards against a stuck worker.
        manager._thread.join()
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

    def basic(path, kind="music", audio=None):
        events.append(("basic", path.name))
        if path == old_song:
            source_id = manager.catalog.add_source(new_folder)["id"]
            assert manager.start_scan({source_id})
        return fake_basic(path, kind)

    def measure(path, kind="music"):
        events.append(("loudness", path.name))
        return fake_loudness(path, kind)

    with (
        patch("autodj.library.analyze_basic", side_effect=basic),
        patch("autodj.library.analyze_loudness", side_effect=measure),
        patch("autodj.intro_lift.measure_source_intro", return_value=0.0),
    ):
        assert manager.start_scan()
        manager._thread.join()
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
        patch("autodj.intro_lift.measure_source_intro", return_value=0.0),
    ):
        assert manager.start_scan()
        manager._thread.join()
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
        patch("autodj.intro_lift.measure_source_intro", return_value=0.0),
    ):
        assert manager.start_scan()
        manager._thread.join()
        assert not manager._thread.is_alive()

    assert manager.entry_for_path(new_song) is not None
    assert manager._scan_generation == 2
    with (
        patch("autodj.model_identity.model_analysis_available", return_value=True),
        patch("autodj.jobs.get_manager", return_value=Jobs()),
    ):
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
    manager.catalog.put_entry(make_entry(song))
    manager.catalog.set_points(song, {"start": 0, "outro": 170, "end": 180}, source=point_source)
    set_vector(manager.catalog, song, np.ones(FEATURE_DIM, dtype=np.float32))

    song.write_bytes(b"replacement audio with different bytes and length")

    def short_track(path: Path, kind: str = "music", audio=None):
        del kind, audio
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
    manager.catalog.put_entry(original, points={"start": 0, "segue": 150, "end": 170})
    manager.catalog.set_loudness(old, fake_loudness(old))
    vector = np.ones(FEATURE_DIM, dtype=np.float32)
    set_vector(manager.catalog, old, vector)
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
    assert record["loudness"] == fake_loudness(new)
    assert manager.ledger.last_played()[os.path.normcase(str(new))] == 42.0
    assert analyze.call_count == 0
    measure.assert_not_called()
    np.testing.assert_array_equal(vectors_snapshot(manager.catalog)[1][0], vector)


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
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
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
    set_vector(manager.catalog, song, np.zeros(FEATURE_DIM, dtype=np.float32))
    assert manager.analysis_pending() == []


def test_basic_suggestions_include_legacy_intro_outro_without_overwriting_manual_points(
    manager_factory, tmp_path
):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"music")
    manager = manager_factory(music=music)
    manager.cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    manager.cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (manager.cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    (manager.cfg.index.library_dir / "dj_meta.db").write_bytes(b"placeholder")

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
    assert os.path.normcase(str(song)) not in manager.ledger.store.last_played()
    assert manager.ledger.last_played()[os.path.normcase(str(song))] == 99.5
    assert manager.ledger.recent_history() == []
    manager.tick()
    assert manager.ledger.store.last_played()[os.path.normcase(str(song))] == 99.5
    stored = manager.ledger.recent_history()
    assert len(stored) == 1
    assert (stored[0]["event_id"], stored[0]["title"], stored[0]["played_at"]) == (
        first["event_id"],
        first["title"],
        first["played_at"],
    )
    assert stored[0]["entry_id"] is None

    second = {**first, "event_id": "play-2", "title": "Interrupted"}
    manager.ledger.remember_history(second)
    manager.ledger.interrupt_history(second["event_id"])
    assert len(manager.ledger.recent_history()) == 1
    manager.close()

    reopened = StationStore(manager.cfg.index.stations_dir / "default" / "station.db", DEFAULT_SLUG)
    try:
        assert [row["event_id"] for row in reopened.recent_history(100)] == ["play-1"]
        rows = reopened._db.execute(
            "SELECT event_id,interrupted FROM play_history ORDER BY rowid"
        ).fetchall()
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
    write_history = manager.ledger.store.write_history
    enqueued = False

    def write_during_callback(rows, interrupted=()):
        nonlocal enqueued
        if not enqueued:
            enqueued = True
            manager.ledger.remember_history(second)
            manager.ledger.interrupt_history(first["event_id"])
        write_history(rows, interrupted)

    monkeypatch.setattr(manager.ledger.store, "write_history", write_during_callback)
    manager.tick()
    assert [row["event_id"] for row in manager.ledger.recent_history()] == ["play-1"]

    manager.tick()
    assert [row["event_id"] for row in manager.ledger.recent_history()] == ["play-2"]
    rows = manager.ledger.store._db.execute(
        "SELECT event_id,interrupted FROM play_history ORDER BY rowid"
    ).fetchall()
    assert [(row["event_id"], row["interrupted"]) for row in rows] == [
        ("play-1", 1),
        ("play-2", 0),
    ]


def test_runtime_settings_are_buffered_until_tick_and_close(manager_factory, tmp_path):
    manager = manager_factory()
    manager.ledger.remember_setting("clock", {"cursor": 3, "enabled": True})
    assert manager.ledger.store.get_setting("clock") is None
    manager.tick()
    assert manager.ledger.store.get_setting("clock") == {"cursor": 3, "enabled": True}

    manager.ledger.remember_setting("clock", {"cursor": 4, "enabled": True})
    manager.close()
    reopened = StationStore(manager.cfg.index.stations_dir / "default" / "station.db", DEFAULT_SLUG)
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
    initial.catalog.add_source(music)
    try:
        initial.catalog.put_entry(make_entry(song), points={"start": 0.0, "end": 170.0})
    finally:
        initial.close()

    vector = np.full(FEATURE_DIM, 0.5, dtype=np.float32)

    def embed(tracks, wrapper, workers, checkpoint, throttle_ms, report, gate=None):
        del wrapper, workers, throttle_ms, report
        advanced = make_entry(tracks[0].path)
        advanced.bpm, advanced.key, advanced.mode = 121.0, 7, 1
        advanced.size, advanced.fingerprint = file_fingerprint(advanced.path)
        checkpoint([advanced], [vector])
        return [advanced], [vector]

    with patch("autodj.library._embed_new_tracks", side_effect=embed):
        assert analyse_library(cfg, object()) == 1
    check = LibraryManager(cfg, Similarity())
    try:
        assert check.catalog.has_vector(song)
        assert check.entry_for_path(song).bpm == 120
        assert check.entry_for_path(song).key == 7
        assert not (cfg.index.library_dir / "index-manifest.json").exists()
    finally:
        check.close()


@pytest.mark.parametrize("change_file", [None, "size", "same_fingerprint"])
def test_analysis_keeps_tag_edits_and_user_cues_during_decode(tmp_path, change_file):
    cfg = make_config(tmp_path)
    song = cfg.library.music_dir / "song.flac"
    song.write_bytes(b"x" * (FINGERPRINT_BYTES * 2))
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        catalog.add_source(cfg.library.music_dir, advanced=True)
        catalog.put_entry(make_entry(song, title="Old title"), points={"start": 0, "end": 180})
        catalog.set_points(song, {"intro": 12.5}, source="user")

    def decode(path):
        with LibraryCatalog(cfg.index.library_dir / "library.db") as other:
            assert other.retag(path, {"title": "New title"})
        if change_file == "size":
            song.write_bytes(b"y" * 4100)
        elif change_file == "same_fingerprint":
            before = file_fingerprint(song)
            with song.open("r+b") as audio:
                audio.write(b"y")
            assert file_fingerprint(song) == before
            os.utime(song, ns=(song.stat().st_atime_ns, song.stat().st_mtime_ns + 1_000_000_000))
        return np.ones(16, np.float32), np.zeros(10, np.float32), 22050, {}

    from autodj.indexer import TrackAnalysis

    vector = np.zeros(FEATURE_DIM, np.float32)
    vector[0] = 1.0
    analysis = TrackAnalysis(vector, 0.3, 5, 1, 120.0, 0.9)
    with (
        patch("autodj.indexer._extract_librosa_features", side_effect=decode),
        patch("autodj.indexer.finish_analysis", return_value=analysis),
    ):
        # Size and fingerprint stand for the audio bytes; a change they do
        # not see (same_fingerprint) still stores the analysis, but the tag
        # edit and the user cue made during the decode are kept either way.
        stored = change_file != "size"
        assert analyse_library(cfg, wrapper=None) == (1 if stored else 0)

    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        record = catalog.record_for_path(song)
        assert record["title"] == "New title"
        assert record["points"]["intro"] == 12.5
        assert catalog.has_vector(song) is stored


def test_liner_source_takes_over_scanned_music(manager_factory, tmp_path):
    music = tmp_path / "Music"
    voice = music / "Voice"
    voice.mkdir(parents=True)
    clip = voice / "id.flac"
    clip.write_bytes(b"station id" * 50)
    manager = manager_factory(music=music)
    manager.set_dj_analysis_enabled(False)
    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
        patch("autodj.audio_meta.read_file_tags", side_effect=OSError),
    ):
        manager.scan()
        assert manager.catalog.record_for_path(clip)["kind"] == "music"
        manager.catalog.add_source(voice, kind="liner")
        manager.scan()
    assert manager.catalog.record_for_path(clip)["kind"] == "liner"
    assert any(entry.path == _path(clip) for entry in manager.entries_snapshot("liner"))


@pytest.mark.dj_analysis
@pytest.mark.parametrize("migrated", [False, True])
def test_rescan_reads_external_tag_edit_with_unchanged_size(manager_factory, tmp_path, migrated):
    from autodj.dj_meta import Cue, DjMeta

    song = tmp_path / "Music" / "song.flac"
    song.parent.mkdir()
    audio_samples = np.random.default_rng(1).uniform(-0.3, 0.3, (44100 * 5, 2))
    sf.write(song, audio_samples.astype(np.float32), 44100, format="FLAC")
    audio = FLAC(song)
    audio["title"] = "Old title"
    audio.save()
    manager = manager_factory(music=song.parent, processing="smooth")
    manager.set_dj_analysis_enabled(False)
    with patch("autodj.library.analyze_loudness", side_effect=fake_loudness):
        manager.scan()
        assert manager.entry_for_path(song).title == "Old title"
        manager.catalog.set_points(song, {"intro": 0.2}, source="user")
        manager.catalog.set_intro_lift(song, 2.0)
        vector = np.zeros(FEATURE_DIM, np.float32)
        vector[0] = 1
        set_vector(manager.catalog, song, vector)
        cue = Cue(0.3, type="user", label="My cue", source="user")
        cache = manager._dj_cache()
        cache.set(str(song), DjMeta(analysed=True, beats=[0.3], cues=[cue]))
        cache.flush(force=True)
        before = file_fingerprint(song)
        before_mtime = song.stat().st_mtime_ns
        if migrated:
            manager.catalog._db().execute(
                "UPDATE catalog_entries SET mtime_ns=0 WHERE path=?", (_path(song),)
            )
            manager.catalog._db().commit()
        else:
            audio = FLAC(song)
            audio["title"] = "Edited elsewhere"
            audio.save()
            os.utime(song, ns=(song.stat().st_atime_ns, before_mtime + 1_000_000_000))
        assert file_fingerprint(song) == before
        before_record = manager.catalog.record_for_path(song)
        manager.set_dj_analysis_enabled(True)
        with (
            patch("autodj.library.analyze_basic", side_effect=AssertionError("basic analysis")),
            patch("autodj.library.load_for_analysis", side_effect=AssertionError("DJ analysis")),
            patch("autodj.library.analyze_loudness", side_effect=AssertionError("loudness")),
        ):
            manager.scan()
    assert manager.entry_for_path(song).title == ("Old title" if migrated else "Edited elsewhere")
    assert manager.catalog.points(song)["intro"] == 0.2
    assert manager.catalog.has_vector(song)
    after_record = manager.catalog.record_for_path(song)
    assert after_record["mtime_ns"] == song.stat().st_mtime_ns
    assert after_record["loudness"]["mtime_ns"] == song.stat().st_mtime_ns
    assert after_record["intro_lift_db"] == before_record["intro_lift_db"]
    assert after_record["energy"] == before_record["energy"]
    assert cache.get(str(song)).cues == [cue]
    assert cache.get(str(song)).beats == [0.3]


def test_nonfinite_bpm_tags_do_not_drop_tracks_or_break_browse(manager_factory, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    audio_samples = np.random.default_rng(2).uniform(-0.3, 0.3, (44100, 2))
    for name, bpm in (("inf", "inf"), ("nan", "nan"), ("ok", "120")):
        song = music / f"{name}.flac"
        sf.write(song, audio_samples.astype(np.float32), 44100, format="FLAC")
        audio = FLAC(song)
        audio["title"] = name
        audio["bpm"] = bpm
        audio.save()
    manager = manager_factory(music=music)
    with patch("autodj.library.analyze_loudness", return_value={}):
        manager.scan()
    assert sorted(entry.title for entry in manager.entries_snapshot()) == ["inf", "nan", "ok"]
    JSONResponse(manager.browse_tracks())


@pytest.mark.skipif(os.name != "nt", reason="catalog case-folds paths on Windows")
@pytest.mark.dj_analysis
def test_dj_backlog_finds_imported_cues_for_case_folded_catalog_path(manager_factory, tmp_path):
    from autodj.dj_meta import DjMeta, close_cache

    music = tmp_path / "Music"
    music.mkdir()
    song = music / "My Song.flac"
    song.write_bytes(b"x" * 1000)
    url = "file://localhost/" + song.as_posix().replace(" ", "%20")
    (music / "Library.xml").write_text(
        "<DJ_PLAYLISTS><COLLECTION>"
        f'<TRACK Location="{url}"><POSITION_MARK Name="Drop" Type="0" Start="42.0"/></TRACK>'
        "</COLLECTION></DJ_PLAYLISTS>",
        encoding="utf-8",
    )
    manager = manager_factory(music=music)
    manager.catalog.add_source(music)
    manager.catalog.put_entry(make_entry(song), points={"start": 0, "end": 180})
    manager.set_dj_analysis_enabled(True)
    object.__setattr__(manager.cfg.playback, "import_external_cues", True)
    catalog_path = manager.catalog.all_entries("music")[0]["path"]
    try:
        with (
            patch("autodj.dj_cues_import._default_search_paths", return_value=[]),
            patch("autodj.dj_meta.analyse_audio", return_value=DjMeta(analysed=True)),
        ):
            manager._dj_analyse(Path(catalog_path), np.zeros(22050 * 5, np.float32))
        meta = manager._dj_cache().get(catalog_path)
        assert any(abs(cue.time_s - 42.0) < 0.01 for cue in meta.cues)
    finally:
        close_cache()


def test_scan_does_not_create_prepared_audio(manager_factory, tmp_path):
    music = tmp_path / "Music"
    song = music / "song.flac"
    music.mkdir()
    song.write_bytes(b"music")
    manager = manager_factory(music=music)
    cache = (manager.cfg.index.stations_dir / "default") / "prepared"
    cache.mkdir(parents=True)
    prepared = cache / "legacy.wav"
    prepared.write_bytes(b"existing prepared audio")
    manager.catalog.put_entry(make_entry(song))
    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
    ):
        manager.scan()

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
    set_vector(manager.catalog, song, np.ones(FEATURE_DIM, dtype=np.float32))
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
    assert manager.ledger.store.last_played()[os.path.normcase(str(song))] == 50.0

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
        set_vector(worker, song, vector)
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
    manager.catalog.add_source(music)
    try:
        manager.catalog.put_entry(make_entry(song, title="Old"))
        set_vector(manager.catalog, song, np.ones(FEATURE_DIM, dtype=np.float32))
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
        assert manager.is_gone(deleted)
        assert not manager.is_gone(remote)  # its share is only away
        assert not manager.is_gone(kept)
        moved.rename(share)
        manager.catalog.update_source(manager.catalog.sources("music")[-1]["id"], True, True)
        manager.tick()
    assert {Path(entry.path).name for entry in manager.entries_snapshot()} == {
        "kept.flac",
        "remote.flac",
    }


def test_missing_removal_requires_clean_preview_and_preserves_history(manager_factory, tmp_path):
    music, share = tmp_path / "Music", tmp_path / "Share"
    music.mkdir()
    share.mkdir()
    gone, kept, remote = music / "gone.flac", music / "kept.flac", share / "remote.flac"
    for path in (gone, kept, remote):
        path.write_bytes(path.name.encode())
    manager = manager_factory(music=music)
    manager.catalog.add_source(share)
    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
    ):
        manager.scan()
        manager.ledger.store.write_history(
            [
                {
                    "event_id": "gone-play",
                    "path": str(gone),
                    "title": "gone",
                    "artist": "artist",
                    "duration": 3.0,
                    "played_at": "2025-01-01T00:00:00+00:00",
                }
            ],
            [],
        )
        gone.unlink()
        moved = tmp_path / "Share-offline"
        share.rename(moved)
        manager.scan()
    preview = manager.missing_removal_preview()
    assert preview["ready"] is True
    assert preview["count"] == 1
    assert [item["title"] for item in preview["tracks"]] == ["gone"]
    manager.set_advanced_max_minutes(0)
    assert manager.missing_removal_preview()["token"] == preview["token"]
    with pytest.raises(ValueError, match="confirmation"):
        manager.remove_missing(preview["token"], confirmed=False)
    gone.write_bytes(b"returned")
    with pytest.raises(RuntimeError, match="changed"):
        manager.remove_missing(preview["token"], confirmed=True, allow_mass=True)
    gone.unlink()
    assert manager.remove_missing(preview["token"], confirmed=True, allow_mass=True) == 1
    assert manager.ledger.recent_history()[0]["path"] == os.path.normcase(str(gone))
    assert manager.catalog.record_for_path(remote) is not None


def test_missing_removal_rejects_an_empty_but_mounted_source(manager_factory, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    songs = [music / "one.flac", music / "two.flac"]
    for song in songs:
        song.write_bytes(song.name.encode())
    manager = manager_factory(music=music)
    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
    ):
        manager.scan()
        for song in songs:
            song.unlink()
        manager.scan()

    preview = manager.missing_removal_preview()
    assert preview["ready"] is True
    assert preview["count"] == 0
    assert all(manager.catalog.record_for_path(song) is not None for song in songs)


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
    set_vector(manager.catalog, song, np.ones(FEATURE_DIM, dtype=np.float32))
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
        patch("autodj.model_identity.model_analysis_available", return_value=True),
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


def test_catalog_vectors_replace_stale_similarity_without_seeding(tmp_path, monkeypatch):
    from autodj.similarity import SimilarityIndex

    cfg = make_config(tmp_path)
    songs = [cfg.library.music_dir / "a.flac", cfg.library.music_dir / "b.flac"]
    for song in songs:
        song.write_bytes(song.name.encode())
    entries = [make_entry(song) for song in songs]
    vectors = np.eye(2, FEATURE_DIM, dtype=np.float32)
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        catalog.add_source(cfg.library.music_dir)
        catalog.seed_entries(entries)
        catalog.seed_vectors(entries, vectors)

    def no_seeding(*_args, **_kwargs):
        raise AssertionError("re-imported")

    monkeypatch.setattr(LibraryCatalog, "seed_entries", no_seeding)
    sim = SimilarityIndex.empty()
    manager = LibraryManager(cfg, sim)
    try:
        assert [Path(entry.path) for entry in sim.entries_snapshot()] == songs
        np.testing.assert_array_equal(sim.faiss_index.reconstruct_n(0, 2), vectors)
    finally:
        manager.close()


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
        set_vector(other, song, np.zeros(FEATURE_DIM, dtype=np.float32))
    manager.tick()
    before = manager.sim.replaced
    vector = np.ones(FEATURE_DIM, dtype=np.float32)
    if change in {"vector_add", "vector_update"}:
        set_vector(other, song, vector)
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
    set_vector(other, song, np.zeros(FEATURE_DIM, dtype=np.float32))
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


def test_a_finished_scan_calls_its_tidy_up_and_survives_its_failure(
    manager_factory, tmp_path, caplog
):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "one.flac").write_bytes(b"one")
    manager = manager_factory(music=music)
    seen = []

    def tidy() -> None:
        seen.append(len(manager.entries_snapshot()))  # after the refresh
        raise OSError("disk gone")

    manager.on_scan_done = tidy
    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
        caplog.at_level(logging.ERROR, logger="autodj.library"),
    ):
        manager.scan()
    assert seen == [1]
    assert "after-scan tidy-up failed" in caplog.text
    assert manager._state["running"] is False


def test_a_folder_the_scan_cannot_read_marks_nothing_gone_and_skips_the_tidy_up(
    manager_factory, tmp_path, monkeypatch
):
    import autodj.library as library_module

    music = tmp_path / "Music"
    (music / "Sub").mkdir(parents=True)
    kept, deleted = music / "kept.flac", music / "Sub" / "deleted.flac"
    for path in (kept, deleted):
        path.write_bytes(path.name.encode())
    manager = manager_factory(music=music)
    tidied = []
    manager.on_scan_done = lambda: tidied.append(True)
    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
    ):
        manager.scan()
        assert tidied == [True]
        deleted.unlink()
        real_walk = os.walk

        def failing_walk(top, *args, onerror=None, **kwargs):
            # The subfolder cannot be read just now: its files are not seen.
            for current, directories, files in real_walk(top, *args, **kwargs):
                if "Sub" in directories:
                    directories.remove("Sub")
                    onerror(PermissionError(13, "Permission denied", str(Path(current, "Sub"))))
                yield current, directories, files

        monkeypatch.setattr(library_module.os, "walk", failing_walk)
        manager.scan()
    assert not manager.is_gone(deleted)  # not seen is not gone
    assert tidied == [True]  # no tidy-up after a scan that could not read a folder
    monkeypatch.setattr(library_module.os, "walk", real_walk)
    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
    ):
        manager.scan()
    assert manager.is_gone(deleted)
    assert tidied == [True, True]


def test_a_file_listed_but_not_resolvable_is_not_marked_gone(manager_factory, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    flapping = music / "flapping.flac"
    flapping.write_bytes(b"x")
    (music / "kept.flac").write_bytes(b"y")
    manager = manager_factory(music=music)
    tidied = []
    manager.on_scan_done = lambda: tidied.append(True)
    real_resolve = Path.resolve

    def flap(self: Path, strict: bool = False) -> Path:
        if self.name == "flapping.flac" and strict:
            raise OSError(64, "The specified network name is no longer available")
        return real_resolve(self, strict=strict)

    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
    ):
        manager.scan()
        with patch.object(Path, "resolve", flap), patch("os.path.isfile", return_value=False):
            manager.scan()
    assert not manager.is_gone(flapping)
    assert tidied == [True, True]


def test_a_scan_that_fails_part_way_runs_no_tidy_up(manager_factory, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "one.flac").write_bytes(b"one")
    manager = manager_factory(music=music)
    tidied = []
    manager.on_scan_done = lambda: tidied.append(True)

    def broken(*_args, **_kwargs):
        raise RuntimeError("catalog locked")

    with (
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
        patch.object(manager, "_reconcile_moves", broken),
        pytest.raises(RuntimeError),
    ):
        manager.scan()
    assert tidied == []


@pytest.mark.dj_analysis
def test_scan_finds_beats_from_the_same_decode_and_catches_up_older_songs(
    manager_factory, tmp_path
):
    from autodj.dj_meta import DjMeta, get_cache

    music = tmp_path / "Music"
    music.mkdir()
    old, new = music / "old.flac", music / "new.flac"
    old.write_bytes(b"old")
    manager = manager_factory(music=music)
    manager.cfg.playback.import_external_cues = False
    manager.catalog.put_entry(make_entry(old))
    manager.catalog.set_points(old, {"start": 0, "intro": 4.0, "outro": 170, "end": 179}, "user")
    new.write_bytes(b"new")
    decoded = np.zeros((44100 * 2, 2), dtype=np.float32)
    calls = []

    def analyse(audio, sr, intro_end_s=None, outro_start_s=None):
        calls.append((len(audio), intro_end_s, outro_start_s))
        return DjMeta(analysed=True)

    with (
        patch("autodj.library.load_for_analysis", return_value=decoded) as load,
        patch("autodj.library.analyze_basic", side_effect=fake_basic) as basic,
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
        patch("autodj.dj_meta.analyse_audio", side_effect=analyse),
    ):
        manager.scan()
        # The new song is decoded once, for its cue points and its beats.
        assert basic.call_args.kwargs["audio"] is decoded
        assert load.call_count == 2  # the new song, then the older one catching up
        # Beats and cues are placed from the saved Intro and Outro.
        assert (len(decoded), 10.0, 165.0) in calls
        assert (len(decoded), 4.0, 170) in calls
        cache = get_cache()
        assert cache.get(str(new)).analysed and cache.get(str(old)).analysed
        manager.scan()
        assert load.call_count == 2

        manager.set_dj_analysis_enabled(False)
        new.write_bytes(b"new bytes")
        manager.scan()
        assert basic.call_args.kwargs["audio"] is None
        assert load.call_count == 2


def test_new_songs_are_listed_from_their_tags_before_they_are_decoded(manager_factory, tmp_path):
    from autodj.audio_meta import FileTags

    music = tmp_path / "Music"
    music.mkdir()
    good, bad = music / "good.flac", music / "bad.flac"
    good.write_bytes(b"good")
    bad.write_bytes(b"bad")
    manager = manager_factory(music=music)
    listed = {}

    def basic(path, kind="music", audio=None):
        # Both songs are already listed, silent, when the first is decoded.
        for song in (good, bad):
            entry = manager.entry_for_path(song)
            listed[song.name] = entry is not None and entry.is_silent
        if path.name == "bad.flac":
            raise ValueError("not audio")
        return fake_basic(path, kind)

    with (
        patch(
            "autodj.audio_meta.read_file_tags", return_value=FileTags(title="Tagged", length=200.0)
        ),
        patch("autodj.library.analyze_basic", side_effect=basic),
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
    ):
        manager.scan()
    assert listed == {"good.flac": True, "bad.flac": True}
    assert not manager.entry_for_path(good).is_silent
    # A song that cannot be decoded leaves the library, as before.
    assert manager.catalog.record_for_path(bad) is None
    errors = manager.snapshot()["import"]["errors"]
    assert [error for error in errors if "Music" in error or "music" in error] == [
        next(error for error in errors if "bad.flac" in error)
    ]


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="FFmpeg is unavailable")
def test_a_song_longer_than_the_old_limit_is_imported_without_errors(
    manager_factory, tmp_path, monkeypatch
):
    """#391: a long mix is listed and analysed from its start and end, never refused."""
    from autodj import processing

    music = tmp_path / "Music"
    music.mkdir()
    mix = music / "mix.flac"
    t = np.arange(44100 * 4, dtype=np.float32) / 44100
    tone = 0.2 * np.sin(2 * np.pi * 440 * t)
    sf.write(mix, np.column_stack((tone, tone)), 44100)
    # Four seconds count as a long track here.
    monkeypatch.setattr(processing, "LONG_TRACK_SECONDS", 1.0)
    monkeypatch.setattr(processing, "ANALYSIS_WINDOW_SECONDS", 0.5)
    windows = []
    real_read = processing.read_windows
    monkeypatch.setattr(
        processing, "read_windows", lambda *a, **k: windows.append(a) or real_read(*a, **k)
    )
    manager = manager_factory(music=music)
    with (
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
        patch("autodj.intro_lift.measure_source_intro", return_value=0.0),
    ):
        manager.scan()
    snapshot = manager.snapshot()
    assert snapshot["import"]["errors"] == []
    assert windows, "the long song was read for its ends, not decoded whole"
    entry = manager.entry_for_path(mix)
    assert entry is not None and not entry.is_silent
    assert entry.length == pytest.approx(4.0, abs=0.01)
    assert manager.catalog.points(mix)["end"] == pytest.approx(4.0, abs=0.01)


def test_advanced_analysis_leaves_out_songs_over_the_length_cutoff(manager_factory, tmp_path):
    """Long songs are not queued, not errors, and come back when the cutoff allows."""
    music = tmp_path / "Music"
    music.mkdir()
    short, long = music / "song.flac", music / "mix.flac"
    short.write_bytes(b"song")
    long.write_bytes(b"mix")
    manager = manager_factory(music=music)
    for path, length in ((short, 180.0), (long, 62 * 60.0)):
        entry = make_entry(path)
        entry.length = length
        manager.catalog.put_entry(entry)
    catalog = manager.catalog
    assert catalog.advanced_max_minutes() == 15
    assert catalog.unembedded_paths() == [_path(short)]
    assert [r["path"] for r in manager.analysis_pending()] == [_path(short)]
    assert [r["path"] for r in manager.analysis_pending(force=True)] == [_path(short)]
    snapshot = manager.snapshot()
    assert snapshot["too_long_for_analysis"] == 1
    assert snapshot["advanced_max_minutes"] == 15
    assert snapshot["import"]["errors"] == []

    manager._analysis_idle = ("remembered",)
    generation = manager._scan_generation
    manager.set_advanced_max_minutes(0)
    # Looked for again at once, without waiting for a scan.
    assert manager._analysis_idle is None and manager._scan_generation == generation
    assert manager._analysis_started_generation is None
    assert sorted(catalog.unembedded_paths()) == sorted([_path(short), _path(long)])
    assert catalog.too_long_for_analysis_count() == 0
    manager.set_advanced_max_minutes(90)
    assert catalog.unembedded_count() == 2
    manager.set_advanced_max_minutes(60)
    assert catalog.unembedded_paths() == [_path(short)]
    # A bad stored value falls back to the default.
    catalog.set_setting("advanced_max_minutes", "lots")
    assert catalog.advanced_max_minutes() == 15


def test_library_snapshot_says_when_beets_songs_match_no_library_song(manager_factory, tmp_path):
    import sqlite3

    import autodj.beets_paths as beets_paths

    beets_paths._latest.clear()
    music = tmp_path / "Music"
    music.mkdir()
    manager = manager_factory(music=music)
    for name in ("one.flac", "two.flac", "three.flac"):
        song = music / "Artist" / name
        song.parent.mkdir(exist_ok=True)
        song.write_bytes(name.encode())
        manager.catalog.put_entry(make_entry(song))
    manager.refresh()
    db = tmp_path / "beets.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB)")
    conn.executemany(
        "INSERT INTO items (path) VALUES (?)",
        [(f"Artist/{name}".encode(),) for name in ("one.flac", "two.flac", "three.flac")],
    )
    conn.commit()
    conn.close()
    wrong = tmp_path / "Wrong"
    wrong.mkdir()
    manager.cfg.library.beets_db = db
    manager.cfg.library.music_dir = wrong
    try:
        view = manager.snapshot()["beets_paths"]
        assert view["mismatch"] is True
        assert f"[library] music_dir, which is {wrong}." in view["problem"]
        # The catalog keeps Windows paths case-folded, and so does the suggestion.
        assert f'set music_dir = "{music.as_posix()}"'.casefold() in view["fix"].casefold()

        manager.cfg.library.music_dir = music
        view = manager.snapshot()["beets_paths"]
        assert view["mismatch"] is False and view["problem"] is None
        assert view["summary"] == "3 of 3 library songs have a beets entry"
    finally:
        beets_paths._latest.clear()


def test_no_model_keeps_basic_tracks_and_remote_analysis_queue(
    manager_factory, tmp_path, monkeypatch
):
    monkeypatch.setenv("AUTODJ_NO_MODEL", "1")
    manager = manager_factory()
    song = manager.cfg.library.music_dir / "song.flac"
    song.write_bytes(b"music")
    manager.catalog.put_entry(make_entry(song), "music")
    manager.refresh()
    with patch("autodj.jobs.get_manager") as jobs:
        assert not manager.schedule_analysis()
        jobs.assert_not_called()
    assert manager.entry_for_path(song) is not None
    assert len(manager.analysis_pending()) == 1
    with patch.object(manager.analysis_queue, "worker_active", return_value=True):
        assert not manager.schedule_analysis()
    assert manager._yielded_to_worker
