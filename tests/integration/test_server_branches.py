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
