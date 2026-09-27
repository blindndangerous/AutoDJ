"""Behavior tests for defensive and recovery paths in ``autodj.indexer``."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import faiss
import numpy as np
import pytest

import autodj.indexer as indexer
from autodj.index_manifest import IndexConsistencyError, IndexSnapshotToken


def _entry(path: str, *, artist: str = "Artist") -> indexer.IndexEntry:
    return indexer.IndexEntry(
        path=path,
        title="Song",
        artist=artist,
        album="Album",
        genre="Rock",
        bpm=120.0,
        year=2024,
        length=180.0,
        energy=0.5,
        key=0,
        mode=1,
        tempo_confidence=0.8,
    )


def _vector() -> np.ndarray:
    vector = np.zeros((1, indexer.FEATURE_DIM), dtype=np.float32)
    vector[0, 7] = 1.0
    return vector


def _create_legacy_cores(parent: Path, *, vector_in: Path | None = None) -> None:
    entry = _entry(str(parent / "song.flac"))
    connection = indexer._open_tracks_db(parent)
    try:
        connection.execute(
            indexer._TRACKS_INSERT_SQL,
            indexer._entry_to_row(entry, music_dir=None, vec_row=0),
        )
    finally:
        connection.close()
    destination = parent / "vectors.index" if vector_in is None else vector_in
    faiss.write_index(indexer.build_faiss_index(_vector()), str(destination))


def test_display_name_falls_back_to_title_without_artist() -> None:
    assert _entry("song.flac", artist="").display_name == "Song"


def test_load_audio_downmixes_stereo_soundfile_result() -> None:
    stereo = np.array([[1.0, 3.0], [5.0, 7.0]], dtype=np.float32)

    with patch.object(indexer.sf, "read", return_value=(stereo, 48_000)):
        audio, sample_rate = indexer._load_audio(Path("song.flac"))

    assert sample_rate == 48_000
    assert np.array_equal(audio, np.array([2.0, 6.0], dtype=np.float32))


def test_load_audio_decodes_m4a_with_ffmpeg() -> None:
    stereo = np.array([[1.0, 3.0], [5.0, 7.0]], dtype=np.float32)
    completed = MagicMock(returncode=0, stderr=b"")

    with (
        patch.object(indexer.shutil, "which", return_value="/usr/bin/ffmpeg"),
        patch.object(indexer.subprocess, "run", return_value=completed) as run,
        patch.object(indexer.sf, "read", return_value=(stereo, 44_100)) as read,
    ):
        audio, sample_rate = indexer._load_audio(Path("song.m4a"))

    command = run.call_args.args[0]
    assert command[0] == "/usr/bin/ffmpeg"
    assert command[command.index("-i") + 1] == "song.m4a"
    assert command[-2:] == ["wav", "pipe:1"]
    assert read.call_args.kwargs == {"dtype": "float32", "always_2d": False}
    assert sample_rate == 44_100
    assert np.array_equal(audio, np.array([2.0, 6.0], dtype=np.float32))


def test_load_audio_ffmpeg_reports_missing_binary() -> None:
    with (
        patch.object(indexer.shutil, "which", return_value=None),
        pytest.raises(RuntimeError, match=r"FFmpeg is required to decode \.m4a"),
    ):
        indexer._load_audio_ffmpeg(Path("song.m4a"))


@pytest.mark.parametrize(
    ("stderr", "message"),
    [(b"invalid data", "invalid data"), (b"", "unknown error")],
)
def test_load_audio_ffmpeg_reports_decoder_failure(stderr: bytes, message: str) -> None:
    completed = MagicMock(returncode=1, stderr=stderr)

    with (
        patch.object(indexer.shutil, "which", return_value="/usr/bin/ffmpeg"),
        patch.object(indexer.subprocess, "run", return_value=completed),
        pytest.raises(RuntimeError, match=message),
    ):
        indexer._load_audio_ffmpeg(Path("song.m4a"))


def test_load_audio_falls_back_to_ffmpeg_after_librosa_decoder_error() -> None:
    error = indexer.sf.LibsndfileError("lost sync")
    expected = (np.zeros(4, dtype=np.float32), 48_000)

    with (
        patch.object(indexer.sf, "read", side_effect=error),
        patch.object(indexer.librosa, "load", side_effect=error),
        patch.object(indexer, "_load_audio_ffmpeg", return_value=expected) as ffmpeg,
    ):
        result = indexer._load_audio(Path("song.flac"))

    assert result == expected
    ffmpeg.assert_called_once_with(Path("song.flac"))


def test_load_audio_requires_soundfile() -> None:
    with (
        patch.object(indexer, "sf", None),
        pytest.raises(RuntimeError, match="soundfile is required for audio indexing"),
    ):
        indexer._load_audio(Path("song.flac"))

    with (
        patch.object(indexer, "sf", None),
        pytest.raises(RuntimeError, match="soundfile is required to read FFmpeg"),
    ):
        indexer._load_audio_ffmpeg(Path("song.m4a"))


def test_key_estimation_rejects_ambiguous_chroma() -> None:
    ambiguous = np.array(
        [1.00, 0.99, 1.01, 1.00, 0.98, 1.02, 1.00, 0.99, 1.01, 1.00, 0.98, 1.02],
        dtype=np.float32,
    )

    assert indexer._estimate_key_from_chroma(ambiguous) == (-1, -1)


def test_key_estimation_rejects_near_constant_low_magnitude_chroma() -> None:
    near_constant = np.full(12, 1e-7, dtype=np.float32)
    near_constant[0] = 2e-7

    assert indexer._estimate_key_from_chroma(near_constant) == (-1, -1)


def test_key_estimation_rejects_weak_profile_match() -> None:
    weak_match = np.array(
        [
            0.43263078,
            0.6692973,
            0.4227847,
            0.6331844,
            0.96743596,
            0.6830648,
            0.39162484,
            0.18725257,
            0.34596068,
            0.51106596,
            0.8912094,
            0.77556396,
        ],
        dtype=np.float32,
    )

    assert indexer._estimate_key_from_chroma(weak_match) == (-1, -1)


def test_schema_migration_ignores_non_unique_indexes(tmp_path: Path) -> None:
    database = tmp_path / "tracks.db"
    connection = sqlite3.connect(database, isolation_level=None)
    try:
        connection.executescript(
            """
            CREATE TABLE tracks (path TEXT NOT NULL, title TEXT NOT NULL);
            CREATE INDEX tracks_title_idx ON tracks(title);
            INSERT INTO tracks(path, title) VALUES ('song.flac', 'Song');
            """
        )

        indexer._ensure_vec_row_schema(connection)

        assert connection.execute("SELECT vec_row, path, title FROM tracks").fetchall() == [
            (0, "song.flac", "Song")
        ]
    finally:
        connection.close()


def test_chunked_faiss_write_survives_unsupported_fsync(tmp_path: Path) -> None:
    faiss_index = indexer.build_faiss_index(_vector())
    destination = tmp_path / "vectors.index"

    with patch("os.fsync", side_effect=OSError("unsupported")):
        indexer._write_faiss_chunked(faiss_index, destination, chunk_size=7)

    restored = faiss.read_index(str(destination))
    assert restored.ntotal == 1
    assert np.array_equal(restored.reconstruct(0), _vector()[0])


def test_empty_checkpoint_write_leaves_storage_untouched(tmp_path: Path) -> None:
    checkpoint = indexer.IncrementalCheckpoint(
        index_dir=tmp_path,
        music_dir=None,
        existing_entries=[],
        existing_vectors=[],
        total_new=1,
        expected_snapshot=IndexSnapshotToken(0),
    )

    checkpoint.write([], [])

    assert list(tmp_path.iterdir()) == []


def test_failed_first_checkpoint_refreshes_retry_token(tmp_path: Path) -> None:
    checkpoint = indexer.IncrementalCheckpoint(
        index_dir=tmp_path,
        music_dir=None,
        existing_entries=[],
        existing_vectors=[],
        total_new=1,
        expected_snapshot=IndexSnapshotToken(0),
        flush_every=1,
    )
    refreshed = IndexSnapshotToken(0, 1)

    with (
        patch.object(indexer, "require_snapshot_token", return_value=None),
        patch.object(indexer, "_save_vectors"),
        patch.object(indexer, "_upsert_tracks_metadata"),
        patch.object(indexer, "publish_manifest", side_effect=OSError("publish failed")),
        patch.object(indexer, "current_snapshot_token", return_value=refreshed),
        pytest.raises(OSError, match="publish failed"),
    ):
        checkpoint.write([_entry("song.flac")], [_vector()[0]])

    assert checkpoint.expected_snapshot == refreshed
    assert checkpoint.published_new_count == 0


def test_enrich_skips_index_entries_absent_from_beets(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    entry = _entry(str(tmp_path / "indexed.flac"))
    indexer.save_index([entry], _vector(), index_dir)
    beets_db = tmp_path / "beets.db"
    connection = sqlite3.connect(beets_db)
    try:
        connection.execute(
            """CREATE TABLE items (
                id INTEGER PRIMARY KEY, path BLOB, title TEXT, artist TEXT,
                album TEXT, genre TEXT, bpm REAL, year INTEGER, length REAL,
                initial_key TEXT
            )"""
        )
        connection.execute(
            "INSERT INTO items VALUES (1, ?, 'Other', '', '', '', 0, 0, 0, '')",
            (str(tmp_path / "other.flac").encode(),),
        )
        connection.commit()
    finally:
        connection.close()

    assert indexer.enrich_from_beets(index_dir, music_dir=None, beets_db=beets_db) == (0, 1)


def test_prune_applies_requested_stat_throttle(tmp_path: Path) -> None:
    track = tmp_path / "song.flac"
    track.touch()
    index_dir = tmp_path / "index"
    indexer.save_index([_entry(str(track))], _vector(), index_dir)
    sleeps: list[float] = []

    with patch.object(time, "sleep", side_effect=sleeps.append):
        result = indexer.prune_index(index_dir, throttle_ms=5.0, stat_workers=1)

    assert result == (0, 1)
    assert sleeps == [0.005]


def test_load_rejects_wrong_expected_generation(tmp_path: Path) -> None:
    with pytest.raises(IndexConsistencyError, match="expected generation 1, got None"):
        indexer.load_index(tmp_path, expected_generation=1)


def test_load_detects_artifact_changed_during_read(tmp_path: Path) -> None:
    indexer.save_index([_entry("song.flac")], _vector(), tmp_path)
    stable_hash = "a" * 64
    changed_hash = "b" * 64

    with (
        patch.object(
            indexer,
            "sha256_file",
            side_effect=[stable_hash, stable_hash, changed_hash, stable_hash],
        ),
        pytest.raises(IndexConsistencyError, match="artifact changed during load"),
    ):
        indexer.load_index(tmp_path)


def test_load_rejects_faiss_count_mismatch(tmp_path: Path) -> None:
    indexer.save_index([_entry("song.flac")], _vector(), tmp_path)
    empty_index = faiss.IndexFlatIP(indexer.FEATURE_DIM)

    with (
        patch.object(indexer.faiss, "read_index", return_value=empty_index),
        pytest.raises(IndexConsistencyError, match="index count mismatch"),
    ):
        indexer.load_index(tmp_path)


def test_stat_mtimes_throttles_existing_and_missing_files(tmp_path: Path) -> None:
    existing = tmp_path / "existing.flac"
    existing.touch()
    entries = [_entry(str(existing)), _entry(str(tmp_path / "missing.flac"))]
    sleeps: list[float] = []

    with patch.object(time, "sleep", side_effect=sleeps.append):
        mtimes = indexer._stat_mtimes(entries, throttle_ms=5.0, stat_workers=1)

    assert mtimes[0] == existing.stat().st_mtime
    assert mtimes[1] is None
    assert sleeps == [0.005, 0.005]


def test_backfill_flushes_a_durable_checkpoint_after_25_tracks(tmp_path: Path) -> None:
    class Cache:
        def __init__(self) -> None:
            self.stored: list[str] = []
            self.flushes: list[bool] = []

        def get(self, _path: str):
            return MagicMock(analysed=False)

        def set(self, path: str, _meta: object) -> None:
            self.stored.append(path)

        def flush(self, *, force: bool = False) -> None:
            self.flushes.append(force)

    cache = Cache()
    entries = [_entry(f"song-{number}.flac") for number in range(25)]

    with (
        patch("autodj.dj_meta.get_cache", return_value=cache),
        patch.object(
            indexer,
            "_analyse_one_track",
            side_effect=lambda path: (path, object(), None),
        ),
    ):
        indexer._backfill_dj_meta(entries, tmp_path, workers=1)

    assert cache.stored == [entry.path for entry in entries]
    assert cache.flushes == [False, True]
