"""Branch-coverage tests for autodj.server endpoints.

Targets validate_name 400 paths, profile-not-found 404 paths, liner
upload/delete edge cases, and other small uncovered branches.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Profile name validation 400 paths
# ---------------------------------------------------------------------------


class TestProfileValidateName:
    def test_get_invalid_name_returns_400(self, client) -> None:
        # Names with traversal / special chars trip validate_name -> 400
        resp = client.get("/api/profiles/..%2Fbad")
        assert resp.status_code in (400, 404)

    def test_get_unknown_name_returns_404(self, client, tmp_path: Path) -> None:
        resp = client.get("/api/profiles/no-such-profile-xyz")
        assert resp.status_code == 404

    def test_save_invalid_name_400(self, client) -> None:
        resp = client.post("/api/profiles", json={"name": "../escape", "preset": None})
        assert resp.status_code == 400

    def test_delete_invalid_name_400(self, client) -> None:
        resp = client.request("DELETE", "/api/profiles/..%2Fbad")
        # Some server stacks normalise %2F so the route may not even match.
        assert resp.status_code in (400, 404)

    def test_delete_unknown_returns_404(self, client) -> None:
        resp = client.request("DELETE", "/api/profiles/no-such-profile-xyz")
        assert resp.status_code == 404

    def test_apply_invalid_name_400(self, client) -> None:
        resp = client.post("/api/profiles/..%2Fbad/apply")
        assert resp.status_code in (400, 404)

    def test_apply_unknown_returns_404(self, client) -> None:
        resp = client.post("/api/profiles/no-such-profile-xyz/apply")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Liner upload / delete branches
# ---------------------------------------------------------------------------


class TestLinerEndpoints:
    def test_upload_bad_extension(self, client) -> None:
        resp = client.post(
            "/api/liners/upload",
            files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
        )
        assert resp.status_code == 400

    def test_upload_no_extension(self, client) -> None:
        resp = client.post(
            "/api/liners/upload",
            files={"file": ("noextension", b"data", "audio/wav")},
        )
        # No extension -> 400
        assert resp.status_code == 400

    def test_delete_unknown_liner_404(self, client) -> None:
        resp = client.delete("/api/liners/file/nope.wav")
        assert resp.status_code == 404

    def test_get_unknown_liner_404(self, client) -> None:
        resp = client.get("/api/liners/file/nope.wav")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cover art 404 paths
# ---------------------------------------------------------------------------


class TestArt:
    def test_unknown_track_404(self, client) -> None:
        resp = client.get("/api/art", params={"path": "Z:/no-such-track.flac"})
        assert resp.status_code == 404

    def test_known_track_no_art_404(self, client) -> None:
        resp = client.get("/api/art", params={"path": "Z:/Music/song_0.flac"})
        # bridge.cover_art_for likely returns None for the mock track
        assert resp.status_code == 404

    def test_known_track_with_art_returns_image(self, monkeypatch) -> None:
        # Mock cover_art_for to return real bytes so the FileResponse path
        # (lines 806-807) is exercised.
        from fastapi.testclient import TestClient

        from autodj.server import PlayerBridge, create_app

        from ._helpers import _make_player_mock, _make_sim_mock

        bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock())
        bridge.cover_art_for = lambda path: (b"PNG-bytes", "image/png")  # type: ignore[assignment]
        with TestClient(create_app(bridge)) as tc:
            resp = tc.get("/api/art", params={"path": "Z:/Music/song_0.flac"})
            assert resp.status_code == 200
            assert resp.content == b"PNG-bytes"
            assert resp.headers["content-type"] == "image/png"

    def test_cover_art_lookup_runs_off_event_loop(self, monkeypatch) -> None:
        from fastapi.testclient import TestClient

        from autodj.server import PlayerBridge, create_app

        from ._helpers import _make_player_mock, _make_sim_mock

        bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock())
        called: list[str] = []

        def _cover(path: str):
            called.append(path)
            return (b"JPEG-bytes", "image/jpeg")

        async def _fake_to_thread(fn, *args, **kwargs):
            called.append("to_thread")
            return fn(*args, **kwargs)

        bridge.cover_art_for = _cover  # type: ignore[assignment]
        monkeypatch.setattr("autodj.server.asyncio.to_thread", _fake_to_thread)

        with TestClient(create_app(bridge)) as tc:
            resp = tc.get("/api/art", params={"path": "Z:/Music/song_0.flac"})

        assert resp.status_code == 200
        assert called[-2:] == ["to_thread", "Z:/Music/song_0.flac"]


# ---------------------------------------------------------------------------
# Profile save round-trip
# ---------------------------------------------------------------------------


class TestProfileSaveRoundTrip:
    def test_save_then_get(self, client) -> None:
        body = {
            "name": "test-profile-1",
            "preset": None,
            "bpm_lo": 90,
            "bpm_hi": 130,
        }
        resp = client.post("/api/profiles", json=body)
        assert resp.status_code == 200
        # Should be retrievable
        get_resp = client.get(f"/api/profiles/{body['name']}")
        assert get_resp.status_code == 200
        assert get_resp.json()["name"] == body["name"]
        # Cleanup
        client.request("DELETE", f"/api/profiles/{body['name']}")

    def test_apply_round_trip(self, client) -> None:
        body = {"name": "apply-rt", "preset": None}
        client.post("/api/profiles", json=body)
        resp = client.post(f"/api/profiles/{body['name']}/apply")
        assert resp.status_code == 200
        assert "applied" in resp.json()
        client.request("DELETE", f"/api/profiles/{body['name']}")


# ---------------------------------------------------------------------------
# Module path traversal 404
# ---------------------------------------------------------------------------


class TestModuleTraversal:
    def test_traversal_attempt_404(self, client) -> None:
        # Try to escape /modules/ directory
        resp = client.get("/modules/..%2F..%2Fetc%2Fpasswd")
        assert resp.status_code in (400, 404)

    def test_unknown_module_404(self, client) -> None:
        resp = client.get("/modules/nonexistent.js")
        assert resp.status_code == 404

    def test_traversal_outside_modules_root_returns_404(self, client) -> None:
        # Use absolute or parent-traversal path that defeats relative_to.
        # On Windows, an absolute drive-letter path triggers the ValueError
        # branch (line 443-444) cleanly.
        resp = client.get("/modules/C:/Windows/System32/cmd.exe")
        assert resp.status_code in (400, 404)

    def test_modules_endpoint_serves_existing_module_when_present(self, client) -> None:
        """Best-effort: when the source static dir is the live one (no
        bundled static_dist on this checkout), confirm a real module file
        is served. CI has no static_dist so the response is 200; local
        builds with static_dist may 404 — both are acceptable.
        """
        resp = client.get("/modules/dom-helpers.js")
        # Either 200 (no bundle, real modules dir) or 404 (bundled, no /modules).
        assert resp.status_code in (200, 404)
        if resp.status_code == 200:
            assert "javascript" in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# Profile validate_name 400 paths via direct route (validate_name fires for
# names that survive URL routing but contain disallowed characters like '@').
# ---------------------------------------------------------------------------


class TestProfileBadCharsRouted:
    def test_get_bad_chars_returns_400(self, client) -> None:
        # '@' is rejected by validate_name; the route still matches.
        resp = client.get("/api/profiles/bad@name")
        assert resp.status_code == 400

    def test_delete_bad_chars_returns_400(self, client) -> None:
        resp = client.request("DELETE", "/api/profiles/bad@name")
        assert resp.status_code == 400

    def test_apply_bad_chars_returns_400(self, client) -> None:
        resp = client.post("/api/profiles/bad@name/apply")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Profile apply — exercise BPM range, harmonic mode, preset branches
# ---------------------------------------------------------------------------


class TestProfileApplyBranches:
    def test_apply_with_bpm_and_harmonic_and_preset(self, client) -> None:
        body = {
            "name": "branchcov-1",
            "preset": None,
            "bpm_lo": 90,
            "bpm_hi": 130,
            "harmonic_mode": "compatible",
        }
        client.post("/api/profiles", json=body)
        resp = client.post(f"/api/profiles/{body['name']}/apply")
        assert resp.status_code == 200
        applied = resp.json()["applied"]
        assert "bpm_range" in applied
        assert "harmonic_mode" in applied
        client.request("DELETE", f"/api/profiles/{body['name']}")

    def test_apply_with_preset_set(self, client) -> None:
        body = {
            "name": "branchcov-preset",
            "preset": "warmup",
        }
        client.post("/api/profiles", json=body)
        resp = client.post(f"/api/profiles/{body['name']}/apply")
        assert resp.status_code == 200
        # `preset` may or may not appear depending on whether the
        # built-in preset exists — but the contextlib.suppress branch is hit.
        client.request("DELETE", f"/api/profiles/{body['name']}")


# ---------------------------------------------------------------------------
# Liner upload + delete OSError + ALAC detection
# ---------------------------------------------------------------------------


class TestLinerUploadDelete:
    def test_upload_succeeds_and_delete_works(self, client, tmp_path, monkeypatch) -> None:
        # Point liners folder at tmp_path so writes are isolated.
        # Closure captures bridge.player._cfg.playback.liners_folder;
        # set that to redirect the liner folder.
        from fastapi.testclient import TestClient

        from autodj.server import PlayerBridge, create_app

        from ._helpers import _make_player_mock, _make_sim_mock

        player = _make_player_mock()
        player._cfg.playback.liners_folder = str(tmp_path)
        bridge = PlayerBridge(player=player, sim=_make_sim_mock())
        client = TestClient(create_app(bridge))
        resp = client.post(
            "/api/liners/upload",
            files={"file": ("clip.wav", b"RIFFwavedata", "audio/wav")},
        )
        assert resp.status_code == 200
        # Delete it to exercise the unlink success path.
        resp = client.delete("/api/liners/file/clip.wav")
        assert resp.status_code == 200

    def test_upload_path_with_slash_is_sanitised(self, client, tmp_path, monkeypatch) -> None:
        # Closure captures bridge.player._cfg.playback.liners_folder;
        # set that to redirect the liner folder.
        from fastapi.testclient import TestClient

        from autodj.server import PlayerBridge, create_app

        from ._helpers import _make_player_mock, _make_sim_mock

        player = _make_player_mock()
        player._cfg.playback.liners_folder = str(tmp_path)
        bridge = PlayerBridge(player=player, sim=_make_sim_mock())
        client = TestClient(create_app(bridge))
        # Server strips path components, so the file lands directly in folder.
        resp = client.post(
            "/api/liners/upload",
            files={"file": ("subdir/clip.wav", b"data", "audio/wav")},
        )
        assert resp.status_code == 200
        assert resp.json()["filename"] == "clip.wav"

    def test_delete_unlink_raises_oserror_returns_500(self, client, tmp_path, monkeypatch) -> None:
        from pathlib import Path as _P

        from fastapi.testclient import TestClient

        # Closure captures bridge.player._cfg.playback.liners_folder;
        # set that to redirect the liner folder.
        from autodj.server import PlayerBridge, create_app

        from ._helpers import _make_player_mock, _make_sim_mock

        player = _make_player_mock()
        player._cfg.playback.liners_folder = str(tmp_path)
        bridge = PlayerBridge(player=player, sim=_make_sim_mock())
        client = TestClient(create_app(bridge))
        # Create a real file then patch unlink to raise.
        (tmp_path / "doomed.wav").write_bytes(b"x")
        original_unlink = _P.unlink

        def _broken_unlink(self, *a, **kw):
            if self.name == "doomed.wav":
                raise OSError("locked")
            return original_unlink(self, *a, **kw)

        monkeypatch.setattr(_P, "unlink", _broken_unlink)
        resp = client.delete("/api/liners/file/doomed.wav")
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Library job snapshot success path
# ---------------------------------------------------------------------------


class TestLibraryJobRunSnapshot:
    def test_run_returns_snapshot_when_started(self, client, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from autodj import server as _srv

        mgr = MagicMock()
        mgr.start.return_value = True
        mgr.snapshot.return_value = {"running": True, "name": "stats"}
        monkeypatch.setattr("autodj.jobs.get_manager", lambda: mgr)
        # Need to re-trigger the inner closure import; just call route.
        resp = client.post(
            "/api/library/run",
            json={"name": "stats", "args": []},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "stats"
        _ = _srv
