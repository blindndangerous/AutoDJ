"""Paired settings routes manage uploaded Stereo Tool presets."""

from pathlib import Path

from fastapi.testclient import TestClient

from tests._layout import point_index
from tests._server import create_app
from tests.integration._helpers import patch_station_settings


def test_preset_routes_upload_select_and_delete(bridge, tmp_path: Path) -> None:
    active = tmp_path / "index" / "default"
    active.mkdir(parents=True)
    point_index(bridge.player._cfg.index, active)
    bridge.player._cfg.index.index_dir = active.parent
    client = TestClient(create_app(bridge))

    assert client.get("/api/stereo-tool-presets").json()["presets"] == [
        {"name": "included", "label": "Included", "included": True}
    ]
    upload = client.post(
        "/api/stereo-tool-presets/upload", files={"file": ("Night.sts", b"[Processing]\nGain=0\n")}
    )
    assert upload.status_code == 200
    assert (active / "stereo-tool-presets" / "Night.sts").is_file()
    selected = patch_station_settings(client, {"playback": {"stereotool_preset": "Night.sts"}})
    assert selected.status_code == 200
    assert selected.json()["settings"]["values"]["playback"]["stereotool_preset"] == "Night.sts"
    assert client.delete("/api/stereo-tool-presets/Night.sts").status_code == 409
    assert (
        patch_station_settings(client, {"playback": {"stereotool_preset": "included"}}).status_code
        == 200
    )
    assert client.delete("/api/stereo-tool-presets/Night.sts").status_code == 200
    assert client.delete("/api/stereo-tool-presets/included").status_code == 400
    assert client.delete("/api/stereo-tool-presets/Night.sts").status_code == 404


def test_listing_reports_included_when_the_active_file_is_gone(bridge, tmp_path: Path) -> None:
    active = tmp_path / "index" / "default"
    active.mkdir(parents=True)
    point_index(bridge.player._cfg.index, active)
    bridge.player._cfg.playback.stereotool_preset = "Gone.sts"
    client = TestClient(create_app(bridge))

    assert client.get("/api/stereo-tool-presets").json()["active"] == "included"


def test_upload_refuses_a_second_file_of_the_same_name_in_words(bridge, tmp_path: Path) -> None:
    active = tmp_path / "index" / "default"
    active.mkdir(parents=True)
    point_index(bridge.player._cfg.index, active)
    client = TestClient(create_app(bridge))
    files = {"file": ("Night.sts", b"[Processing]\nGain=0\n")}

    assert client.post("/api/stereo-tool-presets/upload", files=files).status_code == 200
    again = client.post("/api/stereo-tool-presets/upload", files=files)
    assert again.status_code == 409
    assert again.json()["detail"] == "a preset named Night.sts already exists"
    reserved = client.post(
        "/api/stereo-tool-presets/upload", files={"file": ("CON.sts", b"[A]\nB=1\n")}
    )
    assert reserved.status_code == 400
    too_big = client.post(
        "/api/stereo-tool-presets/upload",
        files={"file": ("Big.sts", b"[A]\nB=1\n" + b"x" * (3 * 1024 * 1024))},
    )
    assert too_big.status_code == 413
    assert not (active / "stereo-tool-presets" / "Big.sts").exists()


def test_preset_routes_refuse_unsafe_uploads_and_missing_selection(bridge, tmp_path: Path) -> None:
    active = tmp_path / "index" / "default"
    active.mkdir(parents=True)
    point_index(bridge.player._cfg.index, active)
    bridge.player._cfg.index.index_dir = active.parent
    client = TestClient(create_app(bridge))

    assert (
        client.post(
            "/api/stereo-tool-presets/upload", files={"file": ("../outside.sts", b"[A]\nB=1\n")}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/stereo-tool-presets/upload", files={"file": ("bad.sts", b"not a preset")}
        ).status_code
        == 400
    )
    assert (
        patch_station_settings(
            client, {"playback": {"stereotool_preset": "missing.sts"}}
        ).status_code
        == 400
    )
    assert patch_station_settings(client, {"playback": {"intro_lift_max_db": 7}}).status_code == 422
    assert (
        patch_station_settings(client, {"playback": {"stereotool_input_trim_db": -13}}).status_code
        == 422
    )
