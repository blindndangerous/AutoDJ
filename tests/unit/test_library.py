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
from autodj.library import LibraryManager, analyse_library, music_fade_drop_db
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

    def replace_catalog_faiss(self, entries, index):
        self.replaced = (entries, index.reconstruct_n(0, index.ntotal))

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


def fake_basic(path: Path, kind: str = "music", audio=None, **kwargs):
    del kind, audio, kwargs
    return make_entry(path), {
        "start": 0.0,
        "intro": 10.0,
        "outro": 165.0,
        "segue": 160.0,
        "end": 179.0,
    }


def tick_and_wait(manager):
    manager.tick()
    if manager._thread is not None:
        manager._thread.join(timeout=10)
        assert not manager._thread.is_alive()


def test_recovery_drains_backlog_in_one_run(abandoned_scan, monkeypatch):
    """ADR-0008-06, ADR-0008-22. A serial drain refreshes once without a clock advance."""
    manager, queue, now, songs = abandoned_scan
    for number in range(30):
        song = manager.cfg.library.music_dir / f"extra-{number:02}.flac"
        song.write_bytes(songs[0].read_bytes())
        songs.append(song)
    manager.scan()
    now[0] += 121
    refresh = Mock(wraps=manager.refresh)
    monkeypatch.setattr(manager, "refresh", refresh)
    tick_and_wait(manager)
    assert not queue.pending_scans()
    assert all(manager.catalog.record_for_path(song)["energy"] > 0 for song in songs)
    assert refresh.call_count == 1
    assert not manager.snapshot()["import"]["running"]


def test_recovery_refreshes_catalog_at_bounded_intervals(abandoned_scan, monkeypatch):
    """ADR-0008-22. Long drains publish busy state and refresh at bounded intervals."""
    manager, queue, now, songs = abandoned_scan
    now[0] += 121
    refreshed = []
    refresh = manager.refresh

    def reload():
        refreshed.append(now[0])
        return refresh()

    def decode(path, **kwargs):
        state = manager.snapshot()["import"]
        assert state["running"] and state["phase"] == "recovering analysis"
        assert state["current"] == str(path)
        now[0] += 31
        return fake_basic(path, **kwargs)

    monkeypatch.setattr(manager, "refresh", reload)
    monkeypatch.setattr("autodj.library.analyze_basic", decode)
    tick_and_wait(manager)
    assert not queue.pending_scans()
    assert refreshed == [1152.0, 1183.0, 1183.0]
    state = manager.snapshot()["import"]
    assert not state["running"] and not state["current"]
    assert state["done"] == len(songs)


def test_scan_decodes_locally_if_just_deferred_row_disappears(manager_factory, monkeypatch):
    manager = manager_factory()
    song = manager.cfg.library.music_dir / "new.flac"
    sf.write(song, np.ones((100, 2)) * 0.1, 22050)
    queue = manager.analysis_queue
    queue.seen("worker", "Worker", scan_decode=True)
    add = manager._add_from_tags

    def publish(path, **kwargs):
        queue.cancel_scan(_path(path))
        return add(path, **kwargs)

    monkeypatch.setattr(manager, "_add_from_tags", publish)
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: False)
    monkeypatch.setattr("autodj.library.analyze_basic", fake_basic)
    monkeypatch.setattr("autodj.intro_lift.measure_source_intro", lambda *args: 1.0)
    manager.scan()
    assert manager.catalog.record_for_path(song)["energy"] > 0
    assert not queue.scan_pending(_path(song))


@pytest.mark.parametrize("worker_connected", [False, True])
def test_touched_nonpending_song_preserves_dj_cues_with_invalid_points(
    manager_factory, monkeypatch, worker_connected
):
    from autodj.dj_meta import Cue, DjMeta

    manager = manager_factory()
    if worker_connected:
        manager.analysis_queue.seen("worker", "Worker", scan_decode=True)
    song = manager.cfg.library.music_dir / "song.flac"
    sf.write(song, np.ones((100, 2)) * 0.1, 22050)
    manager.catalog.put_entry(make_entry(song), "music")
    manager.catalog.set_intro_lift(song, 1.0)
    cache = manager._dj_cache()
    cue = Cue(0.3, type="user", label="Saved cue", source="user")
    cache.set(str(song), DjMeta(analysed=True, beats=[0.3], cues=[cue]))
    stat = song.stat()
    os.utime(song, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: True)
    decode = Mock(return_value=np.ones(22050, dtype=np.float32))
    monkeypatch.setattr("autodj.library.load_for_analysis", decode)
    monkeypatch.setattr("autodj.library.analyze_basic", fake_basic)
    manager.scan()
    decode.assert_not_called()
    assert not manager.analysis_queue.scan_pending(_path(song))
    assert cache.get(str(song)).cues == [cue]
    assert cache.get(str(song)).beats == [0.3]


@pytest.mark.parametrize("import_external", [False, True])
def test_remote_scan_preserves_saved_dj_cues(manager_factory, import_external):
    from autodj.dj_meta import Cue, DjMeta

    manager = manager_factory()
    manager.cfg.playback.import_external_cues = import_external
    song = manager.cfg.library.music_dir / "song.flac"
    cache = manager._dj_cache()
    cues = [Cue(1.0, source="user"), Cue(5.0, source="rekordbox")]
    cache.set(str(song), DjMeta(analysed=True, cues=cues))
    manager.store_scan_dj(song, DjMeta(analysed=True, beats=[2.0], cues=[Cue(1.0)]), force=True)
    assert cache.get(str(song)).cues == cues
    assert cache.get(str(song)).beats == [2.0]


@pytest.mark.dj_analysis
@pytest.mark.parametrize("flow", ["local", "worker"])
def test_scan_dj_keeps_current_imported_markers(manager_factory, monkeypatch, flow):
    from autodj.dj_cues_import import _normalise_keys
    from autodj.dj_meta import Cue, DjMeta

    manager = manager_factory()
    manager.cfg.playback.import_external_cues = True
    song = manager.cfg.library.music_dir / "My Song.flac"
    song.write_bytes(b"audio")
    entry, points = fake_basic(song)
    manager.catalog.put_entry(entry, points=points, point_source="analysis")
    before = manager.catalog.record_for_path(song)
    # Use the catalog's runtime path and the importers' normalized keys on both OSes.
    path = Path(before["path"])
    imported = [
        Cue(30.0, "intro_end", source="mixxx"),
        Cue(42.0, "hot", "Drop", "mixxx"),
        Cue(150.0, "outro_start", source="mixxx"),
    ]
    monkeypatch.setattr(
        "autodj.dj_cues_import.auto_import_cues",
        lambda **kwargs: _normalise_keys({song.as_posix(): imported}),
    )
    meta = DjMeta(
        analysed=True,
        intro_end_s=10.0,
        outro_start_s=165.0,
        beats=[2.0],
        cues=[Cue(10.0, "intro_end"), Cue(165.0, "outro_start")],
    )
    cache = manager._dj_cache()
    assert cache is not None
    assert manager.dj_analysis_enabled()
    if flow == "local":
        with patch("autodj.dj_meta.analyse_audio", return_value=meta):
            manager._dj_analyse(path, np.zeros(22050 * 180, np.float32))
    else:
        manager._refresh_scan_cues()
        manager.store_scan_dj(path, meta, force=True)

    stored = cache.get(str(path))
    assert stored.analysed
    assert stored.beats == [2.0]
    assert imported[1] in stored.cues
    assert stored.intro_end_s == 30.0
    assert stored.outro_start_s == 150.0
    assert [cue for cue in stored.cues if cue.source != "auto"] == imported
    after = manager.catalog.record_for_path(path)
    assert after["points"] == before["points"]
    assert after["point_sources"] == before["point_sources"]
    assert after["point_sources"]["intro"] == "analysis"
    assert after["point_sources"]["outro"] == "analysis"


@pytest.mark.dj_analysis
@pytest.mark.parametrize("flow", ["local", "worker"])
@pytest.mark.parametrize("source", ["user", "mixxx"])
@pytest.mark.parametrize("import_external", [True, False])
def test_scan_dj_cached_markers_follow_import_setting(
    manager_factory, monkeypatch, flow, source, import_external
):
    from autodj.dj_meta import Cue, DjMeta

    manager = manager_factory()
    manager.cfg.playback.import_external_cues = import_external
    song = manager.cfg.library.music_dir / "song.flac"
    song.write_bytes(b"old recording")
    cache = manager._dj_cache()
    assert cache is not None
    saved = [
        Cue(5.0, "intro_start", source=source),
        Cue(30.0, "intro_end", source=source),
        Cue(42.0, "hot", "Saved", source),
        Cue(150.0, "outro_start", source=source),
    ]
    cache.set(str(song), DjMeta(analysed=True, cues=saved))
    song.write_bytes(b"replacement recording")
    entry, points = fake_basic(song)
    manager.catalog.put_entry(entry, points=points, point_source="analysis")
    monkeypatch.setattr("autodj.dj_cues_import.auto_import_cues", lambda **kwargs: {})
    meta = DjMeta(
        analysed=True,
        intro_start_s=0.0,
        intro_end_s=10.0,
        outro_start_s=165.0,
        cues=[Cue(0.0, "intro_start"), Cue(10.0, "intro_end"), Cue(165.0, "outro_start")],
    )
    if flow == "local":
        with patch("autodj.dj_meta.analyse_audio", return_value=meta):
            manager._dj_analyse(song, np.zeros(22050 * 180, np.float32), force=True)
    else:
        manager._refresh_scan_cues()
        manager.store_scan_dj(song, meta, force=True)

    stored = cache.get(str(song))
    assert stored.analysed
    assert [cue for cue in stored.cues if cue.source != "auto"] == (
        [saved[2]] if import_external else saved
    )
    assert (stored.intro_start_s, stored.intro_end_s, stored.outro_start_s) == (
        (0.0, 10.0, 165.0) if import_external else (5.0, 30.0, 150.0)
    )


def test_remote_scan_before_snapshot_merges_imported_cues(manager_factory, monkeypatch):
    """ADR-0008-22. Results before snapshot loading receive imported cues afterward."""
    from autodj.dj_meta import Cue, DjMeta

    manager = manager_factory()
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: True)
    song = manager.cfg.library.music_dir / "song.flac"
    cue = Cue(5.0, source="rekordbox")
    monkeypatch.setattr(
        "autodj.dj_cues_import.auto_import_cues", lambda **kwargs: {str(song): [cue]}
    )
    manager.store_scan_dj(song, DjMeta(analysed=True, beats=[2.0]), force=True)
    assert manager._dj_cache().get(str(song)).analysed
    manager._refresh_scan_cues()
    assert manager._dj_cache().get(str(song)).cues == [cue]
    assert manager._dj_cache().get(str(song)).beats == [2.0]


def test_cue_only_refresh_leaves_import_state_idle(manager_factory, monkeypatch):
    """ADR-0008-22. Snapshot refreshes alone never publish a running import."""
    from autodj.dj_meta import DjMeta

    manager = manager_factory()
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: True)
    monkeypatch.setattr(manager, "schedule_analysis", lambda: False)
    before = manager.snapshot()["import"]
    states = []

    def import_cues(**kwargs):
        states.append(manager.snapshot()["import"])
        return {}

    monkeypatch.setattr("autodj.dj_cues_import.auto_import_cues", import_cues)
    manager.store_scan_dj(manager.cfg.library.music_dir / "song.flac", DjMeta(), force=True)
    tick_and_wait(manager)
    assert states == [before]
    assert manager.snapshot()["import"] == before


def test_recovery_resets_count_after_worker_handoff(abandoned_scan, monkeypatch):
    """ADR-0008-22. Each recovery run starts with its own remaining count."""
    manager, queue, now, songs = abandoned_scan
    now[0] += 121

    def decode(path, **kwargs):
        queue.seen("worker", "Worker", scan_decode=True)
        record = manager.catalog.record_for_path(songs[1])
        queue.claim("worker", [(record["path"], record["size"], record["fingerprint"])], 600)
        queue.release_path("worker", record["path"])
        return fake_basic(path, **kwargs)

    monkeypatch.setattr("autodj.library.analyze_basic", decode)
    tick_and_wait(manager)
    assert not manager._scan_fallback_running
    now[0] += 121
    states = []

    def decode_next(path, **kwargs):
        states.append(manager.snapshot()["import"])
        return fake_basic(path, **kwargs)

    monkeypatch.setattr("autodj.library.analyze_basic", decode_next)
    tick_and_wait(manager)
    assert states[0]["total"] == 1
    assert states[0]["done"] == 0


def test_changed_pending_song_stays_playable_until_fallback(manager_factory, monkeypatch):
    """ADR-0008-16. Pending bytes must not replace a playable record with silence."""
    from dataclasses import replace

    from autodj.audio_meta import read_file_tags

    manager = manager_factory()
    song = manager.cfg.library.music_dir / "song.flac"
    sf.write(song, np.ones((22050, 2)) * 0.1, 22050)
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: False)
    monkeypatch.setattr("autodj.library.analyze_basic", fake_basic)
    monkeypatch.setattr(
        "autodj.audio_meta.read_file_tags", lambda path: replace(read_file_tags(path), length=180.0)
    )
    monkeypatch.setattr("autodj.intro_lift.measure_source_intro", lambda *args: 1.0)
    manager.scan()
    set_vector(manager.catalog, song, np.ones(FEATURE_DIM, dtype=np.float32))
    before = manager.catalog.record_for_path(song)
    old_vectors = vectors_snapshot(manager.catalog)[1].copy()
    tags = FLAC(song)
    tags["title"] = "Updated title"
    tags.save()
    queue = manager.analysis_queue
    queue.seen("worker", "Worker", scan_decode=True)
    manager.scan()
    manager.scan()
    assert queue.scan_pending(_path(song))
    assert manager.catalog.record_for_path(song) == before
    assert manager.entries_snapshot()[0].energy > 0
    assert not manager.entry_for_path(song).is_silent
    np.testing.assert_array_equal(vectors_snapshot(manager.catalog)[1], old_vectors)
    queue.forget_worker("worker")
    with manager._scan_lock:
        assert manager._recover_scan_one(False)
    after = manager.catalog.record_for_path(song)
    assert (after["size"], after["fingerprint"]) == file_fingerprint(song)
    assert after["energy"] > 0 and after["points"] == fake_basic(song)[1]
    assert not queue.scan_pending(_path(song))


def test_retag_changed_pending_song_still_decodes_locally(manager_factory, monkeypatch):
    """ADR-0008-16. Tag edits cannot make unmeasured replacement bytes look analysed."""
    manager = manager_factory()
    song = manager.cfg.library.music_dir / "song.flac"
    song.write_bytes(b"original recording")
    manager.catalog.put_entry(make_entry(song), points=fake_basic(song)[1])
    manager.catalog.set_intro_lift(song, 1.0)
    set_vector(manager.catalog, song, np.ones(FEATURE_DIM, dtype=np.float32))
    before = manager.catalog.record_for_path(song)
    song.write_bytes(b"replacement recording")
    queue = manager.analysis_queue
    queue.defer_scan(_path(song), *file_fingerprint(song))
    song.write_bytes(b"replacement recording with edited tags")
    assert manager.catalog.retag(song, {"title": "Edited replacement"})
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: False)
    monkeypatch.setattr("autodj.intro_lift.measure_source_intro", lambda *args: 1.0)

    def decode(path, **kwargs):
        entry, points = fake_basic(path, **kwargs)
        entry.title = "Edited replacement"
        entry.length = 240.0
        points["end"] = 239.0
        return entry, points

    monkeypatch.setattr("autodj.library.analyze_basic", decode)
    with manager._scan_lock:
        assert manager._recover_scan_one(False)
    after = manager.catalog.record_for_path(song)
    assert after["length"] == 240.0
    assert after["points"]["end"] == 239.0
    assert after["title"] == "Edited replacement"
    assert (after["size"], after["fingerprint"]) == file_fingerprint(song)
    assert after["fingerprint"] != before["fingerprint"]
    assert not vectors_snapshot(manager.catalog)[0]
    assert not queue.scan_pending(_path(song))


def test_pending_tag_refresh_keeps_edits_before_local_decode(manager_factory, monkeypatch):
    """ADR-0008-23. A tag estimate must not discard edits before the fallback measures length."""
    from autodj.audio_meta import FileTags

    manager = manager_factory()
    song = manager.cfg.library.music_dir / "song.flac"
    song.write_bytes(b"original recording")
    manager.catalog.put_entry(make_entry(song), points={"start": 0.0, "end": 180.0})
    song.write_bytes(b"replacement recording")
    queue = manager.analysis_queue
    queue.defer_scan(_path(song), *file_fingerprint(song))
    manager.catalog.set_points(song, {"intro": 150.0})
    manager.catalog.retag(song, {"bpm": 133.0, "key": 7, "mode": 1})
    monkeypatch.setattr("autodj.audio_meta.read_file_tags", lambda p: FileTags(length=120.0))
    monkeypatch.setattr("autodj.library.analyze_basic", fake_basic)
    monkeypatch.setattr("autodj.intro_lift.measure_source_intro", lambda *a: 1.0)
    with manager._scan_lock:
        assert manager._recover_scan_one(False)
    after = manager.catalog.record_for_path(song)
    assert after["points"]["intro"] == 150.0
    assert (after["bpm"], after["key"], after["mode"]) == (133.0, 7, 1)


@pytest.mark.parametrize("already_pending", [False, True])
@pytest.mark.parametrize(
    ("old_length", "tag_length", "silent"),
    [
        (180.0, 240.0, True),
        (180.0, 120.0, True),
        (180.0, 0.0, False),
        (180.0, 183.6, False),
        (50.0, 52.0, False),
        (50.0, 52.1, True),
    ],
)
def test_changed_pending_duration_controls_playability(
    manager_factory, monkeypatch, already_pending, old_length, tag_length, silent
):
    """ADR-0008-16. Only a known duration outside tolerance silences a replacement."""
    from autodj.audio_meta import FileTags

    manager = manager_factory()
    song = manager.cfg.library.music_dir / "song.flac"
    song.write_bytes(b"original recording")
    entry = make_entry(song)
    entry.length = old_length
    manager.catalog.put_entry(entry, points={"start": 0.0, "end": old_length})
    manager.catalog.set_intro_lift(song, 1.0)
    set_vector(manager.catalog, song, np.ones(FEATURE_DIM, dtype=np.float32))
    before = manager.catalog.record_for_path(song)
    queue = manager.analysis_queue
    queue.seen("worker", "Worker", scan_decode=True)
    if already_pending:
        queue.defer_scan(_path(song), *file_fingerprint(song))
    song.write_bytes(b"replacement recording")
    monkeypatch.setattr(
        "autodj.audio_meta.read_file_tags", lambda path: FileTags(length=tag_length)
    )
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: False)
    manager.scan()
    after = manager.catalog.record_for_path(song)
    assert queue.scan_pending(_path(song))
    assert manager.entry_for_path(song).is_silent is silent
    if silent:
        assert after["length"] == tag_length
        assert after["points"] == {}
        assert not vectors_snapshot(manager.catalog)[0]
    else:
        assert after == before
        assert vectors_snapshot(manager.catalog)[0]


@pytest.mark.parametrize("recover", [False, True])
def test_pending_decode_rewrite_discards_measurements(manager_factory, monkeypatch, recover):
    """ADR-0008-19. A successful decode cannot publish measurements for replaced bytes."""
    manager = manager_factory()
    song = manager.cfg.library.music_dir / "song.flac"
    song.write_bytes(b"original recording")
    manager.catalog.put_entry(make_entry(song), points=fake_basic(song)[1])
    manager.catalog.set_intro_lift(song, 1.0)
    set_vector(manager.catalog, song, np.ones(FEATURE_DIM, dtype=np.float32))
    before = manager.catalog.record_for_path(song)
    vectors = vectors_snapshot(manager.catalog)[1].copy()
    song.write_bytes(b"replacement recording")
    queue = manager.analysis_queue
    now = [1000.0]
    queue._now = lambda: now[0]
    queue.defer_scan(_path(song), *file_fingerprint(song))
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: False)
    monkeypatch.setattr("autodj.intro_lift.measure_source_intro", lambda *args: 1.0)

    def decode(path, **kwargs):
        path.write_bytes(b"rewritten during decode")
        return fake_basic(path, **kwargs)

    monkeypatch.setattr("autodj.library.analyze_basic", decode)
    assert not manager._read_track("music", song, False, recover_scan=recover)
    assert manager.catalog.record_for_path(song) == before
    np.testing.assert_array_equal(vectors_snapshot(manager.catalog)[1], vectors)
    assert queue.scan_pending(_path(song))
    assert queue.scan_identity(_path(song)) == file_fingerprint(song)
    now[0] += 60
    monkeypatch.setattr("autodj.library.analyze_basic", fake_basic)
    assert manager._read_track("music", song, False, recover_scan=recover)
    assert not queue.scan_pending(_path(song))
    after = manager.catalog.record_for_path(song)
    assert (after["size"], after["fingerprint"]) == file_fingerprint(song)


def test_pending_decode_rewrites_back_off_and_exhaust_budget(abandoned_scan, monkeypatch, caplog):
    """ADR-0008-11. Growing files cannot monopolize recovery or reset its retry budget."""
    manager, queue, now, songs = abandoned_scan
    now[0] += 121
    decoded = []

    def decode(path, **kwargs):
        decoded.append(path)
        if path == songs[0]:
            path.write_bytes(b"growing recording " * len(decoded))
        return fake_basic(path, **kwargs)

    monkeypatch.setattr("autodj.library.analyze_basic", decode)
    with manager._scan_lock:
        assert manager._recover_scan_one(False)
        assert queue.scan_identity(_path(songs[0])) == file_fingerprint(songs[0])
        assert queue.abandoned_scans(worker_active=False) == [_path(songs[1])]
        assert manager._recover_scan_one(False)
        assert not manager._recover_scan_one(False)
        assert decoded == songs
        assert manager.catalog.record_for_path(songs[1])["energy"] > 0
        for _ in range(2):
            now[0] += 59
            assert not manager._recover_scan_one(False)
            now[0] += 1
            assert manager._recover_scan_one(False)
            assert not manager._recover_scan_one(False)
        now[0] += 86400
        assert not manager._recover_scan_one(False)
    assert decoded == [songs[0], songs[1], songs[0], songs[0]]
    assert queue.pending_scans() == {_path(songs[0])}
    assert sum("Scan retries exhausted" in record.message for record in caplog.records) == 1


@pytest.mark.parametrize("new_listing", [False, True])
def test_retag_tag_only_pending_tracks_bytes_and_listing_origin(
    manager_factory, monkeypatch, new_listing
):
    """ADR-0008-16. Pending tag edits preserve claim identity and retirement provenance."""
    from autodj.audio_meta import FileTags
    from autodj.stereo import UnplayableAudioError

    manager = manager_factory()
    song = manager.cfg.library.music_dir / "song.flac"
    song.write_bytes(b"original recording")
    if not new_listing:
        manager.catalog.put_entry(make_entry(song), points=fake_basic(song)[1])
        song.write_bytes(b"replacement recording")
    queue = manager.analysis_queue
    queue.seen("worker", "Worker", scan_decode=True)
    monkeypatch.setattr("autodj.audio_meta.read_file_tags", lambda path: FileTags(length=240.0))
    manager.scan()
    before = manager.catalog.record_for_path(song)
    assert before["energy"] == 0 and not before["points"]
    assert queue.scan_identity(_path(song)) == (before["size"], before["fingerprint"])
    song.write_bytes(b"recording with edited tags")
    assert manager.catalog.retag(song, {"title": "Edited"})
    after = manager.catalog.record_for_path(song)
    identity = file_fingerprint(song)
    assert queue.scan_identity(_path(song)) == identity
    assert (after["size"], after["fingerprint"]) == identity
    assert queue.scan_created_listing(_path(song), *identity) is new_listing
    queue.forget_worker("worker")
    monkeypatch.setattr(
        "autodj.library.analyze_basic", Mock(side_effect=UnplayableAudioError("invalid audio"))
    )
    with manager._scan_lock:
        assert manager._recover_scan_one(False)
    assert (manager.catalog.record_for_path(song) is None) is new_listing
    assert queue.scan_pending(_path(song)) is not new_listing


@pytest.mark.parametrize("reason", ["worker", "stop", "scan"])
def test_recovery_yields_between_candidates(abandoned_scan, monkeypatch, reason):
    """ADR-0008-09. Recovery yields its slot between songs when ownership changes."""
    manager, queue, now, songs = abandoned_scan
    now[0] += 121
    decoded = []
    scanned = []

    def decode(path, **kwargs):
        decoded.append(path)
        if reason == "worker":
            queue.seen("worker", "Worker", scan_decode=True)
            record = manager.catalog.record_for_path(songs[1])
            assert queue.claim(
                "worker", [(record["path"], record["size"], record["fingerprint"])], 600
            )
            queue.release_path("worker", record["path"])
            queue.handoff_scan(record["path"])
        elif reason == "stop":
            manager._stop.set()
        else:
            manager.start_scan()
        return fake_basic(path, **kwargs)

    monkeypatch.setattr("autodj.library.analyze_basic", decode)
    monkeypatch.setattr(manager, "_scan", lambda *args: scanned.append(True))
    tick_and_wait(manager)
    assert decoded == [songs[0]]
    assert queue.pending_scans() == {_path(songs[1])}
    assert scanned == ([True] if reason == "scan" else [])
    assert not manager._scan_lock.locked()


@pytest.mark.parametrize("older", [False, True])
@pytest.mark.parametrize("failure", ["ffmpeg", "timeout", "decoder", "analysis", "mixed"])
def test_exhausted_scan_reports_failure_and_retires_only_new_listing(
    abandoned_scan, monkeypatch, older, failure
):
    """ADR-0008-19. Exhausted non-access failures retire new listings, preserving history."""
    from autodj.stereo import decode_error

    manager, queue, now, songs = abandoned_scan
    song = songs[0]
    queue.cancel_scan(_path(songs[1]))
    if older:
        with queue._write() as db:
            db.execute("UPDATE scan_pending SET new_listing=0")
        manager.ledger.remember_play(song, 99.5)
    before = manager.catalog.record_for_path(song)
    failures = {
        "ffmpeg": RuntimeError("FFmpeg is required to decode"),
        "timeout": TimeoutError("decode timed out"),
        "decoder": decode_error(song, "Decoder not found"),
        "analysis": ValueError("analysis failed"),
        "mixed": ValueError("analysis failed"),
    }
    monkeypatch.setattr("autodj.library.analyze_basic", Mock(side_effect=failures[failure]))
    now[0] += 121
    for attempt in range(3):
        if failure == "mixed" and attempt:
            monkeypatch.setattr(
                "autodj.library.analyze_basic", Mock(side_effect=PermissionError("denied"))
            )
        manager._import_errors.clear()
        tick_and_wait(manager)
        assert any(song.name in error for error in manager.snapshot()["import"]["errors"])
        if attempt < 2:
            assert manager.catalog.record_for_path(song) == before
        now[0] += 21601
    if older:
        assert manager.catalog.record_for_path(song) == before
        assert manager.ledger.store.last_played()[_path(song)] == 99.5
    else:
        assert manager.catalog.record_for_path(song) is None
        assert not queue.scan_pending(_path(song))
    assert song.is_file()


@pytest.mark.parametrize("worker_returns", [False, True])
@pytest.mark.parametrize("failure", ["permission", "io", "missing", "resource"])
def test_access_only_exhaustion_preserves_listing_and_next_scan_retries(
    abandoned_scan, monkeypatch, failure, worker_returns
):
    """ADR-0008-19. Access-only exhaustion preserves the entry and permits a later scan."""
    from autodj.stereo import decode_error

    manager, queue, now, songs = abandoned_scan
    song = songs[0]
    queue.cancel_scan(_path(songs[1]))
    before = manager.catalog.record_for_path(song)
    error = {
        "permission": PermissionError("denied"),
        "io": decode_error(song, "Input/output error"),
        "missing": FileNotFoundError("missing input"),
        "resource": decode_error(song, "Resource temporarily unavailable"),
    }[failure]
    monkeypatch.setattr("autodj.library.analyze_basic", Mock(side_effect=error))
    now[0] += 121
    for _ in range(3):
        tick_and_wait(manager)
        now[0] += 21601
    assert manager.catalog.record_for_path(song) == before
    assert queue.scan_pending(_path(song))
    monkeypatch.setattr("autodj.library.analyze_basic", fake_basic)
    if worker_returns:
        queue.seen("worker", "Worker", scan_decode=True)
        other = manager.catalog.record_for_path(songs[1])
        assert queue.claim("worker", [(other["path"], other["size"], other["fingerprint"])], 600)
    manager.scan()
    if worker_returns:
        assert _path(song) in queue.claimable_scans(limit=10)
        assert queue.claim("worker", [(before["path"], before["size"], before["fingerprint"])], 600)
    else:
        assert not queue.scan_pending(_path(song))
        assert manager.catalog.record_for_path(song)["energy"] > 0


@pytest.mark.parametrize("attempts", [1, 3])
@pytest.mark.parametrize("expired", [False, True])
def test_scan_without_worker_retries_pending_holds(abandoned_scan, attempts, expired):
    """ADR-0008-19. A local scan must not silently skip deferred retry holds."""
    manager, queue, now, songs = abandoned_scan
    now[0] += 121
    with queue._write() as db:
        db.execute(
            "UPDATE scan_pending SET attempts=?, retry_at=?",
            (attempts, now[0] - 1 if expired else now[0] + 21600),
        )
    manager.scan()
    assert not queue.pending_scans()
    assert all(manager.catalog.record_for_path(song)["energy"] > 0 for song in songs)


@pytest.mark.parametrize("changed", [False, True])
def test_exhausted_scan_preserves_disappeared_or_changed_file(abandoned_scan, monkeypatch, changed):
    """ADR-0008-19. Exhaustion cannot retire a listing after its input changes or disappears."""
    manager, queue, now, songs = abandoned_scan
    song = songs[0]
    queue.cancel_scan(_path(songs[1]))
    before = manager.catalog.record_for_path(song)
    with queue._write() as db:
        db.execute("UPDATE scan_pending SET attempts=2")

    def fail(path, **kwargs):
        if changed:
            path.write_bytes(b"changed audio")
        else:
            path.unlink()
        raise ValueError("analysis failed")

    monkeypatch.setattr("autodj.library.analyze_basic", fail)
    now[0] += 121
    tick_and_wait(manager)
    assert manager.catalog.record_for_path(song) == before
    assert queue.scan_pending(_path(song))


@pytest.mark.parametrize("state", ["exhausted", "backoff", "held", "busy"])
def test_tick_starts_no_recovery_without_recoverable_scan(abandoned_scan, monkeypatch, state):
    """ADR-0008-08. Ineligible pending rows never take the scan slot across idle ticks."""
    from autodj.analysis_queue import SCAN_MAX_ATTEMPTS

    manager, queue, now, songs = abandoned_scan
    if state == "exhausted":
        with queue._write() as db:
            db.execute("UPDATE scan_pending SET attempts=?", (SCAN_MAX_ATTEMPTS,))
    elif state == "backoff":
        with queue._write() as db:
            db.execute("UPDATE scan_pending SET retry_at=?", (now[0] + 3600,))
    else:
        for song in songs if state == "held" else songs[:1]:
            record = manager.catalog.record_for_path(song)
            queue.claim("worker", [(record["path"], record["size"], record["fingerprint"])], 3600)
    now[0] += 601
    if state == "busy":
        queue.seen("worker", "Worker", scan_decode=True)
        queue.release_path("worker", _path(songs[0]))
        record = manager.catalog.record_for_path(songs[0])
        queue.claim("worker", [(record["path"], record["size"], record["fingerprint"])], 3600)
    acquire = Mock(wraps=manager._scan_lock)
    monkeypatch.setattr(manager, "_scan_lock", acquire)
    start = Mock(side_effect=AssertionError("unexpected recovery thread"))
    monkeypatch.setattr("autodj.library.threading.Thread.start", start)
    cues = Mock()
    monkeypatch.setattr(manager, "_refresh_scan_cues", cues)
    for _ in range(20):
        manager.tick()
        assert not manager._scan_lock.locked()
    monkeypatch.setattr(manager, "_scan_lock", acquire._mock_wraps)
    start.assert_not_called()
    acquire.acquire.assert_not_called()
    cues.assert_not_called()


def test_tick_checks_auto_rescan_before_recovery(abandoned_scan, monkeypatch):
    manager, _queue, now, _songs = abandoned_scan
    now[0] += 121
    manager._auto_rescan_minutes = 1
    manager._last_scan_end = manager._clock() - 61
    events = []
    monkeypatch.setattr(manager, "start_scan", lambda **kwargs: events.append("rescan"))
    monkeypatch.setattr(manager, "_take_abandoned_scan", lambda: events.append("recovery"))
    manager.tick()
    assert events == ["rescan", "recovery"]


def test_scan_recovers_abandoned_work_between_files(abandoned_scan, monkeypatch):
    """ADR-0008-09. A long scan recovers earlier deferrals before its final file."""
    manager, queue, now, songs = abandoned_scan
    clock = [manager._clock()]
    manager._clock = lambda: clock[0]
    read = manager._read_track
    seen = []

    def visit(kind, path, override_tags, **kwargs):
        if not kwargs.get("recover_scan"):
            seen.append(path)
            if len(seen) == 2:
                assert manager.catalog.record_for_path(songs[0])["energy"] > 0
        result = read(kind, path, override_tags, **kwargs)
        if not kwargs.get("recover_scan") and len(seen) == 1:
            now[0] += 121
            clock[0] += 6
        return result

    monkeypatch.setattr(manager, "_read_track", visit)
    manager.scan()
    assert not queue.pending_scans()


def test_tick_does_not_wait_for_scan_decode(abandoned_scan, monkeypatch):
    """ADR-0008-12. Library maintenance continues while recovery decodes."""
    manager, _queue, now, _songs = abandoned_scan
    now[0] += 121
    entered = threading.Event()
    release = threading.Event()

    def decode(path, **kwargs):
        entered.set()
        assert release.wait(5)
        return fake_basic(path, **kwargs)

    monkeypatch.setattr("autodj.library.analyze_basic", decode)
    caller = threading.Thread(target=manager.tick)
    caller.start()
    try:
        assert entered.wait(5)
        caller.join(timeout=1)
        assert not caller.is_alive()
        manager.tick()
    finally:
        release.set()
        caller.join(timeout=5)
        if manager._thread is not None:
            manager._thread.join(timeout=5)


def test_tick_survives_scan_queue_error(manager_factory, monkeypatch, caplog):
    """ADR-0008-12. Queue failures cannot end periodic library maintenance."""
    import sqlite3

    manager = manager_factory()
    scheduled = []
    monkeypatch.setattr(manager, "schedule_analysis", lambda: scheduled.append(True))
    monkeypatch.setattr(
        manager.analysis_queue,
        "abandoned_scans",
        Mock(side_effect=sqlite3.OperationalError("database is locked")),
    )
    with caplog.at_level(logging.WARNING):
        manager.tick()
        manager.tick()
    assert scheduled == [True, True]
    assert sum("Scan recovery unavailable" in r.message for r in caplog.records) == 1


def test_deferred_scan_loudness_failure_is_nonfatal(abandoned_scan, monkeypatch):
    manager, queue, now, songs = abandoned_scan
    now[0] += 121
    monkeypatch.setattr("autodj.library.analyze_loudness", Mock(side_effect=ValueError("silent")))
    tick_and_wait(manager)
    assert not queue.scan_pending(_path(songs[0]))
    assert manager.catalog.record_for_path(songs[0])["points"]["end"] == 179.0


def test_scan_removes_corrupt_deferred_file(manager_factory, monkeypatch):
    """ADR-0008-19. A real decoder rejection retires only the new tag listing and scan."""
    manager = manager_factory()
    song = manager.cfg.library.music_dir / "broken.flac"
    song.write_bytes(b"not an audio file")
    queue = manager.analysis_queue
    now = [1000.0]
    queue._now = lambda: now[0]
    queue.seen("worker", "Worker", scan_decode=True)
    monkeypatch.setattr(manager, "schedule_analysis", lambda: False)
    manager.scan()
    assert manager.catalog.record_for_path(song) is not None
    assert queue.scan_pending(_path(song))
    now[0] += 121
    tick_and_wait(manager)
    assert manager.catalog.record_for_path(song) is None
    assert not queue.scan_pending(_path(song))
    assert song.read_bytes() == b"not an audio file"


@pytest.mark.parametrize("failure", ["missing", "open", "stat", "unknown"])
@pytest.mark.parametrize("background", [True, False])
def test_pending_scan_preserves_unreadable_file(abandoned_scan, monkeypatch, failure, background):
    """ADR-0008-19. Unavailable bytes retain catalog identity and receive retry backoff."""
    manager, queue, now, songs = abandoned_scan
    from autodj.processing import analyze_basic

    monkeypatch.setattr("autodj.library.analyze_basic", analyze_basic)
    song = songs[0]
    before = manager.catalog.record_for_path(song)
    queue.cancel_scan(_path(songs[1]))
    if failure == "missing":
        song.rename(song.with_suffix(".offline"))
    elif failure in {"open", "stat"}:
        original = getattr(Path, failure)

        def unavailable(path, *args, **kwargs):
            if _path(path) == _path(song):
                raise OSError("file unavailable")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, failure, unavailable)
    else:
        monkeypatch.setattr("autodj.library.file_fingerprint", lambda path: (0, ""))
    refresh = Mock(wraps=queue.refresh_scan)
    reserve = Mock(wraps=queue.claim_scan)
    monkeypatch.setattr(queue, "refresh_scan", refresh)
    monkeypatch.setattr(queue, "claim_scan", reserve)
    now[0] += 121
    for _ in range(2):
        if background:
            tick_and_wait(manager)
        else:
            manager._read_track("music", song, False)
    assert manager.catalog.record_for_path(song) == before
    assert queue._read(
        "SELECT size,fingerprint,retry_at FROM scan_pending WHERE path=?", (_path(song),)
    ) == [(before["size"], before["fingerprint"], now[0] + 21600)]
    refresh.assert_not_called()
    reserve.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [OSError("unavailable"), RuntimeError("decoder unavailable"), ValueError("analysis failed")],
)
def test_pending_scan_keeps_transient_decode_failures(abandoned_scan, monkeypatch, failure):
    """ADR-0008-19. An arbitrary analysis exception is not proof of corrupt audio."""
    manager, queue, now, songs = abandoned_scan
    before = manager.catalog.record_for_path(songs[0])
    monkeypatch.setattr("autodj.library.analyze_basic", Mock(side_effect=failure))
    now[0] += 121
    tick_and_wait(manager)
    assert manager.catalog.record_for_path(songs[0]) == before
    assert queue._read(
        "SELECT attempts,retry_at FROM scan_pending WHERE path=?", (_path(songs[0]),)
    ) == [(1, now[0] + 21600)]


@pytest.mark.parametrize("changed", [False, True])
def test_pending_scan_preserves_file_changed_during_decode(abandoned_scan, monkeypatch, changed):
    """ADR-0008-19. Decoder rejection cannot delete a disappeared or replaced input."""
    from autodj.stereo import UnplayableAudioError

    manager, queue, now, songs = abandoned_scan
    before = manager.catalog.record_for_path(songs[0])

    def reject(path, **kwargs):
        if changed:
            path.write_bytes(b"replacement")
        else:
            path.unlink()
        raise UnplayableAudioError("invalid audio")

    monkeypatch.setattr("autodj.library.analyze_basic", reject)
    now[0] += 121
    tick_and_wait(manager)
    assert manager.catalog.record_for_path(songs[0]) == before
    assert queue.scan_pending(_path(songs[0]))


def test_pending_scan_preserves_decoder_io_failure(abandoned_scan, monkeypatch):
    """ADR-0008-19. FFmpeg I/O errors do not prove readable bytes are corrupt."""
    from autodj.processing import read_windows

    manager, queue, now, songs = abandoned_scan
    before = manager.catalog.record_for_path(songs[0])
    monkeypatch.setattr("autodj.processing._pipe_ffmpeg", lambda *args: (1, b"Input/output error"))
    monkeypatch.setattr("autodj.library.analyze_basic", lambda path, **kwargs: read_windows(path))
    now[0] += 121
    tick_and_wait(manager)
    assert manager.catalog.record_for_path(songs[0]) == before
    assert queue.scan_pending(_path(songs[0]))


def test_pending_scan_preserves_older_corrupt_song_and_history(manager_factory, monkeypatch):
    """ADR-0008-19. Missing cues never make an older song disposable."""
    manager = manager_factory()
    song = manager.cfg.library.music_dir / "older.flac"
    song.write_bytes(b"not audio")
    manager.catalog.put_entry(make_entry(song))
    manager.ledger.remember_play(song, 99.5)
    before = manager.catalog.record_for_path(song)
    queue = manager.analysis_queue
    now = [1000.0]
    queue._now = lambda: now[0]
    queue.seen("worker", "Worker", scan_decode=True)
    monkeypatch.setattr(manager, "schedule_analysis", lambda: False)
    manager.scan()
    now[0] += 121
    tick_and_wait(manager)
    assert manager.catalog.record_for_path(song) == before
    assert manager.ledger.store.last_played()[_path(song)] == 99.5
    assert queue.scan_pending(_path(song))


def test_idle_recovery_checks_are_throttled_outside_library_lock(abandoned_scan, monkeypatch):
    """ADR-0008-20. Repeated idle ticks cannot query the backlog under the library lock."""
    manager, queue, _now, _songs = abandoned_scan
    clock = [0.0]
    manager._clock = lambda: clock[0]
    calls = []
    read = queue.abandoned_scans

    def checked(**kwargs):
        acquired = []

        def probe():
            if manager._lock.acquire(timeout=0.2):
                acquired.append(True)
                manager._lock.release()

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(timeout=1)
        assert acquired == [True]
        calls.append(clock[0])
        return read(**kwargs)

    monkeypatch.setattr(queue, "abandoned_scans", checked)
    for second in range(0, 62, 2):
        clock[0] = float(second)
        manager.tick()
    assert calls == [0.0, 30.0, 60.0]


def test_scan_recovery_repairs_partial_points_after_timestamp_change(abandoned_scan):
    manager, queue, now, songs = abandoned_scan
    song = songs[0]
    manager.catalog.set_points(song, {"intro": 0.5}, source="analysis")
    stat = song.stat()
    os.utime(song, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    now[0] += 121
    tick_and_wait(manager)
    assert not queue.scan_pending(_path(song))
    record = manager.catalog.record_for_path(song)
    assert record["points"]["end"] == 179.0
    assert record["energy"] > 0


def test_background_scan_queue_error_releases_slot_and_recovers(
    abandoned_scan, monkeypatch, caplog
):
    import sqlite3

    manager, queue, now, _songs = abandoned_scan
    now[0] += 121
    read = queue.abandoned_scans
    monkeypatch.setattr(
        queue, "abandoned_scans", Mock(side_effect=sqlite3.OperationalError("locked"))
    )
    with caplog.at_level(logging.WARNING):
        tick_and_wait(manager)
        tick_and_wait(manager)
    assert not manager._scan_lock.locked()
    assert sum("Scan recovery unavailable" in r.message for r in caplog.records) == 1
    monkeypatch.setattr(queue, "abandoned_scans", read)
    now[0] += 30
    tick_and_wait(manager)
    assert not queue.pending_scans()


def test_scan_retry_budget_logs_once_and_preserves_vector_queue(
    abandoned_scan, monkeypatch, caplog
):
    """ADR-0008-11. Repeated storage failure exhausts a persistent scan-only budget."""
    from autodj.analysis_queue import FAILED_RETRY_SECONDS, SERVER_HOLDER

    manager, queue, now, songs = abandoned_scan
    queue.cancel_scan(_path(songs[1]))
    now[0] += 121
    # A transient storage failure differs from an unplayable input file.
    monkeypatch.setattr(
        manager.catalog, "set_intro_lift", Mock(side_effect=RuntimeError("disk full"))
    )
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            tick_and_wait(manager)
            now[0] += FAILED_RETRY_SECONDS + 1
    assert sum("Scan retries exhausted" in r.message for r in caplog.records) == 1
    assert _path(songs[0]) in caplog.text
    record = manager.catalog.record_for_path(songs[0])
    assert record is not None
    assert queue.claim(
        SERVER_HOLDER, [(record["path"], record["size"], record["fingerprint"])], 600
    )


def test_scan_dj_storage_never_imports_on_request_path(manager_factory, monkeypatch):
    from autodj.dj_meta import DjMeta, close_cache

    manager = manager_factory()
    song = manager.cfg.library.music_dir / "song.flac"
    sf.write(song, np.ones((100, 2)) * 0.1, 22050)
    monkeypatch.setattr(
        "autodj.dj_cues_import.auto_import_cues", Mock(side_effect=AssertionError("request import"))
    )
    try:
        manager.store_scan_dj(song, DjMeta(analysed=True), force=True)
        assert manager._dj_cache().get(str(song)).analysed
    finally:
        close_cache()


def test_scan_external_cue_failure_does_not_stop_discovery(manager_factory, monkeypatch, caplog):
    manager = manager_factory()
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: True)
    monkeypatch.setattr(manager, "_dj_backlog", lambda: None)
    song = manager.cfg.library.music_dir / "song.flac"
    song.write_bytes(b"audio")
    monkeypatch.setattr("autodj.library.analyze_basic", fake_basic)
    monkeypatch.setattr("autodj.library.load_for_analysis", lambda path: np.ones(22050))
    monkeypatch.setattr("autodj.intro_lift.measure_source_intro", lambda *args: 1.0)
    monkeypatch.setattr(
        "autodj.dj_cues_import.auto_import_cues", Mock(side_effect=OSError("unavailable"))
    )
    with caplog.at_level(logging.WARNING):
        manager.scan()
        manager.scan()
    assert manager._state["phase"] == "complete"
    assert sum("External cue import unavailable" in r.message for r in caplog.records) == 1


@pytest.mark.parametrize("recovery", [False, True])
def test_external_cues_load_once_per_long_scan_or_drain(abandoned_scan, monkeypatch, recovery):
    """ADR-0008-22. Slow songs do not trigger repeated external library parsing."""
    from autodj.dj_meta import DjMeta

    manager, queue, now, songs = abandoned_scan
    now[0] += 121
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: True)
    imported = Mock(return_value={})
    monkeypatch.setattr("autodj.dj_cues_import.auto_import_cues", imported)

    def decode(path):
        now[0] += 61
        return np.ones(22050)

    monkeypatch.setattr("autodj.library.load_for_analysis", decode)
    monkeypatch.setattr(
        "autodj.dj_meta.analyse_decoded", lambda *args, **kwargs: DjMeta(analysed=True)
    )
    if recovery:
        tick_and_wait(manager)
    else:
        manager.scan()
    assert not queue.pending_scans()
    assert all(manager._dj_cache().get(str(song)).analysed for song in songs)
    imported.assert_called_once()
    # A later pass sees edits to an external library when it next needs DJ analysis.
    new_song = manager.cfg.library.music_dir / "later.flac"
    new_song.write_bytes(songs[0].read_bytes())
    manager.scan()
    assert imported.call_count == 2


def test_scan_without_dj_work_does_not_parse_external_cues(manager_factory, monkeypatch):
    manager = manager_factory()
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: True)
    imported = Mock(return_value={})
    monkeypatch.setattr("autodj.dj_cues_import.auto_import_cues", imported)
    manager.scan()
    manager.scan()
    imported.assert_not_called()


def test_remote_cue_refresh_is_bounded_and_runs_off_request_thread(manager_factory, monkeypatch):
    """ADR-0008-22. Remote results request bounded cue refresh on the scan owner."""
    from autodj.dj_meta import DjMeta

    manager = manager_factory()
    now = [1000.0]
    manager._clock = lambda: now[0]
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: True)
    monkeypatch.setattr(manager, "schedule_analysis", lambda: False)
    imported_on = []

    def import_cues(**kwargs):
        assert manager._scan_lock.locked()
        imported_on.append(threading.current_thread())
        return {}

    monkeypatch.setattr("autodj.dj_cues_import.auto_import_cues", import_cues)
    song = manager.cfg.library.music_dir / "remote.flac"
    manager.store_scan_dj(song, DjMeta(analysed=True), force=True)
    assert not imported_on
    tick_and_wait(manager)
    assert len(imported_on) == 1
    assert imported_on[0] is not threading.current_thread()
    manager.store_scan_dj(song, DjMeta(analysed=True), force=True)
    now[0] += 30
    tick_and_wait(manager)
    assert len(imported_on) == 1
    now[0] += 30
    tick_and_wait(manager)
    assert len(imported_on) == 2
    now[0] += 61
    tick_and_wait(manager)
    assert len(imported_on) == 2


@pytest.mark.parametrize("capable", [None, False, True])
def test_scan_worker_capability_controls_music_decoding(manager_factory, monkeypatch, capable):
    """ADR-0008-06. Only a capable worker takes over scan decoding."""
    manager = manager_factory(processing="smooth")
    song = manager.cfg.library.music_dir / "new.flac"
    sf.write(song, np.ones((22050, 2)) * 0.1, 22050)
    if capable is not None:
        manager.analysis_queue.seen("worker", "Worker", scan_decode=capable)
    basic = Mock(side_effect=fake_basic)
    loudness = Mock(side_effect=fake_loudness)
    monkeypatch.setattr("autodj.library.analyze_basic", basic)
    monkeypatch.setattr("autodj.library.analyze_loudness", loudness)
    monkeypatch.setattr("autodj.library.load_for_analysis", Mock(side_effect=AssertionError))
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: False)
    intro = Mock(return_value=1.0)
    monkeypatch.setattr("autodj.intro_lift.measure_source_intro", intro)
    manager.scan()
    record = manager.catalog.record_for_path(song)
    assert record is not None
    assert basic.call_count == loudness.call_count == intro.call_count == int(not capable)
    assert (record["energy"] == 0) == bool(capable)


@pytest.mark.parametrize("outcome", ["claimed", "failed", "stale", "disconnected"])
def test_scan_expired_worker_lease_falls_back(manager_factory, monkeypatch, outcome):
    """ADR-0008-06. Expiry allows a later scan to finish deferred music."""
    from autodj.analysis_queue import (
        FAILED_RETRY_SECONDS,
        STALE_RETRY_SECONDS,
        WORKER_LEASE_SECONDS,
    )

    manager = manager_factory(processing="smooth")
    now = [1000.0]
    queue = manager.analysis_queue
    queue._now = lambda: now[0]
    queue.seen("worker", "Worker", scan_decode=True)
    song = manager.cfg.library.music_dir / "new.flac"
    sf.write(song, np.ones((22050, 2)) * 0.1, 22050)
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: False)
    manager.scan()
    record = manager.catalog.record_for_path(song)
    if outcome == "disconnected":
        now[0] += 121
    else:
        lease = queue.claim(
            "worker",
            [(record["path"], record["size"], record["fingerprint"])],
            WORKER_LEASE_SECONDS,
        )[0]
        if outcome != "claimed":
            queue.finish(lease, outcome)
        now[0] += {
            "claimed": WORKER_LEASE_SECONDS,
            "failed": FAILED_RETRY_SECONDS,
            "stale": STALE_RETRY_SECONDS,
        }[outcome] + 1
        # Even another connected worker must not postpone this song indefinitely.
        queue.seen("other", "Worker", scan_decode=True)
    basic = Mock(side_effect=fake_basic)
    monkeypatch.setattr("autodj.library.analyze_basic", basic)
    monkeypatch.setattr("autodj.library.analyze_loudness", fake_loudness)
    monkeypatch.setattr("autodj.intro_lift.measure_source_intro", lambda *args: 1.0)
    manager.scan()
    assert basic.call_count == 1
    record = manager.catalog.record_for_path(song)
    assert record["energy"] > 0
    assert record["points"]["end"] == 179.0
    assert record["loudness"]


def test_scan_defers_changed_music_but_still_decodes_liners(manager_factory, monkeypatch, tmp_path):
    liners = tmp_path / "liners"
    liners.mkdir()
    manager = manager_factory(liners=liners, processing="smooth")
    song = manager.cfg.library.music_dir / "changed.flac"
    liner = liners / "liner.flac"
    sf.write(song, np.ones((22050, 2)) * 0.1, 22050)
    sf.write(liner, np.ones((22050, 2)) * 0.1, 22050)
    entry = make_entry(song)
    entry.length = 1.0
    manager.catalog.put_entry(entry, points={"start": 0.0, "end": 1.0})
    sf.write(song, np.ones((44100, 2)) * 0.2, 22050)
    manager.analysis_queue.seen("worker", "Worker", scan_decode=True)
    basic = Mock(side_effect=fake_basic)
    loudness = Mock(side_effect=fake_loudness)
    monkeypatch.setattr("autodj.library.analyze_basic", basic)
    monkeypatch.setattr("autodj.library.analyze_loudness", loudness)
    manager.scan()
    assert [call.args[0] for call in basic.call_args_list] == [liner]
    assert [call.args[0] for call in loudness.call_args_list] == [liner]
    record = manager.catalog.record_for_path(song)
    assert record["energy"] == 0.25
    assert record["points"] == {"start": 0.0, "end": 1.0}
    assert record["length"] == 1.0


def test_scan_adapts_when_worker_connects_after_tag_discovery(manager_factory, monkeypatch):
    manager = manager_factory()
    song = manager.cfg.library.music_dir / "new.flac"
    sf.write(song, np.ones((22050, 2)) * 0.1, 22050)
    add = manager._add_from_tags
    now = [0.0]
    manager._clock = lambda: now[0]

    def connect_after_tags(path, **kwargs):
        result = add(path, **kwargs)
        manager.analysis_queue.seen("worker", "Worker", scan_decode=True)
        now[0] += 6
        return result

    monkeypatch.setattr(manager, "_add_from_tags", connect_after_tags)
    monkeypatch.setattr("autodj.library.analyze_basic", Mock(side_effect=AssertionError("decoded")))
    manager.scan()
    assert manager.catalog.record_for_path(song)["energy"] == 0
    assert manager.analysis_queue.scan_pending(_path(song))


def test_scan_marks_new_worker_song_pending_before_publishing_tags(manager_factory, monkeypatch):
    manager = manager_factory()
    song = manager.cfg.library.music_dir / "new.flac"
    sf.write(song, np.ones((100, 2)) * 0.1, 22050)
    queue = manager.analysis_queue
    queue.seen("worker", "Worker", scan_decode=True)
    add = manager._add_from_tags

    def publish(path, **kwargs):
        assert queue.scan_pending(_path(path))
        return add(path, **kwargs)

    monkeypatch.setattr(manager, "_add_from_tags", publish)
    manager.scan()
    assert queue.pending_scans() == {_path(song)}


def test_scan_relists_retagged_pending_song_while_worker_is_busy(abandoned_scan):
    """ADR-0008-16. Retagged pending music remains claimable during a worker backlog."""
    manager, queue, _now, songs = abandoned_scan
    record = manager.catalog.record_for_path(songs[1])
    queue.claim("worker", [(record["path"], record["size"], record["fingerprint"])], 600)
    tags = FLAC(songs[0])
    tags["title"] = "Updated title"
    tags.save()
    manager.scan()
    record = manager.catalog.record_for_path(songs[0])
    assert (record["size"], record["fingerprint"]) == file_fingerprint(songs[0])
    assert record["title"] == "Updated title"
    assert record["energy"] == 0
    assert queue.scan_pending(record["path"])


@pytest.fixture
def abandoned_scan(manager_factory, monkeypatch):
    manager = manager_factory(processing="smooth")
    now = [1000.0]
    manager._clock = lambda: now[0]
    queue = manager.analysis_queue
    queue._now = lambda: now[0]
    queue.seen("worker", "Worker", scan_decode=True)
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: False)
    monkeypatch.setattr(manager, "schedule_analysis", lambda: False)
    monkeypatch.setattr("autodj.library.analyze_basic", fake_basic)
    monkeypatch.setattr("autodj.library.analyze_loudness", fake_loudness)
    monkeypatch.setattr("autodj.intro_lift.measure_source_intro", lambda *args: 1.0)
    songs = [manager.cfg.library.music_dir / f"pending-{i}.flac" for i in range(2)]
    for song in songs:
        sf.write(song, np.ones((22050, 2)) * 0.1, 22050)
    manager.scan()
    assert len(queue.pending_scans()) == 2
    return manager, queue, now, songs


@pytest.mark.parametrize("outcome", ["disconnected", "expired", "failed", "unclaimed"])
def test_tick_decodes_abandoned_scan_without_rescan(abandoned_scan, outcome, caplog):
    """ADR-0008-06. Pending scans recover without another discovery pass."""
    from autodj.analysis_queue import WORKER_ACTIVE_SECONDS, WORKER_LEASE_SECONDS

    manager, queue, now, songs = abandoned_scan
    if outcome in {"expired", "failed"}:
        record = manager.catalog.record_for_path(songs[0])
        lease = queue.claim(
            "worker",
            [(record["path"], record["size"], record["fingerprint"])],
            WORKER_LEASE_SECONDS,
        )[0]
        if outcome == "failed":
            queue.finish(lease, "failed")
    now[0] += (
        WORKER_LEASE_SECONDS if outcome in {"expired", "unclaimed"} else WORKER_ACTIVE_SECONDS
    ) + 1
    generation = manager._scan_generation
    with caplog.at_level(logging.INFO, logger="autodj.library"):
        tick_and_wait(manager)
        now[0] += 30
        tick_and_wait(manager)
        now[0] += 30
        tick_and_wait(manager)
    assert queue.pending_scans() == set()
    assert manager._scan_generation == generation
    for song in songs:
        record = manager.catalog.record_for_path(song)
        assert record["energy"] > 0
        assert record["points"]["end"] == 179.0
        assert record["loudness"]
        assert record["intro_lift_db"] == 1.0
    messages = [record.message for record in caplog.records if "abandoned scan" in record.message]
    assert len(messages) == 2
    assert "2" in messages[0]


def test_tick_scan_fallback_yields_to_reconnected_worker(abandoned_scan, monkeypatch):
    """ADR-0008-09. Ordinary pending songs return to a reconnected worker."""
    manager, queue, now, _songs = abandoned_scan

    def reconnect(path, **kwargs):
        queue.seen("worker", "Worker", scan_decode=True)
        return fake_basic(path, **kwargs)

    monkeypatch.setattr("autodj.library.analyze_basic", reconnect)
    now[0] += 121
    tick_and_wait(manager)
    assert len(queue.pending_scans()) == 1
    queue.seen("worker", "Worker", scan_decode=True)
    now[0] += 30
    tick_and_wait(manager)
    assert len(queue.pending_scans()) == 1


def test_tick_scan_fallback_respects_scan_lock_and_live_lease(abandoned_scan):
    """ADR-0008-09. Scans and live claims exclude background recovery."""
    from autodj.analysis_queue import WORKER_LEASE_SECONDS

    manager, queue, now, songs = abandoned_scan
    record = manager.catalog.record_for_path(songs[0])
    queue.claim(
        "worker",
        [(record["path"], record["size"], record["fingerprint"])],
        WORKER_LEASE_SECONDS,
    )
    now[0] += 121
    with manager._scan_lock:
        tick_and_wait(manager)
    assert len(queue.pending_scans()) == 2
    tick_and_wait(manager)
    tick_and_wait(manager)
    assert queue.pending_scans() == {_path(songs[0])}


@pytest.mark.parametrize("outcome", ["failed", "expired", "unclaimed"])
def test_tick_scan_recovers_abandoned_song_despite_worker_heartbeats(abandoned_scan, outcome):
    from autodj.analysis_queue import WORKER_LEASE_SECONDS

    manager, queue, now, songs = abandoned_scan
    if outcome != "unclaimed":
        record = manager.catalog.record_for_path(songs[0])
        lease = queue.claim(
            "worker",
            [(record["path"], record["size"], record["fingerprint"])],
            WORKER_LEASE_SECONDS,
        )[0]
        if outcome == "failed":
            queue.finish(lease, "failed")
    if outcome != "failed":
        now[0] += WORKER_LEASE_SECONDS + 1
    queue.seen("worker", "Worker", scan_decode=True)
    tick_and_wait(manager)
    assert len(queue.pending_scans()) == (1 if outcome == "failed" else 0)
    assert manager.catalog.record_for_path(songs[0])["energy"] > 0


def test_tick_scan_holds_server_lease_and_yields_to_queued_scan(abandoned_scan, monkeypatch):

    manager, queue, now, _songs = abandoned_scan
    now[0] += 121
    scanned = []

    def decode(path, **kwargs):
        assert manager._scan_lock.locked()
        record = manager.catalog.record_for_path(path)
        assert (
            queue.claim("worker", [(record["path"], record["size"], record["fingerprint"])], 600)
            == []
        )
        assert queue._read("SELECT attempts FROM scan_pending WHERE path=?", (_path(path),)) == [
            (1,)
        ]
        manager.start_scan()
        return fake_basic(path, **kwargs)

    monkeypatch.setattr("autodj.library.analyze_basic", decode)
    monkeypatch.setattr(manager, "_scan", lambda *args: scanned.append(True))
    tick_and_wait(manager)
    manager._thread.join(timeout=5)
    assert scanned == [True]
    assert not manager._thread.is_alive()


def test_tick_scan_local_failure_does_not_retry_each_tick(abandoned_scan, monkeypatch):
    manager, queue, now, _songs = abandoned_scan
    now[0] += 121
    decode = Mock(side_effect=RuntimeError("unreadable audio"))
    monkeypatch.setattr("autodj.library.analyze_basic", decode)
    for _ in range(5):
        tick_and_wait(manager)
        now[0] += 30
    assert decode.call_count == 2
    assert len(queue.pending_scans()) == 2
    assert len(manager.catalog.all_entries("music")) == 2


def test_tick_scan_decodes_pending_song_after_timestamp_change(abandoned_scan):
    manager, queue, now, songs = abandoned_scan
    now[0] += 121
    song = songs[0]
    stat = song.stat()
    os.utime(song, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    tick_and_wait(manager)
    assert not queue.scan_pending(_path(song))
    record = manager.catalog.record_for_path(song)
    assert record["energy"] > 0
    assert record["points"]["end"] == 179.0


@pytest.mark.parametrize("measured", [False, True])
def test_tick_scan_uses_local_dj_path(abandoned_scan, monkeypatch, measured):
    from autodj.dj_meta import DjMeta, close_cache

    manager, _queue, now, songs = abandoned_scan
    now[0] += 121
    if measured:
        # A remote result can store basic measurements before DJ storage fails.
        entry, points = fake_basic(songs[0])
        manager.catalog.put_entry(entry, points=points)
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: True)
    monkeypatch.setattr("autodj.library.load_for_analysis", lambda path: np.zeros(22050))
    monkeypatch.setattr("autodj.dj_meta.analyse_audio", lambda *a, **kw: DjMeta(analysed=True))
    imports = Mock(return_value={})
    monkeypatch.setattr("autodj.dj_cues_import.auto_import_cues", imports)
    try:
        tick_and_wait(manager)
        now[0] += 30
        tick_and_wait(manager)
        assert manager._dj_cache().get(str(songs[0])).analysed
        assert manager._dj_cache().get(str(songs[1])).analysed
        assert imports.call_count == 1
    finally:
        close_cache()


def test_empty_scan_fallback_uses_bounded_reads_without_decode(manager_factory, monkeypatch):
    """ADR-0008-08. Empty recovery reads worker progress and candidates without writes."""
    manager = manager_factory()
    monkeypatch.setattr(manager, "schedule_analysis", lambda: False)
    queue = manager.analysis_queue
    read = Mock(wraps=queue._read)
    write = Mock(wraps=queue._write)
    decode = Mock(side_effect=AssertionError("unexpected decode"))
    monkeypatch.setattr(queue, "_read", read)
    monkeypatch.setattr(queue, "_write", write)
    monkeypatch.setattr("autodj.library.analyze_basic", decode)
    manager.tick()
    assert read.call_count == 2
    write.assert_not_called()
    decode.assert_not_called()


def test_unchanged_scan_has_no_per_song_queue_work(manager_factory, monkeypatch):
    """ADR-0008-08. Unchanged music and liners add no per-song queue work."""
    manager = manager_factory()
    queue = manager.analysis_queue
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: False)
    monkeypatch.setattr("autodj.library.analyze_basic", fake_basic)
    monkeypatch.setattr("autodj.intro_lift.measure_source_intro", lambda *args: 1.0)
    for i in range(40):
        song = manager.cfg.library.music_dir / f"unchanged-{i}.flac"
        sf.write(song, np.ones((100, 2)) * 0.1, 22050)
    liner = manager.cfg.index.stations_dir / "default" / "liners" / "liner.flac"
    sf.write(liner, np.ones((100, 2)) * 0.1, 22050)
    manager.scan()
    queue.seen("worker", "Worker", scan_decode=True)
    read = Mock(wraps=queue._read)
    write = Mock(wraps=queue._write)
    monkeypatch.setattr(queue, "_read", read)
    monkeypatch.setattr(queue, "_write", write)
    manager.scan()
    write.assert_not_called()
    assert read.call_count <= 2  # Pending paths and capability, once for this pass.


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
    manager.set_music_fade_drop_db(17)
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
        music_call = next(call for call in analyzed.call_args_list if call.args[0] == song)
        liner_call = next(call for call in analyzed.call_args_list if call.args[0] == liner)
        assert music_call.kwargs["music_fade_drop_db"] == 17
        assert "music_fade_drop_db" not in liner_call.kwargs
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

    fade_thresholds = []

    def analyze(path, kind="music", audio=None, music_fade_drop_db=10):
        fade_thresholds.append(music_fade_drop_db)
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
    assert fade_thresholds == [10, 10]
    assert any("bad.flac" in error for error in manager.snapshot()["import"]["errors"])


def test_music_fade_setting_defaults_validates_and_persists(manager_factory):
    manager = manager_factory()
    assert manager.music_fade_drop_db() == 10
    for corrupt in (True, 0, 31, 1.5, "14"):
        manager.catalog.set_setting("music_fade_drop_db", corrupt)
        assert music_fade_drop_db(manager.catalog) == 10
    with patch.object(manager, "start_scan") as start_scan:
        manager.set_music_fade_drop_db(14)
        start_scan.assert_not_called()
    assert manager.music_fade_drop_db() == 14
    with LibraryCatalog(manager.catalog.path) as reopened:
        assert music_fade_drop_db(reopened) == 14
    for invalid in (True, 1.5, 0, 31):
        with pytest.raises(ValueError):
            manager.set_music_fade_drop_db(invalid)


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

    def basic(path, kind="music", audio=None, **kwargs):
        del kwargs
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

    def short_track(path: Path, kind: str = "music", audio=None, **kwargs):
        del kind, audio, kwargs
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


@pytest.mark.parametrize("ambiguity", ["missing", "candidate", "present", "present_pending"])
def test_pending_move_requires_unique_identity(manager_factory, ambiguity):
    """ADR-0008-24. Duplicate pending identities must not transfer another song's history."""
    manager = manager_factory()
    root = manager.cfg.library.music_dir
    old, new, other = root / "old.flac", root / "new.flac", root / "other.flac"
    old.write_bytes(b"old recording")
    manager.catalog.put_entry(make_entry(old))
    old.write_bytes(b"pending recording")
    manager.analysis_queue.defer_scan(_path(old), *file_fingerprint(old))
    old.rename(new)
    other.write_bytes(new.read_bytes())
    found = [("music", new), ("music", other)]
    if ambiguity != "candidate":
        manager.catalog.put_entry(make_entry(other))
        if ambiguity == "present_pending":
            other.write_bytes(b"another replacement")
            manager.analysis_queue.defer_scan(_path(other), *file_fingerprint(other))
        if ambiguity == "missing":
            other.unlink()
            found.pop()
    manager._reconcile_moves(found)
    assert manager.catalog.record_for_path(new) is None
    assert manager.catalog.record_for_path(old) is not None
    assert manager.analysis_queue.scan_pending(_path(old))
    assert not manager.analysis_queue.scan_pending(_path(new))


def test_pending_move_preserves_retry_state(manager_factory):
    """ADR-0008-24. Renaming cannot reset a deferred scan's retry budget."""
    manager = manager_factory()
    old = manager.cfg.library.music_dir / "old.flac"
    new = old.with_name("new.flac")
    old.write_bytes(b"old recording")
    manager.catalog.put_entry(make_entry(old))
    old.write_bytes(b"pending recording")
    queue = manager.analysis_queue
    identity = file_fingerprint(old)
    queue.defer_scan(_path(old), *identity)
    assert queue.claim_scan(_path(old), *identity)
    queue.fail_scan(_path(old), *identity, non_access_failure=True)
    before = queue._read("SELECT * FROM scan_pending WHERE path=?", (_path(old),))[0]
    old.rename(new)
    manager._reconcile_moves([("music", new)])
    after = queue._read("SELECT * FROM scan_pending WHERE path=?", (_path(new),))
    assert after and after[0][1:] == before[1:]


def test_pending_rekey_rolls_back_queue_and_lease_on_catalog_failure(manager_factory):
    import sqlite3

    manager = manager_factory()
    old = manager.cfg.library.music_dir / "old.flac"
    new = old.with_name("new.flac")
    old.write_bytes(b"pending recording")
    manager.catalog.put_entry(make_entry(old))
    queue = manager.analysis_queue
    identity = file_fingerprint(old)
    queue.defer_scan(_path(old), *identity, new_listing=True)
    lease = queue.claim("worker", [(_path(old), *identity)], 600)[0]
    old.rename(new)
    manager.catalog._db().execute(
        "CREATE TEMP TRIGGER reject_move BEFORE UPDATE OF path ON catalog_entries "
        "BEGIN SELECT RAISE(ABORT, 'move failed'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="move failed"):
        manager.catalog.rekey(old, new)
    assert manager.catalog.record_for_path(old) is not None
    assert manager.catalog.record_for_path(new) is None
    assert queue.scan_identity(_path(old)) == identity
    assert queue.scan_created_listing(_path(old), *identity)
    assert not queue.scan_pending(_path(new))
    assert queue.lease(lease.lease_id, "worker") == lease


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
@pytest.mark.parametrize("change", ["size", "fingerprint"])
@pytest.mark.parametrize("dj_enabled", [False, True])
@pytest.mark.parametrize("stale_pending", [False, True])
def test_changed_file_recalculates_cues_without_inheriting_dj_cache(
    manager_factory, tmp_path, change, dj_enabled, stale_pending
):
    """ADR-0006-05: timing analysis belongs to the current file identity."""
    from autodj.dj_meta import Cue, DjMeta, DjMetaCache

    song = tmp_path / "Music" / "song.flac"
    song.parent.mkdir()
    song.write_bytes(b"a" * 1000)
    manager = manager_factory(music=song.parent)
    original = make_entry(song)
    original.length = 18
    object.__setattr__(manager.cfg.playback, "import_external_cues", False)
    record_id = manager.catalog.put_entry(
        original,
        points={"start": 0, "intro": 8, "outro": 12, "end": 18},
        point_source="user",
    )
    original_identity = file_fingerprint(song)
    cache = manager._dj_cache()
    cache.set(
        str(song),
        DjMeta(
            analysed=True,
            intro_end_s=8,
            outro_start_s=12,
            beats=[8, 12],
            cues=[Cue(12, type="outro_downbeat")],
        ),
    )
    cache.flush(force=True)
    manager.set_dj_analysis_enabled(dj_enabled)
    song.write_bytes(b"b" * (1200 if change == "size" else 1000))
    assert file_fingerprint(song) != original_identity
    if stale_pending:
        # The worker finished after discovery captured the pending paths.
        manager._scan_pending = {_path(song)}
    fresh = make_entry(song)
    fresh.length = 24
    fresh_meta = DjMeta(analysed=True, outro_start_s=24, beats=[2, 4])
    with (
        patch(
            "autodj.library.analyze_basic",
            return_value=(
                fresh,
                {
                    "start": 0,
                    "intro": None,
                    "outro": None,
                    "end": 24,
                },
            ),
        ),
        patch(
            "autodj.library.load_for_analysis", return_value=np.zeros((24 * 44100, 2), np.float32)
        ) as decode,
        patch.object(manager, "_legacy_point_suggestions", side_effect=AssertionError("old cues")),
        patch("autodj.dj_meta.analyse_decoded", return_value=fresh_meta) as analyse,
        patch("autodj.intro_lift.measure_source_intro", return_value=0),
    ):
        assert manager._read_track("music", song, override_tags=False)
        record = manager.catalog.record_for_path(song)
        assert record["id"] == record_id
        assert (record["size"], record["fingerprint"]) == file_fingerprint(song)
        assert record["length"] == 24
        assert record["points"]["end"] == 24
        assert record["points"]["intro"] is None
        assert record["points"]["outro"] is None
        if not dj_enabled:
            analyse.assert_not_called()
            decode.assert_not_called()
            assert cache.get(str(song)) == DjMeta()
            disk = DjMetaCache(manager.cfg.index.library_dir / "dj_meta.db", song.parent)
            try:
                assert disk.get(str(song)) == DjMeta()
            finally:
                disk.close()
            manager.set_dj_analysis_enabled(True)
            manager._dj_backlog()
        analyse.assert_called_once()
        assert analyse.call_args.kwargs["intro_end_s"] == 0
        assert analyse.call_args.kwargs["outro_start_s"] == 24
        assert cache.get(str(song)) == fresh_meta


@pytest.mark.dj_analysis
@pytest.mark.parametrize("already_pending", [False, True])
@pytest.mark.parametrize("change", ["size", "fingerprint"])
def test_deferred_replacement_clears_old_dj_cache_before_publishing_identity(
    manager_factory, monkeypatch, already_pending, change
):
    """ADR-0006-05: worker hints and local recovery cannot inherit old DJ timing."""
    from autodj.audio_meta import FileTags
    from autodj.dj_meta import Cue, DjMeta, DjMetaCache

    manager = manager_factory()
    manager.set_dj_analysis_enabled(False)
    song = manager.cfg.library.music_dir / "song.flac"
    song.write_bytes(b"a" * 1000)
    original = make_entry(song)
    original.length = 18
    manager.catalog.put_entry(original, points={"start": 0, "intro": 8, "outro": 12, "end": 18})
    cache = manager._dj_cache()
    cache.set(
        str(song),
        DjMeta(
            analysed=True,
            intro_end_s=8,
            outro_start_s=12,
            beats=[8, 12],
            cues=[Cue(12, type="outro_downbeat")],
        ),
    )
    cache.flush(force=True)
    queue = manager.analysis_queue
    queue.seen("worker", "Worker", scan_decode=True)
    if already_pending:
        queue.defer_scan(_path(song), *file_fingerprint(song))
    song.write_bytes(b"b" * (1200 if change == "size" else 1000))
    monkeypatch.setattr("autodj.audio_meta.read_file_tags", lambda path: FileTags(length=24))
    assert manager._read_track("music", song, override_tags=False)
    record = manager.catalog.record_for_path(song)
    assert (record["size"], record["fingerprint"]) == file_fingerprint(song)
    assert queue.scan_pending(_path(song))
    assert cache.get(str(song)) == DjMeta()
    assert manager.scan_options(song)["suggestions"] == {}
    disk = DjMetaCache(manager.cfg.index.library_dir / "dj_meta.db", song.parent)
    try:
        assert disk.get(str(song)) == DjMeta()
    finally:
        disk.close()
    fresh = make_entry(song)
    fresh.length = 24
    monkeypatch.setattr(
        "autodj.library.analyze_basic",
        lambda *a, **k: (fresh, {"start": 0, "intro": None, "outro": None, "end": 24}),
    )
    assert manager._read_track("music", song, override_tags=False, recover_scan=True)
    record = manager.catalog.record_for_path(song)
    assert record["points"]["intro"] is None
    assert record["points"]["outro"] is None
    assert record["points"]["end"] == 24
    assert cache.get(str(song)) == DjMeta()


@pytest.mark.dj_analysis
@pytest.mark.parametrize("migrated", [False, True])
def test_rescan_reads_external_tag_edit_with_unchanged_size(manager_factory, tmp_path, migrated):
    """ADR-0006-05: unchanged identities preserve their existing timing analysis."""
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

    def basic(path, kind="music", audio=None, **kwargs):
        del kwargs
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


@pytest.mark.dj_analysis
def test_shutdown_stops_the_scan_before_the_dj_meta_cache_closes(manager_factory, monkeypatch):
    """ADR-0008-25. A scan running at shutdown ends before the DJ-meta cache closes.

    It neither opens the cache again nor reloads the catalog.
    """
    import asyncio
    from types import SimpleNamespace

    from autodj import dj_meta
    from autodj.stations import SharedLibrary

    manager = manager_factory()
    song = manager.cfg.library.music_dir / "song.flac"
    song.write_bytes(b"music")
    manager.catalog.put_entry(make_entry(song), "music")
    # The player opened the cache at startup; a big library has many rows.
    cache = dj_meta.get_cache(manager.cfg.index.library_dir, manager.cfg.library.music_dir)
    assert cache is not None
    cache.set(str(song), dj_meta.DjMeta(analysed=True, beats=[0.5]))
    cache.flush(force=True)
    opened = []

    class Spy(dj_meta.DjMetaCache):
        def __init__(self, *args, **kwargs):
            opened.append(args)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(dj_meta, "DjMetaCache", Spy)
    discovering = threading.Event()

    def discover(source_ids=None):
        # A big library's folders take a while to list.
        discovering.set()
        assert manager._stop.wait(5)
        return [("music", song)]

    monkeypatch.setattr(manager, "_discover", discover)
    reconciled, refreshed = [], []
    monkeypatch.setattr(manager, "_reconcile_moves", lambda *args: reconciled.append(args))
    monkeypatch.setattr(manager, "refresh", lambda **kwargs: refreshed.append(kwargs))
    monkeypatch.setattr(manager, "tick", lambda: None)

    async def shut_down() -> bool:
        shared = SharedLibrary(manager, SimpleNamespace(all=lambda: []))
        await shared.start()
        assert await asyncio.to_thread(discovering.wait, 5)
        return await shared.close(True, timeout_s=5)

    assert asyncio.run(shut_down())
    assert opened == []
    assert dj_meta._CACHE is None
    assert reconciled == [] and refreshed == []
    assert manager._thread is not None and not manager._thread.is_alive()


def test_stop_scan_is_bounded_for_a_scan_that_has_not_stopped(manager_factory, monkeypatch):
    """ADR-0008-25. Waiting for the scan to stop has a time limit, and no scan starts after."""
    manager = manager_factory()
    scanning, release = threading.Event(), threading.Event()

    def scan(*_args):
        scanning.set()
        release.wait(5)

    monkeypatch.setattr(manager, "_scan", scan)
    assert manager.start_scan()
    assert scanning.wait(5)
    try:
        assert not manager.stop_scan(0.05)
        assert not manager.start_scan()
    finally:
        release.set()
    assert manager.stop_scan(5)


def test_discovery_stops_between_folders_once_the_library_closes(manager_factory, monkeypatch):
    """ADR-0008-25. Listing a big library's folders stops at the next folder."""
    manager = manager_factory()
    music = manager.cfg.library.music_dir
    for name in ("a", "b", "c"):
        (music / name).mkdir()
        (music / name / "song.flac").write_bytes(b"music")
    walk = os.walk

    def stopping_walk(top, **kwargs):
        for folder in walk(top, **kwargs):
            yield folder
            manager._stop.set()

    monkeypatch.setattr("autodj.library.os.walk", stopping_walk)
    # The top folder holds no song; the walk ends before the first that does.
    assert manager._discover() == []


@pytest.mark.dj_analysis
def test_the_dj_backlog_stops_reading_the_cache_once_the_library_closes(
    manager_factory, monkeypatch
):
    """ADR-0008-25. Listing unanalysed songs reads every row: shutdown cuts that short."""
    from autodj.dj_meta import DjMeta

    manager = manager_factory()
    music = manager.cfg.library.music_dir
    records = tuple({"path": str(music / f"{n}.flac")} for n in range(1000))
    monkeypatch.setattr(manager.catalog, "all_entries", lambda kind=None: records)
    read = []

    class Cache:
        def get(self, path):
            read.append(path)
            if len(read) == 3:
                manager._stop.set()
            return DjMeta()

        def flush(self, force=False):
            pass

    monkeypatch.setattr(manager, "_dj_cache", lambda: Cache())
    analysed = []
    monkeypatch.setattr(manager, "_dj_analyse", lambda path, *args, **kwargs: analysed.append(path))
    manager._dj_backlog()
    assert len(read) == 3
    assert analysed == []


def test_a_closing_library_starts_no_analysis_job(manager_factory, monkeypatch):
    """ADR-0008-25. Shutdown has stopped the job runner: the tick must not start a job."""
    manager = manager_factory()
    monkeypatch.setattr(manager, "analysis_pending", lambda force=False: [{"path": "song"}])
    monkeypatch.setattr("autodj.model_identity.model_analysis_available", lambda: True)
    manager.stop_scan()
    with patch("autodj.jobs.get_manager") as jobs:
        assert not manager.schedule_analysis()
        jobs.assert_not_called()


def _scan_summaries(caplog, level=logging.INFO):
    """The scan summary lines logged at *level*; any other summary line fails the test."""
    records = [
        record
        for record in caplog.records
        if record.name == "autodj.library" and record.getMessage().startswith("Library scan ")
    ]
    caplog.clear()
    assert all(record.levelno == level for record in records)
    return [record.getMessage() for record in records]


def test_a_scan_logs_one_summary_only_when_something_changed(manager_factory, monkeypatch, caplog):
    """Per-song detail stays out of INFO; a periodic rescan that found nothing adds no line."""
    manager = manager_factory()
    now = [1000.0]
    manager._clock = lambda: now[0]
    monkeypatch.setattr(manager, "dj_analysis_enabled", lambda: False)
    monkeypatch.setattr(manager, "schedule_analysis", lambda: False)
    monkeypatch.setattr("autodj.intro_lift.measure_source_intro", lambda *args: 1.0)

    def decode(path, **kwargs):
        now[0] += 25
        return fake_basic(path, **kwargs)

    monkeypatch.setattr("autodj.library.analyze_basic", decode)
    music = manager.cfg.library.music_dir
    songs = [music / f"song-{number}.flac" for number in range(3)]
    for number, song in enumerate(songs):
        sf.write(song, np.ones((22050 + number, 2)) * 0.1, 22050)

    with caplog.at_level(logging.DEBUG, logger="autodj.library"):
        manager.scan()
        assert _scan_summaries(caplog) == [
            "Library scan finished in 1 min 15 s: 3 new, 3 decoded here"
        ]
        manager.scan()
        assert _scan_summaries(caplog, logging.DEBUG) == [
            "Library scan finished in 0 s: nothing changed"
        ]

        sf.write(songs[0], np.ones((44100, 2)) * 0.2, 22050)
        songs[1].rename(music / "renamed.flac")
        songs[2].unlink()
        manager.scan()
        assert _scan_summaries(caplog) == [
            "Library scan finished in 25 s: 1 changed, 1 moved, 1 gone, 1 decoded here"
        ]


def test_scan_summary_counts_work_left_to_a_worker_and_the_take_back(abandoned_scan, caplog):
    """One line names the songs sent to a worker, one the songs taken back."""
    manager, queue, now, _songs = abandoned_scan
    song = manager.cfg.library.music_dir / "late.flac"
    sf.write(song, np.ones((22050, 2)) * 0.3, 22050)
    with caplog.at_level(logging.INFO, logger="autodj.library"):
        manager.scan()
        assert _scan_summaries(caplog) == [
            "Library scan finished in 0 s: 1 new, 1 sent to an analysis worker"
        ]
        now[0] += 121
        tick_and_wait(manager)
    assert not queue.pending_scans()
    taken = [record.getMessage() for record in caplog.records if "abandoned" in record.message]
    assert taken == [
        "Server starting abandoned scan work: 3 songs",
        "Server finished abandoned scan work",
    ]
