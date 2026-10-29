"""Saved station defaults and profile overrides through station routes."""

from __future__ import annotations

import json
from typing import Any

import pytest

from autodj.profiles import ProfileSnapshot, ProfileStore
from tests.integration.test_multi_station import rig as rig

OWL = "/api/stations/nightowl"


def settings_view(client: Any, profile: str, *, base: str = OWL) -> dict[str, Any]:
    response = client.get(f"{base}/settings", params={"profile": profile})
    assert response.status_code == 200, response.text
    return response.json()


def patch_profile(
    client: Any, view: dict[str, Any], profile: str, changes: dict[str, Any], *, base: str = OWL
) -> Any:
    return client.patch(
        f"{base}/settings",
        json={"revision": view["revision"], "profile": profile, "changes": changes},
    )


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
@pytest.mark.parametrize("target", ["", "Evening"])
def test_partial_time_check_profile_draft_can_be_saved(rig: Any, target: str) -> None:
    client, *_ = rig
    station = client.app.state.stations.editable("nightowl")
    active_before = station.time_checks.settings.model_dump()
    if target:
        assert client.post(f"{OWL}/profiles", json={"name": target}).status_code == 200
    view = settings_view(client, target)
    enabled = not view["values"]["time_checks"]["enabled"]

    response = patch_profile(client, view, target, {"time_checks": {"enabled": enabled}})

    assert response.status_code == 200, response.text
    assert settings_view(client, target)["values"]["time_checks"] == {
        **view["values"]["time_checks"],
        "enabled": enabled,
    }
    assert station.time_checks.settings.model_dump() == active_before


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_profile_defaults_editing_apply_reset_and_station_isolation(
    rig: Any,
) -> None:
    """ADR-0018-01, ADR-0018-03, ADR-0018-05."""
    client, _library, _cfg, _song, _closes = rig
    registry = client.app.state.stations
    nightowl = registry.editable("nightowl")
    default = registry.default
    original_crossfade = nightowl.cfg.playback.crossfade_seconds

    # An explicitly empty query target edits saved defaults and resolves to
    # the stopped station's current values.
    defaults = settings_view(client, "")
    assert defaults["profile"] == ""
    assert defaults["active_profile"] is None
    default_crossfade = 4.25
    saved_defaults = patch_profile(
        client, defaults, "", {"playback": {"crossfade_seconds": default_crossfade}}
    )
    assert saved_defaults.status_code == 200, saved_defaults.text
    assert nightowl.cfg.playback.crossfade_seconds == original_crossfade

    created = client.post(
        f"{OWL}/profiles",
        json={
            "name": "Evening",
            "crossfade_seconds": 9.0,
            "beat_sync_fx": False,
            "fade_in_seconds": 0,
        },
    )
    assert created.status_code == 200, created.text
    evening = settings_view(client, "Evening")
    assert evening["values"]["playback"]["crossfade_seconds"] == 9.0
    assert evening["values"]["playback"]["beat_sync_fx"] is False
    assert evening["values"]["playback"]["fade_in_seconds"] == 0
    assert evening["active_profile"] is None

    legacy_profile = nightowl.paths.profiles / "Legacy.json"
    legacy_profile.parent.mkdir(parents=True, exist_ok=True)
    legacy_profile.write_text(
        json.dumps(
            {
                "name": "Legacy",
                "crossfade_seconds": None,
                "beat_sync_fx": False,
                "fade_in_seconds": 0,
            }
        ),
        encoding="utf-8",
    )
    legacy = settings_view(client, "Legacy")
    assert legacy["values"]["playback"]["crossfade_seconds"] == default_crossfade
    assert legacy["values"]["playback"]["beat_sync_fx"] is False
    assert legacy["values"]["playback"]["fade_in_seconds"] == 0

    # Saving a value equal to defaults removes that override, without
    # applying either the profile or its remaining explicit false value.
    inherited = patch_profile(
        client,
        evening,
        "Evening",
        {"playback": {"crossfade_seconds": default_crossfade}},
    )
    assert inherited.status_code == 200, inherited.text
    saved_profile = json.loads(
        (nightowl.paths.profiles / "Evening.json").read_text(encoding="utf-8")
    )
    assert saved_profile["settings"] == {
        "playback": {"beat_sync_fx": False, "fade_in_seconds": 0.0}
    }
    assert nightowl.cfg.playback.crossfade_seconds == original_crossfade
    assert nightowl.cfg.playback.beat_sync_fx is True

    activated = client.post(f"{OWL}/profiles/Evening/apply")
    assert activated.status_code == 200, activated.text
    assert activated.json()["active"] == "Evening"
    assert nightowl.cfg.playback.crossfade_seconds == default_crossfade
    assert nightowl.cfg.playback.beat_sync_fx is False
    assert nightowl.cfg.playback.fade_in_seconds == 0

    # A saved-default edit leaves an active profile alone; its next Apply
    # resolves against the new defaults because crossfade is inherited.
    active_defaults = settings_view(client, "")
    later_crossfade = 5.25
    changed_defaults = patch_profile(
        client,
        active_defaults,
        "",
        {"playback": {"crossfade_seconds": later_crossfade}},
    )
    assert changed_defaults.status_code == 200, changed_defaults.text
    assert nightowl.cfg.playback.crossfade_seconds == default_crossfade
    reapplied = client.post(f"{OWL}/profiles/Evening/apply")
    assert reapplied.status_code == 200, reapplied.text
    assert nightowl.cfg.playback.crossfade_seconds == later_crossfade
    assert nightowl.cfg.playback.beat_sync_fx is False

    # Reset only changes the stored overrides, even for the active profile.
    reset = client.post(f"{OWL}/profiles/Evening/reset")
    assert reset.status_code == 200, reset.text
    reset_profile = json.loads(
        (nightowl.paths.profiles / "Evening.json").read_text(encoding="utf-8")
    )
    assert reset_profile["settings"] == {}
    assert nightowl.cfg.playback.beat_sync_fx is False
    active_delete = client.delete(f"{OWL}/profiles/Evening")
    assert active_delete.status_code == 409

    applied_defaults = client.post(f"{OWL}/profiles/apply-defaults")
    assert applied_defaults.status_code == 200, applied_defaults.text
    assert applied_defaults.json()["active"] is None
    assert nightowl.cfg.playback.crossfade_seconds == later_crossfade
    assert nightowl.cfg.playback.beat_sync_fx is True

    # Identically named profiles are station-owned, with separate overrides
    # and active identities for the default and Night Owl Radio.
    default_profile = client.post(
        "/api/profiles", json={"name": "SharedName", "crossfade_seconds": 2.75}
    )
    owl_profile = client.post(
        f"{OWL}/profiles", json={"name": "SharedName", "crossfade_seconds": 7.25}
    )
    assert default_profile.status_code == owl_profile.status_code == 200
    assert (
        settings_view(client, "SharedName", base="/api")["values"]["playback"]["crossfade_seconds"]
        == 2.75
    )
    assert settings_view(client, "SharedName")["values"]["playback"]["crossfade_seconds"] == 7.25
    assert (default.paths.profiles / "SharedName.json") != (
        nightowl.paths.profiles / "SharedName.json"
    )
    assert client.post("/api/profiles/SharedName/apply").status_code == 200
    assert client.post(f"{OWL}/profiles/SharedName/apply").status_code == 200
    assert default.cfg.playback.crossfade_seconds == 2.75
    assert nightowl.cfg.playback.crossfade_seconds == 7.25
    assert client.get("/api/profiles").json()["active"] == "SharedName"
    assert client.get(f"{OWL}/profiles").json()["active"] == "SharedName"

    scheduled = client.post(
        "/api/schedule",
        json={
            "name": "Apply profile",
            "action": "profile",
            "params": {"name": "SharedName"},
            "repeat": "daily",
            "time": "09:00",
        },
    )
    assert scheduled.status_code == 201, scheduled.text
    run = client.post(f"/api/schedule/{scheduled.json()['id']}/run")
    assert run.status_code == 200, run.text
    assert run.json()["result"] == "ok"
    assert default.cfg.playback.crossfade_seconds == 2.75


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_invalid_harmonic_group_does_not_partially_apply_profile(
    rig: Any,
) -> None:
    """ADR-0018-03."""
    client, _library, _cfg, _song, _closes = rig
    nightowl = client.app.state.stations.editable("nightowl")
    settings_view(client, "")  # Persist the initial station defaults.
    original_crossfade = nightowl.cfg.playback.crossfade_seconds
    original_harmonic = nightowl.cfg.djmix.harmonic_mode
    bad_profile = ProfileStore(nightowl.paths.profiles)
    bad_profile.save(
        ProfileSnapshot(
            name="BadHarmonic",
            settings={
                "playback": {"crossfade_seconds": 9.0},
                "djmix": {"harmonic_mode": "not-a-mode"},
            },
        )
    )

    response = client.post(f"{OWL}/profiles/BadHarmonic/apply")

    assert response.status_code == 400, response.text
    assert nightowl.cfg.playback.crossfade_seconds == original_crossfade
    assert nightowl.cfg.djmix.harmonic_mode == original_harmonic
    assert client.get(f"{OWL}/profiles").json()["active"] is None


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_profile_reports_a_store_failure_after_playback_changed(rig: Any, monkeypatch) -> None:
    client, _library, _cfg, _song, _closes = rig
    station = client.app.state.stations.editable("nightowl")
    created = client.post(f"{OWL}/profiles", json={"name": "Partial", "crossfade_seconds": 8.0})
    assert created.status_code == 200, created.text

    def fail_save(_settings):
        raise OSError("disk write failed")

    monkeypatch.setattr(station.time_checks, "save", fail_save)
    response = client.post(f"{OWL}/profiles/Partial/apply")
    assert response.status_code == 503
    assert "some settings may already be active" in response.json()["detail"]
    assert "time checks" in response.json()["detail"]
    assert station.cfg.playback.crossfade_seconds == 8.0
    assert client.get(f"{OWL}/profiles").json()["active"] is None
    assert not station.paths.profile_selection.exists()
