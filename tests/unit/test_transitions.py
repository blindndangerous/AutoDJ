"""Smoke tests for autodj.transitions — every effect produces sane output."""

import numpy as np
import pytest

from autodj.transitions import (
    _LAYERS,
    _TAIL_EFFECTS,
    TransitionFx,
    _freeze,
    _glitch,
    _halftime,
    _no_louder_than,
    _reverb,
    apply_transition,
    pick_effect,
)

SR = 22050

_TAIL = sorted(_TAIL_EFFECTS)


def _audio(seconds: float = 2.0) -> np.ndarray:
    """Low-energy float32 mono audio for testing.

    Std=0.15 keeps random peaks under ~0.6 so effects that introduce
    even ±2× gain (distortion drive, etc.) stay below clipping.
    """
    rng = np.random.default_rng(42)
    n = int(seconds * SR)
    return rng.standard_normal(n).astype(np.float32) * 0.15


class TestPerEffectSanity:
    """Each outgoing-tail effect must:
    - return a numpy float32 array
    - not introduce NaN / Inf
    - keep peak amplitude ≤ 1.05 (slight headroom OK)
    """

    @pytest.mark.parametrize("effect", _TAIL, ids=str)
    def test_outgoing_tail_effect(self, effect: TransitionFx) -> None:
        a = _audio()
        out = _TAIL_EFFECTS[effect](a, SR)
        assert isinstance(out, np.ndarray)
        assert out.dtype == np.float32
        assert out.shape == a.shape
        assert np.all(np.isfinite(out))
        assert np.abs(out).max() <= 1.05


class TestSynthesisedLayers:
    @pytest.mark.parametrize("effect", sorted(_LAYERS), ids=str)
    def test_fixed_length(self, effect: TransitionFx) -> None:
        layer = _LAYERS[effect](SR, SR, 3)
        assert layer.shape == (SR,)
        assert np.all(np.isfinite(layer))

    @pytest.mark.parametrize("effect", sorted(_LAYERS), ids=str)
    def test_zero_length(self, effect: TransitionFx) -> None:
        assert _LAYERS[effect](0, SR, 3).shape == (0,)


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
    def test_short_input_doesnt_crash(self) -> None:
        short = np.array([0.1, -0.1, 0.05], dtype=np.float32)
        for effect in (
            TransitionFx.TAPE_STOP,
            TransitionFx.BACKSPIN,
            TransitionFx.VINYL_WOW,
            TransitionFx.PITCH_SWELL,
        ):
            out = _TAIL_EFFECTS[effect](short, SR)
            assert out.shape == short.shape

    @pytest.mark.parametrize("length", [0, 1, 10])
    def test_no_effect_raises_on_tiny_buffers(self, length: int) -> None:
        """The module promises no effect raises on short or silent buffers."""
        from autodj.transitions import apply_transition

        silent = np.zeros(length, dtype=np.float32)
        for effect in TransitionFx:
            apply_transition(silent, silent, SR, effect, seed=1)

    @pytest.mark.parametrize(
        "effect", [TransitionFx.PITCH_FALL, TransitionFx.FORWARD_SPIN], ids=str
    )
    def test_spin_shorter_than_its_curve_is_unchanged(self, effect: TransitionFx) -> None:
        audio = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        assert np.array_equal(_TAIL_EFFECTS[effect](audio, SR), audio)

    def test_backspin_on_a_few_samples_keeps_length(self) -> None:
        a = _audio(0.01)
        assert _TAIL_EFFECTS[TransitionFx.BACKSPIN](a, SR).shape == a.shape

    def test_reverb_shorter_than_its_combs(self) -> None:
        # 50 samples: shorter than every comb and allpass delay.
        short = np.linspace(-0.1, 0.1, 50, dtype=np.float32)
        assert _reverb(short, SR, wet=0.45).shape == short.shape

    def test_halftime_at_a_sample_rate_too_low_for_a_window(self) -> None:
        """At 20 Hz a grain is one sample, too short for a Hann window."""
        audio = np.array([0.1, 0.2, 0.3, 0.4, 0.5], dtype=np.float32)
        out = _halftime(audio, sample_rate=20)
        assert out.shape == audio.shape
        assert np.all(np.isfinite(out))


class TestFreeze:
    def test_loops_grain_through_tail(self) -> None:
        a = _audio(1.0)
        out = _freeze(a, SR, grain_ms=100.0)
        assert out.shape == a.shape
        # Undo the fade-out: a grain later the loop repeats itself.
        looped = out / np.maximum(np.linspace(1.0, 0.0, len(out)), 1e-9)
        grain = int(0.1 * SR)
        assert looped[grain // 2] == pytest.approx(looped[grain + grain // 2], abs=1e-4)

    def test_fade_out_brings_tail_to_silence(self) -> None:
        a = _audio(1.0)
        out = _TAIL_EFFECTS[TransitionFx.FREEZE](a, SR)
        assert abs(out[-1]) < 0.05
        assert abs(out[len(out) // 2]) > abs(out[-1])

    def test_grain_longer_than_tail_clamps(self) -> None:
        a = _audio(0.05)  # 50 ms
        out = _freeze(a, SR, grain_ms=200.0)
        assert out.shape == a.shape


class TestGlitch:
    def test_output_same_length(self) -> None:
        a = _audio(1.0)
        assert _glitch(a, SR, 42, slice_ms=50.0).shape == a.shape

    def test_seeded_is_reproducible(self) -> None:
        a = _audio(0.5)
        np.testing.assert_array_equal(
            _glitch(a, SR, 7, slice_ms=80.0), _glitch(a, SR, 7, slice_ms=80.0)
        )

    def test_different_seeds_diverge(self) -> None:
        a = _audio(0.5)
        assert not np.allclose(_glitch(a, SR, 1, slice_ms=80.0), _glitch(a, SR, 2, slice_ms=80.0))

    def test_slice_longer_than_input_returns_copy(self) -> None:
        a = _audio(0.05)
        assert _glitch(a, SR, None, slice_ms=500.0).shape == a.shape

    def test_empty_input_returns_empty(self) -> None:
        assert _glitch(np.zeros(0, dtype=np.float32), SR, None, slice_ms=80.0).shape == (0,)


class TestEdgeCaseInputs:
    """Boundary conditions: empty, single-sample and all-silence buffers.
    Every effect should degrade gracefully (no crash, no NaN/Inf in output).
    """

    @pytest.mark.parametrize("effect", _TAIL, ids=str)
    def test_empty_tail_returns_empty(self, effect: TransitionFx) -> None:
        out = _TAIL_EFFECTS[effect](np.zeros(0, dtype=np.float32), SR)
        assert isinstance(out, np.ndarray)
        assert out.shape == (0,)

    @pytest.mark.parametrize("effect", _TAIL, ids=str)
    def test_one_sample_input(self, effect: TransitionFx) -> None:
        """Single-sample tail must not crash; either passes through or empty."""
        tiny = np.array([0.1], dtype=np.float32)
        out = _TAIL_EFFECTS[effect](tiny, SR)
        assert isinstance(out, np.ndarray)
        assert out.shape == tiny.shape
        assert np.all(np.isfinite(out))

    @pytest.mark.parametrize("effect", _TAIL, ids=str)
    def test_all_silence_input_stays_finite(self, effect: TransitionFx) -> None:
        """Silent buffer in -> silent or finite buffer out (no NaN from /0)."""
        silent = np.zeros(int(0.5 * SR), dtype=np.float32)
        assert np.all(np.isfinite(_TAIL_EFFECTS[effect](silent, SR)))

    def test_apply_transition_zero_length_buffers(self) -> None:
        """apply_transition must accept empty tail/head without crashing."""
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
