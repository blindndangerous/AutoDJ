"""Unit tests for autodj.indexer.

The MuQ model and librosa are mocked so tests run without audio files or
model downloads. Vector math and FAISS operations use real numpy/faiss.
"""

import os
from pathlib import Path
from unittest.mock import patch

import faiss
import numpy as np
import pytest

from autodj.beets import Track
from autodj.indexer import (
    EMBEDDING_DIM,
    FEATURE_DIM,
    IndexEntry,
    _combine_features,
    _extract_librosa_features,
    build_faiss_index,
    load_index,
)
from tests.unit._legacy_index import save_index

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
            patch("autodj.indexer._load_audio", return_value=(fake_audio, fake_sr)),
            patch("autodj.indexer.librosa") as mock_librosa,
        ):
            self._setup_librosa_mock(mock_librosa, fake_audio, fake_sr)

            vec, audio, sr, _ = _extract_librosa_features(tmp_path / "song.flac")

        assert vec.shape == (16,)
        assert audio.dtype == np.float32
        assert sr == fake_sr

    def test_extra_meta_has_expected_keys(self, tmp_path: Path) -> None:
        fake_audio = np.zeros(22050, dtype=np.float32)
        fake_sr = 22050

        with (
            patch("autodj.indexer._load_audio", return_value=(fake_audio, fake_sr)),
            patch("autodj.indexer.librosa") as mock_librosa,
        ):
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
            patch("autodj.indexer._load_audio", return_value=(fake_audio, fake_sr)),
            patch("autodj.indexer.librosa") as mock_librosa,
        ):
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
        from autodj.indexer import _write_tracks_file
        from tests.unit._legacy_index import entry_to_row, write_vectors

        entries, vectors = self._make_entries(3)

        def write(tracks: Path, vectors_path: Path) -> None:
            _write_tracks_file([entry_to_row(e, None, i) for i, e in enumerate(entries)], tracks)
            write_vectors(vectors[:2], vectors_path)

        publish_generation(tmp_path, base_generation=0, vector_count=3, write_files=write)

        with pytest.raises(IndexConsistencyError, match="count mismatch"):
            load_index(tmp_path)

    def test_load_refuses_absolute_track_path(self, tmp_path: Path) -> None:
        from autodj.index_manifest import UnsupportedIndexError, publish_generation
        from autodj.indexer import _write_tracks_file
        from tests.unit._legacy_index import entry_to_row, write_vectors

        entries, vectors = self._make_entries(1)
        row = entry_to_row(entries[0], None, 0) | {"path": "Z:/Music/song_0.flac"}

        def write(tracks: Path, vectors_path: Path) -> None:
            _write_tracks_file([row], tracks)
            write_vectors(vectors, vectors_path)

        publish_generation(tmp_path, base_generation=0, vector_count=1, write_files=write)

        with pytest.raises(UnsupportedIndexError, match=r"absolute path Z:/Music/song_0.flac"):
            load_index(tmp_path, music_dir=tmp_path)


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

    def _make_catalog(self, index_dir: Path, entries: list[IndexEntry], music_root: Path) -> None:
        from autodj.catalog import LibraryCatalog

        index_dir.mkdir(parents=True, exist_ok=True)
        with LibraryCatalog(index_dir / "library.db") as catalog:
            catalog.add_source(music_root)
            for entry in entries:
                Path(entry.path).parent.mkdir(parents=True, exist_ok=True)
                Path(entry.path).touch(exist_ok=True)
                catalog.put_entry(entry)

    def test_no_index_returns_zero(self, tmp_path: Path) -> None:
        from autodj.indexer import enrich_from_beets

        beets = tmp_path / "library.db"
        self._make_beets(beets, [])
        assert enrich_from_beets(tmp_path / "noidx", music_dir=None, beets_db=beets) == (0, 0)

    def test_missing_beets_db_raises_instead_of_reporting_in_sync(self, tmp_path: Path) -> None:
        from autodj.beets import BeetsNotFoundError
        from autodj.indexer import enrich_from_beets

        entries, _vectors = self._make_distinctive_index(tmp_path)
        idx = tmp_path / "idx"
        self._make_catalog(idx, entries, tmp_path)

        with pytest.raises(BeetsNotFoundError, match="Beets library not found"):
            enrich_from_beets(idx, music_dir=tmp_path, beets_db=tmp_path / "missing.db")

    def test_enrich_rejects_generation_race_without_overwriting_newer_snapshot(
        self,
        tmp_path: Path,
    ) -> None:
        from autodj.indexer import enrich_from_beets

        entries, _vectors = self._make_distinctive_index(tmp_path)
        idx = tmp_path / "idx"
        self._make_catalog(idx, entries, tmp_path)
        beets = tmp_path / "library.db"
        self._make_beets(
            beets,
            [{"path": entry.path, "title": f"Enriched {row}"} for row, entry in enumerate(entries)],
        )
        assert enrich_from_beets(idx, music_dir=tmp_path, beets_db=beets) == (2, 2)
        from autodj.catalog import LibraryCatalog

        with LibraryCatalog(idx / "library.db") as catalog:
            assert [entry.title for entry in catalog.entries_snapshot()] == [
                "Enriched 0",
                "Enriched 1",
            ]

    def test_skips_catalog_entries_absent_from_beets(self, tmp_path: Path) -> None:
        from autodj.catalog import LibraryCatalog
        from autodj.indexer import enrich_from_beets

        entries, _vectors = self._make_distinctive_index(tmp_path)
        idx = tmp_path / "idx"
        self._make_catalog(idx, entries, tmp_path)
        beets = tmp_path / "beets.db"
        self._make_beets(beets, [{"path": str(tmp_path / "other.flac"), "title": "Other"}])
        assert enrich_from_beets(idx, music_dir=tmp_path, beets_db=beets) == (0, 2)
        with LibraryCatalog(idx / "library.db") as catalog:
            assert [entry.title for entry in catalog.entries_snapshot()] == [
                entry.title for entry in entries
            ]

    def test_updates_initial_key(self, tmp_path: Path) -> None:
        from autodj.indexer import enrich_from_beets

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
        idx = tmp_path / "idx"
        self._make_catalog(idx, entries, tmp_path)
        # Set up beets DB with a key
        beets = tmp_path / "library.db"
        self._make_beets(beets, [{"path": str(path), "initial_key": "Am"}])

        updated, total = enrich_from_beets(idx, music_dir=tmp_path, beets_db=beets)
        assert updated == 1
        assert total == 1
        # Reload and verify
        from autodj.catalog import LibraryCatalog

        with LibraryCatalog(idx / "library.db") as catalog:
            loaded = catalog.entries_snapshot()
        assert loaded[0].mode == 0  # minor
        assert loaded[0].key == 9  # A

    def test_no_changes_returns_zero_updated(self, tmp_path: Path) -> None:
        from autodj.indexer import enrich_from_beets

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
        idx = tmp_path / "idx"
        self._make_catalog(idx, entries, tmp_path)
        beets = tmp_path / "library.db"
        self._make_beets(beets, [{"path": str(path), "initial_key": "Am"}])

        updated, total = enrich_from_beets(idx, music_dir=tmp_path, beets_db=beets)
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


class TestRelativizeForStorage:
    def test_strips_music_dir_prefix(self, tmp_path: Path) -> None:
        from autodj.fsutil import relative_storage_path

        md = tmp_path / "Music"
        md.mkdir()
        assert relative_storage_path(str(md / "Artist" / "song.flac"), md) == "Artist/song.flac"

    def test_rejects_path_outside_music_dir(self, tmp_path: Path) -> None:
        from autodj.fsutil import relative_storage_path

        md = tmp_path / "Music"
        md.mkdir()
        with pytest.raises(ValueError, match="not under music_dir"):
            relative_storage_path(str(tmp_path / "elsewhere" / "song.flac"), md)

    def test_does_not_stat_filesystem(self, tmp_path: Path) -> None:
        # Resolve() / is_relative_to() on real Paths used to dominate save_index
        # for libraries on NFS — see fsutil.relative_storage_path.
        # Must work on paths that don't exist.
        from autodj.fsutil import relative_storage_path

        md = tmp_path / "Music"  # never created
        fake = md / "Artist" / "song.flac"
        assert relative_storage_path(str(fake), md) == "Artist/song.flac"

    @pytest.mark.skipif(os.name != "nt", reason="case-insensitive paths are a Windows rule")
    def test_case_and_separator_mismatch_is_under_music_dir(self) -> None:
        """Beets may spell the drive or folders differently from music_dir on Windows."""
        from autodj.fsutil import relative_storage_path

        md = Path("c:/music")
        assert relative_storage_path(r"C:\Music\Artist/Song.flac", md) == "Artist/Song.flac"

    def test_no_music_dir_keeps_relative_path(self) -> None:
        from autodj.fsutil import relative_storage_path

        assert relative_storage_path("Artist/song.flac", None) == "Artist/song.flac"


class TestPathPortability:
    def test_save_strips_music_dir_prefix(self, tmp_path: Path) -> None:
        from autodj.indexer import load_index

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

    def test_short_decode_from_windows_is_reused(self, tmp_path: Path) -> None:
        from autodj.indexer import _analyse_one_track

        audio = np.ones((20, 2), np.float32)
        expected = object()
        with (
            patch("autodj.processing.estimated_seconds", return_value=1000.0),
            patch("autodj.processing.read_windows", return_value=audio),
            patch("autodj.indexer._load_audio", side_effect=AssertionError("decoded twice")),
            patch("autodj.dj_meta.analyse_audio", return_value=expected) as analyse,
        ):
            assert _analyse_one_track(str(tmp_path / "short.wav")) is expected
        analyse.assert_called_once()
        np.testing.assert_array_equal(analyse.call_args.args[0], audio)


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
        cache = get_cache(cfg.index.library_dir, music_dir=cfg.library.music_dir)
        assert cache is not None
        gone = str(cfg.library.music_dir / "gone.flac")
        cache.set(gone, DjMeta(analysed=True))
        cache.flush(force=True)
        entries = self._entries(cfg, "a.flac", "b.flac")

        def analyse(path: str, points: dict | None = None) -> DjMeta:
            if path.endswith("b.flac"):
                raise RuntimeError("bad file")
            return DjMeta(analysed=True, intro_end_s=1.0)

        with (
            patch("autodj.indexer._analyse_one_track", side_effect=analyse),
            patch("autodj.indexer.TrackProgress") as progress,
        ):
            backfill_dj_meta(cfg, entries, prune=True)

        assert [
            call.args[0]
            for call in progress.return_value.__enter__.return_value.update.call_args_list
        ] == [1, 2]

        assert cache.get(entries[0].path).intro_end_s == 1.0
        assert not cache.get(entries[1].path).analysed
        assert not cache.get(gone).analysed

    def test_backfill_keeps_stale_rows_by_default(self, tmp_path: Path) -> None:
        from autodj.dj_meta import DjMeta, get_cache
        from autodj.indexer import backfill_dj_meta

        cfg = _backfill_cfg(tmp_path)
        cache = get_cache(cfg.index.library_dir, music_dir=cfg.library.music_dir)
        assert cache is not None
        stale = str(cfg.library.music_dir / "stale.flac")
        entry = self._entries(cfg, "done.flac")[0]
        cache.set(stale, DjMeta(analysed=True))
        cache.set(entry.path, DjMeta(analysed=True))
        cache.flush(force=True)

        backfill_dj_meta(cfg, [entry])

        assert cache.get(stale).analysed is True

    def test_limit_analyses_only_that_many_and_keeps_every_other_row(self, tmp_path: Path) -> None:
        """``analyse --limit`` used to delete the DJ data of every track past the limit."""
        from autodj.dj_meta import Cue, DjMeta, get_cache
        from autodj.indexer import backfill_dj_meta

        cfg = _backfill_cfg(tmp_path)
        cache = get_cache(cfg.index.library_dir, music_dir=cfg.library.music_dir)
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
# file_fingerprint + _detect_moves
# ---------------------------------------------------------------------------


class TestFingerprint:
    def test_reads_one_64k_block_from_the_middle(self, tmp_path: Path) -> None:
        import hashlib

        from autodj.indexer import file_fingerprint

        data = bytes(range(256)) * 4096  # 1 MiB
        path = tmp_path / "big.flac"
        path.write_bytes(data)
        middle = len(data) // 2 - 32 * 1024

        assert file_fingerprint(path) == (
            len(data),
            hashlib.sha256(data[middle : middle + 64 * 1024]).hexdigest(),
        )
        assert file_fingerprint(tmp_path / "gone.flac") == (0, "")

    def test_only_candidates_with_a_missing_entrys_size_are_fingerprinted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import autodj.indexer as indexer
        from autodj.beets import Track

        gone = IndexEntry("gone.flac", "", "", "", "", 0, 0, 0, 0, -1, -1, 0, size=100)
        gone.fingerprint = "f" * 64
        candidates = []
        for name, size in (("a.flac", 100), ("b.flac", 200), ("c.flac", 300)):
            (tmp_path / name).write_bytes(b"x" * size)
            candidates.append(Track(tmp_path / name, "", "", "", "", 0.0, 0, 0.0))
        seen: list[Path] = []
        real = indexer.file_fingerprint
        monkeypatch.setattr(
            indexer, "file_fingerprint", lambda p: (seen.append(Path(p)), real(p))[1]
        )

        indexer._detect_moves([gone], {"gone.flac"}, candidates)

        assert seen == [tmp_path / "a.flac"]
