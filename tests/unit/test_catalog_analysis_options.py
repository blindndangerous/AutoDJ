import os
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

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


def _config(root: Path, music: Path, liners: Path | None = None) -> AutoDJConfig:
    return AutoDJConfig(
        library=LibraryConfig(music_dir=music, supported_formats=["flac"]),
        index=IndexConfig(index_dir=root / "index", model_dir=root / "models"),
        playback=PlaybackConfig(liners_folder=str(liners) if liners else None),
        model=ModelConfig(),
        huggingface=HuggingFaceConfig(),
        config_path=root / "config.toml",
    )


def _entry(path: Path, *, title: str | None = None) -> IndexEntry:
    return IndexEntry(
        str(path), title or path.stem, "Artist", "Album", "", 120, 2001, 180.0, 0.25, -1, -1, 0.0
    )


def _sim():
    return type(
        "Empty",
        (),
        {"faiss_index": type("I", (), {"ntotal": 0})(), "entries_snapshot": lambda self: ()},
    )()


def _catalog(tmp_path: Path, *, count: int = 3, liners: bool = False):
    music = tmp_path / "Music"
    music.mkdir()
    songs = []
    for i in range(count):
        song = music / f"song-{i}.flac"
        song.write_bytes(f"audio {i}".encode())
        songs.append(song)
    liner_root = tmp_path / "Liners" if liners else None
    cfg = _config(tmp_path, music, liner_root)
    manager = LibraryManager(cfg, _sim())
    for song in songs:
        manager.catalog.put_entry(_entry(song))
    liner = None
    if liner_root:
        liner_root.mkdir()
        liner = liner_root / "liner.wav"
        liner.write_bytes(b"liner")
        manager.catalog.add_source(liner_root, "liner", recursive=True, advanced=True)
        manager.catalog.put_entry(_entry(liner), "liner")
    manager.close()
    return cfg, songs, liner


def test_limit_and_workers_use_one_batch_and_force_reembeds_existing_vector(tmp_path):
    cfg, songs, _ = _catalog(tmp_path)
    seed = LibraryManager(cfg, _sim())
    old_vector = np.full(FEATURE_DIM, -1, dtype=np.float32)
    seed.catalog.set_vector(songs[0], old_vector)
    seed.close()

    seen = {}

    def embed(tracks, wrapper, workers, checkpoint, throttle_ms, report):
        del wrapper, throttle_ms, report
        seen.update(paths=[t.path for t in tracks], workers=workers)
        entries = [_entry(t.path) for t in tracks]
        vectors = [np.full(FEATURE_DIM, i, dtype=np.float32) for i, _ in enumerate(tracks)]
        for i in range(len(entries)):
            checkpoint(entries[: i + 1], vectors[: i + 1])
        return entries, vectors

    with patch("autodj.library._embed_new_tracks", side_effect=embed):
        assert analyse_library(cfg, object(), limit=2, force=True, workers=4) == 2
    assert seen == {"paths": songs[:2], "workers": 4}
    check = LibraryManager(cfg, _sim())
    try:
        np.testing.assert_array_equal(check.catalog.vectors_snapshot()[1][0], np.zeros(FEATURE_DIM))
    finally:
        check.close()


def test_analysis_keeps_existing_vector_and_skips_disabled_and_liner_sources_on_failure(tmp_path):
    cfg, songs, liner = _catalog(tmp_path, count=1, liners=True)
    setup = LibraryManager(cfg, _sim())
    music_source = setup.catalog.sources("music")[0]
    setup.catalog.update_source(music_source["id"], recursive=True, advanced=False)
    active_root = tmp_path / "Active"
    active_root.mkdir()
    active_song = active_root / "active.flac"
    active_song.write_bytes(b"active audio")
    setup.catalog.add_source(active_root, advanced=True)
    setup.catalog.put_entry(_entry(active_song))
    existing = np.full(FEATURE_DIM, 0.7, dtype=np.float32)
    setup.catalog.set_vector(songs[0], existing)
    setup.catalog.set_vector(active_song, existing)
    setup.catalog.set_vector(liner, existing)
    setup.catalog.set_points(songs[0], {"start": 3, "end": 170}, source="user")
    setup.catalog.set_setting("sentinel", "kept")
    setup.close()

    def fail(*args, **kwargs):
        raise RuntimeError("embed failed")

    with patch("autodj.library._embed_new_tracks", side_effect=fail), pytest.raises(RuntimeError):
        analyse_library(cfg, object(), force=True)
    check = LibraryManager(cfg, _sim())
    try:
        assert check.catalog.has_vector(songs[0])
        assert check.catalog.has_vector(active_song)
        assert check.catalog.has_vector(liner)
        entries, vectors = check.catalog.vectors_snapshot()
        by_path = {
            os.path.normcase(entry.path): vector
            for entry, vector in zip(entries, vectors, strict=True)
        }
        np.testing.assert_array_equal(by_path[os.path.normcase(str(songs[0]))], existing)
        np.testing.assert_array_equal(by_path[os.path.normcase(str(active_song))], existing)
        assert check.catalog.points(songs[0])["start"] == 3
        assert check.catalog.get_setting("sentinel") == "kept"
    finally:
        check.close()


def test_each_successful_result_is_checkpointed_durably(tmp_path):
    cfg, songs, _ = _catalog(tmp_path, count=2)

    def embed(tracks, wrapper, workers, checkpoint, throttle_ms, report):
        del wrapper, workers, throttle_ms, report
        entries = [_entry(track.path) for track in tracks]
        vectors = [np.full(FEATURE_DIM, i + 1, dtype=np.float32) for i in range(len(entries))]
        checkpoint(entries[:1], vectors[:1])
        checkpoint(entries, vectors)
        return entries, vectors

    with patch("autodj.library._embed_new_tracks", side_effect=embed):
        assert analyse_library(cfg, object()) == 2
    check = LibraryManager(cfg, _sim())
    try:
        assert all(check.catalog.has_vector(song) for song in songs)
        vectors = check.catalog.vectors_snapshot()[1]
        np.testing.assert_array_equal(
            vectors,
            np.asarray(
                [
                    np.ones(FEATURE_DIM, dtype=np.float32),
                    np.full(FEATURE_DIM, 2, dtype=np.float32),
                ]
            ),
        )
    finally:
        check.close()
