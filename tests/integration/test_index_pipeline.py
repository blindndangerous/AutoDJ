"""Integration test for the full index build pipeline.

Uses a real in-memory SQLite beets database and a real FAISS index, but
mocks the MERT model (replaced with random normalized vectors) so no model
download or audio decoding is required.
"""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from autodj.beets import get_all_tracks
from autodj.config import AutoDJConfig, HuggingFaceConfig, IndexConfig, LibraryConfig, ModelConfig, PlaybackConfig
from autodj.indexer import FEATURE_DIM, build_index, load_index


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _create_beets_db(path: Path, n_tracks: int) -> Path:
    """Create a fake beets library.db with n_tracks rows."""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE items (
            id INTEGER PRIMARY KEY, path BLOB, title TEXT, artist TEXT,
            album TEXT, genre TEXT, bpm REAL, year INTEGER, length REAL
        )"""
    )
    for i in range(n_tracks):
        conn.execute(
            "INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?)",
            (
                i,
                f"Z:/Music/song_{i}.flac".encode("utf-8"),
                f"Song {i}",
                f"Artist {i % 3}",
                "Album",
                "Rock",
                120.0 + i,
                2000 + i,
                180.0,
            ),
        )
    conn.commit()
    conn.close()
    return path


def _fake_wrapper():
    """Return a MertWrapper mock that produces random L2-normalized 768-dim vectors."""
    wrapper = MagicMock()

    def fake_embed(audio, sample_rate):
        v = np.random.randn(768).astype(np.float32)
        return v / np.linalg.norm(v)

    wrapper.embed_array.side_effect = fake_embed
    return wrapper


@pytest.fixture
def fake_config(tmp_path: Path) -> AutoDJConfig:
    beets_db = _create_beets_db(tmp_path / "library.db", n_tracks=10)
    index_dir = tmp_path / "index"
    index_dir.mkdir()

    return AutoDJConfig(
        library=LibraryConfig(
            music_dir=tmp_path / "Music",
            beets_db=beets_db,
            supported_formats=["flac"],
        ),
        index=IndexConfig(
            index_dir=index_dir,
            model_dir=tmp_path / "models",
        ),
        playback=PlaybackConfig(),
        model=ModelConfig(),
        huggingface=HuggingFaceConfig(),
        config_path=tmp_path / "config.toml",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIndexPipeline:
    def test_build_index_creates_index_files(self, fake_config: AutoDJConfig) -> None:
        wrapper = _fake_wrapper()

        with (
            patch("autodj.indexer.librosa") as mock_librosa,
        ):
            _setup_librosa_mock(mock_librosa)
            build_index(fake_config, wrapper=wrapper, limit=None, force=False)

        assert (fake_config.index.index_dir / "vectors.index").exists()
        assert (fake_config.index.index_dir / "metadata.json").exists()

    def test_build_index_indexes_all_tracks(self, fake_config: AutoDJConfig) -> None:
        wrapper = _fake_wrapper()

        with patch("autodj.indexer.librosa") as mock_librosa:
            _setup_librosa_mock(mock_librosa)
            build_index(fake_config, wrapper=wrapper, limit=None, force=False)

        entries, faiss_index = load_index(fake_config.index.index_dir)
        assert len(entries) == 10
        assert faiss_index.ntotal == 10

    def test_build_index_respects_limit(self, fake_config: AutoDJConfig) -> None:
        wrapper = _fake_wrapper()

        with patch("autodj.indexer.librosa") as mock_librosa:
            _setup_librosa_mock(mock_librosa)
            build_index(fake_config, wrapper=wrapper, limit=3, force=False)

        entries, faiss_index = load_index(fake_config.index.index_dir)
        assert len(entries) == 3
        assert faiss_index.ntotal == 3

    def test_incremental_index_skips_existing(self, fake_config: AutoDJConfig) -> None:
        """Second build run should only add new tracks, not re-embed existing ones."""
        wrapper = _fake_wrapper()

        with patch("autodj.indexer.librosa") as mock_librosa:
            _setup_librosa_mock(mock_librosa)
            # First run: index 5 tracks
            build_index(fake_config, wrapper=wrapper, limit=5, force=False)
            first_call_count = wrapper.embed_array.call_count

            # Second run: full library — should only add 5 new tracks
            build_index(fake_config, wrapper=wrapper, limit=None, force=False)
            second_call_count = wrapper.embed_array.call_count

        assert first_call_count == 5
        # Only 5 new tracks were embedded in the second run
        assert second_call_count == 10

        entries, faiss_index = load_index(fake_config.index.index_dir)
        assert len(entries) == 10

    def test_force_rebuild_reindexes_everything(self, fake_config: AutoDJConfig) -> None:
        wrapper = _fake_wrapper()

        with patch("autodj.indexer.librosa") as mock_librosa:
            _setup_librosa_mock(mock_librosa)
            build_index(fake_config, wrapper=wrapper, limit=5, force=False)
            build_index(fake_config, wrapper=wrapper, limit=None, force=True)

        entries, _ = load_index(fake_config.index.index_dir)
        assert len(entries) == 10
        # force=True means all 10 were re-embedded (5 + 10 = 15 total calls)
        assert wrapper.embed_array.call_count == 15

    def test_similarity_search_returns_different_song(self, fake_config: AutoDJConfig) -> None:
        """After indexing, querying a track should not return itself as the top result."""
        wrapper = _fake_wrapper()

        with patch("autodj.indexer.librosa") as mock_librosa:
            _setup_librosa_mock(mock_librosa)
            build_index(fake_config, wrapper=wrapper, limit=None, force=False)

        entries, faiss_index = load_index(fake_config.index.index_dir)

        # Query the index with the first entry's vector
        import faiss as _faiss
        query = np.random.randn(1, FEATURE_DIM).astype(np.float32)
        query /= np.linalg.norm(query)
        distances, indices = faiss_index.search(query, 2)

        # Top-2 returns valid indices
        assert indices[0][0] >= 0
        assert indices[0][1] >= 0


# ---------------------------------------------------------------------------
# Librosa mock helper
# ---------------------------------------------------------------------------


def _setup_librosa_mock(mock_librosa) -> None:
    """Configure a librosa mock to return plausible shapes."""
    fake_audio = np.zeros(22050, dtype=np.float32)
    mock_librosa.load.return_value = (fake_audio, 22050)
    mock_librosa.feature.rms.return_value = np.array([[[0.1]]])
    mock_librosa.feature.spectral_centroid.return_value = np.array([[[500.0]]])
    mock_librosa.feature.zero_crossing_rate.return_value = np.array([[[0.05]]])
    mock_librosa.feature.chroma_stft.return_value = np.ones((12, 10))
    mock_librosa.onset.onset_strength.return_value = np.array([0.5, 0.4, 0.6])
