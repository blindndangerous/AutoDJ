"""Tempo- and key-synced transition effects in the server mix.

With a :class:`~autodj.transitions.BeatSync`, the rhythmic effects take
their timing from the beat and start on the downbeat, and the tonal
layers follow the keys.  Without one, or with an untrusted tempo, every
effect renders exactly as before (the golden fingerprints cover that).
"""

from __future__ import annotations

from functools import partial

import numpy as np
import pytest

from autodj.transitions import (
    _LAYER_LEVELS,
    META_MODES,
    BeatSync,
    TransitionFx,
    _beat_repeat,
    _synced_layer,
    _synced_tail_effect,
    apply_transition,
    trusted_bpm,
)

_SR = 8000


def _music(seconds: float, seed: int = 1, sr: int = _SR) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * sr)) / sr
    audio = sum(0.3 * np.sin(2 * np.pi * f * t + rng.uniform(0, 6)) for f in (55, 110, 220))
    return (0.4 * (audio + 0.05 * rng.standard_normal(len(t)))).astype(np.float32)


def _gate_cycle(out: np.ndarray) -> float:
    """Median samples between the starts of a gate's open stretches."""
    opened = np.abs(out) > 1e-6
    starts = np.flatnonzero(opened[1:] & ~opened[:-1]) + 1
    return float(np.median(np.diff(starts)))


class TestTrustedBpm:
    def test_confident_tempo_is_kept(self) -> None:
        assert trusted_bpm(128.0, 0.8) == 128.0

    @pytest.mark.parametrize(
        ("bpm", "confidence"),
        [
            (128.0, 0.3),  # beat tracking unsure
            (0.0, 0.9),  # unknown tempo
            (-5.0, 0.9),
            (300.0, 0.9),  # outside 40-250
            (float("nan"), 0.9),
            ("128", 0.9),  # not a number
            (128.0, None),
        ],
    )
    def test_doubtful_tempo_is_dropped(self, bpm: object, confidence: object) -> None:
        assert trusted_bpm(bpm, confidence) == 0.0


class TestBeatSync:
    def test_beat_length_follows_the_tempo(self) -> None:
        assert BeatSync(bpm=120.0).beat_s == pytest.approx(0.5)
        assert BeatSync().beat_s is None

    def test_one_known_key_stands_for_both(self) -> None:
        assert BeatSync(out_root_hz=440.0).roots == (440.0, 440.0)
        assert BeatSync(in_root_hz=330.0).roots == (330.0, 330.0)
        assert BeatSync(out_root_hz=440.0, in_root_hz=330.0).roots == (440.0, 330.0)
        assert BeatSync(bpm=120.0).roots is None


class TestTimingFollowsTheTempo:
    @pytest.mark.parametrize("bpm", [90.0, 120.0, 174.0])
    def test_echo_repeats_on_eighth_notes(self, bpm: float) -> None:
        fn = _synced_tail_effect(TransitionFx.ECHO_OUT, BeatSync(bpm=bpm))
        assert isinstance(fn, partial)
        assert fn.keywords["delay_ms"] == pytest.approx(30_000.0 / bpm)

    @pytest.mark.parametrize("bpm", [60.0, 120.0, 200.0])
    def test_dub_delay_repeats_on_quarter_notes(self, bpm: float) -> None:
        fn = _synced_tail_effect(TransitionFx.DUB_DELAY, BeatSync(bpm=bpm))
        assert isinstance(fn, partial)
        assert fn.keywords["delay_ms"] == pytest.approx(60_000.0 / bpm)
        assert fn.keywords["damping_hz"] == 1500.0  # still the dark dub repeats

    def test_echo_audio_repeats_after_an_eighth_note(self) -> None:
        tail = np.zeros(_SR * 2, dtype=np.float32)
        tail[_SR // 2] = 1.0  # one click, past the 250 ms seam
        fn = _synced_tail_effect(TransitionFx.ECHO_OUT, BeatSync(bpm=100.0))
        out = fn(tail, _SR)
        echo = int(_SR * 0.3)  # an eighth note at 100 BPM
        assert abs(out[_SR // 2 + echo]) > 0.1
        assert np.all(np.abs(out[_SR // 2 + 1 : _SR // 2 + echo]) < 1e-6)

    @pytest.mark.parametrize("bpm", [96.0, 120.0, 150.0])
    def test_gate_steps_in_sixteenth_notes(self, bpm: float) -> None:
        tail = np.full(_SR * 4, 0.5, dtype=np.float32)
        out, _head, _layer = apply_transition(
            tail, tail.copy(), _SR, TransitionFx.GATE_STUTTER, sync=BeatSync(bpm=bpm)
        )
        assert _gate_cycle(out) == pytest.approx(_SR * 60.0 / bpm / 4, abs=1.5)

    def test_transformer_steps_in_sixteenth_notes(self) -> None:
        fn = _synced_tail_effect(TransitionFx.TRANSFORMER, BeatSync(bpm=128.0))
        assert fn.keywords["rate_hz"] == pytest.approx(128.0 / 60 * 4)

    def test_stutter_build_runs_from_quarter_to_thirty_second_notes(self) -> None:
        fn = _synced_tail_effect(TransitionFx.STUTTER_BUILD, BeatSync(bpm=120.0))
        assert (fn.keywords["start_hz"], fn.keywords["end_hz"]) == pytest.approx((2.0, 16.0))

    def test_pump_ducks_on_every_beat(self) -> None:
        fn = _synced_tail_effect(TransitionFx.SIDECHAIN_PUMP, BeatSync(bpm=128.0))
        assert fn.keywords["bpm"] == pytest.approx(128.0)

    def test_loop_and_scratch_slices_follow_the_beat(self) -> None:
        repeat = _synced_tail_effect(TransitionFx.BEAT_REPEAT, BeatSync(bpm=120.0))
        assert repeat.keywords == {"slice_ms": pytest.approx(250.0), "repeats": None}
        scratch = _synced_tail_effect(TransitionFx.SCRATCH, BeatSync(bpm=120.0))
        assert scratch.keywords["slice_ms"] == pytest.approx(500.0)

    def test_beat_repeat_back_to_back(self) -> None:
        tail = np.full(_SR, 0.5, dtype=np.float32)
        out = _beat_repeat(tail, _SR, slice_ms=125.0, repeats=None)
        # Eight 125 ms slices fill the second without gaps longer than a fade.
        assert np.count_nonzero(out == 0.0) < len(out) // 20


class TestFallback:
    @pytest.mark.parametrize(
        "effect",
        [
            TransitionFx.ECHO_OUT,
            TransitionFx.GATE_STUTTER,
            TransitionFx.SIDECHAIN_PUMP,
            TransitionFx.AIR_HORN,
            TransitionFx.RING_MODULATOR,
        ],
    )
    def test_unknown_tempo_and_keys_render_as_before(self, effect: TransitionFx) -> None:
        tail, head = _music(1.0, 1), _music(1.0, 2)
        plain = apply_transition(tail, head, _SR, effect, seed=3)
        empty = apply_transition(tail, head, _SR, effect, seed=3, sync=BeatSync(downbeat_s=0.2))
        for got, want in zip(empty, plain, strict=True):
            np.testing.assert_array_equal(got, want)

    def test_untrusted_tempo_keeps_the_fixed_gate(self) -> None:
        # trusted_bpm drops a low-confidence tempo, so the sync carries 0.0.
        sync = BeatSync(bpm=trusted_bpm(96.0, 0.2))
        tail = np.full(_SR * 2, 0.5, dtype=np.float32)
        out, _h, _l = apply_transition(tail, tail.copy(), _SR, TransitionFx.GATE_STUTTER, sync=sync)
        assert _gate_cycle(out) == pytest.approx(_SR / 8.0, abs=1.5)  # the fixed 8 Hz


class TestDownbeatStart:
    def test_gate_leaves_the_audio_before_the_downbeat_alone(self) -> None:
        tail = np.full(_SR * 2, 0.5, dtype=np.float32)
        sync = BeatSync(bpm=120.0, downbeat_s=0.4)
        out, _h, _l = apply_transition(tail, tail.copy(), _SR, TransitionFx.GATE_STUTTER, sync=sync)
        dry = int(0.4 * _SR)
        np.testing.assert_allclose(out[:dry], tail[:dry], atol=1e-6)
        # The first gate cycle starts on the downbeat: open (after its
        # 64-sample fade-in), then shut at half a sixteenth note.
        step = int(_SR * 0.125)
        assert out[dry + 100] == pytest.approx(0.5, abs=1e-6)
        assert out[dry + step // 2 + 10] == 0.0

    def test_downbeat_past_half_the_tail_is_ignored(self) -> None:
        tail = np.full(_SR, 0.5, dtype=np.float32)
        late = apply_transition(
            tail, tail.copy(), _SR, TransitionFx.GATE_STUTTER, sync=BeatSync(120.0, downbeat_s=0.8)
        )
        now = apply_transition(
            tail, tail.copy(), _SR, TransitionFx.GATE_STUTTER, sync=BeatSync(120.0)
        )
        np.testing.assert_array_equal(late[0], now[0])

    def test_echo_does_not_wait_for_the_downbeat(self) -> None:
        tail = _music(1.0)
        a = apply_transition(tail, tail, _SR, TransitionFx.ECHO_OUT, sync=BeatSync(120.0))
        b = apply_transition(
            tail, tail, _SR, TransitionFx.ECHO_OUT, sync=BeatSync(120.0, downbeat_s=0.3)
        )
        np.testing.assert_array_equal(a[0], b[0])


class TestKeySync:
    def test_air_horn_sweeps_from_the_outgoing_to_the_incoming_root(self) -> None:
        layer = _synced_layer(TransitionFx.AIR_HORN, BeatSync(out_root_hz=440.0, in_root_hz=330.0))
        assert layer.keywords == {"start_hz": 220.0, "end_hz": 660.0}

    def test_dub_siren_rises_two_octaves_over_the_incoming_root(self) -> None:
        layer = _synced_layer(TransitionFx.DUB_SIREN, BeatSync(out_root_hz=262.0))
        assert layer.keywords == {"start_hz": 262.0, "end_hz": 1048.0}

    def test_ring_modulator_sits_an_octave_under_the_outgoing_root(self) -> None:
        fn = _synced_tail_effect(TransitionFx.RING_MODULATOR, BeatSync(out_root_hz=392.0))
        assert fn.keywords["carrier_hz"] == pytest.approx(196.0)

    @staticmethod
    def _pitch(layer: np.ndarray, sr: int, start_s: float, end_s: float) -> float:
        window = layer[int(start_s * sr) : int(end_s * sr)]
        spectrum = np.abs(np.fft.rfft(window * np.hanning(len(window))))
        return float(np.argmax(spectrum) * sr / len(window))

    def test_tuned_horn_is_audibly_lower_in_a_low_key(self) -> None:
        sr = 22050
        tail = _music(2.0, sr=sr)
        sync = BeatSync(out_root_hz=330.0, in_root_hz=330.0)  # E: 165 -> 660 Hz
        _t, _h, tuned = apply_transition(tail, tail, sr, TransitionFx.AIR_HORN, sync=sync)
        _t, _h, fixed = apply_transition(tail, tail, sr, TransitionFx.AIR_HORN)
        # Over 0.1-0.3 s the tuned horn glides 190-240 Hz, the fixed one 253-319 Hz.
        assert 180.0 <= self._pitch(tuned, sr, 0.1, 0.3) <= 245.0
        assert 245.0 <= self._pitch(fixed, sr, 0.1, 0.3) <= 325.0


_EFFECTS = [fx for fx in TransitionFx if fx not in META_MODES]


class TestInvariants:
    """Synced effects keep the length and level guarantees of apply_transition."""

    @pytest.mark.parametrize("effect", _EFFECTS, ids=str)
    def test_lengths_and_levels_hold(self, effect: TransitionFx) -> None:
        sr = 22050
        tail = np.stack([_music(1.0, 1, sr), _music(1.0, 3, sr) * 0.8], axis=1)
        head = np.stack([_music(1.0, 2, sr), _music(1.0, 4, sr) * 0.9], axis=1)
        sync = BeatSync(bpm=128.0, out_root_hz=293.7, in_root_hz=349.2, downbeat_s=0.1)
        out_tail, out_head, layer = apply_transition(tail, head, sr, effect, seed=5, sync=sync)
        assert out_tail.shape == tail.shape
        assert out_head.shape == head.shape
        assert np.abs(out_tail).max() <= np.abs(tail).max() + 1e-6
        assert np.abs(out_head).max() <= np.abs(head).max() + 1e-6
        if len(layer):
            assert layer.shape[1] == 2
            music_peak = max(np.abs(tail).max(), np.abs(head).max())
            assert np.abs(layer).max() <= _LAYER_LEVELS[effect] * music_peak + 1e-6
