"""Failed profile operations preserve station state and report recovery issues."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from autodj.profile_settings import ProfileSettings
from autodj.profiles import ProfileSnapshot
from tests.unit.test_profile_settings import _create, _defaults, _station


@pytest.mark.parametrize(
    ("field", "value"),
    [("enabled", None), ("dj_placement", None), ("min_intro_seconds", None)],
)
def test_break_settings_reject_null_for_non_nullable_fields(tmp_path, field, value):
    manager = ProfileSettings(_station(tmp_path))

    with pytest.raises(ValueError):
        manager.validate({"breaks": {field: value}})


def test_break_settings_allow_null_duck_depth(tmp_path):
    manager = ProfileSettings(_station(tmp_path))

    manager.validate({"breaks": {"duck_db": None}})


def test_failed_break_apply_restores_config_and_saved_pages_without_dropping_breaks(
    tmp_path, monkeypatch
):
    station = _station(tmp_path)
    manager = ProfileSettings(station)
    owner = manager.owner
    station.cfg.breaks.enabled = True
    station.cfg.breaks.dj_placement = "auto"
    station.cfg.breaks.min_intro_seconds = 3.0
    station.cfg.breaks.duck_db = -9.0
    owner.page_saved = {"stream"}
    before = {
        field: getattr(station.cfg.breaks, field)
        for field in ("enabled", "dj_placement", "min_intro_seconds", "duck_db")
    }
    station.breaks = Mock()
    monkeypatch.setattr(owner, "save", Mock(side_effect=OSError("disk full")))

    with pytest.raises(ValueError, match="Could not apply breaks settings"):
        manager.apply_values(
            {"breaks": {"enabled": False, "dj_placement": "standalone", "duck_db": None}}
        )

    assert {field: getattr(station.cfg.breaks, field) for field in before} == before
    assert owner.page_saved == {"stream"}
    station.breaks.drop_waiting_breaks.assert_not_called()


@pytest.mark.parametrize("broken", ["missing", "invalid"])
def test_restore_problem_does_not_mutate_settings_or_profile_files(tmp_path, broken):
    station = _station(tmp_path)
    manager = ProfileSettings(station)
    _defaults(manager)
    selection = "Missing" if broken == "missing" else "Night"
    if broken == "invalid":
        manager.store.save(
            ProfileSnapshot(name="Night", settings={"playback": {"crossfade_seconds": None}})
        )
    manager.selection_path.parent.mkdir(parents=True, exist_ok=True)
    manager.selection_path.write_text(json.dumps({"profile": selection}), encoding="utf-8")
    before_cfg = {
        "crossfade_seconds": station.cfg.playback.crossfade_seconds,
        "beat_sync_fx": station.cfg.playback.beat_sync_fx,
    }
    paths = [manager.defaults_path, manager.selection_path]
    if broken == "invalid":
        paths.append(manager.store.root / "Night.json")
    before_files = {path: path.read_bytes() for path in paths}

    manager.restore()

    assert manager.problem
    assert manager.active is None
    assert {
        "crossfade_seconds": station.cfg.playback.crossfade_seconds,
        "beat_sync_fx": station.cfg.playback.beat_sync_fx,
    } == before_cfg
    assert {path: path.read_bytes() for path in paths} == before_files


def test_successful_apply_clears_previous_profile_problem(tmp_path):
    manager = ProfileSettings(_station(tmp_path))
    _defaults(manager)
    _create(manager, "Night")
    manager.problem = "previous restore problem"

    manager.apply("Night")

    assert manager.problem is None
    assert manager.active == "Night"


def test_delete_blocks_case_insensitive_active_profile_name(tmp_path):
    manager = ProfileSettings(_station(tmp_path))
    _defaults(manager)
    _create(manager, "Night")
    profile_path = manager.store.root / "Night.json"
    before = profile_path.read_bytes()
    manager.apply("Night")

    with pytest.raises(ValueError):
        manager.delete("night")

    assert profile_path.read_bytes() == before


@pytest.mark.parametrize(
    ("kind", "before", "after"),
    [
        ("cart", {"cart": {"page": "Old"}}, {"cart": {"page": "New"}}),
        ("clock", {"clock": {"name": "Old"}}, {"clock": {"name": None}}),
        (
            "preset",
            {"playback": {"stereotool_preset": "Old"}},
            {"playback": {"stereotool_preset": "included"}},
        ),
        (
            "pack",
            {"time_checks": {"enabled_packs": ["Old", "Other"]}},
            {"time_checks": {"enabled_packs": ["Other"]}},
        ),
    ],
)
def test_resource_changes_update_defaults_and_explicit_profile_references(
    tmp_path, kind, before, after
):
    manager = ProfileSettings(_station(tmp_path))
    manager._write(manager.defaults_path, before)
    manager.store.save(ProfileSnapshot(name="Uses it", settings=before))
    manager.store.save(ProfileSnapshot(name="Inherits", settings={}))
    replacement = "New" if kind == "cart" else "included" if kind == "preset" else None

    with manager.changing_reference(kind, "Old", replacement):
        assert manager.defaults() == after
        assert manager.resolve("Uses it") == after
        assert manager.resolve("Inherits") == after

    assert manager.store.load("Inherits").settings == {}


def test_failed_resource_change_restores_exact_profile_documents(tmp_path):
    manager = ProfileSettings(_station(tmp_path))
    manager._write(manager.defaults_path, {"cart": {"page": "Old"}})
    manager.store.save(ProfileSnapshot(name="Legacy", cart_page="Old"))
    paths = [manager.defaults_path, manager.store.root / "Legacy.json"]
    before = {path: path.read_bytes() for path in paths}

    with (
        pytest.raises(OSError, match="rename failed"),
        manager.changing_reference("cart", "Old", "New"),
    ):
        assert manager.resolve("Legacy")["cart"]["page"] == "New"
        raise OSError("rename failed")

    assert {path: path.read_bytes() for path in paths} == before
