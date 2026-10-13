"""Behavior tests for defensive and recovery paths in ``autodj.indexer``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import faiss
import numpy as np
import pytest

import autodj.indexer as indexer
import autodj.stereo as stereo_mod


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


def test_display_name_falls_back_to_title_without_artist() -> None:
    assert _entry("song.flac", artist="").display_name == "Song"


def test_load_audio_downmixes_stereo_soundfile_result(tmp_path: Path) -> None:
    import soundfile as sf

    path = tmp_path / "song.flac"
    sf.write(path, np.array([[0.25, 0.75], [-0.5, 0.0]], dtype=np.float32), 48_000)

    audio, sample_rate = indexer._load_audio(path)

    assert sample_rate == 48_000
    np.testing.assert_allclose(audio, [0.5, -0.25], atol=1e-4)


def test_load_audio_decodes_m4a_with_ffmpeg_and_averages_channels() -> None:
    stereo = np.array([[1.0, 3.0], [5.0, 7.0]], dtype=np.float32)
    completed = MagicMock(returncode=0, stderr=b"")

    with (
        patch.object(stereo_mod.shutil, "which", return_value="/usr/bin/ffmpeg"),
        patch.object(stereo_mod.subprocess, "run", return_value=completed) as run,
        patch.object(stereo_mod.sf, "read", return_value=(stereo, 44_100)) as read,
    ):
        audio, sample_rate = indexer._load_audio(Path("song.m4a"))

    command = run.call_args.args[0]
    assert command[0] == "/usr/bin/ffmpeg"
    assert command[command.index("-i") + 1] == "song.m4a"
    # FFmpeg's own mono downmix is louder than the channel mean playback hears.
    assert command[command.index("-ac") + 1] == "2"
    assert command[-2:] == ["wav", "pipe:1"]
    assert read.call_args.kwargs == {"dtype": "float32", "always_2d": True}
    assert sample_rate == 44_100
    assert np.array_equal(audio, np.array([2.0, 6.0], dtype=np.float32))


def test_load_audio_reports_missing_ffmpeg() -> None:
    with (
        patch.object(stereo_mod.shutil, "which", return_value=None),
        pytest.raises(RuntimeError, match=r"FFmpeg is required to decode \.m4a"),
    ):
        indexer._load_audio(Path("song.m4a"))


@pytest.mark.parametrize(
    ("stderr", "message"),
    [(b"invalid data", "invalid data"), (b"", "unknown error")],
)
def test_load_audio_reports_decoder_failure(stderr: bytes, message: str) -> None:
    completed = MagicMock(returncode=1, stderr=stderr)

    with (
        patch.object(stereo_mod.shutil, "which", return_value="/usr/bin/ffmpeg"),
        patch.object(stereo_mod.subprocess, "run", return_value=completed),
        pytest.raises(RuntimeError, match=message),
    ):
        indexer._load_audio(Path("song.m4a"))


def test_load_audio_falls_back_to_ffmpeg_after_soundfile_decoder_error() -> None:
    error = stereo_mod.sf.LibsndfileError(0, "lost sync")
    decoded = np.zeros((4, 2), dtype=np.float32)

    with (
        patch.object(stereo_mod.sf, "SoundFile", side_effect=error),
        patch.object(stereo_mod, "load_with_ffmpeg", return_value=(decoded, 48_000)) as ffmpeg,
    ):
        audio, rate = indexer._load_audio(Path("song.flac"))

    assert rate == 48_000
    assert audio.shape == (4,)
    ffmpeg.assert_called_once_with(
        Path("song.flac"), channels=2, sample_rate=None, max_seconds=None
    )


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


def test_chunked_faiss_write_survives_unsupported_fsync(tmp_path: Path) -> None:
    faiss_index = indexer.build_faiss_index(_vector())
    destination = tmp_path / "vectors.index"

    with patch("os.fsync", side_effect=OSError("unsupported")):
        indexer._write_faiss_chunked(faiss_index, destination, chunk_size=7)

    restored = faiss.read_index(str(destination))
    assert restored.ntotal == 1
    assert np.array_equal(restored.reconstruct(0), _vector()[0])
