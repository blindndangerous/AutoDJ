"""Tests for autodj.daypart — wall-clock-driven mood profiles."""

from __future__ import annotations

from datetime import datetime

import pytest

from autodj.daypart import (
    DAYPARTS,
    Daypart,
    current_daypart,
    daypart_for_hour,
)


class TestDaypartCovers:
    def test_normal_window(self) -> None:
        dp = Daypart("morning", 6, 10, 80.0, 0.04)
        assert dp.covers(6) is True
        assert dp.covers(7) is True
        assert dp.covers(9) is True
        assert dp.covers(10) is False  # end exclusive
        assert dp.covers(5) is False

    def test_wrap_past_midnight(self) -> None:
        dp = Daypart("night", 22, 6, 90.0, 0.05)
        assert dp.covers(22) is True
        assert dp.covers(23) is True
        assert dp.covers(0) is True
        assert dp.covers(5) is True
        assert dp.covers(6) is False
        assert dp.covers(12) is False


class TestDaypartForHour:
    @pytest.mark.parametrize(
        "hour,expected",
        [
            (6, "morning"),
            (8, "morning"),
            (10, "midday"),
            (13, "midday"),
            (14, "afternoon"),
            (17, "afternoon"),
            (18, "evening"),
            (21, "evening"),
            (22, "night"),
            (0, "night"),
            (3, "night"),
            (5, "night"),
        ],
    )
    def test_built_in_mappings(self, hour, expected) -> None:
        assert daypart_for_hour(hour).name == expected

    def test_invalid_hour_raises(self) -> None:
        with pytest.raises(ValueError):
            daypart_for_hour(-1)
        with pytest.raises(ValueError):
            daypart_for_hour(24)

    def test_custom_profiles(self) -> None:
        custom = [Daypart("siesta", 14, 16, 60.0, 0.02)]
        assert daypart_for_hour(15, custom).name == "siesta"
        # Falls back to first when no coverage
        assert daypart_for_hour(8, custom).name == "siesta"


class TestCurrentDaypart:
    def test_uses_provided_now(self) -> None:
        morning = datetime(2026, 5, 4, 7, 30)  # 07:30
        assert current_daypart(now=morning).name == "morning"

    def test_default_uses_actual_now(self) -> None:
        # Just ensure it returns a Daypart without crashing
        dp = current_daypart()
        assert isinstance(dp, Daypart)
        assert dp.name in {p.name for p in DAYPARTS}


class TestBuiltInDayparts:
    def test_full_day_coverage(self) -> None:
        # Every hour 0-23 must be covered by at least one daypart
        for h in range(24):
            assert any(dp.covers(h) for dp in DAYPARTS), f"Hour {h} uncovered"

    def test_named_dayparts_present(self) -> None:
        names = {dp.name for dp in DAYPARTS}
        assert {"morning", "midday", "afternoon", "evening", "night"}.issubset(names)
