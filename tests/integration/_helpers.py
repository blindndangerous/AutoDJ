"""Shared mock-builders for the integration test suite.

Pulled out so both ``test_server.py`` (legacy route + bridge tests)
and ``test_server_recent.py`` (today's adds: version stamp, advance
banner, key notation, etc.) can build identical Player / SimilarityIndex
mocks without each duplicating the ~100-line setup.

Helpers are plain functions, not pytest fixtures, so they can be
called from inside test bodies that need to *override* attributes.
The ``client`` and ``bridge`` fixtures live in ``conftest.py`` and
delegate to these.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from autodj.indexer import IndexEntry


def _make_entry(i: int = 0) -> IndexEntry:
    """Build a deterministic test ``IndexEntry`` keyed by ``i``."""
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


def _make_player_mock(entry: IndexEntry | None = None) -> MagicMock:
    """Build a mock Player carrying every attribute PlayerBridge touches."""
    from autodj.player import PlayerState

    player = MagicMock()
    state = PlayerState()
    state.current_track = entry or _make_entry(0)
    state.next_track = _make_entry(1)
    state.is_paused = False
    state.volume = 1.0
    state.is_muted = False
    state.queue = []
    player._state = state
    player._playback_pos = [44100 * 30]  # 30 s into the track
    player._current_sr = 44100
    player._skip_event = MagicMock()
    # Fields exposed via get_state / get_settings on the bridge API.
    player._eq_low = 1.0
    player._eq_mid = 1.0
    player._eq_high = 1.0
    player._beatmatch_ratio = 1.0
    player._last_transition_fx = "none"
    player._dry_run = False
    player._smart_shuffle = False
    player._pure_shuffle = False
    player._anchor_to_seed = False
    player._seed_path = None
    player._previous_track = None
    player._last_pick_mode = "seed"
    player._bpm_range = None
    player._preset = None
    player._discovery_every = None
    player._current_lyrics = []
    player._current_lyrics_plain = ""
    # Concrete config so get_settings does not traverse MagicMock chains
    # and trip JSONResponse on un-serialisable mocks.
    cfg = MagicMock()
    cfg.transitions.effect = "none"
    cfg.transitions.wet_mix = 1.0
    cfg.djmix.harmonic_mixing = False
    cfg.djmix.harmonic_mode = "compatible"
    cfg.djmix.beatmatch = False
    cfg.djmix.phrase_align = False
    cfg.djmix.outro_intro_align = False
    cfg.djmix.filter_sweep = False
    cfg.playback.crossfade_seconds = 3.0
    cfg.playback.fade_in_seconds = 3.0
    cfg.playback.crossfade_eq_duck = False
    cfg.playback.transition_mode = "full_intro_outro"
    cfg.playback.key_notation = "camelot"
    cfg.playback.key_prefer_flats = False
    cfg.playback.show_lyrics = True
    cfg.playback.prefetch_next_track = True
    cfg.playback.silence_trigger_crossfade = True
    cfg.playback.enable_daypart = False
    cfg.playback.enable_mood_arc = False
    cfg.playback.mood_arc_hours = 3.0
    cfg.playback.import_external_cues = True
    cfg.playback.beat_sync_fx = True
    cfg.playback.key_sync_fx = True
    cfg.playback.beatmatch_on_skip = False
    cfg.playback.liners_enabled = False
    cfg.playback.liners_folder = None
    cfg.playback.liners_every_n_songs = None
    cfg.playback.liners_every_minutes = None
    cfg.playback.liners_random_min_minutes = None
    cfg.playback.liners_random_max_minutes = None
    cfg.playback.liners_pick_mode = "random"
    cfg.playback.liners_duck_db = -12.0
    cfg.index.active_dir = "/tmp/_autodj_no_index"
    cfg.replaygain.enabled = False
    cfg.presets = {}
    player._cfg = cfg
    return player


def _make_sim_mock(entries: list[IndexEntry] | None = None) -> MagicMock:
    """Build a mock SimilarityIndex carrying a default 5-track list."""
    sim = MagicMock()
    sim.entries = entries if entries is not None else [_make_entry(i) for i in range(5)]
    return sim
