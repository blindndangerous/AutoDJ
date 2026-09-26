"""The renderer returns stereo audio and carries the next track's offset."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj import player as player_mod
from autodj.mixbus import RenderedTrack


def _entry(name: str, length: float = 10.0) -> MagicMock:
    entry = MagicMock()
    entry.path = Path(f"/music/{name}.flac")
    entry.length = length
    entry.bpm = 120.0
    return entry


@pytest.fixture
def renderer(monkeypatch: pytest.MonkeyPatch) -> player_mod.Player:
    audio = {
        "a": np.full((44100 * 10, 2), 0.1, np.float32),
        "b": np.full((44100 * 10, 2), 0.2, np.float32),
        "short": np.full((100, 2), 0.3, np.float32),
        "empty": np.zeros((0, 2), np.float32),
    }
    monkeypatch.setattr(
        player_mod, "load_stereo", lambda path, target_sr=44100: audio[Path(path).stem]
    )
    p = player_mod.Player.__new__(player_mod.Player)
    p._cfg = MagicMock()
    p._cfg.playback.crossfade_seconds = 2.0
    p._apply_replaygain = lambda audio_in, _path: audio_in
    p._load_lyrics = lambda _path: None
    p._ensure_dj_cache = lambda: None
    p._outgoing_meta = lambda *_a: None
    p._peek_incoming_meta = lambda _e: None
    p._effective_crossfade_seconds = lambda *_a: 2.0
    p._crossfade_start_in_a = lambda audio_a, _sr, _meta, cf: len(audio_a) - cf
    p._maybe_beatmatch = lambda audio_b, *_a: audio_b
    p._skip_incoming_intro_samples = lambda _audio_b, _sr, _e: 0
    p._apply_outgoing_filter_sweep = lambda a, *_a: a
    p._apply_transition_effect = lambda a, _b, head, *_a: (a, head, np.zeros((0, 2), np.float32))
    p._mix_overlap = lambda a, head, cf, _sr, _extra: player_mod._apply_crossfade(
        a, np.concatenate([head, head[:0]]), cf
    )
    p._last_transition_fx = ""
    p._beatmatch_ratio = 1.0
    return p


def test_render_returns_stereo_and_offset(renderer: player_mod.Player) -> None:
    out = renderer._render_track(_entry("a"), _entry("b"), start_offset=0)
    assert isinstance(out, RenderedTrack)
    assert out.audio.ndim == 2 and out.audio.shape[1] == 2
    assert out.next_start_offset == 2 * 44100
    assert len(out.audio) == 10 * 44100


def test_render_skips_already_played_head(renderer: player_mod.Player) -> None:
    out = renderer._render_track(_entry("b"), None, start_offset=2 * 44100)
    assert out is not None
    assert len(out.audio) == 8 * 44100
    assert out.next_start_offset == 0


def test_render_returns_none_for_unloadable_or_empty(
    renderer: player_mod.Player, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert renderer._render_track(_entry("empty"), None, start_offset=0) is None

    def _boom(path: str, target_sr: int = 44100) -> np.ndarray:
        raise OSError("bad file")

    monkeypatch.setattr(player_mod, "load_stereo", _boom)
    assert renderer._render_track(_entry("a"), None, start_offset=0) is None


def test_track_shorter_than_crossfade_is_cut_not_stalled(renderer: player_mod.Player) -> None:
    out = renderer._render_track(_entry("short"), _entry("b"), start_offset=0)
    assert out is not None
    assert len(out.audio) == 100
    assert out.next_start_offset == 0


def test_offset_beyond_track_returns_none(renderer: player_mod.Player) -> None:
    assert renderer._render_track(_entry("short"), None, start_offset=500) is None


def test_offset_scaled_by_beatmatch_ratio(renderer: player_mod.Player) -> None:
    def _stretch(audio_b, *_a):
        return np.repeat(audio_b, 2, axis=0)[: int(len(audio_b) * 1.25)]

    renderer._maybe_beatmatch = _stretch
    renderer._beatmatch_ratio = 1.25
    out = renderer._render_track(_entry("a"), _entry("b"), start_offset=0)
    assert out is not None
    assert out.next_start_offset == int((2 * 44100) / 1.25)


def test_crossfade_trimmed_to_fit_track_end(renderer: player_mod.Player) -> None:
    """When the crossfade start lands near the outgoing track's end, the
    overlap window is shrunk to fit rather than reading past the buffer."""
    renderer._crossfade_start_in_a = lambda audio_a, _sr, _meta, _cf: len(audio_a) - 100
    out = renderer._render_track(_entry("a"), _entry("b"), start_offset=0)
    assert out is not None
    assert len(out.audio) == 10 * 44100
    assert out.next_start_offset == 100


def test_incoming_too_short_after_intro_skip_falls_back_to_no_overlap(
    renderer: player_mod.Player,
) -> None:
    """When skipping the incoming track's intro leaves less than one
    crossfade window, the render falls back to a plain cut."""
    renderer._skip_incoming_intro_samples = lambda *_a: 44100 * 10 - 1000
    out = renderer._render_track(_entry("a"), _entry("b"), start_offset=0)
    assert out is not None
    assert out.transition_fx == ""
    assert out.next_start_offset == 44100 * 10 - 1000
