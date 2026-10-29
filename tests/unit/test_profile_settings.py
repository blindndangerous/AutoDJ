"""Station profile overrides stay separate from active settings until applied."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from autodj.carts import CartStore
from autodj.clockwheel_store import ClockwheelStore
from autodj.config import (
    AutoDJConfig,
    HuggingFaceConfig,
    IndexConfig,
    LibraryConfig,
    ModelConfig,
    PlaybackConfig,
)
from autodj.profile_settings import ProfileSettings, legacy_groups
from autodj.profiles import ProfileSnapshot
from autodj.station_settings import StationSettings
from autodj.stations import StationPaths
from autodj.time_checks import SettingsBody as TimeCheckSettings


def _station(root, slug="default", cfg=None):
    music = root / "music"
    music.mkdir(parents=True, exist_ok=True)
    cfg = cfg or AutoDJConfig(
        library=LibraryConfig(music, None, ["flac"]),
        index=IndexConfig(root / "index", root / "models"),
        playback=PlaybackConfig(),
        model=ModelConfig(),
        huggingface=HuggingFaceConfig(None),
        config_path=root / "config.toml",
    )
    paths = StationPaths.for_station(cfg, slug) if slug != "default" else StationPaths.default(cfg)
    settings = StationSettings(cfg, paths=paths)
    station = SimpleNamespace(
        cfg=cfg,
        paths=paths,
        bridge=SimpleNamespace(settings=settings),
        player=None,
        time_checks=SimpleNamespace(check_choices=lambda _body: None),
        ledger=None,
        carts=None,
        breaks=None,
    )
    return station


def _defaults(manager, *, crossfade=3.0, beat_sync=True, bpm_lo=80.0, bpm_hi=120.0):
    manager._write(
        manager.defaults_path,
        {
            "playback": {"crossfade_seconds": crossfade, "beat_sync_fx": beat_sync},
            "bpm_range": {"lo": bpm_lo, "hi": bpm_hi},
        },
    )


def _create(manager, name):
    manager.create(ProfileSnapshot(name=name))


def test_save_does_not_change_active_settings_and_profiles_resolve_from_defaults(tmp_path):
    station = _station(tmp_path)
    manager = ProfileSettings(station)
    _defaults(manager)
    before = station.cfg.playback.crossfade_seconds

    _create(manager, "A")
    _create(manager, "B")
    manager.save("A", {"playback": {"crossfade_seconds": 6.0}})
    manager.save("B", {"playback": {"beat_sync_fx": False}})

    assert station.cfg.playback.crossfade_seconds == before
    assert manager.resolve("A")["playback"] == {
        "crossfade_seconds": 6.0,
        "beat_sync_fx": True,
    }
    assert manager.resolve("B")["playback"] == {
        "crossfade_seconds": 3.0,
        "beat_sync_fx": False,
    }

    manager.apply("A")
    assert station.cfg.playback.crossfade_seconds == 6.0
    manager.apply("B")
    assert station.cfg.playback.crossfade_seconds == 3.0
    assert station.cfg.playback.beat_sync_fx is False


def test_offline_default_edit_is_saved_without_changing_active_config(tmp_path):
    station = _station(tmp_path)
    manager = ProfileSettings(station)
    _defaults(manager)
    active_before = station.cfg.playback.crossfade_seconds

    manager.save("", {"playback": {"crossfade_seconds": 4.0}})

    assert station.cfg.playback.crossfade_seconds == active_before
    assert manager.defaults()["playback"]["crossfade_seconds"] == 4.0


def test_override_diff_keeps_false_zero_and_explicit_clear(tmp_path):
    """ADR-0018-01."""
    manager = ProfileSettings(_station(tmp_path))
    _defaults(manager)
    _create(manager, "Explicit")

    manager.save(
        "Explicit",
        {
            "playback": {"beat_sync_fx": False, "crossfade_seconds": 0.0},
            "bpm_range": {"lo": None, "hi": 110.0},
        },
    )

    saved = manager.store.load("Explicit").settings
    assert saved == {
        "playback": {"beat_sync_fx": False, "crossfade_seconds": 0.0},
        "bpm_range": {"lo": None, "hi": 110.0},
    }
    assert manager.resolve("Explicit")["bpm_range"] == {"lo": None, "hi": 110.0}


def test_legacy_profile_keeps_only_non_null_fields_as_overrides(tmp_path):
    """ADR-0018-01."""
    manager = ProfileSettings(_station(tmp_path))
    _defaults(manager)
    manager.create(
        ProfileSnapshot(
            name="Legacy",
            bpm_lo=None,
            bpm_hi=100.0,
            crossfade_seconds=None,
            beat_sync_fx=False,
            pure_shuffle=False,
        )
    )

    resolved = manager.resolve("Legacy")
    assert resolved["bpm_range"] == {"lo": 80.0, "hi": 100.0}
    assert resolved["playback"] == {
        "crossfade_seconds": 3.0,
        "beat_sync_fx": False,
        "pick_mode": "similarity",
    }


def test_restore_applies_saved_selection_and_persists_for_offline_reload(tmp_path):
    station = _station(tmp_path)
    manager = ProfileSettings(station)
    _defaults(manager)
    manager.create(ProfileSnapshot(name="Night", settings={"playback": {"crossfade_seconds": 8.0}}))
    manager.selection_path.parent.mkdir(parents=True, exist_ok=True)
    manager.selection_path.write_text('{"profile":"Night"}', encoding="utf-8")

    manager.restore()

    assert station.cfg.playback.crossfade_seconds == 8.0
    assert manager.active == "Night"
    assert (
        json.loads(station.paths.web_state.read_text(encoding="utf-8"))["playback"][
            "crossfade_seconds"
        ]
        == 8.0
    )

    restored_settings = StationSettings(copy.deepcopy(station.cfg), paths=station.paths)
    restored_settings.load()
    assert restored_settings.cfg.playback.crossfade_seconds == 8.0


def test_same_profile_name_and_selection_are_isolated_by_station(tmp_path):
    cfg = _station(tmp_path).cfg
    first = ProfileSettings(_station(tmp_path, "nightowl", cfg))
    second = ProfileSettings(_station(tmp_path, "daybreak", cfg))
    _defaults(first, crossfade=3.0)
    _defaults(second, crossfade=4.0)
    _create(first, "Shared name")
    _create(second, "Shared name")
    first.save("Shared name", {"playback": {"crossfade_seconds": 6.0}})
    second.save("Shared name", {"playback": {"crossfade_seconds": 9.0}})

    assert first.resolve("Shared name")["playback"]["crossfade_seconds"] == 6.0
    assert second.resolve("Shared name")["playback"]["crossfade_seconds"] == 9.0
    first.selection_path.parent.mkdir(parents=True, exist_ok=True)
    first.selection_path.write_text('{"profile":"Shared name"}', encoding="utf-8")
    assert not second.selection_path.exists()


def test_missing_profile_defaults_are_captured_from_station_owners(tmp_path):
    station = _station(tmp_path)
    station.time_checks = SimpleNamespace(
        settings=TimeCheckSettings(
            enabled=False,
            hour_format="12",
            rule="off",
            enabled_packs=[],
            pack_order="rotate",
        )
    )
    station.ledger = SimpleNamespace(get_setting=lambda _key, default: default)
    station.carts = SimpleNamespace(store=CartStore(station.paths.state_dir))
    ClockwheelStore(station.paths.clockwheels).save(
        {
            "version": 1,
            "name": "Morning",
            "steps": [{"type": "track", "source": "folder1", "path": "song.flac"}],
            "resync_minutes": [0],
        }
    )
    station.ledger = SimpleNamespace(get_setting=lambda _key, default: {"name": "Morning"})
    manager = ProfileSettings(station)

    values = manager.defaults()

    assert values["playback"]["crossfade_seconds"] == station.cfg.playback.crossfade_seconds
    assert values["time_checks"] == station.time_checks.settings.model_dump()
    assert values["clock"] == {"name": "Morning"}
    assert values["cart"] == {"page": station.carts.store.wall.pages[0].name}
    assert manager.defaults_path.is_file()
    manager.validate({"clock": {"name": "Morning"}})


def test_invalid_profile_is_rejected_before_profile_or_defaults_are_written(tmp_path):
    station = _station(tmp_path)
    manager = ProfileSettings(station)
    _defaults(manager)
    defaults_before = manager.defaults_path.read_bytes()
    active_before = station.cfg.playback.crossfade_seconds

    with pytest.raises(ValueError):
        manager.save("", {"playback": {"post_queue_seed": "invalid"}})

    assert manager.store.list_names() == []
    assert manager.defaults_path.read_bytes() == defaults_before
    assert station.cfg.playback.crossfade_seconds == active_before


def test_restart_reads_offline_edits_to_defaults_and_selected_profile(tmp_path):
    """ADR-0018-04: the next startup reads JSON edited while the server was stopped."""
    manager = ProfileSettings(_station(tmp_path))
    _defaults(manager)
    manager.create(ProfileSnapshot(name="Night", settings={"playback": {"beat_sync_fx": False}}))
    manager.apply("Night")

    # Simulate edits on disk after shutting down, without live file watching.
    defaults = json.loads(manager.defaults_path.read_text(encoding="utf-8"))
    defaults["playback"]["crossfade_seconds"] = 5.0
    manager.defaults_path.write_text(json.dumps(defaults), encoding="utf-8")
    profile_path = manager.store.root / "Night.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["settings"]["playback"]["music_speed_percent"] = 2.0
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    restarted = _station(tmp_path)
    restored = ProfileSettings(restarted)
    restored.restore()
    assert restored.active == "Night"
    assert restarted.cfg.playback.crossfade_seconds == 5.0
    assert restarted.cfg.playback.beat_sync_fx is False
    assert restarted.cfg.playback.music_speed_percent == 2.0


def test_restart_applies_saved_defaults_before_any_profile_has_been_selected(tmp_path):
    """ADR-0018-04."""
    manager = ProfileSettings(_station(tmp_path))
    _defaults(manager, crossfade=4.0)
    assert not manager.selection_path.exists()

    restarted = _station(tmp_path)
    restored = ProfileSettings(restarted)
    restored.restore()
    assert restored.active is None
    assert restarted.cfg.playback.crossfade_seconds == 4.0


def test_legacy_cart_page_is_preserved_as_an_override():
    assert legacy_groups(ProfileSnapshot(name="Legacy", cart_page="Evening")) == {
        "cart": {"page": "Evening"}
    }


@pytest.mark.parametrize("target", ["", "Evening"])
def test_partial_time_check_edits_merge_with_saved_target(tmp_path, target):
    manager = ProfileSettings(_station(tmp_path))
    _defaults(manager)
    checks = TimeCheckSettings(
        enabled=True, hour_format="12", rule="off", enabled_packs=[], pack_order="rotate"
    ).model_dump()
    manager._write(manager.defaults_path, {**manager.defaults(), "time_checks": checks})
    if target:
        manager.create(
            ProfileSnapshot(name=target, settings={"time_checks": {"hour_format": "24"}})
        )

    saved = manager.save(target, {"time_checks": {"enabled": False}})

    assert saved["time_checks"] == {
        **checks,
        "enabled": False,
        "hour_format": "24" if target else "12",
    }
    assert manager.resolve(target) == saved
    if target:
        assert manager.store.load(target).settings == {
            "time_checks": {"enabled": False, "hour_format": "24"}
        }


def test_missing_time_check_pack_prevents_any_profile_application(tmp_path):
    station = _station(tmp_path)
    station.time_checks.packs = {}
    manager = ProfileSettings(station)
    _defaults(manager)
    checks = TimeCheckSettings(
        enabled=True, hour_format="12", rule="off", enabled_packs=["Missing"], pack_order="rotate"
    ).model_dump()
    # Bypass interactive validation to represent an offline JSON edit.
    manager.store.save(
        ProfileSnapshot(
            name="Offline", settings={"playback": {"crossfade_seconds": 9.0}, "time_checks": checks}
        )
    )
    before = station.cfg.playback.crossfade_seconds

    with pytest.raises(ValueError, match="No voice pack named Missing"):
        manager.apply("Offline")

    assert station.cfg.playback.crossfade_seconds == before
    assert manager.active is None
    assert not manager.selection_path.exists()


@pytest.mark.parametrize(
    "group, field", [("playback", "crossfade_seconds"), ("djmix", "beatmatch"), ("eq", "enabled")]
)
def test_unsupported_json_null_does_not_retain_a_previous_profile_value(tmp_path, group, field):
    manager = ProfileSettings(_station(tmp_path))
    _defaults(manager)
    manager.store.save(ProfileSnapshot(name="Invalid", settings={group: {field: None}}))
    before = manager.station.cfg.playback.crossfade_seconds
    with pytest.raises(ValueError, match="remove the override to inherit defaults"):
        manager.apply("Invalid")
    assert manager.station.cfg.playback.crossfade_seconds == before
    assert manager.active is None
    assert not manager.selection_path.exists()
