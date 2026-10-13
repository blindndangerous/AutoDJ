"""Repeat settings: the related artist and related track limits."""

import pytest
from pydantic import ValidationError

from autodj.repeat_settings import RepeatSettings, ignore_unknown_repeat_keys


def test_related_artists_keep_the_artist_limits_of_older_settings() -> None:
    settings = RepeatSettings.model_validate({"artist": {"tracks": 3, "minutes": 30}})
    assert settings.related_artist.model_dump() == {"tracks": 3, "minutes": 30.0}
    own = RepeatSettings.model_validate(
        {"artist": {"tracks": 3}, "related_artist": {"tracks": 12, "minutes": 5}}
    )
    assert own.related_artist.model_dump() == {"tracks": 12, "minutes": 5.0}


def test_defaults() -> None:
    settings = RepeatSettings()
    assert settings.related_artist.model_dump() == {"tracks": 8, "minutes": 0.0}
    assert settings.related_track.model_dump() == {"tracks": 20, "minutes": 0.0}
    assert settings.use_related_tracks is False


@pytest.mark.parametrize("rule", ["related_artist", "related_track"])
def test_related_track_counts_are_capped(rule: str) -> None:
    with pytest.raises(ValidationError, match=f"{rule} tracks must be between 0 and 5000"):
        RepeatSettings.model_validate({rule: {"tracks": 5001}})


def test_unknown_keys_inside_the_new_rules_are_dropped() -> None:
    cleaned = ignore_unknown_repeat_keys({"related_track": {"tracks": 4, "old": 1}})
    assert cleaned == {"related_track": {"tracks": 4}}
