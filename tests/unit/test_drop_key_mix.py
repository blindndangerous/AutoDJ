"""Drop-to-drop mixing and harmonic key shift in the server mix."""

from __future__ import annotations

from collections.abc import Sequence
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from autodj.beatmatch import HOP, KEY_RAMP_FRAMES, Glide, _read, drop_landing
from autodj.config import DjMixConfig
from autodj.dj_meta import (
    Cue,
    DjMeta,
    confident_drop,
    key_shift_semitones,
    mix_anchors,
    mix_drops,
)
from autodj.mixbus import RenderedTrack
from autodj.player import Player, _BeatmatchPlan
from autodj.transitions import auto_category
from tests.unit._fakes import make_cfg_mock, make_sim_index

SR = 44100


def _grid(bpm: float, start: float, seconds: float) -> list[float]:
    period = 60.0 / bpm
    return [start + k * period for k in range(int((seconds - start) / period))]


def _pitch(audio: np.ndarray, at: int, n: int = 8192) -> float:
    """The strongest frequency in *audio* from sample *at* (FFT, parabolic peak)."""
    seg = audio[at : at + n, 0] * np.hanning(n)
    spec = np.abs(np.fft.rfft(seg, 8 * n))
    k = int(np.argmax(spec))
    a, b, c = np.log(spec[k - 1 : k + 2] + 1e-12)
    return (k + 0.5 * (a - c) / (a - 2 * b + c)) * SR / (8 * n)


def _tone(seconds: float, hz: float = 440.0) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return np.repeat((0.3 * np.sin(2 * np.pi * hz * t))[:, None], 2, axis=1).astype(np.float32)


def _rms(audio: np.ndarray, at: int, n: int = 4096) -> float:
    return float(np.sqrt(np.mean(audio[at : at + n, 0] ** 2)))


# ---------------------------------------------------------------------------
# Which shift makes two keys mix
# ---------------------------------------------------------------------------


class TestKeyShiftSemitones:
    def test_keys_that_mix_need_no_shift(self) -> None:
        assert key_shift_semitones(9, 0, 9, 0) == 0  # same key
        assert key_shift_semitones(9, 0, 0, 1) == 0  # relative major
        assert key_shift_semitones(9, 0, 4, 0) == 0  # one step round the wheel

    def test_smallest_shift_down_first(self) -> None:
        # A minor against A# minor: down a semitone is A minor itself.
        assert key_shift_semitones(9, 0, 10, 0) == -1
        # A minor against G# minor: up a semitone.
        assert key_shift_semitones(9, 0, 8, 0) == 1
        # A minor against B minor: two semitones down.
        assert key_shift_semitones(9, 0, 11, 0) == -2

    def test_harmonic_mode_pair_is_not_undone(self) -> None:
        # Energy boost picks two semitones up on purpose.
        assert key_shift_semitones(9, 0, 11, 0, "energy_boost") == 0

    def test_unknown_or_too_far(self) -> None:
        assert key_shift_semitones(-1, 0, 10, 0) == 0
        assert key_shift_semitones(9, 0, 10, -1) == 0
        # F# major against A minor needs six semitones.
        assert key_shift_semitones(9, 0, 6, 1) == 0


class TestAutoEffectSeesTheShift:
    def test_shifted_pair_is_no_clash(self) -> None:
        out = MagicMock(bpm=120.0, tempo_confidence=0.9, key=9, mode=0, energy=0.1)
        inc = MagicMock(bpm=120.0, tempo_confidence=0.9, key=10, mode=0, energy=0.1)
        assert auto_category(out, inc) == "disguise"
        assert auto_category(out, inc, key_shift=-1) == "clean"

    def test_planned_transition_passes_the_shift(self) -> None:
        cfg = make_cfg_mock()
        cfg.transitions.effect = "auto"
        cfg.djmix.key_shift = True
        player = Player(cfg, make_sim_index(3))
        cur, nxt = player._sim.entries[0], player._sim.entries[1]
        cur.key, cur.mode, nxt.key, nxt.mode = 9, 0, 10, 0
        with patch("autodj.transitions.choose_auto_effect") as choose:
            choose.return_value.value = "none"
            player.planned_transition(cur, nxt)
        assert choose.call_args.kwargs["key_shift"] == -1
        # Turning it off plans the pair again, unshifted.
        cfg.djmix.key_shift = False
        with patch("autodj.transitions.choose_auto_effect") as choose:
            choose.return_value.value = "none"
            player.planned_transition(cur, nxt)
        assert choose.call_args.kwargs["key_shift"] == 0


# ---------------------------------------------------------------------------
# Drops and landings
# ---------------------------------------------------------------------------


def _drop_audio(drop_s: float, seconds: float, *, quiet: float = 0.02) -> np.ndarray:
    """Mono: a quiet build-up, then from *drop_s* a loud held level."""
    rng = np.random.default_rng(5)
    out = quiet * rng.standard_normal(int(seconds * SR)).astype(np.float32)
    at = round(drop_s * SR)
    out[at:] *= 0.5 / quiet
    return out


class TestMixDrops:
    def test_hand_set_drops_win(self) -> None:
        cues = [
            Cue(30.0, "drop", source="auto"),
            Cue(42.0, "user", label="Drop 2", source="rekordbox"),
            Cue(12.0, "drop", source="user"),
            Cue(50.0, "intro_start", label="drop", source="mixxx"),
        ]
        assert mix_drops(cues, np.zeros(SR * 60, np.float32), SR, []) == [12.0, 42.0]

    def test_clear_detected_drop_counts_on_its_beat(self) -> None:
        beats = _grid(120, 0.25, 40)
        drop = beats[40]  # 20.25 s, half way through a cue block
        audio = _drop_audio(drop, 40)
        cue = Cue(round(drop * 2) / 2, "drop", source="auto")
        assert mix_drops([cue], audio, SR, beats) == [pytest.approx(drop)]
        assert confident_drop(audio, SR, cue.time_s, beats) == pytest.approx(drop)

    def test_unclear_detected_drop_does_not(self) -> None:
        beats = _grid(120, 0.25, 40)
        drop = beats[40]
        # The build-up is only 12 dB down: the detector may call it a
        # drop, but not a clear one.
        soft = _drop_audio(drop, 40, quiet=0.125)
        assert mix_drops([Cue(20.0, "drop", source="auto")], soft, SR, beats) == []
        # A single loud hit is not a drop either.
        hit = _drop_audio(drop, 40)
        hit[round((drop + 1.0) * SR) :] = 0.01
        assert confident_drop(hit, SR, 20.0, beats) is None
        # Nor is one with no beat near it.
        assert confident_drop(_drop_audio(drop, 40), SR, 20.0, []) is None
        assert confident_drop(np.zeros(SR, np.float32), SR, 30.0, beats) is None

    def test_anchors_add_hand_set_breakdowns(self) -> None:
        cues = [
            Cue(10.0, "drop", source="serato"),
            Cue(70.0, "breakdown", source="auto"),
            Cue(90.0, "user", label="Breakdown", source="traktor"),
        ]
        assert mix_anchors(cues, np.zeros(SR, np.float32), SR, []) == [10.0, 90.0]


class TestDropLanding:
    _BEATS: Sequence[float] = _grid(120, 0.25, 120)

    def _land(self, **overrides: object) -> float | None:
        args: dict = {
            "phrase_beats": 32,
            "overlap_s": 6.0,
            "target_s": 100.0,
            "earliest_s": 60.0,
            "latest_s": 118.0,
        }
        args.update(overrides)
        return drop_landing(self._BEATS, [self._BEATS[16]], **args)

    def test_whole_phrases_after_the_anchor_nearest_the_usual_end(self) -> None:
        # The anchor is beat 16 (8.25 s); phrases of 32 beats (16 s) later
        # land at 24.25, 40.25, ... 104.25 s.
        assert self._land() == pytest.approx(104.25)
        assert self._land(target_s=94.0) == pytest.approx(88.25)

    def test_out_of_bounds(self) -> None:
        assert self._land(earliest_s=105.0) is None
        assert self._land(latest_s=70.0, earliest_s=0.0) == pytest.approx(56.25)
        # The overlap may not start before the anchor.
        assert self._land(earliest_s=0.0, latest_s=30.0, overlap_s=20.0) is None
        # An anchor off the grid, or no grid at all, gives nothing.
        assert (
            drop_landing(
                self._BEATS,
                [8.5],
                phrase_beats=32,
                overlap_s=6.0,
                target_s=100.0,
                earliest_s=0.0,
                latest_s=118.0,
            )
            is None
        )
        assert (
            drop_landing(
                [1.0],
                [1.0],
                phrase_beats=32,
                overlap_s=6.0,
                target_s=100.0,
                earliest_s=0.0,
                latest_s=118.0,
            )
            is None
        )


# ---------------------------------------------------------------------------
# The key shift in the vocoder
# ---------------------------------------------------------------------------


class TestKeyShiftGlide:
    @pytest.mark.parametrize(("semitones", "ratio"), [(1, 1.0), (-2, 1.04), (2, 0.97)])
    def test_pitch_during_the_overlap_and_after_the_glide(
        self, semitones: int, ratio: float
    ) -> None:
        audio = _tone(60)
        glide = Glide(
            entry=SR,
            ratio=ratio,
            played=6 * SR,
            ramp_frames=round(6 * SR / HOP),
            semitones=semitones,
        )
        out = glide.head(audio, glide.cut + 2 * SR)
        shifted = 440.0 * 2.0 ** (semitones / 12.0)
        assert _pitch(out, SR) == pytest.approx(shifted, abs=0.5)
        assert _pitch(out, glide.played - 8192) == pytest.approx(shifted, abs=0.5)
        # Back at its own key once the glide is over.
        assert _pitch(out, glide.cut) == pytest.approx(440.0, abs=0.5)
        # The level holds all the way (no phasing dip while the key slides).
        levels = [_rms(out, at) for at in range(0, glide.cut, SR // 2)]
        assert min(levels) > 0.95 * 0.3 / np.sqrt(2)
        # The pitch slides back steadily.
        slide = [_pitch(out, at, 4096) for at in range(glide.played, glide.cut - 4096, SR)]
        steps = np.diff(slide) * np.sign(semitones)
        assert np.all(steps < 0.5)

    def test_continuity_and_return_to_the_source(self) -> None:
        rng = np.random.default_rng(2)
        audio = (_tone(40) + 0.05 * rng.standard_normal((40 * SR, 2))).astype(np.float32)
        glide = Glide(entry=SR + 123, ratio=1.03, played=3 * SR, ramp_frames=50, semitones=-1)
        whole = glide.head(audio, glide.cut)
        joined = np.concatenate([glide.head(audio, glide.played), glide.continuation(audio)])
        np.testing.assert_allclose(joined, whole, atol=1e-6)
        end = glide.source_end
        np.testing.assert_allclose(whole[-256:], audio[end - 256 : end], atol=1e-5)
        assert glide.origin == end - (glide.cut - glide.played)

    def test_short_glides_are_lengthened(self) -> None:
        glide = Glide(entry=0, ratio=1.0, played=SR, ramp_frames=0, semitones=1)
        _, steady_from = glide._steps()
        assert steady_from == -(-SR // HOP) + KEY_RAMP_FRAMES

    def test_reader_returns_whole_samples_and_silence_outside(self) -> None:
        audio = np.arange(1, 101, dtype=np.float32)
        np.testing.assert_allclose(
            _read(audio, np.array([-3.0, 0.0, 41.0, 99.0, 105.0]), np.ones(5)),
            [0.0, 1.0, 42.0, 100.0, 0.0],
            atol=1e-5,
        )


# ---------------------------------------------------------------------------
# The server mix
# ---------------------------------------------------------------------------


def _player(**djmix: object) -> tuple[Player, MagicMock, MagicMock]:
    cfg = make_cfg_mock()
    cfg.playback.transition_mode = "fixed"
    cfg.playback.crossfade_seconds = 6.0
    cfg.playback.beat_sync_fx = False
    cfg.playback.key_sync_fx = False
    cfg.djmix.beatmatch = True
    cfg.djmix.beatmatch_glide_bars = 4
    cfg.djmix.drop_mix = True
    for name, value in djmix.items():
        setattr(cfg.djmix, name, value)
    player = Player(cfg, make_sim_index(3))
    cur, nxt = player._sim.entries[0], player._sim.entries[1]
    cur.bpm, nxt.bpm = 120.0, 123.0
    return player, cur, nxt


_A_BEATS = _grid(120, 0.25, 59.5)
_B_BEATS = _grid(123, 0.5, 59.5)
_B_DROP = _B_BEATS[40]  # 20.01 s


def _incoming(drop_s: float = _B_DROP, quiet: float = 0.03) -> np.ndarray:
    """Quiet clicks on every beat, then from the drop loud ones."""
    out = np.zeros((60 * SR, 2), np.float32)
    t = np.arange(int(0.03 * SR)) / SR
    ping = np.sin(2 * np.pi * 1000.0 * t) * np.exp(-t / 0.008)
    for beat in _B_BEATS:
        at = round(beat * SR)
        level = 0.9 if beat >= drop_s - 1e-6 else quiet
        out[at : at + len(ping)] += (level * ping)[:, None]
    return out


def _render(
    player: Player,
    cur: MagicMock,
    nxt: MagicMock,
    *,
    a_cues: Sequence[Cue] = (Cue(_A_BEATS[16], "user", label="Drop", source="rekordbox"),),
    b_cues: Sequence[Cue] = (Cue(_B_DROP, "drop", source="user"),),
    incoming: np.ndarray | None = None,
    outgoing_s: float = 60.0,
) -> tuple[RenderedTrack, RenderedTrack, np.ndarray]:
    audio = {
        str(cur.path): np.zeros((int(outgoing_s * SR), 2), np.float32),
        str(nxt.path): _incoming() if incoming is None else incoming,
    }
    meta = {
        str(cur.path): DjMeta(
            beats=[b for b in _A_BEATS if b < outgoing_s], analysed=True, cues=list(a_cues)
        ),
        str(nxt.path): DjMeta(beats=_B_BEATS, analysed=True, cues=list(b_cues)),
    }
    player._peek_incoming_meta = lambda e: meta[str(e.path)]  # type: ignore[method-assign]
    with patch("autodj.player.load_stereo", side_effect=lambda p, *_a: audio[p].copy()):
        first = player._render_track(cur, nxt, 0)
        assert first is not None
        second = player._render_track(nxt, None, first.next_start_offset, glide_in=first.next_glide)
    assert second is not None
    return first, second, audio[str(nxt.path)]


def _onset(audio: np.ndarray, at: int, window_s: float = 0.1) -> float:
    """Milliseconds from *at* to the first sample over 30 % of the window's peak."""
    half = int(window_s * SR)
    seg = np.abs(audio[at - half : at + half, 0])
    return float(np.argmax(seg > 0.3 * seg.max()) - half) / SR * 1000.0


class TestDropMix:
    def test_incoming_drop_lands_on_the_planned_downbeat(self) -> None:
        player, cur, nxt = _player()
        first, second, _source = _render(player, cur, nxt)
        # A whole phrase (32 beats) after the outgoing drop at beat 16,
        # nearest the usual end of the fade (60 s): beat 112, 56.25 s.
        landing = _A_BEATS[16 + 3 * 32]
        assert len(first.audio) == round(landing * SR)
        assert first.next_glide is not None
        assert first.beatmatch_ratio == pytest.approx(123 / 120)
        stream = np.concatenate([first.audio, second.audio])
        assert abs(_onset(stream, len(first.audio))) < 3.0
        # The overlap kept its length and the incoming track entered 6 s
        # (stretched) before its drop.
        assert first.next_glide.played == 6 * SR
        assert first.next_glide.entry == round(_B_DROP * SR) - round(6 * SR / (123 / 120))
        # The beats after the drop stay on the outgoing grid's tempo in the overlap
        # and on the incoming track's own beats once the glide is over.
        late = [b for b in _B_BEATS if b * SR > first.next_glide.source_end + SR][:3]
        for beat in late:
            onset = _onset(second.audio, round(beat * SR) - second.start_offset)
            assert abs(onset) < 0.1

    def test_auto_effect_becomes_a_riser_that_ends_on_the_drop(self) -> None:
        player, cur, nxt = _player()
        player._cfg.transitions.effect = "none"
        plain, *_ = _render(player, cur, nxt)
        player._cfg.transitions.effect = "auto"
        risen, *_ = _render(player, cur, nxt)
        assert risen.transition_fx == "noise_riser"
        assert player.planned_transition(cur, nxt) == "noise_riser"
        layer = risen.audio - plain.audio
        assert len(layer) == len(plain.audio)
        assert _rms(layer, len(layer) - 4096) > 2.0 * _rms(layer, len(layer) - 3 * SR)
        assert not np.any(layer[: len(layer) - 6 * SR])

    def test_an_effect_chosen_by_name_is_kept(self) -> None:
        player, cur, nxt = _player()
        player._cfg.transitions.effect = "echo_out"
        first, *_ = _render(player, cur, nxt)
        assert first.transition_fx == "echo_out"

    @pytest.mark.parametrize(
        "case",
        [
            "beatmatch_off",
            "no_incoming_drop",
            "unclear_auto_drop",
            "drop_too_late",
            "build_up_too_short",
            "no_outgoing_anchor",
            "cuts_too_much",
            "untrusted_tempo",
        ],
    )
    def test_falls_back_to_the_usual_mix(self, case: str) -> None:
        player, cur, nxt = _player()
        kwargs: dict = {}
        if case == "beatmatch_off":
            player._cfg.djmix.beatmatch = False
        elif case == "no_incoming_drop":
            kwargs["b_cues"] = ()
        elif case == "unclear_auto_drop":
            # Only 10 dB louder than the build-up.
            kwargs["b_cues"] = (Cue(_B_DROP, "drop", source="auto"),)
            kwargs["incoming"] = _incoming(quiet=0.3)
        elif case == "drop_too_late":
            late = _B_BEATS[-2]
            kwargs["b_cues"] = (Cue(late, "drop", source="user"),)
            kwargs["incoming"] = _incoming(late)
            player._DROP_MAX_ENTRY_S = 30.0  # type: ignore[misc]
        elif case == "build_up_too_short":
            early = _B_BEATS[4]  # 2.5 s in
            kwargs["b_cues"] = (Cue(early, "drop", source="user"),)
            kwargs["incoming"] = _incoming(early)
        elif case == "no_outgoing_anchor":
            kwargs["a_cues"] = (Cue(_A_BEATS[16], "breakdown", source="auto"),)
        elif case == "cuts_too_much":
            # The only landing a phrase after the drop is 24.25 s, but the
            # fade would end at 60 s.
            kwargs["a_cues"] = (Cue(_A_BEATS[16], "drop", source="user"),)
            player._cfg.djmix.phrase_bars = 30
        else:
            nxt.tempo_confidence = 0.1
        mixed, *_ = _render(player, cur, nxt, **kwargs)
        player._cfg.djmix.drop_mix = False
        usual, *_ = _render(player, cur, nxt, **kwargs)
        assert len(mixed.audio) == len(usual.audio)
        assert mixed.next_start_offset == usual.next_start_offset
        np.testing.assert_array_equal(mixed.audio, usual.audio)

    def test_clear_detected_drop_is_used(self) -> None:
        player, cur, nxt = _player()
        # The detector's block starts up to half a second before the drop.
        cue = Cue(np.floor(_B_DROP * 2) / 2, "drop", source="auto")
        first, second, _source = _render(player, cur, nxt, b_cues=(cue,))
        assert len(first.audio) == round(_A_BEATS[112] * SR)
        stream = np.concatenate([first.audio, second.audio])
        assert abs(_onset(stream, len(first.audio))) < 3.0

    def test_short_build_up_shortens_the_overlap(self) -> None:
        player, cur, nxt = _player()
        early = _B_BEATS[10]  # 5.4 s in: 5.5 s of build-up once stretched
        first, second, _source = _render(
            player, cur, nxt, b_cues=(Cue(early, "drop", source="user"),), incoming=_incoming(early)
        )
        assert first.next_glide is not None
        assert first.next_glide.entry == 0
        assert first.next_glide.played == int(round(early * SR) * (123 / 120))
        stream = np.concatenate([first.audio, second.audio])
        assert abs(_onset(stream, len(first.audio))) < 3.0


class TestKeyShiftMix:
    def _pair(
        self, *, beatmatch: bool, key_shift: bool = True
    ) -> tuple[RenderedTrack, RenderedTrack]:
        player, cur, nxt = _player(beatmatch=beatmatch, key_shift=key_shift, drop_mix=False)
        cur.key, cur.mode, nxt.key, nxt.mode = 9, 0, 10, 0
        if not beatmatch:
            cur.bpm = nxt.bpm = 120.0
        audio = {
            str(cur.path): np.zeros((30 * SR, 2), np.float32),
            str(nxt.path): _tone(60),
        }
        meta = DjMeta(beats=_grid(120, 0.25, 59.5), analysed=True)
        player._peek_incoming_meta = lambda _e: meta  # type: ignore[method-assign]
        with patch("autodj.player.load_stereo", side_effect=lambda p, *_a: audio[p].copy()):
            first = player._render_track(cur, nxt, 0)
            assert first is not None
            second = player._render_track(
                nxt, None, first.next_start_offset, glide_in=first.next_glide
            )
        assert second is not None
        return first, second

    @pytest.mark.parametrize("beatmatch", [True, False])
    def test_overlap_is_shifted_and_the_track_returns_to_its_key(self, beatmatch: bool) -> None:
        first, second = self._pair(beatmatch=beatmatch)
        glide = first.next_glide
        assert glide is not None and glide.semitones == -1
        # A# minor moved down a semitone mixes as A minor with the outgoing A minor.
        overlap_end = len(first.audio)
        assert _pitch(first.audio, overlap_end - 3 * SR) == pytest.approx(415.30, abs=0.5)
        after = glide.cut - glide.played + SR
        assert _pitch(second.audio, after) == pytest.approx(440.0, abs=0.5)
        # The render boundary is seamless.
        source = _tone(60)
        np.testing.assert_allclose(
            second.audio[:2000], glide.head(source, glide.played + 2000)[glide.played :], atol=1e-5
        )

    def test_off_or_compatible_leaves_the_key_alone(self) -> None:
        first, _second = self._pair(beatmatch=False, key_shift=False)
        assert first.next_glide is None
        assert _pitch(first.audio, len(first.audio) - 3 * SR) == pytest.approx(440.0, abs=0.5)

    def test_too_short_for_a_key_glide(self) -> None:
        plan = _BeatmatchPlan(1.0, 0, semitones=1)
        short = np.zeros((6 * SR, 2), np.float32)
        assert Player._incoming_glide(short, 0, plan, 3 * SR) is None
        stretched = _BeatmatchPlan(1.04, 0, semitones=1)
        glide = Player._incoming_glide(short, 0, stretched, 3 * SR)
        assert glide is not None and glide.semitones == 0 and glide.ratio == 1.04


class TestSettings:
    def test_defaults_off_and_strict_booleans(self) -> None:
        cfg = DjMixConfig()
        assert cfg.drop_mix is False and cfg.key_shift is False
        assert DjMixConfig.from_dict({"drop_mix": True, "key_shift": True}).key_shift is True
        with pytest.raises(TypeError, match="drop_mix"):
            DjMixConfig.from_dict({"drop_mix": "yes"})
        with pytest.raises(TypeError, match="key_shift"):
            DjMixConfig.from_dict({"key_shift": 1})


def test_both_options_on_change_nothing_when_they_do_not_apply() -> None:
    """Keys that already mix and tracks without drops render exactly as with both off."""
    renders = []
    for on in (False, True):
        cfg = make_cfg_mock()
        cfg.playback.transition_mode = "fixed_skip_silence"
        cfg.playback.beat_sync_fx = False
        cfg.playback.key_sync_fx = False
        cfg.djmix.beatmatch = True
        cfg.djmix.drop_mix = on
        cfg.djmix.key_shift = on
        player = Player(cfg, make_sim_index(3))
        cur, nxt = player._sim.entries[0], player._sim.entries[1]
        cur.bpm, nxt.bpm = 120.0, 123.0
        rng = np.random.default_rng(7)
        audio = {
            str(cur.path): (0.2 * rng.standard_normal((20 * SR, 2))).astype(np.float32),
            str(nxt.path): (0.2 * rng.standard_normal((40 * SR, 2))).astype(np.float32),
        }
        metas = {
            str(cur.path): DjMeta(beats=_grid(120, 0.25, 19.5), analysed=True),
            str(nxt.path): DjMeta(beats=_grid(123, 0.5, 39.5), analysed=True),
        }
        player._peek_incoming_meta = lambda e, m=metas: m[str(e.path)]  # type: ignore[method-assign]
        with patch("autodj.player.load_stereo", side_effect=lambda p, *_a, a=audio: a[p].copy()):
            renders.append(player._render_track(cur, nxt, 0))
    off, on_ = renders
    assert off is not None and on_ is not None
    assert off.next_glide is not None and off.next_glide == on_.next_glide
    np.testing.assert_array_equal(off.audio, on_.audio)


def test_unknown_keys_and_missing_analysis_fall_back() -> None:
    player, cur, nxt = _player(key_shift=True)
    cur.key, cur.mode = 9, 0
    nxt.key = MagicMock()  # not a key at all
    assert player._key_shift(cur, nxt) == 0
    player._peek_incoming_meta = lambda _e: None  # type: ignore[method-assign]
    silent = np.zeros((10 * SR, 2), np.float32)
    meta_b = DjMeta(beats=_B_BEATS, analysed=True, cues=[Cue(_B_DROP, "drop", source="user")])
    assert (
        player._drop_mix(
            cur,
            nxt,
            None,
            meta_b,
            silent,
            silent,
            entry=0,
            fade_start=4 * SR,
            crossfade=6 * SR,
            earliest=0,
        )
        is None
    )
