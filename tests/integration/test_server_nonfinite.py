"""Non-finite floats must be rejected before they reach the player state.

pydantic accepts ``NaN``/``Infinity`` JSON tokens by default.  Once such a
value lands in the config the JSON encoder used by ``/api/status``, the
settings endpoints and the WebSocket broadcast raises, which takes the whole
web UI down until the process restarts.  Every numeric request body therefore
has to refuse them with a 4xx.
"""

from __future__ import annotations

import math

import pytest

_JSON = {"Content-Type": "application/json"}

_NON_FINITE = ("Infinity", "-Infinity", "NaN")


def _post_raw(client, path: str, body: str):
    """POST a raw JSON body so the non-finite literals survive encoding."""
    return client.post(path, content=body, headers=_JSON)


def _assert_status_healthy(client) -> None:
    """The state snapshot must still serialise after a rejected request."""
    resp = client.get("/api/status")
    assert resp.status_code == 200
    payload = resp.json()
    assert math.isfinite(payload["volume"])


@pytest.mark.parametrize("token", _NON_FINITE)
class TestNonFiniteRejected:
    def test_volume(self, client, token: str) -> None:
        resp = _post_raw(client, "/api/volume", f'{{"volume": {token}}}')
        assert resp.status_code == 422
        _assert_status_healthy(client)

    def test_seek_seconds(self, client, token: str) -> None:
        resp = _post_raw(client, "/api/seek", f'{{"seconds": {token}}}')
        assert resp.status_code == 422
        _assert_status_healthy(client)

    def test_seek_delta(self, client, token: str) -> None:
        resp = _post_raw(client, "/api/seek", f'{{"delta": {token}}}')
        assert resp.status_code == 422
        _assert_status_healthy(client)

    def test_eq(self, client, token: str) -> None:
        resp = _post_raw(client, "/api/eq", f'{{"low": {token}}}')
        assert resp.status_code == 422
        _assert_status_healthy(client)

    def test_crossfade_seconds(self, client, token: str) -> None:
        resp = _post_raw(client, "/api/playback-settings", f'{{"crossfade_seconds": {token}}}')
        assert resp.status_code == 422
        _assert_status_healthy(client)

    def test_liners_duck_db(self, client, token: str) -> None:
        resp = _post_raw(client, "/api/playback-settings", f'{{"liners_duck_db": {token}}}')
        assert resp.status_code == 422
        _assert_status_healthy(client)

    def test_bpm_range(self, client, token: str) -> None:
        resp = _post_raw(client, "/api/bpm-range", f'{{"lo": {token}, "hi": 200}}')
        assert resp.status_code == 422
        _assert_status_healthy(client)

    def test_profile_save(self, client, token: str) -> None:
        resp = _post_raw(client, "/api/profiles", f'{{"name": "p", "crossfade_seconds": {token}}}')
        assert resp.status_code == 422
        _assert_status_healthy(client)


def test_hand_edited_profile_with_non_finite_value_is_refused(bridge, tmp_path) -> None:
    """A profile file is plain JSON, which Python reads NaN and Infinity from."""
    from fastapi.testclient import TestClient

    from autodj.server import create_app

    bridge.player._cfg.index.active_dir = str(tmp_path / "idx")
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "p.json").write_text('{"name": "p", "crossfade_seconds": NaN}', encoding="utf-8")
    before = bridge.player._cfg.playback.crossfade_seconds

    resp = TestClient(create_app(bridge)).post("/api/profiles/p/apply")

    assert resp.status_code == 400
    assert bridge.player._cfg.playback.crossfade_seconds == before
