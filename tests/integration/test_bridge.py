"""PlayerBridge state, settings and queue behaviour against a mocked player."""

from __future__ import annotations

import pytest

from autodj.dj_meta import Cue, DjMeta
from autodj.indexer import IndexEntry
from autodj.server import PlaybackSettingsBody


def _entry(**over) -> IndexEntry:
    defaults = {
        "path": "Z:/m/x.flac",
        "title": "t",
        "artist": "a",
        "album": "al",
        "genre": "g",
        "bpm": 120.0,
        "year": 2000,
        "length": 180.0,
        "energy": 0.05,
        "key": 0,
        "mode": 1,
        "tempo_confidence": 0.8,
    }
    defaults.update(over)
    return IndexEntry(**defaults)


class _StaticCache:
    """Pretend dj_meta cache returning a fixed DjMeta per path."""

    def __init__(self, meta_by_path: dict[str, DjMeta]) -> None:
        self._m = meta_by_path

    def get(self, path: str) -> DjMeta:
        return self._m.get(path, DjMeta(analysed=False))


def _attach_cache(bridge, cache) -> None:
    bridge.player._dj_cache = cache
    bridge.player._ensure_dj_cache = lambda: None


class TestStateHelpers:
    def test_markers_returns_none_when_no_cache(self, bridge) -> None:
        # No dj_cache attribute → markers return all None
        bridge.player._ensure_dj_cache = lambda: None
        bridge.player._dj_cache = None
        state = bridge.get_state()
        assert state["current_track"]["intro_end_s"] is None
        assert state["current_track"]["outro_start_s"] is None
        assert state["current_track"]["outro_len"] is None

    def test_markers_returns_none_when_meta_not_analysed(self, bridge) -> None:
        cache = _StaticCache(
            {bridge.player._state.current_track.path: DjMeta(analysed=False, intro_end_s=5.0)}
        )
        _attach_cache(bridge, cache)
        st = bridge.get_state()
        assert st["current_track"]["intro_end_s"] is None
        assert st["current_track"]["outro_start_s"] is None

    def test_markers_returns_intro_only_when_outro_zero(self, bridge) -> None:
        cache = _StaticCache(
            {
                bridge.player._state.current_track.path: DjMeta(
                    analysed=True, intro_end_s=3.0, outro_start_s=0.0
                )
            }
        )
        _attach_cache(bridge, cache)
        st = bridge.get_state()
        assert st["current_track"]["intro_end_s"] == 3.0
        assert st["current_track"]["outro_start_s"] is None
        assert st["current_track"]["outro_len"] is None

    def test_markers_full_path(self, bridge) -> None:
        cache = _StaticCache(
            {
                bridge.player._state.current_track.path: DjMeta(
                    analysed=True, intro_end_s=4.0, outro_start_s=170.0
                )
            }
        )
        _attach_cache(bridge, cache)
        st = bridge.get_state()
        assert st["current_track"]["intro_start_s"] is None
        assert st["current_track"]["intro_end_s"] == 4.0
        assert st["current_track"]["outro_start_s"] == 170.0
        # outro_len = 180 - 170 = 10
        assert st["current_track"]["outro_len"] == pytest.approx(10.0)

    def test_markers_include_intro_start(self, bridge) -> None:
        cache = _StaticCache(
            {
                bridge.player._state.current_track.path: DjMeta(
                    analysed=True, intro_start_s=0.4567, intro_end_s=4.0, outro_start_s=170.0
                )
            }
        )
        _attach_cache(bridge, cache)
        st = bridge.get_state()
        assert st["current_track"]["intro_start_s"] == 0.457
        assert st["current_track"]["intro_end_s"] == 4.0

    def test_cues_empty_when_no_cache(self, bridge) -> None:
        bridge.player._ensure_dj_cache = lambda: None
        bridge.player._dj_cache = None
        st = bridge.get_state()
        assert st["current_track"]["cues"] == []

    def test_cues_empty_when_not_analysed(self, bridge) -> None:
        cache = _StaticCache({bridge.player._state.current_track.path: DjMeta(analysed=False)})
        _attach_cache(bridge, cache)
        st = bridge.get_state()
        assert st["current_track"]["cues"] == []

    def test_cues_phrase_subsampled(self, bridge) -> None:
        # Mix of phrase + non-phrase cues; phrase cues should be halved.
        cues = [
            Cue(time_s=1.0, type="phrase", source="auto"),
            Cue(time_s=5.0, type="phrase", source="auto"),
            Cue(time_s=10.0, type="phrase", source="auto"),
            Cue(time_s=15.0, type="phrase", source="auto"),
            Cue(time_s=20.0, type="drop", source="auto"),
        ]
        cache = _StaticCache(
            {
                bridge.player._state.current_track.path: DjMeta(
                    analysed=True, cues=cues, intro_end_s=0.0, outro_start_s=160.0
                )
            }
        )
        _attach_cache(bridge, cache)
        st = bridge.get_state()
        got = st["current_track"]["cues"]
        # Drop survives; phrases halved (kept indices 0 & 2 → 1.0, 10.0).
        types = [c["type"] for c in got]
        assert types.count("drop") == 1
        assert types.count("phrase") == 2

    def test_downbeats_empty_when_no_entry_length(self, bridge) -> None:
        bridge.player._state.current_track = _entry(length=0.0)
        bridge.player._dj_cache = None
        st = bridge.get_state()
        assert st["current_track"]["downbeats_outro"] == []
        assert st["current_track"]["downbeats_intro"] == []

    def test_downbeats_empty_when_no_grid_and_zero_bpm(self, bridge) -> None:
        # Track without bpm and without analysed beats -> downbeats empty.
        bridge.player._state.current_track = _entry(bpm=0.0)
        bridge.player._dj_cache = None
        st = bridge.get_state()
        assert st["current_track"]["downbeats_outro"] == []
        assert st["current_track"]["downbeats_intro"] == []


class TestActiveLyric:
    def test_active_lyric_idx_set_when_lyric_matches(self, bridge) -> None:
        from autodj.audio_meta import LyricLine

        bridge.player._current_lyrics = [
            LyricLine(time_s=0.0, text="a"),
            LyricLine(time_s=10.0, text="b"),
            LyricLine(time_s=60.0, text="c"),
        ]
        # 30 s elapsed → active is the second line
        bridge.player._playback_pos = [44100 * 30]
        bridge.player._current_sr = 44100
        st = bridge.get_state()
        assert st["lyric_index"] == 1
        assert st["lyric_text"] == "b"


class TestPlaybackChoiceValidation:
    @pytest.mark.parametrize(
        "field",
        ["transition_mode", "post_queue_seed", "key_notation", "liners_pick_mode"],
    )
    def test_bad_choice_raises_before_any_field_applies(self, bridge, field) -> None:
        pb = bridge.player._cfg.playback
        pb.crossfade_seconds = 3.0
        pb.liners_enabled = False
        with pytest.raises(ValueError, match=field):
            bridge.set_playback_settings(
                PlaybackSettingsBody(crossfade_seconds=9.0, liners_enabled=True, **{field: "bogus"})
            )
        assert pb.crossfade_seconds == pytest.approx(3.0)
        assert pb.liners_enabled is False


class TestApplyLiners:
    def test_liners_every_minutes_disable_when_zero(self, bridge) -> None:
        bridge.set_playback_settings(PlaybackSettingsBody(liners_every_minutes=5.0))
        assert bridge.player._cfg.playback.liners_every_minutes == 5.0
        bridge.set_playback_settings(PlaybackSettingsBody(liners_every_minutes=0))
        assert bridge.player._cfg.playback.liners_every_minutes is None

    def test_liners_random_min_max_minutes(self, bridge) -> None:
        bridge.set_playback_settings(
            PlaybackSettingsBody(
                liners_random_min_minutes=3.0,
                liners_random_max_minutes=10.0,
            )
        )
        assert bridge.player._cfg.playback.liners_random_min_minutes == 3.0
        assert bridge.player._cfg.playback.liners_random_max_minutes == 10.0
        bridge.set_playback_settings(PlaybackSettingsBody(liners_random_min_minutes=0))
        bridge.set_playback_settings(PlaybackSettingsBody(liners_random_max_minutes=0))
        assert bridge.player._cfg.playback.liners_random_min_minutes is None
        assert bridge.player._cfg.playback.liners_random_max_minutes is None


class TestQueueReorderPreQueueClear:
    def test_reorder_to_empty_clears_pre_queue_seed(self, bridge) -> None:
        # Set pre_queue mode, add a track to populate seed, then reorder to empty.
        bridge.player._cfg.playback.post_queue_seed = "pre_queue"
        bridge.queue_add(bridge.sim.entries[2].path)
        assert bridge.player._state.pre_queue_seed is not None
        # Reorder with an empty list (or path list that doesn't match anything)
        bridge.queue_reorder([])
        assert bridge.player._state.pre_queue_seed is None


class TestServerAudioSkip:
    def test_skip_uses_bus_when_present(self, bridge) -> None:
        from unittest.mock import MagicMock

        bridge.player._dry_run = False
        bridge.player.bus = MagicMock()
        bridge.skip()
        bridge.player.bus.skip.assert_called_once_with()
        bridge.player._skip_event.set.assert_not_called()

    def test_skip_without_bus_signals_the_audio_loop(self, bridge) -> None:
        bridge.player._dry_run = False
        bridge.player.bus = None
        bridge.skip()
        bridge.player._skip_event.set.assert_called_once_with()


class TestServerAudioUpNext:
    def test_bus_mode_announces_the_track_baked_into_the_playing_tail(self, bridge) -> None:
        from unittest.mock import MagicMock

        state = bridge.player._state
        upcoming, queued = _entry(path="Z:/m/up.flac"), _entry(path="Z:/m/q.flac")
        bridge.player.bus = MagicMock()
        bridge.player._playing_render = MagicMock(next_entry=upcoming)
        state.queued_next = queued
        bridge._sync_next_for_prefetch()
        assert state.next_track is upcoming

    def test_bus_mode_before_any_track_starts_leaves_up_next(self, bridge) -> None:
        from unittest.mock import MagicMock

        state = bridge.player._state
        before = state.next_track
        bridge.player.bus = MagicMock()
        bridge.player._playing_render = None
        bridge._sync_next_for_prefetch()
        assert state.next_track is before


def test_repeat_windows_resize_the_running_history(bridge) -> None:
    """A window changed in Settings has to reach the picker without a restart."""
    from types import SimpleNamespace

    from autodj.player import PlayerState

    state = PlayerState(no_repeat_window=10, artist_repeat_window=3)
    for i in range(5):
        state.record_played(_entry(path=f"/m/{i}.mp3", artist=f"Artist {i}"))
    bridge.player._state = state
    bridge.player._sim = SimpleNamespace(ntotal=100)

    bridge.set_playback_settings(PlaybackSettingsBody(no_repeat_window=2, artist_repeat_window=1))

    assert list(state.recently_played) == ["/m/3.mp3", "/m/4.mp3"]
    assert list(state.recently_played_artists) == ["artist 4"]
    settings = bridge.get_settings()["playback"]
    assert (settings["no_repeat_window"], settings["artist_repeat_window"]) == (2, 1)

    # Larger than the library: the picker keeps a tenth of it unplayed.
    bridge.set_playback_settings(PlaybackSettingsBody(no_repeat_window=500))
    assert state.recently_played.maxlen == 90
    assert bridge.get_settings()["playback"]["no_repeat_window"] == 500
