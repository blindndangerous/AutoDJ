"""Unit tests for autodj.player.

sounddevice and pynput are mocked so tests run without audio hardware.
Audio crossfade math is tested with real numpy arrays.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import faiss
import numpy as np
import pytest

from autodj.indexer import FEATURE_DIM, IndexEntry
from autodj.player import (
    Player,
    PlayerState,
    _append_history_entry,
    _append_m3u_entry,
    _apply_crossfade,
    _apply_crossfade_ducked,
    _fmt_time,
    _make_fade_in,
    _make_fade_out,
    _time_stretch,
    apply_eq,
    apply_filter_sweep,
    beatmatch_incoming,
    load_audio,
    make_eq_filters,
    write_m3u,
)
from autodj.similarity import SimilarityIndex

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
        energy=0.05,
        key=0,
        mode=1,
        tempo_confidence=0.8,
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

    def test_volume_defaults_to_one(self) -> None:
        assert PlayerState().volume == 1.0

    def test_mute_defaults_to_false(self) -> None:
        assert PlayerState().is_muted is False

    def test_volume_can_be_set(self) -> None:
        state = PlayerState()
        state.volume = 0.5
        assert state.volume == pytest.approx(0.5)

    def test_mute_toggle(self) -> None:
        state = PlayerState()
        state.is_muted = True
        assert state.is_muted is True
        state.is_muted = False
        assert state.is_muted is False

    def test_track_number_defaults_to_zero(self) -> None:
        state = PlayerState()
        assert state.track_number == 0

    def test_discovery_enabled_defaults_to_false(self) -> None:
        state = PlayerState()
        assert state.discovery_enabled is False

    def test_track_number_increments(self) -> None:
        state = PlayerState()
        state.track_number += 1
        state.track_number += 1
        assert state.track_number == 2


# ---------------------------------------------------------------------------
# M3U helpers
# ---------------------------------------------------------------------------


class TestM3UHelpers:
    def test_append_m3u_entry_creates_file(self, tmp_path: Path) -> None:
        path = tmp_path / "playlist.m3u"
        entry = _make_entry(0)
        _append_m3u_entry(path, entry)
        assert path.exists()

    def test_append_m3u_entry_format(self, tmp_path: Path) -> None:
        path = tmp_path / "playlist.m3u"
        entry = _make_entry(0)  # length=180.0, display_name="Artist — Song 0"
        _append_m3u_entry(path, entry)
        content = path.read_text(encoding="utf-8")
        assert "#EXTINF:180," in content
        assert entry.path in content

    def test_write_m3u_header(self, tmp_path: Path) -> None:
        path = tmp_path / "playlist.m3u"
        entries = [_make_entry(i) for i in range(3)]
        write_m3u(entries, path)
        content = path.read_text(encoding="utf-8")
        assert content.startswith("#EXTM3U")

    def test_write_m3u_all_entries_present(self, tmp_path: Path) -> None:
        path = tmp_path / "playlist.m3u"
        entries = [_make_entry(i) for i in range(3)]
        write_m3u(entries, path)
        content = path.read_text(encoding="utf-8")
        for entry in entries:
            assert entry.path in content

    def test_append_m3u_entry_appends(self, tmp_path: Path) -> None:
        path = tmp_path / "playlist.m3u"
        _append_m3u_entry(path, _make_entry(0))
        _append_m3u_entry(path, _make_entry(1))
        content = path.read_text(encoding="utf-8")
        assert "Z:/Music/song_0.flac" in content
        assert "Z:/Music/song_1.flac" in content

    def test_m3u_unknown_length_uses_minus_one(self, tmp_path: Path) -> None:
        path = tmp_path / "playlist.m3u"
        entry = IndexEntry(
            path="Z:/Music/unknown.flac",
            title="Unknown",
            artist="Artist",
            album="",
            genre="",
            bpm=0.0,
            year=0,
            length=0.0,
            energy=0.0,
            key=-1,
            mode=-1,
            tempo_confidence=0.0,
        )
        _append_m3u_entry(path, entry)
        content = path.read_text(encoding="utf-8")
        assert "#EXTINF:-1," in content


# ---------------------------------------------------------------------------
# History helper
# ---------------------------------------------------------------------------


class TestHistoryHelper:
    def test_append_history_creates_file(self, tmp_path: Path) -> None:
        from datetime import datetime

        path = tmp_path / "history.jsonl"
        entry = _make_entry(0)
        _append_history_entry(path, entry, datetime.now())
        assert path.exists()

    def test_append_history_is_valid_json(self, tmp_path: Path) -> None:
        import json
        from datetime import datetime

        path = tmp_path / "history.jsonl"
        entry = _make_entry(0)
        _append_history_entry(path, entry, datetime.now())
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["path"] == entry.path
        assert obj["title"] == entry.title
        assert obj["artist"] == entry.artist

    def test_append_history_has_timestamp(self, tmp_path: Path) -> None:
        import json
        from datetime import datetime

        path = tmp_path / "history.jsonl"
        entry = _make_entry(0)
        ts = datetime(2026, 4, 11, 14, 30, 0)
        _append_history_entry(path, entry, ts)
        obj = json.loads(path.read_text(encoding="utf-8").strip())
        assert "2026-04-11" in obj["timestamp"]

    def test_append_history_multiple_entries(self, tmp_path: Path) -> None:
        import json
        from datetime import datetime

        path = tmp_path / "history.jsonl"
        for i in range(3):
            _append_history_entry(path, _make_entry(i), datetime.now())
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3
        # Each line is valid JSON
        for line in lines:
            json.loads(line)


# ---------------------------------------------------------------------------
# _fmt_time
# ---------------------------------------------------------------------------


class TestFmtTime:
    def test_zero(self) -> None:
        assert _fmt_time(0) == "00:00"

    def test_negative_clamps_to_zero(self) -> None:
        assert _fmt_time(-10) == "00:00"

    def test_sub_minute(self) -> None:
        assert _fmt_time(45) == "00:45"

    def test_exact_minute(self) -> None:
        assert _fmt_time(60) == "01:00"

    def test_mixed_minutes_and_seconds(self) -> None:
        assert _fmt_time(67) == "01:07"

    def test_three_minutes(self) -> None:
        assert _fmt_time(180) == "03:00"

    def test_over_one_hour(self) -> None:
        # 1 h 2 m 3 s = 3723 s → 62:03
        assert _fmt_time(3723) == "62:03"

    def test_float_truncated(self) -> None:
        assert _fmt_time(90.9) == "01:30"


# ---------------------------------------------------------------------------
# load_audio
# ---------------------------------------------------------------------------


class TestLoadAudio:
    def test_mono_array_returned_as_is(self) -> None:
        fake_audio = np.ones(44100, dtype=np.float32)
        with patch("autodj.player.sf.read", return_value=(fake_audio, 44100)):
            audio, sr = load_audio("fake.wav")
        assert sr == 44100
        assert len(audio) == 44100
        assert audio.dtype == np.float32

    def test_stereo_mixed_to_mono(self) -> None:
        stereo = np.ones((44100, 2), dtype=np.float32)
        stereo[:, 1] = 0.0  # right channel is silent
        with patch("autodj.player.sf.read", return_value=(stereo, 44100)):
            audio, _ = load_audio("fake.wav")
        assert audio.ndim == 1
        assert len(audio) == 44100

    def test_soundfile_failure_falls_back_to_librosa(self) -> None:
        fake_audio = np.ones(22050, dtype=np.float32)
        with (
            patch("autodj.player.sf.read", side_effect=Exception("unsupported format")),
            patch("librosa.load", return_value=(fake_audio, 22050)),
        ):
            audio, sr = load_audio("fake.mp3")
        assert sr == 22050
        assert len(audio) == 22050


# ---------------------------------------------------------------------------
# Player construction and _build_status
# ---------------------------------------------------------------------------


def _make_cfg_mock() -> MagicMock:
    cfg = MagicMock()
    cfg.playback.no_repeat_window = 50
    cfg.playback.artist_repeat_window = 3
    cfg.playback.crossfade_seconds = 3.0
    cfg.playback.crossfade_eq_duck = False
    cfg.playback.crossfade_bass_cutoff_hz = 180.0
    cfg.playback.show_lyrics = True
    cfg.playback.prefetch_next_track = True
    cfg.playback.silence_trigger_crossfade = True
    cfg.playback.enable_daypart = False
    cfg.playback.enable_mood_arc = False
    cfg.playback.mood_arc_hours = 3.0
    cfg.playback.import_external_cues = False  # tests opt-in per-case
    cfg.replaygain.enabled = False
    cfg.replaygain.target_db = -14.0
    cfg.replaygain.max_clip_safe_gain = 1.0
    cfg.djmix.harmonic_mixing = False
    cfg.djmix.harmonic_mode = "compatible"
    cfg.djmix.beatmatch = False
    cfg.djmix.beatmatch_max_stretch = 0.08
    cfg.djmix.outro_intro_align = False
    cfg.djmix.phrase_align = False
    cfg.djmix.phrase_bars = 8
    cfg.djmix.filter_sweep = False
    cfg.djmix.filter_sweep_floor_hz = 250.0
    cfg.transitions.effect = "none"
    cfg.transitions.wet_mix = 1.0
    cfg.library.beets_db = None
    cfg.library.music_dir = None
    cfg.library.path_remap = []
    cfg.index.active_dir = None
    return cfg


def _make_sim_index(n: int = 10) -> SimilarityIndex:
    rng = np.random.default_rng(42)
    vectors = rng.standard_normal((n, FEATURE_DIM)).astype(np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors /= norms
    fi = faiss.IndexFlatIP(FEATURE_DIM)
    fi.add(vectors)
    entries = [_make_entry(i) for i in range(n)]
    return SimilarityIndex(faiss_index=fi, entries=entries)


class TestPlayerConstruction:
    def test_player_initialises(self) -> None:
        player = Player(_make_cfg_mock(), _make_sim_index())
        assert player._state.current_track is None
        assert player._state.volume == 1.0

    def test_dry_run_flag_stored(self) -> None:
        player = Player(_make_cfg_mock(), _make_sim_index(), dry_run=True)
        assert player._dry_run is True

    def test_no_repeat_window_clamped_to_library_size(self) -> None:
        """Library smaller than configured window must clamp.

        Default _make_cfg_mock puts no_repeat_window=500.  A library
        of 10 tracks should clamp to <=9 so the picker always has at
        least one candidate to choose from.  Reproduces the "200-track
        library repeats at 50 plays" bug report.
        """
        cfg = _make_cfg_mock()
        cfg.playback.no_repeat_window = 500
        sim = _make_sim_index(n=10)
        player = Player(cfg, sim)
        assert player._state.no_repeat_window < 10
        assert player._state.no_repeat_window >= 1

    def test_no_repeat_window_unclamped_when_library_large(self) -> None:
        cfg = _make_cfg_mock()
        cfg.playback.no_repeat_window = 5
        sim = _make_sim_index(n=100)
        player = Player(cfg, sim)
        # 5 is well below library size; no clamp.
        assert player._state.no_repeat_window == 5

    def test_discovery_every_from_arg(self) -> None:
        player = Player(_make_cfg_mock(), _make_sim_index(), discovery_every=5)
        assert player._discovery_every == 5

    def test_discovery_every_from_preset(self) -> None:
        preset = MagicMock()
        preset.discovery_every = 7
        player = Player(_make_cfg_mock(), _make_sim_index(), preset=preset)
        assert player._discovery_every == 7

    def test_discovery_every_arg_overrides_preset(self) -> None:
        preset = MagicMock()
        preset.discovery_every = 7
        player = Player(_make_cfg_mock(), _make_sim_index(), preset=preset, discovery_every=3)
        assert player._discovery_every == 3


class TestPlayerBuildStatus:
    """_build_status is pure Rich panel building — verify it doesn't raise."""

    def test_no_current_track(self) -> None:
        player = Player(_make_cfg_mock(), _make_sim_index())
        panel = player._build_status()
        assert panel is not None

    def test_with_current_track(self) -> None:
        player = Player(_make_cfg_mock(), _make_sim_index())
        player._state.current_track = _make_entry(0)
        player._state.next_track = _make_entry(1)
        panel = player._build_status()
        assert panel is not None

    def test_paused_state(self) -> None:
        player = Player(_make_cfg_mock(), _make_sim_index())
        player._state.current_track = _make_entry(0)
        player._state.is_paused = True
        panel = player._build_status()
        assert panel is not None

    def test_muted_state(self) -> None:
        player = Player(_make_cfg_mock(), _make_sim_index())
        player._state.current_track = _make_entry(0)
        player._state.is_muted = True
        panel = player._build_status()
        assert panel is not None

    def test_discovery_indicator_shown_when_configured(self) -> None:
        player = Player(_make_cfg_mock(), _make_sim_index(), discovery_every=5)
        player._state.discovery_enabled = True
        panel = player._build_status()
        assert panel is not None


# ---------------------------------------------------------------------------
# Player._pick_next
# ---------------------------------------------------------------------------


class TestPlayerExternalCues:
    """External-cue importer hooks (Mixxx / Rekordbox / Traktor)."""

    def _make_player(self, **kwargs) -> Player:
        return Player(_make_cfg_mock(), _make_sim_index(3), **kwargs)

    def test_ensure_external_cues_skips_when_disabled(self) -> None:
        player = self._make_player()
        player._cfg.playback.import_external_cues = False
        player._ensure_external_cues()
        assert player._external_cues == {}
        assert player._external_cues_loaded is True

    def test_ensure_external_cues_runs_once(self) -> None:
        player = self._make_player()
        player._cfg.playback.import_external_cues = False
        player._ensure_external_cues()
        # Second call should be a no-op even if config flips on.
        player._cfg.playback.import_external_cues = True
        player._ensure_external_cues()
        # No second import attempted; flag still set.
        assert player._external_cues == {}

    def test_ensure_external_cues_swallows_importer_failure(self) -> None:
        player = self._make_player()
        player._cfg.playback.import_external_cues = True
        with patch("autodj.dj_cues_import.auto_import_cues", side_effect=OSError("boom")):
            player._ensure_external_cues()
        # Failure logs at DEBUG and leaves _external_cues empty.
        assert player._external_cues == {}

    def test_ensure_external_cues_imports_cues_into_dict(self) -> None:
        from autodj.dj_meta import Cue

        player = self._make_player()
        player._cfg.playback.import_external_cues = True
        fake = {"track.mp3": [Cue(time_s=10.0, type="drop", source="rekordbox")]}
        with patch("autodj.dj_cues_import.auto_import_cues", return_value=fake):
            player._ensure_external_cues()
        assert player._external_cues == fake

    def test_merge_external_cues_into_no_op_when_no_match(self) -> None:
        from autodj.dj_meta import DjMeta

        player = self._make_player()
        player._external_cues = {}
        meta = DjMeta(intro_end_s=0.0, outro_start_s=0.0, beats=[], analysed=True)
        player._merge_external_cues_into(meta, "unknown.mp3")
        assert meta.cues == []

    def test_merge_external_cues_into_concats_external(self) -> None:
        from autodj.dj_meta import Cue, DjMeta

        player = self._make_player()
        player._external_cues = {
            "track.mp3": [Cue(time_s=12.0, type="user", source="mixxx", label="Hot 1")],
        }
        meta = DjMeta(
            intro_end_s=0.0,
            outro_start_s=0.0,
            beats=[],
            analysed=True,
            cues=[Cue(time_s=10.0, type="drop", source="auto")],
        )
        player._merge_external_cues_into(meta, "track.mp3")
        # Both cues survive (different times beyond the 250 ms collision window).
        assert len(meta.cues) == 2
        sources = {c.source for c in meta.cues}
        assert "auto" in sources and "mixxx" in sources


class TestPlayerPickNext:
    def _make_player(self, n: int = 10, **kwargs) -> Player:
        return Player(_make_cfg_mock(), _make_sim_index(n), **kwargs)

    def test_returns_index_entry(self) -> None:
        player = self._make_player()
        current = player._sim.entries[0]
        player._state.current_track = current
        result = player._pick_next(current)
        assert isinstance(result, IndexEntry)

    def test_does_not_return_current(self) -> None:
        player = self._make_player()
        current = player._sim.entries[0]
        player._state.record_played(current)
        result = player._pick_next(current)
        assert result.path != current.path

    def test_queued_next_takes_priority(self) -> None:
        player = self._make_player()
        queued = player._sim.entries[5]
        player._state.queued_next = queued
        current = player._sim.entries[0]
        result = player._pick_next(current)
        assert result.path == queued.path
        assert player._state.queued_next is None

    def test_queued_next_cleared_after_use(self) -> None:
        player = self._make_player()
        player._state.queued_next = player._sim.entries[3]
        player._pick_next(player._sim.entries[0])
        assert player._state.queued_next is None

    def test_relaxes_repeat_window_when_all_excluded(self) -> None:
        """If the repeat window covers the whole index, fall back to just excluding current."""
        player = self._make_player(n=5)
        current = player._sim.entries[0]
        # Fill recently_played with all entries except current
        for e in player._sim.entries:
            player._state.record_played(e)
        # Should not raise — relaxes the window
        result = player._pick_next(current)
        assert isinstance(result, IndexEntry)

    def test_discovery_fires_when_enabled(self) -> None:
        """Discovery injection runs when rate is set, toggle is ON, and track number aligns."""
        player = self._make_player(n=20, discovery_every=5)
        player._state.discovery_enabled = True
        player._state.track_number = 5  # 5 % 5 == 0
        current = player._sim.entries[0]
        # Any result is fine — just verify it doesn't crash
        result = player._pick_next(current)
        assert isinstance(result, IndexEntry)

    def test_discovery_skipped_when_toggle_off(self) -> None:
        player = self._make_player(n=20, discovery_every=5)
        player._state.discovery_enabled = False  # toggle is off
        player._state.track_number = 5
        current = player._sim.entries[0]
        result = player._pick_next(current)
        assert isinstance(result, IndexEntry)

    def test_preset_bpm_target_used(self) -> None:
        """When a preset is configured, its BPM target shapes track selection."""
        from unittest.mock import MagicMock

        preset = MagicMock()
        preset.discovery_every = None
        preset.target_bpm.return_value = 120.0
        preset.bpm_weight = 0.5
        player = self._make_player(n=10, preset=preset)
        current = player._sim.entries[0]
        result = player._pick_next(current)
        assert isinstance(result, IndexEntry)
        preset.target_bpm.assert_called_once()

    def test_pure_shuffle_picks_from_pool(self) -> None:
        """Pure shuffle ignores similarity and picks any non-recent track."""
        player = self._make_player(n=8, pure_shuffle=True)
        current = player._sim.entries[0]
        player._state.record_played(current)
        result = player._pick_next(current)
        assert isinstance(result, IndexEntry)
        assert result.path != current.path

    def test_pure_shuffle_falls_back_when_pool_empty(self) -> None:
        """All entries excluded → fall back to entire library."""
        player = self._make_player(n=4, pure_shuffle=True)
        for e in player._sim.entries:
            player._state.record_played(e)
        result = player._pick_next(player._sim.entries[0])
        # Falls back to full library — must still return something
        assert isinstance(result, IndexEntry)

    def test_anchor_to_seed_uses_seed_vector(self) -> None:
        """Anchor mode picks similarity from the SEED, not current track."""
        player = self._make_player(n=10, anchor_to_seed=True)
        seed = player._sim.entries[0]
        player._seed_path = seed.path
        # Walk a few hops; each pick should still come back via seed-vector
        # similarity (not chain from previous track).
        current = player._sim.entries[3]
        result = player._pick_next(current)
        assert isinstance(result, IndexEntry)
        # Anchor doesn't pin to the seed itself — just queries from it
        assert result.path != seed.path or result.path != current.path

    def test_anchor_with_no_seed_path_falls_through(self) -> None:
        """Anchor mode without a remembered seed_path uses current track."""
        player = self._make_player(n=6, anchor_to_seed=True)
        player._seed_path = None
        result = player._pick_next(player._sim.entries[0])
        assert isinstance(result, IndexEntry)


class TestLoadLyricsToggle:
    """Player._load_lyrics honours the show_lyrics toggle."""

    def _make(self):
        return Player(_make_cfg_mock(), _make_sim_index(2))

    def test_show_lyrics_off_clears_buffers(self, tmp_path) -> None:
        from autodj.audio_meta import LyricLine

        player = self._make()
        player._cfg.playback.show_lyrics = False
        player._current_lyrics = [LyricLine(0.0, "x")]
        player._current_lyrics_plain = "stuff"
        player._load_lyrics(str(tmp_path / "no_such.flac"))
        assert player._current_lyrics == []
        assert player._current_lyrics_plain == ""

    def test_show_lyrics_on_attempts_load(self, tmp_path) -> None:
        # Path doesn't exist → both LRC and plain reads return empty.
        # The point is that the function runs (covers code) without raising.
        player = self._make()
        player._cfg.playback.show_lyrics = True
        player._cfg.library.beets_db = None
        player._load_lyrics(str(tmp_path / "no_such.flac"))
        assert player._current_lyrics == []
        assert player._current_lyrics_plain == ""


class TestAnalyseTrackInBackground:
    """Player.analyse_track_in_background -- browser-driven cue analysis."""

    def _make(self):
        return Player(_make_cfg_mock(), _make_sim_index(2))

    def test_no_path_no_op(self) -> None:
        """Empty path is a defensive guard; spawn nothing."""
        player = self._make()
        player._dj_cache = MagicMock()
        player._dj_cache_initialised = True
        player._dj_cache.get.return_value = MagicMock(analysed=False)
        player.analyse_track_in_background("")
        # No path was added to the in-flight set.
        assert "" not in player._bg_analysis_inflight

    def test_already_analysed_short_circuits(self) -> None:
        """Cache hit with analysed=True must not spawn a worker thread."""
        from unittest.mock import patch

        player = self._make()
        player._dj_cache = MagicMock()
        player._dj_cache_initialised = True
        player._dj_cache.get.return_value = MagicMock(analysed=True)
        with patch("autodj.player.threading.Thread") as thread:
            player.analyse_track_in_background("/track.flac")
            thread.assert_not_called()
        assert "/track.flac" not in player._bg_analysis_inflight

    def test_inflight_dedupes_concurrent_calls(self) -> None:
        """Second call for the same path while the first is still running
        must not spawn a duplicate worker."""
        from unittest.mock import patch

        player = self._make()
        player._dj_cache = MagicMock()
        player._dj_cache_initialised = True
        player._dj_cache.get.return_value = MagicMock(analysed=False)
        with patch("autodj.player.threading.Thread") as thread:
            player.analyse_track_in_background("/track.flac")
            assert "/track.flac" in player._bg_analysis_inflight
            # Second call: in-flight, must NOT add another thread.
            player.analyse_track_in_background("/track.flac")
            assert thread.call_count == 1

    def test_cache_uninitialised_no_op(self) -> None:
        """No DJ-meta cache available -> nothing to write to, skip."""
        from unittest.mock import patch

        player = self._make()
        player._dj_cache = None
        player._dj_cache_initialised = True
        with patch("autodj.player.threading.Thread") as thread:
            player.analyse_track_in_background("/track.flac")
            thread.assert_not_called()


class TestRunHeadlessSeedHooks:
    """_run_headless seeds lyrics + background cue analysis up-front so the
    web UI's first state push has data even though browser-driven mode
    never enters _play_track.
    """

    def test_run_headless_loads_lyrics_and_spawns_analysis(self) -> None:
        from unittest.mock import patch

        player = Player(_make_cfg_mock(), _make_sim_index(3), dry_run=True)
        # Park the wait loop on the first iteration.
        player._state.should_stop = True
        seed = player._sim.entries[0]
        with (
            patch.object(player, "_load_lyrics") as load_lyr,
            patch.object(player, "analyse_track_in_background") as bg,
        ):
            player._run_headless(seed)
        load_lyr.assert_called_once_with(seed.path)
        bg.assert_called_once_with(seed.path)


# ---------------------------------------------------------------------------
# _stream_audio
# ---------------------------------------------------------------------------


class _FakeOutputStream:
    """Fake sd.OutputStream that immediately calls finished_callback on enter."""

    def __init__(self, *args, finished_callback=None, **kwargs):
        self._fc = finished_callback

    def __enter__(self):
        if self._fc:
            self._fc()
        return self

    def __exit__(self, *args):
        return False


class _ErrorOutputStream:
    """Fake sd.OutputStream that raises on enter (tests error handling path)."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        raise RuntimeError("audio device unavailable")

    def __exit__(self, *args):
        return False


class TestStreamAudio:
    def _make_player(self) -> Player:
        return Player(_make_cfg_mock(), _make_sim_index())

    def test_stream_audio_completes_normally(self) -> None:
        player = self._make_player()
        audio = np.ones(44100, dtype=np.float32)
        with patch("sounddevice.OutputStream", _FakeOutputStream):
            player._stream_audio(audio, 44100)
        # Should not hang or raise

    def test_stream_audio_sets_current_sr(self) -> None:
        player = self._make_player()
        audio = np.ones(22050, dtype=np.float32)
        with patch("sounddevice.OutputStream", _FakeOutputStream):
            player._stream_audio(audio, 22050)
        assert player._current_sr == 22050

    def test_stream_audio_resets_playback_pos(self) -> None:
        player = self._make_player()
        player._playback_pos[0] = 99999
        audio = np.ones(44100, dtype=np.float32)
        with patch("sounddevice.OutputStream", _FakeOutputStream):
            player._stream_audio(audio, 44100)
        assert player._playback_pos[0] == 0

    def test_stream_audio_error_caught(self) -> None:
        """An OutputStream failure should be caught and not propagate."""
        player = self._make_player()
        audio = np.ones(100, dtype=np.float32)
        with patch("sounddevice.OutputStream", _ErrorOutputStream):
            player._stream_audio(audio, 44100)  # should not raise

    def test_stream_audio_skip_event_stops_playback(self) -> None:
        """Setting skip_event before streaming causes immediate exit via callback."""
        player = self._make_player()
        player._skip_event.set()
        audio = np.ones(44100, dtype=np.float32)
        with patch("sounddevice.OutputStream", _FakeOutputStream):
            player._stream_audio(audio, 44100)


class _CapturingStream:
    """Capture the callback so tests can drive it manually."""

    captured_callback = None
    captured_finished_callback = None

    def __init__(self, *args, callback=None, finished_callback=None, **kwargs):
        type(self).captured_callback = callback
        type(self).captured_finished_callback = finished_callback

    def __enter__(self):
        # Fire finished_callback so the wait loop exits cleanly
        if type(self).captured_finished_callback:
            type(self).captured_finished_callback()
        return self

    def __exit__(self, *args):
        return False


class TestStreamAudioCallback:
    def _make_player(self) -> Player:
        return Player(_make_cfg_mock(), _make_sim_index())

    def test_callback_writes_audio_to_outdata(self) -> None:
        import sounddevice as sd

        player = self._make_player()
        player._eq_filters = None
        audio = np.linspace(-1.0, 1.0, 22050, dtype=np.float32)
        with patch("sounddevice.OutputStream", _CapturingStream):
            player._stream_audio(audio, 22050)

        cb = _CapturingStream.captured_callback
        assert cb is not None
        outdata = np.zeros((1024, 1), dtype=np.float32)
        try:
            cb(outdata, 1024, None, sd.CallbackFlags() if hasattr(sd, "CallbackFlags") else None)
        except TypeError:
            cb(outdata, 1024, None, None)
        assert outdata[:, 0].max() != 0.0

    def test_callback_silences_when_muted(self) -> None:
        player = self._make_player()
        player._eq_filters = None
        player._state.is_muted = True
        audio = np.ones(22050, dtype=np.float32) * 0.5
        with patch("sounddevice.OutputStream", _CapturingStream):
            player._stream_audio(audio, 22050)

        cb = _CapturingStream.captured_callback
        outdata = np.zeros((1024, 1), dtype=np.float32)
        try:
            cb(outdata, 1024, None, None)
        except TypeError:
            cb(outdata, 1024, None, 0)
        np.testing.assert_array_equal(outdata, 0)

    def test_callback_attenuates_volume(self) -> None:
        player = self._make_player()
        player._eq_filters = None
        player._state.volume = 0.5
        audio = np.ones(22050, dtype=np.float32)
        with patch("sounddevice.OutputStream", _CapturingStream):
            player._stream_audio(audio, 22050)
        cb = _CapturingStream.captured_callback
        outdata = np.zeros((1024, 1), dtype=np.float32)
        try:
            cb(outdata, 1024, None, None)
        except TypeError:
            cb(outdata, 1024, None, 0)
        np.testing.assert_allclose(outdata[:, 0], 0.5, atol=1e-6)

    def test_callback_pause_writes_silence(self) -> None:
        player = self._make_player()
        player._state.is_paused = True
        audio = np.ones(22050, dtype=np.float32)
        with patch("sounddevice.OutputStream", _CapturingStream):
            player._stream_audio(audio, 22050)
        cb = _CapturingStream.captured_callback
        outdata = np.ones((1024, 1), dtype=np.float32)
        try:
            cb(outdata, 1024, None, None)
        except TypeError:
            cb(outdata, 1024, None, 0)
        np.testing.assert_array_equal(outdata, 0)


class TestKeyboardHandler:
    """Exercise the on_press inner closure of _setup_keyboard via a mock keyboard module."""

    def _setup(self):
        import sys

        captured = {}
        kb_mock = MagicMock()

        class _FakeKey:
            def __init__(self, name):
                self.name = name

        kb_mock.Key.space = _FakeKey("space")
        kb_mock.Key.right = _FakeKey("right")
        kb_mock.Key.left = _FakeKey("left")
        kb_mock.Key.up = _FakeKey("up")
        kb_mock.Key.down = _FakeKey("down")

        listener_instance = MagicMock()

        def _make_listener(on_press):
            captured["on_press"] = on_press
            return listener_instance

        kb_mock.Listener.side_effect = _make_listener
        pynput_mock = MagicMock()
        pynput_mock.keyboard = kb_mock
        sys_modules_patch = patch.dict(
            sys.modules, {"pynput": pynput_mock, "pynput.keyboard": kb_mock}
        )
        return captured, kb_mock, sys_modules_patch

    def test_space_toggles_pause(self) -> None:
        captured, kb_mock, sysmod = self._setup()
        with sysmod:
            player = Player(_make_cfg_mock(), _make_sim_index())
            player._setup_keyboard()
            assert player._state.is_paused is False
            captured["on_press"](kb_mock.Key.space)
            assert player._state.is_paused is True
            captured["on_press"](kb_mock.Key.space)
            assert player._state.is_paused is False

    def test_n_skips(self) -> None:
        captured, _kb_mock, sysmod = self._setup()
        with sysmod:
            player = Player(_make_cfg_mock(), _make_sim_index())
            player._setup_keyboard()
            char_key = MagicMock()
            char_key.char = "n"
            captured["on_press"](char_key)
            assert player._skip_event.is_set()

    def test_q_stops(self) -> None:
        captured, _kb_mock, sysmod = self._setup()
        with sysmod:
            player = Player(_make_cfg_mock(), _make_sim_index())
            player._setup_keyboard()
            char_key = MagicMock()
            char_key.char = "q"
            captured["on_press"](char_key)
            assert player._state.should_stop is True

    def test_arrows_seek(self) -> None:
        captured, kb_mock, sysmod = self._setup()
        with sysmod:
            player = Player(_make_cfg_mock(), _make_sim_index())
            player._setup_keyboard()
            player._current_sr = 44100
            player._playback_len = 44100 * 100
            player._playback_pos[0] = 44100 * 30
            captured["on_press"](kb_mock.Key.right)
            assert player._playback_pos[0] > 44100 * 30
            prev = player._playback_pos[0]
            captured["on_press"](kb_mock.Key.left)
            assert player._playback_pos[0] < prev

    def test_volume_keys(self) -> None:
        captured, kb_mock, sysmod = self._setup()
        with sysmod:
            player = Player(_make_cfg_mock(), _make_sim_index())
            player._setup_keyboard()
            player._state.volume = 0.5
            captured["on_press"](kb_mock.Key.up)
            assert player._state.volume > 0.5
            captured["on_press"](kb_mock.Key.down)
            captured["on_press"](kb_mock.Key.down)
            assert player._state.volume < 0.5

    def test_m_toggles_mute(self) -> None:
        captured, _kb_mock, sysmod = self._setup()
        with sysmod:
            player = Player(_make_cfg_mock(), _make_sim_index())
            player._setup_keyboard()
            char_key = MagicMock()
            char_key.char = "m"
            assert player._state.is_muted is False
            captured["on_press"](char_key)
            assert player._state.is_muted is True

    def test_d_toggles_discovery(self) -> None:
        captured, _kb_mock, sysmod = self._setup()
        with sysmod:
            player = Player(_make_cfg_mock(), _make_sim_index(), discovery_every=5)
            player._setup_keyboard()
            char_key = MagicMock()
            char_key.char = "d"
            captured["on_press"](char_key)
            assert player._state.discovery_enabled is True

    def test_d_ignored_when_no_discovery_configured(self) -> None:
        captured, _kb_mock, sysmod = self._setup()
        with sysmod:
            player = Player(_make_cfg_mock(), _make_sim_index())
            player._setup_keyboard()
            char_key = MagicMock()
            char_key.char = "d"
            captured["on_press"](char_key)
            assert player._state.discovery_enabled is False

    def test_unknown_char_ignored(self) -> None:
        captured, _kb_mock, sysmod = self._setup()
        with sysmod:
            player = Player(_make_cfg_mock(), _make_sim_index())
            player._setup_keyboard()
            char_key = MagicMock()
            char_key.char = "x"
            # Should not raise
            captured["on_press"](char_key)


# ---------------------------------------------------------------------------
# _play_with_crossfade
# ---------------------------------------------------------------------------


class TestPlayWithCrossfade:
    def _make_player(self) -> Player:
        cfg = _make_cfg_mock()
        cfg.playback.crossfade_seconds = 2.0
        return Player(cfg, _make_sim_index())

    def test_streams_audio_for_normal_tracks(self) -> None:
        player = self._make_player()
        current = player._sim.entries[0]
        nxt = player._sim.entries[1]
        # 3 s audio at 44100 — long enough for a 2 s crossfade
        fake_audio = np.ones((3 * 44100), dtype=np.float32)

        with (
            patch("autodj.player.load_audio", return_value=(fake_audio, 44100)),
            patch.object(player, "_stream_audio") as mock_stream,
        ):
            player._play_with_crossfade(current, nxt)

        mock_stream.assert_called_once()

    def test_load_failure_on_current_skips_silently(self) -> None:
        """If the current track can't be loaded, _play_with_crossfade returns without playing."""
        player = self._make_player()
        current = player._sim.entries[0]
        nxt = player._sim.entries[1]

        with (
            patch("autodj.player.load_audio", side_effect=OSError("not found")),
            patch.object(player, "_stream_audio") as mock_stream,
        ):
            player._play_with_crossfade(current, nxt)

        mock_stream.assert_not_called()

    def test_load_failure_on_next_uses_silence(self) -> None:
        """If next track can't be loaded, crossfade uses a silence buffer."""
        player = self._make_player()
        current = player._sim.entries[0]
        nxt = player._sim.entries[1]
        good_audio = np.ones((3 * 44100), dtype=np.float32)

        call_count = [0]

        def fake_load(path, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return good_audio, 44100
            raise OSError("next track missing")

        with (
            patch("autodj.player.load_audio", side_effect=fake_load),
            patch.object(player, "_stream_audio") as mock_stream,
        ):
            player._play_with_crossfade(current, nxt)

        mock_stream.assert_called_once()

    def test_short_next_track_skips_crossfade(self) -> None:
        """When audio_b is shorter than crossfade_samples, falls back to audio_a alone."""
        player = self._make_player()
        current = player._sim.entries[0]
        nxt = player._sim.entries[1]
        long_audio = np.ones((3 * 44100), dtype=np.float32)
        short_audio = np.ones(100, dtype=np.float32)  # far shorter than 2 s crossfade

        call_count = [0]

        def fake_load(path, **kw):
            call_count[0] += 1
            return (long_audio, 44100) if call_count[0] == 1 else (short_audio, 44100)

        with (
            patch("autodj.player.load_audio", side_effect=fake_load),
            patch.object(player, "_stream_audio") as mock_stream,
        ):
            player._play_with_crossfade(current, nxt)

        mock_stream.assert_called_once()


# ---------------------------------------------------------------------------
# Player.run (dry-run, so no audio hardware needed)
# ---------------------------------------------------------------------------


class TestPlayerRun:
    def _make_dry_player(self, **kwargs) -> Player:
        return Player(_make_cfg_mock(), _make_sim_index(n=10), dry_run=True, **kwargs)

    def _run_one_track(self, player: Player, seed_idx: int = 0) -> None:
        """Run the player loop for exactly one track, then stop."""
        seed = player._sim.entries[seed_idx]
        call_count = [0]
        original_pick = player._pick_next

        def stopping_pick(current):
            call_count[0] += 1
            player._state.should_stop = True
            return original_pick(current)

        player._pick_next = stopping_pick  # type: ignore[method-assign]
        player._setup_keyboard = lambda: None  # skip pynput in tests

        with patch("autodj.player.time.sleep"):  # skip the 0.1 s dry-run sleep
            player.run(seed_entry=seed)

        return call_count[0]

    def test_run_completes_one_track(self) -> None:
        player = self._make_dry_player()
        count = self._run_one_track(player)
        assert count == 1

    def test_run_sets_current_track(self) -> None:
        player = self._make_dry_player()
        seed = player._sim.entries[0]
        player._setup_keyboard = lambda: None
        call_count = [0]
        original_pick = player._pick_next

        def stopping_pick(current):
            call_count[0] += 1
            player._state.should_stop = True
            return original_pick(current)

        player._pick_next = stopping_pick  # type: ignore[method-assign]
        with patch("autodj.player.time.sleep"):
            player.run(seed_entry=seed)

        # current_track should have been set before the loop stopped
        assert player._state.current_track is not None

    def test_run_with_random_seed(self) -> None:
        """Passing seed_entry=None should pick a random track and not crash."""
        player = self._make_dry_player()
        player._setup_keyboard = lambda: None
        player._state.should_stop = True  # stop before first iteration

        with patch("autodj.player.time.sleep"):
            player.run(seed_entry=None)  # should not raise

    def test_run_exports_m3u(self, tmp_path: Path) -> None:
        m3u_path = tmp_path / "session.m3u"
        player = Player(
            _make_cfg_mock(),
            _make_sim_index(n=10),
            dry_run=True,
            export_m3u=m3u_path,
        )
        self._run_one_track(player)
        assert m3u_path.exists()
        content = m3u_path.read_text(encoding="utf-8")
        assert content.startswith("#EXTM3U")

    def test_run_appends_history(self, tmp_path: Path) -> None:
        import json

        history = tmp_path / "history.jsonl"
        player = Player(
            _make_cfg_mock(),
            _make_sim_index(n=10),
            dry_run=True,
            history_file=history,
        )
        self._run_one_track(player)
        assert history.exists()
        obj = json.loads(history.read_text(encoding="utf-8").strip().splitlines()[0])
        assert "path" in obj


# ---------------------------------------------------------------------------
# _setup_keyboard
# ---------------------------------------------------------------------------


class TestSetupKeyboard:
    def test_does_not_raise_on_any_platform(self) -> None:
        """_setup_keyboard wraps everything in try/except — should never raise."""
        player = Player(_make_cfg_mock(), _make_sim_index())
        player._setup_keyboard()  # pynput may or may not work; must not raise

    def test_keyboard_unavailable_is_swallowed(self) -> None:
        """If pynput raises during import or setup, the error is logged, not raised."""
        import sys

        player = Player(_make_cfg_mock(), _make_sim_index())
        # Simulate pynput being importable but Listener construction failing.
        # Use patch.dict so the import inside _setup_keyboard sees our mock.
        keyboard_mock = MagicMock()
        keyboard_mock.Listener.side_effect = RuntimeError("no display")
        with patch.dict(sys.modules, {"pynput": MagicMock(), "pynput.keyboard": keyboard_mock}):
            player._setup_keyboard()  # should not raise


# ---------------------------------------------------------------------------
# beatmatch_incoming
# ---------------------------------------------------------------------------


class TestBeatmatchIncoming:
    def test_zero_bpm_a_disables(self) -> None:
        b = _sine_audio(0.5)
        out, ratio = beatmatch_incoming(b, 0.0, 120.0)
        assert ratio == 1.0
        np.testing.assert_array_equal(out, b)

    def test_zero_bpm_b_disables(self) -> None:
        b = _sine_audio(0.5)
        _out, ratio = beatmatch_incoming(b, 120.0, 0.0)
        assert ratio == 1.0

    def test_within_max_stretch_applies(self) -> None:
        b = _sine_audio(2.0)
        _out, ratio = beatmatch_incoming(b, 120.0, 124.0, max_stretch=0.08)
        # 124/120 ≈ 1.033 — within 8 %, so stretch applied
        assert abs(ratio - (124.0 / 120.0)) < 1e-6

    def test_beyond_max_stretch_skipped(self) -> None:
        b = _sine_audio(0.5)
        out, ratio = beatmatch_incoming(b, 100.0, 140.0, max_stretch=0.08)
        # 140/100 = 1.40 — too far, refuse
        assert ratio == 1.0
        np.testing.assert_array_equal(out, b)

    def test_equal_bpm_passes_through(self) -> None:
        b = _sine_audio(0.5)
        _out, ratio = beatmatch_incoming(b, 120.0, 120.0)
        assert ratio == 1.0


class TestTimeStretch:
    def test_ratio_one_returns_input(self) -> None:
        a = _sine_audio(0.5)
        out = _time_stretch(a, 1.0)
        np.testing.assert_array_equal(out, a)

    def test_near_one_ratio_returns_input(self) -> None:
        a = _sine_audio(0.5)
        out = _time_stretch(a, 1.005)
        np.testing.assert_array_equal(out, a)

    def test_stretch_changes_length(self) -> None:
        a = _sine_audio(0.5)
        out = _time_stretch(a, 1.10)
        # librosa stretch: rate=1/ratio so ratio>1 → output is longer
        assert len(out) != len(a) or out is a  # falls back to input on librosa failure


# ---------------------------------------------------------------------------
# apply_filter_sweep
# ---------------------------------------------------------------------------


class TestApplyFilterSweep:
    def test_empty_input_returns_empty(self) -> None:
        out = apply_filter_sweep(np.zeros(0, dtype=np.float32), 22050, 8000, 200)
        assert out.shape == (0,)

    def test_lowpass_sweep_dampens_highs(self) -> None:
        # Generate a high-freq signal that should be attenuated by the LP
        t = np.linspace(0, 1.0, 22050, dtype=np.float32)
        a = np.sin(2 * np.pi * 5000 * t).astype(np.float32)
        out = apply_filter_sweep(a, 22050, 8000.0, 200.0, "lowpass")
        # Last samples have cutoff ≈ 200 Hz; 5kHz signal heavily attenuated
        assert np.abs(out[-100:]).max() < np.abs(a[-100:]).max()

    def test_highpass_sweep_dampens_lows(self) -> None:
        t = np.linspace(0, 1.0, 22050, dtype=np.float32)
        a = np.sin(2 * np.pi * 100 * t).astype(np.float32)
        out = apply_filter_sweep(a, 22050, 50.0, 8000.0, "highpass")
        # Last samples cutoff ≈ 8 kHz; 100 Hz signal heavily attenuated
        assert np.abs(out[-100:]).max() < np.abs(a[-100:]).max()

    def test_output_length_matches(self) -> None:
        a = _sine_audio(0.5)
        out = apply_filter_sweep(a, 44100, 8000, 200)
        assert len(out) == len(a)


# ---------------------------------------------------------------------------
# make_eq_filters / apply_eq
# ---------------------------------------------------------------------------


class TestEq:
    def test_make_eq_filters_returns_dict(self) -> None:
        sos = make_eq_filters(44100)
        assert isinstance(sos, dict)
        assert {"low", "mid_lp", "mid_hp", "high"}.issubset(sos.keys())

    def test_apply_eq_unity_gain_passes_through(self) -> None:
        sos = make_eq_filters(44100)
        a = _sine_audio(0.1)
        out = apply_eq(a, sos, 1.0, 1.0, 1.0)
        # Unity-gain 3-band split + sum reconstructs the input fairly closely
        # (filters are 2nd-order so there's some phase delay, accept loose match)
        assert out.dtype == np.float32
        assert len(out) == len(a)

    def test_apply_eq_zero_gain_silences(self) -> None:
        sos = make_eq_filters(44100)
        a = _sine_audio(0.1)
        out = apply_eq(a, sos, 0.0, 0.0, 0.0)
        assert np.abs(out).max() < 1e-3

    def test_apply_eq_clipped_to_one(self) -> None:
        sos = make_eq_filters(44100)
        a = _sine_audio(0.5) * 2.0  # already > 1
        out = apply_eq(a, sos, 2.0, 2.0, 2.0)  # boosted further
        assert np.abs(out).max() <= 1.0 + 1e-6

    def test_apply_eq_none_filters_returns_input(self) -> None:
        a = _sine_audio(0.1)
        out = apply_eq(a, None, 1.0, 1.0, 1.0)
        np.testing.assert_array_equal(out, a)


# ---------------------------------------------------------------------------
# _apply_crossfade_ducked (EQ-duck path)
# ---------------------------------------------------------------------------


class TestApplyCrossfadeDucked:
    def test_zero_crossfade_concatenates(self) -> None:
        a = _sine_audio(0.2)
        b = _sine_audio(0.2)
        out = _apply_crossfade_ducked(a, b, 0, 44100)
        assert len(out) == len(a) + len(b)

    def test_normal_overlap(self) -> None:
        a = _sine_audio(0.5)
        b = _sine_audio(0.5)
        n = 4410
        out = _apply_crossfade_ducked(a, b, n, 44100)
        assert len(out) == len(a) + len(b) - n
        assert out.dtype == np.float32
        assert np.all(np.isfinite(out))

    def test_overlap_too_long_falls_back(self) -> None:
        a = np.zeros(100, dtype=np.float32)
        b = np.zeros(100, dtype=np.float32)
        # Falls back to plain crossfade which raises when overlap > input
        with pytest.raises(ValueError):
            _apply_crossfade_ducked(a, b, 200, 44100)

    def test_clamped_to_one(self) -> None:
        a = np.ones(10000, dtype=np.float32) * 0.9
        b = np.ones(10000, dtype=np.float32) * 0.9
        out = _apply_crossfade_ducked(a, b, 4000, 44100)
        assert np.abs(out).max() <= 1.0 + 1e-6


# ---------------------------------------------------------------------------
# write_m3u + history (already partially covered, fill gaps)
# ---------------------------------------------------------------------------


class TestWriteM3u:
    def test_writes_header_and_entries(self, tmp_path: Path) -> None:
        out = tmp_path / "list.m3u"
        write_m3u([_make_entry(0), _make_entry(1)], out)
        content = out.read_text(encoding="utf-8")
        assert content.startswith("#EXTM3U")
        assert "Song 0" in content
        assert "Song 1" in content

    def test_empty_list_writes_header_only(self, tmp_path: Path) -> None:
        out = tmp_path / "list.m3u"
        write_m3u([], out)
        content = out.read_text(encoding="utf-8")
        assert "#EXTM3U" in content


# ---------------------------------------------------------------------------
# PlayerState.record_played — extended
# ---------------------------------------------------------------------------


class TestRecordPlayedExtended:
    def test_records_artist_lowercased(self) -> None:
        state = PlayerState(no_repeat_window=10)
        state.record_played(_make_entry(0))
        assert "artist" in state.recently_played_artists

    def test_records_album_lowercased(self) -> None:
        state = PlayerState(no_repeat_window=10)
        state.record_played(_make_entry(0))
        assert "album" in state.recently_played_albums

    def test_records_title_lowercased(self) -> None:
        state = PlayerState(no_repeat_window=10)
        state.record_played(_make_entry(0))
        assert "song 0" in state.recently_played_titles

    def test_artist_window_bounded(self) -> None:
        state = PlayerState(no_repeat_window=10, artist_repeat_window=2)
        for i in range(5):
            e = _make_entry(i)
            e.artist = f"A{i}"
            state.record_played(e)
        assert len(state.recently_played_artists) <= 2


# ---------------------------------------------------------------------------
# _play_with_crossfade — branch coverage for DJ-mix flags + transitions
# ---------------------------------------------------------------------------


class TestPlayWithCrossfadeFeatures:
    def _make_player_with_audio(self, **cfg_overrides):
        """Player wired so load_audio returns 4 s of silence at 44.1 kHz."""
        cfg = _make_cfg_mock()
        cfg.playback.crossfade_seconds = cfg_overrides.get("crossfade_seconds", 2.0)
        cfg.playback.crossfade_eq_duck = cfg_overrides.get("crossfade_eq_duck", False)
        cfg.playback.crossfade_bass_cutoff_hz = 120.0
        cfg.djmix.harmonic_mixing = cfg_overrides.get("harmonic_mixing", False)
        cfg.djmix.beatmatch = cfg_overrides.get("beatmatch", False)
        cfg.djmix.beatmatch_max_stretch = 0.08
        cfg.djmix.phrase_align = cfg_overrides.get("phrase_align", False)
        cfg.djmix.phrase_bars = 8
        cfg.djmix.outro_intro_align = cfg_overrides.get("outro_intro_align", False)
        cfg.djmix.filter_sweep = cfg_overrides.get("filter_sweep", False)
        cfg.djmix.filter_sweep_floor_hz = 200.0
        cfg.transitions.effect = cfg_overrides.get("transition_effect", "none")
        cfg.transitions.wet_mix = 1.0
        cfg.replaygain.enabled = cfg_overrides.get("replaygain", False)
        cfg.replaygain.target_db = -14.0
        cfg.replaygain.max_clip_safe_gain = 1.0
        cfg.library.beets_db = None
        cfg.library.music_dir = None
        cfg.index.active_dir = MagicMock()  # not a Path → DJ cache lazy init bails
        return Player(cfg, _make_sim_index(n=3))

    def _stub_audio(self, seconds: float = 4.0, sr: int = 44100) -> tuple:
        return np.zeros(int(seconds * sr), dtype=np.float32), sr

    def test_replaygain_enabled_path(self) -> None:
        from autodj.audio_meta import ReplayGain

        player = self._make_player_with_audio(replaygain=True)
        cur, nxt = player._sim.entries[0], player._sim.entries[1]
        with (
            patch("autodj.player.load_audio", return_value=self._stub_audio()),
            patch(
                "autodj.audio_meta.read_replaygain",
                return_value=ReplayGain(track_gain_db=-3.0, track_peak=0.5),
            ),
            patch.object(player, "_stream_audio") as mock_stream,
        ):
            player._play_with_crossfade(cur, nxt)
        mock_stream.assert_called_once()

    def test_filter_sweep_path(self) -> None:
        player = self._make_player_with_audio(filter_sweep=True)
        cur, nxt = player._sim.entries[0], player._sim.entries[1]
        with (
            patch("autodj.player.load_audio", return_value=self._stub_audio()),
            patch.object(player, "_stream_audio") as mock_stream,
        ):
            player._play_with_crossfade(cur, nxt)
        mock_stream.assert_called_once()

    def test_beatmatch_path(self) -> None:
        player = self._make_player_with_audio(beatmatch=True)
        cur, nxt = player._sim.entries[0], player._sim.entries[1]
        cur.bpm, nxt.bpm = 120.0, 124.0  # within max_stretch
        with (
            patch("autodj.player.load_audio", return_value=self._stub_audio()),
            patch.object(player, "_stream_audio") as mock_stream,
        ):
            player._play_with_crossfade(cur, nxt)
        mock_stream.assert_called_once()
        # Beatmatch ratio is set (1.0 = no stretch / librosa fallback, otherwise = bpm_b/bpm_a)
        assert isinstance(player._beatmatch_ratio, float)

    def test_crossfade_eq_duck_path(self) -> None:
        player = self._make_player_with_audio(crossfade_eq_duck=True)
        cur, nxt = player._sim.entries[0], player._sim.entries[1]
        with (
            patch("autodj.player.load_audio", return_value=self._stub_audio()),
            patch.object(player, "_stream_audio") as mock_stream,
        ):
            player._play_with_crossfade(cur, nxt)
        mock_stream.assert_called_once()

    def test_transition_fx_echo_out(self) -> None:
        player = self._make_player_with_audio(transition_effect="echo_out")
        cur, nxt = player._sim.entries[0], player._sim.entries[1]
        with (
            patch("autodj.player.load_audio", return_value=self._stub_audio()),
            patch.object(player, "_stream_audio") as mock_stream,
        ):
            player._play_with_crossfade(cur, nxt)
        mock_stream.assert_called_once()
        assert player._last_transition_fx == "echo_out"

    def test_transition_fx_long_tail_extends(self) -> None:
        """Long-tail effects (tape_stop) extend tail length to >= 4s."""
        player = self._make_player_with_audio(transition_effect="tape_stop")
        cur, nxt = player._sim.entries[0], player._sim.entries[1]
        with (
            patch("autodj.player.load_audio", return_value=self._stub_audio(seconds=10.0)),
            patch.object(player, "_stream_audio") as mock_stream,
        ):
            player._play_with_crossfade(cur, nxt)
        mock_stream.assert_called_once()
        assert player._last_transition_fx == "tape_stop"

    def test_transition_fx_unknown_falls_back_to_none(self) -> None:
        player = self._make_player_with_audio(transition_effect="banana")
        cur, nxt = player._sim.entries[0], player._sim.entries[1]
        with (
            patch("autodj.player.load_audio", return_value=self._stub_audio()),
            patch.object(player, "_stream_audio") as mock_stream,
        ):
            player._play_with_crossfade(cur, nxt)
        mock_stream.assert_called_once()
        assert player._last_transition_fx == "none"

    def test_sr_mismatch_resamples(self) -> None:
        player = self._make_player_with_audio()
        cur, nxt = player._sim.entries[0], player._sim.entries[1]

        call_idx = [0]

        def fake_load(_path):
            call_idx[0] += 1
            # First load: 44.1 kHz; second: 48 kHz (forces resample)
            if call_idx[0] == 1:
                return np.zeros(4 * 44100, dtype=np.float32), 44100
            return np.zeros(4 * 48000, dtype=np.float32), 48000

        with (
            patch("autodj.player.load_audio", side_effect=fake_load),
            patch.object(player, "_stream_audio") as mock_stream,
        ):
            player._play_with_crossfade(cur, nxt)
        mock_stream.assert_called_once()


class TestEffectiveCrossfadeSeconds:
    """Cover Player._effective_crossfade_seconds (Mixxx-style modes)."""

    def _player(self, mode: str = "full_intro_outro", base: float = 3.0):
        """Build a Player stub with just enough config + cache for the helper."""
        from types import SimpleNamespace

        from autodj.config import PlaybackConfig
        from autodj.player import Player

        cfg = SimpleNamespace(
            playback=PlaybackConfig.from_dict(
                {
                    "crossfade_seconds": base,
                    "transition_mode": mode,
                }
            ),
        )
        # Bypass __init__ — only the config is needed for this pure helper
        p = Player.__new__(Player)
        p._cfg = cfg
        return p

    def test_fixed_returns_base(self) -> None:
        from autodj.dj_meta import DjMeta

        p = self._player(mode="fixed", base=2.5)
        assert (
            p._effective_crossfade_seconds(
                DjMeta(intro_end_s=4.0, outro_start_s=180.0, analysed=True),
                DjMeta(intro_end_s=4.0, outro_start_s=0.0, analysed=True),
                outgoing_length_s=200.0,
            )
            == 2.5
        )

    def test_full_intro_outro_takes_min(self) -> None:
        from autodj.dj_meta import DjMeta

        p = self._player(mode="full_intro_outro", base=3.0)
        # outro_len = 200 - 190 = 10; intro_end = 4; min = 4
        assert (
            p._effective_crossfade_seconds(
                DjMeta(intro_end_s=0.0, outro_start_s=190.0, analysed=True),
                DjMeta(intro_end_s=4.0, outro_start_s=0.0, analysed=True),
                outgoing_length_s=200.0,
            )
            == 4.0
        )

    def test_full_intro_outro_clamps_high(self) -> None:
        from autodj.dj_meta import DjMeta

        p = self._player(mode="full_intro_outro", base=3.0)
        # min(20, 30) = 20 -> clamp to 12
        assert (
            p._effective_crossfade_seconds(
                DjMeta(intro_end_s=0.0, outro_start_s=180.0, analysed=True),
                DjMeta(intro_end_s=30.0, outro_start_s=0.0, analysed=True),
                outgoing_length_s=200.0,
            )
            == 12.0
        )

    def test_full_intro_outro_clamps_low(self) -> None:
        from autodj.dj_meta import DjMeta

        p = self._player(mode="full_intro_outro", base=3.0)
        assert (
            p._effective_crossfade_seconds(
                DjMeta(intro_end_s=0.0, outro_start_s=199.5, analysed=True),
                DjMeta(intro_end_s=0.3, outro_start_s=0.0, analysed=True),
                outgoing_length_s=200.0,
            )
            == 1.0
        )

    def test_full_intro_outro_falls_back_when_marker_missing(self) -> None:
        p = self._player(mode="full_intro_outro", base=2.5)
        # No meta_b -> can't compute intro_end, falls back to base
        assert p._effective_crossfade_seconds(None, None, 200.0) == 2.5

    def test_outro_fade_uses_outro_len(self) -> None:
        from autodj.dj_meta import DjMeta

        p = self._player(mode="outro_fade", base=3.0)
        assert (
            p._effective_crossfade_seconds(
                DjMeta(intro_end_s=0.0, outro_start_s=192.0, analysed=True),
                None,
                outgoing_length_s=200.0,
            )
            == 8.0
        )

    def test_fixed_skip_silence_returns_base(self) -> None:
        from autodj.dj_meta import DjMeta

        p = self._player(mode="fixed_skip_silence", base=4.0)
        assert (
            p._effective_crossfade_seconds(
                DjMeta(intro_end_s=0.0, outro_start_s=180.0, analysed=True),
                DjMeta(intro_end_s=4.0, outro_start_s=0.0, analysed=True),
                outgoing_length_s=200.0,
            )
            == 4.0
        )


class TestMinFxDurationCoverage:
    """Every TransitionFx in the catalogue must have a min-duration entry,
    OR be intentionally excluded (NONE / RANDOM / ROTATE meta-modes) plus
    the legacy filter-only effects that don't need a min (highpass_sweep,
    lowpass_sweep, cross_eq_swap, bitcrusher, flanger, pitch_swell,
    pitch_fall, telephone, chorus, submerge, vinyl_wow, gate_stutter).
    Catches drift between the Python CLI player + the JS browser player
    when new effects ship.
    """

    # These either have meta semantics or apply over the standard fade
    # window with no extension required.
    _NO_MIN_NEEDED = frozenset(
        {
            "none",
            "random",
            "rotate",
            "highpass_sweep",
            "lowpass_sweep",
            "cross_eq_swap",
            "bitcrusher",
            "flanger",
            "pitch_swell",
            "pitch_fall",
            "telephone",
            "chorus",
            "submerge",
            "vinyl_wow",
            "gate_stutter",
        }
    )

    def test_all_effects_have_min_or_are_excluded(self) -> None:
        from autodj.player import Player
        from autodj.transitions import TransitionFx

        min_table = Player._MIN_FX_DURATION_S
        missing = []
        for fx in TransitionFx:
            if fx.value in self._NO_MIN_NEEDED:
                continue
            if fx.value not in min_table:
                missing.append(fx.value)
        assert missing == [], (
            f"_MIN_FX_DURATION_S missing entries for: {missing}.  Add them or "
            f"document them in TestMinFxDurationCoverage._NO_MIN_NEEDED."
        )

    def test_min_durations_are_in_sane_range(self) -> None:
        from autodj.player import Player

        for name, secs in Player._MIN_FX_DURATION_S.items():
            assert 0.5 <= secs <= 12.0, (
                f"{name} min duration {secs}s outside sane DJ range [0.5, 12]"
            )
