"""Paired schedule routes and the built-in scheduler actions."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from autodj.profiles import ProfileSnapshot, ProfileStore
from autodj.server import PlayerBridge, create_app

from ._helpers import _make_player_mock, _make_sim_mock

DAILY = {"name": "Morning", "action": "preset", "params": {"name": "winddown"}, "repeat": "daily"}


@pytest.fixture
def rig(tmp_path: Path) -> tuple[TestClient, PlayerBridge, Path]:
    active = tmp_path / "active"
    (active / "liners").mkdir(parents=True)
    (active / "liners" / "station-id.wav").write_bytes(b"RIFF")
    ProfileStore(tmp_path / "profiles").save(ProfileSnapshot(name="Evening", preset="winddown"))
    player = _make_player_mock()
    player._cfg.index.active_dir = str(active)
    player._cfg.server.station_timezone = "UTC"
    bridge = PlayerBridge(player=player, sim=_make_sim_mock())
    return TestClient(create_app(bridge)), bridge, active


def event(**changes: Any) -> dict[str, Any]:
    return {**DAILY, "time": "09:00", **changes}


def test_create_edit_enable_run_and_delete(rig: tuple[TestClient, PlayerBridge, Path]) -> None:
    client, _bridge, active = rig
    created = client.post("/api/schedule", json=event())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["repeat_text"] == "Every day at 09:00"
    assert body["action_text"] == "Switch preset: winddown"
    assert (
        json.loads((active / "schedule.json").read_text(encoding="utf-8"))["events"][0]["id"]
        == (body["id"])
    )
    url = f"/api/schedule/{body['id']}"

    edited = client.put(url, json=event(name="Morning show", timing="hard"))
    assert edited.status_code == 200, edited.text
    assert (edited.json()["name"], edited.json()["timing"]) == ("Morning show", "hard")
    assert client.post(f"{url}/enable", json={"enabled": False}).json()["enabled"] is False
    run = client.post(f"{url}/run").json()
    assert (run["event"], run["result"], run["summary"]) == (
        "Morning show",
        "ok",
        "Switch preset: winddown",
    )

    listed = client.get("/api/schedule").json()
    assert listed["timezone"] == "UTC"
    assert listed["runs"][0]["event"] == "Morning show"
    assert listed["upcoming"] == []  # the only event is disabled
    assert client.delete(url).json() == {"deleted": body["id"], "name": "Morning show"}
    assert client.get("/api/schedule").json()["events"] == []
    for method, path in (("put", url), ("delete", url), ("post", f"{url}/run")):
        missing = client.request(method, path, json=event() if method == "put" else None)
        assert missing.status_code == 404
        assert missing.json()["detail"] == "That event no longer exists"
    assert client.post(f"{url}/enable", json={"enabled": True}).status_code == 404


def test_actions_and_upcoming_come_from_the_server(
    rig: tuple[TestClient, PlayerBridge, Path],
) -> None:
    client, _bridge, _active = rig
    client.post("/api/schedule", json=event(repeat="interval", interval_minutes=30))
    data = client.get("/api/schedule").json()
    actions = {action["name"]: action for action in data["actions"]}
    assert set(actions) == {
        "profile",
        "preset",
        "stereo_tool_preset",
        "pick_mode",
        "liner",
        "time_check",
        "stream",
        "discovery",
        "recording_start",
        "recording_stop",
        "recording_split",
        "play_cart",
    }
    assert actions["time_check"]["fields"] == []
    assert actions["profile"]["fields"][0]["options"] == [{"value": "Evening", "label": "Evening"}]
    assert actions["liner"]["fields"][0]["options"] == [
        {"value": "station-id.wav", "label": "station-id.wav"}
    ]
    assert {"value": "clock", "label": "Clockwheel"} in actions["pick_mode"]["fields"][0]["options"]
    assert len(data["upcoming"]) == 5
    first = datetime.fromisoformat(data["upcoming"][0]["time"])
    assert timedelta(0) < first - datetime.now(UTC) <= timedelta(minutes=30)
    assert data["upcoming"][0]["repeat_text"] == "Every 30 minutes"
    many = client.get("/api/schedule/upcoming?count=12").json()["upcoming"]
    assert len(many) == 12
    assert client.get("/api/schedule/upcoming?count=51").status_code == 422


REFUSED = [
    (event(name=" "), "name", "Enter a name of up to 100 characters"),
    (event(time="25:00"), "time", None),
    (event(time=None), "time", "Enter a time"),
    (event(repeat="days", days=[]), "days", "Choose at least one day"),
    (event(extra=True), "extra", "Unknown field extra"),
    (event(action="cart"), "action", "Action cart is not available"),
    (event(params={}), "params.name", "Choose preset"),
    (event(params={"name": "nope"}), "params.name", "Preset nope was not found"),
    (
        event(action="profile", params={"name": "Gone"}),
        "params.name",
        "Profile Gone was not found",
    ),
    (
        event(action="pick_mode", params={"mode": "loud"}),
        "params.mode",
        "Song picking: loud is not one of the choices",
    ),
    (
        event(repeat="once", date="2020-01-01"),
        "date",
        "Choose a date and time in the future",
    ),
]


def test_invalid_events_are_refused_naming_the_field(
    rig: tuple[TestClient, PlayerBridge, Path],
) -> None:
    client, _bridge, active = rig
    for body, field, detail in REFUSED:
        reply = client.post("/api/schedule", json=body)
        assert reply.status_code == 400, body
        assert reply.json()["field"] == field
        if detail is not None:
            assert reply.json()["detail"] == detail
    assert not (active / "schedule.json").exists()


def test_strict_bodies_and_limits(rig: tuple[TestClient, PlayerBridge, Path]) -> None:
    client, _bridge, _active = rig
    assert client.post("/api/schedule", json=["not", "an", "object"]).status_code == 422
    huge = client.post("/api/schedule", json=event(name="x" * 20_000))
    assert huge.status_code == 413
    created = client.post("/api/schedule", json=event()).json()
    url = f"/api/schedule/{created['id']}"
    assert client.post(f"{url}/enable", json={"enabled": "yes"}).status_code == 422
    assert client.post(f"{url}/enable", json={"enabled": True, "x": 1}).status_code == 422
    # The server assigns IDs; a body cannot carry one.
    assert client.put(url, json={**event(), "id": created["id"]}).json()["field"] == "id"
    past = client.put(url, json=event(repeat="once", date="2020-01-01", enabled=False))
    assert past.status_code == 200
    refused = client.post(f"{url}/enable", json={"enabled": True})
    assert refused.status_code == 400
    assert refused.json()["field"] == "enabled"


def test_builtin_actions_report_failures_in_words(
    rig: tuple[TestClient, PlayerBridge, Path],
) -> None:
    client, bridge, _active = rig

    def run(action: str, params: dict[str, str]) -> dict[str, Any]:
        created = client.post(
            "/api/schedule", json=event(name=action, action=action, params=params)
        )
        assert created.status_code == 201, created.text
        return client.post(f"/api/schedule/{created.json()['id']}/run").json()

    assert run("preset", {"name": "winddown"})["result"] == "ok"
    assert run("profile", {"name": "Evening"})["result"] == "ok"
    assert run("liner", {"name": "station-id.wav"})["message"] == (
        "Scheduled liners need server audio or stream mode"
    )
    assert run("stream", {"state": "start"})["message"] == "Stream control needs stream mode"
    for action in ("recording_start", "recording_stop", "recording_split"):
        assert run(action, {})["message"] == "Recording requires stream mode"
    assert client.get("/api/recording").json()["detail"] == "Recording requires stream mode"
    assert run("discovery", {"state": "on"})["message"] == (
        "Set a discovery rate in Song picking first"
    )
    assert run("pick_mode", {"mode": "clock"})["message"] == (
        "No clockwheel is configured; import one in Library tools"
    )

    bridge.player._discovery_every = 3
    assert run("discovery", {"state": "on"})["result"] == "ok"
    assert bridge.player._state.discovery_enabled is True
    bridge.liner_scheduler = MagicMock()
    bridge.liner_scheduler.fire.return_value = None
    assert run("liner", {"name": "station-id.wav"})["message"] == (
        "Liner station-id.wav could not be played"
    )
    bridge.liner_scheduler.fire.return_value = "station-id.wav"
    assert run("liner", {"name": "station-id.wav"})["result"] == "ok"
    bridge.station = MagicMock()
    assert run("stream", {"state": "auto"})["result"] == "ok"
    bridge.station.set_scheduled_playing.assert_called_with(None)
    bridge.library = MagicMock()
    bridge.library.catalog.get_setting.return_value = {"slots": [{"folder": "music"}]}
    assert run("pick_mode", {"mode": "clock"})["result"] == "ok"
    assert bridge.player._cfg.playback.pick_mode == "clock"


def test_switch_stereo_tool_preset_lists_uploads_and_applies_them(
    rig: tuple[TestClient, PlayerBridge, Path],
) -> None:
    client, bridge, active = rig
    bridge.save_persistent_state = MagicMock()
    folder = active.parent / "stereo-tool-presets"
    folder.mkdir()
    (folder / "Night.sts").write_text("[A]\nB=1\n", encoding="utf-8")

    actions = {action["name"]: action for action in client.get("/api/schedule").json()["actions"]}
    field = actions["stereo_tool_preset"]["fields"][0]
    assert actions["stereo_tool_preset"]["description"] == "Switch Stereo Tool preset"
    assert field["label"] == "Stereo Tool preset"
    assert field["options"] == [
        {"value": "included", "label": "included"},
        {"value": "Night.sts", "label": "Night.sts"},
    ]

    refused = client.post(
        "/api/schedule", json=event(action="stereo_tool_preset", params={"name": "Gone.sts"})
    )
    assert refused.json()["detail"] == "Stereo Tool preset Gone.sts was not found"
    created = client.post(
        "/api/schedule", json=event(action="stereo_tool_preset", params={"name": "Night.sts"})
    ).json()
    assert created["action_text"] == "Switch Stereo Tool preset: Night.sts"
    url = f"/api/schedule/{created['id']}/run"
    assert client.post(url).json()["result"] == "ok"
    assert bridge.player._cfg.playback.stereotool_preset == "Night.sts"
    bridge.save_persistent_state.assert_called()

    # Deleted after the event was saved: the run fails in words, the setting stays.
    (folder / "Night.sts").unlink()
    bridge.player._cfg.playback.stereotool_preset = "included"
    failed = client.post(url).json()
    assert (failed["result"], failed["message"]) == ("error", "preset Night.sts was not found")
    assert bridge.player._cfg.playback.stereotool_preset == "included"


def test_a_profile_removed_after_saving_fails_its_run_in_words(
    rig: tuple[TestClient, PlayerBridge, Path], tmp_path: Path
) -> None:
    client, _bridge, _active = rig
    created = client.post(
        "/api/schedule", json=event(action="profile", params={"name": "Evening"})
    ).json()
    ProfileStore(tmp_path / "profiles").delete("Evening")
    run = client.post(f"/api/schedule/{created['id']}/run").json()
    assert run["result"] == "error"
    assert not run["message"].startswith("4")  # the HTTP detail, not "404: ..."


def test_startup_loads_the_schedule_and_disables_missed_once_events(tmp_path: Path) -> None:
    active = tmp_path / "active"
    active.mkdir()
    row = {
        **event(repeat="once", date="2020-01-01"),
        "id": "a" * 32,
        "days": [],
        "interval_minutes": None,
        "window_start": None,
        "window_end": None,
        "timing": "soft",
        "enabled": True,
    }
    (active / "schedule.json").write_text(
        json.dumps({"schema_version": 1, "events": [row], "runs": []}), encoding="utf-8"
    )
    player = _make_player_mock()
    player._cfg.index.active_dir = str(active)
    bridge = PlayerBridge(player=player, sim=_make_sim_mock())
    with TestClient(create_app(bridge)) as client:
        assert bridge.scheduler_track_changed is not None
        bridge.scheduler_track_changed()  # from the player thread; must not raise
        data = client.get("/api/schedule").json()
        assert [item["enabled"] for item in data["events"]] == [False]
        assert data["runs"][0]["result"] == "missed"
    assert bridge.scheduler_track_changed is None
