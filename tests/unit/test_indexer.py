"""Unit tests for autodj.indexer.

The MuQ model and librosa are mocked so tests run without audio files or
model downloads. Vector math and FAISS operations use real numpy/faiss.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import faiss
import numpy as np
import pytest

from autodj.beets import Track
from autodj.indexer import (
    FEATURE_DIM,
    IndexEntry,
    _combine_features,
    _extract_librosa_features,
    _resolve_beets_path,
    build_faiss_index,
    load_index,
    save_index,
    walk_music_dir,
)
from autodj.model import EMBEDDING_DIM

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_track(path: str, title: str = "Song", artist: str = "Artist") -> Track:
    return Track(
        path=Path(path),
        title=title,
        artist=artist,
        album="Album",
        genre="Rock",
        bpm=120.0,
        year=2000,
        length=180.0,
    )


def _random_embedding(dim: int = EMBEDDING_DIM) -> np.ndarray:
    v = np.random.randn(dim).astype(np.float32)
    return v / np.linalg.norm(v)


# ---------------------------------------------------------------------------
# walk_music_dir
# ---------------------------------------------------------------------------


class TestWalkMusicDir:
    def test_finds_mp3_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.mp3").touch()
        (tmp_path / "b.flac").touch()
        (tmp_path / "c.txt").touch()
        paths = walk_music_dir(tmp_path, ["mp3", "flac"])
        assert Path(tmp_path / "a.mp3") in paths
        assert Path(tmp_path / "b.flac") in paths
        assert Path(tmp_path / "c.txt") not in paths

    def test_recurses_subdirectories(self, tmp_path: Path) -> None:
        sub = tmp_path / "Artist" / "Album"
        sub.mkdir(parents=True)
        (sub / "song.flac").touch()
        paths = walk_music_dir(tmp_path, ["flac"])
        assert sub / "song.flac" in paths

    def test_empty_dir_returns_empty_list(self, tmp_path: Path) -> None:
        assert walk_music_dir(tmp_path, ["mp3"]) == []

    def test_raises_if_dir_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            walk_music_dir(tmp_path / "nonexistent", ["mp3"])


# ---------------------------------------------------------------------------
# _extract_librosa_features
# ---------------------------------------------------------------------------


class TestExtractLibrosaFeatures:
    def _setup_librosa_mock(self, mock_librosa, fake_audio, fake_sr) -> None:
        """Configure a librosa mock to return plausible shapes."""
        mock_librosa.feature.rms.return_value = np.array([[[0.1]]])
        mock_librosa.feature.spectral_centroid.return_value = np.array([[[500.0]]])
        mock_librosa.feature.zero_crossing_rate.return_value = np.array([[[0.05]]])
        mock_librosa.feature.chroma_stft.return_value = np.ones((12, 10))
        mock_librosa.onset.onset_strength.return_value = np.array([0.5, 0.6, 0.4])
        mock_librosa.beat.beat_track.return_value = (120.0, np.array([10, 20, 30, 40, 50]))

    def test_returns_16_dim_vector(self, tmp_path: Path) -> None:
        fake_audio = np.zeros(22050, dtype=np.float32)
        fake_sr = 22050

        with (
            patch("autodj.indexer.sf") as mock_sf,
            patch("autodj.indexer.librosa") as mock_librosa,
        ):
            mock_sf.read.return_value = (fake_audio, fake_sr)
            self._setup_librosa_mock(mock_librosa, fake_audio, fake_sr)

            vec, audio, sr, _ = _extract_librosa_features(tmp_path / "song.flac")

        assert vec.shape == (16,)
        assert audio.dtype == np.float32
        assert sr == fake_sr

    def test_extra_meta_has_expected_keys(self, tmp_path: Path) -> None:
        fake_audio = np.zeros(22050, dtype=np.float32)
        fake_sr = 22050

        with (
            patch("autodj.indexer.sf") as mock_sf,
            patch("autodj.indexer.librosa") as mock_librosa,
        ):
            mock_sf.read.return_value = (fake_audio, fake_sr)
            self._setup_librosa_mock(mock_librosa, fake_audio, fake_sr)

            _, _, _, extra_meta = _extract_librosa_features(tmp_path / "song.flac")

        assert "energy" in extra_meta
        assert "key" in extra_meta
        assert "mode" in extra_meta
        assert "tempo_confidence" in extra_meta
        assert (extra_meta["key"], extra_meta["mode"]) == (-1, -1)
        assert 0.0 <= extra_meta["tempo_confidence"] <= 1.0

    def test_vector_is_finite(self, tmp_path: Path) -> None:
        fake_audio = np.random.randn(22050).astype(np.float32)
        fake_sr = 22050

        with (
            patch("autodj.indexer.sf") as mock_sf,
            patch("autodj.indexer.librosa") as mock_librosa,
        ):
            mock_sf.read.return_value = (fake_audio, fake_sr)
            self._setup_librosa_mock(mock_librosa, fake_audio, fake_sr)

            vec, _, _, _ = _extract_librosa_features(tmp_path / "song.flac")

        assert np.isfinite(vec).all()


# ---------------------------------------------------------------------------
# _combine_features
# ---------------------------------------------------------------------------


class TestCombineFeatures:
    def test_output_dim_matches_feature_dim(self) -> None:
        embedding_vec = _random_embedding(EMBEDDING_DIM)
        librosa_vec = np.random.randn(16).astype(np.float32)
        result = _combine_features(embedding_vec, librosa_vec)
        assert result.shape == (FEATURE_DIM,)

    def test_output_is_l2_normalized(self) -> None:
        embedding_vec = _random_embedding(EMBEDDING_DIM)
        librosa_vec = np.random.randn(16).astype(np.float32)
        result = _combine_features(embedding_vec, librosa_vec)
        norm = np.linalg.norm(result)
        assert abs(norm - 1.0) < 1e-5

    def test_output_is_float32(self) -> None:
        embedding_vec = _random_embedding(EMBEDDING_DIM)
        librosa_vec = np.random.randn(16).astype(np.float32)
        result = _combine_features(embedding_vec, librosa_vec)
        assert result.dtype == np.float32


# ---------------------------------------------------------------------------
# build_faiss_index
# ---------------------------------------------------------------------------


class TestBuildFaissIndex:
    def test_builds_index_with_correct_size(self) -> None:
        n = 5
        vectors = np.random.randn(n, FEATURE_DIM).astype(np.float32)
        # L2-normalize
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors /= norms

        index = build_faiss_index(vectors)
        assert index.ntotal == n

    def test_index_is_inner_product(self) -> None:
        vectors = np.random.randn(3, FEATURE_DIM).astype(np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors /= norms
        index = build_faiss_index(vectors)
        assert isinstance(index, faiss.IndexFlatIP)

    def test_nearest_neighbor_is_self(self) -> None:
        """Querying a vector should return itself as the top result."""
        vectors = np.random.randn(10, FEATURE_DIM).astype(np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors /= norms
        index = build_faiss_index(vectors)

        query = vectors[3:4]  # shape [1, FEATURE_DIM]
        _, indices = index.search(query, 1)
        assert indices[0][0] == 3


# ---------------------------------------------------------------------------
# save_index / load_index
# ---------------------------------------------------------------------------


class TestSaveLoadIndex:
    def _make_entries(self, n: int) -> tuple[list[IndexEntry], np.ndarray]:
        entries = [
            IndexEntry(
                path=f"song_{i}.flac",
                title=f"Song {i}",
                artist="Artist",
                album="Album",
                genre="Rock",
                bpm=120.0,
                year=2000,
                length=180.0,
                energy=0.05,
                key=0,
                mode=1,
                tempo_confidence=0.8,
            )
            for i in range(n)
        ]
        vectors = np.random.randn(n, FEATURE_DIM).astype(np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors /= norms
        return entries, vectors

    def test_round_trip(self, tmp_path: Path) -> None:
        entries, vectors = self._make_entries(5)
        index_dir = tmp_path / "index"
        index_dir.mkdir()

        save_index(entries, vectors, index_dir)
        loaded_entries, loaded_index = load_index(index_dir)

        assert len(loaded_entries) == 5
        assert loaded_index.ntotal == 5

    def test_prune_all_never_reuses_stale_snapshot_token(self, tmp_path: Path) -> None:
        from autodj.index_manifest import IndexConsistencyError, current_snapshot_token
        from autodj.indexer import _delete_index_files

        entries, vectors = self._make_entries(1)
        save_index(entries, vectors, tmp_path)
        stale = current_snapshot_token(tmp_path)
        _delete_index_files(tmp_path, expected_snapshot=stale)
        save_index(entries, vectors, tmp_path)

        with pytest.raises(IndexConsistencyError, match="expected"):
            save_index(entries, vectors, tmp_path, expected_snapshot=stale)

    def test_tracks_db_written(self, tmp_path: Path) -> None:
        entries, vectors = self._make_entries(3)
        index_dir = tmp_path / "index"
        index_dir.mkdir()

        save_index(entries, vectors, index_dir)

        db_path = index_dir / "tracks.db"
        assert db_path.exists()
        import sqlite3 as _sql

        conn = _sql.connect(db_path)
        try:
            rows = conn.execute("SELECT path FROM tracks ORDER BY vec_row ASC").fetchall()
        finally:
            conn.close()
        assert len(rows) == 3
        assert rows[0][0] == "song_0.flac"

    def test_faiss_index_file_written(self, tmp_path: Path) -> None:
        entries, vectors = self._make_entries(3)
        index_dir = tmp_path / "index"
        index_dir.mkdir()

        save_index(entries, vectors, index_dir)

        assert (index_dir / "vectors.index").exists()

    def test_load_raises_if_index_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_index(tmp_path / "nonexistent")

    def test_load_ignores_unpublished_metadata_ahead_crash(self, tmp_path: Path) -> None:
        from autodj.indexer import _save_tracks_metadata

        entries, vectors = self._make_entries(4)
        save_index(entries[:3], vectors[:3], tmp_path)
        _save_tracks_metadata(entries, tmp_path, music_dir=None)
        loaded, faiss_index = load_index(tmp_path)
        assert len(loaded) == faiss_index.ntotal == 3

    def test_load_ignores_unpublished_vectors_ahead_crash(self, tmp_path: Path) -> None:
        from autodj.indexer import _save_vectors

        entries, vectors = self._make_entries(4)
        save_index(entries[:3], vectors[:3], tmp_path)
        _save_vectors(vectors, tmp_path)
        loaded, faiss_index = load_index(tmp_path)
        assert len(loaded) == faiss_index.ntotal == 3

    def test_restart_restores_canonical_working_files_from_live_generation(
        self,
        tmp_path: Path,
    ) -> None:
        from autodj.index_manifest import read_manifest, sha256_file
        from autodj.indexer import _load_existing_artifacts, _save_vectors

        entries, vectors = self._make_entries(3)
        save_index(entries, vectors, tmp_path, music_dir=tmp_path)
        manifest = read_manifest(tmp_path)
        assert manifest is not None
        _save_vectors(np.flip(vectors, axis=0).copy(), tmp_path)
        _load_existing_artifacts(tmp_path, tmp_path)
        assert sha256_file(tmp_path / "tracks.db") == manifest.tracks_sha256
        assert sha256_file(tmp_path / "vectors.index") == manifest.vectors_sha256
        assert not (tmp_path / "tracks.db-wal").exists()

    def test_load_rejects_same_count_vector_mix(self, tmp_path: Path) -> None:
        from autodj.index_manifest import IndexConsistencyError, read_manifest
        from autodj.indexer import _save_vectors

        entries, vectors = self._make_entries(3)
        save_index(entries, vectors, tmp_path)
        manifest = read_manifest(tmp_path)
        assert manifest is not None
        _save_vectors(np.flip(vectors, axis=0).copy(), tmp_path)
        (tmp_path / manifest.vectors_file).write_bytes((tmp_path / "vectors.index").read_bytes())
        with pytest.raises(IndexConsistencyError, match="vectors SHA-256"):
            load_index(tmp_path)

    def test_load_rejects_same_count_metadata_mix(self, tmp_path: Path) -> None:
        import sqlite3

        from autodj.index_manifest import IndexConsistencyError, read_manifest
        from autodj.indexer import _save_tracks_metadata

        old_entries, vectors = self._make_entries(3)
        save_index(old_entries, vectors, tmp_path, music_dir=tmp_path)
        manifest = read_manifest(tmp_path)
        assert manifest is not None
        mixed_entries, _ = self._make_entries(3)
        for index, entry in enumerate(mixed_entries):
            entry.path = f"other/song_{index}.flac"
        _save_tracks_metadata(mixed_entries, tmp_path, music_dir=tmp_path)
        conn = sqlite3.connect(tmp_path / "tracks.db", isolation_level=None)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        (tmp_path / manifest.tracks_file).write_bytes((tmp_path / "tracks.db").read_bytes())
        with pytest.raises(IndexConsistencyError, match="tracks SHA-256"):
            load_index(tmp_path, music_dir=tmp_path)

    def test_load_refuses_absolute_track_path(self, tmp_path: Path) -> None:
        import sqlite3

        from autodj.index_manifest import UnsupportedIndexError, publish_manifest
        from autodj.indexer import _save_vectors

        entries, vectors = self._make_entries(1)
        save_index(entries, vectors, tmp_path)
        conn = sqlite3.connect(tmp_path / "tracks.db")
        try:
            conn.execute("UPDATE tracks SET path = 'Z:/Music/song_0.flac'")
            conn.commit()
        finally:
            conn.close()
        _save_vectors(vectors, tmp_path)
        publish_manifest(tmp_path, 1)

        with pytest.raises(UnsupportedIndexError, match=r"absolute path Z:/Music/song_0.flac"):
            load_index(tmp_path, music_dir=tmp_path)

    def test_failed_second_save_does_not_publish_generation(self, tmp_path: Path) -> None:
        from autodj.index_manifest import read_manifest

        entries, vectors = self._make_entries(4)
        save_index(entries[:3], vectors[:3], tmp_path)
        first = read_manifest(tmp_path)
        assert first is not None
        with (
            patch("autodj.indexer._save_tracks_metadata", side_effect=OSError("injected")),
            pytest.raises(OSError, match="injected"),
        ):
            save_index(entries, vectors, tmp_path)
        current = read_manifest(tmp_path)
        assert current is not None
        assert current.generation == first.generation == 1
        loaded, loaded_faiss = load_index(tmp_path)
        assert len(loaded) == loaded_faiss.ntotal == 3

    def test_load_rejects_manifest_change_during_artifact_read(self, tmp_path: Path) -> None:
        from autodj.index_manifest import IndexConsistencyError, publish_manifest

        entries, vectors = self._make_entries(3)
        save_index(entries, vectors, tmp_path)
        real_read_index = faiss.read_index
        raced = False

        def read_then_publish(path: str):
            nonlocal raced
            loaded = real_read_index(path)
            if not raced:
                raced = True
                publish_manifest(tmp_path, 3)
            return loaded

        with (
            patch("autodj.indexer.faiss.read_index", side_effect=read_then_publish),
            pytest.raises(IndexConsistencyError, match="changed during load"),
        ):
            load_index(tmp_path)

    def test_metadata_path_preserved(self, tmp_path: Path) -> None:
        entries, vectors = self._make_entries(2)
        index_dir = tmp_path / "index"
        index_dir.mkdir()

        save_index(entries, vectors, index_dir)
        loaded_entries, _ = load_index(index_dir)

        assert loaded_entries[0].path == "song_0.flac"


# ---------------------------------------------------------------------------
# _resolve_beets_path — relative path resolution against music_dir
# ---------------------------------------------------------------------------


class TestResolveBeetsPath:
    """Tests for resolving beets-stored paths against the local music_dir.

    Recent beets versions store paths *relative* to the library ``directory``.
    AutoDJ resolves them by prepending ``music_dir``; absolute paths pass through.
    """

    def test_relative_path_is_prepended_with_music_dir(self) -> None:
        path = Path("10 Years/2001 - Into the Half Moon - flac/01 Fallaway.flac")
        result = _resolve_beets_path(path, Path("Z:/Music"))
        result_str = str(result).replace("\\", "/")
        assert result_str.startswith("Z:/Music/")
        assert result_str.endswith("/01 Fallaway.flac")
        assert "10 Years" in result_str

    def test_posix_absolute_path_returned_unchanged(self) -> None:
        path = Path("/volume1/Library/music/Hollow Front/01.flac")
        result = _resolve_beets_path(path, Path("Z:/Music"))
        assert str(result).replace("\\", "/") == "/volume1/Library/music/Hollow Front/01.flac"

    def test_windows_absolute_path_returned_unchanged(self) -> None:
        path = Path("Z:/OtherMount/song.flac")
        result = _resolve_beets_path(path, Path("Z:/Music"))
        assert str(result).replace("\\", "/") == "Z:/OtherMount/song.flac"

    def test_relative_path_with_backslashes(self) -> None:
        """A relative path stored with backslashes (Windows beets) works."""
        path = Path("Artist\\Album\\song.flac")
        result = _resolve_beets_path(path, Path("Z:/Music"))
        result_str = str(result).replace("\\", "/")
        assert result_str.startswith("Z:/Music/")
        assert "song.flac" in result_str

    def test_deep_nested_relative_path(self) -> None:
        path = Path("A/B/C/D/song.flac")
        result = _resolve_beets_path(path, Path("Z:/Music"))
        parts = str(result).replace("\\", "/").split("/")
        assert "A" in parts
        assert "song.flac" in parts
        assert parts[0] == "Z:" or parts[1] == "Music"

    def test_returns_path_object(self) -> None:
        path = Path("Artist/song.flac")
        result = _resolve_beets_path(path, Path("Z:/Music"))
        assert isinstance(result, Path)

    def test_leading_slash_relative_treated_as_absolute(self) -> None:
        """A POSIX-rooted path (starts with /) is treated as absolute, not relative."""
        path = Path("/Artist/song.flac")
        result = _resolve_beets_path(path, Path("Z:/Music"))
        # Should be left alone as absolute; not prepended with music_dir
        assert "Z:/Music" not in str(result).replace("\\", "/")


# ---------------------------------------------------------------------------
# prune_index
# ---------------------------------------------------------------------------


class TestPruneIndex:
    def _distinctive_entries(
        self,
        tmp_path: Path,
        *,
        missing_rows: set[int] | None = None,
    ) -> tuple[list[IndexEntry], np.ndarray]:
        missing_rows = missing_rows or set()
        entries: list[IndexEntry] = []
        vectors = np.zeros((3, FEATURE_DIM), dtype=np.float32)
        for row, marker in enumerate((3, 7, 11)):
            path = tmp_path / f"song_{row}.flac"
            if row not in missing_rows:
                path.write_bytes(b"")
            entries.append(
                IndexEntry(
                    path=str(path),
                    title=f"Original {row}",
                    artist="Artist",
                    album="Album",
                    genre="Genre",
                    bpm=120.0,
                    year=2026,
                    length=180.0,
                    energy=0.5,
                    key=0,
                    mode=1,
                    tempo_confidence=0.8,
                )
            )
            vectors[row, marker] = 1.0
        return entries, vectors

    def _save_with_files(self, tmp_path: Path, n_present: int, n_missing: int) -> Path:
        """Build an index where some entries have real files, others don't."""
        from autodj.indexer import save_index

        entries: list[IndexEntry] = []
        for i in range(n_present):
            f = tmp_path / f"present_{i}.flac"
            f.write_bytes(b"")
            entries.append(
                IndexEntry(
                    path=str(f),
                    title=f"P{i}",
                    artist="A",
                    album="L",
                    genre="G",
                    bpm=100.0,
                    year=2020,
                    length=180.0,
                    energy=0.05,
                    key=0,
                    mode=1,
                    tempo_confidence=0.5,
                )
            )
        for i in range(n_missing):
            entries.append(
                IndexEntry(
                    path=str(tmp_path / f"missing_{i}.flac"),
                    title=f"M{i}",
                    artist="A",
                    album="L",
                    genre="G",
                    bpm=100.0,
                    year=2020,
                    length=180.0,
                    energy=0.05,
                    key=0,
                    mode=1,
                    tempo_confidence=0.5,
                )
            )
        vectors = np.random.randn(len(entries), FEATURE_DIM).astype(np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        index_dir = tmp_path / "idx"
        index_dir.mkdir()
        save_index(entries, vectors, index_dir, music_dir=tmp_path)
        return index_dir

    def test_no_index_returns_zero(self, tmp_path: Path) -> None:
        from autodj.indexer import prune_index

        assert prune_index(tmp_path / "noidx") == (0, 0)

    def test_prunes_missing_files(self, tmp_path: Path) -> None:
        from autodj.indexer import prune_index

        idx = self._save_with_files(tmp_path, n_present=10, n_missing=2)
        removed, kept = prune_index(idx, music_dir=tmp_path)
        assert removed == 2
        assert kept == 10

    def test_no_missing_returns_zero_removed(self, tmp_path: Path) -> None:
        from autodj.indexer import prune_index

        idx = self._save_with_files(tmp_path, n_present=5, n_missing=0)
        removed, kept = prune_index(idx, music_dir=tmp_path)
        assert removed == 0
        assert kept == 5

    def test_prune_ignores_dirty_canonical_metadata_and_preserves_vector_mapping(
        self,
        tmp_path: Path,
    ) -> None:
        from autodj.indexer import (
            _save_tracks_metadata,
            load_index,
            prune_index,
            save_index,
        )

        entries, vectors = self._distinctive_entries(tmp_path, missing_rows={1})
        idx = tmp_path / "idx"
        save_index(entries, vectors, idx, music_dir=tmp_path)
        dirty_entries = [entries[1], entries[0], entries[2]]
        _save_tracks_metadata(dirty_entries, idx, music_dir=tmp_path)

        assert prune_index(idx, allow_mass_prune=True, music_dir=tmp_path) == (1, 2)
        loaded, loaded_vectors = load_index(idx, music_dir=tmp_path)
        assert [Path(entry.path).name for entry in loaded] == ["song_0.flac", "song_2.flac"]
        assert [int(np.argmax(loaded_vectors.reconstruct(row))) for row in range(2)] == [3, 11]

    def test_prune_rejects_generation_race_without_overwriting_newer_snapshot(
        self,
        tmp_path: Path,
    ) -> None:
        from dataclasses import replace

        import autodj.indexer as indexer
        from autodj.index_manifest import IndexConsistencyError, read_manifest
        from autodj.indexer import load_index, prune_index, save_index

        entries, vectors = self._distinctive_entries(tmp_path, missing_rows={2})
        idx = tmp_path / "idx"
        save_index(entries, vectors, idx, music_dir=tmp_path)
        first = read_manifest(idx)
        assert first is not None
        concurrent_entries = [
            replace(entry, title=f"Concurrent {row}") for row, entry in enumerate(entries)
        ]
        concurrent_vectors = np.zeros_like(vectors)
        concurrent_vectors[0, 13] = 1.0
        concurrent_vectors[1, 17] = 1.0
        concurrent_vectors[2, 19] = 1.0
        real_check = indexer._check_prune_safety
        raced = False

        def check_after_concurrent_publish(*args, **kwargs):
            nonlocal raced
            real_check(*args, **kwargs)
            if not raced:
                raced = True
                save_index(concurrent_entries, concurrent_vectors, idx, music_dir=tmp_path)

        with (
            patch(
                "autodj.indexer._check_prune_safety",
                side_effect=check_after_concurrent_publish,
            ),
            pytest.raises(IndexConsistencyError, match="expected generation"),
        ):
            prune_index(idx, allow_mass_prune=True, music_dir=tmp_path)

        current = read_manifest(idx)
        assert current is not None
        assert current.generation == first.generation + 1
        loaded, loaded_vectors = load_index(idx, music_dir=tmp_path)
        assert [entry.title for entry in loaded] == [
            "Concurrent 0",
            "Concurrent 1",
            "Concurrent 2",
        ]
        assert [int(np.argmax(loaded_vectors.reconstruct(row))) for row in range(3)] == [13, 17, 19]

    def test_prune_all_rejects_generation_race_without_deleting_newer_snapshot(
        self,
        tmp_path: Path,
    ) -> None:
        from autodj.index_manifest import IndexConsistencyError
        from autodj.indexer import load_index, prune_index, save_index

        entries, vectors = self._distinctive_entries(tmp_path, missing_rows={0, 1, 2})
        idx = tmp_path / "idx"
        save_index(entries, vectors, idx, music_dir=tmp_path)
        replacement_path = tmp_path / "replacement.flac"
        replacement_path.write_bytes(b"")
        replacement = [
            IndexEntry(
                path=str(replacement_path),
                title="Concurrent replacement",
                artist="Artist",
                album="Album",
                genre="Genre",
                bpm=120.0,
                year=2026,
                length=180.0,
                energy=0.5,
                key=0,
                mode=1,
                tempo_confidence=0.8,
            )
        ]
        replacement_vectors = np.zeros((1, FEATURE_DIM), dtype=np.float32)
        replacement_vectors[0, 23] = 1.0

        def publish_replacement(*_args, **_kwargs) -> None:
            save_index(replacement, replacement_vectors, idx, music_dir=tmp_path)

        with (
            patch(
                "autodj.indexer._check_prune_safety",
                side_effect=publish_replacement,
            ),
            pytest.raises(IndexConsistencyError, match="expected generation"),
        ):
            prune_index(idx, allow_mass_prune=True, music_dir=tmp_path)

        loaded, loaded_vectors = load_index(idx, music_dir=tmp_path)
        assert [entry.title for entry in loaded] == ["Concurrent replacement"]
        assert int(np.argmax(loaded_vectors.reconstruct(0))) == 23

    def test_load_audio_falls_back_to_librosa_when_soundfile_errors(self, tmp_path: Path) -> None:
        # Some FLACs over NFS make libsndfile raise "flac decoder lost sync"
        # mid-stream — librosa's audioread/ffmpeg path decodes them fine and
        # must be tried before giving up.  See indexer.py _load_audio.
        import soundfile as sf

        from autodj.indexer import _load_audio

        flac = tmp_path / "broken.flac"
        flac.write_bytes(b"not really flac")
        fake_audio = np.zeros(48000, dtype=np.float32)

        with (
            patch.object(sf, "read", side_effect=sf.LibsndfileError("flac decoder lost sync")),
            patch("librosa.load", return_value=(fake_audio, 48000)) as mock_librosa,
        ):
            audio, sr = _load_audio(flac)
        assert mock_librosa.called
        assert sr == 48000
        assert len(audio) == 48000

    def test_prints_phase_banner(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # Banner makes the silent NFS stat() loop visible — see indexer.py prune_index.
        from autodj.indexer import prune_index

        idx = self._save_with_files(tmp_path, n_present=3, n_missing=0)
        prune_index(idx, music_dir=tmp_path)
        out = capsys.readouterr().out
        assert "Phase: Pruning" in out
        assert "checking 3 indexed files" in out

    def test_safety_threshold_blocks_mass_prune(self, tmp_path: Path) -> None:
        from autodj.indexer import PruneSafetyError, prune_index

        idx = self._save_with_files(tmp_path, n_present=2, n_missing=10)
        with pytest.raises(PruneSafetyError):
            prune_index(idx, music_dir=tmp_path)

    def test_force_bypasses_safety(self, tmp_path: Path) -> None:
        from autodj.indexer import prune_index

        idx = self._save_with_files(tmp_path, n_present=2, n_missing=10)
        removed, kept = prune_index(idx, allow_mass_prune=True, music_dir=tmp_path)
        assert removed == 10
        assert kept == 2

    def test_all_missing_deletes_index_files(self, tmp_path: Path) -> None:
        from autodj.indexer import prune_index

        idx = self._save_with_files(tmp_path, n_present=0, n_missing=3)
        removed, kept = prune_index(idx, allow_mass_prune=True, music_dir=tmp_path)
        assert removed == 3
        assert kept == 0
        assert not (idx / "vectors.index").exists()
        assert not (idx / "tracks.db").exists()
        assert not (idx / "index-manifest.json").exists()
        assert list(idx.glob("tracks.g*.db")) == []
        assert list(idx.glob("vectors.g*.index")) == []

    def test_prune_propagates_delete_failure_without_reporting_success(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from autodj.index_manifest import (
            current_snapshot_token,
            read_manifest,
        )
        from autodj.indexer import prune_index

        idx = self._save_with_files(tmp_path, n_present=0, n_missing=3)
        before = read_manifest(idx)
        assert before is not None
        real_unlink = Path.unlink

        def refuse_vectors(path: Path, *args: object, **kwargs: object) -> None:
            if path == idx / "vectors.index":
                raise PermissionError("vectors locked")
            real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", refuse_vectors)
        with pytest.raises(PermissionError, match="vectors locked"):
            prune_index(idx, allow_mass_prune=True, music_dir=tmp_path)

        assert read_manifest(idx) is None
        assert current_snapshot_token(idx).generation == 0
        assert (idx / "vectors.index").exists()
        assert (idx / "tracks.db").exists()

    def test_tombstoned_restart_discards_leftover_working_cores(self, tmp_path: Path) -> None:
        from autodj.index_manifest import current_snapshot_token, tombstone_publication
        from autodj.indexer import _load_existing_artifacts, save_index

        entries, vectors = self._distinctive_entries(tmp_path)
        idx = tmp_path / "idx"
        save_index(entries, vectors, idx, music_dir=tmp_path)
        tombstone_publication(idx)

        loaded, loaded_vectors, token = _load_existing_artifacts(idx, tmp_path)

        assert loaded == []
        assert loaded_vectors == []
        assert token == current_snapshot_token(idx)
        assert not (idx / "tracks.db").exists()
        assert not (idx / "vectors.index").exists()

    def test_reservation_only_restart_discards_uncommitted_working_cores(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import autodj.index_manifest as manifest_module
        from autodj.index_manifest import current_snapshot_token
        from autodj.indexer import _load_existing_artifacts, load_index, save_index

        entries, vectors = self._distinctive_entries(tmp_path)
        idx = tmp_path / "idx"
        monkeypatch.setattr(
            manifest_module,
            "_checkpoint_working_tracks",
            lambda _index_dir: (_ for _ in ()).throw(OSError("checkpoint failed")),
        )
        with pytest.raises(OSError, match="checkpoint failed"):
            save_index(entries, vectors, idx, music_dir=tmp_path)

        loaded, loaded_vectors, token = _load_existing_artifacts(idx, tmp_path)

        assert loaded == []
        assert loaded_vectors == []
        assert token == current_snapshot_token(idx)
        assert token.generation == 0 and token.state_revision > 0
        assert not (idx / "tracks.db").exists()
        assert not (idx / "vectors.index").exists()

        monkeypatch.undo()
        save_index(entries, vectors, idx, expected_snapshot=token, music_dir=tmp_path)
        rebuilt, rebuilt_vectors = load_index(idx, music_dir=tmp_path)
        assert [Path(entry.path).name for entry in rebuilt] == [
            Path(entry.path).name for entry in entries
        ]
        assert [int(np.argmax(rebuilt_vectors.reconstruct(row))) for row in range(3)] == [3, 7, 11]


# ---------------------------------------------------------------------------
# enrich_from_beets
# ---------------------------------------------------------------------------


class TestEnrichFromBeets:
    def _make_distinctive_index(
        self,
        tmp_path: Path,
    ) -> tuple[list[IndexEntry], np.ndarray]:
        entries = [
            IndexEntry(
                path=str(tmp_path / f"song_{row}.flac"),
                title=f"Original {row}",
                artist="Artist",
                album="Album",
                genre="Genre",
                bpm=120.0,
                year=2026,
                length=180.0,
                energy=0.5,
                key=0,
                mode=1,
                tempo_confidence=0.8,
            )
            for row in range(2)
        ]
        vectors = np.zeros((2, FEATURE_DIM), dtype=np.float32)
        vectors[0, 3] = 1.0
        vectors[1, 7] = 1.0
        return entries, vectors

    def _make_beets(self, db_path: Path, entries: list[dict]) -> None:
        import sqlite3

        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE items (
            id INTEGER PRIMARY KEY, path BLOB,
            title TEXT, artist TEXT, album TEXT, genre TEXT,
            bpm REAL, year INTEGER, length REAL,
            initial_key TEXT)""")
        for i, e in enumerate(entries):
            conn.execute(
                "INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    i + 1,
                    e["path"].encode("utf-8"),
                    e.get("title", ""),
                    e.get("artist", ""),
                    e.get("album", ""),
                    e.get("genre", ""),
                    e.get("bpm", 0.0),
                    e.get("year", 0),
                    e.get("length", 0.0),
                    e.get("initial_key", ""),
                ),
            )
        conn.commit()
        conn.close()

    def test_no_index_returns_zero(self, tmp_path: Path) -> None:
        from autodj.indexer import enrich_from_beets

        beets = tmp_path / "library.db"
        self._make_beets(beets, [])
        assert enrich_from_beets(tmp_path / "noidx", music_dir=None, beets_db=beets) == (0, 0)

    def test_enrich_ignores_dirty_canonical_metadata_and_preserves_vector_mapping(
        self,
        tmp_path: Path,
    ) -> None:
        from autodj.indexer import (
            _save_tracks_metadata,
            enrich_from_beets,
            load_index,
            save_index,
        )

        entries, vectors = self._make_distinctive_index(tmp_path)
        idx = tmp_path / "idx"
        save_index(entries, vectors, idx, music_dir=tmp_path)
        _save_tracks_metadata(list(reversed(entries)), idx, music_dir=tmp_path)
        beets = tmp_path / "library.db"
        self._make_beets(
            beets,
            [{"path": entry.path, "title": f"Enriched {row}"} for row, entry in enumerate(entries)],
        )

        assert enrich_from_beets(idx, music_dir=tmp_path, beets_db=beets) == (2, 2)
        loaded, loaded_vectors = load_index(idx, music_dir=tmp_path)
        assert [Path(entry.path).name for entry in loaded] == ["song_0.flac", "song_1.flac"]
        assert [entry.title for entry in loaded] == ["Enriched 0", "Enriched 1"]
        assert [int(np.argmax(loaded_vectors.reconstruct(row))) for row in range(2)] == [3, 7]

    def test_enrich_rejects_generation_race_without_overwriting_newer_snapshot(
        self,
        tmp_path: Path,
    ) -> None:
        from dataclasses import replace

        import autodj.indexer as indexer
        from autodj.index_manifest import IndexConsistencyError, read_manifest
        from autodj.indexer import enrich_from_beets, load_index, save_index

        entries, vectors = self._make_distinctive_index(tmp_path)
        idx = tmp_path / "idx"
        save_index(entries, vectors, idx, music_dir=tmp_path)
        first = read_manifest(idx)
        assert first is not None
        beets = tmp_path / "library.db"
        self._make_beets(
            beets,
            [{"path": entry.path, "title": f"Enriched {row}"} for row, entry in enumerate(entries)],
        )
        concurrent_entries = [
            replace(entry, title=f"Concurrent {row}") for row, entry in enumerate(entries)
        ]
        concurrent_vectors = np.zeros_like(vectors)
        concurrent_vectors[0, 11] = 1.0
        concurrent_vectors[1, 13] = 1.0
        real_apply = indexer._apply_beets_row
        raced = False

        def apply_after_concurrent_publish(*args, **kwargs):
            nonlocal raced
            if not raced:
                raced = True
                save_index(concurrent_entries, concurrent_vectors, idx, music_dir=tmp_path)
            return real_apply(*args, **kwargs)

        with (
            patch(
                "autodj.indexer._apply_beets_row",
                side_effect=apply_after_concurrent_publish,
            ),
            pytest.raises(IndexConsistencyError, match="expected generation"),
        ):
            enrich_from_beets(idx, music_dir=tmp_path, beets_db=beets)

        current = read_manifest(idx)
        assert current is not None
        assert current.generation == first.generation + 1
        loaded, loaded_vectors = load_index(idx, music_dir=tmp_path)
        assert [entry.title for entry in loaded] == ["Concurrent 0", "Concurrent 1"]
        assert [int(np.argmax(loaded_vectors.reconstruct(row))) for row in range(2)] == [11, 13]

    def test_updates_initial_key(self, tmp_path: Path) -> None:
        from autodj.indexer import enrich_from_beets, save_index

        # Build an index with one entry
        path = tmp_path / "song.flac"
        path.write_bytes(b"")
        entries = [
            IndexEntry(
                path=str(path),
                title="T",
                artist="A",
                album="L",
                genre="G",
                bpm=100.0,
                year=2020,
                length=180.0,
                energy=0.05,
                key=0,
                mode=1,
                tempo_confidence=0.5,
            )
        ]
        vectors = np.random.randn(1, FEATURE_DIM).astype(np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        idx = tmp_path / "idx"
        idx.mkdir()
        save_index(entries, vectors, idx, music_dir=tmp_path)
        # Set up beets DB with a key
        beets = tmp_path / "library.db"
        self._make_beets(beets, [{"path": str(path), "initial_key": "Am"}])

        updated, total = enrich_from_beets(idx, music_dir=tmp_path, beets_db=beets)
        assert updated == 1
        assert total == 1
        # Reload and verify
        from autodj.indexer import load_index

        loaded, _ = load_index(idx, music_dir=tmp_path)
        assert loaded[0].mode == 0  # minor
        assert loaded[0].key == 9  # A

    def test_no_changes_returns_zero_updated(self, tmp_path: Path) -> None:
        from autodj.indexer import enrich_from_beets, save_index

        path = tmp_path / "song.flac"
        path.write_bytes(b"")
        entries = [
            IndexEntry(
                path=str(path),
                title="T",
                artist="A",
                album="L",
                genre="G",
                bpm=100.0,
                year=2020,
                length=180.0,
                energy=0.05,
                key=9,
                mode=0,
                tempo_confidence=0.5,
            )
        ]
        vectors = np.random.randn(1, FEATURE_DIM).astype(np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        idx = tmp_path / "idx"
        idx.mkdir()
        save_index(entries, vectors, idx, music_dir=tmp_path)
        beets = tmp_path / "library.db"
        self._make_beets(beets, [{"path": str(path), "initial_key": "Am"}])

        updated, total = enrich_from_beets(idx, music_dir=tmp_path, beets_db=beets)
        assert updated == 0
        assert total == 1

    def test_missing_beets_db_returns_zero_updated(self, tmp_path: Path) -> None:
        from autodj.indexer import enrich_from_beets, save_index

        path = tmp_path / "song.flac"
        path.write_bytes(b"")
        entries = [
            IndexEntry(
                path=str(path),
                title="T",
                artist="A",
                album="L",
                genre="G",
                bpm=100.0,
                year=2020,
                length=180.0,
                energy=0.05,
                key=0,
                mode=1,
                tempo_confidence=0.5,
            )
        ]
        vectors = np.random.randn(1, FEATURE_DIM).astype(np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        idx = tmp_path / "idx"
        idx.mkdir()
        save_index(entries, vectors, idx, music_dir=tmp_path)
        updated, total = enrich_from_beets(
            idx, music_dir=tmp_path, beets_db=tmp_path / "missing.db"
        )
        assert updated == 0
        assert total == 1


# ---------------------------------------------------------------------------
# Index entry path roundtrips with music_dir
# ---------------------------------------------------------------------------


class TestDetectStaleEntries:
    def _entry(self, path: str, embedded_at: float = 0.0) -> IndexEntry:
        return IndexEntry(
            path=path,
            title="t",
            artist="a",
            album="al",
            genre="g",
            bpm=120.0,
            year=2020,
            length=180.0,
            energy=0.0,
            key=-1,
            mode=-1,
            tempo_confidence=0.0,
            embedded_at=embedded_at,
        )

    def test_detects_replaced_file(self, tmp_path: Path) -> None:

        from autodj.indexer import _detect_stale_entries

        f = tmp_path / "song.flac"
        f.write_bytes(b"original")
        original_mtime = f.stat().st_mtime
        # Embedded an hour ago, then file replaced now
        e = self._entry(str(f), embedded_at=original_mtime - 3600)
        os.utime(f, (original_mtime, original_mtime))
        assert e.path in _detect_stale_entries([e])

    def test_unstamped_entry_is_re_embedded(self, tmp_path: Path) -> None:
        from autodj.indexer import _detect_stale_entries

        f = tmp_path / "song.flac"
        f.write_bytes(b"x")
        e = self._entry(str(f), embedded_at=0.0)
        assert _detect_stale_entries([e]) == {e.path}

    def test_unchanged_file_not_stale(self, tmp_path: Path) -> None:
        from autodj.indexer import _detect_stale_entries

        f = tmp_path / "song.flac"
        f.write_bytes(b"x")
        # Embedded just after file creation
        e = self._entry(str(f), embedded_at=f.stat().st_mtime + 60)
        assert e.path not in _detect_stale_entries([e])

    def test_missing_file_skipped(self, tmp_path: Path) -> None:
        # prune handles missing files; stale detection ignores them
        from autodj.indexer import _detect_stale_entries

        e = self._entry(str(tmp_path / "gone.flac"), embedded_at=1.0)
        assert _detect_stale_entries([e]) == set()

    def test_reindex_modified_since_flags_fresh_entry(self, tmp_path: Path) -> None:
        from autodj.indexer import _detect_stale_entries

        f = tmp_path / "replaced.flac"
        f.write_bytes(b"x")
        mt = f.stat().st_mtime
        os.utime(f, (mt, mt))
        e = self._entry(str(f), embedded_at=mt + 60)
        # Cutoff one hour BEFORE mtime → file is newer, must be flagged
        assert e.path in _detect_stale_entries([e], reindex_modified_since=mt - 3600)

    def test_from_track_stamps_embedded_at(self) -> None:
        import time

        from autodj.beets import Track

        t = Track(
            path=Path("/tmp/x.flac"),
            title="t",
            artist="a",
            album="al",
            genre="g",
            bpm=120.0,
            year=2020,
            length=180.0,
        )
        before = time.time()
        e = IndexEntry.from_track(t)
        after = time.time()
        assert before <= e.embedded_at <= after

    def _future_track(self, tmp_path: Path, ahead_s: float):
        """Write a file whose mtime is *ahead_s* seconds in the future."""
        import time

        from autodj.beets import Track

        audio = tmp_path / "nas.flac"
        audio.write_bytes(b"\x00")
        stamp = time.time() + ahead_s
        os.utime(audio, (stamp, stamp))
        return Track(
            path=audio,
            title="t",
            artist="a",
            album="al",
            genre="g",
            bpm=120.0,
            year=2020,
            length=180.0,
        )

    def test_from_track_uses_the_file_clock_not_the_local_clock(self, tmp_path: Path) -> None:
        track = self._future_track(tmp_path, ahead_s=3600)
        entry = IndexEntry.from_track(track)
        assert entry.embedded_at == pytest.approx(track.path.stat().st_mtime)

    def test_clock_skew_does_not_make_a_fresh_entry_stale(self, tmp_path: Path) -> None:
        """A NAS clock an hour ahead used to re-embed the whole library forever."""
        track = self._future_track(tmp_path, ahead_s=3600)
        entry = IndexEntry.from_track(track)
        from autodj.indexer import _detect_stale_entries

        assert _detect_stale_entries([entry]) == set()

    def test_a_later_edit_is_still_stale(self, tmp_path: Path) -> None:

        track = self._future_track(tmp_path, ahead_s=3600)
        entry = IndexEntry.from_track(track)
        touched = entry.embedded_at + 120
        os.utime(track.path, (touched, touched))
        from autodj.indexer import _detect_stale_entries

        assert _detect_stale_entries([entry]) == {entry.path}


class TestRelativizeForStorage:
    def test_strips_music_dir_prefix(self, tmp_path: Path) -> None:
        from autodj.indexer import _relativize_for_storage

        md = tmp_path / "Music"
        md.mkdir()
        assert _relativize_for_storage(str(md / "Artist" / "song.flac"), md) == "Artist/song.flac"

    def test_rejects_path_outside_music_dir(self, tmp_path: Path) -> None:
        from autodj.indexer import _relativize_for_storage

        md = tmp_path / "Music"
        md.mkdir()
        with pytest.raises(ValueError, match="not under music_dir"):
            _relativize_for_storage(str(tmp_path / "elsewhere" / "song.flac"), md)

    def test_does_not_stat_filesystem(self, tmp_path: Path) -> None:
        # Resolve() / is_relative_to() on real Paths used to dominate save_index
        # for libraries on NFS — see indexer.py _relativize_for_storage docstring.
        # Must work on paths that don't exist.
        from autodj.indexer import _relativize_for_storage

        md = tmp_path / "Music"  # never created
        fake = md / "Artist" / "song.flac"
        assert _relativize_for_storage(str(fake), md) == "Artist/song.flac"

    def test_no_music_dir_keeps_relative_path(self) -> None:
        from autodj.indexer import _relativize_for_storage

        assert _relativize_for_storage("Artist/song.flac", None) == "Artist/song.flac"


class TestPathPortability:
    def test_save_strips_music_dir_prefix(self, tmp_path: Path) -> None:
        from autodj.indexer import load_index, save_index

        music_dir = tmp_path / "Music"
        music_dir.mkdir()
        for n in ("a.flac", "b.flac"):
            (music_dir / n).write_bytes(b"")
        entries = [
            IndexEntry(
                path=str(music_dir / "a.flac"),
                title="A",
                artist="X",
                album="L",
                genre="G",
                bpm=100,
                year=2020,
                length=180,
                energy=0.05,
                key=0,
                mode=1,
                tempo_confidence=0.5,
            ),
        ]
        v = np.random.randn(1, FEATURE_DIM).astype(np.float32)
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        idx = tmp_path / "idx"
        idx.mkdir()
        save_index(entries, v, idx, music_dir=music_dir)
        # Inspect raw metadata in the SQLite store.
        import sqlite3 as _sql

        conn = _sql.connect(idx / "tracks.db")
        try:
            stored_path = conn.execute(
                "SELECT path FROM tracks ORDER BY vec_row ASC LIMIT 1"
            ).fetchone()[0]
        finally:
            conn.close()
        # Should be stored as relative
        assert stored_path == "a.flac" or stored_path.endswith("a.flac")
        # Round-trip resolves back to absolute
        loaded, _ = load_index(idx, music_dir=music_dir)
        assert Path(loaded[0].path).resolve() == (music_dir / "a.flac").resolve()


# ---------------------------------------------------------------------------
# _backfill_dj_meta + _analyse_one_track
# ---------------------------------------------------------------------------


def _entry(path: str) -> IndexEntry:
    return IndexEntry(
        path=path,
        title="t",
        artist="a",
        album="al",
        genre="g",
        bpm=120,
        year=2020,
        length=180,
        energy=0.05,
        key=0,
        mode=1,
        tempo_confidence=0.5,
    )


class TestAnalyseOneTrack:
    def test_empty_audio_returns_none_meta(self, tmp_path: Path) -> None:
        from autodj.indexer import _analyse_one_track

        with patch(
            "autodj.indexer._load_audio", return_value=(np.zeros(0, dtype=np.float32), 24000)
        ):
            _path, meta, err = _analyse_one_track(str(tmp_path / "x.flac"))
        assert meta is None and err is None

    def test_load_failure_returns_error_string(self, tmp_path: Path) -> None:
        from autodj.indexer import _analyse_one_track

        with patch("autodj.indexer._load_audio", side_effect=OSError("nope")):
            _path, meta, err = _analyse_one_track(str(tmp_path / "x.flac"))
        assert meta is None
        assert err is not None and "OSError" in err

    def test_success_returns_meta(self, tmp_path: Path) -> None:
        from autodj.dj_meta import DjMeta
        from autodj.indexer import _analyse_one_track

        fake_audio = np.zeros(2400, dtype=np.float32)
        fake_meta = DjMeta(analysed=True)
        with (
            patch("autodj.indexer._load_audio", return_value=(fake_audio, 24000)),
            patch("autodj.dj_meta.analyse_audio", return_value=fake_meta),
        ):
            _p, meta, err = _analyse_one_track(str(tmp_path / "x.flac"))
        assert meta is fake_meta and err is None


class TestBackfillDjMeta:
    def test_no_cache_short_circuits(self, tmp_path: Path) -> None:
        from autodj.indexer import _backfill_dj_meta

        with patch("autodj.dj_meta.get_cache", return_value=None):
            _backfill_dj_meta([_entry("a.flac")], tmp_path, workers=1)

    def test_all_already_analysed_short_circuits(self, tmp_path: Path, capsys) -> None:
        from autodj.dj_meta import DjMeta
        from autodj.indexer import _backfill_dj_meta

        cache = type("C", (), {})()
        cache.prune_to_paths = lambda _paths: 0
        cache.get = lambda _p: DjMeta(analysed=True)
        cache.set = lambda *_a, **_kw: None
        cache.flush = lambda *_a, **_kw: None
        with patch("autodj.dj_meta.get_cache", return_value=cache):
            _backfill_dj_meta([_entry("a.flac")], tmp_path, workers=1)
        out = capsys.readouterr().out
        assert "already covers" in out

    def test_prunes_stale_cache_rows_when_supported(self, tmp_path: Path, capsys) -> None:
        from autodj.dj_meta import DjMeta
        from autodj.indexer import _backfill_dj_meta

        seen: list[set[str]] = []

        class _Cache:
            def prune_to_paths(self, paths: set[str]) -> int:
                seen.append(paths)
                return 2

            def get(self, _p: str) -> DjMeta:
                return DjMeta(analysed=True)

            def set(self, *_a, **_kw) -> None:
                pass

            def flush(self, *_a, **_kw) -> None:
                pass

        entries = [_entry("a.flac"), _entry("b.flac")]
        with patch("autodj.dj_meta.get_cache", return_value=_Cache()):
            _backfill_dj_meta(entries, tmp_path, workers=1)

        out = capsys.readouterr().out
        assert seen == [{"a.flac", "b.flac"}]
        assert "pruned 2 stale entries" in out

    def test_serial_path_records_results(self, tmp_path: Path) -> None:
        from autodj.dj_meta import DjMeta
        from autodj.indexer import _backfill_dj_meta

        stored: dict[str, DjMeta] = {}
        flushes: list[bool] = []

        class _Cache:
            def get(self, _p: str) -> DjMeta:
                return DjMeta(analysed=False)

            def set(self, p: str, m: DjMeta) -> None:
                stored[p] = m

            def flush(self, *_a, **_kw) -> None:
                flushes.append(True)

        meta_ok = DjMeta(analysed=True, intro_end_s=1.0)
        with (
            patch("autodj.dj_meta.get_cache", return_value=_Cache()),
            patch(
                "autodj.indexer._analyse_one_track",
                side_effect=[
                    ("a.flac", meta_ok, None),
                    ("b.flac", None, "RuntimeError: bad"),
                ],
            ),
        ):
            _backfill_dj_meta([_entry("a.flac"), _entry("b.flac")], tmp_path, workers=1)
        assert "a.flac" in stored
        assert "b.flac" not in stored  # error path skips set
        assert flushes  # final force-flush

    def test_workers_default_threadpool_path(self, tmp_path: Path) -> None:
        from autodj.dj_meta import DjMeta
        from autodj.indexer import _backfill_dj_meta

        stored: dict[str, DjMeta] = {}

        class _Cache:
            def get(self, _p: str) -> DjMeta:
                return DjMeta(analysed=False)

            def set(self, p: str, m: DjMeta) -> None:
                stored[p] = m

            def flush(self, *_a, **_kw) -> None:
                pass

        meta_ok = DjMeta(analysed=True)
        entries = [_entry(f"t{i}.flac") for i in range(4)]
        with (
            patch("autodj.dj_meta.get_cache", return_value=_Cache()),
            patch(
                "autodj.indexer._analyse_one_track",
                side_effect=lambda p: (p, meta_ok, None),
            ),
        ):
            _backfill_dj_meta(entries, tmp_path, workers=2)
        assert len(stored) == 4

    def test_workers_default_none_uses_serial_path(self, tmp_path: Path) -> None:
        from autodj.dj_meta import DjMeta
        from autodj.indexer import _backfill_dj_meta

        class _Cache:
            def get(self, _p: str) -> DjMeta:
                return DjMeta(analysed=False)

            def set(self, *_a, **_kw) -> None:
                pass

            def flush(self, *_a, **_kw) -> None:
                pass

        with (
            patch("autodj.dj_meta.get_cache", return_value=_Cache()),
            patch(
                "autodj.indexer._analyse_one_track",
                side_effect=lambda p: (p, DjMeta(analysed=True), None),
            ),
        ):
            _backfill_dj_meta([_entry("a.flac")], tmp_path, workers=None)

    def test_throttle_ms_sleeps_serial_path(self, tmp_path: Path) -> None:
        from autodj.dj_meta import DjMeta
        from autodj.indexer import _backfill_dj_meta

        class _Cache:
            def get(self, _p: str) -> DjMeta:
                return DjMeta(analysed=False)

            def set(self, *_a, **_kw) -> None:
                pass

            def flush(self, *_a, **_kw) -> None:
                pass

        sleeps: list[float] = []
        entries = [_entry(f"t{i}.flac") for i in range(3)]
        with (
            patch("autodj.dj_meta.get_cache", return_value=_Cache()),
            patch(
                "autodj.indexer._analyse_one_track",
                side_effect=lambda p: (p, DjMeta(analysed=True), None),
            ),
            patch("time.sleep", side_effect=sleeps.append),
        ):
            _backfill_dj_meta(entries, tmp_path, workers=1, throttle_ms=250.0)
        # one sleep per entry, each 0.25 s
        assert sleeps == [0.25, 0.25, 0.25]

    def test_throttle_ms_sleeps_parallel_path(self, tmp_path: Path) -> None:
        """Parallel worker pool branch should sleep on each submit when
        throttle_ms > 0 (covers _backfill_dj_meta line 1392-1394)."""
        from autodj.dj_meta import DjMeta
        from autodj.indexer import _backfill_dj_meta

        class _Cache:
            def get(self, _p: str) -> DjMeta:
                return DjMeta(analysed=False)

            def set(self, *_a, **_kw) -> None:
                pass

            def flush(self, *_a, **_kw) -> None:
                pass

        sleeps: list[float] = []
        entries = [_entry(f"t{i}.flac") for i in range(5)]
        with (
            patch("autodj.dj_meta.get_cache", return_value=_Cache()),
            patch(
                "autodj.indexer._analyse_one_track",
                side_effect=lambda p: (p, DjMeta(analysed=True), None),
            ),
            patch("time.sleep", side_effect=sleeps.append),
        ):
            _backfill_dj_meta(entries, tmp_path, workers=2, throttle_ms=125.0)
        # One sleep per submit -- 5 entries = 5 submissions = 5 sleeps.
        assert len(sleeps) == 5
        assert all(abs(s - 0.125) < 1e-9 for s in sleeps)

    def test_throttle_ms_zero_no_sleep(self, tmp_path: Path) -> None:
        from autodj.dj_meta import DjMeta
        from autodj.indexer import _backfill_dj_meta

        class _Cache:
            def get(self, _p: str) -> DjMeta:
                return DjMeta(analysed=False)

            def set(self, *_a, **_kw) -> None:
                pass

            def flush(self, *_a, **_kw) -> None:
                pass

        sleeps: list[float] = []
        with (
            patch("autodj.dj_meta.get_cache", return_value=_Cache()),
            patch(
                "autodj.indexer._analyse_one_track",
                side_effect=lambda p: (p, DjMeta(analysed=True), None),
            ),
            patch("time.sleep", side_effect=sleeps.append),
        ):
            _backfill_dj_meta([_entry("a.flac")], tmp_path, workers=1, throttle_ms=0.0)
        assert sleeps == []


# ---------------------------------------------------------------------------
# Throttled FAISS checkpoint + crash-recovery reconciliation
# ---------------------------------------------------------------------------


class TestThrottledFaissCheckpoint:
    """Aligned every-N vector flush + metadata delta + reload recovery."""

    @staticmethod
    def _make_entries(n: int) -> tuple[list[IndexEntry], np.ndarray]:
        entries = [
            IndexEntry(
                path=f"song_{i}.flac",
                title=f"Song {i}",
                artist="Artist",
                album="Album",
                genre="Rock",
                bpm=120.0,
                year=2000,
                length=180.0,
                energy=0.05,
                key=0,
                mode=1,
                tempo_confidence=0.8,
            )
            for i in range(n)
        ]
        vectors = np.random.randn(n, FEATURE_DIM).astype(np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        return entries, vectors

    def test_save_vectors_writes_faiss_only(self, tmp_path: Path) -> None:
        from autodj.indexer import _save_vectors

        _, vectors = self._make_entries(3)
        _save_vectors(vectors, tmp_path)

        assert (tmp_path / "vectors.index").exists()
        # tracks.db not touched -- _save_vectors must not create it.
        assert not (tmp_path / "tracks.db").exists()

    def test_save_tracks_metadata_writes_db_only(self, tmp_path: Path) -> None:
        from autodj.indexer import _save_tracks_metadata

        entries, _ = self._make_entries(3)
        _save_tracks_metadata(entries, tmp_path, music_dir=None)

        assert (tmp_path / "tracks.db").exists()
        # FAISS file not touched.
        assert not (tmp_path / "vectors.index").exists()

        import sqlite3 as _sql

        conn = _sql.connect(tmp_path / "tracks.db")
        try:
            count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        finally:
            conn.close()
        assert count == 3

    def test_delta_checkpoint_upserts_one_stable_vector_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import autodj.indexer as indexer
        from autodj.indexer import (
            _load_tracks_rows,
            _open_tracks_db,
            _upsert_tracks_metadata,
        )

        entries, _ = self._make_entries(3)
        statements: list[str] = []

        def traced_open(index_dir: Path):
            conn = _open_tracks_db(index_dir)
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr(indexer, "_open_tracks_db", traced_open)
        _upsert_tracks_metadata(entries[:2], tmp_path, first_vec_row=0, music_dir=None)
        statements.clear()
        _upsert_tracks_metadata(entries[2:], tmp_path, first_vec_row=2, music_dir=None)
        conn = _open_tracks_db(tmp_path)
        try:
            rows = conn.execute("SELECT vec_row, path FROM tracks ORDER BY vec_row").fetchall()
            loaded = _load_tracks_rows(conn)
        finally:
            conn.close()

        assert rows == [
            (0, entries[0].path),
            (1, entries[1].path),
            (2, entries[2].path),
        ]
        assert [entry.path for entry in loaded] == [entry.path for entry in entries]
        assert not any(
            statement.lstrip().upper().startswith("DELETE FROM TRACKS") for statement in statements
        )

    def test_checkpoint_buffers_metadata_until_vectors_are_flushed(self, tmp_path: Path) -> None:
        from autodj.index_manifest import current_snapshot_token
        from autodj.indexer import IncrementalCheckpoint, _open_tracks_db

        entries, vectors = self._make_entries(2)
        checkpoint = IncrementalCheckpoint(
            index_dir=tmp_path,
            music_dir=None,
            existing_entries=[],
            existing_vectors=[],
            total_new=2,
            expected_snapshot=current_snapshot_token(tmp_path),
            flush_every=2,
        )
        checkpoint.write(entries[:1], [vectors[0]])
        assert not (tmp_path / "tracks.db").exists()
        assert not (tmp_path / "vectors.index").exists()

        checkpoint.write(entries, [vectors[0], vectors[1]])
        conn = _open_tracks_db(tmp_path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 2
        finally:
            conn.close()
        assert (tmp_path / "vectors.index").exists()

    def test_aligned_checkpoint_only_converts_binds_and_queries_delta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import autodj.indexer as indexer
        from autodj.index_manifest import current_snapshot_token
        from autodj.indexer import IncrementalCheckpoint, _open_tracks_db, save_index

        entries, vectors = self._make_entries(65)
        existing_entries = entries[:64]
        existing_vectors = list(vectors[:64])
        save_index(existing_entries, vectors[:64], tmp_path, music_dir=None)

        converted_paths: list[str] = []
        statements: list[str] = []
        real_entry_to_row = indexer._entry_to_row
        real_open_tracks_db = _open_tracks_db

        def traced_entry_to_row(entry, music_dir, vec_row):
            converted_paths.append(entry.path)
            return real_entry_to_row(entry, music_dir, vec_row)

        def traced_open(index_dir: Path):
            conn = real_open_tracks_db(index_dir)
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr(indexer, "_entry_to_row", traced_entry_to_row)
        monkeypatch.setattr(indexer, "_open_tracks_db", traced_open)
        checkpoint = IncrementalCheckpoint(
            index_dir=tmp_path,
            music_dir=None,
            existing_entries=existing_entries,
            existing_vectors=existing_vectors,
            total_new=1,
            expected_snapshot=current_snapshot_token(tmp_path),
            flush_every=1,
        )

        checkpoint.write(entries[64:], [vectors[64]])

        conn = real_open_tracks_db(tmp_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        finally:
            conn.close()
        metadata_inserts = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("INSERT INTO TRACKS")
        ]
        baseline_queries = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT VEC_ROW, PATH FROM TRACKS")
        ]

        assert count == 65
        assert converted_paths == [entries[64].path]
        assert len(metadata_inserts) == 1
        assert baseline_queries == []

    def test_checkpoint_restores_published_baseline_before_delta(self, tmp_path: Path) -> None:
        from autodj.index_manifest import current_snapshot_token
        from autodj.indexer import (
            IncrementalCheckpoint,
            _save_tracks_metadata,
            load_index,
            save_index,
        )

        entries, vectors = self._make_entries(3)
        save_index(entries[:2], vectors[:2], tmp_path, music_dir=None)
        # Same-count canonical dirt must never become the next checkpoint baseline.
        _save_tracks_metadata([entries[1], entries[0]], tmp_path, music_dir=None)
        checkpoint = IncrementalCheckpoint(
            index_dir=tmp_path,
            music_dir=None,
            existing_entries=entries[:2],
            existing_vectors=list(vectors[:2]),
            total_new=1,
            expected_snapshot=current_snapshot_token(tmp_path),
            flush_every=1,
        )

        checkpoint.write(entries[2:], [vectors[2]])

        loaded, loaded_vectors = load_index(tmp_path)
        assert [entry.path for entry in loaded] == [entry.path for entry in entries]
        assert np.allclose(loaded_vectors.reconstruct(0), vectors[0])
        assert np.allclose(loaded_vectors.reconstruct(1), vectors[1])

    def test_embed_propagates_checkpoint_failure_and_allows_full_delta_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import faiss

        import autodj.indexer as indexer
        from autodj.index_manifest import current_snapshot_token
        from autodj.indexer import (
            IncrementalCheckpoint,
            _embed_new_tracks,
            _open_tracks_db,
        )

        entries, _ = self._make_entries(1)
        marker_vector = np.zeros(FEATURE_DIM, dtype=np.float32)
        marker_vector[17] = 1.0
        embedding = marker_vector[:EMBEDDING_DIM]
        track = Track(
            path=Path(entries[0].path),
            title=entries[0].title,
            artist=entries[0].artist,
            album=entries[0].album,
            genre=entries[0].genre,
            bpm=entries[0].bpm,
            year=entries[0].year,
            length=entries[0].length,
        )
        wrapper = MagicMock()
        wrapper.embed_array.return_value = embedding
        checkpoint = IncrementalCheckpoint(
            index_dir=tmp_path,
            music_dir=None,
            existing_entries=[],
            existing_vectors=[],
            total_new=1,
            expected_snapshot=current_snapshot_token(tmp_path),
            flush_every=1,
        )
        initial_snapshot = checkpoint.expected_snapshot
        real_upsert = indexer._upsert_tracks_metadata

        def fail_metadata(*_args, **_kwargs):
            raise OSError("metadata checkpoint failed")

        monkeypatch.setattr(indexer, "_upsert_tracks_metadata", fail_metadata)
        with (
            patch(
                "autodj.indexer._extract_librosa_features",
                return_value=(
                    np.zeros(16, dtype=np.float32),
                    np.zeros(32, dtype=np.float32),
                    22050,
                    {
                        "energy": 0.0,
                        "key": -1,
                        "mode": -1,
                        "bpm": 0.0,
                        "tempo_confidence": 0.0,
                    },
                ),
            ),
            pytest.raises(OSError, match="metadata checkpoint failed"),
        ):
            _embed_new_tracks([track], wrapper, workers=1, checkpoint=checkpoint.write)

        assert checkpoint.published_new_count == 0
        assert checkpoint.expected_snapshot == initial_snapshot
        assert faiss.read_index(str(tmp_path / "vectors.index")).ntotal == 1

        monkeypatch.setattr(indexer, "_upsert_tracks_metadata", real_upsert)
        checkpoint.write(entries, [marker_vector])
        conn = _open_tracks_db(tmp_path)
        try:
            paths = conn.execute("SELECT path FROM tracks ORDER BY vec_row").fetchall()
        finally:
            conn.close()
        retried = faiss.read_index(str(tmp_path / "vectors.index"))
        assert paths == [(entries[0].path,)]
        assert int(np.argmax(retried.reconstruct(0))) == 17
        assert checkpoint.expected_snapshot.generation == 1

    def test_final_save_rejects_generation_newer_than_checkpoint_token(
        self,
        tmp_path: Path,
    ) -> None:
        from dataclasses import replace

        from autodj.index_manifest import IndexConsistencyError, IndexSnapshotToken
        from autodj.indexer import IncrementalCheckpoint, load_index, save_index

        entries, vectors = self._make_entries(2)
        checkpoint = IncrementalCheckpoint(
            index_dir=tmp_path,
            music_dir=None,
            existing_entries=[],
            existing_vectors=[],
            total_new=2,
            expected_snapshot=IndexSnapshotToken(0),
            flush_every=2,
        )
        checkpoint.write(entries, [vectors[0], vectors[1]])
        assert checkpoint.expected_snapshot.generation == 1

        concurrent_entries = [
            replace(entry, title=f"Concurrent {row}") for row, entry in enumerate(entries)
        ]
        concurrent_vectors = np.zeros_like(vectors)
        concurrent_vectors[0, 83] = 1.0
        concurrent_vectors[1, 89] = 1.0
        save_index(concurrent_entries, concurrent_vectors, tmp_path)

        with pytest.raises(IndexConsistencyError, match="expected generation"):
            save_index(
                entries,
                vectors,
                tmp_path,
                expected_snapshot=checkpoint.expected_snapshot,
            )

        loaded, loaded_vectors = load_index(tmp_path)
        assert [entry.title for entry in loaded] == ["Concurrent 0", "Concurrent 1"]
        assert [int(np.argmax(loaded_vectors.reconstruct(row))) for row in range(2)] == [83, 89]

    def test_checkpoint_reconciles_interior_stale_baseline_before_delta(
        self, tmp_path: Path
    ) -> None:
        from autodj.indexer import (
            IncrementalCheckpoint,
            _load_existing_index,
            load_index,
            save_index,
        )

        entries, _ = self._make_entries(3)
        for index, entry in enumerate(entries):
            track_path = tmp_path / f"song_{index}.flac"
            track_path.touch()
            entry.path = str(track_path)
            entry.embedded_at = track_path.stat().st_mtime
        entries[1].embedded_at -= 10.0

        vectors = np.zeros((3, FEATURE_DIM), dtype=np.float32)
        for row, marker in enumerate((3, 7, 11)):
            vectors[row, marker] = 1.0
        index_dir = tmp_path / "index"
        save_index(entries, vectors, index_dir, music_dir=tmp_path)

        (
            existing_entries,
            existing_vectors,
            _,
            baseline_requires_reconcile,
            snapshot_token,
        ) = _load_existing_index(
            index_dir,
            music_dir=tmp_path,
            force=False,
            reindex_modified_since=None,
        )
        assert [entry.path for entry in existing_entries] == [
            entries[0].path,
            entries[2].path,
        ]
        assert [int(np.argmax(vector)) for vector in existing_vectors] == [3, 11]

        replacement = entries[1]
        replacement.embedded_at = Path(replacement.path).stat().st_mtime
        replacement_vector = np.zeros(FEATURE_DIM, dtype=np.float32)
        replacement_vector[19] = 1.0
        checkpoint = IncrementalCheckpoint(
            index_dir=index_dir,
            music_dir=tmp_path,
            existing_entries=existing_entries,
            existing_vectors=existing_vectors,
            total_new=1,
            expected_snapshot=snapshot_token,
            baseline_requires_reconcile=baseline_requires_reconcile,
            flush_every=1,
        )
        checkpoint.write([replacement], [replacement_vector])

        loaded_entries, loaded_index = load_index(index_dir, music_dir=tmp_path)
        assert [entry.path for entry in loaded_entries] == [
            entries[0].path,
            entries[2].path,
            entries[1].path,
        ]
        assert [
            int(np.argmax(loaded_index.reconstruct(row))) for row in range(loaded_index.ntotal)
        ] == [3, 11, 19]

    def test_fused_missing_prune_rejects_generation_published_during_stat(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from dataclasses import replace

        import autodj.indexer as indexer
        from autodj.index_manifest import IndexConsistencyError
        from autodj.indexer import _load_existing_index, load_index, save_index

        entries, vectors = self._make_entries(5)
        mtimes: list[float | None] = []
        for row, entry in enumerate(entries):
            path = tmp_path / f"song_{row}.flac"
            entry.path = str(path)
            if row == 4:
                mtimes.append(None)
            else:
                path.write_bytes(b"")
                mtimes.append(path.stat().st_mtime)
        index_dir = tmp_path / "idx"
        save_index(entries, vectors, index_dir, music_dir=tmp_path)
        concurrent_entries = [
            replace(entry, title=f"Concurrent {row}") for row, entry in enumerate(entries)
        ]
        concurrent_vectors = np.zeros_like(vectors)
        for row, marker in enumerate((53, 59, 61, 67, 71)):
            concurrent_vectors[row] = 0.0
            concurrent_vectors[row, marker] = 1.0

        def stat_after_concurrent_publish(*_args, **_kwargs):
            save_index(
                concurrent_entries,
                concurrent_vectors,
                index_dir,
                music_dir=tmp_path,
            )
            return mtimes

        monkeypatch.setattr(indexer, "_stat_mtimes", stat_after_concurrent_publish)
        with pytest.raises(IndexConsistencyError, match="expected generation"):
            _load_existing_index(
                index_dir,
                music_dir=tmp_path,
                force=False,
                reindex_modified_since=None,
            )

        loaded, loaded_vectors = load_index(index_dir, music_dir=tmp_path)
        assert [entry.title for entry in loaded] == [f"Concurrent {row}" for row in range(5)]
        assert [int(np.argmax(loaded_vectors.reconstruct(row))) for row in range(5)] == [
            53,
            59,
            61,
            67,
            71,
        ]

    def test_force_checkpoint_rejects_generation_published_after_start(
        self,
        tmp_path: Path,
    ) -> None:
        from dataclasses import replace

        from autodj.index_manifest import IndexConsistencyError, IndexSnapshotToken
        from autodj.indexer import (
            IncrementalCheckpoint,
            _load_existing_index,
            load_index,
            save_index,
        )

        entries, vectors = self._make_entries(2)
        index_dir = tmp_path / "idx"
        save_index(entries, vectors, index_dir, music_dir=tmp_path)
        (
            existing_entries,
            existing_vectors,
            _paths,
            baseline_requires_reconcile,
            snapshot_token,
        ) = _load_existing_index(
            index_dir,
            music_dir=tmp_path,
            force=True,
            reindex_modified_since=None,
        )
        assert existing_entries == []
        assert existing_vectors == []
        assert baseline_requires_reconcile is True
        assert isinstance(snapshot_token, IndexSnapshotToken)

        concurrent_entries = [
            replace(entry, title=f"Concurrent {row}") for row, entry in enumerate(entries)
        ]
        concurrent_vectors = np.zeros_like(vectors)
        concurrent_vectors[0, 73] = 1.0
        concurrent_vectors[1, 79] = 1.0
        save_index(concurrent_entries, concurrent_vectors, index_dir, music_dir=tmp_path)
        checkpoint = IncrementalCheckpoint(
            index_dir=index_dir,
            music_dir=tmp_path,
            existing_entries=[],
            existing_vectors=[],
            total_new=1,
            expected_snapshot=snapshot_token,
            baseline_requires_reconcile=True,
            flush_every=1,
        )
        replacement = replace(entries[0], title="Force replacement")
        with pytest.raises(IndexConsistencyError, match="expected generation"):
            checkpoint.write([replacement], [vectors[0]])

        loaded, loaded_vectors = load_index(index_dir, music_dir=tmp_path)
        assert [entry.title for entry in loaded] == ["Concurrent 0", "Concurrent 1"]
        assert [int(np.argmax(loaded_vectors.reconstruct(row))) for row in range(2)] == [73, 79]

    def test_save_index_still_writes_both_files(self, tmp_path: Path) -> None:
        """Public save_index() API kept its both-files contract; the
        split into _save_vectors / _save_tracks_metadata is internal."""
        from autodj.indexer import save_index

        entries, vectors = self._make_entries(2)
        save_index(entries, vectors, tmp_path)
        assert (tmp_path / "vectors.index").exists()
        assert (tmp_path / "tracks.db").exists()
