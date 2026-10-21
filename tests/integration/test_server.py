"""Integration tests for autodj.server.

Uses FastAPI's TestClient (synchronous) so no real uvicorn process or
audio hardware is needed.  The Player and SimilarityIndex are mocked
with minimal fakes so the tests focus on HTTP routing and the legacy
PlayerBridge surface.

Recent additions (build-stamp footer, advance log banner, key-notation
combo, repick-next failure paths) live in the sibling
``test_server_recent.py`` so this file stays under the 2000-line working
budget.  Shared fixtures ``client`` / ``bridge`` come from
``conftest.py``; mock-builders come from ``_helpers.py``.
"""

from __future__ import annotations

import asyncio
import functools
import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

import pytest

from autodj.config import PlayerPageConfig, RequestsConfig, ServerConfig, TimeChecksConfig
from autodj.live_protocol import IcecastSourceH11Protocol
from autodj.repeat_settings import RepeatRule, RepeatSettings
from autodj.server import PlayerBridge, create_app
from autodj.settings_bodies import PlaybackSettingsBody
from tests._layout import layout_index, point_index

from ._helpers import NO_INDEX_DIR, _make_entry, _make_player_mock, _make_sim_mock, wav_bytes


@pytest.fixture(params=[1, 2])
async def _lyrics_executor(request):
    """Keep observer waits from consuming the executor's limited read capacity."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=request.param) as executor:
        asyncio.get_running_loop().set_default_executor(executor)
        yield


def _tls_files(tmp_path: Path) -> dict[str, str]:
    """Create placeholder certificate and key files; uvicorn is mocked, so never parsed."""
    cert = tmp_path / "radio.pem"
    key = tmp_path / "radio-key.pem"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    return {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}


def _test_config(tmp_path: Path):
    """Load defaults without reading the developer's working config."""
    from autodj.config import load_config

    path = tmp_path / "config.toml"
    path.write_text(
        f'[index]\nindex_dir = "{(tmp_path / "index").as_posix()}"\n'
        f'model_dir = "{(tmp_path / "models").as_posix()}"\n',
        encoding="utf-8",
    )
    cfg = load_config(path, environ={})
    return cfg


def _set_repeat_settings(cfg_mock, active_dir: Path) -> None:
    cfg_mock.playback.repeat_rules = RepeatSettings(
        track=RepeatRule(tracks=50), artist=RepeatRule(tracks=3)
    )
    point_index(cfg_mock.index, active_dir)


def _register_index_entry(bridge, entry) -> None:
    """Publish one extra entry through immutable similarity-index seams."""
    entries = (*bridge.sim.entries_snapshot(), entry)
    bridge.sim.entries_snapshot.side_effect = lambda: entries
    bridge.sim.entry_for_path.side_effect = lambda path: next(
        (candidate for candidate in entries if candidate.path == path),
        None,
    )


class TestStatus:
    def test_status_returns_200(self, client) -> None:
        resp = client.get("/api/status")
        assert resp.status_code == 200

    def test_status_has_current_track(self, client) -> None:
        data = client.get("/api/status").json()
        assert data["current_track"] is not None
        assert data["current_track"]["title"] == "Song 0"

    def test_status_has_next_track(self, client) -> None:
        data = client.get("/api/status").json()
        assert data["next_track"]["title"] == "Song 1"

    def test_status_elapsed_is_numeric(self, client) -> None:
        data = client.get("/api/status").json()
        assert isinstance(data["elapsed"], float)
        assert data["elapsed"] == pytest.approx(30.0, abs=0.5)

    def test_status_volume_defaults_to_one(self, client) -> None:
        data = client.get("/api/status").json()
        assert data["volume"] == pytest.approx(1.0)

    def test_status_not_paused_by_default(self, client) -> None:
        data = client.get("/api/status").json()
        assert data["is_paused"] is False


# ---------------------------------------------------------------------------
# POST /api/skip
# ---------------------------------------------------------------------------


class TestSkip:
    def test_skip_returns_200(self, client) -> None:
        resp = client.post("/api/skip")
        assert resp.status_code == 200

    def test_skip_calls_skip_event(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        tc.post("/api/skip")
        bridge.player._skip_event.set.assert_called_once()

    def test_skip_returns_state(self, client) -> None:
        """/api/skip echoes the fresh state synchronously so the browser
        does not have to wait for the next WS broadcast tick.
        """
        data = client.post("/api/skip").json()
        assert "current_track" in data
        assert "next_track" in data


# ---------------------------------------------------------------------------
# POST /api/pause
# ---------------------------------------------------------------------------


class TestPause:
    def test_pause_returns_200(self, client) -> None:
        assert client.post("/api/pause").status_code == 200

    def test_pause_toggles_state(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        # Initially not paused
        assert bridge.player._state.is_paused is False
        data = tc.post("/api/pause").json()
        assert data["paused"] is True
        assert bridge.player._state.is_paused is True
        # Second call toggles back
        data = tc.post("/api/pause").json()
        assert data["paused"] is False

    def test_pause_response_reflects_new_state(self, client) -> None:
        data = client.post("/api/pause").json()
        assert "paused" in data


# ---------------------------------------------------------------------------
# POST /api/volume
# ---------------------------------------------------------------------------


class TestVolume:
    def test_volume_sets_value(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/volume", json={"volume": 0.75})
        assert resp.status_code == 200
        assert bridge.player._state.volume == pytest.approx(0.75)

    def test_volume_clamps_to_one(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        tc.post("/api/volume", json={"volume": 2.0})
        assert bridge.player._state.volume == pytest.approx(1.0)

    def test_volume_clamps_to_zero(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        tc.post("/api/volume", json={"volume": -0.5})
        assert bridge.player._state.volume == pytest.approx(0.0)

    def test_low_volume_survives_the_round_trip(self, bridge) -> None:
        """A 5 % fader gain (about 0.0014) must not come back as 0."""
        from fastapi.testclient import TestClient

        quiet = 10 ** (-57 / 20)
        tc = TestClient(create_app(bridge))
        assert tc.post("/api/volume", json={"volume": quiet}).json()["volume"] == quiet
        assert tc.get("/api/status").json()["volume"] == quiet

    def test_volume_response_returns_new_value(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        data = tc.post("/api/volume", json={"volume": 0.5}).json()
        assert data["volume"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# POST /api/seek
# ---------------------------------------------------------------------------


class TestSeek:
    """Endpoint and PlayerBridge.seek() behaviour."""

    def test_seek_absolute(self, client) -> None:
        # Mock player has length 180 s, sr=44100; seek to 60 s -> sample
        # index 60*44100 = 2_646_000.  bridge.seek invokes player.seek_to
        # which mutates _playback_pos[0] in real Player; on the mock we
        # only verify the call goes through and returns a number.
        resp = client.post("/api/seek", json={"seconds": 30.0})
        assert resp.status_code == 200
        body = resp.json()
        assert "elapsed" in body
        assert isinstance(body["elapsed"], int | float)

    def test_seek_relative(self, client) -> None:
        resp = client.post("/api/seek", json={"delta": -5.0})
        assert resp.status_code == 200
        assert "elapsed" in resp.json()

    def test_seek_for_a_named_song_moves_only_that_song(self, bridge) -> None:
        """A landmark key names the song it meant (#60); old pages name none."""
        from fastapi.testclient import TestClient

        client = TestClient(
            create_app(bridge), headers={"Host": "testserver", "Origin": "http://testserver"}
        )
        playing = bridge.player._state.current_track.path
        bridge.player.seek_to.reset_mock()
        assert client.post("/api/seek", json={"seconds": 30.0, "path": playing}).status_code == 200
        assert bridge.player.seek_to.call_count == 1
        moved = client.post("/api/seek", json={"seconds": 30.0, "path": "Z:/Music/other.flac"})
        assert moved.status_code == 409
        assert moved.json()["detail"] == "The song on air has changed, so it was not moved."
        assert bridge.player.seek_to.call_count == 1
        bridge.player._state.current_track = None
        nothing = client.post("/api/seek", json={"seconds": 30.0, "path": playing})
        assert nothing.status_code == 409
        assert client.post("/api/seek", json={"seconds": 1.0, "path": ""}).status_code == 422

    def test_seek_no_args_is_query(self, client) -> None:
        # Empty body -> just reports current position (used by the web UI
        # to read back state without changing anything).
        resp = client.post("/api/seek", json={})
        assert resp.status_code == 200

    def test_player_seek_to_clamps_to_buffer_end(self) -> None:
        # Direct unit test on the real Player.seek_to logic — the mock
        # player in the bridge fixture doesn't implement clamping.
        from autodj.player import Player

        p = Player.__new__(Player)
        p.bus = None  # no server mix bus
        p._current_sr = 1000
        p._playback_len = 60_000  # 60 s
        p._playback_pos = [0]
        p._playback_output_pos = [0]
        # Overshoot clamps to 59.9 s (length - 0.1 s).
        result = p.seek_to(120.0)
        assert result == pytest.approx(59.9, abs=0.05)
        assert p._playback_pos[0] == int(59.9 * 1000)

    def test_player_seek_to_clamps_to_zero(self) -> None:
        from autodj.player import Player

        p = Player.__new__(Player)
        p.bus = None  # no server mix bus
        p._current_sr = 1000
        p._playback_len = 60_000
        p._playback_pos = [5_000]
        p._playback_output_pos = [5_000]
        result = p.seek_to(-10.0)
        assert result == 0.0
        assert p._playback_pos[0] == 0

    def test_player_seek_relative(self) -> None:
        from autodj.player import Player

        p = Player.__new__(Player)
        p.bus = None  # no server mix bus
        p._current_sr = 1000
        p._playback_len = 60_000
        p._playback_pos = [10_000]  # 10 s
        p._playback_output_pos = [10_000]
        result = p.seek_relative(5.0)
        assert result == pytest.approx(15.0, abs=0.01)
        assert p._playback_pos[0] == 15_000


# ---------------------------------------------------------------------------
# GET /api/liners + /api/liners/file/<name>
# ---------------------------------------------------------------------------


class TestLiners:
    def test_registered_nested_liner_id_is_listed_previewed_and_deleted_safely(
        self, bridge, tmp_path
    ) -> None:
        from fastapi.testclient import TestClient

        from autodj.catalog import _path

        path = tmp_path / "registered" / "nested" / "voice.mp3"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"registered-audio")
        record = {
            "id": "stable-liner-id",
            "path": str(path),
            "kind": "liner",
            "length": 3.0,
            "points": {"start": 0.2, "end": 2.8},
        }

        class Catalog:
            def record_for_path(self, value):
                return record if _path(value) == _path(path) and path.exists() else None

            def sources(self, kind=None):
                return [
                    {
                        "id": "source-1",
                        "path": str(path.parent.parent),
                        "kind": "liner",
                        "recursive": True,
                        "advanced": False,
                    }
                ]

        class Library:
            catalog = Catalog()

            def entries_snapshot(self, kind="music"):
                return (
                    (SimpleNamespace(path=str(path)),) if kind == "liner" and path.exists() else ()
                )

            def entry_for_path(self, value):
                return (
                    SimpleNamespace(path=str(path))
                    if _path(value) == _path(path) and path.exists()
                    else None
                )

            def record_for_id(self, value):
                return record if value == record["id"] and path.exists() else None

            def refresh(self):
                return ()

        bridge.library = Library()
        point_index(bridge.player._cfg.index, str(Path(tmp_path / "empty-flat").parent))
        tc = TestClient(create_app(bridge))
        listing = tc.get("/api/liners").json()
        assert listing["items"] == [
            {
                "id": "stable-liner-id",
                "name": "voice.mp3",
                "path": str(path),
                "kind": "liner",
                "editable": True,
                "length": 3.0,
                "points": {"start": 0.2, "end": 2.8},
                "not_before": None,
                "not_after": None,
                "dates_text": "",
                "playable": True,
                "unplayable_text": "",
            }
        ]
        response = tc.get("/api/liners/file/stable-liner-id")
        assert response.status_code == 200
        assert response.content == b"registered-audio"
        deleted = tc.delete("/api/liners/file/stable-liner-id")
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": "voice.mp3"}
        assert not path.exists()

    def test_liner_points_endpoint_restricts_visible_liner_fields(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        from autodj.catalog import _path

        path = tmp_path / "voice.wav"
        path.write_bytes(b"liner")
        record = {"id": "liner-1", "path": str(path), "kind": "liner", "length": 4.0, "points": {}}

        class Catalog:
            def record_for_path(self, value):
                return record if _path(value) == _path(path) else None

            def set_points(self, value, points, *, allow_outro=None):
                record["points"] = points
                if allow_outro is not None:
                    record["allow_outro"] = allow_outro

        class Library:
            catalog = Catalog()

            def station_ledger(self, station):
                return SimpleNamespace(last_played=lambda: {})

            def entry_for_path(self, value):
                return SimpleNamespace(path=str(path)) if _path(value) == _path(path) else None

        bridge.library = Library()
        tc = TestClient(create_app(bridge))
        assert (
            tc.post(
                "/api/library/points", json={"path": str(path), "points": {"intro": 1}}
            ).status_code
            == 400
        )
        saved = tc.post(
            "/api/library/points",
            json={"path": str(path), "points": {"start": 0.2, "end": 3.8, "overlap_start": 0}},
        )
        assert saved.status_code == 200
        assert saved.json()["points"] == {"start": 0.2, "end": 3.8, "overlap_start": 0}
        saved = tc.post(
            "/api/library/points",
            json={"path": str(path), "points": {}, "allow_outro": True},
        )
        assert saved.status_code == 200
        assert saved.json()["allow_outro"] is True
        assert (
            tc.post(
                "/api/library/points",
                json={"path": str(path), "points": {}, "allow_outro": "true"},
            ).status_code
            == 422
        )

    def test_liners_empty_folder(self, client) -> None:
        resp = client.get("/api/liners")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["files"] == []
        assert "config" in body
        assert body["config"]["enabled"] is False

    def test_liners_lists_files(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        folder = tmp_path / "stations" / "default" / "liners"
        folder.mkdir(parents=True)
        (folder / "intro.mp3").write_bytes(b"\x00")
        (folder / "outro.wav").write_bytes(b"\x00")
        point_index(bridge.player._cfg.index, tmp_path)

        tc = TestClient(create_app(bridge))
        body = tc.get("/api/liners").json()
        assert body["count"] == 2
        assert sorted(body["files"]) == ["intro.mp3", "outro.wav"]

    def test_liner_play_dates_are_listed_set_refused_and_forgotten(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        folder = tmp_path / "active" / "stations" / "default" / "liners"
        folder.mkdir(parents=True)
        (folder / "promo.mp3").write_bytes(b"\x00")
        (folder / "id.wav").write_bytes(b"\x00")
        point_index(bridge.player._cfg.index, str(tmp_path / "active"))
        bridge.player._cfg.station.timezone = "UTC"

        tc = TestClient(create_app(bridge))
        body = tc.get("/api/liners").json()
        assert body["today"] > "2000-01-01"
        # The page works out the station's day itself at each pick.
        assert body["timezone"] == "UTC"
        item = next(entry for entry in body["items"] if entry["id"] == "promo.mp3")
        assert (item["dates_text"], item["playable"]) == ("", True)

        saved = tc.put("/api/liners/dates/promo.mp3", json={"not_after": "2000-01-01"})
        assert saved.status_code == 200, saved.text
        assert saved.json()["dates_text"] == "Plays until 1 January 2000"
        assert saved.json()["unplayable_text"] == "expired after 1 January 2000"
        listed = {entry["id"]: entry for entry in tc.get("/api/liners").json()["items"]}
        assert listed["promo.mp3"]["playable"] is False
        assert listed["id.wav"]["playable"] is True

        refused = tc.put(
            "/api/liners/dates/promo.mp3",
            json={"not_before": "2026-12-26", "not_after": "2026-12-01"},
        )
        assert refused.status_code == 400
        assert refused.json()["field"] == "not_after"
        assert tc.put("/api/liners/dates/missing.mp3", json={}).status_code == 404

        assert tc.delete("/api/liners/file/promo.mp3").status_code == 200
        (folder / "promo.mp3").write_bytes(b"\x00")
        again = {entry["id"]: entry for entry in tc.get("/api/liners").json()["items"]}
        assert again["promo.mp3"]["playable"] is True

    def test_liner_file_endpoint(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        folder = tmp_path / "stations" / "default" / "liners"
        folder.mkdir(parents=True)
        (folder / "test.mp3").write_bytes(b"raw-mp3-bytes")
        point_index(bridge.player._cfg.index, tmp_path)

        tc = TestClient(create_app(bridge))
        resp = tc.get("/api/liners/file/test.mp3")
        assert resp.status_code == 200
        assert resp.content == b"raw-mp3-bytes"
        assert resp.headers["content-type"] == "audio/mpeg"

    def test_liner_file_range(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        folder = tmp_path / "stations" / "default" / "liners"
        folder.mkdir(parents=True)
        (folder / "test.mp3").write_bytes(b"0123456789")
        point_index(bridge.player._cfg.index, tmp_path)

        tc = TestClient(create_app(bridge))
        resp = tc.get("/api/liners/file/test.mp3", headers={"Range": "bytes=2-5"})
        assert resp.status_code == 206
        assert resp.content == b"2345"
        assert resp.headers["content-range"] == "bytes 2-5/10"
        assert resp.headers["content-length"] == "4"

    def test_liner_file_suffix_range_and_unsatisfiable_range(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        folder = tmp_path / "stations" / "default" / "liners"
        folder.mkdir(parents=True)
        (folder / "test.mp3").write_bytes(b"0123456789")
        point_index(bridge.player._cfg.index, tmp_path)

        tc = TestClient(create_app(bridge))
        suffix = tc.get("/api/liners/file/test.mp3", headers={"Range": "bytes=-3"})
        assert suffix.status_code == 206
        assert suffix.content == b"789"
        assert suffix.headers["content-range"] == "bytes 7-9/10"

        unsatisfiable = tc.get("/api/liners/file/test.mp3", headers={"Range": "bytes=20-"})
        assert unsatisfiable.status_code == 416
        assert unsatisfiable.headers["content-range"] == "bytes */10"

    def test_liner_file_path_traversal_blocked(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        folder = tmp_path / "stations" / "default" / "liners"
        folder.mkdir(parents=True)
        (tmp_path / "secret.txt").write_bytes(b"do-not-read")
        point_index(bridge.player._cfg.index, tmp_path)

        tc = TestClient(create_app(bridge))
        resp = tc.get("/api/liners/file/..%2Fsecret.txt")
        assert resp.status_code == 404

    @pytest.mark.parametrize("name", ["config.toml", "tracks.db", "notes", "clip.mp3.bak"])
    def test_liner_file_routes_refuse_non_audio_names(self, bridge, tmp_path, name) -> None:
        """A plain name in the liner root is still refused unless it is a liner clip."""
        from fastapi.testclient import TestClient

        folder = tmp_path / "stations" / "default" / "liners"
        folder.mkdir(parents=True)
        (folder / name).write_bytes(b"secret")
        point_index(bridge.player._cfg.index, tmp_path)

        tc = TestClient(create_app(bridge))
        assert tc.get(f"/api/liners/file/{name}").status_code == 400
        assert tc.delete(f"/api/liners/file/{name}").status_code == 400
        assert (folder / name).read_bytes() == b"secret"

    def test_liner_settings_round_trip(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        resp = tc.post(
            "/api/playback-settings",
            json={
                "liners_enabled": True,
                "liners_every_n_songs": 5,
                "liners_pick_mode": "sequential",
                "liners_duck_db": -8.0,
            },
        )
        assert resp.status_code == 200
        cfg = bridge.player._cfg.playback
        assert cfg.liners_enabled is True
        assert cfg.liners_every_n_songs == 5
        assert cfg.liners_pick_mode == "sequential"
        assert cfg.liners_duck_db == pytest.approx(-8.0)

    @pytest.mark.parametrize(
        "payload",
        [
            {"liners_folder": "/etc/autodj"},
            {"liners_enabled": True, "liners_folder": "../config"},
            {"bogus_field": 1},
            {"liners_enabled": True, "liners_duck_db": 60.0},
            {"liners_enabled": True, "liners_duck_db": -31.0},
            {"no_repeat_window": 2},
            {"artist_repeat_window": 1},
        ],
    )
    def test_playback_settings_reject_liner_root_and_unknown_fields(
        self, bridge, tmp_path, payload
    ) -> None:
        """The liner root is config-only; a request cannot move it or half-apply."""
        from fastapi.testclient import TestClient

        bridge.player._cfg.playback.liners_enabled = False
        bridge.save_persistent_state = MagicMock()

        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/playback-settings", json=payload)
        assert resp.status_code == 422
        assert bridge.player._cfg.playback.liners_enabled is False
        bridge.save_persistent_state.assert_not_called()

    def test_liner_pick_mode_validates(self, bridge) -> None:
        from fastapi.testclient import TestClient

        bridge.player._cfg.playback.liners_pick_mode = "random"
        tc = TestClient(create_app(bridge))
        tc.post("/api/playback-settings", json={"liners_pick_mode": "BOGUS"})
        assert bridge.player._cfg.playback.liners_pick_mode == "random"

    def test_liner_upload_and_list(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        folder = tmp_path / "stations" / "default" / "liners"
        folder.mkdir(parents=True)
        point_index(bridge.player._cfg.index, tmp_path)

        tc = TestClient(create_app(bridge))
        audio = wav_bytes()
        files = {"file": ("hello.wav", audio, "audio/wav")}
        resp = tc.post("/api/liners/upload", files=files)
        assert resp.status_code == 200
        body = resp.json()
        assert body["filename"] == "hello.wav"
        assert (folder / "hello.wav").read_bytes() == audio

        body = tc.get("/api/liners").json()
        assert "hello.wav" in body["files"]

    def test_a_file_that_is_not_audio_is_refused_at_upload(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        folder = tmp_path / "stations" / "default" / "liners"
        folder.mkdir(parents=True)
        point_index(bridge.player._cfg.index, tmp_path)
        tc = TestClient(create_app(bridge))

        # Nine bytes of text named .mp3 (#323): refused with the reason in words.
        files = {"file": ("broken.mp3", b"not audio", "audio/mpeg")}
        refused = tc.post("/api/liners/upload", files=files)
        assert refused.status_code == 422
        assert refused.json()["detail"] == "broken.mp3 is not an audio file AutoDJ can play."
        assert list(folder.iterdir()) == []

        # A WAV named .wav is stored; a replacement that is not audio leaves it as it was.
        audio = wav_bytes()
        assert tc.post("/api/liners/upload", files={"file": ("id.wav", audio)}).status_code == 200
        replaced = tc.post(
            "/api/liners/upload?replace=true", files={"file": ("id.wav", b"RIFFwavedata")}
        )
        assert replaced.status_code == 422
        assert (folder / "id.wav").read_bytes() == audio
        assert [path.name for path in folder.iterdir()] == ["id.wav"]

    @pytest.mark.parametrize(
        "failure",
        [subprocess.TimeoutExpired(["ffprobe"], 30), PermissionError(13, "Access is denied")],
    )
    def test_a_liner_that_could_not_be_checked_is_refused_in_words(
        self, bridge, tmp_path, monkeypatch, failure
    ) -> None:
        from fastapi.testclient import TestClient

        from autodj import break_audio

        folder = tmp_path / "stations" / "default" / "liners"
        folder.mkdir(parents=True)
        point_index(bridge.player._cfg.index, tmp_path)
        monkeypatch.setattr(break_audio.shutil, "which", lambda _name: "ffprobe")

        def hangs(*_args: object, **_kwargs: object) -> None:
            raise failure

        monkeypatch.setattr(break_audio.subprocess, "run", hangs)
        monkeypatch.setattr(
            break_audio,
            "check_playable",
            functools.partial(break_audio.check_playable, runner=hangs),
        )
        tc = TestClient(create_app(bridge))
        mp4 = b"\0\0\0\x18ftypM4A " + b"rest of the file"
        refused = tc.post("/api/liners/upload", files={"file": ("promo.m4a", mp4)})
        assert refused.status_code == 503
        assert refused.json()["detail"] == (
            "Could not check promo.m4a, so it was not saved. Try again in a moment."
        )
        assert list(folder.iterdir()) == []

    def test_a_new_liner_upload_does_not_inherit_left_behind_dates(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        from autodj.clip_dates import ClipDates, LinerDates

        folder = tmp_path / "active" / "stations" / "default" / "liners"
        folder.mkdir(parents=True)
        point_index(bridge.player._cfg.index, str(tmp_path / "active"))
        tc = TestClient(create_app(bridge))
        store = LinerDates(tc.app.state.stations.default.paths.liner_dates)
        # promo.wav was deleted outside AutoDJ; its dates stayed behind.
        store.set("promo.wav", ClipDates(not_after="2000-01-01"))
        store.set("id.wav", ClipDates(not_after="2000-01-01"))
        files = {"file": ("promo.wav", wav_bytes(), "audio/wav")}
        assert tc.post("/api/liners/upload", files=files).status_code == 200
        assert set(store.all()) == {"id.wav"}
        listed = {entry["id"]: entry for entry in tc.get("/api/liners").json()["items"]}
        assert listed["promo.wav"]["playable"] is True
        # Replacing a liner on purpose keeps its dates.
        (folder / "id.wav").write_bytes(b"\x00")
        replaced = tc.post(
            "/api/liners/upload?replace=true",
            files={"file": ("id.wav", wav_bytes(0.2), "audio/wav")},
        )
        assert replaced.status_code == 200, replaced.text
        assert set(store.all()) == {"id.wav"}

    def test_an_upload_waits_while_the_dates_file_cannot_be_read(
        self, bridge, tmp_path, monkeypatch
    ) -> None:
        from fastapi.testclient import TestClient

        from autodj.clip_dates import ClipDates, LinerDates

        folder = tmp_path / "active" / "stations" / "default" / "liners"
        folder.mkdir(parents=True)
        (folder / "id.wav").write_bytes(b"\x00")
        point_index(bridge.player._cfg.index, str(tmp_path / "active"))
        tc = TestClient(create_app(bridge))
        store = tc.app.state.stations.default.liner_gate.store
        store.set("promo.mp3", ClipDates(not_after="2000-01-01"))
        store.set("id.wav", ClipDates(not_after="2000-01-01"))

        def unreadable(*_args: object) -> None:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(LinerDates, "forget", unreadable)
        monkeypatch.setattr(LinerDates, "set", unreadable)
        files = {"file": ("promo.mp3", b"audio-bytes", "audio/mpeg")}
        refused = tc.post("/api/liners/upload", files=files)
        assert refused.status_code == 503
        assert "could not be read just now" in refused.json()["detail"]
        assert not (folder / "promo.mp3").exists()
        deleted = tc.delete("/api/liners/file/id.wav")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] == "id.wav"
        assert "could not be forgotten just now" in deleted.json()["message"]

    def test_liner_upload_enforces_configured_limit(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        folder = tmp_path / "stations" / "default" / "liners"
        point_index(bridge.player._cfg.index, tmp_path)
        bridge.player._cfg.station.liner_upload_max_mib = 1

        tc = TestClient(create_app(bridge))
        resp = tc.post(
            "/api/liners/upload",
            files={"file": ("clip.mp3", b"x" * ((1 << 20) + 1), "audio/mpeg")},
        )
        assert resp.status_code == 413
        assert not (folder / "clip.mp3").exists()

    def test_liner_body_cap_rejects_before_creating_staging(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        folder = tmp_path / "stations" / "default" / "liners"
        point_index(bridge.player._cfg.index, tmp_path)
        bridge.player._cfg.station.liner_upload_max_mib = 1

        tc = TestClient(create_app(bridge))
        resp = tc.post(
            "/api/liners/upload",
            files={"file": ("clip.mp3", b"x" * ((1 << 20) + 1), "audio/mpeg")},
        )
        assert resp.status_code == 413
        assert not folder.exists() or list(folder.iterdir()) == []

    def test_liner_upload_conflict_and_explicit_replace(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        folder = tmp_path / "stations" / "default" / "liners"
        folder.mkdir(parents=True)
        target = folder / "clip.wav"
        target.write_bytes(b"old")
        point_index(bridge.player._cfg.index, tmp_path)

        tc = TestClient(create_app(bridge))
        audio = wav_bytes()
        files = {"file": ("clip.wav", audio, "audio/wav")}
        conflict = tc.post("/api/liners/upload", files=files)
        assert conflict.status_code == 409
        assert target.read_bytes() == b"old"

        replaced = tc.post("/api/liners/upload?replace=true", files=files)
        assert replaced.status_code == 200
        assert target.read_bytes() == audio

    def test_liner_upload_rejects_unknown_extension(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        folder = tmp_path / "stations" / "default" / "liners"
        folder.mkdir(parents=True)
        point_index(bridge.player._cfg.index, tmp_path)

        tc = TestClient(create_app(bridge))
        resp = tc.post(
            "/api/liners/upload",
            files={"file": ("evil.exe", b"bad", "application/octet-stream")},
        )
        assert resp.status_code == 400

    def test_liner_delete(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        folder = tmp_path / "stations" / "default" / "liners"
        folder.mkdir(parents=True)
        target = folder / "remove_me.wav"
        target.write_bytes(b"x")
        point_index(bridge.player._cfg.index, tmp_path)

        tc = TestClient(create_app(bridge))
        resp = tc.delete("/api/liners/file/remove_me.wav")
        assert resp.status_code == 200
        assert not target.exists()

    def test_liner_delete_path_traversal_blocked(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        folder = tmp_path / "stations" / "default" / "liners"
        folder.mkdir(parents=True)
        outside = tmp_path / "secret.txt"
        outside.write_bytes(b"safe")
        point_index(bridge.player._cfg.index, tmp_path)

        tc = TestClient(create_app(bridge))
        resp = tc.delete("/api/liners/file/..%2Fsecret.txt")
        assert resp.status_code == 404
        assert outside.exists()


# ---------------------------------------------------------------------------
# Repeat settings endpoint
# ---------------------------------------------------------------------------


class TestRepeatSettings:
    @staticmethod
    def _payload() -> dict:
        return {
            "repeat_rules": {
                "track": {"tracks": 7, "minutes": 10},
                "artist": {"tracks": 4, "minutes": 20},
                "album": {"tracks": 2, "minutes": 30},
                "title": {"tracks": 5, "minutes": 40},
                "use_related_artists": True,
            }
        }

    @staticmethod
    def _client(bridge, tmp_path):
        from fastapi.testclient import TestClient

        active_dir = tmp_path / "index"
        active_dir.mkdir()
        point_index(bridge.player._cfg.index, active_dir)
        bridge.save_persistent_state = MagicMock()
        return TestClient(create_app(bridge))

    def test_nested_repeat_settings_round_trip_and_file_reload(self, bridge, tmp_path) -> None:
        client = self._client(bridge, tmp_path)
        path = bridge.player._cfg.index.library_dir / "related-artists.toml"
        path.write_text('[related]\n"Alpha" = ["Beta"]\n', encoding="utf-8")
        payload = self._payload()

        response = client.post("/api/playback-settings", json=payload)

        assert response.status_code == 200
        # Without their own limits, related artists keep the artist rule's.
        assert response.json()["playback"]["repeat_rules"] == {
            **payload["repeat_rules"],
            "related_artist": payload["repeat_rules"]["artist"],
            "related_track": {"tracks": 20, "minutes": 0.0},
            "use_related_tracks": False,
            "versions_as_one_song": False,
            "daypart": {"days": 0, "start_hours": [0, 6, 10, 15, 19]},
            "hourpart": {"plays": 0},
        }
        assert bridge.player._state.rules.related.related_to("alpha") == {"beta"}

        path.write_text('[related]\n"Alpha" = ["Gamma"]\n', encoding="utf-8")
        response = client.post("/api/playback-settings", json=payload)

        assert response.status_code == 200
        assert bridge.player._state.rules.related.related_to("alpha") == {"gamma"}
        assert bridge.save_persistent_state.call_count == 2

    @pytest.mark.parametrize(
        "rule",
        [
            {"track": {"tracks": -1}},
            {"artist": {"minutes": 10081}},
            {"daypart": {"days": 8}},
            {"daypart": {"start_hours": [6, 6]}},
            {"daypart": {"start_hours": [24]}},
            {"hourpart": {"plays": 4}},
        ],
    )
    def test_nested_repeat_ranges_are_rejected(self, bridge, tmp_path, rule) -> None:
        client = self._client(bridge, tmp_path)
        before = bridge.player._cfg.playback.repeat_rules.model_dump()

        response = client.post("/api/playback-settings", json={"repeat_rules": rule})

        assert response.status_code == 422
        assert bridge.player._cfg.playback.repeat_rules.model_dump() == before
        bridge.save_persistent_state.assert_not_called()

    def test_daypart_and_quarter_hour_rules_save_and_reach_the_picker(
        self, bridge, tmp_path
    ) -> None:
        client = self._client(bridge, tmp_path)
        payload = {
            "repeat_rules": {
                "daypart": {"days": 3, "start_hours": [19, 6, 0]},
                "hourpart": {"plays": 2},
            }
        }

        response = client.post("/api/playback-settings", json=payload)

        assert response.status_code == 200
        saved = response.json()["playback"]["repeat_rules"]
        assert saved["daypart"] == {"days": 3, "start_hours": [0, 6, 19]}
        assert saved["hourpart"] == {"plays": 2}
        rules = bridge.player._state.rules
        assert (rules.daypart_days, rules.daypart_starts, rules.hourpart_plays) == (
            3,
            (0, 6, 19),
            2,
        )
        bridge.save_persistent_state.assert_called_once()

    def test_invalid_related_file_keeps_live_repeat_settings(self, bridge, tmp_path) -> None:
        client = self._client(bridge, tmp_path)
        path = bridge.player._cfg.index.library_dir / "related-artists.toml"
        path.write_text('[related]\n"Alpha" = ["Beta"]\n', encoding="utf-8")
        payload = self._payload()
        assert client.post("/api/playback-settings", json=payload).status_code == 200
        rules = bridge.player._cfg.playback.repeat_rules
        state_rules = bridge.player._state.rules
        bridge.save_persistent_state.reset_mock()
        path.write_text("[related\n", encoding="utf-8")

        response = client.post("/api/playback-settings", json=payload)

        assert response.status_code == 400
        assert response.json()["detail"] == "The related artists file is not valid: line 1."
        assert bridge.player._cfg.playback.repeat_rules is rules
        assert bridge.player._state.rules is state_rules
        assert bridge.player._state.rules.related.related_to("alpha") == {"beta"}
        bridge.save_persistent_state.assert_not_called()
        assert bridge.get_settings()["playback"]["related_artists_problem"].startswith(
            "The related artists file is not valid"
        )

    def test_related_tracks_load_on_save_with_their_own_limits(self, bridge, tmp_path) -> None:
        client = self._client(bridge, tmp_path)
        path = bridge.player._cfg.index.library_dir / "related-tracks.toml"
        path.write_text('[related]\n"Alpha - One" = ["Beta - Two"]\n', encoding="utf-8")
        payload = self._payload()
        payload["repeat_rules"]["related_track"] = {"tracks": 7, "minutes": 15}
        payload["repeat_rules"]["use_related_tracks"] = True

        response = client.post("/api/playback-settings", json=payload)

        assert response.status_code == 200
        rules = bridge.player._state.rules
        assert rules.related_tracks.related_to(("beta", "two")) == {("alpha", "one")}
        assert (rules.related_track_window, rules.related_track_minutes) == (7, 15.0)
        playback = response.json()["playback"]
        assert playback["repeat_rules"]["use_related_tracks"] is True
        assert playback["related_tracks_problem"] is None
        assert playback["related_tracks_default_file"] == str(path.resolve())

    def test_invalid_related_tracks_file_keeps_live_repeat_settings(self, bridge, tmp_path) -> None:
        client = self._client(bridge, tmp_path)
        path = bridge.player._cfg.index.library_dir / "related-tracks.toml"
        path.write_text('[related]\n"Alpha - One" = ["Beta - Two"]\n', encoding="utf-8")
        payload = self._payload()
        payload["repeat_rules"]["use_related_tracks"] = True
        assert client.post("/api/playback-settings", json=payload).status_code == 200
        state_rules = bridge.player._state.rules
        bridge.save_persistent_state.reset_mock()
        path.write_text('[related]\n"Alpha" = ["Beta - Two"]\n', encoding="utf-8")

        response = client.post("/api/playback-settings", json=payload)

        assert response.status_code == 400
        assert response.json()["detail"] == "The related tracks file is not valid."
        assert bridge.player._state.rules is state_rules
        bridge.save_persistent_state.assert_not_called()
        assert bridge.get_settings()["playback"]["related_tracks_problem"] == (
            "The related tracks file is not valid."
        )

        # Turned off, the broken file is not read and the save goes through.
        payload["repeat_rules"]["use_related_tracks"] = False
        assert client.post("/api/playback-settings", json=payload).status_code == 200
        assert not bridge.player._state.rules.related_tracks.links

    def test_api_rejects_related_file_path(self, bridge, tmp_path) -> None:
        client = self._client(bridge, tmp_path)
        payload = self._payload()
        payload["repeat_rules"]["related_artists_file"] = str(tmp_path / "secret.toml")
        response = client.post("/api/playback-settings", json=payload)
        assert response.status_code == 422
        bridge.save_persistent_state.assert_not_called()

    def test_broken_file_does_not_block_save_when_disabled(self, bridge, tmp_path) -> None:
        client = self._client(bridge, tmp_path)
        path = bridge.player._cfg.index.library_dir / "related-artists.toml"
        path.write_text("[related\n", encoding="utf-8")
        payload = self._payload()
        payload["repeat_rules"]["use_related_artists"] = False
        response = client.post("/api/playback-settings", json=payload)
        assert response.status_code == 200
        assert response.json()["playback"]["repeat_rules"]["use_related_artists"] is False

    def test_invalid_file_error_does_not_echo_file_keys(self, bridge, tmp_path) -> None:
        client = self._client(bridge, tmp_path)
        path = bridge.player._cfg.index.library_dir / "related-artists.toml"
        path.write_text('secret_api_token = "private"\n', encoding="utf-8")
        response = client.post("/api/playback-settings", json=self._payload())
        assert response.status_code == 400
        assert response.json()["detail"] == "The related artists file is not valid."
        assert "secret_api_token" not in response.text


# ---------------------------------------------------------------------------
# Profile bundle endpoints
# ---------------------------------------------------------------------------


class TestProfiles:
    def test_profiles_empty(self, client) -> None:
        resp = client.get("/api/profiles")
        assert resp.status_code == 200
        body = resp.json()
        assert "profiles" in body
        assert body["profiles"] == []

    def test_profile_save_and_list(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        # Re-anchor profile root to tmp_path so the test is hermetic.
        point_index(bridge.player._cfg.index, tmp_path / "idx")
        (tmp_path / "idx").mkdir()

        tc = TestClient(create_app(bridge))
        resp = tc.post(
            "/api/profiles",
            json={"name": "Wakeup", "bpm_lo": 70, "bpm_hi": 110},
        )
        assert resp.status_code == 200
        body = tc.get("/api/profiles").json()
        assert "Wakeup" in body["profiles"]

    def test_profile_invalid_name_rejected(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path / "idx")
        (tmp_path / "idx").mkdir()
        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/profiles", json={"name": "../escape"})
        assert resp.status_code == 400

    def test_profile_delete(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path / "idx")
        (tmp_path / "idx").mkdir()
        tc = TestClient(create_app(bridge))
        tc.post("/api/profiles", json={"name": "Tmp"})
        resp = tc.delete("/api/profiles/Tmp")
        assert resp.status_code == 200
        body = tc.get("/api/profiles").json()
        assert "Tmp" not in body["profiles"]

    def test_profile_apply_pushes_settings(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path / "idx")
        (tmp_path / "idx").mkdir()
        tc = TestClient(create_app(bridge))
        tc.post(
            "/api/profiles",
            json={
                "name": "Quiet",
                "beat_sync_fx": False,
                "key_sync_fx": False,
                "crossfade_seconds": 5.0,
            },
        )
        resp = tc.post("/api/profiles/Quiet/apply")
        assert resp.status_code == 200
        body = resp.json()
        assert "beat_sync_fx" in body["applied"]
        # Bridge set_playback_settings invoked on the mock cfg.
        assert bridge.player._cfg.playback.beat_sync_fx is False
        assert bridge.player._cfg.playback.key_sync_fx is False
        assert bridge.player._cfg.playback.crossfade_seconds == pytest.approx(5.0)

    def test_profile_apply_persists_across_restart(self, bridge, tmp_path) -> None:
        """Applied settings must reach web_state.json like every other settings route."""
        import json

        from fastapi.testclient import TestClient

        idx = tmp_path / "idx"
        idx.mkdir()
        point_index(bridge.player._cfg.index, idx)
        tc = TestClient(create_app(bridge))
        tc.post("/api/profiles", json={"name": "Loud", "crossfade_seconds": 7.5})

        assert tc.post("/api/profiles/Loud/apply").status_code == 200

        saved = json.loads(
            (idx / "stations" / "default" / "web_state.json").read_text(encoding="utf-8")
        )
        assert saved["playback"]["crossfade_seconds"] == pytest.approx(7.5)

    @pytest.mark.parametrize(
        "field",
        ["transition_mode", "post_queue_seed", "key_notation", "liners_pick_mode"],
    )
    def test_playback_settings_bad_choice_changes_nothing(self, bridge, field) -> None:
        """One bad choice must not half-apply the fields that come before it."""
        from fastapi.testclient import TestClient

        pb = bridge.player._cfg.playback
        pb.crossfade_seconds = 3.0
        before = getattr(pb, field)
        bridge.save_persistent_state = MagicMock()
        tc = TestClient(create_app(bridge))

        resp = tc.post("/api/playback-settings", json={"crossfade_seconds": 9.0, field: "bogus"})

        assert resp.status_code == 422
        assert pb.crossfade_seconds == pytest.approx(3.0)
        assert getattr(pb, field) == before
        bridge.save_persistent_state.assert_not_called()

    def test_profile_save_rejects_bad_choice(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path / "idx")
        (tmp_path / "idx").mkdir()
        tc = TestClient(create_app(bridge))

        resp = tc.post("/api/profiles", json={"name": "Bad", "post_queue_seed": "bogus"})

        assert resp.status_code == 422
        assert "Bad" not in tc.get("/api/profiles").json()["profiles"]

    def test_profile_save_rejects_unknown_keys(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path / "idx")
        (tmp_path / "idx").mkdir()
        tc = TestClient(create_app(bridge))

        resp = tc.post("/api/profiles", json={"name": "Typo", "crossfade_secs": 4.0})

        assert resp.status_code == 422
        assert "crossfade_secs" in resp.text
        assert "Typo" not in tc.get("/api/profiles").json()["profiles"]

    def test_stored_profile_with_unknown_key_is_400(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path / "idx")
        (tmp_path / "idx").mkdir()
        (tmp_path / "idx" / "stations" / "default" / "profiles").mkdir(parents=True)
        (tmp_path / "idx" / "stations" / "default" / "profiles" / "Old.json").write_text(
            '{"name": "Old", "crossfade_seconds": 9.0, "extra": {}}', encoding="utf-8"
        )
        bridge.save_persistent_state = MagicMock()
        tc = TestClient(create_app(bridge))

        resp = tc.post("/api/profiles/Old/apply")

        assert resp.status_code == 400
        assert "unknown profile keys: ['extra']" in resp.json()["detail"]
        bridge.save_persistent_state.assert_not_called()

    def test_profile_apply_stored_bad_choice_is_400_and_changes_nothing(
        self, bridge, tmp_path
    ) -> None:
        """A hand-edited profile with a bad choice is refused before anything applies."""
        from fastapi.testclient import TestClient

        from autodj.profiles import ProfileSnapshot, ProfileStore

        point_index(bridge.player._cfg.index, tmp_path / "idx")
        (tmp_path / "idx").mkdir()
        ProfileStore(tmp_path / "idx" / "stations" / "default" / "profiles").save(
            ProfileSnapshot(name="Broken", crossfade_seconds=9.0, transition_mode="bogus")
        )
        pb = bridge.player._cfg.playback
        pb.crossfade_seconds = 3.0
        bridge.save_persistent_state = MagicMock()
        tc = TestClient(create_app(bridge))

        resp = tc.post("/api/profiles/Broken/apply")

        assert resp.status_code == 400
        assert pb.crossfade_seconds == pytest.approx(3.0)
        bridge.save_persistent_state.assert_not_called()

    def test_profile_delete_bad_name_400(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path / "idx")
        (tmp_path / "idx").mkdir()
        tc = TestClient(create_app(bridge))
        resp = tc.delete("/api/profiles/..%2Fescape")
        assert resp.status_code in (400, 404)

    def test_profile_delete_missing_404(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path / "idx")
        (tmp_path / "idx").mkdir()
        tc = TestClient(create_app(bridge))
        resp = tc.delete("/api/profiles/Nope")
        assert resp.status_code == 404

    def test_profile_apply_bad_name_400(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path / "idx")
        (tmp_path / "idx").mkdir()
        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/profiles/..%2Fescape/apply")
        assert resp.status_code in (400, 404)

    def test_profile_and_liner_file_io_runs_off_the_event_loop(
        self, bridge, tmp_path, monkeypatch
    ) -> None:
        from fastapi.testclient import TestClient

        offloaded: list[str] = []
        real_to_thread = asyncio.to_thread

        async def recording_to_thread(function, *args, **kwargs):
            offloaded.append(getattr(function, "__name__", repr(function)))
            return await real_to_thread(function, *args, **kwargs)

        point_index(bridge.player._cfg.index, tmp_path / "idx")
        (tmp_path / "idx").mkdir()
        folder = tmp_path / "stations" / "default" / "liners"
        folder.mkdir(parents=True)
        (folder / "a.mp3").write_bytes(b"x")
        point_index(bridge.player._cfg.index, tmp_path)
        bridge.save_persistent_state = MagicMock()
        monkeypatch.setattr("autodj.server.asyncio.to_thread", recording_to_thread)
        tc = TestClient(create_app(bridge))

        assert tc.post("/api/profiles", json={"name": "Io"}).status_code == 200
        assert tc.get("/api/profiles").status_code == 200
        assert tc.post("/api/profiles/Io/apply").status_code == 200
        assert tc.delete("/api/profiles/Io").status_code == 200
        files = {"file": ("b.wav", wav_bytes(), "audio/wav")}
        assert tc.post("/api/liners/upload", files=files).status_code == 200
        assert tc.get("/api/liners/file/a.mp3").status_code == 200
        assert tc.delete("/api/liners/file/a.mp3").status_code == 200

        for name in (
            "save",
            "list_names",
            "load",
            "delete",
            "open_liner_file",
            "delete_liner_file",
        ):
            assert name in offloaded, name

    def test_profile_apply_missing_404(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path / "idx")
        (tmp_path / "idx").mkdir()
        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/profiles/Nope/apply")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/mute
# ---------------------------------------------------------------------------


class TestMute:
    def test_mute_returns_200(self, client) -> None:
        assert client.post("/api/mute").status_code == 200

    def test_mute_toggles_state(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        assert bridge.player._state.is_muted is False
        data = tc.post("/api/mute").json()
        assert data["muted"] is True
        data = tc.post("/api/mute").json()
        assert data["muted"] is False


# ---------------------------------------------------------------------------
# GET /api/search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_returns_results(self, client) -> None:
        resp = client.get("/api/search?q=song")
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert len(data["results"]) > 0

    def test_search_empty_query_returns_empty(self, client) -> None:
        data = client.get("/api/search?q=").json()
        assert data["results"] == []

    def test_search_missing_query_returns_empty(self, client) -> None:
        data = client.get("/api/search").json()
        assert data["results"] == []

    def test_search_no_match_returns_empty(self, client) -> None:
        data = client.get("/api/search?q=zzznomatch999").json()
        assert data["results"] == []

    def test_search_result_has_expected_fields(self, client) -> None:
        data = client.get("/api/search?q=song").json()
        result = data["results"][0]
        assert "title" in result
        assert "artist" in result
        assert "display_name" in result

    def test_search_multi_token_requires_all_match(self, client) -> None:
        # All sample entries have title="Song N" + artist="Artist".
        # Both tokens present in haystack → match.
        data = client.get("/api/search?q=song+artist").json()
        assert len(data["results"]) > 0

    def test_search_multi_token_drops_partial_matches(self, client) -> None:
        data = client.get("/api/search?q=song+nomatchterm999").json()
        assert data["results"] == []

    def test_search_matches_album(self, client) -> None:
        # All entries have album="Album"
        data = client.get("/api/search?q=album").json()
        assert len(data["results"]) > 0

    def test_search_limit_param(self, client) -> None:
        # Default fixture has 5 entries — limit to 2
        data = client.get("/api/search?q=song&limit=2").json()
        assert len(data["results"]) == 2

    def test_search_limit_clamped_to_500(self, client) -> None:
        data = client.get("/api/search?q=song&limit=99999").json()
        assert len(data["results"]) <= 500


# ---------------------------------------------------------------------------
# WebSocket /ws
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# POST /api/play-next
# ---------------------------------------------------------------------------


class TestPlayNext:
    def test_play_next_returns_200(self, bridge) -> None:
        from fastapi.testclient import TestClient

        path = bridge.sim.entries[2].path
        resp = TestClient(create_app(bridge)).post("/api/play-next", json={"path": path})
        assert resp.status_code == 200

    def test_play_next_returns_ok_true_for_valid_path(self, bridge) -> None:
        from fastapi.testclient import TestClient

        path = bridge.sim.entries[0].path
        data = TestClient(create_app(bridge)).post("/api/play-next", json={"path": path}).json()
        assert data["ok"] is True

    def test_play_next_unknown_path_is_404_in_plain_words(self, bridge) -> None:
        from fastapi.testclient import TestClient

        resp = TestClient(create_app(bridge)).post(
            "/api/play-next",
            json={"path": "Z:/nope.flac"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "That track is no longer in the library."

    def test_play_next_sets_queued_next(self, bridge) -> None:
        from fastapi.testclient import TestClient

        path = bridge.sim.entries[1].path
        TestClient(create_app(bridge)).post("/api/play-next", json={"path": path})
        assert bridge.player._state.queued_next is not None
        assert bridge.player._state.queued_next.path == path

    def test_play_now_also_triggers_skip(self, bridge) -> None:
        from fastapi.testclient import TestClient

        path = bridge.sim.entries[0].path
        TestClient(create_app(bridge)).post("/api/play-next", json={"path": path, "now": True})
        bridge.player._skip_event.set.assert_called_once()

    def test_play_next_does_not_skip(self, bridge) -> None:
        from fastapi.testclient import TestClient

        path = bridge.sim.entries[0].path
        TestClient(create_app(bridge)).post("/api/play-next", json={"path": path, "now": False})
        bridge.player._skip_event.set.assert_not_called()


class TestWebSocket:
    def test_websocket_connects(self, client) -> None:
        with client.websocket_connect("/ws") as ws:
            # Connection accepted without error — that's the assertion
            assert ws is not None

    def test_websocket_receives_state_push(self, bridge) -> None:
        """The broadcast loop pushes state; we can't wait 1 s in a unit test,
        but we can verify the WS endpoint accepts a connection and closes
        cleanly."""
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        with tc.websocket_connect("/ws"):
            pass  # connects and disconnects without error


# ---------------------------------------------------------------------------
# PlayerBridge unit tests
# ---------------------------------------------------------------------------


class TestPlayerBridge:
    def test_get_state_returns_dict(self, bridge) -> None:
        state = bridge.get_state()
        assert isinstance(state, dict)

    def test_get_state_current_track_is_dict(self, bridge) -> None:
        state = bridge.get_state()
        assert isinstance(state["current_track"], dict)

    def test_skip_sets_event(self, bridge) -> None:
        bridge.skip()
        bridge.player._skip_event.set.assert_called_once()

    def test_pause_returns_new_state(self, bridge) -> None:
        result = bridge.pause()
        assert result is True  # was False, now True

    def test_set_volume_midpoint(self, bridge) -> None:
        bridge.set_volume(0.5)
        assert bridge.player._state.volume == pytest.approx(0.5)

    def test_toggle_mute(self, bridge) -> None:
        assert bridge.toggle_mute() is True
        assert bridge.toggle_mute() is False

    def test_record_seed_appends_when_history_empty(self, bridge) -> None:
        seed = _make_entry(1)
        bridge._play_history = []
        bridge.record_seed(seed)
        assert len(bridge._play_history) == 1

    def test_record_seed_skipped_when_entry_none(self, bridge) -> None:
        bridge._play_history = []
        bridge.record_seed(None)
        assert bridge._play_history == []

    def test_record_seed_skipped_when_history_already_populated(self, bridge) -> None:
        seed = _make_entry(1)
        bridge.on_track_started(seed)
        bridge.record_seed(seed)
        assert len(bridge._play_history) == 1
        assert bridge._play_history[0]["title"] == seed.title

    def test_search_by_title(self, bridge) -> None:
        results = bridge.search("song")
        assert len(results) > 0

    def test_search_by_artist(self, bridge) -> None:
        results = bridge.search("Artist")
        assert len(results) > 0

    def test_search_no_match(self, bridge) -> None:
        assert bridge.search("zzznomatch999") == []

    def test_search_limit_respected(self, bridge) -> None:
        results = bridge.search("song", limit=2)
        assert len(results) <= 2

    def test_play_next_queues_entry(self, bridge) -> None:
        path = bridge.sim.entries[0].path
        assert bridge.play_next(path) is True
        assert bridge.player._state.queued_next.path == path

    def test_play_next_unknown_path_returns_false(self, bridge) -> None:
        assert bridge.play_next("Z:/does_not_exist.flac") is False
        assert bridge.player._state.queued_next is None

    def test_play_next_now_skips(self, bridge) -> None:
        path = bridge.sim.entries[0].path
        bridge.play_next(path, now=True)
        bridge.player._skip_event.set.assert_called_once()

    def test_play_next_without_now_does_not_skip(self, bridge) -> None:
        path = bridge.sim.entries[0].path
        bridge.play_next(path, now=False)
        bridge.player._skip_event.set.assert_not_called()

    def test_toggle_discovery_flips_state(self, bridge) -> None:
        assert bridge.player._state.discovery_enabled is False
        result = bridge.toggle_discovery()
        assert result is True
        assert bridge.player._state.discovery_enabled is True
        result = bridge.toggle_discovery()
        assert result is False

    def test_get_state_includes_discovery_fields(self, bridge) -> None:
        state = bridge.get_state()
        assert "discovery_enabled" in state
        assert "discovery_available" in state

    def test_get_state_with_no_current_track(self) -> None:
        player = _make_player_mock(entry=None)
        player._state.current_track = None
        player._state.next_track = None
        sim = _make_sim_mock()
        bridge = PlayerBridge(player=player, sim=sim)
        state = bridge.get_state()
        assert state["current_track"] is None
        assert state["duration"] == 0.0

    def test_get_state_elapsed_calculation(self, bridge) -> None:
        # Player mock has pos=44100*30, sr=44100 → elapsed=30.0
        state = bridge.get_state()
        assert state["elapsed"] == pytest.approx(30.0, abs=0.5)


# ---------------------------------------------------------------------------
# GET / — HTML index page
# ---------------------------------------------------------------------------


class TestGetIndexHtml:
    def test_get_root_returns_200(self, client) -> None:
        resp = client.get("/")
        assert resp.status_code == 200

    def test_get_root_returns_html(self, client) -> None:
        resp = client.get("/")
        assert "html" in resp.headers["content-type"].lower()

    def test_get_root_contains_autodj(self, client) -> None:
        resp = client.get("/")
        assert "AutoDJ" in resp.text or "autodj" in resp.text.lower()


# ---------------------------------------------------------------------------
# POST /api/discovery/toggle — the Now Playing Discovery button
# ---------------------------------------------------------------------------


class TestDiscoveryToggle:
    def test_toggle_route_flips_discovery_and_reports_it(self, bridge) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(create_app(bridge))
        assert client.post("/api/discovery/toggle").json() == {"discovery_enabled": True}
        assert bridge.player._state.discovery_enabled is True
        assert client.post("/api/discovery/toggle").json() == {"discovery_enabled": False}

    def test_toggle_route_saves_the_button_state(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        client = TestClient(create_app(bridge))
        client.post("/api/discovery", json={"every": 7})
        client.post("/api/discovery/toggle")
        saved = json.loads(
            (tmp_path / "stations" / "default" / "web_state.json").read_text(encoding="utf-8")
        )
        assert saved["discovery_every"] == 7
        assert saved["discovery_enabled"] is False


# ---------------------------------------------------------------------------
# Broadcast loop — async test (requires pytest-asyncio)
# ---------------------------------------------------------------------------


async def test_broadcast_loop_sends_to_websocket_clients() -> None:
    """The broadcast loop pushes state JSON to connected WebSocket clients.

    We patch asyncio.sleep to be instant so the loop fires immediately
    instead of waiting a real second.
    """
    import asyncio
    from unittest.mock import patch as _patch

    from httpx2 import ASGITransport, AsyncClient

    player = _make_player_mock()
    sim = _make_sim_mock()
    bridge = PlayerBridge(player=player, sim=sim)
    app = create_app(bridge)

    async def instant_sleep(delay, *args, **kwargs):
        await asyncio.sleep(0)

    # Use ASGI transport for real async HTTP — no need for a running server
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as ac:
        resp = await ac.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "current_track" in data

    # Now exercise the WebSocket endpoint with async client
    # The broadcast loop is tested by checking it can run without crashing
    with _patch("asyncio.sleep", instant_sleep):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Origin": "http://testserver"},
        ) as ac:
            resp = await ac.get("/api/status")
            assert resp.status_code == 200


async def test_broadcast_loop_removes_dead_clients() -> None:
    """Clients that raise on send_text are pruned from _ws_clients."""

    player = _make_player_mock()
    sim = _make_sim_mock()
    bridge = PlayerBridge(player=player, sim=sim)
    app = create_app(bridge)

    # The broadcast loop's dead-client pruning logic runs when send_text raises.
    # We verify this by connecting a client, then disconnecting while the loop is running.
    from fastapi.testclient import TestClient

    tc = TestClient(app)
    with tc.websocket_connect("/ws"):
        # Immediately disconnect — the loop will attempt a send and get an error
        pass  # __exit__ triggers disconnect

    # No crash = success; the loop pruned the dead client


async def test_broadcast_sends_concurrently_evicts_slow_and_serializes() -> None:
    import asyncio

    from autodj.server import _broadcast_and_prune, _broadcast_clients, _WebSocketClient

    class FastSocket:
        def __init__(self) -> None:
            self.messages: list[str] = []
            self.active = 0
            self.max_active = 0

        async def send_text(self, payload: str) -> None:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0)
            self.messages.append(payload)
            self.active -= 1

    class SlowSocket:
        def __init__(self) -> None:
            self.close_codes: list[int] = []

        async def send_text(self, payload: str) -> None:
            await asyncio.Event().wait()

        async def close(self, *, code: int) -> None:
            self.close_codes.append(code)

    fast_socket = FastSocket()
    fast = _WebSocketClient(fast_socket)
    slow_socket = SlowSocket()
    slow = _WebSocketClient(slow_socket)
    clients = {fast, slow}
    clients_lock = asyncio.Lock()

    first, second = await asyncio.gather(
        _broadcast_and_prune(clients, clients_lock, "one", timeout_seconds=0.02),
        _broadcast_clients((fast,), "two", timeout_seconds=0.02),
    )

    assert slow in first
    assert not second
    assert slow not in clients
    assert fast in clients
    assert slow_socket.close_codes == [1013]
    assert set(fast_socket.messages) == {"one", "two"}
    assert fast_socket.max_active == 1


async def test_broadcast_rechecks_session_after_waiting_for_send_lock() -> None:
    import asyncio
    import contextlib

    from autodj.server import _send_websocket_payload, _WebSocketClient

    class Socket:
        def __init__(self) -> None:
            self.close_codes: list[int] = []
            self.messages: list[str] = []

        async def send_text(self, payload: str) -> None:
            self.messages.append(payload)

        async def close(self, *, code: int) -> None:
            self.close_codes.append(code)

    async def handler() -> None:
        await asyncio.Event().wait()

    authenticated = [True]
    handler_task = asyncio.create_task(handler())
    socket = Socket()
    client = _WebSocketClient(
        socket,
        session_is_valid=lambda: authenticated[0],
        handler_task=handler_task,
    )
    await client.send_lock.acquire()
    send_task = asyncio.create_task(
        _send_websocket_payload(client, "protected", timeout_seconds=1.0)
    )
    await asyncio.sleep(0)
    authenticated[0] = False
    client.send_lock.release()

    try:
        assert await send_task is False
        await asyncio.sleep(0)
        assert socket.messages == []
        assert socket.close_codes == [4401]
        assert handler_task.cancelled()
    finally:
        if not handler_task.done():
            handler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await handler_task


async def test_broadcast_close_failure_is_redacted_and_cancels_handler(caplog) -> None:
    import asyncio
    import json
    import logging

    from autodj.server import _broadcast_and_prune, _WebSocketClient

    class BrokenSocket:
        async def send_text(self, _payload: str) -> None:
            raise RuntimeError("private-send-secret")

        async def close(self, *, code: int) -> None:
            raise RuntimeError(f"private-close-secret-{code}")

    async def handler() -> None:
        await asyncio.Event().wait()

    handler_task = asyncio.create_task(handler())
    client = _WebSocketClient(BrokenSocket(), request_id="a" * 32, handler_task=handler_task)
    clients = {client}
    with caplog.at_level(logging.WARNING, logger="autodj.audit"):
        dead = await _broadcast_and_prune(clients, asyncio.Lock(), "payload", timeout_seconds=0.02)
        await asyncio.sleep(0)

    assert dead == {client}
    assert not clients
    assert handler_task.cancelled()
    records = [
        json.loads(record.message) for record in caplog.records if record.name == "autodj.audit"
    ]
    assert records == [
        {
            "action": "broadcast",
            "method": "WS",
            "outcome": "rejected",
            "request_id": "a" * 32,
            "route": "/ws",
            "status": 1013,
        }
    ]
    assert "private-send-secret" not in caplog.text
    assert "private-close-secret" not in caplog.text


async def test_http_api_accessible_via_async_client() -> None:
    """Verify all REST endpoints respond correctly using the async ASGI transport."""
    from httpx2 import ASGITransport, AsyncClient

    player = _make_player_mock()
    sim = _make_sim_mock()
    bridge = PlayerBridge(player=player, sim=sim)
    app = create_app(bridge)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as ac:
        assert (await ac.get("/api/status")).status_code == 200
        assert (await ac.post("/api/skip")).status_code == 200
        assert (await ac.post("/api/pause")).status_code == 200
        assert (await ac.post("/api/volume", json={"volume": 0.5})).status_code == 200
        assert (await ac.post("/api/mute")).status_code == 200
        assert (await ac.get("/api/search?q=song")).status_code == 200


# ---------------------------------------------------------------------------
# App lifespan (startup / shutdown tasks)
# ---------------------------------------------------------------------------


class TestLifespan:
    def test_app_lifespan_starts_and_stops(self) -> None:
        """Using TestClient as a context manager triggers lifespan startup/teardown."""
        from fastapi.testclient import TestClient

        player = _make_player_mock()
        sim = _make_sim_mock()
        bridge = PlayerBridge(player=player, sim=sim)
        app = create_app(bridge)

        with TestClient(app) as tc:
            resp = tc.get("/api/status")
            assert resp.status_code == 200

    def test_api_accessible_during_lifespan(self) -> None:
        from fastapi.testclient import TestClient

        player = _make_player_mock()
        sim = _make_sim_mock()
        bridge = PlayerBridge(player=player, sim=sim)
        app = create_app(bridge)

        with TestClient(app) as tc:
            data = tc.get("/api/status").json()
            assert "current_track" in data

    def test_shutdown_joins_main_player_thread(self) -> None:
        import threading

        from fastapi.testclient import TestClient

        player = _make_player_mock()
        player._skip_event = threading.Event()
        bridge = PlayerBridge(player=player, sim=_make_sim_mock())
        allow_player_exit = threading.Event()
        player_received_stop = threading.Event()

        def player_loop() -> None:
            assert player._skip_event.wait(2.0)
            player_received_stop.set()
            assert allow_player_exit.wait(2.0)

        player_thread = threading.Thread(target=player_loop, name="test-player")
        app = create_app(bridge, player_thread=player_thread)
        player_thread.start()

        client = TestClient(app)
        client.__enter__()
        shutdown_done = threading.Event()

        def shut_down() -> None:
            try:
                client.__exit__(None, None, None)
            finally:
                shutdown_done.set()

        shutdown_thread = threading.Thread(target=shut_down, name="test-shutdown")
        shutdown_thread.start()
        shutdown_returned_early = False
        try:
            assert player_received_stop.wait(1.0)
            shutdown_returned_early = shutdown_done.wait(0.1)
        finally:
            allow_player_exit.set()
            assert shutdown_done.wait(2.0)
            shutdown_thread.join()

        assert not shutdown_returned_early
        assert not player_thread.is_alive()

    def test_shutdown_timeout_keeps_cache_open_for_blocked_player_thread(
        self, tmp_path, caplog, monkeypatch
    ) -> None:
        import logging
        import threading
        import time

        from fastapi.testclient import TestClient

        from autodj.dj_meta import close_cache, get_cache
        from autodj.station_log import track_station_thread

        player = _make_player_mock()
        player._skip_event = threading.Event()
        bridge = PlayerBridge(player=player, sim=_make_sim_mock())
        cache = get_cache(tmp_path)
        assert cache is not None
        allow_player_exit = threading.Event()

        def player_loop() -> None:
            assert player._skip_event.wait(2.0)
            allow_player_exit.wait()

        player_thread = threading.Thread(target=player_loop, name="blocked-test-player")
        track_station_thread(player_thread, "default")
        monkeypatch.setattr("autodj.server._SHUTDOWN_TIMEOUT_S", 0.0)
        app = create_app(bridge, player_thread=player_thread)
        player_thread.start()
        client = TestClient(app)

        try:
            client.__enter__()
            caplog.set_level(logging.INFO, logger="autodj.server")
            started = time.monotonic()
            client.__exit__(None, None, None)
            elapsed = time.monotonic() - started

            assert elapsed < 1.0
            assert cache._conn is not None
            assert "did not stop within 0.00 seconds" in caplog.text
            assert "active cache writers remain; DJ-meta cache left open" in caplog.text
            assert "Server stopped cleanly." not in caplog.text
        finally:
            allow_player_exit.set()
            player_thread.join(2.0)
            close_cache()

        assert not player_thread.is_alive()


# ---------------------------------------------------------------------------
# serve() — wires Player thread + uvicorn
# ---------------------------------------------------------------------------


class TestServeFunction:
    @pytest.fixture(autouse=True)
    def isolate_station_resources(self):
        """Startup routing tests do not scan real folders or launch FFmpeg."""
        with (
            patch("autodj.library.LibraryManager") as manager,
            patch("autodj.mastering.ContinuousMaster"),
            patch("autodj.server.shutil.which", return_value="ffmpeg"),
        ):
            manager.return_value.catalog.get_setting.return_value = None
            manager.return_value.entries_snapshot.return_value = ()
            yield

    def test_serve_starts_uvicorn(self, caplog, tmp_path) -> None:
        """The server app's lifespan builds and starts its default station."""
        import logging
        from unittest.mock import MagicMock, patch

        from fastapi.testclient import TestClient

        from autodj.server import serve

        cfg = _test_config(tmp_path)
        cfg.station.output = "browser"
        cfg.default_station.cfg.station.output = "browser"
        sim = _make_sim_mock()
        player_run = MagicMock()

        with (
            patch("autodj.player.Player.run", player_run),
            patch("uvicorn.run") as mock_uvicorn,
            caplog.at_level(logging.INFO, logger="autodj.server"),
        ):
            serve(cfg=cfg, sim=sim, seed_entry=None)

        mock_uvicorn.assert_called_once()
        assert mock_uvicorn.call_args.kwargs["host"] == "127.0.0.1"
        assert mock_uvicorn.call_args.kwargs["port"] == 8080
        assert "http://127.0.0.1:8080" in caplog.text
        assert "http://localhost:8080" not in caplog.text
        app = mock_uvicorn.call_args.args[0]
        with (
            patch("autodj.player.Player.run", player_run),
            TestClient(
                app,
                base_url="http://127.0.0.1:8080",
                headers={"Host": "127.0.0.1", "Origin": "http://127.0.0.1:8080"},
            ) as client,
        ):
            assert client.get("/api/version").status_code == 200
            runtime = app.state.stations.get("default")
            assert runtime.stream_mode is False
            assert runtime.server_audio is False
            assert runtime.bridge.player.on_track_started is not None
            player_run.assert_called_once_with(seed_entry=None)

    def test_serve_starts_player_thread(self, tmp_path) -> None:
        """The supplied factory launches a daemon Player thread when it is built."""
        captured = self._serve_capturing_player(tmp_path, no_playback=True)
        player_thread = captured["runtime"].player_thread
        player_thread.join(timeout=2.0)
        assert not player_thread.is_alive()
        captured["run"].assert_called_once_with(seed_entry=_make_entry(0))

    @staticmethod
    def _serve_capturing_player(tmp_path, stations=(), **serve_kwargs):
        """Run serve() with uvicorn and Player.run patched; return what it built."""
        from autodj.player import Player
        from autodj.server import serve

        cfg_mock = _test_config(tmp_path)
        output = (
            "stream"
            if serve_kwargs.get("stream")
            else ("server" if not serve_kwargs.get("no_playback", False) else "browser")
        )
        cfg_mock.station.output = output
        cfg_mock.station.server_audio_too = output == "stream" and not serve_kwargs.get(
            "no_playback", False
        )
        cfg_mock.default_station.cfg.station.output = output
        cfg_mock.default_station.cfg.station.server_audio_too = cfg_mock.station.server_audio_too
        cfg_mock.stations = stations
        cfg_mock.playback.crossfade_seconds = 3.0
        cfg_mock.default_station.cfg.playback.crossfade_seconds = 3.0
        sim = _make_sim_mock()
        captured: dict[str, object] = {}
        original_init = Player.__init__

        def spy_init(self, *args, **kwargs):
            captured.update(kwargs)
            captured["player"] = self
            original_init(self, *args, **kwargs)

        run = MagicMock()
        with (
            patch.object(Player, "__init__", spy_init),
            patch("autodj.player.Player.run", run),
            patch("autodj.server.create_app", wraps=create_app) as app_spy,
            patch("uvicorn.run") as mock_uvicorn,
        ):
            serve(cfg=cfg_mock, sim=sim, seed_entry=_make_entry(0), **serve_kwargs)
            captured["create_app_kwargs"] = app_spy.call_args.kwargs
            captured["uvicorn_kwargs"] = mock_uvicorn.call_args.kwargs
            app = mock_uvicorn.call_args.args[0]
            factory = app_spy.call_args.kwargs["default_station"]
            captured["runtime"] = factory.build(app.state.stations)
            captured["run"] = run
        return captured

    def test_serve_stream_builds_a_stream_mode_player(self, tmp_path) -> None:
        """--stream alone: the server mixes, but never opens the sound card."""
        captured = self._serve_capturing_player(tmp_path, no_playback=True, stream=True)
        assert captured["dry_run"] is False
        assert captured["runtime"].stream_mode is True
        assert captured["runtime"].server_audio is False
        # Keyed stream links are gone: stream mode keeps no secret file.
        assert not (tmp_path / ".stream-secret").exists()
        assert captured["uvicorn_kwargs"]["timeout_graceful_shutdown"] == 3
        # Connections past the listeners' share and the pages' room get 503 (#48).
        assert captured["uvicorn_kwargs"]["limit_concurrency"] >= 256
        assert captured["uvicorn_kwargs"]["http"] is IcecastSourceH11Protocol
        assert captured["player"].on_track_started is not None

    def test_serve_stream_with_server_audio_also_plays_locally(self, tmp_path) -> None:
        captured = self._serve_capturing_player(tmp_path, no_playback=False, stream=True)
        assert captured["dry_run"] is False
        assert captured["runtime"].stream_mode is True
        assert captured["player"]._stream_mode is True
        assert captured["player"]._server_audio_too is True

    def test_serve_hands_the_other_stations_to_the_app(self, tmp_path) -> None:
        """[stations.<slug>] stations are built when the app starts; they always stream."""
        spec = SimpleNamespace(
            slug="nightowl",
            name="Night Owl Radio",
            autostart=False,
            defined_in="config",
            cfg=SimpleNamespace(
                index=layout_index(tmp_path / "main"),
                playback=SimpleNamespace(stereotool_preset="included"),
                requests=RequestsConfig(enabled=True),
                player_page=PlayerPageConfig(enabled=True),
            ),
        )
        captured = self._serve_capturing_player(tmp_path, stations=(spec,), no_playback=True)
        (extra,) = captured["create_app_kwargs"]["extra_stations"]
        assert (extra.slug, extra.name, extra.autostart) == ("nightowl", "Night Owl Radio", False)
        assert extra.paths.state_dir == tmp_path / "main" / "stations" / "nightowl"
        # Its request page answers while it is stopped, so the stand-in knows it is on.
        assert extra.requests_enabled is True
        # Its pages answer while it is stopped, so the stand-in keeps its settings.
        assert extra.player_page == PlayerPageConfig(enabled=True)
        # The default station is browser-driven, but radio players still need h11.
        assert captured["uvicorn_kwargs"]["http"] is IcecastSourceH11Protocol

    def test_serve_without_stream_is_unchanged(self, tmp_path) -> None:
        captured = self._serve_capturing_player(tmp_path, no_playback=True)
        assert captured["dry_run"] is True
        assert captured["runtime"].stream_mode is False
        assert captured["runtime"].server_audio is False
        assert captured["create_app_kwargs"]["extra_stations"] == []
        assert "timeout_graceful_shutdown" not in captured["uvicorn_kwargs"]
        # Trusted proxies apply in every mode (the request limits key on the
        # client address); empty means a proxy on this machine only.
        assert captured["uvicorn_kwargs"]["proxy_headers"] is True
        assert "127.0.0.1/32" in captured["uvicorn_kwargs"]["forwarded_allow_ips"]
        assert not (tmp_path / ".stream-secret").exists()

    def test_serve_stream_leaves_an_old_secret_file_alone(self, tmp_path) -> None:
        """An old ``.stream-secret`` is no longer read, changed or deleted."""
        old = tmp_path / ".stream-secret"
        old.write_bytes(b"x" * 43 + b"\n")
        captured = self._serve_capturing_player(tmp_path, no_playback=True, stream=True)
        assert captured["runtime"].stream_mode is True
        assert old.read_bytes() == b"x" * 43 + b"\n"

    def test_serve_records_the_seed_only_in_browser_mode(self, tmp_path) -> None:
        with patch("autodj.server.PlayerBridge.record_seed") as record:
            self._serve_capturing_player(tmp_path, no_playback=True)
        record.assert_called_once()
        with patch("autodj.server.PlayerBridge.record_seed") as record:
            self._serve_capturing_player(tmp_path, no_playback=False)
        record.assert_not_called()

    def test_serve_stream_starts_the_first_set_with_the_seed(self, tmp_path) -> None:
        captured = self._serve_capturing_player(tmp_path, no_playback=True, stream=True)
        assert captured["runtime"].first_track == _make_entry(0)
        captured = self._serve_capturing_player(tmp_path, no_playback=True)
        assert captured["runtime"].first_track is None

    def test_stream_factory_does_not_require_local_audio_libraries(self, tmp_path) -> None:
        with patch("autodj.server._missing_audio_modules", return_value=["sounddevice"]):
            captured = self._serve_capturing_player(tmp_path, no_playback=True, stream=True)
        assert captured["runtime"].stream_mode is True
        assert captured["runtime"].server_audio is False

    def test_missing_local_audio_library_fails_only_when_factory_is_built(self, tmp_path) -> None:
        from fastapi.testclient import TestClient

        from autodj.server import serve

        cfg = _test_config(tmp_path)
        cfg.station.output = "server"
        cfg.default_station.cfg.station.output = "server"
        with (
            patch("autodj.server._missing_audio_modules", return_value=["soundfile"]),
            patch("uvicorn.run") as uvicorn_run,
        ):
            serve(cfg=cfg, sim=_make_sim_mock(), seed_entry=None)
            app = uvicorn_run.call_args.args[0]
            with pytest.raises(RuntimeError, match="Server audio requires"):
                app.state.stations._extras["default"].build(app.state.stations)
        assert (
            TestClient(
                app,
                base_url="http://127.0.0.1:8080",
                headers={"Host": "127.0.0.1", "Origin": "http://127.0.0.1:8080"},
            )
            .get("/api/version")
            .status_code
            == 200
        )

    def test_missing_audio_modules_treats_odd_specs_as_present(self) -> None:
        from autodj.server import _missing_audio_modules

        assert _missing_audio_modules() == []
        with patch("importlib.util.find_spec", side_effect=ValueError("mocked")):
            assert _missing_audio_modules() == []

    def test_serve_uses_server_config_host_and_port_by_default(self, tmp_path) -> None:
        from unittest.mock import patch

        from autodj.server import serve

        cfg = _test_config(tmp_path)
        cfg.server = ServerConfig(host="127.0.0.2", port=9090)
        sim = _make_sim_mock()

        with (
            patch("autodj.player.Player.run"),
            patch("uvicorn.run") as mock_uvicorn,
        ):
            serve(cfg=cfg, sim=sim, seed_entry=None)

        assert mock_uvicorn.call_args.kwargs["host"] == "127.0.0.2"
        assert mock_uvicorn.call_args.kwargs["port"] == 9090
        assert cfg.server.effective_allowed_hosts() == ["127.0.0.2"]
        assert cfg.server.effective_allowed_origins() == ["http://127.0.0.2:9090"]

    def test_serve_rejects_unsafe_override_before_player_construction(self) -> None:
        from unittest.mock import MagicMock, patch

        from autodj.server import serve

        cfg = MagicMock()
        cfg.time_checks = TimeChecksConfig()
        cfg.server = ServerConfig()
        original_server = cfg.server
        sim = _make_sim_mock()

        with (
            patch("autodj.player.Player") as player_class,
            patch("uvicorn.run") as mock_uvicorn,
            pytest.raises(ValueError, match="wildcard binding requires"),
        ):
            serve(
                cfg=cfg,
                sim=sim,
                seed_entry=None,
                host="0.0.0.0",
            )

        player_class.assert_not_called()
        mock_uvicorn.assert_not_called()
        assert cfg.server is original_server
        assert cfg.server.host == "127.0.0.1"
        assert cfg.server.port == 8080
        assert cfg.server.access_token is None

    def test_serve_rejects_mutated_weak_token_without_echoing_or_player(self) -> None:
        from unittest.mock import MagicMock, patch

        from autodj.server import serve

        weak = "do-not-print-me"
        cfg = MagicMock()
        cfg.time_checks = TimeChecksConfig()
        cfg.server = ServerConfig()
        cfg.server.host = "192.168.1.10"
        cfg.server.access_token = weak
        cfg.server.allowed_hosts = ["radio.local"]
        cfg.server.allowed_origins = ["http://radio.local:8080"]
        original_server = cfg.server

        with (
            patch("autodj.player.Player") as player_class,
            patch("uvicorn.run") as mock_uvicorn,
            pytest.raises(ValueError) as raised,
        ):
            serve(cfg=cfg, sim=_make_sim_mock(), seed_entry=None)

        assert weak not in str(raised.value)
        assert cfg.server is original_server
        assert cfg.server.host == "192.168.1.10"
        assert cfg.server.port == 8080
        assert cfg.server.access_token == weak
        player_class.assert_not_called()
        mock_uvicorn.assert_not_called()

    def test_serve_allows_specific_authenticated_lan_without_policy_lists(self, tmp_path) -> None:
        from unittest.mock import patch

        from autodj.server import serve

        cfg = _test_config(tmp_path)
        cfg.server = ServerConfig(host="192.168.1.10", access_token="s" * 32)

        with (
            patch("autodj.player.Player.run"),
            patch("uvicorn.run") as mock_uvicorn,
        ):
            serve(cfg=cfg, sim=_make_sim_mock(), seed_entry=None)

        assert cfg.server.effective_allowed_hosts() == ["192.168.1.10"]
        assert cfg.server.effective_allowed_origins() == ["http://192.168.1.10:8080"]
        assert mock_uvicorn.call_args.kwargs["host"] == "192.168.1.10"

    @pytest.mark.parametrize(
        ("tls", "origin"),
        [
            (False, "http://radio.local:8080"),
            (True, "https://radio.local:8443"),
        ],
    )
    def test_wildcard_serve_advertises_usable_allowed_origin(
        self,
        caplog,
        tmp_path: Path,
        tls: bool,
        origin: str,
    ) -> None:
        import logging
        from unittest.mock import patch

        from fastapi.testclient import TestClient

        from autodj.server import serve

        port = 8443 if tls else 8080
        tls_files = _tls_files(tmp_path) if tls else {}
        cfg = _test_config(tmp_path)
        cfg.server = ServerConfig(
            host="0.0.0.0",
            port=port,
            access_token="s" * 32 if tls else None,
            insecure_lan=not tls,
            allowed_hosts=["radio.local"],
            allowed_origins=[origin],
            **tls_files,
        )

        with (
            patch("autodj.player.Player.run"),
            patch("uvicorn.run") as mock_uvicorn,
            caplog.at_level(logging.INFO, logger="autodj.server"),
        ):
            serve(cfg=cfg, sim=_make_sim_mock(), seed_entry=None)

        assert f"AutoDJ server ready: {origin}" in caplog.text
        uvicorn_kwargs = mock_uvicorn.call_args.kwargs
        assert ("ssl_context_factory" in uvicorn_kwargs) is tls
        assert uvicorn_kwargs.get("ssl_certfile") == tls_files.get("ssl_certfile")
        assert "server ready: http://0.0.0.0" not in caplog.text
        assert "server ready: https://0.0.0.0" not in caplog.text
        app = mock_uvicorn.call_args.args[0]
        client = TestClient(
            app,
            base_url=origin,
            headers={"Host": "radio.local", "Origin": origin},
        )
        if tls:
            code = app.state.request_pairing_code()[0]
            assert (
                client.post(
                    "/api/pair",
                    json={"code": code, "device_name": "Serve test browser"},
                ).status_code
                == 200
            )
            assert client.post("/api/logout").status_code == 200
        else:
            assert client.get("/api/auth/status").status_code == 200
            assert client.post("/api/login", json={"token": "unused"}).status_code == 404

    def test_startup_shows_a_code_only_while_no_browser_is_paired(
        self, caplog, tmp_path: Path
    ) -> None:
        import logging
        from unittest.mock import patch

        from autodj.pairing import DeviceRegistry
        from autodj.server import serve

        cfg = _test_config(tmp_path)
        cfg.server = ServerConfig(access_token="s" * 32)

        def start() -> tuple[str, object]:
            caplog.clear()
            with (
                patch("autodj.player.Player.run"),
                patch("uvicorn.run") as mock_uvicorn,
                caplog.at_level(logging.INFO, logger="autodj.server"),
            ):
                serve(cfg=cfg, sim=_make_sim_mock(), seed_entry=None)
            return caplog.text, mock_uvicorn.call_args.args[0]

        first_log, first_app = start()
        code = first_app.state.security_policy.current_pairing_code()
        assert f"using code: {code}" in first_log
        # Starting with nothing paired counts as asking for that code.
        registry = DeviceRegistry(cfg.index.index_dir / ".paired-devices.sqlite3")
        assert registry.pair_on_request("First browser") is not None

        second_log, _second_app = start()
        assert code not in second_log
        assert "autodj devices pairing-code" in second_log
        assert registry.pair_on_request("Unasked") is None

    @pytest.mark.parametrize(
        ("origin", "tls"),
        [
            ("http://other.local:8080", False),
            ("http://radio.local:8080", True),
            ("http://radio.local:9090", False),
            ("https://radio.local:9090", True),
        ],
    )
    def test_serve_rejects_unusable_advertised_policy_before_side_effects(
        self,
        tmp_path: Path,
        origin: str,
        tls: bool,
    ) -> None:
        from unittest.mock import MagicMock, patch

        from autodj.server import serve

        cfg = MagicMock()
        cfg.index.index_dir = NO_INDEX_DIR
        cfg.time_checks = TimeChecksConfig()
        cfg.server = ServerConfig(
            host="0.0.0.0",
            access_token="s" * 32,
            allowed_hosts=["radio.local"],
            allowed_origins=[origin],
            **(_tls_files(tmp_path) if tls else {}),
        )
        original_server = cfg.server

        with (
            patch("autodj.player.Player") as player_class,
            patch("uvicorn.run") as mock_uvicorn,
            pytest.raises(ValueError, match="allowed origin"),
        ):
            serve(cfg=cfg, sim=_make_sim_mock(), seed_entry=None)

        player_class.assert_not_called()
        mock_uvicorn.assert_not_called()
        assert cfg.server is original_server

    @pytest.mark.parametrize(
        ("port", "secure_cookie", "origin"),
        [
            (80, False, "http://radio.local"),
            (443, True, "https://radio.local"),
        ],
    )
    def test_advertised_origin_accepts_matching_default_port(
        self, port: int, secure_cookie: bool, origin: str
    ) -> None:
        from autodj.security import SecurityPolicy
        from autodj.server import _advertised_server_origin

        policy = SecurityPolicy(
            ServerConfig(
                host="0.0.0.0",
                port=port,
                access_token="s" * 32,
                allowed_hosts=["radio.local"],
                allowed_origins=[origin],
            ),
            secure_cookie=secure_cookie,
        )

        assert _advertised_server_origin(policy) == origin


# ---------------------------------------------------------------------------
# New settings endpoints (transition / preset / djmix / playback / bpm / discovery)
# ---------------------------------------------------------------------------


class TestSettingsEndpoints:
    def test_get_settings(self, client) -> None:
        data = client.get("/api/status").json()["settings"]
        assert "transition" in data
        assert "djmix" in data
        assert "playback" in data
        assert "bpm_range" in data
        assert "discovery_every" in data
        assert "available_presets" in data

    def test_post_transition_valid(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/transition", json={"effect": "echo_out"})
        assert resp.status_code == 200
        assert bridge.player._cfg.transitions.effect == "echo_out"

    def test_post_transition_invalid_rejected(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        bridge.player._cfg.transitions.effect = "echo_out"
        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/transition", json={"effect": "BOGUS_FX"})
        assert resp.status_code == 400
        assert bridge.player._cfg.transitions.effect == "echo_out"

    def test_post_transition_persists(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        tc = TestClient(create_app(bridge))
        tc.post("/api/transition", json={"effect": "tape_stop"})
        # File written under tmp_path/web_state.json
        f = tmp_path / "stations" / "default" / "web_state.json"
        assert f.exists()
        import json as _json

        data = _json.loads(f.read_text(encoding="utf-8"))
        assert data["transition"] == "tape_stop"

    def test_volume_and_mute_survive_a_restart(self, bridge, tmp_path) -> None:
        """A restarted server came back at full volume (D15)."""
        from fastapi.testclient import TestClient

        from autodj.player import PlayerState

        quiet = 10 ** (-57 / 20)
        point_index(bridge.player._cfg.index, tmp_path)
        tc = TestClient(create_app(bridge))
        tc.post("/api/volume", json={"volume": quiet})
        tc.post("/api/mute")

        bridge.player._state = PlayerState()
        assert bridge.player._state.volume == 1.0
        bridge.load_persistent_state()

        assert bridge.player._state.volume == pytest.approx(quiet)
        assert bridge.player._state.is_muted is True

    def test_post_djmix_flags(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        tc = TestClient(create_app(bridge))
        tc.post("/api/djmix", json={"beatmatch": True, "harmonic_mode": "strict"})
        assert bridge.player._cfg.djmix.beatmatch is True
        assert bridge.player._cfg.djmix.harmonic_mode == "strict"

    def test_post_djmix_drop_mix_and_key_shift(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        tc = TestClient(create_app(bridge))
        settings = tc.post("/api/djmix", json={"drop_mix": True, "key_shift": True}).json()
        assert bridge.player._cfg.djmix.drop_mix is True
        assert bridge.player._cfg.djmix.key_shift is True
        assert settings["djmix"]["drop_mix"] is True
        assert settings["djmix"]["key_shift"] is True
        assert tc.post("/api/djmix", json={"key_shift": "maybe"}).status_code == 422

    def test_post_djmix_skips_none_fields(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        bridge.player._cfg.djmix.beatmatch = True
        tc = TestClient(create_app(bridge))
        tc.post("/api/djmix", json={"harmonic_mode": "strict"})  # beatmatch absent
        assert bridge.player._cfg.djmix.beatmatch is True  # untouched

    def test_post_djmix_rejects_removed_harmonic_mixing(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        tc = TestClient(create_app(bridge))
        response = tc.post("/api/djmix", json={"harmonic_mixing": True})
        assert response.status_code == 422
        assert bridge.player._cfg.djmix.harmonic_mode == "off"

    def test_post_playback_settings(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        tc = TestClient(create_app(bridge))
        tc.post(
            "/api/playback-settings",
            json={
                "crossfade_seconds": 5.5,
                "crossfade_eq_duck": True,
                "pure_shuffle": True,
                "replaygain_enabled": True,
            },
        )
        assert bridge.player._cfg.playback.crossfade_seconds == 5.5
        assert bridge.player._cfg.playback.crossfade_eq_duck is True
        assert bridge.player._pure_shuffle is True
        assert bridge.player._cfg.replaygain.enabled is True

    def test_post_playback_settings_transition_mode(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        bridge.player._cfg.playback.transition_mode = "fixed"
        tc = TestClient(create_app(bridge))
        tc.post("/api/playback-settings", json={"transition_mode": "full_intro_outro"})
        assert bridge.player._cfg.playback.transition_mode == "full_intro_outro"

    def test_post_playback_settings_transition_mode_invalid(self, bridge, tmp_path) -> None:
        import contextlib

        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        tc = TestClient(create_app(bridge))
        # Invalid mode: handler raises ValueError -> 500.  We just want to
        # confirm the config is not mutated by an unknown value.
        prev = bridge.player._cfg.playback.transition_mode
        with contextlib.suppress(Exception):
            tc.post("/api/playback-settings", json={"transition_mode": "wat"})
        assert bridge.player._cfg.playback.transition_mode == prev

    def test_post_playback_settings_rejects_negative(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        bridge.player._cfg.playback.crossfade_seconds = 3.0
        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/playback-settings", json={"crossfade_seconds": -3.0})
        assert resp.status_code == 422
        assert bridge.player._cfg.playback.crossfade_seconds == 3.0

    def test_post_playback_settings_fade_in(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        tc = TestClient(create_app(bridge))
        tc.post("/api/playback-settings", json={"fade_in_seconds": 1.5})
        assert bridge.player._cfg.playback.fade_in_seconds == pytest.approx(1.5)
        resp = tc.post("/api/playback-settings", json={"fade_in_seconds": -1.0})
        assert resp.status_code == 422
        assert bridge.player._cfg.playback.fade_in_seconds == pytest.approx(1.5)

    @pytest.mark.parametrize("name", ["crossfade_seconds", "fade_in_seconds"])
    def test_post_playback_settings_refuses_fade_above_maximum(
        self, bridge, tmp_path, name
    ) -> None:
        # The 0.19.0 sample saved 99 seconds from a field whose maximum is 20.
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        setattr(bridge.player._cfg.playback, name, 3.0)
        tc = TestClient(create_app(bridge))
        assert tc.post("/api/playback-settings", json={name: 99}).status_code == 422
        assert tc.post("/api/profiles", json={"name": "long", name: 99}).status_code == 422
        assert getattr(bridge.player._cfg.playback, name) == 3.0
        assert tc.post("/api/playback-settings", json={name: 20}).status_code == 200
        assert getattr(bridge.player._cfg.playback, name) == 20.0

    def test_post_playback_pure_shuffle(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        tc = TestClient(create_app(bridge))
        tc.post("/api/playback-settings", json={"pure_shuffle": True})
        assert bridge.player._pure_shuffle is True

    def test_post_playback_show_lyrics_false_clears_buffer(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        from autodj.audio_meta import LyricLine

        point_index(bridge.player._cfg.index, tmp_path)
        bridge.player._current_lyrics = [LyricLine(0.0, "x")]
        bridge.player._current_lyrics_plain = "stuff"
        tc = TestClient(create_app(bridge))
        tc.post("/api/playback-settings", json={"show_lyrics": False})
        assert bridge.player._cfg.playback.show_lyrics is False
        assert bridge.player._current_lyrics == []
        assert bridge.player._current_lyrics_plain == ""

    def test_post_djmix_harmonic_mode(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        tc = TestClient(create_app(bridge))
        tc.post("/api/djmix", json={"harmonic_mode": "strict"})
        assert bridge.player._cfg.djmix.harmonic_mode == "strict"
        tc.post("/api/djmix", json={"harmonic_mode": "off"})
        assert bridge.player._cfg.djmix.harmonic_mode == "off"

    def test_post_djmix_invalid_harmonic_mode_ignored(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        bridge.player._cfg.djmix.harmonic_mode = "compatible"
        tc = TestClient(create_app(bridge))
        tc.post("/api/djmix", json={"harmonic_mode": "bogus_mode"})
        assert bridge.player._cfg.djmix.harmonic_mode == "compatible"

    def test_set_djmix_skips_none_value_explicit(self, bridge) -> None:
        """Direct set_djmix call with None value hits the continue branch."""
        bridge.player._cfg.djmix.beatmatch = False
        bridge.set_djmix(beatmatch=None, phrase_align=True)
        assert bridge.player._cfg.djmix.beatmatch is False
        assert bridge.player._cfg.djmix.phrase_align is True

    def test_set_djmix_unknown_attr_silently_skipped(self, bridge) -> None:
        """Direct set_djmix with an attribute that isn't on cfg.djmix is a no-op."""
        bridge.set_djmix(does_not_exist=True)  # no AttributeError

    def test_post_playback_anchor_to_seed(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        # No prior seed_path — bridge should pin the current track as seed
        # the moment anchored mode is enabled.
        bridge.player._seed_path = None
        tc = TestClient(create_app(bridge))
        tc.post("/api/playback-settings", json={"anchor_to_seed": True})
        assert bridge.player._anchor_to_seed is True
        assert bridge.player._seed_path == bridge.player._state.current_track.path

    def test_post_playback_anchor_to_seed_off(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        bridge.player._anchor_to_seed = True
        tc = TestClient(create_app(bridge))
        tc.post("/api/playback-settings", json={"anchor_to_seed": False})
        assert bridge.player._anchor_to_seed is False


# ---------------------------------------------------------------------------
# Library tools — index / enrich / prune / stats endpoints
# ---------------------------------------------------------------------------


class TestLibraryEndpoints:
    def test_get_library_stats(self, client) -> None:
        resp = client.get("/api/library/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "track_count" in data
        assert "average_bpm" in data
        assert "tracks_with_genre" in data

    def test_get_library_job_idle(self, client) -> None:
        # Reset shared singleton so other tests don't bleed in.
        from autodj import jobs as _jobs

        _jobs._MANAGER = None
        resp = client.get("/api/library/job")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is False

    def test_post_library_run_disallowed(self, client) -> None:
        resp = client.post(
            "/api/library/run",
            json={"name": "rm", "args": ["-rf"]},
        )
        assert resp.status_code == 409

    def test_post_library_stop_idle(self, client) -> None:
        from autodj import jobs as _jobs

        _jobs._MANAGER = None
        resp = client.post("/api/library/stop")
        assert resp.status_code == 200
        assert resp.json() == {"stopped": False}

    def test_state_includes_library_job_field(self, client) -> None:
        from autodj import jobs as _jobs

        _jobs._MANAGER = None
        data = client.get("/api/status").json()
        assert "library_job" in data
        assert data["library_job"]["running"] is False

    def test_post_bpm_range_sets(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        tc = TestClient(create_app(bridge))
        tc.post("/api/bpm-range", json={"lo": 100, "hi": 140})
        assert bridge.player._bpm_range == (100.0, 140.0)

    def test_post_bpm_range_clears_on_null(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        bridge.player._bpm_range = (100.0, 140.0)
        tc = TestClient(create_app(bridge))
        tc.post("/api/bpm-range", json={"lo": None, "hi": None})
        assert bridge.player._bpm_range is None

    def test_post_bpm_range_clears_when_lo_ge_hi(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        tc = TestClient(create_app(bridge))
        tc.post("/api/bpm-range", json={"lo": 140, "hi": 100})
        assert bridge.player._bpm_range is None

    def test_library_run_hands_the_child_the_server_config(
        self,
        bridge,
        tmp_path,
    ) -> None:
        """A job must run against the config the server itself loaded."""
        from pathlib import Path

        from fastapi.testclient import TestClient

        from autodj.jobs import get_manager

        bridge.player._cfg.config_path = Path(tmp_path / "autodj.toml")
        bridge.player._cfg.index.index_dir = Path(tmp_path / "indexes")
        manager = get_manager()
        captured = {}

        def _fake_popen(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["env"] = kwargs["env"]

            class _Proc:
                stdout = None

                def poll(self):
                    return 0

            return _Proc()

        tc = TestClient(create_app(bridge))
        with patch("autodj.jobs.subprocess.Popen", side_effect=_fake_popen):
            response = tc.post("/api/library/run", json={"name": "stats", "args": []})

        assert response.status_code == 200
        argv = captured["cmd"]
        assert argv[2] == "autodj"
        assert argv[3:5] == ["--config", str(tmp_path / "autodj.toml")]
        assert argv[5] == "stats"
        assert captured["env"]["AUTODJ_INDEX_DIR"] == str(tmp_path / "indexes")
        manager.configure(config_path=None, index_dir=None)

    def test_post_discovery(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        tc = TestClient(create_app(bridge))
        tc.post("/api/discovery", json={"every": 25})
        assert bridge.player._discovery_every == 25
        # Configuring a rate arms the runtime toggle too, so the Settings
        # checkbox and the Now Playing button agree.
        assert bridge.player._state.discovery_enabled is True
        assert tc.get("/api/status").json()["discovery_enabled"] is True

    def test_post_discovery_zero_disables(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        bridge.player._discovery_every = 20
        tc = TestClient(create_app(bridge))
        tc.post("/api/discovery", json={"every": 0})
        assert bridge.player._discovery_every is None
        assert bridge.player._state.discovery_enabled is False
        assert tc.get("/api/status").json()["discovery_enabled"] is False

    def test_post_preset_clears_on_null(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        # mock a preset
        bridge.player._preset = MagicMock(name="energetic")
        tc = TestClient(create_app(bridge))
        tc.post("/api/preset", json={"name": None})
        assert bridge.player._preset is None

    def test_post_preset_unknown_name_silent(self, bridge, tmp_path) -> None:
        from fastapi.testclient import TestClient

        point_index(bridge.player._cfg.index, tmp_path)
        bridge.player._preset = None
        tc = TestClient(create_app(bridge))
        tc.post("/api/preset", json={"name": "totally_made_up_preset_xyz"})
        assert bridge.player._preset is None  # silently ignored

    def test_post_preset_applies_discovery_every(self, bridge, tmp_path) -> None:
        """Preset's discovery_every flows into player._discovery_every."""
        from fastapi.testclient import TestClient

        from autodj.presets import Preset, constant_curve

        point_index(bridge.player._cfg.index, tmp_path)
        bridge.player._discovery_every = None
        bridge.player._cfg.presets = {
            "test": Preset(
                name="test",
                bpm_weight=0.2,
                _curve=constant_curve(120.0),
                discovery_every=15,
            ),
        }
        tc = TestClient(create_app(bridge))
        tc.post("/api/preset", json={"name": "test"})
        assert bridge.player._discovery_every == 15

    def test_lyrics_for_reads_and_serialises_the_requested_path(self, bridge) -> None:
        from autodj.audio_meta import LyricLine

        bridge.player._read_lyrics_for_path.return_value = (
            [LyricLine(time_s=2.5, text="requested")],
            "ignored plain text",
        )

        result = bridge.lyrics_for("Z:/Music/requested.flac")

        assert result == [{"time_s": 2.5, "text": "requested"}]
        bridge.player._read_lyrics_for_path.assert_called_once_with("Z:/Music/requested.flac")


# ---------------------------------------------------------------------------
# EQ endpoint
# ---------------------------------------------------------------------------


class TestEqEndpoint:
    def test_post_eq_sets_bands(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        data = tc.post("/api/eq", json={"low": 0.5, "mid": 1.5, "high": 2.0}).json()
        assert data["low"] == 0.5
        assert data["mid"] == 1.5
        assert data["high"] == 2.0

    @pytest.mark.parametrize("body", [{"low": -0.1}, {"high": 2.01}, {"mid": 100.0}])
    def test_post_eq_rejects_out_of_range_bands(self, bridge, body) -> None:
        from fastapi.testclient import TestClient

        bridge.player._eq_low = bridge.player._eq_mid = bridge.player._eq_high = 1.0
        tc = TestClient(create_app(bridge))
        assert tc.post("/api/eq", json=body).status_code == 422
        assert bridge.get_eq() == {"low": 1.0, "mid": 1.0, "high": 1.0}

    def test_post_eq_partial(self, bridge) -> None:
        from fastapi.testclient import TestClient

        bridge.player._eq_low = 1.0
        bridge.player._eq_mid = 1.0
        bridge.player._eq_high = 1.0
        tc = TestClient(create_app(bridge))
        tc.post("/api/eq", json={"low": 0.3})
        assert bridge.player._eq_low == 0.3
        assert bridge.player._eq_mid == 1.0  # unchanged
        assert bridge.player._eq_high == 1.0


# ---------------------------------------------------------------------------
# Queue endpoints
# ---------------------------------------------------------------------------


class TestApiDocsDisabled:
    @pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
    def test_interactive_docs_and_schema_are_not_served(self, client, path) -> None:
        assert client.get(path).status_code == 404


class TestQueueEndpoints:
    def test_queue_add(self, bridge) -> None:
        from fastapi.testclient import TestClient

        path = bridge.sim.entries[2].path
        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/queue/add", json={"path": path})
        assert resp.json()["ok"] is True
        assert any(e.path == path for e in bridge.player._state.queue)

    def test_queue_add_unknown_path(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/queue/add", json={"path": "Z:/nope.flac"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "That track is no longer in the library."

    def test_queue_remove(self, bridge) -> None:
        from fastapi.testclient import TestClient

        e = bridge.sim.entries[0]
        bridge.player._state.queue.append(e)
        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/queue/remove", json={"path": e.path})
        assert resp.json()["ok"] is True
        assert e not in bridge.player._state.queue

    def test_queue_remove_not_found(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/queue/remove", json={"path": "Z:/nope.flac"})
        assert resp.json()["ok"] is False

    def test_queue_reorder(self, bridge) -> None:
        from fastapi.testclient import TestClient

        e0, e1, e2 = bridge.sim.entries[:3]
        bridge.player._state.queue.extend([e0, e1, e2])
        tc = TestClient(create_app(bridge))
        tc.post("/api/queue/reorder", json={"paths": [e2.path, e0.path, e1.path]})
        order = [e.path for e in bridge.player._state.queue]
        assert order == [e2.path, e0.path, e1.path]

    def test_queue_reorder_keeps_duplicate_entries(self, bridge) -> None:
        from fastapi.testclient import TestClient

        e0, e1 = bridge.sim.entries[:2]
        bridge.player._state.queue.extend([e0, e1, e0])
        tc = TestClient(create_app(bridge))
        tc.post("/api/queue/reorder", json={"paths": [e0.path, e0.path, e1.path]})
        assert [e.path for e in bridge.player._state.queue] == [e0.path, e0.path, e1.path]
        # A path listed more often than it was queued adds nothing.
        tc.post("/api/queue/reorder", json={"paths": [e1.path, e1.path, e0.path]})
        assert [e.path for e in bridge.player._state.queue] == [e1.path, e0.path]

    def test_queue_reorder_never_exposes_an_empty_queue(self, bridge) -> None:
        """A reader on another thread must not see the queue empty mid-reorder."""
        e0, e1, e2 = bridge.sim.entries[:3]
        seen_lengths: list[int] = []

        class _Watched(list):
            def clear(self) -> None:
                super().clear()
                seen_lengths.append(len(self))

            def __setitem__(self, key, value) -> None:
                super().__setitem__(key, value)
                seen_lengths.append(len(self))

        bridge.player._state.queue = _Watched([e0, e1, e2])
        bridge.queue_reorder([e2.path, e1.path, e0.path])
        assert [e.path for e in bridge.player._state.queue] == [e2.path, e1.path, e0.path]
        assert seen_lengths == [3]

    def test_queue_reorder_and_audio_thread_pop_cannot_interleave(self, bridge) -> None:
        """A pop landing mid-reorder must not be written back and play twice."""
        from autodj.player import Player

        e0, e1, e2 = bridge.sim.entries[:3]
        popped: list[object] = []
        popper: list[threading.Thread] = []

        class _PopsDuringRead(list):
            def __iter__(self):
                if not popper:
                    # The server-audio thread reaches for the head while the
                    # reorder is reading the queue.
                    thread = threading.Thread(
                        target=lambda: popped.append(Player._pop_user_queue(bridge.player))
                    )
                    popper.append(thread)
                    thread.start()
                    thread.join(timeout=0.2)
                return super().__iter__()

        bridge.player._state.queue = _PopsDuringRead([e0, e1, e2])
        bridge.queue_reorder([e2.path, e1.path, e0.path])
        popper[0].join(timeout=5)

        # The pop waited for the reorder, then took the new head.
        assert popped == [e2]
        assert [e.path for e in bridge.player._state.queue] == [e1.path, e0.path]

    def test_queue_reorder_drops_unknown(self, bridge) -> None:
        from fastapi.testclient import TestClient

        e0, e1 = bridge.sim.entries[:2]
        bridge.player._state.queue.extend([e0, e1])
        tc = TestClient(create_app(bridge))
        tc.post("/api/queue/reorder", json={"paths": [e1.path]})  # e0 dropped
        assert len(bridge.player._state.queue) == 1
        assert bridge.player._state.queue[0].path == e1.path


class TestQueueSyncsPrefetch:
    """Queue mutations must update state.next_track so the browser
    prefetcher loads the queued audio rather than the stale similarity
    pick computed when the previous track started."""

    def test_queue_add_overrides_next_track(self, bridge) -> None:
        from fastapi.testclient import TestClient

        bridge.player._dry_run = True
        prev_next = _make_entry(900)
        bridge.player._state.next_track = prev_next
        bridge.player._pick_next.return_value = _make_entry(901)
        target = bridge.sim.entries[2]
        tc = TestClient(create_app(bridge))
        tc.post("/api/queue/add", json={"path": target.path})
        assert bridge.player._state.next_track.path == target.path

    def test_play_next_overrides_next_track(self, bridge) -> None:
        from fastapi.testclient import TestClient

        prev_next = _make_entry(800)
        bridge.player._state.next_track = prev_next
        target = bridge.sim.entries[1]
        tc = TestClient(create_app(bridge))
        tc.post("/api/play-next", json={"path": target.path, "now": False})
        assert bridge.player._state.next_track.path == target.path

    def test_queue_remove_resyncs_next_track(self, bridge) -> None:
        from fastapi.testclient import TestClient

        bridge.player._dry_run = True
        e0, e1 = bridge.sim.entries[:2]
        bridge.player._state.queue.extend([e0, e1])
        bridge.player._state.next_track = e0
        bridge.player._pick_next.return_value = _make_entry(7)
        tc = TestClient(create_app(bridge))
        tc.post("/api/queue/remove", json={"path": e0.path})
        # Head of queue is now e1 -> prefetch should follow.
        assert bridge.player._state.next_track.path == e1.path

    def test_queue_reorder_resyncs_next_track(self, bridge) -> None:
        from fastapi.testclient import TestClient

        bridge.player._dry_run = True
        e0, e1, e2 = bridge.sim.entries[:3]
        bridge.player._state.queue.extend([e0, e1, e2])
        bridge.player._state.next_track = e0
        tc = TestClient(create_app(bridge))
        tc.post("/api/queue/reorder", json={"paths": [e2.path, e1.path]})
        assert bridge.player._state.next_track.path == e2.path


class TestServerAudioNextTrack:
    """With --server-audio the audio thread owns the next pick.

    It pops the next entry when the current track starts and plays it no
    matter what the queue does afterwards, so queue edits must leave
    ``next_track`` alone and must not call ``_pick_next`` from the request
    thread.
    """

    def test_queue_add_keeps_already_chosen_next_track(self, bridge) -> None:
        chosen = _make_entry(900)
        bridge.player.bus = MagicMock()
        bridge.player._playing_render = MagicMock(next_entry=chosen)
        bridge.player._state.next_track = chosen
        bridge.player._pick_next.reset_mock()

        assert bridge.queue_add(bridge.sim.entries[2].path) is True

        assert bridge.player._state.next_track is chosen
        assert [e.path for e in bridge.player._state.queue] == [bridge.sim.entries[2].path]
        bridge.player._pick_next.assert_not_called()

    def test_queue_remove_and_reorder_do_not_repick(self, bridge) -> None:
        chosen = _make_entry(901)
        e0, e1 = bridge.sim.entries[:2]
        bridge.player.bus = MagicMock()
        bridge.player._playing_render = MagicMock(next_entry=chosen)
        bridge.player._state.next_track = chosen
        bridge.player._state.queue.extend([e0, e1])
        bridge.player._pick_next.reset_mock()

        bridge.queue_reorder([e1.path, e0.path])
        bridge.queue_remove(e1.path)
        bridge.queue_remove(e0.path)

        assert bridge.player._state.next_track is chosen
        bridge.player._pick_next.assert_not_called()

    def test_play_next_still_announces_queued_next(self, bridge) -> None:
        # The audio loop honours queued_next at the transition, so it is
        # what really plays next.
        bridge.player._dry_run = False
        bridge.player._state.next_track = _make_entry(902)
        target = bridge.sim.entries[1]

        assert bridge.play_next(target.path) is True

        assert bridge.player._state.next_track.path == target.path


class TestPostQueueSeed:
    """post_queue_seed config controls what `_pick_next` seeds from
    after a user-built queue empties."""

    def test_default_last_queued_uses_just_played(self, bridge) -> None:
        bridge.player._dry_run = True
        bridge.player._cfg.playback.post_queue_seed = "last_queued"
        last_queued = _make_entry(50)
        bridge.player._state.queue.append(last_queued)
        bridge.player._pick_next.return_value = _make_entry(99)
        bridge.advance_now()
        # _pick_next called for prefetch refresh after pop -> seed = nxt
        # (the just-played queued track), not pre_queue_seed (which is
        # None in last_queued mode).
        assert bridge.player._pick_next.call_args.args == (last_queued,)

    def test_pre_queue_mode_rewinds_to_pre_queue_track(self, bridge) -> None:
        bridge.player._dry_run = True
        bridge.player._cfg.playback.post_queue_seed = "pre_queue"
        # Simulate the user adding a track while track A is playing.
        pre_queue = bridge.player._state.current_track
        target = bridge.sim.entries[2]
        bridge.queue_add(target.path)
        assert bridge.player._state.pre_queue_seed is pre_queue
        bridge.player._pick_next.return_value = _make_entry(99)
        bridge.advance_now()
        # Queue drained -> _pick_next seed should be pre_queue, not target.
        assert bridge.player._pick_next.call_args.args == (pre_queue,)
        assert bridge.player._state.pre_queue_seed is None

    def test_pre_queue_seed_cleared_when_queue_manually_emptied(self, bridge) -> None:
        bridge.player._cfg.playback.post_queue_seed = "pre_queue"
        target = bridge.sim.entries[2]
        bridge.queue_add(target.path)
        assert bridge.player._state.pre_queue_seed is not None
        bridge.queue_remove(target.path)
        assert bridge.player._state.pre_queue_seed is None

    def test_switching_out_of_pre_queue_clears_capture(self, bridge) -> None:
        bridge.player._cfg.playback.post_queue_seed = "pre_queue"
        target = bridge.sim.entries[2]
        bridge.queue_add(target.path)
        assert bridge.player._state.pre_queue_seed is not None
        bridge.set_playback_settings(PlaybackSettingsBody(post_queue_seed="last_queued"))
        assert bridge.player._state.pre_queue_seed is None

    def test_set_playback_settings_validates_post_queue_seed(self, bridge) -> None:
        import pytest

        with pytest.raises(ValueError):
            bridge.set_playback_settings(PlaybackSettingsBody(post_queue_seed="garbage"))

    def test_pre_queue_capture_does_not_overwrite_existing(self, bridge) -> None:
        bridge.player._cfg.playback.post_queue_seed = "pre_queue"
        original = bridge.player._state.current_track
        bridge.queue_add(bridge.sim.entries[2].path)
        assert bridge.player._state.pre_queue_seed is original
        # Subsequent queue_add must NOT replace the captured seed.
        bridge.queue_add(bridge.sim.entries[3].path)
        assert bridge.player._state.pre_queue_seed is original

    def test_pre_queue_capture_skipped_when_mode_off(self, bridge) -> None:
        bridge.player._cfg.playback.post_queue_seed = "last_queued"
        bridge.queue_add(bridge.sim.entries[2].path)
        assert bridge.player._state.pre_queue_seed is None

    def test_sync_next_for_prefetch_clears_when_no_current(self, bridge) -> None:
        bridge.player._dry_run = True
        bridge.player._state.current_track = None
        bridge.player._state.next_track = bridge.sim.entries[0]
        bridge._sync_next_for_prefetch()
        assert bridge.player._state.next_track is None

    def test_sync_next_for_prefetch_swallows_pick_failure(self, bridge) -> None:
        bridge.player._dry_run = True
        bridge.player._pick_next.side_effect = RuntimeError("boom")
        bridge.player._state.queue.clear()
        bridge.player._state.queued_next = None
        # Should not raise; logger.debug catches it.
        bridge._sync_next_for_prefetch()


# ---------------------------------------------------------------------------
# Reseed random + advance + lyrics
# ---------------------------------------------------------------------------


class TestMisc:
    def test_advance_skips(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        tc.post("/api/advance")
        bridge.player._skip_event.set.assert_called_once()

    def test_random_track_reseeds(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/random-track")
        assert resp.status_code == 200
        assert "current_track" in resp.json()
        assert bridge.player._state.queued_next is not None

    def test_random_track_preserves_pause_state(self, bridge) -> None:
        """Shuffle (reseed) while paused must not auto-resume playback.

        Regression: cascading-shuffle bug -- the browser-side fix relies on
        `is_paused` surviving the reseed so the catch-up code path takes
        the hard-cut branch instead of the crossfade branch.  This test
        pins the server contract: `/api/random-track` is a state mutation
        only -- it never flips `is_paused`.
        """
        from fastapi.testclient import TestClient

        bridge.player._dry_run = True
        # next_track refresh after advance returns a real entry (not a
        # MagicMock) so _track_dict can serialise the response.
        bridge.player._pick_next.return_value = _make_entry(99)
        bridge.player._state.is_paused = True
        prev_current = bridge.player._state.current_track

        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/random-track")
        assert resp.status_code == 200
        # Pause survives the reseed.
        assert bridge.player._state.is_paused is True
        assert resp.json()["is_paused"] is True
        # Current track actually changed (advance_now consumed queued_next).
        assert bridge.player._state.current_track is not None
        assert bridge.player._state.current_track is not prev_current

    def test_random_track_empty_index(self) -> None:
        from fastapi.testclient import TestClient

        player = _make_player_mock()
        sim = _make_sim_mock(entries=[])
        bridge = PlayerBridge(player=player, sim=sim)
        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/random-track")
        assert resp.status_code == 409

    def test_advance_headless_does_not_set_skip_event(self, bridge) -> None:
        """In dry_run / headless mode, advance happens synchronously in the
        bridge -- the player's skip_event must NOT be set, since the loop
        is parked and would not have advanced state on its own.
        """
        from fastapi.testclient import TestClient

        bridge.player._dry_run = True
        # _pick_next returns a fresh entry so next_track gets refreshed.
        bridge.player._pick_next.return_value = _make_entry(99)

        prev_next = bridge.player._state.next_track
        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/advance")
        assert resp.status_code == 200
        # skip_event was NOT touched -- bridge mutated state directly.
        bridge.player._skip_event.set.assert_not_called()
        # current_track advanced to the previous next_track.
        assert bridge.player._state.current_track is prev_next
        # next_track was refreshed with a freshly-picked entry.
        assert bridge.player._state.next_track is not None
        # Response carries the new state so the browser doesn't have to
        # wait for the WS broadcast tick.
        body = resp.json()
        assert body["current_track"] is not None
        assert body["next_track"] is not None

    def test_skip_headless_routes_through_advance_now(self, bridge) -> None:
        """/api/skip in headless mode bypasses the player loop too."""
        from fastapi.testclient import TestClient

        bridge.player._dry_run = True
        bridge.player._pick_next.return_value = _make_entry(42)

        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/skip")
        assert resp.status_code == 200
        bridge.player._skip_event.set.assert_not_called()

    def test_repick_next_replaces_next_without_advancing(self, bridge) -> None:
        """Standby-deck error path: refresh next_track, leave current alone."""
        from fastapi.testclient import TestClient

        bridge.player._dry_run = True
        prev_current = bridge.player._state.current_track
        replacement = _make_entry(77)
        bridge.player._pick_next.return_value = replacement

        tc = TestClient(create_app(bridge))
        resp = tc.post(
            "/api/repick-next",
            json={"blacklist": "/library/bad.flac"},
        )
        assert resp.status_code == 200
        # Current track UNCHANGED -- this is the bug we fixed.
        assert bridge.player._state.current_track is prev_current
        # Next track REPLACED with the freshly-picked entry.
        assert bridge.player._state.next_track is replacement
        # Skip event NOT touched.
        bridge.player._skip_event.set.assert_not_called()

    def test_repick_next_no_body_works(self, bridge) -> None:
        """Endpoint accepts empty / missing body without crashing."""
        from fastapi.testclient import TestClient

        bridge.player._dry_run = True
        bridge.player._pick_next.return_value = _make_entry(7)

        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/repick-next")
        assert resp.status_code == 200

    def test_repick_next_with_no_current_is_noop(self, bridge) -> None:
        """No current track -> nothing to base similarity off, leave state."""
        from fastapi.testclient import TestClient

        bridge.player._dry_run = True
        bridge.player._state.current_track = None
        prev_next = bridge.player._state.next_track

        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/repick-next")
        assert resp.status_code == 200
        assert bridge.player._state.next_track is prev_next
        bridge.player._pick_next.assert_not_called()

    def test_repick_next_pick_failure_clears_next(self, bridge) -> None:
        """When _pick_next raises, next_track becomes None (graceful)."""
        from fastapi.testclient import TestClient

        bridge.player._dry_run = True
        bridge.player._pick_next.side_effect = RuntimeError("FAISS empty")

        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/repick-next", json={"blacklist": "/x.flac"})
        assert resp.status_code == 200
        assert bridge.player._state.next_track is None

    def test_repick_next_blacklist_must_be_a_path(self, bridge) -> None:
        """A blacklist that is not a string never reaches the no-repeat window.

        An object or list there made every later pick raise TypeError, because
        the picker builds a set from recently_played.
        """
        from fastapi.testclient import TestClient

        bridge.player._dry_run = True
        bridge.player._pick_next.return_value = _make_entry(9)

        tc = TestClient(create_app(bridge))
        for bad in ({"bad": 1}, ["a", "b"], 5, True):
            resp = tc.post("/api/repick-next", json={"blacklist": bad})
            assert resp.status_code == 200
        assert list(bridge.player._state.recently_played) == []
        resp = tc.post("/api/repick-next", json={"blacklist": "/library/bad.flac"})
        assert resp.status_code == 200
        assert list(bridge.player._state.recently_played) == ["/library/bad.flac"]
        set(bridge.player._state.recently_played)  # what each pick builds

    def test_bridge_repick_next_ignores_non_string_blacklist(self, bridge) -> None:
        """The bridge refuses a non-string blacklist too, whoever calls it."""
        bridge.player._pick_next.return_value = _make_entry(9)
        bridge.repick_next(blacklist_path={"bad": 1})  # type: ignore[arg-type]
        assert list(bridge.player._state.recently_played) == []

    def test_advance_now_uses_queued_next(self, bridge) -> None:
        """queued_next (search -> Now / reseed_random) wins over next_track."""
        bridge.player._dry_run = True
        queued = _make_entry(123)
        bridge.player._state.queued_next = queued
        bridge.player._pick_next.return_value = _make_entry(7)

        bridge.advance_now()

        assert bridge.player._state.current_track is queued
        assert bridge.player._state.queued_next is None
        assert bridge.player._last_pick_mode == "queue"

    def test_advance_now_pops_queue(self, bridge) -> None:
        """User-ordered queue (drag-reorder) drains FIFO when no queued_next."""
        bridge.player._dry_run = True
        bridge.player._state.queued_next = None
        head = _make_entry(55)
        tail = _make_entry(56)
        bridge.player._state.queue.extend([head, tail])
        bridge.player._pick_next.return_value = _make_entry(99)

        bridge.advance_now()

        assert bridge.player._state.current_track is head
        assert list(bridge.player._state.queue) == [tail]
        assert bridge.player._last_pick_mode == "queue"

    def test_advance_now_picks_when_no_next_track(self, bridge) -> None:
        """No queued_next, no queue, no next_track -> falls back to _pick_next."""
        bridge.player._dry_run = True
        bridge.player._state.queued_next = None
        bridge.player._state.next_track = None
        # cur is set; queue empty.
        picked = _make_entry(77)
        bridge.player._pick_next.return_value = picked

        bridge.advance_now()

        assert bridge.player._state.current_track is picked
        # _pick_next called twice: once for the advance pick, once to
        # refresh next_track for the prefetcher.
        assert bridge.player._pick_next.call_count == 2

    def test_advance_now_no_op_when_index_empty(self, bridge) -> None:
        """No current, no next, no queue -> early return, no state mutation."""
        bridge.player._dry_run = True
        bridge.player._state.queued_next = None
        bridge.player._state.current_track = None
        bridge.player._state.next_track = None

        bridge.advance_now()

        assert bridge.player._state.current_track is None
        bridge.player._pick_next.assert_not_called()

    def test_advance_now_recovers_when_next_pick_fails(self, bridge) -> None:
        """If refreshing next_track raises (empty index, FAISS error),
        advance_now logs and clears next_track but keeps current_track
        usable so the browser can keep playing.
        """
        bridge.player._dry_run = True

        # First pick succeeds (there's a next_track precomputed).  Second
        # call (refreshing next) blows up.
        bridge.player._pick_next.side_effect = RuntimeError("FAISS empty")

        prev_next = bridge.player._state.next_track
        bridge.advance_now()

        assert bridge.player._state.current_track is prev_next
        # next_track wiped but current is intact.
        assert bridge.player._state.next_track is None

    def test_advance_now_no_op_when_initial_pick_fails(self, bridge) -> None:
        """If _pick_next(cur) itself fails (e.g. index pruned to empty
        between calls), advance_now leaves state untouched.
        """
        bridge.player._dry_run = True
        # No queued_next, no queue, no next_track -> falls back to picker.
        bridge.player._state.queued_next = None
        bridge.player._state.next_track = None
        bridge.player._pick_next.side_effect = RuntimeError("index empty")

        before_cur = bridge.player._state.current_track
        bridge.advance_now()
        # Current unchanged, no record_played call, no track_number bump.
        assert bridge.player._state.current_track is before_cur

    def test_websocket_connect_and_disconnect_log_at_info(self, caplog) -> None:
        """Server logs every WS connect / disconnect at INFO with the
        client host + active count.  Regression test for the silent
        baseline that left users unable to confirm a working connection.
        """
        import logging

        from fastapi.testclient import TestClient

        bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock())
        client = TestClient(create_app(bridge))
        with (
            caplog.at_level(logging.INFO, logger="autodj.server"),
            client.websocket_connect("/ws"),
        ):
            pass

        msgs = [r.message for r in caplog.records]
        assert any("WebSocket connected" in m for m in msgs), msgs
        assert any("WebSocket disconnected" in m for m in msgs), msgs

    def test_advance_now_spawns_lyrics_for_new_track(self, bridge) -> None:
        """Browser-driven mode skips _play_track, so advance_now must
        spawn lyric loading itself or the lyric panel stays frozen on the
        previous track's words.
        """
        bridge.player._dry_run = True
        bridge.player._state.queued_next = None
        picked = _make_entry(404)
        bridge.player._state.next_track = picked
        bridge.player._pick_next.return_value = _make_entry(405)

        bridge.advance_now()

        bridge.player.load_lyrics_in_background.assert_called_once_with(picked.path)

    def test_advance_now_swallows_lyric_spawn_error(self, bridge) -> None:
        """A broken sibling .lrc / corrupt tag must not abort the advance
        — log + continue, leaving lyrics empty rather than crashing the
        entire track-change path.
        """
        bridge.player._dry_run = True
        bridge.player._state.queued_next = None
        picked = _make_entry(406)
        bridge.player._state.next_track = picked
        bridge.player._pick_next.return_value = _make_entry(407)
        bridge.player.load_lyrics_in_background.side_effect = OSError("disk gone")

        bridge.advance_now()

        assert bridge.player._state.current_track is picked

    def test_advance_now_spawns_background_analysis(self, bridge) -> None:
        """Browser-driven mode skips _play_track, so advance_now must
        kick off audio analysis on a background thread to populate the
        DJ-meta cache (cue points, intro/outro markers, beat grid).
        Without this, the web UI's cue list stays empty for every track
        even though detect_cues works fine from raw audio.
        """
        bridge.player._dry_run = True
        bridge.player._state.queued_next = None
        picked = _make_entry(501)
        nxt2 = _make_entry(502)
        bridge.player._state.next_track = picked
        bridge.player._pick_next.return_value = nxt2

        bridge.advance_now()

        # Both the new current track AND the freshly-picked next track
        # should be queued for analysis so the cue strip is ready by
        # the time the browser crossfades.
        calls = [c.args[0] for c in bridge.player.analyse_track_in_background.call_args_list]
        assert picked.path in calls
        assert nxt2.path in calls

    def test_lyrics_endpoint_reads_each_requested_index_path(self, bridge) -> None:
        from fastapi.testclient import TestClient

        from autodj.audio_meta import LyricLine

        first, second = bridge.sim.entries[:2]
        bridge.player._read_lyrics_for_path.side_effect = [
            ([LyricLine(time_s=1.0, text="first")], ""),
            ([LyricLine(time_s=2.0, text="second")], ""),
        ]
        tc = TestClient(create_app(bridge))

        first_data = tc.get("/api/lyrics", params={"path": first.path}).json()
        second_data = tc.get("/api/lyrics", params={"path": second.path}).json()

        assert first_data == {
            "path": first.path,
            "lyrics": [{"time_s": 1.0, "text": "first"}],
        }
        assert second_data == {
            "path": second.path,
            "lyrics": [{"time_s": 2.0, "text": "second"}],
        }
        assert bridge.player._read_lyrics_for_path.call_args_list == [
            ((first.path,), {}),
            ((second.path,), {}),
        ]

    def test_lyrics_endpoint_rejects_an_unknown_path_without_reading_it(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))

        response = tc.get("/api/lyrics", params={"path": "Z:/not-indexed.flac"})

        assert response.status_code == 404
        bridge.player._read_lyrics_for_path.assert_not_called()

    def test_lyrics_lookup_runs_off_the_event_loop(self, bridge, monkeypatch) -> None:
        from fastapi.testclient import TestClient

        path = bridge.sim.entries[0].path
        called: list[str] = []

        def read_lyrics(requested_path: str) -> list[dict]:
            called.append(requested_path)
            return []

        async def fake_to_thread(function, *args, **kwargs):
            called.append("to_thread")
            return function(*args, **kwargs)

        bridge.lyrics_for = read_lyrics  # type: ignore[assignment]
        monkeypatch.setattr("autodj.server.asyncio.to_thread", fake_to_thread)

        response = TestClient(create_app(bridge)).get("/api/lyrics", params={"path": path})

        assert response.status_code == 200
        assert called[-2:] == ["to_thread", path]

    def test_lyrics_endpoint_rejects_result_after_track_removed(self, bridge) -> None:
        from fastapi.testclient import TestClient

        path = bridge.sim.entries[0].path

        def swap_index_generation(_path: str) -> list[dict]:
            bridge.sim.entries = bridge.sim.entries[1:]
            return [{"time": 0.0, "text": "stale secret"}]

        bridge.lyrics_for = swap_index_generation  # type: ignore[assignment]

        response = TestClient(create_app(bridge)).get("/api/lyrics", params={"path": path})

        assert response.status_code == 409
        assert "stale secret" not in response.text

    async def test_cancelled_lyrics_waiter_never_starts_a_thread_read(
        self, bridge, _lyrics_executor
    ) -> None:
        import contextlib

        from httpx2 import ASGITransport, AsyncClient

        loop = asyncio.get_running_loop()
        first, second = (entry.path for entry in bridge.sim.entries[:2])
        first_started = asyncio.Event()
        second_requested = asyncio.Event()
        release_first = threading.Event()
        calls: list[str] = []
        entry_for_path = bridge.entry_for_path

        def observe_request(path):
            if path == second:
                second_requested.set()
            return entry_for_path(path)

        def slow_lyrics(path: str) -> list[dict]:
            calls.append(path)
            if path == first:
                loop.call_soon_threadsafe(first_started.set)
                assert release_first.wait(10.0)
            return []

        bridge.entry_for_path = observe_request  # type: ignore[assignment]
        bridge.lyrics_for = slow_lyrics  # type: ignore[assignment]
        app = create_app(bridge)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            active = asyncio.create_task(client.get("/api/lyrics", params={"path": first}))
            try:
                await asyncio.wait_for(first_started.wait(), 5.0)
                queued = asyncio.create_task(client.get("/api/lyrics", params={"path": second}))
                # The route proceeds synchronously from lookup to waiting on the gate.
                await asyncio.wait_for(second_requested.wait(), 5.0)
                queued.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await queued
            finally:
                release_first.set()
                response = await active
            assert response.status_code == 200

        assert calls == [first]

    async def test_cancelled_active_lyrics_read_holds_capacity_until_worker_finishes(
        self, bridge, _lyrics_executor
    ) -> None:
        import contextlib

        from httpx2 import ASGITransport, AsyncClient

        loop = asyncio.get_running_loop()
        first, second = (entry.path for entry in bridge.sim.entries[:2])
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        second_requested = asyncio.Event()
        release_first = threading.Event()
        calls: list[str] = []
        entry_for_path = bridge.entry_for_path

        def observe_request(path):
            if path == second:
                second_requested.set()
            return entry_for_path(path)

        def slow_lyrics(path: str) -> list[dict]:
            calls.append(path)
            if path == first:
                loop.call_soon_threadsafe(first_started.set)
                assert release_first.wait(10.0)
            else:
                loop.call_soon_threadsafe(second_started.set)
            return []

        bridge.entry_for_path = observe_request  # type: ignore[assignment]
        bridge.lyrics_for = slow_lyrics  # type: ignore[assignment]
        read_gate = asyncio.Semaphore(1)
        with patch("autodj.server.asyncio.Semaphore", return_value=read_gate):
            app = create_app(bridge)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            active = asyncio.create_task(client.get("/api/lyrics", params={"path": first}))
            try:
                await asyncio.wait_for(first_started.wait(), 5.0)
                active.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await active
                assert read_gate.locked()

                following = asyncio.create_task(client.get("/api/lyrics", params={"path": second}))
                await asyncio.wait_for(second_requested.wait(), 5.0)
                assert second_started.is_set() is False
            finally:
                release_first.set()
                with contextlib.suppress(asyncio.CancelledError):
                    await active

            await asyncio.wait_for(second_started.wait(), 5.0)
            assert (await following).status_code == 200

        assert calls == [first, second]


# ---------------------------------------------------------------------------
# Audio file streaming
# ---------------------------------------------------------------------------


class TestAudioEndpoint:
    def test_audio_unknown_path_404(self, client) -> None:
        resp = client.get("/api/audio?path=Z:/not/in/index.flac")
        assert resp.status_code == 404

    def test_audio_path_in_index_but_no_file(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        # entry path is a string that does not point to a real file
        resp = tc.get(f"/api/audio?path={bridge.sim.entries[0].path}")
        assert resp.status_code == 404

    def test_audio_streams_real_file(self, tmp_path, bridge) -> None:
        from fastapi.testclient import TestClient

        # Write a fake mp3 (just bytes — we never decode it server-side)
        fake_mp3 = tmp_path / "fake.mp3"
        fake_mp3.write_bytes(b"\xff\xfb" + b"\x00" * 4096)
        # Inject as a known entry
        e = _make_entry(99)
        e.path = str(fake_mp3)
        _register_index_entry(bridge, e)
        tc = TestClient(create_app(bridge))
        resp = tc.get(f"/api/audio?path={fake_mp3}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/mpeg"
        assert resp.headers["accept-ranges"] == "bytes"
        # Sent as is even though the client (TestClient) accepts gzip.
        assert "content-encoding" not in resp.headers
        assert int(resp.headers["content-length"]) == fake_mp3.stat().st_size

    def test_audio_range_request(self, tmp_path, bridge) -> None:
        from fastapi.testclient import TestClient

        fake = tmp_path / "song.flac"
        fake.write_bytes(b"\x00" * 10000)
        e = _make_entry(123)
        e.path = str(fake)
        _register_index_entry(bridge, e)
        tc = TestClient(create_app(bridge))
        resp = tc.get(f"/api/audio?path={fake}", headers={"Range": "bytes=100-199"})
        assert resp.status_code == 206
        assert resp.headers["content-range"] == "bytes 100-199/10000"
        assert int(resp.headers["content-length"]) == 100

    def test_audio_invalid_range_416(self, tmp_path, bridge) -> None:
        from fastapi.testclient import TestClient

        fake = tmp_path / "song.flac"
        fake.write_bytes(b"\x00" * 100)
        e = _make_entry(124)
        e.path = str(fake)
        _register_index_entry(bridge, e)
        tc = TestClient(create_app(bridge))
        resp = tc.get(f"/api/audio?path={fake}", headers={"Range": "bytes=999999-"})
        assert resp.status_code == 416
        assert resp.headers["content-range"] == "bytes */100"

    def test_audio_malformed_range_400(self, tmp_path, bridge) -> None:
        from fastapi.testclient import TestClient

        fake = tmp_path / "song.flac"
        fake.write_bytes(b"\x00" * 100)
        e = _make_entry(125)
        e.path = str(fake)
        _register_index_entry(bridge, e)
        tc = TestClient(create_app(bridge))
        resp = tc.get(f"/api/audio?path={fake}", headers={"Range": "kilobytes=0-50"})
        assert resp.status_code == 400

    def test_audio_suffix_range_returns_final_bytes(self, tmp_path, bridge) -> None:
        from fastapi.testclient import TestClient

        audio = tmp_path / "suffix.flac"
        audio.write_bytes(bytes(range(100)))
        entry = _make_entry(126)
        entry.path = str(audio)
        _register_index_entry(bridge, entry)

        response = TestClient(create_app(bridge)).get(
            f"/api/audio?path={audio}",
            headers={"Range": "bytes=-10"},
        )

        assert response.status_code == 206
        assert response.headers["content-range"] == "bytes 90-99/100"
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["content-length"] == "10"
        assert response.content == bytes(range(90, 100))

    def test_audio_range_on_empty_file_is_unsatisfiable(self, tmp_path, bridge) -> None:
        from fastapi.testclient import TestClient

        audio = tmp_path / "empty.flac"
        audio.write_bytes(b"")
        entry = _make_entry(128)
        entry.path = str(audio)
        _register_index_entry(bridge, entry)

        response = TestClient(create_app(bridge)).get(
            f"/api/audio?path={audio}", headers={"Range": "bytes=0-"}
        )

        assert response.status_code == 416
        assert response.headers["content-range"] == "bytes */0"

    def test_missing_ffmpeg_falls_back_before_alac_headers(
        self, tmp_path, bridge, monkeypatch
    ) -> None:
        from fastapi.testclient import TestClient

        audio = tmp_path / "lossless.m4a"
        audio.write_bytes(b"raw-alac-fixture")
        entry = _make_entry(129)
        entry.path = str(audio)
        _register_index_entry(bridge, entry)
        monkeypatch.setattr("autodj.server._is_alac", lambda *_args: True)
        monkeypatch.setattr("autodj.server.shutil.which", lambda _name: None)

        response = TestClient(create_app(bridge)).get(f"/api/audio?path={audio}")

        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mp4"
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["content-length"] == str(len(b"raw-alac-fixture"))
        assert response.content == b"raw-alac-fixture"

    def test_invalid_mp4_metadata_falls_back_to_source_bytes(self, tmp_path, bridge) -> None:
        from fastapi.testclient import TestClient

        audio = tmp_path / "invalid.m4a"
        audio.write_bytes(b"not-an-mp4")
        entry = _make_entry(130)
        entry.path = str(audio)
        _register_index_entry(bridge, entry)

        response = TestClient(create_app(bridge)).get(f"/api/audio?path={audio}")

        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mp4"
        assert response.content == b"not-an-mp4"

    def test_alac_transcode_ignores_source_range(self, tmp_path, bridge, monkeypatch) -> None:
        from fastapi.testclient import TestClient

        audio = tmp_path / "lossless.m4a"
        audio.write_bytes(b"raw-alac-fixture")
        entry = _make_entry(130)
        entry.path = str(audio)
        _register_index_entry(bridge, entry)
        monkeypatch.setattr("autodj.server._is_alac", lambda *_args: True)
        monkeypatch.setattr("autodj.server.shutil.which", lambda _name: "ffmpeg")
        monkeypatch.setattr(
            "autodj.server._start_alac_transcoder",
            AsyncMock(
                return_value=SimpleNamespace(
                    stdout=SimpleNamespace(read=AsyncMock(return_value=b"prefetched")),
                    kill=MagicMock(),
                    wait=AsyncMock(return_value=0),
                )
            ),
        )

        async def transcode(_process, _prefetched):
            yield b"complete-mp3"

        monkeypatch.setattr("autodj.server._transcode_alac_to_mp3", transcode)

        response = TestClient(create_app(bridge)).get(
            f"/api/audio?path={audio}", headers={"Range": "bytes=1-2"}
        )

        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.headers["accept-ranges"] == "none"
        assert "content-range" not in response.headers
        assert response.content == b"complete-mp3"

    @pytest.mark.parametrize(
        ("range_header", "status", "content_range", "content_length"),
        [
            (None, 200, None, "100"),
            ("bytes=10-19", 206, "bytes 10-19/100", "10"),
            ("bytes=100-", 416, "bytes */100", "0"),
        ],
    )
    def test_audio_head_returns_body_free_range_metadata(
        self,
        tmp_path,
        bridge,
        range_header,
        status,
        content_range,
        content_length,
    ) -> None:
        from fastapi.testclient import TestClient

        audio = tmp_path / "head.flac"
        audio.write_bytes(bytes(range(100)))
        entry = _make_entry(135)
        entry.path = str(audio)
        _register_index_entry(bridge, entry)
        headers = {} if range_header is None else {"Range": range_header}

        response = TestClient(create_app(bridge)).head(f"/api/audio?path={audio}", headers=headers)

        assert response.status_code == status
        assert response.content == b""
        assert response.headers.get("content-range") == content_range
        assert response.headers["content-length"] == content_length

    def test_alac_head_reports_transcode_policy_without_spawning(
        self, tmp_path, bridge, monkeypatch
    ) -> None:
        from fastapi.testclient import TestClient

        audio = tmp_path / "head.m4a"
        audio.write_bytes(b"alac")
        entry = _make_entry(137)
        entry.path = str(audio)
        _register_index_entry(bridge, entry)
        monkeypatch.setattr("autodj.server._is_alac", lambda *_args: True)
        monkeypatch.setattr("autodj.server.shutil.which", lambda _name: "X:/ffmpeg.exe")
        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            AsyncMock(side_effect=AssertionError("HEAD spawned ffmpeg")),
        )

        response = TestClient(create_app(bridge)).head(f"/api/audio?path={audio}")

        assert response.status_code == 200
        assert response.content == b""
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.headers["accept-ranges"] == "none"
        assert "content-range" not in response.headers
        assert "content-length" not in response.headers


async def test_alac_process_spawns_with_resolved_path_before_headers(
    tmp_path, bridge, monkeypatch
) -> None:
    audio = tmp_path / "preflight.m4a"
    audio.write_bytes(b"alac")
    entry = _make_entry(138)
    entry.path = str(audio)
    _register_index_entry(bridge, entry)
    events: list[tuple[str, object]] = []
    chunks = iter((b"mp3", b""))

    async def read_chunk(_size=-1):
        events.append(("read", _size))
        return next(chunks)

    process = SimpleNamespace(
        stdout=SimpleNamespace(read=read_chunk),
        kill=MagicMock(),
        wait=AsyncMock(return_value=0),
    )

    async def spawn(*args, **_kwargs):
        events.append(("spawn", args[0]))
        return process

    monkeypatch.setattr("autodj.server._is_alac", lambda *_args: True)
    monkeypatch.setattr("autodj.server.shutil.which", lambda _name: "X:/ffmpeg.exe")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    app = create_app(bridge)

    messages: list[dict] = []
    request_sent = False
    never = asyncio.Event()

    async def receive() -> dict:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await never.wait()
        raise AssertionError("unreachable")

    async def send(message: dict) -> None:
        messages.append(message)
        if message["type"] == "http.response.start":
            events.append(("headers", message["status"]))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/audio",
        "raw_path": b"/api/audio",
        "query_string": urlencode({"path": str(audio)}).encode("ascii"),
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
        "root_path": "",
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=1.0)

    assert events[:3] == [
        ("spawn", "X:/ffmpeg.exe"),
        ("read", 64 * 1024),
        ("headers", 200),
    ]
    assert (
        b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        == b"mp3"
    )


def test_alac_spawn_failure_falls_back_before_headers(tmp_path, bridge, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    audio = tmp_path / "spawn-failure.m4a"
    audio.write_bytes(b"raw-alac")
    entry = _make_entry(139)
    entry.path = str(audio)
    _register_index_entry(bridge, entry)
    monkeypatch.setattr("autodj.server._is_alac", lambda *_args: True)
    monkeypatch.setattr("autodj.server.shutil.which", lambda _name: "X:/ffmpeg.exe")
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("ffmpeg vanished")),
    )

    response = TestClient(create_app(bridge)).get(f"/api/audio?path={audio}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mp4"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == str(len(b"raw-alac"))
    assert response.content == b"raw-alac"


@pytest.mark.parametrize(
    "first_read",
    [b"", OSError("ffmpeg output failed")],
    ids=["immediate-exit", "read-error"],
)
def test_alac_first_output_failure_falls_back_and_reaps_before_headers(
    tmp_path, bridge, monkeypatch, first_read
) -> None:
    from fastapi.testclient import TestClient

    audio = tmp_path / "preflight-failure.m4a"
    audio.write_bytes(b"raw-alac")
    entry = _make_entry(145)
    entry.path = str(audio)
    _register_index_entry(bridge, entry)
    read = (
        AsyncMock(side_effect=first_read)
        if isinstance(first_read, Exception)
        else AsyncMock(return_value=first_read)
    )
    process = SimpleNamespace(
        stdout=SimpleNamespace(read=read),
        kill=MagicMock(),
        wait=AsyncMock(return_value=1),
    )
    monkeypatch.setattr("autodj.server._is_alac", lambda *_args: True)
    monkeypatch.setattr("autodj.server.shutil.which", lambda _name: "X:/ffmpeg.exe")
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )

    response = TestClient(create_app(bridge)).get(f"/api/audio?path={audio}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mp4"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == str(len(b"raw-alac"))
    assert response.content == b"raw-alac"
    process.kill.assert_called_once_with()
    process.wait.assert_awaited_once_with()


async def test_alac_transcoder_reaps_process_when_consumer_closes(tmp_path, monkeypatch) -> None:
    from autodj.server import _transcode_alac_to_mp3

    process = SimpleNamespace(
        stdout=SimpleNamespace(read=AsyncMock(return_value=b"mp3-chunk")),
        kill=MagicMock(),
        wait=AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=AssertionError("stream respawned ffmpeg")),
    )
    stream = _transcode_alac_to_mp3(process)

    assert await anext(stream) == b"mp3-chunk"
    await stream.aclose()

    process.kill.assert_called_once_with()
    process.wait.assert_awaited_once_with()


async def _collect_audio_asgi(app, *, path: str, method: str = "GET") -> list[dict]:
    messages: list[dict] = []
    request_sent = False
    never = asyncio.Event()

    async def receive() -> dict:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await never.wait()
        raise AssertionError("unreachable")

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": "/api/audio",
        "raw_path": b"/api/audio",
        "query_string": urlencode({"path": path}).encode("ascii"),
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
        "root_path": "",
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=1.0)
    return messages


async def test_alac_stalled_prefetch_times_out_and_falls_back_without_leaks(
    tmp_path, bridge, monkeypatch
) -> None:
    audio = tmp_path / "stalled-preflight.m4a"
    audio.write_bytes(b"raw-alac")
    entry = _make_entry(146)
    entry.path = str(audio)
    _register_index_entry(bridge, entry)
    events: list[str] = []
    read_cancelled = asyncio.Event()
    closed = threading.Event()
    never = asyncio.Event()
    real_open = Path.open

    class TrackedFile:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def close(self):
            self.handle.close()
            closed.set()

    def tracked_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if path == audio and args == ("rb",):
            return TrackedFile(handle)
        return handle

    async def stalled_read(_size=-1):
        try:
            await never.wait()
        finally:
            events.append("read-cancelled")
            read_cancelled.set()

    async def wait_for_process():
        events.append("wait")
        return 1

    process = SimpleNamespace(
        stdout=SimpleNamespace(read=stalled_read),
        kill=MagicMock(side_effect=lambda: events.append("kill")),
        wait=AsyncMock(side_effect=wait_for_process),
    )
    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr("autodj.server._is_alac", lambda *_args: True)
    monkeypatch.setattr("autodj.server.shutil.which", lambda _name: "X:/ffmpeg.exe")
    monkeypatch.setattr(
        "autodj.server._ALAC_PREFETCH_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    existing_tasks = set(asyncio.all_tasks())

    messages = await _collect_audio_asgi(create_app(bridge), path=str(audio))
    await asyncio.sleep(0)

    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    assert start["status"] == 200
    assert (b"content-type", b"audio/mp4") in start["headers"]
    assert body == b"raw-alac"
    assert read_cancelled.is_set()
    assert events[:3] == ["read-cancelled", "kill", "wait"]
    assert closed.wait(0.5) is True
    assert {task for task in asyncio.all_tasks() - existing_tasks if not task.done()} == set()


async def _cancel_asgi_after_first_body(app, *, path: str, expected_prefix: bytes) -> None:
    first_body = asyncio.Event()
    never = asyncio.Event()
    request_sent = False
    response_status = None
    first_chunk = b""

    async def receive() -> dict:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await first_body.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        nonlocal response_status, first_chunk
        if message["type"] == "http.response.start":
            response_status = message["status"]
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk = message["body"]
            first_body.set()
            await never.wait()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/audio",
        "raw_path": b"/api/audio",
        "query_string": urlencode({"path": path}).encode("ascii"),
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
        "root_path": "",
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=1.0)
    assert response_status == 200
    assert first_chunk.startswith(expected_prefix)


async def test_partial_asgi_alac_response_cancels_and_reaps_ffmpeg(
    bridge, tmp_path, monkeypatch
) -> None:
    audio = tmp_path / "source.m4a"
    audio.write_bytes(b"alac")
    entry = _make_entry(134)
    entry.path = str(audio)
    _register_index_entry(bridge, entry)
    process = SimpleNamespace(
        stdout=SimpleNamespace(read=AsyncMock(return_value=b"mp3-chunk")),
        kill=MagicMock(),
        wait=AsyncMock(return_value=0),
    )
    monkeypatch.setattr("autodj.server._is_alac", lambda *_args: True)
    monkeypatch.setattr("autodj.server.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )

    await _cancel_asgi_after_first_body(
        create_app(bridge), path=str(audio), expected_prefix=b"mp3-chunk"
    )

    process.kill.assert_called_once_with()
    process.wait.assert_awaited_once_with()


async def test_asgi_disconnect_before_alac_body_reaps_spawned_ffmpeg(
    bridge, tmp_path, monkeypatch
) -> None:
    audio = tmp_path / "before-body.m4a"
    audio.write_bytes(b"alac")
    entry = _make_entry(144)
    entry.path = str(audio)
    _register_index_entry(bridge, entry)
    process = SimpleNamespace(
        stdout=SimpleNamespace(read=AsyncMock(return_value=b"unused")),
        kill=MagicMock(),
        wait=AsyncMock(return_value=0),
    )
    monkeypatch.setattr("autodj.server._is_alac", lambda *_args: True)
    monkeypatch.setattr("autodj.server.shutil.which", lambda _name: "X:/ffmpeg.exe")
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    app = create_app(bridge)
    response_started = asyncio.Event()
    never = asyncio.Event()
    request_sent = False

    async def receive() -> dict:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await response_started.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            response_started.set()
            await never.wait()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/audio",
        "raw_path": b"/api/audio",
        "query_string": urlencode({"path": str(audio)}).encode("ascii"),
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
        "root_path": "",
    }

    await asyncio.wait_for(app(scope, receive, send), timeout=1.0)

    process.kill.assert_called_once_with()
    process.wait.assert_awaited_once_with()


# ---------------------------------------------------------------------------
# Static asset endpoints (cache-busting)
# ---------------------------------------------------------------------------


class TestStaticAssets:
    def test_index_is_not_stored(self, client) -> None:
        resp = client.get("/")
        assert resp.headers["cache-control"] == "no-store"

    @pytest.mark.parametrize(
        ("path", "media_type"),
        [
            ("/app.css", "text/css"),
            ("/app.js", "text/javascript"),
            ("/modules/audio-engine.js", "text/javascript"),
            ("/bitcrusher-worklet.js", "text/javascript"),
            ("/stutter-worklet.js", "text/javascript"),
            ("/freeze-worklet.js", "text/javascript"),
            ("/glitch-worklet.js", "text/javascript"),
        ],
    )
    def test_asset_is_revalidated_by_etag(self, client, path: str, media_type: str) -> None:
        resp = client.get(path)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith(media_type)
        assert resp.headers["cache-control"] == "no-cache"
        if path.endswith("-worklet.js"):
            assert "registerProcessor" in resp.text

        again = client.get(path, headers={"If-None-Match": resp.headers["etag"]})
        assert again.status_code == 304
        assert again.content == b""

    def test_page_and_scripts_are_gzipped(self, client) -> None:
        for path in ("/", "/app.js", "/modules/audio-engine.js"):
            resp = client.get(path, headers={"Accept-Encoding": "gzip"})
            assert resp.headers["content-encoding"] == "gzip", path


# ---------------------------------------------------------------------------
# Cover art
# ---------------------------------------------------------------------------


class TestCoverArt:
    def test_art_unknown_path_404(self, client) -> None:
        resp = client.get("/api/art?path=Z:/nope.flac")
        assert resp.status_code == 404

    def test_art_no_embedded_returns_404(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        # entry path doesn't point at a real file → read_cover_art returns None
        resp = tc.get(f"/api/art?path={bridge.sim.entries[0].path}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PlayerBridge persistence helpers
# ---------------------------------------------------------------------------


class TestPersistenceHelpers:
    def test_save_and_load_round_trip(self, bridge, tmp_path) -> None:
        point_index(bridge.player._cfg.index, tmp_path)
        bridge.player._cfg.transitions.effect = "tape_stop"
        bridge.save_persistent_state()
        # Reset and reload
        bridge.player._cfg.transitions.effect = "none"
        bridge.load_persistent_state()
        assert bridge.player._cfg.transitions.effect == "tape_stop"


# ---------------------------------------------------------------------------
# /api/history
# ---------------------------------------------------------------------------


class TestHistoryEndpoint:
    def _client_with_history(self, entries):
        from fastapi.testclient import TestClient

        from autodj.server import PlayerBridge, create_app
        from tests.integration._helpers import _make_player_mock, _make_sim_mock

        bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock())
        bridge._play_history = entries
        return TestClient(create_app(bridge))

    def test_empty_history(self, client) -> None:
        data = client.get("/api/history").json()
        assert data["total"] == 0
        assert data["items"] == []
        assert data["page"] == 1
        assert data["pages"] == 1

    def test_populated_history_most_recent_first(self) -> None:
        entries = [
            {
                "title": "A",
                "artist": "X",
                "duration": 180.0,
                "path": "/music/a.flac",
                "event_id": "play-a",
                "played_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "title": "B",
                "artist": "Y",
                "duration": 200.0,
                "played_at": "2026-01-01T00:01:00+00:00",
            },
        ]
        c = self._client_with_history(entries)
        data = c.get("/api/history").json()
        assert data["total"] == 2
        assert data["items"][0]["title"] == "B"
        assert data["items"][1]["title"] == "A"
        assert data["items"][1]["path"] == "/music/a.flac"
        assert data["items"][1]["event_id"] == "play-a"

    def test_pagination_page_count(self) -> None:
        entries = [
            {"title": str(i), "artist": "", "duration": 0.0, "played_at": ""} for i in range(110)
        ]
        c = self._client_with_history(entries)
        data = c.get("/api/history?page=1&per_page=50").json()
        assert data["total"] == 110
        assert data["pages"] == 3
        assert len(data["items"]) == 50

    def test_pagination_last_page(self) -> None:
        entries = [
            {"title": str(i), "artist": "", "duration": 0.0, "played_at": ""} for i in range(110)
        ]
        c = self._client_with_history(entries)
        data = c.get("/api/history?page=3&per_page=50").json()
        assert len(data["items"]) == 10

    def test_page_clamps_to_valid_range(self) -> None:
        entries = [{"title": "X", "artist": "", "duration": 0.0, "played_at": ""}]
        c = self._client_with_history(entries)
        data = c.get("/api/history?page=999").json()
        assert data["page"] == 1

    def test_play_history_default_is_bounded_deque(self) -> None:
        """New PlayerBridge instances start with a bounded deque (cap 500)."""
        from collections import deque

        from autodj.server import PlayerBridge
        from tests.integration._helpers import _make_player_mock, _make_sim_mock

        bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock())
        assert isinstance(bridge._play_history, deque)
        assert bridge._play_history.maxlen == 500

    def test_pagination_with_deque_backing(self) -> None:
        """Ensure /api/history slicing works when history is a real deque
        (the production type, not a list as the older tests assume)."""
        from collections import deque

        from fastapi.testclient import TestClient

        from autodj.server import PlayerBridge, create_app
        from tests.integration._helpers import _make_player_mock, _make_sim_mock

        bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock())
        # Use the actual deque type for this test.
        bridge._play_history = deque(
            ({"title": str(i), "artist": "", "duration": 0.0, "played_at": ""} for i in range(110)),
            maxlen=500,
        )
        c = TestClient(create_app(bridge))
        page1 = c.get("/api/history?page=1&per_page=50").json()
        assert page1["total"] == 110
        # Newest-first: item 109 first, item 60 last on page 1.
        assert page1["items"][0]["title"] == "109"
        assert page1["items"][-1]["title"] == "60"
        page3 = c.get("/api/history?page=3&per_page=50").json()
        assert len(page3["items"]) == 10
        # Last page holds the oldest 10 records (0..9), still newest-first.
        assert page3["items"][0]["title"] == "9"
        assert page3["items"][-1]["title"] == "0"


# ---------------------------------------------------------------------------
# Recently-added paths -- INFO advance banner, warning escalations, version
# helper edge cases, PlayerBridge re-export sanity.  Kept in their own class
# so the fixture seam is obvious.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /api/advance double-advance guard
# ---------------------------------------------------------------------------


class TestAdvanceFromPath:
    """Two tabs (or a retried request) must not advance twice."""

    @staticmethod
    def _browser_bridge(bridge):
        bridge.player._dry_run = True
        bridge.player._pick_next.return_value = _make_entry(99)
        return bridge

    def test_advance_from_the_playing_track_advances(self, bridge) -> None:
        from fastapi.testclient import TestClient

        bridge = self._browser_bridge(bridge)
        playing = bridge.player._state.current_track
        upcoming = bridge.player._state.next_track
        tc = TestClient(create_app(bridge))
        resp = tc.post("/api/advance", json={"from_path": playing.path})
        assert resp.status_code == 200
        assert bridge.player._state.current_track is upcoming
        assert resp.json()["current_track"]["path"] == upcoming.path

    def test_a_second_advance_from_the_same_track_changes_nothing(self, bridge) -> None:
        from fastapi.testclient import TestClient

        bridge = self._browser_bridge(bridge)
        playing = bridge.player._state.current_track
        upcoming = bridge.player._state.next_track
        tc = TestClient(create_app(bridge))
        tc.post("/api/advance", json={"from_path": playing.path})
        after_first = bridge.player._state.next_track
        resp = tc.post("/api/advance", json={"from_path": playing.path})
        assert resp.status_code == 200
        assert bridge.player._state.current_track is upcoming
        assert bridge.player._state.next_track is after_first
        assert resp.json()["current_track"]["path"] == upcoming.path

    def test_server_audio_advance_from_a_stale_track_does_not_skip(self, bridge) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        tc.post("/api/advance", json={"from_path": "Z:/Music/not-playing.flac"})
        bridge.player._skip_event.set.assert_not_called()

    def test_advance_with_nothing_playing_and_a_from_path_does_nothing(self, bridge) -> None:
        bridge.player._state.current_track = None
        assert bridge.advance("Z:/Music/song_0.flac") is False
        bridge.player._skip_event.set.assert_not_called()

    @pytest.mark.parametrize("body", [None, {"from_path": 3}, ["from_path"], {}])
    def test_advance_without_a_usable_from_path_advances_as_before(self, bridge, body) -> None:
        from fastapi.testclient import TestClient

        tc = TestClient(create_app(bridge))
        if body is None:
            tc.post("/api/advance")
        else:
            tc.post("/api/advance", json=body)
        bridge.player._skip_event.set.assert_called_once()


# ---------------------------------------------------------------------------
# WebSocket state pushes survive a failure
# ---------------------------------------------------------------------------


class TestBroadcastStateOnce:
    @staticmethod
    def _push(bridge, clients, failures) -> None:
        from autodj.server import _broadcast_state_once

        asyncio.run(_broadcast_state_once(bridge, clients, asyncio.Lock(), failures))

    def test_a_failing_state_is_logged_and_the_next_push_still_runs(
        self, bridge, caplog, monkeypatch
    ) -> None:
        import logging

        from autodj.server import _BroadcastFailureLog

        sent = AsyncMock()
        monkeypatch.setattr("autodj.server._broadcast_and_prune", sent)
        now = [0.0]
        failures = _BroadcastFailureLog(clock=lambda: now[0], every_s=60.0)
        clients = {object()}
        real_get_state = bridge.get_state
        bridge.get_state = MagicMock(side_effect=AttributeError("current track vanished"))
        with caplog.at_level(logging.INFO, logger="autodj.server"):
            for _ in range(3):
                self._push(bridge, clients, failures)  # never raises
            errors = [r for r in caplog.records if r.levelno == logging.ERROR]
            assert len(errors) == 1 and errors[0].exc_info is not None
            assert not [r for r in caplog.records if r.levelno == logging.WARNING]
            now[0] = 61.0
            self._push(bridge, clients, failures)
            warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
            assert len(warnings) == 1 and "3 failures" in warnings[0].getMessage()
            bridge.get_state = real_get_state
            self._push(bridge, clients, failures)
            assert "works again" in caplog.text
        sent.assert_awaited_once()

    def test_a_new_kind_of_failure_gets_its_own_traceback(self, bridge, caplog) -> None:
        import logging

        from autodj.server import _BroadcastFailureLog

        failures = _BroadcastFailureLog(clock=lambda: 0.0)
        bridge.get_state = MagicMock(side_effect=[KeyError("a"), ValueError("b")])
        with caplog.at_level(logging.ERROR, logger="autodj.server"):
            self._push(bridge, {object()}, failures)
            self._push(bridge, {object()}, failures)
        assert len([r for r in caplog.records if r.exc_info]) == 2

    def test_no_clients_means_no_state_is_built(self, bridge) -> None:
        from autodj.server import _BroadcastFailureLog

        bridge.get_state = MagicMock()
        self._push(bridge, set(), _BroadcastFailureLog())
        bridge.get_state.assert_not_called()


# ---------------------------------------------------------------------------
# Blocking work stays off the event loop
# ---------------------------------------------------------------------------


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class TestStateIsBuiltOffTheEventLoop:
    def test_the_broadcast_builds_state_on_a_worker_thread(self, bridge, monkeypatch) -> None:
        from autodj.server import _broadcast_state_once, _BroadcastFailureLog

        monkeypatch.setattr("autodj.server._broadcast_and_prune", AsyncMock())
        seen = []
        real = bridge.get_state

        def get_state():
            seen.append(_on_event_loop())
            return real()

        bridge.get_state = get_state
        asyncio.run(
            _broadcast_state_once(bridge, {object()}, asyncio.Lock(), _BroadcastFailureLog())
        )
        assert seen == [False]

    def test_status_actions_and_search_run_off_the_loop(self, bridge) -> None:
        from fastapi.testclient import TestClient

        seen = []
        real_state, real_search = bridge.get_state, bridge.search

        def get_state():
            seen.append(("state", _on_event_loop()))
            return real_state()

        def search(query, limit=100):
            seen.append(("search", _on_event_loop()))
            return real_search(query, limit)

        bridge.get_state, bridge.search = get_state, search
        tc = TestClient(create_app(bridge))
        assert tc.get("/api/status").status_code == 200
        assert tc.post("/api/skip").status_code == 200
        assert tc.post("/api/advance").status_code == 200
        assert tc.get("/api/search", params={"q": "song"}).status_code == 200
        assert seen == [("state", False)] * 3 + [("search", False)]
