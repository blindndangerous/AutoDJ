"""Configuration routes remain available while a station is stopped."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from autodj import player as player_module
from tests.integration._helpers import wav_bytes
from tests.integration.test_multi_station import rig  # noqa: F401

OWL = "/api/stations/nightowl"
ORIGIN = {"Origin": "http://testserver"}


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_stopped_station_playback_schedule_and_clock_settings_are_saved(rig) -> None:  # noqa: F811
    client, _library, _cfg, _song, _closes = rig
    stations = client.app.state.stations
    default_player = stations.default.player
    assert client.get("/api/stations").json()["stations"][1]["state"] == "stopped"

    with patch.object(player_module, "Player", wraps=player_module.Player) as make_player:
        invalid = client.post(
            f"{OWL}/playback-settings",
            json={"transition_mode": "not-a-mode"},
            headers=ORIGIN,
        )
        assert invalid.status_code == 422

        playback = client.post(
            f"{OWL}/playback-settings",
            json={
                "hour_fit": True,
                "hour_fit_window_minutes": 12,
                "liners_enabled": True,
                "liners_every_n_songs": 1,
            },
            headers=ORIGIN,
        )
        assert playback.status_code == 200, playback.text
        assert playback.json()["playback"]["hour_fit"] is True
        assert playback.json()["playback"]["hour_fit_window_minutes"] == 12
        assert playback.json()["playback"]["liners_enabled"] is True

        schedule = client.post(
            f"{OWL}/schedule",
            json={
                "name": "Offline picking change",
                "action": "pick_mode",
                "params": {"mode": "random"},
                "repeat": "daily",
                "time": "09:00",
            },
            headers=ORIGIN,
        )
        assert schedule.status_code == 201, schedule.text
        assert client.get(f"{OWL}/schedule").json()["events"][0]["name"] == (
            "Offline picking change"
        )

        category = client.post(
            f"{OWL}/categories",
            json={"name": "Offline rock", "source": "query", "query": "genre:rock"},
            headers=ORIGIN,
        )
        assert category.status_code == 201, category.text
        category_id = category.json()["category"]["id"]
        clock = {
            "version": 1,
            "name": "Offline clock",
            "steps": [{"type": "category", "category": category_id, "selection": "random"}],
            "resync_minutes": [0],
        }
        saved_clock = client.post(f"{OWL}/library/clock", json=clock, headers=ORIGIN)
        assert saved_clock.status_code == 200, saved_clock.text
        assert client.get(f"{OWL}/library/clock/Offline%20clock").status_code == 200

        assert make_player.call_count == 0

    assert stations.default.player is default_player
    assert client.get("/api/stations").json()["stations"][0]["state"] != "stopped"
    paths = stations._extras["nightowl"].paths
    saved = json.loads(paths.web_state.read_text(encoding="utf-8"))
    assert saved["playback"]["hour_fit"] is True
    assert saved["playback"]["liners_enabled"] is True
    assert client.post(f"{OWL}/start", headers=ORIGIN).status_code == 200
    assert stations.get("nightowl").cfg.playback.hour_fit is True
    assert client.post(f"{OWL}/stop", headers=ORIGIN).status_code == 200
    upload = client.post(
        f"{OWL}/liners/upload",
        files={"file": ("offline.wav", wav_bytes(), "audio/wav")},
        headers=ORIGIN,
    )
    assert upload.status_code == 200, upload.text
    assert client.get(f"{OWL}/liners").json()["files"] == ["offline.wav"]


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_stopped_station_saved_library_and_content_settings_are_editable(rig, monkeypatch) -> None:  # noqa: F811
    client, library, _cfg, _song, _closes = rig
    monkeypatch.setattr(
        player_module,
        "Player",
        lambda *_args, **_kwargs: pytest.fail("Stopped edits must not construct a Player"),
    )
    station_base = f"{OWL}"
    liner_folder = client.app.state.stations._extras["nightowl"].paths.liners
    liner_folder.mkdir(parents=True, exist_ok=True)
    library.catalog.add_source(liner_folder, "liner", recursive=False, advanced=False)

    category = client.post(
        f"{station_base}/categories",
        json={"name": "Offline gold", "source": "query", "query": "genre:rock"},
        headers=ORIGIN,
    )
    assert category.status_code == 201, category.text
    category_id = category.json()["category"]["id"]
    assert client.get(f"{station_base}/categories").json()["categories"][0]["name"] == (
        "Offline gold"
    )

    show = client.post(
        f"{station_base}/shows",
        json={
            "name": "Offline show",
            "source": "category",
            "category": category_id,
            "target_minutes": 30,
        },
        headers=ORIGIN,
    )
    assert show.status_code == 201, show.text
    assert client.get(f"{station_base}/shows").json()["shows"][0]["name"] == "Offline show"

    ads = client.put(
        f"{station_base}/ads/settings",
        json={"separation_minutes": 20, "late_minutes": 15, "miss_after_minutes": 45},
        headers=ORIGIN,
    )
    assert ads.status_code == 200, ads.text
    assert client.get(f"{station_base}/ads").json()["settings"] == {
        "separation_minutes": 20,
        "late_minutes": 15,
        "miss_after_minutes": 45,
    }

    requests = client.post(
        f"{station_base}/requests/settings",
        json={"enabled": False, "approval": "manual", "max_bridges": 2},
        headers=ORIGIN,
    )
    assert requests.status_code == 200, requests.text
    assert requests.json()["approval"] == "manual"
    assert requests.json()["max_bridges"] == 2

    rules = client.post(
        f"{station_base}/intros/rules",
        json={
            "conditions": [{"field": "artist", "operator": "contains", "value": "Example"}],
            "label": "Offline intro rule",
            "clip_ids": [],
            "liner_folder": str(liner_folder),
            "chance": 75,
        },
        headers=ORIGIN,
    )
    assert rules.status_code == 201, rules.text
    assert client.get(f"{station_base}/intros/rules").json()["rules"][0]["label"] == (
        "Offline intro rule"
    )

    carts = client.get(f"{station_base}/carts")
    assert carts.status_code == 200, carts.text
    renamed = client.put(
        f"{station_base}/carts/pages/1/name",
        json={"name": "Saved while stopped"},
        headers=ORIGIN,
    )
    assert renamed.status_code == 200, renamed.text
    assert client.get(f"{station_base}/carts").json()["wall"]["pages"][0]["name"] == (
        "Saved while stopped"
    )

    relay = {
        "name": "offline",
        "type": "icecast",
        "host": "8.8.8.8",
        "port": 8000,
        "mount": "/radio.mp3",
        "password": "secret",
        "enabled": False,
        "public": False,
    }
    saved_relay = client.post(f"{station_base}/stream/relays", json=relay, headers=ORIGIN)
    assert saved_relay.status_code == 200, saved_relay.text
    relays = client.get(f"{station_base}/stream/relays")
    assert relays.status_code == 200, relays.text
    assert relays.json()["relays"][0]["name"] == "offline"

    stream = client.post(
        f"{station_base}/stream/settings",
        json={"station_name": "Saved Night Owl", "continuous": True},
        headers=ORIGIN,
    )
    assert stream.status_code == 200, stream.text
    assert stream.json()["station_name"] == "Saved Night Owl"
    assert stream.json()["continuous"] is True
    stored_settings = client.get(f"{station_base}/settings")
    assert stored_settings.status_code == 200, stored_settings.text
    assert stored_settings.json()["playback"]["stream_station_name"] == "Saved Night Owl"
    assert client.get(f"{station_base}/history").status_code == 200

    recording = client.post(
        f"{station_base}/recording/settings",
        json={"split": "show", "retention_days": 3, "free_space_mb": 10},
        headers=ORIGIN,
    )
    assert recording.status_code == 200, recording.text
    assert recording.json()["split"] == "show"
    assert recording.json()["retention_days"] == 3


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_live_station_is_unchanged_and_stopped_transport_commands_are_refused(rig) -> None:  # noqa: F811
    client, _library, _cfg, song, _closes = rig
    stations = client.app.state.stations
    default_player = stations.default.player
    created = client.post(
        f"{OWL}/schedule",
        json={
            "name": "Stopped event",
            "action": "pick_mode",
            "params": {"mode": "random"},
            "repeat": "daily",
            "time": "09:00",
        },
        headers=ORIGIN,
    )
    assert created.status_code == 201, created.text

    for response in (
        client.post(f"{OWL}/skip", headers=ORIGIN),
        client.post(f"{OWL}/queue/add", json={"path": song.path}, headers=ORIGIN),
        client.post(f"{OWL}/carts/pages/1/slots/1/play", headers=ORIGIN),
        client.post(f"{OWL}/schedule/{created.json()['id']}/run", headers=ORIGIN),
    ):
        assert response.status_code == 409, response.text

    assert stations.default.player is default_player
    assert client.get("/api/stations").json()["stations"][0]["state"] != "stopped"


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_stopped_live_dj_settings_and_rotation_are_saved_without_a_player(rig, monkeypatch) -> None:  # noqa: F811
    client, _library, _cfg, _song, _closes = rig
    monkeypatch.setattr(
        player_module,
        "Player",
        lambda *_args, **_kwargs: pytest.fail("Stopped edits must not construct a Player"),
    )

    settings = client.put(
        f"{OWL}/live/settings",
        json={"mic_return_seconds": 2, "mic_duck_db": 6},
        headers=ORIGIN,
    )
    assert settings.status_code == 200, settings.text
    assert settings.json()["mic_return_seconds"] == 2
    assert settings.json()["mic_duck_db"] == 6
    dj = client.post(
        f"{OWL}/live/djs",
        json={"username": "alice", "password": "correct horse battery", "display_name": "DJ Alice"},
        headers=ORIGIN,
    )
    assert dj.status_code == 200, dj.text
    assert dj.json() == {"username": "alice", "display_name": "DJ Alice", "password_set": True}
    live = client.get(f"{OWL}/live")
    assert live.status_code == 200, live.text
    assert live.json()["settings"]["mic_return_seconds"] == 2
    assert live.json()["djs"][0]["username"] == "alice"
    assert "password" not in live.json()["djs"][0]
    deleted = client.delete(f"{OWL}/live/djs/alice", headers=ORIGIN)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True}

    rotation = client.post(f"{OWL}/rotation-test", json={"picks": 3, "seed": 1}, headers=ORIGIN)
    assert rotation.status_code == 202, rotation.text


@pytest.mark.parametrize("rig", [{}], indirect=True)
def test_default_station_can_be_stopped_edited_and_restarted(rig) -> None:  # noqa: F811
    client, _library, _cfg, _song, _closes = rig
    stations = client.app.state.stations
    original = stations.default.player
    assert original is not None
    stopped = client.post("/api/stations/default/stop", headers=ORIGIN)
    assert stopped.status_code == 200, stopped.text
    assert client.get("/api/stations").json()["stations"][0]["state"] == "stopped"

    changed = client.post(
        "/api/stations/default/playback-settings",
        json={"hour_fit": True, "hour_fit_window_minutes": 9},
        headers=ORIGIN,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["playback"]["hour_fit"] is True
    restarted = client.post("/api/stations/default/start", headers=ORIGIN)
    assert restarted.status_code == 200, restarted.text
    assert stations.default.cfg.playback.hour_fit is True


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_saved_edits_refused_during_station_start(rig) -> None:  # noqa: F811
    import asyncio

    client, _library, _cfg, _song, _closes = rig
    registry = client.app.state.stations

    async def during_start():
        async with registry._locks["nightowl"]:
            response = await asyncio.to_thread(client.get, f"{OWL}/settings")
            assert response.status_code == 409
            assert "nightowl" not in registry._content

    asyncio.run(during_start())


@pytest.mark.parametrize("rig", [{"autostart": "false"}], indirect=True)
def test_history_export_uses_selected_station_store_even_when_stopped(rig):  # noqa: F811
    """ADR-0005-02: exports use station histories even while stopped."""
    from datetime import datetime

    client, library, _cfg, song, _closes = rig
    moment = datetime(2026, 9, 1, 12).astimezone().isoformat()
    for slug, title in [("default", "Main current"), ("nightowl", "Owl current")]:
        row = {
            "event_id": slug,
            "path": song.path,
            "title": title,
            "artist": "Artist",
            "duration": 180.0,
            "played_at": moment,
        }
        library.station_ledger(slug).store.write_history([row], [])
        library.catalog.write_history(
            [{**row, "event_id": "legacy-" + slug, "title": "Stale legacy"}], station=slug
        )
    for prefix, title, other in [
        ("/api", "Main current", "Owl current"),
        (OWL, "Owl current", "Main current"),
    ]:
        response = client.get(prefix + "/history/export?from=2026-09-01&to=2026-09-01")
        assert response.status_code == 200, response.text
        assert title in response.text
        assert other not in response.text
        assert "Stale legacy" not in response.text
        assert response.headers["x-play-count"] == "1"
    assert client.app.state.stations.editable("nightowl").stopped
