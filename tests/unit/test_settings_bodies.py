from __future__ import annotations

import pytest
from pydantic import ValidationError

from autodj.config import PICK_MODES, TRANSITION_MODES
from autodj.settings_bodies import (
    DjMixBody,
    EqBody,
    PlaybackSettingsBody,
    StreamSettingsBody,
)


@pytest.mark.parametrize("mode", PICK_MODES)
def test_playback_body_accepts_pick_modes(mode: str) -> None:
    assert PlaybackSettingsBody.model_validate({"pick_mode": mode}).pick_mode == mode


@pytest.mark.parametrize("mode", ("off", "replaygain", "smooth", "stereotool"))
def test_playback_body_accepts_loudness_modes(mode: str) -> None:
    assert (
        PlaybackSettingsBody.model_validate({"loudness_processing": mode}).loudness_processing
        == mode
    )


def test_playback_body_accepts_saved_points_and_legacy_fields() -> None:
    body = PlaybackSettingsBody.model_validate(
        {"transition_mode": "saved_points", "pure_shuffle": True, "replaygain_enabled": True}
    )
    assert body.transition_mode == "saved_points"
    assert body.pure_shuffle is True
    assert body.replaygain_enabled is True


@pytest.mark.parametrize("value", (0, 7, 12))
def test_playback_body_accepts_agc_drive_range(value: float) -> None:
    assert (
        PlaybackSettingsBody.model_validate(
            {"stereotool_agc_drive_db": value}
        ).stereotool_agc_drive_db
        == value
    )


@pytest.mark.parametrize("value", (-0.1, 12.1, float("inf"), float("nan")))
def test_playback_body_rejects_agc_drive_outside_finite_range(value: float) -> None:
    with pytest.raises(ValidationError):
        PlaybackSettingsBody.model_validate({"stereotool_agc_drive_db": value})


def test_retired_quiet_boost_is_not_an_api_setting() -> None:
    assert "quiet_boost_db" not in PlaybackSettingsBody.model_fields


@pytest.mark.parametrize(
    ("field", "value"),
    [("pick_mode", "pure"), ("loudness_processing", "lufs"), ("transition_mode", "cues")],
)
def test_playback_body_rejects_unknown_modes(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        PlaybackSettingsBody.model_validate({field: value})


def test_playback_body_rejects_stereotool_install_path() -> None:
    with pytest.raises(ValidationError):
        PlaybackSettingsBody.model_validate({"stereotool_path": "custom-command"})


def test_config_transition_modes_include_saved_points() -> None:
    assert "saved_points" in TRANSITION_MODES


@pytest.mark.parametrize(
    ("field", "good", "bad"),
    [
        ("crossfade_bass_cutoff_hz", 180.0, 10.0),
        ("pick_top_k", 5, 0),
        ("pick_temperature", 0.0, 11.0),
        ("replaygain_max_clip_safe_gain", 1.0, 0.0),
        ("prefetch_next_track", False, "maybe"),
        ("silence_trigger_crossfade", True, "maybe"),
    ],
)
def test_playback_body_ranges_for_new_settings(field: str, good: object, bad: object) -> None:
    assert getattr(PlaybackSettingsBody.model_validate({field: good}), field) == good
    with pytest.raises(ValidationError):
        PlaybackSettingsBody.model_validate({field: bad})


@pytest.mark.parametrize(
    ("field", "good", "bad"),
    [
        ("beatmatch_max_stretch", 0.08, 0.6),
        ("beatmatch_glide_bars", 0, 65),
        ("filter_sweep_floor_hz", 250.0, 5001.0),
    ],
)
def test_djmix_body_ranges_for_new_settings(field: str, good: object, bad: object) -> None:
    assert getattr(DjMixBody.model_validate({field: good}), field) == good
    with pytest.raises(ValidationError):
        DjMixBody.model_validate({field: bad})


def test_djmix_body_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValidationError):
        DjMixBody.model_validate({"filter_sweep_floor_hz": float("nan")})


@pytest.mark.parametrize("bad", ["", "   ", "Bad\nName", "x" * 101])
def test_stream_body_refuses_unusable_station_names(bad: str) -> None:
    with pytest.raises(ValidationError):
        StreamSettingsBody.model_validate({"station_name": bad})


def test_stream_body_trims_the_station_name() -> None:
    assert (
        StreamSettingsBody.model_validate({"station_name": " Kitchen "}).station_name == "Kitchen"
    )


@pytest.mark.parametrize("value", (0, 1.0, 2))
def test_eq_body_accepts_bridge_range(value: float) -> None:
    assert EqBody.model_validate({"low": value}).low == value


@pytest.mark.parametrize("value", (-0.01, 2.01, float("inf"), float("nan")))
def test_eq_body_rejects_gains_outside_bridge_range(value: float) -> None:
    with pytest.raises(ValidationError):
        EqBody.model_validate({"mid": value})


@pytest.mark.parametrize("rule", ["artist", "album", "track", "title"])
@pytest.mark.parametrize(
    "limits",
    [
        {"tracks": -1},
        {"tracks": 1.5},
        {"tracks": True},
        {"tracks": 100001},
        {"minutes": -1},
        {"minutes": float("nan")},
        {"minutes": True},
        {"minutes": 10081},
    ],
)
def test_repeat_rules_reject_invalid_limits(rule, limits):
    with pytest.raises(ValidationError):
        PlaybackSettingsBody.model_validate({"repeat_rules": {rule: limits}})


@pytest.mark.parametrize(
    "field",
    [
        "no_repeat_window",
        "artist_repeat_window",
        "no_repeat_minutes",
        "artist_repeat_minutes",
        "title_repeat_window",
        "title_repeat_minutes",
        "related_artists_file",
        "related_artists_starter",
    ],
)
def test_removed_repeat_api_fields_are_not_silently_accepted(field):
    with pytest.raises(ValidationError):
        PlaybackSettingsBody.model_validate({field: 3})
