"""Rescans complete audio preparation while retaining model and operator work."""

import sqlite3
from contextlib import closing
from unittest.mock import Mock

import numpy as np
import pytest
import soundfile as sf

from autodj.catalog import LibraryCatalog
from autodj.indexer import EMBEDDING_DIM
from autodj.library import LibraryManager, analyse_library
from tests.unit._catalog_vectors import vectors_snapshot
from tests.unit.test_library import Similarity, make_config


@pytest.mark.dj_analysis
def test_audio_then_similarity_reuses_features_and_rescan_preserves_vector(tmp_path, monkeypatch):
    import autodj.audio_analysis as audio_analysis

    cfg = make_config(tmp_path)
    song = cfg.library.music_dir / "song.flac"
    sf.write(song, 0.3 * np.sin(np.arange(44100 * 2) * 0.1), 44100)
    manager = LibraryManager(cfg, Similarity())
    source = manager.catalog.add_source(cfg.library.music_dir, analysis="audio")
    shared = Mock(wraps=audio_analysis.analyse_audio_features)
    monkeypatch.setattr(audio_analysis, "analyse_audio_features", shared)
    model = Mock()
    model.embed_array.return_value = np.ones(EMBEDDING_DIM, dtype=np.float32)
    try:
        manager.scan()
        assert shared.call_count == 1
        assert manager.catalog.get_audio_analysis(song) is not None
        assert not manager.catalog.has_vector(song)
        manager.catalog.update_source(source["id"], True, True, analysis="similarity")
        assert analyse_library(cfg, model) == 1
        assert model.embed_array.call_count == 1
        assert shared.call_count == 1
        before = vectors_snapshot(manager.catalog)[1].copy()
        manager.catalog.set_points(song, {"intro": 0.5}, source="user")
        manager.catalog.retag(song, {"bpm": 99, "key": 4})
        with closing(sqlite3.connect(manager.catalog.path)) as db:
            db.execute("DELETE FROM catalog_audio_analysis")
            db.commit()
        manager.scan()
        assert shared.call_count == 2
        assert model.embed_array.call_count == 1
        np.testing.assert_array_equal(vectors_snapshot(manager.catalog)[1], before)
        record = manager.catalog.record_for_path(song)
        assert (record["bpm"], record["key"], record["points"]["intro"]) == (99, 4, 0.5)
        manager.scan()
        assert shared.call_count == 2
    finally:
        manager.close()


@pytest.mark.parametrize("structure", [True, False])
def test_legacy_source_modes_migrate_once_and_highest_policy_wins(tmp_path, structure):
    path = tmp_path / "library.db"
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE catalog_sources (
                id TEXT PRIMARY KEY, path TEXT, kind TEXT, recursive INTEGER, advanced INTEGER);
            CREATE TABLE catalog_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        """)
        db.execute(
            "INSERT INTO catalog_settings VALUES ('dj_analysis',?)", (str(structure).lower(),)
        )
        db.executemany(
            "INSERT INTO catalog_sources VALUES (?,?,?,?,?)",
            [
                ("one", str(tmp_path), "music", 1, 0),
                ("two", str(tmp_path / "nested"), "music", 1, 1),
                ("three", str(tmp_path / "liners"), "liner", 1, 0),
            ],
        )
        db.commit()
    catalog = LibraryCatalog(path)
    try:
        modes = {s["id"]: s["analysis"] for s in catalog.sources()}
        assert modes == {
            "one": "audio" if structure else "basic",
            "two": "similarity",
            "three": "basic",
        }
        assert catalog.analysis_for_path(tmp_path / "nested" / "song.flac") == "similarity"
        catalog.update_source("one", True, True, analysis="similarity")
        catalog.update_source("two", True, False, analysis="basic")
        assert catalog.analysis_for_path(tmp_path / "nested" / "song.flac") == "similarity"
    finally:
        catalog.close()


@pytest.mark.parametrize(
    "weights,runtime,worker,available",
    [
        (None, False, False, False),
        (None, True, False, False),
        ("commit", False, False, False),
        ("commit", True, False, True),
        ("commit", False, True, True),
    ],
)
def test_similarity_choice_reflects_installed_capability(
    tmp_path, monkeypatch, weights, runtime, worker, available
):
    """ADR-0012-08. Offer model analysis only for an installed execution path."""
    manager = LibraryManager(make_config(tmp_path), Similarity())
    try:
        monkeypatch.setattr(manager, "_analysis_model_commit", lambda: weights)
        monkeypatch.setattr("autodj.model_identity.model_analysis_available", lambda: runtime)
        monkeypatch.setattr(manager.analysis_queue, "worker_active", lambda: worker)
        assert manager.similarity_available() is available
    finally:
        manager.close()
