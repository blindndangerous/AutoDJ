"""Voice packs and clips added and removed from the page, paired browsers only (#388)."""

from __future__ import annotations

import io
import json
import zipfile

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient

from autodj.config import TimeChecksConfig
from autodj.server import create_app

_SECRET = "pairing-secret-that-is-at-least-32-bytes"


def _wav() -> bytes:
    buffer = io.BytesIO()
    sf.write(buffer, np.zeros((4410, 2)), 44100, format="WAV")
    return buffer.getvalue()


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _client(bridge, tmp_path, *, secret: str | None = None) -> TestClient:
    cfg = bridge.player._cfg
    cfg.index.active_dir = tmp_path
    cfg.index.index_dir = tmp_path
    cfg.server.station_timezone = "UTC"
    if secret is not None:
        cfg.server.access_token = secret
    app = create_app(bridge)
    app.state.security_policy.now = lambda: 1_000
    client = TestClient(app)
    if secret is not None:
        code = app.state.request_pairing_code()[0]
        paired = client.post("/api/pair", json={"code": code, "device_name": "Desk"})
        assert paired.status_code == 200
    return client


def _upload_pack(client: TestClient, data: bytes, filename: str = "Studio.zip", **params):
    return client.post(
        "/api/time-checks/packs",
        params=params,
        files={"file": (filename, data, "application/zip")},
    )


def test_only_a_paired_browser_can_add_or_remove_packs_and_clips(bridge, tmp_path) -> None:
    cfg = bridge.player._cfg
    cfg.server.access_token = _SECRET
    cfg.index.active_dir = tmp_path
    cfg.index.index_dir = tmp_path
    app = create_app(bridge)
    client = TestClient(app)
    pack = _zip({"hr_03.wav": _wav()})
    files = {"file": ("Studio.zip", pack, "application/zip")}
    assert client.post("/api/time-checks/packs", files=files).status_code == 401
    assert client.delete("/api/time-checks/packs/Studio").status_code == 401
    clip = {"file": ("intro.wav", _wav(), "audio/wav")}
    assert client.post("/api/time-checks/clips", files=clip).status_code == 401
    # A machine token is never a browser session.
    bearer = {"Authorization": "Bearer not-a-browser"}
    assert client.post("/api/time-checks/packs", files=files, headers=bearer).status_code == 401
    assert client.delete("/api/time-checks/packs/Studio", headers=bearer).status_code == 401
    assert not (tmp_path / "time_checks").exists()


def test_a_pack_is_added_from_a_zip_listed_at_once_and_removed(bridge, tmp_path) -> None:
    client = _client(bridge, tmp_path, secret=_SECRET)
    before = client.get("/api/time-checks").json()
    assert before["packs"] == [] and before["pack_upload_max_mb"] == 256
    added = _upload_pack(client, _zip({"Studio/hr_03.wav": _wav(), "Studio/m_05.wav": _wav()}))
    assert added.status_code == 200, added.text
    body = added.json()
    assert body["added"] == "Studio"
    assert [(pack["name"], pack["removable"], pack["enabled"]) for pack in body["packs"]] == [
        ("Studio", True, True)
    ]
    assert (tmp_path / "time_checks" / "Studio" / "hr_03.wav").is_file()
    assert str(tmp_path) not in json.dumps(body)
    # The name is taken: refused until replacing is asked for.
    again = _upload_pack(client, _zip({"m_06.wav": _wav()}), "Other.zip", name="Studio")
    assert again.status_code == 409
    assert again.json() == {
        "detail": "A voice pack named Studio already exists",
        "field": "replace",
    }
    replaced = _upload_pack(
        client, _zip({"m_06.wav": _wav()}), "Other.zip", name="Studio", replace="true"
    )
    assert replaced.status_code == 200
    assert sorted(p.name for p in (tmp_path / "time_checks" / "Studio").iterdir()) == ["m_06.wav"]
    # Named after the zip when it has no one folder and no name is given.
    night = _upload_pack(client, _zip({"hr_03.wav": _wav()}), "Night.zip")
    assert night.json()["added"] == "Night"
    removed = client.delete("/api/time-checks/packs/Studio")
    assert removed.status_code == 200
    assert removed.json()["removed"] == "Studio"
    assert [pack["name"] for pack in removed.json()["packs"]] == ["Night"]
    assert client.delete("/api/time-checks/packs/Studio").status_code == 404
    leftovers = [p.name for p in (tmp_path / "time_checks").iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_unsafe_zips_are_refused_and_leave_nothing(bridge, tmp_path) -> None:
    client = _client(bridge, tmp_path)
    for entries, status, words in (
        ({"../evil.wav": _wav()}, 400, "outside the pack"),
        ({"/abs.wav": _wav()}, 400, "absolute path"),
        ({"hr_03.wav": _wav(), "notes.txt": b"hi"}, 400, "not an audio file"),
        ({"hr_03.wav": b"not audio"}, 400, "hr_03.wav"),
    ):
        refused = _upload_pack(client, _zip(entries))
        assert refused.status_code == status, refused.text
        assert words in refused.json()["detail"]
    assert _upload_pack(client, b"not a zip").json()["detail"] == "The upload is not a zip file"
    bad_name = _upload_pack(client, _zip({"hr_03.wav": _wav()}), name="../x")
    assert bad_name.status_code == 400
    assert not (tmp_path / "evil.wav").exists()
    assert [p.name for p in (tmp_path / "time_checks").iterdir()] == []


def test_a_zip_over_the_upload_cap_is_refused_before_it_is_read(
    bridge, tmp_path, monkeypatch
) -> None:
    from autodj import server

    monkeypatch.setattr(server, "MAX_PACK_UPLOAD_BYTES", 1024)
    client = _client(bridge, tmp_path)
    big = _zip({f"m_{minute:02d}.wav": _wav() for minute in range(10)})
    refused = _upload_pack(client, big)
    assert refused.status_code == 413
    assert not (tmp_path / "time_checks" / "Studio").exists()


def test_a_configured_pack_is_not_removed_or_replaced_from_the_page(bridge, tmp_path) -> None:
    listed = tmp_path / "voices" / "Listed"
    listed.mkdir(parents=True)
    sf.write(listed / "hr_03.wav", np.zeros((4410, 2)), 44100)
    bridge.player._cfg.time_checks = TimeChecksConfig(voice_packs=[str(listed)])
    client = _client(bridge, tmp_path)
    removed = client.delete("/api/time-checks/packs/Listed")
    assert removed.status_code == 409
    assert "listed in config.toml" in removed.json()["detail"]
    replaced = _upload_pack(client, _zip({"hr_03.wav": _wav()}), name="listed", replace="true")
    assert replaced.status_code == 409
    # Not one the page may replace, so it is not offered.
    assert "field" not in replaced.json()
    assert (listed / "hr_03.wav").is_file()


def test_clips_are_uploaded_chosen_and_kept_by_name(bridge, tmp_path) -> None:
    client = _client(bridge, tmp_path)
    uploaded = client.post(
        "/api/time-checks/clips", files={"file": ("hello.wav", _wav(), "audio/wav")}
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["uploaded"] == "hello.wav"
    assert uploaded.json()["clip_files"] == ["hello.wav"]
    taken = client.post(
        "/api/time-checks/clips", files={"file": ("hello.wav", _wav(), "audio/wav")}
    )
    assert taken.status_code == 409 and taken.json()["field"] == "replace"
    replaced = client.post(
        "/api/time-checks/clips",
        params={"replace": "true"},
        files={"file": ("hello.wav", _wav(), "audio/wav")},
    )
    assert replaced.status_code == 200
    for name, data in (("notes.txt", b"x"), ("../x.wav", _wav()), ("bad.wav", b"not audio")):
        refused = client.post("/api/time-checks/clips", files={"file": (name, data, "audio/wav")})
        assert refused.status_code == 400, name
    (tmp_path / "time_checks" / "Clips").mkdir()
    body = {
        "enabled": True,
        "hour_format": "12",
        "rule": "off",
        "enabled_packs": [],
        "pack_order": "rotate",
    }
    chosen = client.put(
        "/api/time-checks", json={**body, "recorded_folder": "Clips", "intro": "hello.wav"}
    )
    assert chosen.status_code == 200, chosen.text
    assert (chosen.json()["recorded_folder"], chosen.json()["intro"]) == ("Clips", "hello.wav")
    # A save that leaves the clip choices out keeps them.
    kept = client.put("/api/time-checks", json={**body, "rule": "after_hour_song"})
    assert (kept.json()["recorded_folder"], kept.json()["intro"]) == ("Clips", "hello.wav")
    saved = json.loads((tmp_path / "time_checks.json").read_text(encoding="utf-8"))
    assert saved["settings"]["intro"] == "hello.wav"
    for choice, words in (
        ({"recorded_folder": "Gone"}, "No folder named Gone"),
        ({"outro": "gone.wav"}, "No outro clip named gone.wav"),
    ):
        refused = client.put("/api/time-checks", json={**body, **choice})
        assert refused.status_code == 400
        assert words in refused.json()["detail"]
    assert client.put("/api/time-checks", json={**body, "intro": "C:/x.wav"}).status_code == 422
    leftovers = [p.name for p in (tmp_path / "time_checks").iterdir() if p.name.startswith(".")]
    assert leftovers == []
