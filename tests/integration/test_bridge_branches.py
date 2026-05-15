"""Branch-coverage tests targeting autodj._bridge.PlayerBridge.

These exercise the small helper functions inside ``get_state``
(``_markers``, ``_cues``, ``_downbeats``) plus the per-setting
``_apply_*`` branches that the broader integration tests do not
explicitly hit.
"""

from __future__ import annotations

import pytest

from autodj.dj_meta import Cue, DjMeta
from autodj.indexer import IndexEntry


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
        assert st["current_track"]["intro_end_s"] == 4.0
        assert st["current_track"]["outro_start_s"] == 170.0
        # outro_len = 180 - 170 = 10
        assert st["current_track"]["outro_len"] == pytest.approx(10.0)

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

    def test_downbeats_synthesised_when_grid_too_sparse(self, bridge) -> None:
        # No analysed meta → empty beats → synth from bpm
        bridge.player._dj_cache = None
        st = bridge.get_state()
        # bpm 120 → synthesised grid present.
        assert isinstance(st["current_track"]["downbeats_outro"], list)


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


class TestCoverArt:
    def test_cover_art_for_returns_data_and_mime(self, bridge, monkeypatch) -> None:
        from autodj import _bridge as _br

        class _Art:
            data = b"PNG-bytes"
            mime_type = "image/png"

        monkeypatch.setattr("autodj.audio_meta.read_cover_art", lambda p: _Art())
        result = bridge.cover_art_for("Z:/anything.flac")
        assert result is not None
        data, mime = result
        assert data == b"PNG-bytes"
        assert mime == "image/png"
        _ = _br  # silence unused

    def test_cover_art_for_returns_none(self, bridge, monkeypatch) -> None:
        monkeypatch.setattr("autodj.audio_meta.read_cover_art", lambda p: None)
        assert bridge.cover_art_for("Z:/nope.flac") is None


class TestApplyKeyPreferFlats:
    def test_key_prefer_flats_path(self, bridge) -> None:
        bridge.player._cfg.playback.key_prefer_flats = False
        bridge.set_playback_settings(key_prefer_flats=True)
        assert bridge.player._cfg.playback.key_prefer_flats is True


class TestApplySessionEnvelopeExtras:
    def test_beatmatch_on_skip_apply(self, bridge) -> None:
        bridge.player._cfg.playback.beatmatch_on_skip = False
        bridge.set_playback_settings(beatmatch_on_skip=True)
        assert bridge.player._cfg.playback.beatmatch_on_skip is True


class TestApplyLiners:
    def test_liners_folder_set_and_clear(self, bridge) -> None:
        bridge.set_playback_settings(liners_folder="some/dir")
        assert bridge.player._cfg.playback.liners_folder == "some/dir"
        # Empty string → folder cleared to None.
        bridge.set_playback_settings(liners_folder="")
        assert bridge.player._cfg.playback.liners_folder is None

    def test_liners_every_minutes_disable_when_zero(self, bridge) -> None:
        bridge.set_playback_settings(liners_every_minutes=5.0)
        assert bridge.player._cfg.playback.liners_every_minutes == 5.0
        bridge.set_playback_settings(liners_every_minutes=0)
        assert bridge.player._cfg.playback.liners_every_minutes is None

    def test_liners_random_min_max_minutes(self, bridge) -> None:
        bridge.set_playback_settings(
            liners_random_min_minutes=3.0,
            liners_random_max_minutes=10.0,
        )
        assert bridge.player._cfg.playback.liners_random_min_minutes == 3.0
        assert bridge.player._cfg.playback.liners_random_max_minutes == 10.0
        bridge.set_playback_settings(liners_random_min_minutes=0)
        bridge.set_playback_settings(liners_random_max_minutes=0)
        assert bridge.player._cfg.playback.liners_random_min_minutes is None
        assert bridge.player._cfg.playback.liners_random_max_minutes is None


class TestCapturePreQueueSeedSkip:
    def test_capture_skipped_when_queue_already_has_items(self, bridge) -> None:
        # pre_queue_seed is None, but queue already populated (manually).
        # Capture must early-return at line 668 without setting the seed.
        bridge.player._cfg.playback.post_queue_seed = "pre_queue"
        bridge.player._state.queue.append(bridge.sim.entries[0])
        bridge.player._state.pre_queue_seed = None
        # Force the helper to run via queue_add of yet another entry.
        bridge.queue_add(bridge.sim.entries[2].path)
        # Capture skipped (line 668), seed remains None.
        assert bridge.player._state.pre_queue_seed is None


class TestQueueReorderPreQueueClear:
    def test_reorder_to_empty_clears_pre_queue_seed(self, bridge) -> None:
        # Set pre_queue mode, add a track to populate seed, then reorder to empty.
        bridge.player._cfg.playback.post_queue_seed = "pre_queue"
        bridge.queue_add(bridge.sim.entries[2].path)
        assert bridge.player._state.pre_queue_seed is not None
        # Reorder with an empty list (or path list that doesn't match anything)
        bridge.queue_reorder([])
        assert bridge.player._state.pre_queue_seed is None
