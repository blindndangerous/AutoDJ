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
        assert client.get("/api/settings").status_code == 200

    def test_mood_arc_hours(self, client, token: str) -> None:
        resp = _post_raw(client, "/api/playback-settings", f'{{"mood_arc_hours": {token}}}')
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


class TestBridgeRejectsNonFinite:
    def test_set_volume_keeps_previous(self, bridge) -> None:
        bridge.set_volume(0.5)
        bridge.set_volume(float("inf"))
        assert bridge.player._state.volume == pytest.approx(0.5)

    def test_set_volume_ignores_nan(self, bridge) -> None:
        bridge.set_volume(0.5)
        bridge.set_volume(float("nan"))
        assert bridge.player._state.volume == pytest.approx(0.5)

    def test_set_eq_ignores_non_finite(self, bridge) -> None:
        bridge.set_eq(low=1.5)
        bridge.set_eq(low=float("inf"), mid=float("nan"))
        eq = bridge.get_eq()
        assert eq["low"] == pytest.approx(1.5)
        assert math.isfinite(eq["mid"])

    def test_crossfade_seconds_ignores_infinity(self, bridge) -> None:
        cfg = bridge.player._cfg
        before = cfg.playback.crossfade_seconds
        bridge.set_playback_settings(crossfade_seconds=float("inf"))
        assert cfg.playback.crossfade_seconds == pytest.approx(before)

    def test_mood_arc_hours_ignores_nan(self, bridge) -> None:
        cfg = bridge.player._cfg
        before = cfg.playback.mood_arc_hours
        bridge.set_playback_settings(mood_arc_hours=float("nan"))
        assert cfg.playback.mood_arc_hours == pytest.approx(before)

    def test_liners_duck_db_ignores_infinity(self, bridge) -> None:
        cfg = bridge.player._cfg
        before = cfg.playback.liners_duck_db
        bridge.set_playback_settings(liners_duck_db=float("-inf"))
        assert cfg.playback.liners_duck_db == pytest.approx(before)

    def test_bpm_range_ignores_non_finite(self, bridge) -> None:
        bridge.set_bpm_range(100.0, 140.0)
        bridge.set_bpm_range(float("nan"), float("inf"))
        assert bridge.player._bpm_range is None

    def test_bpm_range_ignores_one_non_finite_bound(self, bridge) -> None:
        bridge.set_bpm_range(100.0, float("inf"))
        assert bridge.player._bpm_range is None
