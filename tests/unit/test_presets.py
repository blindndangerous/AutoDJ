"""Unit tests for autodj.presets.

Covers curve functions, BUILTIN_PRESETS, preset_from_config, load_user_presets,
and get_preset — without any audio or model dependency.
"""

import pytest

from autodj.presets import (
    BUILTIN_PRESETS,
    Preset,
    constant_curve,
    get_preset,
    linear_curve,
    load_user_presets,
    preset_from_config,
    slide_curve,
)

# ---------------------------------------------------------------------------
# Curve functions
# ---------------------------------------------------------------------------


class TestConstantCurve:
    def test_returns_target_at_all_positions(self) -> None:
        curve = constant_curve(120.0)
        for i in range(10):
            assert curve(i) == pytest.approx(120.0)

    def test_different_bpms(self) -> None:
        assert constant_curve(90.0)(5) == pytest.approx(90.0)
        assert constant_curve(145.0)(0) == pytest.approx(145.0)


class TestLinearCurve:
    def test_starts_at_bpm_start(self) -> None:
        curve = linear_curve(70.0, 130.0, horizon=30)
        assert curve(0) == pytest.approx(70.0)

    def test_ends_at_bpm_end_at_horizon(self) -> None:
        curve = linear_curve(70.0, 130.0, horizon=30)
        assert curve(30) == pytest.approx(130.0)

    def test_plateaus_past_horizon(self) -> None:
        curve = linear_curve(70.0, 130.0, horizon=30)
        assert curve(50) == pytest.approx(130.0)
        assert curve(100) == pytest.approx(130.0)

    def test_monotonically_increasing(self) -> None:
        curve = linear_curve(80.0, 140.0, horizon=20)
        values = [curve(i) for i in range(21)]
        assert all(values[i] <= values[i + 1] for i in range(len(values) - 1))

    def test_midpoint_is_midway(self) -> None:
        curve = linear_curve(80.0, 120.0, horizon=20)
        assert curve(10) == pytest.approx(100.0, abs=1.0)


class TestSlideCurve:
    def test_starts_at_low(self) -> None:
        curve = slide_curve(80.0, 140.0, horizon=40)
        assert curve(0) == pytest.approx(80.0, abs=1.0)

    def test_peaks_near_midpoint(self) -> None:
        curve = slide_curve(80.0, 140.0, horizon=40)
        peak = max(curve(i) for i in range(41))
        assert peak == pytest.approx(140.0, abs=2.0)

    def test_returns_to_low_at_horizon(self) -> None:
        curve = slide_curve(80.0, 140.0, horizon=40)
        assert curve(40) == pytest.approx(80.0, abs=2.0)

    def test_values_in_range(self) -> None:
        curve = slide_curve(80.0, 140.0, horizon=40)
        for i in range(41):
            val = curve(i)
            assert 79.0 <= val <= 141.0, f"curve({i})={val} out of range"


# ---------------------------------------------------------------------------
# BUILTIN_PRESETS
# ---------------------------------------------------------------------------


class TestBuiltinPresets:
    def test_all_expected_names_present(self) -> None:
        expected = {
            "wakeup",
            "winddown",
            "sleep",
            "morning",
            "slide",
            "party",
            "workout",
            "chill",
            "focus",
            "driving",
        }
        assert expected.issubset(set(BUILTIN_PRESETS.keys()))

    def test_each_preset_has_name(self) -> None:
        for name, preset in BUILTIN_PRESETS.items():
            assert preset.name == name

    def test_bpm_weight_in_range(self) -> None:
        for preset in BUILTIN_PRESETS.values():
            assert 0.0 < preset.bpm_weight < 1.0

    def test_target_bpm_returns_float_or_none(self) -> None:
        for preset in BUILTIN_PRESETS.values():
            val = preset.target_bpm(0)
            assert val is None or isinstance(val, float)

    def test_wakeup_bpm_increases(self) -> None:
        preset = BUILTIN_PRESETS["wakeup"]
        bpms = [preset.target_bpm(i) for i in range(30)]
        # Filter out None (shouldn't be any for wakeup)
        bpms = [b for b in bpms if b is not None]
        assert bpms[0] < bpms[-1], "wakeup should increase BPM over time"

    def test_winddown_bpm_decreases(self) -> None:
        preset = BUILTIN_PRESETS["winddown"]
        bpms = [preset.target_bpm(i) for i in range(30)]
        bpms = [b for b in bpms if b is not None]
        assert bpms[0] > bpms[-1], "winddown should decrease BPM over time"

    def test_party_constant_high_bpm(self) -> None:
        preset = BUILTIN_PRESETS["party"]
        bpm = preset.target_bpm(0)
        assert bpm is not None and bpm >= 120.0

    def test_chill_constant_low_bpm(self) -> None:
        preset = BUILTIN_PRESETS["chill"]
        bpm = preset.target_bpm(0)
        assert bpm is not None and bpm < 100.0


# ---------------------------------------------------------------------------
# preset_from_config
# ---------------------------------------------------------------------------


class TestPresetFromConfig:
    def test_bpm_target_only_creates_constant(self) -> None:
        p = preset_from_config("myfocus", {"bpm_target": 90})
        assert p.target_bpm(0) == pytest.approx(90.0)
        assert p.target_bpm(100) == pytest.approx(90.0)

    def test_bpm_start_end_creates_linear(self) -> None:
        p = preset_from_config("rise", {"bpm_start": 80, "bpm_end": 130})
        start = p.target_bpm(0)
        end = p.target_bpm(30)
        assert start == pytest.approx(80.0, abs=2.0)
        assert end is not None and end >= start

    def test_slide_curve_uses_sine_arch(self) -> None:
        p = preset_from_config("arch", {"bpm_start": 80, "bpm_end": 140, "curve": "slide"})
        # Should return to near-start at horizon
        end_bpm = p.target_bpm(40)
        assert end_bpm is not None and end_bpm < 120.0

    def test_discovery_every_forwarded(self) -> None:
        p = preset_from_config("disc", {"bpm_target": 90, "discovery_every": 5})
        assert p.discovery_every == 5

    def test_no_discovery_defaults_to_none(self) -> None:
        p = preset_from_config("nodis", {"bpm_target": 90})
        assert p.discovery_every is None

    def test_custom_bpm_weight_respected(self) -> None:
        p = preset_from_config("heavy", {"bpm_target": 90, "bpm_weight": 0.6})
        assert p.bpm_weight == pytest.approx(0.6)

    def test_name_preserved(self) -> None:
        p = preset_from_config("myriff", {"bpm_target": 100})
        assert p.name == "myriff"

    def test_linear_curve_without_bpm_start_raises(self) -> None:
        """curve='linear' requires both bpm_start and bpm_end."""
        with pytest.raises(ValueError, match="bpm_start"):
            preset_from_config("bad", {"curve": "linear", "bpm_end": 130})

    def test_linear_curve_without_bpm_end_raises(self) -> None:
        with pytest.raises(ValueError, match="bpm_start"):
            preset_from_config("bad", {"curve": "linear", "bpm_start": 80})

    def test_no_bpm_config_raises(self) -> None:
        """A section with no BPM config at all should raise ValueError."""
        with pytest.raises(ValueError):
            preset_from_config("empty", {})


# ---------------------------------------------------------------------------
# load_user_presets
# ---------------------------------------------------------------------------


class TestLoadUserPresets:
    def test_empty_config_returns_empty_dict(self) -> None:
        result = load_user_presets({})
        assert result == {}

    def test_no_presets_section_returns_empty_dict(self) -> None:
        result = load_user_presets({"library": {"music_dir": "/music"}})
        assert result == {}

    def test_loads_single_preset(self) -> None:
        raw = {"presets": {"myjam": {"bpm_target": 95}}}
        result = load_user_presets(raw)
        assert "myjam" in result
        assert result["myjam"].target_bpm(0) == pytest.approx(95.0)

    def test_loads_multiple_presets(self) -> None:
        raw = {
            "presets": {
                "morning": {"bpm_start": 60, "bpm_end": 100},
                "night": {"bpm_target": 75},
            }
        }
        result = load_user_presets(raw)
        assert "morning" in result
        assert "night" in result

    def test_discovery_every_in_user_preset(self) -> None:
        raw = {"presets": {"party2": {"bpm_target": 130, "discovery_every": 8}}}
        result = load_user_presets(raw)
        assert result["party2"].discovery_every == 8

    def test_non_dict_section_is_skipped(self) -> None:
        """A preset section that is not a dict (e.g. a scalar) should be silently skipped."""
        raw = {"presets": {"bad": "not a dict", "good": {"bpm_target": 100}}}
        result = load_user_presets(raw)
        assert "bad" not in result
        assert "good" in result

    def test_invalid_section_is_skipped_with_warning(self) -> None:
        """A section that raises ValueError during parsing should be skipped."""
        raw = {"presets": {"broken": {}, "ok": {"bpm_target": 90}}}
        result = load_user_presets(raw)
        assert "broken" not in result
        assert "ok" in result


# ---------------------------------------------------------------------------
# get_preset
# ---------------------------------------------------------------------------


class TestGetPreset:
    def test_returns_builtin_by_name(self) -> None:
        p = get_preset("chill")
        assert p.name == "chill"

    def test_user_preset_shadows_builtin(self) -> None:
        user = {"chill": preset_from_config("chill", {"bpm_target": 999})}
        p = get_preset("chill", user_presets=user)
        assert p.target_bpm(0) == pytest.approx(999.0)

    def test_raises_on_unknown_name(self) -> None:
        with pytest.raises(ValueError, match="does_not_exist_xyz"):
            get_preset("does_not_exist_xyz")

    def test_error_lists_available_presets(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            get_preset("does_not_exist_xyz")
        msg = str(exc_info.value).lower()
        # Should mention at least one known preset
        assert "chill" in msg or "party" in msg or "wakeup" in msg

    def test_user_presets_default_empty(self) -> None:
        p = get_preset("party")
        assert p is not None


# ---------------------------------------------------------------------------
# Preset.target_bpm
# ---------------------------------------------------------------------------


class TestPresetTargetBpm:
    def test_constant_preset_same_at_all_positions(self) -> None:
        p = Preset(name="test", bpm_weight=0.2, _curve=constant_curve(100.0))
        assert p.target_bpm(0) == pytest.approx(100.0)
        assert p.target_bpm(50) == pytest.approx(100.0)

    def test_discovery_every_none_by_default(self) -> None:
        p = Preset(name="test", bpm_weight=0.2, _curve=constant_curve(100.0))
        assert p.discovery_every is None


class TestPresetMatchesGenres:
    """Cover the Preset.matches_genre method."""

    def test_no_filter_passes(self) -> None:
        p = Preset(name="t", bpm_weight=0.0, _curve=constant_curve(120.0))
        assert p.matches_genre("anything") is True

    def test_filter_with_empty_entry_fails(self) -> None:
        p = Preset(
            name="t",
            bpm_weight=0.0,
            _curve=constant_curve(120.0),
            genres=["rock"],
        )
        assert p.matches_genre("") is False

    def test_filter_substring_match(self) -> None:
        p = Preset(
            name="t",
            bpm_weight=0.0,
            _curve=constant_curve(120.0),
            genres=["rock"],
        )
        assert p.matches_genre("Indie Rock") is True
        assert p.matches_genre("Jazz") is False


class TestPresetGenresFromSection:
    def test_string_genre_wrapped_in_list(self) -> None:
        p = preset_from_config(
            "x",
            {"bpm_target": 100, "bpm_weight": 0.2, "genres": "rock"},
        )
        assert p.genres == ["rock"]

    def test_invalid_genres_type_yields_empty(self) -> None:
        p = preset_from_config(
            "x",
            {"bpm_target": 100, "bpm_weight": 0.2, "genres": 42},
        )
        assert p.genres == []
