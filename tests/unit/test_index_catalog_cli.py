"""Catalog-backed behaviour of ``autodj index`` and beets enrichment."""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from click.testing import CliRunner

from autodj.catalog import LibraryCatalog
from autodj.cli import _load_index_for_serve, cli
from autodj.indexer import FEATURE_DIM, IndexEntry, enrich_from_beets, file_fingerprint
from tests.unit._catalog_vectors import set_vector, vectors_snapshot
from tests.unit._legacy_index import save_index
from tests.unit.test_library import fake_basic, fake_loudness, make_config


@pytest.fixture(autouse=True)
def simulated_model(monkeypatch):
    """Catalog tests simulate model execution; they never load a real model."""
    monkeypatch.setitem(sys.modules, "autodj.model", MagicMock())


def _source(cfg, folder: Path, *, advanced: bool = True) -> None:
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        catalog.seed_sources(cfg.library.music_dir, (cfg.index.stations_dir / "default") / "liners")
        catalog.add_source(folder, advanced=advanced)


def _invoke(cfg, args: list[str], *, deps=True):
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch("autodj.cli._load_index_for_serve", wraps=_load_index_for_serve),
        patch("autodj.cli._can_import", return_value=deps),
        patch("autodj.model_identity.model_analysis_available", return_value=True),
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
        patch("autodj.intro_lift.measure_source_intro", return_value=0.0),
    ):
        return CliRunner().invoke(cli, ["index", *args])


def _entry(path: Path, *, title: str = "Song") -> IndexEntry:
    return IndexEntry(
        str(path),
        title,
        "Artist",
        "Album",
        "",
        120.0,
        2000,
        180.0,
        0.2,
        -1,
        -1,
        0.0,
    )


def _fake_embed(calls: list[dict]):
    def embed(tracks, wrapper, workers, checkpoint, throttle_ms, report, gate=None):
        del wrapper, throttle_ms, report
        calls.append({"tracks": tracks, "workers": workers})
        for track in tracks:
            entry = _entry(track.path, title=track.title)
            entry.size, entry.fingerprint = file_fingerprint(track.path)
            checkpoint(
                [entry],
                [np.full(FEATURE_DIM, len(calls), dtype=np.float32)],
            )
        return [], []

    return embed


def test_index_uses_registered_folder_and_forwards_limit_force_workers(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    registered = tmp_path / "Elsewhere"
    registered.mkdir()
    song = registered / "a-registered.flac"
    other_song = registered / "b-registered.flac"
    song.write_bytes(b"song")
    other_song.write_bytes(b"other song")
    _source(cfg, registered)
    manifest = cfg.index.library_dir / "index-manifest.json"
    manifest.write_text("legacy manifest", encoding="utf-8")
    calls: list[dict] = []
    with (
        patch("autodj.model.download_model_if_needed", return_value=tmp_path / "model"),
        patch("autodj.model.load_model", return_value=object()),
        patch("autodj.library._embed_new_tracks", side_effect=_fake_embed(calls)),
    ):
        result = _invoke(
            cfg, ["--limit", "1", "--workers", "3", "--force", "--no-enrich", "--no-analyse"]
        )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert [track.path for track in calls[0]["tracks"]] == [song]
    assert calls[0]["workers"] == 3
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        assert catalog.has_vector(song)
        assert not catalog.has_vector(other_song)
        assert catalog.record_for_path(song)["title"] == "a-registered"
    assert manifest.read_text(encoding="utf-8") == "legacy manifest"


def test_index_saves_basic_song_before_missing_dependencies(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    _source(cfg, cfg.library.music_dir)
    song = cfg.library.music_dir / "song.flac"
    song.write_bytes(b"song")
    with patch("autodj.model.download_model_if_needed") as download:
        result = _invoke(cfg, ["--no-enrich", "--no-analyse"], deps=False)

    assert result.exit_code != 0
    assert "missing packages" in result.output
    download.assert_not_called()
    assert not (cfg.index.library_dir / "index-manifest.json").exists()
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        assert catalog.record_for_path(song) is not None
        assert not catalog.has_vector(song)


@pytest.mark.parametrize("disabled", [False, True])
def test_index_without_a_model_keeps_playable_tracks_and_pending_analysis(
    tmp_path, monkeypatch, disabled
):
    cfg = make_config(tmp_path)
    _source(cfg, cfg.library.music_dir)
    song = cfg.library.music_dir / "song.flac"
    song.write_bytes(b"song")
    monkeypatch.setenv("AUTODJ_NO_MODEL", "1" if disabled else "0")
    monkeypatch.setitem(sys.modules, "autodj.model", None)
    monkeypatch.setitem(sys.modules, "torch", None)
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch("autodj.library.analyze_basic", side_effect=fake_basic),
        patch("autodj.library.analyze_loudness", side_effect=fake_loudness),
        patch("autodj.intro_lift.measure_source_intro", return_value=0.0),
    ):
        result = CliRunner().invoke(cli, ["index", "--no-enrich", "--no-analyse"])
    assert result.exit_code == 0, result.output
    assert "1 songs available" in result.output
    assert "analysis pending" in result.output.lower()
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        assert catalog.record_for_path(song) is not None
        assert not catalog.has_vector(song)


def test_index_model_failure_leaves_basic_song_available(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    _source(cfg, cfg.library.music_dir)
    song = cfg.library.music_dir / "song.flac"
    song.write_bytes(b"song")
    with patch(
        "autodj.model.download_model_if_needed", side_effect=RuntimeError("model unavailable")
    ):
        result = _invoke(cfg, ["--no-enrich", "--no-analyse"])

    assert result.exit_code != 0
    assert "model unavailable" in result.output
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        assert catalog.record_for_path(song) is not None
        assert not catalog.has_vector(song)


def test_index_skips_model_for_disabled_source(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    registered = tmp_path / "Disabled"
    registered.mkdir()
    song = registered / "song.flac"
    song.write_bytes(b"song")
    _source(cfg, registered, advanced=False)
    with patch("autodj.model.download_model_if_needed") as download:
        result = _invoke(cfg, ["--no-enrich", "--no-analyse"])

    assert result.exit_code == 0, result.output
    download.assert_not_called()
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        assert catalog.record_for_path(song) is not None
        assert not catalog.has_vector(song)


def test_index_keeps_default_postpasses_and_honours_skip_flags(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.library.beets_db = tmp_path / "beets.db"
    with (
        patch("autodj.indexer.enrich_from_beets", return_value=(0, 0)) as enrich,
        patch("autodj.indexer.backfill_dj_meta") as analyse,
    ):
        result = _invoke(cfg, [])
        assert result.exit_code == 0, result.output
        enrich.assert_called_once()
        analyse.assert_called_once()

        enrich.reset_mock()
        analyse.reset_mock()
        result = _invoke(cfg, ["--no-enrich", "--no-analyse"])
        assert result.exit_code == 0, result.output
        enrich.assert_not_called()
        analyse.assert_not_called()


def test_force_replaces_vector_and_preserves_user_data(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    song = cfg.library.music_dir / "song.flac"
    song.write_bytes(b"song")
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        catalog.add_source(cfg.library.music_dir)
        catalog.put_entry(_entry(song))
        set_vector(catalog, song, np.zeros(FEATURE_DIM, dtype=np.float32))
        catalog.set_points(song, {"intro": 12.0}, source="user")
        catalog.write_history(
            [
                {
                    "event_id": "play-1",
                    "path": str(song),
                    "title": "Song",
                    "artist": "Artist",
                    "duration": 180.0,
                    "played_at": "2026-01-01T00:00:00+00:00",
                }
            ]
        )
    calls: list[dict] = []
    with (
        patch("autodj.model.download_model_if_needed", return_value=tmp_path / "model"),
        patch("autodj.model.load_model", return_value=object()),
        patch("autodj.library._embed_new_tracks", side_effect=_fake_embed(calls)),
    ):
        result = _invoke(cfg, ["--force", "--no-enrich", "--no-analyse"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        record = catalog.record_for_path(song)
        assert record["points"]["intro"] == 12.0
        assert record["point_sources"]["intro"] == "user"
        assert catalog.recent_history() and catalog.recent_history()[0]["event_id"] == "play-1"
        _, vectors = vectors_snapshot(catalog)
        assert np.all(vectors[0] == 1)


def test_catalog_beets_enrichment_keeps_vector_and_cues_without_legacy_files(
    tmp_path: Path,
) -> None:
    cfg = make_config(tmp_path)
    song = cfg.library.music_dir / "song.flac"
    song.write_bytes(b"song")
    beets_db = tmp_path / "beets.db"
    with closing(sqlite3.connect(beets_db)) as db, db:
        db.execute(
            "CREATE TABLE items (path BLOB, title TEXT, artist TEXT, album TEXT, genre TEXT, "
            "bpm REAL, year INTEGER, length REAL, initial_key TEXT)"
        )
        db.execute(
            "INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(song).encode(),
                "Updated",
                "New Artist",
                "New Album",
                "Jazz",
                90.0,
                2001,
                181.0,
                "Am",
            ),
        )
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        catalog.add_source(cfg.library.music_dir)
        catalog.put_entry(_entry(song))
        set_vector(catalog, song, np.full(FEATURE_DIM, 0.25, dtype=np.float32))
        catalog.set_points(song, {"intro": 8.0}, source="user")

    updated, total = enrich_from_beets(cfg.index.library_dir, cfg.library.music_dir, beets_db)

    assert (updated, total) == (1, 1)
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        record = catalog.record_for_path(song)
        assert record["title"] == "Updated"
        assert record["points"]["intro"] == 8.0
        assert record["point_sources"]["intro"] == "user"
        _, vectors = vectors_snapshot(catalog)
        assert np.all(vectors[0] == 0.25)
    assert not (cfg.index.library_dir / "index-manifest.json").exists()


def test_index_requires_explicit_migration_without_rewriting_manifest(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    song = cfg.library.music_dir / "old.flac"
    song.write_bytes(b"song")
    vectors = np.ones((1, FEATURE_DIM), dtype=np.float32)
    save_index(
        [_entry(song)],
        vectors,
        cfg.index.library_dir,
        music_dir=cfg.library.music_dir,
        base_generation=0,
    )
    manifest = cfg.index.library_dir / "index-manifest.json"
    original = manifest.read_bytes()
    with patch("autodj.model.download_model_if_needed") as download:
        result = _invoke(cfg, ["--no-enrich", "--no-analyse"])
    assert result.exit_code != 0
    assert "migrate-index" in result.output
    download.assert_not_called()
    assert manifest.read_bytes() == original
    assert not (cfg.index.library_dir / "library.db").exists()


def test_index_force_cannot_bypass_legacy_migration(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    manifest = cfg.index.library_dir / "index-manifest.json"
    manifest.write_text("obsolete", encoding="utf-8")
    result = _invoke(cfg, ["--force", "--no-enrich", "--no-analyse"])
    assert result.exit_code != 0
    assert "migrate-index" in result.output
    assert not (cfg.index.library_dir / "library.db").exists()
    assert manifest.read_text(encoding="utf-8") == "obsolete"


def test_index_backfills_relative_music_root_without_pruning_disabled_folder(
    tmp_path: Path, monkeypatch
) -> None:
    from autodj.dj_meta import DjMeta, close_cache, get_cache

    cfg = make_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg.library.music_dir = Path("Music")
    song = cfg.library.music_dir / "song.flac"
    song.write_bytes(b"song")
    disabled = cfg.library.music_dir / "Disabled"
    disabled.mkdir()
    other = disabled / "other.flac"
    other.write_bytes(b"other")
    _source(cfg, cfg.library.music_dir, advanced=False)
    # A narrower enabled folder should be analysed while the other stays disabled.
    enabled = cfg.library.music_dir / "Enabled"
    enabled.mkdir()
    song = song.rename(enabled / song.name)
    _source(cfg, enabled)
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        catalog.put_entry(_entry(song))
        set_vector(catalog, song, np.ones(FEATURE_DIM, dtype=np.float32))
        catalog.set_points(song, {"intro": 12.0}, source="user")
    close_cache()
    try:
        cache = get_cache(cfg.index.library_dir, music_dir=cfg.library.music_dir.resolve())
        cache.set(str(other.resolve()), DjMeta(analysed=True, intro_end_s=7.0))
        cache.flush(force=True)
        with (
            patch(
                "autodj.indexer._analyse_one_track",
                return_value=DjMeta(analysed=True, intro_end_s=5.0, outro_start_s=160.0),
            ) as analyse,
            patch(
                "autodj.model.download_model_if_needed",
                side_effect=AssertionError("unexpected model load"),
            ),
        ):
            result = _invoke(cfg, ["--no-enrich"])
        assert result.exit_code == 0, result.output
        analyse.assert_called_once()
        assert Path(analyse.call_args.args[0]) == song.resolve()
        assert cache.get(str(other.resolve())).intro_end_s == 7.0
        with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
            record = catalog.record_for_path(song)
            assert record["points"]["intro"] == 12.0
            assert record["point_sources"]["intro"] == "user"
            # The beat analysis no longer writes cue points.
            assert record["points"].get("outro") != 160.0
    finally:
        close_cache()


def test_force_preserves_catalog_vector_after_failed_reanalysis(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    song = cfg.library.music_dir / "old.flac"
    song.write_bytes(b"song")
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        catalog.seed_sources(cfg.library.music_dir, (cfg.index.stations_dir / "default") / "liners")
        catalog.seed_entries([_entry(song)])
        catalog.seed_vectors([_entry(song)], np.ones((1, FEATURE_DIM), dtype=np.float32))
    with patch("autodj.model.download_model_if_needed", side_effect=RuntimeError("unavailable")):
        result = _invoke(cfg, ["--force", "--no-enrich", "--no-analyse"])
    assert result.exit_code != 0
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        assert catalog.has_vector(song)


def test_index_reports_unreadable_file_without_crashing(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    _source(cfg, cfg.library.music_dir)
    (cfg.library.music_dir / "broken.flac").write_bytes(b"broken")
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch("autodj.library.analyze_basic", side_effect=ValueError("bad audio")),
    ):
        result = CliRunner().invoke(cli, ["index", "--no-enrich", "--no-analyse"])
    assert result.exit_code == 0, result.output
    assert "broken.flac" in result.output
    assert "bad audio" in result.output
