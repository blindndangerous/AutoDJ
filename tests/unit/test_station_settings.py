"""Station settings are editable without constructing a Player."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from autodj.settings_bodies import PlaybackSettingsBody
from autodj.station_settings import StationSettings
from tests.unit.test_runtime_state import _make_player


def test_offline_settings_load_edit_save_without_player(tmp_path, monkeypatch) -> None:
    """ADR-0005-03."""
    cfg = _make_player()._cfg
    paths = SimpleNamespace(state_dir=tmp_path, slug="nightowl")
    (tmp_path / "web_state.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "transition": "none",
                "playback": {"crossfade_seconds": 7.0, "volume": 0.4},
                "eq": {"low": 1.3},
            }
        ),
        encoding="utf-8",
    )

    def forbidden_player(*args, **kwargs):
        raise AssertionError("offline settings must not construct Player")

    monkeypatch.setattr("autodj.player.Player", forbidden_player)
    settings = StationSettings(cfg, paths=paths)
    settings.load()
    assert cfg.playback.crossfade_seconds == 7.0
    assert settings.get_settings()["playback"]["volume"] == 0.4
    assert settings.get_eq()["low"] == 1.3

    settings.set_playback_settings(PlaybackSettingsBody(crossfade_seconds=5.0))
    settings.set_volume(0.8)
    settings.set_eq(mid=1.5)
    settings.save()

    restored = StationSettings(cfg, paths=paths)
    restored.load()
    assert restored.get_settings()["playback"]["crossfade_seconds"] == 5.0
    assert restored.get_settings()["playback"]["volume"] == 0.8
    assert restored.get_eq() == {"low": 1.3, "mid": 1.5, "high": 1.0}


def test_offline_settings_use_running_setter_validation(tmp_path) -> None:
    """ADR-0005-03."""
    settings = StationSettings(_make_player()._cfg, paths=SimpleNamespace(state_dir=tmp_path))
    with pytest.raises(ValueError, match="unknown transition effect"):
        settings.set_transition("not-an-effect")
    with pytest.raises(ValueError):
        settings.set_playback_settings(PlaybackSettingsBody(post_queue_seed="invalid"))
