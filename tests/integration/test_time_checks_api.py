"""Paired time-check routes and scheduler action discovery."""

from __future__ import annotations

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
    client.put("/api/time-checks", json={"enabled": True, "hour_format": "12", "rule": "off"})
    assert run()["message"] == "Time checks on the server need server audio or stream mode"
    bridge.skip.assert_not_called()
    bridge.liner_scheduler = MagicMock()
    assert run()["message"] == "No recorded clip or working TTS command for this minute"
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
