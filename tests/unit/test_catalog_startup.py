"""Serving uses the current catalog even when the legacy index is stale or absent."""

from unittest.mock import patch

import numpy as np
import pytest
from click.testing import CliRunner

from autodj.catalog import LibraryCatalog, _path, catalog_counts
from autodj.cli import _load_index_for_serve, cli
from autodj.doctor import CheckStatus, _index_check
from autodj.indexer import FEATURE_DIM, build_faiss_index
from autodj.library import LibraryManager
from autodj.similarity import SimilarityIndex
from tests.unit.test_library import make_config, make_entry


def test_serve_uses_catalog_counts_and_can_seed_an_unanalyzed_song(tmp_path):
    """ADR-0006-01."""
    cfg = make_config(tmp_path)
    song = cfg.library.music_dir / "fresh.flac"
    song.write_bytes(b"audio")
    catalog_path = cfg.index.library_dir / "library.db"
    catalog = LibraryCatalog(catalog_path)
    try:
        catalog.add_source(cfg.library.music_dir)
        catalog.put_entry(make_entry(song, title="Fresh song"), points={"intro": 4.0})
    finally:
        catalog.close()
    (cfg.index.library_dir / "index-manifest.json").parent.mkdir(parents=True, exist_ok=True)
    (cfg.index.library_dir / "index-manifest.json").write_text("invalid old index")
    captured = {}

    def serve(**kwargs):
        library = kwargs["library"]
        captured.update(kwargs)
        assert library.entry_for_path(song).title == "Fresh song"
        assert library.catalog.record_for_path(song)["points"]["intro"] == 4.0

    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch("autodj.indexer.load_index", side_effect=AssertionError("old index read")),
        patch("autodj.server.serve", side_effect=serve),
    ):
        result = CliRunner().invoke(cli, ["serve", "--seed", "Fresh"])
    assert result.exit_code == 0, result.output
    assert "1 songs; 0 analyzed" in result.output
    assert captured["seed_entry"].title == "Fresh song"
    assert catalog_counts(catalog_path) == (1, 0)
    assert _index_check(cfg).status == CheckStatus.PASS


def test_catalog_survives_without_a_manifest_and_ignores_stale_similarity(tmp_path):
    """ADR-0006-01."""
    cfg = make_config(tmp_path)
    song = cfg.library.music_dir / "first.flac"
    song.write_bytes(b"audio")
    entry = make_entry(song)
    vectors = np.ones((1, FEATURE_DIM), dtype=np.float32)
    sim = SimilarityIndex(build_faiss_index(vectors), [entry])
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        catalog.add_source(cfg.library.music_dir)
        catalog.seed_entries([entry])
        catalog.seed_vectors([entry], vectors)
    manager = LibraryManager(cfg, sim)
    manager.close()
    # Even a supplied stale index must not resurrect records on a later startup.
    old = cfg.library.music_dir / "old.flac"
    old.write_bytes(b"audio")
    stale_sim = SimilarityIndex(build_faiss_index(vectors), [make_entry(old)])
    manager = LibraryManager(cfg, stale_sim)
    try:
        assert manager.snapshot()["tracks_ready"] == 1
        assert manager.snapshot()["tracks_indexed"] == 1
        assert manager.entry_for_path(old) is None
        assert stale_sim.entry_for_path(_path(song)) is not None
    finally:
        manager.close()
    assert _load_index_for_serve(cfg).ntotal == 0
    assert _index_check(cfg).status == CheckStatus.PASS


def test_catalog_reporting_does_not_repair_a_corrupt_catalog(tmp_path):
    cfg = make_config(tmp_path)
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    path = cfg.index.library_dir / "library.db"
    path.write_bytes(b"broken catalog")
    assert _index_check(cfg).status == CheckStatus.FAIL
    assert path.read_bytes() == b"broken catalog"


def test_legacy_only_startup_requires_explicit_migration_without_mutation(tmp_path):
    """ADR-0006-02."""
    from autodj.index_manifest import IndexConsistencyError

    cfg = make_config(tmp_path)
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    manifest = cfg.index.library_dir / "index-manifest.json"
    manifest.write_text("old data is inspected only by migration")
    for start in (
        lambda: _load_index_for_serve(cfg),
        lambda: LibraryManager(cfg, SimilarityIndex.empty()),
    ):
        with pytest.raises(IndexConsistencyError, match="migrate-index"):
            start()
        assert not (cfg.index.library_dir / "library.db").exists()
    assert _index_check(cfg).repair == "migrate-index"


@pytest.mark.parametrize("legacy", ["settings", "history", "pending"])
def test_current_catalog_requires_explicit_legacy_state_upgrade(tmp_path, legacy):
    import json

    from autodj.index_manifest import IndexConsistencyError

    cfg = make_config(tmp_path)
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        catalog.seed_sources(cfg.library.music_dir, (cfg.index.stations_dir / "default") / "liners")
        if legacy == "history":
            catalog.set_setting("play_history", [])
        elif legacy == "pending":
            catalog.set_setting("published_index_import_state", {"status": "pending"})
    if legacy == "settings":
        (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
        ((cfg.index.stations_dir / "default") / "web_state.json").write_text(
            json.dumps({"schema_version": 1})
        )
    with pytest.raises(IndexConsistencyError, match="migrate-index"):
        LibraryManager(cfg, SimilarityIndex.empty())
    assert _index_check(cfg).repair == "migrate-index"
