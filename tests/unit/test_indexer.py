"""Unit tests for autodj.indexer.

The MuQ model and librosa are mocked so tests run without audio files or
model downloads. Vector math and FAISS operations use real numpy/faiss.
"""

import json
from pathlib import Path
from unittest.mock import patch

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
        assert 0 <= extra_meta["key"] <= 11
        assert extra_meta["mode"] in (0, 1)
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
                path=f"Z:/Music/song_{i}.flac",
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
            rows = conn.execute("SELECT path FROM tracks ORDER BY id ASC").fetchall()
        finally:
            conn.close()
        assert len(rows) == 3
        assert rows[0][0] == "Z:/Music/song_0.flac"

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
        save_index(entries, vectors, index_dir)
        return index_dir

    def test_no_index_returns_zero(self, tmp_path: Path) -> None:
        from autodj.indexer import prune_index

        assert prune_index(tmp_path / "noidx") == (0, 0)

    def test_prunes_missing_files(self, tmp_path: Path) -> None:
        from autodj.indexer import prune_index

        idx = self._save_with_files(tmp_path, n_present=10, n_missing=2)
        removed, kept = prune_index(idx)
        assert removed == 2
        assert kept == 10

    def test_no_missing_returns_zero_removed(self, tmp_path: Path) -> None:
        from autodj.indexer import prune_index

        idx = self._save_with_files(tmp_path, n_present=5, n_missing=0)
        removed, kept = prune_index(idx)
        assert removed == 0
        assert kept == 5

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
        prune_index(idx)
        out = capsys.readouterr().out
        assert "Phase: Pruning" in out
        assert "checking 3 indexed files" in out

    def test_safety_threshold_blocks_mass_prune(self, tmp_path: Path) -> None:
        from autodj.indexer import PruneSafetyError, prune_index

        idx = self._save_with_files(tmp_path, n_present=2, n_missing=10)
        with pytest.raises(PruneSafetyError):
            prune_index(idx)

    def test_force_bypasses_safety(self, tmp_path: Path) -> None:
        from autodj.indexer import prune_index

        idx = self._save_with_files(tmp_path, n_present=2, n_missing=10)
        removed, kept = prune_index(idx, allow_mass_prune=True)
        assert removed == 10
        assert kept == 2

    def test_all_missing_deletes_index_files(self, tmp_path: Path) -> None:
        from autodj.indexer import prune_index

        idx = self._save_with_files(tmp_path, n_present=0, n_missing=3)
        removed, kept = prune_index(idx, allow_mass_prune=True)
        assert removed == 3
        assert kept == 0
        assert not (idx / "vectors.index").exists()
        assert not (idx / "metadata.json").exists()


# ---------------------------------------------------------------------------
# enrich_from_beets
# ---------------------------------------------------------------------------


class TestEnrichFromBeets:
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
        save_index(entries, vectors, idx)
        # Set up beets DB with a key
        beets = tmp_path / "library.db"
        self._make_beets(beets, [{"path": str(path), "initial_key": "Am"}])

        updated, total = enrich_from_beets(idx, music_dir=None, beets_db=beets)
        assert updated == 1
        assert total == 1
        # Reload and verify
        from autodj.indexer import load_index

        loaded, _ = load_index(idx)
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
        save_index(entries, vectors, idx)
        beets = tmp_path / "library.db"
        self._make_beets(beets, [{"path": str(path), "initial_key": "Am"}])

        updated, total = enrich_from_beets(idx, music_dir=None, beets_db=beets)
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
        save_index(entries, vectors, idx)
        updated, total = enrich_from_beets(idx, music_dir=None, beets_db=tmp_path / "missing.db")
        assert updated == 0
        assert total == 1


# ---------------------------------------------------------------------------
# Index entry path roundtrips with music_dir
# ---------------------------------------------------------------------------


class TestFlatIndexMigration:
    """Auto-migration of pre-0.9 flat-layout indexes into <dir>/<name>/."""

    def _make_flat(self, parent: Path) -> tuple[Path, Path]:
        """Build a fake flat-layout index at *parent* — files directly inside."""
        from autodj.indexer import save_index

        e = [
            IndexEntry(
                path=str(parent / "song.flac"),
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
        v = np.random.randn(1, FEATURE_DIM).astype(np.float32)
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        save_index(e, v, parent)
        return parent / "tracks.db", parent / "vectors.index"

    def test_load_index_auto_migrates(self, tmp_path: Path) -> None:
        from autodj.indexer import load_index

        # Set up the OLD layout: files directly at index_dir
        meta_old, vec_old = self._make_flat(tmp_path)
        target = tmp_path / "default"
        # Sidecars next to old files should also move
        (tmp_path / "web_state.json").write_text('{"k":"v"}', encoding="utf-8")

        # Loading the NEW location should auto-migrate then succeed
        entries, _faiss = load_index(target)

        assert len(entries) == 1
        assert (target / "tracks.db").exists()
        assert (target / "vectors.index").exists()
        # Sidecar moved
        assert (target / "web_state.json").exists()
        # Old files gone
        assert not meta_old.exists()
        assert not vec_old.exists()

    def test_no_migration_when_already_in_place(self, tmp_path: Path) -> None:
        from autodj.indexer import _migrate_flat_index_if_needed, save_index

        target = tmp_path / "default"
        target.mkdir()
        e = [
            IndexEntry(
                path="x.flac",
                title="T",
                artist="A",
                album="L",
                genre="G",
                bpm=100,
                year=2020,
                length=180,
                energy=0.05,
                key=0,
                mode=1,
                tempo_confidence=0.5,
            )
        ]
        v = np.random.randn(1, FEATURE_DIM).astype(np.float32)
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        save_index(e, v, target)
        # No-op when target is already populated
        _migrate_flat_index_if_needed(target)
        assert (target / "tracks.db").exists()

    def test_no_migration_when_no_source(self, tmp_path: Path) -> None:
        from autodj.indexer import _migrate_flat_index_if_needed

        target = tmp_path / "default"
        # Neither source nor target — silent no-op
        _migrate_flat_index_if_needed(target)
        assert not target.exists()


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
        import os

        from autodj.indexer import _detect_stale_entries

        f = tmp_path / "song.flac"
        f.write_bytes(b"original")
        original_mtime = f.stat().st_mtime
        # Embedded an hour ago, then file replaced now
        e = self._entry(str(f), embedded_at=original_mtime - 3600)
        os.utime(f, (original_mtime, original_mtime))
        stale, migrated = _detect_stale_entries([e])
        assert e.path in stale
        assert migrated == 0

    def test_legacy_entry_snapshots_mtime(self, tmp_path: Path) -> None:
        from autodj.indexer import _detect_stale_entries

        f = tmp_path / "song.flac"
        f.write_bytes(b"x")
        mt = f.stat().st_mtime
        e = self._entry(str(f), embedded_at=0.0)
        stale, migrated = _detect_stale_entries([e])
        assert e.path not in stale
        assert migrated == 1
        assert e.embedded_at == pytest.approx(mt)

    def test_unchanged_file_not_stale(self, tmp_path: Path) -> None:
        from autodj.indexer import _detect_stale_entries

        f = tmp_path / "song.flac"
        f.write_bytes(b"x")
        # Embedded just after file creation
        e = self._entry(str(f), embedded_at=f.stat().st_mtime + 60)
        stale, _ = _detect_stale_entries([e])
        assert e.path not in stale

    def test_missing_file_skipped(self, tmp_path: Path) -> None:
        # prune handles missing files; stale detection ignores them
        from autodj.indexer import _detect_stale_entries

        e = self._entry(str(tmp_path / "gone.flac"), embedded_at=1.0)
        stale, migrated = _detect_stale_entries([e])
        assert stale == set()
        assert migrated == 0

    def test_reindex_modified_since_overrides_legacy(self, tmp_path: Path) -> None:
        # The one-shot --reindex-modified-since flag should still flag
        # legacy entries (embedded_at == 0) when their file mtime is newer
        # than the cutoff.
        import os

        from autodj.indexer import _detect_stale_entries

        f = tmp_path / "replaced.flac"
        f.write_bytes(b"x")
        mt = f.stat().st_mtime
        os.utime(f, (mt, mt))
        e = self._entry(str(f), embedded_at=0.0)
        # Cutoff one hour BEFORE mtime → file is newer, must be flagged
        stale, _ = _detect_stale_entries([e], reindex_modified_since=mt - 3600)
        assert e.path in stale

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


class TestRelativizeForStorage:
    def test_strips_music_dir_prefix(self, tmp_path: Path) -> None:
        from autodj.indexer import _relativize_for_storage

        md = tmp_path / "Music"
        md.mkdir()
        assert _relativize_for_storage(str(md / "Artist" / "song.flac"), md) == "Artist/song.flac"

    def test_returns_posix_absolute_when_outside_music_dir(self, tmp_path: Path) -> None:
        from autodj.indexer import _relativize_for_storage

        md = tmp_path / "Music"
        md.mkdir()
        outside = tmp_path / "elsewhere" / "song.flac"
        result = _relativize_for_storage(str(outside), md)
        assert result == outside.as_posix()

    def test_does_not_stat_filesystem(self, tmp_path: Path) -> None:
        # Resolve() / is_relative_to() on real Paths used to dominate save_index
        # for libraries on NFS — see indexer.py _relativize_for_storage docstring.
        # Must work on paths that don't exist.
        from autodj.indexer import _relativize_for_storage

        md = tmp_path / "Music"  # never created
        fake = md / "Artist" / "song.flac"
        assert _relativize_for_storage(str(fake), md) == "Artist/song.flac"

    def test_no_music_dir_returns_posix(self) -> None:
        from autodj.indexer import _relativize_for_storage

        assert _relativize_for_storage("/abs/path/song.flac", None) == "/abs/path/song.flac"


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
                "SELECT path FROM tracks ORDER BY id ASC LIMIT 1"
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
        cache.get = lambda _p: DjMeta(analysed=True)
        cache.set = lambda *_a, **_kw: None
        cache.flush = lambda *_a, **_kw: None
        with patch("autodj.dj_meta.get_cache", return_value=cache):
            _backfill_dj_meta([_entry("a.flac")], tmp_path, workers=1)
        out = capsys.readouterr().out
        assert "already covers" in out

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

    def test_workers_default_none_uses_cpu_count(self, tmp_path: Path) -> None:
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
# Legacy metadata.json -> tracks.db migration
# ---------------------------------------------------------------------------


class TestLegacyMetadataMigration:
    """Auto-migration of pre-v0.10 ``metadata.json`` sidecars into SQLite."""

    @staticmethod
    def _legacy_payload(path: str = "Artist/song.flac") -> list[dict]:
        return [
            {
                "path": path,
                "title": "Title",
                "artist": "Artist",
                "album": "Album",
                "genre": "Rock",
                "bpm": 128.0,
                "year": 2020,
                "length": 200.0,
                "energy": 0.07,
                "key": 5,
                "mode": 1,
                "tempo_confidence": 0.42,
                "embedded_at": 1_700_000_000.0,
            },
        ]

    def test_metadata_json_imported_into_tracks_db(self, tmp_path: Path) -> None:
        from autodj.indexer import (
            _load_tracks_rows,
            _maybe_import_legacy_metadata_json,
            _open_tracks_db,
        )

        legacy = tmp_path / "metadata.json"
        legacy.write_text(json.dumps(self._legacy_payload()), encoding="utf-8")

        _maybe_import_legacy_metadata_json(tmp_path)

        # JSON file is deleted now that SQLite is authoritative.
        assert not legacy.exists()
        assert (tmp_path / "tracks.db").exists()

        conn = _open_tracks_db(tmp_path)
        try:
            rows = _load_tracks_rows(conn)
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0].title == "Title"
        assert rows[0].bpm == 128.0
        assert rows[0].embedded_at == 1_700_000_000.0

    def test_migration_idempotent(self, tmp_path: Path) -> None:
        from autodj.indexer import _maybe_import_legacy_metadata_json

        legacy = tmp_path / "metadata.json"
        legacy.write_text(json.dumps(self._legacy_payload()), encoding="utf-8")
        _maybe_import_legacy_metadata_json(tmp_path)
        # Re-run is a no-op (JSON file is already gone).
        _maybe_import_legacy_metadata_json(tmp_path)
        assert (tmp_path / "tracks.db").exists()

    def test_corrupt_metadata_json_kept_for_inspection(self, tmp_path: Path) -> None:
        from autodj.indexer import _maybe_import_legacy_metadata_json

        legacy = tmp_path / "metadata.json"
        legacy.write_text("{{not json", encoding="utf-8")

        _maybe_import_legacy_metadata_json(tmp_path)

        # Corrupt JSON should NOT be deleted — user needs to inspect it.
        assert legacy.exists()
        # No tracks.db was created either (nothing successfully migrated).
        # Note: _open_tracks_db isn't called in the unreadable-JSON branch.
        assert not (tmp_path / "tracks.db").exists()

    def test_skipped_when_tracks_db_already_exists(self, tmp_path: Path) -> None:
        from autodj.indexer import (
            _maybe_import_legacy_metadata_json,
            _open_tracks_db,
        )

        # Pre-create an empty tracks.db.
        conn = _open_tracks_db(tmp_path)
        conn.close()
        legacy = tmp_path / "metadata.json"
        legacy.write_text(json.dumps(self._legacy_payload()), encoding="utf-8")

        _maybe_import_legacy_metadata_json(tmp_path)
        # JSON untouched: db already exists so we never migrate over it.
        assert legacy.exists()

    def test_non_list_json_skipped(self, tmp_path: Path) -> None:
        from autodj.indexer import _maybe_import_legacy_metadata_json

        legacy = tmp_path / "metadata.json"
        # A dict at the top level is the wrong shape for the legacy format.
        legacy.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
        _maybe_import_legacy_metadata_json(tmp_path)
        # Source kept for inspection, no db created.
        assert legacy.exists()
        assert not (tmp_path / "tracks.db").exists()

    def test_all_rows_malformed_keeps_json(self, tmp_path: Path) -> None:
        from autodj.indexer import _maybe_import_legacy_metadata_json

        legacy = tmp_path / "metadata.json"
        # Three entries that all fail the dict + path checks.
        legacy.write_text(
            json.dumps(["a string", 42, {"no_path_field": 1}]),
            encoding="utf-8",
        )
        _maybe_import_legacy_metadata_json(tmp_path)
        # No row imported, JSON kept around for inspection.
        assert legacy.exists()

    def test_empty_list_json_deletes_legacy(self, tmp_path: Path) -> None:
        from autodj.indexer import _maybe_import_legacy_metadata_json

        legacy = tmp_path / "metadata.json"
        legacy.write_text("[]", encoding="utf-8")
        _maybe_import_legacy_metadata_json(tmp_path)
        # Empty legacy successfully "migrated" -> deleted (no rows to keep).
        assert not legacy.exists()
        assert (tmp_path / "tracks.db").exists()
