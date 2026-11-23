"""Independent library copying and safe stage publication."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from autodj._bridge import LibraryServices
from autodj.audio_analysis import ANALYSIS_VERSION, AudioAnalysis
from autodj.catalog import _path
from autodj.dj_meta import DjMeta
from autodj.indexer import FEATURE_DIM, file_fingerprint
from autodj.library_runtime import Libraries
from tests.unit._catalog_vectors import set_vector
from tests.unit.test_library import Similarity, make_config, make_entry


def _pool(tmp_path: Path) -> Libraries:
    from autodj.library import LibraryManager

    cfg = make_config(tmp_path)
    sim = Similarity()
    manager = LibraryManager(cfg, sim)
    return Libraries(LibraryServices(cfg, sim, manager))


def _close(pool: Libraries) -> None:
    for services in pool.all():
        services.library.close()


def test_duplicate_copies_persistent_analysis_and_settings_but_not_runtime_state(tmp_path):
    """ADR-0023-09: duplicate copies catalog/DJ analysis, not station history or leases."""
    pool = _pool(tmp_path)
    try:
        source = pool.get("main").library
        song = source.cfg.library.music_dir / "song.flac"
        song.write_bytes(b"audio bytes")
        size, fingerprint = file_fingerprint(song)
        normalized = _path(song)
        entry = replace(make_entry(song, title="Copied title"), size=size, fingerprint=fingerprint)
        source.catalog.add_source(song.parent)
        source.catalog.put_entry(entry, points={"intro": 8.0}, point_source="user")
        source.catalog.set_points(song, {"outro": 140.0}, source="user")
        set_vector(source.catalog, song, np.full(FEATURE_DIM, 0.25, dtype=np.float32))
        result = AudioAnalysis(
            version=ANALYSIS_VERSION,
            features=[0.1] * 16,
            energy=0.1,
            key=1,
            mode=1,
            bpm=120,
            tempo_confidence=0.8,
        )
        assert source.catalog.store_audio_analysis(song, size, fingerprint, result)
        source.catalog.set_setting("auto_rescan_minutes", 30)
        source.catalog.set_setting("music_fade_drop_db", 17)
        source.catalog.set_setting("advanced_max_minutes", 0)
        source.catalog.set_setting("time_format", "12")
        source.catalog.set_setting("clock:default", {"name": "Morning"})
        source.catalog._db().execute("PRAGMA journal_mode=WAL")
        source.catalog._db().commit()

        source.catalog._db().execute(
            "INSERT INTO play_history(event_id,title,artist,duration,played_at) "
            "VALUES ('event','Title','Artist',10,'2026-01-01T00:00:00Z')"
        )
        source.catalog._db().execute(
            "INSERT INTO station_plays(station,path,last_played) VALUES ('default',?,123)",
            (normalized,),
        )
        source.catalog._db().execute(
            "UPDATE catalog_entries SET last_played=123 WHERE path=?", (normalized,)
        )
        source.catalog._db().commit()
        with closing(sqlite3.connect(source.catalog.path)) as db, db:
            db.execute(
                "INSERT INTO analysis_leases VALUES ('song', 'lease', 'worker', 'leased', 1, 'f', 10)"
            )
            db.execute(
                "INSERT INTO analysis_workers(token_id,name,device,last_seen,session_started) "
                "VALUES ('worker','Worker','device',1,1)"
            )
            db.execute(
                "INSERT INTO scan_pending(path,size,fingerprint,deadline) VALUES ('song',1,'f',1)"
            )
            db.execute("INSERT INTO scan_workers(token_id,enabled) VALUES ('worker',1)")
        cache = source._dj_cache()
        cache.set(str(song), DjMeta(intro_end_s=12, beats=[0.5, 1.0], analysed=True))
        source_root = source.cfg.index.library_dir
        (source_root / "related-artists.toml").write_text("[related]\n", encoding="utf-8")
        (source_root / "related-tracks.toml").write_text("[[groups]]\n", encoding="utf-8")

        definition = pool.duplicate("main", "copied", "Copied")
        copied = pool.get("copied").library
        assert definition.settings["music_dir"] == str(source.cfg.library.music_dir)
        assert copied.catalog.sources() == source.catalog.sources()
        assert song.read_bytes() == b"audio bytes"
        assert copied.entry_for_path(song).title == "Copied title"
        assert copied.catalog.points(song)["intro"] == 8.0
        assert copied.catalog.points(song)["outro"] == 140.0
        row = (
            copied.catalog._db()
            .execute(
                "SELECT v.vector FROM catalog_vectors v JOIN catalog_entries e "
                "ON e.id=v.entry_id WHERE e.path=?",
                (normalized,),
            )
            .fetchone()
        )
        assert np.frombuffer(row[0], dtype=np.float32).tolist() == [0.25] * FEATURE_DIM
        assert copied.catalog.get_audio_analysis(song) == result.model_dump(mode="json")
        assert copied.catalog.get_setting("auto_rescan_minutes") == 30
        assert copied.catalog.get_setting("music_fade_drop_db") == 17
        assert copied.catalog.get_setting("advanced_max_minutes") == 0
        assert copied.catalog.get_setting("time_format") is None
        assert copied.catalog.get_setting("clock:default") is None
        assert copied.catalog._db().execute("SELECT count(*) FROM play_history").fetchone()[0] == 0
        assert copied.catalog._db().execute("SELECT count(*) FROM station_plays").fetchone()[0] == 0
        assert (
            copied.catalog._db()
            .execute("SELECT last_played FROM catalog_entries WHERE path=?", (normalized,))
            .fetchone()[0]
            == 0
        )
        for table in ("analysis_leases", "analysis_workers", "scan_pending", "scan_workers"):
            assert copied.catalog._db().execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert copied._dj_cache().get(str(song)).beats == [0.5, 1.0]
        assert (copied.cfg.index.library_dir / "related-artists.toml").read_text() == "[related]\n"

        copied.catalog.set_points(song, {"intro": 10.0}, source="user")
        assert copied.catalog.points(song)["intro"] == 10.0
        assert source.catalog.points(song)["intro"] == 8.0
        assert source.catalog._db().execute("SELECT count(*) FROM play_history").fetchone()[0] == 1
        assert (
            source.catalog._db().execute("SELECT count(*) FROM analysis_leases").fetchone()[0] == 1
        )
    finally:
        _close(pool)


def test_duplicate_failure_removes_stage_and_preserves_source(tmp_path, monkeypatch):
    """ADR-0023-09: failed copies do not publish a library or damage the source."""
    pool = _pool(tmp_path)
    try:
        source = pool.get("main").library
        song = source.cfg.library.music_dir / "song.flac"
        song.write_bytes(b"source")
        source.catalog.add_source(song.parent)
        source.catalog.put_entry(make_entry(song, title="Source"))
        source.refresh()

        def fail_snapshot(_source, _stage):
            raise OSError("injected snapshot failure")

        monkeypatch.setattr("autodj.library_runtime.snapshot_library", fail_snapshot)
        with pytest.raises(OSError, match="injected snapshot failure"):
            pool.duplicate("main", "failed-copy", "Failed copy")
        assert [item.id for item in pool.registry.list()] == ["main"]
        assert not list(pool.registry.root.glob(".library-create-*"))
        assert source.entry_for_path(song).title == "Source"
    finally:
        _close(pool)
