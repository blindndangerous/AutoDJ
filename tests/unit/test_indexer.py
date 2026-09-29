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
    _collect_tracks_to_index,
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
        paths = list(walk_music_dir(tmp_path, ["mp3", "flac"]))
        assert Path(tmp_path / "a.mp3") in paths
        assert Path(tmp_path / "b.flac") in paths
        assert Path(tmp_path / "c.txt") not in paths

    def test_recurses_subdirectories(self, tmp_path: Path) -> None:
        sub = tmp_path / "Artist" / "Album"
        sub.mkdir(parents=True)
        (sub / "song.flac").touch()
        paths = list(walk_music_dir(tmp_path, ["flac"]))
        assert sub / "song.flac" in paths

    def test_empty_dir_returns_empty_list(self, tmp_path: Path) -> None:
        assert list(walk_music_dir(tmp_path, ["mp3"])) == []

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

    def test_round_trip_keeps_rows_in_vector_order(self, tmp_path: Path) -> None:
        entries, vectors = self._make_entries(5)

        manifest = save_index(entries, vectors, tmp_path, base_generation=0)
        loaded_entries, loaded_index, loaded_manifest = load_index(tmp_path, music_dir=tmp_path)

        assert loaded_manifest == manifest
        assert [e.path for e in loaded_entries] == [str(tmp_path / e.path) for e in entries]
        np.testing.assert_allclose(loaded_index.reconstruct_n(0, 5), vectors, rtol=1e-6)

    def test_empty_index_round_trips(self, tmp_path: Path) -> None:
        save_index([], np.empty((0, FEATURE_DIM)), tmp_path, base_generation=0)

        loaded_entries, loaded_index, _ = load_index(tmp_path)

        assert loaded_entries == []
        assert loaded_index.ntotal == 0

    def test_load_raises_if_index_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_index(tmp_path / "nonexistent")

    @pytest.mark.parametrize("attribute", ["tracks_file", "vectors_file"])
    def test_load_refuses_a_file_that_does_not_match_the_manifest(
        self, tmp_path: Path, attribute: str
    ) -> None:
        from autodj.index_manifest import IndexConsistencyError

        entries, vectors = self._make_entries(3)
        manifest = save_index(entries, vectors, tmp_path, base_generation=0)
        damaged = tmp_path / getattr(manifest, attribute)
        damaged.write_bytes(damaged.read_bytes()[:-1])

        with pytest.raises(IndexConsistencyError, match="does not match its SHA-256"):
            load_index(tmp_path)

    def test_load_refuses_a_count_that_does_not_match_the_manifest(self, tmp_path: Path) -> None:
        from autodj.index_manifest import IndexConsistencyError, publish_generation
        from autodj.indexer import _entry_to_row, _write_faiss_chunked, _write_tracks_file

        entries, vectors = self._make_entries(3)

        def write(tracks: Path, vectors_path: Path) -> None:
            _write_tracks_file([_entry_to_row(e, None, i) for i, e in enumerate(entries)], tracks)
            _write_faiss_chunked(build_faiss_index(vectors[:2]), vectors_path)

        publish_generation(tmp_path, base_generation=0, vector_count=3, write_files=write)

        with pytest.raises(IndexConsistencyError, match="count mismatch"):
            load_index(tmp_path)

    def test_load_refuses_absolute_track_path(self, tmp_path: Path) -> None:
        from autodj.index_manifest import UnsupportedIndexError, publish_generation
        from autodj.indexer import _entry_to_row, _write_faiss_chunked, _write_tracks_file

        entries, vectors = self._make_entries(1)
        row = _entry_to_row(entries[0], None, 0) | {"path": "Z:/Music/song_0.flac"}

        def write(tracks: Path, vectors_path: Path) -> None:
            _write_tracks_file([row], tracks)
            _write_faiss_chunked(build_faiss_index(vectors), vectors_path)

        publish_generation(tmp_path, base_generation=0, vector_count=1, write_files=write)

        with pytest.raises(UnsupportedIndexError, match=r"absolute path Z:/Music/song_0.flac"):
            load_index(tmp_path, music_dir=tmp_path)


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
        save_index(entries, vectors, index_dir, music_dir=tmp_path, base_generation=0)
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

    def test_prune_keeps_each_survivor_with_its_own_vector(self, tmp_path: Path) -> None:
        from autodj.indexer import load_index, prune_index, save_index

        entries, vectors = self._distinctive_entries(tmp_path, missing_rows={1})
        idx = tmp_path / "idx"
        save_index(entries, vectors, idx, music_dir=tmp_path, base_generation=0)

        assert prune_index(idx, allow_mass_prune=True, music_dir=tmp_path) == (1, 2)
        loaded, loaded_vectors, _ = load_index(idx, music_dir=tmp_path)
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
        save_index(entries, vectors, idx, music_dir=tmp_path, base_generation=0)
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
                save_index(
                    concurrent_entries,
                    concurrent_vectors,
                    idx,
                    music_dir=tmp_path,
                    base_generation=1,
                )

        with (
            patch(
                "autodj.indexer._check_prune_safety",
                side_effect=check_after_concurrent_publish,
            ),
            pytest.raises(IndexConsistencyError, match="another command published"),
        ):
            prune_index(idx, allow_mass_prune=True, music_dir=tmp_path)

        current = read_manifest(idx)
        assert current is not None
        assert current.generation == first.generation + 1
        loaded, loaded_vectors, _ = load_index(idx, music_dir=tmp_path)
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
        save_index(entries, vectors, idx, music_dir=tmp_path, base_generation=0)
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
            save_index(replacement, replacement_vectors, idx, music_dir=tmp_path, base_generation=1)

        with (
            patch(
                "autodj.indexer._check_prune_safety",
                side_effect=publish_replacement,
            ),
            pytest.raises(IndexConsistencyError, match="another command published"),
        ):
            prune_index(idx, allow_mass_prune=True, music_dir=tmp_path)

        loaded, loaded_vectors, _ = load_index(idx, music_dir=tmp_path)
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

    def test_all_missing_publishes_an_empty_generation(self, tmp_path: Path) -> None:
        from autodj.indexer import load_index, prune_index

        idx = self._save_with_files(tmp_path, n_present=0, n_missing=3)
        removed, kept = prune_index(idx, allow_mass_prune=True, music_dir=tmp_path)
        assert removed == 3
        assert kept == 0
        loaded, loaded_vectors, manifest = load_index(idx, music_dir=tmp_path)
        assert loaded == []
        assert loaded_vectors.ntotal == 0
        assert manifest.generation == 2


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

    def test_missing_beets_db_raises_instead_of_reporting_in_sync(self, tmp_path: Path) -> None:
        from autodj.beets import BeetsNotFoundError
        from autodj.indexer import enrich_from_beets, save_index

        entries, vectors = self._make_distinctive_index(tmp_path)
        idx = tmp_path / "idx"
        idx.mkdir()
        save_index(entries, vectors, idx, music_dir=tmp_path, base_generation=0)

        with pytest.raises(BeetsNotFoundError, match="Beets library not found"):
            enrich_from_beets(idx, music_dir=tmp_path, beets_db=tmp_path / "missing.db")

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
        save_index(entries, vectors, idx, music_dir=tmp_path, base_generation=0)
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
                save_index(
                    concurrent_entries,
                    concurrent_vectors,
                    idx,
                    music_dir=tmp_path,
                    base_generation=1,
                )
            return real_apply(*args, **kwargs)

        with (
            patch(
                "autodj.indexer._apply_beets_row",
                side_effect=apply_after_concurrent_publish,
            ),
            pytest.raises(IndexConsistencyError, match="another command published"),
        ):
            enrich_from_beets(idx, music_dir=tmp_path, beets_db=beets)

        current = read_manifest(idx)
        assert current is not None
        assert current.generation == first.generation + 1
        loaded, loaded_vectors, _ = load_index(idx, music_dir=tmp_path)
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
        save_index(entries, vectors, idx, music_dir=tmp_path, base_generation=0)
        # Set up beets DB with a key
        beets = tmp_path / "library.db"
        self._make_beets(beets, [{"path": str(path), "initial_key": "Am"}])

        updated, total = enrich_from_beets(idx, music_dir=tmp_path, beets_db=beets)
        assert updated == 1
        assert total == 1
        # Reload and verify
        from autodj.indexer import load_index

        loaded, _, _ = load_index(idx, music_dir=tmp_path)
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
        save_index(entries, vectors, idx, music_dir=tmp_path, base_generation=0)
        beets = tmp_path / "library.db"
        self._make_beets(beets, [{"path": str(path), "initial_key": "Am"}])

        updated, total = enrich_from_beets(idx, music_dir=tmp_path, beets_db=beets)
        assert updated == 0
        assert total == 1


# ---------------------------------------------------------------------------
# Index entry path roundtrips with music_dir
# ---------------------------------------------------------------------------


def _stale(entries: list[IndexEntry]) -> set[str]:
    from autodj.indexer import _detect_stale_entries, _stat_mtimes

    return _detect_stale_entries(entries, _stat_mtimes(entries))


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

        f = tmp_path / "song.flac"
        f.write_bytes(b"original")
        original_mtime = f.stat().st_mtime
        # Embedded an hour ago, then file replaced now
        e = self._entry(str(f), embedded_at=original_mtime - 3600)
        os.utime(f, (original_mtime, original_mtime))
        assert e.path in _stale([e])

    def test_unstamped_entry_is_re_embedded(self, tmp_path: Path) -> None:

        f = tmp_path / "song.flac"
        f.write_bytes(b"x")
        e = self._entry(str(f), embedded_at=0.0)
        assert _stale([e]) == {e.path}

    def test_epoch_mtime_file_is_not_re_embedded_every_run(self, tmp_path: Path) -> None:
        """A file dated at the epoch is stamped 0 when embedded; that is current, not stale."""

        f = tmp_path / "song.flac"
        f.write_bytes(b"x")
        os.utime(f, (0, 0))
        e = self._entry(str(f), embedded_at=0.0)
        assert _stale([e]) == set()

    def test_unchanged_file_not_stale(self, tmp_path: Path) -> None:

        f = tmp_path / "song.flac"
        f.write_bytes(b"x")
        # Embedded just after file creation
        e = self._entry(str(f), embedded_at=f.stat().st_mtime + 60)
        assert e.path not in _stale([e])

    def test_missing_file_skipped(self, tmp_path: Path) -> None:
        # prune handles missing files; stale detection ignores them

        e = self._entry(str(tmp_path / "gone.flac"), embedded_at=1.0)
        assert _stale([e]) == set()

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

        assert _stale([entry]) == set()

    def test_a_later_edit_is_still_stale(self, tmp_path: Path) -> None:

        track = self._future_track(tmp_path, ahead_s=3600)
        entry = IndexEntry.from_track(track)
        touched = entry.embedded_at + 120
        os.utime(track.path, (touched, touched))

        assert _stale([entry]) == {entry.path}


class TestRelativizeForStorage:
    def test_strips_music_dir_prefix(self, tmp_path: Path) -> None:
        from autodj.index_manifest import relative_storage_path

        md = tmp_path / "Music"
        md.mkdir()
        assert relative_storage_path(str(md / "Artist" / "song.flac"), md) == "Artist/song.flac"

    def test_rejects_path_outside_music_dir(self, tmp_path: Path) -> None:
        from autodj.index_manifest import relative_storage_path

        md = tmp_path / "Music"
        md.mkdir()
        with pytest.raises(ValueError, match="not under music_dir"):
            relative_storage_path(str(tmp_path / "elsewhere" / "song.flac"), md)

    def test_does_not_stat_filesystem(self, tmp_path: Path) -> None:
        # Resolve() / is_relative_to() on real Paths used to dominate save_index
        # for libraries on NFS — see index_manifest.relative_storage_path.
        # Must work on paths that don't exist.
        from autodj.index_manifest import relative_storage_path

        md = tmp_path / "Music"  # never created
        fake = md / "Artist" / "song.flac"
        assert relative_storage_path(str(fake), md) == "Artist/song.flac"

    @pytest.mark.skipif(os.name != "nt", reason="case-insensitive paths are a Windows rule")
    def test_case_and_separator_mismatch_is_under_music_dir(self) -> None:
        """Beets may spell the drive or folders differently from music_dir on Windows."""
        from autodj.index_manifest import relative_storage_path

        md = Path("c:/music")
        assert relative_storage_path(r"C:\Music\Artist/Song.flac", md) == "Artist/Song.flac"

    def test_no_music_dir_keeps_relative_path(self) -> None:
        from autodj.index_manifest import relative_storage_path

        assert relative_storage_path("Artist/song.flac", None) == "Artist/song.flac"


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
        manifest = save_index(entries, v, idx, music_dir=music_dir, base_generation=0)
        # Inspect raw metadata in the SQLite store.
        import sqlite3 as _sql

        conn = _sql.connect(idx / manifest.tracks_file)
        try:
            stored_path = conn.execute(
                "SELECT path FROM tracks ORDER BY vec_row ASC LIMIT 1"
            ).fetchone()[0]
        finally:
            conn.close()
        # Should be stored as relative
        assert stored_path == "a.flac" or stored_path.endswith("a.flac")
        # Round-trip resolves back to absolute
        loaded, _, _ = load_index(idx, music_dir=music_dir)
        assert Path(loaded[0].path).resolve() == (music_dir / "a.flac").resolve()


# ---------------------------------------------------------------------------
# backfill_dj_meta + _analyse_one_track
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
    def test_empty_audio_returns_none(self, tmp_path: Path) -> None:
        from autodj.indexer import _analyse_one_track

        with patch(
            "autodj.indexer._load_audio", return_value=(np.zeros(0, dtype=np.float32), 24000)
        ):
            assert _analyse_one_track(str(tmp_path / "x.flac")) is None


def _backfill_cfg(tmp_path: Path, *, throttle_ms: float = 0.0, import_cues: bool = False):
    from autodj.config import (
        AutoDJConfig,
        HuggingFaceConfig,
        IndexConfig,
        LibraryConfig,
        ModelConfig,
        PlaybackConfig,
    )

    return AutoDJConfig(
        library=LibraryConfig(music_dir=tmp_path / "music"),
        index=IndexConfig(index_dir=tmp_path / "index", throttle_ms=throttle_ms),
        playback=PlaybackConfig(import_external_cues=import_cues),
        model=ModelConfig(),
        huggingface=HuggingFaceConfig(),
        config_path=None,
    )


def test_index_limit_reads_only_the_tags_it_needs(tmp_path: Path) -> None:
    # `autodj index --limit 60` read the tags of all 76,728 tracks on a NAS
    # before applying the limit.  Indexed tracks are skipped unread too.
    from autodj.audio_meta import FileTags

    cfg = _backfill_cfg(tmp_path)
    for album in ("A", "B", "C"):
        (cfg.library.music_dir / album).mkdir(parents=True)
        for n in range(4):
            (cfg.library.music_dir / album / f"{n}.flac").touch()
    indexed = {str(cfg.library.music_dir / "A" / "0.flac")}

    with patch("autodj.audio_meta.read_file_tags", return_value=FileTags()) as read:
        tracks = _collect_tracks_to_index(cfg, indexed, limit=3)

    wanted = [cfg.library.music_dir / "A" / f"{n}.flac" for n in (1, 2, 3)]
    assert [t.path for t in tracks] == wanted
    assert [c.args[0] for c in read.call_args_list] == wanted


def test_beets_items_with_missing_files_are_skipped_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Beets listed 11 deleted files; every `autodj index` pruned them, then
    # queued them again as new and failed all 11 in ffmpeg.
    import dataclasses
    import sqlite3

    base = _backfill_cfg(tmp_path)
    music = base.library.music_dir
    (music / "A").mkdir(parents=True)
    (music / "A" / "real.flac").touch()
    beets = tmp_path / "library.db"
    with sqlite3.connect(beets) as conn:
        conn.execute(
            "CREATE TABLE items (path BLOB, title TEXT, artist TEXT, album TEXT,"
            " genre TEXT, bpm REAL, year INTEGER, length REAL)"
        )
        conn.executemany(
            "INSERT INTO items (path) VALUES (?)", [(b"A/gone.flac",), (b"A/real.flac",)]
        )
    conn.close()
    cfg = dataclasses.replace(base, library=dataclasses.replace(base.library, beets_db=beets))

    with caplog.at_level("WARNING", logger="autodj.indexer"):
        tracks = _collect_tracks_to_index(cfg, set(), limit=None)

    assert [t.path for t in tracks] == [music / "A" / "real.flac"]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "1 beets items" in warnings[0]
    assert "gone.flac" in warnings[0]


class TestBackfillDjMeta:
    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        from autodj.dj_meta import close_cache

        close_cache()
        yield
        close_cache()

    def _entries(self, cfg, *names: str) -> list[IndexEntry]:
        return [_entry(str(cfg.library.music_dir / name)) for name in names]

    def test_analyses_missing_rows_and_drops_rows_of_unindexed_tracks(self, tmp_path: Path) -> None:
        from autodj.dj_meta import DjMeta, get_cache
        from autodj.indexer import backfill_dj_meta

        cfg = _backfill_cfg(tmp_path)
        cache = get_cache(cfg.index.active_dir, music_dir=cfg.library.music_dir)
        assert cache is not None
        gone = str(cfg.library.music_dir / "gone.flac")
        cache.set(gone, DjMeta(analysed=True))
        cache.flush(force=True)
        entries = self._entries(cfg, "a.flac", "b.flac")

        def analyse(path: str) -> DjMeta:
            if path.endswith("b.flac"):
                raise RuntimeError("bad file")
            return DjMeta(analysed=True, intro_end_s=1.0)

        with patch("autodj.indexer._analyse_one_track", side_effect=analyse):
            backfill_dj_meta(cfg, entries)

        assert cache.get(entries[0].path).intro_end_s == 1.0
        assert not cache.get(entries[1].path).analysed
        assert not cache.get(gone).analysed

    def test_limit_analyses_only_that_many_and_keeps_every_other_row(self, tmp_path: Path) -> None:
        """``analyse --limit`` used to delete the DJ data of every track past the limit."""
        from autodj.dj_meta import Cue, DjMeta, get_cache
        from autodj.indexer import backfill_dj_meta

        cfg = _backfill_cfg(tmp_path)
        cache = get_cache(cfg.index.active_dir, music_dir=cfg.library.music_dir)
        assert cache is not None
        entries = self._entries(cfg, "done.flac", "next.flac", "later.flac")
        imported = DjMeta(analysed=True, cues=[Cue(time_s=4.0, source="mixxx")])
        cache.set(entries[0].path, imported)
        cache.flush(force=True)

        with patch(
            "autodj.indexer._analyse_one_track", return_value=DjMeta(analysed=True)
        ) as analyse:
            backfill_dj_meta(cfg, entries, limit=1)

        assert [c.args[0] for c in analyse.call_args_list] == [entries[1].path]
        assert cache.get(entries[0].path) == imported
        assert cache.get(entries[1].path).analysed
        assert not cache.get(entries[2].path).analysed

    @pytest.mark.parametrize(("throttle_ms", "sleeps"), [(250.0, [0.25, 0.25]), (0.0, [])])
    def test_throttle_pauses_before_each_track(
        self, tmp_path: Path, throttle_ms: float, sleeps: list[float]
    ) -> None:
        from autodj.dj_meta import DjMeta
        from autodj.indexer import backfill_dj_meta

        cfg = _backfill_cfg(tmp_path, throttle_ms=throttle_ms)
        slept: list[float] = []
        with (
            patch("autodj.indexer._analyse_one_track", return_value=DjMeta(analysed=True)),
            patch("autodj.indexer.time.sleep", side_effect=slept.append),
        ):
            backfill_dj_meta(cfg, self._entries(cfg, "a.flac", "b.flac"))

        assert slept == sleeps

    @pytest.mark.parametrize("import_cues", [True, False])
    def test_serato_cues_are_merged_when_the_import_is_on(
        self, tmp_path: Path, import_cues: bool
    ) -> None:
        from autodj.dj_meta import Cue, DjMeta, get_cache
        from autodj.indexer import backfill_dj_meta
        from tests.unit._fakes import write_serato_flac

        cfg = _backfill_cfg(tmp_path, import_cues=import_cues)
        cfg.library.music_dir.mkdir()
        track = cfg.library.music_dir / "tagged.flac"
        serato = write_serato_flac(track)
        auto = Cue(time_s=40.0, type="drop", source="auto")

        with (
            patch(
                "autodj.indexer._analyse_one_track", return_value=DjMeta(analysed=True, cues=[auto])
            ),
            patch("autodj.dj_cues_import.auto_import_cues", return_value={}),
        ):
            backfill_dj_meta(cfg, [_entry(str(track))])

        cache = get_cache()
        assert cache is not None
        cues = cache.get(str(track)).cues
        assert cues == ([serato, auto] if import_cues else [auto])


# ---------------------------------------------------------------------------
# Throttled FAISS checkpoint + crash-recovery reconciliation
# ---------------------------------------------------------------------------


class TestIncrementalCheckpoint:
    """Every-N publication during ``autodj index`` and resuming from it."""

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

    def test_publishes_every_flush_every_tracks_and_the_rest_on_finish(
        self, tmp_path: Path
    ) -> None:
        from autodj.index_manifest import read_manifest
        from autodj.indexer import IncrementalCheckpoint

        existing, existing_vectors = self._make_entries(1)
        save_index(existing, existing_vectors, tmp_path, base_generation=0)
        new, new_vectors = self._make_entries(3)
        for entry in new:
            entry.path = "new_" + entry.path
        checkpoint = IncrementalCheckpoint(
            index_dir=tmp_path,
            music_dir=None,
            existing_entries=existing,
            existing_vectors=list(existing_vectors),
            base_generation=1,
            flush_every=2,
        )

        checkpoint.write(new[:1], list(new_vectors[:1]))
        assert read_manifest(tmp_path).generation == 1  # type: ignore[union-attr]
        checkpoint.write(new[:2], list(new_vectors[:2]))
        assert read_manifest(tmp_path).vector_count == 3  # type: ignore[union-attr]
        checkpoint.finish(new, list(new_vectors))

        loaded, loaded_vectors, manifest = load_index(tmp_path)
        assert manifest.generation == 3
        assert [e.path for e in loaded] == [e.path for e in existing + new]
        np.testing.assert_allclose(
            loaded_vectors.reconstruct_n(0, 4),
            np.vstack([existing_vectors, new_vectors]),
            rtol=1e-6,
        )

    def test_replaced_file_is_dropped_and_its_new_vector_appended(self, tmp_path: Path) -> None:
        from autodj.indexer import IncrementalCheckpoint, _load_existing_index

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
        save_index(entries, vectors, index_dir, music_dir=tmp_path, base_generation=0)

        existing_entries, existing_vectors, base = _load_existing_index(
            index_dir, music_dir=tmp_path, force=False
        )
        assert [entry.path for entry in existing_entries] == [entries[0].path, entries[2].path]
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
            base_generation=base,
        )
        checkpoint.finish([replacement], [replacement_vector])

        loaded_entries, loaded_index, _ = load_index(index_dir, music_dir=tmp_path)
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
        from autodj.indexer import _load_existing_index

        entries, vectors = self._make_entries(5)
        mtimes: list[float | None] = []
        for row, entry in enumerate(entries):
            path = tmp_path / f"song_{row}.flac"
            entry.path = str(path)
            if row == 4:
                mtimes.append(None)
            else:
                path.write_bytes(b"")
                mtimes.append(path.stat().st_mtime + 3600)
                entry.embedded_at = mtimes[-1]
        index_dir = tmp_path / "idx"
        save_index(entries, vectors, index_dir, music_dir=tmp_path, base_generation=0)
        concurrent_entries = [
            replace(entry, title=f"Concurrent {row}") for row, entry in enumerate(entries)
        ]

        def stat_after_concurrent_publish(*_args, **_kwargs):
            save_index(
                concurrent_entries, vectors, index_dir, music_dir=tmp_path, base_generation=1
            )
            return mtimes

        monkeypatch.setattr(indexer, "_stat_mtimes", stat_after_concurrent_publish)
        with pytest.raises(IndexConsistencyError, match="another command published"):
            _load_existing_index(index_dir, music_dir=tmp_path, force=False)

        loaded, _, _ = load_index(index_dir, music_dir=tmp_path)
        assert [entry.title for entry in loaded] == [f"Concurrent {row}" for row in range(5)]

    def test_force_rebuild_refuses_to_overwrite_a_generation_published_after_start(
        self, tmp_path: Path
    ) -> None:
        from dataclasses import replace

        from autodj.index_manifest import IndexConsistencyError
        from autodj.indexer import IncrementalCheckpoint, _load_existing_index

        entries, vectors = self._make_entries(2)
        index_dir = tmp_path / "idx"
        save_index(entries, vectors, index_dir, music_dir=tmp_path, base_generation=0)
        existing_entries, existing_vectors, base = _load_existing_index(
            index_dir, music_dir=tmp_path, force=True
        )
        assert (existing_entries, existing_vectors, base) == ([], [], 1)

        concurrent = [replace(entry, title="Concurrent") for entry in entries]
        save_index(concurrent, vectors, index_dir, music_dir=tmp_path, base_generation=1)
        checkpoint = IncrementalCheckpoint(
            index_dir=index_dir,
            music_dir=tmp_path,
            existing_entries=[],
            existing_vectors=[],
            base_generation=base,
        )
        with pytest.raises(IndexConsistencyError, match="another command published"):
            checkpoint.finish([entries[0]], [vectors[0]])

        loaded, _, _ = load_index(index_dir, music_dir=tmp_path)
        assert [entry.title for entry in loaded] == ["Concurrent", "Concurrent"]

    def test_embed_propagates_checkpoint_failure(self, tmp_path: Path) -> None:
        from autodj.indexer import _embed_new_tracks

        track = _fake_track(str(tmp_path / "song.flac"))
        wrapper = MagicMock()
        wrapper.embed_array.return_value = _random_embedding()
        features = (
            np.ones(16, dtype=np.float32),
            np.zeros(32, dtype=np.float32),
            22050,
            {"energy": 0.0, "key": -1, "mode": -1, "bpm": 0.0, "tempo_confidence": 0.0},
        )

        def fail(*_args: object) -> None:
            raise OSError("checkpoint failed")

        with (
            patch("autodj.indexer._extract_librosa_features", return_value=features),
            pytest.raises(OSError, match="checkpoint failed"),
        ):
            _embed_new_tracks([track], wrapper, 1, fail, 0.0)


class TestIndexRecoveryPaths:
    """Destructive --force handling and refusals for index folders that are not current."""

    _WORKING = ("tracks.db", "tracks.db-wal", "tracks.db-shm", "vectors.index")
    _KEPT = (
        "dj_meta.db",
        "web_state.json",
        "tracks.g00000000000000000001.db",
        "vectors.g00000000000000000001.index",
    )

    def _fill(self, index_dir: Path) -> None:
        index_dir.mkdir()
        for name in (*self._WORKING, *self._KEPT):
            (index_dir / name).write_bytes(b"x")
        (index_dir / "liners").mkdir()
        (index_dir / "liners" / "drop.mp3").write_bytes(b"x")

    def test_force_without_manifest_removes_only_working_files(self, tmp_path: Path) -> None:
        from autodj.indexer import _load_existing_index

        index_dir = tmp_path / "idx"
        self._fill(index_dir)

        entries, vectors, _base = _load_existing_index(index_dir, music_dir=tmp_path, force=True)

        assert (entries, vectors) == ([], [])
        assert not any((index_dir / name).exists() for name in self._WORKING)
        assert all((index_dir / name).exists() for name in self._KEPT)
        assert (index_dir / "liners" / "drop.mp3").exists()

    def test_pre_manifest_index_is_refused_without_force(self, tmp_path: Path) -> None:
        from autodj.index_manifest import UnsupportedIndexError
        from autodj.indexer import _load_existing_index

        index_dir = tmp_path / "idx"
        index_dir.mkdir()
        (index_dir / "tracks.db").write_bytes(b"x")
        (index_dir / "vectors.index").write_bytes(b"x")

        with pytest.raises(UnsupportedIndexError, match=r"no index-manifest\.json.*--force"):
            _load_existing_index(index_dir, music_dir=tmp_path, force=False)
        assert (index_dir / "tracks.db").exists()
        assert (index_dir / "vectors.index").exists()

    def test_force_replaces_an_old_manifest(self, tmp_path: Path) -> None:
        """--force is the rebuild path, so an unreadable old manifest must not stop it."""
        import json

        from autodj.index_manifest import MANIFEST_NAME
        from autodj.indexer import _load_existing_index

        index_dir = tmp_path / "idx"
        self._fill(index_dir)
        (index_dir / MANIFEST_NAME).write_text(json.dumps({"schema_version": 2}), "utf-8")

        entries, _vectors, base = _load_existing_index(index_dir, music_dir=tmp_path, force=True)

        assert entries == []
        assert base == 0
        assert not (index_dir / MANIFEST_NAME).exists()
        assert not any((index_dir / name).exists() for name in self._WORKING)

    def test_build_killed_before_first_publish_leaves_nothing_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import autodj.indexer as indexer
        from autodj.indexer import _load_existing_index, save_index

        entries, vectors = TestSaveLoadIndex()._make_entries(2)
        index_dir = tmp_path / "idx"

        def killed(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("killed before publication")

        monkeypatch.setattr(indexer, "_write_faiss_chunked", killed)
        with pytest.raises(RuntimeError, match="killed"):
            save_index(entries, vectors, index_dir, base_generation=0)
        monkeypatch.undo()

        assert [p.name for p in index_dir.iterdir() if not p.name.endswith(".lock")] == []
        assert _load_existing_index(index_dir, music_dir=tmp_path, force=False) == ([], [], 0)

    @pytest.mark.parametrize("command", ["prune", "enrich"])
    def test_generation_files_without_manifest_are_not_reported_as_no_index(
        self, tmp_path: Path, command: str
    ) -> None:
        from autodj.index_manifest import IndexConsistencyError
        from autodj.indexer import enrich_from_beets, prune_index

        index_dir = tmp_path / "idx"
        index_dir.mkdir()
        (index_dir / "tracks.g00000000000000000003.db").write_bytes(b"x")

        with pytest.raises(IndexConsistencyError, match=r"no index-manifest\.json.*--force"):
            if command == "prune":
                prune_index(index_dir, music_dir=tmp_path)
            else:
                enrich_from_beets(index_dir, music_dir=tmp_path, beets_db=tmp_path / "b.db")
