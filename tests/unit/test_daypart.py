"""Tests for autodj.daypart — wall-clock-driven mood profiles."""

from __future__ import annotations

from datetime import datetime

import pytest

from autodj.daypart import (
    DAYPARTS,
    Daypart,
    current_daypart,
    daypart_for_hour,
    from_config_dict,
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


class TestFromConfigDict:
    def test_empty_returns_built_ins(self) -> None:
        assert from_config_dict({}) == DAYPARTS

    def test_loads_custom(self) -> None:
        data = {
            "warmup": {
                "start_hour": 7,
                "end_hour": 9,
                "target_bpm": 70.0,
                "target_energy": 0.03,
                "bpm_weight": 0.4,
            },
        }
        out = from_config_dict(data)
        assert len(out) == 1
        assert out[0].name == "warmup"
        assert out[0].target_bpm == 70.0
        assert out[0].bpm_weight == 0.4

    def test_default_bpm_weight(self) -> None:
        data = {
            "x": {"start_hour": 0, "end_hour": 1, "target_bpm": 100, "target_energy": 0.05},
        }
        out = from_config_dict(data)
        assert out[0].bpm_weight == 0.25

    def test_missing_field_raises(self) -> None:
        data = {"x": {"start_hour": 0, "end_hour": 1}}  # missing target_bpm/energy
        with pytest.raises(ValueError):
            from_config_dict(data)

    def test_invalid_hour_raises(self) -> None:
        data = {
            "x": {"start_hour": 25, "end_hour": 1, "target_bpm": 100, "target_energy": 0.05},
        }
        with pytest.raises(ValueError):
            from_config_dict(data)

    def test_non_table_raises(self) -> None:
        data = {"x": "not a dict"}
        with pytest.raises(ValueError):
            from_config_dict(data)


class TestBuiltInDayparts:
    def test_full_day_coverage(self) -> None:
        # Every hour 0-23 must be covered by at least one daypart
        for h in range(24):
            assert any(dp.covers(h) for dp in DAYPARTS), f"Hour {h} uncovered"

    def test_named_dayparts_present(self) -> None:
        names = {dp.name for dp in DAYPARTS}
        assert {"morning", "midday", "afternoon", "evening", "night"}.issubset(names)


class TestAppliesToIndex:
    def test_empty_indexes_applies_anywhere(self) -> None:
        from autodj.daypart import Daypart

        dp = Daypart(
            name="any",
            start_hour=0,
            end_hour=23,
            target_bpm=100,
            target_energy=0.05,
        )
        assert dp.applies_to_index(None) is True
        assert dp.applies_to_index("anything") is True

    def test_specific_indexes(self) -> None:
        from autodj.daypart import Daypart

        dp = Daypart(
            name="workout",
            start_hour=6,
            end_hour=10,
            target_bpm=140,
            target_energy=0.1,
            indexes=("workout", "cardio"),
        )
        assert dp.applies_to_index("workout") is True
        assert dp.applies_to_index("cardio") is True
        assert dp.applies_to_index("ambient") is False
        assert dp.applies_to_index(None) is False


class TestLoadDaypartsFromDir:
    def test_missing_folder_returns_builtins(self, tmp_path):
        from autodj.daypart import DAYPARTS, load_dayparts_from_dir

        result = load_dayparts_from_dir(tmp_path / "nope")
        assert result == DAYPARTS

    def test_empty_folder_returns_builtins(self, tmp_path):
        from autodj.daypart import DAYPARTS, load_dayparts_from_dir

        (tmp_path / "dp").mkdir()
        result = load_dayparts_from_dir(tmp_path / "dp")
        assert result == DAYPARTS

    def test_loads_one_per_file(self, tmp_path):
        from autodj.daypart import load_dayparts_from_dir

        d = tmp_path / "dp"
        d.mkdir()
        (d / "morning.toml").write_text(
            "start_hour = 6\n"
            "end_hour = 10\n"
            "target_bpm = 80\n"
            "target_energy = 0.04\n"
            'indexes = ["main"]\n',
            encoding="utf-8",
        )
        (d / "evening.toml").write_text(
            "start_hour = 18\nend_hour = 22\ntarget_bpm = 128\ntarget_energy = 0.10\n",
            encoding="utf-8",
        )
        out = load_dayparts_from_dir(d)
        names = sorted(dp.name for dp in out)
        assert names == ["evening", "morning"]
        morning = next(dp for dp in out if dp.name == "morning")
        assert morning.indexes == ("main",)
        evening = next(dp for dp in out if dp.name == "evening")
        assert evening.indexes == ()

    def test_malformed_file_skipped(self, tmp_path):
        from autodj.daypart import load_dayparts_from_dir

        d = tmp_path / "dp"
        d.mkdir()
        (d / "good.toml").write_text(
            "start_hour = 6\nend_hour = 10\ntarget_bpm = 80\ntarget_energy = 0.04\n",
            encoding="utf-8",
        )
        (d / "broken.toml").write_text("start_hour = abc\n", encoding="utf-8")
        out = load_dayparts_from_dir(d)
        assert len(out) == 1
        assert out[0].name == "good"

    def test_missing_field_skipped(self, tmp_path):
        from autodj.daypart import DAYPARTS, load_dayparts_from_dir

        d = tmp_path / "dp"
        d.mkdir()
        (d / "incomplete.toml").write_text("start_hour = 6\n", encoding="utf-8")
        out = load_dayparts_from_dir(d)
        assert out == DAYPARTS

    def test_out_of_range_hours_skipped(self, tmp_path):
        from autodj.daypart import DAYPARTS, load_dayparts_from_dir

        d = tmp_path / "dp"
        d.mkdir()
        (d / "bad.toml").write_text(
            "start_hour = 25\nend_hour = 10\ntarget_bpm = 80\ntarget_energy = 0.04\n",
            encoding="utf-8",
        )
        assert load_dayparts_from_dir(d) == DAYPARTS
