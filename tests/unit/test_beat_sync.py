"""Unit tests for :mod:`autodj.beat_sync`.

Covers the pure-math helpers used by both the browser FX scheduler and
the server-side track payload synthesiser:

- ``extract_downbeats`` subsampling.
- ``synthesize_downbeats`` phase-anchored grid.
- ``next_downbeat_at`` with epsilon tolerance and missing data.
- ``bar_seconds`` divide-by-zero guard.
- ``lerp_bpm`` blend including unknown-side fallbacks.
- ``key_to_hz`` chromatic table + invalid input.
- ``lerp_hz`` log-space pitch glide + unknown-side fallbacks.
- ``FX_BAR_TABLE`` covers every transition effect surfaced by the web UI.
"""

from __future__ import annotations

import pytest

from autodj.beat_sync import (
    FX_BAR_TABLE,
    bar_seconds,
    extract_downbeats,
    fx_bars,
    fx_snaps_to_downbeat,
    key_to_hz,
    lerp_bpm,
    lerp_hz,
    next_downbeat_at,
    synthesize_downbeats,
)


class TestExtractDownbeats:
    def test_empty(self) -> None:
        assert extract_downbeats([]) == []

    def test_subsamples_every_fourth(self) -> None:
        beats = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
        assert extract_downbeats(beats, beats_per_bar=4) == [0.0, 2.0, 4.0]

    def test_zero_beats_per_bar(self) -> None:
        assert extract_downbeats([0.0, 0.5, 1.0], beats_per_bar=0) == []


class TestBarSeconds:
    def test_120_bpm_4_4(self) -> None:
        assert bar_seconds(120.0) == pytest.approx(2.0)

    def test_zero_bpm_safe_default(self) -> None:
        # Falls back to 2.0 (musical default) instead of dividing by zero.
        assert bar_seconds(0.0) == 2.0

    def test_3_4_time(self) -> None:
        assert bar_seconds(120.0, beats_per_bar=3) == pytest.approx(1.5)


class TestSynthesizeDownbeats:
    def test_anchor_phase_locked(self) -> None:
        # 120 BPM, anchor at 8.0 s, length 16 s.  Bar = 2.0 s.  Phase
        # walks back from 8 to 0.  Grid: 0, 2, 4, 6, 8, 10, 12, 14.
        grid = synthesize_downbeats(120.0, 16.0, anchor_s=8.0)
        assert grid == [0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0]

    def test_off_grid_anchor(self) -> None:
        grid = synthesize_downbeats(120.0, 8.0, anchor_s=1.5)
        # Phase = 1.5; walk back to 1.5 - 2*0 = 1.5 (>=0; not -0.5).  Then
        # 1.5, 3.5, 5.5, 7.5
        assert grid == [1.5, 3.5, 5.5, 7.5]

    def test_zero_bpm_returns_empty(self) -> None:
        assert synthesize_downbeats(0.0, 60.0, anchor_s=0.0) == []

    def test_negative_length(self) -> None:
        assert synthesize_downbeats(120.0, -1.0) == []


class TestNextDownbeatAt:
    def test_returns_first_match(self) -> None:
        downbeats = [0.0, 2.0, 4.0, 6.0]
        assert next_downbeat_at(downbeats, 1.5) == 2.0

    def test_epsilon_treats_now_as_now(self) -> None:
        downbeats = [0.0, 2.0]
        # 2.0001 should still snap to 2.0 (within epsilon).
        assert next_downbeat_at(downbeats, 2.0001) == 2.0

    def test_no_future_downbeat(self) -> None:
        assert next_downbeat_at([0.0, 1.0], 5.0) is None

    def test_empty(self) -> None:
        assert next_downbeat_at([], 0.0) is None


class TestLerpBpm:
    def test_midpoint(self) -> None:
        assert lerp_bpm(120.0, 140.0, 0.5) == pytest.approx(130.0)

    def test_clamps_frac(self) -> None:
        assert lerp_bpm(120.0, 140.0, 1.5) == pytest.approx(140.0)
        assert lerp_bpm(120.0, 140.0, -0.5) == pytest.approx(120.0)

    def test_unknown_out(self) -> None:
        assert lerp_bpm(0.0, 140.0, 0.5) == 140.0

    def test_unknown_in(self) -> None:
        assert lerp_bpm(120.0, 0.0, 0.5) == 120.0

    def test_both_unknown(self) -> None:
        assert lerp_bpm(0.0, 0.0, 0.5) == 120.0


class TestKeyToHz:
    def test_c4(self) -> None:
        # C4 ≈ 261.63 Hz
        hz = key_to_hz(0)
        assert hz is not None
        assert hz == pytest.approx(261.63, abs=0.05)

    def test_a4(self) -> None:
        # A4 = 440 Hz exactly (chromatic 9)
        hz = key_to_hz(9)
        assert hz is not None
        assert hz == pytest.approx(440.0, abs=0.01)

    def test_octave_doubles(self) -> None:
        hz4 = key_to_hz(0, octave=4)
        hz5 = key_to_hz(0, octave=5)
        assert hz4 is not None and hz5 is not None
        assert hz5 / hz4 == pytest.approx(2.0)

    def test_unknown_key(self) -> None:
        assert key_to_hz(-1) is None
        assert key_to_hz(12) is None


class TestLerpHz:
    def test_log_midpoint(self) -> None:
        # 100 -> 400 at frac 0.5 → exp(mean of logs) = 200 (geometric mean)
        result = lerp_hz(100.0, 400.0, 0.5)
        assert result is not None
        assert result == pytest.approx(200.0, rel=1e-6)

    def test_one_known(self) -> None:
        assert lerp_hz(None, 440.0, 0.5) == 440.0
        assert lerp_hz(440.0, None, 0.5) == 440.0

    def test_neither_known(self) -> None:
        assert lerp_hz(None, None, 0.5) is None

    def test_clamp(self) -> None:
        # frac 1.5 clamps to 1.0 → exact in_hz
        result = lerp_hz(100.0, 400.0, 1.5)
        assert result is not None
        assert result == pytest.approx(400.0, rel=1e-6)


class TestFxBarTable:
    def test_known_effects_present(self) -> None:
        # Spot-check the rhythmic-set entries called out in the
        # changelog so a future refactor can't silently drop them.
        for name in [
            "beat_repeat",
            "gate_stutter",
            "echo_out",
            "dub_delay",
            "sidechain_pump",
            "halftime",
            "stutter_build",
            "scratch",
            "transformer",
            "air_horn",
            "dub_siren",
            "ring_modulator",
        ]:
            assert name in FX_BAR_TABLE, name

    def test_fx_bars_default(self) -> None:
        assert fx_bars("beat_repeat") == 4
        assert fx_bars("not_a_real_effect") == 4  # unknown-fallback

    def test_fx_snaps_to_downbeat(self) -> None:
        assert fx_snaps_to_downbeat("beat_repeat") is True
        assert fx_snaps_to_downbeat("reverb_tail") is False
        assert fx_snaps_to_downbeat("not_real") is False
