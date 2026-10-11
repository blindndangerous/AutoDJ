from __future__ import annotations

import pytest
from pydantic import ValidationError

from autodj.config import PICK_MODES, TRANSITION_MODES
from autodj.settings_bodies import DjMixBody, PlaybackSettingsBody, StreamSettingsBody


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
