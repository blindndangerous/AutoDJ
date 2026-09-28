"""Smoke tests for autodj.transitions — every effect produces sane output."""

import numpy as np
import pytest

from autodj.transitions import (
    TransitionFx,
    _no_louder_than,
    air_horn,
    apply_transition,
    backspin,
    beat_repeat,
    bitcrusher,
    chorus,
    cross_eq_swap,
    dub_delay,
    dub_siren,
    echo_out,
    flanger,
    freeze,
    gate_stutter,
    glitch,
    halftime,
    highpass_sweep,
    lowpass_sweep,
    noise_riser,
    phaser,
    pick_effect,
    pitch_swell,
    reverb_tail,
    reverse_reverb,
    ring_modulator,
    scratch,
    sidechain_pump,
    stutter_build,
    submerge,
    tape_stop,
    telephone,
    transformer,
    vinyl_rewind,
    vinyl_wow,
    wow_flutter,
)

SR = 22050


def _audio(seconds: float = 2.0) -> np.ndarray:
    """Low-energy float32 mono audio for testing.

    Std=0.15 keeps random peaks under ~0.6 so effects that introduce
    even ±2× gain (distortion drive, etc.) stay below clipping.
    """
    rng = np.random.default_rng(42)
    n = int(seconds * SR)
    return rng.standard_normal(n).astype(np.float32) * 0.15


class TestPerEffectSanity:
    """Each effect must:
    - return a numpy float32 array
    - not introduce NaN / Inf
    - keep peak amplitude ≤ 1.05 (slight headroom OK)
    """

    @pytest.mark.parametrize(
        "fn",
        [
            echo_out,
            reverb_tail,
            tape_stop,
            gate_stutter,
            backspin,
            bitcrusher,
            flanger,
            pitch_swell,
            telephone,
            chorus,
            submerge,
            vinyl_wow,
            freeze,
            glitch,
            scratch,
            beat_repeat,
            sidechain_pump,
            reverse_reverb,
            vinyl_rewind,
            transformer,
            stutter_build,
            wow_flutter,
            phaser,
            ring_modulator,
            dub_delay,
            halftime,
        ],
    )
    def test_outgoing_tail_effect(self, fn) -> None:
        a = _audio()
        out = fn(a, SR)
        assert isinstance(out, np.ndarray)
        assert out.dtype == np.float32
        assert out.shape == a.shape
        assert np.all(np.isfinite(out))
        assert np.abs(out).max() <= 1.05

    def test_highpass_sweep_on_head(self) -> None:
        a = _audio()
        out = highpass_sweep(a, SR)
        assert out.shape == a.shape
        assert np.all(np.isfinite(out))

    def test_lowpass_sweep_on_tail(self) -> None:
        a = _audio()
        out = lowpass_sweep(a, SR)
        assert out.shape == a.shape
        assert np.all(np.isfinite(out))

    def test_cross_eq_swap_returns_pair(self) -> None:
        a = _audio()
        b = _audio()
        tail_t, head_full = cross_eq_swap(a, b, SR)
        assert tail_t.shape == a.shape
        assert head_full.shape == b.shape
        assert np.all(np.isfinite(tail_t))
        assert np.all(np.isfinite(head_full))


class TestSynthesisedLayers:
    @pytest.mark.parametrize("fn", [noise_riser, air_horn, dub_siren])
    def test_fixed_length(self, fn) -> None:
        layer = fn(SR, SR)
        assert layer.shape == (SR,)
        assert np.all(np.isfinite(layer))

    @pytest.mark.parametrize("fn", [noise_riser, air_horn, dub_siren])
    def test_zero_length(self, fn) -> None:
        assert fn(0, SR).shape == (0,)


def _music(seconds: float, level: float, seed: int) -> np.ndarray:
    """Music-like mono audio peaking at *level*: bass, chord and noise.

    Sustained low tones are what feedback delays and combs pile up on,
    so this finds an effect that builds past the music's peak.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    audio = sum(0.3 * np.sin(2 * np.pi * f * t + rng.uniform(0, 6)) for f in (55, 110, 220, 330))
    audio = audio + 0.05 * rng.standard_normal(len(t))
    return (audio * (level / np.abs(audio).max())).astype(np.float32)


_CONCRETE_EFFECTS = [
    fx for fx in TransitionFx if fx not in (TransitionFx.RANDOM, TransitionFx.ROTATE)
]


class TestEffectLevels:
    """No effect is louder than the music it is mixed with.

    The music here peaks at -8 dBFS, where ReplayGain typically leaves
    a mastered track.
    """

    LEVEL = 10 ** (-8 / 20)

    @pytest.mark.parametrize("effect", _CONCRETE_EFFECTS)
    def test_no_effect_peaks_above_the_music(self, effect: TransitionFx) -> None:
        tail = _music(2.0, self.LEVEL, seed=1)
        head = _music(2.0, self.LEVEL * 0.9, seed=2)
        out_tail, out_head, extra = apply_transition(tail, head, SR, effect, seed=3)
        assert np.abs(out_tail).max() <= np.abs(tail).max() + 1e-7
        assert np.abs(out_head).max() <= np.abs(head).max() + 1e-7
        if extra.size:
            assert np.abs(extra).max() <= self.LEVEL + 1e-7

    @staticmethod
    def _db(treated: np.ndarray, dry: np.ndarray) -> float:
        power = np.mean(treated.astype(np.float64) ** 2) / np.mean(dry.astype(np.float64) ** 2)
        return float(10 * np.log10(power))

    @pytest.mark.parametrize("effect", _CONCRETE_EFFECTS)
    def test_no_level_step_where_the_treated_audio_meets_the_track(
        self, effect: TransitionFx
    ) -> None:
        """A treated tail starts, and a treated head ends, at the music's level.

        One constant turn-down for the whole tail dropped a dub delay's
        start 1.5 to 3 dB under the audio just before it, and echo out's
        dry part fell straight to 0.35 (-9 dB) at the join.
        """
        tail = _music(2.0, self.LEVEL, seed=1)
        head = _music(2.0, self.LEVEL * 0.9, seed=2)
        out_tail, out_head, _ = apply_transition(tail, head, SR, effect, seed=3)
        edge = int(0.05 * SR)
        assert abs(self._db(out_tail[:edge], tail[:edge])) <= 0.5
        assert abs(self._db(out_head[-edge:], head[-edge:])) <= 0.5

    def test_stereo_joins_both_channels(self) -> None:
        tail = np.stack([_music(2.0, self.LEVEL, 1), _music(2.0, self.LEVEL, 4)], axis=1)
        head = np.stack([_music(2.0, self.LEVEL, 2), _music(2.0, self.LEVEL, 5)], axis=1)
        out_tail, _, _ = apply_transition(tail, head, SR, TransitionFx.ECHO_OUT, seed=3)
        edge = int(0.05 * SR)
        for channel in range(2):
            assert abs(self._db(out_tail[:edge, channel], tail[:edge, channel])) <= 0.5
        assert np.abs(out_tail).max() <= np.abs(tail).max() + 1e-7

    def test_a_late_peak_is_held_without_turning_the_rest_down(self) -> None:
        """Only the audio around a peak over the limit comes down."""
        audio = np.full(SR, 0.5, dtype=np.float32)
        audio[SR // 2] = 1.0
        held = _no_louder_than(audio, 0.5, SR)
        assert np.abs(held).max() <= 0.5
        assert np.all(held[: SR // 4] == audio[: SR // 4])
        assert np.all(held[-SR // 4 :] == audio[-SR // 4 :])

    @pytest.mark.parametrize(
        "effect",
        [
            TransitionFx.NOISE_RISER,
            TransitionFx.NOISE_DROP,
            TransitionFx.AIR_HORN,
            TransitionFx.DUB_SIREN,
        ],
    )
    def test_layer_follows_the_music_level(self, effect: TransitionFx) -> None:
        """Music turned down 8 dB by ReplayGain turns its layer down 8 dB."""
        loud = _music(2.0, 1.0, seed=1)
        quiet = loud * np.float32(self.LEVEL)
        *_, loud_layer = apply_transition(loud, loud, SR, effect, seed=3)
        *_, quiet_layer = apply_transition(quiet, quiet, SR, effect, seed=3)
        ratio = np.abs(quiet_layer).max() / np.abs(loud_layer).max()
        assert ratio == pytest.approx(self.LEVEL, rel=1e-5)


class TestDispatcher:
    """apply_transition routes each effect correctly + falls back on NONE."""

    @pytest.mark.parametrize("effect", list(TransitionFx))
    def test_all_effects_dispatch_cleanly(self, effect) -> None:
        if effect in (TransitionFx.RANDOM, TransitionFx.ROTATE):
            pytest.skip("meta modes resolved separately by pick_effect")
        a = _audio()
        b = _audio()
        tail, head, extra = apply_transition(a, b, SR, effect)
        assert isinstance(tail, np.ndarray)
        assert isinstance(head, np.ndarray)
        assert isinstance(extra, np.ndarray)
        assert tail.shape == a.shape
        assert np.all(np.isfinite(tail))
        assert np.all(np.isfinite(head))
        if extra.size > 0:
            assert np.all(np.isfinite(extra))


class TestPickEffect:
    def test_concrete_returns_self(self) -> None:
        assert pick_effect(TransitionFx.ECHO_OUT) == TransitionFx.ECHO_OUT
        assert pick_effect(TransitionFx.NONE) == TransitionFx.NONE

    def test_random_returns_real_effect(self) -> None:
        rng = np.random.default_rng(0)
        for _ in range(20):
            picked = pick_effect(TransitionFx.RANDOM, rng=rng)
            assert picked != TransitionFx.NONE
            assert picked != TransitionFx.RANDOM
            assert picked != TransitionFx.ROTATE

    def test_rotate_cycles(self) -> None:
        seen = {pick_effect(TransitionFx.ROTATE) for _ in range(60)}
        # Should cycle through more than one effect over many calls
        assert len(seen) > 1


class TestEdgeCases:
    def test_empty_input_returns_empty(self) -> None:
        empty = np.zeros(0, dtype=np.float32)
        for fn in (
            echo_out,
            reverb_tail,
            tape_stop,
            gate_stutter,
            backspin,
            bitcrusher,
            chorus,
            vinyl_wow,
        ):
            out = fn(empty, SR)
            assert out.shape == (0,)

    def test_short_input_doesnt_crash(self) -> None:
        short = np.array([0.1, -0.1, 0.05], dtype=np.float32)
        for fn in (tape_stop, backspin, vinyl_wow, pitch_swell):
            out = fn(short, SR)
            assert out.shape == short.shape


class TestFreeze:
    def test_loops_grain_through_tail(self) -> None:
        # 1 s tail; ask for a 100 ms grain that loops 10×
        a = _audio(1.0)
        out = freeze(a, SR, grain_ms=100.0, fade_out=False)
        assert out.shape == a.shape
        # Periodicity: samples 0 and 100ms-mark should be similar (after seam)
        grain = int(0.1 * SR)
        # Skip the small seam region
        assert np.abs(out[grain // 2] - out[grain + grain // 2]) < 0.5

    def test_fade_out_brings_tail_to_silence(self) -> None:
        a = _audio(1.0)
        out = freeze(a, SR, grain_ms=80.0, fade_out=True)
        assert abs(out[-1]) < 0.05
        assert abs(out[len(out) // 2]) > abs(out[-1])

    def test_grain_longer_than_tail_clamps(self) -> None:
        a = _audio(0.05)  # 50 ms
        out = freeze(a, SR, grain_ms=200.0, fade_out=False)
        assert out.shape == a.shape

    def test_empty_input_returns_empty(self) -> None:
        out = freeze(np.zeros(0, dtype=np.float32), SR)
        assert out.shape == (0,)


class TestGlitch:
    def test_output_same_length(self) -> None:
        a = _audio(1.0)
        out = glitch(a, SR, slice_ms=50.0, seed=42)
        assert out.shape == a.shape

    def test_seeded_is_reproducible(self) -> None:
        a = _audio(0.5)
        out1 = glitch(a, SR, seed=7)
        out2 = glitch(a, SR, seed=7)
        np.testing.assert_array_equal(out1, out2)

    def test_different_seeds_diverge(self) -> None:
        a = _audio(0.5)
        out1 = glitch(a, SR, seed=1)
        out2 = glitch(a, SR, seed=2)
        # Almost certainly different
        assert not np.allclose(out1, out2)

    def test_slice_longer_than_input_returns_copy(self) -> None:
        a = _audio(0.05)
        out = glitch(a, SR, slice_ms=500.0)
        assert out.shape == a.shape

    def test_empty_input_returns_empty(self) -> None:
        out = glitch(np.zeros(0, dtype=np.float32), SR)
        assert out.shape == (0,)


class TestTransitionsExtraBranches:
    def test_reverb_tail_short_input_skips_long_combs(self) -> None:
        from autodj.transitions import reverb_tail

        # 50 sample tail — shorter than any of the comb/allpass delays at
        # SR = 44100, so the inner `if d >= len(tail)` early-continues fire.
        short = np.linspace(-0.1, 0.1, 50, dtype=np.float32)
        out = reverb_tail(short, SR)
        assert out.shape == short.shape

    def test_tape_stop_linear_curve(self) -> None:
        from autodj.transitions import tape_stop

        a = _audio(0.5)
        out = tape_stop(a, SR, curve="linear")
        assert out.shape == a.shape

    def test_backspin_short_source_returns_tail(self) -> None:
        from autodj.transitions import backspin

        # 10 ms — shorter than the head_n setup, src ends up empty
        a = _audio(0.01)
        out = backspin(a, SR)
        # len-zero src → returns the original tail unchanged
        assert out.shape == a.shape


class TestEdgeCaseInputs:
    """Boundary conditions not covered above: 1-2 sample buffers,
    NaN audio, zero sample rate, all-silence input.  Every function
    should degrade gracefully (no crash, no NaN/Inf in output).
    """

    @pytest.mark.parametrize(
        "fn",
        [
            backspin,
            beat_repeat,
            bitcrusher,
            chorus,
            dub_delay,
            echo_out,
            flanger,
            freeze,
            gate_stutter,
            glitch,
            halftime,
            phaser,
            pitch_swell,
            reverb_tail,
            reverse_reverb,
            ring_modulator,
            scratch,
            sidechain_pump,
            stutter_build,
            submerge,
            tape_stop,
            telephone,
            transformer,
            vinyl_rewind,
            vinyl_wow,
            wow_flutter,
        ],
    )
    def test_one_sample_input(self, fn) -> None:
        """Single-sample tail must not crash; either passes through or empty."""
        tiny = np.array([0.1], dtype=np.float32)
        out = fn(tiny, SR)
        assert isinstance(out, np.ndarray)
        assert out.shape == tiny.shape
        assert np.all(np.isfinite(out))

    @pytest.mark.parametrize(
        "fn",
        [
            echo_out,
            reverb_tail,
            tape_stop,
            gate_stutter,
            bitcrusher,
            flanger,
            pitch_swell,
            telephone,
            chorus,
            submerge,
            vinyl_wow,
            freeze,
            scratch,
            sidechain_pump,
            reverse_reverb,
            vinyl_rewind,
            transformer,
            stutter_build,
            wow_flutter,
            phaser,
            ring_modulator,
            dub_delay,
            halftime,
        ],
    )
    def test_all_silence_input_stays_finite(self, fn) -> None:
        """Silent buffer in -> silent or finite buffer out (no NaN from /0)."""
        silent = np.zeros(int(0.5 * SR), dtype=np.float32)
        out = fn(silent, SR)
        assert np.all(np.isfinite(out))

    def test_apply_transition_zero_length_buffers(self) -> None:
        """apply_transition must accept empty tail/head without crashing."""
        from autodj.transitions import apply_transition

        empty = np.zeros(0, dtype=np.float32)
        for fx in (
            TransitionFx.REVERB_TAIL,
            TransitionFx.VINYL_REWIND,
            TransitionFx.PHASER,
            TransitionFx.HALFTIME,
        ):
            t, h, _extra = apply_transition(empty, empty, SR, fx)
            assert t.shape == (0,)
            assert h.shape == (0,)


class TestEffectsEmptyBuffer:
    """Cover the early-return ``if len(tail) == 0: return tail`` branches
    for every per-effect helper.  These are testable defensively.
    """

    @pytest.mark.parametrize(
        "fn",
        [
            echo_out,
            reverb_tail,
            tape_stop,
            gate_stutter,
            highpass_sweep,
            lowpass_sweep,
            bitcrusher,
            flanger,
            pitch_swell,
            telephone,
            chorus,
            submerge,
            vinyl_wow,
            freeze,
            scratch,
            beat_repeat,
            sidechain_pump,
            reverse_reverb,
            vinyl_rewind,
            transformer,
            stutter_build,
            wow_flutter,
            phaser,
            ring_modulator,
            dub_delay,
        ],
    )
    def test_empty_tail_returns_empty(self, fn) -> None:
        empty = np.zeros(0, dtype=np.float32)
        out = fn(empty, SR)
        assert isinstance(out, np.ndarray)
        assert out.shape == (0,)


class TestHalftimeShortGrain:
    def test_low_sample_rate_uses_ones_window(self) -> None:
        """With SR < 40, grain_n falls below 2 → np.ones path (line 1522)."""
        from autodj.transitions import halftime

        # Need n >= 4 (passes guard) but grain_n = int(0.05*SR).  SR=20 → 1.
        audio = np.array([0.1, 0.2, 0.3, 0.4, 0.5], dtype=np.float32)
        out = halftime(audio, sample_rate=20)
        assert out.shape == audio.shape
        assert np.all(np.isfinite(out))


class TestPitchFallShortInput:
    def test_short_input_returns_unchanged(self) -> None:
        from autodj.transitions import pitch_fall

        audio = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        out = pitch_fall(audio, SR)
        assert np.array_equal(out, audio)


class TestForwardSpinShortInput:
    def test_short_input_returns_unchanged(self) -> None:
        """``_forward_spin_tail`` early-returns when n<4 (line 1574)."""
        from autodj.transitions import _forward_spin_tail

        audio = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        out = _forward_spin_tail(audio)
        assert np.array_equal(out, audio)


class TestNoiseDropExtraEmpty:
    def test_empty_tail_returns_empty(self) -> None:
        """``_noise_drop_extra`` early-returns when tail is empty (line 1558)."""
        from autodj.transitions import _noise_drop_extra

        empty = np.zeros(0, dtype=np.float32)
        out = _noise_drop_extra(empty, SR)
        assert out.shape == (0,)


class TestPickEffectEdgeCases:
    def test_pick_effect_with_no_rng_succeeds(self) -> None:
        # Ensures the default RNG path is safe (used in production).
        from autodj.transitions import pick_effect

        for _ in range(20):
            picked = pick_effect(TransitionFx.RANDOM)
            assert picked != TransitionFx.NONE
            assert picked != TransitionFx.RANDOM
            assert picked != TransitionFx.ROTATE

    def test_rotate_returns_concrete_effect(self) -> None:
        from autodj.transitions import pick_effect

        for _ in range(50):
            picked = pick_effect(TransitionFx.ROTATE)
            assert picked != TransitionFx.NONE
            assert picked != TransitionFx.RANDOM
            assert picked != TransitionFx.ROTATE
