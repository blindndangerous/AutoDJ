"""Unit tests for autodj.player.

sounddevice and pynput are mocked so tests run without audio hardware.
Audio crossfade math is tested with real numpy arrays.
"""

from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from autodj.indexer import IndexEntry
from autodj.player import (
    PlayerState,
    _apply_crossfade,
    _make_fade_out,
    _make_fade_in,
)

# Note: Player no longer accepts a model wrapper — vectors are looked up
# from the pre-built FAISS index at play time via SimilarityIndex.find_next_for_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(i: int = 0) -> IndexEntry:
    return IndexEntry(
        path=f"Z:/Music/song_{i}.flac",
        title=f"Song {i}",
        artist="Artist",
        album="Album",
        genre="Rock",
        bpm=120.0,
        year=2000,
        length=180.0,
    )


def _sine_audio(seconds: float = 1.0, sr: int = 44100) -> np.ndarray:
    """Generate a simple sine wave as float32 audio."""
    t = np.linspace(0, seconds, int(seconds * sr), dtype=np.float32)
    return np.sin(2 * np.pi * 440 * t)


# ---------------------------------------------------------------------------
# Fade helpers
# ---------------------------------------------------------------------------


class TestFadeHelpers:
    def test_fade_out_starts_at_one(self) -> None:
        fade = _make_fade_out(100)
        assert fade[0] == pytest.approx(1.0, abs=0.01)

    def test_fade_out_ends_at_zero(self) -> None:
        fade = _make_fade_out(100)
        assert fade[-1] == pytest.approx(0.0, abs=0.01)

    def test_fade_in_starts_at_zero(self) -> None:
        fade = _make_fade_in(100)
        assert fade[0] == pytest.approx(0.0, abs=0.01)

    def test_fade_in_ends_at_one(self) -> None:
        fade = _make_fade_in(100)
        assert fade[-1] == pytest.approx(1.0, abs=0.01)

    def test_fade_out_length(self) -> None:
        fade = _make_fade_out(256)
        assert len(fade) == 256

    def test_fade_in_length(self) -> None:
        fade = _make_fade_in(512)
        assert len(fade) == 512

    def test_fade_out_monotonically_decreasing(self) -> None:
        fade = _make_fade_out(100)
        assert all(fade[i] >= fade[i + 1] for i in range(len(fade) - 1))

    def test_fade_in_monotonically_increasing(self) -> None:
        fade = _make_fade_in(100)
        assert all(fade[i] <= fade[i + 1] for i in range(len(fade) - 1))


# ---------------------------------------------------------------------------
# _apply_crossfade
# ---------------------------------------------------------------------------


class TestApplyCrossfade:
    def test_output_length_equals_sum_of_inputs(self) -> None:
        a = _sine_audio(1.0)
        b = _sine_audio(1.0)
        crossfade_samples = 4410  # 0.1 s at 44100 Hz
        result = _apply_crossfade(a, b, crossfade_samples)
        assert len(result) == len(a) + len(b) - crossfade_samples

    def test_output_is_float32(self) -> None:
        a = _sine_audio(0.5)
        b = _sine_audio(0.5)
        result = _apply_crossfade(a, b, 2205)
        assert result.dtype == np.float32

    def test_crossfade_zero_is_concat(self) -> None:
        """With no crossfade the result should equal np.concatenate([a, b])."""
        a = _sine_audio(0.5)
        b = _sine_audio(0.5)
        result = _apply_crossfade(a, b, crossfade_samples=0)
        expected = np.concatenate([a, b])
        np.testing.assert_array_almost_equal(result, expected, decimal=5)

    def test_crossfade_region_is_blended(self) -> None:
        """The overlap region should not be identical to either input alone."""
        a = np.ones(1000, dtype=np.float32)
        b = np.zeros(1000, dtype=np.float32)
        result = _apply_crossfade(a, b, crossfade_samples=500)
        # In the crossfade zone: values should be between 0 and 1
        overlap_start = len(a) - 500
        overlap_end = overlap_start + 500
        region = result[overlap_start:overlap_end]
        assert region.max() <= 1.0 + 1e-5
        assert region.min() >= 0.0 - 1e-5

    def test_raises_if_crossfade_exceeds_audio(self) -> None:
        a = np.ones(100, dtype=np.float32)
        b = np.ones(100, dtype=np.float32)
        with pytest.raises(ValueError):
            _apply_crossfade(a, b, crossfade_samples=200)


# ---------------------------------------------------------------------------
# PlayerState
# ---------------------------------------------------------------------------


class TestPlayerState:
    def test_initial_state(self) -> None:
        state = PlayerState()
        assert state.current_track is None
        assert state.next_track is None
        assert not state.is_paused
        assert not state.should_stop

    def test_add_to_history(self) -> None:
        state = PlayerState(no_repeat_window=3)
        entry = _make_entry(0)
        state.record_played(entry)
        assert entry.path in state.recently_played

    def test_history_bounded_by_window(self) -> None:
        state = PlayerState(no_repeat_window=3)
        for i in range(5):
            state.record_played(_make_entry(i))
        assert len(state.recently_played) == 3

    def test_oldest_evicted_first(self) -> None:
        state = PlayerState(no_repeat_window=2)
        e0 = _make_entry(0)
        e1 = _make_entry(1)
        e2 = _make_entry(2)
        state.record_played(e0)
        state.record_played(e1)
        state.record_played(e2)
        assert e0.path not in state.recently_played
        assert e1.path in state.recently_played
        assert e2.path in state.recently_played
