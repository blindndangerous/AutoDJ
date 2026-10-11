from __future__ import annotations

import pytest
from pydantic import ValidationError

from autodj.config import PICK_MODES, TRANSITION_MODES
from autodj.settings_bodies import PlaybackSettingsBody


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
