"""Paired time-check routes and scheduler action discovery."""

from __future__ import annotations

import json

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from autodj.config import TimeChecksConfig
from autodj.server import create_app


def test_settings_routes_save_only_switches_and_register_action(bridge, tmp_path) -> None:
    bridge.player._cfg.index.active_dir = tmp_path
    bridge.player._cfg.index.index_dir = tmp_path
    client = TestClient(create_app(bridge))
    initial = client.get("/api/time-checks")
    assert initial.status_code == 200
    assert initial.json()["missing_minutes"] == 1440
    assert "clips_folder" not in initial.json()
    assert "tts_command" not in initial.json()
    saved = client.put(
        "/api/time-checks",
        json={
            "enabled": True,
            "hour_format": "24",
            "rule": "after_hour_song",
            "enabled_packs": [],
            "pack_order": "rotate",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["hour_format"] == "24"
    assert (
        client.put(
            "/api/time-checks",
            json={
                "enabled": True,
                "hour_format": "24",
                "rule": "off",
                "enabled_packs": [],
                "pack_order": "rotate",
                "tts_command": ["bad"],
            },
        ).status_code
        == 422
    )
    assert client.get("/api/time-checks").json()["rule"] == "after_hour_song"
    actions = client.get("/api/schedule").json()["actions"]
    assert {"name": "time_check", "description": "Time check", "fields": []} in actions
    refused = client.post("/api/time-checks/test")
    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        "Time checks on the server need server audio or stream mode"
    )


def test_unpaired_browser_cannot_read_time_check_routes(bridge, tmp_path) -> None:
    bridge.player._cfg.server.access_token = "pairing-secret-that-is-at-least-32-bytes"
    bridge.player._cfg.index.active_dir = tmp_path
    bridge.player._cfg.index.index_dir = tmp_path
    client = TestClient(create_app(bridge))
    assert client.get("/api/time-checks").status_code in (401, 403)
    assert client.get("/api/time-checks/preview").status_code in (401, 403)


def test_preview_uses_recorded_clip_and_server_test_uses_liner_path(
    bridge, tmp_path, monkeypatch
) -> None:
    from datetime import UTC, datetime
    from unittest.mock import MagicMock
    from zoneinfo import ZoneInfo

    from autodj import time_checks

    bridge.player._cfg.index.active_dir = tmp_path
    bridge.player._cfg.index.index_dir = tmp_path
    bridge.player._cfg.time_checks = TimeChecksConfig(enabled=True, clips_folder=str(tmp_path))
    bridge.player._cfg.server.station_timezone = "UTC"
    now = datetime(2026, 10, 11, 3, 5, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz or ZoneInfo("UTC"))

    monkeypatch.setattr(time_checks, "datetime", FixedDateTime)
    sf.write(tmp_path / f"{now.hour:02d}_{now.minute:02d}.wav", np.zeros((441, 2)), 44100)
    bridge.liner_scheduler = MagicMock()
    client = TestClient(create_app(bridge))
    preview = client.get("/api/time-checks/preview")
    assert preview.status_code == 200
    assert preview.content.startswith(b"RIFF")
    assert client.post("/api/time-checks/test").status_code == 200
    bridge.liner_scheduler.play_audio.assert_called_once()


@pytest.mark.parametrize("timing", ["hard", "soft"])
def test_scheduled_action_says_why_it_did_not_play_then_plays(
    bridge, tmp_path, monkeypatch, timing
) -> None:
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from autodj import time_checks

    now = datetime(2026, 10, 11, 3, 5, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(time_checks, "datetime", FixedDateTime)
    bridge.player._cfg.index.active_dir = tmp_path
    bridge.player._cfg.index.index_dir = tmp_path
    bridge.player._cfg.server.station_timezone = "UTC"
    client = TestClient(create_app(bridge))

    bridge.skip = MagicMock()

    def run() -> dict[str, str]:
        created = client.post(
            "/api/schedule",
            json={
                "name": "Time",
                "action": "time_check",
                "params": {},
                "repeat": "daily",
                "time": "09:00",
                "timing": timing,
                "enabled": True,
            },
        )
        assert created.status_code == 201, created.text
        return client.post(f"/api/schedule/{created.json()['id']}/run").json()

    assert run()["message"] == "Enable time checks in Settings first"
    bridge.skip.assert_not_called()
    client.put(
        "/api/time-checks",
        json={
            "enabled": True,
            "hour_format": "12",
            "rule": "off",
            "enabled_packs": [],
            "pack_order": "rotate",
        },
    )
    assert run()["message"] == "Time checks on the server need server audio or stream mode"
    bridge.skip.assert_not_called()
    bridge.liner_scheduler = MagicMock()
    assert run()["message"] == "No voice pack, recorded clip or working TTS command for this minute"
    bridge.skip.assert_not_called()
    (tmp_path / "time_checks").mkdir()
    sf.write(tmp_path / "time_checks" / "03_05.wav", np.zeros((441, 2)), 44100)
    assert run()["result"] == "ok"
    bridge.liner_scheduler.play_audio.assert_called_once()
    assert bridge.skip.call_count == (1 if timing == "hard" else 0)


def test_lifespan_hands_the_hour_rule_to_the_liner_scheduler(bridge, tmp_path) -> None:
    from unittest.mock import MagicMock

    bridge.player._cfg.index.active_dir = tmp_path
    bridge.player._cfg.index.index_dir = tmp_path
    bridge.liner_scheduler = MagicMock()
    with TestClient(create_app(bridge)):
        assert bridge.liner_scheduler.announcement.__name__ == "due_audio"


def test_voice_packs_are_chosen_by_name_and_tested_one_at_a_time(
    bridge, tmp_path, monkeypatch
) -> None:
    from datetime import UTC, datetime
    from unittest.mock import MagicMock
    from zoneinfo import ZoneInfo

    from autodj import time_checks

    now = datetime(2026, 10, 11, 3, 5, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz or ZoneInfo("UTC"))

    monkeypatch.setattr(time_checks, "datetime", FixedDateTime)
    word = np.concatenate([np.zeros(2205), 0.3 * np.hanning(4410), np.zeros(2205)])
    studio = tmp_path / "packs" / "Studio"
    studio.mkdir(parents=True)
    sf.write(studio / "hr_03.wav", word, 44100)
    sf.write(studio / "m_05.wav", word, 44100)
    bridge.player._cfg.index.active_dir = tmp_path
    bridge.player._cfg.index.index_dir = tmp_path
    bridge.player._cfg.server.station_timezone = "UTC"
    bridge.player._cfg.time_checks = TimeChecksConfig(
        enabled=True, voice_packs=[str(studio), str(tmp_path / "Night")]
    )
    bridge.liner_scheduler = MagicMock()
    client = TestClient(create_app(bridge))
    status = client.get("/api/time-checks").json()
    assert [pack["name"] for pack in status["packs"]] == ["Studio", "Night"]
    assert status["enabled_packs"] == ["Studio", "Night"]
    assert str(tmp_path) not in json.dumps(status)
    body = {
        "enabled": True,
        "hour_format": "12",
        "rule": "off",
        "enabled_packs": ["Studio", "C:/elsewhere"],
        "pack_order": "random",
    }
    refused = client.put("/api/time-checks", json=body)
    assert refused.status_code == 404
    assert refused.json()["detail"] == "No voice pack named C:/elsewhere"
    saved = client.put("/api/time-checks", json={**body, "enabled_packs": ["Night"]})
    assert saved.status_code == 200
    assert [pack["enabled"] for pack in saved.json()["packs"]] == [False, True]
    # Studio is not ticked, but a test can still choose it.
    played = client.post("/api/time-checks/test", json={"pack": "Studio"})
    assert played.json() == {"message": "Time check played from the voice pack Studio"}
    (tmp_path / "time_checks").mkdir()
    sf.write(tmp_path / "time_checks" / "03_05.wav", np.zeros((441, 2)), 44100)
    fallback = client.post("/api/time-checks/test", json={"pack": "Night"})
    assert fallback.json() == {
        "message": "Voice pack Night cannot say this minute; played the recorded clip"
    }
    assert client.post("/api/time-checks/test").json() == {
        "message": "Time check played from the recorded clip"
    }
    assert client.post("/api/time-checks/test", json={"pack": "Gone"}).status_code == 404
    assert client.post("/api/time-checks/test", json={"path": "C:/"}).status_code == 422
    dated = client.put("/api/time-checks/packs/Studio/dates", json={"not_after": "2026-10-10"})
    assert dated.status_code == 200, dated.text
    assert dated.json()["packs"][0]["unplayable_text"] == "expired after 10 October 2026"
    backwards = client.put(
        "/api/time-checks/packs/Studio/dates",
        json={"not_before": "2026-12-26", "not_after": "2026-12-01"},
    )
    assert backwards.status_code == 400
    assert backwards.json()["field"] == "not_after"
    unknown = client.put("/api/time-checks/packs/Gone/dates", json={})
    assert unknown.status_code == 404
    preview = client.get("/api/time-checks/preview", params={"pack": "Studio"})
    assert preview.status_code == 200 and preview.content.startswith(b"RIFF")
    assert client.get("/api/time-checks/preview", params={"pack": "../x"}).status_code == 404


def test_a_voice_pack_that_cannot_say_the_minute_keeps_the_song_on_a_hard_event(
    bridge, tmp_path, monkeypatch
) -> None:
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from autodj import time_checks

    now = datetime(2026, 10, 11, 3, 5, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(time_checks, "datetime", FixedDateTime)
    studio = tmp_path / "packs" / "Studio"
    studio.mkdir(parents=True)
    # The hour, but not the minute: the pack cannot say 3:05.
    sf.write(studio / "hr_03.wav", np.zeros((4410, 2)), 44100)
    bridge.player._cfg.index.active_dir = tmp_path
    bridge.player._cfg.index.index_dir = tmp_path
    bridge.player._cfg.server.station_timezone = "UTC"
    bridge.player._cfg.time_checks = TimeChecksConfig(enabled=True, voice_packs=[str(studio)])
    bridge.liner_scheduler = MagicMock()
    bridge.skip = MagicMock()
    client = TestClient(create_app(bridge))
    assert client.get("/api/time-checks").json()["enabled_packs"] == ["Studio"]
    created = client.post(
        "/api/schedule",
        json={
            "name": "Time",
            "action": "time_check",
            "params": {},
            "repeat": "daily",
            "time": "09:00",
            "timing": "hard",
            "enabled": True,
        },
    )
    assert created.status_code == 201, created.text

    ran = client.post(f"/api/schedule/{created.json()['id']}/run").json()

    assert ran["message"] == "No voice pack, recorded clip or working TTS command for this minute"
    bridge.skip.assert_not_called()
    bridge.liner_scheduler.play_audio.assert_not_called()


def test_settings_are_refused_in_words_while_the_file_cannot_be_read(
    bridge, tmp_path, monkeypatch
) -> None:
    from autodj import time_checks

    bridge.player._cfg.index.active_dir = tmp_path
    bridge.player._cfg.index.index_dir = tmp_path
    client = TestClient(create_app(bridge))

    def unreadable(_path: object) -> None:
        raise time_checks.TimeChecksUnreadable("The time checks file could not be read just now")

    monkeypatch.setattr(time_checks, "_read_state", unreadable)
    body = {
        "enabled": True,
        "hour_format": "24",
        "rule": "off",
        "enabled_packs": [],
        "pack_order": "rotate",
    }
    refused = client.put("/api/time-checks", json=body)
    assert refused.status_code == 503
    assert "could not be read just now" in refused.json()["detail"]
