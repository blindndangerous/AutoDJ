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
def test_active_profile_identity_is_present_without_a_profile_query_and_is_station_owned(
    rig: Any,
) -> None:
    client, *_ = rig

    assert client.get(f"{OWL}/settings").json()["active_profile"] is None
    assert "profile" not in client.get(f"{OWL}/settings").json()
    assert client.get("/api/settings").json()["active_profile"] is None

    created = client.post(f"{OWL}/profiles", json={"name": "Night"})
    assert created.status_code == 200, created.text
    applied = client.post(f"{OWL}/profiles/Night/apply")
    assert applied.status_code == 200, applied.text
    started = client.post(f"{OWL}/start")
    assert started.status_code == 200, started.text

    settings = client.get(f"{OWL}/settings").json()
    assert settings["active_profile"] == "Night"
    assert "profile" not in settings
    assert client.get(f"{OWL}/settings", params={"profile": ""}).json()["active_profile"] == "Night"
    assert client.get(f"{OWL}/status").json()["active_profile"] == "Night"
    assert client.get("/api/settings").json()["active_profile"] is None


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
    if target:
        assert station.time_checks.settings.model_dump() == active_before
    else:
        assert station.time_checks.settings.enabled is enabled


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_profile_defaults_editing_apply_reset_and_station_isolation(
    rig: Any,
) -> None:
    """ADR-0018-01, ADR-0018-03, ADR-0018-05."""
    client, _library, _cfg, _song, _closes = rig
    registry = client.app.state.stations
    nightowl = registry.editable("nightowl")
    default = registry.default

    # An explicitly empty query target edits saved defaults and resolves to
    # the stopped station's current values. The default target is active now.
    defaults = settings_view(client, "")
    assert defaults["profile"] == ""
    assert defaults["active_profile"] is None
    default_crossfade = 4.25
    saved_defaults = patch_profile(
        client, defaults, "", {"playback": {"crossfade_seconds": default_crossfade}}
    )
    assert saved_defaults.status_code == 200, saved_defaults.text
    assert nightowl.cfg.playback.crossfade_seconds == default_crossfade

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

    # Saving a value equal to defaults removes that override while the
    # defaults target is active; the inactive profile is not applied.
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
    assert nightowl.cfg.playback.crossfade_seconds == default_crossfade
    assert nightowl.cfg.playback.beat_sync_fx is True

    activated = client.post(f"{OWL}/profiles/Evening/apply")
    assert activated.status_code == 200, activated.text
    assert activated.json()["active"] == "Evening"
    assert nightowl.cfg.playback.crossfade_seconds == default_crossfade
    assert nightowl.cfg.playback.beat_sync_fx is False
    assert nightowl.cfg.playback.fade_in_seconds == 0

    # A saved-default edit immediately updates inherited active values while
    # preserving the profile's explicit playback overrides and active identity.
    active_defaults = settings_view(client, "")
    later_crossfade = 5.25
    changed_defaults = patch_profile(
        client,
        active_defaults,
        "",
        {"playback": {"crossfade_seconds": later_crossfade}},
    )
    assert changed_defaults.status_code == 200, changed_defaults.text
    assert client.get(f"{OWL}/profiles").json()["active"] == "Evening"
    assert nightowl.cfg.playback.crossfade_seconds == later_crossfade
    assert nightowl.cfg.playback.beat_sync_fx is False
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
def test_saving_a_profile_after_another_profile_becomes_active_does_not_switch_back(
    rig: Any,
) -> None:
    client, *_ = rig
    station = client.app.state.stations.editable("nightowl")
    assert (
        client.post(f"{OWL}/profiles", json={"name": "A", "crossfade_seconds": 7.0}).status_code
        == 200
    )
    assert (
        client.post(f"{OWL}/profiles", json={"name": "B", "crossfade_seconds": 8.0}).status_code
        == 200
    )
    assert client.post(f"{OWL}/profiles/A/apply").status_code == 200
    assert client.post(f"{OWL}/profiles/B/apply").status_code == 200
    assert station.cfg.playback.crossfade_seconds == 8.0

    editing_a = settings_view(client, "A")
    saved_a = patch_profile(client, editing_a, "A", {"playback": {"crossfade_seconds": 9.0}})

    assert saved_a.status_code == 200, saved_a.text
    assert settings_view(client, "A")["values"]["playback"]["crossfade_seconds"] == 9.0
    assert client.get(f"{OWL}/profiles").json()["active"] == "B"
    assert station.cfg.playback.crossfade_seconds == 8.0


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_active_profile_save_reports_partial_apply_failure_after_persisting(
    rig: Any, monkeypatch
) -> None:
    client, *_ = rig
    station = client.app.state.stations.editable("nightowl")
    created = client.post(f"{OWL}/profiles", json={"name": "Partial", "crossfade_seconds": 8.0})
    assert created.status_code == 200, created.text
    applied = client.post(f"{OWL}/profiles/Partial/apply")
    assert applied.status_code == 200, applied.text

    def fail_save(_settings):
        raise OSError("disk write failed")

    monkeypatch.setattr(station.time_checks, "save", fail_save)
    view = settings_view(client, "Partial")
    response = patch_profile(
        client,
        view,
        "Partial",
        {
            "playback": {"crossfade_seconds": 9.0},
            "time_checks": {"enabled": not view["values"]["time_checks"]["enabled"]},
        },
    )

    assert response.status_code == 207, response.text
    assert "Settings were saved" in response.json()["failed"]["playback"]
    assert "time checks" in response.json()["failed"]["playback"]
    assert "disk write failed" in response.json()["failed"]["time_checks"]
    saved = json.loads((station.paths.profiles / "Partial.json").read_text(encoding="utf-8"))
    assert saved["settings"]["playback"]["crossfade_seconds"] == 9.0
    assert (
        saved["settings"]["time_checks"]["enabled"] is not view["values"]["time_checks"]["enabled"]
    )
    assert settings_view(client, "Partial")["values"]["playback"]["crossfade_seconds"] == 9.0
    assert client.get(f"{OWL}/profiles").json()["active"] == "Partial"
    assert station.cfg.playback.crossfade_seconds == 9.0


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

    selection_before = station.paths.profile_selection.read_bytes()
    monkeypatch.setattr(station.time_checks, "save", fail_save)
    response = client.post(f"{OWL}/profiles/Partial/apply")
    assert response.status_code == 503
    assert "some settings may already be active" in response.json()["detail"]
    assert "time checks" in response.json()["detail"]
    assert station.cfg.playback.crossfade_seconds == 8.0
    assert client.get(f"{OWL}/profiles").json()["active"] is None
    assert station.paths.profile_selection.read_bytes() == selection_before


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_cart_rename_keeps_defaults_and_profiles_usable(rig):
    client, *_ = rig
    station = client.app.state.stations.editable("nightowl")
    defaults = settings_view(client, "")
    old = defaults["values"]["cart"]["page"]
    store = ProfileStore(station.paths.profiles)
    store.save(ProfileSnapshot(name="Saved", cart_page=old))
    response = client.put(f"{OWL}/carts/pages/1/name", json={"name": "Renamed"})
    assert response.status_code == 200, response.text
    assert settings_view(client, "")["values"]["cart"]["page"] == "Renamed"
    assert settings_view(client, "Saved")["values"]["cart"]["page"] == "Renamed"
    response = patch_profile(
        client, settings_view(client, ""), "", {"playback": {"crossfade_seconds": 5.0}}
    )
    assert response.status_code == 200, response.text
    assert client.post(f"{OWL}/profiles/Saved/apply").status_code == 200
    assert (
        client.get("/api/settings", params={"profile": ""}).json()["values"]["cart"]["page"]
        == "Page 1"
    )


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_active_profile_cart_save_notifies_other_pages(rig, monkeypatch):
    from unittest.mock import AsyncMock

    client, *_ = rig
    station = client.app.state.stations.editable("nightowl")
    changed = AsyncMock(wraps=station.carts.publish)
    monkeypatch.setattr(station.carts, "publish", changed)
    view = settings_view(client, "")
    response = patch_profile(
        client, view, "", {"cart": {"page": station.carts.store.wall.pages[1].name}}
    )
    assert response.status_code == 200, response.text
    changed.assert_awaited_once_with({"type": "cart_changed"})


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_active_profile_delete_rejects_different_letter_case(rig):
    client, *_ = rig
    assert client.post(f"{OWL}/profiles", json={"name": "Night"}).status_code == 200
    assert client.post(f"{OWL}/profiles/Night/apply").status_code == 200
    response = client.delete(f"{OWL}/profiles/night")
    assert response.status_code == 409, response.text
    assert "Night" in client.get(f"{OWL}/profiles").json()["profiles"]


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_clockwheel_rename_and_delete_update_saved_default_and_profile_references(rig):
    client, *_ = rig
    created = client.post(
        f"{OWL}/library/clock",
        json={
            "version": 1,
            "name": "Morning",
            "resync_minutes": [0],
            "steps": [
                {
                    "type": "music",
                    "match": "all",
                    "conditions": [{"field": "genre", "operator": "is_not", "value": "Slow Jam"}],
                }
            ],
        },
    )
    assert created.status_code == 200, created.text
    assert client.post(f"{OWL}/profiles", json={"name": "Saved"}).status_code == 200
    for profile in ("Saved", ""):
        view = settings_view(client, profile)
        saved = patch_profile(client, view, profile, {"clock": {"name": "Morning"}})
        assert saved.status_code == 200, saved.text

    renamed = client.post(f"{OWL}/library/clock/Morning/rename", json={"name": "Day"})
    assert renamed.status_code == 200, renamed.text
    assert settings_view(client, "")["values"]["clock"]["name"] == "Day"
    assert settings_view(client, "Saved")["values"]["clock"]["name"] == "Day"

    deleted = client.delete(f"{OWL}/library/clock/Day")
    assert deleted.status_code == 200, deleted.text
    assert settings_view(client, "")["values"]["clock"]["name"] is None
    assert settings_view(client, "Saved")["values"]["clock"]["name"] is None


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_deleting_pack_removes_it_from_saved_enabled_pack_references(rig):
    client, *_ = rig
    station = client.app.state.stations.editable("nightowl")
    (station.time_checks.root / "SavedPack").mkdir(parents=True)
    station.time_checks.rescan()
    assert client.post(f"{OWL}/profiles", json={"name": "Saved"}).status_code == 200
    for profile in ("Saved", ""):
        view = settings_view(client, profile)
        result = patch_profile(
            client,
            view,
            profile,
            {"time_checks": {"enabled_packs": ["SavedPack"]}},
        )
        assert result.status_code == 200, result.text

    removed = client.delete(f"{OWL}/time-checks/packs/SavedPack")
    assert removed.status_code == 200, removed.text
    for profile in ("", "Saved"):
        assert settings_view(client, profile)["values"]["time_checks"]["enabled_packs"] == []


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_deleting_pack_refuses_when_a_saved_profile_is_corrupt(rig: Any) -> None:
    client, *_ = rig
    station = client.app.state.stations.editable("nightowl")
    pack = station.time_checks.root / "ValidPack"
    pack.mkdir(parents=True)
    pack_file = pack / "hello.wav"
    pack_bytes = b"pack audio stays intact"
    pack_file.write_bytes(pack_bytes)
    station.time_checks.rescan()
    corrupt_profile = station.paths.profiles / "Broken.json"
    corrupt_bytes = b"{ invalid profile json"
    corrupt_profile.parent.mkdir(parents=True, exist_ok=True)
    corrupt_profile.write_bytes(corrupt_bytes)
    response = client.delete(f"{OWL}/time-checks/packs/ValidPack")

    assert response.status_code == 400, response.text
    assert "Broken" in response.json()["detail"]
    assert "invalid" in response.json()["detail"]
    assert corrupt_profile.read_bytes() == corrupt_bytes
    assert pack.is_dir()
    assert pack_file.read_bytes() == pack_bytes


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_deleting_a_shared_pack_removes_it_from_every_station_that_finds_it(rig):
    client, *_ = rig
    stations = client.app.state.stations
    default = stations.editable("default")
    owl = stations.editable("nightowl")
    # The default station's time checks folder is the library's shared one.
    (default.time_checks.root / "SharedPack").mkdir(parents=True)
    # A pack in Night Owl's own folder keeps its tick.
    (owl.time_checks.root / "OwnPack").mkdir(parents=True)
    for station in (default, owl):
        station.time_checks.rescan()
    assert owl.time_checks.packs["SharedPack"].source == "shared"
    assert client.post(f"{OWL}/profiles", json={"name": "Saved"}).status_code == 200
    for profile in ("Saved", ""):
        view = settings_view(client, profile)
        result = patch_profile(
            client, view, profile, {"time_checks": {"enabled_packs": ["SharedPack", "OwnPack"]}}
        )
        assert result.status_code == 200, result.text

    removed = client.delete("/api/time-checks/packs/SharedPack")
    assert removed.status_code == 200, removed.text
    for profile in ("", "Saved"):
        view = settings_view(client, profile)
        assert view["values"]["time_checks"]["enabled_packs"] == ["OwnPack"]
        saved = patch_profile(client, view, profile, {"playback": {"liners_every_n_songs": 3}})
        assert saved.status_code == 200, saved.text


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_a_shared_pack_stays_while_a_station_finding_it_is_starting(rig):
    import asyncio

    client, *_ = rig
    registry = client.app.state.stations
    default = registry.editable("default")
    (default.time_checks.root / "SharedPack").mkdir(parents=True)
    default.time_checks.rescan()

    async def during_start() -> None:
        async with registry._locks["nightowl"]:
            refused = await asyncio.to_thread(client.delete, "/api/time-checks/packs/SharedPack")
            assert refused.status_code == 409, refused.text
            assert "Night Owl Radio" in refused.json()["detail"]

    asyncio.run(during_start())
    assert (default.time_checks.root / "SharedPack").is_dir()
