"""Unit tests for autodj.indexer.

The MERT model and librosa are mocked so tests run without audio files or
model downloads. Vector math and FAISS operations use real numpy/faiss.
"""

import json
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
    build_faiss_index,
    load_index,
    save_index,
    walk_music_dir,
)


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


def _random_embedding(dim: int = 768) -> np.ndarray:
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
    def test_returns_16_dim_vector(self, tmp_path: Path) -> None:
        fake_audio = np.zeros(22050, dtype=np.float32)
        fake_sr = 22050

        with patch("autodj.indexer.librosa") as mock_librosa:
            mock_librosa.load.return_value = (fake_audio, fake_sr)
            mock_librosa.feature.rms.return_value = np.array([[[0.1]]])
            mock_librosa.feature.spectral_centroid.return_value = np.array([[[500.0]]])
            mock_librosa.feature.zero_crossing_rate.return_value = np.array([[[0.05]]])
            mock_librosa.feature.chroma_stft.return_value = np.ones((12, 10))
            mock_librosa.onset.onset_strength.return_value = np.array([0.5, 0.6, 0.4])

            vec, audio, sr = _extract_librosa_features(tmp_path / "song.flac")

        assert vec.shape == (16,)
        assert audio.dtype == np.float32
        assert sr == fake_sr

    def test_vector_is_finite(self, tmp_path: Path) -> None:
        fake_audio = np.random.randn(22050).astype(np.float32)
        fake_sr = 22050

        with patch("autodj.indexer.librosa") as mock_librosa:
            mock_librosa.load.return_value = (fake_audio, fake_sr)
            mock_librosa.feature.rms.return_value = np.array([[[0.1]]])
            mock_librosa.feature.spectral_centroid.return_value = np.array([[[500.0]]])
            mock_librosa.feature.zero_crossing_rate.return_value = np.array([[[0.05]]])
            mock_librosa.feature.chroma_stft.return_value = np.ones((12, 10))
            mock_librosa.onset.onset_strength.return_value = np.array([0.5])

            vec, _, _ = _extract_librosa_features(tmp_path / "song.flac")

        assert np.isfinite(vec).all()


# ---------------------------------------------------------------------------
# _combine_features
# ---------------------------------------------------------------------------


class TestCombineFeatures:
    def test_output_dim_matches_feature_dim(self) -> None:
        mert_vec = _random_embedding(768)
        librosa_vec = np.random.randn(16).astype(np.float32)
        result = _combine_features(mert_vec, librosa_vec)
        assert result.shape == (FEATURE_DIM,)

    def test_output_is_l2_normalized(self) -> None:
        mert_vec = _random_embedding(768)
        librosa_vec = np.random.randn(16).astype(np.float32)
        result = _combine_features(mert_vec, librosa_vec)
        norm = np.linalg.norm(result)
        assert abs(norm - 1.0) < 1e-5

    def test_output_is_float32(self) -> None:
        mert_vec = _random_embedding(768)
        librosa_vec = np.random.randn(16).astype(np.float32)
        result = _combine_features(mert_vec, librosa_vec)
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
        distances, indices = index.search(query, 1)
        assert indices[0][0] == 3


# ---------------------------------------------------------------------------
# save_index / load_index
# ---------------------------------------------------------------------------


class TestSaveLoadIndex:
    def _make_entries(self, n: int) -> tuple[list[IndexEntry], np.ndarray]:
        entries = [
            IndexEntry(
                path=f"Z:/Music/song_{i}.flac",
                title=f"Song {i}",
                artist="Artist",
                album="Album",
                genre="Rock",
                bpm=120.0,
                year=2000,
                length=180.0,
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

    def test_metadata_json_written(self, tmp_path: Path) -> None:
        entries, vectors = self._make_entries(3)
        index_dir = tmp_path / "index"
        index_dir.mkdir()

        save_index(entries, vectors, index_dir)

        metadata_path = index_dir / "metadata.json"
        assert metadata_path.exists()
        data = json.loads(metadata_path.read_text())
        assert len(data) == 3
        assert data[0]["path"] == "Z:/Music/song_0.flac"

    def test_faiss_index_file_written(self, tmp_path: Path) -> None:
        entries, vectors = self._make_entries(3)
        index_dir = tmp_path / "index"
        index_dir.mkdir()

        save_index(entries, vectors, index_dir)

        assert (index_dir / "vectors.index").exists()

    def test_load_raises_if_index_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_index(tmp_path / "nonexistent")

    def test_metadata_path_preserved(self, tmp_path: Path) -> None:
        entries, vectors = self._make_entries(2)
        index_dir = tmp_path / "index"
        index_dir.mkdir()

        save_index(entries, vectors, index_dir)
        loaded_entries, _ = load_index(index_dir)

        assert loaded_entries[0].path == "Z:/Music/song_0.flac"
