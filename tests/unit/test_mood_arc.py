"""Tests for the set-relative mood arc envelope."""

from __future__ import annotations

import itertools

import pytest

from autodj.mood_arc import (
    DEFAULT_ANCHORS,
    ArcAnchor,
    MoodArc,
    current_arc_target,
    make_default_arc,
)


class TestArcInterpolation:
    def test_warmup_target_is_low(self) -> None:
        arc = MoodArc(start_time_s=0.0, duration_s=3600.0)
        target = current_arc_target(arc, now_s=0.0)
        # At progress=0 the BPM frac is 0.20 -> 95 + 0.20 * (128-95) = 101.6
        assert target.target_bpm < arc.high_bpm
        assert target.target_bpm <= arc.low_bpm + 0.21 * (arc.high_bpm - arc.low_bpm)
        assert target.progress == 0.0

    def test_peak_at_three_quarters(self) -> None:
        arc = MoodArc(start_time_s=0.0, duration_s=3600.0)
        target = current_arc_target(arc, now_s=arc.duration_s * 0.75)
        # Peak anchor: bpm_frac=1.0 -> high_bpm exactly.
        assert target.target_bpm == pytest.approx(arc.high_bpm)
        assert target.target_energy == pytest.approx(arc.high_energy)

    def test_arc_loops_after_completion(self) -> None:
        arc = MoodArc(start_time_s=0.0, duration_s=3600.0)
        first = current_arc_target(arc, now_s=arc.duration_s * 0.25)
        second = current_arc_target(arc, now_s=arc.duration_s * 1.25)
        # Same progress in the loop -> same target.
        assert first.target_bpm == pytest.approx(second.target_bpm)
        assert second.progress < 1.0

    def test_progress_clamps_below_zero(self) -> None:
        arc = MoodArc(start_time_s=100.0, duration_s=3600.0)
        # now_s before start_time_s -> elapsed=0 -> progress=0.
        target = current_arc_target(arc, now_s=0.0)
        assert target.progress == 0.0

    def test_zero_duration_does_not_divide_by_zero(self) -> None:
        # The dataclass allows duration_s <= 0 to test the guard.
        arc = MoodArc(start_time_s=0.0, duration_s=0.0)
        target = current_arc_target(arc, now_s=0.0)
        assert 0.0 <= target.progress <= 1.0

    def test_bpm_weight_clamped(self) -> None:
        arc = MoodArc(start_time_s=0.0, duration_s=3600.0, bpm_weight=2.5)
        target = current_arc_target(arc, now_s=0.0)
        assert target.bpm_weight == 1.0
        arc2 = MoodArc(start_time_s=0.0, duration_s=3600.0, bpm_weight=-1.0)
        target2 = current_arc_target(arc2, now_s=0.0)
        assert target2.bpm_weight == 0.0

    def test_custom_anchors_override_default(self) -> None:
        arc = MoodArc(
            start_time_s=0.0,
            duration_s=100.0,
            low_bpm=100.0,
            high_bpm=100.0,
        )
        flat = (
            ArcAnchor(progress=0.0, bpm_frac=0.5, energy_frac=0.5),
            ArcAnchor(progress=1.0, bpm_frac=0.5, energy_frac=0.5),
        )
        target = current_arc_target(arc, now_s=50.0, anchors=flat)
        # When low==high, target stays at low_bpm regardless of fraction.
        assert target.target_bpm == pytest.approx(100.0)

    def test_default_anchors_are_monotonic_to_peak(self) -> None:
        # BPM should rise from start to peak (progress 0 -> 0.75) and
        # then fall through cool-down anchors.
        peak_idx = next(i for i, a in enumerate(DEFAULT_ANCHORS) if a.progress == 0.75)
        rising = DEFAULT_ANCHORS[: peak_idx + 1]
        falling = DEFAULT_ANCHORS[peak_idx:]
        for lo, hi in itertools.pairwise(rising):
            assert hi.bpm_frac >= lo.bpm_frac
        for lo, hi in itertools.pairwise(falling):
            assert hi.bpm_frac <= lo.bpm_frac


class TestMakeDefaultArc:
    def test_factory_uses_now(self) -> None:
        import time

        before = time.time()
        arc = make_default_arc(duration_hours=2.0)
        after = time.time()
        assert before <= arc.start_time_s <= after
        assert arc.duration_s == pytest.approx(2.0 * 3600.0)

    def test_factory_clamps_tiny_durations(self) -> None:
        arc = make_default_arc(duration_hours=0.0)
        # Floor of 0.25 h prevents divide-by-zero in the picker.
        assert arc.duration_s == pytest.approx(0.25 * 3600.0)
