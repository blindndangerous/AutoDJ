"""The server mix's DJ features: effect planning, beat and key sync, imported
markers, and what the state says about how the playing track came in."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from autodj.dj_meta import Cue, DjMeta
from autodj.mixbus import RenderedTrack
from autodj.player import Player, PlayerState, _FxPlan
from autodj.transitions import _REAL_EFFECTS, AUTO_CATEGORIES, BeatSync, TransitionFx
from tests.unit._fakes import make_cfg_mock, make_sim_index

_SR = 44100


def _player(effect: str = "none", n: int = 12) -> Player:
    cfg = make_cfg_mock()
    cfg.transitions.effect = effect
    cfg.playback.beat_sync_fx = True
    cfg.playback.key_sync_fx = True
    cfg.playback.transition_mode = "fixed"
    return Player(cfg, make_sim_index(n))


class _Cache:
    """A DJ-meta cache held in a dict."""

    def __init__(self, stored: dict[str, DjMeta]) -> None:
        self.stored = stored
        self.sets: list[str] = []

    def get(self, path: str) -> DjMeta:
        return self.stored.get(path, DjMeta(analysed=False))

    def set(self, path: str, meta: DjMeta) -> None:
        self.sets.append(path)
        self.stored[path] = meta

    def flush(self, **_kw: object) -> None:
        pass


# ---------------------------------------------------------------------------
# planned_transition: one choice per pair, shared by the render and the state
# ---------------------------------------------------------------------------


class TestPlannedTransition:
    def test_a_pair_keeps_its_random_effect(self) -> None:
        player = _player("random")
        cur, nxt = player._sim.entries[:2]
        first = player.planned_transition(cur, nxt)
        assert first in {fx.value for fx in _REAL_EFFECTS}
        assert all(player.planned_transition(cur, nxt) == first for _ in range(20))

    def test_a_new_setting_chooses_again(self) -> None:
        player = _player("random")
        cur, nxt = player._sim.entries[:2]
        player.planned_transition(cur, nxt)
        player._cfg.transitions.effect = "echo_out"
        assert player.planned_transition(cur, nxt) == "echo_out"

    def test_rotate_moves_on_once_per_pair(self) -> None:
        player = _player("rotate")
        a, b, c = player._sim.entries[:3]
        first = TransitionFx(player.planned_transition(a, b))
        player.planned_transition(a, b)  # the state asking again
        second = TransitionFx(player.planned_transition(b, c))
        step = (_REAL_EFFECTS.index(second) - _REAL_EFFECTS.index(first)) % len(_REAL_EFFECTS)
        assert step == 1

    def test_auto_uses_the_pick_mode_of_the_first_call(self) -> None:
        player = _player("auto")
        cur, nxt = player._sim.entries[:2]
        chosen = player.planned_transition(cur, nxt, "discovery")
        assert TransitionFx(chosen) in AUTO_CATEGORIES["scene_change"]
        # The state does not know the pick mode; it still gets the same plan.
        assert player.planned_transition(cur, nxt) == chosen

    def test_unknown_setting_is_a_plain_fade(self) -> None:
        player = _player("wobble")
        assert player.planned_transition(*player._sim.entries[:2]) == "none"

    def test_only_a_few_plans_are_kept(self) -> None:
        player = _player("random")
        entries = player._sim.entries
        for i in range(11):
            player.planned_transition(entries[i], entries[i + 1])
        assert len(player._fx_plans) == Player._FX_PLANS_KEPT
        assert (str(entries[10].path), str(entries[11].path)) in player._fx_plans

    def test_next_rendered_plans_with_the_pick_mode(self) -> None:
        player = _player("auto")
        cur, nxt = player._sim.entries[:2]
        player._choose_next = lambda _c, _ctx: (nxt, "discovery")  # type: ignore[method-assign]
        player._render_track = lambda c, n, _o, **_k: RenderedTrack(  # type: ignore[method-assign]
            c, np.zeros((10, 2), np.float32), n, 0, player.planned_transition(c, n)
        )
        player._set_render_cursor(cur, 0, "seed")
        rendered = player._next_rendered()
        assert rendered is not None
        assert TransitionFx(rendered.transition_fx) in AUTO_CATEGORIES["scene_change"]


# ---------------------------------------------------------------------------
# _fx_plan and the downbeat: what the synced effects get from the tracks
# ---------------------------------------------------------------------------


class TestFxPlan:
    def test_trusted_tempo_and_keys_are_passed_on(self) -> None:
        from autodj.beat_sync import key_to_hz

        player = _player("echo_out")
        cur, nxt = player._sim.entries[:2]
        nxt.key, nxt.mode = 7, 1
        beats = [i * 0.5 for i in range(40)]
        plan = player._fx_plan(cur, nxt, DjMeta(analysed=True, beats=beats), _SR * 3)
        assert plan.name == "echo_out"
        assert plan.sync == BeatSync(bpm=120.0, out_root_hz=key_to_hz(0), in_root_hz=key_to_hz(7))
        assert plan.downbeats == tuple(beats[::4])
        assert plan.origin_s == pytest.approx(3.0)

    def test_untrusted_tempo_keeps_only_the_keys(self) -> None:
        player = _player("echo_out")
        cur, nxt = player._sim.entries[:2]
        cur.tempo_confidence = 0.1
        plan = player._fx_plan(cur, nxt, DjMeta(analysed=True, beats=[0.0, 0.5]), 0)
        assert plan.sync is not None
        assert plan.sync.bpm == 0.0
        assert plan.downbeats == ()

    def test_sync_settings_off_give_no_sync(self) -> None:
        player = _player("echo_out")
        player._cfg.playback.beat_sync_fx = False
        player._cfg.playback.key_sync_fx = False
        plan = player._fx_plan(*player._sim.entries[:2], None, 0)
        assert plan == _FxPlan("echo_out")

    def test_beat_grid_comes_from_the_cache_without_marker_modes(self) -> None:
        player = _player("gate_stutter")
        cur, nxt = player._sim.entries[:2]
        player._dj_cache = _Cache(
            {cur.path: DjMeta(analysed=True, beats=[1.0, 1.5, 2.0, 2.5, 3.0])}
        )
        plan = player._fx_plan(cur, nxt, None, 0)
        assert plan.downbeats == (1.0, 3.0)

    def test_effect_starts_on_the_downbeat_of_its_tail(self) -> None:
        player = _player("gate_stutter")
        audio = np.full((4 * _SR, 2), 0.25, np.float32)
        plan = _FxPlan(
            "gate_stutter", BeatSync(bpm=120.0), downbeats=(10.0, 12.3, 14.3), origin_s=10.0
        )
        seen: list[BeatSync | None] = []

        def spy(tail, head, _sr, _fx, *, seed, sync):
            seen.append(sync)
            return tail, head, np.zeros((0, 2), np.float32)

        with patch("autodj.transitions.apply_transition", spy):
            *_, name = player._apply_transition_effect(
                audio, audio, audio[: 2 * _SR], _SR, 2 * _SR, plan=plan
            )
        assert name == "gate_stutter"
        # The 2 s tail starts 12 s into the track; the next downbeat is at 12.3 s.
        assert seen[0] is not None
        assert seen[0].downbeat_s == pytest.approx(0.3)

    def test_render_uses_the_plan_and_the_tempo(self) -> None:
        player = _player("auto")
        cur, nxt = player._sim.entries[:2]
        seen: list[BeatSync | None] = []

        def spy(tail, head, _sr, _fx, *, seed, sync):
            seen.append(sync)
            return tail, head, np.zeros((0, 2), np.float32)

        music = np.full((10 * _SR, 2), 0.1, np.float32)
        with (
            patch("autodj.player.load_stereo", return_value=music),
            patch("autodj.transitions.apply_transition", spy),
            patch("autodj.transitions.choose_auto_effect", return_value=TransitionFx.ECHO_OUT),
        ):
            rendered = player._render_track(cur, nxt, 0)
        assert rendered is not None
        assert rendered.transition_fx == "echo_out" == player.planned_transition(cur, nxt)
        assert seen[0] is not None and seen[0].bpm == 120.0


# ---------------------------------------------------------------------------
# The playing track's beatmatch ratio and effect describe how IT came in
# ---------------------------------------------------------------------------


def _bare_player() -> Player:
    player = Player.__new__(Player)
    player._state = PlayerState()
    player._set_generation = 0
    player._cfg = MagicMock()
    player._set_render_cursor(None, 0, "seed")
    return player


class TestMixedIn:
    def test_next_render_carries_the_stretch_it_came_in_with(self) -> None:
        player = _bare_player()
        first, second, third = MagicMock(), MagicMock(), MagicMock()
        picks = iter([second, third])
        player._choose_next = lambda _c, _ctx: (next(picks), "similarity")  # type: ignore[method-assign]
        ratios = {id(first): ("echo_out", 1.05), id(second): ("none", 0.97)}

        def render(current, nxt, _offset, **_kw):
            fx, ratio = ratios[id(current)]
            return RenderedTrack(current, np.zeros((10, 2), np.float32), nxt, 5, fx, ratio)

        player._render_track = render  # type: ignore[method-assign]
        player._set_render_cursor(first, 0, "seed")
        a = player._next_rendered()
        b = player._next_rendered()
        assert a is not None and b is not None
        assert (a.mixed_in_fx, a.mixed_in_ratio) == ("", 1.0)  # the set's first track
        assert (b.mixed_in_fx, b.mixed_in_ratio) == ("echo_out", 1.05)
        assert player._pending_mixed_in == ("none", 0.97)

    def test_no_overlap_means_no_stretch(self) -> None:
        player = _bare_player()
        first, second = MagicMock(), MagicMock()
        player._choose_next = lambda _c, _ctx: (second, "similarity")  # type: ignore[method-assign]
        player._render_track = lambda c, n, _o, **_k: RenderedTrack(  # type: ignore[method-assign]
            c, np.zeros((10, 2), np.float32), n, 0, "", 1.2
        )
        player._set_render_cursor(first, 0, "seed")
        player._next_rendered()
        assert player._pending_mixed_in == ("", 1.0)

    def test_track_start_reports_how_the_track_came_in(self) -> None:
        player = Player(make_cfg_mock(), make_sim_index(3))
        player.load_lyrics_in_background = MagicMock()  # type: ignore[method-assign]
        a, b, c = player._sim.entries
        # a's tail stretched b by 1.04; b's own tail will not stretch c.
        player._on_track_start(
            RenderedTrack(a, np.zeros((10, 2), np.float32), b, 0, "echo_out", 1.04)
        )
        assert player._beatmatch_ratio == 1.0
        player._on_track_start(
            RenderedTrack(
                b,
                np.zeros((10, 2), np.float32),
                c,
                0,
                "none",
                1.0,
                mixed_in_fx="echo_out",
                mixed_in_ratio=1.04,
            )
        )
        assert player._beatmatch_ratio == pytest.approx(1.04)
        assert player._last_transition_fx == "echo_out"

    def test_cursor_moves_keep_the_mixed_in_data(self) -> None:
        player = _bare_player()
        entry, nxt = MagicMock(), MagicMock()
        track = RenderedTrack(
            entry,
            np.zeros((10, 2), np.float32),
            nxt,
            3,
            "tape_stop",
            1.03,
            mixed_in_fx="echo_out",
            mixed_in_ratio=0.98,
        )
        player._rewind_render_cursor(track)
        assert player._pending_mixed_in == ("echo_out", 0.98)
        player._skip_past_render(track)
        assert player._pending_mixed_in == ("tape_stop", 1.03)
        player._set_render_cursor(entry, 0, "queue")  # a jump or a new set
        assert player._pending_mixed_in == ("", 1.0)


# ---------------------------------------------------------------------------
# Imported intro and outro markers
# ---------------------------------------------------------------------------


class TestImportedMarkers:
    def test_marked_intro_start_sets_where_the_track_comes_in(self) -> None:
        player = _player()
        player._cfg.playback.transition_mode = "full_intro_outro"
        audio = np.full((10 * _SR, 2), 0.2, np.float32)  # sound from the start
        meta = DjMeta(analysed=True, cues=[Cue(1.5, "intro_start", source="mixxx")])
        skipped = player._skip_incoming_silence_samples(audio, _SR, meta_b=meta)
        assert skipped == int(1.5 * _SR)
        # Without a marker the first sound is measured: here at once.
        assert player._skip_incoming_silence_samples(audio, _SR, meta_b=DjMeta()) == 0

    def test_stored_track_gets_its_markers_once_a_session(self) -> None:
        player = _player()
        nxt = player._sim.entries[1]
        old = DjMeta(
            intro_end_s=6.0,
            outro_start_s=170.0,
            analysed=True,
            cues=[Cue(4.0, "first_downbeat", source="mixxx")],
        )
        cache = _Cache({nxt.path: old})
        player._dj_cache = cache  # type: ignore[assignment]
        player._library_cues = {
            nxt.path: [
                Cue(4.0, "intro_start", source="mixxx"),
                Cue(20.0, "intro_end", source="mixxx"),
            ]
        }
        meta = player._peek_incoming_meta(nxt)
        assert meta is not None
        assert (meta.intro_start_s, meta.intro_end_s) == (4.0, 20.0)
        assert cache.stored[nxt.path] is meta
        player._peek_incoming_meta(nxt)
        assert cache.sets == [nxt.path]  # not merged again

    def test_unchanged_or_unlisted_tracks_are_left_alone(self) -> None:
        player = _player()
        cur, nxt = player._sim.entries[:2]
        cues = [Cue(4.0, "intro_start", source="mixxx")]
        current = DjMeta(intro_start_s=4.0, intro_end_s=8.0, analysed=True, cues=list(cues))
        cache = _Cache({cur.path: current, nxt.path: DjMeta(analysed=True)})
        player._dj_cache = cache  # type: ignore[assignment]
        player._library_cues = {cur.path: cues}
        assert player._peek_incoming_meta(cur) is current
        assert player._peek_incoming_meta(nxt) is not None
        assert cache.sets == []

    def test_library_read_later_moves_the_mix_points(self, monkeypatch, tmp_path: Path) -> None:
        track = str(tmp_path / "early.flac")
        player = _player()
        player._cfg.playback.import_external_cues = True
        cache = _Cache({})
        player._dj_cache = cache  # type: ignore[assignment]
        monkeypatch.setattr(
            "autodj.dj_meta.analyse_audio",
            lambda _a, _sr: DjMeta(analysed=True, intro_end_s=6.0, outro_start_s=150.0),
        )
        monkeypatch.setattr(
            "autodj.dj_cues_import.auto_import_cues",
            lambda **_kw: {track: [Cue(170.0, "outro_start", source="traktor")]},
        )
        player._analyse(np.zeros(4410, dtype=np.float32), _SR, track)
        player.load_library_cues()
        assert cache.stored[track].outro_start_s == 170.0
