from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from autodj import processing

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is unavailable")


def _source(path: Path, *, silence: bool = False) -> Path:
    rate = 44_100
    t = np.arange(rate * 3, dtype=np.float32) / rate
    signal = np.zeros_like(t) if silence else 0.12 * np.sin(2 * np.pi * 440 * t)
    sf.write(path, np.column_stack((signal, signal)), rate, subtype="PCM_16")
    return path


def test_prepare_source_caches_stereo_float_and_keeps_source_unchanged(tmp_path: Path) -> None:
    source = _source(tmp_path / "song.wav")
    before = hashlib.sha256(source.read_bytes()).digest()

    prepared = processing.prepare_source(source, tmp_path / "cache")
    again = processing.prepare_source(source, tmp_path / "cache")
    audio, rate = sf.read(prepared, dtype="float32", always_2d=True)

    assert again == prepared
    assert prepared != source
    assert rate == processing.SAMPLE_RATE
    assert audio.shape == (3 * rate, 2)
    assert np.isfinite(audio).all()
    assert hashlib.sha256(source.read_bytes()).digest() == before
    assert processing._measure(prepared) == pytest.approx(-28.0, abs=0.5)


def test_liner_uses_voice_preset_and_has_distinct_cache_entry(tmp_path: Path) -> None:
    source = _source(tmp_path / "liner.wav")
    cache = tmp_path / "cache"

    music = processing.prepare_source(source, cache, "music")
    liner = processing.prepare_source(source, cache, "liner")

    assert music != liner
    assert processing._measure(liner) == pytest.approx(-23.5, abs=0.7)
    with pytest.raises(ValueError, match="kind"):
        processing.prepare_source(source, cache, "jingle")


def test_music_quiet_boost_uses_distinct_reusable_cache_and_leaves_liner_independent(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path / "song.wav")
    cache = tmp_path / "cache"

    music_9 = processing.prepare_source(source, cache, quiet_boost_db=9)
    music_9_again = processing.prepare_source(source, cache, quiet_boost_db=9.0)
    music_0 = processing.prepare_source(source, cache, quiet_boost_db=0)
    liner = processing.prepare_source(source, cache, "liner")

    assert music_9 == music_9_again
    assert music_9 != music_0
    assert processing.prepared_source_matches(music_9, "music", 9)
    assert not processing.prepared_source_matches(music_9, "music", 0)
    assert processing.prepared_source_matches(liner, "liner", 0)
    assert processing.prepared_source_matches(liner, "liner", 12)
    with pytest.raises(ValueError, match="quiet_boost_db"):
        processing.prepare_source(source, cache, quiet_boost_db=12.1)


def test_silent_input_fails_without_publishing_partial_cache(tmp_path: Path) -> None:
    source = _source(tmp_path / "silence.wav", silence=True)
    cache = tmp_path / "cache"

    with pytest.raises(ValueError, match="silent"):
        processing.prepare_source(source, cache)

    assert list(cache.glob("*.wav")) == []
    assert list(cache.glob(".prepare-*")) == []


def test_basic_suggestions_keep_segue_unset_without_sustained_tail_drop(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path / "song.wav")
    entry, points = processing.analyze_basic(source)

    assert entry.length == pytest.approx(3.0)
    assert points["start"] == pytest.approx(0.0, abs=1 / processing.SAMPLE_RATE)
    assert points["end"] == pytest.approx(3.0, abs=1 / processing.SAMPLE_RATE)
    assert points["segue"] is None


def test_liner_segue_uses_only_a_sustained_final_drop() -> None:
    rate = processing.SAMPLE_RATE
    audio = np.full(rate * 6, 0.2, dtype=np.float32)
    audio[rate : rate * 2] = 0.0  # Interior pause followed by more content.
    audio[rate * 5 + rate // 2 :] = 0.0  # Final sustained drop begins at 5.5s.

    assert processing._suggest_liner_segue(audio, rate) == pytest.approx(5.5)

    no_final_drop = np.full(rate * 6, 0.2, dtype=np.float32)
    no_final_drop[rate : rate * 2] = 0.0
    assert processing._suggest_liner_segue(no_final_drop, rate) is None


def test_liner_without_a_final_drop_falls_back_to_end(tmp_path: Path) -> None:
    source = _source(tmp_path / "liner.wav")

    _, music_points = processing.analyze_basic(source)
    _, liner_points = processing.analyze_basic(source, kind="liner")

    assert music_points["segue"] is None
    assert liner_points["segue"] == liner_points["end"]


def test_liner_segue_analysis_handles_silence_and_short_buffers() -> None:
    rate = processing.SAMPLE_RATE

    assert processing._suggest_liner_segue(np.zeros(rate * 4), rate) is None
    assert processing._suggest_liner_segue(np.zeros(rate // 10), rate) is None
