"""The player's side of DJ-style skips, liner talk-ups and the vocal guard."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from autodj._bridge import PlayerBridge
from autodj.audio_meta import LyricLine
from autodj.dj_meta import DjMeta
from autodj.mixbus import RenderedTrack
from autodj.player import Player, _BeatmatchPlan
from autodj.skip_fx import tail_frames
from tests.unit._fakes import make_cfg_mock, make_entry, make_sim_index

_SR = 44100


class _Cache:
    def __init__(self, stored: dict[str, DjMeta]) -> None:
        self.stored = stored

    def get(self, path: str) -> DjMeta:
        return self.stored.get(path, DjMeta(analysed=False))


def _player() -> Player:
    cfg = make_cfg_mock()
    cfg.playback.transition_mode = "fixed"
    return Player(cfg, make_sim_index(4))


def _lyrics(player: Player, by_path: dict[str, list[tuple[float, str]]]) -> MagicMock:
    reader = MagicMock(
        side_effect=lambda path: (
            [LyricLine(t, text) for t, text in by_path.get(str(path), [])],
            "",
        )
    )
    player._read_any_lyrics = reader  # type: ignore[method-assign]
    return reader


# ---------------------------------------------------------------------------
# Skip effects
# ---------------------------------------------------------------------------


class TestPlanSkip:
    @staticmethod
    def _track(player: Player, seconds: float = 20.0) -> RenderedTrack:
        entry = player._sim.entries[0]
        audio = np.full((int(seconds * _SR), 2), 0.25, np.float32)
        return RenderedTrack(entry, audio, None, 0, "", start_offset=_SR)

    def test_the_fade_style_has_no_effect(self) -> None:
        player = _player()
        assert player._plan_skip(self._track(player), 1000) is None

    @pytest.mark.parametrize("style", [MagicMock(), "wobble"])
    def test_an_unknown_style_falls_back_to_the_fade(self, style) -> None:
        player = _player()
        player._cfg.playback.skip_style = style
        assert player._plan_skip(self._track(player), 1000) is None

    def test_too_close_to_the_end_falls_back_to_the_fade(self) -> None:
        player = _player()
        player._cfg.playback.skip_style = "echo_out"
        track = self._track(player)
        assert player._plan_skip(track, len(track.audio) - 100) is None

    def test_a_trusted_tempo_ends_the_effect_on_a_beat_of_the_grid(self) -> None:
        player = _player()
        player._cfg.playback.skip_style = "loop_roll"
        track = self._track(player)
        beats = [0.25 + 0.5 * k for k in range(60)]  # 120 BPM, offset a quarter beat
        player._dj_cache = _Cache({track.entry.path: DjMeta(analysed=True, beats=beats)})  # type: ignore[assignment]
        pos = 5 * _SR + 123
        tail = player._plan_skip(track, pos)
        assert tail is not None and tail.track is track
        assert tail.start == pos + _SR // 10
        end_in_file = (tail.start + len(tail.audio) + track.start_offset) / _SR
        assert any(abs(end_in_file - b) < 1e-4 for b in beats)
        assert 0.5 <= len(tail.audio) / _SR < 1.0 + 1e-9

    def test_an_untrusted_tempo_uses_the_fixed_length(self) -> None:
        player = _player()
        player._cfg.playback.skip_style = "backspin"
        track = self._track(player)
        track.entry.tempo_confidence = 0.0
        tail = player._plan_skip(track, 5 * _SR)
        assert tail is not None
        assert len(tail.audio) == tail_frames(0, 0.0, [])

    def test_the_bus_is_given_the_planner(self) -> None:
        player = _player()
        bus = player._build_bus()
        assert bus._skip_tail_maker == player._plan_skip


# ---------------------------------------------------------------------------
# Lyric timing for the mix
# ---------------------------------------------------------------------------


class TestVocalTiming:
    def test_lyrics_are_read_once_per_track(self) -> None:
        player = _player()
        reader = _lyrics(player, {"a": [(4.0, "hi")]})
        assert player._sung_spans("a") == player._sung_spans("a") == ((4.0, 9.0),)
        assert reader.call_count == 1

    def test_the_mix_reads_lyrics_even_when_they_are_not_shown(self) -> None:
        player = _player()
        player._cfg.playback.show_lyrics = False
        with patch("autodj.audio_meta.load_lrc_for", return_value=[LyricLine(3.0, "x")]):
            assert player._read_lyrics_for_path("a") == ([], "")
            assert player._timed_lyrics("a") == [LyricLine(3.0, "x")]

    def test_a_lyric_read_failure_means_no_timing(self) -> None:
        player = _player()
        player._read_any_lyrics = MagicMock(side_effect=RuntimeError("tag"))  # type: ignore[method-assign]
        assert player._timed_lyrics("a") == []

    def test_vocal_start_prefers_lyrics_then_the_intro_end(self) -> None:
        player = _player()
        a, b, c = player._sim.entries[:3]
        _lyrics(player, {a.path: [(0.0, ""), (12.5, "words")]})
        player._dj_cache = _Cache(  # type: ignore[assignment]
            {
                b.path: DjMeta(analysed=True, intro_end_s=9.0),
                c.path: DjMeta(analysed=True, intro_end_s=0.0),
            }
        )
        assert player.vocal_start_s(a) == 12.5
        assert player.vocal_start_s(b) == 9.0
        assert player.vocal_start_s(c) is None


# ---------------------------------------------------------------------------
# The vocal-clash guard
# ---------------------------------------------------------------------------


class TestGuardVocals:
    @staticmethod
    def _guard(player: Player, fade_start_s: float = 110.0, ratio: float = 1.0):
        cur, nxt = player._sim.entries[:2]
        return player._guard_vocals(
            cur,
            nxt,
            None,
            _BeatmatchPlan(ratio, int(fade_start_s * _SR)),
            entry=0,
            crossfade=8 * _SR,
            earliest=0,
            length=180 * _SR,
        )

    def _sing(self, player: Player) -> None:
        cur, nxt = player._sim.entries[:2]
        _lyrics(
            player,
            {
                cur.path: [(104.0, "b"), (108.0, "c"), (113.0, "")],
                nxt.path: [(0.0, ""), (2.0, "hello")],
            },
        )

    def test_a_clash_shortens_the_fade(self) -> None:
        player = _player()
        self._sing(player)
        assert self._guard(player) == (110 * _SR, 2 * _SR)

    def test_a_beatmatch_stretch_moves_the_incoming_vocal(self) -> None:
        player = _player()
        self._sing(player)
        assert self._guard(player, ratio=1.05) == (110 * _SR, round(2.1 * _SR))

    def test_off_changes_nothing(self) -> None:
        player = _player()
        player._cfg.djmix.vocal_guard = False
        reader = _lyrics(player, {})
        assert self._guard(player) == (110 * _SR, 8 * _SR)
        reader.assert_not_called()

    def test_no_clash_changes_nothing(self) -> None:
        player = _player()
        cur, nxt = player._sim.entries[:2]
        _lyrics(player, {cur.path: [(100.0, "a"), (104.0, "")], nxt.path: [(1.0, "hi")]})
        assert self._guard(player) == (110 * _SR, 8 * _SR)

    def test_without_lyrics_on_one_side_nothing_changes(self) -> None:
        player = _player()
        cur = player._sim.entries[0]
        _lyrics(player, {cur.path: [(108.0, "c"), (113.0, "")]})
        assert self._guard(player) == (110 * _SR, 8 * _SR)

    def test_a_move_lands_on_whole_beats_of_the_grid(self) -> None:
        player = _player()
        cur, nxt = player._sim.entries[:2]
        _lyrics(
            player,
            {
                cur.path: [(108.0, "x"), (112.0, "y"), (117.0, "")],
                nxt.path: [(0.5, "now")],
            },
        )
        meta_a = DjMeta(analysed=True, beats=[0.5 * k for k in range(400)])
        start, fade = player._guard_vocals(
            cur,
            nxt,
            meta_a,
            _BeatmatchPlan(1.0, 110 * _SR),
            entry=0,
            crossfade=6 * _SR,
            earliest=109 * _SR,
            length=180 * _SR,
        )
        assert (start, fade) == (round(116.5 * _SR), 6 * _SR)

    def test_the_render_shortens_its_overlap(self) -> None:
        player = _player()
        player._cfg.playback.crossfade_seconds = 4.0
        cur, nxt = player._sim.entries[:2]
        music = np.full((20 * _SR, 2), 0.1, np.float32)
        # The fade runs 16-20 s; the outgoing line at 15 s runs to 20 s and
        # the incoming track sings 1.5 s after it enters.
        _lyrics(player, {cur.path: [(15.0, "long")], nxt.path: [(1.5, "in")]})
        with patch("autodj.player.load_stereo", return_value=music):
            rendered = player._render_track(cur, nxt, 0)
        assert rendered is not None
        assert rendered.overlap_frames == int(1.5 * _SR)
        assert rendered.next_entry_offset == 0
        assert rendered.next_start_offset == int(1.5 * _SR)
        assert len(rendered.audio) == 16 * _SR + int(1.5 * _SR)


# ---------------------------------------------------------------------------
# The bridge hands talk-ups the render that started
# ---------------------------------------------------------------------------


def test_the_liner_check_gets_the_render_that_started() -> None:
    entry = make_entry(1)
    player = MagicMock()
    player._playing_render = SimpleNamespace(entry=entry)
    bridge = PlayerBridge(player=player, sim=MagicMock())
    seen: list[object] = []
    done = threading.Event()
    scheduler = MagicMock()
    scheduler.on_track_start.side_effect = lambda track: (seen.append(track), done.set())
    bridge.attach_liner_scheduler(scheduler)
    try:
        bridge.on_track_started(entry)
        assert done.wait(2.0)
        assert seen == [player._playing_render]
        done.clear()
        bridge.on_track_started(make_entry(2))  # not the playing render's track
        assert done.wait(2.0)
        assert seen[-1] is None
    finally:
        bridge.shutdown_liner_worker()
